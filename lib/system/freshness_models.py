"""
lib/system/freshness_models.py
==============================
Canonical Lead Freshness Data Models & Taxonomy (Phase 10.4).

Defines:
  - 6 Independent Freshness Dimensions:
      1. WEBSITE STATUS
      2. OPERATIONAL STATUS
      3. PHONE
      4. SOCIAL LINKS
      5. REVIEW EVIDENCE
      6. CONTACTABILITY
  - Canonical Freshness Lifecycle Statuses:
      FRESH, DUE, STALE, REFRESHING, REFRESH_FAILED
  - Priority-Based Ordering (A: OUTREACH_READY -> E: ARCHIVED)
  - Research / Refresh Failure Taxonomy
  - Change History Entries and Aggregated Summaries
"""

from enum import Enum
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional


class FreshnessDimension(str, Enum):
    """Canonical 6 Independent Lead Freshness Dimensions."""
    WEBSITE = "WEBSITE"
    OPERATIONAL = "OPERATIONAL"
    PHONE = "PHONE"
    SOCIAL = "SOCIAL"
    REVIEW = "REVIEW"
    CONTACTABILITY = "CONTACTABILITY"


class FreshnessStatus(str, Enum):
    """Canonical Global and Dimensional Freshness Statuses."""
    FRESH = "FRESH"
    DUE = "DUE"
    STALE = "STALE"
    REFRESHING = "REFRESHING"
    REFRESH_FAILED = "REFRESH_FAILED"


class RefreshPriorityTier(str, Enum):
    """Priority order for re-checking leads (Section 5)."""
    TIER_A = "A"  # OUTREACH_READY (highest priority)
    TIER_B = "B"  # Recently qualified leads / MANUAL_REVIEW
    TIER_C = "C"  # Contactable prospects (RESEARCH_ONLY with contacts)
    TIER_D = "D"  # Research-only prospects
    TIER_E = "E"  # Disqualified / archived records (lowest priority)


class RefreshFailureReason(str, Enum):
    """Canonical Research / Refresh Failure Taxonomy (Section 17)."""
    NONE = ""
    GENUINELY_NO_EVIDENCE_FOUND = "GENUINELY_NO_EVIDENCE_FOUND"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"


# Default Cadence in Days (Section 4). Configurable at runtime.
DEFAULT_REFRESH_CADENCE_DAYS: Dict[str, int] = {
    FreshnessDimension.WEBSITE.value: 30,
    FreshnessDimension.OPERATIONAL.value: 30,
    FreshnessDimension.PHONE.value: 60,
    FreshnessDimension.SOCIAL.value: 30,
    FreshnessDimension.REVIEW.value: 14,  # Most frequent due to Rule B recency
    FreshnessDimension.CONTACTABILITY.value: 30,
}

# Protected Historical States that must NEVER be overwritten or promoted to outreach
PROTECTED_HISTORICAL_STATES = {
    "SENT",
    "BOUNCED",
    "SUPPRESSED",
    "NEVER_CONFIRMED_SENT",
}


@dataclass
class DimensionFreshness:
    """Independent Freshness State for a single evidence dimension (Section 2)."""
    dimension: str
    value: Any
    checked_at: Optional[str] = None
    source: str = "UNKNOWN"
    confidence_or_status: str = "UNKNOWN"
    next_refresh_due: Optional[str] = None
    failure_state: Optional[str] = None
    age_days: Optional[int] = None
    is_due: bool = False
    is_stale: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ChangeHistoryEntry:
    """Audit entry recording factual modification to canonical lead (Section 18)."""
    dimension: str
    field_name: str
    old_value: Any
    new_value: Any
    source: str
    timestamp: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LeadFreshnessSummary:
    """Canonical Freshness Summary for a Lead (Section 3)."""
    lead_id: str
    company_name: str
    freshness_status: str  # FreshnessStatus value
    last_full_refresh_at: Optional[str]
    next_full_refresh_due: Optional[str]
    priority_tier: str     # RefreshPriorityTier value
    priority_score: int    # 1 (highest) to 5 (lowest)
    qualification_state: str
    outreach_status: str
    is_protected: bool
    dimensions: Dict[str, DimensionFreshness] = field(default_factory=dict)
    due_dimensions: List[str] = field(default_factory=list)
    stale_dimensions: List[str] = field(default_factory=list)
    failed_dimensions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        res = asdict(self)
        res["dimensions"] = {k: v.to_dict() if hasattr(v, "to_dict") else v for k, v in self.dimensions.items()}
        return res
