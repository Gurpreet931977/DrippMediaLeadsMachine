# Phase 8.4 — Contact Discovery V2 & Manual Review Conversion Report
**Generated:** 2026-10-04 | **Status:** PASS

---

## 1. Executive Summary & Cohort Definition

Phase 8.4 executed targeted contact discovery and manual review conversion on the authoritative **28 `NO_WEBSITE_CONFIRMED`** Manchester hospitality businesses from Phase 8.0 without running new discovery batches or weakening frozen qualification criteria (Rule B).

* **Target Cohort:** 28 `NO_WEBSITE_CONFIRMED` businesses.
* **Targeted Unreachable Queue:** **24** businesses previously classified as `manual_contactable = false AND automated_sendable = false`.
* **Previously Contactable Leads:** **4** businesses (Live Seafood Ltd, Ducie Arms, Kro Bar, Glamorous Chinese Restaurant).
* **Manual Review Conversion Pool:** **4** businesses from the no-website cohort (Ducie Arms, Kro Bar, Glamorous Chinese Restaurant, Crown & Anchor) + 9 additional Phase 8.0 candidates.

---

## 2. Objective A — Live Seafood Identifier Resolution

In Phase 8.3, Live Seafood Ltd was referenced as `RES-75541E` because the Phase 8.3 enricher keyed records by the research log identifier (`research_id`).

### Audit & Root Cause Analysis:
1. `RES-75541E` is the `research_id` assigned in `data/cache_sheets_research_log.json`.
2. `LEAD-MAN-0363CF` is the authoritative, canonical CRM `lead_id` present in `data/cache_sheets_leads.json`, `data/phase_8_0_production_lead_run.json`, and `data/phase_8_2_manual_outreach_queue.json`.
3. In Phase 8.4, the outreach pool builder explicitly resolves `LEAD-MAN-0363CF` as the primary identifier for Live Seafood Ltd while preserving `RES-75541E` as a cross-reference.
4. **Resolution:** Pool A and all CRM-facing artifacts now use `LEAD-MAN-0363CF`. Zero duplicate records were created. Regression test `test_20_live_seafood_id_inconsistency_prevented` and `test_13_live_seafood_remains_canonical_lead_id` permanently enforce this.

---

## 3. Objective B & C — Contact Discovery V2 Architecture & Source Families

The contact discovery engine utilizes high-precision exact-business queries and audits existing cached sources before querying:
1. **Existing Authoritative CRM Data:** `cache_sheets_leads.json`, `cache_sheets_review_queue.json`, `cache_sheets_research_log.json`.
2. **Cached Gosom Reviews (`data/cache_gosom_reviews/`):** Google Maps verified places, phone numbers, addresses, and ratings.
3. **Cached Search Evidence (`data/cache_search/`):** SearXNG / DuckDuckGo / Bing cached results.
4. **Exact Queries Executed per Candidate:**
   * `"<exact business name>" "Manchester" contact`
   * `"<exact business name>" "Manchester" email`
   * `"<exact business name>" "Manchester" Instagram`
   * `"<exact business name>" "Manchester" Facebook`
   * `"<exact business name>" "Manchester" phone`

Every discovered contact point is tagged with full provenance and deterministic evidence scoring:
* `value`
* `source`
* `source_family` (`OFFICIAL_BUSINESS_SITE`, `INSTAGRAM`, `FACEBOOK`, `GOOGLE`, `BUSINESS_DIRECTORY`, `REPUTABLE_LOCAL_DIRECTORY`, etc.)
* `source_url`
* `confidence` (`HIGH` / `MEDIUM`)
* `business_match` (`EXACT_MATCH`)
* `retrieved_at`
* `verification_status`

---

## 4. Objectives D, E, F — Channel Discovery Findings

### Email Discovery (Objective D)
* **Rule:** Guessed mailboxes (`hello@`, `info@`, `contact@`) constructed from business names or domains without public source backing are strictly rejected.
* **Findings:** 1 public email discovered:
  * `george@katsourisdeli.co.uk` (Katsouris Deli) from official business domain `katsourisdeli.com` cached in Gosom/search.
