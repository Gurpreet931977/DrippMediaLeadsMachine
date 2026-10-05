"""
Dripp Media — Country Adapters Base Architecture & Interfaces
============================================================
Defines the standard contracts for national localization without false universality:
  - BusinessRegistryAdapter (e.g. Companies House in UK, Handelsregister in DE)
  - ComplianceAdapter (e.g. PECR in UK, CAN-SPAM in US, UWG/DSGVO in DE, TRA in UAE)
  - PhoneNormalizer (Dial codes, local formats, search normalization)
  - AddressNormalizer (Postal codes, states/provinces, street parsing)
  - ReviewSourceAdapter (Local platforms, preferred directories, major cities)

Status Codes:
  - SUPPORTED: Provider and active logic exists and functions.
  - NOT_CONFIGURED: Architecture is supported but required API key/server is not provided.
  - UNAVAILABLE: No official registry or automated data source exists for this jurisdiction.
"""

from abc import ABC, abstractmethod
from enum import Enum
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, List, Optional, Tuple


class AdapterStatus(str, Enum):
    SUPPORTED      = "SUPPORTED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAVAILABLE    = "UNAVAILABLE"


@dataclass
class BusinessRegistryRecord:
    registry_status: str = AdapterStatus.UNAVAILABLE.value
    entity_matched: bool = False
    company_number: str = ""
    company_name: str = ""
    registered_name: str = ""
    companies_house_number: str = ""
    company_status: str = "UNAVAILABLE"
    match_status: str = "NO_MATCH"
    match_confidence: str = "UNKNOWN"
    match_reason: str = ""
    subscriber_type: str = "UNKNOWN"
    is_corporate_subscriber: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if not d.get("registered_name"):
            d["registered_name"] = self.company_name
        return d

    def __getitem__(self, item):
        return getattr(self, item)


@dataclass
class ComplianceRecord:
    subscriber_type: str = "UNKNOWN"
    marketing_email_status: str = "MANUAL_REVIEW"
    lawful_basis_status: str = "PENDING_REVIEW"
    opt_out_status: str = "OPT_OUT_REQUIRED"
    compliance_review_status: str = "PENDING_REVIEW"
    compliance_notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __getitem__(self, item):
        return getattr(self, item)


# ──────────────────────────────────────────────────────────────────────────
# 1. BUSINESS REGISTRY ADAPTER INTERFACE
# ──────────────────────────────────────────────────────────────────────────

