#!/usr/bin/env python3
"""Tag a no-primary-traffic Cloud Run candidate, probe it, and wait for readiness."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def _revision_name(value: Any) -> str:
    """Accept both Cloud Run v1 revision names and v2 resource names."""
    if not isinstance(value, str):
        return ""
    return value.rsplit("/", 1)[-1].strip()


def candidate_status(data: dict[str, Any], expected_image: str) -> dict[str, Any]:
    """Separate candidate identification from candidate readiness.

    Service-level Ready conditions can describe the still-serving primary revision;
    latestReadyRevision* is the relevant readiness signal for this candidate.
    """
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
    service_ready_condition = next(
        (
            item.get("status")
            for item in conditions
            if isinstance(item, dict) and item.get("type") == "Ready"
        ),
        None,
    )
    image_matches = image == expected_image
    ready = bool(created and created == ready)

    return {
        "created_revision": created,
        "ready_revision": _revision_name(
            status.get("latestReadyRevisionName") or status.get("latestReadyRevision")
        ),
        "actual_image": image,
        "service_ready_condition": service_ready_condition,
        "candidate_identified": bool(created and image_matches),
        "ready": ready,
        "image_matches": image_matches,
        "accepted": ready and image_matches,
    }


def primary_traffic_matches(data: dict[str, Any], expected_primary_revision: str) -> bool:
    """Fail closed unless the old primary is still the only weighted revision."""
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    traffic = status.get("traffic") or []
    weighted: list[dict[str, Any]] = []
    for item in traffic:
        if not isinstance(item, dict):
            continue
        percent = item.get("percent")
        if isinstance(percent, (int, float)) and percent > 0:
            weighted.append(item)
    return (
        len(weighted) == 1
        and _revision_name(weighted[0].get("revisionName") or weighted[0].get("revision")) == expected_primary_revision
        and float(weighted[0].get("percent", 0)) == 100.0
    )


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
            f"gcloud service describe exited {completed.returncode}"
            + (f": {detail[:500]}" if detail else "")
        )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("gcloud service describe did not return valid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("gcloud service describe returned JSON that was not an object")
    return data


def _tag_candidate(service: str, region: str, project: str, revision: str) -> None:
    """Add a URL tag without changing the weighted primary traffic assignment."""
    completed = subprocess.run(
        [
            "gcloud", "run", "services", "update-traffic", service,
            f"--region={region}", f"--project={project}",
            f"--update-tags=release-candidate={revision}", "--quiet",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().replace("\n", " ")
        raise RuntimeError(
            f"Unable to assign the release-candidate tag (exit {completed.returncode})"
            + (f": {detail[:700]}" if detail else "")
        )


def _probe_candidate(candidate_url: str, timeout_seconds: int = 10) -> tuple[bool, str]:
    """Probe the tagged candidate's health endpoint; never contact the primary URL."""
    url = candidate_url.rstrip("/") + "/api/system/health"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "dripp-cloud-run-candidate-readiness/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            code = int(response.status)
            return code == 200, f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, type(exc).__name__


