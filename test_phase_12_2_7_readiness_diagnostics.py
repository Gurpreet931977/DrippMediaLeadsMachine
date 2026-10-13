"""
test_phase_12_2_7_readiness_diagnostics.py
==========================================
Phase 12.2.7 Production Readiness Diagnostics & Field Contract Verification Suite.

Validates:
  1. Canonical field contract for readiness gates:
     - gate_id, name, display_name, status, description, explanation, timestamp, last_checked
  2. Resilient rendering contracts (no undefined titles, unknown status never defaults to PASS)
  3. Ephemeral container (Cloud Run) detection & storage diagnostics
  4. Ephemeral backup diagnostics & durable cloud backup requirement
  5. Safe startup baseline storage bootstrapper
  6. Operator session remaining TTL contract
"""

import os
import sys
import json
import tempfile
import shutil
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.system.production_readiness import (
    ProductionReadinessAuditor,
    GateStatus,
    GateSeverity,
    ReadinessState,
)
from lib.system.storage_bootstrap import (
    bootstrap_storage_baseline,
    CRITICAL_STORE_DEFAULTS,
)
from lib.system.backup_manager import BackupManager


class TestPhase1227ReadinessDiagnostics(unittest.TestCase):
    """Phase 12.2.7 Diagnostics & Canonical Field Contract Tests."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.auditor = ProductionReadinessAuditor(data_dir=self.tmp_dir, project_root=PROJECT_ROOT)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_canonical_field_contract_all_20_gates(self):
        """1. Every readiness gate contains complete canonical contract fields without null or missing keys."""
        # Bootstrap baseline so checks execute cleanly
        bootstrap_storage_baseline(data_dir=self.tmp_dir, sync_from_sheets_if_available=False)
        report = self.auditor.audit_all_gates()
        self.assertEqual(len(report["gates"]), 20)

        required_canonical_keys = {
            "gate_id",
            "name",
            "display_name",
            "status",
            "description",
            "explanation",
            "timestamp",
            "last_checked",
            "severity",
            "blocking",
            "evidence",
        }

        for gate in report["gates"]:
            for key in required_canonical_keys:
                self.assertIn(key, gate, f"Gate {gate.get('gate_id')} missing canonical key '{key}'")

            # Must not be literal string 'undefined' or empty
            self.assertNotEqual(gate["name"], "undefined")
            self.assertNotEqual(gate["display_name"], "undefined")
            self.assertTrue(len(gate["name"]) > 0)
            self.assertTrue(len(gate["display_name"]) > 0)

            # Timestamps must be valid ISO-8601
            datetime.fromisoformat(gate["timestamp"].replace("Z", "+00:00"))
            datetime.fromisoformat(gate["last_checked"].replace("Z", "+00:00"))

            # Status must be valid enum value
            self.assertIn(gate["status"], [s.value for s in GateStatus])

    def test_storage_health_reports_actionable_remediation_on_unbootstrapped_storage(self):
        """2. When storage baseline is missing, STORAGE_HEALTH returns FAIL with actionable remediation."""
        # In a fresh empty directory without baseline files:
        gate = self.auditor.check_storage_health()
        self.assertEqual(gate["status"], "FAIL")
        self.assertEqual(gate["name"], "Storage Health")
        self.assertIn("remediation", gate["evidence"])
        self.assertIn("storage_bootstrap.bootstrap_storage_baseline", gate["evidence"]["remediation"])
        self.assertTrue(gate["evidence"]["filesystem_writable"])

    def test_cloud_run_ephemeral_storage_detected(self):
        """3. When running under Cloud Run (K_SERVICE/K_REVISION set), ephemeral storage is explicitly flagged."""
        with patch.dict(os.environ, {"K_SERVICE": "dripp-leads-api", "K_REVISION": "dripp-leads-api-00001"}):
            gate = self.auditor.check_storage_health()
            self.assertTrue(gate["evidence"]["ephemeral_filesystem"])

    def test_backup_health_ephemeral_container_requires_durable_cloud_backup(self):
        """4. In Cloud Run, BACKUP_HEALTH flags ephemeral container and lack of durable cloud backup."""
        with patch.dict(os.environ, {"K_SERVICE": "dripp-leads-api"}):
            gate = self.auditor.check_backup_health()
            self.assertTrue(gate["evidence"]["ephemeral_environment"])
            self.assertFalse(gate["evidence"]["durable_cloud_backup_configured"])
            self.assertEqual(gate["status"], "FAIL")
            self.assertIn("remediation", gate["evidence"])
            self.assertIn("Google Cloud Storage (GCS)", gate["evidence"]["remediation"])

    def test_storage_health_passes_once_bootstrapped(self):
        """5. Once storage baseline is safely bootstrapped, STORAGE_HEALTH passes cleanly."""
        boot_res = bootstrap_storage_baseline(data_dir=self.tmp_dir, sync_from_sheets_if_available=False)
        self.assertGreater(boot_res["initialized_files_count"], 0)

        gate = self.auditor.check_storage_health()
        self.assertEqual(gate["status"], "PASS")
        self.assertEqual(gate["evidence"]["status"], "PASS")

    def test_backup_health_requires_durable_storage_on_cloud_run(self):
        """6. Cloud Run requires durable cloud backup; local snapshot passes only in local development."""
        bootstrap_storage_baseline(data_dir=self.tmp_dir, sync_from_sheets_if_available=False)
        bm = BackupManager(data_dir=self.tmp_dir)
        bm.create_backup(label="test_baseline")

        # In local development: local snapshot with manifest passes
        with patch.dict(os.environ, {}, clear=True):
            gate_local = self.auditor.check_backup_health()
            self.assertEqual(gate_local["status"], "PASS")
            self.assertFalse(gate_local["evidence"]["durable_cloud_backup_configured"])

        # On Cloud Run without durable cloud storage: FAILS
        with patch.dict(os.environ, {"K_SERVICE": "dripp-leads-api", "GCS_BACKUP_BUCKET": ""}):
            gate_cloud = self.auditor.check_backup_health()
            self.assertEqual(gate_cloud["status"], "FAIL")
            self.assertTrue(gate_cloud["evidence"]["ephemeral_environment"])
            self.assertFalse(gate_cloud["evidence"]["durable_cloud_backup_configured"])
            self.assertIn("remediation", gate_cloud["evidence"])

        # On Cloud Run WITH durable GCS bucket configured but NO durable artifact: FAILS (Rule 6)
        with patch.dict(os.environ, {"K_SERVICE": "dripp-leads-api", "GCS_BACKUP_BUCKET": "dripp-leads-backups-test"}):
            with patch("lib.system.durable_backup_engine.DurableBackupEngine.verify_durable_cloud_backup", return_value={"is_valid": False, "status": "FAIL", "error": "No durable artifacts in bucket"}):
                gate_unverified = self.auditor.check_backup_health()
                self.assertEqual(gate_unverified["status"], "FAIL")
                self.assertTrue(gate_unverified["evidence"]["durable_cloud_backup_configured"])

        # On Cloud Run WITH durable GCS bucket configured AND verified durable artifact: PASSES
        with patch.dict(os.environ, {"K_SERVICE": "dripp-leads-api", "GCS_BACKUP_BUCKET": "dripp-leads-backups-test"}):
            with patch("lib.system.durable_backup_engine.DurableBackupEngine.verify_durable_cloud_backup", return_value={"is_valid": True, "status": "PASS", "isolated_restore_verified": True}):
                gate_durable = self.auditor.check_backup_health()
                self.assertEqual(gate_durable["status"], "PASS")
                self.assertTrue(gate_durable["evidence"]["durable_cloud_backup_configured"])
                self.assertEqual(gate_durable["evidence"]["durable_destination"], "gs://dripp-leads-backups-test")

    def test_operator_session_status_contract(self):
        """7. /api/operator/session returns session_id, expires_at, and remaining_seconds for valid session tokens."""
        from server import app, operator_sessions, create_operator_session
        from starlette.testclient import TestClient

        client = TestClient(app)
        session_info = create_operator_session()
        token = session_info["session_token"]

        response = client.get("/api/operator/session", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["authenticated"])
        self.assertIn("expires_at", data)
        self.assertIn("remaining_seconds", data)
        self.assertGreater(data["remaining_seconds"], 7100)
        self.assertIn("session_id", data)

    def test_secret_hygiene_in_clean_container_environment(self):
        """8. In container runtime without .git, clean filesystem passes without relying on .gitignore file."""
        with patch("os.path.exists", side_effect=lambda p: False if p.endswith(".git") else os.path.exists(p)):
            with patch("scripts.security_secret_scan.check_prohibited_files", return_value=[]):
                with patch("scripts.security_secret_scan.scan_runtime_files", return_value=[]):
                    with patch.dict(os.environ, {"K_SERVICE": "dripp-leads-api"}):
                        gate = self.auditor.check_secret_hygiene()
                        self.assertEqual(gate["status"], "PASS")
                        self.assertEqual(gate["evidence"]["vcs_context"], "container_runtime")
                        self.assertTrue(gate["evidence"]["credentials_excluded"])

    def test_secret_hygiene_never_bypassed_in_container_when_secret_detected(self):
        """9. In container runtime (Cloud Run), simulated secret pattern strictly triggers FAIL."""
        with patch("os.path.exists", side_effect=lambda p: False if p.endswith(".git") else os.path.exists(p)):
            mock_secret_hits = [{
                "file": "server.py",
                "line": 42,
                "secret_type": "Simulated Live Key",
                "masked_value": "simu***(32 chars)",
            }]
            with patch("scripts.security_secret_scan.check_prohibited_files", return_value=[]):
                with patch("scripts.security_secret_scan.scan_runtime_files", return_value=mock_secret_hits):
                    with patch.dict(os.environ, {"K_SERVICE": "dripp-leads-api"}):
                        gate = self.auditor.check_secret_hygiene()
                        self.assertEqual(gate["status"], "FAIL")
                        self.assertTrue(gate["blocking"])
                        self.assertFalse(gate["evidence"]["credentials_excluded"])
                        self.assertGreater(gate["evidence"]["secret_findings_count"], 0)

    def test_secret_hygiene_fails_in_container_when_prohibited_credential_file_exists(self):
        """10. In container runtime (Cloud Run), presence of .env or key file strictly triggers FAIL."""
        with patch("os.path.exists", side_effect=lambda p: False if p.endswith(".git") else os.path.exists(p)):
            with patch("scripts.security_secret_scan.check_prohibited_files", return_value=[".env"]):
                with patch("scripts.security_secret_scan.scan_runtime_files", return_value=[]):
                    with patch.dict(os.environ, {"K_SERVICE": "dripp-leads-api"}):
                        gate = self.auditor.check_secret_hygiene()
                        self.assertEqual(gate["status"], "FAIL")
                        self.assertTrue(gate["blocking"])
                        self.assertIn(".env", gate["evidence"]["prohibited_files_detected"])


if __name__ == "__main__":
    unittest.main()
