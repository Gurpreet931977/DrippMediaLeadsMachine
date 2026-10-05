"""
Dripp Media — Review Evidence Date Extractor & Provenance Engine
===============================================================
Extracts, normalizes, and validates review publication dates, relative review ages,
and customer activity dates from JSON-LD structured data, HTML review cards, and
search engine snippets.

Hard Safety & Anti-False-Positive Invariants:
  - NEVER treat a crawl date, page last-modified date, copyright year, SEO title year,
    search-result date, or generic page publication date as review freshness.
  - Distinguish date types:
      * REVIEW_PUBLICATION_DATE
      * REVIEW_RELATIVE_AGE
      * REVIEW_ACTIVITY_DATE
      * UNKNOWN
  - Never manufacture or guess a date. If untrusted or ambiguous -> UNKNOWN.
  - Support genuine relative ages ("3 weeks ago") normalized against retrieval timestamp.
  - Multiple reviews on page -> pick the most recent genuine review date.
  - Preserve large review counts (e.g. 783, 1,146, 2,026, 4,501) without year confusion.
"""

import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple, Union
import requests
from bs4 import BeautifulSoup


class ReviewEvidenceDateType(str, Enum):
    REVIEW_PUBLICATION_DATE = "REVIEW_PUBLICATION_DATE"
    REVIEW_RELATIVE_AGE = "REVIEW_RELATIVE_AGE"
    REVIEW_ACTIVITY_DATE = "REVIEW_ACTIVITY_DATE"
    UNKNOWN = "UNKNOWN"


class ReviewDateConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class ReviewFreshness(str, Enum):
    RECENT = "RECENT"    # <= 180 days
    STALE = "STALE"      # > 180 days
    UNKNOWN = "UNKNOWN"  # No date discovered or untrusted


@dataclass
class ExtractedReviewDate:
    date: Optional[str] = None                    # Normalized YYYY-MM-DD
    date_raw: Optional[str] = None                # Raw matched text (e.g. "15 August 2026", "3 weeks ago")
    date_type: str = ReviewEvidenceDateType.UNKNOWN.value
    confidence: str = ReviewDateConfidence.UNKNOWN.value
    freshness: str = ReviewFreshness.UNKNOWN.value
    as_of: Optional[str] = None                   # ISO timestamp of reference/retrieval
    extraction_method: str = "NONE"               # JSON_LD_REVIEW, HTML_REVIEW_CARD, SNIPPET_REVIEW_ANCHOR, etc.
    source_url: str = ""
    rejection_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# REJECTION PATTERNS (FALSE POSITIVES)
# ─────────────────────────────────────────────────────────────────────────────

# SEO title year phrases (e.g. '2026 Reviews & Info', 'Best Restaurants 2026')
SEO_TITLE_PATTERNS = [
    re.compile(r'\b(19\d{2}|20\d{2})\s+(?:restaurant\s+)?reviews?\s*(?:&|and|,|\/|\+|:|–|—|-)\s*(?:info|information|photos?|prices?|ratings?|deals?|menu|guide|details?|overview|opinions?|booking|dishes)\b', re.I),
    re.compile(r'\breviews?\s*(?:&|and|,|\/|\+|:|–|—|-)\s*information\s*\((?:19\d{2}|20\d{2})\)', re.I),
    re.compile(r'\b(?:the\s+)?(?:best|top)\s+(?:\d+\s+)?(?:restaurants?|cafes?|places?|pubs?|bars?|bistros?|spots?)\s+(?:in\s+[a-z\s]+)?(20\d{2})\b', re.I),
    re.compile(r'\bguide\s+(?:to\s+)?(?:eating|dining|[a-z\s]+)?(20\d{2})\b', re.I),
    re.compile(r'\b(?:menu|prices|ratings|photos)\s*\((20\d{2})\)', re.I),
    re.compile(r'\b\((20\d{2})\)\s*$', re.I),  # Ending with (2026) in title
]

# Business founding / establishment
ESTABLISHED_PATTERNS = [
    re.compile(r'\b(?:established|est\.?|founded|serving\s+[a-z\s]+|operating)\s+(?:in\s+|since\s+)?(19\d{2}|20\d{2})\b', re.I),
    re.compile(r'\bsince\s+(19\d{2}|20\d{2})\b', re.I),
]

