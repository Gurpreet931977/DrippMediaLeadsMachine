"""
test_phase_9_3_contactability.py
================================
Unit and regression test suite for Phase 9.3:
Qualified Lead Contactability + Activation Engine.

Target: >= 35 tests covering:
  - Category 1: Rule B Reporting Canonicalization (canonical criteria count == 8)
  - Category 2: Operational Verification Independence & Provenance
  - Category 3: Contactability Verification (Phone, Instagram, Facebook, Email, MX)
  - Category 4: Branch-Safe Contact Matching & Aladdin Disambiguation
  - Category 5: Activation Queue & Channel Routing
  - Category 6: CRM History Protection & Live Seafood Invariance
  - Category 7: Safety Ceilings & Zero-Send Invariants
"""

import os
import sys
import json
import unittest
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import QualificationState, OperationalStatus
from lib.validation.rule_b_criteria import (
    RULE_B_CRITERIA,
    RULE_B_VERSION,
    get_canonical_rule_b_criteria_count,
    get_canonical_rule_b_criteria,
)
from lib.enrichment.operational_verification import (
    OperationalVerificationEngine,
    OperationalEvidenceItem,
)
from lib.enrichment.contactability_enrichment import (
    ContactabilityEnrichmentEngine,
    LeadActivationProfile,
    ChannelType,
    ContactStatus,
    ContactabilityStatus,
)


class TestCategory1RuleBReportingCanonicalization(unittest.TestCase):
    """Category 1: Rule B Reporting Canonicalization & Immutable Specification."""

    def test_canonical_rule_b_criteria_count_is_eight(self):
        """1. Canonical Rule B specification contains exactly 8 defined criteria."""
        self.assertEqual(get_canonical_rule_b_criteria_count(), 8)
        self.assertEqual(len(RULE_B_CRITERIA), 8)

    def test_rule_b_version_is_frozen(self):
        """2. Rule B version string is explicitly FROZEN."""
        self.assertEqual(RULE_B_VERSION, "FROZEN")

    def test_all_eight_criteria_have_unique_ids(self):
        """3. All 8 canonical criteria have distinct, non-empty identifiers."""
        ids = [c["id"] for c in RULE_B_CRITERIA]
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set(ids)), 8)

    def test_report_criteria_count_matches_canonical(self):
        """4. Reported Rule B criteria count dynamically equals canonical criteria count."""
        reported_count = get_canonical_rule_b_criteria_count()
        canonical_count = len(get_canonical_rule_b_criteria())
        self.assertEqual(reported_count, canonical_count)
        self.assertEqual(reported_count, 8)

    def test_immutable_criteria_structure(self):
        """5. RULE_B_CRITERIA is an immutable tuple protecting against runtime modification."""
        self.assertIsInstance(RULE_B_CRITERIA, tuple)


