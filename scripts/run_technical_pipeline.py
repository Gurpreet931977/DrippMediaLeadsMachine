#!/usr/bin/env python3
"""
scripts/run_technical_pipeline.py
=================================
Technical Pipeline Runner for Local & GitHub Actions Execution.

Executes bounded technical acquisition batch:
  Discovery → Deduplication → Research → Qualification → Contactability → CRM Sync → Reporting.

STRICT INVARIANTS:
  - Production Safety Assertions:
      TRAVEL_MODE = True
      COMMERCIAL_ACTIONS_ENABLED = False
      AUTOMATED_EMAIL_ENABLED = False
  - Zero commercial outbound: No emails, DMs, calls, proposals, or follow-ups.
  - Safe by default: dry_run = True.
  - Generates audit reports for GitHub Actions artifacts.
  - Non-zero exit code on critical failure.
"""

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.system.system_config import SystemConfig
from lib.system.file_lock import orchestrator_lock
from lib.system.storage_bootstrap import bootstrap_storage_baseline
from lib.production.market_config import MarketRegistry, MarketConfig
from lib.production.market_runner import ProductionScaleEngine, BatchController, QuotaBudget

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TechnicalPipeline")


def assert_safety_invariants() -> None:
    """Enforces strict safety invariants before any pipeline run."""
    violations = []

    # Travel Mode must be active
    if not SystemConfig.TRAVEL_MODE:
        violations.append("SAFETY_VIOLATION: TRAVEL_MODE is not active.")

    # Commercial actions must be disabled
    if SystemConfig.COMMERCIAL_ACTIONS_ENABLED:
        violations.append("SAFETY_VIOLATION: COMMERCIAL_ACTIONS_ENABLED is True.")

    # Automated email must be disabled
    if SystemConfig.AUTOMATED_EMAIL_ENABLED:
        violations.append("SAFETY_VIOLATION: AUTOMATED_EMAIL_ENABLED is True.")

    # Can execute commercial actions check
    if SystemConfig.can_execute_commercial_actions():
        violations.append("SAFETY_VIOLATION: Commercial actions are permitted by SystemConfig.")

    if violations:
        logger.critical("FATAL: Production safety assertions failed!")
        for v in violations:
            logger.critical(f"  - {v}")
        raise PermissionError(f"Safety assertions violated: {'; '.join(violations)}")


