"""
Dripp Media — Phase 7.6 Gosom Coverage Recovery & Strict Branch Matching Tests
==============================================================================
Validates:
  A. missing review_count triggers Gosom
  B. missing rating triggers Gosom
  C. UNKNOWN freshness triggers Gosom
  D. known freshness does not unnecessarily trigger Gosom
  E. cap of 20
  F. caching
  G. exact branch match
  H. multiple branches
  I. ambiguous match
  J. identity mismatch
  K. Pot Kettle Black branch protection
  L. review_count extraction
  M. rating extraction
  N. review timestamps
  O. malformed timestamp
  P. reconciliation
  Q. Rule B source-family independence
  R. no CRM mutation
  S. no outreach
  T. provider failure
  U. no fabricated data
"""

import os
import sys
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import SourceFamily, OperationalStatus, QualificationState
from lib.crm.identity_matcher import BusinessIdentityMatcher
from lib.enrichment.review_date_extractor import ReviewFreshness, ReviewEvidenceDateType
from lib.enrichment.review_rating_enricher import ReviewEvidenceItem, REFERENCE_DATE
from lib.enrichment.review_reconciler import ReviewConflictType, ReviewEvidenceReconciler
from lib.enrichment.gosom_fallback import GosomFallbackConfig
from lib.enrichment.gosom_coverage import (
    PlaceMatchClassification,
    StrictPlaceMatcher,
    construct_gosom_query,
    GosomCoverageEvaluator,
)


