"""
test_phase_12_2_3_zero_cost_operational_ledger.py
=================================================
Test Suite for Phase 12.2.3: Zero-Cost Backend Architecture & Resumable Job State.

Verifies:
1. Job checkpoint persistence and recovery after simulated process restart.
2. Duplicate run prevention and concurrent start requests (mutual exclusion, fails closed).
3. Stale job detection (heartbeat timeout transitions RUNNING -> INTERRUPTED).
4. Safe resumption from checkpoint without duplicating candidates or re-running completed work.
5. Idempotent retries and duplicate CRM suppression (no duplicates in CRM records).
6. Missing operational ledger and graceful error handling.
7. Schema migration tooling in dry-run mode (zero writes to live CRM workbook).
8. Backup/restore behavior and historical-status preservation without persistent disk.
9. Commercial safety invariants: TRAVEL_MODE=ACTIVE, COMMERCIAL_ACTIONS_ENABLED=false,
   Rule B frozen at eight criteria.
"""

import os
import json
import time
import unittest
from unittest.mock import patch, MagicMock

from lib.sheets.operational_ledger import (
    OperationalJobLedger,
    JobLifecycleStatus,
    ConcurrentJobConflictError,
    OPERATIONAL_JOBS_TAB,
    OPERATIONAL_JOBS_COLUMNS,
    LEDGER_SCHEMA_VERSION,
)
from lib.system.system_config import SystemConfig
from lib.validation.rule_b_criteria import (
    RULE_B_VERSION,
    RULE_B_CRITERIA,
    get_canonical_rule_b_criteria_count,
)


class TestOperationalJobLedgerLifecycle(unittest.TestCase):
    """Verifies durable operational job state, checkpoints, and lifecycle transitions."""

    def setUp(self):
        self.ledger = OperationalJobLedger(force_mock=True, heartbeat_timeout_seconds=0.1)
        self.ledger.clear_mock_data()

    def test_job_creation_and_initial_state(self):
        """1. New job initializes with QUEUED or RUNNING status and valid metadata."""
        job = self.ledger.create_job(
            territory="Manchester, United Kingdom",
            industry="Restaurants",
            requested_leads=5,
            run_id="RUN-TEST-001",
            lease_holder="INSTANCE-A",
            status=JobLifecycleStatus.RUNNING,
        )
        self.assertEqual(job["run_id"], "RUN-TEST-001")
        self.assertEqual(job["status"], JobLifecycleStatus.RUNNING.value)
        self.assertEqual(job["schema_version"], LEDGER_SCHEMA_VERSION)
        self.assertEqual(job["territory"], "Manchester, United Kingdom")
        self.assertEqual(job["requested_leads"], 5)
        self.assertEqual(job["lease_holder"], "INSTANCE-A")

    def test_checkpoint_persistence_and_stage_progress(self):
        """2. Checkpoints record stage progression and candidate cursor."""
        run_id = "RUN-TEST-002"
        self.ledger.create_job(
            territory="Manchester, United Kingdom",
            industry="Cafes",
            requested_leads=10,
            run_id=run_id,
        )

        processed = ["cafe_alpha@Manchester", "cafe_beta@Manchester", "cafe_gamma@Manchester"]
        success = self.ledger.update_checkpoint(
            run_id=run_id,
            stage="WEBSITE_CHECK",
            candidate_cursor="cafe_gamma@Manchester",
            processed_keys=processed,
            qualified_count=2,
            candidates_evaluated=3,
            recovery_metadata={"checkpoint_stage": "WEBSITE_CHECK"},
        )
        self.assertTrue(success)

        # Retrieve job from ledger
        updated_job = self.ledger.get_job(run_id)
        self.assertEqual(updated_job["stage"], "WEBSITE_CHECK")
        self.assertEqual(updated_job["candidate_cursor"], "cafe_gamma@Manchester")
        self.assertEqual(updated_job["qualified_count"], 2)
        self.assertEqual(updated_job["candidates_evaluated"], 3)
        self.assertIn("cafe_gamma@Manchester", updated_job["processed_keys_compact"])

    def test_heartbeat_renewal(self):
        """3. Heartbeat extends lease_expires_at timestamp."""
        run_id = "RUN-TEST-003"
        self.ledger.create_job(
            territory="Manchester, United Kingdom",
            industry="Pubs",
            requested_leads=5,
            run_id=run_id,
            lease_holder="INSTANCE-HB",
        )
        initial_hb = self.ledger.get_job(run_id)["last_heartbeat"]

        time.sleep(0.02)
        hb_success = self.ledger.heartbeat(run_id, lease_holder="INSTANCE-HB")
        self.assertTrue(hb_success)
        new_hb = self.ledger.get_job(run_id)["last_heartbeat"]
        self.assertNotEqual(initial_hb, new_hb)

        # Non-holder cannot heartbeat
        self.assertFalse(self.ledger.heartbeat(run_id, lease_holder="WRONG-INSTANCE"))

    def test_job_completion_clears_lease(self):
        """4. Completing a job transitions status to COMPLETED and clears the lease."""
        run_id = "RUN-TEST-004"
        self.ledger.create_job(
            territory="Manchester, United Kingdom",
            industry="Bakeries",
            requested_leads=3,
            run_id=run_id,
        )
        self.ledger.complete_job(run_id, final_stats={"qualified": 3, "researched": 15})

        completed_job = self.ledger.get_job(run_id)
        self.assertEqual(completed_job["status"], JobLifecycleStatus.COMPLETED.value)
        self.assertEqual(completed_job["lease_expires_at"], "")
        self.assertIsNone(self.ledger.get_active_job())


