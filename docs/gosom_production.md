# Production Gosom Review Freshness Fallback Architecture & Runbook

**Document Version:** 1.0.0  
**Phase:** 7.15 (Production Activation & Hardening)  
**Effective Date:** 2026-10-03  
**Status:** PRODUCTION_ENABLED_UNDER_LIMIT  
**Feature Flag:** `GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED=true`  

---

## 1. Purpose

The Gosom Review Freshness Fallback provides deterministic, privacy-preserving, zero-marginal-cost customer review extraction for UK hospitality candidates discovered via OpenStreetMap (OSM) that lack active customer websites. Its sole functional objective is to recover customer review timestamps and ratings to determine review freshness (`RECENT` $\le 90$ days vs `STALE` $> 90$ days) for lead qualification under Rule B, without making paid API calls to Google Places or Apify.

---

## 2. Why Gosom Exists

Commercial place APIs (Google Places API, Apify actors) incur marginal per-query costs ($0.032 - $0.050/call), introduce vendor lock-in, and risk unexpected billing surges during bulk discovery. Conversely, standard SERP search engines (SearXNG, DuckDuckGo) suffer from inconsistent HTML structure and frequent review date omission. 

Gosom is a standalone, local headless Go binary (`scratch/google_maps_scraper`, pinned version `v1.18.1-0.20260920064515-549e4b5e61c7-549e4b5`) that queries Google Maps search directly and parses embedded JSON review structures with millisecond precision, zero API billing, and zero external dependency on commercial aggregators.

---

## 3. PATH A: Complete Address Routing

When an OSM candidate has complete source-backed street address and postcode data:
- **Condition:** Candidate has `street` AND `postcode` (or complete address string with valid UK postal pattern).
- **Query Construction:** `"<exact business name>" "<street>" "<postcode>" "<city>"`
- **Routing Engine:** Existing address-based Gosom place enricher (`GosomPlaceEnricher`).
- **Matching Principle:** Direct address string corroboration plus name similarity.

---

## 4. PATH B: Coordinate-First Routing

When an OSM candidate has partial or missing street/postcode data but possesses valid geographic coordinates:
- **Condition:** Candidate has valid latitude $[-90, 90]$ and longitude $[-180, 180]$, but lacks complete street address or postcode.
- **Query Construction:** Strictly `"<exact business name>" "<city>"` (e.g., `"Rajdan" "Manchester"`).  
  *Invariant:* Never inject inferred street names, postcodes, house numbers, or reverse-geocoded guesses into the query.
- **Routing Engine:** Frozen `CoordinateFirstMatcher`.
- **Matching Principle:** Dual-evidence coordinate distance and normalized name similarity.

No other fallback path is authorized.

---

## 5. Coordinate Thresholds

Geographic distance between candidate coordinates and scraped Google Maps place coordinates is calculated using the high-precision Haversine formula and classified into frozen, non-overlapping tiers:

| Distance Tier | Threshold | Operational Meaning | Classification |
|---|---|---|---|
| **EXACT** | $\text{distance} \le 50.0\text{m}$ | Storefront / building entrance precision | `EXACT_COORDINATE_MATCH` |
| **STRONG** | $50.0\text{m} < \text{distance} \le 180.0\text{m}$ | Arcade, shopping center, terminal concourse | `STRONG_COORDINATE_MATCH` |
| **MISMATCH** | $\text{distance} > 180.0\text{m}$ | Different branch, divergent location | `COORDINATE_MISMATCH` |
| **NO EVIDENCE** | Coordinates missing on either side | Insufficient spatial data | `NO_COORDINATE_EVIDENCE` |

---

## 6. Identity Thresholds

Business name similarity is calculated using normalized token similarity via `BusinessIdentityMatcher`:

| Identity Tier | Similarity Score | Operational Meaning |
|---|---|---|
| **EXACT** | $\text{score} \ge 0.95$ (or identical normalized tokens) | Direct name match |
| **STRONG** | $0.80 \le \text{score} < 0.95$ | Strong brand match with minor suffixes (e.g., "Restaurant", "Takeaway") |
| **WEAK** | $0.60 \le \text{score} < 0.80$ | Ambiguous or partial token overlap |
| **MISMATCH** | $\text{score} < 0.60$ | Unrelated business entity |

