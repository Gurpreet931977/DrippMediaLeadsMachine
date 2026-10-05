#!/usr/bin/env python3
"""
Qualification Engine V3 - Operational Verification & Evidence Reliability Test Suite
Covers the 12 required V3 test cases from the specification:
1. Inaccessible Instagram URL -> not verified
2. Instagram post only -> not verified
3. Unrelated Instagram account -> not verified
4. Valid business-owned Instagram profile -> verified
5. Stale evidence + no current corroboration -> not ACTIVE_CONFIRMED
6. Current business-owned social + corroborating current evidence -> ACTIVE_CONFIRMED
7. High review count but no current operational evidence -> not OUTREACH_READY
8. Permanently closed business -> EXCLUDED
9. Business identity mismatch -> not OUTREACH_READY
10. Valid business + no website + verified operation -> eligible for qualification
11. Cutee-type case -> cannot automatically remain OUTREACH_READY (routes to MANUAL_REVIEW)
12. Mala/Hong Thai-type verified social cases -> continue through normal qualification
"""

import unittest
from unittest.mock import patch
from datetime import datetime, timedelta

from lib.types import (
    DiscoveredBusiness,
    VerificationStatus,
    QualificationState,
    CountryStatus,
    Priority,
    OperationalStatus,
    OperationalConfidence,
    EvidenceFreshness,
    SocialOwnershipStatus,
    SocialProfileStatus,
    SocialActivityStatus
)
from lib.validation.social_validator import SocialIdentityValidator
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.outreach.outreach_generator import OutreachAngleGenerator

