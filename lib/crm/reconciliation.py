"""
Dripp Media — CRM Lead Reconciliation & Canonical Record Preservation Engine
=============================================================================
Provides deterministic, audit-first reconciliation for Google Sheets CRM:
  - Audits live LEADS, REVIEW_QUEUE, and RESEARCH_LOG tabs for duplicates and anomalies.
  - Classifies entity pairings into:
      1. EXACT_DUPLICATE: Identical identity and overlapping data.
      2. SAME_BUSINESS_DIFFERENT_LEAD_ID: High-confidence match under distinct lead IDs.
      3. POSSIBLE_DUPLICATE: Ambiguous identity requiring manual review.
      4. CONFLICT: Contradictory official attributes (domain / phone).
      5. DISTINCT_BRANCH: Multi-location branches correctly preserved as separate records.
  - Deterministically selects canonical record using multi-attribute priority:
      1. Earliest created / date_added timestamp.
      2. Record containing active outreach history (REPLIED > SENT > BOUNCED).
      3. Record containing richer verified enrichment data.
      4. Stable row order as final deterministic tiebreaker.
  - Generates non-destructive dry-run audit plans before applying changes.
  - Requires explicit confirmation (`apply=True` / `--apply`) to mutate production sheets.
  - Exports complete pre-mutation backups before executing any row removal.
  - Implements concurrency locking (`crm_write_lock`) to prevent race conditions during ingestion.
"""

import os
import json
import fcntl
import copy
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, field
from contextlib import contextmanager
from typing import Dict, Any, List, Optional, Tuple, Set

import gspread

from lib.crm.identity_matcher import (
    BusinessIdentityMatcher,
    IdentityMatchOutcome,
    clean_ascii_text
)


class ReconciliationCategory(str, Enum):
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    SAME_BUSINESS_DIFFERENT_LEAD_ID = "SAME_BUSINESS_DIFFERENT_LEAD_ID"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    CONFLICT = "CONFLICT"
    DISTINCT_BRANCH = "DISTINCT_BRANCH"


class ReconciliationAction(str, Enum):
    MERGE_TO_CANONICAL = "MERGE_TO_CANONICAL"
    MERGE_TO_CANONICAL_PRESERVE_PRIMARY_ID = "MERGE_TO_CANONICAL_PRESERVE_PRIMARY_ID"
    FLAG_FOR_MANUAL_REVIEW = "FLAG_FOR_MANUAL_REVIEW"
    PRESERVE_BOTH_FLAG_CONFLICT = "PRESERVE_BOTH_FLAG_CONFLICT"
    PRESERVE_SEPARATE_RECORDS = "PRESERVE_SEPARATE_RECORDS"


@dataclass
class ReconciliationCandidate:
    category: str  # ReconciliationCategory value
    canonical_row: int  # 1-based sheet row index
    duplicate_row: int  # 1-based sheet row index
    canonical_lead_id: str
    duplicate_lead_id: str
    identity_score: float
    matched_attributes: List[str]
    reason: str
    recommended_action: str
    canonical_record: Dict[str, Any]
    duplicate_record: Dict[str, Any]
    field_differences: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)
    merged_record: Dict[str, Any] = field(default_factory=dict)
    safe_to_remove_duplicate: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "canonical_row": self.canonical_row,
            "duplicate_row": self.duplicate_row,
            "canonical_lead_id": self.canonical_lead_id,
            "duplicate_lead_id": self.duplicate_lead_id,
            "identity_score": self.identity_score,
            "matched_attributes": self.matched_attributes,
            "reason": self.reason,
            "recommended_action": self.recommended_action,
            "field_differences": {k: {"canonical": str(v[0]), "duplicate": str(v[1])} for k, v in self.field_differences.items()},
            "safe_to_remove_duplicate": self.safe_to_remove_duplicate
        }


@contextmanager
def crm_write_lock(lock_path: str = "data/.crm_write.lock", timeout_sec: float = 30.0):
    """
    Cross-process serialization lock to guarantee atomic CRM write operations.
    Prevents simultaneous pipeline runs from creating race-condition duplicate rows.
    """
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    f = open(lock_path, "w")
    try:
        # Acquire non-blocking or blocking exclusive lock
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


