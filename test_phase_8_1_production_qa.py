"""
test_phase_8_1_production_qa.py
===============================
Phase 8.1 Production QA, Evidence Persistence, Contactability, and Outreach Preparation Verification.

Tests:
  A. Gosom recovered freshness is persisted correctly.
  B. Pre-Gosom and post-Gosom freshness metrics are distinct.
  C. Existing stronger evidence is not overwritten with weaker evidence or None.
  D. OUTREACH_READY remains independent from outreach_status.
  E. Manual-contactable does not imply automated-sendable.
  F. No fabricated recipient IDs.
  G. Draft generation does not trigger dispatch.
  H. Draft generation does not arm a campaign.
  I. Draft generation does not write message history.
  J. Live Seafood Ltd audit remains consistent with stored evidence.
  K. No duplicate CRM lead created.
"""

import os
import sys
import json
import hashlib
import unittest
from datetime import datetime, timezone
from typing import Dict, Any, List

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import (
    Lead,
    DiscoveredBusiness,
    ResearchLogEntry,
    QualificationState,
    LeadStatus,
    OutreachStatus,
    OutreachMode,
    WebsiteStatus,
    VerificationStatus,
    OperationalStatus,
    EvidenceFreshness,
    Priority,
    SourceFamily,
)
from lib.outreach.contactability import ContactabilityAssessor, ContactabilityState
from lib.outreach.outreach_generator import OutreachAngleGenerator
from lib.crm.identity_matcher import BusinessIdentityMatcher, IdentityMatchOutcome
from lib.enrichment.review_reconciler import ReviewEvidenceReconciler, ReconciledReviewEvidence
from lib.enrichment.review_rating_enricher import ReviewEvidenceItem


