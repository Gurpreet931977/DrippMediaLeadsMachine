#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# scripts/setup_gcp_deployment_iam.sh
# ==============================================================================
# One-Time Least-Privilege Workload Identity Federation & Deployment Setup
# Run in Google Cloud Shell as GCP Project Owner / IAM Administrator.
#
# Idempotent & Non-Destructive:
# - Read-Only Preflight: Validates job existence and resolves runtime identity
#   BEFORE any resource creation, API enablement, or IAM mutations.
# - Reuses existing resources where present.
# - Scopes Cloud Run permissions strictly to job 'dripp-discovery-job'.
# - Scopes Artifact Registry permissions strictly to repository 'dripp-media'.
# - Resolves runtime Service Account User strictly from Cloud Run Job definition
#   and fails closed if unresolved (no guessing).
# - Restricts OIDC trust to repository 'Gurpreet931977/DrippMediaLeadsMachine' (id: 1405742084)
#   on 'main' running exact workflow '.github/workflows/05-deploy-cloud-run.yml'.
# - Cleans up only the deployer service account's unnecessary bindings on alternatives.
# - Avoids Cloud Build and avoids long-lived service account JSON keys.
# ==============================================================================

PROJECT_ID="poised-eye-509816-a4"
REGION="us-central1"
JOB_NAME="dripp-discovery-job"
REPOSITORY_NAME="dripp-media"
GITHUB_REPO="Gurpreet931977/DrippMediaLeadsMachine"
REPOSITORY_ID="1405742084"
WORKFLOW_PATH=".github/workflows/05-deploy-cloud-run.yml"
POOL_NAME="github-actions-pool"
PROVIDER_NAME="github-actions-provider"
DEPLOYER_SA="github-deployer"

echo "=============================================================================="
echo "  DRIPP MEDIA — GCP DEPLOYMENT INFRASTRUCTURE & IAM PROVISIONING"
echo "=============================================================================="

# ------------------------------------------------------------------------------
# STEP 1: Establish Project Context
# ------------------------------------------------------------------------------
echo "[1/8] Configuring active GCP project (${PROJECT_ID})..."
gcloud config set project "${PROJECT_ID}" --quiet

PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
echo "      Project Number: ${PROJECT_NUMBER}"

# ------------------------------------------------------------------------------
# STEP 2: Read-Only Preflight — Validate Target Job & Resolve Runtime Identity
# (Must run BEFORE API enablement, resource creation, or IAM policy mutations)
# ------------------------------------------------------------------------------
echo "[2/8] Running read-only preflight: Validating Cloud Run job '${JOB_NAME}' in ${REGION}..."

JOB_JSON=$(gcloud run jobs describe "${JOB_NAME}" --region="${REGION}" --format=json 2>/dev/null || true)
if [ -z "${JOB_JSON}" ]; then
  echo "::error::Preflight check failed: Cloud Run job '${JOB_NAME}' was not found in project '${PROJECT_ID}' (region '${REGION}')." >&2
  echo "         Cannot proceed with deployment IAM provisioning without an existing Cloud Run job." >&2
  echo "         Failing closed before enabling APIs, creating resources, or modifying IAM policies." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARSER_HELPER="${SCRIPT_DIR}/parse_cloud_run_sa.py"

if [ -f "${PARSER_HELPER}" ]; then
  ACTUAL_RUNTIME_SA=$(echo "${JOB_JSON}" | python3 "${PARSER_HELPER}" "${PROJECT_ID}")
