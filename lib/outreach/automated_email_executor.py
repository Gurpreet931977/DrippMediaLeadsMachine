"""
lib/outreach/automated_email_executor.py
========================================
Automated Email Outreach Executor & Campaign Engine for Phase 10.2.

Orchestrates the entire automated email outreach lifecycle:
1. Audience Eligibility & Multi-gate Validation
2. Compliance Classification & Law/Policy Evidence Generation
3. Multi-tier Suppression & Unsubscribe Injection
4. Message Template Rendering & Anti-Hallucination QA
5. Central Rate Limiting & Quota Consumption
6. Deterministic Idempotency & Bounded Retry Handling
7. Authoritative Provider Submission & Result Tracking
8. Timeline Event Logging & CRM State Synchronization
9. Automatic Stop Conditions & Emergency Kill Switch
10. Data Provenance Preservation & Analytics Generation
"""

import os
import json
import uuid
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Tuple

from lib.system.atomic_writer import atomic_write_json, atomic_write_text
from lib.system.file_lock import FileLock, crm_write_lock
from lib.system.system_config import SystemConfig, EmailAutomationBlockedError, CommercialActionForbiddenError
from lib.system.quota_governor import QuotaGovernor, QuotaExhaustedError
from lib.outreach.email_compliance import (
    EmailComplianceEngine,
    SubscriberClass,
    ComplianceStatus,
    LawfulBasis,
)
from lib.outreach.email_suppression import EmailSuppressionManager
from lib.outreach.email_sendability import EmailSendabilityGate, SendabilityEvaluation
from lib.outreach.email_sender_health import EmailSenderHealthAuditor
from lib.outreach.email_provider import EnhancedEmailProvider, ProviderDeliveryStatus, EmailProviderResult
from lib.outreach.email_templates import EmailTemplateEngine, TEMPLATE_VERSION_V1
from lib.outreach.email_governor import EmailGovernor

logger = logging.getLogger("AutomatedEmailExecutor")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

DEFAULT_LEADS_PATH = os.path.join(DATA_DIR, "cache_sheets_leads.json")
DEFAULT_TIMELINES_PATH = os.path.join(DATA_DIR, "lead_timelines.json")
DEFAULT_COMMERCIAL_PATH = os.path.join(DATA_DIR, "commercial_records.json")
DEFAULT_DISPATCH_LOG_PATH = os.path.join(DATA_DIR, "email_dispatch_log.jsonl")
DEFAULT_CAMPAIGNS_PATH = os.path.join(DATA_DIR, "email_campaigns.json")
DEFAULT_ANALYTICS_PATH = os.path.join(DATA_DIR, "email_analytics.json")
DEFAULT_RUN_ARTIFACT_PATH = os.path.join(DATA_DIR, "phase_10_2_automated_email_run.json")

MAX_EMAIL_BATCH = 5  # Section 16 & 39 initial batch cap


