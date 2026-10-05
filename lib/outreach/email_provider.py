"""
lib/outreach/email_provider.py
==============================
Authoritative Email Provider Adapter for Phase 10.2.

Implements Section 12 requirements:
1. Reuses existing SMTP and SendGrid send infrastructure.
2. Canonical Provider Result States:
     SUBMITTED, DELIVERED, BOUNCED, REJECTED, RATE_LIMITED, FAILED, UNKNOWN
3. Preserves genuine provider message IDs; sets provider_message_id = None if unconfirmed.
   Never fabricates fake message IDs.
4. Distinguishes retryable transient failures from permanent rejections.
5. Supports isolated sandbox mode for offline testing and verification.
"""

import os
import re
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional

from lib.outreach.send_adapters import EmailAdapter


class ProviderDeliveryStatus:
    SUBMITTED    = "SUBMITTED"
    DELIVERED    = "DELIVERED"
    BOUNCED      = "BOUNCED"
    REJECTED     = "REJECTED"
    RATE_LIMITED = "RATE_LIMITED"
    FAILED       = "FAILED"
    UNKNOWN      = "UNKNOWN"


@dataclass
class EmailProviderResult:
    status: str
    provider: str
    provider_message_id: Optional[str]
    response_text: str
    error: Optional[str]
    submitted_at: str
    is_retryable: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class EnhancedEmailProvider:
    """
    Robust provider adapter interfacing with SMTP, SendGrid, or test sandbox.
    """

    def __init__(self, sandbox_mode: Optional[bool] = None):
        self.sandbox_mode = (
            sandbox_mode
            if sandbox_mode is not None
            else os.environ.get("EMAIL_SANDBOX_MODE", "false").lower() in ("true", "1", "yes")
        )

    def send_email(
        self,
        recipient: str,
        subject: str,
        body: str,
        idempotency_key: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> EmailProviderResult:
        """
        Dispatches email through the authoritative provider and classifies the result.
        """
        clean_recipient = recipient.strip()

        # 1. Sandbox / Mock Provider Mode for Development and Testing
        if self.sandbox_mode:
            # Deterministic simulation based on recipient email pattern
            if "bounce" in clean_recipient.lower():
                return EmailProviderResult(
                    status=ProviderDeliveryStatus.BOUNCED,
                    provider="Sandbox Provider",
                    provider_message_id=None,
                    response_text="550 5.1.1 User unknown",
                    error="Recipient address rejected: mailbox not found",
                    submitted_at=_now_utc(),
                    is_retryable=False,
                )
            if "ratelimit" in clean_recipient.lower():
                return EmailProviderResult(
                    status=ProviderDeliveryStatus.RATE_LIMITED,
                    provider="SANDBOX",
                    provider_message_id=None,
                    response_text="421 4.7.0 Rate limit exceeded",
                    error="Server busy, rate limit hit",
                    submitted_at=_now_utc(),
                    is_retryable=True,
                )
            if "unknown" in clean_recipient.lower():
                return EmailProviderResult(
                    status=ProviderDeliveryStatus.UNKNOWN,
                    provider="SANDBOX",
                    provider_message_id=None,
                    response_text="Connection reset during handshake",
                    error="Network timeout or ambiguous delivery state",
                    submitted_at=_now_utc(),
                    is_retryable=True,
                )
            if "fail" in clean_recipient.lower():
                return EmailProviderResult(
                    status=ProviderDeliveryStatus.FAILED,
                    provider="SANDBOX",
                    provider_message_id=None,
                    response_text="500 Internal provider failure",
                    error="Provider rejected payload",
                    submitted_at=_now_utc(),
                    is_retryable=False,
                )

            # Genuine sandbox submission
            sandbox_mid = f"sandbox-{idempotency_key or 'msg'}-{int(datetime.now(timezone.utc).timestamp())}"
            return EmailProviderResult(
                status=ProviderDeliveryStatus.SUBMITTED,
                provider="SANDBOX",
                provider_message_id=sandbox_mid,
                response_text="250 2.0.0 OK queued for delivery",
                error=None,
                submitted_at=_now_utc(),
                is_retryable=False,
            )

        # 2. Real Production Dispatch via EmailAdapter
        try:
            raw_res = EmailAdapter.send(
                recipient=clean_recipient,
                subject=subject,
                message_body=body,
                idempotency_key=idempotency_key,
            )
        except Exception as e:
            return EmailProviderResult(
                status=ProviderDeliveryStatus.UNKNOWN,
                provider="Unknown",
                provider_message_id=None,
                response_text="",
                error=f"Provider invocation exception: {str(e)}",
                submitted_at=_now_utc(),
                is_retryable=True,
            )

        success = raw_res.get("success", False)
        provider_name = raw_res.get("provider", "SMTP")
        raw_msg_id = raw_res.get("message_id") or None
        provider_resp = str(raw_res.get("provider_response", "") or "")
        err_msg = raw_res.get("error") or None

        if success and raw_msg_id:
            return EmailProviderResult(
                status=ProviderDeliveryStatus.SUBMITTED,
                provider=provider_name,
                provider_message_id=raw_msg_id,
                response_text=provider_resp,
                error=None,
                submitted_at=_now_utc(),
                is_retryable=False,
            )

        # Classify Failure Mode
        clean_err = (err_msg or "").lower()
        clean_resp = provider_resp.lower()

        # Hard bounce patterns
        if any(c in clean_err or c in clean_resp for c in ["550", "551", "552", "553", "554", "user unknown", "mailbox not found", "recipient rejected", "does not exist"]):
            return EmailProviderResult(
                status=ProviderDeliveryStatus.BOUNCED,
                provider=provider_name,
                provider_message_id=None,
                response_text=provider_resp,
                error=err_msg,
                submitted_at=_now_utc(),
                is_retryable=False,
            )

        # Rate limit patterns
        if any(c in clean_err or c in clean_resp for c in ["421", "429", "rate limit", "too many requests", "throttled"]):
            return EmailProviderResult(
                status=ProviderDeliveryStatus.RATE_LIMITED,
                provider=provider_name,
                provider_message_id=None,
                response_text=provider_resp,
                error=err_msg,
                submitted_at=_now_utc(),
                is_retryable=True,
            )

        # Transient network / timeout
        if any(c in clean_err for c in ["timeout", "connection refused", "reset by peer", "temporary"]):
            return EmailProviderResult(
                status=ProviderDeliveryStatus.UNKNOWN,
                provider=provider_name,
                provider_message_id=None,
                response_text=provider_resp,
                error=err_msg,
                submitted_at=_now_utc(),
                is_retryable=True,
            )

        # Authentication or configuration failure
        if "authentication" in clean_err or "channel_not_configured" in clean_err:
            return EmailProviderResult(
                status=ProviderDeliveryStatus.REJECTED,
                provider=provider_name,
                provider_message_id=None,
                response_text=provider_resp,
                error=err_msg,
                submitted_at=_now_utc(),
                is_retryable=False,
            )

        # General failure
        return EmailProviderResult(
            status=ProviderDeliveryStatus.FAILED,
            provider=provider_name,
            provider_message_id=None,
            response_text=provider_resp,
            error=err_msg,
            submitted_at=_now_utc(),
            is_retryable=False,
        )

    def classify_smtp_error(self, exception: Exception) -> EmailProviderResult:
        """
        Classifies an SMTP exception into a canonical EmailProviderResult.
        Used for unit-testing bounce and error classification logic without
        a real send attempt. Does NOT perform any network I/O.
        """
        err_text = str(exception).lower()

        # 1. Permanent rejection / policy rejection (checked before generic bounce)
        if any(c in err_text for c in ["5.1.1", "recipient address rejected", "501", "502", "503", "authentication", "channel_not_configured"]):
            return EmailProviderResult(
                status=ProviderDeliveryStatus.REJECTED,
                provider="SMTP",
                provider_message_id=None,
                response_text=str(exception),
                error=str(exception),
                submitted_at=_now_utc(),
                is_retryable=False,
            )

        # 2. Rate limit / throttle
        if any(c in err_text for c in ["rate limit", "rate limited", "too many requests", "throttled", "429"]):
            return EmailProviderResult(
                status=ProviderDeliveryStatus.RATE_LIMITED,
                provider="SMTP",
                provider_message_id=None,
                response_text=str(exception),
                error=str(exception),
                submitted_at=_now_utc(),
                is_retryable=True,
            )

        # 3. Transient failure (e.g. 421 Temporary system failure)
        if any(c in err_text for c in ["421", "temporary system failure"]):
            return EmailProviderResult(
                status=ProviderDeliveryStatus.FAILED,
                provider="SMTP",
                provider_message_id=None,
                response_text=str(exception),
                error=str(exception),
                submitted_at=_now_utc(),
                is_retryable=True,
            )

        # 4. Hard bounce
        if any(c in err_text for c in ["550", "551", "552", "553", "554", "user unknown",
                                        "mailbox not found", "does not exist", "delivery error", "no such user"]):
            return EmailProviderResult(
                status=ProviderDeliveryStatus.BOUNCED,
                provider="SMTP",
                provider_message_id=None,
                response_text=str(exception),
                error=str(exception),
                submitted_at=_now_utc(),
                is_retryable=False,
            )

        # 5. Transient / temporary network errors
        if any(c in err_text for c in ["timeout", "connection refused", "reset by peer",
                                        "temporary", "451", "452", "4.7.1",
                                        "service unavailable", "try again"]):
            return EmailProviderResult(
                status=ProviderDeliveryStatus.UNKNOWN,
                provider="SMTP",
                provider_message_id=None,
                response_text=str(exception),
                error=str(exception),
                submitted_at=_now_utc(),
                is_retryable=True,
            )

        # 6. Unknown / ambiguous — do not immediately retry
        return EmailProviderResult(
            status=ProviderDeliveryStatus.UNKNOWN,
            provider="SMTP",
            provider_message_id=None,
            response_text=str(exception),
            error=str(exception),
            submitted_at=_now_utc(),
            is_retryable=False,
        )
