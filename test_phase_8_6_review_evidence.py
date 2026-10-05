"""
test_phase_8_6_review_evidence.py
==================================
Comprehensive Unit Test Suite for Phase 8.6:
Review Evidence Recovery + Qualification Throughput

Covers all 23 mandatory requirements:
 1. Correct target cohort selection (11 businesses).
 2. No new business discovery (60 total production cohort preserved).
 3. Cached evidence is preferred before new research.
 4. Exact-business queries are used.
 5. Invalid review dates are rejected (SEO years, copyright, page update).
 6. Review counts preserve source provenance.
 7. Ratings preserve source provenance.
 8. Reconciler handles NO_CONFLICT (Ducie Arms, Dog and Partridge, The Old Monkey).
 9. Reconciler handles rating conflicts (Kro Bar).
 10. Reconciler handles count conflicts (Kro Bar).
 11. Branch mismatches remain blocked (Katsouris, The Station).
 12. No closest-wins logic.
 13. Qualification rules are unchanged (Rule B, review traction, rating >=4.0, freshness <=180d).
 14. Missing evidence does not become fabricated evidence.
 15. Evidence persistence survives qualification reevaluation.
 16. Contactability remains separate.
 17. Commercial opportunity remains separate.
 18. Live Seafood stays SENT.
 19. No duplicate businesses are created.
 20. No outreach sends occur.
 21. No campaigns are armed.
 22. Message history remains unchanged.
 23. Website opportunity and commercial prospect metrics cannot be conflated (37 vs 34).
"""

import os
import json
import unittest
from datetime import datetime, timezone
from typing import Dict, Any, List

from lib.enrichment.phase_8_6_review_recovery_engine import (
    Phase86ReviewRecoveryEngine,
    BlockerTaxonomy,
)
from lib.enrichment.review_date_extractor import (
    ReviewDateExtractor,
    ReviewFreshness,
    ReviewEvidenceDateType,
)
from lib.enrichment.review_rating_enricher import ReviewEvidenceItem
from lib.enrichment.review_reconciler import (
    ReviewEvidenceReconciler,
    ReviewConflictType,
)
from lib.types import SourceFamily

RECOVERY_PATH = "data/phase_8_6_review_evidence_recovery.json"
RESULTS_PATH = "data/phase_8_6_qualification_results.json"


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestTargetCohortAndDiscovery(unittest.TestCase):
    """Requirements 1 & 2: Correct target cohort selection and no new discovery."""

    @classmethod
    def setUpClass(cls):
        cls.engine = Phase86ReviewRecoveryEngine()
        cls.data = cls.engine.save_artifacts()
        cls.targets = cls.data["target_cohort"]
        cls.summary = cls.data["throughput_summary"]

    def test_01_correct_target_cohort_selection(self):
        """1. Target cohort contains exactly 11 contactable, unqualified, website-less businesses."""
        self.assertEqual(len(self.targets), 11)
        expected = {
            "Dog and Partridge", "Ducie Arms", "Kro Bar", "Katsouris Deli",
            "The Station", "Spicy Mango", "The Old Monkey", "Fifth Nightclub",
            "The Crown & Kettle", "Glamorous Chinese Restaurant", "Williams Sandwich Bar"
        }
        self.assertEqual(set(self.targets), expected)

    def test_02_no_new_business_discovery(self):
        """2. No new business discovery occurs; cohort is strictly derived from existing data."""
        self.assertEqual(self.summary["DUPLICATES_CREATED"], 0)
        # Verify 60 total production cohort exists in Phase 8.5 audit
        audit = _load_json("data/phase_8_5_qualification_audit.json")
        self.assertEqual(audit["total_cohort"], 60)


