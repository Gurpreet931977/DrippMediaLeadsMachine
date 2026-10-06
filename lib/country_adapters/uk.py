"""
Dripp Media — United Kingdom (UK) Country Adapter
=================================================
Implements UK localization adapters:
  - BusinessRegistry: Companies House API & web search
  - Compliance: UK PECR / ICO B2B marketing rules
  - PhoneNormalizer: +44 numbering plan (01/02/07/08)
  - AddressNormalizer: UK Postcode alphanumeric patterns
  - ReviewSourceAdapter: Google, Tripadvisor, Trustpilot, Facebook
"""

import re
from typing import Dict, Any, List, Optional, Tuple
from lib.country_adapters.base import (
    BusinessRegistryAdapter,
    ComplianceAdapter,
    PhoneNormalizer,
    AddressNormalizer,
    ReviewSourceAdapter,
    CountryAdapterBundle,
    AdapterStatus
)
from lib.outreach.companies_house import CompaniesHouseVerifier
from lib.outreach.compliance import UKComplianceEvaluator, SubscriberType, MarketingEmailStatus

UK_POSTCODE_REGEX = re.compile(r'\b([A-Z]{1,2}[0-9][0-9A-Z]?\s?[0-9][A-Z]{2})\b', re.IGNORECASE)
UK_PHONE_REGEX = re.compile(r'^(?:\+44|0)(?:\s*\d){9,10}$')


