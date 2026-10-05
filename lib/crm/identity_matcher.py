"""
Dripp Media — Global CRM Business Identity Matcher & Deduplication Engine V2
=============================================================================
Implements global, multi-attribute, confidence-based entity resolution:
  - Protects CRM (Google Sheets LEADS / REVIEW_QUEUE / RESEARCH_LOG) from duplicate leads.
  - Distinctive token analysis eliminates false duplicate merging caused by generic
    hospitality words (Pizza, Coffee, House, Club, Sandwich, Bar, etc.).
  - Country-neutral location logic treats street/postcode disparities and geographic
    separation as negative evidence against merging, preserving distinct branches.
  - Multi-signal requirement prevents weak single signals from triggering POSSIBLE_DUPLICATE.
  - Categorizes comparisons into 4 deterministic outcomes:
      1. NEW_BUSINESS: Distinct business or different branch -> safe to insert as new record.
      2. EXISTING_BUSINESS: High confidence match -> update enrichment in-place, preserve lead_id and outreach history.
      3. POSSIBLE_DUPLICATE: Ambiguous or partial match -> route to REVIEW_QUEUE, never create duplicate lead.
      4. CONFLICT: Materially contradictory signals -> preserve both records with explicit CONFLICT flag.
  - Enforces duplicate outreach safety: (business_identity_id + campaign + channel) idempotency.
"""

import re
import math
import difflib
import unicodedata
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple, Set


class IdentityMatchOutcome(str, Enum):
    NEW_BUSINESS = "NEW_BUSINESS"
    EXISTING_BUSINESS = "EXISTING_BUSINESS"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    CONFLICT = "CONFLICT"


@dataclass
class IdentityMatchResult:
    outcome: str  # IdentityMatchOutcome value
    confidence: float
    matched_lead_id: Optional[str] = None
    matched_lead: Optional[Dict[str, Any]] = None
    match_reasons: List[str] = field(default_factory=list)
    identity_key: str = ""
    conflict_notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "confidence": self.confidence,
            "matched_lead_id": self.matched_lead_id,
            "match_reasons": self.match_reasons,
            "identity_key": self.identity_key,
            "conflict_notes": self.conflict_notes
        }


# Global Stopwords across multiple languages (articles, conjunctions, prepositions)
STOPWORDS = {
    # English
    "the", "a", "an", "and", "of", "in", "at", "on", "by", "for", "with", "to", "from",
    # Romance / Germanic / Latin / Arabic articles & particles
    "el", "la", "le", "les", "los", "las", "il", "lo", "gli", "al", "del", "della", "delle", "dei",
    "de", "du", "des", "der", "die", "das", "ein", "eine", "einer", "eines", "dem", "den"
}

# Standard corporate and business legal suffixes across international jurisdictions
LEGAL_SUFFIXES = {
    "ltd", "limited", "llc", "inc", "incorporated", "corp", "corporation", "co", "company",
    "gmbh", "ag", "sa", "srl", "bv", "nv", "plc", "cic", "holdings", "group", "services",
    "enterprises", "partner", "partners"
}

LEGAL_SUFFIXES_REGEX = re.compile(
    r'\b(?:ltd|limited|llc|inc|incorporated|corp|corporation|co|company|'
    r'gmbh|ag|sa|srl|bv|nv|plc|cic|holdings|group|services|enterprises)\b',
    re.IGNORECASE
)

# Common global hospitality / venue / trade descriptors
GLOBAL_GENERIC_TOKENS = {
    # Venue / trade terms
    "cafe", "café", "restaurant", "bar", "pub", "club", "house", "kitchen", "bistro",
    "lounge", "deli", "delicatessen", "pizzeria", "bakery", "canteen", "eatery", "diner",
    "takeaway", "inn", "hotel", "tavern", "rooms", "room", "shop", "store", "boutique",
    "center", "centre", "station", "base", "corner", "door", "spot", "place",
    # Category / broad food words
    "pizza", "coffee", "sandwich", "sandwiches", "grill", "food", "tea",
    "fisheries", "wok"
}

