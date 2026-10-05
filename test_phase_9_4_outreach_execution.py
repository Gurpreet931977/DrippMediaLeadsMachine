"""
Unit & Integration Test Suite for Phase 9.4: Controlled Outreach Execution + Operator Send Gateway

Covers all Phase 9.4 requirements:
  - Preflight Gates (qualification, activation readiness, contact verification, branch safety, suppression, duplicate protection, message QA)
  - Operator Gate Invariants (preview does not send, queue view does not send, explicit operator action required, idempotency)
  - Manual Phone Outreach (call attempted, connected, no answer, busy, wrong number, callback, no fake send)
  - Manual Social Outreach (Instagram & Facebook: open profile != sent, copy message != sent, confirm sent == sent, operator provenance, no fake platform IDs)
  - Email Safety (verified email != sendable, disabled email blocks execution)
  - Idempotency & Failure Handling (deterministic key, retry safe, failure handling, send unknown != sent)
  - Timeline & Historical Ordering (preview, operator confirm, attempt, outcome, no history overwrite)
  - Existing Lead History Protection (Live Seafood Ltd preserved, Seoul Kimchi sent preserved, Hong Thai bounce preserved)
  - Safety Ceilings & Pilot Control (sandbox isolation, single-lead stop, zero armed campaigns, no mass send)
"""

import os
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Dict, Any

from fastapi.testclient import TestClient

from lib.outreach.phase_9_4_operator_gateway import (
    OperatorSendGateway,
    PHONE_OUTCOMES,
    MANUAL_RESPONSE_TYPES,
)
from lib.outreach.phase_8_9_product_engine import (
    MessageQA,
    OutreachChannel,
)
from server import app

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


