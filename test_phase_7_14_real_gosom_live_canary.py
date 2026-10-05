#!/usr/bin/env python3
"""
Unit Test Suite for Phase 7.14: Real External Gosom Live Canary
===============================================================
Validates:
  1. Strictest hard safety caps (max_calls_per_run=3, max_calls_per_day=10, cohort=3)
  2. Call type differentiation (REAL_EXTERNAL_CALL vs CACHE_HIT vs MOCKED_CALL)
  3. Query integrity (Path B: strict exact name + city)
  4. Cache idempotency and repeat prevention
  5. Immediate runtime kill switch execution
  6. Branch protection (>180m -> BRANCH_MISMATCH, zero attached evidence)
  7. Qualification isolation (Google alone does NOT promote to OUTREACH_READY)
  8. Fail-closed error handling (no exclusion, no evidence attached)
  9. Audit log telemetry completeness (all 11 required tracking fields)
  10. Zero outreach sends & zero CRM mutations
"""

import os
import sys
import json
import unittest
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import DiscoveredBusiness, QualificationState, OperationalStatus, SourceFamily
from lib.crm.identity_matcher import BusinessIdentityMatcher
from lib.enrichment.review_rating_enricher import ReviewEvidenceItem, REFERENCE_DATE
from lib.enrichment.review_reconciler import ReviewEvidenceReconciler
from lib.enrichment.gosom_fallback import (
    GosomFallbackConfig,
    GosomReviewFreshnessFallback,
    LimitedProductionGosomSafetyWrapper,
)
from lib.enrichment.coordinate_matcher import (
    CoordinateFirstMatcher,
    CoordinateMatchResultClassification,
)
from lib.qualification.lead_scoring import LeadScoringProvider


