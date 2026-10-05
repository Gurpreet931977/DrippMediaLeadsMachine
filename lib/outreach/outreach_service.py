import os
import re
import json
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from lib.types import (
    Lead,
    OutreachStatus,
    OutreachMode,
    CallOutcome,
    QualificationState,
    OperationalStatus,
    WebsiteStatus,
    SocialOwnershipStatus
)
from lib.outreach.outreach_generator import OutreachAngleGenerator
from lib.outreach.channel_router import ChannelRouter, ChannelType, summarize_channel_availability
from lib.sheets.google_sheets import GoogleSheetsStorageProvider

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
CAMPAIGNS_FILE = os.path.join(DATA_DIR, "campaigns.json")
QUEUE_FILE = os.path.join(DATA_DIR, "outreach_queue.json")
MANUAL_LOGS_FILE = os.path.join(DATA_DIR, "manual_outreach.json")
EXPERIMENTS_FILE = os.path.join(DATA_DIR, "experiments.json")


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    for f in [CAMPAIGNS_FILE, QUEUE_FILE, MANUAL_LOGS_FILE]:
        if not os.path.exists(f):
            with open(f, "w", encoding="utf-8") as fp:
                json.dump([], fp)
    if not os.path.exists(EXPERIMENTS_FILE):
        with open(EXPERIMENTS_FILE, "w", encoding="utf-8") as fp:
            json.dump({
                "manual": {
                    "assigned_leads": 0,
                    "contacted": 0,
                    "replies": 0,
                    "positive_replies": 0,
                    "meetings": 0,
                    "clients": 0,
                    "revenue": 0.0
                },
                "auto": {
                    "assigned_leads": 0,
                    "sent": 0,
                    "delivered": 0,
                    "blocked": 0,
                    "replies": 0,
                    "positive_replies": 0,
                    "meetings": 0,
                    "clients": 0,
                    "revenue": 0.0
                }
            }, fp, indent=2)


class ChannelConfigManager:
    """
    Section 8 & 23: Strictly verifies whether real, authorized sending integrations
    are configured. Never fakes or simulates sending. Reports CHANNEL NOT CONFIGURED
    when credentials are absent; disables sending.
    """
    SUPPORTED_CHANNELS = [
        "Instagram Direct Message",
        "Facebook Messenger",
        "WhatsApp",
        "Email",
        "SMS / Phone"
    ]

    @classmethod
    def get_channel_status(cls, channel_name: str) -> Dict[str, Any]:
        ch_lower = (channel_name or "").lower()
        if "instagram" in ch_lower:
            has_token = bool(os.getenv("INSTAGRAM_GRAPH_TOKEN") or os.getenv("META_ACCESS_TOKEN"))
            return {
                "channel": channel_name,
                "configured": has_token,
                "provider": "Meta Graph API (Instagram DM)",
                "status_label": "CONFIGURED" if has_token else "CHANNEL NOT CONFIGURED",
                "message": "Connected to Meta Graph API" if has_token else "CHANNEL NOT CONFIGURED: No active Meta Graph API token in .env. Automated sending blocked."
            }
        elif "facebook" in ch_lower:
            has_token = bool(os.getenv("META_PAGE_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN"))
            return {
                "channel": channel_name,
                "configured": has_token,
                "provider": "Meta Business Messaging API",
                "status_label": "CONFIGURED" if has_token else "CHANNEL NOT CONFIGURED",
                "message": "Connected to Meta Business API" if has_token else "CHANNEL NOT CONFIGURED: No active Meta Page Access token in .env. Automated sending blocked."
            }
        elif "whatsapp" in ch_lower:
            token = os.getenv("WHATSAPP_BUSINESS_TOKEN")
            phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
            has_wa = bool(token and phone_id)
            if has_wa:
                msg = f"Connected to WhatsApp Cloud API (Phone ID: {phone_id})"
            elif token:
                msg = "CHANNEL NOT CONFIGURED: WHATSAPP_PHONE_NUMBER_ID missing in .env. Automated sending blocked."
            else:
                msg = "CHANNEL NOT CONFIGURED: No active WhatsApp Business token in .env. Automated sending blocked."
            return {
                "channel": channel_name,
                "configured": has_wa,
                "provider": "WhatsApp Cloud API",
                "status_label": "CONFIGURED" if has_wa else "CHANNEL NOT CONFIGURED",
                "message": msg
            }
        elif "email" in ch_lower:
            has_smtp = bool(os.getenv("SMTP_HOST") or os.getenv("SENDGRID_API_KEY"))
            return {
                "channel": channel_name,
                "configured": has_smtp,
                "provider": "SMTP / SendGrid",
                "status_label": "CONFIGURED" if has_smtp else "CHANNEL NOT CONFIGURED",
                "message": "Connected to Email Gateway" if has_smtp else "CHANNEL NOT CONFIGURED: No active SMTP or SendGrid credentials in .env. Automated sending blocked."
            }
        elif "sms" in ch_lower or "phone" in ch_lower:
            has_twilio = bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN"))
            return {
                "channel": channel_name,
                "configured": has_twilio,
                "provider": "Twilio Messaging",
                "status_label": "CONFIGURED" if has_twilio else "CHANNEL NOT CONFIGURED",
                "message": "Connected to Twilio API" if has_twilio else "CHANNEL NOT CONFIGURED: No active Twilio credentials in .env. Automated sending blocked."
            }
        else:
            return {
                "channel": channel_name,
                "configured": False,
                "provider": "Unknown",
                "status_label": "CHANNEL NOT CONFIGURED",
                "message": f"CHANNEL NOT CONFIGURED: Unknown channel '{channel_name}'."
            }

    @classmethod
    def get_all_channels(cls) -> List[Dict[str, Any]]:
        return [cls.get_channel_status(ch) for ch in cls.SUPPORTED_CHANNELS]


