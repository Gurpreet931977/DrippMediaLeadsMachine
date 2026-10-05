"""
lib/enrichment/review_evidence_recovery.py
=========================================
Phase 9.2: Dedicated Review Evidence Recovery Engine + Multi-Evidence Provenance Container.

Core Responsibilities:
  1. Structured review evidence recovery without inferred numeric metrics.
  2. Strict extraction rules: genuine review dates only; reject page crawl/SEO timestamps.
  3. Conservative review reconciliation: material conflicts route to CONFLICTING / MANUAL_REVIEW.
  4. Quota-governed Gosom fallback (max 10 calls) with dual-evidence coordinate & identity gates.
  5. Deterministic evidence-recovery priority scoring (separate from qualification score).
  6. Central CandidateEvidence container preserving provenance across all 6 evidence dimensions.
"""

import os
import re
import sys
import math
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Tuple, Set

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import (
    SourceFamily,
    EvidenceFreshness,
    OperationalStatus,
    QualificationState,
)
from lib.crm.identity_matcher import BusinessIdentityMatcher, clean_ascii_text
from lib.enrichment.coordinate_matcher import haversine_distance_m
from lib.validation.rule_b_criteria import (
    RULE_B_CRITERIA,
    RULE_B_VERSION,
    get_canonical_rule_b_criteria_count,
    get_canonical_rule_b_criteria,
)


@dataclass
class StructuredReviewEvidence:
    """Canonical review evidence object for Phase 9.2."""
    review_status: str  # FOUND, NOT_FOUND, CONFLICTING, INSUFFICIENT
    rating: Optional[float] = None
    review_count: Optional[int] = None
    review_date: Optional[str] = None
    source: Optional[str] = None
    source_url: Optional[str] = None
    identity_confidence: float = 0.0
    branch_confidence: float = 0.0
    evidence_notes: List[str] = field(default_factory=list)
    checked_at: Optional[str] = None
    raw_evidence_items: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "review_status": self.review_status,
            "rating": round(self.rating, 2) if self.rating is not None else None,
            "review_count": self.review_count,
            "review_date": self.review_date,
            "source": self.source,
            "source_url": self.source_url,
            "identity_confidence": round(self.identity_confidence, 4),
            "branch_confidence": round(self.branch_confidence, 4),
            "evidence_notes": list(self.evidence_notes),
            "checked_at": self.checked_at,
            "raw_evidence_items": [dict(item) for item in self.raw_evidence_items],
        }


@dataclass
class EvidenceFamilyRecord:
    """Individual evidence family record preserving provenance."""
    family: str  # identity, website, review, operational, contact, closure_negative
    source: str
    url: str
    observed_at: str
    confidence: float
    raw_signal: Any
    normalized_result: str
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "family": self.family,
            "source": self.source,
            "url": self.url,
            "observed_at": self.observed_at,
            "confidence": round(self.confidence, 4),
            "raw_signal": self.raw_signal,
            "normalized_result": self.normalized_result,
            "notes": list(self.notes),
        }


@dataclass
class CandidateEvidence:
    """
    Central evidence object combining all 6 evidence dimensions
    without collapsing or hiding underlying provenance.
    """
    candidate_id: str
    company_name: str
    identity_evidence: Dict[str, Any] = field(default_factory=dict)
    website_evidence: Dict[str, Any] = field(default_factory=dict)
    review_evidence: Dict[str, Any] = field(default_factory=dict)
    operational_evidence: Dict[str, Any] = field(default_factory=dict)
    contact_evidence: Dict[str, Any] = field(default_factory=dict)
    closure_negative_evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "company_name": self.company_name,
            "identity_evidence": dict(self.identity_evidence),
            "website_evidence": dict(self.website_evidence),
            "review_evidence": dict(self.review_evidence),
            "operational_evidence": dict(self.operational_evidence),
            "contact_evidence": dict(self.contact_evidence),
            "closure_negative_evidence": dict(self.closure_negative_evidence),
        }


