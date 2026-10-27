"""Unit tests for local execution safeguards, run classification, and gateway configuration."""

import json
import os
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

import server
from lib.system.pipeline_run_manager import (
    DurablePipelineRunManager,
    PipelineRunStatus,
)
from lib.system.cloud_run_job_dispatcher import CloudRunJobDispatcher


class TestRunClassification:
    """Test transparent distinction between completed, failed, incomplete, and simulated runs."""

    def test_classify_live_runs(self):
        # Completed
        assert DurablePipelineRunManager.classify_run({
            "status": PipelineRunStatus.COMPLETED.value, "dry_run": False
        }) == PipelineRunStatus.COMPLETED.value
        # Completed with errors
        assert DurablePipelineRunManager.classify_run({
            "status": PipelineRunStatus.COMPLETED_WITH_ERRORS.value, "dry_run": False
        }) == PipelineRunStatus.COMPLETED_WITH_ERRORS.value
        # Incomplete (Queued / Running)
        assert DurablePipelineRunManager.classify_run({
            "status": PipelineRunStatus.QUEUED.value, "dry_run": False
        }) == "INCOMPLETE"
        assert DurablePipelineRunManager.classify_run({
            "status": PipelineRunStatus.RUNNING.value, "dry_run": False
        }) == "INCOMPLETE"
        # Failed
        assert DurablePipelineRunManager.classify_run({
            "status": PipelineRunStatus.FAILED.value, "dry_run": False
        }) == "FAILED"
        # Cancelled
        assert DurablePipelineRunManager.classify_run({
            "status": PipelineRunStatus.CANCELLED.value, "dry_run": False
        }) == "CANCELLED"

    def test_classify_simulated_runs(self):
        # Simulated completed
        assert DurablePipelineRunManager.classify_run({
            "status": PipelineRunStatus.COMPLETED.value, "dry_run": True
        }) == "SIMULATED_COMPLETED"
        assert DurablePipelineRunManager.classify_run({
            "status": PipelineRunStatus.COMPLETED_WITH_ERRORS.value, "dry_run": True
        }) == "SIMULATED_COMPLETED"
        # Simulated incomplete
        assert DurablePipelineRunManager.classify_run({
            "status": PipelineRunStatus.QUEUED.value, "dry_run": True
        }) == "SIMULATED_INCOMPLETE"
        assert DurablePipelineRunManager.classify_run({
            "status": PipelineRunStatus.RUNNING.value, "dry_run": True
        }) == "SIMULATED_INCOMPLETE"
        # Simulated failed
        assert DurablePipelineRunManager.classify_run({
            "status": PipelineRunStatus.FAILED.value, "dry_run": True
        }) == "SIMULATED_FAILED"
        # Simulated cancelled
        assert DurablePipelineRunManager.classify_run({
            "status": PipelineRunStatus.CANCELLED.value, "dry_run": True
        }) == "SIMULATED_CANCELLED"

    def test_enrich_run_record_classification(self):
        record = {
            "run_id": "RUN-TEST-001",
            "status": PipelineRunStatus.COMPLETED.value,
            "dry_run": True,
        }
        enriched = DurablePipelineRunManager.enrich_run_record_classification(record)
        assert enriched["run_classification"] == "SIMULATED_COMPLETED"
        assert enriched["is_simulated"] is True
        assert enriched["is_completed"] is True
        assert enriched["is_incomplete"] is False
        assert enriched["is_failed"] is False
        assert enriched["is_cancelled"] is False


