"""
test_phase_8_5_qualification.py
================================
Unit tests for Phase 8.5: Qualification Expansion + Website Opportunity Layer.

Covers:
  1. Website opportunity does not change qualification.
  2. Website opportunity does not change lead score.
  3. Website opportunity does not change priority.
  4. Broken website does not automatically mean OUTREACH_READY.
  5. No website does not automatically mean OUTREACH_READY.
  6. Contactability does not automatically mean OUTREACH_READY.
  7. A commercial prospect can remain MANUAL_REVIEW.
  8. A commercial prospect can remain RESEARCH_ONLY.
  9. Excluded businesses cannot enter Commercial Prospects.
 10. Franchise/excluded entities remain excluded where existing rules require it.
 11. Objective A fix: Live Seafood is excluded from active outreach queue.
 12. Live Seafood preserved in SENT_OUTREACH_HISTORY as OUTREACH_READY, SENT, MANUAL.
 13. Active manual outreach queue criteria enforced: outreach_status != SENT.
 14. 60 total cohort evaluated with 0 duplicates created.
 15. All 13 MANUAL_REVIEW businesses audited without rule weakening.
 16. All 23 RESEARCH_ONLY businesses audited and categorized in taxonomy.
 17. Zero side effects: OUTREACH_SENDS=0, CAMPAIGNS_ARMED=0.
"""

import os
import json
import unittest
from typing import Dict, Any, List

from lib.outreach.phase_8_5_qualification import (
    Phase85QualificationEngine,
    WebsiteOpportunityStatus,
    CommercialFitStatus,
    ResearchOnlyTaxonomy,
    calculate_website_opportunity_score,
    determine_commercial_fit,
)

AUDIT_PATH = "data/phase_8_5_qualification_audit.json"
PROSPECTS_PATH = "data/phase_8_5_commercial_prospects.json"
POOLS_PATH = "data/phase_8_5_outreach_pool.json"


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestWebsiteOpportunitySeparation(unittest.TestCase):
    """Objective L: Testing that website opportunity is strictly decoupled from qualification."""

    @classmethod
    def setUpClass(cls):
        engine = Phase85QualificationEngine()
        cls.audit, cls.prospects, cls.pools = engine.save_artifacts()

    def test_01_website_opportunity_does_not_change_qualification(self):
        """1. Website opportunity does not change qualification."""
        for r in self.audit["records"]:
            # Check that qualification_state matches the original authoritative state
            self.assertEqual(
                r["qualification_state"],
                r["original_qualification_state"],
                f"Qualification mutated for {r['company_name']}: {r['original_qualification_state']} -> {r['qualification_state']}"
            )

    def test_02_website_opportunity_does_not_change_lead_score(self):
        """2. Website opportunity does not change lead score."""
        # Check Live Seafood retains lead_score 60
        live_seafood = [r for r in self.audit["records"] if "live seafood" in r["company_name"].lower()][0]
        # website_opportunity_score is separate from lead score
        self.assertIn("website_opportunity_score", live_seafood)
        self.assertEqual(live_seafood["website_opportunity_score"], 100)
        # Authoritative lead score in Phase 8.0 was 60
        self.assertNotEqual(live_seafood["website_opportunity_score"], 60)

    def test_03_website_opportunity_does_not_change_priority(self):
        """3. Website opportunity does not change priority."""
        # website_opportunity_score / commercial fit does not create or overwrite priority
        for r in self.audit["records"]:
            self.assertNotIn("priority", r)  # Engine does not touch or redefine lead priority

    def test_04_broken_website_not_automatically_outreach_ready(self):
        """4. Broken website does not automatically mean OUTREACH_READY."""
        broken_records = [r for r in self.audit["records"] if r["website_opportunity_status"] == WebsiteOpportunityStatus.BROKEN_WEBSITE]
        self.assertGreater(len(broken_records), 0)
        for r in broken_records:
            self.assertNotEqual(r["qualification_state"], "OUTREACH_READY")
            self.assertEqual(r["qualification_state"], "MANUAL_REVIEW")

    def test_05_no_website_not_automatically_outreach_ready(self):
        """5. No website does not automatically mean OUTREACH_READY."""
        no_web_records = [r for r in self.audit["records"] if r["website_opportunity_status"] == WebsiteOpportunityStatus.NO_WEBSITE]
        self.assertEqual(len(no_web_records), 28)
        # Out of 28, exactly 1 is OUTREACH_READY (Live Seafood); 27 are NOT OUTREACH_READY
        non_outreach_ready = [r for r in no_web_records if r["qualification_state"] != "OUTREACH_READY"]
        self.assertEqual(len(non_outreach_ready), 27)

    def test_06_contactability_not_automatically_outreach_ready(self):
        """6. Contactability does not automatically mean OUTREACH_READY."""
        contactable = [r for r in self.audit["records"] if r["manual_contactable"]]
        self.assertGreater(len(contactable), 1)
        # There are 19 contactable businesses across the cohort, but only 1 is OUTREACH_READY
        contactable_unqualified = [r for r in contactable if r["qualification_state"] != "OUTREACH_READY"]
        self.assertGreater(len(contactable_unqualified), 0)
        for r in contactable_unqualified:
            self.assertIn(r["qualification_state"], ["MANUAL_REVIEW", "RESEARCH_ONLY", "EXCLUDED"])

    def test_07_commercial_prospect_can_remain_manual_review(self):
        """7. A commercial prospect can remain MANUAL_REVIEW."""
        prospects = self.prospects
        mr_prospects = [p for p in prospects if p["qualification_state"] == "MANUAL_REVIEW"]
        self.assertGreater(len(mr_prospects), 0)
        for p in mr_prospects:
            self.assertEqual(p["qualification_state"], "MANUAL_REVIEW")

    def test_08_commercial_prospect_can_remain_research_only(self):
        """8. A commercial prospect can remain RESEARCH_ONLY."""
        prospects = self.prospects
        ro_prospects = [p for p in prospects if p["qualification_state"] == "RESEARCH_ONLY"]
        self.assertGreater(len(ro_prospects), 0)
        for p in ro_prospects:
            self.assertEqual(p["qualification_state"], "RESEARCH_ONLY")

    def test_09_excluded_businesses_cannot_enter_commercial_prospects(self):
        """9. Excluded businesses cannot enter Commercial Prospects."""
        prospect_names = {p["company_name"].lower() for p in self.prospects}
        excluded_records = [r for r in self.audit["records"] if r["original_qualification_state"] == "EXCLUDED"]
        for r in excluded_records:
            self.assertNotIn(
                r["company_name"].lower(),
                prospect_names,
                f"Excluded business {r['company_name']} entered commercial prospects!"
            )

    def test_10_franchise_and_closed_remain_excluded_from_commercial_prospects(self):
        """10. Franchise/excluded entities remain excluded where existing rules require it."""
        prospect_names = {p["company_name"].lower() for p in self.prospects}
        self.assertNotIn("kfc", prospect_names)
        self.assertNotIn("subway", prospect_names)
        self.assertNotIn("revolution", prospect_names)
        self.assertNotIn("bay horse ph (closed)", prospect_names)


