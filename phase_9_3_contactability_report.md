# PHASE 9.3 — Qualified Lead Contactability + Activation Engine Report

**Execution Run ID:** `ACTIVATE-MAN-20261004-C37937`  
**Market:** `MANCHESTER_UK`  
**Execution Timestamp:** `2026-10-04T16:34:20Z`  
**Target Cohort:** All Currently Qualified (`OUTREACH_READY`) Manchester Leads  
**Machine Output Artifact:** `data/phase_9_3_contactability_run.json`  

---

## Executive Summary

Phase 9.3 resolves the critical activation bottleneck following Rule B qualification:

$$\text{QUALIFIED LEAD} \longrightarrow \text{CONTACTABILITY} \longrightarrow \text{CHANNEL VERIFICATION} \longrightarrow \text{OUTREACH-ACTIONABLE}$$

Rather than scaling broad discovery across new markets, Phase 9.3 audited the canonical definition of Rule B, audited operational verification provenance, deployed the **Contactability Enrichment Engine** ([lib/enrichment/contactability_enrichment.py](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/lib/enrichment/contactability_enrichment.py)), enforced branch-safe contact attribution, established the **Activation Queue API** (`GET /api/outreach/activation-queue`), and upgraded the Operator Dashboard UI — all while enforcing absolute zero-send invariants.

### Key Headline Results
* **Canonical Rule B Criteria Count:** Exactly **8 criteria** canonically specified in [lib/validation/rule_b_criteria.py](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/lib/validation/rule_b_criteria.py) (`RULE_B_VERSION = "FROZEN"`).
* **Input Qualified Leads Evaluated:** **11**
* **Channels Verified:**
  * **Phone Verified:** 9 (UK landline 0161 / mobile 07 format, verified business premises)
  * **Instagram Verified:** 6 (Identity token matched, branch-safe, non-post URLs)
  * **Facebook Verified:** 6 (Clean business pages, non-video URLs)
  * **Email Verified:** 2 (Strictly genuine discovered emails; 2 confirmed MX valid)
* **Canonical Contactability Breakdown:**
  * **Multi-Channel Contactable:** 9 ($\ge 2$ independently verified usable channels)
  * **Manual Contactable:** 2 (Single verified phone channel)
  * **Automated Contactable:** 0
  * **Not Contactable:** 0
* **Activation Readiness:**
  * **Activation Ready:** **9** (Sufficiently verified with usable, policy-compliant contact paths)
  * **Activation Blocked:** **2** (*Seoul Kimchi* blocked due to confirmed previous send; *Hong Thai* blocked due to previous bounce)
* **Recommended Routing Channels:**
  * **Phone:** 9 (Prioritized for high-traction local independent hospitality)
  * **Instagram:** 1 (*Live Seafood Ltd* — phone absent, Instagram verified)
  * **Facebook:** 0
  * **Email:** 0
* **Outreach Sends:** **0** (Hard ceiling: 0)
* **Production Send Adapter Calls:** **0** (Hard ceiling: 0)
* **Campaigns Armed:** **0** (Hard ceiling: 0)
* **CRM Historical State Modified:** **0** (`LEAD-MAN-0363CF` Live Seafood Ltd intact)

---

## 1. Rule B Reporting Audit & Canonicalization

### Audit Findings
Prior qualification reports informally described Rule B as either "8 criteria" or "10 conditions" due to differing granularities (e.g., whether rating and review count were reported as separate criteria, or whether identity confidence and location verification were separate).

### Canonical Specification Implemented
To guarantee schema consistency and mathematical integrity across all modules, [lib/validation/rule_b_criteria.py](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/lib/validation/rule_b_criteria.py) now defines the single, immutable source of truth:

```python
RULE_B_VERSION: str = "FROZEN"

RULE_B_CRITERIA: Tuple[Dict[str, Any], ...] = (
    {"id": "RULE_B_01_REVIEW_VOLUME", "name": "Review Volume", "threshold": 50},
    {"id": "RULE_B_02_MINIMUM_RATING", "name": "Minimum Rating", "threshold": 4.0},
    {"id": "RULE_B_03_ACCEPTED_SOURCE", "name": "Accepted Review Source", "accepted": ("google_maps", "google")},
    {"id": "RULE_B_04_REVIEW_RECENCY", "name": "Review Recency", "max_age_days": 180},
    {"id": "RULE_B_05_OPERATIONAL_SIGNAL", "name": "Independent Operational Signal", "status": "VERIFIED_ACTIVE"},
    {"id": "RULE_B_06_IDENTITY_LOCATION", "name": "Identity & Location Verified", "min_conf": 0.70},
    {"id": "RULE_B_07_NO_REVIEW_CONFLICT", "name": "No Unresolved Review Conflict", "forbidden": ("CONFLICTING",)},
    {"id": "RULE_B_08_NO_CLOSURE_RED_FLAGS", "name": "Zero Closure / Red Flags", "forbidden": ("CLOSED",)},
)
```

* **Canonical Count:** `len(RULE_B_CRITERIA) == 8`
* **Regression Test:** Proven by `test_canonical_rule_b_criteria_count_is_eight` and `test_report_criteria_count_matches_canonical` in `test_phase_9_3_contactability.py`.

---

## 2. Operational Verification Audit (Little Aladdin)

The Phase 9.2 qualification of **Little Aladdin** (72 High Street, Northern Quarter, Manchester M4 1ES) was audited to verify that `VERIFIED_ACTIVE` was produced strictly through independent multi-family corroboration.

### Evidence Breakdown:
1. **Source Family 1 (`google`):**
   * Source: Google Maps Reviews Places CID
   * Observed Metric: 480 reviews, 4.8★ aggregate rating
   * Review Date: `2026-08-20` (45 days old, strictly $\le 180$ days)
   * Confidence: 0.95
2. **Source Family 2 (`direct_telecom`):**
   * Source: Verified fixed geographic telecom (+44 1618 192265)
   * Area Code: 0161 (Manchester central exchange)
   * Confidence: 0.95

### Policy Rules Enforced:
* **Review activity alone $\longrightarrow$ `WEAK_SIGNAL`** (Tested & proven in `test_review_activity_alone_is_not_verified_active`).
* **Telecom alone $\longrightarrow$ `UNKNOWN`** (Tested & proven in `test_telecom_phone_alone_is_not_verified_active`).
* **OSM node alone $\longrightarrow$ `UNKNOWN`** (Never `VERIFIED_ACTIVE`).
* **Two genuinely independent valid families $\longrightarrow$ `VERIFIED_ACTIVE`** (Tested & proven in `test_two_independent_families_eligible_for_verified_active`).
* **Full Provenance:** Every evidence item preserves `source_family`, `source`, `source_url`, `evidence_type`, `observed_at`, `confidence`, `raw_signal`, and `normalized_result`.

---

## 3. Section A: Qualified Cohort Audit

The Phase 9.3 target cohort comprises all 11 currently qualified (`OUTREACH_READY`) Manchester leads across historical CRM caches and Phase 9.2 promotions:

| # | Lead ID | Company Name | Street Address / District | Qualification State | Outreach Status | Website Status |
| :-: | :--- | :--- | :--- | :---: | :---: | :---: |
| **1** | `Little Aladdin` | Little Aladdin | 72 High Street, Northern Quarter | `OUTREACH_READY` | `NOT_READY` | `NO_WEBSITE` |
| **2** | `LEAD-MAN-0363CF` | Live Seafood Ltd | 375 Ashton New Rd, Bradford | `OUTREACH_READY` | `NOT_READY` | `NO_WEBSITE` |
| **3** | `LEAD-MAN-4098E1` | Dog and Partridge | 665-667 Wilmslow Rd, Didsbury | `OUTREACH_READY` | `NOT_READY` | `NO_WEBSITE` |
| **4** | `LEAD-MAN-525524` | Ducie Arms | 152 Devas St, University Quarter | `OUTREACH_READY` | `NOT_READY` | `NO_WEBSITE` |
| **5** | `LEAD-MAN-3B9091` | The Old Monkey | 90 Portland St, City Centre | `OUTREACH_READY` | `NOT_READY` | `NO_WEBSITE` |
| **6** | `LEAD-MAN-682E5D` | Mary D's Beamish Bar | 13 Grey Mare Ln, Beswick | `OUTREACH_READY` | `READY_FOR_REVIEW` | `NO_WEBSITE` |
| **7** | `LEAD-MAN-14A2D3` | Mala | 8 Tariff St, Northern Quarter | `OUTREACH_READY` | `FAILED` | `NO_WEBSITE` |
| **8** | `LEAD-MAN-E81185` | Manchester Shawarma | 114 Wilmslow Rd, Rusholme | `OUTREACH_READY` | `NOT_READY` | `NO_WEBSITE` |
| **9** | `LEAD-MAN-44F10A` | 99 Reasons | 126 Deansgate, City Centre | `OUTREACH_READY` | `NOT_READY` | `NO_WEBSITE` |
| **10**| `LEAD-MAN-4DB3EF` | Seoul Kimchi | 275 Upper Brook St, Victoria Park | `OUTREACH_READY` | `SENT` | `NO_WEBSITE` |
| **11**| `LEAD-MAN-709C66` | Hong Thai | Arndale Market, City Centre | `OUTREACH_READY` | `BOUNCED` | `NO_WEBSITE` |

---

## 4. Section B: Channel-by-Channel Verification Results

Every lead was evaluated individually across all 4 supported channels with branch-safety filters:

| Lead ID | Company Name | Phone Status | Instagram Status | Facebook Status | Email Status | Contactability Status |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| `Little Aladdin` | Little Aladdin | **VERIFIED** (+44 1618 192265) | MISSING | MISSING | MISSING | `MANUAL_CONTACTABLE` |
| `LEAD-MAN-0363CF` | Live Seafood Ltd | MISSING | **VERIFIED** (@liveseafoodmcr) | **VERIFIED** (Page) | MISSING | `MULTI_CHANNEL_CONTACTABLE` |
| `LEAD-MAN-4098E1` | Dog and Partridge | **VERIFIED** (+44 161 943 9081) | MISSING | MISSING | MISSING | `MANUAL_CONTACTABLE` |
| `LEAD-MAN-525524` | Ducie Arms | **VERIFIED** (+44 161 232 9834) | MISSING | **VERIFIED** (Page) | MISSING | `MULTI_CHANNEL_CONTACTABLE` |
| `LEAD-MAN-3B9091` | The Old Monkey | **VERIFIED** (+44 161 228 6262) | **VERIFIED** (Profile) | MISSING | MISSING | `MULTI_CHANNEL_CONTACTABLE` |
| `LEAD-MAN-682E5D` | Mary D's Beamish Bar | **VERIFIED** (+44 7518 715454) | **VERIFIED** (@marydsbar) | MISSING | MISSING | `MULTI_CHANNEL_CONTACTABLE` |
| `LEAD-MAN-14A2D3` | Mala | **VERIFIED** (+44 7479 592107) | **VERIFIED** (Profile) | **VERIFIED** (Page) | **VERIFIED** (MX Valid) | `MULTI_CHANNEL_CONTACTABLE` |
| `LEAD-MAN-E81185` | Manchester Shawarma | **VERIFIED** (+44 161 526 5396) | **VERIFIED** (Profile) | MISSING | MISSING | `MULTI_CHANNEL_CONTACTABLE` |
| `LEAD-MAN-44F10A` | 99 Reasons | **VERIFIED** (+44 161 820 7726) | **VERIFIED** (Profile) | **VERIFIED** (Page) | MISSING | `MULTI_CHANNEL_CONTACTABLE` |
| `LEAD-MAN-4DB3EF` | Seoul Kimchi | **INVALID** (`#ERROR!`) | **VERIFIED** (Profile) | **VERIFIED** (Page) | **VERIFIED** (MX Valid) | `MULTI_CHANNEL_CONTACTABLE` |
| `LEAD-MAN-709C66` | Hong Thai | **VERIFIED** (+44 7796 046556) | **VERIFIED** (Profile) | **VERIFIED** (Page) | **VERIFIED** (MX Valid) | `MULTI_CHANNEL_CONTACTABLE` |

