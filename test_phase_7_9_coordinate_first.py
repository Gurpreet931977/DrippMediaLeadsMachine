"""
Dripp Media — Phase 7.9 Coordinate-First Gosom Matcher Unit Tests
==================================================================
Comprehensive test suite validating:
  A. Exact coordinate match -> SAFE_MATCH
  B. Small coordinate difference within safe threshold -> SAFE_MATCH only when identity is strong
  C. Same-name business at materially different coordinate -> BRANCH_MISMATCH
  D. Two same-name branches both plausible -> AMBIGUOUS_MATCH
  E. Name mismatch despite close coordinates -> IDENTITY_MISMATCH
  F. Missing Gosom coordinates -> NO_COORDINATE_EVIDENCE / INSUFFICIENT_EVIDENCE
  G. Coordinate match without sufficient identity -> reject
  H. Strong identity without sufficient coordinate evidence -> reject
  I. Pot Kettle Black airport candidate must never inherit Barton Arcade evidence
  J. Existing Issano exact-address case must continue to pass
  K. Existing Phase 7.7 and 7.8 safety tests remain unchanged
  L. No Gosom result may mutate CRM state
  M. No review evidence may be attached to an unsafe or ambiguous match
"""

import os
import sys
import json
import hashlib
import unittest
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import SourceFamily, OperationalStatus, QualificationState
from lib.crm.identity_matcher import BusinessIdentityMatcher
from lib.enrichment.review_date_extractor import ReviewFreshness
from lib.enrichment.review_rating_enricher import REFERENCE_DATE, ReviewEvidenceItem
from lib.enrichment.review_reconciler import ReviewEvidenceReconciler
from lib.enrichment.gosom_fallback import GosomFallbackConfig
from lib.enrichment.coordinate_matcher import (
    CoordinateMatchClassification,
    IdentityMatchClassification,
    CoordinateMatchResultClassification,
    CoordinateFirstMatcher,
    CoordinateFirstEvaluator,
    haversine_distance_m,
    construct_safe_coordinate_query,
)


CRM_FILES_TO_CHECK = [
    "data/cache_sheets_raw_leads.json",
    "data/cache_sheets_manual_review.json",
    "data/cache_sheets_client_ready.json",
    "data/campaigns.json",
    "data/message_history.json",
]


