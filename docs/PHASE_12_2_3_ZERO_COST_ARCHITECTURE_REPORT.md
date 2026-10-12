# PHASE 12.2.3 — ZERO-COST BACKEND ARCHITECTURE & RESUMABLE JOB STATE REPORT

**Project:** Dripp Media Leads Machine  
**Phase:** 12.2.3  
**Target Monthly Budget:** **$0.00 / month (Zero Hosting & Cloud Infrastructure Spend)**  
**Safety Invariants:** STRICT OPERATOR-IN-THE-LOOP (`TRAVEL_MODE=ACTIVE`, `COMMERCIAL_ACTIONS_ENABLED=false`, `AUTOMATED_EMAIL_ENABLED=false`, `CRON=DISABLED`, Rule B Frozen at 8 criteria)  
**Execution Mode:** Design and Code-Readiness Phase (Zero deployments created, zero billing activated)

---

## 1. Executive Summary & Recommended Near-Zero-Cost Architecture

### 1.1 Architecture Recommendation

| Lifecycle Stage | Recommended Architecture | Monthly Cost | Credit Card Required | Financial Overage Risk |
|:---|:---|:---|:---|:---|
| **Pilot Phase (Current)** | **Vercel Static Frontend + Render Free Web Service + Google Sheets CRM & Operational Ledger** | **$0.00 / month** | **NO** | **ZERO ($0.00 Hard Cap)** |
| **Production Scale (Future Upgrade)** | **Vercel Frontend + GCP Cloud Run Service + Cloud Run Jobs + Cloud Firestore** | **$0.00 / month nominal** | **YES (Mandatory)** | **NON-ZERO** (Requires detacher function) |

### 1.2 Why Render Free + Google Sheets Operational Ledger is Recommended for Pilot

1. **Absolute Zero-Cost Guarantee ($0.00 / month):** Render Free provides 750 free instance hours per month for web services with 512 MB RAM and 0.1 shared vCPU. No credit card or billing account is required to deploy.
2. **Hard Spending Cap by Default:** If Render free-tier quotas or bandwidth (100 GB/month) are exceeded, Render simply suspends the service until the next billing cycle. It is technically impossible to receive an unexpected bill.
3. **Zero New Infrastructure Dependencies:** Reuses the existing authenticated Google Sheets Service Account (`dripp-sheets-crm-agent@dripp-media-leads-machine.iam.gserviceaccount.com`). No Firestore database, IAM roles, or GCP billing project need to be created.
4. **CRM & Operational Separation:** Business data (`LEADS`, `REVIEW_QUEUE`, `RESEARCH_LOG`) remains untouched in the Google Sheets CRM. Operational state (job locks, stage checkpoints, candidate cursors, heartbeats) is housed in a dedicated `OPERATIONAL_JOBS` worksheet.
5. **Resumable Single-Instance Execution:** Discovery tasks are operator-initiated single-instance tasks. The custom `OperationalJobLedger` provides heartbeat tracking, single-instance mutual exclusion, and candidate cursor checkpoints, enabling seamless resumption when the Render container sleeps or restarts.

### 1.3 Upgrade Path: GCP Cloud Run + Cloud Firestore

When commercial outreach is unlocked and lead volume exceeds Google Sheets API rate limits (300 requests/minute), the recommended upgrade path is:
- **Cloud Run Service (FastAPI):** Stateless HTTP API with sub-second cold starts.
- **Cloud Run Jobs:** Bounded batch execution tasks running up to 24 hours per invocation without HTTP proxy timeouts.
- **Cloud Firestore:** Low-latency NoSQL persistence for operational leases, checkpoints, and suppressions.
- **Pre-requisite for Upgrade:** Operator must configure a billing account with an automated Cloud Billing Budget alerting threshold and a Cloud Function budget-detacher script to eliminate un-capped overage risks.

---

## 2. Comprehensive Runtime Persistence Dependency Audit

Every location where the application expects state to survive process termination or deployment was audited across the codebase and classified:

| Runtime Dependency | Code Location / Storage Target | Classification | Persistence Reality on Free Hosting | Mitigation / Durable Architecture |
|:---|:---|:---:|:---|:---|
| **Qualified Leads CRM** | `lib/sheets/google_sheets.py` (`LEADS` tab) | **A. Authoritative CRM** | Persistent in Google Cloud (external to backend host) | None needed; Google Sheets is the source of truth |
| **Review Queue** | `lib/sheets/google_sheets.py` (`REVIEW_QUEUE` tab) | **A. Authoritative CRM** | Persistent in Google Cloud | None needed; Google Sheets is the source of truth |
| **Research Log** | `lib/sheets/google_sheets.py` (`RESEARCH_LOG` tab) | **A. Authoritative CRM** | Persistent in Google Cloud | None needed; Google Sheets is the source of truth |
| **Active Job Status & Runs** | `server.py` (`active_jobs` dict) | **B. Durable Operational** | **Lost on container sleep/restart** | **Migrated to `OPERATIONAL_JOBS` tab** via `OperationalJobLedger` |
| **Stage Checkpoints & Cursors** | `lib/discovery/`, `server.py` | **B. Durable Operational** | **Lost on container sleep/restart** | **Persisted to `OPERATIONAL_JOBS`** (`stage`, `candidate_cursor`, `processed_keys`) |
| **Job Execution Lease / Locks** | `server.py` (in-memory lock) | **B. Durable Operational** | **Lost on container sleep/restart** | **Persisted to `OPERATIONAL_JOBS`** (`lease_holder`, `lease_expires_at`, heartbeat) |
| **Stale Job Heartbeats** | `server.py` | **B. Durable Operational** | **Lost on container sleep/restart** | **Persisted to `OPERATIONAL_JOBS`** (`last_heartbeat`, auto-reconcile to `INTERRUPTED`) |
| **Website & Contact Cache** | `lib/enrichment/website_pipeline.py` | **C. Rebuildable Cache** | Ephemeral memory cache | Rebuilt on demand during pipeline execution |
| **OSM Coordinates Cache** | `lib/discovery/local_search_provider.py` | **C. Rebuildable Cache** | Ephemeral memory cache | Re-queried from Nominatim / Overpass if cache missed |
| **Tavily Research Cache** | `lib/enrichment/tavily_provider.py` | **C. Rebuildable Cache** | Ephemeral memory cache | Re-queried from search API if missing |
| **Candidate Discovery JSONs** | `data/candidates/` | **D. Temporary Data** | Ephemeral disk | Kept in-memory or flushed to `/tmp`; progress tracked by cursor |
| **Scraper Output Artifacts** | `data/scraping/` | **D. Temporary Data** | Ephemeral disk | Kept in-memory or flushed to `/tmp` |
| **Local CRM Backups & Manifests** | `data/backups/` | **E. Backup / Integrity** | **Lost on container sleep/restart** | Relies on Google Sheets revision history; local backups run via operator CLI |
| **Preflight Audit Reports** | `data/production_preflight_report.json` | **E. Backup / Integrity** | Ephemeral disk | Regenerated deterministically at runtime via `preflight.py` |
| **Security Audit Logs** | `data/security_audit.log` | **E. Backup / Integrity** | Ephemeral disk | Logged to stdout/stderr (captured by Render log stream) |
| **Operator Sessions & Tokens** | `server.py` (`operator_sessions`) | **B. Durable Operational** | Cleared on container restart | Operator re-authenticates (safe security posture) |
| **Login Rate-Limiting Counters** | `server.py` (`login_attempts`) | **B. Durable Operational** | Reset on container restart | In-memory sliding window; resets safely without security vulnerability |

---

## 3. Durable and Resumable Discovery Job Design

### 3.1 Architecture of `OperationalJobLedger`

A small, robust storage abstraction was implemented in `lib/sheets/operational_ledger.py` that interfaces with the Google Sheets CRM provider:

```
+-------------------------------------------------------------------------+
|                          OPERATIONAL_JOBS TAB                           |
+-------------------------------------------------------------------------+
| run_id                 | Canonical unique execution ID                  |
| schema_version         | 1.0.0                                          |
| status                 | QUEUED | RUNNING | PAUSED | INTERRUPTED | ...   |
| territory / industry   | Manchester, UK | Restaurants                   |
| stage                  | DISCOVERY -> WEBSITE_CHECK -> REVIEW -> CRM    |
| requested_leads        | Target quota (e.g. 10)                         |
| qualified_count        | Current qualified count                        |
| candidates_evaluated   | Processed candidate count                      |
| candidate_cursor       | Last evaluated candidate identifier            |
| processed_keys_compact | Compact JSON list of evaluated keys (dedup)    |
| attempt_count          | Retry attempt counter                          |
| error_code             | Safe error classification                      |
| created_at / updated_at| ISO UTC timestamps                             |
| last_heartbeat         | Heartbeat timestamp refreshed every 45-60s     |
| lease_holder           | Container instance UUID holding execution lock |
| lease_expires_at       | UTC expiration for mutual exclusion            |
| idempotency_hash       | SHA-256 of execution parameters                |
| recovery_metadata_json | Structured recovery payload                    |
+-------------------------------------------------------------------------+
```

### 3.2 Key Operational Capabilities

1. **Single-Instance Mutual Exclusion:**
   - When `create_job()` is called, it queries the ledger for active unexpired jobs.
   - If an unexpired job (`status == "RUNNING"` with `lease_expires_at > now`) exists, it fails closed with `ConcurrentJobConflictError`.
   - Prevents concurrent discovery jobs from competing for memory or Google Sheets rate limits on single-instance free tiers.
2. **Heartbeats and Stale Job Reconciliation:**
   - The running job updates `last_heartbeat` and extends `lease_expires_at` every 45-60 seconds (well below the 120s timeout).
   - If Render Free sleeps, restarts, or terminates the container mid-execution, the heartbeat stops.
   - On the next startup, `reconcile_stale_jobs()` identifies the stale running job and transitions its status to `INTERRUPTED` with `error_code="STALE_HEARTBEAT_TIMEOUT"`.
   - The operator or frontend is truthfully informed of the interruption.
3. **Resumable Checkpoints:**
   - At each pipeline stage (`DISCOVERY`, `WEBSITE_CHECK`, `REVIEW_EVIDENCE`, `CRM_SYNC`), the job records its `candidate_cursor` and appends processed business keys to `processed_keys_compact`.
   - On resumption, the pipeline loads `candidate_cursor` and skips already-processed candidates, preventing duplicate CRM row creation or re-running expensive web scraping.
4. **Google Sheets API Quota Conservation:**
   - Updates are debounced and batched into row updates (`A{idx}:T{idx}`) rather than individual cell writes.
   - Calls to Google Sheets are wrapped in `_execute_with_retry()` with exponential backoff to smoothly absorb transient HTTP 429 quota limits.

---

## 4. Execution Compatibility with Idle/Sleeping Free Services

### 4.1 Render Free Inactivity Lifecycle

- **Idle Sleep:** Render Free services sleep after **15 minutes** of HTTP inactivity.
- **Cold Start:** An incoming HTTP request wakes the container in **30–50 seconds**.
- **HTTP Proxy Timeout:** Render's reverse proxy terminates HTTP requests that do not respond within **100 seconds**.
- **No Native Background Workers on Free Tier:** Background worker processes on Render require a paid plan ($7/mo Starter).

### 4.2 Engineering Rules Enforced

1. **No Continuous Background Claims:** The system never claims to the operator that jobs run indefinitely in the background when the user closes their browser.
2. **Operator Polling as Keep-Alive:** While the operator holds the Leads Machine dashboard open, the frontend polls `/api/system/jobs` every 5–10 seconds. This traffic keeps the Render container awake for the duration of the discovery task.
3. **Safe Interruption Handling:** If the operator closes their browser tab or network disconnects, the container sleeps after 15 minutes. The job's heartbeat expires, and the ledger safely records it as `INTERRUPTED`.
4. **No Automated Cron / Wakeups:** `CRON=DISABLED` remains enforced. No automatic wakeups or headless schedules exist.

