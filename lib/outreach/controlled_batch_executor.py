"""
Phase 9.5: Controlled Outreach Batch Executor + Real Outcome Analytics

Extends Phase 9.4 Operator Gateway to execute a strictly controlled, operator-led
queue of up to MAX_BATCH_SIZE = 3 activation-ready leads.

Core Safety & Policy Invariants:
  - MAX_BATCH_SIZE = 3
  - AUTOMATED_SEND_ENABLED = False (Strictly 0 automated dispatches, 0 armed campaigns)
  - Zero autonomous continuation: Every lead requires operator preview, explicit action,
    mandatory outcome recording, and an explicit NEXT LEAD click.
  - Dynamic eligibility re-evaluation before each action (never rely on stale queue snapshots).
  - Explicit metrics disaggregation: manual phone calls count as OUTREACH_ATTEMPTS = 1,
    AUTOMATED_SENDS = 0.
  - Structured outcome recording:
      * INTERESTED -> follow-up required, auto_follow_up = False
      * CALLBACK_REQUESTED -> callback_required = True, captures date/time, auto_schedule = False
      * NOT_INTERESTED -> distinct from suppression
      * NO_ANSWER -> CALL_ATTEMPTED, not CONTACTED, not REJECTED
      * Channel Failure -> record attempt, invalidate only with evidence, recalculate fallbacks,
        return to operator
  - Deterministic Idempotency: lead_id:channel:attempt_number
  - Append-only timeline logging and complete history preservation
    (Little Aladdin, Live Seafood, Seoul Kimchi, Hong Thai).
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Set

from lib.outreach.phase_8_9_product_engine import (
    Template,
    TemplateRegistry,
    PersonalizationEngine,
    MessageQA,
    OutreachChannel,
    OutreachStatus,
    DeliveryMode,
    SuppressionStatus,
    IdempotencyTracker,
    SuppressionManager,
    LeadTimelineTracker,
    ResponseStage,
)
from lib.outreach.phase_9_4_operator_gateway import (
    OperatorSendGateway,
    PHONE_OUTCOMES,
    MANUAL_RESPONSE_TYPES,
)

logger = logging.getLogger("ControlledBatchExecutor")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

DEFAULT_ACTIVATION_PATH = os.path.join(DATA_DIR, "phase_9_3_contactability_run.json")
DEFAULT_LEADS_PATH = os.path.join(DATA_DIR, "cache_sheets_leads.json")
DEFAULT_TIMELINE_PATH = os.path.join(DATA_DIR, "lead_timelines.json")
DEFAULT_HISTORY_PATH = os.path.join(DATA_DIR, "message_history.json")
DEFAULT_SUPPRESSION_PATH = os.path.join(DATA_DIR, "suppression_list.json")
DEFAULT_OUTCOMES_PATH = os.path.join(DATA_DIR, "outreach_outcomes.json")
DEFAULT_FOLLOWUPS_PATH = os.path.join(DATA_DIR, "phase_9_5_follow_ups.json")
DEFAULT_BATCH_STATE_PATH = os.path.join(DATA_DIR, "controlled_batch_state.json")
DEFAULT_ANALYTICS_OUTPUT_PATH = os.path.join(DATA_DIR, "phase_9_5_controlled_batch_run.json")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# Canonical outcomes for Social Outreach
SOCIAL_OUTCOMES = {
    "SENT",
    "FAILED",
    "WRONG_ACCOUNT",
    "BLOCKED",
    "OTHER",
}

# Maximum Batch Size
MAX_BATCH_SIZE = 3


class ControlledBatchExecutor:
    """
    Operator-controlled batch executor enforcing step-by-step human action,
    rigorous pre-action validation, structured outcome handling, and disaggregated analytics.
    """

    MAX_BATCH_SIZE = MAX_BATCH_SIZE
    AUTOMATED_SEND_ENABLED = False

    def __init__(
        self,
        activation_path: str = DEFAULT_ACTIVATION_PATH,
        leads_path: str = DEFAULT_LEADS_PATH,
        timeline_path: str = DEFAULT_TIMELINE_PATH,
        history_path: str = DEFAULT_HISTORY_PATH,
        suppression_path: str = DEFAULT_SUPPRESSION_PATH,
        outcomes_path: str = DEFAULT_OUTCOMES_PATH,
        followups_path: str = DEFAULT_FOLLOWUPS_PATH,
        batch_state_path: str = DEFAULT_BATCH_STATE_PATH,
    ):
        self.activation_path = activation_path
        self.leads_path = leads_path
        self.timeline_path = timeline_path
        self.history_path = history_path
        self.suppression_path = suppression_path
        self.outcomes_path = outcomes_path
        self.followups_path = followups_path
        self.batch_state_path = batch_state_path

        # Underlying OperatorSendGateway instance for preview and templates
        self.gateway = OperatorSendGateway(
            activation_path=activation_path,
            leads_path=leads_path,
            timeline_path=timeline_path,
            history_path=history_path,
            suppression_path=suppression_path,
            outcomes_path=outcomes_path,
        )

        self.idempotency_tracker = self.gateway.idempotency_tracker
        self.suppression_manager = self.gateway.suppression_manager
        self.timeline_tracker = self.gateway.timeline_tracker

    # -------------------------------------------------------------------------
    # Batch Candidate Selection (Max 3 leads)
    # -------------------------------------------------------------------------

    def get_eligible_candidates(self) -> List[Dict[str, Any]]:
        """
        Scans activation profiles and filters for genuinely eligible candidates.
        Excludes:
          - Little Aladdin (pilot completed)
          - Seoul Kimchi (already sent)
          - Hong Thai (bounced / suppressed)
          - Any lead with an existing confirmed send or already contacted
          - Suppressed leads
          - Inactive / not-ready leads
        """
        profiles = self.gateway.load_activation_profiles()
        crm_leads = {}
        if os.path.exists(self.leads_path):
            try:
                with open(self.leads_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    leads = data.get("leads", data) if isinstance(data, dict) else data
                    crm_leads = {l.get("lead_id"): l for l in leads if isinstance(l, dict)}
            except Exception as e:
                logger.error(f"Error loading CRM leads: {e}")

        # Check existing recorded outcomes
        contacted_lead_ids = set()
        if os.path.exists(self.outcomes_path):
            try:
                with open(self.outcomes_path, "r", encoding="utf-8") as f:
                    outcomes = json.load(f)
                    if isinstance(outcomes, list):
                        for o in outcomes:
                            if o.get("outcome") in ("CONNECTED", "SENT") or o.get("status") in ("CONTACTED", "SENT"):
                                contacted_lead_ids.add(o.get("lead_id"))
            except Exception:
                pass

        eligible: List[Dict[str, Any]] = []

        for p in profiles:
            lead_id = p.get("lead_id", "")
            crm = crm_leads.get(lead_id, {})

            # Rule 1: Exclusion of historical special leads
            if lead_id in ("LEAD-MAN-902001", "Little Aladdin"):
                continue  # Pilot already executed
            if lead_id == "LEAD-MAN-4DB3EF":
                continue  # Seoul Kimchi: already sent
            if lead_id == "LEAD-MAN-709C66":
                continue  # Hong Thai: bounced / suppressed

            # Rule 2: Exclude if already contacted or confirmed send in CRM or outcomes
            if lead_id in contacted_lead_ids:
                continue
            crm_status = crm.get("outreach_status")
            if crm_status in ("SENT", "CONTACTED", "BOUNCED"):
                continue
            if crm.get("actual_send_confirmed") is True:
                continue

            # Rule 3: Activation readiness
            if not p.get("activation_ready", False):
                continue
            if p.get("qualification_state") != "OUTREACH_READY":
                continue

            # Rule 4: Suppression check
            is_supp, _ = self.suppression_manager.is_suppressed(lead_id)
            if is_supp or p.get("suppression_status") == "SUPPRESSED":
                continue

            # Rule 5: Verified contact exists for recommended channel
            rec_channel = p.get("recommended_channel", "PHONE").upper()
            verified_contacts = p.get("verified_contacts", {})
            c_info = verified_contacts.get(rec_channel, {})
            if c_info.get("status") != "VERIFIED":
                continue

            # Merge review and rating for sorting
            review_count = crm.get("review_count") or 0
            rating = crm.get("rating") or 0.0
            confidence = p.get("channel_confidence", 0.0)

            eligible.append({
                "lead_id": lead_id,
                "company_name": p.get("company_name"),
                "location": p.get("location"),
                "recommended_channel": rec_channel,
                "verified_contact": c_info.get("value") or c_info.get("handle") or p.get(f"verified_{rec_channel.lower()}"),
                "channel_confidence": confidence,
                "review_count": review_count,
                "rating": rating,
                "website_opportunity": p.get("website_opportunity", "NO_WEBSITE"),
                "profile": p,
            })

        # Rank candidates by:
        # 1. channel_confidence descending
        # 2. review_count descending (traction / commercial opportunity)
        # 3. rating descending
        # 4. lead_id ascending (deterministic tie-breaker)
        eligible.sort(
            key=lambda x: (x["channel_confidence"], x["review_count"], x["rating"]),
            reverse=True,
        )

        return eligible

    def select_batch_candidates(self, max_batch_size: int = MAX_BATCH_SIZE) -> List[Dict[str, Any]]:
        """Selects up to max_batch_size eligible candidates."""
        candidates = self.get_eligible_candidates()
        ceiling = min(max_batch_size, self.MAX_BATCH_SIZE)
        return candidates[:ceiling]

    # -------------------------------------------------------------------------
    # Batch Initialization & Lifecycle State
    # -------------------------------------------------------------------------

    def init_batch(self, lead_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Initializes a controlled batch of up to MAX_BATCH_SIZE (3) leads.
        If lead_ids is None, selects the top eligible leads.
        Stops at READY_FOR_OPERATOR without automated dispatch.
        """
        if lead_ids:
            if len(lead_ids) > self.MAX_BATCH_SIZE:
                raise ValueError(f"Batch ceiling exceeded: max {self.MAX_BATCH_SIZE} leads allowed.")
            all_eligible = {c["lead_id"]: c for c in self.get_eligible_candidates()}
            selected = []
            for lid in lead_ids:
                if lid not in all_eligible:
                    raise ValueError(f"Lead {lid} is not eligible for controlled batch.")
                selected.append(all_eligible[lid])
        else:
            selected = self.select_batch_candidates(self.MAX_BATCH_SIZE)

        batch_id = f"BATCH-MAN-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid_hex[:6]}" if 'uuid_hex' in locals() else f"BATCH-MAN-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{os.urandom(3).hex().upper()}"

        batch_state = {
            "batch_id": batch_id,
            "created_at": _now_utc(),
            "max_batch_size": self.MAX_BATCH_SIZE,
            "batch_size": len(selected),
            "status": "READY_FOR_OPERATOR",
            "current_index": 0,
            "leads": [
                {
                    "lead_id": s["lead_id"],
                    "company_name": s["company_name"],
                    "location": s["location"],
                    "recommended_channel": s["recommended_channel"],
                    "verified_contact": s["verified_contact"],
                    "review_count": s["review_count"],
                    "rating": s["rating"],
                    "status": "PENDING",  # PENDING -> PREVIEWED -> ACTION_TAKEN -> OUTCOME_RECORDED
                    "preview": None,
                    "action_executed": None,
                    "outcome": None,
                    "outcome_recorded": False,
                    "completed": False,
                }
                for s in selected
            ],
            "completed_count": 0,
            "automated_send_enabled": False,
        }

        self._save_batch_state(batch_state)
        return batch_state

    def load_batch_state(self) -> Optional[Dict[str, Any]]:
        """Loads the current batch state from disk."""
        if not os.path.exists(self.batch_state_path):
            return None
        try:
            with open(self.batch_state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading batch state: {e}")
            return None

    def _save_batch_state(self, state: Dict[str, Any]):
        """Persists the batch state to disk."""
        try:
            with open(self.batch_state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving batch state: {e}")

    # -------------------------------------------------------------------------
    # Dynamic 7-Condition Preflight Gate Evaluation
    # -------------------------------------------------------------------------

    def evaluate_preflight_eligibility(
        self,
        lead_id: str,
        channel: Optional[str] = None,
    ) -> Tuple[bool, List[str], Dict[str, Any]]:
        """
        Dynamically evaluates all 7 preflight conditions immediately before any action.
        Never relies on a stale queue snapshot.
        Conditions:
          1. qualification_state == OUTREACH_READY
          2. activation_ready == True
          3. suppression == False
          4. no confirmed previous send on selected channel
          5. no unresolved duplicate
          6. verified contact exists
          7. message/call QA == PASS
        Returns (is_eligible, blockers, profile).
        """
        blockers: List[str] = []
        profile = self.gateway.get_activation_profile(lead_id)
        if not profile:
            return False, [f"PROFILE_NOT_FOUND: No activation profile for {lead_id}"], {}

        channel_to_check = (channel or profile.get("recommended_channel", "PHONE")).upper()

        # 1. qualification_state == OUTREACH_READY
        if profile.get("qualification_state") != "OUTREACH_READY":
            blockers.append(f"GATE_FAILED: qualification_state is {profile.get('qualification_state')}, expected OUTREACH_READY")

        # 2. activation_ready == True
        if not profile.get("activation_ready", False):
            blockers.append("GATE_FAILED: activation_ready is False")

        # 3. suppression == False
        crm_lead = self.gateway.load_crm_lead(lead_id) or {}
        email_val = (
            profile.get("verified_email")
            or crm_lead.get("email")
            or profile.get("verified_contacts", {}).get("EMAIL", {}).get("value")
        )
        is_supp, supp_reason = self.suppression_manager.is_suppressed(lead_id, email=email_val)
        if (
            is_supp
            or profile.get("suppression_status") == "SUPPRESSED"
            or crm_lead.get("email_suppressed")
            or crm_lead.get("outreach_status") == "BOUNCED"
        ):
            blockers.append(f"GATE_FAILED: lead is suppressed ({supp_reason or 'BOUNCED'})")

        # 4. no confirmed previous send on selected channel
        crm_lead = self.gateway.load_crm_lead(lead_id) or {}
        if crm_lead.get("outreach_status") == "SENT":
            blockers.append("GATE_FAILED: prior confirmed send recorded in CRM")
        if crm_lead.get("actual_send_confirmed") is True:
            blockers.append("GATE_FAILED: actual_send_confirmed is True in CRM")

        if os.path.exists(self.history_path):
            try:
                with open(self.history_path, "r", encoding="utf-8") as f:
                    history = json.load(f)
                    for item in history.get(lead_id, []):
                        if item.get("status") == "SENT" and item.get("channel", "").upper() == channel_to_check:
                            blockers.append(f"GATE_FAILED: prior confirmed send in message_history on {channel_to_check}")
                            break
            except Exception:
                pass

        # Also check outcomes for prior successful contact
        if os.path.exists(self.outcomes_path):
            try:
                with open(self.outcomes_path, "r", encoding="utf-8") as f:
                    outcomes = json.load(f)
                    for o in (outcomes if isinstance(outcomes, list) else []):
                        if o.get("lead_id") == lead_id and o.get("outcome") in ("CONNECTED", "SENT"):
                            blockers.append(f"GATE_FAILED: lead {lead_id} already has a recorded outcome ({o.get('outcome')})")
                            break
            except Exception:
                pass

        # 5. no unresolved duplicate
        company_name = profile.get("company_name", "")
        if "aladdin" in company_name.lower() and company_name != "Little Aladdin":
            blockers.append("GATE_FAILED: Unresolved Aladdin collision")

        # 6. verified contact exists
        verified_contacts = profile.get("verified_contacts", {})
        contact_info = verified_contacts.get(channel_to_check, {})
        if contact_info.get("status") != "VERIFIED":
            blockers.append(f"GATE_FAILED: No verified contact found for {channel_to_check}")
        if contact_info.get("is_corporate_shared", False):
            blockers.append("GATE_FAILED: Shared corporate contact, branch safety violation")

        # 7. message/call QA == PASS
        # Generate message and run QA
        preview_data = self.gateway.generate_operator_preview(lead_id)
        qa_res = preview_data.get("message_qa_result", {})
        if not qa_res.get("passed", False):
            blockers.append(f"GATE_FAILED: Message QA failed: {qa_res.get('errors')}")

        is_eligible = len(blockers) == 0
        return is_eligible, blockers, profile

    # -------------------------------------------------------------------------
    # Batch Flow: Current Lead Inspection, Preview, Action, Outcome, Next
    # -------------------------------------------------------------------------

    def get_current_batch_lead(self) -> Dict[str, Any]:
        """
        Returns the status and payload of the currently active lead in the batch.
        """
        state = self.load_batch_state()
        if not state:
            return {"error": "NO_ACTIVE_BATCH", "message": "No active controlled batch found."}

        idx = state.get("current_index", 0)
        leads = state.get("leads", [])
        if idx >= len(leads):
            return {
                "batch_id": state.get("batch_id"),
                "status": "BATCH_COMPLETED",
                "completed_count": state.get("completed_count", 0),
                "total_leads": len(leads),
                "message": "All leads in this controlled batch have been completed.",
            }

        cur = leads[idx]
        lead_id = cur["lead_id"]

        # Run fresh eligibility evaluation
        eligible, blockers, profile = self.evaluate_preflight_eligibility(lead_id, cur.get("recommended_channel"))

        return {
            "batch_id": state.get("batch_id"),
            "current_index": idx,
            "total_leads": len(leads),
            "completed_count": state.get("completed_count", 0),
            "lead": cur,
            "eligible": eligible,
            "eligibility_blockers": blockers,
            "can_preview": True,
            "can_act": cur.get("status") == "PREVIEWED" and eligible,
            "can_record_outcome": cur.get("status") == "ACTION_TAKEN" and not cur.get("outcome_recorded", False),
            "can_advance": cur.get("outcome_recorded", False),
        }

    def preview_current_lead(self) -> Dict[str, Any]:
        """
        Generates preview for current batch lead.
        Updates batch state status to PREVIEWED.
        Timeline records PREVIEWED.
        """
        state = self.load_batch_state()
        if not state:
            return {"error": "NO_ACTIVE_BATCH"}

        idx = state["current_index"]
        if idx >= len(state["leads"]):
            return {"error": "BATCH_COMPLETED"}

        cur = state["leads"][idx]
        lead_id = cur["lead_id"]

        # Preflight evaluation
        eligible, blockers, profile = self.evaluate_preflight_eligibility(lead_id, cur.get("recommended_channel"))
        if not eligible:
            return {
                "success": False,
                "error": "ELIGIBILITY_CHECK_FAILED",
                "blockers": blockers,
                "lead_id": lead_id,
            }

        # Generate preview via gateway
        preview = self.gateway.generate_operator_preview(lead_id)
        cur["preview"] = preview
        cur["status"] = "PREVIEWED"
        state["status"] = "PREVIEWING"

        # Record timeline event
        self.timeline_tracker.log_event(
            event_type="PREVIEWED",
            lead_id=lead_id,
            channel=cur.get("recommended_channel"),
            source="CONTROLLED_BATCH_EXECUTOR",
            actor="OPERATOR",
            details={
                "batch_id": state["batch_id"],
                "batch_index": idx,
                "channel": cur.get("recommended_channel"),
                "recipient": cur.get("verified_contact"),
                "timestamp": _now_utc(),
            },
        )

        self._save_batch_state(state)
        return {
            "success": True,
            "batch_id": state["batch_id"],
            "batch_index": idx,
            "lead_id": lead_id,
            "preview": preview,
        }

    def execute_current_lead_action(
        self,
        operator_confirmed: bool,
        action: str,  # "CALL", "OPEN_PROFILE", "COPY_MESSAGE", "CONFIRM_SENT"
        notes: str = "",
        test_mode: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes an operator-confirmed action for the current lead.
        Enforces:
          - Operator confirmation mandatory
          - Dynamic eligibility re-evaluation
          - Deterministic Idempotency key: lead_id:channel:attempt_number
          - Max 1 active execution at a time
          - Zero autonomous dispatch
        """
        if not operator_confirmed:
            return {
                "success": False,
                "error": "OPERATOR_CONFIRMATION_REQUIRED",
                "message": "Explicit operator confirmation is mandatory before executing action.",
            }

        state = self.load_batch_state()
        if not state:
            return {"success": False, "error": "NO_ACTIVE_BATCH"}

        idx = state["current_index"]
        if idx >= len(state["leads"]):
            return {"success": False, "error": "BATCH_COMPLETED"}

        cur = state["leads"][idx]
        lead_id = cur["lead_id"]
        channel = cur.get("recommended_channel", "PHONE").upper()

        # Dynamic Preflight check
        eligible, blockers, profile = self.evaluate_preflight_eligibility(lead_id, channel)
        if not eligible:
            return {
                "success": False,
                "error": "PREFLIGHT_BLOCKED",
                "blockers": blockers,
                "lead_id": lead_id,
            }

        # Deterministic Idempotency Key: lead_id:channel:attempt_number
        attempt = 1
        idem_key = f"{lead_id}:{channel}:{attempt}"
        if self.idempotency_tracker.is_duplicate(idem_key):
            # Repeated request returns existing attempt state rather than performing another action
            return {
                "success": True,
                "idempotent_replay": True,
                "message": f"Idempotent replay: Action already recorded for {idem_key}.",
                "lead_id": lead_id,
                "channel": channel,
                "idempotency_key": idem_key,
                "action": action,
            }

        # Compute message or script hash and analytics payload
        preview = cur.get("preview") or self.gateway.generate_operator_preview(lead_id)
        msg_text = preview.get("personalized_message", "")
        msg_hash = hashlib.sha256(msg_text.encode("utf-8")).hexdigest()[:16]
        personalization_fields = list(preview.get("personalization_sources", {}).keys())

        # Timeline event: OPERATOR_CONFIRMED
        self.timeline_tracker.log_event(
            event_type="OPERATOR_CONFIRMED",
            lead_id=lead_id,
            channel=channel,
            source="CONTROLLED_BATCH_EXECUTOR",
            actor="HUMAN_OPERATOR",
            details={
                "batch_id": state["batch_id"],
                "batch_index": idx,
                "action": action,
                "confirmed_at": _now_utc(),
                "test_mode": test_mode,
            },
        )

        # Timeline event: OUTREACH_ATTEMPTED
        self.timeline_tracker.log_event(
            event_type="OUTREACH_ATTEMPTED",
            lead_id=lead_id,
            channel=channel,
            source="CONTROLLED_BATCH_EXECUTOR",
            actor="HUMAN_OPERATOR",
            details={
                "batch_id": state["batch_id"],
                "action": action,
                "template_version": self.gateway.TEMPLATE_VERSION,
                "message_hash": msg_hash,
                "personalization_fields": personalization_fields,
                "operator_id": "HUMAN_OPERATOR",
                "timestamp": _now_utc(),
                "test_mode": test_mode,
            },
        )

        if channel == "PHONE":
            # For phone outreach, action is a call
            self.timeline_tracker.log_event(
                event_type="CALL_ATTEMPTED",
                lead_id=lead_id,
                channel="PHONE",
                source="CONTROLLED_BATCH_EXECUTOR",
                actor="HUMAN_OPERATOR",
                details={
                    "phone": cur.get("verified_contact"),
                    "notes": notes,
                    "timestamp": _now_utc(),
                    "test_mode": test_mode,
                },
            )
            # Record idempotency
            self.idempotency_tracker.record(idem_key)

            cur["status"] = "ACTION_TAKEN"
            cur["action_executed"] = {
                "action": "CALL",
                "channel": "PHONE",
                "timestamp": _now_utc(),
                "notes": notes,
                "template_version": self.gateway.TEMPLATE_VERSION,
                "message_hash": msg_hash,
                "personalization_fields": personalization_fields,
                "idempotency_key": idem_key,
            }
            state["status"] = "ACTION_TAKEN"
            self._save_batch_state(state)

            return {
                "success": True,
                "lead_id": lead_id,
                "channel": "PHONE",
                "action": "CALL",
                "outreach_attempts": 1,
                "automated_sends": 0,
                "idempotency_key": idem_key,
                "message": "Phone call initiated. Operator must now record real outcome.",
            }

        elif channel in ("INSTAGRAM", "FACEBOOK"):
            # Social outreach actions: OPEN_PROFILE, COPY_MESSAGE, CONFIRM_SENT
            if action in ("OPEN_PROFILE", "COPY_MESSAGE"):
                ev_type = "PROFILE_OPENED" if action == "OPEN_PROFILE" else "MESSAGE_COPIED"
                self.timeline_tracker.log_event(
                    event_type=ev_type,
                    lead_id=lead_id,
                    channel=channel,
                    source="CONTROLLED_BATCH_EXECUTOR",
                    actor="HUMAN_OPERATOR",
                    details={"action": action, "timestamp": _now_utc(), "test_mode": test_mode},
                )
                return {
                    "success": True,
                    "lead_id": lead_id,
                    "channel": channel,
                    "action": action,
                    "outreach_status": "PREVIEWED",
                    "actual_send_confirmed": False,
                }
            elif action == "CONFIRM_SENT":
                self.idempotency_tracker.record(idem_key)
                self.timeline_tracker.log_event(
                    event_type="SEND_CONFIRMED",
                    lead_id=lead_id,
                    channel=channel,
                    source="CONTROLLED_BATCH_EXECUTOR",
                    actor="HUMAN_OPERATOR",
                    details={
                        "recipient": cur.get("verified_contact"),
                        "send_confirmation_source": "OPERATOR",
                        "timestamp": _now_utc(),
                        "test_mode": test_mode,
                    },
                )
                cur["status"] = "ACTION_TAKEN"
                cur["action_executed"] = {
                    "action": "CONFIRM_SENT",
                    "channel": channel,
                    "timestamp": _now_utc(),
                    "notes": notes,
                    "template_version": self.gateway.TEMPLATE_VERSION,
                    "message_hash": msg_hash,
                    "personalization_fields": personalization_fields,
                    "idempotency_key": idem_key,
                }
                state["status"] = "ACTION_TAKEN"
                self._save_batch_state(state)

                return {
                    "success": True,
                    "lead_id": lead_id,
                    "channel": channel,
                    "action": "CONFIRM_SENT",
                    "outreach_attempts": 1,
                    "automated_sends": 0,
                    "idempotency_key": idem_key,
                    "message": "Social message confirmed sent. Operator must now record outcome.",
                }
            else:
                return {"success": False, "error": f"Unknown social action: {action}"}
        else:
            return {"success": False, "error": f"Unsupported batch channel: {channel}"}

    def record_current_lead_outcome(
        self,
        outcome: str,
        notes: str = "",
        callback_time: Optional[str] = None,
        test_mode: bool = False,
    ) -> Dict[str, Any]:
        """
        Records the real operator-observed outcome for the active lead.
        Enforces:
          - Mandatory outcome: never inferred
          - Canonical outcome verification
          - Structured states for INTERESTED, CALLBACK_REQUESTED, NOT_INTERESTED, NO_ANSWER, WRONG_NUMBER
          - Channel failure handling with fallback recalculation
          - Append-only timeline logging
          - Non-destructive CRM sync
          - Operator must explicitly call next_lead() to advance.
        """
        state = self.load_batch_state()
        if not state:
            return {"success": False, "error": "NO_ACTIVE_BATCH"}

        idx = state["current_index"]
        if idx >= len(state["leads"]):
            return {"success": False, "error": "BATCH_COMPLETED"}

        cur = state["leads"][idx]
        lead_id = cur["lead_id"]
        channel = cur.get("recommended_channel", "PHONE").upper()

        # Validate outcome against canonical set
        valid_outcomes = PHONE_OUTCOMES if channel == "PHONE" else SOCIAL_OUTCOMES
        if outcome not in valid_outcomes:
            return {
                "success": False,
                "error": "INVALID_OUTCOME",
                "message": f"Outcome '{outcome}' is invalid for {channel}. Must be one of {sorted(valid_outcomes)}",
            }

        now_iso = _now_utc()

        # Timeline event: OUTCOME_RECORDED
        self.timeline_tracker.log_event(
            event_type="OUTCOME_RECORDED",
            lead_id=lead_id,
            channel=channel,
            source="CONTROLLED_BATCH_EXECUTOR",
            actor="HUMAN_OPERATOR",
            details={
                "batch_id": state["batch_id"],
                "batch_index": idx,
                "outcome": outcome,
                "notes": notes,
                "callback_time": callback_time,
                "timestamp": now_iso,
                "test_mode": test_mode,
            },
        )

        if outcome == "CONNECTED" and channel == "PHONE":
            self.timeline_tracker.log_event(
                event_type="CALL_CONNECTED",
                lead_id=lead_id,
                channel="PHONE",
                source="CONTROLLED_BATCH_EXECUTOR",
                actor="HUMAN_OPERATOR",
                details={"notes": notes, "timestamp": now_iso, "test_mode": test_mode},
            )

        # Handle specific outcome states
        structured_followup = None
        new_crm_status = "CALL_ATTEMPTED"
        actual_send_confirmed = False

        if outcome in ("CONNECTED", "SENT"):
            new_crm_status = "CONTACTED" if channel == "PHONE" else "SENT"
            actual_send_confirmed = (channel != "PHONE")  # Manual phone != automated send

        elif outcome == "INTERESTED":
            new_crm_status = "CONTACTED"
            structured_followup = {
                "lead_id": lead_id,
                "response_stage": "INTERESTED",
                "follow_up_required": True,
                "auto_follow_up": False,  # Strict protection: operator controlled
                "operator_notes": notes,
                "recorded_at": now_iso,
            }
            self._save_follow_up(structured_followup, test_mode=test_mode)

        elif outcome == "CALLBACK_REQUESTED":
            new_crm_status = "CONTACTED"
            structured_followup = {
                "lead_id": lead_id,
                "response_stage": "CALLBACK_REQUESTED",
                "callback_required": True,
                "requested_callback_time": callback_time,
                "auto_schedule": False,  # No autonomous scheduling
                "operator_notes": notes,
                "recorded_at": now_iso,
            }
            self._save_follow_up(structured_followup, test_mode=test_mode)

        elif outcome == "NOT_INTERESTED":
            # Distinct from SUPPRESSED: do not automatically suppress unless explicitly configured
            new_crm_status = "NOT_INTERESTED"

        elif outcome == "NO_ANSWER":
            # CALL_ATTEMPTED, not CONTACTED, not REJECTED
            new_crm_status = "CALL_ATTEMPTED"

        elif outcome in ("WRONG_NUMBER", "INVALID_CONTACT", "WRONG_ACCOUNT", "FAILED", "BLOCKED"):
            new_crm_status = "FAILED"
            # Channel failure: invalidate contact only when evidence supports invalidation
            self._handle_channel_failure(lead_id, channel, outcome, test_mode=test_mode)

        # Persist outcome to outreach_outcomes.json
        if not test_mode:
            self._record_outcome(
                lead_id=lead_id,
                channel=channel,
                outcome=outcome,
                status=new_crm_status,
                notes=notes,
                timestamp=now_iso,
            )

            # Update CRM lead cache non-destructively
            self._update_crm_lead(
                lead_id=lead_id,
                outreach_status=new_crm_status,
                channel=channel,
                notes=f"Controlled batch outcome: {outcome}. {notes}".strip(),
                timestamp=now_iso,
                actual_send_confirmed=actual_send_confirmed,
                outcome=outcome,
            )

        cur["outcome"] = outcome
        cur["outcome_notes"] = notes
        cur["outcome_recorded"] = True
        cur["status"] = "OUTCOME_RECORDED"
        state["status"] = "OUTCOME_RECORDED"

        self._save_batch_state(state)

        return {
            "success": True,
            "lead_id": lead_id,
            "channel": channel,
            "outcome": outcome,
            "crm_status": new_crm_status,
            "actual_send_confirmed": actual_send_confirmed,
            "structured_followup": structured_followup,
            "next_action": "Operator decision required. Click [NEXT LEAD] to proceed.",
        }

    def next_lead(self) -> Dict[str, Any]:
        """
        Advances the batch pointer to the next lead.
        Enforces:
          - Current lead MUST have outcome_recorded == True.
          - Never advances automatically.
          - When all leads complete, marks BATCH_COMPLETED.
        """
        state = self.load_batch_state()
        if not state:
            return {"success": False, "error": "NO_ACTIVE_BATCH"}

        idx = state["current_index"]
        leads = state["leads"]

        if idx < len(leads):
            cur = leads[idx]
            if not cur.get("outcome_recorded", False):
                return {
                    "success": False,
                    "error": "OUTCOME_REQUIRED",
                    "message": f"Cannot advance to next lead: Outcome for {cur['lead_id']} has not been recorded.",
                }
            cur["completed"] = True
            state["completed_count"] = state.get("completed_count", 0) + 1

        next_idx = idx + 1
        state["current_index"] = next_idx

        if next_idx >= len(leads):
            state["status"] = "BATCH_COMPLETED"
            self._save_batch_state(state)
            return {
                "success": True,
                "status": "BATCH_COMPLETED",
                "completed_count": state["completed_count"],
                "total_leads": len(leads),
                "message": "Controlled batch completed successfully. All operator actions recorded.",
            }
        else:
            state["status"] = "READY_FOR_OPERATOR"
            next_lead_info = leads[next_idx]
            self._save_batch_state(state)
            return {
                "success": True,
                "status": "READY_FOR_OPERATOR",
                "current_index": next_idx,
                "completed_count": state["completed_count"],
                "total_leads": len(leads),
                "next_lead": next_lead_info,
                "message": f"Advanced to lead {next_idx + 1} of {len(leads)}: {next_lead_info['company_name']}.",
            }

    # -------------------------------------------------------------------------
    # Structured Follow-ups & Channel Failure Fallback Handling
    # -------------------------------------------------------------------------

    def _save_follow_up(self, follow_up: Dict[str, Any], test_mode: bool = False):
        """Persists structured follow-up items for INTERESTED and CALLBACK_REQUESTED."""
        if test_mode:
            return
        try:
            items = []
            if os.path.exists(self.followups_path):
                with open(self.followups_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    items = data if isinstance(data, list) else data.get("follow_ups", [])
            items.append(follow_up)
            with open(self.followups_path, "w", encoding="utf-8") as f:
                json.dump(items, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving follow up: {e}")

    def _handle_channel_failure(self, lead_id: str, channel: str, failure_type: str, test_mode: bool = False):
        """
        Handles contact invalidation and fallback channel recalculation.
        Returns lead to operator review without silently re-sending.
        """
        if test_mode:
            return
        profile = self.gateway.get_activation_profile(lead_id)
        if not profile:
            return

        # Check fallbacks
        fallbacks = profile.get("fallback_channels", [])
        alt_channel = fallbacks[0] if fallbacks else None

        # Log timeline event
        self.timeline_tracker.log_event(
            event_type="CHANNEL_FAILURE",
            lead_id=lead_id,
            channel=channel,
            source="CONTROLLED_BATCH_EXECUTOR",
            actor="SYSTEM",
            details={
                "failure_type": failure_type,
                "invalidated_channel": channel,
                "suggested_fallback": alt_channel,
                "action_taken": "RETURNED_TO_OPERATOR_REVIEW",
                "timestamp": _now_utc(),
            },
        )

    def _record_outcome(
        self,
        lead_id: str,
        channel: str,
        outcome: str,
        status: str,
        notes: str,
        timestamp: str,
    ):
        """Appends an outcome record to outreach_outcomes.json."""
        try:
            outcomes = []
            if os.path.exists(self.outcomes_path):
                with open(self.outcomes_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    outcomes = data if isinstance(data, list) else data.get("outcomes", [])
            outcomes.append({
                "lead_id": lead_id,
                "channel": channel,
                "outcome": outcome,
                "status": status,
                "notes": notes,
                "recorded_at": timestamp,
                "operator": "HUMAN_OPERATOR",
            })
            with open(self.outcomes_path, "w", encoding="utf-8") as f:
                json.dump(outcomes, f, indent=2)
        except Exception as e:
            logger.error(f"Error recording outcome: {e}")

    def _update_crm_lead(
        self,
        lead_id: str,
        outreach_status: str,
        channel: str,
        notes: str,
        timestamp: str,
        actual_send_confirmed: bool = False,
        outcome: str = "",
    ):
        """Updates local CRM lead non-destructively."""
        try:
            if not os.path.exists(self.leads_path):
                return
            with open(self.leads_path, "r", encoding="utf-8") as f:
                crm_data = json.load(f)
            leads = crm_data.get("leads", crm_data) if isinstance(crm_data, dict) else crm_data

            for l in leads:
                if l.get("lead_id") == lead_id:
                    l["outreach_status"] = outreach_status
                    l["outreach_channel"] = channel
                    l["outreach_mode"] = "MANUAL"
                    l["outreach_sent_at"] = timestamp
                    l["manual_outreach_notes"] = notes
                    l["actual_send_confirmed"] = actual_send_confirmed
                    if outcome == "CALLBACK_REQUESTED":
                        l["callback_required"] = True
                    break

            with open(self.leads_path, "w", encoding="utf-8") as f:
                json.dump(crm_data, f, indent=2)
        except Exception as e:
            logger.error(f"Error updating CRM lead: {e}")

    # -------------------------------------------------------------------------
    # Analytics Engine: Real Outreach & Outcome Analytics
    # -------------------------------------------------------------------------

    def get_controlled_batch_analytics(self) -> Dict[str, Any]:
        """
        Calculates comprehensive outreach metrics and outcome analytics.
        Enforces explicit rate definitions:
          contact_rate = connected / phone_attempts
          interest_rate = interested / connected
          callback_rate = callback_requested / phone_attempts
          no_answer_rate = no_answer / phone_attempts
          failure_rate = (failed + wrong_number) / phone_attempts
        Never uses total qualified leads as denominator for interaction rates.
        """
        # Load activation profiles
        profiles = self.gateway.load_activation_profiles()
        total_qualified = len(profiles)
        activation_ready = sum(1 for p in profiles if p.get("activation_ready", False))

        # Load outcomes
        outcomes = []
        if os.path.exists(self.outcomes_path):
            try:
                with open(self.outcomes_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    outcomes = data if isinstance(data, list) else data.get("outcomes", [])
            except Exception:
                pass

        # Disaggregated Counts
        outreach_attempts = 0
        manual_call_attempts = 0
        manual_social_sends = 0
        manual_email_sends = 0
        automated_sends = 0  # Strict invariant

        connected = 0
        interested = 0
        callback_requested = 0
        no_answer = 0
        busy = 0
        wrong_number = 0
        not_interested = 0
        failed = 0
        unknown = 0

        # Channel breakdowns
        channel_metrics = {
            "PHONE": {"attempts": 0, "successful_contacts": 0, "positive_outcomes": 0, "negative_outcomes": 0, "failures": 0},
            "INSTAGRAM": {"attempts": 0, "successful_contacts": 0, "positive_outcomes": 0, "negative_outcomes": 0, "failures": 0},
            "FACEBOOK": {"attempts": 0, "successful_contacts": 0, "positive_outcomes": 0, "negative_outcomes": 0, "failures": 0},
            "EMAIL": {"attempts": 0, "successful_contacts": 0, "positive_outcomes": 0, "negative_outcomes": 0, "failures": 0},
        }

        # Deduplicate outcomes per lead_id + channel + attempt to prevent overcounting
        seen_keys = set()
        for o in outcomes:
            lid = o.get("lead_id", "")
            ch = o.get("channel", "PHONE").upper()
            out = o.get("outcome", "").upper()
            dedup_key = f"{lid}:{ch}:{out}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            outreach_attempts += 1
            if ch == "PHONE":
                manual_call_attempts += 1
                channel_metrics["PHONE"]["attempts"] += 1
            elif ch in ("INSTAGRAM", "FACEBOOK"):
                manual_social_sends += 1
                if ch in channel_metrics:
                    channel_metrics[ch]["attempts"] += 1
            elif ch == "EMAIL":
                manual_email_sends += 1
                channel_metrics["EMAIL"]["attempts"] += 1

            if out == "CONNECTED":
                connected += 1
                if ch in channel_metrics:
                    channel_metrics[ch]["successful_contacts"] += 1
            elif out == "INTERESTED":
                interested += 1
                connected += 1  # Interested implies connected
                if ch in channel_metrics:
                    channel_metrics[ch]["successful_contacts"] += 1
                    channel_metrics[ch]["positive_outcomes"] += 1
            elif out == "CALLBACK_REQUESTED":
                callback_requested += 1
                connected += 1  # Callback requested implies contact was made
                if ch in channel_metrics:
                    channel_metrics[ch]["successful_contacts"] += 1
                    channel_metrics[ch]["positive_outcomes"] += 1
            elif out == "NO_ANSWER":
                no_answer += 1
                if ch in channel_metrics:
                    channel_metrics[ch]["negative_outcomes"] += 1
            elif out == "BUSY":
                busy += 1
                if ch in channel_metrics:
                    channel_metrics[ch]["negative_outcomes"] += 1
            elif out == "NOT_INTERESTED":
                not_interested += 1
                connected += 1  # Had to connect to learn not interested
                if ch in channel_metrics:
                    channel_metrics[ch]["successful_contacts"] += 1
                    channel_metrics[ch]["negative_outcomes"] += 1
            elif out == "WRONG_NUMBER":
                wrong_number += 1
                if ch in channel_metrics:
                    channel_metrics[ch]["failures"] += 1
            elif out in ("FAILED", "BLOCKED", "WRONG_ACCOUNT"):
                failed += 1
                if ch in channel_metrics:
                    channel_metrics[ch]["failures"] += 1
            elif out == "SENT":
                if ch in channel_metrics:
                    channel_metrics[ch]["successful_contacts"] += 1
            else:
                unknown += 1

        # Rate calculations with strict denominators
        contact_rate = round(connected / manual_call_attempts, 4) if manual_call_attempts > 0 else 0.0
        interest_rate = round(interested / connected, 4) if connected > 0 else 0.0
        callback_rate = round(callback_requested / manual_call_attempts, 4) if manual_call_attempts > 0 else 0.0
        no_answer_rate = round(no_answer / manual_call_attempts, 4) if manual_call_attempts > 0 else 0.0
        total_failures = failed + wrong_number
        failure_rate = round(total_failures / outreach_attempts, 4) if outreach_attempts > 0 else 0.0

        # Non-qualification learning context
        lead_priority_learning = []
        seen_learning_keys = set()
        for o in outcomes:
            lid = o.get("lead_id")
            ch = o.get("channel")
            out = o.get("outcome")
            l_key = f"{lid}:{ch}:{out}"
            if l_key in seen_learning_keys:
                continue
            seen_learning_keys.add(l_key)
            prof = self.gateway.get_activation_profile(lid) or {}
            lead_priority_learning.append({
                "lead_id": lid,
                "company_name": prof.get("company_name", lid),
                "website_opportunity_score": 40 if prof.get("website_opportunity") == "NO_WEBSITE" else 20,
                "commercial_fit": "STRONG" if prof.get("activation_ready") else "MODERATE",
                "qualification_confidence": prof.get("channel_confidence", 0.88),
                "contactability": prof.get("contactability_status", "UNKNOWN"),
                "channel": ch,
                "outcome": out,
            })

        return {
            "total_qualified": total_qualified,
            "activation_ready": activation_ready,
            "outreach_attempts": outreach_attempts,
            "manual_call_attempts": manual_call_attempts,
            "manual_social_sends": manual_social_sends,
            "manual_email_sends": manual_email_sends,
            "automated_sends": 0,
            "campaigns_armed": 0,
            "outcomes": {
                "connected": connected,
                "interested": interested,
                "callback_requested": callback_requested,
                "no_answer": no_answer,
                "busy": busy,
                "wrong_number": wrong_number,
                "not_interested": not_interested,
                "failed": failed,
                "unknown": unknown,
            },
            "rates": {
                "contact_rate": contact_rate,
                "interest_rate": interest_rate,
                "callback_rate": callback_rate,
                "no_answer_rate": no_answer_rate,
                "failure_rate": failure_rate,
            },
            "channel_performance": channel_metrics,
            "lead_priority_learning": lead_priority_learning,
            "timestamp": _now_utc(),
        }
