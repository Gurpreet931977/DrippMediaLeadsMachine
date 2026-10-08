"""
Dripp Media — Free-First Review & Rating Enrichment Layer
=========================================================
Discovers and extracts customer review counts, star ratings, evidence snippets,
dates, and source provenance for candidate businesses using free public web sources.

Architecture:
  OSM Discovery
  → Review/Rating Enrichment (This Layer)
  → Website Audit
  → Operational Verification
  → Social Verification
  → Qualification V3

Safety Invariants:
  - Free public web sources only (Tavily/Brave/SearXNG/DDG Lite chain -> Crawl4AI fallback).
  - No mandatory paid APIs, no Apify, no authentication bypass, no anti-bot circumvention.
  - Never fabricate or infer review counts. Missing review data -> UNKNOWN, NOT 0 reviews.
  - Strict business identity & branch isolation (reject wrong branches / multi-branch collisions).
  - Explicit conflict handling: preserve all conflicting sources under CONFLICT_REQUIRES_REVIEW.
  - Never overwrite a stronger verified value with weaker evidence.
"""

import os
import re
import json
import hashlib
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple, Set, Union

from lib.types import DiscoveredBusiness
from lib.discovery.web_search import WebSearchProvider, check_searxng_health
from lib.discovery.crawler import CrawlEngine
from lib.enrichment.review_date_extractor import (
    ReviewDateExtractor,
    ReviewEvidenceDateType,
    ReviewDateConfidence,
    ExtractedReviewDate,
)

# Benchmark reference date: 2026-10-01
REFERENCE_DATE = datetime(2026, 10, 1, tzinfo=timezone.utc)
RECENT_THRESHOLD_DAYS = 180  # ~6 months


class ReviewConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class ReviewFreshness(str, Enum):
    RECENT = "RECENT"    # Within 180 days
    STALE = "STALE"      # Older than 180 days
    UNKNOWN = "UNKNOWN"  # No date discovered


class ReviewStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT_REQUIRES_REVIEW = "CONFLICT_REQUIRES_REVIEW"
    SEARCH_FAILED = "SEARCH_FAILED"


@dataclass
class ReviewEvidenceItem:
    review_count: Optional[int] = None
    rating: Optional[float] = None
    source: str = "Web"
    source_url: str = ""
    evidence_text: str = ""
    evidence_date: Optional[str] = None
    confidence: str = ReviewConfidence.UNKNOWN.value
    freshness: str = ReviewFreshness.UNKNOWN.value
    business_match_tier: str = "PARTIAL"
    reject_reason: Optional[str] = None
    review_provider: str = ""
    # Phase 5.2 Explicit Date Provenance
    evidence_date_raw: Optional[str] = None
    evidence_date_type: str = ReviewEvidenceDateType.UNKNOWN.value
    evidence_date_confidence: str = ReviewConfidence.UNKNOWN.value
    evidence_as_of: Optional[str] = None
    extraction_method: str = "NONE"
    # Phase 7.3 Multi-Source Provenance & Branch Isolation
    source_family: str = "UNKNOWN"
    business_name: str = ""
    identity_confidence: float = 0.0
    retrieved_at: str = ""
    source_quality: str = "STANDARD"
    branch_identifier: str = ""
    street: str = ""
    postcode: str = ""
    city: str = ""
    phone: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewEnrichmentResult:
    review_count: Optional[int] = None
    rating: Optional[float] = None
    review_source: str = ""
    review_source_url: str = ""
    review_evidence: str = ""
    review_evidence_date: Optional[str] = None
    review_confidence: str = ReviewConfidence.UNKNOWN.value
    review_freshness: str = ReviewFreshness.UNKNOWN.value
    review_status: str = ReviewStatus.NOT_FOUND.value
    review_provider: str = ""
    # Phase 5.2 Explicit Date Provenance
    review_evidence_date_raw: Optional[str] = None
    review_evidence_date_type: str = ReviewEvidenceDateType.UNKNOWN.value
    review_evidence_date_confidence: str = ReviewConfidence.UNKNOWN.value
    review_evidence_as_of: Optional[str] = None
    extraction_method: str = "NONE"
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    all_evidence: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Generic directory / listicle patterns that must be rejected
LISTICLE_PATTERNS = [
    re.compile(r'\b(?:the\s+)?\d+\s+best\b', re.IGNORECASE),
    re.compile(r'\btop\s+\d+\s+(?:best\s+)?(?:restaurants|cafes|places|pubs|bars|bistros|takeaways)\b', re.IGNORECASE),
    re.compile(r'\bbest\s+\d+\s+(?:restaurants|cafes|places|pubs|bars)\b', re.IGNORECASE),
    re.compile(r'\bguide\s+to\s+(?:eating|dining|the\s+best)\b', re.IGNORECASE),
    re.compile(r'\bwhere\s+to\s+eat\s+in\b', re.IGNORECASE),
    re.compile(r'\bexplore\s+\d+\s+restaurants\b', re.IGNORECASE),
    re.compile(r'\bbrowse\s+\d+[\d,]*\s+reviews\s+of\b', re.IGNORECASE)
]

