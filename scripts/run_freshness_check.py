#!/usr/bin/env python3
"""
scripts/run_freshness_check.py
==============================
Technical Freshness Runner for Local & GitHub Actions Execution.

Executes non-commercial technical freshness & review updates:
  - Review evidence recency and rating audits
  - Operational signal refresh
  - Contactability channel and MX validation

STRICT INVARIANTS:
  - TRAVEL_MODE = True
  - COMMERCIAL_ACTIONS_ENABLED = False
  - AUTOMATED_EMAIL_ENABLED = False
  - Safe by default: dry_run = True
  - Zero outreach sends, zero commercial modifications
"""

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.system.system_config import SystemConfig
from lib.system.file_lock import orchestrator_lock
from lib.system.storage_bootstrap import bootstrap_storage_baseline
from lib.system.technical_orchestrator import TechnicalOrchestrator, JobType

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FreshnessCheck")


def assert_safety_invariants() -> None:
    violations = []
    if not SystemConfig.TRAVEL_MODE:
        violations.append("TRAVEL_MODE is not active.")
    if SystemConfig.COMMERCIAL_ACTIONS_ENABLED:
        violations.append("COMMERCIAL_ACTIONS_ENABLED is True.")
    if SystemConfig.AUTOMATED_EMAIL_ENABLED:
        violations.append("AUTOMATED_EMAIL_ENABLED is True.")
    if SystemConfig.can_execute_commercial_actions():
        violations.append("Commercial actions are permitted by SystemConfig.")
    if violations:
        raise PermissionError(f"Safety assertions violated: {'; '.join(violations)}")


def run_freshness(
    market_id: str = "MANCHESTER_UK",
    refresh_type: str = "ALL",  # REVIEWS, CONTACTABILITY, ALL
    candidate_limit: int = 50,
    dry_run: bool = True,
    report_path: Optional[str] = None,
) -> Dict[str, Any]:
    start_time = time.time()
    run_timestamp = datetime.now(timezone.utc).isoformat()
    data_dir = os.path.join(PROJECT_ROOT, "data")

    bootstrap_storage_baseline(data_dir=data_dir)
    assert_safety_invariants()

    logger.info("=" * 72)
    logger.info("DRIPP MEDIA — TECHNICAL FRESHNESS RUNNER")
    logger.info(f"Timestamp (UTC): {run_timestamp}")
    logger.info(f"Market         : {market_id}")
    logger.info(f"Refresh Type   : {refresh_type}")
    logger.info(f"Operating Mode : {'DRY RUN (Safe)' if dry_run else 'LIVE REFRESH'}")
    logger.info(f"Travel Mode    : ACTIVE (Commercial Actions Locked)")
    logger.info("=" * 72)

    orchestrator = TechnicalOrchestrator(data_dir=data_dir)
    executed_jobs = []

    job_types_to_run = []
    if refresh_type in ("REVIEWS", "ALL"):
        job_types_to_run.append(JobType.RUN_REVIEW_REFRESH)
    if refresh_type in ("CONTACTABILITY", "ALL"):
        job_types_to_run.append(JobType.RUN_CONTACTABILITY_REFRESH)

    with orchestrator_lock(timeout=15.0):
        for jtype in job_types_to_run:
            logger.info(f"Triggering {jtype.value}...")
            res = orchestrator.trigger_job(
                job_type=jtype,
                market_id=market_id,
                dry_run=dry_run,
                candidate_limit=candidate_limit,
            )
            executed_jobs.append(res)

    elapsed = round(time.time() - start_time, 2)
    all_success = all(j.get("status") in ("COMPLETED", "PAUSED") for j in executed_jobs)
    status = "COMPLETED" if all_success else "FAILED"

    report = {
        "timestamp_utc": run_timestamp,
        "market_id": market_id,
        "refresh_type": refresh_type,
        "dry_run": dry_run,
        "duration_seconds": elapsed,
        "status": status,
        "travel_mode": SystemConfig.TRAVEL_MODE,
        "commercial_actions_enabled": SystemConfig.COMMERCIAL_ACTIONS_ENABLED,
        "jobs": executed_jobs,
    }

    out_dir = os.path.join(data_dir, "market_runs")
    os.makedirs(out_dir, exist_ok=True)
    json_path = report_path or os.path.join(out_dir, f"freshness_report_{int(time.time())}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    latest_json = os.path.join(data_dir, "latest_freshness_report.json")
    with open(latest_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    md_summary = f"""# Technical Freshness Execution Summary

- **Timestamp (UTC):** `{run_timestamp}`
- **Market:** `{market_id}`
- **Refresh Type:** `{refresh_type}`
- **Mode:** `{'DRY RUN (Safe)' if dry_run else 'LIVE REFRESH'}`
- **Status:** `{status}`
- **Duration:** `{elapsed}s`

## Safety State
- **TRAVEL_MODE:** `ACTIVE`
- **COMMERCIAL_ACTIONS_ENABLED:** `FALSE`
- **OUTREACH DISPATCHES:** `0`

## Executed Jobs
| Job Type | Status | Processed | Changed |
| :--- | :--- | :--- | :--- |
"""
    for j in executed_jobs:
        md_summary += f"| {j.get('job_type')} | {j.get('status')} | {j.get('records_processed', 0)} | {j.get('records_changed', 0)} |\n"

    latest_md = os.path.join(data_dir, "latest_freshness_summary.md")
    with open(latest_md, "w", encoding="utf-8") as f:
        f.write(md_summary)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary and os.path.exists(step_summary):
        try:
            with open(step_summary, "a", encoding="utf-8") as sf:
                sf.write(md_summary + "\n")
        except Exception:
            pass

    logger.info("=" * 72)
    logger.info(f"FRESHNESS CHECK FINISHED — Status: {status}")
    logger.info(f"Report saved to: {json_path}")
    logger.info("=" * 72)
    return report


def main():
    parser = argparse.ArgumentParser(description="Dripp Media Technical Freshness Runner")
    parser.add_argument("--market", default="MANCHESTER_UK", help="Target market ID")
    parser.add_argument("--type", choices=["REVIEWS", "CONTACTABILITY", "ALL"], default="ALL", help="Freshness type")
    parser.add_argument("--limit", type=int, default=50, help="Candidate processing limit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "true").lower() in ("true", "1", "yes"),
        help="Dry run mode",
    )
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Disable dry run mode")
    parser.add_argument("--report-path", default=None, help="Custom report output path")

    args = parser.parse_args()

    try:
        report = run_freshness(
            market_id=args.market,
            refresh_type=args.type,
            candidate_limit=args.limit,
            dry_run=args.dry_run,
            report_path=args.report_path,
        )
        if report.get("status") != "COMPLETED":
            sys.exit(1)
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Freshness check failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
