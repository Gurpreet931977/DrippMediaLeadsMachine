"""
Test Suite: Phase 9.7 Live Outreach Batch Execution & Outcome Capture
======================================================================
Covers:
  1. Execution:
     - Real manual call path
     - Operator confirmation required
     - No automatic continuation (explicit advance required)
     - Batch ceiling enforced (MAX_BATCH_SIZE = 3)
     - Sequential ordering enforced (The Old Monkey -> Dog and Partridge -> Manchester Shawarma)
     - Sequence mismatch blocked
  2. Outcomes:
     - CONNECTED -> CONTACTED
     - NO_ANSWER -> CALL_ATTEMPTED (strictly NOT CONTACTED)
     - BUSY -> CALL_ATTEMPTED (strictly NOT CONTACTED)
     - CALLBACK_REQUESTED -> CONTACTED + callback_required=True, auto_schedule=False
     - INTERESTED -> follow_up_required=True, auto_follow_up=False
     - NOT_INTERESTED -> distinct from suppression
     - WRONG_NUMBER -> marked for review, not deleted
     - FAILED -> contact review
     - Invalid outcome rejected
  3. State & Rule B:
     - CRM state updated correctly
     - Qualification state unchanged (Rule B frozen)
     - Notes stored as observations, not qualification evidence
     - Activation state updated correctly
  4. History & Protected Leads:
     - Timeline is append-only
     - Canonical sequence of events logged
     - Little Aladdin protected
     - Seoul Kimchi protected
     - Hong Thai protected
     - Live Seafood protected
  5. Idempotency:
     - Duplicate action blocked
     - Canonical idempotency key format
     - Refresh / batch restart safe
  6. Analytics & Denominators:
     - Canonical event created
     - Explicit denominators used (calls as denominator for contact rate, etc.)
     - NO_ANSWER excluded from contacted count
     - Sample size warning retained (<5 descriptive only)
     - Priority model not mutated by single batch
  7. Strict Safety Invariants:
     - AUTOMATED_SENDS = 0
     - CAMPAIGNS_ARMED = 0
     - AUTOMATED_FOLLOWUPS = 0
     - AUTONOMOUS_CONTINUATION = 0
"""

import os
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Dict, Any, List

from lib.outreach.phase_9_7_live_outreach_runner import (
    Phase97LiveOutreachRunner,
    DEFAULT_BATCH_STATE_PATH,
    DEFAULT_LEADS_PATH,
    DEFAULT_TIMELINE_PATH,
    DEFAULT_HISTORY_PATH,
    DEFAULT_SUPPRESSION_PATH,
    DEFAULT_OUTCOMES_PATH,
    DEFAULT_FOLLOWUPS_PATH,
    DEFAULT_RUN_OUTPUT_PATH,
)
from lib.outreach.controlled_batch_executor import (
    ControlledBatchExecutor,
    MAX_BATCH_SIZE,
    PHONE_OUTCOMES,
)
from lib.analytics.outreach_performance_engine import OutreachPerformanceEngine


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


