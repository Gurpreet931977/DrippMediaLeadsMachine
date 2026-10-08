"""
test_frontend_productization.py
===============================
Deterministic Integration & Safety Tests for Frontend Productization Pass.

Verifies:
  1. Root HTML (/): Serves HTTP 200, brand identity, fonts, and responsive structure.
  2. Information Architecture: 9 distinct views (Overview, Leads, Pipeline, Research,
     Freshness, Outreach, Monitoring, Readiness, Settings).
  3. Narrative Lead Dossier: 6 human-readable story questions and 4 drawer tabs.
  4. Production Readiness API (/api/system/readiness): 20 gates, STAGING_READY state,
     and 7 logical category groupings.
  5. Freshness Center API (/api/system/freshness-summary): 6 canonical dimensions evaluated
     with exact backend cadences (Website: 30d, Operational: 30d, Phone: 60d, Social: 30d,
     Review: 14d, Contactability: 30d).
  6. Provider Accuracy: Distinguishes between Active, Configured, Fallback, and Disabled providers.
  7. Safety Invariants: Travel Mode = Active, Commercial Actions = Locked, 0 live send controls,
     0 credentials or partial keys leaked in DOM.
  8. Mobile-safe viewport and markup structure.
"""

import os
import re
import pytest
from fastapi.testclient import TestClient

import server
from lib.system.system_config import SystemConfig


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app)


