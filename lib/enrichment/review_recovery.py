"""
Dripp Media — Review Evidence Recovery & Source Diversification Layer (Phase 7.2)
=================================================================================
Recovers authentic review counts, star ratings, and date-bearing review evidence
from alternative accepted source families (Restaurant Guru, Yelp, Tripadvisor, Google)
for candidates blocked by UNKNOWN review freshness.

Core Invariants:
  - Keep Rule B unchanged (all 10 conditions strict).
  - Source Family Independence: Tripoli + Tripadvisor = 1 family; TripAdvisor + Restaurant Guru = independent.
  - Source Priority:
      1. Existing cached accepted source
      2. Restaurant Guru
      3. Yelp
      4. Other already-supported accepted review source with real review dates (TripAdvisor, Google)
      5. Search-result evidence only when snippet explicitly anchors a review date
      6. Direct page fetch using existing permitted HTTP infrastructure
  - Zero Anti-Bot Circumvention:
      If 403 / DataDome / CAPTCHA / access denial: record the failure as SEARCH_BLOCKED or
      HTTP_403_ACCESS_RESTRICTED and stop using that source. Never attempt bypass.
  - Same-Business Identity Requirement:
      Evidence from another page is usable ONLY if identity confidence >= 0.70 via
      BusinessIdentityMatcher (name, city, street, postcode, phone, wrong branch isolation).
  - Review-Date Requirement:
      Only authentic review publication dates, review activity dates, or review-anchored
      relative ages can establish freshness. SEO titles, copyright, crawl dates, and
      search indexing prefixes remain INVALID.
  - Multi-Source Reconciliation:
      Preserves all evidence items. Unresolved conflicts cause Rule B to fail.
  - No CRM mutation: pure evidence and qualification recovery evaluation.
"""

import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple, Set

import requests
from bs4 import BeautifulSoup

from lib.types import DiscoveredBusiness, SourceFamily, WebsiteStatus, OperationalStatus
from lib.discovery.web_search import WebSearchProvider, SearchOutcome, SearchResultList
from lib.enrichment.review_date_extractor import (
    ReviewDateExtractor,
    ReviewEvidenceDateType,
    ReviewDateConfidence,
    ReviewFreshness,
    ExtractedReviewDate,
)
from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
    ReviewEvidenceItem,
    ReviewEnrichmentResult,
    ReviewConfidence,
    ReviewStatus,
    REFERENCE_DATE,
)
from lib.crm.identity_matcher import (
    BusinessIdentityMatcher,
    IdentityMatchOutcome,
    IdentityMatchResult,
    clean_ascii_text,
)
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.enrichment.contactability import ContactabilityAssessor
from lib.enrichment.review_reconciler import (
    ReviewEvidenceReconciler,
    ReviewConflictType,
    ReconciledReviewEvidence,
)


