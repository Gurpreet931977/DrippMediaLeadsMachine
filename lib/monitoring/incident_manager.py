"""
lib/monitoring/incident_manager.py
==================================
Central Incident and Alert Manager for Phase 10.3 Technical Monitoring & Alerts.
Implements:
  1. Central singleton / managed incident coordinator.
  2. Deterministic fingerprinting to prevent alert flooding.
  3. Occurrence incrementation and state transitions (OPEN -> RESOLVED).
  4. Cooldown-based alert throttling.
  5. Concurrency-safe atomic persistence with FileLock in data/incidents.json.
  6. Multi-channel alert dispatch with non-fatal execution safety.
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from lib.monitoring.incident_types import (
    Incident,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    compute_deterministic_fingerprint,
)
from lib.monitoring.notification_adapters import (
    BaseNotificationAdapter,
    WebhookNotificationAdapter,
    EmailNotificationAdapter,
)
from lib.system.atomic_writer import atomic_write_json
from lib.system.file_lock import FileLock
from lib.system.system_config import SystemConfig

logger = logging.getLogger("IncidentManager")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DEFAULT_INCIDENTS_FILE = os.path.join(DATA_DIR, "incidents.json")


class IncidentManager:
    """
    Central Alert & Incident Coordinator for Dripp Media Technical Operations.
    """

    def __init__(
        self,
        incidents_file: Optional[str] = None,
        data_dir: Optional[str] = None,
        adapters: Optional[List[BaseNotificationAdapter]] = None,
        warning_cooldown_seconds: Optional[int] = None,
        critical_cooldown_seconds: Optional[int] = None,
    ):
        self.data_dir = data_dir or DATA_DIR
        self.incidents_file = incidents_file or os.path.join(self.data_dir, "incidents.json")
        self.warning_cooldown_seconds = warning_cooldown_seconds or int(os.getenv("ALERT_COOLDOWN_SECONDS", "3600"))
        self.critical_cooldown_seconds = critical_cooldown_seconds or int(os.getenv("CRITICAL_ALERT_COOLDOWN_SECONDS", "900"))

        if adapters is not None:
            self.adapters = adapters
        else:
            self.adapters = [
                WebhookNotificationAdapter(),
                EmailNotificationAdapter(),
            ]

        self.alerts_enabled: bool = (
            getattr(SystemConfig, "ALERTS_ENABLED", False)
            or (os.getenv("ALERTS_ENABLED", "false").lower() in ("true", "1", "yes"))
        )

        self._ensure_storage()

    @property
    def webhook_adapter(self) -> Optional[WebhookNotificationAdapter]:
        for a in self.adapters:
            if isinstance(a, WebhookNotificationAdapter):
                return a
        return None

    @property
    def email_adapter(self) -> Optional[EmailNotificationAdapter]:
        for a in self.adapters:
            if isinstance(a, EmailNotificationAdapter):
                return a
        return None

    def _ensure_storage(self) -> None:
        """Ensures the incidents persistence file exists and is valid."""
        os.makedirs(os.path.dirname(self.incidents_file), exist_ok=True)
        if not os.path.exists(self.incidents_file):
            initial = {"incidents": [], "updated_at": datetime.now(timezone.utc).isoformat()}
            atomic_write_json(self.incidents_file, initial)

    def _load_all(self) -> List[Incident]:
        """Loads all incidents from persistent storage with lock safety."""
        lock = FileLock("incidents_storage", timeout=5.0, lock_dir=self.data_dir, reentrant=True)
        try:
            with lock:
                if not os.path.exists(self.incidents_file):
                    return []
                with open(self.incidents_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    records = data.get("incidents", []) if isinstance(data, dict) else data
                    return [Incident.from_dict(r) for r in records if isinstance(r, dict)]
        except Exception as e:
            logger.error(f"Error reading incidents storage: {e}")
            return []

    def _save_all(self, incidents: List[Incident]) -> None:
        """Persists all incidents to persistent storage with atomic write and lock."""
        lock = FileLock("incidents_storage", timeout=5.0, lock_dir=self.data_dir, reentrant=True)
        try:
            with lock:
                payload = {
                    "incidents": [inc.to_dict() for inc in incidents],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "total_count": len(incidents),
                    "open_count": len([i for i in incidents if i.status == IncidentStatus.OPEN.value]),
                }
                atomic_write_json(self.incidents_file, payload)
        except Exception as e:
            logger.error(f"Error persisting incidents storage: {e}")

    def report_incident(
        self,
        incident_type: str,
        severity: str,
        component: str,
        source: str,
        summary: str,
        impact: str,
        recommended_action: str,
        fingerprint: Optional[str] = None,
        component_key: Optional[str] = None,
        target_identifier: Optional[str] = None,
        run_id: Optional[str] = None,
        job_id: Optional[str] = None,
        metric_name: Optional[str] = None,
        metric_value: Optional[Any] = None,
        threshold_value: Optional[Any] = None,
        baseline_value: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Incident:
        """
        Reports a technical incident. Deduplicates using deterministic fingerprinting.
        Increments occurrence count for existing active incidents.
        Enforces cooldown-based alerting.
        """
        fp = fingerprint or compute_deterministic_fingerprint(
            incident_type=incident_type,
            component_key=component_key or component,
            target_identifier=target_identifier
        )
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        incidents = self._load_all()
        active_inc = next((
            i for i in incidents
            if i.fingerprint == fp and i.status in (IncidentStatus.OPEN.value, IncidentStatus.ACKNOWLEDGED.value, IncidentStatus.SUPPRESSED.value)
        ), None)

        should_alert = False
        target_incident: Incident

        if active_inc:
            # Update existing active incident
            active_inc.occurrence_count += 1
            active_inc.last_detected_at = now_iso
            active_inc.updated_at = now_iso
            active_inc.summary = summary
            active_inc.impact = impact
            active_inc.recommended_action = recommended_action
            if run_id:
                active_inc.run_id = run_id
            if job_id:
                active_inc.job_id = job_id
            if metric_name:
                active_inc.metric_name = metric_name
            if metric_value is not None:
                active_inc.metric_value = metric_value
            if threshold_value is not None:
                active_inc.threshold_value = threshold_value
            if metadata:
                active_inc.metadata.update(metadata)

            # Check cooldown for re-alerting
            last_alert_str = active_inc.metadata.get("last_alert_at")
            if last_alert_str:
                try:
                    last_alert_dt = datetime.fromisoformat(last_alert_str)
                    cooldown = (
                        self.critical_cooldown_seconds
                        if active_inc.severity == IncidentSeverity.CRITICAL.value
                        else self.warning_cooldown_seconds
                    )
                    if (now - last_alert_dt) >= timedelta(seconds=cooldown):
                        should_alert = True
                except Exception:
                    should_alert = False
            else:
                should_alert = True

            target_incident = active_inc
        else:
            # Create fresh incident
            new_inc = Incident.create(
                incident_type=incident_type,
                severity=severity,
                component=component,
                source=source,
                summary=summary,
                impact=impact,
                recommended_action=recommended_action,
                fingerprint=fp,
                run_id=run_id,
                job_id=job_id,
                metric_name=metric_name,
                metric_value=metric_value,
                threshold_value=threshold_value,
                baseline_value=baseline_value,
                metadata=metadata,
            )
            should_alert = True
            incidents.append(new_inc)
            target_incident = new_inc

        # Dispatch notifications if alerts are globally enabled and cooldown passed
        alerts_master = (
            self.alerts_enabled
            or getattr(SystemConfig, "ALERTS_ENABLED", False)
            or (os.getenv("ALERTS_ENABLED", "false").lower() in ("true", "1", "yes"))
        )
        if should_alert and alerts_master and target_incident.status != IncidentStatus.SUPPRESSED.value:
            channels_used = []
            for adapter in self.adapters:
                try:
                    sent = adapter.send_alert(target_incident)
                    if sent:
                        channels_used.append(adapter.__class__.__name__)
                except Exception as ex:
                    logger.warning(f"Notification adapter failed non-fatally: {ex}")

            if channels_used:
                target_incident.alert_sent = True
                target_incident.alert_channels = list(set(target_incident.alert_channels + channels_used))
                target_incident.metadata["last_alert_at"] = now_iso

        self._save_all(incidents)
        logger.info(f"Reported incident: [{target_incident.severity}] {target_incident.incident_type} (occurrences: {target_incident.occurrence_count})")
        return target_incident

    def resolve_incident(self, fingerprint: str, resolution_note: str = "") -> Optional[Incident]:
        """
        Marks an active incident as RESOLVED when component health is recovered.
        Records resolution timestamp and duration without triggering noisy recovery alerts.
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        incidents = self._load_all()
        target = next((
            i for i in incidents
            if (i.fingerprint == fingerprint or i.incident_id == fingerprint) and i.status != IncidentStatus.RESOLVED.value
        ), None)

        if not target:
            return None

        target.status = IncidentStatus.RESOLVED.value
        target.resolved_at = now_iso
        target.updated_at = now_iso

        # Calculate duration
        try:
            start_dt = datetime.fromisoformat(target.first_detected_at)
            duration_s = (now - start_dt).total_seconds()
            target.metadata["resolution_duration_seconds"] = round(duration_s, 2)
        except Exception:
            pass

        if resolution_note:
            target.metadata["resolution_note"] = resolution_note

        self._save_all(incidents)
        logger.info(f"Resolved incident {target.incident_id} [{target.incident_type}]. Note: {resolution_note}")
        return target

    def get_active_incidents(self) -> List[Incident]:
        """Returns all open, acknowledged, or suppressed incidents."""
        incidents = self._load_all()
        return [i for i in incidents if i.status != IncidentStatus.RESOLVED.value]

    def get_incident_by_id(self, incident_id: str) -> Optional[Incident]:
        incidents = self._load_all()
        return next((i for i in incidents if i.incident_id == incident_id), None)

    def get_incident_by_fingerprint(self, fingerprint: str) -> Optional[Incident]:
        incidents = self._load_all()
        return next((i for i in incidents if i.fingerprint == fingerprint and i.status != IncidentStatus.RESOLVED.value), None)

    def list_incidents(
        self,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 50,
    ) -> List[Incident]:
        incidents = self._load_all()
        res = incidents
        if status:
            res = [i for i in res if i.status == status]
        if severity:
            res = [i for i in res if i.severity == severity]
        # Sort newest first
        res.sort(key=lambda x: x.last_detected_at, reverse=True)
        return res[:limit]

    def clear_all_for_testing(self) -> None:
        """Utility for test suites to reset incidents state."""
        self._save_all([])

    def get_summary(self) -> Dict[str, Any]:
        """Provides an executive summary of current incident posture."""
        incidents = self._load_all()
        active = [i for i in incidents if i.status != IncidentStatus.RESOLVED.value]
        by_severity = {s.value: 0 for s in IncidentSeverity}
        for a in active:
            by_severity[a.severity] = by_severity.get(a.severity, 0) + 1

        return {
            "total_incidents_recorded": len(incidents),
            "active_incidents_count": len(active),
            "active_by_severity": by_severity,
            "critical_count": by_severity.get(IncidentSeverity.CRITICAL.value, 0),
            "error_count": by_severity.get(IncidentSeverity.ERROR.value, 0),
            "warning_count": by_severity.get(IncidentSeverity.WARNING.value, 0),
            "info_count": by_severity.get(IncidentSeverity.INFO.value, 0),
            "active_fingerprints": [a.fingerprint for a in active],
        }


# Singleton accessor
_default_incident_manager: Optional[IncidentManager] = None


def get_incident_manager(data_dir: Optional[str] = None) -> IncidentManager:
    """Returns singleton IncidentManager instance."""
    global _default_incident_manager
    if _default_incident_manager is None:
        _default_incident_manager = IncidentManager(data_dir=data_dir)
    elif data_dir and _default_incident_manager.data_dir != data_dir:
        _default_incident_manager = IncidentManager(data_dir=data_dir)
    return _default_incident_manager
