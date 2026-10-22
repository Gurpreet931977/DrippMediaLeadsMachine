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
    DuckDuckGoRateLimiter,
    convert_search_result_to_candidate,
)
from lib.validation.country_validator import CountryValidator
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


class TestLiveSearchHardeningAndEvidenceIntegrity(unittest.TestCase):
    """Tests 29 - 35: Live Search Anti-Bot Hardening, Circuit Protection, and Evidence Provenance"""

    def setUp(self):
        self.provider = WebSearchProvider()
        self.provider.circuit_breakers["DUCKDUCKGO_FALLBACK"].reset()
        self.provider.cache_hits = 0
        self.provider.cache_misses = 0

    @patch("requests.post")
    def test_29_bot_challenge_detection_immediate_blocked_no_retries(self, mock_post):
        """29. HTTP 202 / bot challenge returns SEARCH_BLOCKED immediately without burning retries"""
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.text = "<html><body><script src=\"anomaly.js\"></script>Select all squares containing a duck: Code 40f7</body></html>"
        mock_post.return_value = mock_resp

        with patch.object(self.provider, "_get_cache", return_value=None):
            with patch("time.sleep") as mock_sleep:
                res = self.provider._search_duckduckgo_fallback("dishoom manchester")

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_BLOCKED)
        self.assertFalse(res.succeeded)
        self.assertEqual(res.http_status, 202)
        self.assertIn("bot detection challenge", res.error)
        # Verify no wasteful retry sleeps took place against the interactive challenge
        mock_sleep.assert_not_called()
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    def test_30_circuit_open_prevents_runaway_requests(self, mock_post):
        """30. When circuit is open, search_web returns SEARCH_CIRCUIT_OPEN immediately without network calls"""
        cb = self.provider.circuit_breakers["DUCKDUCKGO_FALLBACK"]
        cb.state = CircuitState.OPEN
        cb.last_state_change = time.time()  # cooldown active

        with patch.object(self.provider, "_get_cache", return_value=None):
            res = self.provider.search_web("any query")

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_CIRCUIT_OPEN)
        self.assertFalse(res.succeeded)
        self.assertFalse(res.attempted)
        self.assertEqual(res.circuit_state, "OPEN")
        mock_post.assert_not_called()

    def test_31_cross_parameter_cache_reuse_normalized_query(self):
        """31. Normalized cache lookup allows reusing cached results across different num_results"""
        import tempfile, shutil
        temp_cache = tempfile.mkdtemp()
        try:
            prov = WebSearchProvider(cache_dir=temp_cache)
            initial_data = [
                {"title": f"Result {i}", "url": f"https://example.com/{i}", "description": f"Snippet {i}"}
                for i in range(6)
            ]
            res_list = SearchResultList(
                initial_data,
                outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS,
                provider="DUCKDUCKGO_FALLBACK",
                query="bundobust leeds"
            )
            # Cache under num_results=6
            prov._set_cache("web_bundobust leeds_6_False", res_list, provider="DUCKDUCKGO_FALLBACK", query="bundobust leeds", advanced=False)

            # Query for num_results=3 should hit the normalized query index
            cached = prov._get_cache("web_bundobust leeds_3_False", query="bundobust leeds", advanced=False)
            self.assertIsNotNone(cached)
            self.assertEqual(prov.cache_hits, 1)
        finally:
            shutil.rmtree(temp_cache, ignore_errors=True)

    def test_32_failed_search_never_cached(self):
        """32. Failed search outcomes (BLOCKED, TIMEOUT, CIRCUIT_OPEN) are never persisted to cache"""
        import tempfile, shutil
        temp_cache = tempfile.mkdtemp()
        try:
            prov = WebSearchProvider(cache_dir=temp_cache)
            failed_res = SearchResultList(
                outcome=SearchOutcome.SEARCH_BLOCKED,
                provider="DUCKDUCKGO_FALLBACK",
                query="failed query",
                error="HTTP 202 Bot challenge"
            )
            prov._set_cache("web_failed query_5_False", failed_res, provider="DUCKDUCKGO_FALLBACK", query="failed query", advanced=False)

            cached = prov._get_cache("web_failed query_5_False", query="failed query", advanced=False)
            self.assertIsNone(cached)
            self.assertEqual(prov.cache_misses, 1)
        finally:
            shutil.rmtree(temp_cache, ignore_errors=True)

    def test_33_untrusted_search_social_url_rejected_without_affinity_or_valid_profile(self):
        """33. Verification rejects post URLs, third-party publishers, and handles without business affinity"""
        verifier = NoWebsiteVerificationProvider(enable_search=False)
        biz = DiscoveredBusiness(
            company_name="Bundobust",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom"
        )

        # 1. Instagram post URL (not a profile)
        verifier._validate_and_assign_social(biz, "https://www.instagram.com/p/Cz12345ABC/")
        self.assertEqual(biz.instagram_url, "")

        # 2. Instagram reel URL
        verifier._validate_and_assign_social(biz, "https://www.instagram.com/reel/Cz12345ABC/")
        self.assertEqual(biz.instagram_url, "")

        # 3. Third-party city food blogger (themanc / manceats)
        verifier._validate_and_assign_social(biz, "https://www.instagram.com/themanc/")
        self.assertEqual(biz.instagram_url, "")

        # 4. Completely unrelated account (no name affinity)
        verifier._validate_and_assign_social(biz, "https://www.instagram.com/randomsalonuk/")
        self.assertEqual(biz.instagram_url, "")

        # 5. Legitimate account profile with name affinity
        verifier._validate_and_assign_social(biz, "https://www.instagram.com/bundobust/")
        self.assertEqual(biz.instagram_url, "https://www.instagram.com/bundobust/")

    def test_34_review_enricher_social_url_requires_profile_validation(self):
        """34. ReviewRatingEnricher does not attach social URLs from review source if invalid or media URL"""
        enricher = ReviewRatingEnricher()
        from lib.enrichment.review_rating_enricher import ReviewEnrichmentResult

        # Unrelated Facebook watch video URL
        cand = DiscoveredBusiness(
            company_name="Bundobust",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom"
        )
        res_video = ReviewEnrichmentResult(
            review_count=100,
            rating=4.5,
            review_status=ReviewStatus.FOUND.value,
            review_source_url="https://www.facebook.com/watch/?v=987654321"
        )
        with patch.object(enricher, "enrich_business", return_value=res_video):
            enricher.enrich_candidate(cand)
        # Verify candidate facebook_url remains unset for video link
        self.assertNotEqual(cand.facebook_url, "https://www.facebook.com/watch/?v=987654321")
        self.assertEqual(cand.facebook_url, "")

        # Valid business Facebook page with affinity
        res_valid = ReviewEnrichmentResult(
            review_count=150,
            rating=4.7,
            review_status=ReviewStatus.FOUND.value,
            review_source_url="https://www.facebook.com/bundobustleeds/"
        )
        with patch.object(enricher, "enrich_business", return_value=res_valid):
            enricher.enrich_candidate(cand)
        self.assertEqual(cand.facebook_url, "https://www.facebook.com/bundobustleeds/")

    @patch("requests.post")
    def test_35_bounded_retry_and_backoff_on_503(self, mock_post):
        """35. Transient HTTP 503 retries up to max_retries with bounded backoff and records failure"""
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_post.return_value = mock_resp

        with patch.object(self.provider, "_get_cache", return_value=None):
            with patch.object(self.provider.ddg_rate_limiter, "wait_if_needed"):
                with patch("time.sleep") as mock_sleep:
                    res = self.provider._search_duckduckgo_fallback("test 503 query", max_retries=2)

        self.assertEqual(res.outcome, SearchOutcome.SEARCH_FAILED)
        self.assertEqual(res.http_status, 503)
        self.assertEqual(mock_post.call_count, 3)  # initial + 2 retries
        self.assertEqual(mock_sleep.call_count, 2)
        self.assertEqual(res.retries, 2)



