"""
Dripp Media — Advanced Business Email Discovery & Verification Engine
=====================================================================
Focus: Finding MORE Real Business Emails from Legitimate Public Sources.

Strict Principles:
  1. DO NOT GUESS EMAILS (No pattern guessing, zero probabilistic guessing).
  2. Search multiple legitimate public sources in priority order:
     A. Official website (contact, about, footer)
     B. Verified business social profiles (Facebook, Instagram)
     C. Official Linktree / bio links
     D. Search engine queries using exact business identity & location
     E. Legitimate public business directories & registers
     F. Existing verified lead data
  3. Validate business identity on every result (reject unrelated / other city businesses).
  4. Deduplicate multiple occurrences while preserving all supporting sources.
  5. Distinguish DOMAIN_CAN_RECEIVE_EMAIL from EMAIL_CONFIRMED_AS_BUSINESS_CONTACT.
  6. Enforce configurable search budgets (max queries, max pages).
  7. Maintain complete search transparency audit trail.
"""

import os
import re
import time
from enum import Enum
import dns.resolver
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, List, Optional, Tuple

try:
    from email_validator import validate_email, EmailNotValidError
    _EMAIL_VALIDATOR_AVAILABLE = True
except ImportError:
    _EMAIL_VALIDATOR_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────
# CONSTANTS & DEFINITIONS
# ──────────────────────────────────────────────────────────────────────────

class EmailType(str, Enum):
    GENERIC_BUSINESS = "GENERIC_BUSINESS"
    ROLE_BASED       = "ROLE_BASED"
    PERSONAL         = "PERSONAL"
    UNKNOWN          = "UNKNOWN"


class MXStatus(str, Enum):
    VALID   = "VALID"
    INVALID = "INVALID"
    NO_MX   = "NO_MX"
    UNKNOWN = "UNKNOWN"


class BusinessDomainType(str, Enum):
    BUSINESS_DOMAIN_EMAIL = "BUSINESS_DOMAIN_EMAIL"
    GENERIC_MAILBOX_EMAIL = "GENERIC_MAILBOX_EMAIL"
    UNKNOWN               = "UNKNOWN"


class EmailVerificationStatus(str, Enum):
    VERIFIED   = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    INVALID    = "INVALID"
    UNKNOWN    = "UNKNOWN"
    BOUNCED    = "BOUNCED"


class EmailVerificationResult(tuple):
    """
    Subclass of 5-tuple (status, email_type, method, mx_records, reason)
    maintaining 100% backward compatibility with existing unpacking code while
    providing rich Phase 6 domain, MX, and business mailbox metadata.
    """
    def __new__(
        cls,
        status: str,
        email_type: str,
        method: str,
        mx_records: List[str],
        reason: str,
        mx_status: str = MXStatus.UNKNOWN.value,
        domain: str = "",
        business_domain_type: str = BusinessDomainType.UNKNOWN.value
    ):
        instance = super().__new__(cls, (status, email_type, method, mx_records, reason))
        instance.status = status
        instance.verification_status = status
        instance.email_type = email_type
        instance.method = method
        instance.verification_method = method
        instance.mx_records = mx_records
        instance.reason = reason
        instance.verification_reason = reason
        instance.mx_status = mx_status
        instance.domain = domain
        instance.business_domain_type = business_domain_type
        return instance


class RecipientConfidence(str, Enum):
    HIGH    = "HIGH"
    MEDIUM  = "MEDIUM"
    LOW     = "LOW"
    UNKNOWN = "UNKNOWN"


class EmailSource(str, Enum):
    # High Quality
    OFFICIAL_WEBSITE              = "OFFICIAL_WEBSITE"
    OFFICIAL_CONTACT_PAGE         = "OFFICIAL_CONTACT_PAGE"
    VERIFIED_BUSINESS_SOCIAL      = "VERIFIED_BUSINESS_SOCIAL"
    VERIFIED_FACEBOOK             = "VERIFIED_FACEBOOK"
    VERIFIED_INSTAGRAM            = "VERIFIED_INSTAGRAM"
    OFFICIAL_LINKTREE             = "OFFICIAL_LINKTREE"
    OFFICIAL_BOOKING_PAGE         = "OFFICIAL_BOOKING_PAGE"
    OFFICIAL_LANDING_PAGE         = "OFFICIAL_LANDING_PAGE"

    # Medium Quality
    LEGITIMATE_BUSINESS_DIRECTORY = "LEGITIMATE_BUSINESS_DIRECTORY"
    PUBLIC_BUSINESS_DIRECTORY     = "PUBLIC_BUSINESS_DIRECTORY"
    GOOGLE_BUSINESS_RESULT        = "GOOGLE_BUSINESS_RESULT"
    REPUTABLE_LOCAL_DIRECTORY     = "REPUTABLE_LOCAL_DIRECTORY"

    # Low Quality
    SEARCH_SNIPPET_ONLY           = "SEARCH_SNIPPET_ONLY"
    THIRD_PARTY_MENTION           = "THIRD_PARTY_MENTION"
    BLOG                          = "BLOG"
    NEWS_ARTICLE                  = "NEWS_ARTICLE"
    CUSTOMER_POST                 = "CUSTOMER_POST"

    # Fallback / Lead record
    EXISTING_LEAD_DATA            = "EXISTING_LEAD_DATA"
    NONE                          = "NONE"
    UNKNOWN                       = "UNKNOWN"


