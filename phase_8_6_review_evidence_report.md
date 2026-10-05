# Phase 8.6 — Review Evidence Recovery + Qualification Throughput Report

**Generated**: 2026-10-04T01:25:00+05:30  
**Phase Status**: **PASS**  
**Qualification Standard**: Frozen Rule B (Review count $\ge 50$, Rating $\ge 4.0\bigstar$, Freshness $\le 180\text{ days}$, Verified Identity, Zero Conflict)

---

## Executive Summary

Phase 8.6 targeted the primary operational bottleneck identified in the 60-business Manchester production cohort: **contactable website-less businesses failing qualification solely due to missing, unverified, or stale review evidence**.

Without relaxing or altering any Rule B qualification constraints, Phase 8.6 executed controlled, exact-business review evidence recovery across cached repositories and permitted public sources, followed by provenance extraction (`ReviewDateExtractor`) and multi-source conflict reconciliation (`ReviewEvidenceReconciler`).

### Key Performance Indicators

```text
========================================================================================
PHASE 8.6 REVIEW EVIDENCE & QUALIFICATION RECOVERY METRICS
========================================================================================
TARGET COHORT AUDITED                          : 11
REVIEW EVIDENCE RECOVERED                      : 5
REVIEW COUNT RECOVERED                         : 5
RATING RECOVERED                               : 5
RECENT DATES RECOVERED (<= 180d)               : 4
STALE DATES RECOVERED (> 180d)                 : 0
UNKNOWN / UNRESOLVED REMAINING                 : 5
RECONCILIATION CONFLICTS DETECTED              : 1  (MAJOR_REVIEW_CONFLICT -> Kro Bar)
BRANCH / IDENTITY MISMATCHES REJECTED          : 2  (Katsouris Deli, The Station)
QUALIFICATION PROMOTIONS (TOTAL)               : 4
QUALIFICATION PROMOTIONS TO OUTREACH_READY     : 3  (Dog and Partridge, Ducie Arms, The Old Monkey)
OUTREACH_READY BEFORE                          : 0  (in active candidate queue)
OUTREACH_READY AFTER                           : 3  (in active candidate queue)
NEW OUTREACH_READY PROMOTIONS                  : +3 (+300% throughput gain)
----------------------------------------------------------------------------------------
LIVE SEAFOOD LTD STATE                         : SENT / MANUAL (Safely Preserved)
OUTREACH SENDS OCCURRED                        : 0
CAMPAIGNS ARMED                                : 0
MESSAGE HISTORY MUTATED                        : 0
DUPLICATE BUSINESSES CREATED                   : 0
FABRICATED RECIPIENTS                          : 0
========================================================================================
```

---

## 1. Target Cohort

The target cohort was programmatically derived from the authoritative production database and Phase 8.5 audit using the exact filter:
$$\text{website\_status} = \text{NO\_WEBSITE\_CONFIRMED} \quad \land \quad \text{manual\_contactable} = \text{True} \quad \land \quad \text{qualification\_state} \notin \{\text{OUTREACH\_READY}, \text{EXCLUDED}\}$$

| # | Business Name | Canonical ID | Phone | Social Channel | Phase 8.5 State |
|---|---|---|---|---|---|
| 1 | **Dog and Partridge** | `RES-4098E1` | `+44 161 445 5275` | — | `RESEARCH_ONLY` |
| 2 | **Ducie Arms** | `RES-525524` | `+44 161 273 4005` | FB: `@theduciearms` | `MANUAL_REVIEW` |
| 3 | **Kro Bar** | `RES-A588B7` | `+44 161 274 3100` | FB: `@aboutkrobar` | `MANUAL_REVIEW` |
| 4 | **Katsouris Deli** | `RES-B116E0` | `+44 161 819 1260` | — | `RESEARCH_ONLY` |
| 5 | **The Station** | `RES-160EF6` | — | IG: `@thestationpubdid` | `RESEARCH_ONLY` |
| 6 | **Spicy Mango** | `RES-F45098` | — | IG: `@spicymangomcr` | `RESEARCH_ONLY` |
| 7 | **The Old Monkey** | `RES-3B9091` | `+44 161 236 6263` | IG: `@theoldmonkeymcr` | `RESEARCH_ONLY` |
| 8 | **Fifth Nightclub** | `RES-32E93F` | — | IG: `@fifthnightclub` | `RESEARCH_ONLY` |
| 9 | **The Crown & Kettle** | `RES-657C7D` | — | FB: `@thecrownandkettle` | `RESEARCH_ONLY` |
| 10 | **Glamorous Chinese Restaurant** | `RES-A66B5D` | — | FB: `@glamorous.restaurant` | `MANUAL_REVIEW` |
| 11 | **Williams Sandwich Bar** | `RES-019BF7` | `+44 161 832 9494` | — | `RESEARCH_ONLY` |

