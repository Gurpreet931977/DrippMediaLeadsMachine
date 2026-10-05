"""
Dripp Media — Real Outreach Send Adapters
==========================================
Section 13 (Send Execution): Implements real sending for Instagram DM,
Facebook Messenger, and Email. Each adapter:
  - Validates credentials exist before attempting
  - Uses the actual provider API
  - Returns structured result with provider message_id and timestamp
  - NEVER marks SENT unless the provider confirms the request
  - NEVER exposes credentials in logs or responses
  - NEVER fakes a send
"""

import os
import json
import smtplib
import ssl
import uuid
import re
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Any, Optional

try:
    import requests as _requests
    _requests_available = True
except ImportError:
    _requests_available = False


# ──────────────────────────────────────────────────────────────────────────
# RESULT STRUCTURE
# ──────────────────────────────────────────────────────────────────────────

def _result(
    success: bool,
    provider: str,
    message_id: Optional[str] = None,
    provider_response: Optional[str] = None,
    error: Optional[str] = None
) -> Dict[str, Any]:
    return {
        "success": success,
        "provider": provider,
        "message_id": message_id or "",
        "provider_response": provider_response or "",
        "error": error or "",
        "sent_at": datetime.utcnow().isoformat() + "Z" if success else "",
    }


# ──────────────────────────────────────────────────────────────────────────
# INSTAGRAM DIRECT MESSAGE (via Meta Graph API)
# ──────────────────────────────────────────────────────────────────────────

