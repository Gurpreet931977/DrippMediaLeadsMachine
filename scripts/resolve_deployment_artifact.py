#!/usr/bin/env python3
"""
scripts/resolve_deployment_artifact.py
=======================================
Fail-closed resolution and verification of deployment container artifact identity,
digests, and build provenance for Cloud Run releases.

Eliminates stale hard-coded digests and tags by dynamically resolving:
1. For standard releases: The immutable sha256 digest from the image pushed
   to Artifact Registry in the current workflow run.
2. For promote-only releases: The verified image reference and recorded digest
   from the specified Cloud Run candidate revision, authenticated against Artifact Registry.
3. Verification: Validating that the deployed Cloud Run candidate revision's
   status.imageDigest strictly matches the verified artifact digest and authorized
   image base, preventing stale artifact promotion or accidental traffic routing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SHA256_REGEX = re.compile(r"^sha256:[a-f0-9]{64}$")


def validate_digest_format(digest: str) -> bool:
    """Validate that digest is an exact, non-empty sha256:64hex string."""
    if not isinstance(digest, str):
        return False
    return bool(SHA256_REGEX.match(digest.strip()))


def validate_authorized_image_base(image_ref: str, authorized_base: str) -> bool:
    """Validate that image_ref strictly points to the authorized repository base."""
    if not isinstance(image_ref, str) or not isinstance(authorized_base, str):
        return False
    clean_ref = image_ref.strip()
    clean_base = authorized_base.strip()
    if clean_ref == clean_base:
        return True
    return clean_ref.startswith(clean_base + ":") or clean_ref.startswith(clean_base + "@")


def extract_digest_from_repo_digests(repo_digests_text: str, image_base: str) -> str | None:
    """Extract sha256 digest from docker inspect .RepoDigests output."""
    if not repo_digests_text or not isinstance(repo_digests_text, str):
        return None
    for line in repo_digests_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "@sha256:" in line:
            prefix, digest_part = line.split("@", 1)
            digest = digest_part.strip()
            if validate_digest_format(digest):
                if not image_base or prefix.strip() == image_base.strip():
                    return digest
    return None


def extract_digest_from_registry_output(registry_data: dict[str, Any] | str) -> str | None:
    """Extract sha256 digest from gcloud artifacts docker images describe output."""
    if isinstance(registry_data, str):
        raw = registry_data.strip()
        if validate_digest_format(raw):
            return raw
        try:
            registry_data = json.loads(raw)
        except Exception:
            return None

    if isinstance(registry_data, dict):
        summary = registry_data.get("image_summary") or {}
        digest = summary.get("digest") or registry_data.get("digest")
        if isinstance(digest, str) and validate_digest_format(digest):
            return digest.strip()
        version = registry_data.get("version") or summary.get("version")
        if isinstance(version, str) and validate_digest_format(version):
            return version.strip()
    return None


def resolve_built_artifact(
    image_base: str,
    tag: str,
    full_sha: str,
    repo_digests_text: str | None = None,
    registry_query_fn: Callable[[str], str | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Resolves the immutable artifact digest for an image built and pushed in the current run.
    Fails closed if the digest cannot be resolved or is malformed.
    """
    if not image_base or not isinstance(image_base, str):
        raise ValueError("image_base must be a non-empty string.")
    if not tag or not isinstance(tag, str):
        raise ValueError("tag must be a non-empty string.")
    if not full_sha or not isinstance(full_sha, str):
        raise ValueError("full_sha must be a non-empty string.")

    target_image = f"{image_base.strip()}:{tag.strip()}"
    digest: str | None = None

    if repo_digests_text:
        digest = extract_digest_from_repo_digests(repo_digests_text, image_base)

    if not digest and registry_query_fn:
        try:
            reg_out = registry_query_fn(target_image)
            digest = extract_digest_from_registry_output(reg_out)
        except Exception as exc:
            raise RuntimeError(f"Failed to query Artifact Registry for {target_image}: {exc}") from exc

    if not digest:
        raise ValueError(
            f"Could not resolve immutable image digest for built artifact '{target_image}'. "
            "Artifact identity could not be established."
        )

    if not validate_digest_format(digest):
        raise ValueError(f"Resolved image digest '{digest}' does not match expected sha256 format.")

    return {
        "mode": "build_and_deploy",
        "image_base": image_base.strip(),
        "tag": tag.strip(),
        "full_sha": full_sha.strip(),
        "expected_image": target_image,
        "expected_digest": digest,
        "immutable_reference": f"{image_base.strip()}@{digest}",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


def resolve_candidate_artifact(
    revision_data: dict[str, Any],
    authorized_image_base: str,
    registry_query_fn: Callable[[str], str | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Resolves and validates the image reference and digest for an existing candidate revision.
    Fails closed if the candidate is not from the authorized repository or has no recorded digest.
    """
    if not isinstance(revision_data, dict):
        raise ValueError("revision_data must be a dictionary.")

    metadata = revision_data.get("metadata") or {}
    spec = revision_data.get("spec") or {}
    status = revision_data.get("status") or {}

    revision_name = metadata.get("name") or ""
    if not revision_name:
        raise ValueError("Candidate revision metadata is missing 'name'.")

    # Extract configured container image
    template_spec = (
        spec.get("template", {}).get("spec")
        if isinstance(spec.get("template"), dict)
        else spec
    )
    containers = template_spec.get("containers") or []
    if not containers or not isinstance(containers[0], dict):
        raise ValueError(f"Candidate revision '{revision_name}' has no configured containers.")

    cand_image = str(containers[0].get("image") or "").strip()
    if not cand_image:
        raise ValueError(f"Candidate revision '{revision_name}' container image is empty.")

    if not validate_authorized_image_base(cand_image, authorized_image_base):
        raise ValueError(
            f"Candidate revision image '{cand_image}' does not point to authorized image base '{authorized_image_base}'."
        )

    # Extract recorded status.imageDigest
    cand_digest = str(status.get("imageDigest") or "").strip()
    # Handle possible format: 'us-central1-docker.pkg.dev/...@sha256:...'
    if "@" in cand_digest:
        cand_digest = cand_digest.split("@")[-1].strip()

    if not validate_digest_format(cand_digest):
        raise ValueError(
            f"Candidate revision '{revision_name}' does not have a valid recorded status.imageDigest (found: '{cand_digest}')."
        )

    # Authenticate against Artifact Registry if query function provided
    if registry_query_fn:
        try:
            reg_out = registry_query_fn(cand_image)
            reg_digest = extract_digest_from_registry_output(reg_out)
            if reg_digest and reg_digest != cand_digest:
                raise ValueError(
                    f"Candidate revision digest '{cand_digest}' does not match Artifact Registry digest '{reg_digest}'."
                )
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise
            raise RuntimeError(f"Failed to verify candidate image in Artifact Registry: {exc}") from exc

    return {
        "mode": "promote_only",
        "candidate_revision": revision_name,
        "image_base": authorized_image_base.strip(),
        "expected_image": cand_image,
        "expected_digest": cand_digest,
        "immutable_reference": f"{authorized_image_base.strip()}@{cand_digest}",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


def verify_candidate_revision_digest(
    candidate_revision_data: dict[str, Any],
    expected_digest: str,
    expected_image: str | None = None,
    authorized_image_base: str | None = None,
) -> list[str]:
    """
    Verifies that a candidate revision's deployed container image and status.imageDigest
    strictly match the expected immutable digest and authorized image reference.
    Returns a list of error strings (empty on full verification pass).
    """
    errors: list[str] = []

    if not validate_digest_format(expected_digest):
        errors.append(f"Expected image digest is empty or invalid sha256 format: '{expected_digest}'.")
        return errors

    if not isinstance(candidate_revision_data, dict):
        errors.append("Candidate revision data must be an object.")
        return errors

    metadata = candidate_revision_data.get("metadata") or {}
    spec = candidate_revision_data.get("spec") or {}
    status = candidate_revision_data.get("status") or {}

    rev_name = metadata.get("name") or "UNKNOWN_REVISION"
    cand_digest_raw = str(status.get("imageDigest") or "").strip()
    cand_digest = cand_digest_raw.split("@")[-1].strip() if "@" in cand_digest_raw else cand_digest_raw

    print(f"Candidate revision name:   {rev_name}")
    print(f"Expected image digest:     {expected_digest}")
    print(f"Actual Cloud Run digest:   {cand_digest or 'MISSING'}")

    if not cand_digest:
        errors.append(f"Candidate revision '{rev_name}' did not report status.imageDigest.")
    elif not validate_digest_format(cand_digest):
        errors.append(f"Candidate revision '{rev_name}' reported malformed imageDigest: '{cand_digest}'.")
    elif cand_digest != expected_digest:
        errors.append(
            f"Candidate image digest mismatch! Expected {expected_digest}, got {cand_digest}."
        )

    template_spec = (
        spec.get("template", {}).get("spec")
        if isinstance(spec.get("template"), dict)
        else spec
    )
    containers = template_spec.get("containers") or []
    actual_image = str(containers[0].get("image") or "").strip() if containers else ""

    print(f"Actual configured image:   {actual_image or 'MISSING'}")

    if authorized_image_base:
        print(f"Authorized image base:     {authorized_image_base}")
        if not validate_authorized_image_base(actual_image, authorized_image_base):
            errors.append(
                f"Candidate container image '{actual_image}' is not from authorized base '{authorized_image_base}'."
            )

    if expected_image:
        print(f"Expected container image:  {expected_image}")
        if actual_image != expected_image:
            errors.append(
                f"Candidate container image '{actual_image}' does not match expected image '{expected_image}'."
            )

    return errors


def _gcloud_registry_query(image_ref: str) -> str:
    """Invokes gcloud artifacts to describe a docker image."""
    cmd = [
        "gcloud", "artifacts", "docker", "images", "describe",
        image_ref, "--format=json",
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    return res.stdout


def _write_outputs(artifact_info: dict[str, Any], output_file: str | None, github_output: str | None) -> None:
    """Persists resolved artifact info to JSON file and appends to $GITHUB_OUTPUT."""
    if output_file:
        out_p = Path(output_file)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(artifact_info, indent=2), encoding="utf-8")
        print(f"Wrote resolved artifact info to {output_file}")

    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"expected_image={artifact_info['expected_image']}\n")
            f.write(f"expected_digest={artifact_info['expected_digest']}\n")
            f.write(f"immutable_reference={artifact_info['immutable_reference']}\n")
            if "tag" in artifact_info:
                f.write(f"deploy_tag={artifact_info['tag']}\n")
            if "candidate_revision" in artifact_info:
                f.write(f"candidate_revision={artifact_info['candidate_revision']}\n")
        print(f"Appended artifact variables to $GITHUB_OUTPUT ({github_output})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve and verify deployment container artifact identity.")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. resolve-built
    p_built = subparsers.add_parser("resolve-built", help="Resolve digest for freshly pushed container image.")
    p_built.add_argument("--image-base", required=True, help="Artifact Registry base repository")
    p_built.add_argument("--tag", required=True, help="Immutable tag (full commit SHA)")
    p_built.add_argument("--full-sha", required=True, help="Git commit SHA")
    p_built.add_argument("--output-file", help="Path to write resolution JSON")
    p_built.add_argument("--github-output", help="Path to $GITHUB_OUTPUT")

    # 2. resolve-candidate
    p_cand = subparsers.add_parser("resolve-candidate", help="Resolve artifact from existing Cloud Run revision.")
    p_cand.add_argument("--service", required=True, help="Cloud Run service name")
    p_cand.add_argument("--revision", required=True, help="Candidate revision name to inspect")
    p_cand.add_argument("--region", required=True, help="GCP region")
    p_cand.add_argument("--project", required=True, help="GCP project ID")
    p_cand.add_argument("--authorized-image-base", required=True, help="Authorized image base")
    p_cand.add_argument("--output-file", help="Path to write resolution JSON")
    p_cand.add_argument("--github-output", help="Path to $GITHUB_OUTPUT")

    # 3. verify-candidate-digest
    p_ver = subparsers.add_parser("verify-candidate-digest", help="Verify revision image digest matches resolved digest.")
    p_ver.add_argument("--service", required=True, help="Cloud Run service name")
    p_ver.add_argument("--revision", required=True, help="Candidate revision name")
    p_ver.add_argument("--region", required=True, help="GCP region")
    p_ver.add_argument("--project", required=True, help="GCP project ID")
    p_ver.add_argument("--artifact-file", help="Path to resolution JSON written by resolver")
    p_ver.add_argument("--expected-digest", help="Expected sha256 digest")
    p_ver.add_argument("--expected-image", help="Expected container image")
    p_ver.add_argument("--authorized-image-base", help="Authorized image base")

    args = parser.parse_args()

    try:
        if args.subcommand == "resolve-built":
            target_image = f"{args.image_base}:{args.tag}"
            repo_digests_raw = ""
            try:
                insp = subprocess.run(
                    ["docker", "inspect", "--format={{range .RepoDigests}}{{.}}{{\"\\n\"}}{{end}}", target_image],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                if insp.returncode == 0:
                    repo_digests_raw = insp.stdout
            except Exception:
                pass

            info = resolve_built_artifact(
                image_base=args.image_base,
                tag=args.tag,
                full_sha=args.full_sha,
                repo_digests_text=repo_digests_raw,
                registry_query_fn=_gcloud_registry_query,
            )
            print("Successfully resolved built artifact:")
            print(f"  Image:     {info['expected_image']}")
            print(f"  Digest:    {info['expected_digest']}")
            print(f"  Immutable: {info['immutable_reference']}")
            _write_outputs(info, args.output_file, args.github_output)
            return 0

        elif args.subcommand == "resolve-candidate":
            rev_cmd = [
                "gcloud", "run", "revisions", "describe", args.revision,
                f"--region={args.region}", f"--project={args.project}", "--format=json",
            ]
            rev_res = subprocess.run(rev_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            rev_data = json.loads(rev_res.stdout)

            info = resolve_candidate_artifact(
                revision_data=rev_data,
                authorized_image_base=args.authorized_image_base,
                registry_query_fn=_gcloud_registry_query,
            )
            print("Successfully resolved existing candidate artifact:")
            print(f"  Revision:  {info['candidate_revision']}")
            print(f"  Image:     {info['expected_image']}")
            print(f"  Digest:    {info['expected_digest']}")
            print(f"  Immutable: {info['immutable_reference']}")
            _write_outputs(info, args.output_file, args.github_output)
            return 0

        elif args.subcommand == "verify-candidate-digest":
            exp_digest = args.expected_digest
            exp_image = args.expected_image

            if args.artifact_file:
                art_data = json.loads(Path(args.artifact_file).read_text(encoding="utf-8"))
                exp_digest = exp_digest or art_data.get("expected_digest")
                exp_image = exp_image or art_data.get("expected_image")

            if not exp_digest:
                print("::error::Expected digest must be provided via --expected-digest or --artifact-file.", file=sys.stderr)
                return 1

            rev_cmd = [
                "gcloud", "run", "revisions", "describe", args.revision,
                f"--region={args.region}", f"--project={args.project}", "--format=json",
            ]
            rev_res = subprocess.run(rev_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            rev_data = json.loads(rev_res.stdout)

            errors = verify_candidate_revision_digest(
                candidate_revision_data=rev_data,
                expected_digest=exp_digest,
                expected_image=exp_image,
                authorized_image_base=args.authorized_image_base,
            )

            if errors:
                for err in errors:
                    print(f"::error::[Artifact Verification Failure] {err}", file=sys.stderr)
                return 1

            print("Artifact identity and digest verification passed. Immutable provenance established.")
            return 0

    except Exception as exc:
        print(f"::error::[Artifact Resolution Error] {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
