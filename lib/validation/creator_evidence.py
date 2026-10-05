"""
Dripp Media — Advanced Creator / Influencer Evidence Engine
============================================================
Detects, extracts, validates, and correlates third-party public creator content.

Core Philosophy:
  Do NOT design as "Find influencer tags."
  Design as: "Find public third-party creator content that references, names,
  locates, reviews, tags, or otherwise clearly identifies the target business."
  Tags are only ONE signal among many.

Reference Types:
  - ACCOUNT_TAG: Tagged @business_handle
  - CAPTION_BUSINESS_MENTION: Explicit business name / trading name in caption
  - CAPTION_LOCATION_MENTION: Location / neighborhood / street mention in caption
  - LOCATION_TAG: Platform location / place tag
  - HASHTAG_MENTION: Exact business-specific hashtag (e.g. #HongThai)
  - ON_CONTENT_TEXT: Text overlay inside video/reel/post
  - TITLE_OR_DESCRIPTION: Video title, blog title, or long description
  - MULTI_SIGNAL: Combination of two or more matching reference signals

Strict Rules:
  1. Third-party creator content is NEVER proof of business account ownership.
  2. Public content only (no private accounts, login-only posts, or restricted data).
  3. Location-only mentions require corroboration (category, street, menu) to avoid false matches.
  4. Operational verification: current creator evidence can corroborate operations,
     but creator content ALONE NEVER produces ACTIVE_CONFIRMED.
  5. Qualification V3: Never weakens 50+ reviews, rating >= 4.0, NO_WEBSITE_CONFIRMED, or ACTIVE_CONFIRMED.
"""

import os
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple, Set
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from lib.types import (
    CreatorEvidenceStatus,
    CreatorEvidenceConfidence,
    CreatorFreshness,
    CreatorReferenceType,
    CreatorEvidenceClassification,
    HandleClassification,
    CreatorTrustConcept,
    EvidenceTier,
    SourceQualityTier,
    CreatorEvidenceItem,
    CreatorEvidenceSummary,
    SocialOwnershipStatus,
    SocialProfileStatus,
    SocialActivityStatus
)
from lib.validation.social_validator import SocialIdentityValidator

# Known major cities to detect clear city mismatches
MAJOR_UK_CITIES = [
    "london", "manchester", "birmingham", "leeds", "glasgow", "sheffield",
    "liverpool", "edinburgh", "bristol", "cardiff", "belfast", "newcastle",
    "nottingham", "southampton", "leicester", "coventry", "hull", "bradford",
    "stoke-on-trent", "wolverhampton", "plymouth", "derby", "swansea", "aberdeen"
]

# Generic hashtags that provide no business-specific corroboration
GENERIC_HASHTAGS = {
    "food", "foodie", "foodporn", "instafood", "yummy", "delicious",
    "dinner", "lunch", "breakfast", "brunch", "restaurant", "cafe",
    "eats", "lifestyle", "travel", "reels", "reel", "shorts", "explore",
    "manchester", "birmingham", "london", "uk", "ukfood", "ukfoodie"
}


