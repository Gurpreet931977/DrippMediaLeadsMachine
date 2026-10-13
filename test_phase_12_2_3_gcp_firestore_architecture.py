"""
test_phase_12_2_3_gcp_firestore_architecture.py
================================================
Comprehensive Test Suite for Phase 12.2.3:
Google Cloud Run Jobs + Cloud Firestore Architecture.

Verifies:
1. Distributed operational leases: mutual exclusion, TTL expiry takeover, and clean release.
2. Resumable checkpoints: saving, retrieving, and resuming interrupted jobs without duplicate work.
3. CRM deduplication & authoritative Google Sheets boundaries: lead records remain strictly in Google Sheets.
4. Quota failure resilience & missing state handling.
5. Credential security: credentials remain server-side without disk leaks.
6. Safety rules: TRAVEL_MODE=ACTIVE, COMMERCIAL_ACTIONS_ENABLED=false, Rule B frozen at 8 criteria.
"""

import os
import json
import time
import unittest
from unittest.mock import patch, MagicMock

from lib.system.firestore_state_store import (
    FirestoreStateStore,
    COLL_LEASES,
    COLL_CHECKPOINTS,
    COLL_RUNS,
    COLL_SUPPRESSIONS,
    COLL_UNSUBSCRIBES,
)
from lib.system.system_config import SystemConfig
from lib.validation.rule_b_criteria import (
    RULE_B_VERSION,
    RULE_B_CRITERIA,
    get_canonical_rule_b_criteria_count,
)


class TestFirestoreOperationalLeases(unittest.TestCase):
    """Verifies distributed lease mutual exclusion, TTL expiration takeover, and renewals."""

    def setUp(self):
        self.store = FirestoreStateStore(force_mock=True)
        self.store.clear_mock_data()

    def test_lease_mutual_exclusion(self):
        """1. Worker A acquires lease; Worker B is blocked from acquiring the same active lease."""
        worker_a = "CRJ-WORKER-ALPHA"
        worker_b = "CRJ-WORKER-BETA"
        lease_name = "discovery_job_runner"

        # Worker A acquires lease
        acquired_a = self.store.acquire_lease(lease_name, worker_a, ttl_seconds=300.0)
        self.assertTrue(acquired_a, "Worker A must successfully acquire unheld lease")

        # Worker B attempts to acquire same lease
        acquired_b = self.store.acquire_lease(lease_name, worker_b, ttl_seconds=300.0)
        self.assertFalse(acquired_b, "Worker B must be rejected while Worker A holds active lease")

        # Check lease state
        lease = self.store.get_lease(lease_name)
        self.assertEqual(lease["holder_id"], worker_a)

    def test_lease_renewal_by_holder(self):
        """2. Existing holder can renew lease TTL."""
        worker_a = "CRJ-WORKER-ALPHA"
        lease_name = "discovery_job_runner"

        self.store.acquire_lease(lease_name, worker_a, ttl_seconds=60.0)
        initial_expires = self.store.get_lease(lease_name)["expires_epoch"]

        time.sleep(0.01)
        renewed = self.store.renew_lease(lease_name, worker_a, ttl_seconds=120.0)
        self.assertTrue(renewed)
        new_expires = self.store.get_lease(lease_name)["expires_epoch"]
        self.assertGreater(new_expires, initial_expires)

        # Non-holder cannot renew
        self.assertFalse(self.store.renew_lease(lease_name, "ROGUE-WORKER", ttl_seconds=60.0))

    def test_lease_voluntary_release(self):
        """3. Holder voluntarily releases lease; next worker can acquire immediately."""
        worker_a = "CRJ-WORKER-ALPHA"
        worker_b = "CRJ-WORKER-BETA"
        lease_name = "discovery_job_runner"

        self.store.acquire_lease(lease_name, worker_a, ttl_seconds=300.0)
        released = self.store.release_lease(lease_name, worker_a)
        self.assertTrue(released)

        # Worker B can now acquire
        acquired_b = self.store.acquire_lease(lease_name, worker_b, ttl_seconds=300.0)
        self.assertTrue(acquired_b)
        self.assertEqual(self.store.get_lease(lease_name)["holder_id"], worker_b)

    def test_expired_lease_takeover(self):
        """4. Expired lease (TTL elapsed) is automatically taken over by new worker."""
        worker_old = "CRJ-CRASHED-WORKER"
        worker_new = "CRJ-NEW-WORKER"
        lease_name = "discovery_job_runner"

        # Acquire with 0.05 second TTL
        self.store.acquire_lease(lease_name, worker_old, ttl_seconds=0.05)
        time.sleep(0.08)  # Wait for TTL to expire

        # Worker New attempts acquisition; should succeed because old lease expired
        acquired_new = self.store.acquire_lease(lease_name, worker_new, ttl_seconds=300.0)
        self.assertTrue(acquired_new, "Expired lease must be eligible for takeover")
        self.assertEqual(self.store.get_lease(lease_name)["holder_id"], worker_new)


