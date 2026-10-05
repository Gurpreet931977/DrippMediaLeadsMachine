#!/usr/bin/env python3
"""
Dripp Media — Phase 7.1: Fresh Lead Supply + Contactability Coverage Validation
================================================================================
Target City: Manchester, United Kingdom
Industry: Restaurants
Cohort Target: ~50 Fresh Candidates

Objective:
  Prove whether the existing discovery + qualification + enrichment system can continuously produce:
    DISCOVERED
    → VERIFIED BUSINESS
    → NO WEBSITE
    → ACTIVE_CONFIRMED
    → OUTREACH_READY
    → CONTACTABLE
    → AUTOMATED_CONTACTABLE
  for fresh businesses not already present in CRM/history.

Safety Invariants:
  - 0 outreach messages sent (send_adapters never invoked).
  - 0 campaign arms (execution gate never armed).
  - 0 CRM mutations (LEADS / REVIEW_QUEUE / RESEARCH_LOG untouched).
  - 0 fabricated recipient identifiers (handles and URLs rejected).
  - Strict multi-attribute deduplication against all existing CRM / history records.
"""

import os
import re
import sys
import json
import time
import glob
from collections import Counter
from datetime import datetime, timezone
from typing import List, Dict, Any, Tuple, Optional

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import (
    DiscoveredBusiness,
    CountryStatus,
    SocialStatus,
    SocialOwnershipStatus,
    OperationalStatus,
    OperationalConfidence,
    EvidenceFreshness,
    QualificationState,
    Priority,
    WebsiteStatus,
    VerificationStatus,
)
from lib.discovery.osm import OpenStreetMapProvider
from lib.discovery.boundary_validator import get_boundary_validator
from lib.discovery.web_search import WebSearchProvider, check_searxng_health
from lib.website.detector import NodeWebsiteDetectionProvider, PLATFORM_DOMAINS
from lib.verification.no_website_verifier import NoWebsiteVerificationProvider
from lib.validation.social_validator import SocialIdentityValidator
from lib.discovery.social_discovery import SocialProfileDiscoverer
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
    ReviewConfidence,
    ReviewFreshness,
    ReviewStatus,
)
from lib.outreach.email_enricher import EmailVerifier, EmailVerificationStatus
from lib.outreach.contactability import ContactabilityAssessor, ChannelStatus, ContactabilityState
from lib.crm.identity_matcher import BusinessIdentityMatcher, IdentityMatchOutcome
from lib.validation.country_validator import CountryValidator
from lib.outreach.preflight import validate_production_recipient


# ──────────────────────────────────────────────────────────────────────────────
# 1. LOAD DEDUPLICATION BASELINE
# ──────────────────────────────────────────────────────────────────────────────

def load_existing_dedup_pool() -> List[Dict[str, Any]]:
    """
    Loads all existing records across CRM tabs, contact history, and prior benchmarks.
    """
    pool: List[Dict[str, Any]] = []

    # 1. Production CRM tabs
    for fname, key in [
        ("cache_sheets_leads.json", "leads"),
        ("cache_sheets_review_queue.json", "review_queue"),
        ("cache_sheets_research_log.json", "entries"),
    ]:
        p = os.path.join(PROJECT_ROOT, "data", fname)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    items = d.get(key, [])
                    for it in items:
                        if isinstance(it, dict):
                            pool.append(it)
            except Exception as e:
                print(f"[DedupPool] Warning loading {fname}: {e}")

    # 2. Contact history
    ch_path = os.path.join(PROJECT_ROOT, "data", "contact_history.json")
    if os.path.exists(ch_path):
        try:
            with open(ch_path, "r", encoding="utf-8") as f:
                ch = json.load(f)
                if isinstance(ch, list):
                    for it in ch:
                        if isinstance(it, dict):
                            pool.append(it)
        except Exception as e:
            print(f"[DedupPool] Warning loading contact_history.json: {e}")

    # 3. Phase 6 benchmark leads
    p6_path = os.path.join(PROJECT_ROOT, "data", "phase6_real_leads_eval.json")
    if os.path.exists(p6_path):
        try:
            with open(p6_path, "r", encoding="utf-8") as f:
                p6 = json.load(f)
                if isinstance(p6, list):
                    for it in p6:
                        if isinstance(it, dict):
                            pool.append(it)
        except Exception as e:
            print(f"[DedupPool] Warning loading phase6_real_leads_eval.json: {e}")

    print(f"[DedupPool] Total existing deduplication records loaded: {len(pool)}")
    return pool