class OutreachEligibilityChecker:
    """
    Section 9: Final Pre-Send Eligibility Check.
    ALL conditions must pass before a lead enters the send queue.
    If any check fails: outreach_status = BLOCKED with recorded outreach_block_reason.
    """
    @classmethod
    def verify_eligibility(
        cls,
        lead: Dict[str, Any],
        campaign_id: str,
        channel: str,
        existing_queue: Optional[List[Dict[str, Any]]] = None,
        dry_run: bool = False
    ) -> Tuple[bool, str]:
        # 1. qualification_status = OUTREACH_READY
        q_state = str(lead.get("qualification_state", "") or lead.get("qualification_status", "")).strip()
        if q_state != QualificationState.OUTREACH_READY.value:
            return False, f"Qualification status is '{q_state}', strictly requires OUTREACH_READY"

        # 2. Country verified
        target_country = str(lead.get("target_country", "")).strip().lower()
        lead_country = str(lead.get("country", "")).strip().lower()
        if target_country and lead_country and target_country not in lead_country and lead_country not in target_country:
            return False, f"Country mismatch: lead '{lead_country}' vs target '{target_country}'"

        # 3. Business identity verified
        name = str(lead.get("company_name", "") or lead.get("business_name", "")).strip()
        if not name or len(name) < 2:
            return False, "Business identity invalid: missing or empty company name"

        # 4. NO_WEBSITE_CONFIRMED
        web_status = str(lead.get("website_status", "")).strip()
        if web_status != WebsiteStatus.NO_WEBSITE_CONFIRMED.value:
            return False, f"Website status is '{web_status}', requires NO_WEBSITE_CONFIRMED"

        # 5. operational_status = ACTIVE_CONFIRMED
        op_status = str(lead.get("operational_status", "")).strip()
        if op_status != OperationalStatus.ACTIVE_CONFIRMED.value:
            return False, f"Operational status is '{op_status}', strictly requires ACTIVE_CONFIRMED"

        # 6. No major red flags
        red_flags = str(lead.get("red_flags", "")).strip()
        fatal_flags = ["PERMANENTLY_CLOSED", "TEMPORARILY_CLOSED", "COUNTRY_MISMATCH", "INVALID_IDENTITY", "CLOSED_OR_UNVERIFIED"]
        for flag in fatal_flags:
            if flag.lower() in red_flags.lower():
                return False, f"Major red flag detected: {flag}"

        # 7. Duplicate protection: not already queued/sent in same campaign
        lead_id = str(lead.get("lead_id", "")).strip()
        if existing_queue:
            for item in existing_queue:
                if item.get("lead_id") == lead_id and item.get("campaign_id") == campaign_id:
                    if item.get("status") in [
                        OutreachStatus.QUEUED.value, OutreachStatus.SENDING.value,
                        OutreachStatus.SENT.value, OutreachStatus.DELIVERED.value
                    ]:
                        return False, f"Duplicate send blocked: lead {lead_id} already queued or sent in campaign {campaign_id}"

        # 8. Channel contactability, recipient verification & compliance gate (Section 18)
        from lib.outreach.contactability import ContactabilityAssessor
        assessment = ContactabilityAssessor.assess_lead(lead)
        ch_eval = assessment.channels.get(channel)

        if not dry_run:
            if not ch_eval or not ch_eval.sendable:
                reason = ch_eval.reason if ch_eval else f"Channel '{channel}' is not supported"
                return False, f"NOT_SENDABLE: {reason}"

        # 9. Valid recipient/contact
        recipient = cls._extract_recipient(lead, channel, assessment)
        if not recipient or recipient in ("NONE", "None", ""):
            return False, f"No valid recipient contact found for channel '{channel}'"

        # 10. Personalized message generated successfully
        msg = str(lead.get("outreach_message", "") or lead.get("outreach_angle", "")).strip()
        if not msg:
            return False, "Personalized outreach message generation failed or is empty"

        return True, ""

    @classmethod
    def _extract_recipient(cls, lead: Dict[str, Any], channel: str, assessment: Optional[Any] = None) -> str:
        if assessment and channel in assessment.channels:
            ch_recipient = assessment.channels[channel].recipient
            if ch_recipient and ch_recipient != "NONE":
                return ch_recipient

        ch_lower = channel.lower()
        if "instagram" in ch_lower:
            ig = lead.get("instagram_url", "")
            if ig and "instagram.com" in ig:
                match = re.search(r"instagram\.com/([a-zA-Z0-9_\.]+)", ig)
                if match and match.group(1) not in ["p", "reel", "stories", "popular", "explore"]:
                    return f"@{match.group(1)}"
            return ""
        elif "facebook" in ch_lower:
            fb = lead.get("facebook_url", "")
            if fb and "facebook.com" in fb:
                match = re.search(r"facebook\.com/([a-zA-Z0-9_\.\-]+)", fb)
                if match and match.group(1) not in ["groups", "posts", "videos"]:
                    return match.group(1)
            return ""
        elif "email" in ch_lower:
            return str(lead.get("email", "")).strip()
        elif "sms" in ch_lower or "phone" in ch_lower:
            phone = str(lead.get("phone", "")).strip().replace("'", "")
            return phone if len(phone) >= 7 else ""
        return ""