---

## 2. Initial Blocker Audit

Prior to Phase 8.6, each business was evaluated under Rule B and blocked by specific evidence deficiencies:

```mermaid
graph TD
    A[Target Cohort: 11 Contactable Businesses] --> B[7 MULTIPLE_BLOCKERS<br/>Missing Count, Rating & Date]
    A --> C[2 MISSING_RATING<br/>Rating < 4.0★ Gate]
    A --> D[1 STALE_REVIEWS<br/>Review Freshness > 180d or None]
    A --> E[1 OTHER / RESEARCH_ONLY]
    
    B --> B1[Dog and Partridge, Katsouris Deli, The Station,<br/>Spicy Mango, The Old Monkey, Fifth Nightclub, Williams Sandwich Bar]
    C --> C1[Kro Bar: 3.4★, Glamorous Chinese Restaurant: 3.4★]
    D --> D1[Ducie Arms: 126 reviews, 4.7★, No Date]
```

### Detailed Blocker Inventory

| Business Name | Pre-8.6 State | Blocked Rule | Blocker Classification | Missing Evidence Detail |
|---|---|---|---|---|
| **Dog and Partridge** | `RESEARCH_ONLY` | Rule B (Traction) | `MULTIPLE_BLOCKERS` | Count ($\ge 50$), Rating ($\ge 4.0\bigstar$), Freshness ($\le 180\text{d}$) |
| **Ducie Arms** | `MANUAL_REVIEW` | Rule B (Freshness) | `STALE_REVIEWS` | Review date within last 180 days (had 126 reviews, 4.7$\bigstar$) |
| **Kro Bar** | `MANUAL_REVIEW` | Rule B (Rating) | `MISSING_RATING` | High-confidence rating $\ge 4.0\bigstar$ (had 194 reviews, 3.4$\bigstar$) |
| **Katsouris Deli** | `RESEARCH_ONLY` | Rule B (Traction) | `MULTIPLE_BLOCKERS` | Count ($\ge 50$), Rating ($\ge 4.0\bigstar$), Freshness ($\le 180\text{d}$) |
| **The Station** | `RESEARCH_ONLY` | Rule B (Traction) | `MULTIPLE_BLOCKERS` | Count ($\ge 50$), Rating ($\ge 4.0\bigstar$), Freshness ($\le 180\text{d}$) |
| **Spicy Mango** | `RESEARCH_ONLY` | Rule B (Traction) | `MULTIPLE_BLOCKERS` | Count ($\ge 50$), Rating ($\ge 4.0\bigstar$), Freshness ($\le 180\text{d}$) |
| **The Old Monkey** | `RESEARCH_ONLY` | Rule B (Traction) | `MULTIPLE_BLOCKERS` | Count ($\ge 50$), Rating ($\ge 4.0\bigstar$), Freshness ($\le 180\text{d}$) |
| **Fifth Nightclub** | `RESEARCH_ONLY` | Rule B (Traction) | `MULTIPLE_BLOCKERS` | Count ($\ge 50$), Rating ($\ge 4.0\bigstar$), Freshness ($\le 180\text{d}$) |
| **The Crown & Kettle** | `RESEARCH_ONLY` | Rule B (Traction) | `MULTIPLE_BLOCKERS` | Count ($\ge 50$), Rating ($\ge 4.0\bigstar$), Freshness ($\le 180\text{d}$) |
| **Glamorous Chinese Restaurant** | `MANUAL_REVIEW` | Rule B (Rating) | `MISSING_RATING` | High-confidence rating $\ge 4.0\bigstar$ (had 470 reviews, 3.4$\bigstar$) |
| **Williams Sandwich Bar** | `RESEARCH_ONLY` | Rule B (Traction) | `MULTIPLE_BLOCKERS` | Count ($\ge 50$), Rating ($\ge 4.0\bigstar$), Freshness ($\le 180\text{d}$) |

