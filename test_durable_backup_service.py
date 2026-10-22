"""Tests for end-to-end durable backup orchestration without live GCP side effects."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from lib.system.storage_bootstrap import bootstrap_storage_baseline
from lib.system.durable_backup_service import create_and_verify_durable_backup


class TestDurableBackupService(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="dripp_backup_service_test_")
        self.data_dir = os.path.join(self.tmp_dir, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        bootstrap_storage_baseline(data_dir=self.data_dir, sync_from_sheets_if_available=False)

        # A stale cache must be replaced by the authoritative live Sheets read.
        with open(os.path.join(self.data_dir, "cache_sheets_leads.json"), "w", encoding="utf-8") as f:
            json.dump({"leads": [{"lead_id": "STALE-CACHE"}]}, f)

        self.sheets = MagicMock()
        self.sheets.fetch_all_leads.return_value = [
            {"lead_id": "LEAD-MAN-AAA111", "company_name": "Live Bistro"}
        ]
        self.sheets.fetch_review_queue.return_value = [
            {"lead_id": "LEAD-MAN-BBB222", "company_name": "Needs Review"}
        ]
        self.sheets.fetch_research_log.return_value = [
            {"research_id": "RES-MAN-001", "company_name": "Research Record"}
        ]

        self.firestore = MagicMock()
        self.firestore.is_live = True
        self.firestore.acquire_lease.return_value = True
        self.firestore.release_lease.return_value = True

        self.engine = MagicMock()
        self.engine.create_durable_backup_bundle.return_value = {
            "backup_id": "backup_test",
            "archive_path": "/tmp/fake-backup.tar.gz",
            "manifest_path": "/tmp/fake-backup.manifest.json",
            "archive_sha256": "abc123",
            "archive_size_bytes": 123,
            "files_count": 12,
            "manifest": {"tier2_summary": {"total_documents_captured": 4}},
        }
        self.engine.execute_isolated_restore_drill.return_value = {
            "status": "PASS",
            "all_checks_passed": True,
        }
        self.engine.upload_durable_backup_to_gcs.return_value = {
            "status": "SUCCESS",
            "archive_uri": "gs://test-bucket/backups/backup_test.tar.gz",
            "manifest_uri": "gs://test-bucket/backups/backup_test.manifest.json",
            "archive_sha256": "abc123",
            "archive_size_bytes": 123,
        }
        self.engine.verify_durable_cloud_backup.return_value = {
            "is_valid": True,
            "status": "PASS",
            "isolated_restore_verified": True,
        }
        self.engine_class = MagicMock(return_value=self.engine)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_live_sheets_snapshot_local_restore_upload_and_remote_restore_all_pass(self):
        captured = {}

        def create_bundle(**kwargs):
            staged_data_dir = self.engine_class.call_args.kwargs["data_dir"]
            with open(os.path.join(staged_data_dir, "cache_sheets_leads.json"), "r", encoding="utf-8") as f:
                captured["leads_cache"] = json.load(f)
            with open(os.path.join(staged_data_dir, "cache_sheets_review_queue.json"), "r", encoding="utf-8") as f:
                captured["review_cache"] = json.load(f)
            return self.engine.create_durable_backup_bundle.return_value

        self.engine.create_durable_backup_bundle.side_effect = create_bundle

        result = create_and_verify_durable_backup(
            "test-bucket",
            data_dir=self.data_dir,
            run_id="TEST-RUN-001",
            sheets_provider=self.sheets,
            firestore_store=self.firestore,
            engine_class=self.engine_class,
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["crm_record_counts"], {"leads": 1, "review_queue": 1, "research_log": 1})
        self.assertEqual(result["firestore_documents"], 4)
        self.assertEqual(captured["leads_cache"]["source"], "LIVE_GOOGLE_SHEETS")
        self.assertEqual(captured["leads_cache"]["leads"][0]["lead_id"], "LEAD-MAN-AAA111")
        self.assertNotEqual(captured["leads_cache"]["leads"][0]["lead_id"], "STALE-CACHE")
        self.assertEqual(captured["review_cache"]["review_queue"][0]["lead_id"], "LEAD-MAN-BBB222")
        self.engine.execute_isolated_restore_drill.assert_called_once()
        self.engine.upload_durable_backup_to_gcs.assert_called_once()
        self.engine.verify_durable_cloud_backup.assert_called_once_with(bucket_name="test-bucket")
        self.firestore.acquire_lease.assert_called_once()
        self.firestore.release_lease.assert_called_once()

    def test_refuses_mock_firestore_before_creating_or_uploading_backup(self):
        self.firestore.is_live = False
        with self.assertRaisesRegex(RuntimeError, "live Firestore"):
            create_and_verify_durable_backup(
                "test-bucket",
                data_dir=self.data_dir,
                sheets_provider=self.sheets,
                firestore_store=self.firestore,
                engine_class=self.engine_class,
            )
        self.engine_class.assert_not_called()
        self.firestore.acquire_lease.assert_not_called()

    def test_local_restore_failure_prevents_upload(self):
        self.engine.execute_isolated_restore_drill.return_value = {
            "status": "FAIL",
            "all_checks_passed": False,
        }
        with self.assertRaisesRegex(RuntimeError, "was not uploaded"):
            create_and_verify_durable_backup(
                "test-bucket",
                data_dir=self.data_dir,
                sheets_provider=self.sheets,
                firestore_store=self.firestore,
                engine_class=self.engine_class,
            )
        self.engine.upload_durable_backup_to_gcs.assert_not_called()
        self.firestore.release_lease.assert_called_once()

    def test_remote_restore_failure_is_not_reported_as_pass(self):
        self.engine.verify_durable_cloud_backup.return_value = {
            "is_valid": False,
            "status": "FAIL",
            "isolated_restore_verified": False,
            "error": "Checksum mismatch",
        }
        with self.assertRaisesRegex(RuntimeError, "Remote GCS backup verification"):
            create_and_verify_durable_backup(
                "test-bucket",
                data_dir=self.data_dir,
                sheets_provider=self.sheets,
                firestore_store=self.firestore,
                engine_class=self.engine_class,
            )
        self.engine.upload_durable_backup_to_gcs.assert_called_once()
        self.firestore.release_lease.assert_called_once()

    def test_empty_bucket_is_rejected_before_reading_external_state(self):
        with self.assertRaisesRegex(ValueError, "bucket name"):
            create_and_verify_durable_backup(
                "",
                data_dir=self.data_dir,
                sheets_provider=self.sheets,
                firestore_store=self.firestore,
                engine_class=self.engine_class,
            )
        self.sheets.fetch_all_leads.assert_not_called()
        self.engine_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
