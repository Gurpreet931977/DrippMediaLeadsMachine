"""
lib/system/observability.py
===========================
Structured logging and telemetry for technical operations.
Guarantees:
  1. Standardized JSON logging payload format.
  2. Automatic scrubbing of secrets, API keys, passwords, and bearer tokens.
  3. No unnecessary private personal data leakage.
"""

import re
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

logger = logging.getLogger("Observability")

# Patterns to mask sensitive tokens and credentials
SENSITIVE_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE), r"\1[MASKED_TOKEN]"),
    (re.compile(r"(api[_\-]?key[\"'\s:=]+)[A-Za-z0-9_\-]{8,}", re.IGNORECASE), r"\1[MASKED_KEY]"),
    (re.compile(r"(password[\"'\s:=]+)[^\s,\"']+", re.IGNORECASE), r"\1[MASKED_PASSWORD]"),
    (re.compile(r"(private[_\-]?key[\"'\s:=]+)[^\s,\"']+", re.IGNORECASE), r"\1[MASKED_KEY]"),
]


def sanitize_text(text: str) -> str:
    """Masks secret patterns, tokens, and credentials in text strings."""
    if not isinstance(text, str):
        return str(text)
    sanitized = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def sanitize_payload(payload: Any) -> Any:
    """Recursively scrubs dictionary and list data structures."""
    if isinstance(payload, dict):
        clean = {}
        for k, v in payload.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in ("token", "secret", "password", "api_key", "apikey", "credential")):
                clean[k] = "[REDACTED_SECRET]"
            else:
                clean[k] = sanitize_payload(v)
        return clean
    elif isinstance(payload, list):
        return [sanitize_payload(item) for item in payload]
    elif isinstance(payload, str):
        return sanitize_text(payload)
    return payload


# Aliases
mask_sensitive_data = sanitize_payload
mask_string_secret = sanitize_text



class StructuredLogger:
    """
    Emits structured, sanitized JSON logs.
    """

    def __init__(self, component: str = "GLOBAL"):
        self.component = component

    def info(self, event: str, **kwargs) -> Dict[str, Any]:
        return self.log_event(event=event, component=self.component, level="INFO", **kwargs)

    def warning(self, event: str, **kwargs) -> Dict[str, Any]:
        return self.log_event(event=event, component=self.component, level="WARNING", **kwargs)

    def error(self, event: str, **kwargs) -> Dict[str, Any]:
        return self.log_event(event=event, component=self.component, level="ERROR", **kwargs)

    @classmethod
    def log_event(
        cls,
        event: str,
        component: str,
        level: str = "INFO",
        run_id: Optional[str] = None,
        job_id: Optional[str] = None,
        market_id: Optional[str] = None,
        lead_id: Optional[str] = None,
        duration_ms: Optional[float] = None,
        result: Optional[str] = "SUCCESS",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Creates a structured, sanitized log event dictionary and outputs to logger.
        """
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level.upper(),
            "event": event,
            "component": component,
            "run_id": run_id or "NONE",
            "job_id": job_id or "NONE",
            "market_id": market_id or "MANCHESTER_UK",
            "lead_id": lead_id or "NONE",
            "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
            "result": result,
            "metadata": sanitize_payload(metadata or {}),
        }

        # Log at appropriate level
        msg = json.dumps(log_entry, default=str)
        if level.upper() == "ERROR":
            logger.error(msg)
        elif level.upper() == "WARNING" or level.upper() == "WARN":
            logger.warning(msg)
        elif level.upper() == "DEBUG":
            logger.debug(msg)
        else:
            logger.info(msg)

        return log_entry