def _print_revision_diagnostics(
    revision: str, region: str, project: str
) -> None:
    """Print bounded condition diagnostics without exposing environment values."""
    completed = subprocess.run(
        [
            "gcloud", "run", "revisions", "describe", revision,
            f"--region={region}", f"--project={project}", "--format=json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().replace("\n", " ")
        print(
            "Candidate revision detail lookup failed: "
            + (detail[:700] if detail else f"exit={completed.returncode}"),
            file=sys.stderr,
        )
        return
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        print("Candidate revision detail lookup returned invalid JSON.", file=sys.stderr)
        return

    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    print(
        "Candidate revision diagnostics: "
        f"name={metadata.get('name', revision)}, "
        f"image_digest={status.get('imageDigest', 'not reported')}",
        file=sys.stderr,
    )
    for condition in status.get("conditions") or []:
        if not isinstance(condition, dict):
            continue
        message = str(condition.get("message") or "").replace("\n", " ")
        print(
            f"  condition={condition.get('type', 'unknown')}, "
            f"status={condition.get('status', 'unknown')}, "
            f"reason={condition.get('reason', 'not reported')}, "
            f"message={message[:700]}",
            file=sys.stderr,
        )


def wait_for_candidate(
    service: str,
    region: str,
    project: str,
    expected_image: str,
    candidate_url: str,
    expected_primary_revision: str,
    timeout_seconds: int = 180,
    poll_interval_seconds: int = 5,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    candidate_revision = ""
    tagged = False
    last_state: dict[str, Any] | None = None
    last_error = ""
    last_probe = "not attempted"
    next_progress_log = time.monotonic()

    while time.monotonic() < deadline:
        try:
            data = _describe_service(service, region, project)
            last_state = candidate_status(data, expected_image)
            last_error = ""

            if not primary_traffic_matches(data, expected_primary_revision):
                print(
                    "::error::Weighted primary traffic changed before candidate promotion; "
                    "refusing to continue.",
                    file=sys.stderr,
                )
                return 1

            if not candidate_revision:
                if last_state["candidate_identified"]:
                    candidate_revision = last_state["created_revision"]
                    print(
                        f"Candidate identified by immutable image: {candidate_revision}",
                        file=sys.stderr,
                    )
                    _tag_candidate(service, region, project, candidate_revision)
                    tagged = True
                    print(
                        "Candidate-only release-candidate tag assigned. "
                        "The existing 100% primary traffic assignment was not changed.",
                        file=sys.stderr,
                    )
                else:
                    last_probe = "waiting for service to report expected immutable image"
            elif last_state["created_revision"] != candidate_revision:
                print(
                    "::error::A different revision became latest-created while candidate "
                    f"{candidate_revision} was being verified; refusing to tag/promote it.",
                    file=sys.stderr,
                )
                return 1
            elif not last_state["image_matches"]:
                print(
                    "::error::Candidate service image no longer matches the immutable build image.",
                    file=sys.stderr,
                )
                return 1

            if candidate_revision and tagged:
                probe_ok, probe_detail = _probe_candidate(candidate_url)
                last_probe = probe_detail
                # Re-read service status after a successful tagged URL request.
                # This request allows Cloud Run to activate the no-primary-traffic candidate.
                if probe_ok:
                    refreshed = _describe_service(service, region, project)
                    refreshed_state = candidate_status(refreshed, expected_image)
                    if not primary_traffic_matches(refreshed, expected_primary_revision):
                        print(
                            "::error::Primary traffic changed while checking candidate health; "
                            "refusing to continue.",
                            file=sys.stderr,
                        )
                        return 1
                    if (
                        refreshed_state["created_revision"] == candidate_revision
                        and refreshed_state["accepted"]
                    ):
                        print(
                            f"Candidate readiness verified: revision={candidate_revision}; "
                            "candidate_health=HTTP 200; primary_traffic_preserved=yes",
                            file=sys.stderr,
                        )
                        print(candidate_revision)
                        return 0
                    last_state = refreshed_state

        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            last_error = str(exc)

        now = time.monotonic()
        if now >= next_progress_log:
            if last_state:
                print(
                    "Waiting for tagged Cloud Run candidate: "
                    f"candidate={candidate_revision or last_state['created_revision'] or 'MISSING'}, "
                    f"ready={last_state['ready_revision'] or 'MISSING'}, "
                    f"image_match={'yes' if last_state['image_matches'] else 'no'}, "
                    f"service_ready_condition={last_state['service_ready_condition']!r}, "
                    f"tagged={'yes' if tagged else 'no'}, "
                    f"candidate_health={last_probe}",
                    file=sys.stderr,
                )
            elif last_error:
                print(
                    f"Waiting for Cloud Run service description: {last_error}",
                    file=sys.stderr,
                )
            next_progress_log = now + 15
        time.sleep(min(poll_interval_seconds, max(0, deadline - now)))

    print(
        "::error::Timed out waiting for the tagged API candidate to become healthy and ready. "
        f"candidate={candidate_revision or (last_state or {}).get('created_revision') or 'MISSING'}, "
        f"ready={(last_state or {}).get('ready_revision') or 'MISSING'}, "
        f"image_match={'yes' if (last_state or {}).get('image_matches') else 'no'}, "
        f"tagged={'yes' if tagged else 'no'}, candidate_health={last_probe}, "
        f"detail={last_error or 'no additional service-description error'}.",
        file=sys.stderr,
    )
    if candidate_revision:
        _print_revision_diagnostics(candidate_revision, region, project)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--candidate-url", required=True)
    parser.add_argument("--expected-primary-revision", required=True)
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
        args.candidate_url,
        args.expected_primary_revision,
        args.timeout_seconds,
        args.poll_interval_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