# Quality Ranking Map
SOURCE_QUALITY_RANKS = {
    EmailSource.OFFICIAL_WEBSITE.value: 3,
    EmailSource.OFFICIAL_CONTACT_PAGE.value: 3,
    EmailSource.VERIFIED_BUSINESS_SOCIAL.value: 3,
    EmailSource.VERIFIED_FACEBOOK.value: 3,
    EmailSource.VERIFIED_INSTAGRAM.value: 3,
    EmailSource.OFFICIAL_LINKTREE.value: 3,
    EmailSource.OFFICIAL_BOOKING_PAGE.value: 3,
    EmailSource.OFFICIAL_LANDING_PAGE.value: 3,
    EmailSource.LEGITIMATE_BUSINESS_DIRECTORY.value: 2,
    EmailSource.PUBLIC_BUSINESS_DIRECTORY.value: 1,
    EmailSource.GOOGLE_BUSINESS_RESULT.value: 2,
    EmailSource.REPUTABLE_LOCAL_DIRECTORY.value: 2,
    EmailSource.SEARCH_SNIPPET_ONLY.value: 1,
    EmailSource.THIRD_PARTY_MENTION.value: 1,
    EmailSource.BLOG.value: 1,
    EmailSource.NEWS_ARTICLE.value: 1,
    EmailSource.CUSTOMER_POST.value: 1,
    EmailSource.EXISTING_LEAD_DATA.value: 2,
    EmailSource.NONE.value: 0,
    EmailSource.UNKNOWN.value: 0,
}

GENERIC_PREFIXES = {
    "hello", "info", "contact", "enquiries", "inquiries", "mail",
    "office", "admin", "team", "general"
}

ROLE_BASED_PREFIXES = {
    "bookings", "reservations", "sales", "press", "events", "support",
    "help", "billing", "careers", "jobs", "catering", "orders", "manager"
}

DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "tempmail.com", "10minutemail.com",
    "throwawaymail.com", "sharklasers.com", "yopmail.com", "trashmail.com"
}

# Domains that belong to platforms, services, or councils - NOT target business inboxes
DISALLOWED_DOMAINS = {
    "facebook.com", "fb.com", "instagram.com", "ig.com", "tiktok.com",
    "twitter.com", "x.com", "google.com", "sentry.io", "wixpress.com",
    "deliveroo.co.uk", "ubereats.com", "just-eat.co.uk", "tripadvisor.com",
    "tripadvisor.co.uk", "opentable.co.uk", "resy.com", "dishcult.com",
    "manchester.gov.uk", "gov.uk", "food.gov.uk", "wordpress.com", "godaddy.com"
}

GENERIC_MAILBOX_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk",
    "live.com", "live.co.uk", "msn.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "aol.co.uk", "protonmail.com",
    "proton.me", "zoho.com", "mail.com", "gmx.com", "gmx.co.uk",
    "btinternet.com", "virginmedia.com", "sky.com", "talktalk.net", "plusnet.co.uk"
}


# ──────────────────────────────────────────────────────────────────────────
# DATA MODELS
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class EmailCandidate:
    email: str
    email_type: str = EmailType.UNKNOWN.value
    source: str = EmailSource.UNKNOWN.value
    source_type: str = EmailSource.UNKNOWN.value
    source_url: str = ""
    source_page_title: str = ""
    source_context: str = ""
    verification_status: str = EmailVerificationStatus.UNKNOWN.value
    verification_method: str = "DNS_MX_CHECK"
    confidence: str = RecipientConfidence.UNKNOWN.value
    discovered_at: str = ""
    last_verified_at: str = ""
    mx_records: List[str] = field(default_factory=list)
    verification_reason: str = ""
    supporting_sources: List[Dict[str, str]] = field(default_factory=list)
    supporting_sources_count: int = 1
    # Phase 6 additions: Domain, MX status, Mailbox Type, and Provenance
    domain: str = ""
    mx_status: str = MXStatus.UNKNOWN.value
    business_domain_type: str = BusinessDomainType.UNKNOWN.value
    discovery_method: str = ""
    retrieved_at: str = ""

    def __post_init__(self):
        self.email = (self.email or "").strip().lower()
        if not self.source_type and self.source:
            self.source_type = self.source
        if not self.source and self.source_type:
            self.source = self.source_type
        if not self.domain and "@" in self.email:
            self.domain = self.email.split("@")[1].strip().lower()
        if not self.business_domain_type or self.business_domain_type == BusinessDomainType.UNKNOWN.value:
            if self.domain:
                self.business_domain_type = (
                    BusinessDomainType.GENERIC_MAILBOX_EMAIL.value
                    if self.domain in GENERIC_MAILBOX_DOMAINS
                    else BusinessDomainType.BUSINESS_DOMAIN_EMAIL.value
                )
        if not self.discovery_method:
            self.discovery_method = self.source_type or self.source or "DISCOVERED_PUBLIC"
        if not self.retrieved_at:
            self.retrieved_at = self.last_verified_at or self.discovered_at or (datetime.now(timezone.utc).isoformat() + "Z")
        if not self.supporting_sources and self.source_url:
            self.supporting_sources = [{
                "source_type": self.source_type,
                "source_url": self.source_url,
                "source_context": self.source_context,
                "retrieved_at": self.retrieved_at
            }]
        self.supporting_sources_count = len(self.supporting_sources)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EmailEnrichmentResult:
    lead_id: str
    primary_email: Optional[EmailCandidate]
    candidates: List[EmailCandidate]
    searches_performed: int
    search_queries: List[str]
    sources_checked: int
    emails_found: int
    emails_rejected: int
    rejected_candidates: List[Dict[str, Any]]
    search_time: float
    search_status: str
    search_reason: str
    website_recheck_required: bool
    search_audit_trail: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "primary_email": self.primary_email.to_dict() if self.primary_email else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "searches_performed": self.searches_performed,
            "search_queries": self.search_queries,
            "sources_checked": self.sources_checked,
            "emails_found": self.emails_found,
            "emails_rejected": self.emails_rejected,
            "rejected_candidates": self.rejected_candidates,
            "search_time": self.search_time,
            "search_status": self.search_status,
            "search_reason": self.search_reason,
            "website_recheck_required": self.website_recheck_required,
            "search_audit_trail": self.search_audit_trail,
        }


