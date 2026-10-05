# Phase 11.0 — GitHub Actions Production Readiness Architecture & Guide

This document specifies the operational and technical architecture for running the Dripp Media Lead Acquisition System inside GitHub Actions runners while maintaining 100% parity with local development.

---

## 1. GitHub Actions Architecture

The GitHub Actions integration is designed with decoupled, least-privilege, single-responsibility workflows. The system adheres to strict defense-in-depth principles: unattended crons are completely disabled until manual verification is concluded.

```
                    GitHub Actions Workflows
                               │
       ┌───────────────────────┼───────────────────────┐
       ▼                       ▼                       ▼
01-tests.yml        02-technical-pipeline.yml   03-freshness.yml / 04-monitoring.yml
(CI Regression &    (Manual workflow_dispatch    (Audits & Health Monitors;
 Security Audit)     Technical Runner)            Cron disabled initially)
       │                       │
       ▼                       ▼
  Regression              Technical
  Test Suite              Orchestrator
  (0 Secrets)                  │
                               ├── Discovery & Dedup (OSM / Local Gosom)
                               ├── Business Intelligence & Research
                               ├── Qualification Engine (Rule B)
                               ├── Contactability Assessment
                               ├── CRM Synchronization (Google Sheets)
                               └── Run Reports & Artifact Upload
```

### Key Principles
1. **Least Privilege**: All workflows declare top-level `permissions: { contents: read }`.
2. **Deterministic Serialization**: Concurrency controls (`cancel-in-progress: false`) prevent concurrent runs from interleaving atomic CRM updates.
3. **Defense-in-Depth Safety Invariants**: The application code enforces hard barriers independent of runner flags:
   - `TRAVEL_MODE = ACTIVE`
   - `COMMERCIAL_ACTIONS_ENABLED = FALSE`
   - `AUTOMATED_EMAIL_ENABLED = FALSE`
4. **Google Sheets as Canonical CRM**: Google Sheets remains the single source of truth. Ephemeral runners reconstruct local caches from Sheets and bootstrap storage directories cleanly on startup.

---

## 2. Required Runtime Versions

| Runtime / Tool | Version | Notes |
| :--- | :--- | :--- |
| **Python** | `3.12` | Production runner version on `ubuntu-latest`. Local testing supports `3.12`–`3.14`. |
| **Node.js** | `20.x` | Required for optional Node backend tooling and dashboard utilities. |
| **Gosom / google-maps-scraper** | `v1.18.1` | Local fallback scraper; cross-compiled Linux x86_64 binary automatically installed by `scripts/setup_gosom.py`. |
| **OS Target** | `ubuntu-latest` (Ubuntu 22.04/24.04 LTS) | macOS Darwin compatibility maintained for local pair programming. |

---

## 3. Configuration & Secrets Contract

Sensitive credentials must be stored strictly in **GitHub Actions Repository Secrets**. Non-sensitive knobs belong in **GitHub Actions Variables** or step environment variables.

### GitHub Actions Secrets (Encrypted)
| Secret Name | Required By | Description | Example / Format |
| :--- | :--- | :--- | :--- |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Sheets CRM Sync | Raw JSON service account key contents | `{"type": "service_account", ...}` |
| `GOOGLE_SHEET_ID` | Sheets CRM Sync | Canonical Google Sheet Spreadsheet ID | `12-34-abc...` |
| `COMPANIES_HOUSE_API_KEY` | Entity Verification (Optional) | Official UK Companies House REST API Key | `sec_...` |
| `SMTP_HOST` | Email Preflight (Dry Run) | Outbound SMTP host | `smtp.gmail.com` |
| `SMTP_PORT` | Email Preflight (Dry Run) | Outbound SMTP TLS port | `587` |
| `SMTP_USER` | Email Preflight (Dry Run) | Sender address | `user@drippmedia.com` |
| `SMTP_PASSWORD` | Email Preflight (Dry Run) | App password / token | `secret-app-password` |

*Note: Secrets are never echoed, printed, or written to generated artifacts.*

### GitHub Actions Variables (Non-Sensitive)
| Variable Name | Default Value | Description |
| :--- | :--- | :--- |
| `MARKET` | `UK` | Target commercial jurisdiction |
| `COUNTRY` | `United Kingdom` | Full country name |
| `CITY` | `Manchester` | Operating city |
| `LEAD_LIMIT` | `10` | Bounded execution cap per run |
| `DRY_RUN` | `true` | When true, freezes all mutating dispatches |
| `TRAVEL_MODE` | `ACTIVE` | Freezes physical local outreach & triggers |
| `COMMERCIAL_ACTIONS_ENABLED` | `FALSE` | Master kill switch for outreach |
| `AUTOMATED_EMAIL_ENABLED` | `FALSE` | Master kill switch for automated emails |
| `OPERATING_TIMEZONE` | `Europe/London` | Business operating schedule reference |

---

## 4. Workflows Overview

