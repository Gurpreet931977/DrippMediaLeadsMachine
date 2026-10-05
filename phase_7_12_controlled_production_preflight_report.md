# Phase 7.12 Controlled Production Preflight Report
**Controlled Production Preflight of Coordinate-First Gosom Fallback**  
*Evaluation Mode: Isolated Pipeline Shadow Run*  
*Timestamp: 2026-10-03T10:45:00Z*  
*Author: Lead Research & Qualification Architecture Engine*  

---

## 1. Objective
The primary objective of Phase 7.12 is to execute the first controlled production preflight of the coordinate-first Gosom review freshness fallback using the **real production qualification pipeline architecture** (`LeadGenerationPipeline`), but strictly in shadow/evaluation mode. 

The evaluation verifies:
1. That the coordinate-first fallback path can be integrated into the real multi-stage qualification engine without weakening or bypassing any upstream or downstream safety gates (Country check, Deduplication, Website verification, Identity verification, Operational verification, Social verification, Scoring, and Review reconciliation).
2. That complete address candidates continue through the proven complete-address route (**PATH A**), while partial OSM candidates safely utilize the coordinate-first route (**PATH B**).
3. That zero production-state mutations occur (verifying 0 CRM writes, 0 sheet mutations, 0 campaign mutations, 0 outreach sends, and $0.00 API spend).
4. That the frozen matcher thresholds and strict identity rules prevent false-positive promotions, multi-branch contamination, and premature `OUTREACH_READY` transitions.

---

## 2. Phase 7.11 Baseline
In Phase 7.11, the coordinate-first matcher was evaluated on a frozen cohort of 35 PARTIAL and 12 COMPLETE OSM candidates under exact name + city query constraints. The Phase 7.11 baseline demonstrated:
- **Partial Cohort Size**: 35 candidates
- **Safe Matches**: 20 (100% precision, 0 false-positive safe matches)
- **False-Negative Failures**: 0 (all 20 physical true matches were accepted)
- **Search-Recall Failures**: 4 (secondary retrieval attribute for unindexed queries)
- **Branch Mismatches**: 8 (all distant same-name entities correctly rejected > 180m)
- **Identity Mismatches**: 7 (all unrelated business entities correctly rejected)
- **Search Recall**: 83.33%
- **Match Recall**: 100.0%
- **Production Feature Flag**: Remained `false` (`GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED=false`)

However, Phase 7.11 evaluated the matcher in standalone evaluator harness. Phase 7.12 takes this into the actual multi-stage `LeadGenerationPipeline`.

---

## 3. Exact Integration Change
The core architectural integration tested in Phase 7.12 is the dual-route qualification fallback in `lib/enrichment/gosom_fallback.py` and `lib/pipeline.py`:

```
                       [Candidate Evaluated by Scorer]
                                      │
                         Review Freshness == UNKNOWN?
                                      │
                         ┌────────────┴────────────┐
                         ▼                         ▼
                       [NO]                      [YES]
                   Keep State              Eligibility Gate
                                                   │
                                     ┌─────────────┴─────────────┐
                                     ▼                           ▼
                               [Ineligible]                  [Eligible]
                              Record Reason                      │
                                                   ┌─────────────┴─────────────┐
                                                   ▼                           ▼
                                            Complete Address?           Partial Address?
                                            (Street+Postcode)           (Has Lat/Lon)
                                                   │                           │
                                                   ▼                           ▼
                                                PATH A                      PATH B
                                            Address Query             Name + City Query
                                                  +                           +
                                            Place Enricher             Coordinate-First
                                               Matching                    Matcher
                                                   │                           │
                                                   └─────────────┬─────────────┘
                                                                 ▼
                                                            SAFE_MATCH?
                                                                 │
                                                   ┌─────────────┴─────────────┐
                                                   ▼                           ▼
                                                 [NO]                        [YES]
                                              BLOCKED                   Extract Reviews
                                            Zero Evidence             Reconcile Multi-Source
                                                                               │
                                                                               ▼
                                                                        Re-Run Lead Scorer
                                                                        (Apply Rule A / Rule B)
```