---

## 7. Ambiguity Handling & Branch Isolation

To prevent review contamination across multiple branches of the same chain or similarly named venues:
1. **Deterministic Dual-Evidence:** A `SAFE_MATCH` requires BOTH `Identity >= STRONG` AND `Distance <= 180m`.
2. **Branch Mismatch Protection:** If a candidate matches on identity but its distance exceeds 180m, it is decisively classified as `BRANCH_MISMATCH`. **Zero evidence is attached.**
3. **Ambiguity Gate:** If multiple same-name places are returned within safe coordinate distance ($\le 180$m), the match is classified as `AMBIGUOUS_MATCH`. **Zero evidence is attached.**
4. **No "Closest Wins":** The system never selects the nearest result if spatial or identity criteria are not strictly met.

---

## 8. Source Provenance

All review evidence items attached via Gosom fallback enforce immutable provenance fields:
- `source_family`: `SourceFamily.GOOGLE` (`"google"`)
- `source_provider`: `"GOSOM_LOCAL"`
- `extraction_method`: `"GOSOM_GOOGLE_EMBEDDED_REVIEW_DATA"`
- Diagnostic fields:
  - `query`
  - `candidate_id` / `candidate_identifier`
  - `source_coordinates` (`latitude`, `longitude`)
  - `matched_coordinates` (`latitude`, `longitude`)
  - `distance_meters`
  - `identity_score`
  - `branch_classification`
  - `review_count`
  - `rating`
  - `latest_review_date`
  - `review_freshness`
  - `cache_hit` (`true` / `false`)
  - `timestamp` (UTC ISO-8601)

No placeholder or fabricated metadata is ever recorded.

---

## 9. Qualification Invariants (Rule B Protection)

A critical architectural invariant is that Google review evidence recovered via coordinate-first fallback establishes **only listing identity and review dates**. It does **NOT** independently establish:
- Current active operation
- Full qualification
- Outreach readiness
- Independent operational corroboration

### Rule B Enforcement
Under the qualification scoring engine (`LeadScoringProvider`):
- A lead qualifies as `OUTREACH_READY` only when fresh review evidence is corroborated by an **independent operational signal** (such as an authentic verified business telephone number from OSM or an active, verified social profile).
- If a lead recovers `RECENT` Google reviews but has **no** verified phone or social channel, it scores below the qualification threshold (< 70) and remains in `MANUAL_REVIEW`. Google review evidence alone can never bypass Rule B.

---

## 10. Conservative Production Caps

The fallback operates under hard, defensive quotas enforced at runtime by `LimitedProductionGosomSafetyWrapper`:

| Parameter | Environment Variable | Hard Production Cap |
|---|---|---|
| Per-Run External Calls | `MAX_GOSOM_FALLBACK_CALLS_PER_RUN` | **5 calls** |
| Daily External Calls | `MAX_GOSOM_FALLBACK_CALLS_PER_DAY` | **10 calls** |
| Per-Run Cohort Bound | `MAX_PRODUCTION_CANARY_CANDIDATES` | **5 candidates** |
| Request Timeout | `GOSOM_TIMEOUT_SECONDS` | **60.0 seconds** |

*Note:* Cache hits do **not** consume per-run or daily external request quotas.

---

## 11. Cache Behavior & Idempotency

Deterministic caching prevents redundant external scraper executions:
1. **Cache Key Calculation:**  
   `sha256(clean(name) | clean(city) | clean(query) | scraper_version)[:20]`
   - Distinct queries produce distinct cache keys, guaranteeing that one branch's cached evidence cannot satisfy a different branch.
2. **Persistent Storage:** Stored as formatted JSON in `data/cache_gosom_reviews/gosom_<hash>.json`.
3. **Cache Invariant:** Provider failures and empty error responses are never cached as valid evidence. Only verified `SUCCESS` extractions are stored.
4. **Idempotency:** Re-enriching an identical candidate is resolved in $< 0.001$s with 0 network calls.

---

## 12. Runtime Kill Switch