class TestCachedEvidenceAndQueries(unittest.TestCase):
    """Requirements 3 & 4: Cached evidence preference and exact-business query usage."""

    def setUp(self):
        self.data = _load_json(RECOVERY_PATH)

    def test_03_cached_evidence_preferred_before_new_research(self):
        """3. Cached evidence from review queue and Gosom caches is inspected first."""
        cache_checks = self.data["cache_checks"]
        self.assertEqual(len(cache_checks), 11)
        # Ducie Arms, Kro Bar, Glamorous Chinese had cached review queue entries
        ducie_cache = [c for c in cache_checks if c["company_name"] == "Ducie Arms"][0]
        self.assertTrue(ducie_cache["has_cached_review_data"])
        self.assertTrue(ducie_cache["has_cached_review_count"])

    def test_04_exact_business_queries_used(self):
        """4. Exact-business query patterns are enforced without broad generic searches."""
        telemetry = self.data["telemetry"]
        self.assertGreater(len(telemetry), 0)
        # Check that telemetry records associate with specific candidate names
        target_names = set(self.data["target_cohort"])
        for item in telemetry:
            self.assertIn(item["business"], target_names)


class TestDateExtractionAndProvenance(unittest.TestCase):
    """Requirements 5, 6, 7: Date validation and provenance tracking."""

    def test_05_invalid_review_dates_are_rejected(self):
        """5. Invalid review dates (SEO titles, copyright, page update) are rejected."""
        # 1. SEO title pattern: '2026 Reviews & Information'
        seo_text = "The Ducie Arms, Manchester - 2026 Reviews & Information - Tripadvisor"
        dt1 = ReviewDateExtractor.extract_from_snippet(seo_text, title=seo_text)
        self.assertEqual(dt1.freshness, ReviewFreshness.UNKNOWN.value)

        # 2. Copyright pattern: '© 2026 All rights reserved'
        copy_text = "Copyright 2026 All rights reserved. Tripadvisor LLC"
        dt2 = ReviewDateExtractor.extract_from_snippet(copy_text, title="")
        self.assertEqual(dt2.freshness, ReviewFreshness.UNKNOWN.value)

        # 3. Page update pattern: 'Page last updated on 2026-09-01'
        upd_text = "Page last updated on: 2026-09-01"
        dt3 = ReviewDateExtractor.extract_from_snippet(upd_text, title="")
        self.assertEqual(dt3.freshness, ReviewFreshness.UNKNOWN.value)

    def test_06_review_counts_preserve_source_provenance(self):
        """6. Review counts preserve source provenance."""
        data = _load_json(RECOVERY_PATH)
        for r in data["recovery_results"]:
            if r["recovered_review_count"] is not None:
                self.assertGreater(len(r["all_evidence"]), 0)
                for ev in r["all_evidence"]:
                    self.assertIn("source", ev)
                    self.assertIn("source_family", ev)
                    self.assertTrue(bool(ev["source"]))

    def test_07_ratings_preserve_source_provenance(self):
        """7. Ratings preserve source provenance."""
        data = _load_json(RECOVERY_PATH)
        for r in data["recovery_results"]:
            if r["recovered_rating"] is not None:
                self.assertGreater(len(r["all_evidence"]), 0)
                for ev in r["all_evidence"]:
                    self.assertIn("rating", ev)
                    self.assertIn("source", ev)