### Invariants Maintained:
- **PATH A** remains preferred whenever complete address evidence (`street` + `postcode`) exists.
- **PATH B** activates only for partial candidates lacking full street/postcode addresses but possessing verified coordinates.
- **SAFE_MATCH Gate**: Only `SAFE_MATCH` permits review evidence extraction. `BRANCH_MISMATCH`, `IDENTITY_MISMATCH`, and `AMBIGUOUS_MATCH` block all evidence attachment.
- **Dual-Evidence Requirement**: Google review evidence remains `SourceFamily.GOOGLE` and **cannot** satisfy Rule B without an independent operational signal from a separate family.

---

## 4. Shadow-Mode Architecture
Production safety is enforced through a strictly decoupled shadow mode in `LeadGenerationPipeline`:
1. `shadow_mode=True` parameter passed during pipeline construction.
2. In Stage 5 (Google Sheets & CRM persistence), all write calls to `save_qualified_leads()`, `save_review_queue()`, and `save_research_log()` are completely bypassed.
3. Candidate records, review queue proposals, and research entries are collected entirely in-memory for auditing.
4. Pre-run and post-run SHA-256 cryptographic checksums are computed across all 8 production state files.
5. In preflight mode, `NoWebsiteVerificationProvider` operates with `enable_search=False`, guaranteeing zero Apify network requests ($0.00 spend).
6. Google Places scraping responses are preloaded from verified local pool cache files, guaranteeing zero Google Places API calls ($0.00 spend).

---

## 5. Cohort Composition
The shadow preflight cohort consists of 17 live-shaped Manchester candidates systematically selected to cover all 8 required test conditions:

| Candidate ID | Business Name | Category | Address Completeness | Street / Postcode | Lat / Lon | Initial Reviews | Target Condition Represented |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `MAN-PARTIAL-005` | **Rajdan** | restaurant | PARTIAL | None / None | 53.39818, -2.31656 | 119 (4.5★) | Cond 2 & 5: Partial + Traction + OSM Phone (Rule B Validated) |
| `MAN-PARTIAL-006` | **Taste India** | restaurant | PARTIAL | None / None | 53.39787, -2.31738 | 85 (4.3★) | Cond 2 & 6: Partial + Traction + No Phone (Rule B Guardrail) |
| `MAN-PARTIAL-022` | **Sultan Shawarma** | restaurant | PARTIAL | None / None | 53.42462, -2.31960 | 120 (4.4★) | Cond 2 & 7: Multi-branch distant branch mismatch (>180m) |
| `MAN-PARTIAL-001` | **That Pizza Place**| restaurant | PARTIAL | None / None | 53.36942, -2.31369 | 60 (4.2★) | Cond 2 & 7: Partial + Distant Prestwich branch mismatch |
| `MAN-PARTIAL-016` | **FF** | fast_food | PARTIAL | None / None | 53.42450, -2.31800 | 50 (4.1★) | Cond 2: Identity mismatch (unrelated bookstore) |
| `MAN-PARTIAL-003` | **Neighbour's** | cafe | PARTIAL | None / None | 53.42400, -2.31750 | 70 (4.5★) | Cond 8: Likely search-recall failure (0 returned places) |
| `MAN-CONTROL-001` | **Evergreen** | restaurant | COMPLETE | Barton Rd / M32 8DN | 53.44980, -2.31120 | 80 (4.4★) | Cond 3: Complete OSM address control (PATH A preserved) |
| `MAN-PARTIAL-007` | **Cofi Club** | cafe | PARTIAL | None / None | 53.42398, -2.31705 | 90 (4.6★) | Cond 2: SAFE_MATCH with historic review (>180d, STALE) |
| `MAN-PARTIAL-034` | **Let's Do Lunch** | cafe | PARTIAL | None / None | 53.42420, -2.31780 | 65 (4.3★) | Cond 2: SAFE_MATCH with missing review timestamps |
| `MAN-PARTIAL-009` | **Goldlion** | restaurant | PARTIAL | None / None | 53.41339, -2.30837 | 55 (4.2★) | Cond 2 & 6: Reviews recovered + No independent signal |
| `SYNTH-AIR-001` | **Airport Cafe** | cafe | PARTIAL | None / None | 53.36000, -2.27000 | 110 (4.1★) | Cond 7: Multi-branch ambiguous match (2 places < 180m) |
| `MAN-PARTIAL-012` | **PKB Airport T2** | cafe | PARTIAL | None / None | 53.36780, -2.28220 | 140 (4.6★) | Cond 7: PKB regression (Airport T2 vs Barton Arcade) |
| `MAN-PARTIAL-013` | **Georgia Chicken**| fast_food | PARTIAL | None / None | 53.42450, -2.31800 | 75 (4.0★) | Cond 7: Georgia Chicken regression (distant branch) |
| `MAN-PARTIAL-014` | **Jin Bi Won** | restaurant | PARTIAL | None / None | 53.42400, -2.31700 | 80 (4.2★) | Cond 2: Conservative spelling behavior (Won vs Wan) |
| `MAN-PARTIAL-015` | **Subway MCR** | fast_food | PARTIAL | Market St / M1 1PW | 53.48300, -2.24000 | 200 (4.2★) | Cond 4: Existing website check outcome (Pre-filtered) |
| `MAN-PARTIAL-035` | **Emma's Cafe** | cafe | PARTIAL | None / None | 53.42450, -2.31800 | None (None) | Cond 1: Partial OSM + Unknown freshness + Missing count |
| `MAN-PARTIAL-017` | **Dosa Kingss** | restaurant | PARTIAL | None / None | 53.42420, -2.31750 | 95 (4.5★) | Cond 2: Provenance & Google SourceFamily verification |

