import re
from typing import Dict, Any, List, Optional, Set
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum

from lib.types import (
    DiscoveredBusiness,
    OperationalStatus,
    OperationalConfidence,
    EvidenceFreshness,
    SocialProfileStatus,
    SocialOwnershipStatus,
    SocialActivityStatus,
    SourceFamily,
    WebsiteStatus,
)


class OperationalSignalType(str, Enum):
    # Strong signals
    VERIFIED_BUSINESS_SOCIAL = "VERIFIED_BUSINESS_SOCIAL"
    DIRECT_OFFICIAL_BUSINESS_SOURCE = "DIRECT_OFFICIAL_BUSINESS_SOURCE"
    
    # Corroborating signals
    RECENT_CUSTOMER_REVIEWS = "RECENT_CUSTOMER_REVIEWS"
    VERIFIED_PHONE = "VERIFIED_PHONE"
    VERIFIED_ADDRESS = "VERIFIED_ADDRESS"
    CURRENT_OPENING_HOURS = "CURRENT_OPENING_HOURS"
    CURRENT_THIRD_PARTY_LISTING = "CURRENT_THIRD_PARTY_LISTING"
    PHOTO_FOOTPRINT = "PHOTO_FOOTPRINT"
    CREATOR_CONTENT = "CREATOR_CONTENT"


class OperationalSignalTier(str, Enum):
    STRONG = "STRONG"
    CORROBORATING = "CORROBORATING"


@dataclass
class OperationalSignal:
    signal_type: OperationalSignalType
    tier: OperationalSignalTier
    source_family: SourceFamily
    description: str
    freshness: str = EvidenceFreshness.UNKNOWN.value
    details: Dict[str, Any] = field(default_factory=dict)


