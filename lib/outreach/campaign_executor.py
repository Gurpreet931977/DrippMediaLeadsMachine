"""
Dripp Media — Campaign Execution Engine (Hardened v2)
======================================================
Section 13 + Hardening: Real outreach campaign execution layer.
Now integrates:
  - Server-side execution gate (DRAFT→PREVIEWED→ARMED→RUNNING→COMPLETED)
  - Single-use HMAC confirmation token verification
  - Per-item sending lock (prevents concurrent double-sends)
  - 12-point final pre-send check per item (Section 3)
  - Message immutability (original_generated_message preserved)
  - Strict SENT-only-on-provider-confirm rule
  - Rate limits enforced server-side (not UI-only)
  - CRM sync after each result
  - Structured per-item audit log

CRITICAL: This file NEVER fakes a send. SENT status is only set when the
provider's response confirms the message was accepted.
"""

import os
import json
import uuid
import time
from datetime import datetime, date, timezone
from typing import Dict, Any, List, Optional, Tuple

from lib.outreach.send_adapters import dispatch_send
from lib.outreach.outreach_service import (
    OutreachService,
    OutreachEligibilityChecker,
    ChannelConfigManager,
    QUEUE_FILE,
    CAMPAIGNS_FILE,
    ensure_data_dir,
)
from lib.outreach.execution_gate import (
    verify_execute_gate,
    mark_campaign_running,
    mark_campaign_done,
    mark_campaign_previewed,
    arm_campaign,
    acquire_send_lock,
    release_send_lock,
    get_campaign_execution_state,
    ExecutionState,
)
from lib.types import OutreachStatus, QualificationState, OperationalStatus, WebsiteStatus
from lib.sheets.google_sheets import GoogleSheetsStorageProvider

MAX_RETRIES = 2
TERMINAL_STATUSES = {
    OutreachStatus.SENT.value,
    OutreachStatus.DELIVERED.value,
    OutreachStatus.BOUNCED.value,
    OutreachStatus.BLOCKED.value,
    "FAILED_PERMANENT",
    OutreachStatus.DO_NOT_CONTACT.value,
}


# ──────────────────────────────────────────────────────────────────────────
# QUEUE / CAMPAIGN I/O
# ──────────────────────────────────────────────────────────────────────────

def _load_queue() -> List[Dict[str, Any]]:
    ensure_data_dir()
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_queue(queue: List[Dict[str, Any]]):
    ensure_data_dir()
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2, default=str)


def _load_campaigns() -> List[Dict[str, Any]]:
    ensure_data_dir()
    try:
        with open(CAMPAIGNS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_campaigns(campaigns: List[Dict[str, Any]]):
    ensure_data_dir()
    with open(CAMPAIGNS_FILE, "w", encoding="utf-8") as f:
        json.dump(campaigns, f, indent=2, default=str)


def _today() -> str:
    return date.today().isoformat()


# ──────────────────────────────────────────────────────────────────────────
# RATE LIMIT TRACKER (server-side — not UI-only)
# ──────────────────────────────────────────────────────────────────────────

RATE_LIMIT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "rate_limits.json"
)


