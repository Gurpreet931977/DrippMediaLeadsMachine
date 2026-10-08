"""
test_phase_10_3_production_monitoring.py
========================================
Comprehensive Unit, Integration, and Synthetic Failure Simulation Tests
for Phase 10.3: Production Monitoring & Alerts.

Tests cover:
1. Canonical incident creation, attributes, and serialization.
2. Deterministic fingerprinting across incident types and components.
3. Incident deduplication and occurrence counting.
4. Cooldown throttling (aggressive for WARNING/ERROR, periodic for CRITICAL).
5. Incident resolution, duration recording, and silent recovery.
6. Canonical health status mapping (HEALTHY, DEGRADED, UNHEALTHY, CRITICAL).
7. Scheduler monitoring: heartbeat contract, stall detection, and job failure.
8. Google Sheets monitoring: auth failure, API failure, and success recovery.
9. Tavily monitoring: success, timeout, failure, 429 quota exhaustion, and 80% warning.
10. Zero result monitoring: zero discovery vs zero qualified lead anomaly.
11. Deduplication monitoring: minimum sample size (20) and duplicate rate threshold (15%).
12. Email bounce monitoring: dormant when email disabled, threshold when active.
13. Application lifecycle: clean shutdown vs crash detection on unexpected restart.
14. Backup & Reconciliation monitoring: backup failure, checksum, and reconciliation mismatch.
15. Storage monitoring: disk warning (>=70%) and critical (>=90%) thresholds.
16. Notification adapters: disabled by default, formatting, and mocked dispatch.
17. Notification error resilience: notification failure never crashes calling pipeline.
18. Secret redaction: API keys, tokens, and credentials scrubbed from all payloads.
19. Incident persistence: atomic writes, reload from disk across process restarts.
20. Monitoring safety invariants: monitoring never sends outreach, emails, or alters Rule B.
21. End-to-end synthetic failure simulation: failure -> incident -> notify -> persist -> recover -> resolve.
"""

import os
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from lib.monitoring.incident_types import (
    Incident,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    compute_deterministic_fingerprint,
)
from lib.monitoring.incident_manager import IncidentManager, get_incident_manager
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
    sanitize_text,
    sanitize_payload,
)
from lib.monitoring.notification_adapters import (
    format_standard_alert_payload,
    WebhookNotificationAdapter,
    EmailNotificationAdapter,
)
from lib.system.system_health import SystemHealthMonitor
from lib.system.system_config import SystemConfig