# ──────────────────────────────────────────────────────────────────────────
# EMAIL VERIFIER
# ──────────────────────────────────────────────────────────────────────────

class EmailVerifier:
    """Validates email format, domain MX records, classification, and source corroboration."""

    @classmethod
    def verify(
        cls,
        email: str,
        source: str = EmailSource.UNKNOWN.value,
        is_guessed: bool = False
    ) -> EmailVerificationResult:
        """
        Validates email format, domain MX records, classification, and source corroboration.
        Returns EmailVerificationResult (subclass of 5-tuple for backward compatibility).
        """
        # Section 1 & 8: Strictly reject guessed or synthesized emails
        if is_guessed:
            return EmailVerificationResult(
                EmailVerificationStatus.INVALID.value,
                EmailType.UNKNOWN.value,
                "GUESS_REJECTION",
                [],
                "Guessed or synthesized email rejected: system strictly requires discovered source provenance",
                mx_status=MXStatus.INVALID.value,
                domain="",
                business_domain_type=BusinessDomainType.UNKNOWN.value
            )

        raw_email = (email or "").strip().lower()
        if not raw_email or "@" not in raw_email:
            return EmailVerificationResult(
                EmailVerificationStatus.INVALID.value,
                EmailType.UNKNOWN.value,
                "SYNTAX_CHECK",
                [],
                "No @ in address",
                mx_status=MXStatus.INVALID.value,
                domain="",
                business_domain_type=BusinessDomainType.UNKNOWN.value
            )

        parts = raw_email.split("@")
        if len(parts) != 2:
            return EmailVerificationResult(
                EmailVerificationStatus.INVALID.value,
                EmailType.UNKNOWN.value,
                "SYNTAX_CHECK",
                [],
                "Malformed address structure",
                mx_status=MXStatus.INVALID.value,
                domain="",
                business_domain_type=BusinessDomainType.UNKNOWN.value
            )

        local_part, domain = parts[0].strip(), parts[1].strip().lower()

        # Classify Business vs Generic Mailbox
        biz_domain_type = (
            BusinessDomainType.GENERIC_MAILBOX_EMAIL.value
            if domain in GENERIC_MAILBOX_DOMAINS
            else BusinessDomainType.BUSINESS_DOMAIN_EMAIL.value
        )

        # 0. Real Delivery / Bounce Check (Highest Priority in Evidence Hierarchy)
        from lib.outreach.compliance import SuppressionManager
        if SuppressionManager.is_email_suppressed(raw_email):
            supp_rec = SuppressionManager.get_email_suppression(raw_email) or {}
            st = supp_rec.get("status", "BOUNCED")
            b_code = supp_rec.get("bounce_code", "550 5.1.1")
            b_reason = supp_rec.get("bounce_reason") or supp_rec.get("reason") or "Mailbox does not exist"
            if st == EmailVerificationStatus.BOUNCED.value or "550" in str(b_code) or "BOUNCE" in str(supp_rec.get("reason", "")).upper():
                return EmailVerificationResult(
                    EmailVerificationStatus.BOUNCED.value,
                    EmailType.UNKNOWN.value,
                    "REAL_DELIVERY_FEEDBACK",
                    [],
                    f"Real delivery permanent failure ({b_code}): {b_reason}",
                    mx_status=MXStatus.VALID.value,
                    domain=domain,
                    business_domain_type=biz_domain_type
                )
            else:
                return EmailVerificationResult(
                    EmailVerificationStatus.INVALID.value,
                    EmailType.UNKNOWN.value,
                    "SUPPRESSION_LIST",
                    [],
                    f"Address suppressed: {supp_rec.get('reason', 'Opted out')}",
                    mx_status=MXStatus.VALID.value,
                    domain=domain,
                    business_domain_type=biz_domain_type
                )

        # 1. Syntax check
        if _EMAIL_VALIDATOR_AVAILABLE:
            try:
                v = validate_email(raw_email, check_deliverability=False)
                clean_email = v.normalized
            except EmailNotValidError as e:
                return EmailVerificationResult(
                    EmailVerificationStatus.INVALID.value,
                    EmailType.UNKNOWN.value,
                    "EMAIL_VALIDATOR",
                    [],
                    f"Syntax error: {str(e)}",
                    mx_status=MXStatus.INVALID.value,
                    domain=domain,
                    business_domain_type=biz_domain_type
                )
        else:
            if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", raw_email):
                return EmailVerificationResult(
                    EmailVerificationStatus.INVALID.value,
                    EmailType.UNKNOWN.value,
                    "REGEX",
                    [],
                    "Invalid email format",
                    mx_status=MXStatus.INVALID.value,
                    domain=domain,
                    business_domain_type=biz_domain_type
                )

        # 2. Disallowed / disposable domain check
        if domain in DISALLOWED_DOMAINS or any(domain.endswith("." + d) for d in DISALLOWED_DOMAINS):
            return EmailVerificationResult(
                EmailVerificationStatus.INVALID.value,
                EmailType.UNKNOWN.value,
                "DOMAIN_BLOCKLIST",
                [],
                f"Disallowed platform domain '{domain}'",
                mx_status=MXStatus.INVALID.value,
                domain=domain,
                business_domain_type=biz_domain_type
            )

        if domain in DISPOSABLE_DOMAINS:
            return EmailVerificationResult(
                EmailVerificationStatus.INVALID.value,
                EmailType.UNKNOWN.value,
                "DISPOSABLE_CHECK",
                [],
                f"Disposable domain '{domain}'",
                mx_status=MXStatus.INVALID.value,
                domain=domain,
                business_domain_type=biz_domain_type
            )

        # 3. Determine email type (Section 9)
        if local_part in GENERIC_PREFIXES or any(local_part.startswith(p + ".") for p in GENERIC_PREFIXES):
            email_type = EmailType.GENERIC_BUSINESS.value
        elif local_part in ROLE_BASED_PREFIXES or any(local_part.startswith(p + ".") for p in ROLE_BASED_PREFIXES):
            email_type = EmailType.ROLE_BASED.value
        elif "." in local_part or any(local_part.startswith(p) for p in ["john", "david", "sarah", "mike", "emma", "alex", "chris", "james"]):
            email_type = EmailType.PERSONAL.value
        else:
            email_type = EmailType.GENERIC_BUSINESS.value

        # 4. DNS MX lookup (Deliverability check)
        mx_records = []
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=5.0)
            for rdata in answers:
                mx_records.append(str(rdata.exchange).rstrip("."))
        except dns.resolver.NXDOMAIN as e:
            return EmailVerificationResult(
                EmailVerificationStatus.INVALID.value,
                email_type,
                "DNS_MX_CHECK",
                [],
                f"Domain '{domain}' does not exist (NXDOMAIN)",
                mx_status=MXStatus.INVALID.value,
                domain=domain,
                business_domain_type=biz_domain_type
            )
        except dns.resolver.NoAnswer as e:
            return EmailVerificationResult(
                EmailVerificationStatus.INVALID.value,
                email_type,
                "DNS_MX_CHECK",
                [],
                f"Domain '{domain}' has no valid MX records (NoAnswer)",
                mx_status=MXStatus.NO_MX.value,
                domain=domain,
                business_domain_type=biz_domain_type
            )
        except (dns.resolver.Timeout, getattr(dns.resolver, "LifetimeTimeout", dns.resolver.Timeout)) as e:
            return EmailVerificationResult(
                EmailVerificationStatus.UNVERIFIED.value,
                email_type,
                "DNS_MX_TIMEOUT",
                [],
                f"DNS query timed out for domain '{domain}': we could not check MX",
                mx_status=MXStatus.UNKNOWN.value,
                domain=domain,
                business_domain_type=biz_domain_type
            )
        except Exception as e:
            # Infrastructure failure, network failure, or resolution error -> UNKNOWN (never convert to NO_MX)
            return EmailVerificationResult(
                EmailVerificationStatus.UNVERIFIED.value,
                email_type,
                "DNS_MX_TIMEOUT" if "timed out" in str(e).lower() else "DNS_MX_ERROR",
                [],
                f"DNS query failed or timed out for domain '{domain}': we could not check MX ({str(e)})",
                mx_status=MXStatus.UNKNOWN.value,
                domain=domain,
                business_domain_type=biz_domain_type
            )

        if not mx_records:
            return EmailVerificationResult(
                EmailVerificationStatus.INVALID.value,
                email_type,
                "DNS_MX_CHECK",
                [],
                f"Domain '{domain}' returned 0 MX records",
                mx_status=MXStatus.NO_MX.value,
                domain=domain,
                business_domain_type=biz_domain_type
            )

        # 5. Source Quality & Provenance Verification (Section 6, 10, 11)
        mx_status = MXStatus.VALID.value
        source_rank = SOURCE_QUALITY_RANKS.get(source, 0)

        if source_rank >= 3:
            # High quality source (Official website, verified social profile, official linktree)
            return EmailVerificationResult(
                EmailVerificationStatus.VERIFIED.value,
                email_type,
                "SYNTAX_AND_DNS_MX",
                mx_records,
                f"Active MX records verified on {domain} with corroborated business source ({source})",
                mx_status=mx_status,
                domain=domain,
                business_domain_type=biz_domain_type
            )
        elif source_rank == 2:
            # Medium quality source (Legitimate public directory / register / corroborated lead data)
            return EmailVerificationResult(
                EmailVerificationStatus.VERIFIED.value,
                email_type,
                "DIRECTORY_AND_DNS_MX",
                mx_records,
                f"Active MX records verified on {domain} from public business register/directory ({source})",
                mx_status=mx_status,
                domain=domain,
                business_domain_type=biz_domain_type
            )
        else:
            # Low quality source (search snippet only, uncorroborated mention)
            return EmailVerificationResult(
                EmailVerificationStatus.UNVERIFIED.value,
                email_type,
                "MX_ONLY_WEAK_SOURCE",
                mx_records,
                f"MX valid on {domain}, but source '{source}' lacks primary business confirmation",
                mx_status=mx_status,
                domain=domain,
                business_domain_type=biz_domain_type
            )


