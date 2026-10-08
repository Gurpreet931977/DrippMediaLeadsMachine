"""
lib/system/canonical_freshness.py
=================================
Canonical Freshness Evaluation & Deterministic Queue Engine (Phase 10.4).

Evaluates 6 independent dimensions per canonical lead:
  1. WEBSITE STATUS
  2. OPERATIONAL STATUS
  3. PHONE
  4. SOCIAL LINKS
  5. REVIEW EVIDENCE
  6. CONTACTABILITY

Calculates:
  - Global freshness status (FRESH, DUE, STALE, REFRESHING, REFRESH_FAILED)
  - Next refresh deadlines per dimension and for global lead
  - Priority-based deterministic refresh ordering (Tier A -> E)
  - Protection of historical outreach states (SENT, BOUNCED, SUPPRESSED)
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

from lib.system.freshness_models import (
    FreshnessDimension,
    FreshnessStatus,
    RefreshPriorityTier,
    RefreshFailureReason,
    DEFAULT_REFRESH_CADENCE_DAYS,
    PROTECTED_HISTORICAL_STATES,
    DimensionFreshness,
    LeadFreshnessSummary,
)

logger = logging.getLogger("CanonicalFreshness")


def parse_iso_or_date(val: Optional[Any]) -> Optional[datetime]:
    """Parses ISO-8601 string or YYYY-MM-DD date into timezone-aware datetime."""
    if not val or not str(val).strip():
        return None
    val_str = str(val).strip()
    try:
        dt = datetime.fromisoformat(val_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    try:
        dt = datetime.strptime(val_str[:10], "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


class CanonicalFreshnessEngine:
    """
    Evaluator for independent lead evidence dimensions and deterministic queue builder.
    """

    def __init__(self, cadences: Optional[Dict[str, int]] = None):
        self.cadences = dict(DEFAULT_REFRESH_CADENCE_DAYS)
        if cadences:
            self.cadences.update(cadences)

        # Allow environment variable overrides
        for dim, key in [
            (FreshnessDimension.WEBSITE.value, "REFRESH_CADENCE_WEBSITE_DAYS"),
            (FreshnessDimension.OPERATIONAL.value, "REFRESH_CADENCE_OPERATIONAL_DAYS"),
            (FreshnessDimension.PHONE.value, "REFRESH_CADENCE_PHONE_DAYS"),
            (FreshnessDimension.SOCIAL.value, "REFRESH_CADENCE_SOCIAL_DAYS"),
            (FreshnessDimension.REVIEW.value, "REFRESH_CADENCE_REVIEW_DAYS"),
            (FreshnessDimension.CONTACTABILITY.value, "REFRESH_CADENCE_CONTACTABILITY_DAYS"),
        ]:
            if os.environ.get(key):
                try:
                    self.cadences[dim] = int(os.environ[key])
                except ValueError:
                    pass

    def evaluate_dimension(
        self,
        dimension_name: str,
        value: Any,
        checked_at_raw: Optional[Any],
        source: str,
        confidence_or_status: str,
        failure_state: Optional[str] = None,
        reference_time: Optional[datetime] = None,
    ) -> DimensionFreshness:
        """
        Evaluates an individual evidence dimension's freshness against its specific cadence.
        """
        ref = reference_time or datetime.now(timezone.utc)
        cadence_days = self.cadences.get(dimension_name, 30)

        checked_dt = parse_iso_or_date(checked_at_raw)
        checked_iso = checked_dt.isoformat() if checked_dt else None

        if checked_dt:
            age_days = max(0, (ref - checked_dt).days)
            next_due_dt = checked_dt + timedelta(days=cadence_days)
            next_due_iso = next_due_dt.isoformat()
            is_due = ref >= next_due_dt
            is_stale = age_days > (cadence_days * 2)
        else:
            age_days = None
            next_due_iso = ref.isoformat()
            is_due = True
            is_stale = True

        return DimensionFreshness(
            dimension=dimension_name,
            value=value,
            checked_at=checked_iso,
            source=source or "UNKNOWN",
            confidence_or_status=confidence_or_status or "UNKNOWN",
            next_refresh_due=next_due_iso,
            failure_state=failure_state,
            age_days=age_days,
            is_due=is_due,
            is_stale=is_stale,
        )

    def evaluate_lead(
        self,
        lead: Dict[str, Any],
        reference_time: Optional[datetime] = None,
    ) -> LeadFreshnessSummary:
        """
        Evaluates a canonical lead across all 6 freshness dimensions and assigns priority.
        """
        ref = reference_time or datetime.now(timezone.utc)
        lead_id = lead.get("lead_id", "UNKNOWN")
        company_name = lead.get("company_name", "UNKNOWN")

        # 1. WEBSITE
        web_val = lead.get("website", "")
        web_ts = lead.get("website_checked_at") or lead.get("website_audit_date") or lead.get("date_added")
        web_status = lead.get("website_status", "UNKNOWN")
        web_source = lead.get("website_source", "OFFICIAL_WEBSITE")
        web_fail = lead.get("website_failure_state")
        dim_web = self.evaluate_dimension(
            FreshnessDimension.WEBSITE.value,
            web_val,
            web_ts,
            web_source,
            web_status,
            web_fail,
            ref,
        )

        # 2. OPERATIONAL
        op_val = lead.get("operational_status", "UNKNOWN")
        op_ts = lead.get("operational_verified_at") or lead.get("operational_checked_at") or lead.get("date_added")
        op_conf = lead.get("operational_confidence", "LOW")
        op_source = lead.get("operational_source", "OPENSTREETMAP_AND_REVIEWS")
        op_fail = lead.get("operational_failure_state")
        dim_op = self.evaluate_dimension(
            FreshnessDimension.OPERATIONAL.value,
            op_val,
            op_ts,
            op_source,
            op_conf,
            op_fail,
            ref,
        )

        # 3. PHONE
        phone_val = lead.get("phone", "")
        phone_ts = lead.get("phone_verified_at") or lead.get("contact_verified_at") or lead.get("date_added")
        phone_status = lead.get("phone_status", "VERIFIED" if phone_val else "NONE")
        phone_source = lead.get("phone_source", "PLACES_OR_WEBSITE")
        phone_fail = lead.get("phone_failure_state")
        dim_phone = self.evaluate_dimension(
            FreshnessDimension.PHONE.value,
            phone_val,
            phone_ts,
            phone_source,
            phone_status,
            phone_fail,
            ref,
        )

        # 4. SOCIAL
        social_val = {
            "instagram_url": lead.get("instagram_url", ""),
            "facebook_url": lead.get("facebook_url", ""),
            "tiktok_url": lead.get("tiktok_url", ""),
        }
        social_ts = (
            lead.get("social_checked_at")
            or lead.get("latest_social_post_date")
            or lead.get("contact_verified_at")
            or lead.get("date_added")
        )
        social_status = lead.get("social_ownership_status", "UNKNOWN")
        social_source = lead.get("social_source", "PROFILE_SCRAPE")
        social_fail = lead.get("social_failure_state")
        dim_social = self.evaluate_dimension(
            FreshnessDimension.SOCIAL.value,
            social_val,
            social_ts,
            social_source,
            social_status,
            social_fail,
            ref,
        )

        # 5. REVIEW
        rev_val = {
            "review_count": lead.get("review_count"),
            "rating": lead.get("rating"),
            "latest_review_date": lead.get("latest_review_date", ""),
        }
        rev_ts = (
            lead.get("review_evidence_updated_at")
            or lead.get("reviews_refreshed_at")
            or lead.get("latest_review_date")
            or lead.get("date_added")
        )
        rev_status = lead.get("evidence_freshness", "UNKNOWN")
        rev_source = lead.get("review_source", "TAVILY_OR_GOSOM")
        rev_fail = lead.get("review_failure_state")
        dim_rev = self.evaluate_dimension(
            FreshnessDimension.REVIEW.value,
            rev_val,
            rev_ts,
            rev_source,
            rev_status,
            rev_fail,
            ref,
        )

        # 6. CONTACTABILITY
        contact_val = {
            "phone": lead.get("phone", ""),
            "email": lead.get("email", ""),
            "instagram": lead.get("instagram_url", ""),
            "facebook": lead.get("facebook_url", ""),
        }
        contact_ts = lead.get("contact_verified_at") or lead.get("date_added")
        contact_status = lead.get("contactability_status", "VERIFIED" if (lead.get("email") or lead.get("phone")) else "UNVERIFIED")
        contact_source = lead.get("contact_source", "DIRECT_DISCOVERY")
        contact_fail = lead.get("contact_failure_state")
        dim_contact = self.evaluate_dimension(
            FreshnessDimension.CONTACTABILITY.value,
            contact_val,
            contact_ts,
            contact_source,
            contact_status,
            contact_fail,
            ref,
        )

        dimensions = {
            FreshnessDimension.WEBSITE.value: dim_web,
            FreshnessDimension.OPERATIONAL.value: dim_op,
            FreshnessDimension.PHONE.value: dim_phone,
            FreshnessDimension.SOCIAL.value: dim_social,
            FreshnessDimension.REVIEW.value: dim_rev,
            FreshnessDimension.CONTACTABILITY.value: dim_contact,
        }

        due_dims = [k for k, d in dimensions.items() if d.is_due]
        stale_dims = [k for k, d in dimensions.items() if d.is_stale]
        failed_dims = [k for k, d in dimensions.items() if d.failure_state is not None]

        # Global freshness calculation
        if failed_dims:
            global_status = FreshnessStatus.REFRESH_FAILED.value
        elif stale_dims:
            global_status = FreshnessStatus.STALE.value
        elif due_dims:
            global_status = FreshnessStatus.DUE.value
        else:
            global_status = FreshnessStatus.FRESH.value

        # Calculate next_full_refresh_due (earliest among all dimensions)
        valid_due_dates = [
            parse_iso_or_date(d.next_refresh_due)
            for d in dimensions.values()
            if d.next_refresh_due and parse_iso_or_date(d.next_refresh_due)
        ]
        next_full_refresh_due = min(valid_due_dates).isoformat() if valid_due_dates else ref.isoformat()

        # Last full refresh at (latest checked_at if all checked, or explicit field)
        last_full = lead.get("last_full_refresh_at")
        if not last_full:
            checked_dates = [
                parse_iso_or_date(d.checked_at)
                for d in dimensions.values()
                if d.checked_at and parse_iso_or_date(d.checked_at)
            ]
            last_full = max(checked_dates).isoformat() if checked_dates else None

        # Priority determination (Section 5)
        q_state = str(lead.get("qualification_state", "")).strip().upper()
        outreach_status = str(lead.get("outreach_status", "")).strip().upper()

        # Check protection
        is_protected = (
            outreach_status in PROTECTED_HISTORICAL_STATES
            or lead.get("email_suppressed", False)
            or lead_id in ("LEAD-MAN-0363CF", "LEAD-MAN-4DB3EF")
        )

        if q_state == "OUTREACH_READY":
            priority_tier = RefreshPriorityTier.TIER_A.value
            priority_score = 1
        elif q_state == "MANUAL_REVIEW":
            priority_tier = RefreshPriorityTier.TIER_B.value
            priority_score = 2
        elif lead.get("phone") or lead.get("email") or lead.get("instagram_url"):
            priority_tier = RefreshPriorityTier.TIER_C.value
            priority_score = 3
        elif q_state == "RESEARCH_ONLY":
            priority_tier = RefreshPriorityTier.TIER_D.value
            priority_score = 4
        else:
            priority_tier = RefreshPriorityTier.TIER_E.value
            priority_score = 5

        return LeadFreshnessSummary(
            lead_id=lead_id,
            company_name=company_name,
            freshness_status=global_status,
            last_full_refresh_at=last_full,
            next_full_refresh_due=next_full_refresh_due,
            priority_tier=priority_tier,
            priority_score=priority_score,
            qualification_state=q_state or "UNKNOWN",
            outreach_status=outreach_status or "NOT_READY",
            is_protected=is_protected,
            dimensions=dimensions,
            due_dimensions=due_dims,
            stale_dimensions=stale_dims,
            failed_dimensions=failed_dims,
        )

    def build_refresh_queue(
        self,
        leads: List[Dict[str, Any]],
        max_limit: int = 25,
        reference_time: Optional[datetime] = None,
        force_all: bool = False,
    ) -> List[LeadFreshnessSummary]:
        """
        Builds a deterministic, priority-ordered refresh queue.
        Sorting keys:
          1. Priority score (1 = Tier A -> 5 = Tier E)
          2. Freshness status (DUE/STALE first)
          3. Next refresh due date (earliest first)
          4. Canonical lead_id (tie-breaker)
        """
        ref = reference_time or datetime.now(timezone.utc)
        summaries: List[LeadFreshnessSummary] = []
        seen_lead_ids = set()

        for lead in leads:
            if not isinstance(lead, dict):
                continue
            lid = lead.get("lead_id")
            if not lid or lid in seen_lead_ids:
                continue
            seen_lead_ids.add(lid)

            summary = self.evaluate_lead(lead, reference_time=ref)
            if force_all or summary.freshness_status in (FreshnessStatus.DUE.value, FreshnessStatus.STALE.value, FreshnessStatus.REFRESH_FAILED.value) or summary.due_dimensions:
                summaries.append(summary)

        # Sort deterministically
        def sort_key(s: LeadFreshnessSummary):
            status_weight = 0 if s.freshness_status in (FreshnessStatus.STALE.value, FreshnessStatus.DUE.value) else 1
            due_str = s.next_full_refresh_due or "9999-99-99T99:99:99"
            return (s.priority_score, status_weight, due_str, s.lead_id)

        summaries.sort(key=sort_key)
        return summaries[:max_limit]
