"""
Dripp Media — Review Evidence Reconciler & Branch-Aware Conflict Detection (Phase 7.3)
=====================================================================================
Auditable review evidence reconciliation layer.
Preserves every source record, evaluates cross-source compatibility, isolates distinct branches,
and detects material review conflicts before qualification.

Safety Invariants:
  - Never assume the first discovered source is authoritative.
  - Never silently overwrite one source with another.
  - Never automatically average ratings without documented statistical/business justification.
  - Never select largest review count without defensible basis.
  - Branch differences (e.g. City Centre vs Airport) must NOT be merged or treated as rating conflicts.
  - When material review conflicts exist, route to MANUAL_REVIEW.
"""

import os
import re
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple, Set

from lib.types import DiscoveredBusiness, SourceFamily
from lib.enrichment.review_date_extractor import (
    ReviewDateExtractor,
    ReviewEvidenceDateType,
    ReviewDateConfidence,
    ReviewFreshness,
)
from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
    ReviewEvidenceItem,
    ReviewEnrichmentResult,
    ReviewConfidence,
    ReviewStatus,
    REFERENCE_DATE,
)
from lib.crm.identity_matcher import BusinessIdentityMatcher, clean_ascii_text


class ReviewConflictType(str, Enum):
    """Explicit taxonomy of review evidence reconciliation outcomes."""
    NO_CONFLICT = "NO_CONFLICT"
    COUNT_CONFLICT = "COUNT_CONFLICT"
    RATING_CONFLICT = "RATING_CONFLICT"
    FRESHNESS_CONFLICT = "FRESHNESS_CONFLICT"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    BRANCH_DIFFERENCE = "BRANCH_DIFFERENCE"
    MAJOR_REVIEW_CONFLICT = "MAJOR_REVIEW_CONFLICT"


# Known UK sub-localities, shopping centers, transit terminals, and branch markers
BRANCH_MARKERS = [
    r'\bterminal\s*\d+\b',
    r'\bairport\b',
    r'\bbarton\s*arcade\b',
    r'\bnorthern\s*quarter\b',
    r'\bancoats\b',
    r'\bdidsbury\b',
    r'\bchorlton\b',
    r'\bspinningfields\b',
    r'\bcorn\s*exchange\b',
    r'\btrafford\s*centre\b',
    r'\bpiccadilly\b',
    r'\bdeansgate\b',
    r'\bmediacity\b',
    r'\boxford\s*road\b',
    r'\bstockport\s*road\b',
    r'\bking\s*street\b',
    r'\bcastlefield\b',
    r'\bfallowfield\b',
    r'\bwithington\b',
    r'\brusholme\b',
    r'\bcheetham\s*hill\b',
]