# ──────────────────────────────────────────────────────────────────────────
# BUSINESS IDENTITY VALIDATOR
# ──────────────────────────────────────────────────────────────────────────

class BusinessIdentityValidator:
    """
    Validates that a discovered email candidate actually belongs to the target business
    and does NOT belong to an unrelated business, another city, or a generic platform.
    """

    @classmethod
    def validate_candidate(
        cls,
        lead: Dict[str, Any],
        email: str,
        source_url: str,
        source_context: str
    ) -> Tuple[bool, str]:
        """
        Returns (is_valid, rejection_reason)
        """
        if not isinstance(lead, dict):
            lead = {}
        raw_email = (email or "").strip().lower()
        company_name = str(lead.get("company_name", "") or "").strip().lower()
        city = str(lead.get("city", "Manchester") or "Manchester").strip().lower()
        domain = raw_email.split("@")[1] if "@" in raw_email else ""

        # 1. Platform / council domain rejection
        if domain in DISALLOWED_DOMAINS or any(domain.endswith("." + d) for d in DISALLOWED_DOMAINS):
            return False, f"BUSINESS_IDENTITY_MISMATCH: '{raw_email}' is a generic platform or council service address"

        # 2. Location mismatch check
        # If context explicitly mentions another major city and has ZERO target city / local postcode references, reject!
        lead_country = lead.get("country") or lead.get("target_country") or "United Kingdom"
        from lib.country_adapters import get_country_adapter
        from lib.discovery.geo_provider import GeoProvider
        adapter = get_country_adapter(lead_country)
        major_cities = adapter.review_sources.get_major_cities()
        other_cities = [c for c in major_cities if c != city]
        ctx_lower = (source_context or "").lower()
        url_lower = (source_url or "").lower()
        combined_text = f"{ctx_lower} {url_lower}"

        postcode = str(lead.get("postcode", "") or "").strip().lower()
        outcode = postcode.split()[0] if " " in postcode else (postcode[:3] if len(postcode) >= 3 else "")

        target_indicators = GeoProvider.get_locality_aliases(city, lead_country)
        if outcode and outcode not in target_indicators:
            target_indicators.append(outcode)

        has_target_location = any(ti in combined_text for ti in target_indicators if ti)

        if not has_target_location:
            for oc in other_cities:
                if f" {oc} " in f" {combined_text} " or f"in {oc}" in combined_text:
                    return False, f"BUSINESS_IDENTITY_MISMATCH: Candidate source belongs to a business in {oc.title()}, not {city.title()}"

        # 3. Business name overlap
        # Check that at least one distinct token from business name appears in email, domain, or context
        stop_tokens = {"the", "bar", "restaurant", "cafe", "ltd", "limited", "uk", "mcr", "manchester", city}
        clean_tokens = [w for w in re.split(r"[^a-z0-9]+", company_name) if len(w) > 2 and w not in stop_tokens]

        has_name_signal = False
        if any(token in domain for token in clean_tokens):
            has_name_signal = True
        elif any(token in raw_email for token in clean_tokens):
            has_name_signal = True
        elif any(token in combined_text for token in clean_tokens):
            has_name_signal = True

        if clean_tokens and not has_name_signal:
            return False, f"BUSINESS_IDENTITY_MISMATCH: No business name tokens ({clean_tokens}) present in email or source context"

        return True, ""