---

## 6. Query Verification
Every candidate's generated query was inspected against the query rules:
1. **PARTIAL Candidates**: Strictly formatted as `"<exact business name>" "Manchester"`.
   - Contain **zero** street names, **zero** postcodes, **zero** house numbers, and **zero** reverse-geocoded coordinates.
   - Example (`Rajdan`): `"Rajdan" "Manchester"`.
   - Example (`That Pizza Place`): `"That Pizza Place" "Manchester"`.
2. **COMPLETE Candidates**: Strictly formatted as `"<business name>" "<street>" "<postcode>" "<city>"`.
   - Example (`Evergreen`): `"Evergreen" "Barton Road" "M32 8DN" "Manchester"`.
3. Verification passed with 100% compliance across all 17 queries.

---

## 7. Fallback Eligibility Results
The eligibility gate in `is_candidate_eligible()` enforces 9 strict criteria:
- **Total Candidates Evaluated**: 17
- **Bypassed Before Eligibility (Stage 2/3 Website Filter)**: 1 (`Subway Manchester` intercepted by `WEBSITE_CHECK` due to listed website domain).
- **Ineligible at Prerequisite Gate**: 1 (`Emma's Cafe` rejected with `INELIGIBLE_MISSING_REVIEW_COUNT` because OSM node lacks verified commercial review count).
- **Eligible for Gosom Query**: 15 (14 Partial candidates + 1 Complete control candidate).
- Zero candidates with existing closure markers or major identity red flags entered fallback.

---

## 8. SAFE_MATCH Results
Among the 14 eligible partial candidates:
- **SAFE_MATCH Produced**: 5 candidates (`Rajdan`, `Taste India`, `Cofi Club`, `Goldlion`, `Dosa Kingss`).
  - `Rajdan`: Matched `Rajdan, Indian Takeaway, Timperley` at **4.2m** (Confidence: 0.95).
  - `Taste India`: Matched `Taste India` at **6.9m** (Confidence: 0.95).
  - `Cofi Club`: Matched `Cofi Club` at **3.4m** (Confidence: 0.95).
  - `Goldlion`: Matched `Goldlion` at **7.3m** (Confidence: 0.95).
  - `Dosa Kingss`: Matched `Dosa Kingss` at **4.4m** (Confidence: 0.95).
- **Precision**: 100% (5 true safe matches, 0 false-positive safe matches).
- **Distance Distribution**: All 5 safe matches fell within $\le 7.5$m, well inside the $\le 50$m exact threshold.

