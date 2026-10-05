"""
Dripp Media — Gosom Google Maps Scraper Review Freshness Fallback (Phase 7.5)
=============================================================================
Controlled, optional review-freshness fallback integration using the local
gosom/google-maps-scraper.

Target Architecture:
  FREE DISCOVERY
      ↓
  INITIAL REVIEW ENRICHMENT
      ↓
  If review freshness known -> continue
  If review_count >= 50 AND rating >= 4.0 AND review_freshness == UNKNOWN:
      ↓
  GOSOM FALLBACK
      ↓
  Google Maps review evidence
      ↓
  ReviewEvidenceReconciler
      ↓
  OperationalValidator
      ↓
  Qualification
      ↓
  Contactability

Core Invariants:
  - Safe-by-default: GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED=false.
  - Hard per-run cap: MAX_GOSOM_REVIEW_FALLBACK_CALLS = 20.
  - Zero Apify calls, zero Apify spend.
  - Zero Google Places API calls.
  - Zero CRM mutations (dry-run only).
  - Zero live outreach, zero armed campaigns.
  - Zero proxies, zero anti-bot circumvention.
  - Source provenance: source_family=GOOGLE, source_provider=GOSOM_LOCAL,
    extraction_method=GOSOM_GOOGLE_EMBEDDED_REVIEW_DATA.
  - Rule B source-family independence preserved: Google Maps review + Google place info
    remains ONE source family.
"""

import os
import re
import sys
import json
import time
import hashlib
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple, Set
from dataclasses import dataclass, field

from lib.types import DiscoveredBusiness, SourceFamily, OperationalStatus, QualificationState
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
from lib.enrichment.gosom_evaluator import GosomReviewParser, GosomPlaceEnricher
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider


PINNED_GOSOM_VERSION = "v1.18.1-0.20260920064515-549e4b5e61c7-549e4b5"
DEFAULT_CACHE_DIR = "data/cache_gosom_reviews"
DEFAULT_SCRAPER_BIN = "scratch/google_maps_scraper"
DEFAULT_MAX_CALLS_PER_RUN = 5
DEFAULT_MAX_CALLS_PER_DAY = 10
DEFAULT_MAX_CALLS = 5  # backward compatibility alias


@dataclass
class GosomFallbackConfig:
    """Configuration container for Gosom review freshness fallback."""
    enabled: bool = False
    max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN
    max_calls_per_day: int = DEFAULT_MAX_CALLS_PER_DAY
    max_calls: int = DEFAULT_MAX_CALLS_PER_RUN  # backward compatibility alias
    cache_dir: str = DEFAULT_CACHE_DIR
    scraper_bin: str = DEFAULT_SCRAPER_BIN
    scraper_version: str = PINNED_GOSOM_VERSION
    timeout_seconds: float = 60.0
    kill_switch_active: bool = False

    @classmethod
    def from_env(cls) -> "GosomFallbackConfig":
        env_enabled = os.getenv("GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED", "false").lower() in ["true", "1", "yes"]
        try:
            env_max_run = int(os.getenv("MAX_GOSOM_FALLBACK_CALLS_PER_RUN", os.getenv("MAX_GOSOM_REVIEW_FALLBACK_CALLS", str(DEFAULT_MAX_CALLS_PER_RUN))))
        except ValueError:
            env_max_run = DEFAULT_MAX_CALLS_PER_RUN
        try:
            env_max_day = int(os.getenv("MAX_GOSOM_FALLBACK_CALLS_PER_DAY", str(DEFAULT_MAX_CALLS_PER_DAY)))
        except ValueError:
            env_max_day = DEFAULT_MAX_CALLS_PER_DAY
        try:
            env_timeout = float(os.getenv("GOSOM_TIMEOUT_SECONDS", "60.0"))
        except ValueError:
            env_timeout = 60.0
        env_kill = os.getenv("GOSOM_FALLBACK_KILL_SWITCH", "false").lower() in ["true", "1", "yes"]
        env_bin = os.getenv("GOSOM_SCRAPER_BIN", DEFAULT_SCRAPER_BIN)
        env_cache = os.getenv("GOSOM_CACHE_DIR", DEFAULT_CACHE_DIR)
        return cls(
            enabled=env_enabled,
            max_calls=env_max_run,
            max_calls_per_run=env_max_run,
            max_calls_per_day=env_max_day,
            cache_dir=env_cache,
            scraper_bin=env_bin,
            timeout_seconds=env_timeout,
            kill_switch_active=env_kill
        )


from lib.enrichment.coordinate_matcher import (
    CoordinateMatchClassification,
    IdentityMatchClassification,
    CoordinateMatchResultClassification,
    CoordinateFirstMatcher,
    haversine_distance_m,
    construct_safe_coordinate_query,
)


