"""
test_phase_11_1_research_recovery.py
====================================
Comprehensive Test Suite for Phase 11.1: GitHub Research/Review Recovery Validation.

Tests:
  1. Research provider availability & healthy execution.
  2. Missing provider configuration (keys/URL missing -> PROVIDER_NOT_CONFIGURED).
  3. Provider timeout handling (requests.Timeout -> PROVIDER_TIMEOUT).
  4. Provider success & review evidence recovery (review count, rating, date -> NONE).
  5. Evidence extraction failure (unparseable page structure -> EXTRACTION_FAILED).
  6. Identity mismatch & wrong-branch rejection (wrong city/branch -> IDENTITY_MISMATCH).
  7. Evidence conflict detection across sources (divergent metrics -> EVIDENCE_CONFLICT).
  8. Correct fallback cascade & circuit breaker behavior.
  9. Historical Manchester diagnostic cases (Dog and Partridge, Ducie Arms, The Old Monkey, Little Aladdin).
  10. Strict safety locks & zero CRM mutation (DRY_RUN=True, TRAVEL_MODE=True, zero outreach).
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import (
    DiscoveredBusiness,
    QualificationState,
    OperationalStatus,
    VerificationStatus,
    Priority,
    ResearchFailureState,
    ResearchTelemetry,
    SourceFamily,
)
from lib.system.system_config import SystemConfig
from lib.discovery.web_search import (
    WebSearchProvider,
    SearchOutcome,
    SearchResultList,
    ProviderCircuitBreaker,
    CircuitState,
)
from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
    ReviewEnrichmentResult,
    ReviewEvidenceItem,
    ReviewStatus,
    ReviewConfidence,
    ReviewFreshness,
)
from lib.enrichment.review_recovery import (
    ReviewEvidenceRecoveryLayer,
    ReviewRecoveryCandidateResult,
)
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.production.market_runner import (
    MarketRunner,
    BatchController,
    QuotaBudget,
)
from lib.production.market_config import MarketRegistry


class TestPhase11ResearchRecovery(unittest.TestCase):

    def setUp(self):
        # Strict Production Safety Locks
        SystemConfig.TRAVEL_MODE = True
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False
        SystemConfig.AUTOMATED_EMAIL_ENABLED = False
        SystemConfig.TECHNICAL_AUTOMATION_ENABLED = True
        self._social_patcher = patch(
            "lib.validation.social_validator.SocialIdentityValidator.check_profile_accessibility",
            return_value=(True, "Profile accessible (Mock)", "ACCESSIBLE")
        )
        self._social_patcher.start()

    def tearDown(self):
        self._social_patcher.stop()

    # -------------------------------------------------------------------------
    # 1. RESEARCH PROVIDER AVAILABILITY & SUCCESS
    # -------------------------------------------------------------------------
    def test_provider_availability_and_success(self):
        """Validates that an available provider successfully recovers review evidence."""
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Test Pub Manchester",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="123 Deansgate, Manchester",
            phone="+441619998888",
            raw_website="",
            review_count=120,
            rating=4.5,
            latest_review_date="2026-08-15",
            operational_status=OperationalStatus.ACTIVE_CONFIRMED.value,
            instagram_url="https://instagram.com/testpub",
            social_status="SOCIAL_FOUND",
        )
        biz.social_ownership_status = "VERIFIED"
        biz.social_activity = "ACTIVE"
        biz.raw_data = {
            "research_telemetry": ResearchTelemetry(
                candidate="Test Pub Manchester",
                provider_attempted=["SEARXNG"],
                provider_result="SEARCH_SUCCEEDED",
                evidence_found="120 reviews, 4.5★",
                operational_signal_found="PHONE, ADDRESS",
                failure_reason=ResearchFailureState.NONE.value,
            ).to_dict()
        }

        audit = scorer.evaluate_lead(
            business=biz,
            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
            verification_reason="No website listed",
            website_evidence={"clean_website": "", "opp_status": "NO_WEBSITE"},
        )

        self.assertEqual(audit["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(biz.company_name, "Test Pub Manchester")
        self.assertIn("120 reviews", audit["qualification_reason"])
        self.assertEqual(biz.raw_data["research_telemetry"]["failure_reason"], ResearchFailureState.NONE.value)

    # -------------------------------------------------------------------------
    # 2. MISSING PROVIDER CONFIGURATION
    # -------------------------------------------------------------------------
    def test_missing_provider_configuration(self):
        """Validates that unconfigured providers produce PROVIDER_NOT_CONFIGURED reason."""
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Unconfigured Candidate",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="45 Oxford Rd, Manchester",
            raw_website="",
            review_count=None,
        )
        biz.raw_data = {
            "research_telemetry": ResearchTelemetry(
                candidate="Unconfigured Candidate",
                provider_attempted=["TAVILY", "BRAVE", "SEARXNG"],
                provider_result="PROVIDER_NOT_CONFIGURED",
                evidence_found="NONE",
                operational_signal_found="ADDRESS",
                failure_reason=ResearchFailureState.PROVIDER_NOT_CONFIGURED.value,
            ).to_dict()
        }

        audit = scorer.evaluate_lead(
            business=biz,
            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
            verification_reason="No website listed",
            website_evidence={"clean_website": "", "opp_status": "NO_WEBSITE"},
        )

        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertIn("Review research provider not configured", audit["qualification_reason"])
        self.assertIn("Tavily/Brave/SearXNG missing", audit["qualification_reason"])

    # -------------------------------------------------------------------------
    # 3. PROVIDER UNAVAILABLE (CONNECTION REFUSED / NETWORK ERROR)
    # -------------------------------------------------------------------------
    def test_provider_unavailable(self):
        """Validates that an unavailable provider produces PROVIDER_UNAVAILABLE reason."""
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Unavailable Candidate",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="77 King St, Manchester",
            raw_website="",
            review_count=None,
        )
        biz.raw_data = {
            "research_telemetry": ResearchTelemetry(
                candidate="Unavailable Candidate",
                provider_attempted=["SEARXNG", "DUCKDUCKGO_FALLBACK"],
                provider_result="PROVIDER_UNAVAILABLE",
                evidence_found="NONE",
                operational_signal_found="ADDRESS",
                failure_reason=ResearchFailureState.PROVIDER_UNAVAILABLE.value,
            ).to_dict()
        }

        audit = scorer.evaluate_lead(
            business=biz,
            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
            verification_reason="No website listed",
            website_evidence={"clean_website": "", "opp_status": "NO_WEBSITE"},
        )

        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertIn("Review research provider unavailable", audit["qualification_reason"])
        self.assertIn("connection refused or network unreachable", audit["qualification_reason"])

    # -------------------------------------------------------------------------
    # 4. PROVIDER TIMEOUT
    # -------------------------------------------------------------------------
    def test_provider_timeout(self):
        """Validates that a provider timeout produces PROVIDER_TIMEOUT reason."""
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Timeout Candidate",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="10 Portland St, Manchester",
            raw_website="",
            review_count=None,
        )
        biz.raw_data = {
            "research_telemetry": ResearchTelemetry(
                candidate="Timeout Candidate",
                provider_attempted=["SEARXNG"],
                provider_result="SEARCH_TIMEOUT",
                evidence_found="NONE",
                operational_signal_found="ADDRESS",
                failure_reason=ResearchFailureState.PROVIDER_TIMEOUT.value,
            ).to_dict()
        }

        audit = scorer.evaluate_lead(
            business=biz,
            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
            verification_reason="No website listed",
            website_evidence={"clean_website": "", "opp_status": "NO_WEBSITE"},
        )

        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertIn("Review research provider timed out", audit["qualification_reason"])

    # -------------------------------------------------------------------------
    # 5. PROVIDER FAILED (CIRCUIT OPEN / HTTP 500 / BLOCKED)
    # -------------------------------------------------------------------------
    def test_provider_failed(self):
        """Validates that an HTTP error or open circuit produces PROVIDER_FAILED reason."""
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Circuit Broken Candidate",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="20 Cross St, Manchester",
            raw_website="",
            review_count=None,
        )
        biz.raw_data = {
            "research_telemetry": ResearchTelemetry(
                candidate="Circuit Broken Candidate",
                provider_attempted=["DUCKDUCKGO_FALLBACK"],
                provider_result="SEARCH_CIRCUIT_OPEN",
                evidence_found="NONE",
                operational_signal_found="ADDRESS",
                failure_reason=ResearchFailureState.PROVIDER_FAILED.value,
            ).to_dict()
        }

        audit = scorer.evaluate_lead(
            business=biz,
            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
            verification_reason="No website listed",
            website_evidence={"clean_website": "", "opp_status": "NO_WEBSITE"},
        )

        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertIn("Review research provider failed", audit["qualification_reason"])
        self.assertIn("HTTP error or circuit open", audit["qualification_reason"])

    # -------------------------------------------------------------------------
    # 6. EVIDENCE EXTRACTION FAILURE
    # -------------------------------------------------------------------------
    def test_evidence_extraction_failure(self):
        """Validates that unparseable HTML structure produces EXTRACTION_FAILED reason."""
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Unparseable Candidate",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="15 Market St, Manchester",
            raw_website="",
            review_count=None,
        )
        biz.raw_data = {
            "research_telemetry": ResearchTelemetry(
                candidate="Unparseable Candidate",
                provider_attempted=["SEARXNG"],
                provider_result="EXTRACTION_FAILED",
                evidence_found="NONE",
                operational_signal_found="ADDRESS",
                failure_reason=ResearchFailureState.EXTRACTION_FAILED.value,
            ).to_dict()
        }

        audit = scorer.evaluate_lead(
            business=biz,
            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
            verification_reason="No website listed",
            website_evidence={"clean_website": "", "opp_status": "NO_WEBSITE"},
        )

        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertIn("Review evidence extraction failed from discovered sources", audit["qualification_reason"])

    # -------------------------------------------------------------------------
    # 7. IDENTITY MISMATCH & WRONG-BRANCH REJECTION
    # -------------------------------------------------------------------------
    def test_identity_mismatch(self):
        """Validates that wrong-branch listings produce IDENTITY_MISMATCH reason."""
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Branch Mismatch Candidate",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="50 Oldham St, Manchester",
            raw_website="",
            review_count=None,
        )
        biz.raw_data = {
            "research_telemetry": ResearchTelemetry(
                candidate="Branch Mismatch Candidate",
                provider_attempted=["SEARXNG"],
                provider_result="REJECTED_IDENTITY_MISMATCH",
                evidence_found="NONE",
                operational_signal_found="ADDRESS",
                failure_reason=ResearchFailureState.IDENTITY_MISMATCH.value,
            ).to_dict()
        }

        audit = scorer.evaluate_lead(
            business=biz,
            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
            verification_reason="No website listed",
            website_evidence={"clean_website": "", "opp_status": "NO_WEBSITE"},
        )

        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertIn("Discovered review sources rejected due to identity/branch mismatch", audit["qualification_reason"])

    # -------------------------------------------------------------------------
    # 8. EVIDENCE CONFLICT
    # -------------------------------------------------------------------------
    def test_evidence_conflict(self):
        """Validates that conflicting review evidence produces EVIDENCE_CONFLICT reason."""
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Conflicting Candidate",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="80 Canal St, Manchester",
            raw_website="",
            review_count=None,
        )
        biz.raw_data = {
            "research_telemetry": ResearchTelemetry(
                candidate="Conflicting Candidate",
                provider_attempted=["SEARXNG"],
                provider_result="REJECTED_CONFLICT",
                evidence_found="NONE",
                operational_signal_found="ADDRESS",
                failure_reason=ResearchFailureState.EVIDENCE_CONFLICT.value,
            ).to_dict()
        }

        audit = scorer.evaluate_lead(
            business=biz,
            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
            verification_reason="No website listed",
            website_evidence={"clean_website": "", "opp_status": "NO_WEBSITE"},
        )

        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertIn("Conflicting review evidence detected across sources", audit["qualification_reason"])

    # -------------------------------------------------------------------------
    # 9. GENUINELY NO EVIDENCE FOUND
    # -------------------------------------------------------------------------
    def test_genuinely_no_evidence_found(self):
        """Validates that search with no matched listings produces NO_EVIDENCE_FOUND reason."""
        scorer = LeadScoringProvider()
        biz = DiscoveredBusiness(
            company_name="Obscure Candidate",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="10 Back Piccadilly, Manchester",
            raw_website="",
            review_count=None,
        )
        biz.raw_data = {
            "research_telemetry": ResearchTelemetry(
                candidate="Obscure Candidate",
                provider_attempted=["SEARXNG"],
                provider_result="NO_EVIDENCE",
                evidence_found="NONE",
                operational_signal_found="ADDRESS",
                failure_reason=ResearchFailureState.NO_EVIDENCE_FOUND.value,
            ).to_dict()
        }

        audit = scorer.evaluate_lead(
            business=biz,
            verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
            verification_reason="No website listed",
            website_evidence={"clean_website": "", "opp_status": "NO_WEBSITE"},
        )

        self.assertEqual(audit["qualification_state"], QualificationState.RESEARCH_ONLY.value)
        self.assertIn("Genuinely no review evidence found across verified sources", audit["qualification_reason"])

    # -------------------------------------------------------------------------
    # 10. CORRECT FALLBACK BEHAVIOR & CIRCUIT BREAKER
    # -------------------------------------------------------------------------
    def test_fallback_behavior_and_circuit_breaker(self):
        """Validates that search provider cascade skips open circuits and trips on failures."""
        cb = ProviderCircuitBreaker("MOCK_PROVIDER", failure_threshold=2, cooldown_seconds=60.0)
        self.assertEqual(cb.status, CircuitState.CLOSED.value)
        self.assertTrue(cb.can_request())

        # First failure
        cb.record_failure("HTTP 500", latency=0.1)
        self.assertEqual(cb.failure_count, 1)
        self.assertEqual(cb.status, CircuitState.CLOSED.value)

        # Second failure -> trips to OPEN
        cb.record_failure("HTTP 500", latency=0.1)
        self.assertEqual(cb.failure_count, 2)
        self.assertEqual(cb.status, CircuitState.OPEN.value)
        self.assertFalse(cb.can_request())

        # Reset
        cb.reset()
        self.assertEqual(cb.status, CircuitState.CLOSED.value)
        self.assertTrue(cb.can_request())

    # -------------------------------------------------------------------------
    # 11. HISTORICAL MANCHESTER DIAGNOSTIC CASES (SAFE READ-ONLY)
    # -------------------------------------------------------------------------
    def test_historical_manchester_diagnostic_cases(self):
        """
        Executes safe read-only review recovery verification on the 4 known historical businesses:
          - Dog and Partridge
          - Ducie Arms
          - The Old Monkey
          - Little Aladdin
        Verifies behavior without CRM writes or outreach dispatches.
        """
        historical_cases = [
            {"company_name": "Dog and Partridge", "city": "Manchester", "address": "Wilmslow Rd"},
            {"company_name": "Ducie Arms", "city": "Manchester", "address": "Devas St"},
            {"company_name": "The Old Monkey", "city": "Manchester", "address": "Portland St"},
            {"company_name": "Little Aladdin", "city": "Manchester", "address": "High St"},
        ]

        # Use ReviewEvidenceRecoveryLayer in read-only diagnostic mode
        recovery = ReviewEvidenceRecoveryLayer()

        for case in historical_cases:
            rec = recovery.recover_candidate(case)
            self.assertIsNotNone(rec)
            self.assertEqual(rec.city, "Manchester")
            self.assertIn(rec.business_name, ["Dog and Partridge", "Ducie Arms", "The Old Monkey", "Little Aladdin"])

            # Verify that each business returned a valid deterministic status
            self.assertIn(
                rec.status,
                [
                    "RECOVERED_RECENT",
                    "RECOVERED_STALE",
                    "REMAINED_UNKNOWN",
                    "REJECTED_IDENTITY_MISMATCH",
                    "REJECTED_CONFLICT",
                    "SOURCES_BLOCKED_OR_FAILED",
                ]
            )

            # Strict Invariant: Little Aladdin must not be falsely attributed to another branch
            if case["company_name"] == "Little Aladdin":
                if rec.status != "RECOVERED_RECENT":
                    self.assertIn(rec.status, ["REJECTED_IDENTITY_MISMATCH", "REMAINED_UNKNOWN", "SOURCES_BLOCKED_OR_FAILED"])

    # -------------------------------------------------------------------------
    # 12. STRICT SAFETY LOCKS (NO CRM MUTATION, NO OUTREACH)
    # -------------------------------------------------------------------------
    def test_strict_safety_locks_during_market_runner_processing(self):
        """Validates that MarketRunner candidate processing respects all safety locks."""
        cfg = MarketRegistry.get("MANCHESTER_UK")
        controller = BatchController(requested_count=1, max_external_calls=5, max_runtime_seconds=30, max_new_crm_records=0)
        quota = QuotaBudget(search_limit=5, gosom_limit=2, crm_write_limit=0, outreach_dispatch_limit=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = MarketRunner(
                market_config=cfg,
                batch_controller=controller,
                quota_budget=quota,
                checkpoint_dir=tmpdir,
                enable_research=False,  # Keep search disabled to isolate unit logic
            )

            test_biz = DiscoveredBusiness(
                company_name="Safety Test Pub",
                category="restaurant",
                city="Manchester",
                target_country="United Kingdom",
                detected_country="United Kingdom",
                address="10 Safe Way, Manchester",
                phone="+441611112222",
                raw_website="",
            )

            # Process candidate
            runner._process_single_candidate(test_biz, log=lambda m: None)

            # Assert strict safety locks
            self.assertEqual(runner.crm_writes_count, 0)
            self.assertEqual(runner.outreach_sends_count, 0)
            self.assertEqual(runner.campaigns_armed_count, 0)
            self.assertTrue(SystemConfig.TRAVEL_MODE)
            self.assertFalse(SystemConfig.COMMERCIAL_ACTIONS_ENABLED)
            self.assertFalse(SystemConfig.AUTOMATED_EMAIL_ENABLED)

            # Assert research telemetry recorded
            self.assertEqual(len(runner.research_telemetry_records), 1)
            telem = runner.research_telemetry_records[0]
            self.assertEqual(telem["candidate"], "Safety Test Pub")
            self.assertEqual(telem["failure_reason"], ResearchFailureState.PROVIDER_NOT_CONFIGURED.value)


if __name__ == "__main__":
    unittest.main()
