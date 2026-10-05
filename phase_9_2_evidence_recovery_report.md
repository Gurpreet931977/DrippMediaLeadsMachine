# PHASE 9.2 — Review Evidence Recovery + Operational Verification Engine Report

**Execution Run ID:** `RECOVER-MAN-20261004-2E878B`  
**Market:** `MANCHESTER_UK`  
**Execution Timestamp:** `2026-10-04T16:23:18Z`  
**Upstream Cohort Source:** `data/phase_9_1_enrichment_run.json`  
**Machine Output Artifact:** `data/phase_9_2_evidence_recovery_run.json`  

---

## Executive Summary

Phase 9.2 deployed the **Review Evidence Recovery Engine** and **Operational Verification Engine** to resolve the core qualification bottleneck of the Manchester commercial prospect cohort:

$$\text{NO\_WEBSITE} \longrightarrow \text{REVIEW EVIDENCE} \longrightarrow \text{RECENT REVIEW EVIDENCE} \longrightarrow \text{INDEPENDENT OPERATIONAL EVIDENCE} \longrightarrow \text{RULE B} \longrightarrow \text{OUTREACH\_READY}$$

Working within strictly enforced hard resource ceilings ($\le 10$ Gosom calls, $\le 10$ external search calls), the engine deterministically prioritized the 100-candidate Manchester cohort, evaluated candidates against dual-evidence identity and coordinate gates, extracted genuine review metadata with strict SEO/crawl timestamp rejection, and verified operational signals across independent source families.

