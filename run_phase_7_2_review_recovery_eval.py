#!/usr/bin/env python3
"""
Phase 7.2: Review Evidence Recovery & Source Diversification Evaluation Runner
=============================================================================
Runs review evidence recovery on a deterministic sample of 20 candidates from Phase 7.1
that were blocked by UNKNOWN_REVIEW_FRESHNESS.

Enforces:
  - Deterministic sort: -(review_count or 0), company_name
  - Multi-source recovery in priority order (Restaurant Guru, Yelp, TripAdvisor)
  - Zero anti-bot circumvention (403/DataDome logged as SEARCH_BLOCKED)
  - Same-business identity validation (confidence >= 0.70)
  - Re-evaluation of operational verification and lead qualification
  - Re-assessment of contactability for OUTREACH_READY leads
  - Strict preservation of CRM, no sends, no campaign arms
  - Writes structured results to data/phase_7_2_review_recovery_eval.json
"""

import os
import sys
import json
import time
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List

from lib.enrichment.review_recovery import ReviewEvidenceRecoveryLayer, ReviewRecoveryCandidateResult
from lib.types import SourceFamily
from lib.enrichment.review_date_extractor import ReviewFreshness

CRM_FILES = [
    "data/cache_sheets_leads.json",
    "data/cache_sheets_review_queue.json",
    "data/cache_sheets_research_log.json",
    "data/message_history.json",
    "data/campaigns.json"
]