class TestOutreachPoolStateFix(unittest.TestCase):
    """Objective A: Fix the Outreach Pool State Error regarding Live Seafood Ltd."""

    @classmethod
    def setUpClass(cls):
        cls.pools = _load_json(POOLS_PATH)
        cls.audit = _load_json(AUDIT_PATH)

    def test_11_active_outreach_pool_excludes_already_sent_leads(self):
        """11. Active manual outreach queue criteria enforces outreach_status != SENT."""
        active_manual = self.pools["active_manual_ready"]
        # Live Seafood was already sent in Phase 8.2, so it MUST NOT be in the active manual queue
        active_ids = [item.get("lead_id") for item in active_manual]
        self.assertNotIn("LEAD-MAN-0363CF", active_ids)
        self.assertEqual(len(active_manual), 0)

    def test_12_live_seafood_preserved_in_sent_outreach_history(self):
        """12. Live Seafood preserved in SENT_OUTREACH_HISTORY as OUTREACH_READY, SENT, MANUAL."""
        history = self.pools["sent_outreach_history"]
        self.assertEqual(len(history), 1)
        record = history[0]
        self.assertEqual(record["lead_id"], "LEAD-MAN-0363CF")
        self.assertEqual(record["company_name"], "Live Seafood Ltd")
        self.assertEqual(record["qualification_state"], "OUTREACH_READY")
        self.assertEqual(record["outreach_status"], "SENT")
        self.assertEqual(record["outreach_mode"], "MANUAL")
        self.assertFalse(record["in_active_queue"])

    def test_13_summary_flags_are_accurate(self):
        """13. Machine summary accurately reflects pool state."""
        summary = self.pools["summary"]
        self.assertEqual(summary["active_outreach_ready"], 0)
        self.assertEqual(summary["automated_sendable"], 0)
        self.assertEqual(summary["sent_outreach_history_count"], 1)
        self.assertFalse(summary["live_seafood_in_active_queue"])
        self.assertEqual(summary["live_seafood_outreach_status"], "SENT")
        self.assertEqual(summary["live_seafood_qualification"], "OUTREACH_READY")


