"""
Base Discovery Provider Abstraction & Common Interfaces
Defines the standard interface for all discovery engines (OpenStreetMap, Foursquare, WebSearch, Apify, Hybrid).
"""
import time
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from lib.types import DiscoveredBusiness

class DiscoveryMetrics:
    def __init__(self, provider_name: str):
        self.provider_name = provider_name
        self.calls: int = 0
        self.errors: int = 0
        self.candidates_raw: int = 0
        self.candidates_unique: int = 0
        self.total_latency: float = 0.0
        self.estimated_cost: float = 0.0

    @property
    def avg_latency(self) -> float:
        return (self.total_latency / self.calls) if self.calls > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "calls": self.calls,
            "errors": self.errors,
            "candidates_raw": self.candidates_raw,
            "candidates_unique": self.candidates_unique,
            "total_latency_sec": round(self.total_latency, 2),
            "avg_latency_sec": round(self.avg_latency, 3),
            "estimated_cost": round(self.estimated_cost, 4)
        }

class DiscoveryProvider(ABC):
    """
    Abstract Discovery Provider interface.
    All discovery engines adhere to this contract.
    """
    def __init__(self, name: str):
        self.name = name
        self.metrics = DiscoveryMetrics(name)

    @abstractmethod
    def search_businesses(
        self,
        city: str,
        country: str,
        industry: str,
        limit: int = 40,
        **kwargs
    ) -> List[DiscoveredBusiness]:
        """Discover candidate businesses matching criteria."""
        pass

    def get_business_details(self, business_id_or_data: Any) -> Optional[DiscoveredBusiness]:
        """Fetch details for a specific place/business record."""
        return None

    def search_web(self, query: str, num_results: int = 5) -> List[Dict[str, Any]]:
        """Search the public web for context or business signals."""
        return []

    def search_social_references(
        self,
        business_name: str,
        city: str,
        country: str = "United Kingdom",
        **kwargs
    ) -> List[Dict[str, Any]]:
        """Search for third-party social / creator references."""
        return []

    @abstractmethod
    def health_check(self) -> Dict[str, Any]:
        """Check provider connectivity, credentials, and readiness."""
        pass

    def record_call(self, latency: float, candidates_count: int = 0, error: bool = False, cost: float = 0.0):
        self.metrics.calls += 1
        self.metrics.total_latency += latency
        self.metrics.candidates_raw += candidates_count
        if error:
            self.metrics.errors += 1
        self.metrics.estimated_cost += cost
