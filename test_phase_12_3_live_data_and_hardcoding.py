"""
test_phase_12_3_live_data_and_hardcoding.py
===========================================
Verification suite for:
1. Complete elimination of hardcoded/static placeholders in static/index.html (e.g. LEAD-00000000).
2. Honest separation of persistent Google Sheets CRM metrics (7 qualified, 64 review queue, 278 research log)
   and latest autonomous discovery run telemetry.
3. Availability and accuracy of /api/runs, /api/runs/latest, /api/review-queue, and /api/manual-outreach/batch-8-7.
4. Dynamic binding of Rule B badges, Freshness dimensions, and Pipeline stage candidates.
5. Strict preservation of safety invariants: TRAVEL_MODE=true, COMMERCIAL_ACTIONS_ENABLED=false, 0 unauthorized sheet writes.
"""

import os
import re
import json
import pytest
from fastapi.testclient import TestClient

import server
from lib.system.system_config import SystemConfig
from lib.system.pipeline_run_manager import run_manager
from lib.sheets.google_sheets import GoogleSheetsStorageProvider


@pytest.fixture(scope="module")
def client():
    c = TestClient(server.app)
    op_key = server._configured_operator_key()
    if op_key:
        sess = server.create_operator_session()
        c.headers.update({
            "Authorization": f"Bearer {sess['session_token']}",
            "X-Requested-With": "XMLHttpRequest",
        })
    return c


def test_index_html_no_static_lead_placeholders():
    """Verify that static/index.html contains no hardcoded LEAD-00000000 or fake business name fallbacks."""
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "LEAD-00000000" not in content, "Found forbidden placeholder LEAD-00000000 in index.html"
    
    # Check that drawerBusinessName and drawerLeadId use clean '--' placeholder
    assert '<h2 id="drawerBusinessName" style="font-size:1.45rem; font-weight:800; letter-spacing:-0.02em; color:var(--text-main);">--</h2>' in content
    assert '<div id="drawerLeadId" style="font-size:0.78rem; font-family:var(--font-mono); color:var(--text-muted); margin-top:2px;">--</div>' in content


def test_index_html_separated_home_metrics_and_run_banner():
    """Verify Home Hero separates persistent CRM counts and latest run banner."""
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert 'id="homeQualifiedVal"' in content
    assert 'id="homeReviewQueueVal"' in content
    assert 'id="homeResearchLogVal"' in content
    assert 'id="homeLatestRunBanner"' in content
    assert 'id="homeLatestRunText"' in content


def test_index_html_dynamic_pills_and_inspector_badges():
    """Verify quick filter pills and inspector have dynamic element IDs."""
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Pills
    assert 'id="pillCountReview"' in content
    assert 'id="pillCountFreshness"' in content

    # Inspector Rule B IDs
    assert 'id="inspCheckWebsite"' in content
    assert 'id="inspCheckRating"' in content
    assert 'id="inspCheckReviews"' in content
    assert 'id="inspCheckRecency"' in content
    assert 'id="inspCheckPolygon"' in content
    assert 'id="inspWebsite"' in content

    # Drawer Rule B IDs
    assert 'id="ruleBWebsiteBadge"' in content
    assert 'id="ruleBRatingBadge"' in content
    assert 'id="ruleBReviewCountBadge"' in content
    assert 'id="ruleBRecencyBadge"' in content
    assert 'id="ruleBPolygonBadge"' in content

    # Freshness dimension IDs
    assert 'id="freshWebsiteVal"' in content
    assert 'id="freshOpsVal"' in content
    assert 'id="freshPhoneVal"' in content
    assert 'id="freshSocialVal"' in content
    assert 'id="freshSentimentVal"' in content
    assert 'id="freshContactVal"' in content


def test_index_html_batch_8_7_modal_dynamic_counts():
    """Verify Phase 8.7 batch modal metrics use dynamic placeholder IDs."""
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert 'id="batchActiveCount"' in content
    assert 'id="batchAutomatedCount"' in content
    assert 'id="batchManualCount"' in content
    assert 'id="phase87BatchTableBody"' in content


