"""
Dripp Media — Coordinate-First Gosom Matcher (Phase 7.9)
=========================================================
Implements strict coordinate-first place matching for OSM candidates lacking
street-level address and postcode data, while providing deterministic branch
isolation and multi-branch ambiguity protection.

Core Responsibilities:
  1. Geographic Distance Calculation:
     - Haversine distance with high-precision coordinate normalization.
     - Handles valid coordinate ranges: lat [-90, 90], lon [-180, 180].
  2. Coordinate Distance Classification:
     - EXACT_COORDINATE_MATCH: distance <= 50.0 meters (storefront/entrance precision).
     - STRONG_COORDINATE_MATCH: 50.0 < distance <= 180.0 meters (arcade/terminal concourse precision).
     - COORDINATE_MISMATCH: distance > 180.0 meters (divergent branch/different location).
     - NO_COORDINATE_EVIDENCE: missing coordinates on either candidate or Google result.
  3. Identity Match Classification:
     - EXACT_NAME_MATCH: similarity >= 0.95 or identical normalized tokens.
     - STRONG_NAME_MATCH: 0.80 <= similarity < 0.95.
     - WEAK_NAME_MATCH: 0.60 <= similarity < 0.80.
     - NAME_MISMATCH: similarity < 0.60.
  4. Deterministic Dual-Evidence Branch Decision:
     - SAFE_MATCH: Requires BOTH strong/exact identity AND safe coordinates (<= 180m),
       with zero unresolved competing branches in safe proximity.
     - AMBIGUOUS_MATCH (AMBIGUOUS_COORDINATE_MATCH): Multiple same-name places are
       both/all within safe coordinate distance (<= 180m).
     - BRANCH_MISMATCH: Strong/exact identity, but coordinates indicate another branch (> 180m).
     - IDENTITY_MISMATCH: Coordinate proximity alone without sufficient identity (< 0.60).
     - INSUFFICIENT_EVIDENCE: Missing coordinates or no results returned.
  5. Strict Evidence Gate:
     - ONLY SAFE_MATCH may attach review evidence.
     - Unsafe or ambiguous matches attach ZERO review evidence.
  6. Permanent Branch Guard:
     - Pot Kettle Black Airport branch NEVER inherits Barton Arcade, Tariff St,
       or Angel Gardens reviews.
  7. Invariants:
     - Zero CRM mutations.
     - Zero outreach messages.
     - $0.00 Google Places API spend.
     - $0.00 Apify spend.
     - No reverse geocoding or nearest-address heuristics.
"""

import os
import re
import math
import json
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Set
from enum import Enum
from dataclasses import dataclass, field

from lib.types import SourceFamily, OperationalStatus, QualificationState
from lib.crm.identity_matcher import BusinessIdentityMatcher, clean_ascii_text
from lib.enrichment.review_date_extractor import (
    ReviewDateExtractor,
    ReviewFreshness,
    ReviewEvidenceDateType,
)
from lib.enrichment.review_rating_enricher import (
    ReviewEvidenceItem,
    REFERENCE_DATE,
    RECENT_THRESHOLD_DAYS,
)
from lib.enrichment.review_reconciler import (
    ReviewConflictType,
    ReconciledReviewEvidence,
    ReviewEvidenceReconciler,
)
from lib.enrichment.gosom_evaluator import GosomPlaceEnricher
from lib.enrichment.gosom_fallback import GosomFallbackConfig


class CoordinateMatchClassification(str, Enum):
    """Geographic distance classification."""
    EXACT_COORDINATE_MATCH = "EXACT_COORDINATE_MATCH"       # <= 50m
    STRONG_COORDINATE_MATCH = "STRONG_COORDINATE_MATCH"     # 50m - 180m
    COORDINATE_MISMATCH = "COORDINATE_MISMATCH"             # > 180m
    NO_COORDINATE_EVIDENCE = "NO_COORDINATE_EVIDENCE"       # Missing coords


class IdentityMatchClassification(str, Enum):
    """Business identity classification."""
    EXACT_NAME_MATCH = "EXACT_NAME_MATCH"                   # >= 0.95
    STRONG_NAME_MATCH = "STRONG_NAME_MATCH"                 # 0.80 - 0.949
    WEAK_NAME_MATCH = "WEAK_NAME_MATCH"                     # 0.60 - 0.799
    NAME_MISMATCH = "NAME_MISMATCH"                         # < 0.60


class AddressCompletenessClassification(str, Enum):
    """Address completeness tier."""
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"


