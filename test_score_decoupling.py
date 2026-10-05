"""
Unit tests for Phase 5.1: Decoupling Qualification State from Priority Score.

Tests:
  - Cases A through J (Section 5)
  - Explicit Score / Priority Invariants (Section 6)
"""

import unittest
from unittest.mock import patch

from lib.types import (
    DiscoveredBusiness,
    OperationalStatus,
    QualificationState,
    WebsiteStatus,
    Priority,
)
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.validation.operational_validator import OperationalValidator


class TestScoreDecoupling(unittest.TestCase):
    def setUp(self):
        self.scorer = LeadScoringProvider()
        self.validator = OperationalValidator()

    def _create_base_passing_biz(self, revs=150, rating=4.8, has_phone=True, has_address=True, category="Italian Restaurant"):
        """Helper creating a business that satisfies all 8 hard gates when operational_status is ACTIVE_CONFIRMED."""
        biz = DiscoveredBusiness(
            company_name="Trattoria Bella",
            category=category,
            city="Leeds",
            target_country="United Kingdom",
            address="12 Bishopgate, Leeds LS1 5DY" if has_address else "",
            phone="+44 113 243 0000" if has_phone else "",
            raw_website="",
            review_count=revs,
            rating=rating,
            latest_review_date="2026-08-15",
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_source": "Tripadvisor",
                "review_count": revs,
                "rating": rating,
                "review_confidence": "HIGH",
                "review_status": "FOUND",
                "review_freshness": "RECENT",
                "review_evidence_date": "2026-08-15"
            }
        }
        return biz

    # =========================================================================
    # SECTION 5: CASES A THROUGH J
    # =========================================================================

    def test_case_a_150_revs_4_8_active_confirmed_no_social(self):
        """Case A: 150 revs, 4.8 rating, ACTIVE_CONFIRMED, no social -> OUTREACH_READY with Priority.LOW (score 50 < 65)"""
        biz = self._create_base_passing_biz(revs=150, rating=4.8)
        biz.operational_status = OperationalStatus.ACTIVE_CONFIRMED.value

        qual = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertEqual(qual["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertTrue(qual["is_outreach_ready"])
        self.assertEqual(qual["score"], 50)
        self.assertEqual(qual["priority"], Priority.LOW.value)
        self.assertIn("verified active operations (ACTIVE_CONFIRMED)", qual["qualification_reason"])

    def test_case_b_150_revs_4_8_active_confirmed_verified_social(self):
        """Case B: 150 revs, 4.8 rating, ACTIVE_CONFIRMED, verified social -> OUTREACH_READY with Priority.MEDIUM (score 70 >= 65)"""
        biz = self._create_base_passing_biz(revs=150, rating=4.8)
        biz.operational_status = OperationalStatus.ACTIVE_CONFIRMED.value
        biz.facebook_url = "https://facebook.com/trattoriabella"

        with patch("lib.validation.social_validator.SocialIdentityValidator.verify_ownership") as mock_soc:
            mock_soc.return_value = {
                "social_status": "SOCIAL_FOUND",
                "social_ownership_status": "VERIFIED",
                "social_profile_status": "ACCESSIBLE",
                "social_activity": "ACTIVE",
                "verified_urls": {"facebook": "https://facebook.com/trattoriabella"},
                "verified_handles": {"facebook": "trattoriabella"},
                "red_flags": [],
                "evidence_notes": []
            }
            qual = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertEqual(qual["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertTrue(qual["is_outreach_ready"])
        self.assertEqual(qual["score"], 70)
        self.assertEqual(qual["priority"], Priority.MEDIUM.value)
        self.assertIn("Facebook (@trattoriabella)", qual["qualification_reason"])

    def test_case_c_25_revs_hard_gate_failure(self):
        """Case C: 25 reviews (<50) + 4.8 rating + ACTIVE_CONFIRMED -> Hard Gate 3 failure -> MANUAL_REVIEW"""
        biz = self._create_base_passing_biz(revs=25, rating=4.8)
        biz.operational_status = OperationalStatus.ACTIVE_CONFIRMED.value

        qual = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertNotEqual(qual["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(qual["qualification_state"], QualificationState.MANUAL_REVIEW.value)
        self.assertIn("Moderate customer traction (25 reviews", qual["qualification_reason"])

    def test_case_d_low_rating_hard_gate_failure(self):
        """Case D: 150 reviews + 3.2 rating (<3.5) + ACTIVE_CONFIRMED -> Hard Gate 2 failure -> MANUAL_REVIEW"""
        biz = self._create_base_passing_biz(revs=150, rating=3.2)
        biz.operational_status = OperationalStatus.ACTIVE_CONFIRMED.value

        qual = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertNotEqual(qual["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(qual["qualification_state"], QualificationState.MANUAL_REVIEW.value)
        self.assertIn("Low rating (3.2★", qual["qualification_reason"])

    def test_case_e_active_likely_hard_gate_failure(self):
        """Case E: 150 reviews + 4.8 rating + ACTIVE_LIKELY (no social) -> Hard Gate 6/7 failure -> MANUAL_REVIEW"""
        biz = self._create_base_passing_biz(revs=150, rating=4.8)
        biz.operational_status = OperationalStatus.ACTIVE_LIKELY.value
        # No independent operational signal to promote to ACTIVE_CONFIRMED
        biz.raw_data = {"review_enrichment": {"review_freshness": "UNKNOWN"}}

        qual = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertNotEqual(qual["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(qual["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    def test_case_f_closure_evidence_excluded(self):
        """Case F: closure evidence -> Hard Gate B -> EXCLUDED"""
        biz = self._create_base_passing_biz(revs=150, rating=4.8)
        biz.is_permanently_closed = True
        biz.operational_status = OperationalStatus.CLOSED_OR_UNVERIFIED.value

        qual = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertEqual(qual["qualification_state"], QualificationState.EXCLUDED.value)
        self.assertIn("permanently closed", qual["qualification_reason"].lower())

    def test_case_g_wrong_location_excluded(self):
        """Case G: wrong location -> Hard Gate A -> EXCLUDED"""
        biz = self._create_base_passing_biz(revs=150, rating=4.8)
        biz.country_status = "COUNTRY_MISMATCH"
        biz.detected_country = "France"

        qual = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertEqual(qual["qualification_state"], QualificationState.EXCLUDED.value)
        self.assertIn("Country mismatch", qual["qualification_reason"])

    def test_case_h_review_conflict_manual_review(self):
        """Case H: review conflict -> Check 0A -> MANUAL_REVIEW"""
        biz = self._create_base_passing_biz(revs=150, rating=4.8)
        biz.raw_data["review_enrichment"]["review_confidence"] = "CONFLICT"
        biz.raw_data["review_enrichment"]["review_status"] = "CONFLICT_REQUIRES_REVIEW"

        qual = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertEqual(qual["qualification_state"], QualificationState.MANUAL_REVIEW.value)
        self.assertIn("Conflicting review/rating data detected", qual["qualification_reason"])

    def test_case_i_50_revs_4_0_rating_active_confirmed_no_social(self):
        """Case I: Exactly 50 reviews, 4.0 rating, ACTIVE_CONFIRMED, no social -> OUTREACH_READY even though score is low (35)"""
        biz = self._create_base_passing_biz(revs=50, rating=4.0)
        biz.operational_status = OperationalStatus.ACTIVE_CONFIRMED.value

        qual = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertEqual(qual["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertTrue(qual["is_outreach_ready"])
        # Score: 50-99 revs (+10), rating 4.0-4.29 (+5), premises (+10), booking (+10) = 35
        self.assertEqual(qual["score"], 35)
        self.assertEqual(qual["priority"], Priority.LOW.value)

    def test_case_j_no_hard_gate_failures_with_extremely_low_score(self):
        """Case J: All hard gates pass + score is extremely low (e.g. 25 pts) -> Remains OUTREACH_READY with Priority.LOW"""
        # 50 reviews (+10), 4.0 rating (+5), address only (no phone -> 0 premises), retail (no booking -> 0 booking)
        biz = self._create_base_passing_biz(revs=50, rating=4.0, has_phone=False, category="Boutique Shop")
        biz.operational_status = OperationalStatus.ACTIVE_CONFIRMED.value

        qual = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertEqual(qual["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(qual["score"], 15)
        self.assertEqual(qual["priority"], Priority.LOW.value)

    # =========================================================================
    # SECTION 6: SCORE / PRIORITY INVARIANT TESTS
    # =========================================================================

    def test_invariant_score_enrichment_does_not_affect_qualification_state(self):
        """Invariant: Enrichment changes that only affect score (social, photos, reviews >= 50) MUST NOT change qualification_state."""
        biz = self._create_base_passing_biz(revs=150, rating=4.8)
        biz.operational_status = OperationalStatus.ACTIVE_CONFIRMED.value

        # Baseline: No social, photo_count=0 -> Score = 50, OUTREACH_READY, Priority.LOW
        qual_base = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(qual_base["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(qual_base["score"], 50)
        self.assertEqual(qual_base["priority"], Priority.LOW.value)

        # Invariant 1: Add photo count (50 photos) -> score does not change without social, qualification remains OUTREACH_READY
        biz.photo_count = 100
        qual_photos = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(qual_photos["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(qual_photos["score"], 50)

        # Invariant 2: Add verified social media -> score jumps to 70, priority jumps to MEDIUM, qualification remains OUTREACH_READY
        biz.facebook_url = "https://facebook.com/trattoriabella"
        with patch("lib.validation.social_validator.SocialIdentityValidator.verify_ownership") as mock_soc:
            mock_soc.return_value = {
                "social_status": "SOCIAL_FOUND",
                "social_ownership_status": "VERIFIED",
                "social_profile_status": "ACCESSIBLE",
                "social_activity": "ACTIVE",
                "verified_urls": {"facebook": "https://facebook.com/trattoriabella"},
                "verified_handles": {"facebook": "trattoriabella"},
                "red_flags": [],
                "evidence_notes": []
            }
            qual_social = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
            self.assertEqual(qual_social["qualification_state"], QualificationState.OUTREACH_READY.value)
            self.assertEqual(qual_social["score"], 75)
            self.assertEqual(qual_social["priority"], Priority.MEDIUM.value)

        # Invariant 3: Increase review count within passing range (150 -> 600) -> score increases, qualification remains OUTREACH_READY
        biz.facebook_url = ""
        biz.review_count = 600
        qual_600 = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(qual_600["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(qual_600["score"], 60) # 25 revs + 10 rating + 10 premises + 10 booking + 5 visibility = 60
        self.assertEqual(qual_600["priority"], Priority.LOW.value)

        # Invariant 4: Increase review count to 1500 -> score 65, priority MEDIUM, qualification remains OUTREACH_READY
        biz.review_count = 1500
        qual_1500 = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(qual_1500["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(qual_1500["score"], 65) # 30 revs + 10 rating + 10 premises + 10 booking + 5 visibility = 65
        self.assertEqual(qual_1500["priority"], Priority.MEDIUM.value)


if __name__ == "__main__":
    unittest.main()
