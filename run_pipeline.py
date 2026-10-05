import sys
import argparse
from dotenv import load_dotenv

from lib.pipeline import LeadGenerationPipeline
from lib.types import DiscoveredBusiness

load_dotenv()

def main():
    parser = argparse.ArgumentParser(description="Dripp Media - No Website Lead Engine")
    parser.add_argument("--country", default="United Kingdom", help="Target country (e.g. 'United Kingdom')")
    parser.add_argument("--city", default="Manchester", help="Target city or comma-separated cities (e.g. 'Manchester')")
    parser.add_argument("--industry", default="Restaurants", help="Target industry niche (e.g. 'Restaurants')")
    parser.add_argument("--limit", "--qualified-leads", dest="limit", type=int, default=10, help="Qualified leads needed (default: 10)")
    parser.add_argument("--batch-size", type=int, default=10, help="Batch size for processing (default: 10)")
    parser.add_argument("--multiplier", type=int, default=5, help="Max research multiplier (default: 5)")
    parser.add_argument("--test-mode", action="store_true", default=False, help="Inject test probe candidates (Manchester NH & MI) to verify rejection")
    args = parser.parse_args()

    cities = [c.strip() for c in args.city.split(",") if c.strip()]
    if not cities:
        cities = ["Manchester"]

    inject_candidates = []
    if args.test_mode and "united kingdom" in args.country.lower() and "manchester" in [c.lower() for c in cities]:
        print("[Test Setup] Injecting test probe candidates (Manchester, NH & Manchester, MI) to test Section 24 Rules 1 & 2...")
        nh_probe = DiscoveredBusiness(
            company_name="The Elm Street Cafe NH",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            address="150 Elm Street, Manchester, NH 03101, United States",
            phone="+1 603-555-0199",
            raw_website="",
            google_maps_url="https://maps.google.com/?q=Manchester+NH+Test",
            review_count=120,
            rating=4.3
        )
        mi_probe = DiscoveredBusiness(
            company_name="River Raisin Tavern MI",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            address="102 East Main Street, Manchester, MI 48158, United States",
            phone="+1 734-555-0144",
            raw_website="",
            google_maps_url="https://maps.google.com/?q=Manchester+MI+Test",
            review_count=85,
            rating=4.1
        )
        inject_candidates = [nh_probe, mi_probe]

    pipeline = LeadGenerationPipeline()
    result = pipeline.run(
        country=args.country,
        cities=cities,
        industry=args.industry,
        requested_qualified_leads=args.limit,
        batch_size=args.batch_size,
        max_research_multiplier=args.multiplier,
        inject_test_candidates=inject_candidates
    )

    if result.get("error"):
        print(f"Execution failed: {result['error']}", file=sys.stderr)
        sys.exit(1)

    stats = result.get("stats", {})

    query_stats = result.get("query_stats", {})

    print("\n" + "=" * 50)
    print("QUALIFICATION ENGINE V3 RUN REPORT")
    print(f"PIPELINE RUN ID: {stats.get('pipeline_run_id', 'N/A')}")
    print("STATUS: PIPELINE_STATUS = COMPLETE")
    print("=" * 50)
    print(f"Raw businesses discovered:    {stats.get('raw_discovered', 0)}")
    print(f"Target-country matches:       {stats.get('target_country_matches', 0)}")
    print(f"Wrong-country results:        {stats.get('wrong_country_results', 0)}")
    print(f"Duplicates:                   {stats.get('duplicates', 0)}")
    print(f"Candidates researched:        {stats.get('valid_candidates_researched', 0)}")
    print(f"Website exists:               {stats.get('website_exists', 0)}")
    print(f"No website confirmed:         {stats.get('no_website_confirmed', 0)}")
    print(f"Website unclear:              {stats.get('website_unclear', 0)}")
    print(f"Website broken:               {stats.get('website_broken', 0)}")
    print(f"Outreach-ready:               {stats.get('outreach_ready', 0)}")
    print(f"Manual review:                {stats.get('manual_review', 0)}")
    print(f"Research only:                {stats.get('research_only', 0)}")
    print(f"Excluded:                     {stats.get('excluded', 0)}")
    print(f"High priority:                {stats.get('high_priority', 0)}")
    print(f"Medium priority:              {stats.get('medium_priority', 0)}")
    print(f"Saved to LEADS:               {stats.get('saved_to_leads', 0)}")
    print(f"Saved to REVIEW_QUEUE:        {stats.get('saved_to_review_queue', 0)}")
    print(f"Saved to RESEARCH_LOG:        {stats.get('saved_to_research_log', 0)}")
    print("-" * 50)
    print(f"Requested qualified leads:    {stats.get('requested_qualified_leads', 0)}")
    print(f"Actual outreach-ready leads:  {stats.get('outreach_ready', 0)}")
    print("=" * 50)

    print("\n" + "=" * 80)
    print("DISCOVERY QUERY PERFORMANCE")
    print("=" * 80)
    print(f"{'Discovery Query Used':<48} | {'Candidates':<10} | {'Qualified Leads':<15}")
    print("-" * 80)
    for q_name, q_data in query_stats.items():
        print(f"{q_name:<48} | {q_data['candidates']:<10} | {q_data['qualified']:<15}")
    print("=" * 80 + "\n")

if __name__ == "__main__":
    main()
