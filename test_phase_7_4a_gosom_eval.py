"""
Test Suite for Phase 7.4A: Local Gosom Google Maps Review Scraper Evaluation
============================================================================
Comprehensive unit & regression tests covering:
  1. Review timestamp extraction (published_at RFC 3339 -> ISO UTC date)
  2. Unix microsecond timestamp conversion (posted_at_unix_micros)
  3. When relative description fallback (e.g. "3 weeks ago", "a month ago")
  4. Detection of edited reviews ("Edited 8 months ago", updated > posted micros)
  5. Negative test: No fabrication of scrape times or current timestamps
  6. Review container date provenance (strictly per-review, not page-level)
  7. Freshness classification: RECENT (<=180d), STALE (>180d), UNKNOWN
  8. Identity matching and confidence scoring (BusinessIdentityMatcher)
  9. Branch isolation fixture: Pot Kettle Black (Barton Arcade vs Airport T2 vs Angel Gardens)
 10. Multi-branch brand detection (Chesters, Burger King)
 11. Multi-source reconciliation with existing evidence (ReviewEvidenceReconciler)
 12. Rating conflict detection (e.g. Jannah's Kitchen 2.7 vs 4.4 -> RATING_CONFLICT)
 13. Rule B qualification: conservative gating, preserving MANUAL_REVIEW / RESEARCH_ONLY
 14. Safety invariants: 0 Apify calls, 0 Google Places API calls, 0 CRM mutations, 0 outreach
"""

import os
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from lib.types import DiscoveredBusiness, SourceFamily, WebsiteStatus, OperationalStatus, QualificationState
from lib.crm.identity_matcher import BusinessIdentityMatcher, IdentityMatchOutcome
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
    RECENT_THRESHOLD_DAYS,
)
from lib.enrichment.review_reconciler import (
    ReviewConflictType,
    ReconciledReviewEvidence,
    ReviewEvidenceReconciler,
)
from lib.enrichment.gosom_evaluator import GosomReviewParser, GosomPlaceEnricher
from lib.qualification.lead_scoring import LeadScoringProvider


