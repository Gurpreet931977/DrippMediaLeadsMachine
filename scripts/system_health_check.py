#!/usr/bin/env python3
"""
scripts/system_health_check.py
==============================
Command-line technical health audit for Dripp Media Lead Engine.

Runs:
  1. Schema validation & file integrity
  2. Identity consistency (canonical IDs, duplicate detection, branch protection)
  3. State consistency (reconciliation invariants)
  4. Quota consistency & consumption
  5. Scheduler status
  6. Backup validation (integrity & checksum)
  7. Commercial kill switch status (Travel mode enforcement)
  8. Technical automation status

Output:
  PASS / WARN / FAIL with actionable explanations.
"""

import sys
import os
import json
from datetime import datetime, timezone

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.system.system_config import SystemConfig
from lib.system.system_health import SystemHealthMonitor
from lib.system.config_validator import ConfigValidator
from lib.system.backup_manager import BackupManager
from lib.system.quota_governor import QuotaGovernor
from lib.system.reconciliation_engine import ReconciliationEngine
from lib.system.identity_integrity import IdentityIntegrityAuditor


def run_health_check(verbose: bool = True) -> int:
    print("=" * 72)
    print("  DRIPP MEDIA LEAD ENGINE — SYSTEM HEALTH AUDIT")
    print(f"  Time (UTC): {datetime.now(timezone.utc).isoformat()}")
    print("=" * 72)

    from lib.system.storage_bootstrap import bootstrap_storage_baseline
    data_dir = os.path.join(PROJECT_ROOT, "data")
    bootstrap_storage_baseline(data_dir=data_dir)

    monitor = SystemHealthMonitor(data_dir=data_dir)
    health = monitor.evaluate_health()
    config_audit = ConfigValidator.validate_all()

    issues = []
    warnings = []

    # 1. File Integrity & Schema
    file_info = health["file_integrity"]
    print("\n[1/8] File Integrity & Storage:")
    if file_info["status"] == "PASS":
        print("  ✓ All critical JSON data stores exist and parse successfully.")
    else:
        print("  ✗ File integrity failure:")
        for fname, fstat in file_info["files"].items():
            if not fstat.get("valid_json"):
                err = fstat.get("error", "File missing or unreadable")
                print(f"    - {fname}: {err}")
                issues.append(f"Corrupted or missing data store: {fname} ({err})")

    # 2. Identity Consistency
    data_quality = health["data_quality_status"]
    print("\n[2/8] Identity Consistency & Canonical Integrity:")
    print(f"  Audited {data_quality.get('total_canonical_leads', 0)} canonical leads.")
    if data_quality["status"] == "PASS":
        print("  ✓ Zero duplicate IDs, branch separation intact, no research ID leakage.")
    elif data_quality["status"] == "WARN":
        print(f"  ⚠ Warnings detected: {data_quality.get('issues_count', 0)} issues.")
        warnings.append(f"Identity audit found {data_quality.get('issues_count')} minor warnings.")
    else:
        print(f"  ✗ Identity failure: {data_quality.get('issues_count', 0)} critical issues.")
        issues.append("Canonical identity integrity compromised.")

    # 3. State Consistency & Reconciliation
    reconcile = health["reconciliation_status"]
    print("\n[3/8] State Consistency & Cross-Store Reconciliation:")
    print(f"  Issues detected: {reconcile.get('issues_count', 0)}")
    if reconcile["status"] == "PASS":
        print("  ✓ All state invariants hold across CRM, Outreach, and Commercial records.")
    elif reconcile["status"] == "WARN":
        print(f"  ⚠ Reconciler warnings: {reconcile.get('issues_count')} discrepancies.")
        warnings.append(f"Reconciliation detected {reconcile.get('issues_count')} non-critical discrepancies.")
    else:
        print(f"  ✗ Critical state reconciliation contradictions found.")
        issues.append("Cross-store state contradictions detected.")

    # 4. Quota Consistency
    quota = health["quota_status"]
    print("\n[4/8] Quota Governance:")
    print(f"  Quota Status: {quota.get('status')}")
    for res_name, res_usage in quota.get("quotas", {}).items():
        used = res_usage.get("used", 0)
        limit = res_usage.get("limit", 0)
        pct = (used / limit * 100) if limit > 0 else 0
        print(f"  - {res_name:<18}: {used:>4} / {limit:>4} ({pct:5.1f}%)")
    if quota.get("status") == "QUOTA_EXHAUSTED":
        warnings.append("One or more technical operation quotas are currently exhausted.")

    # 5. Scheduler Status
    sched = health["scheduler_status"]
    print("\n[5/8] Technical Scheduler:")
    print(f"  Status: {'RUNNING' if sched.get('running') else 'STOPPED'}")
    print(f"  Configured Tasks: {len(sched.get('tasks', []))}")
    for task in sched.get("tasks", []):
        last_run = task.get("last_run") or "NEVER"
        print(f"  - {task.get('task_id'):<26}: Interval={task.get('interval_seconds')}s, Last={last_run}")

    # 6. Backup Validation
    backup_stat = health["backup_status"]
    print("\n[6/8] Backup & Recovery Validation:")
    print(f"  Total Backups: {backup_stat.get('total_backups', 0)}")
    if backup_stat.get("total_backups", 0) > 0:
        latest_id = backup_stat.get("latest_backup_id")
        latest_time = backup_stat.get("latest_backup_time")
        print(f"  Latest Backup: {latest_id} ({latest_time})")
        # Validate checksum of latest backup
        bm = BackupManager(data_dir=os.path.join(PROJECT_ROOT, "data"))
        ver = bm.verify_backup(latest_id)
        if ver.get("status") == "PASS":
            print("  ✓ Latest backup checksum and manifest successfully verified.")
        else:
            print(f"  ✗ Backup verification failed for {latest_id}: {ver.get('errors')}")
            issues.append(f"Latest backup {latest_id} is corrupt.")
    else:
        print("  ⚠ No backups found. Generating baseline snapshot...")
        bm = BackupManager(data_dir=os.path.join(PROJECT_ROOT, "data"))
        snap = bm.create_backup("baseline_health_check")
        print(f"  ✓ Baseline backup created: {snap.get('backup_id')}")

    # 7. Commercial Kill Switch (Travel Mode)
    print("\n[7/8] Commercial Action Kill Switch:")
    travel_mode = health.get("travel_mode", True)
    commercial_enabled = health.get("commercial_actions_enabled", False)
    commercial_locked = health.get("commercial_actions_locked", True)
    print(f"  TRAVEL_MODE                  : {'ACTIVE (Operator Travelling)' if travel_mode else 'INACTIVE'}")
    print(f"  COMMERCIAL_ACTIONS_ENABLED   : {commercial_enabled}")
    print(f"  COMMERCIAL ACTIONS LOCKED    : {'LOCKED (Safety Enforced)' if commercial_locked else 'UNLOCKED'}")
    if not commercial_locked:
        warnings.append("Safety Warning: Commercial actions are currently UNLOCKED.")

    # 8. Technical Automation Status
    print("\n[8/8] Technical Automation Subsystems:")
    tech_enabled = health.get("technical_automation_enabled", True)
    print(f"  TECHNICAL_AUTOMATION_ENABLED : {tech_enabled}")
    print(f"  DISCOVERY_ENABLED            : {SystemConfig.DISCOVERY_ENABLED}")
    print(f"  ENRICHMENT_ENABLED           : {SystemConfig.ENRICHMENT_ENABLED}")
    print(f"  REVIEW_REFRESH_ENABLED       : {SystemConfig.REVIEW_REFRESH_ENABLED}")
    print(f"  CONTACTABILITY_ENABLED       : {SystemConfig.CONTACTABILITY_REFRESH_ENABLED}")
    print(f"  ANALYTICS_ENABLED            : {SystemConfig.ANALYTICS_ENABLED}")
    print(f"  BACKUP_ENABLED               : {SystemConfig.BACKUP_ENABLED}")

    # Overall Verdict
    print("\n" + "=" * 72)
    overall_status = "PASS"
    if issues:
        overall_status = "FAIL"
    elif warnings:
        overall_status = "WARN"

    print(f"  OVERALL SYSTEM STATUS: [ {overall_status} ]")
    print(f"  SYSTEM HEALTH STATE  : {health['system_status']}")
    print("=" * 72)

    if issues:
        print("\nCRITICAL ISSUES TO RESOLVE:")
        for idx, iss in enumerate(issues, 1):
            print(f"  {idx}. {iss}")

    if warnings:
        print("\nADVISORY WARNINGS:")
        for idx, w in enumerate(warnings, 1):
            print(f"  {idx}. {w}")

    print("\nACTIONABLE EXPLANATIONS:")
    if overall_status == "PASS":
        print("  - System is in a fully hardened state.")
        print("  - Unattended technical operations can safely execute.")
        print("  - Commercial actions remain strictly disabled under Travel Mode.")
    elif overall_status == "WARN":
        print("  - System is operational (degraded or advisory conditions present).")
        print("  - Review quota exhaustion or minor reconciliation warnings before launching batch jobs.")
    else:
        print("  - System cannot operate unattended. Address critical file or state errors immediately.")

    print("=" * 72 + "\n")

    # Persist structured health report for artifacts
    report_data = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "overall_status": overall_status,
        "system_health_state": health["system_status"],
        "operating_mode": "TRAVEL_MODE" if SystemConfig.TRAVEL_MODE else "STANDARD",
        "travel_mode": SystemConfig.TRAVEL_MODE,
        "commercial_actions_enabled": SystemConfig.COMMERCIAL_ACTIONS_ENABLED,
        "critical_issues": issues,
        "warnings": warnings,
        "details": health,
    }

    out_dir = os.path.join(data_dir, "market_runs")
    os.makedirs(out_dir, exist_ok=True)
    report_file = os.path.join(data_dir, "latest_health_report.json")
    with open(report_file, "w", encoding="utf-8") as rf:
        json.dump(report_data, rf, indent=2)

    md_summary = f"""# System Health Audit Summary

- **Timestamp (UTC):** `{report_data['timestamp_utc']}`
- **Overall Status:** `[{overall_status}]`
- **Health State:** `{health['system_status']}`
- **Mode:** `{'TRAVEL_MODE (Safe)' if SystemConfig.TRAVEL_MODE else 'STANDARD'}`
- **Commercial Actions Locked:** `{'YES' if not SystemConfig.can_execute_commercial_actions() else 'NO'}`

## Issues & Warnings
- **Critical Issues:** `{len(issues)}`
- **Advisory Warnings:** `{len(warnings)}`
"""
    md_file = os.path.join(data_dir, "latest_health_summary.md")
    with open(md_file, "w", encoding="utf-8") as mf:
        mf.write(md_summary)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary and os.path.exists(step_summary):
        try:
            with open(step_summary, "a", encoding="utf-8") as sf:
                sf.write(md_summary + "\n")
        except Exception:
            pass

    return 0 if overall_status in ("PASS", "WARN") else 1


if __name__ == "__main__":
    sys.exit(run_health_check())
