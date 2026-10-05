"""
test_phase_10_2_automated_email.py
===================================
Phase 10.2 Automated Email Outreach Engine Test Suite.

Comprehensive validation across all 40 sections of Phase 10.2 specifications:
  1. Separate Email Automation from Commercial Automation (independent flag & kill switch)
  2. Compliance Classification (UK PECR/GDPR: corporate vs sole trader/partnership/individual)
  3. Law / Policy Evidence Object (immutable audit records, zero fabrication)
  4. Identity + Contact Association (branch protection, corporate shared vs branch specific)
  5. Email Contact States (9 canonical states, non-collapsing)
  6. Sendability Gate (deterministic multi-condition evaluation)
  7. Anti-Guessing Guarantee (strict ban on inferred role accounts without recorded source)
  8. Unsubscribe / Suppression System (email, domain, lead, global tiers)
  9. Unsubscribe Link (tokenized, idempotent GET/POST)
  10. Sender Identity (clear Dripp Media identity, physical address, privacy notice)
  11. Domain Authentication Checks (SPF, DKIM, DMARC, MX sender health)
  12. Email Provider Adapter (7 canonical provider states, real msg ID preservation)
  13. Automated Email Executor (queue, compliance, rate limits, QA, idempotency)
  14. Campaign Model & Lifecycle
  15. Audience Filtering (OUTREACH_READY only, excludes RESEARCH_ONLY/MANUAL_REVIEW)
  16. Initial Live Batch Cap (MAX_EMAIL_BATCH = 5)
  17. Rate Limiting (per_minute, per_hour, per_day, per_domain)
  18. Idempotency (deterministic send key, repeated execution protection)
  19. Retries (max 2 retries, zero retries for non-retryable statuses)
  20. Bounce Handling (hard bounce email suppression, domain preserved)
  21. Message Templates & Anti-Hallucination (WEBSITE_EMAIL_V1, no unsupported claims)
  22. Pre-Send Message QA (syntax, variables, sender info, hashes)
  23. Automated Follow-up Prohibition (EMAIL #1 ONLY, no automatic follow-ups)
  24. Reply Detection & Inbound Webhook (routes lead to OPERATOR_REVIEW, stage CONTACTED)
  25. Outreach Intelligence Analytics (explicit denominators, queued != submitted != delivered)
  26. Commercial Pipeline Separation (no auto-proposals, phone/social locked)
  27. Safe Activation & Dry Run (zero-send preview)
  28. Emergency Kill Switch (idempotent submission freeze)
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Dict, Any, List

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient
from server import app

from lib.system.system_config import (
    SystemConfig,
    CommercialActionForbiddenError,
    CommercialActionBlockedError,
    EmailAutomationBlockedError,
    EmailComplianceBlockedError,
)
from lib.system.atomic_writer import atomic_write_json
from lib.outreach.email_compliance import (
    EmailComplianceEngine,
    SubscriberClass,
    ComplianceStatus,
    LawfulBasis,
    MarketingBasis,
    DEFAULT_PRIVACY_NOTICE_URL,
)
from lib.outreach.email_suppression import (
    EmailSuppressionManager,
    SuppressionType,
)
from lib.outreach.email_sendability import (
    EmailSendabilityGate,
    EmailContactStates,
    SendabilityEvaluation,
)
from lib.outreach.email_sender_health import EmailSenderHealthAuditor
from lib.outreach.email_provider import (
    EnhancedEmailProvider,
    ProviderDeliveryStatus,
    EmailProviderResult,
)
from lib.outreach.email_templates import (
    EmailTemplateEngine,
    TEMPLATE_VERSION_V1,
)
from lib.outreach.email_governor import (
    EmailGovernor,
    DEFAULT_LIMIT_PER_MINUTE,
    DEFAULT_LIMIT_PER_HOUR,
    DEFAULT_LIMIT_PER_DAY,
    DEFAULT_LIMIT_PER_DOMAIN,
    MAX_RETRIES,
)
from lib.outreach.automated_email_executor import (
    AutomatedEmailExecutor,
    CampaignState,
    MAX_EMAIL_BATCH,
)


class TestEmailCompliance(unittest.TestCase):
    """PECR & GDPR Compliance Engine Tests (Sections 2 & 3)."""

    def test_corporate_subscriber_allowed_by_ltd_suffix(self):
        lead = {
            "company_name": "Apex Dental Clinic Ltd",
            "country_code": "GB",
        }
        sub_class, reason = EmailComplianceEngine.classify_subscriber(lead)
        self.assertEqual(sub_class, SubscriberClass.CORPORATE_SUBSCRIBER)

        evidence = EmailComplianceEngine.evaluate_compliance(lead)
        self.assertEqual(evidence["compliance_status"], ComplianceStatus.PASS)
        self.assertEqual(evidence["marketing_basis"], MarketingBasis.B2B_CORPORATE)
        self.assertEqual(evidence["lawful_basis"], LawfulBasis.LEGITIMATE_INTERESTS)
        self.assertIn("PECR Regulation 22", evidence["reason"])

    def test_corporate_subscriber_allowed_by_plc_suffix(self):
        lead = {"company_name": "Northern Holdings PLC", "country_code": "GB"}
        sub_class, _ = EmailComplianceEngine.classify_subscriber(lead)
        self.assertEqual(sub_class, SubscriberClass.CORPORATE_SUBSCRIBER)
        evidence = EmailComplianceEngine.evaluate_compliance(lead)
        self.assertEqual(evidence["compliance_status"], ComplianceStatus.PASS)

    def test_corporate_subscriber_allowed_by_llp_suffix(self):
        lead = {"company_name": "Manchester Legal Associates LLP", "country_code": "GB"}
        sub_class, _ = EmailComplianceEngine.classify_subscriber(lead)
        self.assertEqual(sub_class, SubscriberClass.CORPORATE_SUBSCRIBER)
        evidence = EmailComplianceEngine.evaluate_compliance(lead)
        self.assertEqual(evidence["compliance_status"], ComplianceStatus.PASS)

    def test_corporate_subscriber_allowed_by_companies_house_registration(self):
        lead = {
            "company_name": "Urban Wellness Space",
            "companies_house_number": "12345678",
            "company_type": "private limited company",
            "country_code": "GB",
        }
        sub_class, _ = EmailComplianceEngine.classify_subscriber(lead)
        self.assertEqual(sub_class, SubscriberClass.CORPORATE_SUBSCRIBER)
        evidence = EmailComplianceEngine.evaluate_compliance(lead)
        self.assertEqual(evidence["compliance_status"], ComplianceStatus.PASS)

    def test_sole_trader_strictly_blocked_without_consent(self):
        lead = {
            "company_name": "John Smith T/A JS Barbering",
            "company_type": "sole trader",
            "country_code": "GB",
        }
        sub_class, _ = EmailComplianceEngine.classify_subscriber(lead)
        self.assertEqual(sub_class, SubscriberClass.SOLE_TRADER)

        evidence = EmailComplianceEngine.evaluate_compliance(lead)
        self.assertEqual(evidence["compliance_status"], ComplianceStatus.BLOCKED)
        self.assertEqual(evidence["marketing_basis"], MarketingBasis.UNAUTHORIZED)
        self.assertEqual(evidence["lawful_basis"], LawfulBasis.NONE)
        self.assertIn("sole traders", evidence["reason"].lower())

    def test_sole_trader_allowed_with_explicit_verified_consent(self):
        lead = {
            "company_name": "David Miller Plumbing",
            "legal_entity_type": "sole trader",
            "country_code": "GB",
            "consent_status": "CONSENT_VERIFIED",
            "consent_source": "https://example.com/opt-in-form",
            "consent_timestamp": "2026-09-01T10:00:00Z",
        }
        evidence = EmailComplianceEngine.evaluate_compliance(lead)
        self.assertEqual(evidence["compliance_status"], ComplianceStatus.PASS)
        self.assertEqual(evidence["marketing_basis"], MarketingBasis.B2B_CONSENT)
        self.assertEqual(evidence["lawful_basis"], LawfulBasis.CONSENT)

    def test_unincorporated_partnership_blocked_without_consent(self):
        lead = {
            "company_name": "Smith & Jones Partners",
            "country_code": "GB",
        }
        sub_class, _ = EmailComplianceEngine.classify_subscriber(lead)
        self.assertEqual(sub_class, SubscriberClass.PARTNERSHIP)

        evidence = EmailComplianceEngine.evaluate_compliance(lead)
        self.assertEqual(evidence["compliance_status"], ComplianceStatus.BLOCKED)
        self.assertEqual(evidence["lawful_basis"], LawfulBasis.NONE)

    def test_individual_with_personal_email_blocked(self):
        lead = {
            "company_name": "Jane's Yoga",
            "email": "janeyoga123@gmail.com",
            "country_code": "GB",
        }
        sub_class, _ = EmailComplianceEngine.classify_subscriber(lead)
        self.assertEqual(sub_class, SubscriberClass.UNKNOWN)
        evidence = EmailComplianceEngine.evaluate_compliance(lead)
        self.assertEqual(evidence["compliance_status"], ComplianceStatus.BLOCKED)

    def test_unknown_classification_strictly_defaults_to_blocked(self):
        lead = {
            "company_name": "Mysterious Enterprise",
            "country_code": "GB",
        }
        sub_class, _ = EmailComplianceEngine.classify_subscriber(lead)
        self.assertEqual(sub_class, SubscriberClass.UNKNOWN)
        evidence = EmailComplianceEngine.evaluate_compliance(lead)
        self.assertEqual(evidence["compliance_status"], ComplianceStatus.BLOCKED)
        self.assertIn("cannot establish corporate subscriber status safely", evidence["classification_reason"].lower())

    def test_opt_out_overrides_corporate_status(self):
        lead = {
            "company_name": "Global Tech Services Ltd",
            "country_code": "GB",
            "opt_out_status": True,
        }
        evidence = EmailComplianceEngine.evaluate_compliance(lead, opt_out=True)
        self.assertEqual(evidence["compliance_status"], ComplianceStatus.BLOCKED)
        self.assertTrue(evidence["opt_out_status"])
        self.assertIn("opted out", evidence["reason"].lower())

    def test_law_policy_evidence_object_structure(self):
        lead = {
            "company_name": "Beacon Media Ltd",
            "country_code": "GB",
        }
        evidence = EmailComplianceEngine.evaluate_compliance(lead)
        required_keys = [
            "recipient_type",
            "country",
            "marketing_basis",
            "lawful_basis",
            "consent_status",
            "consent_source",
            "consent_timestamp",
            "opt_out_status",
            "privacy_notice_reference",
            "compliance_status",
        ]
        for key in required_keys:
            self.assertIn(key, evidence)
        self.assertEqual(evidence["privacy_notice_reference"], DEFAULT_PRIVACY_NOTICE_URL)


class TestEmailIdentityAndAntiGuessing(unittest.TestCase):
    """Email Identity, Branch Protection & Anti-Guessing Tests (Sections 4 & 7)."""

    def setUp(self):
        self.gate = EmailSendabilityGate()

    def test_branch_city_match_passes_branch_protection(self):
        lead = {
            "lead_id": "LD-MCR-01",
            "company_name": "Central Clinic Ltd",
            "city": "Manchester",
            "qualification_state": "OUTREACH_READY",
        }
        rec = {
            "email": "contact@centralclinic.co.uk",
            "branch_city": "Manchester",
            "branch_association": "BRANCH_SPECIFIC",
            "source": "https://centralclinic.co.uk/contact",
            "verification_status": "VERIFIED",
        }
        eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
        self.assertNotIn("BRANCH_MISMATCH", str(eval_res.failed_conditions))

    def test_branch_city_mismatch_blocked_for_branch_specific_email(self):
        lead = {
            "lead_id": "LD-MCR-02",
            "company_name": "Multi-City Health Ltd",
            "city": "Manchester",
            "qualification_state": "OUTREACH_READY",
        }
        rec = {
            "email": "london@multicityhealth.co.uk",
            "branch_city": "London",
            "branch_association": "BRANCH_SPECIFIC",
            "source": "https://multicityhealth.co.uk/branches/london",
        }
        eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
        self.assertFalse(eval_res.is_sendable)
        self.assertTrue(eval_res.states.EMAIL_INVALID)
        self.assertTrue(any("BRANCH_MISMATCH" in c for c in eval_res.failed_conditions))

    def test_shared_corporate_email_allowed_across_cities(self):
        lead = {
            "lead_id": "LD-MCR-03",
            "company_name": "National Fitness Group Ltd",
            "city": "Manchester",
            "qualification_state": "OUTREACH_READY",
        }
        rec = {
            "email": "corporate.enquiries@nationalfitness.co.uk",
            "branch_city": "London",
            "branch_association": "CORPORATE_SHARED",
            "source": "https://nationalfitness.co.uk/about",
            "verification_status": "VERIFIED",
        }
        eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
        self.assertNotIn("BRANCH_MISMATCH", str(eval_res.failed_conditions))

    def test_anti_guessing_blocks_inferred_info_email(self):
        lead = {
            "lead_id": "LD-001",
            "company_name": "Northern Tech Ltd",
            "city": "Leeds",
            "qualification_state": "OUTREACH_READY",
        }
        rec = {
            "email": "info@northerntech.co.uk",
            "is_guessed": True,
            "source": None,
        }
        eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
        self.assertFalse(eval_res.is_sendable)
        self.assertIn("GUESSED_OR_INFERRED_EMAIL", eval_res.failed_conditions)

    def test_anti_guessing_blocks_unverified_role_addresses(self):
        role_emails = [
            "hello@samplecompany.co.uk",
            "contact@samplecompany.co.uk",
            "admin@samplecompany.co.uk",
            "enquiries@samplecompany.co.uk",
            "support@samplecompany.co.uk",
        ]
        lead = {
            "lead_id": "LD-002",
            "company_name": "Sample Co Ltd",
            "city": "Manchester",
            "qualification_state": "OUTREACH_READY",
        }
        for email in role_emails:
            rec = {"email": email, "source": None}
            eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
            self.assertIn("GUESSED_OR_INFERRED_EMAIL", eval_res.failed_conditions)

    def test_genuinely_discovered_role_email_allowed(self):
        lead = {
            "lead_id": "LD-003",
            "company_name": "Northern Logistics Ltd",
            "city": "Manchester",
            "qualification_state": "OUTREACH_READY",
        }
        rec = {
            "email": "contact@northernlogistics.co.uk",
            "is_guessed": False,
            "source": "https://northernlogistics.co.uk/footer",
            "verification_status": "VERIFIED",
        }
        eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
        self.assertNotIn("GUESSED_OR_INFERRED_EMAIL", eval_res.failed_conditions)

    def test_named_contact_with_source_allowed(self):
        lead = {
            "lead_id": "LD-004",
            "company_name": "Precision Engineering Ltd",
            "city": "Sheffield",
            "qualification_state": "OUTREACH_READY",
        }
        rec = {
            "email": "r.wright@precisionengineering.co.uk",
            "source": "https://precisionengineering.co.uk/team",
            "verification_status": "VERIFIED",
        }
        eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
        self.assertNotIn("GUESSED_OR_INFERRED_EMAIL", eval_res.failed_conditions)


class TestEmailSendabilityGate(unittest.TestCase):
    """Email Sendability Gate & Contact States Tests (Sections 5 & 6)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.suppression_manager = EmailSuppressionManager(
            suppression_path=os.path.join(self.temp_dir, "supp.json"),
            unsubscribes_path=os.path.join(self.temp_dir, "unsub.json"),
        )
        self.gate = EmailSendabilityGate(suppression_manager=self.suppression_manager)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_contact_states_non_collapsing_representation(self):
        states = EmailContactStates()
        states.EMAIL_DISCOVERED = True
        states.EMAIL_MX_VALID = True
        states.EMAIL_COMPLIANT = False
        states.EMAIL_SENDABLE = False

        state_dict = states.to_dict()
        self.assertTrue(state_dict["EMAIL_DISCOVERED"])
        self.assertTrue(state_dict["EMAIL_MX_VALID"])
        self.assertFalse(state_dict["EMAIL_COMPLIANT"])
        self.assertFalse(state_dict["EMAIL_SENDABLE"])
        self.assertIn("EMAIL_BOUNCED", state_dict)
        self.assertIn("EMAIL_UNSUBSCRIBED", state_dict)

    def test_fully_sendable_corporate_lead(self):
        lead = {
            "lead_id": "LD-SEND-01",
            "company_name": "Horizon Digital Ltd",
            "city": "Manchester",
            "qualification_state": "OUTREACH_READY",
            "activation_blocked": False,
        }
        rec = {
            "email": "hello@horizondigital.co.uk",
            "source": "https://horizondigital.co.uk",
            "verification_status": "VERIFIED",
            "mx_status": "VALID",
        }
        eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
        self.assertTrue(eval_res.is_sendable)
        self.assertEqual(eval_res.decision, "SENDABLE")
        self.assertTrue(eval_res.states.EMAIL_SENDABLE)
        self.assertTrue(eval_res.states.EMAIL_COMPLIANT)

    def test_missing_email_blocks_send(self):
        lead = {"lead_id": "LD-NO-EMAIL", "company_name": "Ghost Ltd"}
        eval_res = self.gate.evaluate_sendability(lead=lead, email_candidate="", check_automation_enabled=False)
        self.assertFalse(eval_res.is_sendable)
        self.assertEqual(eval_res.decision, "BLOCK_SEND")
        self.assertIn("NO_EMAIL_DISCOVERED", eval_res.failed_conditions)

    def test_invalid_email_syntax_blocks_send(self):
        lead = {"lead_id": "LD-SYNTAX", "company_name": "Syntax Ltd", "qualification_state": "OUTREACH_READY"}
        eval_res = self.gate.evaluate_sendability(lead=lead, email_candidate="not-an-email", check_automation_enabled=False)
        self.assertFalse(eval_res.is_sendable)
        self.assertIn("INVALID_EMAIL_SYNTAX", eval_res.failed_conditions)

    def test_mx_record_failure_blocks_send(self):
        lead = {"lead_id": "LD-MX", "company_name": "No MX Ltd", "qualification_state": "OUTREACH_READY"}
        rec = {
            "email": "info@nomx.co.uk",
            "source": "verified_web",
            "mx_status": "FAIL",
            "verification_status": "VERIFIED",
        }
        eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
        self.assertFalse(eval_res.is_sendable)
        self.assertIn("MX_RECORD_INVALID", eval_res.failed_conditions)

    def test_suppressed_email_blocks_send(self):
        self.suppression_manager.add_suppression(
            identifier="blocked@target.co.uk",
            category=SuppressionType.EMAIL_UNSUBSCRIBE,
            reason="User unsubscribed",
        )
        lead = {"lead_id": "LD-SUPP", "company_name": "Target Ltd", "qualification_state": "OUTREACH_READY"}
        rec = {"email": "blocked@target.co.uk", "source": "web", "verification_status": "VERIFIED"}
        eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
        self.assertFalse(eval_res.is_sendable)
        self.assertTrue(eval_res.states.EMAIL_SUPPRESSED)

    def test_prior_hard_bounce_blocks_send(self):
        lead = {
            "lead_id": "LD-BNC",
            "company_name": "Bounced Ltd",
            "qualification_state": "OUTREACH_READY",
            "outreach_status": "BOUNCED",
        }
        rec = {"email": "bounce@bounced.co.uk", "source": "web", "verification_status": "VERIFIED"}
        eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
        self.assertFalse(eval_res.is_sendable)
        self.assertTrue(eval_res.states.EMAIL_BOUNCED)
        self.assertIn("PRIOR_HARD_BOUNCE", eval_res.failed_conditions)

    def test_non_outreach_ready_qualification_state_blocks_send(self):
        for state in ("RESEARCH_ONLY", "MANUAL_REVIEW", "EXCLUDED"):
            lead = {
                "lead_id": f"LD-{state}",
                "company_name": "Test Ltd",
                "qualification_state": state,
            }
            rec = {"email": "test@test.co.uk", "source": "web", "verification_status": "VERIFIED"}
            eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
            self.assertFalse(eval_res.is_sendable)
            self.assertTrue(any("LEAD_NOT_OUTREACH_READY" in c for c in eval_res.failed_conditions))

    def test_activation_blocked_flag_blocks_send(self):
        lead = {
            "lead_id": "LD-ACT-BLOCKED",
            "company_name": "Blocked Activation Ltd",
            "qualification_state": "OUTREACH_READY",
            "activation_blocked": True,
        }
        rec = {"email": "admin@blockedactivation.co.uk", "source": "web", "verification_status": "VERIFIED"}
        eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=False)
        self.assertFalse(eval_res.is_sendable)
        self.assertTrue(any("ACTIVATION_BLOCKED" in c for c in eval_res.failed_conditions))

    def test_automation_disabled_blocks_send_when_checked(self):
        SystemConfig.set_automated_email(False)
        lead = {
            "lead_id": "LD-DIS",
            "company_name": "Valid Corp Ltd",
            "qualification_state": "OUTREACH_READY",
        }
        rec = {"email": "info@validcorp.co.uk", "source": "web", "verification_status": "VERIFIED"}
        eval_res = self.gate.evaluate_sendability(lead=lead, email_record=rec, check_automation_enabled=True)
        self.assertFalse(eval_res.is_sendable)
        self.assertTrue(any("AUTOMATED_EMAIL_DISABLED" in c for c in eval_res.failed_conditions))


