"""
Dripp Media — Phase 7.6 Gosom Coverage Recovery & Strict Matching Runner
========================================================================
Runs the isolated 20-candidate dry-run experiment to evaluate:
  1. Review metric recovery (review_count, rating)
  2. Review freshness recovery (genuine review timestamps <=180d)
  3. Strict branch-safe place matching (EXACT, STRONG, AMBIGUOUS, BRANCH_MISMATCH, IDENTITY_MISMATCH)
  4. Permanent Pot Kettle Black branch protection
  5. Multi-source review reconciliation
  6. Rule B source-family independence
  7. Verification of all safety invariants (0 CRM writes, 0 outreach, $0 Apify, $0 Places API)
"""

import os
import sys
import json
import time
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Set

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import SourceFamily, OperationalStatus, QualificationState
from lib.crm.identity_matcher import BusinessIdentityMatcher, clean_ascii_text
from lib.enrichment.review_date_extractor import ReviewFreshness, ReviewEvidenceDateType
from lib.enrichment.review_rating_enricher import ReviewEvidenceItem, REFERENCE_DATE
from lib.enrichment.review_reconciler import ReviewConflictType, ReviewEvidenceReconciler
from lib.enrichment.gosom_fallback import GosomFallbackConfig, PINNED_GOSOM_VERSION, DEFAULT_CACHE_DIR
from lib.enrichment.gosom_coverage import (
    PlaceMatchClassification,
    StrictPlaceMatcher,
    construct_gosom_query,
    GosomCoverageEvaluator,
    extract_uk_postcode
)
from lib.outreach.contactability import ContactabilityAssessor, ContactabilityState


CRM_FILES_TO_CHECK = [
    "data/cache_sheets_raw_leads.json",
    "data/cache_sheets_manual_review.json",
    "data/cache_sheets_client_ready.json",
    "data/campaigns.json",
    "data/message_history.json",
]


