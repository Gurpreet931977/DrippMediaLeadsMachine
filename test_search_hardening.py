"""
Unit tests for Search Reliability, Website Verification Hardening,
Circuit Breaker Recovery, Review Enricher Failure Safety, and Location Guardrails.
Phase 1 Hardening Test Suite (Tests 1 - 28).
"""

import unittest
from unittest.mock import patch, MagicMock
import time
from datetime import datetime, timezone

from lib.types import DiscoveredBusiness, WebsiteStatus, VerificationStatus
from lib.discovery.web_search import (
    WebSearchProvider,
    SearchOutcome,
    SearchResultList,
    CircuitState,
    ProviderCircuitBreaker,
    DuckDuckGoRateLimiter
)
from lib.verification.no_website_verifier import NoWebsiteVerificationProvider
from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
    ReviewStatus,
    ReviewConfidence
)
from lib.country_adapters import get_country_adapter


class TestSearchOutcomeModel(unittest.TestCase):
    """Tests 1 - 8: Search Outcome Model & Cache Safety"""

    def setUp(self):
        self.provider = WebSearchProvider()
        self.provider.circuit_breakers["DUCKDUCKGO_FALLBACK"].reset()
        self.provider.cache_hits = 0
        self.provider.cache_misses = 0

    @patch("requests.post")
    def test_01_search_outcome_succeeded_with_results(self, mock_post):
        """1. Successful search with results"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = """
        <table>
            <tr><td><a class="result-link" href="https://bundobust.com/leeds">Bundobust Indian Street Food</a></td></tr>
            <tr><td class="result-snippet">Craft beer & Indian street food in Leeds</td></tr>
        </table>
        """
        mock_post.return_value = mock_resp

        with patch.object(self.provider, "_get_cache", return_value=None):
            res = self.provider._search_duckduckgo_fallback("bundobust leeds")

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS)
        self.assertTrue(res.succeeded)
        self.assertTrue(res.attempted)
        self.assertGreater(len(res), 0)
        self.assertEqual(res.provider, "DUCKDUCKGO_FALLBACK")

    @patch("requests.post")
    def test_02_search_outcome_succeeded_empty(self, mock_post):
        """2. Successful empty search"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>No results found for this query.</body></html>"
        mock_post.return_value = mock_resp

        with patch.object(self.provider, "_get_cache", return_value=None):
            res = self.provider._search_duckduckgo_fallback("completely_nonexistent_business_xyz_12345")

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_SUCCEEDED_EMPTY)
        self.assertTrue(res.succeeded)
        self.assertTrue(res.attempted)
        self.assertEqual(len(res), 0)

    @patch("requests.post")
    def test_03_search_outcome_http_failure(self, mock_post):
        """3. HTTP failure"""
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_post.return_value = mock_resp

        with patch.object(self.provider, "_get_cache", return_value=None):
            with patch("time.sleep", return_value=None):
                res = self.provider._search_duckduckgo_fallback("test query")

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_FAILED)
        self.assertFalse(res.succeeded)
        self.assertEqual(res.http_status, 503)
        self.assertIn("503", res.error)

    @patch("requests.post")
    def test_04_search_outcome_timeout(self, mock_post):
        """4. Timeout"""
        import requests
        mock_post.side_effect = requests.Timeout("Connection timed out after 5.0s")

        with patch.object(self.provider, "_get_cache", return_value=None):
            with patch("time.sleep", return_value=None):
                res = self.provider._search_duckduckgo_fallback("test timeout query")

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_TIMEOUT)
        self.assertFalse(res.succeeded)
        self.assertIn("Timeout", res.error)

    def test_05_search_outcome_circuit_open(self):
        """5. Circuit-open query"""
        self.provider.searxng_url = None
        cb = self.provider.circuit_breakers["DUCKDUCKGO_FALLBACK"]
        cb.trip(cooldown_seconds=60.0)

        with patch.object(self.provider, "_get_cache", return_value=None):
            res = self.provider.search_web("test query when circuit open")

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_CIRCUIT_OPEN)
        self.assertFalse(res.succeeded)
        self.assertEqual(res.circuit_state, "OPEN")
        self.assertEqual(len(res), 0)

    def test_06_search_outcome_provider_unavailable(self):
        """6. Provider unavailable (all unconfigured / open)"""
        for cb in self.provider.circuit_breakers.values():
            cb.trip(cooldown_seconds=60.0)

        with patch.object(self.provider, "_get_cache", return_value=None):
            res = self.provider.search_web("test all down")

        self.assertIn(res.outcome, [SearchOutcome.SEARCH_CIRCUIT_OPEN, SearchOutcome.SEARCH_PROVIDER_UNAVAILABLE])
        self.assertFalse(res.succeeded)

    def test_07_search_outcome_cache_hit(self):
        """7. Cache hit returns structured metadata with is_cached=True"""
        dummy_res = SearchResultList(
            [{"title": "Cached Biz", "result_url": "https://cached.com", "snippet": "Sample"}],
            outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS,
            provider="DUCKDUCKGO_FALLBACK",
            query="cached query"
        )
        self.provider._set_cache("cached_query_key_1", dummy_res, provider="DUCKDUCKGO_FALLBACK")

        # Mock cache retrieval
        with patch.object(self.provider, "_get_cache", return_value=dummy_res):
            res = self.provider.search_web("cached query")

        self.assertTrue(res.is_cached)
        self.assertTrue(res.succeeded)
        self.assertEqual(len(res), 1)

    def test_08_cached_failure_is_not_treated_as_empty_success(self):
        """8. Provider failure is NEVER persisted as an empty successful result"""
        failed_res = SearchResultList(
            [],
            outcome=SearchOutcome.SEARCH_FAILED,
            provider="DUCKDUCKGO_FALLBACK",
            error="HTTP 500 error"
        )
        cache_key = "failed_query_must_not_cache"
        self.provider._set_cache(cache_key, failed_res, provider="DUCKDUCKGO_FALLBACK")

        # Verify it was not written to cache
        cached = self.provider._get_cache(cache_key)
        self.assertIsNone(cached)