class UKBusinessRegistryAdapter(BusinessRegistryAdapter):
    """Companies House UK corporate entity verification."""
    def __init__(self):
        super().__init__(country_code="GB", status=AdapterStatus.SUPPORTED)

    def verify_entity(self, lead: Dict[str, Any]) -> Any:
        return CompaniesHouseVerifier.verify_entity(lead)

    def search_registry(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        return CompaniesHouseVerifier.search_company(query)


class UKComplianceAdapter(ComplianceAdapter):
    """UK PECR / ICO electronic marketing compliance rules."""
    def __init__(self):
        super().__init__(country_code="GB", status=AdapterStatus.SUPPORTED)

    def evaluate_marketing_email(
        self,
        lead: Dict[str, Any],
        email: str,
        entity_record: Optional[Any] = None
    ) -> Any:
        return UKComplianceEvaluator.evaluate(
            lead=lead,
            email=email,
            entity_record=entity_record
        )


class UKPhoneNormalizer(PhoneNormalizer):
    """UK numbering plan (+44)."""
    def __init__(self):
        super().__init__(country_code="GB", calling_code="+44", status=AdapterStatus.SUPPORTED)

    def normalize(self, phone: str) -> str:
        clean = re.sub(r'[^\d+]', '', phone)
        if clean.startswith("0") and len(clean) >= 10:
            return "+44" + clean[1:]
        elif clean.startswith("44") and len(clean) >= 11:
            return "+" + clean
        elif clean.startswith("+44"):
            return clean
        return clean

    def is_valid(self, phone: str) -> bool:
        if not phone: return False
        clean = re.sub(r'[\s\(\)-]', '', phone)
        return bool(UK_PHONE_REGEX.match(clean))

    def normalize_for_search(self, phone: str) -> str:
        # Standard UK search format: 01xxx or 07xxx without +44 and without spaces
        return phone.replace("+44", "0").replace(" ", "").replace("-", "")


class UKAddressNormalizer(AddressNormalizer):
    """UK addresses and postcode extraction."""
    def __init__(self):
        super().__init__(country_code="GB", status=AdapterStatus.SUPPORTED)

    def extract_postal_code(self, address: str) -> Optional[str]:
        if not address: return None
        m = UK_POSTCODE_REGEX.search(address)
        return m.group(1).upper() if m else None

    def is_valid_postal_code(self, code: str) -> bool:
        if not code: return False
        return bool(UK_POSTCODE_REGEX.fullmatch(code.strip()))

    def normalize_address(self, address: str) -> Dict[str, str]:
        pc = self.extract_postal_code(address) or ""
        parts = [p.strip() for p in address.split(",") if p.strip()]
        street = parts[0] if len(parts) > 1 else ""
        city = parts[1] if len(parts) > 2 else (parts[0] if parts else "")
        return {
            "street": street,
            "city": city,
            "postal_code": pc,
            "country": "United Kingdom"
        }


class UKReviewSourceAdapter(ReviewSourceAdapter):
    """UK review platforms and branch isolation."""
    def __init__(self):
        super().__init__(country_code="GB", status=AdapterStatus.SUPPORTED)

    def get_preferred_review_sources(self) -> List[str]:
        return ["Google", "Tripadvisor", "Trustpilot", "Facebook"]

    def get_major_cities(self) -> List[str]:
        return [
            "manchester", "london", "birmingham", "bristol", "edinburgh",
            "glasgow", "sheffield", "liverpool", "newcastle", "nottingham",
            "cardiff", "belfast", "leicester", "southampton", "oxford", "cambridge"
        ]

    def get_search_queries(
        self,
        business_name: str,
        city: str,
        street: str = "",
        postcode: str = ""
    ) -> List[str]:
        q = [
            f'"{business_name}" "{city}" restaurant',
            f'"{business_name}" "{city}" reviews',
            f'"{business_name}" "{city}" rating reviews',
            f'"{business_name}" "{city}" Tripadvisor',
            f'"{business_name}" "{city}" Google reviews',
            f'"{business_name}" "{city}" Facebook reviews'
        ]
        if postcode:
            q.append(f'"{business_name}" "{postcode}" reviews')
            outcode = postcode.split()[0]
            if outcode != postcode:
                q.append(f'"{business_name}" "{outcode}" reviews')
        if street:
            q.append(f'"{business_name}" "{street}" reviews')
        return q

    def evaluate_location_match(
        self,
        title: str,
        snippet: str,
        target_city: str,
        street: str = "",
        postcode: str = "",
        country: str = "United Kingdom"
    ) -> Tuple[bool, Optional[str]]:
        # 1. Base evaluation (major cities + title concrete locations vs locality aliases)
        is_match, reject_reason = super().evaluate_location_match(
            title=title,
            snippet=snippet,
            target_city=target_city,
            street=street,
            postcode=postcode,
            country=country
        )
        if not is_match:
            return False, reject_reason

        # 2. UK Postcode mismatch check
        combined = f"{title} — {snippet}".upper()
        m_pc = UK_POSTCODE_REGEX.search(combined)
        if m_pc:
            cand_pc = m_pc.group(1).upper()
            cand_outward = cand_pc.split()[0]
            m_cand_area = re.match(r'^[A-Z]{1,2}', cand_outward)
            cand_area = m_cand_area.group(0) if m_cand_area else ""

            target_area = ""
            if postcode:
                m_t_area = re.match(r'^[A-Z]{1,2}', postcode.strip().upper())
                if m_t_area:
                    target_area = m_t_area.group(0)
            elif target_city.lower() == "leeds":
                target_area = "LS"
            elif target_city.lower() == "birmingham":
                target_area = "B"
            elif target_city.lower() == "manchester":
                target_area = "M"

            if target_area and cand_area and cand_area != target_area:
                from lib.discovery.geo_provider import GeoProvider
                aliases = [a.lower() for a in GeoProvider.get_locality_aliases(target_city.lower(), "United Kingdom")]
                comb_lower = combined.lower()
                has_target = (
                    target_city.lower() in comb_lower or
                    (street and street.lower() in comb_lower) or
                    any(a in comb_lower for a in aliases if len(a) >= 3)
                )
                if not has_target:
                    return False, f"LOCATION_MISMATCH (Postcode {cand_pc} != {target_area})"

        return True, None


def get_uk_adapter_bundle() -> CountryAdapterBundle:
    return CountryAdapterBundle(
        country_code="GB",
        country_name="United Kingdom",
        business_registry=UKBusinessRegistryAdapter(),
        compliance=UKComplianceAdapter(),
        phone_normalizer=UKPhoneNormalizer(),
        address_normalizer=UKAddressNormalizer(),
        review_sources=UKReviewSourceAdapter()
    )
