"""Regression tests for Cloud Run's guarded Gosom freshness fallback wiring."""

import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_cloud_run_job import create_gosom_fallback_from_env


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


if __name__ == "__main__":
    unittest.main()
