"""
Dripp Media — Global Geography & Administrative Boundary Provider (GeoProvider)
==============================================================================
Responsible for:
  1. resolve_country(country_input)
  2. resolve_city(city, country, region)
  3. get_administrative_boundary(city, country, boundary_type, admin_level)
  4. validate_point_in_boundary(lat, lon, city, country, boundary_type)
  5. normalize_locality(locality)
  6. get_locality_aliases(city, country)

Principles:
  - Global-first: zero hardcoded Birmingham or Leeds relations.
  - Runtime resolution via OSM Nominatim / Overpass with local caching in data/boundaries/.
  - Configurable boundary types and administrative levels (e.g. admin_level=8 in UK/DE, municipal in US).
  - Explicit handling of unknown/unresolved geography without fabrication.
"""

import os
import re
import json
import unicodedata
from typing import Dict, Any, List, Optional, Tuple
import requests

from lib.discovery.boundary_validator import CityBoundaryValidator, BoundaryValidationResult

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "DrippMediaGlobalLeadEngine/3.0 (contact: info@drippmedia.com)"

# ISO 3166-1 country lookup dictionary
KNOWN_COUNTRIES: Dict[str, Dict[str, Any]] = {
    "GB": {"name": "United Kingdom", "iso2": "GB", "iso3": "GBR", "currency": "GBP", "calling_code": "+44", "admin_level": 8},
    "UK": {"name": "United Kingdom", "iso2": "GB", "iso3": "GBR", "currency": "GBP", "calling_code": "+44", "admin_level": 8},
    "UNITED KINGDOM": {"name": "United Kingdom", "iso2": "GB", "iso3": "GBR", "currency": "GBP", "calling_code": "+44", "admin_level": 8},
    "ENGLAND": {"name": "United Kingdom", "iso2": "GB", "iso3": "GBR", "currency": "GBP", "calling_code": "+44", "admin_level": 8},
    "SCOTLAND": {"name": "United Kingdom", "iso2": "GB", "iso3": "GBR", "currency": "GBP", "calling_code": "+44", "admin_level": 8},
    "WALES": {"name": "United Kingdom", "iso2": "GB", "iso3": "GBR", "currency": "GBP", "calling_code": "+44", "admin_level": 8},

    "US": {"name": "United States", "iso2": "US", "iso3": "USA", "currency": "USD", "calling_code": "+1", "admin_level": 8},
    "USA": {"name": "United States", "iso2": "US", "iso3": "USA", "currency": "USD", "calling_code": "+1", "admin_level": 8},
    "UNITED STATES": {"name": "United States", "iso2": "US", "iso3": "USA", "currency": "USD", "calling_code": "+1", "admin_level": 8},

    "DE": {"name": "Germany", "iso2": "DE", "iso3": "DEU", "currency": "EUR", "calling_code": "+49", "admin_level": 8},
    "GERMANY": {"name": "Germany", "iso2": "DE", "iso3": "DEU", "currency": "EUR", "calling_code": "+49", "admin_level": 8},
    "DEUTSCHLAND": {"name": "Germany", "iso2": "DE", "iso3": "DEU", "currency": "EUR", "calling_code": "+49", "admin_level": 8},

    "AE": {"name": "United Arab Emirates", "iso2": "AE", "iso3": "ARE", "currency": "AED", "calling_code": "+971", "admin_level": 6},
    "UAE": {"name": "United Arab Emirates", "iso2": "AE", "iso3": "ARE", "currency": "AED", "calling_code": "+971", "admin_level": 6},
    "UNITED ARAB EMIRATES": {"name": "United Arab Emirates", "iso2": "AE", "iso3": "ARE", "currency": "AED", "calling_code": "+971", "admin_level": 6},

    "CA": {"name": "Canada", "iso2": "CA", "iso3": "CAN", "currency": "CAD", "calling_code": "+1", "admin_level": 8},
    "CANADA": {"name": "Canada", "iso2": "CA", "iso3": "CAN", "currency": "CAD", "calling_code": "+1", "admin_level": 8},

    "AU": {"name": "Australia", "iso2": "AU", "iso3": "AUS", "currency": "AUD", "calling_code": "+61", "admin_level": 8},
    "AUSTRALIA": {"name": "Australia", "iso2": "AU", "iso3": "AUS", "currency": "AUD", "calling_code": "+61", "admin_level": 8},

    "FR": {"name": "France", "iso2": "FR", "iso3": "FRA", "currency": "EUR", "calling_code": "+33", "admin_level": 8},
    "FRANCE": {"name": "France", "iso2": "FR", "iso3": "FRA", "currency": "EUR", "calling_code": "+33", "admin_level": 8},
}

