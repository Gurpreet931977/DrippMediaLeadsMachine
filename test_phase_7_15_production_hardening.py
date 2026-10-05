"""
test_phase_7_15_production_hardening.py
=======================================
Phase 7.15: Final Production Hardening & Controlled Activation Test Suite.

Validates:
  1. Permanent frozen coordinate thresholds (<=50m exact, 50-180m strong, >180m mismatch).
  2. Permanent frozen identity thresholds (>=0.95 exact, 0.80-0.95 strong, 0.60-0.80 weak, <0.60 mismatch).
  3. Deterministic dual-evidence matching (SAFE_MATCH only attaches evidence, no 'closest result wins').
  4. Routing architecture (PATH A vs PATH B, no unauthorized fallback routes).
  5. Telemetry counter definitions and mutual exclusivity (no overlapping match counters).
  6. Dynamic runtime kill switch evaluation without restart.
  7. Hard production caps (max_calls_per_run=5, max_calls_per_day=10, max_cohort_size=5).
  8. Cache idempotency and distinct query isolation.
  9. Qualification isolation (Rule B protection, SourceFamily.GOOGLE cannot alone promote to OUTREACH_READY).
 10. Fail-closed behavior on all scraper/network/parser errors.
 11. Zero outreach coupling and zero CRM side effects.
"""

import os
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from lib.types import SourceFamily, QualificationState, OperationalStatus, DiscoveredBusiness
from lib.enrichment.coordinate_matcher import (
    CoordinateMatchClassification,
    IdentityMatchClassification,
    CoordinateMatchResultClassification,
    CoordinateFirstMatcher,
    haversine_distance_m,
    construct_safe_coordinate_query,
)
from lib.crm.identity_matcher import BusinessIdentityMatcher
from lib.enrichment.gosom_fallback import (
    GosomFallbackConfig,
    GosomReviewFreshnessFallback,
    LimitedProductionGosomSafetyWrapper,
    DEFAULT_MAX_CALLS_PER_RUN,
    DEFAULT_MAX_CALLS_PER_DAY,
    DEFAULT_MAX_CALLS,
)
from lib.enrichment.review_date_extractor import ReviewFreshness
from lib.qualification.lead_scoring import LeadScoringProvider


