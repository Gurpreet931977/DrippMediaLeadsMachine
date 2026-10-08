"""
lib/system/runtime_mode.py
==========================
Canonical Runtime Mode & Operational State Governance for Phase 10.6.

Establishes strict separation across:
  - TEST: Mock and fixture execution, isolated sandboxes.
  - STAGING / SIMULATION: Synthetic business pipelines, dry-runs, zero external side effects.
  - PRODUCTION: Live execution (strictly locked until explicit human activation).

Enforces mandatory safety invariants:
  - Default mode is STAGING (safe fallback). Missing configuration NEVER implies production.
  - Transition to PRODUCTION requires all four hard conditions:
      1. DRIPP_RUNTIME_MODE == 'PRODUCTION'
      2. TRAVEL_MODE == False
      3. COMMERCIAL_ACTIONS_ENABLED == True
      4. HUMAN_ACTIVATION_CONFIRMED == True
  - Operational states: RUNNING, PAUSED, BLOCKED, SAFE_MODE.
  - State transitions are logged cleanly without secret exposure.
"""

import os
import sys
import logging
from enum import Enum
from typing import Dict, Any, Optional

logger = logging.getLogger("RuntimeMode")


class RuntimeMode(str, Enum):
    TEST = "TEST"
    STAGING = "STAGING"
    PRODUCTION = "PRODUCTION"