class TestJobResumabilityAndCheckpoints(unittest.TestCase):
    """Verifies that Cloud Run Jobs checkpoint progress and resume without repeating work."""

    def setUp(self):
        self.store = FirestoreStateStore(force_mock=True)
        self.store.clear_mock_data()

    def test_checkpoint_persistence_and_retrieval(self):
        """5. Interrupted job checkpoints save to and restore from Firestore."""
        run_id = "CRJ-RUN-TEST-001"
        processed_candidates = [
            "Crown & Anchor@Manchester",
            "Albert Wilson@Manchester",
            "Cosmo@Manchester",
        ]
        stats = {
            "requested_count": 5,
            "discovered_count": 40,
            "qualified_count": 2,
        }

        # Save checkpoint mid-run
        self.store.save_checkpoint(run_id, {
            "market_id": "Manchester, United Kingdom",
            "status": "RUNNING",
            "processed_ids": processed_candidates,
            "stats": stats,
        })

        # Emulate container reboot / restart: retrieve checkpoint
        checkpoint = self.store.get_checkpoint(run_id)
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint["run_id"], run_id)
        self.assertEqual(checkpoint["status"], "RUNNING")
        self.assertEqual(checkpoint["processed_ids"], processed_candidates)
        self.assertEqual(len(checkpoint["processed_ids"]), 3)
        self.assertEqual(checkpoint["stats"]["qualified_count"], 2)

    def test_missing_checkpoint_returns_none_safely(self):
        """6. Querying non-existent checkpoint returns None without raising exceptions."""
        checkpoint = self.store.get_checkpoint("NON-EXISTENT-RUN-ID")
        self.assertIsNone(checkpoint)

    def test_checkpoint_cleanup_on_completion(self):
        """7. Checkpoint is deleted cleanly once job finishes successfully."""
        run_id = "CRJ-RUN-TEST-002"
        self.store.save_checkpoint(run_id, {"status": "RUNNING", "processed_ids": ["biz1"]})
        self.assertIsNotNone(self.store.get_checkpoint(run_id))

        self.store.delete_checkpoint(run_id)
        self.assertIsNone(self.store.get_checkpoint(run_id))


class TestCRMAuthorityAndDeduplicationBoundaries(unittest.TestCase):
    """Verifies that Google Sheets remains the authoritative CRM and leads are never stored in Firestore."""

    def setUp(self):
        self.store = FirestoreStateStore(force_mock=True)
        self.store.clear_mock_data()

    def test_firestore_does_not_contain_leads_crm_collection(self):
        """8. Firestore schema does not include a leads CRM collection; CRM leads belong in Google Sheets."""
        self.assertNotIn("leads", self.store._mock_data)
        self.assertNotIn("crm_leads", self.store._mock_data)
        self.assertIn(COLL_LEASES, self.store._mock_data)
        self.assertIn(COLL_CHECKPOINTS, self.store._mock_data)
        self.assertIn(COLL_RUNS, self.store._mock_data)

    def test_run_records_store_metadata_not_full_lead_database(self):
        """9. Pipeline run records store run-level aggregate metrics, not customer CRM rows."""
        run_id = "CRJ-RUN-AGGREGATE"
        run_data = {
            "market": "Manchester, United Kingdom",
            "status": "COMPLETED",
            "stats": {
                "discovered": 100,
                "qualified": 5,
                "manual_review": 12,
            },
            "crm_destination": "GOOGLE_SHEETS_TAB_LEADS",
        }
        self.store.save_run_record(run_id, run_data)
        saved = self.store.get_run_record(run_id)
        self.assertEqual(saved["crm_destination"], "GOOGLE_SHEETS_TAB_LEADS")
        self.assertNotIn("lead_records", saved)