class TestRetryAndBackoff(unittest.TestCase):
    """Tests 9 - 11: Exponential Backoff and Bounded Retries"""

    def setUp(self):
        self.provider = WebSearchProvider()
        self.provider.circuit_breakers["DUCKDUCKGO_FALLBACK"].reset()

    @patch("requests.post")
    def test_09_transient_failure_then_success(self, mock_post):
        """9. Transient failure then success on retry"""
        import requests
        resp_fail = MagicMock()
        resp_fail.status_code = 503

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.text = '<table><tr><td><a class="result-link" href="https://ok.com">OK Result</a></td></tr></table>'

        mock_post.side_effect = [resp_fail, resp_ok]

        with patch.object(self.provider, "_get_cache", return_value=None):
            with patch("time.sleep", return_value=None):
                res = self.provider._search_duckduckgo_fallback("transient query")

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS)
        self.assertTrue(res.succeeded)
        self.assertGreaterEqual(res.retries, 1)

    @patch("requests.post")
    def test_10_transient_failure_through_retry_limit(self, mock_post):
        """10. Transient failure through retry limit"""
        import requests
        mock_post.side_effect = requests.ConnectionError("Connection refused")

        with patch.object(self.provider, "_get_cache", return_value=None):
            with patch("time.sleep", return_value=None):
                res = self.provider._search_duckduckgo_fallback("down query")

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_FAILED)
        self.assertFalse(res.succeeded)
        self.assertGreaterEqual(res.retries, 2)

    @patch("requests.post")
    def test_11_non_transient_failure_not_endlessly_retried(self, mock_post):
        """11. Non-transient client error (400) is NOT retried"""
        resp = MagicMock()
        resp.status_code = 400
        mock_post.return_value = resp

        with patch.object(self.provider, "_get_cache", return_value=None):
            with patch("time.sleep", return_value=None):
                res = self.provider._search_duckduckgo_fallback("bad request query")

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_FAILED)
        self.assertEqual(res.retries, 0)


