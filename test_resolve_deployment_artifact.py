"""Unit tests for deployment artifact resolution, digest verification, and workflow safeguards."""

import json
from pathlib import Path
import pytest
import yaml

from scripts.resolve_deployment_artifact import (
    validate_digest_format,
    validate_authorized_image_base,
    extract_digest_from_repo_digests,
    extract_digest_from_registry_output,
    resolve_built_artifact,
    resolve_candidate_artifact,
    verify_candidate_revision_digest,
)

SAMPLE_VALID_DIGEST = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
STALE_PREVIOUS_DIGEST = "sha256:991bb03c169edd467eb3c4c8548f6907f5d749d5ca69f6a800541d173812cc89"
NEW_CANDIDATE_DIGEST = "sha256:4a5e1e550998c322b78fc100882ab27f8eb25c2759e66380678dcf4f20ec6859"
VALID_IMAGE_BASE = "us-central1-docker.pkg.dev/poised-eye-509816-a4/dripp-media/dripp-discovery-job"


class TestDigestFormatValidation:
    """Validate sha256 digest format checking."""

    def test_valid_sha256_format(self):
        assert validate_digest_format(SAMPLE_VALID_DIGEST) is True
        assert validate_digest_format(NEW_CANDIDATE_DIGEST) is True
        assert validate_digest_format(STALE_PREVIOUS_DIGEST) is True

    def test_invalid_sha256_format_cases(self):
        # Empty / whitespace / None
        assert validate_digest_format("") is False
        assert validate_digest_format("   ") is False
        assert validate_digest_format(None) is False
        assert validate_digest_format(12345) is False
        # Wrong prefix
        assert validate_digest_format("sha1:e3b0c44298fc1c149afbf4c8996fb92427ae41e4") is False
        assert validate_digest_format("md5:e3b0c44298fc1c149afbf4c8996fb924") is False
        # Missing prefix
        assert validate_digest_format("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855") is False
        # Invalid length (too short / too long)
        assert validate_digest_format("sha256:e3b0c44298fc") is False
        assert validate_digest_format(SAMPLE_VALID_DIGEST + "aa") is False
        # Non-hex characters
        assert validate_digest_format("sha256:g3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855") is False
        assert validate_digest_format("sha256:E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855") is False


class TestAuthorizedImageBaseValidation:
    """Validate repository prefix and authorized base domain."""

    def test_authorized_image_matches(self):
        assert validate_authorized_image_base(
            f"{VALID_IMAGE_BASE}:da1bd5e",
            VALID_IMAGE_BASE,
        ) is True
        assert validate_authorized_image_base(
            f"{VALID_IMAGE_BASE}@{SAMPLE_VALID_DIGEST}",
            VALID_IMAGE_BASE,
        ) is True
        assert validate_authorized_image_base(
            VALID_IMAGE_BASE,
            VALID_IMAGE_BASE,
        ) is True

    def test_unauthorized_images_rejected(self):
        # Unauthorized public registry
        assert validate_authorized_image_base("docker.io/library/python:3.11", VALID_IMAGE_BASE) is False
        # Unauthorized GCP project
        assert validate_authorized_image_base(
            "us-central1-docker.pkg.dev/malicious-project/dripp-media/dripp-discovery-job:da1bd5e",
            VALID_IMAGE_BASE,
        ) is False
        # Unauthorized path
        assert validate_authorized_image_base(
            "us-central1-docker.pkg.dev/poised-eye-509816-a4/other-repo/other-job:da1bd5e",
            VALID_IMAGE_BASE,
        ) is False
        # Empty string / invalid inputs
        assert validate_authorized_image_base("", VALID_IMAGE_BASE) is False
        assert validate_authorized_image_base(None, VALID_IMAGE_BASE) is False


