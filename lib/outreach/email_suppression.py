"""
lib/outreach/email_suppression.py
=================================
Automated Email Suppression and Unsubscribe Engine for Phase 10.2.

Supports Section 8 & 9 requirements:
- Suppression Categories:
    EMAIL_UNSUBSCRIBE
    DOMAIN_SUPPRESSION
    LEAD_SUPPRESSION
    GLOBAL_SUPPRESSION
- Token-based unsubscribe generation and verification
- Idempotent unsubscribe processing
- Cross-process advisory locking and atomic JSON persistence
"""

import os
import json
import uuid
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from lib.system.atomic_writer import atomic_write_json
from lib.system.file_lock import FileLock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DEFAULT_SUPPRESSION_PATH = os.path.join(DATA_DIR, "email_suppression.json")
DEFAULT_UNSUBSCRIBES_PATH = os.path.join(DATA_DIR, "email_unsubscribes.json")
DEFAULT_BASE_URL = os.environ.get("DRIPP_APP_URL", "https://drippmedia.com")


class SuppressionType:
    EMAIL_UNSUBSCRIBE   = "EMAIL_UNSUBSCRIBE"
    DOMAIN_SUPPRESSION  = "DOMAIN_SUPPRESSION"
    LEAD_SUPPRESSION    = "LEAD_SUPPRESSION"
    GLOBAL_SUPPRESSION  = "GLOBAL_SUPPRESSION"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class UnsubscribeLink(str):
    """
    Acts as the full URL string while supporting tuple unpacking (token, url).
    """
    def __new__(cls, token: str, url: str):
        obj = super().__new__(cls, url)
        obj.token = token
        obj.url = url
        return obj

    def __iter__(self):
        yield self.token
        yield self.url


