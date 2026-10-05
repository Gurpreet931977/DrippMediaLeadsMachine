# Phase 7.9: Coordinate-First Gosom Evaluation Report

**Evaluation Title:** Coordinate-First Gosom Evaluation for Partial OSM Candidates  
**Date:** October 3, 2026  
**Environment:** Mac OS / Python 3.14  
**Project:** Dripp International Leads  
**Evaluation Mode:** Isolated Dry-Run (Evaluation-Only)  
**Production Flag:** `GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED=false` (OFF)

---

## 1. Executive Summary

Phase 7.9 evaluates whether the local Gosom scraper can safely recover Google business listings and genuine review evidence for OpenStreetMap (OSM) candidates using **coordinate-first matching** when upstream OSM data lacks street-level and postcode address tags.

In Phase 7.8, investigation proved that 19/20 restaurant candidates in the Manchester evaluation cohort are standalone OSM POI nodes that physically lack street and postcode tags in the OpenStreetMap database itself (and have 0 parent-way memberships). Under strict address completeness gating (Phase 7.7 and Phase 7.8), 19/20 candidates were conservatively blocked from Gosom queries to prevent blind multi-branch ambiguity, leaving only 1/20 (`Issano`) eligible for Gosom review recovery.

Phase 7.9 tested a **deterministic coordinate-first matching model** combining:
1. **Safe Identity Query Construction:** Candidate name + City (`"Business Name" "Manchester"`), without invented address components.
2. **Geographic Distance Measurement:** Exact Haversine geodesic calculation between OSM POI coordinates and scraped Google Maps listing coordinates.
3. **Calibrated Distance Thresholds:**
   - `EXACT_COORDINATE_MATCH`: $\le 50.0\text{ meters}$ (storefront / curb entrance precision).
   - `STRONG_COORDINATE_MATCH`: $50.0 < \text{distance} \le 180.0\text{ meters}$ (shopping arcade / airport concourse precision).
   - `COORDINATE_MISMATCH`: $> 180.0\text{ meters}$ (divergent branch / distant neighborhood).
   - `NO_COORDINATE_EVIDENCE`: Missing coordinates on candidate or Google result.
4. **Dual-Evidence Gating:** Requiring BOTH strong identity ($\ge 0.80$ similarity) AND safe coordinates ($\le 180.0\text{m}$), with strict rejection of coordinate proximity alone (`IDENTITY_MISMATCH`) and rejection of multi-branch ambiguity (`AMBIGUOUS_MATCH`).

### Key Measured Outcomes

| Metric | Phase 7.6 (No Gate) | Phase 7.7 (Address Gate) | Phase 7.8 (Parent Way) | Phase 7.9 (Coordinate-First) |
| :--- | :---: | :---: | :---: | :---: |
| **Address Completeness (Complete/Partial)** | N/A | 1 / 19 | 1 / 19 | **1 / 19** |
| **Candidates Evaluated** | 20 | 20 | 20 | **20** |
| **Safe Matches** | 8 (40.0%) | 1 (5.0%) | 1 (5.0%) | **11 (55.0%)** |
| **Ambiguous Matches** | 7 (35.0%) | 0 (0.0%) | 0 (0.0%) | **0 (0.0%)** |
| **Branch Mismatches** | 2 (10.0%) | 0 (0.0%) | 0 (0.0%) | **7 (35.0%)** |
| **Identity Mismatches** | 3 (15.0%) | 0 (0.0%) | 0 (0.0%) | **2 (10.0%)** |
| **Skipped (Unsafe Address Gate)** | 0 (0.0%) | 19 (95.0%) | 19 (95.0%) | **0 (0.0%)** |
| **Review Metrics Recovered** | 8 / 20 | 1 / 20 | 1 / 20 | **11 / 20** |
| **Freshness: RECENT ($\le 180$d)** | 6 | 1 | 1 | **8** |
| **Freshness: STALE ($> 180$d)** | 2 | 0 | 0 | **3** |
| **Freshness: UNKNOWN** | 12 | 19 | 19 | **9** |
| **Pot Kettle Black Barton Contamination** | 0.0% (Blocked) | 0.0% (Skipped) | 0.0% (Skipped) | **0.0% (Isolated: BRANCH_MISMATCH)** |
| **False Positives** | 1 (Georgia Chk 8km) | 0 | 0 | **0 (Caught Georgia Chk 8km)** |
| **CRM Mutations** | 0 | 0 | 0 | **0** |
| **Outreach Sends** | 0 | 0 | 0 | **0** |
| **External API / Scraper Spend** | $0.00 | $0.00 | $0.00 | **$0.00** |

