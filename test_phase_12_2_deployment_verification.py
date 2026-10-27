"""
test_phase_12_2_deployment_verification.py
=========================================
Automated Test Suite for Phase 12.2 — Deployment Verification, Backend Hosting & Credential Security.

Verifies:
  1. Missing backend URL in production (no silent fallback to localhost/fake data).
  2. Backend unavailable or timeout error surfacing.
  3. Incorrect operator credentials handling.
  4. Authorized and unauthorized access to state-modifying protected endpoints.
  5. CORS enforcement from approved (Vercel) vs unapproved origins (no credentialed wildcard).
  6. Google Sheets permission failure and quota/timeout truthful error handling.
  7. Empty-but-valid spreadsheet tab handling (genuine 0 distinct from error).
  8. Live data response metadata, timestamps, and tab status.
  9. Prevention of mock-data fallback across backend and frontend.
 10. Authentication & secret non-exposure in frontend bundles and browser storage.
 11. Truthful separation of backend health vs Google Sheets CRM connectivity.
 12. Existing travel-mode, campaign, email, and cron restrictions.
 13. Preservation of historical lead records (Seoul Kimchi SENT, Hong Thai SUPPRESSED) and duplicate prevention.
"""

import os
import re
import json
import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from server import app, operator_sessions, create_operator_session, validate_session_token
from lib.sheets.google_sheets import GoogleSheetsStorageProvider
from lib.system.system_config import SystemConfig
from lib.crm.identity_matcher import BusinessIdentityMatcher, IdentityMatchOutcome


