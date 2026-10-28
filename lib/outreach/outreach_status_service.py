"""
lib/outreach/outreach_status_service.py
=======================================
Service for managing lead outreach states, check/uncheck status,
granular outcome filtering, and live synchronization to Google Sheets CRM.

Supports:
- Check/Uncheck "Reached" status
- Filtering by how outreach went:
  * Closed client (Won)
  * Meeting booked / Interested
  * Didn't pick up call (No answer)
  * Didn't reply / Ghosted
  * Not interested / Lost
  * Invalid lead (Wrong number, closed, bad fit)
  * Pending outreach
- Live Google Sheets persistence via GoogleSheetsStorageProvider
- Local JSON cache synchronization for zero-latency UI updates
- Immutable append-only audit & message history logging
"""

import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
CACHE_LEADS_PATH = os.path.join(DATA_DIR, "cache_sheets_leads.json")
CACHE_REVIEW_PATH = os.path.join(DATA_DIR, "cache_sheets_review_queue.json")
OUTCOMES_LOG_PATH = os.path.join(DATA_DIR, "outreach_outcomes.json")
MESSAGE_HISTORY_PATH = os.path.join(DATA_DIR, "message_history.json")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# Predefined granular taxonomy for outreach outcomes & aspects
OUTREACH_TAXONOMY = {
    "CLOSED_WON": {
        "label": "Closed Client (Deal Won)",
        "badge_color": "var(--accent-yellow)",
        "group": "POSITIVE",
        "default_lead_status": "CLOSED_WON",
        "default_outreach_status": "CLOSED_WON",
        "default_sales_stage": "WON",
        "default_client_status": "ACTIVE",
        "default_response_status": "REPLIED",
        "default_call_outcome": "CONVERTED",
        "reasons": [
            {"id": "proposal_accepted", "label": "Accepted Website Proposal"},
            {"id": "deposit_paid", "label": "Paid Initial Deposit"},
            {"id": "monthly_retainer", "label": "Signed Monthly SEO / Retainer"},
            {"id": "full_package_closed", "label": "Full Website & Branding Package Closed"},
            {"id": "custom_agreement", "label": "Custom Contract Finalized"}
        ]
    },
    "MEETING_BOOKED": {
        "label": "Meeting Booked / Scheduled",
        "badge_color": "var(--accent-cyan)",
        "group": "POSITIVE",
        "default_lead_status": "IN_PROGRESS",
        "default_outreach_status": "MEETING_BOOKED",
        "default_sales_stage": "MEETING_BOOKED",
        "default_client_status": "PROSPECT",
        "default_response_status": "REPLIED",
        "default_call_outcome": "MEETING_BOOKED",
        "reasons": [
            {"id": "owner_call_scheduled", "label": "Call Scheduled with Owner / Decision Maker"},
            {"id": "in_person_demo_booked", "label": "In-Person Demo / Visit Booked"},
            {"id": "zoom_walkthrough_booked", "label": "Zoom / Virtual Demo Booked"},
            {"id": "callback_requested", "label": "Owner Requested Callback at Specific Time"}
        ]
    },
    "INTERESTED": {
        "label": "Interested (Wants Details)",
        "badge_color": "#4ADE80",
        "group": "POSITIVE",
        "default_lead_status": "IN_PROGRESS",
        "default_outreach_status": "INTERESTED",
        "default_sales_stage": "QUALIFIED_CONVERSATION",
        "default_client_status": "PROSPECT",
        "default_response_status": "REPLIED",
        "default_call_outcome": "INTERESTED",
        "reasons": [
            {"id": "requested_pricing", "label": "Requested Pricing & Packages"},
            {"id": "asked_for_mockup", "label": "Asked for Example Website Mockup"},
            {"id": "considering_website", "label": "Considering Website - Discussing Internally"},
            {"id": "owner_positive", "label": "Owner Was Enthusiastic & Open"}
        ]
    },
    "NO_ANSWER": {
        "label": "Didn't Pick Up Call (No Answer)",
        "badge_color": "#F59E0B",
        "group": "UNANSWERED",
        "default_lead_status": "NO_ANSWER",
        "default_outreach_status": "NO_ANSWER",
        "default_sales_stage": "CONTACTED",
        "default_client_status": "PROSPECT",
        "default_call_status": "NO_ANSWER",
        "default_call_outcome": "NO_ANSWER",
        "default_response_status": "NO_RESPONSE",
        "reasons": [
            {"id": "rang_out_no_answer", "label": "Phone Rang Out (No Answer)"},
            {"id": "busy_line", "label": "Line Busy"},
            {"id": "voicemail_left", "label": "Left Detailed Voicemail"},
            {"id": "straight_to_voicemail", "label": "Call Went Straight to Voicemail"},
            {"id": "staff_gatekeeper_busy", "label": "Staff Answered - Manager Unavailable"}
        ]
    },
    "NO_REPLY": {
        "label": "Didn't Reply / Ghosted",
        "badge_color": "#A855F7",
        "group": "UNANSWERED",
        "default_lead_status": "CONTACTED",
        "default_outreach_status": "NO_REPLY",
        "default_sales_stage": "CONTACTED",
        "default_client_status": "PROSPECT",
        "default_response_status": "NO_RESPONSE",
        "reasons": [
            {"id": "dm_delivered_no_reply", "label": "DM Delivered - No Reply (48h+)"},
            {"id": "email_opened_no_reply", "label": "Email Opened / Delivered - No Reply"},
            {"id": "seen_on_social_no_reply", "label": "Read / Seen on Social - No Reply"},
            {"id": "ghosted_after_initial_chat", "label": "Ghosted After Initial Discussion"}
        ]
    },
    "NOT_INTERESTED": {
        "label": "Not Interested / Declined",
        "badge_color": "#94A3B8",
        "group": "NEGATIVE",
        "default_lead_status": "LOST",
        "default_outreach_status": "NOT_INTERESTED",
        "default_sales_stage": "LOST",
        "default_client_status": "PROSPECT",
        "default_response_status": "REPLIED",
        "default_call_outcome": "NOT_INTERESTED",
        "reasons": [
            {"id": "budget_constraints", "label": "Budget Constraints / Too Expensive"},
            {"id": "social_media_sufficient", "label": "Satisfied with Social Media Only"},
            {"id": "bad_timing", "label": "Too Busy / Bad Timing Right Now"},
            {"id": "no_interest_plain", "label": "Simply Not Interested"}
        ]
    },
    "INVALID_LEAD": {
        "label": "Invalid Lead (Wrong # / Closed)",
        "badge_color": "#EF4444",
        "group": "INVALID",
        "default_lead_status": "INVALID",
        "default_outreach_status": "INVALID_LEAD",
        "default_sales_stage": "LOST",
        "default_client_status": "PROSPECT",
        "default_call_outcome": "WRONG_NUMBER",
        "default_response_status": "INVALID_LEAD",
        "reasons": [
            {"id": "wrong_number_disconnected", "label": "Wrong Number / Disconnected Line"},
            {"id": "business_permanently_closed", "label": "Business Permanently Closed"},
            {"id": "already_has_agency", "label": "Already Has An Agency / Web Team"},
            {"id": "personal_number", "label": "Personal Number - Not A Business"},
            {"id": "not_a_fit", "label": "Not A Commercial Fit"}
        ]
    },
    "DO_NOT_CONTACT": {
        "label": "Do Not Contact / Opt Out",
        "badge_color": "#DC2626",
        "group": "SUPPRESSION",
        "default_lead_status": "DO_NOT_CONTACT",
        "default_outreach_status": "DO_NOT_CONTACT",
        "default_sales_stage": "DO_NOT_CONTACT",
        "default_client_status": "PROSPECT",
        "default_response_status": "DO_NOT_CONTACT",
        "default_call_outcome": "DO_NOT_CONTACT",
        "reasons": [
            {"id": "explicit_opt_out", "label": "Explicitly Requested Do Not Contact"},
            {"id": "hostile_response", "label": "Hostile / Aggressive Response"}
        ]
    },
    "CONTACTED": {
        "label": "Reached (General)",
        "badge_color": "#3B82F6",
        "group": "CONTACTED",
        "default_lead_status": "CONTACTED",
        "default_outreach_status": "CONTACTED",
        "default_sales_stage": "CONTACTED",
        "default_client_status": "PROSPECT",
        "reasons": [
            {"id": "general_message_sent", "label": "Message / DM Sent"},
            {"id": "call_completed", "label": "Call Completed"},
            {"id": "contact_initiated", "label": "Contact Initiated"}
        ]
    }
}


