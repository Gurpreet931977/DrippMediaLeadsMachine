#!/usr/bin/env python3
"""
scripts/run_phase_9_3_contactability.py
=======================================
Phase 9.3: Qualified Lead Contactability + Activation Engine Executable.

Executes contactability enrichment and activation profile generation across all
OUTREACH_READY Manchester leads, strictly enforcing hard safety ceilings and
CRM outreach state preservation.
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.enrichment.contactability_enrichment import ContactabilityEnrichmentEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Phase93Runner")


def run_phase_9_3(
    sheets_leads_path: str = "data/cache_sheets_leads.json",
    phase_9_2_path: str = "data/phase_9_2_evidence_recovery_run.json",
    output_path: str = "data/phase_9_3_contactability_run.json",
) -> dict:
    logger.info("Starting Phase 9.3 Qualified Lead Contactability & Activation Engine...")

    full_sheets_path = os.path.join(PROJECT_ROOT, sheets_leads_path)
    full_9_2_path = os.path.join(PROJECT_ROOT, phase_9_2_path)
    full_output_path = os.path.join(PROJECT_ROOT, output_path)

    # 1. Load sheets leads
    existing_leads = []
    if os.path.exists(full_sheets_path):
        with open(full_sheets_path, "r", encoding="utf-8") as f:
            d = json.load(f)
            existing_leads = d.get("leads", d) if isinstance(d, dict) else d
        logger.info("Loaded %d existing CRM leads from %s", len(existing_leads), sheets_leads_path)
    else:
        logger.warning("Sheets leads cache not found at %s", full_sheets_path)

    # 2. Load Phase 9.2 newly qualified candidates (e.g. Little Aladdin)
    promotions = []
    if os.path.exists(full_9_2_path):
        with open(full_9_2_path, "r", encoding="utf-8") as f:
            d92 = json.load(f)
            promotions = [
                c for c in d92.get("CANDIDATES", [])
                if c.get("qualification_state") == "OUTREACH_READY"
            ]
        logger.info("Loaded %d newly qualified candidates from Phase 9.2", len(promotions))

    # 3. Execute Contactability Enrichment Engine
    engine = ContactabilityEnrichmentEngine(max_search_calls=20)
    result = engine.enrich_qualified_cohort(
        qualified_leads=existing_leads,
        new_promotions=promotions
    )

    # 4. Save Machine Output
    os.makedirs(os.path.dirname(full_output_path), exist_ok=True)
    with open(full_output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    logger.info("Saved Phase 9.3 machine output to %s", full_output_path)

    # 5. Print Execution Summary
    print("\n" + "=" * 80)
    print("PHASE 9.3: QUALIFIED LEAD CONTACTABILITY + ACTIVATION ENGINE")
    print("=" * 80)
    print(f"  Run ID:                      {result['RUN_ID']}")
    print(f"  Market:                      {result['MARKET_ID']}")
    print(f"  Input Qualified Leads:       {result['INPUT_OUTREACH_READY']}")
    print("-" * 80)
    print("  CHANNEL VERIFICATION METRICS")
    print("-" * 80)
    print(f"  Phone Verified:              {result['PHONE_VERIFIED']}")
    print(f"  Instagram Verified:          {result['INSTAGRAM_VERIFIED']}")
    print(f"  Facebook Verified:           {result['FACEBOOK_VERIFIED']}")
    print(f"  Email Verified:              {result['EMAIL_VERIFIED']}")
    print(f"  MX Valid Emails:             {result['MX_VALID_EMAILS']}")
    print("-" * 80)
    print("  CANONICAL CONTACTABILITY STATUS")
    print("-" * 80)
    print(f"  Manual Contactable:          {result['MANUAL_CONTACTABLE']}")
    print(f"  Automated Contactable:       {result['AUTOMATED_CONTACTABLE']}")
    print(f"  Multi-Channel Contactable:   {result['MULTI_CHANNEL_CONTACTABLE']}")
    print(f"  Not Contactable:             {result['NOT_CONTACTABLE']}")
    print("-" * 80)
    print("  LEAD ACTIVATION READINESS")
    print("-" * 80)
    print(f"  Activation Ready:            {result['ACTIVATION_READY']}")
    print(f"  Activation Blocked:          {result['ACTIVATION_BLOCKED']}")
    print(f"  Recommended PHONE:           {result['RECOMMENDED_PHONE']}")
    print(f"  Recommended INSTAGRAM:       {result['RECOMMENDED_INSTAGRAM']}")
    print(f"  Recommended FACEBOOK:        {result['RECOMMENDED_FACEBOOK']}")
    print(f"  Recommended EMAIL:           {result['RECOMMENDED_EMAIL']}")
    print("-" * 80)
    print("  SAFETY INVARIANTS & INTEGRITY")
    print("-" * 80)
    print(f"  Outreach Sends:              {result['OUTREACH_SENDS']} (Ceiling: 0)")
    print(f"  Campaigns Armed:             {result['CAMPAIGNS_ARMED']} (Ceiling: 0)")
    print(f"  CRM Writes:                  {result['CRM_WRITES']}")
    print(f"  Duplicates Skipped:          {result['DUPLICATES_SKIPPED']}")
    print("  Invariants:")
    for k, v in result.get("INVARIANTS", {}).items():
        print(f"    - {k:30}: {v}")
    print("=" * 80 + "\n")

    return result


if __name__ == "__main__":
    run_phase_9_3()