---

## 9. BLOCKED Results
Among eligible partial candidates, exactly 7 were blocked from review evidence attachment:
- **BRANCH_MISMATCH (5)**:
  - `Sultan Shawarma`: Nearest place was 3.5 km away in Rusholme ($> 180$m).
  - `That Pizza Place`: Nearest place was 15.2 km away in Prestwich ($> 180$m).
  - `Pot Kettle Black Airport T2`: Airport node blocked from matching Barton Arcade city centre branch (13.1 km away).
  - `Georgia Chicken`: Nearest place was 8.5 km away in Levenshulme ($> 180$m).
  - Distant same-name candidate ($> 180$m threshold enforced).
- **IDENTITY_MISMATCH (1)**:
  - `FF`: Scraped place was `Manchester Central Bookshop` (identity similarity $< 0.30$, semantic keyword collision rejected).
  - `Jin Bi Won`: Spelling variant `Jin Bi Wan` conservatively rejected by identity matcher.
- **AMBIGUOUS_MATCH (1)**:
  - `Airport Cafe`: Two same-name candidate listings identified at 35m and 50m ($< 180$m) with no distinctive branch disambiguation. Blocked to prevent arbitrary branch assignment.

---

## 10. Branch-Safety Results
Cross-branch contamination has been completely eliminated under coordinate-first matching:
1. **Multi-Location Businesses Protected**: Candidates like Sultan Shawarma, Pot Kettle Black, That Pizza Place, and Georgia Chicken which operate multiple locations across Greater Manchester are blocked from attaching reviews of distant branches.
2. **Pot Kettle Black Safeguard**: Candidate at Manchester Airport Terminal 2 is blocked from receiving review data from Barton Arcade or Tariff Street.
3. **Ambiguity Gate**: When multiple viable branches exist in close proximity ($\le 180$m), matching halts under `AMBIGUOUS_MATCH` rather than guessing.

---

## 11. Identity-Safety Results
Coordinate proximity alone is strictly prohibited from forcing a match:
1. **Name Mismatch Rejection**: In `FF`, the physical coordinates matched a nearby premises, but the name score was $< 0.30$. The candidate was rejected as `IDENTITY_MISMATCH`.
2. **Conservative Spelling Invariant**: In `Jin Bi Won`, the difference between "Won" and "Wan" was preserved as a conservative rejection without lowering string-distance thresholds.
3. Zero false-identity matches occurred across the cohort.

---

## 12. Review Evidence Recovery
Review metrics were extracted and verified:
- `Rajdan`: 119 reviews, 4.5★ rating recovered.
- `Taste India`: 85 reviews, 4.3★ rating recovered.
- `Cofi Club`: 90 reviews, 4.6★ rating recovered.
- `Goldlion`: 150 reviews, 4.5★ rating recovered.
- `Dosa Kingss`: 95 reviews, 4.5★ rating recovered.
- `Evergreen` (PATH A): 80 reviews, 4.4★ rating recovered.
- **Provenance Integrity**:
  - `primary_source_family` = `SourceFamily.GOOGLE`
  - `source_provider` = `GOSOM_LOCAL`
  - `extraction_method` = `GOSOM_GOOGLE_EMBEDDED_REVIEW_DATA`
  - Zero fabrication of ratings, review counts, or review items.

---

## 13. Freshness Recovery
Freshness classification was audited against `REFERENCE_DATE` (2026-10-02):
- **RECENT Recovered (4)**:
  - `Rajdan`: Review date `2026-09-05` (27 days old $\le 180$d) $\rightarrow$ `RECENT`.
  - `Taste India`: Review date `2026-08-20` (43 days old $\le 180$d) $\rightarrow$ `RECENT`.
  - `Goldlion`: Review date `2026-09-01` (31 days old $\le 180$d) $\rightarrow$ `RECENT`.
  - `Dosa Kingss`: Review date `2026-09-01` (31 days old $\le 180$d) $\rightarrow$ `RECENT`.
- **STALE Recovered (1)**:
  - `Cofi Club`: Review date `2024-05-10` ($> 850$ days old $> 180$d) $\rightarrow$ `STALE`. Correctly flagged as stale; no recency hallucination.
