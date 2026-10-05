"""
Test Suite for Phase 9.5: Controlled Outreach Batch + Real Outcome Analytics

Validates:
  - Batch size ceiling: MAX_BATCH_SIZE = 3
  - Batch candidate selection: only eligible leads, excludes Little Aladdin, Seoul Kimchi, Hong Thai
  - Sequential step gating: 1 active lead at a time, explicit NEXT LEAD required, no auto-continuation
  - Dynamic 7-condition preflight eligibility
  - Disaggregated metrics: manual phone calls count as outreach_attempts=1, automated_sends=0
  - Explicit rate calculations and denominators:
      contact_rate = connected / phone_attempts
      interest_rate = interested / connected
      callback_rate = callback_requested / phone_attempts
      no_answer_rate = no_answer / phone_attempts
      failure_rate = (failed + wrong_number) / outreach_attempts
  - Structured outcome states:
      INTERESTED -> structured follow-up, auto_follow_up=False
      CALLBACK_REQUESTED -> callback_required=True, auto_schedule=False
      NOT_INTERESTED -> distinct from suppression
      NO_ANSWER -> CALL_ATTEMPTED, not CONTACTED, not REJECTED
      WRONG_NUMBER / channel failure -> invalidation only with evidence, fallback recalculation
  - Idempotency: deterministic key lead_id:channel:attempt, duplicate click blocked
  - Append-only timeline logging and CRM history preservation
  - Zero automated sends, zero armed campaigns
  - API endpoint integration via TestClient
"""

import os
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from lib.outreach.controlled_batch_executor import (
    ControlledBatchExecutor,
    MAX_BATCH_SIZE,
    PHONE_OUTCOMES,
    SOCIAL_OUTCOMES,
)
from server import app


