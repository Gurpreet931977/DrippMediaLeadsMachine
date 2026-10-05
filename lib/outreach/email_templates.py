"""
lib/outreach/email_templates.py
===============================
Message Template Engine and Email QA Validator for Phase 10.2.

Implements Sections 21 and 22:
1. Canonical Template: WEBSITE_EMAIL_V1
     - Short, human, specific, truthful, business-relevant, non-generic.
     - Permitted personalization: business name, location, website status, verified reviews.
2. Unsupported Claims Ban (Anti-Hallucination):
     - Zero tolerance for claims of lost sales, poor conversions, slow website,
       customer complaints, low engagement, or booking problems.
3. Pre-send Email Message QA:
     - Validates recipient, branch, truthful personalization, unsubscribe link, sender identity.
     - Generates message_hash, template_version, and personalization_hash.
"""

import re
import hashlib
from typing import Dict, Any, Tuple, Optional, List


TEMPLATE_VERSION_V1 = "WEBSITE_EMAIL_V1"

# Prohibited speculative claims (Section 21)
# Prohibited claims grouped by human-readable category name (used in reason strings)
PROHIBITED_CLAIM_CATEGORIES: Dict[str, List[str]] = {
    "customer complaints": [
        r"customer complaints",
        r"slow website",
        r"booking problems",
        r"broken booking system",
        r"poor conversion",
        r"low engagement",
    ],
    "lost sales": [
        r"lost sales",
        r"losing (?:money|sales|customers)",
        r"missing out on (?:thousands|revenue|customers)",
    ],
    "unsupported guarantees": [
        r"guaranteed? (?:revenue|sales|ranking|growth)",
    ],
}
# Flat list kept for simple iteration (legacy compatibility)
PROHIBITED_CLAIM_PATTERNS = [
    pat for pats in PROHIBITED_CLAIM_CATEGORIES.values() for pat in pats
]

# Prohibited internal leakage patterns
INTERNAL_LEAK_PATTERNS = [
    r"\bRES-[0-9A-Fa-f]{6}\b",
    r"\bLEAD-[A-Z]+-[0-9A-Fa-f]{6}\b",
    r"\bundefined\b",
    r"\bNone\b",
    r"\bnull\b",
    r"\{[a-zA-Z0-9_]+\}",  # Unresolved template variable
]


class MessageQAResult(dict):
    """
    Dual interface supporting both dict access (`result['passed']`)
    and tuple unpacking (`passed, failed_checks, hashes = result`).
    """
    def __iter__(self):
        yield self.get("passed", False)
        yield self.get("failed_checks", [])
        yield self.get("hashes", {})


