"""Tests that the fallback uses review freshness from the enrichment result."""

import unittest
from types import SimpleNamespace

from lib.pipeline import _candidate_review_freshness


class TestCandidateReviewFreshness(unittest.TestCase):
    def test_enrichment_result_takes_precedence_over_default_attribute(self):
        business = SimpleNamespace(
            review_freshness="UNKNOWN",
            raw_data={"review_enrichment": {"review_freshness": "STALE"}},
        )
        self.assertEqual(_candidate_review_freshness(business), "STALE")

    def test_business_attribute_is_used_when_enrichment_has_no_value(self):
        business = SimpleNamespace(
            review_freshness="RECENT",
            raw_data={"review_enrichment": {}},
        )
        self.assertEqual(_candidate_review_freshness(business), "RECENT")

    def test_missing_freshness_remains_unknown(self):
        business = SimpleNamespace(raw_data={})
        self.assertEqual(_candidate_review_freshness(business), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