| Workflow File | Name | Trigger | Concurrency Group | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| `01-tests.yml` | `01 — Test Suite & Security Scan` | `push` (main), `pull_request` | `tests-${{ github.ref }}` | Runs repository security scan, full regression suite, and Actions compatibility tests. Requires zero production secrets. |
| `02-technical-pipeline.yml` | `02 — Technical Pipeline (Safe Mode)` | `workflow_dispatch` (Manual) | `production-pipeline-run` | Executes technical lead discovery, research, qualification (Rule B), contactability, and safe CRM sync. Default `dry_run: true`. |
| `03-freshness.yml` | `03 — Freshness & Contactability` | `workflow_dispatch` (Cron commented) | `freshness-maintenance-run` | Evaluates contactability degradation, verifies entity status freshness, and updates records. |
| `04-monitoring.yml` | `04 — System Monitoring & Health` | `workflow_dispatch` (Cron commented) | `monitoring-audit-run` | Performs system integrity checks, reconciles CRM invariants, audits sender health, and uploads health reports. |

---

## 5. Execution Procedures

### Manual Execution Procedure (`workflow_dispatch`)
1. Navigate to repository on GitHub: **Actions** → **02 — Technical Pipeline (Safe Mode)**.
2. Click **Run workflow**.
3. Configure run parameters:
   - `Market`: `UK`
   - `Country`: `United Kingdom`
   - `City`: `Manchester`
   - `Lead limit`: `10` (max 50)
   - `Dry run mode`: Check `true` (default)
   - `Refresh existing leads`: Check `false` (default)
   - `Enable deep research`: Check `true` (default)
   - `Enable contactability check`: Check `true` (default)
4. Click green **Run workflow** button.

### Dry-Run Verification Procedure
- `scripts/run_technical_pipeline.py` starts with:
  ```
  SAFETY CHECK: Asserting production safety invariants...
    ✓ TRAVEL_MODE = ACTIVE
    ✓ COMMERCIAL_ACTIONS_ENABLED = FALSE
    ✓ AUTOMATED_EMAIL_ENABLED = FALSE
    ✓ DRY_RUN = True
  ```
- If any invariant is violated or if outreach dispatches are initiated, the process immediately halts with an exit code of `1`.
- Outputs generated at `artifacts/technical_pipeline_summary.md` and `artifacts/technical_pipeline_report.json` document discovered leads, qualification rates, and contactability scores.

### Failure Recovery Procedure
- If a pipeline step fails:
  1. The runner exits non-zero (`exit 1`).
  2. `if: always()` hooks guarantee that execution logs, run reports, and diagnostic dumps are zipped into GitHub Actions artifacts.
  3. No partial outreach or orphaned state is committed: local file storage is ephemeral and the lock file (`data/pipeline.lock`) is released by `finally:` blocks.
  4. Google Sheets updates are performed in validated atomic batches, ensuring CRM records are never left in half-synced states.

---

## 6. Persistence & Runner Ephemerality

GitHub Actions runners are fresh ephemeral virtual machines. The persistence strategy separates data classes as follows:

| Category | Storage Location | Persistence Mechanism |
| :--- | :--- | :--- |
| **Canonical CRM** | Google Sheets | Remote API (`gspread` / Google Service Account). Persists permanently. |
| **Runtime Storage State** | `data/`, `scratch/`, `logs/` | Bootstrapped dynamically on runner startup via `scripts/storage_bootstrap.py`. |
| **Scraper Binaries** | `scratch/google_maps_scraper` | Installed dynamically per-run by `scripts/setup_gosom.py` based on runner OS/arch. |
| **Audit Logs & Diagnostics** | `artifacts/` | Preserved via `actions/upload-artifact@v4` with a 30-day retention period. |
| **Source Code** | Git Repo | Read-only checkout via `actions/checkout@v4`. |

*Rule: Runtime state and scraper artifacts are never committed back into Git.*

---

## 7. Gosom (google-maps-scraper) Setup

The local open-source fallback scraper is handled seamlessly via `scripts/setup_gosom.py`:
1. Detects runner OS (`linux` vs `darwin`) and architecture (`amd64` vs `arm64`).
2. Fetches official precompiled binary release `v1.18.1` from `https://github.com/gosom/google-maps-scraper/releases`.
3. Verifies or builds binary into `scratch/google_maps_scraper`.
4. Marks binary executable (`chmod +x`).
5. Exposes path via environment variable `GOSOM_BIN_PATH` for `LocalGosomProvider`.
6. Neither Apify nor paid Google Places APIs are invoked.

---

## 8. Scheduler Architecture & Decisions

### Decision: GitHub Actions as Single Orchestrator
- **Chosen Architecture**: Inside GitHub Actions, Node-based background cron daemons are **disabled**. GitHub Actions serves as the single-run caller invoking `scripts/run_technical_pipeline.py`.
- **Rationale**: Having long-running daemon schedulers running inside ephemeral CI jobs causes race conditions, unexpected runner timeouts, and redundant simultaneous runs.
- **Local Compatibility**: Local developers can still invoke `scripts/start_production.py` or standalone orchestrators on macOS.

---

## 9. Current Safety State

The production safety posture remains locked in Safe Mode:

- `TRAVEL_MODE`: **ACTIVE**
- `COMMERCIAL_ACTIONS_ENABLED`: **FALSE**
- `AUTOMATED_EMAIL_ENABLED`: **FALSE**
- `SCHEDULED CRONS`: **DISABLED / COMMENTED OUT**
- `MAX DISPATCH ATTEMPTS`: **0 (Hard blocked by 17 Preflight Gates)**