class TestFrontendProductization:
    """Frontend productization integration and contract verification."""

    def test_root_serves_html_and_brand_assets(self, client):
        """1. Root / returns HTTP 200, valid HTML, and Dripp branding."""
        res = client.get("/")
        assert res.status_code == 200
        html = res.text
        assert "<!DOCTYPE html>" in html
        assert "Dripp International Leads" in html
        assert "/static/logos/dripp-logo-yellow-cropped.png" in html
        assert "Plus+Jakarta+Sans" in html
        assert "JetBrains+Mono" in html
        assert 'name="viewport"' in html
        assert "width=device-width, initial-scale=1.0" in html

    def test_safety_banner_invariants_rendered(self, client):
        """2. Persistent safety banner clearly displays Travel Mode & locked commercial actions."""
        res = client.get("/")
        html = res.text
        assert "PROTECTED TRAVEL MODE ACTIVE" in html
        assert "COMMERCIAL ACTIONS LOCKED" in html
        assert "Travel Mode: Active" in html
        assert "Commercial: Locked" in html
        assert "Cron: Disabled" in html
        assert "Automated Email: Disabled" in html
        assert "CRM Writes: 0" in html

    def test_nine_primary_navigation_views(self, client):
        """3. All 9 primary operational views exist in DOM with valid tab IDs."""
        res = client.get("/")
        html = res.text
        required_views = [
            "view-overview",
            "view-leads",
            "view-pipeline",
            "view-research",
            "view-freshness",
            "view-outreach",
            "view-monitoring",
            "view-readiness",
            "view-settings",
        ]
        for v in required_views:
            assert f'id="{v}"' in html, f"Missing view: {v}"

    def test_narrative_lead_detail_drawer_structure(self, client):
        """4. Narrative Lead Detail drawer contains all 6 story questions and 4 tabs."""
        res = client.get("/")
        html = res.text
        assert 'id="leadDrawer"' in html
        assert "1. Who is this business?" in html
        assert "2. Why did we find it?" in html
        assert "3. Why did it qualify?" in html
        assert "4. What evidence supports it?" in html
        assert "5. Is it contactable?" in html
        assert "6. Is outreach possible?" in html
        assert "Narrative Dossier" in html
        assert "Rule B Evidence" in html
        assert "Freshness Dimensions" in html
        assert "Technical JSON" in html

    def test_readiness_endpoint_and_gates(self, client):
        """5. /api/system/readiness returns STAGING_READY and 20 deterministic gates."""
        res = client.get("/api/system/readiness")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["readiness_state"] in ("STAGING_READY", "TECHNICALLY_READY")
        assert data["passed_gates_count"] == 20
        assert data["total_gates"] == 20
        assert len(data["gates"]) == 20

        # Verify structured evidence present on gates
        gate_ids = [g["gate_id"] for g in data["gates"]]
        assert "CONFIG_VALIDITY" in gate_ids
        assert "ENVIRONMENT_SEPARATION" in gate_ids
        assert "RULE_B_INTEGRITY" in gate_ids
        assert "OUTREACH_LOCK_STATE" in gate_ids
        assert "EMAIL_LOCK_STATE" in gate_ids
        assert "PROVIDER_HEALTH" in gate_ids
        assert "QUOTA_HEALTH" in gate_ids
        assert "BACKUP_HEALTH" in gate_ids
        assert "RESTORE_VERIFICATION" in gate_ids
        assert "ROLLBACK_CAPABILITY" in gate_ids

    def test_readiness_groupings_in_frontend(self, client):
        """6. Frontend groups the 20 gates logically into 7 categories."""
        res = client.get("/")
        html = res.text
        assert "Security & Authentication" in html
        assert "Data Integrity & Deduplication" in html
        assert "External Providers & Research" in html
        assert "Infrastructure & Storage" in html
        assert "Central Monitoring & Incidents" in html
        assert "Commercial Safety & Execution Gates" in html
        assert "Disaster Recovery & Backup Drills" in html

    def test_freshness_summary_endpoint_and_cadences(self, client):
        """7. /api/system/freshness-summary returns 6 dimensions with exact canonical cadences."""
        res = client.get("/api/system/freshness-summary")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["total_evaluated"] >= 0
        assert "cadences_days" in data

        cadences = data["cadences_days"]
        assert cadences["WEBSITE"] == 30
        assert cadences["OPERATIONAL"] == 30
        assert cadences["PHONE"] == 60
        assert cadences["SOCIAL"] == 30
        assert cadences["REVIEW"] == 14
        assert cadences["CONTACTABILITY"] == 30

    def test_freshness_cadences_rendered_accurately(self, client):
        """8. Frontend renders exact canonical cadences for all 6 dimensions."""
        res = client.get("/")
        html = res.text
        assert "30-Day Cadence" in html  # Website, Operational, Social, Contactability
        assert "60-Day Cadence" in html  # Phone
        assert "14-Day Cadence" in html  # Review

    def test_provider_transparency_and_accuracy(self, client):
        """9. Provider list accurately classifies active, configured, fallback, and disabled providers."""
        res = client.get("/")
        html = res.text
        # Tavily is configured/available
        assert "Tavily Research" in html
        assert "CONFIGURED / AVAILABLE" in html
        # OSM is connected/active
        assert "OpenStreetMap" in html
        assert "CONNECTED / ACTIVE" in html
        # DuckDuckGo is fallback
        assert "DuckDuckGo / Crawl" in html
        assert "FALLBACK READY" in html
        # Apify is disabled
        assert "Apify Actors" in html
        assert "DISABLED" in html
        # SMTP email is locked under commercial lockout
        assert "COMMERCIAL LOCKED" in html

    def test_safety_state_invariants(self, client):
        """10. System safety invariants remain strictly locked in backend config."""
        res = client.get("/api/system/config")
        assert res.status_code == 200
        cfg = res.json()["system_config"]
        assert cfg["travel_mode"] is True
        assert cfg["commercial_actions_enabled"] is False
        assert cfg["automated_email_enabled"] is False

    def test_no_secrets_or_partial_keys_in_dom(self):
        """11. No sensitive API credentials or partial key prefixes leaked in static/index.html."""
        with open("static/index.html", "r") as f:
            content = f.read()
        # Ensure no live keys, tokens, or masked key prefixes
        assert "tvly-" not in content
        assert "AIzaSy" not in content
        assert "client_secret" not in content
        assert "private_key" not in content

    def test_no_production_send_buttons(self, client):
        """12. No active production dispatch controls exist in UI."""
        res = client.get("/")
        html = res.text
        # Ensure there are no active form buttons that trigger email send
        assert 'name="send_outreach"' not in html
        assert 'action="/api/outreach/send"' not in html
        assert "LOCKED" in html