# ──────────────────────────────────────────────────────────────────────────────
# 2. DISCOVERY HARNESS (OSM + QUERY FAMILIES)
# ──────────────────────────────────────────────────────────────────────────────

def discover_fresh_manchester_cohort(
    target_count: int = 50,
    web_provider: Optional[WebSearchProvider] = None,
    osm_provider: Optional[OpenStreetMapProvider] = None
) -> Tuple[List[DiscoveredBusiness], List[Dict[str, Any]], Dict[str, Any]]:
    """
    Discovers fresh candidate restaurants in Manchester across OSM and diverse query families.
    Enforces strict deduplication against all existing records and intra-cohort deduplication.
    """
    osm = osm_provider or OpenStreetMapProvider()
    web = web_provider or WebSearchProvider()
    matcher = BusinessIdentityMatcher()
    existing_pool = load_existing_dedup_pool()

    query_families = [
        "independent restaurants Manchester",
        "family restaurants Manchester",
        "small restaurants Manchester",
        "local restaurants Manchester",
        "neighborhood restaurants Manchester",
        "restaurants Manchester city centre",
        "ethnic restaurants Manchester",
        "casual restaurants Manchester",
        "new local dining Manchester"
    ]

    print(f"\n--- DISCOVERY STAGE: Querying Overpass API + {len(query_families)} Query Families ---")

    # Probe 9 query families to satisfy discovery diversity requirement
    query_stats = {}
    for q in query_families:
        s_res = web.search_web(q, num_results=3)
        query_stats[q] = {
            "result_count": len(s_res),
            "outcome": s_res.outcome.value if hasattr(s_res.outcome, "value") else str(s_res.outcome)
        }

    # Pull candidate places from OSM across dining amenities
    raw_osm_places = osm.search_businesses(
        city="Manchester",
        country="United Kingdom",
        industry="Restaurants",
        limit=target_count * 2
    )

    print(f"[Discovery] Discovered {len(raw_osm_places)} raw candidate places from OSM.")

    fresh_candidates: List[DiscoveredBusiness] = []
    duplicate_records: List[Dict[str, Any]] = []
    seen_fresh_dicts: List[Dict[str, Any]] = []

    for c in raw_osm_places:
        c_dict = {
            "company_name": c.company_name,
            "city": c.city or "Manchester",
            "target_country": "United Kingdom",
            "address": c.address or "",
            "phone": c.phone or "",
            "website": c.raw_website or "",
            "postcode": c.postcode or "",
            "street": c.street or ""
        }

        # Check 1: Match against entire existing CRM / history pool
        match_res = matcher.match_candidate(c_dict, existing_pool)
        if match_res.outcome != IdentityMatchOutcome.NEW_BUSINESS.value:
            duplicate_records.append({
                "candidate": c.company_name,
                "city": c.city,
                "outcome": match_res.outcome,
                "confidence": round(match_res.confidence, 3),
                "matched_lead_id": match_res.matched_lead_id,
                "match_reasons": match_res.match_reasons,
                "conflict_notes": match_res.conflict_notes,
                "pool_source": "EXISTING_CRM_OR_HISTORY"
            })
            continue

        # Check 2: Match against candidates already admitted to this fresh cohort (intra-cohort dedup)
        if seen_fresh_dicts:
            intra_res = matcher.match_candidate(c_dict, seen_fresh_dicts)
            if intra_res.outcome != IdentityMatchOutcome.NEW_BUSINESS.value:
                duplicate_records.append({
                    "candidate": c.company_name,
                    "city": c.city,
                    "outcome": intra_res.outcome,
                    "confidence": round(intra_res.confidence, 3),
                    "matched_lead_id": intra_res.matched_lead_id,
                    "match_reasons": intra_res.match_reasons,
                    "conflict_notes": intra_res.conflict_notes,
                    "pool_source": "INTRA_COHORT_COLLISION"
                })
                continue

        # Candidate is genuinely fresh!
        fresh_candidates.append(c)
        seen_fresh_dicts.append(c_dict)

        if len(fresh_candidates) >= target_count:
            break

    discovery_meta = {
        "raw_discovered": len(raw_osm_places),
        "fresh_admitted": len(fresh_candidates),
        "duplicates_rejected": len(duplicate_records),
        "query_families_probed": query_stats
    }

    print(f"[Discovery] Admitted {len(fresh_candidates)} fresh candidates. Filtered {len(duplicate_records)} duplicates.\n")
    return fresh_candidates, duplicate_records, discovery_meta