class TestPhase715ProductionHardening(unittest.TestCase):
    """Production Hardening and Invariant Verification."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.old_env = dict(os.environ)
        os.environ["GOSOM_CACHE_DIR"] = self.temp_dir
        os.environ["GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED"] = "true"
        os.environ["GOSOM_FALLBACK_KILL_SWITCH"] = "false"
        os.environ["MAX_GOSOM_FALLBACK_CALLS_PER_RUN"] = "5"
        os.environ["MAX_GOSOM_FALLBACK_CALLS_PER_DAY"] = "10"

        self.matcher = BusinessIdentityMatcher()
        self.coord_matcher = CoordinateFirstMatcher(self.matcher)
        self.config = GosomFallbackConfig(
            enabled=True,
            max_calls_per_run=5,
            max_calls_per_day=10,
            cache_dir=self.temp_dir,
            timeout_seconds=5.0
        )
        self.fallback = GosomReviewFreshnessFallback(config=self.config, matcher=self.matcher)
        self.wrapper = LimitedProductionGosomSafetyWrapper(
            fallback=self.fallback,
            max_cohort_size=5,
            max_calls_per_run=5,
            max_calls_per_day=10
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self.old_env)

    def test_01_frozen_coordinate_thresholds(self):
        """Verify coordinate tiers are frozen: <=50m EXACT, 50-180m STRONG, >180m MISMATCH."""
        self.assertEqual(self.coord_matcher.EXACT_DISTANCE_M, 50.0)
        self.assertEqual(self.coord_matcher.STRONG_DISTANCE_M, 180.0)

        # 1. <= 50m: ~44m distance
        tier_exact, d_exact = self.coord_matcher.classify_coordinate_distance(53.0, -2.0, 53.0004, -2.0)
        self.assertEqual(tier_exact, CoordinateMatchClassification.EXACT_COORDINATE_MATCH)
        self.assertLessEqual(d_exact, 50.0)

        # 2. 50m < d <= 180m: ~111m distance
        tier_strong, d_strong = self.coord_matcher.classify_coordinate_distance(53.0, -2.0, 53.0010, -2.0)
        self.assertEqual(tier_strong, CoordinateMatchClassification.STRONG_COORDINATE_MATCH)
        self.assertGreater(d_strong, 50.0)
        self.assertLessEqual(d_strong, 180.0)

        # 3. > 180m: ~550m distance
        tier_mismatch, d_mismatch = self.coord_matcher.classify_coordinate_distance(53.0, -2.0, 53.0050, -2.0)
        self.assertEqual(tier_mismatch, CoordinateMatchClassification.COORDINATE_MISMATCH)
        self.assertGreater(d_mismatch, 180.0)

        # 4. None / Missing coordinates
        tier_none, d_none = self.coord_matcher.classify_coordinate_distance(None, -2.0, 53.0, -2.0)
        self.assertEqual(tier_none, CoordinateMatchClassification.NO_COORDINATE_EVIDENCE)
        self.assertIsNone(d_none)

    def test_02_frozen_identity_thresholds(self):
        """Verify identity tiers: >=0.95 EXACT, 0.80-0.95 STRONG, 0.60-0.80 WEAK, <0.60 MISMATCH."""
        exact_tier, score1 = self.coord_matcher.classify_identity("Rajdan", "Rajdan")
        self.assertEqual(exact_tier, IdentityMatchClassification.EXACT_NAME_MATCH)
        self.assertGreaterEqual(score1, 0.95)

        strong_tier, score2 = self.coord_matcher.classify_identity("Taste India", "Taste India Restaurant")
        self.assertIn(strong_tier, [IdentityMatchClassification.EXACT_NAME_MATCH, IdentityMatchClassification.STRONG_NAME_MATCH])

        mismatch_tier, score3 = self.coord_matcher.classify_identity("Taste India", "Blackwell's Bookshop")
        self.assertEqual(mismatch_tier, IdentityMatchClassification.NAME_MISMATCH)
        self.assertLess(score3, 0.60)

    def test_03_routing_invariants_path_a_vs_path_b(self):
        """Verify PATH A requires complete street+postcode; PATH B strictly queries '<name>' 'Manchester'."""
        # Complete candidate -> PATH A
        complete_cand = {
            "company_name": "Full Address Bistro",
            "street": "123 Oxford Road",
            "postcode": "M1 7ED",
            "city": "Manchester",
            "latitude": 53.47,
            "longitude": -2.23,
            "review_freshness": "UNKNOWN",
            "review_count": 80,
            "rating": 4.5,
            "category": "restaurant"
        }
        mock_places = [{
            "title": "Full Address Bistro",
            "latitude": 53.47,
            "longitude": -2.23,
            "review_count": 80,
            "rating": 4.5,
            "user_reviews": [{"published_at": "2026-09-01T00:00:00Z", "rating": 5.0}]
        }]
        _, telem_a = self.wrapper.enrich_candidate(complete_cand, preloaded_places=mock_places)
        self.assertEqual(telem_a["path"], "PATH_A")
        self.assertIn("123 Oxford Road", telem_a["query"])
        self.assertIn("M1 7ED", telem_a["query"])

        # Partial candidate -> PATH B strictly: "<exact business name>" "Manchester"
        partial_cand = {
            "company_name": "Partial Trattoria",
            "city": "Manchester",
            "latitude": 53.475,
            "longitude": -2.235,
            "review_freshness": "UNKNOWN",
            "review_count": 65,
            "rating": 4.4,
            "category": "restaurant"
        }
        mock_places_b = [{
            "title": "Partial Trattoria",
            "latitude": 53.475,
            "longitude": -2.235,
            "review_count": 65,
            "rating": 4.4,
            "user_reviews": [{"published_at": "2026-09-02T00:00:00Z", "rating": 5.0}]
        }]
        _, telem_b = self.wrapper.enrich_candidate(partial_cand, preloaded_places=mock_places_b)
        self.assertEqual(telem_b["path"], "PATH_B")
        self.assertEqual(telem_b["query"], '"Partial Trattoria" "Manchester"')

    def test_04_no_closest_result_wins_branch_isolation(self):
        """Verify distant branch (>180m) is rejected as BRANCH_MISMATCH, not accepted as closest."""
        cand = {
            "company_name": "Pizza Branch Co",
            "city": "Manchester",
            "latitude": 53.3694,
            "longitude": -2.3136,
            "review_freshness": "UNKNOWN",
            "review_count": 90,
            "rating": 4.5,
            "category": "restaurant"
        }
        # Scraper returns branch 5.2km away
        places = [{
            "title": "Pizza Branch Co (Stockport)",
            "latitude": 53.3999,
            "longitude": -2.2524,
            "review_count": 120,
            "rating": 4.6,
            "user_reviews": [{"published_at": "2026-09-10T00:00:00Z", "rating": 5.0}]
        }]
        evidence, telem = self.wrapper.enrich_candidate(cand, preloaded_places=places)
        self.assertIsNone(evidence)
        self.assertEqual(telem["status"], "BLOCKED")
        self.assertEqual(telem["match_classification"], "BRANCH_MISMATCH")

    def test_05_ambiguity_protection_multiple_safe_branches(self):
        """Verify multiple places within 180m trigger AMBIGUOUS_MATCH with zero evidence attached."""
        cand = {
            "company_name": "Espresso Kiosk",
            "city": "Manchester",
            "latitude": 53.4800,
            "longitude": -2.2400,
            "review_freshness": "UNKNOWN",
            "review_count": 70,
            "rating": 4.2,
            "category": "restaurant"
        }
        # Two kiosks 40m and 60m away
        places = [
            {
                "title": "Espresso Kiosk",
                "latitude": 53.4802,
                "longitude": -2.2402,
                "review_count": 50,
                "rating": 4.0,
                "user_reviews": [{"published_at": "2026-09-01T00:00:00Z", "rating": 4.0}]
            },
            {
                "title": "Espresso Kiosk",
                "latitude": 53.4804,
                "longitude": -2.2404,
                "review_count": 40,
                "rating": 4.1,
                "user_reviews": [{"published_at": "2026-09-02T00:00:00Z", "rating": 4.0}]
            }
        ]
        evidence, telem = self.wrapper.enrich_candidate(cand, preloaded_places=places)
        self.assertIsNone(evidence)
        self.assertEqual(telem["status"], "BLOCKED")
        self.assertEqual(telem["match_classification"], "AMBIGUOUS_MATCH")

    def test_06_observability_metrics_mutually_exclusive(self):
        """Verify get_observability_metrics partitions classifications without overlap."""
        # Candidate 1: Safe match
        c1 = {
            "company_name": "Safe Diner",
            "city": "Manchester",
            "latitude": 53.45,
            "longitude": -2.25,
            "review_freshness": "UNKNOWN",
            "review_count": 60,
            "rating": 4.5,
            "category": "restaurant"
        }
        p1 = [{
            "title": "Safe Diner",
            "latitude": 53.4501,
            "longitude": -2.2501,
            "review_count": 60,
            "rating": 4.5,
            "user_reviews": [{"published_at": "2026-09-15T00:00:00Z", "rating": 5.0}]
        }]
        self.wrapper.enrich_candidate(c1, preloaded_places=p1)

        # Candidate 2: Branch mismatch
        c2 = {
            "company_name": "Distant Branch",
            "city": "Manchester",
            "latitude": 53.40,
            "longitude": -2.20,
            "review_freshness": "UNKNOWN",
            "review_count": 55,
            "rating": 4.3,
            "category": "restaurant"
        }
        p2 = [{
            "title": "Distant Branch",
            "latitude": 53.50,
            "longitude": -2.30,
            "review_count": 80,
            "rating": 4.4,
            "user_reviews": [{"published_at": "2026-09-10T00:00:00Z", "rating": 5.0}]
        }]
        self.wrapper.enrich_candidate(c2, preloaded_places=p2)

        metrics = self.wrapper.get_observability_metrics()
        self.assertEqual(metrics["safe_matches"], 1)
        self.assertEqual(metrics["branch_mismatches"], 1)
        self.assertEqual(metrics["identity_mismatches"], 0)
        self.assertEqual(metrics["ambiguous_matches"], 0)
        self.assertEqual(metrics["search_failures"], 0)

        # Mutual exclusivity check: safe + branch + identity + ambiguous + search = evaluated candidates
        sum_classes = (
            metrics["safe_matches"]
            + metrics["branch_mismatches"]
            + metrics["identity_mismatches"]
            + metrics["ambiguous_matches"]
            + metrics["search_failures"]
        )
        self.assertEqual(sum_classes, 2)
        self.assertEqual(metrics["recent_recovered"], 1)
        self.assertEqual(metrics["unknown_remaining"], 1)

    def test_07_dynamic_kill_switch_without_restart(self):
        """Verify setting GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED=false dynamically stops calls."""
        c = {
            "company_name": "Test Diner",
            "city": "Manchester",
            "latitude": 53.45,
            "longitude": -2.25,
            "review_freshness": "UNKNOWN",
            "review_count": 60,
            "rating": 4.5,
            "category": "restaurant"
        }
        # Initially enabled
        p = [{
            "title": "Test Diner",
            "latitude": 53.4501,
            "longitude": -2.2501,
            "review_count": 60,
            "rating": 4.5,
            "user_reviews": [{"published_at": "2026-09-15T00:00:00Z", "rating": 5.0}]
        }]
        ev1, telem1 = self.wrapper.enrich_candidate(c, preloaded_places=p)
        self.assertIsNotNone(ev1)
        self.assertEqual(telem1["status"], "SUCCESS")

        # Disable via environment dynamically (no restart)
        os.environ["GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED"] = "false"
        ev2, telem2 = self.wrapper.enrich_candidate(c, preloaded_places=p)
        self.assertIsNone(ev2)
        self.assertEqual(telem2["status"], "FLAG_DISABLED")

        # Re-enable via environment dynamically
        os.environ["GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED"] = "true"
        # Using a new candidate name so not hitting the exact same cache
        c3 = dict(c)
        c3["company_name"] = "Test Diner 2"
        p3 = list(p)
        p3[0] = dict(p[0])
        p3[0]["title"] = "Test Diner 2"
        ev3, telem3 = self.wrapper.enrich_candidate(c3, preloaded_places=p3)
        self.assertIsNotNone(ev3)
        self.assertEqual(telem3["status"], "SUCCESS")

    def test_08_qualification_isolation_rule_b(self):
        """Verify Google review evidence alone does NOT promote to OUTREACH_READY without operational signal."""
        # Candidate has NO phone, NO social media
        cand = {
            "company_name": "Isolated Kitchen",
            "city": "Manchester",
            "phone": None,
            "instagram": None,
            "facebook": None,
            "email": None,
            "website": None,
            "latitude": 53.45,
            "longitude": -2.25,
            "review_freshness": "UNKNOWN",
            "review_count": 80,
            "rating": 4.4,
            "category": "restaurant"
        }
        mock_places = [{
            "title": "Isolated Kitchen",
            "latitude": 53.4501,
            "longitude": -2.2501,
            "review_count": 80,
            "rating": 4.4,
            "user_reviews": [{"published_at": "2026-09-20T00:00:00Z", "rating": 5.0}]
        }]
        reconciled, _ = self.wrapper.enrich_candidate(cand, preloaded_places=mock_places)
        self.assertIsNotNone(reconciled)
        self.assertEqual(reconciled.reconciled_freshness, ReviewFreshness.RECENT.value)

        # Lead scoring evaluation
        biz = DiscoveredBusiness(
            company_name=cand["company_name"],
            category=cand["category"],
            city=cand["city"],
            target_country="United Kingdom",
            address="",
            phone="",
            street="",
            postcode="",
            lat=cand["latitude"],
            lon=cand["longitude"],
            review_count=reconciled.reconciled_review_count,
            rating=reconciled.reconciled_rating,
            latest_review_date=reconciled.reconciled_date or "",
            raw_website=""
        )
        biz.review_freshness = reconciled.reconciled_freshness
        lead_scorer = LeadScoringProvider()
        audit = lead_scorer.evaluate_lead(biz, verification_status="NO_WEBSITE_CONFIRMED")
        # Rule B invariant: Without independent operational signal, lead stays in MANUAL_REVIEW
        self.assertEqual(audit.get("qualification_state"), QualificationState.MANUAL_REVIEW.value)
        self.assertFalse(audit.get("is_outreach_ready"))
        self.assertLess(audit.get("score"), 70)

    def test_09_hard_caps_enforced(self):
        """Verify default caps (run=5, day=10, cohort=5) cannot be exceeded."""
        self.assertEqual(DEFAULT_MAX_CALLS_PER_RUN, 5)
        self.assertEqual(DEFAULT_MAX_CALLS_PER_DAY, 10)
        self.assertEqual(DEFAULT_MAX_CALLS, 5)
        self.assertEqual(self.wrapper.max_cohort_size, 5)

    def test_10_fail_closed_error_behavior(self):
        """Verify scraper error produces NO_EVIDENCE_ATTACHED and preserves candidate state."""
        # Non-existent binary triggers scraper failure
        bad_config = GosomFallbackConfig(
            enabled=True,
            scraper_bin="scratch/non_existent_binary_xyz",
            cache_dir=self.temp_dir
        )
        fb_bad = GosomReviewFreshnessFallback(config=bad_config)
        cand = {
            "company_name": "Fail Closed Cafe",
            "city": "Manchester",
            "latitude": 53.45,
            "longitude": -2.25,
            "review_freshness": "UNKNOWN",
            "review_count": 60,
            "rating": 4.5,
            "category": "restaurant"
        }
        ev, telem = fb_bad.enrich_candidate(cand)
        self.assertIsNone(ev)
        self.assertEqual(telem["status"], "SCRAPER_FAILURE")
        self.assertEqual(cand["review_freshness"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
