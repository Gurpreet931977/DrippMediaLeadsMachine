"""
Phase 9.6: Canonical Outreach Event Model

Defines an immutable, strictly validated canonical outreach event schema.
Enforces that outreach analytics do not depend on ad-hoc strings scattered across files.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Any, Optional


class OutreachChannelEnum(str, Enum):
    PHONE = "PHONE"
    INSTAGRAM = "INSTAGRAM"
    FACEBOOK = "FACEBOOK"
    EMAIL = "EMAIL"


class OutreachEventType(str, Enum):
    PREPARED = "PREPARED"
    PREVIEWED = "PREVIEWED"
    OPERATOR_CONFIRMED = "OPERATOR_CONFIRMED"
    OUTREACH_ATTEMPTED = "OUTREACH_ATTEMPTED"
    CALL_ATTEMPTED = "CALL_ATTEMPTED"
    CALL_CONNECTED = "CALL_CONNECTED"
    PROFILE_OPENED = "PROFILE_OPENED"
    MESSAGE_COPIED = "MESSAGE_COPIED"
    SEND_CONFIRMED = "SEND_CONFIRMED"
    OUTREACH_SENT = "OUTREACH_SENT"
    OUTCOME_RECORDED = "OUTCOME_RECORDED"
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
    CHANNEL_FAILURE = "CHANNEL_FAILURE"
    SUPPRESSED = "SUPPRESSED"
    MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"


class OutreachOutcomeEnum(str, Enum):
    CONNECTED = "CONNECTED"
    INTERESTED = "INTERESTED"
    CALLBACK_REQUESTED = "CALLBACK_REQUESTED"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    NOT_INTERESTED = "NOT_INTERESTED"
    WRONG_NUMBER = "WRONG_NUMBER"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    SENT = "SENT"
    BLOCKED = "BLOCKED"
    WRONG_ACCOUNT = "WRONG_ACCOUNT"
    BOUNCED = "BOUNCED"


class OutreachSourceEnum(str, Enum):
    OPERATOR = "OPERATOR"
    PROVIDER = "PROVIDER"
    SYSTEM = "SYSTEM"


VALID_CHANNELS = {c.value for c in OutreachChannelEnum}
VALID_EVENT_TYPES = {e.value for e in OutreachEventType}
VALID_OUTCOMES = {o.value for o in OutreachOutcomeEnum}
VALID_SOURCES = {s.value for s in OutreachSourceEnum}


@dataclass(frozen=True)
class OutreachEvent:
    """
    Immutable, canonical outreach event record.
    Cannot be modified after creation.
    """
    event_id: str
    lead_id: str
    channel: str
    event_type: str
    outcome: Optional[str] = None
    template_version: Optional[str] = ""
    message_angle: Optional[str] = ""
    attempt_number: int = 1
    operator_confirmed: bool = False
    occurred_at: str = ""
    source: str = "OPERATOR"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 1. Validate event_id
        if not self.event_id or not isinstance(self.event_id, str):
            object.__setattr__(self, "event_id", f"evt-{uuid.uuid4().hex[:10]}")

        # 2. Validate lead_id
        if not self.lead_id or not isinstance(self.lead_id, str) or not self.lead_id.strip():
            raise ValueError("lead_id must be a non-empty string.")

        # 3. Validate channel
        channel_upper = (self.channel or "").strip().upper()
        if channel_upper not in VALID_CHANNELS:
            raise ValueError(f"Invalid channel '{self.channel}'. Must be one of {sorted(VALID_CHANNELS)}.")
        object.__setattr__(self, "channel", channel_upper)

        # 4. Validate event_type
        type_upper = (self.event_type or "").strip().upper()
        if type_upper not in VALID_EVENT_TYPES:
            raise ValueError(f"Invalid event_type '{self.event_type}'. Must be one of {sorted(VALID_EVENT_TYPES)}.")
        object.__setattr__(self, "event_type", type_upper)

        # 5. Validate outcome if present
        if self.outcome:
            outcome_upper = self.outcome.strip().upper()
            if outcome_upper not in VALID_OUTCOMES:
                raise ValueError(f"Invalid outcome '{self.outcome}'. Must be one of {sorted(VALID_OUTCOMES)}.")
            object.__setattr__(self, "outcome", outcome_upper)

        # 6. Validate source
        source_upper = (self.source or "OPERATOR").strip().upper()
        if source_upper not in VALID_SOURCES:
            raise ValueError(f"Invalid source '{self.source}'. Must be one of {sorted(VALID_SOURCES)}.")
        object.__setattr__(self, "source", source_upper)

        # 7. Validate attempt_number
        if not isinstance(self.attempt_number, int) or self.attempt_number < 1:
            raise ValueError(f"attempt_number must be an integer >= 1, got {self.attempt_number}.")

        # 8. Validate occurred_at timestamp
        if not self.occurred_at or not isinstance(self.occurred_at, str) or not self.occurred_at.strip():
            raise ValueError("occurred_at timestamp is required.")

        # Freeze metadata to prevent mutation
        if isinstance(self.metadata, dict):
            # Ensure metadata is copied to avoid external mutation
            object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> Dict[str, Any]:
        """Serializes event to canonical JSON-friendly dictionary."""
        return {
            "event_id": self.event_id,
            "lead_id": self.lead_id,
            "channel": self.channel,
            "event_type": self.event_type,
            "outcome": self.outcome,
            "template_version": self.template_version,
            "message_angle": self.message_angle,
            "attempt_number": self.attempt_number,
            "operator_confirmed": self.operator_confirmed,
            "occurred_at": self.occurred_at,
            "source": self.source,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OutreachEvent":
        """Instantiates canonical event from dictionary with validation."""
        return cls(
            event_id=data.get("event_id", ""),
            lead_id=data.get("lead_id", ""),
            channel=data.get("channel", "PHONE"),
            event_type=data.get("event_type", "OUTREACH_ATTEMPTED"),
            outcome=data.get("outcome"),
            template_version=data.get("template_version", ""),
            message_angle=data.get("message_angle", ""),
            attempt_number=data.get("attempt_number", 1),
            operator_confirmed=data.get("operator_confirmed", False),
            occurred_at=data.get("occurred_at", ""),
            source=data.get("source", "OPERATOR"),
            metadata=data.get("metadata", {}),
        )