class BusinessRegistryAdapter(ABC):
    """
    Interface for national corporate and commercial business registries.
    Must gracefully return NOT_CONFIGURED or UNAVAILABLE rather than failing.
    """
    def __init__(self, country_code: str, status: AdapterStatus = AdapterStatus.UNAVAILABLE):
        self.country_code = country_code.upper()
        self.status = status.value

    @abstractmethod
    def verify_entity(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        """
        Attempts to verify legal business entity registration.
        Returns standardized entity verification dictionary:
          {
            "registry_status": str,
            "entity_matched": bool,
            "company_number": str,
            "company_name": str,
            "company_status": str,
            "match_reason": str,
            "subscriber_type": str
          }
        """
        pass

    @abstractmethod
    def search_registry(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        """Searches national registry by company name or number."""
        pass


# ──────────────────────────────────────────────────────────────────────────
# 2. COMPLIANCE ADAPTER INTERFACE
# ──────────────────────────────────────────────────────────────────────────

class ComplianceAdapter(ABC):
    """
    Interface for national electronic direct marketing compliance regulations.
    (e.g. UK PECR, US CAN-SPAM, German UWG/DSGVO, UAE TRA).
    """
    def __init__(self, country_code: str, status: AdapterStatus = AdapterStatus.SUPPORTED):
        self.country_code = country_code.upper()
        self.status = status.value

    @abstractmethod
    def evaluate_marketing_email(
        self,
        lead: Dict[str, Any],
        email: str,
        entity_record: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Evaluates whether sending a marketing email complies with national laws.
        Returns standardized compliance dictionary:
          {
            "subscriber_type": str,
            "marketing_email_status": str, # COMPLIANCE_ELIGIBLE, MANUAL_REVIEW, BLOCKED
            "lawful_basis_status": str,
            "opt_out_status": str,
            "compliance_review_status": str,
            "compliance_notes": str
          }
        """
        pass


# ──────────────────────────────────────────────────────────────────────────
# 3. PHONE NORMALIZER INTERFACE
# ──────────────────────────────────────────────────────────────────────────

class PhoneNormalizer(ABC):
    """
    Interface for country-specific telephone numbering plans and normalization.
    """
    def __init__(self, country_code: str, calling_code: str, status: AdapterStatus = AdapterStatus.SUPPORTED):
        self.country_code = country_code.upper()
        self.calling_code = calling_code
        self.status = status.value

    @abstractmethod
    def normalize(self, phone: str) -> str:
        """Standardizes phone number format into clean national or international representation."""
        pass

    @abstractmethod
    def is_valid(self, phone: str) -> bool:
        """Validates if string represents a plausible telephone number in this jurisdiction."""
        pass

    @abstractmethod
    def normalize_for_search(self, phone: str) -> str:
        """Formats phone number for public search engine queries (e.g. stripping calling code or spaces)."""
        pass


# ──────────────────────────────────────────────────────────────────────────
# 4. ADDRESS NORMALIZER INTERFACE
# ──────────────────────────────────────────────────────────────────────────

class AddressNormalizer(ABC):
    """
    Interface for country-specific address formatting, postal code extraction,
    and state/province parsing.
    """
    def __init__(self, country_code: str, status: AdapterStatus = AdapterStatus.SUPPORTED):
        self.country_code = country_code.upper()
        self.status = status.value

    @abstractmethod
    def extract_postal_code(self, address: str) -> Optional[str]:
        """Extracts national postal/ZIP code from address string."""
        pass

    @abstractmethod
    def is_valid_postal_code(self, code: str) -> bool:
        """Validates whether a string conforms to the national postal code format."""
        pass

    @abstractmethod
    def normalize_address(self, address: str) -> Dict[str, str]:
        """Parses address into street, locality, postal_code, region/state components."""
        pass


# ──────────────────────────────────────────────────────────────────────────
# 5. REVIEW SOURCE ADAPTER INTERFACE
# ──────────────────────────────────────────────────────────────────────────

class ReviewSourceAdapter(ABC):
    """
    Interface for national review platforms, directories, and branch isolation.
    """
    def __init__(self, country_code: str, status: AdapterStatus = AdapterStatus.SUPPORTED):
        self.country_code = country_code.upper()
        self.status = status.value

    @abstractmethod
    def get_preferred_review_sources(self) -> List[str]:
        """Returns list of top public review sources prioritized for this country."""
        pass

    @abstractmethod
    def get_major_cities(self) -> List[str]:
        """Returns major metropolitan areas used for wrong-branch collision detection."""
        pass

    @abstractmethod
    def get_search_queries(
        self,
        business_name: str,
        city: str,
        street: str = "",
        postcode: str = ""
    ) -> List[str]:
        """Generates localized search queries for review evidence collection."""
        pass

    def evaluate_location_match(
        self,
        title: str,
        snippet: str,
        target_city: str,
        street: str = "",
        postcode: str = "",
        country: str = ""
    ) -> Tuple[bool, Optional[str]]:
        """
        Country-neutral location evaluation between search result text and target business.
        Returns:
            (is_match: bool, reject_reason: Optional[str])
        """
        import re
        from lib.discovery.geo_provider import GeoProvider

        target_norm = (target_city or "").strip().lower()
        if not target_norm:
            return True, None

        c_name = country or "United Kingdom"
        target_aliases = [a.lower() for a in GeoProvider.get_locality_aliases(target_norm, c_name)]
        if target_norm not in target_aliases:
            target_aliases.append(target_norm)

        combined = f"{title} — {snippet}".lower()
        has_target_location = (
            any(alias in combined for alias in target_aliases if len(alias) >= 2) or
            (street and street.lower() in combined) or
            (postcode and postcode.lower().split()[0] in combined)
        )

        # Check for wrong branch in another major city
        for major_city in self.get_major_cities():
            if major_city in combined and major_city != target_norm and major_city not in target_aliases:
                if not has_target_location:
                    return False, f"WRONG_BRANCH_LOCATION ({major_city.title()})"

        # Extract concrete location following separator in title: e.g. "Name, City -" or "Name | City"
        loc_candidates = []
        m = re.search(r',\s*([A-Za-z\s\'-]{2,25})\s*(?:[-–—|]|\s+(?:restaurant|food|reviews|menu|tripadvisor|facebook|google|\())', title, re.IGNORECASE)
        if m:
            loc_candidates.append(m.group(1).strip().lower())

        m2 = re.search(r'[-–—|]\s*([A-Za-z\s\'-]{2,25})\s*(?:[-–—|]|\s+(?:restaurant|food|reviews|menu|tripadvisor|facebook|google|\())', title, re.IGNORECASE)
        if m2:
            loc_candidates.append(m2.group(1).strip().lower())

        stop_words = {
            "reviews", "review", "restaurant", "restaurants", "menu", "photos", "united kingdom",
            "england", "scotland", "wales", "great britain", "uk", "online", "official",
            "facebook", "tripadvisor", "google", "food", "dining", "bar", "cafe", "takeaway"
        }

        for loc in loc_candidates:
            if loc in stop_words or len(loc) < 3:
                continue
            if loc == target_norm or loc in target_aliases:
                return True, None
            if not has_target_location:
                return False, f"LOCATION_MISMATCH ({loc.title()} != {target_city.title()})"

        return True, None


# ──────────────────────────────────────────────────────────────────────────
# 6. COUNTRY ADAPTER BUNDLE
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class CountryAdapterBundle:
    country_code: str
    country_name: str
    business_registry: BusinessRegistryAdapter
    compliance: ComplianceAdapter
    phone_normalizer: PhoneNormalizer
    address_normalizer: AddressNormalizer
    review_sources: ReviewSourceAdapter

    def to_dict(self) -> Dict[str, Any]:
        return {
            "country_code": self.country_code,
            "country_name": self.country_name,
            "business_registry_status": self.business_registry.status,
            "compliance_status": self.compliance.status,
            "phone_normalizer_status": self.phone_normalizer.status,
            "address_normalizer_status": self.address_normalizer.status,
            "review_sources_status": self.review_sources.status
        }