### Verification Rule Highlights:
* **Branch Protection:** Instagram handles referencing other cities (e.g. `@rudyspizza_london`) and generic Aladdin handles lacking distinctive qualifiers (`@aladdin_grill`) are strictly rejected.
* **Format & Error Scrubbing:** Spreadsheet `#ERROR!` on *Seoul Kimchi* is flagged as `INVALID`, preventing garbage payloads.
* **Email & MX Integrity:** Zero synthesized/guessed emails (`info@`, `hello@`) were created. Only genuine discovered emails with validated MX records passed.

---

## 5. Section C & D: Activation Profiles, Routing & Blockers

`activation_ready` is evaluated strictly: it indicates that the lead is verified, has an actionable channel path, and has zero policy blockers. **It does NOT authorize automated message transmission.**

| Lead ID | Company Name | Recommended Channel | Fallback Channels | Activation Ready | Reason / Blocker Detail |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Little Aladdin** | Little Aladdin | **PHONE** | None | **READY** | Verified direct local landline (`+44 1618 192265`). Single independent venue. |
| **LEAD-MAN-0363CF** | Live Seafood Ltd | **INSTAGRAM** | `FACEBOOK` | **READY** | Phone absent; verified active business Instagram profile. |
| **LEAD-MAN-4098E1** | Dog and Partridge | **PHONE** | None | **READY** | Verified direct Manchester landline (`+44 161 943 9081`). |
| **LEAD-MAN-525524** | Ducie Arms | **PHONE** | `FACEBOOK` | **READY** | Verified direct Manchester landline (`+44 161 232 9834`). |
| **LEAD-MAN-3B9091** | The Old Monkey | **PHONE** | `INSTAGRAM` | **READY** | Verified direct Manchester landline (`+44 161 228 6262`). |
| **LEAD-MAN-682E5D** | Mary D's Beamish Bar | **PHONE** | `INSTAGRAM` | **READY** | Verified direct mobile (`+44 7518 715454`) + active Instagram. |
| **LEAD-MAN-14A2D3** | Mala | **PHONE** | `EMAIL`, `INSTAGRAM`, `FACEBOOK` | **READY** | 4 verified channels. Phone prioritized for direct venue contact. |
| **LEAD-MAN-E81185** | Manchester Shawarma | **PHONE** | `INSTAGRAM` | **READY** | Verified direct Manchester landline (`+44 161 526 5396`). |
| **LEAD-MAN-44F10A** | 99 Reasons | **PHONE** | `INSTAGRAM`, `FACEBOOK` | **READY** | Verified direct Manchester landline (`+44 161 820 7726`). |
| **LEAD-MAN-4DB3EF** | Seoul Kimchi | **NONE** | None | **BLOCKED** | **`ALREADY_SENT_CONFIRMED`:** Lead previously received outreach in production. Channel routing prohibited. |
| **LEAD-MAN-709C66** | Hong Thai | **PHONE** | `EMAIL`, `INSTAGRAM`, `FACEBOOK` | **BLOCKED** | **`PREVIOUS_BOUNCE`:** Previous outreach attempt bounced; requires operator intervention before reactivation. |

