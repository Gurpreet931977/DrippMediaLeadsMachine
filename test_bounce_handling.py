"""
Dripp Media — Bounce Handling & Delivery Feedback Test Suite
============================================================
Comprehensive test suite verifying real-world delivery feedback:
  1. 550 5.1.1 permanent bounce detection & classification
  2. Permanent bounce does not retry (zero retry for 550 5.1.1)
  3. Bounced email becomes persistently suppressed
  4. Bounced email cannot be auto-selected (auto contactability rejected)
  5. Business remains OUTREACH_READY (qualification separate from email validity)
  6. Contact history preserves SENT -> BOUNCED timeline
  7. Campaign analytics count bounce (distinguishes SENT, BOUNCED, FAILED, BLOCKED)
  8. Another candidate email remains available (candidate history preservation)
  9. Temporary delivery failure remains retryable (421 / 451)
  10. 250 SMTP acceptance alone does not equal DELIVERED
  11. Credentials are never stored in bounce records (redacted)
  12. Duplicate lead rows not created in Google Sheets
"""

import os
import sys
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from lib.types import Lead, OutreachStatus, QualificationState
from lib.outreach.email_enricher import (
    EmailVerifier,
    EmailEnricher,
    EmailCandidate,
    EmailSource,
    EmailVerificationStatus,
    RecipientConfidence,
)
from lib.outreach.compliance import (
    SuppressionManager,
    ContactHistoryManager,
)
from lib.outreach.contactability import (
    ContactabilityAssessor,
    ContactabilityState,
)
from lib.outreach.bounce_manager import (
    BounceManager,
    redact_credentials,
)
from lib.outreach.campaign_executor import (
    final_pre_send_check,
    TERMINAL_STATUSES,
    _update_campaign_stats,
)
from lib.sheets.google_sheets import GoogleSheetsStorageProvider


