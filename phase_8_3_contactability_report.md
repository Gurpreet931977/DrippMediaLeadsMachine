# Phase 8.3 — Contactability Expansion Report
**Generated:** 2026-10-03 | **Status:** PASS

---

## 1. Cohort

The authoritative Phase 8.0 production run (2026-10-03) identified **60 Manchester hospitality businesses**. Of those, **28 have NO_WEBSITE_CONFIRMED** status. This phase audited all 28.

| Source | Count |
|---|---|
| Phase 8.0 production run (2026-10-03) | 60 total |
| NO_WEBSITE_CONFIRMED | **28** |
| OUTREACH_READY | 1 |
| MANUAL_REVIEW | 4 |
| RESEARCH_ONLY | 23 |

---

## 2. Contactability Funnel

```
28  NO_WEBSITE_CONFIRMED (Phase 8.0 cohort)
 |
 +-- 4  MANUAL_CONTACTABLE (at least one verified channel)
 |    +-- 1  INSTAGRAM_MANUAL_CONTACTABLE (Live Seafood Ltd)
 |    +-- 4  FACEBOOK_MANUAL_CONTACTABLE (Live Seafood, Ducie Arms, Kro Bar, Glamorous Chinese)
 |    +-- 1  VERIFIED_PHONE (Kro Bar)
 |
 +-- 24  NOT_CONTACTABLE (no verified channel in authoritative data)

0   AUTOMATED_SENDABLE (no IGSIDs, no PSIDs available)
```

Key rule enforced: A public social URL is MANUAL_CONTACTABLE only.
It is NEVER AUTOMATED_SENDABLE without a verified numeric recipient ID.

---

## 3. Channel Breakdown

| Channel | Count | Notes |
|---|---|---|
| VERIFIED_BUSINESS_EMAIL | 0 | No emails found in authoritative data |
| UNVERIFIED_BUSINESS_EMAIL | 0 | |
| MX_VALID_EMAIL | 0 | MX_VALID != VERIFIED_BUSINESS_EMAIL (invariant enforced) |
| VERIFIED_BUSINESS_PHONE | 1 | Kro Bar (+44 161 274 3100) |
| UNVERIFIED_PHONE | 0 | |
| INSTAGRAM_MANUAL_CONTACTABLE | 1 | Live Seafood Ltd |
| FACEBOOK_MANUAL_CONTACTABLE | 4 | Live Seafood, Ducie Arms, Kro Bar, Glamorous Chinese |
| AUTOMATED_SENDABLE | 0 | No numeric recipient IDs available |
| NO_CONTACT_CHANNEL | 24 | No verified channel in any authoritative source |

---

## 4. Qualified Outreach Pool (Pool A — Manual Ready)

Criteria: qualification_state = OUTREACH_READY AND manual_contactable = true

| Lead ID | Business | Recommended Channel | Source |
|---|---|---|---|
| RES-75541E | Live Seafood Ltd | Instagram DM | cache_sheets_leads |

Pool A count: 1

Live Seafood Ltd is already manually contacted (Phase 8.2).
This is the only OUTREACH_READY + contactable business in the cohort.

---

## 5. Automated Review Pool (Pool B)

Criteria: qualification_state = OUTREACH_READY AND automated_sendable = true

Pool B count: 0

No businesses satisfy automated sendability. Reasons:
- No numeric Instagram IGSID available for any business
- No numeric Facebook PSID available for any business
- No verified business email with proper classification

---

## 6. Blocked Businesses

### MANUAL_REVIEW — Contactable (not in Pool A — qualification state insufficient)

| Lead ID | Business | Best Channel |
|---|---|---|
| RES-525524 | Ducie Arms | Facebook (theduciearms) |
| RES-A588B7 | Kro Bar | Phone (+44 161 274 3100) |
| RES-A66B5D | Glamorous Chinese Restaurant | Facebook (glamorous.restaurant) |

These 3 have contact channels but remain MANUAL_REVIEW.
Contact enrichment alone CANNOT promote them to OUTREACH_READY.

### MANUAL_REVIEW — No Contact Channel

