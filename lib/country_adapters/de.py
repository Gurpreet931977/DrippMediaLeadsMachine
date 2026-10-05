"""
Dripp Media — Germany (DE) Country Adapter
==========================================
Implements German localization adapters:
  - BusinessRegistry: Handelsregister commercial register (NOT_CONFIGURED)
  - Compliance: German UWG § 7 Abs. 2 Nr. 3 & DSGVO (GDPR) strict opt-in regime
  - PhoneNormalizer: +49 national numbering plan
  - AddressNormalizer: 5-digit Postleitzahl (PLZ)
  - ReviewSourceAdapter: Google, ProvenExpert, Golocal, Tripadvisor, Facebook
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

DE_PLZ_REGEX = re.compile(r'\b(\d{5})\b')
DE_PHONE_REGEX = re.compile(r'^(?:\+49|0049|0)[1-9]\d{5,13}$')


class DEBusinessRegistryAdapter(BusinessRegistryAdapter):
    """
    German Handelsregister corporate entity verification.
    Requires official Handelsregister API credentials or state portal integration.
    """
    def __init__(self):
        super().__init__(country_code="DE", status=AdapterStatus.NOT_CONFIGURED)

    def verify_entity(self, lead: Dict[str, Any]) -> BusinessRegistryRecord:
        return BusinessRegistryRecord(
            registry_status=self.status,
            entity_matched=False,
            company_number="",
            company_name=lead.get("name", ""),
            registered_name=lead.get("name", ""),
            company_status="UNVERIFIED_HANDELSREGISTER",
            match_status="NO_MATCH",
            match_confidence="UNKNOWN",
            match_reason="Handelsregister portal lookup requires configured DE registry credentials.",
            subscriber_type="KAUFMANN_COMMERCIAL",
            is_corporate_subscriber=False
        )

    def search_registry(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        return []


class DEComplianceAdapter(ComplianceAdapter):
    """
    German UWG § 7 (Gesetz gegen den unlauteren Wettbewerb) and DSGVO compliance.
    Under German law, electronic direct marketing (cold email) to businesses without
    prior express consent (ausdrückliche Einwilligung) is unlawful (§ 7 Abs. 2 Nr. 3 UWG).
    Status: BLOCKED or MANUAL_REVIEW to prevent regulatory infringement.
    """
    def __init__(self):
        super().__init__(country_code="DE", status=AdapterStatus.SUPPORTED)

    def evaluate_marketing_email(
        self,
        lead: Dict[str, Any],
        email: str,
        entity_record: Optional[Any] = None
    ) -> ComplianceRecord:
        return ComplianceRecord(
            subscriber_type="KAUFMANN_COMMERCIAL",
            marketing_email_status="BLOCKED",
            lawful_basis_status="EXPRESS_CONSENT_REQUIRED_UWG_7",
            opt_out_status="OPT_IN_MANDATORY",
            compliance_review_status="FAILED",
            compliance_notes="German UWG § 7(2) requires prior express consent (Double Opt-In) for B2B direct electronic marketing."
        )


class DEPhoneNormalizer(PhoneNormalizer):
    """German numbering plan (+49)."""
    def __init__(self):
        super().__init__(country_code="DE", calling_code="+49", status=AdapterStatus.SUPPORTED)

    def normalize(self, phone: str) -> str:
        clean = re.sub(r'[^\d+]', '', phone)
        if clean.startswith("0") and len(clean) >= 9:
            return "+49" + clean[1:]
        elif clean.startswith("49") and len(clean) >= 10:
            return "+" + clean
        elif clean.startswith("+49"):
            return clean
        return clean

    def is_valid(self, phone: str) -> bool:
        if not phone: return False
        clean = re.sub(r'[\s\(\)-]', '', phone)
        return bool(DE_PHONE_REGEX.match(clean))

    def normalize_for_search(self, phone: str) -> str:
        clean = phone.replace("+49", "0").replace(" ", "").replace("-", "")
        return clean


class DEAddressNormalizer(AddressNormalizer):
    """German address and 5-digit PLZ (Postleitzahl) normalization."""
    def __init__(self):
        super().__init__(country_code="DE", status=AdapterStatus.SUPPORTED)

    def extract_postal_code(self, address: str) -> Optional[str]:
        if not address: return None
        m = DE_PLZ_REGEX.search(address)
        return m.group(1) if m else None

    def is_valid_postal_code(self, code: str) -> bool:
        if not code: return False
        return bool(DE_PLZ_REGEX.fullmatch(code.strip()))

    def normalize_address(self, address: str) -> Dict[str, str]:
        plz = self.extract_postal_code(address) or ""
        parts = [p.strip() for p in address.split(",") if p.strip()]
        street = parts[0] if len(parts) > 1 else ""
        city = parts[1] if len(parts) > 2 else (parts[0] if parts else "")
        return {
            "street": street,
            "city": city,
            "postal_code": plz,
            "country": "Germany"
        }


class DEReviewSourceAdapter(ReviewSourceAdapter):
    """German review platforms and major metropolitan areas."""
    def __init__(self):
        super().__init__(country_code="DE", status=AdapterStatus.SUPPORTED)

    def get_preferred_review_sources(self) -> List[str]:
        return ["Google", "ProvenExpert", "Golocal", "Tripadvisor", "Facebook"]

    def get_major_cities(self) -> List[str]:
        return [
            "berlin", "hamburg", "munich", "münchen", "cologne", "köln",
            "frankfurt", "stuttgart", "düsseldorf", "leipzig", "dortmund",
            "essen", "bremen", "dresden", "hannover", "nuremberg", "nürnberg"
        ]

    def get_search_queries(
        self,
        business_name: str,
        city: str,
        street: str = "",
        postcode: str = ""
    ) -> List[str]:
        q = [
            f'"{business_name}" "{city}" bewertungen',
            f'"{business_name}" "{city}" erfahrungen',
            f'"{business_name}" "{city}" Google bewertungen',
            f'"{business_name}" "{city}" ProvenExpert',
            f'"{business_name}" "{city}" Golocal'
        ]
        if postcode:
            q.append(f'"{business_name}" "{postcode}" bewertungen')
        if street:
            q.append(f'"{business_name}" "{street}" bewertungen')
        return q


def get_de_adapter_bundle() -> CountryAdapterBundle:
    return CountryAdapterBundle(
        country_code="DE",
        country_name="Germany",
        business_registry=DEBusinessRegistryAdapter(),
        compliance=DEComplianceAdapter(),
        phone_normalizer=DEPhoneNormalizer(),
        address_normalizer=DEAddressNormalizer(),
        review_sources=DEReviewSourceAdapter()
    )