class TestDigestExtraction:
    """Test extracting digest from local repo digests and remote registry output."""

    def test_extract_from_repo_digests_matches(self):
        repo_digests = f"{VALID_IMAGE_BASE}@{NEW_CANDIDATE_DIGEST}\nsome-other-tag@{SAMPLE_VALID_DIGEST}"
        digest = extract_digest_from_repo_digests(repo_digests, VALID_IMAGE_BASE)
        assert digest == NEW_CANDIDATE_DIGEST

    def test_extract_from_repo_digests_no_match(self):
        repo_digests = "us-central1-docker.pkg.dev/unrelated/img@sha256:1111111111111111111111111111111111111111111111111111111111111111"
        digest = extract_digest_from_repo_digests(repo_digests, VALID_IMAGE_BASE)
        assert digest is None

    def test_extract_from_registry_json_output(self):
        registry_json = json.dumps({
            "image_summary": {
                "digest": NEW_CANDIDATE_DIGEST,
            }
        })
        digest = extract_digest_from_registry_output(registry_json)
        assert digest == NEW_CANDIDATE_DIGEST

    def test_extract_from_registry_top_level_digest(self):
        registry_json = json.dumps({
            "digest": NEW_CANDIDATE_DIGEST,
        })
        digest = extract_digest_from_registry_output(registry_json)
        assert digest == NEW_CANDIDATE_DIGEST

    def test_extract_from_registry_raw_valid_digest(self):
        digest = extract_digest_from_registry_output(f"  {NEW_CANDIDATE_DIGEST}  \n")
        assert digest == NEW_CANDIDATE_DIGEST

    def test_extract_from_registry_malformed_output(self):
        assert extract_digest_from_registry_output("no digest here") is None
        assert extract_digest_from_registry_output("{}") is None
        assert extract_digest_from_registry_output("") is None


class TestResolveBuiltArtifact:
    """Test standard build-and-deploy resolution logic."""

    def test_resolve_built_artifact_from_repo_digests(self):
        repo_digests = f"{VALID_IMAGE_BASE}@{NEW_CANDIDATE_DIGEST}"
        result = resolve_built_artifact(
            image_base=VALID_IMAGE_BASE,
            tag="da1bd5e",
            full_sha="da1bd5e278696d54cf8a4746ecf7e1b5fafe24f9",
            repo_digests_text=repo_digests,
        )
        assert result["expected_digest"] == NEW_CANDIDATE_DIGEST
        assert result["mode"] == "build_and_deploy"
        assert result["expected_image"] == f"{VALID_IMAGE_BASE}:da1bd5e"
        assert result["immutable_reference"] == f"{VALID_IMAGE_BASE}@{NEW_CANDIDATE_DIGEST}"

    def test_resolve_built_artifact_fallback_to_registry(self):
        registry_fn = lambda img: {"image_summary": {"digest": NEW_CANDIDATE_DIGEST}}
        result = resolve_built_artifact(
            image_base=VALID_IMAGE_BASE,
            tag="da1bd5e",
            full_sha="da1bd5e278696d54cf8a4746ecf7e1b5fafe24f9",
            repo_digests_text="",
            registry_query_fn=registry_fn,
        )
        assert result["expected_digest"] == NEW_CANDIDATE_DIGEST
        assert result["expected_image"] == f"{VALID_IMAGE_BASE}:da1bd5e"

    def test_resolve_built_artifact_fails_closed_when_digest_missing(self):
        with pytest.raises(ValueError, match="Could not resolve immutable image digest"):
            resolve_built_artifact(
                image_base=VALID_IMAGE_BASE,
                tag="da1bd5e",
                full_sha="da1bd5e278696d54cf8a4746ecf7e1b5fafe24f9",
                repo_digests_text="",
                registry_query_fn=lambda img: {},
            )

    def test_resolve_built_artifact_rejects_empty_inputs(self):
        with pytest.raises(ValueError, match="image_base must be a non-empty string"):
            resolve_built_artifact("", "tag", "sha")
        with pytest.raises(ValueError, match="tag must be a non-empty string"):
            resolve_built_artifact(VALID_IMAGE_BASE, "", "sha")
        with pytest.raises(ValueError, match="full_sha must be a non-empty string"):
            resolve_built_artifact(VALID_IMAGE_BASE, "tag", "")