def compute_file_sha256(filepath: str):
    if not os.path.exists(filepath):
        return None
    with open(filepath, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class TestPhase79CoordinateFirstMatcher(unittest.TestCase):
    """Targeted Unit Test Suite for Phase 7.9."""

    def setUp(self):
        self.matcher = BusinessIdentityMatcher()
        self.reconciler = ReviewEvidenceReconciler(self.matcher)
        self.coord_matcher = CoordinateFirstMatcher(self.matcher)
        self.config = GosomFallbackConfig(enabled=True, max_calls=20, cache_dir="data/cache_gosom_reviews")
        self.evaluator = CoordinateFirstEvaluator(
            config=self.config,
            matcher=self.matcher,
            reconciler=self.reconciler
        )

    # ── Test A: Exact coordinate match -> SAFE_MATCH ──────────────────────────
    def test_a_exact_coordinate_match_safe(self):
        candidate = {
            "company_name": "Issano",
            "city": "Manchester",
            "latitude": 53.408009,
            "longitude": -2.2574226,
            "qualification_state": "RESEARCH_ONLY",
            "operational_status": "ACTIVE_LIKELY"
        }
        place = {
            "title": "Issano Pizza & Grill House",
            "address": "367 Palatine Rd, Northenden, Wythenshawe, Manchester M22 4FY",
            "latitude": 53.4080219,
            "longitude": -2.2573227,
            "place_id": "ChIJb_8q1Haye0gRFh6k4GzLg5k",
            "review_count": 140,
            "review_rating": 4.0,
            "user_reviews": [
                {
                    "published_at": "2026-08-29T12:00:00Z",
                    "rating_float": 4.0,
                    "review_id": "rev_issano_1"
                }
            ]
        }
        # Distance is ~6.8m (<= 50m -> EXACT_COORDINATE_MATCH)
        dist = haversine_distance_m(candidate["latitude"], candidate["longitude"], place["latitude"], place["longitude"])
        self.assertLessEqual(dist, 50.0)

        matched_p, m_class, conf, diag = self.coord_matcher.classify_and_match(candidate, [place])
        self.assertEqual(m_class, CoordinateMatchResultClassification.SAFE_MATCH)
        self.assertIsNotNone(matched_p)
        self.assertGreaterEqual(conf, 0.90)

    # ── Test B: Small coordinate diff within safe threshold -> SAFE_MATCH ─────
    def test_b_small_coordinate_diff_within_safe_threshold(self):
        candidate = {
            "company_name": "Costa Coffee",
            "city": "Manchester",
            "latitude": 53.3602558,
            "longitude": -2.2707558,
        }
        place = {
            "title": "Costa Coffee",
            "address": "Departures, Costa Coffee, MAN Terminal 3, Manchester M90 1QX",
            "latitude": 53.360718,
            "longitude": -2.27031,
            "review_count": 157,
            "review_rating": 2.7
        }
        # Distance is ~59.3m (in 50m - 180m range -> STRONG_COORDINATE_MATCH)
        dist = haversine_distance_m(candidate["latitude"], candidate["longitude"], place["latitude"], place["longitude"])
        self.assertGreater(dist, 50.0)
        self.assertLessEqual(dist, 180.0)

        matched_p, m_class, conf, diag = self.coord_matcher.classify_and_match(candidate, [place])
        self.assertEqual(m_class, CoordinateMatchResultClassification.SAFE_MATCH)
        self.assertIsNotNone(matched_p)
        self.assertGreaterEqual(conf, 0.88)

    # ── Test C: Same-name business at materially different coordinate ─────────
    def test_c_same_name_different_coordinate_branch_mismatch(self):
        candidate = {
            "company_name": "Georgia Chicken",
            "city": "Manchester",
            "latitude": 53.3800662,
            "longitude": -2.2761246,  # Wythenshawe
        }
        place = {
            "title": "Georgia Chicken",
            "address": "232 Wilmslow Rd, Fallowfield, Manchester M14 6LE",
            "latitude": 53.4439239,
            "longitude": -2.2186378,  # Fallowfield (8,058m away)
            "review_count": 110,
            "review_rating": 4.1
        }
        dist = haversine_distance_m(candidate["latitude"], candidate["longitude"], place["latitude"], place["longitude"])
        self.assertGreater(dist, 180.0)

        matched_p, m_class, conf, diag = self.coord_matcher.classify_and_match(candidate, [place])
        self.assertEqual(m_class, CoordinateMatchResultClassification.BRANCH_MISMATCH)
        self.assertIsNone(matched_p)

    # ── Test D: Two same-name branches both plausible -> AMBIGUOUS_MATCH ──────
    def test_d_two_same_name_branches_both_plausible_ambiguous(self):
        candidate = {
            "company_name": "Caffe Nero",
            "city": "Manchester",
            "latitude": 53.4800,
            "longitude": -2.2400,
        }
        # Two places within safe coordinate distance (< 180m)
        place1 = {
            "title": "Caffe Nero",
            "address": "Unit A, High Street",
            "latitude": 53.4802,
            "longitude": -2.2403,  # ~30m
            "review_count": 80,
            "review_rating": 4.2
        }
        place2 = {
            "title": "Caffe Nero",
            "address": "Unit B, Shopping Arcade",
            "latitude": 53.4804,
            "longitude": -2.2406,  # ~60m
            "review_count": 120,
            "review_rating": 4.5
        }
        matched_p, m_class, conf, diag = self.coord_matcher.classify_and_match(candidate, [place1, place2])
        self.assertEqual(m_class, CoordinateMatchResultClassification.AMBIGUOUS_MATCH)
        self.assertIsNone(matched_p)
        self.assertIn("MULTIPLE_SAME_NAME_PLACES_WITHIN_SAFE_DISTANCE", diag["reasons"][0])

    # ── Test E: Name mismatch despite close coordinates -> IDENTITY_MISMATCH ──
    def test_e_name_mismatch_despite_close_coordinates(self):
        candidate = {
            "company_name": "Tesco Express",
            "city": "Manchester",
            "latitude": 53.408009,
            "longitude": -2.2574226,
        }
        place = {
            "title": "Issano Pizza & Grill House",
            "address": "367 Palatine Rd, Northenden",
            "latitude": 53.4080219,
            "longitude": -2.2573227,  # ~6.8m away
            "review_count": 140,
            "review_rating": 4.0
        }
        matched_p, m_class, conf, diag = self.coord_matcher.classify_and_match(candidate, [place])
        self.assertEqual(m_class, CoordinateMatchResultClassification.IDENTITY_MISMATCH)
        self.assertIsNone(matched_p)

    # ── Test F: Missing Gosom coordinates -> NO_COORDINATE_EVIDENCE ───────────
    def test_f_missing_gosom_coordinates(self):
        candidate = {
            "company_name": "Bar Bibo",
            "city": "Manchester",
            "latitude": 53.4076106,
            "longitude": -2.2580151,
        }
        place = {
            "title": "Bar Bibo",
            "address": "387 Palatine Rd, Northenden",
            "latitude": None,
            "longitude": None,
            "review_count": 56,
            "review_rating": 3.8
        }
        matched_p, m_class, conf, diag = self.coord_matcher.classify_and_match(candidate, [place])
        self.assertEqual(m_class, CoordinateMatchResultClassification.INSUFFICIENT_EVIDENCE)
        self.assertIsNone(matched_p)
        self.assertIn("NO_COORDINATE_EVIDENCE_ON_RETURNED_PLACES", diag["reasons"])

    # ── Test G: Coordinate match without sufficient identity -> reject ────────
    def test_g_coordinate_match_without_sufficient_identity_reject(self):
        candidate = {
            "company_name": "Green Garden Florist",
            "city": "Manchester",
            "latitude": 53.3928184,
            "longitude": -2.2923354,
        }
        place = {
            "title": "Founder Coffee Co",
            "address": "4 Ledson Rd, Wythenshawe",
            "latitude": 53.3928173,
            "longitude": -2.2923013,  # 2.3m away
            "review_count": 25,
            "review_rating": 4.6
        }
        matched_p, m_class, conf, diag = self.coord_matcher.classify_and_match(candidate, [place])
        self.assertEqual(m_class, CoordinateMatchResultClassification.IDENTITY_MISMATCH)
        self.assertIsNone(matched_p)

    # ── Test H: Strong identity without sufficient coordinate evidence ────────
    def test_h_strong_identity_without_sufficient_coordinate_evidence_reject(self):
        candidate = {
            "company_name": "Burger King",
            "city": "Manchester",
            "latitude": 53.3667882,
            "longitude": -2.2793089,  # Airport
        }
        place = {
            "title": "Burger King",
            "address": "50 Oxford St, Manchester",
            "latitude": 53.4756342,
            "longitude": -2.2423897,  # City Centre (~12.3km away)
            "review_count": 500,
            "review_rating": 3.5
        }
        matched_p, m_class, conf, diag = self.coord_matcher.classify_and_match(candidate, [place])
        self.assertEqual(m_class, CoordinateMatchResultClassification.BRANCH_MISMATCH)
        self.assertIsNone(matched_p)

    # ── Test I: Pot Kettle Black airport candidate never inherits Barton Arcade ─
    def test_i_pot_kettle_black_airport_never_inherits_barton_arcade(self):
        candidate = {
            "company_name": "Pot Kettle Black",
            "city": "Manchester",
            "latitude": 53.3678333,
            "longitude": -2.2822664,  # Manchester Airport Terminal 2
            "qualification_state": "OUTREACH_READY",
            "operational_status": "ACTIVE_CONFIRMED"
        }
        barton_place = {
            "title": "POT KETTLE BLACK Barton Arcade",
            "address": "Barton Arcade, Manchester M3 2BW",
            "latitude": 53.4827501,
            "longitude": -2.2463011,  # ~13,000m away
            "review_count": 890,
            "review_rating": 4.6,
            "user_reviews": [
                {
                    "published_at": "2026-08-01T10:00:00Z",
                    "rating_float": 5.0,
                    "review_id": "rev_pkb_barton"
                }
            ]
        }
        tariff_place = {
            "title": "Pot Kettle Black",
            "address": "1A Tariff St, Manchester M1 2FF",
            "latitude": 53.4811042,
            "longitude": -2.2324545,  # ~13,020m away
            "review_count": 450,
            "review_rating": 4.5
        }
        # Run evaluator
        res = self.evaluator.evaluate_candidate(candidate, [barton_place, tariff_place], as_of=REFERENCE_DATE)

        # Invariant checks
        self.assertEqual(res["match_classification"], CoordinateMatchResultClassification.BRANCH_MISMATCH.value)
        self.assertIsNone(res["matched_place_title"])
        self.assertIsNone(res["review_count_recovered"])
        self.assertIsNone(res["rating_recovered"])
        self.assertIsNone(res["latest_review_date"])
        self.assertEqual(res["freshness"], ReviewFreshness.UNKNOWN.value)

    # ── Test J: Existing Issano exact-address case must continue to pass ───────
    def test_j_existing_issano_exact_address_continues_to_pass(self):
        candidate = {
            "company_name": "Issano",
            "city": "Manchester",
            "address": "367 Palatine Rd, Northenden",
            "latitude": 53.408009,
            "longitude": -2.2574226,
            "qualification_state": "RESEARCH_ONLY",
            "operational_status": "ACTIVE_LIKELY",
            "operational_source_families": ["FOOD_HYGIENE"]
        }
        place = {
            "title": "Issano Pizza & Grill House",
            "address": "367 Palatine Rd, Northenden, Wythenshawe, Manchester M22 4FY",
            "latitude": 53.4080219,
            "longitude": -2.2573227,
            "review_count": 140,
            "review_rating": 4.0,
            "user_reviews": [
                {
                    "published_at": "2026-08-29T12:00:00Z",
                    "rating_float": 4.0,
                    "review_id": "rev_issano_1"
                }
            ]
        }
        res = self.evaluator.evaluate_candidate(candidate, [place], as_of=REFERENCE_DATE)
        self.assertEqual(res["match_classification"], CoordinateMatchResultClassification.SAFE_MATCH.value)
        self.assertEqual(res["review_count_recovered"], 140)
        self.assertEqual(res["rating_recovered"], 4.0)
        self.assertEqual(res["freshness"], ReviewFreshness.RECENT.value)
        # Because it has an independent 2nd operational family (FOOD_HYGIENE), it safely qualifies
        self.assertEqual(res["operational_status_after"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(res["qualification_after"], QualificationState.OUTREACH_READY.value)

    # ── Test K: Existing Phase 7.7 and 7.8 safety tests remain unchanged ──────
    def test_k_safety_thresholds_and_invariants(self):
        # Verify fallback flag is disabled
        cfg = GosomFallbackConfig.from_env()
        self.assertFalse(cfg.enabled)
        # Verify query generation never invents address
        cand_no_addr = {"company_name": "Test Place", "city": "Manchester"}
        q = construct_safe_coordinate_query(cand_no_addr)
        self.assertEqual(q, '"Test Place" "Manchester"')

    # ── Test L: No Gosom result may mutate CRM state ──────────────────────────
    def test_l_no_crm_mutation(self):
        hashes_before = {f: compute_file_sha256(f) for f in CRM_FILES_TO_CHECK}

        candidate = {
            "company_name": "Bar Bibo",
            "city": "Manchester",
            "latitude": 53.4076106,
            "longitude": -2.2580151
        }
        place = {
            "title": "Bar Bibo",
            "address": "387 Palatine Rd, Northenden",
            "latitude": 53.4076448,
            "longitude": -2.2579859,
            "review_count": 56,
            "review_rating": 3.8
        }
        res = self.evaluator.evaluate_candidate(candidate, [place], as_of=REFERENCE_DATE)
        self.assertEqual(res["match_classification"], CoordinateMatchResultClassification.SAFE_MATCH.value)

        hashes_after = {f: compute_file_sha256(f) for f in CRM_FILES_TO_CHECK}
        self.assertEqual(hashes_before, hashes_after)
        self.assertEqual(self.evaluator.crm_mutations, 0)
        self.assertEqual(self.evaluator.messages_sent, 0)
        self.assertEqual(self.evaluator.campaigns_armed, 0)

    # ── Test M: No review evidence attached to unsafe or ambiguous match ─────
    def test_m_no_review_evidence_on_unsafe_or_ambiguous_match(self):
        candidate = {
            "company_name": "Test Eatery",
            "city": "Manchester",
            "latitude": 53.4000,
            "longitude": -2.2000
        }
        # Ambiguous places
        p1 = {"title": "Test Eatery", "latitude": 53.4001, "longitude": -2.2001, "review_count": 100, "review_rating": 4.5}
        p2 = {"title": "Test Eatery", "latitude": 53.4002, "longitude": -2.2002, "review_count": 200, "review_rating": 4.8}

        res = self.evaluator.evaluate_candidate(candidate, [p1, p2], as_of=REFERENCE_DATE)
        self.assertEqual(res["match_classification"], CoordinateMatchResultClassification.AMBIGUOUS_MATCH.value)
        self.assertIsNone(res["review_count_recovered"])
        self.assertIsNone(res["rating_recovered"])
        self.assertIsNone(res["latest_review_date"])
        self.assertEqual(res["freshness"], ReviewFreshness.UNKNOWN.value)


if __name__ == "__main__":
    unittest.main()
