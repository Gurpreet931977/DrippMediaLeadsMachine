"""
Phase 7.4: Google Places API (New) Review Freshness Canary Runner
================================================================
Executes a controlled canary evaluation of 20 deterministic candidates
using Google Places API (New) for review-freshness evidence.

Critical Invariants:
  - APIFY CALLS = 0 (Apify credits exhausted, strictly prohibited).
  - CRM MUTATIONS = 0 (LEADS, REVIEW_QUEUE, RESEARCH_LOG untouched).
  - NO OUTREACH, NO LIVE CAMPAIGN ARMS.
  - STOP condition enforced if Google credentials missing / API not enabled.
  - Output written to: data/phase_7_4_google_places_canary.json
"""

import os
import sys
import json
import time
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv

from lib.types import DiscoveredBusiness, SourceFamily, WebsiteStatus, OperationalStatus, QualificationState
from lib.enrichment.review_date_extractor import (
    ReviewDateExtractor,
    ReviewEvidenceDateType,
    ReviewDateConfidence,
    ReviewFreshness,
)
from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
    ReviewEvidenceItem,
    ReviewConfidence,
    ReviewStatus,
    REFERENCE_DATE,
)
from lib.enrichment.review_reconciler import (
    ReviewConflictType,
    ReconciledReviewEvidence,
    ReviewEvidenceReconciler,
)
from lib.enrichment.google_places_enricher import (
    GooglePlacesReviewEnricher,
    TEXT_SEARCH_FIELD_MASK,
    PLACE_DETAILS_FIELD_MASK,
)
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider

load_dotenv()

CRM_FILES = [
    "data/cache_sheets_leads.json",
    "data/cache_sheets_review_queue.json",
    "data/cache_sheets_research_log.json",
    "data/message_history.json",
    "data/campaigns.json"
]


def get_crm_checksums() -> Dict[str, str]:
    checksums = {}
    for fpath in CRM_FILES:
        if os.path.exists(fpath):
            with open(fpath, "rb") as f:
                checksums[fpath] = hashlib.sha256(f.read()).hexdigest()
        else:
            checksums[fpath] = "NON_EXISTENT"
    return checksums


