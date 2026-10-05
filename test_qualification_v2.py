#!/usr/bin/env python3
"""
Test Suite for Qualification Engine V2
Covers the 12 required test conditions:
  1. 1 review -> RESEARCH_ONLY
  2. 7 reviews -> RESEARCH_ONLY
  3. 42 reviews -> MANUAL_REVIEW
  4. 50 reviews + rating 4.0 + verified social + no website -> potentially OUTREACH_READY
  5. 300 reviews + rating 3.0 -> not OUTREACH_READY
  6. 300 reviews + no verified social + insufficient evidence -> MANUAL_REVIEW
  7. Instagram post URL only -> social ownership not verified
  8. Existing website -> EXCLUDED
  9. No website + weak business evidence -> not OUTREACH_READY
  10. Duplicate business -> one canonical record
  11. +44 phone -> stored as text (starts with ')
  12. Wrong country -> EXCLUDED
"""

import sys
import unittest
from lib.types import (
    DiscoveredBusiness,
    VerificationStatus,
    QualificationState,
    CountryStatus,
    SocialOwnershipStatus,
    SocialActivityStatus,
    Lead,
    ResearchLogEntry
)
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.validation.social_validator import SocialIdentityValidator
from lib.sheets.google_sheets import GoogleSheetsStorageProvider, LEADS_COLUMNS

