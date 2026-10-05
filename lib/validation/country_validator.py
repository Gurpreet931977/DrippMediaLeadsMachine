import re
from typing import Dict, Any, Tuple
from lib.types import CountryStatus

UK_POSTCODE_REGEX = re.compile(r'\b([A-Z]{1,2}[0-9][0-9A-Z]?\s?[0-9][A-Z]{2})\b', re.IGNORECASE)
# US state code must be uppercase 2-letters preceded by comma/space and followed by 5-digit zip or end of line
US_STATE_REGEX = re.compile(r',\s*\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b\s*(\d{5})?')

class CountryValidator:
    """
    Strict post-discovery country validator.
    Validates physical address, postcode, phone dial code, and metadata against the target country.
    """
    def __init__(self):
        pass

    def validate(self, target_country: str, address: str, phone: str = "", raw_data: Dict[str, Any] = None) -> Tuple[str, str, str, str, str]:
        """
        Validates whether the business physically resides in the target country.
        Returns:
            (country_status, detected_country, region, postcode, country_evidence)
        """
        target_norm = (target_country or "").strip().lower()
        addr = (address or "").strip()
        ph = (phone or "").strip()
        raw = raw_data or {}

        # Extract Apify direct metadata if available
        api_country_code = str(raw.get("countryCode", "")).upper()
        api_country = str(raw.get("country", "")).strip()
        api_postal_code = str(raw.get("postalCode", "")).strip()
        api_state = str(raw.get("state", "")).strip()

        # Target: United Kingdom
        if target_norm in ["united kingdom", "uk", "great britain", "gb", "england", "scotland", "wales"]:
            return self._validate_uk(addr, ph, api_country_code, api_country, api_postal_code, api_state)

        # Target: United States
        elif target_norm in ["united states", "usa", "us"]:
            return self._validate_us(addr, ph, api_country_code, api_country, api_postal_code, api_state)

        # Target: Germany
        elif target_norm in ["germany", "de", "deu", "deutschland"]:
            return self._validate_de(addr, ph, api_country_code, api_country, api_postal_code, api_state)

        # Target: United Arab Emirates
        elif target_norm in ["united arab emirates", "uae", "ae", "are"]:
            return self._validate_ae(addr, ph, api_country_code, api_country, api_postal_code, api_state)

        # Generic fallback
        else:
            if target_norm in addr.lower() or target_norm in api_country.lower():
                return CountryStatus.COUNTRY_MATCH.value, target_country, api_state, api_postal_code, f"Address/metadata matches {target_country}"
            return CountryStatus.COUNTRY_UNCLEAR.value, "Unknown", api_state, api_postal_code, "Insufficient country-specific metadata"

    def _validate_uk(self, addr: str, ph: str, api_cc: str, api_country: str, api_pc: str, api_state: str) -> Tuple[str, str, str, str, str]:
        addr_lower = addr.lower()
        evidence_points = []
        mismatch_points = []

        # 1. Check for obvious US state markers
        us_match = US_STATE_REGEX.search(addr)
        if us_match:
            state_found = us_match.group(1).upper()
            mismatch_points.append(f"US State code ', {state_found}' in address")

        if any(w in addr_lower for w in ["usa", "united states"]):
            mismatch_points.append("Address explicitly mentions USA/United States")

        if api_cc == "US" or "united states" in api_country.lower():
            mismatch_points.append(f"Metadata country is US ({api_country})")

        # US Phone Area codes check (e.g. +1 or (603), (734), (860), (718))
        if ph.startswith("+1") or re.search(r'^\s*\(\d{3}\)\s*\d{3}', ph):
            mismatch_points.append(f"US phone format ({ph})")

        if mismatch_points:
            detected = "United States" if "US" in "".join(mismatch_points) else "Foreign Country"
            return (
                CountryStatus.COUNTRY_MISMATCH.value,
                detected,
                api_state,
                api_pc,
                f"Country Mismatch: {'; '.join(mismatch_points)}"
            )

        # 2. Check for UK Postcode
        extracted_postcode = api_pc
        postcode_match = UK_POSTCODE_REGEX.search(addr)
        if postcode_match:
            extracted_postcode = postcode_match.group(1).upper()
            evidence_points.append(f"Valid UK Postcode ({extracted_postcode})")

        # 3. Check for UK Address Keywords
        uk_keywords = ["united kingdom", "england", "scotland", "wales", "northern ireland", "greater manchester", "uk"]
        for kw in uk_keywords:
            if kw in addr_lower:
                evidence_points.append(f"UK keyword in address ('{kw}')")
                break

        # 4. Check for UK Phone formatting (+44 or 0161)
        if ph.startswith("+44") or ph.startswith("01") or ph.startswith("02") or ph.startswith("07"):
            evidence_points.append(f"UK phone format ({ph})")

        # 5. Check metadata countryCode
        if api_cc == "GB":
            evidence_points.append("Apify countryCode is GB")

        # Decision
        if evidence_points:
            region = api_state
            if not region:
                if "manchester" in addr_lower:
                    region = "Greater Manchester"
                elif "birmingham" in addr_lower:
                    region = "West Midlands"
                elif "leeds" in addr_lower:
                    region = "West Yorkshire"
                else:
                    region = ""
            return (
                CountryStatus.COUNTRY_MATCH.value,
                "United Kingdom",
                region,
                extracted_postcode,
                f"Country Match: {', '.join(evidence_points)}"
            )

        return (
            CountryStatus.COUNTRY_UNCLEAR.value,
            "Unknown",
            api_state,
            extracted_postcode,
            "No definitive UK geographic signals confirmed in address or phone"
        )

    def _validate_us(self, addr: str, ph: str, api_cc: str, api_country: str, api_pc: str, api_state: str) -> Tuple[str, str, str, str, str]:
        us_match = US_STATE_REGEX.search(addr)
        from lib.country_adapters import get_country_adapter
        us_adapter = get_country_adapter("US")
        extracted_zip = us_adapter.address_normalizer.extract_postal_code(addr) or api_pc

        if us_match or api_cc == "US" or "usa" in addr.lower() or "united states" in addr.lower():
            state = us_match.group(1).upper() if us_match else api_state
            return (
                CountryStatus.COUNTRY_MATCH.value,
                "United States",
                state,
                extracted_zip,
                "US address pattern confirmed"
            )
        return (
            CountryStatus.COUNTRY_UNCLEAR.value,
            "Unknown",
            api_state,
            extracted_zip,
            "Insufficient US evidence"
        )

    def _validate_de(self, addr: str, ph: str, api_cc: str, api_country: str, api_pc: str, api_state: str) -> Tuple[str, str, str, str, str]:
        from lib.country_adapters import get_country_adapter
        de_adapter = get_country_adapter("DE")
        plz = de_adapter.address_normalizer.extract_postal_code(addr) or api_pc
        has_de_phone = de_adapter.phone_normalizer.is_valid(ph)
        addr_lower = addr.lower()

        if api_cc == "DE" or "germany" in addr_lower or "deutschland" in addr_lower or (plz and has_de_phone):
            return (
                CountryStatus.COUNTRY_MATCH.value,
                "Germany",
                api_state,
                plz,
                "German address / PLZ / dial code confirmed"
            )
        return (
            CountryStatus.COUNTRY_UNCLEAR.value,
            "Unknown",
            api_state,
            plz,
            "Insufficient Germany evidence"
        )

    def _validate_ae(self, addr: str, ph: str, api_cc: str, api_country: str, api_pc: str, api_state: str) -> Tuple[str, str, str, str, str]:
        from lib.country_adapters import get_country_adapter
        ae_adapter = get_country_adapter("AE")
        pobox = ae_adapter.address_normalizer.extract_postal_code(addr) or api_pc
        has_ae_phone = ae_adapter.phone_normalizer.is_valid(ph)
        addr_lower = addr.lower()

        if api_cc == "AE" or "uae" in addr_lower or "united arab emirates" in addr_lower or "dubai" in addr_lower or "abu dhabi" in addr_lower:
            return (
                CountryStatus.COUNTRY_MATCH.value,
                "United Arab Emirates",
                api_state or "Dubai",
                pobox,
                "UAE address / Emirate confirmed"
            )
        return (
            CountryStatus.COUNTRY_UNCLEAR.value,
            "Unknown",
            api_state,
            pobox,
            "Insufficient UAE evidence"
        )