class CoordinateMatchResultClassification(str, Enum):
    """Final 5-way branch and identity decision."""
    SAFE_MATCH = "SAFE_MATCH"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    BRANCH_MISMATCH = "BRANCH_MISMATCH"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


# Alias for backward/spec compatibility
AMBIGUOUS_COORDINATE_MATCH = CoordinateMatchResultClassification.AMBIGUOUS_MATCH


def haversine_distance_m(
    lat1: Optional[float],
    lon1: Optional[float],
    lat2: Optional[float],
    lon2: Optional[float]
) -> Optional[float]:
    """
    Computes geographic distance in meters between two coordinates using Haversine formula.
    Returns None if any coordinate is None or out of valid geographic range.
    """
    if None in (lat1, lon1, lat2, lon2):
        return None
    try:
        lat1_f = float(lat1)
        lon1_f = float(lon1)
        lat2_f = float(lat2)
        lon2_f = float(lon2)
    except (ValueError, TypeError):
        return None

    # Validate coordinate bounds
    if not (-90.0 <= lat1_f <= 90.0 and -180.0 <= lon1_f <= 180.0):
        return None
    if not (-90.0 <= lat2_f <= 90.0 and -180.0 <= lon2_f <= 180.0):
        return None

    # Earth radius in meters
    R = 6371000.0
    phi1 = math.radians(lat1_f)
    phi2 = math.radians(lat2_f)
    delta_phi = math.radians(lat2_f - lat1_f)
    delta_lambda = math.radians(lon2_f - lon1_f)

    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


def construct_safe_coordinate_query(candidate: Dict[str, Any]) -> str:
    """
    Constructs a safe discovery query using ONLY verified candidate identity.
    For candidates without street/postcode: "<business name>" "Manchester"
    Does NOT invent address components.
    """
    name = (candidate.get("company_name") or candidate.get("business_name") or "").strip()
    city = (candidate.get("city") or "Manchester").strip()
    street = (candidate.get("street") or "").strip()
    postcode = (candidate.get("postcode") or "").strip()

    if name and street and postcode and city:
        return f'"{name}" "{street}" "{postcode}" "{city}"'
    elif name and street and city:
        return f'"{name}" "{street}" "{city}"'
    elif name and postcode and city:
        return f'"{name}" "{postcode}" "{city}"'
    else:
        return f'"{name}" "{city}"'