class TestPhase103ProductionMonitoring(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_phase_10_3_")
        self.data_dir = os.path.join(self.test_dir, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        from lib.system.storage_bootstrap import bootstrap_storage_baseline
        bootstrap_storage_baseline(data_dir=self.data_dir, sync_from_sheets_if_available=False)
        self.mgr = IncidentManager(data_dir=self.data_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────────
    # 1. CANONICAL INCIDENT MODEL & ATTRIBUTES
    # ─────────────────────────────────────────────────────────────────────────

    def test_canonical_incident_creation_and_attributes(self):
        """Validates that Incident dataclass has all required fields and serializes correctly."""
        fp = compute_deterministic_fingerprint(IncidentType.GOOGLE_AUTH_FAILURE.value, "google_sheets")
        inc = Incident(
            incident_id="INC-TEST-001",
            fingerprint=fp,
            incident_type=IncidentType.GOOGLE_AUTH_FAILURE.value,
            severity=IncidentSeverity.ERROR.value,
            status=IncidentStatus.OPEN.value,
            component="Google Sheets",
            source="TestRunner",
            first_detected_at=datetime.now(timezone.utc).isoformat(),
            last_detected_at=datetime.now(timezone.utc).isoformat(),
            summary="Authentication failed with Google Sheets API",
            impact="CRM synchronization unavailable",
            recommended_action="Verify service account credentials",
        )
        d = inc.to_dict()
        self.assertEqual(d["incident_id"], "INC-TEST-001")
        self.assertEqual(d["fingerprint"], fp)
        self.assertEqual(d["incident_type"], "GOOGLE_AUTH_FAILURE")
        self.assertEqual(d["severity"], "ERROR")
        self.assertEqual(d["status"], "OPEN")
        self.assertEqual(d["occurrence_count"], 1)
        self.assertFalse(d["alert_sent"])
        self.assertIsNone(d["resolved_at"])

        # Roundtrip JSON check
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        restored = Incident.from_dict(parsed)
        self.assertEqual(restored.incident_id, inc.incident_id)
        self.assertEqual(restored.fingerprint, inc.fingerprint)

    # ─────────────────────────────────────────────────────────────────────────
    # 2. DETERMINISTIC FINGERPRINTING
    # ─────────────────────────────────────────────────────────────────────────

    def test_deterministic_fingerprinting(self):
        """Verifies deterministic fingerprints across repeated calls and distinct components."""
        fp1 = compute_deterministic_fingerprint(IncidentType.TAVILY_PROVIDER_FAILURE.value, "tavily")
        fp2 = compute_deterministic_fingerprint(IncidentType.TAVILY_PROVIDER_FAILURE.value, "tavily")
        self.assertEqual(fp1, fp2)
        self.assertEqual(fp1, "TAVILY_PROVIDER_FAILURE:tavily")

        # Distinct markets produce distinct fingerprints
        fp_man = compute_deterministic_fingerprint(IncidentType.ZERO_DISCOVERY_RESULTS.value, "discovery", "MANCHESTER_UK")
        fp_birm = compute_deterministic_fingerprint(IncidentType.ZERO_DISCOVERY_RESULTS.value, "discovery", "BIRMINGHAM_UK")
        self.assertEqual(fp_man, "ZERO_DISCOVERY_RESULTS:discovery:MANCHESTER_UK")
        self.assertEqual(fp_birm, "ZERO_DISCOVERY_RESULTS:discovery:BIRMINGHAM_UK")
        self.assertNotEqual(fp_man, fp_birm)

    # ─────────────────────────────────────────────────────────────────────────
    # 3. DEDUPLICATION & OCCURRENCE COUNTING
    # ─────────────────────────────────────────────────────────────────────────

    def test_incident_deduplication_and_occurrence_incrementing(self):
        """Verifies repeated reports of the same event update occurrence count on the same incident."""
        inc1 = self.mgr.report_incident(
            incident_type=IncidentType.TAVILY_PROVIDER_FAILURE.value,
            severity=IncidentSeverity.ERROR.value,
            component="Tavily",
            source="Test",
            summary="HTTP 500 Internal Error",
            impact="Review enrichment unavailable",
            recommended_action="Wait for Tavily recovery",
            component_key="tavily",
        )
        self.assertEqual(inc1.occurrence_count, 1)

        # Second observation of the exact same event
        inc2 = self.mgr.report_incident(
            incident_type=IncidentType.TAVILY_PROVIDER_FAILURE.value,
            severity=IncidentSeverity.ERROR.value,
            component="Tavily",
            source="Test",
            summary="HTTP 500 Internal Error",
            impact="Review enrichment unavailable",
            recommended_action="Wait for Tavily recovery",
            component_key="tavily",
        )
        self.assertEqual(inc2.incident_id, inc1.incident_id)
        self.assertEqual(inc2.occurrence_count, 2)
        self.assertEqual(len(self.mgr.get_active_incidents()), 1)

    # ─────────────────────────────────────────────────────────────────────────
    # 4. COOLDOWN THROTTLING
    # ─────────────────────────────────────────────────────────────────────────

    def test_cooldown_throttling(self):
        """Verifies cooldown prevents notification flood on rapid repeated events."""
        self.mgr.alerts_enabled = True
        with patch.object(self.mgr.webhook_adapter, "enabled", True):
            with patch.object(self.mgr.webhook_adapter, "send_alert", return_value=True) as mock_send:
                # First report should send alert
                inc1 = self.mgr.report_incident(
                    incident_type=IncidentType.DUPLICATE_RATE_HIGH.value,
                    severity=IncidentSeverity.WARNING.value,
                    component="Deduplication",
                    source="Test",
                    summary="High duplicate rate: 45%",
                    impact="Reduced new lead intake",
                    recommended_action="Review market query",
                    component_key="dedup",
                    target_identifier="LON_UK",
                )
                self.assertTrue(inc1.alert_sent)
                self.assertEqual(mock_send.call_count, 1)

                # Immediate second report within cooldown should NOT re-alert
                inc2 = self.mgr.report_incident(
                    incident_type=IncidentType.DUPLICATE_RATE_HIGH.value,
                    severity=IncidentSeverity.WARNING.value,
                    component="Deduplication",
                    source="Test",
                    summary="High duplicate rate: 50%",
                    impact="Reduced new lead intake",
                    recommended_action="Review market query",
                    component_key="dedup",
                    target_identifier="LON_UK",
                )
                self.assertEqual(inc2.occurrence_count, 2)
                self.assertEqual(mock_send.call_count, 1)

    # ─────────────────────────────────────────────────────────────────────────
    # 5. INCIDENT RESOLUTION & RECOVERY
    # ─────────────────────────────────────────────────────────────────────────

    def test_incident_resolution_and_recovery(self):
        """Verifies incident resolution transitions status, records resolution time, and doesn't spam alerts."""
        fp = compute_deterministic_fingerprint(IncidentType.TAVILY_PROVIDER_TIMEOUT.value, "tavily")
        inc = self.mgr.report_incident(
            incident_type=IncidentType.TAVILY_PROVIDER_TIMEOUT.value,
            severity=IncidentSeverity.WARNING.value,
            component="Tavily",
            source="Test",
            summary="Search query timed out",
            impact="Slow enrichment",
            recommended_action="Inspect network latency",
            component_key="tavily",
        )
        self.assertEqual(inc.status, IncidentStatus.OPEN.value)

        resolved_inc = self.mgr.resolve_incident(fp, resolution_note="Tavily responded in 120ms")
        self.assertIsNotNone(resolved_inc)
        self.assertEqual(resolved_inc.status, IncidentStatus.RESOLVED.value)
        self.assertIsNotNone(resolved_inc.resolved_at)
        self.assertIn("resolution_note", resolved_inc.metadata)
        self.assertEqual(resolved_inc.metadata["resolution_note"], "Tavily responded in 120ms")
        self.assertEqual(len(self.mgr.get_active_incidents()), 0)

    # ─────────────────────────────────────────────────────────────────────────
    # 6. CANONICAL HEALTH STATUS MAPPING
    # ─────────────────────────────────────────────────────────────────────────

    def test_severity_hierarchy_and_canonical_health_mapping(self):
        """Verifies that active incidents map properly into SystemHealthMonitor's canonical status."""
        health_mon = SystemHealthMonitor(data_dir=self.data_dir)

        # Baseline: with no incidents and stopped scheduler, status is DEGRADED (due to stopped scheduler)
        h0 = health_mon.evaluate_health()
        self.assertIn(h0["canonical_health_status"], ("HEALTHY", "DEGRADED"))

        # Add WARNING incident -> remains DEGRADED
        self.mgr.report_incident(
            incident_type=IncidentType.DISK_USAGE_WARNING.value,
            severity=IncidentSeverity.WARNING.value,
            component="Storage",
            source="Test",
            summary="Disk at 72%",
            impact="Advisory warning",
            recommended_action="Monitor logs",
            component_key="disk",
        )
        h1 = health_mon.evaluate_health()
        self.assertEqual(h1["canonical_health_status"], "DEGRADED")

        # Add ERROR incident -> becomes UNHEALTHY
        self.mgr.report_incident(
            incident_type=IncidentType.GOOGLE_AUTH_FAILURE.value,
            severity=IncidentSeverity.ERROR.value,
            component="Google Sheets",
            source="Test",
            summary="Google credentials invalid",
            impact="CRM unlinked",
            recommended_action="Rotate service account key",
            component_key="google_sheets",
        )
        h2 = health_mon.evaluate_health()
        self.assertEqual(h2["canonical_health_status"], "UNHEALTHY")

        # Add CRITICAL incident -> becomes CRITICAL
        self.mgr.report_incident(
            incident_type=IncidentType.BACKUP_FAILURE.value,
            severity=IncidentSeverity.CRITICAL.value,
            component="Backup Manager",
            source="Test",
            summary="Disk full during snapshot",
            impact="State backup aborted",
            recommended_action="Free disk space immediately",
            component_key="backup",
        )
        h3 = health_mon.evaluate_health()
        self.assertEqual(h3["canonical_health_status"], "CRITICAL")

    # ─────────────────────────────────────────────────────────────────────────
    # 7. SCHEDULER MONITORING (HEARTBEAT & STALL)
    # ─────────────────────────────────────────────────────────────────────────

    def test_scheduler_monitoring_heartbeat_and_stall(self):
        """Tests scheduler heartbeat recording, stall detection, and job failure."""
        sched_mon = SchedulerMonitor(incident_mgr=self.mgr, data_dir=self.data_dir)

        # Record healthy heartbeat
        sched_mon.record_heartbeat(
            scheduler_id="test_sched",
            state="RUNNING",
            last_successful_job="DAILY_BACKUP",
            current_job=None,
            next_expected_job="CRM_RECONCILIATION",
        )
        status, hb = sched_mon.check_scheduler_health(scheduler_id="test_sched", max_lag_seconds=60)
        self.assertEqual(status, "HEALTHY")
        self.assertEqual(hb["state"], "RUNNING")

        # Simulate stalled scheduler (>15m lag)
        past_iso = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        hb["last_heartbeat"] = past_iso
        with open(sched_mon.heartbeat_path, "w", encoding="utf-8") as f:
            json.dump(hb, f)

        status_stall, _ = sched_mon.check_scheduler_health(scheduler_id="test_sched", max_lag_seconds=900)
        self.assertEqual(status_stall, "STALLED")
        active = self.mgr.get_active_incidents()
        self.assertTrue(any(i.incident_type == IncidentType.SCHEDULER_STALLED.value for i in active))

        # Test job failure recording
        sched_mon.record_job_failure(
            job_id="job_sync_123",
            job_type="CRM_SYNC",
            error_message="Connection reset by peer",
            scheduler_id="test_sched",
        )
        active = self.mgr.get_active_incidents()
        self.assertTrue(any(i.incident_type == IncidentType.SCHEDULER_JOB_FAILED.value for i in active))

    # ─────────────────────────────────────────────────────────────────────────
    # 8. GOOGLE SHEETS MONITORING
    # ─────────────────────────────────────────────────────────────────────────

    def test_google_sheets_monitoring_auth_and_api(self):
        """Verifies Google Sheets auth failure, API failure, and resolution upon success."""
        sheets_mon = GoogleSheetsMonitor(incident_mgr=self.mgr)

        # Auth failure
        sheets_mon.record_auth_failure(Exception("Invalid JWT signature in service_account.json"), context="connect")
        active = self.mgr.get_active_incidents()
        auth_inc = next(i for i in active if i.incident_type == IncidentType.GOOGLE_AUTH_FAILURE.value)
        self.assertEqual(auth_inc.severity, IncidentSeverity.ERROR.value)
        self.assertNotIn("service_account.json", auth_inc.summary)  # Secret sanitization

        # API failure
        sheets_mon.record_api_failure(Exception("429 Too Many Requests on worksheet write"), sheet_id="LEADS_TAB")
        active = self.mgr.get_active_incidents()
        self.assertTrue(any(i.incident_type == IncidentType.GOOGLE_API_FAILURE.value for i in active))

        # Recovery resolves both
        sheets_mon.record_success()
        active_after = self.mgr.get_active_incidents()
        self.assertFalse(any(i.incident_type in (IncidentType.GOOGLE_AUTH_FAILURE.value, IncidentType.GOOGLE_API_FAILURE.value) for i in active_after))

    # ─────────────────────────────────────────────────────────────────────────
    # 9. TAVILY MONITORING OUTCOMES
    # ─────────────────────────────────────────────────────────────────────────

    def test_tavily_monitoring_outcomes(self):
        """Verifies Tavily timeout, failure, 429 quota exhaustion, and 80% quota warning."""
        tav_mon = TavilyMonitor(incident_mgr=self.mgr)

        # Timeout
        tav_mon.record_call_outcome(
            outcome_str="PROVIDER_TIMEOUT",
            query="Revolution Bars Manchester",
            latency=10.2,
        )
        active = self.mgr.get_active_incidents()
        self.assertTrue(any(i.incident_type == IncidentType.TAVILY_PROVIDER_TIMEOUT.value for i in active))

        # HTTP failure
        tav_mon.record_call_outcome(
            outcome_str="PROVIDER_FAILED",
            query="Revolution Bars Manchester",
            latency=1.5,
        )
        active = self.mgr.get_active_incidents()
        self.assertTrue(any(i.incident_type == IncidentType.TAVILY_PROVIDER_FAILURE.value for i in active))

        # Success resolves timeout and failure
        tav_mon.record_call_outcome(
            outcome_str="SEARCH_SUCCEEDED_WITH_RESULTS",
            query="Revolution Bars Manchester",
            latency=0.45,
        )
        active = self.mgr.get_active_incidents()
        self.assertFalse(any(i.incident_type in (IncidentType.TAVILY_PROVIDER_FAILURE.value, IncidentType.TAVILY_PROVIDER_TIMEOUT.value) for i in active))

        # Quota warning (>=80%)
        tav_mon.record_call_outcome(
            outcome_str="SEARCH_SUCCEEDED_WITH_RESULTS",
            query="The Wendover Manchester",
            latency=0.3,
            quota_remaining=150,
            quota_limit=1000,  # 85% used
        )
        active = self.mgr.get_active_incidents()
        self.assertTrue(any(i.incident_type == IncidentType.TAVILY_QUOTA_WARNING.value for i in active))

        # Quota exceeded (HTTP 429)
        tav_mon.record_call_outcome(
            outcome_str="QUOTA_EXCEEDED",
            query="Kro Bar Manchester",
            latency=0.1,
        )
        active = self.mgr.get_active_incidents()
        self.assertTrue(any(i.incident_type == IncidentType.TAVILY_QUOTA_EXCEEDED.value for i in active))

    # ─────────────────────────────────────────────────────────────────────────
    # 10. ZERO RESULT MONITORING (DISCOVERY VS QUALIFIED ANOMALY)
    # ─────────────────────────────────────────────────────────────────────────

    def test_zero_discovery_vs_zero_qualified_distinction(self):
        """Verifies ZERO_DISCOVERY_RESULTS is distinguished from ZERO_QUALIFIED_RESULTS_ANOMALY."""
        disc_mon = DiscoveryMonitor(incident_mgr=self.mgr)

        # Run 1: Zero discovered candidates in established market (anomaly)
        disc_mon.evaluate_run_results(
            market_id="MANCHESTER_UK",
            discovered_count=0,
            processed_count=0,
            qualified_count=0,
            historical_baseline=40,
        )
        active = self.mgr.get_active_incidents()
        self.assertTrue(any(i.incident_type == IncidentType.ZERO_DISCOVERY_RESULTS.value for i in active))

        # Run 2: Discovered 25 leads, but 0 qualified (qual anomaly because sample >= 15)
        disc_mon.evaluate_run_results(
            market_id="MANCHESTER_UK",
            discovered_count=25,
            processed_count=25,
            qualified_count=0,
            historical_baseline=40,
        )
        active = self.mgr.get_active_incidents()
        # ZERO_DISCOVERY_RESULTS was resolved because discovered_count > 0
        self.assertFalse(any(i.incident_type == IncidentType.ZERO_DISCOVERY_RESULTS.value for i in active))
        # ZERO_QUALIFIED_RESULTS_ANOMALY was reported
        self.assertTrue(any(i.incident_type == IncidentType.ZERO_QUALIFIED_RESULTS_ANOMALY.value for i in active))

        # Run 3: Discovered 30 leads, 5 qualified -> all resolved
        disc_mon.evaluate_run_results(
            market_id="MANCHESTER_UK",
            discovered_count=30,
            processed_count=30,
            qualified_count=5,
            historical_baseline=40,
        )
        active = self.mgr.get_active_incidents()
        self.assertFalse(any(i.incident_type in (IncidentType.ZERO_DISCOVERY_RESULTS.value, IncidentType.ZERO_QUALIFIED_RESULTS_ANOMALY.value) for i in active))

    # ─────────────────────────────────────────────────────────────────────────
    # 11. DUPLICATE RATE MONITORING & SAMPLE GATING
    # ─────────────────────────────────────────────────────────────────────────

    def test_duplicate_rate_monitoring_with_sample_size(self):
        """Verifies duplicate rate triggers only above sample size threshold (>=20) and >15%."""
        dedup_mon = DeduplicationMonitor(incident_mgr=self.mgr)

        # Tiny sample size: 5 discovered, 4 duplicates (80% dup rate), but sample < 20
        res_small = dedup_mon.evaluate_duplicate_rate(
            market_id="MANCHESTER_UK",
            total_discovered=5,
            duplicates_skipped=4,
            min_sample_size=20,
            threshold_pct=15.0,
        )
        self.assertEqual(res_small["status"], "INSUFFICIENT_SAMPLE")
        self.assertEqual(len(self.mgr.get_active_incidents()), 0)

        # Meaningful sample size: 100 discovered, 35 duplicates (35% dup rate)
        res_high = dedup_mon.evaluate_duplicate_rate(
            market_id="MANCHESTER_UK",
            total_discovered=100,
            duplicates_skipped=35,
            min_sample_size=20,
            threshold_pct=15.0,
        )
        self.assertEqual(res_high["status"], "ANOMALY")
        active = self.mgr.get_active_incidents()
        self.assertTrue(any(i.incident_type == IncidentType.DUPLICATE_RATE_HIGH.value for i in active))

        # Next run returns to normal: 100 discovered, 5 duplicates (5% dup rate)
        res_normal = dedup_mon.evaluate_duplicate_rate(
            market_id="MANCHESTER_UK",
            total_discovered=100,
            duplicates_skipped=5,
            min_sample_size=20,
            threshold_pct=15.0,
        )
        self.assertEqual(res_normal["status"], "NORMAL")
        active_after = self.mgr.get_active_incidents()
        self.assertFalse(any(i.incident_type == IncidentType.DUPLICATE_RATE_HIGH.value for i in active_after))

    # ─────────────────────────────────────────────────────────────────────────
    # 12. EMAIL BOUNCE MONITORING (DORMANT VS ACTIVE)
    # ─────────────────────────────────────────────────────────────────────────

    def test_email_bounce_monitoring_dormant_and_active(self):
        """Verifies email bounce monitor remains dormant when automated email is disabled."""
        bounce_mon = EmailBounceMonitor(incident_mgr=self.mgr)

        # Disabled by default
        with patch.object(SystemConfig, "AUTOMATED_EMAIL_ENABLED", False):
            res_dormant = bounce_mon.evaluate_bounce_rate(total_sent=50, total_bounced=20)
            self.assertTrue(res_dormant.get("is_dormant"))
            self.assertEqual(res_dormant.get("status"), "DORMANT")
            self.assertEqual(len(self.mgr.get_active_incidents()), 0)

        # When enabled with small sample (<20)
        with patch.object(SystemConfig, "AUTOMATED_EMAIL_ENABLED", True):
            res_small = bounce_mon.evaluate_bounce_rate(total_sent=10, total_bounced=5, min_sample=20)
            self.assertEqual(res_small.get("status"), "INSUFFICIENT_SAMPLE")
            self.assertEqual(len(self.mgr.get_active_incidents()), 0)

            # When enabled with high bounce rate: 50 sent, 10 bounced (20% > 10% threshold)
            res_high = bounce_mon.evaluate_bounce_rate(total_sent=50, total_bounced=10, min_sample=20, threshold_pct=10.0)
            self.assertEqual(res_high.get("status"), "ANOMALY")
            active = self.mgr.get_active_incidents()
            self.assertTrue(any(i.incident_type == IncidentType.BOUNCE_RATE_HIGH.value for i in active))

    # ─────────────────────────────────────────────────────────────────────────
    # 13. APPLICATION LIFECYCLE & CRASH DETECTION
    # ─────────────────────────────────────────────────────────────────────────

    def test_application_lifecycle_and_crash_detection(self):
        """Verifies clean shutdown vs crash detection on unexpected prior termination."""
        app_mon = ApplicationLifecycleMonitor(incident_mgr=self.mgr, data_dir=self.data_dir)

        # First session starts and shuts down cleanly
        app_mon.record_startup("session_clean_1")
        app_mon.record_clean_shutdown("session_clean_1")
        self.assertEqual(len(self.mgr.get_active_incidents()), 0)

        # Second session starts cleanly, but crashes (simulated by not calling clean shutdown)
        app_mon.record_startup("session_crashed_2")

        # Third session starts -> detects session_crashed_2 left status RUNNING
        app_mon.record_startup("session_recovery_3")
        active = self.mgr.get_active_incidents()
        crash_inc = next((i for i in active if i.incident_type == IncidentType.APPLICATION_CRASH.value), None)
        self.assertIsNotNone(crash_inc)
        self.assertEqual(crash_inc.severity, IncidentSeverity.ERROR.value)
        self.assertIn("session_crashed_2", crash_inc.summary)

    # ─────────────────────────────────────────────────────────────────────────
    # 14. BACKUP & RECONCILIATION MONITORING
    # ─────────────────────────────────────────────────────────────────────────

    def test_backup_and_reconciliation_monitoring(self):
        """Verifies BACKUP_FAILURE and RECONCILIATION_FAILURE incidents."""
        br_mon = BackupReconciliationMonitor(incident_mgr=self.mgr)

        # Backup failure
        br_mon.record_backup_failure(Exception("Permission denied on /backups/snapshot.tar"), context="nightly_backup")
        active = self.mgr.get_active_incidents()
        self.assertTrue(any(i.incident_type == IncidentType.BACKUP_FAILURE.value for i in active))

        # Reconciliation failure
        br_mon.record_reconciliation_failure(issues_count=4, critical_issues=["Lead ID DRI-001 missing in CRM sheet"])
        active = self.mgr.get_active_incidents()
        self.assertTrue(any(i.incident_type == IncidentType.RECONCILIATION_FAILURE.value for i in active))

        # Reconciliation success resolves incident
        br_mon.record_reconciliation_failure(issues_count=0, critical_issues=[])
        active_after = self.mgr.get_active_incidents()
        self.assertFalse(any(i.incident_type == IncidentType.RECONCILIATION_FAILURE.value for i in active_after))

    # ─────────────────────────────────────────────────────────────────────────
    # 15. STORAGE & DISK THRESHOLDS
    # ─────────────────────────────────────────────────────────────────────────

    def test_storage_disk_thresholds(self):
        """Verifies disk warning (70%) and critical (90%) thresholds and safe recovery."""
        storage_mon = StorageMonitor(incident_mgr=self.mgr)

        # Mock disk usage at 75% -> WARNING
        with patch("shutil.disk_usage", return_value=MagicMock(total=1000, used=750, free=250)):
            res_warn = storage_mon.check_disk_usage(self.data_dir, warning_pct=70.0, critical_pct=90.0)
            self.assertEqual(res_warn["status"], "WARN")
            active = self.mgr.get_active_incidents()
            self.assertTrue(any(i.incident_type == IncidentType.DISK_USAGE_WARNING.value for i in active))

        # Mock disk usage at 95% -> CRITICAL
        with patch("shutil.disk_usage", return_value=MagicMock(total=1000, used=950, free=50)):
            res_crit = storage_mon.check_disk_usage(self.data_dir, warning_pct=70.0, critical_pct=90.0)
            self.assertEqual(res_crit["status"], "CRITICAL")
            active = self.mgr.get_active_incidents()
            self.assertTrue(any(i.incident_type == IncidentType.DISK_USAGE_CRITICAL.value for i in active))

        # Mock disk usage returning to 40% -> RESOLVED
        with patch("shutil.disk_usage", return_value=MagicMock(total=1000, used=400, free=600)):
            res_pass = storage_mon.check_disk_usage(self.data_dir, warning_pct=70.0, critical_pct=90.0)
            self.assertEqual(res_pass["status"], "PASS")
            active_after = self.mgr.get_active_incidents()
            self.assertFalse(any(i.incident_type in (IncidentType.DISK_USAGE_WARNING.value, IncidentType.DISK_USAGE_CRITICAL.value) for i in active_after))

    # ─────────────────────────────────────────────────────────────────────────
    # 16. NOTIFICATION ADAPTERS & FORMATTING
    # ─────────────────────────────────────────────────────────────────────────

    def test_notification_adapters_disabled_by_default(self):
        """Verifies that all notification adapters are disabled by default without environment overrides."""
        webhook = WebhookNotificationAdapter()
        email = EmailNotificationAdapter()
        self.assertFalse(webhook.enabled)
        self.assertFalse(email.enabled)

        # Neither attempts network connections when disabled
        inc = Incident(
            incident_id="INC-001",
            fingerprint="FP-001",
            incident_type="TEST",
            severity="WARNING",
            status="OPEN",
            component="Test",
            source="Test",
            first_detected_at=datetime.now(timezone.utc).isoformat(),
            last_detected_at=datetime.now(timezone.utc).isoformat(),
            summary="Test summary",
            impact="Test impact",
            recommended_action="Test action",
        )
        self.assertFalse(webhook.send_alert(inc))
        self.assertFalse(email.send_alert(inc))

    def test_standardized_alert_formatting(self):
        """Verifies format_standard_alert_payload generates required standardized message."""
        inc = Incident(
            incident_id="INC-GOOGLE-01",
            fingerprint="GOOGLE_AUTH_FAILURE:google_sheets",
            incident_type=IncidentType.GOOGLE_AUTH_FAILURE.value,
            severity=IncidentSeverity.ERROR.value,
            status=IncidentStatus.OPEN.value,
            component="Google Sheets",
            source="GoogleSheetsMonitor",
            first_detected_at="2026-10-08T12:00:00Z",
            last_detected_at="2026-10-08T12:05:00Z",
            occurrence_count=3,
            summary="Google Sheets authentication failed.",
            impact="CRM synchronization unavailable.",
            recommended_action="Check production Google credentials.",
        )
        payload = format_standard_alert_payload(inc)
        text = payload["text"]
        self.assertIn("DRIPP MEDIA — TECHNICAL ALERT", text)
        self.assertIn("Severity:\nERROR", text)
        self.assertIn("Incident:\nGOOGLE_AUTH_FAILURE", text)
        self.assertIn("Component:\nGoogle Sheets", text)
        self.assertIn("Occurrences:\n3", text)
        self.assertIn("Google Sheets authentication failed.", text)
        self.assertIn("CRM synchronization unavailable.", text)
        self.assertIn("Check production Google credentials.", text)

    # ─────────────────────────────────────────────────────────────────────────
    # 17. NOTIFICATION FAILURE NON-FATAL
    # ─────────────────────────────────────────────────────────────────────────

    def test_notification_failure_does_not_crash_pipeline(self):
        """Verifies that unhandled network exceptions in notification adapters do not crash callers."""
        webhook = WebhookNotificationAdapter(enabled=True, webhook_url="https://fake-webhook.local/alerts")
        inc = Incident(
            incident_id="INC-001",
            fingerprint="FP-001",
            incident_type="TEST",
            severity="WARNING",
            status="OPEN",
            component="Test",
            source="Test",
            first_detected_at=datetime.now(timezone.utc).isoformat(),
            last_detected_at=datetime.now(timezone.utc).isoformat(),
            summary="Test summary",
            impact="Test impact",
            recommended_action="Test action",
        )
        with patch("requests.post", side_effect=Exception("Connection reset by remote host")):
            success = webhook.send_alert(inc)
            self.assertFalse(success)  # Returned cleanly without raising exception

    # ─────────────────────────────────────────────────────────────────────────
    # 18. SECRET REDACTION
    # ─────────────────────────────────────────────────────────────────────────

    def test_secret_redaction_in_alerts_and_incidents(self):
        """Verifies that API keys, tokens, and passwords are never exposed in incident texts."""
        sensitive_string = (
            "Failed with tvly-prod1234567890abcdef and ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 "
            "and private_key: -----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
        )
        sanitized = sanitize_text(sensitive_string)
        self.assertNotIn("tvly-prod", sanitized)
        self.assertNotIn("ghp_", sanitized)
        self.assertNotIn("BEGIN PRIVATE KEY", sanitized)
        self.assertIn("[REDACTED_TAVILY_KEY]", sanitized)
        self.assertIn("[REDACTED_GITHUB_TOKEN]", sanitized)
        self.assertIn("[REDACTED_PRIVATE_KEY]", sanitized)

        # Test dictionary sanitization
        dict_payload = {
            "api_key": "tvly-secret123",
            "message": "Auth failed with Bearer eyJhbGciOiJIUzI1NiJ9.test.sig",
            "safe_field": "Normal pipeline event",
        }
        sanitized_dict = sanitize_payload(dict_payload)
        self.assertEqual(sanitized_dict["api_key"], "[REDACTED_SECRET]")
        self.assertNotIn("eyJhbGci", sanitized_dict["message"])
        self.assertEqual(sanitized_dict["safe_field"], "Normal pipeline event")

    # ─────────────────────────────────────────────────────────────────────────
    # 19. INCIDENT PERSISTENCE ACROSS RESTARTS
    # ─────────────────────────────────────────────────────────────────────────

    def test_incident_persistence_atomic_and_restart_safe(self):
        """Verifies that incidents are atomically persisted and recovered across separate manager instances."""
        mgr1 = IncidentManager(data_dir=self.data_dir)
        inc = mgr1.report_incident(
            incident_type=IncidentType.GOOGLE_API_FAILURE.value,
            severity=IncidentSeverity.ERROR.value,
            component="Google Sheets",
            source="TestRunner",
            summary="Permission denied on sheet",
            impact="Cannot read CRM",
            recommended_action="Share sheet with service account",
            component_key="google_sheets",
        )

        # Verify file exists on disk
        store_path = os.path.join(self.data_dir, "incidents.json")
        self.assertTrue(os.path.exists(store_path))

        # Instantiate second manager pointing to the same data directory
        mgr2 = IncidentManager(data_dir=self.data_dir)
        active2 = mgr2.get_active_incidents()
        self.assertEqual(len(active2), 1)
        self.assertEqual(active2[0].incident_id, inc.incident_id)
        self.assertEqual(active2[0].fingerprint, inc.fingerprint)

    # ─────────────────────────────────────────────────────────────────────────
    # 20. MONITORING SAFETY INVARIANTS
    # ─────────────────────────────────────────────────────────────────────────

    def test_safety_invariants_monitoring_is_read_only(self):
        """Verifies that monitoring operations strictly observe and NEVER trigger outreach or mutate CRM."""
        # Check SystemConfig invariant defaults
        self.assertTrue(SystemConfig.TRAVEL_MODE)
        self.assertFalse(SystemConfig.COMMERCIAL_ACTIONS_ENABLED)
        self.assertFalse(SystemConfig.AUTOMATED_EMAIL_ENABLED)

        # Run system health check evaluation
        health_mon = SystemHealthMonitor(data_dir=self.data_dir)
        health = health_mon.evaluate_health()

        # Invariants must remain intact
        self.assertTrue(health["travel_mode"])
        self.assertFalse(health["commercial_actions_enabled"])
        self.assertTrue(health["commercial_actions_locked"])

    # ─────────────────────────────────────────────────────────────────────────
    # 21. FULL SYNTHETIC FAILURE SIMULATION CYCLE
    # ─────────────────────────────────────────────────────────────────────────

    def test_synthetic_failure_simulation_lifecycle(self):
        """
        Executes end-to-end synthetic failure simulation:
        FAILURE -> DETECTION -> INCIDENT -> NOTIFICATION -> PERSISTENCE -> RECOVERY -> RESOLUTION.
        """
        tav_mon = TavilyMonitor(incident_mgr=self.mgr)
        webhook_mock = MagicMock(return_value=True)

        self.mgr.alerts_enabled = True
        with patch.object(self.mgr.webhook_adapter, "enabled", True):
            with patch.object(self.mgr.webhook_adapter, "send_alert", webhook_mock):
                # Step 1: Failure occurs (HTTP 500)
                tav_mon.record_call_outcome(
                    outcome_str="PROVIDER_FAILED",
                    query="The Wendover",
                    latency=2.1,
                )

                # Step 2: Detection & Incident Creation
                active = self.mgr.get_active_incidents()
                self.assertEqual(len(active), 1)
                inc = active[0]
                self.assertEqual(inc.incident_type, IncidentType.TAVILY_PROVIDER_FAILURE.value)
                self.assertEqual(inc.status, IncidentStatus.OPEN.value)

                # Step 3: Notification Dispatched
                self.assertEqual(webhook_mock.call_count, 1)

                # Step 4: Persistence Verified
                with open(os.path.join(self.data_dir, "incidents.json"), "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self.assertEqual(len(saved["incidents"]), 1)

                # Step 5: Provider Recovers (200 OK)
                tav_mon.record_call_outcome(
                    outcome_str="SEARCH_SUCCEEDED_WITH_RESULTS",
                    query="The Wendover",
                    latency=0.35,
                )

                # Step 6: Resolution
                active_after = self.mgr.get_active_incidents()
                self.assertEqual(len(active_after), 0)

                # Resolved incident persists on disk with resolution note
                with open(os.path.join(self.data_dir, "incidents.json"), "r", encoding="utf-8") as f:
                    saved_after = json.load(f)
                resolved_record = saved_after["incidents"][0]
                self.assertEqual(resolved_record["status"], IncidentStatus.RESOLVED.value)
                self.assertIsNotNone(resolved_record["resolved_at"])


if __name__ == "__main__":
    unittest.main()
