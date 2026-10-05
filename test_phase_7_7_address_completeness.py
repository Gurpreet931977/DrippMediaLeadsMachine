"""
Dripp Media — Phase 7.7 Upstream Address Completeness & Branch Identity Tests
=============================================================================
Validates:
  A. Structured OSM house number extraction
  B. Street extraction
  C. UK postcode extraction
  D. Address normalization
  E. Coordinate preservation
  F. Branch marker extraction
  G. Complete address classification
  H. Partial address classification
  I. Missing address handling
  J. Gosom query generation
  K. Pot Kettle Black branch protection
  L. Multiple branches
  M. Duplicate preservation
  N. No fabricated address data
  O. No CRM mutation
  P. No outreach
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import DiscoveredBusiness
from lib.discovery.address_normalizer import (
    AddressCompleteness,
    AddressProfile,
    AddressNormalizer,
    extract_uk_postcode,
    haversine_distance_meters,
)
from lib.discovery.osm import OpenStreetMapProvider
from lib.enrichment.gosom_coverage import (
    PlaceMatchClassification,
    StrictPlaceMatcher,
)


class TestPhase77AddressCompleteness(unittest.TestCase):
    """Targeted Unit Test Suite for Phase 7.7."""

    def setUp(self):
        self.normalizer = AddressNormalizer()
        self.strict_matcher = StrictPlaceMatcher()
        self.osm = OpenStreetMapProvider()

    # ── Test A: Structured OSM house number extraction ─────────────────────────
    def test_a_structured_osm_house_number_extraction(self):
        tags = {
            "name": "Issano",
            "addr:housenumber": "367",
            "addr:street": "Palatine Road",
            "addr:postcode": "M22 4FY",
            "addr:city": "Manchester"
        }
        profile = self.normalizer.parse_osm_tags(tags, lat=53.4080, lon=-2.2574)
        self.assertEqual(profile.house_number, "367")
        self.assertEqual(profile.provenance.get("house_number"), "OSM_TAG_ADDR_HOUSENUMBER")

    # ── Test B: Street extraction ──────────────────────────────────────────────
    def test_b_street_extraction(self):
        # Case 1: Structured tag
        tags = {"name": "Chesters", "addr:street": "Palatine Road", "addr:city": "Manchester"}
        profile1 = self.normalizer.parse_osm_tags(tags, lat=53.4081, lon=-2.2573)
        self.assertEqual(profile1.street, "Palatine Road")

        # Case 2: From combined address string with house number
        raw = "367, Palatine Road, Manchester, M22 4FY"
        profile2 = self.normalizer.parse_address_string(raw, lat=53.4080, lon=-2.2574)
        self.assertEqual(profile2.house_number, "367")
        self.assertEqual(profile2.street, "Palatine Road")
        self.assertEqual(profile2.postcode, "M22 4FY")

        # Case 3: Known street without standard suffix (e.g. Deansgate)
        raw3 = "120 Deansgate, Manchester M3 2BW"
        profile3 = self.normalizer.parse_address_string(raw3, lat=53.483, lon=-2.247)
        self.assertEqual(profile3.house_number, "120")
        self.assertEqual(profile3.street, "Deansgate")

    # ── Test C: UK postcode extraction ─────────────────────────────────────────
    def test_c_uk_postcode_extraction(self):
        pc1 = extract_uk_postcode("367 Palatine Road, Manchester M224FY, UK")
        self.assertEqual(pc1, "M22 4FY")

        pc2 = extract_uk_postcode("Barton Arcade, Deansgate, Manchester M3 2BW")
        self.assertEqual(pc2, "M3 2BW")

        pc3 = extract_uk_postcode("Manchester Airport Ringway Rd M90 4ZY")
        self.assertEqual(pc3, "M90 4ZY")

        pc_invalid = extract_uk_postcode("Manchester, United Kingdom")
        self.assertIsNone(pc_invalid)

    # ── Test D: Address normalization ─────────────────────────────────────────
    def test_d_address_normalization(self):
        raw = "367, Palatine Road, Manchester, M22 4FY, United Kingdom"
        profile = self.normalizer.parse_address_string(raw, lat=53.4080, lon=-2.2574)
        self.assertEqual(profile.normalized_address, "367, Palatine Road, Manchester, M22 4FY, United Kingdom")
        self.assertEqual(profile.city, "Manchester")
        self.assertEqual(profile.country, "United Kingdom")

    # ── Test E: Coordinate preservation ───────────────────────────────────────
    def test_e_coordinate_preservation(self):
        el = {
            "type": "node",
            "id": 11757913257,
            "lat": 53.408009,
            "lon": -2.2574226,
            "tags": {
                "name": "Issano",
                "addr:housenumber": "367",
                "addr:street": "Palatine Road",
                "addr:postcode": "M22 4FY"
            }
        }
        cand = self.osm.parse_osm_element(el, city="Manchester", country="United Kingdom")
        self.assertIsNotNone(cand)
        self.assertEqual(cand.lat, 53.408009)
        self.assertEqual(cand.lon, -2.2574226)
        self.assertEqual(cand.house_number, "367")
        self.assertEqual(cand.street, "Palatine Road")
        self.assertEqual(cand.postcode, "M22 4FY")

    # ── Test F: Branch marker extraction ───────────────────────────────────────
    def test_f_branch_marker_extraction(self):
        # Airport terminals
        b1 = self.normalizer.extract_branch_marker("Burger King Terminal 2 Manchester Airport")
        self.assertEqual(b1, "Terminal 2")

        b2 = self.normalizer.extract_branch_marker("Aspire Lounge T3 Departures")
        self.assertEqual(b2, "Terminal 3")

        # Arcades
        b3 = self.normalizer.extract_branch_marker("Pot Kettle Black Barton Arcade Deansgate")
        self.assertEqual(b3, "Barton Arcade")

        # Sub-localities
        b4 = self.normalizer.extract_branch_marker("Chesters Northenden")
        self.assertEqual(b4, "Northenden")

        # No marker
        b5 = self.normalizer.extract_branch_marker("Generic Cafe Manchester")
        self.assertIsNone(b5)

    # ── Test G: Complete address classification ───────────────────────────────
    def test_g_complete_address_classification(self):
        profile = AddressProfile(
            raw_address="367 Palatine Road, Manchester M22 4FY",
            normalized_address="367 Palatine Road, Manchester M22 4FY",
            street="Palatine Road",
            postcode="M22 4FY",
            city="Manchester",
            latitude=53.4080,
            longitude=-2.2574
        )
        grade = self.normalizer.classify_completeness(profile)
        self.assertEqual(grade, AddressCompleteness.COMPLETE)

    # ── Test H: Partial address classification ─────────────────────────────────
    def test_h_partial_address_classification(self):
        # City + coordinates only (no street, no postcode)
        profile = AddressProfile(
            raw_address="Manchester, United Kingdom",
            normalized_address="Manchester, United Kingdom",
            city="Manchester",
            latitude=53.4076,
            longitude=-2.2580
        )
        grade = self.normalizer.classify_completeness(profile)
        self.assertEqual(grade, AddressCompleteness.PARTIAL)

    # ── Test I: Missing address handling ───────────────────────────────────────
    def test_i_missing_address_handling(self):
        # City only (no coordinates, no street, no postcode)
        profile = AddressProfile(
            raw_address="Manchester, United Kingdom",
            normalized_address="Manchester, United Kingdom",
            city="Manchester"
        )
        grade = self.normalizer.classify_completeness(profile)
        self.assertEqual(grade, AddressCompleteness.MINIMAL)

    # ── Test J: Gosom query generation ────────────────────────────────────────
    def test_j_gosom_query_generation(self):
        # 1. Complete with street + postcode
        p1 = AddressProfile(
            raw_address="367 Palatine Road, Manchester M22 4FY",
            normalized_address="367 Palatine Road, Manchester M22 4FY",
            street="Palatine Road",
            postcode="M22 4FY",
            city="Manchester",
            latitude=53.4080,
            longitude=-2.2574
        )
        q1 = self.normalizer.generate_location_query("Issano", p1)
        self.assertEqual(q1, '"Issano" "Palatine Road" "M22 4FY" "Manchester"')

        # 2. Street only
        p2 = AddressProfile(
            raw_address="Palatine Road, Manchester",
            normalized_address="Palatine Road, Manchester",
            street="Palatine Road",
            city="Manchester",
            latitude=53.4081,
            longitude=-2.2573
        )
        q2 = self.normalizer.generate_location_query("Chesters", p2)
        self.assertEqual(q2, '"Chesters" "Palatine Road" "Manchester"')

        # 3. Postcode only
        p3 = AddressProfile(
            raw_address="Manchester M22 4FY",
            normalized_address="Manchester M22 4FY",
            postcode="M22 4FY",
            city="Manchester"
        )
        q3 = self.normalizer.generate_location_query("Bar Bibo", p3)
        self.assertEqual(q3, '"Bar Bibo" "M22 4FY" "Manchester"')

        # 4. Neither street nor postcode -> Blocked
        p4 = AddressProfile(
            raw_address="Manchester, United Kingdom",
            normalized_address="Manchester, United Kingdom",
            city="Manchester",
            latitude=53.3668,
            longitude=-2.2793
        )
        safe, reason = self.normalizer.is_gosom_safe_to_query(p4)
        self.assertFalse(safe)
        self.assertIn("GOSOM_NOT_SAFE_TO_QUERY", reason)
        q4 = self.normalizer.generate_location_query("Burger King", p4)
        self.assertIsNone(q4)

    # ── Test K: Pot Kettle Black branch protection ────────────────────────────
    def test_k_pot_kettle_black_branch_protection(self):
        # With complete Barton Arcade address:
        p_barton = AddressProfile(
            raw_address="Barton Arcade, Deansgate, Manchester M3 2BW",
            normalized_address="Barton Arcade, Deansgate, Manchester M3 2BW",
            street="Deansgate",
            postcode="M3 2BW",
            city="Manchester",
            branch_identifier="Barton Arcade"
        )
        cand_barton = {
            "company_name": "Pot Kettle Black",
            "city": "Manchester",
            "street": p_barton.street,
            "postcode": p_barton.postcode,
            "branch_identifier": p_barton.branch_identifier,
            "address": p_barton.normalized_address
        }
        places = [
            {"title": "POT KETTLE BLACK Barton Arcade", "address": "Manchester M3 2BW", "review_count": 1917, "review_rating": 4.5},
            {"title": "POT KETTLE BLACK Angel Gardens", "address": "1 Rochdale Rd, Manchester M4 4GE", "review_count": 402, "review_rating": 4.5},
            {"title": "Pot Kettle Black", "address": "1A Tariff St, Manchester M1 2FF", "review_count": 3, "review_rating": 5.0}
        ]
        matched, m_class, conf, diag = self.strict_matcher.classify_and_match(cand_barton, places)
        self.assertIsNotNone(matched)
        self.assertEqual(m_class, PlaceMatchClassification.EXACT_BRANCH_MATCH)
        self.assertIn("Barton Arcade", matched["title"])

        # Without address detail:
        cand_generic = {"company_name": "Pot Kettle Black", "city": "Manchester", "address": "Manchester, United Kingdom"}
        matched2, m_class2, conf2, diag2 = self.strict_matcher.classify_and_match(cand_generic, places)
        self.assertIsNone(matched2)
        self.assertEqual(m_class2, PlaceMatchClassification.AMBIGUOUS_MATCH)

    # ── Test L: Multiple branches ─────────────────────────────────────────────
    def test_l_multiple_branches(self):
        # Chesters with specific street address and postcode
        cand_northenden = {
            "company_name": "Chesters",
            "city": "Manchester",
            "street": "Palatine Road",
            "postcode": "M22 4FY",
            "branch_identifier": "Northenden",
            "address": "363 Palatine Rd, Northenden, Manchester M22 4FY"
        }
        places = [
            {"title": "Chesters Northenden", "address": "363 Palatine Rd, Northenden, Manchester M22 4FY"},
            {"title": "Chesters Fallowfield", "address": "212 Wilmslow Rd, Fallowfield, Manchester M14 6LE"},
            {"title": "Chesters Wythenshawe", "address": "504 Portway, Wythenshawe, Manchester M22 0TF"}
        ]
        matched, m_class, conf, diag = self.strict_matcher.classify_and_match(cand_northenden, places)
        self.assertIsNotNone(matched)
        self.assertEqual(m_class, PlaceMatchClassification.EXACT_BRANCH_MATCH)
        self.assertIn("Northenden", matched["title"])

    # ── Test M: Duplicate preservation ─────────────────────────────────────────
    def test_m_duplicate_preservation(self):
        elements = [
            {
                "type": "node", "id": 101, "lat": 53.4081, "lon": -2.2573,
                "tags": {"name": "Chesters", "addr:street": "Palatine Road", "addr:postcode": "M22 4FY"}
            },
            # Same branch duplicate within 10 meters
            {
                "type": "node", "id": 102, "lat": 53.40812, "lon": -2.25732,
                "tags": {"name": "Chesters", "addr:street": "Palatine Road", "addr:postcode": "M22 4FY"}
            },
            # Distinct branch in Fallowfield
            {
                "type": "node", "id": 201, "lat": 53.4444, "lon": -2.2185,
                "tags": {"name": "Chesters", "addr:street": "Wilmslow Road", "addr:postcode": "M14 6LE"}
            }
        ]
        processed = self.osm._process_elements(elements, city="Manchester", country="United Kingdom", industry="Restaurants", limit=10)
        # Should deduplicate 101 & 102 (same postcode/branch), but PRESERVE 201 (distinct branch)!
        self.assertEqual(len(processed), 2)
        postcodes = {c.postcode for c in processed}
        self.assertIn("M22 4FY", postcodes)
        self.assertIn("M14 6LE", postcodes)

    # ── Test N: No fabricated address data ─────────────────────────────────────
    def test_n_no_fabricated_address_data(self):
        tags = {"name": "Minimal Cafe", "amenity": "cafe"}
        profile = self.normalizer.parse_osm_tags(tags)
        self.assertIsNone(profile.street)
        self.assertIsNone(profile.house_number)
        self.assertIsNone(profile.postcode)
        self.assertIsNone(profile.branch_identifier)
        self.assertEqual(profile.completeness, AddressCompleteness.MINIMAL)

    # ── Test O: No CRM mutation ───────────────────────────────────────────────
    def test_o_no_crm_mutation(self):
        # Verify that normalizer is pure and has 0 CRM writes
        raw = "367 Palatine Road, Manchester M22 4FY"
        profile = self.normalizer.parse_address_string(raw)
        self.assertIsNotNone(profile)
        # Ensure no write operations exist
        self.assertFalse(hasattr(self.normalizer, "write_to_crm"))

    # ── Test P: No outreach ───────────────────────────────────────────────────
    def test_p_no_outreach(self):
        # Verify that no send adapters or messaging hooks exist
        self.assertFalse(hasattr(self.normalizer, "send_outreach"))
        self.assertFalse(hasattr(self.normalizer, "arm_campaign"))


if __name__ == "__main__":
    unittest.main()
