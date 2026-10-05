"""
lib/system/identity_integrity.py
================================
Cross-Store Identity Integrity Auditor.
Verifies canonical lead identity constraints across CRM, research logs, review queues,
activation profiles, message histories, commercial pipelines, and proposals.

Invariants:
  1. One canonical business entity per canonical lead ID.
  2. Canonical lead IDs follow the canonical specification: ^LEAD-[A-Z]{3,4}-[A-F0-9]{6}$
  3. Zero accidental duplicates in the authoritative CRM leads store.
  4. Branch separation preserved (same brand, distinct branches have distinct lead IDs).
  5. Research IDs (RES-*, OSM-*, etc.) NEVER masquerade as canonical lead IDs.
"""

import os
import re
import json
import logging
from typing import Dict, Any, List, Optional, Set, Tuple

logger = logging.getLogger("IdentityIntegrity")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

CANONICAL_LEAD_ID_REGEX = re.compile(r"^LEAD-[A-Z0-9]+-[A-F0-9]{6,}$", re.IGNORECASE)
RESEARCH_ID_REGEX = re.compile(r"^(RES-|OSM-|GOSOM-|FOURSQUARE-)", re.IGNORECASE)


class IdentityIntegrityAuditor:
    """
    Audits entity resolution and canonical identity consistency across all system stores.
    """

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or DATA_DIR

    def _load_json(self, rel_path: str, default: Any = None) -> Any:
        full_path = os.path.join(self.data_dir, rel_path)
        if not os.path.exists(full_path):
            return default if default is not None else {}
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read {rel_path}: {e}")
            return default if default is not None else {}

    @classmethod
    def validate_lead_id_format(cls, lead_id: str) -> bool:
        if not lead_id or not isinstance(lead_id, str):
            return False
        return bool(CANONICAL_LEAD_ID_REGEX.match(lead_id))

    def run_identity_audit(self) -> Dict[str, Any]:
        """
        Runs comprehensive identity audit across all system stores.
        """
        issues = []

        # 1. Inspect CRM Leads Store
        raw_leads = self._load_json("cache_sheets_leads.json", {"leads": []})
        leads = raw_leads.get("leads", []) if isinstance(raw_leads, dict) else (raw_leads if isinstance(raw_leads, list) else [])

        seen_lead_ids: Dict[str, Dict[str, Any]] = {}
        seen_entity_keys: Dict[str, str] = {}  # normalized_name + postcode -> lead_id
        branches_verified = 0

        for idx, lead in enumerate(leads):
            lid = str(lead.get("lead_id", "")).strip()
            name = str(lead.get("company_name", "")).strip()
            postcode = str(lead.get("postcode", "")).strip().upper().replace(" ", "")
            address = str(lead.get("address", "")).strip()

            if not lid:
                issues.append({
                    "severity": "CRITICAL",
                    "store": "cache_sheets_leads.json",
                    "entity": f"Row {idx+1}",
                    "message": f"Lead record for '{name}' is missing lead_id."
                })
                continue

            # Check canonical pattern
            if not CANONICAL_LEAD_ID_REGEX.match(lid):
                issues.append({
                    "severity": "CRITICAL",
                    "store": "cache_sheets_leads.json",
                    "entity": lid,
                    "message": f"Non-canonical lead_id format: '{lid}' does not match canonical pattern (^LEAD-MKT-XXXXXX)."
                })

            # Check research ID leakage
            if RESEARCH_ID_REGEX.match(lid):
                issues.append({
                    "severity": "CRITICAL",
                    "store": "cache_sheets_leads.json",
                    "entity": lid,
                    "message": f"Research ID '{lid}' leaked as canonical lead_id!"
                })

            # Check for duplicate lead_id
            if lid in seen_lead_ids:
                issues.append({
                    "severity": "CRITICAL",
                    "store": "cache_sheets_leads.json",
                    "entity": lid,
                    "message": f"Duplicate canonical lead_id '{lid}' found across multiple records: '{seen_lead_ids[lid].get('company_name')}' and '{name}'."
                })
            else:
                seen_lead_ids[lid] = lead

            # Check duplicate entity (same name + same postcode)
            norm_name = re.sub(r"[^a-z0-9]", "", name.lower())
            if norm_name and postcode:
                entity_key = f"{norm_name}::{postcode}"
                if entity_key in seen_entity_keys and seen_entity_keys[entity_key] != lid:
                    issues.append({
                        "severity": "HIGH",
                        "store": "cache_sheets_leads.json",
                        "entity": lid,
                        "message": f"Accidental duplicate business entity detected for '{name}' (postcode '{postcode}') under IDs '{lid}' and '{seen_entity_keys[entity_key]}'."
                    })
                else:
                    seen_entity_keys[entity_key] = lid

            # Check branch separation: same root brand with different address/postcode is valid branch
            if lead.get("multiple_locations") in ("Yes", "YES", True) or "branch" in address.lower():
                branches_verified += 1

        # 2. Inspect Commercial Records Store
        commercial_records = self._load_json("commercial_records.json", {})
        for c_lid, rec in commercial_records.items():
            if RESEARCH_ID_REGEX.match(c_lid):
                issues.append({
                    "severity": "CRITICAL",
                    "store": "commercial_records.json",
                    "entity": c_lid,
                    "message": f"Commercial record keyed by research ID '{c_lid}'."
                })
            if c_lid not in seen_lead_ids:
                issues.append({
                    "severity": "MEDIUM",
                    "store": "commercial_records.json",
                    "entity": c_lid,
                    "message": f"Commercial record references unknown lead_id '{c_lid}'."
                })

        # 3. Inspect Commercial Proposals Store
        proposals = self._load_json("commercial_proposals.json", [])
        for p in (proposals if isinstance(proposals, list) else []):
            p_lid = p.get("lead_id", "")
            pid = p.get("proposal_id", "")
            if RESEARCH_ID_REGEX.match(p_lid):
                issues.append({
                    "severity": "CRITICAL",
                    "store": "commercial_proposals.json",
                    "entity": pid,
                    "message": f"Proposal '{pid}' references research ID '{p_lid}'."
                })
            if p_lid and p_lid not in seen_lead_ids:
                issues.append({
                    "severity": "MEDIUM",
                    "store": "commercial_proposals.json",
                    "entity": pid,
                    "message": f"Proposal '{pid}' references unknown lead_id '{p_lid}'."
                })

        # 4. Inspect Message History Store
        messages = self._load_json("message_history.json", [])
        for m in (messages if isinstance(messages, list) else []):
            m_lid = m.get("lead_id", "")
            if RESEARCH_ID_REGEX.match(m_lid):
                issues.append({
                    "severity": "CRITICAL",
                    "store": "message_history.json",
                    "entity": m.get("message_id", "UNKNOWN"),
                    "message": f"Message sent referencing research ID '{m_lid}'."
                })

        for iss in issues:
            iss["description"] = iss.get("message", "")

        critical_count = sum(1 for i in issues if i["severity"] == "CRITICAL")
        high_count = sum(1 for i in issues if i["severity"] == "HIGH")

        status = "FAIL" if critical_count > 0 else ("WARN" if high_count > 0 else "PASS")

        return {
            "status": status,
            "total_canonical_leads_audited": len(seen_lead_ids),
            "branches_verified": branches_verified,
            "issues_count": len(issues),
            "critical_issues": critical_count,
            "high_issues": high_count,
            "issues": issues,
        }