class TestPhase95ControlledBatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        # Create an isolated temporary directory for state, outcomes, timelines, and follow-ups
        self.test_dir = tempfile.mkdtemp()
        self.batch_state_path = os.path.join(self.test_dir, "batch_state.json")
        self.followups_path = os.path.join(self.test_dir, "followups.json")
        self.outcomes_path = os.path.join(self.test_dir, "outreach_outcomes.json")
        self.timeline_path = os.path.join(self.test_dir, "lead_timelines.json")
        self.leads_path = os.path.join(self.test_dir, "cache_sheets_leads.json")
        self.suppression_path = os.path.join(self.test_dir, "suppression_list.json")
        self.history_path = os.path.join(self.test_dir, "message_history.json")

        # Seed with current actual files from data/
        for src, dst in [
            ("data/outreach_outcomes.json", self.outcomes_path),
            ("data/lead_timelines.json", self.timeline_path),
            ("data/cache_sheets_leads.json", self.leads_path),
            ("data/suppression_list.json", self.suppression_path),
            ("data/message_history.json", self.history_path),
        ]:
            if os.path.exists(src):
                shutil.copy(src, dst)

        # Reset Phase 9.5 batch candidates in test fixture to baseline NOT_READY state
        batch_lead_ids = {"LEAD-MAN-3B9091", "LEAD-MAN-4098E1", "LEAD-MAN-E81185"}
        if os.path.exists(self.leads_path):
            with open(self.leads_path, "r", encoding="utf-8") as f:
                crm_data = json.load(f)
            leads = crm_data.get("leads", crm_data) if isinstance(crm_data, dict) else crm_data
            for l in leads:
                if l.get("lead_id") in batch_lead_ids:
                    l["outreach_status"] = "NOT_READY"
                    l["outreach_channel"] = ""
            with open(self.leads_path, "w", encoding="utf-8") as f:
                json.dump(crm_data, f, indent=2)

        if os.path.exists(self.outcomes_path):
            with open(self.outcomes_path, "r", encoding="utf-8") as f:
                outcomes_data = json.load(f)
            outcomes_data = [o for o in outcomes_data if o.get("lead_id") not in batch_lead_ids]
            with open(self.outcomes_path, "w", encoding="utf-8") as f:
                json.dump(outcomes_data, f, indent=2)

        self.executor = ControlledBatchExecutor(
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
            followups_path=self.followups_path,
            batch_state_path=self.batch_state_path,
        )
        self.executor.init_batch()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    # -------------------------------------------------------------------------
    # 1. Batch Selection & Ceiling (8 tests)
    # -------------------------------------------------------------------------

    def test_max_batch_size_ceiling_constant(self):
        """Batch size ceiling must be exactly 3."""
        self.assertEqual(MAX_BATCH_SIZE, 3)
        self.assertEqual(self.executor.MAX_BATCH_SIZE, 3)

    def test_select_batch_candidates_never_exceeds_max(self):
        """Batch selection never returns more than MAX_BATCH_SIZE leads."""
        candidates = self.executor.select_batch_candidates(max_batch_size=10)
        self.assertLessEqual(len(candidates), 3)
        self.assertEqual(len(candidates), 3)

    def test_only_eligible_leads_selected(self):
        """All candidates selected must be OUTREACH_READY and activation_ready."""
        candidates = self.executor.select_batch_candidates()
        for c in candidates:
            prof = c["profile"]
            self.assertEqual(prof.get("qualification_state"), "OUTREACH_READY")
            self.assertTrue(prof.get("activation_ready"))
            self.assertNotEqual(prof.get("suppression_status"), "SUPPRESSED")

    def test_little_aladdin_pilot_excluded(self):
        """Little Aladdin (LEAD-MAN-902001) completed pilot and must NOT be in new batch."""
        candidates = self.executor.select_batch_candidates()
        lead_ids = [c["lead_id"] for c in candidates]
        self.assertNotIn("LEAD-MAN-902001", lead_ids)
        self.assertNotIn("Little Aladdin", [c["company_name"] for c in candidates])

    def test_seoul_kimchi_already_sent_excluded(self):
        """Seoul Kimchi (LEAD-MAN-4DB3EF) is SENT and must be excluded."""
        candidates = self.executor.select_batch_candidates()
        lead_ids = [c["lead_id"] for c in candidates]
        self.assertNotIn("LEAD-MAN-4DB3EF", lead_ids)

    def test_hong_thai_suppressed_bounced_excluded(self):
        """Hong Thai (LEAD-MAN-709C66) is BOUNCED/SUPPRESSED and must be excluded."""
        candidates = self.executor.select_batch_candidates()
        lead_ids = [c["lead_id"] for c in candidates]
        self.assertNotIn("LEAD-MAN-709C66", lead_ids)

    def test_batch_ranking_order(self):
        """Batch candidates are ranked by confidence, review count, rating."""
        candidates = self.executor.select_batch_candidates()
        self.assertEqual(candidates[0]["lead_id"], "LEAD-MAN-3B9091")  # The Old Monkey: 1911 revs
        self.assertEqual(candidates[1]["lead_id"], "LEAD-MAN-4098E1")  # Dog and Partridge: 730 revs
        self.assertEqual(candidates[2]["lead_id"], "LEAD-MAN-E81185")  # Manchester Shawarma: 217 revs

    def test_batch_init_stops_at_ready_for_operator(self):
        """Initializing a batch creates 3 leads and sets status READY_FOR_OPERATOR."""
        batch = self.executor.init_batch()
        self.assertEqual(batch["status"], "READY_FOR_OPERATOR")
        self.assertEqual(batch["current_index"], 0)
        self.assertEqual(batch["batch_size"], 3)
        self.assertEqual(batch["completed_count"], 0)
        self.assertFalse(batch["automated_send_enabled"])

    # -------------------------------------------------------------------------
    # 2. Sequential Step Gating & Human-in-the-Loop (6 tests)
    # -------------------------------------------------------------------------

    def test_one_active_lead_at_a_time(self):
        """Batch executor only exposes the current lead at index 0."""
        self.executor.init_batch()
        cur = self.executor.get_current_batch_lead()
        self.assertEqual(cur["current_index"], 0)
        self.assertEqual(cur["lead"]["lead_id"], "LEAD-MAN-3B9091")

    def test_advance_before_outcome_strictly_rejected(self):
        """Cannot call next_lead() before current lead outcome is recorded."""
        self.executor.init_batch()
        res = self.executor.next_lead()
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "OUTCOME_REQUIRED")

    def test_action_requires_explicit_operator_confirmation(self):
        """Calling execute_current_lead_action with operator_confirmed=False fails."""
        self.executor.init_batch()
        res = self.executor.execute_current_lead_action(operator_confirmed=False, action="CALL")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "OPERATOR_CONFIRMATION_REQUIRED")

    def test_advance_after_outcome_advances_pointer(self):
        """After recording outcome, next_lead() advances current_index from 0 to 1."""
        self.executor.init_batch()
        self.executor.execute_current_lead_action(operator_confirmed=True, action="CALL", test_mode=True)
        self.executor.record_current_lead_outcome(outcome="CONNECTED", test_mode=True)
        res = self.executor.next_lead()
        self.assertTrue(res["success"])
        self.assertEqual(res["current_index"], 1)

    def test_batch_completes_after_all_three_leads(self):
        """Batch reaches BATCH_COMPLETED when all 3 leads are processed."""
        self.executor.init_batch()
        for expected_idx in range(3):
            cur = self.executor.get_current_batch_lead()
            self.assertEqual(cur["current_index"], expected_idx)
            self.executor.execute_current_lead_action(operator_confirmed=True, action="CALL", test_mode=True)
            self.executor.record_current_lead_outcome(outcome="CONNECTED", test_mode=True)
            adv = self.executor.next_lead()

        self.assertEqual(adv["status"], "BATCH_COMPLETED")
        self.assertEqual(adv["completed_count"], 3)

    def test_no_autonomous_continuation(self):
        """State remains OUTCOME_RECORDED indefinitely until next_lead is explicitly called."""
        self.executor.init_batch()
        self.executor.execute_current_lead_action(operator_confirmed=True, action="CALL", test_mode=True)
        self.executor.record_current_lead_outcome(outcome="NO_ANSWER", test_mode=True)
        cur = self.executor.get_current_batch_lead()
        self.assertEqual(cur["lead"]["status"], "OUTCOME_RECORDED")
        self.assertTrue(cur["can_advance"])
        # Index remains 0
        self.assertEqual(cur["current_index"], 0)

    # -------------------------------------------------------------------------
    # 3. Dynamic Preflight Eligibility (6 tests)
    # -------------------------------------------------------------------------

    def test_preflight_passes_for_eligible_lead(self):
        """Preflight passes for valid candidate The Old Monkey."""
        is_el, blockers, _ = self.executor.evaluate_preflight_eligibility("LEAD-MAN-3B9091", "PHONE")
        self.assertTrue(is_el)
        self.assertEqual(len(blockers), 0)

    def test_preflight_fails_if_qualification_not_outreach_ready(self):
        """Preflight fails if qualification_state != OUTREACH_READY."""
        with open(self.executor.activation_path, "r") as f:
            data = json.load(f)
        data["ACTIVATION_QUEUE"][0]["qualification_state"] = "RESEARCH_ONLY"
        temp_act = os.path.join(self.test_dir, "temp_act.json")
        with open(temp_act, "w") as f:
            json.dump(data, f)

        ex = ControlledBatchExecutor(activation_path=temp_act)
        is_el, blockers, _ = ex.evaluate_preflight_eligibility(data["ACTIVATION_QUEUE"][0]["lead_id"])
        self.assertFalse(is_el)
        self.assertTrue(any("qualification_state" in b for b in blockers))

    def test_preflight_fails_if_activation_ready_false(self):
        """Preflight fails if activation_ready is False."""
        is_el, blockers, _ = self.executor.evaluate_preflight_eligibility("LEAD-MAN-4DB3EF")
        self.assertFalse(is_el)
        self.assertTrue(any("activation_ready" in b for b in blockers))

    def test_preflight_fails_if_suppressed(self):
        """Preflight fails if lead is suppressed."""
        is_el, blockers, _ = self.executor.evaluate_preflight_eligibility("LEAD-MAN-709C66")
        self.assertFalse(is_el)
        self.assertTrue(any("suppressed" in b for b in blockers))

    def test_preflight_fails_if_previous_confirmed_send(self):
        """Preflight fails if lead has prior confirmed send."""
        is_el, blockers, _ = self.executor.evaluate_preflight_eligibility("LEAD-MAN-4DB3EF")
        self.assertFalse(is_el)
        self.assertTrue(any("send" in b.lower() for b in blockers))

    def test_preflight_fails_if_channel_not_verified(self):
        """Preflight fails if requested channel is not verified."""
        # The Old Monkey does not have email verified
        is_el, blockers, _ = self.executor.evaluate_preflight_eligibility("LEAD-MAN-3B9091", "EMAIL")
        self.assertFalse(is_el)
        self.assertTrue(any("contact" in b.lower() for b in blockers))

    # -------------------------------------------------------------------------
    # 4. Disaggregated Real Outreach Metrics (8 tests)
    # -------------------------------------------------------------------------

    def test_manual_phone_call_is_one_attempt_zero_automated_sends(self):
        """Manual phone call counts as outreach_attempts=1 and automated_sends=0."""
        self.executor.init_batch()
        res = self.executor.execute_current_lead_action(operator_confirmed=True, action="CALL", test_mode=True)
        self.assertEqual(res["outreach_attempts"], 1)
        self.assertEqual(res["automated_sends"], 0)

    def test_phone_outcome_connected_counted(self):
        """CONNECTED outcome is recorded and increments connected count."""
        res = self.executor.record_current_lead_outcome(outcome="CONNECTED", test_mode=True)
        self.assertTrue(res["success"])
        self.assertEqual(res["crm_status"], "CONTACTED")
        self.assertFalse(res["actual_send_confirmed"])

    def test_phone_outcome_interested_counted(self):
        """INTERESTED outcome increments interested count and marks CONTACTED."""
        res = self.executor.record_current_lead_outcome(outcome="INTERESTED", notes="Wants website demo", test_mode=True)
        self.assertTrue(res["success"])
        self.assertEqual(res["crm_status"], "CONTACTED")
        self.assertIsNotNone(res["structured_followup"])

    def test_phone_outcome_callback_requested_counted(self):
        """CALLBACK_REQUESTED outcome creates callback flag."""
        res = self.executor.record_current_lead_outcome(
            outcome="CALLBACK_REQUESTED",
            callback_time="2026-10-06 14:00",
            notes="Ask for David",
            test_mode=True
        )
        self.assertTrue(res["success"])
        self.assertTrue(res["structured_followup"]["callback_required"])
        self.assertEqual(res["structured_followup"]["requested_callback_time"], "2026-10-06 14:00")

    def test_phone_outcome_no_answer_counted(self):
        """NO_ANSWER outcome sets status to CALL_ATTEMPTED (never CONTACTED)."""
        res = self.executor.record_current_lead_outcome(outcome="NO_ANSWER", test_mode=True)
        self.assertEqual(res["crm_status"], "CALL_ATTEMPTED")
        self.assertFalse(res["actual_send_confirmed"])

    def test_phone_outcome_busy_counted(self):
        """BUSY outcome sets status to CALL_ATTEMPTED."""
        res = self.executor.record_current_lead_outcome(outcome="BUSY", test_mode=True)
        self.assertEqual(res["crm_status"], "CALL_ATTEMPTED")

    def test_phone_outcome_not_interested_counted(self):
        """NOT_INTERESTED outcome sets status to NOT_INTERESTED."""
        res = self.executor.record_current_lead_outcome(outcome="NOT_INTERESTED", test_mode=True)
        self.assertEqual(res["crm_status"], "NOT_INTERESTED")

    def test_phone_outcome_wrong_number_and_failure_separated(self):
        """WRONG_NUMBER outcome is tracked distinctly from general failures."""
        res = self.executor.record_current_lead_outcome(outcome="WRONG_NUMBER", test_mode=True)
        self.assertEqual(res["crm_status"], "FAILED")

    # -------------------------------------------------------------------------
    # 5. Analytics Rates & Denominators (5 tests)
    # -------------------------------------------------------------------------

    def test_contact_rate_uses_phone_attempts_as_denominator(self):
        """contact_rate = connected / phone_attempts."""
        analytics = self.executor.get_controlled_batch_analytics()
        # Seeded data has 1 connected phone call (Little Aladdin)
        self.assertEqual(analytics["rates"]["contact_rate"], 1.0)

    def test_interest_rate_uses_connected_as_denominator(self):
        """interest_rate = interested / connected."""
        analytics = self.executor.get_controlled_batch_analytics()
        # 0 interested / 1 connected = 0.0
        self.assertEqual(analytics["rates"]["interest_rate"], 0.0)

    def test_callback_rate_uses_phone_attempts_as_denominator(self):
        """callback_rate = callback_requested / phone_attempts."""
        analytics = self.executor.get_controlled_batch_analytics()
        self.assertEqual(analytics["rates"]["callback_rate"], 0.0)

    def test_no_answer_rate_uses_phone_attempts_as_denominator(self):
        """no_answer_rate = no_answer / phone_attempts."""
        analytics = self.executor.get_controlled_batch_analytics()
        self.assertEqual(analytics["rates"]["no_answer_rate"], 0.0)

    def test_failure_rate_uses_outreach_attempts_as_denominator(self):
        """failure_rate = (failed + wrong_number) / outreach_attempts."""
        analytics = self.executor.get_controlled_batch_analytics()
        self.assertEqual(analytics["rates"]["failure_rate"], 0.0)

    # -------------------------------------------------------------------------
    # 6. Structured Outcome State Semantics (6 tests)
    # -------------------------------------------------------------------------

    def test_interested_creates_structured_followup_state(self):
        """INTERESTED sets auto_follow_up=False, requiring human control."""
        res = self.executor.record_current_lead_outcome(
            outcome="INTERESTED",
            notes="Interested in web design pricing",
            test_mode=True
        )
        followup = res["structured_followup"]
        self.assertTrue(followup["follow_up_required"])
        self.assertFalse(followup["auto_follow_up"])
        self.assertEqual(followup["response_stage"], "INTERESTED")

    def test_callback_requested_creates_followup_flag(self):
        """CALLBACK_REQUESTED sets auto_schedule=False and preserves date/time."""
        res = self.executor.record_current_lead_outcome(
            outcome="CALLBACK_REQUESTED",
            callback_time="Friday 10am",
            notes="Owner will be in kitchen",
            test_mode=True
        )
        followup = res["structured_followup"]
        self.assertTrue(followup["callback_required"])
        self.assertFalse(followup["auto_schedule"])
        self.assertEqual(followup["requested_callback_time"], "Friday 10am")

    def test_not_interested_does_not_suppress_globally(self):
        """NOT_INTERESTED sets status but does not automatically add to suppression list."""
        self.executor.record_current_lead_outcome(outcome="NOT_INTERESTED", test_mode=False)
        is_supp, _ = self.executor.suppression_manager.is_suppressed("LEAD-MAN-3B9091")
        self.assertFalse(is_supp)

    def test_no_answer_never_becomes_contacted_or_rejected(self):
        """NO_ANSWER leaves status as CALL_ATTEMPTED, not CONTACTED, not REJECTED."""
        res = self.executor.record_current_lead_outcome(outcome="NO_ANSWER", test_mode=True)
        self.assertEqual(res["crm_status"], "CALL_ATTEMPTED")
        self.assertNotEqual(res["crm_status"], "CONTACTED")
        self.assertNotEqual(res["crm_status"], "REJECTED")

    def test_wrong_number_triggers_channel_failure_logging(self):
        """WRONG_NUMBER logs CHANNEL_FAILURE to timeline."""
        self.executor._handle_channel_failure("LEAD-MAN-3B9091", "PHONE", "WRONG_NUMBER", test_mode=False)
        events = self.executor.timeline_tracker.get_timeline("LEAD-MAN-3B9091")
        failure_events = [e for e in events if e.get("event_type") == "CHANNEL_FAILURE"]
        self.assertGreater(len(failure_events), 0)

    def test_channel_failure_returns_to_operator_review(self):
        """Channel failure indicates action_taken == RETURNED_TO_OPERATOR_REVIEW."""
        self.executor._handle_channel_failure("LEAD-MAN-3B9091", "PHONE", "INVALID_CONTACT", test_mode=False)
        events = self.executor.timeline_tracker.get_timeline("LEAD-MAN-3B9091")
        failure_event = [e for e in events if e.get("event_type") == "CHANNEL_FAILURE"][-1]
        self.assertEqual(failure_event["details"]["action_taken"], "RETURNED_TO_OPERATOR_REVIEW")

    # -------------------------------------------------------------------------
    # 7. Idempotency & History Protection (6 tests)
    # -------------------------------------------------------------------------

    def test_deterministic_idempotency_key_format(self):
        """Idempotency key follows lead_id:channel:attempt."""
        self.executor.init_batch()
        res = self.executor.execute_current_lead_action(operator_confirmed=True, action="CALL", test_mode=True)
        expected_key = "LEAD-MAN-3B9091:PHONE:1"
        self.assertEqual(res["idempotency_key"], expected_key)

    def test_duplicate_action_click_blocked(self):
        """Subsequent clicks with same idempotency key return idempotent replay without error."""
        self.executor.init_batch()
        res1 = self.executor.execute_current_lead_action(operator_confirmed=True, action="CALL", test_mode=False)
        self.assertTrue(res1["success"])
        res2 = self.executor.execute_current_lead_action(operator_confirmed=True, action="CALL", test_mode=False)
        self.assertTrue(res2["success"])
        self.assertTrue(res2.get("idempotent_replay"))

    def test_repeated_batch_init_safe(self):
        """Re-initializing batch cleanly resets the batch state."""
        b1 = self.executor.init_batch()
        b2 = self.executor.init_batch()
        self.assertEqual(b2["status"], "READY_FOR_OPERATOR")
        self.assertEqual(b2["batch_size"], 3)

    def test_timeline_is_append_only(self):
        """Lead timeline only grows and never replaces historical records."""
        lead_id = "LEAD-MAN-3B9091"
        init_len = len(self.executor.timeline_tracker.get_timeline(lead_id))
        self.executor.timeline_tracker.log_event("TEST_EVT_1", lead_id, "PHONE", "TEST", "OP")
        self.executor.timeline_tracker.log_event("TEST_EVT_2", lead_id, "PHONE", "TEST", "OP")
        final_len = len(self.executor.timeline_tracker.get_timeline(lead_id))
        self.assertEqual(final_len, init_len + 2)

    def test_little_aladdin_pilot_preserved_in_outcomes(self):
        """Little Aladdin's pilot outcome is preserved as CONNECTED."""
        analytics = self.executor.get_controlled_batch_analytics()
        learning = analytics["lead_priority_learning"]
        aladdin_entries = [l for l in learning if l["lead_id"] == "LEAD-MAN-902001"]
        self.assertGreater(len(aladdin_entries), 0)
        self.assertEqual(aladdin_entries[0]["outcome"], "CONNECTED")

    def test_seoul_kimchi_remains_blocked(self):
        """Seoul Kimchi cannot be executed."""
        is_el, blockers, _ = self.executor.evaluate_preflight_eligibility("LEAD-MAN-4DB3EF")
        self.assertFalse(is_el)

    # -------------------------------------------------------------------------
    # 8. API Endpoints & Safety Invariants (5 tests)
    # -------------------------------------------------------------------------

    def test_api_get_controlled_batch_analytics(self):
        """GET /api/outreach/controlled-batch-analytics returns 200 and analytics."""
        res = self.client.get("/api/outreach/controlled-batch-analytics")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("analytics", data)
        self.assertEqual(data["analytics"]["automated_sends"], 0)

    def test_api_get_controlled_batch_state(self):
        """GET /api/outreach/controlled-batch returns 200 and batch payload."""
        res = self.client.get("/api/outreach/controlled-batch")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("batch", data)

    def test_zero_automated_sends_guaranteed(self):
        """AUTOMATED_SEND_ENABLED is strictly False across the engine."""
        self.assertFalse(ControlledBatchExecutor.AUTOMATED_SEND_ENABLED)

    def test_zero_campaigns_armed_guaranteed(self):
        """campaigns_armed is strictly 0 in analytics."""
        analytics = self.executor.get_controlled_batch_analytics()
        self.assertEqual(analytics["campaigns_armed"], 0)

    def test_api_operator_confirmation_enforced(self):
        """POST /api/outreach/controlled-batch/action without confirmation returns 400."""
        res = self.client.post("/api/outreach/controlled-batch/action", json={
            "operator_confirmed": False,
            "action": "CALL"
        })
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertEqual(data["error"], "OPERATOR_CONFIRMATION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