class GosomReviewFreshnessFallback:
    """
    Controlled Review-Freshness Fallback Layer using local Gosom Google Maps Scraper.
    """

    def __init__(
        self,
        config: Optional[GosomFallbackConfig] = None,
        matcher: Optional[BusinessIdentityMatcher] = None,
        reconciler: Optional[ReviewEvidenceReconciler] = None
    ):
        self.config = config or GosomFallbackConfig.from_env()
        self.matcher = matcher or BusinessIdentityMatcher()
        self.reconciler = reconciler or ReviewEvidenceReconciler(self.matcher)
        self.place_enricher = GosomPlaceEnricher(self.matcher)
        self.coord_matcher = CoordinateFirstMatcher(self.matcher)

        # Invariant telemetries
        self.apify_calls = 0
        self.apify_spend_usd = 0.0
        self.google_places_api_calls = 0

        # Processing accounting
        self.configured_cap = self.config.max_calls_per_run
        self.calls_attempted = 0
        self.external_calls_this_run = 0
        self.calls_completed = 0
        self.calls_skipped = 0
        self.cache_hits = 0
        self.places_returned = 0
        self.reviews_returned = 0
        self.reviews_with_valid_timestamps = 0
        self.kill_switch_active = self.config.kill_switch_active
        self.audit_log: List[Dict[str, Any]] = []

        # Ensure cache directory exists
        os.makedirs(self.config.cache_dir, exist_ok=True)

    def activate_kill_switch(self) -> None:
        """Immediately blocks subsequent fallback executions and sets env kill switch."""
        self.kill_switch_active = True
        os.environ["GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED"] = "false"
        os.environ["GOSOM_FALLBACK_KILL_SWITCH"] = "true"

    def deactivate_kill_switch(self) -> None:
        """Deactivates kill switch."""
        self.kill_switch_active = False
        os.environ["GOSOM_FALLBACK_KILL_SWITCH"] = "false"

    def _get_daily_external_calls(self, as_of_date: Optional[str] = None) -> int:
        today_str = as_of_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(self.config.cache_dir, "daily_usage.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return int(data.get(today_str, 0))
            except Exception:
                return 0
        return 0

    def _increment_daily_external_calls(self, count: int = 1, as_of_date: Optional[str] = None) -> int:
        today_str = as_of_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(self.config.cache_dir, "daily_usage.json")
        data: Dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        current = int(data.get(today_str, 0)) + count
        data[today_str] = current
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        return current

    def _reset_daily_external_calls(self, as_of_date: Optional[str] = None) -> None:
        today_str = as_of_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(self.config.cache_dir, "daily_usage.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data[today_str] = 0
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass

    def _record_audit_log(
        self,
        candidate_identifier: str,
        business_name: str,
        source_coordinates: Optional[Tuple[Optional[float], Optional[float]]],
        query: str,
        cache_hit: bool,
        result_count: int,
        classification: str,
        distance: Optional[float],
        identity_score: Optional[float],
        final_evidence_decision: str,
        freshness_result: str,
        failure_reason: Optional[str] = None,
        call_type: str = "CACHE_HIT",
        elapsed_seconds: float = 0.0,
        network_outcome: Optional[str] = None,
        parser_outcome: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Structured production audit logging. Logs zero secrets and zero fabricated placeholders.
        Distinguishes REAL_EXTERNAL_CALL, CACHE_HIT, and MOCKED_CALL.
        """
        record: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "candidate_id": str(candidate_identifier or business_name),
            "candidate_identifier": str(candidate_identifier or business_name),
            "business_name": str(business_name),
            "source_coordinates": {
                "latitude": source_coordinates[0] if source_coordinates else None,
                "longitude": source_coordinates[1] if source_coordinates else None
            } if source_coordinates and (source_coordinates[0] is not None or source_coordinates[1] is not None) else None,
            "query": str(query or ""),
            "call_type": str(call_type),
            "cache_hit": bool(cache_hit),
            "cache_miss": (call_type == "REAL_EXTERNAL_CALL"),
            "external_request_initiated": (call_type == "REAL_EXTERNAL_CALL"),
            "response_received": (call_type == "REAL_EXTERNAL_CALL" and (network_outcome == "SUCCESS" or result_count > 0)),
            "elapsed_seconds": round(float(elapsed_seconds), 3),
            "network_outcome": str(network_outcome or ("SUCCESS" if not failure_reason else failure_reason)),
            "parser_outcome": str(parser_outcome or ("SUCCESS" if result_count > 0 else ("EMPTY" if failure_reason in ["EMPTY_PLACES", "NO_PLACES_RETURNED"] else "NOT_PARSED"))),
            "result_count": int(result_count),
            "matching_outcome": str(classification),
            "classification": str(classification),
            "distance": round(distance, 2) if distance is not None else None,
            "identity_score": round(identity_score, 4) if identity_score is not None else None,
            "final_evidence_decision": str(final_evidence_decision),
            "freshness_result": str(freshness_result),
            "failure_reason": str(failure_reason) if failure_reason else None
        }
        self.audit_log.append(record)
        log_file = os.path.join(self.config.cache_dir, "audit_log.jsonl")
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass
        return record

    @property
    def real_external_calls(self) -> int:
        """Returns the number of genuine external network requests made in this run."""
        return self.external_calls_this_run

    def get_observability_metrics(self) -> Dict[str, Any]:
        """
        Computes structured observability metrics across this run's audit log.
        Guarantees clear separation between:
          - Candidate attempt counters
          - Mutually exclusive match classifications (per candidate)
          - Network outcome counters
          - Evidence recovery counters
          - Quota / cap usage
        """
        total_attempts = len(self.audit_log)
        eligible_attempts = len([r for r in self.audit_log if r.get("classification") not in ["INELIGIBLE", "FLAG_DISABLED", "KILL_SWITCH_ACTIVE", "CAP_EXCEEDED", "CAP_DAILY_EXCEEDED"]])
        external_calls = self.external_calls_this_run
        cache_hits = self.cache_hits

        # Track distinct candidates evaluated through the matching layer
        # To ensure SAFE_MATCH, BRANCH_MISMATCH, IDENTITY_MISMATCH, AMBIGUOUS_MATCH, SEARCH_FAILURE
        # are strictly mutually exclusive:
        distinct_match_records: Dict[str, Dict[str, Any]] = {}
        for r in self.audit_log:
            c_id = r.get("candidate_identifier") or r.get("candidate_id") or r.get("business_name")
            if not c_id:
                continue
            cls_val = r.get("classification", "")
            # Prioritize definitive match outcome if candidate was evaluated multiple times (e.g. initial + repeat)
            if c_id not in distinct_match_records or cls_val in ["SAFE_MATCH", "BRANCH_MISMATCH", "IDENTITY_MISMATCH", "AMBIGUOUS_MATCH"]:
                distinct_match_records[c_id] = r

        safe_matches = 0
        branch_mismatches = 0
        identity_mismatches = 0
        ambiguous_matches = 0
        search_failures = 0
        scraper_failures = 0

        for r in distinct_match_records.values():
            cls_val = r.get("classification", "")
            if cls_val == "SAFE_MATCH":
                safe_matches += 1
            elif cls_val == "BRANCH_MISMATCH":
                branch_mismatches += 1
            elif cls_val == "IDENTITY_MISMATCH":
                identity_mismatches += 1
            elif cls_val == "AMBIGUOUS_MATCH":
                ambiguous_matches += 1
            elif cls_val in ["SEARCH_RECALL_FAILURE", "NO_PLACES_RETURNED", "EMPTY_PLACES"]:
                search_failures += 1
            elif cls_val == "SCRAPER_FAILURE":
                scraper_failures += 1

        matching_candidates = [
            r for r in distinct_match_records.values()
            if r.get("classification") in ["SAFE_MATCH", "BRANCH_MISMATCH", "IDENTITY_MISMATCH", "AMBIGUOUS_MATCH", "SEARCH_RECALL_FAILURE", "NO_PLACES_RETURNED", "EMPTY_PLACES"]
        ]
        recent_recovered = len([r for r in matching_candidates if r.get("freshness_result") == "RECENT" and r.get("final_evidence_decision") == "EVIDENCE_ATTACHED"])
        stale_recovered = len([r for r in matching_candidates if r.get("freshness_result") == "STALE" and r.get("final_evidence_decision") == "EVIDENCE_ATTACHED"])
        unknown_remaining = len([r for r in matching_candidates if r.get("freshness_result") == "UNKNOWN" or r.get("final_evidence_decision") != "EVIDENCE_ATTACHED"])

        latencies = [r.get("elapsed_seconds", 0.0) for r in self.audit_log if r.get("call_type") == "REAL_EXTERNAL_CALL"]
        avg_latency = round(sum(latencies) / len(latencies), 3) if latencies else 0.0
        max_latency = round(max(latencies), 3) if latencies else 0.0

        daily_usage = self._get_daily_external_calls()
        per_run_usage = self.external_calls_this_run

        return {
            "total_fallback_attempts": total_attempts,
            "eligible_attempts": eligible_attempts,
            "external_calls": external_calls,
            "cache_hits": cache_hits,
            "safe_matches": safe_matches,
            "branch_mismatches": branch_mismatches,
            "identity_mismatches": identity_mismatches,
            "ambiguous_matches": ambiguous_matches,
            "search_failures": search_failures,
            "scraper_failures": scraper_failures,
            "recent_recovered": recent_recovered,
            "stale_recovered": stale_recovered,
            "unknown_remaining": unknown_remaining,
            "average_latency_seconds": avg_latency,
            "maximum_latency_seconds": max_latency,
            "daily_usage": daily_usage,
            "per_run_usage": per_run_usage,
            "per_run_cap": self.config.max_calls_per_run,
            "daily_cap": self.config.max_calls_per_day,
            "final_match_classifications": {
                "SAFE_MATCH": safe_matches,
                "BRANCH_MISMATCH": branch_mismatches,
                "IDENTITY_MISMATCH": identity_mismatches,
                "AMBIGUOUS_MATCH": ambiguous_matches,
                "SEARCH_FAILURE": search_failures,
            }
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 1. ELIGIBILITY GATE
    # ──────────────────────────────────────────────────────────────────────────
    def is_candidate_eligible(
        self,
        candidate: Dict[str, Any],
        crm_pool: Optional[Set[str]] = None
    ) -> Tuple[bool, str]:
        """
        Determines whether a candidate is strictly eligible for Gosom review fallback.
        Must satisfy ALL 9 conditions:
          1. Candidate is fresh (not empty/corrupt)
          2. Candidate is not a CRM duplicate
          3. Candidate is a restaurant / food category
          4. review_count >= 50
          5. rating >= 4.0
          6. review_freshness == UNKNOWN
          7. No major identity conflict
          8. No closure signal
          9. Candidate is not already excluded
        """
        c_name = candidate.get("company_name") or candidate.get("business_name") or ""
        if not c_name.strip():
            return False, "INELIGIBLE_MISSING_NAME"

        # 2. CRM Duplicate check
        if crm_pool:
            c_phone = re.sub(r'[^0-9]', '', candidate.get("phone") or "")
            c_norm_name = clean_ascii_text(c_name)
            if c_phone and c_phone in crm_pool:
                return False, "INELIGIBLE_CRM_DUPLICATE_PHONE"
            if c_norm_name and c_norm_name in crm_pool:
                return False, "INELIGIBLE_CRM_DUPLICATE_NAME"

        # 3. Restaurant / food category
        category = (candidate.get("category") or candidate.get("cuisine") or "restaurant").lower()
        non_restaurant_keywords = ["hotel only", "clothing", "retail", "dentist", "car rental"]
        if any(kw in category for kw in non_restaurant_keywords):
            return False, "INELIGIBLE_NON_RESTAURANT"

        # 4. review_count >= 50
        rc = candidate.get("review_count")
        if rc is None:
            return False, "INELIGIBLE_MISSING_REVIEW_COUNT"
        try:
            rc_val = int(rc)
            if rc_val < 50:
                return False, f"INELIGIBLE_INSUFFICIENT_REVIEWS_{rc_val}_LT_50"
        except (ValueError, TypeError):
            return False, "INELIGIBLE_INVALID_REVIEW_COUNT"

        # 5. rating >= 4.0
        rat = candidate.get("rating")
        if rat is None:
            return False, "INELIGIBLE_MISSING_RATING"
        try:
            rat_val = float(rat)
            if rat_val < 4.0:
                return False, f"INELIGIBLE_LOW_RATING_{rat_val}_LT_4_0"
        except (ValueError, TypeError):
            return False, "INELIGIBLE_INVALID_RATING"

        # 6. review_freshness == UNKNOWN
        fresh = candidate.get("review_freshness") or ReviewFreshness.UNKNOWN.value
        if fresh != ReviewFreshness.UNKNOWN.value:
            return False, f"INELIGIBLE_FRESHNESS_ALREADY_KNOWN_{fresh}"

        # 7. No major identity conflict
        red_flags = candidate.get("red_flags") or []
        if any("mismatch" in str(rf).lower() or "conflict" in str(rf).lower() for rf in red_flags):
            return False, "INELIGIBLE_EXISTING_IDENTITY_CONFLICT"

        # 8. No closure signal
        for kw in OperationalValidator.CLOSURE_KEYWORDS:
            if kw in c_name.lower():
                return False, f"INELIGIBLE_CLOSURE_KEYWORD_{kw}"

        # 9. Candidate is not already excluded
        q_state = candidate.get("qualification_state") or ""
        if q_state == QualificationState.EXCLUDED.value:
            return False, "INELIGIBLE_ALREADY_EXCLUDED"

        return True, "ELIGIBLE"

    # ──────────────────────────────────────────────────────────────────────────
    # 2. DETERMINISTIC CACHING
    # ──────────────────────────────────────────────────────────────────────────
    def _compute_cache_key(self, candidate_name: str, city: str, address: Optional[str] = None) -> str:
        """Computes deterministic canonical cache key including scraper version."""
        canonical_str = f"{clean_ascii_text(candidate_name)}|{clean_ascii_text(city)}|{clean_ascii_text(address or '')}|{self.config.scraper_version}"
        sha = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()[:20]
        return f"gosom_{sha}"

    def _get_cache_path(self, cache_key: str) -> str:
        return os.path.join(self.config.cache_dir, f"{cache_key}.json")

    def get_cached_result(self, cache_key: str) -> Optional[Dict[str, Any]]:
        cpath = self._get_cache_path(cache_key)
        if os.path.exists(cpath):
            try:
                with open(cpath, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                # Only return if successful extraction status (never cache provider failures as valid empty evidence)
                if entry.get("status") == "SUCCESS" and entry.get("data") is not None:
                    self.cache_hits += 1
                    return entry.get("data")
            except Exception:
                pass
        return None

    def save_cached_result(
        self,
        cache_key: str,
        query: str,
        places: List[Dict[str, Any]],
        status: str = "SUCCESS",
        place_id: Optional[str] = None
    ) -> None:
        if status != "SUCCESS":
            # Invariant: Do not cache provider failures as valid empty evidence
            return

        cpath = self._get_cache_path(cache_key)
        res_hash = hashlib.sha256(json.dumps(places, sort_keys=True).encode("utf-8")).hexdigest()
        entry = {
            "cache_key": cache_key,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "scraper_version": self.config.scraper_version,
            "query": query,
            "result_hash": res_hash,
            "place_id": place_id or (places[0].get("place_id") if places else ""),
            "review_extraction_status": status,
            "status": status,
            "data": places
        }
        try:
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(entry, f, indent=2)
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────────
    # 3. SCRAPER EXECUTION & FAILURE HANDLING
    # ──────────────────────────────────────────────────────────────────────────
    def execute_scraper_query(
        self,
        query: str,
        cache_key: Optional[str] = None
    ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """
        Executes local gosom scraper for a single query.
        Returns: (scraped_places_list, error_code)
        """
        assert self.apify_calls == 0, "Invariant violation: Apify must not be called"
        assert self.google_places_api_calls == 0, "Invariant violation: Google Places API must not be called"

        if cache_key:
            cached = self.get_cached_result(cache_key)
            if cached is not None:
                return cached, None

        bin_path = self.config.scraper_bin
        if not os.path.exists(bin_path):
            return None, "SCRAPER_EXECUTABLE_MISSING"

        # Prepare temporary input and results file
        tmp_dir = os.path.join(self.config.cache_dir, "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        t_id = hashlib.sha256(f"{query}_{time.time()}".encode("utf-8")).hexdigest()[:12]
        query_file = os.path.join(tmp_dir, f"query_{t_id}.txt")
        result_file = os.path.join(tmp_dir, f"result_{t_id}.json")

        with open(query_file, "w", encoding="utf-8") as f:
            f.write(f"{query}\n")

        cmd = [
            bin_path,
            "-input", query_file,
            "-results", result_file,
            "-json",
            "-depth", "1",
            "-c", "1",
            "-lang", "en"
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds
            )
            if proc.returncode != 0:
                return None, f"SCRAPER_NON_ZERO_EXIT_{proc.returncode}"

            if not os.path.exists(result_file):
                return None, "SCRAPER_RESULTS_FILE_MISSING"

            places = []
            with open(result_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            places.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

            if not places:
                return None, "NO_PLACES_RETURNED"

            # Check schema integrity
            if not isinstance(places[0], dict) or "title" not in places[0]:
                return None, "SCRAPER_SCHEMA_CHANGED"

            if cache_key:
                first_pid = places[0].get("place_id") or places[0].get("data_id")
                self.save_cached_result(cache_key, query, places, status="SUCCESS", place_id=first_pid)

            return places, None

        except subprocess.TimeoutExpired:
            return None, "SCRAPER_TIMEOUT"
        except Exception as e:
            return None, f"SCRAPER_EXECUTION_ERROR: {str(e)}"
        finally:
            if os.path.exists(query_file):
                try: os.remove(query_file)
                except OSError: pass
            if os.path.exists(result_file):
                try: os.remove(result_file)
                except OSError: pass

    # ──────────────────────────────────────────────────────────────────────────
    # 4. CANDIDATE ENRICHMENT & RECONCILIATION
    # ──────────────────────────────────────────────────────────────────────────
    def enrich_candidate(
        self,
        candidate: Dict[str, Any],
        existing_reviews: Optional[List[ReviewEvidenceItem]] = None,
        crm_pool: Optional[Set[str]] = None,
        preloaded_places: Optional[List[Dict[str, Any]]] = None,
        as_of: datetime = REFERENCE_DATE,
        shadow_mode: bool = False
    ) -> Tuple[Optional[ReconciledReviewEvidence], Dict[str, Any]]:
        """
        Coordinates the review freshness fallback for an eligible candidate:
          1. Check feature flag & runtime kill switch (immediate dynamic evaluation)
          2. Check eligibility (9 strict conditions)
          3. Determine PATH A (Complete Address) vs PATH B (Coordinate-First Fallback)
          4. Check deterministic cache (cache hit does NOT consume external call cap)
          5. Check hard per-run (5) and daily (10) external call caps
          6. Execute or retrieve scraper results
          7. Match place with strict identity & branch isolation (Path A place enricher or Path B frozen matcher)
          8. Extract review items under SourceFamily.GOOGLE and GOSOM_LOCAL
          9. Reconcile all evidence through ReviewEvidenceReconciler
          10. Structured audit logging (zero secrets, zero fabricated placeholders)
        Returns: (reconciled_evidence, telemetry_dict)
        """
        c_name = candidate.get("company_name") or candidate.get("business_name") or ""
        c_lat = candidate.get("latitude")
        c_lon = candidate.get("longitude")
        c_id = candidate.get("source_id") or candidate.get("place_id") or c_name
        src_coords = (c_lat, c_lon) if (c_lat is not None or c_lon is not None) else None

        telemetry: Dict[str, Any] = {
            "candidate_name": c_name,
            "status": "INIT",
            "reason": None,
            "cache_hit": False,
            "place_matched": None,
            "reviews_extracted": 0,
            "reconciliation_type": None,
            "path": None,
            "query": None,
            "match_classification": None,
            "distance_meters": None,
            "shadow_mode": shadow_mode
        }

        # 1. Feature Flag & Runtime Kill Switch Check
        kill_switch_active = (
            self.kill_switch_active
            or self.config.kill_switch_active
            or os.getenv("GOSOM_FALLBACK_KILL_SWITCH", "false").lower() in ["true", "1", "yes"]
        )

        if kill_switch_active:
            self.calls_skipped += 1
            telemetry["status"] = "KILL_SWITCH_ACTIVE"
            telemetry["reason"] = "Gosom fallback kill switch activated"
            self._record_audit_log(
                candidate_identifier=c_id,
                business_name=c_name,
                source_coordinates=src_coords,
                query="",
                cache_hit=False,
                result_count=0,
                classification="KILL_SWITCH_ACTIVE",
                distance=None,
                identity_score=None,
                final_evidence_decision="NO_EVIDENCE_ATTACHED",
                freshness_result="UNKNOWN",
                failure_reason="Gosom fallback kill switch activated"
            )
            return None, telemetry

        env_flag_val = os.getenv("GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED")
        if env_flag_val is not None:
            env_val_clean = env_flag_val.strip().lower()
            if env_val_clean in ["false", "0", "no", "off"]:
                is_enabled = False
            elif env_val_clean in ["true", "1", "yes", "on"]:
                is_enabled = True
            else:
                is_enabled = self.config.enabled
        else:
            is_enabled = self.config.enabled

        # If explicitly disabled in config, respect it
        if not self.config.enabled:
            is_enabled = False

        if not is_enabled and not shadow_mode:
            self.calls_skipped += 1
            telemetry["status"] = "FLAG_DISABLED"
            telemetry["reason"] = "GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED is false"
            self._record_audit_log(
                candidate_identifier=c_id,
                business_name=c_name,
                source_coordinates=src_coords,
                query="",
                cache_hit=False,
                result_count=0,
                classification="FLAG_DISABLED",
                distance=None,
                identity_score=None,
                final_evidence_decision="NO_EVIDENCE_ATTACHED",
                freshness_result="UNKNOWN",
                failure_reason="GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED is false"
            )
            return None, telemetry

        # 2. Eligibility Gate Check
        is_eligible, elig_reason = self.is_candidate_eligible(candidate, crm_pool)
        if not is_eligible:
            self.calls_skipped += 1
            telemetry["status"] = "SKIPPED_INELIGIBLE"
            telemetry["reason"] = elig_reason
            self._record_audit_log(
                candidate_identifier=c_id,
                business_name=c_name,
                source_coordinates=src_coords,
                query="",
                cache_hit=False,
                result_count=0,
                classification="INELIGIBLE",
                distance=None,
                identity_score=None,
                final_evidence_decision="NO_EVIDENCE_ATTACHED",
                freshness_result="UNKNOWN",
                failure_reason=elig_reason
            )
            return None, telemetry

        # 3. Address Routing (PATH A vs PATH B)
        c_city = candidate.get("city", "Manchester")
        c_addr = candidate.get("address", "")
        c_street = (candidate.get("street") or "").strip()
        c_postcode = (candidate.get("postcode") or "").strip()

        has_coords = (c_lat is not None and c_lon is not None)
        has_complete_address = bool(c_street and c_postcode) or (
            bool(c_addr) and len(c_addr) > 8 and any(char.isdigit() for char in c_addr) and (
                bool(c_postcode) or bool(re.search(r'[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}', c_addr, re.I))
            )
        )

        if has_complete_address or not has_coords:
            path_type = "PATH_A"
            if c_street and c_postcode:
                query = f'"{c_name}" "{c_street}" "{c_postcode}" "{c_city}"'
            elif c_addr and len(c_addr) > 5:
                query = f"{c_name} {c_addr}"
            elif c_street:
                query = f"{c_name} {c_street} {c_city}"
            else:
                query = f"{c_name} {c_city}"
        else:
            path_type = "PATH_B"
            # For PARTIAL candidates, strictly: "<exact business name>" "Manchester"
            # No inferred street, no inferred postcode, no house number, no reverse-geocoded address
            query = f'"{c_name}" "{c_city}"'

        telemetry["path"] = path_type
        telemetry["query"] = query
        # 4. Deterministic Caching & Hard Call Caps
        cache_key = self._compute_cache_key(c_name, c_city, query)
        scraped_places = preloaded_places
        is_cache_hit = False
        call_type = "MOCKED_CALL" if preloaded_places is not None else "REAL_EXTERNAL_CALL"
        elapsed_seconds = 0.0
        network_outcome = "MOCK_PROVIDED" if preloaded_places is not None else None
        parser_outcome = f"PARSED_{len(preloaded_places)}_PLACES" if preloaded_places is not None else None

        if scraped_places is None:
            cached = self.get_cached_result(cache_key)
            if cached is not None:
                scraped_places = cached
                is_cache_hit = True
                call_type = "CACHE_HIT"
                network_outcome = "CACHE_READ_SUCCESS"
                parser_outcome = f"PARSED_{len(scraped_places)}_PLACES_FROM_CACHE"
                telemetry["cache_hit"] = True

        if is_cache_hit:
            # Cache hit: does NOT count against external scraper call caps
            pass
        else:
            # Not cached: check per-run cap
            effective_cap = min(self.config.max_calls, self.config.max_calls_per_run)
            if self.calls_attempted >= effective_cap or self.external_calls_this_run >= self.config.max_calls_per_run:
                telemetry["status"] = "CAP_EXCEEDED"
                telemetry["reason"] = f"Configured cap of {effective_cap} reached"
                telemetry["call_type"] = "NO_CALL"
                telemetry["network_outcome"] = "CAP_EXCEEDED"
                self._record_audit_log(
                    candidate_identifier=c_id,
                    business_name=c_name,
                    source_coordinates=src_coords,
                    query=query,
                    cache_hit=False,
                    result_count=0,
                    classification="CAP_EXCEEDED",
                    distance=None,
                    identity_score=None,
                    final_evidence_decision="NO_EVIDENCE_ATTACHED",
                    freshness_result="UNKNOWN",
                    failure_reason=telemetry["reason"],
                    call_type="NO_CALL",
                    elapsed_seconds=0.0,
                    network_outcome="CAP_EXCEEDED",
                    parser_outcome="NOT_REACHED"
                )
                return None, telemetry

            # Check daily cap
            today_external = self._get_daily_external_calls()
            if today_external >= self.config.max_calls_per_day:
                telemetry["status"] = "CAP_DAILY_EXCEEDED"
                telemetry["reason"] = f"Daily cap of {self.config.max_calls_per_day} reached ({today_external} calls today)"
                telemetry["call_type"] = "NO_CALL"
                telemetry["network_outcome"] = "CAP_DAILY_EXCEEDED"
                self._record_audit_log(
                    candidate_identifier=c_id,
                    business_name=c_name,
                    source_coordinates=src_coords,
                    query=query,
                    cache_hit=False,
                    result_count=0,
                    classification="CAP_DAILY_EXCEEDED",
                    distance=None,
                    identity_score=None,
                    final_evidence_decision="NO_EVIDENCE_ATTACHED",
                    freshness_result="UNKNOWN",
                    failure_reason=telemetry["reason"],
                    call_type="NO_CALL",
                    elapsed_seconds=0.0,
                    network_outcome="CAP_DAILY_EXCEEDED",
                    parser_outcome="NOT_REACHED"
                )
                return None, telemetry

            self.calls_attempted += 1

            # If not preloaded, execute external scraper
            if scraped_places is None:
                call_type = "REAL_EXTERNAL_CALL"
                t0 = time.time()
                scraped_places, err = self.execute_scraper_query(query, cache_key=cache_key)
                elapsed_seconds = time.time() - t0
                self.external_calls_this_run += 1
                self._increment_daily_external_calls(1)

                if err:
                    telemetry["status"] = "SCRAPER_FAILURE"
                    telemetry["reason"] = err
                    telemetry["call_type"] = call_type
                    telemetry["elapsed_seconds"] = round(elapsed_seconds, 3)
                    telemetry["network_outcome"] = err
                    telemetry["parser_outcome"] = "NOT_REACHED"
                    self._record_audit_log(
                        candidate_identifier=c_id,
                        business_name=c_name,
                        source_coordinates=src_coords,
                        query=query,
                        cache_hit=False,
                        result_count=0,
                        classification="SCRAPER_FAILURE",
                        distance=None,
                        identity_score=None,
                        final_evidence_decision="NO_EVIDENCE_ATTACHED",
                        freshness_result="UNKNOWN",
                        failure_reason=err,
                        call_type=call_type,
                        elapsed_seconds=elapsed_seconds,
                        network_outcome=err,
                        parser_outcome="NOT_REACHED"
                    )
                    return None, telemetry
                elif not scraped_places:
                    telemetry["status"] = "SEARCH_RECALL_FAILURE"
                    telemetry["reason"] = "EMPTY_PLACES"
                    telemetry["call_type"] = call_type
                    telemetry["elapsed_seconds"] = round(elapsed_seconds, 3)
                    telemetry["network_outcome"] = "SUCCESS_EMPTY"
                    telemetry["parser_outcome"] = "EMPTY"
                    self._record_audit_log(
                        candidate_identifier=c_id,
                        business_name=c_name,
                        source_coordinates=src_coords,
                        query=query,
                        cache_hit=False,
                        result_count=0,
                        classification="SEARCH_RECALL_FAILURE",
                        distance=None,
                        identity_score=None,
                        final_evidence_decision="NO_EVIDENCE_ATTACHED",
                        freshness_result="UNKNOWN",
                        failure_reason="EMPTY_PLACES",
                        call_type=call_type,
                        elapsed_seconds=elapsed_seconds,
                        network_outcome="SUCCESS_EMPTY",
                        parser_outcome="EMPTY"
                    )
                    return None, telemetry
                else:
                    network_outcome = "SUCCESS"
                    parser_outcome = f"PARSED_{len(scraped_places)}_PLACES"

        if not scraped_places:
            telemetry["status"] = "SEARCH_RECALL_FAILURE"
            telemetry["reason"] = "NO_PLACES_RETURNED"
            telemetry["call_type"] = call_type
            telemetry["elapsed_seconds"] = round(elapsed_seconds, 3)
            telemetry["network_outcome"] = network_outcome or "SUCCESS_EMPTY"
            telemetry["parser_outcome"] = parser_outcome or "EMPTY"
            self._record_audit_log(
                candidate_identifier=c_id,
                business_name=c_name,
                source_coordinates=src_coords,
                query=query,
                cache_hit=is_cache_hit,
                result_count=0,
                classification="SEARCH_RECALL_FAILURE",
                distance=None,
                identity_score=None,
                final_evidence_decision="NO_EVIDENCE_ATTACHED",
                freshness_result="UNKNOWN",
                failure_reason="NO_PLACES_RETURNED",
                call_type=call_type,
                elapsed_seconds=elapsed_seconds,
                network_outcome=network_outcome or "SUCCESS_EMPTY",
                parser_outcome=parser_outcome or "EMPTY"
            )
            return None, telemetry

        telemetry["call_type"] = call_type
        telemetry["elapsed_seconds"] = round(elapsed_seconds, 3)
        telemetry["network_outcome"] = network_outcome
        telemetry["parser_outcome"] = parser_outcome

        self.calls_completed += 1
        self.places_returned += len(scraped_places)

        # 5. Place Matching & Identity Resolution
        matched_place = None
        dist_m = None
        ident_score = None
        match_classification_str = ""

        if path_type == "PATH_A":
            matched_place, match_err, conf = self.place_enricher.match_place_to_candidate(candidate, scraped_places)
            if not matched_place or match_err:
                telemetry["status"] = "MATCH_FAILED"
                telemetry["reason"] = match_err or "LOW_CONFIDENCE"
                telemetry["match_classification"] = match_err or "MATCH_FAILED"
                self._record_audit_log(
                    candidate_identifier=c_id,
                    business_name=c_name,
                    source_coordinates=src_coords,
                    query=query,
                    cache_hit=is_cache_hit,
                    result_count=len(scraped_places),
                    classification=match_err or "MATCH_FAILED",
                    distance=None,
                    identity_score=None,
                    final_evidence_decision="NO_EVIDENCE_ATTACHED",
                    freshness_result="UNKNOWN",
                    failure_reason=match_err or "LOW_CONFIDENCE",
                    call_type=call_type,
                    elapsed_seconds=elapsed_seconds,
                    network_outcome=network_outcome,
                    parser_outcome=parser_outcome
                )
                return None, telemetry
            match_classification_str = "SAFE_MATCH"
            telemetry["match_classification"] = "SAFE_MATCH"
        else:
            matched_place, match_class, conf, diag = self.coord_matcher.classify_and_match(candidate, scraped_places)
            match_classification_str = match_class.value
            telemetry["match_classification"] = match_class.value
            telemetry["coordinate_diagnostics"] = diag
            dist_m = diag.get("matched_distance_meters")
            ident_score = diag.get("matched_identity_score")
            telemetry["distance_meters"] = dist_m

            if match_class != CoordinateMatchResultClassification.SAFE_MATCH:
                telemetry["status"] = "BLOCKED"
                telemetry["reason"] = match_class.value
                self._record_audit_log(
                    candidate_identifier=c_id,
                    business_name=c_name,
                    source_coordinates=src_coords,
                    query=query,
                    cache_hit=is_cache_hit,
                    result_count=len(scraped_places),
                    classification=match_class.value,
                    distance=dist_m,
                    identity_score=ident_score,
                    final_evidence_decision="NO_EVIDENCE_ATTACHED",
                    freshness_result="UNKNOWN",
                    failure_reason=match_class.value,
                    call_type=call_type,
                    elapsed_seconds=elapsed_seconds,
                    network_outcome=network_outcome,
                    parser_outcome=parser_outcome
                )
                return None, telemetry

        telemetry["place_matched"] = matched_place.get("title")

        # 6. Review Evidence Extraction
        primary_item, extracted_items, ext_err = self.place_enricher.extract_place_review_evidence(
            matched_place, candidate, as_of
        )
        if ext_err or not primary_item or not primary_item.evidence_date:
            telemetry["status"] = "EXTRACTION_FAILED"
            telemetry["reason"] = ext_err or "MISSING_REVIEW_TIMESTAMPS"
            self._record_audit_log(
                candidate_identifier=c_id,
                business_name=c_name,
                source_coordinates=src_coords,
                query=query,
                cache_hit=is_cache_hit,
                result_count=len(scraped_places),
                classification=match_classification_str,
                distance=dist_m,
                identity_score=ident_score,
                final_evidence_decision="NO_EVIDENCE_ATTACHED",
                freshness_result="UNKNOWN",
                failure_reason=ext_err or "MISSING_REVIEW_TIMESTAMPS",
                call_type=call_type,
                elapsed_seconds=elapsed_seconds,
                network_outcome=network_outcome,
                parser_outcome="EXTRACTION_FAILED"
            )
            return None, telemetry

        # Enforce SourceFamily.GOOGLE & GOSOM_LOCAL provenance invariants
        primary_item.source_family = SourceFamily.GOOGLE.value
        primary_item.source_provider = "GOSOM_LOCAL"
        primary_item.extraction_method = "GOSOM_GOOGLE_EMBEDDED_REVIEW_DATA"
        for item in extracted_items:
            item.source_family = SourceFamily.GOOGLE.value
            item.source_provider = "GOSOM_LOCAL"
            item.extraction_method = "GOSOM_GOOGLE_EMBEDDED_REVIEW_DATA"

        self.reviews_returned += len(extracted_items)
        if primary_item.evidence_date:
            self.reviews_with_valid_timestamps += len([i for i in extracted_items if i.evidence_date])
        telemetry["reviews_extracted"] = len(extracted_items)

        # 7. Multi-Source Reconciliation
        all_review_items: List[ReviewEvidenceItem] = list(existing_reviews or [])
        all_review_items.append(primary_item)

        candidate_meta = {
            "company_name": c_name,
            "address": c_addr,
            "city": c_city,
            "postcode": c_postcode,
            "latitude": c_lat,
            "longitude": c_lon,
        }
        reconciled = self.reconciler.reconcile(
            items=all_review_items,
            candidate_meta=candidate_meta,
            as_of=as_of
        )

        telemetry["status"] = "SUCCESS"
        telemetry["reconciliation_type"] = reconciled.conflict_type
        telemetry["is_material_conflict"] = reconciled.is_material_conflict
        telemetry["is_branch_difference"] = reconciled.is_branch_difference

        self._record_audit_log(
            candidate_identifier=c_id,
            business_name=c_name,
            source_coordinates=src_coords,
            query=query,
            cache_hit=is_cache_hit,
            result_count=len(scraped_places),
            classification=match_classification_str,
            distance=dist_m,
            identity_score=ident_score,
            final_evidence_decision="EVIDENCE_ATTACHED",
            freshness_result=reconciled.reconciled_freshness,
            failure_reason=None,
            call_type=call_type,
            elapsed_seconds=elapsed_seconds,
            network_outcome=network_outcome,
            parser_outcome=f"EXTRACTED_{len(extracted_items)}_REVIEWS"
        )

        return reconciled, telemetry


class LimitedProductionGosomSafetyWrapper:
    """
    Production Safety Wrapper strictly bounding Gosom Review Freshness Fallback.
    Guarantees:
      1. Hard per-run cap (max_calls_per_run <= 5)
      2. Hard daily call cap (max_calls_per_day <= 10)
      3. Cohort size upper bound (cohort_max <= 5)
      4. Deterministic idempotency and persistent caching
      5. Immediate runtime kill switch
      6. Complete structured audit logging
      7. No outreach coupling (qualification-only)
      8. Qualification-state safeguards (preserves Rule B)
    """

    def __init__(
        self,
        fallback: Optional[GosomReviewFreshnessFallback] = None,
        max_cohort_size: int = 5,
        max_calls_per_run: int = 5,
        max_calls_per_day: int = 10
    ):
        cfg = fallback.config if fallback else GosomFallbackConfig.from_env()
        # Enforce strict maximum caps
        cfg.max_calls_per_run = min(cfg.max_calls_per_run, max_calls_per_run)
        cfg.max_calls_per_day = min(cfg.max_calls_per_day, max_calls_per_day)
        self.fallback = fallback or GosomReviewFreshnessFallback(config=cfg)
        self.max_cohort_size = max_cohort_size
        self.cohort_candidates_processed = 0
        self.processed_candidate_identifiers: Set[str] = set()

    @property
    def config(self) -> GosomFallbackConfig:
        return self.fallback.config

    @property
    def kill_switch_active(self) -> bool:
        return self.fallback.kill_switch_active

    @property
    def audit_log(self) -> List[Dict[str, Any]]:
        return self.fallback.audit_log

    @property
    def external_calls_this_run(self) -> int:
        return self.fallback.external_calls_this_run

    @property
    def real_external_calls(self) -> int:
        return self.fallback.real_external_calls

    @property
    def cache_hits(self) -> int:
        return self.fallback.cache_hits

    def activate_kill_switch(self) -> None:
        self.fallback.activate_kill_switch()

    def deactivate_kill_switch(self) -> None:
        self.fallback.deactivate_kill_switch()

    def is_candidate_eligible(self, candidate: Dict[str, Any], crm_pool: Optional[Set[str]] = None) -> Tuple[bool, str]:
        return self.fallback.is_candidate_eligible(candidate, crm_pool)

    def get_observability_metrics(self) -> Dict[str, Any]:
        """Exposes structured observability metrics from the underlying fallback layer."""
        return self.fallback.get_observability_metrics()

    def enrich_candidate(
        self,
        candidate: Dict[str, Any],
        existing_reviews: Optional[List[ReviewEvidenceItem]] = None,
        crm_pool: Optional[Set[str]] = None,
        preloaded_places: Optional[List[Dict[str, Any]]] = None,
        as_of: datetime = REFERENCE_DATE,
        shadow_mode: bool = False
    ) -> Tuple[Optional[ReconciledReviewEvidence], Dict[str, Any]]:
        # Kill switch takes absolute precedence over all processing gates
        kill_switch_active = (
            self.kill_switch_active
            or self.config.kill_switch_active
            or os.getenv("GOSOM_FALLBACK_KILL_SWITCH", "false").lower() in ["true", "1", "yes"]
        )
        if kill_switch_active:
            return self.fallback.enrich_candidate(
                candidate=candidate,
                existing_reviews=existing_reviews,
                crm_pool=crm_pool,
                preloaded_places=preloaded_places,
                as_of=as_of,
                shadow_mode=shadow_mode
            )

        cand_id = str(candidate.get("source_id") or candidate.get("place_id") or candidate.get("company_name") or candidate.get("business_name") or "")
        if cand_id not in self.processed_candidate_identifiers:
            if len(self.processed_candidate_identifiers) >= self.max_cohort_size:
                c_name = candidate.get("company_name") or candidate.get("business_name") or ""
                telemetry = {
                    "candidate_name": c_name,
                    "status": "COHORT_LIMIT_EXCEEDED",
                    "reason": f"Maximum production cohort size of {self.max_cohort_size} reached",
                    "cache_hit": False,
                    "place_matched": None,
                    "reviews_extracted": 0,
                    "reconciliation_type": None,
                    "path": None,
                    "query": None,
                    "match_classification": "COHORT_LIMIT_EXCEEDED",
                    "distance_meters": None,
                    "shadow_mode": shadow_mode
                }
                return None, telemetry
            self.processed_candidate_identifiers.add(cand_id)
            self.cohort_candidates_processed = len(self.processed_candidate_identifiers)

        return self.fallback.enrich_candidate(
            candidate=candidate,
            existing_reviews=existing_reviews,
            crm_pool=crm_pool,
            preloaded_places=preloaded_places,
            as_of=as_of,
            shadow_mode=shadow_mode
        )

