import os
import sys
import json
import time
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import (
    DiscoveredBusiness,
    CountryStatus,
    WebsiteStatus,
    VerificationStatus,
    SocialStatus,
    SocialOwnershipStatus,
    OperationalStatus,
    OperationalConfidence,
    EvidenceFreshness,
    QualificationState,
    Priority
)
from lib.validation.country_validator import CountryValidator
from lib.website.detector import NodeWebsiteDetectionProvider, PLATFORM_DOMAINS
from lib.verification.no_website_verifier import NoWebsiteVerificationProvider
from lib.validation.social_validator import SocialIdentityValidator
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.enrichment.review_rating_enricher import ReviewRatingEnricher, ReviewConfidence, ReviewStatus
from lib.discovery.web_search import WebSearchProvider

def run_audit():
    print("="*60)
    print("LIVE LEAD QUALITY & QUALIFICATION AUDIT - LEEDS BENCHMARK")
    print("="*60)

    with open("data/leeds_100_candidates.json", "r") as f:
        cohort = json.load(f)

    detector = NodeWebsiteDetectionProvider()
    verifier = NoWebsiteVerificationProvider()
    web = WebSearchProvider()
    social_val = SocialIdentityValidator()
    scorer = LeadScoringProvider()
    review_enricher = ReviewRatingEnricher(web_search_provider=web)
    country_val = CountryValidator()

    # Stage Counters
    n_raw_discovered = len(cohort)
    n_country_valid = 0
    n_unique = 0
    n_researched = 0
    n_website_found = 0
    n_no_website_confirmed = 0
    n_website_broken_or_unclear = 0
    n_operationally_verified = 0 # ACTIVE_CONFIRMED
    n_operational_likely = 0
    n_operational_unknown = 0
    n_review_evidence_found = 0
    n_rating_usable = 0
    n_social_evidence_found = 0
    n_social_verified = 0
    n_creator_evidence_found = 0
    n_qualification_candidates = 0

    n_outreach_ready = 0
    n_manual_review = 0
    n_research_only = 0
    n_excluded = 0

    seen_keys = set()
    processed_candidates = []

    for raw in cohort:
        c = DiscoveredBusiness(**raw)

        # 1. Country Check
        c_status, det_country, reg, pcode, c_evidence = country_val.validate(
            target_country="United Kingdom",
            address=c.address,
            phone=c.phone,
            raw_data=c.raw_data
        )
        if c_status == CountryStatus.COUNTRY_MATCH.value:
            n_country_valid += 1
        else:
            n_excluded += 1
            continue

        # 2. Deduplication
        dedup_key = f"{c.company_name.lower().strip()}_{c.city.lower().strip()}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        n_unique += 1

        # 3. Researched
        n_researched += 1

        # 4. Review / Rating Enrichment (Free web search cascade)
        c = review_enricher.enrich_candidate(c)
        enrich_meta = c.raw_data.get("review_enrichment", {}) if hasattr(c, "raw_data") and isinstance(c.raw_data, dict) else {}
        if c.review_count is not None:
            n_review_evidence_found += 1
        if c.rating is not None and c.rating >= 4.0:
            n_rating_usable += 1

        # 5. Website Audit & Deep Verification
        raw_web = (c.raw_website or "").strip()
        det = detector.detect_website(raw_web)

        # Platform URL extraction
        if det.get("is_platform"):
            p_type = det.get("platform_type")
            if p_type == "instagram.com" and not c.instagram_url:
                c.instagram_url = raw_web
                c.raw_website = ""
            elif p_type in ["facebook.com", "fb.com"] and not c.facebook_url:
                c.facebook_url = raw_web
                c.raw_website = ""

        # OSM contact tags
        tags = raw.get("raw_data", {}).get("tags", {})
        if not c.facebook_url and tags.get("contact:facebook"):
            c.facebook_url = tags["contact:facebook"]
        if not c.instagram_url and tags.get("contact:instagram"):
            c.instagram_url = tags["contact:instagram"]
        if not c.phone and tags.get("contact:phone"):
            c.phone = tags["contact:phone"]

        if c.raw_website:
            det = detector.detect_website(c.raw_website)
            if det["website_status"] == WebsiteStatus.WEBSITE_EXISTS.value:
                c.website_status = WebsiteStatus.WEBSITE_EXISTS.value
                c.verification_status = VerificationStatus.WEBSITE_EXISTS.value
                n_website_found += 1
            elif det["website_status"] == WebsiteStatus.WEBSITE_BROKEN.value:
                c.website_status = WebsiteStatus.WEBSITE_BROKEN.value
                c.verification_status = VerificationStatus.WEBSITE_BROKEN.value
                n_website_broken_or_unclear += 1
            else:
                c.website_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
                c.verification_status = VerificationStatus.NO_WEBSITE_CONFIRMED.value
                n_no_website_confirmed += 1
        else:
            # Query web search for domain
            q = f'"{c.company_name}" "{c.city}" restaurant'
            search_res = web.search_web(q, num_results=3)
            found_official = None
            found_broken = None
            for r in search_res:
                u = r.get("result_url", "").strip()
                if u and not any(p in u.lower() for p in PLATFORM_DOMAINS):
                    domain = u.split("://")[-1].split("/")[0]
                    if verifier._is_matching_domain(domain, c.company_name, c.city):
                        is_reach, _, _ = detector.check_reachability(u)
                        if is_reach:
                            found_official = u
                            break
                        else:
                            found_broken = u
                if "instagram.com/" in u.lower() and not c.instagram_url:
                    c.instagram_url = u
                if "facebook.com/" in u.lower() and not c.facebook_url:
                    c.facebook_url = u

            if found_official:
                c.raw_website = found_official
                c.website_status = WebsiteStatus.WEBSITE_EXISTS.value
                c.verification_status = VerificationStatus.WEBSITE_EXISTS.value
                n_website_found += 1
            elif found_broken:
                c.raw_website = found_broken
                c.website_status = WebsiteStatus.WEBSITE_BROKEN.value
                c.verification_status = VerificationStatus.WEBSITE_BROKEN.value
                n_website_broken_or_unclear += 1
            else:
                c.website_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
                c.verification_status = VerificationStatus.NO_WEBSITE_CONFIRMED.value
                n_no_website_confirmed += 1

        # 6. Social Verification
        social_dict = {}
        if c.instagram_url: social_dict["instagram"] = c.instagram_url
        if c.facebook_url: social_dict["facebook"] = c.facebook_url
        if social_dict:
            n_social_evidence_found += 1

        soc_audit = social_val.verify_ownership(
            business_name=c.company_name,
            city=c.city,
            industry=c.category or "Restaurant",
            social_urls=social_dict
        )
        c.social_ownership_status = soc_audit["social_ownership_status"]
        if c.social_ownership_status == SocialOwnershipStatus.VERIFIED.value:
            n_social_verified += 1

        # 7. Creator Evidence (Secondary)
        creator_ev = c.raw_data.get("creator_evidence") if hasattr(c, "raw_data") and isinstance(c.raw_data, dict) else None
        if creator_ev:
            n_creator_evidence_found += 1

        # 8. Operational Verification
        check_date = tags.get("check_date") or tags.get("check_date:opening_hours") or ""
        if check_date and not c.latest_review_date:
            c.latest_review_date = check_date

        op_audit = OperationalValidator.verify_operations(c, soc_audit, creator_evidence=creator_ev)
        c.operational_status = op_audit["operational_status"]
        if c.operational_status == OperationalStatus.ACTIVE_CONFIRMED.value:
            n_operationally_verified += 1
        elif c.operational_status == OperationalStatus.ACTIVE_LIKELY.value:
            n_operational_likely += 1
        else:
            n_operational_unknown += 1

        # 9. Qualification Candidates (Passed country, unique, researched, confirmed no website)
        if c.website_status == WebsiteStatus.NO_WEBSITE_CONFIRMED.value:
            n_qualification_candidates += 1

        # 10. Qualification V3 & Scoring
        qual_res = scorer.evaluate_lead(
            business=c,
            verification_status=c.verification_status,
            verification_reason=f"Pipeline evaluation for {c.company_name}",
            website_evidence={}
        )

        state = qual_res["qualification_state"]
        reason = qual_res["qualification_reason"]

        if state == QualificationState.OUTREACH_READY.value:
            n_outreach_ready += 1
        elif state == QualificationState.MANUAL_REVIEW.value:
            n_manual_review += 1
        elif state == QualificationState.RESEARCH_ONLY.value:
            n_research_only += 1
        else:
            n_excluded += 1

        processed_candidates.append({
            "business": c.company_name,
            "city": c.city,
            "website_status": c.website_status,
            "operational_status": c.operational_status,
            "review_count": c.review_count,
            "rating": c.rating,
            "social_status": c.social_ownership_status,
            "creator_evidence": "FOUND" if creator_ev else "NONE",
            "outcome": state,
            "reason": reason
        })

    # Print Funnel Table
    stages = [
        ("Discovered", n_raw_discovered),
        ("Country valid", n_country_valid),
        ("Unique", n_unique),
        ("Researched", n_researched),
        ("Website checked", n_researched),
        ("Website found", n_website_found),
        ("Website broken/unclear", n_website_broken_or_unclear),
        ("No website confirmed", n_no_website_confirmed),
        ("Review evidence found", n_review_evidence_found),
        ("Rating usable (>=4.0)", n_rating_usable),
        ("Social evidence found", n_social_evidence_found),
        ("Social verified", n_social_verified),
        ("Creator evidence found", n_creator_evidence_found),
        ("Operationally verified (ACTIVE_CONFIRMED)", n_operationally_verified),
        ("Operational likely (ACTIVE_LIKELY)", n_operational_likely),
        ("Operational unknown", n_operational_unknown),
        ("Qualification candidates", n_qualification_candidates),
        ("OUTREACH_READY", n_outreach_ready),
        ("MANUAL_REVIEW", n_manual_review),
        ("RESEARCH_ONLY", n_research_only),
        ("EXCLUDED", n_excluded)
    ]

    print("\n--- PIPELINE FUNNEL (LEEDS 100 COHORT) ---")
    print(f"{'Stage':<42} | {'Count':<6} | {'% of Prev':<10} | {'% of Orig':<10}")
    print("-" * 75)
    prev = n_raw_discovered
    for name, cnt in stages:
        pct_prev = (cnt / prev * 100) if prev else 0.0
        pct_orig = (cnt / n_raw_discovered * 100) if n_raw_discovered else 0.0
        print(f"{name:<42} | {cnt:<6} | {pct_prev:>8.1f}% | {pct_orig:>8.1f}%")
        # Update prev for linear progression stages
        if name in ["Discovered", "Country valid", "Unique", "Researched", "No website confirmed", "Qualification candidates"]:
            prev = cnt

    # Diagnostic sample of final 25 candidates
    print("\n--- DIAGNOSTIC SAMPLE (LAST 25 CANDIDATES) ---")
    sample = processed_candidates[-25:]
    print(f"{'Business':<25} | {'City':<10} | {'Web Status':<18} | {'Op Status':<16} | {'Revs':<5} | {'Rate':<5} | {'Social':<10} | {'Outcome':<14} | {'Blocking Reason'}")
    print("-" * 150)
    for s in sample:
        rev_str = str(s['review_count']) if s['review_count'] is not None else "None"
        rat_str = str(s['rating']) if s['rating'] is not None else "None"
        print(f"{s['business'][:25]:<25} | {s['city'][:10]:<10} | {s['website_status'][:18]:<18} | {s['operational_status'][:16]:<16} | {rev_str:<5} | {rat_str:<5} | {s['social_status'][:10]:<10} | {s['outcome'][:14]:<14} | {s['reason'][:60]}")

    return {
        "stages": dict(stages),
        "sample": sample
    }

if __name__ == "__main__":
    run_audit()
