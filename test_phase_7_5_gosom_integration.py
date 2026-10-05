"""
Phase 7.5: Gosom Review-Freshness Fallback Integration Test Suite
================================================================
Comprehensive test suite verifying requirements A through V:
  A. feature flag OFF
  B. feature flag ON
  C. UNKNOWN freshness triggers fallback
  D. known RECENT does not trigger fallback
  E. known STALE does not trigger unnecessary fallback
  F. <50 reviews does not trigger fallback
  G. rating <4.0 does not trigger fallback
  H. CRM duplicate does not trigger fallback
  I. cap enforcement
  J. caching
  K. valid timestamp extraction
  L. missing timestamp
  M. branch mismatch
  N. identity mismatch
  O. rating conflict
  P. count conflict
  Q. reconciliation
  R. Rule B source-family independence
  S. qualification immutability in dry-run
  T. no outreach
  U. no CRM mutation
  V. Gosom failure handling
"""

import os
import shutil
import tempfile
import unittest
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List

from lib.types import DiscoveredBusiness, SourceFamily, OperationalStatus, QualificationState
from lib.crm.identity_matcher import BusinessIdentityMatcher
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
from lib.enrichment.gosom_fallback import (
    GosomFallbackConfig,
    GosomReviewFreshnessFallback,
    PINNED_GOSOM_VERSION,
)
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider


class TestPhase75GosomIntegration(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.config = GosomFallbackConfig(
            enabled=True,
            max_calls=20,
            cache_dir=os.path.join(self.test_dir, "cache"),
            scraper_bin="scratch/google_maps_scraper",
            scraper_version=PINNED_GOSOM_VERSION
        )
        self.fallback = GosomReviewFreshnessFallback(config=self.config)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # ──────────────────────────────────────────────────────────────────────────
    # A. FEATURE FLAG OFF
    # ──────────────────────────────────────────────────────────────────────────
    def test_a_feature_flag_off(self):
        """When GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED=false, fallback is skipped."""
        disabled_config = GosomFallbackConfig(enabled=False, cache_dir=self.config.cache_dir)
        fb = GosomReviewFreshnessFallback(config=disabled_config)

        candidate = {
            "company_name": "Pot Kettle Black",
            "city": "Manchester",
            "review_count": 635,
            "rating": 4.2,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        rec, telem = fb.enrich_candidate(candidate)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "FLAG_DISABLED")
        self.assertEqual(fb.calls_skipped, 1)
        self.assertEqual(fb.calls_attempted, 0)

    # ──────────────────────────────────────────────────────────────────────────
    # B. FEATURE FLAG ON
    # ──────────────────────────────────────────────────────────────────────────
    def test_b_feature_flag_on(self):
        """When enabled=True, an eligible candidate triggers fallback processing."""
        candidate = {
            "company_name": "Pot Kettle Black",
            "city": "Manchester",
            "address": "1A Tariff St, Manchester M1 2FF",
            "review_count": 635,
            "rating": 4.2,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        mock_places = [{
            "title": "Pot Kettle Black",
            "address": "1A Tariff St, Manchester M1 2FF, United Kingdom",
            "place_id": "ChIJLbC5EACxe0gREr5sEbwZ2RQ",
            "review_count": 3,
            "review_rating": 5.0,
            "user_reviews": [{
                "Name": "Alice",
                "Rating": 5,
                "published_at": "2026-09-06T13:30:45Z",
                "When": "a month ago"
            }]
        }]
        rec, telem = self.fallback.enrich_candidate(candidate, preloaded_places=mock_places)
        self.assertIsNotNone(rec)
        self.assertEqual(telem["status"], "SUCCESS")
        self.assertEqual(self.fallback.calls_attempted, 1)
        self.assertEqual(self.fallback.calls_completed, 1)

    # ──────────────────────────────────────────────────────────────────────────
    # C. UNKNOWN FRESHNESS TRIGGERS FALLBACK
    # ──────────────────────────────────────────────────────────────────────────
    def test_c_unknown_freshness_triggers_fallback(self):
        """review_freshness == UNKNOWN makes an otherwise qualified candidate eligible."""
        cand = {
            "company_name": "Eligible Bistro",
            "city": "Manchester",
            "review_count": 100,
            "rating": 4.5,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        eligible, reason = self.fallback.is_candidate_eligible(cand)
        self.assertTrue(eligible)
        self.assertEqual(reason, "ELIGIBLE")

    # ──────────────────────────────────────────────────────────────────────────
    # D. KNOWN RECENT DOES NOT TRIGGER FALLBACK
    # ──────────────────────────────────────────────────────────────────────────
    def test_d_known_recent_does_not_trigger(self):
        """Candidates with known RECENT freshness must NOT trigger Gosom fallback."""
        cand = {
            "company_name": "Active Eatery",
            "city": "Manchester",
            "review_count": 100,
            "rating": 4.5,
            "review_freshness": "RECENT",
            "category": "restaurant"
        }
        eligible, reason = self.fallback.is_candidate_eligible(cand)
        self.assertFalse(eligible)
        self.assertIn("INELIGIBLE_FRESHNESS_ALREADY_KNOWN_RECENT", reason)

        rec, telem = self.fallback.enrich_candidate(cand)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "SKIPPED_INELIGIBLE")

    # ──────────────────────────────────────────────────────────────────────────
    # E. KNOWN STALE DOES NOT TRIGGER UNNECESSARY FALLBACK
    # ──────────────────────────────────────────────────────────────────────────
    def test_e_known_stale_does_not_trigger(self):
        """Candidates with known STALE freshness must NOT trigger Gosom fallback."""
        cand = {
            "company_name": "Old Tavern",
            "city": "Manchester",
            "review_count": 100,
            "rating": 4.5,
            "review_freshness": "STALE",
            "category": "restaurant"
        }
        eligible, reason = self.fallback.is_candidate_eligible(cand)
        self.assertFalse(eligible)
        self.assertIn("INELIGIBLE_FRESHNESS_ALREADY_KNOWN_STALE", reason)

    # ──────────────────────────────────────────────────────────────────────────
    # F. <50 REVIEWS DOES NOT TRIGGER FALLBACK
    # ──────────────────────────────────────────────────────────────────────────
    def test_f_lt_50_reviews_does_not_trigger(self):
        """review_count < 50 or None must NOT trigger Gosom fallback."""
        cand_low = {
            "company_name": "Small Cafe",
            "city": "Manchester",
            "review_count": 44,
            "rating": 4.5,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        eligible, reason = self.fallback.is_candidate_eligible(cand_low)
        self.assertFalse(eligible)
        self.assertIn("INELIGIBLE_INSUFFICIENT_REVIEWS_44_LT_50", reason)

        cand_none = {
            "company_name": "Unreviewed Diner",
            "city": "Manchester",
            "review_count": None,
            "rating": 4.5,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        eligible_none, reason_none = self.fallback.is_candidate_eligible(cand_none)
        self.assertFalse(eligible_none)
        self.assertEqual(reason_none, "INELIGIBLE_MISSING_REVIEW_COUNT")

    # ──────────────────────────────────────────────────────────────────────────
    # G. RATING <4.0 DOES NOT TRIGGER FALLBACK
    # ──────────────────────────────────────────────────────────────────────────
    def test_g_rating_lt_4_0_does_not_trigger(self):
        """rating < 4.0 or None must NOT trigger Gosom fallback."""
        cand_low_rating = {
            "company_name": "Mediocre Kitchen",
            "city": "Manchester",
            "review_count": 250,
            "rating": 2.7,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        eligible, reason = self.fallback.is_candidate_eligible(cand_low_rating)
        self.assertFalse(eligible)
        self.assertIn("INELIGIBLE_LOW_RATING_2.7_LT_4_0", reason)

        cand_none_rating = {
            "company_name": "Unrated Kitchen",
            "city": "Manchester",
            "review_count": 100,
            "rating": None,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        eligible_none, reason_none = self.fallback.is_candidate_eligible(cand_none_rating)
        self.assertFalse(eligible_none)
        self.assertEqual(reason_none, "INELIGIBLE_MISSING_RATING")

    # ──────────────────────────────────────────────────────────────────────────
    # H. CRM DUPLICATE DOES NOT TRIGGER FALLBACK
    # ──────────────────────────────────────────────────────────────────────────
    def test_h_crm_duplicate_does_not_trigger(self):
        """Candidates existing in the CRM pool (name or phone) are rejected."""
        crm_pool = {"441619998888", "pot kettle black"}
        cand_dup_phone = {
            "company_name": "Another Name",
            "phone": "+44 (161) 999-8888",
            "review_count": 100,
            "rating": 4.5,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        eligible, reason = self.fallback.is_candidate_eligible(cand_dup_phone, crm_pool=crm_pool)
        self.assertFalse(eligible)
        self.assertEqual(reason, "INELIGIBLE_CRM_DUPLICATE_PHONE")

        cand_dup_name = {
            "company_name": "Pot Kettle Black",
            "phone": "+44 161 111 2222",
            "review_count": 100,
            "rating": 4.5,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        eligible_name, reason_name = self.fallback.is_candidate_eligible(cand_dup_name, crm_pool=crm_pool)
        self.assertFalse(eligible_name)
        self.assertEqual(reason_name, "INELIGIBLE_CRM_DUPLICATE_NAME")

    # ──────────────────────────────────────────────────────────────────────────
    # I. CAP ENFORCEMENT
    # ──────────────────────────────────────────────────────────────────────────
    def test_i_cap_enforcement(self):
        """MAX_GOSOM_REVIEW_FALLBACK_CALLS stops processing when reached."""
        capped_config = GosomFallbackConfig(enabled=True, max_calls=2, cache_dir=self.config.cache_dir)
        fb = GosomReviewFreshnessFallback(config=capped_config)

        mock_place = [{
            "title": "Bistro",
            "address": "1 Main St, Manchester",
            "place_id": "PID1",
            "review_count": 80,
            "review_rating": 4.5,
            "user_reviews": [{"Name": "A", "Rating": 5, "published_at": "2026-08-01T12:00:00Z"}]
        }]

        cand1 = {"company_name": "Bistro", "city": "Manchester", "address": "1 Main St", "review_count": 80, "rating": 4.5, "review_freshness": "UNKNOWN", "category": "restaurant"}
        cand2 = {"company_name": "Bistro", "city": "Manchester", "address": "1 Main St", "review_count": 80, "rating": 4.5, "review_freshness": "UNKNOWN", "category": "restaurant"}
        cand3 = {"company_name": "Bistro", "city": "Manchester", "address": "1 Main St", "review_count": 80, "rating": 4.5, "review_freshness": "UNKNOWN", "category": "restaurant"}

        r1, t1 = fb.enrich_candidate(cand1, preloaded_places=mock_place)
        self.assertEqual(t1["status"], "SUCCESS")
        self.assertEqual(fb.calls_attempted, 1)

        r2, t2 = fb.enrich_candidate(cand2, preloaded_places=mock_place)
        self.assertEqual(t2["status"], "SUCCESS")
        self.assertEqual(fb.calls_attempted, 2)

        # 3rd call must hit cap
        r3, t3 = fb.enrich_candidate(cand3, preloaded_places=mock_place)
        self.assertIsNone(r3)
        self.assertEqual(t3["status"], "CAP_EXCEEDED")
        self.assertEqual(fb.calls_attempted, 2)  # Cap stopped it before increment

    # ──────────────────────────────────────────────────────────────────────────
    # J. CACHING
    # ──────────────────────────────────────────────────────────────────────────
    def test_j_caching(self):
        """Successful results are cached and retrieved without redundant execution."""
        cache_key = "gosom_test_cache_key_123"
        query = "Test Query Manchester"
        mock_places = [{"title": "Cached Place", "place_id": "CP1", "review_count": 50, "review_rating": 4.5}]

        # Save to cache
        self.fallback.save_cached_result(cache_key, query, mock_places, status="SUCCESS", place_id="CP1")

        # Read from cache
        retrieved = self.fallback.get_cached_result(cache_key)
        self.assertIsNotNone(retrieved)
        self.assertEqual(len(retrieved), 1)
        self.assertEqual(retrieved[0]["title"], "Cached Place")
        self.assertEqual(self.fallback.cache_hits, 1)

        # Test that failure statuses are never cached
        fail_key = "gosom_fail_key_456"
        self.fallback.save_cached_result(fail_key, query, [], status="FAILURE")
        self.assertIsNone(self.fallback.get_cached_result(fail_key))

    # ──────────────────────────────────────────────────────────────────────────
    # K. VALID TIMESTAMP EXTRACTION
    # ──────────────────────────────────────────────────────────────────────────
    def test_k_valid_timestamp_extraction(self):
        """RFC 3339 strings and microsecond timestamps are parsed to ISO UTC date."""
        cand = {"company_name": "Valid Times", "city": "Manchester", "review_count": 100, "rating": 4.5, "review_freshness": "UNKNOWN", "category": "restaurant"}
        mock_place = [{
            "title": "Valid Times",
            "address": "12 Market St, Manchester M1 1AA",
            "place_id": "VT1",
            "review_count": 100,
            "review_rating": 4.5,
            "user_reviews": [
                {
                    "Name": "Bob",
                    "Rating": 5,
                    "published_at": "2026-08-20T10:15:30.123456Z",
                    "posted_at_unix_micros": 1787220930123456,
                    "When": "2 months ago"
                }
            ]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=mock_place)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.reconciled_date, "2026-08-20")
        self.assertEqual(rec.reconciled_freshness, "RECENT")
        self.assertEqual(rec.primary_source_family, SourceFamily.GOOGLE.value)

    # ──────────────────────────────────────────────────────────────────────────
    # L. MISSING TIMESTAMP
    # ──────────────────────────────────────────────────────────────────────────
    def test_l_missing_timestamp(self):
        """Reviews missing any usable timestamp fail extraction cleanly without fabricating dates."""
        cand = {"company_name": "No Dates", "city": "Manchester", "review_count": 100, "rating": 4.5, "review_freshness": "UNKNOWN", "category": "restaurant"}
        mock_place = [{
            "title": "No Dates",
            "address": "12 Market St, Manchester M1 1AA",
            "place_id": "ND1",
            "review_count": 100,
            "review_rating": 4.5,
            "user_reviews": [
                {
                    "Name": "Anonymous",
                    "Rating": 4,
                    "published_at": None,
                    "posted_at_unix_micros": None,
                    "When": ""
                }
            ]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=mock_place)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "EXTRACTION_FAILED")

    # ──────────────────────────────────────────────────────────────────────────
    # M. BRANCH MISMATCH
    # ──────────────────────────────────────────────────────────────────────────
    def test_m_branch_mismatch(self):
        """Branch divergence (e.g. Airport Terminal vs City Centre) routes to BRANCH_DIFFERENCE / MANUAL_REVIEW."""
        cand = {
            "company_name": "Escape Lounge",
            "city": "Manchester",
            "address": "City Centre, Manchester",
            "review_count": 100,
            "rating": 4.2,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        mock_place = [{
            "title": "Escape Lounge MAN Terminal 2",
            "address": "Terminal 2, Manchester Airport, M90 1QX",
            "place_id": "MAN_T2",
            "review_count": 5000,
            "review_rating": 4.3,
            "user_reviews": [{"Name": "Traveler", "Rating": 4, "published_at": "2026-08-01T00:00:00Z"}]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=mock_place)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "MATCH_FAILED")
        self.assertEqual(telem["reason"], "BRANCH_DIFFERENCE")

    # ──────────────────────────────────────────────────────────────────────────
    # N. IDENTITY MISMATCH
    # ──────────────────────────────────────────────────────────────────────────
    def test_n_identity_mismatch(self):
        """Unrelated place (<0.70 identity confidence) fails matching cleanly."""
        cand = {
            "company_name": "Artisan Pizza Manchester",
            "city": "Manchester",
            "review_count": 100,
            "rating": 4.5,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        mock_place = [{
            "title": "London Fish and Chips",
            "address": "Oxford Street, London",
            "place_id": "LON_1",
            "review_count": 300,
            "review_rating": 4.1,
            "user_reviews": [{"Name": "Guest", "Rating": 4, "published_at": "2026-08-01T00:00:00Z"}]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, preloaded_places=mock_place)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "MATCH_FAILED")

    # ──────────────────────────────────────────────────────────────────────────
    # O. RATING CONFLICT
    # ──────────────────────────────────────────────────────────────────────────
    def test_o_rating_conflict(self):
        """Material rating discrepancy across sources triggers RATING_CONFLICT."""
        cand = {
            "company_name": "Jannah's Kitchen",
            "city": "Manchester",
            "address": "34 Portway, Manchester M22 1UB",
            "review_count": 251,
            "rating": 4.4,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        existing_ta = [
            ReviewEvidenceItem(
                business_name="Jannah's Kitchen",
                source="Tripadvisor",
                source_family=SourceFamily.TRIPADVISOR.value,
                source_url="https://tripadvisor.com/jannahs",
                rating=2.7,
                review_count=251,
                evidence_date=None,
                freshness=ReviewFreshness.UNKNOWN.value
            )
        ]
        mock_place = [{
            "title": "Jannah's Kitchen",
            "address": "34 Portway, Wythenshawe, Manchester M22 1UB, United Kingdom",
            "place_id": "JK_MCR",
            "review_count": 236,
            "review_rating": 4.4,
            "user_reviews": [{"Name": "Sam", "Rating": 5, "published_at": "2026-09-03T17:52:28Z"}]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, existing_reviews=existing_ta, preloaded_places=mock_place)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.conflict_type, ReviewConflictType.RATING_CONFLICT.value)
        self.assertTrue(rec.is_material_conflict)

    # ──────────────────────────────────────────────────────────────────────────
    # P. COUNT CONFLICT
    # ──────────────────────────────────────────────────────────────────────────
    def test_p_count_conflict(self):
        """Extreme review count disparity across branches/listings triggers MAJOR_REVIEW_CONFLICT."""
        cand = {
            "company_name": "Pot Kettle Black",
            "city": "Manchester",
            "address": "1A Tariff St, Manchester",
            "review_count": 635,
            "rating": 4.2,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        existing_ta = [
            ReviewEvidenceItem(
                business_name="Pot Kettle Black",
                source="Tripadvisor",
                source_family=SourceFamily.TRIPADVISOR.value,
                source_url="https://tripadvisor.com/pkb",
                rating=4.2,
                review_count=635,
                evidence_date=None,
                freshness=ReviewFreshness.UNKNOWN.value
            )
        ]
        # Scraped 1A Tariff St with only 3 reviews and 5.0 rating
        mock_place = [{
            "title": "Pot Kettle Black",
            "address": "1A Tariff St, Manchester M1 2FF, United Kingdom",
            "place_id": "PKB_TARIFF",
            "review_count": 3,
            "review_rating": 5.0,
            "user_reviews": [{"Name": "Alice", "Rating": 5, "published_at": "2026-09-06T13:30:45Z"}]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, existing_reviews=existing_ta, preloaded_places=mock_place)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.conflict_type, ReviewConflictType.MAJOR_REVIEW_CONFLICT.value)
        self.assertTrue(rec.is_material_conflict)

    # ──────────────────────────────────────────────────────────────────────────
    # Q. RECONCILIATION INTEGRATION
    # ──────────────────────────────────────────────────────────────────────────
    def test_q_reconciliation(self):
        """Every Gosom result passes through ReviewEvidenceReconciler without bypass."""
        cand = {
            "company_name": "Harmony Cafe",
            "city": "Manchester",
            "address": "10 Bridge St, Manchester M3 3AB",
            "review_count": 120,
            "rating": 4.4,
            "review_freshness": "UNKNOWN",
            "category": "restaurant"
        }
        existing = [
            ReviewEvidenceItem(
                business_name="Harmony Cafe",
                source="Restaurant Guru",
                source_family=SourceFamily.RESTAURANT_GURU.value,
                source_url="https://restaurantguru.com/harmony",
                rating=4.3,
                review_count=115,
                evidence_date="2026-08-01",
                freshness=ReviewFreshness.RECENT.value
            )
        ]
        mock_place = [{
            "title": "Harmony Cafe",
            "address": "10 Bridge St, Manchester M3 3AB, United Kingdom",
            "place_id": "HC1",
            "review_count": 125,
            "review_rating": 4.5,
            "user_reviews": [{"Name": "Guest", "Rating": 5, "published_at": "2026-08-15T12:00:00Z"}]
        }]
        rec, telem = self.fallback.enrich_candidate(cand, existing_reviews=existing, preloaded_places=mock_place)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.conflict_type, ReviewConflictType.NO_CONFLICT.value)
        self.assertFalse(rec.is_material_conflict)
        self.assertEqual(len(rec.sources_evaluated), 2)

    # ──────────────────────────────────────────────────────────────────────────
    # R. RULE B SOURCE-FAMILY INDEPENDENCE
    # ──────────────────────────────────────────────────────────────────────────
    def test_r_rule_b_source_family_independence(self):
        """
        Rule B Condition 5: Google review + Google place info = ONE source family.
        Does NOT satisfy independent corroboration alone.
        """
        biz = DiscoveredBusiness(
            company_name="Solitary Google Eatery",
            category="restaurant",
            city="Manchester",
            target_country="GB",
            address="15 King St, Manchester",
            phone="01618887777",
            review_count=150,
            rating=4.5,
            latest_review_date="2026-08-01",
            raw_data={
                "review_enrichment": {
                    "source_family": SourceFamily.GOOGLE.value,
                    "review_freshness": ReviewFreshness.RECENT.value,
                    "review_evidence_date": "2026-08-01"
                },
                "operational_evidence": {
                    # Address & phone both from Google Maps listing
                    "sources": ["Google Maps"]
                }
            }
        )
        social_audit = {"has_verified_social": False, "verified_accounts": {}}
        op_audit = OperationalValidator.verify_operations(biz, social_audit)
        # Without second independent source family, remains ACTIVE_LIKELY, NOT ACTIVE_CONFIRMED
        self.assertNotEqual(op_audit["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

    # ──────────────────────────────────────────────────────────────────────────
    # S. QUALIFICATION IMMUTABILITY IN DRY-RUN
    # ──────────────────────────────────────────────────────────────────────────
    def test_s_qualification_immutability_in_dry_run(self):
        """Fallback evaluation does not mutate production candidate objects or CRM states."""
        cand_dict = {
            "company_name": "Pristine Bistro",
            "city": "Manchester",
            "address": "1 Main St",
            "review_count": 80,
            "rating": 4.5,
            "review_freshness": "UNKNOWN",
            "qualification_state": "MANUAL_REVIEW",
            "category": "restaurant"
        }
        cand_copy = dict(cand_dict)
        mock_place = [{
            "title": "Pristine Bistro",
            "address": "1 Main St, Manchester",
            "place_id": "PB1",
            "review_count": 85,
            "review_rating": 4.6,
            "user_reviews": [{"Name": "Guest", "Rating": 5, "published_at": "2026-08-25T10:00:00Z"}]
        }]
        rec, telem = self.fallback.enrich_candidate(cand_dict, preloaded_places=mock_place)
        self.assertIsNotNone(rec)
        # Verify original candidate dict qualification state is not mutated
        self.assertEqual(cand_dict["qualification_state"], cand_copy["qualification_state"])

    # ──────────────────────────────────────────────────────────────────────────
    # T. NO OUTREACH
    # ──────────────────────────────────────────────────────────────────────────
    def test_t_no_outreach(self):
        """Enricher has no outreach capability and sends zero messages."""
        self.assertFalse(hasattr(self.fallback, "send_outreach"))
        self.assertFalse(hasattr(self.fallback, "arm_campaign"))

    # ──────────────────────────────────────────────────────────────────────────
    # U. NO CRM MUTATION
    # ──────────────────────────────────────────────────────────────────────────
    def test_u_no_crm_mutation(self):
        """Fallback never interacts with CRM cache files."""
        crm_files = [
            "data/cache_sheets_leads.json",
            "data/cache_sheets_review_queue.json",
            "data/cache_sheets_research_log.json",
            "data/message_history.json",
            "data/campaigns.json"
        ]
        hashes_before = {}
        for f in crm_files:
            if os.path.exists(f):
                with open(f, "rb") as fp:
                    hashes_before[f] = hashlib.sha256(fp.read()).hexdigest()

        # Run candidate fallback
        cand = {"company_name": "Test Bistro", "city": "Manchester", "address": "1 High St", "review_count": 70, "rating": 4.2, "review_freshness": "UNKNOWN", "category": "restaurant"}
        mock_place = [{"title": "Test Bistro", "address": "1 High St, Manchester", "place_id": "TB1", "review_count": 70, "review_rating": 4.2, "user_reviews": [{"Name": "A", "Rating": 5, "published_at": "2026-08-01T00:00:00Z"}]}]
        self.fallback.enrich_candidate(cand, preloaded_places=mock_place)

        for f in crm_files:
            if os.path.exists(f):
                with open(f, "rb") as fp:
                    h_after = hashlib.sha256(fp.read()).hexdigest()
                self.assertEqual(hashes_before[f], h_after, f"CRM file {f} was mutated!")

    # ──────────────────────────────────────────────────────────────────────────
    # V. GOSOM FAILURE HANDLING
    # ──────────────────────────────────────────────────────────────────────────
    def test_v_gosom_failure_handling(self):
        """Missing binary, timeouts, and malformed outputs return safe failure codes."""
        # 1. Missing executable
        bad_config = GosomFallbackConfig(
            enabled=True,
            scraper_bin="/non/existent/path/scraper",
            cache_dir=self.config.cache_dir
        )
        fb_bad = GosomReviewFreshnessFallback(config=bad_config)
        places, err = fb_bad.execute_scraper_query("Test Query")
        self.assertIsNone(places)
        self.assertEqual(err, "SCRAPER_EXECUTABLE_MISSING")

        # 2. Candidate enrichment with missing executable remains safe failure
        cand = {"company_name": "Fail Cafe", "city": "Manchester", "review_count": 80, "rating": 4.5, "review_freshness": "UNKNOWN", "category": "restaurant"}
        rec, telem = fb_bad.enrich_candidate(cand)
        self.assertIsNone(rec)
        self.assertEqual(telem["status"], "SCRAPER_FAILURE")
        self.assertEqual(telem["reason"], "SCRAPER_EXECUTABLE_MISSING")


if __name__ == "__main__":
    unittest.main()
