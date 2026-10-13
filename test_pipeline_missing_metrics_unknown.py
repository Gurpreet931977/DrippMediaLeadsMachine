"""Regression tests for preserving unknown review metrics in pipeline output."""
import unittest

from lib.pipeline import _format_optional_metric


class TestMissingReviewMetricsDisplay(unittest.TestCase):
    def test_missing_values_are_not_misreported_as_zero(self):
        self.assertEqual(_format_optional_metric(None), "UNKNOWN")

    def test_real_zero_is_preserved(self):
        self.assertEqual(_format_optional_metric(0), "0")

    def test_existing_review_values_are_preserved(self):
        self.assertEqual(_format_optional_metric(4.5), "4.5")
        self.assertEqual(_format_optional_metric(125), "125")


if __name__ == "__main__":
    unittest.main()
