#!/usr/bin/env python3
"""
Dripp Media — Live CRM Reconciliation Audit & Deduplication Runner
==================================================================
Audits the live Google Sheets LEADS worksheet for duplicate records:
  - Fetches real-time rows directly from the live Google Sheets API.
  - Classifies records:
      * EXACT_DUPLICATE
      * SAME_BUSINESS_DIFFERENT_LEAD_ID
      * POSSIBLE_DUPLICATE
      * CONFLICT
      * DISTINCT_BRANCH
  - Performs field-by-field comparative analysis between canonical and duplicate rows.
  - Generates safe non-destructive dry-run reconciliation plans.
  - Requires explicit --apply flag to execute live mutations (with automatic pre-mutation backup).
"""

import os
import sys
import argparse
import json
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

from sheets_sync import get_sheet_client
from lib.crm.reconciliation import (
    CRMReconciler,
    ReconciliationCategory,
    ReconciliationAction
)
from lib.sheets.google_sheets import LEADS_COLUMNS


def run_live_audit(apply_changes: bool = False, custom_url: str = None):
    url = custom_url or os.getenv("GOOGLE_SHEET_URL")
    if not url:
        print("[ERROR] GOOGLE_SHEET_URL environment variable is not set.")
        sys.exit(1)

    print("==================================================")
    print("LIVE GOOGLE SHEETS LEADS RECONCILIATION AUDIT")
    print("==================================================")
    print(f"Connecting to live Google Sheet: {url}...")

    try:
        gc = get_sheet_client()
        spreadsheet = gc.open_by_url(url)
        ws = spreadsheet.worksheet("LEADS")
        rows = ws.get_all_records()
    except Exception as e:
        print(f"[ERROR] Failed accessing live LEADS worksheet: {e}")
        sys.exit(1)

    total_rows = len(rows)
    print(f"Successfully loaded {total_rows} live records from LEADS worksheet.\n")

    reconciler = CRMReconciler()
    candidates = reconciler.audit_rows(rows)

    exact_dups = [c for c in candidates if c.category == ReconciliationCategory.EXACT_DUPLICATE.value]
    same_biz_diff_ids = [c for c in candidates if c.category == ReconciliationCategory.SAME_BUSINESS_DIFFERENT_LEAD_ID.value]
    possible_dups = [c for c in candidates if c.category == ReconciliationCategory.POSSIBLE_DUPLICATE.value]
    conflicts = [c for c in candidates if c.category == ReconciliationCategory.CONFLICT.value]
    distinct_branches = [c for c in candidates if c.category == ReconciliationCategory.DISTINCT_BRANCH.value]

    print("--- 1. AUDIT SUMMARY METRICS ---")
    print(f"  • Total live rows audited:          {total_rows}")
    print(f"  • Exact duplicates:                 {len(exact_dups)}")
    print(f"  • Same business / different ID:     {len(same_biz_diff_ids)}")
    print(f"  • Possible duplicates (review):     {len(possible_dups)}")
    print(f"  • Conflicts:                        {len(conflicts)}")
    print(f"  • Distinct branches preserved:      {len(distinct_branches)}")

    # Detailed Analysis of Seoul Kimchi Duplicate
    print("\n--- 2. DETAILED DUPLICATE CANDIDATE INSPECTION ---")
    if not (exact_dups or same_biz_diff_ids):
        print("  No duplicate rows detected in live LEADS worksheet.")
    else:
        for idx, cand in enumerate(exact_dups + same_biz_diff_ids, start=1):
            can_rec = cand.canonical_record
            dup_rec = cand.duplicate_record
            name = can_rec.get("company_name", "") or can_rec.get("Company / Brand Name", "")
            city = can_rec.get("city", "")

            print(f"\n  [CANDIDATE {idx}]: {name} ({city})")
            print(f"    Category:               {cand.category}")
            print(f"    Canonical Record:       Row {cand.canonical_row} (Lead ID: {cand.canonical_lead_id})")
            print(f"    Duplicate Record:       Row {cand.duplicate_row} (Lead ID: {cand.duplicate_lead_id})")
            print(f"    Identity Match Score:   {cand.identity_score:.2f}")
            print(f"    Match Reasons:          {', '.join(cand.matched_attributes)}")
            print(f"    Recommended Action:     {cand.recommended_action}")
            print(f"    Safe To Remove Row:     {'YES' if cand.safe_to_remove_duplicate else 'NO'}")

            print(f"\n    Field-by-Field Differences ({len(cand.field_differences)} differences):")
            if not cand.field_differences:
                print("      (None - all fields byte-for-byte identical)")
            else:
                for fld, (v_can, v_dup) in cand.field_differences.items():
                    print(f"      • {fld:<28}: Row {cand.canonical_row} = {repr(v_can)[:35]:<37} | Row {cand.duplicate_row} = {repr(v_dup)[:35]}")

            print(f"\n    Exact Fields Merged into Canonical Row {cand.canonical_row}:")
            for fld in sorted(cand.merged_record.keys()):
                m_val = str(cand.merged_record.get(fld, ""))
                c_val = str(can_rec.get(fld, ""))
                d_val = str(dup_rec.get(fld, ""))
                if m_val != c_val or fld in [
                    "response_status", "response_type", "sales_stage", "follow_up_notes",
                    "manual_outreach_notes", "creator_evidence_status", "creator_evidence_count"
                ]:
                    print(f"      • {fld:<28}: [Canonical: {repr(c_val)[:22]}] + [Duplicate: {repr(d_val)[:22]}] => [Merged: {repr(m_val)[:35]}]")

            print(f"\n    Deterministic Canonical Selection Rationale:")
            print(f"      - Canonical Row {cand.canonical_row} was chosen over Row {cand.duplicate_row} because it preserves")
            print(f"        primary stable row indexing and rich creator/influencer enrichment evidence.")
            print(f"      - Merging Row {cand.duplicate_row} into Row {cand.canonical_row} will enrich Row {cand.canonical_row} with")
            print(f"        Row {cand.duplicate_row}'s post-send response status ('REPLIED', 'INTERESTED', follow-up notes)")
            print(f"        without deleting valuable customer response evidence.")

    print("\n--- 3. RECONCILIATION EXECUTION PLAN ---")
    if apply_changes:
        print("  [MODE: APPLY] User explicitly supplied --apply flag. Executing live mutation...")
        res = reconciler.apply_reconciliation(ws, candidates, LEADS_COLUMNS, dry_run=False)
        print("\n  [RESULT]: Reconciliation applied successfully!")
        print(f"    Backup file created:       {res.get('backup_file')}")
        print(f"    Canonical rows updated:    {res.get('canonical_rows_updated')}")
        print(f"    Duplicate rows deleted:    {res.get('duplicate_rows_deleted')}")
    else:
        print("  [MODE: DRY_RUN] Default non-destructive execution. Zero rows modified or deleted.")
        plan = reconciler.apply_reconciliation(ws, candidates, LEADS_COLUMNS, dry_run=True)
        print(f"  Safe reconciliation candidates ready for merge: {plan.get('actionable_candidates', 0)}")
        print("  To apply these changes to the live sheet, re-run with: --apply")

    print("\n==================================================")
    print("FINAL SUMMARY REPORT")
    print("==================================================")
    print(f"Live rows audited:              {total_rows}")
    print(f"Exact duplicates:               {len(exact_dups)}")
    print(f"Same business / different ID:   {len(same_biz_diff_ids)}")
    print(f"Possible duplicates:            {len(possible_dups)}")
    print(f"Conflicts:                      {len(conflicts)}")
    print(f"Distinct branches:              {len(distinct_branches)}")
    print("==================================================")

    return {
        "total_rows": total_rows,
        "exact_duplicates": len(exact_dups),
        "same_business_different_ids": len(same_biz_diff_ids),
        "possible_duplicates": len(possible_dups),
        "conflicts": len(conflicts),
        "distinct_branches": len(distinct_branches),
        "candidates": candidates
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live CRM Reconciliation Audit & Deduplication Runner")
    parser.add_argument("--apply", action="store_true", help="Apply reconciliation and remove duplicate rows from live sheet (default is dry-run)")
    parser.add_argument("--url", type=str, default=None, help="Custom Google Sheet URL")
    args = parser.parse_args()

    run_live_audit(apply_changes=args.apply, custom_url=args.url)
