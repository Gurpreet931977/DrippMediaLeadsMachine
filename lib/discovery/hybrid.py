"""
Hybrid & Free-First Discovery Engine
Orchestrates the entire discovery stack:
- OpenStreetMap (Primary free place discovery)
- Foursquare Places API (Optional secondary source)
- WebSearchProvider (Tavily -> Brave -> SearXNG -> DDG Lite)
- CrawlEngine (Local Crawl4AI / Playwright page extraction)
- ApifyProvider (Optional legacy provider, gated by APIFY_ENABLED)

Handles:
- Deduplication and canonical identity merging
- Evidence source tracking & source confidence classification
- Strict budget limits (preventing runaway usage)
- Discovery transparency metrics
- Seamless integration with existing Qualification V3 pipeline
"""
import os
import re
import time
import logging
from urllib.parse import urlparse
from typing import List, Dict, Any, Optional, Tuple
from dotenv import load_dotenv

from lib.discovery.base import DiscoveryProvider, DiscoveryMetrics
from lib.discovery.osm import OpenStreetMapProvider, OverpassQueryError
from lib.discovery.foursquare import FoursquareProvider
from lib.discovery.web_search import WebSearchProvider
from lib.discovery.crawler import CrawlEngine
from lib.types import (
    DiscoveredBusiness,
    SocialStatus,
    CountryStatus,
    CreatorEvidenceStatus,
    CreatorEvidenceConfidence,
)
from lib.validation.country_validator import CountryValidator
from lib.validation.creator_evidence import CreatorEvidenceValidator
from lib.validation.social_validator import SocialIdentityValidator

logger = logging.getLogger(__name__)

load_dotenv()

class DiscoveryMode:
    FREE_LOCAL = "FREE_LOCAL"
    HYBRID = "HYBRID"
    APIFY = "APIFY"

