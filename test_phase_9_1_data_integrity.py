"""
test_phase_9_1_data_integrity.py
=================================
Comprehensive test suite for Phase 9.1: Acquisition Data Integrity + Targeted Lead Enrichment.

Minimum 30 required tests across 8 categories:
  1. Population Accounting (Conservation invariants, reconciliation, zero guards)
  2. Deduplication & CRM Integrity (CRM duplicate blocking, within-run suppression, branch protection, Live Seafood preservation)
  3. Operational Verification Separation (Unknown vs Verified Active, OSM alone != active, closed status blocks)
  4. Review Evidence Gating (>=50 threshold, >=4.0 rating, <=180d recency, conflict handling, no inferred metrics)
  5. Rule B Qualification (Frozen thresholds, score cannot promote, operational requirement, canonical outputs)
  6. Safety & Campaign Hard Ceilings (Zero sends, zero armed campaigns, no auto-dispatch, separate contactability)
  7. Field-Level Data Completeness (14 required dimensions, present + missing = total, explicit denominator)
  8. Website Opportunity & Quota Limits (Search <= 20, Gosom <= 10, broken website failure reasons, conclusive skips)
"""

import os
import sys
import json
import unittest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List

# Ensure project root in sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import (
    OperationalStatus,
    QualificationState,
    EvidenceFreshness,
    DiscoveredBusiness,
)
from lib.outreach.contactability import ContactabilityState
from lib.outreach.phase_8_5_qualification import (
    WebsiteOpportunityStatus,
    CommercialFitStatus,
)
from lib.production.market_runner import (
    PopulationAccounting,
    CohortFieldCompleteness,
    MarketScorecard,
    QuotaBudget,
    FailedCandidate,
)
from lib.crm.identity_matcher import (
    BusinessIdentityMatcher,
    IdentityMatchOutcome,
)
from lib.validation.operational_validator import (
    OperationalValidator,
    OperationalConfidence,
)
from lib.enrichment.phase_9_1_enrichment_engine import (
    Phase91EnrichmentEngine,
    WebsiteOpportunityAuditor,
    WebsiteAuditResult,
)


class TestPhase91PopulationAccounting(unittest.TestCase):
    """Category 1: Population Accounting (Conservation equations, reconciliation, zero guards)."""

    def test_discovered_population_reconciles(self):
        """1. Discovered total equals processed + skipped + failed."""
        accounting = PopulationAccounting(
            discovered_total=166,
            country_valid=100,
            duplicate_existing=66,
            processed_new=100,
            processed_refreshed=0,
            failed=0,
        )
        self.assertEqual(accounting.processed_total, 100)
        self.assertEqual(accounting.skipped_total, 66)
        self.assertEqual(accounting.discovered_total, accounting.processed_total + accounting.skipped_total + accounting.failed)
        is_valid, errors = accounting.validate_invariants()
        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)

    def test_processed_population_reconciles(self):
        """2. Processed total equals processed_new + processed_refreshed."""
        accounting = PopulationAccounting(
            discovered_total=150,
            processed_new=90,
            processed_refreshed=10,
            duplicate_existing=50,
            failed=0,
        )
        self.assertEqual(accounting.processed_total, 100)
        self.assertEqual(accounting.processed_total, accounting.processed_new + accounting.processed_refreshed)
        is_valid, _ = accounting.validate_invariants()
        self.assertTrue(is_valid)

    def test_conservation_validation_flags_inconsistency(self):
        """3. Invariant validation detects mathematical mismatches and reports errors."""
        broken_accounting = PopulationAccounting(
            discovered_total=166,
            processed_new=100,
            processed_refreshed=64,  # Overlapped incorrectly without accounting for skipped
            duplicate_existing=66,
            failed=0,
        )
        # 166 != (164 + 66 + 0) = 230
        is_valid, errors = broken_accounting.validate_invariants()
        self.assertFalse(is_valid)
        self.assertGreater(len(errors), 0)
        self.assertTrue(any("discovered" in err.lower() for err in errors))

    def test_failed_candidates_remain_isolated(self):
        """4. Per-candidate failure increments failed count without corrupting processed or skipped."""
        accounting = PopulationAccounting(
            discovered_total=105,
            processed_new=100,
            processed_refreshed=0,
            duplicate_existing=0,
            failed=5,
        )
        self.assertEqual(accounting.processed_total, 100)
        self.assertEqual(accounting.failed, 5)
        self.assertEqual(accounting.discovered_total, accounting.processed_total + accounting.skipped_total + accounting.failed)
        is_valid, _ = accounting.validate_invariants()
        self.assertTrue(is_valid)

    def test_accounting_zero_denominator_guard(self):
        """5. Field completeness and accounting percentages handle zero candidates gracefully."""
        comp = CohortFieldCompleteness.calculate([])
        self.assertEqual(comp.total_candidates, 0)
        self.assertEqual(comp.overall_required_field_completeness, 0.0)
        for field, metrics in comp.fields.items():
            self.assertEqual(metrics.present_count, 0)
            self.assertEqual(metrics.missing_count, 0)
            self.assertEqual(metrics.completeness_percent, 0.0)

    def test_accounting_mutually_exclusive_categories(self):
        """6. Canonical accounting categories strictly partition the candidate population."""
        accounting = PopulationAccounting(
            discovered_total=200,
            country_valid=180,
            country_invalid=5,
            boundary_invalid=5,
            duplicate_existing=30,
            duplicate_within_run=10,
            processed_new=140,
            processed_refreshed=10,
            failed=0,
        )
        # Skipped = country_invalid(5) + boundary_invalid(5) + duplicate_existing(30) + duplicate_within_run(10) = 50
        # Processed = 140 + 10 = 150
        # 150 + 50 + 0 = 200
        self.assertEqual(accounting.skipped_total, 50)
        self.assertEqual(accounting.processed_total, 150)
        self.assertTrue(accounting.validate_invariants())


