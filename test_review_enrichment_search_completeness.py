"""Regression tests for complete review-metric search stopping behavior."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lib.enrichment.review_rating_enricher import ReviewRatingEnricher


class TestReviewEnrichmentSearchCompleteness(unittest.TestCase):
    def setUp(self):
        self.enricher = ReviewRatingEnricher()

    @staticmethod
    def make_adapter():
        review_sources = SimpleNamespace(
            status="CONFIGURED",
            get_search_queries=Mock(return_value=["first targeted query", "second targeted query"]),
            evaluate_location_match=Mock(return_value=(True, "")),
            get_major_cities=Mock(return_value=[]),
        )
        return SimpleNamespace(review_sources=review_sources)

    @staticmethod
    def partial_count_result():
        return [{
            "title": "The Test Bistro, Manchester - Reviews",
            "snippet": "The Test Bistro in Manchester has accumulated over 85 reviews from local diners.",
            "result_url": "https://www.facebook.com/thetestbistro",
            "provider": "Tavily",
        }]

    @staticmethod
    def complete_metrics_result():
        return [{
            "title": "The Test Bistro, Manchester - Restaurant Reviews",
            "snippet": "The Test Bistro in Manchester is rated 4.5 out of 5 stars based on 95 reviews on Tripadvisor.",
            "result_url": "https://www.tripadvisor.co.uk/Restaurant_Review-The_Test_Bistro-Manchester.html",
            "provider": "Tavily",
        }]

    def test_second_query_runs_when_first_query_only_finds_review_count(self):
        adapter = self.make_adapter()
        with patch("lib.country_adapters.get_country_adapter", return_value=adapter):
            self.enricher.web.search_web = Mock(
                side_effect=[
                    self.partial_count_result(),
                    self.complete_metrics_result(),
                ]
            )
            result = self.enricher.enrich_business(
                business_name="The Test Bistro",
                city="Manchester",
                country="United Kingdom",
            )

        self.assertEqual(self.enricher.web.search_web.call_count, 2)
        self.assertEqual(result.review_count, 95)
        self.assertEqual(result.rating, 4.5)

    def test_search_still_stops_when_first_query_has_both_metrics(self):
        adapter = self.make_adapter()
        with patch("lib.country_adapters.get_country_adapter", return_value=adapter):
            self.enricher.web.search_web = Mock(
                side_effect=[
                    self.complete_metrics_result(),
                    self.partial_count_result(),
                ]
            )
            result = self.enricher.enrich_business(
                business_name="The Test Bistro",
                city="Manchester",
                country="United Kingdom",
            )

        self.assertEqual(self.enricher.web.search_web.call_count, 1)
        self.assertEqual(result.review_count, 95)
        self.assertEqual(result.rating, 4.5)

    def test_search_stops_after_low_review_count_instead_of_spending_second_query(self):
        adapter = self.make_adapter()
        low_count_result = [{
            "title": "The Test Bistro, Manchester - Reviews",
            "snippet": "The Test Bistro in Manchester has accumulated over 35 reviews from local diners.",
            "result_url": "https://www.facebook.com/thetestbistro",
            "provider": "Tavily",
        }]
        with patch("lib.country_adapters.get_country_adapter", return_value=adapter):
            self.enricher.web.search_web = Mock(return_value=low_count_result)
            result = self.enricher.enrich_business(
                business_name="The Test Bistro",
                city="Manchester",
                country="United Kingdom",
            )

        self.assertEqual(self.enricher.web.search_web.call_count, 1)
        self.assertEqual(result.review_count, 35)
        self.assertIsNone(result.rating)


if __name__ == "__main__":
    unittest.main()
