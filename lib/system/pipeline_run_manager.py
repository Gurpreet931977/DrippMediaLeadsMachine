"""
lib/system/pipeline_run_manager.py
==================================
Durable Run Record and Lifecycle Manager for Lead Discovery Pipeline.
Fulfills Phase Temp requirements:
  - Durable persistence to data/pipeline_runs/{run_id}.json
  - Safe execution lifecycle: QUEUED, RUNNING, COMPLETED, COMPLETED_WITH_ERRORS, FAILED, CANCELLED
  - Honest real metrics: Discovered, Researched, Qualified, Manual-Review, Rejected, Duplicates, Failures, CRM outcomes, Start/End time
  - Server restart recovery: Interrupted runs automatically transitioned to FAILED (never falsely active or completed)
  - Dry-run / Shadow mode awareness: Bypasses CRM writes safely when enabled
"""

import os
import json
import time
import uuid
from enum import Enum
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
RUNS_DIR = os.path.join(DATA_DIR, "pipeline_runs")
LATEST_RUN_FILE = os.path.join(RUNS_DIR, "latest_run.json")


class PipelineRunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DurablePipelineRunManager:
    """
    Manages durable run records on disk with atomic serialization and restart recovery.
    """
    def __init__(self, runs_dir: Optional[str] = None):
        self.runs_dir = runs_dir or RUNS_DIR
        os.makedirs(self.runs_dir, exist_ok=True)
        self.latest_file = os.path.join(self.runs_dir, "latest_run.json")
        self._active_run_id: Optional[str] = None
        self._cancellation_requested = False

    def _get_run_path(self, run_id: str) -> str:
        return os.path.join(self.runs_dir, f"{run_id}.json")

    def _save_record(self, record: Dict[str, Any]) -> None:
        run_id = record.get("run_id")
        if not run_id:
            return
        run_path = self._get_run_path(run_id)
        tmp_path = f"{run_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2)
            os.replace(tmp_path, run_path)

            # Also update latest pointer
            tmp_latest = f"{self.latest_file}.tmp"
            with open(tmp_latest, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2)
            os.replace(tmp_latest, self.latest_file)
        except Exception as e:
            print(f"[RunManager] Failed to persist run record {run_id}: {e}")

    def create_run(
        self,
        country: str,
        cities: List[str],
        industry: str,
        qualified_leads_needed: int,
        batch_size: int = 10,
        max_research_multiplier: int = 5,
        discovery_mode: str = "HYBRID",
        apify_enabled: bool = False,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """Creates a new durable run record in QUEUED state."""
        now_dt = datetime.now(timezone.utc)
        time_str = now_dt.strftime("%Y%m%d_%H%M%S")
        short_id = uuid.uuid4().hex[:6].upper()
        run_id = f"RUN-{time_str}-{short_id}"

        target_market = f"{cities[0] if cities else 'Manchester'}, {country}"

        record: Dict[str, Any] = {
            "run_id": run_id,
            "status": PipelineRunStatus.QUEUED.value,
            "created_at": now_dt.isoformat(),
            "started_at": None,
            "finished_at": None,
            "elapsed_seconds": 0.0,
            "market": target_market,
            "country": country,
            "cities": cities,
            "industry": industry,
            "qualified_leads_needed": qualified_leads_needed,
            "batch_size": batch_size,
            "max_research_multiplier": max_research_multiplier,
            "discovery_mode": discovery_mode,
            "apify_enabled": apify_enabled,
            "dry_run": dry_run,
            "is_running": False,
            "results": {
                "candidates_discovered": 0,
                "candidates_researched": 0,
                "qualified_leads": 0,
                "manual_review_leads": 0,
                "rejected_leads": 0,
                "duplicates_skipped": 0,
                "provider_failures": 0,
                "quota_exhaustion": None,
                "crm_reconciliation": {
                    "saved_to_leads": 0,
                    "saved_to_review_queue": 0,
                    "saved_to_research_log": 0,
                    "mode": "SHADOW_MODE_DRY_RUN" if dry_run else "LIVE_GOOGLE_SHEETS_CRM",
                    "reconciled": False
                },
                "final_status": PipelineRunStatus.QUEUED.value
            },
            "discovery_transparency": {
                "businesses_discovered_total": 0,
                "unique_candidates": 0,
                "duplicates_merged": 0,
                "by_source": {
                    "OPENSTREETMAP": 0,
                    "FOURSQUARE": 0,
                    "WEB_SEARCH": 0,
                    "CREATOR_REFERENCES": 0,
                    "APIFY": 0
                }
            },
            "current_stats": {
                "requested_qualified_leads": qualified_leads_needed,
                "raw_discovered": 0,
                "businesses_researched": 0,
                "website_exists": 0,
                "no_website_confirmed": 0,
                "not_qualified": 0,
                "duplicates": 0,
                "qualified_leads": 0,
                "manual_review": 0,
                "outreach_ready": 0,
                "saved_to_leads": 0,
                "saved_to_review_queue": 0,
                "saved_to_research_log": 0
            },
            "progress_logs": [
                f"[QUEUED] Pipeline run {run_id} initialized for {target_market} ({industry}).",
                f"[CONFIG] Target: {qualified_leads_needed} leads | Batch Size: {batch_size} | Mode: {discovery_mode} | Dry-Run: {dry_run}"
            ],
            "last_error": None
        }

        self._active_run_id = run_id
        self._cancellation_requested = False
        self._save_record(record)
        return record

    def start_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Transitions run to RUNNING state."""
        record = self.get_run(run_id)
        if not record:
            return None
        record["status"] = PipelineRunStatus.RUNNING.value
        record["started_at"] = datetime.now(timezone.utc).isoformat()
        record["is_running"] = True
        record["progress_logs"].append(f"[RUNNING] Pipeline execution started at {record['started_at']}")
        self._active_run_id = run_id
        self._save_record(record)
        return record

    def append_log(self, run_id: str, msg: str, stats: Optional[Dict[str, Any]] = None) -> None:
        """Appends log message and updates live intermediate stats."""
        record = self.get_run(run_id)
        if not record:
            return
        record["progress_logs"].append(msg)
        if len(record["progress_logs"]) > 500:
            record["progress_logs"].pop(0)

        if stats:
            record["current_stats"].update(stats)
            raw = stats.get("raw_discovered", record["current_stats"].get("raw_discovered", 0))
            researched = stats.get("businesses_researched", stats.get("valid_candidates_researched", 0))
            qualified = stats.get("qualified_leads", stats.get("outreach_ready", 0))
            manual_rev = stats.get("manual_review", 0)
            rejected = stats.get("not_qualified", stats.get("excluded", 0))
            dups = stats.get("duplicates", 0)

            record["results"]["candidates_discovered"] = raw
            record["results"]["candidates_researched"] = researched
            record["results"]["qualified_leads"] = qualified
            record["results"]["manual_review_leads"] = manual_rev
            record["results"]["rejected_leads"] = rejected
            record["results"]["duplicates_skipped"] = dups

        self._save_record(record)

    def finish_run(
        self,
        run_id: str,
        pipeline_result: Dict[str, Any],
        has_warnings_or_errors: bool = False
    ) -> Dict[str, Any]:
        """Finalizes run as COMPLETED or COMPLETED_WITH_ERRORS."""
        record = self.get_run(run_id)
        if not record:
            return {}

        now_dt = datetime.now(timezone.utc)
        record["finished_at"] = now_dt.isoformat()
        record["is_running"] = False

        if record.get("started_at"):
            try:
                start_dt = datetime.fromisoformat(record["started_at"])
                record["elapsed_seconds"] = round((now_dt - start_dt).total_seconds(), 2)
            except Exception:
                record["elapsed_seconds"] = pipeline_result.get("elapsed_seconds", 0.0)
        else:
            record["elapsed_seconds"] = pipeline_result.get("elapsed_seconds", 0.0)

        stats = pipeline_result.get("stats", {})
        transparency = pipeline_result.get("discovery_transparency", {})
        query_stats = pipeline_result.get("query_stats", {})

        record["discovery_transparency"] = transparency
        record["current_stats"].update(stats)

        # Honest result metrics
        discovered = stats.get("raw_discovered", transparency.get("businesses_discovered_total", 0))
        researched = stats.get("valid_candidates_researched", stats.get("businesses_researched", 0))
        qualified = stats.get("outreach_ready", stats.get("qualified_leads", len(pipeline_result.get("leads", []))))
        manual_rev = stats.get("manual_review", len(pipeline_result.get("review_queue", [])))
        rejected = stats.get("not_qualified", stats.get("excluded", 0))
        duplicates = stats.get("duplicates", transparency.get("duplicates_merged", 0))

        # Check provider errors
        provider_failures = 0
        quota_exhaustion = None
        if stats.get("failures_count", 0) > 0:
            provider_failures = stats["failures_count"]

        # CRM outcome
        is_dry = record.get("dry_run", False)
        crm_saved_leads = 0 if is_dry else stats.get("saved_to_leads", len(pipeline_result.get("leads", [])))
        crm_saved_rev = 0 if is_dry else stats.get("saved_to_review_queue", len(pipeline_result.get("review_queue", [])))
        crm_saved_res = 0 if is_dry else stats.get("saved_to_research_log", len(pipeline_result.get("research_entries", [])))

        crm_reconciliation = {
            "saved_to_leads": crm_saved_leads,
            "saved_to_review_queue": crm_saved_rev,
            "saved_to_research_log": crm_saved_res,
            "mode": "SHADOW_MODE_DRY_RUN" if is_dry else "LIVE_GOOGLE_SHEETS_CRM",
            "reconciled": True
        }

        final_status = (
            PipelineRunStatus.COMPLETED_WITH_ERRORS.value
            if (has_warnings_or_errors or provider_failures > 0)
            else PipelineRunStatus.COMPLETED.value
        )

        record["status"] = final_status
        record["results"] = {
            "candidates_discovered": discovered,
            "candidates_researched": researched,
            "qualified_leads": qualified,
            "manual_review_leads": manual_rev,
            "rejected_leads": rejected,
            "duplicates_skipped": duplicates,
            "provider_failures": provider_failures,
            "quota_exhaustion": quota_exhaustion,
            "crm_reconciliation": crm_reconciliation,
            "final_status": final_status
        }

        record["progress_logs"].append(
            f"[{final_status}] Run concluded in {record['elapsed_seconds']}s. "
            f"Yield: {qualified} qualified, {manual_rev} manual-review, {discovered} raw discovered."
        )

        self._active_run_id = None
        self._save_record(record)
        return record

    def fail_run(self, run_id: str, error_msg: str) -> Dict[str, Any]:
        """Marks run as FAILED with error message."""
        record = self.get_run(run_id)
        if not record:
            return {}

        now_dt = datetime.now(timezone.utc)
        record["finished_at"] = now_dt.isoformat()
        record["status"] = PipelineRunStatus.FAILED.value
        record["is_running"] = False
        record["last_error"] = error_msg
        record["results"]["final_status"] = PipelineRunStatus.FAILED.value
        record["progress_logs"].append(f"[FATAL_ERROR] Run aborted: {error_msg}")

        self._active_run_id = None
        self._save_record(record)
        return record

    def cancel_run(self, run_id: str) -> Dict[str, Any]:
        """Marks run as CANCELLED safely."""
        record = self.get_run(run_id)
        if not record:
            return {}

        now_dt = datetime.now(timezone.utc)
        record["finished_at"] = now_dt.isoformat()
        record["status"] = PipelineRunStatus.CANCELLED.value
        record["is_running"] = False
        record["results"]["final_status"] = PipelineRunStatus.CANCELLED.value
        record["progress_logs"].append(f"[CANCELLED] Operator requested run cancellation at {now_dt.isoformat()}")

        self._active_run_id = None
        self._cancellation_requested = True
        self._save_record(record)
        return record

    def is_cancellation_requested(self) -> bool:
        return self._cancellation_requested

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Loads a specific run record from disk."""
        path = self._get_run_path(run_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def get_latest_run(self) -> Optional[Dict[str, Any]]:
        """Loads the most recent run record from latest_run.json."""
        if os.path.exists(self.latest_file):
            try:
                with open(self.latest_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        # Fallback to inspecting all run files
        runs = self.list_runs(limit=1)
        return runs[0] if runs else None

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns list of runs sorted newest first."""
        records: List[Dict[str, Any]] = []
        if not os.path.exists(self.runs_dir):
            return []
        for fname in os.listdir(self.runs_dir):
            if fname.startswith("RUN-") and fname.endswith(".json"):
                fpath = os.path.join(self.runs_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        rec = json.load(f)
                        records.append(rec)
                except Exception:
                    continue

        records.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return records[:limit]

    def recover_interrupted_runs(self) -> int:
        """
        Scans durable runs on server startup.
        Any run left in RUNNING or QUEUED state is marked FAILED with honest error:
        'Run was interrupted by server process restart'.
        Guarantees the UI never falsely displays an interrupted run as active or completed.
        """
        recovered_count = 0
        if not os.path.exists(self.runs_dir):
            return 0

        for fname in os.listdir(self.runs_dir):
            if fname.startswith("RUN-") and fname.endswith(".json"):
                fpath = os.path.join(self.runs_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        rec = json.load(f)
                    
                    st = rec.get("status")
                    if st in (PipelineRunStatus.RUNNING.value, PipelineRunStatus.QUEUED.value):
                        now_dt = datetime.now(timezone.utc).isoformat()
                        rec["status"] = PipelineRunStatus.FAILED.value
                        rec["is_running"] = False
                        rec["finished_at"] = now_dt
                        rec["last_error"] = "Run was interrupted by server process restart"
                        rec["results"]["final_status"] = PipelineRunStatus.FAILED.value
                        rec["progress_logs"].append(
                            f"[RECOVERY] Process restart detected at {now_dt}. Interrupted run transitioned to FAILED."
                        )
                        with open(fpath, "w", encoding="utf-8") as f:
                            json.dump(rec, f, indent=2)
                        recovered_count += 1
                except Exception as e:
                    print(f"[RunManager Recovery Error] {fname}: {e}")

        # Sync latest run if needed
        latest = self.list_runs(limit=1)
        if latest:
            try:
                with open(self.latest_file, "w", encoding="utf-8") as f:
                    json.dump(latest[0], f, indent=2)
            except Exception:
                pass

        if recovered_count > 0:
            print(f"[RunManager] Successfully recovered and marked {recovered_count} interrupted run(s) as FAILED.")
        return recovered_count


# Global singleton instance
run_manager = DurablePipelineRunManager()
PipelineRunManager = DurablePipelineRunManager
