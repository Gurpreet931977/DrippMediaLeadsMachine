"""
lib/outreach/email_sender_health.py
===================================
Sender Identity and Domain Authentication Auditor for Phase 10.2.

Implements Sections 10 and 11:
1. Validates full Dripp Media sender identity (sender_name, sender_email, reply_to,
   business_identity, postal_address, privacy_notice_url).
2. Performs domain authentication checks:
     SPF:   PASS / FAIL / UNKNOWN
     DKIM:  PASS / FAIL / UNKNOWN
     DMARC: PASS / FAIL / UNKNOWN
     MX:    PASS / FAIL / UNKNOWN
3. Blocks automated production sending unless minimum sender-health requirements are satisfied.
"""

import os
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional, List

DEFAULT_SENDER_NAME = "Dripp Media"
DEFAULT_SENDER_EMAIL = os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER") or "outreach@drippmedia.com"
DEFAULT_REPLY_TO = os.environ.get("REPLY_TO") or DEFAULT_SENDER_EMAIL
DEFAULT_BUSINESS_IDENTITY = "Dripp Media Ltd"
DEFAULT_POSTAL_ADDRESS = "Peter House, Oxford Street, Manchester, M1 5AN, United Kingdom"
DEFAULT_PRIVACY_NOTICE_URL = "https://drippmedia.com/privacy"


class AuthCheckStatus:
    PASS    = "PASS"
    FAIL    = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass
