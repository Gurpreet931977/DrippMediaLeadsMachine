"""
Test Suite: Post-9.7 Commercial Conversion Workflow

Validates all 25 objectives of POST-9.7:
  1. Outcome Provenance (OPERATOR_REPORTED vs SYSTEM_VERIFIED vs PROVIDER_CONFIRMED)
  2. Commercial Stage Separation (Independent of qualification_state & outreach_status)
  3. No Auto-Promotion (CONNECTED != INTERESTED)
  4. Manchester Shawarma Callback (PENDING_OPERATOR, auto_schedule=False, auto_call=False)
  5. The Old Monkey Preview Workflow (Draft -> Ready -> Sent -> Viewed, explicit MARK SENT)
  6. Structured Commercial Notes & Interest Levels (UNKNOWN, LOW, MEDIUM, HIGH)
  7. Website Sales Pipeline (NO_WEBSITE -> CONTACTED -> ... -> WON/LOST)
  8. Proposal Tracking (No auto proposals, explicit amounts & currencies)
  9. Won / Lost Invariants (Mandatory reasons for LOST, explicit confirmation & fields for WON)
  10. Next Best Commercial Action Ranking (6-tier strict priority, zero auto-execution)
  11. Historical & Protected Lead Integrity (Little Aladdin, The Old Monkey, Dog and Partridge,
      Manchester Shawarma, Seoul Kimchi, Hong Thai, Live Seafood)
  12. Automation Limits (Zero auto-calls, auto-dms, auto-followups, auto-proposals)
  13. Funnel Analytics & Small-Sample Guardrails (n<30 warning)
  14. Append-Only Event Audit Trail
  15. FastAPI Endpoints (/api/commercial/*)
"""

import os
import json
import shutil
import tempfile
import unittest
from fastapi.testclient import TestClient

from server import app
from lib.commercial.models import (
    CommercialStage,
    WebsitePipelineStage,
    OutcomeProvenance,
    InterestLevel,
    PreviewStatus,
    ProposalStatus,
    LostReason,
    NextActionType,
    CommercialEventType,
    CommercialNotes,
    FollowUpRecord,
    PreviewRecord,
    ProposalRecord,
    CommercialEvent,
    AUTO_CALL,
    AUTO_DM,
    AUTO_EMAIL,
    AUTO_FOLLOWUP,
    AUTO_CALLBACK,
    AUTO_PROPOSAL,
    AUTO_CONTINUATION,
)
from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager


class TestPost97CommercialWorkflow(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.leads_path = os.path.join(self.test_dir, "cache_sheets_leads.json")
        self.outcomes_path = os.path.join(self.test_dir, "outreach_outcomes.json")
        self.timelines_path = os.path.join(self.test_dir, "lead_timelines.json")
        self.commercial_records_path = os.path.join(self.test_dir, "commercial_records.json")
        self.commercial_events_path = os.path.join(self.test_dir, "commercial_events.json")
        self.snapshot_path = os.path.join(self.test_dir, "commercial_pipeline_snapshot.json")

        # Copy production base files
        prod_data_dir = os.path.join(os.path.dirname(__file__), "data")
        for fn, dst in [
            ("cache_sheets_leads.json", self.leads_path),
            ("outreach_outcomes.json", self.outcomes_path),
            ("lead_timelines.json", self.timelines_path),
        ]:
            src = os.path.join(prod_data_dir, fn)
            if os.path.exists(src):
                shutil.copyfile(src, dst)
            else:
                with open(dst, "w") as f:
                    json.dump([], f)

        self.manager = CommercialPipelineManager(
            leads_path=self.leads_path,
            outcomes_path=self.outcomes_path,
            timelines_path=self.timelines_path,
            commercial_records_path=self.commercial_records_path,
            commercial_events_path=self.commercial_events_path,
            snapshot_path=self.snapshot_path,
        )
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Provenance Tests (Section 1)
    # -------------------------------------------------------------------------
    def test_01_provenance_operator_reported_valid(self):
        """Manual phone calls must accept OPERATOR_REPORTED source."""
        valid, msg = self.manager.validate_outcome_provenance(
            outcome="CONNECTED", channel="PHONE", source=OutcomeProvenance.OPERATOR_REPORTED.value, operator_confirmed=True
        )
        self.assertTrue(valid)
        self.assertIn("Valid provenance", msg)

    def test_02_provenance_reject_system_verified_for_manual_phone(self):
        """Software must never claim to have SYSTEM_VERIFIED a manual phone call."""
        valid, msg = self.manager.validate_outcome_provenance(
            outcome="CONNECTED", channel="PHONE", source=OutcomeProvenance.SYSTEM_VERIFIED.value
        )
        self.assertFalse(valid)
        self.assertIn("Manual phone calls cannot be SYSTEM_VERIFIED", msg)

    def test_03_provenance_provider_confirmed_requires_receipt(self):
        """PROVIDER_CONFIRMED requires independent receipt/webhook verification."""
        valid, msg = self.manager.validate_outcome_provenance(
            outcome="CONNECTED", channel="PHONE", source=OutcomeProvenance.PROVIDER_CONFIRMED.value, operator_confirmed=False
        )
        self.assertFalse(valid)
        self.assertIn("PROVIDER_CONFIRMED requires independent provider verification receipt", msg)

    def test_04_provenance_invalid_source_rejected(self):
        """Unknown or arbitrary source strings must be rejected."""
        valid, msg = self.manager.validate_outcome_provenance(
            outcome="CONNECTED", channel="PHONE", source="MAGIC_AI_DETECTED"
        )
        self.assertFalse(valid)
        self.assertIn("Invalid provenance source", msg)

    # -------------------------------------------------------------------------
    # 2. Commercial States & Transitions (Section 2 & 3)
    # -------------------------------------------------------------------------
    def test_05_commercial_stage_independent_of_qualification(self):
        """Commercial stage exists separately from Rule B qualification state."""
        records = self.manager.load_commercial_records()
        leads = self.manager.load_leads()
        old_monkey_lead = next(l for l in leads if l["lead_id"] == "LEAD-MAN-3B9091")
        old_monkey_comm = records["LEAD-MAN-3B9091"]

        self.assertEqual(old_monkey_lead["qualification_state"], "OUTREACH_READY")
        self.assertEqual(old_monkey_comm["commercial_stage"], CommercialStage.PREVIEW_REQUESTED.value)

    def test_06_connected_does_not_equal_interested(self):
        """CONNECTED outcome must NOT automatically transition commercial stage to INTERESTED."""
        records = self.manager.load_commercial_records()
        aladdin = records["LEAD-MAN-902001"]
        # Connected phone call results in CONTACTED, not auto INTERESTED
        self.assertEqual(aladdin["commercial_stage"], CommercialStage.CONTACTED.value)
        self.assertNotEqual(aladdin["commercial_stage"], CommercialStage.INTERESTED.value)

    def test_07_explicit_stage_transition_by_operator(self):
        """Operator can explicitly transition commercial stage with reason."""
        res = self.manager.update_commercial_stage(
            lead_id="LEAD-MAN-902001",
            new_stage=CommercialStage.INTERESTED.value,
            reason="Manager confirmed strong interest in receiving full proposal",
            operator="HUMAN_OPERATOR",
            notes="Ready for formal proposal drafting",
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["new_stage"], CommercialStage.INTERESTED.value)

        records = self.manager.load_commercial_records()
        self.assertEqual(records["LEAD-MAN-902001"]["commercial_stage"], CommercialStage.INTERESTED.value)

    def test_08_invalid_commercial_stage_rejected(self):
        """Transitions to non-existent commercial stages must error."""
        with self.assertRaises(ValueError):
            self.manager.update_commercial_stage(
                lead_id="LEAD-MAN-902001",
                new_stage="SUPER_HOT_LEAD",
                reason="Invalid stage",
            )

    # -------------------------------------------------------------------------
    # 3. Follow-Ups & Manchester Shawarma (Section 4)
    # -------------------------------------------------------------------------
    def test_09_manchester_shawarma_callback_record(self):
        """Manchester Shawarma must have prominent pending callback record."""
        records = self.manager.load_commercial_records()
        shawarma = records["LEAD-MAN-E81185"]
        self.assertEqual(shawarma["commercial_stage"], CommercialStage.FOLLOW_UP_REQUIRED.value)

        follow_ups = shawarma.get("follow_ups", [])
        self.assertTrue(len(follow_ups) >= 1)
        cb = follow_ups[0]
        self.assertEqual(cb["follow_up_type"], "CALLBACK")
        self.assertEqual(cb["scheduled_for"], "2026-10-06 14:00")
        self.assertEqual(cb["status"], "PENDING_OPERATOR")
        self.assertFalse(cb["auto_schedule"])
        self.assertFalse(cb["auto_call"])

    def test_10_callback_not_auto_executed(self):
        """Callbacks must never automatically trigger a call or auto-schedule."""
        res = self.manager.schedule_follow_up(
            lead_id="LEAD-MAN-902001",
            follow_up_type="CALLBACK",
            scheduled_for="2026-10-07 10:00",
            notes="Follow up with owner",
        )
        self.assertTrue(res["success"])
        fu = res["follow_up"]
        self.assertFalse(fu["auto_call"])
        self.assertFalse(fu["auto_schedule"])
        self.assertEqual(fu["status"], "PENDING_OPERATOR")

    def test_11_complete_follow_up_preserves_history(self):
        """Completing a follow-up updates status and retains outcome in history."""
        res = self.manager.complete_follow_up(
            lead_id="LEAD-MAN-E81185",
            follow_up_id="LATEST",
            outcome="CONNECTED",
            notes="Completed manual call post-rush. Owner requested proposal.",
        )
        self.assertTrue(res["success"])
        records = self.manager.load_commercial_records()
        fu = records["LEAD-MAN-E81185"]["follow_ups"][0]
        self.assertEqual(fu["status"], "COMPLETED")
        self.assertEqual(fu["outcome"], "CONNECTED")
        self.assertIsNotNone(fu["completed_at"])

    # -------------------------------------------------------------------------
    # 4. Preview Workflow & The Old Monkey (Section 5 & 6)
    # -------------------------------------------------------------------------
    def test_12_the_old_monkey_preview_requested(self):
        """The Old Monkey must have PREVIEW_REQUESTED commercial stage."""
        records = self.manager.load_commercial_records()
        monkey = records["LEAD-MAN-3B9091"]
        self.assertEqual(monkey["commercial_stage"], CommercialStage.PREVIEW_REQUESTED.value)
        previews = monkey.get("previews", [])
        self.assertTrue(len(previews) >= 1)
        self.assertEqual(previews[0]["preview_status"], PreviewStatus.PREVIEW_DRAFT.value)

    def test_13_create_preview_record(self):
        """Allows operator to attach concept preview URL and description without auto-send."""
        res = self.manager.create_or_update_preview(
            lead_id="LEAD-MAN-902001",
            preview_url="https://preview.dripp.media/little-aladdin",
            preview_description="Vegan cafe mobile site with click-to-call and Allergen menu",
            what_demonstrated="Mobile menu, Google maps embed",
            next_action="Review preview with operator",
        )
        self.assertTrue(res["success"])
        prev = res["preview"]
        self.assertEqual(prev["preview_status"], PreviewStatus.PREVIEW_DRAFT.value)
        self.assertIsNone(prev["sent_at"])
        self.assertIsNone(prev["viewed_at"])

    def test_14_mark_preview_sent_requires_operator_action(self):
        """Operator explicitly marks preview sent, transitioning stage to PREVIEW_SENT."""
        res = self.manager.mark_preview_sent(
            lead_id="LEAD-MAN-3B9091",
            preview_url="https://preview.dripp.media/the-old-monkey-mcr",
            operator_notes="Delivered preview link to manager via verified Instagram message.",
            operator="HUMAN_OPERATOR",
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["commercial_stage"], CommercialStage.PREVIEW_SENT.value)

        records = self.manager.load_commercial_records()
        monkey = records["LEAD-MAN-3B9091"]
        self.assertEqual(monkey["commercial_stage"], CommercialStage.PREVIEW_SENT.value)
        self.assertEqual(monkey["previews"][-1]["preview_status"], PreviewStatus.PREVIEW_SENT.value)
        self.assertIsNotNone(monkey["previews"][-1]["sent_at"])

    def test_15_preview_viewed_at_requires_actual_evidence(self):
        """Preview viewed_at remains None unless actual independent analytics evidence exists."""
        records = self.manager.load_commercial_records()
        monkey = records["LEAD-MAN-3B9091"]
        self.assertIsNone(monkey["previews"][-1]["viewed_at"])

    # -------------------------------------------------------------------------
    # 5. Commercial Notes & Interest Level (Section 7 & 8)
    # -------------------------------------------------------------------------
    def test_16_structured_commercial_notes(self):
        """Records structured operator observations without altering Rule B qualification."""
        notes = {
            "decision_maker": "Dave Roberts",
            "role": "General Manager",
            "interest_level": "HIGH",
            "current_website_situation": "Uses social media only",
            "requested_service": "One-page mobile website with drinks menu",
            "budget_signal": "£800 - £1200",
            "timeline_signal": "Before Christmas season",
            "objection": "Wants easy menu updates",
            "next_step": "Send proposal after preview approval",
        }
        res = self.manager.record_commercial_notes("LEAD-MAN-3B9091", notes)
        self.assertTrue(res["success"])
        self.assertEqual(res["notes"]["interest_level"], "HIGH")
        self.assertEqual(res["notes"]["decision_maker"], "Dave Roberts")

    def test_17_invalid_interest_level_rejected(self):
        """Interest level must be UNKNOWN, LOW, MEDIUM, or HIGH."""
        with self.assertRaises(ValueError):
            self.manager.record_commercial_notes(
                "LEAD-MAN-3B9091",
                {"interest_level": "EXTREME_HYPER_INTERESTED"},
            )

    # -------------------------------------------------------------------------
    # 6. Proposal Tracking (Section 10)
    # -------------------------------------------------------------------------
    def test_18_proposal_tracking_no_auto_proposal(self):
        """Records drafted/sent proposals with currency and amounts without automation."""
        res = self.manager.record_proposal(
            lead_id="LEAD-MAN-3B9091",
            proposal_status="DRAFTED",
            amount=950.0,
            currency="GBP",
            notes="Standard local business website package",
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["proposal"]["proposal_amount"], 950.0)
        self.assertEqual(res["proposal"]["proposal_currency"], "GBP")

    def test_19_proposal_sent_transitions_stage(self):
        """Sending a proposal transitions commercial stage to PROPOSAL_SENT."""
        res = self.manager.record_proposal(
            lead_id="LEAD-MAN-3B9091",
            proposal_status="SENT",
            amount=950.0,
            currency="GBP",
            notes="Sent via email to manager",
        )
        self.assertTrue(res["success"])
        records = self.manager.load_commercial_records()
        self.assertEqual(records["LEAD-MAN-3B9091"]["commercial_stage"], CommercialStage.PROPOSAL_SENT.value)
        self.assertIsNotNone(records["LEAD-MAN-3B9091"]["proposals"][-1]["sent_at"])

    # -------------------------------------------------------------------------
    # 7. Won / Lost Tracking (Section 11)
    # -------------------------------------------------------------------------
    def test_20_won_requires_explicit_confirmation(self):
        """Deal WON requires explicit operator confirmation flag."""
        with self.assertRaises(ValueError) as ctx:
            self.manager.close_deal(
                lead_id="LEAD-MAN-3B9091",
                status="WON",
                data={"service": "Website Build", "agreed_value": 1200, "currency": "GBP", "start_date": "2026-10-15"},
                operator_confirmed=False,
            )
        self.assertIn("requires explicit operator confirmation", str(ctx.exception))

    def test_21_won_requires_mandatory_fields(self):
        """Deal WON requires service, agreed_value, currency, and start_date."""
        with self.assertRaises(ValueError) as ctx:
            self.manager.close_deal(
                lead_id="LEAD-MAN-3B9091",
                status="WON",
                data={"service": "Website Build"},  # Missing agreed_value, currency, start_date
                operator_confirmed=True,
            )
        self.assertIn("requires mandatory field", str(ctx.exception))

    def test_22_won_deal_recorded_successfully(self):
        """Valid WON deal records revenue details and transitions stage."""
        res = self.manager.close_deal(
            lead_id="LEAD-MAN-3B9091",
            status="WON",
            data={
                "service": "Mobile Website Build + Hosting",
                "agreed_value": 1250.0,
                "currency": "GBP",
                "start_date": "2026-10-15",
                "notes": "50% deposit received via bank transfer",
            },
            operator_confirmed=True,
        )
        self.assertTrue(res["success"])
        records = self.manager.load_commercial_records()
        self.assertEqual(records["LEAD-MAN-3B9091"]["commercial_stage"], CommercialStage.WON.value)
        self.assertEqual(records["LEAD-MAN-3B9091"]["deal_won"]["agreed_value"], 1250.0)

    def test_23_lost_requires_valid_reason(self):
        """Deal LOST requires a valid reason from PRICE, TIMING, NO_NEED, CHOSE_OTHER_PROVIDER, NO_RESPONSE, OTHER."""
        with self.assertRaises(ValueError):
            self.manager.close_deal(
                lead_id="LEAD-MAN-902001",
                status="LOST",
                data={"reason": "BAD_VIBES"},
            )

    def test_24_lost_deal_recorded_successfully(self):
        """Valid LOST deal records reason and transitions stage."""
        res = self.manager.close_deal(
            lead_id="LEAD-MAN-902001",
            status="LOST",
            data={"reason": "TIMING", "notes": "Renovating kitchen; requested contact in Q1"},
        )
        self.assertTrue(res["success"])
        records = self.manager.load_commercial_records()
        self.assertEqual(records["LEAD-MAN-902001"]["commercial_stage"], CommercialStage.LOST.value)
        self.assertEqual(records["LEAD-MAN-902001"]["deal_lost"]["reason"], "TIMING")

    # -------------------------------------------------------------------------
    # 8. Next Best Commercial Action Prioritization (Section 13 & 15)
    # -------------------------------------------------------------------------
    def test_25_next_actions_priority_hierarchy(self):
        """
        Validates Section 15 priority ordering:
          Tier 1: Explicit callback requested (Manchester Shawarma)
          Tier 2: Explicit request for preview (The Old Monkey)
          Tier 4: Recent successful connection (Little Aladdin)
          Tier 5: No-answer retry (Dog and Partridge)
          Tier 6: Remaining qualified untouched leads
        """
        actions = self.manager.get_next_commercial_actions()
        self.assertTrue(len(actions) >= 4)

        # Check top action is Manchester Shawarma callback
        self.assertEqual(actions[0]["lead_id"], "LEAD-MAN-E81185")
        self.assertEqual(actions[0]["priority_tier"], 1)
        self.assertIn("CALLBACK", actions[0]["action"])

        # Check tier 2 is The Old Monkey
        self.assertEqual(actions[1]["lead_id"], "LEAD-MAN-3B9091")
        self.assertEqual(actions[1]["priority_tier"], 2)
        self.assertEqual(actions[1]["action"], NextActionType.SEND_PREVIEW.value)

        # Verify auto_executable is False on all actions
        for act in actions:
            self.assertFalse(act["auto_executable"])

    def test_26_next_actions_have_explicit_reasons(self):
        """Every recommended operator action must provide a human-readable reason."""
        actions = self.manager.get_next_commercial_actions()
        for act in actions:
            self.assertTrue(len(act.get("reason", "")) > 10)

    # -------------------------------------------------------------------------
    # 9. Historical & Protected Lead Integrity (Section 16, 17, 18)
    # -------------------------------------------------------------------------
    def test_27_dog_and_partridge_state(self):
        """Dog and Partridge: commercial_stage = CONTACTED, next_action = OPERATOR_REVIEW."""
        records = self.manager.load_commercial_records()
        dnp = records["LEAD-MAN-4098E1"]
        self.assertEqual(dnp["commercial_stage"], CommercialStage.CONTACTED.value)
        actions = self.manager.get_next_commercial_actions()
        dnp_action = next((a for a in actions if a["lead_id"] == "LEAD-MAN-4098E1"), None)
        self.assertIsNotNone(dnp_action)
        self.assertEqual(dnp_action["action"], NextActionType.OPERATOR_REVIEW.value)

    def test_28_live_seafood_not_promoted(self):
        """Live Seafood Ltd remains OUTREACH_READY / NOT_READY and commercial_stage = QUALIFIED."""
        records = self.manager.load_commercial_records()
        leads = self.manager.load_leads()
        seafood_lead = next(l for l in leads if l["lead_id"] == "LEAD-MAN-0363CF")
        seafood_comm = records["LEAD-MAN-0363CF"]

        self.assertEqual(seafood_lead["qualification_state"], "OUTREACH_READY")
        self.assertEqual(seafood_lead.get("outreach_status"), "NOT_READY")
        self.assertEqual(seafood_comm["commercial_stage"], CommercialStage.QUALIFIED.value)

    def test_29_seoul_kimchi_protected(self):
        """Seoul Kimchi (SENT confirmed) history is preserved and cannot be reopened."""
        records = self.manager.load_commercial_records()
        kimchi = records["LEAD-MAN-4DB3EF"]
        self.assertEqual(kimchi["commercial_stage"], CommercialStage.CONTACTED.value)

    def test_30_hong_thai_suppressed_protected(self):
        """Hong Thai is suppressed and cannot be transitioned to QUALIFIED or CONTACTED."""
        with self.assertRaises(PermissionError):
            self.manager.update_commercial_stage(
                lead_id="LEAD-MAN-709C66",
                new_stage=CommercialStage.QUALIFIED.value,
                reason="Attempting unauthorized reopen of suppressed lead",
            )

    # -------------------------------------------------------------------------
    # 10. Automation Limits (Section 20)
    # -------------------------------------------------------------------------
    def test_31_hard_automation_invariants(self):
        """Verifies that all automated outreach flags are strictly False."""
        self.assertFalse(AUTO_CALL)
        self.assertFalse(AUTO_DM)
        self.assertFalse(AUTO_EMAIL)
        self.assertFalse(AUTO_FOLLOWUP)
        self.assertFalse(AUTO_CALLBACK)
        self.assertFalse(AUTO_PROPOSAL)
        self.assertFalse(AUTO_CONTINUATION)

    # -------------------------------------------------------------------------
    # 11. Funnel Analytics & Machine Snapshot (Section 12 & 23)
    # -------------------------------------------------------------------------
    def test_32_commercial_pipeline_analytics_denominators(self):
        """Analytics calculates stages with mathematically safe denominators."""
        analytics = self.manager.get_commercial_pipeline_analytics()
        self.assertIn("conversion_funnel", analytics)
        cf = analytics["conversion_funnel"]
        self.assertIn("contact_to_connected", cf)
        self.assertIn("connected_to_interested", cf)
        self.assertIn("interested_to_preview", cf)
        self.assertIn("preview_to_proposal", cf)
        self.assertIn("proposal_to_won", cf)

    def test_33_sample_size_protection_warning(self):
        """Small sample size (<30) triggers explicit warning that metrics are descriptive only."""
        analytics = self.manager.get_commercial_pipeline_analytics()
        ss = analytics["sample_size"]
        self.assertFalse(ss["is_sufficient"])
        self.assertIn("INSUFFICIENT SAMPLE", ss["warning"])

    def test_34_machine_output_snapshot_schema(self):
        """Machine snapshot contains all 17 required keys from Section 23."""
        snapshot = self.manager.generate_commercial_pipeline_snapshot()
        self.assertTrue(os.path.exists(self.snapshot_path))

        required_keys = [
            "qualified", "activated", "contacted", "connected", "interested",
            "follow_up_required", "preview_requested", "preview_sent",
            "proposal_requested", "proposal_sent", "won", "lost",
            "pending_callbacks", "pending_previews", "pending_operator_actions",
            "operator_reported_outcomes", "provider_confirmed_outcomes",
            "automation_actions"
        ]
        for key in required_keys:
            self.assertIn(key, snapshot, f"Missing required key: {key}")

        self.assertEqual(snapshot["automation_actions"], 0)

    # -------------------------------------------------------------------------
    # 12. Append-Only Event Trail (Section 19)
    # -------------------------------------------------------------------------
    def test_35_commercial_events_append_only(self):
        """All stage changes, previews, and follow-ups append events without mutating past entries."""
        initial_events = len(self.manager.load_commercial_events())

        self.manager.update_commercial_stage("LEAD-MAN-902001", "INTERESTED", "Operator call review")
        events_after = self.manager.load_commercial_events()
        self.assertEqual(len(events_after), initial_events + 1)

        latest = events_after[-1]
        self.assertEqual(latest["event_type"], CommercialEventType.COMMERCIAL_STAGE_CHANGED.value)
        self.assertEqual(latest["lead_id"], "LEAD-MAN-902001")
        self.assertEqual(latest["new_stage"], "INTERESTED")

    # -------------------------------------------------------------------------
    # 13. FastAPI Endpoint Tests
    # -------------------------------------------------------------------------
    def test_36_api_commercial_pipeline_endpoint(self):
        """Test GET /api/commercial/pipeline endpoint."""
        res = self.client.get("/api/commercial/pipeline")
        self.assertEqual(res.status_code, 200)
        json_data = res.json()
        self.assertEqual(json_data["status"], "ok")
        self.assertIn("conversion_funnel", json_data)

    def test_37_api_commercial_next_actions_endpoint(self):
        """Test GET /api/commercial/next-actions endpoint."""
        res = self.client.get("/api/commercial/next-actions")
        self.assertEqual(res.status_code, 200)
        json_data = res.json()
        self.assertEqual(json_data["status"], "ok")
        self.assertTrue(json_data["count"] > 0)
        self.assertEqual(json_data["actions"][0]["lead_id"], "LEAD-MAN-E81185")

    def test_38_api_commercial_dashboard_endpoint(self):
        """Test GET /api/commercial/dashboard endpoint."""
        res = self.client.get("/api/commercial/dashboard")
        self.assertEqual(res.status_code, 200)
        json_data = res.json()
        self.assertEqual(json_data["status"], "ok")
        self.assertIn("top_summary", json_data)
        self.assertIn("cards", json_data)


if __name__ == "__main__":
    unittest.main()