class TestPhase12DeploymentVerification(unittest.TestCase):
    """Phase 12.2 automated test suite."""

    def setUp(self):
        self.client = TestClient(app)
        # Clear test sessions before each run
        operator_sessions.clear()
        session = create_operator_session()
        self.auth_headers = {"Authorization": f"Bearer {session['session_token']}"}

    # -------------------------------------------------------------------------
    # 1. Missing Backend URL in Production
    # -------------------------------------------------------------------------
    def test_missing_backend_url_in_production(self):
        """1. Remote frontend configuration yields unconfigured state, never silent localhost."""
        with open("static/index.html", "r", encoding="utf-8") as f:
            html = f.read()

        # Verify DrippConfig.getBackendUrl returns null on remote unconfigured origins
        self.assertIn("return null;", html)

        # Verify apiFetch detects missing URL and throws clear unconfigured error
        self.assertIn("Backend Gateway Unconfigured", html)

    # -------------------------------------------------------------------------
    # 2. Backend Unavailable or Timeout
    # -------------------------------------------------------------------------
    def test_backend_unavailable_or_timeout_surfacing(self):
        """2. Timeout and network errors are surfaced accurately with AbortSignal."""
        with open("static/index.html", "r", encoding="utf-8") as f:
            html = f.read()

        # Check AbortSignal.timeout handling in apiFetch
        self.assertIn("AbortSignal.timeout(timeoutMs)", html)
        self.assertIn("Timeout: Backend failed to respond", html)
        self.assertIn("updateBackendConnectionStatus(false", html)

    # -------------------------------------------------------------------------
    # 3. Incorrect Operator Credentials
    # -------------------------------------------------------------------------
    def test_incorrect_operator_credentials_rejected(self):
        """3. Invalid operator credentials return HTTP 401 with structured error."""
        with patch.dict(os.environ, {"OPERATOR_KEY": "master-secret-key-12345"}):
            # Test empty or invalid key
            resp_empty = self.client.post("/api/operator/login", json={"operator_key": ""})
            self.assertEqual(resp_empty.status_code, 401)
            self.assertEqual(resp_empty.json()["detail"], "Operator key required")

            resp_wrong = self.client.post("/api/operator/login", json={"operator_key": "wrong-key-attempt"})
            self.assertEqual(resp_wrong.status_code, 401)
            self.assertEqual(resp_wrong.json()["detail"], "Invalid operator key")

    # -------------------------------------------------------------------------
    # 4. Authorized and Unauthorized Access to Protected Endpoints
    # -------------------------------------------------------------------------
    def test_protected_endpoints_access_control(self):
        """4. State-modifying endpoints reject unauthorized access and accept valid sessions."""
        test_key = "master-secret-key-12345"
        with patch.dict(os.environ, {"OPERATOR_KEY": test_key}):
            # Attempt state modification without token -> must be 401
            resp_unauth = self.client.post("/api/search", json={
                "country": "United Kingdom",
                "cities": ["Manchester"],
                "industry": "Restaurants"
            })
            self.assertEqual(resp_unauth.status_code, 401)
            self.assertIn("Operator authentication required", resp_unauth.json()["detail"])

            # Login to acquire short-lived session token
            login_resp = self.client.post("/api/operator/login", json={"operator_key": test_key})
            self.assertEqual(login_resp.status_code, 200)
            token = login_resp.json()["session_token"]
            self.assertTrue(token.startswith("dop_"))

            # Session check endpoint with valid token
            session_resp = self.client.get("/api/operator/session", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(session_resp.status_code, 200)
            self.assertTrue(session_resp.json()["authenticated"])

            # Check that state-modifying action succeeds with valid session (mock run_pipeline_task to prevent live discovery)
            with patch("server.run_pipeline_task") as mock_pipeline:
                mock_pipeline.return_value = None
                resp_auth = self.client.post(
                    "/api/search",
                    json={"country": "United Kingdom", "cities": ["Manchester"], "industry": "Restaurants"},
                    headers={"Authorization": f"Bearer {token}"}
                )
                self.assertIn(resp_auth.status_code, [200, 202])

            # Read-only endpoints remain accessible without auth
            health_resp = self.client.get("/api/system/health")
            self.assertEqual(health_resp.status_code, 200)

            # Public webhook/unsubscribe endpoints remain accessible without operator session
            unsub_resp = self.client.post("/api/email/unsubscribe", json={"email": "test@example.com"})
            self.assertEqual(unsub_resp.status_code, 200)

            # Logout revokes the session
            logout_resp = self.client.post("/api/operator/logout", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(logout_resp.status_code, 200)
            self.assertFalse(logout_resp.json()["authenticated"])

            # Subsequent check with revoked token returns 401
            revoked_resp = self.client.get("/api/operator/session", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(revoked_resp.status_code, 401)

    # -------------------------------------------------------------------------
    # 5. CORS from Approved and Unapproved Origins
    # -------------------------------------------------------------------------
    def test_cors_origin_enforcement(self):
        """5. CORS permits production Vercel origin and disallows wildcard with credentials."""
        # Approved origin
        resp_approved = self.client.get("/api/system/health", headers={"Origin": "https://dripp-media-leads-machine.vercel.app"})
        self.assertEqual(resp_approved.status_code, 200)
        self.assertEqual(resp_approved.headers.get("access-control-allow-origin"), "https://dripp-media-leads-machine.vercel.app")
        self.assertEqual(resp_approved.headers.get("access-control-allow-credentials"), "true")

        # Unapproved origin
        resp_unapproved = self.client.get("/api/system/health", headers={"Origin": "https://unauthorized-attacker.example.com"})
        self.assertEqual(resp_unapproved.status_code, 200)
        self.assertNotEqual(resp_unapproved.headers.get("access-control-allow-origin"), "https://unauthorized-attacker.example.com")
        self.assertNotEqual(resp_unapproved.headers.get("access-control-allow-origin"), "*")

    # -------------------------------------------------------------------------
    # 6. Google Sheets Permission Failure and Quota/Timeout Responses
    # -------------------------------------------------------------------------
    @patch("server.GoogleSheetsStorageProvider")
    def test_google_sheets_error_resilience_and_truthful_failure(self, mock_provider_class):
        """6. CRM failures result in truthful error responses without crashing or mocking."""
        mock_instance = MagicMock()
        mock_instance.fetch_all_leads.side_effect = Exception("Google Sheets API 429: Rate Limit Exceeded")
        mock_provider_class.return_value = mock_instance

        resp = self.client.get("/api/crm/status", headers=self.auth_headers)
        self.assertEqual(resp.status_code, 503)
        data = resp.json()
        self.assertEqual(data["status"], "error")
        self.assertFalse(data["crm_connected"])
        self.assertIn("Rate Limit Exceeded", data["error"])
        self.assertEqual(data["tabs"]["LEADS"]["count"], 0)
        self.assertEqual(data["tabs"]["LEADS"]["status"], "ERROR")

    # -------------------------------------------------------------------------
    # 7. Empty-but-Valid Spreadsheet Tabs
    # -------------------------------------------------------------------------
    @patch("server.GoogleSheetsStorageProvider")
    def test_empty_spreadsheet_tabs_handled_genuinely(self, mock_provider_class):
        """7. Empty sheet tabs return count 0 without being treated as connection failure."""
        mock_instance = MagicMock()
        mock_instance.fetch_all_leads.return_value = []
        mock_instance.fetch_review_queue.return_value = []
        mock_instance.fetch_research_log.return_value = []
        mock_instance.sheet_url = "https://docs.google.com/spreadsheets/d/1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc"
        mock_provider_class.return_value = mock_instance

        resp = self.client.get("/api/crm/status", headers=self.auth_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["crm_connected"])
        self.assertEqual(data["tabs"]["LEADS"]["count"], 0)
        self.assertEqual(data["tabs"]["LEADS"]["status"], "CONNECTED")
        self.assertEqual(data["tabs"]["REVIEW_QUEUE"]["count"], 0)
        self.assertEqual(data["tabs"]["RESEARCH_LOG"]["count"], 0)

    # -------------------------------------------------------------------------
    # 8. Live Data Response Metadata and Timestamps
    # -------------------------------------------------------------------------
    def test_live_data_response_metadata_and_timestamps(self):
        """8. CRM status and leads endpoints supply ISO timestamps and spreadsheet metadata."""
        resp_status = self.client.get("/api/crm/status", headers=self.auth_headers)
        self.assertEqual(resp_status.status_code, 200)
        data = resp_status.json()
        self.assertIn("timestamp", data)
        self.assertIn("spreadsheet_id", data)
        self.assertEqual(data["tabs"]["LEADS"]["sheet_name"], "Dripp Media Leads CRM")

        resp_leads = self.client.get("/api/leads", headers=self.auth_headers)
        self.assertEqual(resp_leads.status_code, 200)
        lead_data = resp_leads.json()
        self.assertIn("timestamp", lead_data)
        self.assertEqual(lead_data["source"], "GOOGLE_SHEETS")

    # -------------------------------------------------------------------------
    # 9. Prevention of Mock-Data Fallback
    # -------------------------------------------------------------------------
    def test_prevention_of_mock_data_fallback(self):
        """9. Neither backend nor frontend silently falls back to synthetic mock records."""
        with open("static/index.html", "r", encoding="utf-8") as f:
            html = f.read()

        # Frontend must not have mock lists
        self.assertNotIn("fallbackLeads", html)
        self.assertNotIn("mock_candidates", html)

        # In case of loadLeadsData error, UI renders disconnected state rather than fake rows
        self.assertIn("Backend Gateway Unconfigured", html)
        self.assertIn("Backend Connection Offline", html)

    # -------------------------------------------------------------------------
    # 10. Authentication and Secret Non-Exposure
    # -------------------------------------------------------------------------
    def test_secret_non_exposure_and_storage_purging(self):
        """10. Frontend does not expose private keys and purges legacy localStorage tokens."""
        with open("static/index.html", "r", encoding="utf-8") as f:
            html = f.read()

        # No private keys or service account credentials in frontend
        self.assertNotIn("BEGIN PRIVATE KEY", html)
        self.assertNotIn("client_email", html)
        self.assertNotIn("private_key_id", html)

        # Frontend contains active migration logic purging permanent operator key from localStorage
        self.assertIn("clearPermanentOperatorSecrets", html)
        self.assertIn("localStorage.removeItem('dripp_operator_key')", html)

        # Frontend uses sessionStorage for session token
        self.assertIn("sessionStorage.getItem('dripp_operator_session_token')", html)

    # -------------------------------------------------------------------------
    # 11. Truthful Separation of Backend Health and CRM Status
    # -------------------------------------------------------------------------
    def test_truthful_separation_of_health_and_crm(self):
        """11. Backend health does not assert CRM status; both are tested independently."""
        # /api/system/health returns canonical system operational state
        resp_health = self.client.get("/api/system/health")
        self.assertEqual(resp_health.status_code, 200)
        health_data = resp_health.json()
        self.assertIn("canonical_health_status", health_data)
        self.assertNotIn("crm_connected", health_data, "Health endpoint must not claim CRM status")

        # Frontend probe tests both endpoints independently
        with open("static/index.html", "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("fetch(`${baseUrl}/api/system/health`", html)
        self.assertIn("fetch(`${baseUrl}/api/crm/status`", html)
        self.assertIn("updateCrmStatusIndicator(crmConnected", html)

    # -------------------------------------------------------------------------
    # 12. Existing Travel-Mode, Campaign, Email, and Cron Restrictions
    # -------------------------------------------------------------------------
    def test_immutable_commercial_safety_restrictions(self):
        """12. Strict safety locks remain frozen."""
        cfg = SystemConfig.get_status_dict()
        self.assertTrue(cfg.get("travel_mode"), "TRAVEL_MODE must be ACTIVE")
        self.assertFalse(cfg.get("commercial_actions_enabled"), "COMMERCIAL_ACTIONS_ENABLED must be false")
        self.assertIsInstance(cfg.get("automated_email_enabled"), bool)
        self.assertFalse(cfg.get("cron_enabled"), "CRON must be DISABLED")

    # -------------------------------------------------------------------------
    # 13. Preservation of Historical Leads and Duplicate Prevention
    # -------------------------------------------------------------------------
    def test_historical_leads_preservation_and_duplicate_prevention(self):
        """13. Historical CRM records (Seoul Kimchi SENT, Hong Thai SUPPRESSED) are preserved."""
        provider = GoogleSheetsStorageProvider()
        leads = provider.fetch_all_leads()
        lead_dict = {l.get("company_name", "").strip().lower(): l for l in leads}

        # Verify Seoul Kimchi preserves SENT outreach status
        self.assertIn("seoul kimchi", lead_dict, "Seoul Kimchi must exist in CRM")
        seoul_lead = lead_dict["seoul kimchi"]
        self.assertEqual(
            seoul_lead.get("outreach_status"), "SENT",
            f"Seoul Kimchi outreach status must be SENT, found: {seoul_lead.get('outreach_status')}"
        )

        # Verify Hong Thai suppression preservation
        self.assertIn("hong thai", lead_dict, "Hong Thai must exist in CRM")
        hong_thai = lead_dict["hong thai"]
        self.assertIn(
            hong_thai.get("outreach_status"), ["SUPPRESSED", "SUPPRESSION", "BOUNCED", "NOT_READY"],
            "Hong Thai must preserve suppression status"
        )

        # Verify BusinessIdentityMatcher identifies exact and fuzzy matches to prevent duplicates
        matcher = BusinessIdentityMatcher()
        match_result = matcher.match_candidate(
            {"company_name": "Seoul Kimchi", "city": "Manchester", "address": "275 Upper Brook St, Manchester M13 0HR"},
            leads
        )
        self.assertIn(
            match_result.outcome,
            [IdentityMatchOutcome.EXISTING_BUSINESS.value, IdentityMatchOutcome.POSSIBLE_DUPLICATE.value],
            "Identity matcher must identify existing lead to prevent duplicate CRM row"
        )


if __name__ == "__main__":
    unittest.main()
