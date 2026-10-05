import os
import sys
import json
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import (
    DiscoveredBusiness,
    WebsiteStatus,
    VerificationStatus,
    SocialOwnershipStatus,
    OperationalStatus,
    QualificationState
)
from lib.website.detector import NodeWebsiteDetectionProvider
from lib.validation.social_validator import SocialIdentityValidator
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.enrichment.review_rating_enricher import ReviewRatingEnricher
from lib.discovery.web_search import WebSearchProvider

with open("data/leeds_100_candidates.json") as f:
    cohort = json.load(f)

detector = NodeWebsiteDetectionProvider()
social_val = SocialIdentityValidator()
scorer = LeadScoringProvider()
web = WebSearchProvider()
enricher = ReviewRatingEnricher(web_search_provider=web)

# Trip circuit breaker on DDG to replicate post-14th query state
web.circuit_breakers["DUCKDUCKGO_FALLBACK"].trip()

results = []
for c_dict in cohort:
    c = DiscoveredBusiness(**c_dict)
    c = enricher.enrich_candidate(c)
    
    # Website status
    raw_web = (c.raw_website or "").strip()
    det = detector.detect_website(raw_web)
    if det["website_status"] == WebsiteStatus.WEBSITE_EXISTS.value:
        c.website_status = WebsiteStatus.WEBSITE_EXISTS.value
        c.verification_status = VerificationStatus.WEBSITE_EXISTS.value
    elif det["website_status"] == WebsiteStatus.WEBSITE_BROKEN.value:
        c.website_status = WebsiteStatus.WEBSITE_BROKEN.value
        c.verification_status = VerificationStatus.WEBSITE_BROKEN.value
    else:
        c.website_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
        c.verification_status = VerificationStatus.NO_WEBSITE_CONFIRMED.value

    # Social
    tags = c_dict.get("raw_data", {}).get("tags", {})
    if not c.facebook_url and tags.get("contact:facebook"):
        c.facebook_url = tags["contact:facebook"]
    if not c.instagram_url and tags.get("contact:instagram"):
        c.instagram_url = tags["contact:instagram"]
    if not c.phone and tags.get("contact:phone"):
        c.phone = tags["contact:phone"]

    soc_dict = {}
    if c.instagram_url: soc_dict["instagram"] = c.instagram_url
    if c.facebook_url: soc_dict["facebook"] = c.facebook_url
    soc_audit = social_val.verify_ownership(c.company_name, c.city, c.category or "Restaurant", soc_dict)
    c.social_ownership_status = soc_audit["social_ownership_status"]

    # Operational
    check_date = tags.get("check_date") or tags.get("check_date:opening_hours") or ""
    if check_date and not c.latest_review_date:
        c.latest_review_date = check_date
    op_audit = OperationalValidator.verify_operations(c, soc_audit)
    c.operational_status = op_audit["operational_status"]

    # Qualification
    qual_res = scorer.evaluate_lead(c, c.verification_status, f"Pipeline evaluation for {c.company_name}")
    state = qual_res["qualification_state"]
    reason = qual_res["qualification_reason"]

    results.append({
        "business": c.company_name,
        "city": c.city,
        "website_status": c.website_status,
        "operational_status": c.operational_status,
        "review_count": c.review_count,
        "rating": c.rating,
        "social_status": c.social_ownership_status,
        "creator_evidence": "NONE",
        "outcome": state,
        "reason": reason
    })

print(f"Total processed: {len(results)}")
print("Outcomes:", Counter(r["outcome"] for r in results))

print("\n| Business | City | Website Status | Operational Status | Reviews | Rating | Social Status | Creator Ev | Outcome | Blocking Reason |")
print("|---|---|---|---|---:|---:|---|---|---|---|")
for r in results[-25:]:
    b = r["business"]
    city = r["city"]
    ws = r["website_status"]
    ops = r["operational_status"]
    rev = str(r["review_count"]) if r["review_count"] is not None else "None"
    rat = str(r["rating"]) if r["rating"] is not None else "None"
    soc = r["social_status"]
    cev = r["creator_evidence"]
    out = r["outcome"]
    reas = r["reason"].replace("|", "/")
    print(f"| {b} | {city} | {ws} | {ops} | {rev} | {rat} | {soc} | {cev} | {out} | {reas} |")