| Lead ID | Business | Blocking Reason |
|---|---|---|
| RES-0DA996 | Crown & Anchor | No profile; No phone; No email |

### RESEARCH_ONLY — Not Contactable (23 businesses)

The Wendover, Dog and Partridge, Revolution, The Cornishman, Katsouris Deli,
The Big Hands, Mr Egg, Albert Wilson, The Station, The Orion, Spicy Mango,
The Parkview, The Old Monkey, Fifth Nightclub, Red Lion, The Crown & Kettle,
Eastern Pearl, Apsley Cottage, Lammars, Bay Horse PH (Closed),
Williams Sandwich Bar, The Waldorf, The Smithfield Market Tavern

All 23 are RESEARCH_ONLY — not qualified for outreach regardless of contact status.
Retained in CRM for future enrichment.

---

## 7. Duplicate Protection

| Counter | Value |
|---|---|
| DUPLICATES_FOUND | 0 |
| DUPLICATES_CREATED | 0 |
| EXISTING_RECORDS_ENRICHED | 28 |

---

## 8. CRM Integrity

All qualification states, lead scores, ratings, review counts, and website statuses
are UNCHANGED by Phase 8.3 enrichment.

qualification_state unchanged:        PASS
website_status unchanged:             PASS
No MANUAL_REVIEW -> OUTREACH_READY:   PASS
MX_VALID != VERIFIED_BUSINESS_EMAIL:  PASS
Public URL != AUTOMATED_SENDABLE:     PASS
LIVE_SEAFOOD_STATE_UNCHANGED:         YES
message_history.json unchanged:       PASS
campaigns.json unchanged:             PASS

---

## 9. Recommended Next Engineering Step

Measured bottleneck: QUALIFICATION (primary) -> CONTACT_ENRICHMENT (secondary)

```
28  NO_WEBSITE_CONFIRMED
 |--  1  OUTREACH_READY  (3.6%)
 |--  4  MANUAL_REVIEW   (14.3%)
 +-- 23  RESEARCH_ONLY   (82.1%) <- PRIMARY BOTTLENECK
```

Recommended actions in priority order:

1. RE-QUALIFY the 23 RESEARCH_ONLY businesses (highest leverage)
   These are unreachable regardless of contact enrichment.
   Requires: additional review freshness data or operational re-evaluation.

2. INVESTIGATE SOCIAL for SOCIAL_FOUND / RESEARCH_ONLY businesses
   Revolution, The Cornishman, The Station, Spicy Mango, The Old Monkey,
   Fifth Nightclub, The Crown & Kettle all have accessible social.
   If re-qualified, they have an immediate manual contact channel.

3. EMAIL DISCOVERY for MANUAL_REVIEW businesses
   Ducie Arms, Glamorous Chinese: have Facebook but no email/phone.
   A verified business email would upgrade them to highest-priority channel.

4. IGSID / PSID ACQUISITION
   Only path to automated sendability.
   Requires Meta Business API access + business owner consent.

---

## Machine Summary

```
PHASE_8_3_STATUS=PASS
NO_WEBSITE_COHORT=28
OUTREACH_READY=1
MANUAL_CONTACTABLE=4
AUTOMATED_SENDABLE=0
VERIFIED_EMAIL=0
MX_VALID_EMAIL=0
VERIFIED_PHONE=1
INSTAGRAM_MANUAL=1
FACEBOOK_MANUAL=4
NO_CONTACT_CHANNEL=24
DUPLICATES_FOUND=0
DUPLICATES_CREATED=0
OUTREACH_SENDS=0
CAMPAIGNS_ARMED=0
MESSAGE_HISTORY_MUTATED=0
LIVE_SEAFOOD_STATE_UNCHANGED=YES
PHASE_8_3_TESTS_PASS=37
PHASE_8_3_TESTS_FAIL=0
PHASE_8_3_TESTS_ERROR=0
PHASE_8_2_TESTS_PASS=16
PHASE_8_1_TESTS_PASS=15
PHASE_7_ALL_TESTS_PASS=255
```
