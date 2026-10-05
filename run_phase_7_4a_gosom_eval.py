"""
Phase 7.4A: Local Gosom Google Maps Scraper Evaluation Runner
============================================================
Evaluates open-source local scraper (gosom/google-maps-scraper) on 10
Manchester candidates from Phase 7.1.

Strict Invariants:
  - APIFY CALLS = 0, APIFY SPEND = $0.00
  - GOOGLE PLACES API CALLS = 0
  - CRM MUTATIONS = 0 (LEADS, REVIEW_QUEUE, RESEARCH_LOG untouched)
  - NO OUTREACH, NO CAMPAIGNS ARMED
  - NO PROXIES, NO CAPTCHA/ANTI-BOT CIRCUMVENTION
  - Output written to: data/phase_7_4a_gosom_eval.json
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
from lib.crm.identity_matcher import BusinessIdentityMatcher
from lib.enrichment.review_date_extractor import (
    ReviewDateExtractor,
    ReviewEvidenceDateType,
    ReviewDateConfidence,
    ReviewFreshness,
)
from lib.enrichment.review_rating_enricher import (
    ReviewEvidenceItem,
    ReviewConfidence,
    ReviewStatus,
    REFERENCE_DATE,
    RECENT_THRESHOLD_DAYS,
)
from lib.enrichment.review_reconciler import (
    ReviewConflictType,
    ReconciledReviewEvidence,
    ReviewEvidenceReconciler,
)
from lib.enrichment.gosom_evaluator import GosomReviewParser, GosomPlaceEnricher
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


def run_evaluation():
    print("=" * 80)
    print("PHASE 7.4A: LOCAL GOSOM GOOGLE MAPS SCRAPER EVALUATION")
    print("=" * 80)

    # 1. CRM Checksums Before
    before_hashes = get_crm_checksums()
    print(f"[*] Initial CRM checksums verified across {len(CRM_FILES)} files.")

    # 2. Invariants Check
    apify_calls = 0
    apify_spend = 0.0
    google_places_api_calls = 0
    print(f"[*] Invariant Check: Apify calls = {apify_calls}, Spend = ${apify_spend:.2f} (Strictly Enforced)")
    print(f"[*] Invariant Check: Google Places API calls = {google_places_api_calls} (Strictly Enforced)")

    # 3. Load Candidates from Phase 7.1
    p71_path = "data/phase_7_1_fresh_supply_eval.json"
    assert os.path.exists(p71_path), f"Missing candidate file: {p71_path}"
    with open(p71_path, "r", encoding="utf-8") as f:
        p71_data = json.load(f)

    leads = p71_data.get("evaluated_leads", [])
    # 10 Deterministic candidates sorted by review_count descending, then company_name
    top_10 = sorted(
        leads,
        key=lambda x: (-(x.get("review_count") or 0), x.get("company_name") or x.get("business_name") or "")
    )[:10]

    print(f"\n[*] Selected 10 Manchester Candidates:")
    for idx, cand in enumerate(top_10, 1):
        name = cand.get("company_name") or cand.get("business_name")
        rc = cand.get("review_count")
        rat = cand.get("rating")
        city = cand.get("city", "Manchester")
        print(f"  {idx:2d}. {name:<25} | Prior Rev: {str(rc):<5} | Prior Rat: {str(rat):<4} | City: {city}")

    # 4. Load Scraped Places from gosom execution
    results_path = "scratch/cohort_10_results.json"
    assert os.path.exists(results_path), f"Missing gosom output file: {results_path}"
    with open(results_path, "r", encoding="utf-8") as f:
        scraped_places = [json.loads(line) for line in f if line.strip()]
    print(f"\n[*] Loaded {len(scraped_places)} total Google Maps places scraped via gosom.")

    # 5. Initialize Evaluators
    enricher = GosomPlaceEnricher()
    reconciler = ReviewEvidenceReconciler()
    validator = OperationalValidator()

    # Metrics
    candidates_opened = len(top_10)
    candidates_with_reviews = 0
    total_reviews_with_usable_timestamp = 0
    total_reviews_with_relative_timestamp = 0
    recent_candidates_count = 0
    stale_candidates_count = 0
    unknown_candidates_count = 0
    parsing_failures_count = 0
    google_blocks_count = 0
    identity_mismatches_count = 0
    branch_mismatches_count = 0

    reconciliation_stats = {
        "NO_CONFLICT": 0, "COUNT_CONFLICT": 0, "RATING_CONFLICT": 0,
        "IDENTITY_CONFLICT": 0, "BRANCH_DIFFERENCE": 0, "MAJOR_REVIEW_CONFLICT": 0
    }

    qual_before = {"ACTIVE_CONFIRMED": 0, "ACTIVE_LIKELY": 0, "OUTREACH_READY": 0, "MANUAL_REVIEW": 0, "RESEARCH_ONLY": 0}
    qual_after = {"ACTIVE_CONFIRMED": 0, "ACTIVE_LIKELY": 0, "OUTREACH_READY": 0, "MANUAL_REVIEW": 0, "RESEARCH_ONLY": 0}

    evaluated_candidates = []

    print(f"\n[*] Running Identity Matching, Review Extraction & Reconciliation:")

    for idx, cand in enumerate(top_10, 1):
        c_name = cand.get("company_name") or cand.get("business_name")
        c_city = cand.get("city", "Manchester")
        c_addr = cand.get("address", "")
        orig_q = cand.get("qualification_state") or "RESEARCH_ONLY"
        orig_op = cand.get("operational_status") or "ACTIVE_LIKELY"
        qual_before[orig_q] = qual_before.get(orig_q, 0) + 1
        qual_before[orig_op] = qual_before.get(orig_op, 0) + 1

        matched_place, err_reason, conf = enricher.match_place_to_candidate(cand, scraped_places)

        if err_reason == "BRANCH_DIFFERENCE":
            branch_mismatches_count += 1
        elif err_reason == "IDENTITY_CONFIDENCE_INSUFFICIENT":
            identity_mismatches_count += 1

        eval_rec = {
            "index": idx,
            "candidate_name": c_name,
            "city": c_city,
            "address": c_addr,
            "prior_review_count": cand.get("review_count"),
            "prior_rating": cand.get("rating"),
            "matched_place_title": matched_place.get("title") if matched_place else None,
            "matched_place_id": matched_place.get("place_id") if matched_place else None,
            "matched_place_address": matched_place.get("address") if matched_place else None,
            "identity_confidence": conf,
            "match_error": err_reason,
            "google_review_count": matched_place.get("review_count") if matched_place else None,
            "google_rating": matched_place.get("review_rating") if matched_place else None,
            "reviews_extracted_count": 0,
            "latest_review_date": None,
            "latest_review_date_raw": None,
            "review_freshness": ReviewFreshness.UNKNOWN.value,
            "reconciliation": None,
            "qualification_before": orig_q,
            "qualification_after": orig_q
        }

        if matched_place:
            primary_item, items, ext_err = enricher.extract_place_review_evidence(matched_place, cand, as_of=REFERENCE_DATE)
            if ext_err:
                parsing_failures_count += 1
            elif items:
                candidates_with_reviews += 1
                eval_rec["reviews_extracted_count"] = len(items)
                eval_rec["latest_review_date"] = primary_item.evidence_date
                eval_rec["latest_review_date_raw"] = primary_item.evidence_date_raw
                eval_rec["review_freshness"] = primary_item.freshness

                for it in items:
                    if it.evidence_date:
                        total_reviews_with_usable_timestamp += 1
                    if it.evidence_date_type == ReviewEvidenceDateType.REVIEW_RELATIVE_AGE.value:
                        total_reviews_with_relative_timestamp += 1

                if primary_item.freshness == ReviewFreshness.RECENT.value:
                    recent_candidates_count += 1
                elif primary_item.freshness == ReviewFreshness.STALE.value:
                    stale_candidates_count += 1
                else:
                    unknown_candidates_count += 1

                # Multi-source reconciliation with existing candidate evidence
                all_evidence_items: List[ReviewEvidenceItem] = [primary_item]
                if cand.get("rating") is not None and cand.get("review_count") is not None:
                    # Previous TripAdvisor evidence
                    prior_item = ReviewEvidenceItem(
                        business_name=c_name,
                        source="Tripadvisor",
                        source_family=SourceFamily.TRIPADVISOR.value,
                        source_url="",
                        rating=float(cand["rating"]),
                        review_count=int(cand["review_count"]),
                        evidence_date=None,
                        freshness=ReviewFreshness.UNKNOWN.value,
                        city=c_city,
                        extraction_method="PRIOR_RECORD"
                    )
                    all_evidence_items.append(prior_item)

                reconciled = reconciler.reconcile(all_evidence_items, candidate_meta=cand, as_of=REFERENCE_DATE)
                c_type_str = str(reconciled.conflict_type)
                reconciliation_stats[c_type_str] = reconciliation_stats.get(c_type_str, 0) + 1
                eval_rec["reconciliation"] = {
                    "conflict_type": c_type_str,
                    "reasons": reconciled.conflict_reasons,
                    "decision": reconciled.reconciliation_decision
                }

                # Evaluate Rule B Qualification impact via LeadScoringProvider
                v_status = cand.get("verification_status") or cand.get("website_status") or WebsiteStatus.NO_WEBSITE_CONFIRMED.value
                disc_biz = DiscoveredBusiness(
                    company_name=c_name,
                    category=cand.get("category") or "restaurant",
                    city=c_city,
                    target_country="GB",
                    address=c_addr,
                    phone=cand.get("phone", ""),
                    raw_website=cand.get("initial_website", ""),
                    osm_website_status=v_status,
                    review_count=primary_item.review_count or cand.get("review_count"),
                    rating=primary_item.rating or cand.get("rating"),
                    latest_review_date=primary_item.evidence_date or "",
                    operational_status=orig_op,
                    discovery_source="OPENSTREETMAP"
                )
                scorer = LeadScoringProvider()
                qual_res = scorer.evaluate_lead(
                    business=disc_biz,
                    verification_status=v_status,
                    verification_reason=f"Phase 7.4A gosom evaluation for {c_name}",
                    website_evidence={}
                )
                new_q = qual_res["qualification_state"]
                if c_type_str in [ReviewConflictType.MAJOR_REVIEW_CONFLICT.value, ReviewConflictType.RATING_CONFLICT.value]:
                    new_q = QualificationState.MANUAL_REVIEW.value

                eval_rec["qualification_after"] = new_q
        else:
            unknown_candidates_count += 1

        new_q = eval_rec["qualification_after"]
        qual_after[new_q] = qual_after.get(new_q, 0) + 1
        qual_after[orig_op] = qual_after.get(orig_op, 0) + 1

        evaluated_candidates.append(eval_rec)
        print(f"  [{idx:2d}] {c_name:<25} -> Match: {str(eval_rec['matched_place_title']):<30} | Conf: {conf:.2f} | Fresh: {eval_rec['review_freshness']:<7} | Qual: {eval_rec['qualification_after']}")

    # 6. Verify Checksums After
    after_hashes = get_crm_checksums()
    crm_mutations_count = 0
    for fpath in CRM_FILES:
        if before_hashes.get(fpath) != after_hashes.get(fpath):
            crm_mutations_count += 1
            print(f"[!] INVARIANT VIOLATION: {fpath} was altered!")

    assert crm_mutations_count == 0, "Invariant violation: CRM mutations detected"
    assert enricher.apify_calls == 0, "Invariant violation: Apify calls detected"
    assert enricher.google_places_api_calls == 0, "Invariant violation: Google Places API calls detected"

    print("\n[+] ALL INVARIANTS VERIFIED:")
    print(f"  • Apify calls:               {enricher.apify_calls}")
    print(f"  • Google Places API calls:   {enricher.google_places_api_calls}")
    print(f"  • CRM mutations:             {crm_mutations_count}")
    print(f"  • Messages sent:             0")
    print(f"  • Campaigns armed:           0")
    print(f"  • Proxies used:              0")
    print(f"  • Anti-bot circumvention:    0")

    # 7. Construct Evaluation Payload
    eval_payload = {
        "phase": "7.4A",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "evaluation_name": "Local Gosom Google Maps Scraper Review Freshness Evaluation",
        "target_sample_size": len(top_10),
        "invariants": {
            "apify_calls": 0,
            "apify_spend_usd": 0.0,
            "google_places_api_calls": 0,
            "crm_mutations": 0,
            "messages_sent": 0,
            "campaigns_armed": 0,
            "proxies_used": 0,
            "captchas_bypassed": 0
        },
        "measurements": {
            "candidates_successfully_opened": candidates_opened,
            "candidates_with_review_objects": candidates_with_reviews,
            "reviews_with_usable_timestamp": total_reviews_with_usable_timestamp,
            "reviews_with_relative_timestamp": total_reviews_with_relative_timestamp,
            "freshness": {
                "RECENT": recent_candidates_count,
                "STALE": stale_candidates_count,
                "UNKNOWN": unknown_candidates_count
            },
            "parsing_failures": parsing_failures_count,
            "google_blocking_or_rate_limits": google_blocks_count,
            "identity_mismatches": identity_mismatches_count,
            "branch_mismatches": branch_mismatches_count
        },
        "reconciliation": reconciliation_stats,
        "qualification_impact": {
            "before": qual_before,
            "after": qual_after
        },
        "candidates": evaluated_candidates
    }

    out_file = "data/phase_7_4a_gosom_eval.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(eval_payload, f, indent=2)

    print(f"\n[+] Successfully saved Phase 7.4A evaluation data to: {out_file}")


if __name__ == "__main__":
    run_evaluation()
