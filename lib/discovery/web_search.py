"""
Web Search Provider
Cascading multi-engine web search implementation:
1. Tavily Search API (if TAVILY_API_KEY is configured, basic search by default)
2. Brave Search API (if BRAVE_API_KEY is configured)
3. SearXNG Local Search (if SEARXNG_URL is configured)
4. Public DuckDuckGo / HTML search fallback
5. Graceful skip (returns empty list, logs reason, never crashes)

Stores structured search evidence:
- search_provider
- query
- result_url
- title
- snippet
- retrieved_at
"""
import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import requests
from urllib.parse import quote_plus
from lib.discovery.base import DiscoveryProvider
from lib.types import DiscoveredBusiness, SocialStatus, CountryStatus
from lib.validation.country_validator import CountryValidator
from lib.discovery.boundary_validator import CityBoundaryValidator
from lib.discovery.address_normalizer import AddressNormalizer, UK_POSTCODE_REGEX

import random
from enum import Enum

class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class SearchOutcome(str, Enum):
    SEARCH_SUCCEEDED_WITH_RESULTS = "SEARCH_SUCCEEDED_WITH_RESULTS"
    SEARCH_SUCCEEDED_EMPTY = "SEARCH_SUCCEEDED_EMPTY"
    SEARCH_FAILED = "SEARCH_FAILED"
    SEARCH_BLOCKED = "SEARCH_BLOCKED"
    SEARCH_CIRCUIT_OPEN = "SEARCH_CIRCUIT_OPEN"
    SEARCH_PROVIDER_UNAVAILABLE = "SEARCH_PROVIDER_UNAVAILABLE"
    SEARCH_TIMEOUT = "SEARCH_TIMEOUT"
    # Phase 11.2 differentiated outcomes
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"


class SearchResultList(list):
    """
    Subclass of list preserving 100% backward compatibility for any caller
    treating search results as a list (e.g. len(), iteration, indexing, truthiness),
    while exposing structured execution metadata for downstream verifiers and enforcers.
    """
    def __init__(
        self,
        iterable=None,
        outcome: SearchOutcome = SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS,
        provider: str = "",
        query: str = "",
        error: str = "",
        http_status: Optional[int] = None,
        circuit_state: str = "CLOSED",
        is_cached: bool = False,
        retries: int = 0,
        latency: float = 0.0
    ):
        super().__init__(iterable or [])
        self.outcome = outcome
        self.provider = provider
        self.query = query
        self.error = error
        self.http_status = http_status
        self.circuit_state = circuit_state
        self.is_cached = is_cached
        self.retries = retries
        self.latency = latency
        self.attempted = (outcome not in [
            SearchOutcome.SEARCH_CIRCUIT_OPEN,
            SearchOutcome.SEARCH_PROVIDER_UNAVAILABLE,
            SearchOutcome.PROVIDER_UNAVAILABLE,
            SearchOutcome.PROVIDER_NOT_CONFIGURED,
            SearchOutcome.QUOTA_EXCEEDED,
        ])
        self.succeeded = (outcome in [SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS, SearchOutcome.SEARCH_SUCCEEDED_EMPTY])
        self.result_count = len(self)

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "outcome": self.outcome.value if isinstance(self.outcome, Enum) else str(self.outcome),
            "provider": self.provider,
            "query": self.query,
            "result_count": len(self),
            "error": self.error,
            "http_status": self.http_status,
            "circuit_state": self.circuit_state,
            "is_cached": self.is_cached,
            "retries": self.retries,
            "latency": round(self.latency, 3)
        }


class DuckDuckGoRateLimiter:
    """
    Rate limiter specifically for DuckDuckGo Lite fallback to prevent burst requests
    and avoid triggering 403 / anti-bot blocks.
    Configurable via DDG_MIN_DELAY and DDG_MAX_DELAY environment variables or constructor args.
    """
    def __init__(self, min_delay: Optional[float] = None, max_delay: Optional[float] = None):
        env_min = os.getenv("DDG_MIN_DELAY")
        env_max = os.getenv("DDG_MAX_DELAY")
        self.min_delay = float(env_min) if env_min is not None else (min_delay if min_delay is not None else 1.5)
        self.max_delay = float(env_max) if env_max is not None else (max_delay if max_delay is not None else 2.5)
        self.last_request_time: float = 0.0

    def wait_if_needed(self):
        now = time.time()
        elapsed = now - self.last_request_time
        target_delay = random.uniform(self.min_delay, self.max_delay)
        if elapsed < target_delay:
            sleep_duration = target_delay - elapsed
            time.sleep(sleep_duration)
        self.last_request_time = time.time()

    def reset(self):
        self.last_request_time = 0.0


class ProviderCircuitBreaker:
    """
    Per-provider 3-state circuit breaker:
    - CLOSED: Normal search operation
    - OPEN: Temporarily skipping failing provider during cooldown
    - HALF_OPEN: Probing recovery with a single test request
    - Automatically resumes to CLOSED after successful probe without restarting
    """
    def __init__(
        self,
        provider: str,
        failure_threshold: int = 2,
        cooldown_seconds: float = 30.0
    ):
        self.provider = provider
        env_th = os.getenv("SEARCH_FAILURE_THRESHOLD")
        self.failure_threshold = int(env_th) if (env_th and failure_threshold == 2) else failure_threshold
        env_cd = os.getenv("SEARCH_CIRCUIT_COOLDOWN_SECONDS")
        self.cooldown_seconds = float(env_cd) if (env_cd and cooldown_seconds == 30.0) else cooldown_seconds
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.opened_at: Optional[str] = None
        self.cooldown_until: Optional[float] = None
        self.last_success: Optional[str] = None
        self.last_failure: Optional[str] = None
        self.probe_result: Optional[str] = None
        self.probe_in_progress: bool = False

        # Metrics
        self.initial_state = CircuitState.CLOSED.value
        self.queries = 0
        self.results = 0
        self.failures = 0
        self.open_events = 0
        self.recovery_probes = 0
        self.recovered_count = 0
        self.fallback_usage = 0
        self.elapsed_time = 0.0

        # Detailed Audit Telemetry
        self.queries_attempted = 0
        self.successful_queries = 0
        self.empty_successful_queries = 0
        self.failed_queries = 0
        self.blocked_queries = 0
        self.circuit_open_queries = 0
        self.retries = 0
        self.retry_successes = 0
        self.retry_failures = 0

    @property
    def provider_attempts(self) -> int:
        return self.queries_attempted

    @property
    def circuit_open_bypasses(self) -> int:
        return self.circuit_open_queries

    @property
    def status(self) -> str:
        # Check automatic transition from OPEN to HALF_OPEN when cooldown expires
        if self.state == CircuitState.OPEN and self.cooldown_until is not None:
            if time.time() >= self.cooldown_until:
                self.state = CircuitState.HALF_OPEN
                self.probe_in_progress = False
        return self.state.value

    def is_available(self) -> bool:
        """Returns True if provider is ready to accept requests (CLOSED or HALF_OPEN probe ready)."""
        cur_status = self.status
        if cur_status == CircuitState.CLOSED.value:
            return True
        elif cur_status == CircuitState.HALF_OPEN.value:
            return not self.probe_in_progress
        return False

    def can_request(self) -> bool:
        cur_status = self.status
        if cur_status == CircuitState.CLOSED.value:
            return True
        elif cur_status == CircuitState.HALF_OPEN.value:
            if not self.probe_in_progress:
                self.probe_in_progress = True
                self.recovery_probes += 1
                return True
            return False
        return False  # OPEN

    def record_success(self, results_count: int = 0, latency: float = 0.0):
        now_iso = datetime.now(timezone.utc).isoformat()
        self.last_success = now_iso
        self.queries += 1
        self.results += results_count
        self.elapsed_time += latency
        if results_count > 0:
            self.successful_queries += 1
        else:
            self.empty_successful_queries += 1

        if self.state == CircuitState.HALF_OPEN:
            self.probe_result = "SUCCESS"
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.opened_at = None
            self.cooldown_until = None
            self.probe_in_progress = False
            self.recovered_count += 1
        else:
            self.failure_count = 0

    def record_failure(self, error_msg: str = "", latency: float = 0.0):
        now_iso = datetime.now(timezone.utc).isoformat()
        self.last_failure = now_iso
        self.failures += 1
        self.failure_count += 1
        self.failed_queries += 1
        if "403" in error_msg or "blocked" in error_msg.lower() or "429" in error_msg:
            self.blocked_queries += 1
        self.elapsed_time += latency

        if self.state == CircuitState.HALF_OPEN:
            self.probe_result = "FAILURE"
            self.state = CircuitState.OPEN
            self.opened_at = now_iso
            self.cooldown_until = time.time() + self.cooldown_seconds
            self.probe_in_progress = False
            self.open_events += 1
        elif self.state == CircuitState.CLOSED and self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = now_iso
            self.cooldown_until = time.time() + self.cooldown_seconds
            self.open_events += 1

    def trip(self, cooldown_seconds: Optional[float] = None):
        """Forces the circuit breaker into OPEN state (for testing / manual trip)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        self.state = CircuitState.OPEN
        self.opened_at = now_iso
        cd = cooldown_seconds if cooldown_seconds is not None else self.cooldown_seconds
        self.cooldown_until = time.time() + cd
        self.failure_count = self.failure_threshold
        self.open_events += 1

    def reset(self):
        """Resets the circuit breaker to CLOSED state."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.opened_at = None
        self.cooldown_until = None
        self.probe_result = None
        self.probe_in_progress = False
        self.queries_attempted = 0
        self.successful_queries = 0
        self.empty_successful_queries = 0
        self.failed_queries = 0
        self.blocked_queries = 0
        self.circuit_open_queries = 0
        self.retries = 0
        self.retry_successes = 0
        self.retry_failures = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "initial_state": self.initial_state,
            "failure_count": self.failure_count,
            "opened_at": self.opened_at,
            "cooldown_until": self.cooldown_until,
            "last_success": self.last_success,
            "last_failure": self.last_failure,
            "probe_result": self.probe_result,
            "queries": self.queries,
            "results": self.results,
            "failures": self.failures,
            "open_events": self.open_events,
            "recovery_probes": self.recovery_probes,
            "recovered_count": self.recovered_count,
            "fallback_usage": self.fallback_usage,
            "elapsed_time": round(self.elapsed_time, 3),
            "queries_attempted": self.queries_attempted,
            "provider_attempts": self.queries_attempted,
            "successful_queries": self.successful_queries,
            "successful_provider_queries": self.successful_queries,
            "empty_successful_queries": self.empty_successful_queries,
            "successful_empty_queries": self.empty_successful_queries,
            "failed_queries": self.failed_queries,
            "provider_failures": self.failed_queries,
            "blocked_queries": self.blocked_queries,
            "circuit_open_queries": self.circuit_open_queries,
            "circuit_open_bypasses": self.circuit_open_queries,
            "retries": self.retries,
            "retry_successes": self.retry_successes,
            "retry_failures": self.retry_failures
        }


