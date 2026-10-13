"""
Regression Test Suite: Enrichment and Verification Data Path
============================================================
Tests:
1. Missing review provider data (preserves unknown/None, routes to RESEARCH_ONLY, never fabricates values)
2. Successful review enrichment (satisfies Rule B with 50+ reviews, 4.0+ rating, recent date, OSM phone/address -> ACTIVE_CONFIRMED -> OUTREACH_READY)
3. Moderate review enrichment (10-49 reviews -> MANUAL_REVIEW)
4. Provider failure / timeout / outage (gracefully caught, differentiated failure state, routes to RESEARCH_ONLY)
5. DuckDuckGo HTML tag parsing (whitespace preserved across inline <b> tags)
6. Pipeline end-to-end flow with candidate enrichment and funnel stats
"""

import unittest
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

from bs4 import BeautifulSoup

from lib.types import (
    DiscoveredBusiness,
    Lead,
    WebsiteStatus,
    VerificationStatus,
    QualificationState,
    OperationalStatus,
    SourceFamily,
    Priority,
    ResearchFailureState,
)
from lib.discovery.web_search import WebSearchProvider, SearchOutcome, SearchResultList
from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
    ReviewEvidenceItem,
    ReviewEnrichmentResult,
    ReviewStatus,
    ReviewConfidence,
    ReviewFreshness,
)
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.validation.operational_validator import OperationalValidator
from lib.pipeline import LeadGenerationPipeline


class MockWebSearchProvider(WebSearchProvider):
    def __init__(self, canned_results=None, fail_mode=None):
        super().__init__()
        self.canned_results = canned_results or []
        self.fail_mode = fail_mode
        self.calls = []

    def search_web(self, query: str, num_results: int = 5, advanced: bool = False) -> SearchResultList:
        self.calls.append(query)
        if self.fail_mode == "TIMEOUT":
            return SearchResultList(
                outcome=SearchOutcome.SEARCH_TIMEOUT,
                provider="MOCK_FAIL",
                query=query,
                error="Read timed out",
                latency=5.0
            )
        elif self.fail_mode == "HTTP_ERROR":
            return SearchResultList(
                outcome=SearchOutcome.SEARCH_FAILED,
                provider="MOCK_FAIL",
                query=query,
                error="HTTP 500 Internal Server Error",
                latency=0.5
            )
        elif self.fail_mode == "CIRCUIT_OPEN":
            return SearchResultList(
                outcome=SearchOutcome.SEARCH_CIRCUIT_OPEN,
                provider="MOCK_FAIL",
                query=query,
                error="Circuit breaker is OPEN",
                latency=0.0
            )
        elif self.fail_mode == "EMPTY":
            return SearchResultList(
                iterable=[],
                outcome=SearchOutcome.SEARCH_SUCCEEDED_EMPTY,
                provider="MOCK_EMPTY",
                query=query,
                latency=0.1
            )

        return SearchResultList(
            iterable=self.canned_results,
            outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS if self.canned_results else SearchOutcome.SEARCH_SUCCEEDED_EMPTY,
            provider="MOCK_SUCCESS",
            query=query,
            latency=0.1
        )


