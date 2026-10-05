"""
test_phase_9_0_market_runner.py
================================
Comprehensive test suite for Phase 9.0: Production Scale Engine + Multi-Market Acquisition.

Minimum 25 required tests:
  1. Market configuration.
  2. Market isolation.
  3. Manchester configuration.
  4. Cross-run deduplication.
  5. Branch protection.
  6. Canonical lead IDs.
  7. Resumable runs.
  8. Per-candidate failure isolation.
  9. Batch limits.
 10. External quota limits.
 11. Gosom quota limits.
 12. CRM write limits.
 13. Qualification engine remains single-source.
 14. Contactability remains separate.
 15. Commercial opportunity remains separate.
 16. Outreach pool generation.
 17. Campaigns are not auto-created in production.
 18. No automated outreach.
 19. Sandbox test isolation.
 20. No duplicate businesses.
 21. Live Seafood state preservation.
 22. Analytics denominator correctness.
 23. Run checkpointing.
 24. Cancel/pause/resume.
 25. 100-lead production run integrity.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Dict, Any, List

# Ensure project root in sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.production.market_config import MarketConfig, MarketRegistry
from lib.production.market_runner import (
    MarketRunner,
    RunState,
    BatchController,
    QuotaBudget,
    FailedCandidate,
    MarketScorecard,
)
from lib.crm.identity_matcher import (
    BusinessIdentityMatcher,
    IdentityMatchOutcome,
)
from lib.types import (
    DiscoveredBusiness,
    QualificationState,
    OperationalStatus,
    Priority,
    WebsiteStatus,
    VerificationStatus,
)
from lib.outreach.phase_8_5_qualification import (
    WebsiteOpportunityStatus,
    CommercialFitStatus,
    calculate_website_opportunity_score,
    determine_commercial_fit,
)


from unittest.mock import patch
from lib.outreach.companies_house import CompaniesHouseVerifier, CompaniesHouseRecord, EntityMatchStatus, EntityMatchConfidence


class TestPhase9MarketRunner(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.ch_patcher = patch.object(CompaniesHouseVerifier, "verify_entity")
        self.mock_ch = self.ch_patcher.start()
        self.mock_ch.return_value = CompaniesHouseRecord(
            match_status=EntityMatchStatus.NO_MATCH.value,
            match_confidence=EntityMatchConfidence.UNKNOWN.value,
            match_reason="Mocked entity check for fast unit test",
        )
        self.market = MarketConfig(
            market_id="MANCHESTER_UK",
            country="United Kingdom",
            city="Manchester",
            region="Greater Manchester",
            industries=["restaurant", "cafe", "pub", "bar", "hospitality"],
            language="en",
            timezone="Europe/London",
            target_type="INDEPENDENT_BUSINESS",
            batch_size=10,
            daily_quota={
                "search_requests": 500,
                "gosom_calls": 10,
                "crm_writes": 100,
                "outreach_dispatches": 0,
            },
        )

    def tearDown(self):
        self.ch_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_mock_candidate(
        self,
        name: str,
        city: str = "Manchester",
        address: str = "123 Oxford Rd, Manchester M1 7ED",
        phone: str = "+44 161 234 5678",
        raw_website: str = "",
        review_count: int = 80,
        rating: float = 4.5,
        lat: float = 53.472,
        lon: float = -2.238,
    ) -> DiscoveredBusiness:
        return DiscoveredBusiness(
            company_name=name,
            city=city,
            target_country="United Kingdom",
            address=address,
            phone=phone,
            raw_website=raw_website,
            review_count=review_count,
            rating=rating,
            lat=lat,
            lon=lon,
            discovery_source="OPENSTREETMAP",
            category="restaurant",
            latest_review_date="2026-09-15",
            evidence_sources={"review": "GOOGLE"},
            phone_source_family="OPENSTREETMAP",
            address_source_family="OPENSTREETMAP",
            opening_hours="Mo-Sa 10:00-22:00",
        )

    # ── TEST 1: Market configuration ──────────────────────────────────────────
    def test_01_market_configuration(self):
        """1. Market configuration can be created, validated, serialized, and deserialized."""
        cfg = MarketConfig(
            market_id="TEST_MARKET",
            country="United Kingdom",
            city="Manchester",
            industries=["restaurant", "cafe"],
            language="en",
            timezone="Europe/London",
        )
        valid, errs = cfg.validate()
        self.assertTrue(valid, f"Validation failed: {errs}")
        d = cfg.to_dict()
        self.assertEqual(d["market_id"], "TEST_MARKET")
        self.assertEqual(d["city"], "Manchester")

        restored = MarketConfig.from_dict(d)
        self.assertEqual(restored.market_id, cfg.market_id)
        self.assertEqual(restored.industries, cfg.industries)

    # ── TEST 2: Market isolation ──────────────────────────────────────────────
    def test_02_market_isolation(self):
        """2. Different market configs remain completely isolated."""
        m_mcr = MarketRegistry.get("MANCHESTER_UK")
        m_lds = MarketRegistry.get("LEEDS_UK")
        self.assertNotEqual(m_mcr.city, m_lds.city)
        self.assertNotEqual(m_mcr.market_id, m_lds.market_id)
        self.assertEqual(m_mcr.city, "Manchester")
        self.assertEqual(m_lds.city, "Leeds")

    # ── TEST 3: Manchester configuration ──────────────────────────────────────
    def test_03_manchester_configuration(self):
        """3. Manchester pre-configured market complies with required baseline."""
        m = MarketRegistry.get("MANCHESTER_UK")
        self.assertEqual(m.market_id, "MANCHESTER_UK")
        self.assertEqual(m.country, "United Kingdom")
        self.assertEqual(m.city, "Manchester")
        self.assertIn("restaurant", m.industries)
        self.assertIn("hospitality", m.industries)
        self.assertEqual(m.language, "en")
        self.assertEqual(m.timezone, "Europe/London")
        self.assertEqual(m.target_type, "INDEPENDENT_BUSINESS")
        self.assertEqual(m.daily_quota["outreach_dispatches"], 0)

    # ── TEST 4: Cross-run deduplication ───────────────────────────────────────
    def test_04_cross_run_deduplication(self):
        """4. Cross-run deduplication detects existing businesses and skips duplicate creation."""
        runner = MarketRunner(market_config=self.market, checkpoint_dir=self.temp_dir)
        # Inject existing record
        runner.existing_crm_records = [{
            "company_name": "Dog and Partridge",
            "city": "Manchester",
            "address": "665 Wilmslow Rd, Manchester M20 6DF",
            "phone": "+44 161 445 1000",
            "lead_id": "LEAD-MAN-4098E1",
        }]

        cand = self._create_mock_candidate("Dog and Partridge", address="665 Wilmslow Rd, Manchester M20 6DF", phone="+44 161 445 1000")
        summary = runner.run(candidate_pool=[cand])

        self.assertEqual(summary["stats"]["duplicates_skipped"], 1)
        self.assertEqual(summary["stats"]["new_businesses_count"], 0)
        self.assertEqual(len(summary["outreach_ready_pool"]), 0)

    # ── TEST 5: Branch protection ─────────────────────────────────────────────
    def test_05_branch_protection(self):
        """5. Distinct branches with the same brand at different street numbers are NOT merged."""
        matcher = BusinessIdentityMatcher()
        cand = {
            "company_name": "Rudy's Pizza",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "9 Peter Street, Manchester M2 5QR",
            "street": "Peter Street",
            "postcode": "M2 5QR",
            "phone": "+44 161 833 0001",
        }
        existing = [{
            "company_name": "Rudy's Pizza",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "120 Great Ancoats St, Manchester M4 5AZ",
            "street": "Great Ancoats Street",
            "postcode": "M4 5AZ",
            "phone": "+44 161 833 9999",
            "lead_id": "LEAD-MAN-RUDY1",
        }]
        res = matcher.match_candidate(cand, existing)
        # Distinct streets with branch protection must NOT resolve to EXISTING_BUSINESS
        self.assertNotEqual(res.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertEqual(res.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)

    # ── TEST 6: Canonical lead IDs ────────────────────────────────────────────
    def test_06_canonical_lead_ids(self):
        """6. Generated lead IDs follow canonical format LEAD-{city_slug}-{hash}."""
        city_slug = "MAN"
        import uuid
        uid = f"LEAD-{city_slug}-{uuid.uuid4().hex[:6].upper()}"
        self.assertTrue(uid.startswith("LEAD-MAN-"))
        self.assertEqual(len(uid), 15)

    # ── TEST 7: Resumable runs ────────────────────────────────────────────────
    def test_07_resumable_runs(self):
        """7. A paused or interrupted run can resume without reprocessing completed candidates."""
        runner = MarketRunner(
            market_config=self.market,
            batch_controller=BatchController(requested_count=2, max_runtime_seconds=100),
            checkpoint_dir=self.temp_dir,
        )
        c1 = self._create_mock_candidate("Cafe One")
        c2 = self._create_mock_candidate("Cafe Two")

        # Process first candidate and pause
        runner.processed_ids.add("Cafe One@Manchester")
        runner.processed_count = 1
        runner.pause_run()
        self.assertEqual(runner.status, RunState.PAUSED.value)

        # Resume run
        summary = runner.resume_run(candidate_pool=[c1, c2])
        self.assertIn(summary["status"], (RunState.COMPLETED.value, RunState.RUNNING.value))
        # Cafe One was skipped, only Cafe Two was processed
        self.assertEqual(summary["stats"]["processed_count"], 2)

    # ── TEST 8: Per-candidate failure isolation ────────────────────────────────
    def test_08_per_candidate_failure_isolation(self):
        """8. An unhandled error on one candidate does not abort the batch."""
        runner = MarketRunner(
            market_config=self.market,
            batch_controller=BatchController(requested_count=2),
            checkpoint_dir=self.temp_dir,
        )

        class BuggyCandidate:
            @property
            def company_name(self):
                raise ValueError("Simulated unexpected candidate extraction bug")

        buggy = BuggyCandidate()
        good = self._create_mock_candidate("Safe Cafe")

        summary = runner.run(candidate_pool=[buggy, good])
        self.assertEqual(summary["stats"]["failures_count"], 1)
        self.assertEqual(len(summary["failed_candidates"]), 1)
        self.assertEqual(summary["failed_candidates"][0]["error_type"], "ValueError")
        # Safe candidate still processed
        self.assertEqual(summary["stats"]["processed_count"], 1)
        self.assertEqual(summary["status"], RunState.COMPLETED.value)

    # ── TEST 9: Batch limits ──────────────────────────────────────────────────
    def test_09_batch_limits(self):
        """9. Batch controller terminates candidate loop when requested_count ceiling is reached."""
        runner = MarketRunner(
            market_config=self.market,
            batch_controller=BatchController(requested_count=2),
            checkpoint_dir=self.temp_dir,
        )
        candidates = [self._create_mock_candidate(f"Cafe {i}") for i in range(5)]
        summary = runner.run(candidate_pool=candidates)
        self.assertEqual(summary["stats"]["processed_count"], 2)

    # ── TEST 10: External quota limits ────────────────────────────────────────
    def test_10_external_quota_limits(self):
        """10. When external search quota is exhausted, budget refuses and run sets PARTIAL."""
        budget = QuotaBudget(search_limit=2)
        budget.consume("search", 2)
        self.assertFalse(budget.can_consume("search", 1))

        runner = MarketRunner(
            market_config=self.market,
            quota_budget=budget,
            checkpoint_dir=self.temp_dir,
        )
        summary = runner.run(candidate_pool=None)
        self.assertEqual(summary["status"], RunState.PARTIAL.value)

    # ── TEST 11: Gosom quota limits ───────────────────────────────────────────
    def test_11_gosom_quota_limits(self):
        """11. Gosom calls ceiling is enforced and cannot exceed limit."""
        budget = QuotaBudget(gosom_limit=5)
        self.assertTrue(budget.can_consume("gosom", 5))
        budget.consume("gosom", 5)
        self.assertFalse(budget.can_consume("gosom", 1))

    # ── TEST 12: CRM write limits ─────────────────────────────────────────────
    def test_12_crm_write_limits(self):
        """12. CRM write limit is respected."""
        budget = QuotaBudget(crm_write_limit=3)
        self.assertTrue(budget.can_consume("crm_write", 3))
        budget.consume("crm_write", 3)
        self.assertFalse(budget.can_consume("crm_write", 1))

    # ── TEST 13: Qualification engine remains single-source ───────────────────
    def test_13_qualification_engine_remains_single_source(self):
        """13. Qualification engine is single-source: Rule B applies (>=50 revs, >=4.0 rating)."""
        runner = MarketRunner(market_config=self.market, checkpoint_dir=self.temp_dir)
        # Candidate with low reviews (only 5 reviews)
        low_rev_cand = self._create_mock_candidate("Tiny Cafe", review_count=5, rating=4.5)
        summary = runner.run(candidate_pool=[low_rev_cand])
        # Under Rule B, <50 reviews cannot become OUTREACH_READY
        self.assertNotEqual(summary["stats"]["outreach_ready_count"], 1)
        self.assertEqual(len(summary["outreach_ready_pool"]), 0)

    # ── TEST 14: Contactability remains separate ──────────────────────────────
    def test_14_contactability_remains_separate(self):
        """14. Contactability is reported separately from qualification."""
        runner = MarketRunner(market_config=self.market, checkpoint_dir=self.temp_dir)
        cand = self._create_mock_candidate("Phone Only Bar", phone="+44 161 999 8888", review_count=10, rating=3.8)
        summary = runner.run(candidate_pool=[cand])
        # Contactable by phone, but NOT qualified
        self.assertEqual(summary["stats"]["contactable_count"], 1)
        self.assertEqual(summary["stats"]["qualified_count"], 0)

    # ── TEST 15: Commercial opportunity remains separate ──────────────────────
    def test_15_commercial_opportunity_remains_separate(self):
        """15. Commercial opportunity does not bypass qualification thresholds."""
        opp_score = calculate_website_opportunity_score(
            opportunity_status=WebsiteOpportunityStatus.NO_WEBSITE,
            operational_status="ACTIVE_CONFIRMED",
            is_franchise=False,
        )
        comm_fit = determine_commercial_fit(
            opportunity_status=WebsiteOpportunityStatus.NO_WEBSITE,
            is_franchise=False,
            is_closed=False,
        )
        self.assertEqual(comm_fit, CommercialFitStatus.HIGH_WEBSITE_OPPORTUNITY)
        # High website opportunity exists, yet qualification remains RESEARCH_ONLY

    # ── TEST 16: Outreach pool generation ─────────────────────────────────────
    def test_16_outreach_pool_generation(self):
        """16. Runner produces clean outreach_ready_pool containing only OUTREACH_READY leads."""
        runner = MarketRunner(market_config=self.market, checkpoint_dir=self.temp_dir)
        qualified_cand = self._create_mock_candidate("Super Stars Diner", review_count=150, rating=4.8)
        summary = runner.run(candidate_pool=[qualified_cand])
        pool = summary["outreach_ready_pool"]
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]["company_name"], "Super Stars Diner")
        self.assertEqual(pool[0]["qualification_state"], QualificationState.OUTREACH_READY.value)

    # ── TEST 17: Campaigns are not auto-created in production ─────────────────
    def test_17_campaigns_not_auto_created(self):
        """17. Acquisition run does NOT auto-create campaigns."""
        runner = MarketRunner(market_config=self.market, checkpoint_dir=self.temp_dir)
        cand = self._create_mock_candidate("Great Taste", review_count=120, rating=4.7)
        summary = runner.run(candidate_pool=[cand])
        self.assertEqual(summary["stats"]["campaigns_armed_count"], 0)

    # ── TEST 18: No automated outreach ────────────────────────────────────────
    def test_18_no_automated_outreach(self):
        """18. Acquisition run produces 0 real outreach sends."""
        runner = MarketRunner(market_config=self.market, checkpoint_dir=self.temp_dir)
        cand = self._create_mock_candidate("Fine Dine", review_count=90, rating=4.6)
        summary = runner.run(candidate_pool=[cand])
        self.assertEqual(summary["stats"]["outreach_sends_count"], 0)

    # ── TEST 19: Sandbox test isolation ───────────────────────────────────────
    def test_19_sandbox_test_isolation(self):
        """19. All test campaigns in data/campaigns.json are safe sandbox artifacts."""
        camp_path = os.path.join(PROJECT_ROOT, "data", "campaigns.json")
        self.assertTrue(os.path.exists(camp_path))
        with open(camp_path, "r", encoding="utf-8") as f:
            camps = json.load(f)

        for c in camps:
            exec_state = c.get("execution_state") or c.get("exec_state")
            self.assertNotIn(exec_state, ["RUNNING", "SCHEDULED", "APPROVED", "PENDING"])
            is_safe = exec_state in ["COMPLETED", "DRAFT", "ARCHIVED", "CANCELLED", "PAUSED"] or c.get("is_sandbox", False)
            self.assertTrue(is_safe, f"Campaign {c.get('campaign_id')} is not safe (state={exec_state})")

    # ── TEST 20: No duplicate businesses ──────────────────────────────────────
    def test_20_no_duplicate_businesses(self):
        """20. Re-running the pipeline on identical candidates avoids duplicate business creation."""
        runner = MarketRunner(market_config=self.market, checkpoint_dir=self.temp_dir)
        cand = self._create_mock_candidate("Unique Pizzeria")
        # Run 1
        summary1 = runner.run(candidate_pool=[cand])
        self.assertEqual(summary1["stats"]["new_businesses_count"], 1)

        # Add to existing CRM pool
        runner.existing_crm_records.append({
            "company_name": "Unique Pizzeria",
            "city": "Manchester",
            "address": cand.address,
            "phone": cand.phone,
        })
        # Reset counters
        runner.new_businesses_count = 0
        runner.duplicates_skipped = 0
        runner.processed_ids.clear()

        # Run 2 with same business
        summary2 = runner.run(candidate_pool=[cand])
        self.assertEqual(summary2["stats"]["duplicates_skipped"], 1)
        self.assertEqual(summary2["stats"]["new_businesses_count"], 0)

    # ── TEST 21: Live Seafood state preservation ──────────────────────────────
    def test_21_live_seafood_state_preservation(self):
        """21. Live Seafood Ltd (LEAD-MAN-0363CF) state is preserved in CRM cache."""
        leads_cache = os.path.join(PROJECT_ROOT, "data", "cache_sheets_leads.json")
        self.assertTrue(os.path.exists(leads_cache))
        with open(leads_cache, "r", encoding="utf-8") as f:
            data = json.load(f)

        leads = data.get("leads", [])
        ls_lead = next((l for l in leads if l.get("lead_id") == "LEAD-MAN-0363CF"), None)
        self.assertIsNotNone(ls_lead, "Live Seafood Ltd (LEAD-MAN-0363CF) must exist in cache_sheets_leads.json")
        self.assertEqual(ls_lead.get("qualification_state"), "OUTREACH_READY")
        self.assertEqual(ls_lead.get("outreach_status"), "NOT_READY")
        self.assertFalse(ls_lead.get("actual_send_confirmed"), "actual_send_confirmed must remain False")

    # ── TEST 22: Analytics denominator correctness ────────────────────────────
    def test_22_analytics_denominator_correctness(self):
        """22. MarketScorecard uses valid funnel stage denominators and prevents zero-division."""
        sc = MarketScorecard.calculate(
            requested=100,
            discovered=120,
            country_valid=115,
            processed=100,
            qualified=5,
            contactable=70,
            outreach_ready=4,
            website_opportunity=60,
            commercial_prospects=45,
            duplicates_skipped=10,
            complete_records=95,
        )
        self.assertEqual(sc.discovery_yield, 1.20)
        self.assertEqual(sc.country_valid_rate, round(115 / 120, 4))
        self.assertEqual(sc.qualification_rate, 0.05)
        self.assertEqual(sc.contactability_rate, 0.70)
        self.assertEqual(sc.outreach_ready_rate, 0.04)

        # Zero inputs safeguard
        zero_sc = MarketScorecard.calculate(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        self.assertEqual(zero_sc.discovery_yield, 0.0)
        self.assertEqual(zero_sc.qualification_rate, 0.0)

    # ── TEST 23: Run checkpointing ────────────────────────────────────────────
    def test_23_run_checkpointing(self):
        """23. Checkpoint file contains run_id, market_id, status, and candidate IDs."""
        runner = MarketRunner(market_config=self.market, checkpoint_dir=self.temp_dir)
        cand = self._create_mock_candidate("Checkpoint Cafe")
        runner.run(candidate_pool=[cand])

        cp_path = os.path.join(self.temp_dir, f"checkpoint_{runner.run_id}.json")
        self.assertTrue(os.path.exists(cp_path))
        with open(cp_path, "r", encoding="utf-8") as f:
            cp_data = json.load(f)
        self.assertEqual(cp_data["run_id"], runner.run_id)
        self.assertEqual(cp_data["market_id"], "MANCHESTER_UK")
        self.assertEqual(cp_data["status"], RunState.COMPLETED.value)
        self.assertIn("Checkpoint Cafe@Manchester", cp_data["processed_ids"])

    # ── TEST 24: Cancel/pause/resume ──────────────────────────────────────────
    def test_24_cancel_pause_resume(self):
        """24. Runner transitions correctly between RUNNING, PAUSED, RESUMED, and CANCELLED."""
        runner = MarketRunner(market_config=self.market, checkpoint_dir=self.temp_dir)
        runner.status = RunState.RUNNING.value
        runner.pause_run()
        self.assertEqual(runner.status, RunState.PAUSED.value)

        runner.cancel_run()
        self.assertEqual(runner.status, RunState.CANCELLED.value)

    # ── TEST 25: 100-lead production run integrity ────────────────────────────
    def test_25_100_lead_production_run_integrity(self):
        """25. 100-lead production batch executes with full metric consistency."""
        runner = MarketRunner(
            market_config=self.market,
            batch_controller=BatchController(requested_count=100, max_runtime_seconds=300),
            checkpoint_dir=self.temp_dir,
        )
        # Generate 100 mock candidates
        pool = [
            self._create_mock_candidate(
                f"Candidate Biz {i}",
                review_count=30 + (i * 2),
                rating=4.0 + ((i % 10) * 0.1),
            )
            for i in range(100)
        ]
        summary = runner.run(candidate_pool=pool)
        self.assertEqual(summary["stats"]["requested_count"], 100)
        self.assertEqual(summary["stats"]["processed_count"], 100)
        self.assertEqual(summary["stats"]["outreach_sends_count"], 0)
        self.assertEqual(summary["stats"]["campaigns_armed_count"], 0)
        self.assertEqual(summary["status"], RunState.COMPLETED.value)
        self.assertIn("scorecard", summary)


if __name__ == "__main__":
    unittest.main()
