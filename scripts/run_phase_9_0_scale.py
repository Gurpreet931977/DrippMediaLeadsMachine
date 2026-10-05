#!/usr/bin/env python3
"""
Production Scale Acquisition Run - Phase 9.0
Executes controlled acquisition batch targeting 100 fresh candidate businesses for MANCHESTER_UK.
Persists run results to data/phase_9_0_market_run.json.
"""
import os
import sys
import json
import logging
from datetime import datetime, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.production.market_config import MarketRegistry
from lib.production.market_runner import MarketRunner, BatchController, QuotaBudget

def main():
    print("=" * 80)
    print("PHASE 9.0: CONTROLLED PRODUCTION ACQUISITION BATCH (MANCHESTER_UK)")
    print("=" * 80)

    # 1. Fetch Manchester market configuration
    market_config = MarketRegistry.get("MANCHESTER_UK")
    print(f"Target Market: {market_config.market_id} ({market_config.city}, {market_config.country})")
    print(f"Target Candidate Count: 100 fresh businesses")

    # 2. Configure Batch Controller & Hard Ceilings
    controller = BatchController(
        requested_count=100,
        max_external_calls=150,
        max_runtime_seconds=600.0,
        max_new_crm_records=100
    )

    # 3. Configure Quota Budget (Dispatches MUST be 0)
    budget = QuotaBudget(
        search_limit=50,
        gosom_limit=20,
        crm_write_limit=100,
        outreach_dispatch_limit=0
    )

    # 4. Initialize Market Runner
    runner = MarketRunner(
        market_config=market_config,
        batch_controller=controller,
        quota_budget=budget,
    )

    # 5. Execute acquisition run
    print(f"\n[Execution] Initiating run {runner.run_id}...")
    run_result = runner.run()

    # 6. Verify top-level convenience keys
    output_payload = dict(run_result)
    stats = run_result.get("stats", {})
    output_payload.update({
        "discovered_count": stats.get("discovered_count", 0),
        "processed_count": stats.get("processed_count", 0),
        "new_businesses_count": stats.get("new_businesses_count", 0),
        "duplicates_skipped_count": stats.get("duplicates_skipped", 0),
        "country_valid_count": stats.get("country_valid_count", 0),
        "operational_verified_count": stats.get("operational_verified_count", 0),
        "review_evidence_count": stats.get("review_evidence_count", 0),
        "qualified_count": stats.get("qualified_count", 0),
        "contactable_count": stats.get("contactable_count", 0),
        "automated_sendable_count": stats.get("automated_sendable_count", 0),
        "manual_contactable_count": stats.get("manual_contactable_count", 0),
        "commercial_fit_count": stats.get("commercial_fit_count", 0),
        "failures_count": stats.get("failures_count", 0),
        "outreach_sends_count": stats.get("outreach_sends_count", 0),
        "crm_records_created": stats.get("crm_records_created", 0),
    })

    # 7. Persist to data/phase_9_0_market_run.json
    out_file = os.path.join(PROJECT_ROOT, "data", "phase_9_0_market_run.json")
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2, ensure_ascii=False)
    print(f"\n[Artifact] Run results saved to {out_file}")

    # 8. Print Executive Summary
    runner.print_scorecard()

    # 9. Verify Safety Invariants
    assert output_payload["outreach_sends_count"] == 0, "Invariant violation: outreach sends > 0!"
    print("\n[Safety Invariant Verified] outreach_sends_count == 0 (Strict read-only acquisition).")

if __name__ == "__main__":
    main()
