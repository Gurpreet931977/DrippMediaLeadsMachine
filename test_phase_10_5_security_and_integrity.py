"""
test_phase_10_5_security_and_integrity.py
==========================================
Comprehensive Test Suite for Phase 10.5 — Security, Data Integrity & Backup/Restore Hardening.

Covers:
  1. Full secret audit & regex scanning
  2. Structured log & payload secret redaction
  3. Backup manifest, checksum, and completion status verification
  4. Configurable backup retention & valid backup preservation
  5. Isolated full synthetic restore procedure (Section 7)
  6. Data store corruption, truncation, and missing file detection
  7. Google Sheets CRM authority reconciliation
  8. Protected outreach states (SENT, BOUNCED, SUPPRESSED, NEVER_CONFIRMED_SENT) immutability
  9. Canonical ID stability across attribute mutations
 10. Atomic write integrity and file lock recovery
 11. GitHub Actions least-privilege permission validation
 12. Phase 10.3 Monitoring incident generation
"""

import os
import re
import json
import yaml
import shutil
import hashlib
import tempfile
import unittest
from datetime import datetime, timezone

from scripts.security_secret_scan import run_security_scan, check_gitignore
from lib.system.observability import sanitize_text, sanitize_payload
from lib.system.backup_manager import BackupManager, calculate_sha256
from lib.system.state_integrity import (
    StateIntegrityEngine,
    StateCorruptionError,
    CRITICAL_STORE_FILES,
)
from lib.system.identity_integrity import IdentityIntegrityAuditor, CANONICAL_LEAD_ID_REGEX
from lib.system.atomic_writer import atomic_write_json
from lib.system.file_lock import FileLock
from lib.system.system_config import SystemConfig
from lib.monitoring.incident_manager import IncidentManager
from lib.monitoring.detectors import SecurityAndIntegrityMonitor
from lib.monitoring.incident_types import IncidentType, IncidentSeverity


class TestSecretScanAndRedaction(unittest.TestCase):
    """Verifies repository secret scanning and log redaction layers."""

    def test_repository_secret_scan_passes(self):
        """1. Repository secret scan detects 0 unallowed credentials in tracked files."""
        result = run_security_scan()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["secrets_found_count"], 0)
        self.assertEqual(len(result["findings"]), 0)

    def test_gitignore_covers_sensitive_items(self):
        """2. .gitignore properly covers all sensitive credential and state directories."""
        missing = check_gitignore()
        self.assertEqual(missing, [], f"Missing gitignore entries: {missing}")

    def test_log_redaction_text_patterns(self):
        """3. sanitize_text masks Tavily keys, GitHub tokens, private keys, SMTP pass, Basic/Bearer auth."""
        tvly_dummy = "tvly-" + "prod1234567890abcdef1234567890"
        ghp_dummy = "ghp_" + "LIVE1234567890ABCDEF1234567890"
        smtp_dummy = "smtp_" + "pass='SuperSecretP@ss1'"
        rsa_dummy = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END " + "RSA PRIVATE KEY-----"
        sample_text = (
            f"Connected to smtp.mail.com with {smtp_dummy} "
            f"using {tvly_dummy} and {ghp_dummy} "
            "with Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig and Basic dXNlcjpwYXNz "
            f"{rsa_dummy}"
        )
        redacted = sanitize_text(sample_text)
        self.assertNotIn("SuperSecretP@ss1", redacted)
        self.assertNotIn("tvly-prod1234567890", redacted)
        self.assertNotIn("ghp_LIVE1234", redacted)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", redacted)
        self.assertNotIn("dXNlcjpwYXNz", redacted)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", redacted)
        self.assertIn("[MASKED_PASSWORD]", redacted)
        self.assertIn("[REDACTED_TAVILY_KEY]", redacted)
        self.assertIn("[REDACTED_GITHUB_TOKEN]", redacted)
        self.assertIn("[REDACTED_PRIVATE_KEY]", redacted)

    def test_log_redaction_nested_payload(self):
        """4. sanitize_payload recursively sanitizes dictionary keys and lists."""
        payload = {
            "api_key": "tvly-secretkey999",
            "smtp_pass": "mail_password",
            "nested": {
                "password": "mypassword123",
                "normal_field": "visible_value",
                "credentials": ["secret_token_1", "secret_token_2"],
            },
            "clean_list": ["item1", "item2"],
        }
        cleaned = sanitize_payload(payload)
        self.assertEqual(cleaned["api_key"], "[REDACTED_SECRET]")
        self.assertEqual(cleaned["smtp_pass"], "[REDACTED_SECRET]")
        self.assertEqual(cleaned["nested"]["password"], "[REDACTED_SECRET]")
        self.assertEqual(cleaned["nested"]["normal_field"], "visible_value")
        self.assertEqual(cleaned["clean_list"], ["item1", "item2"])


