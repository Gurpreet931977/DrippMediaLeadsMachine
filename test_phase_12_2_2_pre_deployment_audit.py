"""
test_phase_12_2_2_pre_deployment_audit.py
=========================================
Phase 12.2.2 - Final Pre-Deployment Audit Test Suite.

Verifies pre-deployment security requirements:
1. CORS lockdown: exact production origin trusted, arbitrary *.vercel.app rejected.
2. Inbound webhook authentication, HMAC-SHA256 verification, replay protection, and 503 disabled fallback.
3. Operator login sliding-window rate limiting (HTTP 429 after 5 failed attempts).
4. Google Service Account JSON direct in-memory environment variable parsing without disk leakage.
5. Preservation of travel mode, commercial lockout, and Rule B frozen criteria.
"""

import os
import json
import time
import hmac
import hashlib
import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from server import app, is_allowed_origin, login_failed_attempts
from sheets_sync import get_sheet_client
from lib.system.system_config import SystemConfig


class TestCORSOriginLockdown(unittest.TestCase):
    """Verifies that CORS origins are restricted to exact production origin and explicitly approved origins."""

    def setUp(self):
        self.client = TestClient(app)

    def test_production_vercel_origin_allowed(self):
        """1. Exact production origin https://dripp-media-leads-machine.vercel.app is allowed."""
        self.assertTrue(is_allowed_origin("https://dripp-media-leads-machine.vercel.app"))
        self.assertTrue(is_allowed_origin("https://dripp-media-leads-machine.vercel.app/"))

        headers = {
            "Origin": "https://dripp-media-leads-machine.vercel.app",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        }
        resp = self.client.options("/api/leads", headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers.get("access-control-allow-origin"),
            "https://dripp-media-leads-machine.vercel.app"
        )
        self.assertEqual(resp.headers.get("access-control-allow-credentials"), "true")

    def test_arbitrary_vercel_subdomain_rejected(self):
        """2. Blanket trust of *.vercel.app is eliminated; arbitrary subdomains are not allowed."""
        self.assertFalse(is_allowed_origin("https://attacker-app.vercel.app"))
        self.assertFalse(is_allowed_origin("https://evil-spoof.vercel.app"))
        self.assertFalse(is_allowed_origin("https://random-preview.vercel.app"))

        headers = {
            "Origin": "https://attacker-app.vercel.app",
            "Access-Control-Request-Method": "GET",
        }
        resp = self.client.options("/api/leads", headers=headers)
        # CORSMiddleware does not set access-control-allow-origin for unauthorized origins
        self.assertNotEqual(
            resp.headers.get("access-control-allow-origin"),
            "https://attacker-app.vercel.app"
        )

    def test_arbitrary_external_origins_rejected(self):
        """3. External untrusted origins are strictly rejected."""
        self.assertFalse(is_allowed_origin("https://malicious.com"))
        self.assertFalse(is_allowed_origin("http://attacker.local"))

    def test_localhost_dev_origins_allowed(self):
        """4. Standard local development origins are permitted."""
        self.assertTrue(is_allowed_origin("http://localhost:8000"))
        self.assertTrue(is_allowed_origin("http://127.0.0.1:8000"))
        self.assertTrue(is_allowed_origin("http://localhost:3000"))
        self.assertTrue(is_allowed_origin("http://127.0.0.1:3000"))


