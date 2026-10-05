"""
Dripp Media — Geography Neutrality & Country Adapter Tests
==========================================================
Verifies that:
  - City lookup is not Birmingham-specific.
  - Boundary validation is not Leeds-specific.
  - Country filtering is not UK-specific.
  - Postcodes are not assumed to follow UK format.
  - Phone formatting is not assumed to be +44.
  - Business registries are optional and return SUPPORTED / NOT_CONFIGURED / UNAVAILABLE.
  - Compliance adapters are localized and optional.
  - Global pipeline components handle fictional and real global locations without regressions.
"""

import unittest
import os
import json
import tempfile
from typing import Dict, Any

from lib.discovery.geo_provider import GeoProvider, get_geo_provider
from lib.country_adapters import (
    get_country_adapter,
    AdapterStatus,
    get_uk_adapter_bundle,
    get_us_adapter_bundle,
    get_de_adapter_bundle,
    get_ae_adapter_bundle,
    get_generic_adapter_bundle
)
from lib.validation.country_validator import CountryValidator
from lib.types import CountryStatus
from lib.outreach.contactability import ContactabilityAssessor


class TestGeographyNeutrality(unittest.TestCase):

    def setUp(self):
        self.geo = get_geo_provider()
        self.validator = CountryValidator()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. GEO PROVIDER TESTS (Not Birmingham or Leeds specific)
    # ──────────────────────────────────────────────────────────────────────────

    def test_country_resolution_global_and_fictional(self):
        """Resolves real global countries and handles unknown/fictional jurisdictions."""
        # Real countries
        us = self.geo.resolve_country("United States")
        self.assertEqual(us["iso2"], "US")
        self.assertEqual(us["calling_code"], "+1")
        self.assertEqual(us["status"], "RESOLVED")

        de = self.geo.resolve_country("Germany")
        self.assertEqual(de["iso2"], "DE")
        self.assertEqual(de["calling_code"], "+49")
        self.assertEqual(de["status"], "RESOLVED")

        ae = self.geo.resolve_country("United Arab Emirates")
        self.assertEqual(ae["iso2"], "AE")
        self.assertEqual(ae["calling_code"], "+971")
        self.assertEqual(ae["status"], "RESOLVED")

        # Fictional / uncatalogued jurisdiction (Atlantis)
        atlantis = self.geo.resolve_country("Atlantis")
        self.assertEqual(atlantis["name"], "Atlantis")
        self.assertEqual(atlantis["status"], "UNRESOLVED_GENERIC")
        self.assertFalse(atlantis["calling_code"])

    def test_locality_normalization_multilingual(self):
        """Strips diacritics and normalizes international city names."""
        self.assertEqual(self.geo.normalize_locality("München"), "Munchen")
        self.assertEqual(self.geo.normalize_locality("Düsseldorf"), "Dusseldorf")
        self.assertEqual(self.geo.normalize_locality("São Paulo"), "Sao Paulo")
        self.assertEqual(self.geo.normalize_locality("Zürich"), "Zurich")

    def test_locality_aliases_not_birmingham_specific(self):
        """Retrieves aliases for international cities and falls back for fictional ones."""
        # Registered cities
        berlin_aliases = self.geo.get_locality_aliases("Berlin", "Germany")
        self.assertIn("mitte", berlin_aliases)
        self.assertIn("kreuzberg", berlin_aliases)

        dubai_aliases = self.geo.get_locality_aliases("Dubai", "United Arab Emirates")
        self.assertIn("dxb", dubai_aliases)
        self.assertIn("marina", dubai_aliases)

        # Fictional city (Metropolis)
        metro_aliases = self.geo.get_locality_aliases("Metropolis Central", "Atlantis")
        self.assertIn("metropolis central", metro_aliases)
        self.assertIn("metropolis", metro_aliases)
        self.assertIn("central", metro_aliases)

    def test_boundary_validation_synthetic_fictional_polygon(self):
        """Boundary validation works on arbitrary synthetic polygons, not just Leeds/Birmingham."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fictional_geo = GeoProvider(boundaries_dir=tmpdir)
            # Create a synthetic boundary polygon for fictional city 'Novapolis'
            # Box between lat 10.0 to 11.0, lon 20.0 to 21.0
            synthetic_boundary = {
                "type": "Feature",
                "properties": {"name": "Novapolis", "boundary": "administrative"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [20.0, 10.0],
                        [21.0, 10.0],
                        [21.0, 11.0],
                        [20.0, 11.0],
                        [20.0, 10.0]
                    ]]
                }
            }
            cache_file = os.path.join(tmpdir, "novapolis_admin_boundary.geojson")
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(synthetic_boundary, f)

            # Point inside (lat 10.5, lon 20.5)
            is_in, reason = fictional_geo.validate_point_in_boundary(
                lat=10.5, lon=20.5, city="Novapolis", country="Utopia"
            )
            self.assertTrue(is_in, f"Point should be inside: {reason}")
            self.assertIn("polygon", reason.lower())

            # Point outside (lat 12.0, lon 25.0)
            is_in_out, reason_out = fictional_geo.validate_point_in_boundary(
                lat=12.0, lon=25.0, city="Novapolis", country="Utopia"
            )
            self.assertFalse(is_in_out, f"Point should be outside: {reason_out}")
            self.assertIn("outside", reason_out.lower())

    # ──────────────────────────────────────────────────────────────────────────
    # 2. COUNTRY ADAPTERS & PHONE / ADDRESS / COMPLIANCE NORMALIZATION
    # ──────────────────────────────────────────────────────────────────────────

    def test_phone_formatting_not_assumed_plus44(self):
        """Phone normalizer supports UK (+44), US (+1), DE (+49), AE (+971), and Generic."""
        uk_phone = get_country_adapter("GB").phone_normalizer
        self.assertTrue(uk_phone.is_valid("0121 236 1234"))
        self.assertTrue(uk_phone.is_valid("+44 7911 123456"))
        self.assertEqual(uk_phone.normalize("0121 236 1234"), "+441212361234")
        self.assertEqual(uk_phone.normalize_for_search("+44 121 236 1234"), "01212361234")

        us_phone = get_country_adapter("US").phone_normalizer
        self.assertTrue(us_phone.is_valid("(415) 555-2671"))
        self.assertTrue(us_phone.is_valid("+1 415 555 2671"))
        self.assertEqual(us_phone.normalize("415 555 2671"), "+14155552671")
        self.assertEqual(us_phone.normalize_for_search("4155552671"), "415-555-2671")

        de_phone = get_country_adapter("DE").phone_normalizer
        self.assertTrue(de_phone.is_valid("+49 30 1234567"))
        self.assertTrue(de_phone.is_valid("030 1234567"))
        self.assertEqual(de_phone.normalize("030 1234567"), "+49301234567")

        ae_phone = get_country_adapter("AE").phone_normalizer
        self.assertTrue(ae_phone.is_valid("+971 4 1234567"))
        self.assertTrue(ae_phone.is_valid("050 1234567"))
        self.assertEqual(ae_phone.normalize("050 1234567"), "+971501234567")

    def test_postcode_not_assumed_uk_format(self):
        """Address normalizer extracts national postal codes across jurisdictions."""
        # UK Postcode
        uk_addr = get_country_adapter("GB").address_normalizer
        self.assertEqual(uk_addr.extract_postal_code("10 High Street, Leeds LS1 2AB"), "LS1 2AB")

        # US 5-digit ZIP
        us_addr = get_country_adapter("US").address_normalizer
        self.assertEqual(us_addr.extract_postal_code("100 Market St, San Francisco, CA 94105"), "94105")

        # DE 5-digit PLZ
        de_addr = get_country_adapter("DE").address_normalizer
        self.assertEqual(de_addr.extract_postal_code("Friedrichstraße 43, 10117 Berlin"), "10117")

        # AE P.O. Box or Makani (no standard street postcodes)
        ae_addr = get_country_adapter("AE").address_normalizer
        self.assertEqual(ae_addr.extract_postal_code("Sheikh Zayed Rd, P.O. Box 9214, Dubai"), "P.O. Box 9214")

    def test_business_registry_status_and_no_false_universality(self):
        """Verifies SUPPORTED, NOT_CONFIGURED, and UNAVAILABLE statuses without false data."""
        # UK: Supported (Companies House)
        uk_reg = get_country_adapter("GB").business_registry
        self.assertEqual(uk_reg.status, AdapterStatus.SUPPORTED.value)

        # US: Not configured (requires state-level integration)
        us_reg = get_country_adapter("US").business_registry
        self.assertEqual(us_reg.status, AdapterStatus.NOT_CONFIGURED.value)
        us_res = us_reg.verify_entity({"name": "Acme Corp", "city": "Austin"})
        self.assertFalse(us_res["entity_matched"])
        self.assertEqual(us_res["registry_status"], "NOT_CONFIGURED")

        # DE: Not configured (requires Handelsregister integration)
        de_reg = get_country_adapter("DE").business_registry
        self.assertEqual(de_reg.status, AdapterStatus.NOT_CONFIGURED.value)

        # AE: Not configured (requires NER/DED integration)
        ae_reg = get_country_adapter("AE").business_registry
        self.assertEqual(ae_reg.status, AdapterStatus.NOT_CONFIGURED.value)

        # Fictional: Unavailable (Never claims false universality or fabricated data)
        fictional_reg = get_country_adapter("Atlantis").business_registry
        self.assertEqual(fictional_reg.status, AdapterStatus.UNAVAILABLE.value)
        fic_res = fictional_reg.verify_entity({"name": "Poseidon Cafe", "city": "Atlantis City"})
        self.assertFalse(fic_res["entity_matched"])
        self.assertEqual(fic_res["company_status"], "UNAVAILABLE")

    def test_compliance_adapter_regional_rules(self):
        """Compliance rules differ according to national electronic marketing regulations."""
        # US: CAN-SPAM commercial opt-out
        us_comp = get_country_adapter("US").compliance
        us_eval = us_comp.evaluate_marketing_email(
            lead={"company_name": "Bay Tech LLC", "city": "San Francisco"},
            email="contact@baytech.io"
        )
        self.assertEqual(us_eval["marketing_email_status"], "COMPLIANCE_ELIGIBLE")
        self.assertEqual(us_eval["lawful_basis_status"], "CAN_SPAM_COMMERCIAL_OPT_OUT")

        # DE: German UWG § 7 strict opt-in
        de_comp = get_country_adapter("DE").compliance
        de_eval = de_comp.evaluate_marketing_email(
            lead={"company_name": "Berlin Gastro GmbH", "city": "Berlin"},
            email="info@berlingastro.de"
        )
        self.assertEqual(de_eval["marketing_email_status"], "BLOCKED")
        self.assertIn("UWG", de_eval["compliance_notes"])

        # AE: TDRA regulation
        ae_comp = get_country_adapter("AE").compliance
        ae_eval = ae_comp.evaluate_marketing_email(
            lead={"company_name": "Dubai Marina Grill", "city": "Dubai"},
            email="info@marinagrill.ae"
        )
        self.assertEqual(ae_eval["marketing_email_status"], "MANUAL_REVIEW")

    # ──────────────────────────────────────────────────────────────────────────
    # 3. GLOBAL PIPELINE COMPATIBILITY
    # ──────────────────────────────────────────────────────────────────────────

    def test_country_validator_supports_multiple_jurisdictions(self):
        """CountryValidator validates US, DE, AE, and Generic without assuming UK."""
        # US lead
        status, detected, region, pc, evidence = self.validator.validate(
            target_country="United States",
            address="500 Howard St, CA 94105",
            phone="+1 415 555 1234"
        )
        self.assertEqual(status, CountryStatus.COUNTRY_MATCH.value)
        self.assertEqual(detected, "United States")
        self.assertEqual(region, "CA")
        self.assertEqual(pc, "94105")

        # German lead
        status, detected, region, pc, evidence = self.validator.validate(
            target_country="Germany",
            address="Alexanderplatz 1, 10178 Berlin",
            phone="+49 30 901820"
        )
        self.assertEqual(status, CountryStatus.COUNTRY_MATCH.value)
        self.assertEqual(detected, "Germany")
        self.assertEqual(pc, "10178")

        # UAE lead
        status, detected, region, pc, evidence = self.validator.validate(
            target_country="United Arab Emirates",
            address="Sheikh Zayed Road, Dubai",
            phone="+971 4 330 0000"
        )
        self.assertEqual(status, CountryStatus.COUNTRY_MATCH.value)
        self.assertEqual(detected, "United Arab Emirates")

    def test_contactability_assessor_handles_us_lead_without_companies_house(self):
        """ContactabilityAssessor assesses US lead gracefully without Companies House error."""
        us_lead = {
            "lead_id": "LEAD-US-001",
            "company_name": "Pacific Blue Seafood",
            "city": "Seattle",
            "state": "WA",
            "country": "United States",
            "target_country": "United States",
            "qualification_state": "OUTREACH_READY",
            "address": "1200 Alaskan Way, Seattle, WA 98101",
            "phone": "+1 206 555 9876",
            "website": "https://pacificblueseafood.com"
        }
        assessment = ContactabilityAssessor.assess_lead(us_lead)
        self.assertEqual(assessment.lead_id, "LEAD-US-001")
        # Registry should be NOT_CONFIGURED, not failed or falsely matched
        self.assertEqual(assessment.entity_data.get("registry_status"), "NOT_CONFIGURED")
        self.assertFalse(assessment.entity_data.get("entity_matched"))


if __name__ == "__main__":
    unittest.main()
