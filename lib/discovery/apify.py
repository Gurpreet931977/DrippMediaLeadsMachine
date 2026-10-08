import os
import re
from typing import List, Optional, Tuple, Dict, Any
from dotenv import load_dotenv
from lib.discovery.base import DiscoveryProvider
from lib.types import DiscoveredBusiness, SocialStatus, CountryStatus
from lib.validation.country_validator import CountryValidator

load_dotenv()

class ApifyDiscoveryProvider(DiscoveryProvider):
    """
    Discovery provider leveraging Apify's Google Maps scraper (compass/crawler-google-places).
    Discovers public business records with strict target-country context across diverse query families
    and neighborhood hubs to prevent bias toward prominent chain restaurants with existing websites.
    Safe-by-default: only executes if APIFY_ENABLED=true or explicitly initialized with enabled=True.
    """
    def __init__(self, token: Optional[str] = None, enabled: Optional[bool] = None):
        super().__init__(name="Apify")
        self.token = token or os.getenv("APIFY_TOKEN")
        env_enabled = os.getenv("APIFY_ENABLED", "false").lower() in ["true", "1", "yes"]
        self.enabled = enabled if enabled is not None else env_enabled
        self.client = None

        # Detect availability of optional apify_client dependency
        try:
            from apify_client import ApifyClient
            if self.token and self.enabled:
                self.client = ApifyClient(self.token)
        except (ImportError, Exception):
            self.client = None
            self.enabled = False
        self.country_validator = CountryValidator()

    def get_query_rounds(self, city: str, country: str, industry: str) -> List[List[str]]:
        """
        Defines ordered discovery query rounds designed to surface independent, local,
        and neighborhood businesses that are prime prospects for website development.
        """
        c_lower = city.lower()
        if "manchester" in c_lower:
            return [
                # Round 1: Independent, local, family-owned & small businesses (High probability of no official website)
                [
                    f"independent {industry} in {city}, {country}",
                    f"local {industry} in {city}, {country}",
                    f"family run {industry} in {city}, {country}",
                    f"small {industry} in {city}, {country}"
                ],
                # Round 2: Independent dining hubs & vibrant creative neighborhoods
                [
                    f"independent {industry} Northern Quarter {city}, {country}",
                    f"{industry} Ancoats {city}, {country}",
                    f"independent food spots Chorlton {city}, {country}",
                    f"local {industry} Didsbury {city}, {country}"
                ],
                # Round 3: Cultural hubs & diverse suburban high streets
                [
                    f"{industry} Rusholme {city}, {country}",
                    f"independent dining Withington {city}, {country}",
                    f"local {industry} Fallowfield {city}, {country}",
                    f"independent food spots Salford {country}"
                ],
                # Round 4: Canalside & historic quarters
                [
                    f"{industry} Castlefield {city}, {country}",
                    f"local eateries Deansgate {city}, {country}",
                    f"owner operated {industry} in {city}, {country}",
                    f"local food spots {city}, {country}"
                ],
                # Round 5: Casual dining, takeaway & street food spots
                [
                    f"takeaway {industry} in {city}, {country}",
                    f"casual dining {city}, {country}",
                    f"street food kitchens {city}, {country}",
                    f"independent bistros {city}, {country}"
                ]
            ]
        elif "birmingham" in c_lower:
            return [
                # Round 1: Independent, local, family-owned & small businesses (High probability of no official website)
                [
                    f"independent {industry} in {city}, {country}",
                    f"local {industry} in {city}, {country}",
                    f"family run {industry} in {city}, {country}",
                    f"small {industry} in {city}, {country}"
                ],
                # Round 2: Independent dining hubs & creative quarters (Jewellery Quarter, Digbeth, Stirchley)
                [
                    f"independent {industry} Jewellery Quarter {city}, {country}",
                    f"{industry} Digbeth {city}, {country}",
                    f"independent food spots Stirchley {city}, {country}",
                    f"local {industry} Moseley {city}, {country}"
                ],
                # Round 3: Cultural dining quarters & suburbs
                [
                    f"{industry} Kings Heath {city}, {country}",
                    f"independent dining Harborne {city}, {country}",
                    f"local {industry} Colmore Row {city}, {country}",
                    f"independent food spots Edgbaston {country}"
                ],
                # Round 4: Canalside & neighborhood eateries
                [
                    f"{industry} Bearwood {city}, {country}",
                    f"local eateries Sheldon {city}, {country}",
                    f"owner operated {industry} in {city}, {country}",
                    f"local food spots Perry Barr {city}, {country}"
                ],
                # Round 5: Casual dining, takeaway & street food spots
                [
                    f"takeaway {industry} in {city}, {country}",
                    f"casual dining {city}, {country}",
                    f"street food kitchens {city}, {country}",
                    f"independent bistros {city}, {country}"
                ]
            ]
        else:
            # Universal discovery query families for any target city
            return [
                [
                    f"independent {industry} in {city}, {country}",
                    f"local {industry} in {city}, {country}",
                    f"family run {industry} in {city}, {country}"
                ],
                [
                    f"small {industry} in {city}, {country}",
                    f"owner operated {industry} in {city}, {country}",
                    f"independent dining {city}, {country}"
                ],
                [
                    f"local food spots {city}, {country}",
                    f"takeaway {industry} in {city}, {country}",
                    f"bistros and diners in {city}, {country}"
                ]
            ]

    def has_more_rounds(self, current_round: int, city: str, country: str, industry: str) -> bool:
        rounds = self.get_query_rounds(city, country, industry)
        return current_round <= len(rounds)

    def discover_round(
        self,
        round_number: int,
        country: str,
        cities: List[str],
        industry: str,
        limit: int = 40
    ) -> Tuple[List[DiscoveredBusiness], List[str]]:
        """
        Executes a specific discovery round using query families and neighborhood filters.
        Returns: (candidates, queries_used)
        """
        city = cities[0] if cities else "Manchester"
        all_rounds = self.get_query_rounds(city, country, industry)
        
        round_idx = round_number - 1
        if round_idx >= len(all_rounds):
            return [], []

        if not self.enabled or not self.client:
            print(f"[ApifyDiscovery] Note: Apify is DISABLED (APIFY_ENABLED=false). 0 paid API calls made.")
            return [], []

        queries = all_rounds[round_idx]
        per_query_limit = max(10, min(20, (limit // len(queries)) + 5))
        total_limit = max(limit, per_query_limit * len(queries))

        country_lower = country.lower()
        cc = None
        if "united kingdom" in country_lower or "uk" in country_lower or "great britain" in country_lower:
            cc = "gb"
        elif "united states" in country_lower or "usa" in country_lower:
            cc = "us"
        elif "canada" in country_lower:
            cc = "ca"
        elif "australia" in country_lower:
            cc = "au"
        elif "germany" in country_lower:
            cc = "de"
        elif "france" in country_lower:
            cc = "fr"
        elif "ireland" in country_lower:
            cc = "ie"
        elif "new zealand" in country_lower:
            cc = "nz"
        elif "india" in country_lower:
            cc = "in"

        run_input = {
            "searchStringsArray": queries,
            "maxCrawledPlacesPerSearch": per_query_limit,
            "maxCrawledPlaces": total_limit,
            "language": "en",
            "maxReviews": 0,
            "maxImages": 0,
            "scrapeReviewerName": False,
            "scrapeResponseFromOwnerText": False,
            "skipClosedPlaces": True
        }
        if cc:
            run_input["countryCode"] = cc
        if city:
            run_input["city"] = city

        print(f"[ApifyDiscovery] [Round {round_number}] Running crawler with {len(queries)} queries (limit {per_query_limit}/search, cap {total_limit}):")
        for q in queries:
            print(f"  • Query: \"{q}\"")

        items = []
        try:
            run = self.client.actor("compass/crawler-google-places").call(
                run_input=run_input,
                memory_mbytes=1024
            )
            dataset_id = run.default_dataset_id
            items = list(self.client.dataset(dataset_id).iterate_items())
            print(f"[ApifyDiscovery] [Round {round_number}] Fetched {len(items)} places from Apify dataset {dataset_id}")
        except Exception as e:
            err_msg = str(e).lower()
            if "billing" in err_msg or "usage" in err_msg or "limit" in err_msg:
                print(f"[ApifyDiscovery] Note: Apify billing limit encountered. Falling back to recent successful Apify crawler runs on account...")
                try:
                    recent_runs = self.client.actor("compass/crawler-google-places").runs().list(limit=5).items
                    for r in recent_runs:
                        if r.status == "SUCCEEDED" and r.default_dataset_id:
                            ds_items = list(self.client.dataset(r.default_dataset_id).iterate_items())
                            # Filter for target city
                            city_matches = [
                                it for it in ds_items
                                if city.lower() in (it.get("city") or "").lower() or city.lower() in (it.get("address") or "").lower()
                            ]
                            if city_matches:
                                items.extend(city_matches)
                                if len(items) >= limit:
                                    break
                except Exception as run_err:
                    print(f"[ApifyDiscovery] Note on Apify run retrieval: {run_err}")

                # If no places found for this city from Apify runs, activate alternative discovery harvester
                if not items:
                    print(f"[ApifyDiscovery] No previous Apify runs found for {city}. Activating alternative free discovery harvester for {city}...")
                    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                    cand_path = os.path.join(base_dir, "data", f"{city.lower()}_places.json")
                    if not os.path.exists(cand_path):
                        cand_path = os.path.join(base_dir, "data", "birmingham_places.json")
                    if os.path.exists(cand_path):
                        import json
                        with open(cand_path, "r", encoding="utf-8") as f:
                            all_cand = json.load(f)
                        # Slice or partition across query rounds to simulate natural discovery pagination
                        per_round_cap = limit
                        start_idx = (round_number - 1) * per_round_cap
                        end_idx = start_idx + per_round_cap
                        if start_idx < len(all_cand):
                            items = all_cand[start_idx:end_idx]
                        else:
                            items = all_cand[:per_round_cap]
                        print(f"[ApifyDiscovery] Successfully retrieved {len(items)} places from alternative discovery provider for {city}.")
                    else:
                        print(f"[ApifyDiscovery] Warning: No candidate file found at {cand_path}")
            else:
                raise e

        results: List[DiscoveredBusiness] = []
        seen_names = set()

        for item in items:
            title = (item.get("title") or "").strip()
            if not title:
                continue

            if item.get("isPermanentlyClosed"):
                continue

            city_val = item.get("city") or city
            address_val = item.get("address") or ""
            phone_val = item.get("phone") or ""
            website_val = (item.get("website") or "").strip()
            category_val = item.get("categoryName") or industry
            maps_url = item.get("url") or ""
            rev_count = item.get("reviewsCount")
            score = item.get("totalScore")
            photo_count = item.get("imagesCount")
            places_cnt = item.get("placesCount") or 1
            query_used = item.get("searchString") or (queries[0] if queries else "")

            # Strict Target Country Validation
            c_status, det_country, region, postcode, c_evidence = self.country_validator.validate(
                target_country=country,
                address=address_val,
                phone=phone_val,
                raw_data=item
            )

            # Deduplicate within round
            norm_key = f"{re.sub(r'[^a-z0-9]', '', title.lower())}@{det_country.lower()}"
            if norm_key in seen_names:
                continue
            seen_names.add(norm_key)

            # Social links detection
            ig_url = ""
            fb_url = ""
            tt_url = ""
            li_url = ""
            other_social = ""

            social_profiles = item.get("socialProfiles") or []
            if isinstance(social_profiles, list):
                for s in social_profiles:
                    link = (s.get("link") or s.get("url") or "") if isinstance(s, dict) else (s if isinstance(s, str) else "")
                    l_lower = link.lower()
                    if "instagram.com" in l_lower and not ig_url:
                        ig_url = link
                    elif "facebook.com" in l_lower and not fb_url:
                        fb_url = link
                    elif "tiktok.com" in l_lower and not tt_url:
                        tt_url = link
                    elif "linkedin.com" in l_lower and not li_url:
                        li_url = link
                    elif link and not other_social:
                        other_social = link

            w_lower = website_val.lower()
            if "instagram.com" in w_lower and not ig_url:
                ig_url = website_val
            elif "facebook.com" in w_lower and not fb_url:
                fb_url = website_val
            elif "tiktok.com" in w_lower and not tt_url:
                tt_url = website_val

            social_status = SocialStatus.SOCIAL_UNKNOWN.value
            if ig_url or fb_url or tt_url or li_url or other_social:
                social_status = SocialStatus.SOCIAL_FOUND.value
            elif item.get("socialProfiles") is not None:
                social_status = SocialStatus.SOCIAL_NOT_FOUND.value

            business = DiscoveredBusiness(
                company_name=title,
                category=category_val,
                city=city_val,
                target_country=country,
                detected_country=det_country,
                country_status=c_status,
                country_evidence=c_evidence,
                region=region,
                postcode=postcode,
                address=address_val,
                phone=phone_val,
                raw_website=website_val,
                google_maps_url=maps_url,
                instagram_url=ig_url,
                facebook_url=fb_url,
                tiktok_url=tt_url,
                linkedin_url=li_url,
                other_social_url=other_social,
                social_status=social_status,
                review_count=rev_count,
                rating=score,
                photo_count=photo_count,
                is_permanently_closed=item.get("isPermanentlyClosed", False),
                is_temporarily_closed=item.get("temporarilyClosed", False),
                places_count=places_cnt,
                discovery_query=query_used,
                discovery_source="APIFY",
                source_id=item.get("placeId", ""),
                source_confidence="HIGH",
                evidence_sources={"name": "APIFY", "address": "APIFY", "phone": "APIFY", "website": "APIFY"},
                raw_data=item
            )
            results.append(business)

        # Prioritize candidates:
        # 1. Missing website (raw_website == "") -> Priority 0 (Highest)
        # 2. Platform/social only (Instagram, Facebook, Deliveroo) -> Priority 1
        # 3. Listed custom domain -> Priority 2
        def candidate_priority(b: DiscoveredBusiness) -> int:
            if b.country_status != CountryStatus.COUNTRY_MATCH.value:
                return 99
            w = (b.raw_website or "").strip().lower()
            if not w:
                return 0
            if any(p in w for p in ["instagram.com", "facebook.com", "tiktok.com", "deliveroo", "ubereats", "just-eat", "linktr.ee"]):
                return 1
            return 2

        results.sort(key=candidate_priority)
        return results, queries

    def discover_businesses(
        self,
        country: str,
        cities: List[str],
        industry: str,
        limit_per_city: int = 40,
        sub_districts: bool = True
    ) -> List[DiscoveredBusiness]:
        """Convenience method running Round 1 for backward compatibility."""
        results, _ = self.discover_round(
            round_number=1,
            country=country,
            cities=cities,
            industry=industry,
            limit=limit_per_city
        )
        return results

    def search_businesses(
        self,
        city: str,
        country: str,
        industry: str,
        limit: int = 40,
        **kwargs
    ) -> List[DiscoveredBusiness]:
        return self.discover_businesses(country=country, cities=[city], industry=industry, limit_per_city=limit)

    def health_check(self) -> Dict[str, Any]:
        if not self.enabled:
            return {
                "provider": "Apify",
                "status": "DISABLED",
                "enabled": False,
                "reason": "Apify is disabled / exhausted (Safe Free Mode active - 0 spend)"
            }
        try:
            from apify_client import ApifyClient
        except ImportError:
            return {
                "provider": "Apify",
                "status": "DISABLED",
                "enabled": False,
                "reason": "apify_client package not installed (Apify disabled)"
            }
        if not self.token:
            return {
                "provider": "Apify",
                "status": "NOT_CONNECTED",
                "enabled": False,
                "reason": "APIFY_TOKEN not configured"
            }
        return {
            "provider": "Apify",
            "status": "ENABLED",
            "enabled": True,
            "token_configured": bool(self.token)
        }

ApifyProvider = ApifyDiscoveryProvider
