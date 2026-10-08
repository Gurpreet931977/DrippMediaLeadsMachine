#!/usr/bin/env python3
"""
scripts/production_preflight.py
===============================
Canonical Production Readiness & Pre-flight Verification Script (Phase 10.6).

Audits system readiness against 20 deterministic gates:
  - Runtime mode
  - Travel mode & commercial action state
  - Automated email & cron state
  - Google & Tavily provider configurations
  - Storage & backup health
  - Monitoring & Rule B integrity
  - Secret scan & regression status
  - Comprehensive readiness gates table

Exits 0 if all required gates pass (or system is TECHNICALLY_READY / STAGING_READY).
Exits 1 if any required gate is BLOCKED.
"""

import os
import sys
import json
from datetime import datetime

# Add project root to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.system.system_config import SystemConfig
from lib.system.runtime_mode import RuntimeModeManager
from lib.system.production_readiness import ProductionReadinessAuditor, ReadinessState


def run_preflight() -> int:
    print("=" * 76)
    print("  DRIPP MEDIA — SYSTEM PRE-FLIGHT & PRODUCTION READINESS AUDIT")
    print("=" * 76)

    auditor = ProductionReadinessAuditor()
    report = auditor.audit_all_gates()

    status_dict = SystemConfig.get_status_dict()
    mode_info = RuntimeModeManager.get_status_summary()

    tavily_key = os.getenv("TAVILY_API_KEY")
    sheets_token = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

    print(f"\n[RUNTIME & OPERATIONAL STATE]")
    print(f"  • Runtime Mode:               {mode_info['runtime_mode']}")
    print(f"  • Operational State:          {mode_info['operational_state']}")
    print(f"  • Travel Mode:                {'ACTIVE' if status_dict['travel_mode'] else 'DISABLED'}")
    print(f"  • Commercial Actions:         {'LOCKED' if status_dict['commercial_actions_locked'] else 'ENABLED'}")
    print(f"  • Automated Email Outreach:   {'ENABLED' if status_dict['automated_email_active'] else 'DISABLED'}")
    print(f"  • Operating Mode Banner:      {status_dict.get('operating_mode_banner', '')}")

    print(f"\n[PROVIDER CONFIGURATION STATUS]")
    print(f"  • Tavily Search Provider:     {'CONFIGURED' if tavily_key else 'UNCONFIGURED (MOCKS ACTIVE)'}")
    print(f"  • Google Sheets CRM Provider: {'CONFIGURED' if sheets_token else 'UNCONFIGURED (LOCAL CACHE ACTIVE)'}")

    print(f"\n[DETERMINISTIC READINESS GATES ({report['total_gates']} GATES)]")
    print(f"  {'GATE ID':<26} {'STATUS':<10} {'SEVERITY':<10} {'BLOCKING':<10}")
    print(f"  {'-'*24} {'-'*8} {'-'*8} {'-'*8}")

    for g in report["gates"]:
        gate_id = g["gate_id"]
        status = g["status"]
        severity = g["severity"]
        blocking = "YES" if g["blocking"] else "NO"
        status_marker = "✓" if status == "PASS" else ("⚠" if status == "WARN" else "✗")
        print(f"  {status_marker} {gate_id:<24} {status:<10} {severity:<10} {blocking:<10}")

    print(f"\n[AUDIT SUMMARY]")
    print(f"  • Total Gates Evaluated:      {report['total_gates']}")
    print(f"  • Passed Gates:               {report['passed_gates_count']}")
    print(f"  • Warning Gates:              {report['warning_gates_count']}")
    print(f"  • Blocking Gates:             {report['blocking_gates_count']}")
    print(f"  • Readiness State:            {report['readiness_state']}")
    print(f"  • Overall Preflight Verdict:  {report['overall_status']}")
    print("=" * 76)

    # Persist report to data/production_preflight_report.json if directory exists
    out_dir = os.path.join(PROJECT_ROOT, "data")
    if os.path.exists(out_dir):
        out_file = os.path.join(out_dir, "production_preflight_report.json")
        try:
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            print(f"Machine-readable audit report written to: {out_file}\n")
        except Exception:
            pass

    if report["overall_status"] == "BLOCKED" or report["blocking_gates_count"] > 0:
        print("✗ PREFLIGHT FAILED: One or more required readiness gates are blocking.")
        return 1

    print("✓ PREFLIGHT PASSED: System meets all technical readiness standards.")
    return 0


if __name__ == "__main__":
    sys.exit(run_preflight())