class OperationalValidator:
    """
    Qualification Engine V3 - Operational Verification & Multi-Signal Reliability
    
    Evaluates:
      1. Negative / Closure detection (permanently closed, dissolved, ceased trading).
      2. Evidence freshness (RECENT: <=90d, RECENT_ENOUGH: 91-180d, STALE: 181+d, UNKNOWN).
      3. Combination of independent current evidence sources & source families:
         - Strong current sources (accessible active business social, verified direct contact, first-party listing).
         - Corroborating sources (current Maps listing, recent reviews, current opening hours, visual media).
      4. Source Family Independence:
         - Google Maps, Tripadvisor, Restaurant Guru, OpenStreetMap, Business Registry, etc.
         - Signals from the same source family (e.g. Google review + Google phone + Google address)
           do NOT count as independent corroboration.
      5. Deterministic OperationalStatus:
         - Rule A: At least 1 strong current source + at least 1 corroborating source -> ACTIVE_CONFIRMED.
         - Rule B (Multi-Signal Operational Rule):
             1. review_count >= 50
             2. rating >= 4.0
             3. review evidence from accepted review source
             4. review evidence is recent (<=180d)
             5. at least ONE independent current operational signal (phone, address, hours, registry from separate family)
             6. no closure signal exists
             7. business identity and target location verified
             8. website state is NO_WEBSITE_CONFIRMED
             9. no unresolved review conflict
             10. no serious red flag
             -> ACTIVE_CONFIRMED
         - ACTIVE_LIKELY: Plausible operation lacking independent corroboration.
         - OPERATIONAL_UNKNOWN: Insufficient evidence to determine status.
         - CLOSED_OR_UNVERIFIED: Permanently closed, contradictory identity, or unverified closure.
    """

    CLOSURE_KEYWORDS = [
        "permanently closed",
        "closed permanently",
        "permanently shut",
        "dissolved",
        "liquidation",
        "closure announcement",
        "no longer operating",
        "ceased trading",
        "shut down",
        "business moved",
        "unreachable / invalid identity",
        "out of business",
    ]

    ACCEPTED_REVIEW_SOURCES = {
        SourceFamily.TRIPADVISOR,
        SourceFamily.GOOGLE,
        SourceFamily.RESTAURANT_GURU,
        SourceFamily.YELP,
        SourceFamily.OTHER_DIRECTORY,
    }

    @staticmethod
    def parse_evidence_date(date_str: str) -> Optional[datetime]:
        """Parses ISO dates, month-year formats, or relative date strings (e.g. '2 weeks ago')."""
        if not date_str or not isinstance(date_str, str):
            return None
        date_clean = date_str.strip()

        # Try structured date formats
        for fmt in (
            "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y",
            "%d %B %Y", "%d %b %Y", "%B %Y", "%b %Y", "%Y-%m"
        ):
            try:
                return datetime.strptime(date_clean, fmt)
            except Exception:
                pass

        try:
            # Fallback ISO prefix if len >= 10
            if len(date_clean) >= 10 and date_clean[4] == "-" and date_clean[7] == "-":
                return datetime.strptime(date_clean[:10], "%Y-%m-%d")
        except Exception:
            pass

        now = datetime.now()
        low = date_clean.lower()
        if "day" in low or "today" in low or "yesterday" in low:
            return now - timedelta(days=1)
        if "week" in low:
            m = re.search(r"(\d+)\s*week", low)
            weeks = int(m.group(1)) if m else 1
            return now - timedelta(days=weeks * 7)
        if "month" in low:
            m = re.search(r"(\d+)\s*month", low)
            months = int(m.group(1)) if m else 1
            return now - timedelta(days=months * 30)
        if "year" in low:
            m = re.search(r"(\d+)\s*year", low)
            years = int(m.group(1)) if m else 1
            return now - timedelta(days=years * 365)

        return None

    @classmethod
    def evaluate_freshness(cls, date_str: str) -> str:
        """Categorizes evidence age: RECENT (<=90d), RECENT_ENOUGH (91-180d), STALE (181+d), UNKNOWN."""
        dt = cls.parse_evidence_date(date_str)
        if not dt:
            return EvidenceFreshness.UNKNOWN.value
        age_days = (datetime.now() - dt).days
        if age_days <= 90:
            return EvidenceFreshness.RECENT.value
        elif age_days <= 180:
            return EvidenceFreshness.RECENT_ENOUGH.value
        else:
            return EvidenceFreshness.STALE.value

    @classmethod
    def classify_source_family(cls, source_str: Optional[str]) -> SourceFamily:
        """Classifies a source string or URL into an explicit SourceFamily enum."""
        if not source_str or not isinstance(source_str, str):
            return SourceFamily.UNKNOWN
        s = source_str.strip().lower()
        if "tripadvisor" in s:
            return SourceFamily.TRIPADVISOR
        if "google" in s or "gmaps" in s:
            return SourceFamily.GOOGLE
        if "restaurantguru" in s or "restaurant_guru" in s or "restaurant guru" in s:
            return SourceFamily.RESTAURANT_GURU
        if "facebook" in s or "fb.com" in s:
            return SourceFamily.FACEBOOK
        if "instagram" in s or "instagr.am" in s:
            return SourceFamily.INSTAGRAM
        if "yelp" in s:
            return SourceFamily.YELP
        if "openstreetmap" in s or "osm" in s:
            return SourceFamily.OPENSTREETMAP
        if "companies_house" in s or "companieshouse" in s or "registry" in s or "business_registry" in s:
            return SourceFamily.BUSINESS_REGISTRY
        if "website" in s or "official" in s:
            return SourceFamily.OFFICIAL_WEBSITE
        if any(k in s for k in ["yell", "foursquare", "directory", "thomson", "cylex"]):
            return SourceFamily.OTHER_DIRECTORY
        return SourceFamily.UNKNOWN

    @classmethod
    def verify_operations(
        cls,
        business: DiscoveredBusiness,
        social_audit: Dict[str, Any],
        creator_evidence: Optional[Dict[str, Any]] = None,
        website_verification_status: Optional[str] = None,
        review_enrichment: Optional[Dict[str, Any]] = None,
        closure_indicators: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        red_flags: List[str] = []
        strong_sources: List[str] = []
        corroborating_sources: List[str] = []
        signals: List[OperationalSignal] = []

        # -------------------------------------------------------------
        # 1. CLOSURE & NEGATIVE EVIDENCE CHECK
        # -------------------------------------------------------------
        is_permanently_closed = getattr(business, "is_permanently_closed", False)
        is_temporarily_closed = getattr(business, "is_temporarily_closed", False)

        closure_flag = False
        closure_reason = ""

        if is_permanently_closed:
            closure_flag = True
            closure_reason = "Business is permanently closed on Google Maps."
        elif is_temporarily_closed:
            closure_flag = True
            closure_reason = "Business is temporarily closed on Google Maps."

        # Inspect textual closure phrases in notes, operational evidence, closure indicators, raw_data
        text_corpus = " ".join([
            getattr(business, "operational_evidence", "") or "",
            getattr(business, "notes", "") or "",
            " ".join(closure_indicators or []),
            str(getattr(business, "raw_data", ""))
        ]).lower()

        for kw in cls.CLOSURE_KEYWORDS:
            if kw in text_corpus:
                closure_flag = True
                closure_reason = f"Explicit closure keyword detected: '{kw}'"
                break

        if closure_flag:
            red_flags.append(closure_reason)
            maps_status = "CLOSED_PERMANENTLY" if (is_permanently_closed or "permanent" in closure_reason.lower() or "dissolved" in closure_reason.lower()) else "CLOSED_TEMPORARILY"
            return {
                "current_maps_status": maps_status,
                "recent_business_activity": "NONE",
                "recent_review_activity": "NONE",
                "current_social_activity": "NONE",
                "current_contact_evidence": "CLOSED",
                "operational_status": OperationalStatus.CLOSED_OR_UNVERIFIED.value,
                "operational_confidence": OperationalConfidence.HIGH.value,
                "operational_evidence": f"Business operation unverified / closed ({closure_reason}).",
                "evidence_freshness": EvidenceFreshness.UNKNOWN.value,
                "strong_sources": [],
                "corroborating_sources": [],
                "red_flags": red_flags,
                "signals": [],
                "independent_operational_signals": [],
                "independent_source_families": [],
                "review_source_family": SourceFamily.UNKNOWN.value,
                "multi_signal_rule_applied": False
            }

        maps_status = "OPERATIONAL"

        # -------------------------------------------------------------
        # 2. EVIDENCE FRESHNESS CHECK
        # -------------------------------------------------------------
        r_enrich = review_enrichment
        if r_enrich is None and hasattr(business, "raw_data") and isinstance(business.raw_data, dict):
            r_enrich = business.raw_data.get("review_enrichment")

        latest_review_date = getattr(business, "latest_review_date", "") or ""
        review_date_type = "UNKNOWN"
        review_date_conf = "UNKNOWN"
        if r_enrich:
            if r_enrich.get("review_evidence_date"):
                latest_review_date = r_enrich.get("review_evidence_date", "") or ""
            elif r_enrich.get("review_freshness") in [EvidenceFreshness.UNKNOWN.value, "UNKNOWN"]:
                latest_review_date = ""
            review_date_type = r_enrich.get("review_evidence_date_type", "UNKNOWN")
            review_date_conf = r_enrich.get("review_evidence_date_confidence", "UNKNOWN")

        review_freshness = cls.evaluate_freshness(latest_review_date)
        social_freshness = cls.evaluate_freshness(getattr(business, "latest_social_post_date", ""))

        # Overall freshness determination
        if review_freshness == EvidenceFreshness.RECENT.value or social_freshness == EvidenceFreshness.RECENT.value:
            overall_freshness = EvidenceFreshness.RECENT.value
        elif review_freshness == EvidenceFreshness.RECENT_ENOUGH.value or social_freshness == EvidenceFreshness.RECENT_ENOUGH.value:
            overall_freshness = EvidenceFreshness.RECENT_ENOUGH.value
        elif review_freshness == EvidenceFreshness.STALE.value and social_freshness == EvidenceFreshness.STALE.value:
            overall_freshness = EvidenceFreshness.STALE.value
            red_flags.append("All available operational evidence is stale (>180 days old).")
        elif review_freshness == EvidenceFreshness.STALE.value and social_freshness == EvidenceFreshness.UNKNOWN.value:
            overall_freshness = EvidenceFreshness.STALE.value
            red_flags.append("Customer review evidence is stale (>180 days old) with no recent social activity.")
        else:
            overall_freshness = EvidenceFreshness.UNKNOWN.value

        # -------------------------------------------------------------
        # 3. CONTACT PRESENCE EVALUATION
        # -------------------------------------------------------------
        has_phone = bool(business.phone and len(str(business.phone).strip()) >= 7)
        has_address = bool(business.address and len(str(business.address).strip()) >= 10)

        if has_phone and has_address:
            contact_evidence = "VERIFIED_PHONE_AND_ADDRESS"
            corroborating_sources.append("Direct verified business phone & complete physical premises")
        elif has_phone:
            contact_evidence = "PHONE_ONLY"
            corroborating_sources.append("Direct verified business phone")
        elif has_address:
            contact_evidence = "ADDRESS_ONLY"
            corroborating_sources.append("Complete physical street address")
        else:
            contact_evidence = "NONE"
            red_flags.append("No direct business contact phone or address found.")

        # -------------------------------------------------------------
        # 4. SOCIAL PRESENCE EVALUATION (Strong Source when verified & accessible)
        # -------------------------------------------------------------
        verified_social_urls = social_audit.get("verified_urls", {})
        social_ownership = social_audit.get("social_ownership_status", SocialOwnershipStatus.UNKNOWN.value)
        social_profile_status = social_audit.get("social_profile_status", SocialProfileStatus.UNKNOWN.value)
        social_activity = social_audit.get("social_activity", SocialActivityStatus.UNKNOWN.value)

        has_verified_accessible_social = (
            bool(verified_social_urls)
            and social_ownership in [SocialOwnershipStatus.VERIFIED.value, "VERIFIED_OWNED", "VERIFIED"]
            and social_profile_status == SocialProfileStatus.ACCESSIBLE.value
            and social_activity in [SocialActivityStatus.ACTIVE.value, "SOCIAL_ACTIVE", "ACTIVE"]
            and social_freshness != EvidenceFreshness.STALE.value
        )

        if has_verified_accessible_social:
            channels = ", ".join([p.capitalize() for p in verified_social_urls.keys()])
            strong_sources.append(f"Accessible, active business-owned {channels} profile(s)")
            social_activity_evidence = f"ACTIVE ({channels})"

            for plat in verified_social_urls.keys():
                sf = cls.classify_source_family(plat)
                signals.append(OperationalSignal(
                    signal_type=OperationalSignalType.VERIFIED_BUSINESS_SOCIAL,
                    tier=OperationalSignalTier.STRONG,
                    source_family=sf,
                    description=f"Verified active business-owned {plat.capitalize()} profile",
                    freshness=social_freshness,
                    details={"url": verified_social_urls.get(plat)}
                ))
        elif social_profile_status in [SocialProfileStatus.INACCESSIBLE.value, SocialProfileStatus.INVALID_FORMAT.value]:
            social_activity_evidence = "INACCESSIBLE_OR_INVALID"
            red_flags.append(f"Discovered social profile is unavailable or invalid ({social_profile_status}).")
        elif social_ownership == SocialOwnershipStatus.UNVERIFIED.value:
            social_activity_evidence = "UNVERIFIED"
            if verified_social_urls:
                red_flags.append("Social profile ownership is unverified or belongs to unrelated entity.")
        else:
            social_activity_evidence = "UNKNOWN"

        # -------------------------------------------------------------
        # 5. CORROBORATING SOURCES
        # -------------------------------------------------------------
        # A: Current Google Maps listing
        if maps_status == "OPERATIONAL":
            corroborating_sources.append("Current operational Google Maps listing")

        # B: Detailed opening hours
        has_hours = bool(business.opening_hours and len(str(business.opening_hours).strip()) >= 5)
        if has_hours:
            corroborating_sources.append("Detailed current opening hours published")

        # C: Customer review activity
        if review_freshness in [EvidenceFreshness.RECENT.value, EvidenceFreshness.RECENT_ENOUGH.value]:
            corroborating_sources.append(f"Recent customer review activity ({review_freshness})")
            review_activity = f"RECENT ({latest_review_date})"
        elif review_freshness == EvidenceFreshness.STALE.value:
            review_activity = f"STALE ({latest_review_date})"
        else:
            review_activity = "UNKNOWN"

        # D: Customer-facing visual media footprint
        photo_count = getattr(business, "photo_count", 0) or 0
        if photo_count >= 30:
            corroborating_sources.append(f"Established customer-facing photo presence ({photo_count} photos)")

        # E: Third-party creator / influencer evidence (Corroborating only, never strong)
        if creator_evidence:
            c_status = creator_evidence.get("creator_evidence_status")
            c_conf = creator_evidence.get("creator_evidence_confidence")
            c_date = creator_evidence.get("creator_latest_date", "")
            c_freshness = cls.evaluate_freshness(c_date) if c_date else EvidenceFreshness.UNKNOWN.value
            if c_status == "FOUND" and c_conf in ["HIGH", "MEDIUM"] and c_freshness != EvidenceFreshness.STALE.value:
                corroborating_sources.append("Current third-party creator/influencer content corroboration")
                signals.append(OperationalSignal(
                    signal_type=OperationalSignalType.CREATOR_CONTENT,
                    tier=OperationalSignalTier.CORROBORATING,
                    source_family=cls.classify_source_family(creator_evidence.get("platform")),
                    description="Third-party creator content mention",
                    freshness=c_freshness,
                    details=creator_evidence
                ))

        # -------------------------------------------------------------
        # 6. SOURCE FAMILY IDENTIFICATION & INDEPENDENCE (Section 4)
        # -------------------------------------------------------------
        # Review Source Family
        review_sf = SourceFamily.UNKNOWN
        rev_src_str = ""
        if r_enrich:
            rev_src_str = r_enrich.get("review_source") or r_enrich.get("review_source_url") or ""
        elif business.evidence_sources and "review" in business.evidence_sources:
            rev_src_str = business.evidence_sources["review"]
        elif getattr(business, "review_source_family", None):
            rev_src_str = business.review_source_family

        if rev_src_str:
            review_sf = cls.classify_source_family(rev_src_str)
        elif getattr(business, "discovery_source", "") == "APIFY" and (business.review_count or 0) > 0:
            review_sf = SourceFamily.GOOGLE

        # Phone Source Family
        phone_sf = cls.classify_source_family(
            getattr(business, "phone_source_family", None)
            or (business.evidence_sources.get("phone") if business.evidence_sources else None)
        )
        if phone_sf == SourceFamily.UNKNOWN:
            if getattr(business, "discovery_source", "") in ["OSM", "OPENSTREETMAP"] or getattr(business, "osm_type", None):
                phone_sf = SourceFamily.OPENSTREETMAP
            elif getattr(business, "discovery_source", "") == "APIFY":
                phone_sf = SourceFamily.GOOGLE

        # Address Source Family
        addr_sf = cls.classify_source_family(
            getattr(business, "address_source_family", None)
            or (business.evidence_sources.get("address") if business.evidence_sources else None)
        )
        if addr_sf == SourceFamily.UNKNOWN:
            if getattr(business, "discovery_source", "") in ["OSM", "OPENSTREETMAP"] or getattr(business, "osm_type", None):
                addr_sf = SourceFamily.OPENSTREETMAP
            elif getattr(business, "discovery_source", "") == "APIFY":
                addr_sf = SourceFamily.GOOGLE

        # Opening Hours Source Family
        hours_sf = cls.classify_source_family(
            getattr(business, "hours_source_family", None)
            or (business.evidence_sources.get("hours") if business.evidence_sources else None)
        )
        if hours_sf == SourceFamily.UNKNOWN:
            if getattr(business, "discovery_source", "") in ["OSM", "OPENSTREETMAP"] or getattr(business, "osm_type", None):
                hours_sf = SourceFamily.OPENSTREETMAP
            elif getattr(business, "discovery_source", "") == "APIFY":
                hours_sf = SourceFamily.GOOGLE

        # Independent Operational Signals relative to review_sf
        independent_ops_signals: List[str] = []
        independent_source_families: Set[SourceFamily] = set()

        if has_phone and phone_sf != SourceFamily.UNKNOWN and phone_sf != review_sf:
            independent_ops_signals.append(f"Verified business phone via {phone_sf.value}")
            independent_source_families.add(phone_sf)
            signals.append(OperationalSignal(
                signal_type=OperationalSignalType.VERIFIED_PHONE,
                tier=OperationalSignalTier.CORROBORATING,
                source_family=phone_sf,
                description=f"Verified business phone: {business.phone}"
            ))

        if has_address and addr_sf != SourceFamily.UNKNOWN and addr_sf != review_sf:
            independent_ops_signals.append(f"Complete physical address via {addr_sf.value}")
            independent_source_families.add(addr_sf)
            signals.append(OperationalSignal(
                signal_type=OperationalSignalType.VERIFIED_ADDRESS,
                tier=OperationalSignalTier.CORROBORATING,
                source_family=addr_sf,
                description=f"Complete physical address: {business.address}"
            ))

        if has_hours and hours_sf != SourceFamily.UNKNOWN and hours_sf != review_sf:
            independent_ops_signals.append(f"Current opening hours via {hours_sf.value}")
            independent_source_families.add(hours_sf)
            signals.append(OperationalSignal(
                signal_type=OperationalSignalType.CURRENT_OPENING_HOURS,
                tier=OperationalSignalTier.CORROBORATING,
                source_family=hours_sf,
                description=f"Current opening hours: {business.opening_hours}"
            ))

        # -------------------------------------------------------------
        # 7. MULTI-SIGNAL OPERATIONAL RULE (Rule B) EVALUATION
        # -------------------------------------------------------------
        revs = business.review_count
        rating = business.rating

        is_accepted_review_source = review_sf in cls.ACCEPTED_REVIEW_SOURCES
        is_review_recent = review_freshness in [EvidenceFreshness.RECENT.value, EvidenceFreshness.RECENT_ENOUGH.value]

        # Location verification check
        city_match = getattr(business, "city_match", True)
        biz_city = (getattr(business, "city", "") or "").strip().lower()
        other_major_cities = {"birmingham", "manchester", "london", "sheffield", "liverpool", "bristol", "edinburgh", "glasgow", "newcastle"}
        other_cities_conflicting = other_major_cities - {biz_city}

        review_text = ""
        if r_enrich:
            review_text = str(r_enrich.get("review_evidence", "")) + " " + str(r_enrich.get("review_source_url", ""))
        location_conflict = False
        if biz_city and review_text:
            rev_low = review_text.lower()
            for oc in other_cities_conflicting:
                if f"/{oc}" in rev_low or f"-{oc}_" in rev_low or f"in {oc}" in rev_low or f", {oc}" in rev_low:
                    location_conflict = True
                    red_flags.append(f"Location conflict detected: review evidence references '{oc.capitalize()}', contradicting target city '{biz_city.capitalize()}'.")
                    break

        is_identity_location_verified = (city_match is not False) and (not location_conflict)

        # Website state check: must be NO_WEBSITE_CONFIRMED
        eff_web_status = website_verification_status or getattr(business, "verification_status", None) or getattr(business, "osm_website_status", "")
        is_no_website = bool(
            eff_web_status in [WebsiteStatus.NO_WEBSITE_CONFIRMED.value, "NO_WEBSITE_CONFIRMED", "WEBSITE_NOT_LISTED_IN_OSM"]
            or (not business.raw_website and eff_web_status not in [WebsiteStatus.WEBSITE_EXISTS.value, "WEBSITE_EXISTS"])
        )
        if eff_web_status in [WebsiteStatus.WEBSITE_EXISTS.value, "WEBSITE_EXISTS"] or business.raw_website:
            is_no_website = False

        # Review conflict check
        has_review_conflict = False
        if r_enrich and (
            r_enrich.get("review_confidence") == "CONFLICT"
            or r_enrich.get("review_status") == "CONFLICT_REQUIRES_REVIEW"
            or r_enrich.get("is_material_conflict") is True
            or r_enrich.get("is_branch_difference") is True
        ):
            has_review_conflict = True
            red_flags.append("Unresolved review conflict detected across sources.")

        # Serious red flags check
        serious_red_flags = [
            rf for rf in red_flags
            if any(k in rf.lower() for k in ["closed", "mismatch", "unrelated", "contradict", "conflict", "fake", "wrong branch", "wrong city", "location conflict"])
        ]

        rule_b_eligible = (
            revs is not None and revs >= 50
            and rating is not None and rating >= 4.0
            and is_accepted_review_source
            and is_review_recent
            and len(independent_ops_signals) >= 1
            and not closure_flag
            and is_identity_location_verified
            and is_no_website
            and not has_review_conflict
            and len(serious_red_flags) == 0
        )

        # -------------------------------------------------------------
        # 8. OPERATIONAL DECISION RULE (Section 7)
        # -------------------------------------------------------------
        rule_a_eligible = (
            has_verified_accessible_social
            and len(corroborating_sources) >= 1
            and maps_status == "OPERATIONAL"
            and not closure_flag
            and len(serious_red_flags) == 0
        )

        multi_signal_rule_applied = False

        if rule_a_eligible:
            operational_status = OperationalStatus.ACTIVE_CONFIRMED.value
            operational_confidence = OperationalConfidence.HIGH.value
            operational_evidence = f"Confirmed active: {'; '.join(strong_sources[:2])} corroborated by {'; '.join(corroborating_sources[:2])}."

        elif rule_b_eligible:
            operational_status = OperationalStatus.ACTIVE_CONFIRMED.value
            operational_confidence = OperationalConfidence.HIGH.value
            operational_evidence = (
                f"Confirmed active via multi-signal verification: {revs} reviews ({rating}★) via {review_sf.value} "
                f"({review_freshness}) corroborated by independent {', '.join(independent_ops_signals[:2])}."
            )
            multi_signal_rule_applied = True

        elif overall_freshness == EvidenceFreshness.STALE.value:
            operational_status = OperationalStatus.ACTIVE_LIKELY.value
            operational_confidence = OperationalConfidence.LOW.value
            operational_evidence = "Evidence is stale (>180 days old) without current primary corroboration."
            red_flags.append("Operational evidence is stale; current active operation cannot be confirmed.")

        elif len(corroborating_sources) >= 2 and maps_status == "OPERATIONAL":
            operational_status = OperationalStatus.ACTIVE_LIKELY.value
            operational_confidence = OperationalConfidence.MEDIUM.value
            operational_evidence = f"Likely active: Corroborated by {'; '.join(corroborating_sources[:2])}, but lacking verified strong current operational evidence or independent multi-signal corroboration."

        else:
            operational_status = OperationalStatus.OPERATIONAL_UNKNOWN.value
            operational_confidence = OperationalConfidence.LOW.value
            operational_evidence = "Current operational status cannot be verified from available evidence."

        return {
            "current_maps_status": maps_status,
            "recent_business_activity": "ACTIVE_SOCIAL" if has_verified_accessible_social else "UNKNOWN",
            "recent_review_activity": review_activity,
            "current_social_activity": social_activity_evidence,
            "current_contact_evidence": contact_evidence,
            "operational_status": operational_status,
            "operational_confidence": operational_confidence,
            "operational_evidence": operational_evidence,
            "evidence_freshness": overall_freshness,
            "strong_sources": strong_sources,
            "corroborating_sources": corroborating_sources,
            "red_flags": red_flags,
            "signals": [s.__dict__ if hasattr(s, "__dict__") else s for s in signals],
            "independent_operational_signals": independent_ops_signals,
            "independent_source_families": [sf.value for sf in independent_source_families],
            "review_source_family": review_sf.value,
            "review_evidence_date": latest_review_date or None,
            "review_evidence_date_type": review_date_type,
            "review_freshness": review_freshness,
            "multi_signal_rule_applied": multi_signal_rule_applied
        }