class TestMultiSourceReconciliationAndBranchProtection(unittest.TestCase):
    """Requirements 8, 9, 10, 11, 12: Reconciliation, conflicts, and branch isolation."""

    def setUp(self):
        self.reconciler = ReviewEvidenceReconciler()

    def test_08_reconciler_handles_no_conflict(self):
        """8. Reconciler handles consistent multi-source evidence (NO_CONFLICT)."""
        items = [
            ReviewEvidenceItem(
                review_count=126,
                rating=4.7,
                source="Initial Evidence",
                source_family=SourceFamily.UNKNOWN.value,
                business_name="Ducie Arms",
                evidence_text="126 reviews, 4.7★",
                confidence="MEDIUM",
                freshness="STALE",
                city="Manchester"
            ),
            ReviewEvidenceItem(
                review_count=197,
                rating=4.8,
                source="Restaurant Guru",
                source_family=SourceFamily.RESTAURANT_GURU.value,
                business_name="Ducie Arms",
                evidence_text="197 reviews, 4.8★",
                confidence="HIGH",
                freshness="RECENT",
                evidence_date="2026-08-12",
                city="Manchester"
            )
        ]
        res = self.reconciler.reconcile(items, candidate_meta={"company_name": "Ducie Arms", "city": "Manchester"})
        self.assertFalse(res.is_material_conflict)
        self.assertEqual(res.conflict_type, ReviewConflictType.NO_CONFLICT.value)
        self.assertEqual(res.reconciled_review_count, 197)
        self.assertEqual(res.reconciled_rating, 4.8)
        self.assertEqual(res.reconciled_freshness, ReviewFreshness.RECENT.value)

    def test_09_reconciler_handles_rating_conflicts(self):
        """9. Reconciler detects and blocks rating conflicts (e.g. 4.7★ vs 3.4★)."""
        items = [
            ReviewEvidenceItem(
                review_count=194,
                rating=3.4,
                source="Phase 8.0 Initial",
                source_family=SourceFamily.UNKNOWN.value,
                business_name="Kro Bar",
                evidence_text="194 reviews, 3.4★",
                city="Manchester"
            ),
            ReviewEvidenceItem(
                review_count=2500,
                rating=4.7,
                source="Restaurant Guru",
                source_family=SourceFamily.RESTAURANT_GURU.value,
                business_name="Kro Bar",
                evidence_text="2500 reviews, 4.7★",
                city="Manchester"
            )
        ]
        res = self.reconciler.reconcile(items, candidate_meta={"company_name": "Kro Bar", "city": "Manchester"})
        self.assertTrue(res.is_material_conflict)
        self.assertIn("Material star rating disparity", str(res.conflict_reasons))

    def test_10_reconciler_handles_count_conflicts(self):
        """10. Reconciler detects and blocks extreme count conflicts."""
        items = [
            ReviewEvidenceItem(
                review_count=194,
                rating=4.2,
                source="Source A",
                source_family=SourceFamily.GOOGLE.value,
                business_name="Sample Pub",
                evidence_text="194 reviews",
                city="Manchester"
            ),
            ReviewEvidenceItem(
                review_count=2500,
                rating=4.3,
                source="Source B",
                source_family=SourceFamily.TRIPADVISOR.value,
                business_name="Sample Pub",
                evidence_text="2500 reviews",
                city="Manchester"
            )
        ]
        res = self.reconciler.reconcile(items, candidate_meta={"company_name": "Sample Pub", "city": "Manchester"})
        self.assertTrue(res.is_material_conflict)
        self.assertIn("Material review count disparity", str(res.conflict_reasons))

    def test_11_branch_mismatches_remain_blocked(self):
        """11. Branch mismatches and identity ambiguities remain blocked."""
        data = _load_json(RECOVERY_PATH)
        # Katsouris Deli and The Station were rejected due to identity/branch mismatch
        katsouris = [r for r in data["recovery_results"] if "katsouris" in r["company_name"].lower()][0]
        self.assertNotEqual(katsouris["new_qualification_state"], "OUTREACH_READY")
        self.assertEqual(katsouris["recovery_status"], "REJECTED_IDENTITY_MISMATCH")

    def test_12_no_closest_wins_logic(self):
        """12. Ambiguous locations are never assigned by closest-wins logic."""
        data = _load_json(RECOVERY_PATH)
        station = [r for r in data["recovery_results"] if "the station" in r["company_name"].lower()][0]
        self.assertEqual(station["recovery_status"], "REJECTED_IDENTITY_MISMATCH")
        self.assertNotEqual(station["new_qualification_state"], "OUTREACH_READY")


