"""Unit tests for fail-closed Cloud Run API service deployment verification."""
from scripts.verify_cloud_run_api_service import verify_service
from scripts.wait_for_cloud_run_api_candidate import (
    candidate_status,
    primary_traffic_matches,
    wait_for_candidate,
)

EXPECTED_SA = "dripp-leads-api@poised-eye-509816-a4.iam.gserviceaccount.com"
EXPECTED_URL = "https://dripp-leads-api-ukng7vge6q-uc.a.run.app"
EXPECTED_IMAGE = "us-central1-docker.pkg.dev/poised-eye-509816-a4/dripp-media/dripp-discovery-job:testsha"
EXPECTED_REVISION = "dripp-leads-api-00013-pkt"


def service_fixture():
    return {
        "spec": {"template": {"spec": {
            "serviceAccountName": EXPECTED_SA,
            "containers": [{
                "image": EXPECTED_IMAGE,
                "resources": {"limits": {"memory": "1Gi"}},
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
        "status": {
            "url": EXPECTED_URL,
            "conditions": [{"type": "Ready", "status": "True"}],
            "latestReadyRevisionName": EXPECTED_REVISION,
            "traffic": [{"revisionName": EXPECTED_REVISION, "percent": 100}],
        },
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


def test_deployed_mode_rejects_insufficient_memory():
    data = service_fixture()
    data["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = "512Mi"
    errors = verify_service(data, "deployed", EXPECTED_SA, EXPECTED_URL, EXPECTED_IMAGE)
    assert any("memory limit must be 1Gi" in error for error in errors)


def test_routed_mode_accepts_expected_revision_at_100_percent():
    assert verify_service(
        service_fixture(), "routed", EXPECTED_SA, EXPECTED_URL,
        EXPECTED_IMAGE, EXPECTED_REVISION
    ) == []


def test_routed_mode_rejects_traffic_pinned_to_an_older_revision():
    data = service_fixture()
    data["status"]["traffic"] = [{"revisionName": "dripp-leads-api-00009-6nf", "percent": 100}]
    errors = verify_service(
        data, "routed", EXPECTED_SA, EXPECTED_URL,
        EXPECTED_IMAGE, EXPECTED_REVISION
    )
    assert any("100% primary traffic" in error for error in errors)



def test_candidate_mode_accepts_candidate_ready_without_moving_primary_traffic():
    data = service_fixture()
    data["status"]["traffic"] = [{"revisionName": "dripp-leads-api-00009-6nf", "percent": 100}]
    assert verify_service(
        data, "candidate", EXPECTED_SA, EXPECTED_URL,
        EXPECTED_IMAGE, EXPECTED_REVISION, "dripp-leads-api-00009-6nf"
    ) == []


def test_candidate_mode_rejects_primary_traffic_moving_before_readiness_passes():
    data = service_fixture()
    errors = verify_service(
        data, "candidate", EXPECTED_SA, EXPECTED_URL,
        EXPECTED_IMAGE, EXPECTED_REVISION, "dripp-leads-api-00009-6nf"
    )
    assert any("previous revision before promotion" in error for error in errors)



def test_candidate_status_accepts_ready_v1_service_and_expected_image():
    data = {
        "spec": {"template": {"spec": {"containers": [{"image": EXPECTED_IMAGE}]}}},
        "status": {
            "latestCreatedRevisionName": EXPECTED_REVISION,
            "latestReadyRevisionName": EXPECTED_REVISION,
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    state = candidate_status(data, EXPECTED_IMAGE)
    assert state["accepted"] is True
    assert state["created_revision"] == EXPECTED_REVISION
    assert state["ready_revision"] == EXPECTED_REVISION


def test_candidate_status_rejects_revision_that_is_not_ready_yet():
    data = {
        "spec": {"template": {"spec": {"containers": [{"image": EXPECTED_IMAGE}]}}},
        "status": {
            "latestCreatedRevisionName": "dripp-leads-api-00014-abc",
            "latestReadyRevisionName": EXPECTED_REVISION,
            "conditions": [{"type": "Ready", "status": "False"}],
        },
    }
    state = candidate_status(data, EXPECTED_IMAGE)
    assert state["ready"] is False
    assert state["accepted"] is False


def test_candidate_status_supports_cloud_run_v2_resource_shape():
    data = {
        "template": {"containers": [{"image": EXPECTED_IMAGE}]},
        "status": {
            "latestCreatedRevision": (
                "projects/poised-eye-509816-a4/locations/us-central1/"
                "services/dripp-leads-api/revisions/" + EXPECTED_REVISION
            ),
            "latestReadyRevision": (
                "projects/poised-eye-509816-a4/locations/us-central1/"
                "services/dripp-leads-api/revisions/" + EXPECTED_REVISION
            ),
        },
    }
    state = candidate_status(data, EXPECTED_IMAGE)
    assert state["accepted"] is True
    assert state["created_revision"] == EXPECTED_REVISION


def test_candidate_status_rejects_immutable_image_mismatch():
    data = {
        "spec": {"template": {"spec": {"containers": [{"image": "mutable:latest"}]}}},
        "status": {
            "latestCreatedRevisionName": EXPECTED_REVISION,
            "latestReadyRevisionName": EXPECTED_REVISION,
        },
    }
    state = candidate_status(data, EXPECTED_IMAGE)
    assert state["ready"] is True
    assert state["image_matches"] is False
    assert state["accepted"] is False



def test_candidate_can_be_identified_before_latest_ready_moves():
    data = {
        "spec": {"template": {"spec": {"containers": [{"image": EXPECTED_IMAGE}]}}},
        "status": {
            "latestCreatedRevisionName": EXPECTED_REVISION,
            "latestReadyRevisionName": "dripp-leads-api-00012-xyz",
            "conditions": [{"type": "Ready", "status": "True"}],
            "traffic": [{"revisionName": "dripp-leads-api-00012-xyz", "percent": 100}],
        },
    }
    state = candidate_status(data, EXPECTED_IMAGE)
    assert state["candidate_identified"] is True
    assert state["ready"] is False
    assert state["accepted"] is False


def test_primary_traffic_check_allows_zero_weight_tagged_candidate():
    data = {
        "status": {
            "traffic": [
                {"revisionName": "dripp-leads-api-00012-xyz", "percent": 100},
                {"revisionName": EXPECTED_REVISION, "tag": "release-candidate"},
            ]
        }
    }
    assert primary_traffic_matches(data, "dripp-leads-api-00012-xyz") is True


def test_primary_traffic_check_rejects_candidate_primary_promotion():
    data = {
        "status": {
            "traffic": [{"revisionName": EXPECTED_REVISION, "percent": 100}]
        }
    }
    assert primary_traffic_matches(data, "dripp-leads-api-00012-xyz") is False


def test_wait_for_candidate_tags_then_health_checks_before_accepting(monkeypatch, capsys):
    old_primary = "dripp-leads-api-00012-xyz"
    before_ready = {
        "spec": {"template": {"spec": {"containers": [{"image": EXPECTED_IMAGE}]}}},
        "status": {
            "latestCreatedRevisionName": EXPECTED_REVISION,
            "latestReadyRevisionName": old_primary,
            "conditions": [{"type": "Ready", "status": "True"}],
            "traffic": [{"revisionName": old_primary, "percent": 100}],
        },
    }
    after_health = {
        "spec": {"template": {"spec": {"containers": [{"image": EXPECTED_IMAGE}]}}},
        "status": {
            "latestCreatedRevisionName": EXPECTED_REVISION,
            "latestReadyRevisionName": EXPECTED_REVISION,
            "conditions": [{"type": "Ready", "status": "True"}],
            "traffic": [
                {"revisionName": old_primary, "percent": 100},
                {"revisionName": EXPECTED_REVISION, "tag": "release-candidate"},
            ],
        },
    }
    describe_results = iter([before_ready, after_health])
    calls = []
    monkeypatch.setattr(
        "scripts.wait_for_cloud_run_api_candidate._describe_service",
        lambda service, region, project: next(describe_results),
    )
    monkeypatch.setattr(
        "scripts.wait_for_cloud_run_api_candidate._tag_candidate",
        lambda service, region, project, revision: calls.append(("tag", revision)),
    )
    monkeypatch.setattr(
        "scripts.wait_for_cloud_run_api_candidate._probe_candidate",
        lambda url: (calls.append(("probe", url)) is None, "HTTP 200"),
    )

    result = wait_for_candidate(
        "dripp-leads-api", "us-central1", "test-project", EXPECTED_IMAGE,
        "https://candidate.example", old_primary, timeout_seconds=5,
        poll_interval_seconds=1,
    )
    assert result == 0
    assert calls[0] == ("tag", EXPECTED_REVISION)
    assert calls[1] == ("probe", "https://candidate.example")
    assert EXPECTED_REVISION in capsys.readouterr().out


def test_wait_for_candidate_refuses_to_tag_if_primary_traffic_changed(monkeypatch):
    changed_traffic = {
        "spec": {"template": {"spec": {"containers": [{"image": EXPECTED_IMAGE}]}}},
        "status": {
            "latestCreatedRevisionName": EXPECTED_REVISION,
            "latestReadyRevisionName": EXPECTED_REVISION,
            "traffic": [{"revisionName": EXPECTED_REVISION, "percent": 100}],
        },
    }
    tags = []
    monkeypatch.setattr(
        "scripts.wait_for_cloud_run_api_candidate._describe_service",
        lambda service, region, project: changed_traffic,
    )
    monkeypatch.setattr(
        "scripts.wait_for_cloud_run_api_candidate._tag_candidate",
        lambda *args: tags.append(args),
    )
    result = wait_for_candidate(
        "dripp-leads-api", "us-central1", "test-project", EXPECTED_IMAGE,
        "https://candidate.example", "dripp-leads-api-00012-xyz",
        timeout_seconds=1, poll_interval_seconds=1,
    )
    assert result == 1
    assert tags == []
