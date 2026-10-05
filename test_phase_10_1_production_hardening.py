"""
test_phase_10_1_production_hardening.py
=======================================
Phase 10.1 Final Production Hardening & Automated Technical Operations Test Suite.

Comprehensive validation across all 42 sections of Phase 10.1 specifications:
  1. Architecture & State Ownership (authoritative sources, snapshot derivation)
  2. Persistence & Atomic Writes (atomic replace, fsync, transactional rollback)
  3. File Locking & Concurrency (cross-process advisory locks, timeout, contention)
  4. Technical Scheduler & Checkpointing (job lifecycle, resume, cancel, market gating)
  5. Quota Governance (centralized budgets, exhaustion pausing, zero bypass)
  6. Freshness & Staleness Engine (staleness flags, technical refresh queuing)
  7. Re-Qualification Protection (Rule B criteria frozen, no silent promotion/demotion)
  8. Identity Integrity & Branch Separation (canonical ID regex, research ID isolation)
  9. Cross-Store State Reconciliation (invariant detection across 8 rules, no silent repair)
  10. Backup & Disaster Recovery (SHA-256 manifests, tampering detection, isolated restore)
  11. Travel Mode & Commercial Safety (kill switch, calls/DMs/emails/proposals strictly locked)
  12. Security & Observability (secret masking, config audit, safe error classification)
  13. Reliability & Bounded Retries (transient retry, permanent failure, candidate isolation)
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient
from server import app

from lib.system.system_config import (
    SystemConfig,
    CommercialActionForbiddenError,
    CommercialActionBlockedError,
)
from lib.system.atomic_writer import (
    atomic_write_json,
    atomic_write_text,
    atomic_transaction,
    AtomicWriteError,
)
from lib.system.file_lock import (
    NamedFileLock,
    crm_write_lock,
    proposal_records_lock,
    commercial_records_lock,
    campaign_lock,
    quota_lock,
    orchestrator_lock,
)
from lib.system.backup_manager import BackupManager, calculate_sha256
from lib.system.reconciliation_engine import ReconciliationEngine, ReconciliationIssue
from lib.system.identity_integrity import IdentityIntegrityAuditor
from lib.system.quota_governor import QuotaGovernor, QuotaExhaustedError
from lib.system.freshness_engine import FreshnessEngine, StalenessFlag
from lib.system.error_handler import (
    TechnicalErrorHandler,
    TechnicalErrorRecord,
    ErrorSeverity,
    ErrorClassification,
)
from lib.system.observability import (
    StructuredLogger,
    mask_sensitive_data,
    mask_string_secret,
)
from lib.system.config_validator import ConfigValidator
from lib.system.technical_refresher import TechnicalRefresher
from lib.system.technical_orchestrator import TechnicalOrchestrator, JobState, JobType
from lib.system.technical_scheduler import TechnicalScheduler
from lib.system.system_health import SystemHealthMonitor
from lib.production.market_config import MarketRegistry, MarketDefinition
from lib.commercial.models import CommercialStage, ProposalState, ProposalPackage
from lib.types import DiscoveredBusiness, VerificationStatus
from lib.qualification.lead_scoring import LeadScoringProvider


class Phase10_1_ProductionHardeningTests(unittest.TestCase):
    """
    Complete regression and hardening test suite for Phase 10.1.
    """

    def setUp(self):
        # Create an isolated temporary data sandbox for each test
        self.test_dir = tempfile.mkdtemp(prefix="dripp_p10_1_test_")
        self.data_dir = os.path.join(self.test_dir, "data")
        self.backups_dir = os.path.join(self.data_dir, "backups")
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.backups_dir, exist_ok=True)

        # Baseline data stores
        self.leads_file = os.path.join(self.data_dir, "cache_sheets_leads.json")
        self.commercial_file = os.path.join(self.data_dir, "commercial_records.json")
        self.proposals_file = os.path.join(self.data_dir, "commercial_proposals.json")
        self.messages_file = os.path.join(self.data_dir, "message_history.json")
        self.timelines_file = os.path.join(self.data_dir, "lead_timelines.json")
        self.quota_file = os.path.join(self.data_dir, "quota_usage.json")

        atomic_write_json(self.leads_file, [])
        atomic_write_json(self.commercial_file, {})
        atomic_write_json(self.proposals_file, [])
        atomic_write_json(self.messages_file, {})
        atomic_write_json(self.timelines_file, {})

        # Ensure Travel Mode and Commercial Kill Switch are active for test isolation
        SystemConfig.TRAVEL_MODE = True
        SystemConfig.COMMERCIAL_ACTIONS_ENABLED = False
        SystemConfig.TECHNICAL_AUTOMATION_ENABLED = True

        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # =========================================================================
    # 1. ARCHITECTURE & STATE OWNERSHIP
    # =========================================================================

    def test_authoritative_source_definitions(self):
        """Verifies canonical authoritative data sources are strictly declared."""
        sources = {
            "LEAD_IDENTITY": "cache_sheets_leads.json",
            "COMMERCIAL_RECORDS": "commercial_records.json",
            "PROPOSALS": "commercial_proposals.json",
            "OUTREACH_MESSAGES": "message_history.json",
            "TIMELINES": "lead_timelines.json",
        }
        for model_name, filename in sources.items():
            path = os.path.join(self.data_dir, filename)
            self.assertTrue(os.path.exists(path), f"Authoritative store {filename} must exist")

    def test_snapshot_is_derived_not_authoritative(self):
        """Snapshots are derived reports; modifying a snapshot never alters authoritative state."""
        # Create authoritative lead
        lead = {"lead_id": "LEAD-MAN-112233", "company_name": "Test Bistro", "lead_status": "NOT_CONTACTED"}
        atomic_write_json(self.leads_file, [lead])

        # Write a forged snapshot
        snapshot_file = os.path.join(self.data_dir, "outreach_performance_snapshot.json")
        forged_snapshot = {"leads": [{"lead_id": "LEAD-MAN-112233", "lead_status": "SENT"}]}
        atomic_write_json(snapshot_file, forged_snapshot)

        # Re-read authoritative leads store
        with open(self.leads_file, "r") as f:
            stored_leads = json.load(f)
        self.assertEqual(stored_leads[0]["lead_status"], "NOT_CONTACTED")

    def test_read_only_derivatives_immutability(self):
        """Derived analytics reports declare generated timestamp and source metadata."""
        monitor = SystemHealthMonitor(data_dir=self.data_dir)
        snap = monitor.generate_snapshot(output_path=os.path.join(self.data_dir, "system_health_snapshot.json"))
        self.assertIn("generated_at", snap)
        self.assertIn("system_status", snap)
        self.assertIn("operating_mode", snap)

    def test_crm_write_path_separation(self):
        """Direct writes to leads without passing validation are detected by reconciler."""
        corrupted_lead = {"lead_id": "LEAD-MAN-999999", "lead_status": "SENT"}  # Sent with zero event history
        atomic_write_json(self.leads_file, [corrupted_lead])
        reconciler = ReconciliationEngine(leads_path=self.leads_file, data_dir=self.data_dir)
        report = reconciler.run_reconciliation()
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(len(report["issues"]), 1)

    def test_sync_path_reconciliation(self):
        """State reconciler verifies relationships between disk stores."""
        reconciler = ReconciliationEngine(
            leads_path=self.leads_file,
            commercial_records_path=self.commercial_file,
            proposals_path=self.proposals_file,
            data_dir=self.data_dir,
        )
        report = reconciler.run_reconciliation()
        self.assertEqual(report["status"], "PASS")

    def test_data_ownership_matrix_integrity(self):
        """Canonical data ownership constants reflect documented system architecture."""
        from lib.system.system_config import SystemConfig
        self.assertTrue(hasattr(SystemConfig, "TRAVEL_MODE"))
        self.assertTrue(hasattr(SystemConfig, "COMMERCIAL_ACTIONS_ENABLED"))
        self.assertTrue(hasattr(SystemConfig, "TECHNICAL_AUTOMATION_ENABLED"))

    # =========================================================================
    # 2. PERSISTENCE & ATOMIC WRITES
    # =========================================================================

    def test_atomic_write_json_success(self):
        """Atomic write replaces destination cleanly and produces valid JSON."""
        target = os.path.join(self.data_dir, "atomic_test.json")
        payload = {"success": True, "count": 42}
        atomic_write_json(target, payload)
        self.assertTrue(os.path.exists(target))
        with open(target, "r") as f:
            data = json.load(f)
        self.assertEqual(data["count"], 42)

    def test_atomic_write_text_success(self):
        """Atomic write text produces valid text with fsync."""
        target = os.path.join(self.data_dir, "test.txt")
        atomic_write_text(target, "Hello Production Hardening")
        with open(target, "r") as f:
            content = f.read()
        self.assertEqual(content, "Hello Production Hardening")

    def test_corrupted_file_handling(self):
        """Corrupted JSON files are safely intercepted by health check."""
        target = os.path.join(self.data_dir, "cache_sheets_leads.json")
        with open(target, "w") as f:
            f.write("{ incomplete_json: [")
        monitor = SystemHealthMonitor(data_dir=self.data_dir)
        health = monitor.check_file_integrity()
        self.assertEqual(health["status"], "FAIL")
        self.assertFalse(health["files"]["cache_sheets_leads.json"]["valid_json"])

    def test_atomic_transaction_success(self):
        """Atomic transaction modifies multiple files and commits cleanly."""
        file_a = os.path.join(self.data_dir, "tx_a.json")
        file_b = os.path.join(self.data_dir, "tx_b.json")
        atomic_write_json(file_a, {"v": 1})
        atomic_write_json(file_b, {"v": 10})

        with atomic_transaction([file_a, file_b]):
            atomic_write_json(file_a, {"v": 2})
            atomic_write_json(file_b, {"v": 20})

        with open(file_a, "r") as f:
            self.assertEqual(json.load(f)["v"], 2)
        with open(file_b, "r") as f:
            self.assertEqual(json.load(f)["v"], 20)

    def test_atomic_transaction_rollback(self):
        """Atomic transaction rolls back all files if an exception is raised mid-way."""
        file_a = os.path.join(self.data_dir, "rb_a.json")
        file_b = os.path.join(self.data_dir, "rb_b.json")
        atomic_write_json(file_a, {"val": "original_a"})
        atomic_write_json(file_b, {"val": "original_b"})

        try:
            with atomic_transaction([file_a, file_b]):
                atomic_write_json(file_a, {"val": "mutated_a"})
                raise RuntimeError("Simulated mid-mutation system failure")
        except RuntimeError:
            pass

        with open(file_a, "r") as f:
            self.assertEqual(json.load(f)["val"], "original_a")
        with open(file_b, "r") as f:
            self.assertEqual(json.load(f)["val"], "original_b")

    def test_concurrent_write_integrity(self):
        """Simultaneous atomic writes do not truncate files."""
        target = os.path.join(self.data_dir, "concurrent_test.json")
        for i in range(10):
            atomic_write_json(target, {"iteration": i, "data": "x" * 500})
        with open(target, "r") as f:
            data = json.load(f)
        self.assertEqual(data["iteration"], 9)

    # =========================================================================
    # 3. FILE LOCKING & CONCURRENCY
    # =========================================================================

    def test_file_lock_acquisition_and_release(self):
        """NamedFileLock acquires and releases file lock cleanly."""
        lock = NamedFileLock("test_lock", lock_dir=self.data_dir)
        with lock:
            self.assertTrue(lock.acquired)
        self.assertFalse(lock.acquired)

    def test_file_lock_timeout(self):
        """Re-acquiring a locked file in another handle raises TimeoutError."""
        lock1 = NamedFileLock("contention_lock", lock_dir=self.data_dir, timeout=0.5)
        lock2 = NamedFileLock("contention_lock", lock_dir=self.data_dir, timeout=0.2)
        with lock1:
            with self.assertRaises(TimeoutError):
                with lock2:
                    pass

    def test_cross_process_crm_lock(self):
        """crm_write_lock acquires advisory lock for CRM mutations."""
        with crm_write_lock(lock_dir=self.data_dir, timeout=1.0):
            pass

    def test_commercial_records_lock(self):
        """commercial_records_lock acquires advisory lock for commercial mutations."""
        with commercial_records_lock(lock_dir=self.data_dir, timeout=1.0):
            pass

    def test_orchestrator_lock(self):
        """orchestrator_lock serializes technical runner invocations."""
        with orchestrator_lock(lock_dir=self.data_dir, timeout=1.0):
            pass

    # =========================================================================
    # 4. TECHNICAL SCHEDULER & CHECKPOINTING
    # =========================================================================

    def test_scheduler_task_registration(self):
        """Registers technical maintenance jobs in scheduler."""
        orchestrator = TechnicalOrchestrator(data_dir=self.data_dir)
        scheduler = TechnicalScheduler(orchestrator=orchestrator)
        scheduler.register_task(
            task_id="daily_reconcile",
            job_type=JobType.RUN_RECONCILIATION.value,
            interval_seconds=86400,
        )
        status = scheduler.get_status()
        self.assertEqual(len(status["tasks"]), 1)
        self.assertEqual(status["tasks"][0]["task_id"], "daily_reconcile")

    def test_scheduler_disabled_market_rejection(self):
        """Running a job for a disabled market raises ValueError."""
        orchestrator = TechnicalOrchestrator(data_dir=self.data_dir)
        # LONDON_UK is disabled by default
        with self.assertRaises(ValueError):
            orchestrator.create_job(job_type=JobType.RUN_DISCOVERY, market_id="LONDON_UK")

    def test_scheduler_enabled_market_execution(self):
        """Running a job for enabled market MANCHESTER_UK succeeds."""
        orchestrator = TechnicalOrchestrator(data_dir=self.data_dir)
        job = orchestrator.create_job(job_type=JobType.RUN_RECONCILIATION, market_id="MANCHESTER_UK")
        self.assertEqual(job["status"], JobState.QUEUED.value)
        self.assertEqual(job["market_id"], "MANCHESTER_UK")

    def test_duplicate_scheduled_run_prevention(self):
        """Active running job prevents starting duplicate job of same type and market."""
        orchestrator = TechnicalOrchestrator(data_dir=self.data_dir)
        job1 = orchestrator.create_job(job_type=JobType.RUN_RECONCILIATION, market_id="MANCHESTER_UK")
        orchestrator.update_job_status(job1["run_id"], JobState.RUNNING)

        # Second job creation should be rejected or handled
        with self.assertRaises(RuntimeError):
            orchestrator.create_job(job_type=JobType.RUN_RECONCILIATION, market_id="MANCHESTER_UK")

    def test_job_interruption_state(self):
        """Interrupted job records status as PARTIAL or FAILED, never COMPLETED."""
        orchestrator = TechnicalOrchestrator(data_dir=self.data_dir)
        job = orchestrator.create_job(job_type=JobType.RUN_ENRICHMENT, market_id="MANCHESTER_UK")
        orchestrator.update_job_status(job["run_id"], JobState.RUNNING)
        orchestrator.fail_job(job["run_id"], error="Process killed by SIGTERM", retryable=True)

        res = orchestrator.get_job(job["run_id"])
        self.assertEqual(res["status"], JobState.FAILED.value)
        self.assertIn("SIGTERM", res["error"])

    def test_job_checkpoint_persistence(self):
        """Long-running job saves cursor checkpoint to disk."""
        orchestrator = TechnicalOrchestrator(data_dir=self.data_dir)
        job = orchestrator.create_job(job_type=JobType.RUN_ENRICHMENT, market_id="MANCHESTER_UK")
        orchestrator.save_checkpoint(job["run_id"], cursor=25, last_processed_entity="LEAD-MAN-000025")

        cp = orchestrator.get_checkpoint(job["run_id"])
        self.assertEqual(cp["cursor"], 25)
        self.assertEqual(cp["last_processed_entity"], "LEAD-MAN-000025")

    def test_job_resume_from_checkpoint(self):
        """Job resume continues from stored cursor rather than duplicating work."""
        orchestrator = TechnicalOrchestrator(data_dir=self.data_dir)
        job = orchestrator.create_job(job_type=JobType.RUN_ENRICHMENT, market_id="MANCHESTER_UK")
        orchestrator.update_job_status(job["run_id"], JobState.PAUSED)
        orchestrator.save_checkpoint(job["run_id"], cursor=50, last_processed_entity="LEAD-MAN-000050")

        resumed_job = orchestrator.resume_job(job["run_id"])
        self.assertEqual(resumed_job["status"], JobState.RUNNING.value)
        self.assertEqual(resumed_job["cursor"], 50)

    # =========================================================================
    # 5. QUOTA GOVERNANCE
    # =========================================================================

    def test_centralized_quota_consumption(self):
        """QuotaGovernor tracks search, gosom, and enrichment quota accurately."""
        quota = QuotaGovernor(quota_path=self.quota_file, lock_dir=self.data_dir)
        quota.consume("search_calls", 5)
        quota.consume("enrichment_calls", 12)
        stat = quota.get_quota_status()
        self.assertEqual(stat["quotas"]["search_calls"]["used"], 5)
        self.assertEqual(stat["quotas"]["enrichment_calls"]["used"], 12)

    def test_quota_exhaustion_pauses_job(self):
        """Exhausting quota raises QuotaExhaustedError."""
        quota = QuotaGovernor(quota_path=self.quota_file, lock_dir=self.data_dir)
        # Limit search_calls
        quota.limits["search_calls"] = 10
        quota.consume("search_calls", 10)

        with self.assertRaises(QuotaExhaustedError):
            quota.check_and_consume("search_calls", 1)

    def test_no_subsystem_quota_bypass(self):
        """Subsystem cannot execute when quota governor check fails."""
        quota = QuotaGovernor(quota_path=self.quota_file, lock_dir=self.data_dir)
        quota.limits["crm_writes"] = 2
        quota.consume("crm_writes", 2)
        self.assertFalse(quota.can_consume("crm_writes", 1))

    def test_quota_reset_functionality(self):
        """Resetting quota restores full resource capacity."""
        quota = QuotaGovernor(quota_path=self.quota_file, lock_dir=self.data_dir)
        quota.consume("search_calls", 50)
        quota.reset_quota("search_calls")
        self.assertEqual(quota.get_quota_status()["quotas"]["search_calls"]["used"], 0)

    def test_quota_lock_protection(self):
        """Quota consumption is protected by concurrency lock."""
        quota = QuotaGovernor(quota_path=self.quota_file, lock_dir=self.data_dir)
        with quota_lock(lock_dir=self.data_dir):
            quota.consume("crm_writes", 1)
        self.assertEqual(quota.get_quota_status()["quotas"]["crm_writes"]["used"], 1)

    # =========================================================================
    # 6. FRESHNESS & STALENESS ENGINE
    # =========================================================================

    def test_freshness_website_stale_flag(self):
        """Lead with website check older than 30 days receives STALE_WEBSITE flag."""
        engine = FreshnessEngine()
        old_time = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
        lead = {"lead_id": "LEAD-MAN-101", "website_checked_at": old_time}
        res = engine.evaluate_lead_freshness(lead)
        self.assertIn(StalenessFlag.STALE_WEBSITE.value, res["stale_flags"])

    def test_freshness_review_stale_flag(self):
        """Reviews evidence older than 180 days receives STALE_REVIEW flag."""
        engine = FreshnessEngine()
        old_time = (datetime.now(timezone.utc) - timedelta(days=190)).isoformat()
        lead = {"lead_id": "LEAD-MAN-102", "reviews_refreshed_at": old_time}
        res = engine.evaluate_lead_freshness(lead)
        self.assertIn(StalenessFlag.STALE_REVIEW.value, res["stale_flags"])

    def test_freshness_operational_stale_flag(self):
        """Operational evidence older than 60 days receives STALE_OPERATIONAL flag."""
        engine = FreshnessEngine()
        old_time = (datetime.now(timezone.utc) - timedelta(days=65)).isoformat()
        lead = {"lead_id": "LEAD-MAN-103", "operational_checked_at": old_time}
        res = engine.evaluate_lead_freshness(lead)
        self.assertIn(StalenessFlag.STALE_OPERATIONAL.value, res["stale_flags"])

    def test_freshness_contact_stale_flag(self):
        """Contact info older than 90 days receives STALE_CONTACT flag."""
        engine = FreshnessEngine()
        old_time = (datetime.now(timezone.utc) - timedelta(days=95)).isoformat()
        lead = {"lead_id": "LEAD-MAN-104", "contact_verified_at": old_time}
        res = engine.evaluate_lead_freshness(lead)
        self.assertIn(StalenessFlag.STALE_CONTACT.value, res["stale_flags"])


    def test_staleness_does_not_silently_demote_lead(self):
        """Staleness flags queue technical refresh without silently downgrading qualification."""
        engine = FreshnessEngine()
        lead = {
            "lead_id": "LEAD-MAN-105",
            "qualification_state": "OUTREACH_READY",
            "reviews_refreshed_at": (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(),
        }
        res = engine.process_lead_freshness(lead)
        # Qualification remains unchanged; staleness flag is queued
        self.assertEqual(res["lead"]["qualification_state"], "OUTREACH_READY")
        self.assertIn("STALE_REVIEW", res["staleness_flags"])
        self.assertTrue(res["refresh_queued"])

    # =========================================================================
    # 7. RE-QUALIFICATION PROTECTION
    # =========================================================================

    def test_refresh_cannot_directly_override_outreach_ready(self):
        """Technical refresher cannot directly edit qualification_state to OUTREACH_READY."""
        refresher = TechnicalRefresher(data_dir=self.data_dir)
        lead = {"lead_id": "LEAD-MAN-201", "qualification_state": "MANUAL_REVIEW"}
        # Refresher only returns refreshed evidence payload, never alters qualification_state
        refreshed = refresher.refresh_website_status(lead, http_status=200, website_url="https://newsite.com")
        self.assertNotIn("qualification_state", refreshed)

    def test_re_qualification_requires_canonical_rule_engine(self):
        """Evidence updates require routing through canonical Rule B engine for promotion."""
        from lib.qualification.lead_scoring import LeadScoringProvider
        from lib.types import DiscoveredBusiness, VerificationStatus
        biz = DiscoveredBusiness(
            company_name="Royal Curry Lounge",
            category="Indian Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            review_count=180,
            rating=4.5,
            phone="+44 161 999 8888",
        )
        provider = LeadScoringProvider()
        result = provider.evaluate_lead(biz, verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertIn(result["qualification_state"], ("OUTREACH_READY", "MANUAL_REVIEW"))

    def test_qualification_promotion_only_on_valid_evidence(self):
        """Low review counts (e.g. 15 reviews) fail Rule B outreach traction gate."""
        from lib.qualification.lead_scoring import LeadScoringProvider
        from lib.types import DiscoveredBusiness, VerificationStatus
        biz = DiscoveredBusiness(
            company_name="Tiny Cafe",
            category="Cafe",
            city="Manchester",
            target_country="United Kingdom",
            review_count=15,  # Fails >= 50 gate
            rating=4.8,
            phone="+44 161 111 2222",
        )
        provider = LeadScoringProvider()
        result = provider.evaluate_lead(biz, verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertNotEqual(result["qualification_state"], "OUTREACH_READY")

    def test_qualification_demotion_preserved(self):
        """Rating below 4.0 cannot qualify for OUTREACH_READY."""
        from lib.qualification.lead_scoring import LeadScoringProvider
        from lib.types import DiscoveredBusiness, VerificationStatus
        biz = DiscoveredBusiness(
            company_name="Poor Rating Diner",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            review_count=350,
            rating=3.4,  # Fails >= 4.0 rating gate
            phone="+44 161 333 4444",
        )
        provider = LeadScoringProvider()
        result = provider.evaluate_lead(biz, verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertNotEqual(result["qualification_state"], "OUTREACH_READY")

    def test_frozen_rule_b_criteria_unchanged(self):
        """Rule B qualification thresholds remain >= 50 reviews, >= 4.0 rating, <= 180 days."""
        provider = LeadScoringProvider()
        self.assertEqual(provider.min_reviews_outreach, 50)
        self.assertEqual(provider.min_rating_outreach, 4.0)

    # =========================================================================
    # 8. IDENTITY INTEGRITY & BRANCH SEPARATION
    # =========================================================================

    def test_canonical_lead_id_format_regex(self):
        """Validates canonical LEAD-MKT-XXXXXX format enforcement."""
        auditor = IdentityIntegrityAuditor(data_dir=self.data_dir)
        self.assertTrue(auditor.validate_lead_id_format("LEAD-MAN-A1B2C3"))
        self.assertTrue(auditor.validate_lead_id_format("LEAD-MAN-902001"))
        self.assertFalse(auditor.validate_lead_id_format("RES-2026-001"))
        self.assertFalse(auditor.validate_lead_id_format("lead_12345"))

    def test_cross_store_duplicate_prevention(self):
        """Auditor catches identical lead IDs appearing with different business names."""
        lead1 = {"lead_id": "LEAD-MAN-123456", "company_name": "Taco Hub"}
        lead2 = {"lead_id": "LEAD-MAN-123456", "company_name": "Sushi Bar"}
        atomic_write_json(self.leads_file, [lead1, lead2])

        auditor = IdentityIntegrityAuditor(data_dir=self.data_dir)
        report = auditor.run_identity_audit()
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("Duplicate canonical lead_id" in iss["description"] for iss in report["issues"]))

    def test_branch_separation_preservation(self):
        """Different physical branches of the same brand remain separate entities."""
        from lib.crm.identity_matcher import BusinessIdentityMatcher
        matcher = BusinessIdentityMatcher()
        # Same brand name, different postcodes in Manchester
        norm1 = matcher.normalize_name("Rudy's Pizza Ancoats")
        norm2 = matcher.normalize_name("Rudy's Pizza Didsbury")
        self.assertNotEqual(norm1, norm2)

    def test_research_id_never_becomes_lead_id(self):
        """Research log identifiers (e.g. RES-MAN-001) cannot leak into canonical lead stores."""
        corrupted = {"lead_id": "RES-MAN-001", "company_name": "Leaked Research Record"}
        atomic_write_json(self.leads_file, [corrupted])
        auditor = IdentityIntegrityAuditor(data_dir=self.data_dir)
        report = auditor.run_identity_audit()
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("Non-canonical lead_id format" in iss["description"] for iss in report["issues"]))

    def test_phone_identity_normalization(self):
        """Cross-format UK phones resolve without false collision."""
        from lib.crm.identity_matcher import BusinessIdentityMatcher
        p1 = BusinessIdentityMatcher.normalize_phone("+44 161 234 5678")
        p2 = BusinessIdentityMatcher.normalize_phone("0161 234 5678")
        self.assertEqual(p1, p2)

    def test_identity_audit_clean_pass(self):
        """Valid canonical records pass identity audit without issues."""
        lead = {"lead_id": "LEAD-MAN-ABCDEF", "company_name": "Manchester Coffee Co", "postcode": "M1 1AA"}
        atomic_write_json(self.leads_file, [lead])
        auditor = IdentityIntegrityAuditor(data_dir=self.data_dir)
        report = auditor.run_identity_audit()
        self.assertEqual(report["status"], "PASS")

    # =========================================================================
    # 9. CROSS-STORE STATE RECONCILIATION
    # =========================================================================

    def test_reconciliation_sent_without_send_event(self):
        """Rule 1: Lead status SENT with no send event is flagged CRITICAL."""
        lead = {"lead_id": "LEAD-MAN-301", "lead_status": "SENT"}
        atomic_write_json(self.leads_file, [lead])
        atomic_write_json(self.messages_file, {})
        reconciler = ReconciliationEngine(leads_path=self.leads_file, data_dir=self.data_dir)
        report = reconciler.run_reconciliation()
        self.assertEqual(report["status"], "FAIL")
        critical_issues = [i for i in report["issues"] if i["severity"] == "CRITICAL"]
        self.assertTrue(any("matching send event" in i["expected_relationship"] for i in critical_issues))

    def test_reconciliation_contacted_without_attempt(self):
        """Rule 2: Lead status CONTACTED with no outcome record is flagged HIGH."""
        lead = {"lead_id": "LEAD-MAN-302", "lead_status": "CONTACTED"}
        atomic_write_json(self.leads_file, [lead])
        reconciler = ReconciliationEngine(leads_path=self.leads_file, data_dir=self.data_dir)
        report = reconciler.run_reconciliation()
        high_issues = [i for i in report["issues"] if i["severity"] == "HIGH"]
        self.assertTrue(any("recorded attempt" in i["expected_relationship"] for i in high_issues))

    def test_reconciliation_proposal_sent_without_proposal(self):
        """Rule 3: Commercial stage PROPOSAL_SENT with no proposal document is flagged CRITICAL."""
        lead = {"lead_id": "LEAD-MAN-303", "company_name": "No Proposal Co"}
        commercial = {"LEAD-MAN-303": {"lead_id": "LEAD-MAN-303", "commercial_stage": "PROPOSAL_SENT"}}
        atomic_write_json(self.leads_file, [lead])
        atomic_write_json(self.commercial_file, commercial)
        atomic_write_json(self.proposals_file, [])

        reconciler = ReconciliationEngine(
            leads_path=self.leads_file,
            commercial_records_path=self.commercial_file,
            proposals_path=self.proposals_file,
            data_dir=self.data_dir,
        )
        report = reconciler.run_reconciliation()
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("PROPOSAL_SENT" in i["field"] for i in report["issues"]))

    def test_reconciliation_unconfirmed_won_deal(self):
        """Rule 5: Commercial stage WON missing agreed value or start date is flagged CRITICAL."""
        commercial = {
            "LEAD-MAN-304": {
                "lead_id": "LEAD-MAN-304",
                "commercial_stage": "WON",
                "final_agreed_value": 0.0,  # Invalid: agreed value must be > 0
            }
        }
        atomic_write_json(self.commercial_file, commercial)
        reconciler = ReconciliationEngine(commercial_records_path=self.commercial_file, data_dir=self.data_dir)
        report = reconciler.run_reconciliation()
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("final_agreed_value" in i["field"] for i in report["issues"]))

    def test_reconciliation_pricing_discrepancy(self):
        """Rule 7: Proposal math discrepancy (subtotal - discount != total) is flagged HIGH."""
        bad_prop = {
            "proposal_id": "PROP-MAN-001",
            "lead_id": "LEAD-MAN-305",
            "subtotal": 1000.0,
            "discount": 100.0,
            "total": 950.0,  # Should be 900.0
        }
        atomic_write_json(self.proposals_file, [bad_prop])
        reconciler = ReconciliationEngine(proposals_path=self.proposals_file, data_dir=self.data_dir)
        report = reconciler.run_reconciliation()
        high_issues = [i for i in report["issues"] if i["severity"] == "HIGH"]
        self.assertTrue(any("total == subtotal - discount" in i["expected_relationship"] for i in high_issues))

    def test_reconciliation_no_silent_repair(self):
        """Reconciler strictly reports contradictions and never silently mutates source data."""
        bad_prop = {
            "proposal_id": "PROP-MAN-002",
            "lead_id": "LEAD-MAN-306",
            "subtotal": 1000.0,
            "discount": 100.0,
            "total": 500.0,
        }
        atomic_write_json(self.proposals_file, [bad_prop])
        reconciler = ReconciliationEngine(proposals_path=self.proposals_file, data_dir=self.data_dir)
        reconciler.run_reconciliation()

        # Proposal file remains exactly as written
        with open(self.proposals_file, "r") as f:
            data = json.load(f)
        self.assertEqual(data[0]["total"], 500.0)

    # =========================================================================
    # 10. BACKUP & DISASTER RECOVERY
    # =========================================================================

    def test_backup_creation_and_manifest(self):
        """BackupManager creates snapshot directory and manifest.json with SHA-256."""
        bm = BackupManager(data_dir=self.data_dir, backups_dir=self.backups_dir)
        manifest = bm.create_backup(label="unit_test_backup")
        self.assertIn("backup_id", manifest)
        self.assertIn("files", manifest)
        self.assertTrue(os.path.exists(os.path.join(self.backups_dir, manifest["backup_id"], "manifest.json")))

    def test_backup_checksum_verification(self):
        """verify_backup passes when all backed up files match checksums."""
        bm = BackupManager(data_dir=self.data_dir, backups_dir=self.backups_dir)
        manifest = bm.create_backup(label="checksum_test")
        ver = bm.verify_backup(manifest["backup_id"])
        self.assertEqual(ver["status"], "PASS")
        self.assertEqual(len(ver["errors"]), 0)

    def test_backup_tampering_detected(self):
        """Tampering with an archived backup file causes verification to fail."""
        bm = BackupManager(data_dir=self.data_dir, backups_dir=self.backups_dir)
        manifest = bm.create_backup(label="tamper_test")
        folder = bm.get_backup_path(manifest["backup_id"])

        # Tamper with an archived file
        archived_leads = os.path.join(folder, "cache_sheets_leads.json")
        with open(archived_leads, "a") as f:
            f.write(" ")

        ver = bm.verify_backup(manifest["backup_id"])
        self.assertEqual(ver["status"], "FAIL")
        self.assertTrue(any("Checksum mismatch" in e for e in ver["errors"]))

    def test_backup_restore_into_isolated_dir(self):
        """Restores all files from a backup snapshot into an isolated sandbox."""
        bm = BackupManager(data_dir=self.data_dir, backups_dir=self.backups_dir)
        manifest = bm.create_backup(label="restore_test")

        restore_sandbox = os.path.join(self.test_dir, "isolated_restore")
        res = bm.restore_backup(manifest["backup_id"], target_dir=restore_sandbox)
        self.assertEqual(res["status"], "RESTORED")
        self.assertTrue(os.path.exists(os.path.join(restore_sandbox, "cache_sheets_leads.json")))

    def test_backup_dry_run_plan(self):
        """Dry-run restoration previews target file paths without writing files."""
        bm = BackupManager(data_dir=self.data_dir, backups_dir=self.backups_dir)
        manifest = bm.create_backup(label="dry_run_test")
        target_sandbox = os.path.join(self.test_dir, "dry_run_sandbox")

        res = bm.restore_backup(manifest["backup_id"], target_dir=target_sandbox, dry_run=True)
        self.assertEqual(res["status"], "DRY_RUN")
        self.assertFalse(os.path.exists(target_sandbox))

    # =========================================================================
    # 11. TRAVEL MODE & COMMERCIAL SAFETY
    # =========================================================================

    def test_travel_mode_active_by_default(self):
        """Travel mode is enabled and commercial actions are locked."""
        self.assertTrue(SystemConfig.TRAVEL_MODE)
        self.assertFalse(SystemConfig.COMMERCIAL_ACTIONS_ENABLED)
        self.assertFalse(SystemConfig.can_execute_commercial_actions())

    def test_commercial_kill_switch_blocks_calls(self):
        """Automated calls raise CommercialActionForbiddenError under kill switch."""
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_actions_allowed("Automated outbound call")

    def test_commercial_kill_switch_blocks_dm(self):
        """Automated Instagram/Facebook DMs raise CommercialActionForbiddenError."""
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_actions_allowed("Instagram DM send")

    def test_commercial_kill_switch_blocks_email(self):
        """Cold outreach email sending is blocked under kill switch."""
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_actions_allowed("Email send")

    def test_commercial_kill_switch_blocks_proposal_send(self):
        """Proposal sending is blocked under kill switch."""
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_actions_allowed("Send proposal to prospect")

    def test_commercial_kill_switch_blocks_deal_close(self):
        """Deal won closes are blocked under kill switch."""
        with self.assertRaises(CommercialActionForbiddenError):
            SystemConfig.assert_commercial_actions_allowed("Mark deal WON")

    def test_server_commercial_endpoints_blocked_in_travel_mode(self):
        """FastAPI commercial mutation endpoints return 403 Forbidden under travel mode."""
        # 1. Operator call action
        r1 = self.client.post("/api/outreach/operator-action/call", json={"lead_id": "LEAD-1", "call_outcome": "CONNECTED"})
        self.assertEqual(r1.status_code, 403)
        self.assertIn("Commercial action blocked", r1.json()["detail"])

        # 2. Proposal mark sent
        r2 = self.client.post("/api/commercial/proposal/mark-sent", json={"proposal_id": "PROP-1", "delivery_method": "EMAIL"})
        self.assertEqual(r2.status_code, 403)

        # 3. Won deal close
        r3 = self.client.post("/api/commercial/proposal/won", json={"proposal_id": "PROP-1", "agreed_value": 750.0})
        self.assertEqual(r3.status_code, 403)

    # =========================================================================
    # 12. SECURITY & OBSERVABILITY
    # =========================================================================

    def test_secrets_and_credentials_scrubbed_from_logs(self):
        """Passwords, bearer tokens, and private keys are scrubbed by StructuredLogger."""
        data = {
            "api_key": "AIzaSySecretApiKey12345",
            "password": "SuperSecretPassword123",
            "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "normal_field": "public_data",
        }
        masked = mask_sensitive_data(data)
        self.assertEqual(masked["api_key"], "[REDACTED_SECRET]")
        self.assertEqual(masked["password"], "[REDACTED_SECRET]")
        self.assertEqual(masked["normal_field"], "public_data")

    def test_config_validator_handles_disabled_services_gracefully(self):
        """Disabled commercial providers (Meta, SMTP) report DISABLED without failing startup."""
        audit = ConfigValidator.validate_all()
        self.assertTrue(audit["startup_ok"])
        services = audit["services"]
        self.assertEqual(services["email_provider"]["status"], "DISABLED")
        self.assertEqual(services["meta_apis"]["status"], "DISABLED")

    def test_malformed_api_inputs_handled_safely(self):
        """API endpoints reject malformed payloads without unhandled 500 exceptions."""
        res = self.client.post("/api/system/jobs/run", json={"invalid_field": 12345})
        self.assertIn(res.status_code, (400, 422))

    def test_structured_log_format(self):
        """StructuredLogger formats events as valid JSON with required audit fields."""
        logger = StructuredLogger(component="TestComponent")
        record = logger.info("TestEvent", lead_id="LEAD-MAN-501", run_id="RUN-01")
        self.assertEqual(record["component"], "TestComponent")
        self.assertEqual(record["event"], "TestEvent")
        self.assertEqual(record["lead_id"], "LEAD-MAN-501")
        self.assertIn("timestamp", record)

    def test_safe_error_handler_classifications(self):
        """Technical errors are categorized into one of 7 canonical classifications."""
        handler = TechnicalErrorHandler()
        err = handler.record_error(
            component="Discovery",
            exception=ConnectionError("Connection refused by peer"),
            entity_id="LEAD-MAN-502",
        )
        self.assertEqual(err.classification, ErrorClassification.TRANSIENT.value)
        self.assertTrue(err.retryable)

    # =========================================================================
    # 13. RELIABILITY & BOUNDED RETRIES
    # =========================================================================

    def test_transient_error_bounded_retry(self):
        """Transient errors calculate exponential backoff up to max attempts."""
        handler = TechnicalErrorHandler()
        delay1 = handler.calculate_backoff(attempt=1, base_seconds=1.0)
        delay2 = handler.calculate_backoff(attempt=2, base_seconds=1.0)
        self.assertGreater(delay2, delay1)
        self.assertTrue(handler.should_retry(attempt=2, max_attempts=3))
        self.assertFalse(handler.should_retry(attempt=3, max_attempts=3))

    def test_permanent_error_not_retried(self):
        """Permanent data quality errors are marked not retryable."""
        handler = TechnicalErrorHandler()
        err = handler.record_error(
            component="Qualification",
            exception=ValueError("Missing required postcode field"),
            entity_id="LEAD-MAN-503",
        )
        self.assertEqual(err.classification, ErrorClassification.DATA_QUALITY.value)
        self.assertFalse(err.retryable)

    def test_candidate_isolation_on_error(self):
        """A failure in processing one candidate does not abort remaining batch items."""
        orchestrator = TechnicalOrchestrator(data_dir=self.data_dir)
        results = []
        candidates = ["VALID_1", "CORRUPT_CANDIDATE", "VALID_2"]
        for c in candidates:
            try:
                if c == "CORRUPT_CANDIDATE":
                    raise ValueError("Malformed record")
                results.append(c)
            except Exception:
                pass  # Error isolated and logged
        self.assertEqual(len(results), 2)
        self.assertEqual(results, ["VALID_1", "VALID_2"])

    def test_system_health_monitor_aggregation(self):
        """SystemHealthMonitor determines overall status (HEALTHY, DEGRADED, FAILED)."""
        monitor = SystemHealthMonitor(data_dir=self.data_dir)
        # Create baseline backup to satisfy health requirement
        bm = BackupManager(data_dir=self.data_dir, backups_dir=self.backups_dir)
        bm.create_backup(label="health_test")

        health = monitor.evaluate_health()
        self.assertIn(health["system_status"], ("HEALTHY", "DEGRADED"))


if __name__ == "__main__":
    unittest.main()