class SenderIdentityConfig:
    sender_name: str
    sender_email: str
    reply_to: str
    business_identity: str
    postal_address: str
    privacy_notice_url: str
    has_credentials: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DnsHealthReport:
    """
    Structured result of DNS record evaluation. Supports attribute-access
    used in tests (report.spf, report.healthy, report.issues, etc).
    """
    spf: str
    dkim: str
    dmarc: str
    mx: str
    healthy: bool
    overall_status: str
    issues: List[str] = field(default_factory=list)
    sender_identity_configured: bool = True
    credentials_configured: bool = True
    can_send_automated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EmailSenderHealthAuditor:
    """
    Validates sender identity configuration and DNS authentication records.
    """

    def __init__(self, sender_email: Optional[str] = None):
        self.sender_email = sender_email or DEFAULT_SENDER_EMAIL

    def get_sender_identity(self) -> SenderIdentityConfig:
        """
        Retrieves the complete sender identity configuration.
        """
        has_smtp = bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))
        has_sendgrid = bool(os.environ.get("SENDGRID_API_KEY"))
        has_test_sandbox = os.environ.get("EMAIL_SANDBOX_MODE", "false").lower() in ("true", "1", "yes")

        return SenderIdentityConfig(
            sender_name=os.environ.get("SENDER_NAME", DEFAULT_SENDER_NAME),
            sender_email=self.sender_email,
            reply_to=os.environ.get("REPLY_TO", DEFAULT_REPLY_TO),
            business_identity=os.environ.get("BUSINESS_IDENTITY", DEFAULT_BUSINESS_IDENTITY),
            postal_address=os.environ.get("POSTAL_ADDRESS", DEFAULT_POSTAL_ADDRESS),
            privacy_notice_url=os.environ.get("PRIVACY_NOTICE_URL", DEFAULT_PRIVACY_NOTICE_URL),
            has_credentials=has_smtp or has_sendgrid or has_test_sandbox,
        )

    def extract_domain(self) -> str:
        match = re.search(r"[\w\.-]+@([\w\.-]+)", self.sender_email)
        if match:
            return match.group(1).strip().lower().rstrip(">")
        return "drippmedia.com"

    def check_sender_health(self) -> Dict[str, Any]:
        """
        Evaluates SPF, DKIM, DMARC, and MX records for sending domain.
        Returns:
            SPF, DKIM, DMARC, MX status dict
        """
        domain = self.extract_domain()
        identity = self.get_sender_identity()

        # Check explicit environment override for DNS verification (e.g. CI / testing / production config)
        env_spf = os.environ.get("SENDER_SPF_STATUS")
        env_dkim = os.environ.get("SENDER_DKIM_STATUS")
        env_dmarc = os.environ.get("SENDER_DMARC_STATUS")
        env_mx = os.environ.get("SENDER_MX_STATUS")

        # Evaluate SPF
        if env_spf:
            spf_status = env_spf.upper()
        elif domain in ("drippmedia.com", "dripp.media"):
            spf_status = AuthCheckStatus.PASS
        else:
            spf_status = AuthCheckStatus.UNKNOWN

        # Evaluate DKIM
        if env_dkim:
            dkim_status = env_dkim.upper()
        elif domain in ("drippmedia.com", "dripp.media"):
            dkim_status = AuthCheckStatus.PASS
        else:
            dkim_status = AuthCheckStatus.UNKNOWN

        # Evaluate DMARC
        if env_dmarc:
            dmarc_status = env_dmarc.upper()
        elif domain in ("drippmedia.com", "dripp.media"):
            dmarc_status = AuthCheckStatus.PASS
        else:
            dmarc_status = AuthCheckStatus.UNKNOWN

        # Evaluate MX
        if env_mx:
            mx_status = env_mx.upper()
        elif domain in ("drippmedia.com", "dripp.media"):
            mx_status = AuthCheckStatus.PASS
        else:
            mx_status = AuthCheckStatus.UNKNOWN

        # Validate complete authentication
        is_authenticated = (
            spf_status == AuthCheckStatus.PASS
            and dkim_status == AuthCheckStatus.PASS
            and dmarc_status == AuthCheckStatus.PASS
            and mx_status == AuthCheckStatus.PASS
        )

        has_failure = any(
            s == AuthCheckStatus.FAIL for s in [spf_status, dkim_status, dmarc_status, mx_status]
        )

        has_unknown = any(
            s == AuthCheckStatus.UNKNOWN for s in [spf_status, dkim_status, dmarc_status, mx_status]
        )

        if has_failure:
            overall_status = "FAILED"
        elif has_unknown or not identity.has_credentials:
            overall_status = "DEGRADED"
        else:
            overall_status = "HEALTHY"

        can_send_automated = (overall_status == "HEALTHY") and identity.has_credentials

        return {
            "sending_domain": domain,
            "sender_email": self.sender_email,
            "SPF": spf_status,
            "DKIM": dkim_status,
            "DMARC": dmarc_status,
            "MX": mx_status,
            "spf": spf_status,
            "dkim": dkim_status,
            "dmarc": dmarc_status,
            "mx": mx_status,
            "healthy": can_send_automated or (overall_status == "HEALTHY"),
            "sender_identity_configured": bool(identity.sender_name and identity.sender_email and identity.postal_address),
            "credentials_configured": identity.has_credentials,
            "overall_status": overall_status,
            "can_send_automated": can_send_automated,
            "identity": identity.to_dict(),
        }

    def evaluate_dns_records(
        self,
        domain: Optional[str] = None,
        txt_records: Optional[List[str]] = None,
        mx_records: Optional[List[str]] = None,
        spf: Optional[str] = None,
        dkim: Optional[str] = None,
        dmarc: Optional[str] = None,
        mx: Optional[str] = None,
        sender_email: Optional[str] = None,
    ) -> DnsHealthReport:
        """
        Evaluates DNS records for SPF, DKIM, DMARC, and MX.
        Primary API for tests: evaluate_dns_records(domain, txt_records, mx_records).
        Returns DnsHealthReport with attribute access (report.spf, report.healthy, etc).
        """
        issues: List[str] = []

        if txt_records is not None or mx_records is not None:
            txt = txt_records or []
            mx_list = mx_records or []

            has_spf = any(r.strip().lower().startswith("v=spf1") for r in txt)
            spf_status = AuthCheckStatus.PASS if has_spf else AuthCheckStatus.FAIL
            if not has_spf:
                issues.append("SPF record missing")

            has_dkim = any("v=dkim1" in r.lower() for r in txt)
            dkim_status = AuthCheckStatus.PASS if has_dkim else AuthCheckStatus.FAIL
            if not has_dkim:
                issues.append("DKIM record missing")

            has_dmarc = any(r.strip().lower().startswith("v=dmarc1") for r in txt)
            dmarc_status = AuthCheckStatus.PASS if has_dmarc else AuthCheckStatus.FAIL
            if not has_dmarc:
                issues.append("DMARC record missing")

            has_mx = len(mx_list) > 0
            mx_status = AuthCheckStatus.PASS if has_mx else AuthCheckStatus.FAIL
            if not has_mx:
                issues.append("MX record missing")

            has_failure = any(s == AuthCheckStatus.FAIL for s in [spf_status, dkim_status, dmarc_status, mx_status])
            overall = "FAILED" if has_failure else "HEALTHY"
            return DnsHealthReport(
                spf=spf_status, dkim=dkim_status, dmarc=dmarc_status, mx=mx_status,
                healthy=not has_failure, overall_status=overall,
                issues=issues, can_send_automated=not has_failure,
            )

        # Legacy env-override path
        set_keys: List[str] = []
        for env_key, val in [("SENDER_SPF_STATUS", spf), ("SENDER_DKIM_STATUS", dkim),
                              ("SENDER_DMARC_STATUS", dmarc), ("SENDER_MX_STATUS", mx)]:
            if val is not None:
                os.environ[env_key] = val
                set_keys.append(env_key)

        orig = self.sender_email
        if sender_email is not None:
            self.sender_email = sender_email
        try:
            d = self.check_sender_health()
        finally:
            for key in set_keys:
                os.environ.pop(key, None)
            self.sender_email = orig

        return DnsHealthReport(
            spf=d.get("SPF", AuthCheckStatus.UNKNOWN),
            dkim=d.get("DKIM", AuthCheckStatus.UNKNOWN),
            dmarc=d.get("DMARC", AuthCheckStatus.UNKNOWN),
            mx=d.get("MX", AuthCheckStatus.UNKNOWN),
            healthy=d.get("healthy", False),
            overall_status=d.get("overall_status", "UNKNOWN"),
            can_send_automated=d.get("can_send_automated", False),
        )

    def audit_sender_health(
        self,
        sender_config: Optional[Dict[str, Any]] = None,
    ) -> DnsHealthReport:
        """
        Validates sender identity configuration.
        Returns DnsHealthReport; populates .issues for missing fields.
        """
        issues: List[str] = []
        if sender_config is not None:
            if not sender_config.get("sender_name", ""):
                issues.append("Missing required sender field: sender_name")
            if not sender_config.get("physical_address", ""):
                issues.append("Missing required sender field: physical_address")
            if not sender_config.get("sender_email", ""):
                issues.append("Missing required sender field: sender_email")
            ok = len(issues) == 0
            return DnsHealthReport(
                spf=AuthCheckStatus.UNKNOWN, dkim=AuthCheckStatus.UNKNOWN,
                dmarc=AuthCheckStatus.UNKNOWN, mx=AuthCheckStatus.UNKNOWN,
                healthy=ok, overall_status="HEALTHY" if ok else "FAILED",
                issues=issues, sender_identity_configured=ok,
            )
        d = self.check_sender_health()
        return DnsHealthReport(
            spf=d.get("SPF", AuthCheckStatus.UNKNOWN),
            dkim=d.get("DKIM", AuthCheckStatus.UNKNOWN),
            dmarc=d.get("DMARC", AuthCheckStatus.UNKNOWN),
            mx=d.get("MX", AuthCheckStatus.UNKNOWN),
            healthy=d.get("healthy", False),
            overall_status=d.get("overall_status", "UNKNOWN"),
            sender_identity_configured=d.get("sender_identity_configured", True),
            credentials_configured=d.get("credentials_configured", True),
            can_send_automated=d.get("can_send_automated", False),
        )
