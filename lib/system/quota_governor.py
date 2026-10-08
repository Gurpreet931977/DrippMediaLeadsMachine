"""
lib/system/quota_governor.py
============================
Central Resource Governance & Quota Enforcement Engine.
Enforces the mandatory Phase 10.1 invariant:
  Every scheduled technical process must consume through one central quota mechanism.
  Never allow an individual subsystem to bypass quotas.
  If a quota is exhausted: PAUSE rather than OVERRIDE.
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from lib.system.file_lock import quota_lock
from lib.system.atomic_writer import atomic_write_json

logger = logging.getLogger("QuotaGovernor")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DEFAULT_QUOTA_FILE = os.path.join(DATA_DIR, ".quota_state.json")

DEFAULT_DAILY_LIMITS = {
    "search_calls": 500,
    "gosom_calls": 10,
    "enrichment_calls": 50,
    "crm_writes": 100,
}


class QuotaExhaustedError(RuntimeError):
    """Raised when an operation would exceed the central quota allocation."""

    def __init__(self, resource: str, current: int, limit: int, requested: int):
        self.resource = resource
        self.current = current
        self.limit = limit
        self.requested = requested
        super().__init__(
            f"Quota exhausted for resource '{resource}': used {current}/{limit} units. "
            f"Requested {requested} units. System PAUSED to prevent quota overrun."
        )


class LimitsProxy(dict):
    """Proxy dict that automatically persists changes back to QuotaGovernor."""
    def __init__(self, governor, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.governor = governor

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.governor.set_limits({key: value})


class QuotaGovernor:
    """
    Thread-safe and process-safe central resource quota governor.
    """

    def __init__(
        self,
        state_file: Optional[str] = None,
        quota_path: Optional[str] = None,
        lock_dir: Optional[str] = None,
        data_dir: Optional[str] = None,
    ):
        self.lock_dir = lock_dir or data_dir
        actual_path = quota_path or state_file
        if not actual_path and (data_dir or lock_dir):
            base = data_dir or lock_dir
            self.state_file = os.path.join(base, ".quota_state.json")
        else:
            self.state_file = actual_path or DEFAULT_QUOTA_FILE
        self._ensure_state_file()

    def _ensure_state_file(self) -> None:
        if not os.path.exists(self.state_file):
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            initial_state = {
                "date": today,
                "limits": dict(DEFAULT_DAILY_LIMITS),
                "usage": {k: 0 for k in DEFAULT_DAILY_LIMITS},
                "history": [],
            }
            atomic_write_json(self.state_file, initial_state)

    def _load_state(self) -> Dict[str, Any]:
        with open(self.state_file, "r", encoding="utf-8") as f:
            state = json.load(f)

        # Check date rollover
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("date") != today:
            if "history" not in state or not isinstance(state["history"], list):
                state["history"] = []
            state["history"].append({
                "date": state.get("date"),
                "usage": state.get("usage", {}),
            })
            state["date"] = today
            state["usage"] = {k: 0 for k in state.get("limits", DEFAULT_DAILY_LIMITS)}
            atomic_write_json(self.state_file, state)

        return state

    def check_quota(self, resource: str, units: int = 1) -> bool:
        """Checks if resource has remaining units without consuming."""
        with quota_lock(lock_dir=self.lock_dir):
            state = self._load_state()
            limits = state.get("limits", DEFAULT_DAILY_LIMITS)
            usage = state.get("usage", {})
            limit = limits.get(resource, 0)
            current = usage.get(resource, 0)
            return (current + units) <= limit

    def consume_quota(self, resource: str, units: int = 1, run_id: Optional[str] = None) -> int:
        """
        Consumes units of a resource under lock.
        Raises QuotaExhaustedError if consumption exceeds daily limit.
        """
        with quota_lock(lock_dir=self.lock_dir):
            state = self._load_state()
            limits = state.get("limits", DEFAULT_DAILY_LIMITS)
            usage = state.get("usage", {})

            limit = limits.get(resource, 0)
            current = usage.get(resource, 0)

            if (current + units) > limit:
                logger.warning(
                    f"QuotaExhausted: {resource} requested {units}, current {current}, limit {limit} [run_id={run_id}]"
                )
                raise QuotaExhaustedError(
                    resource=resource,
                    current=current,
                    limit=limit,
                    requested=units,
                )

            usage[resource] = current + units
            state["usage"] = usage
            state["last_consumed_at"] = datetime.now(timezone.utc).isoformat()
            if run_id:
                state["last_run_id"] = run_id

            atomic_write_json(self.state_file, state)
            return usage[resource]

    def get_quota_status(self) -> Dict[str, Any]:
        """Returns structured dictionary of quota usage, limits, and remaining budget."""
        with quota_lock(lock_dir=self.lock_dir):
            state = self._load_state()
            limits = state.get("limits", DEFAULT_DAILY_LIMITS)
            usage = state.get("usage", {})

            status = {}
            for res, lim in limits.items():
                used = usage.get(res, 0)
                remaining = max(0, lim - used)
                pct = round((used / lim * 100), 1) if lim > 0 else 100.0
                status[res] = {
                    "limit": lim,
                    "used": used,
                    "remaining": remaining,
                    "percent_used": pct,
                    "exhausted": remaining <= 0,
                }

            return {
                "date": state.get("date"),
                "status": "HEALTHY" if not any(s["exhausted"] for s in status.values()) else "QUOTA_EXHAUSTED",
                "resources": status,
                "quotas": status,
            }

    @property
    def limits(self) -> Dict[str, int]:
        with quota_lock(lock_dir=self.lock_dir):
            state = self._load_state()
            lims = state.get("limits", dict(DEFAULT_DAILY_LIMITS))
            return LimitsProxy(self, lims)

    def consume(self, resource: str, units: int = 1, run_id: Optional[str] = None) -> int:
        return self.consume_quota(resource, units=units, run_id=run_id)

    def check_and_consume(self, resource: str, units: int = 1, run_id: Optional[str] = None) -> int:
        return self.consume_quota(resource, units=units, run_id=run_id)

    def can_consume(self, resource: str, units: int = 1) -> bool:
        return self.check_quota(resource, units=units)

    def reset_quota(self, resource: Optional[str] = None) -> None:
        with quota_lock(lock_dir=self.lock_dir):
            state = self._load_state()
            if resource:
                state.setdefault("usage", {})[resource] = 0
            else:
                state["usage"] = {k: 0 for k in state.get("limits", DEFAULT_DAILY_LIMITS)}
            atomic_write_json(self.state_file, state)

    def set_limits(self, limits: Dict[str, int]) -> None:
        """Updates resource limits under lock."""
        with quota_lock(lock_dir=self.lock_dir):
            state = self._load_state()
            state.setdefault("limits", {}).update(limits)
            for res in limits:
                state.setdefault("usage", {}).setdefault(res, 0)
            atomic_write_json(self.state_file, state)

    def reset_daily_quotas(self) -> None:
        """Resets all usage counters for the current day."""
        self.reset_quota()
