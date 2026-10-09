# Phase 12.0 — Connect the Existing Lead Discovery UI to the Real Pipeline

## Executive Summary
This document provides the complete audit, architectural decisions, and verification results for **PHASE 12.0 — CONNECT THE EXISTING LEAD DISCOVERY UI TO THE REAL PIPELINE**.

The primary objective was to turn the existing Lead Discovery & Boundary Acquisition page in `static/index.html` into a fully operational, production-safe control panel for the genuine backend pipeline (`LeadGenerationPipeline`), without creating duplicate discovery interfaces, redesigning existing UI tokens, or compromising commercial safety invariants.

---

## 1. Audit of the Existing Discovery Implementation

Prior to Phase 12.0, the Discovery page possessed rich layout controls and telemetry panes, but several critical gaps prevented it from operating as a genuine control panel for the backend:

1. **Launch Wiring**:
   - The primary action button was partially disconnected from the backend API schema; it sent requests without strict market boundaries or conservative batch limits.
   - It lacked an explicit confirmation barrier summarizing the mission parameters before dispatching background jobs.

2. **Market Enforcement**:
   - Multiple UK cities (Leeds, Birmingham, London) appeared selectable in the territory grid, despite only Manchester Relation #162378 being verified and authorized for production lead acquisition.
   - The backend did not enforce market-level authorization gates, allowing arbitrary city strings to be accepted.

3. **Provider Configuration Misrepresentation**:
   - The interface displayed "Hybrid (OSM + Places + Apify)", implying Google Places API and Apify Actors were active.
   - In reality:
     - Google Places API is strictly disallowed by architecture constraints.
     - Apify provider credits have been completely exhausted.
     - OpenStreetMap Overpass (Relation #162378) is the primary verified boundary engine.
     - Tavily / local web crawling is the active verification engine.

4. **Metrics Provenance & Hardcoded Artifacts**:
   - Several DOM nodes contained static numbers (e.g., `142 Discovered`, `31 Researched`, `12 Qualified`, `9 Contactable`, `21.8%`) rather than deriving from persistent CRM records or durable run manager state.

5. **Rule B Qualification Clarity**:
   - The Quality Guarantee card in the UI previously emphasized rating and review thresholds alone, obscuring the fact that Rule B consists of 8 frozen canonical criteria where score determines review priority, not qualification.

---

## 2. Corrections Implemented

### 2.1 Backend Market & Provider Gatekeeping (`server.py`)
- **Strict Market Validation**: In `POST /api/search`, requests targeting cities other than Manchester or jurisdictions outside the UK are immediately rejected with `HTTP 400 Bad Request`.
- **Disallowed Provider Filtering**: Requests specifying `GOOGLE_PLACES` or `APIFY` are rejected with `HTTP 400 Bad Request`.
- **Supported Modes**: Supported and validated modes are restricted to `FREE_LOCAL` (OpenStreetMap Overpass + Local Crawler) and `HYBRID` (OSM + Foursquare + Web Search).
- **Conservative Batch Limits**: Batch processing sizes are strictly bounded between 1 and 25 candidates per batch.

### 2.2 Provider Adapter Corrections (`lib/discovery/hybrid.py`)
- Corrected `health_check()` and `transparency_stats` boundary to Manchester Relation #162378 (previously referenced Birmingham).
- Updated health diagnostics to honestly report:
  - `OpenStreetMap`: `CONNECTED`
  - `GooglePlaces`: `DISALLOWED (Architecture Constraint)`
  - `Apify`: `EXHAUSTED / DISABLED`
- Added explicit provider notes detailing cost and quota constraints.

### 2.3 Durable Execution Lifecycle (`lib/system/pipeline_run_manager.py`)
- Durable run records persisted to `data/pipeline_runs/{run_id}.json` and `latest_run.json`.
- Strict concurrency enforcement: overlapping requests return `HTTP 409 Conflict`.
- Process restart recovery: interrupted runs are automatically marked `FAILED` with restart diagnostics.
- Safe operator cancellation via `POST /api/search/cancel`.

### 2.4 Lead Discovery UI Enhancements (`static/index.html`)
- **Territory Controls**: Manchester is the active operating market. Secondary cities (Leeds, Birmingham, London) are explicitly locked with `opacity: 0.42`, `cursor: not-allowed`, and a `LOCKED (PHASE 12.0)` badge. Clicks on locked cities display a non-blocking operator toast.
- **Provider Matrix**: Mode selectors and health matrix updated to show `FREE_LOCAL` (recommended) and `HYBRID`. Disallowed/exhausted options are disabled with clear status tags.
- **Frozen Rule B Quality Gate**: Updated summary to explicitly list all 8 canonical criteria (review volume, minimum rating, accepted evidence source, review recency, operational signal, boundary match, zero review conflict, and zero closure flags).
- **100% Backend-Driven HUD & Pipeline Tabs**: Removed all static hardcoded numbers (`142`, `31`, `12`, `9`). Values now dynamically populate from `/api/status` or CRM workspace leads.

---

## 3. Commercial Safety Invariants

All technical lead discovery and qualification operations are completely separated from commercial dispatch. The following core invariants remain strictly locked:

- `TRAVEL_MODE`: `True` / `ACTIVE`
- `COMMERCIAL_ACTIONS_ENABLED`: `False` / `LOCKED`
- `AUTOMATED_EMAIL_ENABLED`: `False` / `DISABLED`
- `CRON`: `DISABLED`
- `RULE B`: `FROZEN` (8 canonical criteria)

No technical discovery run can send emails, send social messages, arm campaigns, or trigger commercial dispatch.

---

## 4. Verification & Test Results

### 4.1 Phase 12.0 Dedicated Test Suite (`test_phase_12_0_lead_discovery_ui.py`)
- **19 of 19 tests passed (100%)**
- Verified:
  1. DOM element presence and modal launch wiring.
  2. Rule B Quality Gate 8 canonical criteria summary.
  3. Elimination of hardcoded counts from DOM.
  4. Provider health matrix honest diagnostic reporting.
  5. Manchester dry-run request acceptance.
  6. Rejection of unsupported markets (Leeds, London) with HTTP 400.
  7. Rejection of disallowed providers (Google Places, Apify) with HTTP 400.
  8. Enforcement of batch size limits (1-25).
  9. Duplicate submission prevention (HTTP 409 Conflict).
  10. Run cancellation via `POST /api/search/cancel`.
  11. Real-time run status via `GET /api/status`.
  12. Provider diagnostics via `GET /api/discovery/status`.
  13. Commercial safety invariants locked (`TRAVEL_MODE`, `COMMERCIAL_ACTIONS_ENABLED`, `AUTOMATED_EMAIL_ENABLED`, `RULE_B_FROZEN`).

### 4.2 Website Pipeline Test Suite (`test_website_pipeline_phase_temp.py`)
- **15 of 15 tests passed (100%)**

### 4.3 Full Repository Regression Suite (`pytest -q`)
- **1,629 passed, 0 failed, 76 warnings in 300.16s (0:05:00)**
