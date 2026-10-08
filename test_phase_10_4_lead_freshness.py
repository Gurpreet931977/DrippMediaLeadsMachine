"""
test_phase_10_4_lead_freshness.py
=================================
Comprehensive Test Suite for Phase 10.4 — Automatic Lead Freshness & Canonical Refresh.

Covers:
  1. Fresh lead skipped
  2. Stale lead queued
  3. Due lead refreshed
  4. Website changed without identity change
  5. Phone changed without identity change
  6. Social changed without identity change
  7. Review count changed
  8. Rating changed
  9. Review date changed
  10. Provider unavailable (evidence preserved)
  11. Provider timeout (evidence preserved)
  12. Quota exhausted (job safe pause)
  13. Identity mismatch handled
  14. Evidence conflict handled
  15. Unchanged lead handled
  16. Canonical lead_id strictly preserved
  17. Duplicate prevention via BusinessIdentityMatcher
  18. SENT lead historical state protected
  19. BOUNCED lead historical state protected
  20. SUPPRESSED lead historical state protected
  21. Historical message history preserved
  22. Failed refresh preserves previous valid evidence
  23. Rule B frozen invariants (>=50, >=4.0★, <=180d)
  24. Contactability recalculated correctly
  25. Refresh priority ordering (Tier A -> E)
  26. Batch ceiling enforced (MAX_REFRESH_LEADS_PER_RUN)
  27. Idempotent rerun
  28. Interrupted refresh recovery / resumable state
  29. Google Sheets row update by lead_id (no duplicate row append)
  30. Monitoring incident generation (IncidentManager integration)
  31. Synthetic multi-lead refresh simulation
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from lib.system.freshness_models import (
    FreshnessDimension,
    FreshnessStatus,
    RefreshPriorityTier,
    RefreshFailureReason,
    DEFAULT_REFRESH_CADENCE_DAYS,
    PROTECTED_HISTORICAL_STATES,
    DimensionFreshness,
    ChangeHistoryEntry,
    LeadFreshnessSummary,
)
from lib.system.canonical_freshness import CanonicalFreshnessEngine
from lib.system.canonical_refresh_engine import CanonicalRefreshEngine
from lib.system.quota_governor import QuotaGovernor
from lib.monitoring.incident_manager import IncidentManager
from lib.monitoring.incident_types import IncidentType, IncidentSeverity
from lib.qualification.lead_scoring import LeadScorer


class TestPhase104LeadFreshness(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.now = datetime.now(timezone.utc)
        self.now_iso = self.now.isoformat()
        self.freshness_evaluator = CanonicalFreshnessEngine()
        self.quota_gov = QuotaGovernor(lock_dir=self.test_dir)
        self.incidents_file = os.path.join(self.test_dir, "incidents.json")
        self.incident_mgr = IncidentManager(incidents_file=self.incidents_file, data_dir=self.test_dir)
        self.engine = CanonicalRefreshEngine(
            data_dir=self.test_dir,
            quota_governor=self.quota_gov,
            incident_mgr=self.incident_mgr,
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # 1. Fresh lead skipped
    def test_01_fresh_lead_skipped(self):
        """Lead with recent timestamps across all dimensions is classified FRESH and skipped from default queue."""
        recent = (self.now - timedelta(days=2)).isoformat()
        lead = {
            "lead_id": "LEAD-MAN-001",
            "company_name": "Fresh Cafe",
            "website": "https://freshcafe.co.uk",
            "website_checked_at": recent,
            "operational_status": "ACTIVE_CONFIRMED",
            "operational_verified_at": recent,
            "phone": "+44 161 111 2222",
            "phone_verified_at": recent,
            "instagram_url": "https://instagram.com/freshcafe",
            "social_checked_at": recent,
            "review_count": 120,
            "rating": 4.6,
            "latest_review_date": (self.now - timedelta(days=5)).strftime("%Y-%m-%d"),
            "review_evidence_updated_at": recent,
            "contact_verified_at": recent,
            "qualification_state": "OUTREACH_READY",
        }
        summary = self.freshness_evaluator.evaluate_lead(lead, reference_time=self.now)
        self.assertEqual(summary.freshness_status, FreshnessStatus.FRESH.value)
        self.assertEqual(len(summary.due_dimensions), 0)

        # Fresh lead is excluded from default refresh queue
        queue = self.freshness_evaluator.build_refresh_queue([lead], reference_time=self.now)
        self.assertEqual(len(queue), 0)

    # 2. Stale lead queued
    def test_02_stale_lead_queued(self):
        """Lead with checks older than 2x cadence is marked STALE and queued."""
        stale_date = (self.now - timedelta(days=70)).isoformat()
        lead = {
            "lead_id": "LEAD-MAN-002",
            "company_name": "Old Tavern",
            "website_checked_at": stale_date,  # Website cadence is 30d -> 70d is STALE
            "qualification_state": "OUTREACH_READY",
        }
        summary = self.freshness_evaluator.evaluate_lead(lead, reference_time=self.now)
        self.assertEqual(summary.freshness_status, FreshnessStatus.STALE.value)
        self.assertIn("WEBSITE", summary.stale_dimensions)

        queue = self.freshness_evaluator.build_refresh_queue([lead], reference_time=self.now)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].lead_id, "LEAD-MAN-002")

    # 3. Due lead refreshed
    def test_03_due_lead_refreshed(self):
        """Lead due for review refresh is refreshed with updated timestamps."""
        due_date = (self.now - timedelta(days=16)).isoformat()  # Review cadence is 14d -> 16d is DUE
        lead = {
            "lead_id": "LEAD-MAN-003",
            "company_name": "Due Diner",
            "review_count": 85,
            "rating": 4.2,
            "review_evidence_updated_at": due_date,
            "qualification_state": "OUTREACH_READY",
        }
        summary = self.freshness_evaluator.evaluate_lead(lead, reference_time=self.now)
        self.assertTrue(summary.dimensions["REVIEW"].is_due)

        updated, changes, req, state = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["REVIEW"],
            reference_time=self.now,
        )
        self.assertIsNotNone(updated.get("review_evidence_updated_at"))
        self.assertEqual(updated["lead_id"], "LEAD-MAN-003")

    # 4. Website changed without identity change
    def test_04_website_changed_without_identity_change(self):
        """Website URL modification updates canonical record and records change history without altering lead_id."""
        lead = {
            "lead_id": "LEAD-MAN-004",
            "company_name": "Pasta Bar",
            "website": "https://oldpastabar.co.uk",
            "website_status": "FUNCTIONAL",
            "qualification_state": "OUTREACH_READY",
        }
        sim = {
            "website_override": {
                "website": "https://newpastabar.com",
                "website_status": "FUNCTIONAL",
                "http_status": 200,
            }
        }
        updated, changes, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["WEBSITE"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertEqual(updated["lead_id"], "LEAD-MAN-004")
        self.assertEqual(updated["website"], "https://newpastabar.com")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].field_name, "website")
        self.assertEqual(changes[0].old_value, "https://oldpastabar.co.uk")
        self.assertEqual(changes[0].new_value, "https://newpastabar.com")

    # 5. Phone changed without identity change
    def test_05_phone_changed_without_identity_change(self):
        """Phone modification updates canonical lead, preserves prior phone in audit history, and preserves lead_id."""
        lead = {
            "lead_id": "LEAD-MAN-005",
            "company_name": "City Coffee",
            "phone": "+44 161 222 3333",
            "qualification_state": "OUTREACH_READY",
        }
        sim = {
            "phone_override": {
                "phone": "+44 161 999 8888",
                "phone_status": "VERIFIED",
            }
        }
        updated, changes, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["PHONE"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertEqual(updated["lead_id"], "LEAD-MAN-005")
        self.assertEqual(updated["phone"], "+44 161 999 8888")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].field_name, "phone")
        # Prior phone preserved in contact audit
        audit = updated.get("contact_audit_history", [])
        self.assertTrue(any(a.get("old_value") == "+44 161 222 3333" for a in audit))

    # 6. Social changed without identity change
    def test_06_social_changed_without_identity_change(self):
        """Social handle/profile update records change history and keeps canonical lead_id."""
        lead = {
            "lead_id": "LEAD-MAN-006",
            "company_name": "Urban Bakery",
            "instagram_url": "https://instagram.com/urban_bakery_old",
            "qualification_state": "OUTREACH_READY",
        }
        sim = {
            "social_override": {
                "instagram_url": "https://instagram.com/urban_bakery_mcr",
                "social_ownership_status": "VERIFIED",
            }
        }
        updated, changes, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["SOCIAL"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertEqual(updated["lead_id"], "LEAD-MAN-006")
        self.assertEqual(updated["instagram_url"], "https://instagram.com/urban_bakery_mcr")
        self.assertTrue(any(c.field_name == "instagram_url" for c in changes))

    # 7. Review count changed
    def test_07_review_count_changed(self):
        """Review count growth records change history while preserving lead_id."""
        lead = {
            "lead_id": "LEAD-MAN-007",
            "company_name": "Corner Bistro",
            "review_count": 60,
            "rating": 4.5,
            "qualification_state": "OUTREACH_READY",
        }
        sim = {
            "review_override": {
                "review_count": 95,
                "rating": 4.5,
                "review_source": "Tavily",
            }
        }
        updated, changes, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["REVIEW"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertEqual(updated["review_count"], 95)
        self.assertTrue(any(c.field_name == "review_count" and c.new_value == 95 for c in changes))

    # 8. Rating changed
    def test_08_rating_changed(self):
        """Star rating update records change history and triggers Rule B re-evaluation."""
        lead = {
            "lead_id": "LEAD-MAN-008",
            "company_name": "Riverside Grill",
            "review_count": 120,
            "rating": 4.2,
            "qualification_state": "OUTREACH_READY",
        }
        sim = {
            "review_override": {
                "review_count": 120,
                "rating": 4.6,
                "review_source": "Tavily",
            }
        }
        updated, changes, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["REVIEW"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertEqual(updated["rating"], 4.6)
        self.assertTrue(any(c.field_name == "rating" and c.new_value == 4.6 for c in changes))

    # 9. Review date changed
    def test_09_review_date_changed(self):
        """Latest review publication date updates recency provenance."""
        lead = {
            "lead_id": "LEAD-MAN-009",
            "company_name": "Anchor Pub",
            "latest_review_date": "2026-06-01",
            "qualification_state": "OUTREACH_READY",
        }
        sim = {
            "review_override": {
                "latest_review_date": "2026-09-25",
                "review_source": "Tavily",
            }
        }
        updated, changes, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["REVIEW"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertEqual(updated["latest_review_date"], "2026-09-25")
        self.assertTrue(any(c.field_name == "latest_review_date" for c in changes))

    # 10. Provider unavailable (evidence preserved)
    def test_10_provider_unavailable_evidence_preserved(self):
        """CRITICAL: If research provider is unavailable, prior valid review count & rating are preserved intact."""
        lead = {
            "lead_id": "LEAD-MAN-010",
            "company_name": "Crown Hotel",
            "review_count": 250,
            "rating": 4.4,
            "latest_review_date": "2026-08-10",
            "qualification_state": "OUTREACH_READY",
        }
        sim = {
            "review_override": {
                "failure_state": RefreshFailureReason.PROVIDER_UNAVAILABLE.value,
            }
        }
        updated, changes, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["REVIEW"],
            simulated_data=sim,
            reference_time=self.now,
        )
        # Prior evidence preserved!
        self.assertEqual(updated["review_count"], 250)
        self.assertEqual(updated["rating"], 4.4)
        self.assertEqual(updated["latest_review_date"], "2026-08-10")
        self.assertEqual(updated["review_failure_state"], RefreshFailureReason.PROVIDER_UNAVAILABLE.value)
        # No erroneous changes logged
        self.assertEqual(len(changes), 0)

    # 11. Provider timeout (evidence preserved)
    def test_11_provider_timeout_evidence_preserved(self):
        """Provider timeout preserves existing factual data and records timeout failure state."""
        lead = {
            "lead_id": "LEAD-MAN-011",
            "company_name": "Spice Lounge",
            "review_count": 88,
            "rating": 4.1,
            "qualification_state": "OUTREACH_READY",
        }
        sim = {
            "review_override": {
                "failure_state": RefreshFailureReason.PROVIDER_TIMEOUT.value,
            }
        }
        updated, changes, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["REVIEW"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertEqual(updated["review_count"], 88)
        self.assertEqual(updated["rating"], 4.1)
        self.assertEqual(updated["review_failure_state"], RefreshFailureReason.PROVIDER_TIMEOUT.value)

    # 12. Quota exhausted (job safe pause)
    def test_12_quota_exhausted_safe_pause(self):
        """Exhausting central quota safely stops batch execution without corrupting data."""
        lead = {"lead_id": "LEAD-MAN-012", "company_name": "Quota Test", "review_count": 70, "rating": 4.2}
        leads_cache = os.path.join(self.test_dir, "cache_sheets_leads.json")
        with open(leads_cache, "w") as f:
            import json
            json.dump({"leads": [lead]}, f)

        # Set quota usage to exceed limit
        from lib.system.atomic_writer import atomic_write_json
        state = self.quota_gov._load_state()
        state.setdefault("usage", {})["enrichment_calls"] = 1000
        atomic_write_json(self.quota_gov.state_file, state)

        res = self.engine.execute_batch_refresh(
            candidate_limit=10,
            dry_run=True,
            reference_time=self.now,
        )
        self.assertEqual(res["metrics"]["quota_blocked"], 1)

    # 13. Identity mismatch handled
    def test_13_identity_mismatch_handled(self):
        """Identity mismatch in research flags review_failure_state without altering lead."""
        lead = {
            "lead_id": "LEAD-MAN-013",
            "company_name": "Red Lion",
            "review_count": 150,
            "rating": 4.3,
            "qualification_state": "OUTREACH_READY",
        }
        sim = {
            "review_override": {
                "failure_state": RefreshFailureReason.IDENTITY_MISMATCH.value,
            }
        }
        updated, _, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["REVIEW"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertEqual(updated["review_count"], 150)
        self.assertEqual(updated["review_failure_state"], RefreshFailureReason.IDENTITY_MISMATCH.value)

    # 14. Evidence conflict handled
    def test_14_evidence_conflict_handled(self):
        """Evidence conflict flag preserves prior evidence and notes conflict state."""
        lead = {
            "lead_id": "LEAD-MAN-014",
            "company_name": "Blue Bell",
            "review_count": 90,
            "rating": 4.0,
        }
        sim = {
            "review_override": {
                "failure_state": RefreshFailureReason.EVIDENCE_CONFLICT.value,
            }
        }
        updated, _, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["REVIEW"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertEqual(updated["review_count"], 90)
        self.assertEqual(updated["review_failure_state"], RefreshFailureReason.EVIDENCE_CONFLICT.value)

    # 15. Unchanged lead handled
    def test_15_unchanged_lead_handled(self):
        """Re-checking an unchanged lead updates timestamps but produces zero change history entries."""
        lead = {
            "lead_id": "LEAD-MAN-015",
            "company_name": "Stable Pub",
            "website": "https://stablepub.co.uk",
            "website_status": "FUNCTIONAL",
            "review_count": 110,
            "rating": 4.5,
            "latest_review_date": "2026-09-01",
            "qualification_state": "OUTREACH_READY",
        }
        sim = {
            "website_override": {"website": "https://stablepub.co.uk", "website_status": "FUNCTIONAL"},
            "review_override": {"review_count": 110, "rating": 4.5, "latest_review_date": "2026-09-01"},
        }
        updated, changes, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["WEBSITE", "REVIEW"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertEqual(len(changes), 0)
        self.assertEqual(updated["lead_id"], "LEAD-MAN-015")

    # 16. Canonical lead_id strictly preserved
    def test_16_canonical_id_preserved(self):
        """Canonical lead_id is strictly immutable across any refresh operations."""
        lead = {
            "lead_id": "LEAD-MAN-016",
            "company_name": "Test Place",
            "phone": "+44 161 000 0000",
            "website": "https://site1.com",
            "review_count": 100,
        }
        sim = {
            "website_override": {"website": "https://site2.com"},
            "phone_override": {"phone": "+44 161 999 9999"},
            "review_override": {"review_count": 150},
        }
        updated, _, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["WEBSITE", "PHONE", "REVIEW"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertEqual(updated["lead_id"], "LEAD-MAN-016")

    # 17. Duplicate prevention via BusinessIdentityMatcher
    def test_17_duplicate_prevention_entity_resolution(self):
        """Refreshing a lead does not create duplicates or merge different branches."""
        lead_branch_1 = {
            "lead_id": "LEAD-MAN-017A",
            "company_name": "Gaucho",
            "city": "Manchester",
            "address": "2A St Mary's St, Manchester M3 2LB",
            "review_count": 500,
        }
        lead_branch_2 = {
            "lead_id": "LEAD-MAN-017B",
            "company_name": "Gaucho",
            "city": "Manchester",
            "address": "100 Deansgate, Manchester M3 2QG",
            "review_count": 300,
        }
        pool = [lead_branch_1, lead_branch_2]
        updated, _, _, _ = self.engine.refresh_lead(
            lead=lead_branch_1,
            existing_leads_pool=pool,
            dimensions_to_refresh=["REVIEW"],
            simulated_data={"review_override": {"review_count": 520}},
            reference_time=self.now,
        )
        self.assertEqual(updated["lead_id"], "LEAD-MAN-017A")
        self.assertNotIn("possible_duplicate_of", updated)

    # 18. SENT lead historical state protected
    def test_18_sent_lead_protected(self):
        """CRITICAL: A lead with outreach_status='SENT' preserves its outreach status and send history."""
        sent_lead = {
            "lead_id": "LEAD-MAN-018",
            "company_name": "Seoul Kimchi",
            "outreach_status": "SENT",
            "outreach_sent_at": "2026-09-20 12:00:00",
            "outreach_message": "Hello Seoul Kimchi...",
            "review_count": 400,
            "rating": 4.5,
            "qualification_state": "OUTREACH_READY",
        }
        updated, _, _, _ = self.engine.refresh_lead(
            lead=sent_lead,
            dimensions_to_refresh=["REVIEW"],
            simulated_data={"review_override": {"review_count": 410}},
            reference_time=self.now,
        )
        self.assertEqual(updated["outreach_status"], "SENT")
        self.assertEqual(updated["outreach_sent_at"], "2026-09-20 12:00:00")
        self.assertEqual(updated["outreach_message"], "Hello Seoul Kimchi...")

    # 19. BOUNCED lead historical state protected
    def test_19_bounced_lead_protected(self):
        """A lead with outreach_status='BOUNCED' preserves its bounced status and is never reset."""
        bounced_lead = {
            "lead_id": "LEAD-MAN-019",
            "company_name": "Bounced Bistro",
            "outreach_status": "BOUNCED",
            "bounce_reason": "Mailbox unavailable (550)",
            "review_count": 80,
            "rating": 4.1,
        }
        updated, _, _, _ = self.engine.refresh_lead(
            lead=bounced_lead,
            dimensions_to_refresh=["REVIEW"],
            simulated_data={"review_override": {"review_count": 85}},
            reference_time=self.now,
        )
        self.assertEqual(updated["outreach_status"], "BOUNCED")
        self.assertEqual(updated.get("bounce_reason"), "Mailbox unavailable (550)")

    # 20. SUPPRESSED lead historical state protected
    def test_20_suppressed_lead_protected(self):
        """A lead with outreach_status='SUPPRESSED' maintains suppression across refresh."""
        supp_lead = {
            "lead_id": "LEAD-MAN-020",
            "company_name": "Suppressed Cafe",
            "outreach_status": "SUPPRESSED",
            "email_suppressed": True,
            "email_suppression_reason": "Unsubscribed by owner",
            "review_count": 120,
        }
        updated, _, _, _ = self.engine.refresh_lead(
            lead=supp_lead,
            dimensions_to_refresh=["REVIEW"],
            simulated_data={"review_override": {"review_count": 130}},
            reference_time=self.now,
        )
        self.assertEqual(updated["outreach_status"], "SUPPRESSED")
        self.assertTrue(updated["email_suppressed"])

    # 21. Historical message history preserved
    def test_21_historical_message_history_preserved(self):
        """Historical message and contact audit histories are preserved intact."""
        lead = {
            "lead_id": "LEAD-MAN-021",
            "company_name": "History Tavern",
            "contact_audit_history": [{"channel": "EMAIL", "action": "VERIFIED", "timestamp": "2026-08-01"}],
            "qualification_state": "OUTREACH_READY",
        }
        updated, _, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["WEBSITE"],
            simulated_data={"website_override": {"website": "https://newsite.com"}},
            reference_time=self.now,
        )
        self.assertEqual(len(updated["contact_audit_history"]), 1)
        self.assertEqual(updated["contact_audit_history"][0]["channel"], "EMAIL")

    # 22. Failed refresh preserves previous valid evidence
    def test_22_failed_refresh_preserves_previous_valid_evidence(self):
        """Operational provider failure preserves existing operational evidence."""
        lead = {
            "lead_id": "LEAD-MAN-022",
            "company_name": "Safe Tavern",
            "operational_status": "ACTIVE_CONFIRMED",
            "operational_confidence": "HIGH",
        }
        sim = {
            "operational_override": {
                "failure_state": RefreshFailureReason.PROVIDER_FAILED.value,
            }
        }
        updated, _, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["OPERATIONAL"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertEqual(updated["operational_status"], "ACTIVE_CONFIRMED")
        self.assertEqual(updated["operational_confidence"], "HIGH")
        self.assertEqual(updated["operational_failure_state"], RefreshFailureReason.PROVIDER_FAILED.value)

    # 23. Rule B unchanged
    def test_23_rule_b_unchanged(self):
        """Rule B thresholds (>=50 reviews, >=4.0★) strictly govern requalification."""
        # Lead whose reviews drop below 50 becomes RESEARCH_ONLY
        lead = {
            "lead_id": "LEAD-MAN-023",
            "company_name": "Borderline Bar",
            "review_count": 55,
            "rating": 4.2,
            "website_status": "NO_WEBSITE_CONFIRMED",
            "operational_status": "ACTIVE_CONFIRMED",
            "qualification_state": "OUTREACH_READY",
        }
        sim = {
            "review_override": {
                "review_count": 42,  # < 50 Rule B violation!
                "rating": 4.2,
            }
        }
        updated, _, req, new_state = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["REVIEW"],
            simulated_data=sim,
            reference_time=self.now,
        )
        self.assertTrue(req)
        self.assertIn(new_state, ["MANUAL_REVIEW", "RESEARCH_ONLY"])
        self.assertNotEqual(new_state, "OUTREACH_READY")

    # 24. Contactability recalculated correctly
    def test_24_contactability_recalculated(self):
        """Contactability checked timestamp and status are refreshed."""
        lead = {
            "lead_id": "LEAD-MAN-024",
            "company_name": "Contact Cafe",
            "email": "info@contactcafe.co.uk",
            "phone": "+44 161 777 8888",
        }
        updated, _, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["CONTACTABILITY"],
            reference_time=self.now,
        )
        self.assertEqual(updated["contact_verified_at"], self.now_iso)

    # 25. Refresh priority ordering
    def test_25_refresh_priority_ordering(self):
        """Tier A (OUTREACH_READY) leads are queued before Tier D (RESEARCH_ONLY) or Tier E."""
        due_time = (self.now - timedelta(days=20)).isoformat()
        lead_ready = {
            "lead_id": "LEAD-MAN-025A",
            "company_name": "Tier A Lead",
            "qualification_state": "OUTREACH_READY",
            "review_evidence_updated_at": due_time,
        }
        lead_research = {
            "lead_id": "LEAD-MAN-025B",
            "company_name": "Tier D Lead",
            "qualification_state": "RESEARCH_ONLY",
            "review_evidence_updated_at": due_time,
        }
        queue = self.freshness_evaluator.build_refresh_queue(
            [lead_research, lead_ready],
            reference_time=self.now,
        )
        self.assertEqual(len(queue), 2)
        # Tier A must be first in queue
        self.assertEqual(queue[0].lead_id, "LEAD-MAN-025A")
        self.assertEqual(queue[0].priority_tier, RefreshPriorityTier.TIER_A.value)
        self.assertEqual(queue[1].lead_id, "LEAD-MAN-025B")

    # 26. Batch ceiling enforced
    def test_26_batch_ceiling_enforced(self):
        """Queue builder strictly respects max_limit batch ceiling."""
        due_time = (self.now - timedelta(days=25)).isoformat()
        leads = [
            {"lead_id": f"LEAD-MAN-BATCH-{i:02d}", "review_evidence_updated_at": due_time, "qualification_state": "OUTREACH_READY"}
            for i in range(10)
        ]
        queue = self.freshness_evaluator.build_refresh_queue(leads, max_limit=4, reference_time=self.now)
        self.assertEqual(len(queue), 4)

    # 27. Idempotent rerun
    def test_27_idempotent_rerun(self):
        """Running refresh twice consecutively on freshly updated lead skips it on second run."""
        recent = self.now_iso
        lead = {
            "lead_id": "LEAD-MAN-027",
            "company_name": "Idempotent Inn",
            "website_checked_at": recent,
            "operational_verified_at": recent,
            "phone_verified_at": recent,
            "social_checked_at": recent,
            "contact_verified_at": recent,
            "review_count": 80,
            "rating": 4.3,
            "review_evidence_updated_at": (self.now - timedelta(days=20)).isoformat(),
            "qualification_state": "OUTREACH_READY",
        }
        # First run: refreshes lead
        updated, _, _, _ = self.engine.refresh_lead(
            lead=lead,
            dimensions_to_refresh=["REVIEW"],
            simulated_data={"review_override": {"review_count": 82}},
            reference_time=self.now,
        )
        # Second evaluation: lead is now fresh across all dimensions!
        summary = self.freshness_evaluator.evaluate_lead(updated, reference_time=self.now)
        self.assertFalse(summary.dimensions["REVIEW"].is_due)
        self.assertEqual(len(summary.due_dimensions), 0)
        # Excluded from queue on rerun
        queue = self.freshness_evaluator.build_refresh_queue([updated], reference_time=self.now)
        self.assertEqual(len(queue), 0)

    # 28. Interrupted refresh recovery
    def test_28_interrupted_refresh_recovery(self):
        """Interrupted batch run leaves unaffected leads intact for clean resumption."""
        lead1 = {"lead_id": "LEAD-MAN-028A", "review_count": 70, "rating": 4.1}
        lead2 = {"lead_id": "LEAD-MAN-028B", "review_count": 90, "rating": 4.4}
        leads_cache = os.path.join(self.test_dir, "cache_sheets_leads.json")
        with open(leads_cache, "w") as f:
            import json
            json.dump({"leads": [lead1, lead2]}, f)

        # Batch limit = 1 processes first lead without touching second
        res = self.engine.execute_batch_refresh(candidate_limit=1, dry_run=False, reference_time=self.now)
        self.assertEqual(res["metrics"]["leads_refreshed"], 1)

    # 29. Google Sheets row update by lead_id
    def test_29_google_sheets_update_by_lead_id(self):
        """Simulated Sheets synchronization updates row in place by lead_id rather than appending duplicate."""
        from lib.crm.identity_matcher import BusinessIdentityMatcher, IdentityMatchOutcome
        matcher = BusinessIdentityMatcher()
        existing_sheet_rows = [
            {"lead_id": "LEAD-MAN-029", "company_name": "The Old Monkey", "review_count": 730, "rating": 4.5}
        ]
        refreshed_lead = {
            "lead_id": "LEAD-MAN-029",
            "company_name": "The Old Monkey",
            "review_count": 745,
            "rating": 4.5,
        }
        match_res = matcher.match_candidate(refreshed_lead, existing_sheet_rows)
        self.assertEqual(match_res.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertEqual(match_res.matched_lead_id, "LEAD-MAN-029")

    # 30. Monitoring incident generation
    def test_30_monitoring_incident_generation(self):
        """High failure rate or job failure triggers IncidentManager."""
        self.engine.freshness_monitor.record_job_failure(
            Exception("Simulated network outage"), context="test_run"
        )
        incidents = self.incident_mgr.list_incidents()
        self.assertTrue(any(i.incident_type == IncidentType.REFRESH_JOB_FAILED.value for i in incidents))


    # 31. Synthetic multi-lead refresh simulation
    def test_31_synthetic_multi_lead_simulation(self):
        """Comprehensive synthetic test covering diverse lead categories simultaneously."""
        old_time = (self.now - timedelta(days=25)).isoformat()
        dataset = [
            # 1. Fresh lead
            {"lead_id": "LEAD-SIM-01", "company_name": "L1", "review_evidence_updated_at": self.now_iso, "qualification_state": "OUTREACH_READY"},
            # 2. Stale lead
            {"lead_id": "LEAD-SIM-02", "company_name": "L2", "review_evidence_updated_at": old_time, "qualification_state": "OUTREACH_READY", "review_count": 80, "rating": 4.2},
            # 3. SENT lead
            {"lead_id": "LEAD-SIM-03", "company_name": "L3", "outreach_status": "SENT", "review_evidence_updated_at": old_time, "review_count": 150, "rating": 4.5},
            # 4. BOUNCED lead
            {"lead_id": "LEAD-SIM-04", "company_name": "L4", "outreach_status": "BOUNCED", "review_evidence_updated_at": old_time, "review_count": 60, "rating": 4.0},
            # 5. SUPPRESSED lead
            {"lead_id": "LEAD-SIM-05", "company_name": "L5", "outreach_status": "SUPPRESSED", "email_suppressed": True, "review_evidence_updated_at": old_time, "review_count": 90, "rating": 4.3},
            # 6. Provider failure lead
            {"lead_id": "LEAD-SIM-06", "company_name": "L6", "review_evidence_updated_at": old_time, "review_count": 120, "rating": 4.4, "qualification_state": "OUTREACH_READY"},
        ]
        leads_cache = os.path.join(self.test_dir, "cache_sheets_leads.json")
        with open(leads_cache, "w") as f:
            import json
            json.dump({"leads": dataset}, f)

        sim_map = {
            "LEAD-SIM-06": {"review_override": {"failure_state": RefreshFailureReason.PROVIDER_UNAVAILABLE.value}}
        }

        res = self.engine.execute_batch_refresh(
            candidate_limit=10,
            dry_run=True,
            simulation_map=sim_map,
            reference_time=self.now,
        )
        self.assertEqual(res["status"], "COMPLETED")
        self.assertGreaterEqual(res["metrics"]["leads_refreshed"], 4)


if __name__ == "__main__":
    unittest.main()
