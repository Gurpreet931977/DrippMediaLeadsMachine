# PHASE 12.2.5 — GCP ARCHITECTURE RECONCILIATION & COST REALITY REPORT

**Project:** Dripp Media Leads Machine  
**Phase:** 12.2.5  
**Selected Platform Architecture:** Google Cloud (Cloud Run Service + Cloud Run Jobs + Cloud Firestore)  
**Safety Regime:** Strict Operator-in-the-Loop (`TRAVEL_MODE=ACTIVE`, `COMMERCIAL_ACTIONS_ENABLED=false`, `AUTOMATED_EMAIL_ENABLED=false`, `CRON=DISABLED`, Rule B frozen at 8 criteria)  
**Deployment Status:** Design & Code-Readiness Complete — Zero Cloud Resources Created, Zero Billing Activated

---

## 1. Architecture Decision Record (ADR 2026-10-12)

### 1.1 Context
In Phase 12.2.3, two alternative architectures were evaluated: Render Free ($0/mo with Google Sheets state) and Google Cloud Run + Firestore. The operator has made the definitive decision:
**Google Cloud is selected as the primary backend architecture**, with an operational requirement to keep costs near zero.

### 1.2 Decision: Canonical Component Roles

```
                                    +-----------------------------------------+
                                    |         Vercel Static Frontend          |
                                    | (https://dripp-media-leads-machine.     |
                                    |              vercel.app)                |
                                    +--------------------+--------------------+
                                                         | HTTPS (Cookie/Bearer)
                                                         v
                                    +-----------------------------------------+
                                    |         Cloud Run Service (API)         |
                                    |       Authenticated FastAPI Server      |
                                    +---------+---------------------+---------+
                                              |                     |
                      Triggers batch execution|                     | Reads/Writes CRM
                                              v                     v
+-----------------------------------+  +---------------+  +-------------------+
|        Cloud Run Jobs             |  |Cloud Firestore|  |   Google Sheets   |
| Operator-triggered Discovery Task |  |  (Native Mode)|  | (Authoritative CRM|
| (Up to 24hr execution timeout)    |  +---------------+  |   for Business)   |
+-----------------+-----------------+  | Leases & Locks|  +-------------------+
                  |                    | Checkpoints   |  | LEADS             |
                  +------------------->| Pipeline Runs |  | REVIEW_QUEUE      |
                     Lease & State     | Suppressions  |  | RESEARCH_LOG      |
                                       +---------------+  +-------------------+
```

1. **Frontend:** Existing static UI hosted on Vercel (`https://dripp-media-leads-machine.vercel.app`).
2. **Authenticated API:** FastAPI backend hosted as a **Cloud Run Service** (request-driven compute with scale-to-zero when idle).
3. **Autonomous Discovery Tasks:** Executed as **Cloud Run Jobs** (`scripts/run_cloud_run_job.py`) triggered by the operator separately from HTTP requests, eliminating HTTP proxy timeouts.
4. **Canonical Operational State Store:** **Google Cloud Firestore (Native Mode)** (`lib/system/firestore_state_store.py`) is the **SOLE AUTHORITATIVE STORE** for distributed operational leases, atomic transaction locks, job lifecycle records, resumable stage checkpoints, and email suppression rules.
5. **Authoritative Business CRM:** **Google Sheets** remains the sole authoritative CRM for business entities (`LEADS`, `REVIEW_QUEUE`, `RESEARCH_LOG`). Business leads are NEVER migrated into Firestore.
6. **Role of Google Sheets Operational Ledger:** The `OPERATIONAL_JOBS` tab implemented in `lib/sheets/operational_ledger.py` acts as a **secondary human-inspectable audit view / export adapter**. It is **NOT** used for distributed locking or multi-container concurrency, which is strictly governed by Firestore transactions (`@firestore.transactional`).

---

## 2. Official Google Cloud Quotas, Limits & True Cost Analysis

### 2.1 Cloud Run Service: Request-Based vs Instance-Based Quotas

| Quota Dimension | Request-Based (Recommended) | Instance-Based (Always Allocated) | Free Tier Eligibility |
|:---|:---|:---|:---:|
### 2.1 Cloud Run Service: Request-Based vs Instance-Based Quotas & Unit Pricing

