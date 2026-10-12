# PHASE 12.2.3 — ZERO-COST HOSTING ARCHITECTURE AUDIT & PERSISTENCE REDESIGN

**Document Version:** 1.0.0  
**Target Monthly Budget:** **$0.00 / month (Zero Hosting & Cloud Spend)**  
**Safety Operating Regime:** STRICT OPERATOR-IN-THE-LOOP (`TRAVEL_MODE=ACTIVE`, Commercial Actions Locked, Automated Email Disabled, Cron Disabled, Rule B Frozen)  
**Evaluated Architectures:**
1. **Option 1: Render Free Web Service + Google Sheets-Backed Resumable Persistence**
2. **Option 2: Google Cloud Run (Services & Jobs) + Cloud Firestore NoSQL**

---

## 1. Executive Summary & Core Verdict

The operator has mandated keeping the backend hosting target at **$0.00/month** without creating paid cloud resources, enabling billing accounts, or incurring credit card overage risks.

| Evaluation Dimension | Option 1: Render Free + Google Sheets-Backed State | Option 2: GCP Cloud Run Jobs + Cloud Firestore |
|:---|:---|:---|
| **Monthly Financial Cost** | **$0.00 / month** | **$0.00 / month (nominal)** |
| **Credit Card / Billing Account Requirement** | **NO CREDIT CARD REQUIRED** | **MANDATORY CREDIT CARD REQUIRED** to enable GCP APIs |
| **Financial Overage Risk** | **ZERO ($0.00 hard cap)** — Render suspends free tier at limit; Google Sheets throttles HTTP 429 rather than billing | **NON-ZERO RISK** — Runaway loops, DDoS, or egress spikes automatically bill the linked card |
| **Persistent Disk Availability** | None (Filesystem is ephemeral on container restart/sleep) | None (Stateless containers; state stored in Firestore) |
| **State Persistence Mechanism** | **Google Sheets-Backed Read/Write-Through Cache** (`storage_bootstrap.py` + atomic sheet tabs) | **Firestore NoSQL Collections** (`suppressions`, `jobs`, `rate_limits`) |
| **Container Lifecycle & Sleep** | Sleeps after 15 min idle; wakes on HTTP request in 30–50s | Scales to zero; sub-second to 3s cold start |
| **Long-Running Job Feasibility** | HTTP background tasks terminated if container sleeps; needs keep-alive or chunked jobs | Cloud Run Jobs execute independently up to 24 hours |
| **Readiness for Pilot** | **IMMEDIATE (Uses existing Google Service Account credentials)** | Requires GCP project setup, IAM, Artifact Registry, billing linkage |

### Verdict & Strategic Recommendation
For a pilot deployment with commercial actions locked:
- **Adopt Option 1 (Render Free + Google Sheets-Backed Persistence)** as the primary $0/month deployment architecture. It requires **no credit card**, incurs **zero overage risk**, reuses the already authenticated Google Service Account, and safeguards all state items across container restarts.
- **Reserve Option 2 (GCP Cloud Run + Firestore)** for future production scaling if and when commercial sending volumes exceed Google Sheets API throughput quotas.

---

## 2. Detailed Technical Comparison: Quotas, Limits & Risks

### 2.1 Quota & Free Tier Specifications (2026 Verification)

