"""
scripts/setup_operational_sheets_schema.py
==========================================
Migration Tooling for Phase 12.2.3 Operational Ledger Worksheet.

Safely initializes the dedicated 'OPERATIONAL_JOBS' worksheet in the designated
Google Sheets CRM workbook without modifying existing CRM records.

Rules:
1. DEFAULTS TO DRY-RUN: Runs safely in inspection mode without modifying anything.
2. EXPLICIT APPROVAL REQUIRED: Only creates the worksheet if '--execute' is passed.
3. PRESERVES CRM TABS: Strictly does NOT touch or alter 'LEADS', 'REVIEW_QUEUE', or 'RESEARCH_LOG'.
"""

import os
import sys
import argparse
import logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv()

from lib.sheets.google_sheets import GoogleSheetsStorageProvider
from lib.sheets.operational_ledger import (
    OperationalJobLedger,
    OPERATIONAL_JOBS_TAB,
    OPERATIONAL_JOBS_COLUMNS,
    LEDGER_SCHEMA_VERSION,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SchemaMigration")


def parse_args():
    parser = argparse.ArgumentParser(description="Operational Jobs Worksheet Schema Migration Tool")
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Explicitly execute schema migration against the Google Sheets workbook. Default is DRY-RUN.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    dry_run = not args.execute

    print("================================================================================")
    print("  DRIPP MEDIA — OPERATIONAL WORKSHEET SCHEMA MIGRATION")
    print("================================================================================")
    print(f"  • Target Worksheet:      {OPERATIONAL_JOBS_TAB}")
    print(f"  • Schema Version:        {LEDGER_SCHEMA_VERSION}")
    print(f"  • Execution Mode:        {'DRY-RUN (Safe Inspection)' if dry_run else 'LIVE EXECUTION'}")
    print(f"  • Column Count:          {len(OPERATIONAL_JOBS_COLUMNS)}")
    print("================================================================================")
    print("Required Schema Columns:")
    for idx, col in enumerate(OPERATIONAL_JOBS_COLUMNS, start=1):
        print(f"  {idx:2d}. {col}")
    print("================================================================================")

    # Initialize storage provider
    try:
        storage = GoogleSheetsStorageProvider()
        ledger = OperationalJobLedger(storage_provider=storage)
    except Exception as e:
        logger.error("Could not connect to Google Sheets CRM provider: %s", e)
        print("\n[NOTICE] Unable to inspect live workbook without valid Google credentials.")
        return 1

    result = ledger.ensure_operational_worksheet(dry_run=dry_run)
    print(f"\nMigration Status: {result.get('status')}")
    print(f"Action Taken:     {result.get('action')}")
    if dry_run and result.get("status") == "MISSING":
        print("\n[ACTION REQUIRED] Dry-run complete. To apply changes to the Google Sheets workbook,")
        print("run this script with operator approval: python scripts/setup_operational_sheets_schema.py --execute")
    elif result.get("status") == "EXISTS":
        print(f"\n[OK] Worksheet '{OPERATIONAL_JOBS_TAB}' is already present and correctly configured.")
    elif not dry_run and result.get("status") == "CREATED":
        print(f"\n[SUCCESS] Worksheet '{OPERATIONAL_JOBS_TAB}' successfully initialized with {len(OPERATIONAL_JOBS_COLUMNS)} columns.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