# Major UK cities to detect wrong-branch / multi-branch collisions
MAJOR_UK_CITIES = [
    "manchester", "london", "birmingham", "bristol", "edinburgh",
    "glasgow", "sheffield", "liverpool", "newcastle", "nottingham",
    "cardiff", "belfast", "leicester", "southampton", "oxford", "cambridge"
]


class ReviewRatingEnricher:
    """
    Enriches candidate businesses with verified review counts, ratings,
    and supporting provenance without synthetic injection or paid APIs.
    """

    def __init__(
        self,
        web_search_provider: Optional[WebSearchProvider] = None,
        crawler: Optional[CrawlEngine] = None,
        cache_dir: Optional[str] = None
    ):
        self.web = web_search_provider or WebSearchProvider()
        self.crawler = crawler or CrawlEngine()
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = cache_dir or os.path.join(base_dir, "data", "cache_review_enrichment")
        os.makedirs(self.cache_dir, exist_ok=True)

    # ──────────────────────────────────────────────────────────────────────────
    # 1. QUERY GENERATION
    # ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def generate_queries(
        business_name: str,
        city: str,
        street: str = "",
        postcode: str = ""
    ) -> List[str]:
        """
        Generates targeted search queries with punctuation normalization.
        """
        clean_name = re.sub(r'["\']', '', business_name).strip()
        queries = [
            f'"{clean_name}" "{city}" restaurant',
            f'"{clean_name}" "{city}" reviews',
            f'"{clean_name}" "{city}" rating reviews',
            f'"{clean_name}" "{city}" Tripadvisor',
            f'"{clean_name}" "{city}" Google reviews',
            f'"{clean_name}" "{city}" Facebook reviews'
        ]

        if postcode:
            pc_clean = postcode.strip()
            queries.append(f'"{clean_name}" "{pc_clean}" reviews')
            # Outer postcode area (e.g. LS21, LS19)
            pc_prefix = pc_clean.split()[0]
            if pc_prefix != pc_clean:
                queries.append(f'"{clean_name}" "{pc_prefix}" reviews')

        if street:
            queries.append(f'"{clean_name}" "{street.strip()}" reviews')

        # Normalized variation without apostrophes/special characters
        alt_name = re.sub(r'[^a-zA-Z0-9\s]', '', clean_name).strip()
        if alt_name.lower() != clean_name.lower():
            queries.append(f'"{alt_name}" "{city}" reviews')

        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for q in queries:
            if q not in seen:
                seen.add(q)
                deduped.append(q)

        return deduped

    # ──────────────────────────────────────────────────────────────────────────
    # 2. EVIDENCE EXTRACTION FROM SEARCH SNIPPET / PAGE
    # ──────────────────────────────────────────────────────────────────────────
    @classmethod
    def extract_evidence_from_snippet(
        cls,
        snippet: str,
        title: str,
        url: str,
        business_name: str,
        city: str,
        street: str = "",
        postcode: str = "",
        country: str = "United Kingdom",
        provider: str = ""
    ) -> Optional[ReviewEvidenceItem]:
        """
        Extracts review count, rating, date, and source provenance from a snippet.
        Applies rigorous business identity matching and branch isolation.
        """
        if not snippet and not title:
            return None

        combined_text = f"{title} — {snippet}".strip()

        # ── Gate A: Reject generic city roundup pages / listicles ──
        for pat in LISTICLE_PATTERNS:
            if pat.search(title) or pat.search(snippet):
                # Only allow if title explicitly starts with or focuses on the exact target business
                clean_biz = re.sub(r'[^a-z0-9]', '', business_name.lower())
                clean_title = re.sub(r'[^a-z0-9]', '', title.lower())
                if not clean_title.startswith(clean_biz):
                    return ReviewEvidenceItem(
                        reject_reason="GENERIC_LISTICLE_OR_ROUNDUP_PAGE",
                        evidence_text=combined_text[:120]
                    )

        # ── Gate B: Business Identity Verification ──
        def _clean_text(s: str) -> str:
            # Strip quotes and apostrophes completely so "Shezzaan's" -> "shezzaans"
            t = re.sub(r"['’`\"]", "", s.lower())
            return re.sub(r'[^a-z0-9\s]', ' ', t)

        norm_bname = _clean_text(business_name).strip()
        norm_combined = _clean_text(combined_text)
        bname_tokens = norm_bname.split()
        if not bname_tokens:
            return None

        norm_title = _clean_text(title)
        name_in_title = norm_bname in norm_title
        name_in_snippet = norm_bname in norm_combined

        if not name_in_title and not name_in_snippet:
            # Check if all major tokens (or stem without trailing 's') appear
            all_tokens_present = all(
                (tok in norm_combined or (tok.endswith('s') and tok[:-1] in norm_combined))
                for tok in bname_tokens if len(tok) > 2
            )
            if not all_tokens_present:
                return ReviewEvidenceItem(
                    reject_reason="MISSING_BUSINESS_IDENTITY",
                    evidence_text=combined_text[:120]
                )

        # ── Gate C: Location & Wrong-Branch Isolation ──
        target_city_norm = (city or "").lower().strip()
        target_pc_norm = (postcode or "").lower().strip()
        pc_prefix = target_pc_norm.split()[0] if target_pc_norm else ""

        from lib.country_adapters import get_country_adapter
        adapter = get_country_adapter(country if country else "United Kingdom")
        if hasattr(adapter.review_sources, "evaluate_location_match"):
            is_loc_match, loc_reject_reason = adapter.review_sources.evaluate_location_match(
                title=title,
                snippet=snippet,
                target_city=city,
                street=street,
                postcode=postcode,
                country=country
            )
            if not is_loc_match:
                return ReviewEvidenceItem(
                    reject_reason=loc_reject_reason or f"LOCATION_MISMATCH ({city.title()})",
                    evidence_text=combined_text[:120]
                )
        else:
            major_cities = adapter.review_sources.get_major_cities() if hasattr(adapter.review_sources, "get_major_cities") else MAJOR_UK_CITIES
            for major_city in major_cities:
                if major_city in norm_combined and major_city != target_city_norm:
                    has_target_location = (
                        target_city_norm in norm_combined or
                        (pc_prefix and pc_prefix in norm_combined) or
                        (street and street.lower() in norm_combined)
                    )
                    if not has_target_location:
                        return ReviewEvidenceItem(
                            reject_reason=f"WRONG_BRANCH_LOCATION ({major_city.title()})",
                            evidence_text=combined_text[:120]
                        )

        # ── Gate D: Extract Rating ──
        rating_val: Optional[float] = None
        # Support decimal dot or comma (4.6 or 4,6), /5, stars, out of 5
        rat_pats = [
            r"(?:rating|rated|score|stars?)[:\s]*([1-5](?:[.,]\d)?)\s*(?:out of 5|/5|★|stars)?",
            r"\b([1-5][.,]\d)\s*(?:out of 5|/5|★|stars?)",
            r"\b([1-5][.,]\d)\s*[·•\-]\s*‎?\d+",
            r"\b([1-5][.,]\d)\s*\(\s*\d+",
            r"\b([1-5](?:[.,]\d)?)\s*(?:out of 5|/5|★|stars)\b",
        ]
        for pat in rat_pats:
            m = re.search(pat, combined_text, re.IGNORECASE)
            if m:
                raw_rat = m.group(1).replace(",", ".")
                try:
                    r = float(raw_rat)
                    if 1.0 <= r <= 5.0:
                        rating_val = r
                        break
                except ValueError:
                    pass

        # ── Gate E: Extract Review Count ──
        review_count_val: Optional[int] = None
        rev_pats = [
            r"(?:from|based on|with|over|accumulating|see)?\s*(\d{1,3}(?:[,\s]\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)\s*(?:(?:customer|user|diner|guest|google|tripadvisor|visitor|verified|unbiased)\s+)?(?:reviews?|ratings?|votes?)",
            r"(?:reviews?|ratings?)[:\s]*(\d{1,3}(?:[,\s]\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)",
            r"[·•\-(]\s*‎?(\d{1,3}(?:[,\s]\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)\s*(?:reviews?|votes?)\s*\)?",
            r"based on\s+(\d{1,3}(?:[,\s]\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)",
        ]

        # Check for unrelated entity in snippet preceding review count
        landmark_pattern = re.search(r'\b(?:near|next to|opposite|adjacent to|behind|close to)\s+' + re.escape(norm_bname), norm_combined)
        if landmark_pattern:
            m_entity = re.search(r'([A-Z][A-Za-z0-9\s&\'’]+?)\s*[·(]\s*(\d+[\d,]*)\s+reviews?', combined_text)
            if m_entity:
                cand_ent = m_entity.group(1).strip().lower()
                cand_ent = re.sub(r'^(?:located|visit the|visit|see the|see|explore)\s+', '', cand_ent).strip()
                if norm_bname not in cand_ent:
                    return ReviewEvidenceItem(
                        reject_reason="UNRELATED_ENTITY_REVIEW_COUNT",
                        evidence_text=combined_text[:120]
                    )

        unrelated_pattern = re.search(r'(?:near|next to|opposite|adjacent to)\s+([A-Za-z\s]+?)\s*[·(]?\s*(\d+[\d,]*)\s+reviews?', combined_text, re.IGNORECASE)
        if unrelated_pattern:
            unrelated_name = unrelated_pattern.group(1).strip().lower()
            if norm_bname not in unrelated_name and unrelated_name not in norm_bname:
                return ReviewEvidenceItem(
                    reject_reason="UNRELATED_ENTITY_REVIEW_COUNT",
                    evidence_text=combined_text[:120]
                )

        # Ignore directory SEO title phrases where year is placed right before reviews (e.g. '2026 Reviews & Info / Information / Photos / Deals')
        text_for_rev_count = re.sub(
            r'\b(19\d{2}|20\d{2})\s+(?:restaurant\s+)?reviews?\s*(?:&|and|,|\/|\+|:|–|—|-)\s*(?:info|information|photos?|prices?|ratings?|deals?|menu|guide|details?|overview|opinions?|booking|dishes)\b',
            r'year \1',
            combined_text,
            flags=re.IGNORECASE
        )

        for pat in rev_pats:
            m = re.search(pat, text_for_rev_count, re.IGNORECASE)
            if m:
                raw_cnt = m.group(1).strip()
                clean_cnt = re.sub(r"[,\.\s]", "", raw_cnt)
                try:
                    cnt = int(clean_cnt)
                    if cnt > 0:
                        review_count_val = cnt
                        break
                except ValueError:
                    pass

        if review_count_val is None and rating_val is None:
            return None

        # ── Gate F: Source Provenance Detection ──
        url_lower = url.lower()
        if "tripadvisor" in url_lower:
            source = "Tripadvisor"
        elif "google" in url_lower:
            source = "Google"
        elif "facebook" in url_lower:
            source = "Facebook"
        elif "restaurantguru" in url_lower:
            source = "Restaurant Guru"
        elif "yelp" in url_lower:
            source = "Yelp"
        elif "opentable" in url_lower:
            source = "OpenTable"
        elif "trustpilot" in url_lower:
            source = "Trustpilot"
        elif "deliveroo" in url_lower:
            source = "Deliveroo"
        elif "just-eat" in url_lower:
            source = "Just Eat"
        else:
            source = "Web"

        # ── Gate G: Date & Freshness Parsing (Phase 5.2 Date Provenance) ──
        date_ev = ReviewDateExtractor.extract_from_snippet(
            text=snippet,
            title=title,
            source_url=url,
            as_of=REFERENCE_DATE
        )
        ev_date = date_ev.date
        freshness = date_ev.freshness
        ev_date_raw = date_ev.date_raw
        ev_date_type = date_ev.date_type
        ev_date_conf = date_ev.confidence
        ev_as_of = date_ev.as_of
        ev_method = date_ev.extraction_method

        # ── Gate H: Match Tier & Confidence Assignment ──
        from lib.discovery.geo_provider import GeoProvider
        target_aliases = [a.lower() for a in GeoProvider.get_locality_aliases(target_city_norm, country or "United Kingdom")]
        has_local_corroboration = (
            target_city_norm in norm_combined or
            (pc_prefix and pc_prefix in norm_combined) or
            (street and street.lower() in norm_combined) or
            any(alias in norm_combined for alias in target_aliases if len(alias) >= 3)
        )

        if name_in_title and has_local_corroboration:
            match_tier = "EXACT_LOCATION"
        elif name_in_snippet and has_local_corroboration:
            match_tier = "NAME_AND_CITY"
        else:
            match_tier = "PARTIAL"

        # Assign confidence
        is_known_platform = source in ["Tripadvisor", "Google", "Facebook", "Restaurant Guru", "Yelp"]
        if match_tier in ["EXACT_LOCATION", "NAME_AND_CITY"] and is_known_platform and review_count_val is not None and rating_val is not None:
            if freshness == ReviewFreshness.STALE.value:
                confidence = ReviewConfidence.LOW.value
            else:
                confidence = ReviewConfidence.HIGH.value
        elif match_tier in ["EXACT_LOCATION", "NAME_AND_CITY"] and (review_count_val is not None or rating_val is not None):
            if freshness == ReviewFreshness.STALE.value:
                confidence = ReviewConfidence.LOW.value
            else:
                confidence = ReviewConfidence.MEDIUM.value
        else:
            confidence = ReviewConfidence.LOW.value

        return ReviewEvidenceItem(
            review_count=review_count_val,
            rating=rating_val,
            source=source,
            source_url=url,
            evidence_text=combined_text[:280],
            evidence_date=ev_date,
            confidence=confidence,
            freshness=freshness,
            business_match_tier=match_tier,
            review_provider=provider,
            evidence_date_raw=ev_date_raw,
            evidence_date_type=ev_date_type,
            evidence_date_confidence=ev_date_conf,
            evidence_as_of=ev_as_of,
            extraction_method=ev_method
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 3. DATE AND FRESHNESS PARSING
    # ──────────────────────────────────────────────────────────────────────────
    @classmethod
    def parse_date_and_freshness(
        cls,
        text: str,
        as_of: Optional[Union[datetime, str]] = None
    ) -> Tuple[Optional[str], str]:
        """
        Parses dates from snippet text and calculates freshness relative to 2026-10-01 (or as_of).
        Never invents dates. Returns (None, "UNKNOWN") if no trustworthy date found.
        """
        date_ev = ReviewDateExtractor.extract_from_snippet(text, as_of=as_of or REFERENCE_DATE)
        return date_ev.date, date_ev.freshness

    # ──────────────────────────────────────────────────────────────────────────
    # 4. CONFLICT DETECTION & RESOLUTION
    # ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def detect_conflicts(items: List[ReviewEvidenceItem]) -> Tuple[bool, List[ReviewEvidenceItem]]:
        """
        Detects if sources materially disagree on review counts or star ratings.
        Conditions:
          - Rating disparity >= 0.5 stars across verified items
          - Review count disparity > 2.0x ratio AND absolute difference >= 100
        """
        valid_items = [it for it in items if not it.reject_reason and (it.review_count is not None or it.rating is not None)]
        if len(valid_items) < 2:
            return False, []

        ratings = [it.rating for it in valid_items if it.rating is not None]
        counts = [it.review_count for it in valid_items if it.review_count is not None]

        # Check rating disagreement
        if len(ratings) >= 2:
            max_r = max(ratings)
            min_r = min(ratings)
            if (max_r - min_r) >= 0.5:
                return True, valid_items

        # Check review count disagreement (e.g. 300 vs 920)
        if len(counts) >= 2:
            sorted_counts = sorted(counts)
            min_c = sorted_counts[0]
            max_c = sorted_counts[-1]
            if min_c > 0 and (max_c / min_c) > 2.0 and (max_c - min_c) >= 100:
                return True, valid_items

        return False, []

    # ──────────────────────────────────────────────────────────────────────────
    # 5. BEST EVIDENCE SELECTION
    # ──────────────────────────────────────────────────────────────────────────
    @classmethod
    def resolve_best_evidence(
        cls,
        items: List[ReviewEvidenceItem],
        search_failed: bool = False,
        search_outcome: Optional[str] = None
    ) -> ReviewEnrichmentResult:
        """
        Resolves the best evidence item without overwriting stronger verified values with weaker ones.
        """
        valid_items = [it for it in items if not it.reject_reason]
        if not valid_items:
            if search_failed:
                return ReviewEnrichmentResult(
                    review_status=ReviewStatus.SEARCH_FAILED.value,
                    review_confidence=ReviewConfidence.UNKNOWN.value,
                    review_freshness=ReviewFreshness.UNKNOWN.value,
                    review_evidence=f"Search provider failure ({search_outcome or 'SEARCH_FAILED'}). Could not search for review evidence.",
                    all_evidence=[it.to_dict() for it in items]
                )
            return ReviewEnrichmentResult(
                review_status=ReviewStatus.NOT_FOUND.value,
                review_confidence=ReviewConfidence.UNKNOWN.value,
                review_freshness=ReviewFreshness.UNKNOWN.value,
                all_evidence=[it.to_dict() for it in items]
            )

        # Check for conflicts across sources
        has_conflict, conflicting_items = cls.detect_conflicts(valid_items)
        if has_conflict:
            return ReviewEnrichmentResult(
                review_count=None,
                rating=None,
                review_source="Multiple (Conflicting)",
                review_evidence="Conflicting review data detected across multiple public sources.",
                review_confidence=ReviewConfidence.CONFLICT.value,
                review_freshness=ReviewFreshness.UNKNOWN.value,
                review_status=ReviewStatus.CONFLICT_REQUIRES_REVIEW.value,
                conflicts=[it.to_dict() for it in conflicting_items],
                all_evidence=[it.to_dict() for it in items]
            )

        # Priority ranking:
        # Tier 1: HIGH confidence, RECENT
        # Tier 2: HIGH confidence, UNKNOWN freshness
        # Tier 3: MEDIUM confidence, RECENT
        # Tier 4: MEDIUM confidence, UNKNOWN freshness
        # Tier 5: LOW confidence
        # Tier 6: STALE evidence
        def score_item(it: ReviewEvidenceItem) -> int:
            score = 0
            if it.confidence == ReviewConfidence.HIGH.value:
                score += 100
            elif it.confidence == ReviewConfidence.MEDIUM.value:
                score += 50
            elif it.confidence == ReviewConfidence.LOW.value:
                score += 10

            if it.freshness == ReviewFreshness.RECENT.value:
                score += 30
            elif it.freshness == ReviewFreshness.UNKNOWN.value:
                score += 10
            elif it.freshness == ReviewFreshness.STALE.value:
                score -= 40

            if it.review_count is not None and it.rating is not None:
                score += 20
            elif it.review_count is not None:
                score += 10

            return score

        best = max(valid_items, key=score_item)

        return ReviewEnrichmentResult(
            review_count=best.review_count,
            rating=best.rating,
            review_source=best.source,
            review_source_url=best.source_url,
            review_evidence=best.evidence_text,
            review_evidence_date=best.evidence_date,
            review_confidence=best.confidence,
            review_freshness=best.freshness,
            review_status=ReviewStatus.FOUND.value,
            review_provider=best.review_provider,
            review_evidence_date_raw=best.evidence_date_raw,
            review_evidence_date_type=best.evidence_date_type,
            review_evidence_date_confidence=best.evidence_date_confidence,
            review_evidence_as_of=best.evidence_as_of,
            extraction_method=best.extraction_method,
            all_evidence=[it.to_dict() for it in items]
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 6. BUSINESS ENRICHMENT PIPELINE ENTRYPOINT
    # ──────────────────────────────────────────────────────────────────────────
    def enrich_business(
        self,
        business_name: str,
        city: str,
        street: str = "",
        postcode: str = "",
        category: str = "",
        phone: str = "",
        website: str = "",
        country: str = "United Kingdom",
        region: str = "",
        search_results: Optional[List[Dict[str, Any]]] = None,
        enable_page_fetch: bool = False
    ) -> ReviewEnrichmentResult:
        """
        Executes free-first review and rating enrichment for a single business.
        Returns SOURCE_NOT_CONFIGURED if no review source is configured for this market.
        """
        from lib.country_adapters import get_country_adapter, AdapterStatus
        adapter = get_country_adapter(country if country else "United Kingdom")
        if not adapter or getattr(adapter.review_sources, "status", "") == AdapterStatus.NOT_CONFIGURED.value:
            return ReviewEnrichmentResult(
                review_count=None,
                rating=None,
                review_source="SOURCE_NOT_CONFIGURED",
                review_source_url="",
                review_evidence="Review source not configured for this market.",
                review_confidence=ReviewConfidence.UNKNOWN.value,
                review_freshness=ReviewFreshness.UNKNOWN.value,
                review_status="SOURCE_NOT_CONFIGURED",
                review_evidence_date_type=ReviewEvidenceDateType.UNKNOWN.value,
                review_evidence_date_confidence=ReviewConfidence.UNKNOWN.value,
                extraction_method="NONE"
            )

        evidence_items: List[ReviewEvidenceItem] = []
        search_failed = False
        search_outcome = None

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
            "QUOTA_EXCEEDED",
        }

        # If pre-supplied search results (e.g. from tests or prior web search)
        if search_results is not None:
            results_to_process = search_results
            out_val = getattr(search_results, "outcome", None)
            out_name = out_val.value if hasattr(out_val, "value") else str(out_val or "")
            if out_name in search_failed_states:
                search_failed = True
                search_outcome = out_name
        else:
            # Generate targeted queries and query free-tier search provider
            if hasattr(adapter.review_sources, "get_search_queries"):
                queries = adapter.review_sources.get_search_queries(business_name, city, street, postcode)
            else:
                queries = self.generate_queries(business_name, city, street, postcode)
            results_to_process = []
            attempted_outcomes = []
            for q in queries[:2]:  # Query top targeted searches
                res = self.web.search_web(q, num_results=3)
                out_val = getattr(res, "outcome", None)
                out_name = out_val.value if hasattr(out_val, "value") else str(out_val or "")
                attempted_outcomes.append(out_name)
                results_to_process.extend(res)
                if len(res) >= 3:
                    break

            # If all attempted queries failed
            if attempted_outcomes and all(o in search_failed_states for o in attempted_outcomes):
                search_failed = True
                search_outcome = attempted_outcomes[-1]

        # Process each result snippet
        for r in results_to_process:
            snippet = r.get("snippet", "")
            title = r.get("title", "")
            url = r.get("result_url", "")

            ev = self.extract_evidence_from_snippet(
                snippet=snippet,
                title=title,
                url=url,
                business_name=business_name,
                city=city,
                street=street,
                postcode=postcode,
                country=country,
                provider=r.get("provider") or r.get("search_provider", "")
            )
            if ev:
                # If valid candidate evidence was found without date, attempt page extraction if enabled
                if enable_page_fetch and ev.freshness == ReviewFreshness.UNKNOWN.value and ev.source_url and not ev.reject_reason:
                    page_res = ReviewDateExtractor.fetch_and_extract(
                        ev.source_url,
                        as_of=REFERENCE_DATE,
                        cache_dir=os.path.join(self.cache_dir, "page_dates")
                    )
                    if page_res and page_res.date and page_res.freshness != ReviewFreshness.UNKNOWN.value:
                        ev.evidence_date = page_res.date
                        ev.freshness = page_res.freshness
                        ev.evidence_date_raw = page_res.date_raw
                        ev.evidence_date_type = page_res.date_type
                        ev.evidence_date_confidence = page_res.confidence
                        ev.evidence_as_of = page_res.as_of
                        ev.extraction_method = page_res.extraction_method

                evidence_items.append(ev)

        return self.resolve_best_evidence(
            evidence_items,
            search_failed=search_failed,
            search_outcome=search_outcome
        )

    def enrich_candidate(self, candidate: DiscoveredBusiness) -> DiscoveredBusiness:
        """
        Enriches a DiscoveredBusiness candidate object in-place before Qualification V3.
        Does not overwrite an already stronger verified value with weaker evidence.
        """
        c_country = getattr(candidate, "country", None) or getattr(candidate, "target_country", None) or "United Kingdom"
        c_region = getattr(candidate, "region", "") or ""
        res = self.enrich_business(
            business_name=candidate.company_name,
            city=candidate.city,
            street=candidate.street,
            postcode=candidate.postcode,
            category=candidate.category or candidate.amenity or "restaurant",
            phone=candidate.phone,
            website=candidate.raw_website,
            country=c_country,
            region=c_region
        )

        # Store full enrichment diagnostics in raw_data
        if not hasattr(candidate, "raw_data") or not isinstance(candidate.raw_data, dict):
            candidate.raw_data = {}
        candidate.raw_data["review_enrichment"] = res.to_dict()

        if res.review_status == ReviewStatus.FOUND.value:
            # Only update if candidate currently lacks review data OR if new evidence is HIGH confidence
            if candidate.review_count is None and res.review_count is not None:
                candidate.review_count = res.review_count
            elif res.review_confidence == ReviewConfidence.HIGH.value and res.review_count is not None:
                candidate.review_count = res.review_count

            if candidate.rating is None and res.rating is not None:
                candidate.rating = res.rating
            elif res.review_confidence == ReviewConfidence.HIGH.value and res.rating is not None:
                candidate.rating = res.rating

            if res.review_evidence_date:
                candidate.latest_review_date = res.review_evidence_date

        return candidate