---

## 6. Section E: Safety & Anti-Outreach Invariants

All 6 safety and integrity invariants evaluated to **PASS**:

| Invariant | Status | Verification Evidence |
| :--- | :---: | :--- |
| `ZERO_SENDS` | **PASS** | `outreach_sends == 0`. Zero email, SMS, DM, or phone transmissions occurred. |
| `ZERO_ARMED_CAMPAIGNS` | **PASS** | `campaigns_armed == 0`. Zero campaigns scheduled, approved, or armed. |
| `ZERO_PRODUCTION_SENDER_CALLS`| **PASS** | No production send adapters, Meta Graph APIs, or SMTP sessions invoked. |
| `RULE_B_UNCHANGED` | **PASS** | Frozen Rule B criteria untouched; canonical count locked at 8. |
| `HISTORICAL_OUTREACH_PRESERVED`| **PASS** | `LEAD-MAN-0363CF` (*Live Seafood Ltd*) intact in `NOT_READY`, `actual_send_confirmed = False`. |
| `BRANCH_IDENTITY_PROTECTED` | **PASS** | Cross-branch social handles and corporate national lines isolated. |

---

## 7. Section F: History & CRM Invariance

* **Live Seafood Ltd (`LEAD-MAN-0363CF`):**
  * `qualification_state`: `OUTREACH_READY` (Preserved)
  * `outreach_status`: `NOT_READY` (Preserved)
  * `actual_send_confirmed`: `False` (Preserved)
  * `outreach_mode`: `MANUAL` (Preserved)
* **Seoul Kimchi (`LEAD-MAN-4DB3EF`):**
  * Retains `SENT` outreach status with confirmed send timestamp. Prevented from entering active outreach routing.
* **Hong Thai (`LEAD-MAN-709C66`):**
  * Retains `BOUNCED` outreach status. Prevented from automated reactivation.
* **CRM Writes:** 0 database or spreadsheet writes performed in this verification phase.

---

## 8. Regression Test Execution Summary

Across all 6 test suites, **173 out of 173 tests passed with a 100% pass rate**:

```text
================================================================================
COMPLETE REGRESSION EXECUTION SUMMARY
================================================================================
1. test_phase_9_3_contactability.py:          39 / 39  PASS  (100%)
   - Category 1: Rule B Reporting Canonical:   5 / 5   PASS
   - Category 2: Operational Independence:     4 / 4   PASS
   - Category 3: Contactability Verification: 13 / 13  PASS
   - Category 4: Branch-Safe Contact Match:    4 / 4   PASS
   - Category 5: Activation & Channel Routing: 6 / 6   PASS
   - Category 6: CRM History & Live Seafood:   3 / 3   PASS
   - Category 7: Safety Ceilings & Zero Sends: 4 / 4   PASS

2. test_phase_9_2_evidence_recovery.py:       45 / 45  PASS  (100%)
3. test_phase_9_1_data_integrity.py:          39 / 39  PASS  (100%)
4. test_phase_9_0_market_runner.py:           25 / 25  PASS  (100%)
5. test_phase_8_1_production_qa.py:           15 / 15  PASS  (100%)
6. test_phase_7_15_production_hardening.py:   10 / 10  PASS  (100%)
--------------------------------------------------------------------------------
TOTAL TEST COVERAGE:                         173 / 173 PASS  (100.0%)
================================================================================
```

---

## 9. Next Steps

1. **Operator Review:** Operators can now inspect the 9 activation-ready leads via the newly created dashboard section or `GET /api/outreach/activation-queue`.
2. **Channel Dispatch Preparedness:** With verified phone numbers for 9 qualified venues (including *Little Aladdin*, *Dog and Partridge*, *The Old Monkey*, and *Ducie Arms*) and verified Instagram for *Live Seafood Ltd*, the Manchester qualified cohort is fully verified and ready for controlled operator review.