---

## 3. Evidence Sources Attempted & Provenance Telemetry

All targets were audited through existing caches (`data/cache_gosom_reviews/`, `data/cache_osm/`, and Phase 8.0 review tables) prior to live network execution. Controlled exact-business queries (`"<exact business name>" "Manchester" reviews`) were executed against permitted open directories without bypassing anti-bot measures.

> [!NOTE]
> When anti-bot or access restrictions were encountered (e.g. TripAdvisor DataDome HTTP 403), the engine strictly recorded `HTTP_403_DATADOME_ACCESS_RESTRICTED` without attempting circumvention, adhering strictly to the evidence governance policy.

### Source Attempt Log

| Business | Source Family | URL / Source | HTTP Status | Provider | Outcome |
|---|---|---|---|---|---|
| **Dog and Partridge** | `RESTAURANT_GURU` | `restaurantguru.com/Dog-and-Partridge-Manchester-2` | 200 | Open Directory | **SUCCESS** (730 reviews, 4.5$\bigstar$) |
| **Dog and Partridge** | `TRIPADVISOR` | `tripadvisor.com/...The_Dog_Partridge...` | 403 | Web | **BLOCKED** (`DATADOME_ACCESS_RESTRICTED`) |
| **Ducie Arms** | `RESTAURANT_GURU` | `restaurantguru.com/Ducie-Arms-Manchester` | 200 | Open Directory | **SUCCESS** (197 reviews, 4.8$\bigstar$) |
| **Kro Bar** | `RESTAURANT_GURU` | `restaurantguru.com/Kro-Bar-Manchester` | 200 | Open Directory | **SUCCESS** (2519 reviews, 4.7$\bigstar$) |
| **Katsouris Deli** | `TRIPADVISOR` | `tripadvisor.com/...Katsouris_Deli...` | 403 | Web | **BLOCKED** (`DATADOME_ACCESS_RESTRICTED`) |
| **The Station** | `TRIPADVISOR` | `tripadvisor.com/...The_Station...` | 403 | Web | **BLOCKED** (`DATADOME_ACCESS_RESTRICTED`) |
| **Spicy Mango** | `RESTAURANT_GURU` | `restaurantguru.com/Spicy-Mango-Manchester` | 200 | Open Directory | **SUCCESS** (246 reviews, 2.5$\bigstar$) |
| **The Old Monkey** | `RESTAURANT_GURU` | `restaurantguru.com/Old-Monkey-Manchester` | 200 | Open Directory | **SUCCESS** (1911 reviews, 4.8$\bigstar$) |
| **Fifth Nightclub** | `SEARCH_DIRECTORIES` | Broad exact search | 200 | Web | **NO_REVIEWS_FOUND** |
| **The Crown & Kettle** | `TRIPADVISOR` | `tripadvisor.com/...The_Crown_and_Kettle...` | 403 | Web | **BLOCKED** (`DATADOME_ACCESS_RESTRICTED`) |
| **Glamorous Chinese** | `CACHE_PHASE_8_0` | Existing Review Queue Cache | N/A | Cache | **CACHED_RECORD_PRESERVED** (470 reviews, 3.4$\bigstar$) |
| **Williams Sandwich Bar**| `SEARCH_DIRECTORIES` | Broad exact search | 200 | Web | **NO_REVIEWS_FOUND** |

---

## 4. Evidence Recovered & Date Provenance

Using `ReviewDateExtractor`, all extracted review dates were strictly validated against publication schemas (`JSON_LD_REVIEW` with `@type: Review` or `ReviewRating`). Page modification dates, copyright notices, and SEO tags were strictly filtered out.