INDUSTRY_TERMS_REGEX = re.compile(
    r'\b(?:restaurant|cafe|café|bar|pub|grill|kitchen|bistro|takeaway|'
    r'deli|pizzeria|bakery|canteen|eatery|lounge|inn|hotel|tavern)\b',
    re.IGNORECASE
)

# Platform domains that should never be used as unique business identity domains
PLATFORM_DOMAINS = {
    "instagram.com", "facebook.com", "tiktok.com", "twitter.com", "x.com",
    "google.com", "goo.gl", "maps.google.com", "tripadvisor.com", "tripadvisor.co.uk",
    "yelp.com", "yelp.co.uk", "deliveroo.co.uk", "deliveroo.com", "ubereats.com",
    "just-eat.co.uk", "opentable.com", "opentable.co.uk", "restaurantguru.com"
}


def clean_ascii_text(text: str) -> str:
    """Normalizes accented/diacritic characters to clean lowercase ASCII."""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKD", str(text))
    return norm.encode("ASCII", "ignore").decode("utf-8").lower().strip()


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes great-circle distance in meters between two coordinates."""
    R = 6371000.0  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0)**2
    return 2.0 * R * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def extract_tokens(name: str) -> Tuple[List[str], List[str]]:
    """
    Extracts non-stopword tokens and distinctive tokens from a business name.
    Preserves acronyms (e.g. W R V S -> wrvs).
    """
    if not name:
        return [], []
    clean = clean_ascii_text(name)
    words = re.findall(r"[a-z0-9]+", clean)
    tokens = []
    single_run = []
    for w in words:
        if len(w) == 1:
            single_run.append(w)
        else:
            if len(single_run) >= 3:
                tokens.append("".join(single_run))
            elif single_run:
                tokens.extend(single_run)
            single_run = []
            tokens.append(w)
    if len(single_run) >= 3:
        tokens.append("".join(single_run))
    elif single_run:
        tokens.extend(single_run)

    non_stop = [t for t in tokens if t not in STOPWORDS and t not in LEGAL_SUFFIXES]
    distinctive = [t for t in non_stop if t not in GLOBAL_GENERIC_TOKENS]
    return non_stop, distinctive


class BusinessIdentityMatcher:
    """
    Global confidence-based identity matcher for CRM deduplication V2.
    Works country-neutrally across international jurisdictions.
    """

    def __init__(self, high_confidence_threshold: float = 0.85, possible_dup_threshold: float = 0.55):
        self.high_threshold = high_confidence_threshold
        self.possible_threshold = possible_dup_threshold

    @staticmethod
    def normalize_name(name: str) -> str:
        """Strips punctuation, legal suffixes, industry descriptors, and whitespace."""
        if not name:
            return ""
        s = clean_ascii_text(name)
        s = LEGAL_SUFFIXES_REGEX.sub("", s)
        s = INDUSTRY_TERMS_REGEX.sub("", s)
        s = re.sub(r'[^a-z0-9]', '', s)
        return s

    @staticmethod
    def normalize_name_tokens(name: str) -> List[str]:
        """Returns sorted, cleaned list of non-trivial word tokens."""
        if not name:
            return []
        non_stop, _ = extract_tokens(name)
        return sorted(non_stop)

    @staticmethod
    def normalize_country(country: str) -> str:
        """Standardizes country representations globally."""
        if not country:
            return ""
        c = country.strip().lower()
        mapping = {
            "united kingdom": "gb", "uk": "gb", "great britain": "gb", "england": "gb", "scotland": "gb", "wales": "gb",
            "united states": "us", "usa": "us", "united states of america": "us",
            "germany": "de", "deutschland": "de",
            "united arab emirates": "ae", "uae": "ae", "dubai": "ae"
        }
        return mapping.get(c, re.sub(r'[^a-z0-9]', '', c))

    @staticmethod
    def normalize_city(city: str) -> str:
        """Standardizes city or locality."""
        if not city:
            return ""
        return re.sub(r'[^a-z0-9]', '', clean_ascii_text(city))

    @staticmethod
    def normalize_phone(phone: str) -> str:
        """Extracts digit sequence, keeping significant terminal digits (last 9 digits)."""
        if not phone:
            return ""
        digits = re.sub(r'[^\d]', '', phone)
        # Strip leading national trunk 0 if 11 digits (e.g. UK 07... -> 7...)
        if len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        # Use last 9 digits for cross-format comparison (e.g. +44 7745 527603 vs 07745 527603)
        return digits[-9:] if len(digits) >= 9 else digits

    @staticmethod
    def extract_clean_domain(website: str) -> Optional[str]:
        """Extracts clean registrable domain from URL, ignoring generic platform hosts."""
        if not website:
            return None
        w = website.lower().strip()
        for p in PLATFORM_DOMAINS:
            if p in w:
                return None
        clean = re.sub(r'^https?://', '', w)
        clean = re.sub(r'^www\.', '', clean)
        clean = clean.split('/')[0].split('?')[0].split(':')[0].strip()
        return clean if clean and '.' in clean else None

    @staticmethod
    def extract_address_components(address: str = "", street: str = "", postcode: str = "", city: str = "") -> Dict[str, Any]:
        """
        Global country-neutral address component extractor.
        Extracts street numbers, key street name tokens, and full/area postal codes.
        """
        combined = f"{address} {street} {postcode}".lower()
        clean_comb = clean_ascii_text(combined)
        numbers = set(re.findall(r'\b\d+[a-z]?\b', clean_comb))
        pc_clean = re.sub(r'[^a-z0-9]', '', clean_ascii_text(postcode)) if postcode else ""

        words = re.findall(r'[a-z]+', clean_ascii_text(f"{address} {street}"))
        road_types = {
            "st", "street", "rd", "road", "ave", "avenue", "ln", "lane", "cl", "close",
            "dr", "drive", "way", "blvd", "boulevard", "sq", "square", "ct", "court",
            "pl", "place", "ter", "terrace", "pkwy", "parkway", "str", "strasse",
            "rue", "via", "calle", "near", "unit", "floor", "suite", "apt", "apartment",
            "uk", "united", "kingdom"
        }
        if city:
            road_types.update(re.findall(r'[a-z]+', clean_ascii_text(city)))

        street_tokens = {w for w in words if len(w) > 2 and w not in road_types}

        return {
            "numbers": numbers,
            "street_tokens": street_tokens,
            "full_postcode": pc_clean,
            "pc_prefix": pc_clean[:4] if len(pc_clean) >= 4 else pc_clean
        }

    def generate_identity_key(
        self,
        company_name: str,
        country: str,
        city: str,
        street: str = "",
        postcode: str = "",
        website: str = "",
        phone: str = ""
    ) -> str:
        """
        Generates a composite, deterministic identity key for exact indexing.
        """
        domain = self.extract_clean_domain(website)
        norm_country = self.normalize_country(country)
        norm_city = self.normalize_city(city)

        if domain:
            return f"dom:{domain}@{norm_country}"

        norm_name = self.normalize_name(company_name)
        norm_phone = self.normalize_phone(phone)
        addr = self.extract_address_components("", street=street, postcode=postcode)

        num_part = sorted(list(addr["numbers"]))[0] if addr["numbers"] else ""
        pc_part = addr["full_postcode"] or addr["pc_prefix"]

        if norm_phone and len(norm_phone) >= 7:
            return f"phone:{norm_name}@{norm_phone}@{norm_city}"

        if num_part and pc_part:
            return f"loc:{norm_name}@{num_part}_{pc_part}@{norm_city}@{norm_country}"

        if pc_part:
            return f"pc:{norm_name}@{pc_part}@{norm_city}@{norm_country}"

        return f"biz:{norm_name}@{norm_city}@{norm_country}"

    def compute_name_similarity(self, cand_name: str, exist_name: str) -> Tuple[float, List[str], bool]:
        """
        Evaluates business name similarity using distinctive tokens.
        Generic category/venue terms alone NEVER produce a positive match.
        Returns: (name_score, reasons, is_compatible)
        """
        c_clean = clean_ascii_text(cand_name)
        e_clean = clean_ascii_text(exist_name)
        if not c_clean or not e_clean:
            return 0.0, [], False

        c_alphanumeric = re.sub(r'[^a-z0-9]', '', c_clean)
        e_alphanumeric = re.sub(r'[^a-z0-9]', '', e_clean)
        if c_alphanumeric == e_alphanumeric and c_alphanumeric:
            return 1.0, ["EXACT_NAME_MATCH"], True

        c_non_stop, c_dist = extract_tokens(cand_name)
        e_non_stop, e_dist = extract_tokens(exist_name)

        c_norm_str = "".join(c_non_stop)
        e_norm_str = "".join(e_non_stop)
        if c_norm_str == e_norm_str and c_norm_str:
            return 1.0, ["EXACT_NORMALIZED_NAME"], True

        c_dist_set = set(c_dist)
        e_dist_set = set(e_dist)

        if c_dist_set and e_dist_set:
            inter = c_dist_set.intersection(e_dist_set)
            union = c_dist_set.union(e_dist_set)
            jaccard = len(inter) / len(union) if union else 0.0
            overlap_ratio = len(inter) / max(len(c_dist_set), len(e_dist_set))

            c_d_str = " ".join(c_dist)
            e_d_str = " ".join(e_dist)
            seq_sim = difflib.SequenceMatcher(None, c_d_str, e_d_str).ratio()

            if c_dist_set == e_dist_set:
                return 1.0, ["EXACT_DISTINCTIVE_NAME_MATCH"], True

            if c_dist_set.issubset(e_dist_set) or e_dist_set.issubset(c_dist_set):
                return 0.90, [f"DISTINCTIVE_NAME_CONTAINMENT ({overlap_ratio:.2f})"], True

            if overlap_ratio >= 0.50:
                return overlap_ratio, [f"HIGH_DISTINCTIVE_SIMILARITY ({overlap_ratio:.2f})"], True

            if inter:
                return overlap_ratio, [f"PARTIAL_DISTINCTIVE_OVERLAP ({overlap_ratio:.2f})"], True

            if seq_sim >= 0.85:
                return seq_sim, [f"TYPO_FUZZY_NAME_MATCH ({seq_sim:.2f})"], True
            else:
                return 0.0, ["NO_DISTINCTIVE_TOKEN_OVERLAP"], False

        if (c_dist_set and not e_dist_set) or (e_dist_set and not c_dist_set):
            return 0.0, ["GENERIC_VS_DISTINCTIVE_MISMATCH"], False

        if not c_dist_set and not e_dist_set:
            c_ns_set = set(c_non_stop)
            e_ns_set = set(e_non_stop)
            if c_ns_set == e_ns_set and c_ns_set:
                return 0.85, ["GENERIC_EXACT_NAME_MATCH"], True
            else:
                return 0.0, ["DIFFERENT_GENERIC_TERMS"], False

        return 0.0, [], False

    def compute_similarity(
        self,
        cand: Dict[str, Any],
        existing: Dict[str, Any]
    ) -> Tuple[float, List[str], Optional[str]]:
        """
        Computes composite identity match score between candidate and existing record.
        Location serves as a discriminating signal, not a false duplicate trigger.
        Returns: (confidence_score, match_reasons, conflict_note)
        """
        reasons = []
        conflict = None

        # ── 1. Distinctive Name Similarity ──
        c_name = cand.get("company_name", "")
        e_name = existing.get("company_name", "")
        name_score, name_reasons, name_compatible = self.compute_name_similarity(c_name, e_name)
        reasons.extend(name_reasons)

        # ── 2. Country & City Check (Branch & Multi-Location Isolation) ──
        c_country = self.normalize_country(cand.get("target_country") or cand.get("country", ""))
        e_country = self.normalize_country(existing.get("target_country") or existing.get("country", ""))

        c_city = self.normalize_city(cand.get("city", ""))
        e_city = self.normalize_city(existing.get("city", ""))

        # If countries are specified and distinct -> Different businesses / branches
        if c_country and e_country and c_country != e_country:
            if name_score >= 0.70 or name_compatible:
                reasons.append("DISTINCT_BRANCH_DIFFERENT_COUNTRY")
            else:
                reasons.append("DIFFERENT_COUNTRY")
            return 0.0, reasons, None

        # If cities are specified and clearly distinct -> Different branch/location
        if c_city and e_city and c_city != e_city:
            if name_score >= 0.70 or name_compatible:
                reasons.append(f"DIFFERENT_CITY ({c_city} vs {e_city})")
                reasons.append("DISTINCT_BRANCH")
                return 0.05, reasons, None
            else:
                reasons.append(f"DIFFERENT_CITY ({c_city} vs {e_city})")
                return 0.0, reasons, None

        # ── 3. Verified Domain Match ──
        c_dom = self.extract_clean_domain(cand.get("website", ""))
        e_dom = self.extract_clean_domain(existing.get("website", ""))
        domain_matched = False
        if c_dom and e_dom:
            if c_dom == e_dom:
                if name_compatible:
                    reasons.append(f"EXACT_DOMAIN_MATCH ({c_dom})")
                    domain_matched = True
                    return 1.0, reasons, None
                else:
                    reasons.append(f"SHARED_PORTAL_OR_COMMUNITY_DOMAIN ({c_dom})")
            else:
                if name_score >= 0.85:
                    return 0.60, [f"CONFLICTING_OFFICIAL_DOMAINS ({c_dom} vs {e_dom})"], "Conflicting distinct business domains."
                else:
                    return 0.05, [f"DISTINCT_DOMAINS ({c_dom} vs {e_dom})"], None

        # ── 4. Phone Number Match ──
        c_phone = self.normalize_phone(cand.get("phone", ""))
        e_phone = self.normalize_phone(existing.get("phone", ""))
        phone_matched = False
        if c_phone and e_phone and len(c_phone) >= 7 and len(e_phone) >= 7:
            if c_phone == e_phone:
                if name_compatible:
                    phone_matched = True
                    reasons.append(f"PHONE_MATCH ({c_phone})")
                else:
                    return 0.55, reasons + [f"SAME_PHONE_DIFFERENT_NAME ({c_phone})"], "Same telephone number used by distinct business names."

        # ── 5. Location & Address Extraction ──
        c_addr = self.extract_address_components(
            cand.get("address", ""),
            cand.get("street", ""),
            cand.get("postcode", ""),
            city=cand.get("city", "")
        )
        e_addr = self.extract_address_components(
            existing.get("address", ""),
            existing.get("street", ""),
            existing.get("postcode", ""),
            city=existing.get("city", "")
        )

        number_match = bool(c_addr["numbers"] and e_addr["numbers"] and c_addr["numbers"].intersection(e_addr["numbers"]))
        number_conflict = bool(c_addr["numbers"] and e_addr["numbers"] and not c_addr["numbers"].intersection(e_addr["numbers"]))

        street_match = bool(c_addr["street_tokens"] and e_addr["street_tokens"] and c_addr["street_tokens"].intersection(e_addr["street_tokens"]))
        street_conflict = bool(c_addr["street_tokens"] and e_addr["street_tokens"] and not c_addr["street_tokens"].intersection(e_addr["street_tokens"]))

        exact_pc_match = bool(c_addr["full_postcode"] and e_addr["full_postcode"] and c_addr["full_postcode"] == e_addr["full_postcode"])
        pc_conflict = bool(c_addr["full_postcode"] and e_addr["full_postcode"] and c_addr["full_postcode"] != e_addr["full_postcode"])
        pc_prefix_match = bool(c_addr["pc_prefix"] and e_addr["pc_prefix"] and c_addr["pc_prefix"] == e_addr["pc_prefix"])

        # Coordinates / Geospatial distance
        c_lat = cand.get("lat") or cand.get("latitude")
        c_lon = cand.get("lon") or cand.get("lng") or cand.get("longitude")
        e_lat = existing.get("lat") or existing.get("latitude")
        e_lon = existing.get("lon") or existing.get("lng") or existing.get("longitude")

        geo_close = False
        geo_distant = False
        if c_lat is not None and c_lon is not None and e_lat is not None and e_lon is not None:
            try:
                geo_dist = haversine_distance(float(c_lat), float(c_lon), float(e_lat), float(e_lon))
                if geo_dist < 50.0:
                    geo_close = True
                    reasons.append(f"GEOPROXIMITY_VERY_CLOSE ({geo_dist:.0f}m)")
                elif geo_dist >= 250.0:
                    geo_distant = True
                    reasons.append(f"GEOPROXIMITY_DISTANT ({geo_dist:.0f}m)")
            except (ValueError, TypeError):
                pass

        if exact_pc_match:
            reasons.append("EXACT_POSTCODE_MATCH")
        elif pc_prefix_match:
            reasons.append(f"POSTCODE_AREA_MATCH ({c_addr['pc_prefix']})")

        if number_match:
            reasons.append("STREET_NUMBER_MATCH")
        if street_match:
            reasons.append("STREET_NAME_TOKEN_MATCH")

        # ── 6. Negative Location Evidence (Location as a Discriminating Signal) ──
        # If no strong identifier (phone or domain) matched:
        if not phone_matched and not domain_matched:
            # Different street numbers on same street -> Distinct branch if same brand
            if number_conflict and street_match:
                if name_score >= 0.70 or name_compatible:
                    reasons.append("DISTINCT_BRANCH_DIFFERENT_STREET_NUMBER")
                    return 0.10, reasons, None
                else:
                    reasons.append("DIFFERENT_BUSINESS_DIFFERENT_STREET_NUMBER")
                    return 0.05, reasons, None

            # Different streets (in same city or postcode area) -> Negative evidence against duplicate
            if street_conflict:
                if name_score >= 0.70 or name_compatible:
                    reasons.append("DISTINCT_BRANCH_DIFFERENT_STREET")
                    return 0.10, reasons, None
                else:
                    reasons.append("DIFFERENT_STREET_LOCATIONS")
                    return 0.15, reasons, None

            # Different postcodes with distant coordinates or without street corroboration
            if pc_conflict and (geo_distant or not street_match):
                reasons.append("DIFFERENT_POSTAL_LOCATIONS")
                return 0.15, reasons, None

            # Geographically distant (> 250m) with no exact street match
            if geo_distant and not street_match:
                reasons.append("GEOGRAPHICALLY_SEPARATED_LOCATIONS")
                return 0.15, reasons, None

        # ── 7. Composite Confidence Calculation ──
        score = 0.0

        if phone_matched:
            if name_score >= 0.70:
                score = 0.95
            elif name_score >= 0.40:
                score = 0.88
            else:
                score = 0.55

        if name_score >= 0.85:
            if number_match and exact_pc_match:
                score = max(score, 0.98)
            elif street_match and exact_pc_match:
                score = max(score, 0.96)
            elif number_match and street_match:
                score = max(score, 0.95)
            elif geo_close:
                score = max(score, 0.94)
            elif exact_pc_match:
                score = max(score, 0.92)
            elif street_match:
                score = max(score, 0.88)
            elif not c_addr["numbers"] and not e_addr["numbers"] and not c_addr["full_postcode"] and not e_addr["full_postcode"]:
                score = max(score, 0.88)
        elif name_score >= 0.50:
            if number_match and exact_pc_match:
                score = max(score, 0.90)
            elif street_match and exact_pc_match:
                score = max(score, 0.85)
            elif geo_close:
                score = max(score, 0.85)
            elif street_match and number_match:
                score = max(score, 0.85)
            elif phone_matched:
                score = max(score, 0.85)
            elif not street_conflict and not pc_conflict and (c_city == e_city) and not c_addr["full_postcode"] and not e_addr["full_postcode"]:
                score = max(score, 0.60)
            else:
                score = max(score, 0.20)
        else:
            score = max(score, 0.10)

        return score, reasons, conflict

    def match_candidate(
        self,
        candidate: Dict[str, Any],
        existing_leads: List[Dict[str, Any]]
    ) -> IdentityMatchResult:
        """
        Evaluates a candidate business against all known existing leads.
        Returns the best identity match result.
        """
        best_match = None
        best_score = 0.0
        best_reasons = []
        conflict_detected = None

        cand_key = self.generate_identity_key(
            company_name=candidate.get("company_name", ""),
            country=candidate.get("target_country") or candidate.get("country", ""),
            city=candidate.get("city", ""),
            street=candidate.get("street", ""),
            postcode=candidate.get("postcode", ""),
            website=candidate.get("website", ""),
            phone=candidate.get("phone", "")
        )

        for exist in existing_leads:
            score, reasons, conflict = self.compute_similarity(candidate, exist)
            if conflict and score >= 0.50:
                conflict_detected = conflict
                best_score = max(best_score, score)
                best_match = exist
                best_reasons = reasons
            elif score > best_score:
                best_score = score
                best_match = exist
                best_reasons = reasons

        if conflict_detected and best_score >= 0.50:
            return IdentityMatchResult(
                outcome=IdentityMatchOutcome.CONFLICT.value,
                confidence=best_score,
                matched_lead_id=str(best_match.get("lead_id", "")) if best_match else None,
                matched_lead=best_match,
                match_reasons=best_reasons,
                identity_key=cand_key,
                conflict_notes=conflict_detected
            )

        if best_score >= self.high_threshold and best_match:
            return IdentityMatchResult(
                outcome=IdentityMatchOutcome.EXISTING_BUSINESS.value,
                confidence=best_score,
                matched_lead_id=str(best_match.get("lead_id", "")),
                matched_lead=best_match,
                match_reasons=best_reasons,
                identity_key=cand_key
            )
        elif best_score >= self.possible_threshold and best_match:
            return IdentityMatchResult(
                outcome=IdentityMatchOutcome.POSSIBLE_DUPLICATE.value,
                confidence=best_score,
                matched_lead_id=str(best_match.get("lead_id", "")),
                matched_lead=best_match,
                match_reasons=best_reasons,
                identity_key=cand_key
            )
        else:
            return IdentityMatchResult(
                outcome=IdentityMatchOutcome.NEW_BUSINESS.value,
                confidence=best_score,
                identity_key=cand_key,
                match_reasons=best_reasons
            )

    @staticmethod
    def get_outreach_uniqueness_key(business_identity_id: str, campaign: str, channel: str) -> str:
        """
        Enforces outreach idempotency across campaigns and channels.
        Format: (business_identity_id + campaign + channel).
        """
        clean_id = str(business_identity_id).strip().lower()
        clean_camp = str(campaign).strip().lower() if campaign else "default"
        clean_chan = str(channel).strip().lower() if channel else "email"
        return f"{clean_id}:{clean_camp}:{clean_chan}"