_DEFAULT_COUNTRY_VALIDATOR: Optional[CountryValidator] = None
_DEFAULT_BOUNDARY_VALIDATOR: Optional[CityBoundaryValidator] = None

def _get_default_country_validator() -> CountryValidator:
    global _DEFAULT_COUNTRY_VALIDATOR
    if _DEFAULT_COUNTRY_VALIDATOR is None:
        _DEFAULT_COUNTRY_VALIDATOR = CountryValidator()
    return _DEFAULT_COUNTRY_VALIDATOR

def _get_default_boundary_validator() -> CityBoundaryValidator:
    global _DEFAULT_BOUNDARY_VALIDATOR
    if _DEFAULT_BOUNDARY_VALIDATOR is None:
        _DEFAULT_BOUNDARY_VALIDATOR = CityBoundaryValidator()
    return _DEFAULT_BOUNDARY_VALIDATOR


def convert_search_result_to_candidate(
    result: Dict[str, Any],
    city: str,
    country: str,
    industry: str,
    country_validator: Optional[CountryValidator] = None,
    boundary_validator: Optional[CityBoundaryValidator] = None
) -> Optional[DiscoveredBusiness]:
    """
    Converts a valid search result dictionary into a candidate DiscoveredBusiness record
    only when sufficient business identity and target-location evidence exist.
    Reuses existing validation and deduplication models.
    """
    if isinstance(result, DiscoveredBusiness):
        return result

    if not isinstance(result, dict):
        return None

    title = (result.get("title") or "").strip()
    snippet = (result.get("snippet") or result.get("content") or "").strip()
    raw_url = (result.get("result_url") or result.get("url") or "").strip()
    query = (result.get("query") or "").strip()

    if not title:
        return None

    # 1. Reject forums, social aggregators, directories, news, and listicle sites
    DISALLOWED_DISCOVERY_HOSTS = [
        "reddit.com", "quora.com", "wikipedia.org", "tripadvisor.com",
        "tripadvisor.co.uk", "yelp.com", "yelp.co.uk", "opentable.co.uk",
        "opentable.com", "timeout.com", "theguardian.com", "bbc.co.uk",
        "manchestereveningnews.co.uk", "eater.com", "cntraveller.com",
        "visitmanchester.com", "confidentialguides.com", "squaremeal.co.uk",
        "happycow.net", "trevors.org", "linkedin.com", "youtube.com",
        "x.com", "twitter.com"
    ]
    raw_url_lower = raw_url.lower()
    if any(host in raw_url_lower for host in DISALLOWED_DISCOVERY_HOSTS):
        return None

    # Reject forum questions and discussion titles
    if "?" in title:
        return None

    # 1b. Reject listicles, directories, roundups, articles
    LISTICLE_PATTERNS = [
        r'\b(?:top|best)\s+\d+\b',
        r'\b\d+\s+(?:of\s+the\s+)?(?:best|top|greatest|favourite|favorite|must-visit)\b',
        r'\b(?:the\s+)?(?:best|top|greatest)\s+(?:\w+\s+){0,3}(?:restaurants|cafes|bistros|places|spots|bars|pubs|eats|food)\b',
        r'\bguide\s+to\b',
        r'\bplaces\s+to\s+eat\b',
        r'\bwhere\s+to\s+eat\b',
        r'\bcheap\s+eats\b',
        r'\broundup\b',
        r'\bdirectory\b',
        r'\blist\s+of\b',
        r'\bthe 10 best\b',
        r'\bthe best \w+ in\b',
        r'\bthings\s+to\s+do\b',
        r'\bwhat\s+to\s+do\b',
        r'\bfood\s+guide\b',
        r'\brestaurant\s+guide\b',
        r'\brestaurant\s+awards\b',
        r'\boptions\s+for\b',
        r'\brecommendations\b',
        r'\bindie\s+in\s+the\s+city\b',
        r'\bsuppliers?\b',
        r'\blink\s+to\b',
        r'\bno\s+website\b',
        r'\bfamily\s+friendly\s+restaurants?\b'
    ]
    for pat in LISTICLE_PATTERNS:
        if re.search(pat, title, re.IGNORECASE):
            return None

    # 2. Extract and clean business name
    clean_title = re.sub(r'^(?:welcome to|home\s*[-|:]|about\s*[-|:]|about us\s*[-|:])\s*', '', title, flags=re.IGNORECASE).strip()
    clean_title = re.sub(r'\s*\(@[a-zA-Z0-9._-]+\)', '', clean_title)
    clean_title = re.sub(r'\s*•\s*Instagram.*$', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\s*[-|•]\s*Facebook.*$', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\s*[-|•]\s*Tripadvisor.*$', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\s*[-|•]\s*Yelp.*$', '', clean_title, flags=re.IGNORECASE)

    parts = [p.strip() for p in re.split(r'\s+[-|:•–—]\s+', clean_title) if p.strip()]
    if not parts:
        return None

    name_candidate = parts[0]
    if name_candidate.lower() in ["home", "welcome", "about", "about us", "menu", "contact", "contact us"] and len(parts) > 1:
        name_candidate = parts[1]

    cleaned_name = name_candidate.strip(" \"'()[]{}.,-")
    city_pat = rf'\b{re.escape(city)}\b'
    stripped_city_name = re.sub(city_pat, '', cleaned_name, flags=re.IGNORECASE).strip(" \"'()[]{}.,-")
    if len(stripped_city_name) >= 3 and re.search(r'[a-zA-Z]', stripped_city_name):
        if stripped_city_name.lower() not in ["independent", "family", "the", "restaurant", "cafe", "bar", "bistro", "pub", "grill"]:
            company_name = stripped_city_name
        else:
            company_name = cleaned_name
    else:
        company_name = cleaned_name

    company_name = re.sub(r'\s+', ' ', company_name).strip()

    # 3. Identity validation
    if len(company_name) < 3 or len(company_name) > 70:
        return None
    if not re.search(r'[a-zA-Z]', company_name):
        return None

    low_name = company_name.lower()
    GENERIC_NAME_BLACKLIST = {
        "restaurant", "restaurants", "cafe", "cafes", "bistro", "bistros",
        "bar", "bars", "pub", "pubs", "food", "dining", "eatery", "eateries",
        "fast food", "takeaway", "takeaways", "grill", "kitchen", "diner",
        "home", "welcome", "about us", "contact us", "menu", "reviews",
        "independent restaurant", "family restaurant", "italian restaurant",
        "best restaurants", "best independent restaurants", "places to eat",
        "tripadvisor", "yelp", "instagram", "facebook", "deliveroo", "ubereats",
        "just eat", "just-eat", "opentable", "wikipedia", "timeout", "michelin"
    }
    if low_name in GENERIC_NAME_BLACKLIST:
        return None
    if low_name.startswith(("best ", "top ", "the best ", "independent ", "family ", "list of ", "guide to ")):
        return None
    if re.search(r'\b(?:restaurants?|cafes?|bistros?|bars?|pubs?|places|eateries|suppliers?)\b', low_name) and \
       re.search(r'\b(?:best|top|independent|family|guide|options|favourite|favorite|friendly)\b', low_name):
        return None

    if low_name in [
        f"{industry.lower()} in {city.lower()}",
        f"{industry.lower()} {city.lower()}",
        f"independent {industry.lower()} {city.lower()}",
        f"independent {industry.lower()}"
    ]:
        return None

    # 4. Target-Location Evidence Verification
    # Only result title/snippet are positive location evidence. A target-city word
    # appearing only in a URL slug must not qualify an unrelated page.
    source_location_text = f"{title} {snippet}".strip()
    combined_text = source_location_text.lower()
    city_lower = (city or "").strip().lower()

    has_city_mention = bool(
        re.search(rf"\b{re.escape(city_lower)}\b", combined_text)
    ) if city_lower else False
    postcode_match = UK_POSTCODE_REGEX.search(source_location_text)
    extracted_postcode = postcode_match.group(1).upper() if postcode_match else ""

    MANCHESTER_AREAS = [
        "ancoats", "didsbury", "chorlton", "deansgate", "northern quarter",
        "spinningfields", "castlefield", "rusholme", "fallowfield", "salford",
        "trafford", "wythenshawe", "cheetham", "prestwich", "piccadilly",
        "oxford road", "peter street", "king street", "cross street", "arndale"
    ]
    has_area_mention = any(a in combined_text for a in MANCHESTER_AREAS) if city_lower == "manchester" else False

    # A UK postcode can support country validation, but without a target-city or
    # target-area mention it cannot establish that the business is in this city.
    if not (has_city_mention or has_area_mention):
        return None

    # Parse address components
    addr_profile = AddressNormalizer.parse_address_string(
        address=snippet,
        city=city,
        country=country,
        company_name=company_name
    )
    street = addr_profile.street or ""
    house_number = addr_profile.house_number or ""
    postcode = extracted_postcode or addr_profile.postcode or ""
    full_address = addr_profile.normalized_address or f"{city}, {country}"

    phone = ""
    phone_m = re.search(r'\b(?:(?:\+44\s?\(0\)\s?|\+44\s?|0)(?:1\d{1,3}|2\d{1,2}|3\d{2}|7\d{3})\s?\d{3,4}\s?\d{3,4})\b', snippet)
    if phone_m:
        phone = phone_m.group(0).strip()

    # 5. Target Country Validation
    # Do not feed a synthesized fallback address (e.g. "Manchester, United Kingdom")
    # into the validator: that fabricates the very country evidence this gate must prove.
    cv = country_validator or _get_default_country_validator()
    c_status, det_country, reg, pcode, c_evidence = cv.validate(
        target_country=country,
        address=source_location_text,
        phone=phone,
        raw_data={**result, "title": title, "snippet": snippet, "url": raw_url}
    )
    if c_status != CountryStatus.COUNTRY_MATCH.value:
        return None

    # 6. City Boundary Validation
    bv = boundary_validator or _get_default_boundary_validator()
    b_res = bv.validate_candidate(
        city=city,
        country=country,
        lat=None,
        lon=None,
        address=full_address,
        detected_city=addr_profile.city or city
    )
    if not b_res.city_match:
        return None

    # 7. Online presence classification (Website, Instagram, Facebook)
    raw_website = ""
    instagram_url = ""
    facebook_url = ""
    social_status = SocialStatus.SOCIAL_UNKNOWN.value

    AGGREGATOR_DOMAINS = [
        "tripadvisor", "yelp", "foursquare", "opentable", "deliveroo",
        "ubereats", "just-eat", "justeat", "wikipedia", "timeout",
        "michelin", "goodfoodguide", "happycow", "squaremeal", "booking.com",
        "trivago", "expedia", "trustpilot", "reddit.com", "bbc.co.uk",
        "manchestereveningnews.co.uk", "theguardian.com", "standard.co.uk"
    ]

    url_lower = raw_url.lower()
    if "instagram.com/" in url_lower:
        instagram_url = raw_url
        social_status = SocialStatus.SOCIAL_FOUND.value
    elif "facebook.com/" in url_lower:
        facebook_url = raw_url
        social_status = SocialStatus.SOCIAL_FOUND.value
    elif any(dom in url_lower for dom in AGGREGATOR_DOMAINS):
        raw_website = ""
    else:
        raw_website = raw_url

    if not instagram_url:
        ig_m = re.search(r'https?://(?:www\.)?instagram\.com/[a-zA-Z0-9._-]+/?', snippet)
        if ig_m:
            instagram_url = ig_m.group(0)
            social_status = SocialStatus.SOCIAL_FOUND.value

    if not facebook_url:
        fb_m = re.search(r'https?://(?:www\.)?facebook\.com/[a-zA-Z0-9._-]+/?', snippet)
        if fb_m:
            facebook_url = fb_m.group(0)
            social_status = SocialStatus.SOCIAL_FOUND.value

    return DiscoveredBusiness(
        company_name=company_name,
        category=industry,
        city=city,
        target_country=country,
        detected_country=det_country or country,
        country_status=c_status,
        country_evidence=c_evidence,
        region=reg or addr_profile.city or "",
        postcode=postcode or pcode or "",
        address=full_address,
        phone=phone,
        raw_website=raw_website,
        google_maps_url="",
        instagram_url=instagram_url,
        facebook_url=facebook_url,
        social_status=social_status,
        discovery_query=query,
        discovery_source="WEB_SEARCH",
        source_id=raw_url,
        street=street,
        house_number=house_number,
        source_confidence="MEDIUM",
        city_match=b_res.city_match,
        city_match_reason=b_res.city_match_reason,
        boundary_source=b_res.boundary_source,
        boundary_validation_method=b_res.boundary_validation_method,
        evidence_sources={
            "name": "WEB_SEARCH",
            "location": "WEB_SEARCH",
            "website": "WEB_SEARCH" if raw_website else "NONE",
            "instagram": "WEB_SEARCH" if instagram_url else "NONE",
            "facebook": "WEB_SEARCH" if facebook_url else "NONE",
            "phone": "WEB_SEARCH" if phone else "NONE"
        },
        raw_data={
            **result,
            "social_ownership_status": "UNVERIFIED"
        }
    )


class WebSearchProvider(DiscoveryProvider):
    def __init__(
        self,
        tavily_key: Optional[str] = None,
        brave_key: Optional[str] = None,
        searxng_url: Optional[str] = None,
        cache_dir: Optional[str] = None,
        cache_ttl_seconds: Optional[int] = None,
        cooldown_seconds: float = 30.0,
        failure_threshold: int = 2
    ):
        super().__init__(name="WebSearch")
        self.tavily_key = tavily_key or os.getenv("TAVILY_API_KEY") or ""
        self.brave_key = brave_key or os.getenv("BRAVE_API_KEY") or ""
        configured_searxng = searxng_url or os.getenv("SEARXNG_URL")
        self.searxng_url = (configured_searxng or "http://localhost:8080").rstrip("/")
        self._searxng_explicitly_set = bool(configured_searxng)
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = cache_dir or os.path.join(self.base_dir, "data", "cache_search")
        os.makedirs(self.cache_dir, exist_ok=True)
        ttl_env = os.getenv("WEB_SEARCH_CACHE_TTL_SECONDS")
        self.cache_ttl = cache_ttl_seconds if cache_ttl_seconds is not None else (int(ttl_env) if ttl_env else 365 * 86400)
        env_th = os.getenv("SEARCH_FAILURE_THRESHOLD")
        effective_threshold = int(env_th) if (env_th and failure_threshold == 2) else failure_threshold
        env_cd = os.getenv("SEARCH_CIRCUIT_COOLDOWN_SECONDS")
        effective_cooldown = float(env_cd) if (env_cd and cooldown_seconds == 30.0) else cooldown_seconds

        self.cooldown_seconds = effective_cooldown
        self.failure_threshold = effective_threshold

        # Dedicated per-provider circuit breakers with independent state
        self.circuit_breakers: Dict[str, ProviderCircuitBreaker] = {
            "TAVILY": ProviderCircuitBreaker("TAVILY", failure_threshold=effective_threshold, cooldown_seconds=effective_cooldown),
            "BRAVE": ProviderCircuitBreaker("BRAVE", failure_threshold=effective_threshold, cooldown_seconds=effective_cooldown),
            "SEARXNG": ProviderCircuitBreaker("SEARXNG", failure_threshold=effective_threshold, cooldown_seconds=effective_cooldown),
            "DUCKDUCKGO_FALLBACK": ProviderCircuitBreaker("DUCKDUCKGO_FALLBACK", failure_threshold=effective_threshold, cooldown_seconds=effective_cooldown)
        }

        self.ddg_rate_limiter = DuckDuckGoRateLimiter()
        self.cache_hits: int = 0
        self.cache_misses: int = 0
        self._query_cache_index: Dict[str, str] = {}
        self._index_existing_cache()

        self.quota_budget = None

        # Legacy stats dictionary compatibility layer
        self.stats_by_provider = self._build_stats_view()

        global _GLOBAL_WEB_SEARCH_PROVIDER
        _GLOBAL_WEB_SEARCH_PROVIDER = self

    def set_quota_budget(self, quota_budget: Any) -> None:
        """Attaches central QuotaBudget to WebSearchProvider for quota governance."""
        self.quota_budget = quota_budget

    def _sync_stats(self):
        """Synchronizes circuit breaker states into stats_by_provider."""
        for name, cb in self.circuit_breakers.items():
            d = cb.to_dict()
            # If provider is not configured with an API key, reflect NOT_CONNECTED in stats status
            if name == "TAVILY" and not self.tavily_key:
                d["status"] = "NOT_CONNECTED"
            elif name == "BRAVE" and not self.brave_key:
                d["status"] = "NOT_CONNECTED"
            elif name == "SEARXNG" and not getattr(self, "searxng_url", ""):
                d["status"] = "NOT_CONNECTED"
            self.stats_by_provider[name] = d

    def _build_stats_view(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        for name, cb in self.circuit_breakers.items():
            d = cb.to_dict()
            if name == "TAVILY" and not getattr(self, "tavily_key", ""):
                d["status"] = "NOT_CONNECTED"
            elif name == "BRAVE" and not getattr(self, "brave_key", ""):
                d["status"] = "NOT_CONNECTED"
            elif name == "SEARXNG" and not getattr(self, "searxng_url", ""):
                d["status"] = "NOT_CONNECTED"
            out[name] = d
        return out

    def get_telemetry(self) -> Dict[str, Any]:
        """
        Returns full explicit telemetry per search provider and aggregate.
        Fields: provider, status, failure_count, opened_at, cooldown_until,
                last_success, last_failure, probe_result, queries, results, elapsed_time,
                queries_attempted, successful_queries, empty_successful_queries,
                failed_queries, blocked_queries, circuit_open_queries, retries,
                retry_successes, retry_failures.
        """
        self._sync_stats()
        out = {name: cb.to_dict() for name, cb in self.circuit_breakers.items()}
        total_attempted = sum(cb.queries_attempted for cb in self.circuit_breakers.values())
        total_successful = sum(cb.successful_queries for cb in self.circuit_breakers.values())
        total_empty = sum(cb.empty_successful_queries for cb in self.circuit_breakers.values())
        total_failed = sum(cb.failed_queries for cb in self.circuit_breakers.values())
        total_blocked = sum(cb.blocked_queries for cb in self.circuit_breakers.values())
        total_circuit_open = sum(cb.circuit_open_queries for cb in self.circuit_breakers.values())
        total_retries = sum(cb.retries for cb in self.circuit_breakers.values())
        total_retry_success = sum(cb.retry_successes for cb in self.circuit_breakers.values())
        total_retry_failures = sum(cb.retry_failures for cb in self.circuit_breakers.values())
        total_latency = sum(cb.elapsed_time for cb in self.circuit_breakers.values())
        avg_latency = (total_latency / max(1, total_attempted)) if total_attempted > 0 else 0.0

        out["_AGGREGATE"] = {
            "queries_attempted": total_attempted,
            "provider_attempts": total_attempted,
            "successful_queries": total_successful,
            "successful_provider_queries": total_successful,
            "empty_successful_queries": total_empty,
            "successful_empty_queries": total_empty,
            "failed_queries": total_failed,
            "provider_failures": total_failed,
            "blocked_queries": total_blocked,
            "circuit_open_queries": total_circuit_open,
            "circuit_open_bypasses": total_circuit_open,
            "retries": total_retries,
            "retry_successes": total_retry_success,
            "retry_failures": total_retry_failures,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "total_latency": round(total_latency, 3),
            "average_latency": round(avg_latency, 3)
        }
        return out

    def get_aggregate_telemetry(self) -> Dict[str, Any]:
        return self.get_telemetry().get("_AGGREGATE", {})

    def check_searxng_health(self) -> str:
        """
        Explicit health probe for local SearXNG instance (Part 1 & 2).
        Exposes: CONNECTED, NOT_CONNECTED, ERROR.
        """
        if not self.searxng_url:
            return "NOT_CONNECTED"
        try:
            r = requests.get(f"{self.searxng_url}/", timeout=1.0)
            if r.status_code in [200, 301, 302]:
                return "CONNECTED"
            else:
                return "ERROR"
        except (requests.ConnectionError, requests.Timeout):
            return "NOT_CONNECTED"
        except Exception:
            return "ERROR"

    def _is_searxng_alive(self) -> bool:
        """Probes local SearXNG instance without using third-party public instances."""
        return self.check_searxng_health() == "CONNECTED"

    @staticmethod
    def _normalize_query_key(query: str, advanced: bool = False) -> str:
        clean = re.sub(r'["\']', '', query.strip().lower())
        clean = " ".join(clean.split())
        return f"norm_{clean}_{advanced}"

    def _index_existing_cache(self):
        """Indexes existing cache files by normalized query key for cross-parameter cache reuse."""
        if not os.path.isdir(self.cache_dir):
            return
        try:
            for fname in os.listdir(self.cache_dir):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(self.cache_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        entry = json.load(f)
                    k = entry.get("key", "")
                    if k:
                        m = re.match(r"^web_(.*)_(\d+)_(True|False)$", k, re.IGNORECASE)
                        if m:
                            raw_q = m.group(1)
                            adv = m.group(3).lower() == "true"
                            norm_k = self._normalize_query_key(raw_q, adv)
                            if norm_k not in self._query_cache_index:
                                self._query_cache_index[norm_k] = fpath
                except Exception:
                    continue
        except Exception:
            pass

    def _get_cache(self, key: str, query: str = "", advanced: bool = False) -> Optional[Dict[str, Any]]:
        # 1. Exact direct key lookup
        cache_file = os.path.join(self.cache_dir, f"{hashlib.md5(key.encode()).hexdigest()}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                if time.time() - entry.get("timestamp", 0) < self.cache_ttl:
                    self.cache_hits += 1
                    return entry
            except Exception:
                pass

        # 2. Normalized query key lookup for cross-parameter cache reuse
        if query:
            norm_k = self._normalize_query_key(query, advanced)
            indexed_path = self._query_cache_index.get(norm_k)
            if indexed_path and os.path.exists(indexed_path):
                try:
                    with open(indexed_path, "r", encoding="utf-8") as f:
                        entry = json.load(f)
                    if time.time() - entry.get("timestamp", 0) < self.cache_ttl:
                        self.cache_hits += 1
                        return entry
                except Exception:
                    pass

        self.cache_misses += 1
        return None

    def _set_cache(self, key: str, results: Any, provider: str = "", query: str = "", advanced: bool = False):
        """
        CRITICAL SAFETY INVARIANT: A provider/network failure must NEVER be persisted
        as a successful query with zero results.
        """
        succeeded = getattr(results, "succeeded", True if results else False)
        outcome_val = getattr(results, "outcome", None)
        out_name = outcome_val.value if hasattr(outcome_val, "value") else str(outcome_val or "")
        search_failed_states = {
            "SEARCH_FAILED",
            "SEARCH_BLOCKED",
            "SEARCH_CIRCUIT_OPEN",
            "SEARCH_PROVIDER_UNAVAILABLE",
            "SEARCH_TIMEOUT",
            "PROVIDER_NOT_CONFIGURED",
            "PROVIDER_FAILED",
            "PROVIDER_TIMEOUT",
            "PROVIDER_UNAVAILABLE",
            "QUOTA_EXCEEDED"
        }
        if not succeeded or out_name in search_failed_states:
            return

        cache_file = os.path.join(self.cache_dir, f"{hashlib.md5(key.encode()).hexdigest()}.json")
        try:
            if outcome_val is None:
                outcome_str = SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS.value if results else SearchOutcome.SEARCH_SUCCEEDED_EMPTY.value
            elif isinstance(outcome_val, Enum):
                outcome_str = outcome_val.value
            else:
                outcome_str = str(outcome_val)

            prov_str = provider or getattr(results, "provider", "")
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({
                    "timestamp": time.time(),
                    "key": key,
                    "outcome": outcome_str,
                    "provider": prov_str,
                    "data": list(results)
                }, f)
            if query:
                norm_k = self._normalize_query_key(query, advanced)
                self._query_cache_index[norm_k] = cache_file
        except Exception:
            pass

    def health_check(self) -> Dict[str, Any]:
        """Reports connectivity of all available search engines with clear transparency."""
        searxng_status = self.check_searxng_health()
        engines = {
            "tavily": "CONNECTED" if self.tavily_key else "NOT_CONNECTED",
            "brave": "CONNECTED" if self.brave_key else "NOT_CONNECTED",
            "searxng": searxng_status,
            "duckduckgo": self.circuit_breakers["DUCKDUCKGO_FALLBACK"].status
        }
        self._sync_stats()
        self.stats_by_provider["TAVILY"]["status"] = engines["tavily"]
        self.stats_by_provider["BRAVE"]["status"] = engines["brave"]
        self.stats_by_provider["SEARXNG"]["status"] = engines["searxng"]

        active_engine = "duckduckgo_fallback"
        if self.tavily_key and self.circuit_breakers["TAVILY"].is_available():
            active_engine = "tavily"
        elif self.brave_key and self.circuit_breakers["BRAVE"].is_available():
            active_engine = "brave"
        elif searxng_status == "CONNECTED" and self.circuit_breakers["SEARXNG"].is_available():
            active_engine = "searxng"

        return {
            "provider": "WebSearch",
            "status": "READY",
            "active_engine": active_engine,
            "engines": engines,
            "stats_by_provider": self.stats_by_provider,
            "telemetry": self.get_telemetry()
        }

    def _notify_tavily_monitor(self, outcome_str: str, query: str, latency: float = 0.0) -> None:
        try:
            from lib.monitoring.detectors import TavilyMonitor
            rem = getattr(self.quota_budget, "search_remaining", None) if self.quota_budget else None
            lim = getattr(self.quota_budget, "search_budget", None) if self.quota_budget else None
            TavilyMonitor().record_call_outcome(
                outcome_str=outcome_str,
                query=query,
                latency=latency,
                quota_remaining=rem,
                quota_limit=lim,
            )
        except Exception:
            pass

    def _search_tavily(self, query: str, num_results: int = 5, advanced: bool = False) -> SearchResultList:
        """Tavily search API (Basic search by default to conserve credits)."""
        if not self.tavily_key:
            return SearchResultList(
                outcome=SearchOutcome.PROVIDER_NOT_CONFIGURED,
                provider="TAVILY",
                query=query,
                error="Tavily API key is not configured"
            )

        if self.quota_budget is not None and not self.quota_budget.can_consume("tavily", 1):
            self._notify_tavily_monitor("QUOTA_EXCEEDED", query, 0.0)
            return SearchResultList(
                outcome=SearchOutcome.QUOTA_EXCEEDED,
                provider="TAVILY",
                query=query,
                error="Tavily quota budget exhausted"
            )

        cb = self.circuit_breakers["TAVILY"]
        if not cb.can_request():
            cb.circuit_open_queries += 1
            self._sync_stats()
            return SearchResultList(
                outcome=SearchOutcome.SEARCH_CIRCUIT_OPEN,
                provider="TAVILY",
                query=query,
                circuit_state=cb.status,
                error="Circuit breaker is OPEN"
            )

        cb.queries_attempted += 1
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": self.tavily_key,
            "query": query,
            "search_depth": "advanced" if advanced else "basic",
            "include_answer": False,
            "max_results": num_results
        }
        start_t = time.time()
        try:
            resp = requests.post(url, json=payload, timeout=10)
            latency = time.time() - start_t
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except (ValueError, json.JSONDecodeError) as json_err:
                    cb.record_failure(error_msg=f"Malformed JSON: {json_err}", latency=latency)
                    self.record_call(latency=latency, error=True)
                    self._sync_stats()
                    self._notify_tavily_monitor("PROVIDER_FAILED", query, latency)
                    return SearchResultList(
                        outcome=SearchOutcome.PROVIDER_FAILED,
                        provider="TAVILY",
                        query=query,
                        error=f"Malformed JSON response: {json_err}",
                        http_status=200,
                        circuit_state=cb.status,
                        latency=latency
                    )

                if self.quota_budget is not None:
                    self.quota_budget.consume("tavily", 1)

                results = []
                retrieved_at = datetime.now(timezone.utc).isoformat()
                for r in data.get("results", []):
                    results.append({
                        "search_provider": "TAVILY",
                        "query": query,
                        "result_url": r.get("url", ""),
                        "url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "snippet": r.get("content", ""),
                        "score": r.get("score", 0.0),
                        "retrieved_at": retrieved_at
                    })
                cb.record_success(results_count=len(results), latency=latency)
                self.record_call(latency=latency, candidates_count=len(results), cost=0.005 if advanced else 0.001)
                self._sync_stats()
                outcome = SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS if results else SearchOutcome.SEARCH_SUCCEEDED_EMPTY
                self._notify_tavily_monitor(outcome.value, query, latency)
                return SearchResultList(
                    iterable=results,
                    outcome=outcome,
                    provider="TAVILY",
                    query=query,
                    http_status=200,
                    circuit_state=cb.status,
                    latency=latency
                )
            elif resp.status_code == 429:
                cb.record_failure(error_msg=f"HTTP 429 Quota Exceeded / Rate Limited", latency=latency)
                self.record_call(latency=latency, error=True)
                self._sync_stats()
                self._notify_tavily_monitor("QUOTA_EXCEEDED", query, latency)
                return SearchResultList(
                    outcome=SearchOutcome.QUOTA_EXCEEDED,
                    provider="TAVILY",
                    query=query,
                    error="HTTP 429: Quota exceeded or rate limited",
                    http_status=429,
                    circuit_state=cb.status,
                    latency=latency
                )
            else:
                cb.record_failure(error_msg=f"HTTP {resp.status_code}", latency=latency)
                self.record_call(latency=latency, error=True)
                self._sync_stats()
                self._notify_tavily_monitor("PROVIDER_FAILED", query, latency)
                return SearchResultList(
                    outcome=SearchOutcome.PROVIDER_FAILED,
                    provider="TAVILY",
                    query=query,
                    error=f"HTTP {resp.status_code}",
                    http_status=resp.status_code,
                    circuit_state=cb.status,
                    latency=latency
                )
        except requests.Timeout as ex:
            latency = time.time() - start_t
            cb.record_failure(error_msg=str(ex), latency=latency)
            self.record_call(latency=0.0, error=True)
            self._sync_stats()
            self._notify_tavily_monitor("PROVIDER_TIMEOUT", query, latency)
            return SearchResultList(
                outcome=SearchOutcome.PROVIDER_TIMEOUT,
                provider="TAVILY",
                query=query,
                error=str(ex),
                circuit_state=cb.status,
                latency=latency
            )
        except (requests.ConnectionError, requests.exceptions.ConnectionError) as ex:
            latency = time.time() - start_t
            cb.record_failure(error_msg=str(ex), latency=latency)
            self.record_call(latency=0.0, error=True)
            self._sync_stats()
            self._notify_tavily_monitor("PROVIDER_FAILED", query, latency)
            return SearchResultList(
                outcome=SearchOutcome.PROVIDER_UNAVAILABLE,
                provider="TAVILY",
                query=query,
                error=str(ex),
                circuit_state=cb.status,
                latency=latency
            )
        except Exception as ex:
            latency = time.time() - start_t
            cb.record_failure(error_msg=str(ex), latency=latency)
            self.record_call(latency=0.0, error=True)
            self._sync_stats()
            outcome = SearchOutcome.PROVIDER_TIMEOUT if "Timeout" in type(ex).__name__ else (
                SearchOutcome.PROVIDER_UNAVAILABLE if ("Connection" in type(ex).__name__ or "ConnectionRefused" in str(ex)) else SearchOutcome.PROVIDER_FAILED
            )
            self._notify_tavily_monitor(outcome.value, query, latency)
            return SearchResultList(
                outcome=outcome,
                provider="TAVILY",
                query=query,
                error=str(ex),
                circuit_state=cb.status,
                latency=latency
            )

    def _search_brave(self, query: str, num_results: int = 5) -> SearchResultList:
        """Brave Search API."""
        cb = self.circuit_breakers["BRAVE"]
        if not cb.can_request():
            cb.circuit_open_queries += 1
            self._sync_stats()
            return SearchResultList(
                outcome=SearchOutcome.SEARCH_CIRCUIT_OPEN,
                provider="BRAVE",
                query=query,
                circuit_state=cb.status,
                error="Circuit breaker is OPEN"
            )

        cb.queries_attempted += 1
        url = "https://api.search.brave.com/res/v1/web/search"
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.brave_key
        }
        params = {"q": query, "count": num_results}
        start_t = time.time()
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            latency = time.time() - start_t
            if resp.status_code == 200:
                data = resp.json()
                results = []
                retrieved_at = datetime.now(timezone.utc).isoformat()
                for r in data.get("web", {}).get("results", []):
                    results.append({
                        "search_provider": "BRAVE",
                        "query": query,
                        "result_url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "snippet": r.get("description", ""),
                        "retrieved_at": retrieved_at
                    })
                cb.record_success(results_count=len(results), latency=latency)
                self.record_call(latency=latency, candidates_count=len(results), cost=0.003)
                self._sync_stats()
                outcome = SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS if results else SearchOutcome.SEARCH_SUCCEEDED_EMPTY
                return SearchResultList(
                    iterable=results,
                    outcome=outcome,
                    provider="BRAVE",
                    query=query,
                    http_status=200,
                    circuit_state=cb.status,
                    latency=latency
                )
            else:
                cb.record_failure(error_msg=f"HTTP {resp.status_code}", latency=latency)
                self.record_call(latency=latency, error=True)
                self._sync_stats()
                return SearchResultList(
                    outcome=SearchOutcome.SEARCH_BLOCKED if resp.status_code in [403, 429] else SearchOutcome.SEARCH_FAILED,
                    provider="BRAVE",
                    query=query,
                    error=f"HTTP {resp.status_code}",
                    http_status=resp.status_code,
                    circuit_state=cb.status,
                    latency=latency
                )
        except Exception as ex:
            latency = time.time() - start_t
            cb.record_failure(error_msg=str(ex), latency=latency)
            self.record_call(latency=0.0, error=True)
            self._sync_stats()
            outcome = SearchOutcome.SEARCH_TIMEOUT if "Timeout" in type(ex).__name__ else SearchOutcome.SEARCH_FAILED
            return SearchResultList(
                outcome=outcome,
                provider="BRAVE",
                query=query,
                error=str(ex),
                circuit_state=cb.status,
                latency=latency
            )

    def _search_searxng(self, query: str, num_results: int = 5) -> SearchResultList:
        """SearXNG local instance search (Part 1 & 2)."""
        cb = self.circuit_breakers["SEARXNG"]
        if not cb.can_request():
            cb.circuit_open_queries += 1
            self._sync_stats()
            return SearchResultList(
                outcome=SearchOutcome.SEARCH_CIRCUIT_OPEN,
                provider="SEARXNG",
                query=query,
                circuit_state=cb.status,
                error="Circuit breaker is OPEN"
            )

        if not self.searxng_url:
            self._sync_stats()
            return SearchResultList(
                outcome=SearchOutcome.SEARCH_PROVIDER_UNAVAILABLE,
                provider="SEARXNG",
                query=query,
                circuit_state=cb.status,
                error="SearXNG URL is not configured"
            )

        cb.queries_attempted += 1
        url = f"{self.searxng_url}/search"
        params = {"q": query, "format": "json"}
        start_t = time.time()
        try:
            resp = requests.get(url, params=params, timeout=(2.0, 5.0))
            latency = time.time() - start_t
            if resp.status_code == 200:
                data = resp.json()
                results = []
                retrieved_at = datetime.now(timezone.utc).isoformat()
                for r in data.get("results", [])[:num_results]:
                    results.append({
                        "search_provider": "SEARXNG",
                        "searxng_engine": r.get("engine", ""),
                        "query": query,
                        "result_url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "snippet": r.get("content", ""),
                        "retrieved_at": retrieved_at
                    })
                unresponsive = data.get("unresponsive_engines", [])
                if not results and unresponsive:
                    err_summary = ", ".join(f"{eng}: {msg}" for eng, msg in unresponsive)
                    is_blocked = any(
                        any(k in str(m).lower() for k in ["captcha", "too many requests", "access denied", "403", "429", "blocked", "suspended"])
                        for _, m in unresponsive
                    )
                    is_timeout = any("timeout" in str(m).lower() for _, m in unresponsive)
                    if is_blocked:
                        outcome = SearchOutcome.SEARCH_BLOCKED
                    elif is_timeout:
                        outcome = SearchOutcome.SEARCH_TIMEOUT
                    else:
                        outcome = SearchOutcome.SEARCH_FAILED

                    cb.record_failure(error_msg=f"Engines unresponsive ({err_summary})", latency=latency)
                    self.record_call(latency=latency, error=True)
                    self._sync_stats()
                    return SearchResultList(
                        iterable=[],
                        outcome=outcome,
                        provider="SEARXNG",
                        query=query,
                        error=f"SearXNG engines unresponsive: {err_summary}",
                        http_status=200,
                        circuit_state=cb.status,
                        latency=latency
                    )

                cb.record_success(results_count=len(results), latency=latency)
                self.record_call(latency=latency, candidates_count=len(results), cost=0.0)
                self._sync_stats()
                outcome = SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS if results else SearchOutcome.SEARCH_SUCCEEDED_EMPTY
                return SearchResultList(
                    iterable=results,
                    outcome=outcome,
                    provider="SEARXNG",
                    query=query,
                    http_status=200,
                    circuit_state=cb.status,
                    latency=latency
                )
            else:
                cb.record_failure(error_msg=f"HTTP {resp.status_code}", latency=latency)
                self.record_call(latency=latency, error=True)
                self._sync_stats()
                return SearchResultList(
                    outcome=SearchOutcome.SEARCH_BLOCKED if resp.status_code in [403, 429] else SearchOutcome.SEARCH_FAILED,
                    provider="SEARXNG",
                    query=query,
                    error=f"HTTP {resp.status_code}",
                    http_status=resp.status_code,
                    circuit_state=cb.status,
                    latency=latency
                )
        except Exception as ex:
            latency = time.time() - start_t
            cb.record_failure(error_msg=str(ex), latency=latency)
            self.record_call(latency=0.0, error=True)
            self._sync_stats()
            ex_name = type(ex).__name__
            if "Timeout" in ex_name:
                outcome = SearchOutcome.SEARCH_TIMEOUT
            elif "Connection" in ex_name or "ConnectionRefused" in str(ex):
                outcome = SearchOutcome.SEARCH_PROVIDER_UNAVAILABLE
            else:
                outcome = SearchOutcome.SEARCH_FAILED
            return SearchResultList(
                outcome=outcome,
                provider="SEARXNG",
                query=query,
                error=str(ex),
                circuit_state=cb.status,
                latency=latency
            )

    def _search_duckduckgo_fallback(self, query: str, num_results: int = 5, max_retries: Optional[int] = None) -> SearchResultList:
        """
        Lightweight HTTP fallback search using DuckDuckGo Lite.
        Public, free, no API key required.
        Protected by self.circuit_breakers['DUCKDUCKGO_FALLBACK'] and self.ddg_rate_limiter.
        Implements bounded exponential backoff on transient errors before recording failure.
        """
        cb = self.circuit_breakers["DUCKDUCKGO_FALLBACK"]
        if not cb.can_request():
            cb.circuit_open_queries += 1
            self._sync_stats()
            return SearchResultList(
                outcome=SearchOutcome.SEARCH_CIRCUIT_OPEN,
                provider="DUCKDUCKGO_FALLBACK",
                query=query,
                circuit_state=cb.status,
                error="Circuit breaker is OPEN"
            )

        url = "https://lite.duckduckgo.com/lite/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded"
        }

        # Configurable retry policy
        if max_retries is None:
            max_retries = int(os.getenv("SEARCH_MAX_RETRIES", "2"))
        initial_backoff = float(os.getenv("SEARCH_INITIAL_BACKOFF", "1.0"))
        backoff_factor = float(os.getenv("SEARCH_BACKOFF_FACTOR", "2.0"))
        max_backoff = float(os.getenv("SEARCH_MAX_BACKOFF", "8.0"))

        is_bot_challenge = False
        last_error = ""
        last_status = None
        attempt = 0
        total_retries = 0
        total_latency = 0.0

        while attempt <= max_retries:
            # Respect rate limiter before every HTTP request
            self.ddg_rate_limiter.wait_if_needed()
            cb.queries_attempted += 1

            start_t = time.time()
            try:
                resp = requests.post(url, data={"q": query}, headers=headers, timeout=(2.5, 4.0))
                latency = time.time() - start_t
                total_latency += latency
                last_status = resp.status_code

                if resp.status_code == 200:
                    results = []
                    retrieved_at = datetime.now(timezone.utc).isoformat()
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(resp.text, "html.parser")
                    links = soup.find_all("a", class_="result-link")
                    snippets = soup.find_all("td", class_="result-snippet")

                    for i, link_tag in enumerate(links[:num_results]):
                        link = link_tag.get("href", "").strip()
                        title = link_tag.get_text(" ", strip=True)
                        snippet = snippets[i].get_text(" ", strip=True) if i < len(snippets) else ""

                        if link:
                            results.append({
                                "search_provider": "DUCKDUCKGO_FALLBACK",
                                "query": query,
                                "result_url": link,
                                "title": title,
                                "snippet": snippet,
                                "retrieved_at": retrieved_at
                            })

                    cb.record_success(results_count=len(results), latency=total_latency)
                    self.record_call(latency=total_latency, candidates_count=len(results), cost=0.0)
                    if total_retries > 0:
                        cb.retry_successes += 1
                    self._sync_stats()
                    outcome = SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS if results else SearchOutcome.SEARCH_SUCCEEDED_EMPTY
                    return SearchResultList(
                        iterable=results,
                        outcome=outcome,
                        provider="DUCKDUCKGO_FALLBACK",
                        query=query,
                        http_status=200,
                        circuit_state=cb.status,
                        retries=total_retries,
                        latency=total_latency
                    )

                is_bot_challenge = (
                    resp.status_code == 202
                    or "anomaly.js" in resp.text
                    or "Select all squares containing a duck" in resp.text
                    or "confirm this search was made by a human" in resp.text
                    or (resp.status_code != 200 and "challenge" in resp.text.lower())
                )

                if is_bot_challenge or resp.status_code == 403:
                    # Non-bypassable anti-bot challenge or 403 access denial - do not burn retries
                    last_error = f"HTTP {resp.status_code} DuckDuckGo bot detection challenge" if is_bot_challenge else "HTTP 403 Forbidden"
                    break

                elif resp.status_code in [429, 500, 502, 503, 504]:
                    # Transient server error or rate limit - eligible for bounded backoff retry
                    last_error = f"HTTP {resp.status_code}"
                    if attempt < max_retries:
                        attempt += 1
                        total_retries += 1
                        cb.retries += 1
                        backoff = min(max_backoff, initial_backoff * (backoff_factor ** (attempt - 1)))
                        time.sleep(backoff)
                        continue
                    else:
                        cb.retry_failures += 1
                        break
                else:
                    # Permanent client error (e.g. 400, 404) - do not retry
                    last_error = f"HTTP {resp.status_code}"
                    break

            except (requests.ConnectionError, requests.Timeout) as ex:
                latency = time.time() - start_t
                total_latency += latency
                last_error = f"{type(ex).__name__}: {str(ex)}"
                if attempt < max_retries:
                    attempt += 1
                    total_retries += 1
                    cb.retries += 1
                    backoff = min(max_backoff, initial_backoff * (backoff_factor ** (attempt - 1)))
                    time.sleep(backoff)
                    continue
                else:
                    cb.retry_failures += 1
                    break
            except Exception as ex:
                last_error = str(ex)
                break

        # If retries exhausted or challenge hit
        cb.record_failure(error_msg=last_error, latency=total_latency)
        self.record_call(latency=total_latency, error=True)
        self._sync_stats()

        outcome = SearchOutcome.SEARCH_TIMEOUT if "Timeout" in last_error else (
            SearchOutcome.SEARCH_BLOCKED if (
                is_bot_challenge or last_status in [202, 403, 429] or "403" in last_error or "202" in last_error or "challenge" in last_error.lower()
            ) else SearchOutcome.SEARCH_FAILED
        )
        return SearchResultList(
            outcome=outcome,
            provider="DUCKDUCKGO_FALLBACK",
            query=query,
            error=last_error,
            http_status=last_status,
            circuit_state=cb.status,
            retries=total_retries,
            latency=total_latency
        )

    def search_web(self, query: str, num_results: int = 5, advanced: bool = False) -> SearchResultList:
        """
        Executes web search in order (Part 1 & 2):
        1. Tavily if configured and circuit closed/half-open
        2. Brave if configured and circuit closed/half-open
        3. Local SearXNG if connected and circuit closed/half-open
        4. DuckDuckGo Lite fallback if circuit closed/half-open
        5. Skip (never hard-fails the pipeline)
        Caches query results to avoid redundant calls.
        """
        cache_key = f"web_{query.strip().lower()}_{num_results}_{advanced}"
        cached_entry = self._get_cache(cache_key, query=query, advanced=advanced)
        if cached_entry is not None:
            # Reconstruct SearchResultList with is_cached=True from cache entry
            if isinstance(cached_entry, dict):
                data = cached_entry.get("data", [])
                outcome_val = cached_entry.get("outcome")
                prov = cached_entry.get("provider") or (data[0].get("search_provider") if data else "DUCKDUCKGO_FALLBACK")
            else:
                data = list(cached_entry)
                outcome_val = getattr(cached_entry, "outcome", SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS)
                prov = getattr(cached_entry, "provider", "DUCKDUCKGO_FALLBACK")

            if not outcome_val:
                outcome_val = SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS.value if data else SearchOutcome.SEARCH_SUCCEEDED_EMPTY.value

            if hasattr(outcome_val, "value"):
                outcome_val = outcome_val.value

            if num_results and len(data) > num_results:
                data = data[:num_results]

            if prov in self.circuit_breakers:
                cb = self.circuit_breakers[prov]
                cb.queries += 1
                cb.results += len(data)
                if prov == "DUCKDUCKGO_FALLBACK":
                    cb.fallback_usage += 1
            self._sync_stats()
            return SearchResultList(
                iterable=data,
                outcome=SearchOutcome(outcome_val) if outcome_val in SearchOutcome._value2member_map_ else SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS,
                provider=prov,
                query=query,
                circuit_state=self.circuit_breakers.get(prov, ProviderCircuitBreaker(prov)).status,
                is_cached=True,
                latency=0.0
            )

        last_result: Optional[SearchResultList] = None

        # 1. Tavily if configured
        if self.tavily_key and self.circuit_breakers["TAVILY"].is_available():
            res = self._search_tavily(query, num_results=num_results, advanced=advanced)
            if not isinstance(res, SearchResultList):
                res = SearchResultList(res, outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS if res else SearchOutcome.SEARCH_SUCCEEDED_EMPTY, provider="TAVILY", query=query)
            if res.succeeded and len(res) > 0:
                self._set_cache(cache_key, res, provider="TAVILY", query=query, advanced=advanced)
                self._sync_stats()
                return res
            last_result = res
            self.circuit_breakers["TAVILY"].fallback_usage += 1

        # 2. Brave fallback if configured
        if self.brave_key and self.circuit_breakers["BRAVE"].is_available():
            res = self._search_brave(query, num_results=num_results)
            if not isinstance(res, SearchResultList):
                res = SearchResultList(res, outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS if res else SearchOutcome.SEARCH_SUCCEEDED_EMPTY, provider="BRAVE", query=query)
            if res.succeeded and len(res) > 0:
                self._set_cache(cache_key, res, provider="BRAVE", query=query, advanced=advanced)
                self._sync_stats()
                return res
            last_result = res
            self.circuit_breakers["BRAVE"].fallback_usage += 1

        # 3. Local SearXNG if configured and circuit allows
        if self.searxng_url and self.circuit_breakers["SEARXNG"].is_available():
            res = self._search_searxng(query, num_results=num_results)
            if not isinstance(res, SearchResultList):
                res = SearchResultList(res, outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS if res else SearchOutcome.SEARCH_SUCCEEDED_EMPTY, provider="SEARXNG", query=query)
            if res.succeeded:
                # Cache successful queries (both WITH_RESULTS and EMPTY)
                self._set_cache(cache_key, res, provider="SEARXNG", query=query, advanced=advanced)
                self._sync_stats()
                return res
            last_result = res
            self.circuit_breakers["SEARXNG"].fallback_usage += 1
        elif self.searxng_url and not self.circuit_breakers["SEARXNG"].is_available():
            cb = self.circuit_breakers["SEARXNG"]
            cb.circuit_open_queries += 1
            last_result = SearchResultList(
                outcome=SearchOutcome.SEARCH_CIRCUIT_OPEN,
                provider="SEARXNG",
                query=query,
                circuit_state=cb.status,
                error="Circuit breaker is OPEN"
            )

        # 4. DuckDuckGo HTML fallback
        if self.circuit_breakers["DUCKDUCKGO_FALLBACK"].is_available():
            res = self._search_duckduckgo_fallback(query, num_results=num_results)
            if not isinstance(res, SearchResultList):
                res = SearchResultList(res, outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS if res else SearchOutcome.SEARCH_SUCCEEDED_EMPTY, provider="DUCKDUCKGO_FALLBACK", query=query)
            if res.succeeded:
                # Cache successful queries (both WITH_RESULTS and EMPTY)
                self._set_cache(cache_key, res, provider="DUCKDUCKGO_FALLBACK", query=query, advanced=advanced)
                self._sync_stats()
                return res
            last_result = res
            self.circuit_breakers["DUCKDUCKGO_FALLBACK"].fallback_usage += 1
        elif not last_result:
            cb = self.circuit_breakers["DUCKDUCKGO_FALLBACK"]
            cb.circuit_open_queries += 1
            last_result = SearchResultList(
                outcome=SearchOutcome.SEARCH_CIRCUIT_OPEN,
                provider="DUCKDUCKGO_FALLBACK",
                query=query,
                circuit_state=cb.status,
                error="Circuit breaker is OPEN"
            )

        self._sync_stats()
        if last_result is not None:
            if not isinstance(last_result, SearchResultList):
                last_result = SearchResultList(last_result, outcome=SearchOutcome.SEARCH_SUCCEEDED_WITH_RESULTS if last_result else SearchOutcome.SEARCH_SUCCEEDED_EMPTY, query=query)
            return last_result

        # All providers unavailable / unconfigured
        if not self.tavily_key and not self.brave_key and not self._searxng_explicitly_set:
            outcome = SearchOutcome.PROVIDER_NOT_CONFIGURED
            err_msg = "No search providers configured (TAVILY_API_KEY, BRAVE_API_KEY, or SEARXNG_URL missing)"
        else:
            outcome = SearchOutcome.SEARCH_PROVIDER_UNAVAILABLE
            err_msg = "No search providers configured or available"
        return SearchResultList(
            outcome=outcome,
            provider="NONE",
            query=query,
            error=err_msg
        )

    def search_businesses(
        self,
        city: str,
        country: str,
        industry: str,
        limit: int = 40,
        **kwargs
    ) -> List[Any]:
        """
        Discovery query generator using diverse query families.
        Surfaces independent restaurants without websites (raw results for enrichment).
        """
        industry_term = re.sub(
            r"^(?:(?:independent|family[- ]run)\s+)+",
            "",
            (industry or "").strip(),
            flags=re.IGNORECASE,
        ).strip().lower() or "restaurants"

        names = kwargs.get("candidate_names") or kwargs.get("business_names") or []
        names = list(dict.fromkeys(
            str(name).strip().replace('"', "")
            for name in names if str(name).strip()
        ))[:10]

        if names:
            # Search businesses individually to avoid broad OR-query noise.
            queries = [
                f'site:instagram.com OR site:facebook.com "{name}" "{city}"'
                for name in names
            ]
        else:
            queries = [
                f'site:instagram.com "{city}" "{industry_term}"',
                f'site:facebook.com "{city}" "{industry_term}"',
            ]

        result_limit = max(0, int(limit))
        if result_limit == 0:
            return []

        # Keep candidate searches useful while bounding each provider response.
        per_query_limit = min(
            5 if names else 10,
            max(1, (result_limit + len(queries) - 1) // len(queries)),
        )
        result_batches = []
        for query in queries:
            rows = self.search_web(query, num_results=per_query_limit)
            result_batches.append(list(rows or []))

        # Round-robin results so early candidates cannot consume the whole limit.
        all_results = []
        seen_urls = set()
        max_depth = max((len(batch) for batch in result_batches), default=0)
        for depth in range(max_depth):
            for batch in result_batches:
                if depth >= len(batch):
                    continue
                item = batch[depth]
                if isinstance(item, dict):
                    url = str(
                        item.get("result_url") or item.get("url") or ""
                    ).strip()
                    key = url.rstrip("/").lower()
                    if key and key in seen_urls:
                        continue
                    if key:
                        seen_urls.add(key)
                all_results.append(item)
                if len(all_results) >= result_limit:
                    return all_results
        return all_results

    def discover_businesses(
        self,
        city: str,
        country: str,
        industry: str,
        limit: int = 40,
        **kwargs
    ) -> List[DiscoveredBusiness]:
        """
        Independent web-search discovery fallback when primary providers fail.
        Queries diverse discovery families and converts valid search results
        into validated DiscoveredBusiness records with strict location and identity gates.
        """
        city_label = (city or "").strip()
        country_label = (country or "").strip()
        industry_label = (industry or "").strip().lower()
        country_key = re.sub(r"[^a-z]", "", country_label.lower())

        # Geographic context is part of every fallback query. Searching a city
        # name alone returned unrelated places that share the same name
        # (e.g. Manchester, Vermont or Maryland) when Overpass was unavailable.
        if country_key in {"unitedkingdom", "uk", "greatbritain", "gb", "england", "scotland", "wales"}:
            queries = [
                f'"independent {industry_label}" "{city_label}" England UK',
                f'"{industry_label}" "{city_label}" England "no website"',
                f'"{industry_label}" "{city_label}" "{country_label}"',
                f'"family-run {industry_label}" "{city_label}" England UK',
            ]
            if city_label.casefold() == "manchester":
                # Greater Manchester terminology is common in local listings;
                # retain it as a separate query family rather than widening the
                # location gate or accepting ambiguous city-name matches.
                queries.extend([
                    f'"independent {industry_label}" "Greater Manchester" England',
                    f'"{industry_label}" "Greater Manchester" "no website"',
                ])
        else:
            queries = [
                f'"independent {industry_label}" "{city_label}" "{country_label}"',
                f'"{industry_label}" "{city_label}" "{country_label}" "no website"',
                f'"{industry_label}" in "{city_label}" "{country_label}"',
                f'"family-run {industry_label}" "{city_label}" "{country_label}"',
            ]
        candidates: List[DiscoveredBusiness] = []
        seen_identities = set()
        any_query_attempted = False
        all_failed = True
        last_error = ""

        for q in queries:
            if len(candidates) >= limit:
                break
            any_query_attempted = True
            search_res = self.search_web(q, num_results=min(limit, 10))
            is_succeeded = getattr(search_res, "succeeded", True if isinstance(search_res, list) and not hasattr(search_res, "succeeded") else False)
            if is_succeeded:
                all_failed = False
                for r in search_res:
                    if isinstance(r, DiscoveredBusiness):
                        cand = r
                    else:
                        cand = convert_search_result_to_candidate(
                            result=r,
                            city=city,
                            country=country,
                            industry=industry
                        )
                    if not cand:
                        continue
                    canon_key = f"{re.sub(r'[^a-z0-9]', '', cand.company_name.lower())}@{cand.city.lower()}@{cand.target_country.lower()}"
                    if canon_key in seen_identities:
                        continue
                    seen_identities.add(canon_key)
                    candidates.append(cand)
                    if len(candidates) >= limit:
                        break
            else:
                last_error = getattr(search_res, "error", "") or str(getattr(search_res, "outcome", ""))

        if any_query_attempted and all_failed:
            raise RuntimeError(f"WebSearchProvider unavailable or failed: {last_error or 'all queries failed'}")

        self.metrics.candidates_unique = len(candidates)
        return candidates

    def search_social_references(
        self,
        business_name: str,
        city: str,
        country: str = "United Kingdom",
        neighborhood: str = "",
        street: str = "",
        postcode: str = "",
        category: str = "Restaurant",
        limit_queries: int = 5,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """
        Searches for public third-party creator / social references to the business (Part 2).
        Builds multiple query families:
        1. exact business name + city
        2. exact business name + Instagram
        3. exact business name + TikTok
        4. exact business name + review
        5. exact business name + food blogger
        6. exact business name + food creator
        7. exact business name + reels
        8. exact business name + neighborhood (if given)
        9. exact business name + street (if given)
        10. business name variations (without legal suffixes)
        11. business name without punctuation
        12. business name + postcode (if available)
        """
        clean_name = business_name.replace('"', '').strip()
        clean_city = city.replace('"', '').strip()
        no_punct_name = re.sub(r"[^\w\s]", "", clean_name).strip()
        var_name = re.sub(r"\b(ltd|limited|co|restaurant|cafe|bar|pub|grill|kitchen)\b", "", clean_name, flags=re.IGNORECASE).strip()

        all_queries = [
            f'"{clean_name}" "{clean_city}"',
            f'"{clean_name}" Instagram',
            f'"{clean_name}" TikTok',
            f'"{clean_name}" review',
            f'"{clean_name}" food blogger',
            f'"{clean_name}" food creator',
            f'"{clean_name}" reels',
        ]
        if neighborhood:
            clean_neigh = neighborhood.replace('"', '').strip()
            all_queries.append(f'"{clean_name}" "{clean_neigh}"')
        if street:
            clean_street = street.replace('"', '').strip()
            all_queries.append(f'"{clean_name}" "{clean_street}"')
        if postcode:
            clean_pc = postcode.replace('"', '').strip()
            all_queries.append(f'"{clean_name}" "{clean_pc}"')
        if no_punct_name and no_punct_name != clean_name:
            all_queries.append(f'"{no_punct_name}" "{clean_city}"')
        if var_name and var_name != clean_name and len(var_name) >= 3:
            all_queries.append(f'"{var_name}" "{clean_city}" food')

        selected_queries = all_queries[:limit_queries]
        results = []
        seen_urls = set()
        for q in selected_queries:
            search_res = self.search_web(q, num_results=3)
            for r in search_res:
                r["discovery_query"] = q
                u = r.get("result_url")
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    results.append(r)
        return results


_GLOBAL_WEB_SEARCH_PROVIDER: Optional[WebSearchProvider] = None

def get_web_search_provider() -> WebSearchProvider:
    global _GLOBAL_WEB_SEARCH_PROVIDER
    if _GLOBAL_WEB_SEARCH_PROVIDER is None:
        _GLOBAL_WEB_SEARCH_PROVIDER = WebSearchProvider()
    return _GLOBAL_WEB_SEARCH_PROVIDER

def check_searxng_health(url: str = "http://localhost:8080") -> str:
    """Helper function to probe SearXNG health returning CONNECTED, NOT_CONNECTED, or ERROR."""
    from lib.discovery.searxng_helper import SearXNGHelper
    helper = SearXNGHelper(url=url)
    res = helper.check_health()
    return res.get("status", "NOT_CONNECTED")

def get_search_stats(provider: Optional[WebSearchProvider] = None) -> Dict[str, Any]:
    """Returns the transparency stats dictionary for web search providers."""
    p = provider or get_web_search_provider()
    return p.stats_by_provider

def reset_search_stats(provider: Optional[WebSearchProvider] = None):
    """Resets the search stats counters and circuit breakers for testing/benchmarking."""
    p = provider or get_web_search_provider()
    for cb in p.circuit_breakers.values():
        cb.reset()
        cb.queries = 0
        cb.results = 0
        cb.failures = 0
        cb.fallback_usage = 0
        cb.elapsed_time = 0.0
    p.cache_hits = 0
    p.cache_misses = 0
    if hasattr(p, "ddg_rate_limiter") and p.ddg_rate_limiter:
        p.ddg_rate_limiter.reset()
    p._sync_stats()