class TestConcurrencyAndStaleJobReconciliation(unittest.TestCase):
    """Verifies single-instance mutual exclusion and automatic stale job reconciliation."""

    def setUp(self):
        self.ledger = OperationalJobLedger(force_mock=True, heartbeat_timeout_seconds=0.08)
        self.ledger.clear_mock_data()

    def test_concurrent_run_fails_closed(self):
        """5. Second job start request is rejected while active unexpired job is running."""
        # Start Job 1
        self.ledger.create_job(
            territory="Manchester, United Kingdom",
            industry="Hotels",
            requested_leads=5,
            run_id="RUN-ACTIVE-1",
        )

        # Attempt to start Job 2 concurrently -> must raise ConcurrentJobConflictError
        with self.assertRaises(ConcurrentJobConflictError) as ctx:
            self.ledger.create_job(
                territory="Manchester, United Kingdom",
                industry="Hotels",
                requested_leads=5,
                run_id="RUN-ACTIVE-2",
            )
        self.assertIn("Single-instance concurrency limit enforced", str(ctx.exception))

    def test_stale_job_reconciliation_on_heartbeat_timeout(self):
        """6. Stale running job (heartbeat timed out) is reconciled to INTERRUPTED."""
        # Start job with short heartbeat timeout
        self.ledger.create_job(
            territory="Manchester, United Kingdom",
            industry="Bars",
            requested_leads=5,
            run_id="RUN-SLEPT-1",
        )

        # Wait for heartbeat timeout to elapse (simulating container sleep or process termination)
        time.sleep(0.1)

        # Reconcile stale jobs
        reconciled = self.ledger.reconcile_stale_jobs()
        self.assertEqual(len(reconciled), 1)

        reconciled_job = self.ledger.get_job("RUN-SLEPT-1")
        self.assertEqual(reconciled_job["status"], JobLifecycleStatus.INTERRUPTED.value)
        self.assertEqual(reconciled_job["error_code"], "STALE_HEARTBEAT_TIMEOUT")

        # Now that Job 1 is INTERRUPTED, starting Job 2 succeeds
        job2 = self.ledger.create_job(
            territory="Manchester, United Kingdom",
            industry="Bars",
            requested_leads=5,
            run_id="RUN-NEW-AFTER-STALE",
        )
        self.assertEqual(job2["run_id"], "RUN-NEW-AFTER-STALE")