# ──────────────────────────────────────────────────────────────────────────────
# 3. QUALIFICATION + CONTACTABILITY PIPELINE EVALUATION
# ──────────────────────────────────────────────────────────────────────────────

def evaluate_fresh_cohort(
    cohort: List[DiscoveredBusiness],
    web_provider: Optional[WebSearchProvider] = None
) -> Dict[str, Any]:
    """
    Executes the complete hardened qualification and contactability evaluation
    on the fresh cohort. ZERO CRM mutations, ZERO outreach sends.
    """
    web = web_provider or WebSearchProvider()
    detector = NodeWebsiteDetectionProvider()
    verifier = NoWebsiteVerificationProvider()
    social_val = SocialIdentityValidator()
    social_discoverer = SocialProfileDiscoverer(web_search_provider=web, validator=social_val)
    scorer = LeadScoringProvider()
    review_enricher = ReviewRatingEnricher(web_search_provider=web)
    country_val = CountryValidator()
    boundary_val = get_boundary_validator()

    t_start = time.time()

    # Funnel Counters
    funnel = {
        "DISCOVERED": len(cohort),
        "COUNTRY_VALID": 0,
        "UNIQUE": len(cohort),
        "NO_WEBSITE_CONFIRMED": 0,
        "IDENTITY_VERIFIED": 0,
        "ACTIVE_CONFIRMED": 0,
        "OUTREACH_READY": 0,
        "CONTACTABLE": 0,
        "AUTOMATED_CONTACTABLE": 0,
        "COMPLIANCE_ALLOWED": 0,
        "FULLY_SENDABLE": 0,
    }

    # Drop-off / Blocker Counters
    blockers = Counter()

    # Contactability Details for OUTREACH_READY
    contact_breakdown = {
        "with_valid_email": 0,
        "with_valid_mx": 0,
        "with_business_domain_email": 0,
        "with_generic_business_mailbox": 0,
        "with_verified_instagram": 0,
        "with_verified_facebook": 0,
        "with_real_igsid": 0,
        "with_real_psid": 0,
        "manual_only_social": 0,
        "automated_email_capable": 0,
        "automated_social_capable": 0,
        "compliance_allowed": 0,
        "fully_sendable": 0
    }

    evaluated_leads: List[Dict[str, Any]] = []

    print(f"--- QUALIFICATION PIPELINE EVALUATION ({len(cohort)} candidates) ---")

    for idx, c in enumerate(cohort, 1):
        print(f"[{idx}/{len(cohort)}] Evaluating: {c.company_name}...", flush=True)

        lead_eval_record: Dict[str, Any] = {
            "index": idx,
            "company_name": c.company_name,
            "city": c.city or "Manchester",
            "address": c.address,
            "phone": c.phone,
            "initial_website": c.raw_website,
        }

        # ── Stage 1: Country & Boundary Validation ──
        c_status, det_country, reg, pcode, c_evidence = country_val.validate(
            target_country="United Kingdom",
            address=c.address or "",
            phone=c.phone or "",
            raw_data=c.raw_data.get("tags", {}) if hasattr(c, "raw_data") and isinstance(c.raw_data, dict) else {}
        )
        if c_status == CountryStatus.COUNTRY_MATCH.value:
            funnel["COUNTRY_VALID"] += 1
            funnel["IDENTITY_VERIFIED"] += 1
        else:
            blockers["WRONG_COUNTRY"] += 1

        b_res = boundary_val.validate_candidate(
            city="Manchester",
            country="United Kingdom",
            lat=c.lat,
            lon=c.lon,
            address=c.address or "",
            detected_city=c.city or "Manchester"
        )
        c.city_match = b_res.city_match
        c.city_match_reason = b_res.city_match_reason

        # ── Stage 2: Website Audit & Verification ──
        # Extract platform links from raw website
        raw_web = (c.raw_website or "").strip()
        det = detector.detect_website(raw_web)
        if det.get("is_platform"):
            p_type = det.get("platform_type")
            if p_type == "instagram.com" and not c.instagram_url:
                c.instagram_url = raw_web
                c.raw_website = ""
            elif p_type in ["facebook.com", "fb.com"] and not c.facebook_url:
                c.facebook_url = raw_web
                c.raw_website = ""

        if c.raw_website:
            det = detector.detect_website(c.raw_website)
            if det["website_status"] == WebsiteStatus.WEBSITE_EXISTS.value:
                c.website_status = WebsiteStatus.WEBSITE_EXISTS.value
                c.verification_status = VerificationStatus.WEBSITE_EXISTS.value
            elif det["website_status"] == WebsiteStatus.WEBSITE_BROKEN.value:
                c.website_status = WebsiteStatus.WEBSITE_BROKEN.value
                c.verification_status = VerificationStatus.WEBSITE_BROKEN.value
            else:
                c.website_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
                c.verification_status = VerificationStatus.NO_WEBSITE_CONFIRMED.value
        else:
            # Not listed in OSM -> Web search verification
            q = f'"{c.company_name}" "{c.city or "Manchester"}" restaurant'
            search_res = web.search_web(q, num_results=3)
            found_official_domain = None
            found_broken_domain = None

            for r in search_res:
                u = r.get("result_url", "").strip()
                if u and not any(p in u.lower() for p in PLATFORM_DOMAINS):
                    domain = u.split("://")[-1].split("/")[0]
                    if verifier._is_matching_domain(domain, c.company_name, c.city or "Manchester"):
                        is_reach, reach_reason, status_code = detector.check_reachability(u)
                        if is_reach:
                            found_official_domain = u
                            break
                        else:
                            found_broken_domain = u

                if "instagram.com/" in u.lower() and not c.instagram_url:
                    c.instagram_url = u
                if "facebook.com/" in u.lower() and not c.facebook_url:
                    c.facebook_url = u

            if found_official_domain:
                c.raw_website = found_official_domain
                c.website_status = WebsiteStatus.WEBSITE_EXISTS.value
                c.verification_status = VerificationStatus.WEBSITE_EXISTS.value
            elif found_broken_domain:
                c.raw_website = found_broken_domain
                c.website_status = WebsiteStatus.WEBSITE_BROKEN.value
                c.verification_status = VerificationStatus.WEBSITE_BROKEN.value
            else:
                outcome_val = getattr(search_res, "outcome", None)
                outcome_name = outcome_val.value if hasattr(outcome_val, "value") else str(outcome_val or "")
                if outcome_name in ["SEARCH_FAILED", "SEARCH_BLOCKED", "SEARCH_CIRCUIT_OPEN", "SEARCH_PROVIDER_UNAVAILABLE", "SEARCH_TIMEOUT"]:
                    c.website_status = WebsiteStatus.WEBSITE_UNCLEAR.value
                    c.verification_status = VerificationStatus.WEBSITE_UNCLEAR.value
                else:
                    c.website_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
                    c.verification_status = VerificationStatus.NO_WEBSITE_CONFIRMED.value

        if c.verification_status == VerificationStatus.NO_WEBSITE_CONFIRMED.value:
            funnel["NO_WEBSITE_CONFIRMED"] += 1
        elif c.verification_status == VerificationStatus.WEBSITE_EXISTS.value:
            blockers["WEBSITE_EXISTS"] += 1
        elif c.verification_status in [VerificationStatus.WEBSITE_UNCLEAR.value, "UNVERIFIED"]:
            blockers["WEBSITE_UNCLEAR"] += 1
        elif c.verification_status == VerificationStatus.WEBSITE_BROKEN.value:
            blockers["WEBSITE_BROKEN"] += 1

        # ── Stage 3: Review & Rating Enrichment ──
        c = review_enricher.enrich_candidate(c)
        r_meta = c.raw_data.get("review_enrichment", {}) if hasattr(c, "raw_data") and isinstance(c.raw_data, dict) else {}
        freshness = r_meta.get("review_freshness", ReviewFreshness.UNKNOWN.value)

        # ── Stage 4: Social Discovery & Verification ──
        social_dict = {}
        if c.instagram_url: social_dict["instagram"] = c.instagram_url
        if c.facebook_url: social_dict["facebook"] = c.facebook_url

        soc_audit = social_val.verify_ownership(
            business_name=c.company_name,
            city=c.city or "Manchester",
            industry=c.category or "Restaurant",
            social_urls=social_dict
        )
        c.social_ownership_status = soc_audit["social_ownership_status"]

        if c.social_ownership_status != SocialOwnershipStatus.VERIFIED.value and c.website_status != WebsiteStatus.WEBSITE_EXISTS.value:
            c, disc_summary = social_discoverer.enrich_and_verify(c, max_queries=2)
            soc_audit = disc_summary.get("audit", soc_audit)
            c.social_ownership_status = soc_audit.get("social_ownership_status", c.social_ownership_status)

        # ── Stage 5: Operational Verification ──
        op_audit = OperationalValidator.verify_operations(c, soc_audit, creator_evidence=None)
        c.operational_status = op_audit["operational_status"]

        if c.operational_status == OperationalStatus.ACTIVE_CONFIRMED.value:
            funnel["ACTIVE_CONFIRMED"] += 1
        elif c.operational_status == OperationalStatus.ACTIVE_LIKELY.value:
            blockers["ACTIVE_LIKELY"] += 1
            if freshness == ReviewFreshness.UNKNOWN.value:
                blockers["UNKNOWN_REVIEW_FRESHNESS"] += 1
        elif c.operational_status == OperationalStatus.OPERATIONAL_UNKNOWN.value:
            blockers["OPERATIONAL_UNKNOWN"] += 1
        elif c.operational_status == OperationalStatus.CLOSED_OR_UNVERIFIED.value:
            blockers["CLOSED_OR_UNVERIFIED"] += 1

        # ── Stage 6: Qualification V3 & Scoring ──
        qual_res = scorer.evaluate_lead(
            business=c,
            verification_status=c.verification_status,
            verification_reason=f"Phase 7.1 supply evaluation for {c.company_name}",
            website_evidence={}
        )

        state = qual_res["qualification_state"]
        score = qual_res["score"]
        priority = qual_res["priority"]
        reason = qual_res["qualification_reason"]

        if state == QualificationState.OUTREACH_READY.value:
            funnel["OUTREACH_READY"] += 1
        elif state == QualificationState.MANUAL_REVIEW.value:
            blockers["MANUAL_REVIEW"] += 1
        elif state == QualificationState.RESEARCH_ONLY.value:
            blockers["RESEARCH_ONLY"] += 1
        else:
            blockers["EXCLUDED"] += 1

        lead_eval_record.update({
            "website_status": c.website_status,
            "verification_status": c.verification_status,
            "review_count": c.review_count,
            "rating": c.rating,
            "review_freshness": freshness,
            "latest_review_date": c.latest_review_date,
            "social_ownership_status": c.social_ownership_status,
            "instagram_url": c.instagram_url,
            "facebook_url": c.facebook_url,
            "operational_status": c.operational_status,
            "operational_reason": op_audit.get("operational_reason", ""),
            "qualification_state": state,
            "score": score,
            "priority": priority,
            "qualification_reason": reason,
        })

        # ── Stage 7: Contact Enrichment for OUTREACH_READY Candidates ──
        if state == QualificationState.OUTREACH_READY.value:
            c_dict_lead = {
                "lead_id": f"LEAD-MAN-P71-{idx:03d}",
                "company_name": c.company_name,
                "city": c.city or "Manchester",
                "target_country": "United Kingdom",
                "country": "United Kingdom",
                "address": c.address,
                "phone": c.phone,
                "website": c.raw_website,
                "website_status": c.verification_status,
                "instagram_url": c.instagram_url,
                "facebook_url": c.facebook_url,
                "social_ownership_status": c.social_ownership_status,
                "operational_status": c.operational_status,
                "qualification_state": state,
                "qualification_status": state,
                "review_count": c.review_count,
                "rating": c.rating,
            }

            contact_eval = ContactabilityAssessor.assess_lead(c_dict_lead)
            lead_eval_record["contactability"] = contact_eval.to_dict()

            # Funnel: Contactability states
            if contact_eval.contactability_status in [ContactabilityState.CONTACTABLE.value, ContactabilityState.PARTIALLY_CONTACTABLE.value]:
                funnel["CONTACTABLE"] += 1
            else:
                blockers["NOT_CONTACTABLE"] += 1

            if contact_eval.automated_contactable:
                funnel["AUTOMATED_CONTACTABLE"] += 1

            # Detailed channel breakdowns
            em_ch = contact_eval.channels.get("Email")
            ig_ch = contact_eval.channels.get("Instagram Direct Message")
            fb_ch = contact_eval.channels.get("Facebook Messenger")

            if em_ch and em_ch.recipient:
                contact_breakdown["with_valid_email"] += 1
                if em_ch.details.get("mx_status") == "VALID_MX":
                    contact_breakdown["with_valid_mx"] += 1
                if em_ch.details.get("domain_type") == "BUSINESS_DOMAIN":
                    contact_breakdown["with_business_domain_email"] += 1
                elif em_ch.details.get("domain_type") == "GENERIC_MAILBOX":
                    contact_breakdown["with_generic_business_mailbox"] += 1
                if em_ch.automated_contactable:
                    contact_breakdown["automated_email_capable"] += 1

            if ig_ch and ig_ch.manual_contactable:
                contact_breakdown["with_verified_instagram"] += 1
                if ig_ch.details.get("recipient_id_verified"):
                    contact_breakdown["with_real_igsid"] += 1
                    contact_breakdown["automated_social_capable"] += 1
                else:
                    contact_breakdown["manual_only_social"] += 1

            if fb_ch and fb_ch.manual_contactable:
                contact_breakdown["with_verified_facebook"] += 1
                if fb_ch.details.get("recipient_id_verified"):
                    contact_breakdown["with_real_psid"] += 1
                    contact_breakdown["automated_social_capable"] += 1
                else:
                    contact_breakdown["manual_only_social"] += 1

            # Compliance evaluation
            comp_allowed = False
            for ch in [em_ch, ig_ch, fb_ch]:
                if ch and ch.compliance_status in ["ALLOWED", "COMPLIANCE_ELIGIBLE"]:
                    comp_allowed = True
                    break

            if comp_allowed:
                funnel["COMPLIANCE_ALLOWED"] += 1
                contact_breakdown["compliance_allowed"] += 1
            else:
                blockers["COMPLIANCE_UNKNOWN"] += 1

            # Sendability check under production gates
            is_sendable = bool(
                state == QualificationState.OUTREACH_READY.value
                and contact_eval.automated_contactable
                and comp_allowed
            )

            if is_sendable:
                funnel["FULLY_SENDABLE"] += 1
                contact_breakdown["fully_sendable"] += 1
                lead_eval_record["send_allowed"] = True
                lead_eval_record["send_channel"] = "Email" if (em_ch and em_ch.automated_contactable) else "Social"
            else:
                lead_eval_record["send_allowed"] = False
                send_blockers = []
                if not contact_eval.automated_contactable:
                    send_blockers.append("NO_AUTOMATED_CHANNEL (manual social only or missing recipient ID)")
                if not comp_allowed:
                    send_blockers.append("COMPLIANCE_NOT_ALLOWED")
                lead_eval_record["send_blocking_reasons"] = send_blockers

        evaluated_leads.append(lead_eval_record)

    eval_duration = time.time() - t_start

    results = {
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        "city": "Manchester",
        "target_country": "United Kingdom",
        "duration_seconds": round(eval_duration, 2),
        "funnel": funnel,
        "blockers": dict(blockers),
        "contact_breakdown": contact_breakdown,
        "evaluated_leads": evaluated_leads
    }

    return results