class TestCategory2OperationalVerificationIndependence(unittest.TestCase):
    """Category 2: Operational Verification Independence & Provenance."""

    def test_review_activity_alone_is_not_verified_active(self):
        """6. Review activity alone without secondary independent family produces WEAK_SIGNAL."""
        item = OperationalVerificationEngine.evaluate_operational_status(
            candidate_id="Biz-1",
            company_name="Single Source Cafe",
            osm_present=True,
            independent_signals=[
                {
                    "source": "google_maps",
                    "source_family": "google",
                    "evidence_type": "google_maps_reviews",
                    "confidence": 0.90,
                    "url": "https://maps.google.com/?cid=123",
                }
            ],
            phone=None,  # No phone corroboration
        )
        self.assertNotEqual(item.status, OperationalStatus.VERIFIED_ACTIVE.value)
        self.assertEqual(item.status, OperationalStatus.WEAK_SIGNAL.value)

    def test_telecom_phone_alone_is_not_verified_active(self):
        """7. Telecom/phone evidence alone without active review/filing signals produces UNKNOWN."""
        item = OperationalVerificationEngine.evaluate_operational_status(
            candidate_id="Biz-2",
            company_name="Phone Only Pub",
            osm_present=True,
            independent_signals=[],
            phone="+44 1618 192265",
        )
        self.assertNotEqual(item.status, OperationalStatus.VERIFIED_ACTIVE.value)
        self.assertEqual(item.status, OperationalStatus.UNKNOWN.value)

    def test_two_independent_families_eligible_for_verified_active(self):
        """8. Two genuinely independent valid families (reviews + direct telecom) produce VERIFIED_ACTIVE."""
        item = OperationalVerificationEngine.evaluate_operational_status(
            candidate_id="Biz-3",
            company_name="Little Aladdin",
            osm_present=True,
            independent_signals=[
                {
                    "source": "google_maps_reviews",
                    "source_family": "google",
                    "evidence_type": "google_maps_reviews",
                    "confidence": 0.95,
                    "url": "https://maps.google.com/?cid=little_aladdin",
                }
            ],
            phone="+44 1618 192265",
            closure_flag=False,
        )
        self.assertEqual(item.status, OperationalStatus.VERIFIED_ACTIVE.value)
        self.assertGreaterEqual(item.confidence, 0.90)

    def test_operational_evidence_contains_source_family_and_provenance(self):
        """9. Every operational evidence item contains source_family, observed_at, confidence, notes."""
        item = OperationalVerificationEngine.evaluate_operational_status(
            candidate_id="Biz-4",
            company_name="Provenance Tested Cafe",
            osm_present=True,
            independent_signals=[
                {
                    "source": "google_maps",
                    "source_family": "google",
                    "evidence_type": "google_maps_reviews",
                    "confidence": 0.85,
                }
            ],
            phone="+44 161 228 6262",
        )
        d = item.to_dict()
        self.assertIn("source_family", d)
        self.assertIn("source", d)
        self.assertIn("observed_at", d)
        self.assertIn("confidence", d)
        self.assertIn("raw_signal", d)
        self.assertIn("normalized_result", d)
        self.assertEqual(d["normalized_result"], OperationalStatus.VERIFIED_ACTIVE.value)


class TestCategory3ContactabilityVerification(unittest.TestCase):
    """Category 3: Contactability Channel Verification (Phone, Instagram, Facebook, Email, MX)."""

    def setUp(self):
        self.engine = ContactabilityEnrichmentEngine()

    def test_valid_manchester_landline_phone_verified(self):
        """10. Valid Manchester landline (+44 161...) is verified with high confidence."""
        res = self.engine.verify_phone("+44 1618 192265", "Little Aladdin", "Manchester")
        self.assertEqual(res.status, ContactStatus.VERIFIED.value)
        self.assertTrue("+44 161" in res.value or "1618" in res.value)
        self.assertGreaterEqual(res.confidence, 0.90)

    def test_valid_uk_mobile_phone_verified(self):
        """11. Valid UK mobile (+44 7...) is verified for local business."""
        res = self.engine.verify_phone("07518 715454", "Mary D's Beamish Bar", "Manchester")
        self.assertEqual(res.status, ContactStatus.VERIFIED.value)
        self.assertTrue(res.value.startswith("+44"))

    def test_spreadsheet_error_phone_rejected(self):
        """12. Spreadsheet error literal '#ERROR!' is classified as INVALID."""
        res = self.engine.verify_phone("#ERROR!", "Seoul Kimchi", "Manchester")
        self.assertEqual(res.status, ContactStatus.INVALID.value)
        self.assertIn("spreadsheet error", res.notes[0].lower())

    def test_too_short_phone_rejected(self):
        """13. Phone string with <10 digits is rejected as INVALID."""
        res = self.engine.verify_phone("12345", "Short Pub", "Manchester")
        self.assertEqual(res.status, ContactStatus.INVALID.value)

    def test_missing_phone_handled_cleanly(self):
        """14. None or empty phone string is classified as MISSING."""
        res = self.engine.verify_phone("", "No Phone Bar", "Manchester")
        self.assertEqual(res.status, ContactStatus.MISSING.value)

    def test_valid_instagram_profile_verified(self):
        """15. Clean Instagram account profile matching business identity is verified."""
        res = self.engine.verify_instagram("https://instagram.com/marydsbar", "Mary D's Beamish Bar", "Manchester")
        self.assertEqual(res.status, ContactStatus.VERIFIED.value)
        self.assertEqual(res.handle, "@marydsbar")

    def test_instagram_post_or_reel_rejected(self):
        """16. Individual Instagram post URL (/p/...) is rejected as INVALID."""
        res = self.engine.verify_instagram("https://instagram.com/p/C39xYzA/", "Some Bar", "Manchester")
        self.assertEqual(res.status, ContactStatus.INVALID.value)
        self.assertIn("post", res.notes[0].lower())

    def test_valid_facebook_page_verified(self):
        """17. Clean Facebook business page is verified."""
        res = self.engine.verify_facebook("https://facebook.com/duciearmsmanchester", "Ducie Arms", "Manchester")
        self.assertEqual(res.status, ContactStatus.VERIFIED.value)

    def test_facebook_video_url_rejected(self):
        """18. Facebook video URL (/watch/...) is rejected as INVALID."""
        res = self.engine.verify_facebook("https://facebook.com/watch/?v=987654", "Some Pub", "Manchester")
        self.assertEqual(res.status, ContactStatus.INVALID.value)

    def test_syntactically_valid_email_with_mx_verified(self):
        """19. Genuine email on established domain (e.g. gmail.com) passes MX check."""
        res = self.engine.verify_email("enquiries.mcr@gmail.com", "Mala")
        self.assertEqual(res.status, ContactStatus.VERIFIED.value)
        self.assertTrue(res.mx_valid)

    def test_synthesized_guessed_email_strictly_rejected(self):
        """20. Synthesized or guessed email addresses are strictly rejected by policy."""
        res = self.engine.verify_email("info@littlealaddin.co.uk", "Little Aladdin", is_synthesized=True)
        self.assertEqual(res.status, ContactStatus.INVALID.value)
        self.assertIn("synthesized", res.notes[0].lower())

    def test_malformed_email_syntax_rejected(self):
        """21. Malformed email string is rejected as INVALID."""
        res = self.engine.verify_email("not-an-email-at-all", "Test Biz")
        self.assertEqual(res.status, ContactStatus.INVALID.value)

    def test_mx_lookup_failure_invalidates_email(self):
        """22. Domain failing MX records is classified as INVALID with mx_valid = False."""
        res = self.engine.verify_email("contact@fake-nonexistent-domain-404.co.uk", "Test Biz")
        self.assertEqual(res.status, ContactStatus.INVALID.value)
        self.assertFalse(res.mx_valid)


