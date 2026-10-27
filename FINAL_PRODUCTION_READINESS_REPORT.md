# Dripp Media Leads Machine: Final Production Readiness & Verification Report

**Date:** October 27, 2026  
**Local Workspace:** `/Users/metagurpreet/Desktop/Dripp International Leads`  
**Git Working Branch:** `feature/live-discovery-pipeline-readiness`  
**Git HEAD Commit:** `097f77f` (`fix(deploy): resolve dynamic image digest and safeguard traffic promotion`)  
**Previous Commits:** `da1bd5e` (`feat(sheets): implement isolated TEST_ONLY test namespace, proxy guard, and unit tests`), `ce0d84e` (`fix(discovery): repair provider resilience, budget fallbacks, and validation for live discovery`)  
**Deployment Region / GCP Project:** `us-central1` / `poised-eye-509816-a4`  
**Canonical Google Spreadsheet:** `1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc` ("Dripp Media International Leads")  

---

## 1. Executive Status Dashboard & Verdict Summary

Each operational gate has been audited against cryptographic, runtime, and empirical evidence. No assertions are made on assumptions or in-memory mocks alone.

| Subsystem / Operational Area | Verdict | Evidence & Rationale |
| :--- | :---: | :--- |
| **1. Local Discovery & Qualification** | **PASS** | Genuine multi-provider discovery (OSM Overpass with 22s hard budget, 120s cooldowns, automatic failover to Tavily). Strict boundary validation for Manchester, UK. Confirmed domain verification and truthful exclusion (`WEBSITE_EXISTS`) without synthetic injection. |
| **2. Real Google Sheets Persistence** | **PASS** | Dedicated `TEST_ONLY_*` namespace implemented in existing canonical spreadsheet. 10/10 live API persistence gates passed (`scratch/run_test_namespace_sheets_integration.py`): actual write, read-back, schema width, and in-place updates verified against live Google Sheets API. |
| **3. Duplicate Handling & Recovery** | **PASS** | 0 new rows created upon reprocessing identical candidate; updated in-place with existing `lead_id` strictly preserved. Multi-signal identity matcher verified across 37 test cases in `test_search_circuit_and_crm.py`. |
| **4. Production CRM Isolation** | **PASS** | All 5 production operational tabs (`OVERVIEW`, `LEADS`, `RESEARCH_LOG`, `REVIEW_QUEUE`, `RESEARCH_LOG_QUARANTINE`) remained 100% untouched before, during, and after live testing. Pre- and post-run cryptographic SHA-256 hashes and row counts match 100%. |
| **5. API & Operator Dashboard** | **PASS** | Live Cloud Run service `dripp-leads-api` at `https://dripp-leads-api-ukng7vge6q-uc.a.run.app` verified healthy (HTTP 200, `OPERATIONAL`). Unauthenticated requests to protected endpoints (`/api/leads`, `/api/crm/status`, etc.) strictly fail closed with HTTP 401. Dashboard HTML delivers HTTP 200. |
| **6. CI/CD Deployment Workflow & Digest Verification** | **PASS (FIXED & VALIDATED)** | Stale hardcoded image digest (`sha256:991bb03c...`) and tag (`123ee3b...`) eliminated. Created dynamic resolver `scripts/resolve_deployment_artifact.py`. Changed `promote_traffic` default to `false`. Verified across 29 focused tests and 78 total deployment verification tests. |
| **7. Cloud Run Discovery-Job Release Alignment** | **PENDING GITHUB MERGE/DISPATCH** | Deployed Cloud Run Job `dripp-discovery-job` in GCP is currently pinned to commit `123ee3bbce9b1207a50a7a6c8828cb24014c5ce2`. Local commits (`ce0d84e`, `da1bd5e`, and workflow fix) exist locally on branch `feature/live-discovery-pipeline-readiness` and must be pushed/merged to GitHub to trigger the release. |
| **8. Automated Outreach Safeguards** | **PASS (MUST REMAIN DISABLED)** | Commercial outreach remains strictly disabled across all code paths: `TRAVEL_MODE=ACTIVE`, `COMMERCIAL_ACTIONS_ENABLED=false`, `AUTOMATED_EMAIL_ENABLED=false`. Exactly 0 emails, DMs, or commercial proposals dispatched. |

---

## 2. Deployment Workflow Defect and Fix Details

### 2.1 The Stale Image-Digest Defect
- **Root Cause Analysis:** In `.github/workflows/05-deploy-cloud-run.yml`, line 273 contained a hardcoded image digest:
  ```bash
  EXPECTED_DIGEST="sha256:991bb03c169edd467eb3c4c8548f6907f5d749d5ca69f6a800541d173812cc89"
  ```
  Additionally, line 125 hardcoded `DEPLOY_TAG="123ee3bbce9b1207a50a7a6c8828cb24014c5ce2"` for promote-only candidate runs.