| Quota Dimension | Request-Based (Recommended) | Instance-Based (Always Allocated) | Free Tier Eligibility |
|:---|:---|:---|:---:|
| **CPU Allocation** | CPU allocated only during request processing | CPU allocated 100% of instance life | **Request-based only** |
| **Idle Behavior** | Scales to 0 instances after requests cease | Holds minimum instances active (`--min-instances 1+`) | Scale-to-zero required |
| **Monthly Free Requests** | **2,000,000 requests** / month | Shares 2M request quota | Tier 1 regions |
| **Monthly Free vCPU-seconds**| **360,000 vCPU-seconds** / month (~100 hrs of 1 vCPU) | Consumed continuously; exhausts 100 hrs in 4.1 days | Tier 1 regions |
| **Monthly Free Memory** | **180,000 GiB-seconds** / month (~50 hrs of 1 GiB) | Consumed continuously; exhausts in 2.1 days | Tier 1 regions |
| **Unit Cost Beyond Free Tier** | vCPU: **$0.00002400 / vCPU-sec** ($0.0864/hr)<br>Memory: **$0.00000250 / GiB-sec** ($0.009/hr)<br>Requests: **$0.40 / million requests** | Continuous per-instance billing at same rates | Tier 1 regions (`us-central1`) |
| **Cold Start** | 1 to 3 seconds | Sub-millisecond (always warm) | — |

> **OPERATING DIRECTIVE:** The Cloud Run Service must be deployed with `--min-instances 0` and `--cpu-throttling` (request-based compute) to ensure 100% qualification for the free compute allowance during idle periods.

### 2.2 Cloud Run Jobs Quotas & Limits

- **Task Execution Timeout:**
  - Default: **10 minutes (600 seconds)**.
  - Maximum Configurable Timeout: Up to **24 hours (86,400 seconds)** per task (compared to 60 minutes max for Cloud Run Service, and 100 seconds max for Render HTTP proxy).
- **Billing Model:** Charged for CPU and memory allocated during active task execution at Tier 1 rates ($0.00002400/vCPU-s, $0.00000250/GiB-s), sharing the monthly 360,000 vCPU-s and 180,000 GiB-s free pool.
- **Concurrent Task Limit:** Default 100 concurrent tasks per region. Dripp Media enforces a single-instance lease in Firestore (`holder_id`), guaranteeing active concurrency = 1.

### 2.3 Cloud Firestore (Native Mode) Quotas & Unit Pricing

- **Stored Data:** **1 GiB** stored data free per billing account ($0.18 / GiB / month thereafter).
- **Daily Document Reads:** **50,000 reads** per day free ($0.06 per 100,000 reads thereafter).
- **Daily Document Writes:** **20,000 writes** per day free ($0.18 per 100,000 writes thereafter).
- **Daily Document Deletes:** **20,000 deletes** per day free ($0.02 per 100,000 deletes thereafter).
- **Network Transfer:** 10 GiB/month outbound data transfer free within Google Cloud.

### 2.4 Ancillary Dependencies & Potential Cost Exposure

| Service Dependency | Free Tier Allowance | Overage Cost Beyond Free Tier | Risk Mitigation Rule |
|:---|:---|:---|:---|
| **Artifact Registry** | **0.5 GB** storage free / month | **$0.10 / GB / month** | Apply automated cleanup policy keeping only latest 2 image digests (~300 MB). |
| **Cloud Build** | **120 build-minutes** / day free (`e2-standard-2`) | **$0.003 / minute** | Builds take ~2 min; 1-2 deployments/week consumes < 1% of daily free allowance. |
| **Network Egress** | **1 GB** worldwide egress / month free | **$0.12 / GB** (Tier 1) | Response payloads are JSON API only (< 10 MB/month total). |
| **Cloud Logging** | First **50 GiB** log ingestion free / month | **$0.50 / GiB** | Set log level to `INFO` in production; suppress debug payloads. |

### 2.5 Hard Spending Cap Reality & Possibility of Overage

