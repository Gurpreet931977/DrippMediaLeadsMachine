"""
Dripp Media — Generic / International Fallback Country Adapter
=============================================================
Provides safe fallback implementations for jurisdictions without dedicated adapters.
Explicitly avoids false universality:
  - BusinessRegistry returns AdapterStatus.UNAVAILABLE (does not fabricate matches)
  - Compliance evaluates international baseline (valid email + opt-out requirement)
  - Phone normalizes via general digits/E.164 rules
  - Review sources uses standard global engines (Google, Tripadvisor, Facebook)
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


class GenericBusinessRegistryAdapter(BusinessRegistryAdapter):
    """Fallback business registry for jurisdictions without integrated APIs."""
    def __init__(self, country_code: str = "GLOBAL"):
        super().__init__(country_code=country_code, status=AdapterStatus.UNAVAILABLE)

    def verify_entity(self, lead: Dict[str, Any]) -> BusinessRegistryRecord:
        return BusinessRegistryRecord(
            registry_status=self.status,
            entity_matched=False,
            company_number="",
            company_name=lead.get("name", ""),
            registered_name=lead.get("name", ""),
            company_status="UNAVAILABLE",
            match_status="NO_MATCH",
            match_confidence="UNKNOWN",
            match_reason=f"No automated business registry adapter is configured for country '{self.country_code}'.",
            subscriber_type="UNKNOWN",
            is_corporate_subscriber=False
        )

    def search_registry(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        return []


class GenericComplianceAdapter(ComplianceAdapter):
    """Baseline international electronic commercial communications compliance."""
    def __init__(self, country_code: str = "GLOBAL"):
        super().__init__(country_code=country_code, status=AdapterStatus.SUPPORTED)

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
                compliance_notes="Valid email address required for commercial communication."
            )

        return ComplianceRecord(
            subscriber_type="COMMERCIAL_RECIPIENT",
            marketing_email_status="COMPLIANCE_ELIGIBLE",
            lawful_basis_status="INTERNATIONAL_B2B_BASELINE_OPT_OUT",
            opt_out_status="OPT_OUT_REQUIRED",
            compliance_review_status="PASSED",
            compliance_notes="International baseline: verify clear unsubscribe mechanism and sender physical address."
        )


class GenericPhoneNormalizer(PhoneNormalizer):
    """Generic international E.164 phone normalizer."""
    def __init__(self, country_code: str = "GLOBAL", calling_code: str = ""):
        super().__init__(country_code=country_code, calling_code=calling_code, status=AdapterStatus.SUPPORTED)

    def normalize(self, phone: str) -> str:
        clean = re.sub(r'[^\d+]', '', phone)
        if clean.startswith("00"):
            clean = "+" + clean[2:]
        elif self.calling_code and not clean.startswith("+") and not clean.startswith("0"):
            clean = self.calling_code + clean
        return clean

    def is_valid(self, phone: str) -> bool:
        if not phone: return False
        digits = re.sub(r'\D', '', phone)
        return 7 <= len(digits) <= 15

    def normalize_for_search(self, phone: str) -> str:
        return re.sub(r'[^\d]', '', phone)


class GenericAddressNormalizer(AddressNormalizer):
    """Generic address splitter."""
    def __init__(self, country_code: str = "GLOBAL"):
        super().__init__(country_code=country_code, status=AdapterStatus.SUPPORTED)

    def extract_postal_code(self, address: str) -> Optional[str]:
        # Generic alphanumeric token at end or after comma
        if not address: return None
        tokens = [t.strip() for t in address.replace(",", " ").split() if t.strip()]
        for t in reversed(tokens):
            if re.match(r'^[A-Z0-9-]{3,10}$', t, re.IGNORECASE) and any(c.isdigit() for c in t):
                return t
        return None

    def is_valid_postal_code(self, code: str) -> bool:
        if not code: return False
        return bool(re.match(r'^[A-Z0-9\s-]{3,10}$', code.strip(), re.IGNORECASE))

    def normalize_address(self, address: str) -> Dict[str, str]:
        parts = [p.strip() for p in address.split(",") if p.strip()]
        street = parts[0] if len(parts) > 1 else ""
        city = parts[1] if len(parts) > 2 else (parts[0] if parts else "")
        pc = self.extract_postal_code(address) or ""
        return {
            "street": street,
            "city": city,
            "postal_code": pc,
            "country": self.country_code
        }


class GenericReviewSourceAdapter(ReviewSourceAdapter):
    """Global generic review platforms."""
    def __init__(self, country_code: str = "GLOBAL"):
        super().__init__(country_code=country_code, status=AdapterStatus.SUPPORTED)

    def get_preferred_review_sources(self) -> List[str]:
        return ["Google", "Tripadvisor", "Facebook", "Trustpilot"]

    def get_major_cities(self) -> List[str]:
        return []

    def get_search_queries(
        self,
        business_name: str,
        city: str,
        street: str = "",
        postcode: str = ""
    ) -> List[str]:
        q = [
            f'"{business_name}" "{city}" reviews',
            f'"{business_name}" "{city}" ratings',
            f'"{business_name}" "{city}" Google reviews',
            f'"{business_name}" "{city}" Tripadvisor'
        ]
        if street:
            q.append(f'"{business_name}" "{street}" reviews')
        return q


def get_generic_adapter_bundle(country_code: str = "GLOBAL", country_name: str = "Global") -> CountryAdapterBundle:
    return CountryAdapterBundle(
        country_code=country_code.upper(),
        country_name=country_name,
        business_registry=GenericBusinessRegistryAdapter(country_code=country_code),
        compliance=GenericComplianceAdapter(country_code=country_code),
        phone_normalizer=GenericPhoneNormalizer(country_code=country_code),
        address_normalizer=GenericAddressNormalizer(country_code=country_code),
        review_sources=GenericReviewSourceAdapter(country_code=country_code)
    )