class TestQualificationEngineV3(unittest.TestCase):

    def setUp(self):
        self.scorer = LeadScoringProvider()
        self.outreach = OutreachAngleGenerator()

    # -------------------------------------------------------------------------
    # TEST 1: Inaccessible Instagram URL -> not verified
    # -------------------------------------------------------------------------
    def test_01_inaccessible_instagram_url_not_verified(self):
        biz_name = "The Northern Corner"
        city = "Manchester"
        social_url = "https://www.instagram.com/nonexistent_broken_handle_404_fail/"

        with patch.object(SocialIdentityValidator, "check_profile_accessibility", return_value=(False, "HTTP 404: Profile not found", "INACCESSIBLE")):
            audit = SocialIdentityValidator.verify_ownership(
                business_name=biz_name,
                city=city,
                industry="Cafe",
                social_urls={"instagram": social_url}
            )

        self.assertNotEqual(audit["social_ownership_status"], SocialOwnershipStatus.VERIFIED.value)
        self.assertEqual(audit["social_profile_status"], SocialProfileStatus.INACCESSIBLE.value)
        self.assertEqual(len(audit["verified_urls"]), 0)
        self.assertTrue(any("inaccessible" in rf.lower() or "404" in rf.lower() for rf in audit["red_flags"]))

    # -------------------------------------------------------------------------
    # TEST 2: Instagram post only -> not verified
    # -------------------------------------------------------------------------
    def test_02_instagram_post_only_not_verified(self):
        biz_name = "Piccadilly Coffee"
        city = "Manchester"
        post_url = "https://www.instagram.com/p/DF3x891Axyz/"

        audit = SocialIdentityValidator.verify_ownership(
            business_name=biz_name,
            city=city,
            industry="Coffee Shop",
            social_urls={"instagram": post_url}
        )

        self.assertNotEqual(audit["social_ownership_status"], SocialOwnershipStatus.VERIFIED.value)
        self.assertEqual(audit["social_profile_status"], SocialProfileStatus.INVALID_FORMAT.value)
        self.assertEqual(len(audit["verified_urls"]), 0)
        self.assertTrue(any("individual instagram post url" in rf.lower() or "not an account profile" in rf.lower() for rf in audit["red_flags"]))

    # -------------------------------------------------------------------------
    # TEST 3: Unrelated Instagram account -> not verified
    # -------------------------------------------------------------------------
    def test_03_unrelated_instagram_account_not_verified(self):
        biz_name = "Northern Soul Grilled Cheese"
        city = "Manchester"
        unrelated_url = "https://www.instagram.com/chimokungfu/"

        with patch.object(SocialIdentityValidator, "check_profile_accessibility", return_value=(True, "OK", "ACCESSIBLE")):
            audit = SocialIdentityValidator.verify_ownership(
                business_name=biz_name,
                city=city,
                industry="Restaurant",
                social_urls={"instagram": unrelated_url}
            )

        self.assertEqual(audit["social_ownership_status"], SocialOwnershipStatus.UNVERIFIED.value)
        self.assertEqual(len(audit["verified_urls"]), 0)
        self.assertTrue(any("unrelated" in rf.lower() or "no clear association" in rf.lower() for rf in audit["red_flags"]))

    # -------------------------------------------------------------------------
    # TEST 4: Valid business-owned Instagram profile -> verified
    # -------------------------------------------------------------------------
    def test_04_valid_business_owned_instagram_profile_verified(self):
        biz_name = "Mala"
        city = "Manchester"
        valid_url = "https://www.instagram.com/malamcr/"

        with patch.object(SocialIdentityValidator, "check_profile_accessibility", return_value=(True, "OK", "ACCESSIBLE")):
            audit = SocialIdentityValidator.verify_ownership(
                business_name=biz_name,
                city=city,
                industry="Bar & Restaurant",
                social_urls={"instagram": valid_url}
            )

        self.assertEqual(audit["social_ownership_status"], SocialOwnershipStatus.VERIFIED.value)
        self.assertEqual(audit["social_profile_status"], SocialProfileStatus.ACCESSIBLE.value)
        self.assertIn("instagram", audit["verified_urls"])
        self.assertEqual(audit["verified_handles"].get("instagram"), "malamcr")

    # -------------------------------------------------------------------------
    # TEST 5: Stale evidence + no current corroboration -> not ACTIVE_CONFIRMED
    # -------------------------------------------------------------------------
    def test_05_stale_evidence_not_active_confirmed(self):
        # Review from 300 days ago (>180 days -> STALE)
        stale_date = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
        biz = DiscoveredBusiness(
            company_name="Old Brickhouse Grill",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            country_status=CountryStatus.COUNTRY_MATCH.value,
            address="12 Oldham St, Manchester M1 1JN",
            phone="+44 161 228 1234",
            raw_website="",
            review_count=350,
            rating=4.5,
            latest_review_date=stale_date,
            opening_hours="Mon-Sun 12:00-22:00"
        )

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {},
            "verified_handles": {},
            "red_flags": []
        }

        op_audit = OperationalValidator.verify_operations(biz, social_audit)
        self.assertEqual(op_audit["evidence_freshness"], EvidenceFreshness.STALE.value)
        self.assertNotEqual(op_audit["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertIn(op_audit["operational_status"], [OperationalStatus.ACTIVE_LIKELY.value, OperationalStatus.OPERATIONAL_UNKNOWN.value])

    # -------------------------------------------------------------------------
    # TEST 6: Current business-owned social + corroborating evidence -> ACTIVE_CONFIRMED
    # -------------------------------------------------------------------------
    def test_06_current_social_plus_corroboration_active_confirmed(self):
        recent_date = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d")
        biz = DiscoveredBusiness(
            company_name="Hong Thai",
            category="Thai Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            country_status=CountryStatus.COUNTRY_MATCH.value,
            address="44 Oldham Rd, Manchester M4 5EE",
            phone="+44 161 832 9999",
            raw_website="",
            instagram_url="https://www.instagram.com/hongthaimcr/",
            review_count=210,
            rating=4.7,
            latest_social_post_date=recent_date,
            latest_review_date=recent_date,
            opening_hours="Tue-Sat 12:00-21:00"
        )

        with patch.object(SocialIdentityValidator, "check_profile_accessibility", return_value=(True, "OK", "ACCESSIBLE")):
            eval_result = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertEqual(eval_result["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(eval_result["operational_confidence"], OperationalConfidence.HIGH.value)
        self.assertEqual(eval_result["qualification_state"], QualificationState.OUTREACH_READY.value)

    # -------------------------------------------------------------------------
    # TEST 7: High review count but no current operational evidence -> not OUTREACH_READY
    # -------------------------------------------------------------------------
    def test_07_high_reviews_no_current_operations_not_outreach_ready(self):
        # 1,500 reviews, but stale review date (400 days ago), no active social
        stale_date = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
        biz = DiscoveredBusiness(
            company_name="Historic Tavern",
            category="Pub & Bar",
            city="Manchester",
            target_country="United Kingdom",
            country_status=CountryStatus.COUNTRY_MATCH.value,
            address="15 Deansgate, Manchester M3 2NW",
            phone="+44 161 834 1111",
            raw_website="",
            review_count=1500,
            rating=4.6,
            latest_review_date=stale_date
        )

        eval_result = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertNotEqual(eval_result["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertNotEqual(eval_result["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(eval_result["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    # -------------------------------------------------------------------------
    # TEST 8: Permanently closed business -> EXCLUDED
    # -------------------------------------------------------------------------
    def test_08_permanently_closed_business_excluded(self):
        biz = DiscoveredBusiness(
            company_name="Old City Diner",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            country_status=CountryStatus.COUNTRY_MATCH.value,
            address="100 Market St, Manchester M1 1NN",
            phone="+44 161 234 5678",
            raw_website="",
            review_count=450,
            rating=4.4,
            is_permanently_closed=True
        )

        eval_result = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertEqual(eval_result["operational_status"], OperationalStatus.CLOSED_OR_UNVERIFIED.value)
        self.assertEqual(eval_result["qualification_state"], QualificationState.EXCLUDED.value)
        self.assertFalse(eval_result["is_outreach_ready"])

    # -------------------------------------------------------------------------
    # TEST 9: Business identity mismatch -> not OUTREACH_READY
    # -------------------------------------------------------------------------
    def test_09_business_identity_mismatch_not_outreach_ready(self):
        biz = DiscoveredBusiness(
            company_name="Manchester Artisan Bakery",
            category="Bakery",
            city="Manchester",
            target_country="United Kingdom",
            country_status=CountryStatus.COUNTRY_MATCH.value,
            address="5 Tib St, Manchester M4 1CG",
            phone="+44 161 839 2222",
            raw_website="",
            instagram_url="https://www.instagram.com/tokyo_ramen_bar_japan/",
            review_count=180,
            rating=4.6
        )

        with patch.object(SocialIdentityValidator, "check_profile_accessibility", return_value=(True, "OK", "ACCESSIBLE")):
            eval_result = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertNotEqual(eval_result["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(eval_result["qualification_state"], QualificationState.MANUAL_REVIEW.value)
        self.assertTrue(any("mismatch" in rf.lower() or "unrelated" in rf.lower() for rf in eval_result["red_flags"]))

    # -------------------------------------------------------------------------
    # TEST 10: Valid business + no website + verified operation -> eligible for qualification
    # -------------------------------------------------------------------------
    def test_10_valid_business_no_website_verified_operation_eligible(self):
        recent_date = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
        biz = DiscoveredBusiness(
            company_name="Northern Kitchen",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            country_status=CountryStatus.COUNTRY_MATCH.value,
            address="77 High St, Manchester M4 1FS",
            phone="+44 161 832 7777",
            raw_website="",
            instagram_url="https://www.instagram.com/northernkitchenmcr/",
            review_count=145,
            rating=4.6,
            latest_social_post_date=recent_date,
            latest_review_date=recent_date,
            opening_hours="Mon-Sun 11:30-22:00"
        )

        with patch.object(SocialIdentityValidator, "check_profile_accessibility", return_value=(True, "OK", "ACCESSIBLE")):
            eval_result = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertEqual(eval_result["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(eval_result["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertTrue(eval_result["is_outreach_ready"])
        self.assertGreaterEqual(eval_result["score"], 60)

    # -------------------------------------------------------------------------
    # TEST 11: Cutee-type case -> cannot automatically remain OUTREACH_READY
    # -------------------------------------------------------------------------
    def test_11_cutee_type_case_routes_to_manual_review(self):
        # Current automated record has social URLs, but QA found invalid /profilecard/ dynamic link
        biz = DiscoveredBusiness(
            company_name="Cutee Cafe & Restaurant",
            category="Cafe",
            city="Manchester",
            target_country="United Kingdom",
            country_status=CountryStatus.COUNTRY_MATCH.value,
            address="150 Stockport Rd, Manchester M13 9AB",
            phone="+44 161 273 4567",
            raw_website="",
            # Invalid dynamic profilecard link as discovered in real run
            instagram_url="https://www.instagram.com/profilecard/visit?guide_id=123456",
            review_count=112,
            rating=4.3,
            opening_hours="Mon-Sun 10:00-22:00"
        )

        eval_result = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)

        # Must NOT be OUTREACH_READY
        self.assertNotEqual(eval_result["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(eval_result["qualification_state"], QualificationState.MANUAL_REVIEW.value)
        self.assertEqual(eval_result["social_profile_status"], SocialProfileStatus.INVALID_FORMAT.value)
        self.assertEqual(eval_result["social_ownership_status"], SocialOwnershipStatus.UNVERIFIED.value)

    # -------------------------------------------------------------------------
    # TEST 12: Mala / Hong Thai-type verified social cases continue through normal qualification
    # -------------------------------------------------------------------------
    def test_12_mala_hong_thai_type_verified_social_remain_outreach_ready(self):
        recent_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        
        # Hong Thai test
        hong_thai = DiscoveredBusiness(
            company_name="Hong Thai",
            category="Thai Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            country_status=CountryStatus.COUNTRY_MATCH.value,
            address="44 Oldham Rd, Manchester M4 5EE",
            phone="+44 161 832 9999",
            raw_website="",
            facebook_url="https://www.facebook.com/HongThaiManchester/",
            review_count=210,
            rating=4.7,
            latest_social_post_date=recent_date,
            latest_review_date=recent_date,
            opening_hours="Tue-Sat 12:00-21:00"
        )

        with patch.object(SocialIdentityValidator, "check_profile_accessibility", return_value=(True, "OK", "ACCESSIBLE")):
            hong_eval = self.scorer.evaluate_lead(hong_thai, VerificationStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertEqual(hong_eval["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(hong_eval["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(hong_eval["social_ownership_status"], SocialOwnershipStatus.VERIFIED.value)

        # Mala test
        mala = DiscoveredBusiness(
            company_name="Mala",
            category="Bar & Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            country_status=CountryStatus.COUNTRY_MATCH.value,
            address="8 Lever St, Manchester M1 1FL",
            phone="+44 161 236 8888",
            raw_website="",
            instagram_url="https://www.instagram.com/malamcr/",
            review_count=185,
            rating=4.5,
            latest_social_post_date=recent_date,
            latest_review_date=recent_date,
            opening_hours="Mon-Sun 12:00-23:00"
        )

        with patch.object(SocialIdentityValidator, "check_profile_accessibility", return_value=(True, "OK", "ACCESSIBLE")):
            mala_eval = self.scorer.evaluate_lead(mala, VerificationStatus.NO_WEBSITE_CONFIRMED.value)

        self.assertEqual(mala_eval["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(mala_eval["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(mala_eval["social_ownership_status"], SocialOwnershipStatus.VERIFIED.value)

if __name__ == "__main__":
    unittest.main()
