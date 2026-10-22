import os
import re
import json
from typing import List, Dict, Any, Optional, Tuple, Sequence, Union
from datetime import datetime, timezone
import gspread
from dotenv import load_dotenv

from lib.types import Lead, ResearchLogEntry, QualificationState, OutreachStatus
from sheets_sync import get_sheet_client
from lib.crm.identity_matcher import BusinessIdentityMatcher, IdentityMatchOutcome
from lib.crm.reconciliation import crm_write_lock

load_dotenv()

def normalize_outreach_status(status: Any) -> str:
    """
    Deterministically normalizes outreach_status.
    If empty, None, or whitespace -> returns 'NOT_READY'.
    Preserves all valid states (SENT, BOUNCED, FAILED, READY_FOR_REVIEW, etc.).
    NEVER modifies qualification_state or infers qualification from outreach.
    """
    if status is None:
        return OutreachStatus.NOT_READY.value
    s = str(status).strip()
    return s if s else OutreachStatus.NOT_READY.value

def col_to_letter(col_num: int) -> str:
    string = ""
    while col_num > 0:
        col_num, remainder = divmod(col_num - 1, 26)
        string = chr(65 + remainder) + string
    return string

def validate_row_width(row_values: Sequence[Any], expected_columns: Sequence[str], sheet_name: str) -> None:
    """
    Validates that a row to be written matches the expected schema column count exactly.
    Prevents truncated writes, misaligned columns, or shifted data corruption.
    """
    if len(row_values) != len(expected_columns):
        raise ValueError(
            f"Row width mismatch for {sheet_name}: expected {len(expected_columns)} columns, got {len(row_values)} values."
        )

# Strict Qualification Engine V3 Column Definitions
LEADS_COLUMNS = [
    "lead_id",
    "company_name",
    "industry",
    "target_country",
    "country",
    "city",
    "region",
    "postcode",
    "address",
    "phone",
    "website",
    "website_status",
    "verification_status",
    "verification_reason",
    "instagram_url",
    "facebook_url",
    "tiktok_url",
    "review_count",
    "rating",
    "social_status",
    "social_ownership_status",
    "social_profile_status",
    "social_activity",
    "operational_status",
    "operational_confidence",
    "operational_evidence",
    "evidence_freshness",
    "multiple_locations",
    "business_activity_signal",
    "qualification_signals",
    "lead_score",
    "priority",
    "qualification_state",
    "qualification_reason",
    "red_flags",
    "outreach_angle",
    "lead_status",
    "date_added",
    # Post-Pipeline Outreach Architecture Fields (Section 19)
    "campaign_id",
    "pipeline_run_id",
    "verified_social_platforms",
    "verified_social_urls",
    "outreach_mode",
    "outreach_status",
    "outreach_channel",
    "outreach_message",
    "outreach_generated_at",
    "outreach_sent_at",
    "outreach_attempt_count",
    "outreach_message_id",
    "outreach_block_reason",
    "selected_channel",
    "channel_selection_reason",
    "channel_availability",
    "call_status",
    "call_notes",
    "call_outcome",
    "next_follow_up",
    "response_status",
    "manual_outreach_notes",
    # Campaign Execution Fields (Section 13 / 20)
    "original_generated_message",
    "final_message",
    "edited_by_user",
    "edited_at",
    "failure_reason",
    "provider_response",
    "outreach_sent_message",
    "follow_up_enabled",
    "follow_up_at",
    "last_contact_at",
    # Contact Enrichment & Recipient Verification Fields (Section 19 / 20)
    "companies_house_number",
    "registered_name",
    "company_type",
    "company_status",
    "registered_office",
    "sic_codes",
    "companies_house_url",
    "entity_match_status",
    "entity_match_confidence",
    "entity_match_reason",
    "subscriber_type",
    "email",
    "email_type",
    "email_source",
    "email_source_url",
    "email_verification_status",
    "email_confidence",
    "email_last_verified_at",
    "marketing_email_status",
    "lawful_basis_status",
    "opt_out_status",
    "compliance_review_status",
    "compliance_notes",
    "recipient_verified",
    "recipient_confidence",
    "contactability_status",
    "contactability_reason",
    "contact_history_count",
    "global_cooldown_until",
    # UK Entity Refinements (Section 7, 19)
    "trading_name",
    "legal_entity_name",
    "entity_source",
    # Business Email Discovery Refinements (Section 21)
    "primary_email",
    "email_candidates",
    "email_source_type",
    "email_source_context",
    "email_discovered_at",
    "email_search_queries",
    "email_searches_performed",
    "email_search_status",
    "email_search_reason",
    "website_recheck_required",
    # ── Outcome Tracking Fields (Section 22 / Post-Send) ──────────────────
    "response_type",
    "follow_up_number",
    "follow_up_notes",
    "sales_stage",
    "meeting_status",
    "proposal_status",
    "client_status",
    "revenue",
    # ── Real Delivery & Bounce Tracking Fields ─────────────────────────
    "bounce_code",
    "bounce_reason",
    "bounced_at",
    "bounce_provider",
    "email_suppressed",
    "email_suppression_reason",
    # ── Creator / Influencer Evidence Fields ───────────────────────────
    "creator_evidence_status",
    "creator_evidence_count",
    "creator_evidence_confidence",
    "creator_latest_date",
    "creator_evidence_summary",
    "creator_evidence_urls",
    "creator_discovered_at",
]

REVIEW_QUEUE_COLUMNS = LEADS_COLUMNS
CRM_COLUMNS = LEADS_COLUMNS

RESEARCH_LOG_COLUMNS = [
    "research_id",
    "company_name",
    "industry",
    "target_country",
    "detected_country",
    "country_status",
    "city",
    "region",
    "postcode",
    "address",
    "phone",
    "website",
    "website_status",
    "verification_status",
    "review_count",
    "rating",
    "social_status",
    "social_ownership_status",
    "social_profile_status",
    "operational_status",
    "operational_confidence",
    "evidence_freshness",
    "qualification_state",
    "qualification_status",
    "disqualification_reason",
    "red_flags",
    "source_url",
    "date_researched"
]


def _safe_str(val: Any) -> str:
    """Safely extracts clean string representation without crashing on None, numbers, or MagicMock objects."""
    if val is None:
        return ""
    type_name = type(val).__name__
    if "Mock" in type_name:
        return ""
    if isinstance(val, str):
        return val.strip()
    return str(val).strip()


def _clean_source_url(url: Any) -> str:
    """Normalizes source URLs (e.g. OpenStreetMap node/way/relation links) for reliable deduplication."""
    s = _safe_str(url).lower()
    if not s:
        return ""
    s = re.sub(r'/+$', '', s)
    if s in (
        "https://www.openstreetmap.org", "http://www.openstreetmap.org",
        "https://maps.google.com", "http://maps.google.com",
        "https://google.com", "https://www.google.com"
    ):
        return ""
    return s


def _clean_phone_digits(phone: Any) -> str:
    """Extracts numeric digits from phone numbers and normalizes standard country prefixes."""
    s = _safe_str(phone)
    if not s:
        return ""
    digits = re.sub(r'[^0-9]', '', s)
    if digits.startswith("0") and len(digits) == 11:
        digits = "44" + digits[1:]
    return digits


