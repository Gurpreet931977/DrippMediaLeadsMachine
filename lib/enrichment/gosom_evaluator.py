"""
Dripp Media — Gosom Google Maps Scraper Review Evaluator
=========================================================
Phase 7.4A: Local Open-Source Google Maps Scraper Evaluation.

Parses, normalizes, and reconciles review evidence scraped locally via
gosom/google-maps-scraper without using Apify, Google Places API, proxies,
or anti-bot circumvention.

Key Responsibilities:
  1. Parse gosom user_reviews[]:
     - Extracts published_at (RFC 3339 / ISO timestamp).
     - Extracts posted_at_unix_micros / updated_at_unix_micros.
     - Extracts When (relative date string: "a month ago", "Edited 8 months ago").
     - Distinguishes edited vs original review timestamps.
  2. Bounded freshness classification:
     - <= 180 days -> RECENT
     - > 180 days  -> STALE
     - missing     -> UNKNOWN
  3. Strict identity & branch divergence protection:
     - Compares candidate identity vs scraped place identity (BusinessIdentityMatcher).
     - Isolates branch markers (Barton Arcade vs Airport Terminal 2 vs Angel Gardens).
  4. Multi-source reconciliation:
     - Integrates with ReviewEvidenceReconciler under SourceFamily.GOOGLE.
  5. Invariants:
     - Apify calls = 0, Apify spend = $0.00
     - CRM mutations = 0, Outreach sent = 0
     - Proxies = 0, Circumvention = 0
"""

import os
import re
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple, Set

from lib.types import DiscoveredBusiness, SourceFamily
from lib.crm.identity_matcher import BusinessIdentityMatcher, clean_ascii_text
from lib.enrichment.review_date_extractor import (
    ReviewDateExtractor,
    ReviewEvidenceDateType,
    ReviewDateConfidence,
    ReviewFreshness,
)
from lib.enrichment.review_rating_enricher import (
    ReviewEvidenceItem,
    ReviewConfidence,
    ReviewStatus,
    REFERENCE_DATE,
    RECENT_THRESHOLD_DAYS,
)
from lib.enrichment.review_reconciler import (
    ReviewConflictType,
    ReconciledReviewEvidence,
    ReviewEvidenceReconciler,
)


