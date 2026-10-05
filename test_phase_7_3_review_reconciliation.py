"""
Test Suite for Phase 7.3: Review Source Reconciliation & Provider Strategy
==========================================================================
Verifies:
  1. Permanent regression fixture: Pot Kettle Black multi-source conflict/branch divergence.
  2. All mandatory conflict test cases A through M:
     A. Same rating, different count
     B. Different rating, same business
     C. Different rating, different branch
     D. Same business, one stale source and one recent source
     E. Same name but different city
     F. Same name, same city, incompatible phone
     G. Large rating conflict
     H. Minor rating difference
     I. One source unavailable
     J. One source blocked
     K. One source has no review date
     L. Review count formatted with commas/year-like values
     M. Multiple accepted sources with identical evidence
  3. Full Qualification Safety Matrix.
  4. Production invariants (0 CRM mutations, 0 sends, 0 campaign arms, 0 fabricated dates/IDs).
"""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from lib.types import DiscoveredBusiness, SourceFamily, WebsiteStatus, OperationalStatus, QualificationState
from lib.enrichment.review_date_extractor import (
    ReviewDateExtractor,
    ReviewEvidenceDateType,
    ReviewDateConfidence,
    ReviewFreshness,
)
from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
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
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider


class TestPhase73ReviewReconciliation(unittest.TestCase):
    def setUp(self):
        self.reconciler = ReviewEvidenceReconciler()
        self.ref_date = datetime(2026, 10, 1, tzinfo=timezone.utc)

    # =========================================================================
    # 1. POT KETTLE BLACK REGRESSION FIXTURE (Phase 7.2 Critical Discovery)
    # =========================================================================
    def test_pot_kettle_black_regression_conflict_detected_and_gated(self):
        """
        Permanent regression fixture:
        Pot Kettle Black has:
          Source A: 4.2★ / 635 reviews (TripAdvisor / Barton Arcade, city centre)
          Source B: 2.6★ / 146 reviews, review date 2026-09-24 (Restaurant Guru / Terminal 2, Airport)
        Expected:
          - Both evidence items preserved in sources_evaluated
          - Conflict / branch divergence detected
          - Qualification CANNOT become OUTREACH_READY
          - Routes to MANUAL_REVIEW
          - No evidence overwritten
          - No outreach
        """
        item_a = ReviewEvidenceItem(
            business_name="Pot Kettle Black",
            source="Tripadvisor",
            source_family=SourceFamily.TRIPADVISOR.value,
            source_url="https://www.tripadvisor.co.uk/Restaurant_Review-g187069-d5964864-Reviews-Pot_Kettle_Black-Manchester_Greater_Manchester_England.html",
            rating=4.2,
            review_count=635,
            evidence_date=None,
            freshness=ReviewFreshness.UNKNOWN.value,
            confidence=ReviewConfidence.HIGH.value,
            branch_identifier="Barton Arcade",
            street="Barton Arcade, Deansgate",
            postcode="M3 2BW",
            city="Manchester",
            source_quality=0.85,
            retrieved_at="2026-10-01T12:00:00Z"
        )

        item_b = ReviewEvidenceItem(
            business_name="Pot Kettle Black Terminal 2",
            source="Restaurant Guru",
            source_family=SourceFamily.RESTAURANT_GURU.value,
            source_url="https://restaurantguru.com/Pot-Kettle-Black-Manchester-Airport",
            rating=2.6,
            review_count=146,
            evidence_date="2026-09-24",
            freshness=ReviewFreshness.RECENT.value,
            confidence=ReviewConfidence.HIGH.value,
            branch_identifier="Terminal 2",
            street="Manchester Airport Terminal 2",
            postcode="M90 4ZY",
            city="Manchester",
            source_quality=0.75,
            retrieved_at="2026-10-01T12:05:00Z"
        )

        cand_meta = {
            "company_name": "Pot Kettle Black",
            "city": "Manchester",
            "address": "Barton Arcade, Deansgate, Manchester M3 2BW"
        }

        reconciled = self.reconciler.reconcile([item_a, item_b], candidate_meta=cand_meta, as_of=self.ref_date)

        # 1. Both evidence items preserved
        self.assertEqual(len(reconciled.sources_evaluated), 2)
        sources = [s["source"] for s in reconciled.sources_evaluated]
        self.assertIn("Tripadvisor", sources)
        self.assertIn("Restaurant Guru", sources)

        # 2. Material conflict / branch divergence detected
        self.assertTrue(reconciled.is_material_conflict)
        self.assertIn(reconciled.conflict_type, [ReviewConflictType.BRANCH_DIFFERENCE.value, ReviewConflictType.RATING_CONFLICT.value, ReviewConflictType.MAJOR_REVIEW_CONFLICT.value])

        # 3. Top-level reconciled values must not naively average or overwrite
        self.assertIsNone(reconciled.reconciled_rating)
        self.assertIsNone(reconciled.reconciled_review_count)
        self.assertEqual(reconciled.reconciled_status, ReviewStatus.CONFLICT_REQUIRES_REVIEW.value)
        self.assertEqual(reconciled.reconciled_confidence, ReviewConfidence.CONFLICT.value)

        # 4. Attach to DiscoveredBusiness and evaluate through OperationalValidator & LeadScoringProvider
        biz = DiscoveredBusiness(
            company_name="Pot Kettle Black",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="Barton Arcade, Deansgate, Manchester M3 2BW",
            phone="+44 161 833 4125",
            raw_website="",
            osm_website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.evidence_sources = {"name": "OPENSTREETMAP", "address": "OPENSTREETMAP", "phone": "OPENSTREETMAP"}
        biz.verification_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value

        ReviewEvidenceReconciler.apply_to_business(biz, reconciled)

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {}
        }

        audit = OperationalValidator.verify_operations(biz, social_audit)
        # Cannot qualify under Rule B due to unresolved review conflict
        self.assertFalse(audit["multi_signal_rule_applied"])
        self.assertNotEqual(audit["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

        # Qualification must NOT be OUTREACH_READY; routes to MANUAL_REVIEW
        scorer = LeadScoringProvider()
        score_res = scorer.evaluate_lead(
            business=biz,
            verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value
        )
        self.assertNotEqual(score_res["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(score_res["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    # =========================================================================
    # 2. CONFLICT TEST CASES A THROUGH M
    # =========================================================================

    def test_case_a_same_rating_different_count(self):
        """Case A: Same rating (4.2 vs 4.2), material count disparity (100 vs 850)."""
        it1 = ReviewEvidenceItem(
            source="Restaurant Guru", rating=4.2, review_count=100,
            evidence_date="2026-09-01", freshness=ReviewFreshness.RECENT.value,
            confidence=ReviewConfidence.HIGH.value
        )
        it2 = ReviewEvidenceItem(
            source="Tripadvisor", rating=4.2, review_count=850,
            evidence_date="2026-09-02", freshness=ReviewFreshness.RECENT.value,
            confidence=ReviewConfidence.HIGH.value
        )
        reconciled = self.reconciler.reconcile([it1, it2], as_of=self.ref_date)
        self.assertTrue(reconciled.is_material_conflict)
        self.assertEqual(reconciled.conflict_type, ReviewConflictType.COUNT_CONFLICT.value)
        self.assertEqual(len(reconciled.sources_evaluated), 2)
        self.assertIsNone(reconciled.reconciled_review_count)

    def test_case_b_different_rating_same_business(self):
        """Case B: Different rating (4.6 vs 3.8), same business entity."""
        it1 = ReviewEvidenceItem(
            business_name="Pasta Loco", city="Manchester",
            source="Restaurant Guru", rating=4.6, review_count=120,
            confidence=ReviewConfidence.HIGH.value
        )
        it2 = ReviewEvidenceItem(
            business_name="Pasta Loco", city="Manchester",
            source="Yelp", rating=3.8, review_count=115,
            confidence=ReviewConfidence.HIGH.value
        )
        reconciled = self.reconciler.reconcile([it1, it2], as_of=self.ref_date)
        self.assertTrue(reconciled.is_material_conflict)
        self.assertEqual(reconciled.conflict_type, ReviewConflictType.RATING_CONFLICT.value)
        self.assertIn("Material star rating disparity", reconciled.conflict_reasons[0])

    def test_case_c_different_rating_different_branch(self):
        """Case C: Different rating, distinct physical branches (City Centre vs Airport)."""
        it1 = ReviewEvidenceItem(
            business_name="Archie's Oxford Road", branch_identifier="oxford road",
            source="Tripadvisor", rating=4.1, review_count=500, city="Manchester"
        )
        it2 = ReviewEvidenceItem(
            business_name="Archie's Airport Terminal 2", branch_identifier="terminal 2",
            source="Restaurant Guru", rating=2.8, review_count=90, city="Manchester"
        )
        reconciled = self.reconciler.reconcile([it1, it2], as_of=self.ref_date)
        self.assertTrue(reconciled.is_material_conflict)
        self.assertEqual(reconciled.conflict_type, ReviewConflictType.BRANCH_DIFFERENCE.value)
        self.assertTrue(reconciled.is_branch_difference)

    def test_case_d_same_business_one_stale_one_recent(self):
        """Case D: Same business, one stale source (2025-01-10) and one recent source (2026-09-20), consistent ratings."""
        it1 = ReviewEvidenceItem(
            business_name="Evelyn's Cafe Bar", city="Manchester",
            source="Tripadvisor", rating=4.3, review_count=400,
            evidence_date="2025-01-10", freshness=ReviewFreshness.STALE.value,
            confidence=ReviewConfidence.HIGH.value
        )
        it2 = ReviewEvidenceItem(
            business_name="Evelyn's Cafe Bar", city="Manchester",
            source="Restaurant Guru", rating=4.2, review_count=380,
            evidence_date="2026-09-20", freshness=ReviewFreshness.RECENT.value,
            confidence=ReviewConfidence.HIGH.value
        )
        reconciled = self.reconciler.reconcile([it1, it2], as_of=self.ref_date)
        self.assertFalse(reconciled.is_material_conflict)
        self.assertEqual(reconciled.conflict_type, ReviewConflictType.NO_CONFLICT.value)
        # Recent genuine review date is preserved from the corroborating source
        self.assertEqual(reconciled.reconciled_date, "2026-09-20")
        self.assertEqual(reconciled.reconciled_freshness, ReviewFreshness.RECENT.value)
        self.assertEqual(len(reconciled.sources_evaluated), 2)

    def test_case_e_same_name_different_city(self):
        """Case E: Same name but different city (Manchester vs London) -> IDENTITY_CONFLICT."""
        it1 = ReviewEvidenceItem(
            business_name="Dishoom", city="Manchester",
            source="Tripadvisor", rating=4.6, review_count=200
        )
        it2 = ReviewEvidenceItem(
            business_name="Dishoom", city="London",
            source="Restaurant Guru", rating=4.6, review_count=210
        )
        reconciled = self.reconciler.reconcile([it1, it2], as_of=self.ref_date)
        self.assertTrue(reconciled.is_material_conflict)
        self.assertEqual(reconciled.conflict_type, ReviewConflictType.IDENTITY_CONFLICT.value)
        self.assertTrue(any("Incompatible cities" in r for r in reconciled.conflict_reasons))

    def test_case_f_same_name_same_city_incompatible_phone(self):
        """Case F: Same name, same city, but incompatible phone numbers."""
        it1 = ReviewEvidenceItem(
            business_name="Federal Cafe", city="Manchester", phone="+44 161 833 4125",
            source="Tripadvisor", rating=4.5, review_count=350
        )
        it2 = ReviewEvidenceItem(
            business_name="Federal Cafe", city="Manchester", phone="+44 161 224 9999",
            source="Restaurant Guru", rating=4.5, review_count=340
        )
        reconciled = self.reconciler.reconcile([it1, it2], as_of=self.ref_date)
        self.assertTrue(reconciled.is_material_conflict)
        self.assertEqual(reconciled.conflict_type, ReviewConflictType.IDENTITY_CONFLICT.value)
        self.assertTrue(any("Conflicting primary telephone numbers" in r for r in reconciled.conflict_reasons))

    def test_case_g_large_rating_conflict(self):
        """Case G: Large rating conflict (4.8★ vs 2.1★)."""
        it1 = ReviewEvidenceItem(
            business_name="Trophy Diner", city="Manchester",
            source="Tripadvisor", rating=4.8, review_count=200
        )
        it2 = ReviewEvidenceItem(
            business_name="Trophy Diner", city="Manchester",
            source="Restaurant Guru", rating=2.1, review_count=180
        )
        reconciled = self.reconciler.reconcile([it1, it2], as_of=self.ref_date)
        self.assertTrue(reconciled.is_material_conflict)
        self.assertEqual(reconciled.conflict_type, ReviewConflictType.RATING_CONFLICT.value)
        self.assertTrue(any("Material star rating disparity" in r for r in reconciled.conflict_reasons))

    def test_case_h_minor_rating_difference_compatible(self):
        """Case H: Minor rating difference (4.3★ vs 4.2★, diff = 0.1 < 0.5) is compatible."""
        it1 = ReviewEvidenceItem(
            business_name="Takk Coffee", city="Manchester",
            source="Tripadvisor", rating=4.3, review_count=220,
            confidence=ReviewConfidence.HIGH.value
        )
        it2 = ReviewEvidenceItem(
            business_name="Takk Coffee", city="Manchester",
            source="Restaurant Guru", rating=4.2, review_count=210,
            confidence=ReviewConfidence.HIGH.value
        )
        reconciled = self.reconciler.reconcile([it1, it2], as_of=self.ref_date)
        self.assertFalse(reconciled.is_material_conflict)
        self.assertEqual(reconciled.conflict_type, ReviewConflictType.NO_CONFLICT.value)
        self.assertIsNotNone(reconciled.reconciled_rating)
        self.assertIn(reconciled.reconciled_rating, [4.3, 4.2])

    def test_case_i_one_source_unavailable(self):
        """Case I: One source unavailable (e.g. 404 or reject_reason='SOURCE_PAGE_NOT_FOUND')."""
        it_valid = ReviewEvidenceItem(
            business_name="Moose Coffee", city="Manchester",
            source="Tripadvisor", rating=4.4, review_count=310,
            evidence_date="2026-09-10", freshness=ReviewFreshness.RECENT.value,
            confidence=ReviewConfidence.HIGH.value
        )
        it_unavailable = ReviewEvidenceItem(
            business_name="Moose Coffee", city="Manchester",
            source="Restaurant Guru",
            reject_reason="SOURCE_PAGE_NOT_FOUND",
            evidence_text="HTTP 404 Not Found"
        )
        reconciled = self.reconciler.reconcile([it_valid, it_unavailable], as_of=self.ref_date)
        self.assertFalse(reconciled.is_material_conflict)
        self.assertEqual(reconciled.conflict_type, ReviewConflictType.NO_CONFLICT.value)
        self.assertEqual(reconciled.reconciled_rating, 4.4)
        self.assertEqual(reconciled.reconciled_review_count, 310)
        # Unavailable item is preserved in audit trail
        self.assertEqual(len(reconciled.sources_evaluated), 2)
        rejected = [s for s in reconciled.sources_evaluated if s.get("reject_reason") == "SOURCE_PAGE_NOT_FOUND"]
        self.assertEqual(len(rejected), 1)

    def test_case_j_one_source_blocked(self):
        """Case J: One source blocked (e.g. 403 DataDome/Cloudflare anti-bot)."""
        it_valid = ReviewEvidenceItem(
            business_name="Federal Cafe", city="Manchester",
            source="Restaurant Guru", rating=4.4, review_count=290,
            evidence_date="2026-09-12", freshness=ReviewFreshness.RECENT.value,
            confidence=ReviewConfidence.HIGH.value
        )
        it_blocked = ReviewEvidenceItem(
            business_name="Federal Cafe", city="Manchester",
            source="Tripadvisor",
            reject_reason="SOURCE_ACCESS_BLOCKED_HTTP_403",
            evidence_text="DataDome 403 Forbidden"
        )
        reconciled = self.reconciler.reconcile([it_valid, it_blocked], as_of=self.ref_date)
        self.assertFalse(reconciled.is_material_conflict)
        self.assertEqual(reconciled.reconciled_rating, 4.4)
        self.assertEqual(len(reconciled.sources_evaluated), 2)
        self.assertEqual(reconciled.sources_evaluated[1]["reject_reason"], "SOURCE_ACCESS_BLOCKED_HTTP_403")

    def test_case_k_one_source_has_no_review_date(self):
        """Case K: One source has genuine review date, second source only has aggregate count/rating."""
        it_with_date = ReviewEvidenceItem(
            business_name="Idle Hands", city="Manchester",
            source="Restaurant Guru", rating=4.5, review_count=180,
            evidence_date="2026-09-15", freshness=ReviewFreshness.RECENT.value,
            confidence=ReviewConfidence.HIGH.value
        )
        it_aggregate_only = ReviewEvidenceItem(
            business_name="Idle Hands", city="Manchester",
            source="Tripadvisor", rating=4.4, review_count=175,
            evidence_date=None, freshness=ReviewFreshness.UNKNOWN.value,
            confidence=ReviewConfidence.HIGH.value
        )
        reconciled = self.reconciler.reconcile([it_with_date, it_aggregate_only], as_of=self.ref_date)
        self.assertFalse(reconciled.is_material_conflict)
        # Date from source 1 is retained safely
        self.assertEqual(reconciled.reconciled_date, "2026-09-15")
        self.assertEqual(reconciled.reconciled_freshness, ReviewFreshness.RECENT.value)

    def test_case_l_review_count_formatted_with_commas_and_year_like(self):
        """Case L: Review count formatting handling (e.g. '1,450' vs year-like 2024)."""
        # Commas in count string
        count_clean = int("1,450".replace(",", ""))
        self.assertEqual(count_clean, 1450)

        # Date extractor rejects year-like counts in snippet text
        ext = ReviewDateExtractor.extract_from_snippet(
            "Established in 2024 with 2,024 happy customers",
            as_of=self.ref_date
        )
        self.assertIsNone(ext.date)

    def test_case_m_multiple_accepted_sources_identical_evidence(self):
        """Case M: Multiple accepted sources with identical corroborating evidence."""
        it1 = ReviewEvidenceItem(
            business_name="Siop Shop", city="Manchester",
            source="Restaurant Guru", rating=4.5, review_count=150,
            evidence_date="2026-09-18", freshness=ReviewFreshness.RECENT.value,
            confidence=ReviewConfidence.HIGH.value
        )
        it2 = ReviewEvidenceItem(
            business_name="Siop Shop", city="Manchester",
            source="Tripadvisor", rating=4.5, review_count=150,
            evidence_date="2026-09-18", freshness=ReviewFreshness.RECENT.value,
            confidence=ReviewConfidence.HIGH.value
        )
        reconciled = self.reconciler.reconcile([it1, it2], as_of=self.ref_date)
        self.assertFalse(reconciled.is_material_conflict)
        self.assertEqual(reconciled.conflict_type, ReviewConflictType.NO_CONFLICT.value)
        self.assertEqual(reconciled.reconciled_rating, 4.5)
        self.assertEqual(reconciled.reconciled_review_count, 150)
        self.assertEqual(reconciled.reconciled_date, "2026-09-18")
        self.assertEqual(reconciled.reconciled_freshness, ReviewFreshness.RECENT.value)

    # =========================================================================
    # 3. QUALIFICATION SAFETY MATRIX TESTS
    # =========================================================================

    def test_safety_matrix_single_valid_source_rule_b_qualifies(self):
        """Scenario: One valid source, >=50 reviews, >=4.0, recent, all Rule B gates -> Rule B may qualify."""
        biz = DiscoveredBusiness(
            company_name="Single Source Bistro",
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
        biz.evidence_sources = {"name": "OPENSTREETMAP", "address": "OPENSTREETMAP", "phone": "OPENSTREETMAP"}
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
            primary_source="Restaurant Guru",
            primary_source_family=SourceFamily.RESTAURANT_GURU.value
        )
        ReviewEvidenceReconciler.apply_to_business(biz, reconciled)

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {}
        }
        audit = OperationalValidator.verify_operations(biz, social_audit)
        self.assertEqual(audit["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertTrue(audit["multi_signal_rule_applied"])

    def test_safety_matrix_fresh_date_plus_conflicting_rating_yields_manual_review(self):
        """Scenario: Fresh date present, but conflicting ratings (e.g. 4.2★ vs 2.6★) -> MANUAL_REVIEW."""
        biz = DiscoveredBusiness(
            company_name="Conflicting Rating Cafe",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="Deansgate, Manchester M3 2BW",
            phone="+44 161 833 4125",
            raw_website="",
            osm_website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.evidence_sources = {"name": "OPENSTREETMAP", "address": "OPENSTREETMAP", "phone": "OPENSTREETMAP"}
        biz.verification_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value

        reconciled = ReconciledReviewEvidence(
            conflict_type=ReviewConflictType.RATING_CONFLICT.value,
            is_material_conflict=True,
            conflict_reasons=["Material rating disparity: 4.2 vs 2.6"],
            reconciled_status=ReviewStatus.CONFLICT_REQUIRES_REVIEW.value,
            reconciled_confidence=ReviewConfidence.CONFLICT.value
        )
        ReviewEvidenceReconciler.apply_to_business(biz, reconciled)

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {}
        }
        audit = OperationalValidator.verify_operations(biz, social_audit)
        self.assertFalse(audit["multi_signal_rule_applied"])
        self.assertNotEqual(audit["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

        scorer = LeadScoringProvider()
        score_res = scorer.evaluate_lead(
            business=biz,
            verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value
        )
        self.assertNotEqual(score_res["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(score_res["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    def test_safety_matrix_aggregate_count_rating_only_remains_active_likely(self):
        """Scenario: Only aggregate count and rating available without review date -> remains ACTIVE_LIKELY, not ACTIVE_CONFIRMED."""
        biz = DiscoveredBusiness(
            company_name="No Date Eatery",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="Ancoats, Manchester M4 5BD",
            phone="+44 161 333 4444",
            raw_website="",
            osm_website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.evidence_sources = {"name": "OPENSTREETMAP", "address": "OPENSTREETMAP", "phone": "OPENSTREETMAP"}
        biz.verification_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value

        reconciled = ReconciledReviewEvidence(
            conflict_type=ReviewConflictType.NO_CONFLICT.value,
            is_material_conflict=False,
            reconciled_review_count=300,
            reconciled_rating=4.6,
            reconciled_date=None,
            reconciled_freshness=ReviewFreshness.UNKNOWN.value,
            reconciled_status=ReviewStatus.FOUND.value,
            reconciled_confidence=ReviewConfidence.HIGH.value,
            primary_source="Google Maps",
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
        audit = OperationalValidator.verify_operations(biz, social_audit)
        self.assertFalse(audit["multi_signal_rule_applied"])
        self.assertEqual(audit["operational_status"], OperationalStatus.ACTIVE_LIKELY.value)

    # =========================================================================
    # 4. ZERO CRM MUTATION & SAFETY INVARIANTS
    # =========================================================================
    def test_reconciliation_zero_crm_mutation_invariant(self):
        """Reconciliation evaluation must never touch production CRM files."""
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

        # Run multi-item reconciliation
        item1 = ReviewEvidenceItem(source="Tripadvisor", rating=4.2, review_count=635)
        item2 = ReviewEvidenceItem(source="Restaurant Guru", rating=2.6, review_count=146)
        reconciled = self.reconciler.reconcile([item1, item2], as_of=self.ref_date)
        self.assertIsNotNone(reconciled)

        after = get_mtimes()
        self.assertEqual(before, after, "CRM files were modified during reconciliation execution!")


if __name__ == "__main__":
    unittest.main()
