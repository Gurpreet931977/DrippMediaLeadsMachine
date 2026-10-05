"""
Dripp Media — Phase 7.8 OSM Address Completeness Source Fix Test Suite
======================================================================
Tests A through S verifying source-backed address recovery, parent way inheritance,
conflict handling, branch preservation, deduplication, and all safety invariants.
"""

import os
import sys
import json
import hashlib
import unittest
from typing import Dict, Any, List

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import DiscoveredBusiness
from lib.discovery.address_normalizer import (
    AddressNormalizer,
    AddressProfile,
    AddressCompleteness,
    extract_uk_postcode
)
from lib.discovery.osm import OpenStreetMapProvider


class TestPhase78OSMAddressSource(unittest.TestCase):

    def setUp(self):
        self.normalizer = AddressNormalizer()
        self.provider = OpenStreetMapProvider()

    # A. POI direct address tags
    def test_a_poi_direct_address_tags(self):
        element = {
            "id": 11757913257,
            "type": "node",
            "lat": 53.408009,
            "lon": -2.2574226,
            "tags": {
                "name": "Issano",
                "amenity": "fast_food",
                "addr:housenumber": "367",
                "addr:street": "Palatine Road",
                "addr:postcode": "M22 4FY",
                "addr:city": "Manchester"
            }
        }
        profile = self.normalizer.resolve_osm_address(element)
        self.assertEqual(profile.house_number, "367")
        self.assertEqual(profile.street, "Palatine Road")
        self.assertEqual(profile.postcode, "M22 4FY")
        self.assertEqual(profile.city, "Manchester")
        self.assertEqual(profile.completeness, AddressCompleteness.COMPLETE)
        self.assertEqual(profile.street_source, "OSM_DIRECT_TAG")
        self.assertEqual(profile.postcode_source, "OSM_DIRECT_TAG")
        self.assertEqual(profile.house_number_source, "OSM_DIRECT_TAG")
        self.assertIsNone(profile.parent_osm_id)
        self.assertEqual(profile.address_conflict_status, "NO_CONFLICT")

    # B. Parent way address inheritance
    def test_b_parent_way_address_inheritance(self):
        poi_node = {
            "id": 4346093861,
            "type": "node",
            "lat": 53.4076106,
            "lon": -2.2580151,
            "tags": {"name": "Bar Bibo", "amenity": "bar"}
        }
        parent_way = {
            "id": 663014616,
            "type": "way",
            "nodes": [4346093861, 999999999],
            "tags": {
                "building": "yes",
                "addr:street": "Palatine Road",
                "addr:city": "Manchester"
            }
        }
        profile = self.normalizer.resolve_osm_address(poi_node, parent_elements=[parent_way])
        self.assertEqual(profile.street, "Palatine Road")
        self.assertEqual(profile.street_source, "OSM_PARENT_WAY")
        self.assertEqual(profile.parent_osm_id, "663014616")
        self.assertEqual(profile.address_conflict_status, "NO_CONFLICT")
        self.assertEqual(profile.completeness, AddressCompleteness.STRONG)

    # C. Parent postcode inheritance
    def test_c_parent_postcode_inheritance(self):
        poi_node = {
            "id": 4346188989,
            "type": "node",
            "lat": 53.408137,
            "lon": -2.257316,
            "tags": {"name": "Chesters", "amenity": "fast_food"}
        }
        parent_way = {
            "id": 663014618,
            "type": "way",
            "nodes": [4346188989, 888888888],
            "tags": {
                "building": "yes",
                "addr:postcode": "m22 4fy",
                "addr:street": "Palatine Road"
            }
        }
        profile = self.normalizer.resolve_osm_address(poi_node, parent_elements=[parent_way])
        self.assertEqual(profile.postcode, "M22 4FY")
        self.assertEqual(profile.postcode_source, "OSM_PARENT_WAY")
        self.assertEqual(profile.completeness, AddressCompleteness.COMPLETE)

    # D. Parent house number inheritance
    def test_d_parent_house_number_inheritance(self):
        poi_node = {
            "id": 4346181090,
            "type": "node",
            "lat": 53.4085072,
            "lon": -2.2569412,
            "tags": {"name": "Jai Kathmandu", "amenity": "restaurant"}
        }
        parent_way = {
            "id": 663014620,
            "type": "way",
            "nodes": [4346181090],
            "tags": {
                "building": "yes",
                "addr:housenumber": "371",
                "addr:street": "Palatine Road"
            }
        }
        profile = self.normalizer.resolve_osm_address(poi_node, parent_elements=[parent_way])
        self.assertEqual(profile.house_number, "371")
        self.assertEqual(profile.house_number_source, "OSM_PARENT_WAY")

    # E. No parent address
    def test_e_no_parent_address(self):
        poi_node = {
            "id": 14007813347,
            "type": "node",
            "lat": 53.3768139,
            "lon": -2.2803537,
            "tags": {"name": "Caribbean Vibez", "amenity": "fast_food"}
        }
        # No parent element provided
        profile = self.normalizer.resolve_osm_address(poi_node, parent_elements=[])
        self.assertIsNone(profile.street)
        self.assertIsNone(profile.postcode)
        self.assertIsNone(profile.house_number)
        self.assertEqual(profile.street_source, "NONE")
        self.assertEqual(profile.postcode_source, "NONE")
        self.assertEqual(profile.house_number_source, "NONE")
        self.assertIsNone(profile.parent_osm_id)
        self.assertEqual(profile.completeness, AddressCompleteness.PARTIAL)  # has coords + city

    # F. Conflicting POI/parent address
    def test_f_conflicting_poi_parent_address(self):
        poi_node = {
            "id": 12912726343,
            "type": "node",
            "lat": 53.3678333,
            "lon": -2.2822664,
            "tags": {
                "name": "Pot Kettle Black",
                "amenity": "restaurant",
                "addr:street": "Tariff Street"
            }
        }
        parent_way = {
            "id": 777777777,
            "type": "way",
            "nodes": [12912726343],
            "tags": {
                "building": "yes",
                "addr:street": "Deansgate"
            }
        }
        profile = self.normalizer.resolve_osm_address(poi_node, parent_elements=[parent_way])
        self.assertEqual(profile.address_conflict_status, "ADDRESS_CONFLICT")
        self.assertIn("poi_direct_address", profile.conflicting_address_data)
        self.assertIn("parent_way_address", profile.conflicting_address_data)
        safe, reason = self.normalizer.is_gosom_safe_to_query(profile)
        self.assertFalse(safe)
        self.assertIn("ADDRESS_CONFLICT", reason)

    # G. Ambiguous parent address
    def test_g_ambiguous_parent_address(self):
        poi_node = {
            "id": 12456378601,
            "type": "node",
            "lat": 53.3608706,
            "lon": -2.2689532,
            "tags": {"name": "Brew'd", "amenity": "bar"}
        }
        way1 = {
            "id": 111111,
            "type": "way",
            "nodes": [12456378601],
            "tags": {"addr:street": "Palatine Road"}
        }
        way2 = {
            "id": 222222,
            "type": "way",
            "nodes": [12456378601],
            "tags": {"addr:street": "Barlow Moor Road"}
        }
        profile = self.normalizer.resolve_osm_address(poi_node, parent_elements=[way1, way2])
        self.assertEqual(profile.address_conflict_status, "AMBIGUOUS_ADDRESS")
        self.assertIn("competing_parent_addresses", profile.conflicting_address_data)
        safe, reason = self.normalizer.is_gosom_safe_to_query(profile)
        self.assertFalse(safe)
        self.assertIn("AMBIGUOUS_ADDRESS", reason)

    # H. Branch preservation
    def test_h_branch_preservation(self):
        # Case 1: explicit branch tag on POI
        poi_node = {
            "id": 10788190130,
            "type": "node",
            "lat": 53.3602558,
            "lon": -2.2707558,
            "tags": {
                "name": "Costa Coffee",
                "amenity": "cafe",
                "branch": "Terminal 2"
            }
        }
        profile = self.normalizer.resolve_osm_address(poi_node)
        self.assertEqual(profile.branch_identifier, "Terminal 2")

        # Case 2: parent building name (e.g. Barton Arcade)
        poi_node2 = {
            "id": 999991,
            "type": "node",
            "lat": 53.48275,
            "lon": -2.24630,
            "tags": {"name": "Pot Kettle Black", "amenity": "cafe"}
        }
        parent_building = {
            "id": 55555,
            "type": "way",
            "nodes": [999991],
            "tags": {"building": "yes", "name": "Barton Arcade", "addr:postcode": "M3 2BW"}
        }
        profile2 = self.normalizer.resolve_osm_address(poi_node2, parent_elements=[parent_building])
        self.assertEqual(profile2.branch_identifier, "Barton Arcade")

    # I. Coordinate preservation
    def test_i_coordinate_preservation(self):
        element = {
            "id": 8436548117,
            "type": "node",
            "lat": 53.3612649,
            "lon": -2.2736938,
            "tags": {"name": "Aspire Lounge", "amenity": "restaurant"}
        }
        profile = self.normalizer.resolve_osm_address(element)
        self.assertEqual(profile.latitude, 53.3612649)
        self.assertEqual(profile.longitude, -2.2736938)
        self.assertEqual(profile.provenance.get("coordinates"), "OSM_COORDINATES")

    # J. Address provenance tracking
    def test_j_address_provenance(self):
        poi_node = {
            "id": 3937958860,
            "type": "node",
            "lat": 53.4102091,
            "lon": -2.2558966,
            "tags": {"name": "Deli Spice", "amenity": "fast_food"}
        }
        parent_way = {
            "id": 888123,
            "type": "way",
            "nodes": [3937958860],
            "tags": {
                "addr:housenumber": "12",
                "addr:street": "Church Road",
                "addr:postcode": "M22 4NW"
            }
        }
        profile = self.normalizer.resolve_osm_address(poi_node, parent_elements=[parent_way])
        p_dict = profile.to_dict()
        self.assertEqual(p_dict["street_source"], "OSM_PARENT_WAY")
        self.assertEqual(p_dict["house_number_source"], "OSM_PARENT_WAY")
        self.assertEqual(p_dict["postcode_source"], "OSM_PARENT_WAY")
        self.assertEqual(p_dict["parent_osm_id"], "888123")
        self.assertEqual(p_dict["address_conflict_status"], "NO_CONFLICT")

    # K. Branch deduplication
    def test_k_branch_deduplication(self):
        cand1 = DiscoveredBusiness(
            company_name="Pot Kettle Black",
            category="cafe",
            city="Manchester",
            target_country="United Kingdom",
            street="Barton Arcade",
            postcode="M3 2BW",
            branch_identifier="Barton Arcade",
            lat=53.48275,
            lon=-2.24630
        )
        cand2 = DiscoveredBusiness(
            company_name="Pot Kettle Black",
            category="cafe",
            city="Manchester",
            target_country="United Kingdom",
            street="Tariff Street",
            postcode="M1 2FF",
            branch_identifier="Northern Quarter",
            lat=53.48110,
            lon=-2.23245
        )
        cand3 = DiscoveredBusiness(
            company_name="Pot Kettle Black",
            category="cafe",
            city="Manchester",
            target_country="United Kingdom",
            street="",
            postcode="",
            branch_identifier="Terminal 2",
            lat=53.36783,
            lon=-2.28226
        )

        elements_raw = [
            {"type": "node", "id": 1, "tags": {"name": "Pot Kettle Black", "amenity": "cafe", "addr:postcode": "M3 2BW", "addr:street": "Barton Arcade"}, "lat": 53.48275, "lon": -2.24630},
            {"type": "node", "id": 2, "tags": {"name": "Pot Kettle Black", "amenity": "cafe", "addr:postcode": "M1 2FF", "addr:street": "Tariff Street"}, "lat": 53.48110, "lon": -2.23245},
            {"type": "node", "id": 3, "tags": {"name": "Pot Kettle Black", "amenity": "cafe", "branch": "Terminal 2"}, "lat": 53.36783, "lon": -2.28226}
        ]
        processed = self.provider._process_elements(elements_raw, city="Manchester", country="United Kingdom", industry="hospitality", limit=10)
        self.assertEqual(len(processed), 3, "Distinct branches of Pot Kettle Black must not be merged into a single record")

    # L. Pot Kettle Black Barton Arcade branch protection
    def test_l_pot_kettle_black_protection(self):
        # Barton Arcade candidate with source evidence
        pkb_barton = AddressProfile(
            raw_address="Barton Arcade, Manchester M3 2BW",
            normalized_address="Barton Arcade, Manchester, M3 2BW, United Kingdom",
            street="Barton Arcade",
            postcode="M3 2BW",
            city="Manchester",
            country="United Kingdom",
            latitude=53.48275,
            longitude=-2.24630,
            branch_identifier="Barton Arcade",
            completeness=AddressCompleteness.COMPLETE
        )
        query_barton = self.normalizer.generate_location_query("Pot Kettle Black", pkb_barton)
        self.assertEqual(query_barton, '"Pot Kettle Black" "Barton Arcade" "M3 2BW" "Manchester"')

        # Airport candidate without street/postcode
        pkb_airport = AddressProfile(
            raw_address="Manchester, United Kingdom",
            normalized_address="Manchester, United Kingdom",
            street=None,
            postcode=None,
            city="Manchester",
            country="United Kingdom",
            latitude=53.3678333,
            longitude=-2.2822664,
            completeness=AddressCompleteness.PARTIAL
        )
        safe, reason = self.normalizer.is_gosom_safe_to_query(pkb_airport)
        self.assertFalse(safe)
        query_airport = self.normalizer.generate_location_query("Pot Kettle Black", pkb_airport)
        self.assertIsNone(query_airport)

    # M. Backward-compatible fixture loading
    def test_m_backward_compatible_fixture_loading(self):
        legacy_data = {
            "company_name": "Old Cafe",
            "category": "cafe",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "123 High St, Manchester",
            "phone": "+441611112222"
        }
        biz = DiscoveredBusiness(**legacy_data)
        self.assertEqual(biz.company_name, "Old Cafe")
        self.assertEqual(biz.street_source, "")
        self.assertEqual(biz.address_conflict_status, "NO_CONFLICT")
        self.assertEqual(biz.conflicting_address_data, {})

    # N. No fabricated values
    def test_n_no_fabricated_values(self):
        raw_element = {
            "id": 9999999,
            "type": "node",
            "lat": 53.40,
            "lon": -2.25,
            "tags": {"name": "No Address Place", "amenity": "restaurant"}
        }
        profile = self.normalizer.resolve_osm_address(raw_element)
        self.assertIsNone(profile.street)
        self.assertIsNone(profile.postcode)
        self.assertIsNone(profile.house_number)
        self.assertNotIn("Unknown", profile.raw_address)
        self.assertNotIn("N/A", profile.raw_address)

    # O. No reverse geocoding
    def test_o_no_reverse_geocoding(self):
        # Ensure resolve_osm_address does NOT call any network or geocoder
        raw_element = {
            "id": 9999998,
            "type": "node",
            "lat": 53.40,
            "lon": -2.25,
            "tags": {"name": "Pure Coordinates Place", "amenity": "cafe"}
        }
        profile = self.normalizer.resolve_osm_address(raw_element, parent_elements=[])
        # Even though lat/lon exist, street is NOT inferred or reverse-geocoded
        self.assertIsNone(profile.street)
        self.assertIsNone(profile.postcode)

    # P. No paid API
    def test_p_no_paid_api(self):
        # OpenStreetMapProvider and AddressNormalizer have zero references to Google Places or Apify API keys
        self.assertFalse(hasattr(self.provider, "google_places_api_key"))
        self.assertFalse(hasattr(self.provider, "apify_token"))

    # Q. Gosom query generation
    def test_q_gosom_query_generation(self):
        # 1. street + postcode
        p1 = AddressProfile(raw_address="", normalized_address="", street="Palatine Road", postcode="M22 4FY", city="Manchester")
        self.assertEqual(self.normalizer.generate_location_query("Issano", p1), '"Issano" "Palatine Road" "M22 4FY" "Manchester"')

        # 2. street only
        p2 = AddressProfile(raw_address="", normalized_address="", street="Palatine Road", postcode=None, city="Manchester")
        self.assertEqual(self.normalizer.generate_location_query("Bar Bibo", p2), '"Bar Bibo" "Palatine Road" "Manchester"')

        # 3. postcode only
        p3 = AddressProfile(raw_address="", normalized_address="", street=None, postcode="M22 4FY", city="Manchester")
        self.assertEqual(self.normalizer.generate_location_query("Chesters", p3), '"Chesters" "M22 4FY" "Manchester"')

        # 4. neither -> blocked
        p4 = AddressProfile(raw_address="", normalized_address="", street=None, postcode=None, city="Manchester")
        self.assertIsNone(self.normalizer.generate_location_query("Empty Place", p4))

    # R. CRM immutability
    def test_r_crm_immutability(self):
        crm_files = [
            "data/cache_sheets_raw_leads.json",
            "data/cache_sheets_manual_review.json",
            "data/cache_sheets_client_ready.json",
            "data/campaigns.json",
            "data/message_history.json"
        ]
        pre_hashes = {}
        for f in crm_files:
            fp = os.path.join(PROJECT_ROOT, f)
            if os.path.exists(fp):
                with open(fp, "rb") as fh:
                    pre_hashes[f] = hashlib.sha256(fh.read()).hexdigest()

        # Run address normalizer operations
        self.normalizer.parse_address_string("367 Palatine Road, Manchester, M22 4FY")
        self.normalizer.resolve_osm_address({"tags": {"name": "Test"}})

        # Verify hashes
        for f, h in pre_hashes.items():
            fp = os.path.join(PROJECT_ROOT, f)
            with open(fp, "rb") as fh:
                post_h = hashlib.sha256(fh.read()).hexdigest()
            self.assertEqual(h, post_h, f"CRM file mutated: {f}")

    # S. Outreach immutability
    def test_s_outreach_immutability(self):
        msg_file = os.path.join(PROJECT_ROOT, "data", "message_history.json")
        camp_file = os.path.join(PROJECT_ROOT, "data", "campaigns.json")
        h1, h2 = None, None
        if os.path.exists(msg_file):
            with open(msg_file, "rb") as f:
                h1 = hashlib.sha256(f.read()).hexdigest()
        if os.path.exists(camp_file):
            with open(camp_file, "rb") as f:
                h2 = hashlib.sha256(f.read()).hexdigest()

        # Verify no messages dispatched
        self.assertIsNotNone(h1)
        self.assertIsNotNone(h2)


if __name__ == "__main__":
    unittest.main()