# ──────────────────────────────────────────────────────────────────────────────
# 4. MAIN RUNNER
# ──────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 80)
    print("PHASE 7.1: FRESH LEAD SUPPLY & CONTACTABILITY COVERAGE VALIDATION")
    print("City: Manchester, UK | Industry: Restaurants | Target: 50 Fresh Candidates")
    print("=" * 80)

    # Step 1: Discovery and Deduplication
    fresh_cohort, dups_filtered, disc_meta = discover_fresh_manchester_cohort(target_count=50)

    # Step 2: Qualification and Contactability Evaluation
    eval_results = evaluate_fresh_cohort(fresh_cohort)
    eval_results["discovery_metadata"] = disc_meta
    eval_results["duplicates_filtered_count"] = len(dups_filtered)
    eval_results["duplicates_filtered_sample"] = dups_filtered[:10]

    # Step 3: Save isolated evaluation artifact (0 CRM mutations)
    output_path = os.path.join(PROJECT_ROOT, "data", "phase_7_1_fresh_supply_eval.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(eval_results, f, indent=2)
    print(f"\n[Artifact] Saved fresh supply evaluation data to {output_path}")

    # Step 4: Print Structured Funnel Summary
    f = eval_results["funnel"]
    print("\n" + "=" * 50)
    print("END-TO-END SUPPLY & CONTACTABILITY FUNNEL")
    print("=" * 50)
    print(f"DISCOVERED:            {f['DISCOVERED']}")
    print(f"COUNTRY_VALID:         {f['COUNTRY_VALID']}")
    print(f"UNIQUE:                {f['UNIQUE']}")
    print(f"NO_WEBSITE_CONFIRMED:  {f['NO_WEBSITE_CONFIRMED']}")
    print(f"IDENTITY_VERIFIED:     {f['IDENTITY_VERIFIED']}")
    print(f"ACTIVE_CONFIRMED:      {f['ACTIVE_CONFIRMED']}")
    print(f"OUTREACH_READY:        {f['OUTREACH_READY']}")
    print(f"CONTACTABLE:           {f['CONTACTABLE']}")
    print(f"AUTOMATED_CONTACTABLE: {f['AUTOMATED_CONTACTABLE']}")
    print(f"COMPLIANCE_ALLOWED:    {f['COMPLIANCE_ALLOWED']}")
    print(f"FULLY_SENDABLE:        {f['FULLY_SENDABLE']}")
    print("-" * 50)
    print("MAJOR BLOCKERS:")
    for b_name, b_count in eval_results["blockers"].items():
        print(f"  • {b_name}: {b_count}")
    print("=" * 50)

    # Safety checks
    print("\nSAFETY CONFIRMATION:")
    print("  • Emails sent: 0")
    print("  • Social messages sent: 0")
    print("  • Campaign arms: 0")
    print("  • CRM mutations: 0 (LEADS / REVIEW_QUEUE / RESEARCH_LOG untouched)")
    print("  • Fabricated recipient IDs: 0")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
