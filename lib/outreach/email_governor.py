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
    def check_rate_limits(self, domain: str) -> Tuple[bool, Optional[str]]:
        """
        Validates sending against minute, hour, day, and per-domain limits.
        Returns:
            (allowed, pause_reason)
        """
        clean_domain = domain.strip().lower()
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
            return False, f"RATE_LIMIT_PAUSE: Per-minute limit reached ({min_count}/{self.limit_per_minute})"
        if hour_count >= self.limit_per_hour:
            return False, f"RATE_LIMIT_PAUSE: Per-hour limit reached ({hour_count}/{self.limit_per_hour})"
        if day_count >= self.limit_per_day:
            return False, f"RATE_LIMIT_PAUSE: Daily send limit reached ({day_count}/{self.limit_per_day})"
        if domain_day_count >= self.limit_per_domain:
            return False, f"RATE_LIMIT_PAUSE: Domain daily limit reached for @{clean_domain} ({domain_day_count}/{self.limit_per_domain})"

        return True, None

    def record_send(self, recipient: str, domain: str) -> None:
        """
        Atomically records an initiated send and purges entries older than 24h.
        """
        clean_domain = domain.strip().lower()
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

    # -------------------------------------------------------------------------
    # 3. Retries (Section 19)
    # -------------------------------------------------------------------------
    @classmethod
    def can_retry(
        cls,
        status: str,
        current_attempt: int,
        is_retryable_error: bool = False,
    ) -> Tuple[bool, str]:
        """
        Evaluates whether a failed send may be retried.
        """
        # Maximum 2 retries (attempt 1 -> retry 1 -> retry 2 -> halt)
        if current_attempt >= (MAX_RETRIES + 1):
            return False, f"MAX_RETRIES_REACHED: Attempt {current_attempt} exceeds max limit of {MAX_RETRIES}"

        # Strictly non-retryable statuses
        if status in ("UNSUBSCRIBED", "BOUNCED", "REJECTED", "INVALID_RECIPIENT", "SUPPRESSED", "COMPLIANCE_BLOCKED"):
            return False, f"NON_RETRYABLE_STATUS: {status} must never be retried"

        # Ambiguous unknown result: do not immediately retry
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
        bounce_evidence: str,
        is_hard_bounce: bool = True,
    ) -> Dict[str, Any]:
        """
        Processes a bounce event:
        1. Hard bounce -> suppresses specific email address.
        2. Preserves domain (does not suppress domain from one bad email).
        3. Returns audit dictionary.
        """
        clean_email = email.strip().lower()
        if is_hard_bounce:
            supp_record = self.suppression_manager.suppress_email(
                clean_email,
                reason=f"HARD_BOUNCE: {bounce_evidence}",
                metadata={"lead_id": lead_id, "evidence": bounce_evidence},
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