@dataclass
class ReconciledReviewEvidence:
    """Auditable result of multi-source review evidence reconciliation."""
    conflict_type: str = ReviewConflictType.NO_CONFLICT.value
    is_material_conflict: bool = False
    is_branch_difference: bool = False
    conflict_reasons: List[str] = field(default_factory=list)
    reconciled_review_count: Optional[int] = None
    reconciled_rating: Optional[float] = None
    reconciled_date: Optional[str] = None
    reconciled_freshness: str = ReviewFreshness.UNKNOWN.value
    reconciled_status: str = ReviewStatus.NOT_FOUND.value
    reconciled_confidence: str = ReviewConfidence.UNKNOWN.value
    primary_source: str = ""
    primary_source_family: str = SourceFamily.UNKNOWN.value
    primary_source_url: str = ""
    branch_detected: Optional[str] = None
    branch_divergence_details: Optional[Dict[str, Any]] = None
    sources_evaluated: List[Dict[str, Any]] = field(default_factory=list)
    reconciliation_decision: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ReviewEvidenceReconciler:
    """
    Reconciles review evidence across multiple accepted source families.
    Preserves all source provenance, detects branch differences, identifies
    material discrepancies, and determines safe qualification inputs.
    """

    def __init__(self, matcher: Optional[BusinessIdentityMatcher] = None):
        self.matcher = matcher or BusinessIdentityMatcher()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. BRANCH IDENTIFIER EXTRACTION
    # ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def extract_branch_marker(text: str) -> Optional[str]:
        """
        Extracts distinctive physical branch/terminal/neighborhood markers from text/title/URL.
        """
        if not text:
            return None
        text_low = text.lower()
        for pat in BRANCH_MARKERS:
            m = re.search(pat, text_low)
            if m:
                return m.group(0).strip()
        return None

    @staticmethod
    def extract_uk_outcode(postcode: str) -> Optional[str]:
        """Extracts the outward code of a UK postcode (e.g. 'M3 2BW' -> 'M3', 'M90 4ZY' -> 'M90')."""
        if not postcode:
            return None
        pc_clean = postcode.strip().upper()
        m = re.match(r'^([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})?$', pc_clean)
        if m:
            return m.group(1)
        return None

    # ──────────────────────────────────────────────────────────────────────────
    # 2. BRANCH DIVERGENCE DETECTION
    # ──────────────────────────────────────────────────────────────────────────
    def detect_branch_difference(
        self,
        item1: ReviewEvidenceItem,
        item2: ReviewEvidenceItem,
        candidate_meta: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        Determines whether two evidence items represent distinct branches of the same brand.
        Examines:
          - Branch markers in titles/snippets/URLs (e.g. 'Terminal 2' vs 'Barton Arcade')
          - Disparate postcode outcodes (e.g. 'M90' airport vs 'M3' city center)
          - Street address divergence
        """
        cand_meta = candidate_meta or {}
        text1 = f"{item1.business_name} {item1.branch_identifier} {item1.evidence_text} {item1.source_url} {item1.street}"
        text2 = f"{item2.business_name} {item2.branch_identifier} {item2.evidence_text} {item2.source_url} {item2.street}"

        b1 = item1.branch_identifier or self.extract_branch_marker(text1)
        b2 = item2.branch_identifier or self.extract_branch_marker(text2)

        # Check explicit branch markers
        if b1 and b2 and b1 != b2:
            return True, f"Distinct branch locations detected: '{b1}' vs '{b2}'", {
                "branch_1": b1,
                "branch_2": b2,
                "source_1": item1.source,
                "source_2": item2.source
            }

        # Check postcode outcodes if present
        pc1 = item1.postcode or (self.extract_uk_outcode(text1))
        pc2 = item2.postcode or (self.extract_uk_outcode(text2))
        out1 = self.extract_uk_outcode(pc1) if pc1 else None
        out2 = self.extract_uk_outcode(pc2) if pc2 else None

        if out1 and out2 and out1 != out2:
            # Significant outcode difference (e.g. M3 vs M90)
            return True, f"Disparate geographic postcodes detected: '{out1}' vs '{out2}'", {
                "outcode_1": out1,
                "outcode_2": out2,
                "source_1": item1.source,
                "source_2": item2.source
            }

        # Check if one item has a clear branch marker while the other lacks it
        if (b1 and not b2) or (b2 and not b1):
            detected_b = b1 or b2
            cand_addr = str(cand_meta.get("address", "")).lower()
            if detected_b and detected_b not in cand_addr and "terminal" in detected_b or "airport" in detected_b:
                return True, f"One listing references specialized branch '{detected_b}' not present in candidate address", {
                    "isolated_branch": detected_b,
                    "candidate_address": cand_addr
                }

        return False, None, None

    # ──────────────────────────────────────────────────────────────────────────
    # 3. CONFLICT DETECTION
    # ──────────────────────────────────────────────────────────────────────────
    def detect_conflicts(
        self,
        items: List[ReviewEvidenceItem],
        candidate_meta: Optional[Dict[str, Any]] = None
    ) -> Tuple[ReviewConflictType, List[str], bool]:
        """
        Examines evidence items for material discrepancies before qualification.
        Returns: (conflict_type, reasons, is_material_conflict)
        """
        valid_items = [it for it in items if not it.reject_reason and (it.review_count is not None or it.rating is not None or it.evidence_date)]
        if len(valid_items) < 2:
            return ReviewConflictType.NO_CONFLICT, [], False

        reasons: List[str] = []
        is_material = False
        conflict_types: Set[ReviewConflictType] = set()

        # ── Check 1: Identity & City Conflicts ──
        for i in range(len(valid_items)):
            for j in range(i + 1, len(valid_items)):
                it_a = valid_items[i]
                it_b = valid_items[j]

                # City difference
                city_a = (it_a.city or "").strip().lower()
                city_b = (it_b.city or "").strip().lower()
                if city_a and city_b and city_a != city_b:
                    conflict_types.add(ReviewConflictType.IDENTITY_CONFLICT)
                    reasons.append(f"Incompatible cities across sources: '{it_a.city}' ({it_a.source}) vs '{it_b.city}' ({it_b.source})")
                    is_material = True

                # Phone difference
                ph_a = re.sub(r'[^0-9]', '', it_a.phone or "")
                ph_b = re.sub(r'[^0-9]', '', it_b.phone or "")
                if ph_a and ph_b and len(ph_a) >= 8 and len(ph_b) >= 8 and ph_a != ph_b:
                    conflict_types.add(ReviewConflictType.IDENTITY_CONFLICT)
                    reasons.append(f"Conflicting primary telephone numbers: '{it_a.phone}' vs '{it_b.phone}'")
                    is_material = True

                # Branch difference
                is_branch, branch_msg, branch_details = self.detect_branch_difference(it_a, it_b, candidate_meta)
                if is_branch:
                    conflict_types.add(ReviewConflictType.BRANCH_DIFFERENCE)
                    reasons.append(f"Branch divergence: {branch_msg}")
                    is_material = True

        # ── Check 2: Rating Conflicts ──
        ratings_with_items = [(it.rating, it) for it in valid_items if it.rating is not None]
        if len(ratings_with_items) >= 2:
            ratings = [r for r, _ in ratings_with_items]
            max_r = max(ratings)
            min_r = min(ratings)
            diff_r = max_r - min_r

            # Material rating disparity >= 0.5 stars
            if diff_r >= 0.5:
                conflict_types.add(ReviewConflictType.RATING_CONFLICT)
                is_material = True
                reasons.append(f"Material star rating disparity: {max_r:.1f}★ vs {min_r:.1f}★ (diff: {diff_r:.1f}★)")

            # Threshold crossing: one >= 4.0 (qualifying) while another < 3.0 or < 4.0
            has_qualifying = any(r >= 4.0 for r in ratings)
            has_substandard = any(r < 3.0 for r in ratings)
            if has_qualifying and has_substandard:
                conflict_types.add(ReviewConflictType.RATING_CONFLICT)
                is_material = True
                reasons.append("Rating threshold crossing: one source qualifies (>= 4.0★) while another fails (< 3.0★)")

        # ── Check 3: Review Count Conflicts ──
        counts_with_items = [(it.review_count, it) for it in valid_items if it.review_count is not None]
        if len(counts_with_items) >= 2:
            counts = [c for c, _ in counts_with_items]
            sorted_counts = sorted(counts)
            min_c = sorted_counts[0]
            max_c = sorted_counts[-1]

            if min_c > 0 and (max_c / min_c) > 2.0 and (max_c - min_c) >= 100:
                conflict_types.add(ReviewConflictType.COUNT_CONFLICT)
                is_material = True
                reasons.append(f"Material review count disparity: {max_c} vs {min_c} (ratio: {max_c/min_c:.1f}x, diff: {max_c - min_c})")

        # ── Determine Overall Conflict Type ──
        if not conflict_types:
            return ReviewConflictType.NO_CONFLICT, [], False

        if ReviewConflictType.BRANCH_DIFFERENCE in conflict_types:
            # Branch difference takes precedence in classification
            return ReviewConflictType.BRANCH_DIFFERENCE, reasons, True

        if len(conflict_types) >= 2 or (ReviewConflictType.RATING_CONFLICT in conflict_types and ReviewConflictType.COUNT_CONFLICT in conflict_types):
            return ReviewConflictType.MAJOR_REVIEW_CONFLICT, reasons, True

        primary_conflict = list(conflict_types)[0]
        return primary_conflict, reasons, is_material

    # ──────────────────────────────────────────────────────────────────────────
    # 4. EVIDENCE RECONCILIATION
    # ──────────────────────────────────────────────────────────────────────────
    def reconcile(
        self,
        items: List[ReviewEvidenceItem],
        candidate_meta: Optional[Dict[str, Any]] = None,
        as_of: datetime = REFERENCE_DATE
    ) -> ReconciledReviewEvidence:
        """
        Auditably reconciles multi-source review evidence into a single safe decision payload.
        Ensures:
          - Every source record is preserved in sources_evaluated.
          - Ratings are never averaged without basis.
          - Material conflicts block automated OUTREACH_READY.
          - Genuine recent dates are preserved when identity is corroborated.
        """
        all_eval_dicts = [it.to_dict() for it in items]
        valid_items = [it for it in items if not it.reject_reason and (it.review_count is not None or it.rating is not None or it.evidence_date)]

        if not valid_items:
            # No valid review items discovered
            return ReconciledReviewEvidence(
                conflict_type=ReviewConflictType.NO_CONFLICT.value,
                is_material_conflict=False,
                reconciled_status=ReviewStatus.NOT_FOUND.value,
                reconciled_confidence=ReviewConfidence.UNKNOWN.value,
                reconciled_freshness=ReviewFreshness.UNKNOWN.value,
                sources_evaluated=all_eval_dicts,
                reconciliation_decision="No usable review evidence found across checked sources."
            )

        # Detect conflicts across valid evidence items
        conflict_type, conflict_reasons, is_material = self.detect_conflicts(valid_items, candidate_meta)

        if is_material:
            # Material conflict or branch difference: Do NOT average or pick arbitrarily.
            # Preserve all underlying records and surface CONFLICT_REQUIRES_REVIEW
            decision_msg = f"Material conflict detected ({conflict_type.value}): {'; '.join(conflict_reasons)}. Gated to MANUAL_REVIEW."
            return ReconciledReviewEvidence(
                conflict_type=conflict_type.value,
                is_material_conflict=True,
                is_branch_difference=(conflict_type == ReviewConflictType.BRANCH_DIFFERENCE),
                conflict_reasons=conflict_reasons,
                reconciled_review_count=None,
                reconciled_rating=None,
                reconciled_date=None,
                reconciled_freshness=ReviewFreshness.UNKNOWN.value,
                reconciled_status=ReviewStatus.CONFLICT_REQUIRES_REVIEW.value,
                reconciled_confidence=ReviewConfidence.CONFLICT.value,
                primary_source="Multiple (Conflicting)",
                primary_source_family=SourceFamily.UNKNOWN.value,
                sources_evaluated=all_eval_dicts,
                reconciliation_decision=decision_msg
            )

        # ── Case: Consistent evidence across sources (NO_CONFLICT) ──
        # Rank items by quality and evidence completeness
        def _item_quality(it: ReviewEvidenceItem) -> int:
            score = 0
            if it.confidence == ReviewConfidence.HIGH.value:
                score += 100
            elif it.confidence == ReviewConfidence.MEDIUM.value:
                score += 50
            if it.freshness == ReviewFreshness.RECENT.value:
                score += 40
            elif it.freshness == ReviewFreshness.UNKNOWN.value:
                score += 10
            if it.review_count is not None:
                score += 15
            if it.rating is not None:
                score += 15
            if it.evidence_date:
                score += 20
            return score

        valid_items.sort(key=_item_quality, reverse=True)
        primary = valid_items[0]

        # Check if any other source has a genuine recent date that corroborates
        best_date = primary.evidence_date
        best_freshness = primary.freshness
        for it in valid_items:
            if it.freshness == ReviewFreshness.RECENT.value and it.evidence_date:
                best_date = it.evidence_date
                best_freshness = ReviewFreshness.RECENT.value
                break

        sf = primary.source_family
        if not sf or sf == "UNKNOWN":
            from lib.validation.operational_validator import OperationalValidator
            sf = OperationalValidator.classify_source_family(primary.source or primary.source_url).value

        return ReconciledReviewEvidence(
            conflict_type=ReviewConflictType.NO_CONFLICT.value,
            is_material_conflict=False,
            is_branch_difference=False,
            reconciled_review_count=primary.review_count,
            reconciled_rating=primary.rating,
            reconciled_date=best_date,
            reconciled_freshness=best_freshness,
            reconciled_status=ReviewStatus.FOUND.value,
            reconciled_confidence=primary.confidence,
            primary_source=primary.source,
            primary_source_family=sf,
            primary_source_url=primary.source_url,
            sources_evaluated=all_eval_dicts,
            reconciliation_decision=f"Harmonized evidence across {len(valid_items)} consistent source(s). Selected {primary.source} as primary reference."
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 5. ATTACH TO DISCOVERED BUSINESS
    # ──────────────────────────────────────────────────────────────────────────
    @classmethod
    def apply_to_business(
        cls,
        business: DiscoveredBusiness,
        reconciled: ReconciledReviewEvidence
    ):
        """
        Safely attaches reconciled review evidence to a DiscoveredBusiness instance.
        Ensures downstream gates (OperationalValidator and LeadScoringProvider)
        receive explicit conflict or verified evidence fields.
        """
        # Ensure raw_data exists
        if not hasattr(business, "raw_data") or not isinstance(business.raw_data, dict):
            business.raw_data = {}

        business.raw_data["review_enrichment"] = {
            "review_count": reconciled.reconciled_review_count,
            "rating": reconciled.reconciled_rating,
            "review_evidence_date": reconciled.reconciled_date,
            "review_freshness": reconciled.reconciled_freshness,
            "review_source": reconciled.primary_source_family,
            "review_source_url": reconciled.primary_source_url,
            "review_confidence": reconciled.reconciled_confidence,
            "review_status": reconciled.reconciled_status,
            "conflict_type": reconciled.conflict_type,
            "is_material_conflict": reconciled.is_material_conflict,
            "is_branch_difference": reconciled.is_branch_difference,
            "conflict_reasons": reconciled.conflict_reasons,
            "reconciliation_decision": reconciled.reconciliation_decision,
            "sources_evaluated": reconciled.sources_evaluated
        }

        # If material conflict exists, do NOT overwrite top-level counts with misleading values
        if reconciled.is_material_conflict:
            business.review_count = None
            business.rating = None
            business.latest_review_date = ""
            business.review_source_family = SourceFamily.UNKNOWN.value
        else:
            business.review_count = reconciled.reconciled_review_count
            business.rating = reconciled.reconciled_rating
            business.latest_review_date = reconciled.reconciled_date or ""
            business.review_source_family = reconciled.primary_source_family