class TestCircuitBreakerBehavior(unittest.TestCase):
    """Tests 12 - 15: Circuit Breaker Transitions & Isolation"""

    def setUp(self):
        self.cb = ProviderCircuitBreaker(provider="TEST_DDG", failure_threshold=2, cooldown_seconds=0.1)

    def test_12_circuit_open_to_half_open_to_closed(self):
        """12. OPEN -> HALF_OPEN -> CLOSED on successful probe"""
        self.cb.record_failure("err 1")
        self.cb.record_failure("err 2")
        self.assertEqual(self.cb.status, CircuitState.OPEN.value)

        # Wait past cooldown
        time.sleep(0.12)
        self.assertTrue(self.cb.can_request())  # Transitions to HALF_OPEN probe
        self.assertEqual(self.cb.status, CircuitState.HALF_OPEN.value)

        # Successful probe
        self.cb.record_success(results_count=1)
        self.assertEqual(self.cb.status, CircuitState.CLOSED.value)
        self.assertEqual(self.cb.probe_result, "SUCCESS")

    def test_13_circuit_open_to_half_open_to_open(self):
        """13. OPEN -> HALF_OPEN -> OPEN on failed probe"""
        self.cb.record_failure("err 1")
        self.cb.record_failure("err 2")
        self.assertEqual(self.cb.status, CircuitState.OPEN.value)

        time.sleep(0.12)
        self.assertTrue(self.cb.can_request())  # HALF_OPEN probe allowed
        self.assertEqual(self.cb.status, CircuitState.HALF_OPEN.value)

        # Failed probe
        self.cb.record_failure("probe failed")
        self.assertEqual(self.cb.status, CircuitState.OPEN.value)
        self.assertEqual(self.cb.probe_result, "FAILURE")

    def test_14_only_one_half_open_probe(self):
        """14. Exactly one probe allowed in HALF_OPEN; second request rejected"""
        self.cb.trip(cooldown_seconds=0.05)
        time.sleep(0.06)

        first_allowed = self.cb.can_request()
        second_allowed = self.cb.can_request()

        self.assertTrue(first_allowed)
        self.assertFalse(second_allowed)
        self.assertEqual(self.cb.status, CircuitState.HALF_OPEN.value)

    def test_15_provider_isolation(self):
        """15. Failure in DDG does NOT affect other providers"""
        web = WebSearchProvider()
        web.circuit_breakers["DUCKDUCKGO_FALLBACK"].trip()

        self.assertEqual(web.circuit_breakers["DUCKDUCKGO_FALLBACK"].status, CircuitState.OPEN.value)
        self.assertEqual(web.circuit_breakers["TAVILY"].status, CircuitState.CLOSED.value)
        self.assertEqual(web.circuit_breakers["BRAVE"].status, CircuitState.CLOSED.value)
        self.assertEqual(web.circuit_breakers["SEARXNG"].status, CircuitState.CLOSED.value)


