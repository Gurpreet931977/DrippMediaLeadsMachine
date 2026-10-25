"""
test_phase_12_3_data_provenance_regression.py
=============================================
Regression tests verifying:
1. Separation of original execution records and derived candidate artifacts.
2. Handling of missing derived artifacts, malformed JSON, and unknown run IDs.
3. Accurate provenance metadata and flagging of synthetic/reconstructed IDs.
4. Preservation of null/unverified fields (no invented phone or address).
5. Authentication and authorization enforcement on candidate endpoints.
6. Latest completed run resolution across multiple historical run records.
7. Zero mutation to CRM or Sheets during candidate inspection.
"""

import os
import json
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

import server
from lib.system.pipeline_run_manager import PipelineRunManager, run_manager
from lib.sheets.google_sheets import GoogleSheetsStorageProvider


@pytest.fixture(scope="module")
def auth_client():
    c = TestClient(server.app)
    op_key = server._configured_operator_key()
    if op_key:
        sess = server.create_operator_session()
        c.headers.update({
            "Authorization": f"Bearer {sess['session_token']}",
            "X-Requested-With": "XMLHttpRequest",
        })
    return c


@pytest.fixture(scope="module")
def unauth_client():
    return TestClient(server.app)


def test_unknown_run_id_returns_404(auth_client):
    """1. Unknown run ID returns 404 with structured error response."""
    resp = auth_client.get("/api/runs/RUN-UNKNOWN-999999-NOTFOUND/candidates")
    assert resp.status_code == 404
    data = resp.json()
    assert data.get("status") == "error"
    assert "not found" in data.get("message", "").lower()
    assert data.get("candidates") == []
    assert data.get("count") == 0


def test_unauthenticated_candidate_requests_rejected(unauth_client):
    """2. Non-public candidate endpoints require operator authentication."""
    op_key = server._configured_operator_key()
    if not op_key:
        pytest.skip("Operator authentication not configured in this test environment.")

    # 1. Latest candidates requires auth
    res_latest = unauth_client.get("/api/runs/latest/candidates")
    assert res_latest.status_code in (401, 403)

    # 2. Specific run candidates requires auth
    res_specific = unauth_client.get("/api/runs/RUN-20261025_071312-C9AB1F/candidates")
    assert res_specific.status_code in (401, 403)

    # 3. Invalid token rejected
    bad_client = TestClient(server.app)
    bad_client.headers.update({"Authorization": "Bearer INVALID_TOKEN_12345"})
    res_bad = bad_client.get("/api/runs/latest/candidates")
    assert res_bad.status_code == 401


def test_derived_candidate_artifact_integrity_and_provenance(auth_client):
    """3. Derived candidates must expose honest provenance metadata."""
    res = auth_client.get("/api/runs/RUN-20261025_071312-C9AB1F/candidates")
    assert res.status_code == 200
    data = res.json()
    assert data.get("status") == "ok"
    assert data.get("run_id") == "RUN-20261025_071312-C9AB1F"

    # Verify run-level candidate provenance
    prov = data.get("candidate_provenance", {})
    assert prov.get("provenance_status") == "RECONSTRUCTED_FROM_RUN_LOGS"
    assert prov.get("is_authoritative_original_pipeline_output") is False
    assert "reconstructed" in prov.get("audit_summary", "").lower()

    candidates = data.get("candidates", [])
    assert len(candidates) == 2

    # Check each candidate exposes data provenance
    for cand in candidates:
        assert cand.get("is_dry_run") is True
        assert cand.get("is_shadow_mode") is True
        cprov = cand.get("data_provenance", {})
        assert cprov.get("status") == "RECONSTRUCTED_FROM_RUN_LOGS"
        assert cprov.get("is_authoritative_original_pipeline_output") is False
        assert "lead_id" in cprov.get("reconstructed_or_placeholder_fields", {})