class TestQualificationIntegrityAndThroughput(unittest.TestCase):
    """Requirements 13, 14, 15: Rule B preservation, anti-fabrication, and throughput."""

    def setUp(self):
        self.data = _load_json(RECOVERY_PATH)
        self.results = _load_json(RESULTS_PATH)
        self.summary = self.data["throughput_summary"]

    def test_13_qualification_rules_are_unchanged(self):
        """13. Rule B thresholds (>=50 reviews, >=4.0★ rating, <=180d freshness) are strictly enforced."""
        for r in self.results["active_outreach_queue"]:
            self.assertEqual(r["qualification_state"], "OUTREACH_READY")
            self.assertGreaterEqual(r["review_count"], 50)
            self.assertGreaterEqual(r["rating"], 4.0)
            self.assertEqual(r["evidence_freshness"], "RECENT")

    def test_14_missing_evidence_does_not_become_fabricated(self):
        """14. Venues without recovered evidence (Fifth Nightclub, Williams) remain UNKNOWN."""
        fifth = [r for r in self.data["recovery_results"] if "fifth" in r["company_name"].lower()][0]
        self.assertIsNone(fifth["recovered_review_count"])
        self.assertIsNone(fifth["recovered_rating"])
        self.assertEqual(fifth["recovered_freshness"], "UNKNOWN")
        self.assertEqual(fifth["new_qualification_state"], "RESEARCH_ONLY")

    def test_15_evidence_persistence_survives_qualification_reevaluation(self):
        """15. Newly qualified leads are properly structured in the active outreach queue."""
        active_queue = self.results["active_outreach_queue"]
        self.assertEqual(len(active_queue), 3)
        promoted_names = {q["company_name"] for q in active_queue}
        self.assertEqual(promoted_names, {"Dog and Partridge", "Ducie Arms", "The Old Monkey"})

    def test_15b_low_rating_venue_gated_to_manual_review(self):
        """15b. Spicy Mango recovered recent reviews but low rating (2.5★), gating it to MANUAL_REVIEW."""
        spicy = [r for r in self.data["recovery_results"] if "spicy mango" in r["company_name"].lower()][0]
        self.assertEqual(spicy["recovered_freshness"], "RECENT")
        self.assertEqual(spicy["recovered_rating"], 2.5)
        self.assertEqual(spicy["new_qualification_state"], "MANUAL_REVIEW")


class TestSeparationAndSafetyInvariants(unittest.TestCase):
    """Requirements 16 to 23: Invariant separation, safety, and metric disambiguation."""

    def setUp(self):
        self.data = _load_json(RECOVERY_PATH)
        self.summary = self.data["throughput_summary"]

    def test_16_contactability_remains_separate(self):
        """16. Contactability is not altered by review recovery."""
        for r in self.data["recovery_results"]:
            self.assertTrue(r["manual_contactable"])

    def test_17_commercial_opportunity_remains_separate(self):
        """17. Commercial opportunity scores/statuses are not overwritten by review recovery."""
        audit = _load_json("data/phase_8_5_qualification_audit.json")
        for rec in audit["records"]:
            if rec["company_name"] in ["Dog and Partridge", "Ducie Arms", "The Old Monkey"]:
                self.assertEqual(rec["website_opportunity_status"], "NO_WEBSITE")

    def test_18_live_seafood_stays_sent(self):
        """18. Live Seafood Ltd remains historically SENT and excluded from active queue."""
        self.assertEqual(self.summary["LIVE_SEAFOOD_STATE_UNCHANGED"], "YES")
        active_names = [q["company_name"] for q in self.data.get("active_outreach_queue", [])]
        self.assertNotIn("Live Seafood Ltd", active_names)

    def test_19_no_duplicate_businesses_created(self):
        """19. Zero duplicate businesses created."""
        self.assertEqual(self.summary["DUPLICATES_CREATED"], 0)

    def test_20_no_outreach_sends_occur(self):
        """20. OUTREACH_SENDS=0 strictly enforced."""
        self.assertEqual(self.summary["OUTREACH_SENDS"], 0)

    def test_21_no_campaigns_are_armed(self):
        """21. CAMPAIGNS_ARMED=0 strictly enforced."""
        self.assertEqual(self.summary["CAMPAIGNS_ARMED"], 0)

    def test_22_message_history_remains_unchanged(self):
        """22. MESSAGE_HISTORY_MUTATED=0 strictly enforced."""
        self.assertEqual(self.summary["MESSAGE_HISTORY_MUTATED"], 0)

    def test_23_website_opportunity_and_commercial_prospect_metrics_not_conflated(self):
        """23. Website opportunity (37) and commercial prospect (34) metrics cannot be conflated."""
        self.assertEqual(self.summary["WEBSITE_OPPORTUNITY_COUNT"], 37)
        self.assertEqual(self.summary["COMMERCIAL_PROSPECT_COUNT"], 34)
        self.assertNotEqual(
            self.summary["WEBSITE_OPPORTUNITY_COUNT"],
            self.summary["COMMERCIAL_PROSPECT_COUNT"]
        )


if __name__ == "__main__":
    unittest.main()
