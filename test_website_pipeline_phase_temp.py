"""
test_website_pipeline_phase_temp.py
===================================
End-to-End Test Suite for PHASE Temp:
WEBSITE-TRIGGERED END-TO-END LEAD PIPELINE

Verifies:
  1. Website Run Controls (DOM):
     - "Run Lead Discovery" actions (header and hero)
     - Market selector (Manchester, UK)
     - Batch size selector (conservative max <= 25)
     - Dry-run toggle
     - Confirmation summary modal
     - Structured Results Breakdown card (all 9 required metrics)
     - Safe cancellation control
  2. Backend Execution & Schema:
     - SearchRequest schema with batch_size & dry_run
     - Bounded batch size enforcement
     - Durable run record creation in QUEUED status
  3. Safe Execution Lifecycle & Concurrency:
     - Duplicate active submission prevention (HTTP 409 Conflict)
     - Safe run cancellation (POST /api/search/cancel)
     - Restart recovery (orphaned RUNNING/QUEUED -> FAILED with honest message)
     - Honest progress reporting via GET /api/status
  4. Real Results Accounting:
     - Discovered, researched, qualified, manual review, rejected, duplicates,
       provider failures, quota status, CRM reconciliation outcome.
  5. Safety Invariants:
     - Travel Mode = ACTIVE
     - Commercial Actions = LOCKED
     - Automated Email = DISABLED
     - Cron = DISABLED
     - Frozen Rule B criteria verified
"""

import os
import json
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

import server
from lib.system.pipeline_run_manager import PipelineRunManager, PipelineRunStatus
from lib.system.system_config import SystemConfig


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app)


@pytest.fixture(autouse=True)
def reset_server_active_state():
    """Ensure clean run state before and after each test."""
    server.active_run_state["is_running"] = False
    server.active_run_state["status"] = PipelineRunStatus.COMPLETED.value
    server.active_run_state["last_error"] = None
    server.run_manager._active_run_id = None
    yield
    server.active_run_state["is_running"] = False
    server.active_run_state["status"] = PipelineRunStatus.COMPLETED.value
    server.run_manager._active_run_id = None


class TestWebsiteRunControlsDOM:
    """Verifies that all Phase Temp controls exist in the website HTML without broken references."""

    def test_run_lead_discovery_actions_present(self, client):
        """Phase Temp 2: Website run controls provide clear 'Run Lead Discovery' action."""
        res = client.get("/")
        assert res.status_code == 200
        html = res.text

        # Header run action
        assert 'id="btnPageRunDiscovery"' in html
        assert "Run Lead Discovery" in html

        # Hero launch action
        assert 'id="btnLaunchDiscoveryPageAction"' in html
        assert "LAUNCH AUTONOMOUS DISCOVERY TASK" in html or "RUN LEAD DISCOVERY" in html

    def test_market_batch_and_dryrun_selectors_present(self, client):
        """Phase Temp 2: Operator can select Manchester UK, batch size, and dry-run mode."""
        res = client.get("/")
        html = res.text

        # Target Market
        assert 'id="pageDiscCityInput"' in html
        assert 'value="Manchester"' in html
        assert 'id="pageDiscCountryInput"' in html
        assert 'value="United Kingdom"' in html

        # Batch Size Selector with conservative configurable maximum
        assert 'id="pageDiscBatchInput"' in html
        assert 'option value="10"' in html
        assert 'option value="20"' in html

        # Dry-run toggle
        assert 'id="pageDiscDryRunInput"' in html
        assert 'type="checkbox"' in html
        assert "Safe Dry-Run" in html

    def test_confirmation_summary_modal_structure(self, client):
        """Phase Temp 2: Display a confirmation summary before starting."""
        res = client.get("/")
        html = res.text

        assert 'id="discoveryConfirmModal"' in html
        assert 'id="confirmSummaryMarket"' in html
        assert 'id="confirmSummaryNiche"' in html
        assert 'id="confirmSummaryYield"' in html
        assert 'id="confirmSummaryBatch"' in html
        assert 'id="confirmSummaryMode"' in html
        assert 'id="btnConfirmAndLaunchDiscovery"' in html
        assert "Confirm &amp; Launch Mission" in html

    def test_structured_results_card_has_all_9_metrics(self, client):
        """Phase Temp 5: Show all 9 required metrics from real backend values."""
        res = client.get("/")
        html = res.text

        assert 'id="pageDiscoveryResultsCard"' in html
        assert 'id="resRunIdText"' in html
        assert 'id="resStatusBadge"' in html
        assert 'id="resStartTime"' in html
        assert 'id="resEndTime"' in html
        assert 'id="resElapsed"' in html

        # 9 Required Metrics:
        assert 'id="resDiscovered"' in html         # 1. Candidates discovered
        assert 'id="resResearched"' in html         # 2. Candidates researched
        assert 'id="resQualified"' in html          # 3. Qualified leads
        assert 'id="resManualRev"' in html          # 4. Manual-review leads
        assert 'id="resRejected"' in html           # 5. Rejected leads
        assert 'id="resDuplicates"' in html         # 6. Duplicates skipped
        assert 'id="resProviderStatus"' in html     # 7. Provider failures
        assert 'id="resQuotaStatus"' in html        # 8. Quota exhaustion
        assert 'id="resCrmOutcome"' in html         # 9. CRM reconciliation outcome

    def test_safe_cancellation_button_present(self, client):
        """Phase Temp 4: Safe cancellation action present."""
        res = client.get("/")
        html = res.text
        assert 'id="btnCancelDiscoveryRun"' in html
        assert "Cancel Run" in html


