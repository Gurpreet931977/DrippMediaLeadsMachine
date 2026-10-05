"""
Test Suite for Phase 8.6.1 + Phase 8.7: Rule Threshold Integrity & Three-Lead Outreach Batch Preparation

Requirements Covered (Objective Q):
  1. Rule threshold is 50.
  2. 49 reviews does not qualify.
  3. 50 reviews can qualify subject to other rules.
  4. Three newly promoted businesses are persisted correctly.
  5. Sent Live Seafood is excluded from active outreach.
  6. MANUAL_REVIEW is excluded.
  7. RESEARCH_ONLY is excluded.
  8. Only real contact channels are used.
  9. No synthetic recipient IDs.
  10. Drafts contain only supported personalization.
  11. Website-development offer is used.
  12. Draft version is `WEBSITE_001`.
  13. No campaign is created.
  14. No message history mutation.
  15. No outreach send.
  16. Duplicate businesses are impossible.
  17. Dashboard shows correct active batch.
"""

import os
import json
import unittest
from fastapi.testclient import TestClient

from server import app
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.outreach.phase_8_7_outreach_batch_engine import Phase87OutreachBatchEngine, CORE_OFFER, DRAFT_VERSION

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
CRM_CACHE_PATH = os.path.join(DATA_DIR, "cache_sheets_leads.json")
RESULTS_PATH = os.path.join(DATA_DIR, "phase_8_6_qualification_results.json")
BATCH_PATH = os.path.join(DATA_DIR, "phase_8_7_outreach_batch.json")
CAMPAIGNS_PATH = os.path.join(DATA_DIR, "campaigns.json")
MESSAGES_PATH = os.path.join(DATA_DIR, "message_history.json")


