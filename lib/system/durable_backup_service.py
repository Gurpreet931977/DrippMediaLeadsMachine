"""
Verified multi-tier backup orchestration for the production API runtime.

This path exports CRM data directly from authoritative Google Sheets tabs,
requires live Firestore, and uploads only after an isolated local restore drill
succeeds. It then verifies the uploaded remote artifact and restore.
"""

import os
import shutil
import tempfile
import uuid
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from lib.system.backup_manager import CRITICAL_FILES
from lib.system.atomic_writer import atomic_write_json
from lib.system.durable_backup_engine import DurableBackupEngine

logger = logging.getLogger("DurableBackupService")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_records(value: Any, name: str) -> list:
    if not isinstance(value, list):
        raise TypeError(f"Live Google Sheets export '{name}' did not return a list.")
    if any(not isinstance(row, dict) for row in value):
        raise TypeError(f"Live Google Sheets export '{name}' contains a non-record row.")
    return value


def create_and_verify_durable_backup(
    bucket_name: str,
    data_dir: Optional[str] = None,
    run_id: Optional[str] = None,
    *,
    sheets_provider: Optional[Any] = None,
    firestore_store: Optional[Any] = None,
    engine_class: Optional[Any] = None,
    storage_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Creates, locally restores, uploads, and remotely verifies a durable backup.

    No CRM writes are performed. Google Sheets is read directly, Firestore is
    captured from the live named database, and the archive is built from an
    isolated staging copy of operational stores. Any incomplete capture or
    failed local/remote restore check raises and must not be reported as success.
    """
    bucket = (bucket_name or "").strip()
    if not bucket:
        raise ValueError("GCS backup bucket name must be configured.")
    source_data_dir = data_dir or DEFAULT_DATA_DIR
    backup_run_id = run_id or (
        f"DURABLE-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    )

    if sheets_provider is None:
        from lib.sheets.google_sheets import GoogleSheetsStorageProvider
        sheets_provider = GoogleSheetsStorageProvider()

    # Fresh, read-only authoritative exports. Never fall back to stale local caches.
    leads = _require_records(sheets_provider.fetch_all_leads(), "LEADS")
    review_queue = _require_records(sheets_provider.fetch_review_queue(), "REVIEW_QUEUE")
    research_log = _require_records(sheets_provider.fetch_research_log(), "RESEARCH_LOG")

    if firestore_store is None:
        from lib.system.firestore_state_store import FirestoreStateStore
        firestore_store = FirestoreStateStore(database_id="drippmedialeads", force_mock=False)
    if not bool(getattr(firestore_store, "is_live", False)):
        raise RuntimeError(
            "Durable backup requires a live Firestore connection; mock/standalone mode is not accepted."
        )

    if engine_class is None:
        engine_class = DurableBackupEngine

    holder_id = backup_run_id
    lease_name = "durable_backup_runner"
    try:
        lease_acquired = bool(
            firestore_store.acquire_lease(
                lease_name,
                holder_id,
                ttl_seconds=1800.0,
                metadata={"operation": "durable_backup", "bucket": bucket},
            )
        )
    except Exception as exc:
        raise RuntimeError(f"Could not acquire the distributed durable-backup lease: {exc}") from exc
    if not lease_acquired:
        raise RuntimeError("Another durable backup is already running; refusing a concurrent GCS upload.")

    try:
        with tempfile.TemporaryDirectory(prefix="dripp_durable_backup_") as scratch:
            staged_data_dir = os.path.join(scratch, "data")
            artifact_dir = os.path.join(scratch, "artifacts")
            restore_dir = os.path.join(scratch, "isolated_restore")
            os.makedirs(staged_data_dir, exist_ok=True)
            os.makedirs(artifact_dir, exist_ok=True)
            os.makedirs(restore_dir, exist_ok=True)

            # Bring over current operational stores without modifying the live
            # container cache. Replace CRM cache files with fresh Sheets reads.
            copied_files = 0
            for relative_name in CRITICAL_FILES:
                source = os.path.join(source_data_dir, relative_name)
                destination = os.path.join(staged_data_dir, relative_name)
                if os.path.isfile(source):
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    shutil.copy2(source, destination)
                    copied_files += 1

            captured_at = _now_utc()
            live_sheet_exports = [
                ("cache_sheets_leads.json", "leads", leads),
                ("cache_sheets_review_queue.json", "review_queue", review_queue),
                ("cache_sheets_research_log.json", "entries", research_log),
            ]
            for filename, collection_key, records in live_sheet_exports:
                atomic_write_json(
                    os.path.join(staged_data_dir, filename),
                    {
                        collection_key: records,
                        "count": len(records),
                        "cached": False,
                        "source": "LIVE_GOOGLE_SHEETS",
                        "captured_at": captured_at,
                    },
                )

            engine = engine_class(data_dir=staged_data_dir, firestore_store=firestore_store)
            bundle = engine.create_durable_backup_bundle(
                run_id=backup_run_id,
                label="production_durable",
                output_dir=artifact_dir,
            )

            local_drill = engine.execute_isolated_restore_drill(
                archive_path=bundle["archive_path"],
                manifest_path=bundle["manifest_path"],
                isolated_dest_dir=restore_dir,
            )
            if local_drill.get("status") != "PASS" or not local_drill.get("all_checks_passed", False):
                raise RuntimeError("Local isolated restore drill failed; durable backup was not uploaded.")

            upload = engine.upload_durable_backup_to_gcs(
                bucket_name=bucket,
                archive_path=bundle["archive_path"],
                manifest_path=bundle["manifest_path"],
                storage_client=storage_client,
            )
            if upload.get("status") != "SUCCESS" or not upload.get("archive_uri") or not upload.get("manifest_uri"):
                raise RuntimeError("GCS upload did not confirm both archive and manifest objects.")

            remote = engine.verify_durable_cloud_backup(bucket_name=bucket)
            if not (
                remote.get("is_valid")
                and remote.get("status") == "PASS"
                and remote.get("isolated_restore_verified")
            ):
                raise RuntimeError(
                    "Remote GCS backup verification or isolated restore drill failed: "
                    + str(remote.get("error") or remote.get("remediation") or "no specific reason returned")
                )

            manifest = bundle.get("manifest") or {}
            tier2_summary = manifest.get("tier2_summary") or {}
            return {
                "status": "PASS",
                "backup_id": bundle.get("backup_id"),
                "bucket": bucket,
                "archive_uri": upload.get("archive_uri"),
                "manifest_uri": upload.get("manifest_uri"),
                "archive_sha256": upload.get("archive_sha256") or bundle.get("archive_sha256"),
                "archive_size_bytes": upload.get("archive_size_bytes") or bundle.get("archive_size_bytes"),
                "files_count": bundle.get("files_count", 0),
                "local_operational_files_copied": copied_files,
                "firestore_documents": tier2_summary.get("total_documents_captured", 0),
                "crm_record_counts": {
                    "leads": len(leads),
                    "review_queue": len(review_queue),
                    "research_log": len(research_log),
                },
                "local_restore_status": local_drill.get("status"),
                "remote_restore_status": remote.get("status"),
                "isolated_restore_verified": True,
                "completed_at": _now_utc(),
            }
    finally:
        try:
            firestore_store.release_lease(lease_name, holder_id)
        except Exception:
            logger.exception("Failed to release distributed durable-backup lease; it will expire by TTL.")