---

## 5. Workbook Schema Migration Tooling

### 5.1 Migration Command Specification

An idempotent, safe CLI tool was implemented: `scripts/setup_operational_sheets_schema.py`.
- **Default Behavior:** `--dry-run` (zero workbook modifications).
- **Live Application:** Requires explicit `--apply` flag.
- **Isolation:** Operates strictly on `OPERATIONAL_JOBS`. Never touches `LEADS`, `REVIEW_QUEUE`, or `RESEARCH_LOG`.

### 5.2 Dry-Run Verification Output

```bash
$ ./.venv/bin/python scripts/setup_operational_sheets_schema.py --dry-run
========================================================================
  DRIPP MEDIA — OPERATIONAL WORKSHEET MIGRATION
========================================================================
Operating Mode: DRY RUN (Inspecting without modifying)

[1/2] Connecting to Google Sheets CRM workbook...
  • Target Sheet ID: 1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc
  • Connected successfully.

[2/2] Checking worksheet 'OPERATIONAL_JOBS'...
  • Status: MISSING
  • Action: WOULD_CREATE_TAB
  • Details: Dry run: Would create worksheet 'OPERATIONAL_JOBS' with 20 columns.
  • Columns (20):
    1. run_id
    2. schema_version
    3. status
    4. territory
    5. industry
    6. stage
    7. requested_leads
    8. qualified_count
    9. candidates_evaluated
    10. candidate_cursor
    11. processed_keys_compact
    12. attempt_count
    13. error_code
    14. created_at
    15. updated_at
    16. last_heartbeat
    17. lease_holder
    18. lease_expires_at
    19. idempotency_hash
    20. recovery_metadata_json

========================================================================
  MIGRATION RESULT: DRY RUN COMPLETE
  No changes were written to the live Google Sheets CRM.
  To apply this migration, run with: --apply
========================================================================
```

---

## 6. Filesystem and Backup Dependencies Under Ephemeral Hosting

### 6.1 Ephemeral Filesystem Constraints

On Render Free and GCP Cloud Run, the container's local disk is ephemeral and is completely wiped whenever:
1. The container goes to sleep and wakes up.
2. A deployment or restart occurs.
3. The instance is rescheduled across host servers.

### 6.2 Backup & Restore Posture

| Component | Ephemeral Host Reality | Production Recommendation |
|:---|:---|:---|
| **Authoritative Lead Records** | Stored in Google Sheets CRM | Google Sheets automated version history provides point-in-time recovery |
| **Local Snapshot Archives (`data/backups/`)** | Ephemeral; destroyed on container restart | Operator runs `scripts/backup_crm_snapshot.py` locally before/after major operations |
| **Operational State** | Stored in Google Sheets `OPERATIONAL_JOBS` | Survives all container restarts and sleeping events |
| **External Cloud Storage (GCS/Drive)** | Not configured; no credentials for Drive API | Do NOT enable without operator approval |

---

## 7. Provider Comparison & Realistic Cost Exposure

| Parameter | Render Free | Google Cloud Run + Firestore | Railway | Koyeb Eco |
|:---|:---|:---|:---|:---|
| **Monthly Base Cost** | **$0.00 / month** | **$0.00 / month nominal** | **$5.00 / month** (No free tier) | **$0.00 / month** |
| **Credit Card Required?** | **NO** | **YES (Mandatory)** | YES | NO |
| **Free Compute Hours** | 750 hours/month | 360,000 vCPU-seconds | None | 550 hours/month |
| **Memory / CPU** | 512 MB / 0.1 vCPU | Up to 32 GB / 4 vCPU | 512 MB / shared | 512 MB / 0.1 vCPU |
| **Free Network Egress** | 100 GB / month | 1 GB / month free ($0.12/GB thereafter) | 100 GB / month | 50 GB / month |
| **Free Database Quota** | None (Uses Sheets API) | 50k reads, 20k writes/day | None | None |
| **Overage Behavior** | **Service suspends** | **Billed to credit card** | Shuts down at limit | Shuts down at limit |
| **Financial Risk** | **$0.00 (Zero)** | **Un-capped unless scripted** | $5.00/mo min | Exhausts in 23 days |