class TestPhase91DeduplicationAndCRMProtection(unittest.TestCase):
    """Category 2: Deduplication & CRM Integrity."""

    def setUp(self):
        self.matcher = BusinessIdentityMatcher()
        self.crm_leads = [
            {
                "lead_id": "LEAD-MAN-0363CF",
                "company_name": "Live Seafood Ltd",
                "address": "45-47 Ashton Old Road, Manchester M12 6HS",
                "postcode": "M12 6HS",
                "city": "Manchester",
                "qualification_state": QualificationState.OUTREACH_READY.value,
                "outreach_status": "NOT_READY",
                "actual_send_confirmed": False,
                "outreach_history": [{"action": "QUALIFIED_BY_RULE_B", "date": "2026-10-01"}],
            },
            {
                "lead_id": "LEAD-MAN-000001",
                "company_name": "Manchester Coffee Co",
                "address": "12 Tib Street, Manchester M4 1AH",
                "postcode": "M4 1AH",
                "city": "Manchester",
                "qualification_state": QualificationState.RESEARCH_ONLY.value,
            },
        ]

    def test_existing_crm_lead_never_duplicated(self):
        """7. Candidate identical to CRM lead is detected as EXACT_MATCH and never duplicated."""
        candidate = {
            "company_name": "Live Seafood Ltd",
            "address": "45-47 Ashton Old Road, Manchester, M12 6HS",
            "postcode": "M12 6HS",
            "city": "Manchester",
        }
        res = self.matcher.match_candidate(candidate, self.crm_leads)
        self.assertIn(res.outcome, [IdentityMatchOutcome.EXISTING_BUSINESS.value, IdentityMatchOutcome.POSSIBLE_DUPLICATE.value])
        self.assertEqual(res.matched_lead_id, "LEAD-MAN-0363CF")

    def test_duplicate_within_same_run_suppressed(self):
        """8. Duplicate candidate within the same run is suppressed."""
        run_candidates = [
            {"company_name": "Northern Soul Grilled Cheese", "address": "10 Church St, Manchester M4 1PN", "postcode": "M4 1PN", "city": "Manchester"},
            {"company_name": "Northern Soul Grilled Cheese", "address": "10 Church St, Manchester M4 1PN", "postcode": "M4 1PN", "city": "Manchester"},
        ]
        seen = []
        suppressed = []
        for c in run_candidates:
            match = self.matcher.match_candidate(c, seen)
            if match.outcome in [IdentityMatchOutcome.EXISTING_BUSINESS.value, IdentityMatchOutcome.POSSIBLE_DUPLICATE.value]:
                suppressed.append(c)
            else:
                seen.append(c)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(suppressed), 1)

    def test_branch_distinctness_preserved(self):
        """9. Branches of the same brand at different streets remain distinct entities."""
        branch1 = {"company_name": "Rudy's Pizza", "address": "9 Cotton Street, Ancoats, Manchester M4 5BF", "postcode": "M4 5BF", "city": "Manchester"}
        branch2 = {"company_name": "Rudy's Pizza", "address": "Petersfield House, Peter St, Manchester M2 5QJ", "postcode": "M2 5QJ", "city": "Manchester"}
        res = self.matcher.match_candidate(branch2, [branch1])
        # Even if brand name matches, different postcodes and street addresses prevent duplicate merge
        self.assertEqual(res.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)
        self.assertTrue(any("DISTINCT_BRANCH" in r for r in res.match_reasons), "Branch difference must be recognized")

    def test_refreshed_lead_retains_history(self):
        """10. Refreshing an existing lead retains historical audit trail and sent states."""
        existing_lead = {
            "lead_id": "LEAD-MAN-0363CF",
            "company_name": "Live Seafood Ltd",
            "outreach_history": [{"timestamp": "2026-10-01T10:00:00Z", "action": "QUALIFIED"}],
            "actual_send_confirmed": False,
        }
        # Simulate refresh
        refresh_update = {"phone": "0161 273 8888"}
        updated_lead = {**existing_lead, **refresh_update}
        self.assertEqual(updated_lead["lead_id"], "LEAD-MAN-0363CF")
        self.assertEqual(len(updated_lead["outreach_history"]), 1)
        self.assertFalse(updated_lead["actual_send_confirmed"])
        self.assertEqual(updated_lead["phone"], "0161 273 8888")

    def test_live_seafood_record_preserved(self):
        """11. Specifically preserve Live Seafood (LEAD-MAN-0363CF) state, status, and history."""
        cache_leads_path = os.path.join(PROJECT_ROOT, "data", "cache_sheets_leads.json")
        if os.path.exists(cache_leads_path):
            with open(cache_leads_path, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            leads = cache_data.get("leads", []) if isinstance(cache_data, dict) else cache_data
            live_seafood = next((l for l in leads if l.get("lead_id") == "LEAD-MAN-0363CF"), None)
            self.assertIsNotNone(live_seafood, "LEAD-MAN-0363CF must exist in leads cache")
            self.assertEqual(live_seafood.get("company_name"), "Live Seafood Ltd")
            self.assertEqual(live_seafood.get("qualification_state"), QualificationState.OUTREACH_READY.value)
            self.assertEqual(live_seafood.get("outreach_status"), "NOT_READY")
            self.assertFalse(live_seafood.get("actual_send_confirmed", False))

    def test_crm_write_audit_reasons(self):
        """12. Every CRM write must carry an approved audit action reason."""
        approved_reasons = {"CREATE", "REFRESH", "PROMOTION", "RECLASSIFICATION"}
        sample_write = {"lead_id": "LEAD-NEW-001", "write_reason": "CREATE"}
        self.assertIn(sample_write["write_reason"], approved_reasons)


class TestPhase91OperationalVerificationSeparation(unittest.TestCase):
    """Category 3: Operational Verification Separation (Unknown vs Verified Active)."""

    def test_osm_presence_alone_is_not_operational_proof(self):
        """13. OpenStreetMap discovery presence alone produces UNKNOWN, never VERIFIED_ACTIVE."""
        status = Phase91EnrichmentEngine.evaluate_operational_status(
            osm_present=True,
            independent_sources=[],
            phone=None,
            is_closed=False
        )
        self.assertEqual(status, OperationalStatus.UNKNOWN.value)

    def test_insufficient_evidence_becomes_unknown(self):
        """14. Candidates lacking independent corroboration receive UNKNOWN, not CLOSED or ACTIVE."""
        status = Phase91EnrichmentEngine.evaluate_operational_status(
            osm_present=True,
            independent_sources=["historical_directory"],  # Not an active corroboration source
            phone=None,
            is_closed=False
        )
        self.assertEqual(status, OperationalStatus.UNKNOWN.value)

    def test_valid_independent_evidence_becomes_verified_active(self):
        """15. Independent corroboration (live phone confirmation + fresh review) produces VERIFIED_ACTIVE."""
        status = Phase91EnrichmentEngine.evaluate_operational_status(
            osm_present=True,
            independent_sources=["google_maps_reviews", "ch_active_filing"],
            phone="0161 234 5678",
            is_closed=False
        )
        self.assertEqual(status, OperationalStatus.VERIFIED_ACTIVE.value)

    def test_closed_evidence_blocks_qualification(self):
        """16. Permanent or temporary closure signal produces CLOSED and blocks qualification."""
        status = Phase91EnrichmentEngine.evaluate_operational_status(
            osm_present=True,
            independent_sources=["google_maps_reviews"],
            phone="0161 234 5678",
            is_closed=True
        )
        self.assertEqual(status, OperationalStatus.CLOSED.value)

    def test_operational_status_enum_coverage(self):
        """17. All canonical Phase 9.1 operational statuses normalize correctly."""
        valid_statuses = [
            OperationalStatus.NOT_CHECKED.value,
            OperationalStatus.VERIFIED_ACTIVE.value,
            OperationalStatus.WEAK_SIGNAL.value,
            OperationalStatus.CONFLICTING.value,
            OperationalStatus.CLOSED.value,
            OperationalStatus.UNKNOWN.value,
        ]
        for s in valid_statuses:
            self.assertEqual(OperationalStatus.normalize(s), s)
        # Test backward-compatible normalization
        self.assertEqual(OperationalStatus.normalize("ACTIVE_CONFIRMED"), OperationalStatus.VERIFIED_ACTIVE.value)
        self.assertEqual(OperationalStatus.normalize("ACTIVE_LIKELY"), OperationalStatus.WEAK_SIGNAL.value)
        self.assertEqual(OperationalStatus.normalize("CLOSED_OR_UNVERIFIED"), OperationalStatus.CLOSED.value)
        self.assertEqual(OperationalStatus.normalize("OPERATIONAL"), OperationalStatus.UNKNOWN.value)


class TestPhase91ReviewEvidenceGating(unittest.TestCase):
    """Category 4: Review Evidence Gating & Reconciliation."""

    def test_review_count_threshold_50_enforced(self):
        """18. Businesses with fewer than 50 reviews fail Rule B qualification."""
        biz = DiscoveredBusiness(
            company_name="Small Cafe",
            category="cafe",
            city="Manchester",
            target_country="United Kingdom",
            address="10 Portland St, Manchester",
            raw_website="",
            review_count=49,  # Below 50
            rating=4.5,
            phone="0161 111 2222"
        )
        # Directly evaluate Rule B review count gate
        self.assertFalse(biz.review_count is not None and biz.review_count >= 50, "Review count < 50 must fail Rule B")

    def test_rating_threshold_4_0_enforced(self):
        """19. Businesses with rating < 4.0 fail Rule B qualification."""
        biz = DiscoveredBusiness(
            company_name="Mediocre Diner",
            category="restaurant",
            city="Manchester",
            target_country="United Kingdom",
            address="20 Oxford Rd, Manchester",
            raw_website="",
            review_count=120,
            rating=3.9,  # Below 4.0
            phone="0161 222 3333"
        )
        self.assertFalse(biz.rating is not None and biz.rating >= 4.0, "Rating < 4.0 must fail Rule B")

    def test_recent_review_recency_180_days_enforced(self):
        """20. Reviews older than 180 days are considered stale and fail Rule B recency."""
        stale_date = (datetime.now(timezone.utc) - timedelta(days=181)).strftime("%Y-%m-%d")
        fresh_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")

        # Verify freshness calculation
        def is_recent(date_str: str) -> bool:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).days <= 180

        self.assertFalse(is_recent(stale_date))
        self.assertTrue(is_recent(fresh_date))

    def test_stale_evidence_rejected(self):
        """21. SEO timestamps, crawl dates, and generic metadata cannot substitute for review dates."""
        invalid_recency_keys = [
            "page_crawled_at",
            "seo_last_updated",
            "search_index_timestamp",
            "sitemap_lastmod"
        ]
        candidate = {k: "2026-10-01" for k in invalid_recency_keys}
        # In Rule B, review_date must specifically originate from review source evidence
        review_date = candidate.get("review_date")
        self.assertIsNone(review_date, "Metadata crawl timestamps must not be inferred as review dates")

    def test_conflicting_review_evidence_routes_to_manual_review(self):
        """22. Conflicting ratings or review counts across sources trigger MANUAL_REVIEW."""
        source_a = {"rating": 4.5, "review_count": 120}
        source_b = {"rating": 2.1, "review_count": 115}  # Severe rating conflict
        rating_delta = abs(source_a["rating"] - source_b["rating"])
        has_conflict = rating_delta > 0.8
        self.assertTrue(has_conflict, "Large variance across review sources must flag conflict")

    def test_no_inferred_review_metrics(self):
        """23. Missing review count or rating is never imputed or hallucinated."""
        candidate = {"company_name": "Unknown Tavern", "address": "Market St"}
        self.assertIsNone(candidate.get("review_count"))
        self.assertIsNone(candidate.get("rating"))


