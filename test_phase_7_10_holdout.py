#!/usr/bin/env python3
"""
Unit tests for Phase 7.10 Blind Holdout Validation
===================================================
Verifies:
  1. Frozen matcher parameter integrity (no retuning permitted).
  2. Second-best match analysis and ambiguity protection.
  3. Rejection of "closest result wins" fallback when competing branches are within 180m.
  4. Multi-branch isolation (Costa, Greggs, KFC, Escape Lounge).
  5. Permanent regressions (Pot Kettle Black Airport, Issano).
  6. Phase 7.10 evaluation artifact verification (40 candidates, 0 false positives, 1.000 precision).
  7. Qualification invariant: Google place + review evidence remains ONE source family.
  8. Zero CRM mutations, zero outreach sends, production flag OFF.
"""

import os
import sys
import json
import unittest
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import SourceFamily, OperationalStatus, QualificationState
from lib.crm.identity_matcher import BusinessIdentityMatcher
from lib.enrichment.review_date_extractor import ReviewDateExtractor, ReviewFreshness
from lib.enrichment.review_rating_enricher import REFERENCE_DATE
from lib.enrichment.review_reconciler import ReviewEvidenceReconciler
from lib.enrichment.gosom_fallback import GosomFallbackConfig
from lib.enrichment.coordinate_matcher import (
    CoordinateMatchClassification,
    IdentityMatchClassification,
    CoordinateMatchResultClassification,
    CoordinateFirstMatcher,
    CoordinateFirstEvaluator,
    haversine_distance_m,
    construct_safe_coordinate_query,
)


