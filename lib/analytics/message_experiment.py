"""
Phase 9.6: Message Experiment & Angle Testing Framework

Defines variant testing support (CONTROL, VARIANT_A, VARIANT_B) and message angles
(WEBSITE_FIRST, DIGITAL_PRESENCE, MOBILE_EXPERIENCE, ONLINE_BOOKING, BRAND_PRESENTATION).

Safety Invariants:
  - EXPERIMENTS_ENABLED = False (strict guardrail: disabled until sample size >= MIN_SAMPLE_SIZE_FOR_EXPERIMENTS)
  - MIN_SAMPLE_SIZE_FOR_EXPERIMENTS = 30
  - Truthful personalization only: no fabricated metrics, no unsupported claims, no invented pain points.
  - Message angles are analytical tags, NEVER qualification criteria.
"""

from enum import Enum
from typing import Dict, Any, List, Optional


class ExperimentVariant(str, Enum):
    CONTROL = "CONTROL"
    VARIANT_A = "VARIANT_A"
    VARIANT_B = "VARIANT_B"


class MessageAngle(str, Enum):
    WEBSITE_FIRST = "WEBSITE_FIRST"
    DIGITAL_PRESENCE = "DIGITAL_PRESENCE"
    MOBILE_EXPERIENCE = "MOBILE_EXPERIENCE"
    ONLINE_BOOKING = "ONLINE_BOOKING"
    BRAND_PRESENTATION = "BRAND_PRESENTATION"


VALID_EXPERIMENT_VARIANTS = {v.value for v in ExperimentVariant}
VALID_MESSAGE_ANGLES = {a.value for a in MessageAngle}


class MessageExperimentFramework:
    """
    Framework governing pitch variant testing and message angle analytics.
    Enforces that experiments remain disabled until statistical sample size is sufficient.
    """

    EXPERIMENTS_ENABLED = False
    MIN_SAMPLE_SIZE_FOR_EXPERIMENTS = 30
    DEFAULT_TEMPLATE_VERSION = "WEBSITE_DEV_V1"
    DEFAULT_MESSAGE_ANGLE = MessageAngle.WEBSITE_FIRST.value

    # Variant definitions
    VARIANTS: Dict[str, Dict[str, Any]] = {
        ExperimentVariant.CONTROL.value: {
            "template_id": "TPL-PHASE94-PHONE",
            "version": "WEBSITE_DEV_V1",
            "message_angle": MessageAngle.WEBSITE_FIRST.value,
            "description": "Direct, transparent website proposition based on confirmed review traction and location.",
            "status": "ACTIVE_DEFAULT",
        },
        ExperimentVariant.VARIANT_A.value: {
            "template_id": "TPL-PHASE96-DIGITAL",
            "version": "DIGITAL_PRESENCE_V1",
            "message_angle": MessageAngle.DIGITAL_PRESENCE.value,
            "description": "Emphasizes local search visibility and direct verified customer destination.",
            "status": "DISABLED_PENDING_SAMPLE",
        },
        ExperimentVariant.VARIANT_B.value: {
            "template_id": "TPL-PHASE96-MOBILE",
            "version": "MOBILE_EXPERIENCE_V1",
            "message_angle": MessageAngle.MOBILE_EXPERIENCE.value,
            "description": "Focuses on smartphone customer experience and mobile menu accessibility.",
            "status": "DISABLED_PENDING_SAMPLE",
        },
    }

    @classmethod
    def is_experiment_active(cls, current_sample_size: int = 0) -> bool:
        """
        Determines whether message experiments can be activated.
        Requires both explicit flag and sufficient sample size.
        """
        if not cls.EXPERIMENTS_ENABLED:
            return False
        return current_sample_size >= cls.MIN_SAMPLE_SIZE_FOR_EXPERIMENTS

    @classmethod
    def get_active_variant(cls, current_sample_size: int = 0) -> Dict[str, Any]:
        """
        Returns active template variant. Defaults to CONTROL until sample size is sufficient.
        """
        if not cls.is_experiment_active(current_sample_size):
            return cls.VARIANTS[ExperimentVariant.CONTROL.value]
        # Future variant routing logic can be unlocked here
        return cls.VARIANTS[ExperimentVariant.CONTROL.value]

    def __init__(self, experiments_enabled: bool = False):
        self.experiments_enabled = experiments_enabled

    def enable_experiments(self, total_confirmed_attempts: int):
        """Enforces minimum sample size before unlocking experiments."""
        if total_confirmed_attempts < self.MIN_SAMPLE_SIZE_FOR_EXPERIMENTS:
            raise ValueError(
                f"Insufficient sample size ({total_confirmed_attempts}). "
                f"Experiments require at least {self.MIN_SAMPLE_SIZE_FOR_EXPERIMENTS} confirmed attempts."
            )
        self.experiments_enabled = True

    def get_variant_for_lead(self, lead_id: str, template_version: str = DEFAULT_TEMPLATE_VERSION) -> str:
        """Deterministically selects variant when experiments are enabled, else CONTROL."""
        if not self.experiments_enabled:
            return ExperimentVariant.CONTROL.value
        import hashlib
        h = int(hashlib.md5(lead_id.encode('utf-8')).hexdigest(), 16)
        variants = [ExperimentVariant.CONTROL.value, ExperimentVariant.VARIANT_A.value, ExperimentVariant.VARIANT_B.value]
        return variants[h % len(variants)]

    @classmethod
    def validate_message_angle(cls, angle: str) -> str:
        """Validates that a message angle belongs to the canonical set."""
        upper = (angle or "").strip().upper()
        if upper not in VALID_MESSAGE_ANGLES:
            return cls.DEFAULT_MESSAGE_ANGLE
        return upper
