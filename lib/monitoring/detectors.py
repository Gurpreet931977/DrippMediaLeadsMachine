"""
lib/monitoring/detectors.py
===========================
Technical Event and Anomaly Detectors for Phase 10.3 Technical Monitoring & Alerts.
Implements:
  1. Scheduler heartbeat contract and stall/failure detection.
  2. Google Sheets authentication & API failure detection with secret scrubbing.
  3. Tavily provider failure, timeout, quota warning, and quota exhaustion detection.
  4. Zero discovery results vs zero qualified results anomaly distinction.
  5. Duplicate rate anomaly detection with minimum sample gating.
  6. Email bounce rate and provider failure detection (dormant when disabled).
  7. Application crash & unexpected restart classification.
  8. Backup and reconciliation failure detection.
  9. Storage and disk capacity threshold monitoring.
"""

import os
import shutil
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

from lib.monitoring.incident_types import (
    IncidentType,
    IncidentSeverity,
    compute_deterministic_fingerprint,
)
from lib.monitoring.incident_manager import IncidentManager, get_incident_manager
from lib.system.observability import sanitize_text, sanitize_payload
from lib.system.atomic_writer import atomic_write_json
from lib.system.system_config import SystemConfig

logger = logging.getLogger("MonitoringDetectors")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
HEARTBEAT_FILE = os.path.join(DATA_DIR, ".scheduler_heartbeat.json")
LIFECYCLE_FILE = os.path.join(DATA_DIR, ".app_lifecycle.json")


# ─────────────────────────────────────────────────────────────────────────────
# 1. SCHEDULER MONITOR & HEARTBEAT CONTRACT
# ─────────────────────────────────────────────────────────────────────────────

