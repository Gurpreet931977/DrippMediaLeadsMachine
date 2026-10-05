import re
import socket
import urllib.parse
from typing import Dict, Any, List, Optional, Tuple
import requests
from dotenv import load_dotenv
from apify_client import ApifyClient

from lib.types import DiscoveredBusiness, WebsiteStatus, VerificationStatus
from lib.website.detector import NodeWebsiteDetectionProvider, PLATFORM_DOMAINS

load_dotenv()

class NoWebsiteVerificationProvider:
    """
    Multi-stage verification provider for confirming 'NO_WEBSITE_CONFIRMED'.
    Executes:
      Check A: Initial discovery & reachability
      Check B: Web search via Apify (batched or single)
      Check C: Social profile & bio link analysis
      Check D: Direct domain name resolution
    """
    def __init__(self, apify_client: Optional[ApifyClient] = None, enable_search: bool = True):
        import os
        token = os.getenv("APIFY_TOKEN")
        self.apify_client = (apify_client or (ApifyClient(token) if token else None)) if enable_search else None
        self.detector = NodeWebsiteDetectionProvider()
        self._search_disabled = not enable_search

    def _normalize_name(self, name: str) -> str:
        clean = re.sub(r'[^a-zA-Z0-9\s]', '', name).lower()
        return "-".join(clean.split())

    def _is_matching_domain(self, domain: str, business_name: str, city: str = "") -> bool:
        """
        Precise check to determine if a discovered web domain is an official domain for the business.
        Prevents false positive matches on generic words like 'bar', 'tap', 'grill', 'cafe'.
        """
        d = domain.lower()
        # Strip common tlds
        d_core = re.sub(r'\.(co\.uk|com|org|net|uk|co|io|biz|site|info)$', '', d)
        d_core = re.sub(r'^(www\.)', '', d_core)
        d_clean = re.sub(r'[^a-z0-9]', '', d_core)
        
        b_clean = re.sub(r'[^a-z0-9]', '', business_name.lower())
        c_clean = re.sub(r'[^a-z0-9]', '', city.lower())
        
        # 1. Exact clean match: e.g. "dishoom.com" -> dishoom == dishoom
        if d_clean == b_clean:
            return True
            
        # 2. Exact match with city or city aliases suffix: e.g. "tattumanchester.com" or "fenixmcr.co.uk"
        if c_clean and d_clean == f"{b_clean}{c_clean}":
            return True
            
        from lib.discovery.geo_provider import GeoProvider
        city_aliases = GeoProvider.get_locality_aliases(city, "") if city else []
        city_slugs = [re.sub(r'[^a-z0-9]', '', a.lower()) for a in city_aliases if len(a) >= 2]
        for slug in city_slugs:
            if d_clean == f"{b_clean}{slug}":
                return True
        if d_clean in [f"{b_clean}uk", f"{b_clean}ltd"]:
            return True

        # 3. Domain starts/ends with business name if business name is sufficiently distinctive (>= 6 chars)
        if len(b_clean) >= 6:
            valid_qualifiers = ["restaurant", "restaurants", "cafe", "bar", "kitchen", "dining", "grill", "takeaway", "pub"] + city_slugs
            for q in valid_qualifiers:
                if d_clean == f"{b_clean}{q}" or d_clean == f"{q}{b_clean}":
                    return True
                if d_clean == f"the{b_clean}":
                    return True

        return False

    def batch_web_search(self, queries: List[str]) -> Dict[str, List[Dict[str, str]]]:
        """
        Executes a single batched run of apify/google-search-scraper for multiple queries.
        Returns a dict mapping query -> list of organic results {title, url, description}.
        """
        from lib.discovery.web_search import SearchOutcome, SearchResultList

        if not queries:
            return {}

        if self._search_disabled:
            return {
                q: SearchResultList(
                    outcome=SearchOutcome.SEARCH_BLOCKED,
                    provider="APIFY",
                    query=q,
                    error="Apify usage or billing limit reached"
                )
                for q in queries
            }

        if not self.apify_client:
            return {}

        results_map: Dict[str, Any] = {q: [] for q in queries}
        
        # Apify google-search-scraper accepts queries separated by newlines
        combined_queries = "\n".join(queries)
        run_input = {
            "queries": combined_queries,
            "maxPagesPerQuery": 1,
            "resultsPerPage": 6,
            "mobileResults": False
        }

        try:
            print(f"[NoWebsiteVerifier] Batch searching Apify Google Search for {len(queries)} candidates...")
            run = self.apify_client.actor("apify/google-search-scraper").call(
                run_input=run_input,
                memory_mbytes=1024
            )
            dataset_id = run.default_dataset_id
            items = list(self.apify_client.dataset(dataset_id).iterate_items())
            
            for item in items:
                search_query = item.get("searchQuery", {}).get("term") or ""
                org_results = item.get("organicResults", [])
                
                # Match to queries
                matched_q = None
                for q in queries:
                    if q.lower() == search_query.lower() or search_query.lower() in q.lower():
                        matched_q = q
                        break
                if not matched_q and queries:
                    matched_q = queries[0]

                if matched_q:
                    raw_items = [
                        {
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "description": r.get("description", "")
                        }
                        for r in org_results
                    ]
                    results_map[matched_q] = SearchResultList(
                        raw_items,
                        outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS if raw_items else SearchOutcome.SEARCH_SUCCEEDED_EMPTY,
                        provider="APIFY",
                        query=matched_q
                    )
            # Ensure any unmatched queries get SearchResultList
            for q in queries:
                if not isinstance(results_map[q], SearchResultList):
                    raw = results_map[q]
                    results_map[q] = SearchResultList(
                        raw,
                        outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS if raw else SearchOutcome.SEARCH_SUCCEEDED_EMPTY,
                        provider="APIFY",
                        query=q
                    )
        except Exception as e:
            err_msg = str(e).lower()
            if "billing" in err_msg or "usage" in err_msg or "limit" in err_msg:
                self._search_disabled = True
                print(f"[NoWebsiteVerifier] Batch search temporarily disabled due to Apify usage limit: {e}")
                for q in queries:
                    results_map[q] = SearchResultList(
                        outcome=SearchOutcome.SEARCH_BLOCKED,
                        provider="APIFY",
                        query=q,
                        error=f"Apify limit: {e}"
                    )
            else:
                print(f"[NoWebsiteVerifier] Batch search error: {e}")
                for q in queries:
                    results_map[q] = SearchResultList(
                        outcome=SearchOutcome.SEARCH_FAILED,
                        provider="APIFY",
                        query=q,
                        error=f"Apify error: {e}"
                    )

        return results_map

    def check_direct_domains(self, business_name: str, city: str, country: str) -> Optional[str]:
        """
        Check D: Fast DNS probe for obvious domain candidates.
        """
        clean_name = re.sub(r'[^a-z0-9]', '', business_name.lower())
        if len(clean_name) < 4:
            return None

        clean_city = re.sub(r'[^a-z0-9]', '', city.lower())
        candidates = [
            f"{clean_name}.co.uk",
            f"{clean_name}.com",
            f"{clean_name}{clean_city}.co.uk",
            f"{clean_name}-{clean_city}.co.uk"
        ]

        for cand in candidates:
            try:
                ip = socket.gethostbyname(cand)
                if ip:
                    reachable, reason, _ = self.detector.check_reachability(cand, timeout=3)
                    if reachable:
                        return f"https://{cand}"
            except Exception:
                continue

        return None

    def verify_business(
        self,
        business: DiscoveredBusiness,
        cached_search_results: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Full 4-step verification for a single business.
        """
        evidence = {
            "check_a": {},
            "check_b": {},
            "check_c": {},
            "check_d": {}
        }

        # CHECK A: Initial Discovery Website Check
        det_result = self.detector.detect_website(business.raw_website)
        evidence["check_a"] = det_result

        if det_result["website_status"] == WebsiteStatus.WEBSITE_EXISTS.value:
            return {
                "website_status": WebsiteStatus.WEBSITE_EXISTS.value,
                "verification_status": VerificationStatus.WEBSITE_EXISTS.value,
                "verification_reason": det_result["reason"],
                "verified_website": det_result["clean_website"],
                "evidence": evidence
            }
        
        if det_result["website_status"] == WebsiteStatus.WEBSITE_BROKEN.value:
            return {
                "website_status": WebsiteStatus.WEBSITE_BROKEN.value,
                "verification_status": VerificationStatus.WEBSITE_BROKEN.value,
                "verification_reason": det_result["reason"],
                "verified_website": det_result["clean_website"],
                "evidence": evidence
            }

        # If platform URL was in the website field, preserve it
        if det_result.get("is_platform"):
            p_url = det_result.get("extracted_platform_url")
            if "instagram.com" in p_url and not business.instagram_url:
                business.instagram_url = p_url
            elif "facebook.com" in p_url and not business.facebook_url:
                business.facebook_url = p_url

        # CHECK B: Public Search Check
        search_query = f"{business.company_name} {business.city}"
        org_results = cached_search_results
        if org_results is None and self.apify_client:
            b_map = self.batch_web_search([search_query])
            org_results = b_map.get(search_query)

        if org_results is None:
            org_results = []

        evidence["check_b"]["search_query"] = search_query
        evidence["check_b"]["results_count"] = len(org_results)
        evidence["check_b"]["top_results"] = [r.get("url") for r in org_results[:5]]

        found_official_domain = None
        social_found = []

        for r in org_results:
            u = r.get("url", "")
            parsed = urllib.parse.urlparse(u)
            domain = parsed.netloc.lower()
            
            is_plat, p_type = self.detector.is_platform_url(u)
            if is_plat:
                social_found.append(u)
                if "instagram.com" in u and not business.instagram_url:
                    business.instagram_url = u
                elif "facebook.com" in u and not business.facebook_url:
                    business.facebook_url = u
                elif "tiktok.com" in u and not business.tiktok_url:
                    business.tiktok_url = u
                continue
            
            # Check if domain matches business name
            if self._is_matching_domain(domain, business.company_name, business.city):
                # Verify domain reachability
                reachable, reach_reason, _ = self.detector.check_reachability(u, timeout=4)
                if reachable:
                    found_official_domain = u
                    break
                else:
                    return {
                        "website_status": WebsiteStatus.WEBSITE_BROKEN.value,
                        "verification_status": VerificationStatus.WEBSITE_BROKEN.value,
                        "verification_reason": f"Official domain found via search but unreachable/broken: {u} ({reach_reason})",
                        "verified_website": u,
                        "evidence": evidence
                    }

        # CHECK C: Social Profiles Link Check
        evidence["check_c"]["discovered_socials"] = social_found
        if found_official_domain:
            return {
                "website_status": WebsiteStatus.WEBSITE_EXISTS.value,
                "verification_status": VerificationStatus.WEBSITE_EXISTS.value,
                "verification_reason": f"Official business domain verified via Google Search check ({found_official_domain}).",
                "verified_website": found_official_domain,
                "evidence": evidence
            }

        # CHECK B OUTCOME VERIFICATION:
        # If search failed (SEARCH_FAILED, BLOCKED, CIRCUIT_OPEN, PROVIDER_UNAVAILABLE, TIMEOUT),
        # this failure MUST NOT be interpreted as evidence that no website exists.
        outcome_val = getattr(org_results, "outcome", None)
        outcome_name = outcome_val.value if hasattr(outcome_val, "value") else str(outcome_val or "")
        search_failed_states = {
            "SEARCH_FAILED",
            "SEARCH_BLOCKED",
            "SEARCH_CIRCUIT_OPEN",
            "SEARCH_PROVIDER_UNAVAILABLE",
            "SEARCH_TIMEOUT"
        }
        if outcome_name in search_failed_states:
            err_reason = getattr(org_results, "error", "") or outcome_name
            evidence["check_b"]["search_outcome"] = outcome_name
            evidence["check_b"]["search_error"] = err_reason
            return {
                "website_status": WebsiteStatus.WEBSITE_UNCLEAR.value,
                "verification_status": VerificationStatus.WEBSITE_UNCLEAR.value,
                "verification_reason": f"Web search provider failure ({outcome_name}: {err_reason}). Cannot confirm absence of website due to search outage or failure.",
                "verified_website": "",
                "evidence": evidence
            }

        # DECISION LOGIC:
        # If we reached here:
        # - Google Maps has no official custom website (or only social/platform)
        # - Search query succeeded with zero matching official domains (SEARCH_SUCCEEDED_EMPTY or no official domain in results)
        # - Social profiles yielded no external official domain link
        social_summary = []
        if business.instagram_url:
            social_summary.append("Instagram")
        if business.facebook_url:
            social_summary.append("Facebook")
        if business.tiktok_url:
            social_summary.append("TikTok")
        
        platforms_str = ", ".join(social_summary) if social_summary else "public profiles"
        reason = (
            f"No identifiable official business domain was found in Google Places profile, "
            f"linked social accounts ({platforms_str}), or public search results."
        )
        return {
            "website_status": WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
            "verification_status": VerificationStatus.NO_WEBSITE_CONFIRMED.value,
            "verification_reason": reason,
            "verified_website": "",
            "evidence": evidence
        }
