"""
Channel-Aware Outreach Routing Layer
=====================================
Sits between OUTREACH_READY and OUTREACH_SEND.

Key Principle: A verified social profile URL != a send integration.
  ownership_status   -> Was a verified business social profile found?
  recipient_status   -> Is there a valid, extractable recipient for the channel?
  integration_status -> Does the project have real authorized API credentials?
  sendable           -> ALL THREE must be TRUE/VALID/CONFIGURED
"""

import os
import re
from enum import Enum
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Tuple


class ChannelType(str, Enum):
    INSTAGRAM = "Instagram Direct Message"
    FACEBOOK  = "Facebook Messenger"
    EMAIL     = "Email"
    WHATSAPP  = "WhatsApp"

    @classmethod
    def from_string(cls, value: str) -> Optional["ChannelType"]:
        v = (value or "").strip().lower()
        for ch in cls:
            if ch.value.lower() == v or ch.name.lower() == v:
                return ch
        if "instagram" in v:
            return cls.INSTAGRAM
        if "facebook" in v or "messenger" in v:
            return cls.FACEBOOK
        if "email" in v:
            return cls.EMAIL
        if "whatsapp" in v:
            return cls.WHATSAPP
        return None

    @classmethod
    def all_channels(cls) -> List["ChannelType"]:
        return list(cls)


AUTO_SELECT_PRIORITY = [
    ChannelType.EMAIL,
    ChannelType.INSTAGRAM,
    ChannelType.FACEBOOK,
    ChannelType.WHATSAPP,
]


class OwnershipStatus:
    VERIFIED   = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    UNKNOWN    = "UNKNOWN"


class RecipientStatus:
    VALID     = "VALID"
    INVALID   = "INVALID"
    NOT_FOUND = "NOT_FOUND"


class IntegrationStatus:
    CONFIGURED     = "CONFIGURED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass
class ChannelEligibility:
    channel: str
    ownership_status: str
    recipient: str
    recipient_status: str
    integration_status: str
    sendable: bool
    block_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ChannelRoutingResult:
    selected_channel: str
    channel_selection_reason: str
    sendable: bool
    block_reason: str
    channel_availability: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_channel": self.selected_channel,
            "channel_selection_reason": self.channel_selection_reason,
            "sendable": self.sendable,
            "block_reason": self.block_reason,
            "channel_availability": self.channel_availability,
        }


class IntegrationChecker:
    @staticmethod
    def check(channel: ChannelType) -> Tuple[str, str]:
        if channel == ChannelType.INSTAGRAM:
            has_token = bool(os.getenv("INSTAGRAM_GRAPH_TOKEN") or os.getenv("META_ACCESS_TOKEN"))
            if has_token:
                return IntegrationStatus.CONFIGURED, "Meta Graph API token present"
            return IntegrationStatus.NOT_CONFIGURED, "CHANNEL_NOT_CONFIGURED: No INSTAGRAM_GRAPH_TOKEN or META_ACCESS_TOKEN in .env"
        elif channel == ChannelType.FACEBOOK:
            has_token = bool(os.getenv("META_PAGE_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN"))
            if has_token:
                return IntegrationStatus.CONFIGURED, "Meta Page Access token present"
            return IntegrationStatus.NOT_CONFIGURED, "CHANNEL_NOT_CONFIGURED: No META_PAGE_ACCESS_TOKEN or META_ACCESS_TOKEN in .env"
        elif channel == ChannelType.EMAIL:
            has_email = bool(os.getenv("SMTP_HOST") or os.getenv("SENDGRID_API_KEY"))
            if has_email:
                return IntegrationStatus.CONFIGURED, "SMTP/SendGrid credentials present"
            return IntegrationStatus.NOT_CONFIGURED, "CHANNEL_NOT_CONFIGURED: No SMTP_HOST or SENDGRID_API_KEY in .env"
        elif channel == ChannelType.WHATSAPP:
            token = os.getenv("WHATSAPP_BUSINESS_TOKEN")
            phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
            if token and phone_id:
                return IntegrationStatus.CONFIGURED, f"WhatsApp Cloud API configured (Phone ID: {phone_id})"
            elif token:
                return IntegrationStatus.NOT_CONFIGURED, "CHANNEL_NOT_CONFIGURED: WHATSAPP_PHONE_NUMBER_ID missing in .env"
            return IntegrationStatus.NOT_CONFIGURED, "CHANNEL_NOT_CONFIGURED: No WHATSAPP_BUSINESS_TOKEN in .env"
        return IntegrationStatus.NOT_CONFIGURED, f"CHANNEL_NOT_CONFIGURED: Unknown channel '{channel}'"


