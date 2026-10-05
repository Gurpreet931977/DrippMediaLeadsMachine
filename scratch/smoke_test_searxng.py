"""
Smoke Test for Local SearXNG Provider
Evaluates 5 real Leeds businesses from data/leeds_100_candidates.json
Runs 1 review query and 1 website query per business.
Zero CRM mutations, zero Google Sheets calls.
"""
import os
import sys
import json
import time

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from lib.discovery.web_search import WebSearchProvider, SearchOutcome
from lib.enrichment.review_rating_enricher import ReviewRatingEnricher
from lib.country_adapters.uk import UKReviewSourceAdapter

def run_smoke_test():
    candidates_file = os.path.join(BASE_DIR, "data", "leeds_100_candidates.json")
    with open(candidates_file, "r", encoding="utf-8") as f:
        candidates = json.load(f)[:5]

    searxng_url = os.getenv("SEARXNG_URL", "http://127.0.0.1:8080")
    print(f"Running Smoke Test against SearXNG: {searxng_url}\n")
    provider = WebSearchProvider(searxng_url=searxng_url)
    enricher = ReviewRatingEnricher(web_search_provider=provider)
    uk_adapter = UKReviewSourceAdapter()

    smoke_results = []

    for idx, c in enumerate(candidates, 1):
        name = c.get("company_name", "")
        city = c.get("city", "Leeds")
        street = c.get("street", "")
        postcode = c.get("postcode", "")

        # 1. Review Query
        review_q = f'"{name}" "{city}" reviews'
        t0 = time.time()
        res_review = provider.search_web(review_q, num_results=5)
        lat_review = time.time() - t0

        # Review evidence extraction via enricher
        enrichment_res = enricher.enrich_business(
            business_name=name,
            city=city,
            street=street,
            postcode=postcode,
            country="United Kingdom",
            search_results=res_review
        )

        # 2. Website Query
        site_q = f'"{name}" "{city}" official site'
        t1 = time.time()
        res_site = provider.search_web(site_q, num_results=5)
        lat_site = time.time() - t1

        # Check for potential official website in results
        website_cand = None
        for r in res_site:
            u = r.get("result_url", "")
            # Filter directories
            if not any(d in u.lower() for d in ["tripadvisor", "facebook", "yell.com", "instagram", "deliveroo", "just-eat", "ubereats"]):
                website_cand = u
                break

        record = {
            "index": idx,
            "business_name": name,
            "city": city,
            "postcode": postcode,
            "review_query": {
                "query": review_q,
                "provider": res_review.provider,
                "outcome": res_review.outcome.value if hasattr(res_review.outcome, "value") else str(res_review.outcome),
                "result_count": len(res_review),
                "is_cached": res_review.is_cached,
                "latency_sec": round(lat_review, 2),
                "review_status": enrichment_res.review_status,
                "rating": enrichment_res.rating,
                "review_count": enrichment_res.review_count,
                "evidence": enrichment_res.review_evidence[:100] if enrichment_res.review_evidence else "",
                "review_provider": enrichment_res.review_provider
            },
            "website_query": {
                "query": site_q,
                "provider": res_site.provider,
                "outcome": res_site.outcome.value if hasattr(res_site.outcome, "value") else str(res_site.outcome),
                "result_count": len(res_site),
                "is_cached": res_site.is_cached,
                "latency_sec": round(lat_site, 2),
                "discovered_website": website_cand or "NONE_FOUND"
            }
        }
        smoke_results.append(record)

        print(f"[{idx}/5] {name} ({city}):")
        print(f"  Review Search : Provider={res_review.provider}, Outcome={res_review.outcome}, Results={len(res_review)}, Latency={lat_review:.2f}s, Cached={res_review.is_cached}")
        print(f"  Review Evidence: Status={enrichment_res.review_status}, Rating={enrichment_res.rating}, Reviews={enrichment_res.review_count}, Provider={enrichment_res.review_provider}")
        print(f"  Website Search: Provider={res_site.provider}, Outcome={res_site.outcome}, Results={len(res_site)}, Latency={lat_site:.2f}s, Discovered Site={website_cand}")
        print("-" * 70)

    # Telemetry report
    print("\nProvider Telemetry:")
    print(json.dumps(provider.get_telemetry(), indent=2))

if __name__ == "__main__":
    run_smoke_test()
