import re


def _format_optional_metric(value) -> str:
    """Render missing evidence as UNKNOWN; preserve a genuine numeric zero."""
    return "UNKNOWN" if value is None else str(value)


def _candidate_review_freshness(business) -> str:
    """Prefer the review enrichment result over a stale/default model attribute."""
    raw_data = getattr(business, "raw_data", None)
    if isinstance(raw_data, dict):
        enrichment = raw_data.get("review_enrichment")
        if isinstance(enrichment, dict):
            freshness = enrichment.get("review_freshness")
            if isinstance(freshness, str) and freshness.strip():
                return freshness.strip().upper()

    freshness = getattr(business, "review_freshness", None)
    return str(freshness).strip().upper() if freshness else "UNKNOWN"


def _qualification_reason_bucket(state: str, reason: str) -> str:
    """Return a stable, non-business-identifying bucket for aggregate diagnostics."""
    text = str(reason or "").strip().lower()
    if not text:
        return "missing_reason"

    rules = (
        ("country mismatch", "country_mismatch"),
        ("permanently closed", "closed_or_unverified"),
        ("official website confirmed", "website_exists"),
        ("listed website is broken", "website_broken"),
        ("website existence unclear", "website_unclear"),
        ("website status", "website_status_unresolved"),
        ("conflicting review/rating data", "review_evidence_conflict"),
        ("conflicting review evidence", "review_evidence_conflict"),
        ("review data is stale", "review_evidence_stale"),
        ("provider not configured", "review_provider_not_configured"),
        ("provider unavailable", "review_provider_unavailable"),
        ("provider timed out", "review_provider_timeout"),
        ("provider failed", "review_provider_failed"),
        ("quota exceeded", "review_provider_quota_exceeded"),
        ("evidence extraction failed", "review_evidence_extraction_failed"),
        ("identity/branch mismatch", "review_identity_mismatch"),
        ("genuinely no review evidence found", "review_evidence_not_found"),
        ("review data unknown / missing", "review_data_missing"),
        ("minimal customer review traction", "review_count_too_low"),
        ("moderate customer traction", "review_count_below_threshold"),
        ("low rating", "rating_or_traction_below_threshold"),
        ("rating unknown / missing", "rating_missing"),
        ("is below", "rating_below_threshold"),
        ("commercial signals", "insufficient_commercial_signals"),
        ("social identity unverified", "social_ownership_unverified"),
        ("no verified business-owned social presence", "social_ownership_unverified"),
        ("lacks dual independent corroboration", "operational_evidence_not_corroborated"),
        ("current operational status unknown", "operational_status_unknown"),
        ("operational status", "operational_status_not_confirmed"),
        ("unresolved identity contradiction", "identity_contradiction"),
    )
    for phrase, bucket in rules:
        if phrase in text:
            return bucket

    state_slug = re.sub(r"[^a-z0-9]+", "_", str(state or "unknown").lower()).strip("_")
    if state_slug == "outreach_ready" or text.startswith("established "):
        return "all_hard_gates_passed"
    return "other"


import json
import uuid
import time
from typing import List, Dict, Any, Callable, Optional, Set
from datetime import datetime

from lib.types import (
    DiscoveredBusiness,
    Lead,
    ResearchLogEntry,
    WebsiteStatus,
    VerificationStatus,
    CountryStatus,
    DisqualificationReason,
    Priority,
    LeadStatus,
    ProcessingState,
    QualificationState,
    SocialStatus,
    SocialOwnershipStatus,
    SocialActivityStatus,
    SocialProfileStatus,
    OperationalStatus,
    OperationalConfidence,
    EvidenceFreshness,
    OutreachStatus,
    OutreachMode,
    CallOutcome,
    SourceFamily,
)
from lib.discovery.hybrid import HybridDiscoveryEngine
from lib.website.detector import NodeWebsiteDetectionProvider
from lib.verification.no_website_verifier import NoWebsiteVerificationProvider
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.outreach.outreach_generator import OutreachAngleGenerator
from lib.sheets.google_sheets import GoogleSheetsStorageProvider
from lib.validation.country_validator import CountryValidator
from lib.enrichment.review_rating_enricher import ReviewEvidenceItem, ReviewRatingEnricher
from lib.enrichment.gosom_fallback import GosomReviewFreshnessFallback

