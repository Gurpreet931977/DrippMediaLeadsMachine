#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# scripts/setup_gcp_deployment_iam.sh
# ==============================================================================
# One-Time Least-Privilege Workload Identity Federation & Deployment Setup
# Run in Google Cloud Shell as GCP Project Owner / IAM Administrator.
#
# Idempotent & Non-Destructive:
# - Reuses existing resources where present.
# - Scopes Cloud Run permissions strictly to job 'dripp-discovery-job'.
# - Scopes Artifact Registry permissions strictly to repository 'dripp-media'.
# - Scopes Service Account User strictly to the job's actual runtime identity.
# - Restricts OIDC trust to repository 'Gurpreet931977/DrippMediaLeadsMachine' (id: 1405742084)
#   on 'main' running '.github/workflows/05-deploy-cloud-run.yml'.
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
RUNTIME_SA_DEFAULT="dripp-international-leads@${PROJECT_ID}.iam.gserviceaccount.com"

echo "=============================================================================="
echo "  DRIPP MEDIA — GCP DEPLOYMENT INFRASTRUCTURE & IAM PROVISIONING"
echo "=============================================================================="

echo "[1/7] Configuring active GCP project (${PROJECT_ID})..."
gcloud config set project "${PROJECT_ID}" --quiet

PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
echo "      Project Number: ${PROJECT_NUMBER}"

echo "[2/7] Enabling minimal required GCP Service APIs..."
gcloud services enable \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com

echo "[3/7] Ensuring Artifact Registry repository '${REPOSITORY_NAME}' exists..."
if ! gcloud artifacts repositories describe "${REPOSITORY_NAME}" --location="${REGION}" >/dev/null 2>&1; then
  echo "      Creating Artifact Registry repository '${REPOSITORY_NAME}' in ${REGION}..."
  gcloud artifacts repositories create "${REPOSITORY_NAME}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Docker repository for Dripp Media backend images"
else
  echo "      Artifact Registry repository '${REPOSITORY_NAME}' already exists."
fi

echo "[4/7] Ensuring Workload Identity Pool '${POOL_NAME}' exists..."
if ! gcloud iam workload-identity-pools describe "${POOL_NAME}" --location="global" >/dev/null 2>&1; then
  echo "      Creating Workload Identity Pool '${POOL_NAME}'..."
  gcloud iam workload-identity-pools create "${POOL_NAME}" \
    --location="global" \
    --display-name="GitHub Actions Pool" \
    --description="Workload Identity Pool for GitHub Actions CI/CD"
else
  echo "      Workload Identity Pool '${POOL_NAME}' already exists."
fi

echo "[5/7] Ensuring Workload Identity Provider '${PROVIDER_NAME}' exists..."
MAPPING="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref,attribute.job_workflow_ref=assertion.job_workflow_ref"
CONDITION="assertion.repository_id == '${REPOSITORY_ID}' && assertion.repository == '${GITHUB_REPO}' && assertion.ref == 'refs/heads/main' && assertion.job_workflow_ref.startsWith('${GITHUB_REPO}/${WORKFLOW_PATH}@')"

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

echo "[6/7] Ensuring Deployer Service Account exists..."
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

echo "[7/7] Granting scoped, least-privilege deployment permissions to ${DEPLOYER_EMAIL}..."

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

# 3. Dynamic inspection of Cloud Run Job to identify actual runtime service account
echo "      - Inspecting Cloud Run job '${JOB_NAME}' to identify actual runtime service account..."
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
DISCOVERY_SA="${RUNTIME_SA_DEFAULT}"

JOB_JSON=$(gcloud run jobs describe "${JOB_NAME}" --region="${REGION}" --format=json 2>/dev/null || true)
ACTUAL_RUNTIME_SA=""
if [ -n "${JOB_JSON}" ]; then
  ACTUAL_RUNTIME_SA=$(echo "${JOB_JSON}" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    sa = data.get('spec', {}).get('template', {}).get('spec', {}).get('template', {}).get('spec', {}).get('serviceAccountName')
    if not sa:
        sa = data.get('spec', {}).get('template', {}).get('spec', {}).get('serviceAccountName')
    if not sa:
        sa = data.get('spec', {}).get('template', {}).get('serviceAccount')
    print((sa or '').strip())
except Exception:
    print('')
")
fi

if [ -z "${ACTUAL_RUNTIME_SA}" ]; then
  # If job doesn't specify custom SA, check if DISCOVERY_SA exists in project, else fallback to Compute SA
  if gcloud iam service-accounts describe "${DISCOVERY_SA}" >/dev/null 2>&1; then
    ACTUAL_RUNTIME_SA="${DISCOVERY_SA}"
  else
    ACTUAL_RUNTIME_SA="${COMPUTE_SA}"
  fi
  echo "        Runtime SA not explicitly set in job spec; resolved to: ${ACTUAL_RUNTIME_SA}"
else
  echo "        Discovered runtime SA from existing Cloud Run job: ${ACTUAL_RUNTIME_SA}"
fi

# Ensure runtime SA exists before binding
if gcloud iam service-accounts describe "${ACTUAL_RUNTIME_SA}" >/dev/null 2>&1; then
  echo "        Granting roles/iam.serviceAccountUser strictly on ${ACTUAL_RUNTIME_SA}..."
  gcloud iam service-accounts add-iam-policy-binding "${ACTUAL_RUNTIME_SA}" \
    --member="serviceAccount:${DEPLOYER_EMAIL}" \
    --role="roles/iam.serviceAccountUser" \
    --quiet
else
  echo "        WARNING: Runtime service account ${ACTUAL_RUNTIME_SA} not found in IAM."
fi

# Remove unnecessary bindings on alternative identities
CANDIDATE_ALTERNATIVES=(
  "${DISCOVERY_SA}"
  "${COMPUTE_SA}"
)
for ALT_SA in "${CANDIDATE_ALTERNATIVES[@]}"; do
  if [ "${ALT_SA}" != "${ACTUAL_RUNTIME_SA}" ]; then
    echo "        Removing unnecessary serviceAccountUser binding from alternative identity ${ALT_SA} if present..."
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