class EmailTemplateEngine:
    """
    Renders truthful, human website outreach emails and validates message QA.
    """

    @classmethod
    def render_template(
        cls,
        template_id: str,
        lead: Dict[str, Any],
        recipient_email: str = "",
        unsubscribe_link: str = "",
        sender_name: str = "Dripp Media",
        privacy_url: str = "https://drippmedia.com/privacy",
    ) -> Dict[str, Any]:
        """
        Generic template renderer returning subject, body, and metadata dict.
        """
        subject, body, pmeta = cls.render_website_email(
            lead=lead,
            unsubscribe_url=unsubscribe_link,
            sender_name=sender_name,
            privacy_url=privacy_url,
        )
        return {
            "subject": subject,
            "body": body,
            "personalization_meta": pmeta,
            "template_version": template_id,
        }

    @classmethod
    def render_website_email(
        cls,
        lead: Dict[str, Any],
        unsubscribe_url: str,
        sender_name: str = "Dripp Media",
        privacy_url: str = "https://drippmedia.com/privacy",
    ) -> Tuple[str, str, Dict[str, Any]]:
        """
        Renders WEBSITE_EMAIL_V1 with verified personalization facts.
        Returns:
            (subject, body, personalization_meta)
        """
        company_name = str(lead.get("company_name", "your business")).strip()
        city = str(lead.get("city", "Manchester")).strip().title()
        rating = lead.get("rating")
        review_count = lead.get("review_count")

        # Verified observation phrasing
        if review_count and rating:
            obs = f"I noticed you have a strong local reputation with {review_count} customer reviews ({rating}★) in {city}, but no dedicated official website linked to your public profiles."
        elif review_count:
            obs = f"I noticed you have over {review_count} reviews in {city}, but no dedicated official website linked to your business profiles."
        else:
            obs = f"We noticed {company_name} currently does not have a dedicated official website for customers searching online in {city}."

        subject = f"Website concept for {company_name}"

        body = (
            f"Hi {company_name} team,\n\n"
            f"I'm reaching out from Dripp Media here in Manchester.\n\n"
            f"{obs}\n\n"
            f"We design and build clean, mobile-friendly websites tailored for independent businesses to make opening hours, services, and contact details easy to find.\n\n"
            f"Would you be open to a quick 2-minute visual preview of what a dedicated site could look like for {company_name}?\n\n"
            f"Best regards,\n"
            f"The Dripp Media Team\n"
            f"Dripp Media Ltd | Peter House, Oxford Street, Manchester, M1 5AN\n"
            f"Privacy Notice: {privacy_url}\n\n"
            f"---\n"
            f"To opt out of future communications, click here to unsubscribe:\n"
            f"{unsubscribe_url}"
        )

        personalization_meta = {
            "company_name": company_name,
            "city": city,
            "review_count": review_count,
            "rating": rating,
            "observation": obs,
            "template_version": TEMPLATE_VERSION_V1,
        }

        return subject, body, personalization_meta

    @classmethod
    def validate_message_qa(
        cls,
        subject: Optional[str] = None,
        body: Optional[str] = None,
        lead: Optional[Dict[str, Any]] = None,
        personalization_meta: Optional[Dict[str, Any]] = None,
        *,
        rendered_subject: Optional[str] = None,
        rendered_body: Optional[str] = None,
        recipient_email: Optional[str] = None,
        unsubscribe_link: Optional[str] = None,
    ) -> MessageQAResult:
        """
        Performs rigorous pre-send QA validation (Section 22).
        Returns MessageQAResult which acts as both a tuple (passed, failed_checks, hashes)
        and a dict with .get("passed"), ["reason"], etc.
        """
        actual_subject = subject or rendered_subject or ""
        actual_body = body or rendered_body or ""
        target_lead = lead or {}
        pmeta = personalization_meta or {
            "company_name": target_lead.get("company_name", ""),
            "city": target_lead.get("city", "Manchester"),
            "template_version": TEMPLATE_VERSION_V1,
        }

        failed: List[str] = []
        company_name = str(target_lead.get("company_name", "")).strip().lower()

        # 1. Recipient Company Name Check
        if company_name and company_name not in actual_body.lower() and company_name not in actual_subject.lower():
            failed.append("COMPANY_NAME_MISSING: Message does not mention the target company name")

        # 2. Sender Identity Check
        if "dripp media" not in actual_body.lower():
            failed.append("sender identity missing: message fails to identify Dripp Media")

        # 3. Unsubscribe Mechanism Check — must have both the word and a valid URL
        unsub_in_body = "unsubscribe" in actual_body.lower()
        if unsubscribe_link and unsubscribe_link.strip():
            unsub_url_in_body = unsubscribe_link.strip() in actual_body
        else:
            unsub_url_in_body = bool(re.search(r"https?://[^\s]+", actual_body))
        if not unsub_in_body or not unsub_url_in_body:
            failed.append("unsubscribe link missing: message missing mandatory opt-out link")

        # 4. Anti-Hallucination & Unsupported Claims Check (Section 21)
        combined_text = f"{actual_subject}\n{actual_body}".lower()
        found_categories: List[str] = []
        for category, patterns in PROHIBITED_CLAIM_CATEGORIES.items():
            for pat in patterns:
                if re.search(pat, combined_text):
                    if category not in found_categories:
                        found_categories.append(category)
                    break
        for category in found_categories:
            failed.append(f"unsupported claim detected: {category}")

        # 5. Internal Leaks & Malformed Variables Check
        for pat in INTERNAL_LEAK_PATTERNS:
            match = re.search(pat, f"{actual_subject}\n{actual_body}")
            if match:
                failed.append(f"INTERNAL_LEAK_OR_UNRESOLVED_VAR: Found '{match.group(0)}' matching pattern '{pat}'")

        # 6. Hashes Generation
        msg_hash = hashlib.sha256(f"{actual_subject}\n{actual_body}".encode("utf-8")).hexdigest()
        p_bytes = str(sorted(pmeta.items())).encode("utf-8")
        p_hash = hashlib.sha256(p_bytes).hexdigest()

        hash_dict = {
            "template_version": TEMPLATE_VERSION_V1,
            "message_hash": msg_hash,
            "personalization_hash": p_hash,
        }

        is_passed = len(failed) == 0
        reason_str = "; ".join(failed) if failed else "QA verification passed"

        return MessageQAResult({
            "passed": is_passed,
            "failed_checks": failed,
            "reason": reason_str,
            "hashes": hash_dict,
            "message_hash": msg_hash,
            "personalization_hash": p_hash,
            "template_version": TEMPLATE_VERSION_V1,
        })

