"""Unit tests for fail-closed Cloud Run API service deployment verification."""
from scripts.verify_cloud_run_api_service import verify_service

EXPECTED_SA = "dripp-leads-api@poised-eye-509816-a4.iam.gserviceaccount.com"
EXPECTED_URL = "https://dripp-leads-api-ukng7vge6q-uc.a.run.app"
EXPECTED_IMAGE = "us-central1-docker.pkg.dev/poised-eye-509816-a4/dripp-media/dripp-discovery-job:testsha"


def service_fixture():
    return {
        "spec": {"template": {"spec": {
            "serviceAccountName": EXPECTED_SA,
            "containers": [{
                "image": EXPECTED_IMAGE,
                "command": ["uvicorn"],
                "args": ["server:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"],
                "env": [
                    {"name": "OPERATOR_KEY", "valueSource": {"secretKeyRef": {"secret": "operator-key", "version": "latest"}}},
                    {"name": "GOOGLE_SHEET_ID", "value": "test-sheet-id"},
                    {"name": "GCS_BACKUP_BUCKET", "value": "dripp-leads-backups"},
                    {"name": "TRAVEL_MODE", "value": "ACTIVE"},
                    {"name": "COMMERCIAL_ACTIONS_ENABLED", "value": "false"},
                    {"name": "AUTOMATED_EMAIL_ENABLED", "value": "false"},
                    {"name": "TECHNICAL_AUTOMATION_ENABLED", "value": "true"},
                ],
            }],
        }}},
        "status": {"url": EXPECTED_URL, "conditions": [{"type": "Ready", "status": "True"}]},
    }


def test_preflight_accepts_healthy_api_with_secret_reference():
    assert verify_service(service_fixture(), "preflight", EXPECTED_SA, EXPECTED_URL) == []


def test_preflight_blocks_missing_operator_key():
    data = service_fixture()
    data["spec"]["template"]["spec"]["containers"][0]["env"] = [
        e for e in data["spec"]["template"]["spec"]["containers"][0]["env"]
        if e["name"] != "OPERATOR_KEY"
    ]
    errors = verify_service(data, "preflight", EXPECTED_SA, EXPECTED_URL)
    assert any("Operator authentication" in error for error in errors)


def test_preflight_blocks_missing_durable_backup_bucket():
    data = service_fixture()
    data["spec"]["template"]["spec"]["containers"][0]["env"] = [
        e for e in data["spec"]["template"]["spec"]["containers"][0]["env"]
        if e["name"] != "GCS_BACKUP_BUCKET"
    ]
    errors = verify_service(data, "preflight", EXPECTED_SA, EXPECTED_URL)
    assert any("Durable backup bucket" in error for error in errors)


def test_deployed_mode_accepts_immutable_image_and_explicit_server_command():
    assert verify_service(service_fixture(), "deployed", EXPECTED_SA, EXPECTED_URL, EXPECTED_IMAGE) == []


def test_deployed_mode_rejects_wrong_image_and_enabled_email():
    data = service_fixture()
    container = data["spec"]["template"]["spec"]["containers"][0]
    container["image"] = "mutable:latest"
    for item in container["env"]:
        if item["name"] == "AUTOMATED_EMAIL_ENABLED":
            item["value"] = "true"
    errors = verify_service(data, "deployed", EXPECTED_SA, EXPECTED_URL, EXPECTED_IMAGE)
    assert any("immutable deployment image" in error for error in errors)
    assert any("AUTOMATED_EMAIL_ENABLED" in error for error in errors)
