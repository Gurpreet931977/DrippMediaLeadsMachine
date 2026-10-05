"""
Dripp Media — United States (US) Country Adapter
=================================================
Implements US localization adapters:
  - BusinessRegistry: State-level Secretary of State / OpenCorporates (NOT_CONFIGURED)
  - Compliance: US CAN-SPAM Act (15 U.S.C. 7704) opt-out regime
  - PhoneNormalizer: NANP +1 format (10-digit NPA-NXX-XXXX)
  - AddressNormalizer: 5-digit ZIP / ZIP+4 and 50 State abbreviations
  - ReviewSourceAdapter: Yelp, Google, BBB, Tripadvisor, Facebook
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

US_ZIP_REGEX = re.compile(r'\b(\d{5})(?:-\d{4})?\b')
US_PHONE_REGEX = re.compile(r'^(?:\+1|1)?[2-9]\d{2}[2-9]\d{6}$')

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"
}


class USBusinessRegistryAdapter(BusinessRegistryAdapter):
    """
    US corporate entity verification.
    US business registries operate at the individual 50 state levels.
    Without a configured federal aggregator API, returns NOT_CONFIGURED.
    """
    def __init__(self):
        super().__init__(country_code="US", status=AdapterStatus.NOT_CONFIGURED)

    def verify_entity(self, lead: Dict[str, Any]) -> BusinessRegistryRecord:
        return BusinessRegistryRecord(
            registry_status=self.status,
            entity_matched=False,
            company_number="",
            company_name=lead.get("name", ""),
            registered_name=lead.get("name", ""),
            company_status="UNVERIFIED_STATE_REGISTRY",
            match_status="NO_MATCH",
            match_confidence="UNKNOWN",
            match_reason="US state-level business registry lookup requires configured state API key.",
            subscriber_type="COMMERCIAL_RECIPIENT",
            is_corporate_subscriber=False
        )

    def search_registry(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        return []


class USComplianceAdapter(ComplianceAdapter):
    """
    US CAN-SPAM Act compliance evaluation.
    CAN-SPAM is an opt-out regime: commercial emails are permissible without prior consent
    provided they offer clear opt-out, accurate headers, and sender physical postal address.
    """
    def __init__(self):
        super().__init__(country_code="US", status=AdapterStatus.SUPPORTED)

    def evaluate_marketing_email(
        self,
        lead: Dict[str, Any],
        email: str,
        entity_record: Optional[Any] = None
    ) -> ComplianceRecord:
        has_address = bool(lead.get("address") or lead.get("city"))
        
        if not email or "@" not in email:
            return ComplianceRecord(
                subscriber_type="UNKNOWN",
                marketing_email_status="BLOCKED",
                lawful_basis_status="INVALID_EMAIL",
                opt_out_status="OPT_OUT_REQUIRED",
                compliance_review_status="FAILED",
                compliance_notes="CAN-SPAM requires a valid recipient address."
            )

        return ComplianceRecord(
            subscriber_type="COMMERCIAL_RECIPIENT",
            marketing_email_status="COMPLIANCE_ELIGIBLE",
            lawful_basis_status="CAN_SPAM_COMMERCIAL_OPT_OUT",
            opt_out_status="OPT_OUT_REQUIRED",
            compliance_review_status="PASSED" if has_address else "CONDITIONAL_ON_PHYSICAL_ADDRESS",
            compliance_notes="CAN-SPAM compliant: must include sender physical address and functional unsubscribe mechanism."
        )


class USPhoneNormalizer(PhoneNormalizer):
    """US North American Numbering Plan (+1)."""
    def __init__(self):
        super().__init__(country_code="US", calling_code="+1", status=AdapterStatus.SUPPORTED)

    def normalize(self, phone: str) -> str:
        digits = re.sub(r'\D', '', phone)
        if len(digits) == 10 and digits[0] in '23456789':
            return "+1" + digits
        elif len(digits) == 11 and digits.startswith("1") and digits[1] in '23456789':
            return "+" + digits
        return phone.strip()

    def is_valid(self, phone: str) -> bool:
        if not phone: return False
        digits = re.sub(r'\D', '', phone)
        return bool(US_PHONE_REGEX.match(digits))

    def normalize_for_search(self, phone: str) -> str:
        digits = re.sub(r'\D', '', phone)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return digits


class USAddressNormalizer(AddressNormalizer):
    """US address and 5-digit ZIP code normalization."""
    def __init__(self):
        super().__init__(country_code="US", status=AdapterStatus.SUPPORTED)

    def extract_postal_code(self, address: str) -> Optional[str]:
        if not address: return None
        m = US_ZIP_REGEX.search(address)
        return m.group(1) if m else None

    def is_valid_postal_code(self, code: str) -> bool:
        if not code: return False
        return bool(US_ZIP_REGEX.fullmatch(code.strip()))

    def normalize_address(self, address: str) -> Dict[str, str]:
        zip_code = self.extract_postal_code(address) or ""
        parts = [p.strip() for p in address.split(",") if p.strip()]
        street = parts[0] if len(parts) > 1 else ""
        city = parts[1] if len(parts) > 2 else (parts[0] if parts else "")
        state = ""
        for p in parts:
            p_clean = p.strip().upper()
            if p_clean in US_STATES:
                state = p_clean
                break
        return {
            "street": street,
            "city": city,
            "state": state,
            "postal_code": zip_code,
            "country": "United States"
        }


class USReviewSourceAdapter(ReviewSourceAdapter):
    """US review platforms and metropolitan directories."""
    def __init__(self):
        super().__init__(country_code="US", status=AdapterStatus.SUPPORTED)

    def get_preferred_review_sources(self) -> List[str]:
        return ["Yelp", "Google", "BBB", "Tripadvisor", "Facebook"]

    def get_major_cities(self) -> List[str]:
        return [
            "new york", "los angeles", "chicago", "houston", "phoenix",
            "philadelphia", "san antonio", "san diego", "dallas", "austin",
            "san jose", "san francisco", "miami", "seattle", "boston",
            "denver", "atlanta", "las vegas", "portland", "detroit"
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
            f'"{business_name}" "{city}" Yelp',
            f'"{business_name}" "{city}" Google reviews',
            f'"{business_name}" "{city}" BBB'
        ]
        if postcode:
            q.append(f'"{business_name}" "{postcode}" reviews')
        if street:
            q.append(f'"{business_name}" "{street}" reviews')
        return q


def get_us_adapter_bundle() -> CountryAdapterBundle:
    return CountryAdapterBundle(
        country_code="US",
        country_name="United States",
        business_registry=USBusinessRegistryAdapter(),
        compliance=USComplianceAdapter(),
        phone_normalizer=USPhoneNormalizer(),
        address_normalizer=USAddressNormalizer(),
        review_sources=USReviewSourceAdapter()
    )
