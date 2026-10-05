"""
Dripp Media — Bounce Detection, Processing & Email Suppression Engine
======================================================================
Real-world delivery feedback handler:
  1. Distinguishes INVALID (pre-send technical failure) from BOUNCED (post-send delivery rejection).
  2. Detects permanent bounces (550, 5.1.1, user unknown, mailbox not found).
  3. Enforces zero retry for permanent recipient failures (550 5.1.1).
  4. Creates persistent email suppression record (survives reruns, restarts, enrichment).
  5. Suppresses the SPECIFIC EMAIL ADDRESS, not the business entity (business remains OUTREACH_READY).
  6. Preserves immutable contact history timeline (SENT -> BOUNCED) with timestamps.
  7. Updates campaign analytics (SENT, BOUNCED, FAILED, BLOCKED).
  8. Synchronizes updates to Google Sheets LEADS worksheet without creating duplicate rows.
  9. Redacts sensitive credentials / tokens from all bounce logs and storage.
"""

import os
import re
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from lib.types import OutreachStatus, QualificationState
from lib.outreach.email_enricher import EmailVerificationStatus
from lib.outreach.contactability import ContactabilityState
from lib.outreach.compliance import SuppressionManager, ContactHistoryManager, DATA_DIR
from lib.sheets.google_sheets import GoogleSheetsStorageProvider

QUEUE_FILE = os.path.join(DATA_DIR, "outreach_queue.json")
CAMPAIGNS_FILE = os.path.join(DATA_DIR, "campaigns.json")

# Standard permanent delivery failure indicators
PERMANENT_BOUNCE_CODES = {"550", "551", "552", "553", "554"}
PERMANENT_ENHANCED_CODES = {"5.1.1", "5.1.2", "5.1.3", "5.1.6", "5.2.1", "5.7.1"}

PERMANENT_PATTERNS = [
    r"550\s+5\.1\.1",
    r"account\s+that\s+you\s+tried\s+to\s+reach\s+does\s+not\s+exist",
    r"mailbox\s+(?:not\s+found|unavailable|does\s+not\s+exist)",
    r"user\s+(?:unknown|does\s+not\s+exist|not\s+found)",
    r"no\s+such\s+user",
    r"recipient\s+(?:address\s+)?rejected",
    r"address\s+(?:does\s+not\s+exist|rejected|unknown)",
    r"5\.1\.1\s+bad\s+destination\s+mailbox",
]

TEMPORARY_PATTERNS = [
    r"^4\d{2}",
    r"4\.\d+\.\d+",
    r"try\s+again\s+later",
    r"mailbox\s+full",
    r"temporarily\s+deferred",
    r"greylisted",
    r"rate\s+limit(?:ed)?",
]


def redact_credentials(text: str) -> str:
    """Redacts passwords, tokens, API keys, and sensitive auth data from text."""
    if not text:
        return ""
    s = str(text)
    # Redact common credential patterns
    s = re.sub(r'(?i)(password|passwd|secret|token|api[_-]?key|auth)\s*[:=]\s*[^\s,;]+', r'\1=***REDACTED***', s)
    s = re.sub(r'Bearer\s+[a-zA-Z0-9_\-\.]+', 'Bearer ***REDACTED***', s)
    s = re.sub(r'AIza[0-9A-Za-z-_]{35}', '***REDACTED_API_KEY***', s)
    return s


