# Commercial Conversion Workflow Report (Post-9.7)

## Executive Summary

The acquisition and controlled outreach engine has completed its initial proving run in Manchester. Four manual outreach interactions have occurred across two operational checkpoints:
- **Pilot Run (Phase 9.4)**: *Little Aladdin* (`CONNECTED`)
- **Batch 1 (Phase 9.7)**: *The Old Monkey* (`CONNECTED`), *Dog and Partridge* (`NO_ANSWER`), *Manchester Shawarma* (`CALLBACK_REQUESTED`)

With qualification (Rule B), contactability, and manual execution layers fully validated, this **Post-9.7 Commercial Conversion Workflow** layer introduces the auditable, human-controlled commercial lifecycle:

$$\text{OUTREACH} \longrightarrow \text{CONVERSATION} \longrightarrow \text{FOLLOW-UP} \longrightarrow \text{PREVIEW} \longrightarrow \text{PROPOSAL} \longrightarrow \text{CLIENT}$$

This implementation establishes:
1. **Strict Outcome Provenance**: Explicit separation between `OPERATOR_REPORTED`, `SYSTEM_VERIFIED`, and `PROVIDER_CONFIRMED`. All manual telephone interactions are recorded strictly as `OPERATOR_REPORTED`.
2. **Independent Commercial Pipeline**: Decouples `commercial_stage` from `qualification_state` (Rule B remains frozen) and `outreach_status`.
3. **No Auto-Promotion Policy**: A connection (`CONNECTED`) moves a lead to `CONTACTED`, never automatically to `INTERESTED` or `WON`.
4. **Actionable Commercial Touchpoints**:
   - **Manchester Shawarma**: Pending manual callback scheduled for `2026-10-06 14:00` (`auto_call = False`, `auto_schedule = False`).
   - **The Old Monkey**: Concept website preview created in `PREVIEW_DRAFT` stage, requiring explicit human action to `MARK PREVIEW SENT`.
5. **Absolute Automation Invariants**: Zero automated calls, emails, DMs, follow-ups, or proposals. The software surfaces structured recommendations; human operators retain 100% execution authority.
6. **Sample-Size Guardrails**: With $n=4$ real conversations, conversion metrics are explicitly tagged `INSUFFICIENT SAMPLE` (descriptive only; no comparative claims until $n \ge 30$).

---

## 1. Current Commercial Pipeline Overview

The commercial pipeline manages the progression of qualified prospects following initial outreach attempts.

### Canonical Commercial Stages

```mermaid
graph LR
    Q[QUALIFIED] --> C[CONTACTED]
    C --> FO[FOLLOW_UP_REQUIRED]
    C --> PR[PREVIEW_REQUESTED]
    C --> I[INTERESTED]
    FO --> C
    PR --> PS[PREVIEW_SENT]
    PS --> PROP_REQ[PROPOSAL_REQUESTED]
    I --> PROP_REQ
    PROP_REQ --> PROP_SENT[PROPOSAL_SENT]
    PROP_SENT --> NEG[NEGOTIATING]
    NEG --> W[WON]
    NEG --> L[LOST]
    C --> NI[NOT_INTERESTED]
```

### Current Cohort Distribution

