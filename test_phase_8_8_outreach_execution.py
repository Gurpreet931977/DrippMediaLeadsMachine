"""
Unit & Integration Tests for Phase 8.8: Real Manual Outreach Execution & Outcome Tracking

Covers:
 1. Active queue loads all four qualified leads (Live Seafood Ltd, Dog and Partridge, Ducie Arms, The Old Monkey).
 2. Lead canonical identity preservation (lead_id vs research_id, no fabricated IDs).
 3. Phone call scripts contain all 5 required structured components.
 4. Instagram direct message draft contains tailored commercial website angle.
 5. Unconfirmed outreach action is strictly blocked (operator_confirmed=True).
 6. Confirmed outreach action updates CRM cache, message history, audit log, and outcomes.
 7. Duplicate outreach action is strictly blocked on already contacted leads.
 8. Phone call distinction between connected (CONTACTED) and unanswered (CALL_ATTEMPTED).
 9. Customer response recording across valid stages with validation.
10. Administrative reset functionality with mandatory admin confirmation.
11. FastAPI endpoints (/queue, /daily-view, /analytics, /record-action, /record-response, /reset).
12. Strict safety invariants (OUTREACH_SENDS=0 baseline, zero automated dispatches, campaigns untouched).
"""

import os
import json
import shutil
import tempfile
import unittest
from datetime import datetime

from lib.outreach.phase_8_8_outreach_executor import (
    Phase88OutreachExecutor,
    VALID_OUTCOMES,
    VALID_INTEREST_LEVELS,
)
from fastapi.testclient import TestClient
from server import app


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


