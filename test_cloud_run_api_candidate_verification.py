"""Unit tests for authenticated Cloud Run API candidate verification."""
from scripts.verify_cloud_run_api_candidate import verify_candidate


def good_responder(base_url, path, method="GET", payload=None, token=None, timeout=30):
    if path == "/api/system/health":
        return 200, {"canonical_health_status": "OPERATIONAL"}, 0.1
    if path == "/api/operator/login":
        return 200, {"session_token": "dop-test-token-that-must-not-be-printed"}, 0.1
    if path == "/api/crm/status":
        return 200, {
            "crm_connected": True,
            "source": "GOOGLE_SHEETS",
            "tabs": {
                "LEADS": {"status": "CONNECTED", "count": 7},
                "REVIEW_QUEUE": {"status": "CONNECTED", "count": 64},
            },
        }, 0.2
    if path == "/api/system/readiness":
        return 200, {
            "overall_status": "PASS",
            "readiness_state": "STAGING_READY",
            "passed_gates_count": 20,
            "warning_gates_count": 0,
            "blocking_gates_count": 0,
            "blocking_gates": [],
        }, 0.3
    raise AssertionError(f"Unexpected path: {path}")


def test_candidate_passes_health_auth_crm_and_readiness():
    report, errors = verify_candidate(
        "https://candidate.example", "operator-key-fixture", good_responder
    )
    assert errors == []
    assert report["login_ok"] is True
    assert report["crm_connected"] is True
    assert report["readiness_state"] == "STAGING_READY"
    assert report["passed_gates_count"] == 20


def test_candidate_fails_closed_when_readiness_has_blockers():
    def responder(base_url, path, method="GET", payload=None, token=None, timeout=30):
        code, body, elapsed = good_responder(base_url, path, method, payload, token, timeout)
        if path == "/api/system/readiness":
            body = dict(body, blocking_gates_count=1, blocking_gates=["BACKUP_HEALTH"])
        return code, body, elapsed

    report, errors = verify_candidate(
        "https://candidate.example", "operator-key-fixture", responder
    )
    assert report["blocking_gates_count"] == 1
    assert any("zero blocking gates" in error for error in errors)


def test_candidate_fails_closed_when_readiness_returns_503():
    def responder(base_url, path, method="GET", payload=None, token=None, timeout=30):
        code, body, elapsed = good_responder(base_url, path, method, payload, token, timeout)
        if path == "/api/system/readiness":
            return 503, {}, elapsed
        return code, body, elapsed

    report, errors = verify_candidate(
        "https://candidate.example", "operator-key-fixture", responder
    )
    assert report["readiness_http"] == 503
    assert any("did not return HTTP 200" in error for error in errors)


def test_candidate_fails_closed_when_login_does_not_issue_session():
    def responder(base_url, path, method="GET", payload=None, token=None, timeout=30):
        if path == "/api/operator/login":
            return 401, {}, 0.1
        return good_responder(base_url, path, method, payload, token, timeout)

    report, errors = verify_candidate(
        "https://candidate.example", "operator-key-fixture", responder
    )
    assert report["login_ok"] is False
    assert any("operator login failed" in error for error in errors)
    assert "crm_http" not in report
