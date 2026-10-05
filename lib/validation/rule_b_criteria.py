"""
Rule B Criteria Canonical Specification (Frozen V1)
===================================================

Defines the single source of truth for Multi-Signal Operational Rule (Rule B)
qualification criteria, eliminating any reporting or schema discrepancies.
"""

from typing import Dict, Any, Tuple

RULE_B_VERSION: str = "FROZEN"

RULE_B_CRITERIA: Tuple[Dict[str, Any], ...] = (
    {
        "id": "RULE_B_01_REVIEW_VOLUME",
        "name": "Review Volume",
        "description": "Candidate must possess at least 50 verified customer reviews (review_count >= 50).",
        "metric_key": "review_count",
        "operator": ">=",
        "threshold": 50,
    },
    {
        "id": "RULE_B_02_MINIMUM_RATING",
        "name": "Minimum Rating",
        "description": "Candidate must maintain an aggregate rating of at least 4.0 stars (rating >= 4.0).",
        "metric_key": "rating",
        "operator": ">=",
        "threshold": 4.0,
    },
    {
        "id": "RULE_B_03_ACCEPTED_SOURCE",
        "name": "Accepted Review Source",
        "description": "Review evidence must originate from an accepted primary consumer directory (e.g. Google Maps).",
        "metric_key": "source",
        "accepted_sources": ("google_maps", "google"),
    },
    {
        "id": "RULE_B_04_REVIEW_RECENCY",
        "name": "Review Recency",
        "description": "Most recent authentic customer review must be within 180 days of execution (review_date <= 180d).",
        "metric_key": "review_date",
        "operator": "<=",
        "max_age_days": 180,
    },
    {
        "id": "RULE_B_05_OPERATIONAL_SIGNAL",
        "name": "Independent Operational Signal",
        "description": "Candidate must be independently corroborated as currently active (operational_status == VERIFIED_ACTIVE).",
        "metric_key": "operational_status",
        "required_value": "VERIFIED_ACTIVE",
    },
    {
        "id": "RULE_B_06_IDENTITY_LOCATION",
        "name": "Identity & Location Verified",
        "description": "Identity confidence >= 0.70 with coordinate (<180m) and token validation preventing wrong-branch capture.",
        "metric_key": "identity_confidence",
        "operator": ">=",
        "threshold": 0.70,
    },
    {
        "id": "RULE_B_07_NO_REVIEW_CONFLICT",
        "name": "No Unresolved Review Conflict",
        "description": "Zero unresolved material rating (>0.8★ spread) or volume (>50% variance) conflicts across review sources.",
        "metric_key": "review_status",
        "forbidden_values": ("CONFLICTING",),
    },
    {
        "id": "RULE_B_08_NO_CLOSURE_RED_FLAGS",
        "name": "Zero Closure / Red Flags",
        "description": "Candidate must not be permanently closed or trigger disqualifying red flags (operational_status != CLOSED).",
        "metric_key": "operational_status",
        "forbidden_values": ("CLOSED",),
    },
)


def get_canonical_rule_b_criteria_count() -> int:
    """Returns the exact count of canonical Rule B criteria (strictly 8)."""
    return len(RULE_B_CRITERIA)


def get_canonical_rule_b_criteria() -> Tuple[Dict[str, Any], ...]:
    """Returns immutable tuple of canonical Rule B criteria definitions."""
    return RULE_B_CRITERIA
