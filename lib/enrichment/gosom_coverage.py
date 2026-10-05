"""
Dripp Media — Gosom Review-Metric Coverage Recovery & Strict Branch Matching (Phase 7.6)
========================================================================================
Implements strict branch-safe place matching and review-metric recovery using the local
gosom/google-maps-scraper.

Core Responsibilities:
  1. Deterministic Query Construction:
     - Hierarchical construction: "<name>" "<street>" "<postcode>" "<city>"
     - Prevents address invention.
  2. Strict 5-Way Place Match Classification:
     - EXACT_BRANCH_MATCH: Name + location evidence (postcode/street/phone/branch) clearly agree.
     - STRONG_BUSINESS_MATCH: Single business identity with no competing branch ambiguity.
     - AMBIGUOUS_MATCH: Multiple plausible Google listings remain without resolving discriminator.
     - BRANCH_MISMATCH: Same brand/business name but clearly different branch.
     - IDENTITY_MISMATCH: Different business entirely (low name similarity / category mismatch).
  3. Strict Evidence Gate:
     - ONLY EXACT_BRANCH_MATCH or STRONG_BUSINESS_MATCH may contribute review evidence.
     - AMBIGUOUS_MATCH, BRANCH_MISMATCH, IDENTITY_MISMATCH attach ZERO review evidence.
  4. Permanent Pot Kettle Black Regression Guard:
     - Distinguishes Barton Arcade, Angel Gardens, and Tariff St.
     - Prevents cross-branch review contamination.
  5. Dual Recovery:
     - Question A: Review metric recovery (review_count, rating).
     - Question B: Review freshness recovery (genuine review timestamps <=180d).
  6. Rule B Source-Family Invariant:
     - Google reviews + Google place details count as ONE family (SourceFamily.GOOGLE).
     - Independent second operational family is strictly required for ACTIVE_CONFIRMED.
  7. Production Safety:
     - Feature flag safe-by-default: GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED=false.
     - Zero Apify calls, zero Apify spend ($0.00).
     - Zero Google Places API calls, zero Places spend ($0.00).
     - Zero CRM mutations (dry-run only).
     - Zero live outreach, zero armed campaigns.
     - Zero proxies, zero anti-bot circumvention.
"""

import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple, Set
from enum import Enum
from dataclasses import dataclass, field

from lib.types import DiscoveredBusiness, SourceFamily, OperationalStatus, QualificationState
from lib.crm.identity_matcher import BusinessIdentityMatcher, clean_ascii_text
from lib.enrichment.review_date_extractor import (
    ReviewDateExtractor,
    ReviewEvidenceDateType,
    ReviewDateConfidence,
    ReviewFreshness,
)
from lib.enrichment.review_rating_enricher import (
    ReviewEvidenceItem,
    ReviewConfidence,
    ReviewStatus,
    REFERENCE_DATE,
    RECENT_THRESHOLD_DAYS,
)
from lib.enrichment.review_reconciler import (
    ReviewConflictType,
    ReconciledReviewEvidence,
    ReviewEvidenceReconciler,
)
from lib.enrichment.gosom_evaluator import GosomReviewParser, GosomPlaceEnricher
from lib.enrichment.gosom_fallback import GosomFallbackConfig, PINNED_GOSOM_VERSION, DEFAULT_CACHE_DIR
from lib.validation.operational_validator import OperationalValidator


class PlaceMatchClassification(str, Enum):
    """
    Strict 5-way classification for place matching under Phase 7.6.
    Only EXACT_BRANCH_MATCH or STRONG_BUSINESS_MATCH may contribute review evidence.
    """
    EXACT_BRANCH_MATCH = "EXACT_BRANCH_MATCH"
    STRONG_BUSINESS_MATCH = "STRONG_BUSINESS_MATCH"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    BRANCH_MISMATCH = "BRANCH_MISMATCH"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"