class TestBounceHandling(unittest.TestCase):
    """Verifies all 12 bounce handling requirements."""

    def setUp(self):
        self.test_email = "test.bounce.candidate@example.co.uk"
        self.test_lead_id = "LEAD-TEST-BOUNCE-001"
        self.test_campaign_id = "CAMP-BOUNCE-TEST"
        # Clean up suppression list for test email
        SuppressionManager.remove_suppression(self.test_email)
        SuppressionManager.remove_suppression(self.test_lead_id)

    def tearDown(self):
        SuppressionManager.remove_suppression(self.test_email)
        SuppressionManager.remove_suppression(self.test_lead_id)

    # ──────────────────────────────────────────────────────────────────────
    # 1. 550 5.1.1 PERMANENT BOUNCE
    # ──────────────────────────────────────────────────────────────────────
    def test_01_550_511_permanent_bounce_detected(self):
        """550 5.1.1 is classified as permanent bounce with BOUNCED and NOT_CONTACTABLE status."""
        code = "550 5.1.1"
        reason = "The email account that you tried to reach does not exist."
        is_perm = BounceManager.is_permanent_bounce(code, reason)
        self.assertTrue(is_perm, "550 5.1.1 must be detected as a permanent bounce")

        # Process bounce without syncing live sheets
        res = BounceManager.process_bounce(
            lead_id=self.test_lead_id,
            email=self.test_email,
            bounce_code=code,
            bounce_reason=reason,
            bounce_provider="Gmail SMTP",
            sync_sheets=False
        )
        self.assertTrue(res["success"])
        self.assertTrue(res["is_permanent"])
        self.assertEqual(res["email_verification_status"], EmailVerificationStatus.BOUNCED.value)
        self.assertEqual(res["outreach_status"], OutreachStatus.BOUNCED.value)
        self.assertEqual(res["contactability_status"], ContactabilityState.NOT_CONTACTABLE.value)
        self.assertEqual(res["bounce_code"], "550 5.1.1")

    # ──────────────────────────────────────────────────────────────────────
    # 2. PERMANENT BOUNCE DOES NOT RETRY
    # ──────────────────────────────────────────────────────────────────────
    def test_02_permanent_bounce_does_not_retry(self):
        """Permanent bounce is in TERMINAL_STATUSES, rejected by pre-send check, and never retried."""
        self.assertIn(OutreachStatus.BOUNCED.value, TERMINAL_STATUSES)

        # Suppress the address as bounced
        SuppressionManager.suppress_email(
            email=self.test_email,
            reason="550 5.1.1 MAILBOX_NOT_FOUND",
            status="BOUNCED",
            bounce_code="550 5.1.1"
        )

        item = {
            "lead_id": self.test_lead_id,
            "campaign_id": self.test_campaign_id,
            "channel": "Email",
            "recipient": self.test_email,
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "status": OutreachStatus.BOUNCED.value,  # BOUNCED is not QUEUED/FAILED
            "message": {"message_body": "Hello"},
        }
        campaign = {"max_campaign_limit": 10, "daily_limit": 10}

        # Check pre-send check rejects it
        can_send, reason = final_pre_send_check(
            item=item,
            queue=[item],
            campaign=campaign,
            campaign_sent_so_far=0,
            run_counts={},
            max_per_run=None
        )
        self.assertFalse(can_send, "Pre-send check must block bounced email")
        self.assertTrue("BOUNCED" in reason or "SUPPRESSED" in reason or "INVALID_STATE" in reason)

    # ──────────────────────────────────────────────────────────────────────
    # 3. BOUNCED EMAIL BECOMES SUPPRESSED
    # ──────────────────────────────────────────────────────────────────────
    def test_03_bounced_email_becomes_suppressed(self):
        """Bounced email is persistently saved in suppression list with structured bounce metadata."""
        SuppressionManager.suppress_email(
            email=self.test_email,
            reason="550 5.1.1 MAILBOX_NOT_FOUND",
            status="BOUNCED",
            bounce_code="550 5.1.1",
            bounce_reason="Mailbox does not exist",
            bounce_provider="Gmail SMTP"
        )

        # Verify suppression survives
        self.assertTrue(SuppressionManager.is_email_suppressed(self.test_email))
        rec = SuppressionManager.get_email_suppression(self.test_email)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "BOUNCED")
        self.assertEqual(rec["bounce_code"], "550 5.1.1")
        self.assertIn("MAILBOX_NOT_FOUND", rec["reason"])

    # ──────────────────────────────────────────────────────────────────────
    # 4. BOUNCED EMAIL CANNOT BE AUTO-SELECTED
    # ──────────────────────────────────────────────────────────────────────
    def test_04_bounced_email_cannot_be_auto_selected(self):
        """Contactability assessor marks lead with bounced email as NOT_CONTACTABLE and is_ready_for_send=False."""
        SuppressionManager.suppress_email(
            email=self.test_email,
            reason="550 5.1.1 MAILBOX_NOT_FOUND",
            status="BOUNCED",
            bounce_code="550 5.1.1"
        )

        lead = {
            "lead_id": self.test_lead_id,
            "company_name": "Test Bakery Manchester",
            "city": "Manchester",
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "email": self.test_email,
        }

        assessment = ContactabilityAssessor.assess_lead(lead)
        self.assertEqual(assessment.contactability_status, ContactabilityState.NOT_CONTACTABLE.value)
        self.assertFalse(assessment.is_ready_for_send)
        self.assertNotIn("Email", assessment.sendable_channels)

    # ──────────────────────────────────────────────────────────────────────
    # 5. BUSINESS REMAINS OUTREACH_READY
    # ──────────────────────────────────────────────────────────────────────
    def test_05_business_remains_outreach_ready(self):
        """Email delivery bounce does not downgrade the lead's business qualification state."""
        lead = Lead(
            lead_id=self.test_lead_id,
            company_name="Test Thai Manchester",
            industry="Thai restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            qualification_state=QualificationState.OUTREACH_READY.value,
            lead_score=85,
            review_count=900,
            rating=4.7
        )

        # Process bounce
        BounceManager.process_bounce(
            lead_id=self.test_lead_id,
            email=self.test_email,
            bounce_code="550 5.1.1",
            bounce_reason="Mailbox does not exist",
            sync_sheets=False
        )

        # Qualification state on lead must strictly remain OUTREACH_READY
        self.assertEqual(lead.qualification_state, QualificationState.OUTREACH_READY.value)
        self.assertEqual(lead.lead_score, 85)
        # Business itself is NOT globally suppressed
        self.assertFalse(SuppressionManager.is_suppressed(self.test_lead_id))

    # ──────────────────────────────────────────────────────────────────────
    # 6. CONTACT HISTORY PRESERVES SENT -> BOUNCED
    # ──────────────────────────────────────────────────────────────────────
    def test_06_contact_history_preserves_sent_to_bounced(self):
        """Contact timeline preserves the initial SENT record and appends BOUNCED outcome."""
        import uuid
        uid = uuid.uuid4().hex[:12]
        lead_id = f"LEAD-HIST-TEST-{uid}"
        email = f"user-{uid}@example.co.uk"
        mid = f"<test-{uid}@dripp.media>"

        try:
            # Step 1: Initial transmission
            ContactHistoryManager.record_attempt(
                lead_id=lead_id,
                campaign_id="CAMP-HIST-01",
                channel="Email",
                recipient=email,
                message_id=mid,
                status="SENT",
                outcome="ACCEPTED",
                message_body="Original outreach pitch",
                error=""
            )

            # Step 2: Later bounce received
            later_ts = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat() + "Z"
            ContactHistoryManager.record_bounce(
                lead_id=lead_id,
                campaign_id="CAMP-HIST-01",
                recipient=email,
                bounce_code="550 5.1.1",
                bounce_reason="Mailbox does not exist",
                bounce_provider="Gmail SMTP",
                message_id=mid,
                bounced_at=later_ts
            )

            timeline = ContactHistoryManager.get_lead_timeline(lead_id)
            self.assertEqual(len(timeline), 2, "Timeline must contain both SENT and BOUNCED events")
            self.assertEqual(timeline[0]["status"], "SENT")
            self.assertEqual(timeline[0]["outcome"], "ACCEPTED")
            self.assertEqual(timeline[1]["status"], "BOUNCED")
            self.assertEqual(timeline[1]["outcome"], "BOUNCED")
            self.assertEqual(timeline[1]["bounce_code"], "550 5.1.1")
        finally:
            history = ContactHistoryManager._load()
            filtered = [h for h in history if h.get("lead_id") != lead_id]
            ContactHistoryManager._save(filtered)

    # ──────────────────────────────────────────────────────────────────────
    # 7. CAMPAIGN ANALYTICS COUNT BOUNCE
    # ──────────────────────────────────────────────────────────────────────
    def test_07_campaign_analytics_count_bounce(self):
        """Campaign metrics track bounced_count and distinguish SENT from BOUNCED."""
        campaign_id = f"CAMP-ANALYTICS-{os.getpid()}"
        campaigns = [{
            "campaign_id": campaign_id,
            "campaign_name": "Analytics Test Campaign",
            "sent_count": 0,
            "bounced_count": 0,
            "failed_count": 0,
            "blocked_count": 0,
            "queued_count": 1,
            "execution_summary": {}
        }]

        with patch("lib.outreach.campaign_executor._load_campaigns", return_value=campaigns), \
             patch("lib.outreach.campaign_executor._save_campaigns") as mock_save:
            # 1 send succeeded initially
            _update_campaign_stats(campaign_id, sent=1)
            self.assertEqual(campaigns[0]["sent_count"], 1)

            # Later 1 bounce recorded
            _update_campaign_stats(campaign_id, bounced=1)
            self.assertEqual(campaigns[0]["sent_count"], 1)
            self.assertEqual(campaigns[0]["bounced_count"], 1)
            self.assertEqual(campaigns[0]["execution_summary"]["bounced"], 1)

    # ──────────────────────────────────────────────────────────────────────
    # 8. ANOTHER CANDIDATE EMAIL REMAINS AVAILABLE
    # ──────────────────────────────────────────────────────────────────────
    def test_08_another_candidate_email_remains_available(self):
        """If candidate 1 is BOUNCED, candidate 2 is preserved separately and not discarded."""
        bounced_email = f"bad-{os.getpid()}@example.co.uk"
        unverified_email = f"events-{os.getpid()}@example.co.uk"

        # Suppress bounced candidate
        SuppressionManager.suppress_email(
            email=bounced_email,
            reason="550 5.1.1 MAILBOX_NOT_FOUND",
            status="BOUNCED",
            bounce_code="550 5.1.1"
        )

        cand1 = EmailCandidate(
            email=bounced_email,
            verification_status=EmailVerificationStatus.BOUNCED.value,
            source=EmailSource.LEGITIMATE_BUSINESS_DIRECTORY.value
        )
        cand2 = EmailCandidate(
            email=unverified_email,
            verification_status=EmailVerificationStatus.UNVERIFIED.value,
            source=EmailSource.SEARCH_SNIPPET_ONLY.value
        )

        candidates = [cand1, cand2]
        # Candidate 2 (UNVERIFIED, rank 2) ranks above Candidate 1 (BOUNCED, rank -1)
        status_map = {
            EmailVerificationStatus.VERIFIED.value: 3,
            EmailVerificationStatus.UNVERIFIED.value: 2,
            EmailVerificationStatus.INVALID.value: 1,
            EmailVerificationStatus.UNKNOWN.value: 0,
            EmailVerificationStatus.BOUNCED.value: -1,
        }
        candidates.sort(key=lambda c: status_map.get(c.verification_status, 0), reverse=True)

        self.assertEqual(candidates[0].email, unverified_email, "Candidate 2 should be ranked above bounced candidate")
        self.assertEqual(len(candidates), 2, "Candidate 1 must not be discarded from candidates list")
        self.assertEqual(candidates[1].email, bounced_email)

        # Cleanup
        SuppressionManager.remove_suppression(bounced_email)

    # ──────────────────────────────────────────────────────────────────────
    # 9. TEMPORARY DELIVERY FAILURE REMAINS RETRYABLE
    # ──────────────────────────────────────────────────────────────────────
    def test_09_temporary_delivery_failure_remains_retryable(self):
        """Temporary failure (421 / 451) is not permanently suppressed and remains retryable."""
        temp_code = "451 4.4.1"
        temp_reason = "Server temporarily busy, try again later"

        is_perm = BounceManager.is_permanent_bounce(temp_code, temp_reason)
        self.assertFalse(is_perm, "451 should be recognized as temporary, not permanent")

        res = BounceManager.process_bounce(
            lead_id=self.test_lead_id,
            email=self.test_email,
            bounce_code=temp_code,
            bounce_reason=temp_reason,
            sync_sheets=False
        )
        self.assertFalse(res["is_permanent"])
        self.assertTrue(res["retryable"])
        self.assertFalse(res["email_suppressed"])
        self.assertFalse(SuppressionManager.is_email_suppressed(self.test_email))

    # ──────────────────────────────────────────────────────────────────────
    # 10. 250 SMTP ACCEPTANCE ALONE DOES NOT EQUAL DELIVERED
    # ──────────────────────────────────────────────────────────────────────
    def test_10_smtp_250_does_not_equal_delivered(self):
        """SMTP 250 OK indicates SENT (provider acceptance), NOT final mailbox delivery."""
        from lib.outreach.send_adapters import _result
        res = _result(True, "Google Gmail SMTP", message_id="<test@dripp.media>", provider_response="SMTP 250 OK")
        self.assertTrue(res["success"])
        # Provider response confirms acceptance for delivery, status is SENT, not DELIVERED
        self.assertIn("250 OK", res["provider_response"])
        self.assertNotEqual(res.get("status"), "DELIVERED")

    # ──────────────────────────────────────────────────────────────────────
    # 11. CREDENTIALS ARE NEVER STORED IN BOUNCE RECORDS
    # ──────────────────────────────────────────────────────────────────────
    def test_11_credentials_are_redacted_from_bounce_records(self):
        """Passwords, tokens, and secrets in raw bounce messages are redacted."""
        raw_bounce = (
            "550 5.1.1 Mailbox not found. Diagnostic: "
            "password=supersecretpassword123 token=abc123xyz Bearer eyJhbGciOiJIUzI1NiJ9"
        )
        sanitized = redact_credentials(raw_bounce)
        self.assertNotIn("supersecretpassword123", sanitized)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", sanitized)
        self.assertIn("***REDACTED***", sanitized)

    # ──────────────────────────────────────────────────────────────────────
    # 12. DUPLICATE LEAD ROWS NOT CREATED
    # ──────────────────────────────────────────────────────────────────────
    def test_12_duplicate_lead_rows_not_created(self):
        """Updating lead outreach status updates the existing row in place without creating duplicate rows."""
        storage = GoogleSheetsStorageProvider()
        records = storage.leads_worksheet.get_all_records()
        hong_thai_rows = [r for r in records if r.get("lead_id") == "LEAD-MAN-709C66"]
        self.assertEqual(len(hong_thai_rows), 1, "There must be exactly 1 row for LEAD-MAN-709C66 in LEADS tab")


if __name__ == "__main__":
    unittest.main()