else
  ACTUAL_RUNTIME_SA=$(echo "${JOB_JSON}" | python3 -c "
import sys, json
project_id = sys.argv[1] if len(sys.argv) > 1 else ''
try:
    data = json.load(sys.stdin)
    sa = ''
    if isinstance(data, dict):
        # 1. Cloud Run Jobs v2 API standard: template.template.serviceAccount
        sa = data.get('template', {}).get('template', {}).get('serviceAccount')
        # 2. Cloud Run Jobs v1 API standard: spec.template.spec.template.spec.serviceAccountName
        if not sa:
            sa = (
                data.get('spec', {})
                .get('template', {})
                .get('spec', {})
                .get('template', {})
                .get('spec', {})
                .get('serviceAccountName')
            )
    sa = (sa or '').strip()
    if sa and '@' not in sa and project_id:
        sa = f'{sa}@{project_id}.iam.gserviceaccount.com'
    print(sa)
except Exception:
    print('')
" "${PROJECT_ID}")
fi

if [ -z "${ACTUAL_RUNTIME_SA}" ]; then
  echo "::error::Preflight check failed: Failed to resolve actual runtime service account from Cloud Run job '${JOB_NAME}'." >&2
  echo "         The job definition in project '${PROJECT_ID}' region '${REGION}' did not yield a verifiable service account." >&2
  echo "         Failing closed before enabling APIs, creating resources, or modifying IAM policies." >&2
  echo "         Ensure job '${JOB_NAME}' exists and has an explicit runtime serviceAccount configured." >&2
  exit 1
fi

# Ensure runtime SA exists in IAM before proceeding
if ! gcloud iam service-accounts describe "${ACTUAL_RUNTIME_SA}" >/dev/null 2>&1; then
  echo "::error::Preflight check failed: Resolved runtime service account '${ACTUAL_RUNTIME_SA}' does not exist in IAM for project '${PROJECT_ID}'." >&2
  echo "         Failing closed before provisioning resources or modifying permissions." >&2
  exit 1
fi

echo "      ✓ Preflight verified: Job '${JOB_NAME}' exists and uses verified runtime identity: ${ACTUAL_RUNTIME_SA}"

# ------------------------------------------------------------------------------
# STEP 3: Enable Minimal Required Service APIs
# ------------------------------------------------------------------------------
echo "[3/8] Enabling minimal required GCP Service APIs..."
gcloud services enable \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com

# ------------------------------------------------------------------------------
# STEP 4: Ensure Artifact Registry Repository Exists
# ------------------------------------------------------------------------------
echo "[4/8] Ensuring Artifact Registry repository '${REPOSITORY_NAME}' exists..."
if ! gcloud artifacts repositories describe "${REPOSITORY_NAME}" --location="${REGION}" >/dev/null 2>&1; then
  echo "      Creating Artifact Registry repository '${REPOSITORY_NAME}' in ${REGION}..."
  gcloud artifacts repositories create "${REPOSITORY_NAME}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Docker repository for Dripp Media backend images"
else
  echo "      Artifact Registry repository '${REPOSITORY_NAME}' already exists."
fi

# ------------------------------------------------------------------------------
# STEP 5: Ensure Workload Identity Pool Exists
# ------------------------------------------------------------------------------
echo "[5/8] Ensuring Workload Identity Pool '${POOL_NAME}' exists..."
if ! gcloud iam workload-identity-pools describe "${POOL_NAME}" --location="global" >/dev/null 2>&1; then
  echo "      Creating Workload Identity Pool '${POOL_NAME}'..."
  gcloud iam workload-identity-pools create "${POOL_NAME}" \
    --location="global" \
    --display-name="GitHub Actions Pool" \
    --description="Workload Identity Pool for GitHub Actions CI/CD"
else
  echo "      Workload Identity Pool '${POOL_NAME}' already exists."
fi

# ------------------------------------------------------------------------------
# STEP 6: Ensure Workload Identity Provider Exists (workflow_ref Exact Match)
# ------------------------------------------------------------------------------
echo "[6/8] Ensuring Workload Identity Provider '${PROVIDER_NAME}' exists..."
MAPPING="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref,attribute.workflow_ref=assertion.workflow_ref"
CONDITION="assertion.repository_id == '${REPOSITORY_ID}' && assertion.repository == '${GITHUB_REPO}' && assertion.ref == 'refs/heads/main' && assertion.workflow_ref == '${GITHUB_REPO}/${WORKFLOW_PATH}@refs/heads/main'"

if ! gcloud iam workload-identity-pools providers describe "${PROVIDER_NAME}" \
    --workload-identity-pool="${POOL_NAME}" --location="global" >/dev/null 2>&1; then
  echo "      Creating Workload Identity Provider '${PROVIDER_NAME}'..."
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_NAME}" \
    --workload-identity-pool="${POOL_NAME}" \
    --location="global" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="${MAPPING}" \
    --attribute-condition="${CONDITION}"
else
  echo "      Workload Identity Provider '${PROVIDER_NAME}' already exists. Updating attribute mapping & condition..."
  gcloud iam workload-identity-pools providers update-oidc "${PROVIDER_NAME}" \
    --workload-identity-pool="${POOL_NAME}" \
    --location="global" \
    --attribute-mapping="${MAPPING}" \
    --attribute-condition="${CONDITION}"
fi

