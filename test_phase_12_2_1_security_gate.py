#!/usr/bin/env python3
"""
test_phase_12_2_1_security_gate.py
==================================
PHASE 12.2.1 — FINAL SECURITY GATE BEFORE BACKEND DEPLOYMENT

Proves the complete security boundary:
1. Unauthenticated requests cannot read private CRM data (leads, review queue, research log, crm status, system config, jobs, readiness).
2. Authenticated operator sessions can access protected resources via:
   - Server-managed HttpOnly Secure dripp_session cookie
   - Authorization: Bearer <session_token>
   - X-Session-Token header
   - Direct administrative key in Authorization / X-Operator-Key
3. Expired and revoked sessions are rejected (immediate 401 after logout and TTL expiry).
4. Public health endpoints expose no secrets, disk paths, or sensitive internal state without auth.
5. CSRF protections block cross-origin cookie-based state modifications from untrusted origins.
6. No permanent operator key or Google credential exists in the frontend bundle or browser storage.
7. Existing suppression, send history, lead identity, and Rule B protections remain intact.
"""

import os
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from server import (
    app,
    operator_sessions,
    create_operator_session,
    OPERATOR_SESSION_TTL_SECONDS
)
from lib.system.system_config import SystemConfig
from lib.sheets.google_sheets import GoogleSheetsStorageProvider
from lib.crm.identity_matcher import BusinessIdentityMatcher, IdentityMatchOutcome


class TestUnauthenticatedPrivateDataAccessBlocked(unittest.TestCase):
    """Proves unauthenticated requests cannot read private CRM data or internal config."""

    def setUp(self):
        self.client = TestClient(app)
        self.test_key = "sec-gate-operator-key-9988"

    def test_unauthenticated_crm_reads_blocked_with_401(self):
        """1. Private CRM endpoints reject unauthenticated access with 401 Unauthorized."""
        with patch.dict(os.environ, {"OPERATOR_KEY": self.test_key}):
            private_get_endpoints = [
                "/api/leads",
                "/api/review-queue",
                "/api/research-log",
                "/api/crm/status",
                "/api/system/config",
                "/api/system/jobs",
                "/api/system/readiness",
                "/api/system/freshness-summary",
                "/api/status",
                "/api/discovery/status",
                "/api/production/markets",
                "/api/production/runs/latest",
                "/api/production/scorecard",
            ]

            for endpoint in private_get_endpoints:
                resp = self.client.get(endpoint)
                self.assertEqual(
                    resp.status_code, 401,
                    f"Private endpoint {endpoint} must return 401 when unauthenticated"
                )
                data = resp.json()
                self.assertEqual(data.get("status"), "error")
                self.assertIn("Unauthorized", data.get("message", ""))

    def test_unauthenticated_state_changing_actions_blocked(self):
        """2. Unauthenticated POST/PUT/DELETE endpoints return 401 Unauthorized."""
        with patch.dict(os.environ, {"OPERATOR_KEY": self.test_key}):
            state_changing_endpoints = [
                ("/api/search", {"country": "United Kingdom", "cities": ["Manchester"], "industry": "Restaurants"}),
                ("/api/search/cancel", None),
                ("/api/system/mode", {"travel_mode": True}),
                ("/api/system/backup", None),
                ("/api/system/reconcile", None),
                ("/api/system/jobs/run", {"job_type": "RUN_BACKUP"}),
            ]

            for endpoint, payload in state_changing_endpoints:
                if payload is not None:
                    resp = self.client.post(endpoint, json=payload)
                else:
                    resp = self.client.post(endpoint)
                self.assertEqual(
                    resp.status_code, 401,
                    f"State-changing endpoint {endpoint} must return 401 without auth"
                )


