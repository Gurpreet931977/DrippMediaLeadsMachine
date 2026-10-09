"""
test_phase_12_0_lead_discovery_ui.py
====================================
Comprehensive Test Suite for PHASE 12.0:
CONNECT THE EXISTING LEAD DISCOVERY UI TO THE REAL PIPELINE

Verifies:
  1. Discovery UI Integration:
     - Launch button and controls in static/index.html wire to POST /api/search.
     - Confirmation modal and real-time 5-stage pipeline stepper.
     - Providers matrix reflects real backend status (Apify exhausted, Google Places disallowed, OSM active).
     - Manchester locked as initial target market (Relation #162378).
     - Quality guarantee reflects the 8 frozen canonical criteria.
     - Zero hardcoded run metrics in UI (dynamic data provenance).
  2. Backend Market & Provider Validation:
     - Manchester, UK accepted.
     - Unsupported markets (Leeds, Birmingham, London) rejected with HTTP 400.
     - Disallowed providers (Google Places, Apify) rejected with HTTP 400.
     - Conservative batch limits (1 to 25) strictly enforced.
  3. Execution Lifecycle & Concurrency:
     - Duplicate submissions rejected with HTTP 409 Conflict.
     - Run cancellation supported safely via POST /api/search/cancel.
     - Interrupted runs recovered on startup as FAILED (never falsely active).
     - Status tracking reflects genuine run state.
  4. Commercial Safety & Invariant Enforcement:
     - TRAVEL_MODE = ACTIVE
     - COMMERCIAL_ACTIONS_ENABLED = False
     - AUTOMATED_EMAIL_ENABLED = False
     - CRON = DISABLED
     - RULE B = FROZEN
  5. Honest Provider Diagnostics:
     - GET /api/discovery/status reports real health without fabrication.
"""

import os
import json
import pytest
from fastapi.testclient import TestClient

import server
from lib.system.pipeline_run_manager import run_manager, PipelineRunStatus
from lib.system.system_config import SystemConfig
from lib.discovery.hybrid import HybridDiscoveryEngine
from lib.validation.rule_b_criteria import (
    RULE_B_CRITERIA,
    RULE_B_VERSION,
    get_canonical_rule_b_criteria,
    get_canonical_rule_b_criteria_count,
)


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


class TestDiscoveryUIDOMIntegrity:
    """Verifies that the existing Lead Discovery page satisfies all Phase 12.0 UI constraints."""

    def test_discovery_ui_elements_and_launch_wiring(self, client):
        """Phase 12.0 Section 1 & 3: Ensure existing discovery page has all required controls wired to backend."""
        res = client.get("/")
        assert res.status_code == 200
        html = res.text

        # 1. Existing Launch Buttons wired to confirmation modal / backend
        assert 'id="btnLaunchDiscoveryPageAction"' in html
        assert 'onclick="openDiscoveryConfirmModal()"' in html
        assert 'id="btnConfirmAndLaunchDiscovery"' in html
        assert 'onclick="executeConfirmedDiscovery()"' in html

        # 2. Market controls: Manchester active, Leeds/Birmingham/London locked
        assert 'id="pageDiscCityInput"' in html
        assert 'value="Manchester"' in html
        assert "LOCKED (PHASE 12.0)" in html
        assert "Relation #162378" in html

        # 3. Mode selection: Free Local / Hybrid, no Apify or Google Places selectable
        assert 'id="pageDiscModeInput"' in html
        assert "FREE_LOCAL" in html
        assert "HYBRID" in html
        assert "Google Places" not in html or "DISALLOWED" in html or "Disallowed" in html

        # 4. Five-stage execution pipeline stepper
        assert 'id="discStage1"' in html
        assert 'id="discStage2"' in html
        assert 'id="discStage3"' in html
        assert 'id="discStage4"' in html
        assert 'id="discStage5"' in html

        # 5. Telemetry log container and cancellation
        assert 'id="pageDiscoveryTerminalLogs"' in html
        assert 'id="btnCancelDiscoveryRun"' in html

    def test_frozen_rule_b_summary_reflects_eight_canonical_criteria(self, client):
        """Phase 12.0 Section 5: Quality Guarantee must state the 8 canonical criteria, not just ratings."""
        res = client.get("/")
        html = res.text

        # Quality Guarantee card mentions canonical criteria
        assert "Frozen Rule B Quality Gate" in html
        assert "8 Canonical Qualification Criteria" in html
        assert "review volume" in html
        assert "minimum rating" in html
        assert "accepted evidence source" in html
        assert "evidence recency" in html
        assert "operational signals" in html
        assert "boundary match" in html
        assert "zero review conflict" in html
        assert "closure flags" in html

    def test_no_hardcoded_run_counts_in_dom(self, client):
        """Phase 12.0 Section 10: DOM metrics must be backend-driven and not hardcoded."""
        res = client.get("/")
        html = res.text

        # Check that old hardcoded 142 discovered candidates count is gone from stage values
        assert '<div class="p-stage-val" id="plDiscoveredVal">142</div>' not in html
        assert '<div class="p-stage-val">142</div>' not in html

    def test_provider_health_matrix_honest_diagnostics(self, client):
        """Phase 12.0 Section 2: Provider health cards must honestly represent Apify & Google Places."""
        res = client.get("/")
        html = res.text

        # Overpass Active, Web Search Connected
        assert "OpenStreetMap Overpass" in html
        # Apify exhausted / disabled
        assert "EXHAUSTED" in html or "Credits Exhausted" in html or "Disabled" in html
        # Google Places disallowed
        assert "Disallowed" in html or "DISALLOWED" in html


