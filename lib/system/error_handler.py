"""
lib/system/error_handler.py
===========================
Structured error records, classification, and bounded retry mechanisms.
Classifies errors into:
  - TRANSIENT
  - PERMANENT
  - DATA_QUALITY
  - QUOTA
  - CONFIGURATION
  - IDENTITY_CONFLICT
  - EXTERNAL_PROVIDER
"""

import time
import uuid
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Any, Optional, Callable, TypeVar, Tuple

logger = logging.getLogger("ErrorHandler")

T = TypeVar("T")

class ErrorClassification(str, Enum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    DATA_QUALITY = "DATA_QUALITY"
    QUOTA = "QUOTA"
    CONFIGURATION = "CONFIGURATION"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    EXTERNAL_PROVIDER = "EXTERNAL_PROVIDER"


class ErrorSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# Canonical Error Categories
ERROR_CATEGORIES = {
    "TRANSIENT": "Transient error that may succeed on immediate or short backoff retry",
    "PERMANENT": "Fatal logic or schema violation; retrying will fail",
    "DATA_QUALITY": "Inconsistent, corrupt, or invalid entity data",
    "QUOTA": "Resource budget or rate limit exhausted",
    "CONFIGURATION": "Missing, invalid, or disabled system configuration",
    "IDENTITY_CONFLICT": "Unresolvable duplicate entity or ambiguous identity",
    "EXTERNAL_PROVIDER": "Third-party API downtime, DNS failure, or 5xx response",
}


@dataclass
class TechnicalErrorRecord:
    error_id: str
    run_id: str
    component: str
    entity_id: Optional[str]
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    category: str  # TRANSIENT, PERMANENT, DATA_QUALITY, QUOTA, CONFIGURATION, IDENTITY_CONFLICT, EXTERNAL_PROVIDER
    exception_class: str
    message: str
    retryable: bool
    attempt_number: int
    timestamp: str

    @property
    def classification(self) -> str:
        return self.category

    @classification.setter
    def classification(self, val: str) -> None:
        self.category = val

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["classification"] = self.category
        return d


class TechnicalErrorHandler:
    """Object-oriented error handler and backoff calculator."""

    def record_error(
        self,
        component: str,
        exception: Exception,
        run_id: str = "GLOBAL",
        entity_id: Optional[str] = None,
        severity: str = "HIGH",
        attempt_number: int = 1,
    ) -> TechnicalErrorRecord:
        return create_error_record(
            component=component,
            exc=exception,
            run_id=run_id,
            entity_id=entity_id,
            severity=severity,
            attempt_number=attempt_number,
        )

    def calculate_backoff(self, attempt: int, base_seconds: float = 1.0) -> float:
        return base_seconds * (2 ** (attempt - 1))

    def should_retry(self, attempt: int, max_attempts: int = 3) -> bool:
        return attempt < max_attempts



def classify_exception(exc: Exception) -> Tuple[str, bool]:
    """
    Classifies an exception into a canonical category and determines if it is retryable.
    Returns: (category, retryable)
    """
    exc_name = exc.__class__.__name__
    msg = str(exc).lower()

    if "quota" in msg or "quotaexhausted" in exc_name.lower():
        return "QUOTA", False
    if "config" in msg or ("missing" in msg and "key" in msg):
        return "CONFIGURATION", False
    if "identity" in msg or "duplicate" in msg:
        return "IDENTITY_CONFLICT", False
    if any(k in msg for k in ["validation", "corrupt", "schema", "missing required", "postcode", "invalid format"]):
        return "DATA_QUALITY", False
    if isinstance(exc, (ConnectionError, TimeoutError)) or any(k in msg for k in ["timeout", "connection reset", "connection refused", "503", "502", "temporary", "network"]):
        return "TRANSIENT", True
    if any(k in msg for k in ["permission", "forbidden", "unauthorized", "invalid key"]):
        return "PERMANENT", False

    return "PERMANENT", False


def create_error_record(
    component: str,
    exc: Exception,
    run_id: str = "GLOBAL",
    entity_id: Optional[str] = None,
    severity: str = "HIGH",
    attempt_number: int = 1,
) -> TechnicalErrorRecord:
    """Creates a structured TechnicalErrorRecord from an exception."""
    category, retryable = classify_exception(exc)
    err_id = f"ERR-{uuid.uuid4().hex[:8].upper()}"
    return TechnicalErrorRecord(
        error_id=err_id,
        run_id=run_id,
        component=component,
        entity_id=entity_id,
        severity=severity,
        category=category,
        exception_class=exc.__class__.__name__,
        message=str(exc),
        retryable=retryable,
        attempt_number=attempt_number,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def bounded_retry(
    func: Callable[[], T],
    max_retries: int = 3,
    base_delay: float = 0.1,
    component: str = "Generic",
    run_id: str = "RUN",
) -> T:
    """
    Executes a callable with bounded exponential backoff for transient errors.
    Permanent and non-retryable errors fail immediately without wasteful retries.
    """
    attempt = 1
    delay = base_delay

    while True:
        try:
            return func()
        except Exception as e:
            category, retryable = classify_exception(e)
            record = create_error_record(
                component=component,
                exc=e,
                run_id=run_id,
                attempt_number=attempt,
            )

            if not retryable or attempt >= max_retries:
                logger.error(f"[{component}] Operation failed on attempt {attempt}/{max_retries} ({category}): {e}")
                raise e

            logger.warning(
                f"[{component}] Transient failure on attempt {attempt}/{max_retries}. Retrying in {delay:.2f}s... ({e})"
            )
            time.sleep(delay)
            delay *= 2
            attempt += 1
