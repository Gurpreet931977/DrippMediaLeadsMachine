"""
Dripp Media — United Arab Emirates (AE) Country Adapter
======================================================
Implements UAE localization adapters:
  - BusinessRegistry: National Economic Register (NER) / DED Free Zones (NOT_CONFIGURED)
  - Compliance: UAE TDRA Spam Regulations & Federal Decree-Law No. 45/2021
  - PhoneNormalizer: +971 national numbering plan (05x mobile, 02/04/06/07/09 landlines)
  - AddressNormalizer: Emirate-based address & P.O. Box / Makani recognition
  - ReviewSourceAdapter: Google, Tripadvisor, Zomato, Talabat, Facebook
"""

import re
from typing import Dict, Any, List, Optional
from lib.country_adapters.base import (
    BusinessRegistryAdapter,
    ComplianceAdapter,
    PhoneNormalizer,
    AddressNormalizer,
    ReviewSourceAdapter,
    CountryAdapterBundle,
    AdapterStatus,
    BusinessRegistryRecord,
    ComplianceRecord
)

AE_POBOX_REGEX = re.compile(r'\b(?:P\.?O\.?\s*Box|POB)\s*(\d{3,6})\b', re.IGNORECASE)
AE_MAKANI_REGEX = re.compile(r'\b(\d{5}\s*\d{5})\b')
AE_PHONE_REGEX = re.compile(r'^(?:\+971|00971|0)(?:5[024568]|2|3|4|6|7|9)\d{7}$')

EMIRATES = {
    "DUBAI", "ABU DHABI", "SHARJAH", "AJMAN",
    "RAS AL KHAIMAH", "FUJAIRAH", "UMM AL QUWAIN", "AL AIN"
}