class TestSuppressionsAndUnsubscribes(unittest.TestCase):
    """Verifies durable suppression and unsubscribe records in Firestore."""

    def setUp(self):
        self.store = FirestoreStateStore(force_mock=True)
        self.store.clear_mock_data()

    def test_suppression_persistence_and_case_normalization(self):
        """10. Suppressed emails are normalized to lowercase and retrieved accurately."""
        self.store.save_suppression("Test@Example.COM", {
            "reason": "Hard bounce 550",
            "suppression_type": "EMAIL_UNSUBSCRIBE",
        })
        supp = self.store.get_suppression("test@example.com")
        self.assertIsNotNone(supp)
        self.assertEqual(supp["target"], "test@example.com")
        self.assertEqual(supp["reason"], "Hard bounce 550")

    def test_unsubscribe_token_lookup(self):
        """11. One-click unsubscribe tokens are stored and retrievable."""
        token = "unsub_token_99182a"
        self.store.save_unsubscribe(token, {
            "email": "optout@drippmedia.com",
            "lead_id": "LEAD-MAN-14A2D3",
            "unsubscribed": True,
        })
        rec = self.store.get_unsubscribe(token)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["email"], "optout@drippmedia.com")
        self.assertTrue(rec["unsubscribed"])


class TestSafetyInvariantsAndRules(unittest.TestCase):
    """Verifies that all commercial safety locks and Rule B qualification criteria remain frozen."""

    def test_travel_mode_and_commercial_locks_frozen(self):
        """12. TRAVEL_MODE=ACTIVE, COMMERCIAL_ACTIONS_ENABLED=false, AUTOMATED_EMAIL_ENABLED=false."""
        status = SystemConfig.get_status_dict()
        self.assertTrue(status.get("travel_mode"), "TRAVEL_MODE must remain ACTIVE")
        self.assertFalse(status.get("commercial_actions_enabled"), "COMMERCIAL_ACTIONS_ENABLED must remain false")
        self.assertFalse(status.get("automated_email_enabled"), "AUTOMATED_EMAIL_ENABLED must remain false")
        self.assertFalse(status.get("cron_enabled"), "CRON must remain DISABLED")

    def test_rule_b_criteria_strictly_eight(self):
        """13. Rule B operational criteria remains frozen at exactly 8 criteria."""
        self.assertEqual(RULE_B_VERSION, "FROZEN")
        self.assertEqual(get_canonical_rule_b_criteria_count(), 8)
        self.assertEqual(len(RULE_B_CRITERIA), 8)

    def test_no_secrets_in_mock_or_disk(self):
        """14. Synthetic or master credentials are not serialized into lease or checkpoint documents."""
        store = FirestoreStateStore(force_mock=True)
        store.acquire_lease("test_lease", "worker1", metadata={"safe_key": "safe_val"})
        lease = store.get_lease("test_lease")
        lease_str = json.dumps(lease)
        self.assertNotIn("secret", lease_str.lower())
        self.assertNotIn("private_key", lease_str.lower())
        self.assertNotIn("password", lease_str.lower())


class TestProductionFailClosedAndLocationSemantics(unittest.TestCase):
    """Verifies fail-closed behavior in production and location distinction."""

    def test_production_mode_requires_real_firestore_client_fails_closed(self):
        """15. In production mode, failing to connect to Firestore raises RuntimeError (no silent mock fallback)."""
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            with patch("google.cloud.firestore.Client", side_effect=Exception("GCP connection error")):
                with self.assertRaises(RuntimeError) as ctx:
                    FirestoreStateStore(force_mock=False)
                self.assertIn("Production Firestore initialization failure", str(ctx.exception))
                self.assertIn("Silent fallback to in-memory mock state is strictly prohibited", str(ctx.exception))

    def test_firestore_enabled_flag_fails_closed_when_live_client_unavailable(self):
        """16. FIRESTORE_ENABLED=true raises RuntimeError if live SDK is unavailable."""
        with patch.dict(os.environ, {"FIRESTORE_ENABLED": "true", "ENVIRONMENT": "development"}):
            with patch("google.cloud.firestore.Client", side_effect=Exception("GCP SDK unavailable")):
                with self.assertRaises(RuntimeError) as ctx:
                    FirestoreStateStore(force_mock=False)
                self.assertIn("Production Firestore initialization failure", str(ctx.exception))

    def test_location_identifier_distinction_nam5_vs_us_central1(self):
        """17. nam5 (multi-region US) and us-central1 (regional Iowa) are distinct, non-interchangeable locations."""
        store_multi = FirestoreStateStore(force_mock=True, location_id="nam5")
        store_regional = FirestoreStateStore(force_mock=True, location_id="us-central1")

        self.assertEqual(store_multi.location_id, "nam5")
        self.assertEqual(store_regional.location_id, "us-central1")
        self.assertNotEqual(store_multi.location_id, store_regional.location_id)

    def test_reconcile_stale_leases(self):
        """18. reconcile_stale_leases finds expired leases and removes them from active pool."""
        store = FirestoreStateStore(force_mock=True)
        store.clear_mock_data()

        # Acquire lease with 0.05s TTL
        store.acquire_lease("job_lock_1", "worker_old", ttl_seconds=0.05)
        self.assertIsNotNone(store.get_lease("job_lock_1"))

        time.sleep(0.06)
        reconciled = store.reconcile_stale_leases()
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["lease_name"], "job_lock_1")

        # Lease is now cleared
        self.assertIsNone(store.get_lease("job_lock_1"))


