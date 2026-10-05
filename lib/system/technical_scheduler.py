"""
lib/system/technical_scheduler.py
=================================
Configurable technical operations scheduler.
Automates recurring technical tasks without commercial sends:
  DAILY:
    - CRM reconciliation
    - data-quality audit
    - analytics snapshot
    - backup
  EVERY 2 DAYS:
    - technical enrichment refresh
  WEEKLY:
    - stale lead audit
    - identity integrity audit
    - backup recovery verification
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from lib.system.atomic_writer import atomic_write_json
from lib.system.system_config import SystemConfig
from lib.system.technical_orchestrator import TechnicalOrchestrator

logger = logging.getLogger("TechnicalScheduler")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DEFAULT_SCHEDULER_FILE = os.path.join(DATA_DIR, ".scheduler_state.json")

DEFAULT_SCHEDULE = [
    {
        "task_id": "DAILY_BACKUP",
        "job_type": "RUN_BACKUP",
        "interval_hours": 24,
        "description": "Daily snapshot of all critical state stores with SHA-256 manifest",
        "market_id": "MANCHESTER_UK",
        "enabled": True,
    },
    {
        "task_id": "DAILY_CRM_RECONCILIATION",
        "job_type": "RUN_RECONCILIATION",
        "interval_hours": 24,
        "description": "Daily audit for multi-store contradictions",
        "market_id": "MANCHESTER_UK",
        "enabled": True,
    },
    {
        "task_id": "DAILY_DATA_QUALITY_AUDIT",
        "job_type": "RUN_DATA_QUALITY",
        "interval_hours": 24,
        "description": "Daily identity integrity and canonical lead ID validation",
        "market_id": "MANCHESTER_UK",
        "enabled": True,
    },
    {
        "task_id": "DAILY_ANALYTICS_SNAPSHOT",
        "job_type": "RUN_ANALYTICS",
        "interval_hours": 24,
        "description": "Daily deterministic regeneration of outreach performance snapshot",
        "market_id": "MANCHESTER_UK",
        "enabled": True,
    },
    {
        "task_id": "BI_DAILY_ENRICHMENT_REFRESH",
        "job_type": "RUN_REVIEW_REFRESH",
        "interval_hours": 48,
        "description": "Bi-daily review and operational freshness refresh",
        "market_id": "MANCHESTER_UK",
        "enabled": True,
    },
    {
        "task_id": "WEEKLY_CONTACT_AUDIT",
        "job_type": "RUN_CONTACTABILITY_REFRESH",
        "interval_hours": 168,
        "description": "Weekly verification of contact channels and MX records",
        "market_id": "MANCHESTER_UK",
        "enabled": True,
    },
]


class TechnicalScheduler:
    """
    Local scheduling manager for automated unattended operations.
    """

    def __init__(
        self,
        state_file: Optional[str] = None,
        orchestrator: Optional[TechnicalOrchestrator] = None,
        load_defaults: bool = True,
    ):
        self.orchestrator = orchestrator or TechnicalOrchestrator()
        if not state_file:
            if orchestrator and hasattr(orchestrator, "data_dir") and orchestrator.data_dir != DATA_DIR:
                self.state_file = os.path.join(orchestrator.data_dir, ".scheduler_state.json")
                load_defaults = False
            else:
                self.state_file = DEFAULT_SCHEDULER_FILE
        else:
            self.state_file = state_file
        self.load_defaults = load_defaults
        self._ensure_state_file()

    def _ensure_state_file(self) -> None:
        if not os.path.exists(self.state_file):
            initial_state = {
                "tasks": DEFAULT_SCHEDULE if self.load_defaults else [],
                "last_run_timestamps": {},
                "scheduler_active": True,
            }
            atomic_write_json(self.state_file, initial_state)

    def _load_state(self) -> Dict[str, Any]:
        with open(self.state_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_state(self, state: Dict[str, Any]) -> None:
        atomic_write_json(self.state_file, state)

    def get_schedule(self) -> List[Dict[str, Any]]:
        state = self._load_state()
        return state.get("tasks", DEFAULT_SCHEDULE)

    def get_due_tasks(self) -> List[Dict[str, Any]]:
        """Identifies all scheduled tasks whose interval has elapsed since last run."""
        state = self._load_state()
        tasks = state.get("tasks", DEFAULT_SCHEDULE)
        last_runs = state.get("last_run_timestamps", {})
        now = datetime.now(timezone.utc)

        due = []
        for t in tasks:
            if not t.get("enabled", True):
                continue

            tid = t["task_id"]
            last_ts_str = last_runs.get(tid)
            if not last_ts_str:
                due.append(t)
                continue

            try:
                last_dt = datetime.fromisoformat(last_ts_str)
                interval = timedelta(hours=t.get("interval_hours", 24))
                if (now - last_dt) >= interval:
                    due.append(t)
            except Exception:
                due.append(t)

        return due

    def run_due_tasks(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Executes all due tasks through the TechnicalOrchestrator.
        """
        if not SystemConfig.TECHNICAL_AUTOMATION_ENABLED:
            return {
                "status": "DISABLED",
                "message": "Technical automation is disabled by SystemConfig.",
                "executed_count": 0,
            }

        due = self.get_due_tasks()
        state = self._load_state()
        last_runs = state.get("last_run_timestamps", {})

        results = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for task in due:
            tid = task["task_id"]
            jtype = task["job_type"]
            mkt = task.get("market_id", "MANCHESTER_UK")

            try:
                run_res = self.orchestrator.trigger_job(
                    job_type=jtype,
                    market_id=mkt,
                    dry_run=dry_run,
                )
                if not dry_run:
                    last_runs[tid] = now_iso
                results.append({
                    "task_id": tid,
                    "job_type": jtype,
                    "status": run_res.get("status"),
                    "run_id": run_res.get("run_id"),
                })
            except Exception as e:
                logger.error(f"Failed executing scheduled task '{tid}': {e}")
                results.append({
                    "task_id": tid,
                    "job_type": jtype,
                    "status": "FAILED",
                    "error": str(e),
                })

        state["last_run_timestamps"] = last_runs
        if not dry_run:
            self._save_state(state)

        return {
            "status": "COMPLETED",
            "executed_count": len(results),
            "results": results,
        }

    def register_task(
        self,
        task_id: str,
        job_type: str,
        interval_seconds: int = 86400,
        description: str = "",
        market_id: str = "MANCHESTER_UK",
        enabled: bool = True,
    ) -> Dict[str, Any]:
        state = self._load_state()
        tasks = state.get("tasks", [])
        tasks = [t for t in tasks if t.get("task_id") != task_id]
        new_task = {
            "task_id": task_id,
            "job_type": job_type,
            "interval_seconds": interval_seconds,
            "interval_hours": interval_seconds / 3600.0,
            "description": description,
            "market_id": market_id,
            "enabled": enabled,
        }
        tasks.append(new_task)
        state["tasks"] = tasks
        self._save_state(state)
        return new_task

    def get_status(self) -> Dict[str, Any]:
        """Returns structured scheduler status for system health reports."""
        state = self._load_state()
        due = self.get_due_tasks()
        return {
            "scheduler_enabled": SystemConfig.TECHNICAL_AUTOMATION_ENABLED,
            "tasks_count": len(state.get("tasks", [])),
            "tasks": state.get("tasks", []),
            "due_tasks_count": len(due),
            "due_tasks": [d["task_id"] for d in due],
            "last_run_timestamps": state.get("last_run_timestamps", {}),
        }