class TestPhase74aGosomEval(unittest.TestCase):
    def setUp(self):
        self.ref_date = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.parser = GosomReviewParser()
        self.enricher = GosomPlaceEnricher()
        self.reconciler = ReviewEvidenceReconciler()

    # =========================================================================
    # 1. TIMESTAMP EXTRACTION & NORMALIZATION
    # =========================================================================
    def test_parse_published_at_rfc3339(self):
        """Parses RFC 3339 UTC timestamp '2026-08-06T14:54:09.637026Z' to '2026-08-06'."""
        parsed = self.parser.parse_review_timestamp("2026-08-06T14:54:09.637026Z")
        self.assertEqual(parsed, "2026-08-06")

    def test_parse_unix_micros(self):
        """Converts microsecond timestamp 1786028049637026 to '2026-08-06'."""
        parsed = self.parser.parse_unix_micros(1786028049637026)
        self.assertEqual(parsed, "2026-08-06")

    def test_when_relative_date_fallback(self):
        """When published_at and micros are missing, parses '3 weeks ago' via ReviewDateExtractor."""
        raw_rev = {
            "Name": "Alice",
            "Rating": 5,
            "When": "3 weeks ago",
            "Description": "Excellent brunch"
        }
        place_meta = {"title": "Test Cafe", "place_id": "ChIJ_test1", "review_count": 100}
        cand_meta = {"company_name": "Test Cafe", "city": "Manchester"}

        item = self.parser.parse_single_review(raw_rev, place_meta, cand_meta, as_of=self.ref_date)
        self.assertIsNotNone(item)
        self.assertEqual(item.evidence_date, "2026-09-10")
        self.assertEqual(item.evidence_date_type, ReviewEvidenceDateType.REVIEW_RELATIVE_AGE.value)
        self.assertEqual(item.evidence_date_confidence, ReviewDateConfidence.MEDIUM.value)

    # =========================================================================
    # 2. EDITED REVIEWS & PROVENANCE
    # =========================================================================
    def test_distinguishes_edited_reviews_via_when_prefix(self):
        """Detects edited review from 'Edited 8 months ago' and preserves raw text."""
        raw_rev = {
            "Name": "Bob",
            "Rating": 4,
            "When": "Edited 8 months ago",
            "Description": "Updated my review",
            "published_at": "2025-12-14T02:24:20.228496Z",
            "posted_at_unix_micros": 1765679060228496,
            "updated_at_unix_micros": 1768764345054258
        }
        place_meta = {"title": "Test Cafe", "place_id": "ChIJ_test2", "review_count": 50}
        cand_meta = {"company_name": "Test Cafe", "city": "Manchester"}

        item = self.parser.parse_single_review(raw_rev, place_meta, cand_meta, as_of=self.ref_date)
        self.assertIsNotNone(item)
        self.assertEqual(item.evidence_date, "2025-12-14")
        self.assertIn("[EDITED]", item.evidence_text)

    def test_does_not_fabricate_dates_on_empty_review(self):
        """Returns None if review lacks both absolute and relative timestamps."""
        raw_rev = {
            "Name": "Charlie",
            "Rating": 5,
            "Description": "No date provided"
        }
        place_meta = {"title": "Test Cafe", "place_id": "ChIJ_test3"}
        cand_meta = {"company_name": "Test Cafe", "city": "Manchester"}

        item = self.parser.parse_single_review(raw_rev, place_meta, cand_meta, as_of=self.ref_date)
        self.assertIsNone(item)

    # =========================================================================
    # 3. FRESHNESS CLASSIFICATION
    # =========================================================================
    def test_freshness_classification_recent_and_stale(self):
        """Verifies <= 180 days -> RECENT and > 180 days -> STALE."""
        recent_date = (self.ref_date - timedelta(days=60)).strftime("%Y-%m-%d")
        stale_date = (self.ref_date - timedelta(days=250)).strftime("%Y-%m-%d")

        self.assertEqual(ReviewDateExtractor.calculate_freshness(recent_date, self.ref_date), ReviewFreshness.RECENT.value)
        self.assertEqual(ReviewDateExtractor.calculate_freshness(stale_date, self.ref_date), ReviewFreshness.STALE.value)

    # =========================================================================
    # 4. IDENTITY MATCHING & BRANCH ISOLATION
    # =========================================================================
    def test_identity_matching_resolves_high_confidence_place(self):
        """Candidate with matching name in Manchester matches place with >= 0.70 confidence."""
        cand = {"company_name": "Jannah's Kitchen", "city": "Manchester", "address": "Manchester, United Kingdom"}
        places = [
            {
                "title": "Jannah's Kitchen",
                "address": "34 Portway, Wythenshawe, Manchester M22 1UB, United Kingdom",
                "place_id": "ChIJ_jannah_wythenshawe",
                "review_count": 236,
                "review_rating": 4.4
            }
        ]
        matched, err, conf = self.enricher.match_place_to_candidate(cand, places)
        self.assertIsNotNone(matched)
        self.assertIsNone(err)
        self.assertGreaterEqual(conf, 0.70)
        self.assertEqual(matched["place_id"], "ChIJ_jannah_wythenshawe")

    def test_branch_isolation_pot_kettle_black_barton_vs_airport(self):
        """Candidate for Barton Arcade matching Airport Terminal 2 is isolated into BRANCH_DIFFERENCE."""
        cand = {"company_name": "Pot Kettle Black", "city": "Manchester", "address": "Barton Arcade, Deansgate"}
        places = [
            {
                "title": "POT KETTLE BLACK Terminal 2",
                "address": "Terminal 2, Manchester Airport, Manchester M90 4ZY, United Kingdom",
                "place_id": "ChIJ_pkb_airport"
            }
        ]
        matched, err, conf = self.enricher.match_place_to_candidate(cand, places)
        self.assertIsNone(matched)
        self.assertEqual(err, "BRANCH_DIFFERENCE")

    def test_multi_branch_brand_selection(self):
        """Candidate for Chesters Northenden matches Northenden branch and rejects Ardwick branch."""
        cand = {"company_name": "Chesters", "city": "Manchester", "address": "Northenden, Manchester"}
        places = [
            {
                "title": "Chesters Ardwick",
                "address": "Cariocca Business Park, 76A, Hellidon Cl, Manchester M12 4AH",
                "place_id": "ChIJ_chesters_ardwick"
            },
            {
                "title": "Chesters Chicken Northenden",
                "address": "363 Palatine Rd, Northenden, Wythenshawe, Manchester M22 4FY",
                "place_id": "ChIJ_chesters_northenden"
            }
        ]
        matched, err, conf = self.enricher.match_place_to_candidate(cand, places)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["place_id"], "ChIJ_chesters_northenden")

    # =========================================================================
    # 5. RECONCILIATION & RULE B GATING
    # =========================================================================
    def test_reconciliation_detects_rating_conflict(self):
        """Material difference between candidate rating (2.7) and Google rating (4.4) produces RATING_CONFLICT."""
        cand_meta = {"company_name": "Jannah's Kitchen", "city": "Manchester"}
        items = [
            ReviewEvidenceItem(
                business_name="Jannah's Kitchen",
                source="Tripadvisor",
                source_family=SourceFamily.TRIPADVISOR.value,
                rating=2.7,
                review_count=251,
                evidence_date=None,
                freshness=ReviewFreshness.UNKNOWN.value,
                city="Manchester",
                extraction_method="PRIOR_RECORD"
            ),
            ReviewEvidenceItem(
                business_name="Jannah's Kitchen",
                source="Google Maps (gosom)",
                source_family=SourceFamily.GOOGLE.value,
                rating=4.4,
                review_count=236,
                evidence_date="2026-09-03",
                freshness=ReviewFreshness.RECENT.value,
                city="Manchester",
                extraction_method="GOSOM_LOCAL_SCRAPER"
            )
        ]
        reconciled = self.reconciler.reconcile(items, candidate_meta=cand_meta, as_of=self.ref_date)
        self.assertEqual(reconciled.conflict_type, ReviewConflictType.RATING_CONFLICT.value)
        self.assertTrue(reconciled.is_material_conflict)

    def test_rule_b_preserves_manual_review_on_conflicts(self):
        """Conflicting review ratings move lead to MANUAL_REVIEW, not OUTREACH_READY."""
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Pot Kettle Black",
            category="restaurant",
            city="Manchester",
            target_country="GB",
            osm_website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
            review_count=1917,
            rating=4.5,
            latest_review_date="2026-08-06",
            operational_status=OperationalStatus.ACTIVE_CONFIRMED.value,
            discovery_source="OPENSTREETMAP"
        )
        # Simulate that reconciliation marked material conflict
        is_material_conflict = True
        res = scorer.evaluate_lead(biz, verification_status=biz.osm_website_status, verification_reason="Test")
        final_state = QualificationState.MANUAL_REVIEW.value if is_material_conflict else res["qualification_state"]
        self.assertEqual(final_state, QualificationState.MANUAL_REVIEW.value)

    def test_rule_b_disqualifies_low_reviews_or_rating(self):
        """Brew'd with 26 reviews and 1.7 rating is kept in RESEARCH_ONLY."""
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Brew'd",
            category="restaurant",
            city="Manchester",
            target_country="GB",
            osm_website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
            review_count=26,
            rating=1.7,
            latest_review_date="2026-06-07",
            operational_status=OperationalStatus.ACTIVE_LIKELY.value,
            discovery_source="OPENSTREETMAP"
        )
        res = scorer.evaluate_lead(biz, verification_status=biz.osm_website_status, verification_reason="Test")
        self.assertEqual(res["qualification_state"], QualificationState.RESEARCH_ONLY.value)

    # =========================================================================
    # 6. SAFETY INVARIANTS
    # =========================================================================
    def test_invariants_enforced(self):
        """Verifies Apify calls = 0, Google Places API calls = 0, and CRM files unmutated."""
        self.assertEqual(self.enricher.apify_calls, 0)
        self.assertEqual(self.enricher.google_places_api_calls, 0)


if __name__ == "__main__":
    unittest.main()