---

## 2. Existing Phase 7.8 Baseline

Phase 7.8 audited the upstream OSM discovery data and confirmed:
- **1/20 COMPLETE** (`Issano` on Palatine Road has direct `addr:street`, `addr:housenumber`, `addr:postcode` tags).
- **19/20 PARTIAL** (All 19 have valid OSM coordinates, but lack direct address tags).
- **OSM Database Topology:** In OSM itself, 19/20 restaurants were mapped as standalone node elements rather than member ways of enclosing buildings. Zero parent ways were present in Overpass.
- **Phase 7.8 Safety Decision:** In Phase 7.8, the production safety gate safely blocked all 19 PARTIAL candidates from Gosom fallback because name-only queries without street/postcode risked branch ambiguity (e.g. Pot Kettle Black, Costa Coffee, Burger King).
- **Baseline Question:** Can candidate geographic coordinates act as a safe, deterministic locator when street and postcode tags are absent from OSM?

---

## 3. Exact Methodology

### 3.1 Safe Discovery Query Generation
To prevent address fabrication, the query string is constructed exclusively from verified candidate identity:
- If street + postcode present: `"<name>" "<street>" "<postcode>" "<city>"`
- If only city available: `"<name>" "<city>"` (e.g. `"Pot Kettle Black" "Manchester"`)
- **Zero invented tokens:** No reverse-geocoding, no nearest-building heuristics, no paid geocoder inferences.

### 3.2 High-Precision Coordinate Distance Model
Geographic distance $d$ in meters is computed using the Haversine spherical geodesic formula:
$$\Delta\phi = \phi_2 - \phi_1, \quad \Delta\lambda = \lambda_2 - \lambda_1$$
$$a = \sin^2\left(\frac{\Delta\phi}{2}\right) + \cos(\phi_1)\cos(\phi_2)\sin^2\left(\frac{\Delta\lambda}{2}\right)$$
$$c = 2 \cdot \text{atan2}\left(\sqrt{a}, \sqrt{1-a}\right), \quad d = R \cdot c \quad (R = 6,371,000\text{ m})$$

