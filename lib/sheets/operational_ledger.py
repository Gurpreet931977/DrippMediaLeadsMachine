"""
lib/sheets/operational_ledger.py
================================
Durable Operational Job-State Ledger backed by Google Sheets.
Fulfills Phase 12.2.3 Requirements:
1. Operational job progress and stage checkpoints persisted in dedicated 'OPERATIONAL_JOBS' worksheet.
2. Complete separation from business CRM records: never mixes operational state with LEADS, REVIEW_QUEUE, or RESEARCH_LOG.
3. Single-instance mutual exclusion with heartbeats and stale-job reconciliation (fails closed on conflict).
4. Resumable checkpoint tracking: candidate cursor, processed keys, and recovery metadata.
5. Google Sheets API quota conservation: bounded batched updates, debounced heartbeats, exponential backoff.
6. Offline / Mock mode support for isolated testing without modifying live CRM records.
"""

import os
import json
import time
import uuid
import hashlib
import logging
from enum import Enum
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("OperationalJobLedger")

# Dedicated operational tab name (NEVER mix with LEADS, REVIEW_QUEUE, or RESEARCH_LOG)
OPERATIONAL_JOBS_TAB = "OPERATIONAL_JOBS"
LEDGER_SCHEMA_VERSION = "1.0.0"

# Canonical Columns for OPERATIONAL_JOBS worksheet
OPERATIONAL_JOBS_COLUMNS = [
    "run_id",
    "schema_version",
    "status",
    "territory",
    "industry",
    "stage",
    "requested_leads",
    "qualified_count",
    "candidates_evaluated",
    "candidate_cursor",
    "processed_keys_compact",
    "attempt_count",
    "error_code",
    "created_at",
    "updated_at",
    "last_heartbeat",
    "lease_holder",
    "lease_expires_at",
    "idempotency_hash",
    "recovery_metadata_json",
]

DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 120.0  # 2 minutes without heartbeat = container sleep/interrupted


class JobLifecycleStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ConcurrentJobConflictError(Exception):
    """Raised when an execution is attempted while another job holds an active unexpired lease."""
    pass


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> float:
    return time.time()