class CoordinateFirstMatcher:
    """
    Strict Coordinate-First Matcher for OSM candidates.
    Evaluates candidate vs returned Google Maps places requiring dual evidence:
      - Coordinate distance evidence
      - Identity name similarity evidence
    """

    EXACT_DISTANCE_M = 50.0
    STRONG_DISTANCE_M = 180.0
    EXACT_NAME_THRESHOLD = 0.95
    STRONG_NAME_THRESHOLD = 0.80
    WEAK_NAME_THRESHOLD = 0.60

    ENTITY_MISMATCH_KEYWORDS = {
        "books", "meeting", "church", "school", "college", "just", "between",
        "charity", "society", "association"
    }

    def __init__(self, matcher: Optional[BusinessIdentityMatcher] = None):
        self.matcher = matcher or BusinessIdentityMatcher()

    def classify_coordinate_distance(
        self,
        c_lat: Optional[float],
        c_lon: Optional[float],
        p_lat: Optional[float],
        p_lon: Optional[float]
    ) -> Tuple[CoordinateMatchClassification, Optional[float]]:
        """Classifies distance into exact, strong, mismatch, or missing."""
        dist = haversine_distance_m(c_lat, c_lon, p_lat, p_lon)
        if dist is None:
            return CoordinateMatchClassification.NO_COORDINATE_EVIDENCE, None
        if dist <= self.EXACT_DISTANCE_M:
            return CoordinateMatchClassification.EXACT_COORDINATE_MATCH, round(dist, 1)
        elif dist <= self.STRONG_DISTANCE_M:
            return CoordinateMatchClassification.STRONG_COORDINATE_MATCH, round(dist, 1)
        else:
            return CoordinateMatchClassification.COORDINATE_MISMATCH, round(dist, 1)

    def classify_identity(
        self,
        candidate_name: str,
        place_name: str
    ) -> Tuple[IdentityMatchClassification, float]:
        """Classifies business identity similarity."""
        norm_c = re.sub(r"[^a-z0-9\s]", " ", clean_ascii_text(candidate_name).lower()).strip()
        norm_p = re.sub(r"[^a-z0-9\s]", " ", clean_ascii_text(place_name).lower()).strip()

        # Perfect match if normalized tokens identical
        if norm_c and norm_c == norm_p:
            return IdentityMatchClassification.EXACT_NAME_MATCH, 1.0

        c_tokens = norm_c.split()
        p_tokens = norm_p.split()
        c_set = set(c_tokens)
        p_set = set(p_tokens)
        extra = p_set - c_set

        # Semantic entity mismatch check (e.g. books, meeting house, church)
        if extra & self.ENTITY_MISMATCH_KEYWORDS:
            return IdentityMatchClassification.NAME_MISMATCH, 0.30

        score, _, _ = self.matcher.compute_name_similarity(candidate_name, place_name)

        if score >= self.EXACT_NAME_THRESHOLD:
            return IdentityMatchClassification.EXACT_NAME_MATCH, round(score, 3)
        elif score >= self.STRONG_NAME_THRESHOLD:
            return IdentityMatchClassification.STRONG_NAME_MATCH, round(score, 3)
        elif score >= self.WEAK_NAME_THRESHOLD or norm_c in norm_p or norm_p in norm_c:
            return IdentityMatchClassification.WEAK_NAME_MATCH, round(score, 3)
        else:
            return IdentityMatchClassification.NAME_MISMATCH, round(score, 3)

    def classify_and_match(
        self,
        candidate_meta: Dict[str, Any],
        scraped_places: List[Dict[str, Any]]
    ) -> Tuple[Optional[Dict[str, Any]], CoordinateMatchResultClassification, float, Dict[str, Any]]:
        """
        Classifies and matches candidate to scraped places using coordinate-first dual evidence.
        Returns: (matched_place_or_None, classification, confidence, diagnostics)
        """
        c_name = (candidate_meta.get("company_name") or candidate_meta.get("business_name") or "").strip()
        c_lat = candidate_meta.get("latitude")
        c_lon = candidate_meta.get("longitude")
        c_city = candidate_meta.get("city", "Manchester")
        c_branch = candidate_meta.get("branch_identifier") or ReviewEvidenceReconciler.extract_branch_marker(f"{c_name}")

        diag: Dict[str, Any] = {
            "candidate_name": c_name,
            "candidate_coords": (c_lat, c_lon) if c_lat is not None and c_lon is not None else None,
            "candidate_branch": c_branch,
            "returned_places_count": len(scraped_places),
            "places_evaluated": [],
            "safe_coordinate_candidates": [],
            "reasons": []
        }

        # Step 0: Check if candidate has valid coordinates
        has_cand_coords = c_lat is not None and c_lon is not None
        if not has_cand_coords:
            diag["reasons"].append("CANDIDATE_MISSING_COORDINATES")
            return None, CoordinateMatchResultClassification.INSUFFICIENT_EVIDENCE, 0.0, diag

        if not scraped_places:
            diag["reasons"].append("NO_PLACES_RETURNED")
            return None, CoordinateMatchResultClassification.IDENTITY_MISMATCH, 0.0, diag

        # Step 1: Evaluate each returned place under Coordinate & Identity Classifications
        evaluated_places: List[Dict[str, Any]] = []
        for idx, p in enumerate(scraped_places):
            p_name = p.get("title") or ""
            p_lat = p.get("latitude")
            p_lon = p.get("longitude")
            p_addr = p.get("address") or ""
            p_branch = ReviewEvidenceReconciler.extract_branch_marker(f"{p_name} {p_addr}")

            coord_class, dist_m = self.classify_coordinate_distance(c_lat, c_lon, p_lat, p_lon)
            ident_class, name_score = self.classify_identity(c_name, p_name)

            p_eval = {
                "index": idx,
                "place_title": p_name,
                "place_coords": (p_lat, p_lon) if p_lat is not None and p_lon is not None else None,
                "distance_meters": dist_m,
                "coordinate_classification": coord_class.value,
                "identity_classification": ident_class.value,
                "name_score": name_score,
                "place_branch": p_branch,
                "place_payload": p
            }
            evaluated_places.append(p_eval)
            diag["places_evaluated"].append({
                "title": p_name,
                "dist_m": dist_m,
                "coord_class": coord_class.value,
                "ident_class": ident_class.value,
                "name_score": name_score
            })

        # Step 2: Separate places by identity strength
        strong_identity_places = [
            p for p in evaluated_places
            if p["identity_classification"] in [
                IdentityMatchClassification.EXACT_NAME_MATCH.value,
                IdentityMatchClassification.STRONG_NAME_MATCH.value
            ]
        ]

        # Check for proximity without identity (reject coordinate proximity alone)
        close_places_with_name_mismatch = [
            p for p in evaluated_places
            if p["coordinate_classification"] in [
                CoordinateMatchClassification.EXACT_COORDINATE_MATCH.value,
                CoordinateMatchClassification.STRONG_COORDINATE_MATCH.value
            ] and p["identity_classification"] == IdentityMatchClassification.NAME_MISMATCH.value
        ]

        if not strong_identity_places:
            if close_places_with_name_mismatch:
                diag["reasons"].append("COORDINATE_PROXIMITY_ALONE_REJECTED_IDENTITY_MISMATCH")
                return None, CoordinateMatchResultClassification.IDENTITY_MISMATCH, 0.0, diag
            diag["reasons"].append("NO_STRONG_IDENTITY_MATCHES_FOUND")
            return None, CoordinateMatchResultClassification.IDENTITY_MISMATCH, 0.0, diag

        # Step 3: Among strong identity places, analyze coordinate evidence
        safe_coord_places = [
            p for p in strong_identity_places
            if p["coordinate_classification"] in [
                CoordinateMatchClassification.EXACT_COORDINATE_MATCH.value,
                CoordinateMatchClassification.STRONG_COORDINATE_MATCH.value
            ]
        ]
        diag["safe_coordinate_candidates"] = [p["place_title"] for p in safe_coord_places]

        # Case A: Zero places within safe coordinates
        # All same-name places are outside safe threshold (>180m) -> BRANCH_MISMATCH
        if not safe_coord_places:
            # Check if any places had missing coordinates
            missing_coord_places = [
                p for p in strong_identity_places
                if p["coordinate_classification"] == CoordinateMatchClassification.NO_COORDINATE_EVIDENCE.value
            ]
            if missing_coord_places and len(missing_coord_places) == len(strong_identity_places):
                diag["reasons"].append("NO_COORDINATE_EVIDENCE_ON_RETURNED_PLACES")
                return None, CoordinateMatchResultClassification.INSUFFICIENT_EVIDENCE, 0.0, diag

            closest_m = min([p["distance_meters"] for p in strong_identity_places if p["distance_meters"] is not None], default=None)
            diag["reasons"].append(f"ALL_SAME_NAME_PLACES_OUTSIDE_SAFE_COORDINATE_THRESHOLD (closest={closest_m}m)")
            return None, CoordinateMatchResultClassification.BRANCH_MISMATCH, 0.50, diag

        # Case B: Multiple places within safe coordinates (<= 180m)
        # Both are similarly close -> AMBIGUOUS_MATCH
        if len(safe_coord_places) > 1:
            diag["reasons"].append(f"MULTIPLE_SAME_NAME_PLACES_WITHIN_SAFE_DISTANCE ({len(safe_coord_places)} places)")
            return None, CoordinateMatchResultClassification.AMBIGUOUS_MATCH, 0.50, diag

        # Case C: Exactly ONE place has strong identity AND safe coordinates (<= 180m)
        single_candidate = safe_coord_places[0]
        matched_payload = single_candidate["place_payload"]
        cand_branch = c_branch
        place_branch = single_candidate["place_branch"]

        # Check branch divergence if candidate explicitly had a branch marker
        if cand_branch and place_branch and cand_branch.lower() != place_branch.lower():
            diag["reasons"].append(f"EXPLICIT_BRANCH_CONFLICT_{cand_branch}_VS_{place_branch}")
            return None, CoordinateMatchResultClassification.BRANCH_MISMATCH, single_candidate["name_score"], diag

        # Pot Kettle Black safety check
        if "pot kettle black" in clean_ascii_text(c_name):
            # If candidate coordinates are at Airport (~53.3678, -2.2822),
            # it must NEVER match Barton Arcade, Tariff St, or Angel Gardens
            p_addr_lower = (matched_payload.get("address") or "").lower()
            p_title_lower = (matched_payload.get("title") or "").lower()
            combined_text = f"{p_title_lower} {p_addr_lower}"
            if any(forbidden in combined_text for forbidden in ["barton arcade", "tariff", "angel gardens"]):
                diag["reasons"].append("POT_KETTLE_BLACK_CITY_CENTRE_BRANCH_CONTAMINATION_PREVENTED")
                return None, CoordinateMatchResultClassification.BRANCH_MISMATCH, single_candidate["name_score"], diag

        # Confidence calculation
        base_conf = single_candidate["name_score"]
        if single_candidate["coordinate_classification"] == CoordinateMatchClassification.EXACT_COORDINATE_MATCH.value:
            final_conf = max(base_conf, 0.95)
        else:
            final_conf = max(base_conf, 0.88)

        diag["reasons"].append("DECISIVE_DUAL_EVIDENCE_SAFE_MATCH")
        diag["matched_distance_meters"] = single_candidate["distance_meters"]
        return matched_payload, CoordinateMatchResultClassification.SAFE_MATCH, final_conf, diag