class ResearchLogMap(dict):
    """
    Structured in-memory mapping of RESEARCH_LOG records.
    Subclasses standard dict (mapping `norm_id -> (row_idx, row_dict)`) for 100%
    backwards-compatibility with all legacy callers and tests.
    Provides verified multi-signal indexing and query methods:
      - source_url indexing (e.g. OSM URL)
      - website clean domain indexing
      - phone digits + name indexing
      - address key tokens + name indexing
      - malformed row tracking with explicit fail-closed detection
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.url_map: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self.phone_map: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self.address_map: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self.malformed_rows: Dict[int, Dict[str, Any]] = {}
        self.normal_rows: Dict[int, Dict[str, Any]] = {}

    def add_entry(
        self,
        row_idx: int,
        row_dict: Dict[str, Any],
        norm_id: str = "",
        source_url: str = "",
        phone: str = "",
        address: str = "",
        company_name: str = "",
        is_malformed: bool = False
    ) -> None:
        entry_meta = dict(row_dict)
        entry_meta["_row_idx"] = row_idx
        entry_meta["_is_malformed"] = is_malformed
        entry_meta["_source_url"] = source_url
        entry_meta["_company_name"] = company_name

        if is_malformed:
            self.malformed_rows[row_idx] = entry_meta
        else:
            self.normal_rows[row_idx] = entry_meta

        # Primary dict mapping: norm_id -> (row_idx, row_dict)
        if norm_id:
            # If norm_id is already mapped by a normal row, do not let a shifted duplicate overwrite it
            if norm_id not in self:
                self[norm_id] = (row_idx, entry_meta)
            elif is_malformed and not self[norm_id][1].get("is_malformed", False):
                pass
            else:
                self[norm_id] = (row_idx, entry_meta)

        # Secondary index: clean source_url
        clean_url = _clean_source_url(source_url)
        if clean_url and clean_url not in self.url_map:
            self.url_map[clean_url] = (row_idx, entry_meta)

        # Secondary index: phone + clean name
        clean_p = _clean_phone_digits(phone)
        clean_n = re.sub(r'[^a-z0-9]', '', (company_name or "").lower())
        if clean_p and len(clean_p) >= 7 and clean_n:
            phone_key = f"{clean_p}@{clean_n}"
            if phone_key not in self.phone_map:
                self.phone_map[phone_key] = (row_idx, entry_meta)

        # Secondary index: address tokens + clean name
        clean_a = re.sub(r'[^a-z0-9]', ' ', (address or "").lower()).split()
        key_tokens = [w for w in clean_a if len(w) >= 3 and w not in ("street", "road", "united", "kingdom", "lane", "avenue")]
        if key_tokens and clean_n:
            addr_key = f"{key_tokens[0]}@{key_tokens[-1]}@{clean_n}"
            if addr_key not in self.address_map:
                self.address_map[addr_key] = (row_idx, entry_meta)

    def find_match(
        self,
        company_name: str,
        city: str = "",
        target_country: str = "",
        website: str = "",
        source_url: str = "",
        phone: str = "",
        address: str = ""
    ) -> Optional[Tuple[int, Dict[str, Any], str]]:
        """
        Evaluates verified multi-signal identity against existing records.
        Returns: (row_idx, row_dict, match_signal_description) or None.
        CRITICAL INVARIANT: NEVER matches on company name alone.
        """
        c_name_str = _safe_str(company_name)
        city_str = _safe_str(city)
        country_str = _safe_str(target_country)
        web_str = _safe_str(website)
        src_str = _safe_str(source_url)
        phone_str = _safe_str(phone)
        addr_str = _safe_str(address)

        # 1. Non-empty, verified source URL match (e.g. OpenStreetMap node)
        clean_url = _clean_source_url(src_str)
        if clean_url and clean_url in self.url_map:
            r_idx, r_dict = self.url_map[clean_url]
            return r_idx, r_dict, f"source_url match ('{clean_url}')"

        # 2. Verified website domain match (non-platform domain)
        if web_str:
            clean_dom = re.sub(r'^https?://(www\.)?', '', web_str.lower()).split('/')[0].strip()
            if clean_dom and not any(p in clean_dom for p in [
                "instagram.com", "facebook.com", "tiktok.com", "google.com",
                "deliveroo", "ubereats", "just-eat", "tripadvisor", "yelp"
            ]):
                dom_key = f"dom:{clean_dom}"
                if dom_key in self:
                    r_idx, r_dict = self[dom_key]
                    return r_idx, r_dict, f"domain match ('{clean_dom}')"

        # 3. Verified phone + normalized company name match
        clean_p = _clean_phone_digits(phone_str)
        clean_n = re.sub(r'[^a-z0-9]', '', c_name_str.lower())
        if clean_p and len(clean_p) >= 7 and clean_n:
            phone_key = f"{clean_p}@{clean_n}"
            if phone_key in self.phone_map:
                r_idx, r_dict = self.phone_map[phone_key]
                return r_idx, r_dict, f"phone + name match ('{clean_p}')"

        # 4. Verified address key tokens + normalized company name match
        clean_a = re.sub(r'[^a-z0-9]', ' ', addr_str.lower()).split()
        key_tokens = [w for w in clean_a if len(w) >= 3 and w not in ("street", "road", "united", "kingdom", "lane", "avenue")]
        if key_tokens and clean_n:
            addr_key = f"{key_tokens[0]}@{key_tokens[-1]}@{clean_n}"
            if addr_key in self.address_map:
                r_idx, r_dict = self.address_map[addr_key]
                return r_idx, r_dict, f"address + name match ('{addr_str}')"

        # 5. Verified normalized identity match: (clean_name + clean_city + clean_country)
        clean_city = re.sub(r'[^a-z0-9]', '', city_str.lower())
        clean_country = re.sub(r'[^a-z0-9]', '', country_str.lower())
        if clean_n and clean_city and clean_country:
            biz_key = f"biz:{clean_n}@{clean_city}@{clean_country}"
            if biz_key in self:
                r_idx, r_dict = self[biz_key]
                return r_idx, r_dict, f"normalized identity match ('{biz_key}')"

        return None


class GoogleSheetsStorageProvider:
    """
    Storage provider interfacing with Google Sheets as the single source of truth.
    Manages three separate tabs (Section 19):
      - LEADS: Contains ONLY OUTREACH_READY confirmed qualified prospects.
      - REVIEW_QUEUE: Contains MANUAL_REVIEW prospects awaiting human verification.
      - RESEARCH_LOG: Contains all discovered & researched businesses including rejected/disqualified.
    Implements normalization, deduplication, row updating, and text-safe phone formatting.
    """
    def __init__(self, sheet_url: Optional[str] = None):
        sheet_id = os.getenv("GOOGLE_SHEET_ID") or os.getenv("SPREADSHEET_ID") or "1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc"
        default_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
        self.sheet_url = sheet_url or os.getenv("GOOGLE_SHEET_URL") or default_url
        self._gc = None
        self._spreadsheet = None
        self._leads_ws = None
        self._review_ws = None
        self._research_ws = None
        self.matcher = BusinessIdentityMatcher()
        self.stats = {
            "new_businesses": 0,
            "existing_businesses": 0,
            "possible_duplicates": 0,
            "conflicts": 0,
            "duplicates_prevented": 0
        }

    @property
    def gc(self):
        if not self._gc:
            try:
                self._gc = get_sheet_client()
            except Exception as e:
                try:
                    from lib.monitoring.detectors import GoogleSheetsMonitor
                    GoogleSheetsMonitor().record_auth_failure(e, context="connect")
                except Exception:
                    pass
                raise
        return self._gc

    @property
    def spreadsheet(self):
        if not self._spreadsheet:
            sheet_id = os.getenv("GOOGLE_SHEET_ID") or os.getenv("SPREADSHEET_ID") or "1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc"
            if not self.sheet_url:
                self.sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
            try:
                if sheet_id:
                    try:
                        self._spreadsheet = self.gc.open_by_key(sheet_id)
                    except Exception:
                        self._spreadsheet = self.gc.open_by_url(self.sheet_url)
                else:
                    self._spreadsheet = self.gc.open_by_url(self.sheet_url)
                try:
                    from lib.monitoring.detectors import GoogleSheetsMonitor
                    GoogleSheetsMonitor().record_success()
                except Exception:
                    pass
            except Exception as e:
                try:
                    from lib.monitoring.detectors import GoogleSheetsMonitor
                    GoogleSheetsMonitor().record_api_failure(e, sheet_id=self.sheet_url)
                except Exception:
                    pass
                raise
        return self._spreadsheet

    def _get_or_create_worksheet(self, title: str, cols: int = 40) -> gspread.Worksheet:
        try:
            return self.spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            if title == "LEADS":
                try:
                    s1 = self.spreadsheet.worksheet("Sheet1")
                    s1.update_title("LEADS")
                    return s1
                except Exception:
                    pass
            print(f"[GoogleSheets] Creating new worksheet tab: '{title}'...")
            return self.spreadsheet.add_worksheet(title=title, rows=1000, cols=cols)

    @property
    def leads_worksheet(self) -> gspread.Worksheet:
        if not self._leads_ws:
            self._leads_ws = self._get_or_create_worksheet("LEADS", cols=len(LEADS_COLUMNS) + 2)
            self.ensure_leads_columns()
        return self._leads_ws

    @property
    def review_queue_worksheet(self) -> gspread.Worksheet:
        if not self._review_ws:
            self._review_ws = self._get_or_create_worksheet("REVIEW_QUEUE", cols=len(REVIEW_QUEUE_COLUMNS) + 2)
            self.ensure_review_queue_columns()
        return self._review_ws

    @property
    def research_worksheet(self) -> gspread.Worksheet:
        if not self._research_ws:
            self._research_ws = self._get_or_create_worksheet("RESEARCH_LOG", cols=len(RESEARCH_LOG_COLUMNS) + 2)
            self.ensure_research_columns()
        return self._research_ws

    # Backward compatibility alias
    @property
    def worksheet(self) -> gspread.Worksheet:
        return self.leads_worksheet

    def ensure_leads_columns(self):
        """Ensures the required columns are set up in Row 1 of LEADS."""
        needed_cols = len(LEADS_COLUMNS)
        if self.leads_worksheet.col_count < needed_cols:
            try:
                self.leads_worksheet.add_cols(needed_cols - self.leads_worksheet.col_count + 5)
            except Exception as e:
                print(f"[GoogleSheets] Add cols note: {e}")

        existing_headers = self.leads_worksheet.row_values(1)
        if not existing_headers or existing_headers != LEADS_COLUMNS:
            print("[GoogleSheets] Initializing LEADS headers...")
            end_col = col_to_letter(len(LEADS_COLUMNS))
            self.leads_worksheet.update(values=[LEADS_COLUMNS], range_name=f"A1:{end_col}1")
            try:
                self.leads_worksheet.format(f"A1:{end_col}1", {
                    "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 0.84, "blue": 0.0}},
                    "backgroundColor": {"red": 0.08, "green": 0.08, "blue": 0.09}
                })
                self.leads_worksheet.freeze(rows=1, cols=2)
            except Exception as e:
                print(f"[GoogleSheets] LEADS header styling note: {e}")

    def ensure_review_queue_columns(self):
        """Ensures the required columns are set up in Row 1 of REVIEW_QUEUE."""
        needed_cols = len(REVIEW_QUEUE_COLUMNS)
        if self.review_queue_worksheet.col_count < needed_cols:
            try:
                self.review_queue_worksheet.add_cols(needed_cols - self.review_queue_worksheet.col_count + 5)
            except Exception as e:
                print(f"[GoogleSheets] Add cols note: {e}")

        existing_headers = self.review_queue_worksheet.row_values(1)
        if not existing_headers or existing_headers != REVIEW_QUEUE_COLUMNS:
            print("[GoogleSheets] Initializing REVIEW_QUEUE headers...")
            end_col = col_to_letter(len(REVIEW_QUEUE_COLUMNS))
            self.review_queue_worksheet.update(values=[REVIEW_QUEUE_COLUMNS], range_name=f"A1:{end_col}1")
            try:
                self.review_queue_worksheet.format(f"A1:{end_col}1", {
                    "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 0.84, "blue": 0.0}},
                    "backgroundColor": {"red": 0.08, "green": 0.08, "blue": 0.09}
                })
                self.review_queue_worksheet.freeze(rows=1, cols=2)
            except Exception as e:
                print(f"[GoogleSheets] REVIEW_QUEUE header styling note: {e}")

    def ensure_research_columns(self):
        """Ensures the 24 required columns are set up in Row 1 of RESEARCH_LOG."""
        existing_headers = self.research_worksheet.row_values(1)
        if not existing_headers or existing_headers != RESEARCH_LOG_COLUMNS:
            print("[GoogleSheets] Initializing RESEARCH_LOG headers...")
            end_col = col_to_letter(len(RESEARCH_LOG_COLUMNS))
            self.research_worksheet.update(values=[RESEARCH_LOG_COLUMNS], range_name=f"A1:{end_col}1")
            try:
                self.research_worksheet.format(f"A1:{end_col}1", {
                    "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 0.84, "blue": 0.0}},
                    "backgroundColor": {"red": 0.08, "green": 0.08, "blue": 0.09}
                })
                self.research_worksheet.freeze(rows=1, cols=2)
            except Exception as e:
                print(f"[GoogleSheets] RESEARCH_LOG header styling note: {e}")

    def normalize_identity(
        self,
        company_name: str,
        city: str,
        target_country: str = "",
        website: str = ""
    ) -> str:
        """
        Creates a normalized key for duplicate detection.
        Respects target country so Manchester UK and Manchester US are distinct.
        """
        if website and not any(p in website.lower() for p in [
            "instagram.com", "facebook.com", "tiktok.com", "google.com",
            "deliveroo", "ubereats", "just-eat", "tripadvisor", "yelp"
        ]):
            clean_dom = re.sub(r'^https?://(www\.)?', '', website.lower()).split('/')[0].strip()
            if clean_dom:
                return f"dom:{clean_dom}"
        
        clean_name = re.sub(r'[^a-z0-9]', '', (company_name or "").lower())
        clean_city = re.sub(r'[^a-z0-9]', '', (city or "").lower())
        clean_country = re.sub(r'[^a-z0-9]', '', (target_country or "").lower())
        return f"biz:{clean_name}@{clean_city}@{clean_country}"

    def get_existing_leads_map(self) -> Dict[str, Tuple[int, Dict[str, Any]]]:
        """
        Loads all existing rows from LEADS tab and indexes them by normalized identity.
        Returns: { normalized_id: (row_index_1_based, row_dict) }
        """
        try:
            # Read-only helper: do not use the schema-repairing worksheet property.
            rows = self.spreadsheet.worksheet("LEADS").get_all_records()
        except Exception:
            return {}

        leads_map = {}
        for idx, row in enumerate(rows, start=2):
            c_name = str(row.get("company_name", ""))
            city = str(row.get("city", ""))
            target_country = str(row.get("target_country", "") or row.get("country", ""))
            web = str(row.get("website", ""))
            norm_id = self.normalize_identity(c_name, city, target_country, web)
            if norm_id:
                leads_map[norm_id] = (idx, row)
        return leads_map

    def get_existing_research_map(self, *, fail_closed: bool = False) -> ResearchLogMap:
        """
        Loads existing RESEARCH_LOG rows and indexes them by identity/source evidence.
        Read/display callers may tolerate a failed read; write callers must fail closed.
        Correctly parses normal rows (A:AB) and shifted malformed rows (AA:BB).
        """
        try:
            ws = getattr(self, "_research_ws", None) or self.spreadsheet.worksheet("RESEARCH_LOG")
            raw_rows = ws.get_all_values()
        except Exception as exc:
            if fail_closed:
                raise RuntimeError(
                    "[GoogleSheets Deduplication Block] Could not read RESEARCH_LOG before writing; "
                    "refusing to append or update research records without a complete deduplication snapshot."
                ) from exc
            return ResearchLogMap()

        if not raw_rows or len(raw_rows) < 2:
            return ResearchLogMap()

        research_map = ResearchLogMap()

        for r_idx, r in enumerate(raw_rows[1:], start=2):
            if not any(str(c).strip() for c in r):
                continue

            has_a_or_b = (len(r) > 0 and bool(str(r[0]).strip())) or (len(r) > 1 and bool(str(r[1]).strip()))
            has_aa_or_ab = (len(r) > 26 and bool(str(r[26]).strip())) or (len(r) > 27 and bool(str(r[27]).strip()))

            if has_a_or_b:
                res_id = str(r[0]).strip() if len(r) > 0 else ""
                c_name = str(r[1]).strip() if len(r) > 1 else ""
                target_country = str(r[3]).strip() if len(r) > 3 else ""
                city = str(r[6]).strip() if len(r) > 6 else ""
                addr = str(r[9]).strip() if len(r) > 9 else ""
                phone = str(r[10]).strip() if len(r) > 10 else ""
                web = str(r[11]).strip() if len(r) > 11 else ""
                src_url = str(r[26]).strip() if len(r) > 26 else ""
                is_malformed = False
            elif has_aa_or_ab:
                res_id = str(r[26]).strip() if len(r) > 26 else ""
                c_name = str(r[27]).strip() if len(r) > 27 else ""
                target_country = str(r[29]).strip() if len(r) > 29 else ""
                city = str(r[32]).strip() if len(r) > 32 else ""
                addr = str(r[35]).strip() if len(r) > 35 else ""
                phone = str(r[36]).strip() if len(r) > 36 else ""
                web = str(r[37]).strip() if len(r) > 37 else ""
                src_url = str(r[52]).strip() if len(r) > 52 else ""
                is_malformed = True
            else:
                continue

            norm_id = self.normalize_identity(c_name, city, target_country, web)
            row_dict = {
                "research_id": res_id,
                "company_name": c_name,
                "target_country": target_country,
                "city": city,
                "address": addr,
                "phone": phone,
                "website": web,
                "source_url": src_url,
                "is_malformed": is_malformed,
                "row_index": r_idx
            }

            research_map.add_entry(
                row_idx=r_idx,
                row_dict=row_dict,
                norm_id=norm_id,
                source_url=src_url,
                phone=phone,
                address=addr,
                company_name=c_name,
                is_malformed=is_malformed
            )

        return research_map

    def diagnose_research_log(self) -> Dict[str, Any]:
        """
        Audits RESEARCH_LOG for row width, schema alignment, and historical column shifts.
        Read-only diagnostic method: never mutates Google Sheets.
        """
        try:
            raw_rows = self.research_worksheet.get_all_values()
        except Exception as e:
            return {"error": f"Failed to access RESEARCH_LOG: {e}"}

        if not raw_rows:
            return {"total_rows": 0, "status": "EMPTY", "can_safely_deduplicate": True}

        header = raw_rows[0]
        normal_rows = []
        shifted_rows = []
        empty_rows = []

        for r_idx, r in enumerate(raw_rows[1:], start=2):
            has_a = bool(r[0].strip()) if len(r) > 0 else False
            has_b = bool(r[1].strip()) if len(r) > 1 else False
            has_aa = bool(r[26].strip()) if len(r) > 26 else False
            has_ab = bool(r[27].strip()) if len(r) > 27 else False

            if has_a or has_b:
                normal_rows.append(r_idx)
            elif has_aa or has_ab:
                shifted_rows.append({
                    "row_index": r_idx,
                    "research_id": r[26] if len(r) > 26 else "",
                    "company_name": r[27] if len(r) > 27 else "",
                    "target_country": r[29] if len(r) > 29 else "",
                    "city": r[32] if len(r) > 32 else "",
                    "address": r[35] if len(r) > 35 else "",
                    "phone": r[36] if len(r) > 36 else "",
                    "website": r[37] if len(r) > 37 else "",
                    "source_url": r[52] if len(r) > 52 else "",
                    "date_researched": r[53] if len(r) > 53 else ""
                })
            else:
                empty_rows.append(r_idx)

        dates_shifted = {}
        for s in shifted_rows:
            d = s["date_researched"]
            dates_shifted[d] = dates_shifted.get(d, 0) + 1

        return {
            "total_rows": len(raw_rows),
            "header_column_count": len(header),
            "expected_column_count": len(RESEARCH_LOG_COLUMNS),
            "normal_rows_count": len(normal_rows),
            "shifted_rows_count": len(shifted_rows),
            "empty_rows_count": len(empty_rows),
            "can_safely_deduplicate": len(shifted_rows) == 0,
            "shifted_dates": dates_shifted,
            "sample_shifted_rows": shifted_rows[:5],
            "shifted_rows": shifted_rows
        }

    validate_row_width = staticmethod(validate_row_width)

    def safe_clear_worksheet(
        self,
        worksheet: Union[str, Any],
        confirm_token: str = "",
        backup_dir: str = "data/backups",
    ) -> Dict[str, Any]:
        """
        Safely clears a worksheet ONLY when explicitly confirmed with confirm_token='CONFIRM_DESTRUCTIVE_CLEAR'.
        Automatically takes a local timestamped backup of the worksheet before clearing.
        Supports both worksheet title strings and worksheet objects.
        """
        ws_title = worksheet if isinstance(worksheet, str) else getattr(worksheet, "title", str(worksheet))
        if confirm_token != "CONFIRM_DESTRUCTIVE_CLEAR":
            raise PermissionError(
                f"Destructive clear on worksheet '{ws_title}' blocked: "
                "requires confirm_token='CONFIRM_DESTRUCTIVE_CLEAR'."
            )
        ws = self.spreadsheet.worksheet(worksheet) if isinstance(worksheet, str) else worksheet
        vals = ws.get_all_values()

        # Save pre-clear backup
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        os.makedirs(backup_dir, exist_ok=True)
        backup_file = os.path.join(backup_dir, f"pre_clear_{ws_title}_{ts}.json")
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump({
                "worksheet": ws_title,
                "timestamp": ts,
                "row_count": len(vals),
                "values": vals,
            }, f, indent=2)

        ws.clear()
        return {"status": "CLEARED", "worksheet": ws_title, "rows_cleared": len(vals), "backup_path": backup_file}

    def get_existing_review_queue_map(self, *, fail_closed: bool = False) -> Dict[str, Tuple[int, Dict[str, Any]]]:
        """
        Loads all existing rows from REVIEW_QUEUE tab and indexes them by normalized identity.
        Read/display callers may tolerate a failed read; write callers must fail closed.
        """
        try:
            # Reuse an already-injected/initialized worksheet, but never create or repair a tab here.
            ws = getattr(self, "_review_ws", None) or self.spreadsheet.worksheet("REVIEW_QUEUE")
            rows = ws.get_all_records()
        except Exception as exc:
            if fail_closed:
                raise RuntimeError(
                    "[GoogleSheets Deduplication Block] Could not read REVIEW_QUEUE before writing; "
                    "refusing to append a review candidate without a complete deduplication snapshot."
                ) from exc
            return {}

        review_map = {}
        for idx, row in enumerate(rows, start=2):
            c_name = str(row.get("company_name", ""))
            city = str(row.get("city", ""))
            target_country = str(row.get("target_country", "") or row.get("country", ""))
            web = str(row.get("website", ""))
            norm_id = self.normalize_identity(c_name, city, target_country, web)
            if norm_id:
                review_map[norm_id] = (idx, row)
        return review_map

    def save_qualified_leads(self, leads: List[Lead]) -> Tuple[int, int]:
        """
        Saves OUTREACH_READY leads strictly to the LEADS tab.
        Applies Global CRM Deduplication via BusinessIdentityMatcher:
          - EXISTING_BUSINESS: updates existing row in place, preserving original lead_id,
            outreach history, and qualification history.
          - POSSIBLE_DUPLICATE: routes to REVIEW_QUEUE, preventing duplicate rows in LEADS.
          - CONFLICT: flags conflict, never merges automatically.
          - NEW_BUSINESS: appends new row.
        Returns: (inserted_count, updated_count)
        """
        if not leads:
            return 0, 0

        # CHANGE 1: DEFENSIVE QUALIFICATION GUARD
        # LEADS must strictly contain ONLY confirmed OUTREACH_READY leads.
        for lead in leads:
            q_state = getattr(lead, "qualification_state", None)
            if q_state is None and isinstance(lead, dict):
                q_state = lead.get("qualification_state")
            q_state_str = str(q_state or "").strip().upper()
            if q_state_str != QualificationState.OUTREACH_READY.value:
                lead_id = getattr(lead, "lead_id", None) or (lead.get("lead_id") if isinstance(lead, dict) else "UNKNOWN")
                company_name = getattr(lead, "company_name", None) or (lead.get("company_name") if isinstance(lead, dict) else "UNKNOWN")
                err_msg = (
                    f"Defensive Qualification Guard Violation: save_qualified_leads received non-OUTREACH_READY lead. "
                    f"lead_id='{lead_id}', company_name='{company_name}', qualification_state='{q_state}', "
                    f"intended_destination='LEADS', reason='Only confirmed OUTREACH_READY leads may enter LEADS.'"
                )
                print(f"[GoogleSheets Guard Error] {err_msg}")
                raise ValueError(err_msg)

        with crm_write_lock():
            self.ensure_leads_columns()
            try:
                existing_rows = self.leads_worksheet.get_all_records()
            except Exception as exc:
                # Never treat an unreadable CRM as an empty CRM: that would
                # bypass identity matching and could append duplicate leads.
                raise RuntimeError(
                    "[GoogleSheets Deduplication Block] Could not read LEADS before writing; "
                    "refusing to insert or update qualified leads without a complete deduplication snapshot."
                ) from exc

            today_str = datetime.now().strftime("%Y-%m-%d")
            rows_to_insert = []
            updated_count = 0
            inserted_count = 0
            end_col = col_to_letter(len(LEADS_COLUMNS))

            for lead in leads:
                if not lead.date_added:
                    lead.date_added = today_str

                if lead.phone and lead.phone.startswith("+") and not lead.phone.startswith("'"):
                    lead.phone = f"'{lead.phone}"

                # Deterministically normalize blank outreach_status to NOT_READY
                if hasattr(lead, "outreach_status"):
                    lead.outreach_status = normalize_outreach_status(getattr(lead, "outreach_status", None))

                lead_dict = lead.to_dict() if hasattr(lead, "to_dict") else vars(lead)
                match_res = self.matcher.match_candidate(lead_dict, existing_rows)

                if match_res.outcome == IdentityMatchOutcome.EXISTING_BUSINESS.value and match_res.matched_lead:
                    self.stats["existing_businesses"] += 1
                    self.stats["duplicates_prevented"] += 1
                    old_data = match_res.matched_lead
                    row_idx = None
                    for idx, r in enumerate(existing_rows, start=2):
                        if r.get("lead_id") and str(r.get("lead_id")).strip() == str(old_data.get("lead_id")).strip():
                            row_idx = idx
                            break
                        elif r == old_data:
                            row_idx = idx
                            break

                    if old_data.get("lead_id"):
                        lead.lead_id = str(old_data["lead_id"])

                    for fld in [
                        "campaign_id", "outreach_mode", "outreach_status", "outreach_channel",
                        "outreach_message", "outreach_generated_at", "outreach_sent_at",
                        "outreach_attempt_count", "outreach_message_id", "outreach_block_reason",
                        "selected_channel", "channel_selection_reason", "channel_availability",
                        "call_status", "call_notes", "call_outcome", "next_follow_up",
                        "response_status", "manual_outreach_notes", "notes", "lead_status",
                        "last_contact_at", "follow_up_enabled", "follow_up_at", "response_type",
                        "follow_up_number", "follow_up_notes", "sales_stage", "meeting_status",
                        "proposal_status", "client_status", "revenue", "bounce_code", "bounce_reason",
                        "bounced_at", "bounce_provider", "email_suppressed", "email_suppression_reason",
                        "date_added"
                    ]:
                        if old_data.get(fld):
                            if fld == "outreach_status":
                                old_stat = normalize_outreach_status(old_data.get("outreach_status"))
                                if old_stat != "NOT_READY":
                                    lead.outreach_status = old_stat
                                else:
                                    cur_stat = normalize_outreach_status(getattr(lead, "outreach_status", None))
                                    lead.outreach_status = cur_stat if cur_stat != "NOT_READY" else "NOT_READY"
                            elif fld == "lead_status":
                                if old_data.get("lead_status") != "NOT_CONTACTED":
                                    lead.lead_status = old_data["lead_status"]
                            elif not getattr(lead, fld, None):
                                setattr(lead, fld, old_data[fld])
                            else:
                                if fld in ["outreach_sent_at", "outreach_message_id", "last_contact_at", "campaign_id"]:
                                    setattr(lead, fld, old_data[fld])

                    if old_data.get("qualification_state") == "OUTREACH_READY":
                        lead.qualification_state = "OUTREACH_READY"

                    if not getattr(lead, "review_count", None) and old_data.get("review_count"):
                        lead.review_count = old_data["review_count"]
                    if not getattr(lead, "rating", None) and old_data.get("rating"):
                        lead.rating = old_data["rating"]

                    # Preserve creator evidence fields
                    for cf in ["creator_evidence_status", "creator_evidence_count", "creator_evidence_confidence",
                               "creator_latest_date", "creator_evidence_summary", "creator_evidence_urls", "creator_discovered_at"]:
                        if not getattr(lead, cf, None) and old_data.get(cf):
                            setattr(lead, cf, old_data[cf])

                    lead.outreach_status = normalize_outreach_status(getattr(lead, "outreach_status", None))
                    row_values = lead.to_sheet_row(LEADS_COLUMNS)
                    if row_idx:
                        cell_range = f"A{row_idx}:{end_col}{row_idx}"
                        try:
                            self.leads_worksheet.update(values=[row_values], range_name=cell_range, value_input_option="USER_ENTERED")
                            updated_count += 1
                            existing_rows[row_idx - 2] = {col: row_values[i] for i, col in enumerate(LEADS_COLUMNS) if i < len(row_values)}
                        except Exception as e:
                            print(f"[GoogleSheets] Failed to update row {row_idx} in LEADS: {e}")
                    else:
                        updated_count += 1

                elif match_res.outcome == IdentityMatchOutcome.POSSIBLE_DUPLICATE.value:
                    self.stats["possible_duplicates"] += 1
                    self.stats["duplicates_prevented"] += 1
                    print(f"[GoogleSheets] Possible duplicate detected for '{lead.company_name}' in {lead.city} (Matched: {match_res.matched_lead_id}, Confidence: {match_res.confidence:.2f}). Routing to REVIEW_QUEUE.")
                    lead.notes = f"POSSIBLE_DUPLICATE of {match_res.matched_lead_id} ({match_res.confidence:.2f}): {', '.join(match_res.match_reasons)}"
                    self.save_review_queue([lead])

                elif match_res.outcome == IdentityMatchOutcome.CONFLICT.value:
                    self.stats["conflicts"] += 1
                    print(f"[GoogleSheets] Identity CONFLICT detected for '{lead.company_name}': {match_res.conflict_notes}. Preserving record in REVIEW_QUEUE.")
                    lead.red_flags = f"IDENTITY_CONFLICT: {match_res.conflict_notes}"
                    self.save_review_queue([lead])

                else:  # NEW_BUSINESS
                    self.stats["new_businesses"] += 1
                    lead.outreach_status = normalize_outreach_status(getattr(lead, "outreach_status", None))
                    row_values = lead.to_sheet_row(LEADS_COLUMNS)
                    validate_row_width(row_values, LEADS_COLUMNS, "LEADS")
                    rows_to_insert.append(row_values)
                    inserted_count += 1
                    new_dict = {col: row_values[i] for i, col in enumerate(LEADS_COLUMNS) if i < len(row_values)}
                    existing_rows.append(new_dict)

            if rows_to_insert:
                for r in rows_to_insert:
                    validate_row_width(r, LEADS_COLUMNS, "LEADS")
                self.leads_worksheet.append_rows(rows_to_insert, value_input_option="USER_ENTERED", table_range=f"A1:{end_col}")

            print(f"[GoogleSheets] LEADS tab updated: {inserted_count} inserted, {updated_count} updated, {self.stats['duplicates_prevented']} duplicate rows prevented.")
            return inserted_count, updated_count

    def save_review_queue(self, leads: List[Lead]) -> Tuple[int, int]:
        """
        Saves MANUAL_REVIEW leads strictly to the REVIEW_QUEUE tab.
        Updates existing rows if already present; appends new rows.
        Returns: (inserted_count, updated_count)
        """
        if not leads:
            return 0, 0

        self.ensure_review_queue_columns()
        existing_map = self.get_existing_review_queue_map(fail_closed=True)
        today_str = datetime.now().strftime("%Y-%m-%d")

        rows_to_insert = []
        updated_count = 0
        inserted_count = 0
        end_col = col_to_letter(len(REVIEW_QUEUE_COLUMNS))

        for lead in leads:
            if not lead.date_added:
                lead.date_added = today_str

            if lead.phone and lead.phone.startswith("+") and not lead.phone.startswith("'"):
                lead.phone = f"'{lead.phone}"

            norm_id = self.normalize_identity(lead.company_name, lead.city, lead.target_country, lead.website)

            if norm_id in existing_map:
                row_idx, old_data = existing_map[norm_id]
                # Preserve existing stable lead_id and notes
                if old_data.get("lead_id"):
                    lead.lead_id = str(old_data["lead_id"])
                for fld in [
                    "campaign_id", "outreach_mode", "outreach_status", "outreach_channel",
                    "outreach_message", "outreach_generated_at", "outreach_sent_at",
                    "outreach_attempt_count", "outreach_message_id", "outreach_block_reason",
                    "call_status", "call_notes", "call_outcome", "next_follow_up",
                    "response_status", "manual_outreach_notes", "notes", "lead_status"
                ]:
                    if old_data.get(fld) and not getattr(lead, fld, None):
                        setattr(lead, fld, old_data[fld])

                row_values = lead.to_sheet_row(REVIEW_QUEUE_COLUMNS)
                cell_range = f"A{row_idx}:{end_col}{row_idx}"
                try:
                    self.review_queue_worksheet.update(values=[row_values], range_name=cell_range, value_input_option="USER_ENTERED")
                    updated_count += 1
                except Exception as e:
                    print(f"[GoogleSheets] Failed to update row {row_idx} in REVIEW_QUEUE: {e}")
            else:
                row_values = lead.to_sheet_row(REVIEW_QUEUE_COLUMNS)
                validate_row_width(row_values, REVIEW_QUEUE_COLUMNS, "REVIEW_QUEUE")
                rows_to_insert.append(row_values)
                inserted_count += 1
                existing_map[norm_id] = (len(existing_map) + len(rows_to_insert) + 1, {})

        if rows_to_insert:
            for r in rows_to_insert:
                validate_row_width(r, REVIEW_QUEUE_COLUMNS, "REVIEW_QUEUE")
            self.review_queue_worksheet.append_rows(rows_to_insert, value_input_option="USER_ENTERED", table_range=f"A1:{end_col}")

        print(f"[GoogleSheets] REVIEW_QUEUE tab updated: {inserted_count} inserted, {updated_count} updated.")
        return inserted_count, updated_count

    def save_research_log(self, entries: List[ResearchLogEntry]) -> Tuple[int, int]:
        """
        Saves all discovered and researched businesses to the RESEARCH_LOG tab.
        Uses a deterministic, explicit-range write (A{start_row}:AB{end_row}) under crm_write_lock.
        Does NOT rely on Google Sheets values.append or table-detection heuristics.
        Destination row is determined from the last non-empty row across the entire worksheet,
        guaranteeing that existing data (including shifted rows 280-317) is never overwritten.
        """
        if not entries:
            return 0, 0

        with crm_write_lock():
            self.ensure_research_columns()
            existing_map = self.get_existing_research_map(fail_closed=True)
            today_str = datetime.now().strftime("%Y-%m-%d")

            rows_to_insert = []
            insert_entries = []
            updated_count = 0
            inserted_count = 0
            end_col = col_to_letter(len(RESEARCH_LOG_COLUMNS))

            start_row = 0
            end_row = 0
            for entry in entries:
                row_values = entry.to_sheet_row(RESEARCH_LOG_COLUMNS)
                validate_row_width(row_values, RESEARCH_LOG_COLUMNS, "RESEARCH_LOG")

                if not entry.date_researched:
                    entry.date_researched = today_str

                if entry.phone and str(entry.phone).startswith("+") and not str(entry.phone).startswith("'"):
                    entry.phone = f"'{entry.phone}"

                # Multi-signal deduplication: never match on company name alone.
                match_result = existing_map.find_match(
                    company_name=entry.company_name,
                    city=entry.city,
                    target_country=entry.target_country,
                    website=entry.website,
                    source_url=getattr(entry, "source_url", "") or "",
                    phone=entry.phone,
                    address=entry.address
                )

                if match_result is not None:
                    row_idx, matched_dict, match_signal = match_result
                    is_malformed = matched_dict.get("is_malformed", False)

                    # FAIL CLOSED: if matched record is malformed in AA:BB (e.g. rows 280-317)
                    if is_malformed:
                        raise RuntimeError(
                            f"[GoogleSheets Malformed Deduplication Block] Cannot write research log for venue '{entry.company_name}': "
                            f"matches existing malformed record at row {row_idx} via {match_signal}. "
                            f"The existing record is located in shifted columns AA:BB and has not been recovered to A:AB or quarantined. "
                            f"Appending this record would create a duplicate CRM entry; updating in-place would overwrite corrupt cells. "
                            f"Safe deduplication cannot proceed. Production CRM writes fail closed until row {row_idx} is recovered or quarantined."
                        )

                    # Normal row update in A:AB
                    row_values = entry.to_sheet_row(RESEARCH_LOG_COLUMNS)
                    validate_row_width(row_values, RESEARCH_LOG_COLUMNS, "RESEARCH_LOG")
                    cell_range = f"A{row_idx}:{end_col}{row_idx}"
                    try:
                        self.research_worksheet.update(values=[row_values], range_name=cell_range, value_input_option="USER_ENTERED")
                        # Readback verification
                        try:
                            read_back = self.research_worksheet.get_values(cell_range)
                            if read_back and read_back[0]:
                                if str(read_back[0][0]).strip() != str(row_values[0]).strip():
                                    print(f"[GoogleSheets Warning] Readback research_id mismatch on row {row_idx}: expected {row_values[0]}, got {read_back[0][0]}")
                        except Exception:
                            pass
                        updated_count += 1
                    except Exception as e:
                        print(f"[GoogleSheets] Failed to update row {row_idx} in RESEARCH_LOG: {e}")
                else:
                    # No multi-signal match found.
                    # Verify safe deduplication: if unquarantined malformed rows exist in the sheet, ensure
                    # that an incoming record sharing a company name cannot be accidentally appended without
                    # verified multi-signal differentiation (source URL, distinct city, phone, or address).
                    if existing_map.malformed_rows:
                        clean_entry_name = re.sub(r'[^a-z0-9]', '', (entry.company_name or "").lower())
                        for m_row_idx, m_meta in existing_map.malformed_rows.items():
                            clean_m_name = re.sub(r'[^a-z0-9]', '', (m_meta.get("company_name", "")).lower())
                            if clean_entry_name and clean_m_name and clean_entry_name == clean_m_name:
                                entry_city = re.sub(r'[^a-z0-9]', '', (entry.city or "").lower())
                                m_city = re.sub(r'[^a-z0-9]', '', (m_meta.get("city", "")).lower())
                                entry_url = _clean_source_url(getattr(entry, "source_url", ""))
                                m_url = _clean_source_url(m_meta.get("source_url", ""))
                                entry_addr = re.sub(r'[^a-z0-9]', '', (entry.address or "").lower())
                                m_addr = re.sub(r'[^a-z0-9]', '', (m_meta.get("address", "")).lower())

                                is_distinct = False
                                if entry_city and m_city and entry_city != m_city:
                                    is_distinct = True
                                elif entry_url and m_url and entry_url != m_url:
                                    is_distinct = True
                                elif entry_addr and m_addr and entry_addr != m_addr:
                                    is_distinct = True

                                if not is_distinct:
                                    raise RuntimeError(
                                        f"[GoogleSheets Malformed Deduplication Block] Safe deduplication cannot be guaranteed for '{entry.company_name}': "
                                        f"shares company name with malformed record at row {m_row_idx} but lacks verified multi-signal "
                                        f"differentiation (source URL, distinct city, phone, or address). "
                                        f"Production CRM writes fail closed until row {m_row_idx} is recovered or quarantined."
                                    )

                    norm_id = self.normalize_identity(entry.company_name, entry.city, entry.target_country, entry.website)
                    row_values = entry.to_sheet_row(RESEARCH_LOG_COLUMNS)
                    validate_row_width(row_values, RESEARCH_LOG_COLUMNS, "RESEARCH_LOG")
                    rows_to_insert.append(row_values)
                    insert_entries.append((norm_id, entry))

            if rows_to_insert:
                for r in rows_to_insert:
                    validate_row_width(r, RESEARCH_LOG_COLUMNS, "RESEARCH_LOG")

                # 1. Determine destination row from the last non-empty row across the ENTIRE worksheet
                try:
                    all_values = self.research_worksheet.get_all_values()
                except Exception as e:
                    raise RuntimeError(f"[GoogleSheets] Failed to inspect RESEARCH_LOG grid for safe append destination: {e}")

                if not all_values:
                    raise RuntimeError("[GoogleSheets] Cannot determine destination row: RESEARCH_LOG worksheet appears empty (missing headers).")

                last_non_empty_row = 0
                for r_idx, r in enumerate(all_values, start=1):
                    if any(str(cell).strip() for cell in r):
                        last_non_empty_row = r_idx

                if last_non_empty_row < 1:
                    raise RuntimeError("[GoogleSheets] Failed to find valid header or content row in RESEARCH_LOG; failing closed.")

                start_row = last_non_empty_row + 1
                end_row = start_row + len(rows_to_insert) - 1
                dest_range = f"A{start_row}:{end_col}{end_row}"

                # 2. Ensure worksheet grid has sufficient rows allocated
                try:
                    current_grid_rows = self.research_worksheet.row_count
                    if end_row > current_grid_rows:
                        needed = (end_row - current_grid_rows) + 50
                        self.research_worksheet.add_rows(needed)
                except Exception as e:
                    print(f"[GoogleSheets] Grid expansion notice: {e}")

                # 3. Destination safety check: ensure destination cells are not occupied
                for check_idx in range(start_row, min(end_row + 1, len(all_values) + 1)):
                    row_data = all_values[check_idx - 1]
                    if any(str(c).strip() for c in row_data):
                        raise RuntimeError(
                            f"[GoogleSheets Safety Block] Destination row {check_idx} in range {dest_range} "
                            f"is not empty. Aborting write to prevent data corruption."
                        )

                # 4. Deterministic write to explicit range A{start_row}:AB{end_row}
                try:
                    self.research_worksheet.update(
                        values=rows_to_insert,
                        range_name=dest_range,
                        value_input_option="USER_ENTERED"
                    )
                except Exception as e:
                    raise RuntimeError(f"[GoogleSheets] Failed to write new records to {dest_range}: {e}")

                # 5. Read back and verify the written records
                try:
                    read_back = self.research_worksheet.get_values(dest_range)
                    if len(read_back) != len(rows_to_insert):
                        raise RuntimeError(
                            f"[GoogleSheets Verification Failed] Expected {len(rows_to_insert)} rows in {dest_range}, "
                            f"but read back {len(read_back)} rows."
                        )
                    for offset, (exp_row, act_row) in enumerate(zip(rows_to_insert, read_back)):
                        curr_row = start_row + offset
                        if not act_row:
                            raise RuntimeError(f"[GoogleSheets Verification Failed] Row {curr_row} read back empty.")
                        # Verify research_id (index 0) and company_name (index 1)
                        if str(act_row[0]).strip() != str(exp_row[0]).strip():
                            raise RuntimeError(
                                f"[GoogleSheets Verification Failed] Row {curr_row} research_id mismatch: "
                                f"expected '{exp_row[0]}', read back '{act_row[0]}'."
                            )
                        if len(act_row) > 1 and len(exp_row) > 1:
                            if str(act_row[1]).strip() != str(exp_row[1]).strip():
                                raise RuntimeError(
                                    f"[GoogleSheets Verification Failed] Row {curr_row} company_name mismatch: "
                                    f"expected '{exp_row[1]}', read back '{act_row[1]}'."
                                )
                except Exception as e:
                    raise RuntimeError(f"[GoogleSheets Readback Verification Error] {e}")

                # 6. Update existing_map and count
                for offset, (norm_id, entry) in enumerate(insert_entries):
                    existing_map[norm_id] = (start_row + offset, {})
                    inserted_count += 1

            range_info = f" (rows {start_row}-{end_row} in A:AB)" if inserted_count > 0 else ""
            print(f"[GoogleSheets] RESEARCH_LOG tab updated: {inserted_count} inserted{range_info}, {updated_count} updated.")
            return inserted_count, updated_count

    def save_leads(self, leads: List[Lead]) -> Tuple[int, int]:
        """Backward compatibility: saves leads to LEADS tab."""
        return self.save_qualified_leads(leads)

    def fetch_all_leads(self) -> List[Dict[str, Any]]:
        """
        Fetches all qualified leads from LEADS tab for dashboard views.
        Normalizes blank outreach_status values to 'NOT_READY' deterministically.
        """
        # Dashboard reads must never rewrite headers, add columns, or create tabs.
        records = self.spreadsheet.worksheet("LEADS").get_all_records()
        for r in records:
            r["outreach_status"] = normalize_outreach_status(r.get("outreach_status"))
        return records

    def fetch_review_queue(self) -> List[Dict[str, Any]]:
        """
        Fetches all manual review candidates from REVIEW_QUEUE tab.
        """
        # Read the existing tab directly; schema setup belongs to explicit write/setup workflows.
        return self.spreadsheet.worksheet("REVIEW_QUEUE").get_all_records()

    def fetch_research_log(self) -> List[Dict[str, Any]]:
        """
        Fetches all researched businesses from RESEARCH_LOG tab.
        Uses expected_headers to handle trailing empty column headers safely.
        """
        # Read-only path: never invoke schema repair or implicit worksheet creation.
        worksheet = self.spreadsheet.worksheet("RESEARCH_LOG")
        try:
            return worksheet.get_all_records(expected_headers=RESEARCH_LOG_COLUMNS)
        except Exception:
            rows = worksheet.get_all_values()
            if not rows or len(rows) < 2:
                return []
            headers = [h.strip() for h in rows[0] if h.strip()]
            records = []
            for row in rows[1:]:
                rec = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}
                records.append(rec)
            return records

    def update_lead_outreach(self, lead_id: str, fields: Dict[str, Any]) -> bool:
        """
        Updates outreach fields for a specific lead in LEADS or REVIEW_QUEUE tab by lead_id.
        Preserves existing lead data and uses a single batch row update to avoid API rate limits.
        Deterministically normalizes blank outreach_status to 'NOT_READY'.
        """
        self.ensure_leads_columns()
        for ws in [self.leads_worksheet, self.review_queue_worksheet]:
            try:
                records = ws.get_all_records()
            except Exception:
                continue

            headers = ws.row_values(1)
            for idx, row in enumerate(records, start=2):
                if str(row.get("lead_id", "")).strip() == lead_id.strip():
                    updated_row = []
                    for h in headers:
                        if h in fields:
                            val_str = str(fields[h]).strip() if fields[h] is not None else ""
                            if h == "phone" and val_str.startswith("+") and not val_str.startswith("'"):
                                val_str = f"'{val_str}"
                            elif h == "outreach_status":
                                val_str = normalize_outreach_status(val_str)
                            updated_row.append(val_str)
                        else:
                            val_str = str(row.get(h, "")).strip()
                            if h == "outreach_status":
                                val_str = normalize_outreach_status(val_str)
                            updated_row.append(val_str)
                    end_col = col_to_letter(len(headers))
                    try:
                        ws.update(values=[updated_row], range_name=f"A{idx}:{end_col}{idx}")
                        return True
                    except Exception as e:
                        print(f"[GoogleSheets] Error updating row {idx}: {e}")
                        return False
        return False

    def apply_crm_theme(self) -> Dict[str, Any]:
        """
        Applies the luxury Yellow, Black, and White theme across all CRM worksheets:
        - OVERVIEW: Executive KPI command dashboard with live formulas
        - LEADS: Confirmed qualified prospects
        - REVIEW_QUEUE: Ambiguous prospects awaiting human review
        - RESEARCH_LOG: Full audit trail of all discovered businesses
        """
        from lib.sheets.theme import apply_full_crm_theme
        return apply_full_crm_theme(self)