- **Impact:** Because every new GitHub Actions release builds and tags a container image from the current commit SHA (e.g. `da1bd5e`), Cloud Run records the actual sha256 digest of that newly built artifact. When the post-deployment read-back check executed, it compared the new artifact's digest against the hardcoded digest from commit `123ee3b`, causing immediate failure and blocking all deployments of new releases.
- **Vulnerability / Risk:** In candidate-only runs, hardcoding `123ee3b` caused any candidate revision from any other commit to be verified against the wrong tag or artifact. Furthermore, if an operator attempted to promote a candidate, a stale digest check created confusion and risk of artifact mismatches.

### 2.2 The Fix: Dynamic Immutable Artifact Resolution (`scripts/resolve_deployment_artifact.py`)
Rather than replacing one hardcoded digest with another, a fail-closed, dynamic artifact resolution tool was implemented:
1. **Standard Build & Deploy Path (`resolve-built`):**
   - Extracts the exact sha256 digest from Docker's local `RepoDigests` after `docker push`.
   - If not available locally, queries Google Artifact Registry directly (`gcloud artifacts docker images describe --format=json`).
   - Validates that the digest matches strict regex `^sha256:[a-f0-9]{64}$`.
   - Validates that the base image matches the authorized repository (`us-central1-docker.pkg.dev/poised-eye-509816-a4/dripp-media/dripp-discovery-job`).
   - Persists the resolved identity to `$RUNNER_TEMP/artifact_resolution.json` and outputs `expected_digest`, `expected_image`, and `immutable_reference` to `$GITHUB_OUTPUT`.
2. **Promote-Only Candidate Path (`resolve-candidate`):**
   - Retrieves the Cloud Run revision JSON for the candidate revision.
   - Extracts the candidate's container image and validates that it points to the authorized repository.
   - Extracts `status.imageDigest` from the candidate revision and verifies it against Artifact Registry.
   - Fails closed if the candidate is unapproved, lacks a valid digest, or points to an unauthorized image.
3. **Read-Back Verification (`verify-candidate-digest`):**
   - Reads back `status.imageDigest` from the deployed Cloud Run revision.
   - Compares the deployed digest against the dynamically resolved `expected_digest`.
   - Fails closed if there is any digest mismatch, stale artifact, or missing container spec.

### 2.3 Traffic Promotion Protection & Separation of Concerns
In `.github/workflows/05-deploy-cloud-run.yml`:
1. **Safe Default for Traffic Promotion:**
   - Changed `promote_traffic` input definition from `default: "true"` to `default: "false"`.
   - An operator must now **explicitly opt-in** by passing `promote_traffic: true` to shift 100% production traffic.
2. **Clear Separation of Workflow Phases:**
   - **Candidate API Deployment:** Deploys new revision with `--no-traffic` and assigns candidate tag (`candidate`). Verifies readiness and authenticated health endpoints at zero production traffic.
   - **Discovery Job Deployment:** Updates `dripp-discovery-job` configuration with the new image reference and validates environment variables. Does **not** trigger or execute the job.
   - **Traffic Promotion (Gated):** Executes `gcloud run services update-traffic $API_SERVICE --to-revisions=$CANDIDATE_REVISION=100` **only** if `${{ github.event.inputs.promote_traffic == 'true' }}`.
   - **Zero-Traffic Completion Step:** When `promote_traffic` is `false`, the workflow explicitly confirms zero-traffic deployment completion, reports candidate revision details and candidate URL, and provides the exact rerun parameters for operator promotion.
3. **Preservation of Safety Gates:**
   - Preserved `confirm_deployment` input check (`CONFIRM_DEPLOY_TO_PRODUCTION` or `CONFIRM_PROMOTE_API_TO_PRODUCTION`).
   - Preserved preflight audit, durable GCS backup, Playwright post-promotion tests, and automatic rollback to previous primary revision on failure.
   - Preserved commercial safety locks: `TRAVEL_MODE=ACTIVE`, `COMMERCIAL_ACTIONS_ENABLED=false`, `AUTOMATED_EMAIL_ENABLED=false`.

---

## 3. Test Suite & Local Verification Results