class TestInboundWebhookSecurity(unittest.TestCase):
    """Verifies cryptographic signature validation, replay defense, and safe failure behavior for inbound webhooks."""

    def setUp(self):
        self.client = TestClient(app)

    def test_webhook_returns_503_when_secret_not_configured(self):
        """5. Inbound webhook returns HTTP 503 (disabled) when INBOUND_WEBHOOK_SECRET is not set."""
        with patch.dict(os.environ, {"INBOUND_WEBHOOK_SECRET": "", "EMAIL_WEBHOOK_SECRET": ""}):
            # Test primary route
            resp1 = self.client.post("/api/email/inbound/reply-webhook", json={"lead_id": "LEAD-001"})
            self.assertEqual(resp1.status_code, 503)
            data1 = resp1.json()
            self.assertEqual(data1.get("status"), "disabled")
            self.assertIn("authentication secret not configured", data1.get("message", ""))

            # Test alias route
            resp2 = self.client.post("/api/webhook/inbound-reply", json={"lead_id": "LEAD-001"})
            self.assertEqual(resp2.status_code, 503)

    def test_webhook_rejects_unauthenticated_request_when_secret_is_set(self):
        """6. Inbound webhook rejects requests with no signature or secret with HTTP 401."""
        with patch.dict(os.environ, {"INBOUND_WEBHOOK_SECRET": "whsec_test_secret_key_42"}):
            resp = self.client.post(
                "/api/email/inbound/reply-webhook",
                json={"lead_id": "LEAD-001", "snippet": "I am interested"}
            )
            self.assertEqual(resp.status_code, 401)
            data = resp.json()
            self.assertEqual(data.get("status"), "unauthorized")

    def test_webhook_rejects_invalid_hmac_signature(self):
        """7. Inbound webhook rejects forged HMAC signature."""
        with patch.dict(os.environ, {"INBOUND_WEBHOOK_SECRET": "whsec_test_secret_key_42"}):
            headers = {
                "X-Webhook-Signature": "sha256=invaliddeadbeefsignature1234567890abcdef",
                "X-Webhook-Timestamp": str(time.time()),
            }
            resp = self.client.post(
                "/api/email/inbound/reply-webhook",
                json={"lead_id": "LEAD-001"},
                headers=headers
            )
            self.assertEqual(resp.status_code, 401)

    def test_webhook_rejects_replayed_or_expired_timestamp(self):
        """8. Inbound webhook rejects requests with timestamps drifted > 300 seconds (replay defense)."""
        secret = "whsec_test_secret_key_42"
        with patch.dict(os.environ, {"INBOUND_WEBHOOK_SECRET": secret}):
            payload = json.dumps({"lead_id": "LEAD-001"}).encode("utf-8")
            valid_sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

            # Timestamp from 10 minutes ago (expired)
            expired_ts = str(time.time() - 600)
            headers = {
                "X-Webhook-Signature": f"sha256={valid_sig}",
                "X-Webhook-Timestamp": expired_ts,
                "Content-Type": "application/json",
            }
            resp = self.client.post(
                "/api/email/inbound/reply-webhook",
                content=payload,
                headers=headers
            )
            self.assertEqual(resp.status_code, 401)
            self.assertIn("replay", resp.json().get("message", "").lower())

    def test_webhook_accepts_valid_hmac_signature(self):
        """9. Inbound webhook succeeds with valid cryptographic HMAC-SHA256 signature and fresh timestamp."""
        secret = "whsec_test_secret_key_42"
        with patch.dict(os.environ, {"INBOUND_WEBHOOK_SECRET": secret}):
            payload = json.dumps({
                "lead_id": "LEAD-MAN-TEST01",
                "message_id": "IN-MSG-1234",
                "provider": "TEST_WEBHOOK",
                "snippet": "Sounds good, let's schedule a call."
            }).encode("utf-8")
            valid_sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
            fresh_ts = str(time.time())

            headers = {
                "X-Webhook-Signature": f"sha256={valid_sig}",
                "X-Webhook-Timestamp": fresh_ts,
                "Content-Type": "application/json",
            }

            with patch("lib.outreach.automated_email_executor.AutomatedEmailExecutor.record_inbound_reply") as mock_record:
                mock_record.return_value = {
                    "status": "RECORDED",
                    "lead_id": "LEAD-MAN-TEST01",
                    "action_required": "OPERATOR_REVIEW"
                }
                resp = self.client.post(
                    "/api/email/inbound/reply-webhook",
                    content=payload,
                    headers=headers
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json().get("status"), "RECORDED")
                mock_record.assert_called_once()

    def test_webhook_accepts_valid_shared_secret_header(self):
        """10. Inbound webhook succeeds with valid X-Webhook-Secret header."""
        secret = "whsec_test_secret_key_42"
        with patch.dict(os.environ, {"INBOUND_WEBHOOK_SECRET": secret}):
            payload = json.dumps({"lead_id": "LEAD-MAN-TEST01"}).encode("utf-8")
            headers = {
                "X-Webhook-Secret": secret,
                "Content-Type": "application/json",
            }
            with patch("lib.outreach.automated_email_executor.AutomatedEmailExecutor.record_inbound_reply") as mock_record:
                mock_record.return_value = {"status": "RECORDED"}
                resp = self.client.post(
                    "/api/webhook/inbound-reply",
                    content=payload,
                    headers=headers
                )
                self.assertEqual(resp.status_code, 200)


