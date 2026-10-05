"""
Phase 2: Local Search Provider (SearXNG) Integration & Telemetry Verification Tests
===================================================================================
Covers all 15 requirements specified in Phase 2:
1. Local SearXNG healthy result.
2. Local SearXNG successful empty result.
3. Local SearXNG unavailable (ConnectionError -> SEARCH_PROVIDER_UNAVAILABLE).
4. SearXNG timeout (Timeout -> SEARCH_TIMEOUT).
5. SearXNG circuit open (bypasses requests, tracks circuit_open_bypasses).
6. SearXNG provider isolation from DDG (failing SearXNG does not trip DDG).
7. Provider fallback SearXNG -> DDG (on SearXNG failure/unavailable, falls back to DDG).
8. Cache hit bypasses provider request (no HTTP call on cached query).
9. SearXNG failure never becomes NOT_FOUND (routes to SEARCH_FAILED in reviewer).
10. SearXNG failure never becomes NO_WEBSITE_CONFIRMED (routes to WEBSITE_UNCLEAR in verifier).
11. Telemetry distinguishes provider attempts from circuit bypasses.
12. Review evidence preserves provider identity (records provider: SEARXNG and engine).
13. Existing DDG tests continue passing.
14. Existing circuit-breaker tests continue passing.
15. Existing location/identity tests continue passing.
"""

import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import requests

from lib.types import (
    DiscoveredBusiness,
    WebsiteStatus,
    VerificationStatus
)
from lib.discovery.web_search import (
    WebSearchProvider,
    ProviderCircuitBreaker,
    CircuitState,
    SearchOutcome,
    SearchResultList
)
from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
    ReviewStatus
)
from lib.verification.no_website_verifier import (
    NoWebsiteVerificationProvider
)
from lib.country_adapters.base import ReviewSourceAdapter
from lib.country_adapters.uk import UKReviewSourceAdapter


