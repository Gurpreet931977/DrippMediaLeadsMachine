"""
Dripp Media — Discovery V3 Administrative City Boundary Validator
==================================================================
Performs strict point-in-polygon validation of candidate coordinates
against true OpenStreetMap administrative boundary relations (e.g. OSM Relation 162378
for the City of Birmingham, England).

Features:
- Dynamically discovers administrative boundary relations from OpenStreetMap / Nominatim.
- Caches boundary GeoJSON files locally in data/boundaries/.
- Pure-Python ray casting point-in-polygon algorithm (supports Polygon and MultiPolygon).
- Bounding-box pre-filtering for O(1) fast rejection.
- Secondary safeguards (address and locality text) as safety checks.
- Generates:
    city_match: bool
    city_match_reason: str
    boundary_source: str
    boundary_validation_method: str
"""

import os
import re
import json
import time
import requests
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple


@dataclass
class BoundaryValidationResult:
    city_match: bool
    city_match_reason: str
    boundary_source: str
    boundary_validation_method: str


class CityBoundaryValidator:
    """
    Validates geographic coordinates against official administrative boundaries.
    """

    def __init__(self, boundaries_dir: Optional[str] = None):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.boundaries_dir = boundaries_dir or os.path.join(base_dir, "data", "boundaries")
        os.makedirs(self.boundaries_dir, exist_ok=True)
        self._loaded_boundaries: Dict[str, Dict[str, Any]] = {}
        self._boundary_bboxes: Dict[str, Tuple[float, float, float, float]] = {}

    @staticmethod
    def _point_in_ring(x: float, y: float, ring: List[List[float]]) -> bool:
        """
        Ray-casting point-in-polygon algorithm.
        x = lon, y = lat
        ring = [[lon, lat], ...]
        """
        n = len(ring)
        if n < 3:
            return False

        inside = False
        p1x, p1y = ring[0][0], ring[0][1]
        for i in range(1, n + 1):
            p2x, p2y = ring[i % n][0], ring[i % n][1]
            if y > min(p1y, p2y):
                if y <= max(p1y, p2y):
                    if x <= max(p1x, p2x):
                        if p1y != p2y:
                            xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                        if p1x == p2x or x <= xinters:
                            inside = not inside
            p1x, p1y = p2x, p2y

        return inside

    @classmethod
    def point_in_geometry(cls, lon: float, lat: float, geometry: Dict[str, Any]) -> bool:
        """
        Tests if (lon, lat) is within GeoJSON Polygon or MultiPolygon geometry.
        Respects polygon holes (interior rings).
        """
        geom_type = geometry.get("type")
        coords = geometry.get("coordinates", [])

        if geom_type == "Polygon":
            if not coords:
                return False
            # Outer ring must contain the point
            if not cls._point_in_ring(lon, lat, coords[0]):
                return False
            # Point must not fall inside any interior ring (hole)
            for hole in coords[1:]:
                if cls._point_in_ring(lon, lat, hole):
                    return False
            return True

        elif geom_type == "MultiPolygon":
            for poly_coords in coords:
                if not poly_coords:
                    continue
                if cls._point_in_ring(lon, lat, poly_coords[0]):
                    # Check holes in this polygon
                    in_hole = False
                    for hole in poly_coords[1:]:
                        if cls._point_in_ring(lon, lat, hole):
                            in_hole = True
                            break
                    if not in_hole:
                        return True
            return False

        return False

    @staticmethod
    def compute_geometry_bbox(geometry: Dict[str, Any]) -> Tuple[float, float, float, float]:
        """Computes (min_lon, min_lat, max_lon, max_lat) from geometry coordinates."""
        min_lon, min_lat = float("inf"), float("inf")
        max_lon, max_lat = float("-inf"), float("-inf")

        def _traverse(val):
            nonlocal min_lon, min_lat, max_lon, max_lat
            if isinstance(val, (list, tuple)):
                if len(val) >= 2 and isinstance(val[0], (int, float)) and isinstance(val[1], (int, float)):
                    lon, lat = val[0], val[1]
                    if lon < min_lon: min_lon = lon
                    if lon > max_lon: max_lon = lon
                    if lat < min_lat: min_lat = lat
                    if lat > max_lat: max_lat = lat
                else:
                    for sub in val:
                        _traverse(sub)

        _traverse(geometry.get("coordinates", []))
        return (min_lon, min_lat, max_lon, max_lat)

    def get_or_fetch_boundary(self, city: str, country: str = "") -> Optional[Dict[str, Any]]:
        """
        Loads cached administrative boundary GeoJSON, or discovers and fetches
        it dynamically from OpenStreetMap / Nominatim.
        """
        city_slug = re.sub(r"[^a-z0-9]", "_", city.strip().lower())
        cache_file = os.path.join(self.boundaries_dir, f"{city_slug}_admin_boundary.geojson")

        if city_slug in self._loaded_boundaries:
            return self._loaded_boundaries[city_slug]

        # 1. Load from local cache if exists
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    feature = json.load(f)
                self._loaded_boundaries[city_slug] = feature
                geom = feature.get("geometry", {})
                self._boundary_bboxes[city_slug] = self.compute_geometry_bbox(geom)
                return feature
            except Exception as e:
                print(f"[BoundaryValidator] Error loading cached boundary for {city}: {e}")

        # 2. Dynamic discovery from Nominatim / OpenStreetMap
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "city": city,
            "country": country,
            "featuretype": "settlement",
            "format": "json",
            "polygon_geojson": 1
        }
        headers = {"User-Agent": "DrippLeadsEngine/3.0 (contact@drippmedia.com)"}

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                # Find relation with administrative boundary
                admin_rels = [
                    x for x in data
                    if x.get("osm_type") == "relation" and x.get("class") == "boundary"
                ]
                chosen = admin_rels[0] if admin_rels else (data[0] if data else None)
                if chosen and chosen.get("geojson"):
                    feature = {
                        "type": "Feature",
                        "properties": {
                            "osm_id": chosen.get("osm_id"),
                            "osm_type": chosen.get("osm_type"),
                            "name": city,
                            "display_name": chosen.get("display_name"),
                            "boundary": chosen.get("type", "administrative"),
                            "source": f"OpenStreetMap {chosen.get('osm_type', 'relation').capitalize()} {chosen.get('osm_id')}"
                        },
                        "geometry": chosen.get("geojson")
                    }
                    with open(cache_file, "w", encoding="utf-8") as f:
                        json.dump(feature, f, indent=2)

                    self._loaded_boundaries[city_slug] = feature
                    self._boundary_bboxes[city_slug] = self.compute_geometry_bbox(chosen.get("geojson"))
                    return feature
        except Exception as ex:
            print(f"[BoundaryValidator] Dynamic boundary fetch failed for {city}: {ex}")

        return None

    def validate_candidate(
        self,
        city: str,
        country: str,
        lat: Optional[float],
        lon: Optional[float],
        address: str = "",
        detected_city: str = ""
    ) -> BoundaryValidationResult:
        """
        Performs true administrative boundary validation for candidate location (Section 4).
        """
        c_target = (city or "").strip()
        c_detected = (detected_city or "").strip().lower()
        addr_low = (address or "").strip().lower()
        city_slug = re.sub(r"[^a-z0-9]", "_", c_target.lower())

        feature = self.get_or_fetch_boundary(city=c_target, country=country)

        # ── 1. POINT-IN-POLYGON (PRIMARY VALIDATION) ──
        if feature and lat is not None and lon is not None:
            geom = feature.get("geometry", {})
            props = feature.get("properties", {})
            source_desc = props.get("source") or f"OSM_RELATION_{props.get('osm_id', 'UNKNOWN')}"

            # O(1) Bounding-box pre-filtering
            bbox = self._boundary_bboxes.get(city_slug)
            if bbox:
                min_lon, min_lat, max_lon, max_lat = bbox
                if lon < min_lon or lon > max_lon or lat < min_lat or lat > max_lat:
                    return BoundaryValidationResult(
                        city_match=False,
                        city_match_reason=f"Coordinates ({lat:.4f}, {lon:.4f}) fall outside {c_target} boundary envelope",
                        boundary_source=source_desc,
                        boundary_validation_method="POINT_IN_POLYGON_RAY_CAST"
                    )

            # Exact Ray-casting point-in-polygon
            is_inside = self.point_in_geometry(lon=lon, lat=lat, geometry=geom)
            if not is_inside:
                return BoundaryValidationResult(
                    city_match=False,
                    city_match_reason=f"Coordinates ({lat:.4f}, {lon:.4f}) fall outside {c_target} administrative boundary polygon",
                    boundary_source=source_desc,
                    boundary_validation_method="POINT_IN_POLYGON_RAY_CAST"
                )

            # Secondary safeguard check: check address text for incompatible outer entities if known for target city
            CITY_EXCLUSIONS = {
                "birmingham": {
                    "cofton hackett", "solihull", "bromsgrove", "dudley", "walsall",
                    "sandwell", "wolverhampton", "west bromwich", "halesowen",
                    "stourbridge", "redditch", "tamworth", "worcestershire"
                }
            }
            adjacent_boroughs = CITY_EXCLUSIONS.get(c_target, set())
            if adjacent_boroughs:
                if c_detected in adjacent_boroughs:
                    return BoundaryValidationResult(
                        city_match=False,
                        city_match_reason=f"Detected municipality '{detected_city}' is outside {c_target} boundary",
                        boundary_source=source_desc,
                        boundary_validation_method="POINT_IN_POLYGON_SAFEGUARD"
                    )

                for adj in adjacent_boroughs:
                    if f", {adj}," in addr_low or addr_low.endswith(f", {adj}"):
                        return BoundaryValidationResult(
                            city_match=False,
                            city_match_reason=f"Address contains adjacent municipality '{adj}' outside {c_target} boundary",
                            boundary_source=source_desc,
                            boundary_validation_method="POINT_IN_POLYGON_SAFEGUARD"
                        )

            return BoundaryValidationResult(
                city_match=True,
                city_match_reason=f"Verified within {c_target} administrative boundary polygon",
                boundary_source=source_desc,
                boundary_validation_method="POINT_IN_POLYGON_RAY_CAST"
            )

        # ── 2. TEXT SAFEGUARDS (FALLBACK WHEN COORDS UNAVAILABLE) ──
        if "birmingham" in c_target.lower():
            adjacent_boroughs = {
                "cofton hackett", "solihull", "bromsgrove", "dudley", "walsall",
                "sandwell", "wolverhampton", "west bromwich", "halesowen",
                "stourbridge", "redditch", "tamworth", "worcestershire"
            }
            if c_detected in adjacent_boroughs:
                return BoundaryValidationResult(
                    city_match=False,
                    city_match_reason=f"Detected municipality '{detected_city}' is outside Birmingham boundary",
                    boundary_source="ADDRESS_METADATA",
                    boundary_validation_method="TEXT_SAFEGUARD_FALLBACK"
                )
            for adj in adjacent_boroughs:
                if f", {adj}," in addr_low or addr_low.endswith(f", {adj}"):
                    return BoundaryValidationResult(
                        city_match=False,
                        city_match_reason=f"Address contains adjacent municipality '{adj}' outside Birmingham boundary",
                        boundary_source="ADDRESS_METADATA",
                        boundary_validation_method="TEXT_SAFEGUARD_FALLBACK"
                    )

        if c_detected and c_target.lower() not in c_detected and c_detected not in addr_low:
            return BoundaryValidationResult(
                city_match=False,
                city_match_reason=f"Detected locality '{detected_city}' does not match target city '{city}'",
                boundary_source="ADDRESS_METADATA",
                boundary_validation_method="TEXT_SAFEGUARD_FALLBACK"
            )

        return BoundaryValidationResult(
            city_match=True,
            city_match_reason=f"Matches target city '{city}' via text safeguard",
            boundary_source="ADDRESS_METADATA",
            boundary_validation_method="TEXT_SAFEGUARD_FALLBACK"
        )

    def is_point_in_boundary(self, lat: float, lon: float, city: str = "Birmingham", country: str = "United Kingdom") -> Tuple[bool, str]:
        """Convenience method returning (is_inside, reason) for a given coordinate."""
        res = self.validate_candidate(city=city, country=country, lat=lat, lon=lon)
        return res.city_match, res.city_match_reason


_GLOBAL_BOUNDARY_VALIDATOR: Optional[CityBoundaryValidator] = None

def get_boundary_validator() -> CityBoundaryValidator:
    """Returns the singleton CityBoundaryValidator instance."""
    global _GLOBAL_BOUNDARY_VALIDATOR
    if _GLOBAL_BOUNDARY_VALIDATOR is None:
        _GLOBAL_BOUNDARY_VALIDATOR = CityBoundaryValidator()
    return _GLOBAL_BOUNDARY_VALIDATOR