class TestRecoveryAndCRJPrefix:
    """Test server process restart recovery with both RUN- and CRJ-RUN- prefixes."""

    def test_recover_interrupted_runs_in_temp_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = DurablePipelineRunManager(runs_dir=tmpdir)
            
            # Create an interrupted RUN- record
            run1 = {
                "run_id": "RUN-20261027_010000-AAAAAA",
                "status": PipelineRunStatus.RUNNING.value,
                "is_running": True,
                "dry_run": True,
                "progress_logs": [],
                "results": {"final_status": "RUNNING"}
            }
            with open(os.path.join(tmpdir, "RUN-20261027_010000-AAAAAA.json"), "w") as f:
                json.dump(run1, f)

            # Create an interrupted CRJ-RUN- record
            run2 = {
                "run_id": "CRJ-RUN-20261027_020000-BBBBBB",
                "status": PipelineRunStatus.QUEUED.value,
                "is_running": False,
                "dry_run": False,
                "progress_logs": [],
                "results": {"final_status": "QUEUED"}
            }
            with open(os.path.join(tmpdir, "CRJ-RUN-20261027_020000-BBBBBB.json"), "w") as f:
                json.dump(run2, f)

            # Create an already completed run (should not be touched)
            run3 = {
                "run_id": "RUN-20261027_030000-CCCCCC",
                "status": PipelineRunStatus.COMPLETED.value,
                "is_running": False,
                "dry_run": False,
                "progress_logs": [],
                "results": {"final_status": "COMPLETED"}
            }
            with open(os.path.join(tmpdir, "RUN-20261027_030000-CCCCCC.json"), "w") as f:
                json.dump(run3, f)

            recovered = mgr.recover_interrupted_runs()
            assert recovered == 2

            # Check run1
            with open(os.path.join(tmpdir, "RUN-20261027_010000-AAAAAA.json")) as f:
                rec1 = json.load(f)
            assert rec1["status"] == PipelineRunStatus.FAILED.value
            assert rec1["is_running"] is False
            assert "interrupted by server process restart" in rec1["last_error"]
            assert rec1["is_failed"] is True

            # Check run2
            with open(os.path.join(tmpdir, "CRJ-RUN-20261027_020000-BBBBBB.json")) as f:
                rec2 = json.load(f)
            assert rec2["status"] == PipelineRunStatus.FAILED.value
            assert rec2["is_failed"] is True

            # Check run3 remains completed
            with open(os.path.join(tmpdir, "RUN-20261027_030000-CCCCCC.json")) as f:
                rec3 = json.load(f)
            assert rec3["status"] == PipelineRunStatus.COMPLETED.value


class TestLocalExecutionSafeguards:
    """Verify local execution never triggers remote Cloud Run jobs."""

    def test_dispatcher_not_configured_locally_by_default(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("FORCE_CLOUD_RUN_JOB", raising=False)
        
        dispatcher = CloudRunJobDispatcher()
        assert dispatcher.is_cloud_run_configured() is False

        res = dispatcher.dispatch_discovery_job(
            country="United Kingdom",
            cities=["Manchester"],
            industry="Restaurants",
            leads=5,
            dry_run=True,
        )
        assert res["dispatched"] is False
        assert res["execution_type"] == "IN_PROCESS"

    def test_local_only_flag_blocks_remote_dispatch(self, monkeypatch):
        monkeypatch.setenv("LOCAL_ONLY", "true")
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("K_SERVICE", "dripp-leads-api")
        
        dispatcher = CloudRunJobDispatcher()
        assert dispatcher.is_cloud_run_configured() is False


class TestFrontendLocalGatewayConfig:
    """Verify index.html contains local gateway resolution logic."""

    def test_index_html_handles_local_origins(self):
        html_path = Path(__file__).parent / "static" / "index.html"
        assert html_path.exists()
        content = html_path.read_text(encoding="utf-8")

        # Verify localhost and 127.0.0.1 handling
        assert "127.0.0.1" in content
        assert "localhost" in content
        # Verify clearPermanentOperatorSecrets exists to prevent key leak
        assert "clearPermanentOperatorSecrets" in content


class TestLocalAPIServerSmoke:
    """Test that local FastAPI server endpoints function correctly."""

    @pytest.fixture(scope="class")
    def client(self):
        return TestClient(server.app)

    def test_public_health_endpoint(self, client):
        resp = client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"
        assert data.get("canonical_health_status") == "OPERATIONAL"

    def test_discovery_status_endpoint_requires_auth(self, client):
        resp = client.get("/api/discovery/status")
        assert resp.status_code == 401

    def test_discovery_status_endpoint_with_auth(self, client):
        sess = server.create_operator_session()
        auth_client = TestClient(server.app)
        auth_client.headers.update({
            "Authorization": f"Bearer {sess['session_token']}",
            "X-Requested-With": "XMLHttpRequest",
        })
        resp = auth_client.get("/api/discovery/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data

    def test_root_serves_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Dripp Media" in resp.text