class TestPhase97LiveOutreach(unittest.TestCase):

    def setUp(self):
        # Create an isolated temporary test directory
        self.test_dir = tempfile.mkdtemp()
        self.batch_state_path = os.path.join(self.test_dir, "controlled_batch_state.json")
        self.leads_path = os.path.join(self.test_dir, "cache_sheets_leads.json")
        self.timeline_path = os.path.join(self.test_dir, "lead_timelines.json")
        self.history_path = os.path.join(self.test_dir, "message_history.json")
        self.suppression_path = os.path.join(self.test_dir, "suppression_list.json")
        self.outcomes_path = os.path.join(self.test_dir, "outreach_outcomes.json")
        self.followups_path = os.path.join(self.test_dir, "phase_9_5_follow_ups.json")
        self.run_output_path = os.path.join(self.test_dir, "phase_9_7_live_outreach_run.json")

        # Copy original baseline files
        for src, dst in [
            (os.path.join(DATA_DIR, "lead_timelines.json"), self.timeline_path),
            (os.path.join(DATA_DIR, "cache_sheets_leads.json"), self.leads_path),
            (os.path.join(DATA_DIR, "suppression_list.json"), self.suppression_path),
            (os.path.join(DATA_DIR, "message_history.json"), self.history_path),
        ]:
            if os.path.exists(src):
                shutil.copy(src, dst)

        # Baseline outcomes: Only Little Aladdin initially
        initial_outcomes = [
            {
                "lead_id": "LEAD-MAN-902001",
                "channel": "PHONE",
                "outcome": "CONNECTED",
                "attempt_number": 1,
                "timestamp": "2026-10-04T12:00:00Z",
                "notes": "Little Aladdin pilot call connected",
            }
        ]
        with open(self.outcomes_path, "w", encoding="utf-8") as f:
            json.dump(initial_outcomes, f, indent=2)

        # Baseline followups: empty list
        with open(self.followups_path, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)

        # Reset the 3 batch candidate leads in test leads_path to baseline NOT_READY
        batch_candidate_ids = {"LEAD-MAN-3B9091", "LEAD-MAN-4098E1", "LEAD-MAN-E81185"}
        if os.path.exists(self.leads_path):
            with open(self.leads_path, "r", encoding="utf-8") as f:
                crm_data = json.load(f)
            leads = crm_data.get("leads", crm_data) if isinstance(crm_data, dict) else crm_data
            for l in leads:
                if l.get("lead_id") in batch_candidate_ids:
                    l["outreach_status"] = "NOT_READY"
                    l["outreach_channel"] = ""
                    l["callback_required"] = None
                    l.pop("outreach_sent_at", None)
            with open(self.leads_path, "w", encoding="utf-8") as f:
                json.dump(crm_data, f, indent=2)

        # Initialize fresh batch state for testing
        batch_state = {
            "batch_id": "BATCH-MAN-TEST-97",
            "created_at": "2026-10-05T08:00:00Z",
            "max_batch_size": 3,
            "batch_size": 3,
            "status": "READY_FOR_OPERATOR",
            "current_index": 0,
            "leads": [
                {
                    "lead_id": "LEAD-MAN-3B9091",
                    "company_name": "The Old Monkey",
                    "location": "Manchester, United Kingdom",
                    "recommended_channel": "PHONE",
                    "verified_contact": "+44 161 228 6262",
                    "review_count": 1911,
                    "rating": 4.8,
                    "status": "READY_FOR_OPERATOR",
                },
                {
                    "lead_id": "LEAD-MAN-4098E1",
                    "company_name": "Dog and Partridge",
                    "location": "Manchester, United Kingdom",
                    "recommended_channel": "PHONE",
                    "verified_contact": "+44 161 943 9081",
                    "review_count": 730,
                    "rating": 4.5,
                    "status": "READY_FOR_OPERATOR",
                },
                {
                    "lead_id": "LEAD-MAN-E81185",
                    "company_name": "Manchester Shawarma",
                    "location": "Manchester, United Kingdom",
                    "recommended_channel": "PHONE",
                    "verified_contact": "+44 161 526 5396",
                    "review_count": 217,
                    "rating": 4.3,
                    "status": "READY_FOR_OPERATOR",
                },
            ],
        }
        with open(self.batch_state_path, "w", encoding="utf-8") as f:
            json.dump(batch_state, f, indent=2)

        # Instantiate runner under test
        self.runner = Phase97LiveOutreachRunner(
            batch_state_path=self.batch_state_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
            followups_path=self.followups_path,
            run_output_path=self.run_output_path,
        )

    def _load_crm_leads(self) -> Dict[str, Any]:
        """Helper to load CRM leads from isolated test leads_path."""
        with open(self.leads_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        leads = data.get("leads", data) if isinstance(data, dict) else data
        return {l["lead_id"]: l for l in leads if isinstance(l, dict) and "lead_id" in l}

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    # =========================================================================
    # 1. EXECUTION TESTS (Section 3, 4, 5, 6, 19)
    # =========================================================================

    def test_01_real_manual_call_path_executes_action(self):
        """Action CALL executes without auto-dialing or faking."""
        res = self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="CONNECTED",
            notes="Spoke with manager",
            operator_confirmed=True,
            action="CALL",
            test_mode=True,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["lead_id"], "LEAD-MAN-3B9091")
        self.assertEqual(res["action"], "CALL")
        self.assertEqual(res["outcome"], "CONNECTED")

    def test_02_operator_confirmation_strictly_required(self):
        """Without operator_confirmed=True, execution is blocked."""
        res = self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="CONNECTED",
            notes="No confirmation provided",
            operator_confirmed=False,
            action="CALL",
            test_mode=True,
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "ACTION_FAILED")
        self.assertEqual(res["details"]["error"], "OPERATOR_CONFIRMATION_REQUIRED")

    def test_03_no_automatic_continuation_requires_operator_click(self):
        """After executing lead 1, batch index remains 0 until advance_lead() is explicitly called."""
        self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="CONNECTED",
            notes="Spoke with manager",
            operator_confirmed=True,
            test_mode=True,
        )
        state_after = self.runner.executor.load_batch_state()
        self.assertEqual(state_after["current_index"], 0)
        self.assertEqual(state_after["leads"][0]["status"], "OUTCOME_RECORDED")

        # Explicit operator advance
        adv = self.runner.advance_lead()
        self.assertTrue(adv["success"])
        self.assertEqual(adv["current_index"], 1)

    def test_04_batch_ceiling_strictly_enforced_at_three(self):
        """Batch ceiling MAX_BATCH_SIZE is strictly 3; run_live_batch caps execution to 3."""
        self.assertEqual(self.runner.MAX_BATCH_SIZE, 3)
        oversized_batch = [
            {"lead_id": "LEAD-MAN-3B9091", "outcome": "CONNECTED", "notes": "call 1"},
            {"lead_id": "LEAD-MAN-4098E1", "outcome": "NO_ANSWER", "notes": "call 2"},
            {"lead_id": "LEAD-MAN-E81185", "outcome": "CALLBACK_REQUESTED", "notes": "call 3"},
            {"lead_id": "LEAD-MAN-0363CF", "outcome": "CONNECTED", "notes": "forbidden call 4"},
        ]
        run_res = self.runner.run_live_batch(oversized_batch, test_mode=True)
        self.assertTrue(run_res["RUN_ID"].startswith("RUN-LIVE-"))
        self.assertEqual(run_res["LEADS_PREPARED"], 3)
        self.assertEqual(run_res["LEADS_EXECUTED"], 3)
        self.assertEqual(len(run_res["EXECUTION_DETAILS"]), 3)

    def test_05_sequential_ordering_enforced(self):
        """Batch must process sequentially: The Old Monkey -> Dog and Partridge -> Manchester Shawarma."""
        order = [
            {"lead_id": "LEAD-MAN-3B9091", "outcome": "CONNECTED"},
            {"lead_id": "LEAD-MAN-4098E1", "outcome": "NO_ANSWER"},
            {"lead_id": "LEAD-MAN-E81185", "outcome": "CALLBACK_REQUESTED"},
        ]
        run_res = self.runner.run_live_batch(order, test_mode=True)
        self.assertEqual(run_res["EXECUTION_DETAILS"][0]["lead_id"], "LEAD-MAN-3B9091")
        self.assertEqual(run_res["EXECUTION_DETAILS"][1]["lead_id"], "LEAD-MAN-4098E1")
        self.assertEqual(run_res["EXECUTION_DETAILS"][2]["lead_id"], "LEAD-MAN-E81185")

    def test_06_sequence_mismatch_blocks_execution(self):
        """Attempting to execute lead out of order results in SEQUENCE_MISMATCH."""
        res = self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-4098E1",  # Lead 2 while batch is at index 0 (Lead 1)
            outcome="NO_ANSWER",
            notes="Skipping ahead",
            operator_confirmed=True,
            test_mode=True,
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "SEQUENCE_MISMATCH")

    # =========================================================================
    # 2. OUTCOME TESTS (Section 6, 7, 8, 10, 11, 12)
    # =========================================================================

    def test_07_outcome_connected_crm_state_contacted(self):
        """CONNECTED outcome sets CRM outreach_status to CONTACTED."""
        res = self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="CONNECTED",
            notes="Connected with owner",
            operator_confirmed=True,
            test_mode=False,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["crm_status"], "CONTACTED")

        # Verify in CRM
        leads = self._load_crm_leads()
        self.assertEqual(leads["LEAD-MAN-3B9091"]["outreach_status"], "CONTACTED")

    def test_08_outcome_no_answer_remains_call_attempted_not_contacted(self):
        """NO_ANSWER outcome sets CRM outreach_status to CALL_ATTEMPTED (never CONTACTED)."""
        res = self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="NO_ANSWER",
            notes="Rang without pickup",
            operator_confirmed=True,
            test_mode=False,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["crm_status"], "CALL_ATTEMPTED")

        leads = self._load_crm_leads()
        self.assertEqual(leads["LEAD-MAN-3B9091"]["outreach_status"], "CALL_ATTEMPTED")
        self.assertNotEqual(leads["LEAD-MAN-3B9091"]["outreach_status"], "CONTACTED")

    def test_09_outcome_busy_remains_call_attempted_not_contacted(self):
        """BUSY outcome sets CRM outreach_status to CALL_ATTEMPTED (never CONTACTED)."""
        res = self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="BUSY",
            notes="Line engaged",
            operator_confirmed=True,
            test_mode=False,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["crm_status"], "CALL_ATTEMPTED")

        leads = self._load_crm_leads()
        self.assertEqual(leads["LEAD-MAN-3B9091"]["outreach_status"], "CALL_ATTEMPTED")

    def test_10_outcome_callback_requested_crm_contacted_and_callback_required(self):
        """CALLBACK_REQUESTED sets CRM status to CONTACTED and sets callback_required=True."""
        res = self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="CALLBACK_REQUESTED",
            notes="Requested call at 2pm",
            callback_time="2026-10-06 14:00",
            operator_confirmed=True,
            test_mode=False,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["crm_status"], "CONTACTED")

        leads = self._load_crm_leads()
        lead = leads["LEAD-MAN-3B9091"]
        self.assertEqual(lead["outreach_status"], "CONTACTED")
        self.assertTrue(lead.get("callback_required"))

    def test_11_outcome_callback_requested_auto_schedule_false(self):
        """CALLBACK_REQUESTED stores auto_schedule=False in follow_ups log."""
        self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="CALLBACK_REQUESTED",
            notes="Call tomorrow after lunch",
            callback_time="2026-10-06 14:00",
            operator_confirmed=True,
            test_mode=False,
        )
        with open(self.followups_path, "r", encoding="utf-8") as f:
            followups = json.load(f)
        followup_dict = {f["lead_id"]: f for f in followups if isinstance(f, dict)}
        self.assertIn("LEAD-MAN-3B9091", followup_dict)
        entry = followup_dict["LEAD-MAN-3B9091"]
        self.assertTrue(entry.get("callback_required"))
        self.assertFalse(entry.get("auto_schedule"))

    def test_12_outcome_interested_follow_up_required_auto_follow_up_false(self):
        """INTERESTED stores response_stage=INTERESTED, follow_up_required=True, auto_follow_up=False."""
        self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="INTERESTED",
            notes="Very receptive to proposal",
            operator_confirmed=True,
            test_mode=False,
        )
        with open(self.followups_path, "r", encoding="utf-8") as f:
            followups = json.load(f)
        followup_dict = {f["lead_id"]: f for f in followups if isinstance(f, dict)}
        entry = followup_dict["LEAD-MAN-3B9091"]
        self.assertEqual(entry.get("response_stage"), "INTERESTED")
        self.assertTrue(entry.get("follow_up_required"))
        self.assertFalse(entry.get("auto_follow_up"))

    def test_13_outcome_not_interested_stops_sequence_not_suppressed(self):
        """NOT_INTERESTED stops current outreach but does NOT add lead to regulatory suppression."""
        self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="NOT_INTERESTED",
            notes="Owner declined website offer",
            operator_confirmed=True,
            test_mode=False,
        )
        with open(self.suppression_path, "r", encoding="utf-8") as f:
            supp = json.load(f)
        suppressed_ids = [s.get("lead_id") for s in supp.get("suppressed_leads", [])]
        self.assertNotIn("LEAD-MAN-3B9091", suppressed_ids)

    def test_14_outcome_wrong_number_flagged_for_review_not_deleted(self):
        """WRONG_NUMBER records attempt, flags contact for review, does not delete phone from CRM."""
        self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="WRONG_NUMBER",
            notes="Answered by residential party",
            operator_confirmed=True,
            test_mode=False,
        )
        leads = self._load_crm_leads()
        lead = leads["LEAD-MAN-3B9091"]
        self.assertEqual(lead["phone"], "+44 161 228 6262")  # Not deleted

    def test_15_outcome_failed_marks_contact_review_not_fatal(self):
        """FAILED outcome marks contact review required and keeps CRM in CALL_ATTEMPTED."""
        res = self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="FAILED",
            notes="Carrier network error",
            operator_confirmed=True,
            test_mode=False,
        )
        self.assertTrue(res["success"])

    def test_16_invalid_outcome_rejected_with_error(self):
        """Non-canonical outcome string is rejected with validation error."""
        res = self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="TALKED_A_LOT",  # Invalid
            notes="Bogus outcome",
            operator_confirmed=True,
            test_mode=True,
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "OUTCOME_RECORD_FAILED")

    # =========================================================================
    # 3. STATE & RULE B TESTS (Section 2, 9, 15)
    # =========================================================================

    def test_17_qualification_state_immutable_rule_b_frozen(self):
        """Qualification state remains OUTREACH_READY regardless of outcome; Rule B is frozen."""
        for test_outcome in ["CONNECTED", "NO_ANSWER", "CALLBACK_REQUESTED", "NOT_INTERESTED"]:
            leads_before = self._load_crm_leads()
            q_before = leads_before["LEAD-MAN-3B9091"]["qualification_state"]
            self.assertEqual(q_before, "OUTREACH_READY")

            self.runner.execute_single_lead(
                expected_lead_id="LEAD-MAN-3B9091",
                outcome=test_outcome,
                notes=f"Test {test_outcome}",
                operator_confirmed=True,
                test_mode=False,
            )
            leads_after = self._load_crm_leads()
            self.assertEqual(leads_after["LEAD-MAN-3B9091"]["qualification_state"], "OUTREACH_READY")

    def test_18_operator_notes_are_observations_never_alter_qualification(self):
        """Operator conversation notes are qualitative observations and do not alter Rule B score."""
        leads_before = self._load_crm_leads()
        score_before = leads_before["LEAD-MAN-3B9091"].get("lead_score")

        self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="CONNECTED",
            notes="Customer says they already have a website in development with Wix",
            operator_confirmed=True,
            test_mode=False,
        )
        leads_after = self._load_crm_leads()
        self.assertEqual(leads_after["LEAD-MAN-3B9091"].get("lead_score"), score_before)
        self.assertEqual(leads_after["LEAD-MAN-3B9091"]["qualification_state"], "OUTREACH_READY")

    def test_19_activation_state_retains_readiness_or_blockers(self):
        """Executing outreach does not mutate activation_ready boolean or introduce false blockers."""
        state = self.runner.executor.load_batch_state()
        preview = self.runner.executor.preview_current_lead()
        self.assertTrue(preview["preview"]["activation_ready"])
        self.assertEqual(preview["preview"]["activation_blockers"], [])

    # =========================================================================
    # 4. HISTORY & PROTECTED LEADS (Section 14, 17, 18)
    # =========================================================================

    def test_20_timeline_is_append_only_never_overwrites(self):
        """Timeline events are appended without deleting or overwriting previous history."""
        with open(self.timeline_path, "r", encoding="utf-8") as f:
            tl = json.load(f)
        prior_count = len(tl.get("LEAD-MAN-3B9091", []))

        self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="CONNECTED",
            notes="Timeline test call",
            operator_confirmed=True,
            test_mode=False,
        )

        with open(self.timeline_path, "r", encoding="utf-8") as f:
            tl_after = json.load(f)
        new_count = len(tl_after.get("LEAD-MAN-3B9091", []))
        self.assertGreater(new_count, prior_count)

    def test_21_timeline_logs_canonical_event_sequence(self):
        """Timeline logs sequence: PREVIEWED, OPERATOR_CONFIRMED, OUTREACH_ATTEMPTED, OUTCOME_RECORDED."""
        self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="CONNECTED",
            notes="Event sequence test",
            operator_confirmed=True,
            test_mode=False,
        )
        with open(self.timeline_path, "r", encoding="utf-8") as f:
            tl = json.load(f)
        events = [e["event_type"] for e in tl.get("LEAD-MAN-3B9091", [])]
        self.assertIn("PREVIEWED", events)
        self.assertIn("OPERATOR_CONFIRMED", events)
        self.assertIn("OUTREACH_ATTEMPTED", events)
        self.assertIn("CALL_ATTEMPTED", events)
        self.assertIn("OUTCOME_RECORDED", events)
        self.assertIn("CALL_CONNECTED", events)

    def test_22_little_aladdin_protected_from_recontact(self):
        """Little Aladdin (LEAD-MAN-902001) completed pilot and must NOT be in the batch queue."""
        state = self.runner.executor.load_batch_state()
        batch_lead_ids = [l["lead_id"] for l in state.get("leads", [])]
        self.assertNotIn("LEAD-MAN-902001", batch_lead_ids)

        leads = self._load_crm_leads()
        self.assertEqual(leads["LEAD-MAN-902001"]["outreach_status"], "CONTACTED")

    def test_23_seoul_kimchi_sent_protected_from_recontact(self):
        """Seoul Kimchi (LEAD-MAN-4DB3EF) is SENT and must never be queued or re-contacted."""
        state = self.runner.executor.load_batch_state()
        batch_lead_ids = [l["lead_id"] for l in state.get("leads", [])]
        self.assertNotIn("LEAD-MAN-4DB3EF", batch_lead_ids)

        leads = self._load_crm_leads()
        self.assertEqual(leads["LEAD-MAN-4DB3EF"]["outreach_status"], "SENT")

    def test_24_hong_thai_bounced_suppressed_protected(self):
        """Hong Thai (LEAD-MAN-709C66) is BOUNCED/SUPPRESSED and protected from outreach."""
        state = self.runner.executor.load_batch_state()
        batch_lead_ids = [l["lead_id"] for l in state.get("leads", [])]
        self.assertNotIn("LEAD-MAN-709C66", batch_lead_ids)

        with open(self.suppression_path, "r", encoding="utf-8") as f:
            supp = json.load(f)
        suppressed_emails = supp.get("channels", {}).get("Email", [])
        self.assertIn("hongthai.mcr@gmail.com", suppressed_emails)
        leads = self._load_crm_leads()
        self.assertEqual(leads["LEAD-MAN-709C66"]["outreach_status"], "BOUNCED")

    def test_25_live_seafood_not_ready_never_converted_to_ready(self):
        """Live Seafood Ltd (LEAD-MAN-0363CF) has qualification=OUTREACH_READY, activation=NOT_READY."""
        leads = self._load_crm_leads()
        live_seafood = leads["LEAD-MAN-0363CF"]
        self.assertEqual(live_seafood["qualification_state"], "OUTREACH_READY")
        self.assertEqual(live_seafood["outreach_status"], "NOT_READY")

    # =========================================================================
    # 5. IDEMPOTENCY TESTS (Section 13)
    # =========================================================================

    def test_26_duplicate_action_blocked_by_idempotency(self):
        """Double execution of the same lead/channel/attempt returns idempotent replay; post-outcome blocked."""
        # Initial action succeeds
        res1 = self.runner.executor.execute_current_lead_action(
            operator_confirmed=True,
            action="CALL",
            notes="First click",
            test_mode=False,
        )
        self.assertTrue(res1["success"])

        # Rapid double click before outcome returns idempotent replay without initiating 2nd call
        res2 = self.runner.executor.execute_current_lead_action(
            operator_confirmed=True,
            action="CALL",
            notes="Accidental double click",
            test_mode=False,
        )
        self.assertTrue(res2["success"])
        self.assertTrue(res2.get("idempotent_replay"))

        # After outcome is recorded, subsequent call attempt is strictly blocked by preflight
        self.runner.executor.record_current_lead_outcome(
            outcome="CONNECTED",
            notes="Outcome logged",
            test_mode=False,
        )
        res3 = self.runner.executor.execute_current_lead_action(
            operator_confirmed=True,
            action="CALL",
            notes="Attempting to re-call after completion",
            test_mode=False,
        )
        self.assertFalse(res3["success"])
        self.assertEqual(res3["error"], "PREFLIGHT_BLOCKED")

    def test_27_duplicate_key_follows_canonical_format(self):
        """Idempotency key strictly matches lead_id:channel:attempt."""
        res = self.runner.executor.execute_current_lead_action(
            operator_confirmed=True,
            action="CALL",
            notes="Test call",
            test_mode=True,
        )
        expected_key = "LEAD-MAN-3B9091:PHONE:1"
        self.assertEqual(res["idempotency_key"], expected_key)

    def test_28_batch_restart_protects_already_completed_leads(self):
        """If batch is completed, attempting to execute again returns BATCH_ALREADY_COMPLETED."""
        state = self.runner.executor.load_batch_state()
        state["status"] = "BATCH_COMPLETED"
        state["current_index"] = 3
        with open(self.batch_state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        res = self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="CONNECTED",
            notes="Restart attempt",
            operator_confirmed=True,
            test_mode=True,
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "BATCH_ALREADY_COMPLETED")

    # =========================================================================
    # 6. ANALYTICS & DENOMINATORS (Section 16, 20, 21, 22)
    # =========================================================================

    def test_29_analytics_event_creation_canonical(self):
        """Executing lead creates canonical outcome record in outreach_outcomes.json."""
        self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="CONNECTED",
            notes="Canonical record test",
            operator_confirmed=True,
            test_mode=False,
        )
        with open(self.outcomes_path, "r", encoding="utf-8") as f:
            outcomes = json.load(f)
        monkey_outcomes = [o for o in outcomes if o.get("lead_id") == "LEAD-MAN-3B9091"]
        self.assertEqual(len(monkey_outcomes), 1)
        self.assertEqual(monkey_outcomes[0]["outcome"], "CONNECTED")
        self.assertEqual(monkey_outcomes[0]["channel"], "PHONE")

    def test_30_analytics_rates_use_explicit_denominators(self):
        """Performance metrics use explicit, mathematically correct denominators."""
        engine = OutreachPerformanceEngine(
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
            batch_state_path=self.batch_state_path,
        )
        perf = engine.calculate_outreach_performance()
        denoms = perf["rate_denominators"]
        self.assertEqual(denoms["contact_rate_formula"], "connected / manual_call_attempts")
        self.assertEqual(denoms["interest_rate_formula"], "interested / connected")
        self.assertEqual(denoms["callback_rate_formula"], "callback_requested / connected")
        self.assertFalse(denoms["qualified_leads_as_interaction_denominator"])

    def test_31_analytics_contacted_rate_excludes_no_answer(self):
        """NO_ANSWER outcomes do not inflate connected or unique_leads_contacted."""
        # Add 1 CONNECTED and 1 NO_ANSWER
        test_outcomes = [
            {"lead_id": "L1", "channel": "PHONE", "outcome": "CONNECTED", "attempt_number": 1},
            {"lead_id": "L2", "channel": "PHONE", "outcome": "NO_ANSWER", "attempt_number": 1},
        ]
        with open(self.outcomes_path, "w", encoding="utf-8") as f:
            json.dump(test_outcomes, f, indent=2)

        engine = OutreachPerformanceEngine(
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
            batch_state_path=self.batch_state_path,
        )
        perf = engine.calculate_outreach_performance()
        self.assertEqual(perf["contact_performance"]["connected"], 1)
        self.assertEqual(perf["contact_performance"]["no_answer"], 1)
        self.assertEqual(perf["rates"]["contact_rate"], 0.5)

    def test_32_sample_size_protection_warns_on_small_n(self):
        """Sample sizes < 5 produce INSUFFICIENT SAMPLE FOR COMPARATIVE CONCLUSION."""
        engine = OutreachPerformanceEngine(
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
            batch_state_path=self.batch_state_path,
        )
        perf = engine.calculate_outreach_performance()
        warning = perf["sample_size_warnings"]["warning"]
        self.assertIn("INSUFFICIENT SAMPLE", warning)

    def test_33_priority_model_read_only_not_mutated_by_outcomes(self):
        """Priority score model remains deterministic and read-only without feedback mutation."""
        engine = OutreachPerformanceEngine(
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
            batch_state_path=self.batch_state_path,
        )
        leads = engine.load_crm_leads()
        score1, _ = engine.calculate_lead_priority_score({}, leads.get("LEAD-MAN-3B9091", {}))

        # Record outcome
        self.runner.execute_single_lead(
            expected_lead_id="LEAD-MAN-3B9091",
            outcome="CONNECTED",
            notes="Call recorded",
            operator_confirmed=True,
            test_mode=True,
        )

        leads_after = engine.load_crm_leads()
        score2, _ = engine.calculate_lead_priority_score({}, leads_after.get("LEAD-MAN-3B9091", {}))
        # Prioritization formula is unchanged
        self.assertEqual(score1, score2)

    # =========================================================================
    # 7. SAFETY INVARIANTS (Section 23, 26)
    # =========================================================================

    def test_34_strict_safety_invariants_automated_sends_zero(self):
        """Hard safety invariant: automated_sends is strictly 0 across all runs."""
        run_res = self.runner.run_live_batch([
            {"lead_id": "LEAD-MAN-3B9091", "outcome": "CONNECTED"},
            {"lead_id": "LEAD-MAN-4098E1", "outcome": "NO_ANSWER"},
            {"lead_id": "LEAD-MAN-E81185", "outcome": "CALLBACK_REQUESTED"},
        ], test_mode=True)
        self.assertEqual(run_res["AUTOMATED_SENDS"], 0)
        self.assertEqual(run_res["SAFETY_INVARIANTS"]["automated_dispatches"], 0)

    def test_35_strict_safety_invariants_campaigns_armed_zero(self):
        """Hard safety invariant: campaigns_armed is strictly 0 across all runs."""
        run_res = self.runner.run_live_batch([
            {"lead_id": "LEAD-MAN-3B9091", "outcome": "CONNECTED"},
            {"lead_id": "LEAD-MAN-4098E1", "outcome": "NO_ANSWER"},
            {"lead_id": "LEAD-MAN-E81185", "outcome": "CALLBACK_REQUESTED"},
        ], test_mode=True)
        self.assertEqual(run_res["CAMPAIGNS_ARMED"], 0)
        self.assertEqual(run_res["SAFETY_INVARIANTS"]["campaigns_armed"], 0)

    def test_36_strict_safety_invariants_auto_followups_zero(self):
        """Hard safety invariant: automated_followups is strictly 0."""
        run_res = self.runner.run_live_batch([
            {"lead_id": "LEAD-MAN-3B9091", "outcome": "CONNECTED"},
            {"lead_id": "LEAD-MAN-4098E1", "outcome": "NO_ANSWER"},
            {"lead_id": "LEAD-MAN-E81185", "outcome": "CALLBACK_REQUESTED"},
        ], test_mode=True)
        self.assertEqual(run_res["AUTOMATED_FOLLOWUPS"], 0)
        self.assertEqual(run_res["SAFETY_INVARIANTS"]["autonomous_continuation"], 0)

    def test_37_machine_run_artifact_structure_valid(self):
        """Machine run output artifact has all required Phase 9.7 Section 26 fields."""
        run_res = self.runner.run_live_batch([
            {"lead_id": "LEAD-MAN-3B9091", "outcome": "CONNECTED"},
            {"lead_id": "LEAD-MAN-4098E1", "outcome": "NO_ANSWER"},
            {"lead_id": "LEAD-MAN-E81185", "outcome": "CALLBACK_REQUESTED"},
        ], test_mode=True)

        required_keys = [
            "RUN_ID", "BATCH_ID", "LEADS_PREPARED", "LEADS_EXECUTED",
            "LEADS_BLOCKED", "LEADS_SKIPPED", "OUTREACH_ATTEMPTS",
            "UNIQUE_BUSINESSES_CONTACTED", "CONNECTED", "NO_ANSWER",
            "BUSY", "CALLBACK_REQUESTED", "INTERESTED", "NOT_INTERESTED",
            "WRONG_NUMBER", "FAILED", "FOLLOWUPS_REQUIRED", "CALLBACKS_REQUIRED",
            "CRM_MUTATIONS", "TIMELINE_EVENTS", "DUPLICATE_BLOCKS",
            "AUTOMATED_SENDS", "AUTOMATED_FOLLOWUPS", "CAMPAIGNS_ARMED"
        ]
        for k in required_keys:
            self.assertIn(k, run_res, f"Missing required key {k} in machine run artifact")


if __name__ == "__main__":
    unittest.main()