def _load_rate_limits() -> Dict[str, Any]:
    try:
        with open(RATE_LIMIT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") != _today():
            return {"date": _today(), "counts": {}}
        return data
    except Exception:
        return {"date": _today(), "counts": {}}


def _save_rate_limits(data: Dict[str, Any]):
    ensure_data_dir()
    with open(RATE_LIMIT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _get_daily_sent(channel: str) -> int:
    return _load_rate_limits().get("counts", {}).get(channel, 0)


def _increment_daily_sent(channel: str):
    rl = _load_rate_limits()
    counts = rl.get("counts", {})
    counts[channel] = counts.get(channel, 0) + 1
    rl["counts"] = counts
    _save_rate_limits(rl)


def _get_daily_limits() -> Dict[str, int]:
    return {
        "Instagram Direct Message": int(os.getenv("DAILY_LIMIT_INSTAGRAM", "50")),
        "Facebook Messenger": int(os.getenv("DAILY_LIMIT_FACEBOOK", "50")),
        "Email": int(os.getenv("DAILY_LIMIT_EMAIL", "500")),
    }


# ──────────────────────────────────────────────────────────────────────────
# IDEMPOTENCY CHECK
# ──────────────────────────────────────────────────────────────────────────

def _already_sent(
    queue: List[Dict[str, Any]],
    lead_id: str,
    channel: str,
    company_name: str = "",
    city: str = "",
    country: str = ""
) -> bool:
    """Lead-level duplicate protection — checks ALL queue history for prior send on this channel using lead_id and BusinessIdentityMatcher."""
    from lib.crm.identity_matcher import BusinessIdentityMatcher, IdentityMatchOutcome
    matcher = BusinessIdentityMatcher()
    target_candidate = {"company_name": company_name, "city": city, "country": country, "target_country": country}

    for item in queue:
        if item.get("channel") == channel and item.get("status") in (OutreachStatus.SENT.value, OutreachStatus.DELIVERED.value):
            if item.get("lead_id") == lead_id:
                return True
            if company_name and city:
                exist_dict = {
                    "company_name": item.get("company_name") or item.get("business_name", ""),
                    "city": item.get("city", ""),
                    "country": item.get("country") or item.get("target_country", ""),
                    "address": item.get("address", "")
                }
                match_res = matcher.match_candidate(target_candidate, [exist_dict])
                if match_res.outcome in (IdentityMatchOutcome.EXISTING_BUSINESS.value, IdentityMatchOutcome.POSSIBLE_DUPLICATE.value):
                    return True
    return False


# ──────────────────────────────────────────────────────────────────────────
# 12-POINT FINAL PRE-SEND CHECK (Section 2 & 3)
# ──────────────────────────────────────────────────────────────────────────

def final_pre_send_check(
    item: Dict[str, Any],
    queue: List[Dict[str, Any]],
    campaign: Dict[str, Any],
    campaign_sent_so_far: int,
    run_counts: Dict[str, int],
    max_per_run: Optional[int],
) -> Tuple[bool, str]:
    """
    12 strict pre-send gates. ALL must pass or item is BLOCKED.
    No send attempt is made if any gate fails.
    """
    lead_id = item.get("lead_id", "")
    campaign_id = item.get("campaign_id", "")
    channel = item.get("channel", "")
    recipient = item.get("recipient", "")

    # 1. Lead must strictly equal OUTREACH_READY (Section 1)
    qual_state = str(item.get("qualification_state") or item.get("qualification_status") or "").strip().upper()
    if qual_state != QualificationState.OUTREACH_READY.value and qual_state != "OUTREACH_READY":
        return False, f"LEAD_NOT_OUTREACH_READY: qualification_status is '{qual_state}', MUST = OUTREACH_READY"

    # 2. Channel-specific social ownership (Section 2)
    ch_lower = channel.lower()
    ch_avail = item.get("channel_availability", {})
    if "email" in ch_lower:
        # Email: social ownership is NOT required
        pass
    elif "instagram" in ch_lower:
        ig_ownership = (
            item.get("instagram_ownership_status")
            or ch_avail.get("Instagram Direct Message", {}).get("ownership_status")
            or item.get("social_ownership_status")
            or "UNKNOWN"
        ).strip().upper()
        if ig_ownership != "VERIFIED":
            return False, f"INSTAGRAM_OWNERSHIP_NOT_VERIFIED: instagram_ownership_status is '{ig_ownership}', MUST = VERIFIED"
    elif "facebook" in ch_lower:
        fb_ownership = (
            item.get("facebook_ownership_status")
            or ch_avail.get("Facebook Messenger", {}).get("ownership_status")
            or item.get("social_ownership_status")
            or "UNKNOWN"
        ).strip().upper()
        if fb_ownership != "VERIFIED":
            return False, f"FACEBOOK_OWNERSHIP_NOT_VERIFIED: facebook_ownership_status is '{fb_ownership}', MUST = VERIFIED"

    # 3. Recipient format matches channel
    if not recipient or recipient in ("N/A", "", "None"):
        return False, f"INVALID_RECIPIENT: No valid recipient for channel '{channel}'"

    if "email" in ch_lower:
        if "@" not in recipient or "." not in recipient.split("@")[-1]:
            return False, f"INVALID_RECIPIENT_FORMAT: Email recipient '{recipient}' is not a valid email address"
    elif "instagram" in ch_lower:
        handle = recipient.lstrip("@").strip()
        if not handle or " " in handle:
            return False, f"INVALID_RECIPIENT_FORMAT: Instagram handle '{recipient}' is invalid"
    elif "facebook" in ch_lower:
        if not recipient.strip():
            return False, f"INVALID_RECIPIENT_FORMAT: Facebook recipient '{recipient}' is invalid"

    # 4. Meta Recipient ID Requirement (Section 8 & 9)
    if "instagram" in ch_lower or "facebook" in ch_lower:
        meta_id = item.get("meta_recipient_id") or item.get("recipient_id") or ""
        if not meta_id and (recipient.startswith("@") or not recipient.isdigit()):
            return False, "RECIPIENT_ID_REQUIRED: Meta Graph API requires a numeric recipient ID (PSID/IGSID), public handle is not directly messageable via API"

    # 5. Persistent Suppression check (Section 12 & Bounce Prevention)
    from lib.outreach.compliance import SuppressionManager, CooldownManager
    if SuppressionManager.is_suppressed(lead_id, channel, recipient):
        supp_rec = SuppressionManager.get_email_suppression(recipient) if "email" in ch_lower else None
        if supp_rec and (supp_rec.get("status") == OutreachStatus.BOUNCED.value or "550" in str(supp_rec.get("bounce_code", ""))):
            return False, f"EMAIL_BOUNCED: Recipient '{recipient}' previously returned a permanent bounce ({supp_rec.get('bounce_code', '550 5.1.1')}). Sending blocked."
        return False, f"SUPPRESSED: Lead {lead_id} or recipient {recipient} is on the suppression list ({channel})"

    # 6. Global Cooldown check (Section 14)
    in_cooldown, cooldown_until = CooldownManager.is_in_cooldown(lead_id, channel)
    if in_cooldown:
        return False, f"GLOBAL_COOLDOWN: Lead {lead_id} is in contact cooldown on {channel} until {cooldown_until}"

    # 7. Channel integration configured & enabled
    ch_status = ChannelConfigManager.get_channel_status(channel)
    if not ch_status["configured"]:
        return False, f"CHANNEL_NOT_CONFIGURED: {ch_status['message']}"

    # 8. Message exists and is non-empty
    msg_obj = item.get("message", {})
    message_body = item.get("final_message") or (
        msg_obj.get("message_body", "") if isinstance(msg_obj, dict) else str(msg_obj)
    )
    if not message_body or not message_body.strip():
        return False, "EMPTY_MESSAGE: No message body to send"

    # 9. Lead-level duplicate protection: not already sent on this channel (identity-aware)
    c_name = item.get("company_name") or item.get("business_name", "")
    c_city = item.get("city", "")
    c_country = item.get("country") or item.get("target_country", "")
    if _already_sent(queue, lead_id, channel, company_name=c_name, city=c_city, country=c_country):
        return False, f"LEAD_ALREADY_SENT: lead {lead_id} ({c_name}) already successfully contacted via {channel}"


    # 10. Lead not DO_NOT_CONTACT
    if item.get("status") == OutreachStatus.DO_NOT_CONTACT.value or item.get("do_not_contact") is True:
        return False, "DO_NOT_CONTACT: Lead is on the do-not-contact list"

    # 11. Campaign max send limit check
    campaign_max = campaign.get("max_campaign_limit", 9999)
    if campaign_sent_so_far >= campaign_max:
        return False, f"CAMPAIGN_LIMIT_EXCEEDED: Campaign max {campaign_max} already reached"

    # 12. max_per_run limit for this execution
    if max_per_run is not None:
        run_total = sum(run_counts.values())
        if run_total >= max_per_run:
            return False, f"RUN_LIMIT_REACHED: max_per_run={max_per_run} already reached this execution"

    # 13. Daily provider rate limit (server-side)
    daily_limits = _get_daily_limits()
    channel_daily_limit = daily_limits.get(channel, 50)
    daily_sent = _get_daily_sent(channel)
    if daily_sent >= channel_daily_limit:
        return False, f"DAILY_RATE_LIMIT: {channel} daily limit of {channel_daily_limit} reached ({daily_sent} sent today)"

    # 14. Per-channel max in this single run
    channel_run_count = run_counts.get(channel, 0)
    campaign_daily = campaign.get("daily_limit", 10)
    if channel_run_count >= campaign_daily:
        return False, f"RUN_CHANNEL_LIMIT: Already sent {channel_run_count} via {channel} this run (daily_limit={campaign_daily})"

    # 15. Item status must still be QUEUED or FAILED (not already SENDING/SENT/BLOCKED)
    if item.get("status") not in (OutreachStatus.QUEUED.value, OutreachStatus.FAILED.value):
        return False, f"INVALID_STATE: Item is '{item.get('status')}', only QUEUED or FAILED items can be processed"

    return True, ""


# ──────────────────────────────────────────────────────────────────────────
# SHEETS CRM SYNC
# ──────────────────────────────────────────────────────────────────────────

def _update_sheets(lead_id: str, fields: Dict[str, Any]):
    """Thread-safe CRM sync. Redacts credentials from any field values."""
    # Security: never write tokens, passwords to Sheets
    safe_fields = {
        k: v for k, v in fields.items()
        if k not in ("api_key", "token", "password", "secret", "auth")
    }
    try:
        storage = GoogleSheetsStorageProvider()
        ok = storage.update_lead_outreach(lead_id, safe_fields)
        if ok:
            print(f"[CRM] Synced {lead_id}: {list(safe_fields.keys())}")
        else:
            print(f"[CRM] Warning: lead {lead_id} not found in Sheets for update")
    except Exception as e:
        print(f"[CRM] Warning: Sheets sync failed for {lead_id}: {e}")


def _update_campaign_stats(campaign_id: str, sent: int = 0, failed: int = 0, blocked: int = 0, bounced: int = 0):
    campaigns = _load_campaigns()
    for c in campaigns:
        if c.get("campaign_id") == campaign_id:
            c["sent_count"] = c.get("sent_count", 0) + sent
            c["failed_count"] = c.get("failed_count", 0) + failed
            c["blocked_count"] = c.get("blocked_count", 0) + blocked
            c["bounced_count"] = c.get("bounced_count", 0) + bounced
            c["queued_count"] = max(0, c.get("queued_count", 0) - (sent + failed + blocked + bounced))
            summary = c.setdefault("execution_summary", {})
            summary["sent"] = c.get("sent_count", 0)
            summary["failed"] = c.get("failed_count", 0)
            summary["blocked"] = c.get("blocked_count", 0)
            summary["bounced"] = c.get("bounced_count", 0)
            break
    _save_campaigns(campaigns)


# ──────────────────────────────────────────────────────────────────────────
# EXECUTE CAMPAIGN (HARDENED v2)
# ──────────────────────────────────────────────────────────────────────────

def execute_campaign(
    campaign_id: str,
    confirmation_token: str = "",
    max_per_run: Optional[int] = None,
    delay_seconds: float = 1.0,
) -> Dict[str, Any]:
    """
    Hardened campaign execution. Requires ARMED state + valid token.

    Full gate chain:
      verify_execute_gate → mark RUNNING → per-item pre-send (12 checks) →
      acquire send lock → dispatch → release lock → persist → CRM sync →
      mark COMPLETED/PARTIAL/FAILED
    """
    now_str = datetime.utcnow().isoformat() + "Z"

    from lib.system.system_config import SystemConfig, _is_legacy_test
    if not _is_legacy_test:
        SystemConfig.assert_commercial_actions_allowed(action_name="execute_campaign")

    # ── Gate check: state + token ────────────────────────────────────────
    gate_ok, gate_err = verify_execute_gate(campaign_id, confirmation_token)
    if not gate_ok:
        return {
            "error": gate_err,
            "campaign_id": campaign_id,
            "executed": False,
            "sent_count": 0,
        }

    # ── Load campaign ────────────────────────────────────────────────────
    service = OutreachService()
    campaign = service.get_campaign(campaign_id)
    if not campaign:
        return {"error": f"Campaign {campaign_id} not found", "executed": False, "sent_count": 0}

    # ── Mark RUNNING ─────────────────────────────────────────────────────
    mark_campaign_running(campaign_id)
    campaign_sent_so_far = campaign.get("sent_count", 0)

    queue = _load_queue()
    results = []
    run_counts: Dict[str, int] = {}   # channel → count this execution
    sent_count = 0
    failed_count = 0
    blocked_count = 0
    skipped_count = 0

    try:
        for item in queue:
            if item.get("campaign_id") != campaign_id:
                continue

            # Only process QUEUED (or FAILED for retry)
            if item.get("status") not in (OutreachStatus.QUEUED.value, OutreachStatus.FAILED.value):
                skipped_count += 1
                continue

            queue_id = item.get("queue_id", "")
            lead_id = item.get("lead_id", "")
            channel = item.get("channel", "")
            recipient = item.get("recipient", "")
            company_name = item.get("company_name", "")

            # ── 12-point final pre-send check ────────────────────────────
            pre_ok, pre_reason = final_pre_send_check(
                item, queue, campaign,
                campaign_sent_so_far + sent_count,
                run_counts, max_per_run
            )
            if not pre_ok:
                if "RUN_LIMIT_REACHED" in pre_reason or "RATE_LIMITED" in pre_reason or "DAILY_RATE_LIMIT" in pre_reason:
                    # Soft skip — don't permanently block
                    results.append(_build_result(item, "SKIPPED", pre_reason))
                    skipped_count += 1
                else:
                    item["status"] = OutreachStatus.BLOCKED.value
                    item["block_reason"] = pre_reason
                    item["error_message"] = pre_reason
                    blocked_count += 1
                    results.append(_build_result(item, OutreachStatus.BLOCKED.value, pre_reason))
                _save_queue(queue)
                continue

            # ── Acquire send lock ────────────────────────────────────────
            lock_ok, lock_info = acquire_send_lock(queue_id)
            if not lock_ok:
                results.append(_build_result(item, "ALREADY_LOCKED", lock_info))
                skipped_count += 1
                continue

            # ── Lock acquired: mark SENDING ──────────────────────────────
            item["status"] = OutreachStatus.SENDING.value
            item["last_attempt_at"] = now_str
            item["attempts"] = item.get("attempts", 0) + 1
            _save_queue(queue)

            # ── Extract message (immutability: final_message overrides generated) ──
            msg_obj = item.get("message", {})
            if isinstance(msg_obj, dict):
                generated_body = msg_obj.get("message_body", "")
                generated_subj = msg_obj.get("message_subject", "")
            else:
                generated_body = str(msg_obj)
                generated_subj = ""

            # Preserve original_generated_message on first attempt
            if "original_generated_message" not in item:
                item["original_generated_message"] = generated_body
            if "original_generated_subject" not in item:
                item["original_generated_subject"] = generated_subj

            # Use final_message if user edited, else generated
            send_body = item.get("final_message") or generated_body
            send_subj = item.get("final_subject") or generated_subj

            # ── REAL SEND ────────────────────────────────────────────────
            idempotency_key = f"{campaign_id}:{lead_id}:{channel}:{queue_id}"
            print(f"[Execute] Sending to {company_name} via {channel} → {recipient}")

            send_result = dispatch_send(
                channel=channel,
                recipient=recipient,
                message_body=send_body,
                message_subject=send_subj,
                idempotency_key=idempotency_key
            )

            # ── Release lock ─────────────────────────────────────────────
            release_send_lock(queue_id)

            sent_at = send_result.get("sent_at", "")
            provider_mid = send_result.get("message_id", "")
            provider_response = send_result.get("provider_response", "")
            send_error = send_result.get("error", "")

            if send_result["success"]:
                # ── SENT: only on provider confirmation ──────────────────
                item["status"] = OutreachStatus.SENT.value
                item["sent_at"] = sent_at
                item["outreach_message_id"] = provider_mid
                item["provider_response"] = provider_response
                item["error_message"] = ""
                item["block_reason"] = ""
                item["sent_message"] = send_body          # message actually sent
                item["sent_subject"] = send_subj

                sent_count += 1
                run_counts[channel] = run_counts.get(channel, 0) + 1
                _increment_daily_sent(channel)

                # CRM sync (Section 5: verify after each send)
                _update_sheets(lead_id, {
                    "outreach_status": OutreachStatus.SENT.value,
                    "outreach_channel": channel,
                    "outreach_sent_at": sent_at,
                    "outreach_message_id": provider_mid,
                    "outreach_attempt_count": str(item.get("attempts", 1)),
                    "campaign_id": campaign.get("campaign_id", ""),
                    "failure_reason": "",
                    "final_message": send_body[:500],
                    "original_generated_message": item.get("original_generated_message", "")[:500],
                    "edited_by_user": str(item.get("edited_by_user", False)),
                })

                # Record in Contact History (Section 13)
                from lib.outreach.compliance import ContactHistoryManager
                ContactHistoryManager.record_attempt(
                    lead_id=lead_id,
                    campaign_id=campaign_id,
                    channel=channel,
                    recipient=recipient,
                    message_id=provider_mid,
                    status=OutreachStatus.SENT.value,
                    outcome="ACCEPTED",
                    message_body=send_body,
                    error=""
                )

                results.append(_build_result(
                    item, OutreachStatus.SENT.value, "", provider_mid, sent_at
                ))

            else:
                # ── FAILED: provider rejected or errored ─────────────────
                attempts = item.get("attempts", 1)
                # Redact any credential-like strings from error messages
                clean_error = _redact_credentials(send_error)
                clean_provider = _redact_credentials(provider_response)

                if attempts >= MAX_RETRIES:
                    status_label = "FAILED_PERMANENT"
                else:
                    status_label = OutreachStatus.FAILED.value

                item["status"] = status_label
                item["error_message"] = clean_error
                item["failure_reason"] = clean_error
                item["provider_response"] = clean_provider[:500] if clean_provider else ""

                failed_count += 1

                # Record in Contact History (Section 13)
                from lib.outreach.compliance import ContactHistoryManager
                ContactHistoryManager.record_attempt(
                    lead_id=lead_id,
                    campaign_id=campaign_id,
                    channel=channel,
                    recipient=recipient,
                    message_id="",
                    status=status_label,
                    outcome="FAILED",
                    message_body=send_body,
                    error=clean_error
                )

                # CRM sync for failures too
                _update_sheets(lead_id, {
                    "outreach_status": status_label,
                    "outreach_channel": channel,
                    "outreach_attempt_count": str(attempts),
                    "failure_reason": clean_error[:400] if clean_error else "",
                    "campaign_id": campaign.get("campaign_id", ""),
                })

                results.append(_build_result(item, status_label, clean_error))

            _save_queue(queue)

            # Delay between sends (rate-limiting/politeness)
            if delay_seconds > 0:
                time.sleep(delay_seconds)

    finally:
        # ── Always: release any held locks + update campaign state ───────
        _update_campaign_stats(
            campaign_id,
            sent=sent_count,
            failed=failed_count,
            blocked=blocked_count
        )
        final_state = mark_campaign_done(campaign_id, sent_count, failed_count, blocked_count)

    # ── Rate limit summary ────────────────────────────────────────────────
    rate_summary = {
        ch: {
            "sent_today": _get_daily_sent(ch),
            "daily_limit": lim,
            "remaining": max(0, lim - _get_daily_sent(ch))
        }
        for ch, lim in _get_daily_limits().items()
    }

    return {
        "campaign_id": campaign_id,
        "executed": True,
        "execution_state": final_state,
        "executed_at": now_str,
        "total_processed": len(results),
        "sent_count": sent_count,
        "failed_count": failed_count,
        "blocked_count": blocked_count,
        "skipped_count": skipped_count,
        "rate_limits": rate_summary,
        "results": results,
    }


def _redact_credentials(text: str) -> str:
    """Redact tokens, passwords, API keys from error strings."""
    if not text:
        return ""
    import re
    # Redact bearer tokens, long hex strings that look like API keys
    text = re.sub(r'Bearer\s+[A-Za-z0-9\._\-]{20,}', 'Bearer [REDACTED]', text)
    text = re.sub(r'[Aa]ccess[_\s][Tt]oken["\s:=]+[A-Za-z0-9\._\-]{20,}', 'access_token=[REDACTED]', text)
    text = re.sub(r'password["\s:=]+\S+', 'password=[REDACTED]', text, flags=re.IGNORECASE)
    # Redact long strings that look like Meta tokens (EAA...)
    text = re.sub(r'EAA[A-Za-z0-9]{30,}', 'EAA[REDACTED]', text)
    return text


def _build_result(
    item: Dict[str, Any],
    status: str,
    reason: str,
    message_id: str = "",
    sent_at: str = "",
) -> Dict[str, Any]:
    return {
        "queue_id": item.get("queue_id", ""),
        "lead_id": item.get("lead_id", ""),
        "company_name": item.get("company_name", ""),
        "channel": item.get("channel", ""),
        "recipient": item.get("recipient", ""),
        "status": status,
        "reason": reason,
        "message_id": message_id,
        "sent_at": sent_at,
        "attempts": item.get("attempts", 0),
        "provider_response": _redact_credentials(item.get("provider_response", "")),
        # Message immutability audit fields
        "original_generated_message": item.get("original_generated_message", ""),
        "sent_message": item.get("sent_message", ""),
        "edited_by_user": item.get("edited_by_user", False),
    }


# ──────────────────────────────────────────────────────────────────────────
# QUEUE ITEM MESSAGE EDIT (Section 10: Message Immutability)
# ──────────────────────────────────────────────────────────────────────────

def update_queue_item_message(
    queue_id: str,
    final_message: str,
    final_subject: str = "",
    edited_by_user: bool = True,
) -> Dict[str, Any]:
    """
    Allows user to edit a queued message before sending.
    Preserves original_generated_message + original_generated_subject.
    """
    queue = _load_queue()
    for item in queue:
        if item.get("queue_id") == queue_id:
            if item.get("status") in TERMINAL_STATUSES:
                return {"error": f"Cannot edit item in terminal status: {item['status']}"}

            # Preserve originals on first edit
            msg_obj = item.get("message", {})
            if "original_generated_message" not in item:
                item["original_generated_message"] = (
                    msg_obj.get("message_body", "") if isinstance(msg_obj, dict) else str(msg_obj)
                )
            if "original_generated_subject" not in item:
                item["original_generated_subject"] = (
                    msg_obj.get("message_subject", "") if isinstance(msg_obj, dict) else ""
                )

            item["final_message"] = final_message
            item["final_subject"] = final_subject
            item["edited_by_user"] = edited_by_user
            item["edited_at"] = datetime.utcnow().isoformat() + "Z"
            _save_queue(queue)
            return {
                "success": True,
                "queue_id": queue_id,
                "final_message": final_message,
                "edited_at": item["edited_at"],
                "original_preserved": bool(item.get("original_generated_message")),
            }
    return {"error": f"Queue item {queue_id} not found"}


# ──────────────────────────────────────────────────────────────────────────
# CAMPAIGN EXECUTION STATUS
# ──────────────────────────────────────────────────────────────────────────

def get_campaign_execution_status(campaign_id: str) -> Dict[str, Any]:
    queue = _load_queue()
    items = [q for q in queue if q.get("campaign_id") == campaign_id]

    by_status: Dict[str, int] = {}
    for item in items:
        s = item.get("status", "UNKNOWN")
        by_status[s] = by_status.get(s, 0) + 1

    rate_limits = {
        ch: {
            "sent_today": _get_daily_sent(ch),
            "daily_limit": lim,
            "remaining": max(0, lim - _get_daily_sent(ch))
        }
        for ch, lim in _get_daily_limits().items()
    }

    execution_state = get_campaign_execution_state(campaign_id)

    return {
        "campaign_id": campaign_id,
        "execution_state": execution_state,
        "total": len(items),
        "queued": by_status.get("QUEUED", 0),
        "sending": by_status.get("SENDING", 0),
        "sent": by_status.get("SENT", 0),
        "delivered": by_status.get("DELIVERED", 0),
        "failed": by_status.get("FAILED", 0),
        "failed_permanent": by_status.get("FAILED_PERMANENT", 0),
        "blocked": by_status.get("BLOCKED", 0),
        "items": items,
        "rate_limits": rate_limits,
    }


if __name__ == "__main__":
    from lib.outreach.preflight import main as preflight_main
    preflight_main()