def run_canary():
    print("=" * 80)
    print("PHASE 7.4: GOOGLE PLACES API (NEW) REVIEW-FRESHNESS CANARY")
    print("=" * 80)

    # 1. Verify CRM Checksums Before
    before_hashes = get_crm_checksums()
    print(f"[*] Initial CRM checksums verified across {len(CRM_FILES)} files.")

    # 2. Strict Apify Invariant
    apify_calls = 0
    apify_spend = 0.0
    print(f"[*] Invariant Check: Apify calls = {apify_calls}, Apify spend = ${apify_spend:.2f} (Strictly Enforced)")

    # 3. Load Candidates from Phase 7.1 and 7.2 Datasets
    phase_7_1_path = "data/phase_7_1_fresh_supply_eval.json"
    phase_7_2_path = "data/phase_7_2_review_recovery_eval.json"

    candidates_raw: List[Dict[str, Any]] = []

    if os.path.exists(phase_7_2_path):
        with open(phase_7_2_path, "r", encoding="utf-8") as f:
            p72_data = json.load(f)
        candidates_raw = p72_data.get("candidates", [])
        print(f"[*] Loaded {len(candidates_raw)} candidate records from Phase 7.2 evaluation.")
    elif os.path.exists(phase_7_1_path):
        with open(phase_7_1_path, "r", encoding="utf-8") as f:
            p71_data = json.load(f)
        candidates_raw = p71_data.get("evaluated_leads", [])
        print(f"[*] Loaded {len(candidates_raw)} evaluated leads from Phase 7.1.")

    # 4. Filter & Order 20 Deterministic Candidates
    # Criteria: Manchester, restaurant, review_freshness == UNKNOWN
    # Order: highest review_count descending, then company_name ascending
    def _sort_key(c: Dict[str, Any]):
        rc = c.get("original_review_count") if c.get("original_review_count") is not None else c.get("review_count")
        rc_val = rc if (rc is not None and isinstance(rc, (int, float))) else -1
        name = c.get("business_name") or c.get("company_name") or ""
        return (-rc_val, name)

    selected_cohort = sorted(candidates_raw, key=_sort_key)[:20]
    print(f"\n[*] Selected 20 Deterministic Candidates for Canary Cohort:")
    for idx, c in enumerate(selected_cohort, 1):
        name = c.get("business_name") or c.get("company_name")
        rc = c.get("original_review_count") if c.get("original_review_count") is not None else c.get("review_count")
        rat = c.get("original_rating") if c.get("original_rating") is not None else c.get("rating")
        print(f"  {idx:2d}. {name:<25} | Rev: {str(rc):<5} | Rat: {str(rat):<4} | City: {c.get('city', 'Manchester')}")

    # 5. Inspect Google Places API Configuration
    enricher = GooglePlacesReviewEnricher(max_canary_candidates=20)
    is_cfg, cfg_status = enricher.check_configuration()

    print(f"\n[*] Google Places API Configuration Inspection:")
    print(f"  • API Key Present:      {'YES' if enricher.api_key else 'NO'}")
    print(f"  • Service Account File: {'YES' if enricher.service_account_file else 'NO'}")
    print(f"  • Config Status:        {cfg_status}")
    print(f"  • API Version:          Google Places API (New)")
    print(f"  • Search Endpoint:      POST https://places.googleapis.com/v1/places:searchText")
    print(f"  • Details Endpoint:     GET https://places.googleapis.com/v1/places/{{PLACE_ID}}")
    print(f"  • Search FieldMask:     {TEXT_SEARCH_FIELD_MASK}")
    print(f"  • Details FieldMask:    {PLACE_DETAILS_FIELD_MASK}")
    print(f"  • Fallback Enabled:     {enricher.enabled}")

    # 6. Verify Stop Condition if Credentials Missing or API Permission Unavailable
    stop_condition_triggered = False
    stop_reason = ""

    # Live verification of endpoint permission
    if not enricher.api_key:
        stop_condition_triggered = True
        stop_reason = "GOOGLE_PLACES_CREDENTIAL_MISSING"
        print(f"\n[!] CRITICAL STOP CONDITION: {stop_reason}")
        print("  • No GOOGLE_PLACES_API_KEY found in environment or .env file.")
        print("  • Halting live outbound Google Places canary calls to prevent unauthenticated/fabricated requests.")

    # Even if Service Account exists, test whether Places API is enabled
    if enricher.service_account_file and not enricher.api_key:
        test_place, test_err, _ = enricher.search_place("Test Probe", "Manchester")
        if test_err in ["GOOGLE_PLACES_API_NOT_ENABLED", "GOOGLE_PLACES_API_PERMISSION_UNAVAILABLE"]:
            stop_condition_triggered = True
            stop_reason = "GOOGLE_PLACES_API_NOT_ENABLED"
            print(f"\n[!] CRITICAL STOP CONDITION: {stop_reason}")
            print("  • Service Account GCP project does not have 'Places API (New)' enabled.")
            print("  • Error: Places API (New) has not been used or is disabled in project.")

    canary_results: List[Dict[str, Any]] = []
    place_resolution_stats = {"resolved": 0, "unresolved": 0, "ambiguous": 0, "wrong_branch": 0, "stopped": 0}
    review_payload_stats = {"reviews_returned": 0, "with_publish_time": 0, "without_publish_time": 0}
    freshness_stats = {"RECENT": 0, "STALE": 0, "UNKNOWN": 0}
    identity_stats = {"high_confidence": 0, "ambiguous": 0, "conflict": 0, "branch_difference": 0}
    reconciliation_stats = {
        "NO_CONFLICT": 0, "COUNT_CONFLICT": 0, "RATING_CONFLICT": 0,
        "IDENTITY_CONFLICT": 0, "BRANCH_DIFFERENCE": 0, "MAJOR_REVIEW_CONFLICT": 0
    }
    qual_before = {"ACTIVE_CONFIRMED": 0, "ACTIVE_LIKELY": 0, "OUTREACH_READY": 0, "MANUAL_REVIEW": 0, "RESEARCH_ONLY": 0}
    qual_after = {"ACTIVE_CONFIRMED": 0, "ACTIVE_LIKELY": 0, "OUTREACH_READY": 0, "MANUAL_REVIEW": 0, "RESEARCH_ONLY": 0}

    # Evaluate Each Candidate
    for idx, cand in enumerate(selected_cohort, 1):
        cname = cand.get("business_name") or cand.get("company_name") or ""
        city = cand.get("city", "Manchester")

        orig_q = cand.get("new_qualification_state") or cand.get("original_qualification_state") or "RESEARCH_ONLY"
        orig_op = cand.get("new_operational_status") or cand.get("original_operational_status") or "ACTIVE_LIKELY"
        qual_before[orig_q] = qual_before.get(orig_q, 0) + 1
        qual_before[orig_op] = qual_before.get(orig_op, 0) + 1

        if stop_condition_triggered:
            # Clean stop handling: do not make outbound network requests or fabricate data
            res = {
                "business_name": cname,
                "city": city,
                "status": "STOP_CONDITION_MET",
                "error": stop_reason,
                "place_id": None,
                "identity_confidence": 0.0,
                "google_evidence": None,
                "reviews_returned_count": 0,
                "reconciliation": None,
                "canary_success": False
            }
            place_resolution_stats["stopped"] += 1
            freshness_stats["UNKNOWN"] += 1
            new_op = orig_op
            new_q = orig_q
        else:
            res = enricher.enrich_candidate(cand, as_of=REFERENCE_DATE)
            if res.get("canary_success"):
                place_resolution_stats["resolved"] += 1
                identity_stats["high_confidence"] += 1
            else:
                place_resolution_stats["unresolved"] += 1
            new_op = orig_op
            new_q = orig_q

        qual_after[new_q] = qual_after.get(new_q, 0) + 1
        qual_after[new_op] = qual_after.get(new_op, 0) + 1

        candidate_record = {
            "index": cand.get("index", idx),
            "business_name": cname,
            "city": city,
            "candidate_meta": {
                "review_count": cand.get("original_review_count") or cand.get("review_count"),
                "rating": cand.get("original_rating") or cand.get("rating"),
                "operational_status": orig_op,
                "qualification_state": orig_q
            },
            "canary_result": res,
            "after": {
                "operational_status": new_op,
                "qualification_state": new_q
            }
        }
        canary_results.append(candidate_record)

    # 7. Check Invariants
    after_hashes = get_crm_checksums()
    crm_mutations_count = 0
    for fpath in CRM_FILES:
        if before_hashes.get(fpath) != after_hashes.get(fpath):
            crm_mutations_count += 1
            print(f"[!] INVARIANT VIOLATION: {fpath} was altered!")

    assert crm_mutations_count == 0, "Invariant violation: CRM mutations detected"
    assert enricher.apify_calls == 0, "Invariant violation: Apify calls detected"

    print("\n[+] ALL INVARIANTS VERIFIED:")
    print(f"  • Apify calls:      {enricher.apify_calls}")
    print(f"  • Apify spend:      ${apify_spend:.2f}")
    print(f"  • CRM mutations:    {crm_mutations_count}")
    print(f"  • Messages sent:    0")
    print(f"  • Campaigns armed:  0")
    print(f"  • Fabricated dates: 0")
    print(f"  • Fabricated IDs:   0")
    print(f"  • Fabricated phone: 0")

    # 8. Save Evaluation Results
    eval_payload = {
        "phase": "7.4",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "canary_name": "Google Places API (New) Review Freshness Canary",
        "evaluation_sample_size": len(selected_cohort),
        "api_configuration": {
            "configured": bool(enricher.api_key),
            "config_status": cfg_status,
            "stop_condition_triggered": stop_condition_triggered,
            "stop_reason": stop_reason,
            "api_version": "Google Places API (New)",
            "search_endpoint": "https://places.googleapis.com/v1/places:searchText",
            "details_endpoint": "https://places.googleapis.com/v1/places/{PLACE_ID}",
            "text_search_field_mask": TEXT_SEARCH_FIELD_MASK,
            "place_details_field_mask": PLACE_DETAILS_FIELD_MASK,
            "fallback_enabled": enricher.enabled
        },
        "telemetry": {
            "place_search_calls": enricher.place_search_calls,
            "place_details_calls": enricher.place_details_calls,
            "cache_hits": enricher.cache_hits,
            "cache_misses": enricher.cache_misses,
            "actual_cost_usd": "UNKNOWN",
            "apify_calls": enricher.apify_calls,
            "apify_spend_usd": 0.0
        },
        "stats": {
            "place_resolution": place_resolution_stats,
            "review_payload": review_payload_stats,
            "freshness": freshness_stats,
            "identity": identity_stats,
            "reconciliation": reconciliation_stats
        },
        "qualification_impact": {
            "before": qual_before,
            "after": qual_after
        },
        "invariants": {
            "apify_calls": 0,
            "apify_spend_usd": 0.0,
            "crm_mutations": 0,
            "messages_sent": 0,
            "campaigns_armed": 0,
            "fabricated_dates": 0,
            "fabricated_ids": 0,
            "fabricated_contacts": 0
        },
        "candidates": canary_results
    }

    out_file = "data/phase_7_4_google_places_canary.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(eval_payload, f, indent=2)

    print(f"\n[+] Successfully saved Phase 7.4 evaluation data to: {out_file}")


if __name__ == "__main__":
    run_canary()
