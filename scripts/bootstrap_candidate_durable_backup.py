#!/usr/bin/env python3
"""Create and verify a durable backup on an isolated Cloud Run candidate.

The production deployment workflow is manually authorized. This helper runs
before traffic promotion so readiness can require a real verified GCS backup,
even when this candidate is the first version with the durable backup endpoint.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

RequestFn = Callable[..., tuple[int, dict[str, Any], float]]


def request_json(
    base_url: str,
    path: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any], float]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base_url.rstrip("/") + path, data=body, headers=headers, method=method
    )
    import time
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            parsed = json.loads(raw) if raw else {}
            return response.status, parsed if isinstance(parsed, dict) else {}, time.monotonic() - started
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {}
        return exc.code, parsed if isinstance(parsed, dict) else {}, time.monotonic() - started
    except Exception:
        # Do not echo request objects, URLs, or headers (which may contain credentials).
        return 0, {"request_error": True}, time.monotonic() - started


def bootstrap_candidate_durable_backup(
    base_url: str,
    operator_key: str,
    bucket_name: str,
    request_fn: RequestFn = request_json,
) -> tuple[dict[str, Any], list[str]]:
    report: dict[str, Any] = {}
    errors: list[str] = []

    clean_url = (base_url or "").strip().rstrip("/")
    clean_bucket = (bucket_name or "").strip()
    if not clean_url:
        return report, ["Candidate API URL is missing."]
    if not operator_key:
        return report, ["Operator key is missing."]
    if not clean_bucket:
        return report, ["GCS backup bucket is missing."]

    login_code, login, login_seconds = request_fn(
        clean_url,
        "/api/operator/login",
        method="POST",
        payload={"operator_key": operator_key},
        timeout=30,
    )
    token = login.get("session_token") if isinstance(login, dict) else None
    report["login_http"] = login_code
    report["login_seconds"] = round(login_seconds, 1)
    if login_code != 200 or not isinstance(token, str) or not token:
        errors.append("Candidate operator login failed; durable backup was not attempted.")
        return report, errors

    backup_code, backup, backup_seconds = request_fn(
        clean_url,
        "/api/system/backup",
        method="POST",
        token=token,
        timeout=300,
    )
    report["backup_http"] = backup_code
    report["backup_seconds"] = round(backup_seconds, 1)
    report["backup_type"] = backup.get("backup_type", "UNKNOWN")
    report["backup_status"] = backup.get("backup_status", "UNKNOWN")
    report["local_restore_status"] = backup.get("local_restore_status", "UNKNOWN")
    report["remote_restore_status"] = backup.get("remote_restore_status", "UNKNOWN")
    report["isolated_restore_verified"] = backup.get("isolated_restore_verified") is True

    expected_prefix = f"gs://{clean_bucket}/backups/"
    archive_uri = str(backup.get("archive_uri") or "")
    manifest_uri = str(backup.get("manifest_uri") or "")
    report["archive_uri_matches_bucket"] = archive_uri.startswith(expected_prefix)
    report["manifest_uri_matches_bucket"] = manifest_uri.startswith(expected_prefix)
    report["archive_sha256_present"] = bool(backup.get("archive_sha256"))

    if backup_code != 200:
        errors.append("Candidate durable-backup endpoint did not return HTTP 200.")
    if backup.get("status") != "ok":
        errors.append("Candidate durable-backup endpoint did not report API status ok.")
    if report["backup_type"] != "DURABLE_GCS_VERIFIED":
        errors.append("Candidate did not confirm the durable GCS backup path.")
    if report["backup_status"] != "PASS":
        errors.append("Durable backup did not report PASS.")
    if report["local_restore_status"] != "PASS":
        errors.append("Local isolated restore verification did not pass.")
    if report["remote_restore_status"] != "PASS" or not report["isolated_restore_verified"]:
        errors.append("Remote GCS checksum/isolated restore verification did not pass.")
    if not report["archive_uri_matches_bucket"] or not report["manifest_uri_matches_bucket"]:
        errors.append("Durable backup archive/manifest URIs do not match the configured bucket.")
    if not report["archive_sha256_present"]:
        errors.append("Durable backup archive checksum is missing.")

    return report, errors


def main() -> int:
    base_url = os.environ.get("API_CANDIDATE_URL", "")
    operator_key = os.environ.get("DRIPP_OPERATOR_KEY", "")
    bucket = os.environ.get("GCS_BACKUP_BUCKET", "")
    report, errors = bootstrap_candidate_durable_backup(base_url, operator_key, bucket)

    print("Candidate operator login:", "PASS" if report.get("login_http") == 200 else "FAIL")
    print("Durable backup endpoint:", report.get("backup_http", "NOT RUN"))
    print("Backup type:", report.get("backup_type", "NOT REPORTED"))
    print("Backup verification:", report.get("backup_status", "NOT REPORTED"))
    print("Local restore verification:", report.get("local_restore_status", "NOT REPORTED"))
    print("Remote restore verification:", report.get("remote_restore_status", "NOT REPORTED"))
    print("Archive/manifest bucket:", "PASS" if report.get("archive_uri_matches_bucket") and report.get("manifest_uri_matches_bucket") else "FAIL")
    print("Archive checksum present:", "YES" if report.get("archive_sha256_present") else "NO")

    if errors:
        for error in errors:
            print("::error::" + error, file=sys.stderr)
        print("DURABLE PRE-PROMOTION BACKUP: HOLD")
        return 1
    print("DURABLE PRE-PROMOTION BACKUP: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