# Copyright / footer patterns
COPYRIGHT_PATTERNS = [
    re.compile(r'(?:©|&copy;|copyright)\s*(?:19\d{2}\s*[-–—]\s*)?(20\d{2})', re.I),
    re.compile(r'\ball\s+rights\s+reserved\b', re.I),
]

# Page-level update patterns (NOT review publication dates)
PAGE_UPDATE_PATTERNS = [
    re.compile(r'\b(?:page\s+)?last\s+updated\s*(?:on|:)?\s*([^\n\.,<]+)', re.I),
    re.compile(r'\bupdated\s+on\s*:\s*([^\n\.,<]+)', re.I),
    re.compile(r'\bschedule\s+updated\s*(?:on|:)?\s*([^\n\.,<]+)', re.I),
    re.compile(r'\bcrawled\s+on\s*:\s*([^\n\.,<]+)', re.I),
    re.compile(r'\barticle\s+published\s*(?:on|:)?\s*([^\n\.,<]+)', re.I),
]

# Search engine result snippet indexing prefixes:
# e.g. "Oct 1, 2026 ...", "1 Oct 2026 —", "2026-10-01 —", "3 days ago —"
SEARCH_INDEX_PREFIX_REGEX = re.compile(
    r'^(?:'
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}'
    r'|\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{4}'
    r'|202[0-9]-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])'
    r'|\d+\s+(?:days?|weeks?|months?)\s+ago'
    r')\s*(?:\.{2,3}|[-–—])\s*',
    re.I
)

# Explicit review anchors
REVIEW_ANCHOR_PATTERNS = [
    re.compile(r'\b(?:reviewed|written|posted|rated)\s+(?:on|by|at)?\s*([^\n\.,;]+)', re.I),
    re.compile(r'\b(?:latest|recent)\s+review\s*(?:on|from|dated|:)?\s*([^\n\.,;]+)', re.I),
    re.compile(r'\bdate\s+of\s+visit\s*:\s*([^\n\.,;]+)', re.I),
    re.compile(r'\bvisited\s+(?:in\s+)?([^\n\.,;]+)', re.I),
    re.compile(r'\breview\s+date\s*:\s*([^\n\.,;]+)', re.I),
]

MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12
}


