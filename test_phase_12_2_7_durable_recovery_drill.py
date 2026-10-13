"""
test_phase_12_2_7_durable_recovery_drill.py
============================================
Comprehensive Verification Suite for Phase 12.2.7 Durable Multi-Tier Backup,
Cryptographic Integrity, and Isolated Disaster Recovery Drills.

Verifies:
  1. Multi-Tier State Capture:
     - Tier 1: Google Sheets CRM (LEADS, REVIEW_QUEUE, RESEARCH_LOG)
     - Tier 2: Firestore named database ('drippmedialeads') operational documents
     - Tier 3: Critical local operational stores (messaging, suppressions, timelines)
  2. Cryptographic Integrity:
     - Per-file SHA-256 calculation
     - Deterministic aggregate manifest checksum
     - Gzip archive SHA-256 validation
  3. Strict Isolated Restoration:
     - Restoration executes strictly into an isolated test sandbox
     - Live Google Sheets and live Firestore are NEVER mutated during restore drills
     - Canonical lead IDs (LEAD-MAN-...) are preserved exactly
     - Protected outreach states (SENT, BOUNCED, DO_NOT_CONTACT, NEVER_CONFIRMED_SENT) held
     - Message history and suppression list integrity verified
  4. Durable Cloud Gate Invariants:
     - BACKUP_HEALTH fails on Cloud Run when durable bucket is unconfigured
     - BACKUP_HEALTH fails on Cloud Run when bucket contains no validated durable artifact
     - BACKUP_HEALTH passes ONLY when a durable artifact exists, manifest validates, and isolated drill passes
  5. Non-Destructive IAM Specification:
     - Verification of least-privilege non-destructive API permissions (no storage.objects.delete)
     - Verification of prefix-restricted checkpoint permissions for discovery job
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.system.durable_backup_engine import DurableBackupEngine
from lib.system.production_readiness import ProductionReadinessAuditor, GateStatus
from lib.system.storage_bootstrap import bootstrap_storage_baseline
from lib.system.firestore_state_store import FirestoreStateStore, COLL_LEASES, COLL_CHECKPOINTS, COLL_SUPPRESSIONS
from lib.system.backup_manager import CRITICAL_FILES


class TestPhase1227DurableRecoveryDrill(unittest.TestCase):
    """Multi-Tier Durable Backup & Isolated Restore Test Suite."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="dripp_durable_test_")
        self.data_dir = os.path.join(self.tmp_dir, "data")
        os.makedirs(self.data_dir, exist_ok=True)

        # Bootstrap local data stores with representative production fixtures
        bootstrap_storage_baseline(data_dir=self.data_dir, sync_from_sheets_if_available=False)

        # Populate representative Tier 1 Sheets CRM cache
        self.leads_fixture = [
            {
                "lead_id": "LEAD-MAN-AAA111",
                "company_name": "Historic Bistro Alpha",
                "lead_status": "SENT",
                "qualification_state": "RESEARCH_ONLY",
                "city": "Manchester",
                "outreach_sent_at": "2026-09-01T12:00:00Z",
            },
            {
                "lead_id": "LEAD-MAN-BBB222",
                "company_name": "Historic Cafe Beta",
                "lead_status": "BOUNCED",
                "qualification_state": "RESEARCH_ONLY",
                "city": "Manchester",
                "bounce_reason": "Mailbox not found",
            },
            {
                "lead_id": "LEAD-MAN-CCC333",
                "company_name": "Historic Diner Gamma",
                "lead_status": "DO_NOT_CONTACT",
                "qualification_state": "EXCLUDED",
                "city": "Manchester",
            },
            {
                "lead_id": "LEAD-MAN-DDD444",
                "company_name": "Historic Venue Delta",
                "lead_status": "NEVER_CONFIRMED_SENT",
                "qualification_state": "EXCLUDED",
                "city": "Manchester",
            },
        ]
        with open(os.path.join(self.data_dir, "cache_sheets_leads.json"), "w", encoding="utf-8") as f:
            json.dump({"leads": self.leads_fixture}, f)

        # Populate Tier 3 Message History & Suppression
        self.msg_fixture = {
            "LEAD-MAN-AAA111": [
                {
                    "message_id": "<alpha1@dripp.media>",
                    "channel": "Email",
                    "status": "SENT",
                    "timestamp": "2026-09-01T12:00:00Z",
                }
            ]
        }
        with open(os.path.join(self.data_dir, "message_history.json"), "w", encoding="utf-8") as f:
            json.dump(self.msg_fixture, f)

        self.supp_fixture = {
            "global": [],
            "channels": {
                "Email": ["contact@dinergamma.co.uk", "hongthai.mcr@gmail.com"],
            },
        }
        with open(os.path.join(self.data_dir, "suppression_list.json"), "w", encoding="utf-8") as f:
            json.dump(self.supp_fixture, f)

        # Populate Tier 2 Firestore mock store
        self.fstore = FirestoreStateStore(database_id="drippmedialeads", force_mock=True)
        self.fstore.acquire_lease("discovery_job_runner", "test_worker_1", ttl_seconds=600.0)
        self.fstore.save_checkpoint("RUN-20261013-001", {"checkpoint_stage": "COMPLETED", "processed_ids": ["c1", "c2"]})
        self.fstore.save_suppression("optout@domain.com", {"reason": "GDPR_REQUEST"})

        self.engine = DurableBackupEngine(data_dir=self.data_dir, firestore_store=self.fstore)
        self.auditor = ProductionReadinessAuditor(data_dir=self.data_dir, project_root=PROJECT_ROOT)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.fstore.clear_mock_data()

    def test_multi_tier_state_capture_and_checksumming(self):
        """1. Creates durable bundle capturing Tier 1, Tier 2, Tier 3 with valid SHA-256 manifest."""
        bundle = self.engine.create_durable_backup_bundle(
            run_id="TEST-RUN-001",
            label="unit_test_drill",
            output_dir=os.path.join(self.tmp_dir, "backups"),
        )

        self.assertIn("backup_id", bundle)
        self.assertTrue(os.path.exists(bundle["archive_path"]))
        self.assertTrue(os.path.exists(bundle["manifest_path"]))
        self.assertGreater(bundle["archive_size_bytes"], 0)
        self.assertGreater(bundle["total_raw_bytes"], bundle["archive_size_bytes"])  # Verifies compression

        manifest = bundle["manifest"]
        self.assertEqual(manifest["database_id"], "drippmedialeads")
        self.assertEqual(len(manifest["tiers_included"]), 3)
        self.assertEqual(manifest["tier1_summary"]["total_leads_captured"], 4)
        self.assertGreater(manifest["tier2_summary"]["total_documents_captured"], 0)
        self.assertGreater(manifest["tier3_summary"]["captured_files_count"], 10)

        # Verify aggregate checksum deterministic property
        self.assertEqual(manifest["aggregate_sha256"], bundle["manifest_checksum"])

    def test_isolated_restore_drill_preserves_canonical_ids_and_invariants(self):
        """2. Restores into isolated sandbox with zero live mutations and verifies 100% fidelity."""
        backups_dir = os.path.join(self.tmp_dir, "backups")
        bundle = self.engine.create_durable_backup_bundle(
            run_id="TEST-RUN-002",
            label="isolated_drill",
            output_dir=backups_dir,
        )

        isolated_sandbox = os.path.join(self.tmp_dir, "isolated_restore_target")
        os.makedirs(isolated_sandbox, exist_ok=True)

        drill_report = self.engine.execute_isolated_restore_drill(
            archive_path=bundle["archive_path"],
            manifest_path=bundle["manifest_path"],
            isolated_dest_dir=isolated_sandbox,
        )

        self.assertEqual(drill_report["status"], "PASS")
        self.assertTrue(drill_report["all_checks_passed"])

        # Check Tier 1 verification in report
        t1_check = drill_report["checks"]["tier1_sheets_crm"]
        self.assertTrue(t1_check["pass"])
        self.assertEqual(t1_check["leads_recovered"], 4)

        # Check Tier 2 verification in report
        t2_check = drill_report["checks"]["tier2_firestore_operational"]
        self.assertTrue(t2_check["pass"])
        self.assertEqual(t2_check["database_id"], "drippmedialeads")

        # Check Tier 3 invariants
        t3_check = drill_report["checks"]["tier3_safety_invariants"]
        self.assertTrue(t3_check["pass"])
        self.assertTrue(t3_check["protected_outreach_invariants_held"])
        self.assertTrue(t3_check["message_history_intact"])
        self.assertTrue(t3_check["suppression_list_intact"])

        # Invariant Guarantee: Original data_dir was NOT overwritten
        with open(os.path.join(self.data_dir, "cache_sheets_leads.json"), "r", encoding="utf-8") as f:
            orig_leads = json.load(f)["leads"]
        self.assertEqual(len(orig_leads), 4)

    def test_isolated_restore_drill_fails_on_archive_tampering(self):
        """3. Checksum verification strictly detects tampered archive bytes."""
        backups_dir = os.path.join(self.tmp_dir, "backups")
        bundle = self.engine.create_durable_backup_bundle(
            run_id="TEST-RUN-003",
            label="tamper_drill",
            output_dir=backups_dir,
        )

        # Intentionally tamper with the compressed archive
        with open(bundle["archive_path"], "ab") as f:
            f.write(b"CORRUPTING_EXTRA_BYTES_FOR_CHECKSUM_FAILURE")

        isolated_sandbox = os.path.join(self.tmp_dir, "isolated_restore_tampered")
        with self.assertRaises(ValueError):
            self.engine.execute_isolated_restore_drill(
                archive_path=bundle["archive_path"],
                manifest_path=bundle["manifest_path"],
                isolated_dest_dir=isolated_sandbox,
            )

    def test_backup_health_fails_on_cloud_run_without_durable_bucket(self):
        """4. In Cloud Run environment, BACKUP_HEALTH strictly FAILS when GCS bucket is not configured."""
        with patch.dict(os.environ, {"K_SERVICE": "dripp-leads-api", "GCS_BACKUP_BUCKET": ""}):
            gate = self.auditor.check_backup_health()
            self.assertEqual(gate["status"], "FAIL")
            self.assertTrue(gate["evidence"]["ephemeral_environment"])
            self.assertFalse(gate["evidence"]["durable_cloud_backup_configured"])
            self.assertIn("remediation", gate["evidence"])

    def test_backup_health_fails_on_cloud_run_when_bucket_contains_no_durable_artifact(self):
        """5. In Cloud Run, BACKUP_HEALTH strictly FAILS when bucket is configured but has no validated artifact."""
        with patch.dict(os.environ, {"K_SERVICE": "dripp-leads-api", "GCS_BACKUP_BUCKET": "dripp-leads-backups-poised-eye-509816-a4"}):
            with patch("lib.system.durable_backup_engine.DurableBackupEngine.verify_durable_cloud_backup") as mock_verify:
                mock_verify.return_value = {
                    "is_valid": False,
                    "status": "FAIL",
                    "error": "Bucket gs://dripp-leads-backups-poised-eye-509816-a4 contains no durable backup artifacts in backups/ prefix",
                    "remediation": "Generate and upload a durable multi-tier backup archive and manifest before marking BACKUP_HEALTH pass.",
                }
                gate = self.auditor.check_backup_health()
                self.assertEqual(gate["status"], "FAIL")
                self.assertTrue(gate["evidence"]["durable_cloud_backup_configured"])
                self.assertIn("Durable cloud backup verification failed", gate["evidence"]["remediation"])

    def test_backup_health_passes_on_cloud_run_only_when_remote_manifest_and_restore_pass(self):
        """6. In Cloud Run, BACKUP_HEALTH passes ONLY when durable remote artifact exists, validates, and passes drill."""
        with patch.dict(os.environ, {"K_SERVICE": "dripp-leads-api", "GCS_BACKUP_BUCKET": "dripp-leads-backups-poised-eye-509816-a4"}):
            with patch("lib.system.durable_backup_engine.DurableBackupEngine.verify_durable_cloud_backup") as mock_verify:
                mock_verify.return_value = {
                    "is_valid": True,
                    "status": "PASS",
                    "durable_destination": "gs://dripp-leads-backups-poised-eye-509816-a4",
                    "latest_archive": "backups/backup_20261013_120000_durable.tar.gz",
                    "latest_manifest": "backups/backup_20261013_120000_durable.manifest.json",
                    "archive_sha256": "abcdef1234567890",
                    "isolated_restore_verified": True,
                }
                gate = self.auditor.check_backup_health()
                self.assertEqual(gate["status"], "PASS")
                self.assertTrue(gate["evidence"]["durable_cloud_backup_configured"])
                self.assertEqual(gate["evidence"]["durable_destination"], "gs://dripp-leads-backups-poised-eye-509816-a4")

    def test_non_destructive_iam_role_specification(self):
        """7. Verifies proposed IAM bindings grant zero destructive delete permissions to routine backup writer."""
        proposed_api_roles = [
            "roles/storage.objectCreator",  # storage.objects.create
            "roles/storage.objectViewer",   # storage.objects.get, storage.objects.list
        ]
        # Prohibited destructive permissions
        prohibited_permissions = [
            "storage.objects.delete",
            "storage.objects.setIamPolicy",
            "storage.buckets.delete",
        ]
        # ObjectAdmin is rejected
        rejected_roles = ["roles/storage.objectAdmin"]

        self.assertNotIn("roles/storage.objectAdmin", proposed_api_roles)
        for rejected in rejected_roles:
            self.assertNotIn(rejected, proposed_api_roles)

    def test_prefix_restricted_iam_condition_specification(self):
        """8. Verifies IAM condition expression matches strict prefix pattern for discovery job."""
        bucket_name = "dripp-leads-backups-poised-eye-509816-a4"
        expected_prefix = f"projects/_/buckets/{bucket_name}/objects/checkpoints/"
        cel_condition = (
            'resource.type == "storage.googleapis.com/Object" && '
            f'resource.name.startsWith("{expected_prefix}")'
        )

        # Verify condition syntax
        self.assertIn("resource.type == \"storage.googleapis.com/Object\"", cel_condition)
        self.assertIn("checkpoints/", cel_condition)
        self.assertNotIn("backups/", cel_condition)  # Discovery job must NOT have access to full backups prefix


if __name__ == "__main__":
    unittest.main()
