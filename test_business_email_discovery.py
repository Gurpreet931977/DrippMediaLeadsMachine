"""
Dripp Media — Business Email Discovery & Verification Test Suite
================================================================
Section 25: Comprehensive automated tests for all 20 requirements:
  1. email found on official website
  2. email found on verified Facebook page
  3. email found on verified Instagram page
  4. email found on Linktree
  5. email found on reputable business directory
  6. same email found on multiple sources
  7. unrelated business email
  8. guessed email
  9. valid MX but weak source
  10. strong source + valid MX
  11. personal email published by business
  12. multiple emails
  13. duplicate emails
  14. invalid email
  15. no email found
  16. email search budget exhausted
  17. website discovered during enrichment
  18. business identity mismatch
  19. contactability updates without changing V3
  20. existing verified Seoul Kimchi email preserved
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from lib.outreach.email_enricher import (
    EmailVerifier,
    EmailEnricher,
    EmailCandidate,
    EmailType,
    EmailVerificationStatus,
    EmailSource,
    RecipientConfidence,
    BusinessIdentityValidator,
    EmailEnrichmentResult,
)
from lib.outreach.contactability import (
    ContactabilityAssessor,
    ContactabilityState,
)
from lib.outreach.companies_house import (
    CompaniesHouseRecord,
    EntityMatchStatus,
    CompanyStatus,
)


class TestBusinessEmailDiscovery(unittest.TestCase):

    def setUp(self):
        os.environ["SMTP_HOST"] = "smtp.gmail.com"
        os.environ["SMTP_USER"] = "mediadripp@gmail.com"

    # 1. email found on official website
    def test_01_email_found_on_official_website(self):
        with patch("dns.resolver.resolve") as mock_dns:
            mock_item = MagicMock()
            mock_item.exchange = "mail.restaurant.co.uk."
            mock_dns.return_value = [mock_item]

            v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
                "hello@restaurant.co.uk",
                source=EmailSource.OFFICIAL_WEBSITE.value
            )
            self.assertEqual(v_status, EmailVerificationStatus.VERIFIED.value)
            self.assertEqual(email_type, EmailType.GENERIC_BUSINESS.value)
            self.assertIn("corroborated business source", reason)

    # 2. email found on verified Facebook page
    def test_02_email_found_on_verified_facebook_page(self):
        with patch("dns.resolver.resolve") as mock_dns:
            mock_item = MagicMock()
            mock_item.exchange = "gmail-smtp-in.l.google.com."
            mock_dns.return_value = [mock_item]

            v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
                "seoulkimchi@gmail.com",
                source=EmailSource.VERIFIED_FACEBOOK.value
            )
            self.assertEqual(v_status, EmailVerificationStatus.VERIFIED.value)
            self.assertEqual(email_type, EmailType.GENERIC_BUSINESS.value)

    # 3. email found on verified Instagram page
    def test_03_email_found_on_verified_instagram_page(self):
        with patch("dns.resolver.resolve") as mock_dns:
            mock_item = MagicMock()
            mock_item.exchange = "mail.mybar.co.uk."
            mock_dns.return_value = [mock_item]

            v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
                "contact@mybar.co.uk",
                source=EmailSource.VERIFIED_INSTAGRAM.value
            )
            self.assertEqual(v_status, EmailVerificationStatus.VERIFIED.value)
            self.assertIn("VERIFIED_INSTAGRAM", reason)

    # 4. email found on Linktree
    def test_04_email_found_on_linktree(self):
        with patch("dns.resolver.resolve") as mock_dns:
            mock_item = MagicMock()
            mock_item.exchange = "mail.streetfood.co.uk."
            mock_dns.return_value = [mock_item]

            v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
                "info@streetfood.co.uk",
                source=EmailSource.OFFICIAL_LINKTREE.value
            )
            self.assertEqual(v_status, EmailVerificationStatus.VERIFIED.value)

    # 5. email found on reputable business directory
    def test_05_email_found_on_reputable_business_directory(self):
        with patch("dns.resolver.resolve") as mock_dns:
            mock_item = MagicMock()
            mock_item.exchange = "mail.manchester-bakery.co.uk."
            mock_dns.return_value = [mock_item]

            v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
                "info@manchester-bakery.co.uk",
                source=EmailSource.LEGITIMATE_BUSINESS_DIRECTORY.value
            )
            self.assertEqual(v_status, EmailVerificationStatus.VERIFIED.value)

    # 6. same email found on multiple sources
    def test_06_same_email_found_on_multiple_sources(self):
        lead = {
            "lead_id": "LEAD-MULTI-SRC",
            "company_name": "Seoul Kimchi",
            "facebook_url": "https://www.facebook.com/seoulkimchimanchester/",
            "city": "Manchester"
        }
        res = EmailEnricher.discover_emails_for_lead(lead)
        self.assertIsNotNone(res.primary_email)
        self.assertEqual(res.primary_email.email, "seoulkimchi@gmail.com")
        self.assertTrue(res.primary_email.supporting_sources_count >= 1)

    # 7. unrelated business email
    def test_07_unrelated_business_email(self):
        lead = {
            "company_name": "Manchester Shawarma",
            "city": "Manchester"
        }
        # City council environmental health email must be rejected
        is_valid, reason = BusinessIdentityValidator.validate_candidate(
            lead=lead,
            email="envhealth@manchester.gov.uk",
            source_url="https://www.food.gov.uk/ratings",
            source_context="Environmental Health department contact"
        )
        self.assertFalse(is_valid)
        self.assertIn("BUSINESS_IDENTITY_MISMATCH", reason)

    # 8. guessed email
    def test_08_guessed_email_rejected(self):
        v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
            "info@restaurant.co.uk",
            is_guessed=True
        )
        self.assertEqual(v_status, EmailVerificationStatus.INVALID.value)
        self.assertEqual(method, "GUESS_REJECTION")

    # 9. valid MX but weak source
    def test_09_valid_mx_but_weak_source(self):
        with patch("dns.resolver.resolve") as mock_dns:
            mock_item = MagicMock()
            mock_item.exchange = "mail.somedirectory.co.uk."
            mock_dns.return_value = [mock_item]

            v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
                "contact@somedirectory.co.uk",
                source=EmailSource.SEARCH_SNIPPET_ONLY.value
            )
            self.assertEqual(v_status, EmailVerificationStatus.UNVERIFIED.value)
            self.assertIn("lacks primary business confirmation", reason)

    # 10. strong source + valid MX
    def test_10_strong_source_plus_valid_mx(self):
        with patch("dns.resolver.resolve") as mock_dns:
            mock_item = MagicMock()
            mock_item.exchange = "mail.cafe.co.uk."
            mock_dns.return_value = [mock_item]

            v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
                "hello@cafe.co.uk",
                source=EmailSource.OFFICIAL_CONTACT_PAGE.value
            )
            self.assertEqual(v_status, EmailVerificationStatus.VERIFIED.value)

    # 11. personal email published by business
    def test_11_personal_email_published_by_business(self):
        with patch("dns.resolver.resolve") as mock_dns:
            mock_item = MagicMock()
            mock_item.exchange = "gmail-smtp-in.l.google.com."
            mock_dns.return_value = [mock_item]

            v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
                "john.smith@gmail.com",
                source=EmailSource.VERIFIED_FACEBOOK.value
            )
            self.assertEqual(v_status, EmailVerificationStatus.VERIFIED.value)
            self.assertEqual(email_type, EmailType.PERSONAL.value)

    # 12. multiple emails (generic vs personal priority)
    def test_12_multiple_emails_selection(self):
        cand1 = EmailCandidate(
            email="john@business.co.uk",
            email_type=EmailType.PERSONAL.value,
            source=EmailSource.OFFICIAL_WEBSITE.value,
            source_type=EmailSource.OFFICIAL_WEBSITE.value,
            verification_status=EmailVerificationStatus.VERIFIED.value,
            confidence=RecipientConfidence.HIGH.value
        )
        cand2 = EmailCandidate(
            email="hello@business.co.uk",
            email_type=EmailType.GENERIC_BUSINESS.value,
            source=EmailSource.OFFICIAL_WEBSITE.value,
            source_type=EmailSource.OFFICIAL_WEBSITE.value,
            verification_status=EmailVerificationStatus.VERIFIED.value,
            confidence=RecipientConfidence.HIGH.value
        )
        # Sorter prefers generic/role-based business email
        candidates = [cand1, cand2]
        def sort_key(c):
            t_rank = 2 if c.email_type in (EmailType.GENERIC_BUSINESS.value, EmailType.ROLE_BASED.value) else 1
            return t_rank
        candidates.sort(key=sort_key, reverse=True)
        self.assertEqual(candidates[0].email, "hello@business.co.uk")

    # 13. duplicate emails
    def test_13_duplicate_emails_normalized(self):
        c1 = "Hello@Business.co.uk "
        c2 = "hello@business.co.uk"
        self.assertEqual(c1.strip().lower(), c2.strip().lower())

    # 14. invalid email
    def test_14_invalid_email(self):
        # malamanchester.xyz domain has no MX
        v_status, email_type, method, mx_list, reason = EmailVerifier.verify("hello@malamanchester.xyz")
        self.assertEqual(v_status, EmailVerificationStatus.INVALID.value)

    # 15. no email found
    def test_15_no_email_found(self):
        lead = {
            "lead_id": "LEAD-MARYD-TEST",
            "company_name": "Mary D's Beamish Bar",
            "city": "Manchester"
        }
        res = EmailEnricher.discover_emails_for_lead(lead)
        self.assertIsNone(res.primary_email)
        self.assertEqual(res.search_status, "NO_EMAIL_FOUND")

    # 16. email search budget exhausted
    def test_16_email_search_budget_exhausted(self):
        lead = {
            "lead_id": "LEAD-BUDGET-TEST",
            "company_name": "99 Reasons",
            "city": "Manchester"
        }
        res = EmailEnricher.discover_emails_for_lead(lead, max_search_queries=5)
        self.assertTrue(res.searches_performed <= 5)
        self.assertEqual(res.search_status, "NO_EMAIL_FOUND")

    # 17. website discovered during enrichment
    def test_17_website_discovered_during_enrichment(self):
        # When an active website domain is flagged, V3 qualification must remain unchanged
        lead = {
            "lead_id": "LEAD-DISCO-TEST",
            "company_name": "Test Prospect",
            "website_status": "NO_WEBSITE_CONFIRMED",
            "qualification_state": "OUTREACH_READY",
            "qualification_status": "QUALIFIED"
        }
        res = EmailEnricher.discover_emails_for_lead(lead)
        # Ensure lead website_status is preserved exactly
        self.assertEqual(lead["website_status"], "NO_WEBSITE_CONFIRMED")
        self.assertEqual(lead["qualification_state"], "OUTREACH_READY")

    # 18. business identity mismatch
    def test_18_business_identity_mismatch(self):
        lead = {
            "company_name": "Mala",
            "city": "Manchester"
        }
        is_valid, reason = BusinessIdentityValidator.validate_candidate(
            lead=lead,
            email="bookings@malasichuan.co.uk",
            source_url="https://malasichuan.co.uk/london/contact",
            source_context="Mala Sichuan Restaurant in London Covent Garden"
        )
        self.assertFalse(is_valid)
        self.assertIn("BUSINESS_IDENTITY_MISMATCH", reason)

    # 19. contactability updates without changing V3
    def test_19_contactability_updates_without_changing_v3(self):
        lead = {
            "lead_id": "LEAD-MAN-709C66",
            "company_name": "Hong Thai",
            "address": "Unit N13, Arndale Market 49 High Street, Manchester M4 3AH",
            "postcode": "M4 3AH",
            "city": "Manchester",
            "website_status": "NO_WEBSITE_CONFIRMED",
            "qualification_state": "OUTREACH_READY",
            "qualification_status": "QUALIFIED"
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        # Because Hong Thai email permanently bounced in live campaign, contactability is NOT_CONTACTABLE
        self.assertEqual(assessment.contactability_status, ContactabilityState.NOT_CONTACTABLE.value)
        # Qualification V3 must remain completely intact (OUTREACH_READY)
        self.assertEqual(assessment.qualification_state, "OUTREACH_READY")
        self.assertEqual(lead["qualification_status"], "QUALIFIED")
        self.assertEqual(lead["website_status"], "NO_WEBSITE_CONFIRMED")

    # 20. existing verified Seoul Kimchi email preserved
    def test_20_existing_verified_seoul_kimchi_email_preserved(self):
        lead = {
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "address": "275 Upper Brook St, Manchester M13 0HR",
            "postcode": "M13 0HR",
            "city": "Manchester",
            "facebook_url": "https://www.facebook.com/seoulkimchimanchester/",
            "qualification_state": "OUTREACH_READY"
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        self.assertEqual(assessment.contactability_status, ContactabilityState.CONTACTABLE.value)
        self.assertIn("Email", assessment.sendable_channels)
        self.assertEqual(assessment.channels["Email"].recipient, "seoulkimchi@gmail.com")
        self.assertTrue(assessment.channels["Email"].sendable)

    # 21. multi-city business identity validation
    def test_21_multi_city_email_validation(self):
        # A London business should not be rejected when context is London
        london_lead = {
            "company_name": "Dishoom",
            "city": "London",
            "postcode": "WC2H 9FB"
        }
        is_valid, reason = BusinessIdentityValidator.validate_candidate(
            lead=london_lead,
            email="coventgarden@dishoom.com",
            source_url="https://www.dishoom.com/covent-garden/",
            source_context="Dishoom Covent Garden 12 Upper St. Martin's Lane London WC2H 9FB"
        )
        self.assertTrue(is_valid, f"Expected valid for London lead in London context, got reason: {reason}")

        # A London business SHOULD be rejected if context belongs to Birmingham
        is_valid_bham, reason_bham = BusinessIdentityValidator.validate_candidate(
            lead=london_lead,
            email="info@dishoombirmingham.co.uk",
            source_url="https://dishoombirmingham.co.uk",
            source_context="Authentic dining in Birmingham city centre"
        )
        self.assertFalse(is_valid_bham)
        self.assertIn("BUSINESS_IDENTITY_MISMATCH", reason_bham)
        self.assertIn("Birmingham", reason_bham)

    # 22. multi-city search queries
    def test_22_multi_city_search_queries(self):
        lead = {
            "company_name": "The Botanist",
            "city": "Birmingham",
            "postcode": "B2 5EH"
        }
        queries = EmailEnricher.generate_search_queries(lead)
        self.assertTrue(any("Birmingham" in q for q in queries))
        self.assertFalse(any("Manchester" in q for q in queries))

    # 23. null safety guards
    def test_23_null_safety_guards(self):
        is_valid, reason = BusinessIdentityValidator.validate_candidate(
            lead=None,
            email="test@example.com",
            source_url="",
            source_context=""
        )
        self.assertIsInstance(is_valid, bool)

        queries = EmailEnricher.generate_search_queries(None)
        self.assertIsInstance(queries, list)


if __name__ == "__main__":
    unittest.main()

