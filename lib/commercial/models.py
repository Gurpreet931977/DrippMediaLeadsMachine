"""
Post-9.7 Commercial Conversion Workflow: Canonical Data Models & Constants

Defines:
  1. Outcome Provenance:
     - SYSTEM_VERIFIED: Deterministically verified by automated tests/code.
     - OPERATOR_REPORTED: Explicitly entered by human operator (mandatory for manual phone calls).
     - PROVIDER_CONFIRMED: Confirmed by telecom or messaging provider webhook/receipt.
  2. Commercial Stage:
     - QUALIFIED, CONTACTED, INTERESTED, FOLLOW_UP_REQUIRED, PREVIEW_REQUESTED,
       PREVIEW_SENT, PROPOSAL_REQUESTED, PROPOSAL_SENT, NEGOTIATING, WON, LOST, NOT_INTERESTED.
  3. Website Pipeline Stage:
     - NO_WEBSITE, CONTACTED, OPEN_TO_DISCUSSION, PREVIEW_SENT, INTERESTED,
       PROPOSAL_REQUESTED, PROPOSAL_SENT, NEGOTIATING, WON, LOST.
  4. Structured Notes, Previews, Proposals, Follow-Ups, and Won/Lost records.
  5. Strict Safety Invariants:
     - AUTO_CALL = False
     - AUTO_DM = False
     - AUTO_EMAIL = False
     - AUTO_FOLLOWUP = False
     - AUTO_CALLBACK = False
     - AUTO_PROPOSAL = False
     - AUTO_CONTINUATION = False
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Any, Optional, List
import uuid


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------------------------------------------------------
# 1. Outcome Provenance (Section 1)
# -----------------------------------------------------------------------------
class OutcomeProvenance(str, Enum):
    SYSTEM_VERIFIED = "SYSTEM_VERIFIED"
    OPERATOR_REPORTED = "OPERATOR_REPORTED"
    PROVIDER_CONFIRMED = "PROVIDER_CONFIRMED"


VALID_PROVENANCE = {p.value for p in OutcomeProvenance}


# -----------------------------------------------------------------------------
# 2. Commercial Stages (Section 2)
# -----------------------------------------------------------------------------
class CommercialStage(str, Enum):
    QUALIFIED = "QUALIFIED"
    CONTACTED = "CONTACTED"
    INTERESTED = "INTERESTED"
    FOLLOW_UP_REQUIRED = "FOLLOW_UP_REQUIRED"
    PREVIEW_REQUESTED = "PREVIEW_REQUESTED"
    PREVIEW_SENT = "PREVIEW_SENT"
    PROPOSAL_REQUESTED = "PROPOSAL_REQUESTED"
    PROPOSAL_SENT = "PROPOSAL_SENT"
    NEGOTIATING = "NEGOTIATING"
    WON = "WON"
    LOST = "LOST"
    NOT_INTERESTED = "NOT_INTERESTED"


VALID_COMMERCIAL_STAGES = {s.value for s in CommercialStage}


# -----------------------------------------------------------------------------
# 3. Website Sales Pipeline Stages (Section 9)
# -----------------------------------------------------------------------------
class WebsitePipelineStage(str, Enum):
    NO_WEBSITE = "NO_WEBSITE"
    CONTACTED = "CONTACTED"
    OPEN_TO_DISCUSSION = "OPEN_TO_DISCUSSION"
    PREVIEW_SENT = "PREVIEW_SENT"
    INTERESTED = "INTERESTED"
    PROPOSAL_REQUESTED = "PROPOSAL_REQUESTED"
    PROPOSAL_SENT = "PROPOSAL_SENT"
    NEGOTIATING = "NEGOTIATING"
    WON = "WON"
    LOST = "LOST"


VALID_WEBSITE_STAGES = {s.value for s in WebsitePipelineStage}


# -----------------------------------------------------------------------------
# 4. Interest Level (Section 8)
# -----------------------------------------------------------------------------
class InterestLevel(str, Enum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


VALID_INTEREST_LEVELS = {i.value for i in InterestLevel}


# -----------------------------------------------------------------------------
# 5. Preview Status (Section 6)
# -----------------------------------------------------------------------------
class PreviewStatus(str, Enum):
    NONE = "NONE"
    PREVIEW_DRAFT = "PREVIEW_DRAFT"
    PREVIEW_READY = "PREVIEW_READY"
    PREVIEW_SENT = "PREVIEW_SENT"
    PREVIEW_FEEDBACK_RECEIVED = "PREVIEW_FEEDBACK_RECEIVED"


VALID_PREVIEW_STATUSES = {p.value for p in PreviewStatus}


# -----------------------------------------------------------------------------
# 6. Proposal States (Section 1 & 10)
# -----------------------------------------------------------------------------
class ProposalState(str, Enum):
    DRAFT = "DRAFT"
    INTERNAL_REVIEW = "INTERNAL_REVIEW"
    READY_TO_SEND = "READY_TO_SEND"
    SENT = "SENT"
    VIEWED = "VIEWED"
    NEGOTIATING = "NEGOTIATING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


VALID_PROPOSAL_STATES = {s.value for s in ProposalState}


class ProposalStatus(str, Enum):
    """Backward compatibility alias for Phase 9.7 models."""
    NONE = "NONE"
    REQUESTED = "REQUESTED"
    DRAFTED = "DRAFTED"
    DRAFT = "DRAFT"
    INTERNAL_REVIEW = "INTERNAL_REVIEW"
    READY_TO_SEND = "READY_TO_SEND"
    SENT = "SENT"
    VIEWED = "VIEWED"
    NEGOTIATING = "NEGOTIATING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


VALID_PROPOSAL_STATUSES = {p.value for p in ProposalStatus}


class ProposalPackage(str, Enum):
    CUSTOM = "CUSTOM"
    STARTER = "STARTER"
    STANDARD = "STANDARD"
    PREMIUM = "PREMIUM"


VALID_PACKAGES = {p.value for p in ProposalPackage}


# -----------------------------------------------------------------------------
# 7. Lost Reason (Section 11 & Section 20)
# -----------------------------------------------------------------------------
class LostReason(str, Enum):
    PRICE = "PRICE"
    TIMING = "TIMING"
    NO_NEED = "NO_NEED"
    CHOSE_OTHER_PROVIDER = "CHOSE_OTHER_PROVIDER"
    NO_RESPONSE = "NO_RESPONSE"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    OTHER = "OTHER"


VALID_LOST_REASONS = {r.value for r in LostReason}


# -----------------------------------------------------------------------------
# 8. Next Commercial Action Types (Section 13 & 15 & Section 21)
# -----------------------------------------------------------------------------
class NextActionType(str, Enum):
    CALLBACK_TODAY = "CALLBACK TODAY"
    CALLBACK = "CALLBACK"
    SEND_PREVIEW = "SEND PREVIEW"
    WAIT_FOR_RESPONSE = "WAIT_FOR_RESPONSE"
    CREATE_PROPOSAL = "CREATE_PROPOSAL"
    REVIEW_PROPOSAL = "REVIEW PROPOSAL"
    OPERATOR_FOLLOW_UP = "OPERATOR_FOLLOW_UP"
    FOLLOW_UP = "FOLLOW UP"
    RETRY_CONTACT = "RETRY CONTACT"
    OPERATOR_REVIEW = "OPERATOR_REVIEW"
    REVIEW_NEGOTIATION = "REVIEW_NEGOTIATION"
    ONBOARDING = "ONBOARDING"
    NO_ACTION = "NO ACTION"


# -----------------------------------------------------------------------------
# 9. Commercial Event Types (Section 19 & Section 30)
# -----------------------------------------------------------------------------
class CommercialEventType(str, Enum):
    COMMERCIAL_STAGE_CHANGED = "COMMERCIAL_STAGE_CHANGED"
    FOLLOW_UP_CREATED = "FOLLOW_UP_CREATED"
    FOLLOW_UP_COMPLETED = "FOLLOW_UP_COMPLETED"
    PREVIEW_CREATED = "PREVIEW_CREATED"
    PREVIEW_SENT = "PREVIEW_SENT"
    PROPOSAL_CREATED = "PROPOSAL_CREATED"
    PROPOSAL_APPROVED = "PROPOSAL_APPROVED"
    PROPOSAL_SENT = "PROPOSAL_SENT"
    PROPOSAL_VERSION_CREATED = "PROPOSAL_VERSION_CREATED"
    NEGOTIATION_STARTED = "NEGOTIATION_STARTED"
    NEGOTIATION_UPDATED = "NEGOTIATION_UPDATED"
    DEAL_WON = "DEAL_WON"
    DEAL_LOST = "DEAL_LOST"
    COMMERCIAL_NOTES_UPDATED = "COMMERCIAL_NOTES_UPDATED"


VALID_COMMERCIAL_EVENT_TYPES = {e.value for e in CommercialEventType}


# -----------------------------------------------------------------------------
# 10. Strict Automation Safety Invariants (Section 20 & Section 28)
# -----------------------------------------------------------------------------
AUTO_CALL: bool = False
AUTO_DM: bool = False
AUTO_EMAIL: bool = False
AUTO_FOLLOWUP: bool = False
AUTO_CALLBACK: bool = False
AUTO_PROPOSAL: bool = False
AUTO_SEND_PROPOSAL: bool = False
AUTO_EMAIL_PROPOSAL: bool = False
AUTO_PAYMENT_REQUEST: bool = False
AUTO_CONTRACT: bool = False
AUTO_CONTINUATION: bool = False


# -----------------------------------------------------------------------------
# 11. Dataclasses
# -----------------------------------------------------------------------------
@dataclass
class CommercialNotes:
    """Structured commercial observations captured by operator (Section 7)."""
    decision_maker: str = ""
    role: str = ""
    interest_level: str = "UNKNOWN"
    current_website_situation: str = ""
    requested_service: str = ""
    budget_signal: str = ""
    timeline_signal: str = ""
    objection: str = ""
    next_step: str = ""
    updated_at: str = field(default_factory=now_utc_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_maker": self.decision_maker,
            "role": self.role,
            "interest_level": self.interest_level,
            "current_website_situation": self.current_website_situation,
            "requested_service": self.requested_service,
            "budget_signal": self.budget_signal,
            "timeline_signal": self.timeline_signal,
            "objection": self.objection,
            "next_step": self.next_step,
            "updated_at": self.updated_at,
        }


@dataclass
class FollowUpRecord:
    """Follow-up or callback scheduled for operator execution (Section 4)."""
    follow_up_id: str
    lead_id: str
    company_name: str
    follow_up_type: str = "CALLBACK"  # CALLBACK, MESSAGE, EMAIL, IN_PERSON
    scheduled_for: str = ""
    auto_schedule: bool = False
    auto_call: bool = False
    status: str = "PENDING_OPERATOR"  # PENDING_OPERATOR, COMPLETED, CANCELLED
    notes: str = ""
    created_at: str = field(default_factory=now_utc_iso)
    completed_at: Optional[str] = None
    outcome: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "follow_up_id": self.follow_up_id,
            "lead_id": self.lead_id,
            "company_name": self.company_name,
            "follow_up_type": self.follow_up_type,
            "scheduled_for": self.scheduled_for,
            "auto_schedule": self.auto_schedule,
            "auto_call": self.auto_call,
            "status": self.status,
            "notes": self.notes,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "outcome": self.outcome,
        }


@dataclass
class PreviewRecord:
    """Preview tracking for concept demonstration (Section 5 & 6)."""
    lead_id: str
    company_name: str
    preview_status: str = "PREVIEW_DRAFT"
    preview_url: str = ""
    preview_description: str = ""
    what_demonstrated: str = ""
    next_action: str = ""
    operator_notes: str = ""
    created_at: str = field(default_factory=now_utc_iso)
    sent_at: Optional[str] = None
    viewed_at: Optional[str] = None  # None unless independent proof exists
    marked_sent_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "company_name": self.company_name,
            "preview_status": self.preview_status,
            "preview_url": self.preview_url,
            "preview_description": self.preview_description,
            "what_demonstrated": self.what_demonstrated,
            "next_action": self.next_action,
            "operator_notes": self.operator_notes,
            "created_at": self.created_at,
            "sent_at": self.sent_at,
            "viewed_at": self.viewed_at,
            "marked_sent_by": self.marked_sent_by,
        }


@dataclass
class ProposalRecord:
    """Proposal tracking for negotiated deals (Section 10)."""
    lead_id: str
    company_name: str
    proposal_status: str = "NONE"
    proposal_amount: Optional[float] = None
    proposal_currency: str = "GBP"
    sent_at: Optional[str] = None
    notes: str = ""
    created_at: str = field(default_factory=now_utc_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "company_name": self.company_name,
            "proposal_status": self.proposal_status,
            "proposal_amount": self.proposal_amount,
            "proposal_currency": self.proposal_currency,
            "sent_at": self.sent_at,
            "notes": self.notes,
            "created_at": self.created_at,
        }


@dataclass
class ProposalDocument:
    """Canonical proposal document for commercial deals (Section 2, 16, 17, 18)."""
    proposal_id: str
    lead_id: str
    company_name: str
    service: str = "WEBSITE_DEVELOPMENT"
    package_name: str = "CUSTOM"
    currency: str = "GBP"
    subtotal: float = 0.0
    discount: float = 0.0
    total: float = 0.0
    payment_terms: str = "50% upfront deposit upon agreement, 50% upon final delivery prior to launch"
    timeline: str = "10 - 14 business days"
    revision_limit: str = "2 revision rounds included"
    scope: List[str] = field(default_factory=list)
    exclusions: List[str] = field(default_factory=list)
    deliverables: List[str] = field(default_factory=list)
    valid_until: str = ""
    status: str = "DRAFT"
    created_at: str = field(default_factory=now_utc_iso)
    sent_at: Optional[str] = None
    operator_notes: str = ""
    version: int = 1
    supersedes_proposal_id: Optional[str] = None
    superseded_by_proposal_id: Optional[str] = None
    change_reason: Optional[str] = None
    delivery_method: Optional[str] = None
    negotiation_notes: List[Dict[str, Any]] = field(default_factory=list)
    quoted_value: float = 0.0
    final_agreed_value: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "lead_id": self.lead_id,
            "company_name": self.company_name,
            "service": self.service,
            "package_name": self.package_name,
            "currency": self.currency,
            "subtotal": round(self.subtotal, 2),
            "discount": round(self.discount, 2),
            "total": round(self.total, 2),
            "payment_terms": self.payment_terms,
            "timeline": self.timeline,
            "revision_limit": self.revision_limit,
            "scope": list(self.scope),
            "exclusions": list(self.exclusions),
            "deliverables": list(self.deliverables),
            "valid_until": self.valid_until,
            "status": self.status,
            "created_at": self.created_at,
            "sent_at": self.sent_at,
            "operator_notes": self.operator_notes,
            "version": self.version,
            "supersedes_proposal_id": self.supersedes_proposal_id,
            "superseded_by_proposal_id": self.superseded_by_proposal_id,
            "change_reason": self.change_reason,
            "delivery_method": self.delivery_method,
            "negotiation_notes": self.negotiation_notes,
            "quoted_value": round(self.quoted_value, 2),
            "final_agreed_value": round(self.final_agreed_value, 2) if self.final_agreed_value is not None else None,
        }


@dataclass
class CommercialEvent:
    """Append-only commercial audit event (Section 19 & 30)."""
    event_id: str
    lead_id: str
    event_type: str
    previous_stage: str
    new_stage: str
    reason: str
    notes: str
    proposal_id: Optional[str] = None
    operator: str = "OPERATOR"
    source: str = "OPERATOR_REPORTED"
    timestamp: str = field(default_factory=now_utc_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "lead_id": self.lead_id,
            "proposal_id": self.proposal_id,
            "event_type": self.event_type,
            "previous_stage": self.previous_stage,
            "new_stage": self.new_stage,
            "reason": self.reason,
            "notes": self.notes,
            "operator": self.operator,
            "source": self.source,
            "timestamp": self.timestamp,
        }

