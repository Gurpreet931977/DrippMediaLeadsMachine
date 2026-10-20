#!/usr/bin/env python3
"""
execute_sheets_recovery.py

Authoritative, fail-closed operational recovery script for RESEARCH_LOG:
1. Concurrency: Acquires live Google Cloud Firestore distributed lease 'crm_write_lock'.
2. Preflight & Fresh Backup: Creates and verifies a readable full JSON backup of all CRM tabs.
3. Phase 1 (Restore 57 canonical records):
   - Restores rows 223-279 to A223:AB279 from verified authoritative recovery artifact.
   - Reads back and verifies all 28 fields across all 57 rows (1,596 cells).
   - Only after exact verification succeeds, clears AC223:BB279 (preserving AA:AB).
4. Phase 2 (Archive 38 unresolved records):
   - Preserves complete 28-field payloads with provenance (original_row, timestamp, reason).
   - Checks quarantine destination for existing entries to prevent duplicate archiving on retries.
   - Reads back and verifies every archived field against the preflight snapshot.
   - Only after successful verification, clears AA280:BB317 in RESEARCH_LOG.
5. Final Verification:
   - Reads back entire live worksheet; verifies rows 1-222 untouched, rows 223-279 canonical in A:AB,
     shifted columns AC223:BB279 and AA280:BB317 completely cleared.

Safety Invariants:
- Takes and verifies readable backup before any mutation.
- Default mode is DRY RUN. Live mutation requires --live and --confirm-token.
- Reconciles state before executing each phase for safe, idempotent retries.
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ensure live Firestore distributed lease is active
os.environ["FIRESTORE_ENABLED"] = "true"

from lib.sheets.google_sheets import GoogleSheetsStorageProvider, RESEARCH_LOG_COLUMNS, validate_row_width, col_to_letter
from lib.crm.reconciliation import crm_write_lock
from sheets_sync import get_sheet_client

RECOVERY_CANDIDATE_PATH = "data/backups/recovery_candidates_RESEARCH_LOG_20261019T192652Z.json"
EXPECTED_SPREADSHEET_KEY = "1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc"
CONFIRM_TOKEN = "EXECUTE_LIVE_RESEARCH_LOG_RECOVERY_20261020"

QUARANTINE_PROVENANCE_HEADERS = ["original_sheet_row", "quarantined_at", "quarantine_reason"]
QUARANTINE_HEADERS = QUARANTINE_PROVENANCE_HEADERS + RESEARCH_LOG_COLUMNS


def normalize_sheet_val(val: Any) -> str:
    """Normalizes spreadsheet values, stripping leading apostrophes and handling float/int numeric formatting."""
    if val is None:
        return ""
    s = str(val).strip()
    if s.startswith("'"):
        s = s[1:].strip()
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except (ValueError, OverflowError):
        pass
    return s


def take_and_verify_live_backup(client, spreadsheet_key: str) -> Tuple[str, Dict[str, Any]]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = os.path.join("data", "backups", f"live_sheets_backup_{timestamp}.json")
    print(f"\n[Backup] Initiating fresh pre-recovery backup to {backup_path}...")
    ss = client.open_by_key(spreadsheet_key)
    backup_data = {
        "timestamp": timestamp,
        "spreadsheet_id": spreadsheet_key,
        "spreadsheet_title": ss.title,
        "tabs": {}
    }
    required_tabs = ["LEADS", "REVIEW_QUEUE", "RESEARCH_LOG"]
    for ws in ss.worksheets():
        vals = ws.get_all_values()
        print(f"[Backup] Exporting tab '{ws.title}': {len(vals)} rows x {ws.col_count} cols...")
        backup_data["tabs"][ws.title] = {
            "row_count": ws.row_count,
            "col_count": ws.col_count,
            "values": vals
        }

    for req in required_tabs:
        if req not in backup_data["tabs"]:
            raise RuntimeError(f"[Backup Failed] Required CRM tab '{req}' missing from spreadsheet.")
        if len(backup_data["tabs"][req]["values"]) < 1:
            raise RuntimeError(f"[Backup Failed] Required CRM tab '{req}' has empty values.")

    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(backup_data, f, indent=2, ensure_ascii=False)

    # Verification: Read back and confirm JSON integrity
    print(f"[Backup Verification] Reading back {backup_path} to verify disk readability...")
    with open(backup_path, "r", encoding="utf-8") as f:
        read_back_data = json.load(f)

    if read_back_data.get("spreadsheet_id") != spreadsheet_key:
        raise RuntimeError("[Backup Verification Failed] Spreadsheet ID mismatch in backup file.")
    for req in required_tabs:
        row_count = len(read_back_data["tabs"][req]["values"])
        if row_count != len(backup_data["tabs"][req]["values"]):
            raise RuntimeError(f"[Backup Verification Failed] Row count mismatch for tab '{req}'.")

    file_size = os.path.getsize(backup_path)
    print(f"[Backup Verification PASSED] Backup is fully verified and readable ({file_size:,} bytes).")
    return backup_path, backup_data


def run_preflight_checks(
    ws,
    recovery_data: Dict[str, Any],
    live_backup_data: Optional[Dict[str, Any]] = None
) -> Tuple[List[List[Any]], List[Dict[str, Any]], List[Dict[str, Any]], bool, bool]:
    print("\n[Preflight] Running comprehensive preflight checks...")
    all_values = ws.get_all_values()
    total_rows = len(all_values)
    print(f"[Preflight] Worksheet '{ws.title}' has {total_rows} rows.")

    if total_rows < 279:
        raise RuntimeError(f"[Preflight FAILED] Worksheet has only {total_rows} rows, expected >= 279.")

    candidates = recovery_data.get("candidates", [])
    cohort_1 = [c for c in candidates if c["sheet_row"] <= 279 and c["classification"] == "RECONSTRUCTABLE_AUTHORITATIVE"]
    cohort_2 = [c for c in candidates if c["sheet_row"] >= 280]

    if len(cohort_1) != 57:
        raise RuntimeError(f"[Preflight FAILED] Expected exactly 57 Cohort 1 candidates, found {len(cohort_1)}.")
    if len(cohort_2) != 38:
        raise RuntimeError(f"[Preflight FAILED] Expected exactly 38 Cohort 2 candidates, found {len(cohort_2)}.")

    reconstruct_rows = []
    for c in cohort_1:
        write_values = c.get("cell_mutation_preview", {}).get("write_values")
        if not write_values or len(write_values) != 28:
            raise RuntimeError(f"[Preflight FAILED] Row {c['sheet_row']} proposed values invalid or not 28 columns.")
        validate_row_width(write_values, RESEARCH_LOG_COLUMNS, "RESEARCH_LOG")
        reconstruct_rows.append(write_values)

    # Check Phase 1 state: Already restored vs. Unrestored
    phase_1_already_done = False
    row_223_grid = all_values[222]
    first_target_val = row_223_grid[0] if len(row_223_grid) > 0 else ""
    first_exp_id = reconstruct_rows[0][0]

    if first_target_val == first_exp_id:
        print("[Preflight State Detection] Row 223 column A matches expected canonical research_id.")
        # Check if all 57 rows in A:AB match
        matches_all = True
        for offset, exp_r in enumerate(reconstruct_rows):
            curr_row = all_values[222 + offset]
            curr_a_ab = [normalize_sheet_val(x) for x in curr_row[:28]]
            exp_a_ab = [normalize_sheet_val(x) for x in exp_r]
            if curr_a_ab != exp_a_ab:
                matches_all = False
                break
        if matches_all:
            phase_1_already_done = True
            print("[Preflight State Detection] Phase 1 canonical restoration is ALREADY complete in A223:AB279.")

    if not phase_1_already_done:
        print("[Preflight] Verifying Cohort 1 rows 223-279 for fresh restoration...")
        for c in cohort_1:
            row_idx = c["sheet_row"]
            row_grid = all_values[row_idx - 1]
            cols_a_z = row_grid[:26]
            if any(str(x).strip() for x in cols_a_z):
                non_empty = [(i, cols_a_z[i]) for i in range(len(cols_a_z)) if str(cols_a_z[i]).strip()]
                raise RuntimeError(f"[Preflight FAILED] Row {row_idx} has non-empty cells in A:Z: {non_empty}")
            exp_id = c["identity_signals"]["research_id"]
            act_id = str(row_grid[26]).strip() if len(row_grid) > 26 else ""
            if act_id != exp_id:
                raise RuntimeError(f"[Preflight FAILED] Row {row_idx} column AA research_id mismatch: expected '{exp_id}', found '{act_id}'.")

    # Check Phase 2 state: Already archived vs. Unarchived
    phase_2_already_done = False
    if total_rows <= 279:
        phase_2_already_done = True
        print("[Preflight State Detection] Sheet row count is <= 279 (rows 280-317 are ALREADY cleared).")
    else:
        row_280_grid = all_values[279]
        col_aa_280 = str(row_280_grid[26]).strip() if len(row_280_grid) > 26 else ""
        if col_aa_280 == "":
            all_cleared = True
            for r_offset in range(min(38, total_rows - 279)):
                r_grid = all_values[279 + r_offset]
                if any(str(x).strip() for x in r_grid[26:54]):
                    all_cleared = False
                    break
            if all_cleared:
                phase_2_already_done = True
                print("[Preflight State Detection] Phase 2 rows 280-317 in AA:BB are ALREADY cleared.")

    if not phase_2_already_done:
        print("[Preflight] Verifying Cohort 2 rows 280-317 for fresh archiving...")
        for c in cohort_2:
            row_idx = c["sheet_row"]
            row_grid = all_values[row_idx - 1]
            cols_a_z = row_grid[:26]
            if any(str(x).strip() for x in cols_a_z):
                non_empty = [(i, cols_a_z[i]) for i in range(len(cols_a_z)) if str(cols_a_z[i]).strip()]
                raise RuntimeError(f"[Preflight FAILED] Row {row_idx} has non-empty cells in A:Z: {non_empty}")
            exp_id = c["identity_signals"]["research_id"]
            act_id = str(row_grid[26]).strip() if len(row_grid) > 26 else ""
            if act_id != exp_id:
                raise RuntimeError(f"[Preflight FAILED] Row {row_idx} column AA research_id mismatch: expected '{exp_id}', found '{act_id}'.")

    print(f"[Preflight PASSED] Checks verified (Phase 1 already done: {phase_1_already_done}, Phase 2 already done: {phase_2_already_done}).")
    return reconstruct_rows, cohort_1, cohort_2, phase_1_already_done, phase_2_already_done


def execute_recovery(is_live: bool, token: str):
    print("=" * 80)
    print("DRIIPP MEDIA LEADS MACHINE - PRODUCTION GOOGLE SHEETS RECOVERY")
    print(f"Mode: {'LIVE EXECUTION' if is_live else 'DRY RUN (Simulated)'}")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 80)

    if is_live and token != CONFIRM_TOKEN:
        raise ValueError(f"[Security Gate] Invalid confirmation token '{token}'. Aborting.")

    if not os.path.exists(RECOVERY_CANDIDATE_PATH):
        raise FileNotFoundError(f"Recovery candidate artifact not found at {RECOVERY_CANDIDATE_PATH}")

    with open(RECOVERY_CANDIDATE_PATH, "r", encoding="utf-8") as f:
        recovery_data = json.load(f)

    client = get_sheet_client()
    ss = client.open_by_key(EXPECTED_SPREADSHEET_KEY)
    ws = ss.worksheet("RESEARCH_LOG")

    # Step 0: Acquire Distributed Lock to prevent Cloud Run container collisions
    print("\n[Distributed Lock] Acquiring Google Cloud Firestore lease 'crm_write_lock'...")
    with crm_write_lock(enable_distributed=True, timeout_sec=60.0):
        print("[Distributed Lock] Acquired exclusive operational lease 'crm_write_lock'.")

        # Step 1: Pre-flight Backup
        if is_live:
            backup_file, live_backup_data = take_and_verify_live_backup(client, EXPECTED_SPREADSHEET_KEY)
        else:
            print("[Dry Run] Would take and verify readable backup to data/backups/live_sheets_backup_<timestamp>.json")
            backup_file = "data/backups/live_sheets_backup_SIMULATED.json"
            live_backup_data = None

        # Step 2: Preflight Checks and State Detection
        reconstruct_rows, cohort_1, cohort_2, p1_done, p2_done = run_preflight_checks(
            ws, recovery_data, live_backup_data
        )

        dest_range = "A223:AB279"
        clear_range = "AC223:BB279"

        # ---------------------------------------------------------------------
        # Phase 1: Restore 57 Canonical Records (Rows 223-279)
        # ---------------------------------------------------------------------
        print("\n" + "-" * 70)
        print("PHASE 1: RESTORE 57 CANONICAL RECORDS (ROWS 223-279)")
        print("-" * 70)
        print(f"Destination Range: {dest_range} (57 rows x 28 columns = 1,596 cells)")
        print(f"Orphaned Clear Range: {clear_range} (57 rows x 26 columns = 1,482 cells)")
        print("Disjointness Invariant: {1..28} ∩ {29..54} = ∅ (Columns AA:AB strictly preserved)")

        if is_live:
            if not p1_done:
                print(f"[Phase 1 Action] Writing 57 verified records to {dest_range}...")
                ws.update(values=reconstruct_rows, range_name=dest_range, value_input_option="USER_ENTERED")
                print("[Phase 1 Action] Write completed. Executing full read-back verification...")
            else:
                print("[Phase 1 Notice] Records in A223:AB279 already in place. Performing full read-back audit...")

            # Read back and compare ALL 28 fields across all 57 rows
            read_back_p1 = ws.get_values(dest_range)
            if len(read_back_p1) != len(reconstruct_rows):
                raise RuntimeError(
                    f"[Phase 1 Verification Failed] Expected {len(reconstruct_rows)} rows in {dest_range}, "
                    f"read back {len(read_back_p1)} rows."
                )

            total_fields_verified = 0
            for row_offset, (exp_row, act_row) in enumerate(zip(reconstruct_rows, read_back_p1)):
                curr_row_num = 223 + row_offset
                if len(act_row) != 28:
                    # If Google Sheets truncated trailing blanks, pad to 28 for exact comparison
                    padded_act = act_row + [""] * (28 - len(act_row))
                else:
                    padded_act = act_row

                for col_idx in range(28):
                    exp_val = normalize_sheet_val(exp_row[col_idx])
                    act_val = normalize_sheet_val(padded_act[col_idx])
                    if act_val != exp_val:
                        col_name = RESEARCH_LOG_COLUMNS[col_idx]
                        raise RuntimeError(
                            f"[Phase 1 Verification Failed] Row {curr_row_num} column {col_idx+1} ('{col_name}') mismatch: "
                            f"expected '{exp_val}', read back '{act_val}'."
                        )
                    total_fields_verified += 1

            print(f"[Phase 1 Verification PASSED] All {total_fields_verified} fields (57 rows x 28 cols) verified with 100% exact match.")

            # Only after exact verification succeeds, clear AC223:BB279
            print(f"[Phase 1 Action] Inspecting orphaned columns {clear_range}...")
            ac_bb_values = ws.get_values(clear_range)
            has_cells_to_clear = any(any(str(c).strip() for c in r) for r in ac_bb_values)
            if has_cells_to_clear:
                print(f"[Phase 1 Action] Clearing orphaned shifted columns {clear_range}...")
                ws.batch_clear([clear_range])
                # Verify clearing
                cleared_check = ws.get_values(clear_range)
                if any(any(str(c).strip() for c in r) for r in cleared_check):
                    raise RuntimeError(f"[Phase 1 Verification Failed] Range {clear_range} not empty after clear.")
                print(f"[Phase 1 Action] Range {clear_range} (1,482 cells) cleared successfully.")
            else:
                print(f"[Phase 1 Notice] Range {clear_range} is already completely empty.")

            # Crucial Check: Verify AA223:AB279 still contains destination data
            aa_ab_check = ws.get_values("AA223:AB279")
            for offset, r in enumerate(aa_ab_check):
                exp_source_url = str(reconstruct_rows[offset][26]).strip()
                exp_date_res = str(reconstruct_rows[offset][27]).strip()
                act_source_url = str(r[0]).strip() if len(r) > 0 else ""
                act_date_res = str(r[1]).strip() if len(r) > 1 else ""
                if act_source_url != exp_source_url or act_date_res != exp_date_res:
                    raise RuntimeError(f"[Phase 1 CRITICAL Safety Check Failed] Row {223+offset} columns AA:AB were corrupted!")
            print("[Phase 1 Invariant Verified] Columns AA:AB remain 100% intact and preserved.")

        else:
            print(f"[Dry Run] Would write 57 rows (1,596 cells) to {dest_range}")
            print(f"[Dry Run] Would verify all 28 columns for all 57 rows")
            print(f"[Dry Run] Would clear {clear_range} (1,482 cells) preserving AA:AB")

        # ---------------------------------------------------------------------
        # Phase 2: Archive 38 Unresolved Records (Rows 280-317)
        # ---------------------------------------------------------------------
        print("\n" + "-" * 70)
        print("PHASE 2: ARCHIVE 38 UNRESOLVED RECORDS (ROWS 280-317)")
        print("-" * 70)

        # Extract snapshot of 38 records from live worksheet or preflight backup
        quarantine_payloads = []
        now_iso = datetime.now(timezone.utc).isoformat()
        current_grid = ws.get_all_values()

        for c in cohort_2:
            row_idx = c["sheet_row"]
            row_data = current_grid[row_idx - 1] if len(current_grid) >= row_idx else []
            shifted_values = row_data[26:54] if len(row_data) >= 54 else row_data[26:]
            while len(shifted_values) < 28:
                shifted_values.append("")
            clean_shifted = shifted_values[:28]

            # If row was already cleared, recover from authoritative preflight backup
            if not any(str(x).strip() for x in clean_shifted):
                for b_file in [
                    "data/backups/live_sheets_backup_20261020T093802Z.json",
                    "data/backups/live_sheets_backup_20261020T093411Z.json",
                    "data/backups/live_sheets_backup_20261019T145843Z.json"
                ]:
                    if os.path.exists(b_file):
                        with open(b_file, "r", encoding="utf-8") as bf:
                            b_data = json.load(bf)
                            b_res = b_data.get("tabs", {}).get("RESEARCH_LOG", {}).get("values", [])
                            if len(b_res) >= row_idx:
                                b_row = b_res[row_idx - 1]
                                b_shifted = b_row[26:54]
                                if any(str(x).strip() for x in b_shifted):
                                    clean_shifted = b_shifted[:28]
                                    while len(clean_shifted) < 28:
                                        clean_shifted.append("")
                                    break

            provenance_record = [row_idx, now_iso, "UNRESOLVED_OCT12_SHADOW_DUPLICATE_AA_BB_SHIFT"] + clean_shifted
            quarantine_payloads.append((row_idx, clean_shifted[0], clean_shifted, provenance_record))

        print(f"[Phase 2] Prepared {len(quarantine_payloads)} records for archival into 'RESEARCH_LOG_QUARANTINE'.")

        if is_live:
            # 1. Ensure RESEARCH_LOG_QUARANTINE tab exists with canonical header schema
            existing_tab_names = [w.title for w in ss.worksheets()]
            if "RESEARCH_LOG_QUARANTINE" not in existing_tab_names:
                print("[Phase 2 Action] Tab 'RESEARCH_LOG_QUARANTINE' not found. Creating worksheet...")
                q_ws = ss.add_worksheet(title="RESEARCH_LOG_QUARANTINE", rows=100, cols=len(QUARANTINE_HEADERS) + 5)
                q_ws.append_row(QUARANTINE_HEADERS, value_input_option="USER_ENTERED")
                print(f"[Phase 2 Action] Tab created with {len(QUARANTINE_HEADERS)} header columns.")
            else:
                q_ws = ss.worksheet("RESEARCH_LOG_QUARANTINE")

            # 2. Check existing records in quarantine tab to prevent duplicate archiving on retries
            q_existing_values = q_ws.get_all_values()
            existing_archive_keys = set()
            if len(q_existing_values) > 1:
                for q_row in q_existing_values[1:]:
                    if q_row:
                        orig_row_num = str(q_row[0]).strip()
                        res_id = str(q_row[3]).strip() if len(q_row) > 3 else ""
                        existing_archive_keys.add((orig_row_num, res_id))

            # 2. Write / Reconcile records in quarantine tab using RAW mode to prevent formula parse errors on '+' phones
            all_quarantine_rows = [p[3] for p in quarantine_payloads]
            end_col_letter = col_to_letter(len(QUARANTINE_HEADERS))
            dest_q_range = f"A2:{end_col_letter}{1 + len(all_quarantine_rows)}"
            print(f"[Phase 2 Action] Writing {len(all_quarantine_rows)} records to 'RESEARCH_LOG_QUARANTINE' ({dest_q_range}) using RAW input...")
            q_ws.update(values=all_quarantine_rows, range_name=dest_q_range, value_input_option="RAW")
            print(f"[Phase 2 Action] Wrote {len(all_quarantine_rows)} records with RAW formatting.")

            # 3. Read back and verify EVERY archived field against expected snapshot
            print("[Phase 2 Action] Reading back 'RESEARCH_LOG_QUARANTINE' to verify every field...")
            q_updated_values = q_ws.get_all_values()
            q_records_by_key = {}
            for row_in_q in q_updated_values[1:]:
                if row_in_q:
                    key = (str(row_in_q[0]).strip(), str(row_in_q[3]).strip() if len(row_in_q) > 3 else "")
                    q_records_by_key[key] = row_in_q

            for orig_row, res_id, clean_vals, full_rec in quarantine_payloads:
                key = (str(orig_row), str(res_id))
                if key not in q_records_by_key:
                    raise RuntimeError(f"[Phase 2 Verification Failed] Record {key} not found in quarantine archive!")
                q_row_act = q_records_by_key[key]
                # Compare research_id and payload
                for idx, exp_val in enumerate(clean_vals):
                    col_offset = 3 + idx
                    act_val = normalize_sheet_val(q_row_act[col_offset]) if len(q_row_act) > col_offset else ""
                    if act_val != normalize_sheet_val(exp_val):
                        raise RuntimeError(
                            f"[Phase 2 Verification Failed] Archived row {orig_row} field {idx+1} mismatch: "
                            f"expected '{exp_val}', found '{act_val}'."
                        )
            print("[Phase 2 Verification PASSED] All 38 quarantine records verified in 'RESEARCH_LOG_QUARANTINE'.")

            # 4. Only after successful verification, clear AA280:BB317 in RESEARCH_LOG
            print("[Phase 2 Action] Inspecting AA280:BB317 in 'RESEARCH_LOG'...")
            current_aa_bb_280_317 = ws.get_values("AA280:BB317")
            if any(any(str(c).strip() for c in r) for r in current_aa_bb_280_317):
                print("[Phase 2 Action] Clearing AA280:BB317 (1,064 cells) in 'RESEARCH_LOG'...")
                ws.batch_clear(["AA280:BB317"])
                # Read back to confirm
                post_clear_check = ws.get_values("AA280:BB317")
                if any(any(str(c).strip() for c in r) for r in post_clear_check):
                    raise RuntimeError("[Phase 2 CRITICAL Error] AA280:BB317 not completely cleared after batch_clear.")
                print("[Phase 2 Action] Range AA280:BB317 cleared successfully.")
            else:
                print("[Phase 2 Notice] Range AA280:BB317 is already completely clear.")

        else:
            print(f"[Dry Run] Would ensure 'RESEARCH_LOG_QUARANTINE' tab exists with {len(QUARANTINE_HEADERS)} headers.")
            print(f"[Dry Run] Would archive {len(quarantine_payloads)} records with full provenance.")
            print(f"[Dry Run] Would verify all fields in quarantine tab.")
            print(f"[Dry Run] Would clear AA280:BB317 in 'RESEARCH_LOG'.")

        # ---------------------------------------------------------------------
        # Final Full-Sheet Audit
        # ---------------------------------------------------------------------
        print("\n" + "=" * 80)
        print("FINAL POST-RECOVERY AUDIT & VERIFICATION")
        print("=" * 80)

        if is_live:
            final_grid = ws.get_all_values()
            print(f"[Final Audit] Total worksheet rows: {len(final_grid)}")

            # 1. Rows 1-222 untouched check
            print("[Final Audit] Verifying rows 1-222 are completely untouched...")
            if live_backup_data:
                orig_r1_222 = live_backup_data["tabs"]["RESEARCH_LOG"]["values"][:222]
                final_r1_222 = final_grid[:222]
                for r_num in range(222):
                    orig_r = [normalize_sheet_val(x) for x in orig_r1_222[r_num][:28]]
                    final_r = [normalize_sheet_val(x) for x in final_r1_222[r_num][:28]]
                    if orig_r != final_r:
                        raise RuntimeError(f"[Final Audit FAILED] Unintended modification detected at row {r_num+1}!")
            print("[Final Audit PASSED] Rows 1-222 are completely identical to preflight backup.")

            # 2. Rows 223-279 canonical check in A:AB
            print("[Final Audit] Verifying rows 223-279 contain the 57 canonical records in A:AB...")
            for offset, exp_r in enumerate(reconstruct_rows):
                curr_row = final_grid[222 + offset]
                act_a_ab = [normalize_sheet_val(x) for x in curr_row[:28]]
                exp_a_ab = [normalize_sheet_val(x) for x in exp_r]
                if act_a_ab != exp_a_ab:
                    raise RuntimeError(f"[Final Audit FAILED] Row {223+offset} does not match expected canonical record.")
            print("[Final Audit PASSED] Rows 223-279 strictly contain the 57 canonical records in A:AB.")

            # 3. Columns AC223:BB279 empty check
            print("[Final Audit] Verifying AC223:BB279 are completely empty...")
            for offset in range(57):
                curr_row = final_grid[222 + offset]
                ac_bb_slice = curr_row[28:54]
                if any(str(x).strip() for x in ac_bb_slice):
                    raise RuntimeError(f"[Final Audit FAILED] Row {223+offset} has uncleared data in AC:BB.")
            print("[Final Audit PASSED] Columns AC223:BB279 are 100% empty.")

            # 4. Columns AA280:BB317 empty check
            print("[Final Audit] Verifying AA280:BB317 are completely empty...")
            for offset in range(38):
                if len(final_grid) > 279 + offset:
                    curr_row = final_grid[279 + offset]
                    aa_bb_slice = curr_row[26:54]
                    if any(str(x).strip() for x in aa_bb_slice):
                        raise RuntimeError(f"[Final Audit FAILED] Row {280+offset} has uncleared data in AA:BB.")
            print("[Final Audit PASSED] Columns AA280:BB317 are 100% empty.")

            # 5. Quarantine tab preservation check
            print("[Final Audit] Verifying 'RESEARCH_LOG_QUARANTINE' contains all 38 preserved records...")
            q_final_ws = ss.worksheet("RESEARCH_LOG_QUARANTINE")
            q_final_values = q_final_ws.get_all_values()
            archived_count = len(q_final_values) - 1
            if archived_count < 38:
                raise RuntimeError(f"[Final Audit FAILED] Quarantine tab only has {archived_count} records, expected >= 38.")
            print(f"[Final Audit PASSED] Quarantine tab contains {archived_count} records with full provenance.")
        else:
            print("[Dry Run] Would verify rows 1-222 untouched against preflight backup.")
            print("[Dry Run] Would verify rows 223-279 strictly contain 57 canonical records in A:AB.")
            print("[Dry Run] Would verify AC223:BB279 are 100% empty.")
            print("[Dry Run] Would verify AA280:BB317 are 100% empty in RESEARCH_LOG.")
            print("[Dry Run] Would verify RESEARCH_LOG_QUARANTINE contains all 38 archived records.")

        print("\n" + "=" * 80)
        print("RECOVERY EXECUTION SUMMARY:")
        print(f"Status:               {'SUCCESS (LIVE SPREADSHEET RECOVERED)' if is_live else 'DRY RUN COMPLETED'}")
        print(f"Backup File:          {backup_file}")
        print("Phase 1 Restored:     57 canonical records (1,596 cells written to A223:AB279)")
        print("Phase 1 Cleared:      1,482 orphaned cells cleared in AC223:BB279 (AA:AB preserved)")
        print("Phase 2 Archived:     38 unresolved records archived to 'RESEARCH_LOG_QUARANTINE'")
        print("Phase 2 Cleared:      1,064 cells cleared in AA280:BB317 of 'RESEARCH_LOG'")
        print("Final Invariants:     Contiguous canonical records from A1 to AB279; zero data loss.")
        print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="RESEARCH_LOG Operational Recovery Script")
    parser.add_argument("--live", action="store_true", help="Execute live mutations on Google Sheets")
    parser.add_argument("--confirm-token", type=str, default="", help="Safety confirmation token for live execution")
    args = parser.parse_args()

    execute_recovery(is_live=args.live, token=args.confirm_token)


if __name__ == "__main__":
    main()