class RecipientExtractor:
    INVALID_IG_PATHS = {"p", "reel", "stories", "popular", "explore", "tv", "topics"}
    INVALID_FB_PATHS = {"groups", "posts", "videos", "events", "marketplace", "watch"}

    @staticmethod
    def extract(lead: Dict[str, Any], channel: ChannelType) -> Tuple[str, str]:
        if channel == ChannelType.INSTAGRAM:
            return RecipientExtractor._instagram(lead)
        elif channel == ChannelType.FACEBOOK:
            return RecipientExtractor._facebook(lead)
        elif channel == ChannelType.EMAIL:
            return RecipientExtractor._email(lead)
        elif channel == ChannelType.WHATSAPP:
            return RecipientExtractor._whatsapp(lead)
        return "", RecipientStatus.NOT_FOUND

    @staticmethod
    def _instagram(lead: Dict[str, Any]) -> Tuple[str, str]:
        ig_url = str(lead.get("instagram_url", "") or "").strip()
        if not ig_url or "instagram.com" not in ig_url:
            return "", RecipientStatus.NOT_FOUND
        ig_url = ig_url.split("?")[0].rstrip("/")
        match = re.search(r"instagram\.com/([a-zA-Z0-9_\.]+)", ig_url)
        if not match:
            return "", RecipientStatus.INVALID
        username = match.group(1)
        if username.lower() in RecipientExtractor.INVALID_IG_PATHS:
            return "", RecipientStatus.INVALID
        return f"@{username}", RecipientStatus.VALID

    @staticmethod
    def _facebook(lead: Dict[str, Any]) -> Tuple[str, str]:
        fb_url = str(lead.get("facebook_url", "") or "").strip()
        if not fb_url or "facebook.com" not in fb_url:
            return "", RecipientStatus.NOT_FOUND
        fb_url = fb_url.split("?")[0].rstrip("/")
        match = re.search(r"facebook\.com/([a-zA-Z0-9_\.\-]+)", fb_url)
        if not match:
            return "", RecipientStatus.INVALID
        slug = match.group(1)
        if slug.lower() in RecipientExtractor.INVALID_FB_PATHS:
            return "", RecipientStatus.INVALID
        return slug, RecipientStatus.VALID

    @staticmethod
    def _email(lead: Dict[str, Any]) -> Tuple[str, str]:
        email = str(lead.get("email", "") or "").strip()
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            return "", RecipientStatus.NOT_FOUND
        return email, RecipientStatus.VALID

    @staticmethod
    def _whatsapp(lead: Dict[str, Any]) -> Tuple[str, str]:
        phone = str(lead.get("phone", "") or "").strip().replace("'", "")
        if not phone or len(phone) < 7:
            return "", RecipientStatus.NOT_FOUND
        return phone, RecipientStatus.VALID


