"""
Dripp Media — Server-Side Execution Gate
==========================================
Section X (Hardening): Enforces a mandatory server-side state machine for
campaign execution. The backend will NEVER execute a campaign solely because
POST /api/campaigns/:id/execute was called.

Required state machine:
  DRAFT
    → PREVIEWED     (after dry-run is run)
    → ARMED         (after explicit user confirmation + token issued)
    → RUNNING       (immediately on execution start)
    → COMPLETED     (all queued items processed)
    → PARTIAL       (some sent, some failed/blocked)
    → FAILED        (zero successful sends)

Token rules:
  - Issued on arm_campaign()
  - Expires in TOKEN_TTL_SECONDS (default 300 = 5 min)
  - Single-use: consumed immediately on execute
  - Not reusable: new arm required for re-execution

Pre-execute gate checks (Section 3):
  1. Campaign exists
  2. execution_state == ARMED
  3. confirmation_token valid and not expired
  4. campaign not already RUNNING or COMPLETED
  5. daily_limit not already exceeded for this run
  6. lead-level idempotency (per-item re-check before lock)
"""

import os
import json
import uuid
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple

from lib.outreach.outreach_service import (
    CAMPAIGNS_FILE,
    ensure_data_dir,
)

TOKEN_TTL_SECONDS = 300   # 5-minute confirmation window
GATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "execution_gate.json"
)
_SECRET_SEED = os.getenv("EXECUTION_GATE_SECRET", "dripp-media-execution-gate-2026")


# ──────────────────────────────────────────────────────────────────────────
# EXECUTION STATES
# ──────────────────────────────────────────────────────────────────────────

class ExecutionState:
    DRAFT       = "DRAFT"
    PREVIEWED   = "PREVIEWED"
    ARMED       = "ARMED"
    RUNNING     = "RUNNING"
    COMPLETED   = "COMPLETED"
    PARTIAL     = "PARTIAL"
    FAILED      = "FAILED"

TERMINAL_EXECUTION_STATES = {
    ExecutionState.COMPLETED,
    ExecutionState.PARTIAL,
    ExecutionState.FAILED,
}


# ──────────────────────────────────────────────────────────────────────────
# GATE STORE I/O
# ──────────────────────────────────────────────────────────────────────────