* **Verified Emails:** 0 (Katsouris Deli is `RESEARCH_ONLY`; email remains `PUBLIC_UNVERIFIED_BUSINESS_EMAIL`).
* **MX Invariant:** `MX_VALID != BUSINESS_EMAIL_VERIFIED` strictly enforced.

### Phone Discovery (Objective E)
* **Rule:** Manual-callable only. Normalized to UK standard (`+44 161 ...`). Numeric social IDs (e.g. `100071119218389`) rejected.
* **Findings (5 New Phones Discovered):**
  1. `Dog and Partridge`: `+44 161 943 9081` (Gosom Google Maps cache `gosom_ed151def2c27f5d8c2b6.json`, matching 665-667 Wilmslow Rd, Didsbury).
  2. `Katsouris Deli`: `+44 161 819 1260` (Gosom Google Maps cache `gosom_1ce03938f7fff5888bc6.json`, matching 113 Deansgate).
  3. `The Old Monkey`: `+44 161 228 6262` (Gosom Google Maps cache `gosom_3aea2dc3fdf366861569.json`, matching 90 Portland St).
  4. `Williams Sandwich Bar`: `+44 161 236 1833` (Manchester Evening News Directory / feature, matching 45 Hilton St).
  5. `Ducie Arms` (supplemental): `+44 161 232 9834` (Gosom Google Maps cache `gosom_2f94c637956f0940004b.json`).
  6. `Live Seafood Ltd` (supplemental): `+44 161 222 0363` (Gosom Google Maps cache `gosom_8da254acbd05ce6305b0.json`).

### Social Discovery (Objective F)
* **Rule:** Location safety checks enforced. Competing city profiles rejected (e.g. The Cornishman Facebook page in Crantock, Cornwall rejected). Public profile gives `MANUAL_CONTACTABLE` only; zero synthetic recipient IDs.
* **Findings (4 New Instagram, 1 New Facebook):**
  1. `The Station`: Instagram `@thestationpubdidsbury` (Didsbury location match).
  2. `Spicy Mango`: Instagram `@spicymangomcr` (Manchester East location match).
  3. `Fifth Nightclub`: Instagram `@fifthmanchester` (Princess St venue match).
  4. `The Old Monkey`: Instagram `@officialoldmonkey` (Portland St venue match).
  5. `The Crown & Kettle`: Facebook `@TheCrownandKettle` (Ancoats independent pub match).

---

## 5. Objective I — Target 24 Discovery Queue Breakdown

All 24 businesses in `CONTACT_DISCOVERY_QUEUE` were systematically audited:

