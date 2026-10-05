"""
Unit & Integration Tests for Phase 6: Contactability Engine Hardening
====================================================================
Comprehensive tests for:
  - Cases A through N (Section 16)
  - Channel-level states (EMAIL, INSTAGRAM, FACEBOOK)
  - Manual vs Automated contactability separation
  - MX states (VALID, INVALID, NO_MX, UNKNOWN)
  - Generic vs Business domain email classification
  - Hard bounce suppression preservation (550 5.1.1)
  - Compliance state separation from technical availability
  - Absolute qualification invariance (zero qualification mutation)
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock
import dns.resolver

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from lib.types import QualificationState
from lib.outreach.contactability import (
    ContactabilityAssessor,
    ContactabilityState,
    ChannelStatus,
    ComplianceState,
    ChannelContactability,
    LeadContactabilityAssessment,
)
from lib.outreach.email_enricher import (
    EmailVerifier,
    EmailEnricher,
    EmailCandidate,
    EmailVerificationStatus,
    MXStatus,
    BusinessDomainType,
    EmailType,
    RecipientConfidence,
)
from lib.outreach.compliance import (
    SuppressionManager,
    MarketingEmailStatus,
)


class TestContactabilityHardening(unittest.TestCase):
    def setUp(self):
        # Ensure test suppression / clean state
        pass

    # ──────────────────────────────────────────────────────────────────────
    # SECTION 16: SPECIAL CASES A THROUGH N
    # ──────────────────────────────────────────────────────────────────────

    # Case A: Valid Gmail business mailbox
    def test_case_a_valid_gmail_business_mailbox(self):
        """A business legitimately using Gmail is classified as GENERIC_MAILBOX_EMAIL with VALID MX."""
        res = EmailVerifier.verify("restaurant.booking@gmail.com", source="OFFICIAL_WEBSITE")
        self.assertEqual(res.business_domain_type, BusinessDomainType.GENERIC_MAILBOX_EMAIL.value)
        self.assertEqual(res.domain, "gmail.com")
        self.assertIn(res.mx_status, [MXStatus.VALID.value, MXStatus.UNKNOWN.value])
        # Verify candidate object preserves classification
        cand = EmailCandidate(
            email="restaurant.booking@gmail.com",
            source="OFFICIAL_WEBSITE",
            verification_status=EmailVerificationStatus.VERIFIED.value,
            mx_status=MXStatus.VALID.value,
            business_domain_type=res.business_domain_type,
            domain="gmail.com",
        )
        self.assertEqual(cand.business_domain_type, BusinessDomainType.GENERIC_MAILBOX_EMAIL.value)

    # Case B: Invalid email syntax
    def test_case_b_invalid_email_syntax(self):
        """Invalid email syntax returns INVALID status and MXStatus.INVALID."""
        res = EmailVerifier.verify("not-an-email", source="OFFICIAL_WEBSITE")
        self.assertEqual(res.verification_status, EmailVerificationStatus.INVALID.value)
        self.assertEqual(res.mx_status, MXStatus.INVALID.value)

        res2 = EmailVerifier.verify("bad@@domain.com", source="OFFICIAL_WEBSITE")
        self.assertEqual(res2.verification_status, EmailVerificationStatus.INVALID.value)
        self.assertEqual(res2.mx_status, MXStatus.INVALID.value)

    # Case C: Valid syntax but no MX (NXDOMAIN or NoAnswer)
    def test_case_c_valid_syntax_no_mx(self):
        """Domain with no MX records returns NO_MX (not UNKNOWN)."""
        with patch("dns.resolver.resolve") as mock_resolve:
            mock_resolve.side_effect = dns.resolver.NoAnswer()
            res = EmailVerifier.verify("info@domain-with-no-mx-records.co.uk", source="OFFICIAL_WEBSITE")
            self.assertEqual(res.mx_status, MXStatus.NO_MX.value)
            self.assertEqual(res.verification_status, EmailVerificationStatus.INVALID.value)
            self.assertIn("NoAnswer", res.reason)

    # Case D: MX lookup unavailable / DNS timeout
    def test_case_d_mx_lookup_unavailable_timeout(self):
        """DNS timeout returns MXStatus.UNKNOWN and does NOT convert to NO_MX."""
        with patch("dns.resolver.resolve") as mock_resolve:
            mock_resolve.side_effect = dns.resolver.Timeout("DNS server timed out")
            res = EmailVerifier.verify("contact@slow-dns-server.co.uk", source="OFFICIAL_WEBSITE")
            self.assertEqual(res.mx_status, MXStatus.UNKNOWN.value)
            self.assertNotEqual(res.mx_status, MXStatus.NO_MX.value)
            self.assertIn("DNS_MX_TIMEOUT", res.method)
            self.assertIn("could not check MX", res.reason)

    # Case E: Business Instagram URL without IGSID
    def test_case_e_business_instagram_without_igsid(self):
        """Instagram profile found & owned, but no numeric IGSID -> manual_contactable=True, sendable=False."""
        lead = {
            "lead_id": "LEAD-IG-001",
            "company_name": "Artisan Bakery",
            "instagram_url": "https://www.instagram.com/artisanbakery_mcr/",
            "instagram_ownership_status": "VERIFIED",
            "qualification_state": QualificationState.OUTREACH_READY.value,
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        ig = assessment.channels["Instagram Direct Message"]
        self.assertTrue(ig.business_owned)
        self.assertTrue(ig.profile_found)
        self.assertTrue(ig.profile_accessible)
        self.assertFalse(ig.recipient_id_available)
        self.assertTrue(ig.manual_contactable)
        self.assertFalse(ig.automated_contactable)
        self.assertFalse(ig.sendable)
        self.assertEqual(ig.status, ChannelStatus.AVAILABLE.value)
        self.assertIn("RECIPIENT_ID_REQUIRED", ig.reason)

    # Case F: Business Instagram URL with verified real recipient ID
    def test_case_f_business_instagram_with_real_recipient_id(self):
        """Instagram profile with verified numeric IGSID and integration -> sendable=True."""
        lead = {
            "lead_id": "LEAD-IG-002",
            "company_name": "Artisan Bakery",
            "instagram_url": "https://www.instagram.com/artisanbakery_mcr/",
            "instagram_ownership_status": "VERIFIED",
            "meta_instagram_id": "17841400123456789",  # Real numeric IGSID format
            "qualification_state": QualificationState.OUTREACH_READY.value,
        }
        with patch.dict(os.environ, {"INSTAGRAM_GRAPH_TOKEN": "mock_valid_token"}):
            assessment = ContactabilityAssessor.assess_lead(lead)
            ig = assessment.channels["Instagram Direct Message"]
            self.assertTrue(ig.recipient_id_available)
            self.assertTrue(ig.automated_contactable)
            self.assertTrue(ig.sendable)
            self.assertEqual(ig.status, ChannelStatus.AVAILABLE.value)
            self.assertIn("Verified IGSID available", ig.reason)

    # Case G: Facebook page without PSID
    def test_case_g_facebook_page_without_psid(self):
        """Facebook page found & owned, but no numeric PSID -> manual_contactable=True, sendable=False."""
        lead = {
            "lead_id": "LEAD-FB-001",
            "company_name": "Corner Cafe",
            "facebook_url": "https://www.facebook.com/CornerCafeLeeds/",
            "facebook_ownership_status": "VERIFIED",
            "qualification_state": QualificationState.OUTREACH_READY.value,
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        fb = assessment.channels["Facebook Messenger"]
        self.assertTrue(fb.business_owned)
        self.assertTrue(fb.profile_found)
        self.assertTrue(fb.profile_accessible)
        self.assertFalse(fb.recipient_id_available)
        self.assertTrue(fb.manual_contactable)
        self.assertFalse(fb.automated_contactable)
        self.assertFalse(fb.sendable)
        self.assertEqual(fb.status, ChannelStatus.AVAILABLE.value)
        self.assertIn("RECIPIENT_ID_REQUIRED", fb.reason)

    # Case H: Facebook page with real supported recipient ID
    def test_case_h_facebook_page_with_real_psid(self):
        """Facebook page with verified numeric PSID and integration -> sendable=True."""
        lead = {
            "lead_id": "LEAD-FB-002",
            "company_name": "Corner Cafe",
            "facebook_url": "https://www.facebook.com/CornerCafeLeeds/",
            "facebook_ownership_status": "VERIFIED",
            "meta_facebook_id": "100063849201948",  # Real numeric PSID format
            "qualification_state": QualificationState.OUTREACH_READY.value,
        }
        with patch.dict(os.environ, {"META_PAGE_ACCESS_TOKEN": "mock_page_token"}):
            assessment = ContactabilityAssessor.assess_lead(lead)
            fb = assessment.channels["Facebook Messenger"]
            self.assertTrue(fb.recipient_id_available)
            self.assertTrue(fb.automated_contactable)
            self.assertTrue(fb.sendable)
            self.assertEqual(fb.status, ChannelStatus.AVAILABLE.value)
            self.assertIn("Verified PSID available", fb.reason)

    # Case I: Valid email + Instagram profile
    def test_case_i_valid_email_plus_instagram_profile(self):
        """Lead with both verified email and Instagram profile is contactable and reports multi-channel availability."""
        lead = {
            "lead_id": "LEAD-MULTI-001",
            "company_name": "Seoul Kimchi",
            "address": "275 Upper Brook St, Manchester M13 0HR",
            "postcode": "M13 0HR",
            "city": "Manchester",
            "email": "seoulkimchi@gmail.com",
            "instagram_url": "https://www.instagram.com/seoulkimchi_mcr/",
            "instagram_ownership_status": "VERIFIED",
            "qualification_state": QualificationState.OUTREACH_READY.value,
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        self.assertTrue(assessment.manual_contactable)
        self.assertEqual(assessment.channels["Instagram Direct Message"].status, ChannelStatus.AVAILABLE.value)
        self.assertTrue(assessment.channels["Instagram Direct Message"].manual_contactable)
        self.assertTrue(assessment.channels["Email"].manual_contactable)
        self.assertIn("Email", assessment.sendable_channels)

    # Case J: Social only, no automation ID
    def test_case_j_social_only_no_automation_id(self):
        """Lead with verified social profile but no numeric IGSID/PSID is PARTIALLY_CONTACTABLE (manual only)."""
        lead = {
            "lead_id": "LEAD-SOC-001",
            "company_name": "Boutique Tapas",
            "instagram_url": "https://www.instagram.com/boutiquetapas/",
            "instagram_ownership_status": "VERIFIED",
            "qualification_state": QualificationState.OUTREACH_READY.value,
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        self.assertTrue(assessment.manual_contactable)
        self.assertFalse(assessment.automated_contactable)
        self.assertEqual(len(assessment.sendable_channels), 0)
        self.assertEqual(assessment.contactability_status, ContactabilityState.PARTIALLY_CONTACTABLE.value)

    # Case K: No channel available
    def test_case_k_no_channel_available(self):
        """Lead with no email and no social profiles is NOT_CONTACTABLE."""
        lead = {
            "lead_id": "LEAD-NOCHAN-001",
            "company_name": "Ghost Kitchen",
            "qualification_state": QualificationState.OUTREACH_READY.value,
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        self.assertFalse(assessment.manual_contactable)
        self.assertFalse(assessment.automated_contactable)
        self.assertEqual(assessment.contactability_status, ContactabilityState.NOT_CONTACTABLE.value)
        self.assertEqual(assessment.channels["Email"].status, ChannelStatus.UNAVAILABLE.value)
        self.assertEqual(assessment.channels["Instagram Direct Message"].status, ChannelStatus.UNAVAILABLE.value)
        self.assertEqual(assessment.channels["Facebook Messenger"].status, ChannelStatus.UNAVAILABLE.value)

    # Case L: Bounced email already suppressed
    def test_case_l_bounced_email_already_suppressed(self):
        """Permanently bounced email (550 5.1.1) is SUPPRESSED, unsendable, but qualification is untouched."""
        lead = {
            "lead_id": "LEAD-BOUNCE-001",
            "company_name": "Hong Thai",
            "email": "hongthai.mcr@gmail.com",
            "bounce_code": "550 5.1.1",
            "email_suppressed": "True",
            "qualification_state": QualificationState.OUTREACH_READY.value,
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        em = assessment.channels["Email"]
        self.assertEqual(em.status, ChannelStatus.SUPPRESSED.value)
        self.assertFalse(em.sendable)
        self.assertFalse(em.automated_contactable)
        self.assertFalse(em.manual_contactable)
        self.assertIn("BOUNCED", em.reason)
        # Qualification must remain OUTREACH_READY
        self.assertEqual(assessment.qualification_state, QualificationState.OUTREACH_READY.value)
        self.assertEqual(lead["qualification_state"], QualificationState.OUTREACH_READY.value)

    # Case M: OUTREACH_READY lead that remains NOT_CONTACTABLE
    def test_case_m_outreach_ready_remains_not_contactable(self):
        """An OUTREACH_READY lead that cannot be contacted does NOT get downgraded to EXCLUDED."""
        lead = {
            "lead_id": "LEAD-MAN-94F2CB",
            "company_name": "Waka Waka MCR",
            "city": "Manchester",
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "operational_status": "ACTIVE_CONFIRMED",
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        self.assertEqual(assessment.qualification_state, QualificationState.OUTREACH_READY.value)
        self.assertEqual(assessment.contactability_status, ContactabilityState.NOT_CONTACTABLE.value)
        self.assertNotEqual(assessment.qualification_state, QualificationState.EXCLUDED.value)
        self.assertEqual(lead["qualification_state"], QualificationState.OUTREACH_READY.value)

    # Case N: MANUAL_REVIEW lead with highly contactable channels
    def test_case_n_manual_review_lead_never_promoted(self):
        """A MANUAL_REVIEW lead with verified email and Instagram profile NEVER gets promoted to OUTREACH_READY."""
        lead = {
            "lead_id": "LEAD-LEEDS-001",
            "company_name": "Casa Di Alessia",
            "city": "Leeds",
            "email": "info@casadialessia.co.uk",
            "instagram_url": "https://www.instagram.com/casadialessia/",
            "instagram_ownership_status": "VERIFIED",
            "qualification_state": QualificationState.MANUAL_REVIEW.value,
            "operational_status": "ACTIVE_CONFIRMED",
        }
        assessment = ContactabilityAssessor.assess_lead(lead)
        # Qualification state MUST remain strictly MANUAL_REVIEW
        self.assertEqual(assessment.qualification_state, QualificationState.MANUAL_REVIEW.value)
        self.assertEqual(lead["qualification_state"], QualificationState.MANUAL_REVIEW.value)
        self.assertNotEqual(assessment.qualification_state, QualificationState.OUTREACH_READY.value)

    # ──────────────────────────────────────────────────────────────────────
    # SECTION 19: ADDITIONAL AUDIT & UNIT TESTS
    # ──────────────────────────────────────────────────────────────────────

    def test_email_normalization_and_whitespace(self):
        """Email normalization strips leading/trailing whitespace and normalizes case."""
        cand = EmailCandidate(
            email="  Info@MyRestaurant.CO.UK  ",
            source="OFFICIAL_WEBSITE"
        )
        self.assertEqual(cand.email, "info@myrestaurant.co.uk")
        self.assertEqual(cand.domain, "myrestaurant.co.uk")

    def test_generic_mailbox_domain_classification(self):
        """Known generic domains (gmail, outlook, yahoo, hotmail) are correctly tagged."""
        for dom in ["gmail.com", "outlook.com", "yahoo.com", "hotmail.co.uk", "icloud.com"]:
            email = f"test@{dom}"
            cand = EmailCandidate(email=email, source="CONTACT_PAGE")
            self.assertEqual(cand.business_domain_type, BusinessDomainType.GENERIC_MAILBOX_EMAIL.value)
            self.assertEqual(cand.domain, dom)

        # Custom business domain
        biz_cand = EmailCandidate(email="bookings@manchesterbistro.co.uk", source="OFFICIAL_WEBSITE")
        self.assertEqual(biz_cand.business_domain_type, BusinessDomainType.BUSINESS_DOMAIN_EMAIL.value)
        self.assertEqual(biz_cand.domain, "manchesterbistro.co.uk")

    def test_compliance_separation_unknown_does_not_become_sendable(self):
        """Valid email with UNKNOWN compliance status does NOT become sendable."""
        lead = {
            "lead_id": "LEAD-COMP-001",
            "company_name": "Unregistered Pop-up",
            "email": "hello@popupbistro.co.uk",
            "qualification_state": QualificationState.OUTREACH_READY.value,
        }
        with patch("lib.country_adapters.uk.UKComplianceAdapter.evaluate_marketing_email") as mock_comp:
            mock_comp.return_value = MagicMock(
                marketing_email_status=MarketingEmailStatus.UNKNOWN,
                compliance_notes="Subscriber type cannot be determined",
                to_dict=lambda: {"marketing_email_status": "UNKNOWN"}
            )
            with patch("lib.outreach.email_enricher.EmailVerifier.verify") as mock_v:
                from lib.outreach.email_enricher import EmailVerificationResult
                mock_v.return_value = EmailVerificationResult(
                    status=EmailVerificationStatus.VERIFIED.value,
                    email_type=EmailType.GENERIC_BUSINESS.value,
                    method="DNS_MX_CHECK",
                    mx_records=["mail.popupbistro.co.uk"],
                    reason="Domain has valid MX",
                    mx_status=MXStatus.VALID.value,
                    domain="popupbistro.co.uk",
                    business_domain_type=BusinessDomainType.BUSINESS_DOMAIN_EMAIL.value
                )
                assessment = ContactabilityAssessor.assess_lead(lead)
                em = assessment.channels["Email"]
                self.assertEqual(em.status, ChannelStatus.AVAILABLE.value)
                self.assertTrue(em.manual_contactable)
                self.assertFalse(em.automated_contactable)
                self.assertFalse(em.sendable)
                self.assertIn("Email", assessment.channels)
                self.assertNotIn("Email", assessment.sendable_channels)

    def test_lead_id_and_crm_fields_untouched(self):
        """Lead dictionary passed to assess_lead has zero mutated fields."""
        original_lead = {
            "lead_id": "LEAD-IMMUTABLE-001",
            "company_name": "Stable Pub",
            "lead_score": 85,
            "priority": "HIGH",
            "review_count": 500,
            "rating": 4.6,
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "operational_status": "ACTIVE_CONFIRMED",
        }
        lead_copy = dict(original_lead)
        assessment = ContactabilityAssessor.assess_lead(lead_copy)
        self.assertEqual(lead_copy, original_lead)


if __name__ == "__main__":
    unittest.main()
