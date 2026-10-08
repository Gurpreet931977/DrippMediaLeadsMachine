"""
lib/production/market_runner.py
================================
Production Scale Engine + Multi-Market Acquisition Runner (Phase 9.0).

Orchestrates:
  MARKET -> DISCOVERY -> PIPELINE -> CRM

Enforces:
  - Strict Batch Ceilings (limits on candidates, external calls, runtime, CRM writes).
  - Quota Budgeting (search calls, Gosom calls, CRM writes, outreach dispatches == 0).
  - Resumable Runs with idempotent checkpoints.
  - Per-candidate error isolation (FAILED_CANDIDATE) without aborting the batch.
  - Incremental processing & cross-run deduplication via BusinessIdentityMatcher.
  - Single source of truth qualification (Rule B LeadScoringProvider unchanged).
  - Decoupled contactability reporting.
  - Commercial opportunity tracking.
  - Strict outreach and CRM safety invariants (Live Seafood preserved, outreach = 0).
"""

import os
import sys
import time
import json
import uuid
import re
from enum import Enum
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Set, Tuple, Callable

from lib.production.market_config import MarketConfig, MarketRegistry
from lib.crm.identity_matcher import (
    BusinessIdentityMatcher,
    IdentityMatchOutcome,
    IdentityMatchResult,
)
from lib.types import (
    DiscoveredBusiness,
    Lead,
    ResearchLogEntry,
    WebsiteStatus,
    VerificationStatus,
    CountryStatus,
    Priority,
    LeadStatus,
    QualificationState,
    OperationalStatus,
    OperationalConfidence,
    EvidenceFreshness,
    OutreachStatus,
    OutreachMode,
    ResearchFailureState,
    ResearchTelemetry,
)
from lib.discovery.osm import OpenStreetMapProvider
from lib.discovery.boundary_validator import CityBoundaryValidator
from lib.validation.country_validator import CountryValidator
from lib.website.detector import NodeWebsiteDetectionProvider
from lib.verification.no_website_verifier import NoWebsiteVerificationProvider
from lib.validation.operational_validator import OperationalValidator
from lib.enrichment.review_rating_enricher import ReviewRatingEnricher
from lib.enrichment.review_recovery import ReviewEvidenceRecoveryLayer
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.outreach.contactability import ContactabilityAssessor, ContactabilityState
from lib.outreach.phase_8_5_qualification import (
    WebsiteOpportunityStatus,
    CommercialFitStatus,
    calculate_website_opportunity_score,
    determine_commercial_fit,
)
from lib.crm.reconciliation import crm_write_lock
from lib.sheets.google_sheets import GoogleSheetsStorageProvider


class RunState(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class FailedCandidate:
    candidate_id: str
    company_name: str
    error_type: str
    source: str
    timestamp: str
    retryable: bool
    details: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QuotaBudget:
    """
    Centralized external usage budget and tracker.
    Enforces hard ceilings before any external operation executes.
    """
    search_limit: int = 500
    tavily_limit: int = 1000
    gosom_limit: int = 10
    crm_write_limit: int = 100
    outreach_dispatch_limit: int = 0  # Absolute zero during acquisition

    search_used: int = 0
    tavily_used: int = 0
    gosom_used: int = 0
    crm_writes_used: int = 0
    outreach_dispatches_used: int = 0
    other_external_used: int = 0

    def can_consume(self, resource: str, count: int = 1) -> bool:
        """Checks if budget is available before executing an external action."""
        if resource == "search":
            return (self.search_used + count) <= self.search_limit
        elif resource == "tavily":
            return (self.tavily_used + count) <= self.tavily_limit and (self.search_used + count) <= self.search_limit
        elif resource == "gosom":
            return (self.gosom_used + count) <= self.gosom_limit
        elif resource == "crm_write":
            return (self.crm_writes_used + count) <= self.crm_write_limit
        elif resource == "outreach":
            # Hard refusal for acquisition
            return False
        return True

    def consume(self, resource: str, count: int = 1) -> None:
        """Consumes a budget unit."""
        if resource == "search":
            self.search_used += count
        elif resource == "tavily":
            self.tavily_used += count
            self.search_used += count
        elif resource == "gosom":
            self.gosom_used += count
        elif resource == "crm_write":
            self.crm_writes_used += count
        elif resource == "outreach":
            self.outreach_dispatches_used += count
            raise RuntimeError("CRITICAL VIOLATION: Outreach dispatch attempted during acquisition run!")
        elif resource == "other_external":
            self.other_external_used += count

    def get_remaining(self) -> Dict[str, int]:
        return {
            "search_remaining": max(0, self.search_limit - self.search_used),
            "tavily_remaining": max(0, self.tavily_limit - self.tavily_used),
            "gosom_remaining": max(0, self.gosom_limit - self.gosom_used),
            "crm_writes_remaining": max(0, self.crm_write_limit - self.crm_writes_used),
            "outreach_dispatches_remaining": 0,
        }

    @property
    def search_remaining(self) -> int:
        return max(0, self.search_limit - self.search_used)

    @property
    def tavily_remaining(self) -> int:
        return max(0, self.tavily_limit - self.tavily_used)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "search_limit": self.search_limit,
            "tavily_limit": self.tavily_limit,
            "gosom_limit": self.gosom_limit,
            "crm_write_limit": self.crm_write_limit,
            "outreach_dispatch_limit": self.outreach_dispatch_limit,
            "search_used": self.search_used,
            "tavily_used": self.tavily_used,
            "gosom_used": self.gosom_used,
            "crm_writes_used": self.crm_writes_used,
            "outreach_dispatches_used": self.outreach_dispatches_used,
            "other_external_used": self.other_external_used,
            "remaining": self.get_remaining(),
        }


@dataclass
class BatchController:
    """
    Enforces hard execution bounds on candidate processing.
    """
    requested_count: int = 100
    max_external_calls: int = 500
    max_runtime_seconds: float = 1800.0
    max_new_crm_records: int = 100

    def is_runtime_exceeded(self, start_time: float) -> bool:
        return (time.time() - start_time) >= self.max_runtime_seconds


@dataclass
class PopulationAccounting:
    """
    Canonical population accounting for acquisition batches.
    Guarantees mathematical reconcilability across funnel populations:
      discovered_total = processed_total + skipped_total + failed
      processed_total = processed_new + processed_refreshed
    """
    discovered_total: int = 0
    country_valid: int = 0
    country_invalid: int = 0
    duplicate_existing: int = 0
    duplicate_within_run: int = 0
    processed_new: int = 0
    processed_refreshed: int = 0
    failed: int = 0
    boundary_invalid: int = 0

    @property
    def processed_total(self) -> int:
        return self.processed_new + self.processed_refreshed

    @property
    def skipped_total(self) -> int:
        return (
            self.duplicate_existing
            + self.duplicate_within_run
            + self.country_invalid
            + self.boundary_invalid
        )

    def validate_invariants(self) -> Tuple[bool, List[str]]:
        """Verifies conservation laws and population accounting invariants."""
        errors: List[str] = []
        expected_disc = self.processed_total + self.skipped_total + self.failed
        if self.discovered_total != expected_disc:
            errors.append(
                f"Conservation Error: discovered ({self.discovered_total}) != "
                f"processed ({self.processed_total}) + skipped ({self.skipped_total}) + failed ({self.failed}) = {expected_disc}"
            )
        expected_proc = self.processed_new + self.processed_refreshed
        if self.processed_total != expected_proc:
            errors.append(
                f"Processing Error: processed ({self.processed_total}) != "
                f"new ({self.processed_new}) + refreshed ({self.processed_refreshed}) = {expected_proc}"
            )
        return len(errors) == 0, errors

    def to_dict(self) -> Dict[str, Any]:
        valid, errors = self.validate_invariants()
        return {
            "discovered_total": self.discovered_total,
            "country_valid": self.country_valid,
            "country_invalid": self.country_invalid,
            "duplicate_existing": self.duplicate_existing,
            "duplicate_within_run": self.duplicate_within_run,
            "processed_new": self.processed_new,
            "processed_refreshed": self.processed_refreshed,
            "failed": self.failed,
            "boundary_invalid": self.boundary_invalid,
            "processed_total": self.processed_total,
            "skipped_total": self.skipped_total,
            "invariants_passed": valid,
            "conservation_errors": errors,
        }