def col_to_letter(col_num: int) -> str:
    string = ""
    while col_num > 0:
        col_num, remainder = divmod(col_num - 1, 26)
        string = chr(65 + remainder) + string
    return string


class CRMReconciler:
    """
    Deterministic reconciliation engine for CRM duplicate resolution and data protection.
    """

    def __init__(self, matcher: Optional[BusinessIdentityMatcher] = None):
        self.matcher = matcher or BusinessIdentityMatcher()

    @staticmethod
    def _parse_timestamp(date_val: Any) -> Optional[datetime]:
        """Parses various date/timestamp formats into a comparable datetime."""
        if not date_val:
            return None
        s = str(date_val).strip()
        if not s:
            return None
        # ISO formats
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            pass
        # YYYY-MM-DD
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            pass
        return None

    def select_canonical_record(
        self,
        record_a: Dict[str, Any],
        record_b: Dict[str, Any],
        row_idx_a: int,
        row_idx_b: int
    ) -> Tuple[Dict[str, Any], Dict[str, Any], int, int]:
        """
        Deterministically selects the canonical record from two matching records:
          1. Earliest created / added timestamp (date_added).
          2. Presence of outreach history over records with zero outreach history.
          3. Richer verified enrichment attributes (creator evidence, reviews, ratings, emails).
          4. Stable row order as final tiebreaker (lower row number wins).
        Returns: (canonical_record, duplicate_record, canonical_row, duplicate_row)
        """
        # Criterion 1: Earliest created timestamp
        ts_a = self._parse_timestamp(record_a.get("date_added") or record_a.get("created_at"))
        ts_b = self._parse_timestamp(record_b.get("date_added") or record_b.get("created_at"))
        if ts_a and ts_b and ts_a != ts_b:
            if ts_a < ts_b:
                return record_a, record_b, row_idx_a, row_idx_b
            else:
                return record_b, record_a, row_idx_b, row_idx_a

        # Criterion 2: Presence of outreach history over uncontacted / not ready records
        def has_outreach(r: Dict[str, Any]) -> bool:
            st = str(r.get("outreach_status", "")).upper()
            return st in ["SENT", "REPLIED", "BOUNCED", "CALLED", "FAILED"] or bool(r.get("outreach_sent_at"))

        has_a = has_outreach(record_a)
        has_b = has_outreach(record_b)
        if has_a != has_b:
            if has_a:
                return record_a, record_b, row_idx_a, row_idx_b
            else:
                return record_b, record_a, row_idx_b, row_idx_a

        # Criterion 3: Richer verified enrichment attributes
        def enrichment_score(r: Dict[str, Any]) -> int:
            score = 0
            if r.get("creator_evidence_status") == "FOUND":
                score += 5
            if r.get("creator_evidence_count"):
                score += 2
            if r.get("review_count"):
                score += 2
            if r.get("rating"):
                score += 1
            if r.get("email") or r.get("primary_email"):
                score += 2
            if r.get("instagram_url"):
                score += 1
            if r.get("facebook_url"):
                score += 1
            return score

        e_a = enrichment_score(record_a)
        e_b = enrichment_score(record_b)
        if e_a != e_b:
            if e_a > e_b:
                return record_a, record_b, row_idx_a, row_idx_b
            else:
                return record_b, record_a, row_idx_b, row_idx_a

        # Criterion 4: Stable row order tiebreaker (first occurrence in sheet wins)
        if row_idx_a <= row_idx_b:
            return record_a, record_b, row_idx_a, row_idx_b
        else:
            return record_b, record_a, row_idx_b, row_idx_a

    def merge_records(self, canonical: Dict[str, Any], duplicate: Dict[str, Any]) -> Dict[str, Any]:
        """
        Merges duplicate record data into canonical record without losing history or enrichment.
        Preserves original canonical lead_id and qualification state.
        Fills missing fields in canonical from duplicate.
        """
        merged = copy.deepcopy(canonical)

        # 1. Fill missing / empty fields from duplicate
        for k, dup_val in duplicate.items():
            can_val = merged.get(k)
            if (can_val is None or str(can_val).strip() == "") and (dup_val is not None and str(dup_val).strip() != ""):
                merged[k] = dup_val

        # 2. Advance outreach and response progression if duplicate had further progression
        status_rank = {
            "REPLIED": 5, "SENT": 4, "CALLED": 3, "BOUNCED": 2, "FAILED": 1, "READY_FOR_REVIEW": 0, "NOT_READY": 0
        }
        can_stat = str(canonical.get("outreach_status", "")).upper()
        dup_stat = str(duplicate.get("outreach_status", "")).upper()
        if status_rank.get(dup_stat, 0) > status_rank.get(can_stat, 0):
            merged["outreach_status"] = duplicate["outreach_status"]

        if duplicate.get("response_status") == "REPLIED":
            merged["response_status"] = duplicate["response_status"]
            if duplicate.get("response_type"):
                merged["response_type"] = duplicate["response_type"]
            if duplicate.get("sales_stage"):
                merged["sales_stage"] = duplicate["sales_stage"]
            if duplicate.get("follow_up_at"):
                merged["follow_up_at"] = duplicate["follow_up_at"]
            if duplicate.get("next_follow_up"):
                merged["next_follow_up"] = duplicate["next_follow_up"]

        # 3. Merge notes cleanly
        can_notes = str(canonical.get("manual_outreach_notes", "")).strip()
        dup_notes = str(duplicate.get("manual_outreach_notes", "")).strip()
        if dup_notes and dup_notes != can_notes:
            if can_notes:
                merged["manual_outreach_notes"] = f"{can_notes} | [Merged]: {dup_notes}"
            else:
                merged["manual_outreach_notes"] = dup_notes

        can_fn = str(canonical.get("follow_up_notes", "")).strip()
        dup_fn = str(duplicate.get("follow_up_notes", "")).strip()
        if dup_fn and dup_fn != can_fn:
            if can_fn:
                merged["follow_up_notes"] = f"{can_fn} | [Merged]: {dup_fn}"
            else:
                merged["follow_up_notes"] = dup_fn

        # 4. Retain latest contact date if duplicate was contacted more recently
        can_lc = self._parse_timestamp(canonical.get("last_contact_at"))
        dup_lc = self._parse_timestamp(duplicate.get("last_contact_at"))
        if dup_lc and (not can_lc or dup_lc > can_lc):
            merged["last_contact_at"] = duplicate["last_contact_at"]

        # 5. Creator evidence preservation (take richer)
        if not canonical.get("creator_evidence_status") and duplicate.get("creator_evidence_status"):
            for cf in ["creator_evidence_status", "creator_evidence_count", "creator_evidence_confidence",
                       "creator_latest_date", "creator_evidence_summary", "creator_evidence_urls", "creator_discovered_at"]:
                if duplicate.get(cf):
                    merged[cf] = duplicate[cf]

        # Invariant: Never overwrite canonical lead_id
        merged["lead_id"] = canonical["lead_id"]
        return merged

    def compute_field_differences(self, rec_a: Dict[str, Any], rec_b: Dict[str, Any]) -> Dict[str, Tuple[Any, Any]]:
        """Identifies all field differences between two records."""
        diffs = {}
        all_keys = set(rec_a.keys()).union(set(rec_b.keys()))
        for k in sorted(all_keys):
            va = str(rec_a.get(k, "")).strip()
            vb = str(rec_b.get(k, "")).strip()
            if va != vb:
                diffs[k] = (rec_a.get(k, ""), rec_b.get(k, ""))
        return diffs

    def audit_rows(self, rows: List[Dict[str, Any]]) -> List[ReconciliationCandidate]:
        """
        Scans all records in a worksheet and pairs up matching/duplicate entities.
        Returns list of ReconciliationCandidates with deterministic canonical assignments.
        """
        candidates: List[ReconciliationCandidate] = []
        n = len(rows)

        # Pairwise comparison across rows
        for i in range(n):
            for j in range(i + 1, n):
                r_a = rows[i]
                r_b = rows[j]
                sheet_row_a = i + 2  # Row 1 is header
                sheet_row_b = j + 2

                score, reasons, conflict = self.matcher.compute_similarity(r_a, r_b)

                # Check for same name in different city / branch
                is_diff_city = any("DIFFERENT_CITY" in r for r in reasons)
                is_branch = any("DISTINCT_BRANCH" in r for r in reasons)

                if is_diff_city or is_branch:
                    # Genuinely different branches
                    candidates.append(ReconciliationCandidate(
                        category=ReconciliationCategory.DISTINCT_BRANCH.value,
                        canonical_row=sheet_row_a,
                        duplicate_row=sheet_row_b,
                        canonical_lead_id=str(r_a.get("lead_id", "")),
                        duplicate_lead_id=str(r_b.get("lead_id", "")),
                        identity_score=score,
                        matched_attributes=reasons,
                        reason="Distinct branches/locations of the same brand",
                        recommended_action=ReconciliationAction.PRESERVE_SEPARATE_RECORDS.value,
                        canonical_record=r_a,
                        duplicate_record=r_b,
                        safe_to_remove_duplicate=False
                    ))
                    continue

                if conflict and score >= 0.50:
                    candidates.append(ReconciliationCandidate(
                        category=ReconciliationCategory.CONFLICT.value,
                        canonical_row=sheet_row_a,
                        duplicate_row=sheet_row_b,
                        canonical_lead_id=str(r_a.get("lead_id", "")),
                        duplicate_lead_id=str(r_b.get("lead_id", "")),
                        identity_score=score,
                        matched_attributes=reasons,
                        reason=conflict or "Contradictory identity attributes",
                        recommended_action=ReconciliationAction.PRESERVE_BOTH_FLAG_CONFLICT.value,
                        canonical_record=r_a,
                        duplicate_record=r_b,
                        safe_to_remove_duplicate=False
                    ))
                    continue

                if score >= self.matcher.high_threshold:
                    # High confidence duplicate match
                    canonical, duplicate, can_row, dup_row = self.select_canonical_record(
                        r_a, r_b, sheet_row_a, sheet_row_b
                    )
                    diffs = self.compute_field_differences(canonical, duplicate)
                    merged = self.merge_records(canonical, duplicate)

                    id_a = str(r_a.get("lead_id", "")).strip()
                    id_b = str(r_b.get("lead_id", "")).strip()

                    if id_a and id_b and id_a == id_b:
                        cat = ReconciliationCategory.EXACT_DUPLICATE.value
                        act = ReconciliationAction.MERGE_TO_CANONICAL.value
                        safe_remove = True
                    else:
                        cat = ReconciliationCategory.SAME_BUSINESS_DIFFERENT_LEAD_ID.value
                        act = ReconciliationAction.MERGE_TO_CANONICAL_PRESERVE_PRIMARY_ID.value
                        safe_remove = True

                    candidates.append(ReconciliationCandidate(
                        category=cat,
                        canonical_row=can_row,
                        duplicate_row=dup_row,
                        canonical_lead_id=str(canonical.get("lead_id", "")),
                        duplicate_lead_id=str(duplicate.get("lead_id", "")),
                        identity_score=score,
                        matched_attributes=reasons,
                        reason=f"High-confidence entity match ({score:.2f}) on: {', '.join(reasons)}",
                        recommended_action=act,
                        canonical_record=canonical,
                        duplicate_record=duplicate,
                        field_differences=diffs,
                        merged_record=merged,
                        safe_to_remove_duplicate=safe_remove
                    ))

                elif score >= self.matcher.possible_threshold:
                    # Ambiguous / partial match
                    candidates.append(ReconciliationCandidate(
                        category=ReconciliationCategory.POSSIBLE_DUPLICATE.value,
                        canonical_row=sheet_row_a,
                        duplicate_row=sheet_row_b,
                        canonical_lead_id=str(r_a.get("lead_id", "")),
                        duplicate_lead_id=str(r_b.get("lead_id", "")),
                        identity_score=score,
                        matched_attributes=reasons,
                        reason=f"Partial identity match ({score:.2f}) requiring manual verification: {', '.join(reasons)}",
                        recommended_action=ReconciliationAction.FLAG_FOR_MANUAL_REVIEW.value,
                        canonical_record=r_a,
                        duplicate_record=r_b,
                        safe_to_remove_duplicate=False
                    ))

        return candidates

    def export_backup(self, rows: List[Dict[str, Any]], backup_dir: str = "data/backups") -> str:
        """
        Saves timestamped JSON backup of all rows before any mutation is applied.
        """
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(backup_dir, f"leads_reconciliation_backup_{ts}.json")
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "row_count": len(rows),
            "rows": rows
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"[CRMReconciler] Backup created: {filepath}")
        return filepath

    def apply_reconciliation(
        self,
        worksheet: gspread.Worksheet,
        candidates: List[ReconciliationCandidate],
        columns: List[str],
        dry_run: bool = True
    ) -> Dict[str, Any]:
        """
        Executes reconciliation on the live worksheet.
        If dry_run is True: returns action plan without mutating.
        If dry_run is False: merges canonical data and safely removes duplicate rows.
        """
        actionable_merges = [
            c for c in candidates 
            if c.category == ReconciliationCategory.EXACT_DUPLICATE.value and c.safe_to_remove_duplicate
        ]
        if not actionable_merges:
            return {"status": "NO_ACTION_REQUIRED", "merged_count": 0, "removed_count": 0}

        if dry_run:
            print("[CRMReconciler] DRY RUN: No changes applied to live spreadsheet.")
            return {
                "status": "DRY_RUN",
                "actionable_candidates": len(actionable_merges),
                "plan": [c.to_dict() for c in actionable_merges]
            }

        # Backup current rows before mutating
        current_rows = worksheet.get_all_records()
        backup_path = self.export_backup(current_rows)

        # Sort duplicate rows in descending order so deleting row N does not shift row < N
        sorted_candidates = sorted(actionable_merges, key=lambda c: c.duplicate_row, reverse=True)

        updated_canonical_rows = set()
        deleted_rows = []

        for cand in sorted_candidates:
            # 1. Update canonical row with merged record (if not already updated)
            if cand.canonical_row not in updated_canonical_rows:
                merged = cand.merged_record
                row_vals = [str(merged.get(col, "")) for col in columns]
                end_col_char = col_to_letter(len(columns))
                cell_range = f"A{cand.canonical_row}:{end_col_char}{cand.canonical_row}"
                try:
                    worksheet.update(values=[row_vals], range_name=cell_range, value_input_option="USER_ENTERED")
                    updated_canonical_rows.add(cand.canonical_row)
                    print(f"[CRMReconciler] Canonical Row {cand.canonical_row} ({cand.canonical_lead_id}) updated with merged data.")
                except Exception as e:
                    print(f"[CRMReconciler] Failed updating Row {cand.canonical_row}: {e}")
                    raise

            # 2. Re-read and verify canonical row before deleting duplicate
            verified_canonical_vals = worksheet.row_values(cand.canonical_row)
            if not verified_canonical_vals or verified_canonical_vals[0] != cand.canonical_lead_id:
                raise RuntimeError(
                    f"Verification failed: Canonical Row {cand.canonical_row} does not match expected lead_id {cand.canonical_lead_id}"
                )

            # 3. Re-read and verify duplicate row immediately before deletion
            current_dup_vals = worksheet.row_values(cand.duplicate_row)
            if not current_dup_vals or current_dup_vals[0] != cand.duplicate_lead_id:
                raise RuntimeError(
                    f"Verification failed: Target Duplicate Row {cand.duplicate_row} does not match expected lead_id {cand.duplicate_lead_id}"
                )

            # 4. Only after verified canonical write and duplicate match, delete duplicate row
            try:
                worksheet.delete_rows(cand.duplicate_row)
                deleted_rows.append(cand.duplicate_row)
                print(f"[CRMReconciler] Duplicate Row {cand.duplicate_row} ({cand.duplicate_lead_id}) deleted successfully.")
            except Exception as e:
                print(f"[CRMReconciler] Failed deleting Row {cand.duplicate_row}: {e}")
                raise

        return {
            "status": "APPLIED",
            "backup_file": backup_path,
            "canonical_rows_updated": list(updated_canonical_rows),
            "duplicate_rows_deleted": deleted_rows
        }
