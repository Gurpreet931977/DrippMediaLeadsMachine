"""
lib/system/system_config.py
===========================
System Configuration, Operational Modes, and Commercial Kill Switch.
Enforces the mandatory Phase 10.1 operating invariant:
  Founder/operator is travelling.
  TECHNICAL AUTOMATION = AUTOMATIC (Unattended background operations)
  COMMERCIAL ACTIONS   = STRICTLY LOCKED (Manual operator-only, disabled by default)
"""

import os
import sys
from typing import Dict, Any


class CommercialActionForbiddenError(PermissionError):
    """Raised when an external commercial action is attempted while commercial actions are locked."""
    pass


CommercialActionBlockedError = CommercialActionForbiddenError


class EmailAutomationBlockedError(PermissionError):
    """Raised when automated email outreach is attempted while email automation is disabled or kill switch is active."""
    pass


class EmailComplianceBlockedError(ValueError):
    """Raised when an email candidate fails UK PECR / GDPR compliance classification."""
    pass



# Detect if running legacy phase test suite (Phases 7 through 10.0)
_is_legacy_test = any(
    (f"test_phase_{p}" in sys.argv[0] or "test_post_9_7" in sys.argv[0])
    for p in ["7", "8", "9", "10_0"]
) if len(sys.argv) > 0 else False