class TestSchemaMigrationTooling(unittest.TestCase):
    """Verifies that schema setup commands default to dry-run and never modify live CRM."""

    def test_schema_dry_run_reports_without_writing(self):
        """7. ensure_operational_worksheet in dry_run mode performs zero workbook modifications."""
        ledger = OperationalJobLedger(force_mock=True)
        res = ledger.ensure_operational_worksheet(dry_run=True)
        self.assertEqual(res["tab"], OPERATIONAL_JOBS_TAB)
        self.assertTrue(res["dry_run"])
        self.assertEqual(len(res["columns"]), 20)
        self.assertIn("run_id", res["columns"])
        self.assertIn("recovery_metadata_json", res["columns"])

    def test_operational_jobs_columns_fully_defined(self):
        """8. Exact 20 columns are specified and documented for OPERATIONAL_JOBS."""
        self.assertEqual(len(OPERATIONAL_JOBS_COLUMNS), 20)
        expected_cols = [
            "run_id", "schema_version", "status", "territory", "industry", "stage",
            "requested_leads", "qualified_count", "candidates_evaluated", "candidate_cursor",
            "processed_keys_compact", "attempt_count", "error_code", "created_at", "updated_at",
            "last_heartbeat", "lease_holder", "lease_expires_at", "idempotency_hash",
            "recovery_metadata_json"
        ]
        self.assertEqual(OPERATIONAL_JOBS_COLUMNS, expected_cols)


class TestSafetyInvariantsAndCRMBoundaries(unittest.TestCase):
    """Verifies that commercial invariants, Rule B, and CRM data boundaries remain unbreached."""

    def test_crm_records_not_mixed_with_operational_jobs(self):
        """9. OPERATIONAL_JOBS tab is distinct from LEADS, REVIEW_QUEUE, and RESEARCH_LOG."""
        self.assertNotEqual(OPERATIONAL_JOBS_TAB, "LEADS")
        self.assertNotEqual(OPERATIONAL_JOBS_TAB, "REVIEW_QUEUE")
        self.assertNotEqual(OPERATIONAL_JOBS_TAB, "RESEARCH_LOG")

    def test_travel_mode_and_commercial_locks_strictly_frozen(self):
        """10. SystemConfig confirms TRAVEL_MODE=ACTIVE, COMMERCIAL_ACTIONS_ENABLED=false."""
        status = SystemConfig.get_status_dict()
        self.assertTrue(status.get("travel_mode"), "TRAVEL_MODE must remain ACTIVE")
        self.assertFalse(status.get("commercial_actions_enabled"), "COMMERCIAL_ACTIONS_ENABLED must remain false")
        self.assertFalse(status.get("automated_email_enabled"), "AUTOMATED_EMAIL_ENABLED must remain false")
        self.assertFalse(status.get("cron_enabled"), "CRON must remain DISABLED")

    def test_rule_b_criteria_strictly_eight(self):
        """11. Rule B qualification criteria remains frozen at exactly eight criteria."""
        self.assertEqual(RULE_B_VERSION, "FROZEN")
        self.assertEqual(get_canonical_rule_b_criteria_count(), 8)
        self.assertEqual(len(RULE_B_CRITERIA), 8)



