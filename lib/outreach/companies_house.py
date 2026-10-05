"""
Dripp Media — Companies House UK Entity Verification Layer
===========================================================
Sections 1, 2, 3, 4, 5, 6:
  - Connects to official Companies House API if credentials are configured
  - Fallback to official public directory lookup if API key is not configured
  - Multi-factor entity matching (Name, Postcode, Address, City, SIC code)
  - Distinguishes trading name from legal entity
  - Assesses company status (ACTIVE, DISSOLVED, LIQUIDATION, ADMINISTRATION, OTHER)
  - Scores match confidence (HIGH, MEDIUM, LOW, UNKNOWN)
  - Assigns match status (MATCHED, POSSIBLE_MATCH, NO_MATCH, AMBIGUOUS)
"""

import os
import re
import urllib.parse
import urllib.request
import json
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple


class EntityMatchStatus(str, Enum):
    MATCHED_ACTIVE    = "MATCHED_ACTIVE"
    MATCHED_DISSOLVED = "MATCHED_DISSOLVED"
    POSSIBLE_MATCH    = "POSSIBLE_MATCH"
    NO_MATCH          = "NO_MATCH"
    AMBIGUOUS         = "AMBIGUOUS"
    CONFLICT          = "CONFLICT"
    # Backward compatibility alias
    MATCHED           = "MATCHED_ACTIVE"


class EntitySource(str, Enum):
    OFFICIAL_API    = "OFFICIAL_API"
    PUBLIC_FALLBACK = "PUBLIC_FALLBACK"


class EntityMatchConfidence(str, Enum):
    HIGH    = "HIGH"
    MEDIUM  = "MEDIUM"
    LOW     = "LOW"
    UNKNOWN = "UNKNOWN"


class CompanyStatus(str, Enum):
    ACTIVE         = "ACTIVE"
    DISSOLVED      = "DISSOLVED"
    LIQUIDATION    = "LIQUIDATION"
    ADMINISTRATION = "ADMINISTRATION"
    OTHER          = "OTHER"


# Standard Hospitality / Restaurant / Food & Beverage SIC Codes
RELEVANT_FOOD_SIC_PREFIXES = [
    "5610",  # Restaurants and mobile food service activities (56101 licensed, 56102 unlic, 56103 takeaway)
    "5621",  # Event catering activities
    "5629",  # Other food service activities
    "5630",  # Beverage serving activities (pubs, bars)
    "4711",  # Retail sale in non-specialised stores with food/beverage
    "472",   # Retail sale of food, beverages
    "108",   # Manufacture of other food products
]

CORPORATE_COMPANY_TYPES = [
    "ltd",
    "private limited company",
    "private-limited-guarant-nfs",
    "private-limited-shares-section-30-exemption",
    "plc",
    "public limited company",
    "llp",
    "limited liability partnership",
    "limited-partnership",
    "royal-charter",
]


