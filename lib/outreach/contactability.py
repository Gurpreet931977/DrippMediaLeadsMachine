"""
Dripp Media — Lead Contactability & Recipient Verification Layer
=================================================================
Sections 7, 8, 9, 18, 19:
  - Calculates per-channel contactability (Instagram, Facebook, Email)
  - Separates business_identity_verified from recipient_verified & channel_sendable
  - Enforces Meta recipient ID requirement (rejects public handles with RECIPIENT_ID_REQUIRED)
  - Manages distinct lifecycle states:
      QualificationState (OUTREACH_READY)
      ContactabilityState (CONTACTABLE / NOT_CONTACTABLE / MANUAL_REVIEW)
      OutreachState (READY_FOR_REVIEW / READY_FOR_SEND / QUEUED / SENT / BLOCKED ...)
"""

import os
from enum import Enum
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, List, Optional, Tuple

from lib.outreach.email_enricher import (
    EmailEnricher,
    EmailVerificationStatus,
    RecipientConfidence,
    MXStatus,
    BusinessDomainType,
)
from lib.outreach.compliance import (
    UKComplianceEvaluator,
    MarketingEmailStatus,
    SuppressionManager,
    CooldownManager,
)
from lib.outreach.companies_house import (
    CompaniesHouseVerifier,
    CompaniesHouseRecord,
    EntityMatchStatus,
    EntityMatchConfidence,
    CompanyStatus,
)
from lib.country_adapters import get_country_adapter


# ──────────────────────────────────────────────────────────────────────────
# LIFECYCLE STATES & ENUMS (Phase 6)
# ──────────────────────────────────────────────────────────────────────────

class ContactabilityState(str, Enum):
    CONTACTABLE              = "CONTACTABLE"
    PARTIALLY_CONTACTABLE    = "PARTIALLY_CONTACTABLE"
    NOT_CONTACTABLE          = "NOT_CONTACTABLE"
    CONTACTABILITY_UNKNOWN   = "CONTACTABILITY_UNKNOWN"
    MANUAL_REVIEW            = "MANUAL_REVIEW"


class ChannelStatus(str, Enum):
    AVAILABLE   = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID     = "INVALID"
    UNKNOWN     = "UNKNOWN"
    BLOCKED     = "BLOCKED"
    SUPPRESSED  = "SUPPRESSED"


class ComplianceState(str, Enum):
    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class ChannelSendabilityState(str, Enum):
    SENDABLE         = "SENDABLE"
    NOT_SENDABLE     = "NOT_SENDABLE"
    REQUIRES_REVIEW  = "REQUIRES_REVIEW"
    SUPPRESSED       = "SUPPRESSED"
    COOLDOWN         = "COOLDOWN"


@dataclass
class ChannelContactability:
    channel: str
    business_identity_verified: bool
    recipient: str
    recipient_verified: bool
    recipient_confidence: str
    send_integration_available: bool
    compliance_status: str
    sendable: bool
    reason: str
    status: str = ChannelStatus.UNKNOWN.value
    manual_contactable: bool = False
    automated_contactable: bool = False
    profile_found: bool = False
    profile_accessible: bool = False
    business_owned: bool = False
    recipient_id_available: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LeadContactabilityAssessment:
    lead_id: str
    company_name: str
    qualification_state: str
    contactability_status: str
    contactability_reason: str
    channels: Dict[str, ChannelContactability]
    sendable_channels: List[str]
    email_data: Optional[Dict[str, Any]]
    compliance_data: Optional[Dict[str, Any]]
    entity_data: Optional[Dict[str, Any]]
    is_ready_for_send: bool
    email_enrichment_data: Optional[Dict[str, Any]] = None
    manual_contactable: bool = False
    automated_contactable: bool = False
    compliance_status: str = ComplianceState.UNKNOWN.value
    channel_statuses: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "company_name": self.company_name,
            "qualification_state": self.qualification_state,
            "contactability_status": self.contactability_status,
            "contactability_state": self.contactability_status,
            "contactability_reason": self.contactability_reason,
            "manual_contactable": self.manual_contactable,
            "automated_contactable": self.automated_contactable,
            "compliance_status": self.compliance_status,
            "channel_statuses": self.channel_statuses,
            "channels": {k: v.to_dict() for k, v in self.channels.items()},
            "sendable_channels": self.sendable_channels,
            "email_data": self.email_data,
            "compliance_data": self.compliance_data,
            "entity_data": self.entity_data,
            "is_ready_for_send": self.is_ready_for_send,
            "email_enrichment_data": self.email_enrichment_data,
        }


