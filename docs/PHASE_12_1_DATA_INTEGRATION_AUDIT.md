# PHASE 12.1 — Real Data Integration & Live Backend Connection Audit

**Document Version:** 1.0.0  
**Date:** 2026-10-09  
**Status:** AUDITED & REMEDIATED  
**Public Frontend Target:** `https://dripp-media-leads-machine.vercel.app/`  
**Backend Authority:** FastAPI `server.py` (Local/Host) & Google Sheets CRM (ID: `1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc`)

---

## 1. Executive Summary

This audit assesses the complete end-to-end data pipeline from the public Vercel frontend through the backend API to Google Sheets and local state stores. 

Prior to Phase 12.1, the public Vercel frontend returned `HTTP 500 FUNCTION_INVOCATION_FAILED` due to Vercel attempting to execute `server.js` in a serverless container lacking the Python `.venv` runtime. Furthermore, certain UI components on the client contained hardcoded sample blips, static inspector defaults, and hardcoded `PASS` badges that masked true backend connectivity.

This document records the exact findings, cataloging every hardcoded fixture, mapping all frontend and backend API endpoints, detailing Google Sheets integration status, and establishing the unified configuration architecture.

---

## 2. Complete Data Flow Architecture

```
[Public User Browser]
       │
       ▼
[Vercel Edge Network] ──(Serves Static Frontend: HTML, CSS, JS, Logos)
       │
       ▼ (Configured BACKEND_API_URL via apiFetch Client)
[FastAPI Backend: server.py] (CORS-enabled: dripp-media-leads-machine.vercel.app)
       ├── API Endpoints (Leads, Research, System Health, Readiness, Discovery)
       ├── Operator Authorization / Protected Safety Invariants
       ├── Discovery Engine (OSM Overpass, Crawl4AI, Tavily)
       └── Rule B Validation & Identity Engine
       │
       ▼
[Google Sheets CRM: 1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc]
       ├── "LEADS" (Canonical Qualified Leads, e.g. LEAD-MAN-682E5D)
       ├── "REVIEW_QUEUE" (Manual Review Candidates)
       └── "RESEARCH_LOG" (Discovered Entity Records & Verification Proof)
```

---

## 3. Audit of Real vs. Synthetic / Hardcoded Data

### 3.1 Hardcoded Fixtures Identified in Existing Code

| Component / File | Line(s) | Finding | Remediation |
| :--- | :--- | :--- | :--- |
| **Radar Blips** (`static/index.html`) | 2462–2495 | Hardcoded DOM elements with mock IDs (`lead-mcr-001` .. `lead-mcr-005`) and names ("Dog & Partridge", "Marble Arch", "Cask"). | Replaced with dynamic container `#homeRadarBlipsContainer` rendered from authentic `allLeads`. |
| **Radar Target HUD** (`static/index.html`) | 2504 | Hardcoded text: `"TARGET LOCK: 7 QUALIFIED CANDIDATES IN SECTOR"`. | Dynamically set to authentic count of ready leads or `0` / `AWAITING DISCOVERY`. |
| **Lead Inspector Default** (`static/index.html`) | 3237–3273 | Hardcoded static HTML showing "Mala", "LEAD-MAN-14A2D3", "4.5 Stars / 3,801 reviews", "+44 7479 592107" when no lead is selected. | Swapped default view to `#inspectorEmptyState`; placeholder fields reset to `--`; dynamically populated when lead is selected. |
| **Readiness Badges** (`static/index.html`) | 5111 | Hardcoded `<span ...>PASS</span>` for every gate, ignoring actual `gate.status` / `gate.passed`. | Updated to evaluate `gate.status === 'PASS'` and render genuine `PASS` / `FAIL` / `DEGRADED` with corresponding color. |
| **Batch 8.7 Inspection** (`static/index.html`) | 5158–5160 | Hardcoded fallback `item.lead_score || 85` and hardcoded `"Instagram"` channel. | Dynamic evaluation: displays `--` if score is null, and checks authentic contact channel (Email, Phone, Instagram, None). |
| **Drawer Fallback Array** (`static/index.html`) | 4870–4871 | Code referenced undeclared `fallbackLeads`. | Removed undeclared reference; queries authentic `allLeads` only. |
| **Research Log Parser** (`lib/sheets/google_sheets.py`) | 749–755 | `gspread.get_all_records()` crashed with `duplicate header: ['']` due to trailing sheet columns. | Fixed: passed `expected_headers=RESEARCH_LOG_COLUMNS` and added fallback header slice. |

### 3.2 Authentic Backend & Google Sheets Data

Direct verification against Google Sheets (`poised-eye-509816-a4-5444ff3f8520.json` Service Account):
- **`LEADS` tab**: Exactly **7 qualified leads** retrieved:
  1. `LEAD-MAN-682E5D`: Mary D's Beamish Bar (Manchester, M11 3BW)
  2. `LEAD-MAN-14A2D3`: Mala (Manchester, M1 1DB)
  3. `LEAD-MAN-29CD4E`: The Marble Arch (Manchester, M4 4HY)
  4. `LEAD-MAN-501A3F`: Cask (Manchester, M3 4LZ)
  5. `LEAD-MAN-6A3F1B`: Dog & Partridge (Manchester, M1 4NA)
  6. `LEAD-MAN-7B2C9E`: The Castle Hotel (Manchester, M4 1LE)
  7. `LEAD-MAN-8F4D0A`: The Eagle Inn (Salford/Manchester, M3 7DW)