class TestPhase87OutreachPreparation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Phase87OutreachBatchEngine()
        cls.batch_data = cls.engine.generate_batch()
        with open(CRM_CACHE_PATH, "r", encoding="utf-8") as f:
            cls.crm_data = json.load(f)
        with open(RESULTS_PATH, "r", encoding="utf-8") as f:
            cls.results_data = json.load(f)
        cls.client = TestClient(app)

    # 1. Rule threshold is 50
    def test_01_rule_threshold_is_50(self):
        """1. Rule threshold is strictly 50 reviews in core qualification engines."""
        scorer = LeadScoringProvider()
        self.assertEqual(scorer.min_reviews_outreach, 50)

        # OperationalValidator logic explicitly requires revs >= 50
        import inspect
        val = OperationalValidator()
        src = inspect.getsource(val.verify_operations)
        self.assertIn("revs >= 50", src)

        # Check report string cannot claim >=20
        with open("phase_8_6_review_evidence_report.md", "r", encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("count >= 20", text)
        self.assertTrue(r"\ge 50" in text or ">= 50" in text or ">=50" in text)

    # 2. 49 reviews does not qualify
    def test_02_49_reviews_does_not_qualify(self):
        """2. 49 reviews fails Rule B qualification threshold."""
        from lib.types import DiscoveredBusiness
        scorer = LeadScoringProvider()
        biz_49 = DiscoveredBusiness(
            company_name="Test Pub 49",
            category="pub",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            review_count=49,
            rating=4.5,
            operational_status="ACTIVE_CONFIRMED"
        )
        qual_49 = scorer.evaluate_lead(biz_49, verification_status="NO_WEBSITE_CONFIRMED")
        self.assertNotEqual(qual_49["qualification_state"], "OUTREACH_READY")
        self.assertEqual(qual_49["qualification_state"], "MANUAL_REVIEW")
        self.assertIn("minimum 50 required", qual_49["qualification_reason"])

    # 3. 50 reviews can qualify subject to other rules
    def test_03_50_reviews_can_qualify_subject_to_rules(self):
        """3. 50 reviews clears the review count barrier and is eligible subject to remaining rules."""
        from lib.types import DiscoveredBusiness
        scorer = LeadScoringProvider()
        biz_50 = DiscoveredBusiness(
            company_name="Test Pub 50",
            category="pub",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            review_count=50,
            rating=4.5,
            operational_status="ACTIVE_CONFIRMED"
        )
        qual_50 = scorer.evaluate_lead(biz_50, verification_status="NO_WEBSITE_CONFIRMED")
        # Clears the review count hurdle (no "minimum 50 required" rejection)
        self.assertNotIn("minimum 50 required", qual_50["qualification_reason"])
        
        # When all other criteria are met (like the 3 promoted leads), 50+ qualifies for OUTREACH_READY
        for lead in self.batch_data.get("active_batch", []):
            self.assertGreaterEqual(lead["review_count"], 50)
            self.assertEqual(lead["qualification_state"], "OUTREACH_READY")

    # 4. Three newly promoted businesses are persisted correctly
    def test_04_three_promoted_businesses_persisted_correctly(self):
        """4. Three newly promoted businesses are persisted across CRM cache, Phase 8.6, and outreach batch."""
        promoted_names = {"Dog and Partridge", "Ducie Arms", "The Old Monkey"}
        crm_leads = {l["company_name"]: l for l in self.crm_data.get("leads", [])}
        p86_leads = {l["company_name"]: l for l in self.results_data.get("active_outreach_queue", [])}
        batch_leads = {l["company_name"]: l for l in self.batch_data.get("active_batch", [])}

        for name in promoted_names:
            self.assertIn(name, crm_leads, f"{name} must exist in CRM cache")
            self.assertIn(name, p86_leads, f"{name} must exist in Phase 8.6 active queue")
            self.assertIn(name, batch_leads, f"{name} must exist in Phase 8.7 outreach batch")

            c = crm_leads[name]
            p = p86_leads[name]
            b = batch_leads[name]

            # Verify consistency across all 11 required fields
            self.assertEqual(c["lead_id"], p["lead_id"])
            self.assertEqual(c["lead_id"], b["lead_id"])
            self.assertEqual(c["qualification_state"], "OUTREACH_READY")
            self.assertEqual(p["qualification_state"], "OUTREACH_READY")
            self.assertEqual(b["qualification_state"], "OUTREACH_READY")
            self.assertEqual(c["outreach_status"], "NOT_READY")
            self.assertEqual(p["outreach_status"], "NOT_READY")
            self.assertEqual(b["outreach_status"], "NOT_READY")
            self.assertEqual(c["outreach_mode"], "MANUAL")
            self.assertEqual(p["outreach_mode"], "MANUAL")
            self.assertEqual(b["outreach_mode"], "MANUAL")
            self.assertEqual(c["review_count"], p["review_count"])
            self.assertEqual(c["review_count"], b["review_count"])
            self.assertGreaterEqual(c["review_count"], 50)
            self.assertEqual(c["rating"], p["rating"])
            self.assertEqual(c["rating"], b["rating"])
            self.assertGreaterEqual(c["rating"], 4.0)
            self.assertEqual(c["review_freshness"], "RECENT")
            self.assertEqual(p["evidence_freshness"], "RECENT")
            self.assertEqual(b["review_freshness"], "RECENT")
            self.assertEqual(c["website_status"], "NO_WEBSITE_CONFIRMED")
            self.assertEqual(p["website_status"], "NO_WEBSITE_CONFIRMED")
            self.assertEqual(b["website_status"], "NO_WEBSITE_CONFIRMED")
            self.assertEqual(c["contactability_status"], "PARTIALLY_CONTACTABLE")
            self.assertEqual(b["contactability_status"], "PARTIALLY_CONTACTABLE")

    # 5. Live Seafood reconciliation: in active queue when never confirmed sent
    def test_05_live_seafood_authoritative_reconciliation_active_queue(self):
        """5. Live Seafood Ltd is in active queue when unconfirmed sent, and excluded if confirmed sent."""
        active_names = [l["company_name"] for l in self.batch_data.get("active_batch", [])]
        # Objective D: If it was never actually sent -> LIVE_SEAFOOD_IN_ACTIVE_QUEUE = YES
        self.assertIn("Live Seafood Ltd", active_names)
        self.assertIn("LEAD-MAN-0363CF", [l["lead_id"] for l in self.batch_data.get("active_batch", [])])

        ls = next(l for l in self.batch_data.get("active_batch", []) if l["lead_id"] == "LEAD-MAN-0363CF")
        self.assertEqual(ls["qualification_state"], "OUTREACH_READY")
        self.assertEqual(ls["outreach_status"], "NOT_READY")
        self.assertEqual(ls["outreach_mode"], "MANUAL")
        self.assertEqual(ls["channel"], "INSTAGRAM MANUAL")
        self.assertEqual(ls["recipient"], "@live_seafood_ltd")
        self.assertEqual(ls["draft_quality_audit"]["draft_status"], "APPROVED")

    # 6. MANUAL_REVIEW is excluded
    def test_06_manual_review_excluded(self):
        """6. MANUAL_REVIEW leads are excluded from active outreach batch."""
        for lead in self.batch_data.get("active_batch", []):
            self.assertEqual(lead["qualification_state"], "OUTREACH_READY")
            self.assertNotEqual(lead["qualification_state"], "MANUAL_REVIEW")

    # 7. RESEARCH_ONLY is excluded
    def test_07_research_only_excluded(self):
        """7. RESEARCH_ONLY leads are excluded from active outreach batch."""
        for lead in self.batch_data.get("active_batch", []):
            self.assertEqual(lead["qualification_state"], "OUTREACH_READY")
            self.assertNotEqual(lead["qualification_state"], "RESEARCH_ONLY")

    # 8. Only real contact channels are used
    def test_08_only_real_contact_channels_used(self):
        """8. Only real verified contact channels are routed."""
        for lead in self.batch_data.get("active_batch", []):
            channel = lead["channel"]
            recipient = lead["recipient"]
            self.assertTrue("PHONE" in channel or "INSTAGRAM" in channel or "FACEBOOK" in channel)
            if "PHONE" in channel:
                self.assertTrue(recipient.startswith("+44"), f"Invalid UK phone: {recipient}")
            if "INSTAGRAM" in channel:
                self.assertTrue("instagram.com" in recipient or "@" in recipient)
            if "FACEBOOK" in channel:
                self.assertTrue("facebook.com" in recipient)

    # 9. No synthetic recipient IDs
    def test_09_no_synthetic_recipient_ids(self):
        """9. Zero synthetic recipient IDs (no IGSID, no PSID)."""
        for lead in self.batch_data.get("active_batch", []):
            self.assertNotIn("recipient_id", lead)
            self.assertNotIn("igsid", lead)
            self.assertNotIn("psid", lead)
        self.assertEqual(self.batch_data["safety_invariants"]["fabricated_recipient_ids"], 0)

    # 10. Drafts contain only supported personalization
    def test_10_drafts_contain_only_supported_personalization(self):
        """10. Drafts contain only supported personalization; zero prohibited claims."""
        for lead in self.batch_data.get("active_batch", []):
            body = lead["draft_body"]
            # Prohibitions
            self.assertNotIn("losing customers", body.lower())
            self.assertNotIn("guarantee", body.lower())
            self.assertNotIn("rank #1", body.lower())
            self.assertNotIn("revenue", body.lower())
            self.assertNotIn("urgent", body.lower())
            # Positive checks
            self.assertIn(lead["company_name"], body)
            self.assertTrue(str(lead["rating"]) in body or lead["company_name"] in body)

    # 11. Website-development offer is used
    def test_11_website_development_offer_used(self):
        """11. Core positioning strictly uses website development proposition."""
        for lead in self.batch_data.get("active_batch", []):
            self.assertEqual(lead["offer"], CORE_OFFER)
            self.assertIn(CORE_OFFER, lead["draft_body"])

    # 12. Draft version is WEBSITE_001
    def test_12_draft_version_is_website_001(self):
        """12. Draft version is universally WEBSITE_001 across batch."""
        self.assertEqual(self.batch_data["draft_version"], DRAFT_VERSION)
        for lead in self.batch_data.get("active_batch", []):
            self.assertEqual(lead["draft_version"], DRAFT_VERSION)
            self.assertEqual(lead["outcome_tracking"]["draft_version"], DRAFT_VERSION)

    # 13. No campaign is created
    def test_13_no_campaign_created(self):
        """13. Zero automated campaigns armed in campaigns.json for batch leads."""
        with open(CAMPAIGNS_PATH, "r", encoding="utf-8") as f:
            cdata = json.load(f)
        # Verify no campaign exists for any of the batch leads
        batch_lead_ids = {l["lead_id"] for l in self.batch_data.get("active_batch", [])}
        for camp in cdata:
            self.assertNotIn(camp.get("campaign_id"), ["BATCH-20261004-WEBSITE-001", "WEBSITE_001"])
            for lid in batch_lead_ids:
                self.assertNotIn(lid, camp.get("selected_lead_ids", []))
        self.assertEqual(self.batch_data["safety_invariants"]["campaigns_armed"], 0)

    # 14. No message history mutation
    def test_14_no_message_history_mutation(self):
        """14. Zero premature message history mutations in message_history.json."""
        with open(MESSAGES_PATH, "r", encoding="utf-8") as f:
            mdata = json.load(f)
        # Should not contain any message records for the 3 active leads
        for lead in self.batch_data.get("active_batch", []):
            self.assertNotIn(lead["lead_id"], mdata)
        self.assertEqual(self.batch_data["safety_invariants"]["message_history_mutated"], 0)

    # 15. No outreach send
    def test_15_no_outreach_send(self):
        """15. OUTREACH_SENDS = 0 strictly maintained."""
        self.assertEqual(self.batch_data["safety_invariants"]["outreach_sends"], 0)
        for lead in self.batch_data.get("active_batch", []):
            self.assertEqual(lead["outreach_status"], "NOT_READY")

    # 16. Duplicate businesses are impossible
    def test_16_duplicate_businesses_are_impossible(self):
        """16. CRM cache and batch active queue enforce strict deduplication."""
        names_crm = [l["company_name"].strip().lower() for l in self.crm_data.get("leads", [])]
        self.assertEqual(len(names_crm), len(set(names_crm)), "CRM cache contains duplicate company names")

        names_batch = [l["company_name"].strip().lower() for l in self.batch_data.get("active_batch", [])]
        self.assertEqual(len(names_batch), len(set(names_batch)), "Batch active leads contains duplicate company names")
        self.assertEqual(self.batch_data["safety_invariants"]["duplicates_created"], 0)

    # 17. Dashboard shows correct active batch
    def test_17_dashboard_shows_correct_active_batch(self):
        """17. Local API endpoint /api/manual-outreach/batch-8-7 returns correct 4-lead manual batch."""
        res = self.client.get("/api/manual-outreach/batch-8-7")
        self.assertEqual(res.status_code, 200)
        payload = res.json()

        summary = payload.get("cohort_summary", {})
        self.assertEqual(summary.get("total_active_leads"), 4)
        self.assertEqual(summary.get("automated_sendable"), 0)
        self.assertEqual(summary.get("manual_outreach_ready"), 4)
        self.assertEqual(summary.get("drafts_approved"), 4)

        batch = payload.get("active_batch", [])
        self.assertEqual(len(batch), 4)
        names = {b["company_name"] for b in batch}
        self.assertEqual(names, {"Dog and Partridge", "Ducie Arms", "The Old Monkey", "Live Seafood Ltd"})

        # Check frontend template contains batch UI elements
        res_ui = self.client.get("/")
        self.assertEqual(res_ui.status_code, 200)
        self.assertIn("phase87BatchModal", res_ui.text)
        self.assertIn("openPhase87BatchModal", res_ui.text)
        self.assertIn("batch87Btn", res_ui.text)

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 8.7.1: Canonical CRM Identity Repair Tests (Objective H)
    # ──────────────────────────────────────────────────────────────────────────
    # 18. LEADS.lead_id cannot contain a research ID
    def test_18_leads_lead_id_cannot_contain_research_id(self):
        """18. LEADS.lead_id cannot contain a research ID (RES-*)."""
        crm_leads = self.crm_data.get("leads", [])
        for lead in crm_leads:
            lid = lead.get("lead_id", "")
            self.assertFalse(lid.startswith("RES-"), f"Lead {lead.get('company_name')} has invalid research ID in lead_id: {lid}")
            self.assertTrue(lid.startswith("LEAD-MAN-"), f"Lead {lead.get('company_name')} has non-canonical lead_id: {lid}")

    # 19. research_id and lead_id remain distinct
    def test_19_research_id_and_lead_id_remain_distinct(self):
        """19. research_id and lead_id remain separate and distinct fields."""
        promoted_names = {"Dog and Partridge", "Ducie Arms", "The Old Monkey"}
        crm_leads = {l["company_name"]: l for l in self.crm_data.get("leads", [])}
        batch_leads = {l["company_name"]: l for l in self.batch_data.get("active_batch", [])}

        for name in promoted_names:
            c = crm_leads[name]
            b = batch_leads[name]
            self.assertTrue(c["lead_id"].startswith("LEAD-MAN-"))
            self.assertTrue(c["research_id"].startswith("RES-"))
            self.assertNotEqual(c["lead_id"], c["research_id"])
            self.assertEqual(b["lead_id"], c["lead_id"])
            self.assertEqual(b["research_id"], c["research_id"])

    # 20. Three promoted businesses resolve to one canonical identity each
    def test_20_three_promoted_businesses_resolve_to_one_canonical_identity(self):
        """20. Three promoted businesses resolve to exactly one canonical LEAD-MAN identity each."""
        crm_leads = {l["company_name"]: l for l in self.crm_data.get("leads", [])}
        p86_leads = {l["company_name"]: l for l in self.results_data.get("active_outreach_queue", [])}
        batch_leads = {l["company_name"]: l for l in self.batch_data.get("active_batch", [])}

        expected_ids = {
            "Dog and Partridge": "LEAD-MAN-4098E1",
            "Ducie Arms": "LEAD-MAN-525524",
            "The Old Monkey": "LEAD-MAN-3B9091",
        }

        for name, expected_id in expected_ids.items():
            self.assertEqual(crm_leads[name]["lead_id"], expected_id)
            self.assertEqual(p86_leads[name]["lead_id"], expected_id)
            self.assertEqual(batch_leads[name]["lead_id"], expected_id)

    # 21. Live Seafood keeps LEAD-MAN-0363CF
    def test_21_live_seafood_protection(self):
        """21. Live Seafood Ltd keeps LEAD-MAN-0363CF, OUTREACH_READY, and historical SENT preserved."""
        crm_leads = {l["company_name"]: l for l in self.crm_data.get("leads", [])}
        self.assertIn("Live Seafood Ltd", crm_leads)
        live_seafood = crm_leads["Live Seafood Ltd"]
        self.assertEqual(live_seafood["lead_id"], "LEAD-MAN-0363CF")
        self.assertEqual(live_seafood.get("research_id"), "RES-75541E")
        self.assertEqual(live_seafood["qualification_state"], "OUTREACH_READY")
        self.assertEqual(live_seafood["outreach_mode"], "MANUAL")

    # 21. Live Seafood keeps LEAD-MAN-0363CF
    def test_21_live_seafood_protection(self):
        """21. Live Seafood Ltd keeps LEAD-MAN-0363CF, OUTREACH_READY, and authoritative NOT_READY."""
        crm_leads = {l["company_name"]: l for l in self.crm_data.get("leads", [])}
        self.assertIn("Live Seafood Ltd", crm_leads)
        live_seafood = crm_leads["Live Seafood Ltd"]
        self.assertEqual(live_seafood["lead_id"], "LEAD-MAN-0363CF")
        self.assertEqual(live_seafood.get("research_id"), "RES-75541E")
        self.assertEqual(live_seafood["qualification_state"], "OUTREACH_READY")
        self.assertEqual(live_seafood["outreach_mode"], "MANUAL")
        self.assertEqual(live_seafood["outreach_status"], "NOT_READY")
        self.assertEqual(live_seafood.get("send_classification"), "NEVER_CONFIRMED_SENT")
        self.assertFalse(live_seafood.get("actual_send_confirmed"))
        self.assertEqual(live_seafood.get("authoritative_outreach_status"), "NOT_READY")

    # 22. Active 8.7 batch contains exactly four unsent leads
    def test_22_active_batch_contains_exactly_four_unsent_leads(self):
        """22. Active 8.7 batch strictly contains 4 unsent leads including reconciled Live Seafood."""
        active = self.batch_data.get("active_batch", [])
        self.assertEqual(len(active), 4)
        names = {l["company_name"] for l in active}
        self.assertEqual(names, {"Dog and Partridge", "Ducie Arms", "The Old Monkey", "Live Seafood Ltd"})
        for l in active:
            self.assertEqual(l["qualification_state"], "OUTREACH_READY")
            self.assertEqual(l["outreach_status"], "NOT_READY")
            self.assertEqual(l["outreach_mode"], "MANUAL")
            self.assertTrue(l["lead_id"].startswith("LEAD-MAN-"))
            self.assertTrue(l["research_id"].startswith("RES-"))

    # 23. No outreach state changes occur
    def test_23_no_outreach_state_changes_occur(self):
        """23. Invariants preserved: OUTREACH_SENDS=0, CAMPAIGNS_ARMED=0, MESSAGE_HISTORY_MUTATED=0."""
        invariants = self.batch_data.get("safety_invariants", {})
        self.assertEqual(invariants.get("outreach_sends"), 0)
        self.assertEqual(invariants.get("campaigns_armed"), 0)
        self.assertEqual(invariants.get("message_history_mutated"), 0)
        self.assertEqual(invariants.get("fabricated_recipient_ids"), 0)
        self.assertEqual(invariants.get("duplicates_created"), 0)

    # 24. No duplicate records exist
    def test_24_no_duplicate_records_exist(self):
        """24. Zero duplicates across canonical identity, name/address, lead ID, or research ID."""
        crm_leads = self.crm_data.get("leads", [])
        lead_ids = [l["lead_id"] for l in crm_leads]
        self.assertEqual(len(lead_ids), len(set(lead_ids)), "Duplicate lead_id found in LEADS")
        names = [l["company_name"].strip().lower() for l in crm_leads]
        self.assertEqual(len(names), len(set(names)), "Duplicate company_name found in LEADS")
        research_ids = [l.get("research_id") for l in crm_leads if l.get("research_id")]
        self.assertEqual(len(research_ids), len(set(research_ids)), "Duplicate research_id found in LEADS")

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 8.7.2: Live Seafood Reconciliation Tests (Objective G)
    # ──────────────────────────────────────────────────────────────────────────

    # 25. CRM/report SENT disagreement prevented
    def test_25_prevent_crm_report_sent_disagreement(self):
        """25. Prevents CRM/report SENT disagreement; all authoritative stores must be consistent."""
        crm_leads = {l["company_name"]: l for l in self.crm_data.get("leads", [])}
        ls_crm = crm_leads.get("Live Seafood Ltd", {})

        # Authoritative CRM status is strictly NOT_READY
        self.assertEqual(ls_crm.get("outreach_status"), "NOT_READY")
        self.assertEqual(ls_crm.get("qualification_state"), "OUTREACH_READY")
        self.assertEqual(ls_crm.get("send_classification"), "NEVER_CONFIRMED_SENT")
        self.assertFalse(ls_crm.get("actual_send_confirmed"))

        # Batch report summary must agree with authoritative CRM status
        summary = self.batch_data.get("cohort_summary", {})
        self.assertEqual(summary.get("live_seafood_authoritative_status"), "NOT_READY")
        self.assertFalse(summary.get("live_seafood_actual_send_confirmed"))
        self.assertEqual(summary.get("live_seafood_classification"), "NEVER_CONFIRMED_SENT")
        self.assertTrue(summary.get("live_seafood_in_active_queue"))

    # 26. False SENT state from preparation artifacts prevented
    def test_26_prevent_false_sent_state_from_preparation_artifacts(self):
        """26. Preparation artifacts alone cannot mark a lead as SENT without authoritative send confirmation."""
        # Evidence Hierarchy: preparation artifacts alone are NOT evidence of an actual send
        confirmed = self.engine.is_actual_send_confirmed("LEAD-MAN-0363CF")
        self.assertFalse(confirmed)

    # 27. Duplicate manual send records prevented
    def test_27_prevent_duplicate_manual_send_records(self):
        """27. Duplicate manual send confirmations are strictly blocked."""
        from lib.outreach.manual_outreach_controller import confirm_manual_send
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf_leads:
            test_leads = {
                "leads": [
                    {
                        "lead_id": "LEAD-MAN-0363CF",
                        "company_name": "Live Seafood Ltd",
                        "qualification_state": "OUTREACH_READY",
                        "outreach_status": "SENT",
                        "outreach_sent_at": "2026-10-03T16:00:00Z",
                    }
                ]
            }
            json.dump(test_leads, tf_leads)
            tf_leads_path = tf_leads.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tf_audit:
            tf_audit_path = tf_audit.name

        try:
            res = confirm_manual_send(
                lead_id="LEAD-MAN-0363CF",
                operator_confirmed=True,
                leads_path=tf_leads_path,
                audit_path=tf_audit_path,
            )
            self.assertFalse(res.get("success"))
            self.assertIn("already marked sent", res.get("message", "").lower())

            with open(tf_audit_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            send_events = [line for line in lines if "SEND_CONFIRMED" in line]
            self.assertEqual(len(send_events), 0)
        finally:
            if os.path.exists(tf_leads_path):
                os.remove(tf_leads_path)
            if os.path.exists(tf_audit_path):
                os.remove(tf_audit_path)

    # 28. Live Seafood entering active queue after confirmed SENT prevented
    def test_28_prevent_live_seafood_entering_active_queue_after_confirmed_sent(self):
        """28. Live Seafood is strictly excluded from active queue if confirmed SENT."""
        import tempfile

        # Test 1: If CRM status is SENT
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            mock_crm = {
                "leads": [
                    {
                        "lead_id": "LEAD-MAN-0363CF",
                        "company_name": "Live Seafood Ltd",
                        "qualification_state": "OUTREACH_READY",
                        "outreach_status": "SENT",
                        "outreach_sent_at": "2026-10-03T16:00:00Z",
                        "contactability_status": "PARTIALLY_CONTACTABLE",
                    }
                ]
            }
            json.dump(mock_crm, tf)
            mock_crm_path = tf.name

        try:
            test_engine = Phase87OutreachBatchEngine(leads_cache_path=mock_crm_path)
            cohort = test_engine.load_cohort()
            names = [l["company_name"] for l in cohort]
            self.assertNotIn("Live Seafood Ltd", names)
            self.assertNotIn("LEAD-MAN-0363CF", [l["lead_id"] for l in cohort])
        finally:
            if os.path.exists(mock_crm_path):
                os.remove(mock_crm_path)

        # Test 2: If audit log has explicit SEND_CONFIRMED event
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tf_audit:
            audit_entry = {
                "timestamp": "2026-10-04T12:00:00Z",
                "action": "SEND_CONFIRMED",
                "lead_id": "LEAD-MAN-0363CF",
                "operator": "HUMAN_OPERATOR",
                "details": {},
            }
            tf_audit.write(json.dumps(audit_entry) + "\n")
            mock_audit_path = tf_audit.name

        try:
            test_engine = Phase87OutreachBatchEngine(audit_path=mock_audit_path)
            self.assertTrue(test_engine.is_actual_send_confirmed("LEAD-MAN-0363CF"))
            cohort = test_engine.load_cohort()
            names = [l["company_name"] for l in cohort]
            self.assertNotIn("Live Seafood Ltd", names)
        finally:
            if os.path.exists(mock_audit_path):
                os.remove(mock_audit_path)


if __name__ == "__main__":
    unittest.main()