class TestNoWebsiteVerifierHardening(unittest.TestCase):
    """Tests 16 - 19: No-Website Verifier Search Failure Safety"""

    def setUp(self):
        self.verifier = NoWebsiteVerificationProvider()
        self.biz = DiscoveredBusiness(
            company_name="Test Neighborhood Diner",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            raw_website=""
        )

    def test_16_successful_empty_search_contributes_to_no_website(self):
        """16. Successful empty search can contribute to NO_WEBSITE_CONFIRMED"""
        empty_search = SearchResultList(
            [],
            outcome=SearchOutcome.SEARCH_SUCCEEDED_EMPTY,
            provider="DUCKDUCKGO_FALLBACK"
        )
        res = self.verifier.verify_business(self.biz, cached_search_results=empty_search)
        self.assertEqual(res["website_status"], WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(res["verification_status"], VerificationStatus.NO_WEBSITE_CONFIRMED.value)

    def test_17_circuit_open_cannot_produce_no_website_confirmed(self):
        """17. Circuit-open search MUST NOT produce NO_WEBSITE_CONFIRMED"""
        circuit_open_search = SearchResultList(
            [],
            outcome=SearchOutcome.SEARCH_CIRCUIT_OPEN,
            provider="DUCKDUCKGO_FALLBACK",
            circuit_state="OPEN",
            error="Circuit breaker is OPEN"
        )
        res = self.verifier.verify_business(self.biz, cached_search_results=circuit_open_search)
        self.assertNotEqual(res["website_status"], WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(res["website_status"], WebsiteStatus.WEBSITE_UNCLEAR.value)
        self.assertEqual(res["verification_status"], VerificationStatus.WEBSITE_UNCLEAR.value)

    def test_18_timeout_cannot_produce_no_website_confirmed(self):
        """18. Timeout search MUST NOT produce NO_WEBSITE_CONFIRMED"""
        timeout_search = SearchResultList(
            [],
            outcome=SearchOutcome.SEARCH_TIMEOUT,
            provider="DUCKDUCKGO_FALLBACK",
            error="Read timed out"
        )
        res = self.verifier.verify_business(self.biz, cached_search_results=timeout_search)
        self.assertNotEqual(res["website_status"], WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(res["website_status"], WebsiteStatus.WEBSITE_UNCLEAR.value)

    def test_19_provider_error_cannot_produce_no_website_confirmed(self):
        """19. Provider HTTP error MUST NOT produce NO_WEBSITE_CONFIRMED"""
        failed_search = SearchResultList(
            [],
            outcome=SearchOutcome.SEARCH_FAILED,
            provider="DUCKDUCKGO_FALLBACK",
            http_status=403,
            error="HTTP 403 Forbidden"
        )
        res = self.verifier.verify_business(self.biz, cached_search_results=failed_search)
        self.assertNotEqual(res["website_status"], WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(res["website_status"], WebsiteStatus.WEBSITE_UNCLEAR.value)


class TestReviewEnrichmentHardening(unittest.TestCase):
    """Tests 20 - 24: Review Enricher Hardening & Parser Verification"""

    def setUp(self):
        self.enricher = ReviewRatingEnricher()

    def test_20_successful_empty_search_yields_not_found(self):
        """20. Successful empty search -> NOT_FOUND"""
        empty_search = SearchResultList([], outcome=SearchOutcome.SEARCH_SUCCEEDED_EMPTY)
        res = self.enricher.enrich_business(
            business_name="Obscure Cafe",
            city="Leeds",
            country="United Kingdom",
            search_results=empty_search
        )
        self.assertEqual(res.review_status, ReviewStatus.NOT_FOUND.value)

    def test_21_failed_search_yields_search_failed(self):
        """21. Failed search -> SEARCH_FAILED (not silently converted to NOT_FOUND)"""
        failed_search = SearchResultList(
            [],
            outcome=SearchOutcome.SEARCH_FAILED,
            error="HTTP 500 Server Error"
        )
        res = self.enricher.enrich_business(
            business_name="Shezzaan's",
            city="Leeds",
            country="United Kingdom",
            search_results=failed_search
        )
        self.assertEqual(res.review_status, ReviewStatus.SEARCH_FAILED.value)

    def test_22_circuit_open_yields_search_failed(self):
        """22. Circuit-open search -> SEARCH_FAILED"""
        open_search = SearchResultList(
            [],
            outcome=SearchOutcome.SEARCH_CIRCUIT_OPEN,
            error="Circuit breaker is OPEN"
        )
        res = self.enricher.enrich_business(
            business_name="Shezzaan's",
            city="Leeds",
            country="United Kingdom",
            search_results=open_search
        )
        self.assertEqual(res.review_status, ReviewStatus.SEARCH_FAILED.value)

    def test_23_parser_evidence_still_works(self):
        """23. Review parser evidence extraction operates normally on real snippet"""
        item = self.enricher.extract_evidence_from_snippet(
            snippet="Bundobust, Leeds: See 850 unbiased reviews of Bundobust, rated 4.5 of 5 on Tripadvisor and ranked #15 of 1,200 restaurants in Leeds.",
            title="Bundobust, Leeds - Restaurant Reviews, Photos - Tripadvisor",
            url="https://www.tripadvisor.co.uk/Restaurant_Review-g186411-d6878345-Reviews-Bundobust-Leeds_West_Yorkshire_England.html",
            business_name="Bundobust",
            city="Leeds",
            postcode="LS1 4DY"
        )
        self.assertIsNotNone(item)
        self.assertIsNone(item.reject_reason)
        self.assertEqual(item.rating, 4.5)
        self.assertEqual(item.review_count, 850)
        self.assertEqual(item.confidence, ReviewConfidence.HIGH.value)

    def test_24_existing_tripadvisor_regression_remains_passing(self):
        """24. Existing Tripadvisor regression parsing test remains passing"""
        snippet = "Read what customers say about Fenix Manchester. Rating: 4.8 · 350 reviews. Contemporary Greek cuisine."
        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title="Fenix Manchester - Restaurant Reviews",
            url="https://www.tripadvisor.co.uk/Restaurant_Review-Fenix.html",
            business_name="Fenix",
            city="Manchester"
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.rating, 4.8)
        self.assertEqual(item.review_count, 350)


class TestLocationSafetyGuardrails(unittest.TestCase):
    """Tests 25 - 28: Location Matching and Suburb Preservation"""

    def setUp(self):
        self.enricher = ReviewRatingEnricher()

    def test_25_wrong_small_town_business_rejected(self):
        """25. Unrelated business from an unlisted small town (Derby) is rejected"""
        snippet = "The Olive Branch, Derby: See 180 unbiased reviews of The Olive Branch, rated 4.5 of 5 on Tripadvisor."
        title = "The Olive Branch, Derby - Restaurant Reviews, Photos - Tripadvisor"
        url = "https://www.tripadvisor.co.uk/Restaurant_Review-The_Olive_Branch_Derby.html"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="The Olive Branch",
            city="Leeds"
        )
        self.assertIsNotNone(item)
        self.assertIsNotNone(item.reject_reason)
        self.assertIn("LOCATION_MISMATCH", item.reject_reason)

    def test_26_legitimate_suburb_accepted(self):
        """26. Legitimate suburb (Headingley) is accepted and treated as local corroboration"""
        snippet = "Headingley Taps, Headingley: 240 customer reviews. Rated 4.2 out of 5 stars."
        title = "Headingley Taps, Headingley - Reviews"
        url = "https://www.tripadvisor.co.uk/Headingley_Taps.html"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Headingley Taps",
            city="Leeds"
        )
        self.assertIsNotNone(item)
        self.assertIsNone(item.reject_reason)
        self.assertEqual(item.confidence, ReviewConfidence.HIGH.value)

    def test_27_legitimate_administrative_district_inclusion_accepted(self):
        """27. Legitimate administrative-district inclusion (Otley in Leeds district) is accepted"""
        snippet = "Il Forno, Otley: Authentic wood fired pizza in Otley. Rated 4.6 of 5 with 140 reviews."
        title = "Il Forno, Otley - Italian Restaurant Reviews"
        url = "https://www.tripadvisor.co.uk/Il_Forno_Otley.html"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Il Forno",
            city="Leeds"
        )
        self.assertIsNotNone(item)
        self.assertIsNone(item.reject_reason)
        self.assertEqual(item.review_count, 140)
        self.assertEqual(item.rating, 4.6)

    def test_28_cross_city_same_name_business_rejected(self):
        """28. Cross-city same-name business (Birmingham vs Leeds) is rejected"""
        snippet = "Bundobust, Birmingham: Craft beer and Indian street food. Rated 4.6 with 520 reviews."
        title = "Bundobust, Birmingham - Restaurant Reviews"
        url = "https://www.tripadvisor.co.uk/Bundobust_Birmingham.html"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Bundobust",
            city="Leeds"
        )
        self.assertIsNotNone(item)
        self.assertIsNotNone(item.reject_reason)
        self.assertTrue("LOCATION_MISMATCH" in item.reject_reason or "WRONG_BRANCH" in item.reject_reason)


if __name__ == "__main__":
    unittest.main()