class TestOperatorLoginRateLimiting(unittest.TestCase):
    """Verifies that brute force attacks against /api/operator/login are halted with HTTP 429."""

    def setUp(self):
        self.client = TestClient(app)
        login_failed_attempts.clear()

    def tearDown(self):
        login_failed_attempts.clear()

    def test_operator_login_rate_limiting_after_5_failures(self):
        """11. 5 consecutive failed logins trigger HTTP 429 rate limit lockout for the client IP."""
        test_ip = "192.168.1.105"
        with patch.dict(os.environ, {"OPERATOR_KEY": "super-secret-operator-key"}):
            headers = {"X-Forwarded-For": test_ip}

            # First 5 failed attempts return 401
            for i in range(5):
                resp = self.client.post(
                    "/api/operator/login",
                    json={"operator_key": f"wrong-key-{i}"},
                    headers=headers
                )
                self.assertEqual(resp.status_code, 401, f"Attempt {i+1} should return 401")

            # 6th attempt should be blocked with 429 Too Many Requests
            resp_blocked = self.client.post(
                "/api/operator/login",
                json={"operator_key": "wrong-key-6"},
                headers=headers
            )
            self.assertEqual(resp_blocked.status_code, 429)
            self.assertIn("Too many failed login attempts", resp_blocked.json().get("detail", ""))

            # Even a correct key is locked out during the rate limit window
            resp_correct_but_locked = self.client.post(
                "/api/operator/login",
                json={"operator_key": "super-secret-operator-key"},
                headers=headers
            )
            self.assertEqual(resp_correct_but_locked.status_code, 429)

            # Different IP is not affected
            resp_other_ip = self.client.post(
                "/api/operator/login",
                json={"operator_key": "super-secret-operator-key"},
                headers={"X-Forwarded-For": "192.168.1.200"}
            )
            self.assertEqual(resp_other_ip.status_code, 200)


