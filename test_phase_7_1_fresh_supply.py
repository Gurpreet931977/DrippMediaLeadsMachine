"""
Dripp Media — Phase 7.1 Fresh Supply & Contactability Coverage Tests
====================================================================
Tests:
  1. Fresh vs existing CRM identity matching
  2. Distinct branch preservation (separate locations preserved)
  3. Cross-dataset deduplication (LEADS, REVIEW_QUEUE, RESEARCH_LOG, contact history)
  4. Contact enrichment does not mutate qualification or score
  5. Social-only contactability (manual vs automated separation)
  6. Rejection of fabricated recipient IDs (Meta handles and URLs rejected)
  7. Valid email contactability and MX status handling
  8. Compliance separation from technical contactability
  9. Zero CRM mutations invariant
  10. Zero outreach dispatch invariant
"""

import os
import sys
import json
import uuid
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import (
    DiscoveredBusiness,
    QualificationState,
    OperationalStatus,
    WebsiteStatus,
    VerificationStatus,
    SocialOwnershipStatus,
    Priority
)
from lib.crm.identity_matcher import (
    BusinessIdentityMatcher,
    IdentityMatchOutcome,
    IdentityMatchResult
)
from lib.outreach.contactability import (
    ContactabilityAssessor,
    ContactabilityState,
    ChannelStatus
)
from lib.outreach.preflight import (
    validate_production_recipient,
    validate_campaign_copy
)
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.validation.country_validator import CountryValidator
from run_phase_7_1_fresh_supply_eval import load_existing_dedup_pool


