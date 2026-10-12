"""
scripts/run_cloud_run_job.py
============================
Standalone Execution Entrypoint for Google Cloud Run Jobs.

Executes autonomous discovery tasks separately from HTTP requests with:
1. Distributed operational lease acquisition in Cloud Firestore (prevents dual-execution collisions).
2. Resumable checkpointing (resumes interrupted runs without repeating candidates or scraping).
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("CloudRunJob")


def parse_args():
    parser = argparse.ArgumentParser(description="Dripp Media Autonomous Discovery - Cloud Run Job Runner")
    parser.add_argument("--country", default="United Kingdom", help="Target country (must be United Kingdom)")
    parser.add_argument("--cities", nargs="+", default=["Manchester"], help="Target cities (Manchester only for Phase 12)")
    parser.add_argument("--industry", default="Restaurants", help="Target business niche")
    parser.add_argument("--leads", type=int, default=5, help="Qualified leads target count")
    parser.add_argument("--batch-size", type=int, default=10, help="Batch processing size")
    parser.add_argument("--max-multiplier", type=int, default=5, help="Max discovery search multiplier")
    parser.add_argument("--run-id", default=None, help="Specific run ID (if resuming)")
    parser.add_argument("--dry-run", action="store_true", help="Bypass Google Sheets CRM writes (shadow mode)")
    parser.add_argument("--lease-name", default="discovery_job_runner", help="Distributed lease identifier in Firestore")
    parser.add_argument("--ttl-seconds", type=float, default=600.0, help="Lease TTL in seconds")
    return parser.parse_args()


def run_discovery_task(args) -> int:
    # 1. Enforce safety invariants
    status_dict = SystemConfig.get_status_dict()
    if not status_dict.get("travel_mode"):
        logger.error("SAFETY VIOLATION: TRAVEL_MODE is not ACTIVE. Aborting job execution.")
        return 1

    if status_dict.get("commercial_actions_enabled"):
        logger.error("SAFETY VIOLATION: COMMERCIAL_ACTIONS_ENABLED is TRUE. Commercial actions must remain locked.")
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

    # 4. Check for prior resumable checkpoint in Firestore
    checkpoint = firestore_store.get_checkpoint(run_id)
    processed_ids: List[str] = []
    prior_qualified = 0
    if checkpoint:
        processed_ids = checkpoint.get("processed_ids", [])
        prior_qualified = checkpoint.get("stats", {}).get("outreach_ready", 0)
        logger.info(
            "Found prior checkpoint for run %s. Resuming execution with %d pre-processed candidates (%d already qualified).",
            run_id, len(processed_ids), prior_qualified
        )
    else:
        logger.info("Starting fresh discovery run %s for %s (%d requested leads)...", run_id, cities, args.leads)

    remaining_leads = max(0, args.leads - prior_qualified) if checkpoint else args.leads
    if checkpoint and remaining_leads == 0:
        logger.info("Target qualified leads (%d) already satisfied in prior checkpoint. Marking run complete.", args.leads)
        firestore_store.save_run_record(run_id, {
            "run_id": run_id,
            "execution_id": execution_id,
            "status": "COMPLETED",
            "market": f"Manchester, {country}",
            "country": country,
            "cities": cities,
            "industry": args.industry,
            "qualified_leads_needed": args.leads,
            "dry_run": args.dry_run,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "leads_count": prior_qualified,
            "processed_candidates_count": len(processed_ids),
        })
        firestore_store.delete_checkpoint(run_id)
        return 0

    # 5. Initialize run record in Firestore
    initial_record = {
        "run_id": run_id,
        "execution_id": execution_id,
        "status": "RUNNING",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "market": f"Manchester, {country}",
        "country": country,
        "cities": cities,
        "industry": args.industry,
        "qualified_leads_needed": args.leads,
        "dry_run": args.dry_run,
        "processed_candidates_count": len(processed_ids),
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

    # 7. Progress callback updates checkpoint in Firestore
    def job_progress_callback(msg: str, data: Optional[Dict[str, Any]] = None):
        if data and "processed_ids" in data:
            current_processed = data.get("processed_ids", [])
            firestore_store.save_checkpoint(run_id, {
                "market_id": f"Manchester, {country}",
                "status": "RUNNING",
                "processed_ids": current_processed,
                "stats": data.get("stats", {}),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        logger.info("[PROGRESS] %s", msg)

    try:
        discovery_engine = HybridDiscoveryEngine(mode="HYBRID", apify_enabled=False)
        pipeline = LeadGenerationPipeline(
            discovery_provider=discovery_engine,
            shadow_mode=args.dry_run
        )

        result = pipeline.run(
            country=country,
            cities=cities,
            industry=args.industry,
            requested_qualified_leads=remaining_leads,
            batch_size=args.batch_size,
            max_research_multiplier=args.max_multiplier,
            log_callback=job_progress_callback,
            resume_processed_ids=processed_ids
        )

        # 8. Finalize run record and remove checkpoint upon success
        total_leads_count = prior_qualified + len(result.get("leads", []))
        final_record = {
            **initial_record,
            "status": "COMPLETED",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "results": result,
            "stats": result.get("stats", {}),
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
