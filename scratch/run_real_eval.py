#!/usr/bin/env python3
"""
Evaluate the 100 Leeds candidates from data/leeds_100_candidates.json
through the complete REAL pipeline with ZERO manually injected profiles or synthetic values.
"""
import os
import re
import json
import time
from collections import Counter
from typing import Dict, Any, List

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
    VerificationStatus
)
from lib.discovery.web_search import WebSearchProvider
from lib.website.detector import NodeWebsiteDetectionProvider, PLATFORM_DOMAINS
from lib.verification.no_website_verifier import NoWebsiteVerificationProvider
from lib.validation.social_validator import SocialIdentityValidator
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.outreach.email_enricher import EmailVerifier, EmailVerificationStatus
from lib.outreach.contactability import ContactabilityAssessor

def run_real_evaluation():
    with open("data/leeds_100_candidates.json", "r", encoding="utf-8") as f:
        cands_json = json.load(f)

    detector = NodeWebsiteDetectionProvider()
    verifier = NoWebsiteVerificationProvider()
    web = WebSearchProvider()
    social_val = SocialIdentityValidator()
    scorer = LeadScoringProvider()

    print(f"Total candidates loaded: {len(cands_json)}")

    # Counters
    website_exists = 0
    no_website_confirmed = 0
    website_unresolved = 0
    website_broken = 0

    operational_active_confirmed = 0
    operational_active_likely = 0
    operational_unknown = 0
    operational_unverified_closed = 0

    social_verified = 0
    social_unverified = 0
    social_invalid_missing = 0

    outreach_ready = 0
    manual_review = 0
    research_only = 0
    excluded = 0

    rejection_reasons = Counter()

    results = []

    for idx, raw in enumerate(cands_json, 1):
        c = DiscoveredBusiness(**raw)
        
        # 1. Website detection / audit on OSM raw_website
        raw_web = (c.raw_website or "").strip()
        det = detector.detect_website(raw_web)

        # Handle platform URLs (e.g. Instagram / Facebook in website field)
        if det.get("is_platform"):
            p_type = det.get("platform_type")
            if p_type == "instagram.com" and not c.instagram_url:
                c.instagram_url = raw_web
                c.raw_website = ""
            elif p_type in ["facebook.com", "fb.com"] and not c.facebook_url:
                c.facebook_url = raw_web
                c.raw_website = ""

        # Check OSM contact tags if present in raw_data
        tags = raw.get("raw_data", {}).get("tags", {})
        if not c.facebook_url and tags.get("contact:facebook"):
            c.facebook_url = tags["contact:facebook"]
        if not c.instagram_url and tags.get("contact:instagram"):
            c.instagram_url = tags["contact:instagram"]
        if not c.phone and tags.get("contact:phone"):
            c.phone = tags["contact:phone"]

        # Re-check website after platform extraction
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
            # Missing in OSM -> perform web search enrichment if cached/available
            q = f'"{c.company_name}" "{c.city}" restaurant'
            search_res = web.search_web(q, num_results=3)
            found_official_domain = None
            found_broken_domain = None

            for r in search_res:
                u = r.get("result_url", "").strip()
                snip = r.get("snippet", "")
                title = r.get("title", "")

                # Check if official domain
                if u and not any(p in u.lower() for p in PLATFORM_DOMAINS):
                    domain = u.split("://")[-1].split("/")[0]
                    if verifier._is_matching_domain(domain, c.company_name, c.city):
                        is_reach, reach_reason, status_code = detector.check_reachability(u)
                        if is_reach:
                            found_official_domain = u
                            break
                        else:
                            found_broken_domain = u

                # Check social profiles from search results
                if "instagram.com/" in u.lower() and not c.instagram_url:
                    c.instagram_url = u
                if "facebook.com/" in u.lower() and not c.facebook_url:
                    c.facebook_url = u

                # Extract reviews / rating if snippet contains review count
                m_rev = re.search(r"(\d+)\s+reviews?", snip)
                if m_rev and c.review_count is None:
                    c.review_count = int(m_rev.group(1))
                m_rat = re.search(r"(?:rated\s*)?([1-5]\.\d)\s*(?:out of 5|stars|★)?", snip)
                if m_rat and c.rating is None:
                    c.rating = float(m_rat.group(1))

            if found_official_domain:
                c.raw_website = found_official_domain
                c.website_status = WebsiteStatus.WEBSITE_EXISTS.value
                c.verification_status = VerificationStatus.WEBSITE_EXISTS.value
            elif found_broken_domain:
                c.raw_website = found_broken_domain
                c.website_status = WebsiteStatus.WEBSITE_BROKEN.value
                c.verification_status = VerificationStatus.WEBSITE_BROKEN.value
            else:
                c.website_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
                c.verification_status = VerificationStatus.NO_WEBSITE_CONFIRMED.value

        # Website metrics
        if c.website_status == WebsiteStatus.WEBSITE_EXISTS.value:
            website_exists += 1
        elif c.website_status == WebsiteStatus.NO_WEBSITE_CONFIRMED.value:
            no_website_confirmed += 1
        elif c.website_status == WebsiteStatus.WEBSITE_BROKEN.value:
            website_broken += 1
        else:
            website_unresolved += 1

        # 2. Social Identity Verification
        social_dict = {}
        if c.instagram_url: social_dict["instagram"] = c.instagram_url
        if c.facebook_url: social_dict["facebook"] = c.facebook_url

        soc_audit = social_val.verify_ownership(
            business_name=c.company_name,
            city=c.city,
            industry=c.category or "Restaurant",
            social_urls=social_dict
        )

        c.social_ownership_status = soc_audit["social_ownership_status"]
        if c.social_ownership_status == SocialOwnershipStatus.VERIFIED.value:
            social_verified += 1
        elif c.social_ownership_status == SocialOwnershipStatus.UNVERIFIED.value:
            social_unverified += 1
        else:
            social_invalid_missing += 1

        # 3. Operational Verification
        # Check OSM check_date for freshness if available
        check_date = tags.get("check_date") or tags.get("check_date:opening_hours") or ""
        if check_date:
            c.latest_review_date = check_date

        op_audit = OperationalValidator.verify_operations(c, soc_audit, creator_evidence=None)
        c.operational_status = op_audit["operational_status"]

        if c.operational_status == OperationalStatus.ACTIVE_CONFIRMED.value:
            operational_active_confirmed += 1
        elif c.operational_status == OperationalStatus.ACTIVE_LIKELY.value:
            operational_active_likely += 1
        elif c.operational_status == OperationalStatus.OPERATIONAL_UNKNOWN.value:
            operational_unknown += 1
        else:
            operational_unverified_closed += 1

        # 4. Qualification V3 & Scoring
        qual_res = scorer.evaluate_lead(
            business=c,
            verification_status=c.verification_status,
            verification_reason=f"Pipeline evaluation for {c.company_name}",
            website_evidence={}
        )

        state = qual_res["qualification_state"]
        score = qual_res["score"]
        priority = qual_res["priority"]
        reason = qual_res["qualification_reason"]

        if state == QualificationState.OUTREACH_READY.value:
            outreach_ready += 1
        elif state == QualificationState.MANUAL_REVIEW.value:
            manual_review += 1
            # Primary rejection reason attribution
            if c.verification_status in [VerificationStatus.WEBSITE_BROKEN.value, VerificationStatus.WEBSITE_UNCLEAR.value]:
                rejection_reasons["website unresolved"] += 1
            elif (c.review_count or 0) < 50 and (c.review_count or 0) >= 10:
                rejection_reasons["insufficient reviews"] += 1
            elif (c.rating or 0.0) < 4.0 and (c.review_count or 0) >= 50:
                rejection_reasons["rating below threshold"] += 1
            elif c.social_ownership_status != SocialOwnershipStatus.VERIFIED.value:
                rejection_reasons["social ownership not verified"] += 1
            elif c.operational_status != OperationalStatus.ACTIVE_CONFIRMED.value:
                rejection_reasons["operational status not ACTIVE_CONFIRMED"] += 1
            elif len(qual_res.get("signals", {})) < 2:
                rejection_reasons["insufficient commercial signals"] += 1
            else:
                rejection_reasons["other qualification gate"] += 1
        elif state == QualificationState.RESEARCH_ONLY.value:
            research_only += 1
            rejection_reasons["insufficient reviews"] += 1
        else: # EXCLUDED
            excluded += 1
            if c.verification_status == VerificationStatus.WEBSITE_EXISTS.value:
                rejection_reasons["website exists"] += 1
            elif c.operational_status == OperationalStatus.CLOSED_OR_UNVERIFIED.value:
                rejection_reasons["operational status not ACTIVE_CONFIRMED"] += 1
            else:
                rejection_reasons["other qualification gate"] += 1

        results.append({
            "name": c.company_name,
            "city": c.city,
            "website_status": c.website_status,
            "raw_website": c.raw_website,
            "reviews": c.review_count,
            "rating": c.rating,
            "social_ownership": c.social_ownership_status,
            "operational_status": c.operational_status,
            "qualification_state": state,
            "score": score,
            "reason": reason
        })

    print("\n================ EVALUATION SUMMARY ================")
    print(f"Website:")
    print(f"  Existing: {website_exists}")
    print(f"  No website confirmed: {no_website_confirmed}")
    print(f"  Broken: {website_broken}")
    print(f"  Unresolved: {website_unresolved}")
    print(f"\nOperational:")
    print(f"  Active confirmed: {operational_active_confirmed}")
    print(f"  Active likely: {operational_active_likely}")
    print(f"  Unknown: {operational_unknown}")
    print(f"  Closed/unverified: {operational_unverified_closed}")
    print(f"\nSocial:")
    print(f"  Verified: {social_verified}")
    print(f"  Unverified: {social_unverified}")
    print(f"  Invalid/Missing: {social_invalid_missing}")
    print(f"\nQualification:")
    print(f"  OUTREACH_READY: {outreach_ready}")
    print(f"  MANUAL_REVIEW: {manual_review}")
    print(f"  RESEARCH_ONLY: {research_only}")
    print(f"  EXCLUDED: {excluded}")
    print(f"\nPrimary Rejection Reasons:")
    for reason, count in sorted(rejection_reasons.items(), key=lambda x: x[1], reverse=True):
        pct = (count / 100) * 100
        print(f"  - {reason}: {count} ({pct:.1f}%)")

if __name__ == "__main__":
    run_real_evaluation()