class TestQualificationEngineV2(unittest.TestCase):

    def setUp(self):
        self.scorer = LeadScoringProvider()

    def test_01_one_review_research_only(self):
        """1 review -> RESEARCH_ONLY (e.g. Happi Kitchen)"""
        biz = DiscoveredBusiness(
            company_name="Happi Kitchen",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            review_count=1,
            rating=5.0,
            address="123 Manchester St, Manchester M1 1AA",
            phone="+44 161 111 2222"
        )
        audit = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertIn("Minimal customer review traction", audit["qualification_reason"])

    def test_02_seven_reviews_research_only(self):
        """7 reviews -> RESEARCH_ONLY (e.g. Panela Street Food)"""
        biz = DiscoveredBusiness(
            company_name="Panela Street Food",
            category="Street Food",
            city="Manchester",
            target_country="United Kingdom",
            review_count=7,
            rating=5.0,
            address="45 Oxford Rd, Manchester M1 7ED",
            phone="+44 161 333 4444"
        )
        audit = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)

    def test_03_forty_two_reviews_manual_review(self):
        """42 reviews -> MANUAL_REVIEW (e.g. Tom de Terre)"""
        biz = DiscoveredBusiness(
            company_name="Tom de Terre",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            review_count=42,
            rating=4.9,
            address="10 Tib St, Manchester M4 1TE",
            phone="+44 161 555 6666",
            instagram_url="https://www.instagram.com/tomdeterremcr/"
        )
        audit = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(audit["qualification_state"], QualificationState.MANUAL_REVIEW.value)
        self.assertIn("Moderate customer traction", audit["qualification_reason"])

    def test_04_fifty_reviews_four_rating_outreach_ready(self):
        """50 reviews + rating 4.0 + verified social + no website -> potentially OUTREACH_READY"""
        from unittest.mock import patch
        biz = DiscoveredBusiness(
            company_name="Artisan Bistro",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            review_count=50,
            rating=4.0,
            address="15 Deansgate, Manchester M3 2BB",
            phone="+44 161 777 8888",
            instagram_url="https://www.instagram.com/artisanbistromcr/"
        )
        with patch.object(SocialIdentityValidator, "check_profile_accessibility", return_value=(True, "OK", "ACCESSIBLE")):
            audit = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(audit["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertTrue(audit["is_outreach_ready"])
        self.assertGreaterEqual(audit["score"], 60)

    def test_05_three_hundred_reviews_low_rating(self):
        """300 reviews + rating 3.0 -> not OUTREACH_READY (MANUAL_REVIEW)"""
        biz = DiscoveredBusiness(
            company_name="City Burger",
            category="Burger restaurant",
            city="Manchester",
            target_country="United Kingdom",
            review_count=300,
            rating=3.0,
            address="20 Market St, Manchester M1 1PW",
            phone="+44 161 999 0000",
            instagram_url="https://www.instagram.com/cityburgermcr/"
        )
        audit = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertNotEqual(audit["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(audit["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    def test_06_three_hundred_reviews_no_verified_social(self):
        """300 reviews + no verified social + insufficient evidence -> MANUAL_REVIEW"""
        biz = DiscoveredBusiness(
            company_name="Anonymous Diner",
            category="Diner",
            city="Manchester",
            target_country="United Kingdom",
            review_count=300,
            rating=4.5,
            address="80 Shudehill, Manchester M4 4AN",
            phone="+44 161 222 3333",
            photo_count=10  # Insufficient alternative digital footprint
        )
        audit = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(audit["qualification_state"], QualificationState.MANUAL_REVIEW.value)
        self.assertIn("insufficient alternative digital footprint", audit["qualification_reason"].lower())

    def test_07_instagram_post_url_not_verified(self):
        """Instagram post URL only -> social ownership not verified"""
        res = SocialIdentityValidator.verify_ownership(
            business_name="Urban Cafe",
            city="Manchester",
            industry="Cafe",
            social_urls={"instagram": "https://www.instagram.com/p/DVI5iN5iMQj/"}
        )
        self.assertEqual(res["social_ownership_status"], SocialOwnershipStatus.UNVERIFIED.value)
        self.assertEqual(len(res["verified_urls"]), 0)
        self.assertTrue(any("Individual Instagram post URL" in rf for rf in res["red_flags"]))

    def test_08_existing_website_excluded(self):
        """Existing website -> EXCLUDED"""
        biz = DiscoveredBusiness(
            company_name="Dishoom Manchester",
            category="Indian restaurant",
            city="Manchester",
            target_country="United Kingdom",
            raw_website="https://www.dishoom.com/manchester/",
            review_count=10709,
            rating=4.8
        )
        audit = self.scorer.evaluate_lead(biz, VerificationStatus.WEBSITE_EXISTS.value)
        self.assertEqual(audit["qualification_state"], QualificationState.EXCLUDED.value)
        self.assertFalse(audit["is_outreach_ready"])

    def test_09_no_website_weak_business_evidence(self):
        """No website + weak business evidence -> not OUTREACH_READY"""
        biz = DiscoveredBusiness(
            company_name="Ghost Stall",
            category="Kiosk",
            city="Manchester",
            target_country="United Kingdom",
            review_count=60,
            rating=4.1,
            address="",  # No address
            phone="",    # No phone
            instagram_url=""  # No social
        )
        audit = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertNotEqual(audit["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(audit["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    def test_10_duplicate_business_canonical(self):
        """Duplicate business -> one canonical normalized identity"""
        storage = GoogleSheetsStorageProvider()
        id1 = storage.normalize_identity("Mackie Mayor", "Manchester", "United Kingdom", "")
        id2 = storage.normalize_identity("MACKIE MAYOR ", "manchester", "UNITED KINGDOM", "")
        id3 = storage.normalize_identity("Mackie Mayor Ltd", "Manchester", "United Kingdom", "")
        self.assertEqual(id1, id2)
        # Verify deduplication key uniqueness across cities/countries
        id_us = storage.normalize_identity("Mackie Mayor", "Manchester", "United States", "")
        self.assertNotEqual(id1, id_us)

    def test_11_phone_stored_as_text(self):
        """+44 phone -> stored as text prefixed with apostrophe to prevent spreadsheet formula error"""
        lead = Lead(
            lead_id="LEAD-TEST-001",
            company_name="Test Pub",
            industry="Pub",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            phone="+44 161 123 4567"
        )
        row = lead.to_sheet_row(LEADS_COLUMNS)
        phone_idx = LEADS_COLUMNS.index("phone")
        phone_cell = row[phone_idx]
        self.assertTrue(phone_cell.startswith("'+44"), f"Expected phone cell to start with apostrophe, got: {phone_cell}")

        # Also test ResearchLogEntry
        entry = ResearchLogEntry(
            research_id="RES-TEST-001",
            company_name="Test Pub",
            industry="Pub",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            country_status="COUNTRY_MATCH",
            city="Manchester",
            phone="+44 7700 900077"
        )
        from lib.sheets.google_sheets import RESEARCH_LOG_COLUMNS
        row_res = entry.to_sheet_row(RESEARCH_LOG_COLUMNS)
        res_phone_idx = RESEARCH_LOG_COLUMNS.index("phone")
        self.assertTrue(row_res[res_phone_idx].startswith("'+44"))

    def test_12_wrong_country_excluded(self):
        """Wrong country -> EXCLUDED"""
        biz = DiscoveredBusiness(
            company_name="Manchester Bar & Grill",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United States",
            country_status=CountryStatus.COUNTRY_MISMATCH.value,
            address="123 Main St, Manchester, NH 03101, USA",
            review_count=120,
            rating=4.5
        )
        audit = self.scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(audit["qualification_state"], QualificationState.EXCLUDED.value)
        self.assertIn("Country mismatch", audit["qualification_reason"])

if __name__ == "__main__":
    unittest.main()
