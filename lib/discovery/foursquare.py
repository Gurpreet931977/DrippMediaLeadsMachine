"""
Foursquare Places API Discovery Provider (Optional Secondary Source)
Queries Foursquare Places API v3 when FOURSQUARE_API_KEY is configured.
If FOURSQUARE_API_KEY is missing or invalid:
- Gracefully skips without error.
- Health check marks 'NOT_CONNECTED'.
- Never halts the discovery pipeline.
"""
import os
import re
import json
import time
import hashlib
from typing import List, Dict, Any, Optional
import requests

from lib.discovery.base import DiscoveryProvider
from lib.types import DiscoveredBusiness, SocialStatus
from lib.validation.country_validator import CountryValidator

FOURSQUARE_SEARCH_URL = "https://api.foursquare.com/v3/places/search"

class FoursquareProvider(DiscoveryProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_dir: Optional[str] = None,
        cache_ttl_seconds: int = 86400
    ):
        super().__init__(name="Foursquare")
        self.api_key = api_key or os.getenv("FOURSQUARE_API_KEY") or ""
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = cache_dir or os.path.join(self.base_dir, "data", "cache_foursquare")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_ttl = cache_ttl_seconds
        self.country_validator = CountryValidator()

    def _get_cache(self, key: str) -> Optional[Any]:
        cache_file = os.path.join(self.cache_dir, f"{hashlib.md5(key.encode()).hexdigest()}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                if time.time() - entry.get("timestamp", 0) < self.cache_ttl:
                    return entry.get("data")
            except Exception:
                pass
        return None

    def _set_cache(self, key: str, data: Any):
        cache_file = os.path.join(self.cache_dir, f"{hashlib.md5(key.encode()).hexdigest()}.json")
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"timestamp": time.time(), "key": key, "data": data}, f)
        except Exception:
            pass

    def health_check(self) -> Dict[str, Any]:
        """Checks Foursquare credentials and endpoint readiness with differentiated statuses."""
        if not self.api_key:
            return {
                "provider": "Foursquare",
                "status": "NOT_CONFIGURED",
                "connected": False,
                "reason": "FOURSQUARE_API_KEY is not configured"
            }
        
        headers = {
            "Accept": "application/json",
            "Authorization": self.api_key if self.api_key.startswith("Bearer ") else self.api_key,
            "Accept-Language": "en"
        }
        try:
            r = requests.get(FOURSQUARE_SEARCH_URL, params={"near": "London, UK", "limit": 1}, headers=headers, timeout=5)
            if r.status_code == 200:
                return {
                    "provider": "Foursquare",
                    "status": "CONNECTED",
                    "connected": True,
                    "endpoint": FOURSQUARE_SEARCH_URL
                }
            elif r.status_code in [401, 403]:
                return {
                    "provider": "Foursquare",
                    "status": "AUTH_ERROR",
                    "connected": False,
                    "reason": f"HTTP {r.status_code}: Invalid or unauthorized API key"
                }
            elif r.status_code == 429:
                return {
                    "provider": "Foursquare",
                    "status": "RATE_LIMITED",
                    "connected": False,
                    "reason": "HTTP 429: Quota or rate limit exceeded"
                }
            elif r.status_code >= 500:
                return {
                    "provider": "Foursquare",
                    "status": "DEGRADED",
                    "connected": False,
                    "reason": f"HTTP {r.status_code}: Service temporarily unavailable"
                }
            return {
                "provider": "Foursquare",
                "status": "FAILED",
                "connected": False,
                "reason": f"HTTP {r.status_code}: {r.text[:100]}"
            }
        except requests.exceptions.Timeout:
            return {
                "provider": "Foursquare",
                "status": "TIMEOUT",
                "connected": False,
                "reason": "Health check timed out after 5.0s"
            }
        except Exception as ex:
            return {
                "provider": "Foursquare",
                "status": "FAILED",
                "connected": False,
                "reason": str(ex)
            }

    def search_businesses(
        self,
        city: str,
        country: str,
        industry: str,
        limit: int = 40,
        **kwargs
    ) -> List[DiscoveredBusiness]:
        """
        Discovers candidate places from Foursquare Places API.
        Gracefully skips if no API key is provided.
        """
        if not self.api_key:
            return []

        cache_key = f"fsq_{city.lower()}_{country.lower()}_{industry.lower()}_{limit}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return [DiscoveredBusiness(**item) if isinstance(item, dict) else item for item in cached]

        headers = {
            "Accept": "application/json",
            "Authorization": self.api_key if self.api_key.startswith("Bearer ") else self.api_key,
            "Accept-Language": "en"
        }
        params = {
            "near": f"{city}, {country}",
            "query": industry,
            "limit": min(limit, 50),
            # Standard free fields
            "fields": "fsq_id,name,location,categories,tel,website,social_media,hours,geocodes,rating,stats"
        }

        start_t = time.time()
        try:
            r = requests.get(FOURSQUARE_SEARCH_URL, params=params, headers=headers, timeout=12)
            latency = time.time() - start_t
            if r.status_code != 200:
                self.record_call(latency=latency, error=True)
                print(f"[Foursquare] Error HTTP {r.status_code}: {r.text[:120]}")
                return []

            data = r.json()
            results = data.get("results", [])
            self.record_call(latency=latency, candidates_count=len(results))

            candidates: List[DiscoveredBusiness] = []
            seen_names = set()

            for item in results:
                name = (item.get("name") or "").strip()
                if not name:
                    continue

                fsq_id = item.get("fsq_id") or ""
                loc = item.get("location") or {}
                address = loc.get("formatted_address") or loc.get("address") or ""
                city_val = loc.get("locality") or city
                postcode = loc.get("postcode") or ""
                region = loc.get("region") or ""

                phone = item.get("tel") or ""
                website = (item.get("website") or "").strip()

                social_media = item.get("social_media") or {}
                ig_handle = social_media.get("instagram") or ""
                fb_handle = social_media.get("facebook_id") or ""
                ig_url = f"https://instagram.com/{ig_handle}" if ig_handle else ""
                fb_url = f"https://facebook.com/{fb_handle}" if fb_handle else ""

                geocodes = item.get("geocodes", {}).get("main", {})
                lat = geocodes.get("latitude")
                lon = geocodes.get("longitude")

                cats = item.get("categories", [])
                category_name = cats[0].get("name") if cats else industry

                # Foursquare rating is on a 0-10 scale; normalize to 0-5 scale
                raw_rating = item.get("rating")
                rating = round(float(raw_rating) / 2.0, 1) if raw_rating is not None else None
                stats = item.get("stats") or {}
                review_count = stats.get("ratings_total") or stats.get("total_ratings") or None

                norm_key = f"{re.sub(r'[^a-z0-9]', '', name.lower())}@{city.lower()}"
                if norm_key in seen_names:
                    continue
                seen_names.add(norm_key)

                # Target country check
                c_status, det_country, reg, pcode, c_evidence = self.country_validator.validate(
                    target_country=country,
                    address=address,
                    phone=phone,
                    raw_data=item
                )

                candidate = DiscoveredBusiness(
                    company_name=name,
                    category=category_name,
                    city=city_val,
                    target_country=country,
                    detected_country=det_country,
                    country_status=c_status,
                    country_evidence=c_evidence,
                    region=region or reg,
                    postcode=postcode or pcode,
                    address=address,
                    phone=phone,
                    raw_website=website,
                    rating=rating,
                    review_count=review_count,
                    google_maps_url=f"https://foursquare.com/v/{fsq_id}" if fsq_id else "",
                    instagram_url=ig_url,
                    facebook_url=fb_url,
                    social_status=SocialStatus.SOCIAL_FOUND.value if (ig_url or fb_url) else SocialStatus.SOCIAL_UNKNOWN.value,
                    discovery_query=f"Foursquare {industry} in {city}, {country}",
                    discovery_source="FOURSQUARE",
                    source_id=fsq_id,
                    street=loc.get("address", ""),
                    lat=lat,
                    lon=lon,
                    source_confidence="HIGH",
                    evidence_sources={
                        "name": "FOURSQUARE",
                        "address": "FOURSQUARE" if address else "UNKNOWN",
                        "phone": "FOURSQUARE" if phone else "UNKNOWN",
                        "website": "FOURSQUARE" if website else "UNKNOWN"
                    },
                    raw_data=item
                )
                candidates.append(candidate)

            self.metrics.candidates_unique = len(candidates)
            # Store in cache as dicts
            self._set_cache(cache_key, [c.__dict__ for c in candidates])
            return candidates
        except Exception as ex:
            self.record_call(latency=0.0, error=True)
            print(f"[Foursquare] Search exception: {ex}")
            return []
