#!/usr/bin/env python3
"""
Unit Test Suite for Search-Based Social Discovery (Phase 3)
===========================================================
Tests requirements from Sections 4, 5, 6:
  - Targeted query generation
  - CANDIDATE_OFFICIAL_ACCOUNT classification
  - Promotion to VERIFIED_BUSINESS_ACCOUNT solely via SocialIdentityValidator
  - Location safety: conflicting city rejection (e.g. Birmingham for Leeds)
  - Legitimate suburb and administrative area support
  - AMBIGUOUS / INVALID classification for mismatched profiles
"""

import unittest
from unittest.mock import MagicMock

from lib.types import (
    DiscoveredBusiness,
    HandleClassification,
    SocialOwnershipStatus
)
from lib.discovery.web_search import SearchResultList, SearchOutcome
from lib.discovery.social_discovery import (
    SocialProfileDiscoverer,
    DiscoveredSocialCandidate
)
from lib.validation.social_validator import SocialIdentityValidator


class TestSocialDiscovery(unittest.TestCase):

    def setUp(self):
        self.mock_web = MagicMock()
        self.validator = SocialIdentityValidator()
        self.discoverer = SocialProfileDiscoverer(
            web_search_provider=self.mock_web,
            validator=self.validator
        )

    # 1. Query Generation
    def test_01_query_generation_produces_targeted_queries(self):
        queries = SocialProfileDiscoverer.generate_social_queries("Town Hall Tavern", "Leeds")
        self.assertTrue(any('facebook OR instagram' in q for q in queries))
        self.assertTrue(any('facebook' in q for q in queries))
        self.assertTrue(any('instagram' in q for q in queries))
        self.assertTrue(all('Town Hall Tavern' in q for q in queries))

    # 2. Location Safety: rejects conflicting cities
    def test_02_location_safety_rejects_conflicting_city(self):
        # Snippet mentions Birmingham when business is in Leeds
        snippet = "Original Patty Men, Birmingham - Best Burgers in the West Midlands"
        safe, reason = self.discoverer.check_location_safety(
            text=snippet,
            target_city="Leeds",
            business_name="Original Patty Men"
        )
        self.assertFalse(safe)
        self.assertIn("Location conflict", reason)
        self.assertIn("birmingham", reason.lower())

    # 3. Location Safety: accepts target city and legitimate local areas
    def test_03_location_safety_accepts_target_city_and_suburbs(self):
        cases = [
            ("Town Hall Tavern, Leeds city centre pub with craft beer", "Leeds"),
            ("A Casa Di Alessia, Otley Italian restaurant in West Yorkshire", "Otley"),
            ("WinneBagel Cafe in Pudsey serving artisan bagels", "Pudsey"),
            ("Local bistro in Yeadon near Leeds", "Yeadon")
        ]
        for text, city in cases:
            safe, reason = self.discoverer.check_location_safety(
                text=text,
                target_city=city,
                business_name="Test Business"
            )
            self.assertTrue(safe, f"Expected {text} to be safe for city {city}, got reason: {reason}")

    # 4. Search produces CANDIDATE_OFFICIAL_ACCOUNT
    def test_04_search_produces_candidate_official_account(self):
        self.mock_web.search_web.return_value = SearchResultList([
            {
                "result_url": "https://www.facebook.com/townhalltavern/",
                "title": "Town Hall Tavern - Leeds Pub",
                "snippet": "Welcome to Town Hall Tavern in Leeds."
            }
        ], outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS, provider="SEARXNG")

        candidates = self.discoverer.discover_candidates_for_business("Town Hall Tavern", "Leeds")
        self.assertIn("facebook", candidates)
        cand = candidates["facebook"]
        self.assertEqual(cand.classification, HandleClassification.CANDIDATE_OFFICIAL_ACCOUNT.value)
        self.assertEqual(cand.handle, "townhalltavern")
        self.assertEqual(cand.platform, "facebook")

    # 5. Promotion to VERIFIED_BUSINESS_ACCOUNT solely via SocialIdentityValidator
    def test_05_validator_promotes_matching_candidate(self):
        self.mock_web.search_web.return_value = SearchResultList([
            {
                "result_url": "https://www.facebook.com/townhalltavern/",
                "title": "Town Hall Tavern - Leeds Pub",
                "snippet": "Town Hall Tavern, Leeds City Centre."
            }
        ], outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS, provider="SEARXNG")

        biz = DiscoveredBusiness(
            company_name="Town Hall Tavern",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom"
        )

        biz, summary = self.discoverer.enrich_and_verify(biz)
        self.assertEqual(summary["classifications"].get("facebook"), "VERIFIED_BUSINESS_ACCOUNT")
        self.assertEqual(biz.social_ownership_status, SocialOwnershipStatus.VERIFIED.value)
        self.assertEqual(biz.facebook_url, "https://www.facebook.com/townhalltavern/")

    # 6. Unmatched candidate classified as AMBIGUOUS, not verified
    def test_06_unmatched_candidate_classified_as_ambiguous(self):
        # Returns a completely different business name
        self.mock_web.search_web.return_value = SearchResultList([
            {
                "result_url": "https://www.facebook.com/completelyunrelatedbar/",
                "title": "Unrelated Bar - Leeds",
                "snippet": "Great drinks in Leeds."
            }
        ], outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS, provider="SEARXNG")

        biz = DiscoveredBusiness(
            company_name="Town Hall Tavern",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom"
        )

        biz, summary = self.discoverer.enrich_and_verify(biz)
        self.assertEqual(summary["classifications"].get("facebook"), "AMBIGUOUS")
        self.assertNotEqual(biz.social_ownership_status, SocialOwnershipStatus.VERIFIED.value)
        self.assertEqual(biz.facebook_url, "")


if __name__ == "__main__":
    unittest.main()
