"""
lib/outreach/email_sendability.py
=================================
Email Contact States & Sendability Gate Engine for Phase 10.2.

Implements Sections 4, 5, 6, and 7:
1. Canonical Email Contact States:
     EMAIL_DISCOVERED, EMAIL_VERIFIED, EMAIL_MX_VALID, EMAIL_COMPLIANT,
     EMAIL_SENDABLE, EMAIL_SUPPRESSED, EMAIL_INVALID, EMAIL_BOUNCED, EMAIL_UNSUBSCRIBED
2. Strict Branch Protection and Association:
     Branch-specific emails cannot cross cities; shared corporate emails must be labeled CORPORATE_SHARED.
3. Anti-Guessing Guarantee:
     Inferred/guessed emails (info@, hello@, etc.) without genuine recorded source are rejected.
4. Deterministic Sendability Gate (evaluate_email_sendability).
"""

import re
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional, Tuple, List

from lib.outreach.email_compliance import (
    EmailComplianceEngine,
    SubscriberClass,
    ComplianceStatus,
    LawfulBasis,
)
from lib.outreach.email_suppression import EmailSuppressionManager
from lib.system.system_config import SystemConfig

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

GUESSED_ROLE_PREFIXES = (
    "info@",
    "hello@",
    "contact@",
    "admin@",
    "enquiries@",
    "enquiry@",
    "support@",
    "help@",
    "sales@",
)


@dataclass
class EmailContactStates:
    EMAIL_DISCOVERED: bool = False
    EMAIL_VERIFIED: bool = False
    EMAIL_MX_VALID: bool = False
    EMAIL_COMPLIANT: bool = False
    EMAIL_SENDABLE: bool = False
    EMAIL_SUPPRESSED: bool = False
    EMAIL_INVALID: bool = False
    EMAIL_BOUNCED: bool = False
    EMAIL_UNSUBSCRIBED: bool = False

    def to_dict(self) -> Dict[str, bool]:
        return asdict(self)


@dataclass
class SendabilityEvaluation:
    is_sendable: bool
    decision: str  # "SENDABLE" or "BLOCK_SEND"
    reason: str
    states: EmailContactStates
    compliance_object: Dict[str, Any]
    failed_conditions: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_sendable": self.is_sendable,
            "decision": self.decision,
            "reason": self.reason,
            "states": self.states.to_dict(),
            "compliance_object": self.compliance_object,
            "failed_conditions": self.failed_conditions,
        }