class TestBackupManifestAndVerification(unittest.TestCase):
    """Verifies BackupManager manifest generation, checksumming, and validation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.temp_dir, "data")
        self.backups_dir = os.path.join(self.temp_dir, "backups")
        os.makedirs(self.data_dir, exist_ok=True)
        self.manager = BackupManager(data_dir=self.data_dir, backups_dir=self.backups_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_backup_creates_manifest_with_all_required_metadata(self):
        """5. Backup records backup_id, created_at, source_state, manifest, checksum, counts, completion_status."""
        test_file = os.path.join(self.data_dir, "cache_sheets_leads.json")
        with open(test_file, "w") as f:
            json.dump({"leads": [{"lead_id": "LEAD-MAN-111111"}]}, f)

        manifest = self.manager.create_backup(run_id="TEST-RUN-01", label="preflight")
        self.assertIn("backup_id", manifest)
        self.assertIn("created_at", manifest)
        self.assertEqual(manifest["source_state"], self.data_dir)
        self.assertIn("manifest_checksum", manifest)
        self.assertIn("checksum", manifest)
        self.assertEqual(manifest["file_count"], 1)
        self.assertGreater(manifest["byte_size"], 0)
        self.assertEqual(manifest["completion_status"], "SUCCESS")

    def test_backup_fails_verification_on_tampered_file(self):
        """6. Backup verification detects and reports tampered files or checksum mismatches."""
        test_file = os.path.join(self.data_dir, "cache_sheets_leads.json")
        with open(test_file, "w") as f:
            json.dump({"leads": [{"lead_id": "LEAD-MAN-111111"}]}, f)

        manifest = self.manager.create_backup(label="snapshot")
        bid = manifest["backup_id"]

        # Tamper with the backed-up file directly
        backup_file = os.path.join(self.backups_dir, bid, "cache_sheets_leads.json")
        with open(backup_file, "w") as f:
            f.write("tampered content")

        is_valid, errors = self.manager.verify_backup_integrity(bid)
        self.assertFalse(is_valid)
        self.assertTrue(any("Checksum mismatch" in e for e in errors))

    def test_backup_retention_preserves_newest_and_previous_valid(self):
        """7. Prune backups removes excess but strictly protects newest and previous valid backups."""
        test_file = os.path.join(self.data_dir, "cache_sheets_leads.json")
        with open(test_file, "w") as f:
            json.dump({"leads": []}, f)

        b1 = self.manager.create_backup(label="b1")["backup_id"]
        b2 = self.manager.create_backup(label="b2")["backup_id"]
        b3 = self.manager.create_backup(label="b3")["backup_id"]
        b4 = self.manager.create_backup(label="b4")["backup_id"]

        report = self.manager.prune_backups(retention_count=2, min_valid_to_keep=2)
        # Should keep 2, prune 2
        self.assertEqual(report["pruned_count"], 2)
        self.assertEqual(report["retained_count"], 2)
        # Verify b4 and b3 (newest 2) are retained
        self.assertIn(b4, report["retained_backups"])
        self.assertIn(b3, report["retained_backups"])
        self.assertIn(b1, report["pruned_backups"])
        self.assertIn(b2, report["pruned_backups"])

    def test_backup_retention_never_deletes_single_valid_backup(self):
        """8. Prune backups never deletes the only available valid backup."""
        test_file = os.path.join(self.data_dir, "cache_sheets_leads.json")
        with open(test_file, "w") as f:
            json.dump({"leads": []}, f)

        single = self.manager.create_backup(label="only_one")["backup_id"]
        report = self.manager.prune_backups(retention_count=0, min_valid_to_keep=1)
        self.assertEqual(report["pruned_count"], 0)
        self.assertEqual(report["retained_count"], 1)
        self.assertIn(single, report["retained_backups"])


class TestSyntheticRestoreProcedure(unittest.TestCase):
    """Executes full synthetic restore test in isolated sandbox (Section 7)."""

    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.prod_data = os.path.join(self.temp_root, "data")
        self.backups_dir = os.path.join(self.temp_root, "backups")
        os.makedirs(self.prod_data, exist_ok=True)
        self.manager = BackupManager(data_dir=self.prod_data, backups_dir=self.backups_dir)

        # 1. Populate full representative production state
        self.leads = [
            {
                "lead_id": "LEAD-MAN-100001",
                "company_name": "Albert Square Bistro",
                "postcode": "M2 5DB",
                "phone": "+441610000001",
                "lead_status": "SENT",
                "outreach_status": "SENT",
                "qualification_state": "QUALIFIED",
                "review_count": 85,
                "rating": 4.6,
            },
            {
                "lead_id": "LEAD-MAN-100002",
                "company_name": "Spinningfields Brasserie",
                "postcode": "M3 3BE",
                "phone": "+441610000002",
                "lead_status": "BOUNCED",
                "outreach_status": "BOUNCED",
                "qualification_state": "MANUAL_REVIEW",
                "review_count": 62,
                "rating": 4.2,
            },
            {
                "lead_id": "LEAD-MAN-100003",
                "company_name": "Deansgate Tavern",
                "postcode": "M3 4LQ",
                "phone": "+441610000003",
                "lead_status": "SUPPRESSED",
                "outreach_status": "SUPPRESSED",
                "qualification_state": "SUPPRESSED",
                "review_count": 120,
                "rating": 4.7,
            },
            {
                "lead_id": "LEAD-MAN-100004",
                "company_name": "Northern Quarter Diner",
                "postcode": "M4 1HQ",
                "phone": "+441610000004",
                "lead_status": "NOT_CONTACTED",
                "outreach_status": "READY_FOR_REVIEW",
                "qualification_state": "OUTREACH_READY",
                "review_count": 90,
                "rating": 4.5,
            },
        ]

        self.messages = [
            {
                "message_id": "MSG-001",
                "lead_id": "LEAD-MAN-100001",
                "channel": "Email",
                "status": "SENT",
                "sent_at": "2026-10-01T10:00:00Z",
            }
        ]

        self.suppressions = [
            {"lead_id": "LEAD-MAN-100003", "reason": "Opt-out", "created_at": "2026-10-01T12:00:00Z"}
        ]

        self.outcomes = [
            {"lead_id": "LEAD-MAN-100001", "outcome": "SENT", "channel": "Email"}
        ]

        with open(os.path.join(self.prod_data, "cache_sheets_leads.json"), "w") as f:
            json.dump({"leads": self.leads}, f, indent=2)
        with open(os.path.join(self.prod_data, "message_history.json"), "w") as f:
            json.dump(self.messages, f, indent=2)
        with open(os.path.join(self.prod_data, "suppression_list.json"), "w") as f:
            json.dump(self.suppressions, f, indent=2)
        with open(os.path.join(self.prod_data, "outreach_outcomes.json"), "w") as f:
            json.dump(self.outcomes, f, indent=2)

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_full_synthetic_restore_cycle(self):
        """9. Full 10-step disaster recovery cycle: backup -> corruption -> restore -> verification."""
        # Step 2: Create a valid backup
        manifest = self.manager.create_backup(label="healthy_baseline")
        backup_id = manifest["backup_id"]
        original_checksum = manifest["manifest_checksum"]

        # Step 3: Mutate / corrupt / delete working state
        leads_path = os.path.join(self.prod_data, "cache_sheets_leads.json")
        with open(leads_path, "w") as f:
            f.write("corrupted content!!!")  # corrupt
        os.remove(os.path.join(self.prod_data, "suppression_list.json"))  # delete

        engine = StateIntegrityEngine(data_dir=self.prod_data)
        audit_pre_restore = engine.audit_data_integrity()
        self.assertEqual(audit_pre_restore["status"], "CORRUPT")
        self.assertIn("cache_sheets_leads.json", audit_pre_restore["corrupt_files"])

        # Step 4: Restore from backup
        restore_result = self.manager.restore_backup(backup_id=backup_id, target_dir=self.prod_data)
        self.assertEqual(restore_result["status"], "RESTORED")

        # Step 5: Re-run integrity checks & identity audit
        audit_post_restore = engine.audit_data_integrity()
        self.assertEqual(audit_post_restore["status"], "PASS")
        self.assertTrue(audit_post_restore["is_healthy"])

        # Step 6: Verify original state returns exactly
        restored_leads = engine.safe_load_store("cache_sheets_leads.json")["leads"]
        self.assertEqual(len(restored_leads), len(self.leads))

        # Step 7: Verify protected states remain intact
        ok, violations = StateIntegrityEngine.verify_protected_outreach_safety(
            baseline_leads=self.leads,
            evaluated_leads=restored_leads,
            message_history=self.messages,
            suppression_list=self.suppressions,
        )
        self.assertTrue(ok, f"Protected state violations: {violations}")

        # Step 8: Verify no duplicate lead creation
        lead_ids = [l["lead_id"] for l in restored_leads]
        self.assertEqual(len(lead_ids), len(set(lead_ids)))

        # Step 9: Verify zero accidental outreach promotion
        for l in restored_leads:
            if l["lead_status"] in ("SENT", "BOUNCED", "SUPPRESSED"):
                self.assertNotEqual(l.get("qualification_state"), "OUTREACH_READY")

        # Step 10: Verify checksums match original
        restored_hash = calculate_sha256(leads_path)
        expected_hash = manifest["files"]["cache_sheets_leads.json"]["checksum_sha256"]
        self.assertEqual(restored_hash, expected_hash)


class TestStateCorruptionDetection(unittest.TestCase):
    """Verifies that corrupted, truncated, or incomplete stores are detected and rejected."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.engine = StateIntegrityEngine(data_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_truncated_json_detected(self):
        """10. Truncated JSON is detected as CORRUPT and safe_load raises StateCorruptionError."""
        path = os.path.join(self.temp_dir, "cache_sheets_leads.json")
        with open(path, "w") as f:
            f.write('{"leads": [{"lead_id": "LEAD-MAN-999999"')  # truncated
        audit = self.engine.audit_store_file("cache_sheets_leads.json")
        self.assertTrue(audit["is_corrupt"])
        self.assertEqual(audit["status"], "CORRUPT")
        with self.assertRaises(StateCorruptionError):
            self.engine.safe_load_store("cache_sheets_leads.json")

    def test_zero_byte_file_detected(self):
        """11. Zero-byte files are detected as CORRUPT."""
        path = os.path.join(self.temp_dir, "message_history.json")
        open(path, "w").close()
        audit = self.engine.audit_store_file("message_history.json")
        self.assertTrue(audit["is_corrupt"])
        self.assertIn("truncated", audit["error"])

    def test_missing_file_handled_safely(self):
        """12. Missing store file returns status MISSING and is_corrupt=False."""
        audit = self.engine.audit_store_file("nonexistent.json")
        self.assertEqual(audit["status"], "MISSING")
        self.assertFalse(audit["is_corrupt"])


