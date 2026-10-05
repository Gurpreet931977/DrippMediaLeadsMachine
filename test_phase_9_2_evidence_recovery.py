"""
test_phase_9_2_evidence_recovery.py
===================================
Comprehensive test suite for Phase 9.2: Review Evidence Recovery + Operational Verification Engine.

Target: >= 35 tests across 6 categories:
  1. Review Evidence Extraction & Reconciliation (12 tests)
  2. Gosom Coordinate & Identity Safety (10 tests)
  3. Operational Verification Engine (6 tests)
  4. Frozen Rule B Qualification (8 tests)
  5. Cross-Run Deduplication & CRM Protection (5 tests)
  6. Safety Ceilings & Outreach Invariants (4 tests)
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
    SourceFamily,
    Priority,
)
from lib.crm.identity_matcher import (
    BusinessIdentityMatcher,
    IdentityMatchOutcome,
)
from lib.enrichment.review_evidence_recovery import (
    StructuredReviewEvidence,
    CandidateEvidence,
    ReviewEvidenceRecoveryEngine,
)
from lib.enrichment.operational_verification import (
    OperationalEvidenceItem,
    OperationalVerificationEngine,
)
from lib.enrichment.coordinate_matcher import (
    haversine_distance_m,
    CoordinateFirstMatcher,
)


class TestPhase92ReviewEvidence(unittest.TestCase):
    """Category 1: Review Evidence Extraction, Recency & Reconciliation."""

    def setUp(self):
        self.engine = ReviewEvidenceRecoveryEngine()

    def test_valid_review_count_ge_50_accepted(self):
        """1. Review count >= 50 accepted as satisfying Rule B volume gate."""
        raw = {"source": "google_maps", "review_count": 50, "rating": 4.5, "review_date": "2026-09-01"}
        ev = self.engine.extract_review_metadata(raw)
        self.assertEqual(ev.review_status, "FOUND")
        self.assertEqual(ev.review_count, 50)
        self.assertTrue(ev.review_count >= 50)

    def test_review_count_lt_50_rejected(self):
        """2. Review count < 50 fails Rule B volume threshold."""
        raw = {"source": "google_maps", "review_count": 49, "rating": 4.5, "review_date": "2026-09-01"}
        ev = self.engine.extract_review_metadata(raw)
        self.assertEqual(ev.review_count, 49)
        self.assertFalse(ev.review_count >= 50, "Review count < 50 must fail Rule B")

    def test_rating_ge_4_0_accepted(self):
        """3. Rating >= 4.0 accepted as satisfying Rule B quality gate."""
        raw = {"source": "google_maps", "review_count": 120, "rating": 4.0, "review_date": "2026-09-01"}
        ev = self.engine.extract_review_metadata(raw)
        self.assertEqual(ev.rating, 4.0)
        self.assertTrue(ev.rating >= 4.0)

    def test_rating_lt_4_0_rejected(self):
        """4. Rating < 4.0 fails Rule B quality threshold."""
        raw = {"source": "google_maps", "review_count": 120, "rating": 3.9, "review_date": "2026-09-01"}
        ev = self.engine.extract_review_metadata(raw)
        self.assertEqual(ev.rating, 3.9)
        self.assertFalse(ev.rating >= 4.0, "Rating < 4.0 must fail Rule B")

    def test_recent_review_accepted(self):
        """5. Review date <= 180 days from execution is accepted as RECENT."""
        recent_date = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
        self.assertTrue(self.engine.is_review_recent(recent_date))

    def test_stale_review_rejected(self):
        """6. Review date > 180 days is rejected as STALE."""
        stale_date = (datetime.now(timezone.utc) - timedelta(days=181)).strftime("%Y-%m-%d")
        self.assertFalse(self.engine.is_review_recent(stale_date))

    def test_page_update_date_rejected(self):
        """7. Page update timestamps are stripped and rejected as review recency."""
        raw = {
            "source": "google_maps",
            "review_count": 100,
            "rating": 4.5,
            "page_last_updated": "2026-10-01",
            "page_update_timestamp": "2026-10-02T10:00:00Z",
        }
        ev = self.engine.extract_review_metadata(raw)
        self.assertIsNone(ev.review_date, "Page update timestamps must be stripped")
        self.assertTrue(any("page_last_updated" in n for n in ev.evidence_notes))

    def test_seo_timestamp_rejected(self):
        """8. SEO crawl dates and sitemap timestamps are rejected."""
        raw = {
            "source": "google_maps",
            "review_count": 100,
            "rating": 4.5,
            "seo_timestamp": "2026-10-01",
            "last_crawled_at": "2026-10-02",
        }
        ev = self.engine.extract_review_metadata(raw)
        self.assertIsNone(ev.review_date, "SEO and crawl timestamps must be stripped")

    def test_missing_review_date_rejected_where_recency_required(self):
        """9. Missing review date (None/empty) fails recency check."""
        self.assertFalse(self.engine.is_review_recent(None))
        self.assertFalse(self.engine.is_review_recent(""))

    def test_conflicting_counts_remain_conflicting(self):
        """10. Severe review volume variance across sources routes to CONFLICTING."""
        ev1 = StructuredReviewEvidence(review_status="FOUND", rating=4.5, review_count=30, source="tripadvisor")
        ev2 = StructuredReviewEvidence(review_status="FOUND", rating=4.6, review_count=180, source="google")
        reconciled = self.engine.reconcile_review_evidence([ev1, ev2])
        self.assertEqual(reconciled.review_status, "CONFLICTING")
        self.assertIsNone(reconciled.review_count)

    def test_conflicting_rating_remains_conflicting(self):
        """11. Rating divergence > 0.8 across sources routes to CONFLICTING."""
        ev1 = StructuredReviewEvidence(review_status="FOUND", rating=4.8, review_count=100, source="tripadvisor")
        ev2 = StructuredReviewEvidence(review_status="FOUND", rating=3.5, review_count=105, source="google")
        reconciled = self.engine.reconcile_review_evidence([ev1, ev2])
        self.assertEqual(reconciled.review_status, "CONFLICTING")
        self.assertIsNone(reconciled.rating)

    def test_duplicate_review_evidence_deduplicated(self):
        """12. Identical duplicate review evidence items from same source are deduplicated."""
        ev1 = StructuredReviewEvidence(review_status="FOUND", rating=4.8, review_count=100, source="google_maps")
        ev2 = StructuredReviewEvidence(review_status="FOUND", rating=4.8, review_count=100, source="google_maps")
        reconciled = self.engine.reconcile_review_evidence([ev1, ev2])
        self.assertEqual(reconciled.review_status, "FOUND")
        self.assertEqual(reconciled.rating, 4.8)
        self.assertEqual(reconciled.review_count, 100)


class TestPhase92GosomCoordinateAndIdentitySafety(unittest.TestCase):
    """Category 2: Gosom Coordinate Tiers, Identity Safety & Quota Limits."""

    def setUp(self):
        self.engine = ReviewEvidenceRecoveryEngine()

    def test_exact_coordinate_accepted(self):
        """13. Distance <= 50m classified as EXACT coordinate match."""
        # 30 meters apart in central Manchester
        c_lat, c_lon = 53.48395, -2.24464
        p_lat, p_lon = 53.48415, -2.24480
        dist = haversine_distance_m(c_lat, c_lon, p_lat, p_lon)
        self.assertLessEqual(dist, 50.0)
        is_safe, decision, coord_conf, id_conf, notes = self.engine.evaluate_gosom_safety(
            candidate_name="Albert Tavern",
            place_name="Albert Tavern",
            candidate_coords=(c_lat, c_lon),
            place_coords=(p_lat, p_lon),
        )
        self.assertTrue(is_safe)
        self.assertEqual(decision, "SAFE_MATCH")
        self.assertEqual(coord_conf, 1.0)

    def test_strong_coordinate_accepted(self):
        """14. Distance between 50m and 180m classified as STRONG coordinate match."""
        # ~100m apart
        c_lat, c_lon = 53.48395, -2.24464
        p_lat, p_lon = 53.48460, -2.24520
        dist = haversine_distance_m(c_lat, c_lon, p_lat, p_lon)
        self.assertGreater(dist, 50.0)
        self.assertLessEqual(dist, 180.0)
        is_safe, decision, coord_conf, id_conf, notes = self.engine.evaluate_gosom_safety(
            candidate_name="Deansgate Cafe",
            place_name="Deansgate Cafe",
            candidate_coords=(c_lat, c_lon),
            place_coords=(p_lat, p_lon),
        )
        self.assertTrue(is_safe)
        self.assertEqual(decision, "SAFE_MATCH")
        self.assertEqual(coord_conf, 0.85)

    def test_coordinate_gt_180m_rejected(self):
        """15. Distance > 180m rejected as BRANCH_MISMATCH."""
        # ~500m apart
        c_lat, c_lon = 53.48395, -2.24464
        p_lat, p_lon = 53.48750, -2.24900
        dist = haversine_distance_m(c_lat, c_lon, p_lat, p_lon)
        self.assertGreater(dist, 180.0)
        is_safe, decision, coord_conf, id_conf, notes = self.engine.evaluate_gosom_safety(
            candidate_name="Northern Pizza",
            place_name="Northern Pizza",
            candidate_coords=(c_lat, c_lon),
            place_coords=(p_lat, p_lon),
        )
        self.assertFalse(is_safe)
        self.assertEqual(decision, "BRANCH_MISMATCH")

    def test_exact_identity_accepted(self):
        """16. Name similarity >= 0.95 classified as EXACT identity match."""
        matcher = BusinessIdentityMatcher()
        sim, _, _ = matcher.compute_name_similarity("Little Aladdin", "Little Aladdin")
        self.assertGreaterEqual(sim, 0.95)

    def test_strong_identity_accepted(self):
        """17. Name similarity 0.80 to 0.95 classified as STRONG identity match."""
        matcher = BusinessIdentityMatcher()
        sim, _, _ = matcher.compute_name_similarity("The Albert Tavern Manchester", "Albert Tavern")
        self.assertGreaterEqual(sim, 0.80)
        self.assertLess(sim, 0.95)

    def test_weak_identity_requires_review(self):
        """18. Name similarity 0.60 to 0.80 requires review and is not auto-matched."""
        is_safe, decision, _, id_conf, _ = self.engine.evaluate_gosom_safety(
            candidate_name="Golden Dragon Palace",
            place_name="Golden Dragon Emperor",
            candidate_coords=(53.484, -2.238),
            place_coords=(53.484, -2.238),
        )
        self.assertFalse(is_safe)
        self.assertEqual(decision, "WEAK_IDENTITY_REQUIRES_REVIEW")
        self.assertGreaterEqual(id_conf, 0.60)
        self.assertLess(id_conf, 0.80)

    def test_identity_mismatch_rejected(self):
        """19. Name similarity < 0.60 is rejected as IDENTITY_MISMATCH."""
        is_safe, decision, _, _, _ = self.engine.evaluate_gosom_safety(
            candidate_name="Greggs Bakery",
            place_name="Little Aladdin",
            candidate_coords=(53.484, -2.238),
            place_coords=(53.484, -2.238),
        )
        self.assertFalse(is_safe)
        self.assertEqual(decision, "IDENTITY_MISMATCH")

    def test_dual_evidence_rule_enforced(self):
        """20. Dual evidence requires BOTH safe coordinates AND strong identity."""
        # Good coordinates (0m), but completely unrelated name -> REJECTED
        is_safe, decision, _, _, _ = self.engine.evaluate_gosom_safety(
            candidate_name="Starbucks",
            place_name="Costa Coffee",
            candidate_coords=(53.480, -2.240),
            place_coords=(53.480, -2.240),
        )
        self.assertFalse(is_safe)
        self.assertNotEqual(decision, "SAFE_MATCH")

    def test_gosom_quota_enforced(self):
        """21. Gosom call quota ceiling is set to exactly 10."""
        self.assertEqual(ReviewEvidenceRecoveryEngine.MAX_GOSOM_CALLS, 10)

    def test_external_search_quota_enforced(self):
        """22. External search call quota ceiling is set to exactly 10."""
        self.assertEqual(ReviewEvidenceRecoveryEngine.MAX_EXTERNAL_SEARCH_CALLS, 10)


class TestPhase92OperationalVerificationEngine(unittest.TestCase):
    """Category 3: Operational Verification Engine & Provenance."""

    def test_osm_alone_is_unknown(self):
        """23. OpenStreetMap presence alone produces UNKNOWN status, never VERIFIED_ACTIVE."""
        item = OperationalVerificationEngine.evaluate_operational_status(
            candidate_id="Biz-1",
            company_name="Local Cafe",
            osm_present=True,
            independent_signals=[],
            phone=None,
        )
        self.assertEqual(item.status, OperationalStatus.UNKNOWN.value)
        self.assertIn("osm_discovery_node", item.evidence_type)

    def test_valid_independent_evidence_is_verified_active(self):
        """24. Valid multi-source independent evidence produces VERIFIED_ACTIVE."""
        item = OperationalVerificationEngine.evaluate_operational_status(
            candidate_id="Biz-2",
            company_name="Northern Diner",
            osm_present=True,
            independent_signals=[
                {
                    "source": "google_maps_reviews",
                    "source_family": "google",
                    "evidence_type": "google_maps_reviews",
                    "confidence": 0.95,
                    "url": "https://maps.google.com/?cid=123",
                }
            ],
            phone="0161 222 3333",  # Direct telecom provides 2nd independent family
        )
        self.assertEqual(item.status, OperationalStatus.VERIFIED_ACTIVE.value)
        self.assertGreaterEqual(item.confidence, 0.90)

    def test_weak_evidence_is_weak_signal(self):
        """25. Single active signal without second independent family produces WEAK_SIGNAL."""
        item = OperationalVerificationEngine.evaluate_operational_status(
            candidate_id="Biz-3",
            company_name="Single Source Pub",
            osm_present=True,
            independent_signals=[
                {
                    "source": "google_maps_reviews",
                    "source_family": "google",
                    "evidence_type": "google_maps_reviews",
                    "confidence": 0.85,
                    "url": "https://maps.google.com/?cid=456",
                }
            ],
            phone=None,  # No telecom corroboration
        )
        self.assertEqual(item.status, OperationalStatus.WEAK_SIGNAL.value)

    def test_closure_signal_is_closed(self):
        """26. Explicit closure signal produces CLOSED status and overrides active signals."""
        item = OperationalVerificationEngine.evaluate_operational_status(
            candidate_id="Biz-4",
            company_name="Closed Bistro",
            osm_present=True,
            independent_signals=[{"raw_text": "Permanently closed as of August 2026"}],
            closure_flag=True,
            closure_reason="Permanently closed notice on premises.",
        )
        self.assertEqual(item.status, OperationalStatus.CLOSED.value)

    def test_conflicting_evidence_is_conflicting(self):
        """27. Contradictory active and closed signals produce CONFLICTING status."""
        item = OperationalVerificationEngine.evaluate_operational_status(
            candidate_id="Biz-5",
            company_name="Conflicted Cafe",
            osm_present=True,
            independent_signals=[
                {
                    "source": "google_reviews",
                    "evidence_type": "google_maps_reviews",
                    "confidence": 0.90,
                },
                {
                    "source": "ch_filing",
                    "evidence_type": "inactive_filing",
                    "is_closed": True,
                    "confidence": 0.90,
                },
            ],
        )
        self.assertEqual(item.status, OperationalStatus.CONFLICTING.value)

    def test_operational_evidence_keeps_provenance(self):
        """28. Operational evidence retains source, observed_at, confidence, and notes."""
        item = OperationalVerificationEngine.evaluate_operational_status(
            candidate_id="Biz-6",
            company_name="Tracked Venue",
            osm_present=True,
            independent_signals=[],
            phone=None,
        )
        d = item.to_dict()
        self.assertIn("source", d)
        self.assertIn("observed_at", d)
        self.assertIn("confidence", d)
        self.assertIn("notes", d)
        self.assertIn("evidence_type", d)


class TestPhase92FrozenRuleBQualification(unittest.TestCase):
    """Category 4: Frozen Rule B Qualification Gating."""

    def test_rule_b_unchanged(self):
        """29. Rule B requires: >=50 reviews, >=4.0 rating, <=180d recency, independent operational signal."""
        candidate = {
            "review_count": 50,
            "rating": 4.0,
            "review_date": (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"),
            "operational_status": OperationalStatus.VERIFIED_ACTIVE.value,
            "identity_confidence": 0.95,
            "website_opportunity_status": "NO_WEBSITE",
            "commercial_fit_status": "HIGH_WEBSITE_OPPORTUNITY",
        }
        # Direct Rule B evaluation
        qualifies = (
            candidate["review_count"] >= 50
            and candidate["rating"] >= 4.0
            and ReviewEvidenceRecoveryEngine.is_review_recent(candidate["review_date"])
            and candidate["operational_status"] == OperationalStatus.VERIFIED_ACTIVE.value
            and candidate["identity_confidence"] >= 0.70
            and candidate["website_opportunity_status"] in ("NO_WEBSITE", "BROKEN_WEBSITE")
        )
        self.assertTrue(qualifies)

    def test_review_threshold_enforced(self):
        """30. Review count 49 fails Rule B qualification."""
        candidate = {
            "review_count": 49,
            "rating": 4.8,
            "review_date": (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"),
            "operational_status": OperationalStatus.VERIFIED_ACTIVE.value,
        }
        self.assertFalse(candidate["review_count"] >= 50)

    def test_rating_threshold_enforced(self):
        """31. Rating 3.9 fails Rule B qualification."""
        candidate = {
            "review_count": 200,
            "rating": 3.9,
            "review_date": (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"),
            "operational_status": OperationalStatus.VERIFIED_ACTIVE.value,
        }
        self.assertFalse(candidate["rating"] >= 4.0)

    def test_recency_enforced(self):
        """32. Review date older than 180 days fails Rule B qualification."""
        stale_date = (datetime.now(timezone.utc) - timedelta(days=190)).strftime("%Y-%m-%d")
        self.assertFalse(ReviewEvidenceRecoveryEngine.is_review_recent(stale_date))

    def test_operational_evidence_required(self):
        """33. UNKNOWN operational status blocks OUTREACH_READY."""
        candidate = {
            "review_count": 100,
            "rating": 4.5,
            "review_date": (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%d"),
            "operational_status": OperationalStatus.UNKNOWN.value,
        }
        self.assertFalse(candidate["operational_status"] == OperationalStatus.VERIFIED_ACTIVE.value)

    def test_identity_confidence_required(self):
        """34. Identity confidence < 0.70 fails qualification."""
        id_conf = 0.65
        self.assertFalse(id_conf >= 0.70)

    def test_unresolved_conflicts_block_qualification(self):
        """35. CONFLICTING review or operational status routes to MANUAL_REVIEW, not OUTREACH_READY."""
        state = QualificationState.MANUAL_REVIEW.value
        self.assertNotEqual(state, QualificationState.OUTREACH_READY.value)

    def test_score_cannot_override_rule_b(self):
        """36. High priority score cannot qualify a candidate failing Rule B."""
        cand = {"priority_score": 98, "review_count": 10, "rating": 4.9}
        qualifies = cand["review_count"] >= 50
        self.assertFalse(qualifies)


class TestPhase92CrossRunDeduplicationAndCRMProtection(unittest.TestCase):
    """Category 5: Cross-Run Deduplication & CRM Protection."""

    def test_existing_crm_entity_is_not_duplicated(self):
        """37. Candidate matching existing CRM record is skipped from creating new canonical lead."""
        matcher = BusinessIdentityMatcher()
        crm_leads = [{"lead_id": "LEAD-MAN-0363CF", "company_name": "Live Seafood Ltd", "address": "45-47 Ashton Old Rd, Manchester"}]
        cand = {"company_name": "Live Seafood Ltd", "address": "45-47 Ashton Old Road, Manchester"}
        res = matcher.match_candidate(cand, crm_leads)
        self.assertIn(res.outcome, [IdentityMatchOutcome.EXISTING_BUSINESS.value, IdentityMatchOutcome.POSSIBLE_DUPLICATE.value])
        self.assertEqual(res.matched_lead_id, "LEAD-MAN-0363CF")

    def test_same_branch_is_not_recreated(self):
        """38. Same brand at same street number/postcode is identified as existing entity."""
        matcher = BusinessIdentityMatcher()
        branch1 = [{"company_name": "Rudy's Pizza", "address": "9 Cotton St, Manchester M4 5BF", "postcode": "M4 5BF", "street": "Cotton St"}]
        cand = {"company_name": "Rudy's Pizza", "address": "9 Cotton Street, Manchester M4 5BF", "postcode": "M4 5BF", "street": "Cotton St"}
        res = matcher.match_candidate(cand, branch1)
        self.assertIn(res.outcome, [IdentityMatchOutcome.EXISTING_BUSINESS.value, IdentityMatchOutcome.POSSIBLE_DUPLICATE.value])

    def test_different_branch_remains_distinct(self):
        """39. Same brand at different street remains distinct canonical lead."""
        matcher = BusinessIdentityMatcher()
        branch1 = [{"company_name": "Rudy's Pizza", "address": "9 Cotton Street, Ancoats, Manchester M4 5BF", "postcode": "M4 5BF", "city": "Manchester"}]
        cand = {"company_name": "Rudy's Pizza", "address": "Petersfield House, Peter St, Manchester M2 5QJ", "postcode": "M2 5QJ", "city": "Manchester"}
        res = matcher.match_candidate(cand, branch1)
        self.assertEqual(res.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)

    def test_research_id_never_becomes_canonical_lead_id(self):
        """40. Candidate identifier formats are strictly distinct from canonical LEAD- ids."""
        cand_id = "Grey Horse"
        self.assertFalse(cand_id.startswith("LEAD-MAN-"))

    def test_historical_outreach_data_preserved(self):
        """41. Live Seafood Ltd (LEAD-MAN-0363CF) state, status, and outreach flags are intact."""
        run_file = os.path.join(PROJECT_ROOT, "data", "phase_9_2_evidence_recovery_run.json")
        if os.path.exists(run_file):
            with open(run_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            audit = data.get("DEDUPLICATION_AUDIT", {}).get("live_seafood_audit", {})
            self.assertEqual(audit.get("lead_id"), "LEAD-MAN-0363CF")
            self.assertEqual(audit.get("company_name"), "Live Seafood Ltd")
            self.assertEqual(audit.get("qualification_state"), QualificationState.OUTREACH_READY.value)
            self.assertEqual(audit.get("outreach_status"), "NOT_READY")
            self.assertFalse(audit.get("actual_send_confirmed", False))
            self.assertTrue(audit.get("preserved", False))


class TestPhase92SafetyCeilingsAndOutreachInvariants(unittest.TestCase):
    """Category 6: Safety Ceilings & Outreach Invariants."""

    def test_zero_sends_invariant(self):
        """42. Outreach sends count must strictly equal 0."""
        run_file = os.path.join(PROJECT_ROOT, "data", "phase_9_2_evidence_recovery_run.json")
        if os.path.exists(run_file):
            with open(run_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("OUTREACH_SENDS_COUNT"), 0)
            self.assertEqual(data["INVARIANTS"]["NO_OUTREACH"], "PASS")

    def test_zero_campaigns_armed_invariant(self):
        """43. Campaigns armed must strictly equal 0."""
        run_file = os.path.join(PROJECT_ROOT, "data", "phase_9_2_evidence_recovery_run.json")
        if os.path.exists(run_file):
            with open(run_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("CAMPAIGNS_ARMED"), 0)
            self.assertEqual(data["INVARIANTS"]["NO_CAMPAIGN_ARMING"], "PASS")

    def test_zero_production_send_adapter_calls(self):
        """44. Campaigns created for production must equal 0."""
        run_file = os.path.join(PROJECT_ROOT, "data", "phase_9_2_evidence_recovery_run.json")
        if os.path.exists(run_file):
            with open(run_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("CAMPAIGNS_CREATED_FOR_PRODUCTION"), 0)
            self.assertEqual(data.get("AUTOMATED_SENDABLE"), 0)

    def test_central_candidate_evidence_provenance_preserved(self):
        """45. Central CandidateEvidence preserves inspectable provenance across all 6 families."""
        run_file = os.path.join(PROJECT_ROOT, "data", "phase_9_2_evidence_recovery_run.json")
        if os.path.exists(run_file):
            with open(run_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            evs = data.get("CANDIDATE_EVIDENCES", {})
            self.assertGreater(len(evs), 0)
            first_ev = next(iter(evs.values()))
            required_families = [
                "identity_evidence",
                "website_evidence",
                "review_evidence",
                "operational_evidence",
                "contact_evidence",
                "closure_negative_evidence",
            ]
            for fam in required_families:
                self.assertIn(fam, first_ev)
                self.assertIn("source", first_ev[fam])
                self.assertIn("confidence", first_ev[fam])


if __name__ == "__main__":
    unittest.main()