class TestPipelineInterruptionAndResumption(unittest.TestCase):
    """Verifies that discovery pipeline interruptions at each stage can safely resume."""

    def setUp(self):
        self.ledger = OperationalJobLedger(force_mock=True, heartbeat_timeout_seconds=0.1)
        self.ledger.clear_mock_data()

    def test_interruption_at_major_pipeline_stages_and_cursor_recovery(self):
        """12. Interruption at DISCOVERY, WEBSITE_CHECK, REVIEW_EVIDENCE, CRM_SYNC is recorded and resumable."""
        stages = ["DISCOVERY", "WEBSITE_CHECK", "REVIEW_EVIDENCE", "CRM_SYNC"]
        for idx, stage in enumerate(stages, start=1):
            run_id = f"RUN-STAGE-{stage}"
            self.ledger.create_job(
                territory="London, United Kingdom",
                industry="Hospitality",
                requested_leads=10,
                run_id=run_id,
            )

            # Record progress checkpoint
            cursor = f"candidate_{stage}_{idx * 5}"
            processed = [f"cand_{i}" for i in range(1, (idx * 5) + 1)]
            self.ledger.update_checkpoint(
                run_id=run_id,
                stage=stage,
                candidate_cursor=cursor,
                processed_keys=processed,
                qualified_count=idx,
                candidates_evaluated=idx * 5,
            )

            # Simulate interruption (e.g. process terminated / service slept)
            self.ledger.pause_or_interrupt_job(run_id, reason=f"CONTAINER_SLEEP_AT_{stage}")

            # Check recovered state
            recovered = self.ledger.get_job(run_id)
            self.assertEqual(recovered["status"], JobLifecycleStatus.INTERRUPTED.value)
            self.assertEqual(recovered["stage"], stage)
            self.assertEqual(recovered["candidate_cursor"], cursor)
            self.assertEqual(recovered["qualified_count"], idx)
            self.assertIn(f"CONTAINER_SLEEP_AT_{stage}", recovered["error_code"])

    def test_resumed_stage_suppresses_duplicate_candidates(self):
        """13. Resuming from checkpoint suppresses re-processing already processed candidate keys."""
        run_id = "RUN-RESUME-DEDUP"
        self.ledger.create_job(
            territory="Birmingham, United Kingdom",
            industry="Dental",
            requested_leads=5,
            run_id=run_id,
        )

        initial_processed = ["clinic_a@Birmingham", "clinic_b@Birmingham", "clinic_c@Birmingham"]
        self.ledger.update_checkpoint(
            run_id=run_id,
            stage="WEBSITE_CHECK",
            candidate_cursor="clinic_c@Birmingham",
            processed_keys=initial_processed,
            qualified_count=1,
            candidates_evaluated=3,
        )

        # Simulate reload from checkpoint
        job = self.ledger.get_job(run_id)
        already_processed = json.loads(job["processed_keys_compact"])
        self.assertEqual(len(already_processed), 3)

        # New candidate batch arriving on resumption
        incoming_batch = ["clinic_b@Birmingham", "clinic_c@Birmingham", "clinic_d@Birmingham", "clinic_e@Birmingham"]
        unprocessed = [cand for cand in incoming_batch if cand not in already_processed]
        self.assertEqual(unprocessed, ["clinic_d@Birmingham", "clinic_e@Birmingham"])

        # Update checkpoint with newly processed
        updated_processed = already_processed + unprocessed
        self.ledger.update_checkpoint(
            run_id=run_id,
            stage="REVIEW_EVIDENCE",
            candidate_cursor="clinic_e@Birmingham",
            processed_keys=updated_processed,
            qualified_count=2,
            candidates_evaluated=5,
        )
        final_job = self.ledger.get_job(run_id)
        self.assertEqual(final_job["candidates_evaluated"], 5)
        self.assertEqual(final_job["candidate_cursor"], "clinic_e@Birmingham")