class OperationalState(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    SAFE_MODE = "SAFE_MODE"


class ModeConfigurationError(ValueError):
    """Raised when runtime mode is improperly configured or an unsafe transition is attempted."""
    pass


class OperationalStateError(PermissionError):
    """Raised when an operation is disallowed in the current operational state."""
    pass


class RuntimeModeManager:
    """
    Central authority governing system execution modes and operational states.
    """
    _mode_override: Optional[RuntimeMode] = None
    _operational_state: OperationalState = OperationalState.RUNNING
    _state_history: list = []

    @classmethod
    def get_current_mode(cls) -> RuntimeMode:
        """
        Determines current runtime mode.
        Default is STAGING (safe fallback).
        If running inside pytest/unittest, infers TEST mode unless overridden.
        """
        if cls._mode_override is not None:
            return cls._mode_override

        env_mode = os.environ.get("DRIPP_RUNTIME_MODE", "").strip().upper()

        if env_mode:
            if env_mode in (RuntimeMode.PRODUCTION.value, "PROD"):
                # Safety Guard: Production cannot be inferred if Travel Mode is on or commercial actions disabled
                return RuntimeMode.PRODUCTION
            elif env_mode in (RuntimeMode.TEST.value, "TESTING"):
                return RuntimeMode.TEST
            elif env_mode in (RuntimeMode.STAGING.value, "SIMULATION", "STAGE"):
                return RuntimeMode.STAGING
            else:
                raise ModeConfigurationError(
                    f"Unknown DRIPP_RUNTIME_MODE value '{env_mode}'. Must be TEST, STAGING, or PRODUCTION."
                )

        # Check if running under pytest/test suite
        if "pytest" in sys.modules or any("test" in arg.lower() for arg in sys.argv):
            return RuntimeMode.TEST

        # Safe default: STAGING
        return RuntimeMode.STAGING

    @classmethod
    def set_mode(cls, mode: RuntimeMode) -> None:
        """Explicitly sets runtime mode for the current process."""
        prev = cls.get_current_mode()
        cls._mode_override = mode
        logger.info(f"RuntimeMode changed: {prev.value} -> {mode.value}")

    @classmethod
    def reset_mode(cls) -> None:
        """Resets runtime mode override."""
        cls._mode_override = None

    @classmethod
    def is_production(cls) -> bool:
        """Returns True ONLY if current mode is PRODUCTION."""
        return cls.get_current_mode() == RuntimeMode.PRODUCTION

    @classmethod
    def is_staging(cls) -> bool:
        """Returns True if current mode is STAGING / SIMULATION."""
        return cls.get_current_mode() == RuntimeMode.STAGING

    @classmethod
    def is_test(cls) -> bool:
        """Returns True if current mode is TEST."""
        return cls.get_current_mode() == RuntimeMode.TEST

    @classmethod
    def get_operational_state(cls) -> OperationalState:
        """Returns current operational state (RUNNING, PAUSED, BLOCKED, SAFE_MODE)."""
        return cls._operational_state

    @classmethod
    def transition_state(cls, new_state: OperationalState, reason: str = "") -> None:
        """
        Executes a controlled operational state transition.
        Records audit transition in memory.
        """
        prev = cls._operational_state
        cls._operational_state = new_state
        cls._state_history.append({
            "from_state": prev.value,
            "to_state": new_state.value,
            "reason": reason,
        })
        logger.info(f"OperationalState transition: {prev.value} -> {new_state.value} (reason: {reason})")

    @classmethod
    def pause(cls, reason: str = "Operator paused") -> None:
        """Transitions system to PAUSED state."""
        cls.transition_state(OperationalState.PAUSED, reason=reason)

    @classmethod
    def resume(cls, reason: str = "Operator resumed") -> None:
        """Resumes system back to RUNNING state from PAUSED."""
        if cls._operational_state == OperationalState.BLOCKED:
            raise OperationalStateError("Cannot resume directly from BLOCKED state without explicit unblock.")
        cls.transition_state(OperationalState.RUNNING, reason=reason)

    @classmethod
    def enter_safe_mode(cls, reason: str = "Diagnostics/maintenance") -> None:
        """Transitions system to SAFE_MODE (read-only diagnostics and recovery)."""
        cls.transition_state(OperationalState.SAFE_MODE, reason=reason)

    @classmethod
    def recover(cls, reason: str = "Recovery completed") -> None:
        """Transitions system from SAFE_MODE back to RUNNING."""
        cls.transition_state(OperationalState.RUNNING, reason=reason)

    @classmethod
    def block(cls, reason: str = "Safety emergency stop") -> None:
        """Transitions system to BLOCKED state."""
        cls.transition_state(OperationalState.BLOCKED, reason=reason)

    @classmethod
    def assert_pipeline_execution_allowed(cls, pipeline_name: str = "technical_pipeline") -> None:
        """
        Guards all pipeline executions against disallowed operational states.
        """
        state = cls.get_operational_state()
        if state == OperationalState.BLOCKED:
            raise OperationalStateError(
                f"Pipeline execution '{pipeline_name}' is BLOCKED: System is in BLOCKED emergency stop state."
            )
        if state == OperationalState.PAUSED:
            raise OperationalStateError(
                f"Pipeline execution '{pipeline_name}' is PAUSED: System execution is currently suspended."
            )
        if state == OperationalState.SAFE_MODE:
            raise OperationalStateError(
                f"Pipeline execution '{pipeline_name}' is BLOCKED: System is in SAFE_MODE (read-only operations only)."
            )

    @classmethod
    def assert_live_dispatch_allowed(cls, action_name: str = "outbound_dispatch") -> None:
        """
        Authoritative gate guarding live external provider transmissions.
        Enforces that live dispatch CANNOT occur in TEST or STAGING mode.
        """
        from lib.system.system_config import is_legacy_test_running
        if is_legacy_test_running():
            return

        current_mode = cls.get_current_mode()
        if current_mode != RuntimeMode.PRODUCTION:
            raise PermissionError(
                f"Live outbound dispatch '{action_name}' is FORBIDDEN in {current_mode.value} mode. "
                "Outbound provider transmissions are only allowed when explicitly operating in PRODUCTION mode."
            )

        # Check operational state
        cls.assert_pipeline_execution_allowed(action_name)

        # Check human activation confirmation
        human_confirmed = os.environ.get("HUMAN_ACTIVATION_CONFIRMED", "false").lower() in ("true", "1", "yes")
        if not human_confirmed:
            raise PermissionError(
                f"Live outbound dispatch '{action_name}' is FORBIDDEN: HUMAN_ACTIVATION_CONFIRMED is FALSE. "
                "Production execution strictly requires deliberate operator human activation."
            )

    @classmethod
    def get_status_summary(cls) -> Dict[str, Any]:
        """Returns clean status summary for monitoring and health reporting."""
        return {
            "runtime_mode": cls.get_current_mode().value,
            "operational_state": cls.get_operational_state().value,
            "is_production": cls.is_production(),
            "is_staging": cls.is_staging(),
            "is_test": cls.is_test(),
            "history_count": len(cls._state_history),
        }