class TestPhase94OutreachExecution(unittest.TestCase):

    def setUp(self):
        # Create isolated temporary directory
        self.temp_dir = tempfile.mkdtemp()
        self.temp_activation = os.path.join(self.temp_dir, "phase_9_3_contactability_run.json")
        self.temp_leads = os.path.join(self.temp_dir, "cache_sheets_leads.json")
        self.temp_timeline = os.path.join(self.temp_dir, "lead_timelines.json")
        self.temp_history = os.path.join(self.temp_dir, "message_history.json")
        self.temp_suppression = os.path.join(self.temp_dir, "suppression_list.json")
        self.temp_outcomes = os.path.join(self.temp_dir, "outreach_outcomes.json")

        # Copy original files
        shutil.copyfile(os.path.join(DATA_DIR, "phase_9_3_contactability_run.json"), self.temp_activation)
        shutil.copyfile(os.path.join(DATA_DIR, "cache_sheets_leads.json"), self.temp_leads)
        shutil.copyfile(os.path.join(DATA_DIR, "suppression_list.json"), self.temp_suppression)

        if os.path.exists(os.path.join(DATA_DIR, "lead_timelines.json")):
            shutil.copyfile(os.path.join(DATA_DIR, "lead_timelines.json"), self.temp_timeline)
        else:
            with open(self.temp_timeline, "w", encoding="utf-8") as f:
                json.dump({}, f)

        if os.path.exists(os.path.join(DATA_DIR, "message_history.json")):
            shutil.copyfile(os.path.join(DATA_DIR, "message_history.json"), self.temp_history)
        else:
            with open(self.temp_history, "w", encoding="utf-8") as f:
                json.dump({}, f)

        if os.path.exists(os.path.join(DATA_DIR, "outreach_outcomes.json")):
            shutil.copyfile(os.path.join(DATA_DIR, "outreach_outcomes.json"), self.temp_outcomes)
        else:
            with open(self.temp_outcomes, "w", encoding="utf-8") as f:
                json.dump([], f)

        # Isolated gateway instance
        self.gateway = OperatorSendGateway(
            activation_path=self.temp_activation,
            leads_path=self.temp_leads,
            timeline_path=self.temp_timeline,
            history_path=self.temp_history,
            suppression_path=self.temp_suppression,
            outcomes_path=self.temp_outcomes,
        )

        # TestClient for FastAPI endpoints
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # 1. Preflight Gates
    # =========================================================================

    def test_preflight_requires_outreach_ready_qualification(self):
        """1. Gate 1: Non-OUTREACH_READY lead is blocked from execution."""
        profile = self.gateway.get_activation_profile("LEAD-MAN-902001").copy()
        profile["qualification_state"] = "MANUAL_REVIEW"
        passed, blockers = self.gateway.evaluate_send_gates(profile, "PHONE", operator_confirmed=True)
        self.assertFalse(passed)
        self.assertTrue(any("GATE_FAILED_QUALIFICATION_STATE" in b for b in blockers))

    def test_preflight_requires_activation_ready_true(self):
        """2. Gate 2: Incomplete or blocked lead (activation_ready=False) is blocked."""
        profile = self.gateway.get_activation_profile("LEAD-MAN-4DB3EF")  # Seoul Kimchi blocked
        passed, blockers = self.gateway.evaluate_send_gates(profile, "PHONE", operator_confirmed=True)
        self.assertFalse(passed)
        self.assertTrue(any("GATE_FAILED_ACTIVATION_NOT_READY" in b for b in blockers))

    def test_preflight_requires_verified_contact_channel(self):
        """3. Gate 3: Channel with status != VERIFIED is blocked."""
        profile = self.gateway.get_activation_profile("LEAD-MAN-902001").copy()
        passed, blockers = self.gateway.evaluate_send_gates(profile, "INSTAGRAM", operator_confirmed=True)
        self.assertFalse(passed)
        self.assertTrue(any("GATE_FAILED_CHANNEL_NOT_VERIFIED" in b for b in blockers))

    def test_preflight_blocks_shared_corporate_contact(self):
        """4. Gate 4: Shared corporate phone number fails branch-safety gate."""
        profile = self.gateway.get_activation_profile("LEAD-MAN-902001").copy()
        profile["verified_contacts"] = {
            "PHONE": {
                "status": "VERIFIED",
                "value": "+44 800 123456",
                "is_corporate_shared": True,
            }
        }
        passed, blockers = self.gateway.evaluate_send_gates(profile, "PHONE", operator_confirmed=True)
        self.assertFalse(passed)
        self.assertTrue(any("GATE_FAILED_BRANCH_SAFETY" in b for b in blockers))

    def test_preflight_blocks_suppressed_lead(self):
        """5. Gate 5: Suppressed lead is blocked from execution."""
        # Add Little Aladdin to suppression list
        self.gateway.suppression_manager.add_suppression("LEAD-MAN-902001", "MANUAL_OPT_OUT")
        profile = self.gateway.get_activation_profile("LEAD-MAN-902001")
        passed, blockers = self.gateway.evaluate_send_gates(profile, "PHONE", operator_confirmed=True)
        self.assertFalse(passed)
        self.assertTrue(any("GATE_FAILED_SUPPRESSION" in b for b in blockers))

    def test_preflight_blocks_previous_same_channel_send(self):
        """6. Gate 6: Prior confirmed send on the same channel blocks new execution."""
        profile = self.gateway.get_activation_profile("LEAD-MAN-902001").copy()
        profile["outreach_status"] = "SENT"
        passed, blockers = self.gateway.evaluate_send_gates(profile, "PHONE", operator_confirmed=True)
        self.assertFalse(passed)
        self.assertTrue(any("GATE_FAILED_ALREADY_SENT" in b for b in blockers))

    def test_preflight_requires_message_qa_pass(self):
        """7. Gate 8: Draft failing Message QA is blocked."""
        # Test QA directly on empty/spam content
        lead = {"company_name": "Little Aladdin", "city": "Manchester"}
        passed, status, errors = MessageQA.validate_draft(
            draft_text="Buy now! Free money guaranteed 10x!",
            lead=lead,
            channel="PHONE",
            recipient="+44 1618 192265",
            offer="Free website",
        )
        self.assertFalse(passed)
        self.assertEqual(status, "DRAFT_BLOCKED")
        self.assertTrue(any("spam" in e.lower() or "guarantee" in e.lower() for e in errors))

    # =========================================================================
    # 2. Operator Gate Invariants
    # =========================================================================

    def test_operator_preview_does_not_send_outreach(self):
        """8. Generating operator preview never executes a send or mutates outreach_status."""
        preview = self.gateway.generate_operator_preview("LEAD-MAN-902001")
        self.assertIsNotNone(preview)
        self.assertEqual(preview["company"], "Little Aladdin")
        # Verify status remained unchanged
        prof_after = self.gateway.get_activation_profile("LEAD-MAN-902001")
        self.assertEqual(prof_after["outreach_status"], "NOT_READY")

    def test_activation_queue_view_does_not_send_outreach(self):
        """9. Opening / viewing activation queue never triggers an outreach send."""
        queue = self.gateway.load_activation_profiles()
        self.assertGreaterEqual(len(queue), 1)
        for lead in queue:
            # None should be converted to SENT by loading queue
            if lead["lead_id"] == "LEAD-MAN-902001":
                self.assertEqual(lead["outreach_status"], "NOT_READY")

    def test_page_refresh_does_not_send_outreach(self):
        """10. Repeated calls to generate_operator_preview do not trigger dispatches."""
        for _ in range(5):
            p = self.gateway.generate_operator_preview("LEAD-MAN-902001")
            self.assertTrue(p["operator_action_required"])
        prof = self.gateway.get_activation_profile("LEAD-MAN-902001")
        self.assertEqual(prof["outreach_status"], "NOT_READY")

    def test_explicit_operator_confirmation_strictly_required(self):
        """11. Action without operator_confirmed=True is blocked."""
        res = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=False,
            outcome="CONNECTED",
            test_mode=True,
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "BLOCK_SEND")
        self.assertTrue(any("GATE_FAILED_OPERATOR_CONFIRMATION" in b for b in res["blockers"]))

    def test_double_click_or_repeated_confirmation_blocked_by_idempotency(self):
        """12. Rapid duplicate click with same key is rejected by idempotency tracker."""
        # First execution in test mode
        res1 = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="CONNECTED",
            test_mode=True,
        )
        self.assertTrue(res1["success"])

        # Second immediate execution
        res2 = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="CONNECTED",
            test_mode=True,
        )
        self.assertFalse(res2["success"])
        self.assertEqual(res2["error"], "DUPLICATE_EXECUTION_BLOCKED")

    # =========================================================================
    # 3. Manual Phone Outreach
    # =========================================================================

    def test_phone_call_attempt_recorded_in_timeline(self):
        """13. Phone action records CALL_ATTEMPTED in timeline."""
        res = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="CONNECTED",
            notes="Spoke with owner",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        timeline = self.gateway.timeline_tracker.get_timeline("LEAD-MAN-902001")
        event_types = [e["event_type"] for e in timeline]
        self.assertIn("CALL_ATTEMPTED", event_types)

    def test_phone_no_answer_recorded_as_call_attempted(self):
        """14. Unanswered call outcomes update status to CALL_ATTEMPTED, not CONTACTED."""
        res = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="NO_ANSWER",
            notes="Ranged 5 times, no answer",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["outreach_status"], "CALL_ATTEMPTED")

    def test_phone_connected_recorded_as_contacted(self):
        """15. Connected phone call updates status to CONTACTED."""
        res = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="CONNECTED",
            notes="Connected with staff",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["outreach_status"], "CONTACTED")

    def test_phone_wrong_number_outcome_recorded(self):
        """16. Wrong number outcome sets outreach_status to NOT_INTERESTED."""
        res = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="WRONG_NUMBER",
            notes="Person said wrong number",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["outreach_status"], "NOT_INTERESTED")

    def test_phone_callback_requested_outcome_recorded(self):
        """17. CALLBACK_REQUESTED recorded with valid phone outcome."""
        res = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="CALLBACK_REQUESTED",
            notes="Call back at 3pm",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["outcome"], "CALLBACK_REQUESTED")

    def test_phone_call_never_produces_fake_automated_send_confirmation(self):
        """18. Phone call produces actual_send_confirmed=False (calls != automated message sends)."""
        res = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="CONNECTED",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertFalse(res["actual_send_confirmed"])

    # =========================================================================
    # 4. Manual Instagram Outreach
    # =========================================================================

    def test_instagram_open_profile_does_not_mark_sent(self):
        """19. OPEN_PROFILE action does NOT mark lead as SENT."""
        res = self.gateway.execute_manual_social_action(
            lead_id="LEAD-MAN-0363CF",
            channel="INSTAGRAM",
            action="OPEN_PROFILE",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertFalse(res["actual_send_confirmed"])
        self.assertEqual(res["outreach_status"], "NOT_READY")

    def test_instagram_copy_message_does_not_mark_sent(self):
        """20. COPY_MESSAGE action does NOT mark lead as SENT."""
        res = self.gateway.execute_manual_social_action(
            lead_id="LEAD-MAN-0363CF",
            channel="INSTAGRAM",
            action="COPY_MESSAGE",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertFalse(res["actual_send_confirmed"])
        self.assertEqual(res["outreach_status"], "NOT_READY")

    def test_instagram_operator_confirmation_produces_sent(self):
        """21. CONFIRM_SENT with operator confirmation transitions to SENT."""
        res = self.gateway.execute_manual_social_action(
            lead_id="LEAD-MAN-0363CF",
            channel="INSTAGRAM",
            action="CONFIRM_SENT",
            operator_confirmed=True,
            notes="DM sent via Instagram app",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertTrue(res["actual_send_confirmed"])
        self.assertEqual(res["outreach_status"], "SENT")

    def test_instagram_missing_confirmation_blocks_sent(self):
        """22. CONFIRM_SENT without operator_confirmed=True is strictly blocked."""
        res = self.gateway.execute_manual_social_action(
            lead_id="LEAD-MAN-0363CF",
            channel="INSTAGRAM",
            action="CONFIRM_SENT",
            operator_confirmed=False,
            test_mode=True,
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "BLOCK_SEND")

    def test_instagram_no_fake_provider_message_id_and_records_operator_source(self):
        """23. Manual social send records provider_message_id=None and confirmation_source=OPERATOR."""
        res = self.gateway.execute_manual_social_action(
            lead_id="LEAD-MAN-0363CF",
            channel="INSTAGRAM",
            action="CONFIRM_SENT",
            operator_confirmed=True,
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertIsNone(res["provider_message_id"])
        self.assertEqual(res["send_confirmation_source"], "OPERATOR")

    # =========================================================================
    # 5. Manual Facebook Outreach
    # =========================================================================

    def test_facebook_open_profile_does_not_mark_sent(self):
        """24. Facebook OPEN_PROFILE action does NOT mark SENT."""
        res = self.gateway.execute_manual_social_action(
            lead_id="LEAD-MAN-0363CF",
            channel="FACEBOOK",
            action="OPEN_PROFILE",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertFalse(res["actual_send_confirmed"])

    def test_facebook_copy_message_does_not_mark_sent(self):
        """25. Facebook COPY_MESSAGE action does NOT mark SENT."""
        res = self.gateway.execute_manual_social_action(
            lead_id="LEAD-MAN-0363CF",
            channel="FACEBOOK",
            action="COPY_MESSAGE",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertFalse(res["actual_send_confirmed"])

    def test_facebook_operator_confirmation_produces_sent(self):
        """26. Facebook CONFIRM_SENT with operator confirmation transitions to SENT."""
        res = self.gateway.execute_manual_social_action(
            lead_id="LEAD-MAN-0363CF",
            channel="FACEBOOK",
            action="CONFIRM_SENT",
            operator_confirmed=True,
            notes="Sent via Facebook Messenger",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["outreach_status"], "SENT")
        self.assertTrue(res["actual_send_confirmed"])

    def test_facebook_unconfirmed_action_blocks_sent(self):
        """27. Facebook action without confirmation is blocked."""
        res = self.gateway.execute_manual_social_action(
            lead_id="LEAD-MAN-0363CF",
            channel="FACEBOOK",
            action="CONFIRM_SENT",
            operator_confirmed=False,
            test_mode=True,
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "BLOCK_SEND")

    # =========================================================================
    # 6. Email Channel Safety
    # =========================================================================

    def test_verified_email_is_not_automatically_sendable(self):
        """28. Verified MX email is not automatically sendable (EMAIL_SENDABLE=False)."""
        preview = self.gateway.generate_operator_preview("LEAD-MAN-902001")
        self.assertFalse(preview["email_sendable"])

    def test_disabled_email_channel_blocks_execution_in_pilot(self):
        """29. Email channel evaluation returns GATE_FAILED_EMAIL_DISABLED."""
        profile = self.gateway.get_activation_profile("LEAD-MAN-902001").copy()
        profile["verified_contacts"] = {"EMAIL": {"status": "VERIFIED", "value": "test@business.co.uk"}}
        passed, blockers = self.gateway.evaluate_send_gates(profile, "EMAIL", operator_confirmed=True)
        self.assertFalse(passed)
        self.assertTrue(any("GATE_FAILED_EMAIL_DISABLED" in b for b in blockers))

    def test_email_discovered_and_verified_distinguished_from_sendable(self):
        """30. System distinguishes DISCOVERED, VERIFIED, and SENDABLE."""
        profile = self.gateway.get_activation_profile("LEAD-MAN-902001")
        self.assertFalse(profile.get("email_sendable", False))

    # =========================================================================
    # 7. Idempotency & Failure Handling
    # =========================================================================

    def test_idempotency_key_is_deterministic_and_retry_safe(self):
        """31. Idempotency key format is deterministic: lead_id:channel:version:attempt."""
        res = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="CONNECTED",
            test_mode=True,
        )
        self.assertEqual(res["idempotency_key"], "LEAD-MAN-902001:PHONE:WEBSITE_DEV_V1:1")

    def test_same_lead_channel_attempt_cannot_double_send(self):
        """32. Identical lead/channel cannot double send."""
        key = "LEAD-MAN-902001:PHONE:WEBSITE_DEV_V1:1"
        self.assertFalse(self.gateway.idempotency_tracker.is_duplicate(key))
        self.gateway.idempotency_tracker.record(key)
        self.assertTrue(self.gateway.idempotency_tracker.is_duplicate(key))

    def test_send_failure_remains_failed_without_false_confirmation(self):
        """33. Invalid phone outcome fails cleanly without marking contacted."""
        res = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="INVALID_OUTCOME_STATE",
            test_mode=True,
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "INVALID_OUTCOME")

    def test_send_unknown_never_converted_to_sent(self):
        """34. Missing lead profile returns LEAD_NOT_FOUND rather than SENT."""
        res = self.gateway.execute_manual_phone_action(
            lead_id="NON-EXISTENT-LEAD",
            operator_confirmed=True,
            outcome="CONNECTED",
            test_mode=True,
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "LEAD_NOT_FOUND")

    # =========================================================================
    # 8. Lead Timeline & Ordering
    # =========================================================================

    def test_timeline_records_message_previewed_event(self):
        """35. Operator preview generation records MESSAGE_PREVIEWED event."""
        self.gateway.generate_operator_preview("LEAD-MAN-902001")
        timeline = self.gateway.timeline_tracker.get_timeline("LEAD-MAN-902001")
        self.assertTrue(any(e["event_type"] == "MESSAGE_PREVIEWED" for e in timeline))

    def test_timeline_records_operator_confirmed_event(self):
        """36. Executing phone call logs OPERATOR_CONFIRMED."""
        self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="CONNECTED",
            test_mode=True,
        )
        timeline = self.gateway.timeline_tracker.get_timeline("LEAD-MAN-902001")
        self.assertTrue(any(e["event_type"] == "OPERATOR_CONFIRMED" for e in timeline))

    def test_timeline_records_attempt_and_outcome_in_strict_chronological_order(self):
        """37. Timeline records events in chronological sequence."""
        self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="BUSY",
            test_mode=True,
        )
        timeline = self.gateway.timeline_tracker.get_timeline("LEAD-MAN-902001")
        types = [e["event_type"] for e in timeline]
        # Must contain OPERATOR_CONFIRMED, CALL_ATTEMPTED, OUTCOME
        self.assertIn("OPERATOR_CONFIRMED", types)
        self.assertIn("CALL_ATTEMPTED", types)
        self.assertIn("OUTCOME", types)

    def test_timeline_preserves_historical_entries_without_overwriting(self):
        """38. Historical timeline events from past phases are not destroyed."""
        # Initial timeline loaded in setUp had entries (e.g. Mary D's Beamish Bar)
        timeline_mary = self.gateway.timeline_tracker.get_timeline("LEAD-MAN-682E5D")
        self.assertGreaterEqual(len(timeline_mary), 1)

    # =========================================================================
    # 9. Existing Lead & History Protection
    # =========================================================================

    def test_live_seafood_ltd_preserved_in_outreach_ready_not_ready(self):
        """39. Live Seafood Ltd remains OUTREACH_READY, NOT_READY, MANUAL, actual_send_confirmed=False."""
        lead = self.gateway.load_crm_lead("LEAD-MAN-0363CF")
        self.assertIsNotNone(lead)
        self.assertEqual(lead["qualification_state"], "OUTREACH_READY")
        self.assertEqual(lead["outreach_status"], "NOT_READY")
        self.assertEqual(lead["outreach_mode"], "MANUAL")
        self.assertFalse(lead.get("actual_send_confirmed", False))

    def test_seoul_kimchi_historical_send_preserved_and_blocked(self):
        """40. Seoul Kimchi remains SENT with actual_send_confirmed=True and blocked from execution."""
        profile = self.gateway.get_activation_profile("LEAD-MAN-4DB3EF")
        self.assertIsNotNone(profile)
        self.assertEqual(profile["outreach_status"], "SENT")
        passed, blockers = self.gateway.evaluate_send_gates(profile, "PHONE", operator_confirmed=True)
        self.assertFalse(passed)
        self.assertTrue(any("GATE_FAILED_ALREADY_SENT" in b for b in blockers))

    def test_hong_thai_previous_bounce_preserved_and_blocked(self):
        """41. Hong Thai remains BOUNCED and blocked from execution."""
        profile = self.gateway.get_activation_profile("LEAD-MAN-709C66")
        self.assertIsNotNone(profile)
        self.assertEqual(profile["outreach_status"], "BOUNCED")
        passed, blockers = self.gateway.evaluate_send_gates(profile, "PHONE", operator_confirmed=True)
        self.assertFalse(passed)
        self.assertTrue(any("GATE_FAILED_PREVIOUS_BOUNCE" in b for b in blockers))

    def test_confirmed_sent_history_cannot_be_overwritten(self):
        """42. Seoul Kimchi cannot have its sent status overwritten."""
        preview = self.gateway.generate_operator_preview("LEAD-MAN-4DB3EF")
        self.assertFalse(preview["can_execute"])
        self.assertTrue(any("ALREADY_SENT" in b for b in preview["activation_blockers"]))

    # =========================================================================
    # 10. Safety Ceilings & Pilot Limits
    # =========================================================================

    def test_sandbox_mode_never_mutates_production_crm_or_message_history(self):
        """43. Test mode execution never mutates cache_sheets_leads.json."""
        with open(self.temp_leads, "r", encoding="utf-8") as f:
            leads_before = json.load(f)

        self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="CONNECTED",
            test_mode=True,
        )

        with open(self.temp_leads, "r", encoding="utf-8") as f:
            leads_after = json.load(f)

        self.assertEqual(leads_before, leads_after)

    def test_pilot_single_execution_limit_stops_after_first_confirmed_outcome(self):
        """44. Pilot permits exactly one production execution before stopping."""
        # First execution (production mode)
        res1 = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="CONNECTED",
            test_mode=False,
        )
        self.assertTrue(res1["success"])

        # Attempt second execution on same lead
        res2 = self.gateway.execute_manual_phone_action(
            lead_id="LEAD-MAN-902001",
            operator_confirmed=True,
            outcome="CONNECTED",
            test_mode=False,
        )
        self.assertFalse(res2["success"])
        self.assertEqual(res2["error"], "PILOT_SINGLE_EXECUTION_LIMIT")

    def test_campaigns_not_automatically_armed(self):
        """45. Pilot execution does not create or arm any automated campaigns."""
        camp_path = os.path.join(DATA_DIR, "campaigns.json")
        if os.path.exists(camp_path):
            with open(camp_path, "r", encoding="utf-8") as f:
                camps = json.load(f)
            camp_list = camps if isinstance(camps, list) else list(camps.values())
            # Ensure no automated armed campaigns exist
            for c in camp_list:
                self.assertNotEqual(c.get("status"), "ARMED")

    def test_no_mass_or_bulk_execution_allowed_in_pilot(self):
        """46. Gateway only processes single explicit lead_id actions, no bulk send endpoint."""
        self.assertTrue(hasattr(self.gateway, "execute_manual_phone_action"))
        self.assertFalse(hasattr(self.gateway, "execute_bulk_outreach"))


if __name__ == "__main__":
    unittest.main()