---

## 8. Verification & Test Suite Results

### 8.1 Test Suites Summary

1. **Phase 12.2.3 Zero-Cost Operational Ledger Suite (`test_phase_12_2_3_zero_cost_operational_ledger.py`):**
   - **17 / 17 PASSED** (Job lifecycle, checkpoints, stage interruptions, candidate dedup, concurrency conflict, stale job recovery, dry-run schema setup, GSheets API backoff, cold-start truthfulness, safety invariants).
2. **Phase 12.2.3 GCP Firestore Architecture Suite (`test_phase_12_2_3_gcp_firestore_architecture.py`):**
   - **14 / 14 PASSED** (Lease expiration, mutual exclusion, checkpoint persistence, CRM authority isolation, suppression persistence, Rule B criteria frozen).
3. **Phase 12.2.2 Pre-Deployment Audit Suite (`test_phase_12_2_2_pre_deployment_audit.py`):**
   - **15 / 15 PASSED** (Strict CORS origin enforcement, inbound webhook security, rate limiting, credential parsing, frozen locks).
4. **Phase 12.2.1 Security Gate Suite (`test_phase_12_2_1_security_gate.py`):**
   - **13 / 13 PASSED** (Authentication required on all private CRM endpoints, CSRF protection, session expiry, minimal health endpoint).
5. **Phase 12.2 Deployment Verification Suite (`test_phase_12_2_deployment_verification.py`):**
   - **13 / 13 PASSED** (Missing backend URL, error surfacing, mock prevention, historical lead protection).
6. **Total Phase 12.2 Test Suite:**
   - **72 / 72 PASSED (100% Pass Rate)** in 23.36s.

### 8.2 Security & Credential Scan

- Tool: `scripts/security_secret_scan.py`
- Result: **PASS (0 tracked credentials detected across 432 repository files)**.
- `.gitignore`: Properly protects all environment files, tokens, and logs.

### 8.3 Production Preflight Verification

- Tool: `scripts/production_preflight.py`
- Result: **20 / 20 Deterministic Gates PASSED**.
- Overall Verdict: **STAGING_READY**.

---

## 9. Blockers Requiring Explicit Operator Approval

Before any cloud deployment or live schema migration takes place, the operator must review and approve:

1. **Workbook Migration Approval:**
   - Operator approval is required to run `python scripts/setup_operational_sheets_schema.py --apply` to add the `OPERATIONAL_JOBS` worksheet (20 columns) to the live Google Sheets workbook (`1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc`).
2. **Hosting Provider Selection Sign-Off:**
   - Confirm adoption of **Render Free** ($0.00/month, zero credit card, zero overage risk) as the primary pilot backend target.
3. **Environment Variable Configuration Approval:**
   - Approve the set of environment variables to be supplied in the Render dashboard:
     * `GOOGLE_SERVICE_ACCOUNT_JSON`
     * `GOOGLE_SHEET_ID`
     * `OPERATOR_PASSWORD_HASH`
     * `JWT_SECRET`
     * `ALLOWED_ORIGINS=https://dripp-media-leads-machine.vercel.app`
     * `TRAVEL_MODE=ACTIVE`
     * `COMMERCIAL_ACTIONS_ENABLED=false`
     * `AUTOMATED_EMAIL_ENABLED=false`
     * `CRON=DISABLED`
4. **No Deployment Triggered:**
   - As mandated, **no cloud services have been created and no code has been deployed**. The codebase is fully code-ready for deployment upon operator authorization.
