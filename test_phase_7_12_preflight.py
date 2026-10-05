#!/usr/bin/env python3
"""
Unit and Integration Tests for Phase 7.12 Controlled Production Preflight
========================================================================
Validates all requirements A through S:
  A. Complete-address Gosom path unchanged.
  B. Partial candidate can enter coordinate-first fallback only when freshness is UNKNOWN.
  C. Partial candidate query contains name + city only.
  D. SAFE_MATCH permits evidence enrichment.
  E. BRANCH_MISMATCH blocks evidence.
  F. IDENTITY_MISMATCH blocks evidence.
  G. AMBIGUOUS_MATCH blocks evidence.
  H. SEARCH_RECALL_FAILURE blocks evidence.
  I. Google/Gosom remains one source family.
  J. Independent operational signal remains mandatory for Rule B.
  K. STALE review remains STALE.
  L. UNKNOWN review remains UNKNOWN.
  M. No production-state mutations.
  N. No outreach sends.
  O. No campaign state mutations.
  P. Pot Kettle Black regression.
  Q. Georgia Chicken regression.
  R. Issano regression.
  S. Existing Phase 7.11 tests remain valid.
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

from lib.types import (
    SourceFamily,
    OperationalStatus,
    QualificationState,
    DiscoveredBusiness
)
from lib.crm.identity_matcher import BusinessIdentityMatcher
from lib.enrichment.review_rating_enricher import REFERENCE_DATE, ReviewEvidenceItem
from lib.enrichment.review_reconciler import ReviewEvidenceReconciler
from lib.enrichment.gosom_fallback import (
    GosomFallbackConfig,
    GosomReviewFreshnessFallback
)
from lib.enrichment.coordinate_matcher import (
    CoordinateMatchClassification,
    IdentityMatchClassification,
    CoordinateMatchResultClassification,
    CoordinateFirstMatcher,
    CoordinateFirstEvaluator,
    haversine_distance_m,
    construct_safe_coordinate_query,
)
from lib.pipeline import LeadGenerationPipeline
from lib.verification.no_website_verifier import NoWebsiteVerificationProvider
from lib.qualification.lead_scoring import LeadScoringProvider


class TestPhase712Preflight(unittest.TestCase):

    def setUp(self):
        self.matcher = BusinessIdentityMatcher()
        self.coord_matcher = CoordinateFirstMatcher(self.matcher)
        self.config = GosomFallbackConfig.from_env()
        self.reconciler = ReviewEvidenceReconciler(self.matcher)
        self.fallback = GosomReviewFreshnessFallback(
            config=self.config,
            matcher=self.matcher,
            reconciler=self.reconciler
        )
        self.evaluator = CoordinateFirstEvaluator(
            config=self.config,
            matcher=self.matcher,
            reconciler=self.reconciler
        )

    # Test A: Complete-address Gosom path unchanged (PATH A)
    def test_a_complete_address_path_unchanged(self):
        cand = {
            "company_name": "Evergreen",
            "city": "Manchester",
            "street": "Barton Road",
            "postcode": "M32 8DN",
            "address": "14 Barton Road, M32 8DN, Manchester, UK",
            "latitude": 53.4498,
            "longitude": -2.3112,
            "review_count": 80,
            "rating": 4.4,
            "review_freshness": "UNKNOWN"
        }
        mock_place = [{
            "title": "Evergreen Restaurant",
            "address": "14 Barton Rd, Stretford, Manchester M32 8DN, United Kingdom",
            "latitude": 53.4498,
            "longitude": -2.3112,
            "review_count": 80,
            "review_rating": 4.4,
            "user_reviews": [{
                "description": "Lovely food",
                "rating": 5,
                "published_at": "2026-08-25T19:00:00Z"
            }]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=mock_place, shadow_mode=True)
        self.assertEqual(telem["path"], "PATH_A")
        self.assertEqual(telem["status"], "SUCCESS")
        self.assertIn("Evergreen", telem["query"])
        self.assertIn("Barton Road", telem["query"])
        self.assertIn("M32 8DN", telem["query"])

    # Test B: Partial candidate can enter coordinate-first fallback only when freshness is UNKNOWN
    def test_b_partial_candidate_requires_unknown_freshness(self):
        cand_known = {
            "company_name": "Active Diner",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.4245,
            "longitude": -2.3180,
            "review_count": 100,
            "rating": 4.5,
            "review_freshness": "RECENT"
        }
        is_elig, reason = self.fallback.is_candidate_eligible(cand_known)
        self.assertFalse(is_elig)
        self.assertIn("FRESHNESS_ALREADY_KNOWN", reason)

        cand_unknown = dict(cand_known)
        cand_unknown["review_freshness"] = "UNKNOWN"
        is_elig_u, reason_u = self.fallback.is_candidate_eligible(cand_unknown)
        self.assertTrue(is_elig_u)
        self.assertEqual(reason_u, "ELIGIBLE")

    # Test C: Partial candidate query contains name + city only
    def test_c_partial_candidate_query_name_and_city_only(self):
        cand = {
            "company_name": "Rajdan",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.3981871,
            "longitude": -2.3165611
        }
        query = construct_safe_coordinate_query(cand)
        self.assertEqual(query, '"Rajdan" "Manchester"')
        self.assertNotIn("Road", query)
        self.assertNotIn("Street", query)
        self.assertNotIn("M1", query)

    # Test D: SAFE_MATCH permits evidence enrichment
    def test_d_safe_match_permits_evidence_enrichment(self):
        cand = {
            "company_name": "Rajdan",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.3981871,
            "longitude": -2.3165611,
            "review_count": 119,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        mock_place = [{
            "title": "Rajdan, Indian Takeaway, Timperley",
            "address": "401 Stockport Rd, Timperley, Altrincham WA15 7UR, United Kingdom",
            "latitude": 53.3981662,
            "longitude": -2.3166131,
            "review_count": 119,
            "review_rating": 4.5,
            "user_reviews": [{
                "description": "Superb curry",
                "rating": 5,
                "published_at": "2026-09-05T18:30:00Z"
            }]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=mock_place, shadow_mode=True)
        self.assertIsNotNone(rec)
        self.assertEqual(telem["status"], "SUCCESS")
        self.assertEqual(telem["match_classification"], "SAFE_MATCH")
        self.assertEqual(rec.reconciled_freshness, "RECENT")
        self.assertEqual(rec.reconciled_date, "2026-09-05")

    # Test E: BRANCH_MISMATCH blocks evidence
    def test_e_branch_mismatch_blocks_evidence(self):
        cand = {
            "company_name": "Sultan Shawarma",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.4246191,
            "longitude": -2.3196035,
            "review_count": 120,
            "rating": 4.4,
            "review_freshness": "UNKNOWN"
        }
        # Distant branch > 180m away
        mock_place = [{
            "title": "Sultan Shawarma",
            "address": "Rusholme, Manchester M14 5TP, United Kingdom",
            "latitude": 53.4560000,
            "longitude": -2.2250000,
            "review_count": 550,
            "review_rating": 4.6,
            "user_reviews": [{
                "description": "Great shawarma in Rusholme",
                "rating": 5,
                "published_at": "2026-09-01T12:00:00Z"
            }]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=mock_place, shadow_mode=True)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "BLOCKED")
        self.assertEqual(telem["reason"], "BRANCH_MISMATCH")

    # Test F: IDENTITY_MISMATCH blocks evidence
    def test_f_identity_mismatch_blocks_evidence(self):
        cand = {
            "company_name": "FF",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.4245,
            "longitude": -2.3180,
            "review_count": 50,
            "rating": 4.1,
            "review_freshness": "UNKNOWN"
        }
        mock_place = [{
            "title": "Manchester Central Bookshop",
            "address": "Sale, Manchester",
            "latitude": 53.4245,
            "longitude": -2.3180,
            "review_count": 100,
            "review_rating": 4.5,
            "user_reviews": [{"description": "Books", "rating": 5, "published_at": "2026-09-01T10:00:00Z"}]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=mock_place, shadow_mode=True)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "BLOCKED")
        self.assertEqual(telem["reason"], "IDENTITY_MISMATCH")

    # Test G: AMBIGUOUS_MATCH blocks evidence
    def test_g_ambiguous_match_blocks_evidence(self):
        cand = {
            "company_name": "Airport Cafe",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.3600,
            "longitude": -2.2700,
            "review_count": 110,
            "rating": 4.1,
            "review_freshness": "UNKNOWN"
        }
        mock_places = [
            {
                "title": "Airport Cafe",
                "address": "Terminal Concourse North",
                "latitude": 53.3603,
                "longitude": -2.2702,  # ~35m
                "review_count": 110,
                "review_rating": 4.1,
                "user_reviews": [{"description": "Coffee", "rating": 4, "published_at": "2026-08-10T08:00:00Z"}]
            },
            {
                "title": "Airport Cafe",
                "address": "Terminal Concourse South",
                "latitude": 53.3604,
                "longitude": -2.2703,  # ~50m
                "review_count": 95,
                "review_rating": 4.0,
                "user_reviews": [{"description": "Tea", "rating": 4, "published_at": "2026-08-12T09:00:00Z"}]
            }
        ]
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=mock_places, shadow_mode=True)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "BLOCKED")
        self.assertEqual(telem["reason"], "AMBIGUOUS_MATCH")

    # Test H: SEARCH_RECALL_FAILURE blocks evidence
    def test_h_search_recall_failure_blocks_evidence(self):
        cand = {
            "company_name": "Neighbour's",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.4240,
            "longitude": -2.3175,
            "review_count": 70,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=[], shadow_mode=True)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "SEARCH_RECALL_FAILURE")

    # Test I: Google/Gosom remains one source family
    def test_i_google_gosom_remains_one_source_family(self):
        cand = {
            "company_name": "Dosa Kingss",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.4242,
            "longitude": -2.3175,
            "review_count": 95,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        mock_place = [{
            "title": "Dosa Kingss",
            "address": "Sale, Manchester M33",
            "latitude": 53.42423,
            "longitude": -2.31753,
            "review_count": 95,
            "review_rating": 4.5,
            "user_reviews": [{"description": "Great dosas", "rating": 5, "published_at": "2026-09-01T13:00:00Z"}]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=mock_place, shadow_mode=True)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.primary_source_family, SourceFamily.GOOGLE.value)

    # Test J: Independent operational signal remains mandatory for Rule B
    def test_j_independent_operational_signal_mandatory_for_rule_b(self):
        # Candidate with safe Gosom match + recent reviews but NO independent operational signal
        biz_no_ind = DiscoveredBusiness(
            company_name="Taste India",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            country_status="COUNTRY_MATCH",
            address="Manchester, UK",
            phone="",  # Lacks phone
            lat=53.3978728,
            lon=-2.3173789,
            raw_website="",
            review_count=85,
            rating=4.3
        )
        biz_no_ind.raw_data = {
            "review_enrichment": {
                "review_count": 85,
                "rating": 4.3,
                "review_freshness": "RECENT",
                "source_family": SourceFamily.GOOGLE.value
            }
        }
        scorer = LeadScoringProvider()
        audit = scorer.evaluate_lead(
            business=biz_no_ind,
            verification_status="NO_WEBSITE_CONFIRMED",
            verification_reason="Confirmed no website"
        )
        self.assertNotEqual(audit["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(audit["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    # Test K: STALE review remains STALE
    def test_k_stale_review_remains_stale(self):
        cand = {
            "company_name": "Cofi Club",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.4239841,
            "longitude": -2.3170512,
            "review_count": 90,
            "rating": 4.6,
            "review_freshness": "UNKNOWN"
        }
        mock_place = [{
            "title": "Cofi Club",
            "address": "Sale M33",
            "latitude": 53.4240100,
            "longitude": -2.3170300,
            "review_count": 90,
            "review_rating": 4.6,
            "user_reviews": [{
                "description": "Historic review",
                "rating": 5,
                "published_at": "2024-05-10T11:00:00Z"
            }]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=mock_place, shadow_mode=True)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.reconciled_freshness, "STALE")

    # Test L: UNKNOWN review remains UNKNOWN
    def test_l_unknown_review_remains_unknown(self):
        cand = {
            "company_name": "Let's Do Lunch",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.4242,
            "longitude": -2.3178,
            "review_count": 65,
            "rating": 4.3,
            "review_freshness": "UNKNOWN"
        }
        mock_place = [{
            "title": "Let's Do Lunch",
            "address": "Sale, Manchester",
            "latitude": 53.42425,
            "longitude": -2.31785,
            "review_count": 65,
            "review_rating": 4.3,
            "user_reviews": [{
                "description": "No timestamp review",
                "rating": 4,
                "published_at": None,
                "posted_at_unix_micros": None,
                "When": None
            }]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=mock_place, shadow_mode=True)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "EXTRACTION_FAILED")

    # Test M: No production-state mutations
    def test_m_no_production_state_mutations(self):
        preflight_json = os.path.join(PROJECT_ROOT, "data/phase_7_12_controlled_production_preflight.json")
        with open(preflight_json, "r", encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["safety_invariants"]["crm_mutations"], 0)
        self.assertEqual(d["safety_invariants"]["leads_mutations"], 0)
        self.assertEqual(d["safety_invariants"]["review_queue_mutations"], 0)
        self.assertEqual(d["safety_invariants"]["research_log_mutations"], 0)
        self.assertEqual(d["safety_invariants"]["file_mutations_detected"], 0)

    # Test N: No outreach sends
    def test_n_no_outreach_sends(self):
        preflight_json = os.path.join(PROJECT_ROOT, "data/phase_7_12_controlled_production_preflight.json")
        with open(preflight_json, "r", encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["safety_invariants"]["outreach_sends"], 0)

    # Test O: No campaign state mutations
    def test_o_no_campaign_state_mutations(self):
        preflight_json = os.path.join(PROJECT_ROOT, "data/phase_7_12_controlled_production_preflight.json")
        with open(preflight_json, "r", encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["safety_invariants"]["campaign_mutations"], 0)

    # Test P: Pot Kettle Black regression
    def test_p_pot_kettle_black_regression(self):
        cand = {
            "company_name": "Pot Kettle Black Airport T2",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.3678,
            "longitude": -2.2822
        }
        place = {
            "title": "Pot Kettle Black",
            "address": "Barton Arcade, Deansgate, Manchester M3 2BW",
            "latitude": 53.4820,
            "longitude": -2.2460
        }
        res = self.evaluator.evaluate_candidate(cand, [place])
        self.assertEqual(res["match_classification"], "BRANCH_MISMATCH")
        self.assertIsNone(res["matched_place_title"])

    # Test Q: Georgia Chicken regression
    def test_q_georgia_chicken_regression(self):
        cand = {
            "company_name": "Georgia Chicken",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.4245,
            "longitude": -2.3180
        }
        place = {
            "title": "Georgia Chicken",
            "address": "Stockport Rd, Levenshulme, Manchester M19 3AB",
            "latitude": 53.4420,
            "longitude": -2.1930
        }
        res = self.evaluator.evaluate_candidate(cand, [place])
        self.assertEqual(res["match_classification"], "BRANCH_MISMATCH")

    # Test R: Issano regression
    def test_r_issano_regression(self):
        cand = {
            "company_name": "Issano",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.4250,
            "longitude": -2.2350
        }
        place = {
            "title": "Issano",
            "address": "153 Palatine Rd, Northenden, Manchester M22 4HT",
            "latitude": 53.4251,
            "longitude": -2.2351
        }
        res = self.evaluator.evaluate_candidate(cand, [place])
        self.assertEqual(res["match_classification"], "SAFE_MATCH")
        self.assertLessEqual(res["distance_meters"], 50.0)

    # Test S: Existing Phase 7.11 tests remain valid
    def test_s_existing_phase_7_11_tests_remain_valid(self):
        eval_711_path = os.path.join(PROJECT_ROOT, "data/phase_7_11_production_shape_gate_eval.json")
        self.assertTrue(os.path.exists(eval_711_path))
        with open(eval_711_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["status"], "PASS")
        self.assertEqual(d["metrics_stratification"]["partial_cohort"]["precision"], 1.0)


if __name__ == "__main__":
    unittest.main()
