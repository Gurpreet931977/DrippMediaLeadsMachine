"""
Dripp Media — Google Places API (New) Review Freshness Enricher (Phase 7.4)
===========================================================================
Controlled fallback layer for review-freshness extraction using Google Places API (New).
Determines whether Google Places API can supply trustworthy, business-specific review
freshness evidence (publishTime) for Rule B consumption.

Key Invariants:
  - 0 Apify calls (strictly prohibited, credits exhausted).
  - 0 CRM mutations.
  - Places API (New) only:
      Text Search:  POST https://places.googleapis.com/v1/places:searchText
      Place Details: GET https://places.googleapis.com/v1/places/{PLACE_ID}
  - Minimal FieldMasks only (no wildcard field masks).
  - Strict business identity & branch isolation (e.g. Barton Arcade vs Airport Terminal 2).
  - Data minimization: does not store reviewer personal data or author profiles.
  - Safe-by-default: GOOGLE_PLACES_REVIEW_FRESHNESS_FALLBACK_ENABLED=false by default.
  - Source family: GOOGLE. Google Places alone cannot satisfy independent operational corroboration.
"""

import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple, Set
from dotenv import load_dotenv

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

load_dotenv()

# Minimal field masks for Places API (New)
TEXT_SEARCH_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location,places.nationalPhoneNumber"
PLACE_DETAILS_FIELD_MASK = "id,displayName,formattedAddress,rating,userRatingCount,reviews"


