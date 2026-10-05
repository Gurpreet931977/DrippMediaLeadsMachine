"""
lib/system/reconciliation_engine.py
===================================
State Reconciliation Engine for Dripp Media International Lead System.
Cross-audits CRM state, outreach event streams, commercial records, and proposals
to detect multi-store contradictions.

CRITICAL INVARIANT:
  Do NOT silently repair contradictions.
  Flag contradictions with structured severity, expected relationships, and recommended actions.
"""

import os
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Set

logger = logging.getLogger("ReconciliationEngine")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


@dataclass
class ReconciliationIssue:
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    entity: str    # lead_id or proposal_id
    field: str
    current_value: Any
    expected_relationship: str
    recommended_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ReconciliationEngine:
    """
    Cross-system invariant auditor.
    Detects mismatches between CRM rows, message events, commercial pipelines, and proposals.
    """

    def __init__(
        self,
        leads_path: Optional[str] = None,
        commercial_records_path: Optional[str] = None,
        proposals_path: Optional[str] = None,
        message_history_path: Optional[str] = None,
        outcomes_path: Optional[str] = None,
        suppression_path: Optional[str] = None,
        gate_path: Optional[str] = None,
        timelines_path: Optional[str] = None,
        data_dir: Optional[str] = None,
    ):
        base_dir = data_dir or DATA_DIR
        self.data_dir = base_dir
        self.leads_path = leads_path or os.path.join(base_dir, "cache_sheets_leads.json")
        self.commercial_records_path = commercial_records_path or os.path.join(base_dir, "commercial_records.json")
        self.proposals_path = proposals_path or os.path.join(base_dir, "commercial_proposals.json")
        self.message_history_path = message_history_path or os.path.join(base_dir, "message_history.json")
        self.outcomes_path = outcomes_path or os.path.join(base_dir, "outreach_outcomes.json")
        self.suppression_path = suppression_path or os.path.join(base_dir, "suppression_list.json")
        self.gate_path = gate_path or os.path.join(base_dir, "execution_gate.json")
        self.timelines_path = timelines_path or os.path.join(base_dir, "lead_timelines.json")

    def _load_json(self, path: str, default: Any = None) -> Any:
        if not os.path.exists(path):
            return default if default is not None else {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error loading {path}: {e}")
            return default if default is not None else {}

    def run_reconciliation(
        self,
        leads_data: Optional[List[Dict[str, Any]]] = None,
        commercial_records: Optional[Dict[str, Any]] = None,
        proposals_data: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Executes cross-system audit across all stores and returns structured reconciliation report.
        """
        issues: List[ReconciliationIssue] = []

        # 1. Load data
        if leads_data is None:
            raw_leads = self._load_json(self.leads_path, {"leads": []})
            leads = raw_leads.get("leads", []) if isinstance(raw_leads, dict) else (raw_leads if isinstance(raw_leads, list) else [])
        else:
            leads = leads_data

        commercial = commercial_records if commercial_records is not None else self._load_json(self.commercial_records_path, {})
        proposals = proposals_data if proposals_data is not None else self._load_json(self.proposals_path, [])
        message_history = self._load_json(self.message_history_path, {})
        outcomes = self._load_json(self.outcomes_path, [])
        # Also check outcome_tracking.json if available
        tracking_path = os.path.join(DATA_DIR, "outcome_tracking.json")
        outcomes_alt = self._load_json(tracking_path, {})
        suppression = self._load_json(self.suppression_path, [])
        gate = self._load_json(self.gate_path, {})
        timelines = self._load_json(self.timelines_path, {})

        # Build index sets for fast cross-referencing
        sent_lead_ids: Set[str] = set()
        if isinstance(message_history, dict):
            for lid, msgs in message_history.items():
                if isinstance(msgs, list):
                    for m in msgs:
                        if isinstance(m, dict) and (m.get("status") == "SENT" or m.get("direction") == "OUTBOUND" or m.get("channel")):
                            sent_lead_ids.add(lid)
                elif isinstance(msgs, dict):
                    if msgs.get("status") == "SENT" or msgs.get("direction") == "OUTBOUND":
                        sent_lead_ids.add(lid)
        elif isinstance(message_history, list):
            for msg in message_history:
                lid = msg.get("lead_id")
                if lid and (msg.get("status") == "SENT" or msg.get("direction") == "OUTBOUND" or "status" not in msg):
                    sent_lead_ids.add(lid)

        outreach_attempted_ids: Set[str] = set()
        if isinstance(outcomes, list):
            for out in outcomes:
                lid = out.get("lead_id")
                if lid:
                    outreach_attempted_ids.add(lid)
        elif isinstance(outcomes, dict):
            outreach_attempted_ids.update(outcomes.keys())

        if isinstance(outcomes_alt, dict):
            outreach_attempted_ids.update(outcomes_alt.keys())
        elif isinstance(outcomes_alt, list):
            for out in outcomes_alt:
                lid = out.get("lead_id") if isinstance(out, dict) else None
                if lid:
                    outreach_attempted_ids.add(lid)

        outreach_attempted_ids.update(sent_lead_ids)

        # Timelines also reflect outreach events
        for lid, t_events in (timelines if isinstance(timelines, dict) else {}).items():
            for evt in t_events:
                etype = str(evt.get("event_type", "")).upper()
                if "SENT" in etype or "CALL" in etype or "OUTREACH" in etype:
                    outreach_attempted_ids.add(lid)

        suppressed_lead_ids: Set[str] = set()
        if isinstance(suppression, list):
            for s in suppression:
                if isinstance(s, dict) and s.get("lead_id"):
                    suppressed_lead_ids.add(s["lead_id"])
                elif isinstance(s, str):
                    suppressed_lead_ids.add(s)
        elif isinstance(suppression, dict):
            suppressed_lead_ids.update(suppression.keys())

        proposals_by_id: Dict[str, Dict[str, Any]] = {}
        proposals_by_lead: Dict[str, List[Dict[str, Any]]] = {}
        for p in (proposals if isinstance(proposals, list) else []):
            pid = p.get("proposal_id")
            plid = p.get("lead_id")
            if pid:
                proposals_by_id[pid] = p
            if plid:
                proposals_by_lead.setdefault(plid, []).append(p)

        # ---------------------------------------------------------------------
        # Rule 1: CRM says SENT, but no authoritative send event exists
        # ---------------------------------------------------------------------
        for lead in leads:
            lid = lead.get("lead_id")
            status = str(lead.get("lead_status", "")).upper()
            outreach_status = str(lead.get("outreach_status", "")).upper()

            if (status == "SENT" or outreach_status == "SENT") and lid not in sent_lead_ids:
                issues.append(
                    ReconciliationIssue(
                        severity="CRITICAL",
                        entity=lid,
                        field="lead_status / outreach_status",
                        current_value=f"lead_status={status}, outreach_status={outreach_status}",
                        expected_relationship="Lead marked SENT must have a matching send event in message_history.json.",
                        recommended_action="Investigate if message was delivered outside the standard pipeline or revert status to READY_FOR_REVIEW.",
                    )
                )

        # ---------------------------------------------------------------------
        # Rule 2: CRM says CONTACTED, but no outreach attempt exists
        # ---------------------------------------------------------------------
        for lead in leads:
            lid = lead.get("lead_id")
            status = str(lead.get("lead_status", "")).upper()
            if status == "CONTACTED" and lid not in outreach_attempted_ids:
                issues.append(
                    ReconciliationIssue(
                        severity="HIGH",
                        entity=lid,
                        field="lead_status",
                        current_value=status,
                        expected_relationship="Lead marked CONTACTED must have recorded attempt in outreach_outcomes.json or message_history.json.",
                        recommended_action="Log corresponding outcome attempt or revert lead status to NOT_CONTACTED.",
                    )
                )

        # ---------------------------------------------------------------------
        # Rule 3: Commercial stage says PROPOSAL_SENT / PROPOSAL_ACCEPTED, but no proposal exists
        # ---------------------------------------------------------------------
        for lid, rec in commercial.items():
            c_stage = str(rec.get("commercial_stage", "")).upper()
            if c_stage in ("PROPOSAL_SENT", "PROPOSAL_ACCEPTED", "NEGOTIATING"):
                lead_props = proposals_by_lead.get(lid, [])
                if not lead_props:
                    issues.append(
                        ReconciliationIssue(
                            severity="CRITICAL",
                            entity=lid,
                            field=f"commercial_stage ({c_stage})",
                            current_value=c_stage,
                            expected_relationship=f"Lead in stage '{c_stage}' must have an authoritative proposal record in commercial_proposals.json.",
                            recommended_action=f"Create formal proposal in Proposal Workspace or revert commercial stage to PREVIEW_DELIVERED.",
                        )
                    )

        # ---------------------------------------------------------------------
        # Rule 4: Proposal says SENT, but no operator confirmation exists
        # ---------------------------------------------------------------------
        for p in (proposals if isinstance(proposals, list) else []):
            pid = p.get("proposal_id", "UNKNOWN")
            p_state = str(p.get("state", p.get("proposal_status", ""))).upper()
            if p_state == "SENT":
                sent_at = p.get("sent_at")
                sent_by = p.get("sent_by") or p.get("operator")
                operator_confirmed = p.get("operator_confirmed", True if sent_by else False)
                if not sent_at or not sent_by or not operator_confirmed:
                    issues.append(
                        ReconciliationIssue(
                            severity="HIGH",
                            entity=pid,
                            field="sent_at / sent_by / operator_confirmed",
                            current_value=f"sent_at={sent_at}, sent_by={sent_by}, operator_confirmed={operator_confirmed}",
                            expected_relationship="Proposals marked SENT must record timestamp, operator ID, and explicit operator confirmation.",
                            recommended_action="Revert proposal to READY_TO_SEND until human operator confirms delivery.",
                        )
                    )

        # ---------------------------------------------------------------------
        # Rule 5: Qualification says OUTREACH_READY, but activation says BLOCKED or SUPPRESSED
        # ---------------------------------------------------------------------
        for lead in leads:
            lid = lead.get("lead_id")
            q_state = str(lead.get("qualification_state", "")).upper()
            if q_state == "OUTREACH_READY":
                if lid in suppressed_lead_ids:
                    issues.append(
                        ReconciliationIssue(
                            severity="HIGH",
                            entity=lid,
                            field="qualification_state vs suppression",
                            current_value="OUTREACH_READY vs SUPPRESSED",
                            expected_relationship="Suppressed leads must not remain in OUTREACH_READY status.",
                            recommended_action="Demote lead qualification_state to SUPPRESSED or remove from suppression list if erroneous.",
                        )
                    )

        # ---------------------------------------------------------------------
        # Rule 6: Deal says WON / CLOSED_WON, but missing required closing data
        # ---------------------------------------------------------------------
        for lid, rec in commercial.items():
            c_stage = str(rec.get("commercial_stage", "")).upper()
            if c_stage in ("CLOSED_WON", "WON"):
                deal_won = rec.get("deal_won") or {}
                agreed_value = rec.get("final_agreed_value", deal_won.get("agreed_value", 0.0))
                payment_terms = deal_won.get("payment_terms") or rec.get("payment_terms")
                operator_confirmed = deal_won.get("operator_confirmed", rec.get("operator_confirmed", False))
                if agreed_value <= 0 or not payment_terms or not operator_confirmed:
                    issues.append(
                        ReconciliationIssue(
                            severity="CRITICAL",
                            entity=lid,
                            field="final_agreed_value / deal_won closing data",
                            current_value=f"agreed_value={agreed_value}, payment_terms={payment_terms}, operator_confirmed={operator_confirmed}",
                            expected_relationship="Deals marked WON require agreed_value > 0, explicit payment_terms, and operator confirmation.",
                            recommended_action="Complete formal deal closing fields via /api/commercial/close or revert stage to NEGOTIATING.",
                        )
                    )

        # ---------------------------------------------------------------------
        # Rule 7: Deal says LOST / CLOSED_LOST, but missing lost reason
        # ---------------------------------------------------------------------
        for lid, rec in commercial.items():
            c_stage = str(rec.get("commercial_stage", "")).upper()
            if c_stage in ("CLOSED_LOST", "LOST"):
                deal_lost = rec.get("deal_lost") or {}
                reason = deal_lost.get("reason")
                if not reason or not str(reason).strip():
                    issues.append(
                        ReconciliationIssue(
                            severity="MEDIUM",
                            entity=lid,
                            field="deal_lost.reason",
                            current_value=reason,
                            expected_relationship="Deals marked LOST must specify a canonical lost reason.",
                            recommended_action="Record loss reason via /api/commercial/close.",
                        )
                    )

        # ---------------------------------------------------------------------
        # Rule 8: Proposal package pricing mathematical inconsistency
        # ---------------------------------------------------------------------
        for p in (proposals if isinstance(proposals, list) else []):
            pid = p.get("proposal_id", "UNKNOWN")
            subtotal = float(p.get("subtotal", 0.0))
            discount = float(p.get("discount", 0.0))
            total = float(p.get("total", subtotal - discount))
            expected_total = max(0.0, round(subtotal - discount, 2))
            if round(total, 2) != expected_total or total < 0:
                issues.append(
                    ReconciliationIssue(
                        severity="HIGH",
                        entity=pid,
                        field="pricing: total vs (subtotal - discount)",
                        current_value=f"subtotal={subtotal}, discount={discount}, total={total}",
                        expected_relationship=f"total == subtotal - discount (expected {expected_total}) and cannot be negative.",
                        recommended_action="Recalculate proposal pricing to ensure strict mathematical correctness.",
                    )
                )

        by_severity = {
            "CRITICAL": sum(1 for i in issues if i.severity == "CRITICAL"),
            "HIGH": sum(1 for i in issues if i.severity == "HIGH"),
            "MEDIUM": sum(1 for i in issues if i.severity == "MEDIUM"),
            "LOW": sum(1 for i in issues if i.severity == "LOW"),
        }

        if by_severity["CRITICAL"] > 0:
            status = "FAIL"
        elif by_severity["HIGH"] > 0 or by_severity["MEDIUM"] > 0:
            status = "WARN"
        else:
            status = "PASS"

        return {
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "records_audited": len(leads) + len(commercial) + len(proposals),
            "issues_count": len(issues),
            "by_severity": by_severity,
            "issues": [i.to_dict() for i in issues],
        }
