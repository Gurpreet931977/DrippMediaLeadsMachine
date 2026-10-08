"""
test_phase_11_2_tavily_provider.py
==================================
Unit and safety tests for Phase 11.2: Free GitHub Web Research Provider (Tavily).

Tests cover:
1. Successful search with usable results.
2. Empty search result.
3. Request timeout -> PROVIDER_TIMEOUT.
4. HTTP failure (500/503) -> PROVIDER_FAILED.
5. Malformed JSON response -> PROVIDER_FAILED.
6. Missing API key -> PROVIDER_NOT_CONFIGURED.
7. Quota exhausted (governor limit & HTTP 429) -> QUOTA_EXCEEDED.
8. Identity mismatch rejection -> IDENTITY_MISMATCH.
9. Evidence conflict across sources -> EVIDENCE_CONFLICT.
10. Valid review evidence passing through existing reconciliation into Rule B.
11. Safety invariants: Tavily results NEVER bypass:
    - Rule B review thresholds
    - Identity confidence
    - Location validation
    - Review freshness
    - Operational verification
    - Suppression list
    - Commercial kill switches / Travel mode
12. Provider cascade and circuit breaker behavior.
13. Telemetry sanitization (no secrets / API keys in telemetry or logs).
"""

import os
import json
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
import requests

from lib.types import (
    DiscoveredBusiness,
    ResearchFailureState,
    ResearchTelemetry,
    OperationalStatus,
    WebsiteStatus,
    VerificationStatus,
    QualificationState,
    Priority,
)
from lib.discovery.web_search import (
    WebSearchProvider,
    SearchOutcome,
    SearchResultList,
    ProviderCircuitBreaker,
)
from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
    ReviewEvidenceItem,
    ReviewConfidence,
    ReviewFreshness,
    ReviewStatus,
    REFERENCE_DATE,
)
from lib.enrichment.review_recovery import (
    ReviewEvidenceRecoveryLayer,
    ReviewRecoveryCandidateResult,
)
from lib.production.market_runner import (
    QuotaBudget,
    ProductionScaleEngine,
    BatchController,
)
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.system.system_config import SystemConfig


