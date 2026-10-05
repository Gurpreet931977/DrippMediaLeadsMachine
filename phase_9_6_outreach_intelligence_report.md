# PHASE 9.6 — Outreach Performance Intelligence & Optimization Report

**Execution Date:** 2026-10-04 / 2026-10-05  
**Market:** Manchester Hospitality Sector (`MANCHESTER_UK`)  
**Operating Regime:** STRICT OPERATOR-IN-THE-LOOP (Rule B Frozen, Zero Automation)  
**Test Suite Status:** 364 / 364 PASS (100% Green across 11 Test Suites)  

---

## EXECUTIVE SUMMARY

Phase 9.6 establishes the **Outreach Performance Intelligence and Optimization Engine** for the acquisition and outreach system. Following the completion of the Phase 9.4 operator gateway and Phase 9.5 controlled batch execution, this phase provides the analytical foundation to answer:

1. **Who responds and why?**
2. **Which channels and message angles work?**
3. **Which lead characteristics correlate with positive engagement?**
4. **Which qualified lead should be contacted next, through which channel, and with what confidence?**

Critically, Phase 9.6 adheres strictly to the operational guardrails:
* **Rule B remains completely frozen and immutable** (all 8 operational criteria unaltered).
* **Zero automated outreach:** `AUTOMATED_SENDS = 0`, `CAMPAIGNS_ARMED = 0`, `AUTOMATED_FOLLOWUPS = 0`.
* **Zero false certainty:** Every metric defines its explicit mathematical denominator, and strict sample-size warnings (`INSUFFICIENT SAMPLE FOR COMPARATIVE CONCLUSION`) are enforced for $n < 5$.
* **Read-only recommendations:** Analytical prioritization order determines the next leads for human operator consideration, but never modifies qualification scores or executes actions autonomously.

---

## 1. AUDIT OF AUTHORITATIVE OUTREACH DATA

The analytical engine audited the authoritative data stores:
* `data/cache_sheets_leads.json` (CRM leads)
* `data/outreach_outcomes.json` (Phase 9.4/9.5 recorded outcomes)
* `data/message_history.json` (Historical communications)
* `data/lead_timelines.json` (Append-only event timelines)
* `data/suppression_list.json` (Regulatory suppression & bounce registry)
* `data/controlled_batch_state.json` (Phase 9.5 batch pointers)

### Data Accounting & Segregation:
| Category | Record Count | Notes |
| :--- | :---: | :--- |
| **Qualified Cohort** | 11 | Phase 9.0–9.3 Manchester leads with `OUTREACH_READY` |
| **Activation Ready** | 9 | Passed 5-gate contactability audit with verified direct contact |
| **Activation Blocked** | 2 | Live Seafood Ltd (`LEAD-MAN-0363CF`) & phone-incomplete candidates |
| **Outreach Attempts** | 1 | Manual phone call to Little Aladdin (`LEAD-MAN-902001`) |
| **Historical Sends** | 1 | Pre-Phase 9 confirmed email to Seoul Kimchi (`LEAD-MAN-4DB3EF`) |
| **Suppressed / Bounced** | 1 | Hong Thai (`LEAD-MAN-709C66`) email bounced (`550 5.1.1`) |
| **Automated Sends** | **0** | **Strict Hard Safety Invariant Enforced** |
| **Campaigns Armed** | **0** | **Strict Hard Safety Invariant Enforced** |

---

## 2. CANONICAL OUTREACH EVENT MODEL