class TestAuthenticatedOperatorAccessModes(unittest.TestCase):
    """Proves authenticated operator sessions can access resources via cookies and headers."""

    def setUp(self):
        self.client = TestClient(app)
        self.test_key = "gatekeeper-master-key-7733"

    @patch("server.GoogleSheetsStorageProvider")
    def test_cookie_based_authentication(self, mock_storage_cls):
        """3. Server-managed HttpOnly dripp_session cookie authenticates private CRM reads."""
        mock_instance = MagicMock()
        mock_instance.fetch_all_leads.return_value = [{"company_name": "Test Company", "qualification_state": "OUTREACH_READY"}]
        mock_instance.fetch_review_queue.return_value = []
        mock_instance.fetch_research_log.return_value = []
        mock_instance.sheet_url = "https://docs.google.com/spreadsheets/d/test"
        mock_storage_cls.return_value = mock_instance

        with patch.dict(os.environ, {"OPERATOR_KEY": self.test_key}):
            # Exchange key for session
            login_resp = self.client.post("/api/operator/login", json={"operator_key": self.test_key})
            self.assertEqual(login_resp.status_code, 200)
            token = login_resp.json()["session_token"]
            self.assertTrue(token.startswith("dop_"))

            # Verify cookie is set on login response
            self.assertIn("dripp_session", login_resp.cookies)
            cookie_val = login_resp.cookies["dripp_session"]
            self.assertEqual(cookie_val, token)

            # Access protected CRM endpoints using purely the cookie (no Authorization header)
            cookies = {"dripp_session": token}
            leads_resp = self.client.get("/api/leads", cookies=cookies)
            self.assertEqual(leads_resp.status_code, 200)
            self.assertEqual(leads_resp.json()["status"], "ok")

            crm_status_resp = self.client.get("/api/crm/status", cookies=cookies)
            self.assertEqual(crm_status_resp.status_code, 200)
            self.assertEqual(crm_status_resp.json()["status"], "ok")

    @patch("server.GoogleSheetsStorageProvider")
    def test_bearer_and_custom_header_authentication(self, mock_storage_cls):
        """4. Bearer session token and custom headers permit authorized access."""
        mock_instance = MagicMock()
        mock_instance.fetch_all_leads.return_value = [{"company_name": "Test Company", "qualification_state": "OUTREACH_READY"}]
        mock_storage_cls.return_value = mock_instance

        with patch.dict(os.environ, {"OPERATOR_KEY": self.test_key}):
            # Generate session
            login_resp = self.client.post("/api/operator/login", json={"operator_key": self.test_key})
            token = login_resp.json()["session_token"]

            # Access via Authorization: Bearer
            bearer_resp = self.client.get("/api/leads", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(bearer_resp.status_code, 200)

            # Access via X-Session-Token
            hdr_resp = self.client.get("/api/leads", headers={"X-Session-Token": token})
            self.assertEqual(hdr_resp.status_code, 200)

            # Direct administrative master key
            admin_resp = self.client.get("/api/leads", headers={"Authorization": f"Bearer {self.test_key}"})
            self.assertEqual(admin_resp.status_code, 200)

            op_key_resp = self.client.get("/api/leads", headers={"X-Operator-Key": self.test_key})
            self.assertEqual(op_key_resp.status_code, 200)

    def test_incorrect_credentials_strictly_rejected(self):
        """5. Invalid credentials return 401 across all channels."""
        with patch.dict(os.environ, {"OPERATOR_KEY": self.test_key}):
            bad_login = self.client.post("/api/operator/login", json={"operator_key": "wrong-secret"})
            self.assertEqual(bad_login.status_code, 401)

            bad_bearer = self.client.get("/api/leads", headers={"Authorization": "Bearer fake-token-123"})
            self.assertEqual(bad_bearer.status_code, 401)

            bad_cookie = self.client.get("/api/leads", cookies={"dripp_session": "dop_non_existent"})
            self.assertEqual(bad_cookie.status_code, 401)


class TestSessionRevocationAndExpiry(unittest.TestCase):
    """Proves expired and revoked sessions are immediately rejected."""

    def setUp(self):
        self.client = TestClient(app)
        self.test_key = "gatekeeper-session-test-key-5544"

    def test_logout_revokes_session_and_clears_cookie(self):
        """6. Logout invalidates session token and instructs browser to delete cookie."""
        with patch.dict(os.environ, {"OPERATOR_KEY": self.test_key}):
            login_resp = self.client.post("/api/operator/login", json={"operator_key": self.test_key})
            token = login_resp.json()["session_token"]

            # Verify active session works
            active_resp = self.client.get("/api/operator/session", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(active_resp.status_code, 200)
            self.assertTrue(active_resp.json()["authenticated"])

            # Call logout
            logout_resp = self.client.post(
                "/api/operator/logout",
                headers={"Authorization": f"Bearer {token}"},
                cookies={"dripp_session": token}
            )
            self.assertEqual(logout_resp.status_code, 200)
            self.assertFalse(logout_resp.json()["authenticated"])

            # Verify token removed from server memory
            self.assertNotIn(token, operator_sessions)

            # Subsequent calls with revoked token return 401
            subsequent_resp = self.client.get("/api/leads", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(subsequent_resp.status_code, 401)

    def test_expired_session_is_rejected(self):
        """7. Sessions past their TTL are automatically pruned and rejected with 401."""
        with patch.dict(os.environ, {"OPERATOR_KEY": self.test_key}):
            login_resp = self.client.post("/api/operator/login", json={"operator_key": self.test_key})
            token = login_resp.json()["session_token"]

            # Force session expiry by rolling back expires_at
            operator_sessions[token]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=10)

            # Call protected endpoint
            expired_resp = self.client.get("/api/leads", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(expired_resp.status_code, 401)
            self.assertNotIn(token, operator_sessions)


class TestPublicHealthSanitizationAndCSRF(unittest.TestCase):
    """Proves public health sanitization and CSRF validation."""

    def setUp(self):
        self.client = TestClient(app)
        self.test_key = "sanitization-key-1122"

    def test_public_health_endpoint_exposes_no_internal_data(self):
        """8. Unauthenticated /api/system/health returns minimal status with 0 private info."""
        with patch.dict(os.environ, {"OPERATOR_KEY": self.test_key}):
            resp = self.client.get("/api/system/health")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()

            # Liveness indicators present
            self.assertEqual(data.get("status"), "ok")
            self.assertEqual(data.get("canonical_health_status"), "OPERATIONAL")
            self.assertFalse(data.get("authenticated"))

            # Sensitive internals absent
            self.assertNotIn("crm_connected", data)
            self.assertNotIn("subsystems", data)
            self.assertNotIn("storage_health", data)
            self.assertNotIn("quota", data)

            # No filesystem paths in payload
            payload_str = json.dumps(data)
            self.assertNotIn("/Users/", payload_str)
            self.assertNotIn("/app/", payload_str)
            self.assertNotIn(".json", payload_str)

    def test_authenticated_health_endpoint_returns_full_telemetry(self):
        """9. Authenticated operator receives full diagnostic health telemetry."""
        with patch.dict(os.environ, {"OPERATOR_KEY": self.test_key}):
            resp = self.client.get("/api/system/health", headers={"Authorization": f"Bearer {self.test_key}"})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("authenticated"))
            self.assertIn("scheduler_status", data)
            self.assertIn("quota_status", data)

    def test_csrf_origin_validation_for_cookie_auth(self):
        """10. Cross-origin cookie state mutation from untrusted origin is rejected with 403."""
        with patch.dict(os.environ, {"OPERATOR_KEY": self.test_key}):
            # Create valid session
            login_resp = self.client.post("/api/operator/login", json={"operator_key": self.test_key})
            token = login_resp.json()["session_token"]

            # Attacker origin submitting state-changing POST with cookie only (no custom header)
            attacker_resp = self.client.post(
                "/api/system/backup",
                cookies={"dripp_session": token},
                headers={"Origin": "https://malicious-attacker.com"}
            )
            self.assertEqual(attacker_resp.status_code, 403)
            self.assertIn("CSRF", attacker_resp.json().get("detail", ""))

            # Legitimate Vercel origin with cookie
            legit_resp = self.client.post(
                "/api/system/backup",
                cookies={"dripp_session": token},
                headers={"Origin": "https://dripp-media-leads-machine.vercel.app"}
            )
            self.assertEqual(legit_resp.status_code, 200)

            # Legitimate AJAX call with custom X-Requested-With header
            ajax_resp = self.client.post(
                "/api/system/backup",
                cookies={"dripp_session": token},
                headers={"X-Requested-With": "XMLHttpRequest"}
            )
            self.assertEqual(ajax_resp.status_code, 200)


class TestFrontendBundlesAndInvariants(unittest.TestCase):
    """Proves frontend bundle cleanliness and preservation of safety invariants."""

    def test_no_permanent_keys_or_google_credentials_in_bundle(self):
        """11. Frontend static bundle contains no permanent operator key or Google credentials."""
        with open("static/index.html", "r", encoding="utf-8") as f:
            html = f.read()

        # No Google Private Keys or service account email
        self.assertNotIn("BEGIN PRIVATE KEY", html)
        self.assertNotIn("private_key_id", html)
        self.assertNotIn("iam.gserviceaccount.com", html)

        # No permanent operator keys
        self.assertNotIn("dop_master", html)
        self.assertNotIn("sec-gate-operator", html)

        # Storage hygiene verified
        self.assertIn("localStorage.removeItem('dripp_operator_key')", html)
        self.assertIn("localStorage.removeItem('OPERATOR_KEY')", html)
        self.assertIn("sessionStorage.getItem('dripp_operator_session_token')", html)

    def test_existing_suppression_and_send_history_preserved(self):
        """12. Historical leads (Seoul Kimchi SENT, Hong Thai BOUNCED/SUPPRESSED) remain frozen."""
        provider = GoogleSheetsStorageProvider()
        leads = provider.fetch_all_leads()
        lead_dict = {l.get("company_name", "").strip().lower(): l for l in leads}

        self.assertIn("seoul kimchi", lead_dict)
        self.assertEqual(lead_dict["seoul kimchi"].get("outreach_status"), "SENT")

        self.assertIn("hong thai", lead_dict)
        self.assertIn(
            lead_dict["hong thai"].get("outreach_status"),
            ["SUPPRESSED", "SUPPRESSION", "BOUNCED", "NOT_READY"]
        )

    def test_rule_b_and_commercial_safety_locks_frozen(self):
        """13. Rule B has 8 criteria and all safety locks remain active."""
        from lib.validation.rule_b_criteria import (
            get_canonical_rule_b_criteria,
            get_canonical_rule_b_criteria_count,
        )

        self.assertEqual(get_canonical_rule_b_criteria_count(), 8)
        self.assertEqual(len(get_canonical_rule_b_criteria()), 8)

        cfg = SystemConfig.get_status_dict()
        self.assertTrue(cfg.get("travel_mode"), "TRAVEL_MODE must be ACTIVE")
        self.assertFalse(cfg.get("commercial_actions_enabled"), "COMMERCIAL_ACTIONS_ENABLED must be false")
        self.assertIsInstance(cfg.get("automated_email_enabled"), bool)
        self.assertFalse(cfg.get("cron_enabled"), "CRON must be DISABLED")


if __name__ == "__main__":
    unittest.main()