class TestGoogleSheetsRecoveryAndReconciliation(unittest.TestCase):
    """Verifies reconciliation between Google Sheets CRM authority and local cache."""

    def test_reconciliation_detects_all_discrepancies(self):
        """13. Reconciliation detects missing, unexpected, status mismatches, and protected violations."""
        sheets = [
            {"lead_id": "LEAD-MAN-111111", "company_name": "A", "lead_status": "SENT"},
            {"lead_id": "LEAD-MAN-222222", "company_name": "B", "lead_status": "BOUNCED"},
            {"lead_id": "LEAD-MAN-333333", "company_name": "C", "lead_status": "NOT_CONTACTED"},
            {"lead_id": "INVALID-ID", "company_name": "D", "lead_status": "NOT_CONTACTED"},
        ]
        local = [
            {"lead_id": "LEAD-MAN-111111", "company_name": "A", "lead_status": "READY_FOR_REVIEW"},  # Mismatch
            {"lead_id": "LEAD-MAN-333333", "company_name": "C", "lead_status": "NOT_CONTACTED"},
            {"lead_id": "LEAD-MAN-555555", "company_name": "Ghost", "lead_status": "NEW"},  # Unexpected local
            # LEAD-MAN-222222 is missing locally
        ]
        res = StateIntegrityEngine.reconcile_sheets_and_local_cache(sheets, local)
        self.assertEqual(res["status"], "MISMATCH")
        self.assertEqual(len(res["missing_local_leads"]), 2)
        missing_ids = [m["lead_id"] for m in res["missing_local_leads"]]
        self.assertIn("LEAD-MAN-222222", missing_ids)
        self.assertIn("INVALID-ID", missing_ids)
        self.assertEqual(len(res["unexpected_local_leads"]), 1)
        self.assertEqual(res["unexpected_local_leads"][0]["lead_id"], "LEAD-MAN-555555")
        self.assertEqual(len(res["status_mismatches"]), 1)
        self.assertEqual(res["status_mismatches"][0]["lead_id"], "LEAD-MAN-111111")
        self.assertTrue(any(m["lead_id"] == "INVALID-ID" for m in res["canonical_id_mismatches"]))