```
========================================================================================
RESOURCE / METRIC       OPTION 1: RENDER FREE TIER      OPTION 2: GCP FREE TIER
========================================================================================
Compute CPU             0.1 vCPU (shared)               Up to 4 vCPUs per container instance
Compute Memory          512 MB RAM                      Up to 32 GB RAM per instance
Free Monthly Compute    750 instance hours / month      360,000 vCPU-seconds (~100 hrs of 1vCPU)
                                                        180,000 GiB-seconds memory
Free Monthly Requests   Unlimited within compute/hours  2,000,000 requests / month
Network Egress          100 GB / month free             1 GB / month free (then $0.12/GB)
Container Sleep         Sleeps after 15m idle HTTP      Scales to 0; wakes on demand (1-4s)
HTTP Request Timeout    100 seconds max (Render proxy)  60 minutes max (Cloud Run Service)
Job Execution Timeout   15 minutes (unless HTTP kept)   24 hours max (Cloud Run Job)
Database / State Store  Google Sheets API (Free)        Google Cloud Firestore (Native Mode)
Database Free Quota     300 read & 300 write reqs / min 50k reads, 20k writes, 20k deletes / day
                        10 million cells / sheet        1 GiB storage free
Credit Card Required?   NO (Free forever on email)      YES (GCP requires active billing account)
Over-Quota Behavior     Service sleeps until next cycle Automatically bills linked credit card
========================================================================================
```

### 2.2 Deep Dive: Overage Risks & Runaway Loop Analysis

#### Option 1 (Render Free + Sheets):
- **Overage Exposure: $0.00 Absolute Guarantee.**
  - Render Free accounts do not have credit card details on file. If the 750-hour limit or 100 GB bandwidth is exhausted, Render simply pauses the web service until the first day of the next calendar month.
  - Google Sheets API enforces hard rate limits (300 requests/minute). If the application makes too many requests, Google returns HTTP 429 (`RESOURCE_EXHAUSTED`). It never bills for Google Sheets API calls.
  - **Worst-case failure mode:** Temporary service suspension or HTTP 429 throttling. Zero monetary liability.

#### Option 2 (Google Cloud Run + Firestore):
- **Overage Exposure: Uncapped Variable Invoicing.**
  - To enable Cloud Run and Firestore, Google Cloud demands a linked payment method (credit card).
  - Even if budgets and alerts are configured at $0.01, GCP budgets **do NOT halt running containers by default**. An automated spending kill-switch requires a custom Cloud Billing Pub/Sub topic and a Cloud Function to disable project billing.
  - **Runaway Scenarios:**
    1. A discovery job gets stuck in an infinite retry loop: 24 hours of 4 vCPUs running = ~345,600 vCPU-sec, burning through the entire monthly tier in a single day and triggering hourly billing.
    2. Large lead payload downloads or repeated HTML scrapes exceed 1 GB egress: billed at $0.12 per GB.
    3. Firestore indexing loops or unbounded event listeners exceeding 20k writes/day: billed at $0.18 per 100,000 document writes.
  - **Worst-case failure mode:** Unexpected charges on the operator's credit card.

---

## 3. Ephemeral Storage Audit & The Persistence Gap

On Render Free, the local Docker container filesystem (`/app/data`) is **ephemeral**. When the container spins down due to 15 minutes of inactivity or receives a git deployment:
- All files written to `/app/data` are erased.
- The next request spins up a pristine container from the Docker image.

### State Item Audit & Recovery Strategy

| File / Component | Purpose | Current Format | Ephemeral Risk | Redesigned Zero-Cost Persistence Solution |
|:---|:---|:---|:---:|:---|
| **CRM Leads** | Canonical qualified leads | Google Sheets `LEADS` tab | None | Already persisted in Google Sheets CRM. Read on startup. |
| **Review Queue** | Leads awaiting manual review | Google Sheets `REVIEW_QUEUE` | None | Already persisted in Google Sheets CRM. |
| **Research Log** | Discovery & scraping audit | Google Sheets `RESEARCH_LOG` | None | Already persisted in Google Sheets CRM. |
| **Email Suppressions** | Hard bounces & manual opt-outs | `data/email_suppression.json` | **CRITICAL** | **Sync to Google Sheets tab `SUPPRESSIONS`** on mutation; restore on startup. |
| **Email Unsubscribes** | One-click unsubscribe tokens | `data/email_unsubscribes.json`| **CRITICAL** | **Sync to Google Sheets tab `UNSUBSCRIBES`** on mutation; restore on startup. |
| **Rate Limit Counters**| Rolling 24h email send counts | `data/email_rate_limits.json` | **HIGH** | **Sync to Google Sheets tab `RATE_LIMITS`** or reconstruct from Google Sheets timestamps. |
| **Idempotency Logs** | Prevent duplicate email sends | `data/email_idempotency_log.json` | **HIGH** | Reconstructed from `outreach_sent_at` column in `LEADS` tab. |
| **Pipeline Runs** | Discovery run history & stats | `data/pipeline_runs/*.json` | **MEDIUM** | **Sync to Google Sheets tab `PIPELINE_RUNS`** upon completion or error. |
| **Job Checkpoints** | In-progress discovery state | `data/job_checkpoints/*.json` | **MEDIUM** | **Sync to Google Sheets tab `JOB_CHECKPOINTS`** after each batch. |
| **Operator Sessions** | Login session tokens | In-memory `operator_sessions` | Low | Re-login required after container sleep. Expected and secure. |

