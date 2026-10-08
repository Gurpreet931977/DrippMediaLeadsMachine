"""
lib/system/state_integrity.py
=============================
Production State Integrity, Corruption Detection & Recovery Validation Engine.
Phase 10.5 — Security, Data Integrity & Backup/Restore Hardening.

INVARIANTS:
  1. Detect -> Report -> Refuse unsafe mutation -> Preserve known-good state.
  2. Never silently guess, forge, or repair ambiguous/corrupted state.
  3. Google Sheets remains canonical CRM authority.
  4. Protected historical outreach states (SENT, BOUNCED, SUPPRESSED, NEVER_CONFIRMED_SENT)
     are strictly immutable.
  5. Canonical lead IDs (^LEAD-[A-Z0-9]+-[A-F0-9]{6,}$) are strictly preserved.
  6. Interrupted atomic writes (.tmp) must never be loaded as valid production state.
"""

import os
import re
import json
import logging
from typing import Dict, Any, List, Optional, Tuple, Set

from lib.system.identity_integrity import CANONICAL_LEAD_ID_REGEX, IdentityIntegrityAuditor
from lib.system.freshness_models import PROTECTED_HISTORICAL_STATES

logger = logging.getLogger("StateIntegrity")

CRITICAL_STORE_FILES = [
    "cache_sheets_leads.json",
    "message_history.json",
    "suppression_list.json",
    "outreach_outcomes.json",
    "campaigns.json",
    "execution_gate.json",
    "commercial_records.json",
    "commercial_proposals.json",
]


class StateCorruptionError(Exception):
    """Raised when critical state is corrupted, truncated, or unparseable."""
    pass