class GosomReviewParser:
    """
    Parses and normalizes individual review records produced by gosom/google-maps-scraper.
    """

    @staticmethod
    def parse_review_timestamp(raw_ts: Optional[str]) -> Optional[str]:
        """
        Parses RFC 3339 / ISO 8601 UTC timestamp string to ISO date YYYY-MM-DD.
        """
        if not raw_ts or not isinstance(raw_ts, str):
            return None
        raw_clean = raw_ts.strip()
        m = re.match(r'^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$', raw_clean)
        if m:
            return m.group(1)
        # Fallback to ReviewDateExtractor normalize_absolute_date
        norm_date, _ = ReviewDateExtractor.normalize_absolute_date(raw_clean)
        return norm_date

    @staticmethod
    def parse_unix_micros(micros: Optional[int]) -> Optional[str]:
        """
        Converts integer Unix microsecond timestamp to ISO UTC date YYYY-MM-DD.
        Valid range: 2007-01-01 to near future.
        """
        if not micros or not isinstance(micros, (int, float)):
            return None
        try:
            val = int(micros)
            if val <= 0:
                return None
            dt = datetime.fromtimestamp(val / 1_000_000.0, tz=timezone.utc)
            if dt.year < 2007 or dt.year > 2030:
                return None
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            return None

    @classmethod
    def parse_single_review(
        cls,
        rev: Dict[str, Any],
        place_meta: Dict[str, Any],
        candidate_meta: Dict[str, Any],
        as_of: datetime = REFERENCE_DATE
    ) -> Optional[ReviewEvidenceItem]:
        """
        Extracts a single ReviewEvidenceItem from a gosom user_reviews item.
        Preserves provenance, distinguishes edited vs original, never fabricates dates.
        """
        if not isinstance(rev, dict):
            return None

        r_name = rev.get("Name") or rev.get("author_name") or ""
        r_rating = rev.get("Rating") if rev.get("Rating") is not None else rev.get("rating")
        r_desc = rev.get("Description") or rev.get("text") or ""
        r_when = rev.get("When") or rev.get("relative_time_description") or ""
        r_pub = rev.get("published_at")
        r_posted_micros = rev.get("posted_at_unix_micros")
        r_updated_micros = rev.get("updated_at_unix_micros")
        r_id = rev.get("review_id") or ""
        r_author_url = rev.get("author_url") or ""

        # Determine if review is edited
        is_edited = False
        if r_when and "edited" in r_when.lower():
            is_edited = True
        elif r_posted_micros and r_updated_micros and r_updated_micros > r_posted_micros:
            is_edited = True

        # Extract publication date
        parsed_date = None
        raw_date_text = None
        date_type = ReviewEvidenceDateType.UNKNOWN.value
        confidence = ReviewDateConfidence.UNKNOWN.value

        # 1. Primary: published_at (RFC 3339)
        if r_pub:
            norm_pub = cls.parse_review_timestamp(r_pub)
            if norm_pub:
                parsed_date = norm_pub
                raw_date_text = r_pub
                date_type = ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value
                confidence = ReviewDateConfidence.HIGH.value

        # 2. Secondary: posted_at_unix_micros
        if not parsed_date and r_posted_micros:
            norm_micros = cls.parse_unix_micros(r_posted_micros)
            if norm_micros:
                parsed_date = norm_micros
                raw_date_text = str(r_posted_micros)
                date_type = ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value
                confidence = ReviewDateConfidence.HIGH.value

        # 3. Tertiary: When (relative string description)
        if not parsed_date and r_when:
            clean_when = re.sub(r'^(?:edited\s+)', '', r_when.strip(), flags=re.IGNORECASE)
            norm_rel, raw_rel, days_rel = ReviewDateExtractor.normalize_relative_date(clean_when, as_of)
            if norm_rel:
                parsed_date = norm_rel
                raw_date_text = r_when
                date_type = ReviewEvidenceDateType.REVIEW_RELATIVE_AGE.value
                confidence = ReviewDateConfidence.MEDIUM.value

        if not parsed_date:
            return None

        business_title = place_meta.get("title") or candidate_meta.get("company_name", "")
        source_url = place_meta.get("link") or f"https://www.google.com/maps/place/?q=place_id:{place_meta.get('place_id', '')}"

        edit_label = " [EDITED]" if is_edited else ""
        item = ReviewEvidenceItem(
            business_name=business_title,
            source="Google Maps (gosom)",
            source_family=SourceFamily.GOOGLE.value,
            source_url=source_url,
            rating=float(r_rating) if r_rating is not None else None,
            review_count=place_meta.get("review_count"),
            evidence_date=parsed_date,
            evidence_date_raw=raw_date_text,
            evidence_date_type=date_type,
            evidence_date_confidence=confidence,
            evidence_as_of=as_of.isoformat(),
            extraction_method="GOSOM_LOCAL_SCRAPER",
            evidence_text=f"Google review by {r_name}{edit_label}: rating {r_rating} date {parsed_date} ('{raw_date_text}')"
        )
        return item


