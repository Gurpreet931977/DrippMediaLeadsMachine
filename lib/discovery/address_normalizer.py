"""
Dripp Media — Address Completeness & Branch Identity Normalizer (Phase 7.7)
===========================================================================
Hardens upstream discovery address completeness and branch identity before review
enrichment and Gosom fallback.

Core Responsibilities:
  1. Address Completeness Model:
     - COMPLETE: street + postcode + city + coordinates (lat/lon)
     - STRONG: street + city + coordinates (lat/lon)
     - PARTIAL: city + coordinates only (or street + city without coordinates/postcode)
     - MINIMAL: city only
     - UNKNOWN: missing city/coordinates
  2. Structured OSM Tag Extraction:
     - Prefers structured tags: addr:housenumber, addr:street, addr:postcode, addr:city, addr:suburb, etc.
     - Leaves missing fields as None (zero fabrication).
  3. UK Address & Street Extraction:
     - Extracts house numbers, unit numbers, street names with standard UK suffixes:
       (Road, Street, Avenue, Lane, Drive, Way, Place, Terrace, Gardens, Square, Arcade, Court, Close, Crescent, Boulevard, etc.)
     - Does NOT treat the entire string as street.
  4. UK Postcode Extraction & Normalization:
     - Extracts valid UK postcodes via UK_POSTCODE_REGEX.
     - Normalizes spacing (e.g. 'M22 4FY', 'M1 2AG', 'M90 4ZY').
     - Zero fabrication.
  5. Coordinate Preservation:
     - Retains latitude and longitude from discovery sources.
     - Provides Haversine distance checking for branch disambiguation.
  6. Branch Identity Hardening:
     - Identifies airport terminals (Terminal 1, Terminal 2, Terminal 3).
     - Identifies arcades and shopping centres (Barton Arcade, Arndale, Trafford Centre, etc.).
     - Identifies sub-localities (Northenden, Wythenshawe, Fallowfield, Northern Quarter, Angel Gardens, etc.).
  7. Gosom Eligibility Hardening:
     - If candidate has neither street nor postcode -> GOSOM_NOT_SAFE_TO_QUERY.
     - Hierarchical query generation:
       1. "<name>" "<street>" "<postcode>" "<city>"
       2. "<name>" "<street>" "<city>"
       3. "<name>" "<postcode>" "<city>"
       4. Blocked (None).
"""

import re
import math
from enum import Enum
from typing import Dict, Any, Optional, Tuple, List, Set
from dataclasses import dataclass, field


class AddressCompleteness(str, Enum):
    """
    Data quality classification for discovered business address completeness.
    This is data quality, NOT lead qualification.
    """
    COMPLETE = "COMPLETE"  # street + postcode + city + coordinates
    STRONG = "STRONG"      # street + city + coordinates
    PARTIAL = "PARTIAL"    # city + coordinates only (or street + city without coordinates/postcode)
    MINIMAL = "MINIMAL"    # city only
    UNKNOWN = "UNKNOWN"    # missing city


# Standard UK Street Suffixes and Types
UK_STREET_TYPES = [
    "road", "rd",
    "street", "st",
    "avenue", "ave",
    "lane", "ln",
    "drive", "dr",
    "way",
    "place", "pl",
    "terrace", "ter",
    "gardens", "gdn", "gdns",
    "square", "sq",
    "arcade",
    "court", "ct",
    "close", "cl",
    "crescent", "cres",
    "boulevard", "blvd",
    "walk",
    "parade",
    "row",
    "hill",
    "mews",
    "rise",
    "circus",
    "grove",
    "approach",
    "gate",
    "view",
    "yard",
    "wharf",
    "bank",
    "quay",
    "mall"
]

UK_POSTCODE_REGEX = re.compile(r'\b([A-Z]{1,2}[0-9][0-9A-Z]?\s?[0-9][A-Z]{2})\b', re.IGNORECASE)

KNOWN_BRANCH_MARKERS = [
    # Airport markers
    "terminal 1", "terminal 2", "terminal 3", "terminal 4", "terminal 5",
    "t1", "t2", "t3",
    "manchester airport", "airport", "departures", "arrivals",
    # Arcades & Centers
    "barton arcade", "arndale centre", "arndale", "trafford centre",
    "corn exchange", "royal exchange",
    # Sub-localities & Neighborhoods in Greater Manchester
    "northern quarter", "nq", "angel gardens", "ancoats",
    "northenden", "wythenshawe", "fallowfield", "didsbury", "west didsbury",
    "chorlton", "rusholme", "withington", "cheetham hill", "castlefield",
    "deansgate", "piccadilly", "spinningfields", "whalley range", "hulme",
    "salford", "stockport", "bury", "altrincham", "sale"
]


