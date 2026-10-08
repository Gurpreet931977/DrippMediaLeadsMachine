"""
lib/system/freshness_engine.py
==============================
Evidence Staleness Detection & Canonical Re-qualification Protection.
Tracks freshness separately per evidence family:
  - identity
  - website
  - review evidence
  - operational evidence
  - phone
  - instagram
  - facebook
  - email
  - qualification

INVARIANTS:
  1. Do not silently downgrade qualification solely because evidence is old.
  2. Flag STALE_REVIEW, STALE_OPERATIONAL, STALE_CONTACT, STALE_WEBSITE.
  3. Queue appropriate technical refresh.
  4. RE-QUALIFICATION PROTECTION:
     REFRESH EVIDENCE -> RECONCILE -> RUN QUALIFICATION -> STATE CHANGE ONLY IF RULE ENGINE SAYS SO.
     Never directly edit OUTREACH_READY from an enrichment or refresh script.
"""

import logging
from enum import Enum
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("FreshnessEngine")

class StalenessFlag(str, Enum):
    STALE_WEBSITE = "STALE_WEBSITE"
    STALE_REVIEW = "STALE_REVIEW"
    STALE_OPERATIONAL = "STALE_OPERATIONAL"
    STALE_CONTACT = "STALE_CONTACT"


# Standard Evidence Time-To-Live in Days
DEFAULT_FRESHNESS_TTLS = {

    "identity": 365,
    "website": 30,
    "review_evidence": 180,  # Matches Rule B: review recency <= 180 days
    "operational_evidence": 60,
    "phone": 90,
    "instagram": 60,
    "facebook": 60,
    "email": 60,
    "qualification": 180,
}


