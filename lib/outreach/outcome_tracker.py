"""
Dripp Media — Outreach Outcome Tracker
========================================
Section 22 (Post-Send Tracking):
Tracks everything that happens AFTER a message is sent.

Strict rules:
  - SENT ≠ DELIVERED. Do not auto-upgrade SENT to DELIVERED.
  - response_status stays AWAITING_RESPONSE until evidence changes it.
  - Response types are user-classified, never inferred automatically.
  - Call outcomes are user-recorded, never inferred.
  - Follow-ups are scheduled, NEVER auto-sent.
  - DO_NOT_CONTACT triggers persistent suppression.
  - Message history is append-only (never overwrite a sent message).
  - All state changes are persisted to data/outcome_tracking.json.
  - CRM sync happens after every mutation.
"""

import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data"
)
OUTCOME_FILE = os.path.join(DATA_DIR, "outcome_tracking.json")
MESSAGE_HISTORY_FILE = os.path.join(DATA_DIR, "message_history.json")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────────
# STATUS ENUMERATIONS  (Section spec: §3, §4, §5, §6)
# ──────────────────────────────────────────────────────────────────────────

class ResponseStatus:
    AWAITING_RESPONSE = "AWAITING_RESPONSE"
    NO_RESPONSE       = "NO_RESPONSE"
    REPLIED           = "REPLIED"
    BOUNCED           = "BOUNCED"
    OPTED_OUT         = "OPTED_OUT"
    DO_NOT_CONTACT    = "DO_NOT_CONTACT"

    ALL = [
        AWAITING_RESPONSE, NO_RESPONSE, REPLIED, BOUNCED,
        OPTED_OUT, DO_NOT_CONTACT,
    ]


class ResponseType:
    INTERESTED       = "INTERESTED"
    NOT_INTERESTED   = "NOT_INTERESTED"
    NEEDS_MORE_INFO  = "NEEDS_MORE_INFO"
    CALL_BACK        = "CALL_BACK"
    MEETING_REQUEST  = "MEETING_REQUEST"
    WRONG_CONTACT    = "WRONG_CONTACT"
    OTHER            = "OTHER"

    ALL = [
        INTERESTED, NOT_INTERESTED, NEEDS_MORE_INFO, CALL_BACK,
        MEETING_REQUEST, WRONG_CONTACT, OTHER,
    ]


class CallStatus:
    NOT_CALLED  = "NOT_CALLED"
    CALLED      = "CALLED"
    NO_ANSWER   = "NO_ANSWER"
    CALL_BACK   = "CALL_BACK"
    COMPLETED   = "COMPLETED"

    ALL = [NOT_CALLED, CALLED, NO_ANSWER, CALL_BACK, COMPLETED]


class CallOutcome:
    INTERESTED      = "INTERESTED"
    NOT_INTERESTED  = "NOT_INTERESTED"
    MEETING_BOOKED  = "MEETING_BOOKED"
    WRONG_NUMBER    = "WRONG_NUMBER"
    DO_NOT_CONTACT  = "DO_NOT_CONTACT"
    CONVERTED       = "CONVERTED"
    OTHER           = "OTHER"

    ALL = [
        INTERESTED, NOT_INTERESTED, MEETING_BOOKED, WRONG_NUMBER,
        DO_NOT_CONTACT, CONVERTED, OTHER,
    ]


class SalesStage:
    NEW                   = "NEW"
    CONTACTED             = "CONTACTED"
    RESPONDED             = "RESPONDED"
    QUALIFIED_CONVERSATION = "QUALIFIED_CONVERSATION"
    MEETING_BOOKED        = "MEETING_BOOKED"
    PROPOSAL_SENT         = "PROPOSAL_SENT"
    NEGOTIATION           = "NEGOTIATION"
    WON                   = "WON"
    LOST                  = "LOST"
    DO_NOT_CONTACT        = "DO_NOT_CONTACT"

    ALL = [
        NEW, CONTACTED, RESPONDED, QUALIFIED_CONVERSATION, MEETING_BOOKED,
        PROPOSAL_SENT, NEGOTIATION, WON, LOST, DO_NOT_CONTACT,
    ]