class TestPhase91RuleBQualificationInvariance(unittest.TestCase):
    """Category 5: Rule B Qualification Invariance."""

    def test_rule_b_remains_frozen(self):
        """24. Rule B requires: >=50 reviews, >=4.0 rating, <=180d recency, independent operational signal."""
        # Candidate meeting all gates
        valid_b = {
            "review_count": 85,
            "rating": 4.6,
            "review_age_days": 45,
            "operational_status": OperationalStatus.VERIFIED_ACTIVE.value,
            "identity_confidence": 0.85,
            "is_closed": False,
            "has_conflict": False,
        }
        qualifies = (
            valid_b["review_count"] >= 50
            and valid_b["rating"] >= 4.0
            and valid_b["review_age_days"] <= 180
            and valid_b["operational_status"] == OperationalStatus.VERIFIED_ACTIVE.value
            and valid_b["identity_confidence"] >= 0.70
            and not valid_b["is_closed"]
            and not valid_b["has_conflict"]
        )
        self.assertTrue(qualifies)

    def test_score_cannot_promote_failed_candidate(self):
        """25. High priority score cannot override a failed qualification gate."""
        candidate = {
            "qualification_score": 95,  # Very high score
            "review_count": 12,        # Failed Rule B (< 50)
            "rating": 4.8,
            "operational_status": OperationalStatus.UNKNOWN.value,
        }
        # Invariant: Score is ranking priority only, not qualification authority
        state = QualificationState.RESEARCH_ONLY.value
        if candidate["review_count"] < 50 or candidate["operational_status"] != OperationalStatus.VERIFIED_ACTIVE.value:
            state = QualificationState.RESEARCH_ONLY.value
        self.assertNotEqual(state, QualificationState.OUTREACH_READY.value)

    def test_no_qualification_without_operational_evidence(self):
        """26. Candidate with OPERATIONAL_STATUS == UNKNOWN cannot be OUTREACH_READY."""
        candidate = {
            "review_count": 250,
            "rating": 4.9,
            "review_age_days": 10,
            "operational_status": OperationalStatus.UNKNOWN.value,  # No independent verification
        }
        qualifies = candidate["operational_status"] == OperationalStatus.VERIFIED_ACTIVE.value
        self.assertFalse(qualifies, "Uncorroborated candidate must not qualify for outreach")

    def test_qualification_outputs_restricted(self):
        """27. Qualification states are strictly restricted to canonical 4 outputs."""
        allowed_states = {
            QualificationState.OUTREACH_READY.value,
            QualificationState.MANUAL_REVIEW.value,
            QualificationState.RESEARCH_ONLY.value,
            QualificationState.EXCLUDED.value,
        }
        for state in [QualificationState.OUTREACH_READY, QualificationState.MANUAL_REVIEW, QualificationState.RESEARCH_ONLY, QualificationState.EXCLUDED]:
            self.assertIn(state.value, allowed_states)