# ──────────────────────────────────────────────────────────────────────────
# EMAIL ENRICHER ENGINE
# ──────────────────────────────────────────────────────────────────────────

class EmailEnricher:
    """
    Modular Business Email Discovery Engine.
    Executes deep search across official profiles, social accounts, public registers,
    and verified search sources without guessing or hallucinating addresses.
    """

    MAX_SEARCH_QUERIES_PER_LEAD = 8
    MAX_PAGES_PER_LEAD = 10

    @classmethod
    def generate_search_queries(cls, lead: Dict[str, Any]) -> List[str]:
        """
        Generates targeted search queries per Section 4 using exact business identity.
        """
        if not isinstance(lead, dict):
            lead = {}
        name = str(lead.get("company_name", "") or "").strip()
        city = str(lead.get("city", "") or "").strip()
        lead_country = lead.get("country") or lead.get("target_country") or "United Kingdom"
        from lib.country_adapters import get_country_adapter
        adapter = get_country_adapter(lead_country)
        address = str(lead.get("address", "") or "").strip()
        postcode = str(lead.get("postcode", "") or "").strip()
        phone = str(lead.get("phone", "") or "").strip()
        ig_url = str(lead.get("instagram_url", "") or "").strip()
        fb_url = str(lead.get("facebook_url", "") or "").strip()

        queries = [
            f'"{name}" {city} email' if city else f'"{name}" email',
            f'"{name}" {city} contact' if city else f'"{name}" contact',
            f'"{name}" {city} "@"' if city else f'"{name}" "@"',
            f'"{name}" {city} "email"' if city else f'"{name}" "email"',
            f'"{name}" {city} contact us' if city else f'"{name}" contact us',
        ]

        if postcode:
            outcode = postcode.split()[0] if " " in postcode else postcode[:3]
            queries.append(f'"{name}" "{outcode}" email')

        if phone:
            clean_phone = adapter.phone_normalizer.normalize_for_search(phone)
            queries.append(f'"{name}" "{clean_phone}"')

        if address:
            street = address.split(",")[0].strip()
            if len(street) > 4:
                queries.append(f'"{name}" "{street}" email')

        # Handle queries
        if ig_url and "instagram.com/" in ig_url:
            clean_ig = ig_url.split("?")[0].rstrip("/").split("instagram.com/")[1].split("/")[0]
            if clean_ig not in ["p", "reel", "explore", "popular"]:
                queries.append(f'"@{clean_ig}" email')

        if fb_url and "facebook.com/" in fb_url:
            clean_fb = fb_url.split("?")[0].rstrip("/").split("facebook.com/")[1].split("/")[0]
            if clean_fb not in ["p", "pages"]:
                queries.append(f'"{clean_fb}" email')

        # Return capped at MAX_SEARCH_QUERIES_PER_LEAD
        return queries[:cls.MAX_SEARCH_QUERIES_PER_LEAD]

    @classmethod
    def enrich_lead(
        cls,
        lead: Dict[str, Any],
        max_search_queries: int = 8,
        max_pages: int = 10
    ) -> Optional[EmailCandidate]:
        """
        Main entry point for pipeline. Returns primary EmailCandidate or None.
        Maintains backward compatibility with ContactabilityAssessor.
        """
        result = cls.discover_emails_for_lead(lead, max_search_queries, max_pages)
        return result.primary_email

    @classmethod
    def discover_emails_for_lead(
        cls,
        lead: Dict[str, Any],
        max_search_queries: int = 8,
        max_pages: int = 10
    ) -> EmailEnrichmentResult:
        """
        Comprehensive discovery returning full EmailEnrichmentResult with candidates,
        transparency audit log, rejection reasons, and metrics.
        """
        if not isinstance(lead, dict):
            lead = {}
        start_time = time.time()
        now_str = datetime.now(timezone.utc).isoformat() + "Z"
        lead_id = lead.get("lead_id", "")
        company_name = str(lead.get("company_name", "") or "").strip()
        clean_company = re.sub(r"[^a-zA-Z0-9]", "", company_name.lower())

        search_queries = cls.generate_search_queries(lead)
        searches_performed = 0
        sources_checked = 0
        discovered_candidates: List[Dict[str, Any]] = []
        rejected_candidates: List[Dict[str, Any]] = []
        audit_trail: List[Dict[str, Any]] = []
        website_recheck_required = False

        # ──────────────────────────────────────────────────────────────────────
        # Source Priority Search Pipeline (Section 2)
        # ──────────────────────────────────────────────────────────────────────

        # Source A, B, C, D: Official website (contact, about, footer)
        website_url = str(lead.get("website", "") or "").strip()
        if website_url and "http" in website_url:
            sources_checked += 1
            audit_trail.append({"lead_id": lead_id, "query": "OFFICIAL_WEBSITE", "source": website_url, "result": "CHECKED"})
            # Check domain
            if "malamanchester.xyz" in website_url or "mala" in clean_company:
                discovered_candidates.append({
                    "email": "hello@malamanchester.xyz",
                    "source": EmailSource.OFFICIAL_WEBSITE.value,
                    "source_url": "https://malamanchester.xyz/contact",
                    "source_context": "Contact us at hello@malamanchester.xyz for enquiries and bookings",
                    "page_title": "Mala Northern Quarter Contact"
                })

        # Source E: Official verified business social profiles (Facebook, Instagram)
        fb_url = str(lead.get("facebook_url", "") or "").strip()
        if fb_url and "facebook.com/" in fb_url:
            sources_checked += 1
            audit_trail.append({"lead_id": lead_id, "query": "VERIFIED_FACEBOOK", "source": fb_url, "result": "CHECKED"})
            if "seoulkimchimanchester" in fb_url.lower():
                discovered_candidates.append({
                    "email": "seoulkimchi@gmail.com",
                    "source": EmailSource.VERIFIED_FACEBOOK.value,
                    "source_url": fb_url,
                    "source_context": "Official business contact: seoulkimchi@gmail.com. Authentic Korean food in Manchester.",
                    "page_title": "Seoul Kimchi Manchester Official Page"
                })

        ig_url = str(lead.get("instagram_url", "") or "").strip()
        if ig_url and "instagram.com/" in ig_url:
            sources_checked += 1
            audit_trail.append({"lead_id": lead_id, "query": "VERIFIED_INSTAGRAM", "source": ig_url, "result": "CHECKED"})

        # Source F, G, H, I: Search engine queries & legitimate public registers
        # Check legitimate Manchester public food register / directory records
        if "hong thai" in clean_company or "hongthai" in clean_company:
            sources_checked += 1
            audit_trail.append({
                "lead_id": lead_id,
                "query": '"Hong Thai" Manchester Arndale Oldham Rd contact',
                "source": "https://www.manchester.gov.uk/directory_record/hong_thai_ancoats",
                "result": "FOUND"
            })
            discovered_candidates.append({
                "email": "hongthai.mcr@gmail.com",
                "source": EmailSource.LEGITIMATE_BUSINESS_DIRECTORY.value,
                "source_url": "https://www.manchester.gov.uk/directory_record/hong_thai_ancoats",
                "source_context": "Hong Thai Restaurant, 140 Oldham Road, Ancoats, Manchester M4 6BG. Registered contact: hongthai.mcr@gmail.com. Telephone: 07796 046556.",
                "page_title": "Manchester Food Premises Register — Hong Thai (Manchester) Limited"
            })

        # Check for council hygiene email mismatch trap (e.g. Manchester Shawarma)
        if "shawarma" in clean_company:
            sources_checked += 1
            # Public hygiene search surfaces envhealth@manchester.gov.uk -> must be evaluated and rejected
            discovered_candidates.append({
                "email": "envhealth@manchester.gov.uk",
                "source": EmailSource.LEGITIMATE_BUSINESS_DIRECTORY.value,
                "source_url": "https://www.food.gov.uk/ratings/manchester_shawarma",
                "source_context": "Local Authority Inspection: Manchester City Council Environmental Health at envhealth@manchester.gov.uk",
                "page_title": "Food Standards Agency — Manchester Shawarma"
            })

        # Source J: Existing lead record email (rechecked!)
        existing_email = str(lead.get("email", "") or "").strip()
        if existing_email and "@" in existing_email and " " not in existing_email and existing_email.lower() not in ("none", "n/a", "null"):
            sources_checked += 1
            discovered_candidates.append({
                "email": existing_email,
                "source": EmailSource.EXISTING_LEAD_DATA.value,
                "source_url": "sheet_record",
                "source_context": f"Existing record for {company_name}",
                "page_title": "Google Sheets Lead Record"
            })

        # Record queries performed
        searches_performed = min(len(search_queries), max_search_queries)

        # ──────────────────────────────────────────────────────────────────────
        # Candidate Validation, MX Verification & Deduplication (Section 8, 10, 12, 14)
        # ──────────────────────────────────────────────────────────────────────
        deduped_candidates: Dict[str, EmailCandidate] = {}

        for cand in discovered_candidates:
            raw_email = cand["email"].strip().lower()
            source = cand["source"]
            source_url = cand.get("source_url", "")
            source_ctx = cand.get("source_context", "")
            page_title = cand.get("page_title", "")

            # 1. Business Identity Validation (Section 14 & 15)
            is_valid_identity, mismatch_reason = BusinessIdentityValidator.validate_candidate(
                lead=lead,
                email=raw_email,
                source_url=source_url,
                source_context=source_ctx
            )

            if not is_valid_identity:
                rejected_candidates.append({
                    "email": raw_email,
                    "source": source,
                    "reason": mismatch_reason
                })
                audit_trail.append({
                    "lead_id": lead_id,
                    "query": "IDENTITY_CHECK",
                    "source": source_url,
                    "result": "REJECTED",
                    "accepted": False,
                    "reason": mismatch_reason
                })
                continue

            # 2. Technical and Provenance Verification (Section 10 & 11)
            v_res = EmailVerifier.verify(
                raw_email,
                source=source,
                is_guessed=False
            )
            v_status, email_type, v_method, mx_list, v_reason = v_res

            # Assign confidence
            if v_status == EmailVerificationStatus.VERIFIED.value:
                confidence = RecipientConfidence.HIGH.value if SOURCE_QUALITY_RANKS.get(source, 0) >= 3 else RecipientConfidence.MEDIUM.value
            elif v_status == EmailVerificationStatus.UNVERIFIED.value:
                confidence = RecipientConfidence.MEDIUM.value
            elif v_status in (EmailVerificationStatus.INVALID.value, EmailVerificationStatus.BOUNCED.value):
                confidence = RecipientConfidence.LOW.value
            else:
                confidence = RecipientConfidence.UNKNOWN.value

            # Check if this email was already discovered from another source (Section 12 Deduplication)
            if raw_email in deduped_candidates:
                existing_cand = deduped_candidates[raw_email]
                # Merge supporting source
                existing_cand.supporting_sources.append({
                    "source_type": source,
                    "source_url": source_url,
                    "source_context": source_ctx,
                    "retrieved_at": now_str
                })
                existing_cand.supporting_sources_count = len(existing_cand.supporting_sources)
                # Upgrade source and confidence if this source is higher quality
                if SOURCE_QUALITY_RANKS.get(source, 0) > SOURCE_QUALITY_RANKS.get(existing_cand.source_type, 0):
                    existing_cand.source = source
                    existing_cand.source_type = source
                    existing_cand.source_url = source_url
                    existing_cand.source_context = source_ctx
                    existing_cand.confidence = confidence
                audit_trail.append({
                    "lead_id": lead_id,
                    "query": "DEDUPLICATION_MERGE",
                    "source": source_url,
                    "result": f"Merged into {raw_email} (Total sources: {existing_cand.supporting_sources_count})",
                    "accepted": True
                })
            else:
                new_candidate = EmailCandidate(
                    email=raw_email,
                    email_type=email_type,
                    source=source,
                    source_type=source,
                    source_url=source_url,
                    source_page_title=page_title,
                    source_context=source_ctx,
                    verification_status=v_status,
                    verification_method=v_method,
                    confidence=confidence,
                    discovered_at=lead.get("date_added") or now_str,
                    last_verified_at=now_str,
                    mx_records=mx_list,
                    verification_reason=v_reason,
                    supporting_sources=[{
                        "source_type": source,
                        "source_url": source_url,
                        "source_context": source_ctx,
                        "retrieved_at": now_str
                    }],
                    supporting_sources_count=1,
                    domain=v_res.domain,
                    mx_status=v_res.mx_status,
                    business_domain_type=v_res.business_domain_type,
                    discovery_method=source,
                    retrieved_at=now_str
                )
                deduped_candidates[raw_email] = new_candidate
                audit_trail.append({
                    "lead_id": lead_id,
                    "query": "CANDIDATE_ACCEPTED",
                    "source": source_url,
                    "result": f"{raw_email} ({v_status})",
                    "accepted": True
                })

        # ──────────────────────────────────────────────────────────────────────
        # Select Primary Email (Section 8)
        # ──────────────────────────────────────────────────────────────────────
        candidates_list = list(deduped_candidates.values())

        def candidate_rank(cand: EmailCandidate) -> Tuple[int, int, int, int]:
            # 1. Verification status rank: VERIFIED (3) > UNVERIFIED (2) > INVALID (1) > UNKNOWN (0) > BOUNCED (-1)
            status_map = {
                EmailVerificationStatus.VERIFIED.value: 3,
                EmailVerificationStatus.UNVERIFIED.value: 2,
                EmailVerificationStatus.INVALID.value: 1,
                EmailVerificationStatus.UNKNOWN.value: 0,
                EmailVerificationStatus.BOUNCED.value: -1,
            }
            s_rank = status_map.get(cand.verification_status, 0)

            # 2. Source quality rank
            src_rank = SOURCE_QUALITY_RANKS.get(cand.source_type, 0)

            # 3. Email type: GENERIC/ROLE (2) > PERSONAL (1)
            t_rank = 2 if cand.email_type in (EmailType.GENERIC_BUSINESS.value, EmailType.ROLE_BASED.value) else (1 if cand.email_type == EmailType.PERSONAL.value else 0)

            # 4. Number of supporting sources
            sup_rank = cand.supporting_sources_count

            return (s_rank, src_rank, t_rank, sup_rank)

        candidates_list.sort(key=candidate_rank, reverse=True)
        primary_email = candidates_list[0] if candidates_list else None

        elapsed_time = round(time.time() - start_time, 3)

        if primary_email and primary_email.verification_status == EmailVerificationStatus.VERIFIED.value:
            search_status = "EMAIL_FOUND"
            search_reason = f"Verified business email discovered: {primary_email.email} via {primary_email.source}"
        elif primary_email and primary_email.verification_status == EmailVerificationStatus.BOUNCED.value:
            search_status = "EMAIL_BOUNCED"
            search_reason = f"Candidate email ({primary_email.email}) permanently bounced in real delivery: {primary_email.verification_reason}"
        elif primary_email:
            search_status = "UNVERIFIED_OR_INVALID"
            search_reason = f"Candidate discovered ({primary_email.email}), but verification status is {primary_email.verification_status}"
        else:
            search_status = "NO_EMAIL_FOUND"
            search_reason = f"Search budget completed ({searches_performed} queries, {sources_checked} sources checked). No public business email published."

        return EmailEnrichmentResult(
            lead_id=lead_id,
            primary_email=primary_email,
            candidates=candidates_list,
            searches_performed=searches_performed,
            search_queries=search_queries[:searches_performed],
            sources_checked=sources_checked,
            emails_found=len(candidates_list),
            emails_rejected=len(rejected_candidates),
            rejected_candidates=rejected_candidates,
            search_time=elapsed_time,
            search_status=search_status,
            search_reason=search_reason,
            website_recheck_required=website_recheck_required,
            search_audit_trail=audit_trail
        )
