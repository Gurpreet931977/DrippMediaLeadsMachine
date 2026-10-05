#!/usr/bin/env python3
"""
scripts/start_production.py
===========================
Production Startup Pre-flight Verification and Technical Operations Bootstrapper.

Enforces Section 37 requirements:
  Validates:
    - config
    - storage
    - schema
    - locks
    - scheduler
    - backup availability
    - kill switches (TRAVEL_MODE active, commercial actions locked)
    - market configuration (Only MANCHESTER_UK enabled)
    - health

  CRITICAL INVARIANT:
    If a critical condition fails:
      STARTUP_BLOCKED
    Do not partially initialize production.
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.system.system_config import SystemConfig
from lib.system.config_validator import ConfigValidator
from lib.system.file_lock import crm_write_lock, proposal_records_lock
from lib.system.backup_manager import BackupManager
from lib.system.system_health import SystemHealthMonitor
from lib.system.technical_scheduler import TechnicalScheduler
from lib.production.market_config import MarketRegistry


def startup_check(verbose: bool = True) -> bool:
    print("=" * 72)
    print("  DRIPP MEDIA LEAD ENGINE — PRODUCTION STARTUP VERIFICATION")
    print(f"  Startup Time (UTC): {datetime.now(timezone.utc).isoformat()}")
    print("=" * 72)

    critical_failures = []

    # 1. Configuration Validation
    print("\n[1/9] Validating Environment Configuration...")
    config_audit = ConfigValidator.validate_all()
    services = config_audit.get("services", {})
    storage_cfg = services.get("storage", {})
    if storage_cfg.get("status") != "CONFIGURED":
        critical_failures.append(f"Storage config invalid: {storage_cfg.get('details')}")
    else:
        print("  ✓ Core environment and storage validated.")

    # 2. Storage Directory Writable
    print("\n[2/9] Validating Storage Readiness...")
    data_dir = os.path.join(PROJECT_ROOT, "data")
    backups_dir = os.path.join(data_dir, "backups")
    if not (os.path.exists(data_dir) and os.access(data_dir, os.W_OK)):
        critical_failures.append(f"Data directory '{data_dir}' not writable.")
    if not os.path.exists(backups_dir):
        os.makedirs(backups_dir, exist_ok=True)
    print(f"  ✓ Storage directory verified at {data_dir}")

    # 3. Schema & Data Store Integrity
    print("\n[3/9] Validating Schema & JSON Store Integrity...")
    monitor = SystemHealthMonitor(data_dir=data_dir)
    file_health = monitor.check_file_integrity()
    if file_health["status"] != "PASS":
        failed_files = [k for k, v in file_health["files"].items() if not v.get("valid_json")]
        critical_failures.append(f"Corrupt or missing critical JSON stores: {', '.join(failed_files)}")
    else:
        print("  ✓ All critical data stores exist and parse successfully.")

    # 4. Cross-Process File Locks
    print("\n[4/9] Validating File Locking Mechanism...")
    try:
        with crm_write_lock(timeout=2.0):
            with proposal_records_lock(timeout=2.0):
                pass
        print("  ✓ Cross-process locks acquired and released cleanly.")
    except Exception as e:
        critical_failures.append(f"File locking mechanism failed: {e}")

    # 5. Technical Scheduler
    print("\n[5/9] Validating Technical Scheduler...")
    try:
        scheduler = TechnicalScheduler()
        sched_status = scheduler.get_status()
        print(f"  ✓ Scheduler initialized ({len(sched_status.get('tasks', []))} default tasks ready).")
    except Exception as e:
        critical_failures.append(f"Scheduler initialization error: {e}")

    # 6. Backup Availability
    print("\n[6/9] Validating Backup & Disaster Recovery Availability...")
    bm = BackupManager(data_dir=data_dir)
    backups = bm.list_backups()
    if not backups:
        print("  ⚠ No prior backups found. Creating initial production snapshot...")
        try:
            snapshot = bm.create_backup(label="startup_baseline")
            print(f"  ✓ Baseline backup created: {snapshot['backup_id']}")
        except Exception as e:
            critical_failures.append(f"Failed creating baseline backup: {e}")
    else:
        latest = backups[0]
        ver = bm.verify_backup(latest["backup_id"])
        if ver.get("status") == "PASS":
            print(f"  ✓ Latest backup verified: {latest['backup_id']}")
        else:
            critical_failures.append(f"Latest backup {latest['backup_id']} failed checksum verification.")

    # 7. Commercial Kill Switch & Safe Travel Mode
    print("\n[7/9] Enforcing Safety Invariants & Travel Mode...")
    # Production startup MUST strictly enforce safe travel mode unless explicitly overridden
    if not SystemConfig.TRAVEL_MODE:
        print("  ⚠ Notice: Travel mode was inactive. Enforcing TRAVEL_MODE = True for startup safety.")
        SystemConfig.TRAVEL_MODE = True
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False

    if SystemConfig.can_execute_commercial_actions():
        critical_failures.append("SAFETY VIOLATION: Commercial actions are active during production startup!")
    else:
        print("  ✓ TRAVEL_MODE = ACTIVE.")
        print("  ✓ COMMERCIAL_ACTIONS_ENABLED = False (All calls, DMs, emails, proposals LOCKED).")

    # 8. Market Configuration
    print("\n[8/9] Verifying Market Configuration...")
    enabled_markets = MarketRegistry.list_enabled_markets()
    if enabled_markets != ["MANCHESTER_UK"]:
        # Enforce Section 12 rule: default MANCHESTER_UK = enabled, all others disabled
        critical_failures.append(
            f"Invalid market configuration: Expected exactly ['MANCHESTER_UK'] enabled, found: {enabled_markets}"
        )
    else:
        print(f"  ✓ Market isolation verified: Only {enabled_markets} enabled.")

    # 9. Global Health Evaluation
    print("\n[9/9] Running Pre-flight Health Evaluation...")
    health = monitor.evaluate_health()
    if health["system_status"] == "FAILED":
        critical_failures.append(f"System health check failed: {health.get('reconciliation_status')}")
    else:
        print(f"  ✓ Health status: {health['system_status']}")

    # Final Decision
    print("\n" + "=" * 72)
    if critical_failures:
        print("  >>> STARTUP_BLOCKED <<<")
        print("  Critical pre-flight checks failed. Halting startup immediately.")
        print("=" * 72)
        for idx, fail in enumerate(critical_failures, 1):
            print(f"  {idx}. {fail}")
        print("\nDo not partially initialize production until all critical issues are resolved.\n")
        return False

    print("  PRODUCTION STARTUP SUCCESSFUL")
    print("=" * 72)
    print("  OPERATING STATE:")
    print("    TECHNICAL AUTOMATION : ENABLED  (Discovery, Enrichment, Backups, Reconciliation)")
    print("    COMMERCIAL ACTIONS   : LOCKED   (Operator Travelling — No Sends, DMs, or Calls)")
    print(f"    ACTIVE MARKETS       : {enabled_markets}")
    print(f"    SYSTEM STATUS        : {health['system_status']}")
    print("=" * 72 + "\n")
    return True


def main():
    parser = argparse.ArgumentParser(description="Production startup check for Dripp Media Lead Engine")
    parser.add_argument("--check-only", action="store_true", help="Run checks and exit without starting server")
    args = parser.parse_args()

    success = startup_check()
    if not success:
        sys.exit(1)

    print("Ready for unattended technical operations.")
    sys.exit(0)


if __name__ == "__main__":
    main()
