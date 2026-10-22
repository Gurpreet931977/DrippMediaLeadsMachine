#!/usr/bin/env python3
"""Wait for a Cloud Run API candidate revision to become ready before tagging it."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any


def _revision_name(value: Any) -> str:
    """Accept both Cloud Run v1 revision names and v2 resource names."""
    if not isinstance(value, str):
        return ""
    return value.rsplit("/", 1)[-1].strip()


def candidate_status(data: dict[str, Any], expected_image: str) -> dict[str, Any]:
    """Normalize v1/v2 service descriptions and report whether this is the ready candidate."""
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    created = _revision_name(
        status.get("latestCreatedRevisionName") or status.get("latestCreatedRevision")
    )
    ready = _revision_name(
        status.get("latestReadyRevisionName") or status.get("latestReadyRevision")
    )

    spec = data.get("spec") if isinstance(data.get("spec"), dict) else {}
    spec_template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    template_spec = (
        spec_template.get("spec") if isinstance(spec_template.get("spec"), dict) else {}
    )
    v2_template = data.get("template") if isinstance(data.get("template"), dict) else {}
    containers = (
        template_spec.get("containers")
        or spec_template.get("containers")
        or v2_template.get("containers")
        or []
    )
    image = (
        containers[0].get("image", "")
        if containers and isinstance(containers[0], dict)
        else ""
    )

    conditions = status.get("conditions") or []
    ready_condition = next(
        (
            item.get("status")
            for item in conditions
            if isinstance(item, dict) and item.get("type") == "Ready"
        ),
        None,
    )
    condition_ok = ready_condition is None or ready_condition in (True, "True")
    is_ready = bool(created and created == ready and condition_ok)

    return {
        "created_revision": created,
        "ready_revision": ready,
        "actual_image": image,
        "ready_condition": ready_condition,
        "ready": is_ready,
        "image_matches": image == expected_image,
        "accepted": is_ready and image == expected_image,
    }


def _describe_service(service: str, region: str, project: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "gcloud", "run", "services", "describe", service,
            f"--region={region}", f"--project={project}", "--format=json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().replace("\n", " ")
        raise RuntimeError(
            f"gcloud describe exited {completed.returncode}"
            + (f": {detail[:500]}" if detail else "")
        )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("gcloud describe did not return valid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("gcloud describe returned JSON that was not an object")
    return data


def wait_for_candidate(
    service: str,
    region: str,
    project: str,
    expected_image: str,
    timeout_seconds: int = 180,
    poll_interval_seconds: int = 5,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    last_state: dict[str, Any] | None = None
    last_error = ""
    next_progress_log = time.monotonic()

    while True:
        try:
            data = _describe_service(service, region, project)
            last_state = candidate_status(data, expected_image)
            last_error = ""
            if last_state["accepted"]:
                revision = last_state["created_revision"]
                print(
                    f"Ready candidate verified: revision={revision}; "
                    f"immutable_image_match=yes",
                    file=sys.stderr,
                )
                print(revision)
                return 0
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            last_error = str(exc)

        now = time.monotonic()
        if now >= deadline:
            break
        if now >= next_progress_log:
            if last_state:
                print(
                    "Waiting for Cloud Run candidate readiness: "
                    f"created={last_state['created_revision'] or 'MISSING'}, "
                    f"ready={last_state['ready_revision'] or 'MISSING'}, "
                    f"image_match={'yes' if last_state['image_matches'] else 'no'}, "
                    f"Ready_condition={last_state['ready_condition']!r}",
                    file=sys.stderr,
                )
            elif last_error:
                print(
                    f"Waiting for Cloud Run service description: {last_error}",
                    file=sys.stderr,
                )
            next_progress_log = now + 15
        time.sleep(min(poll_interval_seconds, max(0, deadline - now)))

    if last_state:
        print(
            "::error::Timed out waiting for the expected API image and a ready "
            "Cloud Run candidate revision. "
            f"created={last_state['created_revision'] or 'MISSING'}, "
            f"ready={last_state['ready_revision'] or 'MISSING'}, "
            f"actual_image={last_state['actual_image'] or 'MISSING'}, "
            f"ready_condition={last_state['ready_condition']!r}.",
            file=sys.stderr,
        )
    else:
        print(
            f"::error::Timed out reading Cloud Run candidate status. {last_error}",
            file=sys.stderr,
        )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--poll-interval-seconds", type=int, default=5)
    args = parser.parse_args()
    if args.timeout_seconds <= 0 or args.poll_interval_seconds <= 0:
        parser.error("timeout and polling interval must be positive")
    return wait_for_candidate(
        args.service,
        args.region,
        args.project,
        args.expected_image,
        args.timeout_seconds,
        args.poll_interval_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
