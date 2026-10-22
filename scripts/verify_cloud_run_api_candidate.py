#!/usr/bin/env python3
"""Verify an isolated Cloud Run API candidate before assigning primary traffic.

The operator key and session token are kept in process memory and never printed.
Only status summaries are emitted for CI logs.
"""
from __future__ import annotations

import json
import os
import sys
import time
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
        # Do not echo exception/request objects into CI logs; they can contain URLs or headers.
        return 0, {"request_error": True}, time.monotonic() - started


def verify_candidate(
    base_url: str,
    operator_key: str,
    request_fn: RequestFn = request_json,
) -> tuple[dict[str, Any], list[str]]:
    report: dict[str, Any] = {}
    errors: list[str] = []

    code, health, elapsed = request_fn(base_url, "/api/system/health", timeout=30)
    report["health_http"] = code
    report["health_status"] = health.get("canonical_health_status", health.get("status", "UNKNOWN"))
    report["health_seconds"] = round(elapsed, 1)
    if code != 200 or report["health_status"] != "OPERATIONAL":
        errors.append("Candidate health endpoint did not report HTTP 200 and OPERATIONAL status.")

    login_code, login, elapsed = request_fn(
        base_url, "/api/operator/login", method="POST",
        payload={"operator_key": operator_key}, timeout=30
    )
    token = login.get("session_token") if isinstance(login, dict) else None
    report["login_http"] = login_code
    report["login_seconds"] = round(elapsed, 1)
    report["login_ok"] = login_code == 200 and isinstance(token, str) and bool(token)
    if not report["login_ok"]:
        errors.append("Candidate operator login failed.")
        return report, errors

    crm_code, crm, elapsed = request_fn(base_url, "/api/crm/status", token=token, timeout=60)
    report["crm_http"] = crm_code
    report["crm_seconds"] = round(elapsed, 1)
    report["crm_connected"] = crm.get("crm_connected") is True
    report["crm_source"] = crm.get("source", "UNKNOWN")
    report["crm_tabs"] = {
        name: {"status": info.get("status", "UNKNOWN"), "count": info.get("count")}
        for name, info in (crm.get("tabs", {}) or {}).items()
        if isinstance(info, dict)
    }
    if crm_code != 200 or not report["crm_connected"]:
        errors.append("Candidate CRM check did not return HTTP 200 with crm_connected=true.")

    ready_code, readiness, elapsed = request_fn(
        base_url, "/api/system/readiness", token=token, timeout=150
    )
    report["readiness_http"] = ready_code
    report["readiness_seconds"] = round(elapsed, 1)
    report["overall_status"] = readiness.get("overall_status", "UNKNOWN")
    report["readiness_state"] = readiness.get("readiness_state", "UNKNOWN")
    report["passed_gates_count"] = readiness.get("passed_gates_count")
    report["warning_gates_count"] = readiness.get("warning_gates_count")
    report["blocking_gates_count"] = readiness.get("blocking_gates_count")
    report["blocking_gates"] = readiness.get("blocking_gates", [])
    if ready_code != 200:
        errors.append("Candidate readiness endpoint did not return HTTP 200.")
    if report["overall_status"] != "PASS":
        errors.append("Candidate readiness overall_status is not PASS.")
    if report["warning_gates_count"] != 0:
        errors.append("Candidate readiness must report zero warning gates.")
    if not isinstance(report["blocking_gates_count"], int) or report["blocking_gates_count"] != 0:
        errors.append("Candidate readiness must report zero blocking gates.")
    return report, errors


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: verify_cloud_run_api_candidate.py CANDIDATE_BASE_URL", file=sys.stderr)
        return 2
    operator_key = os.environ.get("DRIPP_OPERATOR_KEY", "")
    if not operator_key:
        print("DRIPP_OPERATOR_KEY environment secret is required for candidate readiness.", file=sys.stderr)
        return 2

    report, errors = verify_candidate(sys.argv[1], operator_key)
    print("Candidate health:", report.get("health_http"), report.get("health_status", "UNKNOWN"))
    print("Operator login:", "PASS" if report.get("login_ok") else f"FAIL (HTTP {report.get('login_http', 0)})")
    print("CRM:", "PASS" if report.get("crm_connected") and report.get("crm_http") == 200 else f"FAIL (HTTP {report.get('crm_http', 0)})")
    if report.get("crm_source"):
        print("CRM source:", report["crm_source"])
    for name, info in report.get("crm_tabs", {}).items():
        print(f"  {name}: {info['status']}; rows={info['count']}")
    print("Readiness HTTP:", report.get("readiness_http", "NOT RUN"))
    print("Overall status:", report.get("overall_status", "NOT REPORTED"))
    print("Readiness state:", report.get("readiness_state", "NOT REPORTED"))
    print("Passed gates:", report.get("passed_gates_count", "NOT REPORTED"))
    print("Warning gates:", report.get("warning_gates_count", "NOT REPORTED"))
    print("Blocking gates:", report.get("blocking_gates_count", "NOT REPORTED"))
    print("Blocking gate IDs:", ", ".join(map(str, report.get("blocking_gates", []) or [])) or "none reported")
    print("Readiness duration (seconds):", report.get("readiness_seconds", "NOT REPORTED"))
    if errors:
        for error in errors:
            print("::error::" + error, file=sys.stderr)
        print("CANDIDATE RESULT: HOLD")
        return 1
    print("CANDIDATE RESULT: PASS — health, operator authentication, CRM, and readiness verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
