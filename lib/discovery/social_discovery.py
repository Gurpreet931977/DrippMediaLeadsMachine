"""
Dripp Media — Search-Based Social Profile Discovery
==================================================
Discovers candidate social profiles (Instagram, Facebook, TikTok) for businesses
using free-first search providers (SearXNG / WebSearchProvider).

Safety & Evidence Invariants:
  1. Produces CANDIDATE_OFFICIAL_ACCOUNT items; never self-promotes to VERIFIED.
  2. SocialIdentityValidator remains the sole authority for promotion to VERIFIED_BUSINESS_ACCOUNT.
  3. Enforces strict Location Safety: rejects candidate profiles located in conflicting major cities.
  4. Preserves branch isolation and legitimate administrative area/suburb matching.
"""

import os
import re
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

from lib.types import (
    DiscoveredBusiness,
    HandleClassification,
    SocialOwnershipStatus,
    SocialStatus,
    SocialProfileStatus
)
from lib.discovery.web_search import WebSearchProvider
from lib.validation.social_validator import SocialIdentityValidator

# Conflicting major UK cities outside Leeds metropolitan area
CONFLICTING_MAJOR_UK_CITIES = [
    "birmingham", "london", "manchester", "liverpool", "glasgow",
    "edinburgh", "bristol", "cardiff", "newcastle", "sheffield",
    "nottingham", "leicester", "southampton", "belfast", "brighton"
]

# Legitimate Leeds administrative suburbs and local market towns
LEEDS_LOCAL_AREAS = [
    "leeds", "otley", "pudsey", "yeadon", "guiseley", "horsforth",
    "morley", "wetherby", "rothwell", "ilkley", "headingley",
    "chapel allerton", "roundhay", "harehills", "kirkstall", "calverley",
    "farsley", "bramley", "armley", "garforth", "boston spa", "west yorkshire"
]

# Legitimate Manchester administrative suburbs and districts
MANCHESTER_LOCAL_AREAS = [
    "manchester", "mcr", "didsbury", "chorlton", "fallowfield", "rusholme",
    "withington", "ancoats", "deansgate", "castlefield", "northern quarter",
    "cheetham hill", "hulme", "moss side", "whalley range", "longsight",
    "levenshulme", "gorton", "altrincham", "sale", "stretford", "urmstston",
    "salford", "greater manchester", "curry mile"
]


@dataclass
class DiscoveredSocialCandidate:
    platform: str
    url: str
    handle: str
    classification: str = HandleClassification.CANDIDATE_OFFICIAL_ACCOUNT.value
    discovery_query: str = ""
    source_title: str = ""
    source_snippet: str = ""
    city_match: bool = False
    location_warning: str = ""
    is_valid_format: bool = True
    reject_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "url": self.url,
            "handle": self.handle,
            "classification": self.classification,
            "discovery_query": self.discovery_query,
            "source_title": self.source_title,
            "city_match": self.city_match,
            "location_warning": self.location_warning,
            "is_valid_format": self.is_valid_format,
            "reject_reason": self.reject_reason
        }


