from lib.discovery.base import DiscoveryProvider, DiscoveryMetrics
from lib.discovery.osm import OpenStreetMapProvider
from lib.discovery.foursquare import FoursquareProvider
from lib.discovery.web_search import WebSearchProvider
from lib.discovery.crawler import CrawlEngine
from lib.discovery.hybrid import HybridDiscoveryEngine, DiscoveryMode

from lib.discovery.geo_provider import GeoProvider, get_geo_provider
from lib.discovery.address_normalizer import (
    AddressCompleteness,
    AddressProfile,
    AddressNormalizer,
    extract_uk_postcode,
    haversine_distance_meters
)

def __getattr__(name: str):
    """Lazily load legacy Apify adapter only when explicitly referenced."""
    if name in ("ApifyDiscoveryProvider", "ApifyProvider"):
        try:
            from lib.discovery.apify import ApifyDiscoveryProvider
            return ApifyDiscoveryProvider
        except Exception as e:
            raise AttributeError(f"Apify adapter unavailable: {e}")
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

__all__ = [
    "DiscoveryProvider",
    "DiscoveryMetrics",
    "OpenStreetMapProvider",
    "FoursquareProvider",
    "WebSearchProvider",
    "CrawlEngine",
    "HybridDiscoveryEngine",
    "DiscoveryMode",
    "GeoProvider",
    "get_geo_provider",
    "AddressCompleteness",
    "AddressProfile",
    "AddressNormalizer",
    "extract_uk_postcode",
    "haversine_distance_meters"
]