def construct_gosom_query(candidate: Dict[str, Any]) -> str:
    """
    Improves Gosom query construction following Section 9 hierarchy:
      1. "<name>" "<street>" "<postcode>" "<city>"
      2. If postcode unavailable: "<name>" "<street>" "<city>"
      3. If street unavailable: "<name>" "<branch>" "<city>" or "<name>" "<postcode>" "<city>"
      4. Fallback: "<name>" "<city>"
    Does NOT invent addresses.
    """
    name = (candidate.get("company_name") or candidate.get("business_name") or "").strip()
    city = (candidate.get("city") or "Manchester").strip()
    addr = candidate.get("address") or ""
    street = (candidate.get("street") or "").strip()
    postcode = (candidate.get("postcode") or "").strip()
    branch = (candidate.get("branch_identifier") or "").strip()

    # Extract postcode if present in address
    if not postcode and addr:
        pc_m = re.search(r'\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b', addr, re.IGNORECASE)
        if pc_m:
            postcode = pc_m.group(1).upper()

    # Extract street if present in address
    if not street and addr:
        clean = addr.replace("Manchester, United Kingdom", "").replace("Manchester", "").replace("United Kingdom", "")
        if postcode:
            clean = clean.replace(postcode, "")
        parts = [p.strip() for p in clean.split(",") if p.strip()]
        for p in parts:
            if re.search(r'\b(road|rd|street|st|avenue|ave|lane|ln|way|walk|arcade|drive|dr|place|pl|terrace|gardens|square|sq)\b', p, re.IGNORECASE):
                street = p
                break

    # Extract branch marker if present in name/address
    if not branch:
        branch = ReviewEvidenceReconciler.extract_branch_marker(f"{name} {addr}") or ""

    # Hierarchy construction
    if name and street and postcode and city:
        return f'"{name}" "{street}" "{postcode}" "{city}"'
    elif name and street and city:
        return f'"{name}" "{street}" "{city}"'
    elif name and branch and city:
        return f'"{name}" "{branch}" "{city}"'
    elif name and postcode and city:
        return f'"{name}" "{postcode}" "{city}"'
    else:
        return f'"{name}" "{city}"'


def extract_uk_postcode(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r'\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b', text, re.IGNORECASE)
    return m.group(1).upper() if m else None