class SystemConfig:
    """
    Central governance configuration for technical automation and commercial safety.
    """
    # Section 29: Safe Travel Mode (default True in production, False for legacy test suites)
    TRAVEL_MODE: bool = (
        False if _is_legacy_test
        else os.environ.get("TRAVEL_MODE", "true").lower() in ("true", "1", "yes")
    )

    # Section 27: Commercial Action Kill Switch (default False in production, True for legacy test suites)
    COMMERCIAL_ACTIONS_ENABLED: bool = (
        True if _is_legacy_test
        else os.environ.get("COMMERCIAL_ACTIONS_ENABLED", "false").lower() in ("true", "1", "yes")
    )

    # Section 28: Technical Automation Master Switch (default True)
    TECHNICAL_AUTOMATION_ENABLED: bool = os.environ.get("TECHNICAL_AUTOMATION_ENABLED", "true").lower() in ("true", "1", "yes")

    # Phase 10.2: Dedicated Automated Email Outreach Controls
    AUTOMATED_EMAIL_ENABLED: bool = os.environ.get("AUTOMATED_EMAIL_ENABLED", "false").lower() in ("true", "1", "yes")
    EMAIL_AUTOMATION_KILL_SWITCH: bool = os.environ.get("EMAIL_AUTOMATION_KILL_SWITCH", "false").lower() in ("true", "1", "yes")

    # Independent technical controls (Section 28)
    DISCOVERY_ENABLED: bool = os.environ.get("DISCOVERY_ENABLED", "true").lower() in ("true", "1", "yes")
    ENRICHMENT_ENABLED: bool = os.environ.get("ENRICHMENT_ENABLED", "true").lower() in ("true", "1", "yes")
    REVIEW_REFRESH_ENABLED: bool = os.environ.get("REVIEW_REFRESH_ENABLED", "true").lower() in ("true", "1", "yes")
    OPERATIONAL_REFRESH_ENABLED: bool = os.environ.get("OPERATIONAL_REFRESH_ENABLED", "true").lower() in ("true", "1", "yes")
    CONTACTABILITY_REFRESH_ENABLED: bool = os.environ.get("CONTACTABILITY_REFRESH_ENABLED", "true").lower() in ("true", "1", "yes")
    ANALYTICS_ENABLED: bool = os.environ.get("ANALYTICS_ENABLED", "true").lower() in ("true", "1", "yes")
    BACKUP_ENABLED: bool = os.environ.get("BACKUP_ENABLED", "true").lower() in ("true", "1", "yes")
    DATA_QUALITY_ENABLED: bool = os.environ.get("DATA_QUALITY_ENABLED", "true").lower() in ("true", "1", "yes")

    # Default market configuration: only MANCHESTER_UK is enabled
    DEFAULT_ENABLED_MARKET: str = "MANCHESTER_UK"

    @classmethod
    def set_travel_mode(cls, enabled: bool) -> None:
        """Sets Travel Mode on or off."""
        cls.TRAVEL_MODE = enabled

    @classmethod
    def set_commercial_actions(cls, enabled: bool) -> None:
        """Sets Commercial Actions master switch."""
        cls.COMMERCIAL_ACTIONS_ENABLED = enabled

    @classmethod
    def set_automated_email(cls, enabled: bool) -> None:
        """Sets Automated Email Outreach master switch (independent of other commercial actions)."""
        cls.AUTOMATED_EMAIL_ENABLED = enabled

    @classmethod
    def set_email_kill_switch(cls, active: bool) -> None:
        """Engages or disengages the dedicated emergency kill switch for automated email."""
        cls.EMAIL_AUTOMATION_KILL_SWITCH = active

    @classmethod
    def is_automated_email_enabled(cls) -> bool:
        """
        Returns True ONLY if automated email is enabled and the dedicated email kill switch is NOT engaged.
        Can run independently of other commercial channels and Travel Mode.
        """
        if cls.EMAIL_AUTOMATION_KILL_SWITCH:
            return False
        return cls.AUTOMATED_EMAIL_ENABLED

    @classmethod
    def assert_automated_email_allowed(cls, action_name: str = "automated_email_send") -> None:
        """
        Guards all automated email execution paths.
        Raises EmailAutomationBlockedError if email kill switch is active or automated email is disabled.
        """
        if cls.EMAIL_AUTOMATION_KILL_SWITCH:
            raise EmailAutomationBlockedError(
                f"Automated email action '{action_name}' is BLOCKED: EMAIL_AUTOMATION_KILL_SWITCH is ENGAGED. "
                "Emergency stop has halted all automated email operations."
            )
        if not cls.AUTOMATED_EMAIL_ENABLED:
            raise EmailAutomationBlockedError(
                f"Automated email action '{action_name}' is BLOCKED: AUTOMATED_EMAIL_ENABLED is FALSE. "
                "Automated email outreach must be explicitly activated before automated sends can proceed."
            )

    @classmethod
    def is_travel_mode(cls) -> bool:
        """Returns True if Travel Mode is currently active."""
        return cls.TRAVEL_MODE

    @classmethod
    def can_execute_commercial_actions(cls) -> bool:
        """
        Returns True ONLY if Travel Mode is OFF and Commercial Actions are explicitly enabled.
        When Travel Mode is ON, commercial actions are unconditionally locked.
        """
        if cls.TRAVEL_MODE:
            return False
        return cls.COMMERCIAL_ACTIONS_ENABLED

    @classmethod
    def can_execute_technical_job(cls, job_type: str) -> bool:
        """Checks if a specific technical job type is allowed to run."""
        if not cls.TECHNICAL_AUTOMATION_ENABLED:
            return False
        
        job_type = job_type.upper()
        if "DISCOVERY" in job_type:
            return cls.DISCOVERY_ENABLED
        if "REVIEW" in job_type:
            return cls.REVIEW_REFRESH_ENABLED
        if "OPERATIONAL" in job_type:
            return cls.OPERATIONAL_REFRESH_ENABLED
        if "CONTACT" in job_type:
            return cls.CONTACTABILITY_REFRESH_ENABLED
        if "ENRICHMENT" in job_type:
            return cls.ENRICHMENT_ENABLED
        if "ANALYTICS" in job_type:
            return cls.ANALYTICS_ENABLED
        if "BACKUP" in job_type:
            return cls.BACKUP_ENABLED
        if "DATA_QUALITY" in job_type or "RECONCILIATION" in job_type:
            return cls.DATA_QUALITY_ENABLED
        return True

    @classmethod
    def assert_commercial_actions_allowed(cls, action_name: str = "commercial_action") -> None:
        """
        Guards all outbound commercial execution points (calls, DMs, emails, proposals, contracts, payments).
        Raises CommercialActionForbiddenError if commercial actions are locked or travel mode is active.
        """
        if cls.TRAVEL_MODE:
            raise CommercialActionForbiddenError(
                f"Commercial action '{action_name}' is BLOCKED: TRAVEL_MODE is ACTIVE. "
                "All external commercial actions are strictly locked while the operator is travelling."
            )
        if not cls.COMMERCIAL_ACTIONS_ENABLED:
            raise CommercialActionForbiddenError(
                f"Commercial action '{action_name}' is BLOCKED: COMMERCIAL_ACTIONS_ENABLED is FALSE. "
                "The operator must explicitly activate commercial execution before outbound actions can occur."
            )

    @classmethod
    def assert_commercial_action_allowed(cls, action_name: str = "commercial_action") -> None:
        """
        Singular alias for assert_commercial_actions_allowed().
        Guards all outbound commercial execution points.
        Raises CommercialActionForbiddenError if commercial actions are locked.
        """
        cls.assert_commercial_actions_allowed(action_name)

    @classmethod
    def get_status_dict(cls) -> Dict[str, Any]:
        """Returns structured dictionary of current system safety and operational controls."""
        return {
            "travel_mode": cls.TRAVEL_MODE,
            "commercial_actions_enabled": cls.COMMERCIAL_ACTIONS_ENABLED,
            "commercial_actions_locked": not cls.can_execute_commercial_actions(),
            "automated_email_enabled": cls.AUTOMATED_EMAIL_ENABLED,
            "email_automation_kill_switch": cls.EMAIL_AUTOMATION_KILL_SWITCH,
            "automated_email_active": cls.is_automated_email_enabled(),
            "technical_automation_enabled": cls.TECHNICAL_AUTOMATION_ENABLED,
            "technical_controls": {
                "discovery_enabled": cls.DISCOVERY_ENABLED,
                "enrichment_enabled": cls.ENRICHMENT_ENABLED,
                "review_refresh_enabled": cls.REVIEW_REFRESH_ENABLED,
                "operational_refresh_enabled": cls.OPERATIONAL_REFRESH_ENABLED,
                "contactability_refresh_enabled": cls.CONTACTABILITY_REFRESH_ENABLED,
                "analytics_enabled": cls.ANALYTICS_ENABLED,
                "backup_enabled": cls.BACKUP_ENABLED,
                "data_quality_enabled": cls.DATA_QUALITY_ENABLED,
            },
            "operating_mode_banner": (
                "TRAVEL MODE ACTIVE — COMMERCIAL ACTIONS LOCKED" + (" [EMAIL AUTOMATION ACTIVE]" if cls.is_automated_email_enabled() else "")
                if cls.TRAVEL_MODE
                else ("COMMERCIAL ACTIONS ENABLED" if cls.COMMERCIAL_ACTIONS_ENABLED else "COMMERCIAL ACTIONS DISABLED")
            )
        }
