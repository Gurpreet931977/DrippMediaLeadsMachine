#!/usr/bin/env python3
"""Fail-closed verification for the Cloud Run API service deployment."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from typing import Any


def get(obj: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def verify_service(data: dict[str, Any], mode: str, expected_sa: str,
                   expected_url: str, expected_image: str | None = None) -> list[str]:
    spec = get(data, "spec", "template", "spec") or {}
    containers = spec.get("containers", []) or get(data, "template", "containers") or []
    container = containers[0] if containers and isinstance(containers[0], dict) else {}
    actual_sa = (spec.get("serviceAccountName") or
                 get(data, "spec", "template", "serviceAccount") or
                 get(data, "template", "serviceAccount") or "")
    actual_url = get(data, "status", "url") or ""
    conditions = get(data, "status", "conditions") or []
    ready = next((c.get("status") for c in conditions
                  if isinstance(c, dict) and c.get("type") == "Ready"), None)

    env = {}
    for item in container.get("env", []) or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            env[item["name"]] = item

    def configured(name: str) -> bool:
        e = env.get(name) or {}
        value = e.get("value")
        return bool((isinstance(value, str) and value.strip())
                    or e.get("valueSource") or e.get("valueFrom"))

    errors = []
    print("API URL matches expected:", "YES" if actual_url == expected_url else "NO")
    print("API service Ready:", ready or "NOT REPORTED")
    print("Runtime service account matches expected:", "YES" if actual_sa == expected_sa else "NO")
    print("Operator key configured:", "YES" if (configured("OPERATOR_KEY") or configured("OPERATOR_API_KEY")) else "NO")
    print("Google Sheets ID configured:", "YES" if (configured("GOOGLE_SHEET_ID") or configured("GOOGLE_SHEET_KEY")) else "NO")
    print("Durable backup bucket configured:", "YES" if configured("GCS_BACKUP_BUCKET") else "NO")

    if ready not in (True, "True"):
        errors.append("Cloud Run API service is not Ready.")
    if actual_sa != expected_sa:
        errors.append("Cloud Run API runtime service account is missing or unexpected.")
    if actual_url != expected_url:
        errors.append("Cloud Run API URL does not match the configured API origin.")
    if not containers:
        errors.append("Cloud Run API service has no configured container.")
    if not (configured("OPERATOR_KEY") or configured("OPERATOR_API_KEY")):
        errors.append("Operator authentication is not configured on the API service.")
    if not (configured("GOOGLE_SHEET_ID") or configured("GOOGLE_SHEET_KEY")):
        errors.append("Google Sheets CRM ID/key is not configured on the API service.")
    if not configured("GCS_BACKUP_BUCKET"):
        errors.append("Durable backup bucket is not configured on the API service.")

    allowed = {
        "TRAVEL_MODE": {"active", "true", "1", "yes"},
        "COMMERCIAL_ACTIONS_ENABLED": {"false"},
        "AUTOMATED_EMAIL_ENABLED": {"false"},
    }
    if mode == "deployed":
        allowed["TECHNICAL_AUTOMATION_ENABLED"] = {"true"}
    for name, values in allowed.items():
        value = env.get(name, {}).get("value", "")
        value = value.strip().lower() if isinstance(value, str) else ""
        if value not in values:
            errors.append(f"API safety invariant {name} is missing or incorrect.")

    if mode == "deployed":
        actual_image = container.get("image", "")
        command = container.get("command") or []
        args = container.get("args") or []
        expected_args = ["server:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
        print("API image matches expected immutable image:", "YES" if actual_image == expected_image else "NO")
        print("API command is Uvicorn:", "YES" if command == ["uvicorn"] else "NO")
        print("API args match server entrypoint:", "YES" if args == expected_args else "NO")
        if actual_image != expected_image:
            errors.append("API service image does not match the immutable deployment image.")
        if command != ["uvicorn"]:
            errors.append("API service command must be uvicorn.")
        if args != expected_args:
            errors.append("API service arguments do not match the expected Uvicorn entrypoint.")
    return errors


def main() -> int:
    if len(sys.argv) not in (5, 6):
        print("Usage: verify_cloud_run_api_service.py preflight|deployed JSON_FILE EXPECTED_SA EXPECTED_URL [EXPECTED_IMAGE]", file=sys.stderr)
        return 2
    mode, filename, expected_sa, expected_url = sys.argv[1:5]
    expected_image = sys.argv[5] if len(sys.argv) == 6 else None
    if mode not in ("preflight", "deployed") or (mode == "deployed" and not expected_image):
        print("Invalid verification mode or missing expected image.", file=sys.stderr)
        return 2
    try:
        data = json.loads(Path(filename).read_text(encoding="utf-8"))
    except Exception as exc:
        print("Failed to parse Cloud Run API service JSON: " + type(exc).__name__, file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("Cloud Run API service JSON must be an object.", file=sys.stderr)
        return 1
    errors = verify_service(data, mode, expected_sa, expected_url, expected_image)
    if errors:
        for error in errors:
            print("::error::" + error, file=sys.stderr)
        return 1
    print("API service verification passed. Secret values were not read or printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