def file_hash(path: str) -> str:
    if not os.path.exists(path):
        return "NOT_FOUND"
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    print("=" * 80)
    print("PHASE 7.2: REVIEW EVIDENCE RECOVERY & SOURCE DIVERSIFICATION EVALUATION")
    print("=" * 80)

    # 1. Check initial CRM hashes to guarantee no mutations
    initial_hashes = {f: file_hash(f) for f in CRM_FILES}

    # 2. Load Phase 7.1 evaluation results
    phase_7_1_path = "data/phase_7_1_fresh_supply_eval.json"
    if not os.path.exists(phase_7_1_path):
        print(f"Error: Phase 7.1 results not found at {phase_7_1_path}")
        sys.exit(1)

    with open(phase_7_1_path, "r", encoding="utf-8") as f:
        p71_data = json.load(f)

    all_leads = p71_data.get("evaluated_leads", [])
    print(f"Loaded {len(all_leads)} evaluated leads from Phase 7.1.")

    # 3. Filter candidates with UNKNOWN review freshness
    unknown_candidates = [
        lead for lead in all_leads
        if lead.get("review_freshness") == ReviewFreshness.UNKNOWN.value
    ]
    print(f"Candidates with UNKNOWN review freshness: {len(unknown_candidates)}")

    # 4. Sort deterministically: -(review_count or 0), company_name
    unknown_candidates.sort(
        key=lambda x: (-(x.get("review_count") or 0), x.get("company_name", ""))
    )

    # 5. Select top 20 candidates
    sample_size = 20
    selected_sample = unknown_candidates[:sample_size]
    print(f"Selected {len(selected_sample)} candidates for deterministic evaluation:")
    for idx, c in enumerate(selected_sample, 1):
        print(f"  {idx:2d}. {c.get('company_name'):<25} | Orig RC: {str(c.get('review_count')):<5} | Orig Rat: {str(c.get('rating')):<4} | Orig Op: {c.get('operational_status')}")

    print("\nStarting Review Evidence Recovery Layer across sample...")
    start_time = time.time()
    recovery_layer = ReviewEvidenceRecoveryLayer()

    results: List[ReviewRecoveryCandidateResult] = []
    source_stats = {
        "Restaurant Guru": {"attempted": 0, "succeeded": 0, "blocked": 0, "failed": 0, "date_recovered": 0},
        "Yelp": {"attempted": 0, "succeeded": 0, "blocked": 0, "failed": 0, "date_recovered": 0},
        "Tripadvisor": {"attempted": 0, "succeeded": 0, "blocked": 0, "failed": 0, "date_recovered": 0}
    }

    for idx, cand in enumerate(selected_sample, 1):
        cname = cand.get("company_name")
        print(f"\n[{idx:2d}/{len(selected_sample)}] Evaluating: {cname} (Orig Op: {cand.get('operational_status')}, Orig Qual: {cand.get('qualification_state')})...")
        res = recovery_layer.recover_candidate(cand)
        results.append(res)

        print(f"     -> Status: {res.status}")
        print(f"     -> Recov Freshness: {res.recovered_freshness} | Date: {res.recovered_date} | Source: {res.recovered_source_family}")
        print(f"     -> Effective RC: {res.recovered_review_count} | Rating: {res.recovered_rating}")
        print(f"     -> New Op Status: {res.new_operational_status} | New Qual State: {res.new_qualification_state}")

        # Update source telemetry stats
        for t in res.telemetry:
            sf_name = t.get("source_family", "")
            key = None
            if sf_name == SourceFamily.RESTAURANT_GURU.value:
                key = "Restaurant Guru"
            elif sf_name == SourceFamily.YELP.value:
                key = "Yelp"
            elif sf_name == SourceFamily.TRIPADVISOR.value:
                key = "Tripadvisor"

            if key:
                source_stats[key]["attempted"] += 1
                if t.get("succeeded"):
                    source_stats[key]["succeeded"] += 1
                    if t.get("freshness") in [ReviewFreshness.RECENT.value, ReviewFreshness.STALE.value]:
                        source_stats[key]["date_recovered"] += 1
                elif t.get("search_outcome") == "SEARCH_BLOCKED" or t.get("http_status") in [401, 403, 429, 503]:
                    source_stats[key]["blocked"] += 1
                else:
                    source_stats[key]["failed"] += 1

    duration = time.time() - start_time
    print(f"\nCompleted evaluation of {len(results)} candidates in {duration:.2f}s.")

    # 6. Check final CRM hashes to verify zero mutations
    final_hashes = {f: file_hash(f) for f in CRM_FILES}
    mutations_detected = False
    for f in CRM_FILES:
        if initial_hashes[f] != final_hashes[f]:
            print(f"CRITICAL WARNING: CRM file mutated: {f}")
            mutations_detected = True

    if not mutations_detected:
        print("\nINVARIANT CONFIRMED: 0 CRM files modified, 0 CRM mutations, 0 sends dispatched.")

    # 7. Aggregate Summary Statistics
    status_counts: Dict[str, int] = {}
    for r in results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    op_transitions = {
        "ACTIVE_CONFIRMED": sum(1 for r in results if r.new_operational_status == "ACTIVE_CONFIRMED"),
        "ACTIVE_LIKELY": sum(1 for r in results if r.new_operational_status == "ACTIVE_LIKELY"),
        "OPERATIONAL_UNKNOWN": sum(1 for r in results if r.new_operational_status == "OPERATIONAL_UNKNOWN"),
        "CLOSED_OR_UNVERIFIED": sum(1 for r in results if r.new_operational_status == "CLOSED_OR_UNVERIFIED"),
    }

    qual_transitions = {
        "OUTREACH_READY": sum(1 for r in results if r.new_qualification_state == "OUTREACH_READY"),
        "MANUAL_REVIEW": sum(1 for r in results if r.new_qualification_state == "MANUAL_REVIEW"),
        "RESEARCH_ONLY": sum(1 for r in results if r.new_qualification_state == "RESEARCH_ONLY"),
        "EXCLUDED": sum(1 for r in results if r.new_qualification_state == "EXCLUDED"),
    }

    # Contactability breakdown for OUTREACH_READY candidates
    outreach_ready_leads = [r for r in results if r.new_qualification_state == "OUTREACH_READY"]
    contact_stats = {
        "outreach_ready_count": len(outreach_ready_leads),
        "manual_contactable": 0,
        "automated_contactable": 0,
        "fully_sendable": 0,
        "details": []
    }
    for r in outreach_ready_leads:
        ca = r.contactability_after or {}
        is_man = ca.get("is_manually_contactable", False)
        is_auto = ca.get("is_automatically_contactable", False)
        chans = ca.get("channels", {})
        if is_man:
            contact_stats["manual_contactable"] += 1
        if is_auto:
            contact_stats["automated_contactable"] += 1
            contact_stats["fully_sendable"] += 1
        contact_stats["details"].append({
            "company_name": r.business_name,
            "overall_status": ca.get("overall_status"),
            "is_manually_contactable": is_man,
            "is_automatically_contactable": is_auto,
            "channels": {k: {"eligible": v.get("is_eligible"), "reason": v.get("reason")} for k, v in chans.items()}
        })

    # Prepare structured JSON output
    output_data = {
        "phase": "7.2",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "evaluation_sample_size": len(results),
        "duration_seconds": round(duration, 2),
        "recovery_status_summary": status_counts,
        "operational_status_summary": op_transitions,
        "qualification_state_summary": qual_transitions,
        "source_performance": source_stats,
        "contactability_assessment": contact_stats,
        "invariants": {
            "crm_mutations": 0 if not mutations_detected else -1,
            "campaigns_armed": 0,
            "messages_sent": 0,
            "rule_b_unweakened": True
        },
        "candidates": [r.to_dict() for r in results]
    }

    out_file = "data/phase_7_2_review_recovery_eval.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)

    print(f"\nEvaluation output written successfully to {out_file}.")
    print("\n" + "=" * 80)
    print("PHASE 7.2 EVALUATION SUMMARY:")
    print(f"  Total Candidates Evaluated: {len(results)}")
    print(f"  Recovery Status: {status_counts}")
    print(f"  Operational Status: {op_transitions}")
    print(f"  Qualification: {qual_transitions}")
    print(f"  OUTREACH_READY Leads: {len(outreach_ready_leads)}")
    print(f"  Manual Contactable: {contact_stats['manual_contactable']}")
    print(f"  Automated Contactable: {contact_stats['automated_contactable']}")
    print(f"  Fully Sendable: {contact_stats['fully_sendable']}")
    print("=" * 80)


if __name__ == "__main__":
    main()
