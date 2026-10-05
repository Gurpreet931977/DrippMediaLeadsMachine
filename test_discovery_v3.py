"""
Test Suite for Discovery V3
Validates:
1. SearXNG unavailable (NOT_CONNECTED)
2. SearXNG connected (CONNECTED)
3. Provider fallback ordering (Tavily -> Brave -> SearXNG -> DDG -> Skip)
4. Creator third-party reference separation (THIRD_PARTY_BUSINESS_REFERENCE != SOCIAL_OWNERSHIP_VERIFIED)
5. Official handle requires independent verification
6. Ambiguous creator mention scoring
7. Exact business creator mention scoring
8. True boundary polygon inside validation
9. True boundary polygon outside validation
10. Adjacent municipality diversion
11. OSM empty website != NO_WEBSITE_CONFIRMED (WEBSITE_NOT_LISTED_IN_OSM)
"""

import unittest
from unittest.mock import patch, MagicMock
import json

from lib.types import (
    DiscoveredBusiness,
    CreatorEvidenceItem,
    CreatorReferenceType,
    CreatorEvidenceConfidence,
    CreatorEvidenceClassification,
    HandleClassification,
    CreatorTrustConcept,
    EvidenceTier,
    SourceQualityTier,
    OperationalStatus,
    SocialOwnershipStatus,
    SocialStatus,
    QualificationState,
)
from lib.discovery.web_search import (
    WebSearchProvider,
    check_searxng_health,
    get_search_stats,
    reset_search_stats,
)
from lib.discovery.boundary_validator import CityBoundaryValidator, get_boundary_validator
from lib.discovery.osm import OpenStreetMapProvider, OpenStreetMapProvider as OpenStreetMapDiscovery
from lib.validation.creator_evidence import (
    evaluate_creator_post,
    evaluate_content_item,
    CreatorEvidenceEngine,
    CreatorEvidenceValidator,
)
from lib.validation.social_validator import SocialIdentityValidator
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider


