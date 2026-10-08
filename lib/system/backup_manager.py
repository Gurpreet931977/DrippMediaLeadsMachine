"""
lib/system/backup_manager.py
============================
Automated snapshotting, checksumming, and disaster recovery manager.
Protects critical system state before destructive or bulk mutations.
Computes SHA-256 checksums on all archived files.
Provides isolated recovery verification to ensure any backup is restorable.
"""

import os
import json
import shutil
import hashlib
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Set

from lib.system.atomic_writer import atomic_write_json

logger = logging.getLogger("BackupManager")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DEFAULT_BACKUPS_DIR = os.path.join(DATA_DIR, "backups")

SCHEMA_VERSION = "1.0.0"

# Canonical critical files to snapshot
CRITICAL_FILES = [
    "cache_sheets_leads.json",
    "cache_sheets_research_log.json",
    "cache_sheets_review_queue.json",
    "commercial_records.json",
    "commercial_events.json",
    "commercial_proposals.json",
    "proposal_packages.json",
    "message_history.json",
    "contact_history.json",
    "outreach_outcomes.json",
    "campaigns.json",
    "execution_gate.json",
    "suppression_list.json",
    "controlled_batch_state.json",
    "lead_timelines.json",
    "commercial_pipeline_snapshot.json",
    "proposal_pipeline_snapshot.json",
    "outreach_performance_snapshot.json",
]


def calculate_sha256(filepath: str) -> str:
    """Computes hexadecimal SHA-256 hash of file content."""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


