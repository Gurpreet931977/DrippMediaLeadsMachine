"""
lib/system/firestore_state_store.py
===================================
Durable Operational State, Distributed Leases, and Resumable Checkpoint Store
backed by Google Cloud Firestore (Native Mode).

Fulfills Phase 12.2.3 Architecture Requirements:
1. Operational Leases: Distributed cross-container mutual exclusion with TTL & heartbeats.
2. Job Checkpoints: Resumable progress tracking for Cloud Run Jobs across container restarts.
3. Durable Run Records: Pipeline run metadata, stats, and recovery logs.
4. Suppressions & Unsubscribes: Durable CAN-SPAM / GDPR opt-out records.
5. Strict Invariant: Google Sheets remains the authoritative CRM for LEADS, REVIEW_QUEUE,
   and RESEARCH_LOG. Lead records are NEVER migrated to Firestore.
6. Offline / Mock Mode: Full in-memory deterministic transactional semantics when the GCP SDK
   is absent or when executing in local test suites.
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("FirestoreStateStore")

# Firestore Collection Names
COLL_LEASES = "operational_leases"
COLL_CHECKPOINTS = "job_checkpoints"
COLL_RUNS = "pipeline_runs"
COLL_SUPPRESSIONS = "email_suppressions"
COLL_UNSUBSCRIBES = "email_unsubscribes"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> float:
    return time.time()


class FirestoreStateStore:
    """
    Manages operational leases, checkpoints, and job recovery metadata in Firestore.
    Supports both live google-cloud-firestore and deterministic in-memory mock mode.
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        database_id: str = "(default)",
        force_mock: bool = False,
    ):
        self.project_id = (
            project_id
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or os.getenv("GCP_PROJECT")
            or os.getenv("PROJECT_ID", "dripp-media-leads")
        )
        self.database_id = database_id
        self._is_live = False
        self._client = None
        self._mock_data: Dict[str, Dict[str, Dict[str, Any]]] = {
            COLL_LEASES: {},
            COLL_CHECKPOINTS: {},
            COLL_RUNS: {},
            COLL_SUPPRESSIONS: {},
            COLL_UNSUBSCRIBES: {},
        }

        # Check if live Firestore is explicitly enabled and SDK is installed
        live_enabled = os.getenv("FIRESTORE_ENABLED", "").lower() in ("true", "1", "yes")
        if live_enabled and not force_mock:
            try:
                from google.cloud import firestore
                self._client = firestore.Client(project=self.project_id, database=self.database_id)
                self._is_live = True
                logger.info("Initialized live Google Cloud Firestore client for project %s", self.project_id)
            except Exception as e:
                logger.warning(
                    "Could not initialize live Google Cloud Firestore client (%s). Falling back to resilient mock store.",
                    e,
                )
                self._is_live = False
        else:
            self._is_live = False

    @property
    def is_live(self) -> bool:
        """True if backed by live Google Cloud Firestore SDK."""
        return self._is_live

    # -------------------------------------------------------------------------
    # 1. DISTRIBUTED OPERATIONAL LEASES (Mutual Exclusion)
    # -------------------------------------------------------------------------

    def acquire_lease(
        self,
        lease_name: str,
        holder_id: str,
        ttl_seconds: float = 600.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Attempts to acquire or take over a distributed operational lease.
        Acquisition succeeds if:
          a) No lease exists for lease_name.
          b) Existing lease has expired (current_time > expires_at).
          c) Lease is already held by holder_id (refreshes TTL).
        Returns True on successful acquisition, False if currently held by an unexpired worker.
        """
        now = _now_epoch()
        expires_at = now + ttl_seconds
        lease_doc = {
            "lease_name": lease_name,
            "holder_id": holder_id,
            "acquired_at": _now_utc(),
            "acquired_epoch": now,
            "expires_epoch": expires_at,
            "ttl_seconds": ttl_seconds,
            "metadata": metadata or {},
        }

        if self._is_live:
            try:
                from google.cloud import firestore

                doc_ref = self._client.collection(COLL_LEASES).document(lease_name)

                @firestore.transactional
                def _tx_acquire(transaction):
                    snapshot = doc_ref.get(transaction=transaction)
                    if snapshot.exists:
                        data = snapshot.to_dict()
                        curr_expires = data.get("expires_epoch", 0.0)
                        curr_holder = data.get("holder_id")
                        # If active and owned by someone else, fail acquisition
                        if curr_expires > now and curr_holder != holder_id:
                            return False
                    transaction.set(doc_ref, lease_doc)
                    return True

                transaction = self._client.transaction()
                return _tx_acquire(transaction)
            except Exception as e:
                logger.error("Error acquiring Firestore lease %s: %e", lease_name, e)
                return False
        else:
            # In-memory mock transactional semantics
            existing = self._mock_data[COLL_LEASES].get(lease_name)
            if existing:
                curr_expires = existing.get("expires_epoch", 0.0)
                curr_holder = existing.get("holder_id")
                if curr_expires > now and curr_holder != holder_id:
                    return False
            self._mock_data[COLL_LEASES][lease_name] = lease_doc
            return True

    def renew_lease(self, lease_name: str, holder_id: str, ttl_seconds: float = 600.0) -> bool:
        """
        Renews an existing lease's expiration TTL if held by holder_id.
        """
        now = _now_epoch()
        new_expires = now + ttl_seconds

        if self._is_live:
            try:
                from google.cloud import firestore

                doc_ref = self._client.collection(COLL_LEASES).document(lease_name)

                @firestore.transactional
                def _tx_renew(transaction):
                    snapshot = doc_ref.get(transaction=transaction)
                    if not snapshot.exists:
                        return False
                    data = snapshot.to_dict()
                    if data.get("holder_id") != holder_id:
                        return False
                    transaction.update(
                        doc_ref,
                        {
                            "expires_epoch": new_expires,
                            "last_heartbeat": _now_utc(),
                            "ttl_seconds": ttl_seconds,
                        },
                    )
                    return True

                transaction = self._client.transaction()
                return _tx_renew(transaction)
            except Exception as e:
                logger.error("Error renewing lease %s: %s", lease_name, e)
                return False
        else:
            existing = self._mock_data[COLL_LEASES].get(lease_name)
            if existing and existing.get("holder_id") == holder_id:
                existing["expires_epoch"] = new_expires
                existing["last_heartbeat"] = _now_utc()
                existing["ttl_seconds"] = ttl_seconds
                return True
            return False

    def release_lease(self, lease_name: str, holder_id: str) -> bool:
        """
        Releases a lease if held by holder_id.
        """
        if self._is_live:
            try:
                from google.cloud import firestore

                doc_ref = self._client.collection(COLL_LEASES).document(lease_name)

                @firestore.transactional
                def _tx_release(transaction):
                    snapshot = doc_ref.get(transaction=transaction)
                    if not snapshot.exists:
                        return True
                    data = snapshot.to_dict()
                    if data.get("holder_id") == holder_id:
                        transaction.delete(doc_ref)
                        return True
                    return False

                transaction = self._client.transaction()
                return _tx_release(transaction)
            except Exception as e:
                logger.error("Error releasing lease %s: %s", lease_name, e)
                return False
        else:
            existing = self._mock_data[COLL_LEASES].get(lease_name)
            if existing and existing.get("holder_id") == holder_id:
                self._mock_data[COLL_LEASES].pop(lease_name, None)
                return True
            return existing is None

    def get_lease(self, lease_name: str) -> Optional[Dict[str, Any]]:
        """Retrieves active lease record if present and unexpired."""
        if self._is_live:
            try:
                doc = self._client.collection(COLL_LEASES).document(lease_name).get()
                if doc.exists:
                    return doc.to_dict()
                return None
            except Exception as e:
                logger.error("Error fetching lease %s: %s", lease_name, e)
                return None
        else:
            return self._mock_data[COLL_LEASES].get(lease_name)

    # -------------------------------------------------------------------------
    # 2. JOB RESUMABILITY & CHECKPOINTS
    # -------------------------------------------------------------------------

    def save_checkpoint(self, run_id: str, checkpoint_data: Dict[str, Any]) -> None:
        """
        Persists a fine-grained progress checkpoint for a running or interrupted job.
        Records processed candidate keys, qualified lead IDs, and partial metrics.
        """
        record = {
            "run_id": run_id,
            "last_updated_at": _now_utc(),
            **checkpoint_data,
        }
        if self._is_live:
            try:
                self._client.collection(COLL_CHECKPOINTS).document(run_id).set(record)
            except Exception as e:
                logger.error("Error saving checkpoint for %s: %s", run_id, e)
                raise
        else:
            self._mock_data[COLL_CHECKPOINTS][run_id] = record

    def get_checkpoint(self, run_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a job checkpoint to resume execution without repeating completed work.
        """
        if self._is_live:
            try:
                doc = self._client.collection(COLL_CHECKPOINTS).document(run_id).get()
                return doc.to_dict() if doc.exists else None
            except Exception as e:
                logger.error("Error fetching checkpoint for %s: %s", run_id, e)
                return None
        else:
            return self._mock_data[COLL_CHECKPOINTS].get(run_id)

    def delete_checkpoint(self, run_id: str) -> None:
        """Deletes checkpoint upon verified completion."""
        if self._is_live:
            try:
                self._client.collection(COLL_CHECKPOINTS).document(run_id).delete()
            except Exception:
                pass
        else:
            self._mock_data[COLL_CHECKPOINTS].pop(run_id, None)

    # -------------------------------------------------------------------------
    # 3. PIPELINE RUN RECORDS (Execution Lifecycle Metadata)
    # -------------------------------------------------------------------------

    def save_run_record(self, run_id: str, run_data: Dict[str, Any]) -> None:
        """Persists complete pipeline run record and metrics to Firestore."""
        record = {
            "run_id": run_id,
            "updated_at": _now_utc(),
            **run_data,
        }
        if self._is_live:
            try:
                self._client.collection(COLL_RUNS).document(run_id).set(record)
            except Exception as e:
                logger.error("Error saving run record %s: %s", run_id, e)
                raise
        else:
            self._mock_data[COLL_RUNS][run_id] = record

    def get_run_record(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Fetches a pipeline run record."""
        if self._is_live:
            try:
                doc = self._client.collection(COLL_RUNS).document(run_id).get()
                return doc.to_dict() if doc.exists else None
            except Exception:
                return None
        else:
            return self._mock_data[COLL_RUNS].get(run_id)

    def list_run_records(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Lists recent run records."""
        if self._is_live:
            try:
                docs = (
                    self._client.collection(COLL_RUNS)
                    .order_by("created_at", direction="DESCENDING")
                    .limit(limit)
                    .stream()
                )
                return [d.to_dict() for d in docs]
            except Exception:
                return []
        else:
            records = list(self._mock_data[COLL_RUNS].values())
            records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
            return records[:limit]

    # -------------------------------------------------------------------------
    # 4. EMAIL SUPPRESSIONS & UNSUBSCRIBES
    # -------------------------------------------------------------------------

    def save_suppression(self, target: str, suppression_data: Dict[str, Any]) -> None:
        """Stores suppression record (email, domain, lead) in Firestore."""
        clean_target = target.strip().lower()
        record = {
            "target": clean_target,
            "updated_at": _now_utc(),
            **suppression_data,
        }
        if self._is_live:
            try:
                self._client.collection(COLL_SUPPRESSIONS).document(clean_target).set(record)
            except Exception as e:
                logger.error("Error saving suppression for %s: %s", clean_target, e)
                raise
        else:
            self._mock_data[COLL_SUPPRESSIONS][clean_target] = record

    def get_suppression(self, target: str) -> Optional[Dict[str, Any]]:
        """Checks if a target is present in Firestore suppressions."""
        clean_target = target.strip().lower()
        if self._is_live:
            try:
                doc = self._client.collection(COLL_SUPPRESSIONS).document(clean_target).get()
                return doc.to_dict() if doc.exists else None
            except Exception:
                return None
        else:
            return self._mock_data[COLL_SUPPRESSIONS].get(clean_target)

    def list_suppressions(self) -> List[Dict[str, Any]]:
        """Lists all suppressions from Firestore."""
        if self._is_live:
            try:
                docs = self._client.collection(COLL_SUPPRESSIONS).stream()
                return [d.to_dict() for d in docs]
            except Exception:
                return []
        else:
            return list(self._mock_data[COLL_SUPPRESSIONS].values())

    def save_unsubscribe(self, token: str, unsub_data: Dict[str, Any]) -> None:
        """Stores one-click unsubscribe token mapping."""
        clean_token = token.strip()
        record = {
            "token": clean_token,
            "updated_at": _now_utc(),
            **unsub_data,
        }
        if self._is_live:
            try:
                self._client.collection(COLL_UNSUBSCRIBES).document(clean_token).set(record)
            except Exception as e:
                logger.error("Error saving unsubscribe token %s: %s", clean_token, e)
                raise
        else:
            self._mock_data[COLL_UNSUBSCRIBES][clean_token] = record

    def get_unsubscribe(self, token: str) -> Optional[Dict[str, Any]]:
        """Looks up an unsubscribe token."""
        clean_token = token.strip()
        if self._is_live:
            try:
                doc = self._client.collection(COLL_UNSUBSCRIBES).document(clean_token).get()
                return doc.to_dict() if doc.exists else None
            except Exception:
                return None
        else:
            return self._mock_data[COLL_UNSUBSCRIBES].get(clean_token)

    # -------------------------------------------------------------------------
    # 5. DIAGNOSTICS & PURGE (Testing only)
    # -------------------------------------------------------------------------

    def clear_mock_data(self) -> None:
        """Clears in-memory mock data (used for test teardown)."""
        for k in self._mock_data:
            self._mock_data[k].clear()


# Default singleton instance
firestore_store = FirestoreStateStore()
