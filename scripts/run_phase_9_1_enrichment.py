#!/usr/bin/env python3
"""
scripts/run_phase_9_1_enrichment.py
===================================
Executes Phase 9.1: Acquisition Data Integrity + Targeted Lead Enrichment.
Produces data/phase_9_1_enrichment_run.json and displays complete metrics.
"""

import os
import sys
import json

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.enrichment.phase_9_1_enrichment_engine import Phase91EnrichmentEngine

def main():
    print("=" * 80)
    print("PHASE 9.1: ACQUISITION DATA INTEGRITY + TARGETED LEAD ENRICHMENT")
    print("=" * 80)

    engine = Phase91EnrichmentEngine(
        phase_9_0_path="data/phase_9_0_market_run.json",
        output_path="data/phase_9_1_enrichment_run.json",
        search_budget_limit=20,
        gosom_budget_limit=10,
    )

    print("\n[Stage 1] Loading Phase 9.0 Manchester Cohort...")
    engine.load_data()
    print(f"  Loaded {len(engine.candidates)} candidate records.")

    print("\n[Stage 2] Executing Population Accounting & Field Completeness Audit...")
    summary = engine.execute_and_save()

    acct = summary["POPULATION_ACCOUNTING"]
    print("\n" + "-" * 70)
    print("  CANONICAL POPULATION ACCOUNTING")
    print("-" * 70)
    print(f"  Discovered Total:       {acct['discovered_total']}")
    print(f"  Country Valid:          {acct['country_valid']}")
    print(f"  Duplicate Existing:     {acct['duplicate_existing']}")
    print(f"  Processed (New):        {acct['processed_new']}")
    print(f"  Processed (Refreshed):  {acct['processed_refreshed']}")
    print(f"  Failed:                 {acct['failed']}")
    print(f"  Processed Total:        {acct['processed_total']}")
    print(f"  Skipped Total:          {acct['skipped_total']}")
    print(f"  Conservation Verified:  {acct['invariants_passed']}")

    invars = summary["INVARIANTS"]
    print("\n" + "-" * 70)
    print("  MATHEMATICAL INVARIANTS & SAFETY VERIFICATION")
    print("-" * 70)
    for invar_name, res in invars.items():
        print(f"  {invar_name:<35} : {res}")

    print("\n" + "-" * 70)
    print("  ENRICHMENT & QUALIFICATION RESULTS")
    print("-" * 70)
    print(f"  Input Candidates:       {summary['INPUT_CANDIDATES']}")
    print(f"  Enriched (Budgeted):    {summary['ENRICHED']}")
    print(f"  Skipped (Conclusive):   {summary['SKIPPED_ALREADY_CONCLUSIVE']}")
    print(f"  Review Evidence Found:  {summary['REVIEW_EVIDENCE_FOUND']}")
    print(f"  Operational Verified:   {summary['OPERATIONAL_VERIFIED']}")
    print(f"  Operational Unknown:    {summary['OPERATIONAL_UNKNOWN']}")
    print(f"  Qualified (Outreach):   {summary['QUALIFIED']}")
    print(f"  Manual Review Queue:    {summary['MANUAL_REVIEW']}")
    print(f"  Research Only:          {summary['RESEARCH_ONLY']}")
    print(f"  Excluded:               {summary['EXCLUDED']}")
    print(f"  Commercial Prospects:   {summary['COMMERCIAL_PROSPECTS']}")
    print(f"  No Website:             {summary['NO_WEBSITE']}")
    print(f"  Broken Website:         {summary['BROKEN_WEBSITE']}")
    print(f"  Functional Website:     {summary['FUNCTIONAL_WEBSITE']}")

    print("\n" + "-" * 70)
    print("  FIELD-LEVEL DATA COMPLETENESS (POST-ENRICHMENT)")
    print("-" * 70)
    post_fc = summary["DATA_COMPLETENESS_BY_FIELD"]["post_enrichment"]
    print(f"  Overall Required Completeness: {post_fc['overall_required_field_completeness']:.1f}%")
    print(f"  {'Field Name':<32} {'Present':<10} {'Missing':<10} {'Completeness'}")
    for fname, fstat in post_fc["fields"].items():
        print(f"  {fname:<32} {fstat['present_count']:<10} {fstat['missing_count']:<10} {fstat['completeness_percent']:.1f}%")

    print("\n" + "-" * 70)
    print("  RESOURCE GOVERNANCE & SAFETY")
    print("-" * 70)
    print(f"  External Search Calls:  {summary['EXTERNAL_SEARCH_USED']} / 20")
    print(f"  Gosom Calls:            {summary['GOSOM_CALLS_USED']} / 10")
    print(f"  CRM Writes:             {summary['CRM_WRITES']}")
    print(f"  Outreach Sends:         {summary['OUTREACH_SENDS']} (Hard ceiling: 0)")
    print(f"  Campaigns Armed:        {summary['CAMPAIGNS_ARMED']} (Hard ceiling: 0)")

    print(f"\n[Artifact] Phase 9.1 machine output persisted to: {engine.output_path}")

if __name__ == "__main__":
    main()
