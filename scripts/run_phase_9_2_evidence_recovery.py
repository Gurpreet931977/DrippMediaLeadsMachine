#!/usr/bin/env python3
"""
scripts/run_phase_9_2_evidence_recovery.py
=========================================
CLI runner for Phase 9.2: Review Evidence Recovery + Operational Verification Engine.
"""

import os
import sys
import json
import logging

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.enrichment.phase_9_2_evidence_recovery_engine import Phase92EvidenceRecoveryEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main():
    print("=" * 80)
    print("PHASE 9.2: REVIEW EVIDENCE RECOVERY + OPERATIONAL VERIFICATION ENGINE")
    print("=" * 80)
    print()

    engine = Phase92EvidenceRecoveryEngine(
        phase_9_1_path="data/phase_9_1_enrichment_run.json",
        output_path="data/phase_9_2_evidence_recovery_run.json",
        max_gosom_calls=10,
        max_external_search_calls=10,
    )

    print("[Stage 1] Loading Phase 9.1 Manchester Cohort...")
    results = engine.execute_evidence_recovery()

    print("\n----------------------------------------------------------------------")
    print("  EVIDENCE RECOVERY & COHORT ACCOUNTING")
    print("----------------------------------------------------------------------")
    print(f"  Run ID:                      {results['RUN_ID']}")
    print(f"  Market:                      {results['MARKET_ID']}")
    print(f"  Input Candidates:            {results['INPUT_CANDIDATES']}")
    print(f"  Candidates Prioritized:      {results['CANDIDATES_PRIORITIZED']}")
    print(f"  Candidates Enriched:         {results['CANDIDATES_ENRICHED']}")
    print(f"  Duplicates Skipped:          {results['DUPLICATES_SKIPPED']}")
    print()
    print("  Review Evidence Found:       ", results['REVIEW_EVIDENCE_FOUND'])
    print("  Review Evidence Recent:      ", results['REVIEW_EVIDENCE_RECENT'])
    print("  Review Evidence Conflicting: ", results['REVIEW_EVIDENCE_CONFLICTING'])
    print("  Review Evidence Insufficient:", results['REVIEW_EVIDENCE_INSUFFICIENT'])
    print()
    print("  Operational Verified:        ", results['OPERATIONAL_VERIFIED'])
    print("  Operational Unknown:         ", results['OPERATIONAL_UNKNOWN'])
    print("  Operational Weak Signal:     ", results['OPERATIONAL_WEAK'])
    print("  Operational Conflicting:     ", results['OPERATIONAL_CONFLICTING'])
    print("  Operational Closed:          ", results['OPERATIONAL_CLOSED'])
    print()
    print("  Qualified (Outreach Ready):  ", results['OUTREACH_READY'])
    print("  Manual Review Queue:         ", results['MANUAL_REVIEW'])
    print("  Research Only:               ", results['RESEARCH_ONLY'])
    print("  Excluded:                    ", results['EXCLUDED'])
    print()
    print("----------------------------------------------------------------------")
    print("  RESOURCE GOVERNANCE & SAFETY CEILINGS")
    print("----------------------------------------------------------------------")
    print(f"  Gosom Calls Used:            {results['GOSOM_CALLS_USED']} / 10")
    print(f"  External Search Calls Used:  {results['EXTERNAL_SEARCH_CALLS_USED']} / 10")
    print(f"  CRM Writes:                  {results['CRM_WRITES']}")
    print(f"  Outreach Sends Count:        {results['OUTREACH_SENDS_COUNT']} (Hard ceiling: 0)")
    print(f"  Campaigns Armed:             {results['CAMPAIGNS_ARMED']} (Hard ceiling: 0)")
    print(f"  Automated Sendable:          {results['AUTOMATED_SENDABLE']} (Hard ceiling: 0)")
    print()
    print("----------------------------------------------------------------------")
    print("  INVARIANT VERIFICATION")
    print("----------------------------------------------------------------------")
    for inv, status in results['INVARIANTS'].items():
        print(f"  {inv:<32}: {status}")

    comp_before = results['DATA_COMPLETENESS_BEFORE']['overall_required_field_completeness']
    comp_after = results['DATA_COMPLETENESS_AFTER']['overall_required_field_completeness']
    print(f"\n  Data Completeness:           {comp_before:.1f}% -> {comp_after:.1f}%")
    print(f"\n[Artifact] Phase 9.2 machine output persisted to: {engine.output_path}")


if __name__ == "__main__":
    main()