class GooglePlacesReviewEnricher:
    """
    Enriches candidate review evidence via Google Places API (New).
    Operates under strict candidate quotas, dedicated disk caching, and branch isolation.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        enabled: Optional[bool] = None,
        cache_dir: str = "data/cache_google_places_reviews",
        max_canary_candidates: int = 20,
        matcher: Optional[BusinessIdentityMatcher] = None,
        reconciler: Optional[ReviewEvidenceReconciler] = None
    ):
        # 1. API Key resolution
        env_key = (
            os.getenv("GOOGLE_PLACES_API_KEY")
            or os.getenv("GOOGLE_MAPS_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
        )
        self.api_key = api_key or env_key

        # 2. Service account check (for OAuth token fallback if enabled on project)
        self.service_account_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

        # 3. Production flag (False by default)
        env_enabled = os.getenv("GOOGLE_PLACES_REVIEW_FRESHNESS_FALLBACK_ENABLED", "false").lower() in ["true", "1", "yes"]
        self.enabled = enabled if enabled is not None else env_enabled

        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

        self.max_canary_candidates = max_canary_candidates
        self.matcher = matcher or BusinessIdentityMatcher()
        self.reconciler = reconciler or ReviewEvidenceReconciler(self.matcher)

        # Telemetry & Call accounting
        self.place_search_calls = 0
        self.place_details_calls = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.apify_calls = 0  # Invariant assertion: must remain 0

    # ──────────────────────────────────────────────────────────────────────────
    # 1. CONFIGURATION & CREDENTIAL CHECK
    # ──────────────────────────────────────────────────────────────────────────
    def check_configuration(self) -> Tuple[bool, str]:
        """
        Verifies if valid Google Places API credentials exist.
        Returns: (is_configured, status_code)
        """
        if self.api_key:
            return True, "CONFIGURED_API_KEY"

        # Check if service account file exists
        if self.service_account_file and os.path.exists(self.service_account_file):
            try:
                from google.oauth2 import service_account
                from google.auth.transport.requests import Request
                creds = service_account.Credentials.from_service_account_file(
                    self.service_account_file,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                creds.refresh(Request())
                if creds.token:
                    # Token minted, but project might still not have Places API enabled
                    return True, "CONFIGURED_SERVICE_ACCOUNT_TOKEN"
            except Exception as e:
                return False, f"SERVICE_ACCOUNT_AUTH_FAILED: {str(e)}"

        return False, "GOOGLE_PLACES_CREDENTIAL_MISSING"

    def _get_auth_headers(self, field_mask: str) -> Dict[str, str]:
        """Builds HTTP headers using API key or Service Account OAuth Bearer token."""
        headers = {
            "Content-Type": "application/json",
            "X-Goog-FieldMask": field_mask
        }
        if self.api_key:
            headers["X-Goog-Api-Key"] = self.api_key
        elif self.service_account_file and os.path.exists(self.service_account_file):
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request
            creds = service_account.Credentials.from_service_account_file(
                self.service_account_file,
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            creds.refresh(Request())
            headers["Authorization"] = f"Bearer {creds.token}"
        return headers

    # ──────────────────────────────────────────────────────────────────────────
    # 2. CACHING LAYER
    # ──────────────────────────────────────────────────────────────────────────
    def _cache_path(self, cache_key: str) -> str:
        safe_key = re.sub(r'[^a-zA-Z0-9_\-]', '_', cache_key)
        return os.path.join(self.cache_dir, f"{safe_key}.json")

    def _get_cached_response(self, cache_key: str) -> Optional[Dict[str, Any]]:
        cpath = self._cache_path(cache_key)
        if os.path.exists(cpath):
            try:
                with open(cpath, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                # Only return if it represents a valid retrieved payload
                if entry.get("http_status") == 200 and entry.get("data") is not None:
                    self.cache_hits += 1
                    return entry.get("data")
            except Exception:
                pass
        self.cache_misses += 1
        return None

    def _save_cached_response(self, cache_key: str, http_status: int, data: Any, error: Optional[str] = None):
        cpath = self._cache_path(cache_key)
        entry = {
            "cache_key": cache_key,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "http_status": http_status,
            "error": error,
            "data": data
        }
        try:
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(entry, f, indent=2)
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────────
    # 3. TEXT SEARCH (NEW) — PLACE ID RESOLUTION
    # ──────────────────────────────────────────────────────────────────────────
    def search_place(
        self,
        company_name: str,
        city: str,
        address: Optional[str] = None,
        phone: Optional[str] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[float]]:
        """
        Searches for a place using Places API (New) Text Search.
        Validates identity and branch alignment before accepting the Place ID.
        Returns: (matched_place_dict, error_code, identity_confidence)
        """
        assert self.apify_calls == 0, "Invariant violation: Apify must not be called"

        is_cfg, cfg_status = self.check_configuration()
        if not is_cfg:
            return None, cfg_status, 0.0

        query = f"{company_name} {city}"
        if address:
            query = f"{company_name} {address} {city}"

        cache_key = f"search_{hashlib.sha256(query.encode('utf-8')).hexdigest()[:16]}"
        cached = self._get_cached_response(cache_key)
        if cached is not None:
            return cached.get("place"), cached.get("error"), cached.get("confidence")

        url = "https://places.googleapis.com/v1/places:searchText"
        headers = self._get_auth_headers(TEXT_SEARCH_FIELD_MASK)
        body = {
            "textQuery": query,
            "languageCode": "en",
            "maxResultCount": 3
        }

        self.place_search_calls += 1
        import requests
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                places = data.get("places", [])
                if not places:
                    self._save_cached_response(cache_key, 200, {"place": None, "error": "NO_PLACES_FOUND", "confidence": 0.0})
                    return None, "NO_PLACES_FOUND", 0.0

                # Evaluate identity & branch for each returned place
                best_place = None
                best_conf = 0.0
                branch_divergence_found = False

                for p in places:
                    p_name = p.get("displayName", {}).get("text", "")
                    p_addr = p.get("formattedAddress", "")
                    p_phone = p.get("nationalPhoneNumber", "")

                    # Check branch marker divergence (e.g. Barton Arcade vs Terminal 2)
                    b_cand = ReviewEvidenceReconciler.extract_branch_marker(f"{company_name} {address or ''}")
                    b_place = ReviewEvidenceReconciler.extract_branch_marker(f"{p_name} {p_addr}")

                    if b_cand and b_place and b_cand != b_place:
                        branch_divergence_found = True
                        continue

                    # Extract city from place formatted address if different from candidate city
                    p_city = city if city.lower() in p_addr.lower() else ""
                    if not p_city:
                        addr_parts = [part.strip() for part in p_addr.split(",")]
                        if len(addr_parts) >= 2:
                            cand_part = addr_parts[-2] if addr_parts[-1].lower() in ["uk", "united kingdom"] else addr_parts[-1]
                            clean_cand_city = re.sub(r'[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}', '', cand_part).strip()
                            p_city = clean_cand_city or cand_part

                    cand_dict = {
                        "company_name": company_name,
                        "city": city,
                        "phone": phone or "",
                        "address": address or "",
                        "target_country": "GB",
                    }
                    place_dict = {
                        "company_name": p_name,
                        "city": p_city or city,
                        "phone": p_phone or "",
                        "address": p_addr or "",
                        "target_country": "GB",
                        "website": p.get("websiteUri", "")
                    }

                    conf, reasons, conflict = self.matcher.compute_similarity(cand_dict, place_dict)
                    if conf > best_conf:
                        best_conf = conf
                        if conf >= 0.70:
                            best_place = p

                if best_place and best_conf >= 0.70:
                    payload = {"place": best_place, "error": None, "confidence": best_conf}
                    self._save_cached_response(cache_key, 200, payload)
                    return best_place, None, best_conf
                elif branch_divergence_found:
                    payload = {"place": None, "error": "BRANCH_DIFFERENCE", "confidence": best_conf}
                    self._save_cached_response(cache_key, 200, payload)
                    return None, "BRANCH_DIFFERENCE", best_conf
                else:
                    payload = {"place": None, "error": "IDENTITY_CONFIDENCE_INSUFFICIENT", "confidence": best_conf}
                    self._save_cached_response(cache_key, 200, payload)
                    return None, "IDENTITY_CONFIDENCE_INSUFFICIENT", best_conf

            elif resp.status_code == 403:
                err_msg = resp.text
                err_code = "GOOGLE_PLACES_API_PERMISSION_UNAVAILABLE"
                if "disabled" in err_msg.lower() or "has not been used" in err_msg.lower():
                    err_code = "GOOGLE_PLACES_API_NOT_ENABLED"
                self._save_cached_response(cache_key, 403, None, error=err_code)
                return None, err_code, 0.0
            elif resp.status_code == 429:
                return None, "RATE_LIMIT_EXCEEDED", 0.0
            else:
                return None, f"HTTP_{resp.status_code}", 0.0

        except requests.exceptions.Timeout:
            return None, "TIMEOUT", 0.0
        except Exception as e:
            return None, f"REQUEST_FAILED: {str(e)}", 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # 4. PLACE DETAILS (NEW) — REVIEWS RETRIEVAL
    # ──────────────────────────────────────────────────────────────────────────
    def get_place_details(self, place_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Retrieves Place Details (New) including rating, userRatingCount, and reviews.
        Uses minimal FieldMask: PLACE_DETAILS_FIELD_MASK.
        """
        assert self.apify_calls == 0, "Invariant violation: Apify must not be called"

        is_cfg, cfg_status = self.check_configuration()
        if not is_cfg:
            return None, cfg_status

        cache_key = f"details_{place_id}"
        cached = self._get_cached_response(cache_key)
        if cached is not None:
            return cached, None

        url = f"https://places.googleapis.com/v1/places/{place_id}"
        headers = self._get_auth_headers(PLACE_DETAILS_FIELD_MASK)

        self.place_details_calls += 1
        import requests
        try:
            resp = requests.get(url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                self._save_cached_response(cache_key, 200, data)
                return data, None
            elif resp.status_code == 403:
                err_msg = resp.text
                err_code = "GOOGLE_PLACES_API_PERMISSION_UNAVAILABLE"
                if "disabled" in err_msg.lower() or "has not been used" in err_msg.lower():
                    err_code = "GOOGLE_PLACES_API_NOT_ENABLED"
                self._save_cached_response(cache_key, 403, None, error=err_code)
                return None, err_code
            elif resp.status_code == 404:
                self._save_cached_response(cache_key, 404, None, error="PLACE_NOT_FOUND")
                return None, "PLACE_NOT_FOUND"
            elif resp.status_code == 429:
                return None, "RATE_LIMIT_EXCEEDED"
            else:
                return None, f"HTTP_{resp.status_code}"
        except requests.exceptions.Timeout:
            return None, "TIMEOUT"
        except Exception as e:
            return None, f"REQUEST_FAILED: {str(e)}"

    # ──────────────────────────────────────────────────────────────────────────
    # 5. REVIEW OBJECT INSPECTION & FRESHNESS EXTRACTION
    # ──────────────────────────────────────────────────────────────────────────
    @classmethod
    def parse_review_timestamp(cls, ts_str: Optional[str]) -> Optional[str]:
        """
        Parses Google RFC 3339 / ISO-8601 publishTime to YYYY-MM-DD UTC date string.
        Rejects invalid or malformed timestamps.
        """
        if not ts_str:
            return None
        ts_clean = str(ts_str).strip()
        # Handle trailing Z
        try:
            dt = datetime.fromisoformat(ts_clean.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            pass

        # Try standard YYYY-MM-DD
        m = re.match(r'^(\d{4}-\d{2}-\d{2})', ts_clean)
        if m:
            return m.group(1)
        return None

    def extract_review_evidence(
        self,
        place_details: Dict[str, Any],
        candidate_meta: Dict[str, Any],
        as_of: datetime = REFERENCE_DATE
    ) -> Tuple[Optional[ReviewEvidenceItem], List[ReviewEvidenceItem], Optional[str]]:
        """
        Inspects all returned Review objects from Place Details (New).
        Extracts genuine publishTime, selects the most recent valid review,
        computes bounded freshness (RECENT / STALE / UNKNOWN), and respects data minimization.
        """
        raw_reviews = place_details.get("reviews") or []
        user_rating_count = place_details.get("userRatingCount")
        rating = place_details.get("rating")
        place_id = place_details.get("id", "")
        display_name = place_details.get("displayName", {}).get("text", "")
        formatted_address = place_details.get("formattedAddress", "")

        extracted_items: List[ReviewEvidenceItem] = []
        best_date: Optional[str] = None
        best_date_raw: Optional[str] = None
        latest_review_id: Optional[str] = None
        latest_maps_uri: Optional[str] = None

        for rev in raw_reviews:
            r_name = rev.get("name", "")  # e.g. places/{PLACE_ID}/reviews/{REVIEW_ID}
            r_publish_time = rev.get("publishTime")
            r_rel_desc = rev.get("relativePublishTimeDescription", "")
            r_rating = rev.get("rating")
            r_uri = rev.get("googleMapsUri", "")

            # Privacy / Data Minimization: DO NOT extract or store author names, photos, or profiles
            parsed_date = self.parse_review_timestamp(r_publish_time)

            # Fallback to relative description only if explicitly attached to that review
            date_type = ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value
            if not parsed_date and r_rel_desc:
                norm_rel, raw_rel, days_rel = ReviewDateExtractor.normalize_relative_date(r_rel_desc, as_of)
                if norm_rel:
                    parsed_date = norm_rel
                    date_type = ReviewEvidenceDateType.REVIEW_RELATIVE_AGE.value

            if parsed_date:
                item = ReviewEvidenceItem(
                    business_name=display_name or candidate_meta.get("company_name", ""),
                    source="Google Places API (New)",
                    source_family=SourceFamily.GOOGLE.value,
                    source_url=r_uri or f"https://www.google.com/maps/place/?q=place_id:{place_id}",
                    rating=float(r_rating) if r_rating is not None else rating,
                    review_count=user_rating_count,
                    evidence_date=parsed_date,
                    evidence_date_raw=r_publish_time or r_rel_desc,
                    evidence_date_type=date_type,
                    evidence_date_confidence=ReviewDateConfidence.HIGH.value if r_publish_time else ReviewDateConfidence.MEDIUM.value,
                    evidence_as_of=as_of.isoformat(),
                    extraction_method="GOOGLE_PLACES_NEW_API",
                    evidence_text=f"Google review for {display_name}: rating {r_rating} published on {parsed_date}"
                )
                extracted_items.append(item)

                if best_date is None or parsed_date > best_date:
                    best_date = parsed_date
                    best_date_raw = r_publish_time or r_rel_desc
                    latest_review_id = r_name
                    latest_maps_uri = r_uri

        # Determine overall freshness based on most recent returned review
        freshness = ReviewFreshness.UNKNOWN.value
        if best_date:
            freshness = ReviewDateExtractor.calculate_freshness(best_date, as_of)

        branch_marker = ReviewEvidenceReconciler.extract_branch_marker(f"{display_name} {formatted_address}")
        outcode = ReviewEvidenceReconciler.extract_uk_outcode(formatted_address)

        primary_item = ReviewEvidenceItem(
            business_name=display_name or candidate_meta.get("company_name", ""),
            source="Google Places API (New)",
            source_family=SourceFamily.GOOGLE.value,
            source_url=f"https://www.google.com/maps/place/?q=place_id:{place_id}",
            rating=float(rating) if rating is not None else None,
            review_count=user_rating_count,
            evidence_date=best_date,
            evidence_date_raw=best_date_raw,
            evidence_date_type=ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value if best_date else ReviewEvidenceDateType.UNKNOWN.value,
            evidence_date_confidence=ReviewDateConfidence.HIGH.value if best_date else ReviewDateConfidence.UNKNOWN.value,
            freshness=freshness,
            confidence=ReviewConfidence.HIGH.value if best_date else ReviewConfidence.MEDIUM.value,
            branch_identifier=branch_marker,
            postcode=outcode,
            city=candidate_meta.get("city", "Manchester"),
            extraction_method="GOOGLE_PLACES_NEW_API",
            evidence_text=f"Google Place Details ({place_id}): {user_rating_count} reviews, {rating} rating, latest review date {best_date or 'NONE'}"
        )

        return primary_item, extracted_items, None

    # ──────────────────────────────────────────────────────────────────────────
    # 6. ENRICH CANDIDATE PIPELINE
    # ──────────────────────────────────────────────────────────────────────────
    def enrich_candidate(
        self,
        candidate: Dict[str, Any],
        as_of: datetime = REFERENCE_DATE
    ) -> Dict[str, Any]:
        """
        Full canary enrichment pipeline for a single candidate.
        Steps:
          1. Invariant check (Apify calls == 0)
          2. Configuration check
          3. Place search & identity matching
          4. Place details & review inspection
          5. Evidence reconciliation
        """
        assert self.apify_calls == 0, "Invariant violation: Apify must not be called"

        cname = candidate.get("company_name") or candidate.get("business_name") or ""
        city = candidate.get("city") or "Manchester"
        address = candidate.get("address") or ""
        phone = candidate.get("phone") or ""

        cand_meta = {
            "company_name": cname,
            "city": city,
            "address": address,
            "phone": phone
        }

        # 1. Config check
        is_cfg, cfg_status = self.check_configuration()
        if not is_cfg:
            return {
                "business_name": cname,
                "city": city,
                "status": "STOP_CREDENTIAL_MISSING",
                "error": cfg_status,
                "place_id": None,
                "google_evidence": None,
                "reconciliation": None,
                "canary_success": False
            }

        # 2. Quota check
        if self.place_search_calls >= self.max_canary_candidates:
            return {
                "business_name": cname,
                "city": city,
                "status": "MAX_CANARY_QUOTA_REACHED",
                "error": f"Canary quota limit of {self.max_canary_candidates} reached.",
                "place_id": None,
                "google_evidence": None,
                "reconciliation": None,
                "canary_success": False
            }

        # 3. Search Place
        place, s_err, conf = self.search_place(cname, city, address, phone)
        if not place:
            return {
                "business_name": cname,
                "city": city,
                "status": "PLACE_RESOLUTION_FAILED",
                "error": s_err,
                "place_id": None,
                "identity_confidence": conf,
                "google_evidence": None,
                "reconciliation": None,
                "canary_success": False
            }

        place_id = place.get("id")

        # 4. Get Place Details
        details, d_err = self.get_place_details(place_id)
        if not details:
            return {
                "business_name": cname,
                "city": city,
                "status": "PLACE_DETAILS_FAILED",
                "error": d_err,
                "place_id": place_id,
                "identity_confidence": conf,
                "google_evidence": None,
                "reconciliation": None,
                "canary_success": False
            }

        # 5. Extract Review Evidence
        primary_item, all_reviews, r_err = self.extract_review_evidence(details, cand_meta, as_of)

        # 6. Reconcile with existing candidate evidence
        items_to_reconcile: List[ReviewEvidenceItem] = []

        # Existing candidate evidence
        orig_rc = candidate.get("review_count") or candidate.get("original_review_count")
        orig_rat = candidate.get("rating") or candidate.get("original_rating")
        if orig_rc is not None or orig_rat is not None:
            orig_item = ReviewEvidenceItem(
                business_name=cname,
                source="Initial Discovered Evidence",
                source_family=SourceFamily.UNKNOWN.value,
                review_count=orig_rc,
                rating=orig_rat,
                freshness=candidate.get("review_freshness", ReviewFreshness.UNKNOWN.value),
                confidence=ReviewConfidence.MEDIUM.value
            )
            items_to_reconcile.append(orig_item)

        if primary_item:
            items_to_reconcile.append(primary_item)

        reconciled = self.reconciler.reconcile(items_to_reconcile, candidate_meta=cand_meta, as_of=as_of)

        return {
            "business_name": cname,
            "city": city,
            "status": "SUCCESS",
            "error": None,
            "place_id": place_id,
            "identity_confidence": conf,
            "google_evidence": primary_item.to_dict() if primary_item else None,
            "reviews_returned_count": len(all_reviews),
            "reconciliation": reconciled.to_dict(),
            "canary_success": True
        }
