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
# - Scopes Service Account User strictly to the job's runtime identity.
# - Restricts OIDC trust to repository 'Gurpreet931977/DrippMediaLeadsMachine' on 'main'.
# - Avoids Cloud Build and avoids long-lived service account JSON keys.
# ==============================================================================

PROJECT_ID="poised-eye-509816-a4"
REGION="us-central1"
JOB_NAME="dripp-discovery-job"
REPOSITORY_NAME="dripp-media"
GITHUB_REPO="Gurpreet931977/DrippMediaLeadsMachine"
POOL_NAME="github-actions-pool"
PROVIDER_NAME="github-actions-provider"
DEPLOYER_SA="github-deployer"
RUNTIME_SA="dripp-international-leads@${PROJECT_ID}.iam.gserviceaccount.com"

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
if ! gcloud iam workload-identity-pools providers describe "${PROVIDER_NAME}" \
    --workload-identity-pool="${POOL_NAME}" --location="global" >/dev/null 2>&1; then
  echo "      Creating Workload Identity Provider '${PROVIDER_NAME}'..."
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_NAME}" \
    --workload-identity-pool="${POOL_NAME}" \
    --location="global" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref,attribute.job_workflow_ref=assertion.job_workflow_ref" \
    --attribute-condition="assertion.repository == '${GITHUB_REPO}' && assertion.ref == 'refs/heads/main'"
else
  echo "      Workload Identity Provider '${PROVIDER_NAME}' already exists. Updating attribute mapping & condition..."
  gcloud iam workload-identity-pools providers update-oidc "${PROVIDER_NAME}" \
    --workload-identity-pool="${POOL_NAME}" \
    --location="global" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref,attribute.job_workflow_ref=assertion.job_workflow_ref" \
    --attribute-condition="assertion.repository == '${GITHUB_REPO}' && assertion.ref == 'refs/heads/main'"
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

# Bind Workload Identity User role to the GitHub repository on main branch
echo "      Binding Workload Identity User role to repository ${GITHUB_REPO}..."
gcloud iam service-accounts add-iam-policy-binding "${DEPLOYER_EMAIL}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository/${GITHUB_REPO}" \
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

# 3. Service Account User permission scoped strictly to the runtime service account
echo "      - Granting roles/iam.serviceAccountUser on runtime service account..."
if gcloud iam service-accounts describe "${RUNTIME_SA}" >/dev/null 2>&1; then
  gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
    --member="serviceAccount:${DEPLOYER_EMAIL}" \
    --role="roles/iam.serviceAccountUser" \
    --quiet
fi

# Also grant on default Compute Engine SA if job runs as default compute
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
if gcloud iam service-accounts describe "${COMPUTE_SA}" >/dev/null 2>&1; then
  gcloud iam service-accounts add-iam-policy-binding "${COMPUTE_SA}" \
    --member="serviceAccount:${DEPLOYER_EMAIL}" \
    --role="roles/iam.serviceAccountUser" \
    --quiet
fi

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