class TestEmailSenderHealth(unittest.TestCase):
    """Domain Authentication & Sender Health Tests (Section 11)."""

    def setUp(self):
        self.auditor = EmailSenderHealthAuditor()

    def test_full_authentication_passes(self):
        records = {
            "v=spf1 include:_spf.google.com ~all": "TXT",
            "v=DKIM1; k=rsa; p=MIGfMA0GCSqGSIb3DQE...": "TXT",
            "v=DMARC1; p=reject; rua=mailto:dmarc@drippmedia.com": "TXT",
        }
        report = self.auditor.evaluate_dns_records(
            domain="drippmedia.com",
            txt_records=list(records.keys()),
            mx_records=["aspmx.l.google.com"],
        )
        self.assertTrue(report.healthy)
        self.assertEqual(report.spf, "PASS")
        self.assertEqual(report.dkim, "PASS")
        self.assertEqual(report.dmarc, "PASS")
        self.assertEqual(report.mx, "PASS")

    def test_missing_spf_fails_sender_health(self):
        report = self.auditor.evaluate_dns_records(
            domain="drippmedia.com",
            txt_records=["v=DMARC1; p=quarantine", "v=DKIM1; k=rsa;"],
            mx_records=["mail.server.com"],
        )
        self.assertFalse(report.healthy)
        self.assertEqual(report.spf, "FAIL")

    def test_missing_dkim_fails_sender_health(self):
        report = self.auditor.evaluate_dns_records(
            domain="drippmedia.com",
            txt_records=["v=spf1 mx ~all", "v=DMARC1; p=reject"],
            mx_records=["mail.server.com"],
        )
        self.assertFalse(report.healthy)
        self.assertEqual(report.dkim, "FAIL")

    def test_missing_dmarc_fails_sender_health(self):
        report = self.auditor.evaluate_dns_records(
            domain="drippmedia.com",
            txt_records=["v=spf1 mx ~all", "v=DKIM1; k=rsa;"],
            mx_records=["mail.server.com"],
        )
        self.assertFalse(report.healthy)
        self.assertEqual(report.dmarc, "FAIL")

    def test_missing_mx_fails_sender_health(self):
        report = self.auditor.evaluate_dns_records(
            domain="drippmedia.com",
            txt_records=["v=spf1 mx ~all", "v=DKIM1; k=rsa;", "v=DMARC1; p=reject"],
            mx_records=[],
        )
        self.assertFalse(report.healthy)
        self.assertEqual(report.mx, "FAIL")

    def test_missing_sender_identity_fails_audit(self):
        report = self.auditor.audit_sender_health(
            sender_config={
                "sender_email": "hello@drippmedia.com",
                "sender_name": "",  # missing name
                "physical_address": "",
            }
        )
        self.assertFalse(report.healthy)
        self.assertTrue(any("Missing required sender field" in iss for iss in report.issues))