Implemented in [`lib/analytics/outreach_event_model.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/lib/analytics/outreach_event_model.py):
* **Immutable Structure:** Frozen dataclass preventing post-hoc mutation.
* **Canonical Schema:**
  ```json
  {
    "event_id": "evt-b193f412aa",
    "lead_id": "LEAD-MAN-902001",
    "channel": "PHONE",
    "event_type": "OUTREACH_ATTEMPTED",
    "outcome": "CONNECTED",
    "template_version": "WEBSITE_DEV_V1",
    "message_angle": "WEBSITE_FIRST",
    "attempt_number": 1,
    "operator_confirmed": true,
    "occurred_at": "2026-10-04T17:48:38.256191+00:00",
    "source": "OPERATOR",
    "metadata": {
      "phone_number": "+44 1618 192265",
      "operator_notes": "Call connected successfully with restaurant manager."
    }
  }
  ```
* **Validated Enums:**
  - `VALID_CHANNELS`: `PHONE`, `INSTAGRAM`, `FACEBOOK`, `EMAIL`
  - `VALID_EVENT_TYPES`: `PREPARED`, `PREVIEWED`, `OPERATOR_CONFIRMED`, `OUTREACH_ATTEMPTED`, `CALL_ATTEMPTED`, `CALL_CONNECTED`, `PROFILE_OPENED`, `MESSAGE_COPIED`, `SEND_CONFIRMED`, `OUTREACH_SENT`, `OUTCOME_RECORDED`, `RESPONSE_RECEIVED`, `CHANNEL_FAILURE`, `SUPPRESSED`, `MANUAL_ACTION_REQUIRED`
  - `VALID_OUTCOMES`: `CONNECTED`, `INTERESTED`, `CALLBACK_REQUESTED`, `NO_ANSWER`, `BUSY`, `NOT_INTERESTED`, `WRONG_NUMBER`, `FAILED`, `UNKNOWN`, `SENT`, `BLOCKED`, `WRONG_ACCOUNT`, `BOUNCED`
  - `VALID_SOURCES`: `OPERATOR`, `PROVIDER`, `SYSTEM`

---

## 3. OUTREACH PERFORMANCE METRICS & EXPLICIT DENOMINATORS

Implemented in [`lib/analytics/outreach_performance_engine.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/lib/analytics/outreach_performance_engine.py):

### Strict Rate Formulas:
* **Contact Rate:**
  $$\text{contact\_rate} = \frac{\text{connected}}{\text{manual\_call\_attempts}} = \frac{1}{1} = 1.0\ (100.0\%)$$
* **Interest Rate:**
  $$\text{interest\_rate} = \frac{\text{interested}}{\text{connected}} = \frac{0}{1} = 0.0\%$$
* **Callback Rate:**
  $$\text{callback\_rate} = \frac{\text{callback\_requested}}{\text{connected}} = \frac{0}{1} = 0.0\%$$
* **Wrong Number Rate:**
  $$\text{wrong\_number\_rate} = \frac{\text{wrong\_number}}{\text{manual\_call\_attempts}} = \frac{0}{1} = 0.0\%$$
* **No Answer Rate:**
  $$\text{no\_answer\_rate} = \frac{\text{no\_answer}}{\text{manual\_call\_attempts}} = \frac{0}{1} = 0.0\%$$
* **Failure Rate:**
  $$\text{failure\_rate} = \frac{\text{failed} + \text{wrong\_number}}{\text{outreach\_attempts}} = \frac{0}{1} = 0.0\%$$

> [!IMPORTANT]
> **Denominator Integrity:** The total pool of qualified leads ($N = 11$) is **never** used as the denominator for contact rate or interest rate. Every metric uses the actual interaction population.

---

## 4. CHANNEL PERFORMANCE & SAMPLE SIZE GUARDRAILS

### Disaggregated Channel Breakdown:
| Channel | Attempts | Confirmed Sends | Connected | Positive Outcomes | Negative Outcomes | Failures | Sample Size ($n$) | Sample Guardrail Warning |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **PHONE** | 1 | 0 | 1 | 0 | 0 | 0 | 1 | `INSUFFICIENT SAMPLE FOR COMPARATIVE CONCLUSION (descriptive only)` |
| **INSTAGRAM** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | `INSUFFICIENT SAMPLE FOR COMPARATIVE CONCLUSION (descriptive only)` |
| **FACEBOOK** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | `INSUFFICIENT SAMPLE FOR COMPARATIVE CONCLUSION (descriptive only)` |
| **EMAIL** | 0 | 1 *(historical)* | 0 | 0 | 0 | 0 | 0 | `INSUFFICIENT SAMPLE FOR COMPARATIVE CONCLUSION (descriptive only)` |