class TestGoogleCredentialsEnvironmentParsing(unittest.TestCase):
    """Verifies that GOOGLE_SERVICE_ACCOUNT_JSON parses directly from environment without writing to disk."""

    def test_synthetic_service_account_fixture_initialization(self):
        """12. get_sheet_client successfully initializes using GOOGLE_SERVICE_ACCOUNT_JSON fixture."""
        synthetic_pk = "-----BEGIN " + "PRIVATE KEY-----\n" + "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC3\n" + "-----END " + "PRIVATE KEY-----\n"
        synthetic_sa = {
            "type": "service_" + "account",
            "project_id": "test-synthetic-project",
            "private_key_id": "synthetic12345",
            "private_key": synthetic_pk,
            "client_email": "synthetic-bot@test-synthetic-project.iam.gserviceaccount.com",
            "client_id": "100000000000000000001",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        sa_json_str = json.dumps(synthetic_sa)

        with patch.dict(os.environ, {
            "GOOGLE_SERVICE_ACCOUNT_JSON": sa_json_str,
            "GOOGLE_APPLICATION_CREDENTIALS": "",
        }):
            with patch("google.oauth2.service_account.Credentials.from_service_account_info") as mock_from_info, \
                 patch("gspread.authorize") as mock_authorize:
                mock_creds = MagicMock()
                mock_from_info.return_value = mock_creds
                mock_client = MagicMock()
                mock_authorize.return_value = mock_client

                client = get_sheet_client()

                self.assertIs(client, mock_client)
                mock_from_info.assert_called_once()
                call_arg = mock_from_info.call_args[0][0]
                self.assertEqual(call_arg["client_email"], "synthetic-bot@test-synthetic-project.iam.gserviceaccount.com")
                mock_authorize.assert_called_once_with(mock_creds)

    def test_malformed_service_account_json_raises_descriptive_error(self):
        """13. Malformed GOOGLE_SERVICE_ACCOUNT_JSON raises ValueError without swallowing error."""
        with patch.dict(os.environ, {
            "GOOGLE_SERVICE_ACCOUNT_JSON": "{invalid-json-content",
            "GOOGLE_APPLICATION_CREDENTIALS": "",
        }):
            with self.assertRaises(ValueError) as ctx:
                get_sheet_client()
            self.assertIn("Failed to initialize Google credentials", str(ctx.exception))

    def test_cloud_run_adc_scope_initialization(self):
        """13b. Cloud Run environment requests spreadsheets scope via ADC without service account keys."""
        with patch.dict(os.environ, {
            "GOOGLE_SERVICE_ACCOUNT_JSON": "",
            "GOOGLE_APPLICATION_CREDENTIALS": "",
            "CLOUD_RUN_JOB_NAME": "dripp-discovery-job",
        }, clear=False):
            with patch("google.auth.default") as mock_auth_default, \
                 patch("gspread.authorize") as mock_authorize:
                mock_creds = MagicMock()
                mock_auth_default.return_value = (mock_creds, "poised-eye-509816-a4")
                mock_client = MagicMock()
                mock_authorize.return_value = mock_client

                client = get_sheet_client()

                self.assertIs(client, mock_client)
                mock_auth_default.assert_called_once_with(scopes=["https://www.googleapis.com/auth/spreadsheets"])
                mock_authorize.assert_called_once_with(mock_creds)

    def test_cloud_run_adc_failure_raises_runtime_error_without_key_fallback(self):
        """13c. Cloud Run ADC failure raises descriptive RuntimeError and halts without disk key fallback."""
        with patch.dict(os.environ, {
            "GOOGLE_SERVICE_ACCOUNT_JSON": "",
            "GOOGLE_APPLICATION_CREDENTIALS": "",
            "CLOUD_RUN_JOB_NAME": "dripp-discovery-job",
        }, clear=False):
            with patch("google.auth.default", side_effect=Exception("Metadata server token request failed")):
                with self.assertRaises(RuntimeError) as ctx:
                    get_sheet_client()
                self.assertIn("Cloud Run Google Sheets ADC authorization failed", str(ctx.exception))
                self.assertIn("Metadata server token request failed", str(ctx.exception))

    def test_local_dev_fallback_when_adc_fails(self):
        """13d. Local development falls back to local dev credentials when ADC raises error."""
        with patch.dict(os.environ, {
            "GOOGLE_SERVICE_ACCOUNT_JSON": "",
            "GOOGLE_APPLICATION_CREDENTIALS": "",
            "CLOUD_RUN_JOB_NAME": "",
            "K_SERVICE": "",
            "CLOUD_RUN_JOB": "",
            "K_REVISION": "",
        }, clear=False):
            with patch("google.auth.default", side_effect=Exception("No ADC on Mac")), \
                 patch("os.path.exists", return_value=True), \
                 patch("google.oauth2.service_account.Credentials.from_service_account_file") as mock_from_file, \
                 patch("gspread.authorize") as mock_authorize:
                mock_creds = MagicMock()
                mock_from_file.return_value = mock_creds
                mock_client = MagicMock()
                mock_authorize.return_value = mock_client

                client = get_sheet_client()

                self.assertIs(client, mock_client)
                mock_from_file.assert_called_once()
                mock_authorize.assert_called_once_with(mock_creds)



class TestPreDeploymentSafetyInvariants(unittest.TestCase):
    """Verifies that all required commercial locks and qualification rules remain frozen."""

    def test_travel_mode_and_commercial_locks_strictly_frozen(self):
        """14. SystemConfig confirms TRAVEL_MODE=ACTIVE, COMMERCIAL_ACTIONS_ENABLED=false, etc."""
        status = SystemConfig.get_status_dict()
        self.assertTrue(status.get("travel_mode"), "TRAVEL_MODE must remain ACTIVE")
        self.assertFalse(status.get("commercial_actions_enabled"), "COMMERCIAL_ACTIONS_ENABLED must remain false")
        self.assertFalse(status.get("automated_email_enabled"), "AUTOMATED_EMAIL_ENABLED must remain false")
        self.assertFalse(status.get("cron_enabled"), "CRON must remain DISABLED")

    def test_rule_b_criteria_frozen_at_eight(self):
        """15. Rule B qualification criteria remains frozen at exactly 8 mandatory criteria."""
        from lib.validation.rule_b_criteria import (
            RULE_B_VERSION,
            RULE_B_CRITERIA,
            get_canonical_rule_b_criteria_count
        )
        self.assertEqual(RULE_B_VERSION, "FROZEN")
        self.assertEqual(get_canonical_rule_b_criteria_count(), 8)
        self.assertEqual(len(RULE_B_CRITERIA), 8)


if __name__ == "__main__":
    unittest.main()