---

## 4. Persistence Redesign: Google Sheets-Backed State Provider

To achieve 100% data persistence at $0/month on Render Free without a paid disk, we redesign the persistence layer using a **Dual-Tier Cache Pattern**:
1. **Tier 1 (Local Ephemeral Cache):** Blazing-fast in-memory and atomic local JSON reads/writes at `/app/data/`.
2. **Tier 2 (Google Sheets Backing Store):** Asynchronous and on-mutation write-through to dedicated tabs within the existing `Dripp Media Leads CRM` Google Spreadsheet.
3. **Startup Hydration:** `lib/system/storage_bootstrap.py` executes on application boot. If `/app/data/` lacks suppression or run history, it automatically hydrates from Google Sheets.

```
+--------------------------------------------------------------------------------------------------+
|                   PERSISTENCE REDESIGN ARCHITECTURE (DUAL-TIER RECOVERY)                         |
+--------------------------------------------------------------------------------------------------+
|                                                                                                  |
|   CONTAINER BOOT (Cold Start)                                                                    |
|   +------------------------------------------------------------------------------------------+   |
|   | 1. storage_bootstrap.py runs                                                             |   |
|   | 2. Checks Google Sheets CRM via GOOGLE_SERVICE_ACCOUNT_JSON                              |   |
|   | 3. Hydrates local /app/data/ from Google Sheets:                                         |   |
|   |    • Tab 'LEADS'           -> /app/data/cache_sheets_leads.json                          |   |
|   |    • Tab 'SUPPRESSIONS'    -> /app/data/email_suppression.json                           |   |
|   |    • Tab 'UNSUBSCRIBES'    -> /app/data/email_unsubscribes.json                          |   |
|   |    • Tab 'JOB_CHECKPOINTS' -> /app/data/job_checkpoints/                                 |   |
|   |    • Tab 'RATE_LIMITS'     -> /app/data/email_rate_limits.json                           |   |
|   +------------------------------------------------------------------------------------------+   |
|                                                                                                  |
|   RUNTIME OPERATIONS (Read Fast, Write Through)                                                  |
|   +----------------------------------+          +--------------------------------------------+   |
|   | FAST IN-MEMORY / LOCAL DISK READ |          | MUTATION OCCURS (e.g. Unsubscribe / Bounce)|   |
|   | • Instant response (<5ms)        |          | 1. Atomic write to local JSON file         |   |
|   | • Serves dashboard & checks      |          | 2. Background queue writes to Google Sheet |   |
|   +----------------------------------+          +--------------------------------------------+   |
|                                                                                                  |
|   CONTAINER SLEEP / REDEPLOY                                                                     |
|   Container shuts down -> Local disk wiped -> Next request boots -> Hydrates from Sheets (SAFE!) |
+--------------------------------------------------------------------------------------------------+
```

### 4.1 Schema for Google Sheets Backing Store Tabs

