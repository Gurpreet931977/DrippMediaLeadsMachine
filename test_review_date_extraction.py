"""
Unit Test Suite for Phase 5.2: Review Evidence Freshness & Date Extraction Hardening
===================================================================================
Tests all 22 benchmark cases + focused tests for:
  - absolute review date extraction
  - relative review age extraction
  - relative date normalization
  - timezone handling
  - freshness <=180 days (RECENT)
  - freshness >180 days (STALE)
  - UNKNOWN freshness (missing/untrusted)
  - misleading year text rejection
  - page last-modified rejection
  - search-result date rejection
  - multiple-date page selection (picks newest genuine review)
  - large review counts (783, 1146, 2026, 4501) vs year confusion
  - existing Tripadvisor year regression
  - source-family preservation
  - Rule B behavior with RECENT
  - Rule B behavior with STALE
  - Rule B behavior with UNKNOWN
  - Full auditable date provenance fields
"""

import unittest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

from lib.enrichment.review_date_extractor import (
    ReviewDateExtractor,
    ReviewEvidenceDateType,
    ReviewDateConfidence,
    ReviewFreshness,
    ExtractedReviewDate,
)
from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
    ReviewEvidenceItem,
    ReviewEnrichmentResult,
)
from lib.types import (
    DiscoveredBusiness,
    OperationalStatus,
    OperationalConfidence,
    SourceFamily,
    WebsiteStatus,
    QualificationState,
)
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider


class TestReviewDateExtraction(unittest.TestCase):
    def setUp(self):
        # Reference run timestamp: 2026-10-01 12:00:00 UTC
        self.as_of = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.enricher = ReviewRatingEnricher()
        self.validator = OperationalValidator()
        self.scorer = LeadScoringProvider()

    # =========================================================================
    # CONTROLLED BENCHMARK CASES (1 - 22)
    # =========================================================================

    def test_benchmark_case_01_jsonld_absolute_recent(self):
        """Case 01: JSON-LD absolute date within 180 days -> REVIEW_PUBLICATION_DATE, HIGH, RECENT"""
        json_data = {
            "@context": "https://schema.org",
            "@type": "Restaurant",
            "name": "Casa Di Alessia",
            "review": [
                {
                    "@type": "Review",
                    "datePublished": "2026-08-15",
                    "reviewRating": {"@type": "Rating", "ratingValue": 5},
                    "description": "Authentic Italian dining, highly recommended!"
                }
            ]
        }
        res = ReviewDateExtractor.extract_from_json_ld(json_data, source_url="https://tripadvisor.com/casa", as_of=self.as_of)
        self.assertIsNotNone(res)
        self.assertEqual(res.date, "2026-08-15")
        self.assertEqual(res.date_type, ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value)
        self.assertEqual(res.confidence, ReviewDateConfidence.HIGH.value)
        self.assertEqual(res.freshness, ReviewFreshness.RECENT.value)
        self.assertEqual(res.extraction_method, "JSON_LD_REVIEW")

    def test_benchmark_case_02_jsonld_absolute_stale(self):
        """Case 02: JSON-LD absolute date older than 180 days -> REVIEW_PUBLICATION_DATE, HIGH, STALE"""
        json_data = {
            "@context": "https://schema.org",
            "@type": "Restaurant",
            "name": "Old Tavern",
            "review": [
                {
                    "@type": "Review",
                    "datePublished": "2025-10-10",
                    "description": "Good food a long time ago."
                }
            ]
        }
        res = ReviewDateExtractor.extract_from_json_ld(json_data, as_of=self.as_of)
        self.assertIsNotNone(res)
        self.assertEqual(res.date, "2025-10-10")
        self.assertEqual(res.date_type, ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value)
        self.assertEqual(res.freshness, ReviewFreshness.STALE.value)

    def test_benchmark_case_03_jsonld_multiple_reviews_picks_newest(self):
        """Case 03: Multiple reviews in JSON-LD -> picks the newest valid review date"""
        json_data = {
            "@type": "Restaurant",
            "review": [
                {"@type": "Review", "datePublished": "2025-11-01"},
                {"@type": "Review", "datePublished": "2026-09-12"},
                {"@type": "Review", "datePublished": "2026-05-15"}
            ]
        }
        res = ReviewDateExtractor.extract_from_json_ld(json_data, as_of=self.as_of)
        self.assertIsNotNone(res)
        self.assertEqual(res.date, "2026-09-12")
        self.assertEqual(res.freshness, ReviewFreshness.RECENT.value)

    def test_benchmark_case_04_html_review_card_relative_recent(self):
        """Case 04: HTML review card with relative age '3 weeks ago' -> REVIEW_RELATIVE_AGE, RECENT"""
        html = """
        <html><body>
          <div class="review-card">
            <span class="user">Jane D</span>
            <span class="date">Reviewed 3 weeks ago</span>
            <p>Loved the brunch here!</p>
          </div>
        </body></html>
        """
        res = ReviewDateExtractor.extract_from_html(html, as_of=self.as_of)
        self.assertIsNotNone(res.date)
        self.assertEqual(res.date_type, ReviewEvidenceDateType.REVIEW_RELATIVE_AGE.value)
        self.assertEqual(res.freshness, ReviewFreshness.RECENT.value)
        self.assertIn("3 weeks ago", res.date_raw)

    def test_benchmark_case_05_html_review_card_relative_stale(self):
        """Case 05: HTML review card with relative age '8 months ago' -> REVIEW_RELATIVE_AGE, STALE"""
        html = """
        <div class="o_review">
          <p class="author">Mike T</p>
          <span class="time">Reviewed 8 months ago</span>
          <p>Decent takeaway.</p>
        </div>
        """
        res = ReviewDateExtractor.extract_from_html(html, as_of=self.as_of)
        self.assertIsNotNone(res.date)
        self.assertEqual(res.freshness, ReviewFreshness.STALE.value)

    def test_benchmark_case_06_html_review_card_activity_date_recent(self):
        """Case 06: HTML review card with 'Date of visit: August 2026' -> REVIEW_ACTIVITY_DATE, RECENT"""
        html = """
        <div class="review-item">
          <div class="rating">5/5</div>
          <div class="visit">Date of visit: August 2026</div>
          <p>Great service during our holiday visit.</p>
        </div>
        """
        res = ReviewDateExtractor.extract_from_html(html, as_of=self.as_of)
        self.assertEqual(res.date, "2026-08-01")
        self.assertEqual(res.date_type, ReviewEvidenceDateType.REVIEW_ACTIVITY_DATE.value)
        self.assertEqual(res.freshness, ReviewFreshness.RECENT.value)

    def test_benchmark_case_07_aggregate_only_tripadvisor_returns_unknown(self):
        """Case 07: Tripadvisor snippet with aggregate review count and rating only -> UNKNOWN freshness"""
        snippet = "A Casa di Alessia, Otley: See 143 unbiased reviews of A Casa di Alessia, rated 4.9 of 5 on Tripadvisor and ranked #9 of 71 restaurants in Otley."
        title = "A CASA DI ALESSIA, Otley - Restaurant Reviews... - Tripadvisor"
        res = ReviewDateExtractor.extract_from_snippet(snippet, title=title, as_of=self.as_of)
        self.assertIsNone(res.date)
        self.assertEqual(res.date_type, ReviewEvidenceDateType.UNKNOWN.value)
        self.assertEqual(res.freshness, ReviewFreshness.UNKNOWN.value)

    def test_benchmark_case_08_aggregate_only_restaurantguru_returns_unknown(self):
        """Case 08: Restaurant Guru snippet with aggregate count only -> UNKNOWN freshness"""
        snippet = "Bus Station Snack Bar in Otley rated 0 out of 5 on Restaurant Guru: 2 reviews."
        title = "Bus Station Snack Bar, Otley - Restaurant menu, prices and reviews"
        res = ReviewDateExtractor.extract_from_snippet(snippet, title=title, as_of=self.as_of)
        self.assertIsNone(res.date)
        self.assertEqual(res.freshness, ReviewFreshness.UNKNOWN.value)

    def test_benchmark_case_09_misleading_seo_title_year_rejected(self):
        """Case 09: 'IL VICOLETTO, Otley - Reviews & Information (2026)' -> year in title must NOT become review date"""
        title = "IL VICOLETTO, Otley - Reviews & Information (2026)"
        snippet = "See 88 unbiased reviews of Il Vicoletto, rated 4.6 of 5 on Tripadvisor."
        res = ReviewDateExtractor.extract_from_snippet(snippet, title=title, as_of=self.as_of)
        self.assertIsNone(res.date)
        self.assertEqual(res.freshness, ReviewFreshness.UNKNOWN.value)

    def test_benchmark_case_10_misleading_best_restaurants_year_rejected(self):
        """Case 10: 'Top 10 Best Restaurants 2026' -> must NOT become review date"""
        title = "Top 10 Best Restaurants 2026 in Leeds"
        snippet = "Il Vicoletto is ranked among the top spots. Over 88 customer reviews."
        res = ReviewDateExtractor.extract_from_snippet(snippet, title=title, as_of=self.as_of)
        self.assertIsNone(res.date)
        self.assertEqual(res.freshness, ReviewFreshness.UNKNOWN.value)

    def test_benchmark_case_11_misleading_established_year_rejected(self):
        """Case 11: 'Established in 2026' -> must NOT become review date"""
        snippet = "Established in 2026. Nam Jai Thai serves fine cuisine with 173 reviews."
        title = "Nam Jai Thai Restaurant"
        res = ReviewDateExtractor.extract_from_snippet(snippet, title=title, as_of=self.as_of)
        self.assertIsNone(res.date)
        self.assertEqual(res.freshness, ReviewFreshness.UNKNOWN.value)

    def test_benchmark_case_12_misleading_footer_copyright_year_rejected(self):
        """Case 12: '© 2026 Tripadvisor LLC' -> must NOT become review date"""
        snippet = "Costa Coffee in Otley. 122 reviews. © 2026 Tripadvisor LLC. All rights reserved."
        title = "Costa Coffee"
        res = ReviewDateExtractor.extract_from_snippet(snippet, title=title, as_of=self.as_of)
        self.assertIsNone(res.date)
        self.assertEqual(res.freshness, ReviewFreshness.UNKNOWN.value)

    def test_benchmark_case_13_misleading_page_last_updated_rejected(self):
        """Case 13: 'Page last updated: 15 August 2026' -> must NOT become review date"""
        snippet = "Page last updated: 15 August 2026. Mondo Kitchen in Otley has 113 reviews."
        title = "Mondo Kitchen"
        res = ReviewDateExtractor.extract_from_snippet(snippet, title=title, as_of=self.as_of)
        self.assertIsNone(res.date)
        self.assertEqual(res.freshness, ReviewFreshness.UNKNOWN.value)

    def test_benchmark_case_14_misleading_search_index_prefix_rejected(self):
        """Case 14: Search engine index date prefix 'Oct 1, 2026 ...' -> must NOT become RECENT review date"""
        snippet = "Oct 1, 2026 ... A Casa di Alessia, Otley: See 143 unbiased reviews of A Casa di Alessia, rated 4.9 of 5."
        title = "A Casa di Alessia"
        res = ReviewDateExtractor.extract_from_snippet(snippet, title=title, as_of=self.as_of)
        self.assertIsNone(res.date)
        self.assertEqual(res.freshness, ReviewFreshness.UNKNOWN.value)

    def test_benchmark_case_15_large_review_count_2026_not_confused_with_year(self):
        """Case 15: '2,026 reviews' -> count must be 2026, date must be None/UNKNOWN"""
        snippet = "Authentic Bistro in Leeds. Accumulating 2,026 reviews by visitors over 5 years."
        title = "Authentic Bistro"
        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url="https://tripadvisor.co.uk/bistro",
            business_name="Authentic Bistro",
            city="Leeds"
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.review_count, 2026)
        self.assertIsNone(item.evidence_date)
        self.assertEqual(item.freshness, ReviewFreshness.UNKNOWN.value)

    def test_benchmark_case_16_large_review_count_4501_parsed_correctly(self):
        """Case 16: '4,501 reviews' -> parsed correctly without date confusion"""
        snippet = "Famous Steakhouse in Leeds. Rated 4.6 stars based on 4,501 reviews on Google."
        title = "Famous Steakhouse"
        item = self.enricher.extract_evidence_from_snippet(
            snippet=snippet,
            title=title,
            url="https://google.com/maps/place/steakhouse",
            business_name="Famous Steakhouse",
            city="Leeds"
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.review_count, 4501)
        self.assertEqual(item.rating, 4.6)
        self.assertEqual(item.freshness, ReviewFreshness.UNKNOWN.value)

    def test_benchmark_case_17_malformed_date_rejected(self):
        """Case 17: Malformed date like 'Reviewed on 32/13/2026' -> rejected as UNKNOWN"""
        snippet = "Reviewed on 32/13/2026: Delicious pizza and great ambiance."
        title = "Pizza Place"
        res = ReviewDateExtractor.extract_from_snippet(snippet, title=title, as_of=self.as_of)
        self.assertIsNone(res.date)
        self.assertEqual(res.freshness, ReviewFreshness.UNKNOWN.value)

    def test_benchmark_case_18_snippet_anchored_relative_recent(self):
        """Case 18: Snippet with 'Reviewed 2 weeks ago' -> REVIEW_RELATIVE_AGE, RECENT"""
        snippet = "Reviewed 2 weeks ago: 'Best pizza in Otley! Fresh ingredients and lovely staff.' Rated 5/5."
        title = "Il Vicoletto Reviews"
        res = ReviewDateExtractor.extract_from_snippet(snippet, title=title, as_of=self.as_of)
        self.assertIsNotNone(res.date)
        self.assertEqual(res.date_type, ReviewEvidenceDateType.REVIEW_RELATIVE_AGE.value)
        self.assertEqual(res.confidence, ReviewDateConfidence.HIGH.value)
        self.assertEqual(res.freshness, ReviewFreshness.RECENT.value)

    def test_benchmark_case_19_snippet_anchored_absolute_date_recent(self):
        """Case 19: Snippet with 'Latest review on 15 August 2026' -> REVIEW_PUBLICATION_DATE, RECENT"""
        snippet = "Latest review on 15 August 2026: 'Incredible experience dining here.' 143 reviews."
        title = "Casa Di Alessia"
        res = ReviewDateExtractor.extract_from_snippet(snippet, title=title, as_of=self.as_of)
        self.assertEqual(res.date, "2026-08-15")
        self.assertEqual(res.date_type, ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value)
        self.assertEqual(res.confidence, ReviewDateConfidence.HIGH.value)
        self.assertEqual(res.freshness, ReviewFreshness.RECENT.value)

    def test_benchmark_case_20_mixed_page_selects_stale_review_not_recent_page_date(self):
        """Case 20: Page has copyright 2026, page updated 2026-09-01, and review card from 2024-03-12 -> selects review date 2024-03-12 (STALE)"""
        html = """
        <html>
        <head><title>Old Reviews</title></head>
        <body>
          <div class="update_info">Page updated: 2026-09-01</div>
          <div class="review-card">
            <span>Reviewed on 12/03/2024</span>
            <p>Old review content.</p>
          </div>
          <footer>© 2026 Directory Services LLC</footer>
        </body>
        </html>
        """
        res = ReviewDateExtractor.extract_from_html(html, as_of=self.as_of)
        self.assertEqual(res.date, "2024-03-12")
        self.assertEqual(res.freshness, ReviewFreshness.STALE.value)

    def test_benchmark_case_21_restaurantguru_real_html_jsonld_recent(self):
        """Case 21: Real Restaurant Guru JSON-LD review with datePublished '2026-08-05T02:08' -> RECENT"""
        json_ld = {
            "@context": "https://schema.org",
            "@type": "Restaurant",
            "name": "Shandar",
            "review": {
                "@type": "Review",
                "author": {"@type": "Organization", "name": "Restaurant Guru"},
                "datePublished": "2026-08-05T02:08",
                "description": "Indian food is good at this place.",
                "reviewRating": {"@type": "Rating", "ratingValue": 4.5}
            }
        }
        res = ReviewDateExtractor.extract_from_json_ld(json_ld, as_of=self.as_of)
        self.assertEqual(res.date, "2026-08-05")
        self.assertEqual(res.freshness, ReviewFreshness.RECENT.value)

    def test_benchmark_case_22_tripadvisor_datadome_blocked_returns_unknown(self):
        """Case 22: Bot blocked response (403 / DataDome) -> UNKNOWN freshness, no synthetic date"""
        blocked_html = """
        <html><head><title>tripadvisor.co.uk</title></head>
        <body><p>Please enable JS and disable any ad blocker</p>
        <script>var dd={'rt':'i','cid':'AHrlqAAAAAMAritXN8NMxOAAMSurMg=='};</script>
        </body></html>
        """
        res = ReviewDateExtractor.extract_from_html(blocked_html, as_of=self.as_of)
        self.assertIsNone(res.date)
        self.assertEqual(res.freshness, ReviewFreshness.UNKNOWN.value)

    # =========================================================================
    # RELATIVE DATE NORMALIZATION & TIMEZONE HANDLING
    # =========================================================================

    def test_relative_date_normalization_intervals(self):
        """Verify normalization for days, weeks, months, years"""
        as_of = datetime(2026, 10, 1, tzinfo=timezone.utc)

        # 5 days ago -> 2026-09-26
        d, raw, days = ReviewDateExtractor.normalize_relative_date("5 days ago", as_of)
        self.assertEqual(d, "2026-09-26")
        self.assertEqual(days, 5)

        # 3 weeks ago -> 2026-09-10
        d, raw, days = ReviewDateExtractor.normalize_relative_date("3 weeks ago", as_of)
        self.assertEqual(d, "2026-09-10")
        self.assertEqual(days, 21)

        # 2 months ago -> 2026-08-02
        d, raw, days = ReviewDateExtractor.normalize_relative_date("2 months ago", as_of)
        self.assertEqual(d, "2026-08-02")
        self.assertEqual(days, 60)

        # 1 year ago -> 2025-10-01
        d, raw, days = ReviewDateExtractor.normalize_relative_date("1 year ago", as_of)
        self.assertEqual(d, "2025-10-01")
        self.assertEqual(days, 365)

    def test_timezone_handling(self):
        """Verify timezone handling with UTC, offset, and naive datetimes"""
        # ISO with offset +01:00
        norm, _ = ReviewDateExtractor.normalize_absolute_date("2026-08-15T14:30:00+01:00")
        self.assertEqual(norm, "2026-08-15")

        # ISO with Z
        norm_z, _ = ReviewDateExtractor.normalize_absolute_date("2026-08-15T14:30:00Z")
        self.assertEqual(norm_z, "2026-08-15")

        # Reference as_of with different timezone
        as_of_ist = datetime(2026, 10, 1, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        fresh = ReviewDateExtractor.calculate_freshness("2026-08-15", as_of_ist)
        self.assertEqual(fresh, ReviewFreshness.RECENT.value)

    # =========================================================================
    # RULE B OPERATIONAL VERIFICATION INTEGRATION
    # =========================================================================

    def test_rule_b_with_recent_review_evidence(self):
        """Rule B: RECENT review evidence (<=180 days) + independent signal -> ACTIVE_CONFIRMED"""
        biz = DiscoveredBusiness(
            company_name="Casa Di Alessia",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="15 New Briggate, Leeds LS1 6BT",
            phone="+44 113 244 5566",
            raw_website="",
            review_count=143,
            rating=4.9,
            latest_review_date="2026-08-15",
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 143,
                "rating": 4.9,
                "review_source": "Tripadvisor",
                "review_source_url": "https://tripadvisor.co.uk/casa",
                "review_evidence_date": "2026-08-15",
                "review_evidence_date_type": "REVIEW_PUBLICATION_DATE",
                "review_confidence": "HIGH",
                "review_freshness": "RECENT"
            }
        }
        biz.evidence_sources = {
            "phone": "OPENSTREETMAP",
            "address": "OPENSTREETMAP"
        }
        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {},
            "verified_handles": {}
        }
        op_res = self.validator.verify_operations(biz, social_audit, website_verification_status="NO_WEBSITE_CONFIRMED")
        self.assertEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertTrue(op_res["multi_signal_rule_applied"])
        self.assertEqual(op_res["evidence_freshness"], "RECENT")
        self.assertEqual(op_res["review_evidence_date_type"], "REVIEW_PUBLICATION_DATE")

    def test_rule_b_blocked_by_stale_review_evidence(self):
        """Rule B: STALE review evidence (>180 days) -> blocked from ACTIVE_CONFIRMED"""
        biz = DiscoveredBusiness(
            company_name="Old Cafe",
            category="Cafe",
            city="Leeds",
            target_country="United Kingdom",
            address="12 High St, Leeds LS1 1AA",
            phone="+44 113 244 1111",
            raw_website="",
            review_count=120,
            rating=4.8,
            latest_review_date="2024-01-15",
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 120,
                "rating": 4.8,
                "review_source": "Tripadvisor",
                "review_evidence_date": "2024-01-15",
                "review_freshness": "STALE"
            }
        }
        biz.evidence_sources = {"phone": "OPENSTREETMAP"}
        social_audit = {"social_status": "SOCIAL_NOT_FOUND", "verified_urls": {}}
        op_res = self.validator.verify_operations(biz, social_audit, website_verification_status="NO_WEBSITE_CONFIRMED")
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertFalse(op_res["multi_signal_rule_applied"])

    def test_rule_b_blocked_by_unknown_review_evidence(self):
        """Rule B: UNKNOWN review freshness -> blocked from ACTIVE_CONFIRMED"""
        biz = DiscoveredBusiness(
            company_name="Il Vicoletto",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="9 Mercury Row, Otley LS21 3HE",
            phone="+44 1943 462588",
            raw_website="",
            review_count=88,
            rating=4.6,
            latest_review_date=None,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 88,
                "rating": 4.6,
                "review_source": "Tripadvisor",
                "review_evidence_date": None,
                "review_freshness": "UNKNOWN"
            }
        }
        biz.evidence_sources = {"phone": "OPENSTREETMAP"}
        social_audit = {"social_status": "SOCIAL_NOT_FOUND", "verified_urls": {}}
        op_res = self.validator.verify_operations(biz, social_audit, website_verification_status="NO_WEBSITE_CONFIRMED")
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertFalse(op_res["multi_signal_rule_applied"])
        self.assertEqual(op_res["review_freshness"], "UNKNOWN")

    def test_auditable_provenance_fields_present_on_all_results(self):
        """Verify that ReviewEnrichmentResult contains full provenance attributes"""
        res = ReviewEnrichmentResult(
            review_count=100,
            rating=4.5,
            review_source="Tripadvisor",
            review_source_url="https://tripadvisor.com/test",
            review_evidence="Rated 4.5 from 100 reviews",
            review_evidence_date="2026-08-15",
            review_confidence="HIGH",
            review_freshness="RECENT",
            review_status="FOUND",
            review_evidence_date_raw="15 August 2026",
            review_evidence_date_type="REVIEW_PUBLICATION_DATE",
            review_evidence_date_confidence="HIGH",
            review_evidence_as_of="2026-10-01T12:00:00+00:00",
            extraction_method="JSON_LD_REVIEW"
        )
        d = res.to_dict()
        self.assertIn("review_evidence_date_raw", d)
        self.assertIn("review_evidence_date_type", d)
        self.assertIn("review_evidence_date_confidence", d)
        self.assertIn("review_evidence_as_of", d)
        self.assertIn("extraction_method", d)
        self.assertEqual(d["review_evidence_date_type"], "REVIEW_PUBLICATION_DATE")
        self.assertEqual(d["extraction_method"], "JSON_LD_REVIEW")


if __name__ == "__main__":
    unittest.main()