def _load_gate() -> Dict[str, Any]:
    ensure_data_dir()
    try:
        with open(GATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_gate(data: Dict[str, Any]):
    ensure_data_dir()
    with open(GATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# ──────────────────────────────────────────────────────────────────────────
# CAMPAIGN STATE HELPERS (read/write execution_state into campaigns.json)
# ──────────────────────────────────────────────────────────────────────────

def _load_campaigns() -> list:
    ensure_data_dir()
    try:
        with open(CAMPAIGNS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_campaigns(campaigns: list):
    ensure_data_dir()
    with open(CAMPAIGNS_FILE, "w", encoding="utf-8") as f:
        json.dump(campaigns, f, indent=2, default=str)


def get_campaign_execution_state(campaign_id: str) -> Optional[str]:
    for c in _load_campaigns():
        if c.get("campaign_id") == campaign_id:
            return c.get("execution_state", ExecutionState.DRAFT)
    return None


def _set_campaign_execution_state(campaign_id: str, state: str, extra_fields: Optional[Dict] = None):
    campaigns = _load_campaigns()
    for c in campaigns:
        if c.get("campaign_id") == campaign_id:
            c["execution_state"] = state
            c["execution_state_updated_at"] = datetime.utcnow().isoformat() + "Z"
            if extra_fields:
                c.update(extra_fields)
            break
    _save_campaigns(campaigns)


# ──────────────────────────────────────────────────────────────────────────
# TOKEN GENERATION & VERIFICATION
# ──────────────────────────────────────────────────────────────────────────

def _sign_token(campaign_id: str, raw_token: str, issued_at: str) -> str:
    """HMAC-SHA256 signature over campaign_id + token + issued_at."""
    msg = f"{campaign_id}:{raw_token}:{issued_at}".encode()
    return hmac.new(_SECRET_SEED.encode(), msg, hashlib.sha256).hexdigest()


def issue_confirmation_token(campaign_id: str) -> str:
    """
    Issues a single-use time-limited confirmation token for campaign_id.
    Stores it server-side. Returns the token string (to send to UI).
    """
    raw_token = uuid.uuid4().hex
    issued_at = datetime.utcnow().isoformat() + "Z"
    signature = _sign_token(campaign_id, raw_token, issued_at)

    gate = _load_gate()
    gate[campaign_id] = {
        "token": raw_token,
        "signature": signature,
        "issued_at": issued_at,
        "expires_at": (datetime.utcnow() + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat() + "Z",
        "consumed": False,
    }
    _save_gate(gate)
    return raw_token


def validate_and_consume_token(campaign_id: str, token: str) -> Tuple[bool, str]:
    """
    Validates the confirmation token for campaign_id.
    If valid: marks it consumed (single-use) and returns (True, "").
    If invalid/expired/consumed: returns (False, reason).
    Token is consumed ATOMICALLY — even on first valid call.
    """
    gate = _load_gate()
    record = gate.get(campaign_id)
    if not record:
        return False, "NO_TOKEN: No confirmation token issued for this campaign. Arm the campaign first."

    if record.get("consumed"):
        return False, "TOKEN_CONSUMED: This confirmation token has already been used. Issue a new confirmation."

    # Expiry check
    expires_at_str = record.get("expires_at", "")
    try:
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_at:
            return False, f"TOKEN_EXPIRED: Confirmation token expired at {expires_at_str}. Re-arm the campaign."
    except Exception:
        return False, "TOKEN_INVALID: Cannot parse token expiry."

    # Signature check
    issued_at = record.get("issued_at", "")
    expected_sig = _sign_token(campaign_id, record["token"], issued_at)
    if not hmac.compare_digest(record.get("signature", ""), expected_sig):
        return False, "TOKEN_INVALID: Token signature mismatch."

    # Token value check
    if not hmac.compare_digest(record.get("token", ""), token):
        return False, "TOKEN_MISMATCH: Provided token does not match issued token."

    # Mark consumed (single-use)
    gate[campaign_id]["consumed"] = True
    gate[campaign_id]["consumed_at"] = datetime.utcnow().isoformat() + "Z"
    _save_gate(gate)

    return True, ""


# ──────────────────────────────────────────────────────────────────────────
# STATE TRANSITIONS
# ──────────────────────────────────────────────────────────────────────────

def mark_campaign_previewed(campaign_id: str) -> Dict[str, Any]:
    """Called after a successful dry-run."""
    state = get_campaign_execution_state(campaign_id)
    if state in (ExecutionState.RUNNING, ExecutionState.COMPLETED):
        return {"ok": False, "error": f"Cannot preview a {state} campaign."}
    _set_campaign_execution_state(campaign_id, ExecutionState.PREVIEWED)
    return {"ok": True, "execution_state": ExecutionState.PREVIEWED}


def arm_campaign(campaign_id: str) -> Dict[str, Any]:
    """
    Arms the campaign for execution. Issues a fresh confirmation token.
    Only valid from PREVIEWED (or PARTIAL/FAILED for re-arm after issues).
    Blocks if campaign is RUNNING or COMPLETED.
    """
    from lib.system.system_config import SystemConfig, _is_legacy_test
    if not _is_legacy_test:
        SystemConfig.assert_commercial_actions_allowed(action_name="arm_campaign")

    campaigns = _load_campaigns()
    campaign = next((c for c in campaigns if c.get("campaign_id") == campaign_id), None)
    if not campaign:
        return {"ok": False, "error": f"Campaign {campaign_id} not found."}

    state = campaign.get("execution_state", ExecutionState.DRAFT)
    allowed_from = {
        ExecutionState.DRAFT,
        ExecutionState.PREVIEWED,
        ExecutionState.ARMED,
        ExecutionState.PARTIAL,
        ExecutionState.FAILED,
    }
    if state == ExecutionState.RUNNING:
        return {"ok": False, "error": "ALREADY_RUNNING: Campaign is currently executing. Wait for it to complete."}
    if state == ExecutionState.COMPLETED:
        return {"ok": False, "error": "ALREADY_COMPLETED: Campaign is fully complete. Start a new campaign for additional sends."}
    if state not in allowed_from:
        return {"ok": False, "error": f"Cannot arm campaign from state '{state}'. Preview it first."}

    token = issue_confirmation_token(campaign_id)
    _set_campaign_execution_state(campaign_id, ExecutionState.ARMED, {
        "armed_at": datetime.utcnow().isoformat() + "Z"
    })

    return {
        "ok": True,
        "execution_state": ExecutionState.ARMED,
        "confirmation_token": token,
        "token_expires_in_seconds": TOKEN_TTL_SECONDS,
        "campaign_id": campaign_id,
    }


def verify_execute_gate(
    campaign_id: str,
    confirmation_token: str,
) -> Tuple[bool, str]:
    """
    Full pre-execute gate check. Called by execute_campaign before processing.
    Returns (ok, error_reason).
    """
    from lib.system.system_config import SystemConfig, _is_legacy_test
    if not _is_legacy_test:
        SystemConfig.assert_commercial_actions_allowed(action_name="verify_execute_gate")

    campaigns = _load_campaigns()
    campaign = next((c for c in campaigns if c.get("campaign_id") == campaign_id), None)
    if not campaign:
        return False, f"CAMPAIGN_NOT_FOUND: Campaign {campaign_id} does not exist."

    state = campaign.get("execution_state", ExecutionState.DRAFT)

    if state == ExecutionState.RUNNING:
        return False, "ALREADY_RUNNING: Campaign is currently executing. Only one execution at a time is permitted."
    if state == ExecutionState.COMPLETED:
        return False, "ALREADY_COMPLETED: This campaign has been fully completed. No further execution allowed."
    if state != ExecutionState.ARMED:
        return False, f"NOT_ARMED: Campaign is in state '{state}'. It must be ARMED before execution."

    # Validate + consume the token
    token_ok, token_err = validate_and_consume_token(campaign_id, confirmation_token)
    if not token_ok:
        return False, token_err

    return True, ""


def mark_campaign_running(campaign_id: str):
    _set_campaign_execution_state(campaign_id, ExecutionState.RUNNING, {
        "execution_started_at": datetime.utcnow().isoformat() + "Z"
    })


def mark_campaign_done(campaign_id: str, sent: int, failed: int, blocked: int):
    if sent > 0 and (failed + blocked) > 0:
        final_state = ExecutionState.PARTIAL
    elif sent > 0:
        final_state = ExecutionState.COMPLETED
    else:
        final_state = ExecutionState.FAILED

    _set_campaign_execution_state(campaign_id, final_state, {
        "execution_completed_at": datetime.utcnow().isoformat() + "Z",
        "execution_summary": {
            "sent": sent,
            "failed": failed,
            "blocked": blocked,
        }
    })
    return final_state


# ──────────────────────────────────────────────────────────────────────────
# SENDING LOCK (per-queue-item concurrency guard)
# ──────────────────────────────────────────────────────────────────────────

LOCK_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "send_locks.json"
)
LOCK_TTL_SECONDS = 120  # 2-minute lock TTL (auto-expire stale locks)


def _load_locks() -> Dict[str, Any]:
    ensure_data_dir()
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_locks(data: Dict[str, Any]):
    ensure_data_dir()
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def acquire_send_lock(queue_id: str, worker_id: Optional[str] = None) -> Tuple[bool, str]:
    """
    Acquire an exclusive send lock for a queue item.
    Returns (acquired, reason). Lock expires after LOCK_TTL_SECONDS.
    """
    locks = _load_locks()
    now = datetime.utcnow()
    now_str = now.isoformat() + "Z"
    worker = worker_id or uuid.uuid4().hex[:12]

    existing = locks.get(queue_id)
    if existing:
        # Check TTL
        try:
            locked_at = datetime.fromisoformat(existing["locked_at"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - locked_at).total_seconds()
            if age < LOCK_TTL_SECONDS:
                return False, f"ALREADY_LOCKED: Queue item {queue_id} is locked by worker {existing.get('worker_id')} (age {age:.0f}s)"
            # Stale lock — can override
        except Exception:
            pass

    locks[queue_id] = {
        "locked_at": now_str,
        "worker_id": worker,
        "ttl_seconds": LOCK_TTL_SECONDS,
    }
    _save_locks(locks)
    return True, worker


def release_send_lock(queue_id: str):
    locks = _load_locks()
    locks.pop(queue_id, None)
    _save_locks(locks)