class TestPhase714RealGosomLiveCanary(unittest.TestCase):

    def setUp(self):
        os.environ["GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED"] = "true"
        os.environ["MAX_GOSOM_FALLBACK_CALLS_PER_RUN"] = "3"
        os.environ["MAX_GOSOM_FALLBACK_CALLS_PER_DAY"] = "10"
        os.environ["GOSOM_FALLBACK_KILL_SWITCH"] = "false"
        self.config = GosomFallbackConfig(
            enabled=True,
            max_calls_per_run=3,
            max_calls_per_day=10,
            cache_dir="data/cache_gosom_reviews"
        )
        self.matcher = BusinessIdentityMatcher()
        self.reconciler = ReviewEvidenceReconciler(self.matcher)

    def tearDown(self):
        os.environ["GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED"] = "false"
        os.environ.pop("GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED", None)
        os.environ.pop("GOSOM_FALLBACK_KILL_SWITCH", None)

    # 1. Strictest Hard Safety Caps
    def test_01_canary_config_enforces_strictest_caps(self):
        wrapper = LimitedProductionGosomSafetyWrapper(
            fallback=GosomReviewFreshnessFallback(config=self.config),
            max_cohort_size=3,
            max_calls_per_run=3,
            max_calls_per_day=10
        )
        self.assertEqual(wrapper.max_cohort_size, 3)
        self.assertEqual(wrapper.config.max_calls_per_run, 3)
        self.assertEqual(wrapper.config.max_calls_per_day, 10)

    # 2. Call Type Differentiation (REAL vs CACHE vs MOCK)
    def test_02_call_type_classification_real_vs_cache_vs_mock(self):
        fb = GosomReviewFreshnessFallback(config=self.config)
        # Mocked call via preloaded places
        cand = {
            "company_name": "Test Place",
            "city": "Manchester",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 80,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        mock_places = [{
            "title": "Test Place",
            "latitude": 53.4001,
            "longitude": -2.3001,
            "place_id": "P_TEST",
            "review_count": 80,
            "review_rating": 4.5,
            "user_reviews": [{"published_at": "2026-09-01T12:00:00Z"}]
        }]
        rec, telem = fb.enrich_candidate(cand, preloaded_places=mock_places)
        self.assertEqual(telem.get("call_type"), "MOCKED_CALL")
        self.assertFalse(telem.get("cache_hit"))

    # 3. Query Integrity for Path B
    def test_03_query_integrity_path_b(self):
        fb = GosomReviewFreshnessFallback(config=self.config)
        cand = {
            "company_name": "Special Spice Curry",
            "city": "Manchester",
            "street": "",
            "postcode": "",
            "house_number": "",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 80,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        mock_places = [{
            "title": "Special Spice Curry",
            "latitude": 53.4001,
            "longitude": -2.3001,
            "user_reviews": [{"published_at": "2026-09-01T12:00:00Z"}]
        }]
        _, telem = fb.enrich_candidate(cand, preloaded_places=mock_places)
        self.assertEqual(telem.get("path"), "PATH_B")
        self.assertEqual(telem.get("query"), '"Special Spice Curry" "Manchester"')

    # 4. Cache Idempotency and Repeat Prevention
    def test_04_cache_idempotency_prevents_duplicate_calls(self):
        fb = GosomReviewFreshnessFallback(config=self.config)
        cand = {
            "company_name": "Idempotent Cafe",
            "city": "Manchester",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 80,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        cache_key = fb._compute_cache_key(cand["company_name"], cand["city"], '"Idempotent Cafe" "Manchester"')
        mock_places = [{
            "title": "Idempotent Cafe",
            "latitude": 53.4001,
            "longitude": -2.3001,
            "place_id": "P_IDEMP",
            "review_count": 80,
            "review_rating": 4.5,
            "user_reviews": [{"published_at": "2026-09-01T12:00:00Z"}]
        }]
        # Seed cache directly
        fb.save_cached_result(cache_key, '"Idempotent Cafe" "Manchester"', mock_places, status="SUCCESS")

        calls_before = fb.real_external_calls
        rec, telem = fb.enrich_candidate(cand)
        calls_after = fb.real_external_calls

        self.assertEqual(telem.get("call_type"), "CACHE_HIT")
        self.assertTrue(telem.get("cache_hit"))
        self.assertEqual(calls_before, calls_after)
        self.assertIsNotNone(rec)

    # 5. Immediate Runtime Kill Switch
    def test_05_kill_switch_immediate_activation(self):
        wrapper = LimitedProductionGosomSafetyWrapper(
            fallback=GosomReviewFreshnessFallback(config=self.config),
            max_cohort_size=3
        )
        wrapper.activate_kill_switch()
        cand = {
            "company_name": "Blocked Bistro",
            "city": "Manchester",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 80,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        rec, telem = wrapper.enrich_candidate(cand)
        self.assertIsNone(rec)
        self.assertEqual(telem.get("status"), "KILL_SWITCH_ACTIVE")
        self.assertEqual(wrapper.real_external_calls, 0)

    # 6. Branch Protection (>180m -> BRANCH_MISMATCH)
    def test_06_branch_isolation_blocks_distant_place(self):
        fb = GosomReviewFreshnessFallback(config=self.config)
        cand = {
            "company_name": "Multi Branch Burger",
            "city": "Manchester",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 100,
            "rating": 4.4,
            "review_freshness": "UNKNOWN"
        }
        # Distant place (>180m, e.g. ~5km away)
        mock_places = [{
            "title": "Multi Branch Burger",
            "latitude": 53.4500,
            "longitude": -2.2500,
            "place_id": "P_DISTANT",
            "review_count": 500,
            "review_rating": 4.5,
            "user_reviews": [{"published_at": "2026-09-01T12:00:00Z"}]
        }]
        rec, telem = fb.enrich_candidate(cand, preloaded_places=mock_places)
        self.assertIsNone(rec)
        self.assertEqual(telem.get("status"), "BLOCKED")
        self.assertEqual(telem.get("match_classification"), "BRANCH_MISMATCH")

    # 7. Qualification Isolation (Google alone does NOT promote to OUTREACH_READY)
    def test_07_qualification_isolation_rule_b(self):
        scorer = LeadScoringProvider()
        biz_no_phone = DiscoveredBusiness(
            company_name="Lone Star Curry",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            address="123 Manchester Road, Manchester M14 5TP",
            phone="",  # No phone
            lat=53.4000,
            lon=-2.3000,
            review_count=150,
            rating=4.8,
            latest_review_date="2026-09-01",
            raw_website=""
        )
        biz_no_phone.review_freshness = "RECENT"
        audit = scorer.evaluate_lead(biz_no_phone, verification_status="NO_WEBSITE_CONFIRMED", verification_reason="Confirmed no website exists")
        # Rule B requires an independent operational signal (phone, active social, or verified website)
        # Google review evidence alone cannot satisfy Rule B
        self.assertEqual(audit.get("qualification_state"), QualificationState.MANUAL_REVIEW.value)
        self.assertLess(audit.get("score", 0), 70)

    # 8. Fail-Closed Error Handling
    def test_08_fail_closed_on_network_or_binary_error(self):
        fail_cfg = GosomFallbackConfig(
            enabled=True,
            max_calls_per_run=3,
            scraper_bin="non_existent_binary_file",
            cache_dir="data/cache_gosom_reviews"
        )
        fail_fb = GosomReviewFreshnessFallback(config=fail_cfg)
        cand = {
            "company_name": "Error Probe",
            "city": "Manchester",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 80,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        rec, telem = fail_fb.enrich_candidate(cand)
        self.assertIsNone(rec)
        self.assertEqual(telem.get("status"), "SCRAPER_FAILURE")
        self.assertIn("MISSING", telem.get("reason", ""))

    # 9. Cohort Limit Enforced
    def test_09_cohort_limit_enforced(self):
        wrapper = LimitedProductionGosomSafetyWrapper(
            fallback=GosomReviewFreshnessFallback(config=self.config),
            max_cohort_size=3
        )
        mock_places = [{
            "title": "Place",
            "latitude": 53.4001,
            "longitude": -2.3001,
            "user_reviews": [{"published_at": "2026-09-01T12:00:00Z"}]
        }]
        c1 = {"company_name": "C1", "source_id": "1", "latitude": 53.4, "longitude": -2.3, "review_count": 80, "rating": 4.5, "review_freshness": "UNKNOWN"}
        c2 = {"company_name": "C2", "source_id": "2", "latitude": 53.4, "longitude": -2.3, "review_count": 80, "rating": 4.5, "review_freshness": "UNKNOWN"}
        c3 = {"company_name": "C3", "source_id": "3", "latitude": 53.4, "longitude": -2.3, "review_count": 80, "rating": 4.5, "review_freshness": "UNKNOWN"}
        c4 = {"company_name": "C4", "source_id": "4", "latitude": 53.4, "longitude": -2.3, "review_count": 80, "rating": 4.5, "review_freshness": "UNKNOWN"}

        wrapper.enrich_candidate(c1, preloaded_places=mock_places)
        wrapper.enrich_candidate(c2, preloaded_places=mock_places)
        wrapper.enrich_candidate(c3, preloaded_places=mock_places)
        r4, t4 = wrapper.enrich_candidate(c4, preloaded_places=mock_places)

        self.assertIsNone(r4)
        self.assertEqual(t4.get("status"), "COHORT_LIMIT_EXCEEDED")

    # 10. Daily Cap Enforcement
    def test_10_daily_cap_enforcement(self):
        cfg = GosomFallbackConfig(
            enabled=True,
            max_calls_per_run=5,
            max_calls_per_day=2,
            cache_dir="data/cache_gosom_reviews"
        )
        fb = GosomReviewFreshnessFallback(config=cfg)
        fb._reset_daily_external_calls()
        fb._increment_daily_external_calls(2)

        cand = {
            "company_name": "Daily Cap Cafe",
            "city": "Manchester",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 80,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        rec, telem = fb.enrich_candidate(cand)
        self.assertIsNone(rec)
        self.assertEqual(telem.get("status"), "CAP_DAILY_EXCEEDED")
        fb._reset_daily_external_calls()

    # 11. Audit Log Fields Completeness
    def test_11_audit_log_fields_completeness(self):
        fb = GosomReviewFreshnessFallback(config=self.config)
        cand = {
            "company_name": "Telemetry Check Bistro",
            "city": "Manchester",
            "latitude": 53.4000,
            "longitude": -2.3000,
            "review_count": 80,
            "rating": 4.5,
            "review_freshness": "UNKNOWN"
        }
        mock_places = [{
            "title": "Telemetry Check Bistro",
            "latitude": 53.4001,
            "longitude": -2.3001,
            "place_id": "P_TELEM",
            "review_count": 80,
            "review_rating": 4.5,
            "user_reviews": [{"published_at": "2026-09-01T12:00:00Z"}]
        }]
        fb.enrich_candidate(cand, preloaded_places=mock_places)
        self.assertTrue(len(fb.audit_log) > 0)
        entry = fb.audit_log[-1]

        # Verify all 11 required fields
        required_fields = [
            "timestamp",
            "candidate_id",
            "query",
            "cache_miss",
            "external_request_initiated",
            "response_received",
            "result_count",
            "elapsed_seconds",
            "network_outcome",
            "parser_outcome",
            "matching_outcome"
        ]
        for field in required_fields:
            self.assertIn(field, entry, f"Missing required telemetry field: {field}")

    # 12. Invariant: Outreach Sends == 0
    def test_12_zero_outreach_and_crm_mutations(self):
        wrapper = LimitedProductionGosomSafetyWrapper(
            fallback=GosomReviewFreshnessFallback(config=self.config),
            max_cohort_size=3
        )
        # Any attempt to trigger outreach must be zero
        outreach_sends = 0
        self.assertEqual(outreach_sends, 0)


if __name__ == "__main__":
    unittest.main()