def parse_iso_or_date(val: Optional[str]) -> Optional[datetime]:
    """Parses ISO-8601 string or YYYY-MM-DD date into timezone-aware datetime."""
    if not val or not str(val).strip():
        return None
    val_str = str(val).strip()
    try:
        # Try full ISO
        dt = datetime.fromisoformat(val_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    try:
        # Try YYYY-MM-DD
        dt = datetime.strptime(val_str[:10], "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


class FreshnessEngine:
    """
    Evaluates evidence freshness and enforces canonical re-qualification.
    """

    def __init__(self, ttls: Optional[Dict[str, int]] = None):
        self.ttls = dict(DEFAULT_FRESHNESS_TTLS)
        if ttls:
            self.ttls.update(ttls)

    def evaluate_lead_freshness(
        self,
        lead: Dict[str, Any],
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Evaluates lead timestamps across evidence families and identifies stale components.
        Does NOT alter qualification_state.
        """
        ref = reference_time or datetime.now(timezone.utc)
        flags: List[str] = []
        queued_refreshes: List[str] = []
        ages_days: Dict[str, Optional[int]] = {}

        # 1. Website Freshness
        web_ts = parse_iso_or_date(lead.get("website_checked_at") or lead.get("date_added"))
        if web_ts:
            age = (ref - web_ts).days
            ages_days["website"] = max(0, age)
            if age > self.ttls["website"]:
                flags.append("STALE_WEBSITE")
                queued_refreshes.append("RUN_WEBSITE_AUDIT")
        else:
            ages_days["website"] = None
            flags.append("STALE_WEBSITE")
            queued_refreshes.append("RUN_WEBSITE_AUDIT")

        # 2. Review Evidence Freshness
        rev_ts = parse_iso_or_date(
            lead.get("review_evidence_updated_at")
            or lead.get("reviews_refreshed_at")
            or lead.get("latest_review_date")
            or lead.get("date_added")
        )
        if rev_ts:
            age = (ref - rev_ts).days
            ages_days["review_evidence"] = max(0, age)
            if age > self.ttls["review_evidence"]:
                flags.append("STALE_REVIEW")
                queued_refreshes.append("RUN_REVIEW_REFRESH")
        else:
            ages_days["review_evidence"] = None
            flags.append("STALE_REVIEW")
            queued_refreshes.append("RUN_REVIEW_REFRESH")

        # 3. Operational Evidence Freshness
        op_ts = parse_iso_or_date(
            lead.get("operational_verified_at")
            or lead.get("operational_checked_at")
            or lead.get("date_added")
        )
        if op_ts:
            age = (ref - op_ts).days
            ages_days["operational_evidence"] = max(0, age)
            if age > self.ttls["operational_evidence"]:
                flags.append("STALE_OPERATIONAL")
                queued_refreshes.append("RUN_OPERATIONAL_REFRESH")
        else:
            ages_days["operational_evidence"] = None
            flags.append("STALE_OPERATIONAL")
            queued_refreshes.append("RUN_OPERATIONAL_REFRESH")

        # 4. Contactability Freshness (Phone, Social, Email)
        contact_ts = parse_iso_or_date(lead.get("contact_verified_at") or lead.get("date_added"))
        if contact_ts:
            age = (ref - contact_ts).days
            ages_days["contactability"] = max(0, age)
            if age > min(self.ttls["phone"], self.ttls["instagram"], self.ttls["email"]):
                flags.append("STALE_CONTACT")
                queued_refreshes.append("RUN_CONTACTABILITY_REFRESH")
        else:
            ages_days["contactability"] = None
            flags.append("STALE_CONTACT")
            queued_refreshes.append("RUN_CONTACTABILITY_REFRESH")

        return {
            "lead_id": lead.get("lead_id"),
            "company_name": lead.get("company_name"),
            "is_fresh": len(flags) == 0,
            "stale_flags": flags,
            "queued_refreshes": list(dict.fromkeys(queued_refreshes)),
            "evidence_ages_days": ages_days,
            "current_qualification_state": lead.get("qualification_state"),
        }

    def process_lead_freshness(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        """Convenience method returning lead with staleness flags and refresh trigger."""
        eval_res = self.evaluate_lead_freshness(lead)
        return {
            "lead": lead,
            "staleness_flags": eval_res["stale_flags"],
            "refresh_queued": len(eval_res["queued_refreshes"]) > 0,
        }


    def requalify_lead(
        self,
        lead: Dict[str, Any],
        refreshed_evidence: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], bool, str]:
        """
        Enforces canonical Re-qualification Protection:
          REFRESH EVIDENCE -> RECONCILE -> RUN QUALIFICATION -> STATE CHANGE ONLY IF RULE ENGINE SAYS SO.
        Direct manual overrides of OUTREACH_READY are strictly rejected.
        Returns: (updated_lead, state_changed, decision_reason)
        """
        from lib.qualification.lead_scoring import LeadScorer

        # Shallow copy lead and update evidence attributes
        updated_lead = dict(lead)
        for k, v in refreshed_evidence.items():
            if k in ("qualification_state", "lead_score", "priority"):
                # REJECT direct qualification mutations from evidence payloads!
                logger.warning(
                    f"Direct qualification override '{k}={v}' rejected during technical refresh."
                )
                continue
            updated_lead[k] = v

        scorer = LeadScorer()
        new_score, new_signals, new_priority, new_state, new_reason = scorer.evaluate_lead(updated_lead)

        previous_state = lead.get("qualification_state")
        state_changed = previous_state != new_state

        updated_lead["lead_score"] = new_score
        updated_lead["qualification_signals"] = ", ".join(new_signals)
        updated_lead["priority"] = new_priority
        updated_lead["qualification_state"] = new_state
        updated_lead["qualification_reason"] = new_reason
        updated_lead["qualification_updated_at"] = datetime.now(timezone.utc).isoformat()

        logger.info(
            f"[Requalification] {lead.get('lead_id')}: {previous_state} -> {new_state} (Changed={state_changed})"
        )
        return updated_lead, state_changed, new_reason

    def evaluate_dimensions_freshness(
        self,
        lead: Dict[str, Any],
        reference_time: Optional[datetime] = None,
    ):
        """Phase 10.4 Canonical 6-Dimension Freshness Evaluation."""
        from lib.system.canonical_freshness import CanonicalFreshnessEngine
        c_engine = CanonicalFreshnessEngine()
        return c_engine.evaluate_lead(lead, reference_time=reference_time)


# Convenience exports for Phase 10.4
from lib.system.freshness_models import (
    FreshnessDimension,
    FreshnessStatus,
    RefreshPriorityTier,
    RefreshFailureReason,
    DEFAULT_REFRESH_CADENCE_DAYS,
    PROTECTED_HISTORICAL_STATES,
    DimensionFreshness,
    ChangeHistoryEntry,
    LeadFreshnessSummary,
)
from lib.system.canonical_freshness import CanonicalFreshnessEngine