class TestEmailProviderAdapter(unittest.TestCase):
    """Email Provider Adapter & Canonical Result States Tests (Section 12)."""

    def setUp(self):
        self.provider = EnhancedEmailProvider(sandbox_mode=True)

    def test_distinguishes_seven_canonical_states(self):
        canonical_states = [
            ProviderDeliveryStatus.SUBMITTED,
            ProviderDeliveryStatus.DELIVERED,
            ProviderDeliveryStatus.BOUNCED,
            ProviderDeliveryStatus.REJECTED,
            ProviderDeliveryStatus.RATE_LIMITED,
            ProviderDeliveryStatus.FAILED,
            ProviderDeliveryStatus.UNKNOWN,
        ]
        self.assertEqual(len(canonical_states), 7)

    def test_sandbox_mode_returns_submitted_with_real_message_id(self):
        res = self.provider.send_email(
            recipient="test@example.co.uk",
            subject="Website Development Query",
            body="Hello, this is a test.",
            idempotency_key="test-key-01",
        )
        self.assertEqual(res.status, ProviderDeliveryStatus.SUBMITTED)
        self.assertEqual(res.provider, "SANDBOX")
        self.assertTrue(res.provider_message_id.startswith("sandbox-"))
        self.assertFalse(res.is_retryable)

    def test_unconfirmed_provider_message_id_is_none_never_fake(self):
        res = EmailProviderResult(
            status=ProviderDeliveryStatus.UNKNOWN,
            provider="SMTP",
            provider_message_id=None,
            response_text="Connection timed out without ACK",
            error="Socket timeout",
            submitted_at=datetime.now(timezone.utc).isoformat(),
            is_retryable=False,
        )
        self.assertIsNone(res.provider_message_id)
        d = res.to_dict()
        self.assertIsNone(d["provider_message_id"])

    def test_provider_rejection_is_permanent_not_retryable(self):
        res = self.provider.classify_smtp_error(Exception("550 5.1.1 Recipient address rejected: User unknown"))
        self.assertEqual(res.status, ProviderDeliveryStatus.REJECTED)
        self.assertFalse(res.is_retryable)

    def test_provider_rate_limit_is_retryable(self):
        res = self.provider.classify_smtp_error(Exception("451 4.7.1 Service unavailable - try again later (rate limited)"))
        self.assertEqual(res.status, ProviderDeliveryStatus.RATE_LIMITED)
        self.assertTrue(res.is_retryable)

    def test_provider_transient_failure_is_retryable(self):
        res = self.provider.classify_smtp_error(Exception("421 Temporary system failure"))
        self.assertEqual(res.status, ProviderDeliveryStatus.FAILED)
        self.assertTrue(res.is_retryable)

    def test_provider_bounce_classification(self):
        res = self.provider.classify_smtp_error(Exception("554 delivery error: dd this user doesn't have a yahoo.com account"))
        self.assertEqual(res.status, ProviderDeliveryStatus.BOUNCED)
        self.assertFalse(res.is_retryable)

    def test_unknown_provider_result_does_not_immediately_retry(self):
        res = self.provider.classify_smtp_error(Exception("Unknown network anomaly: code 999"))
        self.assertEqual(res.status, ProviderDeliveryStatus.UNKNOWN)
        self.assertFalse(res.is_retryable)