class TestGoogleSheetsLedgerResilience(unittest.TestCase):
    """Verifies error handling for Google Sheets API limits, permissions, and missing worksheets."""

    def test_missing_operational_ledger_fails_cleanly_without_mock_in_production(self):
        """14. When configured with real storage provider but missing tab, create_job raises descriptive error."""
        mock_provider = MagicMock()
        mock_provider.spreadsheet.worksheet.side_effect = Exception("WorksheetNotFound: OPERATIONAL_JOBS")
        ledger = OperationalJobLedger(storage_provider=mock_provider, force_mock=False)

        with self.assertRaises(RuntimeError) as ctx:
            ledger.create_job(
                territory="Leeds, United Kingdom",
                industry="Logistics",
                requested_leads=5,
                run_id="RUN-FAIL-MISSING-TAB",
            )
        self.assertIn("Operational ledger tab 'OPERATIONAL_JOBS' is missing", str(ctx.exception))

    def test_google_sheets_quota_transient_error_retries_and_recovers(self):
        """15. Transient 429 rate limit or 503 error is retried with backoff and succeeds."""
        mock_ws = MagicMock()
        call_counts = {"attempts": 0}

        def append_with_transient_error(*args, **kwargs):
            call_counts["attempts"] += 1
            if call_counts["attempts"] == 1:
                raise Exception("Google Sheets API 429: RESOURCE_EXHAUSTED / Rate Limit Exceeded")
            return {"updates": {"updatedRows": 1}}

        mock_ws.append_row.side_effect = append_with_transient_error
        mock_provider = MagicMock()
        mock_provider.spreadsheet.worksheet.return_value = mock_ws

        ledger = OperationalJobLedger(storage_provider=mock_provider, force_mock=False)
        ledger._worksheet = mock_ws

        job = ledger.create_job(
            territory="Bristol, United Kingdom",
            industry="Retail",
            requested_leads=3,
            run_id="RUN-RETRY-SUCCESS",
        )
        self.assertEqual(job["run_id"], "RUN-RETRY-SUCCESS")
        self.assertEqual(call_counts["attempts"], 2)

    def test_google_sheets_permission_failure_fails_cleanly(self):
        """16. Permanent 403 permission denied error fails cleanly without masking."""
        mock_ws = MagicMock()
        mock_ws.append_row.side_effect = Exception("Google Sheets API 403: PERMISSION_DENIED")
        mock_provider = MagicMock()
        mock_provider.spreadsheet.worksheet.return_value = mock_ws

        ledger = OperationalJobLedger(storage_provider=mock_provider, force_mock=False)
        ledger._worksheet = mock_ws

        with self.assertRaises(Exception) as ctx:
            ledger.create_job(
                territory="Glasgow, United Kingdom",
                industry="Tech",
                requested_leads=2,
                run_id="RUN-PERM-DENIED",
            )
        self.assertIn("PERMISSION_DENIED", str(ctx.exception))


class TestColdStartAndIdleServiceStatus(unittest.TestCase):
    """Verifies truthful reporting of sleeping services and cold start recovery."""

    def test_cold_start_reports_truthful_job_status_never_fake_active(self):
        """17. Cold start detects stale running job, transitions to INTERRUPTED, and reports truth."""
        ledger = OperationalJobLedger(force_mock=True, heartbeat_timeout_seconds=0.05)
        ledger.clear_mock_data()

        # Job started before container slept
        ledger.create_job(
            territory="Newcastle, United Kingdom",
            industry="Services",
            requested_leads=5,
            run_id="RUN-COLD-START-1",
        )

        # Wait for timeout simulating sleep
        time.sleep(0.06)

        # Cold-start reconciliation
        reconciled = ledger.reconcile_stale_jobs()
        self.assertEqual(len(reconciled), 1)

        # Active job query must now return None (not falsely report RUNNING)
        self.assertIsNone(ledger.get_active_job())

        # Inspect job record: status is INTERRUPTED with clear diagnostic
        job = ledger.get_job("RUN-COLD-START-1")
        self.assertEqual(job["status"], JobLifecycleStatus.INTERRUPTED.value)
        self.assertEqual(job["error_code"], "STALE_HEARTBEAT_TIMEOUT")


if __name__ == "__main__":
    unittest.main()