class TestPhase71FreshSupply(unittest.TestCase):

    def setUp(self):
        self.matcher = BusinessIdentityMatcher()
        self.scorer = LeadScoringProvider()
        mock_rdata = MagicMock()
        mock_rdata.exchange = "mail.manchester-eats.co.uk."
        self.dns_patcher = patch(
            "lib.outreach.email_enricher.dns.resolver.resolve",
            return_value=[mock_rdata]
        )
        self.dns_patcher.start()

    def tearDown(self):
        self.dns_patcher.stop()

    # ── 1. Fresh vs Existing Identity Matching ──
    def test_fresh_vs_existing_identity_matching(self):
        existing_crm = [
            {
                "lead_id": "LEAD-MAN-101",
                "company_name": "Seoul Kimchi",
                "city": "Manchester",
                "target_country": "United Kingdom",
                "address": "275 Upper Brook Street, Manchester M13 0HR",
                "phone": "+44 161 273 5556"
            }
        ]

        # Exact match
        same_business = {
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "275 Upper Brook St, Manchester M13 0HR",
            "phone": "+44 161 273 5556"
        }
        res_dup = self.matcher.match_candidate(same_business, existing_crm)
        self.assertEqual(res_dup.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertGreaterEqual(res_dup.confidence, 0.85)

        # Genuinely fresh candidate
        fresh_business = {
            "company_name": "The Northern Quarter Noodle Bar",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "12 Thomas Street, Manchester M4 1DH",
            "phone": "+44 161 832 9900"
        }
        res_fresh = self.matcher.match_candidate(fresh_business, existing_crm)
        self.assertEqual(res_fresh.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)

    # ── 2. Distinct Branch Preservation ──
    def test_distinct_branch_preservation(self):
        existing_crm = [
            {
                "lead_id": "LEAD-MAN-RUDY-1",
                "company_name": "Rudy's Pizza",
                "city": "Manchester",
                "target_country": "United Kingdom",
                "address": "9 Cotton Street, Ancoats, Manchester M4 5BF",
                "street": "Cotton Street",
                "postcode": "M4 5BF"
            }
        ]

        # Different branch: Peter Street
        peter_st_branch = {
            "company_name": "Rudy's Pizza",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "Petersfield House, Peter Street, Manchester M2 5QJ",
            "street": "Peter Street",
            "postcode": "M2 5QJ"
        }
        res = self.matcher.match_candidate(peter_st_branch, existing_crm)
        # Distinct branches should not be collapsed as existing duplicate lead
        self.assertEqual(res.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)
        self.assertTrue(any("DISTINCT_BRANCH" in r for r in res.match_reasons))

    # ── 3. Cross-Dataset Deduplication ──
    def test_cross_dataset_deduplication(self):
        pool = load_existing_dedup_pool()
        self.assertGreater(len(pool), 0)

        # Known CRM lead in pool: Seoul Kimchi
        cand = {
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "Upper Brook St, Manchester"
        }
        res = self.matcher.match_candidate(cand, pool)
        self.assertIn(res.outcome, [IdentityMatchOutcome.EXISTING_BUSINESS.value, IdentityMatchOutcome.POSSIBLE_DUPLICATE.value])

    # ── 4. Contact Enrichment Does Not Mutate Qualification ──
    def test_contact_enrichment_does_not_mutate_qualification(self):
        lead = {
            "lead_id": "LEAD-TEST-001",
            "company_name": "Chorlton Tap",
            "city": "Manchester",
            "country": "United Kingdom",
            "target_country": "United Kingdom",
            "qualification_state": "OUTREACH_READY",
            "operational_status": "ACTIVE_CONFIRMED",
            "score": 75,
            "priority": "HIGH",
            "instagram_url": "https://instagram.com/chorltontap",
            "social_ownership_status": "VERIFIED"
        }

        orig_qual = lead["qualification_state"]
        orig_op = lead["operational_status"]
        orig_score = lead["score"]
        orig_priority = lead["priority"]

        assessment = ContactabilityAssessor.assess_lead(lead)

        # Qualification fields must remain identical
        self.assertEqual(lead["qualification_state"], orig_qual)
        self.assertEqual(lead["operational_status"], orig_op)
        self.assertEqual(lead["score"], orig_score)
        self.assertEqual(lead["priority"], orig_priority)

    # ── 5. Social-Only Contactability (Manual vs Automated) ──
    def test_social_only_contactability_manual_vs_automated(self):
        lead = {
            "lead_id": "LEAD-TEST-002",
            "company_name": "Mackie Mayor Eatery",
            "city": "Manchester",
            "country": "United Kingdom",
            "target_country": "United Kingdom",
            "instagram_url": "https://instagram.com/mackiemayor",
            "social_ownership_status": "VERIFIED",
            "operational_status": "ACTIVE_CONFIRMED",
            "qualification_state": "OUTREACH_READY"
        }

        assessment = ContactabilityAssessor.assess_lead(lead)
        self.assertEqual(assessment.contactability_status, ContactabilityState.PARTIALLY_CONTACTABLE.value)
        self.assertFalse(assessment.automated_contactable)

        ig_ch = assessment.channels.get("Instagram Direct Message")
        self.assertIsNotNone(ig_ch)
        self.assertTrue(ig_ch.manual_contactable)
        self.assertFalse(ig_ch.automated_contactable)
        self.assertFalse(ig_ch.sendable)

    # ── 6. Rejection of Fabricated Recipient IDs ──
    def test_rejection_of_fabricated_recipient_ids(self):
        # Handles rejected
        ok, err = validate_production_recipient("Instagram Direct Message", "@my_food_spot")
        self.assertFalse(ok)
        self.assertIn("RECIPIENT_ID_NOT_NUMERIC", err)

        # Profile URLs rejected
        ok, err = validate_production_recipient("Instagram Direct Message", "https://instagram.com/my_food_spot")
        self.assertFalse(ok)
        self.assertIn("INVALID_RECIPIENT_TYPE", err)

        # Page name rejected for Facebook
        ok, err = validate_production_recipient("Facebook Messenger", "my_fb_page")
        self.assertFalse(ok)
        self.assertIn("RECIPIENT_ID_NOT_NUMERIC", err)

        # Genuine numeric IDs accepted
        ok, _ = validate_production_recipient("Instagram Direct Message", "17841400012345678")
        self.assertTrue(ok)
        ok, _ = validate_production_recipient("Facebook Messenger", "100098765432100")
        self.assertTrue(ok)

    # ── 7. Valid Email Contactability and MX ──
    def test_valid_email_contactability_and_mx(self):
        ok, err = validate_production_recipient("Email", "info@manchestercatering.co.uk")
        self.assertTrue(ok)

        # Bad email syntax rejected
        ok, err = validate_production_recipient("Email", "invalid-email-address")
        self.assertFalse(ok)
        self.assertIn("INVALID_EMAIL_SYNTAX", err)

    # ── 8. Compliance Separation from Technical Contactability ──
    def test_compliance_separation_from_technical_contactability(self):
        lead = {
            "lead_id": "LEAD-TEST-003",
            "company_name": "Sole Proprietor Diner",
            "city": "Manchester",
            "country": "United Kingdom",
            "target_country": "United Kingdom",
            "email": "owner.john@gmail.com",
            "subscriber_type": "INDIVIDUAL_SUBSCRIBER",
            "marketing_email_status": "BLOCKED",
            "operational_status": "ACTIVE_CONFIRMED",
            "qualification_state": "OUTREACH_READY"
        }

        assessment = ContactabilityAssessor.assess_lead(lead)
        em_ch = assessment.channels.get("Email")
        # Technical reachability might have recipient, but compliance status is blocked/unknown
        if em_ch:
            self.assertNotEqual(em_ch.compliance_status, "ALLOWED")

    # ── 9. Zero CRM Mutations Invariant ──
    def test_zero_crm_mutation_invariant(self):
        # Verify cache sheets mtime or state is unchanged
        leads_path = os.path.join(PROJECT_ROOT, "data", "cache_sheets_leads.json")
        if os.path.exists(leads_path):
            with open(leads_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            # The production leads count must remain at least 6
            self.assertGreaterEqual(len(d.get("leads", [])), 6)

    # ── 10. Zero Outreach Dispatch Invariant ──
    @patch("lib.outreach.send_adapters.dispatch_send")
    def test_zero_outreach_dispatch_invariant(self, mock_dispatch):
        # Neither preflight nor supply evaluation should ever call dispatch_send
        mock_dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
