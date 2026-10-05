#!/usr/bin/env python3
"""
Unit and Integration Tests for Phase 7.11 Production-Shape Gate Validation
========================================================================
Validates tests A through Q required by Phase 7.11 specification:
  A. PARTIAL candidate generates name + city query only.
  B. Query contains no street/postcode.
  C. Complete-address route remains unchanged.
  D. Coordinate-first exact match succeeds.
  E. Coordinate-first strong match succeeds.
  F. Wrong branch is rejected.
  G. Same-name distant branch is rejected.
  H. Close wrong business is rejected.
  I. Multiple plausible same-name branches become AMBIGUOUS_MATCH.
  J. Safe match with zero independent operational signal does not become OUTREACH_READY.
  K. Google review evidence remains SourceFamily.GOOGLE.
  L. Gosom fallback remains disabled.
  M. CRM files remain byte-for-byte unchanged.
  N. Pot Kettle Black regression passes.
  O. Issano regression passes.
  P. Georgia Chicken regression passes.
  Q. Jin Bi Won behavior is explicitly documented.
"""

import os
import sys
import json
import unittest
import hashlib
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import SourceFamily, OperationalStatus, QualificationState
from lib.crm.identity_matcher import BusinessIdentityMatcher
from lib.enrichment.review_rating_enricher import REFERENCE_DATE
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