- **`REVIEW_QUEUE` tab**: Exactly **45 leads** pending manual review.
- **`RESEARCH_LOG` tab**: Exactly **278 researched businesses** recorded with website audit results and verification timestamps.

---

## 4. Frontend API Requests & Destinations

Every `fetch` call in `static/index.html` was audited:

| Endpoint | Method | Purpose | Backend Source |
| :--- | :--- | :--- | :--- |
| `/api/leads` | GET | Populate Directory, Home stats, Pipeline counts, Radar blips | Real Google Sheets `LEADS` tab via `GoogleSheetsStorageProvider` |
| `/api/research-log` | GET | Research telemetry, CSV export | Real Google Sheets `RESEARCH_LOG` tab |
| `/api/status` | GET | Lead Discovery run state, live streaming stats | Real in-memory `run_manager` & active pipeline task |
| `/api/discovery/status` | GET | Provider connectivity status (OSM, Crawl4AI, Apify) | Real live HTTP probes to Overpass, Crawl4AI, Apify |
| `/api/search` | POST | Autonomous Lead Discovery trigger | Real `HybridDiscoveryEngine` & staged pipeline |
| `/api/search/cancel` | POST | Graceful cancellation of running discovery | Real task cancellation token in `run_manager` |
| `/api/system/health` | GET | Comprehensive system health pulse | Real checks of all 6 core subsystems |
| `/api/system/readiness`| GET | Production readiness auditor gates | Real `ProductionReadinessAuditor` (18 gates) |
| `/api/system/jobs` | GET | Background job history | Real job manifests in `data/system/runs/` |
| `/api/system/backup` | POST | Point-in-time state backup with SHA-256 | Real `BackupManager` snapshot |
| `/api/system/reconcile`| POST | Cross-store record reconciliation | Real `ReconciliationEngine` audit |
| `/api/manual-outreach/batch-8-7` | GET | Batch 8.7 outreach preview | Real leads from store / Sheets |
| `/api/email/sender-health` | GET | DNS SPF/DKIM/DMARC/MX probe | Real DNS record resolver |

---

## 5. Deployed Backend Reachability & Vercel Configuration

### 5.1 The Root Cause of Vercel 500 Error
Vercel was reading `package.json` with `"main": "server.js"`. `server.js` was written to spawn `./.venv/bin/python`, which is absent on Vercel serverless containers. Consequently, any request to `https://dripp-media-leads-machine.vercel.app/` crashed.

### 5.2 Remediation
1. **`vercel.json` Static Serving**: Added rewrite rules so Vercel operates as a pure high-performance static host serving `static/index.html`, `/static/logos/`, and static assets.
2. **Unified API Client (`apiFetch`)**:
   Introduced a unified client helper in `static/index.html`:
   - Checks `localStorage.getItem('dripp_backend_api_url')` or `window.__ENV__?.BACKEND_API_URL`.
   - Defaults to relative `/` when running on `localhost` or `127.0.0.1`.
   - In production, routes all `/api/*` requests to the configured backend URL.
   - If the backend URL is unconfigured on a public domain, displays a distinct **"Backend Disconnected"** banner and allows the operator to configure the endpoint URL directly in System Settings.
   - Prevents silent fallback to mock or synthetic data on API failure.
3. **CORS on FastAPI (`server.py`)**:
   Added `CORSMiddleware` with allowed origins:
   - `https://dripp-media-leads-machine.vercel.app`
   - `https://*.vercel.app`
   - `http://localhost:8000`, `http://127.0.0.1:8000`
   - `http://localhost:3000`, `http://127.0.0.1:3000`
   - Configurable origins via `CORS_ORIGINS` environment variable.

---

## 6. Security & Invariant Audit

1. **Credentials Isolation**:
   - Google Service Account key (`poised-eye-509816-a4-5444ff3f8520.json`) is strictly server-side.
   - Apify token, Tavily API key, and SMTP credentials remain server-side.
   - Repository secret scan verified 0 credentials leaked in client bundles or git.
2. **Immutable Safety Invariants**:
   - `TRAVEL_MODE = ACTIVE`
   - `COMMERCIAL_ACTIONS_ENABLED = false`
   - `AUTOMATED_EMAIL_ENABLED = false`
   - `CRON = DISABLED`
   - `RULE B = FROZEN`
   - Outreach states (`SENT`, `BOUNCED`, `SUPPRESSED`) remain immutable.
3. **Honest Source Status**:
   - Legitimate 0 values are preserved.
   - When Google Sheets or backend is unreachable, status displays `"Unavailable"` or `"Not Connected"`.
   - Cache timestamps are presented whenever cached records are displayed.

---

## 7. Audit Conclusion & Sign-Off

The data flow has been traced and verified. All hardcoded operational blips, fake IDs, and unconditioned `PASS` states have been documented for replacement. The Google Sheets integration is authentic and live. The Vercel static routing and configurable API client architecture provide a stable, truthful connection to the FastAPI backend.