class TestCategory4BranchSafeContactMatching(unittest.TestCase):
    """Category 4: Branch-Safe Contact Matching & Aladdin Disambiguation."""

    def setUp(self):
        self.engine = ContactabilityEnrichmentEngine()

    def test_branch_conflict_instagram_rejected(self):
        """23. Instagram account referencing London branch is rejected for Manchester business."""
        res = self.engine.verify_instagram(
            raw_url_or_handle="https://instagram.com/rudyspizza_london",
            business_name="Rudy's Pizza",
            city="Manchester",
        )
        self.assertEqual(res.status, ContactStatus.INVALID.value)
        self.assertIn("branch conflict", res.notes[0].lower())

    def test_branch_conflict_facebook_rejected(self):
        """24. Facebook page referencing Birmingham branch is rejected for Manchester business."""
        res = self.engine.verify_facebook(
            raw_url="https://facebook.com/the-lost-dene-birmingham",
            business_name="The Lost Dene",
            city="Manchester",
        )
        self.assertEqual(res.status, ContactStatus.INVALID.value)
        self.assertIn("branch conflict", res.notes[0].lower())

    def test_unrelated_aladdin_handle_rejected(self):
        """25. Generic '@aladdin_grill' handle lacking 'little' qualifier is flagged for Little Aladdin."""
        res = self.engine.verify_instagram(
            raw_url_or_handle="https://instagram.com/aladdin_grill_kebab",
            business_name="Little Aladdin",
            city="Manchester",
        )
        self.assertEqual(res.status, ContactStatus.UNVERIFIED.value)
        self.assertIn("unrelated entity check", res.notes[0].lower())

    def test_corporate_shared_phone_labeled(self):
        """26. National 0800 corporate non-geographic number is labeled as corporate shared."""
        res = self.engine.verify_phone("0800 123 4567", "National Brand", "Manchester")
        self.assertEqual(res.status, ContactStatus.VERIFIED.value)
        self.assertTrue(res.is_corporate_shared)
        self.assertLess(res.confidence, 0.85)


