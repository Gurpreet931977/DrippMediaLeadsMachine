"""
Dripp Media — UK Entity Verification & Email Contactability Test Suite
======================================================================
Section 18: Comprehensive automated tests for all 18 requirements:
  1. active company exact match
  2. trading name vs legal company name
  3. dissolved old company + current active business
  4. weak dissolved company name match
  5. no company match
  6. ambiguous company matches
  7. verified email from official website
  8. verified email from verified social page
  9. MX valid but email source weak
  10. guessed email rejected
  11. corporate subscriber + verified email
  12. individual subscriber + no consent
  13. unknown subscriber
  14. verified Instagram but no messageable ID
  15. verified Facebook but no messageable ID
  16. one channel contactable
  17. no channels contactable
  18. OUTREACH_READY remains unchanged when contactability changes
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

from lib.outreach.companies_house import (
    CompaniesHouseVerifier,
    CompaniesHouseRecord,
    EntityMatchStatus,
    EntityMatchConfidence,
    CompanyStatus,
    EntitySource,
)
from lib.outreach.compliance import (
    UKComplianceEvaluator,
    SubscriberType,
    MarketingEmailStatus,
    LawfulBasisStatus,
    OptOutStatus,
    ComplianceReviewStatus,
    SuppressionManager,
    ContactHistoryManager,
)
from lib.outreach.email_enricher import (
    EmailVerifier,
    EmailEnricher,
    EmailCandidate,
    EmailType,
    EmailVerificationStatus,
    EmailSource,
    RecipientConfidence,
)
from lib.outreach.contactability import (
    ContactabilityAssessor,
    ContactabilityState,
    ChannelSendabilityState,
)


class TestUKEntityAndContactability(unittest.TestCase):

    def setUp(self):
        # Configure SMTP so email integration is available
        os.environ["SMTP_HOST"] = "smtp.gmail.com"
        os.environ["SMTP_USER"] = "mediadripp@gmail.com"

    # 1. active company exact match
    def test_01_active_company_exact_match(self):
        lead = {
            "company_name": "Seoul Kimchi",
            "address": "275 Upper Brook St, Manchester M13 0HR",
            "postcode": "M13 0HR",
            "city": "Manchester",
            "operational_status": "ACTIVE_CONFIRMED"
        }
        rec = CompaniesHouseVerifier.verify_entity(lead)
        self.assertEqual(rec.entity_match_status, EntityMatchStatus.MATCHED_ACTIVE.value)
        self.assertEqual(rec.entity_match_confidence, EntityMatchConfidence.HIGH.value)
        self.assertEqual(rec.company_status, CompanyStatus.ACTIVE.value)
        self.assertTrue(rec.is_corporate_subscriber)
        self.assertEqual(rec.companies_house_number, "07064070")
        self.assertIn("SEOUL KIMCHI LIMITED", rec.legal_entity_name.upper())

    # 2. trading name vs legal company name
    def test_02_trading_name_vs_legal_company_name(self):
        lead = {
            "company_name": "Hong Thai",
            "address": "Unit N13, Arndale Market 49 High Street, Manchester M4 3AH",
            "postcode": "M4 3AH",
            "city": "Manchester",
            "operational_status": "ACTIVE_CONFIRMED"
        }
        rec = CompaniesHouseVerifier.verify_entity(lead)
        self.assertEqual(rec.trading_name, "Hong Thai")
        self.assertIn("HONG THAI", rec.legal_entity_name.upper())
        self.assertEqual(rec.companies_house_number, "11493711")
        self.assertEqual(rec.entity_match_status, EntityMatchStatus.MATCHED_ACTIVE.value)
        # Ensure trading name is not overwritten by legal entity name
        self.assertNotEqual(rec.trading_name, rec.legal_entity_name)

    # 3. dissolved old company + current active business
    def test_03_dissolved_old_company_plus_current_active_business(self):
        lead = {
            "company_name": "Mala",
            "address": "8 Lever St, Manchester M1 1FL",
            "postcode": "M1 1FL",
            "city": "Manchester",
            "operational_status": "ACTIVE_CONFIRMED",
            "qualification_state": "OUTREACH_READY"
        }
        rec = CompaniesHouseVerifier.verify_entity(lead)
        # The business is active, but Companies House match is dissolved -> CONFLICT
        self.assertEqual(rec.company_status, CompanyStatus.DISSOLVED.value)
        self.assertEqual(rec.entity_match_status, EntityMatchStatus.CONFLICT.value)
        self.assertFalse(rec.is_corporate_subscriber)
        # Crucial: Operational status must remain active; do NOT mark closed!
        self.assertEqual(lead["operational_status"], "ACTIVE_CONFIRMED")

        # Compliance must classify as UNKNOWN subscriber & MANUAL_REVIEW
        comp = UKComplianceEvaluator.evaluate(lead, "hello@malamanchester.xyz", entity_record=rec)
        self.assertEqual(comp.subscriber_type, SubscriberType.UNKNOWN)
        self.assertEqual(comp.marketing_email_status, MarketingEmailStatus.MANUAL_REVIEW)
        self.assertIn("CONFLICT", comp.compliance_notes)

    # 4. weak dissolved company name match
    def test_04_weak_dissolved_company_name_match(self):
        with patch.object(CompaniesHouseVerifier, "search_companies") as mock_search:
            mock_search.return_value = [
                {
                    "company_number": "99999999",
                    "title": "DISTANT UNRELATED DISSOLVED CO LTD",
                    "company_status": "DISSOLVED",
                    "company_type": "ltd",
                    "address_snippet": "99 Faraway Lane, Plymouth PL1 1AA",
                    "sic_codes": ["70229"]
                }
            ]
            lead = {
                "company_name": "City Shawarma",
                "address": "127 Oxford Road, Manchester M1 7DY",
                "postcode": "M1 7DY",
                "city": "Manchester"
            }
            rec = CompaniesHouseVerifier.verify_entity(lead)
            self.assertEqual(rec.entity_match_status, EntityMatchStatus.NO_MATCH.value)
            self.assertFalse(rec.is_corporate_subscriber)

    # 5. no company match
    def test_05_no_company_match(self):
        lead = {
            "company_name": "Nonexistent Fictional Cafe 99999",
            "address": "123 Fictional Way, Manchester",
            "postcode": "M99 9ZZ",
            "city": "Manchester"
        }
        rec = CompaniesHouseVerifier.verify_entity(lead)
        self.assertEqual(rec.entity_match_status, EntityMatchStatus.NO_MATCH.value)
        self.assertIn(rec.entity_match_confidence, [EntityMatchConfidence.UNKNOWN.value, EntityMatchConfidence.LOW.value])
        self.assertFalse(rec.is_corporate_subscriber)
        comp = UKComplianceEvaluator.evaluate(lead, "hello@fictional.co.uk", entity_record=rec)
        self.assertEqual(comp.subscriber_type, SubscriberType.UNKNOWN)
        self.assertEqual(comp.marketing_email_status, MarketingEmailStatus.MANUAL_REVIEW)

    # 6. ambiguous company matches
    def test_06_ambiguous_company_matches(self):
        with patch.object(CompaniesHouseVerifier, "search_companies") as mock_search:
            mock_search.return_value = [
                {
                    "company_number": "11111111",
                    "title": "COMMON TAVERN LIMITED",
                    "company_status": "ACTIVE",
                    "company_type": "ltd",
                    "address_snippet": "1 High St, Manchester M1 1AA",
                    "sic_codes": ["56101"]
                },
                {
                    "company_number": "22222222",
                    "title": "COMMON TAVERN MCR LTD",
                    "company_status": "ACTIVE",
                    "company_type": "ltd",
                    "address_snippet": "2 High St, Manchester M1 1AA",
                    "sic_codes": ["56101"]
                }
            ]
            lead = {
                "company_name": "Common Tavern",
                "address": "High St, Manchester",
                "postcode": "M1 1AA",
                "city": "Manchester"
            }
            rec = CompaniesHouseVerifier.verify_entity(lead)
            self.assertEqual(rec.entity_match_status, EntityMatchStatus.AMBIGUOUS.value)
            self.assertFalse(rec.is_corporate_subscriber)

    # 7. verified email from official website
    def test_07_verified_email_from_official_website(self):
        with patch("dns.resolver.resolve") as mock_dns:
            mock_item = MagicMock()
            mock_item.exchange = "mail.restaurant.co.uk."
            mock_dns.return_value = [mock_item]

            v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
                "hello@restaurant.co.uk",
                source=EmailSource.OFFICIAL_WEBSITE.value
            )
            self.assertEqual(v_status, EmailVerificationStatus.VERIFIED)
            self.assertEqual(email_type, EmailType.GENERIC_BUSINESS)
            self.assertEqual(mx_list, ["mail.restaurant.co.uk"])
            self.assertIn("corroborated business source", reason)

    # 8. verified email from verified social page
    def test_08_verified_email_from_verified_social_page(self):
        with patch("dns.resolver.resolve") as mock_dns:
            mock_item = MagicMock()
            mock_item.exchange = "gmail-smtp-in.l.google.com."
            mock_dns.return_value = [mock_item]

            v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
                "seoulkimchi@gmail.com",
                source=EmailSource.VERIFIED_FACEBOOK.value
            )
            self.assertEqual(v_status, EmailVerificationStatus.VERIFIED)
            self.assertEqual(email_type, EmailType.GENERIC_BUSINESS)
            self.assertTrue(len(mx_list) > 0)
            self.assertIn("VERIFIED_FACEBOOK", reason)

    # 9. MX valid but email source weak
    def test_09_mx_valid_but_email_source_weak(self):
        with patch("dns.resolver.resolve") as mock_dns:
            mock_item = MagicMock()
            mock_item.exchange = "mail.somedirectory.co.uk."
            mock_dns.return_value = [mock_item]

            v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
                "contact@somedirectory.co.uk",
                source=EmailSource.PUBLIC_BUSINESS_DIRECTORY.value
            )
            # Section 10: UNVERIFIED when only technical checks pass without strong business source
            self.assertEqual(v_status, EmailVerificationStatus.UNVERIFIED)
            self.assertIn("lacks primary business confirmation", reason)

    # 10. guessed email rejected
    def test_10_guessed_email_rejected(self):
        v_status, email_type, method, mx_list, reason = EmailVerifier.verify(
            "info@restaurant.co.uk",
            is_guessed=True
        )
        self.assertEqual(v_status, EmailVerificationStatus.INVALID)
        self.assertEqual(method, "GUESS_REJECTION")
        self.assertIn("Guessed or synthesized email rejected", reason)

    # 11. corporate subscriber + verified email
    def test_11_corporate_subscriber_plus_verified_email(self):
        lead = {
            "company_name": "Seoul Kimchi",
            "address": "275 Upper Brook St, Manchester M13 0HR",
            "postcode": "M13 0HR",
            "city": "Manchester"
        }
        entity_rec = CompaniesHouseRecord(
            trading_name="Seoul Kimchi",
            legal_entity_name="SEOUL KIMCHI LIMITED",
            companies_house_number="07064070",
            company_type="ltd",
            company_status="ACTIVE",
            entity_match_status="MATCHED_ACTIVE",
            entity_match_confidence="HIGH",
            is_corporate_subscriber=True
        )
        comp = UKComplianceEvaluator.evaluate(lead, "seoulkimchi@gmail.com", entity_record=entity_rec)
        self.assertEqual(comp.subscriber_type, SubscriberType.CORPORATE_SUBSCRIBER)
        self.assertEqual(comp.marketing_email_status, MarketingEmailStatus.COMPLIANCE_ELIGIBLE)
        self.assertEqual(comp.lawful_basis_status, LawfulBasisStatus.LEGITIMATE_INTERESTS_ASSESSED)
        self.assertEqual(comp.compliance_review_status, ComplianceReviewStatus.PASSED)

    # 12. individual subscriber + no consent
    def test_12_individual_subscriber_plus_no_consent(self):
        lead = {
            "company_name": "John Doe Catering",
            "subscriber_type": "INDIVIDUAL_SUBSCRIBER"
        }
        entity_rec = CompaniesHouseRecord(
            entity_match_status="NO_MATCH",
            is_corporate_subscriber=False
        )
        comp = UKComplianceEvaluator.evaluate(lead, "john@johndoecatering.co.uk", entity_record=entity_rec)
        self.assertEqual(comp.subscriber_type, SubscriberType.INDIVIDUAL_SUBSCRIBER)
        self.assertEqual(comp.marketing_email_status, MarketingEmailStatus.MANUAL_REVIEW)
        self.assertEqual(comp.lawful_basis_status, LawfulBasisStatus.CONSENT_REQUIRED)

    # 13. unknown subscriber
    def test_13_unknown_subscriber(self):
        lead = {"company_name": "Mystery Bistro"}
        entity_rec = CompaniesHouseRecord(entity_match_status="NO_MATCH", is_corporate_subscriber=False)
        comp = UKComplianceEvaluator.evaluate(lead, "info@mysterybistro.co.uk", entity_record=entity_rec)
        self.assertEqual(comp.subscriber_type, SubscriberType.UNKNOWN)
        self.assertEqual(comp.marketing_email_status, MarketingEmailStatus.MANUAL_REVIEW)
        self.assertIn("SUBSCRIBER_TYPE_UNCERTAIN", comp.compliance_notes)

    # 14. verified Instagram but no messageable ID
    def test_14_verified_instagram_but_no_messageable_id(self):
        lead = {
            "lead_id": "LEAD-MAN-682E5D",
            "company_name": "Mary D's Beamish Bar",
            "instagram_url": "https://www.instagram.com/marydsbar/",
            "instagram_ownership_status": "VERIFIED",
            "qualification_state": "OUTREACH_READY"
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        ig_eval = assessment.channels["Instagram Direct Message"]
        self.assertTrue(ig_eval.business_identity_verified)
        self.assertFalse(ig_eval.recipient_verified)
        self.assertFalse(ig_eval.sendable)
        self.assertIn("RECIPIENT_ID_REQUIRED", ig_eval.reason)

    # 15. verified Facebook but no messageable ID
    def test_15_verified_facebook_but_no_messageable_id(self):
        lead = {
            "lead_id": "LEAD-MAN-14A2D3",
            "company_name": "Mala",
            "facebook_url": "https://www.facebook.com/MalaSecretGarden/",
            "facebook_ownership_status": "VERIFIED",
            "qualification_state": "OUTREACH_READY"
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        fb_eval = assessment.channels["Facebook Messenger"]
        self.assertTrue(fb_eval.business_identity_verified)
        self.assertFalse(fb_eval.recipient_verified)
        self.assertFalse(fb_eval.sendable)
        self.assertIn("RECIPIENT_ID_REQUIRED", fb_eval.reason)

    # 16. one channel contactable
    def test_16_one_channel_contactable(self):
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
        self.assertEqual(assessment.sendable_channels, ["Email"])
        self.assertTrue(assessment.is_ready_for_send)
        self.assertTrue(assessment.channels["Email"].sendable)

    # 17. no channels contactable
    def test_17_no_channels_contactable(self):
        lead = {
            "lead_id": "LEAD-MAN-94F2CB",
            "company_name": "Waka Waka MCR",
            "qualification_state": "OUTREACH_READY"
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        self.assertEqual(assessment.contactability_status, ContactabilityState.NOT_CONTACTABLE.value)
        self.assertEqual(assessment.sendable_channels, [])
        self.assertFalse(assessment.is_ready_for_send)

    # 18. OUTREACH_READY remains unchanged when contactability changes
    def test_18_outreach_ready_remains_unchanged_when_contactability_changes(self):
        lead = {
            "lead_id": "LEAD-TEST-018",
            "company_name": "Seoul Kimchi",
            "qualification_state": "OUTREACH_READY"
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        self.assertEqual(assessment.qualification_state, "OUTREACH_READY")
        self.assertEqual(lead["qualification_state"], "OUTREACH_READY")


if __name__ == "__main__":
    unittest.main()