class MeetingStatus:
    NONE      = "NONE"
    REQUESTED = "REQUESTED"
    BOOKED    = "BOOKED"
    HELD      = "HELD"
    NO_SHOW   = "NO_SHOW"
    CANCELLED = "CANCELLED"

    ALL = [NONE, REQUESTED, BOOKED, HELD, NO_SHOW, CANCELLED]


class ProposalStatus:
    NONE      = "NONE"
    DRAFTED   = "DRAFTED"
    SENT      = "SENT"
    ACCEPTED  = "ACCEPTED"
    REJECTED  = "REJECTED"

    ALL = [NONE, DRAFTED, SENT, ACCEPTED, REJECTED]


class ClientStatus:
    PROSPECT   = "PROSPECT"
    ACTIVE     = "ACTIVE"
    PAUSED     = "PAUSED"
    CHURNED    = "CHURNED"

    ALL = [PROSPECT, ACTIVE, PAUSED, CHURNED]


class MessageDirection:
    OUTBOUND = "OUTBOUND"
    INBOUND  = "INBOUND"


# ──────────────────────────────────────────────────────────────────────────
# OUTCOME STORE I/O
# ──────────────────────────────────────────────────────────────────────────

def _load_outcomes() -> Dict[str, Any]:
    """Load all tracked outcomes keyed by lead_id."""
    _ensure_dir()
    try:
        with open(OUTCOME_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_outcomes(data: Dict[str, Any]):
    _ensure_dir()
    with open(OUTCOME_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def _load_history() -> Dict[str, List[Dict[str, Any]]]:
    """Load message history keyed by lead_id."""
    _ensure_dir()
    try:
        with open(MESSAGE_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_history(data: Dict[str, List[Dict[str, Any]]]):
    _ensure_dir()
    with open(MESSAGE_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# ──────────────────────────────────────────────────────────────────────────
# DEFAULT OUTCOME RECORD
# ──────────────────────────────────────────────────────────────────────────

def _default_outcome(lead_id: str, campaign_id: str = "", channel: str = "") -> Dict[str, Any]:
    """Canonical structure for a fresh post-send outcome record."""
    return {
        "lead_id": lead_id,
        "campaign_id": campaign_id,
        "channel": channel,
        "created_at": _now_utc(),
        "updated_at": _now_utc(),

        # §3 Response status
        "outreach_status": "SENT",
        "response_status": ResponseStatus.AWAITING_RESPONSE,
        "response_received_at": "",
        "response_type": "",

        # §5 Call tracking
        "call_status": CallStatus.NOT_CALLED,
        "call_outcome": "",
        "call_notes": "",

        # §6 Sales pipeline
        "sales_stage": SalesStage.CONTACTED,

        # Funnel milestones
        "meeting_status": MeetingStatus.NONE,
        "proposal_status": ProposalStatus.NONE,
        "client_status": ClientStatus.PROSPECT,
        "revenue": 0.0,

        # §7 Follow-up
        "follow_up_required": False,
        "next_follow_up_at": "",
        "follow_up_number": 0,
        "follow_up_notes": "",

        # Misc
        "last_contact_at": _now_utc(),
        "notes": "",
    }


# ──────────────────────────────────────────────────────────────────────────
# OUTCOME TRACKER (public API)
# ──────────────────────────────────────────────────────────────────────────

class OutcomeTracker:
    """
    All post-send outcome mutations for a lead.
    Every public method:
      1. Validates the action is legal.
      2. Persists the change to data/outcome_tracking.json.
      3. Records an event in the audit log (the outcome record's 'events' list).
      4. Syncs to Google Sheets CRM (fire-and-forget, non-fatal on failure).
    """

    # ── Initialise / get record ───────────────────────────────────────────

    @classmethod
    def get_or_create(
        cls,
        lead_id: str,
        campaign_id: str = "",
        channel: str = "",
        initial_outreach_status: str = "SENT",
    ) -> Dict[str, Any]:
        """
        Retrieve existing outcome record or create a fresh one.
        Does NOT overwrite an existing record — safe to call multiple times.
        """
        outcomes = _load_outcomes()
        if lead_id not in outcomes:
            rec = _default_outcome(lead_id, campaign_id, channel)
            rec["outreach_status"] = initial_outreach_status
            outcomes[lead_id] = rec
            _save_outcomes(outcomes)
        return outcomes[lead_id]

    @classmethod
    def get(cls, lead_id: str) -> Optional[Dict[str, Any]]:
        return _load_outcomes().get(lead_id)

    # ── Response recording (§3, §4) ───────────────────────────────────────

    @classmethod
    def mark_replied(
        cls,
        lead_id: str,
        response_type: str,
        notes: str = "",
        received_at: str = "",
    ) -> Dict[str, Any]:
        """
        Mark that the prospect replied. Moves sales_stage to RESPONDED.
        Response type must be provided by the user (never inferred).
        """
        if response_type not in ResponseType.ALL:
            return {"error": f"Invalid response_type '{response_type}'. Must be one of: {ResponseType.ALL}"}

        outcomes = _load_outcomes()
        rec = outcomes.get(lead_id, _default_outcome(lead_id))
        rec["response_status"]     = ResponseStatus.REPLIED
        rec["response_type"]       = response_type
        rec["response_received_at"] = received_at or _now_utc()
        rec["sales_stage"]         = SalesStage.RESPONDED
        rec["last_contact_at"]     = _now_utc()
        rec["updated_at"]          = _now_utc()
        if notes:
            rec["notes"] = notes
        cls._append_event(rec, "mark_replied", {"response_type": response_type, "notes": notes})
        outcomes[lead_id] = rec
        _save_outcomes(outcomes)
        cls._sync_sheets(lead_id, rec)
        return rec

    @classmethod
    def mark_bounced(cls, lead_id: str, notes: str = "") -> Dict[str, Any]:
        outcomes = _load_outcomes()
        rec = outcomes.get(lead_id, _default_outcome(lead_id))
        rec["response_status"] = ResponseStatus.BOUNCED
        rec["updated_at"]      = _now_utc()
        if notes:
            rec["notes"] = notes
        cls._append_event(rec, "mark_bounced", {"notes": notes})
        outcomes[lead_id] = rec
        _save_outcomes(outcomes)
        cls._sync_sheets(lead_id, rec)
        return rec

    @classmethod
    def mark_opted_out(cls, lead_id: str, channel: str = "", recipient: str = "") -> Dict[str, Any]:
        """
        Opt-out: marks the response and triggers global suppression.
        Future send gate will block this lead on all channels.
        """
        outcomes = _load_outcomes()
        rec = outcomes.get(lead_id, _default_outcome(lead_id))
        rec["response_status"] = ResponseStatus.OPTED_OUT
        rec["sales_stage"]     = SalesStage.DO_NOT_CONTACT
        rec["updated_at"]      = _now_utc()
        cls._append_event(rec, "opted_out", {"channel": channel, "recipient": recipient})
        outcomes[lead_id] = rec
        _save_outcomes(outcomes)

        # §12 Trigger persistent suppression
        cls._add_suppression(lead_id, channel, recipient, reason="OPT_OUT_REPLY")
        cls._sync_sheets(lead_id, rec)
        return rec

    # ── DNC (§12) ─────────────────────────────────────────────────────────

    @classmethod
    def mark_do_not_contact(
        cls,
        lead_id: str,
        reason: str = "USER_MARKED",
        channel: str = "",
        recipient: str = "",
    ) -> Dict[str, Any]:
        """
        Hard DO_NOT_CONTACT. Triggers global suppression across all channels.
        The send gate checks suppression before any future send.
        """
        outcomes = _load_outcomes()
        rec = outcomes.get(lead_id, _default_outcome(lead_id))
        rec["response_status"] = ResponseStatus.DO_NOT_CONTACT
        rec["sales_stage"]     = SalesStage.DO_NOT_CONTACT
        rec["updated_at"]      = _now_utc()
        cls._append_event(rec, "do_not_contact", {"reason": reason})
        outcomes[lead_id] = rec
        _save_outcomes(outcomes)

        # Global suppression (no channel = global block)
        cls._add_suppression(lead_id, channel="", recipient=recipient, reason=reason)
        cls._sync_sheets(lead_id, rec)
        return rec

    # ── Interested / Not interested (convenience, §4) ─────────────────────

    @classmethod
    def mark_interested(cls, lead_id: str, notes: str = "") -> Dict[str, Any]:
        outcomes = _load_outcomes()
        rec = outcomes.get(lead_id, _default_outcome(lead_id))
        rec["response_type"] = ResponseType.INTERESTED
        if rec.get("response_status") == ResponseStatus.AWAITING_RESPONSE:
            rec["response_status"] = ResponseStatus.REPLIED
            rec["response_received_at"] = _now_utc()
        rec["sales_stage"] = SalesStage.QUALIFIED_CONVERSATION
        rec["updated_at"]  = _now_utc()
        if notes:
            rec["notes"] = notes
        cls._append_event(rec, "mark_interested", {"notes": notes})
        outcomes[lead_id] = rec
        _save_outcomes(outcomes)
        cls._sync_sheets(lead_id, rec)
        return rec

    @classmethod
    def mark_not_interested(cls, lead_id: str, notes: str = "") -> Dict[str, Any]:
        outcomes = _load_outcomes()
        rec = outcomes.get(lead_id, _default_outcome(lead_id))
        rec["response_type"] = ResponseType.NOT_INTERESTED
        if rec.get("response_status") == ResponseStatus.AWAITING_RESPONSE:
            rec["response_status"] = ResponseStatus.REPLIED
            rec["response_received_at"] = _now_utc()
        rec["sales_stage"] = SalesStage.LOST
        rec["updated_at"]  = _now_utc()
        if notes:
            rec["notes"] = notes
        cls._append_event(rec, "mark_not_interested", {"notes": notes})
        outcomes[lead_id] = rec
        _save_outcomes(outcomes)
        cls._sync_sheets(lead_id, rec)
        return rec

    # ── Note (§8 UI) ──────────────────────────────────────────────────────

    @classmethod
    def add_note(cls, lead_id: str, note: str) -> Dict[str, Any]:
        outcomes = _load_outcomes()
        rec = outcomes.get(lead_id, _default_outcome(lead_id))
        existing = rec.get("notes", "")
        ts = _now_utc()
        rec["notes"] = f"{existing}\n[{ts}] {note}".strip() if existing else f"[{ts}] {note}"
        rec["updated_at"] = ts
        cls._append_event(rec, "add_note", {"note": note[:200]})
        outcomes[lead_id] = rec
        _save_outcomes(outcomes)
        cls._sync_sheets(lead_id, rec)
        return rec

    # ── Follow-up tracking (§7) ───────────────────────────────────────────

    @classmethod
    def set_follow_up(
        cls,
        lead_id: str,
        follow_up_at: str,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Schedule a follow-up. Does NOT send anything automatically.
        follow_up_at: ISO 8601 string.
        """
        outcomes = _load_outcomes()
        rec = outcomes.get(lead_id, _default_outcome(lead_id))
        rec["follow_up_required"] = True
        rec["next_follow_up_at"]  = follow_up_at
        rec["follow_up_number"]   = rec.get("follow_up_number", 0) + 1
        rec["follow_up_notes"]    = notes
        rec["updated_at"]         = _now_utc()
        cls._append_event(rec, "set_follow_up", {"follow_up_at": follow_up_at, "notes": notes})
        outcomes[lead_id] = rec
        _save_outcomes(outcomes)
        cls._sync_sheets(lead_id, rec)
        return rec

    @classmethod
    def clear_follow_up(cls, lead_id: str) -> Dict[str, Any]:
        outcomes = _load_outcomes()
        rec = outcomes.get(lead_id, _default_outcome(lead_id))
        rec["follow_up_required"] = False
        rec["next_follow_up_at"]  = ""
        rec["updated_at"]         = _now_utc()
        cls._append_event(rec, "clear_follow_up", {})
        outcomes[lead_id] = rec
        _save_outcomes(outcomes)
        cls._sync_sheets(lead_id, rec)
        return rec

    # ── Call tracking (§5) ────────────────────────────────────────────────

    @classmethod
    def record_call(
        cls,
        lead_id: str,
        call_status: str,
        call_outcome: str = "",
        notes: str = "",
    ) -> Dict[str, Any]:
        if call_status not in CallStatus.ALL:
            return {"error": f"Invalid call_status '{call_status}'. Must be one of: {CallStatus.ALL}"}
        if call_outcome and call_outcome not in CallOutcome.ALL:
            return {"error": f"Invalid call_outcome '{call_outcome}'. Must be one of: {CallOutcome.ALL}"}

        outcomes = _load_outcomes()
        rec = outcomes.get(lead_id, _default_outcome(lead_id))
        rec["call_status"]  = call_status
        rec["call_outcome"] = call_outcome
        if notes:
            rec["call_notes"] = notes
        rec["last_contact_at"] = _now_utc()
        rec["updated_at"]      = _now_utc()

        # Propagate significant call outcomes to sales stage
        if call_outcome == CallOutcome.MEETING_BOOKED:
            rec["sales_stage"]   = SalesStage.MEETING_BOOKED
            rec["meeting_status"] = MeetingStatus.BOOKED
        elif call_outcome == CallOutcome.CONVERTED:
            rec["sales_stage"]   = SalesStage.WON
            rec["client_status"] = ClientStatus.ACTIVE
        elif call_outcome == CallOutcome.DO_NOT_CONTACT:
            rec["sales_stage"]   = SalesStage.DO_NOT_CONTACT
            cls._add_suppression(lead_id, channel="", recipient="", reason="CALL_DNC")

        cls._append_event(rec, "record_call", {
            "call_status": call_status, "call_outcome": call_outcome, "notes": notes[:200]
        })
        outcomes[lead_id] = rec
        _save_outcomes(outcomes)
        cls._sync_sheets(lead_id, rec)
        return rec

    # ── Sales stage (§6) ──────────────────────────────────────────────────

    @classmethod
    def set_sales_stage(cls, lead_id: str, stage: str, notes: str = "") -> Dict[str, Any]:
        if stage not in SalesStage.ALL:
            return {"error": f"Invalid sales_stage '{stage}'. Must be one of: {SalesStage.ALL}"}

        outcomes = _load_outcomes()
        rec = outcomes.get(lead_id, _default_outcome(lead_id))
        old_stage = rec.get("sales_stage", "")
        rec["sales_stage"] = stage
        rec["updated_at"]  = _now_utc()
        if notes:
            rec["notes"] = notes
        cls._append_event(rec, "set_sales_stage", {"from": old_stage, "to": stage})

        if stage == SalesStage.DO_NOT_CONTACT:
            rec["response_status"] = ResponseStatus.DO_NOT_CONTACT
            cls._add_suppression(lead_id, channel="", recipient="", reason="STAGE_DNC")

        outcomes[lead_id] = rec
        _save_outcomes(outcomes)
        cls._sync_sheets(lead_id, rec)
        return rec

    # ── Revenue (§2) ──────────────────────────────────────────────────────

    @classmethod
    def record_revenue(cls, lead_id: str, amount: float, notes: str = "") -> Dict[str, Any]:
        outcomes = _load_outcomes()
        rec = outcomes.get(lead_id, _default_outcome(lead_id))
        rec["revenue"]      = amount
        rec["client_status"] = ClientStatus.ACTIVE
        rec["sales_stage"]  = SalesStage.WON
        rec["updated_at"]   = _now_utc()
        if notes:
            rec["notes"] = notes
        cls._append_event(rec, "record_revenue", {"amount": amount})
        outcomes[lead_id] = rec
        _save_outcomes(outcomes)
        cls._sync_sheets(lead_id, rec)
        return rec

    # ── Suppression check (§12, §16.8) ────────────────────────────────────

    @classmethod
    def is_suppressed(cls, lead_id: str, channel: str = "", recipient: str = "") -> bool:
        """
        Primary suppression gate used by pre-send checks.
        Reads from the SuppressionManager (data/suppression_list.json).
        """
        from lib.outreach.compliance import SuppressionManager
        return SuppressionManager.is_suppressed(lead_id, channel, recipient)

    # ── Message history (§11) ─────────────────────────────────────────────

    @classmethod
    def append_message_history(
        cls,
        lead_id: str,
        channel: str,
        direction: str,
        status: str,
        message: str,
        sent_at: str = "",
        message_id: str = "",
    ):
        """
        Append-only. Historical entries are NEVER overwritten or deleted.
        direction: OUTBOUND | INBOUND
        """
        history = _load_history()
        if lead_id not in history:
            history[lead_id] = []

        entry = {
            "timestamp":  sent_at or _now_utc(),
            "channel":    channel,
            "direction":  direction,
            "status":     status,
            "message_id": message_id,
            "message":    message,
            "recorded_at": _now_utc(),
        }
        history[lead_id].append(entry)
        _save_history(history)

    @classmethod
    def get_message_history(cls, lead_id: str) -> List[Dict[str, Any]]:
        """Returns the full append-only message history for a lead."""
        return _load_history().get(lead_id, [])

    # ── Internal helpers ──────────────────────────────────────────────────

    @classmethod
    def _append_event(cls, rec: Dict[str, Any], event_type: str, data: Dict[str, Any]):
        if "events" not in rec:
            rec["events"] = []
        rec["events"].append({
            "event_type": event_type,
            "at": _now_utc(),
            "data": data,
        })

    @classmethod
    def _add_suppression(
        cls,
        lead_id: str,
        channel: str,
        recipient: str,
        reason: str,
    ):
        try:
            from lib.outreach.compliance import SuppressionManager
            SuppressionManager.add_suppression(
                identifier=lead_id,
                channel=channel or None,
                recipient=recipient or None,
                reason=reason
            )
        except Exception as e:
            print(f"[OutcomeTracker] Warning: suppression write failed: {e}")

    @classmethod
    def _sync_sheets(cls, lead_id: str, rec: Dict[str, Any]):
        """Non-fatal CRM sync. Maps outcome fields → Sheets column names."""
        fields = {
            "response_status":     rec.get("response_status", ""),
            "response_received_at": rec.get("response_received_at", ""),
            "call_status":         rec.get("call_status", ""),
            "call_outcome":        rec.get("call_outcome", ""),
            "call_notes":          rec.get("call_notes", ""),
            "sales_stage":         rec.get("sales_stage", ""),
            # Existing Sheets columns for follow-up (follow_up_enabled is the legacy name)
            "follow_up_enabled":   str(rec.get("follow_up_required", "")),
            "follow_up_required":  str(rec.get("follow_up_required", "")),
            "follow_up_at":        rec.get("next_follow_up_at", ""),
            "next_follow_up":      rec.get("next_follow_up_at", ""),
            "follow_up_number":    str(rec.get("follow_up_number", "")),
            "follow_up_notes":     rec.get("follow_up_notes", ""),
            "last_contact_at":     rec.get("last_contact_at", ""),
            "manual_outreach_notes": rec.get("notes", ""),
            "meeting_status":      rec.get("meeting_status", ""),
            "proposal_status":     rec.get("proposal_status", ""),
            "client_status":       rec.get("client_status", ""),
            "revenue":             str(rec.get("revenue", "")),
            "opt_out_status":      "OPTED_OUT" if rec.get("response_status") in (
                ResponseStatus.OPTED_OUT, ResponseStatus.DO_NOT_CONTACT
            ) else "ACTIVE",
        }
        # Only write response_type if set (not empty)
        if rec.get("response_type"):
            fields["response_type"] = rec["response_type"]

        try:
            from lib.sheets.google_sheets import GoogleSheetsStorageProvider
            storage = GoogleSheetsStorageProvider()
            ok = storage.update_lead_outreach(lead_id, fields)
            if ok:
                print(f"[CRM] Outcome synced for {lead_id}: {list(fields.keys())}")
            else:
                print(f"[CRM] Warning: lead {lead_id} not found in Sheets for outcome sync")
        except Exception as e:
            print(f"[CRM] Warning: outcome Sheets sync failed for {lead_id}: {e}")