class StrictPlaceMatcher:
    """
    Branch-Safe Strict Place Matcher.
    Evaluates candidate vs returned Google Maps places and classifies match into:
      - EXACT_BRANCH_MATCH
      - STRONG_BUSINESS_MATCH
      - AMBIGUOUS_MATCH
      - BRANCH_MISMATCH
      - IDENTITY_MISMATCH
    """

    KNOWN_BRANCH_KEYWORDS = [
        "terminal", "airport", "station", "rail station", "arcade",
        "shopping centre", "centre", "gardens", "square", "park",
        "north", "south", "east", "west"
    ]

    def __init__(self, matcher: Optional[BusinessIdentityMatcher] = None):
        self.matcher = matcher or BusinessIdentityMatcher()

    def classify_and_match(
        self,
        candidate_meta: Dict[str, Any],
        scraped_places: List[Dict[str, Any]]
    ) -> Tuple[Optional[Dict[str, Any]], PlaceMatchClassification, float, Dict[str, Any]]:
        """
        Classifies and matches candidate to scraped places.
        Returns: (matched_place_or_None, classification, confidence, diagnostics)
        """
        c_name = (candidate_meta.get("company_name") or candidate_meta.get("business_name") or "").strip()
        c_city = (candidate_meta.get("city") or "Manchester").strip()
        c_addr = candidate_meta.get("address") or ""
        c_street = (candidate_meta.get("street") or "").strip()
        c_postcode = (candidate_meta.get("postcode") or "").strip()
        c_phone = re.sub(r'\D', '', candidate_meta.get("phone") or "")
        c_branch = candidate_meta.get("branch_identifier") or ReviewEvidenceReconciler.extract_branch_marker(f"{c_name} {c_addr}")

        diag: Dict[str, Any] = {
            "candidate_name": c_name,
            "candidate_branch": c_branch,
            "plausible_places_count": 0,
            "reasons": []
        }

        if not scraped_places:
            diag["reasons"].append("NO_PLACES_RETURNED")
            return None, PlaceMatchClassification.IDENTITY_MISMATCH, 0.0, diag

        # Extract postcode if present in address
        if not c_postcode and c_addr:
            c_postcode = extract_uk_postcode(c_addr) or ""

        # Step 1: Identify plausible candidates by brand/business name similarity
        plausible: List[Tuple[Dict[str, Any], float, Optional[str], Optional[str], Optional[str]]] = []
        for p in scraped_places:
            p_name = p.get("title") or ""
            p_addr = p.get("address") or ""
            p_phone = re.sub(r'\D', '', p.get("phone") or "")
            p_branch = ReviewEvidenceReconciler.extract_branch_marker(f"{p_name} {p_addr}")

            p_pc = extract_uk_postcode(p_addr) or ""
            norm_c_name = clean_ascii_text(c_name)
            norm_p_name = clean_ascii_text(p_name)

            # Name similarity score
            score, _, comp = self.matcher.compute_name_similarity(c_name, p_name)

            # Check if core brand token matches (e.g. "pot kettle black" in "pot kettle black barton arcade")
            is_brand_match = (
                score >= 0.70
                or norm_c_name in norm_p_name
                or norm_p_name in norm_c_name
            )

            if is_brand_match:
                plausible.append((p, score, p_branch, p_pc, p_phone))

        diag["plausible_places_count"] = len(plausible)

        # If zero plausible name matches -> IDENTITY_MISMATCH
        if not plausible:
            diag["reasons"].append("NO_NAME_SIMILARITY_MATCH")
            return None, PlaceMatchClassification.IDENTITY_MISMATCH, 0.0, diag

        # Step 2: Handle Multiple Plausible Results (Branch disambiguation required)
        if len(plausible) > 1:
            diag["reasons"].append(f"MULTIPLE_BRANCHES_FOUND_{len(plausible)}")

            # Check for exact location match among plausible places
            exact_matches: List[Tuple[Dict[str, Any], float, str]] = []
            distinct_branches_seen: Set[str] = set()

            for p, score, p_branch, p_pc, p_phone in plausible:
                p_name = p.get("title") or ""
                p_addr = p.get("address") or ""
                if p_branch:
                    distinct_branches_seen.add(p_branch.lower())

                # Check phone match
                if c_phone and p_phone and c_phone == p_phone:
                    exact_matches.append((p, max(score, 0.95), "PHONE_MATCH"))
                    continue

                # Check postcode match
                if c_postcode and p_pc and c_postcode.replace(" ", "") == p_pc.replace(" ", ""):
                    exact_matches.append((p, max(score, 0.95), "POSTCODE_MATCH"))
                    continue

                # Check branch identifier match
                if c_branch and p_branch and c_branch.lower() == p_branch.lower():
                    exact_matches.append((p, max(score, 0.95), "BRANCH_IDENTIFIER_MATCH"))
                    continue

                # Check street match in address
                if c_street and len(c_street) > 3 and c_street.lower() in p_addr.lower():
                    exact_matches.append((p, max(score, 0.90), "STREET_MATCH"))
                    continue

            # If exactly one place has decisive location evidence -> EXACT_BRANCH_MATCH
            if len(exact_matches) == 1:
                p_match, conf, reason = exact_matches[0]
                diag["reasons"].append(f"EXACT_LOCATION_MATCH_{reason}")
                return p_match, PlaceMatchClassification.EXACT_BRANCH_MATCH, conf, diag

            # If candidate specifically requested a branch (e.g. Barton Arcade)
            # but none of the returned places matched it -> BRANCH_MISMATCH
            if c_branch:
                matching_cand_branch = [p for p, s, pb, ppc, pph in plausible if pb and pb.lower() == c_branch.lower()]
                if not matching_cand_branch:
                    diag["reasons"].append(f"CANDIDATE_BRANCH_{c_branch}_NOT_IN_RETURNED_PLACES")
                    return None, PlaceMatchClassification.BRANCH_MISMATCH, 0.50, diag

            # If multiple plausible places remain and no location evidence disambiguates -> AMBIGUOUS_MATCH
            diag["reasons"].append("COMPETING_BRANCHES_WITHOUT_DISCRIMINATOR")
            return None, PlaceMatchClassification.AMBIGUOUS_MATCH, 0.50, diag

        # Step 3: Exactly One Plausible Result
        single_p, score, p_branch, p_pc, p_phone = plausible[0]
        p_name = single_p.get("title") or ""
        p_addr = single_p.get("address") or ""
        diag["single_place_title"] = p_name
        diag["single_place_address"] = p_addr

        # If candidate specified a branch, it MUST be corroborated by the place
        if c_branch:
            cand_branch_in_place = (
                (p_branch and c_branch.lower() == p_branch.lower())
                or (c_branch.lower() in p_name.lower())
                or (c_branch.lower() in p_addr.lower())
            )
            if not cand_branch_in_place:
                diag["reasons"].append(f"CANDIDATE_BRANCH_{c_branch}_MISSING_IN_PLACE")
                return None, PlaceMatchClassification.BRANCH_MISMATCH, score, diag

        # Check for branch divergence between candidate and single place
        # e.g. candidate specifies Barton Arcade, but place is Tariff St
        if c_branch and p_branch and c_branch.lower() != p_branch.lower():
            diag["reasons"].append(f"BRANCH_DIVERGENCE_{c_branch}_VS_{p_branch}")
            return None, PlaceMatchClassification.BRANCH_MISMATCH, score, diag

        # Check postcode outcode mismatch if both have postcodes
        if c_postcode and p_pc:
            c_clean_pc = c_postcode.replace(" ", "").upper()
            p_clean_pc = p_pc.replace(" ", "").upper()
            if c_clean_pc != p_clean_pc:
                c_out = c_postcode.split()[0].upper() if " " in c_postcode else c_postcode[:3].upper()
                p_out = p_pc.split()[0].upper() if " " in p_pc else p_pc[:3].upper()
                if c_out != p_out:
                    diag["reasons"].append(f"POSTCODE_OUTCODE_MISMATCH_{c_out}_VS_{p_out}")
                    return None, PlaceMatchClassification.BRANCH_MISMATCH, score, diag

        # e.g. candidate is generic "Escape Lounge, Manchester" but place is "Terminal 2, Airport"
        if p_branch and any(kw in p_branch.lower() for kw in ["terminal", "airport", "station"]):
            cand_full = f"{c_name} {c_addr}".lower()
            if not any(kw in cand_full for kw in ["terminal", "airport", "station"]):
                diag["reasons"].append(f"BRANCH_MARKER_PRESENT_IN_PLACE_BUT_ABSENT_IN_CANDIDATE_{p_branch}")
                return None, PlaceMatchClassification.BRANCH_MISMATCH, score, diag

        # Check if single place has matching location evidence
        has_location_evidence = False
        if c_phone and p_phone and c_phone == p_phone:
            has_location_evidence = True
        elif c_postcode and p_pc and c_postcode.replace(" ", "") == p_pc.replace(" ", ""):
            has_location_evidence = True
        elif c_street and len(c_street) > 3 and c_street.lower() in p_addr.lower():
            has_location_evidence = True
        elif c_branch and p_branch and c_branch.lower() == p_branch.lower():
            has_location_evidence = True

        if has_location_evidence:
            diag["reasons"].append("SINGLE_PLACE_EXACT_LOCATION_MATCH")
            return single_p, PlaceMatchClassification.EXACT_BRANCH_MATCH, max(score, 0.90), diag

        # If name similarity is high, both in Manchester, and no branch divergence -> STRONG_BUSINESS_MATCH
        if score >= 0.75 and ("manchester" in p_addr.lower() or c_city.lower() in p_addr.lower()):
            diag["reasons"].append("STRONG_BUSINESS_MATCH_SINGLE_LOCATION")
            return single_p, PlaceMatchClassification.STRONG_BUSINESS_MATCH, score, diag

        # Fallback if confidence is weak
        diag["reasons"].append("INSUFFICIENT_IDENTITY_CONFIDENCE")
        return None, PlaceMatchClassification.IDENTITY_MISMATCH, score, diag


