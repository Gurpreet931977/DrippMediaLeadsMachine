"""
Unit Tests for Phase 8.2: Manual Outreach Execution Control + CRM State Transition

Covers all 16 mandatory test requirements:
 1. Only OUTREACH_READY leads enter the queue.
 2. Manual-only leads can enter the queue.
 3. Automated-sendable=false is enforced.
 4. No recipient IDs are fabricated.
 5. Public Instagram URLs remain manual.
 6. Draft loads from approved Phase 8.1 artifact.
 7. Copy action does not mutate CRM.
 8. Opening Instagram does not invoke a send adapter.
 9. No confirmation means outreach remains NOT_READY.
10. Confirmation changes status to SENT.
11. Qualification remains OUTREACH_READY after send.
12. Manual send is distinguishable from automated send.
13. Duplicate confirmation is blocked.
14. Campaign files remain unchanged.
15. No pre-send message-history mutation.
16. No API dispatch occurs anywhere in the manual path.
"""

import os
import json
import hashlib
import tempfile
import unittest
from datetime import datetime

from lib.outreach.manual_outreach_controller import (
    generate_manual_outreach_queue,
    get_manual_outreach_queue,
    log_audit_event,
    confirm_manual_send,
    get_audit_log,
)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

EXPECTED_CAMPAIGNS_HASH = "2448504b93d0792590a5d2cd281fd456d37db249ceb7202441840e2957384a42"
EXPECTED_HISTORY_HASH = "c54c7376a81b16a9076f4ed9bff38ed671d33c2172da1edf33479e175053289e"


def _compute_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