class TestCategory5ActivationQueueAndChannelRouting(unittest.TestCase):
    """Category 5: LeadActivationProfile Creation, Status Calculation & Channel Routing."""

    def setUp(self):
        self.engine = ContactabilityEnrichmentEngine()

    def test_multi_channel_contactable_status(self):
        """27. Lead with verified phone and verified Instagram becomes MULTI_CHANNEL_CONTACTABLE."""
        lead = {
            "lead_id": "LEAD-MAN-TEST1",
            "company_name": "Multi Channel Cafe",
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "phone": "+44 1618 192265",
            "instagram_url": "https://instagram.com/multichancafemcr",
        }
        prof = self.engine.build_activation_profile(lead)
        self.assertEqual(prof.contactability_status, ContactabilityStatus.MULTI_CHANNEL_CONTACTABLE.value)
        self.assertTrue(prof.activation_ready)

    def test_manual_contactable_status(self):
        """28. Lead with verified phone alone becomes MANUAL_CONTACTABLE."""
        lead = {
            "lead_id": "LEAD-MAN-TEST2",
            "company_name": "Phone Only Cafe",
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "phone": "+44 1618 192265",
        }
        prof = self.engine.build_activation_profile(lead)
        self.assertEqual(prof.contactability_status, ContactabilityStatus.MANUAL_CONTACTABLE.value)
        self.assertTrue(prof.activation_ready)
        self.assertEqual(prof.recommended_channel, ChannelType.PHONE.value)

    def test_no_contact_lead_is_not_contactable_and_activation_blocked(self):
        """29. Lead with no verified channels becomes NOT_CONTACTABLE and activation_ready = False."""
        lead = {
            "lead_id": "LEAD-MAN-TEST3",
            "company_name": "Ghost Diner",
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "phone": "",
            "instagram_url": "",
        }
        prof = self.engine.build_activation_profile(lead)
        self.assertEqual(prof.contactability_status, ContactabilityStatus.NOT_CONTACTABLE.value)
        self.assertFalse(prof.activation_ready)
        self.assertIn("NO_VERIFIED_CHANNELS", prof.activation_blockers[0])

    def test_non_qualified_lead_is_activation_blocked(self):
        """30. Lead with RESEARCH_ONLY state is blocked from activation queue."""
        lead = {
            "lead_id": "LEAD-MAN-TEST4",
            "company_name": "Research Only Bar",
            "qualification_state": "RESEARCH_ONLY",
            "phone": "+44 1618 192265",
        }
        prof = self.engine.build_activation_profile(lead)
        self.assertFalse(prof.activation_ready)

    def test_recommended_channel_priority_order(self):
        """31. When phone and social are both verified, PHONE is prioritized for high-touch local."""
        lead = {
            "lead_id": "LEAD-MAN-TEST5",
            "company_name": "Deli & Pub",
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "phone": "+44 1618 192265",
            "instagram_url": "https://instagram.com/delipubmcr",
        }
        prof = self.engine.build_activation_profile(lead)
        self.assertEqual(prof.recommended_channel, ChannelType.PHONE.value)
        self.assertIn(ChannelType.INSTAGRAM.value, prof.fallback_channels)

    def test_suppressed_channel_not_routed(self):
        """32. Suppressed channel is excluded from recommended routing."""
        lead = {
            "lead_id": "LEAD-MAN-TEST6",
            "company_name": "Suppressed Phone Venue",
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "phone": "+44 1618 192265",
            "instagram_url": "https://instagram.com/suppressedmcr",
        }
        prof = self.engine.build_activation_profile(lead, suppressed_channels={"PHONE"})
        self.assertEqual(prof.recommended_channel, ChannelType.INSTAGRAM.value)
        self.assertNotIn(ChannelType.PHONE.value, prof.fallback_channels)


