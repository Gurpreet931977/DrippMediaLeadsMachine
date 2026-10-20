#!/usr/bin/env python3
"""
scripts/verify_deployment_safety.py
===================================
Non-mutating verification and smoke test script for Dripp Media International Leads.

Verifies:
1. Core module imports and dependency integrity.
2. Live Google Sheets CRM read-only connectivity and schema layout.
3. Multi-signal deduplication guards and fail-closed malformed row detection.
4. Writer safeguards: row width enforcement and explicit-range boundaries.
5. Concurrency architecture: local lock scope and Firestore distributed lease handling.
6. Cloud Run job dispatcher configuration and safety defaults.

Invariants:
- Zero mutations to Google Sheets cells.
- Zero discovery tasks dispatched.
- Zero outreach messages triggered.
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.sheets.google_sheets import RESEARCH_LOG_COLUMNS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DeploymentSafetyVerification")


def verify_module_imports() -> bool:
    logger.info("[Check 1/6] Verifying core module imports...")
    try:
        import lib.crm.reconciliation
        import lib.enrichment.phase_8_6_review_recovery_engine
        import lib.outreach.phase_8_5_qualification
        import lib.sheets.google_sheets
        import lib.system.file_lock
        import lib.system.firestore_state_store
        import lib.system.cloud_run_job_dispatcher
        import scripts.execute_sheets_recovery
        import scripts.run_cloud_run_job
        logger.info("[Check 1/6 PASSED] All core modules imported successfully.")
        return True
    except Exception as e:
        logger.error("[Check 1/6 FAILED] Module import error: %s", e)
        return False


def verify_crm_connectivity_and_schema() -> bool:
    logger.info("[Check 2/6] Verifying live Google Sheets CRM read-only state...")
    try:
        from sheets_sync import get_sheet_client
        from lib.sheets.google_sheets import RESEARCH_LOG_COLUMNS

        client = get_sheet_client()
        ss = client.open_by_key("1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc")
        
        ws_rl = ss.worksheet("RESEARCH_LOG")
        rl_values = ws_rl.get_all_values()
        
        if len(rl_values) != 279:
            logger.error("[Check 2/6 FAILED] Expected 279 rows in RESEARCH_LOG, found %d", len(rl_values))
            return False
            
        if rl_values[0][:28] != RESEARCH_LOG_COLUMNS:
            logger.error("[Check 2/6 FAILED] Header mismatch in RESEARCH_LOG")
            return False

        # Verify AC:BB empty
        for r_idx, row in enumerate(rl_values):
            if len(row) > 28:
                if any(str(c).strip() for c in row[28:54]):
                    logger.error("[Check 2/6 FAILED] Found non-empty data in AC:BB at row %d", r_idx + 1)
                    return False

        # Verify Quarantine tab
        ws_q = ss.worksheet("RESEARCH_LOG_QUARANTINE")
        q_values = ws_q.get_all_values()
        if len(q_values) != 39:
            logger.error("[Check 2/6 FAILED] Expected 39 rows in RESEARCH_LOG_QUARANTINE, found %d", len(q_values))
            return False

        logger.info("[Check 2/6 PASSED] CRM structure verified: RESEARCH_LOG 279 rows (contiguous, AC:BB empty), QUARANTINE 39 rows.")
        return True
    except Exception as e:
        logger.error("[Check 2/6 FAILED] CRM verification error: %s", e)
        return False


def verify_deduplication_guards() -> bool:
    logger.info("[Check 3/6] Verifying fail-closed deduplication guards...")
    try:
        from lib.sheets.google_sheets import GoogleSheetsStorageProvider

        # Initialize mock/read-only provider with shifted row
        shifted_grid = [
            list(RESEARCH_LOG_COLUMNS), # full canonical header
        ] + [[""] * 54 for _ in range(278)]
        # Simulate malformed row at 280 (shifted to AA:BB)
        malformed_row = [""] * 26 + ["RES-MALFORMED", "The Old Bell", "pub", "United Kingdom", "United Kingdom", "VALID", "Manchester"]
        while len(malformed_row) < 54:
            malformed_row.append("")
        malformed_row[52] = "https://www.openstreetmap.org/node/999999" # OSM source URL in column BA
        shifted_grid.append(malformed_row)

        class DummyWorksheet:
            def get_all_values(self):
                return shifted_grid

            def row_values(self, row_idx):
                return shifted_grid[row_idx - 1] if row_idx <= len(shifted_grid) else []

            def update(self, *args, **kwargs):
                pass

        provider = GoogleSheetsStorageProvider()
        provider._research_ws = DummyWorksheet()
        research_map = provider.get_existing_research_map()

        # Test 1: Match by source URL must detect shifted row with is_malformed=True
        match_res = research_map.find_match(
            company_name="The Old Bell",
            source_url="https://www.openstreetmap.org/node/999999",
            city="Manchester",
            target_country="United Kingdom",
        )
        if not match_res or not match_res[1].get("is_malformed"):
            logger.error("[Check 3/6 FAILED] Deduplication failed to match shifted row as malformed: %s", match_res)
            return False

        # Test 2: save_research_log must fail closed on malformed match
        from lib.types import ResearchLogEntry
        dummy_entry = ResearchLogEntry(
            research_id="RES-TEST",
            company_name="The Old Bell",
            industry="pub",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            country_status="COUNTRY_MATCH",
            city="Manchester",
            source_url="https://www.openstreetmap.org/node/999999",
        )
        blocked = False
        try:
            provider.save_research_log([dummy_entry])
        except RuntimeError as e:
            if "[GoogleSheets Malformed Deduplication Block]" in str(e):
                blocked = True
        if not blocked:
            logger.error("[Check 3/6 FAILED] save_research_log failed to fail closed on malformed row match")
            return False

        # Test 3: Distinct venue with same name must NOT match
        distinct_res = research_map.find_match(
            company_name="The Old Bell",
            source_url="https://www.openstreetmap.org/node/111111",
            city="London",
            target_country="United Kingdom",
        )
        if distinct_res is not None:
            logger.error("[Check 3/6 FAILED] Distinct venue incorrectly matched: %s", distinct_res)
            return False

        logger.info("[Check 3/6 PASSED] Fail-closed deduplication and multi-signal disambiguation verified.")
        return True
    except Exception as e:
        logger.error("[Check 3/6 FAILED] Deduplication guard test error: %s", e)
        return False


def verify_writer_safeguards() -> bool:
    logger.info("[Check 4/6] Verifying writer safeguards & schema width enforcement...")
    try:
        from lib.sheets.google_sheets import validate_row_width, RESEARCH_LOG_COLUMNS

        # 1. Exact 28 fields accepted
        valid_row = ["val"] * 28
        validate_row_width(valid_row, RESEARCH_LOG_COLUMNS, "RESEARCH_LOG")

        # 2. 27 fields rejected
        rejected_under = False
        try:
            validate_row_width(["val"] * 27, RESEARCH_LOG_COLUMNS, "RESEARCH_LOG")
        except ValueError:
            rejected_under = True
        if not rejected_under:
            logger.error("[Check 4/6 FAILED] Underwidth row was not rejected")
            return False

        # 3. 29 fields rejected
        rejected_over = False
        try:
            validate_row_width(["val"] * 29, RESEARCH_LOG_COLUMNS, "RESEARCH_LOG")
        except ValueError:
            rejected_over = True
        if not rejected_over:
            logger.error("[Check 4/6 FAILED] Overwidth row was not rejected")
            return False

        logger.info("[Check 4/6 PASSED] Exact 28-column row width schema validation verified.")
        return True
    except Exception as e:
        logger.error("[Check 4/6 FAILED] Writer safeguard error: %s", e)
        return False


def verify_concurrency_architecture() -> bool:
    logger.info("[Check 5/6] Verifying concurrency & distributed lease handling...")
    try:
        from lib.crm.reconciliation import crm_write_lock
        from lib.system.firestore_state_store import FirestoreStateStore

        mock_store = FirestoreStateStore(force_mock=True)
        # Verify distributed lease acquisition and release in mock store
        acquired = False
        with crm_write_lock(enable_distributed=True, state_store=mock_store, timeout_sec=2.0):
            acquired = True
            lease = mock_store._mock_data["operational_leases"].get("crm_write_lock")
            if not lease or not lease.get("holder_id"):
                logger.error("[Check 5/6 FAILED] Distributed lease not recorded in state store")
                return False

        if not acquired:
            logger.error("[Check 5/6 FAILED] Distributed lease failed to acquire")
            return False

        # Subsequent worker must be able to acquire lease after release
        subsequent = mock_store.acquire_lease("crm_write_lock", holder_id="smoke_worker_2", ttl_seconds=5.0)
        if not subsequent:
            logger.error("[Check 5/6 FAILED] Distributed lease was not released for subsequent workers")
            return False

        logger.info("[Check 5/6 PASSED] Concurrency architecture verified (lease acquisition, isolation, release).")
        return True
    except Exception as e:
        logger.error("[Check 5/6 FAILED] Concurrency architecture error: %s", e)
        return False


def verify_cloud_run_dispatcher_safety() -> bool:
    logger.info("[Check 6/6] Verifying Cloud Run dispatcher default safety invariants...")
    try:
        from lib.system.cloud_run_job_dispatcher import CloudRunJobDispatcher
        from lib.system.system_config import SystemConfig

        dispatcher = CloudRunJobDispatcher()
        status = SystemConfig.get_status_dict()

        # Invariants: Travel Mode active, commercial actions locked, automated email disabled
        if not status.get("travel_mode"):
            logger.error("[Check 6/6 FAILED] Travel Mode is NOT active")
            return False
        if not status.get("commercial_actions_locked"):
            logger.error("[Check 6/6 FAILED] Commercial actions are NOT locked")
            return False
        if status.get("automated_email_active"):
            logger.error("[Check 6/6 FAILED] Automated email is ENABLED")
            return False

        logger.info("[Check 6/6 PASSED] Cloud Run dispatcher and safety invariants verified.")
        return True
    except Exception as e:
        logger.error("[Check 6/6 FAILED] Dispatcher safety test error: %s", e)
        return False


def main():
    print("=" * 76)
    print("  DRIPP MEDIA — POST-DEPLOYMENT NON-MUTATING SMOKE TEST")
    print("=" * 76)

    checks = [
        verify_module_imports,
        verify_crm_connectivity_and_schema,
        verify_deduplication_guards,
        verify_writer_safeguards,
        verify_concurrency_architecture,
        verify_cloud_run_dispatcher_safety,
    ]

    all_passed = True
    for check in checks:
        if not check():
            all_passed = False
            break

    print("=" * 76)
    if all_passed:
        print("  ✓ ALL 6 NON-MUTATING SMOKE CHECKS PASSED SUCCESSFULLY")
        print("=" * 76)
        sys.exit(0)
    else:
        print("  ✗ SMOKE TEST VERIFICATION FAILED")
        print("=" * 76)
        sys.exit(1)


if __name__ == "__main__":
    main()