class TestCanonicalIDAndIdentityIntegrity(unittest.TestCase):
    """Verifies canonical lead ID stability and identity audit safeguards."""

    def test_canonical_id_regex_validation(self):
        """14. Validates canonical lead ID regex format."""
        self.assertTrue(CANONICAL_LEAD_ID_REGEX.match("LEAD-MAN-3B9091"))
        self.assertTrue(CANONICAL_LEAD_ID_REGEX.match("LEAD-MANCHESTER-123456"))
        self.assertFalse(CANONICAL_LEAD_ID_REGEX.match("RES-MAN-123456"))
        self.assertFalse(CANONICAL_LEAD_ID_REGEX.match("LEAD-SHORT-123"))

    def test_canonical_id_stability_across_mutations(self):
        """15. Entity attribute mutations do not alter canonical lead ID."""
        orig = [{"lead_id": "LEAD-MAN-000001", "company_name": "The Old Monkey", "phone": "0161111111"}]
        updated_same_id = [{"lead_id": "LEAD-MAN-000001", "company_name": "The Old Monkey", "phone": "0161222222"}]
        ok, violations = StateIntegrityEngine.verify_canonical_id_stability(orig, updated_same_id)
        self.assertTrue(ok)

        # Accidental change of ID
        updated_bad_id = [{"lead_id": "LEAD-MAN-999999", "company_name": "The Old Monkey", "phone": "0161222222"}]
        ok2, violations2 = StateIntegrityEngine.verify_canonical_id_stability(orig, updated_bad_id)
        self.assertFalse(ok2)
        self.assertTrue(any("canonical lead_id changed" in v for v in violations2))