class TestWebDiscoveryLocationEvidenceHardening(unittest.TestCase):
    """Regression tests for non-fabricated location/country evidence in web discovery."""

    def _convert(self, title, snippet, url):
        boundary = MagicMock()
        boundary.validate_candidate.return_value = MagicMock(
            city_match=True,
            city_match_reason="mocked boundary for deterministic unit test",
            boundary_source="TEST",
            boundary_validation_method="TEST_ONLY",
        )
        return convert_search_result_to_candidate(
            result={"title": title, "snippet": snippet, "result_url": url},
            city="Manchester",
            country="United Kingdom",
            industry="restaurants",
            country_validator=CountryValidator(),
            boundary_validator=boundary,
        )

    def test_rejects_manchester_maryland_instead_of_fabricating_uk_location(self):
        candidate = self._convert(
            "Restaurants in Manchester, MD",
            "Find restaurants in Manchester, MD, Maryland, United States.",
            "https://sellbuymdhomes.com/real-estate-blog/restaurants-in-manchester-md",
        )
        self.assertIsNone(candidate)

    def test_rejects_manchester_vermont_when_source_has_no_uk_evidence(self):
        candidate = self._convert(
            "Taste | Manchester, Vermont",
            "Food and drink around Manchester, Vermont. Visitor information.",
            "https://www.manchestervermont.com/taste",
        )
        self.assertIsNone(candidate)

    def test_city_in_url_only_is_not_target_location_evidence(self):
        candidate = self._convert(
            "The Example Grill",
            "Independent restaurant with an evening menu and friendly service.",
            "https://directory.example/manchester/the-example-grill",
        )
        self.assertIsNone(candidate)

    def test_word_duke_does_not_count_as_uk_country_evidence(self):
        candidate = self._convert(
            "The Duke - Manchester",
            "A casual restaurant on Duke Street serving lunch and dinner.",
            "https://theduke.example/",
        )
        self.assertIsNone(candidate)


    def test_third_party_directory_url_is_not_misclassified_as_official_website(self):
        candidate = self._convert(
            "Cotton - Manchester",
            "Cotton is an independent restaurant in Manchester, England, UK.",
            "https://manchesterfoodguide.example/independent-restaurants/cotton",
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.company_name, "Cotton")
        self.assertEqual(candidate.raw_website, "")
        self.assertIn("result_url", candidate.raw_data)

    def test_business_named_domain_is_retained_as_probable_official_website(self):
        candidate = self._convert(
            "Cotton - Manchester",
            "Cotton is an independent restaurant in Manchester, England, UK.",
            "https://www.cottonfood.com/",
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.raw_website, "https://www.cottonfood.com/")

    def test_brand_token_inside_unrelated_domain_is_not_enough_for_official_website(self):
        candidate = self._convert(
            "Mala - Manchester",
            "Mala is an independent restaurant in Manchester, England, UK.",
            "https://smalabar.example/",
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.company_name, "Mala")
        self.assertEqual(candidate.raw_website, "")

    def test_explicit_manchester_uk_business_is_still_accepted(self):
        candidate = self._convert(
            "The Example Grill - Manchester, UK",
            "Independent restaurant in Manchester, UK. Family-run food and drinks.",
            "https://examplegrill.co.uk/",
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.raw_website, "https://examplegrill.co.uk/")