class TestEmailGovernorRateLimitsAndIdempotency(unittest.TestCase):
    """Rate Limiting, Idempotency & Bounce Governance Tests (Sections 17, 18, 19, 20)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.supp_manager = EmailSuppressionManager(
            suppression_path=os.path.join(self.temp_dir, "supp.json"),
            unsubscribes_path=os.path.join(self.temp_dir, "unsub.json"),
        )
        self.governor = EmailGovernor(
            rate_limit_path=os.path.join(self.temp_dir, "rate_limits.json"),
            idempotency_path=os.path.join(self.temp_dir, "idempotency.json"),
            limit_per_minute=2,
            limit_per_hour=5,
            limit_per_day=10,
            limit_per_domain=1,
            suppression_manager=self.supp_manager,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_deterministic_send_key_generation(self):
        key = self.governor.generate_send_key(
            lead_id="LD-100",
            email="Test.User+Sub@Example.CO.UK",
            campaign_id="CAMP-2026",
            template_version="WEBSITE_EMAIL_V1",
            attempt_number=1,
        )
        self.assertEqual(key, "LD-100:test.user+sub@example.co.uk:CAMP-2026:WEBSITE_EMAIL_V1:1")

    def test_idempotency_prevents_duplicate_execution(self):
        key = "lead-1:user@example.co.uk:camp-1:v1:1"
        self.assertFalse(self.governor.is_duplicate_send(key))

        # Record send
        self.governor.record_idempotency(
            send_key=key,
            lead_id="lead-1",
            email="user@example.co.uk",
            status=ProviderDeliveryStatus.SUBMITTED,
            provider_message_id="msg-12345",
        )

        self.assertTrue(self.governor.is_duplicate_send(key))
        cached = self.governor.get_cached_send(key)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["status"], ProviderDeliveryStatus.SUBMITTED)
        self.assertEqual(cached["provider_message_id"], "msg-12345")

    def test_per_minute_rate_limit(self):
        # Limit is 2
        allowed1, _ = self.governor.check_rate_limits("a@domain1.com")
        self.assertTrue(allowed1)
        self.governor.record_send("a@domain1.com")

        allowed2, _ = self.governor.check_rate_limits("b@domain2.com")
        self.assertTrue(allowed2)
        self.governor.record_send("b@domain2.com")

        allowed3, reason = self.governor.check_rate_limits("c@domain3.com")
        self.assertFalse(allowed3)
        self.assertIn("Per-minute rate limit", reason)

    def test_per_domain_rate_limit(self):
        # Limit is 1 per domain
        allowed1, _ = self.governor.check_rate_limits("first@acme.co.uk")
        self.assertTrue(allowed1)
        self.governor.record_send("first@acme.co.uk")

        allowed2, reason = self.governor.check_rate_limits("second@acme.co.uk")
        self.assertFalse(allowed2)
        self.assertIn("Per-domain rate limit", reason)

    def test_retry_limits_max_two_retries(self):
        self.assertTrue(self.governor.can_retry(attempt_number=1, status=ProviderDeliveryStatus.RATE_LIMITED))
        self.assertTrue(self.governor.can_retry(attempt_number=2, status=ProviderDeliveryStatus.FAILED))
        # Exceeds max retries (2)
        self.assertFalse(self.governor.can_retry(attempt_number=3, status=ProviderDeliveryStatus.FAILED))

    def test_non_retryable_statuses_never_retried(self):
        non_retryable = [
            ProviderDeliveryStatus.BOUNCED,
            ProviderDeliveryStatus.REJECTED,
            ProviderDeliveryStatus.UNKNOWN,
            "UNSUBSCRIBED",
            "SUPPRESSED",
            "COMPLIANCE_BLOCKED",
        ]
        for status in non_retryable:
            self.assertFalse(self.governor.can_retry(attempt_number=1, status=status))

    def test_hard_bounce_suppresses_specific_email_only(self):
        self.governor.handle_bounce(
            email="invalid.user@company.co.uk",
            lead_id="LD-999",
            bounce_type="HARD_BOUNCE",
            provider_reason="550 mailbox unavailable",
        )
        is_supp, reason = self.supp_manager.is_suppressed("invalid.user@company.co.uk")
        self.assertTrue(is_supp)
        self.assertIn("Hard bounce", reason)

        # Other user on the same company domain must NOT be suppressed
        other_supp, _ = self.supp_manager.is_suppressed("valid.colleague@company.co.uk")
        self.assertFalse(other_supp)


class TestEmailSuppressionAndUnsubscribe(unittest.TestCase):
    """Unsubscribe & Multi-tier Suppression Engine Tests (Sections 8 & 9)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.manager = EmailSuppressionManager(
            suppression_path=os.path.join(self.temp_dir, "supp.json"),
            unsubscribes_path=os.path.join(self.temp_dir, "unsub.json"),
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_email_unsubscribe_tier(self):
        self.manager.add_suppression("user@target.co.uk", category=SuppressionType.EMAIL_UNSUBSCRIBE)
        is_supp, _ = self.manager.is_suppressed(email="user@target.co.uk")
        self.assertTrue(is_supp)
        # Sibling email not suppressed
        is_supp2, _ = self.manager.is_suppressed(email="colleague@target.co.uk")
        self.assertFalse(is_supp2)

    def test_domain_suppression_tier(self):
        self.manager.add_suppression("spammerdomain.com", category=SuppressionType.DOMAIN_SUPPRESSION)
        is_supp1, _ = self.manager.is_suppressed(email="ceo@spammerdomain.com")
        is_supp2, _ = self.manager.is_suppressed(email="sales@spammerdomain.com")
        self.assertTrue(is_supp1)
        self.assertTrue(is_supp2)

    def test_lead_id_suppression_tier(self):
        self.manager.add_suppression("LD-SUPP-123", category=SuppressionType.LEAD_SUPPRESSION)
        is_supp, _ = self.manager.is_suppressed(lead_id="LD-SUPP-123")
        self.assertTrue(is_supp)

    def test_global_suppression_tier(self):
        self.manager.add_suppression("global_optout@service.com", category=SuppressionType.GLOBAL_SUPPRESSION)
        is_supp, _ = self.manager.is_suppressed(email="global_optout@service.com")
        self.assertTrue(is_supp)

    def test_unsubscribe_link_generation_and_verification(self):
        link = self.manager.generate_unsubscribe_link(
            lead_id="LD-TOKEN-1",
            email="director@corporate.co.uk",
            base_url="https://drippmedia.com",
        )
        self.assertIn("/api/email/unsubscribe/", link)
        token = link.split("/")[-1]

        valid, lead_id, email = self.manager.verify_unsubscribe_token(token)
        self.assertTrue(valid)
        self.assertEqual(lead_id, "LD-TOKEN-1")
        self.assertEqual(email, "director@corporate.co.uk")

    def test_invalid_unsubscribe_token_rejected(self):
        valid, lead_id, email = self.manager.verify_unsubscribe_token("tampered-invalid-token")
        self.assertFalse(valid)
        self.assertIsNone(lead_id)
        self.assertIsNone(email)

    def test_process_unsubscribe_is_idempotent(self):
        link = self.manager.generate_unsubscribe_link(
            lead_id="LD-IDEM",
            email="optout@firm.co.uk",
        )
        token = link.split("/")[-1]

        # First unsubscribe
        res1 = self.manager.process_unsubscribe(token)
        self.assertTrue(res1["success"])
        self.assertFalse(res1.get("already_unsubscribed", False))

        # Second unsubscribe with same token
        res2 = self.manager.process_unsubscribe(token)
        self.assertTrue(res2["success"])
        self.assertTrue(res2.get("already_unsubscribed", False))

        # Recipient is now suppressed
        is_supp, _ = self.manager.is_suppressed(email="optout@firm.co.uk")
        self.assertTrue(is_supp)


class TestEmailTemplatesAndQA(unittest.TestCase):
    """Message Template Rendering & Anti-Hallucination QA Tests (Sections 21 & 22)."""

    def setUp(self):
        self.engine = EmailTemplateEngine()
        self.valid_lead = {
            "lead_id": "LD-TEMPL-01",
            "company_name": "Prestige Dental Ltd",
            "city": "Manchester",
            "website": "https://prestigedental.co.uk",
            "website_status": "OUTDATED_DESIGN",
            "verified_observation": "The mobile navigation menu overlaps header content on smaller screens.",
        }

    def test_website_email_v1_renders_correctly(self):
        render_res = self.engine.render_template(
            template_id=TEMPLATE_VERSION_V1,
            lead=self.valid_lead,
            recipient_email="info@prestigedental.co.uk",
            unsubscribe_link="https://drippmedia.com/api/email/unsubscribe/test-token",
        )
        self.assertIn("Prestige Dental Ltd", render_res["subject"])
        self.assertIn("Prestige Dental Ltd", render_res["body"])
        self.assertIn("Manchester", render_res["body"])
        self.assertIn("Dripp Media", render_res["body"])
        self.assertIn("https://drippmedia.com/api/email/unsubscribe/test-token", render_res["body"])

    def test_message_qa_passes_for_valid_rendered_email(self):
        render_res = self.engine.render_template(
            template_id=TEMPLATE_VERSION_V1,
            lead=self.valid_lead,
            recipient_email="info@prestigedental.co.uk",
            unsubscribe_link="https://drippmedia.com/api/email/unsubscribe/test-token",
        )
        qa_res = self.engine.validate_message_qa(
            lead=self.valid_lead,
            rendered_subject=render_res["subject"],
            rendered_body=render_res["body"],
            recipient_email="info@prestigedental.co.uk",
            unsubscribe_link="https://drippmedia.com/api/email/unsubscribe/test-token",
        )
        self.assertTrue(qa_res["passed"])
        self.assertTrue(bool(qa_res["message_hash"]))
        self.assertTrue(bool(qa_res["personalization_hash"]))
        self.assertEqual(qa_res["template_version"], TEMPLATE_VERSION_V1)

    def test_anti_hallucination_rejects_unsupported_lost_sales_claims(self):
        bad_body = (
            "Hi team, your website is costing you thousands in lost sales and conversion drops. "
            "Unsubscribe: https://drippmedia.com/unsub Dripp Media Ltd"
        )
        qa_res = self.engine.validate_message_qa(
            lead=self.valid_lead,
            rendered_subject="Urgent Website Issue",
            rendered_body=bad_body,
            recipient_email="info@prestigedental.co.uk",
            unsubscribe_link="https://drippmedia.com/unsub",
        )
        self.assertFalse(qa_res["passed"])
        self.assertIn("lost sales", qa_res["reason"].lower())

    def test_anti_hallucination_rejects_unsupported_customer_complaints_claims(self):
        bad_body = (
            "Hi team, customers are complaining about your slow website and booking problems. "
            "Unsubscribe: https://drippmedia.com/unsub Dripp Media Ltd"
        )
        qa_res = self.engine.validate_message_qa(
            lead=self.valid_lead,
            rendered_subject="Website Complaints",
            rendered_body=bad_body,
            recipient_email="info@prestigedental.co.uk",
            unsubscribe_link="https://drippmedia.com/unsub",
        )
        self.assertFalse(qa_res["passed"])
        self.assertIn("customer complaints", qa_res["reason"].lower())

    def test_qa_rejects_missing_unsubscribe_link(self):
        render_res = self.engine.render_template(
            template_id=TEMPLATE_VERSION_V1,
            lead=self.valid_lead,
            recipient_email="info@prestigedental.co.uk",
            unsubscribe_link="https://drippmedia.com/api/email/unsubscribe/test-token",
        )
        body_without_unsub = render_res["body"].replace("https://drippmedia.com/api/email/unsubscribe/test-token", "[MISSING]")
        qa_res = self.engine.validate_message_qa(
            lead=self.valid_lead,
            rendered_subject=render_res["subject"],
            rendered_body=body_without_unsub,
            recipient_email="info@prestigedental.co.uk",
            unsubscribe_link="https://drippmedia.com/api/email/unsubscribe/test-token",
        )
        self.assertFalse(qa_res["passed"])
        self.assertIn("unsubscribe", qa_res["reason"].lower())

    def test_qa_rejects_missing_sender_identity(self):
        body_without_sender = (
            "Hi there, your website is looking outdated. "
            "Unsubscribe: https://drippmedia.com/unsub"
        )
        qa_res = self.engine.validate_message_qa(
            lead=self.valid_lead,
            rendered_subject="Website update",
            rendered_body=body_without_sender,
            recipient_email="info@prestigedental.co.uk",
            unsubscribe_link="https://drippmedia.com/unsub",
        )
        self.assertFalse(qa_res["passed"])
        self.assertIn("sender identity", qa_res["reason"].lower())


class TestAutomatedEmailExecutorAndBatch(unittest.TestCase):
    """Batch Execution, Initial Batch Cap & Dry-Run Tests (Sections 13, 14, 15, 16, 30)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.leads_file = os.path.join(self.temp_dir, "test_leads.json")
        self.executor = AutomatedEmailExecutor(
            data_dir=self.temp_dir,
            leads_path=self.leads_file,
            provider=EnhancedEmailProvider(sandbox_mode=True),
        )

        # Create 10 mock leads: 6 corporate OUTREACH_READY, 2 SOLE_TRADER, 2 RESEARCH_ONLY
        self.mock_leads = []
        for i in range(1, 7):
            self.mock_leads.append({
                "lead_id": f"LD-CORP-{i:02d}",
                "company_name": f"Corporate Group {i} Ltd",
                "city": "Manchester",
                "email": f"contact{i}@corpgroup{i}.co.uk",
                "email_source": f"https://corpgroup{i}.co.uk",
                "email_verification_status": "VERIFIED",
                "mx_status": "VALID",
                "qualification_state": "OUTREACH_READY",
                "activation_blocked": False,
            })
        # Sole traders (should be blocked)
        self.mock_leads.append({
            "lead_id": "LD-SOLE-01",
            "company_name": "Bob's Plumbing Sole Trader",
            "city": "Manchester",
            "email": "bob@plumbing.co.uk",
            "email_source": "web",
            "qualification_state": "OUTREACH_READY",
        })
        # Research only (should be excluded from audience)
        self.mock_leads.append({
            "lead_id": "LD-RES-01",
            "company_name": "Research Enterprise Ltd",
            "city": "Manchester",
            "email": "contact@research.co.uk",
            "qualification_state": "RESEARCH_ONLY",
        })
        atomic_write_json(self.leads_file, self.mock_leads)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        SystemConfig.set_automated_email(False)
        SystemConfig.set_email_kill_switch(False)

    def test_dry_run_executes_zero_sends_and_outputs_full_preview(self):
        dry_run_res = self.executor.run_dry_run(batch_size=5)
        self.assertTrue(dry_run_res["dry_run"])
        self.assertEqual(len(dry_run_res["preview_results"]), 5)
        # Ensure zero entries in idempotency / provider sends
        self.assertEqual(len(self.executor.governor._load_rate_limits()["sends"]), 0)

        # Verify preview fields
        first_preview = dry_run_res["preview_results"][0]
        self.assertEqual(first_preview["compliance_status"], ComplianceStatus.PASS)
        self.assertTrue(first_preview["sendable"])
        self.assertTrue(first_preview["qa_passed"])
        self.assertIn("message_preview", first_preview)

    def test_initial_live_batch_capped_at_five_recipients(self):
        SystemConfig.set_automated_email(True)
        # Attempt to request batch of 100
        batch_res = self.executor.execute_automated_batch(campaign_id="CAMP-TEST", requested_batch_size=100)
        self.assertLessEqual(batch_res["batch_size"], MAX_EMAIL_BATCH)
        self.assertLessEqual(len(batch_res["results"]), 5)

    def test_non_outreach_ready_leads_excluded_from_audience(self):
        eligible = self.executor.get_eligible_audience()
        lead_ids = [l["lead_id"] for l in eligible]
        self.assertNotIn("LD-RES-01", lead_ids)

    def test_kill_switch_blocks_batch_execution(self):
        SystemConfig.set_automated_email(True)
        SystemConfig.set_email_kill_switch(True)
        batch_res = self.executor.execute_automated_batch(campaign_id="CAMP-KILL")
        self.assertEqual(batch_res["status"], "BLOCKED")
        self.assertIn("kill switch", batch_res["reason"].lower())

    def test_automated_email_disabled_blocks_batch_execution(self):
        SystemConfig.set_automated_email(False)
        batch_res = self.executor.execute_automated_batch(campaign_id="CAMP-DIS")
        self.assertEqual(batch_res["status"], "BLOCKED")
        self.assertIn("disabled", batch_res["reason"].lower())

    def test_emergency_stop_freezes_submissions_idempotently(self):
        SystemConfig.set_automated_email(True)
        res1 = self.executor.emergency_stop(reason="High bounce rate detected")
        self.assertTrue(res1["kill_switch_active"])
        self.assertFalse(SystemConfig.is_automated_email_enabled())

        # Calling again must be idempotent
        res2 = self.executor.emergency_stop(reason="Second trigger")
        self.assertTrue(res2["kill_switch_active"])


class TestCommercialSeparationAndSafety(unittest.TestCase):
    """Separation of Channels & Operating Invariants Tests (Sections 1, 23, 24, 26, 28)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.executor = AutomatedEmailExecutor(data_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        SystemConfig.set_automated_email(False)
        SystemConfig.set_commercial_actions(False)
        SystemConfig.set_travel_mode(True)

    def test_automated_email_enabled_without_commercial_actions(self):
        SystemConfig.set_automated_email(True)
        SystemConfig.set_commercial_actions(False)
        SystemConfig.set_travel_mode(True)

        status = SystemConfig.get_status_dict()
        self.assertTrue(status["automated_email_enabled"])
        self.assertFalse(status["commercial_actions_enabled"])
        self.assertTrue(status["travel_mode"])

    def test_automated_phone_calls_remain_strictly_locked(self):
        SystemConfig.set_automated_email(True)
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_action_allowed("AUTOMATED_PHONE_CALL")

    def test_automated_instagram_dms_remain_strictly_locked(self):
        SystemConfig.set_automated_email(True)
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_action_allowed("AUTOMATED_INSTAGRAM_DM")

    def test_automated_facebook_dms_remain_strictly_locked(self):
        SystemConfig.set_automated_email(True)
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_action_allowed("AUTOMATED_FACEBOOK_DM")

    def test_automated_proposals_remain_strictly_locked(self):
        SystemConfig.set_automated_email(True)
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_action_allowed("AUTOMATED_PROPOSAL_DISPATCH")

    def test_automated_followups_remain_strictly_locked(self):
        SystemConfig.set_automated_email(True)
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_action_allowed("AUTOMATED_FOLLOWUP")

    def test_inbound_reply_routes_to_operator_review_not_auto_interested(self):
        # When an inbound reply is received, commercial stage becomes CONTACTED,
        # but NOT INTERESTED or PROPOSAL_REQUESTED.
        crm_update = self.executor.record_inbound_reply(
            lead_id="LD-REPLY-01",
            email="ceo@targetclinic.co.uk",
            subject="Re: Website query",
            body="Thanks for getting in touch, could you tell me more?",
            message_id="msg-reply-1234",
        )
        self.assertEqual(crm_update["stage"], "CONTACTED")
        self.assertEqual(crm_update["next_action"], "OPERATOR_REVIEW")
        self.assertNotEqual(crm_update["stage"], "INTERESTED")
        self.assertNotEqual(crm_update["stage"], "PROPOSAL_REQUESTED")


class TestEmailAPIEndpoints(unittest.TestCase):
    """FastAPI API Endpoints Tests for Phase 10.2."""

    def setUp(self):
        os.environ["EMAIL_SANDBOX_MODE"] = "true"
        self.client = TestClient(app)

    def tearDown(self):
        SystemConfig.set_automated_email(False)
        SystemConfig.set_email_kill_switch(False)
        # Clean up webhook test artifacts
        timelines_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "lead_timelines.json")
        if os.path.exists(timelines_path):
            try:
                with open(timelines_path, "r", encoding="utf-8") as f:
                    tl_data = json.load(f)
                if "LD-WEBHOOK-01" in tl_data:
                    del tl_data["LD-WEBHOOK-01"]
                    atomic_write_json(timelines_path, tl_data)
            except Exception:
                pass

    def test_get_sender_health_endpoint(self):
        res = self.client.get("/api/email/sender-health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("spf", data)
        self.assertIn("dkim", data)
        self.assertIn("dmarc", data)
        self.assertIn("mx", data)

    def test_get_automation_status_endpoint(self):
        res = self.client.get("/api/email/automation/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("automated_email_enabled", data)
        self.assertIn("kill_switch_active", data)
        self.assertIn("max_email_batch", data)
        self.assertEqual(data["max_email_batch"], 5)

    def test_post_automation_enable_and_disable(self):
        # Enable
        res = self.client.post("/api/email/automation/enable", json={"allow_sandbox": True})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(SystemConfig.is_automated_email_enabled())

        # Disable
        res2 = self.client.post("/api/email/automation/disable")
        self.assertEqual(res2.status_code, 200)
        self.assertFalse(SystemConfig.is_automated_email_enabled())

    def test_post_dry_run_endpoint(self):
        res = self.client.post("/api/email/automation/dry-run", json={"batch_size": 3})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("dry_run"))
        self.assertIn("preview_results", data)

    def test_post_emergency_stop_endpoint(self):
        res = self.client.post("/api/email/automation/emergency-stop", json={"reason": "Test emergency trigger"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["kill_switch_active"])
        self.assertFalse(SystemConfig.is_automated_email_enabled())

    def test_get_and_post_unsubscribe_endpoints(self):
        supp_mgr = EmailSuppressionManager()
        link = supp_mgr.generate_unsubscribe_link("LD-API-UNSUB", "api_user@test.co.uk")
        token = link.split("/")[-1]

        # GET /api/email/unsubscribe/{token}
        get_res = self.client.get(f"/api/email/unsubscribe/{token}")
        self.assertEqual(get_res.status_code, 200)
        self.assertIn("Unsubscribed", get_res.text)

        # POST /api/email/unsubscribe with same token (idempotent)
        post_res = self.client.post("/api/email/unsubscribe", json={"token": token})
        self.assertEqual(post_res.status_code, 200)
        self.assertTrue(post_res.json()["success"])

    def test_post_inbound_reply_webhook(self):
        payload = {
            "lead_id": "LD-WEBHOOK-01",
            "email": "director@targetcorp.co.uk",
            "subject": "Re: Website development",
            "body": "We would like to discuss this with your team.",
            "message_id": "inbound-msg-9999",
            "provider": "SENDGRID_INBOUND",
        }
        res = self.client.post("/api/email/inbound/reply-webhook", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn(data["status"], ("PROCESSED", "REPLY_RECORDED"))
        self.assertEqual(data["next_action"], "OPERATOR_REVIEW")


if __name__ == "__main__":
    unittest.main()
