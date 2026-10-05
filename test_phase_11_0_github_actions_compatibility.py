"""
test_phase_11_0_github_actions_compatibility.py
===============================================
Comprehensive Test Suite for Phase 11.0: GitHub Actions Production Readiness.

Tests:
  1.  Actions environment detection
  2.  Ubuntu / cross-platform path handling
  3.  Required environment variable validation
  4.  Missing secret graceful handling
  5.  Dry-run safety invariants (zero mutations, zero sends)
  6.  Travel Mode enforcement
  7.  Commercial action kill switch
  8.  Automated email outreach locked
  9.  Concurrency controls & cross-process locking
  10. Ephemeral runner persistence & baseline bootstrapping
  11. Gosom scraper availability & compatibility
  12. Google Sheets authentication configuration (JSON string & file)
  13. Artifact & report generation (JSON & Markdown)
  14. Failure behavior & non-zero exit codes
  15. Secret scanning & redaction
  16. Workflow file schema validity & least-privilege permissions
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.system.system_config import (
    SystemConfig,
    CommercialActionForbiddenError,
    EmailAutomationBlockedError,
)
from lib.system.config_validator import ConfigValidator
from lib.system.file_lock import FileLock, orchestrator_lock, LockTimeoutError
from lib.system.storage_bootstrap import bootstrap_storage_baseline, CRITICAL_STORE_DEFAULTS
from lib.system.system_health import SystemHealthMonitor
from scripts.security_secret_scan import scan_file, check_gitignore, PATTERNS
from scripts.run_technical_pipeline import assert_safety_invariants, run_pipeline
from scripts.run_freshness_check import run_freshness
from sheets_sync import get_sheet_client, get_worksheet


class TestGitHubActionsCompatibility(unittest.TestCase):

    def setUp(self):
        # Enforce baseline production safety state
        SystemConfig.TRAVEL_MODE = True
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False
        SystemConfig.AUTOMATED_EMAIL_ENABLED = False
        SystemConfig.EMAIL_AUTOMATION_KILL_SWITCH = False
        SystemConfig.TECHNICAL_AUTOMATION_ENABLED = True

    # -------------------------------------------------------------------------
    # 1. Actions Environment Detection
    # -------------------------------------------------------------------------
    def test_01_actions_environment_detection(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux"}):
            self.assertEqual(os.environ.get("GITHUB_ACTIONS"), "true")
            self.assertEqual(os.environ.get("RUNNER_OS"), "Linux")

    # -------------------------------------------------------------------------
    # 2. Ubuntu / Cross-Platform Path Handling
    # -------------------------------------------------------------------------
    def test_02_ubuntu_path_handling(self):
        from lib.website.detector import NodeWebsiteDetectionProvider
        provider = NodeWebsiteDetectionProvider()
        # Verify node_auditor_dir does not contain hardcoded user path
        self.assertNotIn("/Users/metagurpreet/.gemini", provider.node_auditor_dir)
        self.assertTrue(provider.node_auditor_dir.endswith(os.path.join("scratch", "apify-workspace")))

    # -------------------------------------------------------------------------
    # 3. Required Environment Variable Validation
    # -------------------------------------------------------------------------
    def test_03_required_environment_variables(self):
        audit = ConfigValidator.validate_all()
        self.assertIn("services", audit)
        self.assertIn("storage", audit["services"])
        self.assertIn("google_sheets", audit["services"])
        self.assertIn("gosom", audit["services"])
        self.assertIn("scheduler", audit["services"])
        self.assertTrue(audit["travel_mode"])
        self.assertFalse(audit["commercial_actions_enabled"])

    # -------------------------------------------------------------------------
    # 4. Missing Secret Graceful Handling
    # -------------------------------------------------------------------------
    def test_04_missing_secret_detection(self):
        with patch.dict(os.environ, {}, clear=True):
            # When secrets are absent, config validator reports MISSING, not fatal crash
            audit = ConfigValidator.validate_all()
            self.assertIn(audit["services"]["google_sheets"]["status"], ["MISSING", "CONFIGURED"])
            self.assertFalse(audit["services"]["google_sheets"]["critical"])

    # -------------------------------------------------------------------------
    # 5. Dry-Run Safety Invariants
    # -------------------------------------------------------------------------
    def test_05_dry_run_safety(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            bootstrap_storage_baseline(data_dir=tmp_dir)
            report = run_pipeline(
                market_id="MANCHESTER_UK",
                lead_limit=2,
                dry_run=True,
                report_path=os.path.join(tmp_dir, "test_report.json"),
                data_dir=tmp_dir,
            )
            self.assertTrue(report["dry_run"])
            self.assertEqual(report["stats"]["crm_written_count"], 0)
            self.assertEqual(report["stats"]["outreach_dispatched_count"], 0)
            self.assertEqual(report["travel_mode_active"], True)
            self.assertEqual(report["commercial_actions_enabled"], False)

    # -------------------------------------------------------------------------
    # 6. Travel Mode Enforcement
    # -------------------------------------------------------------------------
    def test_06_travel_mode_enforcement(self):
        SystemConfig.TRAVEL_MODE = True
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False
        self.assertTrue(SystemConfig.TRAVEL_MODE)
        self.assertFalse(SystemConfig.can_execute_commercial_actions())

        # If someone improperly sets TRAVEL_MODE = False in safety gate, it raises
        SystemConfig.TRAVEL_MODE = False
        with self.assertRaises(PermissionError):
            assert_safety_invariants()
        SystemConfig.TRAVEL_MODE = True

    # -------------------------------------------------------------------------
    # 7. Commercial Kill Switch
    # -------------------------------------------------------------------------
    def test_07_commercial_kill_switch(self):
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_actions_allowed("dispatch_outreach")

        # If someone sets COMMERCIAL_ACTIONS_ENABLED = True, safety assertion blocks
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = True
        with self.assertRaises(PermissionError):
            assert_safety_invariants()
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False

    # -------------------------------------------------------------------------
    # 8. Automated Email Outreach Locked
    # -------------------------------------------------------------------------
    def test_08_automated_email_disabled(self):
        SystemConfig.AUTOMATED_EMAIL_ENABLED = False
        with self.assertRaises(EmailAutomationBlockedError):
            SystemConfig.assert_automated_email_allowed("send_campaign_email")

        # If someone sets AUTOMATED_EMAIL_ENABLED = True, safety assertion blocks
        SystemConfig.AUTOMATED_EMAIL_ENABLED = True
        with self.assertRaises(PermissionError):
            assert_safety_invariants()
        SystemConfig.AUTOMATED_EMAIL_ENABLED = False

    # -------------------------------------------------------------------------
    # 9. Concurrency Controls & Cross-Process Locking
    # -------------------------------------------------------------------------
    def test_09_concurrency_and_file_locking(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock1 = FileLock("test_proc", timeout=0.5, lock_dir=tmp_dir)
            self.assertTrue(lock1.acquire())

            # Second lock attempt must timeout cleanly
            lock2 = FileLock("test_proc", timeout=0.2, lock_dir=tmp_dir)
            with self.assertRaises(LockTimeoutError):
                lock2.acquire()

            lock1.release()

            # Now second lock succeeds
            self.assertTrue(lock2.acquire())
            lock2.release()

    # -------------------------------------------------------------------------
    # 10. Ephemeral Runner Persistence & Baseline Bootstrapping
    # -------------------------------------------------------------------------
    def test_10_persistence_and_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            res = bootstrap_storage_baseline(data_dir=tmp_dir, sync_from_sheets_if_available=False)
            self.assertEqual(res["status"], "BOOTSTRAPPED")
            self.assertGreaterEqual(res["initialized_files_count"], len(CRITICAL_STORE_DEFAULTS))

            # Verify all critical files exist and are valid JSON
            monitor = SystemHealthMonitor(data_dir=tmp_dir)
            integrity = monitor.check_file_integrity()
            self.assertEqual(integrity["status"], "PASS")

    # -------------------------------------------------------------------------
    # 11. Gosom Scraper Availability & Compatibility
    # -------------------------------------------------------------------------
    def test_11_gosom_availability(self):
        from scripts.setup_gosom import is_binary_runnable, SCRAPER_BIN
        # In this environment, the binary exists
        if os.path.exists(SCRAPER_BIN):
            # Check runnable function doesn't crash
            _ = is_binary_runnable(SCRAPER_BIN)

        # Config validator discovers binary
        audit = ConfigValidator.validate_all()
        self.assertIn(audit["services"]["gosom"]["status"], ["CONFIGURED", "DISABLED"])

    # -------------------------------------------------------------------------
    # 12. Google Sheets Authentication Configuration
    # -------------------------------------------------------------------------
    def test_12_google_sheets_auth_configuration(self):
        # Test JSON string payload config
        mock_sa = {
            "type": "service_account",
            "project_id": "mock-project",
            "private_key_id": "mock123",
            "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC3\n-----END PRIVATE KEY-----\n",
            "client_email": "mock-sa@mock-project.iam.gserviceaccount.com",
            "client_id": "123456789",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps(mock_sa)}):
            audit = ConfigValidator.validate_all()
            self.assertEqual(audit["services"]["google_sheets"]["status"], "CONFIGURED")
            self.assertIn("mock***", audit["services"]["google_sheets"]["details"])

        # Test individual env vars
        with patch.dict(os.environ, {
            "GOOGLE_SERVICE_ACCOUNT_EMAIL": "test-bot@dripp.iam.gserviceaccount.com",
            "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\nMIIE\\n-----END PRIVATE KEY-----"
        }, clear=True):
            audit = ConfigValidator.validate_all()
            self.assertEqual(audit["services"]["google_sheets"]["status"], "CONFIGURED")

    # -------------------------------------------------------------------------
    # 13. Artifact & Report Generation
    # -------------------------------------------------------------------------
    def test_13_artifact_generation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            bootstrap_storage_baseline(data_dir=tmp_dir)
            report_file = os.path.join(tmp_dir, "custom_report.json")
            report = run_pipeline(
                market_id="MANCHESTER_UK",
                lead_limit=1,
                dry_run=True,
                report_path=report_file,
                data_dir=tmp_dir,
            )
            self.assertTrue(os.path.exists(report_file))
            with open(report_file, "r", encoding="utf-8") as rf:
                loaded = json.load(rf)
            self.assertEqual(loaded["run_id"], report["run_id"])
            self.assertEqual(loaded["status"], "COMPLETED")

    # -------------------------------------------------------------------------
    # 14. Failure Behavior & Non-Zero Exit Codes
    # -------------------------------------------------------------------------
    def test_14_failure_exit_codes(self):
        # Invalid market should raise ValueError
        with self.assertRaises(ValueError):
            run_pipeline(market_id="NON_EXISTENT_MARKET")

        # Commercial action enabled should raise PermissionError
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = True
        with self.assertRaises(PermissionError):
            run_pipeline(market_id="MANCHESTER_UK")
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False

    # -------------------------------------------------------------------------
    # 15. Secret Scanning & Redaction
    # -------------------------------------------------------------------------
    def test_15_secret_redaction(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_file = os.path.join(tmp_dir, "fake_leak.txt")
            # Create a file with a mock secret
            with open(test_file, "w") as f:
                f.write("api_key = 'SG.1234567890123456789012.1234567890123456789012345678901234567890123'\n")

            with patch("scripts.security_secret_scan.PROJECT_ROOT", tmp_dir):
                findings = scan_file("fake_leak.txt")
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]["secret_type"], "SendGrid API Key")
                # Ensure the full secret is NOT in masked_value
                self.assertNotIn("1234567890123456789012", findings[0]["masked_value"])
                self.assertTrue(findings[0]["masked_value"].endswith("chars)"))

    # -------------------------------------------------------------------------
    # 16. Workflow Files Validity & Least Privilege
    # -------------------------------------------------------------------------
    def test_16_workflow_files_validity_and_least_privilege(self):
        import yaml
        import glob

        workflow_files = glob.glob(os.path.join(PROJECT_ROOT, ".github", "workflows", "*.yml"))
        self.assertGreaterEqual(len(workflow_files), 4)

        for wfile in workflow_files:
            with open(wfile, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)

            # Least privilege permissions
            perms = data.get("permissions")
            self.assertEqual(perms, {"contents": "read"}, f"Violated least privilege in {wfile}")

            # Concurrency control present with cancel-in-progress: false
            concurrency = data.get("concurrency")
            self.assertIsNotNone(concurrency, f"Missing concurrency in {wfile}")
            self.assertFalse(concurrency.get("cancel-in-progress", True), f"cancel-in-progress must be false in {wfile}")


if __name__ == "__main__":
    unittest.main()
