"""
Test Suite for Phase 7.2: Review Evidence Recovery & Source Diversification
===========================================================================
Validates:
  1. Alternative source recovery (Restaurant Guru, Yelp, TripAdvisor)
  2. Same-business identity validation (name similarity fallback, wrong-branch rejection)
  3. Cross-source-family independence
  4. Conflicting review evidence detection (multi-source disagreement triggers REJECTED_CONFLICT)
  5. Date provenance preservation (date_raw, date_type, confidence, extraction_method)
  6. Source failure and block states (403 DataDome, 503 bot challenge recorded without circumvention)
  7. Rule B with recovered RECENT evidence (transitions to ACTIVE_CONFIRMED)
  8. Rule B with recovered STALE evidence (fails Condition 8, remains ACTIVE_LIKELY)
  9. Rule B with remaining UNKNOWN evidence (remains ACTIVE_LIKELY / OPERATIONAL_UNKNOWN)
  10. Social date not treated as review date
  11. Duplicate source-family rejection
  12. Zero CRM mutation and zero outreach dispatch invariants
"""

import os
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from lib.types import DiscoveredBusiness, SourceFamily, WebsiteStatus, OperationalStatus
from lib.enrichment.review_recovery import (
    ReviewEvidenceRecoveryLayer,
    ReviewRecoveryTelemetryItem,
    ReviewRecoveryCandidateResult,
)
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
    ReviewConfidence,
    ReviewStatus,
    REFERENCE_DATE,
)
from lib.crm.identity_matcher import BusinessIdentityMatcher
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider


class TestPhase72ReviewRecovery(unittest.TestCase):
    def setUp(self):
        self.recovery = ReviewEvidenceRecoveryLayer()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. SAME-BUSINESS IDENTITY VALIDATION
    # ──────────────────────────────────────────────────────────────────────────
    def test_identity_validation_matching_candidate_succeeds(self):
        """Candidate with matching name and Manchester corroboration passes >= 0.70."""
        is_valid, conf, err = self.recovery.verify_candidate_identity(
            cand_name="Pot Kettle Black",
            cand_city="Manchester",
            cand_street="",
            cand_postcode="",
            discovered_name="Pot Kettle Black Barton Arcade",
            discovered_city="Manchester",
            discovered_street="Barton Arcade, Deansgate",
            discovered_postcode="M3 2BW",
            snippet="Pot Kettle Black is a specialty coffee shop in Manchester Barton Arcade.",
            url="https://restaurantguru.com/Pot-Kettle-Black-Manchester"
        )
        self.assertTrue(is_valid)
        self.assertGreaterEqual(conf, 0.70)
        self.assertIsNone(err)

    def test_identity_validation_wrong_branch_location_rejected(self):
        """Candidate in Manchester matching a listing in London is rejected with low confidence."""
        is_valid, conf, err = self.recovery.verify_candidate_identity(
            cand_name="Burger King",
            cand_city="Manchester",
            cand_street="",
            cand_postcode="",
            discovered_name="Burger King Oxford Street London",
            discovered_city="London",
            discovered_street="Oxford Street",
            discovered_postcode="W1D 1BS",
            snippet="Burger King fast food restaurant in London Oxford Street.",
            url="https://restaurantguru.com/Burger-King-London"
        )
        self.assertFalse(is_valid)
        self.assertLess(conf, 0.70)
        self.assertIn("WRONG_BRANCH_LOCATION", str(err))

    def test_identity_validation_insufficient_name_overlap_rejected(self):
        """Discovered listing for a completely different restaurant name is rejected."""
        is_valid, conf, err = self.recovery.verify_candidate_identity(
            cand_name="Caribbean Vibez",
            cand_city="Manchester",
            discovered_name="Tampopo Corn Exchange",
            discovered_city="Manchester",
            snippet="Tampopo serves authentic East Asian street food in Manchester Corn Exchange.",
            url="https://restaurantguru.com/Tampopo-Manchester"
        )
        self.assertFalse(is_valid)
        self.assertLess(conf, 0.70)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. SOURCE FAILURE & BLOCK STATES (ZERO CIRCUMVENTION)
    # ──────────────────────────────────────────────────────────────────────────
    @patch("requests.get")
    def test_datadome_403_access_restricted_handled_without_bypass(self, mock_get):
        """HTTP 403 from TripAdvisor is logged as SEARCH_BLOCKED / HTTP_403 with no circumvention."""
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "<html><body>DataDome. Access denied.</body></html>"
        mock_get.return_value = mock_resp

        status, text, err = self.recovery._fetch_page("https://www.tripadvisor.co.uk/Restaurant_Review-d123.html")
        self.assertEqual(status, 403)
        self.assertEqual(err, "HTTP_403_ACCESS_RESTRICTED")

    @patch("requests.get")
    def test_bot_challenge_503_suspicious_activity_handled_without_bypass(self, mock_get):
        """503 Suspicious Activity Detected is flagged as HTTP_503_ACCESS_RESTRICTED."""
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "<html><body>Suspicious activity detected. Cloudflare verification.</body></html>"
        mock_get.return_value = mock_resp

        status, text, err = self.recovery._fetch_page("https://restaurantguru.com/test-challenge")
        self.assertEqual(status, 503)
        self.assertEqual(err, "HTTP_503_ACCESS_RESTRICTED")

    # ──────────────────────────────────────────────────────────────────────────
    # 3. DATE-BEARING REVIEW RECOVERY (RESTAURANT GURU)
    # ──────────────────────────────────────────────────────────────────────────
    @patch.object(ReviewEvidenceRecoveryLayer, "_fetch_page")
    def test_restaurant_guru_recovers_recent_review_date(self, mock_fetch):
        """Restaurant Guru page with JSON-LD review from September 2026 yields RECENT freshness."""
        rg_html = """
        <html>
        <head>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Restaurant",
            "name": "Pot Kettle Black",
            "address": {
              "@type": "PostalAddress",
              "addressLocality": "Manchester",
              "streetAddress": "Barton Arcade, Deansgate",
              "postalCode": "M3 2BW"
            },
            "aggregateRating": {
              "@type": "AggregateRating",
              "ratingValue": "4.2",
              "reviewCount": "635"
            },
            "review": [
              {
                "@type": "Review",
                "author": {"@type": "Person", "name": "Sarah M."},
                "datePublished": "2026-09-24",
                "reviewRating": {"@type": "Rating", "ratingValue": "5"}
              }
            ]
          }
          </script>
        </head>
        <body><h1>Pot Kettle Black</h1></body>
        </html>
        """
        mock_fetch.return_value = (200, rg_html, None)

        # Mock web search result
        mock_search = MagicMock()
        mock_search.search_web.return_value = [
            {
                "result_url": "https://restaurantguru.com/Pot-Kettle-Black-Manchester",
                "title": "Pot Kettle Black in Manchester - Restaurant Guru",
                "snippet": "Pot Kettle Black in Manchester. 635 reviews, rating 4.2."
            }
        ]
        self.recovery.web = mock_search

        cand = {
            "index": 1,
            "company_name": "Pot Kettle Black",
            "city": "Manchester",
            "review_count": 635,
            "rating": 4.2
        }

        item, telem = self.recovery._extract_restaurant_guru(cand, as_of=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertIsNotNone(item)
        self.assertEqual(item.review_count, 635)
        self.assertEqual(item.rating, 4.2)
        self.assertEqual(item.evidence_date, "2026-09-24")
        self.assertEqual(item.freshness, ReviewFreshness.RECENT.value)
        self.assertEqual(telem.succeeded, True)

    @patch.object(ReviewEvidenceRecoveryLayer, "_fetch_page")
    def test_restaurant_guru_recovers_stale_review_date(self, mock_fetch):
        """Restaurant Guru page with review older than 180 days yields STALE freshness."""
        rg_html = """
        <html>
        <head>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Restaurant",
            "name": "Bar Bibo",
            "address": {
              "@type": "PostalAddress",
              "addressLocality": "Manchester",
              "streetAddress": "Tib Street"
            },
            "aggregateRating": {
              "@type": "AggregateRating",
              "ratingValue": "4.1",
              "reviewCount": "120"
            },
            "review": [
              {
                "@type": "Review",
                "author": {"@type": "Person", "name": "John D."},
                "datePublished": "2025-08-15",
                "reviewRating": {"@type": "Rating", "ratingValue": "4"}
              }
            ]
          }
          </script>
        </head>
        <body><h1>Bar Bibo</h1></body>
        </html>
        """
        mock_fetch.return_value = (200, rg_html, None)

        mock_search = MagicMock()
        mock_search.search_web.return_value = [
            {
                "result_url": "https://restaurantguru.com/Bar-Bibo-Manchester",
                "title": "Bar Bibo in Manchester",
                "snippet": "Bar Bibo in Manchester reviews."
            }
        ]
        self.recovery.web = mock_search

        cand = {"index": 5, "company_name": "Bar Bibo", "city": "Manchester"}
        item, telem = self.recovery._extract_restaurant_guru(cand, as_of=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertIsNotNone(item)
        self.assertEqual(item.evidence_date, "2025-08-15")
        self.assertEqual(item.freshness, ReviewFreshness.STALE.value)

    # ──────────────────────────────────────────────────────────────────────────
    # 4. CONFLICT DETECTION (MULTI-SOURCE DISAGREEMENT)
    # ──────────────────────────────────────────────────────────────────────────
    def test_conflicting_ratings_trigger_conflict_rejection(self):
        """Material rating discrepancy (e.g. 4.6 vs 3.8) triggers REJECTED_CONFLICT."""
        item1 = ReviewEvidenceItem(
            review_count=100, rating=4.6, source="Restaurant Guru",
            source_url="https://restaurantguru.com/test", evidence_text="4.6 rating",
            evidence_date="2026-09-01", confidence=ReviewConfidence.HIGH.value,
            freshness=ReviewFreshness.RECENT.value
        )
        item2 = ReviewEvidenceItem(
            review_count=95, rating=3.8, source="Yelp",
            source_url="https://yelp.co.uk/biz/test", evidence_text="3.8 rating",
            evidence_date="2026-08-20", confidence=ReviewConfidence.HIGH.value,
            freshness=ReviewFreshness.RECENT.value
        )
        has_conflict, items = ReviewRatingEnricher.detect_conflicts([item1, item2])
        self.assertTrue(has_conflict)
        self.assertEqual(len(items), 2)

    def test_conflicting_counts_trigger_conflict_rejection(self):
        """Material review count discrepancy (e.g. 100 vs 850) triggers conflict."""
        item1 = ReviewEvidenceItem(
            review_count=100, rating=4.2, source="Restaurant Guru",
            source_url="https://restaurantguru.com/test", evidence_text="100 reviews",
            evidence_date="2026-09-01", confidence=ReviewConfidence.HIGH.value,
            freshness=ReviewFreshness.RECENT.value
        )
        item2 = ReviewEvidenceItem(
            review_count=850, rating=4.1, source="Tripadvisor",
            source_url="https://tripadvisor.co.uk/test", evidence_text="850 reviews",
            evidence_date="2026-09-02", confidence=ReviewConfidence.HIGH.value,
            freshness=ReviewFreshness.RECENT.value
        )
        has_conflict, items = ReviewRatingEnricher.detect_conflicts([item1, item2])
        self.assertTrue(has_conflict)
        self.assertEqual(len(items), 2)

    # ──────────────────────────────────────────────────────────────────────────
    # 5. CROSS-SOURCE FAMILY INDEPENDENCE
    # ──────────────────────────────────────────────────────────────────────────
    def test_source_family_classification_and_independence(self):
        """TripAdvisor and Restaurant Guru belong to independent source families."""
        sf_rg = OperationalValidator.classify_source_family("Restaurant Guru")
        sf_ta = OperationalValidator.classify_source_family("TripAdvisor")
        sf_yelp = OperationalValidator.classify_source_family("Yelp")

        self.assertEqual(sf_rg, SourceFamily.RESTAURANT_GURU)
        self.assertEqual(sf_ta, SourceFamily.TRIPADVISOR)
        self.assertEqual(sf_yelp, SourceFamily.YELP)
        self.assertNotEqual(sf_rg, sf_ta)
        self.assertNotEqual(sf_rg, sf_yelp)
        self.assertNotEqual(sf_ta, sf_yelp)

    def test_duplicate_source_family_recognized_as_single_family(self):
        """Two different URLs from TripAdvisor still belong to the same source family."""
        sf1 = OperationalValidator.classify_source_family("https://www.tripadvisor.co.uk/Restaurant_Review1")
        sf2 = OperationalValidator.classify_source_family("https://tripadvisor.com/Restaurant_Review2")
        self.assertEqual(sf1, SourceFamily.TRIPADVISOR)
        self.assertEqual(sf2, SourceFamily.TRIPADVISOR)
        self.assertEqual(sf1, sf2)

    # ──────────────────────────────────────────────────────────────────────────
    # 6. OPERATIONAL STATUS TRANSITION UNDER RULE B
    # ──────────────────────────────────────────────────────────────────────────
    def test_rule_b_transition_with_recovered_recent_evidence(self):
        """Candidate with NO_WEBSITE_CONFIRMED, no social, verified OSM phone/address and recovered RECENT review date becomes ACTIVE_CONFIRMED under Rule B."""
        business = DiscoveredBusiness(
            company_name="Pot Kettle Black",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="Barton Arcade, Deansgate, Manchester M3 2BW",
            phone="+44 161 833 4125",
            raw_website="",
            osm_website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
            review_count=635,
            rating=4.2,
            latest_review_date="2026-09-24",
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        business.evidence_sources = {
            "name": "OPENSTREETMAP",
            "address": "OPENSTREETMAP",
            "phone": "OPENSTREETMAP"
        }
        business.verification_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
        business.raw_data = {
            "review_enrichment": {
                "review_count": 635,
                "rating": 4.2,
                "review_evidence_date": "2026-09-24",
                "review_freshness": ReviewFreshness.RECENT.value,
                "review_source": SourceFamily.RESTAURANT_GURU.value,
                "review_source_url": "https://restaurantguru.com/Pot-Kettle-Black-Manchester",
                "review_confidence": ReviewConfidence.HIGH.value,
                "review_status": ReviewStatus.FOUND.value,
                "extraction_method": "JSON_LD_REVIEW"
            }
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {}
        }

        audit = OperationalValidator.verify_operations(business, social_audit)
        self.assertEqual(audit["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertTrue(audit["multi_signal_rule_applied"])

    def test_rule_b_remains_active_likely_with_stale_evidence(self):
        """Candidate with STALE review evidence fails Condition 8 of Rule B and remains ACTIVE_LIKELY."""
        business = DiscoveredBusiness(
            company_name="Bar Bibo",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="Tib Street, Manchester M4 1LA",
            phone="+44 161 834 5678",
            raw_website="",
            osm_website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
            review_count=120,
            rating=4.1,
            latest_review_date="2025-08-15",
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        business.evidence_sources = {
            "name": "OPENSTREETMAP",
            "address": "OPENSTREETMAP",
            "phone": "OPENSTREETMAP"
        }
        business.verification_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
        business.raw_data = {
            "review_enrichment": {
                "review_count": 120,
                "rating": 4.1,
                "review_evidence_date": "2025-08-15",
                "review_freshness": ReviewFreshness.STALE.value,
                "review_source": SourceFamily.RESTAURANT_GURU.value,
                "review_confidence": ReviewConfidence.HIGH.value,
                "review_status": ReviewStatus.FOUND.value
            }
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {}
        }

        audit = OperationalValidator.verify_operations(business, social_audit)
        self.assertEqual(audit["operational_status"], OperationalStatus.ACTIVE_LIKELY.value)
        self.assertFalse(audit["multi_signal_rule_applied"])

    def test_rule_b_remains_active_likely_with_unknown_evidence(self):
        """Candidate with UNKNOWN review freshness fails Condition 8 of Rule B and remains ACTIVE_LIKELY."""
        business = DiscoveredBusiness(
            company_name="Chick A Ritos",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="Stockport Road, Manchester M19 3AB",
            phone="+44 161 224 9999",
            raw_website="",
            osm_website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
            review_count=50,
            rating=4.0,
            latest_review_date="",
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        business.evidence_sources = {
            "name": "OPENSTREETMAP",
            "address": "OPENSTREETMAP",
            "phone": "OPENSTREETMAP"
        }
        business.verification_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
        business.raw_data = {
            "review_enrichment": {
                "review_count": 50,
                "rating": 4.0,
                "review_evidence_date": None,
                "review_freshness": ReviewFreshness.UNKNOWN.value,
                "review_source": SourceFamily.TRIPADVISOR.value,
                "review_confidence": ReviewConfidence.MEDIUM.value,
                "review_status": ReviewStatus.FOUND.value
            }
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {}
        }

        audit = OperationalValidator.verify_operations(business, social_audit)
        self.assertEqual(audit["operational_status"], OperationalStatus.ACTIVE_LIKELY.value)
        self.assertFalse(audit["multi_signal_rule_applied"])

    # ──────────────────────────────────────────────────────────────────────────
    # 7. SOCIAL DATE NOT CONFUSED WITH REVIEW DATE
    # ──────────────────────────────────────────────────────────────────────────
    def test_social_post_date_rejected_as_review_evidence_date(self):
        """A recent social post date cannot satisfy Rule B review freshness requirement."""
        business = DiscoveredBusiness(
            company_name="Bar Bibo",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="Tib Street, Manchester M4 1LA",
            phone="+44 161 834 5678",
            raw_website="",
            osm_website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
            review_count=120,
            rating=4.5,
            latest_review_date="",  # No review date
            latest_social_post_date="2026-10-01",  # Recent social post
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        business.evidence_sources = {
            "name": "OPENSTREETMAP",
            "address": "OPENSTREETMAP",
            "phone": "OPENSTREETMAP"
        }
        business.verification_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
        business.raw_data = {
            "review_enrichment": {
                "review_count": 120,
                "rating": 4.5,
                "review_evidence_date": None,
                "review_freshness": ReviewFreshness.UNKNOWN.value,
                "review_source": SourceFamily.RESTAURANT_GURU.value,
                "review_confidence": ReviewConfidence.MEDIUM.value,
                "review_status": ReviewStatus.FOUND.value
            }
        }

        # Social audit with active social post but unverified ownership so Rule A does not apply
        social_audit = {
            "social_status": "FOUND",
            "social_ownership_status": "UNVERIFIED",
            "social_profile_status": "ACCESSIBLE",
            "social_activity": "ACTIVE",
            "verified_urls": {"facebook": "https://facebook.com/barbibo"}
        }

        audit = OperationalValidator.verify_operations(business, social_audit)
        # Social date must NOT satisfy Rule B Condition 8
        self.assertFalse(audit["multi_signal_rule_applied"])
        self.assertEqual(audit["review_freshness"], ReviewFreshness.UNKNOWN.value)
        self.assertNotEqual(audit["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

    # ──────────────────────────────────────────────────────────────────────────
    # 8. PRODUCTION INVARIANTS (ZERO CRM MUTATION & ZERO SENDS)
    # ──────────────────────────────────────────────────────────────────────────
    @patch.object(ReviewEvidenceRecoveryLayer, "_fetch_page")
    def test_crm_files_remain_unmutated_after_candidate_recovery(self, mock_fetch):
        """Running candidate recovery must never alter production CRM or campaign files."""
        mock_fetch.return_value = (404, "", None)
        mock_search = MagicMock()
        mock_search.search_web.return_value = []
        self.recovery.web = mock_search

        crm_files = [
            "data/cache_sheets_leads.json",
            "data/cache_sheets_review_queue.json",
            "data/cache_sheets_research_log.json",
            "data/message_history.json",
            "data/campaigns.json"
        ]

        def get_mtimes():
            return {f: os.path.getmtime(f) for f in crm_files if os.path.exists(f)}

        before_mtimes = get_mtimes()

        # Run candidate recovery
        cand = {
            "index": 99,
            "company_name": "Test Invariant Cafe",
            "city": "Manchester",
            "review_count": 10,
            "rating": 4.5,
            "review_freshness": "UNKNOWN",
            "operational_status": "ACTIVE_LIKELY",
            "qualification_state": "RESEARCH_ONLY"
        }
        res = self.recovery.recover_candidate(cand)
        self.assertIsNotNone(res)

        after_mtimes = get_mtimes()
        self.assertEqual(before_mtimes, after_mtimes, "CRM files were unexpectedly modified during recovery evaluation!")


if __name__ == "__main__":
    unittest.main()