@dataclass
class AddressProfile:
    """Explicitly preserves all address and branch identity components."""
    raw_address: str
    normalized_address: str
    house_number: Optional[str] = None
    street: Optional[str] = None
    postcode: Optional[str] = None
    city: str = "Manchester"
    country: str = "United Kingdom"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    branch_identifier: Optional[str] = None
    completeness: AddressCompleteness = AddressCompleteness.MINIMAL
    provenance: Dict[str, str] = field(default_factory=dict)
    # Phase 7.8: OSM Address Source Fix Fields
    street_source: str = "NONE"       # "OSM_DIRECT_TAG", "OSM_PARENT_WAY", "STRING_EXTRACTED", "NONE"
    house_number_source: str = "NONE" # "OSM_DIRECT_TAG", "OSM_PARENT_WAY", "STRING_EXTRACTED", "NONE"
    postcode_source: str = "NONE"     # "OSM_DIRECT_TAG", "OSM_PARENT_WAY", "STRING_EXTRACTED", "NONE"
    parent_osm_id: Optional[str] = None
    address_conflict_status: str = "NO_CONFLICT"  # "NO_CONFLICT", "ADDRESS_CONFLICT", "AMBIGUOUS_ADDRESS"
    conflicting_address_data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_address": self.raw_address,
            "normalized_address": self.normalized_address,
            "house_number": self.house_number,
            "street": self.street,
            "postcode": self.postcode,
            "city": self.city,
            "country": self.country,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "branch_identifier": self.branch_identifier,
            "completeness": self.completeness.value,
            "provenance": self.provenance,
            "street_source": self.street_source,
            "house_number_source": self.house_number_source,
            "postcode_source": self.postcode_source,
            "parent_osm_id": self.parent_osm_id,
            "address_conflict_status": self.address_conflict_status,
            "conflicting_address_data": self.conflicting_address_data
        }


def extract_uk_postcode(text: Optional[str]) -> Optional[str]:
    """
    Extracts and standardizes a valid UK postcode.
    Returns format: 'OUTCODE INCODE' (e.g. 'M22 4FY', 'M1 2AG', 'M90 4ZY').
    Returns None if no valid UK postcode is found. Does NOT fabricate postcodes.
    """
    if not text:
        return None
    m = UK_POSTCODE_REGEX.search(text)
    if not m:
        return None
    raw_pc = m.group(1).upper().strip()
    # Normalize internal spacing
    compact = raw_pc.replace(" ", "")
    if len(compact) < 5 or len(compact) > 7:
        return None
    # Incode is always the last 3 characters (e.g. '4FY')
    outcode = compact[:-3]
    incode = compact[-3:]
    return f"{outcode} {incode}"


def haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates great-circle distance between two coordinates in meters."""
    R = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


class AddressNormalizer:
    """
    Normalizes addresses, extracts structured components, classifies data completeness,
    and identifies physical branches without data fabrication.
    """

    @classmethod
    def resolve_osm_address(
        cls,
        element: Dict[str, Any],
        parent_elements: Optional[List[Dict[str, Any]]] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        target_city: str = "Manchester",
        target_country: str = "United Kingdom"
    ) -> AddressProfile:
        """
        Phase 7.8: Recovers source-backed address information from OSM.
        When a POI lacks direct address tags, it may inherit address information ONLY
        from a clearly related enclosing/parent OSM element when that relationship is
        explicitly represented by returned OSM data (e.g. node is a member of the parent way).
        
        Zero nearest-building guessing.
        Zero reverse-geocoding.
        Zero paid API calls.
        Explicit provenance and conflict/ambiguity tracking.
        """
        element = element or {}
        parent_elements = parent_elements or []
        tags = element.get("tags", {})
        provenance: Dict[str, str] = {}

        # 1. Coordinates
        cand_lat = lat if lat is not None else element.get("lat") or (element.get("center") or {}).get("lat")
        cand_lon = lon if lon is not None else element.get("lon") or (element.get("center") or {}).get("lon")
        if cand_lat is not None and cand_lon is not None:
            provenance["coordinates"] = "OSM_COORDINATES"

        # 2. Extract direct POI tags
        direct_hn = tags.get("addr:housenumber") or tags.get("addr:housename") or tags.get("addr:unit")
        direct_st = tags.get("addr:street") or tags.get("street")
        direct_pc_raw = tags.get("addr:postcode") or tags.get("postal_code")
        direct_pc = extract_uk_postcode(direct_pc_raw) if direct_pc_raw else None
        direct_city = (tags.get("addr:city") or target_city).strip()
        direct_branch = tags.get("branch") or tags.get("operator")

        # 3. Inspect related parent elements (ways / relations where POI is an explicit member)
        node_id = element.get("id")
        valid_parents: List[Dict[str, Any]] = []
        for p in parent_elements:
            # Must be an explicitly related parent element
            p_type = p.get("type", "way")
            if p_type == "way":
                p_nodes = p.get("nodes", [])
                if node_id is None or not p_nodes or node_id in p_nodes:
                    valid_parents.append(p)
            elif p_type == "relation":
                p_members = [m.get("ref") for m in p.get("members", []) if isinstance(m, dict)]
                if node_id is None or not p_members or node_id in p_members:
                    valid_parents.append(p)

        # Extract address info from valid parents
        parent_address_records: List[Dict[str, Any]] = []
        for p in valid_parents:
            p_tags = p.get("tags", {})
            p_hn = p_tags.get("addr:housenumber") or p_tags.get("addr:housename") or p_tags.get("addr:unit")
            p_st = p_tags.get("addr:street") or p_tags.get("street")
            p_pc_raw = p_tags.get("addr:postcode") or p_tags.get("postal_code")
            p_pc = extract_uk_postcode(p_pc_raw) if p_pc_raw else None
            p_bld_name = p_tags.get("name") or p_tags.get("building:name")
            p_city = p_tags.get("addr:city")

            if p_hn or p_st or p_pc or p_bld_name:
                parent_address_records.append({
                    "parent_id": str(p.get("id", "")),
                    "parent_type": p.get("type", "way"),
                    "house_number": str(p_hn).strip() if p_hn else None,
                    "street": str(p_st).strip() if p_st else None,
                    "postcode": p_pc,
                    "city": str(p_city).strip() if p_city else None,
                    "building_name": str(p_bld_name).strip() if p_bld_name else None,
                    "tags": p_tags
                })

        # 4. Conflict & Ambiguity Detection
        conflict_status = "NO_CONFLICT"
        conflicting_data: Dict[str, Any] = {}
        inherited_parent: Optional[Dict[str, Any]] = None

        # A. Ambiguous Parent Check: multiple parents with conflicting address tags
        parents_with_addr = [pr for pr in parent_address_records if pr["street"] or pr["postcode"] or pr["house_number"]]
        if len(parents_with_addr) > 1:
            # Check if all address records agree
            distinct_streets = {pr["street"].lower() for pr in parents_with_addr if pr["street"]}
            distinct_pcs = {pr["postcode"].replace(" ", "").upper() for pr in parents_with_addr if pr["postcode"]}
            distinct_hns = {pr["house_number"].lower() for pr in parents_with_addr if pr["house_number"]}

            if len(distinct_streets) > 1 or len(distinct_pcs) > 1 or len(distinct_hns) > 1:
                conflict_status = "AMBIGUOUS_ADDRESS"
                conflicting_data = {"competing_parent_addresses": parents_with_addr}
            else:
                inherited_parent = parents_with_addr[0]
        elif len(parents_with_addr) == 1:
            inherited_parent = parents_with_addr[0]

        # B. Conflict Check: Direct POI tags vs Parent tags
        if inherited_parent and conflict_status == "NO_CONFLICT":
            p_st = inherited_parent["street"]
            p_pc = inherited_parent["postcode"]
            p_hn = inherited_parent["house_number"]

            is_conflict = False
            # Check street divergence
            if direct_st and p_st:
                norm_d_st = re.sub(r'[^a-z0-9]', '', str(direct_st).lower())
                norm_p_st = re.sub(r'[^a-z0-9]', '', str(p_st).lower())
                if norm_d_st != norm_p_st and norm_d_st not in norm_p_st and norm_p_st not in norm_d_st:
                    is_conflict = True
            # Check postcode divergence
            if direct_pc and p_pc:
                if direct_pc.replace(" ", "").upper() != p_pc.replace(" ", "").upper():
                    is_conflict = True
            # Check house number divergence
            if direct_hn and p_hn:
                if str(direct_hn).strip().lower() != str(p_hn).strip().lower():
                    is_conflict = True

            if is_conflict:
                conflict_status = "ADDRESS_CONFLICT"
                conflicting_data = {
                    "poi_direct_address": {
                        "house_number": str(direct_hn).strip() if direct_hn else None,
                        "street": str(direct_st).strip() if direct_st else None,
                        "postcode": direct_pc
                    },
                    "parent_way_address": {
                        "parent_id": inherited_parent["parent_id"],
                        "house_number": p_hn,
                        "street": p_st,
                        "postcode": p_pc
                    }
                }

        # 5. Resolve Final Values with Strict Provenance
        final_hn: Optional[str] = None
        hn_source = "NONE"
        final_st: Optional[str] = None
        st_source = "NONE"
        final_pc: Optional[str] = None
        pc_source = "NONE"
        parent_id_used: Optional[str] = None
        final_branch: Optional[str] = None

        if direct_hn:
            final_hn = str(direct_hn).strip()
            hn_source = "OSM_DIRECT_TAG"
            provenance["house_number"] = "OSM_TAG_ADDR_HOUSENUMBER"
        elif inherited_parent and inherited_parent["house_number"] and conflict_status == "NO_CONFLICT":
            final_hn = inherited_parent["house_number"]
            hn_source = "OSM_PARENT_WAY"
            parent_id_used = inherited_parent["parent_id"]
            provenance["house_number"] = f"OSM_PARENT_WAY_{parent_id_used}"

        if direct_st:
            final_st = str(direct_st).strip()
            st_source = "OSM_DIRECT_TAG"
            provenance["street"] = "OSM_TAG_ADDR_STREET"
        elif inherited_parent and inherited_parent["street"] and conflict_status == "NO_CONFLICT":
            final_st = inherited_parent["street"]
            st_source = "OSM_PARENT_WAY"
            parent_id_used = inherited_parent["parent_id"]
            provenance["street"] = f"OSM_PARENT_WAY_{parent_id_used}"

        if direct_pc:
            final_pc = direct_pc
            pc_source = "OSM_DIRECT_TAG"
            provenance["postcode"] = "OSM_TAG_ADDR_POSTCODE"
        elif inherited_parent and inherited_parent["postcode"] and conflict_status == "NO_CONFLICT":
            final_pc = inherited_parent["postcode"]
            pc_source = "OSM_PARENT_WAY"
            parent_id_used = inherited_parent["parent_id"]
            provenance["postcode"] = f"OSM_PARENT_WAY_{parent_id_used}"

        # Branch resolution
        if direct_branch:
            final_branch = str(direct_branch).strip()
            provenance["branch_identifier"] = "OSM_TAG_BRANCH"
        elif inherited_parent and inherited_parent["building_name"] and conflict_status == "NO_CONFLICT":
            final_branch = inherited_parent["building_name"]
            provenance["branch_identifier"] = f"OSM_PARENT_BUILDING_NAME_{inherited_parent['parent_id']}"
        else:
            name_val = tags.get("name", "")
            final_branch = cls.extract_branch_marker(f"{name_val} {direct_city}")
            if final_branch:
                provenance["branch_identifier"] = "NAME_SUB_LOCALITY"

        # City resolution
        city_val = direct_city
        if inherited_parent and inherited_parent["city"] and tags.get("addr:city") is None:
            city_val = inherited_parent["city"]
            provenance["city"] = f"OSM_PARENT_WAY_CITY_{inherited_parent['parent_id']}"
        else:
            provenance["city"] = "OSM_TAG_ADDR_CITY" if tags.get("addr:city") else "DEFAULT_CITY"

        # Build combined address
        addr_parts = [p for p in [final_hn, final_st, city_val, final_pc, target_country] if p]
        full_addr = ", ".join(addr_parts) if addr_parts else f"{city_val}, {target_country}"

        profile = AddressProfile(
            raw_address=full_addr,
            normalized_address=full_addr,
            house_number=final_hn,
            street=final_st,
            postcode=final_pc,
            city=city_val,
            country=target_country,
            latitude=cand_lat,
            longitude=cand_lon,
            branch_identifier=final_branch,
            provenance=provenance,
            street_source=st_source,
            house_number_source=hn_source,
            postcode_source=pc_source,
            parent_osm_id=parent_id_used,
            address_conflict_status=conflict_status,
            conflicting_address_data=conflicting_data
        )
        profile.completeness = cls.classify_completeness(profile)
        return profile

    @classmethod
    def parse_osm_tags(
        cls,
        tags: Dict[str, Any],
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        parent_elements: Optional[List[Dict[str, Any]]] = None
    ) -> AddressProfile:
        """
        Extracts structured address profile directly from OSM tags.
        Maintains backward compatibility with Phase 7.7 callers while supporting parent elements.
        """
        return cls.resolve_osm_address(
            element={"tags": tags},
            parent_elements=parent_elements,
            lat=lat,
            lon=lon
        )

    @classmethod
    def parse_address_string(
        cls,
        address: str,
        city: str = "Manchester",
        country: str = "United Kingdom",
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        company_name: str = ""
    ) -> AddressProfile:
        """
        Parses a free-text UK address string into structured components.
        Example: '367, Palatine Road, Manchester, M22 4FY'
        -> house_number: '367', street: 'Palatine Road', city: 'Manchester', postcode: 'M22 4FY'.
        """
        raw = (address or "").strip()
        provenance: Dict[str, str] = {}

        if not raw or raw.lower() in ["manchester", "manchester, united kingdom", "united kingdom"]:
            # Minimal address
            profile = AddressProfile(
                raw_address=raw or f"{city}, {country}",
                normalized_address=f"{city}, {country}",
                city=city,
                country=country,
                latitude=lat,
                longitude=lon,
                branch_identifier=cls.extract_branch_marker(f"{company_name} {raw}"),
                completeness=AddressCompleteness.PARTIAL if (lat is not None and lon is not None) else AddressCompleteness.MINIMAL,
                provenance={"address": "MINIMAL_CITY_FALLBACK"},
                street_source="NONE",
                house_number_source="NONE",
                postcode_source="NONE",
                address_conflict_status="NO_CONFLICT"
            )
            return profile

        # Extract Postcode
        postcode = extract_uk_postcode(raw)
        if postcode:
            provenance["postcode"] = "STRING_REGEX_POSTCODE"

        # Remove country and postcode for street/house parsing
        clean_text = raw
        if postcode:
            clean_text = clean_text.replace(postcode, "")
        clean_text = re.sub(r'\b(united kingdom|uk|england)\b', '', clean_text, flags=re.IGNORECASE)
        clean_text = clean_text.strip().strip(",")

        # Split into tokens/segments by comma
        segments = [s.strip() for s in clean_text.split(",") if s.strip()]

        house_number: Optional[str] = None
        street: Optional[str] = None
        detected_city: str = city
        branch_id = cls.extract_branch_marker(f"{company_name} {raw}")

        for seg in segments:
            # Check if segment contains city
            if seg.lower() in ["manchester", "greater manchester", "birmingham", "leeds", "london"]:
                detected_city = seg.strip()
                continue

            # Check if segment starts with house/unit number
            hn_m = re.match(r'^(?:unit\s+)?(\d+[a-z]?|\d+-\d+)\s*(.*)$', seg, re.IGNORECASE)
            if hn_m:
                hn_val = hn_m.group(1).strip()
                rest = hn_m.group(2).strip()
                if not house_number:
                    house_number = hn_val
                    provenance["house_number"] = "STRING_REGEX_HOUSENUMBER"
                if rest and not street:
                    street = rest
                    provenance["street"] = "STRING_SEGMENT_STREET"
                continue

            # Check if segment matches a known street type
            words = seg.split()
            if any(w.lower() in UK_STREET_TYPES for w in words):
                if not street:
                    street = seg
                    provenance["street"] = "STRING_SUFFIX_STREET"
                continue

            # Check if segment is a known thoroughfare name without standard suffix (e.g. 'Deansgate')
            if seg.lower() in ["deansgate", "portway", "hall lane", "ledson road", "market street", "mosley street"]:
                if not street:
                    street = seg
                    provenance["street"] = "STRING_KNOWN_STREET"
                continue

        # Build normalized address
        addr_components = [p for p in [house_number, street, detected_city, postcode, country] if p]
        norm_address = ", ".join(addr_components) if addr_components else f"{city}, {country}"

        profile = AddressProfile(
            raw_address=raw,
            normalized_address=norm_address,
            house_number=house_number,
            street=street,
            postcode=postcode,
            city=detected_city,
            country=country,
            latitude=lat,
            longitude=lon,
            branch_identifier=branch_id,
            provenance=provenance,
            street_source="STRING_EXTRACTED" if street else "NONE",
            house_number_source="STRING_EXTRACTED" if house_number else "NONE",
            postcode_source="STRING_EXTRACTED" if postcode else "NONE",
            address_conflict_status="NO_CONFLICT"
        )
        profile.completeness = cls.classify_completeness(profile)
        return profile

    @classmethod
    def extract_branch_marker(cls, text: Optional[str]) -> Optional[str]:
        """
        Extracts verified branch identifiers without fabricating evidence.
        Examples: 'Terminal 2', 'Terminal 3', 'Barton Arcade', 'Northenden', 'Angel Gardens'.
        """
        if not text:
            return None
        t_low = text.lower()

        # 1. Airport Terminals
        term_m = re.search(r'\b(terminal\s*[1-3]|t[1-3])\b', t_low)
        if term_m:
            raw_term = term_m.group(1).upper()
            if "T" in raw_term and "TERMINAL" not in raw_term:
                return f"Terminal {raw_term[1]}"
            return raw_term.title()

        # 2. Arcades & Shopping Centres
        for marker in ["barton arcade", "arndale centre", "trafford centre", "corn exchange", "royal exchange"]:
            if marker in t_low:
                return marker.title()

        # 3. Specific Sub-localities
        for loc in ["angel gardens", "northern quarter", "northenden", "wythenshawe", "fallowfield", "didsbury", "chorlton", "cheetham hill", "withington"]:
            if loc in t_low:
                return loc.title()

        return None

    @classmethod
    def classify_completeness(cls, profile: AddressProfile) -> AddressCompleteness:
        """
        Computes address completeness grade:
          COMPLETE: street + postcode + city + coordinates
          STRONG: street + city + coordinates
          PARTIAL: city + coordinates only (or street + city without coordinates/postcode)
          MINIMAL: city only
          UNKNOWN: missing city
        """
        has_city = bool(profile.city and len(profile.city.strip()) > 1)
        has_street = bool(profile.street and len(profile.street.strip()) > 2)
        has_postcode = bool(profile.postcode and len(profile.postcode.strip()) >= 5)
        has_coords = bool(profile.latitude is not None and profile.longitude is not None)

        if not has_city:
            return AddressCompleteness.UNKNOWN

        if has_street and has_postcode and has_city and has_coords:
            return AddressCompleteness.COMPLETE
        elif has_street and has_city and has_coords:
            return AddressCompleteness.STRONG
        elif has_city and (has_coords or has_street):
            return AddressCompleteness.PARTIAL
        else:
            return AddressCompleteness.MINIMAL

    @classmethod
    def is_gosom_safe_to_query(cls, profile: AddressProfile) -> Tuple[bool, str]:
        """
        Section 9 & Section 5:
        If address conflict or ambiguity exists:
          BLOCKED: ADDRESS_CONFLICT / AMBIGUOUS_ADDRESS
        If address completeness is insufficient (neither street nor postcode):
          GOSOM_NOT_SAFE_TO_QUERY rather than guessing.
        """
        if profile.address_conflict_status == "ADDRESS_CONFLICT":
            return False, "BLOCKED: ADDRESS_CONFLICT (POI address tags contradict parent way tags)"
        if profile.address_conflict_status == "AMBIGUOUS_ADDRESS":
            return False, "BLOCKED: AMBIGUOUS_ADDRESS (multiple conflicting parent addresses)"

        has_street = bool(profile.street and len(profile.street.strip()) > 2)
        has_postcode = bool(profile.postcode and len(profile.postcode.strip()) >= 5)

        if has_street or has_postcode:
            return True, "SAFE_TO_QUERY"
        else:
            return False, "GOSOM_NOT_SAFE_TO_QUERY: INSUFFICIENT_ADDRESS_COMPLETENESS (missing street and postcode)"

    @classmethod
    def generate_location_query(cls, company_name: str, profile: AddressProfile) -> Optional[str]:
        """
        Section 10: Location-specific Gosom query construction hierarchy:
          1. "<name>" "<street>" "<postcode>" "<city>"
          2. If postcode unavailable: "<name>" "<street>" "<city>"
          3. If street unavailable: "<name>" "<postcode>" "<city>"
          4. If neither: None (do NOT call Gosom automatically).
        """
        safe, _ = cls.is_gosom_safe_to_query(profile)
        if not safe:
            return None

        name = company_name.strip()
        city = profile.city.strip()
        street = profile.street.strip() if profile.street else None
        postcode = profile.postcode.strip() if profile.postcode else None

        if street and postcode:
            return f'"{name}" "{street}" "{postcode}" "{city}"'
        elif street:
            return f'"{name}" "{street}" "{city}"'
        elif postcode:
            return f'"{name}" "{postcode}" "{city}"'
        return None