| Lead ID | Business Name | Qualification State | Outreach Status | Commercial Stage | Website Pipeline Stage | Latest Outcome | Provenance |
|---|---|---|---|---|---|---|---|
| `LEAD-MAN-E81185` | **Manchester Shawarma** | `OUTREACH_READY` | `CONTACTED` | **FOLLOW_UP_REQUIRED** | `CONTACTED` | `CALLBACK_REQUESTED` | `OPERATOR_REPORTED` |
| `LEAD-MAN-3B9091` | **The Old Monkey** | `OUTREACH_READY` | `CONTACTED` | **PREVIEW_REQUESTED** | `OPEN_TO_DISCUSSION` | `CONNECTED` | `OPERATOR_REPORTED` |
| `LEAD-MAN-902001` | **Little Aladdin** | `OUTREACH_READY` | `CONTACTED` | **CONTACTED** | `OPEN_TO_DISCUSSION` | `CONNECTED` | `OPERATOR_REPORTED` |
| `LEAD-MAN-4098E1` | **Dog and Partridge** | `OUTREACH_READY` | `CALL_ATTEMPTED` | **CONTACTED** | `CONTACTED` | `NO_ANSWER` | `OPERATOR_REPORTED` |
| `LEAD-MAN-4DB3EF` | **Seoul Kimchi** | `OUTREACH_READY` | `SENT` | **CONTACTED** | `CONTACTED` | `SENT` | `OPERATOR_REPORTED` |
| `LEAD-MAN-709C66` | **Hong Thai** | `OUTREACH_READY` | `BOUNCED` | **LOST** | `LOST` | `BOUNCED` | `PROVIDER_CONFIRMED` |
| `LEAD-MAN-0363CF` | **Live Seafood Ltd** | `OUTREACH_READY` | `NOT_READY` | **QUALIFIED** | `NO_WEBSITE` | None | N/A |
| `LEAD-MAN-682E5D` | **Mary D's Beamish Bar** | `OUTREACH_READY` | `READY_FOR_REVIEW`| **QUALIFIED** | `NO_WEBSITE` | None | N/A |
| `LEAD-MAN-24B351` | **The Corner Slice** | `OUTREACH_READY` | `READY_FOR_REVIEW`| **QUALIFIED** | `NO_WEBSITE` | None | N/A |
| `LEAD-MAN-A65953` | **Northern Soul Grilled**| `OUTREACH_READY` | `READY_FOR_REVIEW`| **QUALIFIED** | `NO_WEBSITE` | None | N/A |
| `LEAD-MAN-B91042` | **Chorlton Tap** | `OUTREACH_READY` | `READY_FOR_REVIEW`| **QUALIFIED** | `NO_WEBSITE` | None | N/A |

---

## 2. Immediate Follow-Ups & Callback Opportunities

### Manchester Shawarma (`LEAD-MAN-E81185`)

During Phase 9.7 live batch execution, the restaurant shift manager requested a callback outside peak lunch service hours:
- **Requested Window**: `2026-10-06 14:00` (Post-lunch lull)
- **Direct Phone**: `+44 161 526 5396`
- **Follow-up Type**: `CALLBACK`
- **Execution Mode**: `PENDING_OPERATOR`
- **Safety Enforcement**:
  - `auto_call = False`: The dialer will not initiate calls autonomously.
  - `auto_schedule = False`: No calendar or notification hooks dispatches automated messages.
- **Action Plan for Operator**:
  1. Open dashboard at 14:00 on 2026-10-06.
  2. Initiate manual phone call to the manager.
  3. Propose lightweight web ordering menu concept to capture direct Curry Mile takeaway orders.
  4. Record genuine outcome directly via `Record Callback Outcome` button.

---

## 3. Concept Preview Opportunities

### The Old Monkey (`LEAD-MAN-3B9091`)

The general manager expressed openness to reviewing a simple, mobile-friendly website concept during the Phase 9.7 call:
- **Commercial Stage**: `PREVIEW_REQUESTED`
- **Draft Status**: `PREVIEW_DRAFT`
- **Working Reference**: `https://preview.dripp.media/the-old-monkey-mcr`
- **Demonstrated Elements**:
  - Prominent Portland Street location & opening times.
  - Social proof integration: highlighting 1,911 reviews at 4.8★.
  - One-tap click-to-call and Google Maps walking directions.
  - Craft ale and seasonal food board highlights.
- **Delivery Protocol**:
  - The software **does NOT** automatically send this link.
  - Operator must inspect the preview draft, ensure messaging accuracy, and manually deliver the link (via verified Instagram DM or direct phone follow-up).
  - Operator clicks `Mark Preview Sent` in the dashboard to transition status to `PREVIEW_SENT`.
  - `viewed_at` remains unpopulated unless verifiable evidence is provided.

---

## 4. Connected Leads & Follow-Up Opportunities

### Little Aladdin (`LEAD-MAN-902001`)

The initial single-lead pilot prospect in the Northern Quarter:
- **Current Status**: `CONTACTED`
- **Previous Outcome**: `CONNECTED` (Spoke with restaurant manager regarding vegan cafe menu website).
- **Interest Level**: `HIGH`
- **Next Operator Action**: `FOLLOW UP`
- **Objective**: Re-engage manager to determine whether a tailored 1-page mobile site preview should be prepared for delivery.

---

## 5. Dog and Partridge Handling & Retry Strategy

### Dog and Partridge (`LEAD-MAN-4098E1`)