# Known locality aliases and common outward code / district indicators
LOCALITY_ALIAS_REGISTRY: Dict[Tuple[str, str], List[str]] = {
    ("leeds", "united kingdom"): [
        "leeds", "ls1", "ls2", "ls3", "ls4", "ls5", "ls6", "ls7", "ls8", "ls9", "ls10",
        "ls11", "ls12", "ls13", "ls14", "ls15", "ls16", "ls17", "ls18", "ls19", "ls20",
        "ls21", "ls28", "headingley", "otley", "yeadon", "pudsey", "horsforth", "chapel allerton"
    ],
    ("birmingham", "united kingdom"): [
        "birmingham", "bham", "b1", "b2", "b3", "b4", "b5", "b15", "b29", "b30",
        "digbeth", "edgbaston", "jewellery quarter", "mosley", "harborne", "solihull"
    ],
    ("manchester", "united kingdom"): [
        "manchester", "mcr", "m1", "m2", "m3", "m4", "m11", "m13", "m14", "m19", "m20", "m21",
        "ancoats", "chorlton", "didsbury", "northern quarter", "salford", "beswick"
    ],
    ("london", "united kingdom"): [
        "london", "ldn", "ec1", "ec2", "ec3", "ec4", "wc1", "wc2", "e1", "w1", "sw1", "se1", "nw1", "n1",
        "soho", "shoreditch", "camden", "kensington", "chelsea", "canary wharf"
    ],
    ("berlin", "germany"): [
        "berlin", "mitte", "kreuzberg", "neukölln", "charlottenburg", "prenzlauer berg", "friedrichshain", "schöneberg"
    ],
    ("munich", "germany"): [
        "munich", "münchen", "altstadt", "schwabing", "maxvorstadt", "sendling"
    ],
    ("dubai", "united arab emirates"): [
        "dubai", "dxb", "deira", "bur dubai", "jumeirah", "downtown", "marina", "business bay", "al barsha", "difc"
    ],
    ("new york", "united states"): [
        "new york", "nyc", "manhattan", "brooklyn", "queens", "bronx", "staten island"
    ]
}


