"""
test_cloud_run_job_resumption_and_heartbeat.py
=============================================
Regression tests verifying:
1. Genuine checkpoint resumption: completed candidates from prior runs are skipped and never re-processed or written twice.
2. Outreach and suppression protection: existing CRM rows retain outreach status, suppression history, and bounce records upon update.
3. Concurrent dispatch safety: dual-runner collision cleanly handled when distributed lease is active.
4. Stale lease recovery: interrupted/crashed workers' expired leases are safely reclaimed.
5. Bounded heartbeat safety: background lease renewal thread pulses and preserves lease during long-running tasks.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from lib.types import DiscoveredBusiness, Lead, CountryStatus, QualificationState, Priority
from lib.pipeline import LeadGenerationPipeline
from lib.system.firestore_state_store import FirestoreStateStore
from lib.sheets.google_sheets import GoogleSheetsStorageProvider
from scripts.run_cloud_run_job import run_discovery_task


class TestCloudRunJobResumptionAndHeartbeat(unittest.TestCase):

    def setUp(self):
        self.mock_store = FirestoreStateStore(force_mock=True)

    def test_pipeline_checkpoint_resumption_skips_completed_candidates(self):
        """Pipeline with resume_processed_ids skips already completed candidates and does not duplicate research or output."""
        pipeline = LeadGenerationPipeline(shadow_mode=True)

        # Create 2 probe candidates
        biz1 = DiscoveredBusiness(
            company_name="Manchester Test Grill",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            address="10 Deansgate, Manchester M3 2FW, UK",
            phone="+44 161 111 2222",
            raw_website="https://manchestertestgrill.co.uk",
            rating=4.5,
            review_count=85
        )
        biz2 = DiscoveredBusiness(
            company_name="Manchester Fresh Bakery",
            category="Bakery",
            city="Manchester",
            target_country="United Kingdom",
            address="15 Market St, Manchester M1 1WR, UK",
            phone="+44 161 333 4444",
            raw_website="",  # No website
            rating=4.8,
            review_count=120
        )

        # Normalize identity for biz1 to simulate it was completed in prior run
        biz1_id = pipeline.storage.normalize_identity(biz1.company_name, biz1.city, "United Kingdom", biz1.raw_website)
        resume_ids = [biz1_id]

        # Run pipeline injecting both candidates with resume_ids active
        result = pipeline.run(
            country="United Kingdom",
            cities=["Manchester"],
            industry="Restaurants",
            requested_qualified_leads=2,
            batch_size=5,
            inject_test_candidates=[biz1, biz2],
            resume_processed_ids=resume_ids
        )

        stats = result["stats"]
        # biz1 must be skipped as duplicate/checkpoint match
        self.assertGreaterEqual(stats["duplicates"], 1)

        # biz1 should NOT be in any result entries
        all_res_names = [e.company_name for e in result["research_entries"]]
        self.assertNotIn("Manchester Test Grill", all_res_names)

        # biz2 must be processed
        processed_names = [e.company_name for e in result["research_entries"]] + [l.company_name for l in result["leads"]]
        self.assertIn("Manchester Fresh Bakery", processed_names)

    def test_crm_preserves_existing_outreach_status_and_suppression_history(self):
        """CRM updates must strictly preserve existing outreach status, notes, and suppression/bounce history."""
        storage = GoogleSheetsStorageProvider.__new__(GoogleSheetsStorageProvider)
        storage.matcher = MagicMock()
        storage.stats = {"existing_businesses": 0, "duplicates_prevented": 0}
        storage.ensure_leads_columns = MagicMock()

        # Simulate existing lead with historical outreach and suppression data
        existing_lead_data = {
            "lead_id": "LEAD-MAN-HIST01",
            "company_name": "Northern Quarter Diner",
            "city": "Manchester",
            "country": "United Kingdom",
            "outreach_status": "CONTACTED",
            "lead_status": "CONTACTED",
            "outreach_message": "Previous tailored outreach pitch",
            "campaign_id": "CAMP-SUMMER-2026",
            "outreach_sent_at": "2026-08-01T10:00:00Z",
            "email_suppressed": "true",
            "email_suppression_reason": "BOUNCED",
            "bounce_code": "550",
            "bounce_reason": "User unknown",
            "bounced_at": "2026-08-02T12:00:00Z",
            "call_status": "COMPLETED",
            "call_notes": "Spoke to general manager",
        }

        # Mock worksheet with existing row
        mock_ws = MagicMock()
        mock_ws.get_all_records.return_value = [existing_lead_data]
        storage._leads_ws = mock_ws

        # Mock matcher matching existing business
        match_result = MagicMock()
        match_result.outcome = "EXISTING_BUSINESS"
        match_result.matched_lead = existing_lead_data
        storage.matcher.match_candidate.return_value = match_result

        # New incoming lead for the same business from a fresh discovery run
        incoming_lead = Lead(
            lead_id="LEAD-MAN-NEW999",
            company_name="Northern Quarter Diner",
            industry="Restaurants",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            outreach_status="NOT_READY",
            lead_status="NOT_CONTACTED",
            qualification_state="OUTREACH_READY",
            qualification_reason="Verified no website",
            date_added="2026-10-12"
        )
        mock_ws.update = MagicMock()

        with patch("lib.sheets.google_sheets.crm_write_lock"):
            ins, upd = storage.save_qualified_leads([incoming_lead])

            # Verify lead_id and critical historical fields were preserved
            self.assertEqual(incoming_lead.lead_id, "LEAD-MAN-HIST01")
            self.assertEqual(incoming_lead.outreach_status, "CONTACTED")
            self.assertEqual(incoming_lead.lead_status, "CONTACTED")
            self.assertEqual(incoming_lead.outreach_message, "Previous tailored outreach pitch")
            self.assertEqual(incoming_lead.campaign_id, "CAMP-SUMMER-2026")
            self.assertEqual(incoming_lead.email_suppressed, "true")
            self.assertEqual(incoming_lead.bounce_code, "550")
            self.assertEqual(incoming_lead.bounce_reason, "User unknown")
            self.assertEqual(incoming_lead.call_status, "COMPLETED")
            self.assertEqual(incoming_lead.call_notes, "Spoke to general manager")

    def test_concurrent_dispatch_dual_runner_collision_prevention(self):
        """When an active worker holds the distributed lease, a second concurrent worker exits cleanly with exit code 0."""
        # Worker 1 holds lease
        self.mock_store.acquire_lease("test_discovery_lease", "WORKER-1", ttl_seconds=300.0)

        # Worker 2 attempts execution
        args = MagicMock()
        args.country = "United Kingdom"
        args.cities = ["Manchester"]
        args.industry = "Restaurants"
        args.leads = 5
        args.batch_size = 10
        args.max_multiplier = 5
        args.run_id = "RUN-CONCURRENT-001"
        args.dry_run = True
        args.lease_name = "test_discovery_lease"
        args.ttl_seconds = 300.0

        with patch("scripts.run_cloud_run_job.firestore_store", self.mock_store):
            with patch("scripts.run_cloud_run_job.SystemConfig.get_status_dict", return_value={"travel_mode": True, "commercial_actions_enabled": False}):
                exit_code = run_discovery_task(args)
                # Must exit cleanly with 0 to prevent Cloud Run retry loop
                self.assertEqual(exit_code, 0)

    def test_stale_lease_recovery_after_interrupted_worker(self):
        """When an interrupted worker crashes and leaves an expired lease, a subsequent worker safely reclaims it."""
        lease_name = "test_stale_lease"
        # Worker 1 acquired lease with very short TTL (0.1 second)
        acquired1 = self.mock_store.acquire_lease(lease_name, "WORKER-DEAD", ttl_seconds=0.1)
        self.assertTrue(acquired1)

        # Worker 1 crashed; wait for lease to expire
        time.sleep(0.15)

        # Worker 2 attempts acquisition after TTL expiry
        acquired2 = self.mock_store.acquire_lease(lease_name, "WORKER-RECOVERED", ttl_seconds=300.0)
        self.assertTrue(acquired2)

        # Verify current holder is WORKER-RECOVERED
        active = self.mock_store.get_lease(lease_name)
        self.assertEqual(active["holder_id"], "WORKER-RECOVERED")

    def test_bounded_heartbeat_renews_lease_during_execution(self):
        """Heartbeat thread actively refreshes the lease expiration epoch to prevent TTL timeout during long tasks."""
        import threading
        lease_name = "test_heartbeat_lease"
        holder_id = "WORKER-HEARTBEAT-TEST"
        ttl = 10.0

        acquired = self.mock_store.acquire_lease(lease_name, holder_id, ttl_seconds=ttl)
        self.assertTrue(acquired)

        initial_lease = self.mock_store.get_lease(lease_name)
        initial_expires = initial_lease["expires_epoch"]

        # Run heartbeat worker every 0.1s
        stop_event = threading.Event()
        def worker():
            while not stop_event.wait(timeout=0.1):
                self.mock_store.renew_lease(lease_name, holder_id, ttl_seconds=ttl)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        # Let heartbeat run for 0.3s
        time.sleep(0.3)
        stop_event.set()
        t.join(timeout=1.0)

        refreshed_lease = self.mock_store.get_lease(lease_name)
        refreshed_expires = refreshed_lease["expires_epoch"]

        # Expiration epoch must have advanced
        self.assertGreater(refreshed_expires, initial_expires)

        # Release lease cleanly
        released = self.mock_store.release_lease(lease_name, holder_id)
        self.assertTrue(released)
        self.assertIsNone(self.mock_store.get_lease(lease_name))


class TestCloudRunJobTravelModeAndSafetyInvariants(unittest.TestCase):
    """
    Verifies that the Cloud Run runner enforces TRAVEL_MODE=ACTIVE and safety invariants:
    1. TRAVEL_MODE=ACTIVE is accepted.
    2. Missing, inactive, or invalid TRAVEL_MODE values abort execution safely (return 1).
    3. COMMERCIAL_ACTIONS_ENABLED=true or AUTOMATED_EMAIL_ENABLED=true abort execution safely.
    4. DRY_RUN=TRUE and LEAD_LIMIT=5 are parsed and enforced.
    """

    def setUp(self):
        import argparse
        from lib.system.system_config import SystemConfig
        self._orig_travel_mode = SystemConfig.TRAVEL_MODE
        self._orig_comm = SystemConfig.COMMERCIAL_ACTIONS_ENABLED
        self._orig_email = SystemConfig.AUTOMATED_EMAIL_ENABLED

        self.base_args = argparse.Namespace(
            country="United Kingdom",
            cities=["Manchester"],
            industry="Restaurants",
            leads=5,
            batch_size=10,
            max_multiplier=5,
            run_id="TEST-RUN-001",
            dry_run=True,
            lease_name="test_safety_lease",
            ttl_seconds=60.0
        )

    def tearDown(self):
        from lib.system.system_config import SystemConfig
        SystemConfig.set_travel_mode(self._orig_travel_mode)
        SystemConfig.set_commercial_actions(self._orig_comm)
        SystemConfig.set_automated_email(self._orig_email)

    def test_travel_mode_active_accepted(self):
        """TRAVEL_MODE=ACTIVE passes safety check and attempts lease acquisition."""
        import os
        from unittest.mock import patch
        from scripts.run_cloud_run_job import run_discovery_task

        env_patch = {
            "TRAVEL_MODE": "ACTIVE",
            "COMMERCIAL_ACTIONS_ENABLED": "false",
            "AUTOMATED_EMAIL_ENABLED": "false",
            "DRY_RUN": "TRUE",
            "LEAD_LIMIT": "5"
        }
        with patch.dict(os.environ, env_patch), \
             patch("scripts.run_cloud_run_job.firestore_store.acquire_lease", return_value=False), \
             patch("scripts.run_cloud_run_job.firestore_store.get_lease", return_value={"holder_id": "other"}):
            # Returns 0 because active lease collision exits cleanly without failure
            res = run_discovery_task(self.base_args)
            self.assertEqual(res, 0)

    def test_travel_mode_missing_rejected(self):
        """Missing TRAVEL_MODE environment variable (empty or completely unset) aborts with exit code 1."""
        import os
        from unittest.mock import patch
        from scripts.run_cloud_run_job import run_discovery_task

        # Test empty string
        env_patch = {
            "TRAVEL_MODE": "",
            "COMMERCIAL_ACTIONS_ENABLED": "false",
            "AUTOMATED_EMAIL_ENABLED": "false",
        }
        with patch.dict(os.environ, env_patch):
            res = run_discovery_task(self.base_args)
            self.assertEqual(res, 1)

        # Test completely unset / deleted key
        clean_env = dict(os.environ)
        clean_env.pop("TRAVEL_MODE", None)
        clean_env["COMMERCIAL_ACTIONS_ENABLED"] = "false"
        clean_env["AUTOMATED_EMAIL_ENABLED"] = "false"
        with patch.dict(os.environ, clean_env, clear=True):
            res = run_discovery_task(self.base_args)
            self.assertEqual(res, 1)

    def test_travel_mode_inactive_rejected(self):
        """TRAVEL_MODE=INACTIVE aborts with exit code 1."""
        import os
        from unittest.mock import patch
        from scripts.run_cloud_run_job import run_discovery_task

        env_patch = {
            "TRAVEL_MODE": "INACTIVE",
            "COMMERCIAL_ACTIONS_ENABLED": "false",
            "AUTOMATED_EMAIL_ENABLED": "false",
        }
        with patch.dict(os.environ, env_patch):
            res = run_discovery_task(self.base_args)
            self.assertEqual(res, 1)

    def test_travel_mode_invalid_value_rejected(self):
        """Invalid TRAVEL_MODE value (e.g. UNKNOWN, BANANA, FALSE) aborts with exit code 1."""
        import os
        from unittest.mock import patch
        from scripts.run_cloud_run_job import run_discovery_task

        for invalid_val in ["UNKNOWN", "BANANA", "FALSE", "disabled", "0"]:
            with patch.dict(os.environ, {
                "TRAVEL_MODE": invalid_val,
                "COMMERCIAL_ACTIONS_ENABLED": "false",
                "AUTOMATED_EMAIL_ENABLED": "false",
            }):
                res = run_discovery_task(self.base_args)
                self.assertEqual(res, 1, f"Failed to reject invalid TRAVEL_MODE='{invalid_val}'")

    def test_commercial_actions_enabled_rejected(self):
        """COMMERCIAL_ACTIONS_ENABLED=true aborts execution even if TRAVEL_MODE is ACTIVE."""
        import os
        from unittest.mock import patch
        from lib.system.system_config import SystemConfig
        from scripts.run_cloud_run_job import run_discovery_task

        env_patch = {
            "TRAVEL_MODE": "ACTIVE",
            "COMMERCIAL_ACTIONS_ENABLED": "true",
            "AUTOMATED_EMAIL_ENABLED": "false",
        }
        with patch.dict(os.environ, env_patch):
            SystemConfig.set_commercial_actions(True)
            res = run_discovery_task(self.base_args)
            self.assertEqual(res, 1)
        # Restore safe state
        SystemConfig.set_commercial_actions(False)

    def test_automated_email_enabled_rejected(self):
        """AUTOMATED_EMAIL_ENABLED=true aborts execution even if TRAVEL_MODE is ACTIVE."""
        import os
        from unittest.mock import patch
        from lib.system.system_config import SystemConfig
        from scripts.run_cloud_run_job import run_discovery_task

        env_patch = {
            "TRAVEL_MODE": "ACTIVE",
            "COMMERCIAL_ACTIONS_ENABLED": "false",
            "AUTOMATED_EMAIL_ENABLED": "true",
        }
        with patch.dict(os.environ, env_patch):
            SystemConfig.set_automated_email(True)
            res = run_discovery_task(self.base_args)
            self.assertEqual(res, 1)
        # Restore safe state
        SystemConfig.set_automated_email(False)

    def test_dry_run_and_lead_limit_environment_parsing(self):
        """parse_args correctly honors DRY_RUN=TRUE and LEAD_LIMIT=5 from environment."""
        import os
        import sys
        from unittest.mock import patch
        from scripts.run_cloud_run_job import parse_args

        env_patch = {
            "DRY_RUN": "TRUE",
            "LEAD_LIMIT": "5",
            "TRAVEL_MODE": "ACTIVE",
        }
        with patch.dict(os.environ, env_patch), patch.object(sys, "argv", ["run_cloud_run_job.py"]):
            args = parse_args()
            self.assertEqual(args.leads, 5)
            self.assertTrue(args.dry_run)

    def test_system_config_travel_mode_active_evaluation(self):
        """SystemConfig evaluates TRAVEL_MODE=ACTIVE as True and reloads properly."""
        import os
        from unittest.mock import patch
        from lib.system.system_config import SystemConfig

        with patch.dict(os.environ, {"TRAVEL_MODE": "ACTIVE"}):
            SystemConfig.reload_from_env()
            self.assertTrue(SystemConfig.TRAVEL_MODE)
            status = SystemConfig.get_status_dict()
            self.assertTrue(status.get("travel_mode"))

        with patch.dict(os.environ, {"TRAVEL_MODE": "INACTIVE"}):
            SystemConfig.reload_from_env()
            self.assertFalse(SystemConfig.TRAVEL_MODE)
            status = SystemConfig.get_status_dict()
            self.assertFalse(status.get("travel_mode"))

        # Restore
        with patch.dict(os.environ, {"TRAVEL_MODE": "ACTIVE"}):
            SystemConfig.reload_from_env()


if __name__ == "__main__":
    unittest.main()