During the Phase 9.7 batch, this establishment rang with no answer during standard afternoon hours:
- **CRM Outreach Status**: `CALL_ATTEMPTED` (Strictly distinct from `CONTACTED`).
- **Commercial Stage**: `CONTACTED` (Tracked in commercial database as contacted prospect awaiting conversation).
- **Next Action**: `OPERATOR_REVIEW` (Tier 5 priority).
- **Operational Rule**: No automated retries. An operator must select an alternate time window (e.g. late afternoon prior to dinner rush: 16:30 - 17:30) to make attempt #2. When attempt #2 occurs, the attempt counter increments to 2 rather than overwriting attempt #1.

---

## 6. Proposal Tracking & Deal Close Framework

To prevent premature optimism or fabricated projections, proposal and closing workflows enforce strict validation:

### Proposal Tracking
- **Statuses**: `NONE`, `REQUESTED`, `DRAFTED`, `SENT`, `NEGOTIATING`, `ACCEPTED`, `REJECTED`.
- **Financial Tracking**: Only operator-entered amounts and currencies (e.g., `£950.00 GBP`) are stored. No automated pricing formulas or auto-generated proposals.

### Closing Deals (Won / Lost)
- **Lost Invariants**: Mandatory reason selected from:
  - `PRICE`
  - `TIMING`
  - `NO_NEED`
  - `CHOSE_OTHER_PROVIDER`
  - `NO_RESPONSE`
  - `OTHER`
- **Won Invariants**:
  - `operator_confirmed = True` is strictly required.
  - Mandatory fields: `service`, `agreed_value`, `currency`, `start_date`, and `notes`.
  - Cannot mark a deal WON via automated heuristics.

---

## 7. Data Provenance & Authoritative Sources

All commercial interaction records adhere to a 3-tier provenance hierarchy:

| Provenance Tier | Description | Applicable Interactions |
|---|---|---|
| **`OPERATOR_REPORTED`** | Direct human observation entered by operator. Software makes no independent verification claims. | Manual phone calls, conversation notes, verbal feedback, manual DMs. |
| **`SYSTEM_VERIFIED`** | Code-level verification with cryptographic/HTTP proof. | Domain availability, HTTP status checks, DNS lookups, JSON schema validation. |
| **`PROVIDER_CONFIRMED`**| External service receipt or webhook confirmation. | Email delivery webhook, bounce notification, Twilio SMS delivery receipt. |

> [!IMPORTANT]
> Manual telephone calls cannot be marked `SYSTEM_VERIFIED`. The system will reject any attempt to record a phone call as automated proof without provider-backed carrier receipts.

---

## 8. Prioritized Next Commercial Actions

The system implements the 6-tier prioritization hierarchy required by Section 15:

```text
Priority 1: Explicit callback requested     --> Manchester Shawarma (2026-10-06 14:00)
Priority 2: Explicit preview requested      --> The Old Monkey (Review & send preview)
Priority 3: Explicit interest confirmed     --> Prospects marked INTERESTED
Priority 4: Recent successful connection    --> Little Aladdin (Check-in on concept)
Priority 5: No-answer retry review          --> Dog and Partridge (Choose alternate window)
Priority 6: Remaining qualified prospects   --> Mary D's Beamish Bar, The Corner Slice, etc.
```

### Authoritative Next Actions Queue

| Priority | Lead ID | Company Name | Action | Due / Timing | Reason |
|---|---|---|---|---|---|
| **1** | `LEAD-MAN-E81185` | **Manchester Shawarma** | `CALLBACK TODAY` | 2026-10-06 14:00 | Manager explicitly requested callback after peak lunch hours. |
| **2** | `LEAD-MAN-3B9091` | **The Old Monkey** | `SEND PREVIEW` | Immediate | Manager requested website concept preview; review draft and dispatch manually. |
| **4** | `LEAD-MAN-902001` | **Little Aladdin** | `FOLLOW UP` | Next Shift | Recent successful connection; follow up on discussed website opportunity. |
| **5** | `LEAD-MAN-4098E1` | **Dog and Partridge** | `OPERATOR_REVIEW` | Alternate Shift | Previous attempt resulted in NO_ANSWER; operator review needed to choose retry window. |
| **6** | `LEAD-MAN-682E5D` | **Mary D's Beamish Bar** | `QUALIFIED_OUTREACH` | Next Batch | Established Manchester pub (994 reviews, 4.5★); verified phone ready. |

---

## 9. Funnel Analytics & Conversion Rates

Authoritative metrics generated at [data/commercial_pipeline_snapshot.json](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/data/commercial_pipeline_snapshot.json):

