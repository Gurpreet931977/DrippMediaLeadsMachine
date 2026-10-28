"""
test_phase_12_5_outreach_status_and_filtering.py
================================================
Comprehensive verification suite for Phase 12.5:
1. Outreach check / uncheck functionality (reaching a brand, reset to pending).
2. "Hide Reached Brands" list removal filter.
3. Granular outcome taxonomy covering all aspects & reasons:
   - Closed client (deal won, deposits, retainers)
   - Meeting booked / interested (positive traction)
   - Didn't pick up call (no answer, busy, voicemail)
   - Didn't reply / ghosted (DM delivered, email opened)
   - Not interested / declined (budget, timing)
   - Invalid lead (wrong number, permanently closed, bad fit)
4. Google Sheets CRM live synchronization mapping (LEADS + REVIEW_QUEUE).
5. API endpoints (GET /api/leads/outreach-aspects, POST /api/leads/outreach-status).
6. Frontend static/index.html UI components and JavaScript controller bindings.
"""

import os
import json
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import server
from lib.outreach.outreach_status_service import OutreachStatusService, OUTREACH_TAXONOMY
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


# =========================================================================
# 1. OUTREACH TAXONOMY & REASONS VERIFICATION
# =========================================================================

def test_outreach_taxonomy_coverage():
    """Verify that OUTREACH_TAXONOMY exhaustively covers all requested aspects and reasons."""
    tax = OutreachStatusService.get_taxonomy()
    
    # 1. Did we close the client?
    assert "CLOSED_WON" in tax
    closed_reasons = [r["id"] for r in tax["CLOSED_WON"]["reasons"]]
    assert "proposal_accepted" in closed_reasons
    assert "deposit_paid" in closed_reasons
    assert "monthly_retainer" in closed_reasons
    assert tax["CLOSED_WON"]["default_sales_stage"] == "WON"
    assert tax["CLOSED_WON"]["default_client_status"] == "ACTIVE"
    
    # 2. Did it go good? (Meeting / Interested)
    assert "MEETING_BOOKED" in tax
    assert "INTERESTED" in tax
    meet_reasons = [r["id"] for r in tax["MEETING_BOOKED"]["reasons"]]
    assert "owner_call_scheduled" in meet_reasons
    assert "callback_requested" in meet_reasons
    
    int_reasons = [r["id"] for r in tax["INTERESTED"]["reasons"]]
    assert "requested_pricing" in int_reasons
    assert "asked_for_mockup" in int_reasons
    
    # 3. Didn't pick up call (No answer)
    assert "NO_ANSWER" in tax
    no_ans_reasons = [r["id"] for r in tax["NO_ANSWER"]["reasons"]]
    assert "rang_out_no_answer" in no_ans_reasons
    assert "busy_line" in no_ans_reasons
    assert "voicemail_left" in no_ans_reasons
    assert "staff_gatekeeper_busy" in no_ans_reasons
    assert tax["NO_ANSWER"]["default_call_status"] == "NO_ANSWER"
    assert tax["NO_ANSWER"]["default_call_outcome"] == "NO_ANSWER"
    
    # 4. Didn't reply / ghosted
    assert "NO_REPLY" in tax
    no_rep_reasons = [r["id"] for r in tax["NO_REPLY"]["reasons"]]
    assert "dm_delivered_no_reply" in no_rep_reasons
    assert "email_opened_no_reply" in no_rep_reasons
    assert "ghosted_after_initial_chat" in no_rep_reasons
    assert tax["NO_REPLY"]["default_response_status"] == "NO_RESPONSE"
    
    # 5. Was lead invalid? (wrong number, permanently closed)
    assert "INVALID_LEAD" in tax
    inv_reasons = [r["id"] for r in tax["INVALID_LEAD"]["reasons"]]
    assert "wrong_number_disconnected" in inv_reasons
    assert "business_permanently_closed" in inv_reasons
    assert "already_has_agency" in inv_reasons
    assert tax["INVALID_LEAD"]["default_call_outcome"] == "WRONG_NUMBER"
    
    # 6. Not interested
    assert "NOT_INTERESTED" in tax
    not_int_reasons = [r["id"] for r in tax["NOT_INTERESTED"]["reasons"]]
    assert "budget_constraints" in not_int_reasons
    assert "social_media_sufficient" in not_int_reasons


# =========================================================================
# 2. OUTREACH STATUS SERVICE UPDATE & CACHE PERSISTENCE
# =========================================================================