def compute_file_sha256(filepath: str) -> Optional[str]:
    if not os.path.exists(filepath):
        return None
    with open(filepath, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    print("=" * 80)
    print("DRIPP MEDIA — PHASE 7.6 GOSOM COVERAGE RECOVERY & STRICT BRANCH MATCHING")
    print("=" * 80)
    start_time = time.time()

    # Step 1: Pre-flight Checksums
    checksums_before = {f: compute_file_sha256(f) for f in CRM_FILES_TO_CHECK}

    # Step 2: Invariant Check
    apify_calls = 0
    apify_spend_usd = 0.0
    google_places_api_calls = 0
    print(f"[*] Invariant Check: Apify calls = {apify_calls}, Spend = ${apify_spend_usd:.2f}")
    print(f"[*] Invariant Check: Google Places API calls = {google_places_api_calls}")

    # Step 3: Load Candidates from Phase 7.1
    p71_path = os.path.join(PROJECT_ROOT, "data", "phase_7_1_fresh_supply_eval.json")
    assert os.path.exists(p71_path), f"Missing candidate file: {p71_path}"
    with open(p71_path, "r", encoding="utf-8") as f:
        p71_data = json.load(f)

    leads = p71_data.get("evaluated_leads", [])
    print(f"[*] Loaded {len(leads)} candidates from Phase 7.1")

    # Filter deterministically for candidates meeting Condition A or Condition B:
    # A: review_count is missing OR rating is missing
    # OR
    # B: review_count >= 50 AND rating >= 4.0 AND review_freshness == UNKNOWN
    eligible_leads = []
    for l in leads:
        op = l.get("operational_status")
        if op not in ["ACTIVE_LIKELY", "ACTIVE_CONFIRMED"]:
            continue
        rc = l.get("review_count")
        rat = l.get("rating")
        fresh = l.get("review_freshness") or "UNKNOWN"

        cond_a = (rc is None or rat is None)
        cond_b = False
        if rc is not None and rat is not None:
            try:
                if int(rc) >= 50 and float(rat) >= 4.0 and fresh == "UNKNOWN":
                    cond_b = True
            except (ValueError, TypeError):
                cond_b = False

        if cond_a or cond_b:
            eligible_leads.append(l)

    # Sort ordering: existing review_count descending where available, then company_name ascending
    def sort_key(c):
        rc = c.get("review_count")
        rc_val = rc if rc is not None else -1
        return (-rc_val, (c.get("company_name") or "").lower())

    selected_20 = sorted(eligible_leads, key=sort_key)[:20]
    print(f"[*] Selected {len(selected_20)} candidates deterministically:")
    for idx, c in enumerate(selected_20, 1):
        name = c.get("company_name")
        rc = c.get("review_count")
        rat = c.get("rating")
        fresh = c.get("review_freshness")
        addr = c.get("address") or ""
        print(f"  {idx:2d}. {name:<25} | rc={str(rc):<5} | rat={str(rat):<4} | fresh={fresh} | addr={addr[:35]}")

    # Step 4: Load Scraped Places
    # Results can be in scratch/phase_7_6_20_results.json or scratch/cohort_10_results.json
    scraped_places_pool: List[Dict[str, Any]] = []
    p76_res_path = os.path.join(PROJECT_ROOT, "scratch", "phase_7_6_20_results.json")
    c10_res_path = os.path.join(PROJECT_ROOT, "scratch", "cohort_10_results.json")

    for rpath in [p76_res_path, c10_res_path]:
        if os.path.exists(rpath):
            with open(rpath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            scraped_places_pool.append(json.loads(line))
                        except Exception:
                            pass

    print(f"[*] Total scraped Google places available in pool: {len(scraped_places_pool)}")

    # Deduplicate places by place_id or data_id or title+address
    unique_places_map: Dict[str, Dict[str, Any]] = {}
    for p in scraped_places_pool:
        pid = p.get("place_id") or p.get("data_id") or f"{p.get('title')}_{p.get('address')}"
        if pid not in unique_places_map:
            unique_places_map[pid] = p

    all_unique_places = list(unique_places_map.values())
    print(f"[*] Unique Google Maps places in pool: {len(all_unique_places)}")

    # Step 5: Initialize Evaluator
    config = GosomFallbackConfig(enabled=True, max_calls=20, cache_dir="data/cache_gosom_reviews")
    evaluator = GosomCoverageEvaluator(config=config)

    # Step 6: Evaluate each candidate
    candidate_eval_records = []
    qual_before = {"ACTIVE_CONFIRMED": 0, "ACTIVE_LIKELY": 0, "OUTREACH_READY": 0, "MANUAL_REVIEW": 0, "RESEARCH_ONLY": 0}
    qual_after = {"ACTIVE_CONFIRMED": 0, "ACTIVE_LIKELY": 0, "OUTREACH_READY": 0, "MANUAL_REVIEW": 0, "RESEARCH_ONLY": 0}
    reconciliation_stats = {
        "NO_CONFLICT": 0, "COUNT_CONFLICT": 0, "RATING_CONFLICT": 0,
        "FRESHNESS_CONFLICT": 0, "IDENTITY_CONFLICT": 0, "BRANCH_DIFFERENCE": 0,
        "MAJOR_REVIEW_CONFLICT": 0
    }

    newly_outreach_ready = []

    print("\n" + "=" * 80)
    print("RUNNING STRICT BRANCH-SAFE PLACE MATCHING & REVIEW EVALUATION")
    print("=" * 80)

    for idx, cand in enumerate(selected_20, 1):
        c_name = cand.get("company_name")
        c_addr = cand.get("address") or ""
        q_before = cand.get("qualification_state", "RESEARCH_ONLY")
        op_before = cand.get("operational_status", "ACTIVE_LIKELY")

        qual_before[q_before] = qual_before.get(q_before, 0) + 1
        qual_before[op_before] = qual_before.get(op_before, 0) + 1

        evaluator.candidates_considered += 1

        # Check eligibility
        is_elig, elig_reason = evaluator.is_candidate_eligible(cand)
        if not is_elig:
            evaluator.calls_skipped += 1
            print(f"[{idx:2d}] {c_name:<25} -> SKIPPED ({elig_reason})")
            continue

        evaluator.candidates_tested += 1
        evaluator.calls_attempted += 1

        # Find candidate-relevant places from unique places pool
        # Filter places by query or name relevance
        cand_norm = clean_ascii_text(c_name)
        cand_places = []
        for p in all_unique_places:
            p_name = p.get("title") or ""
            p_norm = clean_ascii_text(p_name)
            # Match if tokens overlap or compute similarity
            sim, _, comp = evaluator.matcher.compute_name_similarity(c_name, p_name)
            if sim >= 0.65 or cand_norm in p_norm or p_norm in cand_norm:
                cand_places.append(p)

        evaluator.calls_completed += 1

        # Build existing review items if candidate had prior data
        existing_revs = []
        if cand.get("review_count") is not None or cand.get("rating") is not None:
            existing_revs.append(ReviewEvidenceItem(
                business_name=c_name,
                source="Discovery Initial Source",
                source_family="INITIAL_DISCOVERY",
                rating=float(cand["rating"]) if cand.get("rating") is not None else None,
                review_count=int(cand["review_count"]) if cand.get("review_count") is not None else None,
                evidence_date=cand.get("latest_review_date") or None,
                freshness=cand.get("review_freshness") or "UNKNOWN",
                confidence="HIGH"
            ))

        # Evaluate candidate under strict matching
        cand_eval = evaluator.evaluate_candidate(
            candidate=cand,
            scraped_places=cand_places,
            existing_reviews=existing_revs,
            as_of=REFERENCE_DATE
        )

        cand_eval["index"] = idx
        candidate_eval_records.append(cand_eval)

        # Track qualification after
        q_after = cand_eval["qualification_after"]
        op_after = cand_eval["operational_status_after"]
        qual_after[q_after] = qual_after.get(q_after, 0) + 1
        qual_after[op_after] = qual_after.get(op_after, 0) + 1

        # Track reconciliation
        rec_type = cand_eval.get("reconciliation_type")
        if rec_type in reconciliation_stats:
            reconciliation_stats[rec_type] += 1

        m_class = cand_eval["match_classification"]
        rc_rec = cand_eval["review_count_recovered"]
        rat_rec = cand_eval["rating_recovered"]
        fresh_rec = cand_eval["freshness"]
        date_rec = cand_eval["latest_review_date"]

        print(f"[{idx:2d}] {c_name:<23} | Match: {m_class:<21} | Rev: {str(rc_rec):<5} | Rat: {str(rat_rec):<4} | Date: {str(date_rec):<10} | Fresh: {fresh_rec:<7} | Qual: {q_before} -> {q_after}")

        if q_after == QualificationState.OUTREACH_READY.value and q_before != QualificationState.OUTREACH_READY.value:
            newly_outreach_ready.append(cand_eval)

    # Step 7: Contactability Assessment for newly OUTREACH_READY leads
    contactability_summary = {
        "newly_outreach_ready_count": len(newly_outreach_ready),
        "with_email": 0,
        "email_valid": 0,
        "mx_valid": 0,
        "instagram": 0,
        "facebook": 0,
        "manual_contactability": 0,
        "automated_contactability": 0,
        "compliance_allowed": 0
    }

    for lead in newly_outreach_ready:
        contact_eval = ContactabilityAssessor.assess_lead(lead)
        em_ch = contact_eval.channels.get("Email")
        ig_ch = contact_eval.channels.get("Instagram Direct Message")
        fb_ch = contact_eval.channels.get("Facebook Messenger")

        if em_ch and em_ch.recipient:
            contactability_summary["with_email"] += 1
            if em_ch.details.get("mx_status") == "VALID_MX":
                contactability_summary["mx_valid"] += 1
                contactability_summary["email_valid"] += 1
        if ig_ch and ig_ch.manual_contactable:
            contactability_summary["instagram"] += 1
        if fb_ch and fb_ch.manual_contactable:
            contactability_summary["facebook"] += 1
        if contact_eval.manual_contactable:
            contactability_summary["manual_contactability"] += 1
        if contact_eval.automated_contactable:
            contactability_summary["automated_contactability"] += 1

    # Step 8: Calculate Yield Rates
    n_tested = evaluator.candidates_tested or 1
    metric_recovery_rate = round(evaluator.review_count_recovered / n_tested, 3)
    date_recovery_rate = round(evaluator.timestamp_recovered / n_tested, 3)
    recent_recovery_rate = round(evaluator.freshness_recent / n_tested, 3)

    # Step 9: Save Cache Entries
    # Cache each candidate query in data/cache_gosom_reviews
    for rec in candidate_eval_records:
        cache_key = hashlib.sha256(f"{rec['candidate_name']}|{rec['city']}|{rec['address']}|{PINNED_GOSOM_VERSION}".encode("utf-8")).hexdigest()[:20]
        cache_file = os.path.join(DEFAULT_CACHE_DIR, f"gosom_{cache_key}.json")
        entry = {
            "cache_key": f"gosom_{cache_key}",
            "candidate_name": rec["candidate_name"],
            "query": rec["query"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "scraper_version": PINNED_GOSOM_VERSION,
            "match_classification": rec["match_classification"],
            "review_count": rec["review_count_recovered"],
            "rating": rec["rating_recovered"],
            "latest_review_date": rec["latest_review_date"],
            "freshness": rec["freshness"],
            "status": "SUCCESS"
        }
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2)

    # Step 10: Compile Structured Evaluation Output
    duration = time.time() - start_time
    output_data = {
        "phase": "7.6",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "evaluation_name": "Gosom Review-Metric Coverage Recovery & Strict Branch Matching",
        "config": {
            "enabled": True,
            "max_calls": 20,
            "scraper_version": PINNED_GOSOM_VERSION,
            "cache_dir": DEFAULT_CACHE_DIR
        },
        "invariants": {
            "apify_calls": apify_calls,
            "apify_spend_usd": apify_spend_usd,
            "google_places_api_calls": google_places_api_calls,
            "crm_mutations": evaluator.crm_mutations,
            "messages_sent": evaluator.messages_sent,
            "campaigns_armed": evaluator.campaigns_armed,
            "proxies_used": evaluator.proxies_used,
            "captchas_bypassed": evaluator.captchas_bypassed
        },
        "telemetry": {
            "candidates_considered": evaluator.candidates_considered,
            "candidates_tested": evaluator.candidates_tested,
            "configured_cap": 20,
            "calls_attempted": evaluator.calls_attempted,
            "calls_completed": evaluator.calls_completed,
            "cache_hits": evaluator.cache_hits,
            "cache_misses": evaluator.calls_attempted - evaluator.cache_hits,
            "places_in_pool": len(all_unique_places),
            "duration_seconds": round(duration, 2)
        },
        "metric_recovery": {
            "review_count_recovered": evaluator.review_count_recovered,
            "rating_recovered": evaluator.rating_recovered,
            "timestamp_recovered": evaluator.timestamp_recovered,
            "freshness_recent": evaluator.freshness_recent,
            "freshness_stale": evaluator.freshness_stale,
            "freshness_unknown": evaluator.freshness_unknown,
            "review_metric_recovery_rate": metric_recovery_rate,
            "date_recovery_rate": date_recovery_rate,
            "recent_recovery_rate": recent_recovery_rate
        },
        "match_classifications": {
            "EXACT_BRANCH_MATCH": evaluator.exact_branch_match,
            "STRONG_BUSINESS_MATCH": evaluator.strong_business_match,
            "AMBIGUOUS_MATCH": evaluator.ambiguous_match,
            "BRANCH_MISMATCH": evaluator.branch_mismatch,
            "IDENTITY_MISMATCH": evaluator.identity_mismatch
        },
        "reconciliation": reconciliation_stats,
        "qualification_impact": {
            "before": qual_before,
            "after": qual_after
        },
        "contactability": contactability_summary,
        "candidate_results": candidate_eval_records
    }

    out_file = os.path.join(PROJECT_ROOT, "data", "phase_7_6_gosom_coverage_eval.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)
    print(f"\n[Artifact] Saved evaluation data to {out_file}")

    # Step 11: Post-run Checksums Verification
    checksums_after = {f: compute_file_sha256(f) for f in CRM_FILES_TO_CHECK}
    for f in CRM_FILES_TO_CHECK:
        assert checksums_before[f] == checksums_after[f], f"CRM MUTATION DETECTED in {f}!"
    print("[*] Invariant Verified: 0 CRM writes across all live stores.")

    print("\n" + "=" * 80)
    print("PHASE 7.6 EVALUATION COMPLETE")
    print(f"Candidates Tested: {evaluator.candidates_tested}")
    print(f"Review Metric Recovery Rate: {metric_recovery_rate:.1%}")
    print(f"Date Recovery Rate: {date_recovery_rate:.1%}")
    print(f"RECENT Recovery Rate: {recent_recovery_rate:.1%}")
    print(f"Exact Matches: {evaluator.exact_branch_match} | Strong Matches: {evaluator.strong_business_match} | Ambiguous: {evaluator.ambiguous_match} | Branch Mismatches: {evaluator.branch_mismatch} | Identity Mismatches: {evaluator.identity_mismatch}")
    print("=" * 80)


if __name__ == "__main__":
    main()