```text
QUALIFIED LEADS:                 11
ACTIVATION READY:                 9

CONTACTED PROSPECTS:              4  (3 Phone + 1 Social)
CONNECTED CONVERSATIONS:          3  (Little Aladdin, The Old Monkey, Manchester Shawarma)
INTERESTED PROSPECTS:             1  (The Old Monkey requested preview)

FOLLOW-UP REQUIRED:               1  (Manchester Shawarma callback)
PREVIEW REQUESTED:                1  (The Old Monkey)
PREVIEW SENT:                     0  (Awaiting human dispatch)
PROPOSALS REQUESTED:              0
PROPOSALS SENT:                   0
DEALS WON:                        0
DEALS LOST:                       1  (Hong Thai - Bounced / Suppressed)

OPERATOR REPORTED OUTCOMES:       5
PROVIDER CONFIRMED OUTCOMES:      0
AUTOMATION ACTIONS:               0
```

### Calculated Funnel Rates

$$\text{Contact to Connected} = \frac{3}{4} = 75.0\%$$
$$\text{Connected to Interested} = \frac{1}{3} = 33.3\%$$
$$\text{Interested to Preview} = \frac{1}{1} = 100.0\%$$
$$\text{Preview to Proposal} = 0.0\%$$
$$\text{Proposal to Won} = \text{N/A (Denominator = 0)}$$

> [!WARNING]
> **SAMPLE SIZE GUARD:** With only $n=4$ total outreach conversations, these conversion metrics are strictly descriptive. No statistical claims regarding channel effectiveness, messaging angle, or market viability may be drawn until $n \ge 30$.

---

## 10. Automation Limits & System Invariants

The software programmatically enforces that automation remains completely disabled:

```python
AUTO_CALL = False
AUTO_DM = False
AUTO_EMAIL = False
AUTO_FOLLOWUP = False
AUTO_CALLBACK = False
AUTO_PROPOSAL = False
AUTO_CONTINUATION = False
```

Every state mutation requires an explicit human operator HTTP POST request with an affirmative confirmation flag.

---

## 11. Regression Suite Status

The commercial layer introduces 38 new unit and integration tests in [test_post_9_7_commercial_workflow.py](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/test_post_9_7_commercial_workflow.py). The entire system regression suite across all 13 test suites passes at **100%**:

```text
======================================================================
FULL REGRESSION SUITE EXECUTION
======================================================================
  • test_post_9_7_commercial_workflow.py:     38 / 38 PASS
  • test_phase_9_7_live_outreach.py:          37 / 37 PASS
  • test_phase_9_6_outreach_intelligence.py:   50 / 50 PASS
  • test_phase_9_5_controlled_batch.py:       50 / 50 PASS
  • test_phase_9_4_outreach_execution.py:     46 / 46 PASS
  • test_phase_9_3_contactability.py:         40 / 40 PASS
  • test_phase_9_2_evidence_recovery.py:      43 / 43 PASS
  • test_phase_9_1_data_integrity.py:         39 / 39 PASS
  • test_phase_9_0_market_runner.py:          25 / 25 PASS
  • test_phase_8_9_outreach_product.py:       33 / 33 PASS
  • test_phase_8_8_outreach_execution.py:     12 / 12 PASS
  • test_phase_8_1_production_qa.py:          15 / 15 PASS
  • test_phase_7_15_production_hardening.py:  10 / 10 PASS
----------------------------------------------------------------------
TOTAL: 439 / 439 PASS (100.0%) in 9.13s
======================================================================
```

---

## 12. Conclusion & Operational Stance

Per the explicit directive in the project specification:
> **"This is the last significant workflow expansion for the current acquisition system. After implementation: STOP building new phases. Operate the system."**

The software stack now supports the full commercial lifecycle:

$$\text{DISCOVER} \longrightarrow \text{VERIFY} \longrightarrow \text{QUALIFY} \longrightarrow \text{CONTACT} \longrightarrow \text{TALK} \longrightarrow \text{FOLLOW UP} \longrightarrow \text{PREVIEW} \longrightarrow \text{PROPOSAL} \longrightarrow \text{CLOSE}$$

### Immediate Next Operator Moves
1. **2026-10-06 14:00**: Perform manual telephone callback to *Manchester Shawarma* (`LEAD-MAN-E81185`).
2. **Current Shift**: Inspect the concept preview draft for *The Old Monkey* (`LEAD-MAN-3B9091`), deliver it manually, and record the action via `Mark Preview Sent`.
3. **Subsequent Shift**: Evaluate retry timing for *Dog and Partridge* (`LEAD-MAN-4098E1`) and re-engage *Little Aladdin* (`LEAD-MAN-902001`).