class OutreachStatusService:
    """Service to handle lead outreach state mutations and CRM sync."""

    @classmethod
    def get_taxonomy(cls) -> Dict[str, Any]:
        """Returns the full outcome taxonomy and reasons for frontend rendering."""
        return OUTREACH_TAXONOMY

    @classmethod
    def update_outreach_status(
        cls,
        lead_id: str,
        is_reached: bool,
        outcome_category: Optional[str] = None,
        outcome_reason: Optional[str] = None,
        notes: Optional[str] = "",
        channel: Optional[str] = "PHONE",
        sync_sheets: bool = True
    ) -> Dict[str, Any]:
        """
        Updates outreach status for a lead.
        
        Args:
            lead_id: Canonical lead ID (e.g. LEAD-MAN-682E5D)
            is_reached: True if reached/contacted, False if unchecked/pending
            outcome_category: CLOSED_WON, MEETING_BOOKED, INTERESTED, NO_ANSWER, NO_REPLY, etc.
            outcome_reason: Human-readable reason or reason ID
            notes: Operator conversation notes
            channel: PHONE, INSTAGRAM, FACEBOOK, EMAIL
            sync_sheets: If True, writes update directly to Google Sheets
        """
        now_iso = _now_utc()
        fields_to_update: Dict[str, Any] = {}

        if not is_reached:
            # RESET / UNCHECK BACK TO PENDING
            fields_to_update = {
                "outreach_status": "NOT_READY",
                "lead_status": "NOT_CONTACTED",
                "call_status": "NOT_CALLED",
                "call_outcome": "",
                "response_status": "",
                "response_type": "",
                "sales_stage": "NEW",
                "client_status": "PROSPECT",
                "actual_send_confirmed": False,
                "manual_outreach_notes": f"[Unchecked by operator at {now_iso[:19].replace('T', ' ')} UTC: Reset to Pending Outreach]",
                "last_contact_at": ""
            }
            status_summary = "NOT_READY"
            readable_outcome = "Pending Outreach"
        else:
            cat_key = (outcome_category or "CONTACTED").upper()
            cat_cfg = OUTREACH_TAXONOMY.get(cat_key, OUTREACH_TAXONOMY["CONTACTED"])

            outreach_status = cat_cfg["default_outreach_status"]
            lead_status = cat_cfg["default_lead_status"]
            sales_stage = cat_cfg["default_sales_stage"]
            client_status = cat_cfg["default_client_status"]
            resp_status = cat_cfg.get("default_response_status", "")
            call_outcome = cat_cfg.get("default_call_outcome", "")
            call_status = cat_cfg.get("default_call_status", "CALLED" if channel == "PHONE" else "NOT_CALLED")

            # Formulate detailed notes
            formatted_notes = f"[{cat_cfg['label']}]"
            if outcome_reason:
                formatted_notes += f" Reason: {outcome_reason}."
            if notes and notes.strip():
                formatted_notes += f" Notes: {notes.strip()}."
            formatted_notes += f" ({now_iso[:19].replace('T', ' ')} UTC)"

            fields_to_update = {
                "outreach_status": outreach_status,
                "lead_status": lead_status,
                "sales_stage": sales_stage,
                "client_status": client_status,
                "call_status": call_status,
                "call_outcome": call_outcome,
                "response_status": resp_status,
                "response_type": cat_key if cat_key in ["INTERESTED", "NOT_INTERESTED"] else "",
                "outreach_channel": (channel or "PHONE").upper(),
                "outreach_mode": "MANUAL",
                "actual_send_confirmed": True,
                "manual_outreach_notes": formatted_notes,
                "last_contact_at": now_iso
            }
            status_summary = outreach_status
            readable_outcome = cat_cfg["label"]

        # 1. Update local disk caches (cache_sheets_leads.json, cache_sheets_review_queue.json)
        updated_lead = cls._update_local_cache(lead_id, fields_to_update)

        # 2. Append to outcomes log and message history
        cls._append_outcome_history(
            lead_id=lead_id,
            is_reached=is_reached,
            outcome=status_summary,
            channel=channel or "PHONE",
            reason=outcome_reason or "",
            notes=notes or "",
            now_iso=now_iso
        )

        # 3. Synchronize live to Google Sheets
        sheets_synced = False
        if sync_sheets:
            try:
                from lib.sheets.google_sheets import GoogleSheetsStorageProvider
                storage = GoogleSheetsStorageProvider()
                sheets_synced = storage.update_lead_outreach(lead_id, fields_to_update)
            except Exception as e:
                print(f"[OutreachStatusService] Google Sheets live update failed for {lead_id}: {e}")
                sheets_synced = False

        return {
            "status": "ok",
            "success": True,
            "lead_id": lead_id,
            "is_reached": is_reached,
            "outreach_status": status_summary,
            "lead_status": fields_to_update.get("lead_status", ""),
            "outcome_label": readable_outcome,
            "sales_stage": fields_to_update.get("sales_stage", ""),
            "client_status": fields_to_update.get("client_status", ""),
            "call_status": fields_to_update.get("call_status", ""),
            "call_outcome": fields_to_update.get("call_outcome", ""),
            "response_status": fields_to_update.get("response_status", ""),
            "notes": fields_to_update.get("manual_outreach_notes", ""),
            "last_contact_at": fields_to_update.get("last_contact_at", ""),
            "fields_updated": fields_to_update,
            "sheets_synced": sheets_synced,
            "lead": updated_lead
        }

    @classmethod
    def _update_local_cache(cls, lead_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Updates local leads caches so the site reflects changes instantly."""
        found_lead = None

        for cache_path in [CACHE_LEADS_PATH, CACHE_REVIEW_PATH]:
            if not os.path.exists(cache_path):
                continue
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                # Check leads or queue
                leads_list = data.get("leads") if "leads" in data else data.get("queue", [])
                modified = False
                for lead in leads_list:
                    if str(lead.get("lead_id", "")).strip() == lead_id.strip():
                        lead.update(fields)
                        found_lead = dict(lead)
                        modified = True
                        break
                
                if modified:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception as ex:
                print(f"[OutreachStatusService] Cache update error in {cache_path}: {ex}")

        return found_lead

    @classmethod
    def _append_outcome_history(
        cls,
        lead_id: str,
        is_reached: bool,
        outcome: str,
        channel: str,
        reason: str,
        notes: str,
        now_iso: str
    ):
        """Maintains persistent outcome and message history on disk."""
        os.makedirs(DATA_DIR, exist_ok=True)

        # Update outreach_outcomes.json
        outcomes_list = []
        if os.path.exists(OUTCOMES_LOG_PATH):
            try:
                with open(OUTCOMES_LOG_PATH, "r", encoding="utf-8") as f:
                    outcomes_list = json.load(f)
            except Exception:
                outcomes_list = []
        
        entry = {
            "lead_id": lead_id,
            "is_reached": is_reached,
            "channel": channel.upper(),
            "outreach_status": outcome,
            "reason": reason,
            "notes": notes,
            "updated_at": now_iso
        }
        # Deduplicate by lead_id
        outcomes_list = [o for o in outcomes_list if str(o.get("lead_id")) != str(lead_id)]
        outcomes_list.append(entry)
        try:
            with open(OUTCOMES_LOG_PATH, "w", encoding="utf-8") as f:
                json.dump(outcomes_list, f, indent=2)
        except Exception:
            pass

        # Append to message_history.json
        history = {}
        if os.path.exists(MESSAGE_HISTORY_PATH):
            try:
                with open(MESSAGE_HISTORY_PATH, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = {}

        if lead_id not in history:
            history[lead_id] = []
        
        history[lead_id].append({
            "timestamp": now_iso,
            "channel": channel.upper(),
            "action": "OUTREACH_STATUS_UPDATED" if is_reached else "OUTREACH_RESET_PENDING",
            "outcome": outcome,
            "reason": reason,
            "notes": notes,
            "is_reached": is_reached
        })
        try:
            with open(MESSAGE_HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
        except Exception:
            pass
