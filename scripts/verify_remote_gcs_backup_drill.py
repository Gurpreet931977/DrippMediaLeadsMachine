"""
scripts/verify_remote_gcs_backup_drill.py
=========================================
Downloads the latest durable backup artifact directly from Google Cloud Storage
(gs://dripp-leads-backups-poised-eye-509816-a4/backups/), cryptographically verifies
its remote SHA-256 against its remote manifest, and executes a zero-mutation isolated
restoration drill in a sandboxed temporary directory.
"""

import os
import sys
import json
import logging
import tempfile

# Ensure project root is on PYTHONPATH
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.system.durable_backup_engine import DurableBackupEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("RemoteBackupDrill")

BUCKET_NAME = os.getenv("GCS_BACKUP_BUCKET", "dripp-leads-backups-poised-eye-509816-a4")


def main() -> int:
    logger.info("Executing remote durable backup verification drill on gs://%s...", BUCKET_NAME)

    res = DurableBackupEngine.verify_durable_cloud_backup(bucket_name=BUCKET_NAME)
    print("\n" + "=" * 70)
    print("  GCS REMOTE DURABLE BACKUP VERIFICATION REPORT")
    print("=" * 70)
    print(json.dumps(res, indent=2))
    print("=" * 70 + "\n")

    if res.get("status") == "PASS" and res.get("isolated_restore_verified"):
        logger.info("✓ BACKUP_HEALTH VALIDATED PASS: Remote artifacts verified and isolated drill passed.")
        return 0
    else:
        logger.error("✗ BACKUP_HEALTH FAIL: %s", res.get("error", "Unknown error"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