class EmailSuppressionManager:
    """
    Manages multi-tier email suppressions and tokenized opt-outs.
    """

    def __init__(
        self,
        suppression_path: Optional[str] = None,
        unsubscribes_path: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.suppression_path = suppression_path or DEFAULT_SUPPRESSION_PATH
        self.unsubscribes_path = unsubscribes_path or DEFAULT_UNSUBSCRIBES_PATH
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._ensure_files()

    def _ensure_files(self) -> None:
        os.makedirs(os.path.dirname(self.suppression_path), exist_ok=True)
        if not os.path.exists(self.suppression_path):
            atomic_write_json(self.suppression_path, [])
        if not os.path.exists(self.unsubscribes_path):
            atomic_write_json(self.unsubscribes_path, {})

    def _load_suppressions(self) -> List[Dict[str, Any]]:
        try:
            with open(self.suppression_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def _load_unsubscribes(self) -> Dict[str, Any]:
        try:
            with open(self.unsubscribes_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def is_suppressed(
        self,
        email: str = "",
        domain: Optional[str] = None,
        lead_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Evaluates whether an email, domain, or lead is currently suppressed.
        Returns:
            (is_suppressed, reason)
        """
        clean_email = (email or "").strip().lower()
        if not domain and "@" in clean_email:
            domain = clean_email.split("@")[-1].strip().lower()
        clean_domain = (domain or "").strip().lower()
        clean_lead = (lead_id or "").strip()

        suppressions = self._load_suppressions()

        for s in suppressions:
            stype = s.get("suppression_type")
            target = str(s.get("target", "")).strip().lower()

            # 1. Global Suppression
            if stype == SuppressionType.GLOBAL_SUPPRESSION:
                if target == "*" or target in clean_email or (clean_domain and target == clean_domain):
                    return True, f"Blocked by global suppression pattern '{target}': {s.get('reason', '')}"

            # 2. Email Unsubscribe / Suppression
            if stype == SuppressionType.EMAIL_UNSUBSCRIBE and target == clean_email:
                return True, f"Email '{clean_email}' is suppressed: {s.get('reason', 'Recipient opted out / unsubscribed')}"

            # 3. Domain Suppression
            if stype == SuppressionType.DOMAIN_SUPPRESSION and clean_domain and target == clean_domain:
                return True, f"Domain '@{clean_domain}' is suppressed: {s.get('reason', 'Domain-level suppression')}"

            # 4. Lead Suppression
            if stype == SuppressionType.LEAD_SUPPRESSION and clean_lead and target == clean_lead.lower():
                return True, f"Lead '{clean_lead}' is suppressed: {s.get('reason', 'Lead-level suppression')}"

        return False, None

    def add_suppression(
        self,
        target: Optional[str] = None,
        suppression_type: Optional[str] = None,
        reason: str = "Suppression rule applied",
        metadata: Optional[Dict[str, Any]] = None,
        *,
        category: Optional[str] = None,
        identifier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Adds a new suppression record atomically under lock. Idempotent.
        """
        actual_target = str(target or identifier or "").strip().lower()
        actual_type = suppression_type or category or SuppressionType.EMAIL_UNSUBSCRIBE
        lock_file = f"{self.suppression_path}.lock"

        with FileLock(lock_file):
            records = self._load_suppressions()
            # Check existing
            for r in records:
                if r.get("suppression_type") == actual_type and str(r.get("target", "")).lower() == actual_target:
                    return r  # Already suppressed

            entry = {
                "suppression_id": f"SUPP-{uuid.uuid4().hex[:8].upper()}",
                "suppression_type": actual_type,
                "target": actual_target,
                "reason": reason,
                "created_at": _now_utc(),
                "metadata": metadata or {},
            }
            records.append(entry)
            atomic_write_json(self.suppression_path, records)
            return entry

    def suppress_email(self, email: str, reason: str = "Recipient opted out", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.add_suppression(email, SuppressionType.EMAIL_UNSUBSCRIBE, reason, metadata)

    def suppress_domain(self, domain: str, reason: str = "Domain-wide opt-out", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.add_suppression(domain, SuppressionType.DOMAIN_SUPPRESSION, reason, metadata)

    def suppress_lead(self, lead_id: str, reason: str = "Lead-level opt-out", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.add_suppression(lead_id, SuppressionType.LEAD_SUPPRESSION, reason, metadata)

    def suppress_global(self, pattern: str, reason: str = "Global policy suppression", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.add_suppression(pattern, SuppressionType.GLOBAL_SUPPRESSION, reason, metadata)

    def generate_unsubscribe_link(
        self,
        lead_id: str,
        email: str,
        campaign_id: str = "DEFAULT",
        base_url: Optional[str] = None,
    ) -> UnsubscribeLink:
        """
        Generates a tokenized unsubscribe link and stores mapping.
        Returns:
            UnsubscribeLink (acts as URL string and unpacks as (token, url))
        """
        clean_email = email.strip().lower()
        seed = f"{lead_id}:{clean_email}:{uuid.uuid4().hex}"
        token = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]

        lock_file = f"{self.unsubscribes_path}.lock"
        with FileLock(lock_file):
            tokens = self._load_unsubscribes()
            tokens[token] = {
                "unsubscribe_token": token,
                "lead_id": lead_id,
                "email": clean_email,
                "campaign_id": campaign_id,
                "created_at": _now_utc(),
                "unsubscribed": False,
                "unsubscribed_at": None,
                "reason": None,
            }
            atomic_write_json(self.unsubscribes_path, tokens)

        root_url = (base_url or self.base_url).rstrip("/")
        url = f"{root_url}/api/email/unsubscribe/{token}"
        return UnsubscribeLink(token, url)

    def verify_unsubscribe_token(self, token: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Validates an unsubscribe token.
        Returns:
            (valid, lead_id, email)
        """
        rec = self.get_unsubscribe_record(token)
        if rec and rec.get("lead_id") and rec.get("email"):
            return True, rec.get("lead_id"), rec.get("email")
        return False, None, None

    def get_unsubscribe_record(self, token: str) -> Optional[Dict[str, Any]]:
        tokens = self._load_unsubscribes()
        return tokens.get(token)

    def process_unsubscribe(
        self,
        token_or_email: str,
        reason: str = "user_direct_action",
    ) -> Dict[str, Any]:
        """
        Processes an unsubscribe request idempotently.
        Accepts either a token or direct email address.
        """
        identifier = token_or_email.strip()
        lock_file = f"{self.unsubscribes_path}.lock"

        with FileLock(lock_file):
            tokens = self._load_unsubscribes()

            # Check if token
            if identifier in tokens:
                rec = tokens[identifier]
                if rec.get("unsubscribed"):
                    # Idempotent return
                    return {
                        "status": "ALREADY_UNSUBSCRIBED",
                        "success": True,
                        "already_unsubscribed": True,
                        "unsubscribed": True,
                        "email": rec.get("email"),
                        "lead_id": rec.get("lead_id"),
                        "unsubscribed_at": rec.get("unsubscribed_at"),
                    }

                now_ts = _now_utc()
                rec["unsubscribed"] = True
                rec["unsubscribed_at"] = now_ts
                rec["reason"] = reason
                tokens[identifier] = rec
                atomic_write_json(self.unsubscribes_path, tokens)

                # Add to suppression list
                email = rec.get("email", "")
                lead_id = rec.get("lead_id", "")
                self.suppress_email(email, reason=f"Unsubscribe token processed ({reason})", metadata={"lead_id": lead_id})
                if lead_id:
                    self.suppress_lead(lead_id, reason=f"Lead unsubscribed via email ({reason})")

                return {
                    "status": "SUCCESS",
                    "success": True,
                    "already_unsubscribed": False,
                    "unsubscribed": True,
                    "email": email,
                    "lead_id": lead_id,
                    "unsubscribed_at": now_ts,
                }

            # If not a token, treated as direct email address
            if "@" in identifier:
                clean_email = identifier.lower()
                # Find any associated tokens
                found_lead_id = None
                for t, r in tokens.items():
                    if r.get("email") == clean_email:
                        r["unsubscribed"] = True
                        r["unsubscribed_at"] = r.get("unsubscribed_at") or _now_utc()
                        r["reason"] = reason
                        found_lead_id = r.get("lead_id")

                atomic_write_json(self.unsubscribes_path, tokens)
                self.suppress_email(clean_email, reason=f"Direct unsubscribe ({reason})", metadata={"lead_id": found_lead_id})
                if found_lead_id:
                    self.suppress_lead(found_lead_id, reason=f"Lead unsubscribed via direct email ({reason})")

                return {
                    "status": "SUCCESS",
                    "success": True,
                    "already_unsubscribed": False,
                    "unsubscribed": True,
                    "email": clean_email,
                    "lead_id": found_lead_id,
                    "unsubscribed_at": _now_utc(),
                }

            return {
                "status": "INVALID_TOKEN",
                "success": False,
                "unsubscribed": False,
                "error": f"Token or email '{identifier}' not found in registry",
            }
