#!/usr/bin/env python3
"""
Unit Test Suite for Free-First Review & Rating Enrichment Layer
===============================================================
Covers the 12 required test conditions:
  1. exact business + review count
  2. exact business + rating
  3. wrong business with similar name
  4. wrong branch
  5. missing reviews
  6. missing rating
  7. conflicting review counts
  8. conflicting ratings
  9. stale evidence
  10. unknown evidence date
  11. review evidence without business identity
  12. search result with business name but unrelated review count
"""

import unittest
from datetime import datetime, timezone

from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
    ReviewEvidenceItem,
    ReviewEnrichmentResult,
    ReviewConfidence,
    ReviewFreshness,
    ReviewStatus
)
from lib.types import DiscoveredBusiness, QualificationState, VerificationStatus
from lib.qualification.lead_scoring import LeadScoringProvider


class TestReviewRatingEnricher(unittest.TestCase):

    def setUp(self):
        self.enricher = ReviewRatingEnricher()
        self.scorer = LeadScoringProvider()

    # 1. Exact business + review count
    def test_01_exact_business_plus_review_count(self):
        title = "Against The Grain - Yeadon"
        snippet = "Visit Against The Grain craft beer bar in Yeadon. Rated by customers with 165 reviews on Google."
        url = "https://www.google.com/maps/place/Against+The+Grain"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Against The Grain",
            city="Yeadon",
            postcode="LS19 7TA"
        )
        self.assertIsNotNone(item)
        self.assertIsNone(item.reject_reason)
        self.assertEqual(item.review_count, 165)
        self.assertEqual(item.source, "Google")
        self.assertIn(item.confidence, [ReviewConfidence.HIGH.value, ReviewConfidence.MEDIUM.value])

    # 2. Exact business + rating
    def test_02_exact_business_plus_rating(self):
        title = "Raffertys Cafe, Otley - Restaurant Reviews & Photos"
        snippet = "Raffertys Cafe in Otley is rated 4.8 out of 5 stars based on 320 reviews on Tripadvisor."
        url = "https://www.tripadvisor.co.uk/Restaurant_Review-Raffertys_Cafe-Otley.html"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Raffertys Cafe",
            city="Otley",
            postcode="LS21 3AE"
        )
        self.assertIsNotNone(item)
        self.assertIsNone(item.reject_reason)
        self.assertEqual(item.rating, 4.8)
        self.assertEqual(item.review_count, 320)
        self.assertEqual(item.source, "Tripadvisor")
        self.assertEqual(item.confidence, ReviewConfidence.HIGH.value)

    # 3. Wrong business with similar name
    def test_03_wrong_business_with_similar_name(self):
        # Target is "The Grain", but snippet is about completely different "Against The Grain"
        title = "Against The Grain Bar & Kitchen"
        snippet = "Against The Grain Bar & Kitchen in Yeadon is an established craft bar with 180 reviews."
        url = "https://www.tripadvisor.co.uk/Against_The_Grain.html"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Tokyo Ramen Hub",
            city="Leeds",
            postcode="LS1 2AB"
        )
        # Should be rejected due to missing business identity
        self.assertIsNotNone(item)
        self.assertEqual(item.reject_reason, "MISSING_BUSINESS_IDENTITY")

    # 4. Wrong branch (multi-branch collision)
    def test_04_wrong_branch(self):
        # Target is in Leeds, but snippet is for the Manchester branch
        title = "Gaucho Manchester — Steak Restaurant Reviews"
        snippet = "Gaucho Manchester on 2A St Mary's Street, Manchester. Rated 4.6 stars from 1,450 reviews."
        url = "https://www.tripadvisor.co.uk/Restaurant_Review-Gaucho_Manchester.html"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Gaucho",
            city="Leeds",
            postcode="LS1 1HA"
        )
        self.assertIsNotNone(item)
        self.assertIn("WRONG_BRANCH_LOCATION", item.reject_reason)

    # 5. Missing reviews
    def test_05_missing_reviews(self):
        title = "WinneBagel Cafe Otley"
        snippet = "WinneBagel Cafe in Otley offers delicious artisanal bagels and coffee in West Yorkshire."
        url = "https://www.localfoodguide.co.uk/winnebagel"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="WinneBagel Cafe",
            city="Otley"
        )
        self.assertIsNone(item)

    # 6. Missing rating (review count only)
    def test_06_missing_rating(self):
        title = "Secret Garden Cafe - Otley"
        snippet = "Secret Garden Cafe in Otley has accumulated over 85 reviews from local breakfast patrons."
        url = "https://www.facebook.com/secretgardencafeotley/"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Secret Garden Cafe",
            city="Otley"
        )
        self.assertIsNotNone(item)
        self.assertIsNone(item.reject_reason)
        self.assertEqual(item.review_count, 85)
        self.assertIsNone(item.rating)
        self.assertEqual(item.confidence, ReviewConfidence.MEDIUM.value)

    # 7. Conflicting review counts
    def test_07_conflicting_review_counts(self):
        item_a = ReviewEvidenceItem(
            review_count=300,
            rating=4.5,
            source="Source A",
            source_url="https://source-a.com/biz",
            evidence_text="Rated 4.5 based on 300 reviews",
            confidence=ReviewConfidence.HIGH.value,
            freshness=ReviewFreshness.RECENT.value
        )
        item_b = ReviewEvidenceItem(
            review_count=920,
            rating=4.7,
            source="Source B",
            source_url="https://source-b.com/biz",
            evidence_text="Rated 4.7 based on 920 reviews",
            confidence=ReviewConfidence.HIGH.value,
            freshness=ReviewFreshness.RECENT.value
        )

        result = self.enricher.resolve_best_evidence([item_a, item_b])
        self.assertEqual(result.review_status, ReviewStatus.CONFLICT_REQUIRES_REVIEW.value)
        self.assertEqual(result.review_confidence, ReviewConfidence.CONFLICT.value)
        self.assertIsNone(result.review_count)
        self.assertIsNone(result.rating)
        self.assertEqual(len(result.conflicts), 2)

    # 8. Conflicting ratings
    def test_08_conflicting_ratings(self):
        item_a = ReviewEvidenceItem(
            review_count=150,
            rating=4.8,
            source="Source A",
            source_url="https://source-a.com/biz",
            evidence_text="Rated 4.8 out of 5 stars (150 reviews)",
            confidence=ReviewConfidence.HIGH.value,
            freshness=ReviewFreshness.RECENT.value
        )
        item_b = ReviewEvidenceItem(
            review_count=145,
            rating=3.2,
            source="Source B",
            source_url="https://source-b.com/biz",
            evidence_text="Rated 3.2 out of 5 stars (145 reviews)",
            confidence=ReviewConfidence.HIGH.value,
            freshness=ReviewFreshness.RECENT.value
        )

        result = self.enricher.resolve_best_evidence([item_a, item_b])
        self.assertEqual(result.review_status, ReviewStatus.CONFLICT_REQUIRES_REVIEW.value)
        self.assertEqual(result.review_confidence, ReviewConfidence.CONFLICT.value)
        self.assertEqual(len(result.conflicts), 2)

    # 9. Stale evidence
    def test_09_stale_evidence(self):
        title = "Puffin Pottery Cafe - Otley"
        # Date is from 2024-01-15 (older than 180 days relative to 2026-10-01)
        snippet = "2024-01-15 — Puffin Pottery Cafe in Otley. Rated 4.9 stars from 145 reviews."
        url = "https://www.tripadvisor.co.uk/puffinpottery"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Puffin Pottery",
            city="Otley"
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.freshness, ReviewFreshness.STALE.value)
        self.assertEqual(item.evidence_date, "2024-01-15")
        # Stale evidence confidence must be capped at LOW
        self.assertEqual(item.confidence, ReviewConfidence.LOW.value)

    # 10. Unknown evidence date
    def test_10_unknown_evidence_date(self):
        title = "Namjai Thai Restaurant Otley"
        snippet = "Authentic Thai food at Namjai Thai Restaurant in Otley. Rated 4.8 stars based on 195 reviews."
        url = "https://www.facebook.com/namjaithaiotley/"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Namjai Thai",
            city="Otley"
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.freshness, ReviewFreshness.UNKNOWN.value)
        self.assertIsNone(item.evidence_date)

    # 11. Review evidence without business identity
    def test_11_review_evidence_without_business_identity(self):
        title = "Best Casual Dining and Drinks in West Yorkshire"
        snippet = "Rated 4.8/5 based on 250 reviews for great coffee and food."
        url = "https://www.bestofuk.co.uk/casual-dining"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Shandar",
            city="Bradford"
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.reject_reason, "MISSING_BUSINESS_IDENTITY")

    # 12. Search result with business name but unrelated review count
    def test_12_search_result_with_business_name_but_unrelated_review_count(self):
        title = "Local Dining Guide — Gain Lane"
        snippet = "Located opposite Shandar, visit the Bradford Industrial Museum (1,500 reviews, 4.7 stars) for local history."
        url = "https://www.visitbradford.com/attractions"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Shandar",
            city="Bradford"
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.reject_reason, "UNRELATED_ENTITY_REVIEW_COUNT")

    # 13. Pipeline Qualification V3 integration with enriched values
    def test_13_qualification_integration_unknown_reviews(self):
        # When review data is None (missing/unknown), must route to RESEARCH_ONLY
        biz = DiscoveredBusiness(
            company_name="Mystery Kitchen",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            raw_website="",
            review_count=None,  # Missing / unknown
            rating=None
        )
        audit = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)
    # 14. Directory SEO title containing current/upcoming year (e.g. 2026 Reviews & Information)
    def test_14_seo_year_in_title_not_parsed_as_review_count(self):
        title = "ORIGINAL PATTY MEN, Birmingham - 2026 Reviews & Information"
        snippet = "Visit Original Patty Men in Birmingham for craft burgers. Rated 4.5 stars based on 783 reviews on Tripadvisor."
        url = "https://www.tripadvisor.co.uk/Restaurant_Review-Original_Patty_Men-Birmingham.html"

        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url=url,
            business_name="Original Patty Men",
            city="Birmingham"
        )
        self.assertIsNotNone(item)
        self.assertIsNone(item.reject_reason)
        self.assertEqual(item.review_count, 783)
        self.assertEqual(item.rating, 4.5)
        self.assertEqual(item.source, "Tripadvisor")


    # 15. SEO title patterns like '2026 Reviews & Info / Photos / Deals' with no other count must NOT return 2026
    def test_15_seo_year_patterns_without_secondary_count_return_none(self):
        cases = [
            ("Paradise Balti", "Leeds", "Order takeaway and delivery at Paradise Balti & Pizza Leeds", "PARADISE BALTI & PIZZA LEEDS, Yeadon - 2026 Reviews & Info"),
            ("Wok Away", "Otley", "Otley Restaurants.1. WOK A WAY Chinese takeaway. At a glance.", "WOK A WAY CHINESE TAKEAWAY, Otley - 2026 Reviews & Info"),
            ("Otley Cafe", "Otley", "Great local cafe in Otley with hot coffee", "Otley Cafe - 2025 Reviews & Photos"),
            ("Leeds Bistro", "Leeds", "Delicious French dining in central Leeds", "Leeds Bistro - 2026 Reviews & Deals"),
            ("Leeds Bistro", "Leeds", "French bistro in Leeds with outdoor seating", "Leeds Bistro - 2026 Reviews & Information")
        ]

        for bname, city, snippet, title in cases:
            item = self.enricher.extract_evidence_from_snippet(
                snippet=snippet,
                title=title,
                url="https://www.tripadvisor.co.uk/Restaurant_Review-test.html",
                business_name=bname,
                city=city
            )
            # Either item is None or review_count is None (never 2025 or 2026)
            rc = item.review_count if item else None
            self.assertIsNone(rc, f"Expected None for SEO title '{title}', got {rc}")

    # 16. Genuine 4-digit and multi-digit review counts remain valid
    def test_16_genuine_four_digit_review_counts_remain_valid(self):
        cases = [
            ("Good Diner", "Leeds", "See 2,026 reviews from guests across the UK", 2026),
            ("Good Diner 2", "Leeds", "Based on 783 reviews on Google", 783),
            ("Good Diner 3", "Leeds", "Rated 4.5 from 1,357 reviews", 1357),
            ("Big Chain", "Leeds", "Over 10,245 reviews from satisfied customers", 10245),
            ("Authentic Bistro", "Leeds", "Accumulating 2026 reviews by visitors over 5 years", 2026)
        ]

        for bname, city, snippet, expected_count in cases:
            item = self.enricher.extract_evidence_from_snippet(
                snippet=snippet,
                title=f"{bname}, {city} Restaurant",
                url="https://www.tripadvisor.co.uk/Restaurant_Review-test.html",
                business_name=bname,
                city=city
            )
            self.assertIsNotNone(item, f"Expected valid item for '{bname}'")
            self.assertEqual(item.review_count, expected_count, f"Expected {expected_count} for snippet '{snippet}', got {item.review_count}")


if __name__ == "__main__":
    unittest.main()