class TestResolveCandidateArtifact:
    """Test promote_only_candidate resolution path."""

    def test_resolve_candidate_artifact_with_valid_spec_and_status(self):
        candidate_data = {
            "metadata": {"name": "dripp-leads-api-00021-test"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "image": f"{VALID_IMAGE_BASE}:da1bd5e"
                        }]
                    }
                }
            },
            "status": {
                "imageDigest": f"{VALID_IMAGE_BASE}@{NEW_CANDIDATE_DIGEST}"
            }
        }
        result = resolve_candidate_artifact(
            revision_data=candidate_data,
            authorized_image_base=VALID_IMAGE_BASE,
        )
        assert result["candidate_revision"] == "dripp-leads-api-00021-test"
        assert result["expected_digest"] == NEW_CANDIDATE_DIGEST
        assert result["expected_image"] == f"{VALID_IMAGE_BASE}:da1bd5e"
        assert result["immutable_reference"] == f"{VALID_IMAGE_BASE}@{NEW_CANDIDATE_DIGEST}"

    def test_resolve_candidate_artifact_authenticated_against_registry(self):
        candidate_data = {
            "metadata": {"name": "dripp-leads-api-00021-test"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "image": f"{VALID_IMAGE_BASE}:da1bd5e"
                        }]
                    }
                }
            },
            "status": {
                "imageDigest": NEW_CANDIDATE_DIGEST
            }
        }
        registry_fn = lambda img: {"image_summary": {"digest": NEW_CANDIDATE_DIGEST}}
        result = resolve_candidate_artifact(
            revision_data=candidate_data,
            authorized_image_base=VALID_IMAGE_BASE,
            registry_query_fn=registry_fn,
        )
        assert result["expected_digest"] == NEW_CANDIDATE_DIGEST

    def test_resolve_candidate_fails_when_registry_digest_mismatches(self):
        candidate_data = {
            "metadata": {"name": "dripp-leads-api-00021-test"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "image": f"{VALID_IMAGE_BASE}:da1bd5e"
                        }]
                    }
                }
            },
            "status": {
                "imageDigest": NEW_CANDIDATE_DIGEST
            }
        }
        # Registry returns different digest
        registry_fn = lambda img: {"image_summary": {"digest": SAMPLE_VALID_DIGEST}}
        with pytest.raises(ValueError, match="does not match Artifact Registry digest"):
            resolve_candidate_artifact(
                revision_data=candidate_data,
                authorized_image_base=VALID_IMAGE_BASE,
                registry_query_fn=registry_fn,
            )

    def test_resolve_candidate_fails_on_unauthorized_image(self):
        candidate_data = {
            "metadata": {"name": "dripp-leads-api-00021-test"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "image": "docker.io/malicious/img:latest"
                        }]
                    }
                }
            },
            "status": {
                "imageDigest": NEW_CANDIDATE_DIGEST
            }
        }
        with pytest.raises(ValueError, match="does not point to authorized image base"):
            resolve_candidate_artifact(
                revision_data=candidate_data,
                authorized_image_base=VALID_IMAGE_BASE,
            )

    def test_resolve_candidate_fails_on_missing_containers(self):
        candidate_data = {
            "metadata": {"name": "dripp-leads-api-00021-test"},
            "spec": {"template": {"spec": {}}},
        }
        with pytest.raises(ValueError, match="has no configured containers"):
            resolve_candidate_artifact(
                revision_data=candidate_data,
                authorized_image_base=VALID_IMAGE_BASE,
            )


