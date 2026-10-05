"""
Dripp Media — Country Adapters Registry & Factory
=================================================
Provides unified lookup for localized country adapters:
  - United Kingdom (GB / UK)
  - United States (US / USA)
  - Germany (DE / DEU)
  - United Arab Emirates (AE / ARE)
  - International Fallback (Generic)

Guarantees 'No False Universality':
  - Returns explicit AdapterStatus.NOT_CONFIGURED or AdapterStatus.UNAVAILABLE
    for jurisdictions lacking dedicated registry or scraper integrations.
"""

from typing import Dict, Optional
from lib.country_adapters.base import (
    AdapterStatus,
    BusinessRegistryAdapter,
    ComplianceAdapter,
    PhoneNormalizer,
    AddressNormalizer,
    ReviewSourceAdapter,
    CountryAdapterBundle
)
from lib.country_adapters.uk import get_uk_adapter_bundle
from lib.country_adapters.us import get_us_adapter_bundle
from lib.country_adapters.de import get_de_adapter_bundle
from lib.country_adapters.ae import get_ae_adapter_bundle
from lib.country_adapters.generic import get_generic_adapter_bundle

_COUNTRY_BUNDLE_CACHE: Dict[str, CountryAdapterBundle] = {}

def get_country_adapter(country_input: Optional[str] = None) -> CountryAdapterBundle:
    """
    Factory function resolving a CountryAdapterBundle from a country name, ISO code, or alias.
    Defaults to UK if explicitly requested, or Generic fallback for unknown countries.
    """
    if not country_input:
        norm = "GLOBAL"
    else:
        norm = country_input.strip().upper()

    # Normalize common representations
    if norm in {"GB", "UK", "UNITED KINGDOM", "ENGLAND", "SCOTLAND", "WALES", "NORTHERN IRELAND"}:
        iso = "GB"
    elif norm in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
        iso = "US"
    elif norm in {"DE", "DEU", "GERMANY", "DEUTSCHLAND"}:
        iso = "DE"
    elif norm in {"AE", "ARE", "UAE", "UNITED ARAB EMIRATES"}:
        iso = "AE"
    else:
        iso = norm

    if iso in _COUNTRY_BUNDLE_CACHE:
        return _COUNTRY_BUNDLE_CACHE[iso]

    if iso == "GB":
        bundle = get_uk_adapter_bundle()
    elif iso == "US":
        bundle = get_us_adapter_bundle()
    elif iso == "DE":
        bundle = get_de_adapter_bundle()
    elif iso == "AE":
        bundle = get_ae_adapter_bundle()
    else:
        bundle = get_generic_adapter_bundle(country_code=iso, country_name=country_input or "Global")

    _COUNTRY_BUNDLE_CACHE[iso] = bundle
    return bundle


__all__ = [
    "AdapterStatus",
    "BusinessRegistryAdapter",
    "ComplianceAdapter",
    "PhoneNormalizer",
    "AddressNormalizer",
    "ReviewSourceAdapter",
    "CountryAdapterBundle",
    "get_country_adapter",
    "get_uk_adapter_bundle",
    "get_us_adapter_bundle",
    "get_de_adapter_bundle",
    "get_ae_adapter_bundle",
    "get_generic_adapter_bundle"
]
