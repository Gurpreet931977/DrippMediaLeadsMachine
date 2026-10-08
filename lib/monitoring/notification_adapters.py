"""
lib/monitoring/notification_adapters.py
=======================================
Provider-agnostic notification adapters for Phase 10.3 Technical Monitoring & Alerts.
Implements:
  1. Standardized alert payload formatting.
  2. Automatic secret scrubbing and token redaction.
  3. Webhook and Email notification adapters (disabled by default).
  4. Non-fatal failure resilience (notification errors never crash the pipeline).
"""

import os
import json
import logging
from typing import Dict, Any, List, Optional
from abc import ABC, abstractmethod

from lib.monitoring.incident_types import Incident
from lib.system.observability import sanitize_text, sanitize_payload

logger = logging.getLogger("NotificationAdapters")


def format_standard_alert_text(incident: Incident) -> str:
    """
    Renders the canonical Dripp Media standardized alert message per Section 18:
    
    DRIPP MEDIA — TECHNICAL ALERT
    
    Severity:
    ERROR
    
    Incident:
    GOOGLE_AUTH_FAILURE
    
    Component:
    Google Sheets
    
    First detected:
    timestamp
    
    Occurrences:
    3
    
    Summary:
    Google Sheets authentication failed.
    
    Impact:
    CRM synchronization unavailable.
    
    Recommended action:
    Check production Google credentials.
    """
    clean_summary = sanitize_text(incident.summary)
    clean_impact = sanitize_text(incident.impact)
    clean_action = sanitize_text(incident.recommended_action)
    clean_component = sanitize_text(incident.component)

    lines = [
        "DRIPP MEDIA — TECHNICAL ALERT",
        "",
        "Severity:",
        str(incident.severity),
        "",
        "Incident:",
        str(incident.incident_type),
        "",
        "Component:",
        clean_component,
        "",
        "First detected:",
        str(incident.first_detected_at),
        "",
        "Occurrences:",
        str(incident.occurrence_count),
        "",
        "Summary:",
        clean_summary,
        "",
        "Impact:",
        clean_impact,
        "",
        "Recommended action:",
        clean_action,
    ]
    return "\n".join(lines)


def format_standard_alert_payload(incident: Incident) -> Dict[str, Any]:
    """
    Returns standardized alert dictionary payload including rendered text and sanitized incident model.
    """
    text = format_standard_alert_text(incident)
    return {
        "text": text,
        "content": text,  # Compatible with Slack and Discord
        "incident": sanitize_payload(incident.to_dict()),
    }


class BaseNotificationAdapter(ABC):
    """Abstract base class for notification channels."""

    @abstractmethod
    def send_alert(self, incident: Incident) -> bool:
        """
        Sends an alert for the incident.
        Must return True if dispatched, False otherwise.
        Must NEVER raise an unhandled exception.
        """
        pass


class WebhookNotificationAdapter(BaseNotificationAdapter):
    """
    Dispatches alerts to a generic HTTP/HTTPS webhook (e.g. Discord, Slack, custom webhook).
    Disabled by default.
    """

    def __init__(self, webhook_url: Optional[str] = None, enabled: Optional[bool] = None):
        self.webhook_url = webhook_url or os.getenv("ALERT_WEBHOOK_URL", "")
        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = os.getenv("ALERT_WEBHOOK_ENABLED", "false").lower() in ("true", "1", "yes")

    def send_alert(self, incident: Incident) -> bool:
        if not self.enabled:
            return False
        if not self.webhook_url:
            logger.debug("Webhook notification skipped: No webhook_url configured.")
            return False

        message_text = format_standard_alert_payload(incident)
        sanitized_dict = sanitize_payload(incident.to_dict())

        payload = {
            "text": message_text,
            "content": message_text,  # Compatible with Slack & Discord
            "incident": sanitized_dict,
        }

        try:
            import requests
            resp = requests.post(
                self.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=5.0
            )
            success = 200 <= resp.status_code < 300
            if not success:
                logger.warning(f"Webhook dispatch failed with HTTP {resp.status_code}")
            return success
        except Exception as e:
            logger.warning(f"Webhook dispatch exception (non-fatal): {e}")
            return False


class EmailNotificationAdapter(BaseNotificationAdapter):
    """
    Dispatches technical alerts via SMTP email.
    Disabled by default. Read/observe only — does NOT send marketing or lead outreach.
    """

    def __init__(
        self,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
        recipient_emails: Optional[List[str]] = None,
        enabled: Optional[bool] = None,
    ):
        self.smtp_host = smtp_host or os.getenv("ALERT_SMTP_HOST", "")
        self.smtp_port = smtp_port or int(os.getenv("ALERT_SMTP_PORT", "587"))
        self.smtp_user = smtp_user or os.getenv("ALERT_SMTP_USER", "")
        self.smtp_password = smtp_password or os.getenv("ALERT_SMTP_PASSWORD", "")
        raw_recipients = os.getenv("ALERT_EMAIL_RECIPIENTS", "")
        self.recipient_emails = recipient_emails or [r.strip() for r in raw_recipients.split(",") if r.strip()]
        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = os.getenv("ALERT_EMAIL_ENABLED", "false").lower() in ("true", "1", "yes")

    def send_alert(self, incident: Incident) -> bool:
        if not self.enabled:
            return False
        if not self.smtp_host or not self.recipient_emails:
            logger.debug("Email alert skipped: Missing smtp_host or recipient_emails.")
            return False

        message_text = format_standard_alert_payload(incident)
        subject = f"[ALERT] [{incident.severity}] {incident.incident_type} on {incident.component}"

        try:
            import smtplib
            from email.mime.text import MIMEText

            msg = MIMEText(message_text, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = self.smtp_user or "alerts@dripp.media"
            msg["To"] = ", ".join(self.recipient_emails)

            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=5.0) as server:
                if self.smtp_password:
                    server.starttls()
                    server.login(self.smtp_user, self.smtp_password)
                server.sendmail(msg["From"], self.recipient_emails, msg.as_string())
            return True
        except Exception as e:
            logger.warning(f"Email alert dispatch exception (non-fatal): {e}")
            return False