class SocialProfileDiscoverer:
    """
    Search-based social account discovery module.
    Queries search providers for candidate social profiles and subjects them to
    location safety checks before handing off to SocialIdentityValidator.
    """

    def __init__(
        self,
        web_search_provider: Optional[WebSearchProvider] = None,
        validator: Optional[SocialIdentityValidator] = None
    ):
        self.web = web_search_provider or WebSearchProvider()
        self.validator = validator or SocialIdentityValidator()

    @staticmethod
    def generate_social_queries(business_name: str, city: str) -> List[str]:
        """Generates targeted, high-precision social discovery queries."""
        clean_name = re.sub(r'["\'’]', '', business_name).strip()
        clean_city = re.sub(r'["\'’]', '', city).strip()

        queries = [
            f'"{clean_name}" "{clean_city}" facebook OR instagram',
            f'"{clean_name}" "{clean_city}" facebook',
            f'"{clean_name}" "{clean_city}" instagram',
            f'site:facebook.com "{clean_name}" "{clean_city}"',
            f'site:instagram.com "{clean_name}" "{clean_city}"'
        ]
        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for q in queries:
            if q not in seen:
                seen.add(q)
                deduped.append(q)
        return deduped

    def check_location_safety(
        self,
        text: str,
        target_city: str,
        business_name: str
    ) -> Tuple[bool, str]:
        """
        Verifies that a social profile search snippet or title does not associate
        the business with an incompatible UK city (e.g. Birmingham for a Leeds business).
        """
        low = text.lower()
        target_low = target_city.lower().strip()

        # Check if text mentions another major UK city
        competing_cities = [c for c in CONFLICTING_MAJOR_UK_CITIES if c != target_low]
        found_other = [c for c in competing_cities if re.search(r'\b' + re.escape(c) + r'\b', low)]

        # Check if text mentions target city or known administrative areas of target city
        target_indicators = [target_low]
        if target_low in ["leeds", "otley", "pudsey", "yeadon"]:
            target_indicators.extend(LEEDS_LOCAL_AREAS)
        elif target_low in ["manchester", "salford"]:
            target_indicators.extend(MANCHESTER_LOCAL_AREAS)

        has_target = any(re.search(r'\b' + re.escape(ind) + r'\b', low) for ind in target_indicators)

        if found_other and not has_target:
            return False, f"Location conflict: references {', '.join(found_other)} without matching target city '{target_city}'."

        return True, ""

    def discover_candidates_for_business(
        self,
        business_name: str,
        city: str,
        max_queries: int = 2
    ) -> Dict[str, DiscoveredSocialCandidate]:
        """
        Discovers candidate social profiles for a business.
        Returns:
            Dict mapping platform ('facebook', 'instagram', 'tiktok') to DiscoveredSocialCandidate.
            All returned candidates have classification = CANDIDATE_OFFICIAL_ACCOUNT.
        """
        queries = self.generate_social_queries(business_name, city)
        candidates: Dict[str, DiscoveredSocialCandidate] = {}

        for q in queries[:max_queries]:
            if "facebook" in candidates and "instagram" in candidates:
                break

            search_res = self.web.search_web(q, num_results=5)
            for r in search_res:
                u = r.get("result_url", "").strip()
                t = r.get("title", "").strip()
                s = r.get("snippet", "").strip()
                combined_snippet = f"{t} — {s}"

                if not u:
                    continue

                u_lower = u.lower()
                target_platform = None
                if "facebook.com/" in u_lower:
                    target_platform = "facebook"
                elif "instagram.com/" in u_lower:
                    target_platform = "instagram"
                elif "tiktok.com/" in u_lower:
                    target_platform = "tiktok"

                if not target_platform or target_platform in candidates:
                    continue

                clean_url, handle, is_profile, err = self.validator.extract_profile_url(u, target_platform)
                if not is_profile or not handle:
                    continue

                # Location Safety Check
                loc_safe, loc_warning = self.check_location_safety(
                    text=combined_snippet,
                    target_city=city,
                    business_name=business_name
                )
                if not loc_safe:
                    continue

                # Candidate official account generated (Section 5)
                cand = DiscoveredSocialCandidate(
                    platform=target_platform,
                    url=clean_url,
                    handle=handle,
                    classification=HandleClassification.CANDIDATE_OFFICIAL_ACCOUNT.value,
                    discovery_query=q,
                    source_title=t,
                    source_snippet=s,
                    city_match=loc_safe,
                    location_warning=loc_warning,
                    is_valid_format=True
                )
                candidates[target_platform] = cand

        return candidates

    def enrich_and_verify(
        self,
        business: DiscoveredBusiness,
        max_queries: int = 2
    ) -> Tuple[DiscoveredBusiness, Dict[str, Any]]:
        """
        Executes social discovery for a candidate business, verifies ownership
        via SocialIdentityValidator, and updates business social fields.
        Does NOT bypass validation: SocialIdentityValidator remains sole authority.
        """
        existing_urls: Dict[str, str] = {}
        if business.instagram_url:
            existing_urls["instagram"] = business.instagram_url
        if business.facebook_url:
            existing_urls["facebook"] = business.facebook_url
        if business.tiktok_url:
            existing_urls["tiktok"] = business.tiktok_url

        discovered_candidates: Dict[str, DiscoveredSocialCandidate] = {}

        # If any major social channel is missing, search for candidates
        missing_channels = [p for p in ["facebook", "instagram"] if p not in existing_urls]
        if missing_channels:
            found_candidates = self.discover_candidates_for_business(
                business_name=business.company_name,
                city=business.city,
                max_queries=max_queries
            )
            for plat, cand in found_candidates.items():
                discovered_candidates[plat] = cand
                if plat not in existing_urls:
                    existing_urls[plat] = cand.url

        # Sole promotion authority: SocialIdentityValidator
        audit = self.validator.verify_ownership(
            business_name=business.company_name,
            city=business.city,
            industry=business.category or "Restaurant",
            social_urls=existing_urls
        )

        verified_urls = audit.get("verified_urls", {})
        verified_handles = audit.get("verified_handles", {})

        # Attach verified profiles to business
        if "instagram" in verified_urls and not business.instagram_url:
            business.instagram_url = verified_urls["instagram"]
        if "facebook" in verified_urls and not business.facebook_url:
            business.facebook_url = verified_urls["facebook"]
        if "tiktok" in verified_urls and not business.tiktok_url:
            business.tiktok_url = verified_urls["tiktok"]

        # Track discovery classifications
        classifications: Dict[str, str] = {}
        for plat, url in existing_urls.items():
            if plat in verified_urls:
                classifications[plat] = "VERIFIED_BUSINESS_ACCOUNT"
            elif plat in discovered_candidates:
                # Was discovered as candidate, but failed verification
                classifications[plat] = "AMBIGUOUS"
            else:
                classifications[plat] = "UNVERIFIED"

        discovery_summary = {
            "discovered_candidates": {p: c.to_dict() for p, c in discovered_candidates.items()},
            "classifications": classifications,
            "audit": audit
        }

        if hasattr(business, "raw_data") and isinstance(business.raw_data, dict):
            business.raw_data["social_discovery"] = discovery_summary

        business.social_status = audit.get("social_status", SocialStatus.SOCIAL_UNKNOWN.value)
        business.social_ownership_status = audit.get("social_ownership_status", SocialOwnershipStatus.UNKNOWN.value)
        business.social_profile_status = audit.get("social_profile_status", SocialProfileStatus.UNKNOWN.value)
        business.social_activity = audit.get("social_activity", "")

        return business, discovery_summary