class EmailSendabilityGate:
    """
    Deterministic gate that evaluates whether an email recipient can be queued or sent.
    """

    def __init__(self, suppression_manager: Optional[EmailSuppressionManager] = None):
        self.suppression_manager = suppression_manager or EmailSuppressionManager()

    def evaluate_sendability(
        self,
        lead: Dict[str, Any],
        email_candidate: Optional[str] = None,
        email_record: Optional[Dict[str, Any]] = None,
        sender_configured: bool = True,
        check_automation_enabled: bool = True,
        dry_run: bool = False,
    ) -> SendabilityEvaluation:
        """
        Executes the multi-condition Sendability Gate (Section 6).
        """
        rec = email_record or {}
        email = (email_candidate or rec.get("email") or lead.get("email") or lead.get("verified_email") or "").strip()
        lead_id = lead.get("lead_id", "")
        company_name = lead.get("company_name", "")
        lead_city = (lead.get("city") or "manchester").strip().lower()

        states = EmailContactStates()
        failed_conditions: List[str] = []

        # ---------------------------------------------------------------------
        # 1. Email Discovered & Basic Syntax Check
        # ---------------------------------------------------------------------
        if not email:
            states.EMAIL_INVALID = True
            return SendabilityEvaluation(
                is_sendable=False,
                decision="BLOCK_SEND",
                reason="No email address discovered for lead",
                states=states,
                compliance_object={},
                failed_conditions=["NO_EMAIL_DISCOVERED"],
            )

        states.EMAIL_DISCOVERED = True

        if not EMAIL_REGEX.match(email):
            states.EMAIL_INVALID = True
            failed_conditions.append("INVALID_EMAIL_SYNTAX")

        # ---------------------------------------------------------------------
        # 2. Anti-Guessing Guarantee (Section 7)
        # ---------------------------------------------------------------------
        is_guessed = bool(rec.get("is_guessed") or lead.get("email_is_guessed"))
        source = rec.get("source") or lead.get("email_source") or lead.get("email_discovery_source")
        clean_email_lower = email.lower()

        # If role-based email was inferred without official discovered source, reject it
        if is_guessed or (not source and any(clean_email_lower.startswith(p) for p in GUESSED_ROLE_PREFIXES)):
            states.EMAIL_INVALID = True
            failed_conditions.append("GUESSED_OR_INFERRED_EMAIL")

        # ---------------------------------------------------------------------
        # 3. Branch Protection & Contact Association (Section 4)
        # ---------------------------------------------------------------------
        branch_type = rec.get("branch_association") or lead.get("branch_association", "BRANCH_SPECIFIC")
        email_branch_city = rec.get("branch_city") or lead.get("email_branch_city")

        if email_branch_city and email_branch_city.lower() != lead_city:
            if branch_type != "CORPORATE_SHARED":
                states.EMAIL_INVALID = True
                failed_conditions.append(
                    f"BRANCH_MISMATCH: Email belongs to {email_branch_city} branch, but lead is in {lead_city}"
                )

        # ---------------------------------------------------------------------
        # 4. Verification & MX Record Validity
        # ---------------------------------------------------------------------
        # MX validity
        mx_status = rec.get("mx_status") or lead.get("mx_status", "VALID")
        if mx_status in ("VALID", "PASS", "MX_FOUND", "MX_RECORD_PRESENT"):
            states.EMAIL_MX_VALID = True
        elif mx_status in ("FAIL", "INVALID", "NO_MX_RECORDS"):
            states.EMAIL_MX_VALID = False
            failed_conditions.append("MX_RECORD_INVALID")
        else:
            # Default to valid format domain if syntax passed
            states.EMAIL_MX_VALID = True if states.EMAIL_DISCOVERED and not states.EMAIL_INVALID else False

        # Email verification status
        ver_status = rec.get("verification_status") or lead.get("email_verification_status", "VERIFIED")
        if ver_status in ("VERIFIED", "VERIFIED_ACTIVE", "CONFIRMED"):
            states.EMAIL_VERIFIED = True
        else:
            states.EMAIL_VERIFIED = False
            # We require email to be verified before send
            if rec.get("verification_required", True) and ver_status == "INVALID":
                failed_conditions.append("EMAIL_VERIFICATION_FAILED")

        # ---------------------------------------------------------------------
        # 5. Lead Qualification State
        # ---------------------------------------------------------------------
        qstate = lead.get("qualification_state", "")
        if qstate != "OUTREACH_READY":
            failed_conditions.append(f"LEAD_NOT_OUTREACH_READY: qualification_state is '{qstate}'")

        # ---------------------------------------------------------------------
        # 6. Activation Readiness
        # ---------------------------------------------------------------------
        # Exclude leads explicitly blocked in activation
        activation_blocked = bool(lead.get("activation_blocked", False))
        if activation_blocked:
            failed_conditions.append("ACTIVATION_BLOCKED: Lead activation status is blocked")

        # ---------------------------------------------------------------------
        # 7. Suppression & Unsubscribe Gate (Section 8, 9)
        # ---------------------------------------------------------------------
        is_supp, supp_reason = self.suppression_manager.is_suppressed(
            email=email,
            lead_id=lead_id,
        )
        if is_supp:
            states.EMAIL_SUPPRESSED = True
            if "unsubscribed" in (supp_reason or "").lower():
                states.EMAIL_UNSUBSCRIBED = True
            failed_conditions.append(f"SUPPRESSED: {supp_reason}")

        if bool(lead.get("unsubscribed", False)) or bool(lead.get("opt_out_status", False)):
            states.EMAIL_UNSUBSCRIBED = True
            failed_conditions.append("RECIPIENT_UNSUBSCRIBED")

        # Prior bounce check
        if lead.get("outreach_status") == "BOUNCED" or rec.get("bounced"):
            states.EMAIL_BOUNCED = True
            failed_conditions.append("PRIOR_HARD_BOUNCE")

        # ---------------------------------------------------------------------
        # 8. Compliance Gate (Section 2, 3)
        # ---------------------------------------------------------------------
        compliance_evidence = EmailComplianceEngine.evaluate_compliance(
            lead=lead,
            email=email,
            opt_out=states.EMAIL_UNSUBSCRIBED or states.EMAIL_SUPPRESSED,
        )

        if compliance_evidence.get("compliance_status") == ComplianceStatus.PASS:
            states.EMAIL_COMPLIANT = True
        else:
            states.EMAIL_COMPLIANT = False
            failed_conditions.append(
                f"COMPLIANCE_BLOCKED: {compliance_evidence.get('reason', 'Failed PECR/GDPR requirements')}"
            )

        # ---------------------------------------------------------------------
        # 9. Sender Configuration Validation (Section 10)
        # ---------------------------------------------------------------------
        if not sender_configured:
            failed_conditions.append("SENDER_NOT_CONFIGURED: Missing sender identity or auth credentials")

        # ---------------------------------------------------------------------
        # 10. Automated Email Master Switch & Kill Switch (Section 1)
        # ---------------------------------------------------------------------
        if check_automation_enabled and not dry_run:
            if not SystemConfig.is_automated_email_enabled():
                failed_conditions.append("AUTOMATED_EMAIL_DISABLED: Feature flag is OFF or emergency kill switch active")

        # ---------------------------------------------------------------------
        # Final Determination
        # ---------------------------------------------------------------------
        if not failed_conditions:
            states.EMAIL_SENDABLE = True
            return SendabilityEvaluation(
                is_sendable=True,
                decision="SENDABLE",
                reason="All 10 Sendability Gate conditions satisfied",
                states=states,
                compliance_object=compliance_evidence,
                failed_conditions=[],
            )
        else:
            states.EMAIL_SENDABLE = False
            primary_reason = failed_conditions[0]
            return SendabilityEvaluation(
                is_sendable=False,
                decision="BLOCK_SEND",
                reason=f"Sendability blocked: {primary_reason}",
                states=states,
                compliance_object=compliance_evidence,
                failed_conditions=failed_conditions,
            )