### 3.1 Artifact Resolution & Safeguard Unit Tests
**Command:**
```bash
.venv/bin/pytest test_resolve_deployment_artifact.py -v
```
**Results:** **29 passed in 0.04s**
- `TestDigestFormatValidation`: Validates format regex, rejects truncated, uppercase, MD5, SHA-1, and empty strings.
- `TestAuthorizedImageBaseValidation`: Validates authorized GCP artifact registry prefix; rejects unauthorized registries and projects.
- `TestDigestExtraction`: Validates extraction from Docker repo digests, gcloud JSON summaries, and raw text.
- `TestResolveBuiltArtifact`: Validates local repo digest resolution, Artifact Registry fallback, and fail-closed error handling.
- `TestResolveCandidateArtifact`: Validates candidate revision inspection, registry verification, and unauthorized image rejection.
- `TestVerifyCandidateRevisionDigest`: Validates matching digest pass, stale digest rejection, and missing digest fail-closed handling.
- `TestWorkflowFileSafeguards`: Validates YAML syntax, verifies `promote_traffic` defaults to `false`, verifies complete removal of stale hardcoded digests/tags, and ensures traffic promotion requires explicit opt-in.

### 3.2 Full Deployment Verification Suite
**Command:**
```bash
.venv/bin/pytest test_resolve_deployment_artifact.py test_cloud_run_api_service_deployment.py test_cloud_run_api_candidate_verification.py test_phase_12_2_deployment_verification.py test_phase_12_2_1_security_gate.py -v
```
**Results:** **78 passed, 0 failed, 7 starlette cookie deprecation warnings in 12.64s**
- `test_resolve_deployment_artifact.py`: 29 passed.
- `test_cloud_run_api_service_deployment.py`: 19 passed (candidate tagging, image pinning, memory limits, zero-weight verification).
- `test_cloud_run_api_candidate_verification.py`: 4 passed (health, auth, readiness, fail-closed on blockers).
- `test_phase_12_2_deployment_verification.py`: 13 passed (commercial safety restrictions, CRM integrity, error resilience).
- `test_phase_12_2_1_security_gate.py`: 13 passed (unauthenticated 401 checks, session revocation, CSRF origin verification).

### 3.3 Test Namespace Isolation Tests
**Command:**
```bash
.venv/bin/pytest test_test_namespace_isolation.py -v
```
**Results:** **7 passed in 0.09s**
- Verifies default production mode routes to operational tabs.
- Verifies test namespace mode routes exclusively to `TEST_ONLY_*` tabs.
- Verifies `PermissionError` fail-closed when accessing operational tabs in test mode.
- Verifies `clear_test_tab_data` is blocked on operational tabs.

### 3.4 Deployment Safety Smoke Test
**Command:**
```bash
.venv/bin/python3 scripts/verify_deployment_safety.py
```
**Results:** **ALL 6 CHECKS PASSED**
- Check 1: Core module imports and dependency integrity.
- Check 2: Live Google Sheets CRM read-only state (RESEARCH_LOG 279 rows, QUARANTINE 39 rows, AC:BB empty).
- Check 3: Fail-closed deduplication guards and multi-signal disambiguation.
- Check 4: Writer safeguards & exact 28-column row width schema validation.
- Check 5: Concurrency architecture & distributed lease handling.
- Check 6: Cloud Run dispatcher default safety invariants.

---

## 4. Git Repository & Remote Status Audit

### 4.1 Local Commit Status
- **Current Branch:** `feature/live-discovery-pipeline-readiness`
- **Latest Local Commit on Branch (HEAD):** `097f77f` (`fix(deploy): resolve dynamic image digest and safeguard traffic promotion`)
- **Preceding Commit:** `da1bd5e` (`feat(sheets): implement isolated TEST_ONLY test namespace, proxy guard, and unit tests`)
- **Base Commit:** `ce0d84e` (`fix(discovery): repair provider resilience, budget fallbacks, and validation for live discovery`)
- **Committed Files in `097f77f`:**
  - `.github/workflows/05-deploy-cloud-run.yml`
  - `scripts/resolve_deployment_artifact.py`
  - `test_resolve_deployment_artifact.py`
  - `FINAL_PRODUCTION_READINESS_REPORT.md`

### 4.2 GitHub Remote Audit (`origin`)
- **Remote `origin/main` Commit:** `ae290aa` (`test: add automated Playwright post-promotion verification suite for production API and dashboard`)
- **Remote Branch Check (`git branch -r`):**
  - Branch `feature/live-discovery-pipeline-readiness` **DOES NOT EXIST on GitHub (`origin`)**.
  - Local commits `ce0d84e` and `da1bd5e` are **NOT PRESENT on GitHub**.
  - No code or workflow changes from this readiness cycle have been pushed or merged to remote.

---

## 5. Live Discovery & Qualification Evidence

