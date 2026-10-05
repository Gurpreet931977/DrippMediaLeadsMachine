"""
Dripp Media — Compliance, Suppression, Contact History & Cooldown Engine
========================================================================
Section 10, 11, 12, 13, 14:
  - UK PECR / ICO compliant email marketing evaluation
  - Subscriber type discrimination (Corporate vs Sole Trader / Partnership)
  - Persistent multi-channel suppression list (data/suppression_list.json)
  - Persistent comprehensive contact history (data/contact_history.json)
  - Global contact cooldown enforcement (configurable cooldown days)
"""

import os
import json
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Tuple

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data"
)
SUPPRESSION_FILE = os.path.join(DATA_DIR, "suppression_list.json")
HISTORY_FILE     = os.path.join(DATA_DIR, "contact_history.json")


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────────
# COMPLIANCE DATA MODEL
# ──────────────────────────────────────────────────────────────────────────

class SubscriberType:
    CORPORATE_SUBSCRIBER   = "CORPORATE_SUBSCRIBER"
    INDIVIDUAL_SUBSCRIBER  = "INDIVIDUAL_SUBSCRIBER"
    UNKNOWN                = "UNKNOWN"
    # Legacy alias for backward compatibility
    SOLE_TRADER_OR_PARTNERSHIP = "INDIVIDUAL_SUBSCRIBER"
    INDIVIDUAL_OR_UNKNOWN      = "UNKNOWN"


class MarketingEmailStatus:
    COMPLIANCE_ELIGIBLE = "COMPLIANCE_ELIGIBLE"
    ELIGIBLE            = "COMPLIANCE_ELIGIBLE"  # Alias
    MANUAL_REVIEW       = "MANUAL_REVIEW"
    BLOCKED             = "BLOCKED"
    UNKNOWN             = "UNKNOWN"


class LawfulBasisStatus:
    LEGITIMATE_INTERESTS_ASSESSED = "LEGITIMATE_INTERESTS_ASSESSED"
    CONSENT_REQUIRED             = "CONSENT_REQUIRED"
    PENDING_REVIEW               = "PENDING_REVIEW"
    NONE                         = "NONE"


class OptOutStatus:
    ACTIVE     = "ACTIVE"
    OPTED_OUT  = "OPTED_OUT"
    SUPPRESSED = "SUPPRESSED"


class ComplianceReviewStatus:
    PASSED         = "PASSED"
    PENDING_REVIEW = "PENDING_REVIEW"
    FAILED         = "FAILED"


@dataclass
class EmailComplianceRecord:
    subscriber_type: str
    marketing_email_status: str
    lawful_basis_status: str
    opt_out_status: str
    compliance_review_status: str
    compliance_notes: str
    contact_source: str
    contact_source_date: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────────
# UK PECR / ICO COMPLIANCE EVALUATOR
# ──────────────────────────────────────────────────────────────────────────