class TestPhase82ManualOutreach(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.leads_cache_path = os.path.join(DATA_DIR, "cache_sheets_leads.json")
        cls.prep_path = os.path.join(DATA_DIR, "phase_8_1_outreach_preparation.json")
        cls.queue_path = os.path.join(DATA_DIR, "phase_8_2_manual_outreach_queue.json")
        cls.history_path = os.path.join(DATA_DIR, "message_history.json")
        cls.campaigns_path = os.path.join(DATA_DIR, "campaigns.json")

    def test_01_only_outreach_ready_leads_enter_queue(self):
        """1. Only OUTREACH_READY leads enter the queue."""
        queue_data = get_manual_outreach_queue(queue_path=self.queue_path)
        items = queue_data.get("queue", [])
        self.assertGreater(len(items), 0, "Queue must contain eligible candidates")
        for item in items:
            self.assertEqual(item.get("qualification_state"), "OUTREACH_READY")
            self.assertNotIn(item.get("qualification_state"), ["MANUAL_REVIEW", "RESEARCH_ONLY", "EXCLUDED"])

    def test_02_manual_only_leads_can_enter_queue(self):
        """2. Manual-only leads can enter the queue."""
        queue_data = get_manual_outreach_queue(queue_path=self.queue_path)
        for item in queue_data.get("queue", []):
            self.assertEqual(item.get("outreach_mode"), "MANUAL")
            self.assertEqual(item.get("outreach_status"), "NOT_READY")

    def test_03_automated_sendable_false_enforced(self):
        """3. Automated-sendable=false is enforced."""
        queue_data = get_manual_outreach_queue(queue_path=self.queue_path)
        self.assertFalse(queue_data.get("automated_sending_enabled"))
        for item in queue_data.get("queue", []):
            self.assertTrue(item.get("send_confirmation_required"))

    def test_04_no_recipient_ids_are_fabricated(self):
        """4. No recipient IDs are fabricated (no IGSID, no PSID)."""
        queue_data = get_manual_outreach_queue(queue_path=self.queue_path)
        for item in queue_data.get("queue", []):
            self.assertNotIn("recipient_id", item)
            self.assertNotIn("igsid", item)
            self.assertNotIn("psid", item)
            disp = item.get("recipient_display", "")
            self.assertTrue(disp.startswith("@"), f"Recipient display should be handle, got: {disp}")

    def test_05_public_instagram_urls_remain_manual(self):
        """5. Public Instagram URLs remain manual."""
        queue_data = get_manual_outreach_queue(queue_path=self.queue_path)
        for item in queue_data.get("queue", []):
            self.assertEqual(item.get("channel"), "INSTAGRAM")
            url = item.get("recipient_url", "")
            self.assertTrue(url.startswith("https://www.instagram.com/"), f"Unexpected Instagram URL: {url}")
            self.assertEqual(item.get("outreach_mode"), "MANUAL")

    def test_06_draft_loads_from_approved_phase_8_1_artifact(self):
        """6. Draft loads from approved Phase 8.1 artifact."""
        queue_data = get_manual_outreach_queue(queue_path=self.queue_path)
        live_seafood = next(q for q in queue_data["queue"] if q["lead_id"] == "LEAD-MAN-0363CF")
        draft = live_seafood.get("approved_draft", {})
        self.assertEqual(draft.get("word_count"), 83)
        self.assertIn("Hi Live Seafood team", draft.get("body", ""))
        self.assertIn("110+ reviews and a 4.1-star rating", draft.get("body", ""))
        self.assertIn("Dripp Media", draft.get("body", ""))

    def test_07_copy_action_does_not_mutate_crm(self):
        """7. Copy action does not mutate CRM."""
        leads_hash_before = _compute_sha256(self.leads_cache_path)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf:
            temp_audit = tf.name
        try:
            log_audit_event("DRAFT_COPIED", "LEAD-MAN-0363CF", {"word_count": 83}, audit_path=temp_audit)
            leads_hash_after = _compute_sha256(self.leads_cache_path)
            self.assertEqual(leads_hash_before, leads_hash_after, "CRM cache mutated by copy action!")
            events = get_audit_log(audit_path=temp_audit)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["action"], "DRAFT_COPIED")
        finally:
            if os.path.exists(temp_audit):
                os.remove(temp_audit)

    def test_08_opening_instagram_does_not_invoke_send_adapter(self):
        """8. Opening Instagram does not invoke a send adapter."""
        leads_hash_before = _compute_sha256(self.leads_cache_path)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf:
            temp_audit = tf.name
        try:
            log_audit_event("INSTAGRAM_OPENED", "LEAD-MAN-0363CF", {"url": "https://www.instagram.com/live_seafood_ltd/"}, audit_path=temp_audit)
            leads_hash_after = _compute_sha256(self.leads_cache_path)
            self.assertEqual(leads_hash_before, leads_hash_after, "CRM cache mutated by opening Instagram!")
            events = get_audit_log(audit_path=temp_audit)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["action"], "INSTAGRAM_OPENED")
        finally:
            if os.path.exists(temp_audit):
                os.remove(temp_audit)

    def test_09_no_confirmation_means_outreach_remains_not_ready(self):
        """9. No confirmation means outreach remains NOT_READY."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_leads:
            with open(self.leads_cache_path, "r") as f:
                tf_leads.write(f.read())
            temp_leads = tf_leads.name

        try:
            with self.assertRaises(ValueError):
                confirm_manual_send(
                    lead_id="LEAD-MAN-0363CF",
                    operator_confirmed=False,
                    leads_path=temp_leads
                )
            with open(temp_leads, "r") as f:
                data = json.load(f)
            target = next(l for l in data["leads"] if l["lead_id"] == "LEAD-MAN-0363CF")
            self.assertEqual(target.get("outreach_status"), "NOT_READY")
        finally:
            if os.path.exists(temp_leads):
                os.remove(temp_leads)

    def test_10_confirmation_changes_status_to_sent(self):
        """10. Confirmation changes status to SENT."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_leads, \
             tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_hist, \
             tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_audit, \
             tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_q:
            
            with open(self.leads_cache_path, "r") as f:
                tf_leads.write(f.read())
            with open(self.history_path, "r") as f:
                tf_hist.write(f.read())
            with open(self.queue_path, "r") as f:
                tf_q.write(f.read())

            temp_leads = tf_leads.name
            temp_hist = tf_hist.name
            temp_audit = tf_audit.name
            temp_q = tf_q.name

        try:
            res = confirm_manual_send(
                lead_id="LEAD-MAN-0363CF",
                operator_confirmed=True,
                notes="Operator verified send on Instagram mobile app.",
                leads_path=temp_leads,
                history_path=temp_hist,
                audit_path=temp_audit,
                queue_path=temp_q
            )
            self.assertTrue(res.get("success"))
            self.assertEqual(res.get("outreach_status"), "SENT")
            self.assertEqual(res.get("channel"), "INSTAGRAM_MANUAL")
            self.assertTrue(res.get("sent_at"))

            # Check lead state in isolated file
            with open(temp_leads, "r") as f:
                data = json.load(f)
            target = next(l for l in data["leads"] if l["lead_id"] == "LEAD-MAN-0363CF")
            self.assertEqual(target.get("outreach_status"), "SENT")
            self.assertEqual(target.get("outreach_channel"), "INSTAGRAM_MANUAL")
            self.assertEqual(target.get("outreach_mode"), "MANUAL")
            self.assertTrue(target.get("operator_confirmed"))
            self.assertTrue(target.get("outreach_sent_at"))
        finally:
            for p in [temp_leads, temp_hist, temp_audit, temp_q]:
                if os.path.exists(p):
                    os.remove(p)

    def test_11_qualification_remains_outreach_ready_after_send(self):
        """11. Qualification remains OUTREACH_READY after send."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_leads, \
             tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_hist, \
             tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_audit:
            
            with open(self.leads_cache_path, "r") as f:
                tf_leads.write(f.read())
            with open(self.history_path, "r") as f:
                tf_hist.write(f.read())

            temp_leads = tf_leads.name
            temp_hist = tf_hist.name
            temp_audit = tf_audit.name

        try:
            res = confirm_manual_send(
                lead_id="LEAD-MAN-0363CF",
                operator_confirmed=True,
                leads_path=temp_leads,
                history_path=temp_hist,
                audit_path=temp_audit
            )
            with open(temp_leads, "r") as f:
                data = json.load(f)
            target = next(l for l in data["leads"] if l["lead_id"] == "LEAD-MAN-0363CF")
            # Qualification state invariant:
            self.assertEqual(target.get("qualification_state"), "OUTREACH_READY")
            self.assertEqual(target.get("lead_score"), 60)
            self.assertEqual(target.get("priority"), "LOW")
            self.assertEqual(target.get("rating"), 4.1)
            self.assertEqual(target.get("review_count"), 112)
            self.assertEqual(target.get("website_status"), "NO_WEBSITE_CONFIRMED")
        finally:
            for p in [temp_leads, temp_hist, temp_audit]:
                if os.path.exists(p):
                    os.remove(p)

    def test_12_manual_send_is_distinguishable_from_automated_send(self):
        """12. Manual send is distinguishable from automated send."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_leads, \
             tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_hist, \
             tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_audit:
            
            with open(self.leads_cache_path, "r") as f:
                tf_leads.write(f.read())
            with open(self.history_path, "r") as f:
                tf_hist.write(f.read())

            temp_leads = tf_leads.name
            temp_hist = tf_hist.name
            temp_audit = tf_audit.name

        try:
            confirm_manual_send(
                lead_id="LEAD-MAN-0363CF",
                operator_confirmed=True,
                leads_path=temp_leads,
                history_path=temp_hist,
                audit_path=temp_audit
            )
            with open(temp_hist, "r") as f:
                hist_data = json.load(f)
            entries = hist_data.get("LEAD-MAN-0363CF", [])
            self.assertGreater(len(entries), 0)
            last_entry = entries[-1]
            self.assertEqual(last_entry.get("delivery_method"), "MANUAL")
            self.assertEqual(last_entry.get("channel"), "INSTAGRAM_MANUAL")
            self.assertFalse(last_entry.get("automated"))
            self.assertTrue(last_entry.get("operator_confirmed"))
            self.assertEqual(last_entry.get("message_id"), "")  # No synthetic API message ID
        finally:
            for p in [temp_leads, temp_hist, temp_audit]:
                if os.path.exists(p):
                    os.remove(p)

    def test_13_duplicate_confirmation_is_blocked(self):
        """13. Duplicate confirmation is blocked."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_leads, \
             tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_hist, \
             tempfile.NamedTemporaryFile(mode="w", delete=False) as tf_audit:
            
            with open(self.leads_cache_path, "r") as f:
                tf_leads.write(f.read())
            with open(self.history_path, "r") as f:
                tf_hist.write(f.read())

            temp_leads = tf_leads.name
            temp_hist = tf_hist.name
            temp_audit = tf_audit.name

        try:
            # First confirmation
            res1 = confirm_manual_send(
                lead_id="LEAD-MAN-0363CF",
                operator_confirmed=True,
                leads_path=temp_leads,
                history_path=temp_hist,
                audit_path=temp_audit
            )
            self.assertTrue(res1.get("success"))

            # Second confirmation (must be blocked)
            res2 = confirm_manual_send(
                lead_id="LEAD-MAN-0363CF",
                operator_confirmed=True,
                leads_path=temp_leads,
                history_path=temp_hist,
                audit_path=temp_audit
            )
            self.assertFalse(res2.get("success"))
            self.assertEqual(res2.get("error"), "DUPLICATE_SEND_BLOCKED")
            self.assertIn("Already marked SENT", res2.get("message", ""))
        finally:
            for p in [temp_leads, temp_hist, temp_audit]:
                if os.path.exists(p):
                    os.remove(p)

    def test_14_campaign_files_remain_unchanged(self):
        """14. Campaign files remain unchanged."""
        current_hash = _compute_sha256(self.campaigns_path)
        self.assertEqual(current_hash, EXPECTED_CAMPAIGNS_HASH, "Campaigns file was unexpectedly modified!")
        with open(self.campaigns_path, "r") as f:
            camps = json.load(f)
        # Ensure no active campaigns armed in this phase
        for c in camps:
            self.assertNotEqual(c.get("status"), "ARMED", "No campaign should be ARMED")

    def test_15_no_pre_send_message_history_mutation(self):
        """15. No pre-send message-history mutation."""
        current_hash = _compute_sha256(self.history_path)
        self.assertEqual(current_hash, EXPECTED_HISTORY_HASH, "Production message history mutated before real send!")
        with open(self.history_path, "r") as f:
            hist = json.load(f)
        self.assertNotIn("LEAD-MAN-0363CF", hist, "Live Seafood Ltd should not have message history before operator confirms in production!")

    def test_16_no_api_dispatch_occurs_anywhere_in_manual_path(self):
        """16. No API dispatch occurs anywhere in the manual path."""
        import inspect
        import lib.outreach.manual_outreach_controller as moc
        source = inspect.getsource(moc)
        self.assertNotIn("send_adapters", source)
        self.assertNotIn("graph.facebook.com", source)
        self.assertNotIn("api.instagram.com", source)
        self.assertNotIn("MetaGraphClient", source)
        self.assertNotIn("dispatch_campaign", source)


if __name__ == "__main__":
    unittest.main()