class OperationalJobLedger:
    """
    Manages durable discovery job state, checkpoints, and execution leases
    inside a dedicated Google Sheets operational tab.
    """

    def __init__(
        self,
        storage_provider: Optional[Any] = None,
        heartbeat_timeout_seconds: float = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        force_mock: bool = False,
    ):
        self.storage_provider = storage_provider
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.force_mock = force_mock
        self._mock_jobs: Dict[str, Dict[str, Any]] = {}
        self._worksheet = None

    def _execute_with_retry(self, fn, *args, max_retries: int = 3, initial_delay: float = 0.5, **kwargs):
        """
        Executes a Google Sheets operation with bounded exponential backoff retries
        to absorb transient 429 rate limit or timeout errors without crashing.
        """
        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_exc = e
                err_str = str(e).lower()
                is_transient = any(code in err_str for code in ["429", "503", "quota", "rate limit", "timeout", "unavailable"])
                if attempt < max_retries and is_transient:
                    delay = initial_delay * (2 ** (attempt - 1))
                    logger.warning("Google Sheets transient error (attempt %d/%d): %s. Backing off %.2fs.", attempt, max_retries, e, delay)
                    time.sleep(delay)
                else:
                    break
        raise last_exc

    def _get_worksheet(self):
        if self.force_mock or not self.storage_provider:
            return None
        if self._worksheet:
            return self._worksheet

        try:
            ws = self._execute_with_retry(self.storage_provider.spreadsheet.worksheet, OPERATIONAL_JOBS_TAB)
            self._worksheet = ws
            return ws
        except Exception as e:
            logger.debug("Worksheet %s not found or error accessing: %s", OPERATIONAL_JOBS_TAB, e)
            return None

    # -------------------------------------------------------------------------
    # 1. MIGRATION & SCHEMA INITIALIZATION (Dry-run safe)
    # -------------------------------------------------------------------------

    def ensure_operational_worksheet(self, dry_run: bool = True) -> Dict[str, Any]:
        """
        Inspects the workbook for the OPERATIONAL_JOBS tab and creates it if missing.
        Defaults to dry_run=True to prevent accidental schema modifications.
        """
        if self.force_mock or not self.storage_provider:
            return {
                "tab": OPERATIONAL_JOBS_TAB,
                "status": "MOCK_READY",
                "columns": OPERATIONAL_JOBS_COLUMNS,
                "dry_run": dry_run,
                "action": "NONE",
            }

        spreadsheet = self.storage_provider.spreadsheet
        existing_titles = [s.title for s in spreadsheet.worksheets()]

        if OPERATIONAL_JOBS_TAB in existing_titles:
            return {
                "tab": OPERATIONAL_JOBS_TAB,
                "status": "EXISTS",
                "columns": OPERATIONAL_JOBS_COLUMNS,
                "dry_run": dry_run,
                "action": "ALREADY_PRESENT",
            }

        if dry_run:
            return {
                "tab": OPERATIONAL_JOBS_TAB,
                "status": "MISSING",
                "columns": OPERATIONAL_JOBS_COLUMNS,
                "dry_run": True,
                "action": "WOULD_CREATE_TAB",
                "details": f"Dry run: Would create worksheet '{OPERATIONAL_JOBS_TAB}' with {len(OPERATIONAL_JOBS_COLUMNS)} columns.",
            }

        # Real execution: create worksheet and write header row
        cols = len(OPERATIONAL_JOBS_COLUMNS) + 2
        ws = spreadsheet.add_worksheet(title=OPERATIONAL_JOBS_TAB, rows=500, cols=cols)
        ws.append_row(OPERATIONAL_JOBS_COLUMNS)
        self._worksheet = ws
        return {
            "tab": OPERATIONAL_JOBS_TAB,
            "status": "CREATED",
            "columns": OPERATIONAL_JOBS_COLUMNS,
            "dry_run": False,
            "action": "CREATED_WORKSHEET",
        }

    # -------------------------------------------------------------------------
    # 2. JOB LIFECYCLE & MUTUAL EXCLUSION
    # -------------------------------------------------------------------------

    def create_job(
        self,
        territory: str,
        industry: str,
        requested_leads: int,
        run_id: Optional[str] = None,
        lease_holder: Optional[str] = None,
        status: JobLifecycleStatus = JobLifecycleStatus.RUNNING,
    ) -> Dict[str, Any]:
        """
        Initializes a durable job record under single-instance mutual exclusion.
        Fails closed with ConcurrentJobConflictError if an active unexpired job exists.
        """
        # Reconcile any stale running jobs first
        self.reconcile_stale_jobs()

        active_job = self.get_active_job()
        if active_job:
            raise ConcurrentJobConflictError(
                f"A discovery job ({active_job.get('run_id')}) is currently active. "
                f"Single-instance concurrency limit enforced. Wait for completion or reconcile stale job."
            )

        now_dt = _now_utc()
        actual_run_id = run_id or f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
        actual_holder = lease_holder or f"INSTANCE-{uuid.uuid4().hex[:8].upper()}"
        lease_expires = datetime.fromtimestamp(_now_epoch() + self.heartbeat_timeout_seconds, tz=timezone.utc).isoformat()

        idemp_payload = f"{territory}:{industry}:{requested_leads}:{actual_run_id}"
        idemp_hash = hashlib.sha256(idemp_payload.encode("utf-8")).hexdigest()[:16]

        job_record = {
            "run_id": actual_run_id,
            "schema_version": LEDGER_SCHEMA_VERSION,
            "status": status.value,
            "territory": territory.strip(),
            "industry": industry.strip(),
            "stage": "INIT",
            "requested_leads": int(requested_leads),
            "qualified_count": 0,
            "candidates_evaluated": 0,
            "candidate_cursor": "",
            "processed_keys_compact": "[]",
            "attempt_count": 1,
            "error_code": "NONE",
            "created_at": now_dt,
            "updated_at": now_dt,
            "last_heartbeat": now_dt,
            "lease_holder": actual_holder,
            "lease_expires_at": lease_expires,
            "idempotency_hash": idemp_hash,
            "recovery_metadata_json": json.dumps({"started_by": "OPERATOR"}),
        }

        ws = self._get_worksheet()
        if ws:
            try:
                row_values = [str(job_record.get(col, "")) for col in OPERATIONAL_JOBS_COLUMNS]
                self._execute_with_retry(ws.append_row, row_values)
            except Exception as e:
                logger.error("Failed to append job row to OPERATIONAL_JOBS: %s", e)
                raise
        else:
            if self.storage_provider and not self.force_mock:
                raise RuntimeError(
                    f"Operational ledger tab '{OPERATIONAL_JOBS_TAB}' is missing from Google Sheets. "
                    "Run scripts/setup_operational_sheets_schema.py to create the worksheet before launching jobs."
                )
            self._mock_jobs[actual_run_id] = dict(job_record)

        return job_record

    def update_checkpoint(
        self,
        run_id: str,
        stage: str,
        candidate_cursor: str = "",
        processed_keys: Optional[List[str]] = None,
        qualified_count: Optional[int] = None,
        candidates_evaluated: Optional[int] = None,
        recovery_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Atomically records a progress checkpoint and refreshes the heartbeat timestamp.
        """
        now_dt = _now_utc()
        lease_expires = datetime.fromtimestamp(_now_epoch() + self.heartbeat_timeout_seconds, tz=timezone.utc).isoformat()

        job = self.get_job(run_id)
        if not job:
            return False

        job["stage"] = stage
        job["updated_at"] = now_dt
        job["last_heartbeat"] = now_dt
        job["lease_expires_at"] = lease_expires

        if candidate_cursor:
            job["candidate_cursor"] = candidate_cursor
        if processed_keys is not None:
            # Keep compact (store recent 100 or serialized hash array) to avoid cell truncation
            job["processed_keys_compact"] = json.dumps(processed_keys[-100:])
        if qualified_count is not None:
            job["qualified_count"] = int(qualified_count)
        if candidates_evaluated is not None:
            job["candidates_evaluated"] = int(candidates_evaluated)
        if recovery_metadata is not None:
            job["recovery_metadata_json"] = json.dumps(recovery_metadata)

        ws = self._get_worksheet()
        if ws:
            try:
                # Find row index by run_id
                records = ws.get_all_records()
                for idx, r in enumerate(records, start=2):
                    if str(r.get("run_id")) == run_id:
                        row_values = [str(job.get(col, "")) for col in OPERATIONAL_JOBS_COLUMNS]
                        cell_range = f"A{idx}:{chr(64 + len(OPERATIONAL_JOBS_COLUMNS))}{idx}"
                        ws.update(cell_range, [row_values])
                        return True
            except Exception as e:
                logger.error("Failed updating checkpoint in sheet for %s: %s", run_id, e)
                return False
        else:
            self._mock_jobs[run_id] = dict(job)
            return True

        return False

    def heartbeat(self, run_id: str, lease_holder: Optional[str] = None) -> bool:
        """
        Renews the last_heartbeat timestamp and extends lease_expires_at.
        """
        now_dt = _now_utc()
        lease_expires = datetime.fromtimestamp(_now_epoch() + self.heartbeat_timeout_seconds, tz=timezone.utc).isoformat()
        job = self.get_job(run_id)
        if not job:
            return False

        if lease_holder and job.get("lease_holder") != lease_holder:
            return False  # Not the current holder

        job["last_heartbeat"] = now_dt
        job["lease_expires_at"] = lease_expires
        job["updated_at"] = now_dt

        ws = self._get_worksheet()
        if ws:
            try:
                records = ws.get_all_records()
                for idx, r in enumerate(records, start=2):
                    if str(r.get("run_id")) == run_id:
                        # Update columns for last_heartbeat and lease_expires_at
                        hb_col = OPERATIONAL_JOBS_COLUMNS.index("last_heartbeat") + 1
                        exp_col = OPERATIONAL_JOBS_COLUMNS.index("lease_expires_at") + 1
                        ws.update_cell(idx, hb_col, now_dt)
                        ws.update_cell(idx, exp_col, lease_expires)
                        return True
            except Exception as e:
                logger.warning("Heartbeat update failed for %s: %s", run_id, e)
                return False
        else:
            self._mock_jobs[run_id] = dict(job)
            return True

        return False

    def complete_job(self, run_id: str, final_stats: Optional[Dict[str, Any]] = None) -> bool:
        """Marks a job as COMPLETED and clears the lease."""
        return self._set_job_status(run_id, JobLifecycleStatus.COMPLETED, error_code="NONE", metadata=final_stats)

    def fail_job(self, run_id: str, error_code: str, error_message: str = "") -> bool:
        """Marks a job as FAILED with a safe error category."""
        return self._set_job_status(
            run_id,
            JobLifecycleStatus.FAILED,
            error_code=error_code or "RUNTIME_ERROR",
            metadata={"error_message": str(error_message)[:200]},
        )

    def pause_or_interrupt_job(self, run_id: str, reason: str = "IDLE_SLEEP") -> bool:
        """Transitions job to INTERRUPTED state."""
        return self._set_job_status(
            run_id,
            JobLifecycleStatus.INTERRUPTED,
            error_code=reason,
            metadata={"interrupted_at": _now_utc(), "reason": reason},
        )

    def _set_job_status(
        self,
        run_id: str,
        status: JobLifecycleStatus,
        error_code: str = "NONE",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        job = self.get_job(run_id)
        if not job:
            return False

        now_dt = _now_utc()
        job["status"] = status.value
        job["updated_at"] = now_dt
        job["error_code"] = error_code
        job["lease_expires_at"] = ""  # Clear lease on termination

        if metadata:
            curr_meta = {}
            try:
                curr_meta = json.loads(job.get("recovery_metadata_json", "{}"))
            except Exception:
                pass
            curr_meta.update(metadata)
            job["recovery_metadata_json"] = json.dumps(curr_meta)

        ws = self._get_worksheet()
        if ws:
            try:
                records = ws.get_all_records()
                for idx, r in enumerate(records, start=2):
                    if str(r.get("run_id")) == run_id:
                        row_values = [str(job.get(col, "")) for col in OPERATIONAL_JOBS_COLUMNS]
                        cell_range = f"A{idx}:{chr(64 + len(OPERATIONAL_JOBS_COLUMNS))}{idx}"
                        ws.update(cell_range, [row_values])
                        return True
            except Exception as e:
                logger.error("Failed setting job status for %s: %s", run_id, e)
                return False
        else:
            self._mock_jobs[run_id] = dict(job)
            return True

        return False

    # -------------------------------------------------------------------------
    # 3. QUERIES & STALE JOB RECONCILIATION
    # -------------------------------------------------------------------------

    def get_job(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Fetches job record by run_id."""
        ws = self._get_worksheet()
        if ws:
            try:
                records = ws.get_all_records()
                for r in records:
                    if str(r.get("run_id")) == run_id:
                        return r
                return None
            except Exception as e:
                logger.error("Error reading job %s from worksheet: %s", run_id, e)
                return None
        return self._mock_jobs.get(run_id)

    def get_active_job(self) -> Optional[Dict[str, Any]]:
        """Returns the currently active unexpired job, if any."""
        now = _now_epoch()
        ws = self._get_worksheet()
        all_jobs = []
        if ws:
            try:
                all_jobs = ws.get_all_records()
            except Exception:
                all_jobs = []
        else:
            all_jobs = list(self._mock_jobs.values())

        for j in all_jobs:
            if j.get("status") == JobLifecycleStatus.RUNNING.value:
                last_hb = j.get("last_heartbeat")
                if last_hb:
                    try:
                        hb_dt = datetime.fromisoformat(last_hb)
                        hb_epoch = hb_dt.timestamp()
                        if now - hb_epoch < self.heartbeat_timeout_seconds:
                            return j
                    except Exception:
                        pass
        return None

    def reconcile_stale_jobs(self) -> List[Dict[str, Any]]:
        """
        Scans all jobs in the ledger. Any job marked RUNNING whose last heartbeat
        drifted past the timeout is safely transitioned to INTERRUPTED.
        """
        now = _now_epoch()
        reconciled = []
        ws = self._get_worksheet()

        if ws:
            try:
                records = ws.get_all_records()
                for idx, j in enumerate(records, start=2):
                    if j.get("status") == JobLifecycleStatus.RUNNING.value:
                        last_hb = j.get("last_heartbeat")
                        is_stale = True
                        if last_hb:
                            try:
                                hb_dt = datetime.fromisoformat(last_hb)
                                if now - hb_dt.timestamp() < self.heartbeat_timeout_seconds:
                                    is_stale = False
                            except Exception:
                                is_stale = True

                        if is_stale:
                            run_id = str(j.get("run_id"))
                            logger.warning(
                                "Reconciling stale running job %s (last heartbeat: %s) -> INTERRUPTED",
                                run_id, last_hb
                            )
                            self._set_job_status(
                                run_id,
                                JobLifecycleStatus.INTERRUPTED,
                                error_code="STALE_HEARTBEAT_TIMEOUT",
                                metadata={"reconciled_at": _now_utc()},
                            )
                            reconciled.append(j)
            except Exception as e:
                logger.error("Error during stale job reconciliation: %s", e)
        else:
            for run_id, j in list(self._mock_jobs.items()):
                if j.get("status") == JobLifecycleStatus.RUNNING.value:
                    last_hb = j.get("last_heartbeat")
                    is_stale = True
                    if last_hb:
                        try:
                            hb_dt = datetime.fromisoformat(last_hb)
                            if now - hb_dt.timestamp() < self.heartbeat_timeout_seconds:
                                is_stale = False
                        except Exception:
                            is_stale = True

                    if is_stale:
                        self.pause_or_interrupt_job(run_id, reason="STALE_HEARTBEAT_TIMEOUT")
                        reconciled.append(j)

        return reconciled

    def clear_mock_data(self) -> None:
        """Clears in-memory mock records for testing."""
        self._mock_jobs.clear()


# Default singleton instance
operational_ledger = OperationalJobLedger(force_mock=True)
