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
import logging
from typing import List, Dict, Any, Optional, Tuple
import requests

from lib.discovery.base import DiscoveryProvider
from lib.types import DiscoveredBusiness, SocialStatus, CountryStatus
from lib.validation.country_validator import CountryValidator
from lib.discovery.boundary_validator import CityBoundaryValidator
from lib.discovery.address_normalizer import AddressNormalizer, AddressCompleteness, extract_uk_postcode

logger = logging.getLogger(__name__)

DEFAULT_OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
]


def get_default_endpoints() -> List[str]:
    """Retrieves list of Overpass endpoints, allowing override via OVERPASS_ENDPOINTS env var."""
    env_endpoints = os.environ.get("OVERPASS_ENDPOINTS")
    if env_endpoints:
        parsed = [ep.strip() for ep in env_endpoints.split(",") if ep.strip()]
        if parsed:
            return parsed
    return list(DEFAULT_OVERPASS_ENDPOINTS)


OVERPASS_ENDPOINTS = get_default_endpoints()

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

USER_AGENT = "DrippMediaLeadEngine/3.0 (contact: mediadripp@gmail.com; lead-research)"


class OverpassQueryError(RuntimeError):
    """Raised when an OpenStreetMap Overpass query fails across all configured endpoints."""
    pass


class OpenStreetMapProvider(DiscoveryProvider):
    def __init__(
        self,
        cache_dir: Optional[str] = None,
        cache_ttl_seconds: int = 86400, # 24 hours
        request_timeout: Optional[int] = None,
        min_request_interval: float = 1.2,
        endpoints: Optional[List[str]] = None
    ):
        super().__init__(name="OpenStreetMap")
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = cache_dir or os.path.join(self.base_dir, "data", "cache_osm")
        os.makedirs(self.cache_dir, exist_ok=True)
        cache_ttl_env = os.getenv("OSM_CACHE_TTL_SECONDS")
        if cache_ttl_env:
            try:
                self.cache_ttl = int(cache_ttl_env)
            except ValueError:
                self.cache_ttl = cache_ttl_seconds
        else:
            self.cache_ttl = cache_ttl_seconds

        # Sensible timeout: configurable via env OVERPASS_TIMEOUT or param or default 25s
        env_timeout = os.environ.get("OVERPASS_TIMEOUT")
        if request_timeout is not None:
            self.timeout = request_timeout
        elif env_timeout:
            try:
                self.timeout = int(env_timeout)
            except ValueError:
                self.timeout = 25
        else:
            self.timeout = 25

        self.min_interval = min_request_interval
        self.endpoints = list(endpoints) if endpoints is not None else get_default_endpoints()
        self._last_request_time: float = 0.0

        # Once every configured Overpass mirror has failed, do not spend the same
        # multi-minute timeout budget again on each discovery round. Keep the
        # circuit open briefly and let HybridDiscoveryEngine use its web fallback.
        try:
            self.overpass_failure_cooldown_seconds = max(
                0.0, float(os.getenv("OVERPASS_FAILURE_COOLDOWN_SECONDS", "180"))
            )
        except (TypeError, ValueError):
            self.overpass_failure_cooldown_seconds = 180.0
        self._overpass_circuit_open_until = 0.0

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
                if os.getenv("OSM_USE_FALLBACK_CACHE", "true").lower() in ("true", "1", "yes"):
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
        """Checks Overpass API connectivity across configured endpoints."""
        headers = {"User-Agent": USER_AGENT}
        endpoints = self.endpoints or OVERPASS_ENDPOINTS
        for ep in endpoints:
            status_url = ep.replace("/api/interpreter", "/api/status")
            try:
                r = requests.get(status_url, headers=headers, timeout=4)
                if r.status_code == 200:
                    details = r.text.strip().split("\n")[:3]
                    return {
                        "provider": "OpenStreetMap",
                        "status": "CONNECTED",
                        "endpoint": ep,
                        "details": details
                    }
            except Exception:
                continue
        cache_files = [f for f in os.listdir(self.cache_dir) if f.endswith(".json")] if os.path.exists(self.cache_dir) else []
        if cache_files:
            return {
                "provider": "OpenStreetMap",
                "status": "CONNECTED",
                "endpoint": "local_cache",
                "details": [f"Operating with local cache ({len(cache_files)} entries)"]
            }
        return {
            "provider": "OpenStreetMap",
            "status": "NOT_CONNECTED",
            "error": "All Overpass status endpoints unreachable"
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
            logger.warning("[OSM] Nominatim geocoding error for %s, %s: %s", city, country, e)
        return None

    def execute_overpass_query(self, query: str) -> Dict[str, Any]:
        """
        Executes Overpass QL with local caching, rate limiting, and multi-endpoint failover.
        """
        cached = self._get_cache(query)
        if cached is not None:
            return cached

        remaining_cooldown = self._overpass_circuit_open_until - time.monotonic()
        if remaining_cooldown > 0:
            remaining_seconds = max(1, int(remaining_cooldown + 0.999))
            raise OverpassQueryError(
                "OpenStreetMap Overpass circuit is OPEN after a previous all-endpoint outage; "
                f"retry in approximately {remaining_seconds}s. Use the configured discovery fallback."
            )

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json"
        }
        endpoints = self.endpoints or OVERPASS_ENDPOINTS
        last_error = None
        attempt_errors = []

        max_attempts_per_endpoint = 2
        req_timeout = (min(6.0, float(self.timeout)), float(self.timeout)) if isinstance(self.timeout, (int, float)) and self.timeout > 6 else self.timeout

        for endpoint in endpoints:
            for attempt in range(1, max_attempts_per_endpoint + 1):
                self._wait_rate_limit()
                try:
                    start_t = time.time()
                    resp = requests.post(
                        endpoint,
                        data={"data": query},
                        headers=headers,
                        timeout=req_timeout
                    )
                    latency = time.time() - start_t

                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                            elements = data.get("elements", [])
                            self._set_cache(query, data)
                            # Any successful HTTP 200 query (including a legitimate
                            # empty result) proves the provider is reachable again.
                            self._overpass_circuit_open_until = 0.0
                            self.record_call(latency=latency, candidates_count=len(elements))
                            logger.info(
                                "[OSM] Query succeeded via %s in %.2fs (%d elements found)",
                                endpoint, latency, len(elements)
                            )
                            return data
                        except Exception as j_err:
                            last_error = f"JSON decode error from {endpoint}: {j_err}"
                            attempt_errors.append(f"{endpoint} (attempt {attempt}): {last_error}")
                            logger.warning("[OSM] %s", last_error)
                    elif resp.status_code in [429, 502, 503, 504]:
                        backoff = attempt * 1.5
                        last_error = f"HTTP {resp.status_code} from {endpoint}"
                        attempt_errors.append(f"{endpoint} (attempt {attempt}): {last_error}")
                        logger.warning(
                            "[OSM] %s (attempt %d/%d). %s",
                            last_error, attempt, max_attempts_per_endpoint,
                            f"Backing off {backoff:.1f}s..." if attempt < max_attempts_per_endpoint else "Failing over to next mirror."
                        )
                        if attempt < max_attempts_per_endpoint:
                            time.sleep(backoff)
                    else:
                        snippet = resp.text[:150].replace('\n', ' ')
                        last_error = f"HTTP {resp.status_code} from {endpoint}: {snippet}"
                        attempt_errors.append(f"{endpoint} (attempt {attempt}): {last_error}")
                        logger.warning("[OSM] Non-retryable %s. Failing over to next mirror.", last_error)
                        break
                except requests.exceptions.Timeout as t_err:
                    last_error = f"Timeout ({self.timeout}s) from {endpoint}: {t_err}"
                    attempt_errors.append(f"{endpoint} (attempt {attempt}): timeout")
                    logger.warning(
                        "[OSM] Timeout on %s (attempt %d/%d); failing over immediately to the next mirror.",
                        endpoint, attempt, max_attempts_per_endpoint
                    )
                    # An overloaded/slow mirror is unlikely to serve the identical query
                    # on an immediate retry. Preserve availability by spending the time
                    # budget on the next independent mirror instead.
                    break
                except Exception as ex:
                    last_error = f"Error from {endpoint}: {ex}"
                    attempt_errors.append(f"{endpoint} (attempt {attempt}): {last_error}")
                    logger.warning(
                        "[OSM] Error on %s (attempt %d/%d): %s. %s",
                        endpoint, attempt, max_attempts_per_endpoint, ex,
                        "Retrying..." if attempt < max_attempts_per_endpoint else "Failing over to next mirror."
                    )
                    if attempt < max_attempts_per_endpoint:
                        time.sleep(1.0)

        self.record_call(latency=0.0, error=True)
        if self.overpass_failure_cooldown_seconds > 0:
            self._overpass_circuit_open_until = (
                time.monotonic() + self.overpass_failure_cooldown_seconds
            )
        error_msg = (
            f"OpenStreetMap Overpass query failed across all {len(endpoints)} endpoints. "
            f"Last error: {last_error}. Details: {'; '.join(attempt_errors[-4:])}"
        )
        logger.error("[OSM] %s", error_msg)
        raise OverpassQueryError(error_msg)

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
