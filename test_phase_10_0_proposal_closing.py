"""
Test Suite: Phase 10.0 Proposal + Deal Closing Workspace

Validates all 36 sections of Phase 10.0:
  1. Proposal Manager & 9 Commercial Proposal States (DRAFT, INTERNAL_REVIEW, READY_TO_SEND, SENT, VIEWED, NEGOTIATING, ACCEPTED, REJECTED, EXPIRED)
  2. Proposal Data Model & Strict Field Validation
  3. Website Proposal Template (10 Canonical Sections, No Unsubstantiated Claims)
  4. Personalization Claims Validation (Rejects Guaranteed Sales, SEO, Growth)
  5. Scope Builder & Exclusions Preservation
  6. Package System (Configurable Pricing via proposal_packages.json: CUSTOM, STARTER, STANDARD, PREMIUM)
  7. Deterministic Pricing Calculations (Total >= 0, Discount >= 0, Discount <= Subtotal)
  8. Operator-Entered Payment Terms
  9. Timeline Structuring
  10. Revision Policy
  11. Scope Exclusions Separation
  12. Proposal Generation & Workflow Controls (Draft only, no automatic sends)
  13. Proposal Preview (Inspect Complete Offer Before Sending)
  14. Shareable / Printable HTML Output
  15. Mark Proposal Sent (Strict Operator Confirmation Required)
  16. Proposal Versioning & Immutability (Sent Proposals Immutable, v1 -> v2)
  17. Negotiation Tracking (Objections, Proposed Changes Separate from Original Pricing)
  18. Deal Value Tracking (Quoted vs Final Agreed Value)
  19. Deal Won Workflow (Agreed Value > 0, Start Date, Payment Terms, Operator Confirmed)
  20. Deal Lost Workflow (Mandatory Reason, Historical Data Preserved)
  21. Commercial Next Actions Engine
  22-25. Existing Lead State Preservation (The Old Monkey, Manchester Shawarma, Little Aladdin, Dog and Partridge, Live Seafood, Seoul Kimchi, Hong Thai)
  26. Commercial Dashboard & Proposal Pipeline Table
  27. Proposal Analytics & Small-Sample Guardrails (n < 30 Warning)
  28. Commercial Automation Safety Invariants (AUTO_* = False)
  29. Data Integrity Validator
  30. Append-Only Commercial Event History
  31-36. Server API Endpoints & Operational Invariants
"""

import os
import json
import shutil
import tempfile
import unittest
from fastapi.testclient import TestClient

from server import app
from lib.commercial.models import (
    CommercialStage,
    OutcomeProvenance,
    ProposalState,
    ProposalPackage,
    LostReason,
    VALID_LOST_REASONS,
    CommercialEventType,
    CommercialEvent,
    AUTO_PROPOSAL,
    AUTO_SEND_PROPOSAL,
    AUTO_EMAIL_PROPOSAL,
    AUTO_FOLLOWUP,
    AUTO_PAYMENT_REQUEST,
    AUTO_CONTRACT,
)
from lib.commercial.proposal_manager import ProposalManager
from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager


class TestPhase10ProposalClosing(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.leads_path = os.path.join(self.test_dir, "cache_sheets_leads.json")
        self.outcomes_path = os.path.join(self.test_dir, "outreach_outcomes.json")
        self.timelines_path = os.path.join(self.test_dir, "lead_timelines.json")
        self.commercial_records_path = os.path.join(self.test_dir, "commercial_records.json")
        self.commercial_events_path = os.path.join(self.test_dir, "commercial_events.json")
        self.packages_path = os.path.join(self.test_dir, "proposal_packages.json")
        self.proposals_path = os.path.join(self.test_dir, "proposals.json")
        self.snapshot_path = os.path.join(self.test_dir, "proposal_pipeline_snapshot.json")

        prod_data_dir = os.path.join(os.path.dirname(__file__), "data")
        for fn, dst in [
            ("cache_sheets_leads.json", self.leads_path),
            ("outreach_outcomes.json", self.outcomes_path),
            ("lead_timelines.json", self.timelines_path),
            ("commercial_records.json", self.commercial_records_path),
            ("commercial_events.json", self.commercial_events_path),
            ("proposal_packages.json", self.packages_path),
        ]:
            src = os.path.join(prod_data_dir, fn)
            if os.path.exists(src):
                shutil.copyfile(src, dst)
            else:
                with open(dst, "w") as f:
                    json.dump([] if fn.endswith("s.json") else {}, f)

        # Proposals file starts empty
        # Set environment variables for FastAPI client isolation
        os.environ["PROPOSALS_PATH"] = self.proposals_path
        os.environ["PACKAGES_PATH"] = self.packages_path
        os.environ["LEADS_PATH"] = self.leads_path
        os.environ["COMMERCIAL_RECORDS_PATH"] = self.commercial_records_path
        os.environ["COMMERCIAL_EVENTS_PATH"] = self.commercial_events_path
        os.environ["TIMELINES_PATH"] = self.timelines_path
        os.environ["PROPOSAL_SNAPSHOT_PATH"] = self.snapshot_path

        self.pm = ProposalManager(
            proposals_path=self.proposals_path,
            packages_path=self.packages_path,
            leads_path=self.leads_path,
            commercial_records_path=self.commercial_records_path,
            commercial_events_path=self.commercial_events_path,
            timelines_path=self.timelines_path,
            snapshot_path=self.snapshot_path,
        )
        self.client = TestClient(app)
        try:
            from lib.system.system_config import SystemConfig
            self._prev_travel_mode = SystemConfig.TRAVEL_MODE
            self._prev_comm_enabled = SystemConfig.COMMERCIAL_ACTIONS_ENABLED
            SystemConfig.TRAVEL_MODE = False
            SystemConfig.COMMERCIAL_ACTIONS_ENABLED = True
        except ImportError:
            pass

    def tearDown(self):
        try:
            from lib.system.system_config import SystemConfig
            SystemConfig.TRAVEL_MODE = getattr(self, "_prev_travel_mode", True)
            SystemConfig.COMMERCIAL_ACTIONS_ENABLED = getattr(self, "_prev_comm_enabled", False)
        except ImportError:
            pass
        for k in ["PROPOSALS_PATH", "PACKAGES_PATH", "LEADS_PATH", "COMMERCIAL_RECORDS_PATH", "COMMERCIAL_EVENTS_PATH", "TIMELINES_PATH", "PROPOSAL_SNAPSHOT_PATH"]:
            os.environ.pop(k, None)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Proposal Creation & Pricing Validation
    # -------------------------------------------------------------------------
    def test_01_valid_proposal_created(self):
        """Creates a valid proposal with DRAFT status and correct defaults."""
        res = self.pm.create_proposal(
            lead_id="LEAD-MAN-3B9091",
            company_name="The Old Monkey",
            package_name="STANDARD",
            subtotal=1250.0,
            discount=100.0,
            currency="GBP",
            scope=["Responsive website", "Home page", "Location"],
            deliverables=["Production website", "SSL certificate"],
            exclusions=["Domain registration", "Professional photography"],
            operator_notes="Verified pub in Manchester city centre without direct website."
        )
        self.assertTrue(res["success"])
        p = res["proposal"]
        self.assertTrue(p["proposal_id"].startswith("PROP-"))
        self.assertEqual(p["status"], ProposalState.DRAFT.value)
        self.assertEqual(p["subtotal"], 1250.0)
        self.assertEqual(p["discount"], 100.0)
        self.assertEqual(p["total"], 1150.0)
        self.assertEqual(p["version"], 1)

    def test_02_required_fields_validated_missing_lead_id(self):
        """Proposal creation without lead_id must be rejected."""
        with self.assertRaises(ValueError) as ctx:
            self.pm.create_proposal(
                lead_id="",
                company_name="Test Pub",
                subtotal=1000.0
            )
        self.assertIn("lead_id is mandatory", str(ctx.exception))

    def test_03_pricing_calculation_correct(self):
        """Pricing calculation deterministic: subtotal - discount = total."""
        pricing = self.pm.calculate_pricing(subtotal=1500.0, discount=250.0)
        self.assertEqual(pricing["subtotal"], 1500.0)
        self.assertEqual(pricing["discount"], 250.0)
        self.assertEqual(pricing["total"], 1250.0)

    def test_04_discount_cannot_exceed_subtotal(self):
        """Discount cannot exceed subtotal (total cannot be negative)."""
        with self.assertRaises(ValueError) as ctx:
            self.pm.calculate_pricing(subtotal=500.0, discount=600.0)
        self.assertIn("cannot exceed subtotal", str(ctx.exception))

    def test_05_discount_cannot_be_negative(self):
        """Negative discounts must be rejected."""
        with self.assertRaises(ValueError) as ctx:
            self.pm.calculate_pricing(subtotal=500.0, discount=-50.0)
        self.assertIn("cannot be negative", str(ctx.exception))

    def test_06_currency_required_and_standardized(self):
        """Currency must be a 3-character uppercase ISO code."""
        pricing = self.pm.calculate_pricing(subtotal=1000.0, discount=0.0, currency="gbp")
        self.assertEqual(pricing["currency"], "GBP")

        with self.assertRaises(ValueError):
            self.pm.calculate_pricing(subtotal=1000.0, discount=0.0, currency="")

    def test_07_scope_and_exclusions_preserved(self):
        """Scope and exclusions lists are preserved exactly as entered."""
        scope = ["Responsive website", "Menu / Services", "WhatsApp link"]
        exclusions = ["Copywriting", "Ongoing maintenance"]
        res = self.pm.create_proposal(
            lead_id="LEAD-MAN-3B9091",
            scope=scope,
            exclusions=exclusions,
            subtotal=750.0
        )
        p = res["proposal"]
        self.assertEqual(p["scope"], scope)
        self.assertEqual(p["exclusions"], exclusions)

    # -------------------------------------------------------------------------
    # 2. Personalization & Claim Sanitization (Section 3 & 4)
    # -------------------------------------------------------------------------
    def test_08_personalization_rejects_guaranteed_sales(self):
        """Claims guaranteeing sales are strictly forbidden."""
        valid, violations = self.pm.validate_personalization_claims("Our website guarantees sales increases.")
        self.assertFalse(valid)
        self.assertTrue(any("guaranteed sales" in v for v in violations))

    def test_09_personalization_rejects_guaranteed_seo_and_growth(self):
        """Claims guaranteeing SEO rankings or revenue growth are rejected."""
        valid, violations = self.pm.validate_personalization_claims("Includes guaranteed SEO ranking on Google page 1.")
        self.assertFalse(valid)
        self.assertTrue(any("guaranteed SEO" in v for v in violations))

        with self.assertRaises(ValueError):
            self.pm.create_proposal(
                lead_id="LEAD-MAN-3B9091",
                operator_notes="Guaranteed growth for your pub."
            )

    def test_10_packages_loaded_from_configuration(self):
        """Packages are loaded from proposal_packages.json without hardcoded pricing."""
        pkgs = self.pm.load_packages()
        self.assertIn("packages", pkgs)
        packages = pkgs["packages"]
        self.assertIn("STARTER", packages)
        self.assertIn("STANDARD", packages)
        self.assertIn("PREMIUM", packages)
        self.assertIn("CUSTOM", packages)
        self.assertEqual(packages["STARTER"]["price"], 750.0)
        self.assertEqual(packages["STANDARD"]["price"], 1250.0)
        self.assertEqual(packages["PREMIUM"]["price"], 1950.0)

    def test_11_canonical_10_sections_in_preview(self):
        """Proposal preview renders all 10 canonical sections without unsupported claims."""
        res = self.pm.create_proposal(
            lead_id="LEAD-MAN-3B9091",
            company_name="The Old Monkey",
            subtotal=1250.0
        )
        prop_id = res["proposal_id"]
        prev = self.pm.render_proposal_preview(prop_id)
        sections = prev["sections"]
        self.assertEqual(len(sections), 10)
        titles = [s["title"] for s in sections]
        expected_titles = [
            "Business Understanding",
            "Recommended Website",
            "Scope of Work",
            "Deliverables",
            "Estimated Project Timeline",
            "Commercial Investment",
            "Payment Terms",
            "Revisions Policy",
            "What Is Not Included",
            "Next Step"
        ]
        self.assertEqual(titles, expected_titles)

    # -------------------------------------------------------------------------
    # 3. Versioning & Immutability (Section 16)
    # -------------------------------------------------------------------------
    def test_12_sent_proposal_is_immutable(self):
        """A proposal in SENT state cannot be modified in place."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1000.0)
        pid = res["proposal_id"]
        self.pm.mark_proposal_sent(pid, delivery_method="WHATSAPP", operator_confirmed=True)

        with self.assertRaises(PermissionError) as ctx:
            self.pm.update_proposal(pid, updates={"subtotal": 800.0})
        self.assertIn("immutable", str(ctx.exception))

    def test_13_accepted_proposal_is_immutable(self):
        """An accepted proposal cannot be modified in place."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1250.0)
        pid = res["proposal_id"]
        self.pm.mark_proposal_sent(pid, delivery_method="WHATSAPP", operator_confirmed=True)
        self.pm.mark_deal_won(pid, agreed_value=1250.0, start_date="2026-10-15", payment_terms="50/50", operator_confirmed=True)

        with self.assertRaises(PermissionError):
            self.pm.update_proposal(pid, updates={"total": 900.0})

    def test_14_rejected_proposal_is_immutable(self):
        """A rejected proposal cannot be modified in place."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1250.0)
        pid = res["proposal_id"]
        self.pm.mark_proposal_sent(pid, delivery_method="EMAIL", operator_confirmed=True)
        self.pm.mark_deal_lost(pid, reason="PRICE", operator_notes="Client stated budget was lower")

        with self.assertRaises(PermissionError):
            self.pm.update_proposal(pid, updates={"subtotal": 1100.0})

    def test_15_revised_proposal_creates_new_version(self):
        """create_revision creates a new proposal version (v2) for a sent proposal."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1250.0)
        pid_v1 = res["proposal_id"]
        self.pm.mark_proposal_sent(pid_v1, delivery_method="WHATSAPP", operator_confirmed=True)

        rev_res = self.pm.create_revision(
            proposal_id=pid_v1,
            updates={"subtotal": 1000.0, "discount": 100.0},
            change_reason="Negotiated discount with business owner"
        )
        self.assertTrue(rev_res["success"])
        p_v2 = rev_res["new_proposal"]
        self.assertEqual(p_v2["version"], 2)
        self.assertEqual(p_v2["supersedes_proposal_id"], pid_v1)
        self.assertEqual(p_v2["status"], ProposalState.DRAFT.value)
        self.assertEqual(p_v2["total"], 900.0)

    def test_16_supersession_recorded_in_new_version(self):
        """The original proposal records the supersession when a revision is created."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1500.0)
        pid_v1 = res["proposal_id"]
        self.pm.mark_proposal_sent(pid_v1, delivery_method="EMAIL", operator_confirmed=True)

        rev_res = self.pm.create_revision(pid_v1, updates={"subtotal": 1200.0}, change_reason="Budget scope adjustment")
        pid_v2 = rev_res["new_proposal"]["proposal_id"]

        proposals = self.pm.load_proposals()
        p1 = next(p for p in proposals if p["proposal_id"] == pid_v1)
        self.assertEqual(p1["status"], ProposalState.EXPIRED.value)
        self.assertEqual(p1["superseded_by_proposal_id"], pid_v2)

    # -------------------------------------------------------------------------
    # 4. Sending & Transition Controls (Section 12, 13, 15)
    # -------------------------------------------------------------------------
    def test_17_preview_does_not_send_or_change_status(self):
        """Rendering a preview does not change proposal status or send messages."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1000.0)
        pid = res["proposal_id"]
        self.pm.render_proposal_preview(pid)

        proposals = self.pm.load_proposals()
        p = next(x for x in proposals if x["proposal_id"] == pid)
        self.assertEqual(p["status"], ProposalState.DRAFT.value)
        self.assertIsNone(p["sent_at"])

    def test_18_proposal_creation_does_not_send_remains_draft(self):
        """Creating a proposal leaves it strictly in DRAFT status."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1000.0)
        self.assertEqual(res["proposal"]["status"], ProposalState.DRAFT.value)
        self.assertIsNone(res["proposal"]["sent_at"])

    def test_19_approve_proposal_transitions_to_ready_to_send(self):
        """Approving proposal marks it READY_TO_SEND without dispatching anything."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1000.0)
        pid = res["proposal_id"]
        appr = self.pm.approve_proposal(pid)
        self.assertTrue(appr["success"])

        proposals = self.pm.load_proposals()
        p = next(x for x in proposals if x["proposal_id"] == pid)
        self.assertEqual(p["status"], ProposalState.READY_TO_SEND.value)
        self.assertIsNone(p["sent_at"])

    def test_20_mark_sent_requires_operator_confirmation(self):
        """mark_proposal_sent fails if operator_confirmed is False."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1000.0)
        pid = res["proposal_id"]

        with self.assertRaises(ValueError) as ctx:
            self.pm.mark_proposal_sent(pid, delivery_method="WHATSAPP", operator_confirmed=False)
        self.assertIn("explicit operator confirmation", str(ctx.exception))

    def test_21_mark_sent_records_delivery_metadata_and_syncs_commercial_stage(self):
        """mark_proposal_sent updates proposal to SENT and commercial_stage to PROPOSAL_SENT."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1250.0)
        pid = res["proposal_id"]

        sent_res = self.pm.mark_proposal_sent(
            pid,
            delivery_method="WHATSAPP",
            operator="HUMAN_OPERATOR",
            operator_notes="Delivered via direct message per manager request",
            operator_confirmed=True
        )
        self.assertTrue(sent_res["success"])
        self.assertEqual(sent_res["status"], ProposalState.SENT.value)
        self.assertEqual(sent_res["commercial_stage"], CommercialStage.PROPOSAL_SENT.value)

        # Check records synced
        records = self.pm.load_commercial_records()
        self.assertEqual(records["LEAD-MAN-3B9091"]["commercial_stage"], CommercialStage.PROPOSAL_SENT.value)

    def test_22_commercial_timeline_event_appended_on_sent(self):
        """A PROPOSAL_SENT event is appended to lead_timelines.json upon sending."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1250.0)
        pid = res["proposal_id"]
        self.pm.mark_proposal_sent(pid, delivery_method="EMAIL", operator_confirmed=True)

        with open(self.timelines_path, "r") as f:
            timelines = json.load(f)
        events = timelines.get("LEAD-MAN-3B9091", [])
        sent_events = [e for e in events if e.get("new_state") == CommercialStage.PROPOSAL_SENT.value]
        self.assertGreaterEqual(len(sent_events), 1)

    # -------------------------------------------------------------------------
    # 5. Commercial Stages & Explicit Transitions (Section 15, 17, 19, 20)
    # -------------------------------------------------------------------------
    def test_23_proposal_requested_stage_explicit_only(self):
        """Proposal requested commercial stage requires explicit operator action."""
        cpm = CommercialPipelineManager(
            leads_path=self.leads_path,
            commercial_records_path=self.commercial_records_path
        )
        res = cpm.record_proposal(
            lead_id="LEAD-MAN-3B9091",
            proposal_status="REQUESTED",
            amount=1250.0,
            operator="HUMAN_OPERATOR"
        )
        self.assertTrue(res["success"])
        recs = self.pm.load_commercial_records()
        self.assertEqual(recs["LEAD-MAN-3B9091"]["commercial_stage"], CommercialStage.PROPOSAL_REQUESTED.value)

    def test_24_proposal_sent_stage_explicit_only(self):
        """Proposal sent stage only occurs when mark_proposal_sent is executed."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1250.0)
        pid = res["proposal_id"]
        recs = self.pm.load_commercial_records()
        self.assertNotEqual(recs["LEAD-MAN-3B9091"].get("commercial_stage"), CommercialStage.PROPOSAL_SENT.value)

        self.pm.mark_proposal_sent(pid, delivery_method="PHONE", operator_confirmed=True)
        recs = self.pm.load_commercial_records()
        self.assertEqual(recs["LEAD-MAN-3B9091"]["commercial_stage"], CommercialStage.PROPOSAL_SENT.value)

    def test_25_negotiation_stage_explicit_only(self):
        """Negotiation stage only occurs when negotiation is explicitly recorded."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1250.0)
        pid = res["proposal_id"]
        self.pm.mark_proposal_sent(pid, delivery_method="WHATSAPP", operator_confirmed=True)

        neg = self.pm.record_negotiation(
            proposal_id=pid,
            note_type="scope request",
            details="Client asked to include WhatsApp booking button in initial scope",
            operator="HUMAN_OPERATOR"
        )
        self.assertTrue(neg["success"])
        recs = self.pm.load_commercial_records()
        self.assertEqual(recs["LEAD-MAN-3B9091"]["commercial_stage"], CommercialStage.NEGOTIATING.value)

    def test_26_won_stage_explicit_only(self):
        """Won stage requires explicit operator close and does not happen automatically."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1250.0)
        pid = res["proposal_id"]
        self.pm.mark_proposal_sent(pid, delivery_method="IN_PERSON", operator_confirmed=True)

        won_res = self.pm.mark_deal_won(
            proposal_id=pid,
            agreed_value=1250.0,
            currency="GBP",
            start_date="2026-10-12",
            payment_terms="50% deposit upfront, 50% on completion",
            operator_confirmed=True
        )
        self.assertTrue(won_res["success"])
        recs = self.pm.load_commercial_records()
        self.assertEqual(recs["LEAD-MAN-3B9091"]["commercial_stage"], CommercialStage.WON.value)

    def test_27_lost_stage_explicit_only(self):
        """Lost stage requires explicit operator action with a valid reason."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1250.0)
        pid = res["proposal_id"]
        self.pm.mark_proposal_sent(pid, delivery_method="EMAIL", operator_confirmed=True)

        lost_res = self.pm.mark_deal_lost(pid, reason="TIMING", operator_notes="Renovation ongoing until Q1 2027")
        self.assertTrue(lost_res["success"])
        recs = self.pm.load_commercial_records()
        self.assertEqual(recs["LEAD-MAN-3B9091"]["commercial_stage"], CommercialStage.LOST.value)

    def test_28_no_automatic_promotion_across_pipeline_stages(self):
        """Creating a proposal or preview does not automatically promote lead to WON or SENT."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1250.0)
        recs = self.pm.load_commercial_records()
        # Stage remains unchanged (PREVIEW_REQUESTED) until sent or updated
        self.assertEqual(recs["LEAD-MAN-3B9091"]["commercial_stage"], CommercialStage.PREVIEW_REQUESTED.value)

    # -------------------------------------------------------------------------
    # 6. Deal Closing (Won & Lost) Rules (Section 19 & 20)
    # -------------------------------------------------------------------------
    def test_29_mark_deal_won_requires_agreed_value_greater_than_zero(self):
        """mark_deal_won fails if agreed_value <= 0."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1000.0)
        pid = res["proposal_id"]
        with self.assertRaises(ValueError) as ctx:
            self.pm.mark_deal_won(pid, agreed_value=0.0, start_date="2026-10-15", payment_terms="50/50", operator_confirmed=True)
        self.assertIn("greater than zero", str(ctx.exception))

    def test_30_mark_deal_won_requires_mandatory_fields_and_confirmation(self):
        """mark_deal_won requires start_date, payment_terms, and operator_confirmed=True."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1000.0)
        pid = res["proposal_id"]

        # Missing confirmation
        with self.assertRaises(ValueError) as ctx:
            self.pm.mark_deal_won(pid, agreed_value=1000.0, start_date="2026-10-15", payment_terms="50/50", operator_confirmed=False)
        self.assertIn("operator confirmation", str(ctx.exception))

        # Missing start date
        with self.assertRaises(ValueError) as ctx:
            self.pm.mark_deal_won(pid, agreed_value=1000.0, start_date="", payment_terms="50/50", operator_confirmed=True)
        self.assertIn("start_date is mandatory", str(ctx.exception))

        # Missing payment terms
        with self.assertRaises(ValueError) as ctx:
            self.pm.mark_deal_won(pid, agreed_value=1000.0, start_date="2026-10-15", payment_terms="", operator_confirmed=True)
        self.assertIn("payment_terms is mandatory", str(ctx.exception))

    def test_31_mark_deal_won_records_final_agreed_value_separately(self):
        """Won deals store final_agreed_value separately from quoted_value."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1500.0)
        pid = res["proposal_id"]
        self.pm.mark_proposal_sent(pid, delivery_method="WHATSAPP", operator_confirmed=True)

        self.pm.mark_deal_won(pid, agreed_value=1350.0, start_date="2026-10-15", payment_terms="50/50", operator_confirmed=True)
        proposals = self.pm.load_proposals()
        p = next(x for x in proposals if x["proposal_id"] == pid)
        self.assertEqual(p["quoted_value"], 1500.0)
        self.assertEqual(p["final_agreed_value"], 1350.0)

    def test_32_mark_deal_lost_requires_valid_reason(self):
        """mark_deal_lost accepts all reasons in VALID_LOST_REASONS."""
        valid_reasons = ["PRICE", "TIMING", "NO_NEED", "CHOSE_OTHER_PROVIDER", "NO_RESPONSE", "SCOPE_MISMATCH", "OTHER"]
        for r in valid_reasons:
            self.assertIn(r, VALID_LOST_REASONS)

        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1000.0)
        pid = res["proposal_id"]
        lost_res = self.pm.mark_deal_lost(pid, reason="PRICE", operator_notes="Budget cap £500")
        self.assertTrue(lost_res["success"])
        self.assertEqual(lost_res["reason"], "PRICE")

    def test_33_mark_deal_lost_rejects_invalid_reason(self):
        """mark_deal_lost rejects unrecognized lost reasons."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1000.0)
        pid = res["proposal_id"]
        with self.assertRaises(ValueError) as ctx:
            self.pm.mark_deal_lost(pid, reason="CLIENT_ANNOYING")
        self.assertIn("Invalid lost reason", str(ctx.exception))

    def test_34_mark_deal_lost_preserves_historical_lead_and_proposal_data(self):
        """Historical outreach, lead records, and proposal documents are never deleted on LOST."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1000.0)
        pid = res["proposal_id"]
        self.pm.mark_deal_lost(pid, reason="NO_NEED", operator_notes="Has Facebook page only")

        leads = self.pm.load_leads()
        old_monkey = next(l for l in leads if l["lead_id"] == "LEAD-MAN-3B9091")
        self.assertIsNotNone(old_monkey)

        proposals = self.pm.load_proposals()
        prop = next(p for p in proposals if p["proposal_id"] == pid)
        self.assertEqual(prop["status"], ProposalState.REJECTED.value)

    # -------------------------------------------------------------------------
    # 7. Negotiation Tracking (Section 17)
    # -------------------------------------------------------------------------
    def test_35_record_negotiation_creates_negotiation_entry(self):
        """record_negotiation stores proposed changes and objection details."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1250.0)
        pid = res["proposal_id"]
        self.pm.mark_proposal_sent(pid, delivery_method="PHONE", operator_confirmed=True)

        neg = self.pm.record_negotiation(
            proposal_id=pid,
            note_type="price objection",
            details="Manager counter-offered £1,000 for standard build",
            proposed_changes={"proposed_price": 1000.0},
            operator="HUMAN_OPERATOR"
        )
        self.assertTrue(neg["success"])
        entry = neg["entry"]
        self.assertEqual(entry["note_type"], "price objection")
        self.assertEqual(entry["proposed_changes"]["proposed_price"], 1000.0)

    def test_36_negotiation_preserves_original_quoted_pricing(self):
        """Recording a negotiation does not overwrite the proposal's original quoted subtotal or total."""
        res = self.pm.create_proposal(lead_id="LEAD-MAN-3B9091", subtotal=1250.0)
        pid = res["proposal_id"]
        self.pm.mark_proposal_sent(pid, delivery_method="PHONE", operator_confirmed=True)

        self.pm.record_negotiation(
            proposal_id=pid,
            note_type="price objection",
            details="Counter-offer £1,000",
            proposed_changes={"proposed_price": 1000.0}
        )

        proposals = self.pm.load_proposals()
        p = next(x for x in proposals if x["proposal_id"] == pid)
        self.assertEqual(p["subtotal"], 1250.0)
        self.assertEqual(p["total"], 1250.0)
        self.assertEqual(p["status"], ProposalState.NEGOTIATING.value)

    # -------------------------------------------------------------------------
    # 8. Existing Lead State Preservation (Section 22, 23, 24, 25, 31)
    # -------------------------------------------------------------------------
    def test_37_the_old_monkey_state_preserved(self):
        """The Old Monkey (LEAD-MAN-3B9091) remains in PREVIEW_REQUESTED."""
        records = self.pm.load_commercial_records()
        rec = records.get("LEAD-MAN-3B9091", {})
        self.assertEqual(rec.get("commercial_stage"), CommercialStage.PREVIEW_REQUESTED.value)

    def test_38_manchester_shawarma_callback_preserved_2026_10_06(self):
        """Manchester Shawarma preserves explicit callback scheduled for 2026-10-06 14:00."""
        records = self.pm.load_commercial_records()
        rec = records.get("LEAD-MAN-E81185", {})
        self.assertEqual(rec.get("commercial_stage"), CommercialStage.FOLLOW_UP_REQUIRED.value)
        follow_ups = rec.get("follow_ups", [])
        self.assertTrue(any("2026-10-06 14:00" in fu.get("scheduled_for", "") for fu in follow_ups))

    def test_39_little_aladdin_conversation_and_interest_preserved(self):
        """Little Aladdin preserves CONNECTED outcome and HIGH interest notes."""
        records = self.pm.load_commercial_records()
        rec = records.get("LEAD-MAN-902001", {})
        self.assertEqual(rec.get("commercial_stage"), CommercialStage.CONTACTED.value)
        self.assertEqual(rec.get("latest_outcome"), "CONNECTED")
        notes = rec.get("notes", {})
        self.assertEqual(notes.get("interest_level"), "HIGH")

    def test_40_dog_and_partridge_call_attempt_history_preserved(self):
        """Dog and Partridge preserves NO_ANSWER attempt #1 without deletion."""
        records = self.pm.load_commercial_records()
        rec = records.get("LEAD-MAN-4098E1", {})
        self.assertEqual(rec.get("commercial_stage"), CommercialStage.CONTACTED.value)
        self.assertEqual(rec.get("latest_outcome"), "NO_ANSWER")

    def test_41_live_seafood_protected(self):
        """Live Seafood (LEAD-MAN-682E5D) remains protected in QUALIFIED stage."""
        records = self.pm.load_commercial_records()
        rec = records.get("LEAD-MAN-682E5D", {})
        self.assertEqual(rec.get("commercial_stage"), CommercialStage.QUALIFIED.value)

    def test_42_seoul_kimchi_protected(self):
        """Seoul Kimchi (LEAD-MAN-44F10A) remains protected in QUALIFIED stage."""
        records = self.pm.load_commercial_records()
        rec = records.get("LEAD-MAN-44F10A", {})
        self.assertEqual(rec.get("commercial_stage"), CommercialStage.QUALIFIED.value)

    def test_43_hong_thai_protected_and_lost_status_preserved(self):
        """Hong Thai (LEAD-MAN-709C66) remains recorded as LOST without deletion."""
        records = self.pm.load_commercial_records()
        rec = records.get("LEAD-MAN-709C66", {})
        self.assertEqual(rec.get("commercial_stage"), CommercialStage.LOST.value)

    # -------------------------------------------------------------------------
    # 9. Automation Safety Invariants (Section 28)
    # -------------------------------------------------------------------------
    def test_44_safety_auto_proposal_is_false(self):
        """AUTO_PROPOSAL invariant must remain False."""
        self.assertFalse(AUTO_PROPOSAL)

    def test_45_safety_auto_send_proposal_is_false(self):
        """AUTO_SEND_PROPOSAL invariant must remain False."""
        self.assertFalse(AUTO_SEND_PROPOSAL)

    def test_46_safety_auto_email_proposal_is_false(self):
        """AUTO_EMAIL_PROPOSAL invariant must remain False."""
        self.assertFalse(AUTO_EMAIL_PROPOSAL)

    def test_47_safety_auto_followup_is_false(self):
        """AUTO_FOLLOWUP invariant must remain False."""
        self.assertFalse(AUTO_FOLLOWUP)

    def test_48_safety_auto_payment_and_auto_contract_are_false(self):
        """AUTO_PAYMENT_REQUEST and AUTO_CONTRACT invariants must remain False."""
        self.assertFalse(AUTO_PAYMENT_REQUEST)
        self.assertFalse(AUTO_CONTRACT)

    # -------------------------------------------------------------------------
    # 10. Machine Output, Pipeline Table & Analytics (Section 26, 27, 33)
    # -------------------------------------------------------------------------
    def test_49_proposal_pipeline_analytics_insufficient_sample_guard(self):
        """Analytics warns when n < 30 proposals exist."""
        analytics = self.pm.get_proposal_pipeline_analytics()
        self.assertIn("sample_size", analytics)
        sample = analytics["sample_size"]
        self.assertFalse(sample["is_sufficient"])
        self.assertIn("INSUFFICIENT SAMPLE", sample["warning"])

    def test_50_proposal_pipeline_snapshot_generation(self):
        """Snapshot is generated and written to disk matching Section 33."""
        snap = self.pm.generate_proposal_pipeline_snapshot()
        self.assertTrue(os.path.exists(self.snapshot_path))
        self.assertIn("PROPOSALS_CREATED", snap)
        self.assertIn("PROPOSALS_SENT", snap)
        self.assertIn("TOTAL_QUOTED_VALUE", snap)
        self.assertIn("TOTAL_WON_VALUE", snap)
        self.assertIn("OPERATOR_ACTIONS_PENDING", snap)
        self.assertEqual(snap["AUTOMATION_ACTIONS"], 0)

    def test_51_proposal_pipeline_table_columns_and_sections(self):
        """Proposal pipeline table conforms to Section 26 columns and sections."""
        table = self.pm.get_proposal_pipeline_table()
        expected_cols = ["Business", "Stage", "Proposal Value", "Currency", "Last Contact", "Next Action", "Due Date"]
        self.assertEqual(table["columns"], expected_cols)

        expected_sections = [
            "INTERESTED", "PREVIEW REQUESTED", "PREVIEW SENT",
            "PROPOSAL REQUESTED", "PROPOSAL SENT", "NEGOTIATING", "WON", "LOST"
        ]
        for s in expected_sections:
            self.assertIn(s, table["sections"])

    def test_52_data_integrity_validator_detects_no_issues_in_clean_state(self):
        """Clean dataset passes data integrity validation."""
        valid, issues = self.pm.validate_data_integrity()
        self.assertTrue(valid)
        self.assertEqual(len(issues), 0)

    def test_53_data_integrity_validator_detects_negative_total(self):
        """Negative total is flagged as an integrity error."""
        proposals = [{
            "proposal_id": "PROP-BAD",
            "lead_id": "LEAD-MAN-3B9091",
            "total": -100.0,
            "currency": "GBP",
            "status": "DRAFT",
            "version": 1
        }]
        self.pm._save_proposals(proposals)
        valid, issues = self.pm.validate_data_integrity()
        self.assertFalse(valid)
        self.assertTrue(any("negative total" in i for i in issues))

    # -------------------------------------------------------------------------
    # 11. Server API Integration Endpoints
    # -------------------------------------------------------------------------
    def test_54_api_get_proposal_packages(self):
        """GET /api/commercial/proposal/packages returns package options."""
        res = self.client.get("/api/commercial/proposal/packages")
        self.assertEqual(res.status_code, 200)
        json_data = res.json()
        self.assertEqual(json_data["status"], "ok")
        self.assertIn("packages", json_data)

    def test_55_api_create_and_preview_proposal(self):
        """POST /api/commercial/proposal/create and GET /preview work via HTTP."""
        c_res = self.client.post("/api/commercial/proposal/create", json={
            "lead_id": "LEAD-MAN-3B9091",
            "company_name": "The Old Monkey",
            "subtotal": 1250.0,
            "discount": 0.0,
            "currency": "GBP",
            "operator_notes": "Clean central Manchester pub"
        })
        self.assertEqual(c_res.status_code, 200)
        c_json = c_res.json()
        self.assertTrue(c_json["success"])
        pid = c_json["proposal_id"]

        p_res = self.client.get(f"/api/commercial/proposal/{pid}/preview")
        self.assertEqual(p_res.status_code, 200)
        p_json = p_res.json()
        self.assertEqual(p_json["proposal_id"], pid)
        self.assertEqual(len(p_json["sections"]), 10)

    def test_56_api_approve_and_mark_sent(self):
        """POST /api/commercial/proposal/approve and /mark-sent update statuses via HTTP."""
        c_res = self.client.post("/api/commercial/proposal/create", json={
            "lead_id": "LEAD-MAN-3B9091",
            "company_name": "The Old Monkey",
            "subtotal": 1250.0
        })
        pid = c_res.json()["proposal_id"]

        # Approve
        a_res = self.client.post("/api/commercial/proposal/approve", json={"proposal_id": pid})
        self.assertEqual(a_res.status_code, 200)

        # Mark sent without confirmation -> 400
        fail_sent = self.client.post("/api/commercial/proposal/mark-sent", json={
            "proposal_id": pid,
            "delivery_method": "WHATSAPP",
            "operator_confirmed": False
        })
        self.assertEqual(fail_sent.status_code, 400)

        # Mark sent with confirmation -> 200
        ok_sent = self.client.post("/api/commercial/proposal/mark-sent", json={
            "proposal_id": pid,
            "delivery_method": "WHATSAPP",
            "operator_confirmed": True
        })
        self.assertEqual(ok_sent.status_code, 200)
        self.assertEqual(ok_sent.json()["status"], "SENT")

    def test_57_api_negotiate_endpoint(self):
        """POST /api/commercial/proposal/negotiate records note via HTTP."""
        c_res = self.client.post("/api/commercial/proposal/create", json={
            "lead_id": "LEAD-MAN-3B9091",
            "subtotal": 1250.0
        })
        pid = c_res.json()["proposal_id"]
        self.client.post("/api/commercial/proposal/mark-sent", json={
            "proposal_id": pid,
            "delivery_method": "WHATSAPP",
            "operator_confirmed": True
        })

        neg_res = self.client.post("/api/commercial/proposal/negotiate", json={
            "proposal_id": pid,
            "note_type": "timeline request",
            "details": "Requested 7-day launch"
        })
        self.assertEqual(neg_res.status_code, 200)
        self.assertTrue(neg_res.json()["success"])

    def test_58_api_won_and_lost_endpoints(self):
        """POST /api/commercial/proposal/won and /lost validate fields via HTTP."""
        c_res = self.client.post("/api/commercial/proposal/create", json={
            "lead_id": "LEAD-MAN-3B9091",
            "subtotal": 1250.0
        })
        pid = c_res.json()["proposal_id"]

        # WON without confirmation -> 400
        fail_won = self.client.post("/api/commercial/proposal/won", json={
            "proposal_id": pid,
            "agreed_value": 1250.0,
            "start_date": "2026-10-15",
            "payment_terms": "50/50",
            "operator_confirmed": False
        })
        self.assertEqual(fail_won.status_code, 400)

        # WON with confirmation -> 200
        ok_won = self.client.post("/api/commercial/proposal/won", json={
            "proposal_id": pid,
            "agreed_value": 1250.0,
            "start_date": "2026-10-15",
            "payment_terms": "50/50",
            "operator_confirmed": True
        })
        self.assertEqual(ok_won.status_code, 200)
        self.assertTrue(ok_won.json()["success"])

    def test_59_api_shareable_html_output(self):
        """GET /api/commercial/proposal/{proposal_id}/shareable returns clean HTML document."""
        c_res = self.client.post("/api/commercial/proposal/create", json={
            "lead_id": "LEAD-MAN-3B9091",
            "company_name": "The Old Monkey",
            "subtotal": 1250.0
        })
        pid = c_res.json()["proposal_id"]
        res = self.client.get(f"/api/commercial/proposal/{pid}/shareable")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers.get("content-type", ""))
        self.assertIn("The Old Monkey", res.text)
        self.assertIn("Commercial Investment", res.text)
        self.assertIn("DRIPP", res.text)


if __name__ == "__main__":
    unittest.main()