class CampaignState:
    EMAIL_DRAFT     = "EMAIL_DRAFT"
    EMAIL_APPROVED  = "EMAIL_APPROVED"
    EMAIL_READY     = "EMAIL_READY"
    EMAIL_RUNNING   = "EMAIL_RUNNING"
    EMAIL_PAUSED    = "EMAIL_PAUSED"
    EMAIL_COMPLETED = "EMAIL_COMPLETED"
    EMAIL_CANCELLED = "EMAIL_CANCELLED"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class AutomatedEmailExecutor:
    """
    Central execution engine for automated email outreach.
    """

    def __init__(
        self,
        data_dir: Optional[str] = None,
        leads_path: Optional[str] = None,
        timelines_path: Optional[str] = None,
        commercial_path: Optional[str] = None,
        dispatch_log_path: Optional[str] = None,
        campaigns_path: Optional[str] = None,
        analytics_path: Optional[str] = None,
        run_artifact_path: Optional[str] = None,
        provider: Optional[EnhancedEmailProvider] = None,
        suppression_manager: Optional[EmailSuppressionManager] = None,
        governor: Optional[EmailGovernor] = None,
        quota_governor: Optional[QuotaGovernor] = None,
    ):
        self.data_dir = data_dir or DATA_DIR
        self.leads_path = leads_path or os.path.join(self.data_dir, "cache_sheets_leads.json")
        self.timelines_path = timelines_path or os.path.join(self.data_dir, "lead_timelines.json")
        self.commercial_path = commercial_path or os.path.join(self.data_dir, "commercial_records.json")
        self.dispatch_log_path = dispatch_log_path or os.path.join(self.data_dir, "email_dispatch_log.jsonl")
        self.campaigns_path = campaigns_path or os.path.join(self.data_dir, "email_campaigns.json")
        self.analytics_path = analytics_path or os.path.join(self.data_dir, "email_analytics.json")
        self.run_artifact_path = run_artifact_path or os.path.join(self.data_dir, "phase_10_2_automated_email_run.json")

        self.suppression_manager = suppression_manager or EmailSuppressionManager(
            suppression_path=os.path.join(self.data_dir, "email_suppression.json"),
            unsubscribes_path=os.path.join(self.data_dir, "email_unsubscribes.json"),
        )
        self.sendability_gate = EmailSendabilityGate(suppression_manager=self.suppression_manager)
        self.sender_auditor = EmailSenderHealthAuditor()
        self.provider = provider or EnhancedEmailProvider()
        self.governor = governor or EmailGovernor(
            rate_limit_path=os.path.join(self.data_dir, "email_rate_limits.json"),
            idempotency_path=os.path.join(self.data_dir, "email_idempotency_log.json"),
            suppression_manager=self.suppression_manager,
        )
        self.quota_governor = quota_governor or QuotaGovernor(data_dir=self.data_dir)
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.campaigns_path):
            atomic_write_json(self.campaigns_path, [])
        if not os.path.exists(self.dispatch_log_path):
            atomic_write_text(self.dispatch_log_path, "")

    def _load_leads(self) -> List[Dict[str, Any]]:
        try:
            with open(self.leads_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("leads", data) if isinstance(data, dict) else data
        except Exception:
            return []

    def _save_leads(self, leads: List[Dict[str, Any]]) -> None:
        try:
            with open(self.leads_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and "leads" in raw:
                raw["leads"] = leads
                data_to_write = raw
            else:
                data_to_write = leads
        except Exception:
            data_to_write = leads

        atomic_write_json(self.leads_path, data_to_write)

    def _load_timelines(self) -> Dict[str, List[Dict[str, Any]]]:
        try:
            with open(self.timelines_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _append_timeline_event(self, lead_id: str, event: Dict[str, Any]) -> None:
        timelines = self._load_timelines()
        if lead_id not in timelines:
            timelines[lead_id] = []
        timelines[lead_id].append(event)
        atomic_write_json(self.timelines_path, timelines)

    def _append_dispatch_log(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record) + "\n"
        with open(self.dispatch_log_path, "a", encoding="utf-8") as f:
            f.write(line)

    # -------------------------------------------------------------------------
    # 1. Audience Building (Section 15)
    # -------------------------------------------------------------------------
    def build_eligible_audience(
        self,
        market_id: str = "MANCHESTER_UK",
        max_batch: int = MAX_EMAIL_BATCH,
    ) -> List[Dict[str, Any]]:
        """
        Filters authoritative CRM state for qualified candidates matching initial audience criteria:
          - qualification_state == "OUTREACH_READY"
          - activation_ready == True (not blocked)
          - email discovered
          - email not suppressed
        Strictly excludes:
          - RESEARCH_ONLY
          - MANUAL_REVIEW
          - EXCLUDED
          - NOT_READY
        """
        all_leads = self._load_leads()
        eligible: List[Dict[str, Any]] = []

        for lead in all_leads:
            qstate = lead.get("qualification_state")
            if qstate != "OUTREACH_READY":
                continue

            # Must have email
            email = lead.get("email") or lead.get("verified_email")
            if not email:
                continue

            # Exclude already contacted/sent via email in baseline cohort
            ostatus = lead.get("outreach_status")
            if ostatus in ("SENT", "CONTACTED", "BOUNCED", "UNSUBSCRIBED"):
                continue

            # Exclude activation blocked
            if lead.get("activation_blocked", False):
                continue

            # Check suppression
            lead_id = lead.get("lead_id", "")
            is_supp, _ = self.suppression_manager.is_suppressed(email=email, lead_id=lead_id)
            if is_supp:
                continue

            eligible.append(lead)
            if len(eligible) >= max_batch:
                break

        return eligible

    # -------------------------------------------------------------------------
    # 2. Preview / Dry Run (Section 30)
    # -------------------------------------------------------------------------
    def run_dry_run(
        self,
        candidates: Optional[List[Dict[str, Any]]] = None,
        max_batch: int = MAX_EMAIL_BATCH,
    ) -> Dict[str, Any]:
        """
        Deterministic pre-flight dry run showing exactly:
        recipient, company, classification, compliance decision, email, template,
        rendered message, suppression decision, sendability, and reason.
        NO provider call, NO external email send.
        """
        pool = candidates if candidates is not None else self.build_eligible_audience(max_batch=max_batch)
        sender_health = self.sender_auditor.check_sender_health()

        preview_records: List[Dict[str, Any]] = []
        eligible_count = 0
        blocked_count = 0

        for lead in pool:
            email = (lead.get("email") or lead.get("verified_email") or "").strip()
            lead_id = lead.get("lead_id", "")
            company_name = lead.get("company_name", "")

            # Sendability evaluation
            eval_res: SendabilityEvaluation = self.sendability_gate.evaluate_sendability(
                lead=lead,
                email_candidate=email,
                sender_configured=sender_health["credentials_configured"],
                check_automation_enabled=False,  # dry-run can evaluate potential candidates
                dry_run=True,
            )

            # Generate sample unsubscribe link for preview
            _, unsub_url = self.suppression_manager.generate_unsubscribe_link(
                lead_id=lead_id, email=email, campaign_id="DRY_RUN"
            )

            subject, body, pmeta = EmailTemplateEngine.render_website_email(
                lead=lead,
                unsubscribe_url=unsub_url,
                sender_name=sender_health["identity"]["sender_name"],
            )

            qa_pass, qa_fails, hashes = EmailTemplateEngine.validate_message_qa(
                subject=subject, body=body, lead=lead, personalization_meta=pmeta
            )

            is_overall_sendable = eval_res.is_sendable and qa_pass
            if is_overall_sendable:
                eligible_count += 1
            else:
                blocked_count += 1

            record = {
                "lead_id": lead_id,
                "company_name": company_name,
                "recipient_email": email,
                "classification": eval_res.compliance_object.get("recipient_type", SubscriberClass.UNKNOWN),
                "compliance_decision": eval_res.compliance_object.get("compliance_status", ComplianceStatus.BLOCKED),
                "compliance_reason": eval_res.compliance_object.get("reason", ""),
                "suppression_decision": "SUPPRESSED" if eval_res.states.EMAIL_SUPPRESSED else "CLEAR",
                "sendability": "SENDABLE" if is_overall_sendable else "BLOCK_SEND",
                "sendability_reason": eval_res.reason if not is_overall_sendable else "All criteria passed",
                "qa_passed": qa_pass,
                "qa_failures": qa_fails,
                "template": TEMPLATE_VERSION_V1,
                "message_preview": {
                    "subject": subject,
                    "body": body,
                    "message_hash": hashes.get("message_hash"),
                }
            }
            preview_records.append(record)

        return {
            "timestamp": _now_utc(),
            "mode": "DRY_RUN",
            "dry_run": True,
            "sender_health": sender_health,
            "total_evaluated": len(pool),
            "eligible_sendable": eligible_count,
            "blocked": blocked_count,
            "candidates": preview_records,
            "preview_results": preview_records,
            "external_sends_made": 0,
        }

    # -------------------------------------------------------------------------
    # 3. Controlled Batch Execution (Section 13, 16, 31)
    # -------------------------------------------------------------------------
    def execute_automated_batch(
        self,
        campaign_id: str = "CAMP-EMAIL-001",
        max_batch: int = MAX_EMAIL_BATCH,
        dry_run: bool = False,
        allow_sandbox: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes an automated email outreach batch of at most max_batch (<= 5).
        Enforces all stop conditions, idempotency, rate limiting, and compliance gates.
        """
        run_id = f"RUN-EMAIL-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"

        if dry_run:
            return self.run_dry_run(max_batch=max_batch)

        # Master Switch & Safety Assertions (Section 1)
        SystemConfig.assert_automated_email_allowed("execute_automated_batch")

        # Cap max_batch strictly at 5 for initial batches (Section 16, 39)
        max_batch = min(max_batch, MAX_EMAIL_BATCH)

        # Sender Health Validation (Section 11)
        sender_health = self.sender_auditor.check_sender_health()
        if not sender_health["can_send_automated"] and not allow_sandbox:
            raise EmailAutomationBlockedError(
                f"Automated email execution BLOCKED by sender health check: "
                f"SPF={sender_health['SPF']}, DKIM={sender_health['DKIM']}, "
                f"DMARC={sender_health['DMARC']}, Status={sender_health['overall_status']}"
            )

        pool = self.build_eligible_audience(max_batch=max_batch)

        metrics = {
            "eligible": len(pool),
            "compliance_blocked": 0,
            "suppressed": 0,
            "invalid": 0,
            "duplicate_blocked": 0,
            "rate_limit_paused": 0,
            "queued": 0,
            "attempted": 0,
            "submitted": 0,
            "delivered": 0,
            "bounced": 0,
            "failed": 0,
            "unknown": 0,
            "replied": 0,
            "unsubscribed": 0,
        }

        provider_message_ids: List[str] = []
        execution_records: List[Dict[str, Any]] = []
        automation_stop_reason: Optional[str] = None

        all_crm_leads = self._load_leads()
        crm_leads_by_id = {l.get("lead_id"): l for l in all_crm_leads}

        for lead in pool:
            lead_id = lead.get("lead_id")
            company_name = lead.get("company_name", "")
            email = (lead.get("email") or lead.get("verified_email") or "").strip()
            domain = email.split("@")[-1].lower() if "@" in email else ""

            # 1. Sendability & Compliance Evaluation
            eval_res: SendabilityEvaluation = self.sendability_gate.evaluate_sendability(
                lead=lead,
                email_candidate=email,
                sender_configured=sender_health["credentials_configured"],
                check_automation_enabled=True,
                dry_run=False,
            )

            if not eval_res.is_sendable:
                if eval_res.states.EMAIL_SUPPRESSED:
                    metrics["suppressed"] += 1
                elif not eval_res.states.EMAIL_COMPLIANT:
                    metrics["compliance_blocked"] += 1
                elif eval_res.states.EMAIL_INVALID:
                    metrics["invalid"] += 1
                continue

            # 2. Idempotency Check (Section 18)
            idempotency_key = EmailGovernor.generate_idempotency_key(
                lead_id=lead_id,
                email=email,
                campaign_id=campaign_id,
                template_version=TEMPLATE_VERSION_V1,
                attempt_number=1,
            )

            prior_dispatch = self.governor.get_idempotent_record(idempotency_key)
            if prior_dispatch:
                metrics["duplicate_blocked"] += 1
                continue

            # 3. Rate Limit Check (Section 17)
            rate_ok, rate_pause_reason = self.governor.check_rate_limits(domain)
            if not rate_ok:
                metrics["rate_limit_paused"] += 1
                automation_stop_reason = rate_pause_reason
                logger.warning(f"Batch paused by rate limit: {rate_pause_reason}")
                break

            # 4. Message Rendering & QA
            _, unsub_url = self.suppression_manager.generate_unsubscribe_link(
                lead_id=lead_id, email=email, campaign_id=campaign_id
            )
            subject, body, pmeta = EmailTemplateEngine.render_website_email(
                lead=lead,
                unsubscribe_url=unsub_url,
                sender_name=sender_health["identity"]["sender_name"],
            )

            qa_pass, qa_fails, hashes = EmailTemplateEngine.validate_message_qa(
                subject=subject, body=body, lead=lead, personalization_meta=pmeta
            )

            if not qa_pass:
                metrics["invalid"] += 1
                logger.error(f"Message QA failed for {lead_id}: {qa_fails}")
                continue

            # 5. Consume Quota
            try:
                self.quota_governor.check_and_consume("crm_writes", 1, run_id=run_id)
            except QuotaExhaustedError:
                automation_stop_reason = "QUOTA_EXHAUSTED: crm_writes limit reached"
                break

            # 6. Attempt Dispatch
            metrics["queued"] += 1
            metrics["attempted"] += 1

            self.governor.record_send(recipient=email, domain=domain)

            provider_res: EmailProviderResult = self.provider.send_email(
                recipient=email,
                subject=subject,
                body=body,
                idempotency_key=idempotency_key,
            )

            # Record Idempotency
            dispatch_record = {
                "run_id": run_id,
                "campaign_id": campaign_id,
                "lead_id": lead_id,
                "business_name": company_name,
                "email": email,
                "email_source": lead.get("email_source") or "OFFICIAL_WEBSITE",
                "recipient_classification": eval_res.compliance_object.get("recipient_type"),
                "compliance_status": eval_res.compliance_object.get("compliance_status"),
                "template_version": TEMPLATE_VERSION_V1,
                "message_hash": hashes.get("message_hash"),
                "personalization_hash": hashes.get("personalization_hash"),
                "attempt_number": 1,
                "provider": provider_res.provider,
                "provider_message_id": provider_res.provider_message_id,
                "submitted_at": provider_res.submitted_at,
                "provider_result": provider_res.status,
                "error": provider_res.error,
            }
            self.governor.record_idempotent_dispatch(idempotency_key, dispatch_record)
            self._append_dispatch_log(dispatch_record)

            # Update Metrics & Process Status
            if provider_res.status == ProviderDeliveryStatus.SUBMITTED:
                metrics["submitted"] += 1
                if provider_res.provider_message_id:
                    provider_message_ids.append(provider_res.provider_message_id)

                # Update CRM
                if lead_id in crm_leads_by_id:
                    crm_lead = crm_leads_by_id[lead_id]
                    crm_lead["outreach_status"] = "SENT"
                    crm_lead["outreach_channel"] = "EMAIL"
                    crm_lead["outreach_sent_at"] = provider_res.submitted_at
                    crm_lead["outreach_message_id"] = provider_res.provider_message_id or ""
                    crm_lead["marketing_email_status"] = "COMPLIANCE_ELIGIBLE"

                # Timeline event
                self._append_timeline_event(lead_id, {
                    "event_type": "EMAIL_SUBMITTED",
                    "channel": "EMAIL",
                    "timestamp": provider_res.submitted_at,
                    "provider": provider_res.provider,
                    "provider_message_id": provider_res.provider_message_id,
                    "template_version": TEMPLATE_VERSION_V1,
                    "message_hash": hashes.get("message_hash"),
                    "operator": "AUTOMATED_EMAIL_ENGINE",
                })

            elif provider_res.status == ProviderDeliveryStatus.BOUNCED:
                metrics["bounced"] += 1
                self.governor.handle_bounce(
                    email=email,
                    lead_id=lead_id,
                    bounce_evidence=provider_res.error or provider_res.response_text,
                    is_hard_bounce=True,
                )
                if lead_id in crm_leads_by_id:
                    crm_lead = crm_leads_by_id[lead_id]
                    crm_lead["outreach_status"] = "BOUNCED"

                self._append_timeline_event(lead_id, {
                    "event_type": "EMAIL_BOUNCED",
                    "timestamp": provider_res.submitted_at,
                    "provider": provider_res.provider,
                    "error": provider_res.error,
                    "operator": "AUTOMATED_EMAIL_ENGINE",
                })

            elif provider_res.status == ProviderDeliveryStatus.UNKNOWN:
                metrics["unknown"] += 1
            else:
                metrics["failed"] += 1

            execution_records.append(dispatch_record)

            # Automatic Stop Conditions Check (Section 32)
            if metrics["attempted"] > 0:
                bounce_rate = (metrics["bounced"] / metrics["attempted"]) * 100
                fail_rate = (metrics["failed"] / metrics["attempted"]) * 100
                if bounce_rate > 20.0:
                    automation_stop_reason = f"HIGH_BOUNCE_RATE: Bounce rate reached {bounce_rate:.1f}%"
                    break
                if fail_rate > 30.0:
                    automation_stop_reason = f"HIGH_FAILURE_RATE: Provider failure rate reached {fail_rate:.1f}%"
                    break

        # Persist updated CRM leads under lock
        with crm_write_lock():
            self._save_leads(all_crm_leads)

        # Compute Final Analytics
        run_output = {
            "RUN_ID": run_id,
            "GENERATED_AT": _now_utc(),
            "AUTOMATED_EMAIL_ENABLED": SystemConfig.is_automated_email_enabled(),
            "BATCH_SIZE": len(execution_records),
            "MAX_ALLOWED_BATCH": MAX_EMAIL_BATCH,
            "ELIGIBLE": metrics["eligible"],
            "COMPLIANCE_BLOCKED": metrics["compliance_blocked"],
            "SUPPRESSED": metrics["suppressed"],
            "INVALID": metrics["invalid"],
            "DUPLICATE_BLOCKED": metrics["duplicate_blocked"],
            "QUEUED": metrics["queued"],
            "ATTEMPTED": metrics["attempted"],
            "SUBMITTED": metrics["submitted"],
            "DELIVERED": metrics["delivered"],
            "BOUNCED": metrics["bounced"],
            "FAILED": metrics["failed"],
            "UNKNOWN": metrics["unknown"],
            "REPLIED": metrics["replied"],
            "UNSUBSCRIBED": metrics["unsubscribed"],
            "PROVIDER_MESSAGE_IDS": provider_message_ids,
            "DAILY_QUOTA": {
                "used": metrics["submitted"],
                "limit": self.governor.limit_per_day,
            },
            "HOURLY_QUOTA": {
                "used": metrics["submitted"],
                "limit": self.governor.limit_per_hour,
            },
            "DOMAIN_QUOTA": {
                "limit_per_domain": self.governor.limit_per_domain,
            },
            "AUTOMATION_STOP_REASON": automation_stop_reason,
            "PHONE_AUTOMATION": False,
            "INSTAGRAM_AUTOMATION": False,
            "FACEBOOK_AUTOMATION": False,
            "PROPOSAL_AUTOMATION": False,
            "FOLLOWUP_AUTOMATION": False,
            "DISPATCHES": execution_records,
        }

        self.export_machine_run_artifact(run_output)
        return run_output

    def export_machine_run_artifact(self, run_data: Dict[str, Any]) -> str:
        """
        Persists machine output artifact (Section 37).
        """
        atomic_write_json(self.run_artifact_path, run_data)
        return self.run_artifact_path

    # -------------------------------------------------------------------------
    # 4. Inbound Reply Detection Infrastructure (Section 24 & 26)
    # -------------------------------------------------------------------------
    def record_inbound_reply(
        self,
        lead_id: str,
        message_id: str,
        thread_ref: Optional[str] = None,
        provider: str = "INBOUND_WEBHOOK",
        snippet: str = "",
    ) -> Dict[str, Any]:
        """
        Handles inbound reply detection:
        - Logs EMAIL_REPLY_RECEIVED timeline event.
        - Sets commercial_stage = CONTACTED.
        - Routes to OPERATOR_REVIEW.
        - Strictly DOES NOT auto-generate replies, proposals, or INTERESTED state.
        """
        now_ts = _now_utc()

        # Update Lead CRM state
        all_leads = self._load_leads()
        found_lead = None
        for l in all_leads:
            if l.get("lead_id") == lead_id:
                found_lead = l
                l["outreach_status"] = "REPLIED"
                l["reply_received_at"] = now_ts
                l["manual_outreach_notes"] = f"Inbound email reply received ({provider}): {snippet[:100]}"
                break

        if found_lead:
            with crm_write_lock():
                self._save_leads(all_leads)

        # Update Commercial Records
        try:
            with open(self.commercial_path, "r", encoding="utf-8") as f:
                comm_data = json.load(f)
            if lead_id in comm_data:
                # Do NOT auto-advance to INTERESTED; set to CONTACTED and route to operator review
                comm_data[lead_id]["commercial_stage"] = "CONTACTED"
                comm_data[lead_id]["inbound_reply_pending_review"] = True
                atomic_write_json(self.commercial_path, comm_data)
        except Exception:
            pass

        # Append timeline event
        event = {
            "event_type": "EMAIL_REPLY_RECEIVED",
            "timestamp": now_ts,
            "provider": provider,
            "message_id": message_id,
            "thread_ref": thread_ref or "",
            "snippet": snippet[:200],
            "next_step": "OPERATOR_REVIEW",
            "auto_reply_sent": False,
        }
        self._append_timeline_event(lead_id, event)

        return {
            "status": "REPLY_RECORDED",
            "lead_id": lead_id,
            "message_id": message_id,
            "routed_to": "OPERATOR_REVIEW",
            "commercial_stage": "CONTACTED",
            "stage": "CONTACTED",
            "next_action": "OPERATOR_REVIEW",
        }

    # -------------------------------------------------------------------------
    # 5. Emergency Stop (Section 33)
    # -------------------------------------------------------------------------
    def emergency_stop(
        self,
        reason: str = "Operator manual emergency stop",
        operator: str = "HUMAN_OPERATOR",
    ) -> Dict[str, Any]:
        """
        Engages the emergency kill switch for automated email outreach.
        Idempotent. Prevents all new email dispatches immediately.
        """
        SystemConfig.set_email_kill_switch(True)
        record = {
            "emergency_stop_active": True,
            "emergency_stop_at": _now_utc(),
            "reason": reason,
            "operator": operator,
        }
        stop_path = os.path.join(self.data_dir, "email_emergency_stop.json")
        atomic_write_json(stop_path, record)
        logger.warning(f"EMERGENCY STOP ACTIVATED: {reason} by {operator}")
        return record

    # -------------------------------------------------------------------------
    # 6. Analytics & Rates (Section 25)
    # -------------------------------------------------------------------------
    def generate_analytics(self) -> Dict[str, Any]:
        """
        Calculates exact outreach rates with explicit denominators.
        Does not count queued or attempted emails as delivered.
        """
        try:
            with open(self.run_artifact_path, "r", encoding="utf-8") as f:
                last_run = json.load(f)
        except Exception:
            last_run = {}

        submitted = last_run.get("SUBMITTED", 0)
        delivered = last_run.get("DELIVERED", submitted)  # conservative estimate
        bounced = last_run.get("BOUNCED", 0)
        replied = last_run.get("REPLIED", 0)
        unsubscribed = last_run.get("UNSUBSCRIBED", 0)

        # Explicit denominators
        delivery_rate = (delivered / submitted * 100.0) if submitted > 0 else 0.0
        bounce_rate = (bounced / submitted * 100.0) if submitted > 0 else 0.0
        reply_rate = (replied / delivered * 100.0) if delivered > 0 else 0.0
        unsubscribe_rate = (unsubscribed / delivered * 100.0) if delivered > 0 else 0.0

        analytics = {
            "generated_at": _now_utc(),
            "totals": {
                "queued": last_run.get("QUEUED", 0),
                "attempted": last_run.get("ATTEMPTED", 0),
                "submitted": submitted,
                "delivered": delivered,
                "bounced": bounced,
                "failed": last_run.get("FAILED", 0),
                "unknown": last_run.get("UNKNOWN", 0),
                "replied": replied,
                "unsubscribed": unsubscribed,
            },
            "rates_pct": {
                "delivery_rate": round(delivery_rate, 2),
                "bounce_rate": round(bounce_rate, 2),
                "reply_rate": round(reply_rate, 2),
                "unsubscribe_rate": round(unsubscribe_rate, 2),
            },
            "denominators": {
                "delivery_rate_denominator": "emails_submitted",
                "bounce_rate_denominator": "emails_submitted",
                "reply_rate_denominator": "emails_delivered",
                "unsubscribe_rate_denominator": "emails_delivered",
            }
        }
        atomic_write_json(self.analytics_path, analytics)
        return analytics
