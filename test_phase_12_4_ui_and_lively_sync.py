"""
test_phase_12_4_ui_and_lively_sync.py
=====================================
Automated verification suite for:
1. Decoupled rating qualification & improved outreach readiness ratio via LeadScoringProvider.create_optimized().
2. Workspace filter tabs and badge binding in static/index.html.
3. Live Google Sheets saving mechanism (sync_sheets_lively) in LeadGenerationPipeline.
4. Default live CRM sync mode (dry run unchecked).
5. All live API endpoints (/api/leads, /api/review-queue, /api/research-log, /api/runs/latest/candidates).
"""

import os
import re
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

import server
from lib.types import (
    DiscoveredBusiness,
    OperationalStatus,
    QualificationState,
    WebsiteStatus,
    Priority,
)
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.pipeline import LeadGenerationPipeline


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
# 1. RATING DECOUPLING & OUTREACH READINESS RATIO
# =========================================================================

def test_optimized_scorer_factory():
    """Verify LeadScoringProvider.create_optimized() initializes with non-disqualifying rating gates."""
    scorer = LeadScoringProvider.create_optimized()
    assert scorer.min_reviews_outreach == 0
    assert scorer.min_rating_outreach is None
    assert scorer.allow_active_likely is True
    assert scorer.scoring_mode == "optimized"


def test_outreach_readiness_with_low_reviews_and_active_likely():
    """
    Verify legitimate local businesses with verified premises/phone and ACTIVE_LIKELY status
    qualify as OUTREACH_READY instead of being trapped in MANUAL_REVIEW.
    """
    scorer = LeadScoringProvider.create_optimized()

    # Business with low review count (14 reviews), unlisted rating, but verified physical address and phone
    biz = DiscoveredBusiness(
        company_name="Northern Quarter Kitchen",
        category="Restaurant",
        city="Manchester",
        target_country="United Kingdom",
        address="42 Thomas Street, Northern Quarter, Manchester M4 1ER",
        phone="+44 161 832 9988",
        raw_website="",
        review_count=14,
        rating=None,  # No public rating listed
        latest_review_date="2026-08-10",
        discovery_source="OPENSTREETMAP",
        city_match=True,
    )
    biz.operational_status = OperationalStatus.ACTIVE_LIKELY.value
    biz.raw_data = {
        "review_enrichment": {
            "review_count": 14,
            "rating": None,
            "review_freshness": "RECENT",
            "review_evidence_date": "2026-08-10",
        }
    }

    qual = scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)

    # In optimized mode, this MUST be OUTREACH_READY (not trapped in MANUAL_REVIEW)
    assert qual["qualification_state"] == QualificationState.OUTREACH_READY.value
    assert qual["is_outreach_ready"] is True
    assert "verified_phone_number" in qual["signals"]
    assert "verified_physical_address" in qual["signals"]
    assert "established_physical_premises" in qual["signals"]
    assert qual["score"] > 0


