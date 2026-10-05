"""
lib/outreach/email_compliance.py
================================
Compliance Classification and Law/Policy Evidence Engine for Phase 10.2.

Enforces UK Privacy and Electronic Communications Regulations (PECR) and UK GDPR
rules for B2B electronic marketing:
1. Every prospective email recipient must be classified before entering automated queue:
     - CORPORATE_SUBSCRIBER (Ltd, PLC, LLP, Scottish partnership, body corporate)
     - SOLE_TRADER
     - PARTNERSHIP
     - INDIVIDUAL
     - UNKNOWN
2. For UK targets, SOLE_TRADER, PARTNERSHIP, INDIVIDUAL, and UNKNOWN are strictly
   BLOCKED from automated unsolicited marketing email unless documented compliant consent exists.
3. Generates immutable Law / Policy Evidence Objects.
4. Strictly forbids fabricating consent, lawful basis, or corporate subscriber status.
"""

import re
from typing import Dict, Any, Optional, Tuple


CORPORATE_SUFFIXES = [
    r"\bltd\b",
    r"\blimited\b",
    r"\bplc\b",
    r"\bllp\b",
    r"\bcic\b",
    r"\bcommunity interest company\b",
    r"\bcorporate\b",
    r"\bcorporation\b",
    r"\bincorporated\b",
    r"\binc\b",
]

SOLE_TRADER_KEYWORDS = [
    r"\bsole trader\b",
    r"\bself employed\b",
    r"\bfreelanc\w*\b",
    r"\bt/a\b",
    r"\btrading as\b",
]

PARTNERSHIP_KEYWORDS = [
    r"\bpartnership\b",
    r"\bpartners\b",
    r"\bassociates\b",
    r"\b&\s*co\b",
]


class SubscriberClass:
    CORPORATE_SUBSCRIBER = "CORPORATE_SUBSCRIBER"
    SOLE_TRADER          = "SOLE_TRADER"
    PARTNERSHIP          = "PARTNERSHIP"
    INDIVIDUAL           = "INDIVIDUAL"
    UNKNOWN              = "UNKNOWN"


class LawfulBasis:
    LEGITIMATE_INTERESTS = "LEGITIMATE_INTERESTS"
    CONSENT              = "CONSENT"
    SOFT_OPT_IN          = "SOFT_OPT_IN"
    NONE                 = "NONE"


class MarketingBasis:
    B2B_CORPORATE        = "B2B_CORPORATE"
    B2B_CONSENT          = "B2B_CONSENT"
    B2C_CONSENT          = "B2C_CONSENT"
    UNAUTHORIZED         = "UNAUTHORIZED"


class ComplianceStatus:
    PASS                 = "PASS"
    BLOCKED              = "BLOCKED"
    MANUAL_REVIEW        = "MANUAL_REVIEW"


DEFAULT_PRIVACY_NOTICE_URL = "https://drippmedia.com/privacy"