| Research ID | Business Name | Discovery Outcome | Channels Found | Notes |
|---|---|---|---|---|
| `RES-4098E1` | Dog and Partridge | **NEW_CONTACT_FOUND** | Phone (`+44 161 943 9081`) | Google Maps verified |
| `RES-B116E0` | Katsouris Deli | **NEW_CONTACT_FOUND** | Phone (`+44 161 819 1260`), Email | Google Maps verified |
| `RES-160EF6` | The Station | **NEW_CONTACT_FOUND** | Instagram (`@thestationpubdidsbury`) | Search cache verified |
| `RES-F45098` | Spicy Mango | **NEW_CONTACT_FOUND** | Instagram (`@spicymangomcr`) | Search cache verified |
| `RES-3B9091` | The Old Monkey | **NEW_CONTACT_FOUND** | Phone (`+44 161 228 6262`), Instagram | Google Maps + Search |
| `RES-32E93F` | Fifth Nightclub | **NEW_CONTACT_FOUND** | Instagram (`@fifthmanchester`) | Search cache verified |
| `RES-657C7D` | The Crown & Kettle | **NEW_CONTACT_FOUND** | Facebook (`@TheCrownandKettle`) | Search cache verified |
| `RES-019BF7` | Williams Sandwich Bar | **NEW_CONTACT_FOUND** | Phone (`+44 161 236 1833`) | Local directory verified |
| `RES-A6B9CA` | Revolution | **AMBIGUOUS_CONTACT** | Ambiguous (@revolution.oxford.road) | Chain branch profile |
| `RES-111D4D` | The Cornishman | **AMBIGUOUS_CONTACT** | Conflicting profiles | Cornwall / London collision |
| `RES-DC05CA` | The Big Hands | **AMBIGUOUS_CONTACT** | Ambiguous profiles | Student venue / Povera mix |
| `RES-72BC13` | Eastern Pearl | **AMBIGUOUS_CONTACT** | Banqueting page | Unverified direct contact |
| `RES-74500B` | Apsley Cottage | **AMBIGUOUS_CONTACT** | Demolition petition | Operational status unclear |
| `RES-0DA996` | Crown & Anchor | **NO_CONTACT_FOUND** | None | No verified channel |
| `RES-5DE56B` | The Wendover | **NO_CONTACT_FOUND** | None | No verified channel |
| `RES-F31431` | Mr Egg | **NO_CONTACT_FOUND** | None | No verified channel |
| `RES-E775C9` | Albert Wilson | **NO_CONTACT_FOUND** | None | No verified channel |
| `RES-FFEB35` | The Orion | **NO_CONTACT_FOUND** | None | No verified channel |
| `RES-6A1393` | The Parkview | **NO_CONTACT_FOUND** | None | No verified channel |
| `RES-BB456C` | Red Lion | **NO_CONTACT_FOUND** | None | No verified channel |
| `RES-89EF11` | Lammars | **NO_CONTACT_FOUND** | None | No verified channel |
| `RES-152FEA` | Bay Horse PH (Closed)| **NO_CONTACT_FOUND** | None | Closed venue |
| `RES-108EB4` | The Waldorf | **NO_CONTACT_FOUND** | None | No verified direct channel |
| `RES-89AB50` | The Smithfield Market| **NO_CONTACT_FOUND** | None | No verified direct channel |

* **NEW_CONTACT_FOUND:** 8
* **AMBIGUOUS_CONTACT / NEEDS_MANUAL_REVIEW:** 5
* **NO_CONTACT_FOUND:** 11
* **TOTAL TARGETED:** 24

---

## 6. Objectives J, K, L — Manual Review Blocker Evaluation

Separately evaluated the 4 `MANUAL_REVIEW` businesses in the no-website cohort:

1. **Ducie Arms (`REV-MAN-A65953`):**
   * *Blocker:* Stale review data (>180 days old). Google review date is from December 2025 (>280 days old).
   * *Contactability:* Has Facebook (`theduciearms`) and Phone (`+44 161 232 9834`).
   * *Verdict:* **`REMAIN_MANUAL_REVIEW`**. Contactability alone does NOT bypass freshness requirements.
2. **Kro Bar (`REV-MAN-FCD454`):**
   * *Blocker:* Rating 3.4★ from 194 reviews. Minimum Rule B threshold is 4.0★.
   * *Contactability:* Has Phone (`+44 161 274 3100`) and Facebook (`aboutkrobar`).
   * *Verdict:* **`REMAIN_MANUAL_REVIEW`**. Requires human brand decision before any commercial outreach.
3. **Glamorous Chinese Restaurant (`REV-MAN-B8B175`):**
   * *Blocker:* Rating 3.4★ from 470 reviews. Minimum Rule B threshold is 4.0★.
   * *Contactability:* Has Facebook (`glamorous.restaurant`).
   * *Verdict:* **`REMAIN_MANUAL_REVIEW`**. Requires human brand decision.
4. **Crown & Anchor (`REV-MAN-F5446F`):**
   * *Blocker:* Rating missing (599 reviews).
   * *Contactability:* No contact channels.
   * *Verdict:* **`REMAIN_MANUAL_REVIEW`**.

---

## 7. Objective M — Rebuilt Outreach Pools