def test_synthetic_candidate_id_flagged_not_persisted_lead_id(auth_client):
    """4. Synthetic candidate IDs are identified as inspection keys, not authoritative CRM IDs."""
    res = auth_client.get("/api/runs/RUN-20261025_071312-C9AB1F/candidates")
    assert res.status_code == 200
    candidates = res.json().get("candidates", [])

    wendover = next(c for c in candidates if "wendover" in c["company_name"].lower())
    assert wendover["id"].startswith("CAND-")
    assert "lead_id" in wendover["data_provenance"]["reconstructed_or_placeholder_fields"]

    dog = next(c for c in candidates if "dog and partridge" in c["company_name"].lower())
    assert dog["id"].startswith("CAND-")
    assert "lead_id" in dog["data_provenance"]["reconstructed_or_placeholder_fields"]


def test_missing_values_remain_null(auth_client):
    """5. Fields not discovered or logged in dry runs remain null, not invented."""
    res = auth_client.get("/api/runs/RUN-20261025_071312-C9AB1F/candidates")
    assert res.status_code == 200
    candidates = res.json().get("candidates", [])

    wendover = next(c for c in candidates if "wendover" in c["company_name"].lower())
    assert wendover.get("phone") is None
    assert wendover.get("postcode") is None
    assert wendover.get("review_date") is None


def test_missing_derived_artifact_graceful_fallback(tmp_path):
    """6. Missing derived artifact returns empty candidates without crashing."""
    mgr = PipelineRunManager()
    mgr.runs_dir = str(tmp_path)
    run_file = tmp_path / "RUN-TEST-001.json"
    run_file.write_text(json.dumps({"run_id": "RUN-TEST-001", "status": "COMPLETED"}))

    candidates = mgr.get_run_candidates("RUN-TEST-001")
    assert candidates == []
    prov = mgr.get_run_candidate_provenance("RUN-TEST-001")
    assert prov is None


def test_malformed_derived_json_graceful_fallback(tmp_path):
    """7. Malformed derived JSON file is safely caught and returns empty list."""
    mgr = PipelineRunManager()
    mgr.runs_dir = str(tmp_path)
    run_file = tmp_path / "RUN-TEST-002.json"
    run_file.write_text(json.dumps({"run_id": "RUN-TEST-002", "status": "COMPLETED"}))

    derived_dir = tmp_path / "derived"
    derived_dir.mkdir()
    corrupt_file = derived_dir / "RUN-TEST-002.candidates.json"
    corrupt_file.write_text("{CORRUPTED_JSON_NOT_VALID")

    candidates = mgr.get_run_candidates("RUN-TEST-002")
    assert candidates == []
    prov = mgr.get_run_candidate_provenance("RUN-TEST-002")
    assert prov is None


def test_latest_run_resolution_prefers_completed_with_telemetry(tmp_path):
    """8. Latest completed run resolution correctly prefers run with actual telemetry over empty/cancelled 0s runs."""
    mgr = PipelineRunManager()
    mgr.runs_dir = str(tmp_path)

    run1 = {
        "run_id": "RUN-20261025_010000-AAAAAA",
        "timestamp": "2026-10-25T01:00:00Z",
        "status": "COMPLETED",
        "results": {"candidates_discovered": 40, "candidates_researched": 2}
    }
    run2 = {
        "run_id": "RUN-20261025_020000-BBBBBB",
        "timestamp": "2026-10-25T02:00:00Z",
        "status": "CANCELLED",
        "results": {"candidates_discovered": 0, "candidates_researched": 0}
    }

    (tmp_path / "RUN-20261025_010000-AAAAAA.json").write_text(json.dumps(run1))
    (tmp_path / "RUN-20261025_020000-BBBBBB.json").write_text(json.dumps(run2))
    (tmp_path / "latest_run.json").write_text(json.dumps(run2))

    best = mgr.get_latest_completed_run()
    assert best is not None
    assert best["run_id"] == "RUN-20261025_010000-AAAAAA"
    assert best["results"]["candidates_discovered"] == 40


def test_candidate_inspection_never_mutates_crm():
    """9. Candidate inspection read path never mutates Google Sheets or persistent CRM."""
    storage = GoogleSheetsStorageProvider()
    leads_before = storage.fetch_all_leads()
    count_before = len(leads_before)

    mgr = PipelineRunManager()
    candidates = mgr.get_run_candidates("RUN-20261025_071312-C9AB1F")
    assert len(candidates) == 2

    leads_after = storage.fetch_all_leads()
    assert len(leads_after) == count_before
    assert count_before == 7