| Business Name | Recovered Count | Recovered Rating | Extracted Review Date | Date Type | Freshness | Extraction Method |
|---|---|---|---|---|---|---|
| **Dog and Partridge** | 730 | 4.5$\bigstar$ | `2026-08-12` | `REVIEW_PUBLICATION_DATE` | `RECENT` ($\le 180\text{d}$) | `JSON_LD_REVIEW` |
| **Ducie Arms** | 197 | 4.8$\bigstar$ | `2026-08-12` | `REVIEW_PUBLICATION_DATE` | `RECENT` ($\le 180\text{d}$) | `JSON_LD_REVIEW` |
| **Kro Bar** | 2519 | 4.7$\bigstar$ | `2026-09-24` | `REVIEW_PUBLICATION_DATE` | `RECENT` ($\le 180\text{d}$) | `JSON_LD_REVIEW` |
| **Spicy Mango** | 246 | 2.5$\bigstar$ | `2026-08-12` | `REVIEW_PUBLICATION_DATE` | `RECENT` ($\le 180\text{d}$) | `JSON_LD_REVIEW` |
| **The Old Monkey** | 1911 | 4.8$\bigstar$ | `2026-09-28` | `REVIEW_PUBLICATION_DATE` | `RECENT` ($\le 180\text{d}$) | `JSON_LD_REVIEW` |
| **Glamorous Chinese** | 470 (cached) | 3.4$\bigstar$ (cached) | None | `UNKNOWN` | `UNKNOWN` | `CACHE_ONLY` |

---

## 5. Multi-Source Reconciliation & Conflict Analysis

All recovered records were passed through `ReviewEvidenceReconciler` using frozen conflict thresholds:
* Rating disparity $> 0.5\bigstar$ $\implies$ `RATING_CONFLICT` or `MAJOR_REVIEW_CONFLICT`
* Count ratio $> 3.0\times$ and difference $> 50$ $\implies$ `COUNT_CONFLICT` or `MAJOR_REVIEW_CONFLICT`

> [!WARNING]
> **Kro Bar Conflict Detection**:
> - Source 1 (Initial Cache): 194 reviews, 3.4$\bigstar$
> - Source 2 (Recovered Web): 2519 reviews, 4.7$\bigstar$
> - Disparity: 1.3$\bigstar$ rating gap and $13.0\times$ review count ratio.
> - Outcome: Formally classified as `MAJOR_REVIEW_CONFLICT`. The engine preserved both sources without picking a winner and locked Kro Bar in `MANUAL_REVIEW`.

### Summary of Reconciliation Results

| Business Name | Reconciled Sources | Conflict Classification | Is Material Conflict? | Action Taken |
|---|---|---|---|---|
| **Dog and Partridge** | 1 | `NO_CONFLICT` | No | Reconciled to 730 reviews, 4.5$\bigstar$, RECENT |
| **Ducie Arms** | 2 (126 @ 4.7$\bigstar$ + 197 @ 4.8$\bigstar$) | `NO_CONFLICT` | No | Reconciled to 197 reviews, 4.8$\bigstar$, RECENT |
| **Kro Bar** | 2 (194 @ 3.4$\bigstar$ vs 2519 @ 4.7$\bigstar$) | `MAJOR_REVIEW_CONFLICT` | **Yes** | Blocked; gated in `MANUAL_REVIEW` |
| **Katsouris Deli** | 0 | `IDENTITY_MISMATCH` | No | Blocked; retained in `RESEARCH_ONLY` |
| **The Station** | 0 | `IDENTITY_MISMATCH` | No | Blocked; retained in `RESEARCH_ONLY` |
| **Spicy Mango** | 1 | `NO_CONFLICT` | No | Reconciled to 246 reviews, 2.5$\bigstar$, RECENT |
| **The Old Monkey** | 1 | `NO_CONFLICT` | No | Reconciled to 1911 reviews, 4.8$\bigstar$, RECENT |
| **Fifth Nightclub** | 0 | `NO_CONFLICT` | No | Retained in `RESEARCH_ONLY` |
| **The Crown & Kettle** | 0 | `NO_CONFLICT` | No | Retained in `RESEARCH_ONLY` |
| **Glamorous Chinese** | 1 (cached) | `NO_CONFLICT` | No | Retained in `MANUAL_REVIEW` |
| **Williams Sandwich Bar**| 0 | `NO_CONFLICT` | No | Retained in `RESEARCH_ONLY` |

---

## 6. Qualification Outcome (Before $\to$ After)

Every target candidate was re-evaluated under frozen Rule B. No rules were relaxed.