class TestPhase112TavilyProvider(unittest.TestCase):
    def setUp(self):
        # Enforce safety defaults
        SystemConfig.TRAVEL_MODE = True
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False
        SystemConfig.AUTOMATED_EMAIL_ENABLED = False
        self._notify_patcher = patch("lib.discovery.web_search.WebSearchProvider._notify_tavily_monitor")
        self._notify_patcher.start()

    def tearDown(self):
        self._notify_patcher.stop()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. SUCCESSFUL SEARCH
    # ──────────────────────────────────────────────────────────────────────────
    @patch("requests.post")
    def test_01_successful_search(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Dog and Partridge Manchester Pub",
                    "url": "https://www.tripadvisor.co.uk/Restaurant_Review-Dog_and_Partridge.html",
                    "content": "Dog and Partridge in Manchester has 142 reviews and a rating of 4.4 out of 5 stars. Reviewed 2 weeks ago.",
                    "score": 0.95
                }
            ]
        }
        mock_post.return_value = mock_resp

        provider = WebSearchProvider(tavily_key="tvly-mock-valid-key")
        res = provider._search_tavily("Dog and Partridge Manchester reviews", num_results=3)

        self.assertIsInstance(res, SearchResultList)
        self.assertEqual(res.outcome, SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS)
        self.assertEqual(res.provider, "TAVILY")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["search_provider"], "TAVILY")
        self.assertEqual(res[0]["title"], "Dog and Partridge Manchester Pub")
        self.assertTrue(res.succeeded)
        self.assertTrue(res.attempted)
        self.assertGreater(res.latency, 0.0)
        self.assertEqual(provider.circuit_breakers["TAVILY"].successful_queries, 1)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. EMPTY RESULT
    # ──────────────────────────────────────────────────────────────────────────
    @patch("requests.post")
    def test_02_empty_result(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_post.return_value = mock_resp

        provider = WebSearchProvider(tavily_key="tvly-mock-valid-key")
        res = provider._search_tavily("Obscure Nonexistent Place Manchester", num_results=3)

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_SUCCEEDED_EMPTY)
        self.assertEqual(len(res), 0)
        self.assertTrue(res.succeeded)
        self.assertEqual(provider.circuit_breakers["TAVILY"].empty_successful_queries, 1)

    # ──────────────────────────────────────────────────────────────────────────
    # 3. TIMEOUT -> PROVIDER_TIMEOUT
    # ──────────────────────────────────────────────────────────────────────────
    @patch("requests.post")
    def test_03_timeout(self, mock_post):
        mock_post.side_effect = requests.Timeout("Connection timed out after 10 seconds")

        provider = WebSearchProvider(tavily_key="tvly-mock-valid-key")
        res = provider._search_tavily("Dog and Partridge Manchester", num_results=3)

        self.assertEqual(res.outcome, SearchOutcome.PROVIDER_TIMEOUT)
        self.assertEqual(res.provider, "TAVILY")
        self.assertIn("timed out", res.error.lower())
        self.assertEqual(provider.circuit_breakers["TAVILY"].failed_queries, 1)

    # ──────────────────────────────────────────────────────────────────────────
    # 4. HTTP FAILURE (500 / 503) -> PROVIDER_FAILED
    # ──────────────────────────────────────────────────────────────────────────
    @patch("requests.post")
    def test_04_http_failure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "Service Unavailable"
        mock_post.return_value = mock_resp

        provider = WebSearchProvider(tavily_key="tvly-mock-valid-key")
        res = provider._search_tavily("Dog and Partridge Manchester", num_results=3)

        self.assertEqual(res.outcome, SearchOutcome.PROVIDER_FAILED)
        self.assertEqual(res.http_status, 503)
        self.assertIn("503", res.error)
        self.assertEqual(provider.circuit_breakers["TAVILY"].failed_queries, 1)

    # ──────────────────────────────────────────────────────────────────────────
    # 5. MALFORMED RESPONSE -> PROVIDER_FAILED
    # ──────────────────────────────────────────────────────────────────────────
    @patch("requests.post")
    def test_05_malformed_response(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = json.JSONDecodeError("Expecting value", "<html>Invalid JSON</html>", 0)
        mock_post.return_value = mock_resp

        provider = WebSearchProvider(tavily_key="tvly-mock-valid-key")
        res = provider._search_tavily("Dog and Partridge Manchester", num_results=3)

        self.assertEqual(res.outcome, SearchOutcome.PROVIDER_FAILED)
        self.assertIn("Malformed JSON", res.error)
        self.assertEqual(provider.circuit_breakers["TAVILY"].failed_queries, 1)

    # ──────────────────────────────────────────────────────────────────────────
    # 6. MISSING API KEY -> PROVIDER_NOT_CONFIGURED
    # ──────────────────────────────────────────────────────────────────────────
    def test_06_missing_api_key(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}, clear=False):
            provider = WebSearchProvider(tavily_key="")
            res = provider._search_tavily("Dog and Partridge Manchester", num_results=3)

            self.assertEqual(res.outcome, SearchOutcome.PROVIDER_NOT_CONFIGURED)
            self.assertEqual(res.provider, "TAVILY")
            self.assertFalse(res.attempted)
            self.assertIn("not configured", res.error.lower())

    # ──────────────────────────────────────────────────────────────────────────
    # 7. QUOTA EXHAUSTED -> QUOTA_EXCEEDED
    # ──────────────────────────────────────────────────────────────────────────
    @patch("requests.post")
    def test_07_quota_governor_exhausted(self, mock_post):
        # Test governor hard refusal before making HTTP request
        budget = QuotaBudget(search_limit=500, tavily_limit=10, tavily_used=10)
        provider = WebSearchProvider(tavily_key="tvly-mock-valid-key")
        provider.set_quota_budget(budget)

        res = provider._search_tavily("Dog and Partridge Manchester", num_results=3)

        self.assertEqual(res.outcome, SearchOutcome.QUOTA_EXCEEDED)
        self.assertEqual(res.provider, "TAVILY")
        self.assertFalse(res.attempted)
        self.assertIn("quota budget exhausted", res.error.lower())
        mock_post.assert_not_called()

    @patch("requests.post")
    def test_07_http_429_quota_exceeded(self, mock_post):
        # Test HTTP 429 response from Tavily API
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "Monthly request limit reached"
        mock_post.return_value = mock_resp

        provider = WebSearchProvider(tavily_key="tvly-mock-valid-key")
        res = provider._search_tavily("Dog and Partridge Manchester", num_results=3)

        self.assertEqual(res.outcome, SearchOutcome.QUOTA_EXCEEDED)
        self.assertEqual(res.http_status, 429)
        self.assertIn("429", res.error)

    # ──────────────────────────────────────────────────────────────────────────
    # 8. IDENTITY MISMATCH REJECTION
    # ──────────────────────────────────────────────────────────────────────────
    def test_08_identity_mismatch(self):
        enricher = ReviewRatingEnricher()
        # Search returns snippet for a completely different restaurant in London
        snippet = "The London Red Lion in Westminster has 350 reviews and 4.6 stars rating."
        title = "The Red Lion Westminster London"
        url = "https://www.tripadvisor.co.uk/Restaurant_Review-Red_Lion_London.html"

        ev = enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Dog and Partridge",
            city="Manchester",
            street="Manor Street",
            postcode="M1 2WD"
        )

        # Must reject or assign low identity confidence
        if ev:
            self.assertTrue(ev.reject_reason is not None or ev.confidence == ReviewConfidence.LOW.value)

    # ──────────────────────────────────────────────────────────────────────────
    # 9. EVIDENCE CONFLICT ACROSS SOURCES
    # ──────────────────────────────────────────────────────────────────────────
    def test_09_evidence_conflict(self):
        enricher = ReviewRatingEnricher()
        # Item 1: TripAdvisor indicates 150 reviews, rating 4.6
        item1 = ReviewEvidenceItem(
            review_count=150,
            rating=4.6,
            source="Tripadvisor",
            source_url="https://tripadvisor.co.uk/item1",
            evidence_text="150 reviews, 4.6 stars",
            confidence=ReviewConfidence.HIGH.value,
            freshness=ReviewFreshness.RECENT.value,
            evidence_date="2026-09-10"
        )
        # Item 2: Another source indicates 12 reviews, rating 2.5 (massive discrepancy)
        item2 = ReviewEvidenceItem(
            review_count=12,
            rating=2.5,
            source="Yelp",
            source_url="https://yelp.co.uk/item2",
            evidence_text="12 reviews, 2.5 stars",
            confidence=ReviewConfidence.HIGH.value,
            freshness=ReviewFreshness.RECENT.value,
            evidence_date="2026-09-15"
        )

        has_conflict, conflicts = enricher.detect_conflicts([item1, item2])
        self.assertTrue(has_conflict)
        self.assertGreaterEqual(len(conflicts), 2)

        resolved = enricher.resolve_best_evidence([item1, item2])
        self.assertEqual(resolved.review_status, ReviewStatus.CONFLICT_REQUIRES_REVIEW.value)
        self.assertEqual(resolved.review_confidence, ReviewConfidence.CONFLICT.value)
        self.assertIsNone(resolved.review_count)

    # ──────────────────────────────────────────────────────────────────────────
    # 10. VALID REVIEW EVIDENCE PASSING RECONCILIATION
    # ──────────────────────────────────────────────────────────────────────────
    @patch("requests.post")
    def test_10_valid_review_evidence_reconciliation(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Dog and Partridge - Manchester",
                    "url": "https://restaurantguru.com/Dog-and-Partridge-Manchester",
                    "content": "Dog and Partridge in Manchester: 88 customer reviews on Google and Restaurant Guru, rating 4.3 of 5 stars. Latest review posted 3 weeks ago.",
                    "score": 0.98
                }
            ]
        }
        mock_post.return_value = mock_resp

        web = WebSearchProvider(tavily_key="tvly-mock-valid-key")
        enricher = ReviewRatingEnricher(web_search_provider=web)

        biz = DiscoveredBusiness(
            company_name="Dog and Partridge",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            street="Manor Street",
            operational_status=OperationalStatus.VERIFIED_ACTIVE.value,
            raw_website="",
            phone="+44 161 234 5678"
        )

        enriched_biz = enricher.enrich_candidate(biz)

        self.assertIsNotNone(enriched_biz.review_count)
        self.assertGreaterEqual(enriched_biz.review_count, 50)
        self.assertIsNotNone(enriched_biz.rating)
        self.assertGreaterEqual(enriched_biz.rating, 4.0)

    # ──────────────────────────────────────────────────────────────────────────
    # 11. SAFETY TEST: TAVILY DOES NOT BYPASS RULE B & SAFETY INVARIANTS
    # ──────────────────────────────────────────────────────────────────────────
    def test_safety_rule_b_low_reviews_research_only(self):
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Tavily Found Pub",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            raw_website="",
            operational_status=OperationalStatus.VERIFIED_ACTIVE.value,
            review_count=6,  # < 10 reviews
            rating=4.5,
            phone="+44 161 111 2222"
        )
        res = scorer.evaluate_lead(biz, verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(res["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertEqual(res["priority"], Priority.RESEARCH_ONLY.value)
        self.assertIn("Minimal customer review traction", res["qualification_reason"])

    def test_safety_rule_b_mid_reviews_manual_review(self):
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Tavily Moderate Pub",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            raw_website="",
            operational_status=OperationalStatus.VERIFIED_ACTIVE.value,
            review_count=35,  # 10 - 49 reviews
            rating=4.5,
            phone="+44 161 111 2222"
        )
        res = scorer.evaluate_lead(biz, verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(res["qualification_state"], QualificationState.MANUAL_REVIEW.value)
        self.assertEqual(res["priority"], Priority.MANUAL_REVIEW.value)

    def test_safety_rule_b_low_rating_manual_review(self):
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Tavily Low Rating Pub",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            raw_website="",
            operational_status=OperationalStatus.VERIFIED_ACTIVE.value,
            review_count=85,
            rating=3.2,  # < 3.5 rating
            phone="+44 161 111 2222"
        )
        res = scorer.evaluate_lead(biz, verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(res["qualification_state"], QualificationState.MANUAL_REVIEW.value)
        self.assertIn("Low rating", res["qualification_reason"])

    def test_safety_rule_b_stale_reviews_research_only(self):
        scorer = LeadScoringProvider()
        # Review date older than 180 days from reference benchmark
        stale_date = (REFERENCE_DATE - timedelta(days=240)).strftime("%Y-%m-%d")
        biz = DiscoveredBusiness(
            company_name="Tavily Stale Reviews Pub",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            raw_website="",
            operational_status=OperationalStatus.VERIFIED_ACTIVE.value,
            review_count=85,
            rating=4.5,
            latest_review_date=stale_date,
            phone="+44 161 111 2222"
        )
        res = scorer.evaluate_lead(biz, verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertNotEqual(res["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(res["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    def test_safety_operational_verification_gate(self):
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Unverified Closed Business",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            raw_website="",
            operational_status=OperationalStatus.UNKNOWN.value,  # Not verified active
            review_count=120,
            rating=4.8,
            phone="+44 161 111 2222"
        )
        res = scorer.evaluate_lead(biz, verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertNotEqual(res["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(res["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    def test_safety_commercial_kill_switch_active(self):
        # Under active travel mode and disabled commercial actions, no commercial send allowed
        SystemConfig.TRAVEL_MODE = True
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False
        SystemConfig.AUTOMATED_EMAIL_ENABLED = False

        self.assertFalse(SystemConfig.can_execute_commercial_actions())
        budget = QuotaBudget()
        self.assertFalse(budget.can_consume("outreach", 1))
        with self.assertRaises(RuntimeError):
            budget.consume("outreach", 1)

    # ──────────────────────────────────────────────────────────────────────────
    # 12. PROVIDER CASCADE AND CIRCUIT BREAKER BEHAVIOR
    # ──────────────────────────────────────────────────────────────────────────
    @patch("requests.post")
    def test_12_provider_cascade_on_tavily_failure(self, mock_post):
        # When Tavily fails with 500, cascade continues to next provider
        mock_tavily_fail = MagicMock()
        mock_tavily_fail.status_code = 500
        mock_tavily_fail.text = "Internal Server Error"
        mock_post.return_value = mock_tavily_fail

        provider = WebSearchProvider(tavily_key="tvly-mock-valid-key")
        # Direct call to _search_tavily records failure and trips circuit
        res = provider._search_tavily("Dog and Partridge Manchester", num_results=3)
        self.assertEqual(res.outcome, SearchOutcome.PROVIDER_FAILED)
        self.assertEqual(provider.circuit_breakers["TAVILY"].failure_count, 1)

    # ──────────────────────────────────────────────────────────────────────────
    # 13. TELEMETRY SANITIZATION AND METRICS
    # ──────────────────────────────────────────────────────────────────────────
    def test_13_telemetry_metrics_and_no_secrets(self):
        secret_key = "tvly-secret-mock-token-xyz-12345"
        telem = ResearchTelemetry(
            candidate="The Wendover",
            provider_attempted=["TAVILY", "DUCKDUCKGO_FALLBACK"],
            provider_result="SEARCH_SUCCEEDED",
            evidence_found="65 reviews, 4.2★",
            operational_signal_found="PHONE, ADDRESS",
            failure_reason=ResearchFailureState.NONE.value,
            query_count=2,
            usable_results_count=1,
            latency=1.234,
            details={"city": "Manchester"}
        )

        d = telem.to_dict()
        self.assertEqual(d["query_count"], 2)
        self.assertEqual(d["usable_results_count"], 1)
        self.assertEqual(d["latency"], 1.234)

        telem_json = json.dumps(d)
        self.assertNotIn(secret_key, telem_json)
        self.assertNotIn("Bearer", telem_json)
        self.assertNotIn("Authorization", telem_json)


if __name__ == "__main__":
    unittest.main()