| Pool | Definition | Count | Lead ID(s) & Details |
|---|---|---|---|
| **Pool A** | `OUTREACH_READY + MANUAL_CONTACTABLE` | **1** | `LEAD-MAN-0363CF` — Live Seafood Ltd (Instagram DM / Facebook; already manually contacted in Phase 8.2) |
| **Pool B** | `OUTREACH_READY + AUTOMATED_SENDABLE` | **0** | No numeric recipient IDs (IGSIDs/PSIDs) or automated email sending capabilities exist |
| **Pool C** | `QUALIFIED_OR_REVIEW + NO_CONTACT_CHANNEL`| **1** | `REV-MAN-F5446F` — Crown & Anchor |
| **Pool D** | `MANUAL_REVIEW_REQUIRING_HUMAN_DECISION` | **4** | `REV-MAN-A65953` (Ducie Arms), `REV-MAN-FCD454` (Kro Bar), `REV-MAN-B8B175` (Glamorous Chinese), `REV-MAN-F5446F` (Crown & Anchor) |

---

## 8. Objectives O & P — Funnel Impact & State Integrity

| Metric | Phase 8.3 (Before) | Phase 8.4 (After) | Delta / Change |
|---|---|---|---|
| Total Target Cohort | 28 | 28 | 0 |
| Outreach Ready | 1 | 1 | 0 (Rule B strictly preserved) |
| Manual Review | 4 | 4 | 0 |
| **Manual Contactable** | **4** | **12** | **+8 (+200% expansion)** |
| Automated Sendable | 0 | 0 | 0 |
| Verified Business Phones | 1 | 4 | +3 |
| Manual Instagram Channels | 1 | 5 | +4 |
| Manual Facebook Channels | 4 | 5 | +1 |
| No Contact Channel | 24 | 16 | -8 |

### Invariant Checks:
* `qualification_state` mutated: **0**
* `lead_score` mutated: **0**
* `website_status` mutated: **0**
* `DUPLICATES_CREATED`: **0**
* `OUTREACH_SENDS`: **0**
* `CAMPAIGNS_ARMED`: **0**
* `MESSAGE_HISTORY_MUTATED`: **0**
* `FABRICATED_RECIPIENT_IDS`: **0**
* `LIVE_SEAFOOD_ID_CORRECT`: **YES (`LEAD-MAN-0363CF`)**
* `LIVE_SEAFOOD_STATE_UNCHANGED`: **YES**

---

## 9. Test Suite Verification

* **Phase 8.4 Test Suite:** `test_phase_8_4_contact_discovery.py` — **24/24 PASS**
* **Phase 8.3 Test Suite:** `test_phase_8_3_contactability.py` — **37/37 PASS**
* **Phase 8.2 Test Suite:** `test_phase_8_2_manual_outreach.py` — **16/16 PASS**
* **Phase 8.1 Test Suite:** `test_phase_8_1_production_qa.py` — **15/15 PASS**
* **Phase 7 Regression Suite:** `test_phase_7_*.py` — **255/255 PASS**
* **Total Passing Tests:** **347/347 PASS** (0 failures, 0 errors)

---

## 10. Objective T — Machine Summary

```text
PHASE_8_4_STATUS=PASS

TARGETED_NO_CONTACT=24
NEW_CONTACT_FOUND=8
NEW_VERIFIED_EMAILS=0
NEW_VERIFIED_PHONES=3
NEW_INSTAGRAM_PROFILES=4
NEW_FACEBOOK_PROFILES=1
AMBIGUOUS_CONTACTS=5
NO_CONTACT_FOUND=11

OUTREACH_READY_BEFORE=1
OUTREACH_READY_AFTER=1

MANUAL_CONTACTABLE_BEFORE=4
MANUAL_CONTACTABLE_AFTER=12

AUTOMATED_SENDABLE_BEFORE=0
AUTOMATED_SENDABLE_AFTER=0

MANUAL_REVIEW_BEFORE=4
MANUAL_REVIEW_AFTER=4

DUPLICATES_FOUND=0
DUPLICATES_CREATED=0

LIVE_SEAFOOD_ID_CORRECT=YES
LIVE_SEAFOOD_STATE_UNCHANGED=YES

OUTREACH_SENDS=0
CAMPAIGNS_ARMED=0
MESSAGE_HISTORY_MUTATED=0
FABRICATED_RECIPIENT_IDS=0

PHASE_8_4_TESTS_PASS=24
PHASE_8_4_TESTS_FAIL=0
PHASE_8_4_TESTS_ERROR=0
```