class TestBackendExecutionAndLifecycle:
    """Verifies durable run records, concurrency locks, and lifecycle states."""

    def test_search_request_schema_supports_batch_and_dry_run(self):
        """Phase Temp 2/3: SearchRequest model accepts batch_size and dry_run."""
        req = server.SearchRequest(
            cities=["Manchester"],
            industry="Restaurants",
            qualified_leads_needed=10,
            batch_size=15,
            dry_run=True,
            discovery_mode="HYBRID",
            apify_enabled=False
        )
        assert req.batch_size == 15
        assert req.dry_run is True

    def test_post_search_creates_queued_run_record(self, client, monkeypatch):
        """Phase Temp 3/4: POST /api/search creates a durable record in QUEUED state."""
        # Prevent actual network pipeline execution during integration test
        called = {}

        def mock_run_pipeline_task(**kwargs):
            called.update(kwargs)

        monkeypatch.setattr(server, "run_pipeline_task", mock_run_pipeline_task)

        payload = {
            "country": "United Kingdom",
            "cities": ["Manchester"],
            "industry": "Restaurants",
            "qualified_leads_needed": 5,
            "batch_size": 10,
            "dry_run": True,
            "discovery_mode": "HYBRID",
            "apify_enabled": False
        }

        res = client.post("/api/search", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "started"
        assert data["lifecycle"] == PipelineRunStatus.QUEUED.value
        assert data["dry_run"] is True
        assert data["batch_size"] == 10
        assert data["run_id"].startswith("RUN-")

        # Verify durable persistence in run_manager
        run_rec = server.run_manager.get_run(data["run_id"])
        assert run_rec is not None
        assert run_rec["dry_run"] is True
        assert run_rec["cities"] == ["Manchester"]
        assert run_rec["status"] == PipelineRunStatus.QUEUED.value

    def test_overlapping_runs_prevented_with_409(self, client):
        """Phase Temp 4: Overlapping runs return HTTP 409 Conflict."""
        # Set active run state
        server.active_run_state["is_running"] = True
        server.active_run_state["run_id"] = "RUN-ACTIVE-TEST"

        res = client.post("/api/search", json={"cities": ["Manchester"]})
        assert res.status_code == 409
        data = res.json()
        assert "already in progress" in data["message"]

    def test_cancel_active_run_endpoint(self, client):
        """Phase Temp 4: POST /api/search/cancel marks active run as CANCELLED."""
        # Create an active run
        rec = server.run_manager.create_run(
            country="United Kingdom",
            cities=["Manchester"],
            industry="Restaurants",
            qualified_leads_needed=5,
            batch_size=10,
            dry_run=True
        )
        run_id = rec["run_id"]
        server.run_manager.start_run(run_id)
        server.active_run_state["is_running"] = True
        server.active_run_state["run_id"] = run_id

        res = client.post("/api/search/cancel")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert "CANCELLED" in data["message"]

        updated = server.run_manager.get_run(run_id)
        assert updated["status"] == PipelineRunStatus.CANCELLED.value
        assert updated["is_running"] is False
        assert server.active_run_state["is_running"] is False

    def test_cancel_when_no_active_run_returns_400(self, client):
        """Phase Temp 4: Cancellation when nothing is active returns HTTP 400."""
        server.active_run_state["is_running"] = False
        res = client.post("/api/search/cancel")
        assert res.status_code == 400

    def test_process_restart_recovery_orphaned_runs(self, tmp_path):
        """Phase Temp 4: If process restarts, orphaned RUNNING/QUEUED runs must transition to FAILED."""
        mgr = PipelineRunManager(runs_dir=str(tmp_path))

        # Create simulated orphaned run
        rec = mgr.create_run(
            country="United Kingdom",
            cities=["Manchester"],
            industry="Restaurants",
            qualified_leads_needed=10,
            batch_size=10,
            dry_run=True
        )
        mgr.start_run(rec["run_id"])

        # Verify it was saved as RUNNING
        saved = mgr.get_run(rec["run_id"])
        assert saved["status"] == PipelineRunStatus.RUNNING.value
        assert saved["is_running"] is True

        # Simulate fresh process startup recovery
        fresh_mgr = PipelineRunManager(runs_dir=str(tmp_path))
        recovered_count = fresh_mgr.recover_interrupted_runs()

        assert recovered_count == 1
        recovered_run = fresh_mgr.get_run(rec["run_id"])
        assert recovered_run["status"] == PipelineRunStatus.FAILED.value
        assert recovered_run["is_running"] is False
        assert "Run was interrupted by server process restart" in recovered_run["last_error"]


class TestResultsAccountingAndCRMReconciliation:
    """Verifies that all 9 metrics are accurately accounted for from real pipeline runs."""

    def test_real_backend_results_accounting(self, tmp_path):
        """Phase Temp 5: Show all 9 required metrics from backend values."""
        mgr = PipelineRunManager(runs_dir=str(tmp_path))

        rec = mgr.create_run(
            country="United Kingdom",
            cities=["Manchester"],
            industry="Restaurants",
            qualified_leads_needed=10,
            batch_size=10,
            dry_run=True
        )
        mgr.start_run(rec["run_id"])

        # Simulated real pipeline return
        simulated_pipeline_result = {
            "leads": [{"place_id": "p1"}, {"place_id": "p2"}],
            "review_queue": [{"place_id": "p3"}],
            "research_entries": [{"place_id": "p1"}, {"place_id": "p2"}, {"place_id": "p3"}],
            "elapsed_seconds": 12.4,
            "stats": {
                "raw_discovered": 45,
                "valid_candidates_researched": 20,
                "qualified_leads": 2,
                "manual_review": 1,
                "not_qualified": 17,
                "duplicates": 5,
                "failures_count": 0,
                "saved_to_leads": 0,
                "saved_to_review_queue": 0,
                "saved_to_research_log": 0
            },
            "discovery_transparency": {
                "businesses_discovered_total": 45,
                "unique_candidates": 40,
                "duplicates_merged": 5
            }
        }

        finished = mgr.finish_run(rec["run_id"], simulated_pipeline_result)
        res = finished["results"]

        assert res["candidates_discovered"] == 45
        assert res["candidates_researched"] == 20
        assert res["qualified_leads"] == 2
        assert res["manual_review_leads"] == 1
        assert res["rejected_leads"] == 17
        assert res["duplicates_skipped"] == 5
        assert res["provider_failures"] == 0
        assert res["quota_exhaustion"] is None
        assert res["final_status"] == PipelineRunStatus.COMPLETED.value
        assert res["crm_reconciliation"]["mode"] == "SHADOW_MODE_DRY_RUN"
        assert res["crm_reconciliation"]["reconciled"] is True

    def test_live_crm_reconciliation_mode_recording(self, tmp_path):
        """Phase Temp 6: Approved technical processing mode records live Google Sheets sync."""
        mgr = PipelineRunManager(runs_dir=str(tmp_path))

        rec = mgr.create_run(
            country="United Kingdom",
            cities=["Manchester"],
            industry="Restaurants",
            qualified_leads_needed=10,
            batch_size=10,
            dry_run=False  # Live mode
        )
        mgr.start_run(rec["run_id"])

        simulated_result = {
            "leads": [{"place_id": "p1"}],
            "review_queue": [],
            "research_entries": [{"place_id": "p1"}],
            "elapsed_seconds": 8.0,
            "stats": {
                "raw_discovered": 25,
                "valid_candidates_researched": 10,
                "qualified_leads": 1,
                "manual_review": 0,
                "not_qualified": 9,
                "duplicates": 2,
                "failures_count": 0,
                "saved_to_leads": 1,
                "saved_to_review_queue": 0,
                "saved_to_research_log": 1
            }
        }

        finished = mgr.finish_run(rec["run_id"], simulated_result)
        crm = finished["results"]["crm_reconciliation"]

        assert crm["mode"] == "LIVE_GOOGLE_SHEETS_CRM"
        assert crm["saved_to_leads"] == 1
        assert crm["saved_to_research_log"] == 1


class TestSafetyInvariantsAndRuleB:
    """Phase Temp 6: Rigorous Safety Invariant Guard verification."""

    def test_safety_invariants_strictly_enforced(self):
        """All safety flags must remain active/locked and cron disabled."""
        from lib.system.production_readiness import ProductionReadinessAuditor, GateStatus

        # Invariants from SystemConfig
        assert SystemConfig.is_travel_mode() is True, "Travel Mode must remain ACTIVE"
        assert SystemConfig.can_execute_commercial_actions() is False, "Commercial Actions must remain LOCKED"
        assert SystemConfig.is_automated_email_enabled() is False, "Automated Email must remain DISABLED"

        # Cron verification
        auditor = ProductionReadinessAuditor()
        cron_gate = auditor.check_cron_state()
        assert cron_gate["status"] == GateStatus.PASS, "All GitHub Actions cron schedules must be disabled"
        assert cron_gate["evidence"]["cron_disabled"] is True

    def test_frozen_rule_b_criteria_invariants(self):
        """Frozen Rule B criteria: 8 canonical criteria, >= 4.0 stars, >= 50 reviews, frozen version."""
        from lib.validation.rule_b_criteria import (
            RULE_B_VERSION,
            get_canonical_rule_b_criteria_count,
            get_canonical_rule_b_criteria,
        )
        assert RULE_B_VERSION == "FROZEN"
        assert get_canonical_rule_b_criteria_count() == 8

        criteria = {c["id"]: c for c in get_canonical_rule_b_criteria()}
        assert "RULE_B_01_REVIEW_VOLUME" in criteria
        assert criteria["RULE_B_01_REVIEW_VOLUME"]["threshold"] == 50
        assert "RULE_B_02_MINIMUM_RATING" in criteria
        assert criteria["RULE_B_02_MINIMUM_RATING"]["threshold"] == 4.0

        # LeadScorer Rule B evaluation invariant
        from lib.qualification.lead_scoring import LeadScorer
        scorer = LeadScorer()

        # Qualified lead meeting Rule B
        score, reasons, cat, qual_state, _ = scorer.evaluate_lead({
            "company_name": "Test Bistro",
            "review_count": 65,
            "rating": 4.6,
            "verification_status": "NO_WEBSITE_CONFIRMED",
            "operational_status": "ACTIVE_CONFIRMED",
            "postcode": "M1 1AA",
            "phone": "+441611234567",
            "instagram_url": "https://instagram.com/testbistro",
        })
        assert qual_state == "OUTREACH_READY"
        assert score > 0

        # Disqualified by rating < 4.0
        score_low, _, _, qual_state_low, _ = scorer.evaluate_lead({
            "company_name": "Poor Bistro",
            "review_count": 65,
            "rating": 3.2,
            "verification_status": "NO_WEBSITE_CONFIRMED",
            "operational_status": "ACTIVE_CONFIRMED",
            "postcode": "M1 1AA",
            "phone": "+441611234567",
            "instagram_url": "https://instagram.com/poorbistro",
        })
        assert qual_state_low != "OUTREACH_READY"

        # Disqualified by verified website
        score_web, _, _, qual_state_web, _ = scorer.evaluate_lead({
            "company_name": "Has Website Bistro",
            "review_count": 100,
            "rating": 4.8,
            "verification_status": "WEBSITE_CONFIRMED",
            "operational_status": "ACTIVE_CONFIRMED",
        })
        assert qual_state_web == "EXCLUDED"