class TestCountrySpecificDiscoveryQueries(unittest.TestCase):
    """Ensures fallback discovery sends explicit geographic context to search engines."""

    def setUp(self):
        self.provider = WebSearchProvider()
        self.empty_success = SearchResultList(
            [],
            outcome=SearchOutcome.SEARCH_SUCCEEDED_EMPTY,
            provider="TEST",
        )

    def _captured_queries(self, city, country):
        with patch.object(self.provider, "search_web", return_value=self.empty_success) as search:
            self.provider.discover_businesses(city, country, "restaurants", limit=5)
        return [call.args[0] for call in search.call_args_list]

    def test_uk_discovery_queries_include_country_and_regional_context(self):
        queries = self._captured_queries("Manchester", "United Kingdom")
        self.assertTrue(queries)
        self.assertTrue(all("Manchester" in q or "Greater Manchester" in q for q in queries))
        self.assertTrue(any("England" in q or "United Kingdom" in q for q in queries))
        self.assertTrue(any("no website" in q for q in queries))
        self.assertTrue(any("Greater Manchester" in q for q in queries))
        self.assertLessEqual(len(queries), 6)

    def test_other_countries_are_included_in_every_fallback_query(self):
        queries = self._captured_queries("Austin", "United States")
        self.assertEqual(len(queries), 4)
        self.assertTrue(all("Austin" in q and "United States" in q for q in queries))


if __name__ == "__main__":
    unittest.main()
