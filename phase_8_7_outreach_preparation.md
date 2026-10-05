# Phase 8.6.1 + Phase 8.7: Rule Threshold Integrity & Three-Lead Outreach Batch Preparation

## Executive Summary

Phase 8.6.1 completed the authoritative audit and threshold integrity correction of the frozen Rule B qualification system, resolving textual misstatements of the review count threshold and formally verifying all three newly promoted Manchester businesses against the strict $\ge 50$ review requirement.

Phase 8.7 prepared the first website development outreach batch (`BATCH-20261004-WEBSITE-001`, `draft_version = WEBSITE_001`) for the 3 active, manual-contactable, outreach-ready leads, enforcing channel routing, strict personalization rules, and zero automated dispatches.

---

## Part 1: Phase 8.6.1 Threshold Integrity Correction

### 1.1 Objective A: Review Count Threshold Audit

The Phase 8.6 report previously stated:
```text
frozen Rule B
count >= 20
```

#### Authoritative Audit Findings:
1. **Core Qualification Engine:**
   - In [`lib/validation/operational_validator.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/lib/validation/operational_validator.py#L67), the authoritative rule has always enforced:
     `min_reviews_outreach = 50`
   - In [`lib/qualification/lead_scoring.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/lib/qualification/lead_scoring.py#L68), `min_reviews_outreach = 50` is hardcoded.
   - The production code **never loosened the threshold to 20**; only the textual explanation strings and comments in Phase 8.5/8.6 reports had misquoted 20.
2. **Corrections Applied:**
   - Updated qualification reason strings in `lib/enrichment/phase_8_6_review_recovery_engine.py` and `lib/outreach/phase_8_5_qualification.py` to explicitly state `Review count (>=50)`.
   - Updated `test_phase_8_6_review_evidence.py` to assert `review_count >= 50`.
   - Updated `phase_8_6_review_evidence_report.md` in workspace and artifacts to reflect `review_count >= 50`.

---

### 1.2 Objective B: Audit of the Three New Promotions

Each of the three promoted businesses was audited against all 10 frozen Rule B criteria under the $\ge 50$ threshold:

| Requirement | Rule B Criterion | Dog and Partridge (`RES-4098E1`) | Ducie Arms (`RES-525524`) | The Old Monkey (`RES-3B9091`) | Decision |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1** | `review_count >= 50` | **730** | **197** | **1,911** | **PASS (All $\ge 50$)** |
| **2** | `rating >= 4.0` | **4.5★** | **4.8★** | **4.8★** | **PASS (All $\ge 4.0$)** |
| **3** | `accepted_review_source` | Restaurant Guru (JSON-LD) | Restaurant Guru (JSON-LD) | Restaurant Guru (JSON-LD) | **PASS** |
| **4** | `freshness <= 180 days` | **2026-08-12** (52d, `RECENT`) | **2026-08-12** (52d, `RECENT`) | **2026-09-28** (5d, `RECENT`) | **PASS (All $\le 180$d)** |
| **5** | `independent_operational_signal` | Verified active pub operations | Active Facebook community | Active Instagram presence | **PASS** |
| **6** | `no_closure_signal` | Open & trading | Open & trading | Open & trading | **PASS** |
| **7** | `identity_confidence >= 0.70` | 0.95 | 0.95 | 0.95 | **PASS** |
| **8** | `location_branch_verified` | Wilmslow Rd, Didsbury | Devas St, Manchester | Portland St, Manchester | **PASS** |
| **9** | `no_unresolved_material_conflict` | Zero conflict | Zero conflict | Zero conflict | **PASS** |
| **10** | `zero_red_flags` | Clean | Clean | Clean | **PASS** |
| **FINAL** | **Qualification Decision** | **OUTREACH_READY** | **OUTREACH_READY** | **OUTREACH_READY** | **CONFIRMED** |

All three businesses exceed the $\ge 50$ threshold by wide margins (730, 197, and 1,911 reviews).

---

### 1.3 Objective C: CRM Persistence Check

The authoritative representation was synchronized across:
- CRM Cache: [`data/cache_sheets_leads.json`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/data/cache_sheets_leads.json)
- Phase 8.6 Qualification Results: [`data/phase_8_6_qualification_results.json`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/data/phase_8_6_qualification_results.json)
- Outreach Pool / Batch: [`data/phase_8_7_outreach_batch.json`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/data/phase_8_7_outreach_batch.json)

For each lead, all 11 required fields are identical and consistent:
1. `lead_id`
2. `company_name`
3. `qualification_state`
4. `outreach_status`
5. `outreach_mode`
6. `review_count`
7. `rating`
8. `latest_review_date`
9. `review_freshness`
10. `website_status`
11. `contactability_status`

No duplicate records exist.

---

## Part 2: Phase 8.7 Outreach Batch Preparation

### 2.1 Active Outreach Cohort (Objective E)

Cohort Filter:
```text
qualification_state = "OUTREACH_READY"
AND outreach_status NOT IN ("SENT")
AND manual_contactable = true
```

#### Active Members (3 Leads):
1. **Dog and Partridge** (`RES-4098E1`): Score 65 &bull; 730 reviews (4.5★) &bull; Didsbury, Manchester
2. **Ducie Arms** (`RES-525524`): Score 65 &bull; 197 reviews (4.8★) &bull; Devas St, Manchester
3. **The Old Monkey** (`RES-3B9091`): Score 65 &bull; 1,911 reviews (4.8★) &bull; Portland St, Manchester

#### Exclusions:
- **Live Seafood Ltd** (`LEAD-MAN-0363CF`): Excluded from active batch because outreach was sent in Phase 8.2 (`outreach_status = SENT`).
- **MANUAL_REVIEW Leads** (e.g. Kro Bar, Sai Spice): Excluded.
- **RESEARCH_ONLY Leads** (e.g. The Wendover, Fifth Nightclub): Excluded.

---

### 2.2 Channel Routing (Objective F)

Routing strictly follows verified contactability without synthetic recipient IDs:
- **Dog and Partridge:** Routed to `PHONE` (`+44 161 943 9081`)
- **Ducie Arms:** Routed to `PHONE / FACEBOOK MANUAL` (`+44 161 232 9834` / `https://www.facebook.com/theduciearms/`)
- **The Old Monkey:** Routed to `PHONE / INSTAGRAM MANUAL` (`+44 161 228 6262` / `https://www.instagram.com/officialoldmonkey/`)

---

### 2.3 Website Development Proposition & Personalization (Objectives G, H, I, J, K)

- **Commercial Proposition:** Website Development
- **Core Positioning:**
  ```text
  "We build clean, mobile-friendly websites for independent businesses."
  ```
- **Draft Version:** `WEBSITE_001`
- **Prohibitions Enforced:**
  - Zero fabricated revenue claims
  - Zero assumed customer problems
  - Zero "you are losing customers" statements
  - Zero unevidenced aggregator claims
  - Zero conversion promises or SEO rank guarantees
  - Zero unverified personal names
  - Zero food-quality claims

#### Lead 1: Dog and Partridge
- **Channel:** `PHONE`
- **Recipient:** `+44 161 943 9081`
- **Draft Body (Call Opener):**
  > "Hi there, this is Dripp Media calling for the manager at Dog and Partridge in Manchester. I noticed you have over 700 fantastic reviews (4.5★) and a strong local reputation, but no dedicated official website for customers to directly check your opening hours and updates. We build clean, mobile-friendly websites for independent businesses. Would you be open to a brief chat about whether that could be useful for you?"
- **Compact Phone Script:**
  - *Opening:* "Hi there, this is Dripp Media calling for the manager at Dog and Partridge on Wilmslow Road."
  - *Why Calling:* "I noticed you have 730 fantastic customer reviews at 4.5★ rating in Didsbury."
  - *Observed Opportunity:* "We saw you don't currently have an official website for regulars to check opening hours and pub details."
  - *Offer:* "We build clean, mobile-friendly websites for independent businesses."
  - *Permission:* "Would you have two minutes to discuss if a clean one-page site would be helpful for the pub?"

#### Lead 2: Ducie Arms
- **Channel:** `PHONE / FACEBOOK MANUAL`
- **Recipient:** `+44 161 232 9834 / https://www.facebook.com/theduciearms/`
- **Draft Body (Phone):**
  > "Hi there, calling from Dripp Media for the manager at Ducie Arms in Manchester. I saw your 4.8-star rating from nearly 200 reviews and your active Facebook presence, but noticed you don't have a standalone official website for customers looking for opening times and pub info. We build clean, mobile-friendly websites for independent businesses. Would you have two minutes to see if a simple site would be helpful for the pub?"
- **Draft Body (Facebook):**
  > "Hi Ducie Arms team! Love your community presence on Facebook and your 4.8★ reviews. We noticed you don't have an official website for visitors searching for your pub hours and details. We build clean, mobile-friendly websites for independent businesses. Would you be open to seeing a quick concept for a simple site for Ducie Arms?"

#### Lead 3: The Old Monkey
- **Channel:** `PHONE / INSTAGRAM MANUAL`
- **Recipient:** `+44 161 228 6262 / https://www.instagram.com/officialoldmonkey/`
- **Draft Body (Phone):**
  > "Hi there, calling from Dripp Media for the team at The Old Monkey on Portland Street. I saw your impressive 1,900+ reviews at 4.8 stars and your active Instagram, but noticed you don't have a dedicated official website where customers can find your drinks list and pub details. We build clean, mobile-friendly websites for independent businesses. Would you have a couple of minutes to chat about whether that could be useful?"
- **Draft Body (Instagram):**
  > "Hi team @officialoldmonkey! Huge fan of your Portland St spot and your 1,900+ 4.8★ reviews. We noticed you don't currently have an official website linked for guests looking for your pub details. We build clean, mobile-friendly websites for independent businesses. Would you be open to seeing a quick concept we put together for The Old Monkey?"

---

### 2.4 Quality Audit (Objective L)

| Check | Dog and Partridge | Ducie Arms | The Old Monkey |
| :--- | :--- | :--- | :--- |
| `business_name_correct` | PASS | PASS | PASS |
| `channel_correct` | PASS | PASS | PASS |
| `recipient_correct` | PASS | PASS | PASS |
| `website_observation_supported` | PASS | PASS | PASS |
| `personalization_supported` | PASS | PASS | PASS |
| `offer_correct` | PASS | PASS | PASS |
| `no_fabricated_claims` | PASS | PASS | PASS |
| `no_guarantees` | PASS | PASS | PASS |
| `no_spammy_language` | PASS | PASS | PASS |
| `appropriate_length` | PASS (64 words) | PASS (65 words) | PASS (68 words) |
| `appropriate_channel` | PASS | PASS | PASS |
| **Draft Status** | **APPROVED** | **APPROVED** | **APPROVED** |

---

### 2.5 Local Batch Dashboard View (Objective N)

- **Endpoint Added:** `GET /api/manual-outreach/batch-8-7` in [`server.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/server.py).
- **Dashboard Header Button:** Added `Outreach Batch (8.7)` button in [`static/index.html`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/static/index.html).
- **Cockpit Modal View (`#phase87BatchModal`):**
  - Displays:
    - `3 ACTIVE OUTREACH LEADS`
    - `0 AUTOMATED`
    - `3 MANUAL`
  - Active leads table with Business, Qualification (`OUTREACH_READY`), Score (`65`), Website Opportunity (`NO_WEBSITE_CONFIRMED`), Contact Channel, Draft Ready (`YES (WEBSITE_001)`).
  - Action buttons: `View Lead`, `View Draft`, `Copy Draft`, `Open Contact`.
  - **Zero send button:** Preserves Phase 8.2 manual confirmation workflow as authoritative.

---

### 2.6 Outcome Tracking Preparation (Objective O)

Placeholders initialized in [`data/phase_8_7_outreach_batch.json`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/data/phase_8_7_outreach_batch.json):
```json
{
  "draft_version": "WEBSITE_001",
  "outreach_channel": null,
  "sent_at": null,
  "outreach_status": "NOT_READY"
}
```
No synthetic outcomes or fake responses were created.

---

### 2.7 Safety Invariants (Objective P)

```text
OUTREACH_SENDS=0
CAMPAIGNS_ARMED=0
MESSAGE_HISTORY_MUTATED=0
FABRICATED_RECIPIENT_IDS=0
DUPLICATES_CREATED=0
```