class TestEnrichmentVerificationPipeline(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now()
        self.recent_date = (self.now - timedelta(days=20)).strftime("%Y-%m-%d")

    def test_ddg_html_parsing_separates_inline_tags(self):
        """Proves that BeautifulSoup get_text(' ', strip=True) preserves whitespace across <b> tags."""
        html = (
            '<td class="result-snippet">'
            'See 2,308 unbiased <b>reviews</b> of <b>Dishoom</b> Manchester, rated 4.7 of 5 on Tripadvisor.'
            '</td>'
        )
        soup = BeautifulSoup(html, "html.parser")
        snippet_elem = soup.find("td", class_="result-snippet")
        
        # Without separator, words concatenate
        raw_text = snippet_elem.get_text(strip=True)
        self.assertIn("unbiasedreviewsof", raw_text)
        
        # With separator, words are cleanly separated
        clean_text = snippet_elem.get_text(" ", strip=True)
        self.assertIn("unbiased reviews of Dishoom Manchester", clean_text)
        
        # Extraction succeeds with clean text
        ev = ReviewRatingEnricher.extract_evidence_from_snippet(
            snippet=clean_text,
            title="Dishoom Manchester",
            url="https://www.tripadvisor.co.uk/Restaurant_Review-Dishoom.html",
            business_name="Dishoom",
            city="Manchester"
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev.review_count, 2308)
        self.assertEqual(ev.rating, 4.7)

    def test_missing_provider_data_preserves_unknown_and_routes_to_research_only(self):
        """Candidate with no search results leaves reviews/ratings unknown and routes to RESEARCH_ONLY."""
        mock_web = MockWebSearchProvider(fail_mode="EMPTY")
        enricher = ReviewRatingEnricher(web_search_provider=mock_web)
        
        candidate = DiscoveredBusiness(
            company_name="Ghost Bistro",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            address="10 Deansgate, Manchester M3 2FW",
            phone="+44 161 834 1234",
            raw_website="",
            discovery_source="OPENSTREETMAP"
        )
        
        enriched = enricher.enrich_candidate(candidate)
        self.assertIsNone(enriched.review_count)
        self.assertIsNone(enriched.rating)
        self.assertEqual(enriched.raw_data.get("research_telemetry", {}).get("failure_reason"), ResearchFailureState.NO_EVIDENCE_FOUND.value)

        # Qualification evaluates to RESEARCH_ONLY
        scorer = LeadScoringProvider()
        audit = scorer.evaluate_lead(enriched, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertEqual(audit["priority"], Priority.RESEARCH_ONLY.value)
        self.assertIn("Genuinely no review evidence found", audit["qualification_reason"])

    def test_provider_failure_gracefully_handled_and_differentiated(self):
        """Provider timeout or HTTP error does not crash the pipeline and records differentiated telemetry."""
        # A. Timeout
        mock_timeout = MockWebSearchProvider(fail_mode="TIMEOUT")
        enricher = ReviewRatingEnricher(web_search_provider=mock_timeout)
        candidate = DiscoveredBusiness(
            company_name="Timeout Kitchen",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            address="20 King Street, Manchester M2 6AQ",
            phone="+44 161 832 9999",
            raw_website="",
            discovery_source="OPENSTREETMAP"
        )
        enriched = enricher.enrich_candidate(candidate)
        self.assertIsNone(enriched.review_count)
        self.assertEqual(enriched.raw_data["research_telemetry"]["failure_reason"], ResearchFailureState.PROVIDER_TIMEOUT.value)
        
        scorer = LeadScoringProvider()
        audit = scorer.evaluate_lead(enriched, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertIn("timed out", audit["qualification_reason"])

        # B. Provider Failed (HTTP Error)
        mock_http_err = MockWebSearchProvider(fail_mode="HTTP_ERROR")
        enricher_err = ReviewRatingEnricher(web_search_provider=mock_http_err)
        cand_err = DiscoveredBusiness(
            company_name="Error Cafe",
            category="Cafe",
            city="Manchester",
            target_country="United Kingdom",
            address="30 Oxford Rd, Manchester M1 5QA",
            phone="+44 161 275 1111",
            raw_website="",
            discovery_source="OPENSTREETMAP"
        )
        enriched_err = enricher_err.enrich_candidate(cand_err)
        self.assertIsNone(enriched_err.review_count)
        self.assertEqual(enriched_err.raw_data["research_telemetry"]["failure_reason"], ResearchFailureState.PROVIDER_FAILED.value)
        
        audit_err = scorer.evaluate_lead(enriched_err, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(audit_err["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertIn("provider failed", audit_err["qualification_reason"].lower())

    def test_successful_enrichment_qualifies_under_rule_b(self):
        """Candidate with 140 reviews, 4.8 rating, recent date, and OSM phone/address qualifies as OUTREACH_READY."""
        mock_results = [
            {
                "title": "The Manchester Grill - Manchester Reviews - Tripadvisor",
                "snippet": f"The Manchester Grill, Manchester: See 140 unbiased reviews, rated 4.8 of 5 on Tripadvisor. Reviewed {self.recent_date}.",
                "result_url": "https://www.tripadvisor.co.uk/Restaurant_Review-g187069-d99999-The_Manchester_Grill.html",
                "search_provider": "DUCKDUCKGO_FALLBACK"
            }
        ]
        mock_web = MockWebSearchProvider(canned_results=mock_results)
        enricher = ReviewRatingEnricher(web_search_provider=mock_web)
        
        candidate = DiscoveredBusiness(
            company_name="The Manchester Grill",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            address="45 Peter Street, Manchester M2 5QR",
            phone="+44 161 832 5555",
            raw_website="",
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        
        enriched = enricher.enrich_candidate(candidate)
        self.assertEqual(enriched.review_count, 140)
        self.assertEqual(enriched.rating, 4.8)
        self.assertEqual(enriched.latest_review_date, self.recent_date)
        
        scorer = LeadScoringProvider()
        audit = scorer.evaluate_lead(enriched, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        
        # Must satisfy Rule B
        self.assertEqual(audit["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(audit["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertTrue(audit["is_outreach_ready"])

    def test_moderate_reviews_routes_to_manual_review(self):
        """Candidate with 25 reviews (10-49) routes to MANUAL_REVIEW, not OUTREACH_READY."""
        mock_results = [
            {
                "title": "Little Noodle Bar, Manchester - Restaurant Guru",
                "snippet": f"Little Noodle Bar in Manchester rated 4.6 out of 5 on Restaurant Guru: 25 reviews by visitors. Reviewed {self.recent_date}.",
                "result_url": "https://restaurantguru.com/Little-Noodle-Bar-Manchester",
                "search_provider": "DUCKDUCKGO_FALLBACK"
            }
        ]
        mock_web = MockWebSearchProvider(canned_results=mock_results)
        enricher = ReviewRatingEnricher(web_search_provider=mock_web)
        
        candidate = DiscoveredBusiness(
            company_name="Little Noodle Bar",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            address="12 Faulkner Street, Manchester M1 4FH",
            phone="+44 161 236 8888",
            raw_website="",
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        
        enriched = enricher.enrich_candidate(candidate)
        self.assertEqual(enriched.review_count, 25)
        self.assertEqual(enriched.rating, 4.6)
        
        scorer = LeadScoringProvider()
        audit = scorer.evaluate_lead(enriched, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        
        self.assertEqual(audit["qualification_state"], QualificationState.MANUAL_REVIEW.value)
        self.assertFalse(audit["is_outreach_ready"])
        self.assertIn("Moderate customer traction (25 reviews", audit["qualification_reason"])

    def test_pipeline_funnel_and_stats_with_enrichment(self):
        """Full pipeline execution with test candidates exercises enrichment and records active_confirmed."""
        # Candidate 1: Qualifies under Rule B
        cand1 = DiscoveredBusiness(
            company_name="Premier Manchester Kitchen",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            address="100 Deansgate, Manchester M3 2QG",
            phone="+44 161 834 5678",
            raw_website="",
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        # Candidate 2: Unknown review evidence
        cand2 = DiscoveredBusiness(
            company_name="Secret Alley Diner",
            category="Diner",
            city="Manchester",
            target_country="United Kingdom",
            address="5 Tib Street, Manchester M4 1CG",
            phone="+44 161 839 1234",
            raw_website="",
            discovery_source="OPENSTREETMAP",
            city_match=True
        )

        class CustomMockEnricher(ReviewRatingEnricher):
            def __init__(self, recent_date):
                super().__init__(web_search_provider=MockWebSearchProvider())
                self.recent_date = recent_date

            def enrich_candidate(self, candidate: DiscoveredBusiness) -> DiscoveredBusiness:
                if candidate.company_name == "Premier Manchester Kitchen":
                    candidate.review_count = 150
                    candidate.rating = 4.7
                    candidate.latest_review_date = self.recent_date
                    if not hasattr(candidate, "raw_data") or not isinstance(candidate.raw_data, dict):
                        candidate.raw_data = {}
                    candidate.raw_data["review_enrichment"] = {
                        "review_count": 150,
                        "rating": 4.7,
                        "review_source": "Tripadvisor",
                        "review_source_url": "https://tripadvisor.co.uk",
                        "review_evidence": "150 reviews, 4.7 stars",
                        "review_evidence_date": self.recent_date,
                        "review_confidence": "HIGH",
                        "review_freshness": "RECENT",
                        "review_status": "FOUND"
                    }
                    candidate.raw_data["research_telemetry"] = {"failure_reason": ResearchFailureState.NONE.value}
                else:
                    if not hasattr(candidate, "raw_data") or not isinstance(candidate.raw_data, dict):
                        candidate.raw_data = {}
                    candidate.raw_data["research_telemetry"] = {"failure_reason": ResearchFailureState.NO_EVIDENCE_FOUND.value}
                return candidate

        mock_enricher = CustomMockEnricher(self.recent_date)
        pipeline = LeadGenerationPipeline(
            review_enricher=mock_enricher,
            shadow_mode=True
        )

        # Mock the website verifier so both candidates are confirmed NO WEBSITE
        class AlwaysNoWebsiteVerifier:
            def verify_business(self, biz, cached_search_results=None):
                return {
                    "verification_status": VerificationStatus.NO_WEBSITE_CONFIRMED.value,
                    "verification_reason": "No website found in deep search",
                    "verified_website": "",
                    "evidence": {}
                }
        pipeline.verifier = AlwaysNoWebsiteVerifier()

        result = pipeline.run(
            country="United Kingdom",
            cities=["Manchester"],
            industry="Restaurant",
            requested_qualified_leads=2,
            batch_size=5,
            inject_test_candidates=[cand1, cand2]
        )

        stats = result["stats"]
        self.assertEqual(stats["no_website_confirmed"], 2)
        self.assertEqual(stats["active_confirmed"], 1)
        self.assertEqual(stats["outreach_ready"], 1)
        self.assertEqual(stats["research_only"], 1)
        self.assertEqual(len(result["leads"]), 1)
        self.assertEqual(result["leads"][0].company_name, "Premier Manchester Kitchen")
        self.assertEqual(result["leads"][0].operational_status, OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(result["leads"][0].review_count, 150)
        self.assertEqual(result["leads"][0].rating, 4.7)

    def test_no_website_verifier_web_search_fallback_and_social_discovery(self):
        """NoWebsiteVerificationProvider uses web_search_provider fallback when Apify is disabled and captures social profiles."""
        from lib.verification.no_website_verifier import NoWebsiteVerificationProvider
        
        search_results = [
            {
                "title": "Salford Spice Instagram",
                "result_url": "https://www.instagram.com/salfordspice/",
                "snippet": "Official Instagram of Salford Spice restaurant in Manchester."
            },
            {
                "title": "Salford Spice Facebook",
                "result_url": "https://www.facebook.com/salfordspiceofficial/",
                "snippet": "Authentic dining in Manchester. Welcome to our Facebook page."
            }
        ]
        mock_web = MockWebSearchProvider(canned_results=search_results)
        verifier = NoWebsiteVerificationProvider(apify_client=None, enable_search=True, web_search_provider=mock_web)
        
        candidate = DiscoveredBusiness(
            company_name="Salford Spice",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            address="15 Chapel St, Salford M3 7AF",
            phone="+44 161 834 0000",
            raw_website="",
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        
        ver_res = verifier.verify_business(candidate)
        self.assertEqual(ver_res["verification_status"], VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(candidate.instagram_url, "https://www.instagram.com/salfordspice/")
        self.assertEqual(candidate.facebook_url, "https://www.facebook.com/salfordspiceofficial/")


if __name__ == "__main__":
    unittest.main()
