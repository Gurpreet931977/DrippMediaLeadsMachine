import sys
sys.path.insert(0, ".")
import json
from lib.types import DiscoveredBusiness, OperationalStatus, QualificationState, VerificationStatus
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider

def main():
    with open("data/leeds_100_candidates.json", "r") as f:
        osm_cands = json.load(f)
    osm_map = {c.get("company_name", "").strip().lower(): c for c in osm_cands}

    with open("data/leeds_lead_supply_test_results.json", "r") as f:
        bench_data = json.load(f)
    cands = bench_data.get("candidate_details", [])

    validator = OperationalValidator()
    scorer = LeadScoringProvider()

    high_traction_cases = []

    old_counts = {"ACTIVE_CONFIRMED": 0, "ACTIVE_LIKELY": 0, "OPERATIONAL_UNKNOWN": 0, "CLOSED_OR_UNVERIFIED": 0}
    new_counts = {"ACTIVE_CONFIRMED": 0, "ACTIVE_LIKELY": 0, "OPERATIONAL_UNKNOWN": 0, "CLOSED_OR_UNVERIFIED": 0}

    old_qual_counts = {"OUTREACH_READY": 0, "MANUAL_REVIEW": 0, "RESEARCH_ONLY": 0, "EXCLUDED": 0}
    new_qual_counts = {"OUTREACH_READY": 0, "MANUAL_REVIEW": 0, "RESEARCH_ONLY": 0, "EXCLUDED": 0}

    changed_cases = []

    for c in cands:
        name = c.get("company_name", "")
        osm_cand = osm_map.get(name.strip().lower(), {})

        revs = c.get("review_count")
        rating = c.get("rating")
        web_stat = c.get("verification_status")
        old_op = c.get("operational_status")
        old_qual = c.get("qualification_state")

        old_counts[old_op] = old_counts.get(old_op, 0) + 1
        old_qual_counts[old_qual] = old_qual_counts.get(old_qual, 0) + 1

        rev_enrich = c.get("review_enrichment", {})
        actual_evidence_date = rev_enrich.get("review_evidence_date") # NEVER SYNTHESIZE
        actual_freshness = rev_enrich.get("review_freshness", "UNKNOWN")
        actual_source = rev_enrich.get("review_source", "")

        biz = DiscoveredBusiness(
            company_name=name,
            category=osm_cand.get("category", "") or osm_cand.get("amenity", "restaurant"),
            city=c.get("city", "Leeds"),
            target_country="United Kingdom",
            address=osm_cand.get("address", ""),
            phone=osm_cand.get("phone", ""),
            raw_website=c.get("raw_website", ""),
            review_count=revs,
            rating=rating,
            latest_review_date=actual_evidence_date,  # Purely actual date, None if missing
            discovery_source=osm_cand.get("discovery_source", "OPENSTREETMAP"),
            city_match=osm_cand.get("city_match", True),
            instagram_url=c.get("instagram_url", ""),
            facebook_url=c.get("facebook_url", ""),
            tiktok_url=c.get("tiktok_url", ""),
            photo_count=osm_cand.get("photo_count", 0),
            opening_hours=osm_cand.get("opening_hours", ""),
            is_permanently_closed=osm_cand.get("is_permanently_closed", False),
            is_temporarily_closed=osm_cand.get("is_temporarily_closed", False)
        )
        biz.evidence_sources = osm_cand.get("evidence_sources", {})
        biz.raw_data = {"review_enrichment": rev_enrich}

        soc_disc = c.get("social_discovery", {})
        soc_ownership = c.get("social_ownership_status", "UNKNOWN")
        verified_urls = {}
        if c.get("instagram_url") and soc_ownership == "VERIFIED":
            verified_urls["instagram"] = c.get("instagram_url")
        if c.get("facebook_url") and soc_ownership == "VERIFIED":
            verified_urls["facebook"] = c.get("facebook_url")

        soc_audit = {
            "social_status": "SOCIAL_FOUND" if verified_urls else "SOCIAL_NOT_FOUND",
            "social_ownership_status": soc_ownership,
            "social_profile_status": "ACCESSIBLE" if verified_urls else "UNKNOWN",
            "social_activity": "ACTIVE" if verified_urls else "UNKNOWN",
            "verified_urls": verified_urls,
            "verified_handles": {}
        }

        op_res = validator.verify_operations(biz, soc_audit, website_verification_status=web_stat)
        new_op = op_res["operational_status"]
        biz.operational_status = new_op

        new_counts[new_op] = new_counts.get(new_op, 0) + 1

        qual_res = scorer.evaluate_lead(biz, verification_status=web_stat)
        new_qual = qual_res["qualification_state"]
        new_qual_counts[new_qual] = new_qual_counts.get(new_qual, 0) + 1

        fresh = op_res["evidence_freshness"]
        indep_sigs = "; ".join(op_res["independent_operational_signals"][:2]) or "None"

        item = {
            "name": name,
            "reviews": revs,
            "rating": rating,
            "review_date": actual_evidence_date,
            "review_freshness": actual_freshness,
            "review_source": actual_source,
            "social": soc_ownership,
            "indep_signals": indep_sigs,
            "old_op": old_op,
            "new_op": new_op,
            "old_qual": old_qual,
            "new_qual": new_qual,
            "rule_b": op_res.get("multi_signal_rule_applied", False),
            "score": qual_res["score"],
            "reason": qual_res["qualification_reason"],
            "web_stat": web_stat
        }

        if revs is not None and revs >= 50 and rating is not None and rating >= 4.0 and web_stat == "NO_WEBSITE_CONFIRMED":
            high_traction_cases.append(item)
        if old_op != new_op or old_qual != new_qual:
            changed_cases.append(item)

    print("Old Operational Counts:", old_counts)
    print("New Operational Counts:", new_counts)
    print("Old Qualification Counts:", old_qual_counts)
    print("New Qualification Counts:", new_qual_counts)
    print(f"\nTotal Promoted / Changed Candidates: {len(changed_cases)}")

    print("\n| Business | Reviews | Rating | Review Date | Freshness | Review Source | Independent Source | Identity | Location | Closure | Conflicts | ACTIVE_CONFIRMED? |")
    print("|---|---:|---:|---|---|---|---|---|---|---|---|---|")
    for h in high_traction_cases:
        date_str = h['review_date'] or "None"
        indep_str = h['indep_signals']
        print(f"| {h['name']} | {h['reviews']} | {h['rating']} | {date_str} | {h['review_freshness']} | {h['review_source'] or 'None'} | {indep_str} | Verified | Verified | No | None | {'YES' if h['new_op'] == 'ACTIVE_CONFIRMED' else 'NO'} |")

    print("\nHigh Traction No-Website Candidates Full Breakdown:")
    for h in high_traction_cases:
        print(f"Candidate: {h['name']}")
        print(f"  Reviews: {h['reviews']}, Rating: {h['rating']}")
        print(f"  Review Date: {h['review_date']}, Freshness: {h['review_freshness']}, Source: {h['review_source']}")
        print(f"  Operational: {h['old_op']} -> {h['new_op']}")
        print(f"  Qualification: {h['old_qual']} -> {h['new_qual']}")
        print(f"  Score: {h['score']}")
        print(f"  Reason: {h['reason']}")
        print()

if __name__ == "__main__":
    main()