class TestVerifyCandidateRevisionDigest:
    """Test read-back verification of deployed Cloud Run revision against resolved digest."""

    def test_verify_passes_when_digest_matches(self):
        revision_data = {
            "metadata": {"name": "dripp-leads-api-00022-cand"},
            "status": {
                "imageDigest": NEW_CANDIDATE_DIGEST,
                "conditions": [{"type": "Ready", "status": "True"}],
            },
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "image": f"{VALID_IMAGE_BASE}:da1bd5e"
                        }]
                    }
                }
            },
        }
        errors = verify_candidate_revision_digest(
            candidate_revision_data=revision_data,
            expected_digest=NEW_CANDIDATE_DIGEST,
            expected_image=f"{VALID_IMAGE_BASE}:da1bd5e",
            authorized_image_base=VALID_IMAGE_BASE,
        )
        assert errors == []

    def test_verify_fails_closed_when_digest_mismatches_stale(self):
        # Revision deployed has new candidate digest, but expected is stale previous digest
        revision_data = {
            "metadata": {"name": "dripp-leads-api-00022-cand"},
            "status": {
                "imageDigest": NEW_CANDIDATE_DIGEST,
            },
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "image": f"{VALID_IMAGE_BASE}:da1bd5e"
                        }]
                    }
                }
            },
        }
        errors = verify_candidate_revision_digest(
            candidate_revision_data=revision_data,
            expected_digest=STALE_PREVIOUS_DIGEST,
        )
        assert len(errors) > 0
        assert any("Candidate image digest mismatch" in err for err in errors)

    def test_verify_fails_closed_when_digest_missing_from_revision(self):
        revision_data = {
            "metadata": {"name": "dripp-leads-api-00022-cand"},
            "spec": {"template": {"spec": {"containers": [{"image": f"{VALID_IMAGE_BASE}:da1bd5e"}]}}},
            "status": {},
        }
        errors = verify_candidate_revision_digest(
            candidate_revision_data=revision_data,
            expected_digest=NEW_CANDIDATE_DIGEST,
        )
        assert len(errors) > 0
        assert any("did not report status.imageDigest" in err for err in errors)

    def test_verify_fails_when_expected_digest_invalid(self):
        revision_data = {
            "metadata": {"name": "dripp-leads-api-00022-cand"},
            "status": {"imageDigest": NEW_CANDIDATE_DIGEST},
        }
        errors = verify_candidate_revision_digest(
            candidate_revision_data=revision_data,
            expected_digest="invalid-digest-format",
        )
        assert len(errors) > 0
        assert any("invalid sha256 format" in err for err in errors)


class TestWorkflowFileSafeguards:
    """Inspect .github/workflows/05-deploy-cloud-run.yml to ensure safeguards are enforced."""

    @pytest.fixture
    def workflow_content(self):
        wf_path = Path(__file__).parent / ".github" / "workflows" / "05-deploy-cloud-run.yml"
        assert wf_path.exists(), f"Workflow file not found at {wf_path}"
        return wf_path.read_text()

    @pytest.fixture
    def workflow_yaml(self, workflow_content):
        return yaml.safe_load(workflow_content)

    def test_promote_traffic_defaults_to_false(self, workflow_yaml):
        """CRITICAL: promote_traffic must default to false so operators must explicitly opt-in."""
        inputs = workflow_yaml["on"]["workflow_dispatch"]["inputs"]
        promote_input = inputs.get("promote_traffic")
        assert promote_input is not None, "promote_traffic input missing from workflow"
        default_val = str(promote_input.get("default")).lower()
        assert default_val == "false", f"promote_traffic default must be 'false', found '{default_val}'"

    def test_no_hardcoded_stale_digest_in_workflow(self, workflow_content):
        """CRITICAL: Ensure the stale hard-coded digest is completely removed from workflow."""
        stale_fragment = "sha256:991bb03c169edd467eb3c4c8548f6907f5d749d5ca69f6a800541d173812cc89"
        assert stale_fragment not in workflow_content, "Found hardcoded stale digest in workflow file!"

    def test_no_hardcoded_stale_commit_sha_in_deploy_tag(self, workflow_content):
        """CRITICAL: Ensure hard-coded candidate tag 123ee3b... is removed."""
        stale_tag = "123ee3bbce9b1207a50a7a6c8828cb24014c5ce2"
        assert stale_tag not in workflow_content, f"Found hardcoded stale tag {stale_tag} in workflow file!"

    def test_artifact_resolution_script_is_invoked(self, workflow_content):
        """Ensure resolve_deployment_artifact.py is wired for built and candidate resolution."""
        assert "resolve_deployment_artifact.py resolve-built" in workflow_content
        assert "resolve_deployment_artifact.py resolve-candidate" in workflow_content
        assert "resolve_deployment_artifact.py verify-candidate-digest" in workflow_content

    def test_traffic_promotion_step_requires_explicit_opt_in(self, workflow_content):
        """Ensure traffic promotion step requires promote_traffic == 'true'."""
        assert "github.event.inputs.promote_traffic == 'true'" in workflow_content

    def test_zero_traffic_completion_step_present(self, workflow_content):
        """Ensure clear feedback when deployment completes with 0% traffic."""
        assert "Report Candidate Ready (Zero-Traffic Deployment Complete)" in workflow_content