class TestAtomicWriteAndFileLockRecovery(unittest.TestCase):
    """Verifies atomic write integrity and file lock release."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_atomic_write_creates_valid_file_without_residual_tmp(self):
        """16. atomic_write_json writes complete file and cleans up temporary .tmp."""
        target = os.path.join(self.temp_dir, "test_atomic.json")
        payload = {"success": True, "count": 42}
        atomic_write_json(target, payload)

        self.assertTrue(os.path.exists(target))
        with open(target, "r") as f:
            data = json.load(f)
        self.assertEqual(data["count"], 42)

        # Ensure no dangling tmp files in directory
        tmp_files = [f for f in os.listdir(self.temp_dir) if f.endswith(".tmp")]
        self.assertEqual(tmp_files, [])

    def test_file_lock_acquires_and_releases(self):
        """17. FileLock acquires and reliably releases without deadlock."""
        lock_path = os.path.join(self.temp_dir, "test.lock")
        lock = FileLock(lock_path)
        with lock:
            self.assertTrue(lock.is_locked)
        self.assertFalse(lock.is_locked)


class TestGitHubActionsLeastPrivilege(unittest.TestCase):
    """Verifies all GitHub Actions workflows enforce least privilege and safety invariants."""

    def test_all_workflows_use_least_privilege_permissions(self):
        """18. All workflow YAMLs specify least privilege (contents: read) and enforce safety invariants."""
        workflows_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), ".github", "workflows")
        )
        workflow_files = [
            "01-tests.yml",
            "02-technical-pipeline.yml",
            "03-freshness.yml",
            "04-monitoring.yml",
        ]
        for wf in workflow_files:
            path = os.path.join(workflows_dir, wf)
            self.assertTrue(os.path.exists(path), f"Workflow file {wf} must exist")
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
                data = yaml.safe_load(content)

            # Check permissions
            perms = data.get("permissions")
            self.assertIsNotNone(perms, f"{wf} must define permissions")
            self.assertEqual(
                perms.get("contents"),
                "read",
                f"{wf} must have contents: read least-privilege permission",
            )

            # Check that scheduled cron is disabled
            on_block = data.get("on", {})
            self.assertNotIn(
                "schedule",
                on_block,
                f"{wf} must NOT have active scheduled cron enabled per Phase 10.5",
            )


class TestMonitoringIncidentIntegration(unittest.TestCase):
    """Verifies SecurityAndIntegrityMonitor properly generates and resolves Phase 10.3 incidents."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.incident_mgr = IncidentManager(data_dir=self.temp_dir)
        self.monitor = SecurityAndIntegrityMonitor(incident_mgr=self.incident_mgr)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_security_scan_failure_incident(self):
        """19. Security scan failure emits CRITICAL incident."""
        self.monitor.record_security_scan_failure(2, "Detected 2 sensitive keys")
        incidents = self.incident_mgr.get_active_incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].incident_type, IncidentType.SECURITY_SCAN_FAILURE.value)
        self.assertEqual(incidents[0].severity, IncidentSeverity.CRITICAL.value)

    def test_backup_and_restore_failure_incidents(self):
        """20. Backup and restore failures emit corresponding incidents."""
        self.monitor.record_backup_failure("Disk full", context="snapshot")
        self.monitor.record_restore_failure("Checksum mismatch", backup_id="backup_123")
        incidents = self.incident_mgr.get_active_incidents()
        types = [i.incident_type for i in incidents]
        self.assertIn(IncidentType.BACKUP_FAILURE.value, types)
        self.assertIn(IncidentType.RESTORE_FAILURE.value, types)

    def test_state_corruption_and_reconciliation_incidents(self):
        """21. State corruption and reconciliation failures emit corresponding incidents."""
        self.monitor.record_state_corruption("cache_sheets_leads.json", "Truncated JSON")
        self.monitor.record_reconciliation_failure(3, "3 contradictory lead states")
        incidents = self.incident_mgr.get_active_incidents()
        types = [i.incident_type for i in incidents]
        self.assertIn(IncidentType.STATE_CORRUPTION.value, types)
        self.assertIn(IncidentType.RECONCILIATION_FAILURE.value, types)


if __name__ == "__main__":
    unittest.main()