class CreatorEvidenceValidator:
    """
    Production-grade detector and validator for creator / influencer evidence.
    """

    @staticmethod
    def parse_published_date(date_str: str) -> Optional[datetime]:
        """Parses ISO date strings or relative dates."""
        if not date_str or not isinstance(date_str, str):
            return None
        cleaned = date_str.strip()
        try:
            if len(cleaned) >= 10 and cleaned[4] == "-" and cleaned[7] == "-":
                return datetime.strptime(cleaned[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            pass

        now = datetime.now(timezone.utc)
        low = cleaned.lower()
        if "today" in low or "yesterday" in low or "hour" in low or "day" in low:
            m = re.search(r"(\d+)\s*day", low)
            days = int(m.group(1)) if m else (1 if "yesterday" in low else 0)
            return now - timedelta(days=days)
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
    def calculate_freshness(cls, date_str: str) -> str:
        """
        Classifies creator content freshness (Section 7):
          0-90 days:   CURRENT
          91-180 days: RECENT
          181+ days:   STALE
          No date:     UNKNOWN
        """
        dt = cls.parse_published_date(date_str)
        if not dt:
            return CreatorFreshness.UNKNOWN.value
        now = datetime.now(timezone.utc)
        age_days = (now - dt).days
        if age_days < 0:
            age_days = 0

        if age_days <= 90:
            return CreatorFreshness.CURRENT.value
        elif age_days <= 180:
            return CreatorFreshness.RECENT.value
        else:
            return CreatorFreshness.STALE.value

    @staticmethod
    def normalize_text(text: str) -> str:
        """Normalizes text for robust token and phrase matching."""
        if not text:
            return ""
        n = text.lower()
        n = re.sub(r"[^\w\s]", " ", n)
        return " ".join(n.split())

    @staticmethod
    def normalize_canonical_url(url: str) -> str:
        """
        Section 6: Normalizes a URL by stripping tracking parameters (utm_*, igshid, fbclid, etc.),
        removing fragments, normalizing schemes, lowercasing host, and stripping trailing slashes.
        """
        if not url:
            return ""
        try:
            parsed = urlparse(url.strip())
            scheme = parsed.scheme.lower() or "https"
            netloc = parsed.netloc.lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]
            if netloc.startswith("m."):
                netloc = netloc[2:]
            path = parsed.path.rstrip("/")
            if not path:
                path = "/"

            tracking_params = {
                "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                "fbclid", "igshid", "gclid", "ref", "source", "feature", "si", "s",
                "t", "is_from_webapp", "sender_device", "hl"
            }
            query_pairs = parse_qs(parsed.query, keep_blank_values=False)
            clean_pairs = {k: v for k, v in query_pairs.items() if k.lower() not in tracking_params}
            clean_query = urlencode(clean_pairs, doseq=True)

            return urlunparse((scheme, netloc, path, "", clean_query, ""))
        except Exception:
            return url.strip().rstrip("/")

    @classmethod
    def deduplicate_evidence(
        cls,
        items: List[CreatorEvidenceItem],
        business_name: str = ""
    ) -> Tuple[List[CreatorEvidenceItem], int]:
        """
        Section 6: Deduplicates evidence items by (canonical_url, business_name).
        Returns: (unique_items, duplicates_removed_count)
        """
        seen_keys = set()
        unique_items = []
        duplicates_removed = 0

        for item in items:
            canon_url = item.canonical_url or cls.normalize_canonical_url(item.source_url or item.content_url)
            biz_key = (business_name or item.business_reference or "").strip().lower()
            key = (canon_url, biz_key) if canon_url else (id(item), biz_key)

            if key in seen_keys:
                duplicates_removed += 1
                continue
            seen_keys.add(key)
            # Ensure item has canonical_url set
            item.canonical_url = canon_url
            unique_items.append(item)

        return unique_items, duplicates_removed

    @classmethod
    def classify_source_quality(
        cls,
        url: str,
        source_domain: str = "",
        title: str = "",
        snippet: str = "",
        caption: str = "",
        business_name: str = ""
    ) -> Tuple[str, str]:
        """
        Section 3: Classifies source domains/types into HIGH_VALUE, MEDIUM, or LOW.
        HIGH_VALUE:
          - actual creator review/article
          - identifiable food blogger page
          - public video title/description clearly discussing venue
          - genuine public social post snippet with exact business context
        MEDIUM:
          - reputable local publication
          - directory with editorial/business context
          - aggregators with clear venue references
        LOW:
          - search snippets with little context
          - generic hashtag pages
          - generic location pages
          - scraped reposts
          - pages where the business relationship is unclear
        """
        url_low = (url or "").lower()
        domain_low = (source_domain or urlparse(url_low).netloc or "").lower()
        if domain_low.startswith("www."):
            domain_low = domain_low[4:]
        full_text = f"{title} {caption} {snippet}".strip()
        full_text_low = full_text.lower()

        # 1. LOW: Ambiguous listicle ("Top 10 places in Birmingham", "Top 5 ...", etc.)
        if re.search(r"\btop\s+\d+\b", full_text_low) or any(p in full_text_low for p in ["top 10", "top 5", "top places", "things to do in", "places to visit in", "best places to"]):
            return SourceQualityTier.LOW.value, "Ambiguous listicle"

        # 2. LOW: Comment mentions
        if any(p in full_text_low for p in ["commented:", "comment:", "replying to @", "replying to"]) or full_text_low.startswith("@"):
            return SourceQualityTier.LOW.value, "Comment mention"

        # 3. LOW: Generic hashtag exploration pages
        if any(p in url_low for p in ["/explore/tags/", "/tags/", "/tag/", "/hashtag/"]) or (
            any(url_low.endswith(f"/{t}") for t in ["food", "foodie", "ukfood", "eats", "birminghamfood"])
            and "/video/" not in url_low and "/p/" not in url_low
        ):
            return SourceQualityTier.LOW.value, "Generic hashtag exploration page"

        # 4. LOW: Generic location pages lacking venue context
        if any(p in url_low for p in ["/places/birmingham", "/locations/birmingham", "/location/birmingham", "/cities/birmingham", "/geo/"]) and not any(p in url_low for p in ["/video/", "/p/", "/review", "/restaurant/"]):
            return SourceQualityTier.LOW.value, "Generic location directory page"

        # 5. LOW: Search snippets with little context (< 40 chars or minimal text without video/post ID)
        if len(full_text) < 40 and not any(d in domain_low for d in ["tiktok.com", "instagram.com", "youtube.com"]):
            return SourceQualityTier.LOW.value, f"Thin snippet with insufficient context ({len(full_text)} chars)"

        # 6. LOW: Scraped reposts or generic scraper domains
        scraper_domains = ["picuki", "dumpoir", "greatfon", "imginn", "urlebird", "tiktokfull", "savefrom", "repostapp"]
        if any(sd in domain_low for sd in scraper_domains):
            return SourceQualityTier.LOW.value, f"Scraped repost domain ({domain_low})"

        # 7. HIGH_VALUE: Identifiable food blogger page or recognized editorial food publication
        editorial_food_domains = [
            "birminghammail.co.uk", "independent-birmingham.co.uk", "birminghamfoodie.com",
            "birminghamworld.uk", "secretbirmingham.com", "eater.com", "olivemagazine.com",
            "bbcgoodfood.com", "theculturetrip.com", "cntraveller.com", "foodandwine.com",
            "broadsheet.com.au", "manchestersfinest.com", "confidentials.com", "dinebirmingham.co.uk"
        ]
        if any(ed in domain_low for ed in editorial_food_domains):
            return SourceQualityTier.HIGH_VALUE.value, f"Recognized editorial food publication ({domain_low})"

        # Check for actual blog/article review URL with review context
        if any(p in url_low for p in ["/review/", "/reviews/", "/food-review", "-review-", "/blog/"]) and len(full_text) >= 45:
            return SourceQualityTier.HIGH_VALUE.value, "Identifiable creator/food blog review page"

        # 8. HIGH_VALUE / MEDIUM: Genuine public social video or post
        is_social_post = bool(
            ("tiktok.com" in domain_low and "/video/" in url_low) or
            ("instagram.com" in domain_low and ("/p/" in url_low or "/reel/" in url_low)) or
            ("youtube.com" in domain_low and ("/watch" in url_low or "/shorts/" in url_low))
        )
        if is_social_post:
            has_explicit_tag = bool(re.search(r"@[a-zA-Z0-9_\.]+", caption))
            has_structured_review = any(w in full_text_low for w in [
                "review", "tasting menu", "critic", "critique", "michelin", "chef", "unreal", "recommend"
            ])
            has_venue_dining = any(w in full_text_low for w in [
                "visited", "dining", "dined", "ate at", "eating", "food", "burger", "burgers",
                "wings", "dish", "dishes", "menu", "lunch", "dinner", "cocktails",
                "table", "restaurant", "cafe", "bistro", "patty", "patties", "bar"
            ])
            if (has_structured_review or has_explicit_tag) and has_venue_dining and len(full_text) >= 40:
                return SourceQualityTier.HIGH_VALUE.value, f"Public creator review video/post ({domain_low})"
            elif has_venue_dining or len(full_text) >= 30:
                return SourceQualityTier.MEDIUM.value, f"Creator social post with casual mention ({domain_low})"
            else:
                return SourceQualityTier.LOW.value, f"Generic social short/clip with minimal context ({domain_low})"

        # 9. MEDIUM: Reputable local publications, directories with editorial context, aggregators with clear venue references
        medium_directories = [
            "tripadvisor", "yelp", "opentable", "timeout", "squaremeal", "resy",
            "michelin", "hardens", "thefork", "restaurantguru", "visitbirmingham",
            "birminghamtimes", "expressandstar", "greatbritishlife", "birmingham-alive"
        ]
        if any(md in domain_low for md in medium_directories):
            return SourceQualityTier.MEDIUM.value, f"Reputable publication / directory with venue context ({domain_low})"

        # 10. Meaningful general context vs thin
        if len(full_text) >= 75 and any(w in full_text_low for w in ["restaurant", "cafe", "bar", "food", "menu", "dining", "visited"]):
            return SourceQualityTier.MEDIUM.value, "General web reference with meaningful venue context"

        return SourceQualityTier.LOW.value, f"Low-context generic source ({domain_low})"

    @classmethod
    def evaluate_contextual_evidence(
        cls,
        text: str,
        business_name: str = "",
        tagged_handles: Optional[List[str]] = None
    ) -> Tuple[bool, List[str]]:
        """
        Section 4: Contextual evidence requirement.
        A business-name string match is not enough.
        Requires contextual evidence such as:
        - review language
        - recommendation language
        - "visited"
        - "at [business]"
        - "dined at"
        - "ate at"
        - "restaurant"
        - venue-specific description
        - menu/item references
        - explicit creator/business relationship
        """
        if not text:
            return False, []

        text_low = text.lower()
        signals = []

        # 1. Review / recommendation language
        review_patterns = [
            r"\b(?:review|reviewed|reviewing|critic|critique)\b",
            r"\b(?:recommend|recommended|recommending|highly recommend)\b",
            r"\b(?:must try|must visit|top pick|hidden gem|favourite|favorite)\b",
            r"\b(?:rated|rating|unreal|incredible|mind blowing|blown away|insane)\b",
            r"\b(?:tasting menu|chef['']?s menu|omakase|unforgettable)\b"
        ]
        if any(re.search(p, text_low) for p in review_patterns):
            signals.append("review_recommendation_language")

        # 2. Dining / visiting actions
        action_patterns = [
            r"\b(?:visited|visiting|visit|dined|dining|dine)\b",
            r"\b(?:ate at|eat at|eating at|popped into|popped in|stopped by|checked out|went to)\b",
            r"\b(?:grabbed (?:lunch|dinner|a bite|coffee|drinks)|having (?:dinner|lunch|breakfast|brunch)|had (?:dinner|lunch|breakfast|brunch|an unforgettable))\b",
            r"\b(?:tried out|trying out|tried the|feasting|ordered|grabbing)\b"
        ]
        if any(re.search(p, text_low) for p in action_patterns):
            signals.append("dining_visiting_action")

        # 3. Prepositional venue context: "at {biz}", "in {biz}", "outside {biz}", "table at {biz}"
        norm_text = cls.normalize_text(text)
        if business_name:
            biz_norm = cls.normalize_text(business_name)
            prep_patterns = [
                r"\b(?:at|in|inside|outside|table at|heading to|trip to|meal at)\s+" + re.escape(biz_norm),
                r"\b(?:at|in|inside|outside|table at)\s+the\s+" + re.escape(biz_norm)
            ]
            if any(re.search(p, text_low) for p in prep_patterns) or any(re.search(p, norm_text) for p in prep_patterns):
                signals.append("prepositional_venue_context")

        # Text without business name to prevent business name tokens from falsely triggering menu/venue context
        text_without_biz = text_low
        if business_name:
            norm_b = cls.normalize_text(business_name)
            for w in norm_b.split():
                if len(w) >= 3:
                    text_without_biz = re.sub(r"\b" + re.escape(w) + r"\b", " ", text_without_biz)

        # 4. Venue descriptor / hospitality terms (must appear outside the business name itself)
        venue_descriptors = [
            r"\b(?:restaurant|cafe|café|bistro|eatery|diner|bar|pub|joint|bakery|kitchen|pizzeria|grill|streatery|venue|food hall|coffee shop|taproom|roastery|speakeasy|cantina|brasserie|cocktail bar|lounge)\b"
        ]
        if any(re.search(p, text_without_biz) for p in venue_descriptors):
            signals.append("venue_descriptor")

        # 5. Menu / item references (must appear outside the business name itself)
        menu_items = [
            r"\b(?:burger|burgers|patty|patties|wings|fries|chips|cocktail|cocktails|beer|beers|curry|pizza|pizzas|sushi|sashimi|bao|buns|chaat|samosas|tacos|pasta|steaks|steak|meat|feast|feasting|grilled|bbq|barbecue|roast|pastries|sourdough|coffee|tea|dessert|desserts|dish|dishes|menu|tasting|special|specials|wine|pint|pints)\b"
        ]
        if any(re.search(p, text_without_biz) for p in menu_items):
            signals.append("menu_item_references")

        # 6. Explicit creator / business relationship
        relation_patterns = [
            r"\b(?:collab|collaborated|collaboration|invited|gifted|pr visit|hosted by|press invite|ad|advertisement)\b"
        ]
        has_tagged = bool(tagged_handles and len(tagged_handles) > 0)
        if any(re.search(p, text_low) for p in relation_patterns) or has_tagged:
            signals.append("explicit_creator_relationship")

        has_context = len(signals) >= 1
        return has_context, signals

    @classmethod
    def match_business_name(
        cls,
        text: str,
        business_name: str,
        trading_name: str = ""
    ) -> Tuple[bool, bool, str]:
        """
        Matches exact business name, trading name, or common variations (Section 2).
        Returns: (is_match, is_exact, matched_substring)
        """
        if not text:
            return False, False, ""

        norm_text = cls.normalize_text(text)

        names_to_try = [n for n in [business_name, trading_name] if n]
        for b_name in names_to_try:
            norm_biz = cls.normalize_text(b_name)
            if not norm_biz:
                continue

            # 1. Exact phrase match
            pattern = r"\b" + re.escape(norm_biz) + r"\b"
            if re.search(pattern, norm_text, re.IGNORECASE):
                return True, True, b_name

            # 2. Core tokens check (strip common hospitality descriptors)
            stop_tokens = {"cafe", "café", "restaurant", "bar", "bakery", "kitchen", "deli",
                           "takeaway", "bbq", "smokehouse", "birmingham", "manchester", "the",
                           "and", "&", "ltd", "limited"}
            core_tokens = [t for t in norm_biz.split() if t not in stop_tokens]
            if core_tokens:
                core_phrase = " ".join(core_tokens)
                generic_food_nouns = {"burger", "burgers", "pizza", "pizzas", "coffee", "tea", "beer", "beers",
                                      "curry", "curries", "taco", "tacos", "steak", "pasta", "roast", "pie",
                                      "pies", "cake", "cakes", "noodle", "noodles", "food", "drinks", "eats"}
                if len(core_tokens) == 1 and core_phrase in generic_food_nouns:
                    pass
                elif len(core_phrase) >= 4 and re.search(r"\b" + re.escape(core_phrase) + r"\b", norm_text, re.IGNORECASE):
                    is_exact = (len(core_tokens) >= 2)
                    return True, is_exact, core_phrase

        return False, False, ""

    @classmethod
    def match_location(
        cls,
        text: str,
        target_city: str,
        target_address: str = "",
        location_tag: str = "",
        target_postcode: str = "",
        target_street: str = "",
        target_country: str = ""
    ) -> Dict[str, Any]:
        """
        Validates location signals (city, neighborhood, street, postcode, conflicting cities).
        """
        combined = f"{text} {location_tag}".lower()
        norm_combined = cls.normalize_text(combined)
        target_city_clean = (target_city or "").strip().lower()

        from lib.country_adapters import get_country_adapter
        adapter = get_country_adapter(target_country or "United Kingdom")
        major_cities = adapter.review_sources.get_major_cities() or MAJOR_UK_CITIES

        # Check for conflicting major cities
        conflicting_city = ""
        for c in major_cities:
            if c != target_city_clean and re.search(r"\b" + re.escape(c) + r"\b", combined):
                # If target city is also explicitly present, might be a comparison; otherwise conflict
                if not re.search(r"\b" + re.escape(target_city_clean) + r"\b", combined):
                    conflicting_city = c.capitalize()
                    break

        # City match
        city_match = bool(target_city_clean and re.search(r"\b" + re.escape(target_city_clean) + r"\b", combined))

        # Street & neighborhood matching
        street_matched = ""
        neighborhood_matched = ""
        postcode_matched = ""

        # Postcode matching
        postcode_cand = target_postcode.strip().lower()
        if not postcode_cand and target_address:
            extracted_pc = adapter.address_normalizer.extract_postal_code(target_address)
            if extracted_pc:
                postcode_cand = extracted_pc.lower()

        if postcode_cand:
            # Check full postcode or outward code (e.g. "m4" from "m4 5db")
            pc_clean = re.sub(r"\s+", " ", postcode_cand).strip()
            pc_outward = pc_clean.split()[0] if pc_clean else ""
            if re.search(r"\b" + re.escape(pc_clean) + r"\b", combined):
                postcode_matched = pc_clean.upper()
            elif pc_outward and len(pc_outward) >= 2 and re.search(r"\b" + re.escape(pc_outward) + r"\b", combined):
                postcode_matched = pc_outward.upper()

        full_address_check = f"{target_address} {target_street}".strip() if target_street else target_address
        if full_address_check:
            # Extract potential street name and neighborhood from address
            addr_parts = [p.strip() for p in full_address_check.split(",") if p.strip()]
            for part in addr_parts:
                part_low = part.lower()
                clean_street = cls.normalize_text(part_low)
                clean_street_no_num = re.sub(r"^\d+[a-zA-Z]?(?:-\d+[a-zA-Z]?)?\s+", "", clean_street).strip()
                # Check for street designators or direct target_street match
                street_designators = [
                    "road", "street", "lane", "arcade", "avenue", "walk", "way", "row",
                    "drive", "crescent", "passage", "close", "hill", "gate", "yard",
                    "alley", "place", "square", "gardens", "circus", "mews", "court", "wharf", "parade"
                ]
                if target_street or any(k in part_low for k in street_designators):
                    if clean_street_no_num and len(clean_street_no_num) >= 4 and (
                        re.search(r"\b" + re.escape(clean_street_no_num) + r"\b", norm_combined) or
                        re.search(r"\b" + re.escape(part_low) + r"\b", combined)
                    ):
                        street_matched = clean_street_no_num.title()
                        break
                    elif clean_street and len(clean_street) >= 4 and (
                        re.search(r"\b" + re.escape(clean_street) + r"\b", norm_combined) or
                        re.search(r"\b" + re.escape(part_low) + r"\b", combined)
                    ):
                        street_matched = part.strip()
                        break

                # Check for neighborhood / district
                if any(n in part_low for n in ["ancoats", "northern quarter", "jewellery quarter", "digbeth",
                                              "stirchley", "moseley", "harborne", "bearwood", "small heath",
                                              "saltley", "didsbury", "chorlton", "deansgate", "castlefield",
                                              "spinningfields", "salford", "edgbaston"]):
                    clean_neigh = cls.normalize_text(part_low)
                    clean_neigh_no_num = re.sub(r"^\d+[\w\s\-\/]*\s+", "", clean_neigh).strip()
                    if clean_neigh and re.search(r"\b" + re.escape(clean_neigh) + r"\b", combined):
                        neighborhood_matched = part.strip()
                    elif clean_neigh_no_num and re.search(r"\b" + re.escape(clean_neigh_no_num) + r"\b", combined):
                        neighborhood_matched = clean_neigh_no_num.title()

        # Location tag match
        location_tag_clean = location_tag.strip()

        return {
            "city_matched": city_match,
            "conflicting_city": conflicting_city,
            "street_matched": street_matched,
            "neighborhood_matched": neighborhood_matched,
            "postcode_matched": postcode_matched,
            "location_tag": location_tag_clean,
            "has_location_evidence": bool(city_match or street_matched or neighborhood_matched or postcode_matched or location_tag_clean)
        }

    @classmethod
    def extract_hashtags(cls, text: str, business_name: str) -> Tuple[List[str], List[str]]:
        """
        Extracts hashtags and distinguishes business-specific from generic (Section 5).
        Returns: (all_hashtags, business_specific_hashtags)
        """
        if not text:
            return [], []

        raw_tags = re.findall(r"#([a-zA-Z0-9_]+)", text)
        all_tags = [f"#{t}" for t in raw_tags]

        biz_slug = re.sub(r"[^a-z0-9]", "", business_name.lower())
        biz_specific = []

        for tag in raw_tags:
            tag_low = tag.lower()
            if tag_low in GENERIC_HASHTAGS:
                continue
            if biz_slug and (biz_slug in tag_low or tag_low in biz_slug):
                biz_specific.append(f"#{tag}")

        return all_tags, biz_specific

    @classmethod
    def classify_handle(
        cls,
        handle: str,
        business: Dict[str, Any],
        context_text: str = "",
        creator_handle: str = "",
        source_url: str = ""
    ) -> Tuple[str, str]:
        """
        Classifies an extracted handle to prevent mistaking creator usernames,
        commenters, or arbitrary @mentions for candidate business accounts (Section 2).
        Returns:
            (HandleClassification, audit_reason)
        """
        if not handle:
            return HandleClassification.NONE.value, "Empty handle"

        clean_h = handle.strip().lstrip("@").rstrip(".!?,:;").lower()
        if not clean_h:
            return HandleClassification.NONE.value, "Empty handle"

        # 1. Platform / Media Aggregator exclusion
        generic_handles = {
            "instagram", "tiktok", "youtube", "facebook", "reels", "shorts", "explore",
            "foodbible", "secretmanchester", "secretbirmingham", "birminghammail",
            "timeout", "eatmcr", "ukfood", "ukfoodie", "tripadvisor", "deliveroo",
            "ubereats", "justeat", "google", "bbcgoodfood"
        }
        if clean_h in generic_handles:
            return HandleClassification.NONE.value, f"Platform/aggregator account (@{clean_h})"

        # 2. Check if Creator Handle
        clean_creator = creator_handle.strip().lstrip("@").rstrip(".!?,:;").lower()
        if clean_creator and clean_h == clean_creator:
            return HandleClassification.CREATOR_HANDLE.value, f"Matches creator's own handle (@{clean_creator})"

        if source_url:
            url_match = re.search(r"(?:tiktok\.com/@|instagram\.com/)([a-zA-Z0-9_\.]+)", source_url)
            if url_match:
                url_user = url_match.group(1).rstrip(".!?,:;").lower()
                if clean_h == url_user:
                    return HandleClassification.CREATOR_HANDLE.value, f"Matches author handle in source URL (@{url_user})"

        # 3. Check Business Match
        biz_name = (business.get("company_name") or business.get("title") or "").strip()
        trading_name = (business.get("trading_name") or "").strip()
        clean_biz_slug = re.sub(r"[^a-z0-9]", "", biz_name.lower())
        clean_trad_slug = re.sub(r"[^a-z0-9]", "", trading_name.lower()) if trading_name else ""
        handle_slug = re.sub(r"[^a-z0-9]", "", clean_h)

        biz_slug_matched = False
        if clean_biz_slug and len(clean_biz_slug) >= 4:
            if clean_biz_slug in handle_slug:
                biz_slug_matched = True
            elif handle_slug in clean_biz_slug and len(handle_slug) >= max(4, int(len(clean_biz_slug) * 0.75)):
                biz_slug_matched = True
            elif handle_slug.startswith(clean_biz_slug):
                biz_slug_matched = True

        if clean_trad_slug and len(clean_trad_slug) >= 4:
            if clean_trad_slug in handle_slug:
                biz_slug_matched = True
            elif handle_slug in clean_trad_slug and len(handle_slug) >= max(4, int(len(clean_trad_slug) * 0.75)):
                biz_slug_matched = True

        stop_tokens = {"the", "and", "&", "ltd", "limited", "restaurant", "cafe", "café", "bar", "kitchen", "birmingham", "manchester", "uk"}
        biz_tokens = [re.sub(r"[^a-z0-9]", "", t.lower()) for t in biz_name.split() if t.lower() not in stop_tokens]
        biz_tokens = [t for t in biz_tokens if len(t) >= 3]

        token_match_count = sum(1 for t in biz_tokens if t in handle_slug)
        has_multi_token_match = (
            (len(biz_tokens) >= 2 and token_match_count >= 2) or
            (len(biz_tokens) == 1 and token_match_count == 1 and len(biz_tokens[0]) >= 4) or
            (biz_tokens and len(biz_tokens[0]) >= 5 and handle_slug.startswith(biz_tokens[0]) and any(handle_slug.endswith(sfx) for sfx in ["group", "uk", "bham", "restaurant", "official", "co", "hq", "ltd"]))
        )

        # Check commenter / companion / conversational patterns first
        commenter_patterns = [
            r"(?:thanks|thank you|credit|photo|with|love this|try this|go here)\s+@?" + re.escape(clean_h),
            r"@?" + re.escape(clean_h) + r"\s+(?:look|we need to|lets go|let\'s go|lol|haha|omg)"
        ]
        if any(re.search(p, context_text, re.IGNORECASE) for p in commenter_patterns) and not (biz_slug_matched or has_multi_token_match):
            return HandleClassification.COMMENTER_HANDLE.value, f"Handle @{clean_h} identified as commenter/conversational mention"

        if (biz_slug_matched or has_multi_token_match) and not (clean_h == clean_creator):
            return HandleClassification.CANDIDATE_OFFICIAL_ACCOUNT.value, f"Handle @{clean_h} matches business name '{biz_name}'"

        has_venue_tag_context = bool(
            re.search(r"(?:at|visit|visiting|table at|food from|head to|check out|dinner at|lunch at|cocktails at|drinks at)\s+@?" + re.escape(clean_h), context_text, re.IGNORECASE)
        )

        if has_venue_tag_context:
            if has_multi_token_match or biz_slug_matched or (len(biz_tokens) == 1 and token_match_count == 1 and len(biz_tokens[0]) >= 4):
                return HandleClassification.CANDIDATE_OFFICIAL_ACCOUNT.value, f"Handle @{clean_h} explicitly tagged in venue context"
            elif token_match_count == 1:
                return HandleClassification.AMBIGUOUS.value, f"Handle @{clean_h} has partial token match with '{biz_name}' in venue context"

        # 4. Check creator keywords
        creator_keywords = ["blogger", "foodie", "creator", "review", "reviews", "diaries", "roamer", "explorer", "guide", "channel", "eats", "connoisseur", "takeaways", "morning"]
        if any(k in clean_h for k in creator_keywords):
            return HandleClassification.CREATOR_HANDLE.value, f"Handle @{clean_h} contains creator/blogger terms without business match"

        # 5. Check commenter patterns
        if any(re.search(p, context_text, re.IGNORECASE) for p in commenter_patterns):
            return HandleClassification.COMMENTER_HANDLE.value, f"Handle @{clean_h} identified as commenter/conversational mention"

        # 6. Ambiguous handle
        if token_match_count == 1 or clean_h in ["food", "drinks", "burger", "cafe", "patty", "eats", "birmingham"]:
            return HandleClassification.AMBIGUOUS.value, f"Handle @{clean_h} has partial token match with '{biz_name}', requires verification"

        return HandleClassification.COMMENTER_HANDLE.value, f"Handle @{clean_h} does not match business '{biz_name}'"

    @classmethod
    def extract_tagged_business_handles(
        cls,
        text: str,
        creator_handle: str = "",
        tagged_handles: Optional[List[str]] = None
    ) -> List[str]:
        """
        Extracts tagged business handles, strictly excluding the creator's own handle.
        """
        candidates = []
        if tagged_handles:
            for h in tagged_handles:
                clean_h = h.strip().lstrip("@").rstrip(".!?,:;").lower()
                if clean_h:
                    candidates.append(clean_h)

        if text:
            found = re.findall(r"@([a-zA-Z0-9_\.]+)", text)
            for f in found:
                clean_f = f.strip().lstrip("@").rstrip(".!?,:;").lower()
                if clean_f and clean_f not in candidates:
                    candidates.append(clean_f)

        creator_clean = creator_handle.strip().lstrip("@").lower()
        filtered = []
        for c in candidates:
            if c == creator_clean:
                continue
            if c in ["instagram", "tiktok", "youtube", "facebook", "reels", "shorts", "explore"]:
                continue
            if any(agg in c for agg in ["foodbible", "secretmanchester", "timeout", "eatmcr"]):
                continue
            filtered.append(c)

        return filtered

    @classmethod
    def evaluate_creator_post(
        cls,
        post_data: Dict[str, Any],
        business: Dict[str, Any]
    ) -> CreatorEvidenceItem:
        """
        Evaluates a single public creator post against a target business.
        Handles all reference types:
          - ACCOUNT_TAG
          - CAPTION_BUSINESS_MENTION
          - CAPTION_LOCATION_MENTION
          - LOCATION_TAG
          - HASHTAG_MENTION
          - ON_CONTENT_TEXT
          - TITLE_OR_DESCRIPTION
          - MULTI_SIGNAL
        """
        creator_name = (post_data.get("creator_name") or "").strip()
        creator_handle = (post_data.get("creator_handle") or "").strip()
        platform = (post_data.get("platform") or "Instagram").strip()
        content_url = (post_data.get("content_url") or "").strip()
        content_type = (post_data.get("content_type") or "Post").strip()
        published_at = (post_data.get("published_at") or "").strip()

        caption = (post_data.get("caption") or post_data.get("caption_excerpt") or "").strip()
        location_tag_val = (post_data.get("location_tag") or post_data.get("location_tag_name") or post_data.get("location_name") or post_data.get("location_reference") or "").strip()
        location_tag_url = (post_data.get("location_tag_url") or "").strip()
        location_tag_text = (post_data.get("location_tag_text") or location_tag_val).strip()

        on_content_text = (post_data.get("on_content_text") or post_data.get("content_text_reference") or "").strip()
        title_or_desc = (post_data.get("title") or post_data.get("description") or "").strip()
        tagged_handles = post_data.get("tagged_handles") or post_data.get("tagged_accounts") or []
        source_url = (post_data.get("source_url") or content_url).strip()

        biz_name = business.get("company_name") or business.get("title") or business.get("business_name") or ""
        trading_name = business.get("trading_name") or ""
        city = business.get("city") or "Manchester"
        address = business.get("address") or ""
        category = business.get("category") or business.get("categoryName") or "Restaurant"

        canonical_url = cls.normalize_canonical_url(source_url or content_url)
        source_domain_raw = urlparse(source_url or content_url).netloc.lower()
        if source_domain_raw.startswith("www."):
            source_domain_raw = source_domain_raw[4:]
        source_quality, sq_reason = cls.classify_source_quality(
            url=source_url or content_url,
            source_domain=source_domain_raw,
            title=title_or_desc,
            caption=caption,
            business_name=biz_name
        )

        freshness = cls.calculate_freshness(published_at)

        # Reject private / restricted content (Section 5)
        if post_data.get("is_private") or "private" in (post_data.get("privacy") or "").lower():
            return CreatorEvidenceItem(
                creator_evidence_status=CreatorEvidenceStatus.REJECTED.value,
                creator_name=creator_name,
                creator_handle=creator_handle,
                platform=platform,
                content_url=content_url,
                content_type=content_type,
                published_at=published_at,
                evidence_confidence=CreatorEvidenceConfidence.UNKNOWN.value,
                disqualification_reason="Private / login-restricted content is strictly prohibited",
                discovered_at=datetime.now(timezone.utc).isoformat(),
                trust_concept=CreatorTrustConcept.CREATOR_DISCOVERY_SIGNAL.value,
                evidence_tier=EvidenceTier.DISCOVERY_ONLY.value,
                source_quality=source_quality,
                contextual_match=False,
                context_signals=[],
                canonical_url=canonical_url
            )

        # ── SIGNAL 1: CAPTION / TEXT BUSINESS NAME MENTION (Section 2) ──
        name_matched, is_exact_name, matched_name = cls.match_business_name(
            f"{caption} {title_or_desc}",
            biz_name,
            trading_name=trading_name
        )

        # ── SIGNAL 2: ON-CONTENT TEXT (Section 6) ──
        on_content_matched = False
        if on_content_text:
            oc_match, oc_exact, oc_name = cls.match_business_name(on_content_text, biz_name, trading_name=trading_name)
            if oc_match:
                on_content_matched = True

        # ── SIGNAL 3: LOCATION EVALUATION (Section 3, 4, 8) ──
        loc_eval = cls.match_location(
            f"{caption} {title_or_desc}",
            target_city=city,
            target_address=address,
            location_tag=location_tag_val,
            target_postcode=business.get("postcode") or "",
            target_street=business.get("street") or ""
        )

        if loc_eval["conflicting_city"]:
            return CreatorEvidenceItem(
                creator_evidence_status=CreatorEvidenceStatus.REJECTED.value,
                creator_name=creator_name,
                creator_handle=creator_handle,
                platform=platform,
                content_url=content_url,
                content_type=content_type,
                published_at=published_at,
                business_reference=matched_name or "None",
                location_reference=f"Conflicting city: {loc_eval['conflicting_city']}",
                evidence_confidence=CreatorEvidenceConfidence.UNKNOWN.value,
                source_url=source_url,
                discovered_at=datetime.now(timezone.utc).isoformat(),
                freshness=freshness,
                disqualification_reason=f"REJECTED: Conflicting city detected: {loc_eval['conflicting_city']} (target {city})",
                classification=CreatorEvidenceClassification.INVALID.value,
                handle_classification=HandleClassification.NONE.value,
                audit_judgment="INVALID",
                audit_reason=f"Conflicting city detected: {loc_eval['conflicting_city']}",
                trust_concept=CreatorTrustConcept.CREATOR_DISCOVERY_SIGNAL.value,
                evidence_tier=EvidenceTier.DISCOVERY_ONLY.value,
                source_quality=source_quality,
                contextual_match=False,
                context_signals=[],
                canonical_url=canonical_url
            )

        # ── SIGNAL 4: LOCATION TAG (Section 4) ──
        location_tag_matched = False
        venue_location_tag_matched = False
        if location_tag_val:
            loc_tag_match, _, _ = cls.match_business_name(location_tag_val, biz_name, trading_name=trading_name)
            if loc_tag_match:
                venue_location_tag_matched = True
                location_tag_matched = True
            elif city and city.lower() in location_tag_val.lower():
                location_tag_matched = True

        # ── SIGNAL 5: HASHTAGS (Section 5) ──
        all_tags, biz_tags = cls.extract_hashtags(f"{caption} {title_or_desc}", biz_name)
        has_biz_hashtag = len(biz_tags) > 0

        # ── SIGNAL 6: ACCOUNT TAG (Section 1 & 2) ──
        raw_handles = cls.extract_tagged_business_handles(
            caption,
            creator_handle=creator_handle,
            tagged_handles=tagged_handles
        )

        valid_candidate_handles = []
        primary_handle_class = HandleClassification.NONE.value
        primary_handle_reason = ""
        handle_matches_biz = False

        for h in raw_handles:
            h_class, h_reason = cls.classify_handle(
                handle=h,
                business=business,
                context_text=f"{caption} {title_or_desc}",
                creator_handle=creator_handle,
                source_url=source_url
            )
            if h_class == HandleClassification.CANDIDATE_OFFICIAL_ACCOUNT.value:
                valid_candidate_handles.append(h)
                if not handle_matches_biz:
                    handle_matches_biz = True
                    primary_handle_class = h_class
                    primary_handle_reason = h_reason
            elif primary_handle_class == HandleClassification.NONE.value:
                primary_handle_class = h_class
                primary_handle_reason = h_reason

        primary_tagged_handle = valid_candidate_handles[0] if valid_candidate_handles else ""

        # ── SIGNAL 7: CATEGORY / CUISINE CONTEXT (Section 3 & 8) ──
        category_matched = False
        cat_tokens = [t.lower() for t in category.split() if t.lower() not in ["restaurant", "cafe", "takeaway", "bar"]]
        if cat_tokens:
            for ct in cat_tokens:
                if len(ct) >= 4 and ct in f"{caption} {title_or_desc}".lower():
                    category_matched = True
                    break

        # ── COMPILE MATCHING SIGNALS (Section 7 & 13) ──
        matching_signals = []
        if is_exact_name:
            matching_signals.append("exact_business_name")
        elif name_matched:
            matching_signals.append("business_name_mention")

        if on_content_matched:
            matching_signals.append("on_content_text")

        if handle_matches_biz:
            matching_signals.append("tagged_business_handle")

        if location_tag_matched:
            matching_signals.append("matching_location_tag")

        if has_biz_hashtag:
            matching_signals.append("business_specific_hashtag")

        if loc_eval["street_matched"]:
            matching_signals.append(f"exact_street_mention ({loc_eval['street_matched']})")

        if loc_eval["neighborhood_matched"]:
            matching_signals.append(f"neighborhood_mention ({loc_eval['neighborhood_matched']})")

        if loc_eval.get("postcode_matched"):
            matching_signals.append(f"postcode_mention ({loc_eval['postcode_matched']})")

        if loc_eval["city_matched"]:
            matching_signals.append(f"city_mention ({city})")

        if category_matched:
            matching_signals.append(f"category_context ({category})")

        # ── REJECTION IF ZERO RELEVANT SIGNALS ──
        has_corroborated_location = bool(
            (loc_eval["street_matched"] or loc_eval["neighborhood_matched"] or loc_eval.get("postcode_matched")) and
            (category_matched or (loc_eval["street_matched"] and loc_eval["neighborhood_matched"]))
        )

        if not name_matched and not on_content_matched and not handle_matches_biz and not has_biz_hashtag and not location_tag_matched and not has_corroborated_location:
            return CreatorEvidenceItem(
                creator_evidence_status=CreatorEvidenceStatus.REJECTED.value,
                creator_name=creator_name,
                creator_handle=creator_handle,
                platform=platform,
                content_url=content_url,
                content_type=content_type,
                published_at=published_at,
                business_reference="None",
                evidence_confidence=CreatorEvidenceConfidence.UNKNOWN.value,
                source_url=source_url,
                discovered_at=datetime.now(timezone.utc).isoformat(),
                freshness=freshness,
                disqualification_reason="REJECTED: No relevant business name, tag, or corroborated location signals found",
                classification=CreatorEvidenceClassification.INVALID.value,
                handle_classification=primary_handle_class,
                audit_judgment="INVALID",
                audit_reason="No relevant business name, tag, or corroborated location signals found",
                trust_concept=CreatorTrustConcept.CREATOR_DISCOVERY_SIGNAL.value,
                evidence_tier=EvidenceTier.DISCOVERY_ONLY.value,
                source_quality=source_quality,
                contextual_match=False,
                context_signals=[],
                canonical_url=canonical_url
            )

        # ── DETERMINE REFERENCE TYPE (Section 1) ──
        has_caption_mention = bool(name_matched and caption)
        has_account_tag = bool(handle_matches_biz)
        has_loc_tag = bool(location_tag_matched)
        has_hashtag = bool(has_biz_hashtag)
        has_on_content = bool(on_content_matched)
        has_title_desc = bool(title_or_desc and name_matched and not caption)

        distinct_modalities = sum([
            1 if has_caption_mention else 0,
            1 if has_account_tag else 0,
            1 if has_loc_tag else 0,
            1 if has_hashtag else 0,
            1 if has_on_content else 0,
            1 if has_title_desc else 0,
        ])

        if distinct_modalities >= 2:
            reference_type = CreatorReferenceType.MULTI_SIGNAL.value
        elif has_account_tag:
            reference_type = CreatorReferenceType.ACCOUNT_TAG.value
        elif has_loc_tag and not has_caption_mention:
            reference_type = CreatorReferenceType.LOCATION_TAG.value
        elif has_hashtag and not has_caption_mention:
            reference_type = CreatorReferenceType.HASHTAG_MENTION.value
        elif has_on_content and not has_caption_mention:
            reference_type = CreatorReferenceType.ON_CONTENT_TEXT.value
        elif has_title_desc:
            reference_type = CreatorReferenceType.TITLE_OR_DESCRIPTION.value
        elif has_caption_mention:
            reference_type = CreatorReferenceType.CAPTION_BUSINESS_MENTION.value
        elif loc_eval["has_location_evidence"] and not name_matched:
            reference_type = CreatorReferenceType.CAPTION_LOCATION_MENTION.value
        else:
            reference_type = CreatorReferenceType.CAPTION_BUSINESS_MENTION.value

        # ── SCORING & CONFIDENCE RULES (Part 3) ──
        # Higher confidence when multiple independent signals agree:
        # - exact business name
        # - correct city
        # - correct street/neighborhood
        # - visible business name
        # - creator/content context clearly related to the business
        # - repeated references across sources
        # Lower confidence for:
        # - generic location-only mentions
        # - ambiguous business names
        # - old content without current corroboration
        # - unclear relationship between venue and mention

        evidence_score = 0.0

        # Exact / Core business name matching
        if is_exact_name:
            evidence_score += 35.0
        elif name_matched:
            evidence_score += 20.0

        if on_content_matched:
            evidence_score += 15.0

        if handle_matches_biz:
            evidence_score += 30.0

        if has_biz_hashtag:
            evidence_score += 20.0

        # Location matching
        if loc_eval["street_matched"]:
            evidence_score += 25.0
        elif loc_eval["neighborhood_matched"]:
            evidence_score += 20.0
        elif location_tag_matched:
            evidence_score += 20.0
        elif loc_eval.get("postcode_matched"):
            evidence_score += 20.0

        if loc_eval["city_matched"]:
            evidence_score += 15.0

        # Creator / review context
        has_creator_context = bool(
            any(w in f"{caption} {title_or_desc}".lower() for w in ["review", "food blogger", "food creator", "reel", "menu", "taste", "delicious", "eats", "tried"])
        )
        if has_creator_context:
            evidence_score += 15.0

        # Ambiguous business name penalty (short generic single-word names without street corroboration)
        clean_biz_words = [w for w in biz_name.split() if w.lower() not in ["the", "&", "and", "ltd", "limited"]]
        if len(clean_biz_words) == 1 and len(clean_biz_words[0]) <= 6 and not loc_eval["street_matched"] and not loc_eval["neighborhood_matched"]:
            evidence_score -= 20.0

        # Old content penalty
        if freshness == CreatorFreshness.STALE.value:
            evidence_score -= 15.0

        has_strong_name = bool(name_matched or on_content_matched)
        has_strong_tag = bool(handle_matches_biz)
        has_specific_loc = bool(loc_eval["neighborhood_matched"] or loc_eval["street_matched"] or venue_location_tag_matched or loc_eval.get("postcode_matched"))
        is_location_only = not has_strong_name and not has_strong_tag and not has_biz_hashtag

        # Contextual evidence evaluation (Section 4)
        contextual_match, context_signals = cls.evaluate_contextual_evidence(
            text=f"{caption} {title_or_desc}",
            business_name=biz_name,
            tagged_handles=valid_candidate_handles
        )
        if contextual_match:
            matching_signals.append(f"contextual_evidence ({', '.join(context_signals[:2])})")

        # Check ambiguous business names (Section 1)
        biz_name_clean = biz_name.strip().lower()
        AMBIGUOUS_NAMES = {
            "the lounge", "lounge", "corner cafe", "burger bar", "central bakery",
            "pizza place", "coffee shop", "the bar", "station cafe", "bella italia",
            "the kitchen", "the cafe", "brunch", "k2", "superios", "the bridge"
        }
        clean_biz_words = [w for w in biz_name_clean.split() if w not in ["the", "&", "and", "ltd", "limited"]]
        is_ambiguous_biz_name = (
            biz_name_clean in AMBIGUOUS_NAMES or
            (len(clean_biz_words) == 1 and clean_biz_words[0] in {"lounge", "corner", "burger", "central", "bakery", "pizza", "coffee", "brunch", "bar", "cafe", "grill"})
        )

        has_venue_mention = bool(
            has_creator_context or
            contextual_match or
            any(w in f"{caption} {title_or_desc}".lower() for w in [
                "food", "dish", "dishes", "table", "dinner", "lunch", "breakfast",
                "brunch", "eating", "eats", "visited", "visiting", "dining", "cocktail", "cocktails",
                "pints", "craft beer", "steaks", "burger", "burgers", "wings", "curry", "pizza", "fish and chips",
                "patty", "patties", "try", "recommend", "best", "delicious",
                "pastries", "bread", "buns", "pastry", "coffee", "tea", "chai", "samosas",
                "drinks", "drink", "meal", "menu", "tasting", "fresh", "freshest", "treats", "dessert"
            ])
        )

        classification = CreatorEvidenceClassification.INVALID.value
        audit_judgment = "INVALID"
        audit_reason = ""

        if is_location_only:
            # Location-only mentions must NEVER be automatically accepted or HIGH confidence
            if loc_eval["street_matched"] and (category_matched or loc_eval["neighborhood_matched"]):
                confidence = CreatorEvidenceConfidence.MEDIUM.value
                classification = CreatorEvidenceClassification.AMBIGUOUS.value
                audit_judgment = "AMBIGUOUS"
                audit_reason = "Location-only reference with street and category match, lacking explicit business name"
            else:
                confidence = CreatorEvidenceConfidence.LOW.value
                classification = CreatorEvidenceClassification.INVALID.value
                audit_judgment = "INVALID"
                audit_reason = "Location-only reference lacking business name or handle corroboration"
        elif is_ambiguous_biz_name:
            # Ambiguous generic names REQUIRE specific street/neighborhood, postcode, or official handle corroboration
            has_ambiguous_corroboration = bool(loc_eval["neighborhood_matched"] or loc_eval["street_matched"] or loc_eval.get("postcode_matched") or venue_location_tag_matched or handle_matches_biz)
            if has_ambiguous_corroboration and has_venue_mention and contextual_match and source_quality != SourceQualityTier.LOW.value:
                classification = CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value
                audit_judgment = "VALID THIRD-PARTY REFERENCE"
                audit_reason = f"Ambiguous name '{biz_name}' corroborated by street/neighborhood or official handle"
                if evidence_score >= 70.0:
                    confidence = CreatorEvidenceConfidence.HIGH.value
                else:
                    confidence = CreatorEvidenceConfidence.MEDIUM.value
            else:
                classification = CreatorEvidenceClassification.AMBIGUOUS.value
                audit_judgment = "AMBIGUOUS"
                audit_reason = f"Ambiguous generic business name '{biz_name}' lacking street/neighborhood corroboration"
                evidence_score = min(evidence_score, 35.0)
                confidence = CreatorEvidenceConfidence.LOW.value
        elif not is_exact_name and not on_content_matched and not handle_matches_biz and not has_biz_hashtag:
            # Partial name collision
            classification = CreatorEvidenceClassification.INVALID.value
            audit_judgment = "INVALID"
            audit_reason = "Partial name collision without exact business match or handle"
            confidence = CreatorEvidenceConfidence.LOW.value
        else:
            # Strong business reference candidate (exact name, tagged handle, or hashtag)
            if not contextual_match:
                # Section 4: A business-name string match is not enough without contextual evidence
                classification = CreatorEvidenceClassification.AMBIGUOUS.value
                audit_judgment = "AMBIGUOUS"
                audit_reason = f"Mention of '{biz_name}' lacks contextual evidence (no dining, review, or venue context)"
                confidence = CreatorEvidenceConfidence.LOW.value
            elif source_quality == SourceQualityTier.LOW.value:
                # Section 3: LOW sources can discover a candidate but should rarely classify it as verified/corroborated
                classification = CreatorEvidenceClassification.AMBIGUOUS.value
                audit_judgment = "AMBIGUOUS"
                audit_reason = f"Low quality source ({sq_reason}); reference capped at DISCOVERY_ONLY"
                confidence = CreatorEvidenceConfidence.LOW.value
            elif (loc_eval["city_matched"] or has_specific_loc or handle_matches_biz) and has_venue_mention:
                classification = CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value
                audit_judgment = "VALID THIRD-PARTY REFERENCE"
                audit_reason = f"Exact business reference with {' + '.join(matching_signals[:3])}"
                if (is_exact_name and has_specific_loc) or (handle_matches_biz and (has_specific_loc or loc_eval["city_matched"])) or evidence_score >= 70.0:
                    confidence = CreatorEvidenceConfidence.HIGH.value
                else:
                    confidence = CreatorEvidenceConfidence.MEDIUM.value
            elif loc_eval["city_matched"] or has_specific_loc:
                classification = CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value
                audit_judgment = "VALID THIRD-PARTY REFERENCE"
                audit_reason = f"Business reference matching location ({loc_eval['city_matched'] or has_specific_loc})"
                if evidence_score >= 70.0:
                    confidence = CreatorEvidenceConfidence.HIGH.value
                else:
                    confidence = CreatorEvidenceConfidence.MEDIUM.value
            else:
                classification = CreatorEvidenceClassification.AMBIGUOUS.value
                audit_judgment = "AMBIGUOUS"
                audit_reason = "Business mention without city or venue corroboration"
                confidence = CreatorEvidenceConfidence.LOW.value

        # ── ASSIGN EVIDENCE TIER & TRUST CONCEPT (Section 1 & 2) ──
        evidence_tier = EvidenceTier.DISCOVERY_ONLY.value
        trust_concept = CreatorTrustConcept.CREATOR_DISCOVERY_SIGNAL.value

        if classification == CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value and source_quality != SourceQualityTier.LOW.value:
            has_strong_loc = bool(loc_eval["city_matched"] and (loc_eval["street_matched"] or loc_eval["neighborhood_matched"] or loc_eval.get("postcode_matched") or venue_location_tag_matched))
            has_strong_context = bool(len(context_signals) >= 2 or has_creator_context)

            # Check handle validation where a handle is involved
            handle_is_verified = False
            if primary_tagged_handle:
                if post_data.get("handle_verified") is True or post_data.get("handle_independently_validated") is True:
                    handle_is_verified = True
                elif post_data.get("handle_verified") is False or post_data.get("handle_independently_validated") is False:
                    handle_is_verified = False
                else:
                    b_ig = (business.get("instagram_url") or "").lower()
                    b_v = [u.lower() for u in business.get("verified_social_urls", [])]
                    handle_low = primary_tagged_handle.lower()
                    if handle_low in b_ig or any(handle_low in u for u in b_v):
                        handle_is_verified = True
                    else:
                        handle_is_verified = False
            else:
                # No handle involved in the creator reference
                handle_is_verified = True

            # VERIFIED criteria:
            # - strong business identity match
            # - strong location match (city + street/neighborhood/postcode/venue tag)
            # - source clearly discusses the business (meaningful context >= 2 signals or review)
            # - where a handle is involved, handle has independent validation
            # - source quality is HIGH_VALUE (or MEDIUM with exact street corroboration)
            is_verified_candidate = (
                is_exact_name and
                has_strong_loc and
                has_strong_context and
                (handle_is_verified or (loc_eval["street_matched"] and ("menu_item_references" in context_signals or "venue_descriptor" in context_signals))) and
                (source_quality == SourceQualityTier.HIGH_VALUE.value or (source_quality == SourceQualityTier.MEDIUM.value and loc_eval["street_matched"]))
            )

            if is_verified_candidate:
                evidence_tier = EvidenceTier.VERIFIED.value
                trust_concept = CreatorTrustConcept.CREATOR_VERIFIED_EVIDENCE.value
            elif is_exact_name and loc_eval["city_matched"] and contextual_match and len(matching_signals) >= 2:
                evidence_tier = EvidenceTier.CORROBORATED.value
                trust_concept = CreatorTrustConcept.CREATOR_DISCOVERY_SIGNAL.value
            else:
                evidence_tier = EvidenceTier.DISCOVERY_ONLY.value
                trust_concept = CreatorTrustConcept.CREATOR_DISCOVERY_SIGNAL.value
        else:
            evidence_tier = EvidenceTier.DISCOVERY_ONLY.value
            trust_concept = CreatorTrustConcept.CREATOR_DISCOVERY_SIGNAL.value

        source_domain = source_domain_raw or urlparse(source_url or content_url).netloc.lower()
        discovery_query = (post_data.get("discovery_query") or post_data.get("query") or "").strip()
        biz_name_matched = bool(name_matched or on_content_matched or handle_matches_biz or has_biz_hashtag)
        loc_matched = bool(loc_eval["city_matched"] or loc_eval["street_matched"] or loc_eval["neighborhood_matched"] or location_tag_matched or loc_eval.get("postcode_matched"))

        summary_desc = (
            f"Third-party {platform} {content_type} by {creator_handle or creator_name or 'creator'}: "
            f"Reference type [{reference_type}] with {len(matching_signals)} signal(s) ({', '.join(matching_signals[:3])}). "
            f"Tier: {evidence_tier} ({trust_concept}). Classification: {classification}. "
            f"Confidence: {confidence} (score: {evidence_score:.1f}). Source: {source_quality} ({source_domain})."
        )

        return CreatorEvidenceItem(
            creator_evidence_status=CreatorEvidenceStatus.FOUND.value,
            creator_name=creator_name,
            creator_handle=creator_handle,
            platform=platform,
            content_url=content_url,
            content_type=content_type,
            published_at=published_at,
            business_reference=matched_name or (biz_name if (handle_matches_biz or on_content_matched) else "Location-corroborated reference"),
            location_reference=location_tag_val or (city if loc_eval["city_matched"] else (loc_eval["street_matched"] or "Unknown")),
            tagged_business_handle=f"@{primary_tagged_handle}" if primary_tagged_handle else "",
            caption_excerpt=caption[:250],
            evidence_confidence=confidence,
            source_url=source_url,
            discovered_at=datetime.now(timezone.utc).isoformat(),
            freshness=freshness,
            disqualification_reason="",
            reference_type=reference_type,
            business_name_mentioned=bool(name_matched or on_content_matched),
            location_mentioned=loc_eval["street_matched"] or loc_eval["neighborhood_matched"] or (city if loc_eval["city_matched"] else ""),
            location_tag=location_tag_val,
            location_tag_name=location_tag_val,
            location_tag_url=location_tag_url,
            location_tag_text=location_tag_text,
            business_tag=f"@{primary_tagged_handle}" if primary_tagged_handle else "",
            hashtags=all_tags,
            content_text_reference=on_content_text[:100],
            caption_reference=caption[:200],
            matching_signals=matching_signals,
            evidence_summary=summary_desc,
            source_domain=source_domain,
            discovery_query=discovery_query,
            business_name_match=biz_name_matched,
            location_match=loc_matched,
            evidence_score=round(evidence_score, 1),
            candidate_official_handle=primary_tagged_handle,
            candidate_official_handles=valid_candidate_handles,
            city_match=bool(loc_eval["city_matched"]),
            street_match=bool(loc_eval["street_matched"]),
            street_postcode_match=bool(loc_eval["street_matched"] or loc_eval.get("postcode_matched")),
            classification=classification,
            handle_classification=primary_handle_class,
            audit_judgment=audit_judgment,
            audit_reason=audit_reason,
            trust_concept=trust_concept,
            evidence_tier=evidence_tier,
            source_quality=source_quality,
            contextual_match=contextual_match,
            context_signals=context_signals,
            canonical_url=canonical_url
        )

    # Alias for flexible invocation across discovery and validation
    evaluate_content_item = evaluate_creator_post

    @classmethod
    def discover_and_validate_official_social(
        cls,
        item: CreatorEvidenceItem,
        business: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Enforces Section 9, 10, & 11:
        CREATOR CONTENT CAN DISCOVER OFFICIAL SOCIAL.
        Extracts tagged business handle and validates independently via SocialIdentityValidator.
        Creator tag is ONLY a discovery lead.
        """
        raw_handle = item.tagged_business_handle.strip().lstrip("@")
        if not raw_handle:
            return None

        platform = item.platform.lower()
        if "instagram" in platform or platform == "reel":
            profile_url = f"https://www.instagram.com/{raw_handle}/"
            norm_platform = "instagram"
        elif "facebook" in platform:
            profile_url = f"https://www.facebook.com/{raw_handle}/"
            norm_platform = "facebook"
        else:
            profile_url = f"https://www.instagram.com/{raw_handle}/"
            norm_platform = "instagram"

        biz_name = business.get("company_name") or business.get("title") or ""
        city = business.get("city") or ""
        category = business.get("category") or business.get("categoryName") or "Restaurant"

        audit = SocialIdentityValidator.validate_social_identity(
            raw_url=profile_url,
            platform=norm_platform,
            business_name=biz_name,
            city=city,
            category=category,
            strict_accessibility=True
        )

        ownership_status = audit.get("social_ownership_status")
        profile_status = audit.get("social_profile_status")
        is_verified = (ownership_status == SocialOwnershipStatus.VERIFIED.value)

        if is_verified:
            # When candidate handle is independently validated, promote creator evidence to VERIFIED tier if other conditions met
            if (
                item.classification == CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value
                and item.city_match
                and (item.street_match or item.location_match)
                and item.source_quality != SourceQualityTier.LOW.value
            ):
                item.evidence_tier = EvidenceTier.VERIFIED.value
                item.trust_concept = CreatorTrustConcept.CREATOR_VERIFIED_EVIDENCE.value

        return {
            "candidate_handle": f"@{raw_handle}",
            "platform": norm_platform.capitalize(),
            "profile_url": profile_url,
            "creator_handle": item.creator_handle,
            "creator_content_url": item.content_url,
            "validation_status": "VERIFIED" if is_verified else "REJECTED",
            "social_ownership_status": ownership_status,
            "social_profile_status": profile_status,
            "validation_reason": audit.get("rejection_reason") or "Independently verified official business profile",
            "verified_url": audit.get("clean_url") if is_verified else ""
        }

    @classmethod
    def extract_candidate_businesses_from_content(
        cls,
        creator_post: Dict[str, Any],
        candidate_pool: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Bi-directional Discovery (Section 10):
        Extracts candidate business references from creator content and matches
        against known businesses.
        Does NOT create a lead until identity verification passes.
        """
        matches = []
        for biz in candidate_pool:
            item = cls.evaluate_creator_post(creator_post, biz)
            if item.creator_evidence_status == CreatorEvidenceStatus.FOUND.value and item.evidence_confidence in [CreatorEvidenceConfidence.HIGH.value, CreatorEvidenceConfidence.MEDIUM.value]:
                matches.append({
                    "business": biz,
                    "evidence_item": item,
                    "confidence": item.evidence_confidence,
                    "reference_type": item.reference_type,
                    "matching_signals": item.matching_signals
                })
        return matches

    @classmethod
    def summarize_evidence(
        cls,
        items: List[CreatorEvidenceItem],
        business: Dict[str, Any]
    ) -> CreatorEvidenceSummary:
        """
        Aggregates multiple creator evidence items into a structured summary (Section 13 & 14).
        """
        valid_items = [it for it in items if it.creator_evidence_status == CreatorEvidenceStatus.FOUND.value]

        if not valid_items:
            all_rejected = any(it.creator_evidence_status == CreatorEvidenceStatus.REJECTED.value for it in items)
            return CreatorEvidenceSummary(
                creator_evidence_status=CreatorEvidenceStatus.REJECTED.value if all_rejected else CreatorEvidenceStatus.NOT_FOUND.value,
                creator_evidence_count=0,
                creator_evidence_confidence=CreatorEvidenceConfidence.UNKNOWN.value,
                creator_latest_date="",
                creator_evidence_summary="No matching third-party creator evidence discovered." if not all_rejected else "All creator references rejected (wrong city or mismatch).",
                creator_evidence_urls=[],
                creator_discovered_at=datetime.now(timezone.utc).isoformat(),
                items=items,
                discovered_official_handles=[]
            )

        count = len(valid_items)

        # Confidence hierarchy
        if any(it.evidence_confidence == CreatorEvidenceConfidence.HIGH.value for it in valid_items):
            overall_conf = CreatorEvidenceConfidence.HIGH.value
        elif any(it.evidence_confidence == CreatorEvidenceConfidence.MEDIUM.value for it in valid_items):
            overall_conf = CreatorEvidenceConfidence.MEDIUM.value
        else:
            overall_conf = CreatorEvidenceConfidence.LOW.value

        dates = [it.published_at for it in valid_items if it.published_at]
        latest_date = max(dates) if dates else ""

        urls = [it.content_url for it in valid_items if it.content_url]

        platforms = sorted(list(set(it.platform for it in valid_items if it.platform)))
        plat_str = " & ".join(platforms) if platforms else "Social Media"

        ref_types = sorted(list(set(it.reference_type for it in valid_items if it.reference_type)))
        ref_types_str = ", ".join(ref_types[:3])

        discovered_official = []
        for it in valid_items:
            if it.tagged_business_handle:
                disc_res = cls.discover_and_validate_official_social(it, business)
                if disc_res and not any(d["candidate_handle"] == disc_res["candidate_handle"] for d in discovered_official):
                    discovered_official.append(disc_res)

        summary_text = (
            f"{count} public creator reference{'s' if count != 1 else ''} found on {plat_str} "
            f"[{ref_types_str}] (Confidence: {overall_conf}, Latest: {latest_date or 'N/A'}). "
            f"Third-party corroboration of customer-facing presence."
        )

        return CreatorEvidenceSummary(
            creator_evidence_status=CreatorEvidenceStatus.FOUND.value,
            creator_evidence_count=count,
            creator_evidence_confidence=overall_conf,
            creator_latest_date=latest_date,
            creator_evidence_summary=summary_text,
            creator_evidence_urls=urls,
            creator_discovered_at=datetime.now(timezone.utc).isoformat(),
            items=items,
            discovered_official_handles=discovered_official
        )

    @classmethod
    def generate_search_queries(cls, business: Dict[str, Any]) -> List[str]:
        """
        Generates targeted search queries without requiring tags (Section 14).
        """
        biz_name = business.get("company_name") or business.get("title") or ""
        city = business.get("city") or "Manchester"
        category = business.get("category") or business.get("categoryName") or "Restaurant"
        address = business.get("address") or ""

        clean_name = biz_name.replace('"', '').strip()
        clean_city = city.replace('"', '').strip()

        queries = [
            f'"{clean_name}" "{clean_city}"',
            f'"{clean_name}" review',
            f'"{clean_name}" reel',
            f'"{clean_name}" food',
            f'"{clean_city}" "{category}" "{clean_name}"',
            f'"{clean_name}" "{clean_city}" food blogger'
        ]

        # Add street or neighborhood query if available
        if address:
            addr_parts = [p.strip().replace('"', '') for p in address.split(",") if p.strip()]
            for part in addr_parts:
                if any(k in part.lower() for k in ["road", "street", "lane", "arcade", "avenue", "ancoats", "jewellery quarter", "stirchley", "moseley"]):
                    queries.append(f'"{clean_name}" "{part}"')
                    break

        return queries

    @classmethod
    def generate_hidden_discovery_queries(cls, city: str, neighborhood: str = "") -> List[str]:
        """
        Generates queries to discover hidden businesses from creator content (Section 14).
        """
        clean_city = city.replace('"', '').strip()
        queries = [
            f'"{clean_city}" restaurant reel',
            f'"{clean_city}" cafe reel',
            f'"{clean_city}" food blogger',
            f'"{clean_city}" food creator'
        ]
        if neighborhood:
            clean_neigh = neighborhood.replace('"', '').strip()
            queries.extend([
                f'"{clean_neigh}" restaurant review',
                f'"{clean_neigh}" cafe Instagram'
            ])
        return queries

    @classmethod
    def should_search_creator_evidence(cls, lead: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Search priority rules (Section 17).
        """
        social_status = lead.get("social_status")
        social_ownership = lead.get("social_ownership_status")
        social_profile_status = lead.get("social_profile_status")
        operational_status = lead.get("operational_status")
        ig_url = lead.get("instagram_url")
        fb_url = lead.get("facebook_url")

        if not ig_url and not fb_url:
            return True, "Official business social profile is missing"
        if social_ownership in ["UNVERIFIED", "UNKNOWN", None]:
            return True, "Official business social account is unverified or unclear"
        if social_profile_status in ["INACCESSIBLE", "INVALID_FORMAT"]:
            return True, "Existing social profile URL is invalid or inaccessible"
        if operational_status in ["ACTIVE_LIKELY", "OPERATIONAL_UNKNOWN"]:
            return True, "Operational evidence is weak and needs corroboration"

        return False, "Official social and operational evidence are already verified"


def evaluate_creator_post(post: Dict[str, Any], business: Any = None, **kwargs) -> CreatorEvidenceItem:
    """Flexible wrapper to evaluate a creator post against business dict or kwargs."""
    if isinstance(business, str):
        biz_dict = {"company_name": business}
        for k in ["city", "neighborhood", "address", "street", "postcode", "category"]:
            if k in kwargs:
                biz_dict[k] = kwargs[k]
        return CreatorEvidenceValidator.evaluate_creator_post(post, biz_dict)
    elif isinstance(business, dict):
        return CreatorEvidenceValidator.evaluate_creator_post(post, business)
    elif business is None and "business_name" in kwargs:
        biz_dict = {
            "company_name": kwargs["business_name"],
            "city": kwargs.get("city", ""),
            "neighborhood": kwargs.get("neighborhood", ""),
            "address": kwargs.get("address", ""),
            "street": kwargs.get("street", ""),
            "postcode": kwargs.get("postcode", ""),
            "category": kwargs.get("category", "")
        }
        return CreatorEvidenceValidator.evaluate_creator_post(post, biz_dict)
    return CreatorEvidenceValidator.evaluate_creator_post(post, business or {})


evaluate_content_item = evaluate_creator_post
CreatorEvidenceEngine = CreatorEvidenceValidator

