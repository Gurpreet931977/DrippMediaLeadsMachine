"""
Dripp Media — Global Review Source Provider & Evidence Engine
============================================================
Defines the generic, international contract for review and rating discovery:
  - Country-neutral interface (discover_review_evidence, extract_review_count, extract_rating, validate_business_identity).
  - Uses CountryAdapterBundle to resolve localized search queries, preferred directories, and major cities.
  - Returns SOURCE_NOT_CONFIGURED for markets without a configured review source (never fabricated data).
  - Enforces strict business identity validation, wrong-branch isolation, listicle rejection, and landmark filtering.
"""

import re
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Tuple

from lib.country_adapters import get_country_adapter, AdapterStatus
from lib.discovery.web_search import WebSearchProvider, get_web_search_provider

# Generic listicle and city roundup patterns that must be rejected
GENERIC_LISTICLE_PATTERNS = [
    re.compile(r'\b(?:the\s+)?\d+\s+best\b', re.IGNORECASE),
    re.compile(r'\btop\s+\d+\s+(?:best\s+)?(?:restaurants|cafes|places|pubs|bars|bistros|takeaways|spots)\b', re.IGNORECASE),
    re.compile(r'\bbest\s+\d+\s+(?:restaurants|cafes|places|pubs|bars|spots)\b', re.IGNORECASE),
    re.compile(r'\bguide\s+to\s+(?:eating|dining|the\s+best)\b', re.IGNORECASE),
    re.compile(r'\bwhere\s+to\s+eat\s+in\b', re.IGNORECASE),
    re.compile(r'\bexplore\s+\d+\s+restaurants\b', re.IGNORECASE),
    re.compile(r'\bbrowse\s+\d+[\d,]*\s+reviews\s+of\b', re.IGNORECASE)
]