class StateIntegrityEngine:
    """
    Audits file system state integrity, detects corruption, enforces protected states,
    and reconciles Google Sheets CRM authority against local state.
    """

    def __init__(self, data_dir: Optional[str] = None):
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        self.data_dir = data_dir or os.path.join(project_root, "data")

    def audit_store_file(self, filename: str) -> Dict[str, Any]:
        """
        Audits a single critical data store file for corruption, truncation, or schema failure.
        """
        filepath = os.path.join(self.data_dir, filename)
        if not os.path.exists(filepath):
            return {
                "file": filename,
                "status": "MISSING",
                "is_corrupt": False,
                "error": "File does not exist",
            }

        file_size = os.path.getsize(filepath)
        if file_size == 0:
            return {
                "file": filename,
                "status": "CORRUPT",
                "is_corrupt": True,
                "error": "Zero-byte truncated file",
            }

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError as ue:
            return {
                "file": filename,
                "status": "CORRUPT",
                "is_corrupt": True,
                "error": f"Binary/corrupted encoding: {ue}",
            }

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as je:
            return {
                "file": filename,
                "status": "CORRUPT",
                "is_corrupt": True,
                "error": f"Malformed or truncated JSON: {je}",
            }

        # Check for unfinalized tmp marker or invalid top-level type
        if filename == "cache_sheets_leads.json":
            if not isinstance(parsed, (dict, list)):
                return {
                    "file": filename,
                    "status": "CORRUPT",
                    "is_corrupt": True,
                    "error": "cache_sheets_leads.json root must be dict or list",
                }
            leads_list = parsed.get("leads", []) if isinstance(parsed, dict) else parsed
            if not isinstance(leads_list, list):
                return {
                    "file": filename,
                    "status": "CORRUPT",
                    "is_corrupt": True,
                    "error": "'leads' key must contain a list",
                }

        return {
            "file": filename,
            "status": "VALID",
            "is_corrupt": False,
            "size_bytes": file_size,
        }

    def audit_data_integrity(self) -> Dict[str, Any]:
        """
        Runs comprehensive integrity audit across all critical data stores.
        Detects missing, corrupted, or truncated files.
        """
        results = {}
        corrupt_files = []
        missing_files = []

        for fname in CRITICAL_STORE_FILES:
            audit = self.audit_store_file(fname)
            results[fname] = audit
            if audit["is_corrupt"]:
                corrupt_files.append(fname)
            elif audit["status"] == "MISSING":
                missing_files.append(fname)

        is_healthy = len(corrupt_files) == 0

        # Identity audit on leads if parseable
        identity_audit = {}
        leads_path = os.path.join(self.data_dir, "cache_sheets_leads.json")
        if os.path.exists(leads_path) and "cache_sheets_leads.json" not in corrupt_files:
            try:
                auditor = IdentityIntegrityAuditor(data_dir=self.data_dir)
                identity_audit = auditor.run_identity_audit()
            except Exception as e:
                identity_audit = {"status": "ERROR", "error": str(e)}

        return {
            "status": "PASS" if is_healthy else "CORRUPT",
            "is_healthy": is_healthy,
            "corrupt_files_count": len(corrupt_files),
            "corrupt_files": corrupt_files,
            "missing_files_count": len(missing_files),
            "missing_files": missing_files,
            "stores": results,
            "identity_audit": identity_audit,
        }

    def safe_load_store(self, filename: str) -> Any:
        """
        Safely loads a store file. Refuses to load and raises StateCorruptionError
        if the file is corrupted, truncated, or invalid JSON.
        """
        audit = self.audit_store_file(filename)
        if audit["is_corrupt"]:
            raise StateCorruptionError(f"Refusing to load corrupted store '{filename}': {audit['error']}")

        filepath = os.path.join(self.data_dir, filename)
        if not os.path.exists(filepath):
            return None

        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def reconcile_sheets_and_local_cache(
        sheets_leads: List[Dict[str, Any]],
        local_leads: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Reconciles authoritative Google Sheets CRM leads against local cached state.
        Google Sheets remains the canonical CRM authority.

        Detects:
          - missing_local_lead: In Sheets, missing in local cache
          - unexpected_local_lead: In local cache, missing in Sheets
          - status_mismatch: Disagreement in lead_status / outreach_status
          - canonical_id_mismatch: ID mismatch or non-canonical format
          - historical_state_mismatch: Disagreement on protected historical state
        """
        sheets_by_id = {str(l.get("lead_id", "")).strip(): l for l in sheets_leads if l.get("lead_id")}
        local_by_id = {str(l.get("lead_id", "")).strip(): l for l in local_leads if l.get("lead_id")}

        missing_local = []
        unexpected_local = []
        status_mismatches = []
        canonical_id_mismatches = []
        historical_mismatches = []

        # 1. Inspect leads present in Sheets (CRM Authority)
        for lid, s_lead in sheets_by_id.items():
            if not CANONICAL_LEAD_ID_REGEX.match(lid):
                canonical_id_mismatches.append({
                    "lead_id": lid,
                    "source": "sheets",
                    "reason": f"Non-canonical ID format '{lid}'",
                })

            if lid not in local_by_id:
                missing_local.append({
                    "lead_id": lid,
                    "company_name": s_lead.get("company_name", ""),
                    "sheets_status": s_lead.get("lead_status", ""),
                })
            else:
                l_lead = local_by_id[lid]
                # Status comparison
                s_stat = str(s_lead.get("lead_status") or s_lead.get("outreach_status") or "").upper()
                l_stat = str(l_lead.get("lead_status") or l_lead.get("outreach_status") or "").upper()
                if s_stat and l_stat and s_stat != l_stat:
                    status_mismatches.append({
                        "lead_id": lid,
                        "sheets_status": s_stat,
                        "local_status": l_stat,
                        "authority": "Google Sheets authority overrides local cache",
                    })

                # Protected historical state comparison
                s_is_protected = any(p in s_stat for p in PROTECTED_HISTORICAL_STATES)
                l_is_protected = any(p in l_stat for p in PROTECTED_HISTORICAL_STATES)
                if s_is_protected and not l_is_protected:
                    historical_mismatches.append({
                        "lead_id": lid,
                        "sheets_protected_status": s_stat,
                        "local_unprotected_status": l_stat,
                        "violation": "Local cache failed to reflect Sheets protected historical state",
                    })

        # 2. Inspect leads present in local cache but missing in Sheets
        for lid, l_lead in local_by_id.items():
            if not CANONICAL_LEAD_ID_REGEX.match(lid):
                canonical_id_mismatches.append({
                    "lead_id": lid,
                    "source": "local_cache",
                    "reason": f"Non-canonical ID format '{lid}'",
                })

            if lid not in sheets_by_id:
                unexpected_local.append({
                    "lead_id": lid,
                    "company_name": l_lead.get("company_name", ""),
                    "local_status": l_lead.get("lead_status", ""),
                    "action_required": "Must sync to Sheets or prune unconfirmed local record",
                })

        total_discrepancies = (
            len(missing_local)
            + len(unexpected_local)
            + len(status_mismatches)
            + len(canonical_id_mismatches)
            + len(historical_mismatches)
        )

        return {
            "status": "PASS" if total_discrepancies == 0 else "MISMATCH",
            "sheets_lead_count": len(sheets_by_id),
            "local_lead_count": len(local_by_id),
            "total_discrepancies": total_discrepancies,
            "missing_local_leads": missing_local,
            "unexpected_local_leads": unexpected_local,
            "status_mismatches": status_mismatches,
            "canonical_id_mismatches": canonical_id_mismatches,
            "historical_state_mismatches": historical_mismatches,
        }

    @staticmethod
    def verify_protected_outreach_safety(
        baseline_leads: List[Dict[str, Any]],
        evaluated_leads: List[Dict[str, Any]],
        message_history: Optional[Any] = None,
        suppression_list: Optional[Any] = None,
    ) -> Tuple[bool, List[str]]:
        """
        Verifies that protected historical outreach states (SENT, BOUNCED, SUPPRESSED)
        remain 100% intact after any restore, refresh, or recovery operation.
        Guarantees that a historical record never becomes a fresh outreach opportunity.
        """
        violations = []
        eval_by_id = {str(l.get("lead_id", "")).strip(): l for l in evaluated_leads if l.get("lead_id")}

        for b_lead in baseline_leads:
            lid = str(b_lead.get("lead_id", "")).strip()
            if not lid:
                continue

            b_stat = str(b_lead.get("lead_status") or b_lead.get("outreach_status") or "").upper()
            b_qual = str(b_lead.get("qualification_state", "")).upper()

            is_sent = "SENT" in b_stat
            is_bounced = "BOUNCED" in b_stat
            is_suppressed = "SUPPRESSED" in b_stat or "SUPPRESSED" in b_qual

            if lid not in eval_by_id:
                if is_sent or is_bounced or is_suppressed:
                    violations.append(f"Protected lead '{lid}' ({b_stat}) was completely lost in evaluated state.")
                continue

            e_lead = eval_by_id[lid]
            e_stat = str(e_lead.get("lead_status") or e_lead.get("outreach_status") or "").upper()
            e_qual = str(e_lead.get("qualification_state", "")).upper()

            if is_sent and "SENT" not in e_stat:
                violations.append(f"SENT lead '{lid}' status was improperly altered to '{e_stat}'.")

            if is_bounced and "BOUNCED" not in e_stat:
                violations.append(f"BOUNCED lead '{lid}' status was improperly altered to '{e_stat}'.")

            if is_suppressed and ("SUPPRESSED" not in e_stat and "SUPPRESSED" not in e_qual):
                violations.append(f"SUPPRESSED lead '{lid}' lost suppression status (now stat='{e_stat}', qual='{e_qual}').")

            # Check that historical lead was NOT promoted to active sendable
            if (is_sent or is_bounced or is_suppressed) and e_qual == "OUTREACH_READY":
                violations.append(
                    f"Historical protected lead '{lid}' ({b_stat}) was dangerously promoted to OUTREACH_READY!"
                )

        # Message history immutability check
        if message_history:
            msg_count = len(message_history) if isinstance(message_history, (list, dict)) else 0
            if msg_count == 0 and any("SENT" in str(l.get("outreach_status", "")) for l in baseline_leads):
                violations.append("Message history is empty despite presence of SENT leads.")

        # Suppression list immutability check
        if suppression_list is not None:
            supp_count = len(suppression_list) if isinstance(suppression_list, (list, dict)) else 0
            if supp_count == 0 and any("SUPPRESSED" in str(l.get("lead_status", "")) for l in baseline_leads):
                violations.append("Suppression list is empty despite presence of SUPPRESSED leads.")

        return len(violations) == 0, violations

    @staticmethod
    def verify_canonical_id_stability(
        original_leads: List[Dict[str, Any]],
        updated_leads: List[Dict[str, Any]],
    ) -> Tuple[bool, List[str]]:
        """
        Verifies that factual changes (phone, website, social, reviews) did not
        mutate canonical lead IDs for the same business entities.
        """
        violations = []
        orig_by_name = {
            re.sub(r"[^a-z0-9]", "", str(l.get("company_name", "")).lower()): l.get("lead_id")
            for l in original_leads if l.get("company_name")
        }

        for u_lead in updated_leads:
            name = u_lead.get("company_name", "")
            norm_name = re.sub(r"[^a-z0-9]", "", str(name).lower())
            u_lid = u_lead.get("lead_id")

            if norm_name in orig_by_name:
                orig_lid = orig_by_name[norm_name]
                if orig_lid and u_lid != orig_lid:
                    violations.append(
                        f"Business entity '{name}' canonical lead_id changed from '{orig_lid}' to '{u_lid}'!"
                    )

        return len(violations) == 0, violations
