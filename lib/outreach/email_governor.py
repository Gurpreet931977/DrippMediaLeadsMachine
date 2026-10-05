"""
lib/outreach/email_governor.py
==============================
Rate Limiting, Idempotency, Retry, and Bounce Governance Engine for Phase 10.2.

Implements Sections 17, 18, 19, and 20:
1. Multi-tier rate limiting: per_minute, per_hour, per_day, per_domain.
2. Deterministic send key idempotency protection.
3. Strict retry limits (max 2 retries, zero retries for non-retryable statuses).
4. Permanent bounce handling with specific email suppression (domain preserved).
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List

from lib.system.atomic_writer import atomic_write_json
from lib.system.file_lock import FileLock
from lib.outreach.email_suppression import EmailSuppressionManager

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DEFAULT_RATE_LIMIT_PATH = os.path.join(DATA_DIR, "email_rate_limits.json")
DEFAULT_IDEMPOTENCY_PATH = os.path.join(DATA_DIR, "email_idempotency_log.json")

# Conservative rate limit defaults (Section 17)
DEFAULT_LIMIT_PER_MINUTE = 2
DEFAULT_LIMIT_PER_HOUR   = 10
DEFAULT_LIMIT_PER_DAY    = 50
DEFAULT_LIMIT_PER_DOMAIN = 1  # max 1 send to same domain per day
MAX_RETRIES              = 2  # Section 19


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class EmailGovernor:
    """
    Coordinates email rate limits, idempotency guards, retries, and bounce governance.
    """

    def __init__(
        self,
        rate_limit_path: Optional[str] = None,
        idempotency_path: Optional[str] = None,
        limit_per_minute: int = DEFAULT_LIMIT_PER_MINUTE,
        limit_per_hour: int = DEFAULT_LIMIT_PER_HOUR,
        limit_per_day: int = DEFAULT_LIMIT_PER_DAY,
        limit_per_domain: int = DEFAULT_LIMIT_PER_DOMAIN,
        suppression_manager: Optional[EmailSuppressionManager] = None,
    ):
        self.rate_limit_path = rate_limit_path or DEFAULT_RATE_LIMIT_PATH
        self.idempotency_path = idempotency_path or DEFAULT_IDEMPOTENCY_PATH
        self.limit_per_minute = limit_per_minute
        self.limit_per_hour = limit_per_hour
        self.limit_per_day = limit_per_day
        self.limit_per_domain = limit_per_domain
        self.suppression_manager = suppression_manager or EmailSuppressionManager()
        self._ensure_files()

    def _ensure_files(self) -> None:
        os.makedirs(os.path.dirname(self.rate_limit_path), exist_ok=True)
        if not os.path.exists(self.rate_limit_path):
            atomic_write_json(self.rate_limit_path, {"sends": []})
        if not os.path.exists(self.idempotency_path):
            atomic_write_json(self.idempotency_path, {})

    def _load_rate_data(self) -> Dict[str, Any]:
        try:
            with open(self.rate_limit_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {"sends": []}
        except Exception:
            return {"sends": []}

    def _load_idempotency_data(self) -> Dict[str, Any]:
        try:
            with open(self.idempotency_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # -------------------------------------------------------------------------
    # 1. Rate Limiting (Section 17)
    # -------------------------------------------------------------------------
    def check_rate_limits(self, domain_or_email: str) -> Tuple[bool, Optional[str]]:
        """
        Validates sending against minute, hour, day, and per-domain limits.
        Accepts either a domain (e.g. 'acme.co.uk') or a full email address
        (e.g. 'first@acme.co.uk') — domain is auto-extracted from emails.
        Returns: (allowed, pause_reason)
        """
        raw = domain_or_email.strip()
        if "@" in raw:
            clean_domain = raw.split("@")[-1].lower()
        else:
            clean_domain = raw.lower()
        now = datetime.now(timezone.utc)
        data = self._load_rate_data()
        sends = data.get("sends", [])

        # Filter recent
        one_min_ago = now - timedelta(minutes=1)
        one_hour_ago = now - timedelta(hours=1)
        one_day_ago = now - timedelta(days=1)

        min_count = 0
        hour_count = 0
        day_count = 0
        domain_day_count = 0

        for s in sends:
            try:
                s_time = datetime.fromisoformat(s["timestamp"])
            except Exception:
                continue

            if s_time >= one_day_ago:
                day_count += 1
                if s.get("domain") == clean_domain:
                    domain_day_count += 1
            if s_time >= one_hour_ago:
                hour_count += 1
            if s_time >= one_min_ago:
                min_count += 1

        if min_count >= self.limit_per_minute:
            return False, f"Per-minute rate limit reached ({min_count}/{self.limit_per_minute})"
        if hour_count >= self.limit_per_hour:
            return False, f"Per-hour rate limit reached ({hour_count}/{self.limit_per_hour})"
        if day_count >= self.limit_per_day:
            return False, f"Daily send limit reached ({day_count}/{self.limit_per_day})"
        if domain_day_count >= self.limit_per_domain:
            return False, f"Per-domain rate limit reached for @{clean_domain} ({domain_day_count}/{self.limit_per_domain})"

        return True, None

    def record_send(self, recipient: str, domain: Optional[str] = None) -> None:
        """
        Atomically records an initiated send and purges entries older than 24h.
        If domain is not provided, it is auto-extracted from the recipient email.
        """
        clean_email = recipient.strip()
        if domain:
            clean_domain = domain.strip().lower()
        else:
            # Auto-extract domain from email address
            parts = clean_email.split("@")
            clean_domain = parts[-1].lower() if len(parts) > 1 else clean_email.lower()
        now = datetime.now(timezone.utc)
        one_day_ago = now - timedelta(days=1)
        lock_file = f"{self.rate_limit_path}.lock"

        with FileLock(lock_file):
            data = self._load_rate_data()
            sends = data.get("sends", [])
            # Prune older than 24h
            recent_sends = []
            for s in sends:
                try:
                    if datetime.fromisoformat(s["timestamp"]) >= one_day_ago:
                        recent_sends.append(s)
                except Exception:
                    pass

            recent_sends.append({
                "recipient": recipient,
                "domain": clean_domain,
                "timestamp": now.isoformat(),
            })
            data["sends"] = recent_sends
            atomic_write_json(self.rate_limit_path, data)

    # -------------------------------------------------------------------------
    # 2. Idempotency (Section 18)
    # -------------------------------------------------------------------------
    @classmethod
    def generate_idempotency_key(
        cls,
        lead_id: str,
        email: str,
        campaign_id: str,
        template_version: str,
        attempt_number: int = 1,
    ) -> str:
        """
        Generates deterministic send key:
        lead_id + normalized_email + campaign_id + template_version + attempt_number
        """
        clean_email = email.strip().lower()
        return f"{lead_id}:{clean_email}:{campaign_id}:{template_version}:{attempt_number}"

    @classmethod
    def generate_send_key(
        cls,
        lead_id: str,
        email: str,
        campaign_id: str,
        template_version: str,
        attempt_number: int = 1,
    ) -> str:
        """Alias for generate_idempotency_key(). Generates deterministic send key."""
        return cls.generate_idempotency_key(
            lead_id=lead_id,
            email=email,
            campaign_id=campaign_id,
            template_version=template_version,
            attempt_number=attempt_number,
        )

    def is_duplicate_send(self, send_key: str) -> bool:
        """Returns True if this send_key has already been processed (duplicate guard)."""
        return self.get_idempotent_record(send_key) is not None

    def get_idempotent_record(self, send_key: str) -> Optional[Dict[str, Any]]:
        """
        Returns prior execution record if this key has already been processed.
        """
        data = self._load_idempotency_data()
        return data.get(send_key)

    def record_idempotent_dispatch(self, send_key: str, record: Dict[str, Any]) -> None:
        """
        Atomically saves the send record indexed by idempotency key.
        """
        lock_file = f"{self.idempotency_path}.lock"
        with FileLock(lock_file):
            data = self._load_idempotency_data()
            data[send_key] = record
            atomic_write_json(self.idempotency_path, data)

    def get_cached_send(self, send_key: str) -> Optional[Dict[str, Any]]:
        """Alias for get_idempotent_record()."""
        return self.get_idempotent_record(send_key)

    def record_idempotency(self, send_key: str, result: Optional[Dict[str, Any]] = None, **kwargs) -> None:
        """Alias for record_idempotent_dispatch(). Persists idempotency record by send_key."""
        rec = dict(result or {})
        rec.update(kwargs)
        self.record_idempotent_dispatch(send_key, rec)

    def _load_rate_limits(self) -> Dict[str, Any]:
        """Alias for _load_rate_data(). Returns the full rate-limit data store."""
        return self._load_rate_data()

    # -------------------------------------------------------------------------
    # 3. Retries (Section 19)
    # -------------------------------------------------------------------------
    @classmethod
    def can_retry(
        cls,
        status: str,
        current_attempt: int = 1,
        attempt_number: Optional[int] = None,
        is_retryable_error: bool = True,
    ) -> bool:
        """
        Evaluates whether a failed send may be retried.
        Returns bool for convenience; use can_retry_with_reason() for the full tuple.
        """
        # Support both kwarg names: attempt_number (tests) and current_attempt (internal)
        effective_attempt = attempt_number if attempt_number is not None else current_attempt

        # Maximum 2 retries (attempt 1 -> retry 1 -> retry 2 -> halt)
        if effective_attempt > MAX_RETRIES:
            return False

        # Strictly non-retryable statuses
        if status in ("UNSUBSCRIBED", "BOUNCED", "REJECTED", "INVALID_RECIPIENT", "SUPPRESSED", "COMPLIANCE_BLOCKED"):
            return False

        # Ambiguous unknown result: do not immediately retry
        if status == "UNKNOWN":
            return False

        if is_retryable_error or status in ("RATE_LIMITED", "FAILED"):
            return True

        return False

    @classmethod
    def can_retry_with_reason(
        cls,
        status: str,
        current_attempt: int,
        is_retryable_error: bool = False,
    ) -> Tuple[bool, str]:
        """
        Full retry evaluation returning (allowed, reason).
        Used internally by the executor pipeline.
        """
        if current_attempt >= (MAX_RETRIES + 1):
            return False, f"MAX_RETRIES_REACHED: Attempt {current_attempt} exceeds max limit of {MAX_RETRIES}"
        if status in ("UNSUBSCRIBED", "BOUNCED", "REJECTED", "INVALID_RECIPIENT", "SUPPRESSED", "COMPLIANCE_BLOCKED"):
            return False, f"NON_RETRYABLE_STATUS: {status} must never be retried"
        if status == "UNKNOWN":
            return False, "AMBIGUOUS_UNKNOWN_RESULT: Unconfirmed delivery state, do not immediately retry"
        if is_retryable_error:
            return True, f"RETRY_PERMITTED: Attempt {current_attempt + 1} of {MAX_RETRIES + 1}"
        return False, "NON_RETRYABLE_ERROR"

    # -------------------------------------------------------------------------
    # 4. Bounce Handling (Section 20)
    # -------------------------------------------------------------------------
    def handle_bounce(
        self,
        email: str,
        lead_id: str,
        bounce_evidence: str = "",
        is_hard_bounce: bool = True,
        bounce_type: Optional[str] = None,
        provider_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Processes a bounce event:
        1. Hard bounce -> suppresses specific email address.
        2. Preserves domain (does not suppress domain from one bad email).
        3. Returns audit dictionary.

        Accepts both:
          - is_hard_bounce=True/False (original API)
          - bounce_type="HARD_BOUNCE"|"SOFT_BOUNCE" (test-facing API)
        """
        # Reconcile bounce_type kwarg with is_hard_bounce flag
        if bounce_type is not None:
            is_hard_bounce = bounce_type.upper() == "HARD_BOUNCE"

        evidence = provider_reason or bounce_evidence
        clean_email = email.strip().lower()

        if is_hard_bounce:
            supp_record = self.suppression_manager.suppress_email(
                clean_email,
                reason=f"HARD_BOUNCE: {evidence}",
                metadata={"lead_id": lead_id, "evidence": evidence},
            )
            return {
                "status": "BOUNCE_PROCESSED",
                "email": clean_email,
                "lead_id": lead_id,
                "suppressed": True,
                "suppression_id": supp_record.get("suppression_id"),
                "domain_suppressed": False,
                "notes": "Specific email address suppressed. Entity and domain preserved.",
            }

        return {
            "status": "SOFT_BOUNCE_LOGGED",
            "email": clean_email,
            "lead_id": lead_id,
            "suppressed": False,
            "notes": "Transient soft bounce recorded without immediate suppression.",
        }