The existing spreadsheet `1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc` will support four additional state tabs managed by the backend:

#### 1. `SUPPRESSIONS` Tab
Columns: `suppression_id`, `suppression_type`, `target`, `reason`, `created_at`, `metadata_json`

#### 2. `UNSUBSCRIBES` Tab
Columns: `unsubscribe_token`, `lead_id`, `email`, `campaign_id`, `created_at`, `unsubscribed`, `unsubscribed_at`, `reason`

#### 3. `JOB_CHECKPOINTS` Tab
Columns: `run_id`, `market_id`, `status`, `started_at`, `finished_at`, `discovered_count`, `processed_ids_json`, `failed_ids_json`, `stats_json`

#### 4. `RATE_LIMITS` Tab
Columns: `date_utc`, `provider`, `category`, `daily_count`, `hourly_count`, `last_sent_at`

### 4.2 Handling Google Sheets API Quotas (300 requests/minute)
- **Batching:** Checkpoint updates are flushed after batch increments (every 10 candidates), not on every single item.
- **Throttling:** Background sync queue uses a minimum 1.5-second debounce window to prevent bursts.
- **Total Quota Usage:** Even during active discovery, the persistence layer generates fewer than 8 write requests per minute, well beneath the 300 requests/minute Google quota.

---

## 5. Job Recovery & Keep-Alive Strategy for Render Free

### The 15-Minute Render Idle Constraint
Render Free suspends a web service container if no incoming HTTP traffic is detected for **15 minutes**.

### Mitigation Strategy:
1. **Frontend Active Polling:**  
   When an operator triggers a search from the Vercel frontend, `static/index.html` connects to `/api/stream` (Server-Sent Events) and polls `/api/status` every 2 seconds. This inbound traffic guarantees the container **never sleeps while the operator has the dashboard open**.
2. **Chunked Resumable Execution:**  
   Discovery is split into discrete batches of 10 candidates. After each batch, the state is persisted to `JOB_CHECKPOINTS` in Google Sheets.
3. **Graceful Disconnect Recovery:**  
   If the operator closes their laptop and the container shuts down after 15 minutes:
   - On the next dashboard visit, the container boots up.
   - `storage_bootstrap.py` loads the checkpoint from the `JOB_CHECKPOINTS` tab in Google Sheets.
   - The dashboard displays the run as `INTERRUPTED_CAN_RESUME` rather than corrupted or lost.
   - The operator can click **"Resume Run"** to pick up exactly where execution left off without duplicating discovered candidates or burning extra search queries.

---

## 6. Implementation Plan for Zero-Cost Pilot

### Phase 1: Persistence Adapter Code (Zero Cost, Zero Cloud Resources)
1. Add `lib/sheets/sheets_state_store.py` implementing read-through and write-through methods for suppressions, unsubscribes, and checkpoints.
2. Update `storage_bootstrap.py` to hydrate `/app/data/` from Google Sheets tabs if local files are missing.
3. Update `lib/outreach/email_suppression.py` to notify `sheets_state_store` on new suppressions and unsubscribes.
4. Update `render.yaml` to specify `plan: free` and remove the persistent `disk:` block.

### Phase 2: Staging Validation & Pilot Deployment
1. Verify end-to-end hydration with automated unit and mock tests.
2. Deploy the backend to Render Free ($0/month, no credit card).
3. Verify that Vercel frontend talks to the Render Free backend and all data persists across deliberate container reboots.
4. Keep all commercial sending locks, automated email locks, and cron locks frozen.

---

## 7. Operator Sign-Off Request

Please review the comparison and confirm if you approve proceeding with **Phase 1 of the Zero-Cost Persistence Redesign (Google Sheets-backed resumable state on Render Free at $0/month)**.

* **No cloud resources have been created.**
* **No billing accounts have been enabled.**
* **No changes have been made to the live CRM sheet.**
* **All security invariants remain frozen.**