class ReviewSourceProvider(ABC):
    """
    Abstract contract for global review and rating evidence discovery.
    """

    @abstractmethod
    def discover_review_evidence(
        self,
        country: str,
        city: str,
        business_name: str,
        street: str = "",
        postcode: str = "",
        region: str = "",
        category: str = "",
        search_results: Optional[List[Dict[str, Any]]] = None
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Discovers review evidence items for a business.
        Returns: (status_code, evidence_items)
        Possible status codes: "SUPPORTED", "SOURCE_NOT_CONFIGURED"
        """
        pass

    @abstractmethod
    def extract_review_count(self, text: str) -> Optional[int]:
        """Extracts customer review count integer from text."""
        pass

    @abstractmethod
    def extract_rating(self, text: str) -> Optional[float]:
        """Extracts customer star rating (1.0 to 5.0) from text."""
        pass

    @abstractmethod
    def validate_business_identity(
        self,
        snippet: str,
        business_name: str,
        city: str,
        country: str,
        street: str = "",
        postcode: str = ""
    ) -> Tuple[bool, Optional[str]]:
        """
        Validates whether snippet text corresponds to the exact venue.
        Returns: (is_valid, reject_reason)
        """
        pass


class GlobalReviewSourceProvider(ReviewSourceProvider):
    """
    Concrete global implementation orchestrating web search and country adapters.
    """

    def __init__(self, web_search_provider: Optional[WebSearchProvider] = None):
        self.web = web_search_provider or get_web_search_provider()

    def extract_review_count(self, text: str) -> Optional[int]:
        if not text:
            return None
        # Support thousands separators: comma (1,010), dot (1.010 when followed by 3 digits), or space (1 010)
        pats = [
            r"(?:from|based on|with|over|accumulating)?\s*(\d{1,3}(?:[,\s]\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)\s*(?:(?:customer|user|diner|guest|google|tripadvisor|visitor|verified)\s+)?(?:reviews?|ratings?|votes?)",
            r"(?:reviews?|ratings?)[:\s]*(\d{1,3}(?:[,\s]\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)",
            r"[·•\-(]\s*‎?(\d{1,3}(?:[,\s]\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)\s*(?:reviews?|votes?)\s*\)?",
            r"based on\s+(\d{1,3}(?:[,\s]\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)",
        ]
        for pat in pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                raw_cnt = m.group(1).strip()
                clean_cnt = re.sub(r"[,\.\s]", "", raw_cnt)
                try:
                    cnt = int(clean_cnt)
                    if cnt > 0:
                        return cnt
                except ValueError:
                    pass
        return None

    def extract_rating(self, text: str) -> Optional[float]:
        if not text:
            return None
        # Support decimal dot or comma (4.6 or 4,6), /5, stars, out of 5
        pats = [
            r"(?:rating|rated|score|stars?)[:\s]*([1-5](?:[.,]\d)?)\s*(?:out of 5|/5|★|stars)?",
            r"\b([1-5][.,]\d)\s*(?:out of 5|/5|★|stars?)",
            r"\b([1-5][.,]\d)\s*[·•\-]\s*‎?\d+",
            r"\b([1-5][.,]\d)\s*\(\s*\d+",
            r"\b([1-5](?:[.,]\d)?)\s*(?:out of 5|/5|★|stars)\b",
        ]
        for pat in pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                raw_rat = m.group(1).replace(",", ".")
                try:
                    r = float(raw_rat)
                    if 1.0 <= r <= 5.0:
                        return r
                except ValueError:
                    pass
        return None

    def validate_business_identity(
        self,
        snippet: str,
        business_name: str,
        city: str,
        country: str,
        street: str = "",
        postcode: str = ""
    ) -> Tuple[bool, Optional[str]]:
        if not snippet:
            return False, "EMPTY_SNIPPET"

        def _clean_text(s: str) -> str:
            # Strip quotes and apostrophes completely so "Shezzaan's" -> "shezzaans"
            t = re.sub(r"['’`\"]", "", s.lower())
            return re.sub(r'[^a-z0-9\s]', ' ', t)

        norm_snippet = _clean_text(snippet)
        norm_bname = _clean_text(business_name).strip()
        tokens = norm_bname.split()

        # ── 1. Check for Generic Listicle / Roundup ──
        for pat in GENERIC_LISTICLE_PATTERNS:
            if pat.search(snippet):
                clean_biz = re.sub(r'[^a-z0-9]', '', business_name.lower())
                clean_snip = re.sub(r'[^a-z0-9]', '', snippet.lower())
                if not clean_snip.startswith(clean_biz):
                    return False, "GENERIC_LISTICLE_OR_ROUNDUP_PAGE"

        # ── 2. Business Identity Token Presence (Apostrophe & Stem Aware) ──
        name_matched = (norm_bname in norm_snippet) or (
            bool(tokens) and all(
                (t in norm_snippet or (t.endswith('s') and t[:-1] in norm_snippet))
                for t in tokens if len(t) > 2
            )
        )
        if not name_matched:
            return False, "MISSING_BUSINESS_IDENTITY"

        # ── 3. Wrong Branch / Other Major City Isolation ──
        target_city_norm = city.lower().strip()
        target_pc_norm = postcode.lower().strip()
        pc_prefix = target_pc_norm.split()[0] if target_pc_norm else ""

        adapter = get_country_adapter(country if country else "United Kingdom")
        major_cities = adapter.review_sources.get_major_cities() if hasattr(adapter.review_sources, "get_major_cities") else []

        for major_city in major_cities:
            if major_city in norm_snippet and major_city != target_city_norm:
                has_target_location = (
                    target_city_norm in norm_snippet or
                    (pc_prefix and pc_prefix in norm_snippet) or
                    (street and street.lower() in norm_snippet)
                )
                if not has_target_location:
                    return False, f"WRONG_BRANCH_LOCATION ({major_city.title()})"

        # ── 4. Unrelated Landmarks / Entities in Snippet ──
        unrelated_pattern = re.search(
            r'(?:near|next to|opposite|adjacent to)\s+([A-Za-z\s]+?)\s*[·(]?\s*(\d+[\d,]*)\s+reviews?',
            snippet,
            re.IGNORECASE
        )
        if unrelated_pattern:
            unrelated_name = unrelated_pattern.group(1).strip().lower()
            if norm_bname not in unrelated_name and unrelated_name not in norm_bname:
                return False, "UNRELATED_ENTITY_REVIEW_COUNT"

        return True, None

    def discover_review_evidence(
        self,
        country: str,
        city: str,
        business_name: str,
        street: str = "",
        postcode: str = "",
        region: str = "",
        category: str = "",
        search_results: Optional[List[Dict[str, Any]]] = None
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Global discovery entrypoint. Returns ("SOURCE_NOT_CONFIGURED", []) if market is unconfigured.
        Records Part 3 fields: source_type, source_domain, country, business, review_count,
        rating, confidence, evidence_text.
        """
        adapter = get_country_adapter(country if country else "United Kingdom")
        if not adapter or adapter.review_sources.status == AdapterStatus.NOT_CONFIGURED.value:
            return "SOURCE_NOT_CONFIGURED", []

        raw_results = []
        if search_results is not None:
            raw_results = search_results
        else:
            queries = adapter.review_sources.get_search_queries(
                business_name=business_name,
                city=city,
                street=street,
                postcode=postcode
            )
            for q in queries[:3]:
                res = self.web.search_web(q, num_results=3)
                raw_results.extend(res)

        evidence_items = []
        for r in raw_results:
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            url = r.get("result_url", "")
            combined = f"{title} — {snippet}".strip()

            is_valid, reject_reason = self.validate_business_identity(
                snippet=combined,
                business_name=business_name,
                city=city,
                country=country,
                street=street,
                postcode=postcode
            )

            # Extract apex domain
            source_domain = ""
            if url:
                raw_dom = url.lower().split("://")[-1].split("/")[0].split("?")[0]
                source_domain = raw_dom.replace("www.", "")

            if not is_valid:
                evidence_items.append({
                    "source_type": "SEARCH_SNIPPET",
                    "source_domain": source_domain,
                    "country": country,
                    "business": business_name,
                    "review_count": None,
                    "rating": None,
                    "confidence": "UNKNOWN",
                    "source": "Web",
                    "source_url": url,
                    "evidence_text": combined[:120],
                    "reject_reason": reject_reason
                })
                continue

            cnt = self.extract_review_count(combined)
            rat = self.extract_rating(combined)

            if cnt is not None or rat is not None:
                # Source platform and type identification
                u_low = url.lower()
                if "tripadvisor" in u_low:
                    src = "Tripadvisor"
                    src_type = "REVIEW_PLATFORM"
                elif "google" in u_low:
                    src = "Google"
                    src_type = "REVIEW_PLATFORM"
                elif "facebook" in u_low:
                    src = "Facebook"
                    src_type = "SOCIAL"
                elif "restaurantguru" in u_low:
                    src = "Restaurant Guru"
                    src_type = "DIRECTORY"
                elif "yelp" in u_low:
                    src = "Yelp"
                    src_type = "REVIEW_PLATFORM"
                elif "trustpilot" in u_low:
                    src = "Trustpilot"
                    src_type = "REVIEW_PLATFORM"
                elif "eateasy" in u_low or "wheree" in u_low:
                    src = "Directory"
                    src_type = "DIRECTORY"
                else:
                    src = "Web"
                    src_type = "SEARCH_SNIPPET"

                conf = "HIGH" if (cnt is not None and rat is not None and src != "Web") else ("MEDIUM" if (cnt is not None or rat is not None) else "LOW")

                evidence_items.append({
                    "source_type": src_type,
                    "source_domain": source_domain,
                    "country": country,
                    "business": business_name,
                    "review_count": cnt,
                    "rating": rat,
                    "confidence": conf,
                    "source": src,
                    "source_url": url,
                    "evidence_text": combined[:280],
                    "reject_reason": None
                })

        return "SUPPORTED", evidence_items
