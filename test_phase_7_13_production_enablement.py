#!/usr/bin/env python3
"""
Unit and Integration Tests for Phase 7.13 Limited Production Enablement Review
=============================================================================
Validates all requirements A through T:
  A. Production flag OFF blocks fallback.
  B. Production flag ON permits only explicitly eligible candidates.
  C. Daily cap enforced.
  D. Per-run cap enforced.
  E. Complete-address path unchanged.
  F. Partial coordinate-first path uses name + city only.
  G. SAFE_MATCH attaches evidence only through the existing evidence layer.
  H. Branch mismatch attaches zero evidence.
  I. Identity mismatch attaches zero evidence.
  J. Ambiguous match attaches zero evidence.
  K. Search failure attaches zero evidence.
  L. Google evidence does not bypass Rule B.
  M. No outreach call can originate from Gosom fallback.
  N. Kill switch immediately blocks subsequent fallback calls.
  O. Idempotency prevents duplicate calls where cached.
  P. CRM/campaign/message state unchanged.
  Q. Pot Kettle Black regression.
  R. Georgia Chicken regression.
  S. Issano regression.
  T. Jin Bi Won conservative behavior.
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
    GosomReviewFreshnessFallback,
    LimitedProductionGosomSafetyWrapper,
)
from lib.enrichment.coordinate_matcher import (
    CoordinateMatchClassification,
    IdentityMatchClassification,
    CoordinateMatchResultClassification,
    CoordinateFirstMatcher,
    haversine_distance_m,
    construct_safe_coordinate_query,
)
from lib.pipeline import LeadGenerationPipeline
from lib.qualification.lead_scoring import LeadScoringProvider


class TestPhase713ProductionEnablement(unittest.TestCase):

    def setUp(self):
        os.environ["GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED"] = "true"
        os.environ["GOSOM_FALLBACK_KILL_SWITCH"] = "false"
        self.matcher = BusinessIdentityMatcher()
        self.coord_matcher = CoordinateFirstMatcher(self.matcher)
        self.reconciler = ReviewEvidenceReconciler(self.matcher)
        self.config = GosomFallbackConfig(
            enabled=True,
            max_calls_per_run=5,
            max_calls_per_day=10,
            cache_dir="data/cache_gosom_reviews"
        )
        self.fallback = GosomReviewFreshnessFallback(
            config=self.config,
            matcher=self.matcher,
            reconciler=self.reconciler
        )
        self.wrapper = LimitedProductionGosomSafetyWrapper(
            fallback=self.fallback,
            max_cohort_size=5,
            max_calls_per_run=5,
            max_calls_per_day=10
        )
        # Ensure daily usage is clean for deterministic test runs
        self.fallback._reset_daily_external_calls()

    def tearDown(self):
        # Reset environment flag and kill switch without polluting process
        os.environ.pop("GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED", None)
        os.environ.pop("GOSOM_FALLBACK_KILL_SWITCH", None)

    # Test A: Production flag OFF blocks fallback
    def test_a_flag_off_blocks_fallback(self):
        os.environ["GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED"] = "false"
        cfg = GosomFallbackConfig(enabled=False)
        fb = GosomReviewFreshnessFallback(config=cfg, matcher=self.matcher)
        cand = {
            "company_name": "Rajdan",
            "city": "Manchester",
            "latitude": 53.3981,
            "longitude": -2.3165,
            "review_count": 119,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        rec, telem = fb.enrich_candidate(cand, shadow_mode=False)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "FLAG_DISABLED")

    # Test B: Production flag ON permits only explicitly eligible candidates
    def test_b_flag_on_permits_only_eligible_candidates(self):
        os.environ["GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED"] = "true"
        # 1. Ineligible: low review count (<50)
        cand_low_rev = {
            "company_name": "Low Reviews",
            "city": "Manchester",
            "review_count": 20,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        rec1, telem1 = self.wrapper.enrich_candidate(cand_low_rev)
        self.assertIsNone(rec1)
        self.assertEqual(telem1["status"], "SKIPPED_INELIGIBLE")

        # 2. Ineligible: low rating (<4.0)
        cand_low_rat = {
            "company_name": "Low Rating",
            "city": "Manchester",
            "review_count": 100,
            "rating": 3.5,
            "review_freshness": "UNKNOWN"
        }
        rec2, telem2 = self.wrapper.enrich_candidate(cand_low_rat)
        self.assertIsNone(rec2)
        self.assertEqual(telem2["status"], "SKIPPED_INELIGIBLE")

        # 3. Ineligible: freshness already known
        cand_known = {
            "company_name": "Known Freshness",
            "city": "Manchester",
            "review_count": 100,
            "rating": 4.5,
            "review_freshness": "RECENT"
        }
        rec3, telem3 = self.wrapper.enrich_candidate(cand_known)
        self.assertIsNone(rec3)
        self.assertEqual(telem3["status"], "SKIPPED_INELIGIBLE")

        # 4. Eligible candidate passes gate
        cand_elig = {
            "company_name": "Rajdan",
            "city": "Manchester",
            "latitude": 53.3981871,
            "longitude": -2.3165611,
            "review_count": 119,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        is_elig, reason = self.wrapper.is_candidate_eligible(cand_elig)
        self.assertTrue(is_elig)
        self.assertEqual(reason, "ELIGIBLE")

    # Test C: Daily cap enforced
    def test_c_daily_cap_enforced(self):
        cfg = GosomFallbackConfig(enabled=True, max_calls_per_run=10, max_calls_per_day=2)
        fb = GosomReviewFreshnessFallback(config=cfg, matcher=self.matcher)
        fb._reset_daily_external_calls()
        fb._increment_daily_external_calls(2)  # Max daily cap reached

        cand = {
            "company_name": "New Candidate Never Seen",
            "city": "Manchester",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 80,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        rec, telem = fb.enrich_candidate(cand)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "CAP_DAILY_EXCEEDED")

    # Test D: Per-run cap enforced
    def test_d_per_run_cap_enforced(self):
        cfg = GosomFallbackConfig(enabled=True, max_calls_per_run=2, max_calls_per_day=10)
        fb = GosomReviewFreshnessFallback(config=cfg, matcher=self.matcher)
        mock_places = [{
            "title": "Bistro",
            "place_id": "P1",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 80,
            "review_rating": 4.5,
            "user_reviews": [{"published_at": "2026-08-01T12:00:00Z"}]
        }]
        c1 = {"company_name": "B1", "city": "Manchester", "latitude": 53.4000, "longitude": -2.3000, "review_count": 80, "rating": 4.5, "review_freshness": "UNKNOWN"}
        c2 = {"company_name": "B2", "city": "Manchester", "latitude": 53.4000, "longitude": -2.3000, "review_count": 80, "rating": 4.5, "review_freshness": "UNKNOWN"}
        c3 = {"company_name": "B3", "city": "Manchester", "latitude": 53.4000, "longitude": -2.3000, "review_count": 80, "rating": 4.5, "review_freshness": "UNKNOWN"}

        _, t1 = fb.enrich_candidate(c1, preloaded_places=mock_places)
        _, t2 = fb.enrich_candidate(c2, preloaded_places=mock_places)
        r3, t3 = fb.enrich_candidate(c3, preloaded_places=mock_places)
        self.assertIsNone(r3)
        self.assertEqual(t3["status"], "CAP_EXCEEDED")

    # Test E: Complete-address path unchanged
    def test_e_complete_address_path_unchanged(self):
        cand = {
            "company_name": "Evergreen",
            "city": "Manchester",
            "street": "Barton Road",
            "postcode": "M32 8DN",
            "latitude": 53.4498,
            "longitude": -2.3112,
            "review_count": 80,
            "rating": 4.4,
            "review_freshness": "UNKNOWN"
        }
        mock_place = [{
            "title": "Evergreen Restaurant",
            "address": "14 Barton Rd, Stretford, Manchester M32 8DN, United Kingdom",
            "place_id": "P_EV",
            "latitude": 53.4498,
            "longitude": -2.3112,
            "review_count": 80,
            "review_rating": 4.4,
            "user_reviews": [{"published_at": "2026-08-10T12:00:00Z"}]
        }]
        rec, telem = self.wrapper.enrich_candidate(cand, preloaded_places=mock_place)
        self.assertEqual(telem["path"], "PATH_A")
        self.assertEqual(telem["query"], '"Evergreen" "Barton Road" "M32 8DN" "Manchester"')
        self.assertIsNotNone(rec)

    # Test F: Partial coordinate-first path uses name + city only
    def test_f_partial_coordinate_first_path_query(self):
        cand = {
            "company_name": "Taste India",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "latitude": 53.3978728,
            "longitude": -2.3173789,
            "review_count": 85,
            "rating": 4.3,
            "review_freshness": "UNKNOWN"
        }
        mock_place = [{
            "title": "Taste India",
            "place_id": "P_TI",
            "latitude": 53.3978728,
            "longitude": -2.3173789,
            "review_count": 85,
            "review_rating": 4.3,
            "user_reviews": [{"published_at": "2026-08-20T12:00:00Z"}]
        }]
        rec, telem = self.wrapper.enrich_candidate(cand, preloaded_places=mock_place)
        self.assertEqual(telem["path"], "PATH_B")
        self.assertEqual(telem["query"], '"Taste India" "Manchester"')
        self.assertNotIn("street", telem["query"].lower())
        self.assertNotIn("postcode", telem["query"].lower())

    # Test G: SAFE_MATCH attaches evidence only through the existing evidence layer
    def test_g_safe_match_attaches_evidence(self):
        cand = {
            "company_name": "Rajdan",
            "city": "Manchester",
            "latitude": 53.3981871,
            "longitude": -2.3165611,
            "review_count": 119,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        mock_place = [{
            "title": "Rajdan, Indian Takeaway, Timperley",
            "place_id": "P_RAJ",
            "latitude": 53.3981662,
            "longitude": -2.3166131,
            "review_count": 119,
            "review_rating": 4.5,
            "user_reviews": [{"published_at": "2026-09-05T18:30:00Z"}]
        }]
        rec, telem = self.wrapper.enrich_candidate(cand, preloaded_places=mock_place)
        self.assertIsNotNone(rec)
        self.assertEqual(telem["match_classification"], "SAFE_MATCH")
        self.assertEqual(rec.reconciled_freshness, "RECENT")
        self.assertEqual(rec.primary_source_family, SourceFamily.GOOGLE.value)

    # Test H: Branch mismatch attaches zero evidence
    def test_h_branch_mismatch_blocks_evidence(self):
        cand = {
            "company_name": "Sultan Shawarma",
            "city": "Manchester",
            "latitude": 53.4246191,
            "longitude": -2.3196035,
            "review_count": 120,
            "rating": 4.4,
            "review_freshness": "UNKNOWN"
        }
        # Distant branch in Rusholme (3.5 km away > 180m)
        mock_place = [{
            "title": "Sultan Shawarma",
            "place_id": "P_SULTAN",
            "latitude": 53.4546,
            "longitude": -2.2200,
            "review_count": 120,
            "review_rating": 4.4,
            "user_reviews": [{"published_at": "2026-09-01T12:00:00Z"}]
        }]
        rec, telem = self.wrapper.enrich_candidate(cand, preloaded_places=mock_place)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "BLOCKED")
        self.assertEqual(telem["match_classification"], "BRANCH_MISMATCH")

    # Test I: Identity mismatch attaches zero evidence
    def test_i_identity_mismatch_blocks_evidence(self):
        cand = {
            "company_name": "FF",
            "city": "Manchester",
            "latitude": 53.4245,
            "longitude": -2.3180,
            "review_count": 50,
            "rating": 4.1,
            "review_freshness": "UNKNOWN"
        }
        # Unrelated bookstore
        mock_place = [{
            "title": "Waterstones Manchester",
            "place_id": "P_WS",
            "latitude": 53.4245,
            "longitude": -2.3180,
            "review_count": 50,
            "review_rating": 4.1,
            "user_reviews": [{"published_at": "2026-08-01T12:00:00Z"}]
        }]
        rec, telem = self.wrapper.enrich_candidate(cand, preloaded_places=mock_place)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "BLOCKED")
        self.assertEqual(telem["match_classification"], "IDENTITY_MISMATCH")

    # Test J: Ambiguous match attaches zero evidence
    def test_j_ambiguous_match_blocks_evidence(self):
        cand = {
            "company_name": "Ambiguous Diner",
            "city": "Manchester",
            "latitude": 53.4200,
            "longitude": -2.3100,
            "review_count": 60,
            "rating": 4.2,
            "review_freshness": "UNKNOWN"
        }
        # Two places at identical distance without clear winner
        mock_places = [
            {"title": "Ambiguous Diner North", "place_id": "P_AMB1", "latitude": 53.4203, "longitude": -2.3100, "review_count": 60, "review_rating": 4.2},
            {"title": "Ambiguous Diner South", "place_id": "P_AMB2", "latitude": 53.4203, "longitude": -2.3101, "review_count": 60, "review_rating": 4.2}
        ]
        rec, telem = self.wrapper.enrich_candidate(cand, preloaded_places=mock_places)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "BLOCKED")
        self.assertEqual(telem["match_classification"], "AMBIGUOUS_MATCH")

    # Test K: Search failure attaches zero evidence
    def test_k_search_failure_blocks_evidence(self):
        cand = {
            "company_name": "Ghost Kitchen X",
            "city": "Manchester",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 70,
            "rating": 4.2,
            "review_freshness": "UNKNOWN"
        }
        rec, telem = self.wrapper.enrich_candidate(cand, preloaded_places=[])
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "SEARCH_RECALL_FAILURE")

    # Test L: Google evidence does not bypass Rule B
    def test_l_google_evidence_does_not_bypass_rule_b(self):
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Taste India",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            country_status="COUNTRY_MATCH",
            address="Manchester, UK",
            phone="",  # NO independent operational signal
            lat=53.3978728,
            lon=-2.3173789,
            review_count=85,
            rating=4.3,
            latest_review_date="2026-08-20"
        )
        biz.review_freshness = "RECENT"
        biz.raw_data = {
            "review_enrichment": {
                "source_family": SourceFamily.GOOGLE.value,
                "review_freshness": "RECENT"
            }
        }
        audit = scorer.evaluate_lead(
            business=biz,
            verification_status="NO_WEBSITE_CONFIRMED",
            verification_reason="Confirmed no website"
        )
        # Rule B requires >= 2 independent source families for operational corroboration
        # Here Google Maps review is the ONLY signal, so candidate stays MANUAL_REVIEW / not outreach ready
        self.assertFalse(audit["is_outreach_ready"])
        self.assertEqual(audit["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    # Test M: No outreach call can originate from Gosom fallback
    def test_m_no_outreach_call_origination(self):
        # Fallback returns reconciled evidence only, never dispatches or triggers outreach
        cand = {
            "company_name": "Rajdan",
            "city": "Manchester",
            "latitude": 53.3981871,
            "longitude": -2.3165611,
            "review_count": 119,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        mock_place = [{
            "title": "Rajdan",
            "place_id": "P_RAJ",
            "latitude": 53.3981662,
            "longitude": -2.3166131,
            "review_count": 119,
            "review_rating": 4.5,
            "user_reviews": [{"published_at": "2026-09-05T18:30:00Z"}]
        }]
        rec, telem = self.wrapper.enrich_candidate(cand, preloaded_places=mock_place)
        self.assertNotIn("outreach_sent", telem)
        self.assertNotIn("campaign_id", telem)

    # Test N: Kill switch immediately blocks subsequent fallback calls
    def test_n_kill_switch_immediately_blocks(self):
        cand = {
            "company_name": "Rajdan",
            "city": "Manchester",
            "latitude": 53.3981871,
            "longitude": -2.3165611,
            "review_count": 119,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        # Activate kill switch
        self.wrapper.activate_kill_switch()
        self.assertTrue(self.wrapper.kill_switch_active)

        rec, telem = self.wrapper.enrich_candidate(cand)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "KILL_SWITCH_ACTIVE")

        # Deactivate kill switch
        self.wrapper.deactivate_kill_switch()
        self.assertFalse(self.wrapper.kill_switch_active)

    # Test O: Idempotency prevents duplicate calls where cached
    def test_o_idempotency_prevents_duplicate_calls(self):
        cache_key = self.fallback._compute_cache_key("Idempotent Bistro", "Manchester", '"Idempotent Bistro" "Manchester"')
        mock_places = [{
            "title": "Idempotent Bistro",
            "place_id": "P_IDEM",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 90,
            "review_rating": 4.6,
            "user_reviews": [{"published_at": "2026-08-01T12:00:00Z"}]
        }]
        self.fallback.save_cached_result(cache_key, '"Idempotent Bistro" "Manchester"', mock_places, status="SUCCESS")

        cand = {
            "company_name": "Idempotent Bistro",
            "city": "Manchester",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 90,
            "rating": 4.6,
            "review_freshness": "UNKNOWN"
        }
        initial_ext = self.fallback.external_calls_this_run
        rec1, t1 = self.fallback.enrich_candidate(cand)
        self.assertTrue(t1["cache_hit"])
        self.assertEqual(self.fallback.external_calls_this_run, initial_ext)

        rec2, t2 = self.fallback.enrich_candidate(cand)
        self.assertTrue(t2["cache_hit"])
        self.assertEqual(self.fallback.external_calls_this_run, initial_ext)
        self.assertEqual(rec1.reconciled_freshness, rec2.reconciled_freshness)

    # Test P: CRM/campaign/message state unchanged
    def test_p_state_unchanged(self):
        state_files = [
            "data/cache_sheets_leads.json",
            "data/cache_sheets_review_queue.json",
            "data/cache_sheets_research_log.json",
            "data/campaigns.json",
            "data/message_history.json"
        ]
        hashes_before = {}
        for f in state_files:
            fp = os.path.join(PROJECT_ROOT, f)
            if os.path.exists(fp):
                with open(fp, "rb") as fh:
                    hashes_before[f] = hashlib.sha256(fh.read()).hexdigest()

        # Execute candidate enrichment
        cand = {
            "company_name": "Rajdan",
            "city": "Manchester",
            "latitude": 53.3981871,
            "longitude": -2.3165611,
            "review_count": 119,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        mock_place = [{
            "title": "Rajdan",
            "place_id": "P_RAJ",
            "latitude": 53.3981662,
            "longitude": -2.3166131,
            "review_count": 119,
            "review_rating": 4.5,
            "user_reviews": [{"published_at": "2026-09-05T18:30:00Z"}]
        }]
        self.wrapper.enrich_candidate(cand, preloaded_places=mock_place)

        for f, h_before in hashes_before.items():
            fp = os.path.join(PROJECT_ROOT, f)
            with open(fp, "rb") as fh:
                h_after = hashlib.sha256(fh.read()).hexdigest()
            self.assertEqual(h_before, h_after, f"State file {f} mutated!")

    # Test Q: Pot Kettle Black regression
    def test_q_pot_kettle_black_regression(self):
        # Airport T2 branch candidate (~13 km away from city centre)
        cand_pkb = {
            "company_name": "Pot Kettle Black",
            "city": "Manchester",
            "latitude": 53.3678333,
            "longitude": -2.2822664,
            "review_count": 150,
            "rating": 4.6,
            "review_freshness": "UNKNOWN"
        }
        # City centre places (~13 km away > 180m)
        places = [
            {"title": "Pot Kettle Black - Barton Arcade", "place_id": "PKB_BARTON", "latitude": 53.4827501, "longitude": -2.2463011, "review_count": 150, "review_rating": 4.6, "user_reviews": [{"published_at": "2026-09-01T12:00:00Z"}]},
            {"title": "Pot Kettle Black - Angel Gardens", "place_id": "PKB_ANGEL", "latitude": 53.4865137, "longitude": -2.2360551, "review_count": 95, "review_rating": 4.4, "user_reviews": [{"published_at": "2026-08-15T12:00:00Z"}]}
        ]
        rec, telem = self.wrapper.enrich_candidate(cand_pkb, preloaded_places=places)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "BLOCKED")
        self.assertEqual(telem["match_classification"], "BRANCH_MISMATCH")

    # Test R: Georgia Chicken regression
    def test_r_georgia_chicken_regression(self):
        cand_gc = {
            "company_name": "Georgia Chicken",
            "city": "Manchester",
            "latitude": 53.4400,
            "longitude": -2.2500,
            "review_count": 80,
            "rating": 4.3,
            "review_freshness": "UNKNOWN"
        }
        # Distant branch (> 180m)
        places = [
            {"title": "Georgia Chicken", "place_id": "GC_DISTANT", "latitude": 53.4600, "longitude": -2.2100, "review_count": 80, "review_rating": 4.3, "user_reviews": [{"published_at": "2026-09-01T12:00:00Z"}]}
        ]
        rec, telem = self.wrapper.enrich_candidate(cand_gc, preloaded_places=places)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "BLOCKED")
        self.assertEqual(telem["match_classification"], "BRANCH_MISMATCH")

    # Test S: Issano regression
    def test_s_issano_regression(self):
        cand_issano = {
            "company_name": "Issano Pizza",
            "city": "Manchester",
            "latitude": 53.4328,
            "longitude": -2.2215,
            "review_count": 75,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        places = [
            {"title": "Issano Pizza", "place_id": "ISSANO_1", "latitude": 53.43281, "longitude": -2.22152, "review_count": 75, "review_rating": 4.5, "user_reviews": [{"published_at": "2026-08-25T12:00:00Z"}]}
        ]
        rec, telem = self.wrapper.enrich_candidate(cand_issano, preloaded_places=places)
        self.assertIsNotNone(rec)
        self.assertEqual(telem["match_classification"], "SAFE_MATCH")
        self.assertEqual(rec.reconciled_freshness, "RECENT")

    # Test T: Jin Bi Won conservative behavior
    def test_t_jin_bi_won_conservative_behavior(self):
        cand_jbw = {
            "company_name": "Jin Bi Won",
            "city": "Manchester",
            "latitude": 53.4100,
            "longitude": -2.2600,
            "review_count": 65,
            "rating": 4.2,
            "review_freshness": "UNKNOWN"
        }
        # Inconclusive/distant match (dist: 250m > 180m)
        places = [
            {"title": "Jin Bi Won Korean BBQ", "place_id": "JBW_1", "latitude": 53.4120, "longitude": -2.2625, "review_count": 65, "review_rating": 4.2, "user_reviews": [{"published_at": "2026-08-01T12:00:00Z"}]}
        ]
        rec, telem = self.wrapper.enrich_candidate(cand_jbw, preloaded_places=places)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "BLOCKED")
        self.assertEqual(telem["match_classification"], "BRANCH_MISMATCH")


if __name__ == "__main__":
    unittest.main()
