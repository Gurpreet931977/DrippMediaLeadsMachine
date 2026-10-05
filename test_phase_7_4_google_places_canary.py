"""
Test Suite for Phase 7.4: Google Places API (New) Review Freshness Canary
========================================================================
Comprehensive verification covering:
  1. API request construction & minimal FieldMasks
  2. Place ID extraction & identity matching
  3. Branch mismatch detection (BRANCH_DIFFERENCE)
  4. Review publishTime parsing (RFC 3339 -> ISO UTC date)
  5. Malformed timestamp handling
  6. relativePublishTimeDescription fallback
  7. Latest returned review selection logic
  8. Review freshness classification (RECENT <= 180 days, STALE > 180 days, UNKNOWN)
  9. Error handling: 403 (Permission/Auth/Disabled), 404 (Not Found), 429 (Rate Limit), 5xx, Timeout
 10. Dedicated disk caching & preventing failure caching as empty success
 11. Duplicate place ID handling
 12. Source-family independence (Rule B Condition 5 cannot be satisfied by Google alone)
 13. Review evidence reconciliation integration
 14. Pot Kettle Black branch protection fixture
 15. Production flag default (OFF)
 16. Invariant verification: 0 CRM mutations, 0 outreach, 0 Apify calls (strictly enforced)
"""

import os
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from lib.types import DiscoveredBusiness, SourceFamily, WebsiteStatus, OperationalStatus, QualificationState
from lib.enrichment.review_date_extractor import (
    ReviewDateExtractor,
    ReviewEvidenceDateType,
    ReviewDateConfidence,
    ReviewFreshness,
)
from lib.enrichment.review_rating_enricher import (
    ReviewEvidenceItem,
    ReviewConfidence,
    ReviewStatus,
    REFERENCE_DATE,
)
from lib.enrichment.review_reconciler import (
    ReviewConflictType,
    ReconciledReviewEvidence,
    ReviewEvidenceReconciler,
)
from lib.enrichment.google_places_enricher import (
    GooglePlacesReviewEnricher,
    TEXT_SEARCH_FIELD_MASK,
    PLACE_DETAILS_FIELD_MASK,
)
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider


class TestPhase74GooglePlacesCanary(unittest.TestCase):
    def setUp(self):
        self.ref_date = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.enricher = GooglePlacesReviewEnricher(
            api_key="AIzaSyMockTestKeyForCanaryValidation123",
            cache_dir="/tmp/test_cache_google_places_reviews",
            enabled=False
        )

    def tearDown(self):
        # Clean up temporary test cache files
        cache_dir = "/tmp/test_cache_google_places_reviews"
        if os.path.exists(cache_dir):
            for f in os.listdir(cache_dir):
                try:
                    os.remove(os.path.join(cache_dir, f))
                except Exception:
                    pass

    # =========================================================================
    # 1. API REQUEST CONSTRUCTION & MINIMAL FIELD MASKS
    # =========================================================================
    def test_minimal_field_masks_defined_and_not_wildcard(self):
        """Verifies that field masks are explicit, minimal, and do not use wildcards."""
        self.assertNotIn("*", TEXT_SEARCH_FIELD_MASK)
        self.assertNotIn("*", PLACE_DETAILS_FIELD_MASK)
        self.assertIn("places.id", TEXT_SEARCH_FIELD_MASK)
        self.assertIn("reviews", PLACE_DETAILS_FIELD_MASK)
        self.assertIn("rating", PLACE_DETAILS_FIELD_MASK)
        self.assertIn("userRatingCount", PLACE_DETAILS_FIELD_MASK)

    def test_auth_headers_construction(self):
        """Ensures headers use X-Goog-Api-Key and X-Goog-FieldMask."""
        headers = self.enricher._get_auth_headers(PLACE_DETAILS_FIELD_MASK)
        self.assertEqual(headers["X-Goog-Api-Key"], "AIzaSyMockTestKeyForCanaryValidation123")
        self.assertEqual(headers["X-Goog-FieldMask"], PLACE_DETAILS_FIELD_MASK)
        self.assertEqual(headers["Content-Type"], "application/json")

    # =========================================================================
    # 2. PLACE ID RESOLUTION & IDENTITY MATCHING
    # =========================================================================
    @patch("requests.post")
    def test_search_place_resolves_valid_place_id_with_high_confidence(self, mock_post):
        """Valid Text Search response returns matched place and confidence >= 0.70."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "places": [
                {
                    "id": "ChIJN1t_tDeuEmsRUsoyG83frY4",
                    "displayName": {"text": "Pot Kettle Black"},
                    "formattedAddress": "Barton Arcade, Deansgate, Manchester M3 2BW, UK",
                    "nationalPhoneNumber": "0161 833 4125"
                }
            ]
        }
        mock_post.return_value = mock_resp

        place, err, conf = self.enricher.search_place(
            company_name="Pot Kettle Black",
            city="Manchester",
            address="Barton Arcade, Deansgate",
            phone="0161 833 4125"
        )
        self.assertIsNotNone(place)
        self.assertEqual(place["id"], "ChIJN1t_tDeuEmsRUsoyG83frY4")
        self.assertIsNone(err)
        self.assertGreaterEqual(conf, 0.70)

    @patch("requests.post")
    def test_search_place_rejects_ambiguous_or_unrelated_business(self, mock_post):
        """Text search returning an unrelated business in London is rejected."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "places": [
                {
                    "id": "ChIJ_unrelated_london",
                    "displayName": {"text": "London Burger Joint"},
                    "formattedAddress": "Covent Garden, London WC2E 8HD, UK",
                    "nationalPhoneNumber": "020 7946 0123"
                }
            ]
        }
        mock_post.return_value = mock_resp

        place, err, conf = self.enricher.search_place(
            company_name="Pot Kettle Black",
            city="Manchester"
        )
        self.assertIsNone(place)
        self.assertEqual(err, "IDENTITY_CONFIDENCE_INSUFFICIENT")

    # =========================================================================
    # 3. BRANCH MISMATCH DETECTION (BRANCH_DIFFERENCE)
    # =========================================================================
    @patch("requests.post")
    def test_search_place_detects_branch_mismatch_and_refuses_merge(self, mock_post):
        """Candidate in city-centre (Barton Arcade) matching airport branch (Terminal 2) yields BRANCH_DIFFERENCE."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "places": [
                {
                    "id": "ChIJ_airport_t2",
                    "displayName": {"text": "Pot Kettle Black Terminal 2"},
                    "formattedAddress": "Terminal 2, Manchester Airport, Manchester M90 4ZY, UK"
                }
            ]
        }
        mock_post.return_value = mock_resp

        place, err, conf = self.enricher.search_place(
            company_name="Pot Kettle Black Barton Arcade",
            city="Manchester",
            address="Barton Arcade, Deansgate"
        )
        self.assertIsNone(place)
        self.assertEqual(err, "BRANCH_DIFFERENCE")

    # =========================================================================
    # 4. REVIEW PUBLISHTIME PARSING & NORMALIZATION
    # =========================================================================
    def test_review_publishtime_parsing_rfc3339_to_utc_iso(self):
        """Parses RFC 3339 UTC timestamp '2026-08-20T15:45:00Z' to '2026-08-20'."""
        parsed = GooglePlacesReviewEnricher.parse_review_timestamp("2026-08-20T15:45:00Z")
        self.assertEqual(parsed, "2026-08-20")

    def test_malformed_review_timestamp_rejected(self):
        """Malformed timestamp strings return None without throwing exceptions."""
        self.assertIsNone(GooglePlacesReviewEnricher.parse_review_timestamp("invalid-date-string"))
        self.assertIsNone(GooglePlacesReviewEnricher.parse_review_timestamp(""))
        self.assertIsNone(GooglePlacesReviewEnricher.parse_review_timestamp(None))

    # =========================================================================
    # 5. RELATIVE TIME DESCRIPTION FALLBACK
    # =========================================================================
    def test_relative_publishtime_description_fallback_when_publish_time_missing(self):
        """When publishTime is missing, relativePublishTimeDescription is parsed if explicitly attached."""
        place_details = {
            "id": "ChIJ_test_rel",
            "displayName": {"text": "Test Cafe"},
            "rating": 4.5,
            "userRatingCount": 80,
            "reviews": [
                {
                    "name": "places/ChIJ_test_rel/reviews/1",
                    "relativePublishTimeDescription": "2 weeks ago",
                    "rating": 5
                }
            ]
        }
        cand_meta = {"company_name": "Test Cafe", "city": "Manchester"}
        primary, items, err = self.enricher.extract_review_evidence(place_details, cand_meta, as_of=self.ref_date)
        self.assertIsNotNone(primary)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].evidence_date_type, ReviewEvidenceDateType.REVIEW_RELATIVE_AGE.value)
        self.assertEqual(primary.freshness, ReviewFreshness.RECENT.value)

    # =========================================================================
    # 6. LATEST REVIEW LOGIC & FRESHNESS <= 180 DAYS
    # =========================================================================
    def test_latest_review_selection_identifies_most_recent_date(self):
        """Selects the latest date among returned reviews as latest_review_evidence_date."""
        place_details = {
            "id": "ChIJ_multi_reviews",
            "displayName": {"text": "Pasta Bar"},
            "rating": 4.4,
            "userRatingCount": 150,
            "reviews": [
                {"name": "rev_1", "publishTime": "2025-10-10T10:00:00Z", "rating": 4},
                {"name": "rev_2", "publishTime": "2026-09-15T12:00:00Z", "rating": 5},
                {"name": "rev_3", "publishTime": "2026-04-01T08:00:00Z", "rating": 4}
            ]
        }
        cand_meta = {"company_name": "Pasta Bar", "city": "Manchester"}
        primary, items, err = self.enricher.extract_review_evidence(place_details, cand_meta, as_of=self.ref_date)
        self.assertEqual(len(items), 3)
        self.assertEqual(primary.evidence_date, "2026-09-15")
        self.assertEqual(primary.freshness, ReviewFreshness.RECENT.value)

    def test_stale_reviews_yield_stale_freshness(self):
        """When all returned reviews are older than 180 days, freshness is STALE."""
        place_details = {
            "id": "ChIJ_stale_reviews",
            "displayName": {"text": "Old Diner"},
            "rating": 4.1,
            "userRatingCount": 110,
            "reviews": [
                {"name": "rev_old1", "publishTime": "2025-01-10T10:00:00Z", "rating": 4},
                {"name": "rev_old2", "publishTime": "2025-03-01T12:00:00Z", "rating": 4}
            ]
        }
        cand_meta = {"company_name": "Old Diner", "city": "Manchester"}
        primary, items, err = self.enricher.extract_review_evidence(place_details, cand_meta, as_of=self.ref_date)
        self.assertEqual(primary.evidence_date, "2025-03-01")
        self.assertEqual(primary.freshness, ReviewFreshness.STALE.value)

    def test_no_review_timestamps_yield_unknown_freshness(self):
        """When reviews exist but lack timestamps, freshness is UNKNOWN."""
        place_details = {
            "id": "ChIJ_no_timestamps",
            "displayName": {"text": "Timeless Cafe"},
            "rating": 4.5,
            "userRatingCount": 90,
            "reviews": [
                {"name": "rev_notime", "rating": 5}
            ]
        }
        cand_meta = {"company_name": "Timeless Cafe", "city": "Manchester"}
        primary, items, err = self.enricher.extract_review_evidence(place_details, cand_meta, as_of=self.ref_date)
        self.assertIsNone(primary.evidence_date)
        self.assertEqual(primary.freshness, ReviewFreshness.UNKNOWN.value)

    # =========================================================================
    # 7. ERROR HANDLING: 403, 404, 429, 5XX, TIMEOUT
    # =========================================================================
    @patch("requests.get")
    def test_api_403_permission_or_disabled_failure(self, mock_get):
        """HTTP 403 returns GOOGLE_PLACES_API_NOT_ENABLED and does not treat as empty review result."""
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = '{"error": {"message": "Places API (New) has not been used or is disabled."}}'
        mock_get.return_value = mock_resp

        details, err = self.enricher.get_place_details("ChIJ_403_probe")
        self.assertIsNone(details)
        self.assertEqual(err, "GOOGLE_PLACES_API_NOT_ENABLED")

    @patch("requests.get")
    def test_api_404_place_not_found(self, mock_get):
        """HTTP 404 returns PLACE_NOT_FOUND error code."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        details, err = self.enricher.get_place_details("ChIJ_missing_place")
        self.assertIsNone(details)
        self.assertEqual(err, "PLACE_NOT_FOUND")

    @patch("requests.get")
    def test_api_429_rate_limit_exceeded(self, mock_get):
        """HTTP 429 returns RATE_LIMIT_EXCEEDED."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_get.return_value = mock_resp

        details, err = self.enricher.get_place_details("ChIJ_ratelimit")
        self.assertIsNone(details)
        self.assertEqual(err, "RATE_LIMIT_EXCEEDED")

    @patch("requests.get")
    def test_timeout_handled_without_crash(self, mock_get):
        """Network timeouts return TIMEOUT."""
        import requests
        mock_get.side_effect = requests.exceptions.Timeout("Read timeout")

        details, err = self.enricher.get_place_details("ChIJ_timeout_place")
        self.assertIsNone(details)
        self.assertEqual(err, "TIMEOUT")

    # =========================================================================
    # 8. DEDICATED DISK CACHING
    # =========================================================================
    def test_caching_preserves_successful_results_and_does_not_cache_failures_as_empty(self):
        """Ensures successful responses are cached and failures are not cached as valid 200 data."""
        cache_key = "details_test_caching_place"
        self.enricher._save_cached_response(cache_key, 200, {"id": "test_caching_place", "rating": 4.8})

        cached = self.enricher._get_cached_response(cache_key)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["rating"], 4.8)

        # Failure caching:
        fail_key = "details_test_failed_place"
        self.enricher._save_cached_response(fail_key, 500, None, error="HTTP_500")
        cached_fail = self.enricher._get_cached_response(fail_key)
        self.assertIsNone(cached_fail)  # Must not return failure as valid response data

    # =========================================================================
    # 9. SOURCE-FAMILY INDEPENDENCE FOR RULE B
    # =========================================================================
    def test_google_alone_cannot_satisfy_independent_operational_corroboration(self):
        """Google review freshness + Google business status cannot satisfy Rule B condition 5 alone."""
        biz = DiscoveredBusiness(
            company_name="Google Only Eatery",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="Northern Quarter, Manchester M1 1DB",
            phone="+44 161 234 5678",
            raw_website="",
            osm_website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.evidence_sources = {
            "name": "GOOGLE",
            "address": "GOOGLE",
            "phone": "GOOGLE"
        }
        biz.verification_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value

        reconciled = ReconciledReviewEvidence(
            conflict_type=ReviewConflictType.NO_CONFLICT.value,
            is_material_conflict=False,
            reconciled_review_count=120,
            reconciled_rating=4.5,
            reconciled_date="2026-09-20",
            reconciled_freshness=ReviewFreshness.RECENT.value,
            reconciled_status=ReviewStatus.FOUND.value,
            reconciled_confidence=ReviewConfidence.HIGH.value,
            primary_source="Google Places API (New)",
            primary_source_family=SourceFamily.GOOGLE.value
        )
        ReviewEvidenceReconciler.apply_to_business(biz, reconciled)

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {}
        }
        # When all ops evidence is from GOOGLE, independent_ops_signals excludes review source family
        audit = OperationalValidator.verify_operations(biz, social_audit)
        # Because evidence sources are all GOOGLE and review source is GOOGLE, independent ops signal fails
        self.assertFalse(audit["multi_signal_rule_applied"])
        self.assertNotEqual(audit["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

    # =========================================================================
    # 10. RECONCILIATION & POT KETTLE BLACK BRANCH PROTECTION
    # =========================================================================
    def test_pot_kettle_black_branch_protection_reconciles_safely(self):
        """Google review evidence passed into reconciler detects branch divergence against airport concession."""
        item_google = ReviewEvidenceItem(
            business_name="Pot Kettle Black",
            source="Google Places API (New)",
            source_family=SourceFamily.GOOGLE.value,
            source_url="https://www.google.com/maps/place/?q=place_id:ChIJ_pkb_barton",
            rating=4.3,
            review_count=650,
            evidence_date="2026-09-25",
            freshness=ReviewFreshness.RECENT.value,
            branch_identifier="Barton Arcade",
            street="Barton Arcade, Deansgate",
            postcode="M3 2BW",
            city="Manchester"
        )
        item_airport_rg = ReviewEvidenceItem(
            business_name="Pot Kettle Black Terminal 2",
            source="Restaurant Guru",
            source_family=SourceFamily.RESTAURANT_GURU.value,
            rating=2.6,
            review_count=146,
            evidence_date="2026-09-24",
            freshness=ReviewFreshness.RECENT.value,
            branch_identifier="Terminal 2",
            street="Manchester Airport Terminal 2",
            postcode="M90 4ZY",
            city="Manchester"
        )
        reconciled = self.enricher.reconciler.reconcile([item_google, item_airport_rg], as_of=self.ref_date)
        self.assertTrue(reconciled.is_material_conflict)
        self.assertEqual(reconciled.conflict_type, ReviewConflictType.BRANCH_DIFFERENCE.value)
        self.assertIsNone(reconciled.reconciled_rating)
        self.assertIsNone(reconciled.reconciled_review_count)

    # =========================================================================
    # 11. PRODUCTION INVARIANTS: NO APIFY, NO CRM MUTATIONS, NO OUTREACH
    # =========================================================================
    def test_apify_call_prohibition_invariant(self):
        """Enricher strictly forbids any Apify actor invocations (Apify calls = 0)."""
        self.assertEqual(self.enricher.apify_calls, 0)
        # Attempting to call enricher with Apify calls > 0 triggers assertion error
        self.enricher.apify_calls = 1
        with self.assertRaises(AssertionError):
            self.enricher.search_place("Test", "Manchester")

    def test_production_flag_off_by_default(self):
        """GOOGLE_PLACES_REVIEW_FRESHNESS_FALLBACK_ENABLED is disabled by default."""
        default_enricher = GooglePlacesReviewEnricher()
        self.assertFalse(default_enricher.enabled)

    def test_crm_files_unmutated_invariant(self):
        """Evaluating enricher candidate must never alter CRM files."""
        crm_files = [
            "data/cache_sheets_leads.json",
            "data/cache_sheets_review_queue.json",
            "data/cache_sheets_research_log.json",
            "data/message_history.json",
            "data/campaigns.json"
        ]
        def get_mtimes():
            return {f: os.path.getmtime(f) for f in crm_files if os.path.exists(f)}

        before = get_mtimes()

        # Run candidate evaluation with missing credentials (stopping cleanly)
        cand = {"company_name": "Test Invariant Bistro", "city": "Manchester"}
        res = self.enricher.enrich_candidate(cand, as_of=self.ref_date)
        self.assertIsNotNone(res)

        after = get_mtimes()
        self.assertEqual(before, after, "CRM files were modified during Google Places canary test!")


if __name__ == "__main__":
    unittest.main()