class TestPhase710BlindHoldout(unittest.TestCase):

    def setUp(self):
        self.matcher = BusinessIdentityMatcher()
        self.coord_matcher = CoordinateFirstMatcher(self.matcher)
        self.config = GosomFallbackConfig(enabled=False, max_calls=40, cache_dir="data/cache_gosom_reviews")
        self.reconciler = ReviewEvidenceReconciler(self.matcher)
        self.evaluator = CoordinateFirstEvaluator(
            config=self.config,
            matcher=self.matcher,
            reconciler=self.reconciler
        )

    def test_01_frozen_thresholds_enforced(self):
        """Coordinate and identity thresholds must strictly match Phase 7.9 frozen values."""
        self.assertEqual(CoordinateFirstMatcher.EXACT_DISTANCE_M, 50.0)
        self.assertEqual(CoordinateFirstMatcher.STRONG_DISTANCE_M, 180.0)
        self.assertEqual(CoordinateFirstMatcher.EXACT_NAME_THRESHOLD, 0.95)
        self.assertEqual(CoordinateFirstMatcher.STRONG_NAME_THRESHOLD, 0.80)
        self.assertEqual(CoordinateFirstMatcher.WEAK_NAME_THRESHOLD, 0.60)

    def test_02_second_best_ambiguity_protection(self):
        """
        When two same-name results are BOTH within the safe threshold (<= 180m),
        the system MUST classify AMBIGUOUS_MATCH and never use 'closest result wins'.
        """
        candidate = {
            "company_name": "Test Cafe",
            "latitude": 53.48000,
            "longitude": -2.24000,
            "city": "Manchester"
        }
        places = [
            {
                "title": "Test Cafe",
                "latitude": 53.48020,  # ~25m away
                "longitude": -2.24000,
                "address": "10 High St, Manchester"
            },
            {
                "title": "Test Cafe",
                "latitude": 53.48080,  # ~90m away (both <= 180m)
                "longitude": -2.24000,
                "address": "25 High St, Manchester"
            }
        ]
        matched, outcome, conf, diag = self.coord_matcher.classify_and_match(candidate, places)
        self.assertIsNone(matched)
        self.assertEqual(outcome, CoordinateMatchResultClassification.AMBIGUOUS_MATCH)
        self.assertIn("MULTIPLE_SAME_NAME_PLACES_WITHIN_SAFE_DISTANCE", diag["reasons"][0])

    def test_03_second_best_decisive_separation_safe_match(self):
        """
        When the best candidate is <= 50m and the second-best candidate is > 180m,
        a decisive SAFE_MATCH is declared with a measurable distance margin.
        """
        candidate = {
            "company_name": "KFC",
            "latitude": 53.3607392,
            "longitude": -2.2691744,
            "city": "Manchester"
        }
        places = [
            {
                "title": "KFC Manchester Airport",
                "latitude": 53.3606273,  # 47.2m away
                "longitude": -2.2698599,
                "address": "Terminal 1, Manchester Airport"
            },
            {
                "title": "KFC Terminal 2",
                "latitude": 53.3687797,  # ~1198m away
                "longitude": -2.2812114,
                "address": "Terminal 2, Manchester Airport"
            }
        ]
        matched, outcome, conf, diag = self.coord_matcher.classify_and_match(candidate, places)
        self.assertIsNotNone(matched)
        self.assertEqual(outcome, CoordinateMatchResultClassification.SAFE_MATCH)
        self.assertEqual(matched["title"], "KFC Manchester Airport")
        self.assertLessEqual(diag["matched_distance_meters"], 50.0)

    def test_04_multi_branch_isolation_costa(self):
        """Costa at Stockport Road (WA15 7UG) must isolate from Costa at Sunbank Lane (WA15 0AF)."""
        costa_altrincham = {
            "company_name": "Costa",
            "latitude": 53.3975893,
            "longitude": -2.3185568,
            "city": "Manchester"
        }
        places = [
            {
                "title": "Costa Coffee",
                "latitude": 53.397554,   # 4.5m away
                "longitude": -2.318544,
                "address": "Stockport Road, Altrincham WA15 7UG"
            },
            {
                "title": "Costa Coffee",
                "latitude": 53.356796,   # Sunbank Lane branch (~4.8 km away)
                "longitude": -2.291268,
                "address": "Sunbank Lane, Altrincham WA15 0AF"
            }
        ]
        matched, outcome, conf, diag = self.coord_matcher.classify_and_match(costa_altrincham, places)
        self.assertEqual(outcome, CoordinateMatchResultClassification.SAFE_MATCH)
        self.assertEqual(matched["address"], "Stockport Road, Altrincham WA15 7UG")
        self.assertLessEqual(diag["matched_distance_meters"], 10.0)

    def test_05_pot_kettle_black_permanent_regression(self):
        """Permanent Regression: Airport T2 candidate must reject Barton Arcade, Tariff St, Angel Gardens."""
        pkb_airport = {
            "company_name": "Pot Kettle Black",
            "latitude": 53.3678333,
            "longitude": -2.2822664,
            "city": "Manchester"
        }
        places = [
            {
                "title": "POT KETTLE BLACK Barton Arcade",
                "latitude": 53.4827501,  # ~12.9 km away
                "longitude": -2.2463011,
                "address": "Manchester M3 2BW, United Kingdom"
            },
            {
                "title": "POT KETTLE BLACK Angel Gardens Manchester",
                "latitude": 53.4865137,  # ~13.7 km away
                "longitude": -2.2360551,
                "address": "1 Rochdale Rd, Manchester M4 4GE"
            }
        ]
        matched, outcome, conf, diag = self.coord_matcher.classify_and_match(pkb_airport, places)
        self.assertIsNone(matched)
        self.assertEqual(outcome, CoordinateMatchResultClassification.BRANCH_MISMATCH)

    def test_06_issano_permanent_regression(self):
        """Permanent Regression: Issano Palatine Road exact match must continue to pass."""
        candidate = {
            "company_name": "Issano",
            "city": "Manchester",
            "address": "367 Palatine Rd, Northenden",
            "latitude": 53.408009,
            "longitude": -2.2574226,
            "qualification_state": "RESEARCH_ONLY",
            "operational_status": "ACTIVE_LIKELY"
        }
        place = {
            "title": "Issano Pizza & Grill House",
            "address": "367 Palatine Rd, Northenden, Wythenshawe, Manchester M22 4FY",
            "latitude": 53.4080219,
            "longitude": -2.2573227,
            "review_count": 140,
            "review_rating": 4.0,
            "user_reviews": [
                {
                    "published_at": "2026-08-29T12:00:00Z",
                    "rating_float": 4.0,
                    "review_id": "rev_issano_1"
                }
            ]
        }
        res = self.evaluator.evaluate_candidate(candidate, [place], as_of=REFERENCE_DATE)
        self.assertEqual(res["match_classification"], CoordinateMatchResultClassification.SAFE_MATCH.value)
        self.assertLessEqual(res["distance_meters"], 10.0)
        self.assertEqual(res["review_count_recovered"], 140)
        self.assertEqual(res["rating_recovered"], 4.0)

    def test_07_qualification_multi_source_invariant(self):
        """Rule B: Google place + review evidence remains ONE source family; never promotes to OUTREACH_READY alone."""
        candidate = {
            "company_name": "Caldo Lounge",
            "city": "Manchester",
            "latitude": 53.4252081,
            "longitude": -2.3193445,
            "qualification_state": "RESEARCH_ONLY",
            "operational_status": "ACTIVE_LIKELY",
            "operational_source_families": []
        }
        place = {
            "title": "Caldo Lounge",
            "address": "Waterside Plaza, Sale M33 7BS",
            "latitude": 53.4252407,
            "longitude": -2.3193342,
            "review_count": 80,
            "review_rating": 4.4,
            "user_reviews": [
                {"published_at": "2026-02-22T15:56:46Z", "rating_float": 4.4}
            ]
        }
        res = self.evaluator.evaluate_candidate(candidate, [place], as_of=REFERENCE_DATE)
        self.assertEqual(res["match_classification"], "SAFE_MATCH")
        # Qualification state transitions to MANUAL_REVIEW, NOT OUTREACH_READY (single source family)
        self.assertNotEqual(res["qualification_after"], "OUTREACH_READY")
        self.assertEqual(res["qualification_after"], "MANUAL_REVIEW")

    def test_08_phase_7_10_evaluation_artifact_integrity(self):
        """Verifies Phase 7.10 artifact metrics, precision, recall, and zero CRM mutations."""
        artifact_path = os.path.join(PROJECT_ROOT, "data", "phase_7_10_coordinate_first_holdout_eval.json")
        self.assertTrue(os.path.exists(artifact_path), f"Artifact missing: {artifact_path}")

        with open(artifact_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["phase"], "7.10")
        self.assertEqual(data["status"], "PASS")
        self.assertEqual(data["telemetry"]["candidates_evaluated"], 40)
        self.assertEqual(data["invariants"]["crm_mutations"], 0)
        self.assertEqual(data["invariants"]["outreach_sends"], 0)
        self.assertEqual(data["invariants"]["google_places_api_calls"], 0)
        self.assertEqual(data["invariants"]["apify_calls"], 0)
        self.assertFalse(data["invariants"]["production_fallback_enabled"])

        # Ground truth verification
        gt = data["independent_ground_truth"]
        self.assertEqual(gt["TRUE_SAFE_MATCH"], 22)
        self.assertEqual(gt["FALSE_POSITIVE_SAFE_MATCH"], 0)
        self.assertEqual(gt["FALSE_NEGATIVE_MATCH"], 1)

        # Precision & Recall metrics
        metrics = data["metrics"]
        self.assertEqual(metrics["precision"], 1.0000)
        self.assertEqual(metrics["false_positive_rate"], 0.0000)
        self.assertGreaterEqual(metrics["match_recall"], 0.95)
        self.assertGreaterEqual(metrics["search_recall"], 0.85)
        self.assertEqual(metrics["freshness_recent"], 19)

        # Distance distribution
        coords = data["coordinate_distribution"]["true_matches"]
        self.assertLessEqual(coords["min_meters"], 2.0)
        self.assertLessEqual(coords["median_meters"], 5.0)
        self.assertEqual(coords["count_under_50m"], 20)
        self.assertEqual(coords["count_50m_to_180m"], 2)


if __name__ == "__main__":
    unittest.main()