class OwnershipResolver:
    @staticmethod
    def resolve(lead: Dict[str, Any], channel: ChannelType) -> str:
        social_ownership = str(lead.get("social_ownership_status", "") or "").strip().upper()

        if channel == ChannelType.INSTAGRAM:
            ig_url = str(lead.get("instagram_url", "") or "").strip()
            if not ig_url:
                return OwnershipStatus.UNVERIFIED
            red_flags = str(lead.get("red_flags", "") or "").lower()
            if "instagram" in red_flags and any(x in red_flags for x in ["post url", "topic", "search page"]):
                return OwnershipStatus.UNVERIFIED
            if social_ownership == "VERIFIED":
                ig_clean = ig_url.split("?")[0].rstrip("/")
                match = re.search(r"instagram\.com/([a-zA-Z0-9_\.]+)", ig_clean)
                if match and match.group(1).lower() not in RecipientExtractor.INVALID_IG_PATHS:
                    return OwnershipStatus.VERIFIED
            return OwnershipStatus.UNVERIFIED

        elif channel == ChannelType.FACEBOOK:
            fb_url = str(lead.get("facebook_url", "") or "").strip()
            if not fb_url:
                return OwnershipStatus.UNVERIFIED
            if social_ownership == "VERIFIED" and "facebook.com" in fb_url:
                fb_clean = fb_url.split("?")[0].rstrip("/")
                match = re.search(r"facebook\.com/([a-zA-Z0-9_\.\-]+)", fb_clean)
                if match and match.group(1).lower() not in RecipientExtractor.INVALID_FB_PATHS:
                    return OwnershipStatus.VERIFIED
            return OwnershipStatus.UNVERIFIED

        elif channel == ChannelType.EMAIL:
            email = str(lead.get("email", "") or "").strip()
            if email and "@" in email:
                return OwnershipStatus.VERIFIED
            return OwnershipStatus.UNVERIFIED

        elif channel == ChannelType.WHATSAPP:
            phone = str(lead.get("phone", "") or "").strip().replace("'", "")
            if phone and len(phone) >= 7:
                return OwnershipStatus.VERIFIED
            return OwnershipStatus.UNVERIFIED

        return OwnershipStatus.UNKNOWN


class ChannelEvaluator:
    @staticmethod
    def evaluate(lead: Dict[str, Any], channel: ChannelType) -> ChannelEligibility:
        from lib.outreach.contactability import ContactabilityAssessor

        # Run unified contactability assessment
        assessment = ContactabilityAssessor.assess_lead(lead)
        ch_eval = assessment.channels.get(channel.value)

        if ch_eval:
            ownership_status = OwnershipStatus.VERIFIED if ch_eval.business_identity_verified else OwnershipStatus.UNVERIFIED
            recipient_status = RecipientStatus.VALID if ch_eval.recipient_verified else (
                "RECIPIENT_ID_REQUIRED" if "RECIPIENT_ID_REQUIRED" in ch_eval.reason else RecipientStatus.NOT_FOUND
            )
            integration_status = IntegrationStatus.CONFIGURED if ch_eval.send_integration_available else IntegrationStatus.NOT_CONFIGURED

            return ChannelEligibility(
                channel=channel.value,
                ownership_status=ownership_status,
                recipient=ch_eval.recipient,
                recipient_status=str(recipient_status),
                integration_status=integration_status,
                sendable=ch_eval.sendable,
                block_reason="" if ch_eval.sendable else ch_eval.reason,
            )

        # Fallback for WhatsApp or unhandled channels
        ownership_status = OwnershipResolver.resolve(lead, channel)
        recipient, recipient_status = RecipientExtractor.extract(lead, channel)
        integration_status, integration_note = IntegrationChecker.check(channel)

        return ChannelEligibility(
            channel=channel.value,
            ownership_status=ownership_status,
            recipient=recipient,
            recipient_status=recipient_status,
            integration_status=integration_status,
            sendable=False,
            block_reason="CHANNEL_UNSUPPORTED",
        )