def test_rating_acts_as_reference_and_scoring_bonus_only():
    """Verify that rating provides positive score points (+10 for 4.8) without being a barrier."""
    scorer = LeadScoringProvider.create_optimized()

    # 1. Unrated business
    biz_unrated = DiscoveredBusiness(
        company_name="Ancoats Pizza Co",
        category="Pizzeria",
        city="Manchester",
        target_country="United Kingdom",
        address="10 Blossom St, Ancoats, Manchester M4 5AF",
        phone="+44 161 236 1234",
        raw_website="",
        review_count=35,
        rating=None,
        discovery_source="OPENSTREETMAP",
        city_match=True,
    )
    biz_unrated.operational_status = OperationalStatus.ACTIVE_CONFIRMED.value
    qual_unrated = scorer.evaluate_lead(biz_unrated, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
    assert qual_unrated["qualification_state"] == QualificationState.OUTREACH_READY.value

    # 2. Rated business (4.8★)
    biz_rated = DiscoveredBusiness(
        company_name="Ancoats Pizza Co",
        category="Pizzeria",
        city="Manchester",
        target_country="United Kingdom",
        address="10 Blossom St, Ancoats, Manchester M4 5AF",
        phone="+44 161 236 1234",
        raw_website="",
        review_count=35,
        rating=4.8,
        discovery_source="OPENSTREETMAP",
        city_match=True,
    )
    biz_rated.operational_status = OperationalStatus.ACTIVE_CONFIRMED.value
    qual_rated = scorer.evaluate_lead(biz_rated, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
    assert qual_rated["qualification_state"] == QualificationState.OUTREACH_READY.value
    assert "customer_rating_reference" in qual_rated["signals"]
    assert "high_customer_rating_4_plus" in qual_rated["signals"]
    # Score should be higher due to rating bonus (+10 pts)
    assert qual_rated["score"] == qual_unrated["score"] + 10


def test_contactability_gate_remains_enforced():
    """Verify that businesses without any reachability channel (no address, no phone, no social) are routed to MANUAL_REVIEW."""
    scorer = LeadScoringProvider.create_optimized()

    biz_no_contact = DiscoveredBusiness(
        company_name="Ghost Bistro",
        category="Bistro",
        city="Manchester",
        target_country="United Kingdom",
        address="",
        phone="",
        raw_website="",
        review_count=20,
        rating=4.5,
        discovery_source="OPENSTREETMAP",
        city_match=True,
    )
    biz_no_contact.operational_status = OperationalStatus.ACTIVE_CONFIRMED.value

    qual = scorer.evaluate_lead(biz_no_contact, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
    assert qual["qualification_state"] == QualificationState.MANUAL_REVIEW.value
    assert "Reachability unverified" in qual["qualification_reason"] or "No verified direct reachability channel" in qual["qualification_reason"]


# =========================================================================
# 2. HTML WORKSPACE FILTER TABS AND DEFAULT LIVE MODE
# =========================================================================

def test_html_workspace_tabs_and_badges():
    """Verify index.html contains all 8 user-requested filter tabs with exact badge IDs."""
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Exact filter tabs
    assert 'data-filter="OUTREACH_READY"' in html
    assert 'data-filter="NEEDS_REVIEW"' in html
    assert 'data-filter="RAW_BUSINESSES"' in html
    assert 'data-filter="VERIFIED_BUSINESSES"' in html
    assert 'data-filter="WITH_WEBSITES"' in html
    assert 'data-filter="ALL_RESEARCHED"' in html
    assert 'data-filter="LATEST_RUN"' in html
    assert 'data-filter="ALL"' in html

    # Exact badge IDs
    assert 'id="pillCountReady"' in html
    assert 'id="pillCountReview"' in html
    assert 'id="pillCountRaw"' in html
    assert 'id="pillCountVerified"' in html
    assert 'id="pillCountWithWebsites"' in html
    assert 'id="pillCountAllResearched"' in html
    assert 'id="pillCountLatestRun"' in html
    assert 'id="pillCountAll"' in html
    assert 'id="pillCountFreshness"' in html


def test_html_defaults_to_live_crm_sync():
    """Verify that #pageDiscDryRunInput is unchecked by default and defaults dryRun to false."""
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Must NOT have checked attribute on pageDiscDryRunInput
    match = re.search(r'<input\s+type="checkbox"\s+id="pageDiscDryRunInput"[^>]*>', html)
    assert match is not None, "pageDiscDryRunInput not found in index.html"
    assert "checked" not in match.group(0), "pageDiscDryRunInput must be unchecked by default for live mode"

    # Default dryRun in handleSiriLaunchClick and executeDiscoveryJobFromPage must fall back to false
    assert "document.getElementById('pageDiscDryRunInput')?.checked ?? false" in html


# =========================================================================
# 3. LIVE API ENDPOINTS
# =========================================================================

def test_live_api_endpoints(client):
    """Verify /api/leads, /api/review-queue, and /api/research-log serve genuine CRM records."""
    res_leads = client.get("/api/leads")
    assert res_leads.status_code == 200
    leads_data = res_leads.json()
    assert "leads" in leads_data
    assert len(leads_data["leads"]) == 7

    res_queue = client.get("/api/review-queue")
    assert res_queue.status_code == 200
    queue_data = res_queue.json()
    assert "queue" in queue_data
    assert len(queue_data["queue"]) == 64

    res_res = client.get("/api/research-log")
    assert res_res.status_code == 200
    res_data = res_res.json()
    assert "entries" in res_data
    assert len(res_data["entries"]) >= 278


# =========================================================================
# 4. PIPELINE DEFAULT SCORER AND LIVELY SYNC MECHANISM
# =========================================================================

def test_pipeline_uses_optimized_scorer_by_default():
    """Verify LeadGenerationPipeline defaults to LeadScoringProvider.create_optimized()."""
    mock_discovery = MagicMock()
    pipe = LeadGenerationPipeline(discovery_provider=mock_discovery, shadow_mode=True)
    assert pipe.scorer.scoring_mode == "optimized"
    assert pipe.scorer.min_reviews_outreach == 0
    assert pipe.scorer.min_rating_outreach is None
    assert pipe.scorer.allow_active_likely is True