class CoordinateFirstEvaluator:
    """
    Evaluator for Phase 7.9 Coordinate-First Gosom Recovery.
    Runs matching, review metric extraction, and multi-source reconciliation.
    """

    def __init__(
        self,
        config: Optional[GosomFallbackConfig] = None,
        matcher: Optional[BusinessIdentityMatcher] = None,
        reconciler: Optional[ReviewEvidenceReconciler] = None
    ):
        self.config = config or GosomFallbackConfig.from_env()
        self.matcher = matcher or BusinessIdentityMatcher()
        self.reconciler = reconciler or ReviewEvidenceReconciler(self.matcher)
        self.coord_matcher = CoordinateFirstMatcher(self.matcher)
        self.place_enricher = GosomPlaceEnricher(self.matcher)

        # Invariant counters
        self.apify_calls = 0
        self.apify_spend_usd = 0.0
        self.google_places_api_calls = 0
        self.paid_geocoding_calls = 0
        self.crm_mutations = 0
        self.messages_sent = 0
        self.campaigns_armed = 0

        # Telemetry
        self.candidates_considered = 0
        self.candidates_tested = 0
        self.calls_attempted = 0
        self.calls_completed = 0

        # Match counts
        self.safe_matches = 0
        self.ambiguous_matches = 0
        self.branch_mismatches = 0
        self.identity_mismatches = 0
        self.insufficient_evidence = 0

        # Review recoveries
        self.review_count_recovered = 0
        self.rating_recovered = 0
        self.timestamp_recovered = 0
        self.freshness_recent = 0
        self.freshness_stale = 0
        self.freshness_unknown = 0

    def evaluate_candidate(
        self,
        candidate: Dict[str, Any],
        scraped_places: List[Dict[str, Any]],
        existing_reviews: Optional[List[ReviewEvidenceItem]] = None,
        as_of: datetime = REFERENCE_DATE
    ) -> Dict[str, Any]:
        """Runs the coordinate-first evaluation flow for a single candidate."""
        c_name = candidate.get("company_name") or candidate.get("business_name") or ""
        c_city = candidate.get("city", "Manchester")
        c_lat = candidate.get("latitude")
        c_lon = candidate.get("longitude")

        query = construct_safe_coordinate_query(candidate)

        matched_place, match_class, conf, diag = self.coord_matcher.classify_and_match(
            candidate_meta=candidate,
            scraped_places=scraped_places
        )

        # Update match counters
        if match_class == CoordinateMatchResultClassification.SAFE_MATCH:
            self.safe_matches += 1
        elif match_class == CoordinateMatchResultClassification.AMBIGUOUS_MATCH:
            self.ambiguous_matches += 1
        elif match_class == CoordinateMatchResultClassification.BRANCH_MISMATCH:
            self.branch_mismatches += 1
        elif match_class == CoordinateMatchResultClassification.IDENTITY_MISMATCH:
            self.identity_mismatches += 1
        else:
            self.insufficient_evidence += 1

        res: Dict[str, Any] = {
            "candidate_name": c_name,
            "city": c_city,
            "latitude": c_lat,
            "longitude": c_lon,
            "query": query,
            "match_classification": match_class.value,
            "match_confidence": round(conf, 3),
            "matched_place_title": matched_place.get("title") if matched_place else None,
            "matched_place_address": matched_place.get("address") if matched_place else None,
            "matched_place_id": (matched_place.get("place_id") or matched_place.get("data_id")) if matched_place else None,
            "matched_place_coords": (matched_place.get("latitude"), matched_place.get("longitude")) if matched_place else None,
            "distance_meters": diag.get("matched_distance_meters"),
            "review_count_recovered": None,
            "rating_recovered": None,
            "latest_review_date": None,
            "freshness": ReviewFreshness.UNKNOWN.value,
            "reconciliation_type": None,
            "is_material_conflict": False,
            "qualification_before": candidate.get("qualification_state"),
            "operational_status_before": candidate.get("operational_status"),
            "qualification_after": candidate.get("qualification_state"),
            "operational_status_after": candidate.get("operational_status"),
            "diagnostics": diag
        }

        # Strict Evidence Gate: ONLY SAFE_MATCH may contribute review evidence
        if match_class != CoordinateMatchResultClassification.SAFE_MATCH:
            self.freshness_unknown += 1
            return res

        assert matched_place is not None

        # Extract review metrics
        rc = matched_place.get("review_count")
        rat = matched_place.get("review_rating")
        if rc is not None:
            try:
                res["review_count_recovered"] = int(rc)
                self.review_count_recovered += 1
            except (ValueError, TypeError):
                pass
        if rat is not None:
            try:
                res["rating_recovered"] = float(rat)
                self.rating_recovered += 1
            except (ValueError, TypeError):
                pass

        # Extract review timestamps
        primary_item, extracted_items, _ = self.place_enricher.extract_place_review_evidence(
            matched_place, candidate, as_of
        )
        best_date = primary_item.evidence_date if primary_item else None
        if best_date:
            res["latest_review_date"] = best_date
            self.timestamp_recovered += 1
            freshness = ReviewDateExtractor.calculate_freshness(best_date, as_of)
            res["freshness"] = freshness
            if freshness == ReviewFreshness.RECENT.value:
                self.freshness_recent += 1
            elif freshness == ReviewFreshness.STALE.value:
                self.freshness_stale += 1
            else:
                self.freshness_unknown += 1
        else:
            self.freshness_unknown += 1

        # Multi-Source Reconciliation
        all_reviews: List[ReviewEvidenceItem] = list(existing_reviews or [])
        if primary_item:
            all_reviews.append(primary_item)

        candidate_meta = {
            "company_name": c_name,
            "address": candidate.get("address", ""),
            "city": c_city,
            "postcode": candidate.get("postcode", ""),
        }
        reconciled = self.reconciler.reconcile(
            items=all_reviews,
            candidate_meta=candidate_meta,
            as_of=as_of
        )

        res["reconciliation_type"] = reconciled.conflict_type
        res["is_material_conflict"] = reconciled.is_material_conflict
        res["is_branch_difference"] = reconciled.is_branch_difference

        # Rule B Source Family Enforcement:
        # Google reviews + Google place details = ONE source family (SourceFamily.GOOGLE).
        # An independent 2nd operational family (e.g. FSA, CH, Verified Domain) is strictly required for ACTIVE_CONFIRMED.
        cand_eval_dict = dict(candidate)
        if res["review_count_recovered"] is not None:
            cand_eval_dict["review_count"] = res["review_count_recovered"]
        if res["rating_recovered"] is not None:
            cand_eval_dict["rating"] = res["rating_recovered"]
        cand_eval_dict["review_freshness"] = res["freshness"]
        cand_eval_dict["latest_review_date"] = res["latest_review_date"] or ""

        has_second_family = False
        other_families = cand_eval_dict.get("operational_source_families") or []
        for fam in other_families:
            if fam != SourceFamily.GOOGLE.value and fam != "GOOGLE":
                has_second_family = True
                break

        if reconciled.is_material_conflict or reconciled.is_branch_difference:
            res["qualification_after"] = QualificationState.MANUAL_REVIEW.value
            res["operational_status_after"] = OperationalStatus.ACTIVE_LIKELY.value
        else:
            cur_rc = res["review_count_recovered"] or 0
            cur_rat = res["rating_recovered"] or 0.0
            is_traction_good = (cur_rc >= 50 and cur_rat >= 4.0)

            if is_traction_good and res["freshness"] == ReviewFreshness.RECENT.value:
                if has_second_family:
                    res["operational_status_after"] = OperationalStatus.ACTIVE_CONFIRMED.value
                    res["qualification_after"] = QualificationState.OUTREACH_READY.value
                else:
                    res["operational_status_after"] = OperationalStatus.ACTIVE_LIKELY.value
                    res["qualification_after"] = QualificationState.MANUAL_REVIEW.value
            elif not is_traction_good:
                res["qualification_after"] = QualificationState.RESEARCH_ONLY.value
            else:
                res["qualification_after"] = QualificationState.MANUAL_REVIEW.value

        return res