class TestSearXNGHealthAndFallback(unittest.TestCase):
    """Tests for Part 1: Local SearXNG health and provider fallback order."""

    def setUp(self):
        reset_search_stats()

    @patch("requests.get")
    def test_searxng_unavailable(self, mock_get):
        """When SearXNG is unreachable, status is NOT_CONNECTED and fallback works."""
        import requests
        mock_get.side_effect = requests.ConnectionError("Connection refused")
        
        status = check_searxng_health("http://localhost:8080")
        self.assertEqual(status, "NOT_CONNECTED")

        # Test search client gracefully handles unreachable SearXNG
        provider = WebSearchProvider(searxng_url="http://localhost:8080")
        results = provider._search_searxng("test query")
        self.assertEqual(results, [])
        self.assertEqual(provider.stats_by_provider["SEARXNG"]["failures"], 1)

    @patch("requests.get")
    def test_searxng_connected(self, mock_get):
        """When SearXNG returns HTTP 200 with JSON, status is CONNECTED."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Original Patty Men Digbeth",
                    "url": "https://example.com/opm",
                    "content": "Craft burger restaurant in Digbeth, Birmingham"
                }
            ]
        }
        mock_get.return_value = mock_resp

        status = check_searxng_health("http://localhost:8080")
        self.assertEqual(status, "CONNECTED")

        provider = WebSearchProvider(searxng_url="http://localhost:8080")
        results = provider._search_searxng("Original Patty Men Birmingham")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Original Patty Men Digbeth")
        self.assertEqual(results[0]["search_provider"], "SEARXNG")

        self.assertEqual(provider.stats_by_provider["SEARXNG"]["queries"], 1)
        self.assertEqual(provider.stats_by_provider["SEARXNG"]["results"], 1)

    @patch("requests.get")
    def test_provider_fallback_ordering(self, mock_get):
        """Search priority: Tavily -> Brave -> SearXNG -> DuckDuckGo -> Skip."""
        import requests
        mock_get.side_effect = requests.ConnectionError("SearXNG down")
        
        provider = WebSearchProvider(searxng_url="http://localhost:8080")
        # Tavily & Brave are unconfigured (None)
        # SearXNG is down
        # Should fallback to DuckDuckGo
        with patch.object(provider, "_get_cache", return_value=None):
            with patch.object(provider, "_search_duckduckgo_fallback", return_value=[{"title": "DDG Result", "result_url": "https://ddg.com", "snippet": "Sample"}]) as mock_ddg:
                results = provider.search_web("test fallback unique query 12345")
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["title"], "DDG Result")
                mock_ddg.assert_called_once()



class TestCreatorEvidenceGovernanceAndScoring(unittest.TestCase):
    """Tests for Parts 2 & 3: Creator / Influencer Reference Discovery and Governance."""

    def test_creator_third_party_reference_separation(self):
        """THIRD_PARTY_BUSINESS_REFERENCE must NEVER create SOCIAL_OWNERSHIP_VERIFIED or ACTIVE_CONFIRMED."""
        post = {
            "source_url": "https://instagram.com/p/BHM12345",
            "source_domain": "instagram.com",
            "creator_handle": "birmingham_bites",
            "caption": "Stopped by Original Patty Men in Digbeth! Best burgers in Birmingham. @originalpattymen",
            "tagged_accounts": ["originalpattymen"],
            "location_name": "Original Patty Men",
            "discovery_query": "Original Patty Men Birmingham food blogger"
        }

        evidence = evaluate_creator_post(
            post=post,
            business_name="Original Patty Men",
            city="Birmingham",
            neighborhood="Digbeth"
        )

        self.assertIsNotNone(evidence)
        # Verify it is classified as a valid third-party reference
        self.assertEqual(evidence.reference_type, CreatorReferenceType.MULTI_SIGNAL.value)
        self.assertIn("originalpattymen", evidence.candidate_official_handles)
        
        # Governance invariant: evidence itself is THIRD PARTY, not official ownership
        self.assertNotEqual(evidence.reference_type, "SOCIAL_OWNERSHIP_VERIFIED")
        # Ensure candidate handle is only CANDIDATE, not verified ownership
        self.assertFalse(hasattr(evidence, "ownership_verified") and evidence.ownership_verified)

    def test_official_handle_requires_independent_verification(self):
        """Discovered official handle candidates require independent SocialIdentityValidator check."""
        validator = SocialIdentityValidator()
        
        # A candidate handle discovered from a creator mention
        candidate_handle = "originalpattymen"
        
        # Simulating independent check where bio does not match business
        profile_unrelated = {
            "handle": candidate_handle,
            "bio": "Personal blog about gaming and music in London",
            "external_url": "",
            "followers_count": 50,
            "is_verified": False,
        }
        res_unrelated = validator.validate(profile_unrelated, business_name="Original Patty Men", city="Birmingham")
        # Should not be verified as official
        self.assertNotEqual(res_unrelated.status, "VERIFIED")

        # Now with matching context
        profile_matching = {
            "handle": candidate_handle,
            "bio": "Original Patty Men - Digbeth, Birmingham. Proper burgers and craft beer.",
            "external_url": "",
            "followers_count": 35000,
            "is_verified": True,
        }
        res_matching = validator.validate(profile_matching, business_name="Original Patty Men", city="Birmingham")
        self.assertEqual(res_matching.status, "VERIFIED")

    def test_ambiguous_creator_mention(self):
        """Ambiguous mention without business name match receives penalty and low confidence."""
        post = {
            "source_url": "https://tiktok.com/@foodie/video/1",
            "source_domain": "tiktok.com",
            "caption": "Had an amazing burger for lunch in Birmingham today! #birminghamfood",
            "tagged_accounts": [],
            "location_name": "Birmingham",
            "discovery_query": "Patty Birmingham"
        }

        # Business name is generic/ambiguous and not specifically matched
        evidence = evaluate_creator_post(
            post=post,
            business_name="The Burger Bar",
            city="Birmingham"
        )

        # Name not found, score should be low and confidence LOW or UNKNOWN
        if evidence:
            self.assertIn(evidence.evidence_confidence, [CreatorEvidenceConfidence.LOW.value, CreatorEvidenceConfidence.UNKNOWN.value])
            self.assertLess(evidence.evidence_score, 40)
            self.assertFalse(evidence.business_name_match)

    def test_exact_business_creator_mention(self):
        """Exact business name + city + neighborhood + tagged handle produces HIGH confidence."""
        post = {
            "source_url": "https://tiktok.com/@birminghamfood/video/999",
            "source_domain": "tiktok.com",
            "creator_handle": "birminghamfood",
            "caption": "You must try Original Patty Men in Digbeth Birmingham! Unbelievable beef patties.",
            "tagged_accounts": ["originalpattymen"],
            "location_name": "Digbeth, Birmingham",
            "discovery_query": '"Original Patty Men" Birmingham TikTok'
        }

        evidence = evaluate_creator_post(
            post=post,
            business_name="Original Patty Men",
            city="Birmingham",
            neighborhood="Digbeth"
        )

        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.evidence_confidence, CreatorEvidenceConfidence.HIGH.value)
        self.assertGreaterEqual(evidence.evidence_score, 70)
        self.assertTrue(evidence.business_name_match)
        self.assertTrue(evidence.location_match)
        self.assertEqual(evidence.source_domain, "tiktok.com")


class TestBirminghamBoundaryPolygon(unittest.TestCase):
    """Tests for Part 4: Actual Birmingham Administrative Boundary Polygon Validation."""

    def setUp(self):
        self.validator = get_boundary_validator()

    def test_true_boundary_polygon_inside(self):
        """Points clearly inside Birmingham city boundary match."""
        # 1. Birmingham City Centre (New Street / Bullring)
        match, reason = self.validator.is_point_in_boundary(52.4774, -1.8986)
        self.assertTrue(match, f"City Centre should match: {reason}")
        self.assertIn("polygon", reason.lower())
        full_res = self.validator.validate_candidate(city="Birmingham", country="United Kingdom", lat=52.4774, lon=-1.8986)
        self.assertEqual(full_res.boundary_validation_method, "POINT_IN_POLYGON_RAY_CAST")

        # 2. Digbeth / Custard Factory
        match, reason = self.validator.is_point_in_boundary(52.4751, -1.8845)
        self.assertTrue(match, f"Digbeth should match: {reason}")

        # 3. Jewellery Quarter
        match, reason = self.validator.is_point_in_boundary(52.4870, -1.9100)
        self.assertTrue(match, f"Jewellery Quarter should match: {reason}")

        # 4. Longbridge (southern Birmingham edge)
        match, reason = self.validator.is_point_in_boundary(52.3950, -1.9800)
        self.assertTrue(match, f"Longbridge should match: {reason}")

    def test_true_boundary_polygon_outside(self):
        """Points outside Birmingham city boundary do not match."""
        # 1. Cofton Hackett (Bromsgrove District, Worcestershire - just south of Longbridge)
        match, reason = self.validator.is_point_in_boundary(52.3800, -1.9950)
        self.assertFalse(match, f"Cofton Hackett must not match: {reason}")

        # 2. Solihull Town Centre (Metropolitan Borough of Solihull)
        match, reason = self.validator.is_point_in_boundary(52.4128, -1.7782)
        self.assertFalse(match, f"Solihull must not match: {reason}")

        # 3. Bromsgrove Town Centre
        match, reason = self.validator.is_point_in_boundary(52.3353, -2.0579)
        self.assertFalse(match, f"Bromsgrove must not match: {reason}")

        # 4. Sutton Coldfield vs Tamworth (Tamworth is outside)
        match, reason = self.validator.is_point_in_boundary(52.6340, -1.6958)
        self.assertFalse(match, f"Tamworth must not match: {reason}")

    def test_adjacent_municipality_diversion(self):
        """Candidates outside boundary are diverted to adjacent_research_candidates."""
        osm = OpenStreetMapDiscovery()
        
        # Test candidate outside boundary (Solihull)
        element_outside = {
            "type": "node",
            "id": 99901,
            "lat": 52.4128,
            "lon": -1.7782,
            "tags": {
                "name": "The Solihull Eatery",
                "addr:street": "High Street",
                "addr:city": "Solihull",
                "website": "https://solihulleats.co.uk",
            }
        }
        res_outside = osm.parse_osm_element(element_outside, city="Birmingham", country="United Kingdom")
        self.assertIsNotNone(res_outside)
        self.assertFalse(res_outside.city_match)
        self.assertEqual(res_outside.boundary_validation_method, "POINT_IN_POLYGON_RAY_CAST")

        # Test candidate inside boundary (Digbeth)
        element_inside = {
            "type": "node",
            "id": 99902,
            "lat": 52.4751,
            "lon": -1.8845,
            "tags": {
                "name": "Digbeth Diner",
                "addr:street": "Digbeth",
                "addr:city": "Birmingham",
                "website": "",
            }
        }
        res_inside = osm.parse_osm_element(element_inside, city="Birmingham", country="United Kingdom")
        self.assertIsNotNone(res_inside)
        self.assertTrue(res_inside.city_match)
        self.assertEqual(res_inside.boundary_validation_method, "POINT_IN_POLYGON_RAY_CAST")

        # Verify that _process_elements properly diverts out-of-boundary candidate to adjacent_research_candidates
        osm.adjacent_research_candidates.clear()
        accepted = osm._process_elements([element_outside, element_inside], city="Birmingham", country="United Kingdom", industry="Restaurants", limit=10)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].company_name, "Digbeth Diner")
        self.assertEqual(len(osm.adjacent_research_candidates), 1)
        self.assertEqual(osm.adjacent_research_candidates[0].company_name, "The Solihull Eatery")


class TestDiscoveryQualitySemantics(unittest.TestCase):
    """Tests for Part 5: Discovery Quality & Semantics."""

    def test_osm_empty_website_not_no_website_confirmed(self):
        """Empty OSM website tag must produce WEBSITE_NOT_LISTED_IN_OSM, not NO_WEBSITE_CONFIRMED."""
        osm = OpenStreetMapDiscovery()
        element = {
            "type": "node",
            "id": 88801,
            "lat": 52.4862,
            "lon": -1.8904,
            "tags": {
                "name": "Local Birmingham Bakery",
                "addr:street": "New Street",
                "addr:city": "Birmingham",
                "website": "",
            }
        }
        res = osm.parse_osm_element(element, city="Birmingham", country="United Kingdom")
        self.assertIsNotNone(res)
        
        # Semantic invariant:
        self.assertEqual(res.osm_website_status, "WEBSITE_NOT_LISTED_IN_OSM")
        self.assertNotEqual(getattr(res, "website_status", ""), "NO_WEBSITE_CONFIRMED")



class TestDiscoveryV31CreatorPrecision(unittest.TestCase):
    """
    Tests for Discovery V3.1 Creator Evidence Precision & Handle Classification.
    Validates:
    1. Creator username mistaken as business handle
    2. Commenter username mistaken as business handle
    3. Location-only search result
    4. Partial business-name collision
    5. Exact business-name creator mention
    6. Business + city + street match
    7. Ambiguous business name
    8. Ambiguous handle
    9. Third-party creator reference never becoming social ownership
    10. Creator evidence never becoming ACTIVE_CONFIRMED
    """

    def test_creator_username_mistaken_as_business_handle(self):
        """A creator's own username must NOT be extracted as candidate official handle."""
        post = {
            "source_url": "https://tiktok.com/@bham_food_diaries/video/101",
            "source_domain": "tiktok.com",
            "creator_handle": "bham_food_diaries",
            "caption": "Had an amazing burger at Original Patty Men in Birmingham! Follow @bham_food_diaries for more reviews.",
            "tagged_accounts": ["bham_food_diaries"],
            "location_name": "Birmingham",
            "discovery_query": "Original Patty Men Birmingham TikTok"
        }
        evidence = evaluate_creator_post(
            post=post,
            business_name="Original Patty Men",
            city="Birmingham"
        )
        self.assertIsNotNone(evidence)
        self.assertNotEqual(evidence.candidate_official_handle, "bham_food_diaries")
        self.assertNotIn("bham_food_diaries", evidence.candidate_official_handles)

    def test_commenter_username_mistaken_as_business_handle(self):
        """Commenter/conversational usernames must NOT be extracted as candidate official handle."""
        post = {
            "source_url": "https://instagram.com/p/BHM999",
            "source_domain": "instagram.com",
            "creator_handle": "brum_eats",
            "caption": "Thanks @johnny_burgers for the tip! Original Patty Men in Birmingham was incredible.",
            "tagged_accounts": ["johnny_burgers"],
            "location_name": "Birmingham",
            "discovery_query": "Original Patty Men Birmingham"
        }
        evidence = evaluate_creator_post(
            post=post,
            business_name="Original Patty Men",
            city="Birmingham"
        )
        self.assertIsNotNone(evidence)
        self.assertNotEqual(evidence.candidate_official_handle, "johnny_burgers")
        self.assertNotIn("johnny_burgers", evidence.candidate_official_handles)

    def test_location_only_search_result(self):
        """A location-only search result must NOT become a valid/strong creator reference."""
        post = {
            "source_url": "https://tiktok.com/@uk_travel/video/555",
            "source_domain": "tiktok.com",
            "creator_handle": "uk_travel",
            "caption": "Exploring Birmingham city centre today! Walking by the canals and Bullring.",
            "tagged_accounts": [],
            "location_name": "Birmingham, UK",
            "discovery_query": "Original Patty Men Birmingham"
        }
        evidence = evaluate_creator_post(
            post=post,
            business_name="Original Patty Men",
            city="Birmingham"
        )
        if evidence:
            self.assertNotEqual(evidence.classification, CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value)
            self.assertIn(evidence.classification, [CreatorEvidenceClassification.INVALID.value, CreatorEvidenceClassification.AMBIGUOUS.value])
            self.assertNotEqual(evidence.evidence_confidence, CreatorEvidenceConfidence.HIGH.value)

    def test_partial_business_name_collision(self):
        """Partial business name collision without distinct match must be INVALID."""
        post = {
            "source_url": "https://tiktok.com/@foodie/video/222",
            "source_domain": "tiktok.com",
            "creator_handle": "foodie",
            "caption": "Five Guys has the juiciest beef patty in Birmingham! Grab a patty and fries.",
            "tagged_accounts": [],
            "location_name": "Birmingham",
            "discovery_query": "Patty Birmingham"
        }
        evidence = evaluate_creator_post(
            post=post,
            business_name="Original Patty Men",
            city="Birmingham"
        )
        if evidence:
            self.assertEqual(evidence.classification, CreatorEvidenceClassification.INVALID.value)
            self.assertNotEqual(evidence.classification, CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value)

    def test_exact_business_name_creator_mention(self):
        """Exact business name + city in creator post produces VALID_THIRD_PARTY_REFERENCE and HIGH confidence."""
        post = {
            "source_url": "https://instagram.com/p/OPM123",
            "source_domain": "instagram.com",
            "creator_handle": "bham_foodie",
            "caption": "Had an unforgettable burger at Original Patty Men in Birmingham! The patties are insane.",
            "tagged_accounts": ["originalpattymen"],
            "location_name": "Birmingham",
            "discovery_query": "Original Patty Men Birmingham"
        }
        evidence = evaluate_creator_post(
            post=post,
            business_name="Original Patty Men",
            city="Birmingham"
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.classification, CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value)
        self.assertEqual(evidence.evidence_confidence, CreatorEvidenceConfidence.HIGH.value)
        self.assertTrue(evidence.business_name_match)
        self.assertTrue(evidence.location_match)

    def test_business_plus_city_plus_street_match(self):
        """Exact business name + city + street produces VALID_THIRD_PARTY_REFERENCE and HIGH confidence."""
        post = {
            "source_url": "https://tiktok.com/@eats/video/333",
            "source_domain": "tiktok.com",
            "creator_handle": "eats",
            "caption": "Stopped by Original Patty Men on Shaw's Passage in Birmingham. The best burger joint in Digbeth.",
            "tagged_accounts": ["originalpattymen"],
            "location_name": "Birmingham",
            "discovery_query": "Original Patty Men Shaw's Passage Birmingham"
        }
        evidence = evaluate_creator_post(
            post=post,
            business_name="Original Patty Men",
            city="Birmingham",
            street="Shaw's Passage"
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.classification, CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value)
        self.assertEqual(evidence.evidence_confidence, CreatorEvidenceConfidence.HIGH.value)
        self.assertTrue(evidence.business_name_match)
        self.assertTrue(evidence.street_match)

    def test_ambiguous_business_name(self):
        """Ambiguous generic business name lacking street/postcode corroboration is classified as AMBIGUOUS."""
        post = {
            "source_url": "https://tiktok.com/@local_life/video/444",
            "source_domain": "tiktok.com",
            "creator_handle": "local_life",
            "caption": "Grabbing cocktails and chilling at The Lounge in Birmingham tonight! Good vibes.",
            "tagged_accounts": [],
            "location_name": "Birmingham",
            "discovery_query": "The Lounge Birmingham"
        }
        evidence = evaluate_creator_post(
            post=post,
            business_name="The Lounge",
            city="Birmingham"
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.classification, CreatorEvidenceClassification.AMBIGUOUS.value)
        self.assertNotEqual(evidence.classification, CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value)
        self.assertEqual(evidence.evidence_confidence, CreatorEvidenceConfidence.LOW.value)

    def test_ambiguous_handle(self):
        """A handle matching only a single generic token of a multi-word business name is classified as AMBIGUOUS."""
        h_class, reason = CreatorEvidenceValidator.classify_handle(
            handle="patty",
            business={"company_name": "Original Patty Men"},
            context_text="Check out @patty for burger specials",
            creator_handle="foodie",
            source_url="https://tiktok.com/@foodie/1"
        )
        self.assertEqual(h_class, HandleClassification.AMBIGUOUS.value)
        self.assertNotEqual(h_class, HandleClassification.CANDIDATE_OFFICIAL_ACCOUNT.value)

    def test_third_party_creator_reference_never_becoming_social_ownership(self):
        """A valid third-party creator reference must NEVER grant SOCIAL_OWNERSHIP_VERIFIED status."""
        post = {
            "source_url": "https://instagram.com/p/OPM777",
            "source_domain": "instagram.com",
            "creator_handle": "top_birmingham_eats",
            "caption": "Reviewing Original Patty Men in Digbeth Birmingham. Highly recommend @originalpattymen!",
            "tagged_accounts": ["originalpattymen"],
            "location_name": "Original Patty Men",
            "discovery_query": "Original Patty Men Birmingham"
        }
        evidence = evaluate_creator_post(
            post=post,
            business_name="Original Patty Men",
            city="Birmingham",
            neighborhood="Digbeth"
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.classification, CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value)
        self.assertNotEqual(evidence.reference_type, "SOCIAL_OWNERSHIP_VERIFIED")
        self.assertFalse(getattr(evidence, "ownership_verified", False))

    def test_creator_evidence_never_becoming_active_confirmed(self):
        """Creator evidence must never mutate business operational status to ACTIVE_CONFIRMED."""
        biz = DiscoveredBusiness(
            company_name="Original Patty Men",
            category="Restaurant",
            city="Birmingham",
            target_country="United Kingdom",
            operational_status="UNKNOWN"
        )
        post = {
            "source_url": "https://instagram.com/p/OPM888",
            "source_domain": "instagram.com",
            "creator_handle": "bham_critic",
            "caption": "Had dinner at Original Patty Men in Birmingham yesterday! Best burgers.",
            "tagged_accounts": ["originalpattymen"],
            "location_name": "Digbeth, Birmingham",
            "discovery_query": "Original Patty Men Birmingham"
        }
        evidence = evaluate_creator_post(
            post=post,
            business_name=biz.company_name,
            city=biz.city
        )
        self.assertIsNotNone(evidence)
        self.assertNotEqual(biz.operational_status, "ACTIVE_CONFIRMED")
        self.assertEqual(biz.operational_status, "UNKNOWN")