def test_update_outreach_status_closed_won():
    """Verify marking a lead as CLOSED_WON updates local cache and maps CRM fields."""
    with patch.object(GoogleSheetsStorageProvider, "update_lead_outreach", return_value=True):
        res = OutreachStatusService.update_outreach_status(
            lead_id="LEAD-MAN-682E5D",
            is_reached=True,
            outcome_category="CLOSED_WON",
            outcome_reason="Accepted Website Proposal",
            notes="Owner Dave signed £1,200 website contract",
            channel="PHONE",
            sync_sheets=False
        )
        assert res["status"] == "ok"
        assert res["lead_id"] == "LEAD-MAN-682E5D"
        assert res["is_reached"] is True
        assert res["outreach_status"] == "CLOSED_WON"
        assert res["sales_stage"] == "WON"
        assert res["client_status"] == "ACTIVE"
        assert "Accepted Website Proposal" in res["notes"]
        assert "Dave" in res["notes"]
        assert res["last_contact_at"] != ""


def test_update_outreach_status_no_answer():
    """Verify marking a lead as NO_ANSWER (didn't pick up call)."""
    with patch.object(GoogleSheetsStorageProvider, "update_lead_outreach", return_value=True):
        res = OutreachStatusService.update_outreach_status(
            lead_id="LEAD-MAN-14A2D3",
            is_reached=True,
            outcome_category="NO_ANSWER",
            outcome_reason="Phone Rang Out (No Answer)",
            notes="Called at 2:30pm, rang 6 times no voicemail",
            channel="PHONE",
            sync_sheets=False
        )
        assert res["status"] == "ok"
        assert res["outreach_status"] == "NO_ANSWER"
        assert res["call_status"] == "NO_ANSWER"
        assert res["call_outcome"] == "NO_ANSWER"
        assert res["response_status"] == "NO_RESPONSE"


def test_update_outreach_status_no_reply():
    """Verify marking a lead as NO_REPLY (didn't reply / ghosted)."""
    with patch.object(GoogleSheetsStorageProvider, "update_lead_outreach", return_value=True):
        res = OutreachStatusService.update_outreach_status(
            lead_id="LEAD-MAN-E81185",
            is_reached=True,
            outcome_category="NO_REPLY",
            outcome_reason="DM Delivered - No Reply (48h+)",
            notes="Sent DM with portfolio, seen but no response",
            channel="INSTAGRAM",
            sync_sheets=False
        )
        assert res["status"] == "ok"
        assert res["outreach_status"] == "NO_REPLY"
        assert res["response_status"] == "NO_RESPONSE"


def test_update_outreach_status_uncheck_reset_to_pending():
    """Verify unchecking a lead resets it back to Pending Outreach / NOT_READY."""
    with patch.object(GoogleSheetsStorageProvider, "update_lead_outreach", return_value=True):
        res = OutreachStatusService.update_outreach_status(
            lead_id="LEAD-MAN-682E5D",
            is_reached=False,
            sync_sheets=False
        )
        assert res["status"] == "ok"
        assert res["is_reached"] is False
        assert res["outreach_status"] == "NOT_READY"
        assert res["lead_status"] == "NOT_CONTACTED"
        assert res["sales_stage"] == "NEW"
        assert res["client_status"] == "PROSPECT"
        assert res["last_contact_at"] == ""
        assert "Reset to Pending" in res["notes"]


# =========================================================================
# 3. GOOGLE SHEETS LIVE RECONCILIATION MAPPING
# =========================================================================

def test_google_sheets_storage_provider_update_lead_outreach():
    """Verify GoogleSheetsStorageProvider has update_lead_outreach method with correct columns."""
    assert hasattr(GoogleSheetsStorageProvider, "update_lead_outreach")
    
    provider = GoogleSheetsStorageProvider.__new__(GoogleSheetsStorageProvider)
    mock_ws = MagicMock()
    mock_ws.get_all_records.return_value = [
        {"lead_id": "LEAD-MAN-682E5D", "company_name": "Mary D's Beamish Bar", "outreach_status": "NOT_READY"}
    ]
    mock_ws.row_values.return_value = ["lead_id", "company_name", "outreach_status", "call_outcome", "manual_outreach_notes"]
    provider._leads_ws = mock_ws
    provider._review_ws = MagicMock()
    provider._review_ws.get_all_records.return_value = []
    provider.ensure_leads_columns = MagicMock()
    
    success = provider.update_lead_outreach(
        lead_id="LEAD-MAN-682E5D",
        fields={"outreach_status": "CLOSED_WON", "call_outcome": "CONVERTED", "manual_outreach_notes": "Closed £1.2k"}
    )
    assert success is True
    assert mock_ws.update.called


# =========================================================================
# 4. API ENDPOINTS VERIFICATION
# =========================================================================