class HybridDiscoveryEngine(DiscoveryProvider):
    def __init__(
        self,
        mode: Optional[str] = None,
        apify_enabled: Optional[bool] = None,
        max_osm_queries: int = 6,
        max_foursquare_calls: int = 10,
        max_web_searches: int = 20,
        max_crawls_per_business: int = 5,
        max_pages_per_business: int = 10,
        max_creator_searches: int = 15
    ):
        super().__init__(name="HybridDiscoveryEngine")
        # Discovery mode configuration (env priority)
        env_mode = os.getenv("DISCOVERY_PROVIDER", "HYBRID").strip().upper()
        self.mode = mode or env_mode
        if self.mode not in [DiscoveryMode.FREE_LOCAL, DiscoveryMode.HYBRID, DiscoveryMode.APIFY]:
            self.mode = DiscoveryMode.HYBRID

        # Apify explicit enablement check
        env_apify_en = os.getenv("APIFY_ENABLED", "false").lower() in ["true", "1", "yes"]
        self.apify_enabled = apify_enabled if apify_enabled is not None else env_apify_en

        # Initialize sub-providers
        self.osm = OpenStreetMapProvider()
        self.foursquare = FoursquareProvider()
        self.web_search = WebSearchProvider()
        self.crawler = CrawlEngine(max_pages_per_business=max_pages_per_business)
        self.apify = None
        if self.apify_enabled and self.mode in [DiscoveryMode.HYBRID, DiscoveryMode.APIFY]:
            try:
                from lib.discovery.apify import ApifyDiscoveryProvider
                self.apify = ApifyDiscoveryProvider(enabled=True)
            except Exception:
                self.apify = None
        self.country_validator = CountryValidator()

        # Budgets
        self.budget = {
            "max_osm_queries": max_osm_queries,
            "max_foursquare_calls": max_foursquare_calls,
            "max_web_searches": max_web_searches,
            "max_crawls_per_business": max_crawls_per_business,
            "max_pages_per_business": max_pages_per_business,
            "max_creator_searches": max_creator_searches
        }

        # Discovery Transparency Counters (Part 9)
        self.transparency_stats = {
            "businesses_discovered_total": 0,
            "unique_candidates": 0,
            "duplicates_merged": 0,
            "by_source": {
                "OPENSTREETMAP": 0,
                "FOURSQUARE": 0,
                "WEB_SEARCH": 0,
                "CREATOR_REFERENCES": 0,
                "APIFY": 0
            },
            "creator_discovery": {
                "references_found": 0,
                "valid_references": 0,
                "high_confidence": 0,
                "medium_confidence": 0,
                "low_confidence": 0,
                "candidate_official_handles": 0,
                "independently_verified_official_handles": 0
            },
            "boundary": {
                "polygon_loaded": True,
                "polygon_source": "OpenStreetMap Relation 162378 (Manchester, England, UK)",
                "candidates_inside": 0,
                "candidates_outside": 0,
                "diverted_to_adjacent_research": 0
            }
        }

    def health_check(self) -> Dict[str, Any]:
        """Returns consolidated health and readiness for all providers (Part 9)."""
        osm_h = self.osm.health_check()
        fsq_h = self.foursquare.health_check()
        web_h = self.web_search.health_check()
        crawl_h = self.crawler.health_check()
        apify_h = self.apify.health_check() if self.apify else {
            "provider": "Apify",
            "status": "EXHAUSTED / DISABLED",
            "enabled": False,
            "reason": "Apify provider is disabled / credits exhausted"
        }

        # Update dynamic boundary state for Manchester, UK (#162378)
        b_feat = self.osm.boundary_validator.get_or_fetch_boundary("Manchester", "United Kingdom")
        self.transparency_stats["boundary"] = {
            "polygon_loaded": bool(b_feat),
            "polygon_source": b_feat.get("properties", {}).get("source", "OpenStreetMap Relation 162378 (Manchester, UK)") if b_feat else "None",
            "candidates_inside": self.transparency_stats["businesses_discovered_total"],
            "candidates_outside": len(self.osm.adjacent_research_candidates),
            "diverted_to_adjacent_research": len(self.osm.adjacent_research_candidates)
        }

        return {
            "discovery_mode": self.mode,
            "apify_enabled": self.apify_enabled,
            "providers": {
                "OpenStreetMap": osm_h.get("status", "NOT_CONNECTED"),
                "Foursquare": fsq_h.get("status", "NOT_CONNECTED"),
                "Tavily": web_h.get("engines", {}).get("tavily", "NOT_CONNECTED"),
                "Brave": web_h.get("engines", {}).get("brave", "NOT_CONNECTED"),
                "SearXNG": web_h.get("engines", {}).get("searxng", "NOT_CONNECTED"),
                "DuckDuckGo": web_h.get("engines", {}).get("duckduckgo", "FALLBACK"),
                "Crawl4AI": crawl_h.get("status", "NOT_READY"),
                "Apify": "EXHAUSTED / DISABLED" if not self.apify_enabled else "EXHAUSTED",
                "GooglePlaces": "DISALLOWED (Architecture Constraint)"
            },
            "provider_notes": {
                "OpenStreetMap": "Primary verified geographic provider. Zero API cost Overpass polygon harvest.",
                "GooglePlaces": "Strictly disallowed by system constraints.",
                "Apify": "Credits exhausted. Disabled in production configuration.",
                "Tavily": "Web verification search engine subject to active quota."
            },
            "search_provider_stats": web_h.get("stats_by_provider", {}),
            "creator_discovery": self.transparency_stats["creator_discovery"],
            "boundary": self.transparency_stats["boundary"],
            "transparency": self.transparency_stats
        }

    def _canonical_identity(self, name: str, city: str, country: str) -> str:
        clean_name = re.sub(r'[^a-z0-9]', '', (name or "").lower())
        clean_city = re.sub(r'[^a-z0-9]', '', (city or "").lower())
        clean_country = re.sub(r'[^a-z0-9]', '', (country or "").lower())
        return f"{clean_name}@{clean_city}@{clean_country}"

    def merge_candidates(self, base: DiscoveredBusiness, secondary: DiscoveredBusiness) -> DiscoveredBusiness:
        """
        Merges information from two sources for the same canonical business into ONE record.
        Maintains source tracking in evidence_sources.
        """
        # If secondary has phone and base doesn't
        if not base.phone and secondary.phone:
            base.phone = secondary.phone
            base.evidence_sources["phone"] = secondary.discovery_source

        # If secondary has website and base doesn't
        if not base.raw_website and secondary.raw_website:
            base.raw_website = secondary.raw_website
            base.evidence_sources["website"] = secondary.discovery_source

        # Merge socials
        if not base.instagram_url and secondary.instagram_url:
            base.instagram_url = secondary.instagram_url
            base.evidence_sources["instagram"] = secondary.discovery_source
            base.social_status = SocialStatus.SOCIAL_FOUND.value

        if not base.facebook_url and secondary.facebook_url:
            base.facebook_url = secondary.facebook_url
            base.evidence_sources["facebook"] = secondary.discovery_source
            base.social_status = SocialStatus.SOCIAL_FOUND.value

        if not base.tiktok_url and secondary.tiktok_url:
            base.tiktok_url = secondary.tiktok_url
            base.evidence_sources["tiktok"] = secondary.discovery_source
            base.social_status = SocialStatus.SOCIAL_FOUND.value

        # Postcode & street
        if not base.postcode and secondary.postcode:
            base.postcode = secondary.postcode
        if not base.street and secondary.street:
            base.street = secondary.street

        # City boundary validation
        if not base.city_match and secondary.city_match:
            base.city_match = secondary.city_match
            base.city_match_reason = secondary.city_match_reason
            base.boundary_source = getattr(secondary, "boundary_source", "")
            base.boundary_validation_method = getattr(secondary, "boundary_validation_method", "")
        elif not getattr(base, "boundary_source", "") and getattr(secondary, "boundary_source", ""):
            base.boundary_source = secondary.boundary_source
            base.boundary_validation_method = getattr(secondary, "boundary_validation_method", "")

        # Coordinates
        if base.lat is None and secondary.lat is not None:
            base.lat = secondary.lat
            base.lon = secondary.lon

        # Hours
        if not base.opening_hours and secondary.opening_hours:
            base.opening_hours = secondary.opening_hours

        # Reviews & rating if available from secondary (e.g. Foursquare or Apify)
        if base.review_count is None and secondary.review_count is not None:
            base.review_count = secondary.review_count
        if base.rating is None and secondary.rating is not None:
            base.rating = secondary.rating

        return base

    def enrich_candidate_creator_evidence(
        self,
        candidate: DiscoveredBusiness,
        city: str,
        country: str = "United Kingdom"
    ) -> Optional[Any]:
        """
        Executes targeted creator search and evidence evaluation for a candidate place (Sections 4-10).
        Strict rules enforced:
        - Only searches if priority condition met (missing social, unverified social, or weak operational evidence).
        - Third-party creator content is NEVER proof of official business ownership.
        - If candidate handle found, independently validates it.
        - Creator evidence alone NEVER automatically creates ACTIVE_CONFIRMED.
        """
        biz_dict = {
            "company_name": candidate.company_name,
            "city": candidate.city or city,
            "category": candidate.category or candidate.amenity or "Restaurant",
            "address": candidate.address or "",
            "street": candidate.street or "",
            "instagram_url": candidate.instagram_url or "",
            "facebook_url": candidate.facebook_url or "",
            "social_status": candidate.social_status,
            "social_ownership_status": getattr(candidate, "social_ownership_status", "UNKNOWN"),
            "social_profile_status": getattr(candidate, "social_profile_status", "UNKNOWN"),
            "operational_status": candidate.operational_status
        }

        should_search, reason = CreatorEvidenceValidator.should_search_creator_evidence(biz_dict)
        if not should_search:
            return None

        # Search third-party creator content across platforms without requiring @tags
        search_items = self.web_search.search_social_references(
            business_name=candidate.company_name,
            city=candidate.city or city,
            country=country,
            neighborhood="",
            street=candidate.street,
            category=candidate.category or "Restaurant",
            limit_queries=3
        )

        if not search_items:
            return None

        evidence_items = []
        for item in search_items:
            post_data = dict(item) if isinstance(item, dict) else {}
            if not post_data.get("caption") and post_data.get("snippet"):
                post_data["caption"] = post_data["snippet"]
            if not post_data.get("content_url") and post_data.get("result_url"):
                post_data["content_url"] = post_data["result_url"]
            ev_item = CreatorEvidenceValidator.evaluate_content_item(post_data or item, biz_dict)
            evidence_items.append(ev_item)

        summary = CreatorEvidenceValidator.summarize_evidence(evidence_items, biz_dict)
        if summary.creator_evidence_count > 0:
            self.transparency_stats["by_source"]["CREATOR_REFERENCES"] += summary.creator_evidence_count
            c_stats = self.transparency_stats["creator_discovery"]
            c_stats["references_found"] += summary.creator_evidence_count
            for it in summary.items:
                if it.creator_evidence_status == CreatorEvidenceStatus.FOUND.value:
                    c_stats["valid_references"] += 1
                    if it.evidence_confidence == CreatorEvidenceConfidence.HIGH.value:
                        c_stats["high_confidence"] += 1
                    elif it.evidence_confidence == CreatorEvidenceConfidence.MEDIUM.value:
                        c_stats["medium_confidence"] += 1
                    elif it.evidence_confidence == CreatorEvidenceConfidence.LOW.value:
                        c_stats["low_confidence"] += 1

            candidate.raw_data["creator_evidence"] = summary.to_dict()

            # Check if any candidate official handle was discovered from creator post
            if summary.discovered_official_handles:
                c_stats["candidate_official_handles"] += len(summary.discovered_official_handles)
                for disc in summary.discovered_official_handles:
                    cand_handle = disc.get("candidate_handle")
                    is_verified = (
                        disc.get("validation_status") == "VERIFIED"
                        or disc.get("social_ownership_status") == "VERIFIED"
                    )
                    v_url = disc.get("verified_url") or disc.get("profile_url")
                    if is_verified and v_url:
                        if not candidate.instagram_url and "instagram" in v_url.lower():
                            candidate.instagram_url = v_url
                        elif not candidate.facebook_url and "facebook" in v_url.lower():
                            candidate.facebook_url = v_url
                        candidate.social_status = SocialStatus.SOCIAL_FOUND.value
                        candidate.raw_data["candidate_official_account"] = cand_handle
                        candidate.raw_data["social_ownership_status"] = "VERIFIED"
                        c_stats["independently_verified_official_handles"] += 1
                    else:
                        candidate.raw_data["candidate_official_account"] = cand_handle
                        candidate.raw_data["social_ownership_status"] = "UNVERIFIED"

        return summary

    def discover_from_web_search(
        self,
        city: str,
        country: str,
        industry: str,
        limit: int = 40
    ) -> List[DiscoveredBusiness]:
        """
        Independent web-search discovery fallback when primary providers fail.
        Uses WebSearchProvider.discover_businesses to surface candidate businesses.
        """
        return self.web_search.discover_businesses(
            city=city,
            country=country,
            industry=industry,
            limit=limit
        )

    def search_businesses(
        self,
        city: str,
        country: str,
        industry: str,
        limit: int = 40,
        **kwargs
    ) -> List[DiscoveredBusiness]:
        """
        Discovers candidates according to the configured mode:
        FREE_LOCAL: OSM + Foursquare (if configured)
        HYBRID: OSM + Foursquare + WebSearch (Apify only if explicitly enabled)
        APIFY: Apify only (if explicitly enabled)
        """
        candidates, _ = self.discover_round(
            round_number=1,
            country=country,
            cities=[city],
            industry=industry,
            limit=limit
        )
        return candidates

    def get_query_rounds(self, city: str, country: str, industry: str) -> List[List[str]]:
        """Returns discovery rounds across available query families."""
        return [
            [f"Web 'independent {industry.lower()}' in {city}",
             f"Web 'family-run {industry.lower()}' {city}"],
            [f"OSM {industry} in {city}, {country}",
             f"OSM bistros in {city}, {country}"],
            [f"OSM cafes in {city}, {country}",
             f"OSM pubs & bars in {city}, {country}"],
            [f"OSM fast food & takeaways in {city}, {country}"]
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
        """
        Executes a discovery round with priority to free providers.
        Merges duplicates canonically and updates transparency stats.
        """
        city = cities[0] if cities else "Manchester"
        rounds = self.get_query_rounds(city, country, industry)
        round_idx = round_number - 1
        queries_used = rounds[round_idx] if round_idx < len(rounds) else [f"Discovery {industry} in {city}"]

        pool: Dict[str, DiscoveredBusiness] = {}
        duplicates_in_round = 0

        # Mode APIFY (Explicitly enabled only)
        if self.mode == DiscoveryMode.APIFY:
            if not self.apify_enabled or not self.apify:
                print("[HybridDiscovery] Apify mode requested but Apify is unavailable or disabled. Falling back to FREE_LOCAL.")
            else:
                apify_candidates, ap_queries = self.apify.discover_round(round_number, country, cities, industry, limit)
                for cand in apify_candidates:
                    self.transparency_stats["businesses_discovered_total"] += 1
                    self.transparency_stats["by_source"]["APIFY"] += 1
                    canon_id = self._canonical_identity(cand.company_name, cand.city, cand.target_country)
                    if canon_id in pool:
                        duplicates_in_round += 1
                        pool[canon_id] = self.merge_candidates(pool[canon_id], cand)
                    else:
                        pool[canon_id] = cand
                self.transparency_stats["unique_candidates"] = len(pool)
                self.transparency_stats["duplicates_merged"] += duplicates_in_round
                return list(pool.values()), ap_queries

        # Free-First Stack:
        # Step 1: OpenStreetMap (Primary Discovery)
        osm_error = None
        osm_candidates = []
        try:
            osm_candidates = self.osm.search_businesses(city=city, country=country, industry=industry, limit=limit)
            for cand in osm_candidates:
                self.transparency_stats["businesses_discovered_total"] += 1
                self.transparency_stats["by_source"]["OPENSTREETMAP"] += 1
                canon_id = self._canonical_identity(cand.company_name, cand.city, cand.target_country)
                if canon_id in pool:
                    duplicates_in_round += 1
                    pool[canon_id] = self.merge_candidates(pool[canon_id], cand)
                else:
                    pool[canon_id] = cand
        except Exception as e:
            osm_error = e
            logger.error(f"[HybridDiscovery] OSM discovery provider error: {e}")

        # Step 2: Foursquare Places API (Secondary source if key exists)
        if self.foursquare.api_key:
            fsq_candidates = self.foursquare.search_businesses(city=city, country=country, industry=industry, limit=min(limit, 20))
            for cand in fsq_candidates:
                self.transparency_stats["businesses_discovered_total"] += 1
                self.transparency_stats["by_source"]["FOURSQUARE"] += 1
                canon_id = self._canonical_identity(cand.company_name, cand.city, cand.target_country)
                if canon_id in pool:
                    duplicates_in_round += 1
                    pool[canon_id] = self.merge_candidates(pool[canon_id], cand)
                else:
                    pool[canon_id] = cand

        # Step 3: Web Search Discovery & Enrichment
        # A) Primary failure & zero-yield fallback (Phase 2B):
        # When primary OSM encounters an outage, OR returns zero candidates,
        # invoke independent WebSearch discovery fallback without causing unnecessary searches on successful runs.
        web_fallback_error = None
        budget_searches = getattr(self, "budget", {}).get("max_web_searches", 20)
        web_fallback_on_empty = os.getenv("DISCOVERY_WEB_FALLBACK_ON_EMPTY", "false").lower() == "true"
        needs_web_discovery = (
            (osm_error is not None or (web_fallback_on_empty and len(osm_candidates) == 0 and len(pool) == 0))
            and len(pool) < limit
            and budget_searches > 0
        )
        if needs_web_discovery:
            try:
                deficit = limit - len(pool)
                web_candidates = self.discover_from_web_search(
                    city=city,
                    country=country,
                    industry=industry,
                    limit=deficit
                )
                for cand in web_candidates:
                    self.transparency_stats["businesses_discovered_total"] += 1
                    self.transparency_stats["by_source"]["WEB_SEARCH"] += 1
                    canon_id = self._canonical_identity(cand.company_name, cand.city, cand.target_country)
                    if canon_id in pool:
                        duplicates_in_round += 1
                        pool[canon_id] = self.merge_candidates(pool[canon_id], cand)
                    else:
                        pool[canon_id] = cand
            except Exception as e:
                web_fallback_error = e
                logger.error(f"[HybridDiscovery] Web search discovery fallback failed: {e}")

        # B) Enrich candidates from direct social-profile search results.
        targets = [
            c for c in pool.values()
            if not c.instagram_url or not c.facebook_url
        ][:10]

        if targets:
            try:
                web_results = self.web_search.search_businesses(
                    city=city,
                    country=country,
                    industry=f"independent {industry}" if round_number == 1 else industry,
                    limit=min(30, len(targets) * 3),
                    candidate_names=[c.company_name for c in targets],
                )
            except TypeError as exc:
                if "candidate_names" not in str(exc):
                    raise
                web_results = self.web_search.search_businesses(
                    city=city, country=country, industry=f"independent {industry}" if round_number == 1 else industry,
                    limit=min(30, len(targets) * 3),
                )
            except Exception as exc:
                logger.warning("[HybridDiscovery] Web search error: %s", exc)
                web_results = []

            reserved = {
                "p", "reel", "reels", "stories", "explore", "accounts",
                "directory", "locations", "tags", "tv", "watch", "posts",
                "events", "groups", "share", "login", "search",
                "marketplace", "profile.php", "pages",
            }
            source_ids = set()
            matched_ids = set()

            for hit in web_results:
                if isinstance(hit, DiscoveredBusiness):
                    canon = self._canonical_identity(
                        hit.company_name, hit.city, hit.target_country
                    )
                    if canon in pool:
                        duplicates_in_round += 1
                        pool[canon] = self.merge_candidates(pool[canon], hit)
                    else:
                        pool[canon] = hit
                    source_ids.add(canon)
                    continue

                if not isinstance(hit, dict):
                    continue

                url = str(hit.get("result_url") or hit.get("url") or "").strip()
                if not url:
                    continue

                try:
                    parsed = urlparse(url)
                    host = (parsed.hostname or "").lower()
                    if host.startswith("www."):
                        host = host[4:]
                    parts = [p for p in parsed.path.strip("/").split("/") if p]
                except Exception:
                    continue

                if (host == "instagram.com" and len(parts) == 1
                        and parts[0].lower() not in reserved):
                    platform = "instagram"
                elif (host == "facebook.com" and len(parts) == 1
                        and parts[0].lower() not in reserved):
                    platform = "facebook"
                else:
                    continue

                # Match identity against search text, not the handle alone.
                searchable = re.sub(
                    r"[^a-z0-9]+", "",
                    " ".join([
                        str(hit.get("title") or ""),
                        str(hit.get("snippet") or ""),
                    ]).lower().replace("&", " and "),
                )

                matches = []
                result_text = " ".join([
                    str(hit.get("title") or ""),
                    str(hit.get("snippet") or ""),
                ])
                query_searchable = re.sub(
                    r"[^a-z0-9]+", "",
                    str(hit.get("query") or "").lower().replace("&", " and "),
                )
                known_places = (
                    "london", "birmingham", "liverpool", "leeds", "bristol",
                    "sheffield", "nottingham", "leicester", "coventry",
                    "newcastle", "oxford", "cambridge", "brighton", "edinburgh",
                    "glasgow", "cardiff", "belfast", "southampton", "portsmouth",
                    "exeter", "derby", "norwich", "bath", "wolverhampton",
                    "blackpool", "preston", "bothell", "seattle",
                )

                for canon, candidate in pool.items():
                    name = re.sub(
                        r"[^a-z0-9]+", "",
                        (candidate.company_name or "").lower().replace("&", " and "),
                    )
                    if len(name) < 5 or name not in searchable:
                        continue
                    # When the provider supplies a query, keep its result
                    # associated with the business that was actually searched.
                    if query_searchable and name not in query_searchable:
                        continue

                    city = re.sub(
                        r"[^a-z0-9]+", "",
                        str(candidate.city or "").split(",")[0].lower(),
                    )
                    conflicting_places = [
                        place for place in known_places
                        if place != city and place not in name
                        and re.search(
                            rf"(?<![a-z]){re.escape(place)}(?![a-z])",
                            result_text, flags=re.IGNORECASE,
                        )
                    ]
                    if conflicting_places:
                        continue

                    matches.append((canon, candidate))

                # Do not associate ambiguous profiles with a business.
                if len(matches) != 1:
                    continue

                canon, candidate = matches[0]
                matched_ids.add(canon)

                if not isinstance(candidate.raw_data, dict):
                    candidate.raw_data = {}
                evidence = candidate.raw_data.setdefault("web_search_evidence", [])
                if not any(
                    isinstance(item, dict) and item.get("url") == url
                    for item in evidence
                ) and len(evidence) < 5:
                    evidence.append({
                        "url": url,
                        "title": str(hit.get("title") or ""),
                        "snippet": str(hit.get("snippet") or ""),
                        "query": str(hit.get("query") or ""),
                        "search_provider": str(hit.get("search_provider") or ""),
                        "matched_platform": platform,
                    })

                if not isinstance(candidate.evidence_sources, dict):
                    candidate.evidence_sources = {}

                if platform == "instagram" and not candidate.instagram_url:
                    candidate.instagram_url = url
                    candidate.evidence_sources.setdefault("instagram", "WEB_SEARCH")
                    candidate.social_status = SocialStatus.SOCIAL_FOUND.value
                elif platform == "facebook" and not candidate.facebook_url:
                    candidate.facebook_url = url
                    candidate.evidence_sources.setdefault("facebook", "WEB_SEARCH")
                    candidate.social_status = SocialStatus.SOCIAL_FOUND.value

                # Discovery is not verification of account ownership.
                candidate.social_ownership_status = "UNVERIFIED"
                candidate.raw_data["social_ownership_status"] = "UNVERIFIED"

            source_ids.update(matched_ids)
            self.transparency_stats["by_source"]["WEB_SEARCH"] += len(source_ids)
            self.transparency_stats["web_search_evidence_matches"] = (
                self.transparency_stats.get("web_search_evidence_matches", 0)
                + len(matched_ids)
            )

        # Step 4: Optional Apify fallback ONLY if explicitly enabled
        if self.apify and self.apify_enabled and self.mode == DiscoveryMode.HYBRID and len(pool) < (limit // 2):
            print(f"[HybridDiscovery] Free providers returned {len(pool)} candidates. Invoking enabled Apify fallback...")
            ap_candidates, _ = self.apify.discover_round(round_number, country, cities, industry, limit - len(pool))
            for cand in ap_candidates:
                self.transparency_stats["businesses_discovered_total"] += 1
                self.transparency_stats["by_source"]["APIFY"] += 1
                canon_id = self._canonical_identity(cand.company_name, cand.city, cand.target_country)
                if canon_id in pool:
                    duplicates_in_round += 1
                    pool[canon_id] = self.merge_candidates(pool[canon_id], cand)
                else:
                    pool[canon_id] = cand

        self.transparency_stats["duplicates_merged"] += duplicates_in_round
        self.transparency_stats["unique_candidates"] = len(pool)

        # If pool is empty AND primary provider encountered an outage, do NOT report a false empty success
        if not pool and osm_error is not None:
            if web_fallback_error is not None:
                err_msg = (
                    f"Discovery provider outage: Primary OSM discovery failed ({osm_error}) "
                    f"and web-search fallback failed ({web_fallback_error})."
                )
            else:
                err_msg = (
                    f"Discovery provider outage: Primary OSM discovery failed ({osm_error}) "
                    f"and web-search fallback returned no valid candidates."
                )
            logger.error(f"[HybridDiscovery] {err_msg}")
            raise RuntimeError(err_msg) from osm_error
        elif not pool and web_fallback_error is not None:
            err_msg = (
                f"Discovery provider outage: Primary OSM discovery yielded 0 candidates "
                f"and web-search fallback failed ({web_fallback_error})."
            )
            logger.error(f"[HybridDiscovery] {err_msg}")
            raise RuntimeError(err_msg) from web_fallback_error

        # Candidate prioritization for Dripp Media (Targeting businesses without website)
        # 0: No website listed
        # 1: Social/delivery platform listed
        # 2: Listed website
        results = list(pool.values())
        def priority_sorter(b: DiscoveredBusiness) -> int:
            if b.country_status != CountryStatus.COUNTRY_MATCH.value:
                return 99
            w = (b.raw_website or "").strip().lower()
            if not w:
                return 0
            if any(p in w for p in ["instagram.com", "facebook.com", "tiktok.com", "deliveroo", "ubereats", "just-eat", "linktr.ee"]):
                return 1
            return 2

        results.sort(key=priority_sorter)
        return results, queries_used

    def search_social_references(
        self,
        business_name: str,
        city: str,
        country: str = "United Kingdom",
        **kwargs
    ) -> List[Dict[str, Any]]:
        """Searches third-party creator and social references for a business."""
        self.transparency_stats["by_source"]["CREATOR_REFERENCES"] += 1
        return self.web_search.search_social_references(business_name=business_name, city=city, country=country, **kwargs)