> [!WARNING]
> **Free-tier allowances are NOT a guaranteed $0 bill. GCP does NOT offer a native hard spending cap.**  
> Google Cloud Billing Budgets and alerts **DO NOT** stop running services or cap billing when a threshold is crossed. Budgets are purely asynchronous alert mechanisms that send emails or trigger Pub/Sub topics.  
> If an unexpected traffic surge, infinite retry loop, or denial-of-service event occurs, Google Cloud will continue processing requests and automatically charge the linked credit card.  
> Even with an automated programmatic detacher (`Cloud Budget -> Pub/Sub -> Cloud Function -> update_project_billing_info(name, billing_account_name="")`), project resource teardown takes several minutes, during which small overage charges can still accrue. **Overage risk is non-zero.**

### 2.6 Correction of Render Free Assumptions

Render Free Web Services do not require a credit card and suspend when bandwidth limits are reached, but they do NOT provide a contractual SLA. Render Free instances sleep after 15 minutes of inactivity, introduce 30–50 second cold starts, terminate HTTP requests at 100 seconds, and free plan terms remain subject to provider modification. Render is retained strictly as an alternative reference architecture. Google Cloud is the selected target.

### 2.7 Firestore Database Location Identifiers: Regional `us-central1` Preferred over `nam5`

> [!IMPORTANT]
> **`nam5` and `us-central1` are NOT interchangeable names.**
> - **`nam5`** is a **Multi-Region location identifier** (United States multi-region, replicating across multiple data centers including Iowa and South Carolina).
> - **`us-central1`** is a **Regional location identifier** (Iowa single data center).
>
> **DEPLOYMENT RECOMMENDATION: Co-located Regional `us-central1`**
> - Both qualify for the Firestore 1 GiB free tier and 50k reads/20k writes per day.
> - **Co-locating both Cloud Run and Firestore in regional `us-central1` is strictly preferred**:
>   1. **Lowest Latency:** Intra-datacenter round-trips (< 5ms) for transactional lease acquisition and checkpoint persistence.
>   2. **Zero Cross-Region Egress Fees:** Eliminates multi-region egress costs that can accrue between regional compute and multi-region storage.
>   3. **Tier 1 Free Quota Eligibility:** Qualifies for the maximum Tier 1 compute and request allowances.

---

## 3. Production Fail-Closed Behavior Verification

The production runtime has been explicitly hardened in [`firestore_state_store.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/lib/system/firestore_state_store.py):

1. **Strict Production Client Requirement:**
   When `ENVIRONMENT=production` or `APP_ENV=production` (or `FIRESTORE_ENABLED=true`):
   - The runtime strictly requires a live `google.cloud.firestore.Client`.
   - If the SDK is missing, credentials are invalid, or network is unreachable, it raises `RuntimeError` immediately.
   - **Silent fallback to in-memory mock state is strictly prohibited.**
2. **Atomic Distributed Leases:**
   - Mutual exclusion is enforced via Firestore transactional operations (`@firestore.transactional`).
   - If Worker A holds lease `discovery_job_runner`, Worker B's transaction detects `expires_epoch > now` and aborts with `False`.
3. **Stale Lease Recovery:**
   - Implemented `reconcile_stale_leases()`: If a worker crashes or container terminates, an un-renewed lease expires (`expires_epoch < now`). The next worker safely takes over the lease via transaction, or reconciles the expired record.
4. **Resumable Checkpoints:**
   - Checkpoints (`job_checkpoints` collection) persist `run_id`, `stage`, `candidate_cursor`, `processed_ids`, and `qualified_count`.
   - On container restart, the job retrieves the checkpoint and resumes without re-evaluating processed candidates or duplicating CRM rows.

---

## 4. Reversible Suppression & Unsubscribe Migration Procedure

Implemented [`migrate_suppressions_to_firestore.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/scripts/migrate_suppressions_to_firestore.py):

- **Default Mode:** Defaults to `--dry-run`. Zero database or filesystem mutations occur without explicit `--apply`.
- **Directional Reversibility:**
  - Forward: `--direction to-firestore` migrates local JSON records to Firestore collections (`email_suppressions`, `email_unsubscribes`).
  - Reversal: `--direction from-firestore` exports Firestore collections back to local JSON files.
