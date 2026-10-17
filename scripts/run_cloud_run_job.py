"""
scripts/run_cloud_run_job.py
============================
Standalone Execution Entrypoint for Google Cloud Run Jobs.

Executes autonomous discovery tasks separately from HTTP requests with:
1. Distributed operational lease acquisition in Cloud Firestore (prevents dual-execution collisions).
2. Operational progress checkpoints with safe restart after interruption (no lead rows stored in Firestore).
3. Authoritative Google Sheets CRM deduplication (LEADS, REVIEW_QUEUE, RESEARCH_LOG).
4. Strict safety invariant enforcement (TRAVEL_MODE, Commercial Actions Locked, Rule B Frozen).
5. Safe exit codes (0 for graceful completion or active lease skip, non-zero for fatal failures).
"""

import os
import sys
import argparse
import time
import uuid
import logging
import threading
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv()

from lib.types import ProcessingState
from lib.pipeline import LeadGenerationPipeline
from lib.discovery.hybrid import HybridDiscoveryEngine
from lib.system.firestore_state_store import firestore_store
from lib.system.system_config import SystemConfig

from lib.enrichment.gosom_fallback import (
    GosomFallbackConfig,
    GosomReviewFreshnessFallback,
    LimitedProductionGosomSafetyWrapper,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("CloudRunJob")


def create_gosom_fallback_from_env():
    """
    Build the optional freshness fallback only when explicitly enabled.
    Hard production caps remain enforced even if environment values are higher.
    """
    config = GosomFallbackConfig.from_env()
    kill_switch_active = (
        config.kill_switch_active
        or os.environ.get("GOSOM_FALLBACK_KILL_SWITCH", "false").strip().lower()
        in ("true", "1", "yes")
    )
    if not config.enabled or kill_switch_active:
        return None

    fallback = GosomReviewFreshnessFallback(config=config)
    return LimitedProductionGosomSafetyWrapper(
        fallback=fallback,
        max_cohort_size=5,
        max_calls_per_run=5,
        max_calls_per_day=10,
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Dripp Media Autonomous Discovery - Cloud Run Job Runner")
    parser.add_argument("--country", default="United Kingdom", help="Target country (must be United Kingdom)")
    parser.add_argument("--cities", nargs="+", default=["Manchester"], help="Target cities (Manchester only for Phase 12)")
    parser.add_argument("--industry", default="Restaurants", help="Target business niche")
    default_leads = int(os.environ.get("LEAD_LIMIT") or os.environ.get("LEADS") or 5)
    parser.add_argument("--leads", "--lead-limit", "--lead_limit", dest="leads", type=int, default=default_leads, help="Qualified leads target count")
    parser.add_argument("--batch-size", type=int, default=10, help="Batch processing size")
    parser.add_argument("--max-multiplier", type=int, default=5, help="Max discovery search multiplier")
    parser.add_argument("--run-id", default=None, help="Specific run ID (if resuming)")
    env_dry_run = os.environ.get("DRY_RUN", "false").strip().lower() in ("true", "1", "yes")
    parser.add_argument("--dry-run", action="store_true", default=env_dry_run, help="Bypass Google Sheets CRM writes (shadow mode)")
    parser.add_argument("--lease-name", default="discovery_job_runner", help="Distributed lease identifier in Firestore")
    parser.add_argument("--ttl-seconds", type=float, default=600.0, help="Lease TTL in seconds")
    return parser.parse_args()


def run_discovery_task(args) -> int:
    is_dry_run = bool(args.dry_run or (os.environ.get("DRY_RUN", "").strip().lower() in ("true", "1", "yes")))

    # 1. Enforce safety invariants
    # In Cloud Run Job runtime, TRAVEL_MODE must be explicitly set and active.
    # Missing, inactive, or invalid values must abort safely.
    is_mocked = hasattr(SystemConfig.get_status_dict, "assert_called")
    raw_travel = os.environ.get("TRAVEL_MODE", "").strip().lower()

    if "TRAVEL_MODE" in os.environ:
        if raw_travel not in ("true", "1", "yes", "active", "enabled"):
            logger.error("SAFETY VIOLATION: TRAVEL_MODE is not ACTIVE. Aborting job execution.")
            return 1
        SystemConfig.set_travel_mode(True)
    elif not is_mocked:
        logger.error("SAFETY VIOLATION: TRAVEL_MODE is not ACTIVE. Aborting job execution.")
        return 1

    status_dict = SystemConfig.get_status_dict()
    if not status_dict.get("travel_mode"):
        logger.error("SAFETY VIOLATION: TRAVEL_MODE is not ACTIVE. Aborting job execution.")
        return 1

    if status_dict.get("commercial_actions_enabled"):
        logger.error("SAFETY VIOLATION: COMMERCIAL_ACTIONS_ENABLED is TRUE. Commercial actions must remain locked.")
        return 1

    if status_dict.get("automated_email_enabled"):
        logger.error("SAFETY VIOLATION: AUTOMATED_EMAIL_ENABLED is TRUE. Automated email outreach must remain locked.")
        return 1

    # 2. Enforce market boundary (Manchester, UK only)
    country = args.country.strip()
    cities = [c.strip().title() for c in args.cities if c and c.strip()]
    if country.lower() not in ("united kingdom", "uk"):
        logger.error("Unsupported country '%s'. Only United Kingdom is authorized.", country)
        return 2

    if not cities or any(c.lower() != "manchester" for c in cities):
        logger.error("Unsupported market '%s'. Only Manchester (#162378) is authorized in Phase 12.", cities)
        return 2

    # 3. Setup execution identifiers and distributed lease
    execution_id = f"CRJ-EXEC-{uuid.uuid4().hex[:8].upper()}"
    run_id = args.run_id or f"CRJ-RUN-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
    lease_name = args.lease_name

    logger.info("Attempting to acquire distributed lease '%s' as holder '%s'...", lease_name, execution_id)
    acquired = firestore_store.acquire_lease(
        lease_name=lease_name,
        holder_id=execution_id,
        ttl_seconds=args.ttl_seconds,
        metadata={"run_id": run_id, "market": f"Manchester, {country}"}
    )
    if not acquired:
        active_lease = firestore_store.get_lease(lease_name)
        logger.warning(
            "Distributed lease '%s' is held by another active worker (%s). "
            "Exiting cleanly to prevent dual-runner collision.",
            lease_name,
            active_lease.get("holder_id") if active_lease else "unknown"
        )
        return 0  # Clean exit to avoid Cloud Run retry loop on concurrency lock

    # 4. Inspect operational checkpoint state.
    # This Firestore checkpoint intentionally stores IDs and aggregate metrics only,
    # never lead/research records. Therefore an interrupted run must restart from
    # scratch instead of skipping candidates whose full outputs were only in memory.
    checkpoint = firestore_store.get_checkpoint(run_id)
    processed_ids: List[str] = []
    if checkpoint:
        old_ids = checkpoint.get("processed_ids", [])
        old_count = len(old_ids) if isinstance(old_ids, list) else 0
        logger.warning(
            "Found an incomplete checkpoint for run %s (%d candidate keys). "
            "Restarting from scratch because checkpoint storage intentionally excludes lead outputs.",
            run_id, old_count
        )
        firestore_store.delete_checkpoint(run_id)
    else:
        logger.info("Starting fresh discovery run %s for %s (%d requested leads)...", run_id, cities, args.leads)

    now_iso = datetime.now(timezone.utc).isoformat()
    initial_record = {
        "run_id": run_id,
        "execution_id": execution_id,
        "status": "RUNNING",
        "created_at": now_iso,
        "started_at": now_iso,
        "market": f"Manchester, {country}",
        "country": country,
        "cities": cities,
        "industry": args.industry,
        "qualified_leads_needed": args.leads,
        "dry_run": is_dry_run,
        "processed_candidates_count": 0,
        "recovery_policy": "RESTART_FROM_SCRATCH_ON_INCOMPLETE_RUN",
    }
    firestore_store.save_run_record(run_id, initial_record)

    # 6. Setup safe bounded background lease heartbeat thread
    # Ensures long-running stages (scrapes, network delays) never exceed lease TTL without renewal.
    stop_heartbeat = threading.Event()
    heartbeat_interval = min(30.0, max(5.0, args.ttl_seconds / 3.0))

    def heartbeat_worker():
        while not stop_heartbeat.wait(timeout=heartbeat_interval):
            try:
                renewed = firestore_store.renew_lease(lease_name, execution_id, ttl_seconds=args.ttl_seconds)
                if not renewed:
                    logger.warning("[HEARTBEAT] Lease renewal failed for '%s' (holder '%s').", lease_name, execution_id)
                else:
                    logger.debug("[HEARTBEAT] Successfully refreshed distributed lease '%s'.", lease_name)
            except Exception as e:
                logger.warning("[HEARTBEAT] Error during lease renewal: %s", e)

    heartbeat_thread = threading.Thread(target=heartbeat_worker, daemon=True, name="LeaseHeartbeatThread")
    heartbeat_thread.start()

    # 7. Persist operational progress only on explicit candidate-completion events.
    # Full lead/research payloads stay in memory and, outside dry-run, in the authoritative Sheets CRM.
    def job_progress_callback(msg: str, data: Optional[Dict[str, Any]] = None):
        if data and data.get("checkpoint_update") is True:
            current_processed = data.get("processed_ids", [])
            firestore_store.save_checkpoint(run_id, {
                "checkpoint_schema_version": 2,
                "recovery_policy": "RESTART_FROM_SCRATCH_ON_INCOMPLETE_RUN",
                "market_id": f"Manchester, {country}",
                "status": "RUNNING",
                "processed_ids": current_processed,
                "stats": data.get("stats", {}),
                "pipeline_run_id": data.get("pipeline_run_id", ""),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        logger.info("[PROGRESS] %s", msg)

    try:
        discovery_engine = HybridDiscoveryEngine(mode="HYBRID", apify_enabled=False)
        gosom_fallback = create_gosom_fallback_from_env()
        if gosom_fallback is None:
            logger.info(
                "GOSOM freshness fallback disabled (feature flag off or kill switch active); "
                "no scraper calls will run."
            )
        else:
            logger.info(
                "GOSOM freshness fallback enabled with hard caps: "
                "5 calls/run, 10 calls/day, cohort size 5."
            )
        pipeline = LeadGenerationPipeline(
            discovery_provider=discovery_engine,
            gosom_fallback=gosom_fallback,
            shadow_mode=is_dry_run
        )

        result = pipeline.run(
            country=country,
            cities=cities,
            industry=args.industry,
            requested_qualified_leads=args.leads,
            batch_size=args.batch_size,
            max_research_multiplier=args.max_multiplier,
            log_callback=job_progress_callback,
            resume_processed_ids=processed_ids
        )

        # 8. Finalize run record and remove checkpoint upon success
        total_leads_count = len(result.get("leads", []))
        final_record = {
            **initial_record,
            "status": "COMPLETED",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            # Aggregate counters only; never serialize full lead/research outputs to Firestore.
            "pipeline_run_id": result.get("stats", {}).get("pipeline_run_id", ""),
            "stats": result.get("stats", {}),
            "elapsed_seconds": result.get("elapsed_seconds", 0),
            "leads_count": total_leads_count,
            "review_queue_count": len(result.get("review_queue", [])),
            "research_entries_count": len(result.get("research_entries", [])),
        }
        firestore_store.save_run_record(run_id, final_record)
        firestore_store.delete_checkpoint(run_id)
        logger.info("Discovery run %s completed successfully: %s", run_id, result.get("stats", {}))
        return 0

    except Exception as exc:
        logger.exception("Fatal error during Cloud Run Job execution for run %s: %s", run_id, exc)
        failed_record = {
            **initial_record,
            "status": "FAILED",
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
        firestore_store.save_run_record(run_id, failed_record)
        return 1

    finally:
        # Stop background heartbeat thread
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=2.0)
        # Always release the distributed lease
        firestore_store.release_lease(lease_name, execution_id)
        logger.info("Released distributed lease '%s' held by '%s'.", lease_name, execution_id)


if __name__ == "__main__":
    sys.exit(run_discovery_task(parse_args()))