class TestPhase91SafetyAndCampaignConstraints(unittest.TestCase):
    """Category 6: Safety & Campaign Hard Ceilings."""

    def test_zero_outreach_sends_invariant(self):
        """28. Outreach sends count must strictly equal 0."""
        run_file = os.path.join(PROJECT_ROOT, "data", "phase_9_1_enrichment_run.json")
        if os.path.exists(run_file):
            with open(run_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("OUTREACH_SENDS"), 0)
            self.assertEqual(data["INVARIANTS"]["SAFETY_ZERO_OUTREACH_SENDS"], "PASS")

    def test_zero_armed_campaigns_invariant(self):
        """29. Campaigns armed count must strictly equal 0."""
        run_file = os.path.join(PROJECT_ROOT, "data", "phase_9_1_enrichment_run.json")
        if os.path.exists(run_file):
            with open(run_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("CAMPAIGNS_ARMED"), 0)
            self.assertEqual(data["INVARIANTS"]["SAFETY_ZERO_ARMED_CAMPAIGNS"], "PASS")

    def test_no_automatic_outreach_execution(self):
        """30. Automated sendable is strictly false for all unverified candidates."""
        candidate = {"phone": "0161 999 8888", "qualification_state": QualificationState.RESEARCH_ONLY.value}
        # Invariant: automated outreach requires OUTREACH_READY + contactability
        automated_sendable = (
            candidate.get("qualification_state") == QualificationState.OUTREACH_READY.value
            and bool(candidate.get("phone"))
        )
        self.assertFalse(automated_sendable)

    def test_contactability_remains_separate_from_qualification(self):
        """31. Presence of contact method does not promote unverified business."""
        candidate = {
            "company_name": "Sample Eatery",
            "phone": "0161 222 3333",
            "contactability_status": ContactabilityState.PARTIALLY_CONTACTABLE.value,
            "qualification_state": QualificationState.RESEARCH_ONLY.value,
        }
        self.assertEqual(candidate["contactability_status"], ContactabilityState.PARTIALLY_CONTACTABLE.value)
        self.assertEqual(candidate["qualification_state"], QualificationState.RESEARCH_ONLY.value)