def test_api_runs_and_latest(client):
    """Verify /api/runs and /api/runs/latest return authentic pipeline run data."""
    res_runs = client.get("/api/runs")
    assert res_runs.status_code == 200
    data_runs = res_runs.json()
    assert "runs" in data_runs
    assert "count" in data_runs
    assert data_runs["count"] >= 1

    res_latest = client.get("/api/runs/latest")
    assert res_latest.status_code == 200
    data_latest = res_latest.json()
    assert "run_id" in data_latest
    assert data_latest["status"] in ["COMPLETED", "FAILED", "RUNNING", "CANCELLED"]
    assert "results" in data_latest
    results = data_latest["results"]
    assert "candidates_discovered" in results
    assert "candidates_researched" in results
    assert "qualified_leads" in results


def test_api_review_queue_live(client):
    """Verify /api/review-queue loads the 64 genuine review items."""
    res = client.get("/api/review-queue")
    assert res.status_code == 200
    data = res.json()
    assert data.get("status") == "ok"
    assert data.get("tab") == "REVIEW_QUEUE"
    queue = data.get("queue", [])
    assert len(queue) == 64
    first = queue[0]
    assert "lead_id" in first
    assert "company_name" in first
    assert "rating" in first
    assert "review_count" in first


def test_api_manual_outreach_batch_8_7(client):
    """Verify /api/manual-outreach/batch-8-7 returns cohort summary and active batch leads."""
    res = client.get("/api/manual-outreach/batch-8-7")
    assert res.status_code == 200
    data = res.json()
    assert "cohort_summary" in data
    summary = data["cohort_summary"]
    assert "total_active_leads" in summary
    assert "automated_sendable" in summary
    assert "manual_outreach_ready" in summary
    assert "active_batch" in data
    assert len(data["active_batch"]) >= 1


def test_safety_invariants_and_zero_sheets_pollution():
    """Verify all safety invariants and ensure Google Sheets has not been polluted."""
    assert SystemConfig.is_travel_mode() is True
    assert SystemConfig.can_execute_commercial_actions() is False
    assert SystemConfig.is_automated_email_enabled() == SystemConfig.AUTOMATED_EMAIL_ENABLED

    storage = GoogleSheetsStorageProvider()
    leads = storage.fetch_all_leads()
    assert len(leads) == 7, f"Expected exactly 7 qualified leads in Google Sheets CRM, got {len(leads)}"

    # Confirm dry run candidates (e.g. Dog and Partridge) are NOT written to Google Sheets
    names = [l.get("company_name", "").lower() for l in leads]
    assert not any("dog and partridge" in n for n in names), "Dog and Partridge must NOT be in persistent CRM leads!"


def test_api_latest_run_candidates_and_dog_and_partridge(client):
    """Verify /api/runs/latest/candidates returns Dog and Partridge with full evidence."""
    res = client.get("/api/runs/latest/candidates")
    assert res.status_code == 200
    data = res.json()
    assert data.get("status") == "ok"
    assert "candidates" in data
    cands = data["candidates"]
    assert len(cands) >= 2

    # Find Dog and Partridge
    dog = next((c for c in cands if "dog and partridge" in c.get("company_name", "").lower()), None)
    assert dog is not None, "Dog and Partridge candidate must be present in latest run candidates!"
    assert dog["qualification_state"] == "OUTREACH_READY"
    assert dog["rating"] == 4.3
    assert dog["review_count"] == 88
    assert dog["is_dry_run"] is True
    assert dog["is_shadow_mode"] is True
    assert "wilmslow" in dog["address"].lower()
    assert "instagram" in dog.get("instagram_url", "").lower()
    assert "facebook" in dog.get("facebook_url", "").lower()


def test_index_html_no_invalid_lead_fallback():
    """Verify index.html does not fall back to allLeads[0] on invalid lead ID."""
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Ensure openLeadDrawer and openLeadDrawerById have no fallback to allLeads[0]
    assert "allLeads[0];" not in content
    assert "selectedLead = allLeads[0];" not in content
    assert 'id="pillCountLatestRun"' in content
