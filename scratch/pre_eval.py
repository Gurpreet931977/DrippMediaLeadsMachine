import json
import time
from lib.discovery.osm import OpenStreetMapProvider
from lib.enrichment.review_rating_enricher import ReviewRatingEnricher
from lib.website.detector import NodeWebsiteDetectionProvider
from lib.verification.no_website_verifier import NoWebsiteVerificationProvider
from lib.discovery.web_search import WebSearchProvider
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.types import WebsiteStatus, VerificationStatus, DiscoveredBusiness

osm = OpenStreetMapProvider()
candidates = osm.search_businesses(city="Manchester", country="United Kingdom", industry="Restaurant", limit=40)
print(f"Total discovered candidates: {len(candidates)}")

detector = NodeWebsiteDetectionProvider()
web = WebSearchProvider()
enricher = ReviewRatingEnricher(web_search_provider=web)

for i, c in enumerate(candidates[:15]):
    print(f"\n[{i+1}] {c.company_name} | {c.category} | addr: {c.address} | coords: {c.latitude},{c.longitude}")
    det = detector.detect_website(c.raw_website or "")
    print(f"  Raw website: {det['website_status']} (clean: {det.get('clean_website')})")
    c_enriched = enricher.enrich_candidate(c)
    rc = c_enriched.review_count
    rat = c_enriched.rating
    rev_meta = c_enriched.raw_data.get("review_enrichment", {})
    fresh = rev_meta.get("review_freshness")
    status = rev_meta.get("review_status")
    print(f"  Reviews: {rc}, Rating: {rat}, Freshness: {fresh}, Status: {status}")
