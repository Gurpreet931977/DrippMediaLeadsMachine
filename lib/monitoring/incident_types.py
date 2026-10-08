"""
lib/monitoring/incident_types.py
================================
Canonical Incident Model and Taxonomy for Phase 10.3 Technical Monitoring & Alerts.
Defines canonical incident types, severities, statuses, and data structures.
"""

from enum import Enum
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import hashlib


class IncidentSeverity(str, Enum):
    """Canonical Incident Severity Levels."""
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class IncidentStatus(str, Enum):
    """Canonical Incident Lifecycle Statuses."""
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    SUPPRESSED = "SUPPRESSED"


class IncidentType(str, Enum):
    """Canonical Technical Incident Types Registry."""
    # Scheduler
    SCHEDULER_STOPPED = "SCHEDULER_STOPPED"
    SCHEDULER_STALLED = "SCHEDULER_STALLED"
    SCHEDULER_JOB_FAILED = "SCHEDULER_JOB_FAILED"

    # Google Sheets / CRM
    GOOGLE_AUTH_FAILURE = "GOOGLE_AUTH_FAILURE"
    GOOGLE_API_FAILURE = "GOOGLE_API_FAILURE"

    # Research / Tavily
    TAVILY_PROVIDER_FAILURE = "TAVILY_PROVIDER_FAILURE"
    TAVILY_PROVIDER_TIMEOUT = "TAVILY_PROVIDER_TIMEOUT"
    TAVILY_QUOTA_WARNING = "TAVILY_QUOTA_WARNING"
    TAVILY_QUOTA_EXCEEDED = "TAVILY_QUOTA_EXCEEDED"

    # Outreach / Email (Dormant when commercial/email disabled)
    EMAIL_PROVIDER_FAILURE = "EMAIL_PROVIDER_FAILURE"
    EMAIL_PROVIDER_AUTH_FAILURE = "EMAIL_PROVIDER_AUTH_FAILURE"
    BOUNCE_RATE_HIGH = "BOUNCE_RATE_HIGH"

    # Acquisition Pipeline & Quality
    ZERO_DISCOVERY_RESULTS = "ZERO_DISCOVERY_RESULTS"
    ZERO_QUALIFIED_RESULTS_ANOMALY = "ZERO_QUALIFIED_RESULTS_ANOMALY"
    DUPLICATE_RATE_HIGH = "DUPLICATE_RATE_HIGH"

    # Process & Runtime
    APPLICATION_CRASH = "APPLICATION_CRASH"
    APPLICATION_RESTART = "APPLICATION_RESTART"

    # Data Integrity & Resilience
    BACKUP_FAILURE = "BACKUP_FAILURE"
    RECONCILIATION_FAILURE = "RECONCILIATION_FAILURE"

    # System & Storage
    DISK_USAGE_WARNING = "DISK_USAGE_WARNING"
    DISK_USAGE_CRITICAL = "DISK_USAGE_CRITICAL"

    # General Health
    HEALTH_CHECK_FAILURE = "HEALTH_CHECK_FAILURE"
    UNEXPECTED_PIPELINE_FAILURE = "UNEXPECTED_PIPELINE_FAILURE"


def compute_deterministic_fingerprint(
    incident_type: str,
    component_key: str,
    target_identifier: Optional[str] = None
) -> str:
    """
    Computes a deterministic fingerprint for an incident:
    Format: INCIDENT_TYPE:COMPONENT[:TARGET]
    Example: GOOGLE_AUTH_FAILURE:google_sheets
             ZERO_DISCOVERY_RESULTS:MANCHESTER_UK
             TAVILY_PROVIDER_FAILURE:tavily
    """
    clean_type = str(incident_type).strip().upper()
    clean_comp = str(component_key).strip().lower()
    if target_identifier:
        clean_target = str(target_identifier).strip().upper()
        return f"{clean_type}:{clean_comp}:{clean_target}"
    return f"{clean_type}:{clean_comp}"


@dataclass
class Incident:
    """
    Canonical Incident Model for Dripp Media Technical Operations.
    """
    incident_id: str
    fingerprint: str
    incident_type: str
    severity: str
    status: str
    component: str
    source: str
    first_detected_at: str
    last_detected_at: str
    summary: str
    impact: str
    recommended_action: str
    resolved_at: Optional[str] = None
    occurrence_count: int = 1
    run_id: Optional[str] = None
    job_id: Optional[str] = None
    metric_name: Optional[str] = None
    metric_value: Optional[Any] = None
    threshold_value: Optional[Any] = None
    baseline_value: Optional[Any] = None
    alert_sent: bool = False
    alert_channels: List[str] = field(default_factory=list)
    suppression_until: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serializes the incident to a sanitized dictionary."""
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Incident":
        """Deserializes a dictionary into an Incident instance."""
        valid_fields = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    @classmethod
    def create(
        cls,
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
    ) -> "Incident":
        """Factory method to construct a fresh Incident."""
        fp = fingerprint or compute_deterministic_fingerprint(
            incident_type=incident_type,
            component_key=component_key or component,
            target_identifier=target_identifier
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        # Deterministic but unique ID based on fingerprint and detection timestamp
        id_hash = hashlib.sha256(f"{fp}:{now_iso}".encode("utf-8")).hexdigest()[:8].upper()
        inc_id = f"INC-{id_hash}"

        return cls(
            incident_id=inc_id,
            fingerprint=fp,
            incident_type=incident_type,
            severity=severity,
            status=IncidentStatus.OPEN.value,
            component=component,
            source=source,
            first_detected_at=now_iso,
            last_detected_at=now_iso,
            resolved_at=None,
            occurrence_count=1,
            run_id=run_id,
            job_id=job_id,
            metric_name=metric_name,
            metric_value=metric_value,
            threshold_value=threshold_value,
            baseline_value=baseline_value,
            summary=summary,
            impact=impact,
            recommended_action=recommended_action,
            alert_sent=False,
            alert_channels=[],
            suppression_until=None,
            created_at=now_iso,
            updated_at=now_iso,
            metadata=dict(metadata or {}),
        )
