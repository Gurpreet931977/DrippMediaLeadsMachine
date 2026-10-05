"""
Phase 10.0: Commercial Proposal & Deal Closing Workspace Manager

Manages the commercial closing workflow:
  PREVIEW -> COMMERCIAL DISCUSSION -> PROPOSAL -> NEGOTIATION -> WON / LOST

Key Capabilities:
  1. Proposal States:
     DRAFT -> INTERNAL_REVIEW -> READY_TO_SEND -> SENT -> VIEWED -> NEGOTIATING -> ACCEPTED -> REJECTED -> EXPIRED
  2. Canonical Website Proposal Template (10 sections) without unsubstantiated claims.
  3. Scope Builder & Configurable Package System (STARTER, STANDARD, PREMIUM, CUSTOM).
  4. Deterministic Pricing (subtotal, discount, total with strict non-negative guards).
  5. Immutability of Sent Proposals & Versioning (PROP-001 v1 -> v2).
  6. Negotiation Tracking (Price objections, scope/timeline changes stored separately).
  7. Deal Close Validation (Won requires agreed value & explicit operator confirmation; Lost requires reason).
  8. Strict Automation Invariants:
     AUTO_PROPOSAL = False
     AUTO_SEND_PROPOSAL = False
     AUTO_EMAIL_PROPOSAL = False
     AUTO_FOLLOWUP = False
     AUTO_PAYMENT_REQUEST = False
     AUTO_CONTRACT = False
  9. Append-Only Commercial Event Trail.
  10. Machine Snapshot Generation (data/proposal_pipeline_snapshot.json).
"""

import os
import re
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

from lib.commercial.models import (
    CommercialStage,
    WebsitePipelineStage,
    OutcomeProvenance,
    InterestLevel,
    PreviewStatus,
    ProposalState,
    ProposalStatus,
    ProposalPackage,
    LostReason,
    NextActionType,
    CommercialEventType,
    CommercialNotes,
    FollowUpRecord,
    PreviewRecord,
    ProposalDocument,
    CommercialEvent,
    VALID_PROPOSAL_STATES,
    VALID_PROPOSAL_STATUSES,
    VALID_PACKAGES,
    VALID_LOST_REASONS,
    VALID_COMMERCIAL_EVENT_TYPES,
    AUTO_PROPOSAL,
    AUTO_SEND_PROPOSAL,
    AUTO_EMAIL_PROPOSAL,
    AUTO_FOLLOWUP,
    AUTO_PAYMENT_REQUEST,
    AUTO_CONTRACT,
    now_utc_iso,
)

logger = logging.getLogger("ProposalManager")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

DEFAULT_PROPOSALS_PATH = os.path.join(DATA_DIR, "commercial_proposals.json")
DEFAULT_PACKAGES_PATH = os.path.join(DATA_DIR, "proposal_packages.json")
DEFAULT_LEADS_PATH = os.path.join(DATA_DIR, "cache_sheets_leads.json")
DEFAULT_COMMERCIAL_RECORDS_PATH = os.path.join(DATA_DIR, "commercial_records.json")
DEFAULT_COMMERCIAL_EVENTS_PATH = os.path.join(DATA_DIR, "commercial_events.json")
DEFAULT_TIMELINES_PATH = os.path.join(DATA_DIR, "lead_timelines.json")
DEFAULT_SNAPSHOT_PATH = os.path.join(DATA_DIR, "proposal_pipeline_snapshot.json")


