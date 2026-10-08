"""
test_phase_10_6_production_readiness.py
=======================================
Phase 10.6 Verification Suite: Production Readiness & Controlled Staged Rollout.

Validates:
  1. Readiness state machine & 20 deterministic gates
  2. Runtime mode separation & strict non-production guards
  3. Commercial action hard blocks across all entrypoints
  4. Controlled staged pipeline execution (50 candidates)
  5. Repeatability & idempotency across 3 identical runs
  6. Interruption recovery & atomic write protection
  7. 16-scenario failure injection matrix & incident tracking
  8. Full 13-step backup -> restore -> reconcile disaster drill
  9. Rollback & pause operational contract
  10. Preflight CLI execution & GitHub Actions workflow safety
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import QualificationState, OutreachStatus
from lib.system.system_config import (
    SystemConfig,
    CommercialActionForbiddenError,
    EmailAutomationBlockedError,
)
from lib.system.runtime_mode import (
    RuntimeMode,
    OperationalState,
    RuntimeModeManager,
    OperationalStateError,
    ModeConfigurationError,
)
from lib.system.production_readiness import (
    ProductionReadinessAuditor,
    ReadinessState,
    GateStatus,
    GateSeverity,
)
from lib.system.staged_pipeline import (
    StagedPipelineRunner,
    generate_synthetic_staging_dataset,
)
from lib.system.failure_injection import (
    FailureInjectionSimulator,
    FailureScenario,
    SCENARIO_MATRIX,
)
from lib.system.disaster_recovery_drill import DisasterRecoveryDrill
from lib.system.atomic_writer import atomic_write_json
from lib.system.file_lock import FileLock
from scripts.production_preflight import run_preflight


class TestReadinessStateMachineAndGates(unittest.TestCase):
    """1. Production Readiness Model & 20 Deterministic Gates."""

    def setUp(self):
        self.auditor = ProductionReadinessAuditor(project_root=PROJECT_ROOT)

    def tearDown(self):
        pass

    def test_readiness_states_defined(self):
        """1. Readiness states include NOT_READY, TECHNICALLY_READY, STAGING_READY, PRODUCTION_READY, BLOCKED."""
        expected = {"NOT_READY", "TECHNICALLY_READY", "STAGING_READY", "PRODUCTION_READY", "BLOCKED"}
        actual = {s.value for s in ReadinessState}
        self.assertEqual(expected, actual)

    def test_all_20_readiness_gates_present_and_structured(self):
        """2. ProductionReadinessAuditor evaluates all 20 gates returning structured evidence."""
        report = self.auditor.audit_all_gates()
        self.assertEqual(report["total_gates"], 20)
        self.assertEqual(len(report["gates"]), 20)

        required_keys = {"gate_id", "status", "severity", "blocking", "evidence", "timestamp"}
        gate_ids = set()
        for gate in report["gates"]:
            self.assertTrue(required_keys.issubset(set(gate.keys())))
            self.assertIn(gate["status"], [s.value for s in GateStatus])
            self.assertIn(gate["severity"], [s.value for s in GateSeverity])
            self.assertIsInstance(gate["evidence"], dict)
            gate_ids.add(gate["gate_id"])

        expected_gate_ids = {
            "CONFIG_VALIDITY", "ENVIRONMENT_SEPARATION", "SECRET_HYGIENE",
            "STORAGE_HEALTH", "BACKUP_HEALTH", "RESTORE_VERIFICATION",
            "STATE_INTEGRITY", "RECONCILIATION_HEALTH", "IDENTITY_INTEGRITY",
            "RULE_B_INTEGRITY", "OUTREACH_LOCK_STATE", "EMAIL_LOCK_STATE",
            "SCHEDULER_STATE", "CRON_STATE", "MONITORING_HEALTH",
            "PROVIDER_HEALTH", "QUOTA_HEALTH", "TEST_HEALTH",
            "ARTIFACT_LOG_SAFETY", "ROLLBACK_CAPABILITY",
        }
        self.assertEqual(gate_ids, expected_gate_ids)

    def test_production_ready_forbidden_without_explicit_human_confirmation(self):
        """3. PRODUCTION_READY state cannot be achieved while commercial actions are locked or unconfirmed."""
        report = self.auditor.audit_all_gates()
        self.assertNotEqual(report["readiness_state"], ReadinessState.PRODUCTION_READY.value)
        self.assertIn(report["readiness_state"], (ReadinessState.STAGING_READY.value, ReadinessState.TECHNICALLY_READY.value))

    def test_blocking_gate_forces_blocked_verdict(self):
        """4. Any required gate failure immediately blocks overall status."""
        with patch.object(self.auditor, "check_secret_hygiene", return_value={
            "gate_id": "SECRET_HYGIENE",
            "status": "FAIL",
            "severity": "REQUIRED",
            "blocking": True,
            "evidence": {"found": 1},
            "timestamp": "2026-10-09T00:00:00Z",
        }):
            report = self.auditor.audit_all_gates()
            self.assertEqual(report["overall_status"], "BLOCKED")
            self.assertEqual(report["readiness_state"], ReadinessState.BLOCKED.value)
            self.assertIn("SECRET_HYGIENE", report["blocking_gates"])


class TestRuntimeModeAndSeparation(unittest.TestCase):
    """2. Canonical Runtime Mode Governance & Environment Separation."""

    def setUp(self):
        RuntimeModeManager.reset_mode()

    def tearDown(self):
        RuntimeModeManager.reset_mode()

    def test_safe_default_mode(self):
        """5. Default runtime mode is safe (TEST in test runner or STAGING in normal runs, NEVER PRODUCTION)."""
        mode = RuntimeModeManager.get_current_mode()
        self.assertIn(mode, (RuntimeMode.TEST, RuntimeMode.STAGING))
        self.assertFalse(RuntimeModeManager.is_production())

    def test_missing_environment_variable_never_implies_production(self):
        """6. Unset DRIPP_RUNTIME_MODE defaults safely and never implies production."""
        with patch.dict(os.environ, {}, clear=True):
            mode = RuntimeModeManager.get_current_mode()
            self.assertNotEqual(mode, RuntimeMode.PRODUCTION)

    def test_invalid_runtime_mode_raises_error(self):
        """7. Unknown runtime mode string raises ModeConfigurationError."""
        with patch.dict(os.environ, {"DRIPP_RUNTIME_MODE": "INVALID_UNKNOWN_MODE"}):
            with self.assertRaises(ModeConfigurationError):
                RuntimeModeManager.get_current_mode()

    def test_live_dispatch_assert_blocks_in_non_production(self):
        """8. assert_live_dispatch_allowed raises PermissionError in STAGING and TEST modes."""
        RuntimeModeManager.set_mode(RuntimeMode.STAGING)
        with self.assertRaises(PermissionError):
            RuntimeModeManager.assert_live_dispatch_allowed("test_staging_dispatch")

        RuntimeModeManager.set_mode(RuntimeMode.TEST)
        with self.assertRaises(PermissionError):
            RuntimeModeManager.assert_live_dispatch_allowed("test_test_dispatch")

    def test_production_mode_requires_human_activation_confirmation(self):
        """9. In PRODUCTION mode, assert_live_dispatch_allowed blocks if HUMAN_ACTIVATION_CONFIRMED is false."""
        RuntimeModeManager.set_mode(RuntimeMode.PRODUCTION)
        with patch.dict(os.environ, {"HUMAN_ACTIVATION_CONFIRMED": "false"}):
            with self.assertRaises(PermissionError) as ctx:
                RuntimeModeManager.assert_live_dispatch_allowed("live_production_send")
            self.assertIn("HUMAN_ACTIVATION_CONFIRMED", str(ctx.exception))


class TestCommercialActionHardGates(unittest.TestCase):
    """3. Hard Commercial Action & Outbound Dispatch Gates."""

    def setUp(self):
        self.orig_travel = SystemConfig.TRAVEL_MODE
        self.orig_comm = SystemConfig.COMMERCIAL_ACTIONS_ENABLED
        self.orig_email = SystemConfig.AUTOMATED_EMAIL_ENABLED
        self.orig_kill = SystemConfig.EMAIL_AUTOMATION_KILL_SWITCH

    def tearDown(self):
        SystemConfig.set_travel_mode(self.orig_travel)
        SystemConfig.set_commercial_actions(self.orig_comm)
        SystemConfig.set_automated_email(self.orig_email)
        SystemConfig.set_email_kill_switch(self.orig_kill)

    def test_travel_mode_active_blocks_commercial_action(self):
        """10. Scenario A: Travel Mode ON + any attempted commercial action => BLOCKED."""
        SystemConfig.set_travel_mode(True)
        SystemConfig.set_commercial_actions(True)  # Even if comm actions enabled
        self.assertFalse(SystemConfig.can_execute_commercial_actions())
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_actions_allowed("test_send")

    def test_commercial_actions_disabled_blocks_send(self):
        """11. Scenario B: Commercial actions disabled + attempted send => BLOCKED."""
        SystemConfig.set_travel_mode(False)
        SystemConfig.set_commercial_actions(False)
        self.assertFalse(SystemConfig.can_execute_commercial_actions())
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_actions_allowed("test_send")

    def test_automated_email_disabled_blocks_email_send(self):
        """12. Scenario C: Automated email disabled + email send => BLOCKED."""
        SystemConfig.set_automated_email(False)
        self.assertFalse(SystemConfig.is_automated_email_enabled())
        with self.assertRaises(EmailAutomationBlockedError):
            SystemConfig.assert_automated_email_allowed("test_email")

    def test_email_kill_switch_blocks_email_send(self):
        """13. Scenario C2: Email kill switch active + email send => BLOCKED."""
        SystemConfig.set_automated_email(True)
        SystemConfig.set_email_kill_switch(True)
        self.assertFalse(SystemConfig.is_automated_email_enabled())
        with self.assertRaises(EmailAutomationBlockedError):
            SystemConfig.assert_automated_email_allowed("test_email")

    def test_technical_scheduler_allowed_while_commercial_blocked(self):
        """14. Scenario D: Technical jobs allowed while commercial actions are completely blocked."""
        SystemConfig.set_travel_mode(True)
        SystemConfig.set_commercial_actions(False)
        self.assertTrue(SystemConfig.can_execute_technical_job("DISCOVERY"))
        self.assertTrue(SystemConfig.can_execute_technical_job("BACKUP"))
        self.assertFalse(SystemConfig.can_execute_commercial_actions())

    def test_direct_send_adapters_raise_forbidden_when_locked(self):
        """15. Direct calls to dispatch_send and adapters raise CommercialActionForbiddenError."""
        from lib.outreach.send_adapters import dispatch_send, EmailAdapter, InstagramDMAdapter

        SystemConfig.set_travel_mode(True)
        SystemConfig.set_commercial_actions(False)

        with self.assertRaises(CommercialActionForbiddenError):
            dispatch_send("email", "test@domain.com", "body", "subject")

        with self.assertRaises(CommercialActionForbiddenError):
            EmailAdapter.send("test@domain.com", "subject", "body")

        with self.assertRaises(CommercialActionForbiddenError):
            InstagramDMAdapter.send("testhandle", "body")

    def test_campaign_execution_gate_raises_forbidden_when_locked(self):
        """16. arm_campaign, verify_execute_gate, and execute_campaign raise CommercialActionForbiddenError."""
        from lib.outreach.execution_gate import arm_campaign, verify_execute_gate
        from lib.outreach.campaign_executor import execute_campaign

        SystemConfig.set_travel_mode(True)
        SystemConfig.set_commercial_actions(False)

        with self.assertRaises(CommercialActionForbiddenError):
            arm_campaign("CAMP-TEST-999")

        with self.assertRaises(CommercialActionForbiddenError):
            verify_execute_gate("CAMP-TEST-999", "dummy_token")

        with self.assertRaises(CommercialActionForbiddenError):
            execute_campaign("CAMP-TEST-999")


class TestStagedPipelineExecutionAndRepeatability(unittest.TestCase):
    """4 & 5. Staged Pipeline Execution, Repeatability, and Idempotency."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.temp_dir, "data")
        self.runner = StagedPipelineRunner(data_dir=self.data_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_synthetic_staging_dataset_counts(self):
        """17. Synthetic dataset produces exactly 50 candidates (20 qualified, 10 dup, 10 disq, 10 blocked)."""
        dataset = generate_synthetic_staging_dataset()
        self.assertEqual(len(dataset), 50)

        outcomes = [c["expected_outcome"] for c in dataset]
        self.assertEqual(outcomes.count("QUALIFIED"), 20)
        self.assertEqual(outcomes.count("DUPLICATE"), 10)
        self.assertEqual(outcomes.count("DISQUALIFIED"), 10)
        self.assertEqual(outcomes.count("BLOCKED_MANUAL_REVIEW"), 10)

    def test_staged_pipeline_full_run_zero_side_effects(self):
        """18. StagedPipelineRunner executes 14 stages with 0 CRM writes and 0 provider sends."""
        report = self.runner.run_staged_pipeline(run_id="STAGING-RUN-01")
        self.assertEqual(report["status"], "SUCCESS")
        self.assertEqual(report["runtime_mode"], "STAGING")
        self.assertEqual(report["metrics"]["total_discovered"], 50)
        self.assertEqual(report["metrics"]["unique_entities"], 40)
        self.assertEqual(report["metrics"]["duplicates_detected"], 10)
        self.assertEqual(report["metrics"]["qualified_count"], 20)
        self.assertEqual(report["metrics"]["disqualified_count"], 10)
        self.assertEqual(report["metrics"]["blocked_or_manual_review_count"], 10)

        # Confirm ZERO side effects
        self.assertEqual(report["side_effects"]["live_crm_writes"], 0)
        self.assertEqual(report["side_effects"]["live_provider_sends"], 0)
        self.assertEqual(report["side_effects"]["live_emails_sent"], 0)
        self.assertEqual(report["side_effects"]["campaigns_armed"], 0)

        # Machine-readable report created
        report_file = os.path.join(self.data_dir, "staging_pipeline_report.json")
        self.assertTrue(os.path.exists(report_file))

    def test_staged_pipeline_repeatability_and_idempotency_3_runs(self):
        """19. 3 identical runs produce identical canonical IDs, no duplicate multiplication, and stable metrics."""
        rep1 = self.runner.run_staged_pipeline(run_id="RUN-1")
        rep2 = self.runner.run_staged_pipeline(run_id="RUN-2")
        rep3 = self.runner.run_staged_pipeline(run_id="RUN-3")

        # Canonical lead IDs must be 100% identical
        self.assertEqual(rep1["canonical_lead_ids"], rep2["canonical_lead_ids"])
        self.assertEqual(rep2["canonical_lead_ids"], rep3["canonical_lead_ids"])

        # Metrics must remain identical
        self.assertEqual(rep1["metrics"], rep2["metrics"])
        self.assertEqual(rep2["metrics"], rep3["metrics"])


class TestInterruptionAndCheckpointRecovery(unittest.TestCase):
    """6. Interruption Handling & Atomic Write Recovery."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.target_file = os.path.join(self.temp_dir, "critical_state.json")
        atomic_write_json(self.target_file, {"valid_state": True, "records_count": 10})

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_atomic_write_interruption_preserves_original_state(self):
        """20. Simulated crash during atomic write leaves original target file uncorrupted."""
        # Attempt an interrupted write by injecting an exception during os.replace
        try:
            with patch("os.replace", side_effect=IOError("Simulated power loss during write")):
                atomic_write_json(self.target_file, {"corrupted": True})
        except IOError:
            pass

        # Target file must retain original valid content
        with open(self.target_file, "r") as f:
            data = json.load(f)
        self.assertEqual(data["valid_state"], True)
        self.assertEqual(data["records_count"], 10)

    def test_lock_release_and_recovery(self):
        """21. Lock holder release allows subsequent process to acquire without contention."""
        lock_path = os.path.join(self.temp_dir, "test.lock")
        lock1 = FileLock(lock_path, timeout=1.0)
        acquired = lock1.acquire()
        self.assertTrue(acquired)
        self.assertTrue(lock1.is_locked)

        # Release lock cleanly
        lock1.release()
        self.assertFalse(lock1.is_locked)

        # Process 2 can now acquire immediately
        lock2 = FileLock(lock_path, timeout=1.0)
        self.assertTrue(lock2.acquire())
        lock2.release()


class TestFailureInjectionMatrix(unittest.TestCase):
    """7. 16-Scenario Failure Injection Matrix."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.simulator = FailureInjectionSimulator(data_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_all_16_failure_scenarios_handled_safely(self):
        """22. All 16 failure scenarios classify deterministically with safe non-destructive fallbacks."""
        for scenario in FailureScenario:
            res = self.simulator.inject_and_evaluate(scenario)
            self.assertEqual(res["status"], "HANDLED_SAFELY")
            self.assertFalse(res["data_destruction_occurred"])
            self.assertFalse(res["commercial_side_effect_occurred"])
            self.assertTrue(res["visible_in_monitoring"])
            self.assertIn("incident_id", res)
            self.assertIn("recovery_path", res)


class TestDisasterRecoveryDrill(unittest.TestCase):
    """8. 13-Step Backup -> Restore -> Reconcile Disaster Recovery Drill."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.drill = DisasterRecoveryDrill(sandbox_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_disaster_recovery_drill_all_13_steps_pass(self):
        """23. Full disaster recovery drill restores state, preserves protected records, and reconciles cleanly."""
        report = self.drill.run_drill()
        self.assertEqual(report["drill_status"], "PASS")
        self.assertTrue(report["all_steps_passed"])

        # Verify critical checkpoints
        steps = report["steps"]
        self.assertEqual(steps["1_state_generation"]["status"], "PASS")
        self.assertEqual(steps["2_and_3_backup_creation_and_verification"]["status"], "PASS")
        self.assertEqual(steps["4_and_5_corruption_injection_and_detection"]["status"], "PASS")
        self.assertEqual(steps["6_restore_execution"]["status"], "PASS")
        self.assertEqual(steps["7_canonical_id_integrity"]["status"], "PASS")
        self.assertEqual(steps["8_protected_outreach_preservation"]["status"], "PASS")
        self.assertEqual(steps["9_and_10_history_and_suppression"]["status"], "PASS")
        self.assertEqual(steps["11_incident_preservation"]["status"], "PASS")
        self.assertEqual(steps["12_and_13_reconciliation_against_crm"]["status"], "PASS")


class TestRollbackAndPauseContract(unittest.TestCase):
    """9. Rollback & Pause State Machine Contract."""

    def tearDown(self):
        RuntimeModeManager.transition_state(OperationalState.RUNNING, reason="test_cleanup")

    def test_pause_and_resume_transitions(self):
        """24. System safely transitions RUNNING -> PAUSED -> RESUMED without state corruption."""
        RuntimeModeManager.transition_state(OperationalState.RUNNING)
        self.assertEqual(RuntimeModeManager.get_operational_state(), OperationalState.RUNNING)

        RuntimeModeManager.pause(reason="Maintenance")
        self.assertEqual(RuntimeModeManager.get_operational_state(), OperationalState.PAUSED)
        with self.assertRaises(OperationalStateError):
            RuntimeModeManager.assert_pipeline_execution_allowed("test_pipeline")

        RuntimeModeManager.resume(reason="Maintenance complete")
        self.assertEqual(RuntimeModeManager.get_operational_state(), OperationalState.RUNNING)

    def test_safe_mode_and_recovery_transitions(self):
        """25. System safely transitions RUNNING -> SAFE_MODE -> RECOVERY."""
        RuntimeModeManager.enter_safe_mode(reason="Suspected anomaly")
        self.assertEqual(RuntimeModeManager.get_operational_state(), OperationalState.SAFE_MODE)
        with self.assertRaises(OperationalStateError):
            RuntimeModeManager.assert_pipeline_execution_allowed("test_pipeline")

        RuntimeModeManager.recover(reason="Anomaly resolved")
        self.assertEqual(RuntimeModeManager.get_operational_state(), OperationalState.RUNNING)


class TestConfigurationPreflightAndWorkflows(unittest.TestCase):
    """10. Preflight CLI Verification & GitHub Actions Safety."""

    def test_production_preflight_script_exits_cleanly(self):
        """26. run_preflight() exits 0 under standard staging environment."""
        exit_code = run_preflight()
        self.assertEqual(exit_code, 0)

    def test_github_actions_workflows_least_privilege_and_no_cron(self):
        """27. All workflows have least-privilege permissions and zero active cron triggers."""
        wf_dir = os.path.join(PROJECT_ROOT, ".github", "workflows")
        self.assertTrue(os.path.exists(wf_dir))

        for fname in os.listdir(wf_dir):
            if fname.endswith((".yml", ".yaml")):
                path = os.path.join(wf_dir, fname)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()

                # Least privilege check
                self.assertIn("contents: read", content, f"Workflow {fname} lacks 'contents: read'")

                # Scheduled cron check (must be commented out or absent)
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("- cron:"):
                        self.assertTrue(
                            stripped.startswith("#"),
                            f"Active cron schedule found in {fname}: {line}",
                        )


if __name__ == "__main__":
    unittest.main()