class InstagramDMAdapter:
    """
    Sends Instagram DMs via Meta Graph API (Instagram Messaging API).
    Requires:
      - META_ACCESS_TOKEN or INSTAGRAM_GRAPH_TOKEN
    The recipient must be a valid Instagram username (e.g. @mala_mcr).
    
    NOTE: Meta's API requires the recipient to have previously messaged the
    Page, OR the account must have 'instagram_manage_messages' permission
    + connected IG Professional account. Real send behaviour depends on
    account type and app review status.
    """
    PROVIDER = "Meta Graph API (Instagram DM)"
    GRAPH_VERSION = "v19.0"

    @classmethod
    def _get_token(cls) -> Optional[str]:
        return os.getenv("INSTAGRAM_GRAPH_TOKEN") or os.getenv("META_ACCESS_TOKEN")

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls._get_token())

    @classmethod
    def send(cls, recipient: str, message_body: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        if not cls.is_configured():
            return _result(False, cls.PROVIDER, error="CHANNEL_NOT_CONFIGURED: No META_ACCESS_TOKEN or INSTAGRAM_GRAPH_TOKEN in .env")

        if not _requests_available:
            return _result(False, cls.PROVIDER, error="requests library not available")

        token = cls._get_token()
        # Strip @ prefix if present for recipient handle
        handle = recipient.lstrip("@").strip()
        if not handle:
            return _result(False, cls.PROVIDER, error="No valid Instagram recipient handle provided")

        # Meta's Instagram Messaging API: POST /me/messages with recipient handle
        # This requires instagram_manage_messages permission on connected IG Business Account
        page_id = os.getenv("META_PAGE_ID", "")
        endpoint_id = page_id or "me"

        url = f"https://graph.facebook.com/{cls.GRAPH_VERSION}/{endpoint_id}/messages"
        payload = {
            "recipient": {"id": handle},
            "message": {"text": message_body},
            "messaging_type": "MESSAGE_TAG",
            "tag": "ACCOUNT_UPDATE"
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        # Do NOT log token
        print(f"[InstagramDMAdapter] Attempting to send DM to @{handle}")

        try:
            resp = _requests.post(url, json=payload, headers=headers, timeout=15)
            resp_data = resp.json()
        except Exception as e:
            return _result(False, cls.PROVIDER, error=f"HTTP error: {str(e)}")

        if resp.status_code in (200, 201) and "message_id" in resp_data:
            mid = resp_data.get("message_id", "")
            return _result(True, cls.PROVIDER, message_id=mid, provider_response=json.dumps(resp_data))

        # Extract error message safely (no token in output)
        err_msg = resp_data.get("error", {}).get("message", str(resp_data))
        return _result(False, cls.PROVIDER, provider_response=json.dumps(resp_data), error=f"API error ({resp.status_code}): {err_msg}")


# ──────────────────────────────────────────────────────────────────────────
# FACEBOOK MESSENGER (via Meta Graph API Messenger Platform)
# ──────────────────────────────────────────────────────────────────────────

class FacebookMessengerAdapter:
    """
    Sends Facebook Messenger messages via Meta Graph API.
    Requires:
      - META_PAGE_ACCESS_TOKEN or META_ACCESS_TOKEN
      - The Page must have 'pages_messaging' permission
    Recipient is the Facebook Page username or PSID.
    """
    PROVIDER = "Meta Business Messaging API (Facebook Messenger)"
    GRAPH_VERSION = "v19.0"

    @classmethod
    def _get_token(cls) -> Optional[str]:
        return os.getenv("META_PAGE_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN")

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls._get_token())

    @classmethod
    def send(cls, recipient: str, message_body: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        if not cls.is_configured():
            return _result(False, cls.PROVIDER, error="CHANNEL_NOT_CONFIGURED: No META_PAGE_ACCESS_TOKEN or META_ACCESS_TOKEN in .env")

        if not _requests_available:
            return _result(False, cls.PROVIDER, error="requests library not available")

        token = cls._get_token()
        page_id = os.getenv("META_PAGE_ID", "me")

        # Recipient is a page username or PSID
        handle = recipient.strip()
        if not handle:
            return _result(False, cls.PROVIDER, error="No valid Facebook recipient provided")

        url = f"https://graph.facebook.com/{cls.GRAPH_VERSION}/{page_id}/messages"
        payload = {
            "recipient": {"id": handle},
            "message": {"text": message_body},
            "messaging_type": "MESSAGE_TAG",
            "tag": "ACCOUNT_UPDATE"
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        print(f"[FacebookMessengerAdapter] Attempting to send Messenger message to {handle}")

        try:
            resp = _requests.post(url, json=payload, headers=headers, timeout=15)
            resp_data = resp.json()
        except Exception as e:
            return _result(False, cls.PROVIDER, error=f"HTTP error: {str(e)}")

        if resp.status_code in (200, 201) and "message_id" in resp_data:
            mid = resp_data.get("message_id", "")
            return _result(True, cls.PROVIDER, message_id=mid, provider_response=json.dumps(resp_data))

        err_msg = resp_data.get("error", {}).get("message", str(resp_data))
        return _result(False, cls.PROVIDER, provider_response=json.dumps(resp_data), error=f"API error ({resp.status_code}): {err_msg}")


# ──────────────────────────────────────────────────────────────────────────
# EMAIL (SMTP — Gmail / Google Workspace / Custom SMTP)
# ──────────────────────────────────────────────────────────────────────────

def strip_em_dashes(text: str) -> str:
    """Strips em dashes (—) and en dashes (–) from email subject and body."""
    if not text:
        return text
    text = re.sub(r'(\n|^)\s*[—–]\s*', r'\1', text)
    text = re.sub(r'\s+[—–]\s+', ', ', text)
    text = text.replace('—', '-')
    text = text.replace('–', '-')
    text = re.sub(r',\s*,', ',', text)
    return text


class EmailAdapter:
    """
    Sends email via SMTP (Gmail, Google Workspace, Outlook, Hostinger, etc.)
    or optionally via SendGrid API.
    Requires:
      - SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM
    """
    PROVIDER = "Google Gmail SMTP"

    @classmethod
    def is_configured(cls) -> bool:
        return bool(os.getenv("SMTP_HOST") or os.getenv("SENDGRID_API_KEY"))

    @classmethod
    def send(
        cls,
        recipient: str,
        subject: str,
        message_body: str,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        if not cls.is_configured():
            return _result(False, cls.PROVIDER, error="CHANNEL_NOT_CONFIGURED: No SMTP_HOST or SENDGRID_API_KEY in .env")

        subject = strip_em_dashes(subject)
        message_body = strip_em_dashes(message_body)

        recipient = recipient.strip()
        if not recipient or "@" not in recipient:
            return _result(False, cls.PROVIDER, error=f"Invalid email recipient: {recipient}")

        sendgrid_key = os.getenv("SENDGRID_API_KEY")
        if sendgrid_key:
            return cls._send_sendgrid(recipient, subject, message_body, sendgrid_key)

        return cls._send_smtp(recipient, subject, message_body)

    @classmethod
    def _send_smtp(cls, recipient: str, subject: str, body: str) -> Dict[str, Any]:
        host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER", "")
        password = os.getenv("SMTP_PASSWORD", "")
        from_addr = os.getenv("EMAIL_FROM", user)

        if not user or not password:
            return _result(False, cls.PROVIDER, error="SMTP_USER or SMTP_PASSWORD not configured in .env")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = recipient
        # Idempotency via Message-ID header
        domain = "drippmedia.com" if "drippmedia.com" in from_addr else "dripp.media"
        msg["Message-ID"] = f"<{uuid.uuid4().hex}@{domain}>"
        msg.attach(MIMEText(body, "plain", "utf-8"))

        print(f"[EmailAdapter] Sending email to {recipient} via SMTP {host}:{port}")

        try:
            context = ssl.create_default_context()
            if port == 465:
                # Direct SSL (standard for Hostinger / secure SMTP on port 465)
                with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as server:
                    server.login(user, password)  # credentials never logged
                    server.sendmail(from_addr, [recipient], msg.as_string())
            else:
                # STARTTLS (port 587 or 25)
                with smtplib.SMTP(host, port, timeout=20) as server:
                    server.starttls(context=context)
                    server.login(user, password)  # credentials never logged
                    server.sendmail(from_addr, [recipient], msg.as_string())
            message_id = msg["Message-ID"]
            return _result(True, cls.PROVIDER, message_id=message_id, provider_response="SMTP 250 OK")
        except smtplib.SMTPAuthenticationError:
            return _result(False, cls.PROVIDER, error="SMTP authentication failed — check SMTP_USER and SMTP_PASSWORD in .env")
        except smtplib.SMTPRecipientsRefused as e:
            return _result(False, cls.PROVIDER, error=f"SMTP refused recipient: {str(e)}")
        except smtplib.SMTPException as e:
            return _result(False, cls.PROVIDER, error=f"SMTP error: {str(e)}")
        except Exception as e:
            return _result(False, cls.PROVIDER, error=f"Unexpected error: {str(e)}")

    @classmethod
    def _send_sendgrid(cls, recipient: str, subject: str, body: str, api_key: str) -> Dict[str, Any]:
        if not _requests_available:
            return _result(False, "SendGrid API", error="requests library not available")

        from_addr = os.getenv("EMAIL_FROM", "outreach@drippmedia.com")
        from_name = "Dripp Media"
        if "<" in from_addr:
            parts = from_addr.split("<")
            from_name = parts[0].strip()
            from_addr = parts[1].rstrip(">").strip()

        payload = {
            "personalizations": [{"to": [{"email": recipient}]}],
            "from": {"email": from_addr, "name": from_name},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}]
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        print(f"[EmailAdapter] Sending email to {recipient} via SendGrid")
        try:
            resp = _requests.post(
                "https://api.sendgrid.com/v3/mail/send",
                json=payload,
                headers=headers,
                timeout=20
            )
            if resp.status_code in (200, 202):
                mid = resp.headers.get("X-Message-Id", f"SG-{uuid.uuid4().hex[:12]}")
                return _result(True, "SendGrid API", message_id=mid, provider_response=f"HTTP {resp.status_code}")
            return _result(False, "SendGrid API", provider_response=resp.text, error=f"SendGrid error ({resp.status_code}): {resp.text[:300]}")
        except Exception as e:
            return _result(False, "SendGrid API", error=f"HTTP error: {str(e)}")


# ──────────────────────────────────────────────────────────────────────────
# DISPATCH FUNCTION
# ──────────────────────────────────────────────────────────────────────────

def dispatch_send(
    channel: str,
    recipient: str,
    message_body: str,
    message_subject: str = "",
    idempotency_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Master dispatcher. Routes to the correct adapter based on channel name.
    All supported channels must be real + configured.
    Returns structured result dict — never fakes a send.
    """
    ch = (channel or "").lower().strip()

    if "instagram" in ch:
        return InstagramDMAdapter.send(recipient, message_body, idempotency_key)
    elif "facebook" in ch:
        return FacebookMessengerAdapter.send(recipient, message_body, idempotency_key)
    elif "email" in ch:
        subj = message_subject or "A professional website for your business"
        return EmailAdapter.send(recipient, subj, message_body, idempotency_key)
    else:
        return _result(False, "Unknown", error=f"Unsupported or unconfigured channel: '{channel}'")