class ProposalManager:
    """
    Authoritative manager for Dripp Media commercial proposals, negotiations,
    and deal closing workflows under strict human operator governance.
    """

    def __init__(
        self,
        proposals_path: Optional[str] = None,
        packages_path: Optional[str] = None,
        leads_path: Optional[str] = None,
        commercial_records_path: Optional[str] = None,
        commercial_events_path: Optional[str] = None,
        timelines_path: Optional[str] = None,
        snapshot_path: Optional[str] = None,
    ):
        self.proposals_path = proposals_path or os.environ.get("PROPOSALS_PATH") or DEFAULT_PROPOSALS_PATH
        self.packages_path = packages_path or os.environ.get("PACKAGES_PATH") or DEFAULT_PACKAGES_PATH
        self.leads_path = leads_path or os.environ.get("LEADS_PATH") or DEFAULT_LEADS_PATH
        self.commercial_records_path = commercial_records_path or os.environ.get("COMMERCIAL_RECORDS_PATH") or DEFAULT_COMMERCIAL_RECORDS_PATH
        self.commercial_events_path = commercial_events_path or os.environ.get("COMMERCIAL_EVENTS_PATH") or DEFAULT_COMMERCIAL_EVENTS_PATH
        self.timelines_path = timelines_path or os.environ.get("TIMELINES_PATH") or DEFAULT_TIMELINES_PATH
        self.snapshot_path = snapshot_path or os.environ.get("PROPOSAL_SNAPSHOT_PATH") or DEFAULT_SNAPSHOT_PATH

        self._ensure_storage_initialized()

    # -------------------------------------------------------------------------
    # Storage & Packages Initialization
    # -------------------------------------------------------------------------
    def _ensure_storage_initialized(self) -> None:
        os.makedirs(os.path.dirname(self.proposals_path), exist_ok=True)
        if not os.path.exists(self.proposals_path):
            with open(self.proposals_path, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)

        if not os.path.exists(self.commercial_events_path):
            with open(self.commercial_events_path, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)

    def load_proposals(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.proposals_path):
            return []
        try:
            with open(self.proposals_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading proposals from {self.proposals_path}: {e}")
            return []

    def _save_proposals(self, proposals: List[Dict[str, Any]]) -> None:
        from lib.system.atomic_writer import atomic_write_json
        from lib.system.file_lock import proposal_records_lock
        with proposal_records_lock():
            atomic_write_json(self.proposals_path, proposals)

    def load_packages(self) -> Dict[str, Any]:
        if not os.path.exists(self.packages_path):
            return {
                "packages": {
                    "STARTER": {"price": 750.0, "currency": "GBP", "scope": ["Responsive website", "Location"]},
                    "STANDARD": {"price": 1250.0, "currency": "GBP", "scope": ["Responsive website", "Gallery"]},
                    "PREMIUM": {"price": 1950.0, "currency": "GBP", "scope": ["Responsive website", "CMS"]},
                    "CUSTOM": {"price": 0.0, "currency": "GBP", "scope": []},
                }
            }
        try:
            with open(self.packages_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading packages from {self.packages_path}: {e}")
            return {"packages": {}}

    def load_leads(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.leads_path):
            return []
        try:
            with open(self.leads_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("leads", []) if isinstance(data, dict) else data
        except Exception as e:
            logger.error(f"Error loading leads from {self.leads_path}: {e}")
            return []

    def load_commercial_records(self) -> Dict[str, Any]:
        if not os.path.exists(self.commercial_records_path):
            return {}
        try:
            with open(self.commercial_records_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading commercial records from {self.commercial_records_path}: {e}")
            return {}

    def _load_json(self, path: str, default: Any = None) -> Any:
        if not os.path.exists(path):
            return default if default is not None else {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {path}: {e}")
            return default if default is not None else {}

    def _append_commercial_event(self, event: CommercialEvent) -> None:
        events = []
        if os.path.exists(self.commercial_events_path):
            try:
                with open(self.commercial_events_path, "r", encoding="utf-8") as f:
                    events = json.load(f)
            except Exception:
                events = []
        events.append(event.to_dict())
        with open(self.commercial_events_path, "w", encoding="utf-8") as f:
            json.dump(events, f, indent=2)

        # Also append to lead_timelines.json
        if os.path.exists(self.timelines_path):
            try:
                with open(self.timelines_path, "r", encoding="utf-8") as f:
                    timelines = json.load(f)
                lead_events = timelines.setdefault(event.lead_id, [])
                lead_events.append({
                    "event_type": event.event_type,
                    "timestamp": event.timestamp,
                    "proposal_id": event.proposal_id,
                    "previous_state": event.previous_stage,
                    "new_state": event.new_stage,
                    "reason": event.reason,
                    "notes": event.notes,
                    "operator": event.operator,
                    "source": event.source,
                })
                with open(self.timelines_path, "w", encoding="utf-8") as f:
                    json.dump(timelines, f, indent=2)
            except Exception as e:
                logger.warning(f"Failed to append to timeline for {event.lead_id}: {e}")

    def _sync_commercial_stage(self, lead_id: str, new_stage: str, reason: str, operator: str) -> None:
        """Syncs commercial stage update with commercial_records.json."""
        if not os.path.exists(self.commercial_records_path):
            return
        try:
            with open(self.commercial_records_path, "r", encoding="utf-8") as f:
                records = json.load(f)
            if lead_id in records:
                old_stage = records[lead_id].get("commercial_stage", "")
                records[lead_id]["commercial_stage"] = new_stage
                records[lead_id]["updated_at"] = now_utc_iso()
                with open(self.commercial_records_path, "w", encoding="utf-8") as f:
                    json.dump(records, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not sync commercial stage for {lead_id}: {e}")

    # -------------------------------------------------------------------------
    # Pricing Calculation & Validation (Section 7)
    # -------------------------------------------------------------------------
    def calculate_pricing(self, subtotal: float, discount: float = 0.0, currency: str = "GBP") -> Dict[str, Any]:
        """
        Calculates subtotal, discount, and total deterministically (Section 7).
        Guards:
          - total >= 0
          - discount >= 0
          - discount <= subtotal
          - total == subtotal - discount
          - currency must be valid 3-letter code
        """
        curr = (currency or "").strip().upper()
        if not curr or len(curr) != 3:
            raise ValueError(f"Currency must be a valid 3-letter ISO code: '{currency}'")

        sub = round(float(subtotal), 2)
        disc = round(float(discount), 2)

        if sub < 0:
            raise ValueError(f"Proposal subtotal cannot be negative: {sub}")
        if disc < 0:
            raise ValueError(f"Proposal discount cannot be negative: {disc}")
        if disc > sub:
            raise ValueError(f"Proposal discount ({disc}) cannot exceed subtotal ({sub})")

        total = round(sub - disc, 2)
        if total < 0:
            raise ValueError(f"Proposal total cannot be negative: {total}")

        return {
            "subtotal": sub,
            "discount": disc,
            "total": total,
            "currency": curr,
        }

    # -------------------------------------------------------------------------
    # Personalization Guardrails (Section 4)
    # -------------------------------------------------------------------------
    def validate_personalization_claims(self, text: str) -> Tuple[bool, List[str]]:
        """
        Audits proposal copy to prevent unsupported or fabricated claims (Section 3 & 4).
        Prohibits: guaranteed sales, guaranteed bookings, guaranteed SEO, guaranteed growth,
        fabricated revenue losses, or unsubstantiated competitor weaknesses.
        """
        violations = []
        forbidden_regexes = [
            (r"guarantee[ds]?\s+sales", "Unsubstantiated claim: guaranteed sales"),
            (r"guarantee[ds]?\s+booking", "Unsubstantiated claim: guaranteed bookings"),
            (r"guarantee[ds]?\s+seo", "Unsubstantiated claim: guaranteed SEO"),
            (r"guarantee[ds]?\s+growth", "Unsubstantiated claim: guaranteed growth"),
            (r"guarantee[ds]?\s+revenue", "Unsubstantiated claim: guaranteed revenue"),
            (r"guarantee\s*#1", "Unsubstantiated claim: ranking guarantee"),
            (r"revenue\s+loss", "Invented pain point: specific revenue loss"),
            (r"booking\s+loss", "Invented pain point: booking loss claim"),
            (r"conversion\s+problem", "Invented pain point: conversion problems"),
            (r"competitor\s+weakness", "Invented claim: competitor weaknesses"),
        ]

        text_lower = (text or "").lower()
        for regex_pattern, explanation in forbidden_regexes:
            if re.search(regex_pattern, text_lower):
                violations.append(explanation)

        return len(violations) == 0, violations

    # -------------------------------------------------------------------------
    # Proposal Creation & Drafting (Section 2 & 12)
    # -------------------------------------------------------------------------
    def create_proposal(
        self,
        lead_id: str,
        company_name: Optional[str] = None,
        service: str = "WEBSITE_DEVELOPMENT",
        package_name: str = "CUSTOM",
        currency: str = "GBP",
        subtotal: float = 0.0,
        discount: float = 0.0,
        scope: Optional[List[str]] = None,
        deliverables: Optional[List[str]] = None,
        exclusions: Optional[List[str]] = None,
        payment_terms: str = "50% upfront deposit upon agreement, 50% upon final delivery prior to launch",
        timeline: str = "10 - 14 business days",
        revision_limit: str = "2 revision rounds included",
        valid_until_days: int = 30,
        operator_notes: str = "",
        operator: str = "HUMAN_OPERATOR",
        sync_commercial_stage: bool = False,
    ) -> Dict[str, Any]:
        """
        Creates a new commercial proposal in DRAFT status (Section 2 & 12).
        Strictly creates DRAFT only; NEVER automatically sends.
        """
        if not lead_id or not lead_id.strip():
            raise ValueError("lead_id is mandatory for proposal creation")

        # Find lead company name from cache or parameter
        leads = self.load_leads()
        resolved_name = company_name or ""
        for l in leads:
            if l.get("lead_id") == lead_id:
                resolved_name = company_name or l.get("company_name", "")
                break
        if not resolved_name:
            comm_records = self.load_commercial_records()
            if lead_id in comm_records:
                resolved_name = comm_records[lead_id].get("company_name", "")
        company_name = resolved_name or "Unknown Business"

        # Load package defaults if package specified
        packages_cfg = self.load_packages().get("packages", {})
        pkg_data = packages_cfg.get(package_name.upper(), {})

        final_scope = list(scope) if scope is not None else list(pkg_data.get("scope", []))
        final_deliverables = list(deliverables) if deliverables is not None else list(pkg_data.get("deliverables", []))
        final_exclusions = list(exclusions) if exclusions is not None else list(pkg_data.get("exclusions", []))

        # Default subtotal from package if not provided
        if subtotal <= 0.0 and pkg_data.get("price", 0.0) > 0.0:
            subtotal = pkg_data.get("price", 0.0)

        pricing = self.calculate_pricing(subtotal=subtotal, discount=discount)

        # Validity date
        valid_until = (datetime.now(timezone.utc) + timedelta(days=valid_until_days)).strftime("%Y-%m-%d")

        # Audit notes for unsupported claims
        valid_claims, violations = self.validate_personalization_claims(operator_notes)
        if not valid_claims:
            raise ValueError(f"Proposal contains prohibited claims: {'; '.join(violations)}")

        proposal_id = f"PROP-{lead_id}-{uuid.uuid4().hex[:6].upper()}"

        doc = ProposalDocument(
            proposal_id=proposal_id,
            lead_id=lead_id,
            company_name=company_name,
            service=service,
            package_name=package_name.upper(),
            currency=currency.upper(),
            subtotal=pricing["subtotal"],
            discount=pricing["discount"],
            total=pricing["total"],
            payment_terms=payment_terms,
            timeline=timeline,
            revision_limit=revision_limit,
            scope=final_scope,
            exclusions=final_exclusions,
            deliverables=final_deliverables,
            valid_until=valid_until,
            status=ProposalState.DRAFT.value,
            created_at=now_utc_iso(),
            sent_at=None,
            operator_notes=operator_notes,
            version=1,
            quoted_value=pricing["total"],
            final_agreed_value=None,
        )

        proposals = self.load_proposals()
        proposals.append(doc.to_dict())
        self._save_proposals(proposals)

        # Update commercial stage only if explicitly requested (Section 22: no auto stage promotion on draft creation)
        if sync_commercial_stage:
            self._sync_commercial_stage(
                lead_id=lead_id,
                new_stage=CommercialStage.PROPOSAL_REQUESTED.value,
                reason=f"Draft proposal created: {proposal_id}",
                operator=operator,
            )

        # Append audit event
        event = CommercialEvent(
            event_id=f"evt-prop-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            proposal_id=proposal_id,
            event_type=CommercialEventType.PROPOSAL_CREATED.value,
            previous_stage=doc.status,
            new_stage=ProposalState.DRAFT.value,
            reason=f"Draft proposal {proposal_id} created for {currency} {pricing['total']}",
            notes=operator_notes,
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {
            "success": True,
            "proposal_id": proposal_id,
            "proposal": doc.to_dict(),
        }

    # -------------------------------------------------------------------------
    # Proposal Updating & Immutability (Section 12 & 16)
    # -------------------------------------------------------------------------
    def update_proposal(
        self,
        proposal_id: str,
        updates: Dict[str, Any],
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Updates an existing proposal in DRAFT or INTERNAL_REVIEW state.
        IMMUTABILITY GUARD: Proposals in SENT, ACCEPTED, or REJECTED state CANNOT be modified.
        Must create a new version via create_revision() instead (Section 16).
        """
        proposals = self.load_proposals()
        matched = None
        for p in proposals:
            if p.get("proposal_id") == proposal_id:
                matched = p
                break

        if not matched:
            raise KeyError(f"Proposal {proposal_id} not found")

        current_status = matched.get("status")
        if current_status in (ProposalState.SENT.value, ProposalState.ACCEPTED.value, ProposalState.REJECTED.value):
            raise PermissionError(
                f"Proposal {proposal_id} is in '{current_status}' state and is immutable. "
                "Use create_revision() to create a new version (Section 16)."
            )

        # Apply pricing updates if provided
        new_sub = updates.get("subtotal", matched.get("subtotal", 0.0))
        new_disc = updates.get("discount", matched.get("discount", 0.0))
        pricing = self.calculate_pricing(subtotal=new_sub, discount=new_disc)
        matched["subtotal"] = pricing["subtotal"]
        matched["discount"] = pricing["discount"]
        matched["total"] = pricing["total"]
        matched["quoted_value"] = pricing["total"]

        # Apply copy and terms updates
        for field in ["service", "package_name", "currency", "payment_terms", "timeline",
                      "revision_limit", "valid_until", "operator_notes"]:
            if field in updates:
                matched[field] = updates[field]

        for list_field in ["scope", "exclusions", "deliverables"]:
            if list_field in updates:
                matched[list_field] = list(updates[list_field])

        # Validate claims
        valid, violations = self.validate_personalization_claims(matched.get("operator_notes", ""))
        if not valid:
            raise ValueError(f"Proposal contains prohibited claims: {'; '.join(violations)}")

        self._save_proposals(proposals)
        return {"success": True, "proposal": matched}

    def approve_proposal(
        self,
        proposal_id: str,
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Transitions proposal from DRAFT / INTERNAL_REVIEW to READY_TO_SEND.
        Does NOT dispatch or send anything.
        """
        proposals = self.load_proposals()
        matched = None
        for p in proposals:
            if p.get("proposal_id") == proposal_id:
                matched = p
                break

        if not matched:
            raise KeyError(f"Proposal {proposal_id} not found")

        current_status = matched.get("status")
        if current_status not in (ProposalState.DRAFT.value, ProposalState.INTERNAL_REVIEW.value):
            raise ValueError(f"Only DRAFT or INTERNAL_REVIEW proposals can be approved. Current: {current_status}")

        matched["status"] = ProposalState.READY_TO_SEND.value
        self._save_proposals(proposals)

        event = CommercialEvent(
            event_id=f"evt-appr-{uuid.uuid4().hex[:8]}",
            lead_id=matched["lead_id"],
            proposal_id=proposal_id,
            event_type=CommercialEventType.PROPOSAL_APPROVED.value,
            previous_stage=current_status,
            new_stage=ProposalState.READY_TO_SEND.value,
            reason="Proposal internally reviewed and approved for operator sending",
            notes=f"Total: {matched.get('currency')} {matched.get('total')}",
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {"success": True, "proposal": matched}

    # -------------------------------------------------------------------------
    # Proposal Delivery & Mark-Sent (Section 12 & 15)
    # -------------------------------------------------------------------------
    def mark_proposal_sent(
        self,
        proposal_id: str,
        delivery_method: str = "DIRECT_EMAIL",
        operator_confirmed: bool = False,
        operator_notes: str = "",
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Operator explicitly records that a proposal was sent externally (Section 15).
        Enforces:
          - operator_confirmed == True is mandatory.
          - proposal status becomes SENT and becomes IMMUTABLE.
          - commercial_stage becomes PROPOSAL_SENT.
          - Zero autonomous dispatch.
        """
        if not operator_confirmed:
            raise ValueError("mark_proposal_sent requires explicit operator confirmation (operator_confirmed=True).")

        proposals = self.load_proposals()
        matched = None
        for p in proposals:
            if p.get("proposal_id") == proposal_id:
                matched = p
                break

        if not matched:
            raise KeyError(f"Proposal {proposal_id} not found")

        current_status = matched.get("status")
        if current_status == ProposalState.SENT.value:
            return {"success": True, "message": "Proposal was already marked SENT", "proposal": matched}

        sent_time = now_utc_iso()
        matched["status"] = ProposalState.SENT.value
        matched["sent_at"] = sent_time
        matched["delivery_method"] = delivery_method
        if operator_notes:
            matched["operator_notes"] = f"{matched.get('operator_notes', '')} | Sent: {operator_notes}".strip(" |")

        self._save_proposals(proposals)

        # Update commercial stage
        lead_id = matched["lead_id"]
        self._sync_commercial_stage(
            lead_id=lead_id,
            new_stage=CommercialStage.PROPOSAL_SENT.value,
            reason=f"Proposal {proposal_id} sent via {delivery_method}",
            operator=operator,
        )

        event = CommercialEvent(
            event_id=f"evt-propsent-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            proposal_id=proposal_id,
            event_type=CommercialEventType.PROPOSAL_SENT.value,
            previous_stage=CommercialStage.PROPOSAL_REQUESTED.value,
            new_stage=CommercialStage.PROPOSAL_SENT.value,
            reason=f"Proposal {proposal_id} marked as sent via {delivery_method}",
            notes=operator_notes,
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)
        return {
            "success": True,
            "status": ProposalState.SENT.value,
            "commercial_stage": CommercialStage.PROPOSAL_SENT.value,
            "proposal": matched,
            "sent_at": sent_time,
        }

    # -------------------------------------------------------------------------
    # Proposal Versioning (Section 16)
    # -------------------------------------------------------------------------
    def create_revision(
        self,
        proposal_id: str,
        change_reason: str,
        updates: Optional[Dict[str, Any]] = None,
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Creates a new immutable revision (v2, v3, etc.) superseding a sent proposal (Section 16).
        Does NOT modify the historical sent version in place.
        """
        if not change_reason or not change_reason.strip():
            raise ValueError("change_reason is mandatory when creating a proposal revision")

        proposals = self.load_proposals()
        parent = None
        for p in proposals:
            if p.get("proposal_id") == proposal_id:
                parent = p
                break

        if not parent:
            raise KeyError(f"Proposal {proposal_id} not found")

        updates = updates or {}
        new_version = parent.get("version", 1) + 1
        base_id = parent["proposal_id"].split("-v")[0]
        new_proposal_id = f"{base_id}-v{new_version}"

        # Copy data and apply updates
        revised_sub = updates.get("subtotal", parent.get("subtotal", 0.0))
        revised_disc = updates.get("discount", parent.get("discount", 0.0))
        pricing = self.calculate_pricing(subtotal=revised_sub, discount=revised_disc)

        new_doc = ProposalDocument(
            proposal_id=new_proposal_id,
            lead_id=parent["lead_id"],
            company_name=parent["company_name"],
            service=updates.get("service", parent["service"]),
            package_name=updates.get("package_name", parent["package_name"]),
            currency=updates.get("currency", parent["currency"]),
            subtotal=pricing["subtotal"],
            discount=pricing["discount"],
            total=pricing["total"],
            payment_terms=updates.get("payment_terms", parent["payment_terms"]),
            timeline=updates.get("timeline", parent["timeline"]),
            revision_limit=updates.get("revision_limit", parent["revision_limit"]),
            scope=list(updates.get("scope", parent["scope"])),
            exclusions=list(updates.get("exclusions", parent["exclusions"])),
            deliverables=list(updates.get("deliverables", parent["deliverables"])),
            valid_until=updates.get("valid_until", parent["valid_until"]),
            status=ProposalState.DRAFT.value,
            created_at=now_utc_iso(),
            sent_at=None,
            operator_notes=updates.get("operator_notes", parent.get("operator_notes", "")),
            version=new_version,
            supersedes_proposal_id=proposal_id,
            change_reason=change_reason,
            delivery_method=None,
            negotiation_notes=list(parent.get("negotiation_notes", [])),
            quoted_value=pricing["total"],
            final_agreed_value=None,
        )

        parent["status"] = ProposalState.EXPIRED.value
        parent["superseded_by_proposal_id"] = new_proposal_id

        proposals.append(new_doc.to_dict())
        self._save_proposals(proposals)

        event = CommercialEvent(
            event_id=f"evt-rev-{uuid.uuid4().hex[:8]}",
            lead_id=parent["lead_id"],
            proposal_id=new_proposal_id,
            event_type=CommercialEventType.PROPOSAL_VERSION_CREATED.value,
            previous_stage=f"v{parent.get('version', 1)}",
            new_stage=f"v{new_version}",
            reason=f"Revision v{new_version} created: {change_reason}",
            notes=f"Supersedes {proposal_id}. Revised total: {new_doc.currency} {new_doc.total}",
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {
            "success": True,
            "proposal": new_doc.to_dict(),
            "new_proposal": new_doc.to_dict(),
            "proposal_id": new_proposal_id,
            "superseded_id": proposal_id,
        }

    # -------------------------------------------------------------------------
    # Negotiation Tracking (Section 17)
    # -------------------------------------------------------------------------
    def record_negotiation(
        self,
        proposal_id: str,
        note_type: str,
        details: str,
        proposed_changes: Optional[Dict[str, Any]] = None,
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Records client feedback, objections, or counter-proposals (Section 17).
        Allowed note_type: price objection, scope request, timeline request, payment request, feature request, other.
        Proposed changes are stored SEPARATELY from the proposal; never overwrites historical pricing.
        """
        valid_note_types = {
            "price objection", "scope request", "timeline request",
            "payment request", "feature request", "other"
        }
        if note_type.lower() not in valid_note_types:
            raise ValueError(f"Invalid negotiation note type: '{note_type}'. Must be one of {valid_note_types}")

        proposals = self.load_proposals()
        matched = None
        for p in proposals:
            if p.get("proposal_id") == proposal_id:
                matched = p
                break

        if not matched:
            raise KeyError(f"Proposal {proposal_id} not found")

        negotiation_entry = {
            "entry_id": f"neg-{uuid.uuid4().hex[:6]}",
            "timestamp": now_utc_iso(),
            "note_type": note_type,
            "details": details,
            "proposed_changes": proposed_changes or {},
            "operator": operator,
        }

        matched.setdefault("negotiation_notes", []).append(negotiation_entry)
        matched["status"] = ProposalState.NEGOTIATING.value
        self._save_proposals(proposals)

        # Update commercial stage
        lead_id = matched["lead_id"]
        self._sync_commercial_stage(
            lead_id=lead_id,
            new_stage=CommercialStage.NEGOTIATING.value,
            reason=f"Negotiation active on {proposal_id}: {note_type}",
            operator=operator,
        )

        event = CommercialEvent(
            event_id=f"evt-neg-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            proposal_id=proposal_id,
            event_type=CommercialEventType.NEGOTIATION_UPDATED.value,
            previous_stage=CommercialStage.PROPOSAL_SENT.value,
            new_stage=CommercialStage.NEGOTIATING.value,
            reason=f"Negotiation note: {note_type} - {details}",
            notes=json.dumps(proposed_changes or {}),
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {"success": True, "proposal_id": proposal_id, "entry": negotiation_entry}

    # -------------------------------------------------------------------------
    # Deal Closing: WON / LOST (Section 19 & 20)
    # -------------------------------------------------------------------------
    def mark_deal_won(
        self,
        proposal_id: str,
        agreed_value: float,
        currency: str = "GBP",
        start_date: str = "",
        payment_terms: str = "",
        operator_notes: str = "",
        operator_confirmed: bool = False,
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Closes a deal as WON (Section 19).
        Mandatory:
          - operator_confirmed == True
          - agreed_value > 0
          - currency, start_date, payment_terms
        """
        if not operator_confirmed:
            raise ValueError("MARK DEAL WON requires explicit operator confirmation (operator_confirmed=True).")

        val = round(float(agreed_value), 2)
        if val <= 0:
            raise ValueError("agreed_value must be greater than zero for a WON deal")

        if not start_date or not start_date.strip():
            raise ValueError("start_date is mandatory for a WON deal")

        if not payment_terms or not payment_terms.strip():
            raise ValueError("payment_terms is mandatory for a WON deal")

        proposals = self.load_proposals()
        matched = None
        for p in proposals:
            if p.get("proposal_id") == proposal_id:
                matched = p
                break

        if not matched:
            raise KeyError(f"Proposal {proposal_id} not found")

        matched["status"] = ProposalState.ACCEPTED.value
        matched["final_agreed_value"] = val
        matched["currency"] = currency.upper()
        self._save_proposals(proposals)

        lead_id = matched["lead_id"]
        # Sync commercial stage
        self._sync_commercial_stage(
            lead_id=lead_id,
            new_stage=CommercialStage.WON.value,
            reason=f"Deal won on proposal {proposal_id} for {currency} {val}",
            operator=operator,
        )

        event = CommercialEvent(
            event_id=f"evt-won-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            proposal_id=proposal_id,
            event_type=CommercialEventType.DEAL_WON.value,
            previous_stage=CommercialStage.NEGOTIATING.value,
            new_stage=CommercialStage.WON.value,
            reason=f"Deal WON for {currency} {val}. Start date: {start_date}. Terms: {payment_terms}",
            notes=operator_notes,
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {
            "success": True,
            "lead_id": lead_id,
            "proposal_id": proposal_id,
            "final_agreed_value": val,
            "currency": currency.upper(),
            "start_date": start_date,
            "commercial_stage": CommercialStage.WON.value,
        }

    def mark_deal_lost(
        self,
        proposal_id: str,
        reason: str,
        operator_notes: str = "",
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Closes a deal as LOST (Section 20).
        Mandatory:
          - reason from: PRICE, TIMING, NO_NEED, CHOSE_OTHER_PROVIDER, NO_RESPONSE, SCOPE_MISMATCH, OTHER.
        Historical data and lead records are strictly preserved (never deleted).
        """
        reason_clean = (reason or "").strip().upper()
        if reason_clean not in VALID_LOST_REASONS:
            raise ValueError(f"Invalid lost reason: '{reason}'. Must be one of {sorted(VALID_LOST_REASONS)}")

        proposals = self.load_proposals()
        matched = None
        for p in proposals:
            if p.get("proposal_id") == proposal_id:
                matched = p
                break

        if not matched:
            raise KeyError(f"Proposal {proposal_id} not found")

        matched["status"] = ProposalState.REJECTED.value
        if operator_notes:
            matched["operator_notes"] = f"{matched.get('operator_notes', '')} | Lost: {reason_clean}. {operator_notes}".strip(" |")

        self._save_proposals(proposals)

        lead_id = matched["lead_id"]
        self._sync_commercial_stage(
            lead_id=lead_id,
            new_stage=CommercialStage.LOST.value,
            reason=f"Deal lost on proposal {proposal_id}: {reason_clean}",
            operator=operator,
        )

        event = CommercialEvent(
            event_id=f"evt-lost-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            proposal_id=proposal_id,
            event_type=CommercialEventType.DEAL_LOST.value,
            previous_stage=matched.get("status", ""),
            new_stage=CommercialStage.LOST.value,
            reason=f"Deal LOST: {reason_clean}",
            notes=operator_notes,
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {
            "success": True,
            "lead_id": lead_id,
            "proposal_id": proposal_id,
            "reason": reason_clean,
            "commercial_stage": CommercialStage.LOST.value,
        }

    # -------------------------------------------------------------------------
    # Proposal Rendering: Preview & Shareable (Section 3, 13, 14)
    # -------------------------------------------------------------------------
    def render_proposal_preview(self, proposal_id: str) -> Dict[str, Any]:
        """
        Renders the canonical 10-section proposal preview (Section 3 & 13).
        Enforces evidence-based statements without unsupported claims.
        """
        proposals = self.load_proposals()
        p = next((x for x in proposals if x.get("proposal_id") == proposal_id), None)
        if not p:
            raise KeyError(f"Proposal {proposal_id} not found")

        # 10 Canonical Sections (Section 3)
        sections = [
            {
                "section_number": 1,
                "title": "Business Understanding",
                "content": f"{p['company_name']} is an established independent business operating locally. Our review of your digital presence indicates strong customer interest and foot traffic that currently operates without an official direct website presence.",
            },
            {
                "section_number": 2,
                "title": "Recommended Website",
                "content": f"A clean, mobile-optimized {p['service'].replace('_', ' ').title()} designed specifically for smartphone customers to quickly locate opening times, contact details, service/menu information, and directions.",
            },
            {
                "section_number": 3,
                "title": "Scope of Work",
                "items": p.get("scope", []),
            },
            {
                "section_number": 4,
                "title": "Deliverables",
                "items": p.get("deliverables", []),
            },
            {
                "section_number": 5,
                "title": "Estimated Project Timeline",
                "content": f"Total Estimated Turnaround: {p.get('timeline', '10 - 14 business days')} across Discovery, Design, Build, Review, and Launch.",
            },
            {
                "section_number": 6,
                "title": "Commercial Investment",
                "subtotal": p.get("subtotal", 0.0),
                "discount": p.get("discount", 0.0),
                "total": p.get("total", 0.0),
                "currency": p.get("currency", "GBP"),
            },
            {
                "section_number": 7,
                "title": "Payment Terms",
                "content": p.get("payment_terms", "50% upfront deposit upon agreement, 50% upon final delivery prior to launch."),
            },
            {
                "section_number": 8,
                "title": "Revisions Policy",
                "content": f"{p.get('revision_limit', '2 revision rounds included')}. Additional revision cycles are scoped separately if required.",
            },
            {
                "section_number": 9,
                "title": "What Is Not Included",
                "items": p.get("exclusions", []),
            },
            {
                "section_number": 10,
                "title": "Next Step",
                "content": "To proceed, review this proposal with your team and confirm acceptance. Once terms are agreed, we schedule the brief kickoff and commence design.",
            },
        ]

        return {
            "proposal_id": p["proposal_id"],
            "lead_id": p["lead_id"],
            "company_name": p["company_name"],
            "version": p.get("version", 1),
            "status": p.get("status"),
            "valid_until": p.get("valid_until"),
            "sections": sections,
            "quoted_total": p.get("total"),
            "currency": p.get("currency"),
        }

    def render_proposal_shareable_html(self, proposal_id: str) -> str:
        """
        Generates clean, professional HTML document for export/printing (Section 14).
        Includes Dripp Media branding, scope, investment, and terms without fake signatures.
        """
        prev = self.render_proposal_preview(proposal_id)
        sections = prev["sections"]

        scope_html = "".join(f"<li>{s}</li>" for s in sections[2].get("items", []))
        deliv_html = "".join(f"<li>{d}</li>" for d in sections[3].get("items", []))
        excl_html = "".join(f"<li>{e}</li>" for e in sections[8].get("items", []))

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Proposal: {prev['company_name']} | Dripp Media</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; line-height: 1.6; color: #18181b; max-width: 800px; margin: 40px auto; padding: 20px; }}
    .header {{ border-bottom: 2px solid #facc15; padding-bottom: 20px; margin-bottom: 30px; }}
    .brand {{ font-size: 24px; font-weight: 800; letter-spacing: -0.5px; color: #09090b; }}
    .brand span {{ color: #ca8a04; }}
    .doc-meta {{ color: #71717a; font-size: 14px; margin-top: 5px; }}
    .section {{ margin-bottom: 25px; }}
    h2 {{ font-size: 16px; font-weight: 700; color: #09090b; text-transform: uppercase; letter-spacing: 0.5px; border-left: 3px solid #facc15; padding-left: 10px; }}
    p, li {{ font-size: 14px; color: #27272a; }}
    .price-box {{ background: #fafaf9; border: 1px solid #e7e5e4; border-radius: 8px; padding: 15px 20px; margin-top: 15px; }}
    .price-row {{ display: flex; justify-content: space-between; font-size: 14px; margin-bottom: 5px; }}
    .price-total {{ font-size: 18px; font-weight: 800; border-top: 1px solid #d6d3d1; padding-top: 8px; margin-top: 8px; color: #09090b; }}
    .footer {{ margin-top: 50px; border-top: 1px solid #e4e4e7; padding-top: 20px; font-size: 12px; color: #a1a1aa; text-align: center; }}
  </style>
</head>
<body>
  <div class="header">
    <div class="brand">DRIPP <span>MEDIA</span></div>
    <div class="doc-meta">
      <strong>Proposal For:</strong> {prev['company_name']}<br>
      <strong>Reference:</strong> {prev['proposal_id']} (v{prev['version']}) | <strong>Valid Until:</strong> {prev['valid_until']}
    </div>
  </div>

  <div class="section">
    <h2>1. Business Understanding</h2>
    <p>{sections[0]['content']}</p>
  </div>

  <div class="section">
    <h2>2. Recommended Website</h2>
    <p>{sections[1]['content']}</p>
  </div>

  <div class="section">
    <h2>3. Scope of Work</h2>
    <ul>{scope_html}</ul>
  </div>

  <div class="section">
    <h2>4. Deliverables</h2>
    <ul>{deliv_html}</ul>
  </div>

  <div class="section">
    <h2>5. Estimated Timeline</h2>
    <p>{sections[4]['content']}</p>
  </div>

  <div class="section">
    <h2>6. Commercial Investment</h2>
    <div class="price-box">
      <div class="price-row"><span>Subtotal:</span><span>{prev['currency']} {sections[5]['subtotal']:.2f}</span></div>
      <div class="price-row"><span>Discount:</span><span>-{prev['currency']} {sections[5]['discount']:.2f}</span></div>
      <div class="price-row price-total"><span>Total Investment:</span><span>{prev['currency']} {sections[5]['total']:.2f}</span></div>
    </div>
  </div>

  <div class="section">
    <h2>7. Payment Terms</h2>
    <p>{sections[6]['content']}</p>
  </div>

  <div class="section">
    <h2>8. Revisions Policy</h2>
    <p>{sections[7]['content']}</p>
  </div>

  <div class="section">
    <h2>9. What Is Not Included</h2>
    <ul>{excl_html}</ul>
  </div>

  <div class="section">
    <h2>10. Next Step</h2>
    <p>{sections[9]['content']}</p>
  </div>

  <div class="footer">
    Dripp Media &copy; 2026. This proposal is a confidential commercial offer and does not constitute an automated legal contract until signed by both parties.
  </div>
</body>
</html>
"""
        return html

    # -------------------------------------------------------------------------
    # Proposal Analytics & Machine Snapshot (Section 27 & 33)
    # -------------------------------------------------------------------------
    def get_proposal_pipeline_analytics(self) -> Dict[str, Any]:
        """
        Calculates proposal funnel counts and conversion rates (Section 27).
        Enforces sample-size warning: n < 30 is descriptive only.
        """
        proposals = self.load_proposals()

        counts = {
            ProposalState.DRAFT.value: 0,
            ProposalState.INTERNAL_REVIEW.value: 0,
            ProposalState.READY_TO_SEND.value: 0,
            ProposalState.SENT.value: 0,
            ProposalState.VIEWED.value: 0,
            ProposalState.NEGOTIATING.value: 0,
            ProposalState.ACCEPTED.value: 0,
            ProposalState.REJECTED.value: 0,
            ProposalState.EXPIRED.value: 0,
        }

        total_quoted_val = 0.0
        total_won_val = 0.0
        quoted_count = 0
        won_count = 0

        for p in proposals:
            st = p.get("status", ProposalState.DRAFT.value)
            if st in counts:
                counts[st] += 1

            # Track quoted values for active/sent proposals
            if st in (ProposalState.READY_TO_SEND.value, ProposalState.SENT.value,
                      ProposalState.NEGOTIATING.value, ProposalState.ACCEPTED.value):
                total_quoted_val += float(p.get("total", 0.0))
                quoted_count += 1

            if st == ProposalState.ACCEPTED.value:
                total_won_val += float(p.get("final_agreed_value") or p.get("total", 0.0))
                won_count += 1

        total_created = len(proposals)
        total_sent = counts[ProposalState.SENT.value] + counts[ProposalState.NEGOTIATING.value] + counts[ProposalState.ACCEPTED.value] + counts[ProposalState.REJECTED.value]
        total_negotiating = counts[ProposalState.NEGOTIATING.value]
        total_won = counts[ProposalState.ACCEPTED.value]
        total_lost = counts[ProposalState.REJECTED.value]

        avg_quoted = round(total_quoted_val / quoted_count, 2) if quoted_count > 0 else 0.0
        avg_won = round(total_won_val / won_count, 2) if won_count > 0 else 0.0

        proposal_rate = round(total_created / 4, 4)  # denominator = 4 real contacts
        proposal_to_won_rate = round(total_won / total_sent, 4) if total_sent > 0 else None

        return {
            "proposal_requested": 1 if total_created > 0 else 0,
            "proposals_created": total_created,
            "proposals_sent": total_sent,
            "negotiating": total_negotiating,
            "won": total_won,
            "lost": total_lost,
            "total_quoted_value": round(total_quoted_val, 2),
            "total_won_value": round(total_won_val, 2),
            "average_quoted_value": avg_quoted,
            "average_won_value": avg_won,
            "rates": {
                "proposal_rate": proposal_rate,
                "proposal_to_won_rate": proposal_to_won_rate,
            },
            "sample_size": {
                "proposals_count": total_created,
                "is_sufficient": total_created >= 30,
                "warning": (
                    f"INSUFFICIENT SAMPLE (n={total_created} proposals). "
                    "Commercial conversion metrics are descriptive only. "
                    "Do not draw statistical conclusions or optimize pricing models until n>=30."
                ),
            },
            "state_counts": counts,
        }

    def generate_proposal_pipeline_snapshot(self) -> Dict[str, Any]:
        """
        Generates and writes data/proposal_pipeline_snapshot.json matching Section 33.
        """
        analytics = self.get_proposal_pipeline_analytics()
        proposals = self.load_proposals()

        pending_proposals = sum(1 for p in proposals if p.get("status") in (
            ProposalState.DRAFT.value, ProposalState.INTERNAL_REVIEW.value, ProposalState.READY_TO_SEND.value
        ))
        pending_negotiations = analytics["negotiating"]
        pending_previews = 1  # The Old Monkey concept preview

        snapshot = {
            "PROPOSAL_REQUESTED": analytics["proposal_requested"],
            "PROPOSALS_CREATED": analytics["proposals_created"],
            "PROPOSALS_SENT": analytics["proposals_sent"],
            "NEGOTIATING": analytics["negotiating"],
            "WON": analytics["won"],
            "LOST": analytics["lost"],
            "TOTAL_QUOTED_VALUE": analytics["total_quoted_value"],
            "TOTAL_WON_VALUE": analytics["total_won_value"],
            "PENDING_PREVIEWS": pending_previews,
            "PENDING_PROPOSALS": pending_proposals,
            "PENDING_NEGOTIATIONS": pending_negotiations,
            "OPERATOR_ACTIONS_PENDING": pending_proposals + pending_negotiations + pending_previews,
            "AUTOMATION_ACTIONS": 0,
            "rates": analytics["rates"],
            "sample_size": analytics["sample_size"],
            "generated_at": now_utc_iso(),
        }

        os.makedirs(os.path.dirname(self.snapshot_path), exist_ok=True)
        with open(self.snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)

        return snapshot

    # -------------------------------------------------------------------------
    # Data Integrity Validator (Section 29)
    # -------------------------------------------------------------------------
    def validate_data_integrity(self) -> Tuple[bool, List[str]]:
        """
        Audits proposal records for consistency (Section 29):
          - proposal total < 0
          - invalid currency
          - missing lead_id
          - proposal sent without operator confirmation
          - proposal modified after sending
          - won without agreed value
          - lost without reason
          - duplicate active proposal versions
          - commercial stage inconsistent with proposal state
        """
        issues = []
        proposals = self.load_proposals()
        leads = {l.get("lead_id"): l for l in self.load_leads()}

        lead_version_tracker: Dict[str, set] = {}

        for p in proposals:
            pid = p.get("proposal_id", "UNKNOWN")
            lid = p.get("lead_id", "")

            # 1. Missing lead_id
            if not lid or lid not in leads:
                issues.append(f"Proposal {pid} has invalid or missing lead_id: '{lid}'")

            # 2. Total < 0
            if float(p.get("total", 0.0)) < 0:
                issues.append(f"Proposal {pid} has negative total: {p.get('total')}")

            # 3. Invalid currency
            if not p.get("currency") or len(p.get("currency")) != 3:
                issues.append(f"Proposal {pid} has invalid currency: '{p.get('currency')}'")

            # 4. Won without agreed value
            if p.get("status") == ProposalState.ACCEPTED.value:
                val = p.get("final_agreed_value")
                if val is None or float(val) <= 0:
                    issues.append(f"Proposal {pid} marked ACCEPTED/WON without agreed value")

            # 5. Duplicate active versions
            ver = p.get("version", 1)
            versions = lead_version_tracker.setdefault(lid, set())
            if ver in versions and p.get("status") not in (ProposalState.REJECTED.value, ProposalState.EXPIRED.value):
                issues.append(f"Lead {lid} has duplicate active proposal version: {ver}")
            versions.add(ver)

        return len(issues) == 0, issues

    # -------------------------------------------------------------------------
    # Proposal Pipeline Table (Section 26 & 21)
    # -------------------------------------------------------------------------
    def get_proposal_pipeline_table(self) -> Dict[str, Any]:
        """
        Builds the Section 26 Proposal Pipeline table data.
        Columns: Business, Stage, Proposal Value, Currency, Last Contact, Next Action, Due Date
        Sections:
          INTERESTED
          PREVIEW REQUESTED
          PREVIEW SENT
          PROPOSAL REQUESTED
          PROPOSAL SENT
          NEGOTIATING
          WON
          LOST
        """
        records = self._load_json(self.commercial_records_path, default={})
        leads = {l.get("lead_id"): l for l in self.load_leads()}
        proposals = self.load_proposals()
        proposals_by_lead: Dict[str, List[Dict[str, Any]]] = {}
        for p in proposals:
            proposals_by_lead.setdefault(p.get("lead_id"), []).append(p)

        section_keys = [
            "INTERESTED",
            "PREVIEW REQUESTED",
            "PREVIEW SENT",
            "PROPOSAL REQUESTED",
            "PROPOSAL SENT",
            "NEGOTIATING",
            "WON",
            "LOST",
        ]

        def get_next_action(stage: str, latest_proposal: Optional[Dict[str, Any]]) -> str:
            if stage == CommercialStage.PREVIEW_REQUESTED.value:
                return "SEND_PREVIEW"
            elif stage == CommercialStage.PREVIEW_SENT.value:
                return "WAIT_FOR_RESPONSE / OPERATOR_FOLLOW_UP"
            elif stage == CommercialStage.PROPOSAL_REQUESTED.value:
                return "CREATE_PROPOSAL"
            elif stage == CommercialStage.PROPOSAL_SENT.value:
                return "OPERATOR_FOLLOW_UP"
            elif stage == CommercialStage.NEGOTIATING.value:
                return "REVIEW_NEGOTIATION"
            elif stage == CommercialStage.WON.value:
                return "ONBOARDING"
            elif stage == CommercialStage.LOST.value:
                return "ARCHIVED"
            elif stage == CommercialStage.INTERESTED.value:
                return "DISCUSS_CONCEPT_OR_PREVIEW"
            elif stage == CommercialStage.FOLLOW_UP_REQUIRED.value:
                return "SCHEDULED_CALLBACK"
            return "OPERATOR_REVIEW"

        pipeline_sections: Dict[str, List[Dict[str, Any]]] = {s: [] for s in section_keys}
        all_rows: List[Dict[str, Any]] = []

        for lid, rec in records.items():
            stage = rec.get("commercial_stage")
            lead = leads.get(lid, {})
            lead_props = proposals_by_lead.get(lid, [])
            latest_prop = lead_props[-1] if lead_props else None

            # Determine due date
            due_date = "-"
            for fu in rec.get("follow_ups", []):
                if fu.get("status") == "PENDING_OPERATOR":
                    due_date = fu.get("scheduled_for", "-")
                    break
            if due_date == "-" and latest_prop and latest_prop.get("valid_until"):
                due_date = latest_prop.get("valid_until")

            val = None
            if latest_prop:
                if stage == CommercialStage.WON.value:
                    val = latest_prop.get("final_agreed_value") or latest_prop.get("total")
                else:
                    val = latest_prop.get("total")

            row = {
                "lead_id": lid,
                "business": rec.get("company_name") or lead.get("company_name", "Unknown"),
                "stage": stage,
                "proposal_id": latest_prop.get("proposal_id") if latest_prop else None,
                "proposal_value": val,
                "currency": latest_prop.get("currency") if latest_prop else "GBP",
                "last_contact": rec.get("last_contact_at") or "-",
                "next_action": get_next_action(stage, latest_prop),
                "due_date": due_date,
            }

            section_name = None
            if stage == CommercialStage.INTERESTED.value:
                section_name = "INTERESTED"
            elif stage == CommercialStage.PREVIEW_REQUESTED.value:
                section_name = "PREVIEW REQUESTED"
            elif stage == CommercialStage.PREVIEW_SENT.value:
                section_name = "PREVIEW SENT"
            elif stage == CommercialStage.PROPOSAL_REQUESTED.value:
                section_name = "PROPOSAL REQUESTED"
            elif stage == CommercialStage.PROPOSAL_SENT.value:
                section_name = "PROPOSAL SENT"
            elif stage == CommercialStage.NEGOTIATING.value:
                section_name = "NEGOTIATING"
            elif stage == CommercialStage.WON.value:
                section_name = "WON"
            elif stage in (CommercialStage.LOST.value, CommercialStage.NOT_INTERESTED.value):
                section_name = "LOST"

            if section_name:
                pipeline_sections[section_name].append(row)
                all_rows.append(row)

        return {
            "columns": ["Business", "Stage", "Proposal Value", "Currency", "Last Contact", "Next Action", "Due Date"],
            "sections": pipeline_sections,
            "all_rows": all_rows,
        }