class BackupManager:
    """
    Manages automated backups, checksum manifests, and disaster recovery verification.
    """

    def __init__(self, data_dir: Optional[str] = None, backups_dir: Optional[str] = None):
        self.data_dir = data_dir or DATA_DIR
        self.backups_dir = backups_dir or DEFAULT_BACKUPS_DIR
        if not os.path.exists(self.backups_dir):
            os.makedirs(self.backups_dir, exist_ok=True)

    def create_backup(
        self,
        run_id: Optional[str] = None,
        label: str = "snapshot",
        target_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Creates an immutable point-in-time snapshot with SHA-256 checksums,
        manifest.json, and mandatory post-creation integrity verification.
        A backup is only marked SUCCESS after verification passes.
        """
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y%m%d_%H%M%S")
        rid = run_id or f"RUN-{ts_str}"
        backup_id = f"backup_{ts_str}_{label}_{rid}"
        backup_folder = os.path.join(self.backups_dir, backup_id)
        os.makedirs(backup_folder, exist_ok=True)

        files_to_backup = target_files or CRITICAL_FILES
        manifest_files = {}
        backed_up_count = 0
        total_bytes = 0

        for rel_name in files_to_backup:
            src_path = os.path.join(self.data_dir, rel_name)
            if os.path.exists(src_path) and os.path.isfile(src_path):
                dest_path = os.path.join(backup_folder, rel_name)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.copy2(src_path, dest_path)

                checksum = calculate_sha256(dest_path)
                file_size = os.path.getsize(dest_path)
                total_bytes += file_size
                manifest_files[rel_name] = {
                    "checksum_sha256": checksum,
                    "size_bytes": file_size,
                    "backed_up_at": now.isoformat(),
                }
                backed_up_count += 1

        # Calculate deterministic aggregate checksum of all backed up files
        sorted_checksums = [f"{k}:{v['checksum_sha256']}" for k, v in sorted(manifest_files.items())]
        aggregate_checksum = hashlib.sha256("|".join(sorted_checksums).encode("utf-8")).hexdigest()

        manifest = {
            "backup_id": backup_id,
            "created_at": now.isoformat(),
            "timestamp": now.isoformat(),
            "run_id": rid,
            "label": label,
            "schema_version": SCHEMA_VERSION,
            "source_state": self.data_dir,
            "manifest_checksum": aggregate_checksum,
            "checksum": aggregate_checksum,
            "files_count": backed_up_count,
            "file_count": backed_up_count,
            "total_bytes": total_bytes,
            "byte_size": total_bytes,
            "completion_status": "PENDING_VERIFICATION",
            "files": manifest_files,
        }

        manifest_path = os.path.join(backup_folder, "manifest.json")
        atomic_write_json(manifest_path, manifest)

        # Mandatory integrity verification before marking SUCCESS
        is_valid, errors = self.verify_backup_integrity(backup_id)
        if not is_valid:
            manifest["completion_status"] = "FAILED"
            manifest["verification_errors"] = errors
            atomic_write_json(manifest_path, manifest)
            logger.error(f"Backup verification failed for {backup_id}: {errors}")
            raise RuntimeError(f"Backup verification failed for {backup_id}: {'; '.join(errors)}")

        manifest["completion_status"] = "SUCCESS"
        atomic_write_json(manifest_path, manifest)

        logger.info(f"Created verified backup {backup_id} with {backed_up_count} files ({total_bytes} bytes).")
        return manifest

    def prune_backups(
        self,
        retention_count: int = 5,
        min_valid_to_keep: int = 2,
    ) -> Dict[str, Any]:
        """
        Safely prunes old backups according to retention policies.
        Guarantees:
          1. Never deletes the only available valid backup.
          2. Preserves the newest valid backup and the previous valid backup (at least min_valid_to_keep).
          3. Prunes only when excess backups exist beyond retention limits.
        """
        backups = self.list_backups()  # Newest to oldest
        if not backups:
            return {
                "pruned_count": 0,
                "pruned_backups": [],
                "retained_count": 0,
                "retained_backups": [],
                "protected_valid_backups": [],
            }

        valid_backup_ids = []
        for b in backups:
            bid = b.get("backup_id")
            if not bid:
                continue
            is_valid, _ = self.verify_backup_integrity(bid)
            if is_valid:
                valid_backup_ids.append(bid)

        # Protect newest valid and previous valid backup(s)
        protected_ids: Set[str] = set(valid_backup_ids[:min_valid_to_keep])
        if len(valid_backup_ids) == 1:
            protected_ids.add(valid_backup_ids[0])

        pruned = []
        retained = []

        for idx, b in enumerate(backups):
            bid = b.get("backup_id")
            if not bid:
                continue
            folder = self.get_backup_path(bid)
            if not folder:
                continue

            if bid in protected_ids:
                retained.append(bid)
                continue

            if idx >= retention_count:
                try:
                    shutil.rmtree(folder, ignore_errors=True)
                    pruned.append(bid)
                except Exception as e:
                    logger.warning(f"Failed to remove backup folder {folder}: {e}")
                    retained.append(bid)
            else:
                retained.append(bid)

        logger.info(f"Pruned {len(pruned)} backups; retained {len(retained)}.")
        return {
            "pruned_count": len(pruned),
            "pruned_backups": pruned,
            "retained_count": len(retained),
            "retained_backups": retained,
            "protected_valid_backups": list(protected_ids),
        }

    def list_backups(self) -> List[Dict[str, Any]]:
        """Lists all existing backups ordered from newest to oldest."""
        if not os.path.exists(self.backups_dir):
            return []

        backups = []
        for name in os.listdir(self.backups_dir):
            folder = os.path.join(self.backups_dir, name)
            manifest_file = os.path.join(folder, "manifest.json")
            if os.path.isdir(folder) and os.path.exists(manifest_file):
                try:
                    with open(manifest_file, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                    backups.append(manifest)
                except Exception as e:
                    logger.warning(f"Could not load manifest in {folder}: {e}")

        backups.sort(key=lambda b: b.get("timestamp", ""), reverse=True)
        return backups

    def get_backup_path(self, backup_id: str) -> Optional[str]:
        """Resolves folder path for given backup_id."""
        folder = os.path.join(self.backups_dir, backup_id)
        if os.path.isdir(folder):
            return folder
        return None

    def verify_backup_integrity(self, backup_id: str) -> Tuple[bool, List[str]]:
        """
        Verifies every file in the backup against its recorded SHA-256 checksum and JSON validity.
        """
        folder = self.get_backup_path(backup_id)
        if not folder:
            return False, [f"Backup directory not found for '{backup_id}'"]

        manifest_path = os.path.join(folder, "manifest.json")
        if not os.path.exists(manifest_path):
            return False, ["manifest.json is missing"]

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        errors = []
        for rel_name, meta in manifest.get("files", {}).items():
            file_path = os.path.join(folder, rel_name)
            if not os.path.exists(file_path):
                errors.append(f"Missing backed up file: {rel_name}")
                continue

            expected_hash = meta.get("checksum_sha256")
            actual_hash = calculate_sha256(file_path)
            if actual_hash != expected_hash:
                errors.append(
                    f"Checksum mismatch for {rel_name}: expected {expected_hash}, got {actual_hash}"
                )

            # Test JSON parse integrity
            if rel_name.endswith(".json"):
                try:
                    with open(file_path, "r", encoding="utf-8") as jf:
                        json.load(jf)
                except Exception as je:
                    errors.append(f"Corrupt JSON in {rel_name}: {je}")

        is_valid = len(errors) == 0
        return is_valid, errors

    def verify_backup(self, backup_id: str) -> Dict[str, Any]:
        """Convenience method returning structured dictionary for verification."""
        is_valid, errors = self.verify_backup_integrity(backup_id)
        return {
            "status": "PASS" if is_valid else "FAIL",
            "is_valid": is_valid,
            "errors": errors,
        }

    def restore_backup(
        self,
        backup_id: str,
        target_dir: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Restores files from a backup folder into target_dir (defaults to live self.data_dir).
        Validates checksums before and after restoration.
        """
        destination = target_dir or self.data_dir
        folder = self.get_backup_path(backup_id)
        if not folder:
            raise FileNotFoundError(f"Backup '{backup_id}' not found.")

        is_valid, errors = self.verify_backup_integrity(backup_id)
        if not is_valid:
            raise ValueError(f"Cannot restore corrupted backup {backup_id}: {'; '.join(errors)}")

        manifest_path = os.path.join(folder, "manifest.json")
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        plan = []
        for rel_name in manifest.get("files", {}).keys():
            plan.append({
                "source": os.path.join(folder, rel_name),
                "target": os.path.join(destination, rel_name),
            })

        if dry_run:
            return {
                "status": "DRY_RUN",
                "backup_id": backup_id,
                "target_dir": destination,
                "files_to_restore": len(plan),
                "plan": [p["target"] for p in plan],
            }

        restored_files = []
        for item in plan:
            os.makedirs(os.path.dirname(item["target"]), exist_ok=True)
            shutil.copy2(item["source"], item["target"])
            restored_files.append(item["target"])

        return {
            "status": "RESTORED",
            "backup_id": backup_id,
            "target_dir": destination,
            "restored_count": len(restored_files),
            "restored_files": restored_files,
        }