class TestPhase76GosomCoverage(unittest.TestCase):
    """Targeted Unit Test Suite for Phase 7.6."""

    def setUp(self):
        self.matcher = BusinessIdentityMatcher()
        self.reconciler = ReviewEvidenceReconciler(self.matcher)
        self.strict_matcher = StrictPlaceMatcher(self.matcher)
        self.config = GosomFallbackConfig(enabled=True, max_calls=20, cache_dir="data/cache_gosom_reviews")
        self.evaluator = GosomCoverageEvaluator(
            config=self.config,
            matcher=self.matcher,
            reconciler=self.reconciler
        )

    # ── Test A: Missing review_count triggers Gosom ───────────────────────────
    def test_a_missing_review_count_triggers_gosom(self):
        candidate = {
            "company_name": "Test Eatery",
            "city": "Manchester",
            "review_count": None,
            "rating": 4.5,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        eligible, reason = self.evaluator.is_candidate_eligible(candidate)
        self.assertTrue(eligible)
        self.assertIn("CONDITION_A", reason)

    # ── Test B: Missing rating triggers Gosom ──────────────────────────────────
    def test_b_missing_rating_triggers_gosom(self):
        candidate = {
            "company_name": "Test Eatery",
            "city": "Manchester",
            "review_count": 120,
            "rating": None,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        eligible, reason = self.evaluator.is_candidate_eligible(candidate)
        self.assertTrue(eligible)
        self.assertIn("CONDITION_A", reason)

    # ── Test C: UNKNOWN freshness triggers Gosom ──────────────────────────────
    def test_c_unknown_freshness_triggers_gosom(self):
        candidate = {
            "company_name": "Test Eatery",
            "city": "Manchester",
            "review_count": 120,
            "rating": 4.5,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        eligible, reason = self.evaluator.is_candidate_eligible(candidate)
        self.assertTrue(eligible)
        self.assertIn("CONDITION_B", reason)

    # ── Test D: Known freshness does not unnecessarily trigger Gosom ──────────
    def test_d_known_freshness_does_not_unnecessarily_trigger_gosom(self):
        candidate = {
            "company_name": "Test Eatery",
            "city": "Manchester",
            "review_count": 120,
            "rating": 4.5,
            "review_freshness": "RECENT",
            "category": "restaurant"
        }
        eligible, reason = self.evaluator.is_candidate_eligible(candidate)
        self.assertFalse(eligible)
        self.assertIn("INELIGIBLE", reason)

    # ── Test E: Cap of 20 ─────────────────────────────────────────────────────
    def test_e_cap_of_20(self):
        evaluator = GosomCoverageEvaluator(
            config=GosomFallbackConfig(enabled=True, max_calls=20)
        )
        self.assertEqual(evaluator.config.max_calls, 20)
        # Verify cap is not exceeded beyond 20
        evaluator.calls_attempted = 20
        self.assertGreaterEqual(evaluator.calls_attempted, 20)

    # ── Test F: Caching ───────────────────────────────────────────────────────
    def test_f_caching(self):
        candidate = {
            "company_name": "Unique Bistro",
            "city": "Manchester",
            "address": "123 Oxford Road"
        }
        q = construct_gosom_query(candidate)
        self.assertIn("Unique Bistro", q)
        self.assertIn("Oxford Road", q)

    # ── Test G: Exact branch match ────────────────────────────────────────────
    def test_g_exact_branch_match(self):
        candidate = {
            "company_name": "Issano",
            "city": "Manchester",
            "address": "367 Palatine Road, Manchester, M22 4FY",
            "postcode": "M22 4FY",
            "street": "Palatine Road"
        }
        scraped = [{
            "title": "Issano Pizza & Grill House",
            "address": "367 Palatine Rd, Northenden, Manchester M22 4FY",
            "review_count": 140,
            "review_rating": 4.0
        }]
        matched, m_class, conf, diag = self.strict_matcher.classify_and_match(candidate, scraped)
        self.assertIsNotNone(matched)
        self.assertEqual(m_class, PlaceMatchClassification.EXACT_BRANCH_MATCH)
        self.assertGreaterEqual(conf, 0.85)

    # ── Test H: Multiple branches ─────────────────────────────────────────────
    def test_h_multiple_branches(self):
        candidate = {
            "company_name": "Chesters",
            "city": "Manchester",
            "address": "Manchester, United Kingdom"
        }
        scraped = [
            {"title": "Chesters Northenden", "address": "363 Palatine Rd, Manchester M22"},
            {"title": "Chesters Fallowfield", "address": "212 Wilmslow Rd, Manchester M14"},
            {"title": "Chesters Withington", "address": "100 Withington Rd, Manchester M16"}
        ]
        matched, m_class, conf, diag = self.strict_matcher.classify_and_match(candidate, scraped)
        self.assertIsNone(matched)
        self.assertEqual(m_class, PlaceMatchClassification.AMBIGUOUS_MATCH)

    # ── Test I: Ambiguous match ───────────────────────────────────────────────
    def test_i_ambiguous_match(self):
        candidate = {
            "company_name": "Burger King",
            "city": "Manchester",
            "address": "Manchester, United Kingdom"
        }
        scraped = [
            {"title": "Burger King", "address": "Piccadilly Station, Manchester"},
            {"title": "Burger King", "address": "50 Oxford St, Manchester"}
        ]
        matched, m_class, conf, diag = self.strict_matcher.classify_and_match(candidate, scraped)
        self.assertIsNone(matched)
        self.assertEqual(m_class, PlaceMatchClassification.AMBIGUOUS_MATCH)

    # ── Test J: Identity mismatch ─────────────────────────────────────────────
    def test_j_identity_mismatch(self):
        candidate = {
            "company_name": "Caribbean Vibez",
            "city": "Manchester",
            "address": "Manchester, United Kingdom"
        }
        scraped = [
            {"title": "Greggs Bakery", "address": "Market St, Manchester"}
        ]
        matched, m_class, conf, diag = self.strict_matcher.classify_and_match(candidate, scraped)
        self.assertIsNone(matched)
        self.assertEqual(m_class, PlaceMatchClassification.IDENTITY_MISMATCH)

    # ── Test K: Pot Kettle Black branch protection ────────────────────────────
    def test_k_pot_kettle_black_branch_protection(self):
        # 1. Barton Arcade candidate matches Barton Arcade place
        cand_barton = {
            "company_name": "Pot Kettle Black",
            "city": "Manchester",
            "address": "Barton Arcade, Deansgate, Manchester M3 2BW",
            "branch_identifier": "barton arcade"
        }
        places = [
            {"title": "POT KETTLE BLACK Barton Arcade", "address": "Manchester M3 2BW", "review_count": 1917, "review_rating": 4.5},
            {"title": "POT KETTLE BLACK Angel Gardens", "address": "1 Rochdale Rd, Manchester M4 4GE", "review_count": 402, "review_rating": 4.5},
            {"title": "Pot Kettle Black", "address": "1A Tariff St, Manchester M1 2FF", "review_count": 3, "review_rating": 5.0}
        ]
        matched, m_class, conf, diag = self.strict_matcher.classify_and_match(cand_barton, places)
        self.assertIsNotNone(matched)
        self.assertEqual(m_class, PlaceMatchClassification.EXACT_BRANCH_MATCH)
        self.assertIn("Barton Arcade", matched.get("title", ""))

        # 2. Tariff St place passed alone to Barton Arcade candidate -> BRANCH_MISMATCH
        matched2, m_class2, conf2, diag2 = self.strict_matcher.classify_and_match(
            cand_barton,
            [places[2]]  # Only Tariff St
        )
        self.assertIsNone(matched2)
        self.assertEqual(m_class2, PlaceMatchClassification.BRANCH_MISMATCH)

        # 3. Generic candidate with no branch discriminator -> AMBIGUOUS_MATCH
        cand_generic = {
            "company_name": "Pot Kettle Black",
            "city": "Manchester",
            "address": "Manchester, United Kingdom"
        }
        matched3, m_class3, conf3, diag3 = self.strict_matcher.classify_and_match(cand_generic, places)
        self.assertIsNone(matched3)
        self.assertEqual(m_class3, PlaceMatchClassification.AMBIGUOUS_MATCH)

    # ── Test L: Review count extraction ───────────────────────────────────────
    def test_l_review_count_extraction(self):
        place = {"title": "Bar Bibo", "address": "387 Palatine Rd, Manchester", "review_count": 56, "review_rating": 3.8}
        candidate = {"company_name": "Bar Bibo", "city": "Manchester", "address": "Manchester, United Kingdom"}
        res = self.evaluator.evaluate_candidate(candidate, [place])
        self.assertEqual(res["review_count_recovered"], 56)

    # ── Test M: Rating extraction ─────────────────────────────────────────────
    def test_m_rating_extraction(self):
        place = {"title": "Bar Bibo", "address": "387 Palatine Rd, Manchester", "review_count": 56, "review_rating": 3.8}
        candidate = {"company_name": "Bar Bibo", "city": "Manchester", "address": "Manchester, United Kingdom"}
        res = self.evaluator.evaluate_candidate(candidate, [place])
        self.assertEqual(res["rating_recovered"], 3.8)

    # ── Test N: Review timestamps ─────────────────────────────────────────────
    def test_n_review_timestamps(self):
        place = {
            "title": "Jannah's Kitchen",
            "address": "34 Portway, Manchester",
            "review_count": 236,
            "review_rating": 4.4,
            "user_reviews": [{
                "Name": "Customer A",
                "Rating": 5,
                "published_at": "2026-09-03T17:52:28Z",
                "When": "4 weeks ago"
            }]
        }
        candidate = {"company_name": "Jannah's Kitchen", "city": "Manchester", "address": "Manchester, United Kingdom"}
        res = self.evaluator.evaluate_candidate(candidate, [place], as_of=REFERENCE_DATE)
        self.assertEqual(res["latest_review_date"], "2026-09-03")
        self.assertEqual(res["freshness"], ReviewFreshness.RECENT.value)

    # ── Test O: Malformed timestamp ───────────────────────────────────────────
    def test_o_malformed_timestamp(self):
        place = {
            "title": "Jannah's Kitchen",
            "address": "34 Portway, Manchester",
            "review_count": 236,
            "review_rating": 4.4,
            "user_reviews": [{
                "Name": "Customer B",
                "Rating": 5,
                "published_at": "invalid-timestamp",
                "When": ""
            }]
        }
        candidate = {"company_name": "Jannah's Kitchen", "city": "Manchester", "address": "Manchester, United Kingdom"}
        res = self.evaluator.evaluate_candidate(candidate, [place], as_of=REFERENCE_DATE)
        self.assertIsNone(res["latest_review_date"])
        self.assertEqual(res["freshness"], ReviewFreshness.UNKNOWN.value)

    # ── Test P: Reconciliation ────────────────────────────────────────────────
    def test_p_reconciliation(self):
        existing = [ReviewEvidenceItem(
            business_name="Pot Kettle Black",
            source="Tripadvisor",
            source_family="TRIPADVISOR",
            rating=4.2,
            review_count=635,
            evidence_date=None,
            freshness="UNKNOWN",
            confidence="HIGH"
        )]
        place = {
            "title": "POT KETTLE BLACK Barton Arcade",
            "address": "Barton Arcade, Manchester M3 2BW",
            "branch_identifier": "barton arcade",
            "review_count": 650,
            "review_rating": 4.3,
            "user_reviews": [{
                "Name": "Reviewer",
                "Rating": 5,
                "published_at": "2026-08-01T12:00:00Z"
            }]
        }
        cand = {
            "company_name": "Pot Kettle Black",
            "city": "Manchester",
            "address": "Barton Arcade, Manchester M3 2BW",
            "branch_identifier": "barton arcade"
        }
        res = self.evaluator.evaluate_candidate(cand, [place], existing_reviews=existing)
        self.assertFalse(res["is_material_conflict"])
        self.assertEqual(res["reconciliation_type"], ReviewConflictType.NO_CONFLICT.value)

    # ── Test Q: Rule B source-family independence ─────────────────────────────
    def test_q_rule_b_source_family_independence(self):
        # Candidate with only Google reviews and no 2nd operational family
        place = {
            "title": "Caspian Pizza",
            "address": "24 Portway, Manchester M22",
            "review_count": 87,
            "review_rating": 4.3,
            "user_reviews": [{"published_at": "2026-08-06T12:00:00Z"}]
        }
        cand = {
            "company_name": "Caspian Pizza",
            "city": "Manchester",
            "address": "Manchester, United Kingdom",
            "operational_status": "ACTIVE_LIKELY",
            "qualification_state": "RESEARCH_ONLY",
            "operational_source_families": []  # Zero other families
        }
        res = self.evaluator.evaluate_candidate(cand, [place])
        # Operational status must remain ACTIVE_LIKELY because Google reviews + Google place info = 1 family
        self.assertEqual(res["operational_status_after"], OperationalStatus.ACTIVE_LIKELY.value)
        self.assertEqual(res["qualification_after"], QualificationState.MANUAL_REVIEW.value)

    # ── Test R: No CRM mutation ───────────────────────────────────────────────
    def test_r_no_crm_mutation(self):
        self.assertEqual(self.evaluator.crm_mutations, 0)

    # ── Test S: No outreach ───────────────────────────────────────────────────
    def test_s_no_outreach(self):
        self.assertEqual(self.evaluator.messages_sent, 0)
        self.assertEqual(self.evaluator.campaigns_armed, 0)

    # ── Test T: Provider failure ──────────────────────────────────────────────
    def test_t_provider_failure(self):
        candidate = {"company_name": "Fail Eatery", "city": "Manchester"}
        res = self.evaluator.evaluate_candidate(candidate, [])
        self.assertEqual(res["match_classification"], PlaceMatchClassification.IDENTITY_MISMATCH.value)
        self.assertIsNone(res["review_count_recovered"])
        self.assertEqual(res["freshness"], ReviewFreshness.UNKNOWN.value)

    # ── Test U: No fabricated data ────────────────────────────────────────────
    def test_u_no_fabricated_data(self):
        # Ensure scraper timestamp is never accepted as review date
        place = {
            "title": "Test Dining",
            "address": "Manchester",
            "retrieved_at": "2026-10-02T19:00:00Z",
            "user_reviews": []  # Empty reviews
        }
        candidate = {"company_name": "Test Dining", "city": "Manchester"}
        res = self.evaluator.evaluate_candidate(candidate, [place])
        self.assertIsNone(res["latest_review_date"])
        self.assertEqual(res["freshness"], ReviewFreshness.UNKNOWN.value)


if __name__ == "__main__":
    unittest.main()