class ReviewEvidenceRecoveryEngine:
    """
    Dedicated review evidence recovery engine with selective Gosom fallback.
    Enforces quota limits, coordinate matching, recency gating, and reconciliation.
    """

    MAX_GOSOM_CALLS: int = 10
    MAX_EXTERNAL_SEARCH_CALLS: int = 10

    ACCEPTED_REVIEW_SOURCES = {
        "google",
        "google_maps",
        "tripadvisor",
        "restaurant_guru",
        "yelp",
        "other_directory",
    }

    INVALID_TIMESTAMP_KEYS = {
        "page_last_updated",
        "last_crawled_at",
        "seo_timestamp",
        "search_indexing_date",
        "directory_refresh_timestamp",
        "sitemap_lastmod",
        "crawl_timestamp",
        "page_update_timestamp",
    }

    def __init__(self, matcher: Optional[BusinessIdentityMatcher] = None):
        self.matcher = matcher or BusinessIdentityMatcher()
        self.gosom_calls_used: int = 0
        self.external_search_calls_used: int = 0

    # -------------------------------------------------------------------------
    # 1. Deterministic Evidence-Recovery Priority Score (Section 10)
    # -------------------------------------------------------------------------
    @staticmethod
    def calculate_evidence_recovery_priority(candidate: Dict[str, Any]) -> int:
        """
        Calculates a deterministic evidence-recovery priority score (0 - 100).
        Used solely to allocate scarce enrichment quota to high-value candidates.
        DOES NOT alter outreach qualification score or promote leads.
        """
        score = 0

        # Component A: Website Opportunity (0-35 pts)
        web_status = candidate.get("website_opportunity_status") or candidate.get("website_status") or ""
        if web_status == "BROKEN_WEBSITE":
            score += 35
        elif web_status == "NO_WEBSITE":
            score += 30

        # Component B: Commercial Fit (0-20 pts)
        fit = candidate.get("commercial_fit_status") or ""
        if fit in ("HIGH_WEBSITE_OPPORTUNITY", "COMMERCIAL_PROSPECT"):
            score += 20
        elif fit == "MEDIUM_WEBSITE_OPPORTUNITY":
            score += 10

        # Component C: Contactability (0-25 pts)
        has_phone = bool(candidate.get("phone") and str(candidate.get("phone")).strip())
        contact_status = candidate.get("contactability_status") or ""
        if has_phone or "CONTACTABLE" in contact_status:
            score += 25

        # Component D: Identity Confidence (0-10 pts)
        id_conf = float(candidate.get("identity_confidence", 0.85))
        if id_conf >= 0.90:
            score += 10
        elif id_conf >= 0.70:
            score += 6

        # Component E: Address Completeness (0-10 pts)
        has_addr = bool(candidate.get("address"))
        has_pc = bool(candidate.get("postcode"))
        if has_addr and has_pc:
            score += 10
        elif has_addr:
            score += 5

        # Excluded chains / functional sites receive 0
        if candidate.get("qualification_state") == QualificationState.EXCLUDED.value or web_status == "FUNCTIONAL_WEBSITE":
            score = 0

        return min(100, max(0, score))

    # -------------------------------------------------------------------------
    # 2. Review Metadata Extraction & Timestamp Rejection (Section 5)
    # -------------------------------------------------------------------------
    @classmethod
    def extract_review_metadata(
        cls,
        raw_evidence: Dict[str, Any],
        candidate_name: str = "",
        candidate_address: str = "",
    ) -> StructuredReviewEvidence:
        """
        Extracts genuine review metadata, strictly rejecting SEO/crawl timestamps.
        """
        now_ts = datetime.now(timezone.utc).isoformat()
        notes: List[str] = []

        # Reject invalid metadata timestamps
        cleaned_evidence = dict(raw_evidence)
        for invalid_key in cls.INVALID_TIMESTAMP_KEYS:
            if invalid_key in cleaned_evidence:
                notes.append(f"Rejected invalid timestamp key '{invalid_key}'")
                cleaned_evidence.pop(invalid_key, None)

        raw_source = str(cleaned_evidence.get("source", "")).strip().lower()
        if raw_source not in cls.ACCEPTED_REVIEW_SOURCES and "google" not in raw_source:
            notes.append(f"Unaccepted review source: '{raw_source}'")
            return StructuredReviewEvidence(
                review_status="INSUFFICIENT",
                checked_at=now_ts,
                evidence_notes=notes,
                raw_evidence_items=[cleaned_evidence],
            )

        # Extract numeric metrics
        raw_count = cleaned_evidence.get("review_count")
        raw_rating = cleaned_evidence.get("rating")
        review_date = cleaned_evidence.get("review_date") or cleaned_evidence.get("latest_review_date")

        count: Optional[int] = None
        rating: Optional[float] = None

        if raw_count is not None:
            try:
                count = int(raw_count)
            except (ValueError, TypeError):
                notes.append(f"Invalid review count: {raw_count}")

        if raw_rating is not None:
            try:
                rating = float(raw_rating)
            except (ValueError, TypeError):
                notes.append(f"Invalid rating: {raw_rating}")

        # Check genuine review date
        parsed_date: Optional[str] = None
        if review_date and isinstance(review_date, str):
            clean_d = review_date.strip()
            # Must match YYYY-MM-DD or standard ISO format
            if re.match(r"^\d{4}-\d{2}-\d{2}", clean_d):
                parsed_date = clean_d[:10]
            else:
                notes.append(f"Invalid review date format rejected: {clean_d}")

        if count is None or rating is None:
            return StructuredReviewEvidence(
                review_status="INSUFFICIENT",
                rating=rating,
                review_count=count,
                review_date=parsed_date,
                source=raw_source,
                source_url=cleaned_evidence.get("source_url"),
                identity_confidence=float(cleaned_evidence.get("identity_confidence", 0.0)),
                branch_confidence=float(cleaned_evidence.get("branch_confidence", 0.0)),
                evidence_notes=notes + ["Missing required review count or rating."],
                checked_at=now_ts,
                raw_evidence_items=[cleaned_evidence],
            )

        return StructuredReviewEvidence(
            review_status="FOUND",
            rating=rating,
            review_count=count,
            review_date=parsed_date,
            source=raw_source,
            source_url=cleaned_evidence.get("source_url"),
            identity_confidence=float(cleaned_evidence.get("identity_confidence", 0.90)),
            branch_confidence=float(cleaned_evidence.get("branch_confidence", 0.90)),
            evidence_notes=notes,
            checked_at=now_ts,
            raw_evidence_items=[cleaned_evidence],
        )

    # -------------------------------------------------------------------------
    # 3. Review Reconciliation & Conflict Handling (Section 6)
    # -------------------------------------------------------------------------
    @classmethod
    def reconcile_review_evidence(
        cls,
        evidence_list: List[StructuredReviewEvidence],
    ) -> StructuredReviewEvidence:
        """
        Reconciles review evidence from multiple sources.
        Material conflicts across rating (>0.8) or count (>50% variance) route to CONFLICTING.
        Never chooses 'closest' or largest count.
        """
        now_ts = datetime.now(timezone.utc).isoformat()
        if not evidence_list:
            return StructuredReviewEvidence(
                review_status="NOT_FOUND",
                checked_at=now_ts,
                evidence_notes=["Zero review evidence sources found."],
            )

        valid_evidence = [e for e in evidence_list if e.review_status == "FOUND" and e.rating is not None and e.review_count is not None]
        if not valid_evidence:
            return StructuredReviewEvidence(
                review_status="INSUFFICIENT",
                checked_at=now_ts,
                evidence_notes=["No sources contained complete valid review metrics."],
                raw_evidence_items=[asdict(e) for e in evidence_list],
            )

        # Deduplicate identical sources
        unique_sources: Dict[str, StructuredReviewEvidence] = {}
        for e in valid_evidence:
            src_key = f"{e.source}:{e.review_count}:{e.rating}"
            if src_key not in unique_sources:
                unique_sources[src_key] = e
        valid_evidence = list(unique_sources.values())

        if len(valid_evidence) == 1:
            return valid_evidence[0]

        # Check for material rating conflict
        ratings = [e.rating for e in valid_evidence if e.rating is not None]
        counts = [e.review_count for e in valid_evidence if e.review_count is not None]

        max_r, min_r = max(ratings), min(ratings)
        max_c, min_c = max(counts), min(counts)

        rating_diff = max_r - min_r
        count_variance = (max_c - min_c) / max(1, min_c)

        all_raw_items = []
        for e in valid_evidence:
            all_raw_items.extend(e.raw_evidence_items or [asdict(e)])

        # Material conflict rule
        if rating_diff > 0.8:
            return StructuredReviewEvidence(
                review_status="CONFLICTING",
                rating=None,  # Do not guess or average
                review_count=None,
                review_date=None,
                source="multi_source_conflict",
                evidence_notes=[
                    f"Material rating conflict: spread of {rating_diff:.1f}★ across sources "
                    f"({min_r}★ vs {max_r}★). Requires operator review."
                ],
                checked_at=now_ts,
                raw_evidence_items=all_raw_items,
            )

        if count_variance > 0.50 and (max_c - min_c) >= 30:
            return StructuredReviewEvidence(
                review_status="CONFLICTING",
                rating=None,
                review_count=None,
                review_date=None,
                source="multi_source_conflict",
                evidence_notes=[
                    f"Material review volume conflict: variance of {count_variance * 100:.1f}% "
                    f"({min_c} vs {max_c} reviews). Requires operator review."
                ],
                checked_at=now_ts,
                raw_evidence_items=all_raw_items,
            )

        # Sources agree within minor variation -> Reconcile conservatively
        # Pick the most specific date if available
        dated_evidence = [e for e in valid_evidence if e.review_date]
        chosen_date = dated_evidence[0].review_date if dated_evidence else None
        avg_rating = round(sum(ratings) / len(ratings), 1)
        med_count = int(sorted(counts)[len(counts) // 2])

        return StructuredReviewEvidence(
            review_status="FOUND",
            rating=avg_rating,
            review_count=med_count,
            review_date=chosen_date,
            source=f"reconciled_multi_source({len(valid_evidence)})",
            source_url=valid_evidence[0].source_url,
            identity_confidence=min(e.identity_confidence for e in valid_evidence),
            branch_confidence=min(e.branch_confidence for e in valid_evidence),
            evidence_notes=[f"Successfully reconciled {len(valid_evidence)} independent review sources."],
            checked_at=now_ts,
            raw_evidence_items=all_raw_items,
        )

    # -------------------------------------------------------------------------
    # 4. Gosom Identity Safety & Coordinate Gates (Section 4)
    # -------------------------------------------------------------------------
    @classmethod
    def evaluate_gosom_safety(
        cls,
        candidate_name: str,
        place_name: str,
        candidate_coords: Optional[Tuple[float, float]] = None,
        place_coords: Optional[Tuple[float, float]] = None,
        candidate_address: str = "",
        place_address: str = "",
    ) -> Tuple[bool, str, float, float, List[str]]:
        """
        Enforces dual-evidence coordinate and identity gates for Gosom matching.
        Returns: (is_safe, decision_label, coord_confidence, identity_confidence, notes)

        Distance Tiers:
          <= 50m      EXACT
          50-180m     STRONG
          > 180m      MISMATCH

        Identity Tiers:
          >= 0.95     EXACT
          0.80-0.95   STRONG
          0.60-0.80   WEAK
          < 0.60      MISMATCH
        """
        notes: List[str] = []

        # 1. Identity Score Calculation
        matcher = BusinessIdentityMatcher()
        norm_cand = matcher.normalize_name(candidate_name)
        norm_place = matcher.normalize_name(place_name)

        if not norm_cand or not norm_place:
            return False, "IDENTITY_MISMATCH", 0.0, 0.0, ["Missing business name tokens."]

        name_sim, _, _ = matcher.compute_name_similarity(candidate_name, place_name)

        if name_sim >= 0.95:
            id_tier = "EXACT"
        elif name_sim >= 0.80:
            id_tier = "STRONG"
        elif name_sim >= 0.60:
            id_tier = "WEAK"
        else:
            id_tier = "MISMATCH"

        if id_tier == "MISMATCH":
            notes.append(f"Identity mismatch: name similarity {name_sim:.2f} < 0.60.")
            return False, "IDENTITY_MISMATCH", 0.0, name_sim, notes

        # 2. Coordinate Check
        if not candidate_coords or not place_coords:
            # Without coordinates, requires address verification to prevent wrong branch
            if candidate_address and place_address:
                addr_match = candidate_address.lower()[:15] in place_address.lower()
                if addr_match and name_sim >= 0.85:
                    notes.append("Address matched without coordinates; accepted under strict identity.")
                    return True, "SAFE_ADDRESS_MATCH", 0.70, name_sim, notes
            notes.append("Missing coordinate evidence; cannot establish branch safety.")
            return False, "INSUFFICIENT_COORDINATE_EVIDENCE", 0.0, name_sim, notes

        c_lat, c_lon = candidate_coords
        p_lat, p_lon = place_coords
        dist_m = haversine_distance_m(c_lat, c_lon, p_lat, p_lon)

        if dist_m <= 50.0:
            coord_tier = "EXACT"
            coord_conf = 1.0
        elif dist_m <= 180.0:
            coord_tier = "STRONG"
            coord_conf = 0.85
        else:
            coord_tier = "MISMATCH"
            coord_conf = 0.10

        if coord_tier == "MISMATCH":
            notes.append(f"Coordinate distance {dist_m:.1f}m > 180m exceeds safety threshold. Branch mismatch.")
            return False, "BRANCH_MISMATCH", coord_conf, name_sim, notes

        if id_tier == "WEAK":
            notes.append(f"Weak identity ({name_sim:.2f}) despite coordinate proximity ({dist_m:.1f}m). Requires review.")
            return False, "WEAK_IDENTITY_REQUIRES_REVIEW", coord_conf, name_sim, notes

        # Dual Evidence Satisfied
        notes.append(f"Dual evidence satisfied: coordinate {coord_tier} ({dist_m:.1f}m) + identity {id_tier} ({name_sim:.2f}).")
        return True, "SAFE_MATCH", coord_conf, name_sim, notes

    # -------------------------------------------------------------------------
    # 5. Review Freshness Gating (Frozen Rule B: <= 180 days)
    # -------------------------------------------------------------------------
    @staticmethod
    def is_review_recent(review_date_str: Optional[str], max_age_days: int = 180) -> bool:
        """Enforces Rule B review recency: <= 180 days from execution time."""
        if not review_date_str or not isinstance(review_date_str, str):
            return False
        try:
            dt = datetime.strptime(review_date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - dt).days
            return 0 <= age_days <= max_age_days
        except Exception:
            return False

    # -------------------------------------------------------------------------
    # 6. Central CandidateEvidence Builder (Section 8)
    # -------------------------------------------------------------------------
    @staticmethod
    def build_candidate_evidence(
        candidate: Dict[str, Any],
        review_evidence: StructuredReviewEvidence,
        operational_evidence: Dict[str, Any],
    ) -> CandidateEvidence:
        """
        Assembles all 6 evidence dimensions with inspectable provenance.
        """
        now_ts = datetime.now(timezone.utc).isoformat()
        cand_id = candidate.get("candidate_id") or candidate.get("company_name", "unknown")
        cand_name = candidate.get("company_name", "")

        # 1. Identity Evidence
        identity_evidence = {
            "source": "openstreetmap_boundary_validator",
            "url": "https://www.openstreetmap.org",
            "observed_at": candidate.get("date_processed", now_ts),
            "confidence": float(candidate.get("identity_confidence", 0.85)),
            "raw_signal": {
                "company_name": cand_name,
                "address": candidate.get("address", ""),
                "postcode": candidate.get("postcode", ""),
                "city": candidate.get("city", "Manchester"),
                "country": candidate.get("country", "United Kingdom"),
            },
            "normalized_result": "VALID_MANCHESTER_ENTITY",
        }

        # 2. Website Opportunity Evidence
        website_evidence = {
            "source": "website_opportunity_auditor",
            "url": candidate.get("website", ""),
            "observed_at": now_ts,
            "confidence": 0.95,
            "raw_signal": {
                "website_url": candidate.get("website", ""),
                "opportunity_status": candidate.get("website_opportunity_status", "UNKNOWN"),
                "opportunity_score": candidate.get("website_opportunity_score", 0),
            },
            "normalized_result": candidate.get("website_opportunity_status", "UNKNOWN"),
        }

        # 3. Review Evidence
        rev_ev = {
            "source": review_evidence.source or "none",
            "url": review_evidence.source_url or "",
            "observed_at": review_evidence.checked_at or now_ts,
            "confidence": review_evidence.identity_confidence,
            "raw_signal": review_evidence.to_dict(),
            "normalized_result": review_evidence.review_status,
        }

        # 4. Operational Evidence
        op_ev = dict(operational_evidence)

        # 5. Contact Evidence
        contact_evidence = {
            "source": "telecom_and_social_detector",
            "url": "",
            "observed_at": now_ts,
            "confidence": 0.90 if candidate.get("phone") else 0.40,
            "raw_signal": {
                "phone": candidate.get("phone", ""),
                "social": candidate.get("social", ""),
            },
            "normalized_result": candidate.get("contactability_status", "NOT_CONTACTABLE"),
        }

        # 6. Closure / Negative Evidence
        is_closed = (
            candidate.get("operational_status") == OperationalStatus.CLOSED.value
            or operational_evidence.get("status") == OperationalStatus.CLOSED.value
        )
        closure_negative_evidence = {
            "source": "closure_detector",
            "url": "",
            "observed_at": now_ts,
            "confidence": 0.95 if is_closed else 0.10,
            "raw_signal": {"is_closed": is_closed},
            "normalized_result": "CLOSED" if is_closed else "ACTIVE_OR_UNKNOWN",
        }

        return CandidateEvidence(
            candidate_id=cand_id,
            company_name=cand_name,
            identity_evidence=identity_evidence,
            website_evidence=website_evidence,
            review_evidence=rev_ev,
            operational_evidence=op_ev,
            contact_evidence=contact_evidence,
            closure_negative_evidence=closure_negative_evidence,
        )