# ──────────────────────────────────────────────────────────────────────────
# ASSESSOR
# ──────────────────────────────────────────────────────────────────────────

class ContactabilityAssessor:
    """
    Evaluates contactability across all channels without conflating
    business identity with recipient verification, sendability, or qualification.
    
    Strict Invariants:
      1. NEVER alters qualification_state, operational_status, lead_score, or priority.
      2. Strictly separates manual contactability from automated sendability.
      3. Strictly requires numeric recipient IDs for automated Meta messaging (never fabricates).
      4. Distinguishes MX_VALID, NO_MX, and DNS UNKNOWN.
      5. Separates technical contactability from legal compliance.
    """

    @classmethod
    def assess_lead(cls, lead: Dict[str, Any]) -> LeadContactabilityAssessment:
        lead_id = lead.get("lead_id", "")
        company_name = lead.get("company_name", "")
        # Preserve qualification_state exactly as provided
        qual_state = lead.get("qualification_state", "UNKNOWN")

        # Resolve Country Adapter Bundle
        lead_country = lead.get("country") or lead.get("target_country") or "United Kingdom"
        country_adapter = get_country_adapter(lead_country)

        # ── 1. Business Registry Entity Verification (Localized Adapter) ──
        entity_rec = country_adapter.business_registry.verify_entity(lead)
        entity_data = entity_rec.to_dict() if hasattr(entity_rec, "to_dict") else entity_rec

        # ── 2. Deep Email Discovery & Verification ──
        email_enrichment = EmailEnricher.discover_emails_for_lead(lead)
        email_cand = email_enrichment.primary_email
        email_data = email_cand.to_dict() if email_cand else None
        email_enrichment_data = email_enrichment.to_dict()

        # ── 3. Compliance Evaluation for Email (Localized Adapter) ──
        compliance_rec = country_adapter.compliance.evaluate_marketing_email(
            lead=lead,
            email=email_cand.email if email_cand else "",
            entity_record=entity_rec
        )
        compliance_data = compliance_rec.to_dict() if hasattr(compliance_rec, "to_dict") else compliance_rec

        # ── 4. Instagram Evaluation (Phase 6 Sections 8, 10, 11) ──
        ig_url = str(lead.get("instagram_url", "") or "").strip()
        ig_profile_found = bool(ig_url)
        ig_handle = ""
        if ig_url and "instagram.com/" in ig_url:
            clean = ig_url.split("?")[0].rstrip("/")
            parts = clean.split("instagram.com/")
            if len(parts) > 1:
                candidate_slug = parts[1].split("/")[0]
                if candidate_slug and candidate_slug.lower() not in ["p", "reel", "explore", "popular", "stories", "tv"]:
                    ig_handle = "@" + candidate_slug

        ig_profile_accessible = bool(ig_handle)
        ig_ownership = lead.get("instagram_ownership_status") or (
            "VERIFIED" if lead.get("social_ownership_status") == "VERIFIED" and ig_url and not any(p in ig_url.lower() for p in ["/p/", "/reel/", "/popular/"]) else "UNVERIFIED"
        )
        ig_biz_verified = (ig_ownership == "VERIFIED")

        meta_ig_id = str(lead.get("meta_instagram_id") or lead.get("instagram_recipient_id") or "").strip()
        ig_recipient_verified = bool(meta_ig_id and meta_ig_id.isdigit())
        ig_integration = bool(os.getenv("INSTAGRAM_GRAPH_TOKEN") or os.getenv("META_ACCESS_TOKEN"))

        ig_compliance = ComplianceState.ALLOWED.value if ig_biz_verified else ComplianceState.UNKNOWN.value

        # Manual vs Automated
        ig_manual_contactable = bool(ig_profile_found and ig_profile_accessible and ig_biz_verified)
        ig_automated_contactable = bool(ig_manual_contactable and ig_recipient_verified and ig_integration)
        ig_sendable = ig_automated_contactable

        if not ig_profile_found:
            ig_status = ChannelStatus.UNAVAILABLE.value
            ig_reason = "No Instagram profile found"
        elif not ig_profile_accessible:
            ig_status = ChannelStatus.INVALID.value
            ig_reason = "No valid Instagram handle found (post/reel/topic URL)"
        elif not ig_biz_verified:
            ig_status = ChannelStatus.UNAVAILABLE.value
            ig_reason = "Instagram profile ownership not verified"
        elif not ig_recipient_verified:
            ig_status = ChannelStatus.AVAILABLE.value  # Available for manual outreach
            ig_reason = "RECIPIENT_ID_REQUIRED: Meta Graph API requires numeric IGSID; public handle not directly messageable via API"
        elif not ig_integration:
            ig_status = ChannelStatus.AVAILABLE.value
            ig_reason = "CHANNEL_NOT_CONFIGURED"
        else:
            ig_status = ChannelStatus.AVAILABLE.value
            ig_reason = "Verified IGSID available"

        ig_eval = ChannelContactability(
            channel="Instagram Direct Message",
            business_identity_verified=ig_biz_verified,
            recipient=meta_ig_id or ig_handle or "NONE",
            recipient_verified=ig_recipient_verified,
            recipient_confidence=RecipientConfidence.HIGH.value if ig_recipient_verified else (RecipientConfidence.MEDIUM.value if ig_manual_contactable else RecipientConfidence.LOW.value),
            send_integration_available=ig_integration,
            compliance_status=ig_compliance,
            sendable=ig_sendable,
            reason=ig_reason,
            status=ig_status,
            manual_contactable=ig_manual_contactable,
            automated_contactable=ig_automated_contactable,
            profile_found=ig_profile_found,
            profile_accessible=ig_profile_accessible,
            business_owned=ig_biz_verified,
            recipient_id_available=ig_recipient_verified,
            details={
                "url": ig_url,
                "handle": ig_handle,
                "recipient_id": meta_ig_id,
                "recipient_id_available": ig_recipient_verified,
                "sendable": ig_sendable,
            }
        )

        # ── 5. Facebook Evaluation (Phase 6 Sections 9, 10, 11) ──
        fb_url = str(lead.get("facebook_url", "") or "").strip()
        fb_page_found = bool(fb_url)
        fb_slug = ""
        if fb_url and "facebook.com/" in fb_url:
            clean = fb_url.split("?")[0].rstrip("/")
            parts = clean.split("facebook.com/")
            if len(parts) > 1:
                candidate_slug = parts[1].split("/")[0]
                if candidate_slug and candidate_slug.lower() not in ["p", "reel", "events", "groups", "share", "watch"]:
                    fb_slug = candidate_slug

        fb_page_accessible = bool(fb_slug)
        fb_ownership = lead.get("facebook_ownership_status") or (
            "VERIFIED" if lead.get("social_ownership_status") == "VERIFIED" and fb_url and "facebook.com/" in fb_url else "UNVERIFIED"
        )
        fb_biz_verified = (fb_ownership == "VERIFIED")

        meta_fb_id = str(lead.get("meta_facebook_id") or lead.get("facebook_recipient_id") or "").strip()
        fb_recipient_verified = bool(meta_fb_id and meta_fb_id.isdigit())
        fb_integration = bool(os.getenv("META_PAGE_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN"))

        fb_compliance = ComplianceState.ALLOWED.value if fb_biz_verified else ComplianceState.UNKNOWN.value

        # Manual vs Automated
        fb_manual_contactable = bool(fb_page_found and fb_page_accessible and fb_biz_verified)
        fb_automated_contactable = bool(fb_manual_contactable and fb_recipient_verified and fb_integration)
        fb_sendable = fb_automated_contactable

        if not fb_page_found:
            fb_status = ChannelStatus.UNAVAILABLE.value
            fb_reason = "No Facebook page found"
        elif not fb_page_accessible:
            fb_status = ChannelStatus.INVALID.value
            fb_reason = "No valid Facebook page URL found"
        elif not fb_biz_verified:
            fb_status = ChannelStatus.UNAVAILABLE.value
            fb_reason = "Facebook profile ownership not verified"
        elif not fb_recipient_verified:
            fb_status = ChannelStatus.AVAILABLE.value  # Available for manual outreach
            fb_reason = "RECIPIENT_ID_REQUIRED: Meta Graph API requires numeric PSID; public handle not directly messageable via API"
        elif not fb_integration:
            fb_status = ChannelStatus.AVAILABLE.value
            fb_reason = "CHANNEL_NOT_CONFIGURED"
        else:
            fb_status = ChannelStatus.AVAILABLE.value
            fb_reason = "Verified PSID available"

        fb_eval = ChannelContactability(
            channel="Facebook Messenger",
            business_identity_verified=fb_biz_verified,
            recipient=meta_fb_id or fb_slug or "NONE",
            recipient_verified=fb_recipient_verified,
            recipient_confidence=RecipientConfidence.HIGH.value if fb_recipient_verified else (RecipientConfidence.MEDIUM.value if fb_manual_contactable else RecipientConfidence.LOW.value),
            send_integration_available=fb_integration,
            compliance_status=fb_compliance,
            sendable=fb_sendable,
            reason=fb_reason,
            status=fb_status,
            manual_contactable=fb_manual_contactable,
            automated_contactable=fb_automated_contactable,
            profile_found=fb_page_found,
            profile_accessible=fb_page_accessible,
            business_owned=fb_biz_verified,
            recipient_id_available=fb_recipient_verified,
            details={
                "url": fb_url,
                "slug": fb_slug,
                "recipient_id": meta_fb_id,
                "recipient_id_available": fb_recipient_verified,
                "sendable": fb_sendable,
            }
        )

        # ── 6. Email Evaluation (Phase 6 Sections 4, 5, 6, 7, 12, 17) ──
        email_integration = bool(os.getenv("SMTP_HOST") or os.getenv("SENDGRID_API_KEY"))
        email_found = bool(email_cand and email_cand.email)
        email_recipient = email_cand.email if email_cand else "NONE"
        email_confidence = email_cand.confidence if email_cand else RecipientConfidence.UNKNOWN
        email_domain = getattr(email_cand, "domain", "") if email_cand else ""
        email_mx_status = getattr(email_cand, "mx_status", MXStatus.UNKNOWN.value) if email_cand else MXStatus.UNKNOWN.value
        email_domain_type = getattr(email_cand, "business_domain_type", BusinessDomainType.UNKNOWN.value) if email_cand else BusinessDomainType.UNKNOWN.value
        email_source = email_cand.source if email_cand else "NONE"
        email_source_url = email_cand.source_url if email_cand else ""
        email_discovery_method = getattr(email_cand, "discovery_method", "") if email_cand else ""
        email_retrieved_at = getattr(email_cand, "retrieved_at", "") if email_cand else ""

        # Suppression, bounce, and cooldown
        is_suppressed = SuppressionManager.is_suppressed(lead_id, "Email", email_recipient) if (email_found and email_recipient != "NONE") else False
        in_cooldown, cooldown_until = CooldownManager.is_in_cooldown(lead_id, "Email")
        email_supp_rec = SuppressionManager.get_email_suppression(email_recipient) if (email_found and email_recipient != "NONE") else None
        
        email_is_bounced = bool(
            (email_cand and email_cand.verification_status == EmailVerificationStatus.BOUNCED.value)
            or (email_supp_rec and email_supp_rec.get("status") == EmailVerificationStatus.BOUNCED.value)
            or str(lead.get("email_suppressed", "")).lower() == "true"
            or lead.get("bounce_code")
        )

        raw_comp = compliance_rec.marketing_email_status if compliance_rec else "UNKNOWN"
        if raw_comp in (MarketingEmailStatus.COMPLIANCE_ELIGIBLE, MarketingEmailStatus.ELIGIBLE):
            email_compliance = ComplianceState.ALLOWED.value
        elif raw_comp == MarketingEmailStatus.BLOCKED:
            email_compliance = ComplianceState.BLOCKED.value
        else:
            email_compliance = ComplianceState.UNKNOWN.value

        email_verified = bool(email_cand and email_cand.verification_status == EmailVerificationStatus.VERIFIED.value and not email_is_bounced)

        if not email_found:
            email_status = ChannelStatus.UNAVAILABLE.value
            email_manual = False
            email_auto = False
            email_sendable = False
            email_reason = "No business email discovered from verified sources"
        elif email_is_bounced:
            email_status = ChannelStatus.SUPPRESSED.value
            email_manual = False
            email_auto = False
            email_sendable = False
            b_code = lead.get("bounce_code") or (email_supp_rec.get("bounce_code") if email_supp_rec else "550 5.1.1")
            email_reason = f"BOUNCED: Recipient '{email_recipient}' permanently failed in real delivery ({b_code})"
        elif is_suppressed:
            email_status = ChannelStatus.SUPPRESSED.value
            email_manual = False
            email_auto = False
            email_sendable = False
            email_reason = "Email recipient is on suppression list (Opted-Out / DNC)"
        elif in_cooldown:
            email_status = ChannelStatus.BLOCKED.value
            email_manual = False
            email_auto = False
            email_sendable = False
            email_reason = f"Global contact cooldown active until {cooldown_until}"
        elif email_cand.verification_status == EmailVerificationStatus.INVALID.value or email_mx_status == MXStatus.INVALID.value:
            email_status = ChannelStatus.INVALID.value
            email_manual = False
            email_auto = False
            email_sendable = False
            email_reason = f"Email verification failed ({email_cand.verification_status}): {email_cand.verification_reason}"
        elif email_mx_status == MXStatus.NO_MX.value:
            email_status = ChannelStatus.UNAVAILABLE.value
            email_manual = False
            email_auto = False
            email_sendable = False
            email_reason = f"Email verification failed ({email_cand.verification_status}): {email_cand.verification_reason}"
        elif email_mx_status == MXStatus.UNKNOWN.value:
            # Infrastructure / DNS timeout: we could not check MX. Preserve difference from NO_MX.
            email_status = ChannelStatus.UNKNOWN.value
            email_manual = True  # Address structure valid; manual operator can research/test
            email_auto = False  # Automated send strictly prohibited without verified MX
            email_sendable = False
            email_reason = f"MX_STATUS_UNKNOWN: {email_cand.verification_reason}"
        elif not email_verified:
            email_status = ChannelStatus.UNKNOWN.value
            email_manual = True
            email_auto = False
            email_sendable = False
            email_reason = f"Email verification status ({email_cand.verification_status}): {email_cand.verification_reason}"
        elif not email_integration:
            email_status = ChannelStatus.AVAILABLE.value
            email_manual = True
            email_auto = False
            email_sendable = False
            email_reason = "CHANNEL_NOT_CONFIGURED: No SMTP credentials"
        elif email_compliance == ComplianceState.BLOCKED.value:
            email_status = ChannelStatus.BLOCKED.value
            email_manual = False
            email_auto = False
            email_sendable = False
            notes = getattr(compliance_rec, "compliance_notes", "") or compliance_data.get("compliance_notes", "")
            email_reason = f"Email marketing blocked by compliance: {notes}"
        elif email_compliance == ComplianceState.UNKNOWN.value:
            # Technically available, but compliance requires manual review / is unknown
            email_status = ChannelStatus.AVAILABLE.value
            email_manual = True
            email_auto = False
            email_sendable = False
            notes = getattr(compliance_rec, "compliance_notes", "") or compliance_data.get("compliance_notes", "")
            email_reason = f"COMPLIANCE_MANUAL_REVIEW: {notes}"
        elif email_compliance == ComplianceState.ALLOWED.value:
            email_status = ChannelStatus.AVAILABLE.value
            email_manual = True
            email_auto = True
            email_sendable = True
            corp_name = getattr(entity_rec, "registered_name", "") or getattr(entity_rec, "company_name", "") or entity_data.get("company_name", "") or "Corporate Entity"
            email_reason = f"Verified deliverable email address ({email_recipient}) + active corporate subscriber ({corp_name})"
        else:
            email_status = ChannelStatus.UNKNOWN.value
            email_manual = False
            email_auto = False
            email_sendable = False
            email_reason = f"Compliance status: {raw_comp}"

        email_eval = ChannelContactability(
            channel="Email",
            business_identity_verified=True,
            recipient=email_recipient,
            recipient_verified=email_verified,
            recipient_confidence=RecipientConfidence.LOW.value if email_is_bounced else email_confidence,
            send_integration_available=email_integration,
            compliance_status=raw_comp,
            sendable=email_sendable,
            reason=email_reason,
            status=email_status,
            manual_contactable=email_manual,
            automated_contactable=email_auto,
            profile_found=email_found,
            profile_accessible=email_found and not email_is_bounced,
            business_owned=True,
            recipient_id_available=email_verified,
            details={
                "email": email_recipient,
                "domain": email_domain,
                "mx_status": email_mx_status,
                "business_domain_type": email_domain_type,
                "source": email_source,
                "source_url": email_source_url,
                "discovery_method": email_discovery_method,
                "confidence": email_confidence,
                "retrieved_at": email_retrieved_at,
                "sendable": email_sendable,
                "suppressed": is_suppressed or email_is_bounced,
            }
        )

        channels = {
            "Instagram Direct Message": ig_eval,
            "Facebook Messenger": fb_eval,
            "Email": email_eval,
        }

        channel_statuses = {
            "EMAIL": email_eval.status,
            "INSTAGRAM": ig_eval.status,
            "FACEBOOK": fb_eval.status,
        }

        sendable_channels = [ch_name for ch_name, ev in channels.items() if ev.sendable]
        manual_contactable = any(ch.manual_contactable for ch in channels.values())
        automated_contactable = any(ch.automated_contactable for ch in channels.values())

        # Determine overall lead compliance status
        if any(ch.sendable for ch in channels.values()) or email_compliance == ComplianceState.ALLOWED.value:
            lead_compliance = ComplianceState.ALLOWED.value
        elif all(ch.compliance_status in (ComplianceState.BLOCKED.value, MarketingEmailStatus.BLOCKED) for ch in channels.values() if ch.status != ChannelStatus.UNAVAILABLE.value):
            lead_compliance = ComplianceState.BLOCKED.value
        else:
            lead_compliance = ComplianceState.UNKNOWN.value

        # ── 7. Overall Contactability State ──
        if sendable_channels:
            contactability_status = ContactabilityState.CONTACTABLE.value
            contactability_reason = f"Sendable on: {', '.join(sendable_channels)}"
            is_ready = True
        elif manual_contactable:
            contactability_status = ContactabilityState.PARTIALLY_CONTACTABLE.value
            manual_chs = [ch for ch, ev in channels.items() if ev.manual_contactable]
            contactability_reason = f"Manual contact available on: {', '.join(manual_chs)} (automated sending requires recipient identifiers or compliance clearance)"
            is_ready = False
        elif email_is_bounced:
            contactability_status = ContactabilityState.NOT_CONTACTABLE.value
            contactability_reason = f"Email permanently bounced: {email_reason}"
            is_ready = False
        elif any(ch.status == ChannelStatus.UNKNOWN.value for ch in channels.values() if ch.profile_found):
            contactability_status = ContactabilityState.CONTACTABILITY_UNKNOWN.value
            contactability_reason = "Channel uncertainty: DNS or external channel resolution unavailable"
            is_ready = False
        else:
            contactability_status = ContactabilityState.NOT_CONTACTABLE.value
            reasons = [f"{ch}: {ev.reason}" for ch, ev in channels.items()]
            contactability_reason = "; ".join(reasons)
            is_ready = False

        return LeadContactabilityAssessment(
            lead_id=lead_id,
            company_name=company_name,
            qualification_state=qual_state,
            contactability_status=contactability_status,
            contactability_reason=contactability_reason,
            channels=channels,
            sendable_channels=sendable_channels,
            email_data=email_data,
            compliance_data=compliance_data,
            entity_data=entity_data,
            is_ready_for_send=is_ready,
            email_enrichment_data=email_enrichment_data,
            manual_contactable=manual_contactable,
            automated_contactable=automated_contactable,
            compliance_status=lead_compliance,
            channel_statuses=channel_statuses,
        )

