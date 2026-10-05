"""
OpenStreetMap / Overpass Discovery Provider
Discovers local businesses broad and free using OpenStreetMap Overpass API and Nominatim geocoding.
Implements:
- Local disk caching with TTL
- Rate limiting (min 1.2s delay between calls)
- Multi-endpoint failover with exponential backoff
- Broad hospitality amenity tag queries
- Complete tag extraction (name, official_name, brand, cuisine, street, postcode, phone, website, social, lat, lon)
- Strict discovery_source = "OPENSTREETMAP"
"""
import os
import re
import json
import time
import hashlib
from typing import List, Dict, Any, Optional, Tuple
import requests

from lib.discovery.base import DiscoveryProvider
from lib.types import DiscoveredBusiness, SocialStatus, CountryStatus
from lib.validation.country_validator import CountryValidator
from lib.discovery.boundary_validator import CityBoundaryValidator
from lib.discovery.address_normalizer import AddressNormalizer, AddressCompleteness, extract_uk_postcode

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter"
]

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

USER_AGENT = "DrippMediaLeadEngine/3.0 (contact: mediadripp@gmail.com; lead-research)"

class OpenStreetMapProvider(DiscoveryProvider):
    def __init__(
        self,
        cache_dir: Optional[str] = None,
        cache_ttl_seconds: int = 86400, # 24 hours
        request_timeout: int = 30,
        min_request_interval: float = 1.2
    ):
        super().__init__(name="OpenStreetMap")
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = cache_dir or os.path.join(self.base_dir, "data", "cache_osm")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_ttl = cache_ttl_seconds
        self.timeout = request_timeout
        self.min_interval = min_request_interval
        self._last_request_time: float = 0.0
        self.country_validator = CountryValidator()
        self.boundary_validator = CityBoundaryValidator()
        self.adjacent_research_candidates: List[DiscoveredBusiness] = []

    def _wait_rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_time = time.time()

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
        """Checks Overpass API connectivity."""
        headers = {"User-Agent": USER_AGENT}
        try:
            r = requests.get("https://overpass-api.de/api/status", headers=headers, timeout=10)
            if r.status_code == 200 and "Rate limit" in r.text:
                return {
                    "provider": "OpenStreetMap",
                    "status": "CONNECTED",
                    "endpoint": "overpass-api.de",
                    "details": r.text.strip().split("\n")[:3]
                }
            return {
                "provider": "OpenStreetMap",
                "status": "CONNECTED",
                "endpoint": "overpass-api.de",
                "http_status": r.status_code
            }
        except Exception as ex:
            return {
                "provider": "OpenStreetMap",
                "status": "NOT_CONNECTED",
                "error": str(ex)
            }

    def get_city_bounding_box(self, city: str, country: str) -> Optional[Tuple[str, str, str, str]]:
        """
        Geocodes city to a bounding box [south, north, west, east].
        Cached to avoid repeated Nominatim queries.
        """
        cache_key = f"nominatim_{city.lower()}_{country.lower()}"
        cached = self._get_cache(cache_key)
        if cached:
            return tuple(cached)

        self._wait_rate_limit()
        headers = {"User-Agent": USER_AGENT}
        params = {
            "city": city,
            "country": country,
            "format": "json",
            "limit": 1
        }
        try:
            r = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=12)
            if r.status_code == 200:
                items = r.json()
                if items:
                    bb = items[0].get("boundingbox", [])
                    if len(bb) == 4:
                        # bb is [min_lat, max_lat, min_lon, max_lon] -> (south, north, west, east)
                        res = (str(bb[0]), str(bb[1]), str(bb[2]), str(bb[3]))
                        self._set_cache(cache_key, list(res))
                        return res
        except Exception as e:
            print(f"[OSM] Nominatim geocoding error for {city}, {country}: {e}")
        return None

    def execute_overpass_query(self, query: str) -> Dict[str, Any]:
        """
        Executes Overpass QL with local caching, rate limiting, and multi-endpoint failover.
        """
        cached = self._get_cache(query)
        if cached is not None:
            return cached

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json"
        }
        last_error = None

        for endpoint in OVERPASS_ENDPOINTS:
            for attempt in range(3):
                self._wait_rate_limit()
                try:
                    start_t = time.time()
                    resp = requests.post(
                        endpoint,
                        data={"data": query},
                        headers=headers,
                        timeout=self.timeout
                    )
                    latency = time.time() - start_t

                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                            self._set_cache(query, data)
                            self.record_call(latency=latency, candidates_count=len(data.get("elements", [])))
                            return data
                        except Exception as j_err:
                            last_error = f"JSON decode error: {j_err}"
                    elif resp.status_code in [429, 504, 502, 503]:
                        # Backoff and retry
                        backoff = (attempt + 1) * 2.0
                        time.sleep(backoff)
                        last_error = f"HTTP {resp.status_code} from {endpoint}"
                    else:
                        last_error = f"HTTP {resp.status_code}: {resp.text[:150]}"
                        break
                except Exception as ex:
                    last_error = str(ex)
                    time.sleep(1.5)

        self.record_call(latency=0.0, error=True)
        print(f"[OSM] Query failed across endpoints: {last_error}")
        return {"elements": []}

    def _build_amenity_filter(self, industry: str) -> str:
        ind = industry.lower()
        if "restaurant" in ind or "food" in ind or "dining" in ind or "hospitality" in ind or "bistro" in ind:
            return "^(restaurant|cafe|bar|fast_food|pub|bistro|ice_cream)$"
        elif "pub" in ind or "bar" in ind:
            return "^(pub|bar|biergarten)$"
        elif "cafe" in ind or "coffee" in ind:
            return "^(cafe|coffee_shop)$"
        else:
            # Broad hospitality tags
            return "^(restaurant|cafe|bar|fast_food|pub|bistro)$"

    def validate_city_boundary(
        self,
        target_city: str,
        detected_city: str,
        address: str,
        lat: Optional[float],
        lon: Optional[float],
        country: str = "United Kingdom"
    ) -> Tuple[bool, str]:
        """
        Validates whether candidate strictly falls within target city boundary (Section 11 / Part 4).
        Performs true administrative polygon point-in-polygon validation with secondary safeguards.
        """
        res = self.boundary_validator.validate_candidate(
            city=target_city,
            country=country,
            lat=lat,
            lon=lon,
            address=address,
            detected_city=detected_city
        )
        return res.city_match, res.city_match_reason

    def search_businesses(
        self,
        city: str,
        country: str,
        industry: str,
        limit: int = 40,
        sub_districts: Optional[List[str]] = None,
        **kwargs
    ) -> List[DiscoveredBusiness]:
        """
        Discovers candidate places from OpenStreetMap via Overpass API.
        """
        start_time = time.time()
        amenity_regex = self._build_amenity_filter(industry)
        bbox = self.get_city_bounding_box(city, country)

        # Build Overpass QL with enclosing parent ways (Section 3)
        if bbox:
            south, north, west, east = bbox
            query = f"""[out:json][timeout:{self.timeout}];
(
  node["amenity"~"{amenity_regex}"]({south},{west},{north},{east})->.pois;
  way["amenity"~"{amenity_regex}"]({south},{west},{north},{east})->.poi_ways;
);
(
  .pois;
  .poi_ways;
  way(bn.pois);
);
out body center tags {limit * 3};
"""
        else:
            # Fallback to city area query
            clean_city = city.replace('"', '\\"')
            query = f"""[out:json][timeout:{self.timeout}];
area["name"="{clean_city}"]->.searchArea;
(
  node["amenity"~"{amenity_regex}"](area.searchArea)->.pois;
  way["amenity"~"{amenity_regex}"](area.searchArea)->.poi_ways;
);
(
  .pois;
  .poi_ways;
  way(bn.pois);
);
out body center tags {limit * 3};
"""

        data = self.execute_overpass_query(query)
        elements = data.get("elements", [])
        return self._process_elements(elements, city=city, country=country, industry=industry, limit=limit)

    def parse_osm_element(
        self,
        el: Dict[str, Any],
        city: str = "",
        country: str = "",
        industry: str = "",
        parent_elements: Optional[List[Dict[str, Any]]] = None
    ) -> Optional[DiscoveredBusiness]:
        """Parses a raw OSM node/way/relation into a DiscoveredBusiness candidate with parent address resolution."""
        tags = el.get("tags", {})
        name = (tags.get("name") or tags.get("official_name") or "").strip()
        if not name:
            return None

        official_name = (tags.get("official_name") or "").strip()
        brand = (tags.get("brand") or "").strip()
        amenity = (tags.get("amenity") or "").strip()
        cuisine = (tags.get("cuisine") or "").strip()

        # Coordinates
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        osm_id = str(el.get("id", ""))
        osm_type = el.get("type", "node")

        # Address components via AddressNormalizer with explicit parent way resolution (Phase 7.8)
        addr_profile = AddressNormalizer.resolve_osm_address(
            element=el,
            parent_elements=parent_elements,
            lat=lat,
            lon=lon,
            target_city=city or "Manchester",
            target_country=country or "United Kingdom"
        )
        street = addr_profile.street or ""
        housenumber = addr_profile.house_number or ""
        city_val = (addr_profile.city or city or "Manchester").strip()
        postcode = addr_profile.postcode or ""
        branch = addr_profile.branch_identifier or AddressNormalizer.extract_branch_marker(f"{name} {city_val}") or ""
        full_address = addr_profile.normalized_address
        address_completeness = addr_profile.completeness.value
        street_source = addr_profile.street_source
        house_number_source = addr_profile.house_number_source
        postcode_source = addr_profile.postcode_source
        parent_osm_id = addr_profile.parent_osm_id
        address_conflict_status = addr_profile.address_conflict_status
        conflicting_address_data = addr_profile.conflicting_address_data

        # Contact & Online Presence
        phone = (tags.get("phone") or tags.get("contact:phone") or "").strip()
        website = (tags.get("website") or tags.get("contact:website") or "").strip()
        facebook = (tags.get("contact:facebook") or tags.get("facebook") or "").strip()
        instagram = (tags.get("contact:instagram") or tags.get("instagram") or "").strip()
        opening_hours = (tags.get("opening_hours") or "").strip()

        # Target Country Validation
        c_status, det_country, reg, pcode, c_evidence = self.country_validator.validate(
            target_country=country,
            address=full_address,
            phone=phone,
            raw_data=tags
        )

        # Social status
        social_status = SocialStatus.SOCIAL_UNKNOWN.value
        if instagram or facebook:
            social_status = SocialStatus.SOCIAL_FOUND.value

        # City boundary validation (Section 11 / Part 4)
        b_res = self.boundary_validator.validate_candidate(
            city=city,
            country=country,
            lat=lat,
            lon=lon,
            address=full_address,
            detected_city=city_val
        )
        city_match = b_res.city_match
        city_match_reason = b_res.city_match_reason
        boundary_source = b_res.boundary_source
        boundary_validation_method = b_res.boundary_validation_method

        # Website source attribution (Section 12 / Part 5)
        osm_website_status = "LISTED_IN_OSM" if website else "WEBSITE_NOT_LISTED_IN_OSM"

        evidence_sources = {
            "name": "OPENSTREETMAP",
            "address": "OPENSTREETMAP_PARENT_WAY" if street_source == "OSM_PARENT_WAY" else ("OPENSTREETMAP" if street or postcode else "UNKNOWN"),
            "phone": "OPENSTREETMAP" if phone else "UNKNOWN",
            "website": "OPENSTREETMAP" if website else "WEBSITE_NOT_LISTED_IN_OSM",
            "instagram": "OPENSTREETMAP" if instagram else "UNKNOWN",
            "facebook": "OPENSTREETMAP" if facebook else "UNKNOWN"
        }

        return DiscoveredBusiness(
            company_name=name,
            category=amenity or industry,
            city=city_val,
            target_country=country,
            detected_country=det_country,
            country_status=c_status,
            country_evidence=c_evidence,
            region=reg,
            postcode=postcode or pcode,
            address=full_address,
            phone=phone,
            raw_website=website,
            google_maps_url=f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
            instagram_url=instagram,
            facebook_url=facebook,
            social_status=social_status,
            opening_hours=opening_hours,
            discovery_query=f"OSM {amenity or industry} in {city}, {country}",
            discovery_source="OPENSTREETMAP",
            source_id=osm_id,
            brand=brand,
            amenity=amenity,
            cuisine=cuisine,
            street=street,
            house_number=housenumber,
            branch_identifier=branch,
            address_completeness=address_completeness,
            street_source=street_source,
            house_number_source=house_number_source,
            postcode_source=postcode_source,
            parent_osm_id=parent_osm_id,
            address_conflict_status=address_conflict_status,
            conflicting_address_data=conflicting_address_data,
            lat=lat,
            lon=lon,
            osm_type=osm_type,
            source_confidence="HIGH",
            city_match=city_match,
            city_match_reason=city_match_reason,
            boundary_source=boundary_source,
            boundary_validation_method=boundary_validation_method,
            osm_website_status=osm_website_status,
            evidence_sources=evidence_sources,
            raw_data=el
        )

    def _process_elements(
        self,
        elements: List[Dict[str, Any]],
        city: str,
        country: str,
        industry: str,
        limit: int
    ) -> List[DiscoveredBusiness]:
        candidates: List[DiscoveredBusiness] = []
        seen_keys = set()

        # Build index of parent ways by node ID (single controlled query, 0 N+1 calls)
        node_to_ways: Dict[int, List[Dict[str, Any]]] = {}
        poi_elements: List[Dict[str, Any]] = []

        for el in elements:
            el_type = el.get("type", "node")
            el_tags = el.get("tags", {})
            if el_type == "way":
                for nid in el.get("nodes", []):
                    if nid not in node_to_ways:
                        node_to_ways[nid] = []
                    node_to_ways[nid].append(el)

            # Identify candidate POI elements
            if el_tags.get("name") and (el_tags.get("amenity") or el_tags.get("cuisine") or el_tags.get("shop")):
                poi_elements.append(el)
            elif el_tags.get("name"):
                poi_elements.append(el)

        for el in poi_elements:
            parent_ways = node_to_ways.get(el.get("id"), [])
            candidate = self.parse_osm_element(
                el,
                city=city,
                country=country,
                industry=industry,
                parent_elements=parent_ways
            )
            if not candidate:
                continue

            # Deduplicate with branch awareness (Section 12)
            norm_name = re.sub(r'[^a-z0-9]', '', candidate.company_name.lower())
            if candidate.postcode:
                norm_key = f"{norm_name}@{candidate.postcode.replace(' ', '')}"
            elif candidate.street:
                norm_key = f"{norm_name}@{re.sub(r'[^a-z0-9]', '', candidate.street.lower())}"
            elif hasattr(candidate, "branch_identifier") and candidate.branch_identifier:
                norm_key = f"{norm_name}@{re.sub(r'[^a-z0-9]', '', candidate.branch_identifier.lower())}"
            elif candidate.lat is not None and candidate.lon is not None:
                # Round to ~200m spatial grid
                norm_key = f"{norm_name}@{round(candidate.lat, 3)}_{round(candidate.lon, 3)}"
            else:
                norm_key = f"{norm_name}@{city.lower()}"

            if norm_key in seen_keys:
                continue
            seen_keys.add(norm_key)

            if not candidate.city_match:
                # Section 11: If business is adjacent to the target city but outside the configured boundary:
                # do NOT count it as a city match. Preserve in research data.
                self.adjacent_research_candidates.append(candidate)
                continue

            candidates.append(candidate)
            if len(candidates) >= limit:
                break

        # Prioritize candidates without websites (Prime prospects for Dripp Media)
        def candidate_sort_key(c: DiscoveredBusiness) -> int:
            w = (c.raw_website or "").strip().lower()
            if not w:
                return 0  # No website listed -> Highest priority
            if any(p in w for p in ["instagram.com", "facebook.com", "tiktok.com", "linktr.ee", "deliveroo", "ubereats", "just-eat"]):
                return 1  # Social/delivery platform listed as website
            return 2      # Website listed

        candidates.sort(key=candidate_sort_key)
        self.metrics.candidates_unique = len(candidates)
        return candidates

    def get_query_rounds(self, city: str, country: str, industry: str) -> List[List[str]]:
        """
        Defines progressive discovery rounds for OpenStreetMap.
        """
        return [
            # Round 1: Primary dining (restaurants, bistros)
            [f"amenity=restaurant in {city}, {country}", f"amenity=bistro in {city}, {country}"],
            # Round 2: Cafes and coffee spots
            [f"amenity=cafe in {city}, {country}", f"amenity=ice_cream in {city}, {country}"],
            # Round 3: Bars and pubs
            [f"amenity=bar in {city}, {country}", f"amenity=pub in {city}, {country}"],
            # Round 4: Takeaways and fast food
            [f"amenity=fast_food in {city}, {country}", f"amenity=street_food in {city}, {country}"]
        ]

    def has_more_rounds(self, current_round: int, city: str, country: str, industry: str) -> bool:
        return current_round <= len(self.get_query_rounds(city, country, industry))

    def discover_round(
        self,
        round_number: int,
        country: str,
        cities: List[str],
        industry: str,
        limit: int = 40
    ) -> Tuple[List[DiscoveredBusiness], List[str]]:
        city = cities[0] if cities else "Birmingham"
        rounds = self.get_query_rounds(city, country, industry)
        round_idx = round_number - 1
        if round_idx >= len(rounds):
            return [], []

        queries = rounds[round_idx]
        candidates = self.search_businesses(city=city, country=country, industry=industry, limit=limit)
        return candidates, queries