class ChannelRouter:
    def evaluate_all_channels(self, lead: Dict[str, Any]) -> Dict[str, ChannelEligibility]:
        return {ch.value: ChannelEvaluator.evaluate(lead, ch) for ch in ChannelType.all_channels()}

    def route_lead(self, lead: Dict[str, Any], requested_channel: str = "Auto-select") -> ChannelRoutingResult:
        all_evals = self.evaluate_all_channels(lead)
        availability_dict = {k: v.to_dict() for k, v in all_evals.items()}
        req = (requested_channel or "").strip().lower()
        is_auto = req in ("auto-select", "auto", "auto_select", "", "none")
        if is_auto:
            return self._route_auto(all_evals, availability_dict)
        else:
            return self._route_explicit(requested_channel, all_evals, availability_dict)

    def _route_auto(self, all_evals: Dict[str, ChannelEligibility], availability_dict: Dict[str, Any]) -> ChannelRoutingResult:
        # Priority order: Email -> Instagram -> Facebook
        for ch in AUTO_SELECT_PRIORITY:
            ev = all_evals.get(ch.value)
            if ev and ev.sendable:
                return ChannelRoutingResult(
                    selected_channel=ch.value,
                    channel_selection_reason=f"AUTO_SELECT: {ch.value} chosen — RECIPIENT_VERIFIED + INTEGRATION_ACTIVE + COMPLIANCE_ELIGIBLE",
                    sendable=True,
                    block_reason="",
                    channel_availability=availability_dict,
                )
        reasons = []
        for ch in AUTO_SELECT_PRIORITY:
            ev = all_evals.get(ch.value)
            if ev:
                reasons.append(f"{ch.name}={ev.block_reason or 'BLOCKED'}")
        compound_reason = "NO_SENDABLE_CHANNEL: " + "; ".join(reasons)
        return ChannelRoutingResult(
            selected_channel="NONE",
            channel_selection_reason="AUTO_SELECT: No verified sendable channel found",
            sendable=False,
            block_reason=compound_reason,
            channel_availability=availability_dict,
        )

    def _route_explicit(self, requested_channel: str, all_evals: Dict[str, ChannelEligibility], availability_dict: Dict[str, Any]) -> ChannelRoutingResult:
        ch_type = ChannelType.from_string(requested_channel)
        if ch_type is None:
            return ChannelRoutingResult(
                selected_channel="NONE",
                channel_selection_reason=f"EXPLICIT: Unknown channel '{requested_channel}'",
                sendable=False,
                block_reason=f"UNKNOWN_CHANNEL: '{requested_channel}' is not a supported channel",
                channel_availability=availability_dict,
            )
        ev = all_evals.get(ch_type.value)
        if ev and ev.sendable:
            return ChannelRoutingResult(
                selected_channel=ch_type.value,
                channel_selection_reason=f"EXPLICIT: {ch_type.value} selected - VERIFIED_RECIPIENT + SEND_INTEGRATION_AVAILABLE",
                sendable=True,
                block_reason="",
                channel_availability=availability_dict,
            )
        block = ev.block_reason if ev else "CHANNEL_EVALUATION_FAILED"
        return ChannelRoutingResult(
            selected_channel="NONE",
            channel_selection_reason=f"EXPLICIT: {ch_type.value} selected but not sendable - blocked without fallback",
            sendable=False,
            block_reason=block,
            channel_availability=availability_dict,
        )


def summarize_channel_availability(leads: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    router = ChannelRouter()
    for ch in ChannelType.all_channels():
        summary[ch.value] = {
            "channel": ch.value,
            "verified_recipients": 0,
            "sendable_count": 0,
            "integration_status": None,
            "not_configured_reason": "",
        }
    for lead in leads:
        evals = router.evaluate_all_channels(lead)
        for ch_name, ev in evals.items():
            if ch_name in summary:
                if ev.recipient_status == RecipientStatus.VALID:
                    summary[ch_name]["verified_recipients"] += 1
                if ev.sendable:
                    summary[ch_name]["sendable_count"] += 1
                if summary[ch_name]["integration_status"] is None:
                    summary[ch_name]["integration_status"] = ev.integration_status
                    if ev.integration_status == IntegrationStatus.NOT_CONFIGURED:
                        summary[ch_name]["not_configured_reason"] = ev.block_reason
    return summary