class AEBusinessRegistryAdapter(BusinessRegistryAdapter):
    """
    UAE National Economic Register (NER) & Department of Economy and Tourism (DET) verification.
    UAE licensing is split between mainland economic departments and 40+ free zones.
    """
    def __init__(self):
        super().__init__(country_code="AE", status=AdapterStatus.NOT_CONFIGURED)

    def verify_entity(self, lead: Dict[str, Any]) -> BusinessRegistryRecord:
        return BusinessRegistryRecord(
            registry_status=self.status,
            entity_matched=False,
            company_number="",
            company_name=lead.get("name", ""),
            registered_name=lead.get("name", ""),
            company_status="UNVERIFIED_NER_REGISTRY",
            match_status="NO_MATCH",
            match_confidence="UNKNOWN",
            match_reason="UAE NER/DED portal integration requires configured commercial API credentials.",
            subscriber_type="COMMERCIAL_ESTABLISHMENT",
            is_corporate_subscriber=False
        )

    def search_registry(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        return []


class AEComplianceAdapter(ComplianceAdapter):
    """
    UAE Telecommunications and Digital Government Regulatory Authority (TDRA) regulations
    and Federal Decree-Law No. 45 of 2021 regarding Personal Data Protection.
    Direct marketing requires explicit recipient consent or direct commercial relationship,
    mandatory sender identity, and instant opt-out.
    """
    def __init__(self):
        super().__init__(country_code="AE", status=AdapterStatus.SUPPORTED)

    def evaluate_marketing_email(
        self,
        lead: Dict[str, Any],
        email: str,
        entity_record: Optional[Any] = None
    ) -> ComplianceRecord:
        if not email or "@" not in email:
            return ComplianceRecord(
                subscriber_type="UNKNOWN",
                marketing_email_status="BLOCKED",
                lawful_basis_status="INVALID_EMAIL",
                opt_out_status="OPT_OUT_REQUIRED",
                compliance_review_status="FAILED",
                compliance_notes="TDRA regulations require verifiable recipient contact."
            )

        return ComplianceRecord(
            subscriber_type="COMMERCIAL_ESTABLISHMENT",
            marketing_email_status="MANUAL_REVIEW",
            lawful_basis_status="TDRA_CONSENT_OR_ESTABLISHED_RELATION",
            opt_out_status="OPT_OUT_MANDATORY",
            compliance_review_status="MANUAL_REVIEW",
            compliance_notes="UAE TDRA requires prior consent or existing commercial relationship; sender identification and direct opt-out required."
        )


class AEPhoneNormalizer(PhoneNormalizer):
    """UAE numbering plan (+971)."""
    def __init__(self):
        super().__init__(country_code="AE", calling_code="+971", status=AdapterStatus.SUPPORTED)

    def normalize(self, phone: str) -> str:
        clean = re.sub(r'[^\d+]', '', phone)
        if clean.startswith("0") and len(clean) >= 9:
            return "+971" + clean[1:]
        elif clean.startswith("971") and len(clean) >= 11:
            return "+" + clean
        elif clean.startswith("+971"):
            return clean
        return clean

    def is_valid(self, phone: str) -> bool:
        if not phone: return False
        clean = re.sub(r'[\s\(\)-]', '', phone)
        return bool(AE_PHONE_REGEX.match(clean))

    def normalize_for_search(self, phone: str) -> str:
        clean = phone.replace("+971", "0").replace(" ", "").replace("-", "")
        return clean


class AEAddressNormalizer(AddressNormalizer):
    """UAE addresses with Emirate and P.O. Box normalization."""
    def __init__(self):
        super().__init__(country_code="AE", status=AdapterStatus.SUPPORTED)

    def extract_postal_code(self, address: str) -> Optional[str]:
        if not address: return None
        # UAE uses P.O. Box or Makani number rather than standard postal codes
        m = AE_POBOX_REGEX.search(address)
        if m: return f"P.O. Box {m.group(1)}"
        m_makani = AE_MAKANI_REGEX.search(address)
        if m_makani: return f"Makani {m_makani.group(1)}"
        return None

    def is_valid_postal_code(self, code: str) -> bool:
        if not code: return False
        return bool(AE_POBOX_REGEX.search(code) or AE_MAKANI_REGEX.search(code))

    def normalize_address(self, address: str) -> Dict[str, str]:
        parts = [p.strip() for p in address.split(",") if p.strip()]
        street = parts[0] if len(parts) > 1 else ""
        emirate = ""
        for p in parts:
            for em in EMIRATES:
                if em in p.upper():
                    emirate = em.title()
                    break
            if emirate: break

        pobox = self.extract_postal_code(address) or ""
        return {
            "street": street,
            "city": emirate or (parts[1] if len(parts) > 1 else "Dubai"),
            "emirate": emirate,
            "postal_code": pobox,
            "country": "United Arab Emirates"
        }


class AEReviewSourceAdapter(ReviewSourceAdapter):
    """UAE review platforms and directories."""
    def __init__(self):
        super().__init__(country_code="AE", status=AdapterStatus.SUPPORTED)

    def get_preferred_review_sources(self) -> List[str]:
        return ["Google", "Tripadvisor", "Zomato", "Talabat", "Facebook"]

    def get_major_cities(self) -> List[str]:
        return [
            "dubai", "abu dhabi", "sharjah", "ajman", "ras al khaimah",
            "al ain", "fujairah", "umm al quwain"
        ]

    def get_search_queries(
        self,
        business_name: str,
        city: str,
        street: str = "",
        postcode: str = ""
    ) -> List[str]:
        q = [
            f'"{business_name}" "{city}" reviews',
            f'"{business_name}" "{city}" rating reviews',
            f'"{business_name}" "{city}" Google reviews',
            f'"{business_name}" "{city}" Tripadvisor',
            f'"{business_name}" "{city}" UAE reviews'
        ]
        if street:
            q.append(f'"{business_name}" "{street}" reviews')
        return q


def get_ae_adapter_bundle() -> CountryAdapterBundle:
    return CountryAdapterBundle(
        country_code="AE",
        country_name="United Arab Emirates",
        business_registry=AEBusinessRegistryAdapter(),
        compliance=AEComplianceAdapter(),
        phone_normalizer=AEPhoneNormalizer(),
        address_normalizer=AEAddressNormalizer(),
        review_sources=AEReviewSourceAdapter()
    )