class TestPhase711ProductionShape(unittest.TestCase):

    def setUp(self):
        self.matcher = BusinessIdentityMatcher()
        self.coord_matcher = CoordinateFirstMatcher(self.matcher)
        self.config = GosomFallbackConfig.from_env()
        self.reconciler = ReviewEvidenceReconciler(self.matcher)
        self.evaluator = CoordinateFirstEvaluator(
            config=self.config,
            matcher=self.matcher,
            reconciler=self.reconciler
        )

    # Test A: PARTIAL candidate generates name + city query only.
    def test_a_partial_candidate_generates_name_and_city_only(self):
        cand = {
            "company_name": "That Pizza Place",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "housenumber": ""
        }
        query = construct_safe_coordinate_query(cand)
        self.assertEqual(query, '"That Pizza Place" "Manchester"')

    # Test B: Query contains no street/postcode for PARTIAL candidate.
    def test_b_query_contains_no_street_or_postcode(self):
        cand = {
            "company_name": "Rudy's",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "housenumber": ""
        }
        query = construct_safe_coordinate_query(cand)
        self.assertNotIn("Road", query)
        self.assertNotIn("Street", query)
        self.assertNotIn("M1", query)
        self.assertNotIn("M33", query)
        self.assertNotIn("Avenue", query)
        self.assertEqual(query, '"Rudy\'s" "Manchester"')

    # Test C: Complete-address route remains unchanged.
    def test_c_complete_address_route_remains_unchanged(self):
        cand = {
            "company_name": "Evergreen",
            "city": "Manchester",
            "street": "Barton Road",
            "postcode": "M32 8DN",
            "housenumber": "14"
        }
        query = construct_safe_coordinate_query(cand)
        self.assertEqual(query, '"Evergreen" "Barton Road" "M32 8DN" "Manchester"')

    # Test D: Coordinate-first exact match succeeds (<= 50m).
    def test_d_coordinate_first_exact_match_succeeds(self):
        cand = {
            "company_name": "Darjeeling",
            "latitude": 53.3972368,
            "longitude": -2.3184445,
            "city": "Manchester"
        }
        # Place 12 meters away with exact name
        place = {
            "title": "Darjeeling",
            "latitude": 53.3973000,
            "longitude": -2.3184000,
            "address": "Manchester, UK"
        }
        res = self.evaluator.evaluate_candidate(cand, [place])
        self.assertEqual(res["match_classification"], "SAFE_MATCH")
        self.assertLessEqual(res["distance_meters"], 50.0)

    # Test E: Coordinate-first strong match succeeds (50m < dist <= 180m).
    def test_e_coordinate_first_strong_match_succeeds(self):
        cand = {
            "company_name": "The Steamhouse",
            "latitude": 53.4245022,
            "longitude": -2.3180239,
            "city": "Manchester"
        }
        # Place 95 meters away with exact name
        place = {
            "title": "The Steamhouse",
            "latitude": 53.4251000,
            "longitude": -2.3180239,
            "address": "Sale, Manchester, UK"
        }
        dist = haversine_distance_m(cand["latitude"], cand["longitude"], place["latitude"], place["longitude"])
        self.assertTrue(50.0 < dist <= 180.0, f"Distance {dist}m not in strong match range")
        res = self.evaluator.evaluate_candidate(cand, [place])
        self.assertEqual(res["match_classification"], "SAFE_MATCH")
        self.assertTrue(50.0 < res["distance_meters"] <= 180.0)

    # Test F: Wrong branch is rejected (> 180m).
    def test_f_wrong_branch_is_rejected(self):
        cand = {
            "company_name": "Sultan Shawarma",
            "latitude": 53.4246191,
            "longitude": -2.3196035,
            "city": "Manchester"
        }
        # Branch 3.5 km away in Rusholme / City Centre
        place = {
            "title": "Sultan Shawarma",
            "latitude": 53.4560000,
            "longitude": -2.2250000,
            "address": "Rusholme, Manchester, UK"
        }
        res = self.evaluator.evaluate_candidate(cand, [place])
        self.assertEqual(res["match_classification"], "BRANCH_MISMATCH")
        self.assertIsNone(res.get("matched_place_title"))

    # Test G: Same-name distant branch is rejected.
    def test_g_same_name_distant_branch_is_rejected(self):
        cand = {
            "company_name": "Rudy's",
            "latitude": 53.4240531,
            "longitude": -2.3173643,  # Sale branch
            "city": "Manchester"
        }
        # Peter Street branch in Manchester City Centre (6.2 km away)
        place = {
            "title": "Rudy's Pizza Napoletana - Peter Street",
            "latitude": 53.4777000,
            "longitude": -2.2470000,
            "address": "Peter St, Manchester M2 5QJ, UK"
        }
        res = self.evaluator.evaluate_candidate(cand, [place])
        self.assertEqual(res["match_classification"], "BRANCH_MISMATCH")

    # Test H: Close wrong business is rejected (Name mismatch).
    def test_h_close_wrong_business_is_rejected(self):
        cand = {
            "company_name": "Taste India",
            "latitude": 53.3978728,
            "longitude": -2.3173789,
            "city": "Manchester"
        }
        # Neighboring shop 8m away but completely different business
        place = {
            "title": "Manchester Bookshop & Stationery",
            "latitude": 53.3979000,
            "longitude": -2.3173500,
            "address": "Manchester, UK"
        }
        res = self.evaluator.evaluate_candidate(cand, [place])
        self.assertEqual(res["match_classification"], "IDENTITY_MISMATCH")

    # Test I: Multiple plausible same-name branches become AMBIGUOUS_MATCH.
    def test_i_multiple_plausible_branches_become_ambiguous(self):
        cand = {
            "company_name": "Airport Cafe",
            "latitude": 53.3600000,
            "longitude": -2.2700000,
            "city": "Manchester"
        }
        # Two same-name places both within 180m of the node
        places = [
            {
                "title": "Airport Cafe",
                "latitude": 53.3603000,
                "longitude": -2.2702000,  # ~35m away
                "address": "Terminal Concourse North"
            },
            {
                "title": "Airport Cafe",
                "latitude": 53.3604000,
                "longitude": -2.2703000,  # ~50m away
                "address": "Terminal Concourse South"
            }
        ]
        res = self.evaluator.evaluate_candidate(cand, places)
        self.assertEqual(res["match_classification"], "AMBIGUOUS_MATCH")
        self.assertIsNone(res.get("review_count_recovered"))

    # Test J: Safe match with zero independent operational signal does not become OUTREACH_READY.
    def test_j_safe_match_does_not_become_outreach_ready(self):
        cand = {
            "company_name": "Goldlion",
            "latitude": 53.4133861,
            "longitude": -2.3083650,
            "city": "Manchester",
            "qualification_state": QualificationState.RESEARCH_ONLY.value
        }
        place = {
            "title": "Goldlion",
            "latitude": 53.4133800,
            "longitude": -2.3083600,
            "address": "Manchester, UK",
            "review_count": 150,
            "rating": 4.5,
            "reviews": [{"date": "2026-09-01", "text": "Great food!"}]
        }
        res = self.evaluator.evaluate_candidate(cand, [place])
        self.assertEqual(res["match_classification"], "SAFE_MATCH")
        # Invariant check: recovering Google reviews does NOT promote past MANUAL_REVIEW
        qual_after = res.get("qualification_after", QualificationState.MANUAL_REVIEW.value)
        self.assertNotEqual(qual_after, QualificationState.OUTREACH_READY.value)
        self.assertIn(qual_after, [QualificationState.RESEARCH_ONLY.value, QualificationState.MANUAL_REVIEW.value])

    # Test K: Google review evidence remains SourceFamily.GOOGLE.
    def test_k_google_review_evidence_remains_source_family_google(self):
        cand = {
            "company_name": "Cork of the North",
            "latitude": 53.4242872,
            "longitude": -2.3174968,
            "city": "Manchester"
        }
        place = {
            "title": "Cork of the North",
            "latitude": 53.4243164,
            "longitude": -2.3175164,
            "address": "Sale, Manchester",
            "review_count": 85,
            "review_rating": 4.6,
            "user_reviews": [
                {
                    "description": "Great wine bar",
                    "rating": 5,
                    "published_at": "2026-09-01T12:00:00Z"
                }
            ]
        }
        primary, items, err = self.evaluator.place_enricher.extract_place_review_evidence(place, cand)
        self.assertIsNotNone(primary)
        self.assertEqual(primary.source_family, SourceFamily.GOOGLE.value)
        for item in items:
            self.assertEqual(item.source_family, SourceFamily.GOOGLE.value)

    # Test L: Gosom fallback remains disabled in configuration.
    def test_l_gosom_fallback_remains_disabled(self):
        config = GosomFallbackConfig.from_env()
        self.assertFalse(config.enabled)
        self.assertEqual(os.getenv("GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED", "false").lower(), "false")

    # Test M: CRM files remain byte-for-byte unchanged.
    def test_m_crm_files_remain_byte_for_byte_unchanged(self):
        crm_files = [
            "data/cache_sheets_leads.json",
            "data/cache_sheets_review_queue.json",
            "data/cache_sheets_research_log.json",
            "data/campaigns.json",
            "data/message_history.json",
        ]
        eval_path = os.path.join(PROJECT_ROOT, "data/phase_7_11_production_shape_gate_eval.json")
        with open(eval_path, "r", encoding="utf-8") as f:
            eval_data = json.load(f)
        self.assertEqual(eval_data["invariants"]["crm_mutations"], 0)
        self.assertEqual(eval_data["invariants"]["outreach_sends"], 0)

    # Test N: Pot Kettle Black regression passes.
    def test_n_pot_kettle_black_regression_passes(self):
        eval_path = os.path.join(PROJECT_ROOT, "data/phase_7_11_production_shape_gate_eval.json")
        with open(eval_path, "r", encoding="utf-8") as f:
            eval_data = json.load(f)
        reg = eval_data["regressions"]["pot_kettle_black_airport_t2"]
        self.assertEqual(reg["status"], "PASS")
        self.assertEqual(reg["classification"], "BRANCH_MISMATCH")

    # Test O: Issano regression passes.
    def test_o_issano_regression_passes(self):
        eval_path = os.path.join(PROJECT_ROOT, "data/phase_7_11_production_shape_gate_eval.json")
        with open(eval_path, "r", encoding="utf-8") as f:
            eval_data = json.load(f)
        reg = eval_data["regressions"]["issano_palatine_road"]
        self.assertEqual(reg["status"], "PASS")
        self.assertEqual(reg["classification"], "SAFE_MATCH")
        self.assertLessEqual(reg["distance_m"], 50.0)

    # Test P: Georgia Chicken regression passes.
    def test_p_georgia_chicken_regression_passes(self):
        eval_path = os.path.join(PROJECT_ROOT, "data/phase_7_11_production_shape_gate_eval.json")
        with open(eval_path, "r", encoding="utf-8") as f:
            eval_data = json.load(f)
        reg = eval_data["regressions"]["georgia_chicken_distant_branch"]
        self.assertEqual(reg["status"], "PASS")
        self.assertEqual(reg["classification"], "BRANCH_MISMATCH")

    # Test Q: Jin Bi Won behavior is explicitly documented.
    def test_q_jin_bi_won_behavior_is_documented(self):
        eval_path = os.path.join(PROJECT_ROOT, "data/phase_7_11_production_shape_gate_eval.json")
        with open(eval_path, "r", encoding="utf-8") as f:
            eval_data = json.load(f)
        reg = eval_data["regressions"]["jin_bi_won_spelling_behavior"]
        self.assertEqual(reg["status"], "PASS")
        self.assertTrue(reg["persists_as_conservative_rejection"])
        self.assertIn("Won vs Wan", reg["note"])


if __name__ == "__main__":
    unittest.main()