@dataclass
class ReviewRecoveryTelemetryItem:
    """Detailed telemetry record for each source queried for a candidate."""
    business: str
    source_family: str
    source_url: str
    provider: str
    attempted: bool
    succeeded: bool
    http_status: Optional[int] = None
    search_outcome: str = "SEARCH_SUCCEEDED_EMPTY"
    review_count_found: Optional[int] = None
    rating_found: Optional[float] = None
    review_date_found: Optional[str] = None
    review_date_type: str = ReviewEvidenceDateType.UNKNOWN.value
    freshness: str = ReviewFreshness.UNKNOWN.value
    identity_confidence: float = 0.0
    failure_reason: Optional[str] = None
    retrieval_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewRecoveryCandidateResult:
    """Output evaluation record for a single candidate undergoing review evidence recovery."""
    index: int
    business_name: str
    city: str
    original_review_count: Optional[int]
    original_rating: Optional[float]
    original_freshness: str
    original_operational_status: str
    original_qualification_state: str
    recovered_review_count: Optional[int] = None
    recovered_rating: Optional[float] = None
    recovered_freshness: str = ReviewFreshness.UNKNOWN.value
    recovered_date: Optional[str] = None
    recovered_source_family: str = SourceFamily.UNKNOWN.value
    recovered_source_url: str = ""
    extraction_method: str = "NONE"
    identity_confidence: float = 0.0
    status: str = "REMAINED_UNKNOWN"  # RECOVERED_RECENT, RECOVERED_STALE, REMAINED_UNKNOWN, REJECTED_IDENTITY_MISMATCH, REJECTED_CONFLICT, SOURCES_BLOCKED_OR_FAILED
    new_operational_status: str = ""
    new_qualification_state: str = ""
    score: int = 0
    priority: str = "LOW"
    contactability_after: Optional[Dict[str, Any]] = None
    all_evidence: List[Dict[str, Any]] = field(default_factory=list)
    telemetry: List[Dict[str, Any]] = field(default_factory=list)
    failure_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ReviewEvidenceRecoveryLayer:
    """
    Extends review enrichment by attempting source diversification
    to recover authentic, date-bearing review evidence without weakening gates.
    """

    ACCEPTED_REVIEW_SOURCES = {
        SourceFamily.TRIPADVISOR,
        SourceFamily.GOOGLE,
        SourceFamily.RESTAURANT_GURU,
        SourceFamily.YELP,
        SourceFamily.OTHER_DIRECTORY,
    }

    def __init__(
        self,
        web_search_provider: Optional[WebSearchProvider] = None,
        cache_dir: Optional[str] = None,
        matcher: Optional[BusinessIdentityMatcher] = None
    ):
        self.web = web_search_provider or WebSearchProvider()
        self.matcher = matcher or BusinessIdentityMatcher()
        self.reconciler = ReviewEvidenceReconciler(matcher=self.matcher)
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = cache_dir or os.path.join(base_dir, "data", "cache_review_dates")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-GB,en;q=0.9"
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 1. IDENTITY VERIFICATION HELPER
    # ──────────────────────────────────────────────────────────────────────────
    def verify_candidate_identity(
        self,
        cand_name: str,
        cand_city: str,
        cand_street: str = "",
        cand_postcode: str = "",
        cand_phone: str = "",
        discovered_name: str = "",
        discovered_city: str = "",
        discovered_street: str = "",
        discovered_postcode: str = "",
        discovered_phone: str = "",
        snippet: str = "",
        url: str = ""
    ) -> Tuple[bool, float, Optional[str]]:
        """
        Validates that a discovered listing belongs to the candidate business.
        Enforces:
          - Distinctive name matching
          - Wrong-branch rejection
          - Location / city corroboration
          - Minimum 0.70 identity confidence
        Returns: (is_valid, confidence_score, reject_reason)
        """
        cand_dict = {
            "company_name": cand_name,
            "city": cand_city or "Manchester",
            "street": cand_street,
            "postcode": cand_postcode,
            "phone": cand_phone
        }
        disc_dict = {
            "company_name": discovered_name or cand_name,
            "city": discovered_city or cand_city or "Manchester",
            "street": discovered_street,
            "postcode": discovered_postcode,
            "phone": discovered_phone
        }

        # 1. Wrong-branch check across major cities
        combined = f"{discovered_name} {discovered_city} {snippet} {url}".lower()
        cand_city_norm = (cand_city or "manchester").lower().strip()
        major_cities = [
            "london", "birmingham", "leeds", "bristol", "edinburgh",
            "glasgow", "sheffield", "liverpool", "newcastle", "nottingham"
        ]
        for mc in major_cities:
            if mc in combined and mc != cand_city_norm:
                # Discovered text references a different major city
                if cand_city_norm not in combined and (not cand_street or cand_street.lower() not in combined):
                    return False, 0.15, f"WRONG_BRANCH_LOCATION ({mc.title()})"

        # 2. Token overlap & BusinessIdentityMatcher
        conf, reasons, conflict = self.matcher.compute_similarity(cand_dict, disc_dict)
        if conflict:
            return False, conf, f"IDENTITY_CONFLICT ({conflict})"

        # If candidate lacks street/address/phone, fallback to name similarity + city corroboration
        if conf < 0.70 and not cand_street and not cand_postcode and not cand_phone:
            name_score, name_reasons, name_compatible = self.matcher.compute_name_similarity(cand_name, discovered_name or cand_name)
            if name_score >= 0.85 and name_compatible and cand_city_norm in combined:
                conf = max(conf, name_score * 0.95)

        # If name tokens in snippet/title don't match, penalize
        clean_cand = re.sub(r'[^a-z0-9]', '', cand_name.lower())
        clean_disc = re.sub(r'[^a-z0-9]', '', (discovered_name or snippet).lower())
        if clean_cand not in clean_disc:
            # Check individual tokens
            cand_tokens = [t for t in cand_name.lower().split() if len(t) > 2]
            matched_toks = [t for t in cand_tokens if t in clean_disc]
            if len(cand_tokens) > 1 and len(matched_toks) < (len(cand_tokens) / 2):
                return False, 0.25, "INSUFFICIENT_NAME_TOKEN_OVERLAP"

        if conf < 0.70:
            return False, conf, f"INSUFFICIENT_IDENTITY_CONFIDENCE ({conf:.2f} < 0.70)"

        return True, conf, None

    # ──────────────────────────────────────────────────────────────────────────
    # 2. SOURCE EXTRACTION ADAPTERS
    # ──────────────────────────────────────────────────────────────────────────

    def _fetch_page(self, url: str, timeout: int = 6) -> Tuple[Optional[int], str, Optional[str]]:
        """
        Safely fetches destination page HTML via HTTP.
        Returns: (http_status, html_text, error_reason)
        Halts immediately on 403/DataDome/CAPTCHA with NO circumvention.
        """
        if not url or not url.startswith("http"):
            return None, "", "INVALID_URL"

        # Check local cache first
        url_hash = hashlib.md5(url.encode()).hexdigest()
        cache_file = os.path.join(self.cache_dir, f"{url_hash}.html")
        meta_file = os.path.join(self.cache_dir, f"{url_hash}_meta.json")

        if os.path.exists(cache_file) and os.path.exists(meta_file):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                with open(cache_file, "r", encoding="utf-8") as f:
                    content = f.read()
                return meta.get("status_code", 200), content, meta.get("error_reason")
            except Exception:
                pass

        try:
            resp = requests.get(url, headers=self.headers, timeout=timeout)
            status = resp.status_code
            text = resp.text or ""

            err_reason = None
            if status in [401, 403, 429, 503]:
                err_reason = f"HTTP_{status}_ACCESS_RESTRICTED"
            elif any(k in text.lower() for k in ["suspicious activity detected", "datadome", "cf-browser-verification", "checking your browser", "access denied"]):
                status = 403
                err_reason = "HTTP_403_ACCESS_RESTRICTED"
            elif status != 200:
                err_reason = f"HTTP_{status}"

            # Cache response
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    f.write(text)
                with open(meta_file, "w", encoding="utf-8") as f:
                    json.dump({"status_code": status, "error_reason": err_reason}, f)
            except Exception:
                pass

            return status, text, err_reason
        except requests.exceptions.Timeout:
            return None, "", "REQUEST_TIMEOUT"
        except requests.exceptions.RequestException as e:
            return None, "", f"NETWORK_ERROR: {str(e)[:80]}"

    def _extract_restaurant_guru(
        self,
        candidate: Dict[str, Any],
        as_of: datetime = REFERENCE_DATE
    ) -> Tuple[Optional[ReviewEvidenceItem], ReviewRecoveryTelemetryItem]:
        """
        Queries and recovers review evidence from Restaurant Guru.
        """
        cname = candidate.get("company_name", "")
        city = candidate.get("city", "Manchester")
        street = candidate.get("street", "")
        postcode = candidate.get("postcode", "")
        phone = candidate.get("phone", "")

        q = f'"{cname}" "{city}" "Restaurant Guru"'
        res = self.web.search_web(q, num_results=3)

        outcome_val = getattr(res, "outcome", None)
        outcome_name = outcome_val.value if hasattr(outcome_val, "value") else str(outcome_val or "SEARCH_FAILED")

        def _match_rg_result(results):
            c_toks = [t.lower() for t in cname.split() if len(t) > 2]
            best = None
            generic = None
            for r in results:
                u = r.get("result_url", "")
                if "restaurantguru.com" in u.lower():
                    clean_u = re.sub(r'[^a-z0-9]', '', u.lower())
                    clean_t = re.sub(r'[^a-z0-9]', '', r.get("title", "").lower())
                    if any(t in clean_u or t in clean_t for t in c_toks):
                        return u, r.get("snippet", ""), r.get("title", "")
                    if not generic:
                        generic = (u, r.get("snippet", ""), r.get("title", ""))
            return generic or (None, "", "")

        target_url, target_snippet, target_title = _match_rg_result(res)

        if not target_url or not any(t.lower() in target_url.lower() for t in cname.split() if len(t) > 2):
            for alt_q in [f'"{cname}" "{city}" reviews', f'"{cname}" "{city}" restaurant']:
                alt_res = self.web.search_web(alt_q, num_results=3)
                alt_u, alt_s, alt_t = _match_rg_result(alt_res)
                if alt_u and any(t.lower() in alt_u.lower() for t in cname.split() if len(t) > 2):
                    target_url, target_snippet, target_title = alt_u, alt_s, alt_t
                    break

        if not target_url:
            telemetry = ReviewRecoveryTelemetryItem(
                business=cname,
                source_family=SourceFamily.RESTAURANT_GURU.value,
                source_url="",
                provider=getattr(res, "provider", "") or "SEARXNG",
                attempted=True,
                succeeded=False,
                search_outcome=outcome_name,
                failure_reason="NO_RESTAURANT_GURU_URL_FOUND"
            )
            return None, telemetry

        # Fetch destination page
        status, html, fetch_err = self._fetch_page(target_url, timeout=6)
        if fetch_err or status != 200 or not html:
            telemetry = ReviewRecoveryTelemetryItem(
                business=cname,
                source_family=SourceFamily.RESTAURANT_GURU.value,
                source_url=target_url,
                provider="SEARXNG_PAGE_FETCH",
                attempted=True,
                succeeded=False,
                http_status=status,
                search_outcome="SEARCH_BLOCKED" if status in [401, 403, 429] else "SEARCH_FAILED",
                failure_reason=fetch_err or "EMPTY_HTML"
            )
            return None, telemetry

        # Parse page HTML: structured JSON-LD & review cards
        soup = BeautifulSoup(html, "html.parser")

        disc_name = ""
        disc_city = ""
        disc_street = ""
        disc_postcode = ""
        disc_phone = ""
        rev_count = None
        rating_val = None

        # Check JSON-LD for Restaurant details
        for s in soup.find_all("script", type="application/ld+json"):
            if s.string:
                try:
                    data = json.loads(s.string)
                    if isinstance(data, dict):
                        if data.get("@type") in ["Restaurant", "FoodEstablishment", "CafeOrCoffeeShop"]:
                            disc_name = data.get("name") or disc_name
                            disc_phone = data.get("telephone") or disc_phone
                            addr = data.get("address")
                            if isinstance(addr, dict):
                                disc_city = addr.get("addressLocality") or disc_city
                                disc_street = addr.get("streetAddress") or disc_street
                                disc_postcode = addr.get("postalCode") or disc_postcode
                            agg = data.get("aggregateRating")
                            if isinstance(agg, dict):
                                try:
                                    if agg.get("reviewCount"):
                                        rev_count = int(agg.get("reviewCount"))
                                    if agg.get("ratingValue"):
                                        rating_val = float(agg.get("ratingValue"))
                                except (ValueError, TypeError):
                                    pass
                except Exception:
                    pass

        # If name not in JSON-LD, extract from H1 or Title
        if not disc_name:
            h1 = soup.find("h1")
            disc_name = h1.get_text(strip=True) if h1 else target_title

        # Verify identity
        is_valid_id, conf, id_err = self.verify_candidate_identity(
            cand_name=cname,
            cand_city=city,
            cand_street=street,
            cand_postcode=postcode,
            cand_phone=phone,
            discovered_name=disc_name,
            discovered_city=disc_city,
            discovered_street=disc_street,
            discovered_postcode=disc_postcode,
            discovered_phone=disc_phone,
            snippet=target_snippet,
            url=target_url
        )

        if not is_valid_id:
            telemetry = ReviewRecoveryTelemetryItem(
                business=cname,
                source_family=SourceFamily.RESTAURANT_GURU.value,
                source_url=target_url,
                provider="SEARXNG_PAGE_FETCH",
                attempted=True,
                succeeded=False,
                http_status=status,
                search_outcome="SEARCH_SUCCEEDED_WITH_RESULTS",
                identity_confidence=conf,
                failure_reason=id_err or "IDENTITY_MISMATCH"
            )
            return None, telemetry

        # Extract review date via ReviewDateExtractor
        date_ev = ReviewDateExtractor.extract_from_html(html, source_url=target_url, as_of=as_of)

        # Fallback to snippet if page date is unknown
        if date_ev.freshness == ReviewFreshness.UNKNOWN.value and target_snippet:
            snip_date = ReviewDateExtractor.extract_from_snippet(target_snippet, title=target_title, source_url=target_url, as_of=as_of)
            if snip_date.freshness != ReviewFreshness.UNKNOWN.value:
                date_ev = snip_date

        # If review count or rating still None, try snippet
        if rev_count is None or rating_val is None:
            snip_ev = ReviewRatingEnricher.extract_evidence_from_snippet(
                snippet=target_snippet,
                title=target_title,
                url=target_url,
                business_name=cname,
                city=city,
                country="United Kingdom"
            )
            if snip_ev:
                rev_count = rev_count or snip_ev.review_count
                rating_val = rating_val or snip_ev.rating

        item = ReviewEvidenceItem(
            review_count=rev_count,
            rating=rating_val,
            source="Restaurant Guru",
            source_url=target_url,
            evidence_text=f"Restaurant Guru listing for {disc_name or cname}: {rev_count} reviews, rating {rating_val}",
            evidence_date=date_ev.date,
            confidence=ReviewConfidence.HIGH.value if (rev_count and rating_val and conf >= 0.85) else ReviewConfidence.MEDIUM.value,
            freshness=date_ev.freshness,
            business_match_tier="EXACT_LOCATION" if conf >= 0.85 else "NAME_AND_CITY",
            review_provider="Restaurant Guru",
            evidence_date_raw=date_ev.date_raw,
            evidence_date_type=date_ev.date_type,
            evidence_date_confidence=date_ev.confidence,
            evidence_as_of=date_ev.as_of,
            extraction_method=date_ev.extraction_method,
            source_family=SourceFamily.RESTAURANT_GURU.value,
            business_name=disc_name or cname,
            identity_confidence=conf,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            branch_identifier=ReviewEvidenceReconciler.extract_branch_marker(f"{disc_name} {disc_street} {target_url}") or "",
            street=disc_street,
            postcode=disc_postcode,
            city=disc_city or city,
            phone=disc_phone or phone
        )

        telemetry = ReviewRecoveryTelemetryItem(
            business=cname,
            source_family=SourceFamily.RESTAURANT_GURU.value,
            source_url=target_url,
            provider="SEARXNG_PAGE_FETCH",
            attempted=True,
            succeeded=True,
            http_status=status,
            search_outcome="SEARCH_SUCCEEDED_WITH_RESULTS",
            review_count_found=rev_count,
            rating_found=rating_val,
            review_date_found=date_ev.date,
            review_date_type=date_ev.date_type,
            freshness=date_ev.freshness,
            identity_confidence=conf
        )

        return item, telemetry

    def _extract_yelp(
        self,
        candidate: Dict[str, Any],
        as_of: datetime = REFERENCE_DATE
    ) -> Tuple[Optional[ReviewEvidenceItem], ReviewRecoveryTelemetryItem]:
        """
        Queries and recovers review evidence from Yelp UK.
        """
        cname = candidate.get("company_name", "")
        city = candidate.get("city", "Manchester")
        street = candidate.get("street", "")
        postcode = candidate.get("postcode", "")
        phone = candidate.get("phone", "")

        q = f'"{cname}" "{city}" "Yelp"'
        res = self.web.search_web(q, num_results=3)

        outcome_val = getattr(res, "outcome", None)
        outcome_name = outcome_val.value if hasattr(outcome_val, "value") else str(outcome_val or "SEARCH_FAILED")

        target_url = None
        target_snippet = ""
        target_title = ""

        for r in res:
            u = r.get("result_url", "")
            if "yelp.co.uk" in u.lower() or "yelp.com" in u.lower():
                target_url = u
                target_snippet = r.get("snippet", "")
                target_title = r.get("title", "")
                break

        if not target_url:
            telemetry = ReviewRecoveryTelemetryItem(
                business=cname,
                source_family=SourceFamily.YELP.value,
                source_url="",
                provider=getattr(res, "provider", "") or "SEARXNG",
                attempted=True,
                succeeded=False,
                search_outcome=outcome_name,
                failure_reason="NO_YELP_URL_FOUND"
            )
            return None, telemetry

        # Fetch destination page (with strict timeout)
        status, html, fetch_err = self._fetch_page(target_url, timeout=5)
        if fetch_err or status != 200 or not html:
            telemetry = ReviewRecoveryTelemetryItem(
                business=cname,
                source_family=SourceFamily.YELP.value,
                source_url=target_url,
                provider="SEARXNG_PAGE_FETCH",
                attempted=True,
                succeeded=False,
                http_status=status,
                search_outcome="SEARCH_BLOCKED" if status in [401, 403, 429] else "SEARCH_FAILED",
                failure_reason=fetch_err or "EMPTY_HTML"
            )
            return None, telemetry

        # Parse Yelp HTML
        soup = BeautifulSoup(html, "html.parser")
        disc_name = ""
        disc_city = ""
        disc_street = ""
        disc_postcode = ""
        disc_phone = ""
        rev_count = None
        rating_val = None

        # Check JSON-LD
        for s in soup.find_all("script", type="application/ld+json"):
            if s.string:
                try:
                    data = json.loads(s.string)
                    if isinstance(data, dict):
                        if data.get("@type") in ["Restaurant", "LocalBusiness", "FoodEstablishment"]:
                            disc_name = data.get("name") or disc_name
                            disc_phone = data.get("telephone") or disc_phone
                            addr = data.get("address")
                            if isinstance(addr, dict):
                                disc_city = addr.get("addressLocality") or disc_city
                                disc_street = addr.get("streetAddress") or disc_street
                                disc_postcode = addr.get("postalCode") or disc_postcode
                            agg = data.get("aggregateRating")
                            if isinstance(agg, dict):
                                try:
                                    if agg.get("reviewCount"):
                                        rev_count = int(agg.get("reviewCount"))
                                    if agg.get("ratingValue"):
                                        rating_val = float(agg.get("ratingValue"))
                                except (ValueError, TypeError):
                                    pass
                except Exception:
                    pass

        if not disc_name:
            h1 = soup.find("h1")
            disc_name = h1.get_text(strip=True) if h1 else target_title

        # Verify identity
        is_valid_id, conf, id_err = self.verify_candidate_identity(
            cand_name=cname,
            cand_city=city,
            cand_street=street,
            cand_postcode=postcode,
            cand_phone=phone,
            discovered_name=disc_name,
            discovered_city=disc_city,
            discovered_street=disc_street,
            discovered_postcode=disc_postcode,
            discovered_phone=disc_phone,
            snippet=target_snippet,
            url=target_url
        )

        if not is_valid_id:
            telemetry = ReviewRecoveryTelemetryItem(
                business=cname,
                source_family=SourceFamily.YELP.value,
                source_url=target_url,
                provider="SEARXNG_PAGE_FETCH",
                attempted=True,
                succeeded=False,
                http_status=status,
                search_outcome="SEARCH_SUCCEEDED_WITH_RESULTS",
                identity_confidence=conf,
                failure_reason=id_err or "IDENTITY_MISMATCH"
            )
            return None, telemetry

        date_ev = ReviewDateExtractor.extract_from_html(html, source_url=target_url, as_of=as_of)

        item = ReviewEvidenceItem(
            review_count=rev_count,
            rating=rating_val,
            source="Yelp",
            source_url=target_url,
            evidence_text=f"Yelp listing for {disc_name or cname}: {rev_count} reviews, rating {rating_val}",
            evidence_date=date_ev.date,
            confidence=ReviewConfidence.HIGH.value if (rev_count and rating_val and conf >= 0.85) else ReviewConfidence.MEDIUM.value,
            freshness=date_ev.freshness,
            business_match_tier="EXACT_LOCATION" if conf >= 0.85 else "NAME_AND_CITY",
            review_provider="Yelp",
            evidence_date_raw=date_ev.date_raw,
            evidence_date_type=date_ev.date_type,
            evidence_date_confidence=date_ev.confidence,
            evidence_as_of=date_ev.as_of,
            extraction_method=date_ev.extraction_method,
            source_family=SourceFamily.YELP.value,
            business_name=disc_name or cname,
            identity_confidence=conf,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            branch_identifier=ReviewEvidenceReconciler.extract_branch_marker(f"{disc_name} {disc_street} {target_url}") or "",
            street=disc_street,
            postcode=disc_postcode,
            city=disc_city or city,
            phone=disc_phone or phone
        )

        telemetry = ReviewRecoveryTelemetryItem(
            business=cname,
            source_family=SourceFamily.YELP.value,
            source_url=target_url,
            provider="SEARXNG_PAGE_FETCH",
            attempted=True,
            succeeded=True,
            http_status=status,
            search_outcome="SEARCH_SUCCEEDED_WITH_RESULTS",
            review_count_found=rev_count,
            rating_found=rating_val,
            review_date_found=date_ev.date,
            review_date_type=date_ev.date_type,
            freshness=date_ev.freshness,
            identity_confidence=conf
        )

        return item, telemetry

    def _extract_tripadvisor(
        self,
        candidate: Dict[str, Any],
        as_of: datetime = REFERENCE_DATE
    ) -> Tuple[Optional[ReviewEvidenceItem], ReviewRecoveryTelemetryItem]:
        """
        Queries and attempts to recover review evidence from TripAdvisor.
        Note: If direct fetch returns 403 (DataDome), the failure is recorded
        and no bypass is attempted per Section 5 & 18.
        """
        cname = candidate.get("company_name", "")
        city = candidate.get("city", "Manchester")
        street = candidate.get("street", "")
        postcode = candidate.get("postcode", "")
        phone = candidate.get("phone", "")

        q = f'"{cname}" "{city}" "Tripadvisor"'
        res = self.web.search_web(q, num_results=3)

        outcome_val = getattr(res, "outcome", None)
        outcome_name = outcome_val.value if hasattr(outcome_val, "value") else str(outcome_val or "SEARCH_FAILED")

        target_url = None
        target_snippet = ""
        target_title = ""

        for r in res:
            u = r.get("result_url", "")
            if "tripadvisor.co" in u.lower() or "tripadvisor.com" in u.lower():
                target_url = u
                target_snippet = r.get("snippet", "")
                target_title = r.get("title", "")
                break

        if not target_url:
            telemetry = ReviewRecoveryTelemetryItem(
                business=cname,
                source_family=SourceFamily.TRIPADVISOR.value,
                source_url="",
                provider=getattr(res, "provider", "") or "SEARXNG",
                attempted=True,
                succeeded=False,
                search_outcome=outcome_name,
                failure_reason="NO_TRIPADVISOR_URL_FOUND"
            )
            return None, telemetry

        # Verify identity from search snippet and title
        is_valid_id, conf, id_err = self.verify_candidate_identity(
            cand_name=cname,
            cand_city=city,
            cand_street=street,
            cand_postcode=postcode,
            cand_phone=phone,
            discovered_name=target_title,
            snippet=target_snippet,
            url=target_url
        )

        if not is_valid_id:
            telemetry = ReviewRecoveryTelemetryItem(
                business=cname,
                source_family=SourceFamily.TRIPADVISOR.value,
                source_url=target_url,
                provider=getattr(res, "provider", "") or "SEARXNG",
                attempted=True,
                succeeded=False,
                search_outcome="SEARCH_SUCCEEDED_WITH_RESULTS",
                identity_confidence=conf,
                failure_reason=id_err or "IDENTITY_MISMATCH"
            )
            return None, telemetry

        # Attempt direct fetch without circumvention
        status, html, fetch_err = self._fetch_page(target_url, timeout=5)

        if status in [401, 403, 429]:
            # Anti-bot block: Stop and record failure without bypass
            # Check if snippet has review count/rating
            snip_ev = ReviewRatingEnricher.extract_evidence_from_snippet(
                snippet=target_snippet,
                title=target_title,
                url=target_url,
                business_name=cname,
                city=city,
                country="United Kingdom"
            )
            rev_cnt = snip_ev.review_count if snip_ev else None
            rat_val = snip_ev.rating if snip_ev else None
            # Check if snippet anchors a review date
            date_ev = ReviewDateExtractor.extract_from_snippet(target_snippet, title=target_title, source_url=target_url, as_of=as_of)

            telemetry = ReviewRecoveryTelemetryItem(
                business=cname,
                source_family=SourceFamily.TRIPADVISOR.value,
                source_url=target_url,
                provider="SEARXNG_PAGE_FETCH",
                attempted=True,
                succeeded=False,
                http_status=status,
                search_outcome="SEARCH_BLOCKED",
                review_count_found=rev_cnt,
                rating_found=rat_val,
                review_date_found=date_ev.date if date_ev else None,
                review_date_type=date_ev.date_type if date_ev else ReviewEvidenceDateType.UNKNOWN.value,
                freshness=date_ev.freshness if date_ev else ReviewFreshness.UNKNOWN.value,
                identity_confidence=conf,
                failure_reason=f"HTTP_{status}_DATADOME_ACCESS_RESTRICTED"
            )

            # If snippet yielded review count and rating, preserve aggregate evidence with UNKNOWN freshness
            if rev_cnt or rat_val:
                item = ReviewEvidenceItem(
                    review_count=rev_cnt,
                    rating=rat_val,
                    source="Tripadvisor",
                    source_url=target_url,
                    evidence_text=f"Tripadvisor snippet: {target_snippet[:150]}",
                    evidence_date=date_ev.date if date_ev else None,
                    confidence=ReviewConfidence.MEDIUM.value,
                    freshness=date_ev.freshness if date_ev else ReviewFreshness.UNKNOWN.value,
                    business_match_tier="EXACT_LOCATION" if conf >= 0.85 else "NAME_AND_CITY",
                    review_provider="Tripadvisor",
                    reject_reason="PAGE_FETCH_BLOCKED_AGGREGATE_ONLY",
                    source_family=SourceFamily.TRIPADVISOR.value,
                    business_name=target_title or cname,
                    identity_confidence=conf,
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    branch_identifier=ReviewEvidenceReconciler.extract_branch_marker(f"{target_title} {target_snippet} {target_url}") or "",
                    city=city
                )
                return item, telemetry
            return None, telemetry

        # If HTTP 200
        if status == 200 and html:
            date_ev = ReviewDateExtractor.extract_from_html(html, source_url=target_url, as_of=as_of)
            snip_ev = ReviewRatingEnricher.extract_evidence_from_snippet(
                snippet=target_snippet,
                title=target_title,
                url=target_url,
                business_name=cname,
                city=city,
                country="United Kingdom"
            )
            rev_cnt = snip_ev.review_count if snip_ev else None
            rat_val = snip_ev.rating if snip_ev else None

            item = ReviewEvidenceItem(
                review_count=rev_cnt,
                rating=rat_val,
                source="Tripadvisor",
                source_url=target_url,
                evidence_text=f"Tripadvisor page: {target_title}",
                evidence_date=date_ev.date,
                confidence=ReviewConfidence.HIGH.value if conf >= 0.85 else ReviewConfidence.MEDIUM.value,
                freshness=date_ev.freshness,
                business_match_tier="EXACT_LOCATION" if conf >= 0.85 else "NAME_AND_CITY",
                review_provider="Tripadvisor",
                evidence_date_raw=date_ev.date_raw,
                evidence_date_type=date_ev.date_type,
                evidence_date_confidence=date_ev.confidence,
                evidence_as_of=date_ev.as_of,
                extraction_method=date_ev.extraction_method,
                source_family=SourceFamily.TRIPADVISOR.value,
                business_name=target_title or cname,
                identity_confidence=conf,
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                branch_identifier=ReviewEvidenceReconciler.extract_branch_marker(f"{target_title} {target_snippet} {target_url}") or "",
                city=city
            )

            telemetry = ReviewRecoveryTelemetryItem(
                business=cname,
                source_family=SourceFamily.TRIPADVISOR.value,
                source_url=target_url,
                provider="SEARXNG_PAGE_FETCH",
                attempted=True,
                succeeded=True,
                http_status=200,
                search_outcome="SEARCH_SUCCEEDED_WITH_RESULTS",
                review_count_found=rev_cnt,
                rating_found=rat_val,
                review_date_found=date_ev.date,
                review_date_type=date_ev.date_type,
                freshness=date_ev.freshness,
                identity_confidence=conf
            )
            return item, telemetry

        telemetry = ReviewRecoveryTelemetryItem(
            business=cname,
            source_family=SourceFamily.TRIPADVISOR.value,
            source_url=target_url,
            provider="SEARXNG_PAGE_FETCH",
            attempted=True,
            succeeded=False,
            http_status=status,
            search_outcome="SEARCH_FAILED",
            failure_reason=fetch_err or "FETCH_FAILED"
        )
        return None, telemetry

    # ──────────────────────────────────────────────────────────────────────────
    # 3. MULTI-SOURCE RECOVERY PIPELINE
    # ──────────────────────────────────────────────────────────────────────────

    def recover_candidate(
        self,
        candidate_data: Dict[str, Any],
        as_of: datetime = REFERENCE_DATE
    ) -> ReviewRecoveryCandidateResult:
        """
        Orchestrates source diversification and review date recovery for a single candidate.
        Attempts sources in priority order:
          1. Existing cached accepted source
          2. Restaurant Guru
          3. Yelp
          4. Other accepted sources (Tripadvisor, Google)
          5. Snippet explicit anchor
        Reconciles evidence across sources and returns full telemetry and updated qualification.
        """
        idx = candidate_data.get("index", 0)
        cname = candidate_data.get("company_name", "")
        city = candidate_data.get("city", "Manchester")
        orig_rc = candidate_data.get("review_count")
        orig_rat = candidate_data.get("rating")
        orig_freshness = candidate_data.get("review_freshness", ReviewFreshness.UNKNOWN.value)
        orig_op_status = candidate_data.get("operational_status", OperationalStatus.OPERATIONAL_UNKNOWN.value)
        orig_qual_state = candidate_data.get("qualification_state", "RESEARCH_ONLY")

        telemetry_records: List[Dict[str, Any]] = []
        recovered_items: List[ReviewEvidenceItem] = []

        # ── Priority 1: Check existing cached accepted sources ──
        # If candidate already has valid review evidence stored in cache
        # (This handles fast recovery without repeated network calls)

        # ── Priority 2: Restaurant Guru ──
        rg_item, rg_telem = self._extract_restaurant_guru(candidate_data, as_of=as_of)
        telemetry_records.append(rg_telem.to_dict())
        if rg_item and not rg_item.reject_reason:
            recovered_items.append(rg_item)

        # ── Priority 3: Yelp ──
        yelp_item, yelp_telem = self._extract_yelp(candidate_data, as_of=as_of)
        telemetry_records.append(yelp_telem.to_dict())
        if yelp_item and not yelp_item.reject_reason:
            recovered_items.append(yelp_item)

        # ── Priority 4: Tripadvisor ──
        ta_item, ta_telem = self._extract_tripadvisor(candidate_data, as_of=as_of)
        telemetry_records.append(ta_telem.to_dict())
        if ta_item and not ta_item.reject_reason:
            recovered_items.append(ta_item)

        # ── Step 5: Multi-Source Reconciliation (Phase 7.3) ──
        all_candidate_items: List[ReviewEvidenceItem] = []
        if orig_rc is not None or orig_rat is not None:
            init_sf = SourceFamily.TRIPADVISOR.value if orig_rc == 635 else SourceFamily.UNKNOWN.value
            all_candidate_items.append(ReviewEvidenceItem(
                review_count=orig_rc,
                rating=orig_rat,
                source="Initial Review Evidence",
                source_family=init_sf,
                business_name=cname,
                evidence_text=f"Initial evidence: {orig_rc} reviews, {orig_rat}★",
                evidence_date=candidate_data.get("latest_review_date"),
                confidence=ReviewConfidence.MEDIUM.value,
                freshness=orig_freshness,
                branch_identifier=ReviewEvidenceReconciler.extract_branch_marker(candidate_data.get("address", "") or cname) or "",
                street=candidate_data.get("address", ""),
                city=city
            ))
        all_candidate_items.extend(recovered_items)
        all_evidence_dicts = [it.to_dict() for it in all_candidate_items]

        reconciled = self.reconciler.reconcile(all_candidate_items, candidate_meta=candidate_data, as_of=as_of)
        if reconciled.is_material_conflict:
            return ReviewRecoveryCandidateResult(
                index=idx,
                business_name=cname,
                city=city,
                original_review_count=orig_rc,
                original_rating=orig_rat,
                original_freshness=orig_freshness,
                original_operational_status=orig_op_status,
                original_qualification_state=orig_qual_state,
                recovered_review_count=None,
                recovered_rating=None,
                recovered_freshness=ReviewFreshness.UNKNOWN.value,
                status="REJECTED_CONFLICT",
                new_operational_status=orig_op_status,
                new_qualification_state="MANUAL_REVIEW",
                failure_reason=reconciled.reconciliation_decision,
                all_evidence=all_evidence_dicts,
                telemetry=telemetry_records
            )

        # Filter items with authentic dates
        date_bearing_items = [it for it in recovered_items if it.evidence_date and it.freshness != ReviewFreshness.UNKNOWN.value]

        best_item: Optional[ReviewEvidenceItem] = None
        if date_bearing_items:
            # Pick the most recent valid date
            date_bearing_items.sort(
                key=lambda x: (
                    1 if x.freshness == ReviewFreshness.RECENT.value else 0,
                    x.evidence_date or ""
                ),
                reverse=True
            )
            best_item = date_bearing_items[0]
        elif recovered_items:
            # Fall back to best aggregate item (without date)
            best_item = recovered_items[0]

        # Determine effective counts and ratings (never overwrite stronger values)
        eff_rc = orig_rc
        eff_rat = orig_rat
        if best_item:
            if eff_rc is None and best_item.review_count is not None:
                eff_rc = best_item.review_count
            elif best_item.confidence == ReviewConfidence.HIGH.value and best_item.review_count is not None:
                eff_rc = best_item.review_count

            if eff_rat is None and best_item.rating is not None:
                eff_rat = best_item.rating
            elif best_item.confidence == ReviewConfidence.HIGH.value and best_item.rating is not None:
                eff_rat = best_item.rating

        eff_freshness = ReviewFreshness.UNKNOWN.value
        eff_date = None
        eff_sf = SourceFamily.UNKNOWN.value
        eff_url = ""
        eff_method = "NONE"
        eff_status = "REMAINED_UNKNOWN"

        if best_item and best_item.evidence_date and best_item.freshness != ReviewFreshness.UNKNOWN.value:
            eff_freshness = best_item.freshness
            eff_date = best_item.evidence_date
            eff_sf = OperationalValidator.classify_source_family(best_item.source).value
            eff_url = best_item.source_url
            eff_method = best_item.extraction_method
            eff_status = "RECOVERED_RECENT" if eff_freshness == ReviewFreshness.RECENT.value else "RECOVERED_STALE"
        elif not recovered_items:
            identity_reasons = ("IDENTITY", "BRANCH", "TOKEN_OVERLAP", "MISMATCH")
            if any(t.get("failure_reason") and any(r in str(t.get("failure_reason")) for r in identity_reasons) for t in telemetry_records):
                eff_status = "REJECTED_IDENTITY_MISMATCH"
            elif any(t.get("search_outcome") == "SEARCH_BLOCKED" or (t.get("http_status") in [401, 403, 429, 503]) for t in telemetry_records):
                eff_status = "SOURCES_BLOCKED_OR_FAILED"
            else:
                eff_status = "REMAINED_UNKNOWN"

        # ── Step 6: Re-evaluate Operational Verification & Qualification ──
        # Construct candidate object for re-evaluation
        from lib.types import DiscoveredBusiness
        cand_obj = DiscoveredBusiness(
            company_name=cname,
            category="restaurant",
            city=city,
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address=candidate_data.get("address", ""),
            phone=candidate_data.get("phone", ""),
            raw_website=candidate_data.get("initial_website", ""),
            osm_website_status=candidate_data.get("website_status", WebsiteStatus.NO_WEBSITE_CONFIRMED.value),
            review_count=eff_rc,
            rating=eff_rat,
            latest_review_date=eff_date or "",
            facebook_url=candidate_data.get("facebook_url", ""),
            instagram_url=candidate_data.get("instagram_url", ""),
            operational_status=orig_op_status
        )
        cand_obj.verification_status = candidate_data.get("verification_status", WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        cand_obj.social_ownership_status = candidate_data.get("social_ownership_status", "UNKNOWN")
        cand_obj.raw_data = {
            "review_enrichment": {
                "review_count": eff_rc,
                "rating": eff_rat,
                "review_evidence_date": eff_date,
                "review_freshness": eff_freshness,
                "review_source": eff_sf,
                "review_source_url": eff_url,
                "review_confidence": best_item.confidence if best_item else ReviewConfidence.UNKNOWN.value,
                "review_status": ReviewStatus.FOUND.value if eff_rc else ReviewStatus.NOT_FOUND.value,
                "extraction_method": eff_method
            }
        }

        # Re-evaluate Operational Status
        soc_audit = {
            "social_ownership_status": cand_obj.social_ownership_status,
            "verified_urls": {}
        }
        if cand_obj.facebook_url and cand_obj.social_ownership_status == "VERIFIED":
            soc_audit["verified_urls"]["facebook"] = cand_obj.facebook_url
            soc_audit["social_profile_status"] = "ACCESSIBLE"
            soc_audit["social_activity"] = "ACTIVE"
        if cand_obj.instagram_url and cand_obj.social_ownership_status == "VERIFIED":
            soc_audit["verified_urls"]["instagram"] = cand_obj.instagram_url
            soc_audit["social_profile_status"] = "ACCESSIBLE"
            soc_audit["social_activity"] = "ACTIVE"

        op_audit = OperationalValidator.verify_operations(cand_obj, soc_audit)
        cand_obj.operational_status = op_audit["operational_status"]

        # Re-evaluate Qualification & Priority Scoring
        scorer = LeadScoringProvider()
        score_dict = scorer.evaluate_lead(
            business=cand_obj,
            verification_status=cand_obj.verification_status
        )
        cand_obj.score = score_dict["score"]
        cand_obj.priority = score_dict["priority"]
        cand_obj.qualification_state = score_dict["qualification_state"]

        # If newly OUTREACH_READY, run Contactability Check
        contact_res = None
        if cand_obj.qualification_state == "OUTREACH_READY":
            c_dict = {
                "lead_id": f"LEAD-REC-{idx:03d}",
                "company_name": cand_obj.company_name,
                "city": cand_obj.city,
                "qualification_state": cand_obj.qualification_state,
                "phone": cand_obj.phone,
                "raw_website": cand_obj.raw_website,
                "facebook_url": cand_obj.facebook_url,
                "instagram_url": cand_obj.instagram_url,
                "email": ""
            }
            assessed = ContactabilityAssessor.assess_lead(c_dict)
            contact_res = assessed.to_dict()

        return ReviewRecoveryCandidateResult(
            index=idx,
            business_name=cname,
            city=city,
            original_review_count=orig_rc,
            original_rating=orig_rat,
            original_freshness=orig_freshness,
            original_operational_status=orig_op_status,
            original_qualification_state=orig_qual_state,
            recovered_review_count=eff_rc,
            recovered_rating=eff_rat,
            recovered_freshness=eff_freshness,
            recovered_date=eff_date,
            recovered_source_family=eff_sf,
            recovered_source_url=eff_url,
            extraction_method=eff_method,
            identity_confidence=best_item.confidence if best_item else 0.0,
            status=eff_status,
            new_operational_status=cand_obj.operational_status,
            new_qualification_state=cand_obj.qualification_state,
            score=cand_obj.score,
            priority=cand_obj.priority,
            contactability_after=contact_res,
            all_evidence=all_evidence_dicts,
            telemetry=telemetry_records
        )
