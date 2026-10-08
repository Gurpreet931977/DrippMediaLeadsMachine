"""
lib/monitoring/__init__.py
==========================
Public API for Phase 10.3 Central Technical Monitoring & Alerts.
"""

from lib.monitoring.incident_types import (
    Incident,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    compute_deterministic_fingerprint,
)
from lib.monitoring.incident_manager import (
    IncidentManager,
    get_incident_manager,
)
from lib.monitoring.notification_adapters import (
    BaseNotificationAdapter,
    WebhookNotificationAdapter,
    EmailNotificationAdapter,
    format_standard_alert_payload,
)
from lib.monitoring.detectors import (
    SchedulerMonitor,
    GoogleSheetsMonitor,
    TavilyMonitor,
    DiscoveryMonitor,
    DeduplicationMonitor,
    EmailBounceMonitor,
    StorageMonitor,
    ApplicationLifecycleMonitor,
    BackupReconciliationMonitor,
)

__all__ = [
    "Incident",
    "IncidentSeverity",
    "IncidentStatus",
    "IncidentType",
    "compute_deterministic_fingerprint",
    "IncidentManager",
    "get_incident_manager",
    "BaseNotificationAdapter",
    "WebhookNotificationAdapter",
    "EmailNotificationAdapter",
    "format_standard_alert_payload",
    "SchedulerMonitor",
    "GoogleSheetsMonitor",
    "TavilyMonitor",
    "DiscoveryMonitor",
    "DeduplicationMonitor",
    "EmailBounceMonitor",
    "StorageMonitor",
    "ApplicationLifecycleMonitor",
    "BackupReconciliationMonitor",
]