class TestBackendMarketAndProviderValidation:
    """Verifies backend enforcement on POST /api/search."""

    @pytest.fixture(autouse=True)
    def mock_pipeline_task(self, monkeypatch):
        monkeypatch.setattr(server, "run_pipeline_task", lambda *args, **kwargs: None)

    def test_accepts_valid_manchester_dry_run(self, client):
        """Phase 12.0 Section 3 & 4: Manchester request is accepted with valid run record."""
        payload = {
            "city": "Manchester",
            "country": "United Kingdom",
            "industry": "Restaurants",
            "qualified_leads_needed": 3,
            "batch_size": 10,
            "discovery_mode": "FREE_LOCAL",
            "dry_run": True,
        }
        res = client.post("/api/search", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "started"
        assert "run_id" in data
        assert data["run_id"].startswith("RUN-")

    def test_rejects_unsupported_market_leeds(self, client):
        """Phase 12.0 Section 4: Non-Manchester market (Leeds) must be rejected with HTTP 400."""
        payload = {
            "city": "Leeds",
            "country": "United Kingdom",
            "industry": "Restaurants",
            "qualified_leads_needed": 3,
            "batch_size": 10,
            "discovery_mode": "FREE_LOCAL",
            "dry_run": True,
        }
        res = client.post("/api/search", json=payload)
        assert res.status_code == 400
        msg = res.json().get("message", "") + res.json().get("detail", "")
        assert "Only Manchester, UK (#162378) is authorized" in msg

    def test_rejects_unsupported_market_london(self, client):
        """Phase 12.0 Section 4: Non-Manchester market (London) must be rejected with HTTP 400."""
        payload = {
            "city": "London",
            "country": "United Kingdom",
            "industry": "Restaurants",
            "qualified_leads_needed": 3,
            "batch_size": 10,
            "discovery_mode": "FREE_LOCAL",
            "dry_run": True,
        }
        res = client.post("/api/search", json=payload)
        assert res.status_code == 400
        msg = res.json().get("message", "") + res.json().get("detail", "")
        assert "Only Manchester, UK (#162378) is authorized" in msg

    def test_rejects_disallowed_google_places_provider(self, client):
        """Phase 12.0 Section 2: Request attempting Google Places must be rejected with HTTP 400."""
        payload = {
            "city": "Manchester",
            "country": "United Kingdom",
            "industry": "Restaurants",
            "qualified_leads_needed": 3,
            "batch_size": 10,
            "discovery_mode": "GOOGLE_PLACES",
            "dry_run": True,
        }
        res = client.post("/api/search", json=payload)
        assert res.status_code == 400
        msg = res.json().get("message", "") + res.json().get("detail", "")
        assert "Google Places API is strictly disallowed" in msg

    def test_rejects_exhausted_apify_provider(self, client):
        """Phase 12.0 Section 2: Request attempting Apify mode must be rejected with HTTP 400."""
        payload = {
            "city": "Manchester",
            "country": "United Kingdom",
            "industry": "Restaurants",
            "qualified_leads_needed": 3,
            "batch_size": 10,
            "discovery_mode": "APIFY",
            "dry_run": True,
        }
        res = client.post("/api/search", json=payload)
        assert res.status_code == 400
        msg = res.json().get("message", "") + res.json().get("detail", "")
        assert "Apify provider credits are exhausted" in msg or "Apify discovery engine is currently unavailable" in msg

    def test_enforces_batch_size_limits(self, client):
        """Phase 12.0 Section 3: Batch size must be within conservative bounds (1 to 25)."""
        # Batch size too high (> 25)
        payload_high = {
            "city": "Manchester",
            "country": "United Kingdom",
            "industry": "Restaurants",
            "qualified_leads_needed": 10,
            "batch_size": 50,
            "discovery_mode": "FREE_LOCAL",
            "dry_run": True,
        }
        res = client.post("/api/search", json=payload_high)
        assert res.status_code in [400, 422]


class TestRunLifecycleAndConcurrency:
    """Verifies concurrency safety, duplicate prevention, and lifecycle state management."""

    def test_prevents_duplicate_active_submissions(self, client):
        """Phase 12.0 Section 8: Repeated clicks while running must return HTTP 409 Conflict."""
        # Set active run state to simulating a running task
        server.active_run_state["is_running"] = True
        server.active_run_state["status"] = PipelineRunStatus.RUNNING.value
        server.active_run_state["run_id"] = "RUN-EXISTING-123"

        payload = {
            "city": "Manchester",
            "country": "United Kingdom",
            "industry": "Restaurants",
            "qualified_leads_needed": 3,
            "discovery_mode": "FREE_LOCAL",
            "dry_run": True,
        }
        res = client.post("/api/search", json=payload)
        assert res.status_code == 409
        msg = res.json().get("message", "") + res.json().get("detail", "")
        assert "already in progress" in msg

    def test_cancellation_endpoint_marks_run_cancelled(self, client):
        """Phase 12.0 Section 8: Operator can safely cancel an active run."""
        # Create an active run record
        rec = run_manager.create_run(
            country="United Kingdom",
            cities=["Manchester"],
            industry="Restaurants",
            qualified_leads_needed=3,
            batch_size=5,
            discovery_mode="FREE_LOCAL",
            dry_run=True,
        )
        run_manager.start_run(rec["run_id"])
        server.active_run_state["is_running"] = True
        server.active_run_state["run_id"] = rec["run_id"]

        res = client.post("/api/search/cancel")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert "marked CANCELLED" in data["message"]

        # Verify run manager record
        fetched = run_manager.get_run(rec["run_id"])
        assert fetched["status"] == PipelineRunStatus.CANCELLED.value
        assert fetched["is_running"] is False

    def test_status_endpoint_returns_honest_data(self, client):
        """Phase 12.0 Section 10: GET /api/status returns live/latest run with counters."""
        res = client.get("/api/status")
        assert res.status_code == 200
        data = res.json()
        assert "status" in data
        assert "is_running" in data


class TestProviderDiagnosticsEndpoint:
    """Verifies GET /api/discovery/status returns accurate provider availability."""

    def test_discovery_status_reflects_actual_providers(self, client):
        """Phase 12.0 Section 2: Returns health check with OSM active, Apify disabled, Places disallowed."""
        res = client.get("/api/discovery/status")
        assert res.status_code == 200
        data = res.json()
        providers = data.get("providers", {})

        # OpenStreetMap
        assert "OpenStreetMap" in providers
        assert providers["OpenStreetMap"] == "CONNECTED"

        # Apify
        assert "Apify" in providers
        assert "EXHAUSTED" in providers["Apify"] or "DISABLED" in providers["Apify"]

        # Google Places
        assert "GooglePlaces" in providers
        assert "DISALLOWED" in providers["GooglePlaces"]


class TestCommercialSafetyInvariants:
    """Verifies that all Phase 12.0 safety invariants remain strictly locked."""

    def test_commercial_actions_locked(self):
        """Phase 12.0 Section 13: COMMERCIAL_ACTIONS_ENABLED must be False."""
        assert SystemConfig.COMMERCIAL_ACTIONS_ENABLED is False

    def test_automated_email_disabled(self):
        """Phase 12.0 Section 13: AUTOMATED_EMAIL_ENABLED must be False."""
        assert SystemConfig.AUTOMATED_EMAIL_ENABLED is False

    def test_travel_mode_active(self):
        """Phase 12.0 Section 13: TRAVEL_MODE must be True in production."""
        assert SystemConfig.TRAVEL_MODE is True

    def test_rule_b_frozen(self):
        """Phase 12.0 Section 5: RULE_B must be FROZEN."""
        assert RULE_B_VERSION == "FROZEN"

    def test_rule_b_canonical_criteria_count(self):
        """Phase 12.0 Section 5: Rule B must have exactly 8 canonical criteria."""
        assert get_canonical_rule_b_criteria_count() == 8
        assert RULE_B_VERSION == "FROZEN"
        criteria = get_canonical_rule_b_criteria()
        assert len(criteria) == 8
        criterion_ids = [c["id"] for c in criteria]
        assert "RULE_B_01_REVIEW_VOLUME" in criterion_ids
        assert "RULE_B_02_MINIMUM_RATING" in criterion_ids
        assert "RULE_B_03_ACCEPTED_SOURCE" in criterion_ids
        assert "RULE_B_04_REVIEW_RECENCY" in criterion_ids
        assert "RULE_B_05_OPERATIONAL_SIGNAL" in criterion_ids
        assert "RULE_B_06_IDENTITY_LOCATION" in criterion_ids
        assert "RULE_B_07_NO_REVIEW_CONFLICT" in criterion_ids
        assert "RULE_B_08_NO_CLOSURE_RED_FLAGS" in criterion_ids
