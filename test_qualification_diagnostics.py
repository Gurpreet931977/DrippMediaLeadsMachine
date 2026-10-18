"""Stable aggregate diagnostics for strict lead qualification outcomes."""

import unittest

from lib.pipeline import _qualification_reason_bucket


class TestQualificationReasonBucket(unittest.TestCase):
    def test_review_research_failure_categories(self):
        cases = [
            ("RESEARCH_ONLY", "Research Only: Review research provider not configured. Insufficient verified traction.", "review_provider_not_configured"),
            ("RESEARCH_ONLY", "Research Only: Review research provider unavailable (network unreachable).", "review_provider_unavailable"),
            ("RESEARCH_ONLY", "Research Only: Review research provider timed out.", "review_provider_timeout"),
            ("RESEARCH_ONLY", "Research Only: Review research provider failed (HTTP error).", "review_provider_failed"),
            ("RESEARCH_ONLY", "Research Only: Review research provider quota exceeded.", "review_provider_quota_exceeded"),
            ("RESEARCH_ONLY", "Research Only: Review evidence extraction failed.", "review_evidence_extraction_failed"),
            ("RESEARCH_ONLY", "Research Only: Discovered review sources rejected due to identity/branch mismatch.", "review_identity_mismatch"),
            ("RESEARCH_ONLY", "Research Only: Genuinely no review evidence found across verified sources.", "review_evidence_not_found"),
        ]
        for state, reason, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(_qualification_reason_bucket(state, reason), expected)

    def test_review_and_rating_threshold_categories(self):
        cases = [
            ("RESEARCH_ONLY", "Research Only: Minimal customer review traction (4 reviews).", "review_count_too_low"),
            ("MANUAL_REVIEW", "Manual Review: Moderate customer traction (25 reviews, minimum 50 required).", "review_count_below_threshold"),
            ("RESEARCH_ONLY", "Research Only: Low rating and low traction.", "rating_or_traction_below_threshold"),
            ("MANUAL_REVIEW", "Manual Review: Rating unknown / missing for business with 80 reviews.", "rating_missing"),
            ("MANUAL_REVIEW", "Manual Review: Rating 3.2★ is below 4.0★ outreach threshold.", "rating_below_threshold"),
            ("MANUAL_REVIEW", "Manual Review: Review data is stale (2020-01-01).", "review_evidence_stale"),
            ("MANUAL_REVIEW", "Manual Review: Conflicting review/rating data detected across sources.", "review_evidence_conflict"),
            ("RESEARCH_ONLY", "Research Only: Review data unknown / missing.", "review_data_missing"),
        ]
        for state, reason, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(_qualification_reason_bucket(state, reason), expected)

    def test_commercial_identity_and_operational_gate_categories(self):
        cases = [
            ("MANUAL_REVIEW", "Manual Review: Found only 1 commercial signals (minimum 2 required).", "insufficient_commercial_signals"),
            ("MANUAL_REVIEW", "Manual Review: Social identity unverified (ownership unclear).", "social_ownership_unverified"),
            ("MANUAL_REVIEW", "Manual Review: No verified business-owned social presence.", "social_ownership_unverified"),
            ("MANUAL_REVIEW", "Manual Review: Business appears active but operational evidence lacks dual independent corroboration.", "operational_evidence_not_corroborated"),
            ("RESEARCH_ONLY", "Research Only: Current operational status unknown.", "operational_status_unknown"),
            ("MANUAL_REVIEW", "Manual Review: Operational status requires manual verification.", "operational_status_not_confirmed"),
            ("MANUAL_REVIEW", "Manual Review: Unresolved identity contradiction.", "identity_contradiction"),
        ]
        for state, reason, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(_qualification_reason_bucket(state, reason), expected)

    def test_website_and_country_gate_categories(self):
        cases = [
            ("EXCLUDED", "Excluded: Country mismatch detected.", "country_mismatch"),
            ("EXCLUDED", "Excluded: Business is permanently closed.", "closed_or_unverified"),
            ("EXCLUDED", "Excluded: Official website confirmed.", "website_exists"),
            ("MANUAL_REVIEW", "Manual Review: Listed website is broken/unreachable.", "website_broken"),
            ("MANUAL_REVIEW", "Manual Review: Website existence unclear.", "website_unclear"),
        ]
        for state, reason, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(_qualification_reason_bucket(state, reason), expected)

    def test_buckets_are_stable_and_do_not_copy_business_specific_details(self):
        reason = "Manual Review: Social identity unverified for The Example Bistro at 123 Main Street."
        bucket = _qualification_reason_bucket("MANUAL_REVIEW", reason)
        self.assertEqual(bucket, "social_ownership_unverified")
        self.assertNotIn("Example Bistro", bucket)
        self.assertNotIn("Main Street", bucket)

    def test_empty_and_unrecognized_reasons_have_safe_fallbacks(self):
        self.assertEqual(_qualification_reason_bucket("MANUAL_REVIEW", ""), "missing_reason")
        self.assertEqual(_qualification_reason_bucket("RESEARCH_ONLY", "new future reason format"), "other")
        self.assertEqual(_qualification_reason_bucket("OUTREACH_READY", "Established Manchester restaurant with verified active operations."), "all_hard_gates_passed")


if __name__ == "__main__":
    unittest.main()