class GosomCoverageEvaluator:
    """
    Evaluator for Phase 7.6 Review-Metric Coverage Recovery & Strict Branch Matching.
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
        self.strict_matcher = StrictPlaceMatcher(self.matcher)
        self.place_enricher = GosomPlaceEnricher(self.matcher)

        # Invariant counters
        self.apify_calls = 0
        self.apify_spend_usd = 0.0
        self.google_places_api_calls = 0
        self.crm_mutations = 0
        self.messages_sent = 0
        self.campaigns_armed = 0
        self.proxies_used = 0
        self.captchas_bypassed = 0

        # Telemetry
        self.candidates_considered = 0
        self.candidates_tested = 0
        self.calls_attempted = 0
        self.calls_completed = 0
        self.cache_hits = 0
        self.cache_misses = 0

        # Outcome counters
        self.review_count_recovered = 0
        self.rating_recovered = 0
        self.timestamp_recovered = 0
        self.freshness_recent = 0
        self.freshness_stale = 0
        self.freshness_unknown = 0

        # Match classification counts
        self.exact_branch_match = 0
        self.strong_business_match = 0
        self.ambiguous_match = 0
        self.branch_mismatch = 0
        self.identity_mismatch = 0

        # Ensure cache dir exists
        os.makedirs(self.config.cache_dir, exist_ok=True)

    def is_candidate_eligible(
        self,
        candidate: Dict[str, Any],
        crm_pool: Optional[Set[str]] = None
    ) -> Tuple[bool, str]:
        """
        Evaluates candidate eligibility under Phase 7.6 criteria:
          Condition A: review_count is missing OR rating is missing
          OR
          Condition B: review_count >= 50 AND rating >= 4.0 AND review_freshness == UNKNOWN
        Plus baseline hygiene (fresh, not CRM duplicate, restaurant category, not excluded).
        """
        c_name = candidate.get("company_name") or candidate.get("business_name") or ""
        if not c_name.strip():
            return False, "INELIGIBLE_MISSING_NAME"

        # Check CRM duplicate
        if crm_pool:
            c_phone = re.sub(r'\D', '', candidate.get("phone") or "")
            c_norm_name = clean_ascii_text(c_name)
            if c_phone and c_phone in crm_pool:
                return False, "INELIGIBLE_CRM_DUPLICATE_PHONE"
            if c_norm_name and c_norm_name in crm_pool:
                return False, "INELIGIBLE_CRM_DUPLICATE_NAME"

        # Check category
        category = (candidate.get("category") or candidate.get("cuisine") or "restaurant").lower()
        non_rest_kws = ["hotel only", "clothing", "retail", "dentist", "car rental"]
        if any(kw in category for kw in non_rest_kws):
            return False, "INELIGIBLE_NON_RESTAURANT"

        # Check closure signals
        for kw in OperationalValidator.CLOSURE_KEYWORDS:
            if kw in c_name.lower():
                return False, f"INELIGIBLE_CLOSURE_KEYWORD_{kw}"

        # Check qualification exclusion
        if candidate.get("qualification_state") == QualificationState.EXCLUDED.value:
            return False, "INELIGIBLE_ALREADY_EXCLUDED"

        rc = candidate.get("review_count")
        rat = candidate.get("rating")
        fresh = candidate.get("review_freshness") or "UNKNOWN"

        # Condition A: review_count missing OR rating missing
        cond_a = (rc is None or rat is None)

        # Condition B: review_count >= 50 AND rating >= 4.0 AND review_freshness == UNKNOWN
        cond_b = False
        if rc is not None and rat is not None:
            try:
                rc_val = int(rc)
                rat_val = float(rat)
                if rc_val >= 50 and rat_val >= 4.0 and fresh == "UNKNOWN":
                    cond_b = True
            except (ValueError, TypeError):
                cond_b = False

        if cond_a:
            return True, "ELIGIBLE_CONDITION_A_MISSING_METRICS"
        elif cond_b:
            return True, "ELIGIBLE_CONDITION_B_UNKNOWN_FRESHNESS"
        else:
            return False, f"INELIGIBLE_NOT_MEETING_A_OR_B (rc={rc}, rat={rat}, fresh={fresh})"

    def evaluate_candidate(
        self,
        candidate: Dict[str, Any],
        scraped_places: List[Dict[str, Any]],
        existing_reviews: Optional[List[ReviewEvidenceItem]] = None,
        as_of: datetime = REFERENCE_DATE
    ) -> Dict[str, Any]:
        """
        Runs the full strict matching, review metric extraction, and reconciliation flow.
        """
        c_name = candidate.get("company_name") or candidate.get("business_name") or ""
        c_city = candidate.get("city", "Manchester")
        c_addr = candidate.get("address", "")

        query = construct_gosom_query(candidate)

        # Step 1: Strict Place Matching
        matched_place, match_class, conf, diag = self.strict_matcher.classify_and_match(
            candidate_meta=candidate,
            scraped_places=scraped_places
        )

        # Track match classification
        if match_class == PlaceMatchClassification.EXACT_BRANCH_MATCH:
            self.exact_branch_match += 1
        elif match_class == PlaceMatchClassification.STRONG_BUSINESS_MATCH:
            self.strong_business_match += 1
        elif match_class == PlaceMatchClassification.AMBIGUOUS_MATCH:
            self.ambiguous_match += 1
        elif match_class == PlaceMatchClassification.BRANCH_MISMATCH:
            self.branch_mismatch += 1
        else:
            self.identity_mismatch += 1

        res: Dict[str, Any] = {
            "candidate_name": c_name,
            "city": c_city,
            "address": c_addr,
            "query": query,
            "match_classification": match_class.value,
            "match_confidence": round(conf, 3),
            "matched_place_title": matched_place.get("title") if matched_place else None,
            "matched_place_address": matched_place.get("address") if matched_place else None,
            "matched_place_id": matched_place.get("place_id") or matched_place.get("data_id") if matched_place else None,
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

        # Step 2: Evidence Gate
        # ONLY EXACT_BRANCH_MATCH or STRONG_BUSINESS_MATCH may contribute review evidence
        if match_class not in [PlaceMatchClassification.EXACT_BRANCH_MATCH, PlaceMatchClassification.STRONG_BUSINESS_MATCH]:
            self.freshness_unknown += 1
            return res

        assert matched_place is not None

        # Step 3: Extract Google review metrics
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

        # Step 4: Extract Review Timestamps
        primary_item, extracted_items, ext_err = self.place_enricher.extract_place_review_evidence(
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

        # Step 5: Multi-Source Reconciliation
        all_reviews: List[ReviewEvidenceItem] = list(existing_reviews or [])
        if primary_item:
            all_reviews.append(primary_item)

        candidate_meta = {
            "company_name": c_name,
            "address": c_addr,
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

        # Step 6: Qualification Evaluation
        # If no material conflict and metrics pass thresholds, re-evaluate operational status
        cand_eval_dict = dict(candidate)
        if res["review_count_recovered"] is not None:
            cand_eval_dict["review_count"] = res["review_count_recovered"]
        if res["rating_recovered"] is not None:
            cand_eval_dict["rating"] = res["rating_recovered"]
        cand_eval_dict["review_freshness"] = res["freshness"]
        cand_eval_dict["latest_review_date"] = res["latest_review_date"] or ""

        # Enforce Rule B Condition 5: Google reviews + Google place = ONE source family.
        # Check if candidate has an independent 2nd operational family (e.g. food hygiene, companies house, verified website)
        # Note: If no 2nd family, operational status remains ACTIVE_LIKELY, and qualification remains MANUAL_REVIEW.
        has_second_family = False
        other_families = cand_eval_dict.get("operational_source_families") or []
        for fam in other_families:
            if fam != SourceFamily.GOOGLE.value and fam != "GOOGLE":
                has_second_family = True
                break

        if reconciled.is_material_conflict or reconciled.is_branch_difference:
            # Conflict routes to MANUAL_REVIEW
            res["qualification_after"] = QualificationState.MANUAL_REVIEW.value
            res["operational_status_after"] = OperationalStatus.ACTIVE_LIKELY.value
        else:
            # Check review threshold: review_count >= 50 and rating >= 4.0
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