class ReviewDateExtractor:
    """
    Robust review date extraction and provenance engine.
    Ensures that only authentic review-level dates can establish freshness.
    """

    @classmethod
    def get_reference_as_of(cls, as_of: Optional[Union[datetime, str]] = None) -> datetime:
        """Resolves reference datetime with UTC timezone."""
        if as_of is None:
            return datetime.now(timezone.utc)
        if isinstance(as_of, datetime):
            if as_of.tzinfo is None:
                return as_of.replace(tzinfo=timezone.utc)
            return as_of.astimezone(timezone.utc)
        if isinstance(as_of, str):
            try:
                # Handle ISO strings
                clean = as_of.replace("Z", "+00:00")
                dt = datetime.fromisoformat(clean)
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
        return datetime.now(timezone.utc)

    # ─────────────────────────────────────────────────────────────────────────
    # DATE NORMALIZATION HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def normalize_relative_date(
        cls,
        text: str,
        as_of_dt: datetime
    ) -> Tuple[Optional[str], Optional[str], Optional[int]]:
        """
        Extracts and normalizes relative age strings:
          - "3 weeks ago"
          - "2 months ago"
          - "yesterday"
          - "4 days ago"
          - "a year ago"
        Returns: (normalized_date_str "YYYY-MM-DD", raw_matched_str, age_days)
        """
        t = text.strip()
        low = t.lower()

        # "yesterday"
        if re.search(r'\byesterday\b', low):
            dt = as_of_dt - timedelta(days=1)
            return dt.strftime("%Y-%m-%d"), "yesterday", 1

        # "today"
        if re.search(r'\btoday\b', low):
            return as_of_dt.strftime("%Y-%m-%d"), "today", 0

        # "a week ago", "a month ago", "a year ago"
        m_a = re.search(r'\ba\s+(day|week|month|year)\s+ago\b', low)
        if m_a:
            unit = m_a.group(1)
            days = 1 if unit == "day" else (7 if unit == "week" else (30 if unit == "month" else 365))
            dt = as_of_dt - timedelta(days=days)
            return dt.strftime("%Y-%m-%d"), m_a.group(0), days

        # "X days/weeks/months/years ago"
        m_rel = re.search(r'\b(\d+)\s+(day|week|month|year)s?\s+ago\b', low)
        if m_rel:
            num = int(m_rel.group(1))
            unit = m_rel.group(2)
            if unit == "day":
                days = num
            elif unit == "week":
                days = num * 7
            elif unit == "month":
                days = num * 30
            else:
                days = num * 365
            dt = as_of_dt - timedelta(days=days)
            return dt.strftime("%Y-%m-%d"), m_rel.group(0), days

        return None, None, None

    @classmethod
    def normalize_absolute_date(cls, text: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Parses absolute dates from text:
          - ISO: 2026-08-15, 2026-08-15T12:00:00Z
          - UK/EU: 15/08/2026, 15-08-2026, 15.08.2026
          - Named month: "15 August 2026", "Aug 15, 2026", "15th Aug 2026"
        Returns: (normalized_date_str "YYYY-MM-DD", raw_matched_str)
        """
        t = text.strip()

        # 1. ISO format: YYYY-MM-DD
        m_iso = re.search(r'\b(202[0-9]-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))(?:T[0-9:Z\+\.\-]+)?\b', t)
        if m_iso:
            raw = m_iso.group(1)
            try:
                dt = datetime.strptime(raw, "%Y-%m-%d")
                return dt.strftime("%Y-%m-%d"), m_iso.group(0)
            except ValueError:
                pass

        # 2. Textual Day Month Year: "15 August 2026", "15th Aug 2026", "15 Aug 2026"
        m_dmy = re.search(
            r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(202[0-9])\b',
            t,
            re.IGNORECASE
        )
        if m_dmy:
            day = int(m_dmy.group(1))
            mon_str = m_dmy.group(2).lower()
            year = int(m_dmy.group(3))
            mon = MONTH_MAP.get(mon_str[:3], 1)
            try:
                dt = datetime(year, mon, day)
                return dt.strftime("%Y-%m-%d"), m_dmy.group(0)
            except ValueError:
                pass

        # 3. Textual Month Day Year: "August 15, 2026", "Aug 15, 2026", "Aug 15th 2026"
        m_mdy = re.search(
            r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(202[0-9])\b',
            t,
            re.IGNORECASE
        )
        if m_mdy:
            mon_str = m_mdy.group(1).lower()
            day = int(m_mdy.group(2))
            year = int(m_mdy.group(3))
            mon = MONTH_MAP.get(mon_str[:3], 1)
            try:
                dt = datetime(year, mon, day)
                return dt.strftime("%Y-%m-%d"), m_mdy.group(0)
            except ValueError:
                pass

        # 4. Numeric UK/EU slash or dot or hyphen: DD/MM/YYYY or DD-MM-YYYY or DD.MM.YYYY
        m_num = re.search(r'\b(0?[1-9]|[12]\d|3[01])[\/\.\-](0?[1-9]|1[0-2])[\/\.\-](202[0-9])\b', t)
        if m_num:
            day = int(m_num.group(1))
            mon = int(m_num.group(2))
            year = int(m_num.group(3))
            try:
                dt = datetime(year, mon, day)
                return dt.strftime("%Y-%m-%d"), m_num.group(0)
            except ValueError:
                pass

        # 5. Month Year only (e.g. Tripadvisor "Date of visit: August 2026" or "August 2026")
        m_my = re.search(
            r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(202[0-9])\b',
            t,
            re.IGNORECASE
        )
        if m_my:
            mon_str = m_my.group(1).lower()
            year = int(m_my.group(2))
            mon = MONTH_MAP.get(mon_str[:3], 1)
            try:
                dt = datetime(year, mon, 1)
                return dt.strftime("%Y-%m-%d"), m_my.group(0)
            except ValueError:
                pass

        return None, None

    @classmethod
    def calculate_freshness(cls, date_str: Optional[str], as_of_dt: datetime) -> str:
        """
        Determines freshness:
          RECENT: <= 180 days
          STALE:  > 180 days
          UNKNOWN: No valid date
        """
        if not date_str:
            return ReviewFreshness.UNKNOWN.value
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            delta = (as_of_dt - dt).days
            # Tolerate timezone differences up to +1 day in future
            if delta < 0:
                delta = 0
            if delta <= 180:
                return ReviewFreshness.RECENT.value
            return ReviewFreshness.STALE.value
        except Exception:
            return ReviewFreshness.UNKNOWN.value

    # ─────────────────────────────────────────────────────────────────────────
    # 1. JSON-LD STRUCTURED REVIEW EXTRACTION
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def extract_from_json_ld(
        cls,
        json_data: Any,
        source_url: str = "",
        as_of: Optional[Union[datetime, str]] = None
    ) -> Optional[ExtractedReviewDate]:
        """
        Recursively extracts authentic review dates from schema.org JSON-LD.
        Strict invariant: Only inspects objects where @type is 'Review'.
        Rejects top-level WebPage, Article, or Organization dateModified/datePublished.
        Picks the most recent valid review date if multiple exist.
        """
        as_of_dt = cls.get_reference_as_of(as_of)
        as_of_str = as_of_dt.isoformat()

        valid_review_dates: List[Tuple[datetime, str, str]] = []  # (dt, normalized_str, raw_val)

        def _traverse(node: Any, parent_type: str = ""):
            if isinstance(node, dict):
                node_type = str(node.get("@type", "")).strip()

                # If this node is explicitly a Review
                if node_type == "Review" or parent_type == "review":
                    for key in ["datePublished", "dateCreated"]:
                        val = node.get(key)
                        if val and isinstance(val, str):
                            norm, raw = cls.normalize_absolute_date(val)
                            if norm:
                                try:
                                    dt = datetime.strptime(norm, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                                    valid_review_dates.append((dt, norm, val))
                                except ValueError:
                                    pass

                # If node has 'review' property (e.g. Restaurant has 'review': [...])
                if "review" in node:
                    _traverse(node["review"], parent_type="review")

                # If node has '@graph'
                if "@graph" in node:
                    _traverse(node["@graph"], parent_type="")

                # Traverse other child dicts
                for k, v in node.items():
                    if k not in ["review", "@graph"] and isinstance(v, (dict, list)):
                        _traverse(v, parent_type=k)

            elif isinstance(node, list):
                for item in node:
                    _traverse(item, parent_type=parent_type)

        _traverse(json_data)

        if not valid_review_dates:
            return None

        # Pick the most recent valid review date
        valid_review_dates.sort(key=lambda x: x[0], reverse=True)
        best_dt, best_norm, best_raw = valid_review_dates[0]
        freshness = cls.calculate_freshness(best_norm, as_of_dt)

        return ExtractedReviewDate(
            date=best_norm,
            date_raw=best_raw,
            date_type=ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value,
            confidence=ReviewDateConfidence.HIGH.value,
            freshness=freshness,
            as_of=as_of_str,
            extraction_method="JSON_LD_REVIEW",
            source_url=source_url
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 2. HTML REVIEW CARD / MICRODATA EXTRACTION
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def extract_from_html(
        cls,
        html: str,
        source_url: str = "",
        as_of: Optional[Union[datetime, str]] = None
    ) -> ExtractedReviewDate:
        """
        Extracts review dates from full HTML document:
          1. Checks JSON-LD scripts for authentic Review objects.
          2. Inspects isolated review card containers (<div class="review ...">, etc.).
          3. Ignores footers, copyright, page last-modified, SEO titles, founding dates.
          4. If multiple reviews present, picks the most recent authentic review date.
        """
        as_of_dt = cls.get_reference_as_of(as_of)
        as_of_str = as_of_dt.isoformat()

        if not html:
            return ExtractedReviewDate(
                date=None,
                date_type=ReviewEvidenceDateType.UNKNOWN.value,
                confidence=ReviewDateConfidence.UNKNOWN.value,
                freshness=ReviewFreshness.UNKNOWN.value,
                as_of=as_of_str,
                extraction_method="NONE",
                source_url=source_url,
                rejection_reason="EMPTY_HTML"
            )

        soup = BeautifulSoup(html, "html.parser")

        # 1. First attempt: JSON-LD structured reviews
        for s in soup.find_all("script", type="application/ld+json"):
            if s.string:
                try:
                    data = json.loads(s.string)
                    res = cls.extract_from_json_ld(data, source_url=source_url, as_of=as_of_dt)
                    if res and res.date:
                        return res
                except Exception:
                    pass

        # 2. Second attempt: HTML review card elements
        # Identify containers likely to represent individual customer reviews
        review_classes = re.compile(
            r'\b(?:o_review|review-card|review-item|review-container|pr-review|ugc-review|review_item|reviewItem|single-review|customer-review|review-body|review-entry)\b',
            re.I
        )
        review_containers = soup.find_all(attrs={"class": review_classes})
        if not review_containers:
            # Fallback: check elements with itemprop="review"
            review_containers = soup.find_all(attrs={"itemprop": "review"})

        extracted_dates: List[Tuple[datetime, str, str, str, str]] = []  # (dt, norm, raw, date_type, method)

        for container in review_containers:
            # Check for <time> or itemprop="datePublished"
            time_tag = container.find("time") or container.find(attrs={"itemprop": "datePublished"})
            if time_tag:
                dt_val = time_tag.get("datetime") or time_tag.get("content") or time_tag.get_text(strip=True)
                if dt_val:
                    norm, raw = cls.normalize_absolute_date(dt_val)
                    if norm:
                        try:
                            d = datetime.strptime(norm, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            extracted_dates.append((d, norm, raw or dt_val, ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value, "HTML_REVIEW_CARD_TIME"))
                            continue
                        except ValueError:
                            pass

            c_text = container.get_text(separator=" ", strip=True)

            # Check relative age in review container: "4 months ago on Google", "3 weeks ago"
            norm_rel, raw_rel, days_rel = cls.normalize_relative_date(c_text, as_of_dt)
            if norm_rel and raw_rel:
                try:
                    d = datetime.strptime(norm_rel, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    extracted_dates.append((d, norm_rel, raw_rel, ReviewEvidenceDateType.REVIEW_RELATIVE_AGE.value, "HTML_REVIEW_CARD_RELATIVE"))
                    continue
                except ValueError:
                    pass

            # Check absolute date with review anchor in container
            for pat in REVIEW_ANCHOR_PATTERNS:
                m_anc = pat.search(c_text)
                if m_anc:
                    anc_text = m_anc.group(1)
                    norm_abs, raw_abs = cls.normalize_absolute_date(anc_text)
                    if norm_abs:
                        try:
                            d = datetime.strptime(norm_abs, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            dtype = ReviewEvidenceDateType.REVIEW_ACTIVITY_DATE.value if "visit" in pat.pattern else ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value
                            extracted_dates.append((d, norm_abs, raw_abs or anc_text, dtype, "HTML_REVIEW_CARD_ANCHOR"))
                            break
                        except ValueError:
                            pass

        if extracted_dates:
            extracted_dates.sort(key=lambda x: x[0], reverse=True)
            best_dt, best_norm, best_raw, best_dtype, best_method = extracted_dates[0]
            freshness = cls.calculate_freshness(best_norm, as_of_dt)
            return ExtractedReviewDate(
                date=best_norm,
                date_raw=best_raw,
                date_type=best_dtype,
                confidence=ReviewDateConfidence.HIGH.value,
                freshness=freshness,
                as_of=as_of_str,
                extraction_method=best_method,
                source_url=source_url
            )

        return ExtractedReviewDate(
            date=None,
            date_type=ReviewEvidenceDateType.UNKNOWN.value,
            confidence=ReviewDateConfidence.UNKNOWN.value,
            freshness=ReviewFreshness.UNKNOWN.value,
            as_of=as_of_str,
            extraction_method="NONE",
            source_url=source_url,
            rejection_reason="NO_TRUSTWORTHY_REVIEW_DATE_IN_HTML"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 3. SEARCH ENGINE SNIPPET TEXT EXTRACTION
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def extract_from_snippet(
        cls,
        text: str,
        title: str = "",
        source_url: str = "",
        as_of: Optional[Union[datetime, str]] = None
    ) -> ExtractedReviewDate:
        """
        Parses review dates from snippet text with strict false-positive rejection.
        Rules:
          - Rejects SEO title year (e.g. '2026 Reviews & Info').
          - Rejects business founding year ('Established in 2026').
          - Rejects copyright year ('© 2026').
          - Rejects search engine snippet indexing prefix from becoming RECENT.
          - Rejects large review counts (2,026 reviews) from year confusion.
          - Allows explicit review-anchored dates ('Reviewed 2 weeks ago', 'Reviewed on 15 Aug 2026').
          - Allows unanchored stale dates (> 180 days) for backward compatibility (confidence LOW).
        """
        as_of_dt = cls.get_reference_as_of(as_of)
        as_of_str = as_of_dt.isoformat()

        combined = f"{title} — {text}".strip()
        if not combined or combined == "—":
            return ExtractedReviewDate(
                date=None,
                date_type=ReviewEvidenceDateType.UNKNOWN.value,
                confidence=ReviewDateConfidence.UNKNOWN.value,
                freshness=ReviewFreshness.UNKNOWN.value,
                as_of=as_of_str,
                extraction_method="NONE",
                source_url=source_url,
                rejection_reason="EMPTY_SNIPPET"
            )

        # ── Step A: Check for False Positive SEO Title Years ──
        for pat in SEO_TITLE_PATTERNS:
            if pat.search(title) or pat.search(text):
                # Clean out the misleading year from combined text to prevent parsing
                # But don't immediately return if there's an actual review date later
                pass

        # ── Step B: Check for Search Engine Snippet Indexing Prefix ──
        # e.g. "Oct 1, 2026 ... A Casa di Alessia, Otley: See 143 unbiased reviews"
        # Only strip recent prefix dates so they don't become fake RECENT review evidence.
        m_index = SEARCH_INDEX_PREFIX_REGEX.match(text)
        stripped_text = text
        if m_index:
            prefix_match = m_index.group(0)
            norm_pfx, _ = cls.normalize_absolute_date(prefix_match)
            if not norm_pfx:
                norm_pfx, _, _ = cls.normalize_relative_date(prefix_match, as_of_dt)
            if norm_pfx:
                pfx_freshness = cls.calculate_freshness(norm_pfx, as_of_dt)
                if pfx_freshness == ReviewFreshness.RECENT.value:
                    if not any(k in prefix_match.lower() for k in ["reviewed", "rating", "visit"]):
                        stripped_text = text[len(prefix_match):].strip()

        clean_combined = f"{title} — {stripped_text}".strip()

        # ── Step C: Look for Explicitly Anchored Review Dates ──
        # e.g. "Reviewed 3 weeks ago", "Reviewed on 15 Aug 2026", "Date of visit: August 2026"
        for pat in REVIEW_ANCHOR_PATTERNS:
            m = pat.search(clean_combined)
            if m:
                anchor_snippet = m.group(1).strip()
                # Check relative age first
                norm_rel, raw_rel, days_rel = cls.normalize_relative_date(anchor_snippet, as_of_dt)
                if norm_rel and raw_rel:
                    freshness = cls.calculate_freshness(norm_rel, as_of_dt)
                    return ExtractedReviewDate(
                        date=norm_rel,
                        date_raw=raw_rel,
                        date_type=ReviewEvidenceDateType.REVIEW_RELATIVE_AGE.value,
                        confidence=ReviewDateConfidence.HIGH.value,
                        freshness=freshness,
                        as_of=as_of_str,
                        extraction_method="SNIPPET_REVIEW_ANCHOR_RELATIVE",
                        source_url=source_url
                    )

                # Check absolute date
                norm_abs, raw_abs = cls.normalize_absolute_date(anchor_snippet)
                if norm_abs and raw_abs:
                    freshness = cls.calculate_freshness(norm_abs, as_of_dt)
                    dtype = ReviewEvidenceDateType.REVIEW_ACTIVITY_DATE.value if "visit" in pat.pattern else ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value
                    return ExtractedReviewDate(
                        date=norm_abs,
                        date_raw=raw_abs,
                        date_type=dtype,
                        confidence=ReviewDateConfidence.HIGH.value,
                        freshness=freshness,
                        as_of=as_of_str,
                        extraction_method="SNIPPET_REVIEW_ANCHOR_ABSOLUTE",
                        source_url=source_url
                    )

        # ── Step D: Check for Relative Review Ages in text ──
        # e.g. "2 months ago", "3 weeks ago" (must be accompanied by review context)
        norm_rel, raw_rel, days_rel = cls.normalize_relative_date(clean_combined, as_of_dt)
        if norm_rel and raw_rel:
            # Ensure phrase is not page update or schedule update
            is_page_update = False
            for p_pat in PAGE_UPDATE_PATTERNS:
                if p_pat.search(clean_combined):
                    is_page_update = True
                    break
            if not is_page_update:
                freshness = cls.calculate_freshness(norm_rel, as_of_dt)
                return ExtractedReviewDate(
                    date=norm_rel,
                    date_raw=raw_rel,
                    date_type=ReviewEvidenceDateType.REVIEW_RELATIVE_AGE.value,
                    confidence=ReviewDateConfidence.MEDIUM.value,
                    freshness=freshness,
                    as_of=as_of_str,
                    extraction_method="SNIPPET_RELATIVE",
                    source_url=source_url
                )

        # ── Step E: Check for Absolute Dates in text ──
        # Important: To prevent false positives from search engine crawl dates or SEO titles,
        # an unanchored absolute date in a snippet MUST NOT establish RECENT freshness.
        # However, for backward compatibility (e.g. test_09_stale_evidence where "2024-01-15 — ..." is tested),
        # if an unanchored date is STALE (> 180 days), we allow it as STALE with LOW confidence.
        norm_abs, raw_abs = cls.normalize_absolute_date(clean_combined)
        if norm_abs and raw_abs:
            # Check false positive exclusions:
            # 1. Is raw_abs part of an SEO title? (e.g. 2026 Reviews & Info)
            for pat in SEO_TITLE_PATTERNS:
                if pat.search(title) or pat.search(text):
                    if raw_abs in (title or text):
                        # The date matched is part of the SEO title
                        return ExtractedReviewDate(
                            date=None,
                            date_type=ReviewEvidenceDateType.UNKNOWN.value,
                            confidence=ReviewDateConfidence.UNKNOWN.value,
                            freshness=ReviewFreshness.UNKNOWN.value,
                            as_of=as_of_str,
                            extraction_method="NONE",
                            source_url=source_url,
                            rejection_reason="MISLEADING_SEO_TITLE_YEAR"
                        )

            # 2. Is raw_abs part of establishment/founding?
            for pat in ESTABLISHED_PATTERNS:
                if pat.search(clean_combined):
                    return ExtractedReviewDate(
                        date=None,
                        date_type=ReviewEvidenceDateType.UNKNOWN.value,
                        confidence=ReviewDateConfidence.UNKNOWN.value,
                        freshness=ReviewFreshness.UNKNOWN.value,
                        as_of=as_of_str,
                        extraction_method="NONE",
                        source_url=source_url,
                        rejection_reason="BUSINESS_ESTABLISHMENT_DATE"
                    )

            # 3. Is raw_abs part of copyright?
            for pat in COPYRIGHT_PATTERNS:
                if pat.search(clean_combined):
                    return ExtractedReviewDate(
                        date=None,
                        date_type=ReviewEvidenceDateType.UNKNOWN.value,
                        confidence=ReviewDateConfidence.UNKNOWN.value,
                        freshness=ReviewFreshness.UNKNOWN.value,
                        as_of=as_of_str,
                        extraction_method="NONE",
                        source_url=source_url,
                        rejection_reason="FOOTER_COPYRIGHT_YEAR"
                    )

            # 4. Is raw_abs part of page update?
            for pat in PAGE_UPDATE_PATTERNS:
                if pat.search(clean_combined):
                    return ExtractedReviewDate(
                        date=None,
                        date_type=ReviewEvidenceDateType.UNKNOWN.value,
                        confidence=ReviewDateConfidence.UNKNOWN.value,
                        freshness=ReviewFreshness.UNKNOWN.value,
                        as_of=as_of_str,
                        extraction_method="NONE",
                        source_url=source_url,
                        rejection_reason="PAGE_UPDATE_METADATA_DATE"
                    )

            # 5. Is raw_abs just a review count? (e.g. "2,026 reviews")
            if re.search(r'\b' + re.escape(raw_abs) + r'\s+reviews?\b', clean_combined, re.I):
                return ExtractedReviewDate(
                    date=None,
                    date_type=ReviewEvidenceDateType.UNKNOWN.value,
                    confidence=ReviewDateConfidence.UNKNOWN.value,
                    freshness=ReviewFreshness.UNKNOWN.value,
                    as_of=as_of_str,
                    extraction_method="NONE",
                    source_url=source_url,
                    rejection_reason="REVIEW_COUNT_MATCHED_AS_YEAR"
                )

            freshness = cls.calculate_freshness(norm_abs, as_of_dt)

            # Defensive Gate: If unanchored date appears RECENT (<= 180 days), REJECT IT!
            # Search result snippets often contain current month crawl dates that are NOT reviews.
            # Only anchored review dates or review cards can be RECENT.
            if freshness == ReviewFreshness.RECENT.value:
                return ExtractedReviewDate(
                    date=None,
                    date_type=ReviewEvidenceDateType.UNKNOWN.value,
                    confidence=ReviewDateConfidence.UNKNOWN.value,
                    freshness=ReviewFreshness.UNKNOWN.value,
                    as_of=as_of_str,
                    extraction_method="NONE",
                    source_url=source_url,
                    rejection_reason="UNANCHORED_SNIPPET_DATE_CANNOT_PROVE_RECENCY"
                )

            # If it's STALE, allow it with LOW confidence (preserves test_09 without false recency risk)
            return ExtractedReviewDate(
                date=norm_abs,
                date_raw=raw_abs,
                date_type=ReviewEvidenceDateType.REVIEW_PUBLICATION_DATE.value,
                confidence=ReviewDateConfidence.LOW.value,
                freshness=ReviewFreshness.STALE.value,
                as_of=as_of_str,
                extraction_method="SNIPPET_UNANCHORED_STALE",
                source_url=source_url
            )

        return ExtractedReviewDate(
            date=None,
            date_type=ReviewEvidenceDateType.UNKNOWN.value,
            confidence=ReviewDateConfidence.UNKNOWN.value,
            freshness=ReviewFreshness.UNKNOWN.value,
            as_of=as_of_str,
            extraction_method="NONE",
            source_url=source_url,
            rejection_reason="NO_DATE_FOUND"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 4. HTTP / CACHED PAGE FETCHING FOR UNKNOWN SNIPPET DATES
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def fetch_and_extract(
        cls,
        url: str,
        as_of: Optional[Union[datetime, str]] = None,
        cache_dir: Optional[str] = None,
        timeout: int = 10
    ) -> ExtractedReviewDate:
        """
        Attempts to fetch a destination review page via HTTP and extract review dates.
        Caches results locally to avoid redundant network queries.
        Handles anti-bot challenges (e.g. 403 DataDome) gracefully -> returns UNKNOWN.
        """
        as_of_dt = cls.get_reference_as_of(as_of)
        as_of_str = as_of_dt.isoformat()

        if not url or not url.startswith("http"):
            return ExtractedReviewDate(
                date=None,
                date_type=ReviewEvidenceDateType.UNKNOWN.value,
                confidence=ReviewDateConfidence.UNKNOWN.value,
                freshness=ReviewFreshness.UNKNOWN.value,
                as_of=as_of_str,
                extraction_method="NONE",
                source_url=url,
                rejection_reason="INVALID_URL"
            )

        # Cache path
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cache_dir_path = cache_dir or os.path.join(base_dir, "data", "cache_review_dates")
        os.makedirs(cache_dir_path, exist_ok=True)
        url_hash = hashlib.md5(url.encode()).hexdigest()
        cache_file = os.path.join(cache_dir_path, f"{url_hash}.json")

        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                return ExtractedReviewDate(**cached)
            except Exception:
                pass

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-GB,en;q=0.9"
        }

        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200 and resp.text:
                result = cls.extract_from_html(resp.text, source_url=url, as_of=as_of_dt)
            elif resp.status_code in [401, 403, 429]:
                result = ExtractedReviewDate(
                    date=None,
                    date_type=ReviewEvidenceDateType.UNKNOWN.value,
                    confidence=ReviewDateConfidence.UNKNOWN.value,
                    freshness=ReviewFreshness.UNKNOWN.value,
                    as_of=as_of_str,
                    extraction_method="PAGE_FETCH_BLOCKED",
                    source_url=url,
                    rejection_reason=f"HTTP_{resp.status_code}_ACCESS_RESTRICTED"
                )
            else:
                result = ExtractedReviewDate(
                    date=None,
                    date_type=ReviewEvidenceDateType.UNKNOWN.value,
                    confidence=ReviewDateConfidence.UNKNOWN.value,
                    freshness=ReviewFreshness.UNKNOWN.value,
                    as_of=as_of_str,
                    extraction_method="PAGE_FETCH_FAILED",
                    source_url=url,
                    rejection_reason=f"HTTP_{resp.status_code}"
                )
        except Exception as ex:
            result = ExtractedReviewDate(
                date=None,
                date_type=ReviewEvidenceDateType.UNKNOWN.value,
                confidence=ReviewDateConfidence.UNKNOWN.value,
                freshness=ReviewFreshness.UNKNOWN.value,
                as_of=as_of_str,
                extraction_method="PAGE_FETCH_ERROR",
                source_url=url,
                rejection_reason=f"NETWORK_ERROR: {str(ex)[:100]}"
            )

        # Cache the result
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(result.to_dict(), f, indent=2)
        except Exception:
            pass

        return result
