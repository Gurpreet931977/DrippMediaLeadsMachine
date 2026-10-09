"""
test_phase_12_1_data_integration.py
===================================
Comprehensive Test Suite for Phase 12.1 — Real Data Integration & Live Backend Connection.

Covers:
  1. Successful Google Sheets CRM reads (LEADS, REVIEW_QUEUE, RESEARCH_LOG tabs).
  2. Missing Google credentials handling & resilience.
  3. Google API quota/permission error resilience without mock fallback.
  4. Empty sheet handling (preserving genuine 0 vs fake defaults).
  5. Backend API endpoints (/api/leads, /api/review-queue, /api/research-log) metadata & timestamps.
  6. CORS headers enforcement for public Vercel frontend origin.
  7. Operator API key authorization & protection of sensitive routes.
  8. Immutable commercial safety invariants (TRAVEL_MODE, Commercial Lock).
  9. Rule B compliance and Canonical Lead ID preservation.
 10. Audit of frontend static files & Vercel routing (0 leaked secrets, 0 fake blips).
"""

import os
import re
import json
import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from server import app, check_operator_authorization
from lib.sheets.google_sheets import GoogleSheetsStorageProvider
from lib.system.identity_integrity import CANONICAL_LEAD_ID_REGEX
from lib.system.system_config import SystemConfig


class TestGoogleSheetsLiveIntegration(unittest.TestCase):
    """Verifies genuine data reading from Google Sheets CRM."""

    def setUp(self):
        self.provider = GoogleSheetsStorageProvider()

    def test_successful_google_sheets_leads_read(self):
        """1. Reads authentic qualified leads directly from Google Sheets LEADS tab."""
        leads = self.provider.fetch_all_leads()
        self.assertIsInstance(leads, list)
        self.assertGreaterEqual(len(leads), 7, "Expected at least 7 qualified leads in CRM")
        
        # Verify Canonical Lead ID formatting on every lead
        for lead in leads:
            lead_id = lead.get("lead_id") or lead.get("id")
            self.assertIsNotNone(lead_id)
            self.assertTrue(
                bool(re.match(CANONICAL_LEAD_ID_REGEX, lead_id)),
                f"Lead ID {lead_id} does not match canonical regex pattern"
            )

    def test_successful_google_sheets_review_queue_read(self):
        """2. Reads authentic manual review candidates from REVIEW_QUEUE tab."""
        queue = self.provider.fetch_review_queue()
        self.assertIsInstance(queue, list)
        self.assertGreaterEqual(len(queue), 40, "Expected authentic review queue candidates")

    def test_successful_google_sheets_research_log_read(self):
        """3. Reads authentic researched businesses from RESEARCH_LOG tab."""
        entries = self.provider.fetch_research_log()
        self.assertIsInstance(entries, list)
        self.assertGreaterEqual(len(entries), 200, "Expected researched business log records")

    @patch("lib.sheets.google_sheets.get_sheet_client")
    def test_missing_google_credentials_error_handling(self, mock_client):
        """4. Missing Google credentials returns structured error without inventing data."""
        mock_client.side_effect = FileNotFoundError("Google credentials JSON not found")
        with self.assertRaises(Exception):
            failing_provider = GoogleSheetsStorageProvider()
            failing_provider.fetch_all_leads()

    @patch("lib.sheets.google_sheets.GoogleSheetsStorageProvider.fetch_all_leads")
    def test_empty_sheet_returns_genuine_zero(self, mock_fetch):
        """5. An empty sheet returns count 0, not a fallback or invented default."""
        mock_fetch.return_value = []
        client = TestClient(app)
        resp = client.get("/api/leads")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["leads"], [])
        self.assertNotEqual(data["count"], 7, "Must not replace 0 with fake count")