class SchedulerMonitor:
    """
    Exposes and inspects the external Heartbeat Contract for the Technical Scheduler.
    Note: A process cannot reliably detect its own complete death from inside itself;
    therefore, this monitor exposes a verifiable heartbeat file contract intended
    for consumption by external watchdogs, GitHub Actions, or supervisor agents.
    """

    def __init__(self, incident_mgr: Optional[IncidentManager] = None, data_dir: Optional[str] = None):
        self.data_dir = data_dir or DATA_DIR
        self.incident_mgr = incident_mgr or get_incident_manager(data_dir=self.data_dir)
        self.heartbeat_path = os.path.join(self.data_dir, ".scheduler_heartbeat.json")

    def record_heartbeat(
        self,
        scheduler_id: str = "production_technical_scheduler",
        state: str = "RUNNING",
        current_job: Optional[str] = None,
        last_successful_job: Optional[Dict[str, Any]] = None,
        last_failed_job: Optional[Dict[str, Any]] = None,
        next_expected_job: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Writes the heartbeat state contract to disk atomically."""
        now_iso = datetime.now(timezone.utc).isoformat()
        heartbeat = {
            "scheduler_id": scheduler_id,
            "state": state,
            "last_heartbeat": now_iso,
            "last_successful_job": last_successful_job,
            "last_failed_job": last_failed_job,
            "current_job": current_job,
            "next_expected_job": next_expected_job,
            "pid": os.getpid(),
        }
        atomic_write_json(self.heartbeat_path, heartbeat)
        return heartbeat

    def inspect_heartbeat(
        self,
        stall_threshold_seconds: int = 1800,  # 30 minutes
        stop_threshold_seconds: int = 7200,   # 2 hours
        scheduler_id: Optional[str] = None,
        max_lag_seconds: Optional[int] = None,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Inspects the heartbeat contract and creates incidents if stalled or stopped.
        """
        if max_lag_seconds is not None:
            stall_threshold_seconds = max_lag_seconds

        if not os.path.exists(self.heartbeat_path):
            return "NO_HEARTBEAT", None

        try:
            with open(self.heartbeat_path, "r", encoding="utf-8") as f:
                hb = json.load(f)
        except Exception:
            return "CORRUPTED_HEARTBEAT", None

        last_hb_str = hb.get("last_heartbeat")
        sched_id = scheduler_id or hb.get("scheduler_id", "default_scheduler")
        fp_base = f"scheduler:{sched_id}"

        if not last_hb_str:
            return "INVALID_HEARTBEAT", hb

        try:
            last_dt = datetime.fromisoformat(last_hb_str)
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        except Exception:
            return "PARSE_ERROR", hb

        if elapsed >= stop_threshold_seconds:
            self.incident_mgr.report_incident(
                incident_type=IncidentType.SCHEDULER_STOPPED.value,
                severity=IncidentSeverity.CRITICAL.value,
                component="Technical Scheduler",
                source="SchedulerMonitor",
                summary=f"Scheduler '{sched_id}' has not emitted a heartbeat for {int(elapsed)}s.",
                impact="Automated background jobs are not executing.",
                recommended_action="Inspect host process, daemon logs, or supervisor service.",
                component_key="scheduler",
                target_identifier=sched_id,
                metric_name="heartbeat_delay_seconds",
                metric_value=elapsed,
                threshold_value=stop_threshold_seconds,
            )
            return "STOPPED", hb
        elif elapsed >= stall_threshold_seconds:
            self.incident_mgr.report_incident(
                incident_type=IncidentType.SCHEDULER_STALLED.value,
                severity=IncidentSeverity.WARNING.value,
                component="Technical Scheduler",
                source="SchedulerMonitor",
                summary=f"Scheduler '{sched_id}' heartbeat delayed ({int(elapsed)}s).",
                impact="Scheduled tasks may be lagging behind interval targets.",
                recommended_action="Check if current task is blocking or execution time is unusually long.",
                component_key="scheduler",
                target_identifier=sched_id,
                metric_name="heartbeat_delay_seconds",
                metric_value=elapsed,
                threshold_value=stall_threshold_seconds,
            )
            return "STALLED", hb
        else:
            # Healthy: resolve open stall/stopped incidents for this scheduler
            self.incident_mgr.resolve_incident(
                fingerprint=compute_deterministic_fingerprint(IncidentType.SCHEDULER_STOPPED.value, "scheduler", sched_id),
                resolution_note="Scheduler heartbeat restored to normal intervals."
            )
            self.incident_mgr.resolve_incident(
                fingerprint=compute_deterministic_fingerprint(IncidentType.SCHEDULER_STALLED.value, "scheduler", sched_id),
                resolution_note="Scheduler heartbeat restored to normal intervals."
            )
            return "HEALTHY", hb

    check_scheduler_health = inspect_heartbeat

    def record_job_failure(
        self,
        job_id: str,
        job_type: str,
        error_message: str,
        scheduler_id: str = "production_technical_scheduler",
    ) -> None:
        """Reports a scheduled job failure."""
        clean_err = sanitize_text(error_message)
        self.incident_mgr.report_incident(
            incident_type=IncidentType.SCHEDULER_JOB_FAILED.value,
            severity=IncidentSeverity.ERROR.value,
            component="Technical Scheduler",
            source="SchedulerMonitor",
            summary=f"Scheduled job '{job_id}' ({job_type}) failed: {clean_err[:120]}",
            impact=f"Scheduled maintenance task '{job_type}' was not completed.",
            recommended_action=f"Inspect error logs for job '{job_id}' and retry manually if necessary.",
            component_key="scheduler",
            target_identifier=f"{scheduler_id}_{job_type}",
            job_id=job_id,
            metadata={"job_type": job_type, "error": clean_err},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. GOOGLE SHEETS & CRM MONITOR
# ─────────────────────────────────────────────────────────────────────────────

class GoogleSheetsMonitor:
    """
    Monitors Google Sheets CRM connectivity, authentication, and API exceptions.
    Guarantees strict secret redaction.
    """

    def __init__(self, incident_mgr: Optional[IncidentManager] = None):
        self.incident_mgr = incident_mgr or get_incident_manager()

    def record_auth_failure(self, error: Exception, context: str = "crm_sync") -> None:
        clean_err = sanitize_text(str(error))
        self.incident_mgr.report_incident(
            incident_type=IncidentType.GOOGLE_AUTH_FAILURE.value,
            severity=IncidentSeverity.ERROR.value,
            component="Google Sheets",
            source="GoogleSheetsMonitor",
            summary=f"Google Sheets authentication failed during {context}: {clean_err[:120]}",
            impact="CRM synchronization unavailable. Leads cannot be mirrored to Google Sheets.",
            recommended_action="Verify GOOGLE_SERVICE_ACCOUNT_JSON and sheet sharing permissions.",
            component_key="google_sheets",
            metadata={"context": context},
        )

    def record_api_failure(self, error: Exception, sheet_id: Optional[str] = None) -> None:
        clean_err = sanitize_text(str(error))
        clean_sheet_id = sanitize_text(sheet_id or "default")
        self.incident_mgr.report_incident(
            incident_type=IncidentType.GOOGLE_API_FAILURE.value,
            severity=IncidentSeverity.ERROR.value,
            component="Google Sheets",
            source="GoogleSheetsMonitor",
            summary=f"Google Sheets API call failed on sheet '{clean_sheet_id}': {clean_err[:120]}",
            impact="CRM state updates or read queries encountered an API error.",
            recommended_action="Check Google Sheets API quota limits, network latency, or rate limiting.",
            component_key="google_sheets",
            target_identifier=clean_sheet_id,
            metadata={"error": clean_err},
        )

    def record_success(self, sheet_id: Optional[str] = None) -> None:
        """Resolves active Google Sheets auth and API failure incidents."""
        self.incident_mgr.resolve_incident(
            fingerprint=compute_deterministic_fingerprint(IncidentType.GOOGLE_AUTH_FAILURE.value, "google_sheets"),
            resolution_note="Google Sheets authentication successfully recovered."
        )
        self.incident_mgr.resolve_incident(
            fingerprint=compute_deterministic_fingerprint(IncidentType.GOOGLE_API_FAILURE.value, "google_sheets"),
            resolution_note="Google Sheets API operations responding normally."
        )
        if sheet_id:
            clean_sheet_id = sanitize_text(sheet_id)
            self.incident_mgr.resolve_incident(
                fingerprint=compute_deterministic_fingerprint(IncidentType.GOOGLE_API_FAILURE.value, "google_sheets", clean_sheet_id),
                resolution_note=f"Google Sheets API operations on '{clean_sheet_id}' responding normally."
            )
        # Also resolve any remaining open GOOGLE_API_FAILURE or GOOGLE_AUTH_FAILURE incidents for google_sheets
        active_incidents = self.incident_mgr.get_active_incidents()
        for inc in active_incidents:
            if inc.incident_type in (IncidentType.GOOGLE_API_FAILURE.value, IncidentType.GOOGLE_AUTH_FAILURE.value) and inc.component == "Google Sheets":
                self.incident_mgr.resolve_incident(
                    fingerprint=inc.fingerprint,
                    resolution_note="Google Sheets connection confirmed operational."
                )


# ─────────────────────────────────────────────────────────────────────────────
# 3. TAVILY & WEB RESEARCH MONITOR
# ─────────────────────────────────────────────────────────────────────────────

class TavilyMonitor:
    """
    Monitors Tavily research provider calls and quota governor integration.
    Guarantees zero API key exposure in incidents or telemetry.
    """

    def __init__(self, incident_mgr: Optional[IncidentManager] = None):
        self.incident_mgr = incident_mgr or get_incident_manager()

    def record_call_outcome(
        self,
        outcome_str: str,
        query: str,
        latency: float = 0.0,
        quota_remaining: Optional[int] = None,
        quota_limit: Optional[int] = None,
    ) -> None:
        """Inspects search outcome and triggers corresponding incidents if failed."""
        safe_query = sanitize_text(query)

        if outcome_str in ("SEARCH_SUCCEEDED_WITH_RESULTS", "SEARCH_SUCCEEDED_EMPTY", "SEARCH_SUCCEEDED"):
            # Resolve provider failures on success
            self.incident_mgr.resolve_incident(
                fingerprint=compute_deterministic_fingerprint(IncidentType.TAVILY_PROVIDER_FAILURE.value, "tavily"),
                resolution_note="Tavily search provider responding successfully."
            )
            self.incident_mgr.resolve_incident(
                fingerprint=compute_deterministic_fingerprint(IncidentType.TAVILY_PROVIDER_TIMEOUT.value, "tavily"),
                resolution_note="Tavily search provider latency returned to normal."
            )

        elif outcome_str in ("PROVIDER_TIMEOUT", "SEARCH_TIMEOUT"):
            self.incident_mgr.report_incident(
                incident_type=IncidentType.TAVILY_PROVIDER_TIMEOUT.value,
                severity=IncidentSeverity.WARNING.value,
                component="Tavily Research",
                source="TavilyMonitor",
                summary=f"Tavily search query timed out ({round(latency, 2)}s). Query: '{safe_query[:60]}'",
                impact="Candidate review enrichment delayed; falling back to alternative research providers.",
                recommended_action="Inspect network latency or temporary Tavily API degradation.",
                component_key="tavily",
                metric_name="latency_seconds",
                metric_value=round(latency, 3),
            )

        elif outcome_str in ("PROVIDER_FAILED", "SEARCH_FAILED"):
            self.incident_mgr.report_incident(
                incident_type=IncidentType.TAVILY_PROVIDER_FAILURE.value,
                severity=IncidentSeverity.ERROR.value,
                component="Tavily Research",
                source="TavilyMonitor",
                summary=f"Tavily search API returned HTTP failure on query: '{safe_query[:60]}'",
                impact="Automated review recovery using Tavily is failing.",
                recommended_action="Check Tavily service status, HTTP status codes, or API availability.",
                component_key="tavily",
            )

        elif outcome_str == "QUOTA_EXCEEDED":
            self.incident_mgr.report_incident(
                incident_type=IncidentType.TAVILY_QUOTA_EXCEEDED.value,
                severity=IncidentSeverity.ERROR.value,
                component="Tavily Research",
                source="TavilyMonitor",
                summary="Tavily monthly/daily request budget is fully exhausted (HTTP 429 or quota budget limit).",
                impact="External review enrichment halted; reviews will remain unknown unless alternative providers configured.",
                recommended_action="Wait for monthly quota reset, or increase TAVILY_QUOTA_LIMIT in environment.",
                component_key="tavily",
                metric_name="quota_remaining",
                metric_value=0,
            )

        # Quota budget warning check (if quota numbers provided)
        if quota_remaining is not None and quota_limit is not None and quota_limit > 0:
            used = quota_limit - quota_remaining
            pct_used = (used / quota_limit) * 100.0
            if pct_used >= 80.0 and pct_used < 100.0:
                self.incident_mgr.report_incident(
                    incident_type=IncidentType.TAVILY_QUOTA_WARNING.value,
                    severity=IncidentSeverity.WARNING.value,
                    component="Tavily Research",
                    source="TavilyMonitor",
                    summary=f"Tavily quota consumption reached {round(pct_used, 1)}% ({used}/{quota_limit} calls used).",
                    impact="Remaining budget is low; approaching free tier exhaustion.",
                    recommended_action="Monitor acquisition batch sizes or adjust market lead limits.",
                    component_key="tavily",
                    metric_name="pct_quota_used",
                    metric_value=round(pct_used, 1),
                    threshold_value=80.0,
                )


# ─────────────────────────────────────────────────────────────────────────────
# 4. DISCOVERY & PIPELINE RESULTS MONITOR
# ─────────────────────────────────────────────────────────────────────────────

class DiscoveryMonitor:
    """
    Monitors discovery counts and qualification distributions.
    Strictly distinguishes ZERO_DISCOVERY_RESULTS from ZERO_QUALIFIED_RESULTS_ANOMALY.
    Does not treat zero qualified leads as an anomaly unless sample size is significant
    and historically uncharacteristic.
    """

    def __init__(self, incident_mgr: Optional[IncidentManager] = None):
        self.incident_mgr = incident_mgr or get_incident_manager()

    def evaluate_run_results(
        self,
        market_id: str,
        discovered_count: int,
        processed_count: int,
        qualified_count: int,
        historical_baseline_discovery: int = 20,
        historical_qualification_rate: float = 0.05,
        historical_baseline: Optional[int] = None,
        min_processed_sample: int = 15,
    ) -> None:
        """
        Evaluates acquisition pipeline outcome.
        """
        base_disc = historical_baseline if historical_baseline is not None else historical_baseline_discovery
        # 1. Zero Discovery Results Check
        if discovered_count == 0 and base_disc > 0:
            self.incident_mgr.report_incident(
                incident_type=IncidentType.ZERO_DISCOVERY_RESULTS.value,
                severity=IncidentSeverity.ERROR.value,
                component="Discovery Provider",
                source="DiscoveryMonitor",
                summary=f"Market '{market_id}' returned 0 discovered businesses (expected baseline >= {base_disc}).",
                impact="No candidates available for verification or pipeline processing.",
                recommended_action="Inspect OpenStreetMap Overpass API availability, query bounding box, or network connection.",
                component_key="discovery",
                target_identifier=market_id,
                metric_name="discovered_count",
                metric_value=0,
                baseline_value=base_disc,
            )
        elif discovered_count > 0:
            self.incident_mgr.resolve_incident(
                fingerprint=compute_deterministic_fingerprint(IncidentType.ZERO_DISCOVERY_RESULTS.value, "discovery", market_id),
                resolution_note=f"Discovery restored: {discovered_count} businesses found."
            )

        # 2. Zero Qualified Results Anomaly Check
        if processed_count >= min_processed_sample and qualified_count == 0 and historical_qualification_rate > 0.0:
            self.incident_mgr.report_incident(
                incident_type=IncidentType.ZERO_QUALIFIED_RESULTS_ANOMALY.value,
                severity=IncidentSeverity.WARNING.value,
                component="Lead Qualification",
                source="DiscoveryMonitor",
                summary=f"Processed {processed_count} candidates in '{market_id}' with 0 qualified leads.",
                impact="Candidate yield is zero despite substantial processing volume.",
                recommended_action="Inspect review recovery provider health, website detection, or Rule B gating diagnostics.",
                component_key="qualification",
                target_identifier=market_id,
                metric_name="qualified_count",
                metric_value=0,
                threshold_value=1,
            )
        elif qualified_count > 0:
            self.incident_mgr.resolve_incident(
                fingerprint=compute_deterministic_fingerprint(IncidentType.ZERO_QUALIFIED_RESULTS_ANOMALY.value, "qualification", market_id),
                resolution_note=f"Qualified leads yielded: {qualified_count} leads."
            )


# ─────────────────────────────────────────────────────────────────────────────
# 5. DEDUPLICATION MONITOR
# ─────────────────────────────────────────────────────────────────────────────

class DeduplicationMonitor:
    """
    Monitors duplicate rates during batch runs to detect data drift,
    stale candidate pools, or excessive redundant processing.
    """

    def __init__(self, incident_mgr: Optional[IncidentManager] = None):
        self.incident_mgr = incident_mgr or get_incident_manager()

    def evaluate_duplicate_rate(
        self,
        market_id: str,
        total_discovered: int,
        duplicates_skipped: int,
        min_sample_size: int = 20,
        warning_threshold_pct: float = 15.0,
        threshold_pct: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Calculates duplicate percentage and triggers incident if above threshold with sufficient sample size.
        """
        threshold = threshold_pct if threshold_pct is not None else warning_threshold_pct
        if total_discovered < min_sample_size:
            return {"status": "INSUFFICIENT_SAMPLE", "sample": total_discovered, "min_sample": min_sample_size}

        dup_rate_pct = (duplicates_skipped / total_discovered) * 100.0

        if dup_rate_pct >= threshold:
            self.incident_mgr.report_incident(
                incident_type=IncidentType.DUPLICATE_RATE_HIGH.value,
                severity=IncidentSeverity.WARNING.value,
                component="CRM Deduplication",
                source="DeduplicationMonitor",
                summary=f"High duplicate rate ({round(dup_rate_pct, 1)}%) in market '{market_id}': {duplicates_skipped}/{total_discovered} skipped.",
                impact="Discovery pool is becoming saturated; high compute expenditure on existing entities.",
                recommended_action="Expand discovery queries to adjacent sub-sectors or update geographic bounding coordinates.",
                component_key="deduplication",
                target_identifier=market_id,
                metric_name="duplicate_rate_pct",
                metric_value=round(dup_rate_pct, 1),
                threshold_value=threshold,
            )
            return {"status": "ANOMALY", "rate": dup_rate_pct, "duplicates_skipped": duplicates_skipped, "total": total_discovered}
        else:
            self.incident_mgr.resolve_incident(
                fingerprint=compute_deterministic_fingerprint(IncidentType.DUPLICATE_RATE_HIGH.value, "deduplication", market_id),
                resolution_note=f"Duplicate rate healthy ({round(dup_rate_pct, 1)}%)."
            )
            return {"status": "NORMAL", "rate": dup_rate_pct, "duplicates_skipped": duplicates_skipped, "total": total_discovered}


# ─────────────────────────────────────────────────────────────────────────────
# 6. EMAIL BOUNCE MONITOR (Dormant when disabled)
# ─────────────────────────────────────────────────────────────────────────────

class EmailBounceMonitor:
    """
    Monitors email bounce rates.
    Remains strictly dormant when automated email is disabled.
    """

    def __init__(self, incident_mgr: Optional[IncidentManager] = None):
        self.incident_mgr = incident_mgr or get_incident_manager()

    def evaluate_bounces(
        self,
        total_sent: int,
        total_bounced: int,
        min_sample: int = 20,
        max_bounce_rate_pct: float = 10.0,
        threshold_pct: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not getattr(SystemConfig, "AUTOMATED_EMAIL_ENABLED", False):
            return {"status": "DORMANT", "is_dormant": True}

        if total_sent < min_sample:
            return {"status": "INSUFFICIENT_SAMPLE", "sample": total_sent, "min_sample": min_sample, "is_dormant": False}

        thresh = threshold_pct if threshold_pct is not None else max_bounce_rate_pct
        bounce_rate = (total_bounced / total_sent) * 100.0
        if bounce_rate >= thresh:
            self.incident_mgr.report_incident(
                incident_type=IncidentType.BOUNCE_RATE_HIGH.value,
                severity=IncidentSeverity.CRITICAL.value,
                component="Email Outreach",
                source="EmailBounceMonitor",
                summary=f"Email bounce rate ({round(bounce_rate, 1)}%) exceeded threshold ({thresh}%).",
                impact="Risk of sender domain reputation degradation. Automated email sending must pause.",
                recommended_action="Inspect MX verification rigor, verify suppression list, and pause campaigns.",
                component_key="email_outreach",
                metric_name="bounce_rate_pct",
                metric_value=round(bounce_rate, 1),
                threshold_value=thresh,
            )
            return {"status": "ANOMALY", "bounce_rate": bounce_rate, "threshold": thresh, "is_dormant": False}
        else:
            self.incident_mgr.resolve_incident(
                fingerprint=compute_deterministic_fingerprint(IncidentType.BOUNCE_RATE_HIGH.value, "email_outreach"),
                resolution_note=f"Bounce rate healthy ({round(bounce_rate, 1)}%)."
            )
            return {"status": "NORMAL", "bounce_rate": bounce_rate, "threshold": thresh, "is_dormant": False}

    evaluate_bounce_rate = evaluate_bounces


# ─────────────────────────────────────────────────────────────────────────────
# 7. STORAGE & DISK CAPACITY MONITOR
# ─────────────────────────────────────────────────────────────────────────────

class StorageMonitor:
    """
    Monitors storage space and directory sizes across data/ and logs.
    """

    def __init__(self, incident_mgr: Optional[IncidentManager] = None, data_dir: Optional[str] = None):
        self.data_dir = data_dir or DATA_DIR
        self.incident_mgr = incident_mgr or get_incident_manager(data_dir=self.data_dir)

    def check_disk_usage(
        self,
        target_path: Optional[str] = None,
        warning_pct: float = 70.0,
        critical_pct: float = 90.0,
    ) -> Dict[str, Any]:
        """
        Inspects filesystem usage for the specified target path.
        """
        path_to_check = target_path or self.data_dir
        if not os.path.exists(path_to_check):
            os.makedirs(path_to_check, exist_ok=True)

        try:
            u = shutil.disk_usage(path_to_check)
            if hasattr(u, "total") and hasattr(u, "used") and hasattr(u, "free"):
                total, used, free = u.total, u.used, u.free
            else:
                total, used, free = u[0], u[1], u[2]
            pct_used = (used / total) * 100.0 if total > 0 else 0.0
        except Exception as e:
            logger.warning(f"Failed checking disk usage for {path_to_check}: {e}")
            return {"status": "UNKNOWN", "pct_used": 0.0}

        fp_comp = "storage"
        if pct_used >= critical_pct:
            self.incident_mgr.report_incident(
                incident_type=IncidentType.DISK_USAGE_CRITICAL.value,
                severity=IncidentSeverity.CRITICAL.value,
                component="Storage & Disk",
                source="StorageMonitor",
                summary=f"Disk space critically low: {round(pct_used, 1)}% used on '{path_to_check}'.",
                impact="Risk of atomic file write failures, database corruption, or runner crash.",
                recommended_action="Rotate old market runs, prune cached OSM files, or expand disk volume.",
                component_key=fp_comp,
                metric_name="disk_used_pct",
                metric_value=round(pct_used, 1),
                threshold_value=critical_pct,
            )
        elif pct_used >= warning_pct:
            self.incident_mgr.report_incident(
                incident_type=IncidentType.DISK_USAGE_WARNING.value,
                severity=IncidentSeverity.WARNING.value,
                component="Storage & Disk",
                source="StorageMonitor",
                summary=f"Disk space warning: {round(pct_used, 1)}% used on '{path_to_check}'.",
                impact="Storage usage approaching threshold limits.",
                recommended_action="Inspect log sizes and market run archives.",
                component_key=fp_comp,
                metric_name="disk_used_pct",
                metric_value=round(pct_used, 1),
                threshold_value=warning_pct,
            )
        else:
            self.incident_mgr.resolve_incident(
                fingerprint=compute_deterministic_fingerprint(IncidentType.DISK_USAGE_CRITICAL.value, fp_comp),
                resolution_note=f"Disk space returned to safe levels ({round(pct_used, 1)}% used)."
            )
            self.incident_mgr.resolve_incident(
                fingerprint=compute_deterministic_fingerprint(IncidentType.DISK_USAGE_WARNING.value, fp_comp),
                resolution_note=f"Disk space returned to safe levels ({round(pct_used, 1)}% used)."
            )

        return {
            "status": "CRITICAL" if pct_used >= critical_pct else ("WARN" if pct_used >= warning_pct else "PASS"),
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "pct_used": round(pct_used, 2),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 8. APPLICATION LIFECYCLE & CRASH DETECTOR
# ─────────────────────────────────────────────────────────────────────────────

class ApplicationLifecycleMonitor:
    """
    Tracks application session starts, clean shutdowns, and detects unhandled crashes.
    """

    def __init__(self, incident_mgr: Optional[IncidentManager] = None, data_dir: Optional[str] = None):
        self.data_dir = data_dir or DATA_DIR
        self.incident_mgr = incident_mgr or get_incident_manager(data_dir=self.data_dir)
        self.lifecycle_path = os.path.join(self.data_dir, ".app_lifecycle.json")

    def record_startup(self, session_id: str) -> None:
        """
        Inspects prior session shutdown state before recording current session startup.
        If previous session was RUNNING without a clean shutdown, registers an APPLICATION_CRASH.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        prior_state = None

        if os.path.exists(self.lifecycle_path):
            try:
                with open(self.lifecycle_path, "r", encoding="utf-8") as f:
                    prior_state = json.load(f)
            except Exception:
                pass

        if prior_state and prior_state.get("status") == "RUNNING":
            # Previous session terminated without clean shutdown
            prev_session = prior_state.get("session_id", "unknown_session")
            prev_start = prior_state.get("started_at", "unknown")
            self.incident_mgr.report_incident(
                incident_type=IncidentType.APPLICATION_CRASH.value,
                severity=IncidentSeverity.ERROR.value,
                component="Application Lifecycle",
                source="ApplicationLifecycleMonitor",
                summary=f"Previous session '{prev_session}' terminated unexpectedly without clean shutdown.",
                impact="Possible unexpected SIGKILL, unhandled exception, or host termination.",
                recommended_action="Inspect system crash logs, memory limits, and unhandled exception traces.",
                component_key="lifecycle",
                target_identifier=prev_session,
                metadata={"prior_started_at": prev_start},
            )

        # Record current session
        current = {
            "session_id": session_id,
            "started_at": now_iso,
            "status": "RUNNING",
            "shutdown_type": None,
            "pid": os.getpid(),
        }
        atomic_write_json(self.lifecycle_path, current)

    def record_clean_shutdown(self, session_id: str, reason: str = "NORMAL_EXIT") -> None:
        """Records orderly shutdown."""
        now_iso = datetime.now(timezone.utc).isoformat()
        state = {
            "session_id": session_id,
            "status": "STOPPED",
            "shutdown_type": "CLEAN",
            "shutdown_at": now_iso,
            "reason": reason,
        }
        atomic_write_json(self.lifecycle_path, state)
        self.incident_mgr.resolve_incident(
            fingerprint=compute_deterministic_fingerprint(IncidentType.APPLICATION_CRASH.value, "lifecycle", session_id),
            resolution_note="Application session closed cleanly."
        )


# ─────────────────────────────────────────────────────────────────────────────
# 9. BACKUP & RECONCILIATION MONITOR
# ─────────────────────────────────────────────────────────────────────────────

class BackupReconciliationMonitor:
    """
    Inspects backup manifest integrity and reconciliation multi-store consistency.
    """

    def __init__(self, incident_mgr: Optional[IncidentManager] = None):
        self.incident_mgr = incident_mgr or get_incident_manager()

    def record_backup_failure(self, error: Exception, context: str = "daily_backup") -> None:
        clean_err = sanitize_text(str(error))
        self.incident_mgr.report_incident(
            incident_type=IncidentType.BACKUP_FAILURE.value,
            severity=IncidentSeverity.CRITICAL.value,
            component="Backup Manager",
            source="BackupReconciliationMonitor",
            summary=f"Backup operation failed during {context}: {clean_err[:120]}",
            impact="State snapshot not saved; risk of data loss on runner termination.",
            recommended_action="Check disk permissions, storage space, or manifest generation.",
            component_key="backup",
            metadata={"error": clean_err},
        )

    def record_reconciliation_failure(self, issues_count: int, critical_issues: List[str]) -> None:
        if issues_count == 0:
            self.incident_mgr.resolve_incident(
                fingerprint=compute_deterministic_fingerprint(IncidentType.RECONCILIATION_FAILURE.value, "reconciliation"),
                resolution_note="All data store records reconciled without contradiction."
            )
            return

        self.incident_mgr.report_incident(
            incident_type=IncidentType.RECONCILIATION_FAILURE.value,
            severity=IncidentSeverity.ERROR.value,
            component="Reconciliation Engine",
            source="BackupReconciliationMonitor",
            summary=f"Reconciliation detected {issues_count} contradictions across CRM and local stores.",
            impact="Data stores contain conflicting lead states or missing records.",
            recommended_action="Run manual reconciliation audit and resolve multi-store discrepancies.",
            component_key="reconciliation",
            metric_name="issues_count",
            metric_value=issues_count,
            metadata={"critical_issues": [sanitize_text(c) for c in critical_issues[:5]]},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 10. FRESHNESS & CANONICAL REFRESH MONITOR (Phase 10.4)
# ─────────────────────────────────────────────────────────────────────────────

class FreshnessMonitor:
    """
    Monitors automated lead freshness rechecks and canonical refresh jobs.
    Emits technical incidents only for true anomalies (Section 24).
    """

    def __init__(self, incident_mgr: Optional[IncidentManager] = None):
        self.incident_mgr = incident_mgr or get_incident_manager()

    def record_job_failure(self, error: Exception, context: str = "freshness_job") -> None:
        clean_err = sanitize_text(str(error))
        self.incident_mgr.report_incident(
            incident_type=IncidentType.REFRESH_JOB_FAILED.value,
            severity=IncidentSeverity.ERROR.value,
            component="Freshness Engine",
            source="FreshnessMonitor",
            summary=f"Technical freshness execution failed during {context}: {clean_err[:120]}",
            impact="Lead records and evidence recency were not refreshed.",
            recommended_action="Inspect job execution logs and candidate dataset for unhandled exceptions.",
            component_key="freshness_engine",
            metadata={"error": clean_err},
        )

    def record_provider_failure(self, provider_name: str, error_msg: str) -> None:
        clean_msg = sanitize_text(error_msg)
        clean_prov = sanitize_text(provider_name).lower()
        self.incident_mgr.report_incident(
            incident_type=IncidentType.REFRESH_PROVIDER_FAILURE.value,
            severity=IncidentSeverity.ERROR.value,
            component="Freshness Engine",
            source="FreshnessMonitor",
            summary=f"Freshness evidence provider '{provider_name}' failed: {clean_msg[:120]}",
            impact="Evidence refresh could not query provider; prior evidence preserved.",
            recommended_action=f"Check API availability and network connectivity for {provider_name}.",
            component_key=clean_prov,
            metadata={"provider": provider_name, "error": clean_msg},
        )

    def record_quota_exhausted(self, resource_name: str) -> None:
        clean_res = sanitize_text(resource_name).lower()
        self.incident_mgr.report_incident(
            incident_type=IncidentType.REFRESH_QUOTA_EXCEEDED.value,
            severity=IncidentSeverity.WARNING.value,
            component="Freshness Engine",
            source="FreshnessMonitor",
            summary=f"Daily refresh quota exhausted for resource '{resource_name}'. Job safely paused.",
            impact="Remaining due leads queued for next scheduled technical cycle.",
            recommended_action=f"Wait for quota reset or adjust daily {resource_name} limit.",
            component_key=clean_res,
            metadata={"resource": resource_name},
        )

    def evaluate_failure_rate(self, leads_attempted: int, leads_failed: int, market_id: str = "MANCHESTER_UK") -> None:
        clean_market = sanitize_text(market_id).upper()
        fp = compute_deterministic_fingerprint(IncidentType.REFRESH_FAILURE_RATE_HIGH.value, "freshness_engine", clean_market)
        if leads_attempted < 5:
            return

        fail_rate = leads_failed / leads_attempted
        if fail_rate > 0.50:
            self.incident_mgr.report_incident(
                incident_type=IncidentType.REFRESH_FAILURE_RATE_HIGH.value,
                severity=IncidentSeverity.ERROR.value,
                component="Freshness Engine",
                source="FreshnessMonitor",
                summary=f"High freshness failure rate ({round(fail_rate * 100, 1)}%) in market '{clean_market}' ({leads_failed}/{leads_attempted} failed).",
                impact="Majority of candidate refreshes are failing; check upstream providers or network.",
                recommended_action="Inspect provider credentials, network status, or target market configuration.",
                component_key="freshness_engine",
                target_identifier=clean_market,
                metric_name="fail_rate",
                metric_value=fail_rate,
                threshold_value=0.50,
            )
        else:
            self.incident_mgr.resolve_incident(
                fingerprint=fp,
                resolution_note=f"Freshness failure rate returned to acceptable level ({round(fail_rate * 100, 1)}%)."
            )

    def evaluate_zero_result_anomaly(self, leads_due: int, leads_refreshed: int, market_id: str = "MANCHESTER_UK") -> None:
        clean_market = sanitize_text(market_id).upper()
        fp = compute_deterministic_fingerprint(IncidentType.REFRESH_ZERO_RESULT_ANOMALY.value, "freshness_engine", clean_market)
        if leads_due >= 5 and leads_refreshed == 0:
            self.incident_mgr.report_incident(
                incident_type=IncidentType.REFRESH_ZERO_RESULT_ANOMALY.value,
                severity=IncidentSeverity.WARNING.value,
                component="Freshness Engine",
                source="FreshnessMonitor",
                summary=f"Anomalous zero refreshed leads despite {leads_due} due leads in market '{clean_market}'.",
                impact="No candidate leads were updated in this technical refresh run.",
                recommended_action="Check quota availability, queue filtering, or candidate eligibility criteria.",
                component_key="freshness_engine",
                target_identifier=clean_market,
                metric_name="leads_refreshed",
                metric_value=0,
            )
        elif leads_refreshed > 0:
            self.incident_mgr.resolve_incident(
                fingerprint=fp,
                resolution_note=f"Leads successfully refreshed in market '{clean_market}' ({leads_refreshed} updated)."
            )