class UKComplianceEvaluator:
    """
    Evaluates marketing email eligibility under UK Privacy and Electronic
    Communications Regulations (PECR) and current ICO B2B Marketing Guidance.
    
    Legal reference (ICO Guidance on Direct Marketing to Businesses):
      - Corporate subscribers (Ltd, PLC, LLP, Scottish partnerships, corporate bodies):
        B2B direct marketing permitted under legitimate interests without prior consent,
        provided Dripp Media is clearly identified and an opt-out / unsubscribe mechanism is given.
      - Individual subscribers (Sole traders, unincorporated partnerships):
        Treated under PECR Reg 22, requiring prior consent or existing customer soft opt-in.
      - Unknown / Uncertain:
        Must NOT assume corporate, must NOT assume individual. Flagged as MANUAL_REVIEW
        with reason SUBSCRIBER_TYPE_UNCERTAIN.
    """

    @classmethod
    def evaluate(
        cls,
        lead: Dict[str, Any],
        email: str,
        entity_record: Optional[Any] = None
    ) -> EmailComplianceRecord:
        now_str = datetime.now(timezone.utc).isoformat() + "Z"
        lead_id = lead.get("lead_id", "")

        # 1. Check persistent multi-channel suppression list (Section 12, 17)
        if SuppressionManager.is_suppressed(lead_id, "Email", email):
            return EmailComplianceRecord(
                subscriber_type=SubscriberType.UNKNOWN,
                marketing_email_status=MarketingEmailStatus.BLOCKED,
                lawful_basis_status=LawfulBasisStatus.NONE,
                opt_out_status=OptOutStatus.SUPPRESSED,
                compliance_review_status=ComplianceReviewStatus.FAILED,
                compliance_notes="Recipient or Lead is on the active suppression list (Opted out / DNC)",
                contact_source="suppression_registry",
                contact_source_date=now_str
            )

        # 2. Determine subscriber type from Companies House verification (Section 1, 2, 3, 5, 6)
        if entity_record is None:
            from lib.outreach.companies_house import CompaniesHouseVerifier
            entity_record = CompaniesHouseVerifier.verify_entity(lead)

        match_status = getattr(entity_record, "match_status", "NO_MATCH")
        confidence = getattr(entity_record, "match_confidence", "UNKNOWN")
        company_status = getattr(entity_record, "company_status", "").upper()
        is_corporate_entity = getattr(entity_record, "is_corporate_subscriber", False)
        reg_name = getattr(entity_record, "registered_name", "")
        ch_num = getattr(entity_record, "companies_house_number", "")

        explicit_sub_type = str(lead.get("subscriber_type") or "").strip().upper()

        if is_corporate_entity and match_status in ("MATCHED", "MATCHED_ACTIVE") and confidence == "HIGH" and company_status == "ACTIVE":
            # Active corporate body with verified separate legal personality under PECR
            subscriber_type = SubscriberType.CORPORATE_SUBSCRIBER
            status = MarketingEmailStatus.COMPLIANCE_ELIGIBLE
            lawful_basis = LawfulBasisStatus.LEGITIMATE_INTERESTS_ASSESSED
            review_status = ComplianceReviewStatus.PASSED
            notes = f"Corporate subscriber verified via Companies House ({reg_name} #{ch_num}). Eligible under ICO B2B marketing guidance with opt-out provided."
            # Personal email check under UK GDPR
            if email and not any(email.startswith(p + "@") for p in ["info", "contact", "hello", "enquiries", "office", "admin", "team", "bookings", "reservations"]):
                notes += " (Individual named recipient noted: processing must comply with UK GDPR principles)."

        elif company_status == "DISSOLVED" or match_status in ("MATCHED_DISSOLVED", "CONFLICT"):
            # Section 1, 2, 3, 6: Do not treat DISSOLVED / CONFLICT as active corporate subscriber
            subscriber_type = SubscriberType.UNKNOWN
            status = MarketingEmailStatus.MANUAL_REVIEW
            lawful_basis = LawfulBasisStatus.PENDING_REVIEW
            review_status = ComplianceReviewStatus.PENDING_REVIEW
            if match_status == "CONFLICT":
                notes = f"SUBSCRIBER_TYPE_UNCERTAIN: Matched Companies House entity '{reg_name}' is DISSOLVED while current business appears active (CONFLICT). Cannot assume active corporate subscriber. Investigate legal trading structure."
            else:
                notes = f"SUBSCRIBER_TYPE_UNCERTAIN: Matched Companies House entity '{reg_name}' is DISSOLVED. Cannot treat as active corporate subscriber. Investigate legal trading structure."

        elif explicit_sub_type == SubscriberType.INDIVIDUAL_SUBSCRIBER or "SOLE_TRADER" in explicit_sub_type:
            # Section 9: Individual subscriber requires consent or soft opt-in
            subscriber_type = SubscriberType.INDIVIDUAL_SUBSCRIBER
            status = MarketingEmailStatus.MANUAL_REVIEW
            lawful_basis = LawfulBasisStatus.CONSENT_REQUIRED
            review_status = ComplianceReviewStatus.PENDING_REVIEW
            notes = "Individual subscriber (Sole trader / unincorporated partnership). Under UK PECR Reg 22, prior consent or existing customer soft opt-in required before electronic marketing."

        else:
            # Section 10: Unknown subscriber type
            subscriber_type = SubscriberType.UNKNOWN
            status = MarketingEmailStatus.MANUAL_REVIEW
            lawful_basis = LawfulBasisStatus.PENDING_REVIEW
            review_status = ComplianceReviewStatus.PENDING_REVIEW
            reason_txt = getattr(entity_record, "match_reason", "No verified Companies House entity found")
            notes = f"SUBSCRIBER_TYPE_UNCERTAIN: {reason_txt}. Under ICO B2B guidance, corporate subscriber status cannot be assumed without verified corporate entity."

        return EmailComplianceRecord(
            subscriber_type=subscriber_type,
            marketing_email_status=status,
            lawful_basis_status=lawful_basis,
            opt_out_status=OptOutStatus.ACTIVE,
            compliance_review_status=review_status,
            compliance_notes=notes,
            contact_source=lead.get("email_source") or "discovered_public_profile",
            contact_source_date=now_str
        )