**Source Data:** [data/final_live_discovery_validation.json](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/data/final_live_discovery_validation.json)  
**Target Market:** Restaurants in Manchester, United Kingdom  
**Discovery Provider:** Tavily Web Search (OSM Overpass was on cooldown following HTTP 502 capacity response)  
**Execution Mode:** `DRY_RUN=true`, `shadow_mode=True`, genuine live web discovery  

### Funnel Metrics
- **Raw Discovered Candidates:** 2
- **Country Verified (United Kingdom):** 2 (100%)
- **City Boundary Verified (Manchester):** 2 (100%)
- **Identified Candidates:**
  1. `another hand` — Mews Level, Great Northern building, Manchester M4 4EN
     - Extracted domain: `https://anotherhandmcr.com`
     - Qualification outcome: Classified as **`EXCLUDED`** (Reason: `WEBSITE_EXISTS`).
  2. `Louis Restaurants` — 3 Hardman Square, Manchester M3 3EB
     - Extracted domain: `https://louisrestaurants.com`
     - Qualification outcome: Classified as **`EXCLUDED`** (Reason: `WEBSITE_EXISTS`).
- **Disqualified / Excluded:** 2
- **Manual Review:** 0
- **Outreach-Ready Leads:** 0
- **Invented / Fabricated Data:** **0%** (ratings and review counts left unpopulated when not confirmed).
- **Integrity Assessment:** A sample yielding 0 outreach-ready leads is a **truthful and accurate qualification result**, correctly identifying that businesses with existing active websites are disqualified under campaign criteria.

---

## 6. Live API Service Telemetry & Operational State

- **Primary Service URL:** `https://dripp-leads-api-ukng7vge6q-uc.a.run.app`
- **Active Revision:** `dripp-leads-api-00020-c6x` (serving 100% traffic)
- **Health Probe (`/api/system/health`):**
  ```json
  {
    "status": "ok",
    "canonical_health_status": "OPERATIONAL",
    "service": "Dripp Media Lead Intelligence Suite",
    "timestamp": "2026-10-26T19:50:18.275024+00:00",
    "authenticated": false
  }
  ```
- **Unauthenticated Route Protection (`/api/leads`):** Returns **`HTTP 401 Unauthorized`**.
- **Dashboard Root Route (`/`):** Returns **`HTTP 200 OK`**.

---

## 7. Remaining Release Action and Expected Effect

All local fixes, unit test coverage, safety assertions, and documentation are complete.

### The Remaining Action
Because local commits `ce0d84e`, `da1bd5e`, and the workflow fix exist only on the local machine, the operator must complete the following manual release steps:

1. **Push Local Feature Branch and Open PR:**
   ```bash
   git push origin feature/live-discovery-pipeline-readiness
   ```
   Open and merge the pull request into `main` on GitHub.

2. **Trigger the Production Cloud Run Deployment Workflow:**
   Navigate to GitHub Actions $\rightarrow$ **05 - Production Cloud Run Deployment** ([.github/workflows/05-deploy-cloud-run.yml](file:///Users/metagurpreet/Desktop/Dripp%20International%20Leads/.github/workflows/05-deploy-cloud-run.yml)).
   - **Branch:** `main`
   - **Inputs:**
     - `confirm_deployment`: `CONFIRM_DEPLOY_TO_PRODUCTION`
     - `promote_traffic`: `false` *(Recommended default for initial release: deploys candidate at 0% traffic and updates discovery job)*
     - `promote_only_candidate`: *(leave blank)*

### Expected Effect
1. **Container Build & Push:** GitHub Actions builds a new container image tagged with the merge commit SHA and pushes it to Artifact Registry.
2. **Dynamic Digest Resolution:** `scripts/resolve_deployment_artifact.py` dynamically resolves the immutable sha256 digest directly from the build output (no hardcoded digests).
3. **Candidate Revision Deployment:** Cloud Run creates a new API candidate revision, pins it with tag `candidate` at **0% traffic**, and runs authenticated health and readiness inspections.
4. **Discovery Job Update:** The Cloud Run Job `dripp-discovery-job` configuration is updated to reference the new container image containing all resilience and timeout fixes. The job is **not executed**.
5. **Zero Disruption to Production Traffic:** Because `promote_traffic` defaults to `false`, the active revision (`dripp-leads-api-00020-c6x`) continues serving 100% of production traffic until the operator inspects the candidate and explicitly triggers traffic promotion.
6. **Commercial Locks Preserved:** `TRAVEL_MODE=ACTIVE`, `COMMERCIAL_ACTIONS_ENABLED=false`, and `AUTOMATED_EMAIL_ENABLED=false` remain permanently frozen.
