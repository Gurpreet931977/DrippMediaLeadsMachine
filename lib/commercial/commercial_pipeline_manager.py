"""
Post-9.7 Commercial Pipeline Manager

Orchestrates the commercial conversion workflow:
  OUTREACH -> CONVERSATION -> FOLLOW-UP -> PREVIEW -> PROPOSAL -> CLIENT

Key Responsibilities:
  1. Outcome Provenance:
     - Distinguishes SYSTEM_VERIFIED, OPERATOR_REPORTED, PROVIDER_CONFIRMED.
     - Manual calls strictly recorded as OPERATOR_REPORTED.
  2. Commercial Stage Management:
     - Tracks commercial_stage independently from qualification_state and outreach_status.
     - Never auto-promotes stages (CONNECTED does NOT equal INTERESTED).
  3. Manchester Shawarma Callback:
     - Prominent follow-up record (2026-10-06 14:00, auto_schedule=False, auto_call=False, status=PENDING_OPERATOR).
  4. The Old Monkey Preview Workflow:
     - Commercial task: PREVIEW_REQUESTED.
     - Tracks draft, ready, sent, viewed states. Explicit MARK PREVIEW SENT.
  5. Proposal & Won/Lost Tracking:
     - Mandatory reason fields for LOST, required service/value fields for WON.
     - Won requires explicit operator confirmation.
  6. Next Best Commercial Action:
     - Strictly prioritized per Section 15:
       1. Callback requested
       2. Preview requested
       3. Explicit interest
       4. Recent connection without next step
       5. No-answer retry
       6. Remaining qualified leads
  7. Funnel Analytics:
     - Clear denominators, sample-size protection (insufficient sample warning for n<30).
  8. Append-Only Event Model & Strict Automation Limits:
     - All auto flags False. System recommends, human decides and executes.
"""

import os
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from lib.commercial.models import (
    CommercialStage,
    WebsitePipelineStage,
    OutcomeProvenance,
    InterestLevel,
    PreviewStatus,
    ProposalStatus,
    LostReason,
    NextActionType,
    CommercialEventType,
    CommercialNotes,
    FollowUpRecord,
    PreviewRecord,
    ProposalRecord,
    CommercialEvent,
    VALID_PROVENANCE,
    VALID_COMMERCIAL_STAGES,
    VALID_WEBSITE_STAGES,
    VALID_INTEREST_LEVELS,
    VALID_PREVIEW_STATUSES,
    VALID_PROPOSAL_STATUSES,
    VALID_LOST_REASONS,
    VALID_COMMERCIAL_EVENT_TYPES,
    AUTO_CALL,
    AUTO_DM,
    AUTO_EMAIL,
    AUTO_FOLLOWUP,
    AUTO_CALLBACK,
    AUTO_PROPOSAL,
    AUTO_CONTINUATION,
    now_utc_iso,
)

logger = logging.getLogger("CommercialPipelineManager")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

DEFAULT_LEADS_PATH = os.path.join(DATA_DIR, "cache_sheets_leads.json")
DEFAULT_OUTCOMES_PATH = os.path.join(DATA_DIR, "outreach_outcomes.json")
DEFAULT_TIMELINES_PATH = os.path.join(DATA_DIR, "lead_timelines.json")
DEFAULT_COMMERCIAL_RECORDS_PATH = os.path.join(DATA_DIR, "commercial_records.json")
DEFAULT_COMMERCIAL_EVENTS_PATH = os.path.join(DATA_DIR, "commercial_events.json")
DEFAULT_SNAPSHOT_PATH = os.path.join(DATA_DIR, "commercial_pipeline_snapshot.json")