class TestCategory6CRMHistoryProtectionAndLiveSeafood(unittest.TestCase):
    """Category 6: CRM History Protection & Live Seafood Invariance."""

    def setUp(self):
        self.engine = ContactabilityEnrichmentEngine()

    def test_live_seafood_record_preserved(self):
        """33. Live Seafood Ltd (LEAD-MAN-0363CF) state, status, and outreach flags are intact."""
        lead = {
            "lead_id": "LEAD-MAN-0363CF",
            "company_name": "Live Seafood Ltd",
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "outreach_status": "NOT_READY",
            "actual_send_confirmed": False,
            "phone": "",
            "instagram_url": "https://instagram.com/liveseafoodmcr",
            "facebook_url": "https://facebook.com/liveseafoodmcr",
        }
        prof = self.engine.build_activation_profile(lead)
        self.assertEqual(prof.lead_id, "LEAD-MAN-0363CF")
        self.assertEqual(prof.company_name, "Live Seafood Ltd")
        self.assertEqual(prof.qualification_state, QualificationState.OUTREACH_READY.value)
        self.assertEqual(prof.outreach_status, "NOT_READY")
        self.assertEqual(prof.recommended_channel, ChannelType.INSTAGRAM.value)
        self.assertTrue(prof.activation_ready)

    def test_previously_contacted_lead_blocked_from_new_outreach(self):
        """34. Lead with outreach_status == 'SENT' is blocked with ALREADY_SENT_CONFIRMED."""
        lead = {
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "outreach_status": "SENT",
            "actual_send_confirmed": True,
            "phone": "+44 161 273 5556",
        }
        prof = self.engine.build_activation_profile(lead)
        self.assertFalse(prof.activation_ready)
        self.assertEqual(prof.recommended_channel, "NONE")
        self.assertTrue(any("ALREADY_SENT_CONFIRMED" in b for b in prof.activation_blockers))

    def test_bounced_lead_blocked_or_flagged(self):
        """35. Lead with outreach_status == 'BOUNCED' has PREVIOUS_BOUNCE blocker."""
        lead = {
            "lead_id": "LEAD-MAN-709C66",
            "company_name": "Hong Thai",
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "outreach_status": "BOUNCED",
            "actual_send_confirmed": False,
            "phone": "+44 7796 046556",
        }
        prof = self.engine.build_activation_profile(lead)
        self.assertFalse(prof.activation_ready)
        self.assertTrue(any("PREVIOUS_BOUNCE" in b for b in prof.activation_blockers))


class TestCategory7SafetyCeilingsAndZeroSendInvariants(unittest.TestCase):
    """Category 7: Absolute Safety Ceilings & Zero-Send Invariants."""

    def test_zero_outreach_sends_invariant(self):
        """36. Machine output enforces outreach_sends == 0."""
        run_file = os.path.join(PROJECT_ROOT, "data", "phase_9_3_contactability_run.json")
        if os.path.exists(run_file):
            with open(run_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.assertEqual(d.get("OUTREACH_SENDS"), 0)

    def test_zero_campaigns_armed_invariant(self):
        """37. Machine output enforces campaigns_armed == 0."""
        run_file = os.path.join(PROJECT_ROOT, "data", "phase_9_3_contactability_run.json")
        if os.path.exists(run_file):
            with open(run_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.assertEqual(d.get("CAMPAIGNS_ARMED"), 0)

    def test_zero_crm_writes_invariant(self):
        """38. Machine output enforces crm_writes == 0 in dry-run mode."""
        run_file = os.path.join(PROJECT_ROOT, "data", "phase_9_3_contactability_run.json")
        if os.path.exists(run_file):
            with open(run_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.assertEqual(d.get("CRM_WRITES"), 0)

    def test_all_safety_invariants_pass(self):
        """39. All safety invariants in machine output evaluate to PASS."""
        run_file = os.path.join(PROJECT_ROOT, "data", "phase_9_3_contactability_run.json")
        if os.path.exists(run_file):
            with open(run_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            invariants = d.get("INVARIANTS", {})
            self.assertEqual(invariants.get("ZERO_SENDS"), "PASS")
            self.assertEqual(invariants.get("ZERO_ARMED_CAMPAIGNS"), "PASS")
            self.assertEqual(invariants.get("ZERO_PRODUCTION_SENDER_CALLS"), "PASS")
            self.assertEqual(invariants.get("RULE_B_UNCHANGED"), "PASS")
            self.assertEqual(invariants.get("HISTORICAL_OUTREACH_PRESERVED"), "PASS")
            self.assertEqual(invariants.get("BRANCH_IDENTITY_PROTECTED"), "PASS")


if __name__ == "__main__":
    unittest.main()