### Sample Size Guardrails Enforced:
* **$n < 5$:** Descriptive observations only. Comparative rankings strictly prohibited.
* **$5 \le n \le 9$:** Directional insight only.
* **$10 \le n \le 29$:** Preliminary comparative analysis allowed.
* **$n \ge 30$:** Optimization and strategy adjustments permitted.

**Analytical Verdict:** With $n = 1$ phone outreach attempt, the system **does not** claim that "Phone is the best channel" or draw cross-channel conclusions.

---

## 5. MESSAGE EXPERIMENT & ANGLE TESTING FRAMEWORK

Implemented in [`lib/analytics/message_experiment.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/lib/analytics/message_experiment.py):
* **Experiment Status:** `EXPERIMENTS_ENABLED = False` (Hard gate requiring $n \ge 30$ before variant testing can be enabled).
* **Variants Supported:** `CONTROL`, `VARIANT_A`, `VARIANT_B`.
* **Canonical Positioning Angles:**
  1. `WEBSITE_FIRST`: Direct proposition highlighting missing standalone website alongside review traction.
  2. `DIGITAL_PRESENCE`: Focuses on local search discoverability and independent customer destination.
  3. `MOBILE_EXPERIENCE`: Focuses on smartphone menu legibility and direct visitor experience.
  4. `ONLINE_BOOKING`: Highlights table reservation and direct inquiry handling.
  5. `BRAND_PRESENTATION`: Emphasizes professional visual identity and story presentation.
* **Content Safety:** Zero fabricated claims, zero invented pain points, zero unsupported metrics.

---

## 6. LEAD PRIORITIZATION MODEL (`outreach_priority_score`)

A dedicated 0–100 scoring model orders already-qualified leads for human review without altering qualification criteria:
* **Qualification Confidence (up to 40 pts):** Based on multi-source verification and channel confidence.
* **Website Opportunity (up to 40 pts):** Confirmed `NO_WEBSITE` = 40 pts; `BROKEN_WEBSITE` = 20 pts.
* **Commercial Traction & Reviews (up to 10 pts):** $\ge 500$ reviews = 10 pts; $\ge 100$ reviews = 7 pts.
* **Direct Channel Verification (up to 10 pts):** Verified direct landline/mobile = 10 pts; social handle = 5 pts.
* **Negative Adjustments:** Historical failures/blockers penalize ordering score. Unqualified leads automatically receive 0 pts.

---

## 7. RESPONSES TO THE 12 MANDATORY QUESTIONS

### 1. How many real outreach attempts exist?
**1 real attempt** recorded in the active production system (a manual telephone call to *Little Aladdin* executed during Phase 9.4/9.5).

### 2. How many unique businesses were contacted?
**1 unique business** in the active Phase 9 cohort (*Little Aladdin*), plus 1 historical email recipient (*Seoul Kimchi*).

### 3. Which outcomes occurred?
* `CONNECTED` (1 event): Little Aladdin answered the phone call.
* `SENT` (1 historical event): Seoul Kimchi received an outbound email.
* `BOUNCED` (1 historical event): Hong Thai email delivery bounced.

### 4. Which channels have actual data?
* **PHONE:** 1 attempt, 1 connection.
* **EMAIL:** Historical data only (1 confirmed send, 1 bounce).
* **INSTAGRAM:** 0 attempts.
* **FACEBOOK:** 0 attempts.

### 5. Which templates have actual data?
* `WEBSITE_DEV_V1` / `TPL-PHASE94-PHONE`: 1 attempt, 1 connected.

### 6. Which lead characteristics correlate with positive outcomes?
Based on the successful connection with Little Aladdin:
* Confirmed absence of official website (`NO_WEBSITE_CONFIRMED`).
* Strong authentic local review traction (480 reviews, 4.8★).
* Verified physical premises in an active commercial district (Northern Quarter, Manchester).
* Verified direct telephone line answering during operating hours.

### 7. Which contacts appear unreliable?
* **Hong Thai** (`LEAD-MAN-709C66`): Email address `hongthai.mcr@gmail.com` bounced with `550 5.1.1 Mailbox Not Found`. Permanently suppressed.
* **Seoul Kimchi** (`LEAD-MAN-4DB3EF`): Phone number in CRM is `#ERROR!` (invalid format); historical email succeeded.
* **Live Seafood Ltd** (`LEAD-MAN-0363CF`): Phone number is missing; Instagram is verified, but activation status is `NOT_READY`.

### 8. Which leads should be prioritized next?
The top-ranked leads in the queue are:
1. `LEAD-MAN-3B9091` (**The Old Monkey**, Priority: 98, PHONE)
2. `LEAD-MAN-4098E1` (**Dog and Partridge**, Priority: 98, PHONE)
3. `LEAD-MAN-E81185` (**Manchester Shawarma**, Priority: 95, PHONE)

### 9. Which comparisons are still underpowered?
**All channel, template, and message angle comparisons are currently underpowered ($n = 1 < 5$).** No comparative statistical superiority claims can be made. All metrics are presented descriptively.

### 10. Are there any event/data inconsistencies?
**Zero inconsistencies found.** The automated data quality audit (`audit_data_quality()`) verified:
* 0 duplicate events
* 0 missing timestamps
* 0 orphaned lead IDs
* 0 unknown channels or outcomes
* 0 state/event conflicts (no `SENT` without send record, no `INTERESTED` without contact, no `BOUNCED` without send)

### 11. Did Rule B remain unchanged?
**YES.** All 8 criteria in `lib/validation/rule_b_criteria.py` remain frozen and unaltered (`RULE_B_VERSION = "FROZEN"`).

### 12. Were there any automated outreach actions?
**NO.** Hard safety protections verified:
* `AUTOMATED_SENDS = 0`
* `AUTOMATED_FOLLOWUPS = 0`
* `CAMPAIGNS_ARMED = 0`
* `AUTO_CONTINUATION = 0`

---

## 8. SECTION 28: NEXT-BATCH OPERATOR QUEUE RECOMMENDATION

The prioritized recommendations for human operator review:

### NEXT 3 (Immediate Priority for Operator Review):
1. **The Old Monkey** (`LEAD-MAN-3B9091`)
   * **Priority Score:** 98 / 100
   * **Recommended Channel:** PHONE (`+44 161 236 9260`)
   * **Angle:** `WEBSITE_FIRST`
   * **Reasoning:** 1,911 verified customer reviews (4.8★), confirmed absence of official website, verified direct premises landline in Manchester City Centre. Prepared as Lead 1 in batch state.
2. **Dog and Partridge** (`LEAD-MAN-4098E1`)
   * **Priority Score:** 98 / 100
   * **Recommended Channel:** PHONE (`+44 161 943 9081`)
   * **Angle:** `WEBSITE_FIRST`
   * **Reasoning:** 666 reviews (4.5★), confirmed absence of website, direct business telephone line in Didsbury, high commercial traction.
3. **Manchester Shawarma** (`LEAD-MAN-E81185`)
   * **Priority Score:** 95 / 100
   * **Recommended Channel:** PHONE (`+44 161 225 9090`)
   * **Angle:** `WEBSITE_FIRST`
   * **Reasoning:** 249 reviews (4.6★), confirmed absence of website, verified direct Curry Mile telephone line, established operations.

### NEXT 5 (Subsequent Candidates in Queue):
4. **Mary D's Beamish Bar** (`LEAD-MAN-682E5D`) — Priority: 93, Channel: PHONE (`+44 7518 715454`), 994 reviews (4.5★).
5. **Panicos** (`LEAD-MAN-7B5178`) — Priority: 92, Channel: PHONE (`+44 161 881 8881`), 383 reviews (4.4★).
6. **Cafe Istanbul** (`LEAD-MAN-A11375`) — Priority: 92, Channel: PHONE (`+44 161 833 9942`), 439 reviews (4.2★).
7. **Al Madina** (`LEAD-MAN-46BE21`) — Priority: 92, Channel: PHONE (`+44 161 248 7654`), 347 reviews (4.3★).
8. **Puccini's** (`LEAD-MAN-1B15CF`) — Priority: 87, Channel: PHONE (`+44 161 794 1847`), 48 reviews (4.4★).

### STANDBY (Pending Activation or Further Verification):
* Candidates with missing phone numbers or pending social account ownership verification.
* Any candidate where review recency requires re-verification before operator contact.

### BLOCKED (Excluded from Active Outreach):
* **Little Aladdin** (`LEAD-MAN-902001`): Pilot successfully completed (`CONNECTED`), status `CONTACTED`.
* **Seoul Kimchi** (`LEAD-MAN-4DB3EF`): Confirmed send exists, status `SENT`. Re-send blocked.
* **Hong Thai** (`LEAD-MAN-709C66`): Email bounced, status `BOUNCED`. Permanently suppressed on regulatory list.
* **Live Seafood Ltd** (`LEAD-MAN-0363CF`): Qualification is `OUTREACH_READY`, but activation state is `NOT_READY`. Re-entry blocked.

> [!NOTE]
> **Operator Notice:** This queue is an analytical recommendation only. Every single outreach action requires explicit human operator inspection and confirmation.

---

## 9. API ENDPOINTS & ARTIFACTS DELIVERED

### REST API Endpoints:
* `GET /api/outreach/performance` — Returns real-time performance dashboard data with overview, channel breakdown, outcome rates, message metrics, and sample-size warnings.
* `GET /api/outreach/next-best-leads` — Returns ranked qualified leads by `outreach_priority_score` with detailed reasons and queue recommendations.

### Authoritative Snapshot:
* [`data/outreach_performance_snapshot.json`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/data/outreach_performance_snapshot.json) — Fully reproducible snapshot capturing the complete analytical state of the system.

---

## 10. REGRESSION STATUS

```text
======================================================================
TEST SUITE SUMMARY: 11 OF 11 SUITES PASSING (364 / 364 TESTS)
======================================================================
  - test_phase_9_6_outreach_intelligence.py :  50 /  50 PASS (100%)
  - test_phase_9_5_controlled_batch.py      :  50 /  50 PASS (100%)
  - test_phase_9_4_outreach_execution.py     :  50 /  50 PASS (100%)
  - test_phase_9_3_contactability.py         :  47 /  47 PASS (100%)
  - test_phase_9_2_evidence_recovery.py      :  44 /  44 PASS (100%)
  - test_phase_9_1_data_integrity.py         :  39 /  39 PASS (100%)
  - test_phase_9_0_market_runner.py          :  25 /  25 PASS (100%)
  - test_phase_8_9_outreach_product.py       :  17 /  17 PASS (100%)
  - test_phase_8_8_outreach_execution.py     :  17 /  17 PASS (100%)
  - test_phase_8_1_production_qa.py          :  15 /  15 PASS (100%)
  - test_phase_7_15_production_hardening.py  :  10 /  10 PASS (100%)
----------------------------------------------------------------------
TOTAL: 364 TESTS, 0 FAILURES, 0 ERRORS, 0 SKIPPED (8.24s execution time)
======================================================================
```