class TestLocalSearXNGProvider(unittest.TestCase):
    def setUp(self):
        self.temp_cache = tempfile.mkdtemp()
        self.provider = WebSearchProvider(
            searxng_url="http://localhost:8080",
            cache_dir=self.temp_cache,
            cooldown_seconds=1.0
        )

    def tearDown(self):
        shutil.rmtree(self.temp_cache, ignore_errors=True)

    def test_01_local_searxng_healthy_result(self):
        """1. Local SearXNG healthy result returns SEARCH_SUCCEEDED_WITH_RESULTS."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "url": "https://leeds-diner.co.uk",
                    "title": "Leeds Diner - Yorkshire Dining",
                    "content": "Award winning diner in central Leeds",
                    "engine": "duckduckgo"
                }
            ]
        }

        with patch("requests.get", return_value=mock_resp):
            res = self.provider._search_searxng("Leeds Diner restaurant", num_results=5)

        self.assertIsInstance(res, SearchResultList)
        self.assertEqual(res.outcome, SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS)
        self.assertEqual(res.provider, "SEARXNG")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Leeds Diner - Yorkshire Dining")
        self.assertEqual(res[0]["searxng_engine"], "duckduckgo")
        self.assertTrue(res.succeeded)
        self.assertEqual(self.provider.circuit_breakers["SEARXNG"].status, "CLOSED")

    def test_02_local_searxng_successful_empty_result(self):
        """2. Local SearXNG successful empty result returns SEARCH_SUCCEEDED_EMPTY."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}

        with patch("requests.get", return_value=mock_resp):
            res = self.provider._search_searxng("Nonexistent Place In Leeds", num_results=5)

        self.assertIsInstance(res, SearchResultList)
        self.assertEqual(res.outcome, SearchOutcome.SEARCH_SUCCEEDED_EMPTY)
        self.assertEqual(res.provider, "SEARXNG")
        self.assertEqual(len(res), 0)
        self.assertTrue(res.succeeded)
        self.assertEqual(self.provider.circuit_breakers["SEARXNG"].status, "CLOSED")

    def test_03_local_searxng_unavailable(self):
        """3. Local SearXNG unavailable returns SEARCH_PROVIDER_UNAVAILABLE on ConnectionError."""
        with patch("requests.get", side_effect=requests.ConnectionError("Connection refused to 8080")):
            res = self.provider._search_searxng("Leeds Bistro", num_results=5)

        self.assertIsInstance(res, SearchResultList)
        self.assertEqual(res.outcome, SearchOutcome.SEARCH_PROVIDER_UNAVAILABLE)
        self.assertEqual(res.provider, "SEARXNG")
        self.assertFalse(res.succeeded)
        self.assertEqual(self.provider.circuit_breakers["SEARXNG"].failed_queries, 1)

    def test_04_searxng_timeout(self):
        """4. SearXNG timeout returns SEARCH_TIMEOUT."""
        with patch("requests.get", side_effect=requests.Timeout("Read timed out on 8080")):
            res = self.provider._search_searxng("Leeds Bistro", num_results=5)

        self.assertIsInstance(res, SearchResultList)
        self.assertEqual(res.outcome, SearchOutcome.SEARCH_TIMEOUT)
        self.assertEqual(res.provider, "SEARXNG")
        self.assertFalse(res.succeeded)

    def test_05_searxng_circuit_open(self):
        """5. SearXNG circuit open bypasses calls and records circuit_open_bypasses."""
        cb = self.provider.circuit_breakers["SEARXNG"]
        cb.trip()
        self.assertEqual(cb.status, "OPEN")

        with patch("requests.get") as mock_get:
            res = self.provider._search_searxng("Leeds Bistro", num_results=5)
            mock_get.assert_not_called()

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_CIRCUIT_OPEN)
        self.assertEqual(res.circuit_state, "OPEN")
        self.assertEqual(cb.circuit_open_queries, 1)
        self.assertEqual(cb.circuit_open_bypasses, 1)
        self.assertEqual(cb.provider_attempts, 0)

    def test_06_searxng_provider_isolation_from_ddg(self):
        """6. SearXNG failure does not trip DuckDuckGo circuit breaker."""
        cb_searxng = self.provider.circuit_breakers["SEARXNG"]
        cb_ddg = self.provider.circuit_breakers["DUCKDUCKGO_FALLBACK"]

        # SearXNG fails twice and trips
        with patch("requests.get", side_effect=requests.ConnectionError("Offline")):
            self.provider._search_searxng("Query 1")
            self.provider._search_searxng("Query 2")

        self.assertEqual(cb_searxng.status, "OPEN")
        self.assertEqual(cb_ddg.status, "CLOSED")
        self.assertTrue(cb_ddg.is_available())

    def test_07_provider_fallback_searxng_to_ddg(self):
        """7. Provider fallback SearXNG -> DDG occurs when SearXNG fails."""
        # SearXNG fails with ConnectionError
        # DDG fallback succeeds
        mock_ddg_results = SearchResultList(
            [{"result_url": "https://ddg-result.co.uk", "title": "DDG Result", "snippet": "Leeds diner"}],
            outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS,
            provider="DUCKDUCKGO_FALLBACK",
            query="Leeds Diner"
        )

        with patch.object(self.provider, "_search_searxng", return_value=SearchResultList(
            outcome=SearchOutcome.SEARCH_PROVIDER_UNAVAILABLE,
            provider="SEARXNG",
            query="Leeds Diner",
            error="Connection refused"
        )) as mock_searx, patch.object(self.provider, "_search_duckduckgo_fallback", return_value=mock_ddg_results) as mock_ddg:
            res = self.provider.search_web("Leeds Diner")

        mock_searx.assert_called_once()
        mock_ddg.assert_called_once()
        self.assertEqual(res.provider, "DUCKDUCKGO_FALLBACK")
        self.assertEqual(len(res), 1)
        self.assertEqual(self.provider.circuit_breakers["SEARXNG"].fallback_usage, 1)

    def test_08_cache_hit_bypasses_provider_request(self):
        """8. Cache hit bypasses provider request completely."""
        cached_list = SearchResultList(
            [{"result_url": "https://cached-searxng.com", "title": "Cached", "snippet": "snippet"}],
            outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS,
            provider="SEARXNG",
            query="cached query"
        )
        self.provider._set_cache("web_cached query_5_False", cached_list, provider="SEARXNG")
        self.provider.cache_hits = 0

        with patch("requests.get") as mock_get:
            res = self.provider.search_web("cached query", num_results=5)
            mock_get.assert_not_called()

        self.assertTrue(res.is_cached)
        self.assertEqual(res.provider, "SEARXNG")
        self.assertEqual(self.provider.cache_hits, 1)

    def test_09_searxng_failure_never_becomes_not_found(self):
        """9. SearXNG search failure routes to SEARCH_FAILED, NEVER NOT_FOUND."""
        mock_failed_search = SearchResultList(
            [],
            outcome=SearchOutcome.SEARCH_PROVIDER_UNAVAILABLE,
            provider="SEARXNG",
            error="Connection refused"
        )
        mock_web = MagicMock()
        mock_web.search_web.return_value = mock_failed_search

        enricher = ReviewRatingEnricher(web_search_provider=mock_web)
        res = enricher.enrich_business("Test Eatery", "Leeds", "United Kingdom")

        self.assertNotEqual(res.review_status, ReviewStatus.NOT_FOUND.value)
        self.assertEqual(res.review_status, ReviewStatus.SEARCH_FAILED.value)

    def test_10_searxng_failure_never_becomes_no_website_confirmed(self):
        """10. SearXNG search failure never produces NO_WEBSITE_CONFIRMED."""
        verifier = NoWebsiteVerificationProvider()
        biz = DiscoveredBusiness(
            company_name="Searx Failure Grill",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            raw_website=""
        )
        searx_failure = SearchResultList(
            [],
            outcome=SearchOutcome.SEARCH_TIMEOUT,
            provider="SEARXNG",
            error="Timed out"
        )
        res = verifier.verify_business(biz, cached_search_results=searx_failure)
        self.assertNotEqual(res["website_status"], WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(res["website_status"], WebsiteStatus.WEBSITE_UNCLEAR.value)

    def test_11_telemetry_distinguishes_provider_attempts_from_circuit_bypasses(self):
        """11. Telemetry distinguishes provider attempts from circuit bypasses."""
        cb = self.provider.circuit_breakers["SEARXNG"]
        cb.trip()

        # 3 calls with circuit open
        self.provider._search_searxng("Query 1")
        self.provider._search_searxng("Query 2")
        self.provider._search_searxng("Query 3")

        telem = self.provider.get_telemetry()
        searx_telem = telem["SEARXNG"]

        self.assertEqual(searx_telem["provider_attempts"], 0)
        self.assertEqual(searx_telem["circuit_open_bypasses"], 3)
        self.assertNotEqual(searx_telem["provider_attempts"], searx_telem["circuit_open_bypasses"])

    def test_12_review_evidence_preserves_provider_identity(self):
        """12. Review evidence preserves provider identity and engine metadata."""
        mock_searxng_results = SearchResultList(
            [
                {
                    "search_provider": "SEARXNG",
                    "searxng_engine": "tripadvisor",
                    "query": '"The French Diner" "Leeds" reviews',
                    "result_url": "https://www.tripadvisor.co.uk/Restaurant_Review-Leeds.html",
                    "title": "The French Diner, Leeds - Restaurant Reviews",
                    "snippet": "Rated 4.5 of 5 on Tripadvisor. 120 reviews. Excellent French dining in central Leeds.",
                    "retrieved_at": "2026-10-01T12:00:00Z"
                }
            ],
            outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS,
            provider="SEARXNG",
            query='"The French Diner" "Leeds" reviews'
        )
        mock_web = MagicMock()
        mock_web.search_web.return_value = mock_searxng_results

        enricher = ReviewRatingEnricher(web_search_provider=mock_web)
        res = enricher.enrich_business("The French Diner", "Leeds", "United Kingdom")

        self.assertEqual(res.review_provider, "SEARXNG")
        self.assertEqual(res.review_status, ReviewStatus.FOUND.value)
        self.assertEqual(res.review_count, 120)
        self.assertEqual(res.rating, 4.5)

    def test_13_existing_ddg_tests_continue_passing(self):
        """13. Existing DDG fallback continues working as expected."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = """
        <a class="result-link" href="https://leeds-curry.co.uk">Leeds Curry House</a>
        <td class="result-snippet">Authentic curry on Leeds Road. Best Indian cuisine.</td>
        """
        with patch("requests.post", return_value=mock_resp):
            res = self.provider._search_duckduckgo_fallback("Leeds Curry House")

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS)
        self.assertEqual(res.provider, "DUCKDUCKGO_FALLBACK")
        self.assertEqual(len(res), 1)

    def test_14_existing_circuit_breaker_tests_continue_passing(self):
        """14. Existing circuit breaker 3-state transitions remain intact."""
        cb = ProviderCircuitBreaker("TEST_PROVIDER", failure_threshold=2, cooldown_seconds=0.05)
        self.assertEqual(cb.status, "CLOSED")

        cb.record_failure("error 1")
        self.assertEqual(cb.status, "CLOSED")

        cb.record_failure("error 2")
        self.assertEqual(cb.status, "OPEN")

        time.sleep(0.06)
        self.assertEqual(cb.status, "HALF_OPEN")

        # Single probe can request
        self.assertTrue(cb.can_request())
        # Second probe blocked
        self.assertFalse(cb.can_request())

        # Successful probe closes circuit
        cb.record_success(results_count=1)
        self.assertEqual(cb.status, "CLOSED")

    def test_15_existing_location_identity_tests_continue_passing(self):
        """15. Existing location/identity safety rules remain intact."""
        adapter = UKReviewSourceAdapter()
        # Otley is legitimate Leeds suburb
        is_match, _ = adapter.evaluate_location_match("Otley Restaurant", "Great dining in Otley", "Leeds")
        self.assertTrue(is_match)

        # Manchester is conflicting foreign major city
        is_match, reason = adapter.evaluate_location_match("Manchester Diner", "Located in Manchester city centre", "Leeds")
        self.assertFalse(is_match)
        self.assertIsNotNone(reason)


if __name__ == "__main__":
    unittest.main()
