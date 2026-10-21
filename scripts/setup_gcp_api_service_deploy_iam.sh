#!/usr/bin/env bash
set -euo pipefail

# Additive one-time IAM setup for the existing Cloud Run API service.
# This grants the deployer permission to update this service and impersonate
# its runtime identity. It does not deploy the API or execute a Cloud Run Job.

PROJECT_ID="poised-eye-509816-a4"
REGION="us-central1"
API_SERVICE="dripp-leads-api"
EXPECTED_API_SA="dripp-leads-api@$PROJECT_ID.iam.gserviceaccount.com"
DEPLOYER_EMAIL="github-deployer@$PROJECT_ID.iam.gserviceaccount.com"
REPOSITORY_NAME="dripp-media"
EXPECTED_API_URL="https://dripp-leads-api-ukng7vge6q-uc.a.run.app"

gcloud config set project "$PROJECT_ID" --quiet
TMP_DIR="$(mktemp -d)"
chmod 700 "$TMP_DIR"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "=== Read-only preflight: API service ==="
gcloud run services describe "$API_SERVICE" \
  --region="$REGION" --project="$PROJECT_ID" --format=json > "$TMP_DIR/service.json"

python3 - "$TMP_DIR/service.json" "$EXPECTED_API_SA" "$EXPECTED_API_URL" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
expected_sa, expected_url = sys.argv[2:4]
spec = data.get("spec", {}).get("template", {}).get("spec", {})
containers = spec.get("containers", []) or []
actual_sa = spec.get("serviceAccountName") or ""
actual_url = data.get("status", {}).get("url") or ""
conditions = data.get("status", {}).get("conditions", []) or []
ready = next((c.get("status") for c in conditions if c.get("type") == "Ready"), None)
env = {e.get("name"): e for c in containers for e in (c.get("env") or [])}
def configured(e):
    return bool(e and ((isinstance(e.get("value"), str) and e["value"].strip())
                       or e.get("valueSource") or e.get("valueFrom")))
print("Service URL matches expected:", actual_url == expected_url)
print("Service Ready:", ready in (True, "True"))
print("Runtime service account matches expected:", actual_sa == expected_sa)
print("Operator key configured:", configured(env.get("OPERATOR_KEY")) or configured(env.get("OPERATOR_API_KEY")))
print("Google Sheets ID configured:", configured(env.get("GOOGLE_SHEET_ID")) or configured(env.get("GOOGLE_SHEET_KEY")))
if actual_url != expected_url or ready not in (True, "True") or actual_sa != expected_sa:
    raise SystemExit("Preflight failed: service identity, URL, or readiness mismatch.")
if not (configured(env.get("OPERATOR_KEY")) or configured(env.get("OPERATOR_API_KEY"))):
    raise SystemExit("Preflight failed: operator authentication is not configured.")
if not (configured(env.get("GOOGLE_SHEET_ID")) or configured(env.get("GOOGLE_SHEET_KEY"))):
    raise SystemExit("Preflight failed: Google Sheets CRM ID/key is not configured.")
PY

echo "=== Verifying deployer, runtime identity, and Artifact Registry ==="
gcloud iam service-accounts describe "$DEPLOYER_EMAIL" --project="$PROJECT_ID" >/dev/null
gcloud iam service-accounts describe "$EXPECTED_API_SA" --project="$PROJECT_ID" >/dev/null
gcloud artifacts repositories describe "$REPOSITORY_NAME" \
  --location="$REGION" --project="$PROJECT_ID" >/dev/null

echo "=== Granting least-privilege API deployment permissions ==="
gcloud run services add-iam-policy-binding "$API_SERVICE" \
  --region="$REGION" --project="$PROJECT_ID" \
  --member="serviceAccount:$DEPLOYER_EMAIL" \
  --role="roles/run.developer" --quiet

gcloud iam service-accounts add-iam-policy-binding "$EXPECTED_API_SA" \
  --project="$PROJECT_ID" \
  --member="serviceAccount:$DEPLOYER_EMAIL" \
  --role="roles/iam.serviceAccountUser" --quiet

echo "=== Verifying exact bindings ==="
gcloud run services get-iam-policy "$API_SERVICE" \
  --region="$REGION" --project="$PROJECT_ID" --format=json > "$TMP_DIR/service-iam.json"
gcloud iam service-accounts get-iam-policy "$EXPECTED_API_SA" \
  --project="$PROJECT_ID" --format=json > "$TMP_DIR/runtime-sa-iam.json"

python3 - "$TMP_DIR/service-iam.json" "$TMP_DIR/runtime-sa-iam.json" "$DEPLOYER_EMAIL" <<'PY'
import json, sys
service_policy = json.load(open(sys.argv[1], encoding="utf-8"))
sa_policy = json.load(open(sys.argv[2], encoding="utf-8"))
deployer = "serviceAccount:" + sys.argv[3]
def has(policy, role, member):
    return any(b.get("role") == role and member in b.get("members", [])
               for b in policy.get("bindings", []))
checks = [
    ("roles/run.developer on API service", has(service_policy, "roles/run.developer", deployer)),
    ("roles/iam.serviceAccountUser on API runtime identity", has(sa_policy, "roles/iam.serviceAccountUser", deployer)),
]
for label, ok in checks:
    print(label + ":", "PASS" if ok else "FAIL")
if not all(ok for _, ok in checks):
    raise SystemExit("IAM verification failed.")
PY

echo "Setup complete. No service deployment or Cloud Run Job execution was performed."
