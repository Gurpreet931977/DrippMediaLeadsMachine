"""
lib/production/market_config.py
================================
Reusable market configuration and registry for Phase 9.0 multi-market acquisition.
Removes city-specific and market-specific assumptions from the core runner.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Tuple
import json


@dataclass
class MarketConfig:
    """
    Configuration specification for a target acquisition market.
    Supports country, city, region, target industries, language, timezone,
    discovery sources, batch execution bounds, daily quotas, and enabled state.
    """
    market_id: str
    country: str
    city: str
    region: Optional[str] = None
    industries: List[str] = field(default_factory=lambda: ["restaurant", "cafe", "pub", "bar", "hospitality"])
    language: str = "en"
    timezone: str = "Europe/London"
    target_type: str = "INDEPENDENT_BUSINESS"
    discovery_sources: List[str] = field(default_factory=lambda: ["OPENSTREETMAP"])
    batch_size: int = 10
    daily_quota: Dict[str, int] = field(default_factory=lambda: {
        "search_requests": 500,
        "gosom_calls": 10,
        "enrichment_calls": 50,
        "crm_writes": 100,
        "outreach_dispatches": 0
    })
    coordinates_box: Optional[Tuple[float, float, float, float]] = None  # (south, north, west, east)
    enabled: bool = False
    search_quota: int = 500
    enrichment_quota: int = 50

    def to_dict(self) -> Dict[str, Any]:
        """Serializes market configuration to a dictionary."""
        d = asdict(self)
        if self.coordinates_box:
            d["coordinates_box"] = list(self.coordinates_box)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MarketConfig":
        """Instantiates MarketConfig from dictionary."""
        c = dict(data)
        if "coordinates_box" in c and c["coordinates_box"] is not None:
            c["coordinates_box"] = tuple(c["coordinates_box"])
        # Support both 'industry' (str) and 'industries' (list)
        if "industry" in c and "industries" not in c:
            c["industries"] = [c.pop("industry")]
        return cls(**c)

    def validate(self) -> Tuple[bool, List[str]]:
        """Validates configuration parameters."""
        errors: List[str] = []
        if not self.market_id or not str(self.market_id).strip():
            errors.append("market_id is required.")
        if not self.country or not str(self.country).strip():
            errors.append("country is required.")
        if not self.city or not str(self.city).strip():
            errors.append("city is required.")
        if not self.industries or len(self.industries) == 0:
            errors.append("At least one target industry is required.")
        if self.batch_size <= 0:
            errors.append("batch_size must be a positive integer.")
        if self.daily_quota.get("outreach_dispatches", 0) > 0:
            errors.append("Acquisition market daily_quota must have outreach_dispatches == 0.")
        return len(errors) == 0, errors


MarketDefinition = MarketConfig


class MarketRegistry:
    """
    Registry of pre-configured production markets.
    Supports Manchester, Leeds, Birmingham, London, and custom user-registered markets.
    Enforces market enablement rules (Section 12):
      Only enabled markets may run.
      Default: MANCHESTER_UK = enabled, all others = disabled.
    """
    _markets: Dict[str, MarketConfig] = {}

    @classmethod
    def register(cls, config: MarketConfig) -> None:
        valid, errors = config.validate()
        if not valid:
            raise ValueError(f"Invalid market config '{config.market_id}': {', '.join(errors)}")
        cls._markets[config.market_id.upper()] = config

    @classmethod
    def get(cls, market_id: str) -> MarketConfig:
        key = market_id.strip().upper()
        if key not in cls._markets:
            raise KeyError(f"Market '{market_id}' is not registered in MarketRegistry. Available: {cls.list_markets()}")
        return cls._markets[key]

    @classmethod
    def list_markets(cls) -> List[str]:
        return sorted(list(cls._markets.keys()))

    @classmethod
    def list_enabled_markets(cls) -> List[str]:
        return [k for k in cls.list_markets() if cls._markets[k].enabled]

    @classmethod
    def is_market_enabled(cls, market_id: str) -> bool:
        try:
            return cls.get(market_id).enabled
        except KeyError:
            return False

    @classmethod
    def enable_market(cls, market_id: str) -> None:
        cfg = cls.get(market_id)
        cfg.enabled = True

    @classmethod
    def disable_market(cls, market_id: str) -> None:
        cfg = cls.get(market_id)
        cfg.enabled = False

    @classmethod
    def get_all_markets(cls) -> List[MarketConfig]:
        return [cls._markets[k] for k in cls.list_markets()]

    @classmethod
    def clear(cls) -> None:
        cls._markets.clear()
        cls._init_defaults()

    @classmethod
    def _init_defaults(cls) -> None:
        """Initializes canonical pre-configured UK markets with Section 12 defaults."""
        # 1. Manchester, UK (Primary Production Market — DEFAULT ENABLED)
        cls.register(MarketConfig(
            market_id="MANCHESTER_UK",
            country="United Kingdom",
            city="Manchester",
            region="Greater Manchester",
            industries=["restaurant", "cafe", "pub", "bar", "hospitality"],
            language="en",
            timezone="Europe/London",
            target_type="INDEPENDENT_BUSINESS",
            discovery_sources=["OPENSTREETMAP"],
            batch_size=10,
            daily_quota={
                "search_requests": 500,
                "gosom_calls": 10,
                "enrichment_calls": 50,
                "crm_writes": 100,
                "outreach_dispatches": 0
            },
            coordinates_box=(53.38, 53.55, -2.33, -2.14),
            enabled=True,
            search_quota=500,
            enrichment_quota=50,
        ))

        # 2. Leeds, UK (Disabled by default)
        cls.register(MarketConfig(
            market_id="LEEDS_UK",
            country="United Kingdom",
            city="Leeds",
            region="West Yorkshire",
            industries=["restaurant", "cafe", "pub", "bar", "hospitality"],
            language="en",
            timezone="Europe/London",
            target_type="INDEPENDENT_BUSINESS",
            discovery_sources=["OPENSTREETMAP"],
            batch_size=10,
            daily_quota={
                "search_requests": 500,
                "gosom_calls": 10,
                "enrichment_calls": 50,
                "crm_writes": 100,
                "outreach_dispatches": 0
            },
            coordinates_box=(53.74, 53.86, -1.65, -1.45),
            enabled=False,
            search_quota=500,
            enrichment_quota=50,
        ))

        # 3. Birmingham, UK (Disabled by default)
        cls.register(MarketConfig(
            market_id="BIRMINGHAM_UK",
            country="United Kingdom",
            city="Birmingham",
            region="West Midlands",
            industries=["restaurant", "cafe", "pub", "bar", "hospitality"],
            language="en",
            timezone="Europe/London",
            target_type="INDEPENDENT_BUSINESS",
            discovery_sources=["OPENSTREETMAP"],
            batch_size=10,
            daily_quota={
                "search_requests": 500,
                "gosom_calls": 10,
                "enrichment_calls": 50,
                "crm_writes": 100,
                "outreach_dispatches": 0
            },
            coordinates_box=(52.42, 52.54, -1.98, -1.82),
            enabled=False,
            search_quota=500,
            enrichment_quota=50,
        ))

        # 4. London, UK (Disabled by default)
        cls.register(MarketConfig(
            market_id="LONDON_UK",
            country="United Kingdom",
            city="London",
            region="Greater London",
            industries=["restaurant", "cafe", "pub", "bar", "hospitality"],
            language="en",
            timezone="Europe/London",
            target_type="INDEPENDENT_BUSINESS",
            discovery_sources=["OPENSTREETMAP"],
            batch_size=10,
            daily_quota={
                "search_requests": 500,
                "gosom_calls": 10,
                "enrichment_calls": 50,
                "crm_writes": 100,
                "outreach_dispatches": 0
            },
            coordinates_box=(51.45, 51.55, -0.20, 0.02),
            enabled=False,
            search_quota=500,
            enrichment_quota=50,
        ))


# Initialize default markets
MarketRegistry._init_defaults()
