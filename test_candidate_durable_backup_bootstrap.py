"""Unit tests for the pre-promotion durable backup bootstrap."""

from scripts.bootstrap_candidate_durable_backup import bootstrap_candidate_durable_backup


def good_responder(base_url, path, method="GET", payload=None, token=None, timeout=30):
    if path == "/api/operator/login":
        return 200, {"session_token": "dop-session-fixture"}, 0.1
    if path == "/api/system/backup":
        return 200, {
            "status": "ok",
            "backup_type": "DURABLE_GCS_VERIFIED",
            "backup_status": "PASS",
            "archive_uri": "gs://test-bucket/backups/backup.tar.gz",
            "manifest_uri": "gs://test-bucket/backups/backup.manifest.json",
            "archive_sha256": "a" * 64,
            "local_restore_status": "PASS",
            "remote_restore_status": "PASS",
            "isolated_restore_verified": True,
        }, 0.5
    raise AssertionError(f"Unexpected path: {path}")


def test_candidate_backup_bootstrap_passes_after_verified_local_and_remote_restore():
    report, errors = bootstrap_candidate_durable_backup(
        "https://candidate.example",
        "secret-fixture",
        "test-bucket",
        good_responder,
    )
    assert errors == []
    assert report["backup_status"] == "PASS"
    assert report["archive_uri_matches_bucket"] is True
    assert report["manifest_uri_matches_bucket"] is True
    assert report["archive_sha256_present"] is True


def test_candidate_backup_bootstrap_fails_closed_if_remote_restore_fails():
    def responder(base_url, path, method="GET", payload=None, token=None, timeout=30):
        code, body, elapsed = good_responder(base_url, path, method, payload, token, timeout)
        if path == "/api/system/backup":
            body = dict(body, remote_restore_status="FAIL", isolated_restore_verified=False)
        return code, body, elapsed

    report, errors = bootstrap_candidate_durable_backup(
        "https://candidate.example",
        "secret-fixture",
        "test-bucket",
        responder,
    )
    assert report["remote_restore_status"] == "FAIL"
    assert any("Remote GCS" in error for error in errors)


def test_candidate_backup_bootstrap_fails_if_bucket_uris_do_not_match():
    def responder(base_url, path, method="GET", payload=None, token=None, timeout=30):
        code, body, elapsed = good_responder(base_url, path, method, payload, token, timeout)
        if path == "/api/system/backup":
            body = dict(
                body,
                archive_uri="gs://wrong-bucket/backups/backup.tar.gz",
            )
        return code, body, elapsed

    _, errors = bootstrap_candidate_durable_backup(
        "https://candidate.example",
        "secret-fixture",
        "test-bucket",
        responder,
    )
    assert any("URIs do not match" in error for error in errors)


def test_candidate_backup_bootstrap_does_not_attempt_backup_without_login():
    def responder(base_url, path, method="GET", payload=None, token=None, timeout=30):
        if path == "/api/operator/login":
            return 401, {}, 0.1
        raise AssertionError("Backup must not run without successful operator authentication.")

    report, errors = bootstrap_candidate_durable_backup(
        "https://candidate.example",
        "secret-fixture",
        "test-bucket",
        responder,
    )
    assert report["login_http"] == 401
    assert any("login failed" in error for error in errors)


def test_candidate_backup_bootstrap_rejects_missing_bucket_before_network_calls():
    def responder(*args, **kwargs):
        raise AssertionError("No network requests should be made without a bucket.")

    _, errors = bootstrap_candidate_durable_backup(
        "https://candidate.example",
        "secret-fixture",
        "",
        responder,
    )
    assert any("GCS backup bucket is missing" in error for error in errors)