class TestPhase88OutreachExecution(unittest.TestCase):

    def setUp(self):
        # Create an isolated temporary test directory for tests that mutate state
        self.temp_dir = tempfile.mkdtemp()
        self.temp_leads = os.path.join(self.temp_dir, "cache_sheets_leads.json")
        self.temp_history = os.path.join(self.temp_dir, "message_history.json")
        self.temp_audit = os.path.join(self.temp_dir, "manual_outreach_audit.jsonl")
        self.temp_outcomes = os.path.join(self.temp_dir, "outreach_outcomes.json")
        self.temp_results = os.path.join(self.temp_dir, "phase_8_8_outreach_results.json")
        self.temp_report = os.path.join(self.temp_dir, "phase_8_8_outreach_results.md")

        # Copy original files to temp_dir
        shutil.copyfile(os.path.join(DATA_DIR, "cache_sheets_leads.json"), self.temp_leads)
        shutil.copyfile(os.path.join(DATA_DIR, "message_history.json"), self.temp_history)
        shutil.copyfile(os.path.join(DATA_DIR, "manual_outreach_audit.jsonl"), self.temp_audit)

        # Reset Phase 8.8 cohort in test fixture to baseline NOT_READY state
        target_ids = {"LEAD-MAN-0363CF", "LEAD-MAN-4098E1", "LEAD-MAN-525524", "LEAD-MAN-3B9091"}
        if os.path.exists(self.temp_leads):
            with open(self.temp_leads, "r", encoding="utf-8") as f:
                crm_data = json.load(f)
            leads = crm_data.get("leads", crm_data) if isinstance(crm_data, dict) else crm_data
            for l in leads:
                if l.get("lead_id") in target_ids:
                    l["outreach_status"] = "NOT_READY"
                    l["outreach_channel"] = ""
                    l.pop("outreach_sent_at", None)
            with open(self.temp_leads, "w", encoding="utf-8") as f:
                json.dump(crm_data, f, indent=2)

        # Create isolated executor
        self.isolated_executor = Phase88OutreachExecutor(
            leads_cache_path=self.temp_leads,
            history_path=self.temp_history,
            audit_path=self.temp_audit,
            outcomes_path=self.temp_outcomes,
            results_json_path=self.temp_results,
            results_md_path=self.temp_report,
        )

        # Baseline read-only executor
        self.executor = Phase88OutreachExecutor()

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_01_active_queue_loads_all_four_leads(self):
        """Active queue must return all 4 qualified leads."""
        queue = self.executor.get_active_queue()
        self.assertEqual(len(queue), 4, f"Expected 4 leads in queue, got {len(queue)}")

        expected_ids = {"LEAD-MAN-0363CF", "LEAD-MAN-4098E1", "LEAD-MAN-525524", "LEAD-MAN-3B9091"}
        actual_ids = {q["lead_id"] for q in queue}
        self.assertEqual(actual_ids, expected_ids)

        expected_names = {"Live Seafood Ltd", "Dog and Partridge", "Ducie Arms", "The Old Monkey"}
        actual_names = {q["company_name"] for q in queue}
        self.assertEqual(actual_names, expected_names)

    def test_02_lead_identity_preservation(self):
        """Lead IDs must use canonical LEAD-MAN-* schema, research_id separated, no fabricated IDs."""
        queue = self.executor.get_active_queue()
        for lead in queue:
            lid = lead["lead_id"]
            self.assertTrue(lid.startswith("LEAD-MAN-"), f"Invalid lead_id: {lid}")
            self.assertFalse(lid.startswith("RES-"), f"research_id leak in lead_id: {lid}")

            # Verify no fake IDs
            self.assertNotIn("recipient_id", lead)
            self.assertNotIn("igsid", lead)
            self.assertNotIn("psid", lead)

    def test_03_phone_scripts_structured(self):
        """Phone call scripts must have all 5 structured sections."""
        queue = self.executor.get_active_queue()
        phone_leads = [l for l in queue if l["recommended_channel"] == "PHONE"]
        self.assertEqual(len(phone_leads), 3, "Expected 3 phone leads")

        for lead in phone_leads:
            script = lead.get("phone_script")
            self.assertIsNotNone(script, f"Missing phone_script for {lead['company_name']}")
            self.assertIn("opening", script)
            self.assertIn("why_calling", script)
            self.assertIn("observed_website_opportunity", script)
            self.assertIn("dripp_media_offer", script)
            self.assertIn("permission_to_continue", script)

            self.assertTrue(len(script["opening"]) > 10)
            self.assertTrue(len(script["why_calling"]) > 10)
            self.assertTrue(len(script["observed_website_opportunity"]) > 10)
            self.assertTrue(len(script["dripp_media_offer"]) > 10)
            self.assertTrue(len(script["permission_to_continue"]) > 10)

    def test_04_direct_message_tailored(self):
        """Instagram direct message must be tailored to website opportunity without spam."""
        queue = self.executor.get_active_queue()
        insta_lead = next(l for l in queue if l["lead_id"] == "LEAD-MAN-0363CF")

        self.assertEqual(insta_lead["recommended_channel"], "INSTAGRAM")
        self.assertTrue(insta_lead["recipient"].startswith("@"))

        body = insta_lead["draft_body"]
        self.assertIn("Live Seafood", body)
        self.assertIn("website", body.lower())
        self.assertNotIn("guaranteed 10x ROI", body)
        self.assertNotIn("buy now", body)

    def test_05_prevent_unconfirmed_outreach_action(self):
        """Unconfirmed outreach action must be strictly rejected."""
        with self.assertRaises(ValueError) as ctx:
            self.isolated_executor.record_outreach_action(
                lead_id="LEAD-MAN-4098E1",
                channel="PHONE",
                action_type="CALL_CONNECTED",
                operator_confirmed=False
            )
        self.assertIn("operator_confirmed=True", str(ctx.exception))

    def test_06_record_confirmed_manual_action(self):
        """Confirmed outreach action updates CRM cache, message history, audit log, and outcomes."""
        res = self.isolated_executor.record_outreach_action(
            lead_id="LEAD-MAN-4098E1",
            channel="PHONE",
            action_type="CALL_CONNECTED",
            operator_confirmed=True,
            notes="Spoke with general manager, interested in preview",
            call_connected=True,
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["outreach_status"], "CONTACTED")

        # Verify CRM cache updated
        with open(self.temp_leads, "r", encoding="utf-8") as f:
            leads = json.load(f).get("leads", [])
        dog = next(l for l in leads if l["lead_id"] == "LEAD-MAN-4098E1")
        self.assertEqual(dog["outreach_status"], "CONTACTED")
        self.assertEqual(dog["outreach_mode"], "MANUAL")
        self.assertEqual(dog["outreach_attempt_count"], 1)
        self.assertTrue(dog["actual_send_confirmed"])
        self.assertEqual(dog["send_classification"], "MANUAL_CALL_CONFIRMED")

        # Verify message history record created
        with open(self.temp_history, "r", encoding="utf-8") as f:
            hist = json.load(f)
        lead_hist = hist.get("LEAD-MAN-4098E1", [])
        self.assertEqual(len(lead_hist), 1)
        entry = lead_hist[0]
        self.assertEqual(entry["channel"], "PHONE")
        self.assertEqual(entry["action_type"], "CALL_CONNECTED")
        self.assertEqual(entry["mode"], "MANUAL")
        self.assertTrue(entry["operator_confirmed"])

        # Verify audit log entry
        with open(self.temp_audit, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        dog_audit = [a for a in lines if a.get("lead_id") == "LEAD-MAN-4098E1"]
        self.assertEqual(len(dog_audit), 1)
        self.assertEqual(dog_audit[0]["action"], "CALL_CONNECTED")

        # Verify outcomes file updated
        with open(self.temp_outcomes, "r", encoding="utf-8") as f:
            outcomes = json.load(f)
        dog_outcome = next(o for o in outcomes if o.get("lead_id") == "LEAD-MAN-4098E1")
        self.assertEqual(dog_outcome["outreach_status"], "CONTACTED")

    def test_07_prevent_duplicate_outreach_action(self):
        """Duplicate outreach action on already contacted lead must be blocked."""
        # First send
        self.isolated_executor.record_outreach_action(
            lead_id="LEAD-MAN-4098E1",
            channel="PHONE",
            action_type="CALL_CONNECTED",
            operator_confirmed=True,
            call_connected=True,
        )

        # Second send attempt
        with self.assertRaises(ValueError) as ctx:
            self.isolated_executor.record_outreach_action(
                lead_id="LEAD-MAN-4098E1",
                channel="PHONE",
                action_type="CALL_CONNECTED",
                operator_confirmed=True,
                call_connected=True,
            )
        self.assertIn("already been contacted", str(ctx.exception))

    def test_08_record_call_connected_vs_unanswered(self):
        """Call connected results in CONTACTED, unanswered results in CALL_ATTEMPTED."""
        # Call 1: Ducie Arms unanswered
        res_unanswered = self.isolated_executor.record_outreach_action(
            lead_id="LEAD-MAN-525524",
            channel="PHONE",
            action_type="CALL_UNANSWERED",
            operator_confirmed=True,
            call_connected=False,
            notes="Left voicemail",
        )
        self.assertEqual(res_unanswered["outreach_status"], "CALL_ATTEMPTED")

        # Call 2: The Old Monkey connected
        res_connected = self.isolated_executor.record_outreach_action(
            lead_id="LEAD-MAN-3B9091",
            channel="PHONE",
            action_type="CALL_CONNECTED",
            operator_confirmed=True,
            call_connected=True,
            notes="Spoke with bar manager",
        )
        self.assertEqual(res_connected["outreach_status"], "CONTACTED")

    def test_09_record_outcome_stages(self):
        """Record customer responses across stages with validation."""
        # First mark contacted
        self.isolated_executor.record_outreach_action(
            lead_id="LEAD-MAN-0363CF",
            channel="INSTAGRAM",
            action_type="MESSAGE_SENT",
            operator_confirmed=True,
        )

        # Record REPLIED without summary should fail
        with self.assertRaises(ValueError):
            self.isolated_executor.record_response(
                lead_id="LEAD-MAN-0363CF",
                outcome="REPLIED",
                reply_summary="",
            )

        # Record REPLIED with valid summary
        res_replied = self.isolated_executor.record_response(
            lead_id="LEAD-MAN-0363CF",
            outcome="REPLIED",
            reply_summary="Customer messaged back asking for design samples",
            interest_level="HIGH",
            next_action="Send website mockup link",
        )
        self.assertTrue(res_replied["success"])
        self.assertEqual(res_replied["outcome"], "REPLIED")

        # Record INTERESTED
        res_interested = self.isolated_executor.record_response(
            lead_id="LEAD-MAN-0363CF",
            outcome="INTERESTED",
            reply_summary="Approved review of website mockup",
            interest_level="HIGH",
            next_action="Schedule discovery call",
        )
        self.assertEqual(res_interested["outcome"], "INTERESTED")

    def test_10_administrative_reset(self):
        """Administrative reset requires admin_confirmed=True and restores NOT_READY."""
        # Contact lead
        self.isolated_executor.record_outreach_action(
            lead_id="LEAD-MAN-4098E1",
            channel="PHONE",
            action_type="CALL_CONNECTED",
            operator_confirmed=True,
        )

        # Reset without admin_confirmed should fail
        with self.assertRaises(ValueError):
            self.isolated_executor.reset_lead_outreach(
                lead_id="LEAD-MAN-4098E1",
                admin_confirmed=False
            )

        # Reset with admin_confirmed=True
        res_reset = self.isolated_executor.reset_lead_outreach(
            lead_id="LEAD-MAN-4098E1",
            admin_confirmed=True
        )
        self.assertTrue(res_reset["success"])
        self.assertEqual(res_reset["outreach_status"], "NOT_READY")

        # Verify CRM cache reset
        with open(self.temp_leads, "r", encoding="utf-8") as f:
            leads = json.load(f).get("leads", [])
        dog = next(l for l in leads if l["lead_id"] == "LEAD-MAN-4098E1")
        self.assertEqual(dog["outreach_status"], "NOT_READY")
        self.assertFalse(dog["actual_send_confirmed"])
        self.assertEqual(dog["send_classification"], "NEVER_CONFIRMED_SENT")

    def test_11_fastapi_endpoints(self):
        """Test FastAPI endpoints for Phase 8.8."""
        client = TestClient(app)

        # Queue
        res_q = client.get("/api/manual-outreach/phase-8-8/queue")
        self.assertEqual(res_q.status_code, 200)
        self.assertEqual(res_q.json()["active_batch"], 4)

        # Daily view
        res_dv = client.get("/api/manual-outreach/phase-8-8/daily-view")
        self.assertEqual(res_dv.status_code, 200)
        self.assertIn("summary_counters", res_dv.json())
        self.assertEqual(res_dv.json()["summary_counters"]["ready"], 4)

        # Analytics
        res_an = client.get("/api/manual-outreach/phase-8-8/analytics")
        self.assertEqual(res_an.status_code, 200)
        self.assertEqual(res_an.json()["metrics"]["active_batch"], 4)

        # Record action unconfirmed should fail (400)
        res_act = client.post(
            "/api/manual-outreach/phase-8-8/record-action",
            json={
                "lead_id": "LEAD-MAN-4098E1",
                "channel": "PHONE",
                "action_type": "CALL_CONNECTED",
                "operator_confirmed": False
            }
        )
        self.assertEqual(res_act.status_code, 400)

        # Reset unconfirmed should fail (400)
        res_rst = client.post(
            "/api/manual-outreach/phase-8-8/reset",
            json={"lead_id": "LEAD-MAN-4098E1", "admin_confirmed": False}
        )
        self.assertEqual(res_rst.status_code, 400)

    def test_12_strict_safety_invariants(self):
        """Baseline outreach dispatches must be 0 and campaigns must remain safe."""
        # Ensure campaigns.json is a list and no automated sends are running
        campaigns_path = os.path.join(DATA_DIR, "campaigns.json")
        with open(campaigns_path, "r", encoding="utf-8") as f:
            cdata = json.load(f)
        self.assertIsInstance(cdata, list)

        # Active queue leads must start NOT_READY
        queue = self.executor.get_active_queue()
        for lead in queue:
            self.assertEqual(lead["outreach_status"], "NOT_READY")

        analytics = self.executor.generate_analytics()
        self.assertEqual(analytics["metrics"]["contacted"], 0)
        self.assertEqual(analytics["metrics"]["sent"], 0)


if __name__ == "__main__":
    unittest.main()