class BounceManager:
    """
    Manages delivery feedback, permanent bounce classification,
    persistent email suppression, and CRM synchronization.
    """

    @classmethod
    def is_permanent_bounce(cls, bounce_code: str = "", bounce_reason: str = "", raw_reason: str = "") -> bool:
        """
        Determines whether delivery feedback indicates a permanent recipient failure
        (e.g., 550 5.1.1 "The email account that you tried to reach does not exist")
        vs a temporary retryable failure (e.g., 421 / 451).
        """
        full_text = f"{bounce_code} {bounce_reason} {raw_reason}".lower()

        # Check explicit temporary patterns first
        for temp_pat in TEMPORARY_PATTERNS:
            if re.search(temp_pat, full_text):
                # If it's specifically a 550 5.1.1, 550 overrides any generic temporary text
                if "550" in bounce_code or "5.1.1" in bounce_code:
                    return True
                return False

        # Check permanent codes
        for code in PERMANENT_BOUNCE_CODES:
            if code in bounce_code:
                return True

        for enhanced in PERMANENT_ENHANCED_CODES:
            if enhanced in bounce_code or enhanced in full_text:
                return True

        # Check permanent textual patterns
        for pat in PERMANENT_PATTERNS:
            if re.search(pat, full_text):
                return True

        return False

    @classmethod
    def process_bounce(
        cls,
        lead_id: str,
        email: str,
        bounce_code: str = "550 5.1.1",
        bounce_reason: str = "The email account that you tried to reach does not exist.",
        bounce_provider: str = "Gmail SMTP",
        bounce_raw_reason: str = "",
        campaign_id: Optional[str] = None,
        message_id: Optional[str] = None,
        bounced_at: Optional[str] = None,
        sync_sheets: bool = True
    ) -> Dict[str, Any]:
        """
        Processes real delivery feedback (bounce).
        When permanent bounce (e.g. 550 5.1.1):
          - Classifies: email_verification_status = BOUNCED, outreach_status = BOUNCED, contactability = NOT_CONTACTABLE
          - Suppresses specific email address in SuppressionManager
          - Records BOUNCED in ContactHistoryManager while preserving original SENT record
          - Updates outreach_queue.json item to BOUNCED (terminal, zero retries)
          - Updates campaigns.json stats (bounced_count += 1)
          - Syncs to Google Sheets LEADS row without duplicates
          - Business remains OUTREACH_READY (qualification unchanged)
        """
        now_str = bounced_at or (datetime.now(timezone.utc).isoformat() + "Z")
        clean_email = (email or "").strip().lower()
        clean_code = redact_credentials(bounce_code.strip())
        clean_reason = redact_credentials(bounce_reason.strip())
        clean_provider = redact_credentials(bounce_provider.strip())
        clean_raw = redact_credentials(bounce_raw_reason.strip() or f"{clean_code} {clean_reason}")

        is_perm = cls.is_permanent_bounce(clean_code, clean_reason, clean_raw)

        # ── 1. Locate Lead Context from Queue / History if missing ────────────
        resolved_campaign_id = campaign_id or ""
        resolved_message_id = message_id or ""
        resolved_lead_id = lead_id or ""

        queue_items = cls._load_queue()
        matched_queue_item = None
        for q in queue_items:
            q_lead = str(q.get("lead_id", "")).strip()
            q_recip = str(q.get("recipient", "")).strip().lower()
            if (resolved_lead_id and q_lead == resolved_lead_id) or (clean_email and q_recip == clean_email):
                matched_queue_item = q
                if not resolved_lead_id:
                    resolved_lead_id = q_lead
                if not resolved_campaign_id:
                    resolved_campaign_id = q.get("campaign_id", "")
                if not resolved_message_id:
                    resolved_message_id = q.get("outreach_message_id", "")
                break

        if not resolved_message_id and resolved_lead_id:
            last_c = ContactHistoryManager.get_last_contact(resolved_lead_id, "Email")
            if last_c:
                resolved_message_id = last_c.get("message_id", "")
                if not resolved_campaign_id:
                    resolved_campaign_id = last_c.get("campaign_id", "")

        # ── 2. Handle Permanent Bounce vs Temporary Failure ──────────────────
        if is_perm:
            # A. Persistent Email Suppression (SPECIFIC EMAIL ADDRESS ONLY)
            suppression_reason = f"{clean_code} MAILBOX_NOT_FOUND" if "5.1.1" in clean_code or "550" in clean_code else f"{clean_code} {clean_reason}"
            supp_record = SuppressionManager.suppress_email(
                email=clean_email,
                reason=suppression_reason,
                status=EmailVerificationStatus.BOUNCED.value,
                bounce_code=clean_code,
                bounce_reason=clean_reason,
                bounce_provider=clean_provider,
                bounced_at=now_str
            )

            # B. Append to Contact History (Preserve original SENT, add BOUNCED)
            ContactHistoryManager.record_bounce(
                lead_id=resolved_lead_id,
                campaign_id=resolved_campaign_id,
                recipient=clean_email,
                bounce_code=clean_code,
                bounce_reason=clean_reason,
                bounce_provider=clean_provider,
                message_id=resolved_message_id,
                bounced_at=now_str,
                raw_reason=clean_raw
            )

            # C. Update Queue Record (Mark terminal, zero retries)
            if matched_queue_item:
                matched_queue_item["status"] = OutreachStatus.BOUNCED.value
                matched_queue_item["bounce_code"] = clean_code
                matched_queue_item["bounce_reason"] = clean_reason
                matched_queue_item["bounced_at"] = now_str
                matched_queue_item["bounce_provider"] = clean_provider
                matched_queue_item["error_message"] = f"Permanent bounce ({clean_code}): {clean_reason}"
                cls._save_queue(queue_items)

            # D. Update Campaign Stats (Distinguish SENT, BOUNCED, FAILED, BLOCKED)
            if resolved_campaign_id:
                cls._record_campaign_bounce(resolved_campaign_id)

            # E. Update Google Sheets LEADS row (Preserve lead_id, sent_at, mid, etc.)
            sheets_synced = False
            if sync_sheets and resolved_lead_id:
                try:
                    storage = GoogleSheetsStorageProvider()
                    storage.ensure_leads_columns()
                    update_data = {
                        "email_verification_status": EmailVerificationStatus.BOUNCED.value,
                        "outreach_status": OutreachStatus.BOUNCED.value,
                        "contactability_status": ContactabilityState.NOT_CONTACTABLE.value,
                        "contactability_reason": f"Email permanently bounced ({clean_code}): {clean_reason}",
                        "bounce_code": clean_code,
                        "bounce_reason": clean_reason,
                        "bounced_at": now_str,
                        "bounce_provider": clean_provider,
                        "email_suppressed": "True",
                        "email_suppression_reason": suppression_reason,
                    }
                    sheets_synced = storage.update_lead_outreach(resolved_lead_id, update_data)
                except Exception as e:
                    print(f"[BounceManager] Warning: Sheets sync error for {resolved_lead_id}: {e}")

            return {
                "success": True,
                "is_permanent": True,
                "lead_id": resolved_lead_id,
                "email": clean_email,
                "email_verification_status": EmailVerificationStatus.BOUNCED.value,
                "outreach_status": OutreachStatus.BOUNCED.value,
                "contactability_status": ContactabilityState.NOT_CONTACTABLE.value,
                "bounce_code": clean_code,
                "bounce_reason": clean_reason,
                "bounce_provider": clean_provider,
                "bounced_at": now_str,
                "email_suppressed": True,
                "suppression_record": supp_record,
                "sheets_synced": sheets_synced,
                "message": f"Permanent bounce processed: {clean_email} suppressed with code {clean_code}."
            }

        else:
            # Temporary delivery failure — remain retryable
            if matched_queue_item:
                attempts = matched_queue_item.get("attempts", 1)
                matched_queue_item["status"] = OutreachStatus.FAILED.value
                matched_queue_item["error_message"] = f"Temporary delivery failure ({clean_code}): {clean_reason}"
                cls._save_queue(queue_items)

            ContactHistoryManager.record_attempt(
                lead_id=resolved_lead_id,
                campaign_id=resolved_campaign_id,
                channel="Email",
                recipient=clean_email,
                message_id=resolved_message_id,
                status=OutreachStatus.FAILED.value,
                outcome="TEMPORARY_FAILURE",
                message_body="",
                error=f"{clean_code}: {clean_reason}"
            )

            return {
                "success": True,
                "is_permanent": False,
                "lead_id": resolved_lead_id,
                "email": clean_email,
                "email_verification_status": EmailVerificationStatus.UNVERIFIED.value,
                "outreach_status": OutreachStatus.FAILED.value,
                "bounce_code": clean_code,
                "bounce_reason": clean_reason,
                "email_suppressed": False,
                "retryable": True,
                "message": f"Temporary delivery failure recorded ({clean_code}). Not suppressed, retryable."
            }

    @classmethod
    def _record_campaign_bounce(cls, campaign_id: str):
        try:
            with open(CAMPAIGNS_FILE, "r", encoding="utf-8") as f:
                campaigns = json.load(f)
            for c in campaigns:
                if c.get("campaign_id") == campaign_id:
                    c["bounced_count"] = c.get("bounced_count", 0) + 1
                    summary = c.setdefault("execution_summary", {})
                    summary["sent"] = c.get("sent_count", 0)
                    summary["failed"] = c.get("failed_count", 0)
                    summary["blocked"] = c.get("blocked_count", 0)
                    summary["bounced"] = c.get("bounced_count", 0)
                    break
            with open(CAMPAIGNS_FILE, "w", encoding="utf-8") as f:
                json.dump(campaigns, f, indent=2, default=str)
        except Exception as e:
            print(f"[BounceManager] Warning: failed to update campaign stats: {e}")

    @classmethod
    def _load_queue(cls) -> List[Dict[str, Any]]:
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    @classmethod
    def _save_queue(cls, queue: List[Dict[str, Any]]):
        try:
            with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump(queue, f, indent=2, default=str)
        except Exception as e:
            print(f"[BounceManager] Error saving queue: {e}")