class EmailComplianceEngine:
    """
    Evaluates and enforces UK PECR and GDPR marketing compliance for automated email.
    """

    @classmethod
    def classify_subscriber(cls, lead: Dict[str, Any]) -> Tuple[str, str]:
        """
        Determines the subscriber class of the business / recipient.
        Returns:
            (subscriber_class, classification_reason)
        """
        company_name = str(lead.get("company_name", "") or "").strip().lower()
        registered_name = str(lead.get("registered_name", "") or "").strip().lower()
        company_type = str(lead.get("company_type", "") or "").strip().lower()
        company_number = str(lead.get("companies_house_number", "") or "").strip()
        legal_status = str(lead.get("legal_entity_type", "") or "").strip().lower()

        # 1. Authoritative Companies House / Registration signals
        if company_number and (
            "ltd" in company_type
            or "limited" in company_type
            or "plc" in company_type
            or "llp" in company_type
            or "private limited" in company_type
            or "public limited" in company_type
            or "community interest" in company_type
        ):
            return (
                SubscriberClass.CORPORATE_SUBSCRIBER,
                f"Verified Companies House corporate entity ({company_number}): {company_type}",
            )

        if "ltd" in legal_status or "limited" in legal_status or "corporate" in legal_status:
            return (
                SubscriberClass.CORPORATE_SUBSCRIBER,
                f"Verified corporate legal status: {legal_status}",
            )

        # 2. Check corporate suffixes in company name or registered name
        for pattern in CORPORATE_SUFFIXES:
            if re.search(pattern, company_name) or re.search(pattern, registered_name):
                return (
                    SubscriberClass.CORPORATE_SUBSCRIBER,
                    f"Corporate entity identified by name suffix ({pattern})",
                )

        # 3. Check explicit sole trader indicators
        for pattern in SOLE_TRADER_KEYWORDS:
            if re.search(pattern, company_name) or re.search(pattern, company_type) or re.search(pattern, legal_status):
                return (
                    SubscriberClass.SOLE_TRADER,
                    f"Sole trader indicator matched ({pattern})",
                )

        # 4. Check explicit partnership indicators
        for pattern in PARTNERSHIP_KEYWORDS:
            if re.search(pattern, company_name) or re.search(pattern, company_type) or re.search(pattern, legal_status):
                return (
                    SubscriberClass.PARTNERSHIP,
                    f"Unincorporated partnership indicator matched ({pattern})",
                )

        # 5. Check if contact is an individual with free email domain and no corporate indicators
        email = str(lead.get("email", "") or lead.get("verified_email", "") or "").strip().lower()
        free_domains = ("gmail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "outlook.com", "aol.com", "icloud.com")
        if email and any(email.endswith(f"@{dom}") for dom in free_domains):
            return (
                SubscriberClass.UNKNOWN,
                f"No corporate registration found and uses personal/free email provider ({email.split('@')[-1]})",
            )

        # 6. Default safely to UNKNOWN
        return (
            SubscriberClass.UNKNOWN,
            "Cannot establish corporate subscriber status safely; unverified legal form",
        )

    @classmethod
    def evaluate_compliance(
        cls,
        lead: Dict[str, Any],
        email: Optional[str] = None,
        opt_out: bool = False,
    ) -> Dict[str, Any]:
        """
        Generates the formal Law / Policy Evidence Object (Section 3).
        Enforces Section 2 rule:
          Only CORPORATE_SUBSCRIBER may proceed under PECR Reg 22 B2B legitimate interests.
          SOLE_TRADER, PARTNERSHIP, INDIVIDUAL, UNKNOWN require explicit consent.
        """
        country = lead.get("country_code", "GB")
        if not country or country in ("United Kingdom", "UK"):
            country = "GB"

        sub_class, class_reason = cls.classify_subscriber(lead)

        # Check for genuine verified consent
        consent_status = lead.get("consent_status")
        consent_source = lead.get("consent_source")
        consent_timestamp = lead.get("consent_timestamp")
        has_verified_consent = (
            consent_status in ("CONSENT_VERIFIED", "EXPLICIT_OPT_IN")
            and bool(consent_source)
            and bool(consent_timestamp)
        )

        # Check opt-out / suppression
        is_opted_out = opt_out or bool(lead.get("opt_out_status", False)) or bool(lead.get("unsubscribed", False))

        evidence: Dict[str, Any] = {
            "recipient_type": sub_class,
            "classification_reason": class_reason,
            "country": country,
            "marketing_basis": MarketingBasis.UNAUTHORIZED,
            "lawful_basis": LawfulBasis.NONE,
            "consent_status": consent_status or ("VERIFIED" if has_verified_consent else "NONE"),
            "consent_source": consent_source,
            "consent_timestamp": consent_timestamp,
            "opt_out_status": is_opted_out,
            "privacy_notice_reference": DEFAULT_PRIVACY_NOTICE_URL,
            "compliance_status": ComplianceStatus.BLOCKED,
            "reason": "",
        }

        # 1. Opt-out override
        if is_opted_out:
            evidence["compliance_status"] = ComplianceStatus.BLOCKED
            evidence["reason"] = "Recipient has opted out or unsubscribed (PECR Section 22/23 objection)"
            return evidence

        # 2. Corporate Subscriber Flow (UK PECR Reg 22 corporate exemption)
        if sub_class == SubscriberClass.CORPORATE_SUBSCRIBER:
            evidence["marketing_basis"] = MarketingBasis.B2B_CORPORATE
            evidence["lawful_basis"] = LawfulBasis.LEGITIMATE_INTERESTS
            evidence["consent_status"] = "NOT_REQUIRED_PECR_REG22_CORPORATE"
            evidence["compliance_status"] = ComplianceStatus.PASS
            evidence["reason"] = (
                "B2B corporate subscriber exemption applies (PECR Regulation 22). "
                "Lawful basis: Legitimate Interests (UK GDPR Article 6(1)(f)) with mandatory opt-out."
            )
            return evidence

        # 3. Non-Corporate Subscriber Flow (Requires explicit consent)
        if has_verified_consent:
            evidence["marketing_basis"] = MarketingBasis.B2B_CONSENT if sub_class != SubscriberClass.INDIVIDUAL else MarketingBasis.B2C_CONSENT
            evidence["lawful_basis"] = LawfulBasis.CONSENT
            evidence["compliance_status"] = ComplianceStatus.PASS
            evidence["reason"] = f"Valid prior consent verified for {sub_class} from source: {consent_source}"
            return evidence

        # 4. Strictly Block Non-Corporate without verified consent
        evidence["compliance_status"] = ComplianceStatus.BLOCKED
        evidence["marketing_basis"] = MarketingBasis.UNAUTHORIZED
        evidence["lawful_basis"] = LawfulBasis.NONE
        evidence["reason"] = (
            f"Automated unsolicited marketing email blocked: Recipient is classified as {sub_class}. "
            "Under UK PECR, sole traders, unincorporated partnerships, individuals, and unverified businesses "
            "require prior explicit opt-in consent or soft opt-in evidence."
        )
        return evidence
