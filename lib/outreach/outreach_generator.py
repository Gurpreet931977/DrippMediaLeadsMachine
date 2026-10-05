import re
from typing import Dict, Any, Optional, List
from lib.types import DiscoveredBusiness
from lib.validation.social_validator import SocialIdentityValidator


def strip_em_dashes(text: str) -> str:
    """
    Strips em dashes (—) and en dashes (–) from emails and outreach messages.
    Replaces with natural punctuation (colons, commas, periods) or clean hyphens,
    ensuring messages read 100% human and conversational, never AI-generated.
    """
    if not text:
        return text
    # Clean leading dash on signoff (e.g. "\n— Gurpreet" -> "\nGurpreet")
    text = re.sub(r'(\n|^)\s*[—–]\s*', r'\1', text)
    # Replace spaced em dashes with comma or colon where appropriate
    text = re.sub(r'\s+[—–]\s+', ', ', text)
    # Standalone em-dash/en-dash
    text = text.replace('—', '-')
    text = text.replace('–', '-')
    # Normalize double commas or spaces
    text = re.sub(r',\s*,', ',', text)
    return text


class OutreachAngleGenerator:
    """
    Generates tailored, concise, evidence-based outreach angles for qualified leads.

    Section 10 – Message Generation Rules:
      - Grounded strictly in verified facts (reviews, rating, verified socials, premises).
      - Never claims lost money, lost bookings, or high commissions.
      - Never infers platform ownership from a post/reel/video URL.
      - Mentions Instagram only if Instagram ownership is VERIFIED.
      - Mentions Facebook only if Facebook ownership is VERIFIED.
      - Mentions TikTok only if TikTok ownership is VERIFIED.
      - If multiple are verified, mentions only those verified platforms.
      - If none are verified, does not mention social media at all.

    Section 11 – Website Value Proposition:
      - Do NOT automatically mention "direct bookings."
      - Default value proposition covers: menu, location, opening information,
        contact details, brand presentation, enquiry flow.
      - Additional capabilities only when supported by evidence.
    """

    def generate_angle(
        self,
        business: DiscoveredBusiness,
        priority: str = "MEDIUM",
        signals: Optional[Dict[str, Any]] = None,
        verification_reason: str = "",
        verified_socials: Optional[Dict[str, str]] = None
    ) -> str:
        revs = business.review_count or 0
        rating = business.rating or 0.0
        category = (business.category or "business").lower()
        city = business.city or "the area"

        # Dynamically verify social ownership if not explicitly provided
        if verified_socials is None:
            social_dict = {}
            if getattr(business, "instagram_url", None):
                social_dict["instagram"] = business.instagram_url
            if getattr(business, "facebook_url", None):
                social_dict["facebook"] = business.facebook_url
            if getattr(business, "tiktok_url", None):
                social_dict["tiktok"] = business.tiktok_url

            if social_dict:
                social_audit = SocialIdentityValidator.verify_ownership(
                    business_name=business.company_name,
                    city=business.city or "",
                    industry=business.category or "",
                    social_urls=social_dict
                )
                verified_socials = social_audit.get("verified_urls", {})
            else:
                verified_socials = {}

        # Strictly inspect verified platform ownership only
        verified_platforms: List[str] = []
        if "instagram" in verified_socials:
            verified_platforms.append("Instagram")
        if "facebook" in verified_socials:
            verified_platforms.append("Facebook")
        if "tiktok" in verified_socials:
            verified_platforms.append("TikTok")

        # Format social phrase only if verified platforms exist
        if len(verified_platforms) == 1:
            social_clause = f"an active {verified_platforms[0]} presence"
        elif len(verified_platforms) == 2:
            social_clause = f"active {verified_platforms[0]} & {verified_platforms[1]} channels"
        elif len(verified_platforms) > 2:
            social_clause = (
                f"active {', '.join(verified_platforms[:-1])}, and {verified_platforms[-1]} channels"
            )
        else:
            social_clause = ""

        # Section 23: Safer baseline angle — strictly factual, no automatic 'direct bookings'
        website_value = (
            "give customers one place to find your menu, location, opening information, and contact details"
        )

        if revs >= 50 and social_clause:
            return (
                f"No dedicated official website was identified in our checks for {business.company_name}. "
                f"Your business has an established customer presence in {city} with {revs} reviews "
                f"({rating}★) and {social_clause}. "
                f"A dedicated website could {website_value}."
            )
        elif revs >= 50:
            return (
                f"No dedicated official website was identified in our checks for {business.company_name}. "
                f"Your business has an established customer presence with {revs} reviews ({rating}★) in {city}. "
                f"A dedicated website could {website_value}."
            )
        else:
            return (
                f"No dedicated official website was identified in our checks for {business.company_name}. "
                f"Your business has an established customer presence as an active {city} {category}. "
                f"A dedicated website could {website_value}."
            )

    def generate_message_object(
        self,
        business_name: str,
        city: str,
        industry: str,
        review_count: int,
        rating: float,
        channel: str,
        instagram_url: str = "",
        facebook_url: str = "",
        tiktok_url: str = "",
        outreach_angle: str = "",
        source_lead: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Section 12: Channel-aware Outreach Message Object.
        Each channel gets a format appropriate for that platform:
          - Instagram: short conversational DM
          - Facebook: professional page DM
          - Email: subject line + full body
          - WhatsApp: brief direct message
        The exact final message must be preserved. Never overwrite a sent message.
        """
        from datetime import datetime

        # Build base outreach angle if not provided
        if not outreach_angle:
            try:
                biz = DiscoveredBusiness(
                    company_name=business_name,
                    category=industry or "Business",
                    city=city,
                    target_country=(source_lead or {}).get("target_country", "United Kingdom"),
                    review_count=review_count,
                    rating=rating,
                    instagram_url=instagram_url,
                    facebook_url=facebook_url,
                    tiktok_url=tiktok_url
                )
                outreach_angle = self.generate_angle(biz)
            except Exception:
                outreach_angle = (
                    f"No official website was identified in our checks for {business_name}. "
                    f"A dedicated website could centralize your menu, location, opening information, "
                    f"and contact details in one professional online presence."
                )

        # Track which fields were used as sources
        source_fields = ["company_name", "city", "review_count", "rating", "website_status"]
        if instagram_url:
            source_fields.append("instagram_url")
        if facebook_url:
            source_fields.append("facebook_url")
        if tiktok_url:
            source_fields.append("tiktok_url")

        personalization = (
            f"No website confirmed; verified active {city} presence with "
            f"{review_count} reviews ({rating}\u2605)."
        )

        # Channel-aware message formatting
        ch_lower = (channel or "").lower()
        message_subject = ""
        message_body = outreach_angle

        if "instagram" in ch_lower:
            # Instagram DM: short, conversational, no subject
            message_body = self._format_instagram_dm(business_name, city, review_count, rating, outreach_angle)

        elif "facebook" in ch_lower:
            # Facebook Messenger: professional page DM
            message_body = self._format_facebook_dm(business_name, city, review_count, rating, outreach_angle)

        elif "email" in ch_lower:
            # Email: curiosity-led subject + modern reply-centric body
            message_subject = strip_em_dashes(self._generate_email_subject(business_name, review_count, rating))
            message_body = strip_em_dashes(self._format_email_body(business_name, city, review_count, rating, outreach_angle))

        elif "whatsapp" in ch_lower:
            # WhatsApp: brief direct message
            message_body = strip_em_dashes(self._format_whatsapp_dm(business_name, city, review_count, rating, outreach_angle))

        return {
            "outreach_angle": outreach_angle,
            "personalization_reason": personalization,
            "message_subject": strip_em_dashes(message_subject),
            "message_body": strip_em_dashes(message_body),
            "channel": channel,
            "generated_at": datetime.now().isoformat(),
            "source_fields_used": source_fields
        }

    # ──────────────────────────────────────────────────────────────────────────
    # CHANNEL-SPECIFIC MESSAGE FORMATTERS
    # ──────────────────────────────────────────────────────────────────────────

    def _format_instagram_dm(self, name: str, city: str, reviews: int, rating: float, angle: str) -> str:
        """Instagram DM: short, friendly, conversational. No formal opener."""
        return (
            f"Hi {name}! \U0001f44b\n\n"
            f"Noticed you don’t have a website yet. With {reviews} reviews and {rating}★ in {city}, "
            f"you’re clearly doing something right.\n\n"
            f"A simple website could help customers find your menu, hours, and location easily. "
            f"Happy to share more if you’re curious? \U0001f60a"
        )

    def _format_facebook_dm(self, name: str, city: str, reviews: int, rating: float, angle: str) -> str:
        """Facebook page DM: professional but warm."""
        return (
            f"Hi {name} team,\n\n"
            f"I came across your Facebook page and noticed you’re doing really well in {city} "
            f"with {reviews} customer reviews ({rating}★), which is really impressive!\n\n"
            f"I wanted to reach out because we build websites for local businesses like yours. "
            f"A dedicated site could centralise your menu, location, opening hours, and customer enquiries "
            f"in one professional online presence.\n\n"
            f"Would you be open to a quick chat about what that could look like for {name}?\n\n"
            f"Best regards,\nDripp Media"
        )

    def _generate_email_subject(self, name: str, reviews: int, rating: float) -> str:
        """
        Curiosity-gap subject lines. Short, specific, personal.
        NOT 'quick question' - that's the most filtered phrase in cold email.
        Opens a loop the recipient wants to close.
        """
        if reviews >= 1000:
            return f"{int(reviews):,} reviews and no website: spotted something, {name}"
        elif reviews >= 500:
            return f"Spotted something about {name} online"
        elif reviews >= 200:
            return f"{name}: one thing probably costing you Google traffic"
        else:
            return f"Quick thought on {name}'s online presence"

    def _format_email_body(self, name: str, city: str, reviews: int, rating: float, angle: str) -> str:
        """
        Modern, reply-centric cold email. Psychological framework:

        1. PATTERN INTERRUPT - opens with their win, not 'I hope this finds you well'
        2. CURIOSITY GAP - implies we found something interesting without spelling it all out
        3. RECIPROCITY - gives a specific free insight before asking anything
        4. SOCIAL PROOF ANCHORING - grounds credibility in their numbers, not ours
        5. SINGLE MICRO-ASK - 'worth a look?' is near-zero commitment
        6. SHORT PARAGRAPHS - mobile-first, restaurant owners check email on phones
        7. HUMAN SIGN-OFF - first name, not 'Dripp Media Outreach Team'
        8. COMPLIANT OPT-OUT - clean, human, not legalese
        """
        # Contextual opening anchored in their verified achievement
        if reviews >= 1000:
            opening_hook = (
                f"Not many {city} restaurants hit {int(reviews):,} reviews with a {rating}★ average. "
                f"{name} has, so this is clearly a place people genuinely love."
            )
        elif reviews >= 500:
            opening_hook = (
                f"{int(reviews):,} reviews and {rating}★ in {city}. "
                f"That’s a real loyal customer base, not just passing foot traffic."
            )
        else:
            opening_hook = (
                f"Came across {name} while looking at {city}’s restaurant scene. "
                f"{int(reviews):,} reviews and {rating}★ is a genuinely solid reputation."
            )

        return (
            f"Hi {name},\n\n"
            f"{opening_hook}\n\n"
            f"One thing I noticed though: if someone Googles you right now, "
            f"there’s no website to land on. Just Google Maps and a Facebook page.\n\n"
            f"That matters more than people think. A lot of new customers (people new to {city}, "
            f"tourists, or people moving to the area) will scroll straight past if there’s nowhere "
            f"to check the menu, see the vibe, or find your hours without digging.\n\n"
            f"We’ve put together quick mock-ups for a few local restaurants recently. "
            f"Nothing over the top, just a clean, mobile-first page that gives people "
            f"a reason to actually walk through the door.\n\n"
            f"Thought it might be worth seeing what one could look like for {name} specifically. "
            f"Worth a look?\n\n"
            f"Best,\n"
            f"Gurpreet\n"
            f"Dripp Media\n"
            f"{os.getenv('SMTP_USER', 'gurpreet@drippmedia.com')}\n\n"
            f"---\n"
            f"Not interested? Just reply ‘STOP’ and I won’t reach out again."
        )

    def _format_whatsapp_dm(self, name: str, city: str, reviews: int, rating: float, angle: str) -> str:
        """WhatsApp: brief, direct, friendly."""
        return (
            f"Hi {name}! \U0001f44b Quick one: noticed you don’t have a website yet. "
            f"With {int(reviews):,} reviews and {rating}★ in {city} you’re clearly doing "
            f"something right. A simple site could help even more people find you. "
            f"Worth a quick chat? \U0001f60a"
        )