class OutreachService:
    """
    Central Coordinator for Post-Pipeline Outreach Actions.
    Manages Campaigns, Persistent Queue, Eligibility, Channel Guard,
    Manual Assignment, Manual Call Logging, Dry-Run Preview, and Experiment Tracking.
    """
    def __init__(self):
        ensure_data_dir()
        self.angle_gen = OutreachAngleGenerator()

    # ==========================================
    # CAMPAIGN MANAGEMENT (Section 15)
    # ==========================================
    def list_campaigns(self) -> List[Dict[str, Any]]:
        ensure_data_dir()
        try:
            with open(CAMPAIGNS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def get_campaign(self, campaign_id: str) -> Optional[Dict[str, Any]]:
        campaigns = self.list_campaigns()
        for c in campaigns:
            if c.get("campaign_id") == campaign_id:
                return c
        return None

    def create_campaign(
        self,
        source_pipeline_run_id: str,
        campaign_name: str,
        country: str,
        city: str,
        category: str,
        outreach_mode: str,
        channel: str,
        selected_lead_count: int,
        daily_limit: int = 10,
        max_campaign_limit: int = 10,
        message_strategy: str = "Personalized"
    ) -> Dict[str, Any]:
        ensure_data_dir()
        campaigns = self.list_campaigns()

        city_slug = re.sub(r'[^a-zA-Z0-9]', '', city[:3].upper()) or "MCR"
        num = len(campaigns) + 1
        campaign_id = f"OUT-{city_slug}-{datetime.now().year}-{num:03d}"

        campaign = {
            "campaign_id": campaign_id,
            "campaign_name": campaign_name or f"{city} {category} - Campaign {num:02d}",
            "created_at": datetime.now().isoformat(),
            "source_pipeline_run_id": source_pipeline_run_id,
            "country": country,
            "city": city,
            "category": category,
            "outreach_mode": outreach_mode,
            "channel": channel,
            "daily_limit": daily_limit,
            "max_campaign_limit": max_campaign_limit,
            "message_strategy": message_strategy,
            "selected_lead_count": selected_lead_count,
            "queued_count": 0,
            "sent_count": 0,
            "delivered_count": 0,
            "failed_count": 0,
            "blocked_count": 0,
            "replied_count": 0,
            "status": "ACTIVE"
        }

        campaigns.append(campaign)
        with open(CAMPAIGNS_FILE, "w", encoding="utf-8") as f:
            json.dump(campaigns, f, indent=2)

        return campaign

    # ==========================================
    # OUTREACH QUEUE & SENDING (Sections 9, 10, 16)
    # ==========================================
    def get_queue(self, campaign_id: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_data_dir()
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                queue = json.load(f)
            if campaign_id:
                return [q for q in queue if q.get("campaign_id") == campaign_id]
            return queue
        except Exception:
            return []

    def _build_message_object(self, lead: Dict[str, Any], channel: str, angle: str, now_str: str) -> Dict[str, Any]:
        """Section 12: Build a storable outreach message object (legacy method)."""
        return self._build_channel_message_object(lead, channel, angle, now_str)

    def _build_channel_message_object(self, lead: Dict[str, Any], channel: str, angle: str, now_str: str) -> Dict[str, Any]:
        """Section 12: Build a channel-aware storable outreach message object."""
        return self.angle_gen.generate_message_object(
            business_name=str(lead.get("company_name", "") or lead.get("business_name", "")).strip(),
            city=str(lead.get("city", "")).strip(),
            industry=str(lead.get("industry", "Restaurant")).strip(),
            review_count=int(lead.get("review_count", 0) or 0),
            rating=float(lead.get("rating", 0.0) or 0.0),
            channel=channel,
            instagram_url=str(lead.get("instagram_url", "") or ""),
            facebook_url=str(lead.get("facebook_url", "") or ""),
            tiktok_url=str(lead.get("tiktok_url", "") or ""),
            outreach_angle=angle,
            source_lead=lead
        )

    def _resolve_angle(self, lead: Dict[str, Any]) -> str:
        """Resolve or regenerate the outreach angle for a lead dict."""
        angle = str(lead.get("outreach_angle", "")).strip()
        if not angle:
            from lib.types import DiscoveredBusiness
            d_biz = DiscoveredBusiness(
                company_name=lead.get("company_name", ""),
                category=lead.get("industry", "Restaurant"),
                city=lead.get("city", ""),
                target_country=lead.get("target_country", "United Kingdom"),
                review_count=lead.get("review_count", 0),
                rating=lead.get("rating", 0.0),
                instagram_url=lead.get("instagram_url", ""),
                facebook_url=lead.get("facebook_url", ""),
                tiktok_url=lead.get("tiktok_url", "")
            )
            angle = self.angle_gen.generate_angle(d_biz)
        return angle

    def dry_run_auto_outreach(
        self,
        campaign_id: str,
        leads_data: List[Dict[str, Any]],
        channel: str
    ) -> Dict[str, Any]:
        """
        Section 25 + Channel Router: DRY RUN mode.
        Validates eligibility, generates channel-aware messages, previews routing.
        Does NOT persist queue items or contact any business.
        outreach_status = READY_FOR_SEND for routed leads (never SENT).
        """
        ensure_data_dir()
        current_queue = self.get_queue()
        now_str = datetime.now().isoformat()
        router = ChannelRouter()

        ready_items = []
        blocked_items = []

        for lead in leads_data:
            lead_id = str(lead.get("lead_id", "")).strip()
            name = str(lead.get("company_name", "") or lead.get("business_name", "")).strip()
            angle = self._resolve_angle(lead)

            # STEP 1: Run Channel Router
            routing = router.route_lead(lead, requested_channel=channel)
            selected_channel = routing.selected_channel if routing.sendable else "NONE"

            # STEP 2: Base eligibility check (channel check advisory in dry-run)
            # Use selected_channel for eligibility if auto-routed, else requested
            check_channel = selected_channel if routing.sendable else (channel if channel not in ("Auto-select", "auto", "") else "Instagram Direct Message")
            is_eligible, block_reason = OutreachEligibilityChecker.verify_eligibility(
                lead=lead,
                campaign_id=campaign_id,
                channel=check_channel,
                existing_queue=current_queue,
                dry_run=True
            )

            # If routing says blocked but eligibility passes, respect routing decision
            if not routing.sendable:
                is_eligible = False
                block_reason = routing.block_reason

            recipient = ""
            if routing.sendable:
                ch_eval = routing.channel_availability.get(selected_channel, {})
                recipient = ch_eval.get("recipient", "")

            # Use selected channel for message formatting
            effective_channel = selected_channel if routing.sendable else check_channel
            message_obj = self._build_channel_message_object(lead, effective_channel, angle, now_str)

            # Determine integration status for the channel
            ch_status = ChannelConfigManager.get_channel_status(effective_channel)

            queue_id = f"DRY-{uuid.uuid4().hex[:8].upper()}"
            item = {
                "queue_id": queue_id,
                "campaign_id": campaign_id,
                "lead_id": lead_id,
                "company_name": name,
                "channel": effective_channel,
                "selected_channel": selected_channel,
                "channel_selection_reason": routing.channel_selection_reason,
                "channel_availability": routing.channel_availability,
                "recipient": recipient or "N/A",
                "message": message_obj,
                "qualification_state": lead.get("qualification_state") or lead.get("qualification_status") or "OUTREACH_READY",
                "dry_run": True,
                "channel_configured": ch_status["configured"],
                "channel_status": ch_status["status_label"],
            }

            if is_eligible:
                item["status"] = OutreachStatus.READY_FOR_SEND.value
                item["block_reason"] = ""
                ready_items.append(item)
            else:
                item["status"] = OutreachStatus.BLOCKED.value
                item["block_reason"] = block_reason
                blocked_items.append(item)

        # Build channel availability summary across all leads
        channel_summary = summarize_channel_availability(leads_data)

        # Legacy ch_status for backward compat
        legacy_ch = channel if channel not in ("Auto-select", "auto", "") else "Instagram Direct Message"
        ch_status = ChannelConfigManager.get_channel_status(legacy_ch)

        return {
            "mode": "DRY_RUN",
            "campaign_id": campaign_id,
            "channel": channel,
            "channel_configured": ch_status["configured"],
            "channel_status": ch_status["status_label"],
            "channel_availability_summary": channel_summary,
            "total_processed": len(leads_data),
            "ready_for_send_count": len(ready_items),
            "blocked_count": len(blocked_items),
            "actual_sent": 0,
            "ready_for_send": ready_items,
            "blocked": blocked_items,
            "note": (
                "DRY RUN: No messages sent. No businesses contacted. "
                "No queue items persisted. "
                "Set dry_run=false to create real queue entries (channel must be configured)."
            )
        }

    def queue_leads_for_auto_outreach(
        self,
        campaign_id: str,
        leads_data: List[Dict[str, Any]],
        channel: str
    ) -> Dict[str, Any]:
        """
        Section 16 + Channel Router: Creates persistent outreach queue items for OUTREACH_READY leads.
        Performs immediate channel routing, eligibility re-verification, and duplicate protection.
        Does NOT send — leads are queued for the sending adapter when channels are live.
        """
        ensure_data_dir()
        campaign = self.get_campaign(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found.")

        current_queue = self.get_queue()
        router = ChannelRouter()

        queued_items = []
        blocked_items = []
        now_str = datetime.now().isoformat()
        sheets_updates = []

        for lead in leads_data:
            lead_id = str(lead.get("lead_id", "")).strip()
            name = str(lead.get("company_name", "") or lead.get("business_name", "")).strip()
            angle = self._resolve_angle(lead)

            # STEP 1: Channel routing
            routing = router.route_lead(lead, requested_channel=channel)
            selected_channel = routing.selected_channel if routing.sendable else "NONE"
            check_channel = selected_channel if routing.sendable else (channel if channel not in ("Auto-select", "auto", "") else "Instagram Direct Message")

            # STEP 2: Strict eligibility (not dry-run, channel must be configured)
            is_eligible, block_reason = OutreachEligibilityChecker.verify_eligibility(
                lead=lead,
                campaign_id=campaign_id,
                channel=check_channel,
                existing_queue=current_queue,
                dry_run=False
            )

            # Channel router block overrides eligibility pass
            if not routing.sendable:
                is_eligible = False
                block_reason = routing.block_reason

            recipient = ""
            if routing.sendable:
                ch_eval = routing.channel_availability.get(selected_channel, {})
                recipient = ch_eval.get("recipient", "")

            effective_channel = selected_channel if routing.sendable else check_channel
            message_obj = self._build_channel_message_object(lead, effective_channel, angle, now_str)
            ch_status = ChannelConfigManager.get_channel_status(effective_channel)
            queue_id = f"Q-{uuid.uuid4().hex[:8].upper()}"

            if is_eligible:
                item = {
                    "queue_id": queue_id,
                    "campaign_id": campaign_id,
                    "lead_id": lead_id,
                    "company_name": name,
                    "channel": effective_channel,
                    "selected_channel": selected_channel,
                    "channel_selection_reason": routing.channel_selection_reason,
                    "channel_availability": routing.channel_availability,
                    "recipient": recipient,
                    "message": message_obj,
                    "final_message": message_obj.get("message_body", "") if isinstance(message_obj, dict) else str(message_obj),
                    "final_subject": message_obj.get("message_subject", "") if isinstance(message_obj, dict) else "",
                    "qualification_state": lead.get("qualification_state") or lead.get("qualification_status") or "OUTREACH_READY",
                    "instagram_ownership_status": lead.get("instagram_ownership_status", ""),
                    "facebook_ownership_status": lead.get("facebook_ownership_status", ""),
                    "social_ownership_status": lead.get("social_ownership_status", ""),
                    "status": OutreachStatus.QUEUED.value,
                    "block_reason": "",
                    "attempts": 0,
                    "queued_at": now_str,
                    "sent_at": None,
                    "error_message": None
                }
                queued_items.append(item)
                sheets_updates.append((lead_id, {
                    "campaign_id": campaign_id,
                    "outreach_mode": OutreachMode.AUTO.value,
                    "outreach_status": OutreachStatus.QUEUED.value,
                    "outreach_channel": effective_channel,
                    "outreach_message": angle,
                    "outreach_generated_at": now_str
                }))
            else:
                item = {
                    "queue_id": queue_id,
                    "campaign_id": campaign_id,
                    "lead_id": lead_id,
                    "company_name": name,
                    "channel": effective_channel,
                    "selected_channel": "NONE",
                    "channel_selection_reason": routing.channel_selection_reason,
                    "channel_availability": routing.channel_availability,
                    "recipient": recipient or "N/A",
                    "message": message_obj,
                    "status": OutreachStatus.BLOCKED.value,
                    "block_reason": block_reason,
                    "attempts": 0,
                    "queued_at": now_str,
                    "sent_at": None,
                    "error_message": block_reason
                }
                blocked_items.append(item)
                sheets_updates.append((lead_id, {
                    "campaign_id": campaign_id,
                    "outreach_mode": OutreachMode.AUTO.value,
                    "outreach_status": OutreachStatus.BLOCKED.value,
                    "outreach_channel": channel,
                    "outreach_message": angle,
                    "outreach_block_reason": block_reason,
                    "outreach_generated_at": now_str
                }))

        # Persist to disk (Section 16: queue survives browser refresh)
        all_new = queued_items + blocked_items
        current_queue.extend(all_new)
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(current_queue, f, indent=2)

        # Update campaign stats
        campaigns = self.list_campaigns()
        for c in campaigns:
            if c.get("campaign_id") == campaign_id:
                c["queued_count"] = c.get("queued_count", 0) + len(queued_items)
                c["blocked_count"] = c.get("blocked_count", 0) + len(blocked_items)
                break
        with open(CAMPAIGNS_FILE, "w", encoding="utf-8") as f:
            json.dump(campaigns, f, indent=2)

        # Update Google Sheets CRM
        self._sync_sheets_outreach_updates(sheets_updates)

        # Update experiment tracking data
        self._record_experiment_auto_assigned(len(all_new), len(blocked_items))

        return {
            "campaign_id": campaign_id,
            "total_processed": len(leads_data),
            "queued_count": len(queued_items),
            "blocked_count": len(blocked_items),
            "channel_configured": ch_status["configured"],
            "channel_message": ch_status["message"],
            "queued": queued_items,
            "blocked": blocked_items
        }

    # ==========================================
    # MANUAL OUTREACH ASSIGNMENT (Section 5 & 19)
    # ==========================================
    def assign_manual_outreach(
        self,
        leads_data: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Section 19: Assigns selected leads to MANUAL outreach mode.
        Sets outreach_mode = MANUAL and outreach_status = READY_FOR_REVIEW.
        Does NOT log a call — that is a separate action.
        Does NOT duplicate lead records.
        """
        ensure_data_dir()
        now_str = datetime.now().isoformat()
        assigned = []
        sheets_updates = []

        for lead in leads_data:
            lead_id = str(lead.get("lead_id", "")).strip()
            name = str(lead.get("company_name", "")).strip()
            if not lead_id:
                continue

            angle = self._resolve_angle(lead)

            assigned.append({
                "lead_id": lead_id,
                "company_name": name,
                "outreach_mode": OutreachMode.MANUAL.value,
                "outreach_status": OutreachStatus.READY_FOR_REVIEW.value,
                "assigned_at": now_str
            })

            sheets_updates.append((lead_id, {
                "outreach_mode": OutreachMode.MANUAL.value,
                "outreach_status": OutreachStatus.READY_FOR_REVIEW.value,
                "outreach_channel": "Manual",
                "outreach_message": angle,
                "outreach_generated_at": now_str,
            }))

        self._sync_sheets_outreach_updates(sheets_updates)
        self._record_experiment_manual_assigned(len(assigned))

        return {
            "assigned_count": len(assigned),
            "assigned": assigned
        }

    # ==========================================
    # MANUAL OUTREACH & CALL LOGGING (Section 5)
    # ==========================================
    def log_manual_call(
        self,
        lead_id: str,
        company_name: str,
        call_status: str,
        call_notes: str,
        call_outcome: str = "",
        next_follow_up: str = "",
        marked_contacted: bool = True
    ) -> Dict[str, Any]:
        ensure_data_dir()
        now_str = datetime.now().isoformat()

        try:
            with open(MANUAL_LOGS_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except Exception:
            logs = []

        log_entry = {
            "log_id": f"CALL-{uuid.uuid4().hex[:8].upper()}",
            "lead_id": lead_id,
            "company_name": company_name,
            "call_status": call_status,
            "call_outcome": call_outcome,
            "call_notes": call_notes,
            "next_follow_up": next_follow_up,
            "contacted_at": now_str if marked_contacted else None,
            "logged_at": now_str
        }
        logs.append(log_entry)
        with open(MANUAL_LOGS_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=2)

        # Determine outreach status
        outreach_status = OutreachStatus.SENT.value if marked_contacted else OutreachStatus.READY_FOR_REVIEW.value
        if call_outcome == CallOutcome.MEETING_BOOKED.value:
            outreach_status = OutreachStatus.REPLIED.value
        elif next_follow_up:
            outreach_status = OutreachStatus.FOLLOW_UP_DUE.value

        update_payload = {
            "outreach_mode": OutreachMode.MANUAL.value,
            "outreach_status": outreach_status,
            "call_status": call_status,
            "call_notes": call_notes,
            "call_outcome": call_outcome,
            "next_follow_up": next_follow_up,
            "manual_outreach_notes": call_notes,
            "lead_status": "CONTACTED" if marked_contacted else "NOT_CONTACTED"
        }

        self._sync_sheets_outreach_updates([(lead_id, update_payload)])
        self._record_experiment_manual_call(call_outcome, marked_contacted)

        return log_entry

    def get_manual_logs(self, lead_id: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_data_dir()
        try:
            with open(MANUAL_LOGS_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
            if lead_id:
                return [l for l in logs if l.get("lead_id") == lead_id]
            return logs
        except Exception:
            return []

    # ==========================================
    # EXPERIMENT TRACKING (Section 20)
    # ==========================================
    def get_experiment_stats(self) -> Dict[str, Any]:
        ensure_data_dir()
        try:
            with open(EXPERIMENTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _record_experiment_manual_assigned(self, count: int):
        stats = self.get_experiment_stats()
        manual = stats.get("manual", {})
        manual["assigned_leads"] = manual.get("assigned_leads", 0) + count
        stats["manual"] = manual
        with open(EXPERIMENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)

    def _record_experiment_manual_call(self, outcome: str, contacted: bool):
        stats = self.get_experiment_stats()
        manual = stats.get("manual", {})
        if contacted:
            manual["contacted"] = manual.get("contacted", 0) + 1
        if outcome in [CallOutcome.CONNECTED.value, CallOutcome.CALLBACK_REQUESTED.value]:
            manual["replies"] = manual.get("replies", 0) + 1
        if outcome in [CallOutcome.MEETING_BOOKED.value, CallOutcome.CALLBACK_REQUESTED.value]:
            manual["positive_replies"] = manual.get("positive_replies", 0) + 1
        if outcome == CallOutcome.MEETING_BOOKED.value:
            manual["meetings"] = manual.get("meetings", 0) + 1
        stats["manual"] = manual
        with open(EXPERIMENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)

    def _record_experiment_auto_assigned(self, total: int, blocked: int):
        stats = self.get_experiment_stats()
        auto = stats.get("auto", {})
        auto["assigned_leads"] = auto.get("assigned_leads", 0) + total
        auto["blocked"] = auto.get("blocked", 0) + blocked
        stats["auto"] = auto
        with open(EXPERIMENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)

    # ==========================================
    # SHEETS CRM SYNCHRONIZATION (Section 17)
    # ==========================================
    def _sync_sheets_outreach_updates(self, updates: List[Tuple[str, Dict[str, Any]]]):
        if not updates:
            return
        try:
            storage = GoogleSheetsStorageProvider()
            for lead_id, fields in updates:
                storage.update_lead_outreach(lead_id, fields)
        except Exception as e:
            print(f"[OutreachService] Warning: Sheets outreach sync exception: {e}")

    # ==========================================
    # BOUNCE PROCESSING & SUPPRESSION (Feedback)
    # ==========================================
    def process_bounce(
        self,
        lead_id: str,
        email: str,
        bounce_code: str = "550 5.1.1",
        bounce_reason: str = "The email account that you tried to reach does not exist.",
        bounce_provider: str = "Gmail SMTP",
        bounce_raw_reason: str = "",
        campaign_id: Optional[str] = None,
        message_id: Optional[str] = None,
        bounced_at: Optional[str] = None,
        sync_sheets: bool = True
    ) -> Dict[str, Any]:
        """Processes real delivery feedback (bounce) and updates suppression, queue, and CRM."""
        from lib.outreach.bounce_manager import BounceManager
        return BounceManager.process_bounce(
            lead_id=lead_id,
            email=email,
            bounce_code=bounce_code,
            bounce_reason=bounce_reason,
            bounce_provider=bounce_provider,
            bounce_raw_reason=bounce_raw_reason,
            campaign_id=campaign_id,
            message_id=message_id,
            bounced_at=bounced_at,
            sync_sheets=sync_sheets
        )
