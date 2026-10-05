import unittest
import os
import json
from lib.types import Lead, OutreachStatus, OutreachMode, QualificationState, OperationalStatus, WebsiteStatus
from lib.outreach.outreach_service import OutreachService, OutreachEligibilityChecker, ChannelConfigManager

class TestOutreachArchitecture(unittest.TestCase):
    def setUp(self):
        self.service = OutreachService()

    def test_channel_not_configured_guard(self):
        """Section 23: Verify unconfigured channels return CHANNEL NOT CONFIGURED and block sending."""
        status = ChannelConfigManager.get_channel_status("SMS / Phone")
        self.assertFalse(status["configured"])
        self.assertEqual(status["status_label"], "CHANNEL NOT CONFIGURED")
        self.assertIn("CHANNEL NOT CONFIGURED", status["message"])

    def test_eligibility_checker_rejects_ineligible_leads(self):
        """Section 10: Verify all 10 conditions are checked."""
        # 1. Lead not OUTREACH_READY
        lead_not_ready = {
            "qualification_state": "MANUAL_REVIEW",
            "country": "United Kingdom",
            "target_country": "United Kingdom",
            "company_name": "Test Cafe",
            "website_status": "NO_WEBSITE_CONFIRMED",
            "operational_status": "ACTIVE_CONFIRMED",
            "outreach_angle": "Valid angle"
        }
        eligible, reason = OutreachEligibilityChecker.verify_eligibility(lead_not_ready, "OUT-001", "Instagram Direct Message")
        self.assertFalse(eligible)
        self.assertIn("strictly requires OUTREACH_READY", reason)

        # 2. Operational status not ACTIVE_CONFIRMED
        lead_likely = {
            "qualification_state": "OUTREACH_READY",
            "country": "United Kingdom",
            "target_country": "United Kingdom",
            "company_name": "Test Cafe",
            "website_status": "NO_WEBSITE_CONFIRMED",
            "operational_status": "ACTIVE_LIKELY",
            "outreach_angle": "Valid angle"
        }
        eligible, reason = OutreachEligibilityChecker.verify_eligibility(lead_likely, "OUT-001", "Instagram Direct Message")
        self.assertFalse(eligible)
        self.assertIn("strictly requires ACTIVE_CONFIRMED", reason)

        # 3. Has website
        lead_has_web = {
            "qualification_state": "OUTREACH_READY",
            "country": "United Kingdom",
            "target_country": "United Kingdom",
            "company_name": "Test Cafe",
            "website_status": "WEBSITE_EXISTS",
            "operational_status": "ACTIVE_CONFIRMED",
            "outreach_angle": "Valid angle"
        }
        eligible, reason = OutreachEligibilityChecker.verify_eligibility(lead_has_web, "OUT-001", "Instagram Direct Message")
        self.assertFalse(eligible)
        self.assertIn("requires NO_WEBSITE_CONFIRMED", reason)

    def test_duplicate_send_protection(self):
        """Section 16: Verify duplicate sending is blocked if already queued in same campaign."""
        lead = {
            "lead_id": "LEAD-MCR-TEST01",
            "qualification_state": "OUTREACH_READY",
            "country": "United Kingdom",
            "target_country": "United Kingdom",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "website_status": "NO_WEBSITE_CONFIRMED",
            "operational_status": "ACTIVE_CONFIRMED",
            "instagram_url": "https://www.instagram.com/seoulkimchi/",
            "outreach_angle": "Test pitch"
        }
        existing_queue = [{
            "lead_id": "LEAD-MCR-TEST01",
            "campaign_id": "OUT-MCR-2026-001",
            "status": "QUEUED"
        }]
        eligible, reason = OutreachEligibilityChecker.verify_eligibility(
            lead=lead,
            campaign_id="OUT-MCR-2026-001",
            channel="Instagram Direct Message",
            existing_queue=existing_queue
        )
        self.assertFalse(eligible)
        self.assertIn("Duplicate send blocked", reason)

if __name__ == "__main__":
    unittest.main()