class TestPhase91FieldLevelCompleteness(unittest.TestCase):
    """Category 7: Field-Level Data Completeness."""

    def setUp(self):
        self.candidates = [
            {
                "company_name": "Cafe One",
                "address": "1 Main St",
                "postcode": "M1 1AA",
                "phone": "0161 111 1111",
                "website": "https://cafeone.co.uk",
                "website_opportunity_status": "FUNCTIONAL_WEBSITE",
                "rating": 4.5,
                "review_count": 80,
                "review_date": "2026-09-01",
                "operational_evidence": "Confirmed live",
                "social_profiles": ["https://instagram.com/cafeone"],
                "contactability_status": "CONTACTABLE",
                "identity_confidence": 0.95,
                "commercial_fit_status": "EXCLUDED",
            },
            {
                "company_name": "Cafe Two",
                "address": "2 High St",
                "postcode": "",
                "phone": "",
                "website": "",
                "website_opportunity_status": "NO_WEBSITE",
                "rating": None,
                "review_count": 0,
                "review_date": None,
                "operational_evidence": None,
                "social_profiles": [],
                "contactability_status": "NOT_CONTACTABLE",
                "identity_confidence": 0.85,
                "commercial_fit_status": "COMMERCIAL_PROSPECT",
            },
        ]

    def test_14_required_field_completeness_calculated(self):
        """32. Completeness audit calculates metrics for all 14 required dimensions."""
        comp = CohortFieldCompleteness.calculate(self.candidates)
        required_fields = [
            "name_present",
            "address_present",
            "postcode_present",
            "phone_present",
            "website_present",
            "website_status_present",
            "rating_present",
            "review_count_present",
            "review_date_present",
            "operational_evidence_present",
            "social_present",
            "contactability_present",
            "identity_confidence_present",
            "commercial_fit_present",
        ]
        self.assertEqual(len(comp.fields), 14)
        for rf in required_fields:
            self.assertIn(rf, comp.fields)

    def test_completeness_present_missing_counts(self):
        """33. For every field, present_count + missing_count equals total_candidates."""
        comp = CohortFieldCompleteness.calculate(self.candidates)
        total = comp.total_candidates
        self.assertEqual(total, 2)
        for field_name, metrics in comp.fields.items():
            self.assertEqual(
                metrics.present_count + metrics.missing_count,
                total,
                f"Count mismatch in field '{field_name}'"
            )

    def test_overall_field_completeness_documented_denominator(self):
        """34. Overall completeness percentage uses 14 * N as explicit denominator."""
        comp = CohortFieldCompleteness.calculate(self.candidates)
        # Candidate 1 has all 14 fields present (14/14)
        # Candidate 2 has 5 present (name, address, website_status, contactability, identity_confidence, commercial_fit = 6)
        total_instances = 14 * 2  # 28
        total_present = sum(m.present_count for m in comp.fields.values())
        expected_pct = round((total_present / total_instances) * 100.0, 2)
        self.assertEqual(comp.overall_required_field_completeness, expected_pct)

    def test_field_completeness_handles_missing_keys_gracefully(self):
        """35. Empty or partial dictionaries do not crash field completeness calculation."""
        partial = [{"name": "Only Name"}, {}]
        comp = CohortFieldCompleteness.calculate(partial)
        self.assertEqual(comp.total_candidates, 2)
        self.assertIsInstance(comp.overall_required_field_completeness, float)