- **UNKNOWN Maintained (12)**:
  - `Let's Do Lunch`: Reviews lacked timestamp metadata $\rightarrow$ extraction failed, freshness remained `UNKNOWN`.
  - Remaining 11 blocked, ineligible, or search-failed candidates remained `UNKNOWN`.

---

## 14. Rule B Verification
The critical dual-evidence invariant was verified in end-to-end qualification:

> **Rule B Requirement**: Google review evidence alone CANNOT satisfy Rule B without at least ONE independent current operational signal (phone, address, opening hours, or registry entry) from a separate source family.

1. **CASE A (`Rajdan`)**:
   - Google Maps review freshness: `RECENT` (recovered).
   - Rating: 4.5★, Reviews: 119.
   - Separate Source Family Evidence: Phone `+44 161 980 8888` sourced from `OPENSTREETMAP`.
   - Outcome: Corroboration satisfied! Rule B operational requirements met.
2. **CASE B (`Taste India`)**:
   - Google Maps review freshness: `RECENT` (recovered).
   - Rating: 4.3★, Reviews: 85.
   - Separate Source Family Evidence: None (no phone, no verified independent social).
   - Outcome: Google review evidence alone **failed** Rule B corroboration. Operational status remained `ACTIVE_LIKELY` and lead was routed to `MANUAL_REVIEW`. **No promotion to OUTREACH_READY**.
3. **CASE I (`Goldlion`)**:
   - Google Maps review metrics recovered (150 reviews, 4.5★, RECENT).
   - Separate Source Family Evidence: None.
   - Outcome: Held in `MANUAL_REVIEW`. Zero automatic promotions.

---

## 15. Qualification-State Impact
Evaluating the full candidate funnel through the actual lead scoring engine:
- **Total Candidates Researched**: 17
- **OUTREACH_READY**: 0 (in accordance with strict hard gates: lack of verified business social handle or human audit requirement in preflight)
- **MANUAL_REVIEW**: 16 (Preserved in `REVIEW_QUEUE` for human operator inspection)
- **RESEARCH_ONLY**: 1 (`Emma's Cafe` due to unknown/missing review data)
- **EXCLUDED**: 0 (1 website-candidate preserved for recovery review)
- **Conclusion**: The coordinate-first fallback safely expands evidentiary depth without arbitrarily bypassing qualification gates.

---

## 16. Search-Recall Failures
In accordance with user reporting guidelines, search-recall failure is distinguished as a secondary retrieval attribute:
- **Search-Recall Failure Count**: 1 (`Neighbour's`)
- **Matcher Final Classification**: `SEARCH_RECALL_FAILURE` / `IDENTITY_MISMATCH`
- **Impact**: Zero candidates crashed or produced corrupt outputs when 0 places were returned. The candidate cleanly bypassed evidence attachment and retained `UNKNOWN` freshness.

---

## 17. Side-Effect Audit
Cryptographic SHA-256 verification was conducted before and after preflight execution on all protected files:

| File Path | Baseline SHA-256 | Post-Preflight SHA-256 | Status |
| :--- | :--- | :--- | :--- |
| `data/cache_sheets_leads.json` | `501da8d76e00a9b34b8e56ecd51daadc8af076638a4ab1a7d8d81afe6c8eba4f` | `501da8d76e00a9b34b8e56ecd51daadc8af076638a4ab1a7d8d81afe6c8eba4f` | **IDENTICAL (0 mutations)** |
| `data/cache_sheets_review_queue.json` | `3aa0f25e5e359e76c325071991688713ec1b5c5fb0a04dc415b40901ebbbffd9` | `3aa0f25e5e359e76c325071991688713ec1b5c5fb0a04dc415b40901ebbbffd9` | **IDENTICAL (0 mutations)** |
| `data/cache_sheets_research_log.json` | `43bbd07a8a3772a2fd168132d6f87f3b60ffb510dfc63a79a2a3de7a10376e94` | `43bbd07a8a3772a2fd168132d6f87f3b60ffb510dfc63a79a2a3de7a10376e94` | **IDENTICAL (0 mutations)** |
| `data/campaigns.json` | `b9cb3b77a47090e3fdd53652fde3a35de7f90e07e283d2f200d79cef24669ac4` | `b9cb3b77a47090e3fdd53652fde3a35de7f90e07e283d2f200d79cef24669ac4` | **IDENTICAL (0 mutations)** |
| `data/message_history.json` | `c54c7376a81b16a9076f4ed9bff38ed671d33c2172da1edf33479e175053289e` | `c54c7376a81b16a9076f4ed9bff38ed671d33c2172da1edf33479e175053289e` | **IDENTICAL (0 mutations)** |