def test_api_outreach_aspects(client):
    """Verify GET /api/leads/outreach-aspects returns 200 with full taxonomy."""
    res = client.get("/api/leads/outreach-aspects")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "taxonomy" in data
    assert "CLOSED_WON" in data["taxonomy"]
    assert "NO_ANSWER" in data["taxonomy"]
    assert "NO_REPLY" in data["taxonomy"]
    assert "INVALID_LEAD" in data["taxonomy"]


def test_api_post_outreach_status(client):
    """Verify POST /api/leads/outreach-status updates status and returns updated record."""
    with patch.object(GoogleSheetsStorageProvider, "update_lead_outreach", return_value=True):
        payload = {
            "lead_id": "LEAD-MAN-682E5D",
            "is_reached": True,
            "outcome_category": "MEETING_BOOKED",
            "outcome_reason": "In-Person Demo / Visit Booked",
            "notes": "Booked meeting with manager for Friday 3pm",
            "channel": "PHONE"
        }
        res = client.post("/api/leads/outreach-status", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["lead_id"] == "LEAD-MAN-682E5D"
        assert data["outreach_status"] == "MEETING_BOOKED"
        assert data["sales_stage"] == "MEETING_BOOKED"
        assert data["is_reached"] is True


def test_api_post_outreach_status_uncheck(client):
    """Verify POST /api/leads/outreach-status with is_reached=False unchecks and resets lead."""
    with patch.object(GoogleSheetsStorageProvider, "update_lead_outreach", return_value=True):
        payload = {
            "lead_id": "LEAD-MAN-682E5D",
            "is_reached": False
        }
        res = client.post("/api/leads/outreach-status", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["is_reached"] is False
        assert data["outreach_status"] == "NOT_READY"


# =========================================================================
# 5. FRONTEND STATIC/INDEX.HTML DOM & JS BINDINGS VERIFICATION
# =========================================================================

def test_static_html_outreach_toolbar_and_controls():
    """Verify static/index.html includes required outreach filter toolbar and inspector deck."""
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    assert os.path.exists(html_path)
    
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    
    # 1. Hide reached toggle
    assert 'id="toggleHideReached"' in html
    assert 'handleToggleHideReached(this.checked)' in html
    assert 'Hide Reached Brands' in html
    
    # 2. Sub-filter pills and counts
    assert 'data-outreach="ALL"' in html
    assert 'id="subCountAll"' in html
    assert 'data-outreach="PENDING"' in html
    assert 'id="subCountPending"' in html
    assert 'data-outreach="REACHED"' in html
    assert 'id="subCountReached"' in html
    assert 'data-outreach="CLOSED_WON"' in html
    assert 'id="subCountWon"' in html
    assert 'data-outreach="POSITIVE"' in html
    assert 'id="subCountPositive"' in html
    assert 'data-outreach="NO_ANSWER"' in html
    assert 'id="subCountNoAnswer"' in html
    assert 'data-outreach="NO_REPLY"' in html
    assert 'id="subCountNoReply"' in html
    assert 'data-outreach="NOT_INTERESTED"' in html
    assert 'id="subCountDeclined"' in html
    assert 'data-outreach="INVALID_LEAD"' in html
    assert 'id="subCountInvalid"' in html
    
    # 3. Live Inspector outreach deck
    assert 'id="inspReachedCheck"' in html
    assert 'id="inspOutreachStatusBadge"' in html
    assert 'id="inspOutcomeCategorySelect"' in html
    assert 'id="inspOutcomeReasonSelect"' in html
    assert 'id="inspChannelSelect"' in html
    assert 'id="inspOutcomeNotesInput"' in html
    assert 'saveOutreachFromInspector()' in html
    assert 'uncheckLeadFromInspector()' in html
    
    # 4. Stream card quick check & badge
    assert 'class="lead-stream-outreach-row"' in html
    assert 'toggleLeadReached(' in html
    
    # 5. JavaScript functions
    assert 'function isLeadReached(lead)' in html
    assert 'function getLeadOutreachCategory(lead)' in html
    assert 'function getOutreachBadgeInfo(lead)' in html
    assert 'function loadOutreachTaxonomy()' in html
    assert 'function populateReasonOptions(' in html
    assert 'function setOutreachFilter(flt)' in html
    assert 'function handleToggleHideReached(checked)' in html
    assert 'function saveOutreachFromInspector()' in html
    assert 'function uncheckLeadFromInspector()' in html
    assert 'function toggleLeadReached(' in html
    assert 'function recalculateOutreachCounts()' in html
    assert 'hideReachedBrands' in html