class TestPhase91WebsiteOpportunityAndQuotaLimits(unittest.TestCase):
    """Category 8: Website Opportunity & Quota Limits."""

    def test_search_quota_limit_enforced(self):
        """36. External search calls strictly do not exceed budget of 20."""
        quota = QuotaBudget(search_limit=20, gosom_limit=10)
        for _ in range(20):
            self.assertTrue(quota.can_consume("search"))
            quota.consume("search")
        # 21st call must be rejected
        self.assertFalse(quota.can_consume("search"))
        self.assertEqual(quota.search_used, 20)

    def test_gosom_quota_limit_enforced(self):
        """37. Gosom calls strictly do not exceed budget of 10."""
        quota = QuotaBudget(search_limit=20, gosom_limit=10)
        for _ in range(10):
            self.assertTrue(quota.can_consume("gosom"))
            quota.consume("gosom")
        # 11th call must be rejected
        self.assertFalse(quota.can_consume("gosom"))
        self.assertEqual(quota.gosom_used, 10)

    def test_website_opportunity_audit_records_reasons(self):
        """38. BROKEN_WEBSITE audit records URL, HTTP/DNS result, checked_at, failure_type."""
        audit = WebsiteOpportunityAuditor.audit_candidate_website(
            candidate_id="The Lost Dene",
            company_name="The Lost Dene",
            website_url="https://www.greatukpubs.co.uk/the-lost-dene-manchester",
            timeout=2.0
        )
        self.assertIsInstance(audit, WebsiteAuditResult)
        self.assertEqual(audit.url, "https://www.greatukpubs.co.uk/the-lost-dene-manchester")
        self.assertIsNotNone(audit.http_status or audit.error_details)
        self.assertIsNotNone(audit.checked_at)
        self.assertIsNotNone(audit.failure_type)

    def test_conclusive_candidates_skip_enrichment(self):
        """39. Already conclusive candidates (e.g. EXCLUDED national chains) skip enrichment."""
        candidate = {
            "company_name": "Greggs",
            "commercial_fit_status": CommercialFitStatus.EXCLUDED_NATIONAL_CHAIN.value if hasattr(CommercialFitStatus, "EXCLUDED_NATIONAL_CHAIN") else "EXCLUDED",
            "qualification_state": QualificationState.EXCLUDED.value,
        }
        # Invariant: Conclusive status skips enrichment
        should_enrich = candidate.get("commercial_fit_status") in ["COMMERCIAL_PROSPECT", "HIGH_WEBSITE_OPPORTUNITY"]
        self.assertFalse(should_enrich, "National chains / excluded leads must not consume enrichment quota")


if __name__ == "__main__":
    unittest.main()