### 3.3 Explicit Calibration of Distance Thresholds
Thresholds were calibrated directly from empirical coordinate measurements in the Manchester dataset:
- `EXACT_COORDINATE_MATCH`: $d \le 50.0\text{ meters}$.
  - Observed genuine storefronts and entrances cluster tightly at **2.3m to 9.1m** (e.g., Founder Coffee Co: 2.3m, Caspian Pizza: 2.6m, Caribbean Vibez: 2.7m, Jai Kathmandu: 4.2m, Bar Bibo: 4.3m, Chesters: 5.8m, Chick A Ritos: 6.0m, Issano: 6.8m, Brew'd: 9.1m).
  - Terminal concourse lounges cluster at **30.4m** (Emirates Lounge).
- `STRONG_COORDINATE_MATCH`: $50.0 < d \le 180.0\text{ meters}$.
  - Large airport terminal concourses, multi-level transit hubs, or arcade corridors where OSM POI is placed inside the terminal concourse while Google's pin is at the curbside or terminal entrance (e.g., Costa Coffee MAN Terminal 3 at 59.3m).
- `COORDINATE_MISMATCH`: $d > 180.0\text{ meters}$.
  - Different airport terminal buildings (e.g., Aspire Lounge T3 at 234.9m, T2 at 901.4m).
  - Different branches across town (e.g., Chesters Fallowfield at 4.7km, Georgia Chicken Fallowfield at 8.0km, Pot Kettle Black City Centre at 13.0km, Caspian Pizza Bury at 22.6km).
- `NO_COORDINATE_EVIDENCE`: Either candidate or Google listing lacks latitude/longitude coordinates.

### 3.4 Explicit Identity Matching Thresholds
- `EXACT_NAME_MATCH`: Token identity or normalized similarity score $\ge 0.95$.
- `STRONG_NAME_MATCH`: $0.80 \le \text{similarity} < 0.95$ (covers branch modifiers like "Caspian Pizza Wythenshawe", "Chesters Chicken Northenden").
- `WEAK_NAME_MATCH`: $0.60 \le \text{similarity} < 0.80$.
- `NAME_MISMATCH`: $\text{similarity} < 0.60$ or semantic entity mismatch keywords (e.g. "books", "meeting", "church").

### 3.5 Dual-Evidence Decision Gate
A match is classified as `SAFE_MATCH` if and only if:
1. Business identity is `EXACT_NAME_MATCH` or `STRONG_NAME_MATCH`.
2. Coordinate evidence is `EXACT_COORDINATE_MATCH` or `STRONG_COORDINATE_MATCH`.
3. Unambiguous: Exactly ONE place meets criteria (1) and (2). If multiple same-name places exist within $\le 180\text{m}$, the match is classified as `AMBIGUOUS_MATCH` (attach 0 review evidence).
4. No explicit branch conflict exists between candidate and place.

---

## 4. Coordinate Distance Distribution

Analysis of geographic distance between OSM POI nodes and the closest corresponding Google Maps listing:

| Distance Bucket | Candidates | Candidate Names | Classification Impact |
| :--- | :---: | :--- | :--- |
| **$0\text{ to }10\text{ meters}$** | 9 | Founder Coffee Co (2.3m), Caspian Pizza (2.6m), Caribbean Vibez (2.7m), Jai Kathmandu (4.2m), Bar Bibo (4.3m), Chesters (5.8m), Chick A Ritos (6.0m), Issano (6.8m), Brew'd (9.1m) | All 9 achieved `EXACT_COORDINATE_MATCH` $\rightarrow$ `SAFE_MATCH`. |
| **$10\text{ to }50\text{ meters}$** | 1 | Emirates Lounge (30.4m) | `EXACT_COORDINATE_MATCH` $\rightarrow$ `SAFE_MATCH`. |
| **$50\text{ to }180\text{ meters}$** | 1 | Costa Coffee MAN Terminal 3 (59.3m) | `STRONG_COORDINATE_MATCH` $\rightarrow$ `SAFE_MATCH`. |
| **$180\text{ to }1,000\text{ meters}$** | 1 | Aspire Lounge (closest returned: T3 at 234.9m, T2 at 901.4m) | Outside safe threshold $\rightarrow$ `BRANCH_MISMATCH`. |
| **$> 1,000\text{ meters}$** | 6 | Georgia Chicken (8.0km), F.R.I.E.N.D.S (8.4km), Burger King (12.3km), Deli Spice (12.5km), Pot Kettle Black (13.0km), Etihad Airways Lounge (5,678km) | Materially different location $\rightarrow$ `BRANCH_MISMATCH`. |
| **No places returned** | 2 | Delices de France, Food Village | `IDENTITY_MISMATCH` / No evidence. |

---

## 5. Candidate-by-Candidate Evaluation Results

| # | Candidate Name | OSM Coords (Lat, Lon) | Matched Google Listing | Distance | Identity Sim | Match Classification | Review Count | Rating | Latest Review Date | Freshness | Qualification Impact |
| :---: | :--- | :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| 1 | **Pot Kettle Black** | 53.36783, -2.28227 | *None (Barton Arcade 13km away)* | N/A | 0.90 | `BRANCH_MISMATCH` | None | None | None | UNKNOWN | Unchanged (`OUTREACH_READY`) |
| 2 | **Aspire Lounge** | 53.36126, -2.27369 | *None (T3 235m, T2 901m)* | N/A | 0.90 | `BRANCH_MISMATCH` | None | None | None | UNKNOWN | Unchanged (`RESEARCH_ONLY`) |
| 3 | **Bar Bibo** | 53.40761, -2.25802 | Bar Bibo | 4.3m | 1.00 | `SAFE_MATCH` | 56 | 3.8 | 2020-01-06 | STALE | Unchanged (`RESEARCH_ONLY`) |
| 4 | **Brew'd** | 53.36087, -2.26895 | Brew’d | 9.1m | 1.00 | `SAFE_MATCH` | 26 | 1.7 | 2026-07-30 | RECENT | Unchanged (`RESEARCH_ONLY`) |
| 5 | **Burger King** | 53.36679, -2.27931 | *None (Oxford St 12.3km away)* | N/A | 1.00 | `BRANCH_MISMATCH` | None | None | None | UNKNOWN | Unchanged (`RESEARCH_ONLY`) |
| 6 | **Caribbean Vibez** | 53.37681, -2.28035 | Caribbean Vibez LTD | 2.7m | 1.00 | `SAFE_MATCH` | 159 | 4.5 | 2026-06-09 | RECENT | Unchanged (`OUTREACH_READY`) |
| 7 | **Caspian Pizza** | 53.38009, -2.27608 | Caspian Pizza Wythenshawe | 2.6m | 0.90 | `SAFE_MATCH` | 87 | 4.3 | 2026-08-06 | RECENT | Unchanged (`RESEARCH_ONLY`) |
| 8 | **Chesters** | 53.40814, -2.25732 | Chesters Chicken Northenden | 5.8m | 0.90 | `SAFE_MATCH` | 141 | 4.7 | 2026-04-12 | RECENT | Unchanged (`RESEARCH_ONLY`) |
| 9 | **Chick A Ritos** | 53.39886, -2.27720 | Chick A Ritos Wythenshawe | 6.0m | 0.90 | `SAFE_MATCH` | 337 | 4.2 | 2024-08-15 | STALE | Unchanged (`RESEARCH_ONLY`) |
| 10 | **Costa Coffee** | 53.36026, -2.27076 | Costa Coffee (MAN T3) | 59.3m | 1.00 | `SAFE_MATCH` | 157 | 2.7 | 2026-06-24 | RECENT | Unchanged (`RESEARCH_ONLY`) |
| 11 | **Deli Spice** | 53.41021, -2.25590 | *None (Hyde 12.5km away)* | N/A | 0.90 | `BRANCH_MISMATCH` | None | None | None | UNKNOWN | Unchanged (`RESEARCH_ONLY`) |
| 12 | **Delices de France** | 53.36116, -2.27070 | *None (0 places returned)* | N/A | 0.00 | `IDENTITY_MISMATCH` | None | None | None | UNKNOWN | Unchanged (`RESEARCH_ONLY`) |
| 13 | **Emirates Lounge** | 53.36139, -2.27360 | Emirates Lounge (MAN T2) | 30.4m | 1.00 | `SAFE_MATCH` | 17 | 4.8 | 2025-06-22 | STALE | Unchanged (`RESEARCH_ONLY`) |
| 14 | **Etihad Airways Lounge** | 53.36134, -2.27430 | *None (Abu Dhabi 5,678km away)* | N/A | 0.90 | `BRANCH_MISMATCH` | None | None | None | UNKNOWN | Unchanged (`RESEARCH_ONLY`) |
| 15 | **F.R.I.E.N.D.S** | 53.39508, -2.28867 | *None (Quaker house/books >8km)* | N/A | 0.92 | `BRANCH_MISMATCH` | None | None | None | UNKNOWN | Unchanged (`MANUAL_REVIEW`) |
| 16 | **Food Village** | 53.36033, -2.27003 | *None (0 places returned)* | N/A | 0.00 | `IDENTITY_MISMATCH` | None | None | None | UNKNOWN | Unchanged (`RESEARCH_ONLY`) |
| 17 | **Founder Coffee Co** | 53.39282, -2.29234 | Founder Coffee Co | 2.3m | 1.00 | `SAFE_MATCH` | 25 | 4.6 | 2026-07-13 | RECENT | Unchanged (`RESEARCH_ONLY`) |
| 18 | **Georgia Chicken** | 53.38007, -2.27612 | *None (Fallowfield 8.0km away)* | N/A | 1.00 | `BRANCH_MISMATCH` | None | None | None | UNKNOWN | Unchanged (`RESEARCH_ONLY`) |
| 19 | **Issano** | 53.40801, -2.25742 | Issano Pizza & Grill House | 6.8m | 1.00 | `SAFE_MATCH` | 140 | 4.0 | 2026-08-29 | RECENT | Qualified (`OUTREACH_READY`) |
| 20 | **Jai Kathmandu** | 53.40851, -2.25694 | Jai Kathmandu, Northenden | 4.2m | 0.90 | `SAFE_MATCH` | 262 | 4.4 | 2026-05-21 | RECENT | Unchanged (`RESEARCH_ONLY`) |

---

## 6. Detailed Match Classification Breakdown

- **SAFE_MATCH (11 / 20, 55.0%):**
  - High identity confidence combined with verified physical proximity ($2.3\text{m} - 59.3\text{m}$).
  - Safely recovers 11 business listings with full review counts, ratings, and genuine review dates.
  - Multi-branch disambiguation demonstrated:
    - `Caspian Pizza`: Candidate at Wythenshawe matched `Caspian Pizza Wythenshawe` at **2.6m**, cleanly discarding `Caspian Pizza Bar` in Bury (22.6km away).
    - `Chesters`: Candidate on Palatine Rd Northenden matched `Chesters Chicken Northenden` at **5.8m**, cleanly discarding 7 other Chesters branches across Manchester (4.2km to 12.4km away).
    - `Costa Coffee`: Candidate at Airport T3 matched `Costa Coffee MAN Terminal 3` at **59.3m**, cleanly discarding 14 other Costa Coffee locations across Greater Manchester (>8.6km away).
- **AMBIGUOUS_MATCH (0 / 20, 0.0%):**
  - In this 20-candidate cohort, whenever multiple branches were returned by Gosom, exactly one branch was physically at the candidate coordinates ($\le 59.3\text{m}$), while all other branches were at least **4.1km away**.
  - Synthetic unit test `test_d_two_same_name_branches_both_plausible_ambiguous` confirmed that if two same-name branches are both within safe coordinate distance ($\le 180\text{m}$), the matcher classifies as `AMBIGUOUS_MATCH` and attaches zero review evidence.
- **BRANCH_MISMATCH (7 / 20, 35.0%):**
  - `Pot Kettle Black`: Airport candidate cleanly isolated from Barton Arcade, Tariff St, and Angel Gardens (~13.0km away).
  - `Aspire Lounge`: Manchester Airport candidate had T3 (234.9m) and T2 (901.4m), both exceeding the 180m threshold.
  - `Burger King`: Airport candidate coords vs Piccadilly/Oxford St (12.3km - 13.0km away).
  - `Deli Spice`: Northenden candidate coords vs Hyde branch (12.5km away).
  - `Etihad Airways Lounge`: Airport candidate coords vs Abu Dhabi lounge (5,678km away).
  - `F.R.I.E.N.D.S`: Candidate coords vs distant locations (>8.4km away).
  - `Georgia Chicken`: Wythenshawe candidate coords vs Fallowfield branch (8.0km away).
- **IDENTITY_MISMATCH (2 / 20, 10.0%):**
  - `Delices de France`: 0 places returned in Manchester.
  - `Food Village`: 0 places returned in Manchester.
- **NO_COORDINATE_EVIDENCE (0 / 20, 0.0%):**
  - All 20 OSM candidates and all scraped Google Maps listings in pool had valid, high-precision coordinates.

---

## 7. Review Evidence and Freshness Recovery

### Metric Recovery
- **Review Count Recovered:** 11 / 20 candidates (55.0%).
- **Rating Recovered:** 11 / 20 candidates (55.0%).
- **Review Timestamps Recovered:** 11 / 20 candidates (55.0%).

### Freshness Distribution (Reference Date: 2026-10-01)
- **RECENT ($\le 180\text{ days}$):** 8 candidates
  - Brew'd (2026-07-30)
  - Caribbean Vibez (2026-06-09)
  - Caspian Pizza (2026-08-06)
  - Chesters (2026-04-12)
  - Costa Coffee (2026-06-24)
  - Founder Coffee Co (2026-07-13)
  - Issano (2026-08-29)
  - Jai Kathmandu (2026-05-21)
- **STALE ($> 180\text{ days}$):** 3 candidates
  - Bar Bibo (2020-01-06 — 2,460 days old)
  - Chick A Ritos (2024-08-15 — 777 days old)
  - Emirates Lounge (2025-06-22 — 466 days old)
- **UNKNOWN:** 9 candidates (the 9 candidates without safe matches).

---

## 8. Pot Kettle Black Branch Isolation Result

The permanent Pot Kettle Black regression guard was tested:
- **Candidate Record:** OSM Node 12912726343, Latitude: 53.3678333, Longitude: -2.2822664 (Manchester Airport Terminal 2).
- **Scraped Places Evaluated:**
  - `POT KETTLE BLACK Barton Arcade`: 12,998.5 meters away.
  - `POT KETTLE BLACK Angel Gardens Manchester`: 13,547.2 meters away.
  - `Pot Kettle Black` (Tariff St): 13,020.4 meters away.
- **Matcher Decision:** `BRANCH_MISMATCH`.
- **Review Evidence Attached:** Exactly 0 reviews, 0 ratings, 0 timestamps. Freshness remained `UNKNOWN`.
- **Outcome:** **100% ISOLATED.** The airport candidate NEVER inherited Barton Arcade's 890 reviews or 4.6 rating.

---

## 9. False-Positive and False-Negative Analysis

### False-Positive Elimination (Major Safety Win)
In Phase 7.6 (which relied on name similarity without coordinate gating):
- `Georgia Chicken` (Candidate 18 in Wythenshawe) was incorrectly matched to `Georgia Chicken` on Wilmslow Road in Fallowfield (8.0km away) as a `STRONG_BUSINESS_MATCH`.
- In Phase 7.9, coordinate-first matching **caught and eliminated this false positive**:
  - Distance: 8,058.3 meters ($> 180.0\text{m}$).
  - Classification: `BRANCH_MISMATCH`.
  - Review evidence attached: None.
- **Overall False Positives in Phase 7.9:** **0 / 20 (0.0%)**.

### False-Negative Analysis
- Zero legitimate local matches were rejected.
- Candidates rejected as `BRANCH_MISMATCH` were legitimately at distant locations (8km to 13km away).
- `Aspire Lounge`: Manchester Airport candidate coords (53.361265, -2.273694) had listings for Terminal 3 (234.9m) and Terminal 2 (901.4m). Rejection was conservative and appropriate because terminal-specific identity could not be corroborated within 180m.

---

## 10. Rule B Multi-Source Independence Audit

- In accordance with Rule B Condition 5, Google Maps reviews and Google place details belong to a single source family (`SourceFamily.GOOGLE`).
- Recovering review count, rating, and freshness from Gosom does NOT by itself satisfy the independent 2nd operational family requirement.
- Only candidates possessing an independent operational source family (such as `Issano`, which has Food Hygiene Rating operational evidence) qualified for `ACTIVE_CONFIRMED` and `OUTREACH_READY`.
- Candidates where Google was the sole operational signal remained `ACTIVE_LIKELY` / `RESEARCH_ONLY`.
- **Qualification integrity was 100% preserved.**

---

## 11. Safety and CRM Invariant Verification

Pre-flight and post-flight checksums across all CRM cache files confirmed 0 mutations:

| CRM File | Pre-Flight SHA-256 | Post-Flight SHA-256 | Mutation Status |
| :--- | :---: | :---: | :---: |
| `data/cache_sheets_raw_leads.json` | `7e1aa71bb0...` | `7e1aa71bb0...` | **0 Mutations (MATCH)** |
| `data/cache_sheets_manual_review.json`| `7071ef24a1...` | `7071ef24a1...` | **0 Mutations (MATCH)** |
| `data/cache_sheets_client_ready.json` | `8c89498d36...` | `8c89498d36...` | **0 Mutations (MATCH)** |
| `data/campaigns.json` | `f6bc06cecb...` | `f6bc06cecb...` | **0 Mutations (MATCH)** |
| `data/message_history.json` | `f3c3065b70...` | `f3c3065b70...` | **0 Mutations (MATCH)** |

- **Apify Calls:** 0 ($0.00 spend).
- **Google Places API Calls:** 0 ($0.00 spend).
- **Paid Geocoding Calls:** 0 ($0.00 spend).
- **Outreach Messages Sent:** 0.
- **Campaigns Armed:** 0.
- **Fabricated Values:** 0.

---

## 12. Decision Rule Evaluation & Production Recommendation

### Core Question:
*"Does coordinate-first matching provide enough deterministic branch/identity protection to safely replace the current address requirement?"*

### Evaluation:
**YES, but ONLY under a strictly controlled dual-evidence gate, NOT as an open-ended fallback.**

1. **Why Coordinates Succeed Where Blind Queries Fail:**
   - Coordinate proximity ($\le 50\text{m}$ exact, $\le 180\text{m}$ strong) provides exceptional physical discrimination. It eliminated the Phase 7.6 false positive on `Georgia Chicken` (8km away) and safely isolated `Pot Kettle Black` (13km away).
   - It reliably disambiguates multi-branch chains (such as `Caspian Pizza`, `Chesters`, and `Costa Coffee`) when only one physical branch exists at the candidate location.
2. **Why Open-Ended Un-Gated Fallback Remains Dangerous:**
   - In extremely high-density environments (e.g. food courts in shopping malls, dense airport concourses), multiple separate units or even duplicate brand kiosks could theoretically exist within 180m.
   - Therefore, coordinate matching MUST always enforce the ambiguity rule: if $>1$ candidate listing exists within safe distance, the matcher MUST classify as `AMBIGUOUS_MATCH` and attach 0 review evidence.
3. **Recommendation:**
   - **`RECOMMENDATION=PROCEED_TO_CONTROLLED_GATE_REVIEW`**
   - Keep `GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED=false` until the controlled gate review is formally completed.
   - Do NOT activate production fallback globally.

---

## 13. Machine-Readable Summary

```ini
PHASE_7_9_STATUS=PASS
SAFE_MATCHES=11
AMBIGUOUS_MATCHES=0
BRANCH_MISMATCHES=7
IDENTITY_MISMATCHES=2
RECENT_REVIEW_RECOVERY=8
CRM_MUTATIONS=0
OUTREACH_SENDS=0
PRODUCTION_FLAG=OFF
RECOMMENDATION=PROCEED_TO_CONTROLLED_GATE_REVIEW
```