def run_pipeline(
    market_id: str = "MANCHESTER_UK",
    country: str = "United Kingdom",
    city: str = "Manchester",
    lead_limit: int = 10,
    dry_run: bool = True,
    refresh_existing: bool = False,
    enable_research: bool = True,
    enable_contactability: bool = True,
    report_path: Optional[str] = None,
    data_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Executes a single bounded technical pipeline run with complete safety invariants.
    """
    start_time = time.time()
    run_timestamp = datetime.now(timezone.utc).isoformat()
    data_dir = data_dir or os.path.join(PROJECT_ROOT, "data")
    target_data_dir = data_dir

    # 1. Initialize runtime storage baseline
    bootstrap_storage_baseline(data_dir=target_data_dir, sync_from_sheets_if_available=not dry_run)

    # 2. Strict Safety Gate
    assert_safety_invariants()

    # 3. Market Configuration
    try:
        market_cfg = MarketRegistry.get(market_id.upper())
    except (KeyError, ValueError, Exception):
        raise ValueError(f"Unknown or unconfigured market: {market_id}")

    logger.info("=" * 72)
    logger.info("DRIPP MEDIA — TECHNICAL PIPELINE RUNNER")
    logger.info(f"Run Timestamp (UTC): {run_timestamp}")
    logger.info(f"Target Market     : {market_cfg.market_id} ({city}, {country})")
    logger.info(f"Requested Leads   : {lead_limit}")
    logger.info(f"Operating Mode    : {'DRY RUN (Safe)' if dry_run else 'LIVE CRM SYNC'}")
    logger.info(f"Travel Mode       : {'ACTIVE' if SystemConfig.TRAVEL_MODE else 'INACTIVE'}")
    logger.info(f"Commercial Sends  : LOCKED (Zero Outreach Permitted)")
    logger.info("=" * 72)

    # 4. Batch Controller and Quota Setup
    max_external_calls = max(lead_limit * 5, 50)
    controller = BatchController(
        requested_count=lead_limit,
        max_external_calls=max_external_calls,
        max_runtime_seconds=600.0,
        max_new_crm_records=0 if dry_run else lead_limit,
    )

    # Quota Budget: outreach_dispatch_limit is strictly 0
    quota_budget = QuotaBudget(
        search_limit=50,
        gosom_limit=10,
        crm_write_limit=0 if dry_run else lead_limit,
        outreach_dispatch_limit=0,
    )

    # 5. Acquire orchestrator lock to prevent concurrent execution
    with orchestrator_lock(timeout=15.0):
        engine = ProductionScaleEngine(
            market_config=market_cfg,
            batch_controller=controller,
            quota_budget=quota_budget,
            checkpoint_dir=os.path.join(target_data_dir, "job_checkpoints"),
        )

        # Execute single bounded acquisition pass
        logger.info(f"Launching ProductionScaleEngine (Run ID: {engine.run_id})...")
        run_result = engine.run()

    elapsed = round(time.time() - start_time, 2)
    stats = run_result.get("stats", {})

    report = {
        "run_id": engine.run_id,
        "market_id": market_cfg.market_id,
        "country": country,
        "city": city,
        "lead_limit": lead_limit,
        "dry_run": dry_run,
        "travel_mode_active": SystemConfig.TRAVEL_MODE,
        "commercial_actions_enabled": SystemConfig.COMMERCIAL_ACTIONS_ENABLED,
        "automated_email_enabled": SystemConfig.AUTOMATED_EMAIL_ENABLED,
        "status": run_result.get("status", "COMPLETED"),
        "duration_seconds": elapsed,
        "timestamp_utc": run_timestamp,
        "stats": {
            "discovered_count": stats.get("discovered_count", 0),
            "processed_count": stats.get("processed_count", 0),
            "new_businesses_count": stats.get("new_businesses_count", 0),
            "duplicates_skipped": stats.get("duplicates_skipped", 0),
            "country_valid_count": stats.get("country_valid_count", 0),
            "operational_verified_count": stats.get("operational_verified_count", 0),
            "review_evidence_count": stats.get("review_evidence_count", 0),
            "qualified_count": stats.get("qualified_count", 0),
            "contactable_count": stats.get("contactable_count", 0),
            "crm_written_count": stats.get("crm_written_count", 0),
            "outreach_dispatched_count": 0,
        },
        "errors": run_result.get("errors", []),
    }

    # Write report files
    out_dir = os.path.join(data_dir, "market_runs")
    os.makedirs(out_dir, exist_ok=True)

    json_report_path = report_path or os.path.join(out_dir, f"technical_pipeline_report_{engine.run_id}.json")
    with open(json_report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    # Markdown Summary for GitHub Step Summary
    md_summary_path = os.path.join(out_dir, f"technical_pipeline_summary_{engine.run_id}.md")
    md_content = f"""# Technical Pipeline Execution Summary

- **Run ID:** `{engine.run_id}`
- **Market:** `{market_cfg.market_id}` ({city}, {country})
- **Execution Mode:** `{'DRY RUN (Safe)' if dry_run else 'LIVE CRM SYNC'}`
- **Status:** `{report['status']}`
- **Duration:** `{elapsed}s`
- **Timestamp (UTC):** `{run_timestamp}`

## Safety Invariants
- **TRAVEL_MODE:** `ACTIVE`
- **COMMERCIAL_ACTIONS_ENABLED:** `FALSE`
- **AUTOMATED_EMAIL_ENABLED:** `FALSE`
- **OUTREACH DISPATCHES:** `0`

## Pipeline Metrics
| Metric | Value |
| :--- | :--- |
| Businesses Discovered | {report['stats']['discovered_count']} |
| Candidates Researched | {report['stats']['processed_count']} |
| New Businesses | {report['stats']['new_businesses_count']} |
| Duplicates Filtered | {report['stats']['duplicates_skipped']} |
| Country Verified | {report['stats']['country_valid_count']} |
| Operational Verified | {report['stats']['operational_verified_count']} |
| Review Evidence Found | {report['stats']['review_evidence_count']} |
| Rule B Qualified Leads | {report['stats']['qualified_count']} |
| Contactable Leads | {report['stats']['contactable_count']} |
| CRM Writes Performed | {report['stats']['crm_written_count']} |
| Outreach Dispatches | 0 |
"""
    with open(md_summary_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    # Also write a standard latest link for GitHub Actions artifacts
    latest_json = os.path.join(data_dir, "latest_technical_pipeline_report.json")
    latest_md = os.path.join(data_dir, "latest_technical_pipeline_summary.md")
    with open(latest_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(latest_md, "w", encoding="utf-8") as f:
        f.write(md_content)

    # If running in GitHub Actions, append to GITHUB_STEP_SUMMARY
    github_step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_step_summary and os.path.exists(github_step_summary):
        try:
            with open(github_step_summary, "a", encoding="utf-8") as gf:
                gf.write(md_content + "\n")
        except Exception:
            pass

    logger.info("=" * 72)
    logger.info(f"TECHNICAL PIPELINE FINISHED — Status: {report['status']}")
    logger.info(f"Report saved to: {json_report_path}")
    logger.info("=" * 72)
    return report


def main():
    parser = argparse.ArgumentParser(description="Dripp Media Technical Pipeline Runner")
    parser.add_argument("--market", default=os.environ.get("MARKET", "MANCHESTER_UK"), help="Target market ID")
    parser.add_argument("--country", default=os.environ.get("COUNTRY", "United Kingdom"), help="Target country")
    parser.add_argument("--city", default=os.environ.get("CITY", "Manchester"), help="Target city")
    parser.add_argument("--limit", type=int, default=int(os.environ.get("LEAD_LIMIT", "10")), help="Lead limit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "true").lower() in ("true", "1", "yes"),
        help="Dry run mode (no CRM mutations)",
    )
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Disable dry run mode")
    parser.add_argument("--refresh-existing", action="store_true", default=False, help="Refresh existing leads")
    parser.add_argument("--enable-research", action="store_true", default=True, help="Enable research stage")
    parser.add_argument("--enable-contactability", action="store_true", default=True, help="Enable contactability stage")
    parser.add_argument("--report-path", default=None, help="Custom output report path")

    args = parser.parse_args()

    try:
        report = run_pipeline(
            market_id=args.market,
            country=args.country,
            city=args.city,
            lead_limit=args.limit,
            dry_run=args.dry_run,
            refresh_existing=args.refresh_existing,
            enable_research=args.enable_research,
            enable_contactability=args.enable_contactability,
            report_path=args.report_path,
        )
        if report.get("status") in ("FAILED", "BLOCKED"):
            sys.exit(1)
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Technical pipeline run failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