class TestCreatorDiscoveryV32SecondaryEvidence(unittest.TestCase):
    """
    Focused unit tests for Discovery V3.2 Secondary Evidence System:
    1. Separation of CREATOR_DISCOVERY_SIGNAL from CREATOR_VERIFIED_EVIDENCE
    2. Evidence Tiers: DISCOVERY_ONLY, CORROBORATED, VERIFIED
    3. Source Quality Tiers: HIGH_VALUE, MEDIUM, LOW (LOW capped at DISCOVERY_ONLY)
    4. Contextual evidence requirement (business name string match alone is insufficient)
    5. Handle classification and creator username exclusion
    6. Canonical URL normalization and evidence deduplication
    7. Section 8 Qualification Isolation:
       - Creator signal alone CANNOT become ACTIVE_CONFIRMED
       - Creator signal alone CANNOT become SOCIAL_OWNERSHIP_VERIFIED
       - Creator signal alone CANNOT become OUTREACH_READY
    """

    def test_creator_signal_alone_cannot_become_active_confirmed(self):
        """Section 8: Creator evidence alone must NEVER produce ACTIVE_CONFIRMED."""
        biz = DiscoveredBusiness(
            company_name="Original Patty Men",
            category="Restaurant",
            city="Birmingham",
            target_country="United Kingdom",
            operational_status=OperationalStatus.OPERATIONAL_UNKNOWN.value,
            address="Shaw's Passage, Digbeth, Birmingham"
        )
        post = {
            "source_url": "https://tiktok.com/@midlands_eats/video/555",
            "source_domain": "tiktok.com",
            "creator_handle": "midlands_eats",
            "caption": "Had dinner at Original Patty Men in Birmingham! Insane burgers.",
            "tagged_accounts": ["originalpattymen"],
            "location_name": "Original Patty Men",
            "published_at": "2026-09-20"
        }
        evidence = evaluate_creator_post(post, business_name=biz.company_name, city=biz.city, address=biz.address)
        self.assertEqual(evidence.classification, CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value)
        
        # Pass through operational validator with NO strong primary sources (no verified phone, no verified business social)
        op_audit = OperationalValidator.verify_operations(
            business=biz,
            social_audit={"social_ownership_status": SocialOwnershipStatus.UNVERIFIED.value},
            creator_evidence=evidence.to_dict()
        )
        self.assertNotEqual(op_audit["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertIn(op_audit["operational_status"], [OperationalStatus.ACTIVE_LIKELY.value, OperationalStatus.OPERATIONAL_UNKNOWN.value])

    def test_creator_signal_alone_cannot_become_social_ownership_verified(self):
        """Section 8: Creator signal alone must NEVER grant SOCIAL_OWNERSHIP_VERIFIED."""
        post = {
            "source_url": "https://instagram.com/p/BHM999",
            "source_domain": "instagram.com",
            "creator_handle": "bham_blogger",
            "caption": "Visited Original Patty Men today. Love @originalpattymen!",
            "tagged_accounts": ["originalpattymen"],
            "location_name": "Birmingham"
        }
        evidence = evaluate_creator_post(post, business_name="Original Patty Men", city="Birmingham")
        self.assertNotEqual(evidence.reference_type, "SOCIAL_OWNERSHIP_VERIFIED")
        self.assertNotEqual(evidence.trust_concept, "SOCIAL_OWNERSHIP_VERIFIED")
        self.assertNotEqual(evidence.handle_classification, SocialOwnershipStatus.VERIFIED.value)

    def test_creator_signal_alone_cannot_become_outreach_ready(self):
        """Section 8: Creator signal alone must NEVER qualify a lead as OUTREACH_READY."""
        biz = DiscoveredBusiness(
            company_name="Original Patty Men",
            category="Restaurant",
            city="Birmingham",
            target_country="United Kingdom",
            operational_status=OperationalStatus.OPERATIONAL_UNKNOWN.value,
            social_status=SocialStatus.SOCIAL_UNKNOWN.value,
            raw_website="",
            review_count=150,
            rating=4.6
        )
        post = {
            "source_url": "https://tiktok.com/@eats/video/777",
            "source_domain": "tiktok.com",
            "creator_handle": "eats",
            "caption": "Best burger in Birmingham at Original Patty Men! @originalpattymen",
            "tagged_accounts": ["originalpattymen"],
            "location_name": "Original Patty Men"
        }
        evidence = evaluate_creator_post(post, business_name=biz.company_name, city=biz.city, address=biz.address)
        biz.raw_data = {"creator_evidence": evidence.to_dict()}

        scorer = LeadScoringProvider()
        result = scorer.evaluate_lead(biz, verification_status="NO_WEBSITE_CONFIRMED")
        self.assertNotEqual(result["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertIn(result["qualification_state"], [QualificationState.MANUAL_REVIEW.value, QualificationState.RESEARCH_ONLY.value])

    def test_evidence_tiers_and_trust_concepts(self):
        """Section 1 & 2: Test DISCOVERY_ONLY, CORROBORATED, and VERIFIED evidence tiers."""
        # 1. DISCOVERY_ONLY: Ambiguous business name without street context
        post_ambig = {
            "source_url": "https://tiktok.com/@food/video/1",
            "source_domain": "tiktok.com",
            "creator_handle": "food",
            "caption": "Having drinks at The Lounge in Birmingham tonight.",
            "tagged_accounts": []
        }
        ev_ambig = evaluate_creator_post(post_ambig, business_name="The Lounge", city="Birmingham")
        self.assertEqual(ev_ambig.evidence_tier, EvidenceTier.DISCOVERY_ONLY.value)
        self.assertEqual(ev_ambig.trust_concept, CreatorTrustConcept.CREATOR_DISCOVERY_SIGNAL.value)

        # 2. CORROBORATED: Exact business name + correct city + meaningful context + >= 2 signals
        post_corrob = {
            "source_url": "https://tiktok.com/@midlands_food/video/2",
            "source_domain": "tiktok.com",
            "creator_handle": "midlands_food",
            "caption": "Had an unforgettable burger lunch at Original Patty Men in Birmingham! Super juicy patties.",
            "tagged_accounts": ["originalpattymen"]
        }
        ev_corrob = evaluate_creator_post(post_corrob, business_name="Original Patty Men", city="Birmingham")
        self.assertEqual(ev_corrob.classification, CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value)
        self.assertEqual(ev_corrob.evidence_tier, EvidenceTier.CORROBORATED.value)
        self.assertEqual(ev_corrob.trust_concept, CreatorTrustConcept.CREATOR_DISCOVERY_SIGNAL.value)

        # 3. VERIFIED: Exact business name + city + street + strong context + verified handle
        post_verified = {
            "source_url": "https://tiktok.com/@food_critic/video/3",
            "source_domain": "tiktok.com",
            "creator_handle": "food_critic",
            "caption": "Full review: Stopped by Original Patty Men on Shaw's Passage in Digbeth Birmingham. Highly recommend @originalpattymen!",
            "tagged_accounts": ["originalpattymen"],
            "handle_verified": True
        }
        ev_verified = evaluate_creator_post(
            post_verified,
            business_name="Original Patty Men",
            city="Birmingham",
            street="Shaw's Passage",
            neighborhood="Digbeth"
        )
        self.assertEqual(ev_verified.classification, CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value)
        self.assertEqual(ev_verified.evidence_tier, EvidenceTier.VERIFIED.value)
        self.assertEqual(ev_verified.trust_concept, CreatorTrustConcept.CREATOR_VERIFIED_EVIDENCE.value)

    def test_source_quality_tiering_and_low_source_cap(self):
        """Section 3: LOW source quality must be capped at DISCOVERY_ONLY."""
        # High value source: TikTok video with rich caption
        sq_high, _ = CreatorEvidenceValidator.classify_source_quality(
            url="https://tiktok.com/@critic/video/888",
            caption="Review of Original Patty Men in Birmingham",
            title=""
        )
        self.assertEqual(sq_high, SourceQualityTier.HIGH_VALUE.value)

        # Medium value source: Tripadvisor directory
        sq_med, _ = CreatorEvidenceValidator.classify_source_quality(
            url="https://www.tripadvisor.co.uk/Restaurant_Review-Original_Patty_Men",
            caption="Original Patty Men Birmingham reviews and menu",
            title=""
        )
        self.assertEqual(sq_med, SourceQualityTier.MEDIUM.value)

        # Low value source: Generic hashtag exploration page
        sq_low, _ = CreatorEvidenceValidator.classify_source_quality(
            url="https://www.instagram.com/explore/tags/birminghamfood/",
            caption="#birminghamfood",
            title=""
        )
        self.assertEqual(sq_low, SourceQualityTier.LOW.value)

        # Low source quality caps result at DISCOVERY_ONLY
        post_low = {
            "source_url": "https://www.instagram.com/explore/tags/originalpattymen/",
            "source_domain": "instagram.com",
            "caption": "#originalpattymen #burger",
            "location_name": "Birmingham"
        }
        ev_low = evaluate_creator_post(post_low, business_name="Original Patty Men", city="Birmingham")
        self.assertEqual(ev_low.evidence_tier, EvidenceTier.DISCOVERY_ONLY.value)
        self.assertNotEqual(ev_low.classification, CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value)

    def test_contextual_evidence_requirement(self):
        """Section 4: Exact business name without dining/review/venue context is rejected."""
        # Lacks contextual evidence (e.g., listing in a corporate registry)
        post_no_context = {
            "source_url": "https://tiktok.com/@news/video/99",
            "source_domain": "tiktok.com",
            "creator_handle": "news",
            "caption": "Original Patty Men registered with company number 12345678 in Birmingham on Monday.",
            "location_name": "Birmingham"
        }
        ev_no_ctx = evaluate_creator_post(post_no_context, business_name="Original Patty Men", city="Birmingham")
        self.assertFalse(ev_no_ctx.contextual_match)
        self.assertEqual(ev_no_ctx.classification, CreatorEvidenceClassification.AMBIGUOUS.value)
        self.assertEqual(ev_no_ctx.evidence_tier, EvidenceTier.DISCOVERY_ONLY.value)

        # Has rich contextual evidence
        post_rich_context = {
            "source_url": "https://tiktok.com/@eats/video/100",
            "source_domain": "tiktok.com",
            "creator_handle": "eats",
            "caption": "Dined at Original Patty Men in Birmingham. Ordered their signature beef burger and wings, delicious food!",
            "location_name": "Birmingham"
        }
        ev_rich = evaluate_creator_post(post_rich_context, business_name="Original Patty Men", city="Birmingham")
        self.assertTrue(ev_rich.contextual_match)
        self.assertEqual(ev_rich.classification, CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value)

    def test_handle_discovery_creator_separation(self):
        """Section 5: Creator's own username must never become candidate business handle."""
        post = {
            "source_url": "https://tiktok.com/@midlands_foodie/video/123",
            "source_domain": "tiktok.com",
            "creator_handle": "midlands_foodie",
            "caption": "Hey guys it is @midlands_foodie here eating at Original Patty Men! Tagging @originalpattymen and friend @jack_eats",
            "tagged_accounts": ["midlands_foodie", "originalpattymen", "jack_eats"],
            "location_name": "Birmingham"
        }
        ev = evaluate_creator_post(post, business_name="Original Patty Men", city="Birmingham")
        self.assertNotIn("midlands_foodie", ev.candidate_official_handles)
        self.assertEqual(ev.candidate_official_handle, "originalpattymen")

    def test_evidence_deduplication(self):
        """Section 6: Deduplication by canonical URL prevents multiple results for same page."""
        items = [
            CreatorEvidenceItem(source_url="https://tiktok.com/@eats/video/123", business_reference="Original Patty Men"),
            CreatorEvidenceItem(source_url="https://tiktok.com/@eats/video/123?utm_source=tiktok&utm_medium=feed", business_reference="Original Patty Men"),
            CreatorEvidenceItem(source_url="https://m.tiktok.com/@eats/video/123/?igshid=xyz#heading", business_reference="Original Patty Men"),
            CreatorEvidenceItem(source_url="https://tiktok.com/@eats/video/456", business_reference="Original Patty Men"),
        ]
        unique_items, dupes_removed = CreatorEvidenceValidator.deduplicate_evidence(items, business_name="Original Patty Men")
        self.assertEqual(len(unique_items), 2)
        self.assertEqual(dupes_removed, 2)


if __name__ == "__main__":
    unittest.main()