The system supports immediate, zero-downtime shutdown without requiring an application restart:
- **Environment Variable:** `GOSOM_FALLBACK_KILL_SWITCH=true`
- **Feature Flag:** `GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED=false`
- **Dynamic Check:** The application evaluates the environment flag dynamically before every individual candidate fallback call.
- **Behavior:** When active, `LimitedProductionGosomSafetyWrapper` immediately returns `None` with telemetry `KILL_SWITCH_ACTIVE`. Zero scraper processes are spawned.

---

## 13. Fail-Closed Error Behavior

Under any abnormal condition:
- Scraper subprocess error or non-zero exit code
- Process timeout ($> 60.0$s)
- Scraper binary missing or corrupted
- Malformed JSON output
- Missing coordinates or place fields
- Identity uncertainty (< 0.80)
- Branch mismatch (> 180m)
- Multi-branch ambiguity

The fallback layer strictly returns `None` and records `NO_EVIDENCE_ATTACHED`. The candidate's pre-existing review freshness (`UNKNOWN`) and qualification state are preserved unchanged. Technical failure is never converted into business exclusion.

---

## 14. Monitoring & Telemetry

Every fallback run produces structured, observable metrics via `safety_wrapper.get_observability_metrics()`.

### Metric Definitions
- `total_fallback_attempts`: Total candidate evaluations requested in the run.
- `eligible_attempts`: Candidates meeting all 9 pre-screening eligibility gates.
- `external_calls`: Total genuine external network scraper requests executed.
- `cache_hits`: Invocations resolved via local cache.
- `final_match_classifications` *(Strictly Mutually Exclusive per Candidate)*:
  - `SAFE_MATCH`: Exactly one safe place matched within $\le 180$m with strong identity; evidence attached.
  - `BRANCH_MISMATCH`: Strong identity, but all places exceed 180m; 0 evidence attached.
  - `IDENTITY_MISMATCH`: Places found, but name similarity $< 0.60$; 0 evidence attached.
  - `AMBIGUOUS_MATCH`: Multiple competing places within $\le 180$m; 0 evidence attached.
  - `SEARCH_FAILURE`: Empty places returned or recall failure; 0 evidence attached.
- `scraper_failures`: Subprocess execution or binary errors.
- `recent_recovered`: Evidence items classified as `RECENT` ($\le 90$ days).
- `stale_recovered`: Evidence items classified as `STALE` ($> 90$ days).
- `unknown_remaining`: Candidates remaining in `UNKNOWN` freshness.
- `average_latency_seconds`: Mean runtime of external scraper calls.
- `maximum_latency_seconds`: Peak runtime of external scraper calls.
- `daily_usage`: External calls used today (tracked in `daily_usage.json`).
- `per_run_usage`: External calls used in current execution.

---

## 15. Relationship to Outreach

**Gosom fallback is strictly isolated from outreach execution.**
- Gosom feeds review evidence into the research and qualification pipeline only.
- No Gosom code path may:
  - Send Instagram DMs
  - Send Facebook/Messenger messages
  - Send marketing or transactional emails
  - Arm, transition, or execute campaigns
  - Mutate `outreach_status` or `outreach_state`
- `qualification_state` is authoritative for research qualification; `outreach_status` is authoritative for outreach dispatch.

---

## 16. Rollback Procedure

If any anomaly occurs in production:
1. **Instant Deactivation:**
   ```bash
   export GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED=false
   export GOSOM_FALLBACK_KILL_SWITCH=true
   ```
2. **Criteria for Immediate Rollback:**
   - Any false-positive business match attaching wrong reviews.
   - Any evidence attached across different branches.
   - Any unauthorized mutation to `data/cache_sheets_leads.json`, `data/campaigns.json`, or `data/message_history.json`.
   - Any outreach trigger or campaign armed during enrichment.
   - Any violation of the 5-run / 10-day external call cap.
   - Any failure of the kill switch to halt executions immediately.
3. **Classification:** Mark deployment as `ROLLBACK_REQUIRED` and alert engineering.

---

## Known Limitations

1. **Conservative Fallback, Not Universal Enrichment:** Gosom is designed as a conservative freshness fallback for PARTIAL candidates, not a bulk lead harvester.
2. **Search Recall Limitations:** In niche or crowded urban areas, Google Maps search may return empty results for partial name queries, leaving freshness `UNKNOWN`.
3. **Execution Latency:** Browser context initialization takes 15s to 50s per search; throughput must remain bounded by low cohort sizes ($\le 5$).