# ──────────────────────────────────────────────────────────────────────────
# PERSISTENT SUPPRESSION LIST
# ──────────────────────────────────────────────────────────────────────────

class SuppressionManager:
    """
    Maintains persistent suppression (Opt-out / DO_NOT_CONTACT / BOUNCED).
    Survives across campaigns, pipeline reruns, enrichment, and restarts.
    """

    @classmethod
    def _load(cls) -> Dict[str, Any]:
        _ensure_dir()
        try:
            with open(SUPPRESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("global", [])
                data.setdefault("channels", {"Email": [], "Instagram": [], "Facebook": []})
                data.setdefault("suppressed_emails", {})
                return data
        except Exception:
            return {
                "global": [],
                "channels": {"Email": [], "Instagram": [], "Facebook": []},
                "suppressed_emails": {}
            }

    @classmethod
    def _save(cls, data: Dict[str, Any]):
        _ensure_dir()
        with open(SUPPRESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def is_suppressed(cls, lead_id: str, channel: str = "", recipient: str = "") -> bool:
        data = cls._load()
        # Global DO_NOT_CONTACT
        global_list = data.get("global", [])
        if lead_id and lead_id in global_list:
            return True
        if recipient and recipient.lower() in [g.lower() for g in global_list]:
            return True

        # Specific email suppression check
        clean_recipient = recipient.strip().lower() if recipient else ""
        if clean_recipient and clean_recipient in data.get("suppressed_emails", {}):
            return True

        # Channel-specific
        ch_key = "Email" if "email" in channel.lower() else (
            "Instagram" if "instagram" in channel.lower() else (
                "Facebook" if "facebook" in channel.lower() else channel
            )
        )
        ch_list = data.get("channels", {}).get(ch_key, [])
        if lead_id and lead_id in ch_list:
            return True
        if recipient and recipient.lower() in [c.lower() for c in ch_list]:
            return True

        return False

    @classmethod
    def is_email_suppressed(cls, email: str) -> bool:
        """Returns True if the specific email address is suppressed (e.g. BOUNCED or opted-out)."""
        if not email:
            return False
        clean = email.strip().lower()
        data = cls._load()
        if clean in data.get("suppressed_emails", {}):
            return True
        if clean in [e.lower() for e in data.get("channels", {}).get("Email", [])]:
            return True
        return False

    @classmethod
    def get_email_suppression(cls, email: str) -> Optional[Dict[str, Any]]:
        """Returns the structured suppression record for a specific email address if it exists."""
        if not email:
            return None
        clean = email.strip().lower()
        data = cls._load()
        return data.get("suppressed_emails", {}).get(clean)

    @classmethod
    def suppress_email(
        cls,
        email: str,
        reason: str = "550 5.1.1 MAILBOX_NOT_FOUND",
        status: str = "BOUNCED",
        bounce_code: str = "550 5.1.1",
        bounce_reason: str = "",
        bounce_provider: str = "",
        bounced_at: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Suppresses a specific email address (NOT the business).
        Survives new campaigns, pipeline reruns, enrichment, and restarts.
        """
        clean_email = (email or "").strip().lower()
        if not clean_email:
            return {}
        data = cls._load()
        now_str = bounced_at or (datetime.now(timezone.utc).isoformat() + "Z")

        record = {
            "email": clean_email,
            "status": status,
            "reason": reason,
            "bounce_code": bounce_code,
            "bounce_reason": bounce_reason,
            "bounce_provider": bounce_provider,
            "bounced_at": now_str,
            "created_at": now_str
        }

        data.setdefault("suppressed_emails", {})[clean_email] = record

        # Also add to channels.Email for backwards compatibility
        ch_list = data.setdefault("channels", {}).setdefault("Email", [])
        if clean_email not in [x.lower() for x in ch_list]:
            ch_list.append(clean_email)

        cls._save(data)
        return record

    @classmethod
    def add_suppression(cls, identifier: str, channel: Optional[str] = None, recipient: Optional[str] = None, reason: str = ""):
        data = cls._load()
        now_str = datetime.now(timezone.utc).isoformat() + "Z"
        if not channel:
            if identifier not in data["global"]:
                data["global"].append(identifier)
        else:
            ch_key = "Email" if "email" in channel.lower() else (
                "Instagram" if "instagram" in channel.lower() else (
                    "Facebook" if "facebook" in channel.lower() else channel
                )
            )
            ch_list = data["channels"].setdefault(ch_key, [])
            if identifier and identifier not in ch_list:
                ch_list.append(identifier)
        cls._save(data)

    @classmethod
    def remove_suppression(cls, identifier: str, channel: Optional[str] = None):
        data = cls._load()
        clean = identifier.strip().lower()
        if "suppressed_emails" in data and clean in data["suppressed_emails"]:
            del data["suppressed_emails"][clean]
        if not channel:
            data["global"] = [x for x in data.get("global", []) if x.lower() != identifier.lower()]
            for ch in data.get("channels", {}).values():
                ch[:] = [x for x in ch if x.lower() != identifier.lower()]
        else:
            ch_key = "Email" if "email" in channel.lower() else (
                "Instagram" if "instagram" in channel.lower() else (
                    "Facebook" if "facebook" in channel.lower() else channel
                )
            )
            if ch_key in data.get("channels", {}):
                data["channels"][ch_key] = [x for x in data["channels"][ch_key] if x.lower() != identifier.lower()]
        cls._save(data)


# ──────────────────────────────────────────────────────────────────────────
# PERSISTENT CONTACT HISTORY
# ──────────────────────────────────────────────────────────────────────────

class ContactHistoryManager:
    """
    Maintains persistent audit log of every outreach attempt.
    """

    @classmethod
    def _load(cls) -> List[Dict[str, Any]]:
        _ensure_dir()
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    @classmethod
    def _save(cls, history: List[Dict[str, Any]]):
        _ensure_dir()
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

    @classmethod
    def record_attempt(
        cls,
        lead_id: str,
        campaign_id: str,
        channel: str,
        recipient: str,
        message_id: str,
        status: str,
        outcome: str,
        message_body: str = "",
        error: str = ""
    ):
        history = cls._load()
        entry = {
            "history_id": f"HIST-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{lead_id[-6:] if lead_id else 'LEAD'}",
            "lead_id": lead_id,
            "campaign_id": campaign_id,
            "channel": channel,
            "recipient": recipient,
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "message_id": message_id,
            "status": status,
            "outcome": outcome,
            "message_body": message_body[:400] if message_body else "",
            "error": error[:300] if error else ""
        }
        history.append(entry)
        cls._save(history)

    @classmethod
    def record_bounce(
        cls,
        lead_id: str,
        campaign_id: str,
        recipient: str,
        bounce_code: str,
        bounce_reason: str,
        bounce_provider: str = "Gmail SMTP",
        message_id: str = "",
        bounced_at: Optional[str] = None,
        raw_reason: str = ""
    ):
        """
        Records a post-transmission delivery bounce while strictly preserving
        the prior SENT/ACCEPTED transmission record in contact history.
        """
        history = cls._load()
        now_str = bounced_at or (datetime.now(timezone.utc).isoformat() + "Z")
        entry = {
            "history_id": f"HIST-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{lead_id[-6:] if lead_id else 'BOUNCE'}",
            "lead_id": lead_id,
            "campaign_id": campaign_id,
            "channel": "Email",
            "recipient": recipient,
            "timestamp": now_str,
            "message_id": message_id,
            "status": "BOUNCED",
            "outcome": "BOUNCED",
            "message_body": "",
            "error": f"{bounce_code}: {bounce_reason}".strip(": "),
            "bounce_code": bounce_code,
            "bounce_reason": bounce_reason,
            "bounce_provider": bounce_provider,
            "bounce_raw_reason": raw_reason[:500] if raw_reason else ""
        }
        history.append(entry)
        cls._save(history)

    @classmethod
    def get_lead_history(cls, lead_id: str) -> List[Dict[str, Any]]:
        return [h for h in cls._load() if h.get("lead_id") == lead_id]

    @classmethod
    def get_lead_timeline(cls, lead_id: str) -> List[Dict[str, Any]]:
        """Returns all history items for a lead sorted chronologically."""
        items = cls.get_lead_history(lead_id)
        return sorted(items, key=lambda x: x.get("timestamp", ""))

    @classmethod
    def get_contact_count(cls, lead_id: str) -> int:
        return len(cls.get_lead_history(lead_id))

    @classmethod
    def get_last_contact(cls, lead_id: str, channel: Optional[str] = None) -> Optional[Dict[str, Any]]:
        history = cls.get_lead_history(lead_id)
        if channel:
            history = [h for h in history if h.get("channel") == channel]
        if not history:
            return None
        return sorted(history, key=lambda x: x.get("timestamp", ""), reverse=True)[0]

    @classmethod
    def has_been_contacted(cls, lead_id: str, channel: Optional[str] = None) -> bool:
        return cls.get_last_contact(lead_id, channel) is not None


# ──────────────────────────────────────────────────────────────────────────
# GLOBAL CONTACT COOLDOWN
# ──────────────────────────────────────────────────────────────────────────

class CooldownManager:
    """
    Enforces a configurable global contact cooldown period across campaigns.
    """

    @classmethod
    def get_cooldown_days(cls) -> int:
        return int(os.getenv("GLOBAL_CONTACT_COOLDOWN_DAYS", "30"))

    @classmethod
    def is_in_cooldown(cls, lead_id: str, channel: str) -> Tuple[bool, Optional[str]]:
        """
        Returns:
          (in_cooldown, cooldown_until_iso_str)
        """
        last_contact = ContactHistoryManager.get_last_contact(lead_id, channel)
        if not last_contact:
            return False, None

        # Only sent/delivered messages trigger contact cooldown
        if last_contact.get("status") not in ("SENT", "DELIVERED"):
            return False, None

        ts_str = last_contact.get("timestamp")
        if not ts_str:
            return False, None

        try:
            contact_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            cooldown_days = cls.get_cooldown_days()
            cooldown_until = contact_dt + timedelta(days=cooldown_days)
            now = datetime.now(timezone.utc)

            if now < cooldown_until:
                return True, cooldown_until.isoformat() + "Z"
            return False, None
        except Exception:
            return False, None