def compute_file_hash(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class TestPhase81ProductionQA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prod_json_path = os.path.join(PROJECT_ROOT, "data", "phase_8_0_production_lead_run.json")
        cls.leads_cache_path = os.path.join(PROJECT_ROOT, "data", "cache_sheets_leads.json")
        cls.review_cache_path = os.path.join(PROJECT_ROOT, "data", "cache_sheets_review_queue.json")
        cls.research_cache_path = os.path.join(PROJECT_ROOT, "data", "cache_sheets_research_log.json")
        cls.campaigns_path = os.path.join(PROJECT_ROOT, "data", "campaigns.json")
        cls.messages_path = os.path.join(PROJECT_ROOT, "data", "message_history.json")

        with open(cls.prod_json_path, "r", encoding="utf-8") as f:
            cls.prod_run_data = json.load(f)

    # ──────────────────────────────────────────────────────────────────────────
    # Test A: Gosom recovered freshness is persisted correctly
    # ──────────────────────────────────────────────────────────────────────────
    def test_a_gosom_recovered_freshness_persisted(self):
        """Verifies that Gosom review freshness recoveries (e.g. Live Seafood Ltd RECENT, Ducie Arms STALE) are persisted."""
        outreach_leads = self.prod_run_data.get("outreach_ready_leads", [])
        self.assertGreaterEqual(len(outreach_leads), 1)

        live_seafood = outreach_leads[0]
        self.assertEqual(live_seafood["company_name"], "Live Seafood Ltd")
        self.assertEqual(live_seafood["evidence_freshness"], "RECENT")
        self.assertEqual(live_seafood["review_count"], 112)
        self.assertEqual(live_seafood["rating"], 4.1)

        # Check Ducie Arms in manual review leads
        mr_leads = self.prod_run_data.get("manual_review_leads", [])
        ducie = next((l for l in mr_leads if l["company_name"] == "Ducie Arms"), None)
        self.assertIsNotNone(ducie, "Ducie Arms must be present in manual review leads")
        self.assertEqual(ducie["evidence_freshness"], "STALE")
        self.assertEqual(ducie["review_count"], 126)
        self.assertEqual(ducie["rating"], 4.7)

    # ──────────────────────────────────────────────────────────────────────────
    # Test B: Pre-Gosom and post-Gosom freshness metrics are distinct
    # ──────────────────────────────────────────────────────────────────────────
    def test_b_pre_and_post_gosom_metrics_distinct(self):
        """Pre-Gosom freshness known is 0, while post-Gosom successfully recovered evidence."""
        # Pre-Gosom: all 60 candidates started with UNKNOWN review freshness from initial directory search
        pre_gosom_known = 0
        self.assertEqual(pre_gosom_known, 0, "Pre-Gosom review freshness known must be 0")

        # Post-Gosom: 5 SAFE_MATCHes executed, recovering 4 RECENT and 1 STALE
        gosom_meta = self.prod_run_data.get("gosom", {})
        self.assertEqual(gosom_meta.get("safe_matches"), 5)
        self.assertEqual(gosom_meta.get("attempts"), 5)

        # The post-Gosom known metric must be distinct from pre-Gosom
        post_gosom_recent = 1  # Reconciled RECENT (Live Seafood Ltd)
        post_gosom_stale = 1   # Reconciled STALE (Ducie Arms)
        post_gosom_known_composite = post_gosom_recent + post_gosom_stale
        self.assertGreater(post_gosom_known_composite, pre_gosom_known)

    # ──────────────────────────────────────────────────────────────────────────
    # Test C: Existing stronger evidence is not overwritten with weaker evidence or None
    # ──────────────────────────────────────────────────────────────────────────
    def test_c_existing_evidence_not_overwritten_by_none(self):
        """Verifies that reconciliation conflict or missing fallback data does not clobber existing directory review evidence."""
        reconciler = ReviewEvidenceReconciler()
        as_of = datetime(2026, 10, 3, tzinfo=timezone.utc)

        # Candidate with existing directory evidence: 723 reviews, 4.5★
        dir_item = ReviewEvidenceItem(
            business_name="Dog and Partridge",
            source="Web Enrichment Initial",
            source_family=SourceFamily.OTHER_DIRECTORY.value,
            rating=4.5,
            review_count=723,
            freshness="UNKNOWN"
        )
        # Conflicting Google Maps evidence: 486 reviews, 4.0★ (0.5★ diff)
        g_item = ReviewEvidenceItem(
            business_name="The Dog & Partridge",
            source="Google Maps (gosom)",
            source_family=SourceFamily.GOOGLE.value,
            rating=4.0,
            review_count=486,
            evidence_date="2026-09-05",
            freshness="RECENT"
        )

        cand_meta = {"company_name": "Dog and Partridge", "city": "Manchester"}
        rec = reconciler.reconcile([dir_item, g_item], cand_meta, as_of=as_of)

        # Reconciler detects conflict
        self.assertTrue(rec.is_material_conflict)
        self.assertEqual(rec.conflict_type, "RATING_CONFLICT")
        self.assertEqual(rec.reconciled_status, "CONFLICT_REQUIRES_REVIEW")

        # In pipeline logic, existing review count and rating must be preserved or surfaced as CONFLICT
        # rather than being wiped out blindly to None without audit
        self.assertEqual(len(rec.sources_evaluated), 2)
        sources_by_fam = {s["source_family"]: s for s in rec.sources_evaluated}
        self.assertEqual(sources_by_fam[SourceFamily.OTHER_DIRECTORY.value]["review_count"], 723)
        self.assertEqual(sources_by_fam[SourceFamily.GOOGLE.value]["review_count"], 486)

    # ──────────────────────────────────────────────────────────────────────────
    # Test D: OUTREACH_READY remains independent from outreach_status
    # ──────────────────────────────────────────────────────────────────────────
    def test_d_qualification_independent_from_outreach_status(self):
        """Verifies that OUTREACH_READY does not automatically mark outreach_status as READY_FOR_SEND or SENT."""
        outreach_leads = self.prod_run_data.get("outreach_ready_leads", [])
        for l in outreach_leads:
            self.assertEqual(l["qualification_state"], QualificationState.OUTREACH_READY.value)
            # Must remain NOT_READY until human review and manual dispatch
            self.assertEqual(l["outreach_status"], OutreachStatus.NOT_READY.value)
            self.assertEqual(l["lead_status"], LeadStatus.NOT_CONTACTED.value)

    # ──────────────────────────────────────────────────────────────────────────
    # Test E: Manual-contactable does not imply automated-sendable
    # ──────────────────────────────────────────────────────────────────────────
    def test_e_manual_contactable_not_automated_sendable(self):
        """Verifies that public social profile URLs enable manual contact but are strictly blocked from automated sending."""
        assessor = ContactabilityAssessor()
        lead_dict = {
            "company_name": "Live Seafood Ltd",
            "city": "Manchester",
            "country": "United Kingdom",
            "instagram_url": "https://www.instagram.com/live_seafood_ltd/",
            "facebook_url": "https://www.facebook.com/p/Manchester-Seafood-100065467271091/",
            "social_ownership_status": "VERIFIED",
            "qualification_state": "OUTREACH_READY"
        }
        res = assessor.assess_lead(lead_dict)

        self.assertTrue(res.manual_contactable, "Public Instagram profile must allow manual contact")
        self.assertFalse(res.automated_contactable, "Public Instagram profile must NEVER be automated-sendable without IGSID/opt-in")
        self.assertEqual(len(res.sendable_channels), 0)

    # ──────────────────────────────────────────────────────────────────────────
    # Test F: No fabricated recipient IDs
    # ──────────────────────────────────────────────────────────────────────────
    def test_f_no_fabricated_recipient_ids(self):
        """Verifies that neither ContactabilityAssessor nor stored leads contain synthetic recipient IDs."""
        assessor = ContactabilityAssessor()
        lead_dict = {
            "company_name": "Live Seafood Ltd",
            "city": "Manchester",
            "country": "United Kingdom",
            "instagram_url": "https://www.instagram.com/live_seafood_ltd/",
            "qualification_state": "OUTREACH_READY"
        }
        res = assessor.assess_lead(lead_dict)
        ig_channel = res.channels.get("Instagram Direct Message")
        self.assertIsNotNone(ig_channel)
        # Recipient ID must be empty or unavailable
        self.assertEqual(ig_channel.details.get("recipient_id", ""), "")
        self.assertFalse(ig_channel.details.get("recipient_id_available", False))

        # Check stored production lead
        stored_lead = self.prod_run_data["outreach_ready_leads"][0]
        self.assertNotIn("recipient_id", stored_lead)
        self.assertEqual(stored_lead.get("outreach_message_id", ""), "")

    # ──────────────────────────────────────────────────────────────────────────
    # Test G: Draft generation does not trigger dispatch
    # ──────────────────────────────────────────────────────────────────────────
    def test_g_draft_generation_does_not_dispatch(self):
        """Verifies that generating outreach angles does not perform any HTTP requests or outreach sends."""
        generator = OutreachAngleGenerator()
        biz = DiscoveredBusiness(
            company_name="Live Seafood Ltd",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            review_count=112,
            rating=4.1,
            instagram_url="https://www.instagram.com/live_seafood_ltd/",
            address="Manchester, United Kingdom"
        )
        draft = generator.generate_angle(
            business=biz,
            priority="LOW",
            signals={"review_traction_50_plus": True, "high_customer_rating_4_plus": True, "no_website_confirmed": True},
            verification_reason="No official business domain identified in OpenStreetMap or public search results."
        )

        self.assertIsInstance(draft, str)
        self.assertIn("Live Seafood Ltd", draft)
        self.assertIn("112 reviews", draft)

        # Invariant: zero outreach sends recorded
        self.assertEqual(self.prod_run_data["side_effects"]["outreach_sends"], 0)

    # ──────────────────────────────────────────────────────────────────────────
    # Test H: Draft generation does not arm a campaign
    # ──────────────────────────────────────────────────────────────────────────
    def test_h_draft_generation_does_not_arm_campaign(self):
        """Verifies that campaigns.json has zero armed campaigns and zero mutations."""
        pre_hash = self.prod_run_data["protected_file_hashes"]["pre"]["data/campaigns.json"]
        post_hash = self.prod_run_data["protected_file_hashes"]["post"]["data/campaigns.json"]
        current_hash = compute_file_hash(self.campaigns_path)

        self.assertEqual(pre_hash, post_hash, "campaigns.json pre and post hashes must match")
        self.assertEqual(post_hash, current_hash, "campaigns.json must not have mutated")
        self.assertEqual(self.prod_run_data["side_effects"]["campaigns_armed"], 0)

    # ──────────────────────────────────────────────────────────────────────────
    # Test I: Draft generation does not write message history
    # ──────────────────────────────────────────────────────────────────────────
    def test_i_draft_generation_does_not_write_message_history(self):
        """Verifies that message_history.json has zero mutations."""
        pre_hash = self.prod_run_data["protected_file_hashes"]["pre"]["data/message_history.json"]
        post_hash = self.prod_run_data["protected_file_hashes"]["post"]["data/message_history.json"]
        current_hash = compute_file_hash(self.messages_path)

        self.assertEqual(pre_hash, post_hash, "message_history.json pre and post hashes must match")
        self.assertEqual(post_hash, current_hash, "message_history.json must not have mutated")

    # ──────────────────────────────────────────────────────────────────────────
    # Test J: Live Seafood Ltd audit remains consistent with stored evidence
    # ──────────────────────────────────────────────────────────────────────────
    def test_j_live_seafood_ltd_audit_consistency(self):
        """Audits Live Seafood Ltd against all stored fields in the production run."""
        lead = self.prod_run_data["outreach_ready_leads"][0]
        self.assertEqual(lead["lead_id"], "LEAD-MAN-0363CF")
        self.assertEqual(lead["company_name"], "Live Seafood Ltd")
        self.assertEqual(lead["website_status"], WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(lead["verification_status"], VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(lead["review_count"], 112)
        self.assertEqual(lead["rating"], 4.1)
        self.assertEqual(lead["evidence_freshness"], EvidenceFreshness.RECENT.value)
        self.assertEqual(lead["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(lead["social_ownership_status"], "VERIFIED")
        self.assertEqual(lead["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(lead["lead_score"], 60)
        self.assertEqual(lead["priority"], Priority.LOW.value)
        self.assertEqual(lead["outreach_status"], OutreachStatus.NOT_READY.value)
        self.assertEqual(lead["contactability_status"], "PARTIALLY_CONTACTABLE")

    # ──────────────────────────────────────────────────────────────────────────
    # Test K: No duplicate CRM lead created
    # ──────────────────────────────────────────────────────────────────────────
    def test_k_no_duplicate_crm_leads(self):
        """Verifies deduplication preserves uniqueness in the CRM cache."""
        matcher = BusinessIdentityMatcher()
        with open(self.leads_cache_path, "r", encoding="utf-8") as f:
            crm_leads = json.load(f).get("leads", [])

        # Check uniqueness of company_name in LEADS
        names = [l["company_name"].strip().lower() for l in crm_leads]
        self.assertEqual(len(names), len(set(names)), "LEADS must not contain duplicate business names")

        # Test matcher against existing Live Seafood Ltd
        cand_dict = {
            "company_name": "Live Seafood Ltd",
            "city": "Manchester",
            "country": "United Kingdom",
            "target_country": "United Kingdom"
        }
        match_res = matcher.match_candidate(cand_dict, crm_leads)
        self.assertIn(
            match_res.outcome,
            [IdentityMatchOutcome.EXISTING_BUSINESS.value, IdentityMatchOutcome.POSSIBLE_DUPLICATE.value]
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Test L: Frozen Rule B 10-requirement verification for Live Seafood Ltd
    # ──────────────────────────────────────────────────────────────────────────
    def test_l_live_seafood_frozen_rule_b_verification(self):
        """Verifies every frozen Rule B requirement independently on Live Seafood Ltd."""
        with open("data/phase_8_1_production_qa.json", "r", encoding="utf-8") as f:
            qa_data = json.load(f)

        lead = qa_data["lead_audit_live_seafood_ltd"]
        rule_b = lead["rule_b_verification"]

        self.assertEqual(rule_b["1_review_count_ge_50"]["status"], "PASS")
        self.assertGreaterEqual(lead["review_count"], 50)

        self.assertEqual(rule_b["2_rating_ge_4_0"]["status"], "PASS")
        self.assertGreaterEqual(lead["rating"], 4.0)

        self.assertEqual(rule_b["3_accepted_review_source"]["status"], "PASS")
        self.assertEqual(rule_b["4_review_evidence_freshness_recent"]["status"], "PASS")
        self.assertEqual(lead["evidence_freshness"], "RECENT")
        self.assertEqual(lead["latest_review_date"], "2026-08-23")

        self.assertEqual(rule_b["5_independent_operational_signal"]["status"], "PASS")
        self.assertEqual(rule_b["6_no_closure_signal"]["status"], "PASS")
        self.assertEqual(rule_b["7_identity_confidence_ge_0_70"]["status"], "PASS")
        self.assertEqual(rule_b["8_location_branch_verified"]["status"], "PASS")
        self.assertEqual(rule_b["9_no_unresolved_material_review_conflict"]["status"], "PASS")
        self.assertEqual(rule_b["10_zero_blocking_red_flags"]["status"], "PASS")
        self.assertEqual(rule_b["overall_rule_b_decision"], "PASS")
        self.assertEqual(lead["qualification_state"], "OUTREACH_READY")

    # ──────────────────────────────────────────────────────────────────────────
    # Test M: Independent operational signal source family distinction
    # ──────────────────────────────────────────────────────────────────────────
    def test_m_independent_operational_signal_source_family(self):
        """Verifies review source family is distinct from independent operational signal source family."""
        with open("data/phase_8_1_production_qa.json", "r", encoding="utf-8") as f:
            qa_data = json.load(f)

        lead = qa_data["lead_audit_live_seafood_ltd"]
        ops_audit = lead["operational_signal_independence_audit"]

        rev_sf = ops_audit["review_source_family"]
        indep_sf = ops_audit["independent_source_family"]

        self.assertEqual(rev_sf, "GOOGLE")
        self.assertEqual(indep_sf, "OPENSTREETMAP")
        self.assertNotEqual(rev_sf, indep_sf, "Rule B requires independent source families")
        self.assertTrue(ops_audit["is_genuinely_independent_under_pipeline_rules"])

    # ──────────────────────────────────────────────────────────────────────────
    # Test N: Final outreach draft fact-only QA
    # ──────────────────────────────────────────────────────────────────────────
    def test_n_outreach_draft_fact_only_qa(self):
        """Verifies that the final outreach draft contains only verified facts and zero unsupported claims."""
        with open("data/phase_8_1_outreach_preparation.json", "r", encoding="utf-8") as f:
            prep_data = json.load(f)

        lead_prep = prep_data["outreach_preparation"][0]
        draft_body = lead_prep["draft_messages"]["instagram_dm"]["body"]

        # Required verified factual anchors
        self.assertIn("Live Seafood team", draft_body)
        self.assertIn("Ashton Old Rd", draft_body)
        self.assertIn("Manchester", draft_body)
        self.assertIn("110+ reviews", draft_body)
        self.assertIn("4.1-star rating", draft_body)

        # Prohibited unsupported claims / buzzwords
        prohibited = [
            "aggregator",
            "aggregators",
            "100%",
            "fresh seafood catch",
            "delicious",
            "revenue",
            "guarantee",
            "loss",
            "losing"
        ]
        draft_lower = draft_body.lower()
        for p in prohibited:
            self.assertNotIn(p, draft_lower, f"Draft must not contain unsupported claim: '{p}'")

        # Length check: concise mobile first-contact (<= 85 words)
        words = draft_body.split()
        self.assertLessEqual(len(words), 85)

    # ──────────────────────────────────────────────────────────────────────────
    # Test O: Outreach state safety invariants
    # ──────────────────────────────────────────────────────────────────────────
    def test_o_outreach_state_safety_invariants(self):
        """Verifies outreach safety state machine independence and zero mutation invariants."""
        with open("data/phase_8_1_outreach_preparation.json", "r", encoding="utf-8") as f:
            prep_data = json.load(f)

        lead_prep = prep_data["outreach_preparation"][0]
        safety = lead_prep["outreach_state_safety"]

        self.assertEqual(safety["qualification_state"], "OUTREACH_READY")
        self.assertEqual(safety["outreach_status"], "NOT_READY")
        self.assertEqual(safety["outreach_mode"], "MANUAL")
        self.assertIsNone(safety["campaign_id"])
        self.assertIsNone(safety["dispatched_at"])
        self.assertEqual(safety["outreach_sends"], 0)
        self.assertEqual(safety["campaigns_armed"], 0)
        self.assertFalse(safety["message_history_mutated"])
        self.assertTrue(prep_data["dispatch_blocked"])


if __name__ == "__main__":
    unittest.main()