class TestSuppressionMigrationIdempotence(unittest.TestCase):
    """Verifies safe, reversible suppression migration tooling."""

    def test_migration_dry_run_reports_without_writing(self):
        """19. Migration in dry_run mode calculates records without mutating store."""
        from scripts.migrate_suppressions_to_firestore import run_migration
        mock_store = FirestoreStateStore(force_mock=True)
        mock_store.clear_mock_data()

        res = run_migration(direction="to-firestore", dry_run=True, store=mock_store)
        self.assertEqual(res["status"], "DRY_RUN_COMPLETE")
        self.assertTrue(res["integrity_verified"])
        self.assertEqual(len(mock_store.list_suppressions()), 0)

    def test_synthetic_fixture_migration_and_reversal(self):
        """20. Synthetic suppression migration to Firestore and round-trip export preserves counts and integrity."""
        import tempfile
        from scripts.migrate_suppressions_to_firestore import run_migration

        synthetic_suppressions = [
            {"suppression_id": "SUPP-TEST-1", "suppression_type": "EMAIL_UNSUBSCRIBE", "target": "user1@synth.test"},
            {"suppression_id": "SUPP-TEST-2", "suppression_type": "DOMAIN_SUPPRESSION", "target": "spam.synth.test"},
        ]
        synthetic_unsubscribes = {
            "token_synth_1": {"unsubscribe_token": "token_synth_1", "email": "user1@synth.test", "unsubscribed": True},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            supp_file = os.path.join(tmpdir, "supp.json")
            unsub_file = os.path.join(tmpdir, "unsub.json")

            with open(supp_file, "w") as f:
                json.dump(synthetic_suppressions, f)
            with open(unsub_file, "w") as f:
                json.dump(synthetic_unsubscribes, f)

            mock_store = FirestoreStateStore(force_mock=True)
            mock_store.clear_mock_data()

            # Step A: Migrate to Firestore
            res_forward = run_migration(
                direction="to-firestore",
                suppression_file=supp_file,
                unsubscribes_file=unsub_file,
                dry_run=False,
                store=mock_store,
            )
            self.assertEqual(res_forward["status"], "SUCCESS")
            self.assertEqual(res_forward["suppressions_migrated"], 2)
            self.assertEqual(res_forward["unsubscribes_migrated"], 1)
            self.assertEqual(len(mock_store.list_suppressions()), 2)

            # Step B: Reverse migration (Export from Firestore to fresh destination files)
            export_supp_file = os.path.join(tmpdir, "export_supp.json")
            export_unsub_file = os.path.join(tmpdir, "export_unsub.json")

            res_reverse = run_migration(
                direction="from-firestore",
                suppression_file=export_supp_file,
                unsubscribes_file=export_unsub_file,
                dry_run=False,
                store=mock_store,
            )
            self.assertEqual(res_reverse["status"], "SUCCESS")
            self.assertEqual(res_reverse["suppressions_migrated"], 2)
            self.assertEqual(res_reverse["unsubscribes_migrated"], 1)


class TestPipelineRunManagerFirestoreIntegration(unittest.TestCase):
    """Verifies that PipelineRunManager synchronizes into canonical FirestoreStateStore."""

    def test_pipeline_run_manager_synchronizes_to_firestore(self):
        """21. Creating a run via DurablePipelineRunManager persists record to Firestore."""
        import tempfile
        from lib.system.pipeline_run_manager import DurablePipelineRunManager
        from lib.system.firestore_state_store import firestore_store

        firestore_store.clear_mock_data()

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = DurablePipelineRunManager(runs_dir=tmpdir)
            rec = mgr.create_run(
                country="United Kingdom",
                cities=["Manchester"],
                industry="Restaurants",
                qualified_leads_needed=5,
                dry_run=True,
            )
            run_id = rec["run_id"]

            # Verify presence in canonical Firestore state store
            remote_rec = firestore_store.get_run_record(run_id)
            self.assertIsNotNone(remote_rec, "Run record must be synchronized into Firestore")
            self.assertEqual(remote_rec["run_id"], run_id)
            self.assertEqual(remote_rec["status"], "QUEUED")

    def test_email_suppression_manager_respects_canonical_firestore_suppressions(self):
        """22. EmailSuppressionManager queries and honors suppressions stored in Firestore."""
        import tempfile
        from lib.outreach.email_suppression import EmailSuppressionManager
        from lib.system.firestore_state_store import firestore_store

        firestore_store.clear_mock_data()

        with tempfile.TemporaryDirectory() as tmpdir:
            empty_supp_path = os.path.join(tmpdir, "empty_supp.json")
            empty_unsub_path = os.path.join(tmpdir, "empty_unsub.json")
            supp_mgr = EmailSuppressionManager(
                suppression_path=empty_supp_path,
                unsubscribes_path=empty_unsub_path,
            )

            # Initially not suppressed
            target_email = "optout-corp@manchester-restaurants.co.uk"
            is_supp, reason = supp_mgr.is_suppressed(email=target_email)
            self.assertFalse(is_supp)

            # Store suppression in Firestore canonical store
            firestore_store.save_suppression(target_email, {
                "target": target_email,
                "reason": "CAN-SPAM unsubscribe processed via Cloud Run",
                "suppression_type": "EMAIL_UNSUBSCRIBE",
            })

            # Now EmailSuppressionManager must detect it via Firestore!
            is_supp, reason = supp_mgr.is_suppressed(email=target_email)
            self.assertTrue(is_supp, "Email must be suppressed when present in Firestore canonical store")
            self.assertIn("canonical store", reason)

            # Test unsubscribe token lookup via Firestore
            unsub_token = "token_remote_firestore_99"
            firestore_store.save_unsubscribe(unsub_token, {
                "unsubscribe_token": unsub_token,
                "lead_id": "LD-TEST-99",
                "email": target_email,
                "unsubscribed": True,
            })
            valid, lead_id, email = supp_mgr.verify_unsubscribe_token(unsub_token)
            self.assertTrue(valid)
            self.assertEqual(lead_id, "LD-TEST-99")
            self.assertEqual(email, target_email)


class TestFirestoreSerializationRegression(unittest.TestCase):
    """
    Regression Suite: Verifies Firestore recursive serialization boundary.
    Prevents TypeError: ('Cannot convert to a Firestore Value', ResearchLogEntry(...), ...)
    and ensures nested dataclasses, Enums, sets, and None values encode cleanly into Firestore.
    """

    def setUp(self):
        self.store = FirestoreStateStore(force_mock=True)
        self.store.clear_mock_data()

    def test_research_log_entry_nested_in_run_record_preserves_fields_and_survives_firestore_encoding(self):
        """
        Verifies that a final_record containing ResearchLogEntry dataclass objects
        is recursively converted into Firestore-compatible primitives, preserving
        all 28 canonical fields, None values, research evidence, and passes encode_value.
        """
        from datetime import datetime, timezone
        from lib.types import ResearchLogEntry, Lead, QualificationState, SocialStatus, OperationalStatus
        from google.cloud.firestore_v1 import _helpers

        entry = ResearchLogEntry(
            research_id="res-manchester-001",
            company_name="Apex Fitness Manchester",
            industry="gym",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            country_status="MATCH",
            city="Manchester",
            region="Greater Manchester",
            postcode="M1 2WD",
            address="45 Lever St, Manchester",
            phone="+441619876543",
            website="https://apexfitnessmcr.co.uk",
            website_status="VALID",
            verification_status="NO_WEBSITE_CONFIRMED",
            review_count=None,
            rating=None,
            social_status=SocialStatus.SOCIAL_FOUND.value,
            social_ownership_status="OWNED",
            social_profile_status="ACTIVE",
            operational_status=OperationalStatus.VERIFIED_ACTIVE.value,
            operational_confidence="HIGH",
            evidence_freshness="RECENT",
            qualification_state=QualificationState.OUTREACH_READY.value,
            qualification_status="QUALIFIED",
            disqualification_reason="",
            red_flags="",
            source_url="https://overpass-api.de/api/interpreter",
            date_researched="2026-10-13T20:15:00Z",
        )

        lead = Lead(
            lead_id="lead-manchester-001",
            company_name="Apex Fitness Manchester",
            industry="gym",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            phone="+441619876543",
            website="https://apexfitnessmcr.co.uk",
        )

        final_record = {
            "run_id": "CRJ-DRYRUN-VCT6B",
            "execution_id": "dripp-discovery-job-vct6b",
            "status": "COMPLETED",
            "started_at": "2026-10-13T20:00:00Z",
            "finished_at": "2026-10-13T20:15:00Z",
            "market": "Manchester, United Kingdom",
            "country": "United Kingdom",
            "cities": ["Manchester"],
            "industry": "gym",
            "qualified_leads_needed": 5,
            "dry_run": True,
            "leads_count": 1,
            "review_queue_count": 0,
            "research_entries_count": 1,
            "results": {
                "leads": [lead],
                "review_queue": [],
                "research_entries": [entry],
                "stats": {"discovered": 12, "qualified": 1},
                "processed_ids": {"node:12345", "node:67890"},  # set of strings
            },
        }

        # 1. Save through persistence boundary
        self.store.save_run_record("CRJ-DRYRUN-VCT6B", final_record)

        # 2. Retrieve saved record
        saved = self.store.get_run_record("CRJ-DRYRUN-VCT6B")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["status"], "COMPLETED")
        self.assertEqual(saved["run_id"], "CRJ-DRYRUN-VCT6B")

        # 3. Verify nested ResearchLogEntry is converted to dict, not stringified or discarded
        results = saved["results"]
        entries = results["research_entries"]
        self.assertEqual(len(entries), 1)
        r_saved = entries[0]
        self.assertIsInstance(r_saved, dict)
        self.assertNotIsInstance(r_saved, ResearchLogEntry)

        # 4. Verify canonical fields and values survived intact
        self.assertEqual(r_saved["research_id"], "res-manchester-001")
        self.assertEqual(r_saved["company_name"], "Apex Fitness Manchester")
        self.assertEqual(r_saved["target_country"], "United Kingdom")
        self.assertEqual(r_saved["detected_country"], "United Kingdom")
        self.assertEqual(r_saved["city"], "Manchester")
        self.assertEqual(r_saved["qualification_status"], "QUALIFIED")
        self.assertEqual(r_saved["source_url"], "https://overpass-api.de/api/interpreter")
        self.assertEqual(r_saved["date_researched"], "2026-10-13T20:15:00Z")

        # Crucial: None values are preserved, NOT discarded or turned into string 'None'
        self.assertIsNone(r_saved["review_count"])
        self.assertIsNone(r_saved["rating"])

        # 5. Verify set conversion to list for Firestore compatibility
        self.assertIsInstance(results["processed_ids"], list)
        self.assertEqual(results["processed_ids"], ["node:12345", "node:67890"])

        # 6. Verify nested Lead is also converted to dict
        leads = results["leads"]
        self.assertEqual(len(leads), 1)
        self.assertIsInstance(leads[0], dict)
        self.assertEqual(leads[0]["lead_id"], "lead-manchester-001")

        # 7. Verify Google Cloud Firestore serialization accepts the saved record
        encoded = _helpers.encode_value(saved)
        self.assertIsNotNone(encoded)
        # Check that review_count is explicitly a Firestore NullValue
        entry_val = encoded.map_value.fields["results"].map_value.fields["research_entries"].array_value.values[0]
        self.assertEqual(entry_val.map_value.fields["review_count"].null_value, 0)
        self.assertEqual(entry_val.map_value.fields["research_id"].string_value, "res-manchester-001")

    def test_all_cloud_run_record_types_survive_serialization(self):
        """
        Verifies that all record types emitted during Cloud Run job lifecycles
        (initial STARTING, COMPLETED, FAILED, ABORTED, and COMPLETED_PRIOR)
        serialize properly and encode to Firestore values.
        """
        from google.cloud.firestore_v1 import _helpers

        # A. Initial run record
        init_rec = {
            "run_id": "CRJ-INIT",
            "execution_id": "exec-001",
            "status": "RUNNING",
            "started_at": "2026-10-13T10:00:00Z",
            "market": "Manchester, United Kingdom",
            "country": "United Kingdom",
            "cities": ["Manchester"],
            "industry": "gym",
            "qualified_leads_needed": 5,
            "dry_run": True,
            "processed_candidates_count": 0,
        }
        self.store.save_run_record("CRJ-INIT", init_rec)
        saved_init = self.store.get_run_record("CRJ-INIT")
        self.assertEqual(saved_init["status"], "RUNNING")
        _helpers.encode_value(saved_init)

        # B. Aborted duplicate record
        aborted_rec = {
            "status": "ABORTED_DUPLICATE_OR_ACTIVE",
            "reason": "Active lease held by another container",
            "aborted_at": "2026-10-13T10:01:00Z",
        }
        self.store.save_run_record("CRJ-ABORT", aborted_rec)
        saved_aborted = self.store.get_run_record("CRJ-ABORT")
        self.assertEqual(saved_aborted["status"], "ABORTED_DUPLICATE_OR_ACTIVE")
        _helpers.encode_value(saved_aborted)

        # C. Completed prior execution
        prior_rec = {
            "status": "COMPLETED_PRIOR_EXECUTION",
            "finished_at": "2026-10-13T10:02:00Z",
            "leads_count": 5,
            "processed_candidates_count": 25,
        }
        self.store.save_run_record("CRJ-PRIOR", prior_rec)
        saved_prior = self.store.get_run_record("CRJ-PRIOR")
        self.assertEqual(saved_prior["status"], "COMPLETED_PRIOR_EXECUTION")
        _helpers.encode_value(saved_prior)

        # D. Failed execution record
        failed_rec = {
            "status": "FAILED",
            "failed_at": "2026-10-13T10:05:00Z",
            "error": "Simulated provider failure: Timeout connecting to OSM",
        }
        self.store.save_run_record("CRJ-FAIL", failed_rec)
        saved_fail = self.store.get_run_record("CRJ-FAIL")
        self.assertEqual(saved_fail["status"], "FAILED")
        self.assertEqual(saved_fail["error"], "Simulated provider failure: Timeout connecting to OSM")
        _helpers.encode_value(saved_fail)

    def test_checkpoint_and_lease_serialization(self):
        """
        Verifies that checkpoints and leases containing sets, enums, dates, and None
        are sanitized and encoded cleanly by Firestore.
        """
        from datetime import date
        from google.cloud.firestore_v1 import _helpers
        from lib.types import QualificationState

        # Checkpoint with set of processed IDs
        self.store.save_checkpoint("RUN-CHK-01", {
            "market_id": "Manchester, United Kingdom",
            "status": "RUNNING",
            "processed_ids": {"node:1", "node:2", "way:3"},
            "state_enum": QualificationState.RESEARCH_ONLY,
            "stats": {"count": 10},
            "snapshot_date": date(2026, 10, 13),
            "nullable_metric": None,
        })
        chk = self.store.get_checkpoint("RUN-CHK-01")
        self.assertIsInstance(chk["processed_ids"], list)
        self.assertEqual(chk["state_enum"], "RESEARCH_ONLY")
        self.assertEqual(chk["snapshot_date"], "2026-10-13")
        self.assertIsNone(chk["nullable_metric"])
        _helpers.encode_value(chk)

        # Lease with metadata
        acquired = self.store.acquire_lease(
            lease_name="test_lease_complex",
            holder_id="worker_99",
            metadata={"tags": {"production", "dry_run"}, "mode": QualificationState.OUTREACH_READY},
        )
        self.assertTrue(acquired)
        lease = self.store.get_lease("test_lease_complex")
        self.assertIsInstance(lease["metadata"]["tags"], list)
        self.assertEqual(lease["metadata"]["mode"], "OUTREACH_READY")
        _helpers.encode_value(lease)

    def test_to_firestore_compatible_direct_edges(self):
        """
        Unit tests for to_firestore_compatible helper testing edge cases:
        None, Enums, dataclasses, dicts, lists, sets, date, datetime, UUID, Path, primitives.
        """
        import uuid
        from pathlib import Path
        from datetime import date, datetime, timezone
        from google.cloud.firestore_v1 import _helpers
        from lib.system.firestore_state_store import to_firestore_compatible
        from lib.types import QualificationState

        u = uuid.uuid4()
        p = Path("/tmp/dripp/test.log")
        d = date(2026, 10, 13)
        dt = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)

        data = {
            "none_val": None,
            "enum_val": QualificationState.OUTREACH_READY,
            "uuid_val": u,
            "path_val": p,
            "date_val": d,
            "datetime_val": dt,
            "set_val": {3, 1, 2},
            "tuple_val": ("a", "b"),
            "nested": {
                "flag": True,
                "count": 42,
                "score": 98.6,
            },
        }

        clean = to_firestore_compatible(data)
        self.assertIsNone(clean["none_val"])
        self.assertEqual(clean["enum_val"], "OUTREACH_READY")
        self.assertEqual(clean["uuid_val"], str(u))
        self.assertEqual(clean["path_val"], str(p))
        self.assertEqual(clean["date_val"], "2026-10-13")
        self.assertEqual(clean["datetime_val"], dt)
        self.assertEqual(clean["set_val"], [1, 2, 3])
        self.assertEqual(clean["tuple_val"], ["a", "b"])
        self.assertTrue(clean["nested"]["flag"])

        # Firestore must encode all of this without raising TypeError
        encoded = _helpers.encode_value(clean)
        self.assertIsNotNone(encoded)

    def test_unsupported_object_fails_explicitly_with_descriptive_type_error(self):
        """
        Verifies that unsupported objects (arbitrary classes without to_dict, functions, sockets,
        complex objects) fail explicitly with a descriptive TypeError identifying the type,
        rather than being silently stringified or dumped via __dict__.
        Also verifies that ResearchLogEntry, Lead, nested collections, Enums, and None still
        serialize cleanly.
        """
        from lib.system.firestore_state_store import to_firestore_compatible
        from lib.types import ResearchLogEntry, Lead, QualificationState

        class UnsupportedPayload:
            def __init__(self, raw_value: str):
                self.raw_value = raw_value

            def __repr__(self):
                return f"UnsupportedPayload(raw_value={self.raw_value!r})"

        bad_obj = UnsupportedPayload("secret_binary_buffer")

        # 1. Direct call fails with TypeError identifying UnsupportedPayload
        with self.assertRaises(TypeError) as ctx:
            to_firestore_compatible(bad_obj)
        self.assertIn("UnsupportedPayload", str(ctx.exception))
        self.assertIn("Cannot convert object of type", str(ctx.exception))

        # 2. Nested inside a dictionary fails explicitly
        with self.assertRaises(TypeError) as ctx_nested:
            to_firestore_compatible({"safe_metric": 42, "payload": bad_obj})
        self.assertIn("UnsupportedPayload", str(ctx_nested.exception))

        # 3. Nested inside a list fails explicitly
        with self.assertRaises(TypeError) as ctx_list:
            to_firestore_compatible([1, 2, bad_obj])
        self.assertIn("UnsupportedPayload", str(ctx_list.exception))

        # 4. store.save_run_record fails explicitly when given unsupported objects
        with self.assertRaises(TypeError) as ctx_store:
            self.store.save_run_record("CRJ-BAD-TYPE", {
                "status": "RUNNING",
                "custom_unsupported": bad_obj,
            })
        self.assertIn("UnsupportedPayload", str(ctx_store.exception))

        # 5. Verify supported types still serialize successfully:
        # ResearchLogEntry, Lead, nested collections, Enums, and None
        r = ResearchLogEntry(
            research_id="res-check-99",
            company_name="Valid Gym",
            industry="fitness",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            country_status="MATCH",
            city="Manchester",
            review_count=None,
            rating=None,
            qualification_status="QUALIFIED",
            qualification_state=QualificationState.OUTREACH_READY.value,
        )
        l = Lead(
            lead_id="lead-check-99",
            company_name="Valid Gym",
            industry="fitness",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
        )
        valid_record = {
            "entry": r,
            "lead": l,
            "state": QualificationState.OUTREACH_READY,
            "nested_list": [1, "two", None],
            "nested_set": {"alpha", "beta"},
            "null_field": None,
        }
        clean_valid = to_firestore_compatible(valid_record)
        self.assertIsInstance(clean_valid["entry"], dict)
        self.assertIsNone(clean_valid["entry"]["review_count"])
        self.assertIsInstance(clean_valid["lead"], dict)
        self.assertEqual(clean_valid["state"], "OUTREACH_READY")
        self.assertEqual(clean_valid["nested_list"], [1, "two", None])
        self.assertEqual(clean_valid["nested_set"], ["alpha", "beta"])
        self.assertIsNone(clean_valid["null_field"])


if __name__ == "__main__":
    unittest.main()


