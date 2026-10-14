"""Regression tests for Cloud Run's guarded Gosom freshness fallback wiring."""

import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_cloud_run_job import create_gosom_fallback_from_env
from lib.enrichment.gosom_fallback import (
    GosomFallbackConfig,
    GosomReviewFreshnessFallback,
    LimitedProductionGosomSafetyWrapper,
)


class TestCloudRunGosomFallbackWiring(unittest.TestCase):
    def test_fallback_is_off_when_feature_flag_is_false(self):
        with patch.dict(
            os.environ,
            {
                "GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED": "false",
                "GOSOM_FALLBACK_KILL_SWITCH": "false",
            },
            clear=False,
        ):
            self.assertIsNone(create_gosom_fallback_from_env())

    def test_fallback_respects_hard_production_caps(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            with patch.dict(
                os.environ,
                {
                    "GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED": "true",
                    "GOSOM_FALLBACK_KILL_SWITCH": "false",
                    "MAX_GOSOM_FALLBACK_CALLS_PER_RUN": "50",
                    "MAX_GOSOM_FALLBACK_CALLS_PER_DAY": "50",
                    "GOSOM_CACHE_DIR": cache_dir,
                },
                clear=False,
            ):
                wrapper = create_gosom_fallback_from_env()

        self.assertIsNotNone(wrapper)
        self.assertEqual(wrapper.max_cohort_size, 5)
        self.assertEqual(wrapper.config.max_calls_per_run, 5)
        self.assertEqual(wrapper.config.max_calls_per_day, 10)

    def test_kill_switch_blocks_fallback_even_when_enabled(self):
        with patch.dict(
            os.environ,
            {
                "GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED": "true",
                "GOSOM_FALLBACK_KILL_SWITCH": "true",
            },
            clear=False,
        ):
            self.assertIsNone(create_gosom_fallback_from_env())

    def test_ineligible_candidates_do_not_consume_the_fallback_cohort(self):
        with tempfile.TemporaryDirectory() as cache_dir, patch.dict(
            os.environ,
            {
                "GOSOM_REVIEW_FRESHNESS_FALLBACK_ENABLED": "true",
                "GOSOM_FALLBACK_KILL_SWITCH": "false",
                "GOSOM_CACHE_DIR": cache_dir,
            },
            clear=False,
        ):
            fallback = GosomReviewFreshnessFallback(
                config=GosomFallbackConfig(enabled=True, cache_dir=cache_dir)
            )
            wrapper = LimitedProductionGosomSafetyWrapper(
                fallback=fallback,
                max_cohort_size=1,
                max_calls_per_run=5,
                max_calls_per_day=10,
            )

            # Fails the existing minimum-review threshold; must not use the cohort slot.
            wrapper.enrich_candidate(
                {
                    "company_name": "Low Review Bistro",
                    "source_id": "ineligible-1",
                    "city": "Manchester",
                    "category": "restaurant",
                    "review_count": 10,
                    "rating": 4.5,
                    "review_freshness": "UNKNOWN",
                },
                preloaded_places=[],
            )
            self.assertEqual(wrapper.cohort_candidates_processed, 0)
            self.assertNotIn("ineligible-1", wrapper.processed_candidate_identifiers)

            # An otherwise eligible candidate should still be admitted into the slot.
            _, telemetry = wrapper.enrich_candidate(
                {
                    "company_name": "Eligible Bistro",
                    "source_id": "eligible-1",
                    "city": "Manchester",
                    "category": "restaurant",
                    "review_count": 100,
                    "rating": 4.5,
                    "review_freshness": "UNKNOWN",
                },
                preloaded_places=[],
            )
            self.assertEqual(wrapper.cohort_candidates_processed, 1)
            self.assertIn("eligible-1", wrapper.processed_candidate_identifiers)
            self.assertNotEqual(telemetry.get("status"), "COHORT_LIMIT_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