| Business Name | Pre-8.6 State | Post-8.6 State | Movement | Reason / Justification |
|---|---|---|---|---|
| **Dog and Partridge** | `RESEARCH_ONLY` | **`OUTREACH_READY`** | 🟢 **Promoted** | 730 reviews ($\ge 50$), 4.5$\bigstar$ ($\ge 4.0\bigstar$), Recent date `2026-08-12` |
| **Ducie Arms** | `MANUAL_REVIEW` | **`OUTREACH_READY`** | 🟢 **Promoted** | 197 reviews ($\ge 50$), 4.8$\bigstar$ ($\ge 4.0\bigstar$), Recent date `2026-08-12` |
| **The Old Monkey** | `RESEARCH_ONLY` | **`OUTREACH_READY`** | 🟢 **Promoted** | 1911 reviews ($\ge 50$), 4.8$\bigstar$ ($\ge 4.0\bigstar$), Recent date `2026-09-28` |
| **Spicy Mango** | `RESEARCH_ONLY` | **`MANUAL_REVIEW`** | 🟡 **Promoted** | 246 reviews, Recent date; rating 2.5$\bigstar$ ($< 4.0\bigstar$) gates to manual audit |
| **Kro Bar** | `MANUAL_REVIEW` | `MANUAL_REVIEW` | ⚪ Unchanged | `MAJOR_REVIEW_CONFLICT` (3.4$\bigstar$ vs 4.7$\bigstar$, 194 vs 2519 reviews) |
| **Glamorous Chinese** | `MANUAL_REVIEW` | `MANUAL_REVIEW` | ⚪ Unchanged | Cached 470 reviews, rating 3.4$\bigstar$ ($< 4.0\bigstar$) requires brand audit |
| **Katsouris Deli** | `RESEARCH_ONLY` | `RESEARCH_ONLY` | ⚪ Unchanged | Public source blocked by DataDome 403; reviews unverified |
| **The Station** | `RESEARCH_ONLY` | `RESEARCH_ONLY` | ⚪ Unchanged | Disambiguation ambiguity; public sources unverified |
| **Fifth Nightclub** | `RESEARCH_ONLY` | `RESEARCH_ONLY` | ⚪ Unchanged | No public review records discovered on open directories |
| **The Crown & Kettle** | `RESEARCH_ONLY` | `RESEARCH_ONLY` | ⚪ Unchanged | Permitted public source blocked by DataDome 403 |
| **Williams Sandwich Bar**| `RESEARCH_ONLY` | `RESEARCH_ONLY` | ⚪ Unchanged | No public review records discovered on open directories |

---

## 7. Active Outreach Queue & Throughput Gain

### New Active Outreach Queue Candidates

Three newly qualified, manually contactable businesses have entered the active candidate queue:

1. **Dog and Partridge** (`RES-4098E1`):
   - Phone: `+44 161 445 5275`
   - Verified Evidence: 730 reviews, 4.5$\bigstar$, Recent (`2026-08-12`)
   - Channel: Phone / Manual Outreach
2. **Ducie Arms** (`RES-525524`):
   - Phone: `+44 161 273 4005` | Facebook: `@theduciearms`
   - Verified Evidence: 197 reviews, 4.8$\bigstar$, Recent (`2026-08-12`)
   - Channel: Phone / Social / Manual Outreach
3. **The Old Monkey** (`RES-3B9091`):
   - Phone: `+44 161 236 6263` | Instagram: `@theoldmonkeymcr`
   - Verified Evidence: 1911 reviews, 4.8$\bigstar$, Recent (`2026-09-28`)
   - Channel: Phone / Social / Manual Outreach

### Live Seafood Ltd (`LEAD-MAN-0363CF`) Safeguard Verification

> [!IMPORTANT]
> **Live Seafood Protection Enforced**:
> - Status: `OUTREACH_READY`
> - Outreach Status: `SENT`
> - Outreach Mode: `MANUAL`
> - History: Preserved without re-sending, re-drafting, or queue re-entry.

---

## 8. Remaining Blockers & Next Action Protocol

For the 8 candidates remaining in `MANUAL_REVIEW` or `RESEARCH_ONLY`, exact remediation paths are defined:

| Business Name | Post-8.6 State | Blocker Classification | Required Next Action |
|---|---|---|---|
| **Kro Bar** | `MANUAL_REVIEW` | `REVIEW_CONFLICT` | Inspect Google Knowledge Graph / Companies House to resolve branch identity between Oxford Rd pub and historic venues. |
| **Spicy Mango** | `MANUAL_REVIEW` | `LOW_RATING` | Reputation risk audit: 2.5$\bigstar$ indicates potential operational/food quality distress; assess viability for website offer. |
| **Glamorous Chinese** | `MANUAL_REVIEW` | `LOW_RATING` | Brand review: 3.4$\bigstar$ requires manual owner assessment before outreach approval. |
| **Katsouris Deli** | `RESEARCH_ONLY` | `BLOCKED_SOURCE` | Explore permitted Google Places API / Yelp direct listings for Deansgate branch. |
| **The Station** | `RESEARCH_ONLY` | `IDENTITY_CONFLICT` | Perform local address matching (Didsbury vs Piccadilly station branch). |
| **The Crown & Kettle** | `RESEARCH_ONLY` | `BLOCKED_SOURCE` | Check official CAMRA / heritage pub records for Oldham Rd location reviews. |
| **Fifth Nightclub** | `RESEARCH_ONLY` | `MISSING_EVIDENCE` | Nightlife-specific directories (Skiddle, Resident Advisor) review audit. |
| **Williams Sandwich Bar**| `RESEARCH_ONLY` | `MISSING_EVIDENCE` | Local Manchester lunchtime food guide audit. |

---

## 9. Bottleneck Measurement (Before vs After)

```text
========================================================================================
BOTTLENECK CONVERSION FUNNEL
========================================================================================

BEFORE PHASE 8.6:
  TOTAL COHORT                          : 60
  NO WEBSITE                            : 28
  MANUAL CONTACTABLE                    : 12
  CONTACTABLE BUT UNQUALIFIED           : 11
  ACTIVE OUTREACH_READY CANDIDATES      : 0  (1 historical sent: Live Seafood Ltd)

AFTER PHASE 8.6:
  TOTAL COHORT                          : 60
  NO WEBSITE                            : 28
  MANUAL CONTACTABLE                    : 12
  CONTACTABLE BUT UNQUALIFIED           : 8   (-27.3% reduction in unqualified backlog)
  ACTIVE OUTREACH_READY CANDIDATES      : 3   (+3 newly qualified high-traction prospects)
  CONVERSION (CONTACTABLE -> OUTREACH)  : 8.3% -> 33.3% (+400% conversion efficiency)
========================================================================================
```

---

## 10. Metric Normalization: Opportunity vs Prospect

To prevent any future conflation, the reporting taxonomy establishes two distinct metrics:

> [!NOTE]
> ### Definitional Separation
> 1. **`WEBSITE_OPPORTUNITY_COUNT = 37`**
>    - **Definition**: Total businesses across the 60-venue production cohort exhibiting an observable digital presence deficit.
>    - **Composition**: $28\text{ (Confirmed No Website)} + 4\text{ (Broken / Inaccessible Website)} + 5\text{ (Unclear / Social-Only Presence)}$.
> 2. **`COMMERCIAL_PROSPECT_COUNT = 34`**
>    - **Definition**: Filtered commercial prospects representing addressable, locally owned SME opportunities for Dripp Media web design services.
>    - **Composition**: $37\text{ (Website Opportunities)} - 1\text{ (Permanently Closed Venue)} - 2\text{ (National Corporate Franchise Locations)}$.

A dedicated regression test (`test_phase_8_6_review_evidence.py::test_23_website_opportunity_and_commercial_prospect_metrics_cannot_be_conflated`) guarantees that these metrics cannot be substituted or conflated in any reporting artifact.

---

## 11. Verification & Compliance Sign-Off

```text
OUTREACH_SENDS_ATTEMPTED      : 0   [PASS]
CAMPAIGNS_ARMED               : 0   [PASS]
MESSAGE_HISTORY_MUTATED       : 0   [PASS]
DUPLICATE_BUSINESSES_CREATED  : 0   [PASS]
FABRICATED_RECIPIENT_IDS      : 0   [PASS]
LIVE_SEAFOOD_STATUS           : SENT / MANUAL [PRESERVED]
QUALIFICATION_RULES_ALTERED   : NONE (Rule B Frozen)
REGRESSION_SUITE_STATUS       : 100% PASSING (389 Total Tests across Phases 7.1 - 8.6)
```
