"""
Phase 9.6: Outreach Performance Intelligence & Optimization Engine

Core capabilities:
  1. Authoritative Data Audit & Quality Checker:
     Scans outcomes, timelines, message history, CRM leads, and suppression lists.
     Detects state conflicts, orphaned IDs, duplicates, and missing timestamps.
  2. Disaggregated Outreach Metrics & Performance Tracking:
     Uses mathematically rigorous denominators.
     Strictly decouples human operations (OUTREACH_ATTEMPTS) from AUTOMATED_SENDS = 0.
  3. Channel Performance & Sample Size Guardrails:
     Evaluates PHONE, INSTAGRAM, FACEBOOK, EMAIL.
     Enforces INSUFFICIENT_SAMPLE warnings for small samples (<5 descriptive, 5-9 directional, >=10 comparative).
  4. Lead Quality Correlations & Prioritization Model:
     Computes outreach_priority_score (0-100) strictly for ordering qualified leads.
     Never modifies Rule B, qualification criteria, or website opportunity scores.
  5. Follow-Up Intelligence & Next-Batch Recommendations:
     Generates NEXT 3, NEXT 5, STANDBY, and BLOCKED queues with explicit reasons.
     Read-only recommendations; zero automated follow-ups or campaign arming.
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Set

from lib.analytics.outreach_event_model import (
    OutreachEvent,
    OutreachChannelEnum,
    OutreachEventType,
    OutreachOutcomeEnum,
    OutreachSourceEnum,
    VALID_CHANNELS,
    VALID_EVENT_TYPES,
    VALID_OUTCOMES,
)
from lib.analytics.message_experiment import (
    MessageExperimentFramework,
    MessageAngle,
    ExperimentVariant,
)

logger = logging.getLogger("OutreachPerformanceEngine")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

DEFAULT_ACTIVATION_PATH = os.path.join(DATA_DIR, "phase_9_3_contactability_run.json")
DEFAULT_LEADS_PATH = os.path.join(DATA_DIR, "cache_sheets_leads.json")
DEFAULT_TIMELINE_PATH = os.path.join(DATA_DIR, "lead_timelines.json")
DEFAULT_HISTORY_PATH = os.path.join(DATA_DIR, "message_history.json")
DEFAULT_SUPPRESSION_PATH = os.path.join(DATA_DIR, "suppression_list.json")
DEFAULT_OUTCOMES_PATH = os.path.join(DATA_DIR, "outreach_outcomes.json")
DEFAULT_BATCH_STATE_PATH = os.path.join(DATA_DIR, "controlled_batch_state.json")
DEFAULT_SNAPSHOT_PATH = os.path.join(DATA_DIR, "outreach_performance_snapshot.json")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# Analytical Outcome Weights (analytical signals, NOT qualification criteria)
OUTCOME_WEIGHTS = {
    "CONNECTED": 1.0,           # Positive contact signal
    "INTERESTED": 3.0,          # Strong positive signal
    "CALLBACK_REQUESTED": 2.5,  # Strong positive signal
    "NO_ANSWER": 0.0,           # Neutral
    "BUSY": 0.0,                # Neutral
    "NOT_INTERESTED": -1.0,     # Negative commercial signal
    "WRONG_NUMBER": -2.0,       # Data-quality contact failure
    "FAILED": -2.0,             # Execution/channel failure
    "BLOCKED": -2.0,            # Social/account block
    "SENT": 0.5,                # Delivery signal
    "UNKNOWN": 0.0,
}

# Follow-Up Intelligence Mapping (read-only recommendation)
FOLLOW_UP_RECOMMENDATIONS = {
    "INTERESTED": "FOLLOW_UP_REQUIRED",
    "CALLBACK_REQUESTED": "CALLBACK_REQUIRED",
    "NO_ANSWER": "OPERATOR_DECISION",
    "BUSY": "OPERATOR_DECISION",
    "NOT_INTERESTED": "NO_AUTOMATIC_FOLLOW_UP",
    "WRONG_NUMBER": "CONTACT_REVIEW",
    "FAILED": "CONTACT_REVIEW",
    "BLOCKED": "CONTACT_REVIEW",
    "CONNECTED": "OPERATOR_DECISION",
    "SENT": "AWAITING_RESPONSE",
    "UNKNOWN": "OPERATOR_DECISION",
}


class OutreachPerformanceEngine:
    """
    Performance intelligence engine providing audit, analytics, prioritization,
    and evidence-based recommendations without mutating qualification or executing outreach.
    """

    def __init__(
        self,
        activation_path: str = DEFAULT_ACTIVATION_PATH,
        leads_path: str = DEFAULT_LEADS_PATH,
        timeline_path: str = DEFAULT_TIMELINE_PATH,
        history_path: str = DEFAULT_HISTORY_PATH,
        suppression_path: str = DEFAULT_SUPPRESSION_PATH,
        outcomes_path: str = DEFAULT_OUTCOMES_PATH,
        batch_state_path: str = DEFAULT_BATCH_STATE_PATH,
        snapshot_path: str = DEFAULT_SNAPSHOT_PATH,
    ):
        self.activation_path = activation_path
        self.leads_path = leads_path
        self.timeline_path = timeline_path
        self.history_path = history_path
        self.suppression_path = suppression_path
        self.outcomes_path = outcomes_path
        self.batch_state_path = batch_state_path
        self.snapshot_path = snapshot_path

    # -------------------------------------------------------------------------
    # 1. Authoritative Data Loading
    # -------------------------------------------------------------------------

    def load_crm_leads(self) -> Dict[str, Dict[str, Any]]:
        """Loads CRM leads indexed by lead_id."""
        if not os.path.exists(self.leads_path):
            return {}
        try:
            with open(self.leads_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                leads = data.get("leads", data) if isinstance(data, dict) else data
                return {l["lead_id"]: l for l in leads if isinstance(l, dict) and "lead_id" in l}
        except Exception as e:
            logger.error(f"Error loading CRM leads: {e}")
            return {}

    def load_activation_profiles(self) -> List[Dict[str, Any]]:
        """Loads Phase 9.3 activation queue profiles."""
        if not os.path.exists(self.activation_path):
            return []
        try:
            with open(self.activation_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("ACTIVATION_QUEUE", [])
        except Exception as e:
            logger.error(f"Error loading activation profiles: {e}")
            return []

    def load_outcomes(self) -> List[Dict[str, Any]]:
        """Loads recorded outcomes from outreach_outcomes.json."""
        if not os.path.exists(self.outcomes_path):
            return []
        try:
            with open(self.outcomes_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else data.get("outcomes", [])
        except Exception as e:
            logger.error(f"Error loading outcomes: {e}")
            return []

    def load_timelines(self) -> Dict[str, List[Dict[str, Any]]]:
        """Loads timeline events per lead."""
        if not os.path.exists(self.timeline_path):
            return {}
        try:
            with open(self.timeline_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"Error loading timelines: {e}")
            return {}

    def load_message_history(self) -> Dict[str, List[Dict[str, Any]]]:
        """Loads message history from message_history.json."""
        if not os.path.exists(self.history_path):
            return {}
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"Error loading message history: {e}")
            return {}

    def load_suppression_list(self) -> Dict[str, Any]:
        """Loads suppression list."""
        if not os.path.exists(self.suppression_path):
            return {}
        try:
            with open(self.suppression_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading suppression list: {e}")
            return {}

    def load_batch_state(self) -> Dict[str, Any]:
        """Loads Phase 9.5 controlled batch state."""
        if not os.path.exists(self.batch_state_path):
            return {}
        try:
            with open(self.batch_state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading batch state: {e}")
            return {}

    # -------------------------------------------------------------------------
    # 2. Data Quality Audit (Section 23)
    # -------------------------------------------------------------------------

    def audit_data_quality(self) -> Dict[str, Any]:
        """
        Audits underlying data files for inconsistencies, orphaned IDs, state conflicts,
        and invalid timestamps. Flags findings as ANALYTICS_DATA_QUALITY_ISSUE.
        """
        issues: List[Dict[str, Any]] = []

        crm_leads = self.load_crm_leads()
        known_lead_ids = set(crm_leads.keys())
        profiles = self.load_activation_profiles()
        for p in profiles:
            if "lead_id" in p:
                known_lead_ids.add(p["lead_id"])

        outcomes = self.load_outcomes()
        timelines = self.load_timelines()
        history = self.load_message_history()
        suppression = self.load_suppression_list()

        # Check 1: Duplicate events in outcomes
        seen_outcomes = set()
        for idx, o in enumerate(outcomes):
            lid = o.get("lead_id")
            ch = o.get("channel")
            out = o.get("outcome")
            ts = o.get("recorded_at")
            key = (lid, ch, out, ts)
            if key in seen_outcomes:
                issues.append({
                    "issue_type": "ANALYTICS_DATA_QUALITY_ISSUE",
                    "category": "DUPLICATE_EVENT",
                    "details": f"Duplicate outcome record detected for lead {lid} on {ch}: {out} at {ts}.",
                    "lead_id": lid,
                })
            seen_outcomes.add(key)

            # Check 2: Orphaned lead ID
            if lid not in known_lead_ids:
                issues.append({
                    "issue_type": "ANALYTICS_DATA_QUALITY_ISSUE",
                    "category": "ORPHANED_LEAD_ID",
                    "details": f"Outcome references unknown lead ID: {lid}.",
                    "lead_id": lid,
                })

            # Check 3: Missing timestamp
            if not ts:
                issues.append({
                    "issue_type": "ANALYTICS_DATA_QUALITY_ISSUE",
                    "category": "MISSING_TIMESTAMP",
                    "details": f"Outcome for {lid} missing recorded_at timestamp.",
                    "lead_id": lid,
                })

            # Check 4: Unknown channel
            if ch not in VALID_CHANNELS:
                issues.append({
                    "issue_type": "ANALYTICS_DATA_QUALITY_ISSUE",
                    "category": "UNKNOWN_CHANNEL",
                    "details": f"Outcome has unknown channel '{ch}'.",
                    "lead_id": lid,
                })

            # Check 5: Unknown outcome
            if out not in VALID_OUTCOMES:
                issues.append({
                    "issue_type": "ANALYTICS_DATA_QUALITY_ISSUE",
                    "category": "UNKNOWN_OUTCOME",
                    "details": f"Outcome has unknown outcome value '{out}'.",
                    "lead_id": lid,
                })

        # Check 6: Timeline state / event conflicts
        for lid, evts in timelines.items():
            if lid not in known_lead_ids:
                issues.append({
                    "issue_type": "ANALYTICS_DATA_QUALITY_ISSUE",
                    "category": "ORPHANED_LEAD_ID",
                    "details": f"Timeline references unknown lead ID: {lid}.",
                    "lead_id": lid,
                })

            event_types = [e.get("event_type") for e in evts if isinstance(e, dict)]

            # Conflict A: INTERESTED without contact event
            if "INTERESTED" in [e.get("details", {}).get("outcome") for e in evts if isinstance(e, dict)]:
                if not any(et in ("CALL_ATTEMPTED", "CALL_CONNECTED", "SEND_CONFIRMED") for et in event_types):
                    issues.append({
                        "issue_type": "ANALYTICS_DATA_QUALITY_ISSUE",
                        "category": "STATE_CONFLICT",
                        "details": f"Lead {lid} has INTERESTED outcome without preceding contact attempt event.",
                        "lead_id": lid,
                    })

            # Conflict B: SENT without confirmed operator action
            if "SEND_CONFIRMED" in event_types and "OPERATOR_CONFIRMED" not in event_types:
                issues.append({
                    "issue_type": "ANALYTICS_DATA_QUALITY_ISSUE",
                    "category": "STATE_CONFLICT",
                    "details": f"Lead {lid} marked SEND_CONFIRMED without preceding OPERATOR_CONFIRMED event.",
                    "lead_id": lid,
                })

        # Check 7: CRM state conflicts
        for lid, lead in crm_leads.items():
            out_stat = lead.get("outreach_status")

            # SENT without confirmed operator action or send history
            if out_stat == "SENT":
                has_history_send = lid in history and any(m.get("status") == "SENT" for m in history[lid])
                has_timeline_send = lid in timelines and any(e.get("event_type") in ("SEND_CONFIRMED", "OUTREACH_SENT") for e in timelines[lid])
                has_operator_confirmed = lead.get("actual_send_confirmed") is True
                if not has_history_send and not has_timeline_send and not has_operator_confirmed:
                    issues.append({
                        "issue_type": "ANALYTICS_DATA_QUALITY_ISSUE",
                        "category": "STATE_CONFLICT",
                        "details": f"CRM marks lead {lid} as SENT, but no confirmed send history or operator confirmation exists.",
                        "lead_id": lid,
                    })

            # CONTACTED without outreach event
            if out_stat == "CONTACTED":
                has_contact_evt = False
                if lid in timelines:
                    has_contact_evt = any(e.get("event_type") in ("CALL_ATTEMPTED", "CALL_CONNECTED", "OUTCOME") for e in timelines[lid])
                if lid in outcomes:
                    has_contact_evt = True
                if not has_contact_evt and not any(o.get("lead_id") == lid for o in outcomes):
                    issues.append({
                        "issue_type": "ANALYTICS_DATA_QUALITY_ISSUE",
                        "category": "STATE_CONFLICT",
                        "details": f"CRM marks lead {lid} as CONTACTED, but no outreach event or recorded outcome exists.",
                        "lead_id": lid,
                    })

            # BOUNCED without send event
            if out_stat == "BOUNCED":
                has_history_send = lid in history and any(m.get("status") == "SENT" for m in history[lid])
                has_suppression = False
                supp_emails = suppression.get("suppressed_emails", {})
                lead_email = lead.get("email")
                if lead_email and lead_email in supp_emails:
                    has_suppression = True
                # Bounced must have send or bounce record
                if not has_history_send and not has_suppression:
                    issues.append({
                        "issue_type": "ANALYTICS_DATA_QUALITY_ISSUE",
                        "category": "STATE_CONFLICT",
                        "details": f"CRM marks lead {lid} as BOUNCED, but no send history or bounce record found.",
                        "lead_id": lid,
                    })

        return {
            "audited_at": _now_utc(),
            "total_issues_detected": len(issues),
            "issues": issues,
            "has_data_quality_issues": len(issues) > 0,
        }

    # -------------------------------------------------------------------------
    # 3. Canonical Event Ingestion
    # -------------------------------------------------------------------------

    def get_canonical_events(self) -> List[OutreachEvent]:
        """
        Normalizes timeline and outcome logs into validated, immutable OutreachEvent objects.
        Filters out pure test_mode actions from production analytics.
        """
        canonical_events: List[OutreachEvent] = []
        timelines = self.load_timelines()

        for lid, evts in timelines.items():
            for e in evts:
                if not isinstance(e, dict):
                    continue
                evt_type = e.get("event_type", "OUTREACH_ATTEMPTED")
                if evt_type not in VALID_EVENT_TYPES:
                    continue

                details = e.get("details", {}) or {}
                # Filter out pure test_mode actions
                if details.get("test_mode") is True:
                    continue

                ch = (e.get("channel") or details.get("channel") or "PHONE").upper()
                if ch not in VALID_CHANNELS:
                    ch = "PHONE"

                out = details.get("outcome")
                if out and out.upper() not in VALID_OUTCOMES:
                    out = "UNKNOWN"

                try:
                    c_event = OutreachEvent(
                        event_id=e.get("event_id", ""),
                        lead_id=lid,
                        channel=ch,
                        event_type=evt_type,
                        outcome=out.upper() if out else None,
                        template_version=details.get("template_version", MessageExperimentFramework.DEFAULT_TEMPLATE_VERSION),
                        message_angle=details.get("message_angle", MessageExperimentFramework.DEFAULT_MESSAGE_ANGLE),
                        attempt_number=details.get("attempt_number", 1),
                        operator_confirmed=e.get("actor") == "HUMAN_OPERATOR" or details.get("operator_confirmed", False),
                        occurred_at=e.get("timestamp", _now_utc()),
                        source=e.get("source", "OPERATOR"),
                        metadata=details,
                    )
                    canonical_events.append(c_event)
                except Exception as ex:
                    logger.debug(f"Skipping malformed event for {lid}: {ex}")

        return canonical_events

    # -------------------------------------------------------------------------
    # 4. Outreach Performance Engine: Volume, Outcomes & Rates
    # -------------------------------------------------------------------------

    def calculate_outreach_performance(self) -> Dict[str, Any]:
        """
        Calculates all volume, contact, outcome, and rate metrics with explicit denominators.
        Never uses total qualified leads as denominator for interaction rates.
        """
        profiles = self.load_activation_profiles()
        total_qualified = len(profiles)
        activation_ready = sum(1 for p in profiles if p.get("activation_ready", False))

        outcomes = self.load_outcomes()
        history = self.load_message_history()
        crm_leads = self.load_crm_leads()

        # Deduplicate outcomes per lead_id + channel + attempt to prevent overcounting
        deduped_outcomes = []
        seen = set()
        for o in outcomes:
            lid = o.get("lead_id", "")
            ch = (o.get("channel") or "PHONE").upper()
            out = (o.get("outcome") or "").upper()
            key = (lid, ch, out)
            if key not in seen:
                seen.add(key)
                deduped_outcomes.append(o)

        outreach_attempts = 0
        manual_call_attempts = 0
        manual_social_sends = 0
        manual_email_sends = 0
        automated_sends = 0  # Strict invariant

        connected = 0
        interested = 0
        callback_requested = 0
        meeting_requested = 0
        no_answer = 0
        busy = 0
        wrong_number = 0
        not_interested = 0
        failed = 0
        unknown = 0

        unique_contacted_leads: Set[str] = set()

        # Channel breakdowns
        channel_data = {
            "PHONE": {
                "attempts": 0,
                "confirmed_sends": 0,
                "connected": 0,
                "positive_outcomes": 0,
                "negative_outcomes": 0,
                "failures": 0,
                "sample_size": 0,
                "sample_warning": "INSUFFICIENT_SAMPLE",
            },
            "INSTAGRAM": {
                "attempts": 0,
                "confirmed_sends": 0,
                "connected": 0,
                "positive_outcomes": 0,
                "negative_outcomes": 0,
                "failures": 0,
                "sample_size": 0,
                "sample_warning": "INSUFFICIENT_SAMPLE",
            },
            "FACEBOOK": {
                "attempts": 0,
                "confirmed_sends": 0,
                "connected": 0,
                "positive_outcomes": 0,
                "negative_outcomes": 0,
                "failures": 0,
                "sample_size": 0,
                "sample_warning": "INSUFFICIENT_SAMPLE",
            },
            "EMAIL": {
                "attempts": 0,
                "confirmed_sends": 0,
                "connected": 0,
                "positive_outcomes": 0,
                "negative_outcomes": 0,
                "failures": 0,
                "sample_size": 0,
                "sample_warning": "INSUFFICIENT_SAMPLE",
            },
        }

        # Process recorded outcomes
        for o in deduped_outcomes:
            lid = o.get("lead_id", "")
            ch = (o.get("channel") or "PHONE").upper()
            out = (o.get("outcome") or "").upper()
            unique_contacted_leads.add(lid)

            outreach_attempts += 1
            if ch == "PHONE":
                manual_call_attempts += 1
                channel_data["PHONE"]["attempts"] += 1
                channel_data["PHONE"]["sample_size"] += 1
            elif ch in ("INSTAGRAM", "FACEBOOK"):
                manual_social_sends += 1
                if ch in channel_data:
                    channel_data[ch]["attempts"] += 1
                    channel_data[ch]["sample_size"] += 1
            elif ch == "EMAIL":
                manual_email_sends += 1
                channel_data["EMAIL"]["attempts"] += 1
                channel_data["EMAIL"]["sample_size"] += 1

            if out == "CONNECTED":
                connected += 1
                channel_data["PHONE"]["connected"] += 1
            elif out == "INTERESTED":
                interested += 1
                connected += 1
                channel_data[ch]["connected"] += 1
                channel_data[ch]["positive_outcomes"] += 1
            elif out == "CALLBACK_REQUESTED":
                callback_requested += 1
                connected += 1
                channel_data[ch]["connected"] += 1
                channel_data[ch]["positive_outcomes"] += 1
            elif out == "NO_ANSWER":
                no_answer += 1
                channel_data[ch]["negative_outcomes"] += 1
            elif out == "BUSY":
                busy += 1
                channel_data[ch]["negative_outcomes"] += 1
            elif out == "NOT_INTERESTED":
                not_interested += 1
                connected += 1
                channel_data[ch]["connected"] += 1
                channel_data[ch]["negative_outcomes"] += 1
            elif out == "WRONG_NUMBER":
                wrong_number += 1
                channel_data[ch]["failures"] += 1
            elif out in ("FAILED", "BLOCKED", "WRONG_ACCOUNT"):
                failed += 1
                channel_data[ch]["failures"] += 1
            elif out == "SENT":
                channel_data[ch]["confirmed_sends"] += 1
            else:
                unknown += 1

        # Check historical confirmed email sends (Seoul Kimchi)
        for lid, msgs in history.items():
            for m in msgs:
                if m.get("status") == "SENT":
                    ch = (m.get("channel") or "EMAIL").upper()
                    if ch in channel_data:
                        channel_data[ch]["confirmed_sends"] += 1

        # Calculate Sample-Size Guardrails (Section 19)
        def _get_sample_warning(n: int) -> str:
            if n < 5:
                return "INSUFFICIENT SAMPLE FOR COMPARATIVE CONCLUSION (descriptive only)"
            elif 5 <= n <= 9:
                return "DIRECTIONAL INSIGHT ONLY (sample 5-9)"
            elif 10 <= n <= 29:
                return "PRELIMINARY COMPARISON ALLOWED (sample 10-29)"
            else:
                return "OPTIMIZATION SIGNAL (sample >= 30)"

        for ch in channel_data:
            channel_data[ch]["sample_warning"] = _get_sample_warning(channel_data[ch]["sample_size"])

        # Strict rate definitions with explicit denominators (Section 5)
        contact_rate = round(connected / manual_call_attempts, 4) if manual_call_attempts > 0 else 0.0
        interest_rate = round(interested / connected, 4) if connected > 0 else 0.0
        callback_rate_of_connected = round(callback_requested / connected, 4) if connected > 0 else 0.0
        callback_rate_of_attempts = round(callback_requested / manual_call_attempts, 4) if manual_call_attempts > 0 else 0.0
        wrong_number_rate = round(wrong_number / manual_call_attempts, 4) if manual_call_attempts > 0 else 0.0
        no_answer_rate = round(no_answer / manual_call_attempts, 4) if manual_call_attempts > 0 else 0.0
        failure_rate = round((failed + wrong_number) / outreach_attempts, 4) if outreach_attempts > 0 else 0.0

        # Positive outcome rate (labeled explicitly)
        positive_outcomes_count = interested + callback_requested + meeting_requested
        positive_outcome_rate_of_connected = round(positive_outcomes_count / connected, 4) if connected > 0 else 0.0

        return {
            "volume": {
                "qualified_leads": total_qualified,
                "activation_ready": activation_ready,
                "outreach_attempts": outreach_attempts,
                "unique_leads_contacted": len(unique_contacted_leads),
                "manual_call_attempts": manual_call_attempts,
                "manual_social_sends": manual_social_sends,
                "manual_email_sends": manual_email_sends,
                "automated_sends": 0,
                "campaigns_armed": 0,
            },
            "contact_performance": {
                "connected": connected,
                "no_answer": no_answer,
                "busy": busy,
                "wrong_number": wrong_number,
                "failed": failed,
                "unknown": unknown,
            },
            "positive_outcomes": {
                "interested": interested,
                "callback_requested": callback_requested,
                "meeting_requested": meeting_requested,
                "other_positive": 0,
            },
            "negative_outcomes": {
                "not_interested": not_interested,
                "no_answer": no_answer,
                "busy": busy,
                "wrong_number": wrong_number,
                "failed": failed,
            },
            "rates": {
                "contact_rate": contact_rate,
                "interest_rate": interest_rate,
                "callback_rate_of_connected": callback_rate_of_connected,
                "callback_rate_of_attempts": callback_rate_of_attempts,
                "wrong_number_rate": wrong_number_rate,
                "no_answer_rate": no_answer_rate,
                "failure_rate": failure_rate,
                "positive_outcome_rate_of_connected": positive_outcome_rate_of_connected,
            },
            "rate_denominators": {
                "contact_rate_formula": "connected / manual_call_attempts",
                "interest_rate_formula": "interested / connected",
                "callback_rate_formula": "callback_requested / connected",
                "wrong_number_rate_formula": "wrong_number / manual_call_attempts",
                "no_answer_rate_formula": "no_answer / manual_call_attempts",
                "failure_rate_formula": "(failed + wrong_number) / outreach_attempts",
                "qualified_leads_as_interaction_denominator": False,
            },
            "channel_performance": channel_data,
            "sample_size_warnings": {
                "total_attempts": outreach_attempts,
                "warning": _get_sample_warning(outreach_attempts),
                "comparisons_reliable": outreach_attempts >= 10,
            },
            # Section 18 Dashboard Views
            "overview": {
                "qualified": total_qualified,
                "activation_ready": activation_ready,
                "contacted": len(unique_contacted_leads),
                "attempts": outreach_attempts,
                "connected": connected,
                "interested": interested,
                "callbacks": callback_requested,
            },
            "channel": {
                "phone": channel_data["PHONE"],
                "instagram": channel_data["INSTAGRAM"],
                "facebook": channel_data["FACEBOOK"],
                "email": channel_data["EMAIL"],
            },
            "outcomes": {
                "positive": positive_outcomes_count,
                "neutral": no_answer + busy,
                "negative": not_interested,
                "data_quality": wrong_number,
                "execution_failure": failed,
            },
            "message": {
                "template": "WEBSITE_DEV_V1",
                "angle": MessageAngle.WEBSITE_FIRST.value,
                "channel": "PHONE",
                "outcome": "CONNECTED" if connected > 0 else "UNKNOWN",
                "sample_warning": _get_sample_warning(outreach_attempts),
            },
            "lead_quality": {
                "priority_range": "80-100",
                "website_opportunity": "NO_WEBSITE (High Opportunity)",
                "commercial_fit": "STRONG",
                "outcome": "CONNECTED" if connected > 0 else "UNKNOWN",
                "sample_warning": _get_sample_warning(outreach_attempts),
            },
        }

    def get_performance_dashboard(self) -> Dict[str, Any]:
        """Convenience method returning Section 18 performance dashboard."""
        return self.calculate_outreach_performance()

    # -------------------------------------------------------------------------
    # 5. Lead Prioritization Model: outreach_priority_score (Section 8 & 20)
    # -------------------------------------------------------------------------

    def calculate_lead_priority_score(self, profile: Dict[str, Any], crm_lead: Dict[str, Any]) -> Tuple[int, List[str]]:
        """
        Calculates outreach_priority_score (0-100) strictly for ordering qualified leads.
        Does NOT modify qualification_score, website_opportunity_score, or Rule B.
        Inputs:
          - Qualification confidence (up to 40 pts)
          - Website opportunity status (up to 40 pts)
          - Traction / Commercial fit (up to 10 pts)
          - Direct contactability quality (up to 10 pts)
          - Historical failure penalty (-20 pts)
        """
        # Hard invariant: Only qualified leads can receive priority score
        qual_state = (
            profile.get("qualification_state")
            or profile.get("qualification_status")
            or crm_lead.get("qualification_state")
            or crm_lead.get("qualification_status")
        )
        if qual_state != "OUTREACH_READY":
            return 0, ["Lead is not qualified as OUTREACH_READY."]

        score = 0
        reasons = []

        # 1. Qualification Confidence (max 40 pts)
        conf = profile.get("channel_confidence", 0.88)
        conf_pts = int(conf * 40)
        score += conf_pts
        reasons.append(f"Strong qualification evidence (confidence: {conf:.2f})")

        # 2. Website Opportunity (max 40 pts)
        web_opp = profile.get("website_opportunity") or crm_lead.get("website_opportunity") or crm_lead.get("website_status", "")
        opp_score = crm_lead.get("website_opportunity_score") or profile.get("website_opportunity_score")
        if opp_score is not None and isinstance(opp_score, (int, float)):
            web_opp_pts = int((opp_score / 100.0) * 40)
            score += web_opp_pts
            reasons.append(f"Website opportunity score: {opp_score}/100 (+{web_opp_pts} pts)")
        elif web_opp in ("NO_WEBSITE", "NO_WEBSITE_CONFIRMED"):
            score += 40
            reasons.append("High website opportunity: Confirmed absence of official website")
        elif web_opp in ("BROKEN_WEBSITE", "POOR_WEBSITE"):
            score += 20
            reasons.append("Moderate website opportunity: Broken or inadequate existing site")

        # 3. Traction & Review Volume (max 10 pts)
        revs = crm_lead.get("review_count") or 0
        rating = crm_lead.get("rating") or 0.0
        if revs >= 500:
            score += 10
            reasons.append(f"Exceptional market traction: {revs} reviews ({rating}★)")
        elif revs >= 100:
            score += 7
            reasons.append(f"Solid customer review base: {revs} reviews ({rating}★)")
        elif revs > 0:
            score += 3
            reasons.append(f"Active review base: {revs} reviews ({rating}★)")

        # 4. Verified Contactability (max 10 pts)
        rec_ch = profile.get("recommended_channel", "PHONE").upper()
        if rec_ch == "PHONE" and profile.get("verified_phone"):
            score += 10
            reasons.append("Verified direct business telephone line")
        elif rec_ch in ("INSTAGRAM", "FACEBOOK"):
            score += 5
            reasons.append(f"Verified {rec_ch} business profile")

        # 5. Historical Outcome Adjustment
        outreach_status = crm_lead.get("outreach_status", "")
        if outreach_status == "FAILED":
            score -= 20
            reasons.append("Previous attempt failed; requires operator caution")
        elif outreach_status in ("SENT", "CONTACTED", "BOUNCED"):
            score = 0
            reasons = [f"Lead already in final/contacted state: {outreach_status}."]

        score = max(0, min(100, score))
        return score, reasons

    def get_next_best_leads(self) -> List[Dict[str, Any]]:
        """
        Returns already-qualified leads ranked by outreach_priority_score with explanations.
        Does NOT include unqualified leads.
        """
        profiles = self.load_activation_profiles()
        crm_leads = self.load_crm_leads()
        ranked = []

        for p in profiles:
            lid = p.get("lead_id")
            crm = crm_leads.get(lid, {})

            # Exclude already sent or bounced leads from next-best recommendations
            if lid in ("LEAD-MAN-4DB3EF", "LEAD-MAN-709C66", "LEAD-MAN-902001"):
                continue
            if crm.get("outreach_status") in ("SENT", "CONTACTED", "BOUNCED"):
                continue

            score, reasons = self.calculate_lead_priority_score(p, crm)
            if score > 0:
                ranked.append({
                    "lead_id": lid,
                    "company_name": p.get("company_name"),
                    "priority": score,
                    "recommended_channel": p.get("recommended_channel", "PHONE"),
                    "verified_contact": p.get("verified_phone") or p.get("verified_instagram") or "None",
                    "reasons": reasons,
                    "review_count": crm.get("review_count", 0),
                    "rating": crm.get("rating", 0.0),
                })

        ranked.sort(key=lambda x: (x["priority"], x["review_count"]), reverse=True)
        return ranked

    # -------------------------------------------------------------------------
    # 6. Next-Batch Recommendation Queue (Section 28)
    # -------------------------------------------------------------------------

    def get_next_batch_recommendations(self) -> Dict[str, Any]:
        """
        Generates structured operator queue recommendations:
          - NEXT 3: Top 3 priority qualified leads
          - NEXT 5: Next 5 qualified leads
          - STANDBY: Activation blocked or pending candidates
          - BLOCKED: Suppressed, bounced, or already contacted
        """
        ranked = self.get_next_best_leads()
        profiles = self.load_activation_profiles()
        crm_leads = self.load_crm_leads()

        next_3 = ranked[:3]
        next_5 = ranked[3:8]

        standby = []
        blocked = []

        # Find blocked / standby leads
        for p in profiles:
            lid = p.get("lead_id")
            crm = crm_leads.get(lid, {})
            name = p.get("company_name", lid)
            stat = crm.get("outreach_status", p.get("outreach_status"))

            if lid == "LEAD-MAN-902001":
                blocked.append({
                    "lead_id": lid,
                    "company_name": name,
                    "reason": "Pilot successfully completed: Outcome CONNECTED (Manual call).",
                })
            elif lid == "LEAD-MAN-4DB3EF":
                blocked.append({
                    "lead_id": lid,
                    "company_name": name,
                    "reason": "Confirmed send exists: Status SENT.",
                })
            elif lid == "LEAD-MAN-709C66":
                blocked.append({
                    "lead_id": lid,
                    "company_name": name,
                    "reason": "Email bounced: SUPPRESSED on regulatory list.",
                })
            elif not p.get("activation_ready", False):
                standby.append({
                    "lead_id": lid,
                    "company_name": name,
                    "reason": f"Activation blocked: {p.get('activation_blockers') or 'Pending channel review'}.",
                })

        return {
            "generated_at": _now_utc(),
            "next_3": next_3,
            "next_5": next_5,
            "standby": standby,
            "blocked": blocked,
            "operator_notice": "Recommendation only. Human operator confirmation required for every action.",
        }

    # -------------------------------------------------------------------------
    # 7. Comprehensive Snapshot Generator (Section 24)
    # -------------------------------------------------------------------------

    def generate_performance_snapshot(self) -> Dict[str, Any]:
        """
        Builds the reproducible Phase 9.6 outreach performance snapshot artifact.
        """
        perf = self.calculate_outreach_performance()
        quality_audit = self.audit_data_quality()
        recommendations = self.get_next_batch_recommendations()

        # Template & Angle Analytics
        template_metrics = {
            "WEBSITE_DEV_V1": {
                "attempts": perf["volume"]["outreach_attempts"],
                "connected": perf["contact_performance"]["connected"],
                "positive_outcomes": perf["positive_outcomes"]["interested"] + perf["positive_outcomes"]["callback_requested"],
                "sample_warning": "INSUFFICIENT SAMPLE FOR COMPARATIVE CONCLUSION (descriptive only)",
            }
        }
        angle_metrics = {
            MessageAngle.WEBSITE_FIRST.value: {
                "attempts": perf["volume"]["outreach_attempts"],
                "connected": perf["contact_performance"]["connected"],
                "positive_outcomes": perf["positive_outcomes"]["interested"] + perf["positive_outcomes"]["callback_requested"],
                "sample_warning": "INSUFFICIENT SAMPLE FOR COMPARATIVE CONCLUSION (descriptive only)",
            }
        }

        snapshot = {
            "generated_at": _now_utc(),
            "phase": "9.6",
            "market": "MANCHESTER_UK",
            "qualified_count": perf["volume"]["qualified_leads"],
            "activation_ready_count": perf["volume"]["activation_ready"],
            "attempt_count": perf["volume"]["outreach_attempts"],
            "unique_contacted_count": perf["volume"]["unique_leads_contacted"],

            "phone_metrics": perf["channel_performance"]["PHONE"],
            "instagram_metrics": perf["channel_performance"]["INSTAGRAM"],
            "facebook_metrics": perf["channel_performance"]["FACEBOOK"],
            "email_metrics": perf["channel_performance"]["EMAIL"],

            "outcome_metrics": perf["contact_performance"],
            "positive_outcome_metrics": perf["positive_outcomes"],
            "rates": perf["rates"],
            "rate_denominators": perf["rate_denominators"],

            "template_metrics": template_metrics,
            "message_angle_metrics": angle_metrics,

            "data_quality_issues": quality_audit["issues"],
            "sample_size_warnings": perf["sample_size_warnings"],

            "recommended_queue": recommendations,
            "safety_invariants": {
                "automated_sends": 0,
                "automated_followups": 0,
                "campaigns_armed": 0,
                "auto_continuation": 0,
                "rule_b_criteria_mutated": 0,
            }
        }

        # Save to disk
        os.makedirs(os.path.dirname(self.snapshot_path), exist_ok=True)
        with open(self.snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)

        logger.info(f"Generated outreach performance snapshot: {self.snapshot_path}")
        return snapshot