# ------------------------------------------------------------------------------
# STEP 7: Ensure Deployer Service Account Exists & Bind Immutable Repo ID
# ------------------------------------------------------------------------------
echo "[7/8] Ensuring Deployer Service Account exists..."
DEPLOYER_EMAIL="${DEPLOYER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "${DEPLOYER_EMAIL}" >/dev/null 2>&1; then
  echo "      Creating service account '${DEPLOYER_SA}'..."
  gcloud iam service-accounts create "${DEPLOYER_SA}" \
    --display-name="GitHub Actions Cloud Run Deployer" \
    --description="Least-privilege service account for GitHub Actions deployment"
else
  echo "      Service account '${DEPLOYER_EMAIL}' already exists."
fi

# Clean up legacy mutable repository name binding if present
echo "      Cleaning up legacy mutable repository binding if present..."
gcloud iam service-accounts remove-iam-policy-binding "${DEPLOYER_EMAIL}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository/${GITHUB_REPO}" \
  --quiet 2>/dev/null || true

# Bind Workload Identity User role strictly to immutable repository_id (1405742084)
echo "      Binding Workload Identity User role to immutable repository_id ${REPOSITORY_ID}..."
gcloud iam service-accounts add-iam-policy-binding "${DEPLOYER_EMAIL}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository_id/${REPOSITORY_ID}" \
  --quiet

# ------------------------------------------------------------------------------
# STEP 8: Grant Scoped, Least-Privilege Permissions to Deployer
# ------------------------------------------------------------------------------
echo "[8/8] Granting scoped, least-privilege deployment permissions to ${DEPLOYER_EMAIL}..."

# 1. Cloud Run Developer permission scoped strictly to the target job
echo "      - Granting roles/run.developer on job '${JOB_NAME}'..."
gcloud run jobs add-iam-policy-binding "${JOB_NAME}" \
  --region="${REGION}" \
  --member="serviceAccount:${DEPLOYER_EMAIL}" \
  --role="roles/run.developer" \
  --quiet

# 2. Artifact Registry Writer permission scoped strictly to repository 'dripp-media'
echo "      - Granting roles/artifactregistry.writer on repository '${REPOSITORY_NAME}'..."
gcloud artifacts repositories add-iam-policy-binding "${REPOSITORY_NAME}" \
  --location="${REGION}" \
  --member="serviceAccount:${DEPLOYER_EMAIL}" \
  --role="roles/artifactregistry.writer" \
  --quiet

# 3. Service Account User permission scoped strictly to the preflight-verified runtime identity
echo "      - Granting roles/iam.serviceAccountUser strictly on runtime identity ${ACTUAL_RUNTIME_SA}..."
gcloud iam service-accounts add-iam-policy-binding "${ACTUAL_RUNTIME_SA}" \
  --member="serviceAccount:${DEPLOYER_EMAIL}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet

# 4. Safely clean up only the deployer service account's unnecessary bindings on alternative candidate identities
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
CANDIDATE_ALTERNATIVES=(
  "dripp-international-leads@${PROJECT_ID}.iam.gserviceaccount.com"
  "${COMPUTE_SA}"
)
for ALT_SA in "${CANDIDATE_ALTERNATIVES[@]}"; do
  if [ "${ALT_SA}" != "${ACTUAL_RUNTIME_SA}" ]; then
    echo "        Removing unnecessary serviceAccountUser binding for ${DEPLOYER_EMAIL} from alternative identity ${ALT_SA} if present..."
    gcloud iam service-accounts remove-iam-policy-binding "${ALT_SA}" \
      --member="serviceAccount:${DEPLOYER_EMAIL}" \
      --role="roles/iam.serviceAccountUser" \
      --quiet 2>/dev/null || true
  fi
done

WIF_PROVIDER_ID="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/providers/${PROVIDER_NAME}"

echo ""
echo "=============================================================================="
echo "  ✓ SETUP COMPLETED SUCCESSFULLY"
echo "=============================================================================="
echo "Configure the following two Repository Variables in GitHub Actions:"
echo "GitHub Repo -> Settings -> Secrets and variables -> Actions -> Variables:"
echo ""
echo "1. Variable Name:  GCP_WORKLOAD_IDENTITY_PROVIDER"
echo "   Variable Value: ${WIF_PROVIDER_ID}"
echo ""
echo "2. Variable Name:  GCP_DEPLOYER_SERVICE_ACCOUNT"
echo "   Variable Value: ${DEPLOYER_EMAIL}"
echo "=============================================================================="