class GosomPlaceEnricher:
    """
    Coordinates matching of candidates against gosom-scraped Google Maps places,
    extracts review evidence, enforces branch safety, and reconciles multi-source data.
    """

    def __init__(self, matcher: Optional[BusinessIdentityMatcher] = None):
        self.matcher = matcher or BusinessIdentityMatcher()
        self.apify_calls = 0
        self.google_places_api_calls = 0

    def match_place_to_candidate(
        self,
        candidate_meta: Dict[str, Any],
        scraped_places: List[Dict[str, Any]]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], float]:
        """
        Finds the matching Google Maps place for a given candidate among scraped places.
        Enforces composite identity threshold >= 0.70 and detects branch divergence.
        Returns: (matched_place_dict, error_reason, confidence_score)
        """
        assert self.apify_calls == 0, "Invariant violation: Apify must not be called"
        assert self.google_places_api_calls == 0, "Invariant violation: Google Places API must not be called"

        c_name = candidate_meta.get("company_name") or candidate_meta.get("business_name") or ""
        c_city = candidate_meta.get("city", "Manchester")
        c_addr = candidate_meta.get("address", "")
        c_street = candidate_meta.get("street", "")
        c_postcode = candidate_meta.get("postcode", "")
        c_phone = candidate_meta.get("phone", "")

        best_place = None
        best_conf = 0.0
        branch_divergence_found = False

        for p in scraped_places:
            p_name = p.get("title", "")
            p_addr = p.get("address", "")
            p_phone = p.get("phone", "")
            p_city = "Manchester" if "manchester" in p_addr.lower() else ""
            combined_place = f"{p_name} {p_addr}".lower()

            # Check branch divergence (e.g. Barton Arcade vs Terminal 2 vs Angel Gardens)
            b_cand = ReviewEvidenceReconciler.extract_branch_marker(f"{c_name} {c_addr}")
            b_place = ReviewEvidenceReconciler.extract_branch_marker(f"{p_name} {p_addr}")
            has_divergence = False
            if b_cand and b_place and b_cand != b_place:
                has_divergence = True
            elif b_place and any(kw in b_place for kw in ["terminal", "airport"]):
                cand_full = f"{c_name} {c_addr}".lower()
                if "terminal" not in cand_full and "airport" not in cand_full:
                    has_divergence = True

            if has_divergence:
                branch_divergence_found = True
                continue

            cand_dict = {
                "company_name": c_name,
                "city": c_city,
                "address": c_addr,
                "street": c_street,
                "postcode": c_postcode,
                "phone": c_phone,
                "target_country": "GB"
            }
            place_dict = {
                "company_name": p_name,
                "city": p_city or c_city,
                "address": p_addr,
                "phone": p_phone,
                "target_country": "GB",
                "website": p.get("web_site", "")
            }

            conf, reasons, conflict = self.matcher.compute_similarity(cand_dict, place_dict)

            # Established pattern from ReviewRecoveryEnricher:
            # If candidate lacks street/postcode/phone, fallback to name similarity + city corroboration
            if conf < 0.70 and not c_street and not c_postcode and not c_phone:
                name_score, name_reasons, name_comp = self.matcher.compute_name_similarity(c_name, p_name)
                if name_score >= 0.85 and name_comp and c_city.lower() in combined_place:
                    conf = max(conf, name_score * 0.95)

            if conf > best_conf:
                best_conf = conf
                if conf >= 0.70:
                    best_place = p

        if best_place and best_conf >= 0.70:
            return best_place, None, best_conf
        elif branch_divergence_found:
            return None, "BRANCH_DIFFERENCE", best_conf
        else:
            return None, "IDENTITY_CONFIDENCE_INSUFFICIENT", best_conf

    def extract_place_review_evidence(
        self,
        place: Dict[str, Any],
        candidate_meta: Dict[str, Any],
        as_of: datetime = REFERENCE_DATE
    ) -> Tuple[Optional[ReviewEvidenceItem], List[ReviewEvidenceItem], Optional[str]]:
        """
        Extracts review items and composite place review item from matched place.
        Returns: (primary_review_item, extracted_items, error_reason)
        """
        raw_reviews = place.get("user_reviews") or []
        if not raw_reviews:
            return None, [], "NO_REVIEWS_IN_PAYLOAD"

        extracted_items: List[ReviewEvidenceItem] = []
        best_date = None
        best_date_raw = None

        for rev in raw_reviews:
            item = GosomReviewParser.parse_single_review(rev, place, candidate_meta, as_of)
            if item and item.evidence_date:
                extracted_items.append(item)
                if best_date is None or item.evidence_date > best_date:
                    best_date = item.evidence_date
                    best_date_raw = item.evidence_date_raw

        freshness = ReviewFreshness.UNKNOWN.value
        if best_date:
            freshness = ReviewDateExtractor.calculate_freshness(best_date, as_of)

        p_title = place.get("title") or candidate_meta.get("company_name", "")
        p_addr = place.get("address", "")
        rc = place.get("review_count")
        rat = place.get("review_rating")
        place_id = place.get("place_id") or place.get("data_id") or ""
        source_url = place.get("link") or f"https://www.google.com/maps/place/?q=place_id:{place_id}"

        branch_marker = ReviewEvidenceReconciler.extract_branch_marker(f"{p_title} {p_addr}")
        outcode = ReviewEvidenceReconciler.extract_uk_outcode(p_addr)

        primary_item = ReviewEvidenceItem(
            business_name=p_title,
            source="Google Maps (gosom)",
            source_family=SourceFamily.GOOGLE.value,
            source_url=source_url,
            rating=float(rat) if rat is not None else None,
            review_count=rc,
            evidence_date=best_date,
            evidence_date_raw=best_date_raw,
            evidence_date_type=ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value if best_date else ReviewEvidenceDateType.UNKNOWN.value,
            evidence_date_confidence=ReviewDateConfidence.HIGH.value if best_date else ReviewDateConfidence.UNKNOWN.value,
            freshness=freshness,
            confidence=ReviewConfidence.HIGH.value if best_date else ReviewConfidence.MEDIUM.value,
            branch_identifier=branch_marker,
            postcode=outcode,
            city=candidate_meta.get("city", "Manchester"),
            extraction_method="GOSOM_LOCAL_SCRAPER",
            evidence_text=f"Google Maps via gosom ({place_id}): {rc} reviews, {rat} rating, latest review date {best_date or 'NONE'}"
        )

        return primary_item, extracted_items, None