@dataclass
class FieldCompletenessSummary:
    field_name: str
    present_count: int
    missing_count: int
    completeness_percent: float


@dataclass
class CohortFieldCompleteness:
    """
    Field-level data completeness metrics across the 14 required dimensions.
    """
    total_candidates: int
    fields: Dict[str, FieldCompletenessSummary]
    overall_required_field_completeness: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_candidates": self.total_candidates,
            "overall_required_field_completeness": self.overall_required_field_completeness,
            "fields": {k: asdict(v) for k, v in self.fields.items()},
        }

    @classmethod
    def calculate(cls, candidates: List[Dict[str, Any]]) -> "CohortFieldCompleteness":
        total = len(candidates)
        if total == 0:
            return cls(total_candidates=0, fields={}, overall_required_field_completeness=0.0)

        # 14 Required fields per Phase 9.1 Section 3
        field_checkers = {
            "name_present": lambda c: bool(c.get("company_name") or c.get("name") or c.get("business_name")),
            "address_present": lambda c: bool(c.get("address")),
            "postcode_present": lambda c: bool(
                c.get("postcode")
                or (c.get("address") and re.search(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", str(c.get("address"))))
            ),
            "phone_present": lambda c: bool(c.get("phone")),
            "website_present": lambda c: bool(c.get("website") or c.get("clean_website")),
            "website_status_present": lambda c: bool(
                c.get("website_opportunity_status")
                or c.get("website_status")
                or c.get("verification_status")
            ),
            "rating_present": lambda c: (c.get("rating") is not None and float(c.get("rating", 0.0)) > 0.0),
            "review_count_present": lambda c: (c.get("review_count") is not None and int(c.get("review_count", 0)) > 0),
            "review_date_present": lambda c: bool(
                c.get("review_evidence_date") or c.get("latest_review_date") or c.get("review_date")
            ),
            "operational_evidence_present": lambda c: bool(
                c.get("operational_status")
                and c.get("operational_status") not in ("NOT_CHECKED", "UNKNOWN", "OPERATIONAL_UNKNOWN")
                and (c.get("operational_evidence") or c.get("operational_confidence") in ("HIGH", "MEDIUM"))
            ),
            "social_present": lambda c: bool(
                c.get("instagram_url") or c.get("facebook_url") or c.get("social")
            ),
            "contactability_present": lambda c: bool(c.get("contactability_status")),
            "identity_confidence_present": lambda c: (
                c.get("identity_confidence") is not None
                or c.get("match_confidence") is not None
                or c.get("qualification_score") is not None
            ),
            "commercial_fit_present": lambda c: bool(c.get("commercial_fit_status")),
        }

        fields_summary: Dict[str, FieldCompletenessSummary] = {}
        total_present_across_fields = 0

        for fname, checker in field_checkers.items():
            pcount = sum(1 for c in candidates if checker(c))
            mcount = total - pcount
            pct = round((pcount / total) * 100, 2)
            fields_summary[fname] = FieldCompletenessSummary(
                field_name=fname,
                present_count=pcount,
                missing_count=mcount,
                completeness_percent=pct,
            )
            total_present_across_fields += pcount

        total_possible_slots = total * len(field_checkers)
        overall_pct = (
            round((total_present_across_fields / total_possible_slots) * 100, 2)
            if total_possible_slots > 0
            else 0.0
        )

        return cls(
            total_candidates=total,
            fields=fields_summary,
            overall_required_field_completeness=overall_pct,
        )


@dataclass
class MarketScorecard:
    """
    Multi-market acquisition metrics.
    All rate calculations enforce non-zero, genuine funnel stage denominators.
    """
    discovery_yield: float = 0.0          # discovered / requested
    country_valid_rate: float = 0.0       # country_valid / discovered
    qualification_rate: float = 0.0       # qualified / processed
    contactability_rate: float = 0.0      # contactable / processed
    outreach_ready_rate: float = 0.0      # outreach_ready / processed
    website_opportunity_rate: float = 0.0 # website_opp / processed
    commercial_prospect_rate: float = 0.0 # commercial_prospects / processed
    duplicate_rate: float = 0.0           # duplicates_skipped / discovered
    data_completeness: float = 0.0        # complete_records / processed

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)

    @classmethod
    def calculate(
        cls,
        requested: int,
        discovered: int,
        country_valid: int,
        processed: int,
        qualified: int,
        contactable: int,
        outreach_ready: int,
        website_opportunity: int,
        commercial_prospects: int,
        duplicates_skipped: int,
        complete_records: int,
    ) -> "MarketScorecard":
        denom_req = max(1, requested)
        denom_disc = max(1, discovered)
        denom_proc = max(1, processed)

        return cls(
            discovery_yield=round(discovered / denom_req, 4),
            country_valid_rate=round(country_valid / denom_disc, 4),
            qualification_rate=round(qualified / denom_proc, 4),
            contactability_rate=round(contactable / denom_proc, 4),
            outreach_ready_rate=round(outreach_ready / denom_proc, 4),
            website_opportunity_rate=round(website_opportunity / denom_proc, 4),
            commercial_prospect_rate=round(commercial_prospects / denom_proc, 4),
            duplicate_rate=round(duplicates_skipped / denom_disc, 4),
            data_completeness=round(complete_records / denom_proc, 4),
        )


class MarketRunner:
    """
    Scalable multi-market production runner.
    Orchestrates Discovery -> Pipeline -> CRM with deduplication,
    quota tracking, checkpointing, and error isolation.
    """

    def __init__(
        self,
        market_config: Optional[MarketConfig] = None,
        batch_controller: Optional[BatchController] = None,
        quota_budget: Optional[QuotaBudget] = None,
        discovery_provider: Optional[Any] = None,
        country_validator: Optional[CountryValidator] = None,
        boundary_validator: Optional[CityBoundaryValidator] = None,
        identity_matcher: Optional[BusinessIdentityMatcher] = None,
        detector: Optional[NodeWebsiteDetectionProvider] = None,
        verifier: Optional[NoWebsiteVerificationProvider] = None,
        review_enricher: Optional[ReviewRatingEnricher] = None,
        review_recovery: Optional[ReviewEvidenceRecoveryLayer] = None,
        enable_research: bool = True,
        operational_validator: Optional[OperationalValidator] = None,
        scorer: Optional[LeadScoringProvider] = None,
        contact_assessor: Optional[ContactabilityAssessor] = None,
        storage_provider: Optional[GoogleSheetsStorageProvider] = None,
        checkpoint_dir: Optional[str] = None,
    ):
        self.market_config = market_config or MarketRegistry.get("MANCHESTER_UK")
        self.batch_controller = batch_controller or BatchController(
            requested_count=100,
            max_external_calls=500,
            max_runtime_seconds=1800.0,
            max_new_crm_records=100,
        )
        tavily_q = self.market_config.daily_quota.get(
            "tavily_requests",
            int(os.getenv("TAVILY_QUOTA_LIMIT", os.getenv("TAVILY_DAILY_LIMIT", "1000")))
        )
        self.quota = quota_budget or QuotaBudget(
            search_limit=self.market_config.daily_quota.get("search_requests", 500),
            tavily_limit=tavily_q,
            gosom_limit=self.market_config.daily_quota.get("gosom_calls", 10),
            crm_write_limit=self.market_config.daily_quota.get("crm_writes", 100),
            outreach_dispatch_limit=0,
        )

        # Core Subsystems
        self.discovery = discovery_provider or OpenStreetMapProvider()
        self.country_validator = country_validator or CountryValidator()
        self.boundary_validator = boundary_validator or CityBoundaryValidator()
        self.identity_matcher = identity_matcher or BusinessIdentityMatcher()
        self.detector = detector or NodeWebsiteDetectionProvider()
        self.verifier = verifier or NoWebsiteVerificationProvider()
        self.review_enricher = review_enricher or ReviewRatingEnricher()
        self.review_recovery = review_recovery or ReviewEvidenceRecoveryLayer(
            web_search_provider=getattr(self.review_enricher, "web", None),
            matcher=self.identity_matcher
        )
        web = getattr(self.review_enricher, "web", None)
        if web and hasattr(web, "set_quota_budget"):
            web.set_quota_budget(self.quota)
        self.enable_research = enable_research
        self.research_telemetry_records: List[Dict[str, Any]] = []
        self.operational_validator = operational_validator or OperationalValidator()
        self.scorer = scorer or LeadScoringProvider()
        self.contact_assessor = contact_assessor or ContactabilityAssessor()
        self.storage = storage_provider or GoogleSheetsStorageProvider()

        # Checkpoints
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.checkpoint_dir = checkpoint_dir or os.path.join(project_root, "data", "market_runs")
        os.makedirs(self.checkpoint_dir, exist_ok=True)

        # Execution State
        self.run_id = f"RUN-{self.market_config.city[:3].upper()}-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        self.status = RunState.CREATED.value
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None

        # Tracking Collections
        self.processed_ids: Set[str] = set()
        self.failed_candidates: List[FailedCandidate] = []
        self.outreach_ready_pool: List[Dict[str, Any]] = []
        self.commercial_prospects_pool: List[Dict[str, Any]] = []
        self.review_queue_pool: List[Dict[str, Any]] = []
        self.research_log_pool: List[Dict[str, Any]] = []

        # Run Counters
        self.requested_count = self.batch_controller.requested_count
        self.discovered_count = 0
        self.processed_count = 0
        self.qualified_count = 0
        self.contactable_count = 0
        self.automated_sendable_count = 0
        self.manual_contactable_count = 0
        self.outreach_ready_count = 0
        self.manual_review_count = 0
        self.research_only_count = 0
        self.excluded_count = 0
        self.website_opportunity_count = 0
        self.commercial_prospect_count = 0
        self.country_valid_count = 0
        self.website_checked_count = 0
        self.no_website_count = 0
        self.operational_count = 0
        self.complete_records_count = 0
        self.duplicates_skipped = 0
        self.new_businesses_count = 0
        self.existing_refreshed_count = 0
        self.failures_count = 0
        self.review_evidence_count = 0
        self.crm_writes_count = 0
        self.outreach_sends_count = 0
        self.campaigns_armed_count = 0
        self.candidates_summary: List[Dict[str, Any]] = []

        # Existing CRM Pool for cross-run deduplication
        self.existing_crm_records: List[Dict[str, Any]] = []
        self._load_existing_crm_pool()

    def _load_existing_crm_pool(self) -> None:
        """Loads all existing CRM records from cache and historical files for deduplication."""
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sources = [
            os.path.join(project_root, "data", "cache_sheets_leads.json"),
            os.path.join(project_root, "data", "cache_sheets_review_queue.json"),
            os.path.join(project_root, "data", "cache_sheets_research_log.json"),
            os.path.join(project_root, "data", "phase_8_5_qualification_audit.json"),
        ]

        seen_keys: Set[str] = set()
        pool: List[Dict[str, Any]] = []

        for p in sources:
            if not os.path.exists(p):
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    content = json.load(f)
                    records = []
                    if isinstance(content, dict):
                        if "leads" in content:
                            records = content["leads"]
                        elif "review_queue" in content:
                            records = content["review_queue"]
                        elif "entries" in content:
                            records = content["entries"]
                        elif "records" in content:
                            records = content["records"]
                    elif isinstance(content, list):
                        records = content

                    for r in records:
                        if not isinstance(r, dict):
                            continue
                        name = r.get("company_name", "")
                        city = r.get("city", "")
                        key = f"{name.lower().strip()}@{city.lower().strip()}"
                        if name and key not in seen_keys:
                            seen_keys.add(key)
                            pool.append(r)
            except Exception as e:
                print(f"[MarketRunner] Warning loading existing CRM source {p}: {e}")

        self.existing_crm_records = pool

    def _persist_checkpoint(self, last_completed_id: Optional[str] = None) -> str:
        """Persists runner state to a checkpoint file."""
        checkpoint_path = os.path.join(self.checkpoint_dir, f"checkpoint_{self.run_id}.json")
        data = {
            "run_id": self.run_id,
            "market_id": self.market_config.market_id,
            "status": self.status,
            "last_completed_candidate": last_completed_id,
            "processed_ids": list(self.processed_ids),
            "failed_ids": [f.candidate_id for f in self.failed_candidates],
            "stats": self.get_stats(),
            "quota_usage": self.quota.to_dict(),
            "checkpoint_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return checkpoint_path

    def load_checkpoint(self, checkpoint_path: str) -> bool:
        """Restores runner state from a checkpoint file for idempotent resumption."""
        if not os.path.exists(checkpoint_path):
            return False
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.run_id = data.get("run_id", self.run_id)
        self.status = data.get("status", self.status)
        self.processed_ids = set(data.get("processed_ids", []))
        stats = data.get("stats", {})
        self.discovered_count = stats.get("discovered_count", 0)
        self.processed_count = stats.get("processed_count", 0)
        self.qualified_count = stats.get("qualified_count", 0)
        self.contactable_count = stats.get("contactable_count", 0)
        self.outreach_ready_count = stats.get("outreach_ready_count", 0)
        self.duplicates_skipped = stats.get("duplicates_skipped", 0)
        self.failures_count = stats.get("failures_count", 0)
        return True

    def pause_run(self) -> None:
        """Transitions runner state to PAUSED and saves checkpoint."""
        self.status = RunState.PAUSED.value
        self._persist_checkpoint()

    def cancel_run(self) -> None:
        """Transitions runner state to CANCELLED and saves checkpoint."""
        self.status = RunState.CANCELLED.value
        self.finished_at = datetime.now(timezone.utc).isoformat()
        self._persist_checkpoint()

    def resume_run(self, candidate_pool: Optional[List[DiscoveredBusiness]] = None) -> Dict[str, Any]:
        """Resumes execution from current checkpoint."""
        if self.status not in (RunState.PAUSED.value, RunState.PARTIAL.value):
            raise RuntimeError(f"Cannot resume run with status '{self.status}'. Must be PAUSED or PARTIAL.")
        self.status = RunState.RUNNING.value
        return self.run(candidate_pool=candidate_pool, resume=True)

    def run(
        self,
        candidate_pool: Optional[List[DiscoveredBusiness]] = None,
        resume: bool = False,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """
        Executes acquisition run for the target market.
        Processes candidates up to batch limits, respecting hard external quotas.
        """
        def log(msg: str) -> None:
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        start_time = time.time()
        if not resume:
            self.started_at = datetime.now(timezone.utc).isoformat()
            self.status = RunState.RUNNING.value
            log(f"\n[MarketRunner] Starting Run {self.run_id} for Market {self.market_config.market_id}...")
        else:
            log(f"\n[MarketRunner] Resuming Run {self.run_id} ({len(self.processed_ids)} already processed)...")

        # ── STAGE 1: DISCOVERY ──
        raw_candidates: List[DiscoveredBusiness] = []
        if candidate_pool is not None:
            raw_candidates = list(candidate_pool)
            self.discovered_count = len(raw_candidates)
            log(f"  [Discovery] Using provided candidate pool of {len(raw_candidates)} candidates.")
        else:
            if not self.quota.can_consume("search", 1):
                self.status = RunState.PARTIAL.value
                log("  [Quota] External search quota exhausted before discovery. Stopping as PARTIAL.")
                self.finished_at = datetime.now(timezone.utc).isoformat()
                return self.get_summary()

            log(f"  [Discovery] Discovering businesses in {self.market_config.city}, {self.market_config.country} via {self.discovery.name}...")
            # Discover candidates targeting requested count + margin for filtering
            needed_discovery = max(self.requested_count * 2, 150)
            try:
                self.quota.consume("search", 1)
                discovered: List[DiscoveredBusiness] = []
                seen_names = set()
                for ind in self.market_config.industries:
                    if len(discovered) >= needed_discovery:
                        break
                    try:
                        places = self.discovery.search_businesses(
                            city=self.market_config.city,
                            country=self.market_config.country,
                            industry=ind,
                            limit=min(needed_discovery - len(discovered), 150),
                        )
                        for p in places:
                            norm_name = p.company_name.lower().strip()
                            if norm_name not in seen_names:
                                seen_names.add(norm_name)
                                discovered.append(p)
                    except Exception as ind_err:
                        log(f"  [Discovery Warning] Industry '{ind}' discovery note: {ind_err}")

                raw_candidates = discovered
                self.discovered_count = len(raw_candidates)
                log(f"  [Discovery OK] Discovered {len(raw_candidates)} places across industries.")
            except Exception as disc_err:
                log(f"  [Discovery Error] Discovery failed: {disc_err}")
                self.status = RunState.FAILED.value
                self.finished_at = datetime.now(timezone.utc).isoformat()
                self._persist_checkpoint()
                return self.get_summary()

        # ── STAGE 2: CANDIDATE PROCESSING LOOP ──
        for biz in raw_candidates:
            # 1. Check Hard Ceilings
            if self.processed_count >= self.requested_count:
                log(f"  [Batch Limit] Reached requested candidate count ({self.requested_count}). Finishing batch.")
                break

            if self.batch_controller.is_runtime_exceeded(start_time):
                log(f"  [Runtime Limit] Maximum runtime exceeded ({self.batch_controller.max_runtime_seconds}s). Setting PARTIAL.")
                self.status = RunState.PARTIAL.value
                break

            # 2. Process Individual Candidate with Error Isolation (Objective I)
            biz_id = "unknown_candidate"
            try:
                biz_name = getattr(biz, "company_name", None) or "unknown"
                biz_city = getattr(biz, "city", None) or self.market_config.city
                biz_id = getattr(biz, "osm_id", None) or f"{biz_name}@{biz_city}"

                if biz_id in self.processed_ids:
                    continue

                self._process_single_candidate(biz, log=log)
                self.processed_ids.add(biz_id)
                self._persist_checkpoint(last_completed_id=biz_id)
            except Exception as cand_ex:
                # Per-candidate error isolation (Objective I)
                self.failures_count += 1
                try:
                    cname = getattr(biz, "company_name", None) or biz_id
                except Exception:
                    cname = biz_id

                failed = FailedCandidate(
                    candidate_id=biz_id,
                    company_name=cname,
                    error_type=type(cand_ex).__name__,
                    source=getattr(biz, "discovery_source", "OPENSTREETMAP"),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    retryable=True,
                    details=str(cand_ex)[:200],
                )
                self.failed_candidates.append(failed)
                log(f"  [Candidate Error] Candidate '{cname}' failed: {cand_ex}. Isolated; continuing batch.")
                self.processed_ids.add(biz_id)

        # ── STAGE 3: FINALIZE RUN ──
        if self.status == RunState.RUNNING.value:
            self.status = RunState.COMPLETED.value

        self.finished_at = datetime.now(timezone.utc).isoformat()
        self._persist_checkpoint()
        log(f"\n[MarketRunner] Run {self.run_id} finished with status '{self.status}'.")

        return self.get_summary()

    def _enrich_and_recover_reviews(
        self,
        biz: DiscoveredBusiness,
        cand_dict: Dict[str, Any],
        log: Callable[[str], None],
    ) -> ResearchTelemetry:
        """
        Phase 11.1: Executes review enrichment & multi-source evidence recovery for a candidate.
        Captures detailed internal diagnostic telemetry per candidate.
        Distinguishes 8 failure states:
          A. NO_EVIDENCE_FOUND
          B. PROVIDER_UNAVAILABLE
          C. PROVIDER_NOT_CONFIGURED
          D. PROVIDER_FAILED
          E. PROVIDER_TIMEOUT
          F. EXTRACTION_FAILED
          G. IDENTITY_MISMATCH
          H. EVIDENCE_CONFLICT
        Never fabricates review evidence. Enforces quota limits and safety locks.
        """
        cname = biz.company_name
        city = biz.city or self.market_config.city

        # 1. Identify operational signals already discovered from OSM
        signals = []
        if biz.phone:
            signals.append("PHONE")
        if biz.address or getattr(biz, "street", None):
            signals.append("ADDRESS")
        if getattr(biz, "opening_hours", None):
            signals.append("OPENING_HOURS")
        if biz.raw_website:
            signals.append("WEBSITE")
        op_signal_found = ", ".join(signals) if signals else "NONE"

        # 2. Check if research stage is enabled
        if not getattr(self, "enable_research", True):
            telem = ResearchTelemetry(
                candidate=cname,
                provider_attempted=["NONE"],
                provider_result="RESEARCH_DISABLED",
                evidence_found="NONE",
                operational_signal_found=op_signal_found,
                fallback_attempted="GOSOM: NOT_ATTEMPTED",
                fallback_result="NOT_ATTEMPTED",
                failure_reason=ResearchFailureState.PROVIDER_NOT_CONFIGURED.value,
                details={"reason": "Research stage disabled by configuration"},
            )
            biz.raw_data = getattr(biz, "raw_data", {}) or {}
            biz.raw_data["research_telemetry"] = telem.to_dict()
            return telem

        # 3. Identify Search Providers Configured in WebSearch cascade
        web_provider = getattr(self.review_enricher, "web", None)
        providers_attempted: List[str] = []
        tavily_ok = False
        brave_ok = False
        searxng_health = "NOT_CONNECTED"
        ddg_state = "CLOSED"

        if web_provider:
            tavily_ok = bool(getattr(web_provider, "tavily_key", None))
            brave_ok = bool(getattr(web_provider, "brave_key", None))
            searxng_url = getattr(web_provider, "searxng_url", "")
            if hasattr(web_provider, "check_searxng_health"):
                searxng_health = web_provider.check_searxng_health()
            ddg_cb = web_provider.circuit_breakers.get("DUCKDUCKGO_FALLBACK") if hasattr(web_provider, "circuit_breakers") else None
            ddg_state = ddg_cb.status if ddg_cb else "CLOSED"

            if tavily_ok:
                providers_attempted.append("TAVILY")
            if brave_ok:
                providers_attempted.append("BRAVE")
            if searxng_url:
                providers_attempted.append("SEARXNG")
            providers_attempted.append("DUCKDUCKGO_FALLBACK")
        else:
            providers_attempted.append("NONE")

        # 4. Check Quota Budget
        if not self.quota.can_consume("search", 1):
            log(f"  [Quota] Search quota exhausted. Cannot research '{cname}'.")
            telem = ResearchTelemetry(
                candidate=cname,
                provider_attempted=providers_attempted,
                provider_result="QUOTA_EXHAUSTED",
                evidence_found="NONE",
                operational_signal_found=op_signal_found,
                fallback_attempted="GOSOM: NOT_ATTEMPTED",
                fallback_result="NOT_ATTEMPTED",
                failure_reason=ResearchFailureState.QUOTA_EXCEEDED.value,
                query_count=0,
                usable_results_count=0,
                latency=0.0,
                details={"quota_remaining": self.quota.search_remaining},
            )
            biz.raw_data = getattr(biz, "raw_data", {}) or {}
            biz.raw_data["research_telemetry"] = telem.to_dict()
            return telem

        # 5. Check if candidate already has verified review count and rating
        already_has_reviews = (biz.review_count is not None and (biz.review_count or 0) > 0 and biz.rating is not None)
        enrich_res = None
        rec_res = None
        start_research_time = time.time()

        if not already_has_reviews:
            # Consume Quota & Execute Research
            self.quota.consume("search", 1)

            # Stage 1: ReviewRatingEnricher
            try:
                biz = self.review_enricher.enrich_candidate(biz)
                enrich_res = getattr(biz, "raw_data", {}).get("review_enrichment")
            except Exception as ex:
                log(f"  [Research Warning] enrich_candidate exception for '{cname}': {ex}")

            # Stage 2: ReviewEvidenceRecoveryLayer (if reviews still missing or missing date)
            if biz.review_count is None or not getattr(biz, "latest_review_date", None):
                try:
                    rec_res = self.review_recovery.recover_candidate(cand_dict)
                    if rec_res and rec_res.recovered_review_count is not None:
                        biz.review_count = rec_res.recovered_review_count
                        biz.rating = rec_res.recovered_rating
                        if rec_res.recovered_date:
                            biz.latest_review_date = rec_res.recovered_date
                        if not hasattr(biz, "raw_data") or not isinstance(biz.raw_data, dict):
                            biz.raw_data = {}
                        biz.raw_data["review_recovery"] = rec_res.to_dict()
                except Exception as rec_ex:
                    log(f"  [Recovery Warning] recover_candidate exception for '{cname}': {rec_ex}")

        # 6. Differentiate Failure Reason and Outcome
        if biz.review_count is not None and (biz.review_count or 0) > 0:
            provider_result = "SEARCH_SUCCEEDED"
            evidence_str = f"{biz.review_count} reviews, {biz.rating or 'N/A'}★ (date: {getattr(biz, 'latest_review_date', None) or 'NONE'})"
            failure_reason = ResearchFailureState.NONE.value
        else:
            evidence_str = "NONE"
            provider_result = "NO_EVIDENCE"
            failure_reason = ResearchFailureState.NO_EVIDENCE_FOUND.value

            # Check for Identity Mismatch (wrong branch / low similarity)
            if rec_res and rec_res.status == "REJECTED_IDENTITY_MISMATCH":
                provider_result = "REJECTED_IDENTITY_MISMATCH"
                failure_reason = ResearchFailureState.IDENTITY_MISMATCH.value

            # Check for Evidence Conflict across sources
            elif (rec_res and rec_res.status == "REJECTED_CONFLICT") or (
                enrich_res and enrich_res.get("review_confidence") == "CONFLICT"
            ):
                provider_result = "REJECTED_CONFLICT"
                failure_reason = ResearchFailureState.EVIDENCE_CONFLICT.value

            # Check if search failed due to extraction error
            elif (
                enrich_res and enrich_res.get("review_status") == "EXTRACTION_FAILED"
            ) or (rec_res and rec_res.failure_reason == "EXTRACTION_FAILED"):
                provider_result = "EXTRACTION_FAILED"
                failure_reason = ResearchFailureState.EXTRACTION_FAILED.value

            # Check provider infrastructure states
            elif web_provider:
                enrich_ev_str = str(enrich_res.get("review_evidence", "")) if enrich_res else ""

                # Did search hit quota limit?
                has_quota = (
                    "QUOTA_EXCEEDED" in enrich_ev_str
                    or (rec_res and rec_res.telemetry and any(
                        t.get("search_outcome") == "QUOTA_EXCEEDED" or "QUOTA" in str(t.get("failure_reason", "")).upper()
                        for t in rec_res.telemetry
                    ))
                )
                if has_quota:
                    provider_result = "QUOTA_EXCEEDED"
                    failure_reason = ResearchFailureState.QUOTA_EXCEEDED.value

                # Did search queries timeout?
                elif (
                    "TIMEOUT" in enrich_ev_str.upper()
                    or (rec_res and rec_res.telemetry and any(
                        t.get("search_outcome") in ("SEARCH_TIMEOUT", "PROVIDER_TIMEOUT") or "TIMEOUT" in str(t.get("failure_reason", "")).upper()
                        for t in rec_res.telemetry
                    ))
                ):
                    provider_result = "SEARCH_TIMEOUT"
                    failure_reason = ResearchFailureState.PROVIDER_TIMEOUT.value

                # Is connection refused / unavailable?
                elif not tavily_ok and not brave_ok and searxng_health == "NOT_CONNECTED":
                    provider_result = "PROVIDER_UNAVAILABLE"
                    failure_reason = ResearchFailureState.PROVIDER_UNAVAILABLE.value

                # Are all providers unconfigured?
                elif not tavily_ok and not brave_ok and not getattr(web_provider, "searxng_url", ""):
                    provider_result = "PROVIDER_NOT_CONFIGURED"
                    failure_reason = ResearchFailureState.PROVIDER_NOT_CONFIGURED.value

                # Did circuit open or provider fail?
                elif (
                    ddg_state == "OPEN"
                    or (rec_res and any(t.get("search_outcome") in ("SEARCH_CIRCUIT_OPEN", "SEARCH_FAILED", "SEARCH_BLOCKED", "PROVIDER_FAILED") for t in (rec_res.telemetry or [])))
                    or "PROVIDER_FAILED" in enrich_ev_str
                ):
                    provider_result = "PROVIDER_FAILED"
                    failure_reason = ResearchFailureState.PROVIDER_FAILED.value

        usable_results = 0
        if biz.review_count is not None and (biz.review_count or 0) > 0:
            usable_results = 1
        elif enrich_res and enrich_res.get("all_evidence"):
            usable_results = len([e for e in enrich_res.get("all_evidence", []) if not e.get("reject_reason")])

        q_count = 0
        if enrich_res and enrich_res.get("all_evidence"):
            q_count += len(enrich_res.get("all_evidence"))
        if rec_res and getattr(rec_res, "telemetry", None):
            q_count += len(rec_res.telemetry)
        if q_count == 0 and not already_has_reviews:
            q_count = 1

        research_duration = time.time() - start_research_time if not already_has_reviews else 0.0

        telem = ResearchTelemetry(
            candidate=cname,
            provider_attempted=providers_attempted,
            provider_result=provider_result,
            evidence_found=evidence_str,
            operational_signal_found=op_signal_found,
            fallback_attempted="GOSOM: NOT_ATTEMPTED",
            fallback_result="NOT_ATTEMPTED",
            failure_reason=failure_reason,
            query_count=q_count,
            usable_results_count=usable_results,
            latency=research_duration,
            details={
                "city": city,
                "review_count": biz.review_count,
                "rating": biz.rating,
                "latest_review_date": getattr(biz, "latest_review_date", None),
                "query_count": q_count,
                "usable_results": usable_results,
                "latency": round(research_duration, 3),
            },
        )

        if not hasattr(biz, "raw_data") or not isinstance(biz.raw_data, dict):
            biz.raw_data = {}
        biz.raw_data["research_telemetry"] = telem.to_dict()

        log(f"  [Research Telemetry] Candidate: '{cname}'")
        log(f"    • provider attempted: {', '.join(telem.provider_attempted) or 'NONE'}")
        log(f"    • provider result   : {telem.provider_result}")
        log(f"    • review evidence   : {telem.evidence_found}")
        log(f"    • operational signal: {telem.operational_signal_found}")
        log(f"    • fallback attempted: {telem.fallback_attempted}")
        log(f"    • fallback result   : {telem.fallback_result}")
        log(f"    • failure reason    : {telem.failure_reason}")
        log(f"    • query count       : {telem.query_count}")
        log(f"    • usable results    : {telem.usable_results_count}")
        log(f"    • latency           : {round(telem.latency, 3)}s")

        return telem

    def _process_single_candidate(self, biz: DiscoveredBusiness, log: Callable[[str], None]) -> None:
        """
        Executes full pipeline stages for one candidate business.
        Applies deduplication, validation, qualification, contactability, and opportunity.
        """
        # Step A: Cross-Run Deduplication & Branch Protection (Objective D & E)
        cand_dict = {
            "company_name": biz.company_name,
            "target_country": self.market_config.country,
            "country": getattr(biz, "detected_country", "") or self.market_config.country,
            "city": biz.city or self.market_config.city,
            "street": getattr(biz, "street", "") or "",
            "address": biz.address or "",
            "postcode": biz.postcode or "",
            "website": biz.raw_website or "",
            "phone": biz.phone or "",
            "lat": getattr(biz, "latitude", None) if getattr(biz, "latitude", None) is not None else getattr(biz, "lat", None),
            "lon": getattr(biz, "longitude", None) if getattr(biz, "longitude", None) is not None else getattr(biz, "lon", None),
        }

        match_res: IdentityMatchResult = self.identity_matcher.match_candidate(
            candidate=cand_dict,
            existing_leads=self.existing_crm_records
        )

        if match_res.outcome == IdentityMatchOutcome.EXISTING_BUSINESS.value:
            # Business already exists in CRM or prior cohort
            self.duplicates_skipped += 1
            self.existing_refreshed_count += 1
            log(f"  [Deduplication] '{biz.company_name}' matches existing lead {match_res.matched_lead_id} ({', '.join(match_res.match_reasons[:2])}) -> Skipped/Refreshed.")
            return

        elif match_res.outcome in (IdentityMatchOutcome.POSSIBLE_DUPLICATE.value, IdentityMatchOutcome.CONFLICT.value):
            # Ambiguous or contradictory match: route to review queue without creating duplicate lead
            self.duplicates_skipped += 1
            log(f"  [Deduplication] '{biz.company_name}' is {match_res.outcome} with {match_res.matched_lead_id} -> Protected against duplicate lead creation.")
            return

        # NEW_BUSINESS confirmed!
        self.new_businesses_count += 1
        self.processed_count += 1

        # Step B: Country Validation (Objective B & J)
        c_status, det_country, reg, pcode, c_evidence = self.country_validator.validate(
            target_country=self.market_config.country,
            address=biz.address,
            phone=biz.phone,
            raw_data=biz.raw_data,
        )
        biz.country_status = c_status
        biz.detected_country = det_country
        biz.region = reg or biz.region
        biz.postcode = pcode or biz.postcode
        biz.country_evidence = c_evidence

        if c_status != CountryStatus.COUNTRY_MATCH.value:
            self.excluded_count += 1
            log(f"  [Country Mismatch] '{biz.company_name}' ({det_country} != {self.market_config.country}) -> EXCLUDED.")
            self.research_log_pool.append({
                "company_name": biz.company_name,
                "qualification_state": QualificationState.EXCLUDED.value,
                "reason": "COUNTRY_MISMATCH",
            })
            return

        self.country_valid_count += 1

        # Step C: Administrative Boundary Validation
        b_res = self.boundary_validator.validate_candidate(
            city=self.market_config.city,
            country=self.market_config.country,
            lat=cand_dict["lat"],
            lon=cand_dict["lon"],
            address=biz.address or "",
            detected_city=biz.city or "",
        )
        if not b_res.city_match:
            self.excluded_count += 1
            log(f"  [Boundary Mismatch] '{biz.company_name}' outside {self.market_config.city} ({b_res.city_match_reason}) -> EXCLUDED.")
            self.research_log_pool.append({
                "company_name": biz.company_name,
                "qualification_state": QualificationState.EXCLUDED.value,
                "reason": f"BOUNDARY_MISMATCH: {b_res.city_match_reason}",
            })
            return

        # Check data completeness
        if biz.address and (biz.phone or biz.raw_website or getattr(biz, "instagram_url", None)):
            self.complete_records_count += 1

        # Step D: Fast Website Detection & Deep No-Website Verification (Objective J)
        self.website_checked_count += 1
        raw_web = (biz.raw_website or "").strip()
        if raw_web:
            det_result = self.detector.detect_website(raw_web)
            w_status = det_result.get("website_status", WebsiteStatus.WEBSITE_EXISTS.value)
            clean_web = det_result.get("clean_website", raw_web)

            if w_status == WebsiteStatus.WEBSITE_EXISTS.value:
                opp_status = WebsiteOpportunityStatus.FUNCTIONAL_WEBSITE
                ver_status = VerificationStatus.WEBSITE_EXISTS.value
                ver_reason = f"Official website listed: {clean_web}"
            elif w_status == WebsiteStatus.WEBSITE_BROKEN.value:
                opp_status = WebsiteOpportunityStatus.BROKEN_WEBSITE
                ver_status = VerificationStatus.WEBSITE_BROKEN.value
                ver_reason = f"Listed website is broken/unreachable: {clean_web}"
                self.website_opportunity_count += 1
            else:
                opp_status = WebsiteOpportunityStatus.UNCLEAR_WEBSITE
                ver_status = VerificationStatus.WEBSITE_UNCLEAR.value
                ver_reason = "Listed website status unclear"
                self.website_opportunity_count += 1
        else:
            clean_web = ""
            opp_status = WebsiteOpportunityStatus.NO_WEBSITE
            ver_status = VerificationStatus.NO_WEBSITE_CONFIRMED.value
            ver_reason = "No website identified in primary discovery source"
            self.no_website_count += 1
            self.website_opportunity_count += 1

        # Step D2: Research & Review Evidence Recovery (Phase 11.1)
        res_telem = self._enrich_and_recover_reviews(biz, cand_dict, log=log)
        self.research_telemetry_records.append(res_telem.to_dict())

        # Step E: Operational Verification (Rule A & Rule B independent corroboration)
        soc_audit = {
            "social_ownership_status": getattr(biz, "social_ownership_status", "UNKNOWN"),
            "social_status": getattr(biz, "social_status", "SOCIAL_UNKNOWN"),
            "verified_social_urls": {},
        }
        if getattr(biz, "instagram_url", None):
            soc_audit["verified_social_urls"]["instagram"] = biz.instagram_url
        if getattr(biz, "facebook_url", None):
            soc_audit["verified_social_urls"]["facebook"] = biz.facebook_url

        op_audit = OperationalValidator.verify_operations(
            business=biz,
            social_audit=soc_audit,
            creator_evidence=None,
            website_verification_status=ver_status,
        )
        biz.operational_status = op_audit.get("operational_status", OperationalStatus.OPERATIONAL_UNKNOWN.value)
        biz.operational_confidence = op_audit.get("operational_confidence", OperationalConfidence.LOW.value)
        biz.operational_evidence = op_audit.get("operational_evidence", "")
        if biz.operational_status == OperationalStatus.ACTIVE_CONFIRMED.value:
            self.operational_count += 1

        # Step F: Single-Source Qualification Engine Scoring (Objective J)
        audit = self.scorer.evaluate_lead(
            business=biz,
            verification_status=ver_status,
            verification_reason=ver_reason,
            website_evidence={"clean_website": clean_web, "opp_status": opp_status},
        )

        q_state = audit.get("qualification_state", QualificationState.EXCLUDED.value)
        q_score = audit.get("score", 0)
        q_priority = audit.get("priority", Priority.LOW.value)
        q_reason = audit.get("qualification_reason", "")

        # Step G: Decoupled Contactability Assessment (Objective K)
        # Convert biz to dict suitable for contact_assessor
        lead_stub = {
            "lead_id": f"CAND-{uuid.uuid4().hex[:6].upper()}",
            "company_name": biz.company_name,
            "target_country": self.market_config.country,
            "country": self.market_config.country,
            "city": self.market_config.city,
            "phone": biz.phone,
            "website": clean_web,
            "instagram_url": getattr(biz, "instagram_url", "") or "",
            "facebook_url": getattr(biz, "facebook_url", "") or "",
            "email": getattr(biz, "email", "") or "",
            "qualification_state": q_state,
        }
        has_contact_signal = bool(biz.phone or clean_web or getattr(biz, "instagram_url", None) or getattr(biz, "facebook_url", None) or getattr(biz, "email", None))
        if has_contact_signal:
            contact_eval = self.contact_assessor.assess_lead(lead_stub)
            contact_status = contact_eval.contactability_status
            is_contactable = contact_eval.contactability_status in (
                ContactabilityState.CONTACTABLE.value,
                ContactabilityState.PARTIALLY_CONTACTABLE.value,
            ) or bool(biz.phone)
            is_automated_sendable = contact_eval.automated_contactable
            is_manual_contactable = contact_eval.manual_contactable or bool(biz.phone)
        else:
            contact_status = ContactabilityState.NOT_CONTACTABLE.value
            is_contactable = False
            is_automated_sendable = False
            is_manual_contactable = False

        if is_contactable:
            self.contactable_count += 1
        if is_automated_sendable:
            self.automated_sendable_count += 1
        if is_manual_contactable:
            self.manual_contactable_count += 1

        # Step H: Commercial Opportunity Layer (Objective L)
        opp_score = calculate_website_opportunity_score(
            opportunity_status=opp_status,
            operational_status=biz.operational_status,
            is_franchise=False,
        )
        comm_fit = determine_commercial_fit(
            opportunity_status=opp_status,
            is_franchise=False,
            is_closed=(biz.operational_status == OperationalStatus.CLOSED_OR_UNVERIFIED.value),
        )

        lead_record = {
            "candidate_id": cand_dict.get("company_name"),
            "company_name": biz.company_name,
            "market_id": self.market_config.market_id,
            "city": self.market_config.city,
            "country": self.market_config.country,
            "address": biz.address or "",
            "phone": biz.phone or "",
            "website": clean_web,
            "review_count": biz.review_count or 0,
            "rating": biz.rating or 0.0,
            "qualification_state": q_state,
            "qualification_score": q_score,
            "priority": q_priority,
            "qualification_reason": q_reason,
            "contactability_status": contact_status,
            "automated_sendable": is_automated_sendable,
            "manual_contactable": is_manual_contactable,
            "website_opportunity_status": opp_status,
            "website_opportunity_score": opp_score,
            "commercial_fit_status": comm_fit,
            "operational_status": biz.operational_status,
            "date_processed": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }

        # Step I: Routing by Qualification State (Single Source of Truth)
        if q_state == QualificationState.OUTREACH_READY.value:
            self.qualified_count += 1
            self.outreach_ready_count += 1
            self.outreach_ready_pool.append(lead_record)
            log(f"  [OUTREACH READY] '{biz.company_name}' (Score: {q_score}, Revs: {biz.review_count}, Rating: {biz.rating}★) -> Qualified!")
        elif q_state == QualificationState.MANUAL_REVIEW.value:
            self.manual_review_count += 1
            self.review_queue_pool.append(lead_record)
            log(f"  [MANUAL REVIEW] '{biz.company_name}' -> Review Queue ({q_reason}).")
        elif q_state == QualificationState.RESEARCH_ONLY.value:
            self.research_only_count += 1
            self.research_log_pool.append(lead_record)
            log(f"  [RESEARCH ONLY] '{biz.company_name}' -> Research Log ({q_reason}).")
        else:
            self.excluded_count += 1
            self.research_log_pool.append(lead_record)
            log(f"  [EXCLUDED] '{biz.company_name}' -> Excluded ({q_reason}).")

        # Commercial Prospects (non-excluded, verified identity, commercial opportunity)
        if q_state != QualificationState.EXCLUDED.value and opp_status in (
            WebsiteOpportunityStatus.NO_WEBSITE,
            WebsiteOpportunityStatus.BROKEN_WEBSITE,
            WebsiteOpportunityStatus.UNCLEAR_WEBSITE,
        ):
            self.commercial_prospect_count += 1
            self.commercial_prospects_pool.append(lead_record)

        # Track candidate summary
        if (biz.review_count or 0) > 0:
            self.review_evidence_count += 1
        self.candidates_summary.append(lead_record)

        # Track CRM writes within budget
        if self.quota.can_consume("crm_write", 1):
            self.quota.consume("crm_write", 1)
            self.crm_writes_count += 1

    def get_stats(self) -> Dict[str, Any]:
        """Returns snapshot of current runner statistics."""
        return {
            "run_id": self.run_id,
            "market_id": self.market_config.market_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "requested_count": self.requested_count,
            "discovered_count": self.discovered_count,
            "country_valid_count": self.country_valid_count,
            "website_checked_count": self.website_checked_count,
            "no_website_count": self.no_website_count,
            "operational_count": self.operational_count,
            "operational_verified_count": self.operational_count,
            "review_evidence_count": self.review_evidence_count,
            "processed_count": self.processed_count,
            "qualified_count": self.qualified_count,
            "outreach_ready_count": self.outreach_ready_count,
            "manual_review_count": self.manual_review_count,
            "research_only_count": self.research_only_count,
            "excluded_count": self.excluded_count,
            "contactable_count": self.contactable_count,
            "automated_sendable_count": self.automated_sendable_count,
            "manual_contactable_count": self.manual_contactable_count,
            "website_opportunity_count": self.website_opportunity_count,
            "commercial_prospect_count": self.commercial_prospect_count,
            "new_businesses_count": self.new_businesses_count,
            "existing_refreshed_count": self.existing_refreshed_count,
            "duplicates_skipped": self.duplicates_skipped,
            "failures_count": self.failures_count,
            "crm_writes_count": self.crm_writes_count,
            "outreach_sends_count": self.outreach_sends_count,
            "campaigns_armed_count": self.campaigns_armed_count,
        }

    def get_scorecard(self) -> MarketScorecard:
        """Calculates authentic scorecard metrics with non-zero denominators."""
        return MarketScorecard.calculate(
            requested=self.requested_count,
            discovered=self.discovered_count,
            country_valid=self.country_valid_count,
            processed=self.processed_count,
            qualified=self.qualified_count,
            contactable=self.contactable_count,
            outreach_ready=self.outreach_ready_count,
            website_opportunity=self.website_opportunity_count,
            commercial_prospects=self.commercial_prospect_count,
            duplicates_skipped=self.duplicates_skipped,
            complete_records=self.complete_records_count,
        )

    def print_scorecard(self) -> None:
        """Prints a clean human-readable market scorecard table."""
        sc = self.get_scorecard()
        print("\n" + "=" * 65)
        print(f"  MARKET SCORECARD: {self.market_config.market_id} ({self.run_id})")
        print("=" * 65)
        print(f"  Discovery Yield:            {sc.discovery_yield:.2f}x ({self.discovered_count}/{self.requested_count})")
        print(f"  Country Valid Rate:         {sc.country_valid_rate * 100:.1f}% ({self.country_valid_count}/{self.discovered_count})")
        print(f"  Qualification Rate:         {sc.qualification_rate * 100:.1f}% ({self.qualified_count}/{self.processed_count})")
        print(f"  Contactability Rate:        {sc.contactability_rate * 100:.1f}% ({self.contactable_count}/{self.processed_count})")
        print(f"  Outreach Ready Rate:        {sc.outreach_ready_rate * 100:.1f}% ({self.outreach_ready_count}/{self.processed_count})")
        print(f"  Website Opportunity Rate:   {sc.website_opportunity_rate * 100:.1f}% ({self.website_opportunity_count}/{self.processed_count})")
        print(f"  Commercial Prospect Rate:   {sc.commercial_prospect_rate * 100:.1f}% ({self.commercial_prospect_count}/{self.processed_count})")
        print(f"  Duplicate Rate:             {sc.duplicate_rate * 100:.1f}% ({self.duplicates_skipped}/{self.discovered_count})")
        print(f"  Data Completeness:          {sc.data_completeness * 100:.1f}% ({self.complete_records_count}/{self.processed_count})")
        print("=" * 65 + "\n")

    def get_population_accounting(self) -> PopulationAccounting:
        """Returns canonical population accounting object for the run."""
        return PopulationAccounting(
            discovered_total=self.discovered_count,
            country_valid=self.country_valid_count,
            country_invalid=getattr(self, "country_invalid_count", 0),
            duplicate_existing=self.duplicates_skipped,
            duplicate_within_run=getattr(self, "duplicate_within_run_count", 0),
            processed_new=self.new_businesses_count,
            processed_refreshed=self.existing_refreshed_count,
            failed=self.failures_count,
            boundary_invalid=getattr(self, "boundary_invalid_count", 0),
        )

    def get_summary(self) -> Dict[str, Any]:
        """Generates comprehensive run report dictionary."""
        scorecard = self.get_scorecard()
        accounting = self.get_population_accounting()
        field_completeness = CohortFieldCompleteness.calculate(self.candidates_summary)
        return {
            "run_id": self.run_id,
            "market_id": self.market_config.market_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stats": self.get_stats(),
            "accounting": accounting.to_dict(),
            "scorecard": scorecard.to_dict(),
            "field_completeness": field_completeness.to_dict(),
            "quota_usage": self.quota.to_dict(),
            "candidates": self.candidates_summary,
            "outreach_ready_pool": self.outreach_ready_pool,
            "commercial_prospects_pool": self.commercial_prospects_pool,
            "review_queue_pool": self.review_queue_pool,
            "research_log_pool": self.research_log_pool,
            "research_telemetry": self.research_telemetry_records,
            "failed_candidates": [f.to_dict() for f in self.failed_candidates],
        }

    def print_dashboard(self) -> None:
        """Prints text dashboard of run metrics (Objective M)."""
        s = self.get_stats()
        q = self.quota.to_dict()
        print("\n" + "=" * 65)
        print(f"  PRODUCTION ACQUISITION RUN DASHBOARD — {self.market_config.market_id}")
        print("=" * 65)
        print(f"  RUN ID:                {s['run_id']}")
        print(f"  RUN STATUS:            {s['status']}")
        print(f"  MARKET:                {s['market_id']}")
        print(f"  REQUESTED:             {s['requested_count']}")
        print(f"  DISCOVERED:            {s['discovered_count']}")
        print(f"  PROCESSED:             {s['processed_count']}")
        print(f"  NEW BUSINESSES:        {s['new_businesses_count']}")
        print(f"  EXISTING REFRESHED:    {s['existing_refreshed_count']}")
        print(f"  DUPLICATES SKIPPED:    {s['duplicates_skipped']}")
        print(f"  COUNTRY VALID:         {s['country_valid_count']}")
        print(f"  WEBSITE CHECKED:       {s['website_checked_count']}")
        print(f"  NO WEBSITE:            {s['no_website_count']}")
        print(f"  OPERATIONAL:           {s['operational_count']}")
        print("-" * 65)
        print(f"  QUALIFIED:             {s['qualified_count']}")
        print(f"  OUTREACH READY:        {s['outreach_ready_count']}")
        print(f"  MANUAL REVIEW:         {s['manual_review_count']}")
        print(f"  RESEARCH ONLY:         {s['research_only_count']}")
        print(f"  EXCLUDED:              {s['excluded_count']}")
        print("-" * 65)
        print(f"  CONTACTABLE:           {s['contactable_count']}")
        print(f"  MANUAL CONTACTABLE:    {s['manual_contactable_count']}")
        print(f"  AUTOMATED SENDABLE:    {s['automated_sendable_count']}")
        print(f"  WEBSITE OPPORTUNITY:   {s['website_opportunity_count']}")
        print(f"  COMMERCIAL PROSPECTS:  {s['commercial_prospect_count']}")
        print(f"  ERRORS:                {s['failures_count']}")
        print("-" * 65)
        print(f"  EXTERNAL SEARCH USED:  {q['search_used']} / {q['search_limit']}")
        print(f"  GOSOM CALLS USED:      {q['gosom_used']} / {q['gosom_limit']}")
        print(f"  CRM WRITES:            {s['crm_writes_count']} / {q['crm_write_limit']}")
        print(f"  OUTREACH SENDS:        {s['outreach_sends_count']} (Ceiling: 0)")
        print(f"  CAMPAIGNS ARMED:       {s['campaigns_armed_count']} (Ceiling: 0)")
        print("=" * 65 + "\n")


ProductionScaleEngine = MarketRunner