- **Integrity Verification:** Compares source counts against destination counts, ensuring 100% record retention with zero dropped items.
- **Audit Verification:** Tested against live files in dry-run mode:
  - 3 suppressions and 95 unsubscribes inspected with zero mutations.
  - Tested with synthetic temporary fixtures across forward migration, count assertion, and round-trip reversal.

---

## 5. Verification & Test Results

### 5.1 Test Suites Summary

| Test Suite | File | Tests Run | Result | Key Capabilities Verified |
|:---|:---|:---:|:---:|:---|
| **GCP Firestore Architecture** | [`test_phase_12_2_3_gcp_firestore_architecture.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/test_phase_12_2_3_gcp_firestore_architecture.py) | **21 / 21** | **PASS** | Fail-closed in production, `nam5` vs `us-central1`, stale lease recovery, synthetic suppression migration round-trip, pipeline run synchronization |
| **Operational Ledger** | [`test_phase_12_2_3_zero_cost_operational_ledger.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/test_phase_12_2_3_zero_cost_operational_ledger.py) | **17 / 17** | **PASS** | Checkpoints, heartbeats, concurrency conflict, stale job recovery, dry-run schema setup, GSheets backoff |
| **Pre-Deployment Audit** | [`test_phase_12_2_2_pre_deployment_audit.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/test_phase_12_2_2_pre_deployment_audit.py) | **15 / 15** | **PASS** | Strict CORS enforcement, inbound webhook HMAC security, rate limiting, credential parsing |
| **Security Gate** | [`test_phase_12_2_1_security_gate.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/test_phase_12_2_1_security_gate.py) | **13 / 13** | **PASS** | Private CRM endpoints require authentication, CSRF origin check, session expiry, minimal health endpoint |
| **Deployment Verification**| [`test_phase_12_2_deployment_verification.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/test_phase_12_2_deployment_verification.py) | **13 / 13** | **PASS** | Missing backend URL handling, truthful error surfacing, mock prevention, historical lead protection |
| **Total Phase 12.2 Suite** | All `test_phase_12_2_*.py` | **79 / 79** | **PASS** | **100% Pass Rate in 17.66s** |

### 5.2 Deterministic Preflight & Security Scans

- **Security Secret Scanner ([`security_secret_scan.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/scripts/security_secret_scan.py)):** **PASS (0 tracked credentials across 436 files)**.
- **Production Preflight ([`production_preflight.py`](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/scripts/production_preflight.py)):** **20 / 20 Gates PASSED (`STAGING_READY`)**.

---

## 6. Immutable Safety Invariants

The following safety controls remain strictly active in code and configuration:
- `TRAVEL_MODE=ACTIVE`
- `COMMERCIAL_ACTIONS_ENABLED=false`
- `AUTOMATED_EMAIL_ENABLED=false`
- `CRON=DISABLED`
- Rule B qualification frozen at exactly 8 criteria (`RULE_B_VERSION="FROZEN"`).

---

## 7. Approval Boundaries Before Cloud Deployment

Per operator instructions, **no cloud resources have been provisioned and no billing has been activated.**

To proceed with actual Google Cloud provisioning in subsequent phases, operator approval is required for:
1. **Google Cloud Project & Billing Account Setup:**
   - Authorizing creation of or connection to GCP Project `dripp-media-leads`.
   - Linking a billing account (with required budget threshold alert).
2. **Firestore Database Creation:**
   - Authorizing creation of Firestore Database `(default)` in Native Mode in location `nam5` (or `us-central1`).
3. **Artifact Registry & Cloud Build:**
   - Authorizing creation of an Artifact Registry repository (e.g. `dripp-backend` in `us-central1`) with a retention policy of 2 image digests.
4. **Cloud Run Service & Cloud Run Job Creation:**
   - Authorizing deployment of the Cloud Run Service (FastAPI) and Cloud Run Job (Discovery runner).
5. **Suppression Migration:**
   - Authorizing execution of `python scripts/migrate_suppressions_to_firestore.py --apply` to populate live Firestore suppressions.