class LeadGenerationPipeline:
    """
    Production-ready orchestrator for finding confirmed NO-WEBSITE qualified leads.
    Enforces:
      - 'Qualified Leads Needed': continues discovery/research until target is met or max multiplier reached.
      - Strict post-discovery target country validation (mismatches immediately excluded).
      - Inexpensive filtering first (Country -> Deduplication -> Website Field -> Deep Search -> Qualification).
      - Dual Google Sheets tabs: LEADS (qualified only) and RESEARCH_LOG (all attempts/rejections).
      - Phone number text safety (prevents Google Sheets formula #ERROR!).
    """
    def __init__(
        self,
        discovery_provider: Optional[Any] = None,
        detection_provider: Optional[NodeWebsiteDetectionProvider] = None,
        verification_provider: Optional[NoWebsiteVerificationProvider] = None,
        scoring_provider: Optional[LeadScoringProvider] = None,
        outreach_provider: Optional[OutreachAngleGenerator] = None,
        storage_provider: Optional[GoogleSheetsStorageProvider] = None,
        country_validator: Optional[CountryValidator] = None,
        gosom_fallback: Optional[GosomReviewFreshnessFallback] = None,
        review_enricher: Optional[ReviewRatingEnricher] = None,
        shadow_mode: bool = False
    ):
        self.discovery = discovery_provider or HybridDiscoveryEngine()
        self.detector = detection_provider or NodeWebsiteDetectionProvider()
        self.verifier = verification_provider or NoWebsiteVerificationProvider(web_search_provider=getattr(self.discovery, "web_search", None))
        self.scorer = scoring_provider or LeadScoringProvider.create_optimized()
        self.outreach = outreach_provider or OutreachAngleGenerator()
        self.storage = storage_provider or GoogleSheetsStorageProvider()
        self.country_validator = country_validator or CountryValidator()
        self.gosom_fallback = gosom_fallback
        self.review_enricher = review_enricher or ReviewRatingEnricher(web_search_provider=getattr(self.discovery, "web_search", None))
        self.shadow_mode = shadow_mode

    def run(
        self,
        country: str,
        cities: List[str],
        industry: str,
        requested_qualified_leads: int = 10,
        batch_size: int = 10,
        max_research_multiplier: int = 5,
        min_positive_signals: int = 2,
        inject_test_candidates: Optional[List[DiscoveredBusiness]] = None,
        candidate_places_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        log_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        resume_processed_ids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Executes the research pipeline until requested_qualified_leads is met or max research limit reached.
        Supports checkpoint resumption via resume_processed_ids to avoid reprocessing completed candidates.
        """
        completed_candidate_ids: set = set(resume_processed_ids or [])
        discovery_seen_identities: set = set(completed_candidate_ids)

        def emit_log(msg: str, data: Optional[Dict[str, Any]] = None):
            print(msg)
            if log_callback:
                try:
                    payload = dict(data or {})
                    if "processed_ids" not in payload:
                        payload["processed_ids"] = list(completed_candidate_ids)
                    if "stats" not in payload:
                        try:
                            # `stats` belongs to the enclosing run() scope, not emit_log() locals.
                            # Header messages emitted before stats initialization simply omit it.
                            payload["stats"] = dict(stats)
                        except NameError:
                            pass
                    log_callback(msg, payload)
                except Exception as ex:
                    print(f"Callback error: {ex}")

        def get_candidate_identifiers(biz: DiscoveredBusiness) -> set:
            keys = set()
            norm_id = self.storage.normalize_identity(biz.company_name, biz.city, country, biz.raw_website)
            if norm_id:
                keys.add(norm_id)
            if getattr(biz, "place_id", None):
                keys.add(f"place:{biz.place_id}")
            if getattr(biz, "osm_id", None):
                keys.add(f"osm:{biz.osm_id}")
            if biz.company_name:
                cname_clean = re.sub(r'[^a-z0-9]', '', biz.company_name.lower())
                keys.add(f"bizname:{cname_clean}@{country.lower()}")
            return keys

        def record_candidate_completed(biz: DiscoveredBusiness, entry: Optional[ResearchLogEntry] = None):
            if entry:
                all_research_entries.append(entry)
            c_keys = get_candidate_identifiers(biz)
            completed_candidate_ids.update(c_keys)
            checkpoint_payload = {
                "checkpoint_update": True,
                "processed_ids": list(completed_candidate_ids),
                "stats": dict(stats),
                "pipeline_run_id": stats.get("pipeline_run_id", ""),
            }
            emit_log(
                f"  [Checkpoint Updated] Candidate '{biz.company_name}' recorded in checkpoint ({len(completed_candidate_ids)} IDs).",
                checkpoint_payload,
            )
            # Lively Google Sheets sync
            sync_sheets_lively(force_all=False)

        start_time = time.time()
        max_valid_research = requested_qualified_leads * max_research_multiplier
        target_city = cities[0] if cities else "Global"
        city_slug = re.sub(r'[^a-zA-Z0-9]', '', target_city[:3].upper()) or "GLB"
        pipeline_run_id = f"PIPE-{city_slug}-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"

        emit_log(f"\n=======================================================")
        emit_log(f"[DRIPP MEDIA] QUALIFICATION ENGINE V3")
        emit_log(f"PIPELINE RUN ID: {pipeline_run_id}")
        emit_log(f"Target: {industry} in {', '.join(cities)}, {country}")
        emit_log(f"QUALIFIED LEADS NEEDED (OUTREACH_READY): {requested_qualified_leads}")
        emit_log(f"Max Valid Research Limit: {max_valid_research} (Multiplier: {max_research_multiplier}x, Batch: {batch_size})")
        if resume_processed_ids:
            emit_log(f"Checkpoint Resumption: Active ({len(completed_candidate_ids)} candidate IDs previously processed)")
        emit_log(f"Note: Only genuine OUTREACH_READY candidates count toward target.")
        emit_log(f"Architecture: Post-pipeline outreach decoupled from execution.")
        emit_log(f"=======================================================")

        stats = {
            "pipeline_run_id": pipeline_run_id,
            "requested_qualified_leads": requested_qualified_leads,
            "raw_discovered": 0,
            "target_country_matches": 0,
            "wrong_country_results": 0,
            "duplicates": 0,
            "valid_candidates_researched": 0,
            "businesses_researched": 0,
            "country_matches": 0,
            "country_mismatches": 0,
            "country_unclear": 0,
            "website_exists": 0,
            "no_website_confirmed": 0,
            "website_unclear": 0,
            "website_broken": 0,
            "not_qualified": 0,
            "active_confirmed": 0,
            "social_verified": 0,
            "outreach_ready": 0,
            "manual_review": 0,
            "research_only": 0,
            "excluded": 0,
            "qualified_leads": 0,
            "high_priority": 0,
            "medium_priority": 0,
            "low_priority": 0,
            "saved_to_leads": 0,
            "saved_to_review_queue": 0,
            "saved_to_research_log": 0
        }

        query_stats: Dict[str, Dict[str, int]] = {}
        outreach_ready_leads: List[Lead] = []
        review_queue_leads: List[Lead] = []
        all_research_entries: List[ResearchLogEntry] = []
        candidate_pool: List[DiscoveredBusiness] = []

        written_lead_ids: Set[str] = set()
        written_review_ids: Set[str] = set()
        written_research_ids: Set[str] = set()

        def sync_sheets_lively(force_all: bool = False):
            if self.shadow_mode:
                return

            # Lively append new outreach-ready leads to LEADS tab
            new_leads = [ld for ld in outreach_ready_leads if ld.lead_id not in written_lead_ids]
            if new_leads:
                try:
                    ins_l, upd_l = self.storage.save_qualified_leads(new_leads)
                    for ld in new_leads:
                        written_lead_ids.add(ld.lead_id)
                    stats["saved_to_leads"] = len(written_lead_ids)
                    emit_log(f"  [Google Sheets Lively] Appended {len(new_leads)} outreach-ready lead(s) to LEADS tab (Total live synced: {len(written_lead_ids)}).")
                except Exception as ex:
                    emit_log(f"  [Google Sheets Live Warning] Could not live-save to LEADS tab: {ex}")

            # Lively append new manual review leads to REVIEW_QUEUE tab
            new_reviews = [rv for rv in review_queue_leads if rv.lead_id not in written_review_ids]
            if new_reviews:
                try:
                    ins_r, upd_r = self.storage.save_review_queue(new_reviews)
                    for rv in new_reviews:
                        written_review_ids.add(rv.lead_id)
                    stats["saved_to_review_queue"] = len(written_review_ids)
                    emit_log(f"  [Google Sheets Lively] Appended {len(new_reviews)} candidate(s) to REVIEW_QUEUE tab (Total live synced: {len(written_review_ids)}).")
                except Exception as ex:
                    emit_log(f"  [Google Sheets Live Warning] Could not live-save to REVIEW_QUEUE tab: {ex}")

            # Lively append research entries to RESEARCH_LOG tab
            new_researches = [rs for rs in all_research_entries if rs.research_id not in written_research_ids]
            if new_researches and (force_all or len(new_researches) >= 5):
                try:
                    ins_s, upd_s = self.storage.save_research_log(new_researches)
                    for rs in new_researches:
                        written_research_ids.add(rs.research_id)
                    stats["saved_to_research_log"] = len(written_research_ids)
                    emit_log(f"  [Google Sheets Lively] Appended {len(new_researches)} entry(ies) to RESEARCH_LOG tab (Total live synced: {len(written_research_ids)}).")
                except Exception as ex:
                    emit_log(f"  [Google Sheets Live Warning] Could not live-save to RESEARCH_LOG tab: {ex}")

        # If test probe candidates (e.g. Manchester, NH / Manchester, MI) are injected for testing:
        if inject_test_candidates:
            for biz in inject_test_candidates:
                c_keys = get_candidate_identifiers(biz)
                if any(k in completed_candidate_ids for k in c_keys):
                    stats["duplicates"] += 1
                    emit_log(f"  [Checkpoint / Duplicate Skip] '{biz.company_name}' already processed in prior checkpoint. Skipping.")
                    continue
                if any(k in discovery_seen_identities for k in c_keys):
                    stats["duplicates"] += 1
                    continue
                discovery_seen_identities.update(c_keys)

                stats["raw_discovered"] += 1
                q = biz.discovery_query or "Injected test candidate"
                if q not in query_stats:
                    query_stats[q] = {"candidates": 0, "qualified": 0}
                query_stats[q]["candidates"] += 1

                c_status, det_country, reg, pcode, c_evidence = self.country_validator.validate(
                    target_country=country,
                    address=biz.address,
                    phone=biz.phone,
                    raw_data=biz.raw_data
                )
                biz.country_status = c_status
                biz.detected_country = det_country
                biz.region = reg or biz.region
                biz.postcode = pcode or biz.postcode
                biz.country_evidence = c_evidence

                if c_status != CountryStatus.COUNTRY_MATCH.value:
                    stats["wrong_country_results"] += 1
                    stats["country_mismatches"] += 1
                    emit_log(f"  [Country Mismatch] '{biz.company_name}' ({det_country} vs {country}) -> EXCLUDED (Does NOT consume valid research budget)")
                    res_entry = ResearchLogEntry(
                        research_id=f"RES-{uuid.uuid4().hex[:6].upper()}",
                        company_name=biz.company_name,
                        industry=biz.category or industry,
                        target_country=country,
                        detected_country=det_country,
                        country_status=c_status,
                        city=biz.city,
                        region=biz.region,
                        postcode=biz.postcode,
                        address=biz.address,
                        phone=biz.phone,
                        website=biz.raw_website,
                        website_status="",
                        verification_status="",
                        review_count=biz.review_count,
                        rating=biz.rating,
                        social_status=biz.social_status,
                        qualification_status="EXCLUDED",
                        disqualification_reason=DisqualificationReason.COUNTRY_MISMATCH.value,
                        source_url=biz.google_maps_url,
                        date_researched=datetime.now().strftime("%Y-%m-%d")
                    )
                    record_candidate_completed(biz, res_entry)
                else:
                    stats["target_country_matches"] += 1
                    stats["country_matches"] += 1
                    candidate_pool.append(biz)

        discovery_round = 1

        while len(outreach_ready_leads) < requested_qualified_leads and stats["valid_candidates_researched"] < max_valid_research:
            # Check if discovery needed: pool is low OR remaining pool only contains known website owners
            needs_discovery = False
            if len(candidate_pool) < batch_size:
                needs_discovery = True
            else:
                unverified_candidates = [
                    b for b in candidate_pool
                    if not (b.raw_website or "").strip() or any(
                        p in (b.raw_website or "").lower()
                        for p in ["instagram.com", "facebook.com", "tiktok.com", "deliveroo", "ubereats", "just-eat", "linktr.ee"]
                    )
                ]
                if not unverified_candidates and self.discovery.has_more_rounds(discovery_round, target_city, country, industry):
                    emit_log(f"\n[Notice] Remaining candidate pool only has businesses with listed websites. Triggering next discovery round for independent/neighborhood variety...")
                    needs_discovery = True

            if needs_discovery and not inject_test_candidates and self.discovery.has_more_rounds(discovery_round, target_city, country, industry):
                p_name = getattr(self.discovery, "name", "Discovery Provider")
                emit_log(f"\n[Discovery Round {discovery_round}] Fetching candidates via {p_name}...")
                try:
                    discovered, queries_used = self.discovery.discover_round(
                        round_number=discovery_round,
                        country=country,
                        cities=cities,
                        industry=industry,
                        limit=40
                    )
                    stats["raw_discovered"] += len(discovered)
                    emit_log(f"[OK] Discovered {len(discovered)} raw candidate places across {len(queries_used)} queries.")
                    discovery_round += 1

                    for biz in discovered:
                        q = biz.discovery_query or (queries_used[0] if queries_used else "General")
                        if q not in query_stats:
                            query_stats[q] = {"candidates": 0, "qualified": 0}
                        query_stats[q]["candidates"] += 1

                        # Deduplication and Checkpoint Check
                        c_keys = get_candidate_identifiers(biz)
                        if any(k in completed_candidate_ids for k in c_keys):
                            stats["duplicates"] += 1
                            emit_log(f"  [Checkpoint / Duplicate Skip] '{biz.company_name}' already processed in prior checkpoint. Skipping.")
                            continue
                        if any(k in discovery_seen_identities for k in c_keys):
                            stats["duplicates"] += 1
                            continue
                        discovery_seen_identities.update(c_keys)

                        # EARLY TARGET COUNTRY VALIDATION (Section 6: Immediate filter)
                        c_status, det_country, reg, pcode, c_evidence = self.country_validator.validate(
                            target_country=country,
                            address=biz.address,
                            phone=biz.phone,
                            raw_data=biz.raw_data
                        )
                        biz.country_status = c_status
                        biz.detected_country = det_country
                        biz.region = reg or biz.region
                        biz.postcode = pcode or biz.postcode
                        biz.country_evidence = c_evidence

                        if c_status != CountryStatus.COUNTRY_MATCH.value:
                            stats["wrong_country_results"] += 1
                            stats["country_mismatches"] += 1
                            stats["excluded"] += 1
                            emit_log(f"  [Country Mismatch] '{biz.company_name}' ({det_country} vs {country} - {c_evidence}) -> EXCLUDED immediately (Does NOT consume valid research budget)")
                            res_entry = ResearchLogEntry(
                                research_id=f"RES-{uuid.uuid4().hex[:6].upper()}",
                                company_name=biz.company_name,
                                industry=biz.category or industry,
                                target_country=country,
                                detected_country=det_country,
                                country_status=c_status,
                                city=biz.city,
                                region=biz.region,
                                postcode=biz.postcode,
                                address=biz.address,
                                phone=biz.phone,
                                website=biz.raw_website,
                                website_status="",
                                verification_status="",
                                review_count=biz.review_count,
                                rating=biz.rating,
                                social_status=biz.social_status,
                                qualification_state=QualificationState.EXCLUDED.value,
                                qualification_status="EXCLUDED",
                                disqualification_reason=DisqualificationReason.COUNTRY_MISMATCH.value,
                                source_url=biz.google_maps_url,
                                date_researched=datetime.now().strftime("%Y-%m-%d")
                            )
                            record_candidate_completed(biz, res_entry)
                            continue

                        # Target-country match!
                        stats["target_country_matches"] += 1
                        stats["country_matches"] += 1
                        candidate_pool.append(biz)

                    # Prioritize candidate pool:
                    # Priority 0: Missing website field (raw_website == "") -> researched first
                    # Priority 1: Platform/social profiles (Instagram, Facebook, Deliveroo, etc.)
                    # Priority 2: Listed website
                    def candidate_sort_key(b: DiscoveredBusiness) -> int:
                        w = (b.raw_website or "").strip().lower()
                        if not w:
                            return 0
                        if any(p in w for p in ["instagram.com", "facebook.com", "tiktok.com", "deliveroo", "ubereats", "just-eat", "linktr.ee"]):
                            return 1
                        return 2

                    candidate_pool.sort(key=candidate_sort_key)

                except Exception as disc_err:
                    emit_log(f"[Error] Discovery error: {disc_err}")
                    if stats["raw_discovered"] == 0 and not candidate_pool and not outreach_ready_leads and not review_queue_leads:
                        emit_log(f"[FATAL ERROR] Discovery provider outage resulted in 0 candidates: {disc_err}")
                        raise RuntimeError(f"Discovery provider failure: {disc_err}") from disc_err
                    break

            if not candidate_pool:
                emit_log("[Notice] Discovery exhausted: No further valid target-country candidates available.")
                break

            # Extract next batch
            current_batch = []
            while candidate_pool and len(current_batch) < batch_size and (stats["valid_candidates_researched"] + len(current_batch)) < max_valid_research:
                current_batch.append(candidate_pool.pop(0))

            if not current_batch:
                if stats["valid_candidates_researched"] >= max_valid_research:
                    emit_log(f"[Notice] Reached maximum valid research budget ({max_valid_research} valid target-country candidates researched).")
                break

            emit_log(f"\n--- [Processing Batch] ({len(current_batch)} valid candidates | Outreach-Ready: {len(outreach_ready_leads)}/{requested_qualified_leads} | Manual Review: {len(review_queue_leads)}) ---")

            # Collect queries for candidates needing public search verification
            search_queries_needed = []
            biz_to_query_map = {}
            for biz in current_batch:
                det = self.detector.detect_website(biz.raw_website)
                if det["website_status"] != WebsiteStatus.WEBSITE_EXISTS.value:
                    q = f"{biz.company_name} {biz.city}"
                    search_queries_needed.append(q)
                    biz_to_query_map[biz.company_name] = q

            cached_searches = {}
            if search_queries_needed and (getattr(self.verifier, "apify_client", None) or getattr(self.verifier, "web_search_provider", None)):
                emit_log(f"[Verification] Web search verification for {len(search_queries_needed)} candidates...")
                cached_searches = self.verifier.batch_web_search(search_queries_needed)

            # Process each candidate in current batch
            for biz in current_batch:
                if len(outreach_ready_leads) >= requested_qualified_leads:
                    emit_log(f"[Target Reached] Found {requested_qualified_leads} OUTREACH_READY leads!")
                    break

                if stats["valid_candidates_researched"] >= max_valid_research:
                    emit_log(f"[Limit Reached] Reached maximum valid research limit ({max_valid_research} valid target-country candidates).")
                    break

                # Valid target-country candidate researched
                stats["valid_candidates_researched"] += 1
                stats["businesses_researched"] = stats["valid_candidates_researched"]

                try:
                    emit_log(f"\n[Candidate {stats['valid_candidates_researched']}/{max_valid_research}] '{biz.company_name}' ({biz.city})")
                    emit_log(f"  • Address: {biz.address or 'N/A'}")
                    emit_log(f"  • Phone: {biz.phone or 'N/A'}")
                    emit_log(f"  • Reviews: {_format_optional_metric(biz.review_count)} (Rating: {_format_optional_metric(biz.rating)})")

                    # Fast detection on raw website field
                    det_result = self.detector.detect_website(biz.raw_website)
                    if det_result["website_status"] == WebsiteStatus.WEBSITE_EXISTS.value:
                        stats["website_exists"] += 1
                        stats["excluded"] += 1
                        clean_web = det_result["clean_website"]
                        emit_log(f"  [Website Exists] '{clean_web}' -> EXCLUDED from No-Website leads")
                        res_entry = ResearchLogEntry(
                            research_id=f"RES-{uuid.uuid4().hex[:6].upper()}",
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            detected_country=biz.detected_country or country,
                            country_status=biz.country_status,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website=clean_web,
                            website_status=WebsiteStatus.WEBSITE_EXISTS.value,
                            verification_status=VerificationStatus.WEBSITE_EXISTS.value,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=biz.social_status,
                            qualification_state=QualificationState.EXCLUDED.value,
                            qualification_status="EXCLUDED",
                            disqualification_reason=DisqualificationReason.WEBSITE_EXISTS.value,
                            source_url=biz.google_maps_url,
                            date_researched=datetime.now().strftime("%Y-%m-%d")
                        )
                        record_candidate_completed(biz, res_entry)
                        continue

                    elif det_result["website_status"] == WebsiteStatus.WEBSITE_BROKEN.value:
                        stats["website_broken"] += 1
                        stats["manual_review"] += 1
                        clean_web = det_result["clean_website"]
                        emit_log(f"  [Website Broken] '{clean_web}' -> MANUAL_REVIEW: Preserved in REVIEW_QUEUE for website recovery")
                        city_slug = biz.city[:3].upper() if biz.city else "LOC"
                        rev_lead = Lead(
                            lead_id=f"REV-{city_slug}-{uuid.uuid4().hex[:6].upper()}",
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            country=biz.detected_country or country,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website=clean_web,
                            website_status=WebsiteStatus.WEBSITE_BROKEN.value,
                            verification_status=VerificationStatus.WEBSITE_BROKEN.value,
                            verification_reason="Listed website is broken/unreachable. Potential website recovery candidate.",
                            google_maps_url=biz.google_maps_url,
                            instagram_url=biz.instagram_url,
                            facebook_url=biz.facebook_url,
                            tiktok_url=biz.tiktok_url,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=biz.social_status,
                            priority=Priority.MANUAL_REVIEW.value,
                            qualification_state=QualificationState.MANUAL_REVIEW.value,
                            qualification_reason="Listed website is broken/unreachable. Human audit recommended.",
                            red_flags="Listed website broken or unreachable",
                            lead_status=LeadStatus.NOT_CONTACTED.value,
                            date_added=datetime.now().strftime("%Y-%m-%d"),
                            processing_state="MANUAL_REVIEW"
                        )
                        review_queue_leads.append(rev_lead)
                        res_entry = ResearchLogEntry(
                            research_id=f"RES-{uuid.uuid4().hex[:6].upper()}",
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            detected_country=biz.detected_country or country,
                            country_status=biz.country_status,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website=clean_web,
                            website_status=WebsiteStatus.WEBSITE_BROKEN.value,
                            verification_status=VerificationStatus.WEBSITE_BROKEN.value,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=biz.social_status,
                            qualification_state=QualificationState.MANUAL_REVIEW.value,
                            qualification_status="MANUAL_REVIEW",
                            disqualification_reason=DisqualificationReason.WEBSITE_BROKEN.value,
                            red_flags="Website broken/unreachable",
                            source_url=biz.google_maps_url,
                            date_researched=datetime.now().strftime("%Y-%m-%d")
                        )
                        record_candidate_completed(biz, res_entry)
                        continue

                    # Deep No-Website Verification (Section 4)
                    search_q = biz_to_query_map.get(biz.company_name)
                    search_results = cached_searches.get(search_q, []) if search_q else []
                    ver_res = self.verifier.verify_business(biz, cached_search_results=search_results)
                    v_status = ver_res["verification_status"]
                    v_reason = ver_res["verification_reason"]
                    clean_web = ver_res["verified_website"]
                    evidence = ver_res["evidence"]

                    if v_status == VerificationStatus.WEBSITE_EXISTS.value:
                        stats["website_exists"] += 1
                        stats["excluded"] += 1
                        emit_log(f"  [Website Exists] Official Domain Identified: '{clean_web}' -> EXCLUDED")
                        res_entry = ResearchLogEntry(
                            research_id=f"RES-{uuid.uuid4().hex[:6].upper()}",
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            detected_country=biz.detected_country or country,
                            country_status=biz.country_status,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website=clean_web,
                            website_status=WebsiteStatus.WEBSITE_EXISTS.value,
                            verification_status=VerificationStatus.WEBSITE_EXISTS.value,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=biz.social_status,
                            qualification_state=QualificationState.EXCLUDED.value,
                            qualification_status="EXCLUDED",
                            disqualification_reason=DisqualificationReason.WEBSITE_EXISTS.value,
                            source_url=biz.google_maps_url,
                            date_researched=datetime.now().strftime("%Y-%m-%d")
                        )
                        record_candidate_completed(biz, res_entry)
                        continue

                    elif v_status == VerificationStatus.WEBSITE_BROKEN.value:
                        stats["website_broken"] += 1
                        stats["manual_review"] += 1
                        emit_log(f"  [Website Broken] '{clean_web}' -> MANUAL_REVIEW: Preserved in REVIEW_QUEUE")
                        city_slug = biz.city[:3].upper() if biz.city else "LOC"
                        rev_lead = Lead(
                            lead_id=f"REV-{city_slug}-{uuid.uuid4().hex[:6].upper()}",
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            country=biz.detected_country or country,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website=clean_web,
                            website_status=WebsiteStatus.WEBSITE_BROKEN.value,
                            verification_status=VerificationStatus.WEBSITE_BROKEN.value,
                            verification_reason=v_reason,
                            google_maps_url=biz.google_maps_url,
                            instagram_url=biz.instagram_url,
                            facebook_url=biz.facebook_url,
                            tiktok_url=biz.tiktok_url,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=biz.social_status,
                            priority=Priority.MANUAL_REVIEW.value,
                            qualification_state=QualificationState.MANUAL_REVIEW.value,
                            qualification_reason=v_reason,
                            red_flags="Website broken/unreachable",
                            lead_status=LeadStatus.NOT_CONTACTED.value,
                            date_added=datetime.now().strftime("%Y-%m-%d"),
                            processing_state="MANUAL_REVIEW"
                        )
                        review_queue_leads.append(rev_lead)
                        res_entry = ResearchLogEntry(
                            research_id=f"RES-{uuid.uuid4().hex[:6].upper()}",
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            detected_country=biz.detected_country or country,
                            country_status=biz.country_status,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website=clean_web,
                            website_status=WebsiteStatus.WEBSITE_BROKEN.value,
                            verification_status=VerificationStatus.WEBSITE_BROKEN.value,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=biz.social_status,
                            qualification_state=QualificationState.MANUAL_REVIEW.value,
                            qualification_status="MANUAL_REVIEW",
                            disqualification_reason=DisqualificationReason.WEBSITE_BROKEN.value,
                            red_flags="Website broken/unreachable",
                            source_url=biz.google_maps_url,
                            date_researched=datetime.now().strftime("%Y-%m-%d")
                        )
                        record_candidate_completed(biz, res_entry)
                        continue

                    elif v_status == VerificationStatus.WEBSITE_UNCLEAR.value:
                        stats["website_unclear"] += 1
                        stats["manual_review"] += 1
                        emit_log(f"  [Website Unclear] '{v_reason}' -> MANUAL_REVIEW: Preserved in REVIEW_QUEUE")
                        city_slug = biz.city[:3].upper() if biz.city else "LOC"
                        rev_lead = Lead(
                            lead_id=f"REV-{city_slug}-{uuid.uuid4().hex[:6].upper()}",
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            country=biz.detected_country or country,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website="",
                            website_status=WebsiteStatus.WEBSITE_UNCLEAR.value,
                            verification_status=VerificationStatus.WEBSITE_UNCLEAR.value,
                            verification_reason=v_reason,
                            google_maps_url=biz.google_maps_url,
                            instagram_url=biz.instagram_url,
                            facebook_url=biz.facebook_url,
                            tiktok_url=biz.tiktok_url,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=biz.social_status,
                            priority=Priority.MANUAL_REVIEW.value,
                            qualification_state=QualificationState.MANUAL_REVIEW.value,
                            qualification_reason=v_reason,
                            red_flags="Website existence unclear; human audit required",
                            lead_status=LeadStatus.NOT_CONTACTED.value,
                            date_added=datetime.now().strftime("%Y-%m-%d"),
                            processing_state="MANUAL_REVIEW"
                        )
                        review_queue_leads.append(rev_lead)
                        res_entry = ResearchLogEntry(
                            research_id=f"RES-{uuid.uuid4().hex[:6].upper()}",
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            detected_country=biz.detected_country or country,
                            country_status=biz.country_status,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website="",
                            website_status=WebsiteStatus.WEBSITE_UNCLEAR.value,
                            verification_status=VerificationStatus.WEBSITE_UNCLEAR.value,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=biz.social_status,
                            qualification_state=QualificationState.MANUAL_REVIEW.value,
                            qualification_status="MANUAL_REVIEW",
                            disqualification_reason=DisqualificationReason.WEBSITE_UNCLEAR.value,
                            red_flags="Website unclear",
                            source_url=biz.google_maps_url,
                            date_researched=datetime.now().strftime("%Y-%m-%d")
                        )
                        record_candidate_completed(biz, res_entry)
                        continue

                    # Confirmed NO WEBSITE!
                    stats["no_website_confirmed"] += 1
                    emit_log(f"  [Confirmed No Website] {v_reason}")

                    # Review & Rating Enrichment for No-Website candidates (if missing)
                    if self.review_enricher and (biz.review_count is None or biz.rating is None):
                        try:
                            emit_log(f"  [Review Enrichment] Enriching review/rating for '{biz.company_name}'...")
                            biz = self.review_enricher.enrich_candidate(biz)
                            if biz.review_count is not None:
                                emit_log(f"    [Review Enrichment Found] {biz.review_count} reviews, {biz.rating}★ (Date: {biz.latest_review_date or 'N/A'})")
                            else:
                                emit_log(f"    [Review Enrichment] No review evidence found for '{biz.company_name}'.")
                        except Exception as renrich_ex:
                            emit_log(f"    [Review Enrichment Error] Failed to enrich '{biz.company_name}': {renrich_ex}")

                    # Creator / Influencer Evidence Enrichment (if supported by discovery engine)
                    if hasattr(self.discovery, "enrich_candidate_creator_evidence"):
                        try:
                            self.discovery.enrich_candidate_creator_evidence(biz, city=biz.city or (cities[0] if cities else ""), country=country)
                        except Exception as crev_ex:
                            emit_log(f"    [Creator Evidence Error] Failed for '{biz.company_name}': {crev_ex}")

                    # QUALIFICATION ENGINE V2 EVALUATION
                    audit = self.scorer.evaluate_lead(
                        business=biz,
                        verification_status=v_status,
                        verification_reason=v_reason,
                        website_evidence=evidence
                    )

                    # REVIEW FRESHNESS CHECK & CONTROLLED GOSOM FALLBACK (Phase 7.12 Integration)
                    if self.gosom_fallback is not None:
                        c_lat = getattr(biz, "latitude", None) if getattr(biz, "latitude", None) is not None else getattr(biz, "lat", None)
                        c_lon = getattr(biz, "longitude", None) if getattr(biz, "longitude", None) is not None else getattr(biz, "lon", None)
                        candidate_review_freshness = _candidate_review_freshness(biz)
                        cand_meta = {
                            "company_name": biz.company_name,
                            "business_name": biz.company_name,
                            "city": biz.city or (cities[0] if cities else "Manchester"),
                            "address": biz.address or "",
                            "street": getattr(biz, "street", "") or "",
                            "postcode": biz.postcode or "",
                            "latitude": c_lat,
                            "longitude": c_lon,
                            "phone": biz.phone or "",
                            "category": biz.category or industry,
                            "review_count": biz.review_count,
                            "rating": biz.rating,
                            "review_freshness": candidate_review_freshness,
                            "qualification_state": audit.get("qualification_state"),
                            "operational_status": audit.get("operational_status"),
                        }

                        is_elig, elig_reason = self.gosom_fallback.is_candidate_eligible(cand_meta)
                        if is_elig:
                            emit_log(f"  [Gosom Fallback Eligible] Review freshness UNKNOWN ({elig_reason}). Evaluating fallback...")
                            existing_revs = []
                            if biz.review_count is not None or biz.rating is not None:
                                existing_revs.append(
                                    ReviewEvidenceItem(
                                        business_name=biz.company_name,
                                        source="Discovery Initial",
                                        source_family=SourceFamily.OPENSTREETMAP.value if getattr(biz, "discovery_source", "") in ["OSM", "OPENSTREETMAP"] else SourceFamily.OTHER_DIRECTORY.value,
                                        rating=float(biz.rating) if biz.rating is not None else None,
                                        review_count=biz.review_count,
                                        evidence_date=biz.latest_review_date,
                                        freshness=candidate_review_freshness
                                    )
                                )

                            preloaded = None
                            if candidate_places_map and biz.company_name in candidate_places_map:
                                preloaded = candidate_places_map[biz.company_name]

                            reconciled, fb_telem = self.gosom_fallback.enrich_candidate(
                                candidate=cand_meta,
                                existing_reviews=existing_revs,
                                preloaded_places=preloaded,
                                shadow_mode=self.shadow_mode
                            )

                            if reconciled is not None:
                                emit_log(f"  [Gosom Fallback Reconciled] Freshness: {reconciled.reconciled_freshness}, Reviews: {reconciled.reconciled_review_count}, Rating: {reconciled.reconciled_rating}")
                                biz.review_count = reconciled.reconciled_review_count
                                biz.rating = reconciled.reconciled_rating
                                biz.latest_review_date = reconciled.reconciled_date or ""
                                if not hasattr(biz, "raw_data") or not isinstance(biz.raw_data, dict):
                                    biz.raw_data = {}
                                biz.raw_data["review_enrichment"] = {
                                    "review_count": reconciled.reconciled_review_count,
                                    "rating": reconciled.reconciled_rating,
                                    "review_freshness": reconciled.reconciled_freshness,
                                    "review_confidence": "CONFLICT" if reconciled.is_material_conflict else "HIGH",
                                    "review_status": "CONFLICT_REQUIRES_REVIEW" if reconciled.is_material_conflict else "OK",
                                    "review_evidence_date": reconciled.reconciled_date,
                                    "source_family": reconciled.primary_source_family,
                                    "reconciliation": reconciled.to_dict()
                                }
                                # Re-run scorer with reconciled review data!
                                audit = self.scorer.evaluate_lead(
                                    business=biz,
                                    verification_status=v_status,
                                    verification_reason=v_reason,
                                    website_evidence=evidence
                                )
                            else:
                                emit_log(f"  [Gosom Fallback Blocked/Failed] Status: {fb_telem.get('status')}, Reason: {fb_telem.get('reason')}")
                        else:
                            emit_log(f"  [Gosom Fallback Skipped] Ineligible: {elig_reason}")

                    q_score = audit["score"]
                    q_priority = audit["priority"]
                    q_state = audit["qualification_state"]
                    q_reason = audit["qualification_reason"]

                    # Persist reason-bucket counts alongside ordinary run metrics. These
                    # aggregate counters explain funnel drop-off without relaxing any gate
                    # or storing candidate names/reason text in the run summary.
                    state_slug = re.sub(r"[^a-z0-9]+", "_", str(q_state or "unknown").lower()).strip("_")
                    reason_bucket = _qualification_reason_bucket(q_state, q_reason)
                    diagnostic_key = f"diagnostic__qualification__{state_slug}__{reason_bucket}"
                    stats[diagnostic_key] = int(stats.get(diagnostic_key, 0)) + 1

                    q_signals = audit["signals"]
                    is_outreach_ready = audit["is_outreach_ready"]
                    evidence_obj = audit["evidence"]
                    red_flags = audit["red_flags"]
                    score_breakdown = audit["score_breakdown"]
                    social_status = audit["social_status"]
                    social_ownership = audit["social_ownership_status"]
                    social_profile_status = audit.get("social_profile_status", SocialProfileStatus.UNKNOWN.value)
                    social_activity = audit["social_activity"]
                    operational_status = audit.get("operational_status", OperationalStatus.OPERATIONAL_UNKNOWN.value)
                    operational_confidence = audit.get("operational_confidence", OperationalConfidence.LOW.value)
                    operational_evidence = audit.get("operational_evidence", "")
                    evidence_freshness = audit.get("evidence_freshness", EvidenceFreshness.UNKNOWN.value)
                    red_flags_str = "; ".join(red_flags) if red_flags else ""
                    city_slug = biz.city[:3].upper() if biz.city else "LOC"

                    if operational_status == OperationalStatus.ACTIVE_CONFIRMED.value:
                        stats["active_confirmed"] += 1
                    if social_ownership in [SocialOwnershipStatus.VERIFIED.value, "VERIFIED_OWNED", "VERIFIED"] or bool(audit.get("verified_social_urls")):
                        stats["social_verified"] += 1

                    if q_state == QualificationState.OUTREACH_READY.value:
                        # OUTREACH_READY LEAD!
                        stats["outreach_ready"] += 1
                        stats["qualified_leads"] += 1
                        if q_priority == Priority.HIGH.value:
                            stats["high_priority"] += 1
                        elif q_priority == Priority.MEDIUM.value:
                            stats["medium_priority"] += 1
                        else:
                            stats["low_priority"] += 1

                        # Track qualified count per discovery query
                        q_used = biz.discovery_query or "General"
                        if q_used in query_stats:
                            query_stats[q_used]["qualified"] += 1

                        # Outreach Angle Generation (strictly evidence-based)
                        outreach_text = self.outreach.generate_angle(
                            business=biz,
                            priority=q_priority,
                            signals=q_signals,
                            verification_reason=v_reason,
                            verified_socials=audit.get("verified_social_urls", {})
                        )

                        emit_log(f"  [OUTREACH READY LEAD #{len(outreach_ready_leads) + 1}] ({q_priority} Priority | Score: {q_score}/100)")
                        emit_log(f"    Qualification: {q_reason}")
                        emit_log(f"    Outreach Angle: {outreach_text}")

                        lead_id = f"LEAD-{city_slug}-{uuid.uuid4().hex[:6].upper()}"
                        qual_signals_str = ", ".join([k for k, v in q_signals.items() if v and k != "no_website_confirmed"])

                        lead = Lead(
                            lead_id=lead_id,
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            country=biz.detected_country or country,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website="",
                            website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
                            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
                            verification_reason=v_reason,
                            google_maps_url=biz.google_maps_url,
                            instagram_url=biz.instagram_url,
                            facebook_url=biz.facebook_url,
                            tiktok_url=biz.tiktok_url,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=social_status,
                            social_ownership_status=social_ownership,
                            social_profile_status=social_profile_status,
                            social_activity=social_activity,
                            operational_status=operational_status,
                            operational_confidence=operational_confidence,
                            operational_evidence=operational_evidence,
                            evidence_freshness=evidence_freshness,
                            multiple_locations="Yes" if q_signals.get("multiple_locations") else "No",
                            business_activity_signal=f"Reviews: {_format_optional_metric(biz.review_count)}, Rating: {_format_optional_metric(biz.rating)}",
                            qualification_signals=qual_signals_str,
                            lead_score=q_score,
                            priority=q_priority,
                            qualification_state=q_state,
                            qualification_reason=q_reason,
                            red_flags=red_flags_str,
                            outreach_angle=outreach_text,
                            lead_status=LeadStatus.NOT_CONTACTED.value,
                            date_added=datetime.now().strftime("%Y-%m-%d"),
                            discovery_source=getattr(biz, "discovery_source", "OPENSTREETMAP"),
                            city_match=getattr(biz, "city_match", True),
                            city_match_reason=getattr(biz, "city_match_reason", ""),
                            boundary_source=getattr(biz, "boundary_source", ""),
                            boundary_validation_method=getattr(biz, "boundary_validation_method", ""),
                            pipeline_run_id=pipeline_run_id,
                            verified_social_platforms=", ".join(audit.get("verified_social_urls", {}).keys()) if audit.get("verified_social_urls") else "",
                            verified_social_urls=", ".join(audit.get("verified_social_urls", {}).values()) if audit.get("verified_social_urls") else "",
                            outreach_mode=OutreachMode.MANUAL.value,
                            outreach_status=OutreachStatus.NOT_READY.value,
                            outreach_message=outreach_text,
                            processing_state=ProcessingState.QUALIFIED.value,
                            evidence=evidence_obj,
                            score_breakdown=score_breakdown,
                            creator_evidence_status=audit.get("creator_evidence_status", "NOT_FOUND"),
                            creator_evidence_count=audit.get("creator_evidence_count", 0),
                            creator_evidence_confidence=audit.get("creator_evidence_confidence", "UNKNOWN"),
                            creator_latest_date=audit.get("creator_latest_date", ""),
                            creator_evidence_summary=audit.get("creator_evidence_summary", ""),
                            creator_evidence_urls=json.dumps(audit.get("creator_evidence_urls", [])) if audit.get("creator_evidence_urls") else "",
                            creator_discovered_at=audit.get("creator_discovered_at", "")
                        )
                        outreach_ready_leads.append(lead)

                        res_entry = ResearchLogEntry(
                            research_id=f"RES-{uuid.uuid4().hex[:6].upper()}",
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            detected_country=biz.detected_country or country,
                            country_status=biz.country_status,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website="",
                            website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
                            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=social_status,
                            social_ownership_status=social_ownership,
                            social_profile_status=social_profile_status,
                            operational_status=operational_status,
                            operational_confidence=operational_confidence,
                            evidence_freshness=evidence_freshness,
                            qualification_state=q_state,
                            qualification_status="QUALIFIED",
                            disqualification_reason="",
                            red_flags=red_flags_str,
                            source_url=biz.google_maps_url,
                            date_researched=datetime.now().strftime("%Y-%m-%d")
                        )
                        record_candidate_completed(biz, res_entry)

                    elif q_state == QualificationState.MANUAL_REVIEW.value:
                        # MANUAL_REVIEW CANDIDATE (REVIEW_QUEUE)
                        stats["manual_review"] += 1
                        emit_log(f"  [Manual Review] Candidate '{biz.company_name}' -> Sent to REVIEW_QUEUE ({q_reason})")

                        lead_id = f"REV-{city_slug}-{uuid.uuid4().hex[:6].upper()}"
                        qual_signals_str = ", ".join([k for k, v in q_signals.items() if v and k != "no_website_confirmed"])
                        outreach_text = self.outreach.generate_angle(
                            business=biz,
                            priority=Priority.MANUAL_REVIEW.value,
                            signals=q_signals,
                            verification_reason=v_reason,
                            verified_socials=audit.get("verified_social_urls", {})
                        )

                        rev_lead = Lead(
                            lead_id=lead_id,
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            country=biz.detected_country or country,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website="",
                            website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
                            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
                            verification_reason=v_reason,
                            google_maps_url=biz.google_maps_url,
                            instagram_url=biz.instagram_url,
                            facebook_url=biz.facebook_url,
                            tiktok_url=biz.tiktok_url,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=social_status,
                            social_ownership_status=social_ownership,
                            social_profile_status=social_profile_status,
                            social_activity=social_activity,
                            operational_status=operational_status,
                            operational_confidence=operational_confidence,
                            operational_evidence=operational_evidence,
                            evidence_freshness=evidence_freshness,
                            multiple_locations="Yes" if q_signals.get("multiple_locations") else "No",
                            business_activity_signal=f"Reviews: {_format_optional_metric(biz.review_count)}, Rating: {_format_optional_metric(biz.rating)}",
                            qualification_signals=qual_signals_str,
                            lead_score=q_score,
                            priority=Priority.MANUAL_REVIEW.value,
                            qualification_state=q_state,
                            qualification_reason=q_reason,
                            red_flags=red_flags_str,
                            outreach_angle=outreach_text,
                            lead_status=LeadStatus.NOT_CONTACTED.value,
                            date_added=datetime.now().strftime("%Y-%m-%d"),
                            discovery_source=getattr(biz, "discovery_source", "OPENSTREETMAP"),
                            city_match=getattr(biz, "city_match", True),
                            city_match_reason=getattr(biz, "city_match_reason", ""),
                            boundary_source=getattr(biz, "boundary_source", ""),
                            boundary_validation_method=getattr(biz, "boundary_validation_method", ""),
                            pipeline_run_id=pipeline_run_id,
                            verified_social_platforms=", ".join(audit.get("verified_social_urls", {}).keys()) if audit.get("verified_social_urls") else "",
                            verified_social_urls=", ".join(audit.get("verified_social_urls", {}).values()) if audit.get("verified_social_urls") else "",
                            outreach_mode=OutreachMode.MANUAL.value,
                            outreach_status=OutreachStatus.NOT_READY.value,
                            outreach_message=outreach_text,
                            processing_state="MANUAL_REVIEW",
                            evidence=evidence_obj,
                            score_breakdown=score_breakdown,
                            creator_evidence_status=audit.get("creator_evidence_status", "NOT_FOUND"),
                            creator_evidence_count=audit.get("creator_evidence_count", 0),
                            creator_evidence_confidence=audit.get("creator_evidence_confidence", "UNKNOWN"),
                            creator_latest_date=audit.get("creator_latest_date", ""),
                            creator_evidence_summary=audit.get("creator_evidence_summary", ""),
                            creator_evidence_urls=json.dumps(audit.get("creator_evidence_urls", [])) if audit.get("creator_evidence_urls") else "",
                            creator_discovered_at=audit.get("creator_discovered_at", "")
                        )
                        review_queue_leads.append(rev_lead)

                        res_entry = ResearchLogEntry(
                            research_id=f"RES-{uuid.uuid4().hex[:6].upper()}",
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            detected_country=biz.detected_country or country,
                            country_status=biz.country_status,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website="",
                            website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
                            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=social_status,
                            social_ownership_status=social_ownership,
                            social_profile_status=social_profile_status,
                            operational_status=operational_status,
                            operational_confidence=operational_confidence,
                            evidence_freshness=evidence_freshness,
                            qualification_state=q_state,
                            qualification_status="MANUAL_REVIEW",
                            disqualification_reason=q_reason,
                            red_flags=red_flags_str,
                            source_url=biz.google_maps_url,
                            date_researched=datetime.now().strftime("%Y-%m-%d")
                        )
                        record_candidate_completed(biz, res_entry)

                    elif q_state == QualificationState.RESEARCH_ONLY.value:
                        # RESEARCH_ONLY CANDIDATE (RESEARCH_LOG)
                        stats["research_only"] += 1
                        emit_log(f"  [Research Only] Candidate '{biz.company_name}' ({_format_optional_metric(biz.review_count)} reviews, {_format_optional_metric(biz.rating)}★) -> Logged to RESEARCH_LOG ({q_reason})")
                        res_entry = ResearchLogEntry(
                            research_id=f"RES-{uuid.uuid4().hex[:6].upper()}",
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            detected_country=biz.detected_country or country,
                            country_status=biz.country_status,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website="",
                            website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
                            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=social_status,
                            social_ownership_status=social_ownership,
                            social_profile_status=social_profile_status,
                            operational_status=operational_status,
                            operational_confidence=operational_confidence,
                            evidence_freshness=evidence_freshness,
                            qualification_state=q_state,
                            qualification_status="RESEARCH_ONLY",
                            disqualification_reason=q_reason,
                            red_flags=red_flags_str,
                            source_url=biz.google_maps_url,
                            date_researched=datetime.now().strftime("%Y-%m-%d")
                        )
                        record_candidate_completed(biz, res_entry)

                    else:
                        # EXCLUDED CANDIDATE (RESEARCH_LOG)
                        stats["excluded"] += 1
                        emit_log(f"  [Excluded] Candidate '{biz.company_name}' -> Logged to RESEARCH_LOG ({q_reason})")
                        res_entry = ResearchLogEntry(
                            research_id=f"RES-{uuid.uuid4().hex[:6].upper()}",
                            company_name=biz.company_name,
                            industry=biz.category or industry,
                            target_country=country,
                            detected_country=biz.detected_country or country,
                            country_status=biz.country_status,
                            city=biz.city,
                            region=biz.region,
                            postcode=biz.postcode,
                            address=biz.address,
                            phone=biz.phone,
                            website=biz.raw_website,
                            website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
                            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
                            review_count=biz.review_count,
                            rating=biz.rating,
                            social_status=social_status,
                            social_ownership_status=social_ownership,
                            social_profile_status=social_profile_status,
                            operational_status=operational_status,
                            operational_confidence=operational_confidence,
                            evidence_freshness=evidence_freshness,
                            qualification_state=q_state,
                            qualification_status="EXCLUDED",
                            disqualification_reason=q_reason,
                            red_flags=red_flags_str,
                            source_url=biz.google_maps_url,
                            date_researched=datetime.now().strftime("%Y-%m-%d")
                        )
                        record_candidate_completed(biz, res_entry)

                except Exception as cand_err:
                    emit_log(f"  [Error] Processing candidate '{biz.company_name}': {str(cand_err)}")
                    res_entry = ResearchLogEntry(
                        research_id=f"RES-{uuid.uuid4().hex[:6].upper()}",
                        company_name=biz.company_name,
                        industry=biz.category or industry,
                        target_country=country,
                        detected_country=getattr(biz, "detected_country", "") or country,
                        country_status=getattr(biz, "country_status", CountryStatus.COUNTRY_UNCLEAR.value),
                        city=biz.city,
                        region=biz.region,
                        postcode=biz.postcode,
                        address=biz.address,
                        phone=biz.phone,
                        website=biz.raw_website,
                        qualification_state=QualificationState.EXCLUDED.value,
                        qualification_status="FAILED",
                        disqualification_reason=f"ERROR: {str(cand_err)[:60]}",
                        source_url=biz.google_maps_url,
                        date_researched=datetime.now().strftime("%Y-%m-%d")
                    )
                    record_candidate_completed(biz, res_entry)
                    continue

        # STAGE 5: SAVE TO GOOGLE SHEETS 3 TABS (LEADS, REVIEW_QUEUE, & RESEARCH_LOG)
        if self.shadow_mode:
            emit_log(f"\n[Shadow Mode] Safe preflight evaluation active: Bypassing Google Sheets / CRM writes.")
            emit_log(f"  [Summary] {len(outreach_ready_leads)} OUTREACH_READY, {len(review_queue_leads)} MANUAL_REVIEW, {len(all_research_entries)} RESEARCH entries retained in memory.")
        else:
            emit_log(f"\n[Google Sheets] Finalizing live sync to 3 tabs (LEADS, REVIEW_QUEUE, & RESEARCH_LOG)...")
            try:
                sync_sheets_lively(force_all=True)
                emit_log(f"  [LEADS Tab] Total live synced: {stats['saved_to_leads']} OUTREACH_READY leads.")
                emit_log(f"  [REVIEW_QUEUE Tab] Total live synced: {stats['saved_to_review_queue']} MANUAL_REVIEW candidates.")
                emit_log(f"  [RESEARCH_LOG Tab] Total live synced: {stats['saved_to_research_log']} research entries.")
            except Exception as sync_err:
                emit_log(f"  [Error] Google Sheets final sync failed: {sync_err}")

        elapsed = round(time.time() - start_time, 2)
        emit_log(f"\n=======================================================")
        emit_log(f"[PIPELINE RUN COMPLETE] {elapsed}s")
        emit_log(f"PIPELINE RUN ID: {pipeline_run_id}")
        emit_log(f"STATUS: PIPELINE_STATUS = COMPLETE")
        emit_log(f"=======================================================")
        emit_log(f"PIPELINE FUNNEL:")
        emit_log(f"RAW_DISCOVERED:          {stats['raw_discovered']}")
        emit_log(f"COUNTRY_VERIFIED:        {stats['target_country_matches']}")
        emit_log(f"DUPLICATES:              {stats['duplicates']}")
        emit_log(f"WEBSITE_EXISTS:          {stats['website_exists']}")
        emit_log(f"NO_WEBSITE_CONFIRMED:    {stats['no_website_confirmed']}")
        emit_log(f"IDENTITY_VERIFIED:       {stats['valid_candidates_researched']}")
        emit_log(f"ACTIVE_CONFIRMED:        {stats.get('active_confirmed', 0)}")
        emit_log(f"SOCIAL_VERIFIED:         {stats.get('social_verified', 0)}")
        emit_log(f"OUTREACH_READY:          {stats['outreach_ready']}")
        emit_log(f"MANUAL_REVIEW:           {stats['manual_review']}")
        emit_log(f"RESEARCH_ONLY:           {stats['research_only']}")
        emit_log(f"EXCLUDED:                {stats['excluded']}")
        emit_log(f"-------------------------------------------------------")
        emit_log(f"Requested qualified leads:    {requested_qualified_leads}")
        emit_log(f"Actual outreach-ready leads:  {len(outreach_ready_leads)}")
        emit_log(f"Saved to LEADS:               {stats['saved_to_leads']}")
        emit_log(f"Saved to REVIEW_QUEUE:        {stats['saved_to_review_queue']}")
        emit_log(f"Saved to RESEARCH_LOG:        {stats['saved_to_research_log']}")
        diagnostic_items = sorted(
            (key, value)
            for key, value in stats.items()
            if key.startswith("diagnostic__qualification__") and isinstance(value, int)
        )
        emit_log("[QUALIFICATION DIAGNOSTICS] State / reason-bucket counts:")
        if diagnostic_items:
            for key, value in diagnostic_items:
                emit_log(f"  {key.removeprefix('diagnostic__qualification__')}: {value}")
        else:
            emit_log("  No scored candidates produced qualification diagnostics.")

        if len(outreach_ready_leads) < requested_qualified_leads:
            emit_log(f"Notice: Only {len(outreach_ready_leads)} outreach-ready leads were found under the current qualification rules.")
        emit_log(f"=======================================================")

        emit_log(f"\n=======================================================")
        emit_log(f"[QUERY PERFORMANCE] DISCOVERY QUERY METRICS")
        emit_log(f"=======================================================")
        emit_log(f"{'Discovery Query Used':<48} | {'Candidates':<10} | {'Qualified Leads':<15}")
        emit_log("-" * 80)
        for q_name, q_data in query_stats.items():
            emit_log(f"{q_name:<48} | {q_data['candidates']:<10} | {q_data['qualified']:<15}")
        emit_log(f"=======================================================\n")

        return {
            "leads": outreach_ready_leads,
            "review_queue": review_queue_leads,
            "research_entries": all_research_entries,
            "stats": stats,
            "query_stats": query_stats,
            "discovery_transparency": getattr(self.discovery, "transparency_stats", {}),
            "elapsed_seconds": elapsed,
            "processed_ids": list(completed_candidate_ids)
        }