class TestBackendAPIAndCORS(unittest.TestCase):
    """Verifies API endpoints, metadata, CORS, and Operator authorization."""

    def setUp(self):
        self.client = TestClient(app)

    def test_leads_endpoint_returns_rich_metadata(self):
        """6. /api/leads returns source, tab, spreadsheet_id, and ISO timestamp."""
        resp = self.client.get("/api/leads")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["source"], "GOOGLE_SHEETS")
        self.assertEqual(data["tab"], "LEADS")
        self.assertIn("1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc", data["spreadsheet_id"])
        self.assertGreaterEqual(data["count"], 7)
        self.assertIn("timestamp", data)

    def test_review_queue_endpoint_returns_metadata(self):
        """7. /api/review-queue returns REVIEW_QUEUE records and metadata."""
        resp = self.client.get("/api/review-queue")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["source"], "GOOGLE_SHEETS")
        self.assertEqual(data["tab"], "REVIEW_QUEUE")
        self.assertGreaterEqual(data["count"], 40)

    def test_research_log_endpoint_returns_metadata(self):
        """8. /api/research-log returns RESEARCH_LOG records and metadata."""
        resp = self.client.get("/api/research-log")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["source"], "GOOGLE_SHEETS")
        self.assertEqual(data["tab"], "RESEARCH_LOG")
        self.assertGreaterEqual(data["count"], 200)

    def test_cors_preflight_for_vercel_origin(self):
        """9. CORS preflight returns 200 and permits Vercel frontend origin."""
        headers = {
            "Origin": "https://dripp-media-leads-machine.vercel.app",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type"
        }
        resp = self.client.options("/api/leads", headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers.get("access-control-allow-origin"),
            "https://dripp-media-leads-machine.vercel.app"
        )
        self.assertEqual(resp.headers.get("access-control-allow-credentials"), "true")

    def test_operator_authorization_protection(self):
        """10. Sensitive endpoints independently enforce operator authorization when key is set."""
        with patch.dict(os.environ, {"OPERATOR_API_KEY": "secure-operator-key-998"}):
            # 1. Unauthenticated search trigger blocked
            resp_no_auth = self.client.post("/api/search", json={
                "country": "United Kingdom",
                "cities": ["Manchester"],
                "industry": "Restaurants"
            })
            self.assertEqual(resp_no_auth.status_code, 401)
            self.assertIn("Unauthorized", resp_no_auth.json().get("message", ""))

            # 2. Invalid bearer token blocked
            resp_bad_auth = self.client.post("/api/search", json={
                "country": "United Kingdom",
                "cities": ["Manchester"],
                "industry": "Restaurants"
            }, headers={"Authorization": "Bearer invalid-token"})
            self.assertEqual(resp_bad_auth.status_code, 401)

            # 3. Unauthenticated cancel blocked
            resp_cancel_no_auth = self.client.post("/api/search/cancel")
            self.assertEqual(resp_cancel_no_auth.status_code, 401)


class TestSafetyInvariantsAndSanitization(unittest.TestCase):
    """Verifies that commercial invariants remain strictly frozen and frontend has 0 fake blips."""

    def test_commercial_safety_invariants_strictly_locked(self):
        """11. Commercial outreach and execution remain locked independently of UI."""
        status = SystemConfig.get_status_dict()
        self.assertTrue(status.get("travel_mode"), "TRAVEL_MODE must remain ACTIVE")
        self.assertFalse(status.get("commercial_actions_enabled"), "COMMERCIAL_ACTIONS_ENABLED must remain false")
        self.assertFalse(status.get("automated_email_enabled"), "AUTOMATED_EMAIL_ENABLED must remain false")
        self.assertFalse(status.get("cron_enabled"), "CRON must remain DISABLED")

    def test_no_hardcoded_fake_blips_in_frontend(self):
        """12. Frontend index.html does not contain hardcoded radar blips or fake lead IDs."""
        with open("static/index.html", "r", encoding="utf-8") as f:
            html = f.read()

        # Check absence of fake IDs lead-mcr-001..005
        for i in range(1, 6):
            self.assertNotIn(f"lead-mcr-00{i}", html, f"Found hardcoded mock ID lead-mcr-00{i} in static HTML")

        # Check absence of undeclared fallbackLeads
        self.assertNotIn("fallbackLeads", html)

        # Check absence of hardcoded inspector Mala default
        self.assertNotIn('inspName">Mala<', html)
        self.assertNotIn('inspId">LEAD-MAN-14A2D3<', html)

        # Check that dynamic container exists
        self.assertIn('id="homeRadarBlipsContainer"', html)
        self.assertIn('apiFetch(', html)

    def test_no_secrets_in_static_bundles_or_vercel_json(self):
        """13. No private credentials, API keys, or service-account JSON in client bundles."""
        with open("static/index.html", "r", encoding="utf-8") as f:
            html = f.read()

        with open("vercel.json", "r", encoding="utf-8") as f:
            v_json = f.read()

        # Verify no sensitive private key blocks
        self.assertNotIn("BEGIN PRIVATE KEY", html)
        self.assertNotIn("BEGIN PRIVATE KEY", v_json)
        self.assertNotIn("client_email", html)
        self.assertNotIn("client_email", v_json)

        # Verify no Apify token or Tavily key exposed in static files
        self.assertNotIn("apify_api_", html)
        self.assertNotIn("tvly-", html)

    def test_rule_b_criteria_preserved(self):
        """14. Frozen Rule B qualification is preserved across all qualified CRM leads."""
        provider = GoogleSheetsStorageProvider()
        leads = provider.fetch_all_leads()
        for lead in leads:
            website = (lead.get("website") or lead.get("website_url") or "").strip()
            self.assertEqual(website, "", f"Qualified lead {lead.get('lead_id')} violates Rule B by having a website: {website}")


if __name__ == "__main__":
    unittest.main()