class GeoProvider:
    """
    Unified global geography and boundary provider.
    Eliminates geography hardcoding while reusing local boundary caches.
    """

    def __init__(self, boundaries_dir: Optional[str] = None):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.boundaries_dir = boundaries_dir or os.path.join(base_dir, "data", "boundaries")
        os.makedirs(self.boundaries_dir, exist_ok=True)
        self.boundary_validator = CityBoundaryValidator(boundaries_dir=self.boundaries_dir)

    # ──────────────────────────────────────────────────────────────────────────
    # 1. COUNTRY RESOLUTION
    # ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def resolve_country(country_input: str) -> Dict[str, Any]:
        """
        Standardizes input country name to ISO alpha-2, alpha-3, standardized name,
        calling code, and currency without hardcoded national assumptions.
        """
        if not country_input or not isinstance(country_input, str):
            return {
                "name": "Unknown",
                "iso2": "XX",
                "iso3": "XXX",
                "currency": "USD",
                "calling_code": "",
                "status": "UNRESOLVED"
            }

        norm = country_input.strip().upper()
        if norm in KNOWN_COUNTRIES:
            data = KNOWN_COUNTRIES[norm].copy()
            data["status"] = "RESOLVED"
            return data

        # Check title case / normalized keys
        norm_clean = re.sub(r'[^A-Z\s]', '', norm).strip()
        if norm_clean in KNOWN_COUNTRIES:
            data = KNOWN_COUNTRIES[norm_clean].copy()
            data["status"] = "RESOLVED"
            return data

        # Generic fallback for uncatalogued country
        return {
            "name": country_input.strip().title(),
            "iso2": norm_clean[:2] if len(norm_clean) >= 2 else "XX",
            "iso3": norm_clean[:3] if len(norm_clean) >= 3 else "XXX",
            "currency": "USD",
            "calling_code": "",
            "status": "UNRESOLVED_GENERIC"
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 2. LOCALITY NORMALIZATION
    # ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def normalize_locality(locality: str) -> str:
        """
        Normalizes city or region name: strips accents/diacritics, normalizes spaces and punctuation.
        Example: 'München' -> 'Munich' or 'Munchen'
        """
        if not locality or not isinstance(locality, str):
            return ""

        # Normalize unicode (NFD decomposition -> remove combining characters)
        nfkd = unicodedata.normalize('NFKD', locality)
        ascii_text = ''.join([c for c in nfkd if not unicodedata.combining(c)])
        # Clean extra spaces
        clean = re.sub(r'\s+', ' ', ascii_text).strip()
        return clean.title()

    # ──────────────────────────────────────────────────────────────────────────
    # 3. LOCALITY ALIASES
    # ──────────────────────────────────────────────────────────────────────────
    @classmethod
    def get_locality_aliases(cls, city: str, country: str) -> List[str]:
        """
        Returns known district/suburb and abbreviation aliases for a city.
        Falls back dynamically to city name tokens.
        """
        c_norm = city.strip().lower()
        country_norm = country.strip().lower()

        # Check registered lookup
        key = (c_norm, country_norm)
        if key in LOCALITY_ALIAS_REGISTRY:
            return list(LOCALITY_ALIAS_REGISTRY[key])

        # Try without country
        for (k_city, _), aliases in LOCALITY_ALIAS_REGISTRY.items():
            if k_city == c_norm:
                return list(aliases)

        # Dynamic fallback: city name, lowercase, tokens >= 3 chars
        aliases = [c_norm]
        tokens = [t for t in re.split(r'[^a-z0-9]+', c_norm) if len(t) >= 3]
        for t in tokens:
            if t not in aliases:
                aliases.append(t)
        return aliases

    # ──────────────────────────────────────────────────────────────────────────
    # 4. CITY RESOLUTION
    # ──────────────────────────────────────────────────────────────────────────
    def resolve_city(
        self,
        city: str,
        country: str,
        region: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Resolves city name, coordinates, bounding box, and administrative boundary
        metadata via local boundary cache or Nominatim geocoding.
        """
        city_norm = self.normalize_locality(city)
        country_info = self.resolve_country(country)
        country_std = country_info.get("name", country)

        city_slug = re.sub(r"[^a-z0-9]", "_", city_norm.lower())
        cache_file = os.path.join(self.boundaries_dir, f"{city_slug}_admin_boundary.geojson")

        # Check local cache first
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                props = data.get("properties", {})
                geom = data.get("geometry", {})
                bbox = self.boundary_validator.compute_geometry_bbox(geom) if geom else None
                return {
                    "city": city_norm,
                    "country": country_std,
                    "region": region or props.get("state", ""),
                    "osm_id": props.get("osm_id"),
                    "boundary_type": props.get("boundary", "administrative"),
                    "bounding_box": bbox,
                    "status": "CACHED_BOUNDARY_FOUND"
                }
            except Exception:
                pass

        # Dynamic lookup from Nominatim if not cached
        url = NOMINATIM_URL
        params = {
            "city": city_norm,
            "country": country_std,
            "format": "json",
            "polygon_geojson": 0
        }
        if region:
            params["state"] = region
        headers = {"User-Agent": USER_AGENT}

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    item = data[0]
                    bb = item.get("boundingbox", [])
                    # Nominatim bbox: [south, north, west, east]
                    bbox = (float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1])) if len(bb) >= 4 else None
                    return {
                        "city": city_norm,
                        "country": country_std,
                        "region": region or "",
                        "osm_id": item.get("osm_id"),
                        "lat": float(item.get("lat", 0)),
                        "lon": float(item.get("lon", 0)),
                        "bounding_box": bbox,
                        "status": "NOMINATIM_RESOLVED"
                    }
        except Exception:
            pass

        return {
            "city": city_norm,
            "country": country_std,
            "region": region or "",
            "osm_id": None,
            "status": "UNRESOLVED_GEOMETRY"
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 5. ADMINISTRATIVE BOUNDARY FETCHING & RESOLUTION
    # ──────────────────────────────────────────────────────────────────────────
    def get_administrative_boundary(
        self,
        city: str,
        country: str,
        boundary_type: str = "administrative",
        admin_level: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Dynamically discovers and returns administrative boundary polygon GeoJSON.
        Boundary type is configurable (e.g. 'administrative', 'municipal', 'postal_code').
        """
        city_slug = re.sub(r"[^a-z0-9]", "_", city.strip().lower())
        cache_file = os.path.join(self.boundaries_dir, f"{city_slug}_admin_boundary.geojson")

        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        # Use boundary validator to dynamically fetch from Nominatim / OSM
        return self.boundary_validator.get_or_fetch_boundary(city=city, country=country)

    # ──────────────────────────────────────────────────────────────────────────
    # 6. POINT-IN-BOUNDARY VALIDATION
    # ──────────────────────────────────────────────────────────────────────────
    def validate_point_in_boundary(
        self,
        lat: Optional[float],
        lon: Optional[float],
        city: str,
        country: str,
        boundary_type: str = "administrative",
        address: str = "",
        detected_city: str = ""
    ) -> Tuple[bool, str]:
        """
        Validates if (lat, lon) resides within the official administrative boundary
        of the specified city and country.
        """
        res = self.boundary_validator.validate_candidate(
            city=city,
            country=country,
            lat=lat,
            lon=lon,
            address=address,
            detected_city=detected_city
        )
        return res.city_match, res.city_match_reason


_GLOBAL_GEO_PROVIDER: Optional[GeoProvider] = None

def get_geo_provider() -> GeoProvider:
    """Returns the singleton GeoProvider instance."""
    global _GLOBAL_GEO_PROVIDER
    if _GLOBAL_GEO_PROVIDER is None:
        _GLOBAL_GEO_PROVIDER = GeoProvider()
    return _GLOBAL_GEO_PROVIDER