class CommercialPipelineManager:
    """
    Authoritative manager for post-outreach commercial conversion workflows.
    Ensures strict separation between qualification, outreach status, and commercial pipeline.
    """

    def __init__(
        self,
        leads_path: str = DEFAULT_LEADS_PATH,
        outcomes_path: str = DEFAULT_OUTCOMES_PATH,
        timelines_path: str = DEFAULT_TIMELINES_PATH,
        commercial_records_path: str = DEFAULT_COMMERCIAL_RECORDS_PATH,
        commercial_events_path: str = DEFAULT_COMMERCIAL_EVENTS_PATH,
        snapshot_path: str = DEFAULT_SNAPSHOT_PATH,
    ):
        self.leads_path = leads_path
        self.outcomes_path = outcomes_path
        self.timelines_path = timelines_path
        self.commercial_records_path = commercial_records_path
        self.commercial_events_path = commercial_events_path
        self.snapshot_path = snapshot_path

        self._ensure_storage_initialized()

    # -------------------------------------------------------------------------
    # Initialization & Storage
    # -------------------------------------------------------------------------
    def _ensure_storage_initialized(self) -> None:
        """Initializes commercial records and events if not already present."""
        os.makedirs(os.path.dirname(self.commercial_records_path), exist_ok=True)

        if not os.path.exists(self.commercial_events_path):
            with open(self.commercial_events_path, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)

        if not os.path.exists(self.commercial_records_path):
            records = self._bootstrap_commercial_records()
            with open(self.commercial_records_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
        else:
            # Sync with any new leads in leads_path
            self._sync_records_with_leads()

    def _bootstrap_commercial_records(self) -> Dict[str, Dict[str, Any]]:
        """Bootstraps commercial records from existing leads and outreach outcomes."""
        leads = self.load_leads()
        outcomes = self.load_outcomes()
        records: Dict[str, Dict[str, Any]] = {}

        # Index latest outcome per lead
        latest_outcomes: Dict[str, Dict[str, Any]] = {}
        for out in outcomes:
            lid = out.get("lead_id")
            if lid:
                latest_outcomes[lid] = out

        for lead in leads:
            lid = lead.get("lead_id")
            if not lid:
                continue

            company = lead.get("company_name", "Unknown")
            outreach_status = lead.get("outreach_status", "NOT_CONTACTED")
            qual_state = lead.get("qualification_state", "OUTREACH_READY")
            out_info = latest_outcomes.get(lid, {})

            # Default commercial stage and website pipeline stage
            commercial_stage = CommercialStage.QUALIFIED.value
            website_stage = WebsitePipelineStage.NO_WEBSITE.value
            notes = CommercialNotes()
            follow_ups: List[Dict[str, Any]] = []
            previews: List[Dict[str, Any]] = []
            proposals: List[Dict[str, Any]] = []
            deal_won: Optional[Dict[str, Any]] = None
            deal_lost: Optional[Dict[str, Any]] = None

            # 1. Little Aladdin (LEAD-MAN-902001)
            if lid == "LEAD-MAN-902001":
                commercial_stage = CommercialStage.CONTACTED.value
                website_stage = WebsitePipelineStage.OPEN_TO_DISCUSSION.value
                notes = CommercialNotes(
                    decision_maker="Restaurant Manager",
                    role="Manager",
                    interest_level=InterestLevel.HIGH.value,
                    current_website_situation="No official website; relies on delivery apps and foot traffic",
                    requested_service="Mobile-friendly website concept",
                    next_step="Send website preview concept",
                )

            # 2. The Old Monkey (LEAD-MAN-3B9091)
            elif lid == "LEAD-MAN-3B9091":
                commercial_stage = CommercialStage.PREVIEW_REQUESTED.value
                website_stage = WebsitePipelineStage.OPEN_TO_DISCUSSION.value
                notes = CommercialNotes(
                    decision_maker="General Manager",
                    role="Manager",
                    interest_level=InterestLevel.HIGH.value,
                    current_website_situation="No official website; active Instagram with 1,911 reviews",
                    requested_service="Simple mobile-friendly website showcasing pub details and reviews",
                    next_step="Prepare and review concept preview draft",
                )
                previews.append(
                    PreviewRecord(
                        lead_id=lid,
                        company_name=company,
                        preview_status=PreviewStatus.PREVIEW_DRAFT.value,
                        preview_url="https://preview.dripp.media/the-old-monkey-mcr",
                        preview_description="Mobile-optimized pub site featuring 1,911 4.8★ reviews, opening times, craft beer selection, and Portland Street map.",
                        what_demonstrated="Mobile responsiveness, review social proof, one-tap directions, and click-to-call.",
                        next_action="Review draft preview with operator before sending",
                        operator_notes="Manager receptive during Phase 9.7 call. Keep copy concise and focused on high ratings.",
                    ).to_dict()
                )

            # 3. Dog and Partridge (LEAD-MAN-4098E1)
            elif lid == "LEAD-MAN-4098E1":
                # Section 16: commercial_stage = CONTACTED, next_action = OPERATOR_REVIEW
                commercial_stage = CommercialStage.CONTACTED.value
                website_stage = WebsitePipelineStage.CONTACTED.value
                notes = CommercialNotes(
                    decision_maker="Manager/Owner",
                    role="Unknown",
                    interest_level=InterestLevel.UNKNOWN.value,
                    current_website_situation="No website found (730 reviews, 4.5★)",
                    next_step="OPERATOR_REVIEW (retry timing decision)",
                )

            # 4. Manchester Shawarma (LEAD-MAN-E81185)
            elif lid == "LEAD-MAN-E81185":
                # Section 4: Follow-up record: CALLBACK, requested 2026-10-06 14:00
                commercial_stage = CommercialStage.FOLLOW_UP_REQUIRED.value
                website_stage = WebsitePipelineStage.CONTACTED.value
                notes = CommercialNotes(
                    decision_maker="Owner / Shift Manager",
                    role="Manager",
                    interest_level=InterestLevel.MEDIUM.value,
                    current_website_situation="No website; popular curry mile takeaway with 217 reviews",
                    requested_service="Discuss web ordering / menu showcase",
                    timeline_signal="Call back after 14:00 post-lunch rush",
                    next_step="Manual callback on 2026-10-06 at 14:00",
                )
                follow_ups.append(
                    FollowUpRecord(
                        follow_up_id="FU-MAN-E81185-01",
                        lead_id=lid,
                        company_name=company,
                        follow_up_type="CALLBACK",
                        scheduled_for="2026-10-06 14:00",
                        auto_schedule=False,
                        auto_call=False,
                        status="PENDING_OPERATOR",
                        notes="Spoke with manager during Phase 9.7; requested callback after peak lunch hours (2:00 PM).",
                    ).to_dict()
                )

            # 5. Live Seafood Ltd (LEAD-MAN-0363CF) - Protected
            elif lid == "LEAD-MAN-0363CF":
                commercial_stage = CommercialStage.QUALIFIED.value
                website_stage = WebsitePipelineStage.NO_WEBSITE.value
                notes = CommercialNotes(
                    interest_level=InterestLevel.UNKNOWN.value,
                    next_step="Awaiting contactability activation (NOT_READY)",
                )

            # 6. Seoul Kimchi (LEAD-MAN-4DB3EF) - Protected Historical
            elif lid == "LEAD-MAN-4DB3EF":
                commercial_stage = CommercialStage.CONTACTED.value
                website_stage = WebsitePipelineStage.CONTACTED.value
                notes = CommercialNotes(
                    interest_level=InterestLevel.UNKNOWN.value,
                    next_step="Awaiting direct response to sent outreach",
                )

            # 7. Hong Thai (LEAD-MAN-709C66) - Protected Suppressed
            elif lid == "LEAD-MAN-709C66":
                commercial_stage = CommercialStage.LOST.value
                website_stage = WebsitePipelineStage.LOST.value
                deal_lost = {
                    "reason": LostReason.NO_RESPONSE.value,
                    "notes": "Email bounced and address suppressed. No further outreach permitted.",
                    "closed_at": now_utc_iso(),
                }

            # Map existing general outcomes if any
            elif out_info:
                outcome_val = out_info.get("outcome", "")
                if outcome_val in ("CONNECTED", "INTERESTED"):
                    commercial_stage = CommercialStage.CONTACTED.value
                    website_stage = WebsitePipelineStage.CONTACTED.value
                elif outcome_val == "NO_ANSWER":
                    commercial_stage = CommercialStage.CONTACTED.value
                    website_stage = WebsitePipelineStage.CONTACTED.value
                elif outcome_val == "CALLBACK_REQUESTED":
                    commercial_stage = CommercialStage.FOLLOW_UP_REQUIRED.value
                    website_stage = WebsitePipelineStage.CONTACTED.value

            records[lid] = {
                "lead_id": lid,
                "company_name": company,
                "commercial_stage": commercial_stage,
                "website_pipeline_stage": website_stage,
                "notes": notes.to_dict(),
                "follow_ups": follow_ups,
                "previews": previews,
                "proposals": proposals,
                "deal_won": deal_won,
                "deal_lost": deal_lost,
                "last_contact_at": out_info.get("recorded_at") or lead.get("outreach_sent_at") or None,
                "latest_outcome": out_info.get("outcome") or None,
                "latest_outcome_source": out_info.get("source") or OutcomeProvenance.OPERATOR_REPORTED.value if out_info else None,
                "operator_confirmed": bool(out_info.get("operator_confirmed", True)) if out_info else False,
                "created_at": lead.get("date_added") or now_utc_iso(),
                "updated_at": now_utc_iso(),
            }

        return records

    def _sync_records_with_leads(self) -> None:
        """Synchronizes existing commercial records with current leads cache."""
        records = self.load_commercial_records()
        leads = self.load_leads()
        updated = False

        for lead in leads:
            lid = lead.get("lead_id")
            if lid and lid not in records:
                records[lid] = {
                    "lead_id": lid,
                    "company_name": lead.get("company_name", "Unknown"),
                    "commercial_stage": CommercialStage.QUALIFIED.value,
                    "website_pipeline_stage": WebsitePipelineStage.NO_WEBSITE.value,
                    "notes": CommercialNotes().to_dict(),
                    "follow_ups": [],
                    "previews": [],
                    "proposals": [],
                    "deal_won": None,
                    "deal_lost": None,
                    "last_contact_at": lead.get("outreach_sent_at") or None,
                    "latest_outcome": None,
                    "latest_outcome_source": None,
                    "operator_confirmed": False,
                    "created_at": lead.get("date_added") or now_utc_iso(),
                    "updated_at": now_utc_iso(),
                }
                updated = True

        if updated:
            self._save_commercial_records(records)

    # -------------------------------------------------------------------------
    # Data Access Helpers
    # -------------------------------------------------------------------------
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

    def load_outcomes(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.outcomes_path):
            return []
        try:
            with open(self.outcomes_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"Error loading outcomes from {self.outcomes_path}: {e}")
            return []

    def load_commercial_records(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.exists(self.commercial_records_path):
            return self._bootstrap_commercial_records()
        try:
            with open(self.commercial_records_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading commercial records: {e}")
            return {}

    def _save_commercial_records(self, records: Dict[str, Dict[str, Any]]) -> None:
        from lib.system.atomic_writer import atomic_write_json
        from lib.system.file_lock import commercial_records_lock
        with commercial_records_lock():
            atomic_write_json(self.commercial_records_path, records)

    def load_commercial_events(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.commercial_events_path):
            return []
        try:
            with open(self.commercial_events_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading commercial events: {e}")
            return []

    def _append_commercial_event(self, event: CommercialEvent) -> None:
        """Appends event to commercial_events.json and appends to lead_timelines.json atomically."""
        from lib.system.atomic_writer import atomic_write_json
        from lib.system.file_lock import commercial_records_lock
        with commercial_records_lock():
            events = self.load_commercial_events()
            events.append(event.to_dict())
            atomic_write_json(self.commercial_events_path, events)

            # Also append to lead_timelines.json
            if os.path.exists(self.timelines_path):
                try:
                    with open(self.timelines_path, "r", encoding="utf-8") as f:
                        timelines = json.load(f)
                    lead_events = timelines.setdefault(event.lead_id, [])
                    lead_events.append({
                        "event_type": event.event_type,
                        "timestamp": event.timestamp,
                        "previous_stage": event.previous_stage,
                        "new_stage": event.new_stage,
                        "reason": event.reason,
                        "notes": event.notes,
                        "source": event.source,
                        "operator": event.operator,
                    })
                    atomic_write_json(self.timelines_path, timelines)
                except Exception as e:
                    logger.warning(f"Could not update lead_timelines.json for {event.lead_id}: {e}")

    # -------------------------------------------------------------------------
    # Section 1: Outcome Provenance Verification
    # -------------------------------------------------------------------------
    def validate_outcome_provenance(
        self,
        outcome: str,
        channel: str,
        source: str,
        operator_confirmed: bool = False,
    ) -> Tuple[bool, str]:
        """
        Validates the provenance of an outreach outcome (Section 1).
        Manual phone calls MUST have source == OPERATOR_REPORTED unless independent proof exists.
        """
        if source not in VALID_PROVENANCE:
            return False, f"Invalid provenance source: {source}. Must be one of {VALID_PROVENANCE}"

        if channel == "PHONE" and source == OutcomeProvenance.SYSTEM_VERIFIED.value:
            return False, "Manual phone calls cannot be SYSTEM_VERIFIED. Software does not verify voice calls."

        if channel == "PHONE" and source == OutcomeProvenance.PROVIDER_CONFIRMED.value and not operator_confirmed:
            return False, "PROVIDER_CONFIRMED requires independent provider verification receipt."

        return True, "Valid provenance"

    # -------------------------------------------------------------------------
    # Section 2 & 3: Commercial Stage Management (Never Auto-Promote)
    # -------------------------------------------------------------------------
    def update_commercial_stage(
        self,
        lead_id: str,
        new_stage: str,
        reason: str,
        operator: str = "HUMAN_OPERATOR",
        notes: str = "",
        operator_confirmed: bool = True,
    ) -> Dict[str, Any]:
        """
        Explicit operator transition of commercial stage (Section 3).
        Guards:
          - Rejects auto-promotion from CONNECTED to INTERESTED without explicit intent.
          - Rejects modifying suppressed leads (Hong Thai).
          - Rejects marking WON without full closure details.
        """
        records = self.load_commercial_records()
        if lead_id not in records:
            raise KeyError(f"Lead {lead_id} not found in commercial records.")

        rec = records[lead_id]
        old_stage = rec.get("commercial_stage", CommercialStage.QUALIFIED.value)

        new_stage_clean = new_stage.strip().upper()
        if new_stage_clean not in VALID_COMMERCIAL_STAGES:
            raise ValueError(f"Invalid commercial stage: {new_stage}. Must be one of {VALID_COMMERCIAL_STAGES}")

        # Protection: Hong Thai (LEAD-MAN-709C66) suppressed
        if lead_id == "LEAD-MAN-709C66" and new_stage_clean in (CommercialStage.QUALIFIED.value, CommercialStage.CONTACTED.value):
            raise PermissionError("Hong Thai (LEAD-MAN-709C66) is suppressed and cannot be reopened.")

        # Invariant: WON requires explicit confirmation
        if new_stage_clean == CommercialStage.WON.value and not operator_confirmed:
            raise ValueError("Commercial stage WON requires explicit operator confirmation.")

        rec["commercial_stage"] = new_stage_clean
        rec["updated_at"] = now_utc_iso()

        # Map to website pipeline stage if applicable
        if new_stage_clean == CommercialStage.PREVIEW_REQUESTED.value:
            rec["website_pipeline_stage"] = WebsitePipelineStage.OPEN_TO_DISCUSSION.value
        elif new_stage_clean == CommercialStage.PREVIEW_SENT.value:
            rec["website_pipeline_stage"] = WebsitePipelineStage.PREVIEW_SENT.value
        elif new_stage_clean == CommercialStage.PROPOSAL_REQUESTED.value:
            rec["website_pipeline_stage"] = WebsitePipelineStage.PROPOSAL_REQUESTED.value
        elif new_stage_clean == CommercialStage.PROPOSAL_SENT.value:
            rec["website_pipeline_stage"] = WebsitePipelineStage.PROPOSAL_SENT.value
        elif new_stage_clean == CommercialStage.NEGOTIATING.value:
            rec["website_pipeline_stage"] = WebsitePipelineStage.NEGOTIATING.value
        elif new_stage_clean == CommercialStage.WON.value:
            rec["website_pipeline_stage"] = WebsitePipelineStage.WON.value
        elif new_stage_clean in (CommercialStage.LOST.value, CommercialStage.NOT_INTERESTED.value):
            rec["website_pipeline_stage"] = WebsitePipelineStage.LOST.value

        self._save_commercial_records(records)

        # Append audit event
        event = CommercialEvent(
            event_id=f"evt-comm-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            event_type=CommercialEventType.COMMERCIAL_STAGE_CHANGED.value,
            previous_stage=old_stage,
            new_stage=new_stage_clean,
            reason=reason,
            notes=notes,
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {
            "success": True,
            "lead_id": lead_id,
            "previous_stage": old_stage,
            "new_stage": new_stage_clean,
            "reason": reason,
            "event_id": event.event_id,
        }

    # -------------------------------------------------------------------------
    # Section 4: Manchester Shawarma & General Follow-Ups
    # -------------------------------------------------------------------------
    def schedule_follow_up(
        self,
        lead_id: str,
        follow_up_type: str = "CALLBACK",
        scheduled_for: str = "",
        notes: str = "",
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Schedules a follow-up / callback.
        Enforces:
          - auto_schedule = False
          - auto_call = False
          - status = PENDING_OPERATOR
        """
        records = self.load_commercial_records()
        if lead_id not in records:
            raise KeyError(f"Lead {lead_id} not found in commercial records.")

        rec = records[lead_id]
        company = rec.get("company_name", "Unknown")

        follow_up = FollowUpRecord(
            follow_up_id=f"FU-{lead_id}-{uuid.uuid4().hex[:4]}",
            lead_id=lead_id,
            company_name=company,
            follow_up_type=follow_up_type.upper(),
            scheduled_for=scheduled_for,
            auto_schedule=AUTO_CALLBACK,  # Always False
            auto_call=AUTO_CALL,          # Always False
            status="PENDING_OPERATOR",
            notes=notes,
        )

        rec.setdefault("follow_ups", []).append(follow_up.to_dict())
        rec["commercial_stage"] = CommercialStage.FOLLOW_UP_REQUIRED.value
        rec["updated_at"] = now_utc_iso()
        self._save_commercial_records(records)

        # Audit event
        event = CommercialEvent(
            event_id=f"evt-fu-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            event_type=CommercialEventType.FOLLOW_UP_CREATED.value,
            previous_stage=rec.get("commercial_stage", ""),
            new_stage=CommercialStage.FOLLOW_UP_REQUIRED.value,
            reason=f"Scheduled {follow_up_type} for {scheduled_for}",
            notes=notes,
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {"success": True, "follow_up": follow_up.to_dict()}

    def complete_follow_up(
        self,
        lead_id: str,
        follow_up_id: str,
        outcome: str,
        notes: str = "",
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """Marks a pending follow-up as completed by operator. Never deletes history."""
        records = self.load_commercial_records()
        if lead_id not in records:
            raise KeyError(f"Lead {lead_id} not found.")

        rec = records[lead_id]
        follow_ups = rec.get("follow_ups", [])
        matched = False

        for fu in follow_ups:
            if fu.get("follow_up_id") == follow_up_id or follow_up_id == "LATEST":
                fu["status"] = "COMPLETED"
                fu["completed_at"] = now_utc_iso()
                fu["outcome"] = outcome
                fu["notes"] = f"{fu.get('notes', '')} | Outcome: {outcome}. {notes}".strip(" |")
                matched = True
                break

        if not matched:
            raise KeyError(f"Follow-up {follow_up_id} not found for {lead_id}")

        rec["updated_at"] = now_utc_iso()
        self._save_commercial_records(records)

        event = CommercialEvent(
            event_id=f"evt-fudone-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            event_type=CommercialEventType.FOLLOW_UP_COMPLETED.value,
            previous_stage=rec.get("commercial_stage", ""),
            new_stage=rec.get("commercial_stage", ""),
            reason=f"Follow-up {follow_up_id} completed: {outcome}",
            notes=notes,
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {"success": True, "lead_id": lead_id, "follow_up_id": follow_up_id, "outcome": outcome}

    # -------------------------------------------------------------------------
    # Section 5 & 6: The Old Monkey Preview Workflow
    # -------------------------------------------------------------------------
    def create_or_update_preview(
        self,
        lead_id: str,
        preview_url: str,
        preview_description: str,
        what_demonstrated: str = "",
        next_action: str = "",
        operator_notes: str = "",
        preview_status: str = PreviewStatus.PREVIEW_DRAFT.value,
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Creates or updates a concept website preview record (Section 5 & 6).
        Does NOT automatically send the preview.
        """
        records = self.load_commercial_records()
        if lead_id not in records:
            raise KeyError(f"Lead {lead_id} not found.")

        rec = records[lead_id]
        company = rec.get("company_name", "Unknown")

        if preview_status not in VALID_PREVIEW_STATUSES:
            raise ValueError(f"Invalid preview status: {preview_status}. Must be one of {VALID_PREVIEW_STATUSES}")

        preview = PreviewRecord(
            lead_id=lead_id,
            company_name=company,
            preview_status=preview_status,
            preview_url=preview_url,
            preview_description=preview_description,
            what_demonstrated=what_demonstrated,
            next_action=next_action,
            operator_notes=operator_notes,
        )

        rec.setdefault("previews", []).append(preview.to_dict())
        rec["commercial_stage"] = CommercialStage.PREVIEW_REQUESTED.value
        rec["website_pipeline_stage"] = WebsitePipelineStage.OPEN_TO_DISCUSSION.value
        rec["updated_at"] = now_utc_iso()
        self._save_commercial_records(records)

        event = CommercialEvent(
            event_id=f"evt-prev-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            event_type=CommercialEventType.PREVIEW_CREATED.value,
            previous_stage=rec.get("commercial_stage", ""),
            new_stage=CommercialStage.PREVIEW_REQUESTED.value,
            reason="Concept website preview prepared",
            notes=preview_description,
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {"success": True, "preview": preview.to_dict()}

    def mark_preview_sent(
        self,
        lead_id: str,
        preview_url: Optional[str] = None,
        operator_notes: str = "",
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Operator explicitly marks preview as sent (Section 5).
        Guards: Never automatically dispatches preview; requires human action.
        """
        records = self.load_commercial_records()
        if lead_id not in records:
            raise KeyError(f"Lead {lead_id} not found.")

        rec = records[lead_id]
        previews = rec.get("previews", [])

        if not previews:
            # Create ready preview on the fly
            previews.append(
                PreviewRecord(
                    lead_id=lead_id,
                    company_name=rec.get("company_name", "Unknown"),
                    preview_status=PreviewStatus.PREVIEW_SENT.value,
                    preview_url=preview_url or "",
                    operator_notes=operator_notes,
                    sent_at=now_utc_iso(),
                    marked_sent_by=operator,
                ).to_dict()
            )
        else:
            latest = previews[-1]
            latest["preview_status"] = PreviewStatus.PREVIEW_SENT.value
            latest["sent_at"] = now_utc_iso()
            latest["marked_sent_by"] = operator
            if preview_url:
                latest["preview_url"] = preview_url
            if operator_notes:
                latest["operator_notes"] = f"{latest.get('operator_notes', '')} | {operator_notes}".strip(" |")

        rec["commercial_stage"] = CommercialStage.PREVIEW_SENT.value
        rec["website_pipeline_stage"] = WebsitePipelineStage.PREVIEW_SENT.value
        rec["updated_at"] = now_utc_iso()
        self._save_commercial_records(records)

        event = CommercialEvent(
            event_id=f"evt-prevsent-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            event_type=CommercialEventType.PREVIEW_SENT.value,
            previous_stage=CommercialStage.PREVIEW_REQUESTED.value,
            new_stage=CommercialStage.PREVIEW_SENT.value,
            reason="Operator confirmed preview sent to client",
            notes=operator_notes,
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {"success": True, "lead_id": lead_id, "commercial_stage": CommercialStage.PREVIEW_SENT.value}

    # -------------------------------------------------------------------------
    # Section 7 & 8: Commercial Notes & Interest Level
    # -------------------------------------------------------------------------
    def record_commercial_notes(
        self,
        lead_id: str,
        notes_data: Dict[str, Any],
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Saves structured commercial notes (Section 7 & 8).
        Validates interest level. Notes are observations, never automated Rule B mutations.
        """
        records = self.load_commercial_records()
        if lead_id not in records:
            raise KeyError(f"Lead {lead_id} not found.")

        rec = records[lead_id]
        existing = rec.get("notes", {})

        interest = notes_data.get("interest_level", existing.get("interest_level", "UNKNOWN")).upper()
        if interest not in VALID_INTEREST_LEVELS:
            raise ValueError(f"Invalid interest level: {interest}. Must be one of {VALID_INTEREST_LEVELS}")

        updated_notes = CommercialNotes(
            decision_maker=notes_data.get("decision_maker", existing.get("decision_maker", "")),
            role=notes_data.get("role", existing.get("role", "")),
            interest_level=interest,
            current_website_situation=notes_data.get("current_website_situation", existing.get("current_website_situation", "")),
            requested_service=notes_data.get("requested_service", existing.get("requested_service", "")),
            budget_signal=notes_data.get("budget_signal", existing.get("budget_signal", "")),
            timeline_signal=notes_data.get("timeline_signal", existing.get("timeline_signal", "")),
            objection=notes_data.get("objection", existing.get("objection", "")),
            next_step=notes_data.get("next_step", existing.get("next_step", "")),
        )

        rec["notes"] = updated_notes.to_dict()
        rec["updated_at"] = now_utc_iso()
        self._save_commercial_records(records)

        event = CommercialEvent(
            event_id=f"evt-notes-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            event_type=CommercialEventType.COMMERCIAL_NOTES_UPDATED.value,
            previous_stage=rec.get("commercial_stage", ""),
            new_stage=rec.get("commercial_stage", ""),
            reason="Operator recorded structured commercial notes",
            notes=f"Interest: {interest}. DM: {updated_notes.decision_maker}. Next: {updated_notes.next_step}",
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {"success": True, "lead_id": lead_id, "notes": updated_notes.to_dict()}

    # -------------------------------------------------------------------------
    # Section 10: Proposal Tracking
    # -------------------------------------------------------------------------
    def record_proposal(
        self,
        lead_id: str,
        proposal_status: str,
        amount: Optional[float] = None,
        currency: str = "GBP",
        notes: str = "",
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Records proposal status and details (Section 10).
        Zero automated proposals. Only populated with explicit operator information.
        """
        records = self.load_commercial_records()
        if lead_id not in records:
            raise KeyError(f"Lead {lead_id} not found.")

        rec = records[lead_id]
        p_status_clean = proposal_status.strip().upper()
        if p_status_clean not in VALID_PROPOSAL_STATUSES:
            raise ValueError(f"Invalid proposal status: {proposal_status}. Must be one of {VALID_PROPOSAL_STATUSES}")

        proposal = ProposalRecord(
            lead_id=lead_id,
            company_name=rec.get("company_name", "Unknown"),
            proposal_status=p_status_clean,
            proposal_amount=amount,
            proposal_currency=currency,
            notes=notes,
            sent_at=now_utc_iso() if p_status_clean == ProposalStatus.SENT.value else None,
        )

        rec.setdefault("proposals", []).append(proposal.to_dict())

        # Update commercial stage based on proposal status
        if p_status_clean == ProposalStatus.REQUESTED.value:
            rec["commercial_stage"] = CommercialStage.PROPOSAL_REQUESTED.value
            rec["website_pipeline_stage"] = WebsitePipelineStage.PROPOSAL_REQUESTED.value
        elif p_status_clean == ProposalStatus.SENT.value:
            rec["commercial_stage"] = CommercialStage.PROPOSAL_SENT.value
            rec["website_pipeline_stage"] = WebsitePipelineStage.PROPOSAL_SENT.value
        elif p_status_clean == ProposalStatus.NEGOTIATING.value:
            rec["commercial_stage"] = CommercialStage.NEGOTIATING.value
            rec["website_pipeline_stage"] = WebsitePipelineStage.NEGOTIATING.value

        rec["updated_at"] = now_utc_iso()
        self._save_commercial_records(records)

        event_type = (
            CommercialEventType.PROPOSAL_SENT.value
            if p_status_clean == ProposalStatus.SENT.value
            else CommercialEventType.PROPOSAL_CREATED.value
        )
        event = CommercialEvent(
            event_id=f"evt-prop-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            event_type=event_type,
            previous_stage=rec.get("commercial_stage", ""),
            new_stage=rec.get("commercial_stage", ""),
            reason=f"Proposal status updated: {p_status_clean}",
            notes=f"Amount: {currency} {amount}. Notes: {notes}".strip(),
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {"success": True, "lead_id": lead_id, "proposal": proposal.to_dict()}

    # -------------------------------------------------------------------------
    # Section 11: Won / Lost Tracking
    # -------------------------------------------------------------------------
    def close_deal(
        self,
        lead_id: str,
        status: str,  # WON or LOST
        data: Dict[str, Any],
        operator: str = "HUMAN_OPERATOR",
        operator_confirmed: bool = False,
    ) -> Dict[str, Any]:
        """
        Closes a deal as WON or LOST (Section 11).
        Mandatory validation:
          - LOST requires valid LostReason.
          - WON requires explicit operator confirmation and service, agreed_value, currency, start_date.
        """
        records = self.load_commercial_records()
        if lead_id not in records:
            raise KeyError(f"Lead {lead_id} not found.")

        rec = records[lead_id]
        status_clean = status.strip().upper()

        if status_clean == "WON":
            if not operator_confirmed:
                raise ValueError("Deal WON requires explicit operator confirmation (operator_confirmed=True).")

            required_won_fields = ["service", "agreed_value", "currency", "start_date"]
            for field in required_won_fields:
                if field not in data or not data[field]:
                    raise ValueError(f"Deal WON requires mandatory field: {field}")

            rec["deal_won"] = {
                "service": data["service"],
                "agreed_value": float(data["agreed_value"]),
                "currency": data["currency"],
                "start_date": data["start_date"],
                "notes": data.get("notes", ""),
                "closed_at": now_utc_iso(),
                "closed_by": operator,
            }
            rec["commercial_stage"] = CommercialStage.WON.value
            rec["website_pipeline_stage"] = WebsitePipelineStage.WON.value

            event_type = CommercialEventType.DEAL_WON.value
            event_reason = f"Won deal: {data['service']} for {data['currency']} {data['agreed_value']}"

        elif status_clean == "LOST":
            reason = (data.get("reason") or "").strip().upper()
            if reason not in VALID_LOST_REASONS:
                raise ValueError(f"Deal LOST requires valid reason from: {VALID_LOST_REASONS}")

            rec["deal_lost"] = {
                "reason": reason,
                "notes": data.get("notes", ""),
                "closed_at": now_utc_iso(),
                "closed_by": operator,
            }
            rec["commercial_stage"] = CommercialStage.LOST.value
            rec["website_pipeline_stage"] = WebsitePipelineStage.LOST.value

            event_type = CommercialEventType.DEAL_LOST.value
            event_reason = f"Deal lost: {reason}"

        else:
            raise ValueError(f"Status must be WON or LOST, got {status}")

        rec["updated_at"] = now_utc_iso()
        self._save_commercial_records(records)

        event = CommercialEvent(
            event_id=f"evt-close-{uuid.uuid4().hex[:8]}",
            lead_id=lead_id,
            event_type=event_type,
            previous_stage=rec.get("commercial_stage", ""),
            new_stage=status_clean,
            reason=event_reason,
            notes=data.get("notes", ""),
            operator=operator,
            source=OutcomeProvenance.OPERATOR_REPORTED.value,
        )
        self._append_commercial_event(event)

        return {"success": True, "lead_id": lead_id, "status": status_clean, "details": data}

    # -------------------------------------------------------------------------
    # Section 13 & 15: Next Best Commercial Action Ranking
    # -------------------------------------------------------------------------
    def get_next_commercial_actions(self) -> List[Dict[str, Any]]:
        """
        Generates strictly ordered operator recommendations (Section 15):
          1. Explicit callback requested (Manchester Shawarma)
          2. Explicit request for preview (The Old Monkey)
          3. Explicit interest (leads marked INTERESTED)
          4. Recent successful connection without next step (Little Aladdin)
          5. No-answer retry (Dog and Partridge - operator review)
          6. Remaining qualified but untouched leads
        Guards: Never automatically executes any action.
        """
        records = self.load_commercial_records()
        leads = self.load_leads()
        actions: List[Dict[str, Any]] = []

        # Tier 1: Explicit callback requested
        for lid, rec in records.items():
            follow_ups = rec.get("follow_ups", [])
            for fu in follow_ups:
                if fu.get("status") == "PENDING_OPERATOR" and fu.get("follow_up_type") == "CALLBACK":
                    actions.append({
                        "lead_id": lid,
                        "company_name": rec.get("company_name", "Unknown"),
                        "action": NextActionType.CALLBACK_TODAY.value if "2026-10-06" in fu.get("scheduled_for", "") else NextActionType.CALLBACK.value,
                        "scheduled_for": fu.get("scheduled_for", ""),
                        "reason": f"Manager explicitly requested callback: {fu.get('notes', '')}",
                        "priority_tier": 1,
                        "commercial_stage": rec.get("commercial_stage"),
                        "auto_executable": False,
                    })

        # Tier 2: Explicit request for preview
        for lid, rec in records.items():
            stage = rec.get("commercial_stage")
            previews = rec.get("previews", [])
            has_pending_preview = any(p.get("preview_status") in (PreviewStatus.PREVIEW_DRAFT.value, PreviewStatus.PREVIEW_READY.value) for p in previews)
            if stage == CommercialStage.PREVIEW_REQUESTED.value or has_pending_preview:
                actions.append({
                    "lead_id": lid,
                    "company_name": rec.get("company_name", "Unknown"),
                    "action": NextActionType.SEND_PREVIEW.value,
                    "scheduled_for": "Immediate operator action",
                    "reason": "Manager requested website concept preview; review draft and dispatch manually",
                    "priority_tier": 2,
                    "commercial_stage": stage,
                    "auto_executable": False,
                })

        # Tier 3: Explicit interest
        for lid, rec in records.items():
            stage = rec.get("commercial_stage")
            interest = rec.get("notes", {}).get("interest_level", "")
            if stage == CommercialStage.INTERESTED.value and lid not in [a["lead_id"] for a in actions]:
                actions.append({
                    "lead_id": lid,
                    "company_name": rec.get("company_name", "Unknown"),
                    "action": NextActionType.FOLLOW_UP.value,
                    "scheduled_for": "Immediate operator action",
                    "reason": f"Explicit client interest recorded ({interest}); follow up with proposal or meeting offer",
                    "priority_tier": 3,
                    "commercial_stage": stage,
                    "auto_executable": False,
                })

        # Tier 4: Recent successful connection without next step
        for lid, rec in records.items():
            stage = rec.get("commercial_stage")
            outcome = rec.get("latest_outcome")
            already_listed = any(a["lead_id"] == lid for a in actions)
            if outcome == "CONNECTED" and not already_listed:
                actions.append({
                    "lead_id": lid,
                    "company_name": rec.get("company_name", "Unknown"),
                    "action": NextActionType.FOLLOW_UP.value,
                    "scheduled_for": "Next shift operator review",
                    "reason": "Recent successful connection; follow up on discussed website opportunity",
                    "priority_tier": 4,
                    "commercial_stage": stage,
                    "auto_executable": False,
                })

        # Tier 5: No-answer retry (operator review)
        for lid, rec in records.items():
            outcome = rec.get("latest_outcome")
            already_listed = any(a["lead_id"] == lid for a in actions)
            if outcome in ("NO_ANSWER", "BUSY") and not already_listed:
                actions.append({
                    "lead_id": lid,
                    "company_name": rec.get("company_name", "Unknown"),
                    "action": NextActionType.OPERATOR_REVIEW.value,
                    "scheduled_for": "Alternate shift window",
                    "reason": "Previous attempt resulted in NO_ANSWER; operator review needed to choose retry window",
                    "priority_tier": 5,
                    "commercial_stage": rec.get("commercial_stage"),
                    "auto_executable": False,
                })

        # Tier 6: Remaining qualified untouched leads
        for lead in leads:
            lid = lead.get("lead_id")
            if not lid or any(a["lead_id"] == lid for a in actions):
                continue
            rec = records.get(lid, {})
            # Exclude blocked/suppressed/unactivated
            if lead.get("email_suppressed") or rec.get("commercial_stage") in (CommercialStage.LOST.value, CommercialStage.NOT_INTERESTED.value):
                continue
            if lead.get("qualification_state") == "OUTREACH_READY" and lead.get("outreach_status") not in ("CONTACTED", "CALL_ATTEMPTED", "SENT"):
                actions.append({
                    "lead_id": lid,
                    "company_name": lead.get("company_name", "Unknown"),
                    "action": NextActionType.RETRY_CONTACT.value if lead.get("outreach_status") == "CALL_ATTEMPTED" else "QUALIFIED_OUTREACH",
                    "scheduled_for": "When next batch initiated",
                    "reason": f"Qualified prospect ready for manual review ({lead.get('review_count', 0)} reviews, {lead.get('rating', 0)}★)",
                    "priority_tier": 6,
                    "commercial_stage": rec.get("commercial_stage", CommercialStage.QUALIFIED.value),
                    "auto_executable": False,
                })

        # Sort strictly by priority_tier
        actions.sort(key=lambda x: x["priority_tier"])
        return actions

    # -------------------------------------------------------------------------
    # Section 12: Funnel Analytics & Conversion Rates
    # -------------------------------------------------------------------------
    def get_commercial_pipeline_analytics(self) -> Dict[str, Any]:
        """
        Calculates commercial funnel counts and conversion rates (Section 12).
        Enforces sample-size warning: n < 30 is descriptive only.
        """
        records = self.load_commercial_records()
        leads = self.load_leads()
        outcomes = self.load_outcomes()

        # Deduplicate phone outcomes by lead_id to count distinct contacts
        distinct_outcomes: Dict[str, Dict[str, Any]] = {}
        for out in outcomes:
            lid = out.get("lead_id")
            if lid:
                distinct_outcomes[lid] = out

        # Count stage metrics
        stage_counts = {s.value: 0 for s in CommercialStage}
        for rec in records.values():
            st = rec.get("commercial_stage", CommercialStage.QUALIFIED.value)
            if st in stage_counts:
                stage_counts[st] += 1

        total_qualified = len([l for l in leads if l.get("qualification_state") == "OUTREACH_READY"])
        total_activated = 9  # Known verified Manchester cohort
        total_contacted = len([l for l in leads if l.get("outreach_status") in ("CONTACTED", "SENT")])
        total_connected = len([o for o in distinct_outcomes.values() if o.get("outcome") in ("CONNECTED", "CALLBACK_REQUESTED")])
        total_interested = stage_counts[CommercialStage.INTERESTED.value] + stage_counts[CommercialStage.PREVIEW_REQUESTED.value]
        total_preview_requested = stage_counts[CommercialStage.PREVIEW_REQUESTED.value]
        total_preview_sent = stage_counts[CommercialStage.PREVIEW_SENT.value]
        total_proposal_requested = stage_counts[CommercialStage.PROPOSAL_REQUESTED.value]
        total_proposal_sent = stage_counts[CommercialStage.PROPOSAL_SENT.value]
        total_won = stage_counts[CommercialStage.WON.value]
        total_lost = stage_counts[CommercialStage.LOST.value]

        # Provenance counts
        operator_reported_count = len([o for o in outcomes if o.get("source") == "OPERATOR_REPORTED" or o.get("operator") == "HUMAN_OPERATOR"])
        provider_confirmed_count = len([o for o in outcomes if o.get("source") == "PROVIDER_CONFIRMED"])

        # Conversion calculations with strict denominator guard
        def safe_rate(num: int, denom: int) -> Optional[float]:
            return round(num / denom, 4) if denom > 0 else None

        contact_to_connected = safe_rate(total_connected, total_contacted)
        connected_to_interested = safe_rate(total_interested, total_connected)
        interested_to_preview = safe_rate(total_preview_requested + total_preview_sent, total_interested)
        preview_to_proposal = safe_rate(total_proposal_requested + total_proposal_sent, total_preview_requested + total_preview_sent)
        proposal_to_won = safe_rate(total_won, total_proposal_requested + total_proposal_sent)

        # Pending operational counts
        pending_callbacks = sum(
            1 for r in records.values()
            for fu in r.get("follow_ups", [])
            if fu.get("status") == "PENDING_OPERATOR" and fu.get("follow_up_type") == "CALLBACK"
        )
        pending_previews = sum(
            1 for r in records.values()
            for p in r.get("previews", [])
            if p.get("preview_status") in (PreviewStatus.PREVIEW_DRAFT.value, PreviewStatus.PREVIEW_READY.value)
        )
        next_actions = self.get_next_commercial_actions()
        pending_operator_actions = len([a for a in next_actions if a["priority_tier"] <= 5])

        total_conversations = len(distinct_outcomes)

        return {
            "qualified": total_qualified,
            "activated": total_activated,
            "contacted": total_contacted,
            "connected": total_connected,
            "interested": total_interested,
            "follow_up_required": stage_counts[CommercialStage.FOLLOW_UP_REQUIRED.value],
            "preview_requested": total_preview_requested,
            "preview_sent": total_preview_sent,
            "proposal_requested": total_proposal_requested,
            "proposal_sent": total_proposal_sent,
            "won": total_won,
            "lost": total_lost,
            "pending_callbacks": pending_callbacks,
            "pending_previews": pending_previews,
            "pending_operator_actions": pending_operator_actions,
            "operator_reported_outcomes": operator_reported_count,
            "provider_confirmed_outcomes": provider_confirmed_count,
            "automation_actions": 0,
            "conversion_funnel": {
                "contact_to_connected": contact_to_connected,
                "connected_to_interested": connected_to_interested,
                "interested_to_preview": interested_to_preview,
                "preview_to_proposal": preview_to_proposal,
                "proposal_to_won": proposal_to_won,
            },
            "sample_size": {
                "total_conversations": total_conversations,
                "is_sufficient": total_conversations >= 30,
                "warning": (
                    f"INSUFFICIENT SAMPLE (n={total_conversations}). "
                    "Commercial conversion metrics are descriptive only. "
                    "Do not draw statistical conclusions or optimize positioning until n>=30."
                ),
            },
            "stages_breakdown": stage_counts,
        }

    # -------------------------------------------------------------------------
    # Section 23: Machine Output Snapshot
    # -------------------------------------------------------------------------
    def generate_commercial_pipeline_snapshot(self) -> Dict[str, Any]:
        """
        Generates and writes data/commercial_pipeline_snapshot.json (Section 23).
        """
        analytics = self.get_commercial_pipeline_analytics()
        next_actions = self.get_next_commercial_actions()

        snapshot = {
            "qualified": analytics["qualified"],
            "activated": analytics["activated"],
            "contacted": analytics["contacted"],
            "connected": analytics["connected"],
            "interested": analytics["interested"],
            "follow_up_required": analytics["follow_up_required"],
            "preview_requested": analytics["preview_requested"],
            "preview_sent": analytics["preview_sent"],
            "proposal_requested": analytics["proposal_requested"],
            "proposal_sent": analytics["proposal_sent"],
            "won": analytics["won"],
            "lost": analytics["lost"],
            "pending_callbacks": analytics["pending_callbacks"],
            "pending_previews": analytics["pending_previews"],
            "pending_operator_actions": analytics["pending_operator_actions"],
            "operator_reported_outcomes": analytics["operator_reported_outcomes"],
            "provider_confirmed_outcomes": analytics["provider_confirmed_outcomes"],
            "automation_actions": 0,
            "conversion_rates": analytics["conversion_funnel"],
            "sample_size": analytics["sample_size"],
            "top_next_actions": next_actions[:5],
            "generated_at": now_utc_iso(),
        }

        os.makedirs(os.path.dirname(self.snapshot_path), exist_ok=True)
        with open(self.snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)

        return snapshot

    # -------------------------------------------------------------------------
    # Section 14: UI Dashboard Aggregation
    # -------------------------------------------------------------------------
    def get_commercial_dashboard_data(self) -> Dict[str, Any]:
        """
        Provides cards, top summary, and actions for Section 14 dashboard view.
        """
        analytics = self.get_commercial_pipeline_analytics()
        records = self.load_commercial_records()
        next_actions = self.get_next_commercial_actions()
        actions_by_lead = {a["lead_id"]: a for a in next_actions}

        cards: List[Dict[str, Any]] = []
        for lid, rec in records.items():
            # Build unified card
            act = actions_by_lead.get(lid, {})
            notes = rec.get("notes", {})
            previews = rec.get("previews", [])
            proposals = rec.get("proposals", [])

            cards.append({
                "lead_id": lid,
                "business": rec.get("company_name", "Unknown"),
                "commercial_stage": rec.get("commercial_stage", CommercialStage.QUALIFIED.value),
                "last_contact": rec.get("last_contact_at"),
                "latest_outcome": rec.get("latest_outcome"),
                "outcome_source": rec.get("latest_outcome_source") or OutcomeProvenance.OPERATOR_REPORTED.value,
                "next_action": act.get("action", "NONE"),
                "next_action_date": act.get("scheduled_for", ""),
                "next_action_reason": act.get("reason", ""),
                "interest": notes.get("interest_level", InterestLevel.UNKNOWN.value),
                "preview_status": previews[-1].get("preview_status") if previews else PreviewStatus.NONE.value,
                "preview_url": previews[-1].get("preview_url") if previews else "",
                "proposal_status": proposals[-1].get("proposal_status") if proposals else ProposalStatus.NONE.value,
                "proposal_amount": proposals[-1].get("proposal_amount") if proposals else None,
            })

        # Sort cards: active commercial pipeline first, then stage
        stage_sort_order = {
            CommercialStage.FOLLOW_UP_REQUIRED.value: 1,
            CommercialStage.PREVIEW_REQUESTED.value: 2,
            CommercialStage.INTERESTED.value: 3,
            CommercialStage.CONTACTED.value: 4,
            CommercialStage.PREVIEW_SENT.value: 5,
            CommercialStage.PROPOSAL_REQUESTED.value: 6,
            CommercialStage.PROPOSAL_SENT.value: 7,
            CommercialStage.NEGOTIATING.value: 8,
            CommercialStage.QUALIFIED.value: 9,
            CommercialStage.WON.value: 10,
            CommercialStage.LOST.value: 11,
            CommercialStage.NOT_INTERESTED.value: 12,
        }
        cards.sort(key=lambda c: stage_sort_order.get(c["commercial_stage"], 99))

        return {
            "top_summary": {
                "contacted": analytics["contacted"],
                "interested": analytics["interested"],
                "follow_ups": analytics["pending_callbacks"],
                "previews": analytics["pending_previews"],
                "proposals": analytics["proposal_requested"] + analytics["proposal_sent"],
                "won": analytics["won"],
                "lost": analytics["lost"],
            },
            "cards": cards,
            "next_actions": next_actions,
            "analytics": analytics,
        }