@dataclass
class CompaniesHouseRecord:
    trading_name: str = ""
    legal_entity_name: str = ""
    registered_name: str = ""  # alias for backward compat
    companies_house_number: str = ""
    company_type: str = ""
    company_status: str = ""
    registered_office: str = ""
    sic_codes: List[str] = field(default_factory=list)
    incorporation_date: str = ""
    last_confirmation_statement: str = ""
    last_accounts: str = ""
    companies_house_url: str = ""
    entity_source: str = EntitySource.PUBLIC_FALLBACK.value
    entity_match_status: str = EntityMatchStatus.NO_MATCH.value
    match_status: str = EntityMatchStatus.NO_MATCH.value  # alias
    entity_match_confidence: str = EntityMatchConfidence.UNKNOWN.value
    match_confidence: str = EntityMatchConfidence.UNKNOWN.value  # alias
    entity_match_reason: str = ""
    match_reason: str = ""  # alias
    is_corporate_subscriber: bool = False

    def __post_init__(self):
        # Synchronize aliases
        if not self.legal_entity_name and self.registered_name:
            self.legal_entity_name = self.registered_name
        if not self.registered_name and self.legal_entity_name:
            self.registered_name = self.legal_entity_name
        if not self.match_status or self.match_status == EntityMatchStatus.NO_MATCH.value:
            self.match_status = self.entity_match_status
        if not self.entity_match_status or self.entity_match_status == EntityMatchStatus.NO_MATCH.value:
            self.entity_match_status = self.match_status
        if not self.match_confidence or self.match_confidence == EntityMatchConfidence.UNKNOWN.value:
            self.match_confidence = self.entity_match_confidence
        if not self.entity_match_confidence or self.entity_match_confidence == EntityMatchConfidence.UNKNOWN.value:
            self.entity_match_confidence = self.match_confidence
        if not self.match_reason and self.entity_match_reason:
            self.match_reason = self.entity_match_reason
        if not self.entity_match_reason and self.match_reason:
            self.entity_match_reason = self.match_reason

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CompaniesHouseVerifier:
    """
    Modular Companies House UK Entity Verifier.
    Adheres strictly to backend-only credential security.
    """

    OFFICIAL_API_BASE = "https://api.company-information.service.gov.uk"
    PUBLIC_WEB_BASE = "https://find-and-update.company-information.service.gov.uk"

    @classmethod
    def get_api_key(cls) -> Optional[str]:
        return os.getenv("COMPANIES_HOUSE_API_KEY", "").strip() or None

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls.get_api_key())

    @classmethod
    def get_integration_status(cls) -> str:
        """Returns PASS if official API credentials exist, else NOT CONFIGURED."""
        return "PASS" if cls.is_configured() else "NOT CONFIGURED"

    # ──────────────────────────────────────────────────────────────────────────
    # Normalization & Matching Utilities
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def _normalize_name(cls, name: str) -> str:
        if not name:
            return ""
        s = name.lower()
        # Remove common corporate & trading noise
        s = re.sub(r"\b(limited|ltd|plc|llp|group|uk|the|bar|restaurant|pizzeria|cafe|kitchen|mcr|manchester)\b", " ", s)
        s = re.sub(r"[^a-z0-9 ]+", " ", s)
        return " ".join(s.split())

    @classmethod
    def _normalize_postcode(cls, pc: str) -> str:
        if not pc:
            return ""
        return re.sub(r"[^A-Z0-9]+", "", pc.upper().strip())

    @classmethod
    def _token_similarity(cls, a: str, b: str) -> float:
        norm_a = cls._normalize_name(a)
        norm_b = cls._normalize_name(b)
        tokens_a = set(norm_a.split())
        tokens_b = set(norm_b.split())
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = tokens_a.intersection(tokens_b)
        union = tokens_a.union(tokens_b)
        # Jaccard + subset bonus
        jaccard = len(intersection) / len(union)
        if tokens_a.issubset(tokens_b) or tokens_b.issubset(tokens_a):
            return max(jaccard, 0.85)
        return jaccard

    @classmethod
    def _is_food_sic(cls, sic_codes: List[str]) -> bool:
        for code in sic_codes:
            clean = str(code).strip()
            if any(clean.startswith(prefix) for prefix in RELEVANT_FOOD_SIC_PREFIXES):
                return True
        return False

    @classmethod
    def _is_corporate_type(cls, company_type: str) -> bool:
        t = str(company_type).strip().lower()
        return any(ct in t for ct in CORPORATE_COMPANY_TYPES)

    # ──────────────────────────────────────────────────────────────────────────
    # Search & Extraction
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def search_companies(cls, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Executes search against official Companies House API if key available,
        otherwise falls back to public web directory search.
        """
        if not query or not query.strip():
            return []

        api_key = cls.get_api_key()
        if api_key:
            return cls._search_api(query, api_key, limit)
        else:
            return cls._search_public(query, limit)

    @classmethod
    def _search_api(cls, query: str, api_key: str, limit: int) -> List[Dict[str, Any]]:
        import base64
        url = f"{cls.OFFICIAL_API_BASE}/search/companies?q={urllib.parse.quote(query)}&items_per_page={limit}"
        auth_bytes = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Basic {auth_bytes}",
            "User-Agent": "DrippMedia-ComplianceVerifier/1.0"
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            items = data.get("items", [])
            results = []
            for item in items:
                address_snippet = item.get("address_snippet", "")
                results.append({
                    "company_number": item.get("company_number", ""),
                    "title": item.get("title", ""),
                    "company_status": item.get("company_status", "").upper(),
                    "company_type": item.get("company_type", ""),
                    "address_snippet": address_snippet,
                    "date_of_creation": item.get("date_of_creation", ""),
                    "date_of_cessation": item.get("date_of_cessation", ""),
                    "sic_codes": item.get("sic_codes", []),
                })
            return results
        except Exception as e:
            # Fall back to public search if API error occurs
            return cls._search_public(query, limit)

    _PUBLIC_SEARCH_CACHE: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}

    @classmethod
    def _search_public(cls, query: str, limit: int) -> List[Dict[str, Any]]:
        cache_key = (query.strip().lower(), limit)
        if cache_key in cls._PUBLIC_SEARCH_CACHE:
            return list(cls._PUBLIC_SEARCH_CACHE[cache_key])
        url = f"{cls.PUBLIC_WEB_BASE}/search/companies?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8")
            items = re.findall(r"<li class=\"type-company\">.*?</li>", html, re.DOTALL)
            results = []
            for it in items[:limit]:
                m = re.search(r"<a class=\"govuk-link\" href=\"/company/([A-Z0-9]+)\"[^>]*>([^<]+)</a>", it)
                if not m:
                    continue
                cnum, cname = m.groups()
                meta = re.findall(r"<p[^>]*>(.*?)</p>", it, re.DOTALL)
                clean_meta = [re.sub(r"<[^>]+>", " ", p).strip() for p in meta]
                joined_meta = " ".join(clean_meta)

                # Status detection
                status = "ACTIVE"
                if "Dissolved on" in joined_meta:
                    status = "DISSOLVED"
                elif "Liquidation" in joined_meta:
                    status = "LIQUIDATION"
                elif "Administration" in joined_meta:
                    status = "ADMINISTRATION"

                results.append({
                    "company_number": cnum,
                    "title": cname.strip(),
                    "company_status": status,
                    "company_type": "ltd",  # default
                    "address_snippet": clean_meta[1] if len(clean_meta) > 1 else "",
                    "meta_text": joined_meta
                })
            cls._PUBLIC_SEARCH_CACHE[cache_key] = results
            return results
        except Exception:
            return []

    @classmethod
    def fetch_company_profile(cls, company_number: str) -> Optional[Dict[str, Any]]:
        """Fetches full profile for a specific company number."""
        if not company_number:
            return None

        api_key = cls.get_api_key()
        if api_key:
            import base64
            url = f"{cls.OFFICIAL_API_BASE}/company/{company_number}"
            auth_bytes = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
            req = urllib.request.Request(url, headers={
                "Authorization": f"Basic {auth_bytes}",
                "User-Agent": "DrippMedia-ComplianceVerifier/1.0"
            })
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception:
                pass

        # Fallback to public web profile
        url = f"{cls.PUBLIC_WEB_BASE}/company/{company_number}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8")
            title_m = re.search(r"<h1[^>]*class=\"heading-xlarge\"[^>]*>(.*?)</h1>", html, re.DOTALL)
            status_m = re.search(r"<dd[^>]*id=\"company-status\"[^>]*>(.*?)</dd>", html, re.DOTALL)
            type_m = re.search(r"<dd[^>]*id=\"company-type\"[^>]*>(.*?)</dd>", html, re.DOTALL)
            inc_m = re.search(r"<dd[^>]*id=\"company-creation-date\"[^>]*>(.*?)</dd>", html, re.DOTALL)
            sic_m = re.findall(r"<span id=\"sic[0-9]+\"[^>]*>(.*?)</span>", html, re.DOTALL)
            ro_m = re.search(r"<dd class=\"text address-summary\"[^>]*>(.*?)</dd>", html, re.DOTALL)

            name = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
            status_raw = re.sub(r"<[^>]+>", "", status_m.group(1)).strip() if status_m else "Active"
            status = "DISSOLVED" if "dissolved" in status_raw.lower() else ("ACTIVE" if "active" in status_raw.lower() else "OTHER")
            ctype = re.sub(r"<[^>]+>", "", type_m.group(1)).strip() if type_m else "Private limited Company"
            inc = re.sub(r"<[^>]+>", "", inc_m.group(1)).strip() if inc_m else ""
            sics = [re.sub(r"<[^>]+>", "", s).strip() for s in sic_m]
            ro = re.sub(r"<[^>]+>", " ", ro_m.group(1)).strip() if ro_m else ""

            return {
                "company_number": company_number,
                "company_name": name,
                "company_status": status,
                "type": ctype,
                "date_of_creation": inc,
                "sic_codes": sics,
                "registered_office_address": {"address_line_1": ro},
                "registered_office_snippet": ro
            }
        except Exception:
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Multi-Factor Entity Matching Engine (Section 3, 4, 5, 6)
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def verify_entity(cls, lead: Dict[str, Any]) -> CompaniesHouseRecord:
        """
        Main entity matching pipeline.
        Returns a structured CompaniesHouseRecord containing full entity status,
        match confidence, and corporate subscriber determination.
        """
        company_name = str(lead.get("company_name", "") or "").strip()
        address = str(lead.get("address", "") or "").strip()
        postcode = str(lead.get("postcode", "") or "").strip()
        city = str(lead.get("city", "") or "Manchester").strip()

        if not company_name:
            return CompaniesHouseRecord(
                match_status=EntityMatchStatus.NO_MATCH.value,
                match_confidence=EntityMatchConfidence.UNKNOWN.value,
                match_reason="No business name provided for entity lookup"
            )

        # 1. Search queries: search name, and also search name + city if city provided (Section 3)
        api_key = cls.get_api_key()
        entity_source = EntitySource.OFFICIAL_API.value if api_key else EntitySource.PUBLIC_FALLBACK.value

        candidates = cls.search_companies(company_name, limit=6)
        if city and city.lower() not in company_name.lower():
            loc_cands = cls.search_companies(f"{company_name} {city}", limit=6)
            candidates.extend(loc_cands)

        # Deduplicate candidates by company number
        seen_nums = set()
        unique_candidates = []
        for c in candidates:
            cnum = c.get("company_number")
            if cnum and cnum not in seen_nums:
                seen_nums.add(cnum)
                unique_candidates.append(c)
        candidates = unique_candidates

        if not candidates:
            return CompaniesHouseRecord(
                trading_name=company_name,
                legal_entity_name="",
                registered_name="",
                entity_source=entity_source,
                entity_match_status=EntityMatchStatus.NO_MATCH.value,
                match_status=EntityMatchStatus.NO_MATCH.value,
                entity_match_confidence=EntityMatchConfidence.UNKNOWN.value,
                match_confidence=EntityMatchConfidence.UNKNOWN.value,
                entity_match_reason=f"No Companies House records found matching '{company_name}'",
                match_reason=f"No Companies House records found matching '{company_name}'"
            )

        norm_lead_pc = cls._normalize_postcode(postcode)
        norm_lead_street = address.split(",")[0].strip().lower() if address else ""

        scored_candidates = []

        for cand in candidates:
            cand_name = cand.get("title") or cand.get("company_name") or ""
            cand_num = cand.get("company_number", "")
            cand_status = str(cand.get("company_status", "ACTIVE")).upper()
            cand_addr = cand.get("address_snippet", "")

            # 1. Name Similarity
            name_sim = cls._token_similarity(company_name, cand_name)

            # 2. Postcode Match
            norm_cand_pc = cls._normalize_postcode(cand_addr)
            pc_match = False
            outcode_match = False
            if norm_lead_pc and norm_cand_pc:
                if norm_lead_pc in norm_cand_pc or norm_cand_pc in norm_lead_pc:
                    pc_match = True
                elif norm_lead_pc[:3] == norm_cand_pc[:3]:
                    outcode_match = True

            # 3. Street Address Match
            street_match = False
            if norm_lead_street and len(norm_lead_street) > 4:
                # e.g. "Upper Brook", "Lever St", "Wilmslow"
                key_words = [w for w in re.split(r"[^a-z0-9]+", norm_lead_street) if len(w) > 3 and w not in ["street", "road", "lane"]]
                if any(w in cand_addr.lower() for w in key_words):
                    street_match = True

            # 4. City Match
            city_match = city.lower() in cand_addr.lower() if city else False

            score = name_sim * 0.4
            if pc_match:
                score += 0.35
            elif street_match:
                score += 0.25
            elif outcode_match:
                score += 0.15

            if city_match:
                score += 0.20

            scored_candidates.append({
                "candidate": cand,
                "score": score,
                "name_sim": name_sim,
                "pc_match": pc_match,
                "street_match": street_match,
                "city_match": city_match,
                "status": cand_status
            })

        # Sort by match score descending
        scored_candidates.sort(key=lambda x: x["score"], reverse=True)
        top = scored_candidates[0]

        best_cand = top["candidate"]
        cand_num = best_cand.get("company_number", "")
        cand_name = best_cand.get("title") or best_cand.get("company_name") or ""
        score = top["score"]

        # Fetch detailed profile to verify SIC codes & active status
        profile = cls.fetch_company_profile(cand_num) or best_cand
        sic_codes = profile.get("sic_codes", [])
        is_food_sic = cls._is_food_sic(sic_codes)
        company_status = str(profile.get("company_status", best_cand.get("company_status", "ACTIVE"))).upper()
        company_type = str(profile.get("type", best_cand.get("company_type", "ltd"))).lower()
        is_corporate_type = cls._is_corporate_type(company_type)

        reg_office = profile.get("registered_office_snippet") or (
            ", ".join(v for v in profile.get("registered_office_address", {}).values() if isinstance(v, str))
            if isinstance(profile.get("registered_office_address"), dict) else best_cand.get("address_snippet", "")
        )

        ch_url = f"{cls.PUBLIC_WEB_BASE}/company/{cand_num}"

        # ──────────────────────────────────────────────────────────────────────
        # Evaluation Logic (Section 1, 2, 3, 4, 5, 6)
        # ──────────────────────────────────────────────────────────────────────
        reasons = []

        if top["pc_match"]:
            reasons.append("Exact postcode match")
        elif top["street_match"]:
            reasons.append("Street address match")
        if top["city_match"]:
            reasons.append(f"City match ({city})")
        if top["name_sim"] >= 0.8:
            reasons.append(f"Strong name similarity ({int(top['name_sim']*100)}%)")
        elif top["name_sim"] >= 0.5:
            reasons.append(f"Trading-to-legal name match ({int(top['name_sim']*100)}%)")

        if is_food_sic:
            reasons.append(f"Relevant hospitality SIC code ({', '.join(sic_codes[:2])})")

        reasons.append(f"Status: {company_status}")

        match_reason = "; ".join(reasons)

        # Ambiguity check: if multiple candidates have high, identical scores
        if len(scored_candidates) > 1 and scored_candidates[1]["score"] >= 0.65 and (top["score"] - scored_candidates[1]["score"]) < 0.05:
            return CompaniesHouseRecord(
                trading_name=company_name,
                legal_entity_name=cand_name,
                registered_name=cand_name,
                companies_house_number=cand_num,
                company_type=company_type,
                company_status=company_status,
                registered_office=reg_office,
                sic_codes=sic_codes,
                incorporation_date=str(profile.get("date_of_creation", "")),
                companies_house_url=ch_url,
                entity_source=entity_source,
                entity_match_status=EntityMatchStatus.AMBIGUOUS.value,
                match_status=EntityMatchStatus.AMBIGUOUS.value,
                entity_match_confidence=EntityMatchConfidence.MEDIUM.value,
                match_confidence=EntityMatchConfidence.MEDIUM.value,
                entity_match_reason=f"Multiple ambiguous matches with similar confidence scores: '{cand_name}' vs '{scored_candidates[1]['candidate'].get('title')}'",
                match_reason=f"Multiple ambiguous matches with similar confidence scores: '{cand_name}' vs '{scored_candidates[1]['candidate'].get('title')}'",
                is_corporate_subscriber=False
            )

        # HIGH confidence requirement (Section 6): multiple matching signals
        # - Strong name match + direct address match (postcode/street), OR
        # - Strong name match + matching city + relevant hospitality SIC
        has_direct_address = top["pc_match"] or top["street_match"]
        has_city_and_sic = top["city_match"] and is_food_sic

        is_high_confidence = (
            (top["name_sim"] >= 0.70 and has_direct_address) or
            (top["name_sim"] >= 0.85 and (has_direct_address or has_city_and_sic))
        )

        # If lead has a known city/postcode and candidate has NO location overlap whatsoever, reject HIGH confidence
        has_any_location_signal = top["pc_match"] or top["street_match"] or top["city_match"]
        if (norm_lead_pc or city) and not has_any_location_signal:
            is_high_confidence = False

        lead_is_active = (
            lead.get("operational_status") in ("ACTIVE_CONFIRMED", "ACTIVE")
            or bool((lead.get("review_count") or 0) > 0)
            or lead.get("qualification_state") == "OUTREACH_READY"
        )

        # Status check (Section 1, 2, 3, 6): ACTIVE vs DISSOLVED vs CONFLICT
        if company_status == CompanyStatus.DISSOLVED.value or "dissolved" in company_status.lower():
            # Dissolved entity handling
            is_corp = False
            # Check if this is a weak name-only match in a different place (Section 18 test 4)
            if top["score"] < 0.40 or (not has_any_location_signal and top["name_sim"] < 0.85):
                return CompaniesHouseRecord(
                    trading_name=company_name,
                    legal_entity_name=cand_name if top["name_sim"] > 0.3 else "",
                    registered_name=cand_name if top["name_sim"] > 0.3 else "",
                    companies_house_number=cand_num if top["name_sim"] > 0.3 else "",
                    company_type=company_type,
                    company_status=company_status,
                    registered_office=reg_office,
                    sic_codes=sic_codes,
                    companies_house_url=ch_url,
                    entity_source=entity_source,
                    entity_match_status=EntityMatchStatus.NO_MATCH.value,
                    match_status=EntityMatchStatus.NO_MATCH.value,
                    entity_match_confidence=EntityMatchConfidence.LOW.value if top["name_sim"] > 0.3 else EntityMatchConfidence.UNKNOWN.value,
                    match_confidence=EntityMatchConfidence.LOW.value if top["name_sim"] > 0.3 else EntityMatchConfidence.UNKNOWN.value,
                    entity_match_reason=f"Weak dissolved company match below confidence threshold ({int(top['score']*100)}%): '{cand_name}' (DISSOLVED)",
                    match_reason=f"Weak dissolved company match below confidence threshold ({int(top['score']*100)}%): '{cand_name}' (DISSOLVED)",
                    is_corporate_subscriber=False
                )

            if lead_is_active:
                # Active business matched to a dissolved entity: CONFLICT (Section 1, 2, 3)
                status_val = EntityMatchStatus.CONFLICT.value
                conf_val = EntityMatchConfidence.HIGH.value if is_high_confidence else EntityMatchConfidence.MEDIUM.value
                full_reason = f"CONFLICT: Current trading business appears active ({lead.get('operational_status', 'ACTIVE')}), but matched Companies House entity '{cand_name}' is DISSOLVED. Entity may be an old corporate vehicle or previous operator. Cannot treat as active corporate subscriber."
            else:
                # Trading business itself is confirmed closed/dissolved
                status_val = EntityMatchStatus.MATCHED_DISSOLVED.value
                conf_val = EntityMatchConfidence.HIGH.value if is_high_confidence else EntityMatchConfidence.MEDIUM.value
                full_reason = f"MATCHED_DISSOLVED: Strong evidence trading business is the dissolved entity '{cand_name}'."

            return CompaniesHouseRecord(
                trading_name=company_name,
                legal_entity_name=cand_name,
                registered_name=cand_name,
                companies_house_number=cand_num,
                company_type=company_type,
                company_status=company_status,
                registered_office=reg_office,
                sic_codes=sic_codes,
                incorporation_date=str(profile.get("date_of_creation", "")),
                companies_house_url=ch_url,
                entity_source=entity_source,
                entity_match_status=status_val,
                match_status=status_val,
                entity_match_confidence=conf_val,
                match_confidence=conf_val,
                entity_match_reason=full_reason,
                match_reason=full_reason,
                is_corporate_subscriber=False
            )

        elif is_high_confidence and company_status == CompanyStatus.ACTIVE.value and is_corporate_type:
            # Active corporate body with strong evidence
            return CompaniesHouseRecord(
                trading_name=company_name,
                legal_entity_name=cand_name,
                registered_name=cand_name,
                companies_house_number=cand_num,
                company_type=company_type,
                company_status=company_status,
                registered_office=reg_office,
                sic_codes=sic_codes,
                incorporation_date=str(profile.get("date_of_creation", "")),
                companies_house_url=ch_url,
                entity_source=entity_source,
                entity_match_status=EntityMatchStatus.MATCHED_ACTIVE.value,
                match_status=EntityMatchStatus.MATCHED_ACTIVE.value,
                entity_match_confidence=EntityMatchConfidence.HIGH.value,
                match_confidence=EntityMatchConfidence.HIGH.value,
                entity_match_reason=match_reason,
                match_reason=match_reason,
                is_corporate_subscriber=True
            )

        elif top["score"] >= 0.45 or (top["score"] >= 0.40 and has_any_location_signal):
            # MEDIUM confidence possible match
            return CompaniesHouseRecord(
                trading_name=company_name,
                legal_entity_name=cand_name,
                registered_name=cand_name,
                companies_house_number=cand_num,
                company_type=company_type,
                company_status=company_status,
                registered_office=reg_office,
                sic_codes=sic_codes,
                incorporation_date=str(profile.get("date_of_creation", "")),
                companies_house_url=ch_url,
                entity_source=entity_source,
                entity_match_status=EntityMatchStatus.POSSIBLE_MATCH.value,
                match_status=EntityMatchStatus.POSSIBLE_MATCH.value,
                entity_match_confidence=EntityMatchConfidence.MEDIUM.value,
                match_confidence=EntityMatchConfidence.MEDIUM.value,
                entity_match_reason=f"Possible match: {match_reason} (requires manual corroboration)",
                match_reason=f"Possible match: {match_reason} (requires manual corroboration)",
                is_corporate_subscriber=False
            )

        else:
            return CompaniesHouseRecord(
                trading_name=company_name,
                legal_entity_name=cand_name if top["name_sim"] > 0.3 else "",
                registered_name=cand_name if top["name_sim"] > 0.3 else "",
                companies_house_number=cand_num if top["name_sim"] > 0.3 else "",
                company_type=company_type,
                company_status=company_status,
                registered_office=reg_office,
                sic_codes=sic_codes,
                companies_house_url=ch_url if top["name_sim"] > 0.3 else "",
                entity_source=entity_source,
                entity_match_status=EntityMatchStatus.NO_MATCH.value,
                entity_match_confidence=EntityMatchConfidence.LOW.value if top["name_sim"] >= 0.5 else EntityMatchConfidence.UNKNOWN.value,
                match_confidence=EntityMatchConfidence.LOW.value if top["name_sim"] >= 0.5 else EntityMatchConfidence.UNKNOWN.value,
                entity_match_reason=f"Weak match below confidence threshold ({int(top['score']*100)}%): '{cand_name}'",
                match_reason=f"Weak match below confidence threshold ({int(top['score']*100)}%): '{cand_name}'",
                is_corporate_subscriber=False
            )

