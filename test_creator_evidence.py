#!/usr/bin/env python3
"""
Comprehensive Test Suite: Creator / Influencer Evidence Engine (Section 15)
==========================================================================
Covers the 17 required test cases from the specification:
1. business @tag
2. business name in caption
3. location name in caption
4. exact street in caption
5. location tag
6. business-specific hashtag
7. business name in reel text
8. business name + city
9. location-only ambiguous reference
10. wrong business with same name
11. multiple matching signals
12. old creator content
13. recent creator content
14. creator reference discovers official business profile
15. creator reference never equals ownership verification
16. creator reference supports operational verification
17. creator reference does not bypass V3 qualification
"""

import unittest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

from lib.types import (
    CreatorEvidenceStatus,
    CreatorEvidenceConfidence,
    CreatorFreshness,
    CreatorReferenceType,
    CreatorEvidenceItem,
    CreatorEvidenceSummary,
    SocialOwnershipStatus,
    SocialProfileStatus,
    OperationalStatus,
    OperationalConfidence,
    DiscoveredBusiness,
    QualificationState,
    Lead
)
from lib.validation.creator_evidence import CreatorEvidenceValidator
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider


class TestCreatorEvidenceEngine(unittest.TestCase):

    def setUp(self):
        self.validator = CreatorEvidenceValidator
        self.target_biz = {
            "company_name": "Hong Thai",
            "trading_name": "Hong Thai Restaurant",
            "city": "Manchester",
            "address": "9 Oldham Road, Ancoats, Manchester, M4 5DB",
            "postcode": "M4 5DB",
            "category": "Thai Restaurant"
        }

    # -------------------------------------------------------------------------
    # TEST 1: business @tag
    # -------------------------------------------------------------------------
    def test_01_business_account_tag(self):
        post_data = {
            "creator_handle": "@mcr_foodie",
            "creator_name": "Manchester Foodie",
            "platform": "Instagram",
            "content_url": "https://www.instagram.com/reel/C123456789/",
            "content_type": "Reel",
            "published_at": (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d"),
            "caption": "Amazing food here! Go check them out @hongthai_mcr",
            "tagged_handles": ["@hongthai_mcr"]
        }

        item = self.validator.evaluate_creator_post(post_data, self.target_biz)

        self.assertEqual(item.creator_evidence_status, CreatorEvidenceStatus.FOUND.value)
        self.assertEqual(item.reference_type, CreatorReferenceType.ACCOUNT_TAG.value)
        self.assertEqual(item.tagged_business_handle, "@hongthai_mcr")
        self.assertEqual(item.business_tag, "@hongthai_mcr")
        self.assertEqual(item.creator_handle, "@mcr_foodie")
        self.assertIn("tagged_business_handle", item.matching_signals)
        # Creator handle is distinct from business handle
        self.assertNotEqual(item.creator_handle, item.tagged_business_handle)

    # -------------------------------------------------------------------------
    # TEST 2: business name in caption
    # -------------------------------------------------------------------------
    def test_02_business_name_in_caption(self):
        post_data = {
            "creator_handle": "@uk_eats",
            "platform": "Instagram",
            "content_url": "https://www.instagram.com/p/C987654321/",
            "content_type": "Post",
            "published_at": (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%d"),
            "caption": "Had dinner at Hong Thai last night. Delicious pad thai!"
        }

        item = self.validator.evaluate_creator_post(post_data, self.target_biz)

        self.assertEqual(item.creator_evidence_status, CreatorEvidenceStatus.FOUND.value)
        self.assertEqual(item.reference_type, CreatorReferenceType.CAPTION_BUSINESS_MENTION.value)
        self.assertTrue(item.business_name_mentioned)
        self.assertIn("exact_business_name", item.matching_signals)
        self.assertEqual(item.business_reference, "Hong Thai")

    # -------------------------------------------------------------------------
    # TEST 3: location name in caption
    # -------------------------------------------------------------------------
    def test_03_location_name_in_caption(self):
        post_data = {
            "creator_handle": "@city_roamer",
            "platform": "TikTok",
            "content_url": "https://www.tiktok.com/@city_roamer/video/111222333",
            "content_type": "TikTok",
            "published_at": (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%d"),
            "caption": "Found this incredible Thai spot tucked away in Ancoats!"
        }

        item = self.validator.evaluate_creator_post(post_data, self.target_biz)

        self.assertEqual(item.creator_evidence_status, CreatorEvidenceStatus.FOUND.value)
        self.assertEqual(item.reference_type, CreatorReferenceType.CAPTION_LOCATION_MENTION.value)
        self.assertFalse(item.business_name_mentioned)
        self.assertIn("neighborhood_mention (Ancoats)", item.matching_signals)
        # Location-only safety: confidence must be MEDIUM or LOW, never HIGH
        self.assertIn(item.evidence_confidence, [CreatorEvidenceConfidence.MEDIUM.value, CreatorEvidenceConfidence.LOW.value])
        self.assertNotEqual(item.evidence_confidence, CreatorEvidenceConfidence.HIGH.value)

    # -------------------------------------------------------------------------
    # TEST 4: exact street in caption
    # -------------------------------------------------------------------------
    def test_04_exact_street_in_caption(self):
        post_data = {
            "creator_handle": "@mcr_street_eats",
            "platform": "Instagram",
            "content_url": "https://www.instagram.com/reel/C555666777/",
            "content_type": "Reel",
            "published_at": (datetime.now(timezone.utc) - timedelta(days=12)).strftime("%Y-%m-%d"),
            "caption": "Authentic Thai food right on Oldham Road. A true hidden gem."
        }

        item = self.validator.evaluate_creator_post(post_data, self.target_biz)

        self.assertEqual(item.creator_evidence_status, CreatorEvidenceStatus.FOUND.value)
        self.assertIn("exact_street_mention (Oldham Road)", item.matching_signals)
        self.assertEqual(item.reference_type, CreatorReferenceType.CAPTION_LOCATION_MENTION.value)

    # -------------------------------------------------------------------------
    # TEST 5: location tag
    # -------------------------------------------------------------------------
    def test_05_location_tag(self):
        post_data = {
            "creator_handle": "@weekend_foodie",
            "platform": "Instagram",
            "content_url": "https://www.instagram.com/p/C777888999/",
            "content_type": "Post",
            "published_at": (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d"),
            "caption": "Lovely evening meal with friends!",
            "location_tag": "Hong Thai",
            "location_tag_url": "https://www.instagram.com/explore/locations/12345/hong-thai/",
            "location_tag_text": "Hong Thai, Manchester"
        }

        item = self.validator.evaluate_creator_post(post_data, self.target_biz)

        self.assertEqual(item.creator_evidence_status, CreatorEvidenceStatus.FOUND.value)
        self.assertEqual(item.reference_type, CreatorReferenceType.LOCATION_TAG.value)
        self.assertEqual(item.location_tag, "Hong Thai")
        self.assertEqual(item.location_tag_name, "Hong Thai")
        self.assertEqual(item.location_tag_url, "https://www.instagram.com/explore/locations/12345/hong-thai/")
        self.assertIn("matching_location_tag", item.matching_signals)

    # -------------------------------------------------------------------------
    # TEST 6: business-specific hashtag
    # -------------------------------------------------------------------------
    def test_06_business_specific_hashtag(self):
        post_data = {
            "creator_handle": "@foodlover_uk",
            "platform": "Instagram",
            "content_url": "https://www.instagram.com/p/C333444555/",
            "content_type": "Post",
            "published_at": (datetime.now(timezone.utc) - timedelta(days=18)).strftime("%Y-%m-%d"),
            "caption": "Best curry in town! #HongThai #HongThaiManchester #food #dinner #manchester"
        }

        item = self.validator.evaluate_creator_post(post_data, self.target_biz)

        self.assertEqual(item.creator_evidence_status, CreatorEvidenceStatus.FOUND.value)
        self.assertEqual(item.reference_type, CreatorReferenceType.HASHTAG_MENTION.value)
        self.assertIn("#HongThai", item.hashtags)
        self.assertIn("#HongThaiManchester", item.hashtags)
        self.assertIn("business_specific_hashtag", item.matching_signals)

    # -------------------------------------------------------------------------
    # TEST 7: business name in reel text (ON_CONTENT_TEXT)
    # -------------------------------------------------------------------------
    def test_07_business_name_in_reel_text(self):
        post_data = {
            "creator_handle": "@reel_explorer",
            "platform": "Instagram",
            "content_url": "https://www.instagram.com/reel/C888999000/",
            "content_type": "Reel",
            "published_at": (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%d"),
            "caption": "Check out this spot!",
            "on_content_text": "Hong Thai Manchester"
        }

        item = self.validator.evaluate_creator_post(post_data, self.target_biz)

        self.assertEqual(item.creator_evidence_status, CreatorEvidenceStatus.FOUND.value)
        self.assertEqual(item.reference_type, CreatorReferenceType.ON_CONTENT_TEXT.value)
        self.assertIn("on_content_text", item.matching_signals)
        self.assertEqual(item.content_text_reference, "Hong Thai Manchester")

    # -------------------------------------------------------------------------
    # TEST 8: business name + city
    # -------------------------------------------------------------------------
    def test_08_business_name_plus_city(self):
        post_data = {
            "creator_handle": "@travel_bites",
            "platform": "YouTube",
            "content_url": "https://www.youtube.com/shorts/abc123xyz",
            "content_type": "Shorts",
            "published_at": (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"),
            "caption": "Visiting Hong Thai in Manchester today!"
        }

        item = self.validator.evaluate_creator_post(post_data, self.target_biz)

        self.assertEqual(item.creator_evidence_status, CreatorEvidenceStatus.FOUND.value)
        self.assertTrue(item.business_name_mentioned)
        self.assertIn("city_mention (Manchester)", item.matching_signals)
        # Section 9 Rule: One strong signal + contextual support (Manchester) = MEDIUM
        self.assertEqual(item.evidence_confidence, CreatorEvidenceConfidence.MEDIUM.value)

    # -------------------------------------------------------------------------
    # TEST 9: location-only ambiguous reference
    # -------------------------------------------------------------------------
    def test_09_location_only_ambiguous_reference(self):
        post_data = {
            "creator_handle": "@local_explorer",
            "platform": "Instagram",
            "content_url": "https://www.instagram.com/reel/C444555666/",
            "content_type": "Reel",
            "published_at": (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d"),
            "caption": "Found this amazing Thai restaurant on Oldham Road in Ancoats."
        }

        item = self.validator.evaluate_creator_post(post_data, self.target_biz)

        self.assertEqual(item.creator_evidence_status, CreatorEvidenceStatus.FOUND.value)
        self.assertEqual(item.reference_type, CreatorReferenceType.CAPTION_LOCATION_MENTION.value)
        self.assertFalse(item.business_name_mentioned)
        # Safety rule: must be MEDIUM or LOW, never HIGH
        self.assertEqual(item.evidence_confidence, CreatorEvidenceConfidence.MEDIUM.value)
        self.assertNotEqual(item.evidence_confidence, CreatorEvidenceConfidence.HIGH.value)

    # -------------------------------------------------------------------------
    # TEST 10: wrong business with same name (Conflicting City)
    # -------------------------------------------------------------------------
    def test_10_wrong_business_conflicting_city(self):
        post_data = {
            "creator_handle": "@london_food_diaries",
            "platform": "Instagram",
            "content_url": "https://www.instagram.com/p/C999888777/",
            "content_type": "Post",
            "published_at": (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"),
            "caption": "Best Thai food in London! Hong Thai never disappoints."
        }

        item = self.validator.evaluate_creator_post(post_data, self.target_biz)

        self.assertEqual(item.creator_evidence_status, CreatorEvidenceStatus.REJECTED.value)
        self.assertEqual(item.evidence_confidence, CreatorEvidenceConfidence.UNKNOWN.value)
        self.assertIn("Conflicting city detected: London", item.disqualification_reason)

    # -------------------------------------------------------------------------
    # TEST 11: multiple matching signals
    # -------------------------------------------------------------------------
    def test_11_multiple_matching_signals(self):
        post_data = {
            "creator_handle": "@mcr_gourmet",
            "platform": "Instagram",
            "content_url": "https://www.instagram.com/reel/C111222333/",
            "content_type": "Reel",
            "published_at": (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d"),
            "caption": "Finally tried Hong Thai in Ancoats Manchester on Oldham Road! Tagging @hongthai_mcr",
            "location_tag": "Hong Thai",
            "tagged_handles": ["@hongthai_mcr"]
        }

        item = self.validator.evaluate_creator_post(post_data, self.target_biz)

        self.assertEqual(item.creator_evidence_status, CreatorEvidenceStatus.FOUND.value)
        self.assertEqual(item.reference_type, CreatorReferenceType.MULTI_SIGNAL.value)
        self.assertEqual(item.evidence_confidence, CreatorEvidenceConfidence.HIGH.value)
        self.assertTrue(len(item.matching_signals) >= 4)
        self.assertIn("exact_business_name", item.matching_signals)
        self.assertIn("matching_location_tag", item.matching_signals)
        self.assertIn("tagged_business_handle", item.matching_signals)

    # -------------------------------------------------------------------------
    # TEST 12: old creator content (> 180 days -> STALE)
    # -------------------------------------------------------------------------
    def test_12_old_creator_content_stale(self):
        stale_date = (datetime.now(timezone.utc) - timedelta(days=220)).strftime("%Y-%m-%d")
        freshness = self.validator.calculate_freshness(stale_date)

        self.assertEqual(freshness, CreatorFreshness.STALE.value)

        post_data = {
            "creator_handle": "@old_blogger",
            "platform": "Instagram",
            "content_url": "https://www.instagram.com/p/Coldpost123/",
            "content_type": "Post",
            "published_at": stale_date,
            "caption": "Hong Thai in Manchester was great last summer!"
        }
        item = self.validator.evaluate_creator_post(post_data, self.target_biz)
        self.assertEqual(item.freshness, CreatorFreshness.STALE.value)

    # -------------------------------------------------------------------------
    # TEST 13: recent creator content (0-90 CURRENT, 91-180 RECENT)
    # -------------------------------------------------------------------------
    def test_13_recent_creator_content_current_and_recent(self):
        current_date = (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%d")
        recent_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")

        self.assertEqual(self.validator.calculate_freshness(current_date), CreatorFreshness.CURRENT.value)
        self.assertEqual(self.validator.calculate_freshness(recent_date), CreatorFreshness.RECENT.value)

    # -------------------------------------------------------------------------
    # TEST 14: creator reference discovers official business profile
    # -------------------------------------------------------------------------
    def test_14_creator_reference_discovers_official_profile(self):
        item = CreatorEvidenceItem(
            creator_evidence_status=CreatorEvidenceStatus.FOUND.value,
            creator_handle="@mcr_influencer",
            platform="Instagram",
            content_url="https://www.instagram.com/reel/C123/",
            tagged_business_handle="@hongthai_mcr"
        )

        with patch("lib.validation.social_validator.SocialIdentityValidator.validate_social_identity") as mock_audit:
            mock_audit.return_value = {
                "clean_url": "https://www.instagram.com/hongthai_mcr/",
                "social_ownership_status": SocialOwnershipStatus.VERIFIED.value,
                "social_profile_status": SocialProfileStatus.ACCESSIBLE.value,
                "rejection_reason": ""
            }

            discovery_res = self.validator.discover_and_validate_official_social(item, self.target_biz)

            self.assertIsNotNone(discovery_res)
            self.assertEqual(discovery_res["candidate_handle"], "@hongthai_mcr")
            self.assertEqual(discovery_res["validation_status"], "VERIFIED")
            self.assertEqual(discovery_res["verified_url"], "https://www.instagram.com/hongthai_mcr/")
            mock_audit.assert_called_once()

    # -------------------------------------------------------------------------
    # TEST 15: creator reference never equals ownership verification
    # -------------------------------------------------------------------------
    def test_15_creator_reference_never_equals_ownership_verification(self):
        post_data = {
            "creator_handle": "@influencer_king",
            "platform": "Instagram",
            "content_url": "https://www.instagram.com/reel/C999/",
            "caption": "Had dinner at Hong Thai! Follow them @hongthai_mcr",
            "tagged_handles": ["@hongthai_mcr"]
        }

        item = self.validator.evaluate_creator_post(post_data, self.target_biz)

        # The creator item itself is strictly third-party evidence
        self.assertEqual(item.creator_evidence_status, CreatorEvidenceStatus.FOUND.value)
        self.assertEqual(item.creator_handle, "@influencer_king")
        # Creator handle is NOT assigned as the verified business profile
        self.assertNotEqual(item.creator_handle, item.tagged_business_handle)

        # Independent validation must be passed before any ownership verification is granted
        with patch("lib.validation.social_validator.SocialIdentityValidator.validate_social_identity") as mock_audit:
            # If independent validation fails (e.g. mismatch or generic account)
            mock_audit.return_value = {
                "clean_url": "https://www.instagram.com/hongthai_mcr/",
                "social_ownership_status": SocialOwnershipStatus.POTENTIAL_MISMATCH.value,
                "social_profile_status": SocialProfileStatus.ACCESSIBLE.value,
                "rejection_reason": "Account bio indicates personal fan page"
            }

            discovery = self.validator.discover_and_validate_official_social(item, self.target_biz)
            self.assertEqual(discovery["validation_status"], "REJECTED")
            self.assertNotEqual(discovery["social_ownership_status"], SocialOwnershipStatus.VERIFIED.value)

    # -------------------------------------------------------------------------
    # TEST 16: creator reference supports operational verification
    # -------------------------------------------------------------------------
    def test_16_creator_reference_supports_operational_verification(self):
        # Case A: Creator evidence alone (no reviews, no operational listing)
        # MUST NEVER produce ACTIVE_CONFIRMED (Section 12)
        alone_creator_ev = {
            "creator_evidence_status": CreatorEvidenceStatus.FOUND.value,
            "creator_latest_date": (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d"),
            "creator_evidence_confidence": CreatorEvidenceConfidence.HIGH.value
        }

        biz_alone = DiscoveredBusiness(
            company_name="Hong Thai",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            review_count=0,
            latest_review_date=""
        )
        op_alone = OperationalValidator.verify_operations(
            business=biz_alone,
            social_audit={},
            creator_evidence=alone_creator_ev
        )
        self.assertNotEqual(op_alone["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertIn("lacking verified strong current operational evidence", op_alone["operational_evidence"])

        # Case B: Creator evidence corroborating current operational review & verified social baseline
        from lib.types import SocialActivityStatus
        social_audit_verified = {
            "verified_urls": {"instagram": "https://www.instagram.com/hongthai_mcr/"},
            "social_ownership_status": SocialOwnershipStatus.VERIFIED.value,
            "social_profile_status": SocialProfileStatus.ACCESSIBLE.value,
            "social_activity": SocialActivityStatus.ACTIVE.value
        }
        biz_corroborated = DiscoveredBusiness(
            company_name="Hong Thai",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            review_count=85,
            latest_social_post_date=(datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d"),
            latest_review_date=(datetime.now(timezone.utc) - timedelta(days=25)).strftime("%Y-%m-%d")
        )
        op_corroborated = OperationalValidator.verify_operations(
            business=biz_corroborated,
            social_audit=social_audit_verified,
            creator_evidence=alone_creator_ev
        )
        self.assertEqual(op_corroborated["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertIn("Current third-party creator/influencer content corroboration", op_corroborated["corroborating_sources"])

    # -------------------------------------------------------------------------
    # TEST 17: creator reference does not bypass V3 qualification
    # -------------------------------------------------------------------------
    def test_17_creator_reference_does_not_bypass_v3_qualification(self):
        scorer = LeadScoringProvider()

        # Business with viral creator evidence (HIGH confidence, CURRENT date)
        creator_summary = {
            "creator_evidence_status": CreatorEvidenceStatus.FOUND.value,
            "creator_evidence_count": 5,
            "creator_evidence_confidence": CreatorEvidenceConfidence.HIGH.value,
            "creator_latest_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "creator_evidence_summary": "5 public creator references found",
            "creator_evidence_urls": ["https://www.instagram.com/reel/1/"],
            "discovered_official_handles": []
        }

        # Gate 1: Insufficient reviews (< 50 reviews) -> Cannot be OUTREACH_READY
        biz_low_reviews = DiscoveredBusiness(
            company_name="Viral Spot",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            review_count=20,  # Below 50 threshold
            rating=4.8,
            raw_data={"creator_evidence": creator_summary}
        )
        scored_1 = scorer.evaluate_lead(biz_low_reviews, verification_status="NO_WEBSITE_CONFIRMED")
        self.assertNotEqual(scored_1["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertIn("Manual Review", scored_1["qualification_reason"])

        # Gate 2: Low rating (< 4.0) -> Cannot be OUTREACH_READY
        biz_low_rating = DiscoveredBusiness(
            company_name="Viral Spot 2",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            review_count=120,
            rating=3.5,  # Below 4.0 threshold
            raw_data={"creator_evidence": creator_summary}
        )
        scored_2 = scorer.evaluate_lead(biz_low_rating, verification_status="NO_WEBSITE_CONFIRMED")
        self.assertNotEqual(scored_2["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertIn("Manual Review", scored_2["qualification_reason"])

        # Gate 3: Website exists -> EXCLUDED
        biz_with_website = DiscoveredBusiness(
            company_name="Viral Spot 3",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            review_count=150,
            rating=4.6,
            raw_website="https://www.viralspot3.co.uk",
            raw_data={"creator_evidence": creator_summary}
        )
        scored_3 = scorer.evaluate_lead(biz_with_website, verification_status="WEBSITE_EXISTS")
        self.assertEqual(scored_3["qualification_state"], QualificationState.EXCLUDED.value)
        self.assertIn("Excluded", scored_3["qualification_reason"])


if __name__ == "__main__":
    unittest.main()