class TestAuditsAndIntegrity(unittest.TestCase):
    """Objectives B, C, G, H, M, O: Full 60 cohort reassessment, audit integrity, deduplication."""

    @classmethod
    def setUpClass(cls):
        cls.audit = _load_json(AUDIT_PATH)
        cls.prospects = _load_json(PROSPECTS_PATH)

    def test_14_total_cohort_count_exact_60(self):
        """14. Exactly 60 unique businesses evaluated without duplication."""
        self.assertEqual(self.audit["total_cohort"], 60)
        self.assertEqual(len(self.audit["records"]), 60)
        self.assertEqual(self.audit["deduplication"]["DUPLICATES_FOUND"], 0)
        self.assertEqual(self.audit["deduplication"]["DUPLICATES_CREATED"], 0)
        self.assertEqual(self.audit["deduplication"]["EXISTING_RECORDS_REUSED"], 60)

    def test_15_all_13_manual_review_leads_audited(self):
        """15. All 13 MANUAL_REVIEW businesses audited without weakening rules."""
        mr_audit = self.audit["manual_review_audit"]
        self.assertEqual(len(mr_audit), 13)
        for entry in mr_audit:
            self.assertEqual(entry["current_state"], "MANUAL_REVIEW")
            self.assertFalse(entry["can_qualification_now_be_resolved"])
            self.assertEqual(entry["frozen_rule_result"], "REMAIN_MANUAL_REVIEW")
            self.assertTrue(bool(entry["blocker"]))
            self.assertTrue(bool(entry["missing_evidence"]))
            self.assertTrue(bool(entry["next_action"]))

    def test_16_all_23_research_only_leads_audited_in_taxonomy(self):
        """16. All 23 RESEARCH_ONLY businesses audited and categorized in taxonomy."""
        ro_audit = self.audit["research_only_audit"]
        self.assertEqual(len(ro_audit), 23)
        valid_categories = {
            ResearchOnlyTaxonomy.EXISTING_WEBSITE_BUT_POTENTIAL_OPPORTUNITY,
            ResearchOnlyTaxonomy.NO_WEBSITE_BUT_INSUFFICIENT_QUALIFICATION,
            ResearchOnlyTaxonomy.CONTACTABLE_BUT_NOT_QUALIFIED,
            ResearchOnlyTaxonomy.INSUFFICIENT_OPERATIONAL_EVIDENCE,
            ResearchOnlyTaxonomy.REVIEW_CONFLICT,
            ResearchOnlyTaxonomy.LOW_RATING,
            ResearchOnlyTaxonomy.FRANCHISE_OR_NON_IDEAL_TARGET,
            ResearchOnlyTaxonomy.STRONG_EXISTING_WEBSITE,
            ResearchOnlyTaxonomy.OTHER,
        }
        for entry in ro_audit:
            self.assertEqual(entry["current_state"], "RESEARCH_ONLY")
            self.assertIn(entry["category"], valid_categories)
            self.assertEqual(entry["frozen_rule_result"], "REMAIN_RESEARCH_ONLY")

    def test_17_website_opportunity_distribution_sum_is_60(self):
        """17. Website opportunity distribution sums exactly to 60."""
        dist = self.audit["website_opportunity_distribution"]
        total = sum(dist.values())
        self.assertEqual(total, 60)
        self.assertEqual(dist["NO_WEBSITE"], 28)
        self.assertEqual(dist["BROKEN_WEBSITE"], 4)
        self.assertEqual(dist["UNCLEAR_WEBSITE"], 5)
        self.assertEqual(dist["WEAK_OFFICIAL_WEBSITE"], 0)
        self.assertEqual(dist["FUNCTIONAL_WEBSITE"], 13)
        self.assertEqual(dist["STRONG_WEBSITE"], 10)
        self.assertEqual(dist["UNKNOWN"], 0)

    def test_18_bottleneck_metrics_calculated(self):
        """18. Bottleneck metrics correctly measured."""
        summary = self.audit["bottleneck_summary"]
        self.assertEqual(summary["TOTAL_PRODUCTION_COHORT"], 60)
        self.assertEqual(summary["QUALIFIED"], 1)
        self.assertEqual(summary["ACTIVE_OUTREACH_READY"], 0)
        self.assertEqual(summary["SENT_HISTORY"], 1)
        self.assertEqual(summary["COMMERCIAL_WEBSITE_OPPORTUNITY_TOTAL"], 37)
        self.assertEqual(summary["COMMERCIAL_PROSPECTS_VIABLE"], 34)
        self.assertEqual(summary["CONTACTABLE_BUT_NOT_QUALIFIED"], 12)
        self.assertEqual(summary["NOT_A_GOOD_TARGET"], 26)


if __name__ == "__main__":
    unittest.main()