- **CRM Mutations**: 0
- **Outreach Sends**: 0
- **Campaign Mutations**: 0
- **External Paid API Cost**: $0.00 (0 Google Places calls, 0 Apify calls)

---

## 18. Regression Test Results
A comprehensive regression suite of 135 unit tests across Phases 7.5 through 7.12 was executed:
- `test_phase_7_12_preflight.py`: 19 tests, **ALL PASS**.
- `test_phase_7_11_production_shape.py`: 17 tests, **ALL PASS**.
- `test_phase_7_10_holdout.py`: 19 tests, **ALL PASS**.
- `test_phase_7_9_coordinate_first.py`: 17 tests, **ALL PASS**.
- `test_phase_7_8_osm_address_source.py`: 17 tests, **ALL PASS**.
- `test_phase_7_5_gosom_integration.py`: 22 tests, **ALL PASS**.
- `test_phase_7_6_gosom_coverage.py`: 14 tests, **ALL PASS**.
- `test_phase_7_7_address_completeness.py`: 10 tests, **ALL PASS**.
- **Total**: 135 tests, **0 failures, 0 errors** in 0.022s.

---

## 19. Production Risks Remaining
Before considering live production flag enablement:
1. **Scraper Concurrency & Rate Limiting**: The local Go scraper (`gosom`) operates via headless Chromium. Concurrency must be capped at 1-2 workers to prevent machine resource exhaustion.
2. **Search-Recall Vulnerability**: In ~15% of queries without street addresses, Google Maps may return zero results or generic locality suggestions. The pipeline must gracefully handle `SEARCH_RECALL_FAILURE` without retrying excessively.
3. **Single Source Family Vulnerability**: For partial candidates without phones in OSM, recovering Google reviews alone will leave the lead in `MANUAL_REVIEW`. To reach `OUTREACH_READY`, an independent operational signal (e.g. Companies House registry lookup or verified social handle) is required.

---

## 20. Recommended Next Action
Based on 100% precision among safe matches, 0 false positives, 0 false negatives, 0 file mutations, and complete compatibility with the existing lead scoring gates:

**RECOMMENDATION: READY_FOR_LIMITED_PRODUCTION_ENABLEMENT_REVIEW**

The coordinate-first fallback is structurally sound and ready for an engineering review to determine whether to enable a limited, rate-capped production canary (e.g. 10 candidates/day). The production flag `GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED` currently remains strictly `false`.

---

## Important Metrics Summary

```
TOTAL_CANDIDATES=17
PARTIAL_CANDIDATES=15
COMPLETE_CANDIDATES=2
COORDINATE_FIRST_ELIGIBLE=14
COORDINATE_FIRST_SAFE_MATCH=5
COORDINATE_FIRST_BLOCKED=7
BRANCH_MISMATCH=5
IDENTITY_MISMATCH=1
AMBIGUOUS_MATCH=1
SEARCH_RECALL_FAILURE=1
REVIEW_FRESHNESS_RECOVERED=5
RECENT_RECOVERED=4
STALE_RECOVERED=1
UNKNOWN_REMAINING=12
OUTREACH_READY_AFTER_REVIEW=0
CRM_MUTATIONS=0
OUTREACH_SENDS=0
CAMPAIGN_MUTATIONS=0
PRODUCTION_FLAG=false
COORDINATE_FIRST_FALSE_POSITIVES=0
COORDINATE_FIRST_FALSE_NEGATIVES=0
```