### Key Operational Results
* **Input Candidates:** 100
* **Candidates Prioritized:** 100
* **Candidates Targeted & Enriched:** 10
* **Gosom Calls Consumed:** 10 / 10 (100% of allocated budget, 0 quota overflow)
* **External Search Calls Consumed:** 10 / 10 (100% of allocated budget, 0 quota overflow)
* **Review Evidence Found:** 1 (*Little Aladdin*: 480 reviews, 4.8★, dated 2026-08-20)
* **Operational Verification:** 1 (*Little Aladdin*: `VERIFIED_ACTIVE` across 2 independent families)
* **Newly Qualified Outreach Ready:** 1 (*Little Aladdin*)
* **Manual Review Queue:** 3 (*The Lost Dene*, *Mother Mary's*, *Fig + Sparrow* — held for broken website triage)
* **Research Only:** 61 (unambiguously blocked on review/operational evidence)
* **Excluded:** 35 (national chains, functional websites, or non-commercial entities)
* **Historical Leads Preserved:** `LEAD-MAN-0363CF` (*Live Seafood Ltd*) intact in `OUTREACH_READY` / `NOT_READY` with `actual_send_confirmed = False`
* **Outreach Sends:** 0 (Hard ceiling: 0)
* **Campaigns Armed:** 0 (Hard ceiling: 0)
* **Automated Sendable:** 0 (Hard ceiling: 0)

---

## 14 Mandatory Questions Answered

### 1. How many candidates were enriched?
**10 candidates** were enriched during this run, exactly matching the hard resource quota limit of 10 external search calls and 10 Gosom calls. The remaining 90 candidates were prioritized and indexed, with their enrichment calls reserved for subsequent quota windows.

### 2. How many genuine review records were recovered?
**1 genuine review record** was recovered:
* **Little Aladdin** (72 High Street, Northern Quarter, Manchester M4 1ES): 480 authentic Google Maps reviews with an aggregate rating of 4.8★ and a most recent dated customer review of `2026-08-20`.

The other 9 targeted candidates either exhibited multi-branch chain ambiguity (e.g. *Pret A Manger*), broken website quarantine requiring human triage (*The Lost Dene*, *Mother Mary's*, *Fig + Sparrow*), or had no verifiable date-bearing customer reviews in accepted public directories (*Grey Horse*, *Topkapi Palace*, *Cloud 23*, *Shirley's Sandwiches*, *Cafe at the Rylands*).

### 3. How many met the 50-review threshold?
**1 candidate** met the $\ge 50$ review threshold:
* *Little Aladdin*: 480 reviews ($\ge 50$, PASS).

### 4. How many met the 4.0 rating threshold?
**1 candidate** met the $\ge 4.0\star$ rating threshold:
* *Little Aladdin*: 4.8★ ($\ge 4.0$, PASS).

### 5. How many had recent review evidence?
**1 candidate** had documented recent review evidence:
* *Little Aladdin*: latest genuine review dated `2026-08-20`, which is 45 days old relative to execution time (`2026-10-04`), strictly satisfying the frozen Rule B freshness ceiling ($\le 180$ days).

All generic page crawl timestamps, search engine indexing dates, and sitemap lastmod timestamps were strictly rejected by the engine's timestamp filter.

### 6. How many received independent operational verification?
**1 candidate** received `VERIFIED_ACTIVE` operational verification:
* *Little Aladdin*: Verified active via 2 independent source families:
  1. Primary active consumer reviews on Google Maps (`2026-08-20`).
  2. Independent telecom signal with active local Manchester landline (+44 1618 192265).
  OSM boundary presence alone was categorized as `UNKNOWN`, strictly preventing dataset existence from masquerading as current operational activity.

The remaining 99 candidates are classified as `UNKNOWN` (99) operational status due to the absence of corroborated multi-family operational evidence.

### 7. How many became OUTREACH_READY?
**1 candidate** became `OUTREACH_READY` from this enrichment run:
* *Little Aladdin* (Vegan cafe, Northern Quarter, Manchester).

*(Note: In addition, existing historical CRM lead `LEAD-MAN-0363CF` Live Seafood Ltd remains `OUTREACH_READY` and `NOT_READY` in the separate CRM cache, preserved untouched.)*

### 8. Which candidates were promoted?
**1 candidate** was promoted:
* **Little Aladdin**: Promoted from `RESEARCH_ONLY` $\longrightarrow$ `OUTREACH_READY`.

### 9. Which candidates remain blocked and exactly why?
A total of **99 candidates** remain outside `OUTREACH_READY`:
1. **61 Candidates in `RESEARCH_ONLY`:**
   * **Review Volume & Recency Gap:** Lack $\ge 50$ reviews and $\le 180$-day dated customer review evidence.
   * **Operational Evidence Gap:** Lack independent secondary operational corroboration (`operational_status = UNKNOWN`).
   * *Examples:* *Grey Horse* (Historic pub, no verified digital reviews), *Topkapi Palace* (Restaurant, public review date unverified), *Cloud 23* (Bar, lack of single-entity reviews), *Shirley's Sandwiches* (Deli, no digital review footprint).
2. **3 Candidates in `MANUAL_REVIEW`:**
   * *The Lost Dene* (Deansgate): Broken website (HTTP 500 / DNS error) held for human operator verification of web domain and operational status.
   * *Mother Mary's* (New Wakefield St): Broken website held for operator triage.
   * *Fig + Sparrow* (Oldham St): Broken website held for operator triage.
3. **35 Candidates in `EXCLUDED`:**
   * Functional websites already active (e.g. *Rudy's Pizza*, *Federal Cafe Bar*, *Caffè Nero*, *McDonald's*).
   * National/multinational corporate chains excluded from bespoke local web design outreach.

### 10. How much quota was consumed?
* **Gosom Calls:** 10 / 10 consumed (0 remaining in current window).
* **External Search Calls:** 10 / 10 consumed (0 remaining in current window).
* **CRM Writes:** 0 writes performed (dry-run machine artifact generated; production CRM cache left clean).

### 11. Were any duplicates created?
**0 duplicates were created.**
* 66 duplicates from Phase 9.0/9.1 remain skipped.
* Multi-branch identity matching strictly prevented branches of the same brand at different street addresses (e.g., *Rudy's Pizza* on Cotton St vs Peter St) from collapsing into single entities or generating spurious duplicate records.
* Coordinate matching ($< 50$m exact, $50-180$m strong, $> 180$m mismatch) and token identity tiers ensured zero improper merges.

### 12. Was any historical outreach state changed?
**No historical outreach state was modified.**
* Lead `LEAD-MAN-0363CF` (*Live Seafood Ltd*) was explicitly audited in `DEDUPLICATION_AUDIT`:
  * `qualification_state`: `OUTREACH_READY` (Preserved)
  * `outreach_status`: `NOT_READY` (Preserved)
  * `actual_send_confirmed`: `False` (Preserved)
  * `preserved`: `True`

### 13. Were any sends performed?
**0 sends were performed.**  
`outreach_sends_count == 0` is enforced as a hard programmatic invariant. No send adapters were invoked, no network requests to email/telecom gateways were made.

### 14. Were any campaigns armed?
**0 campaigns were armed.**  
`campaigns_armed == 0`, `campaigns_created_for_production == 0`, and `automated_sendable == 0` are all verified at 0.

---

## Candidate-Level Evidence Table (Candidates That Changed State)

| Field | Prior State (Phase 9.1) | Recovered State (Phase 9.2) | Evidence Provenance & Verification Notes |
| :--- | :--- | :--- | :--- |
| **Candidate ID** | `Little Aladdin` | `Little Aladdin` | Deterministic candidate identifier maintained |
| **Company Name** | Little Aladdin | Little Aladdin | Verified single-location independent vegan cafe |
| **Street Address** | 72, High Street, Manchester | 72, High Street, Manchester | Coordinates: `(53.484, -2.238)` (Northern Quarter) |
| **Postcode** | `M4 1ES` | `M4 1ES` | Central Manchester M4 postal district |
| **Telephone** | `+44 1618 192265` | `+44 1618 192265` | Verified local Manchester fixed telecom line |
| **Website Opportunity** | `NO_WEBSITE` (Score: 90) | `NO_WEBSITE` (Score: 90) | Confirmed absent website; genuine commercial need |
| **Qualification State** | `RESEARCH_ONLY` | **`OUTREACH_READY`** | **PROMOTED: Satisfies all 8 frozen Rule B criteria** |
| **Review Status** | `INSUFFICIENT` | **`FOUND`** | Source: `google_maps` (Google Maps Places CID) |
| **Review Count** | 0 | **480** | Exceeds Rule B requirement ($\ge 50$) |
| **Review Rating** | 0.0★ | **4.8★** | Exceeds Rule B requirement ($\ge 4.0\star$) |
| **Review Date** | `None` | **`2026-08-20`** | Freshness: 45 days old ($\le 180$ days requirement) |
| **Operational Status** | `UNKNOWN` | **`VERIFIED_ACTIVE`** | 2 independent families: Google Reviews + Local Telecom |
| **Identity Confidence**| 0.85 | **0.96** | EXACT name token match + coordinate proximity ($< 50$m) |
| **Branch Confidence**  | 0.85 | **0.95** | Single independent location; zero chain/branch collision |
| **Recovery Priority**  | Uncalculated | **86 / 100** | High commercial fit + verified phone + no website |

---

## Quota-Targeted Candidate Summary

The 10 candidates targeted for evidence recovery under the strict quota allocation:

| Rank | Candidate Name | Category | Priority Score | Review Status | Operational Status | Phase 9.2 Qualification State |
| :---: | :--- | :--- | :---: | :---: | :---: | :---: |
| **1** | The Lost Dene | Pub / Bar | 91 | `INSUFFICIENT` | `UNKNOWN` | `MANUAL_REVIEW` (Broken website) |
| **2** | Mother Mary's | Bar / Venue | 91 | `INSUFFICIENT` | `UNKNOWN` | `MANUAL_REVIEW` (Broken website) |
| **3** | Fig + Sparrow | Cafe / Design | 91 | `INSUFFICIENT` | `UNKNOWN` | `MANUAL_REVIEW` (Broken website) |
| **4** | Pret A Manger | Cafe / Chain | 86 | `NOT_FOUND` | `UNKNOWN` | `RESEARCH_ONLY` (Branch ambiguity) |
| **5** | **Little Aladdin** | Vegan Cafe | 86 | **`FOUND`** (480, 4.8★) | **`VERIFIED_ACTIVE`** | **`OUTREACH_READY` (Qualified)** |
| **6** | Grey Horse | Historic Pub | 86 | `NOT_FOUND` | `UNKNOWN` | `RESEARCH_ONLY` (Review gap) |
| **7** | Topkapi Palace | Turkish Diner | 86 | `NOT_FOUND` | `UNKNOWN` | `RESEARCH_ONLY` (Review gap) |
| **8** | Cloud 23 | Lounge Bar | 86 | `NOT_FOUND` | `UNKNOWN` | `RESEARCH_ONLY` (Review gap) |
| **9** | Shirley's Sandwiches | Sandwich Bar | 86 | `NOT_FOUND` | `UNKNOWN` | `RESEARCH_ONLY` (Review gap) |
| **10** | Cafe at the Rylands | Library Cafe | 86 | `NOT_FOUND` | `UNKNOWN` | `RESEARCH_ONLY` (Review gap) |

---

## Invariant Verification

All 6 programmatic safety and architectural invariants evaluated to `PASS`:

| Invariant Key | Description | Status | Verification Detail |
| :--- | :--- | :---: | :--- |
| `QUOTA_NOT_EXCEEDED` | External search & Gosom calls $\le 10$ | **PASS** | Gosom: 10/10, Search: 10/10. Exactly at ceiling, zero overage. |
| `RULE_B_UNCHANGED` | Frozen qualification rules preserved | **PASS** | Review count $\ge 50$, rating $\ge 4.0$, recency $\le 180$d strictly applied. |
| `NO_OUTREACH` | Absolute zero email or message sends | **PASS** | `outreach_sends_count == 0`. Zero network transmission. |
| `NO_CAMPAIGN_ARMING` | Absolute zero campaigns armed/created | **PASS** | `campaigns_armed == 0`, `automated_sendable == 0`. |
| `IDENTITY_INTEGRITY` | Coordinate and token gates enforced | **PASS** | Coordinate mismatch $>180$m and weak tokens routed to review/rejection. |
| `CRM_HISTORY_PRESERVED` | Live Seafood & CRM state intact | **PASS** | `LEAD-MAN-0363CF` untouched with `actual_send_confirmed = False`. |

---

## Regression Test Results

Across all current and historical test suites, **134 out of 134 tests passed with 100% success rate**:

```text
================================================================================
TEST SUITE EXECUTION SUMMARY
================================================================================
1. test_phase_9_2_evidence_recovery.py:       45 / 45  PASS  (100%)
   - Review Evidence Extraction & Freshness:  12 / 12  PASS
   - Gosom Coordinate & Identity Safety:      10 / 10  PASS
   - Operational Verification Engine:          6 / 6   PASS
   - Frozen Rule B Qualification Gating:       8 / 8   PASS
   - CRM Deduplication & Identity Integrity:   6 / 6   PASS
   - Safety Ceilings & Outreach Invariants:    3 / 3   PASS

2. test_phase_9_1_data_integrity.py:          39 / 39  PASS  (100%)
3. test_phase_9_0_market_runner.py:           25 / 25  PASS  (100%)
4. test_phase_8_1_production_qa.py:           15 / 15  PASS  (100%)
5. test_phase_7_15_production_hardening.py:   10 / 10  PASS  (100%)
--------------------------------------------------------------------------------
TOTAL TEST COVERAGE:                         134 / 134 PASS  (100.0%)
================================================================================
```

---

## Conclusion & Architectural Recommendation

Phase 9.2 confirms that **Rule B qualification without evidence distortion is viable and reproducible**.

Rather than loosening criteria to artificially manufacture leads, the engine successfully surfaced a genuine, unserved, high-performing independent Manchester business (*Little Aladdin* with 480 Google reviews at 4.8★ and no website) while holding candidates with broken websites in `MANUAL_REVIEW` and safely confining candidates lacking verifiable evidence to `RESEARCH_ONLY`.

**Next Phase Recommendation:**  
Prior to initiating any automated outreach, preserve this verified cohort state, enable operator triage for the 3 `MANUAL_REVIEW` broken website candidates (*The Lost Dene*, *Mother Mary's*, *Fig + Sparrow*), and schedule controlled batch evidence recovery for the next 10 prioritized `RESEARCH_ONLY` candidates under refreshed quota limits.
