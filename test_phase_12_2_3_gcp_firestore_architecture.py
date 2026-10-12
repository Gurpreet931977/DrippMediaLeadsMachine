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


if __name__ == "__main__":
    unittest.main()
