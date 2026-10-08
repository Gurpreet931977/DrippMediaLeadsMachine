import os
import re
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
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
        self.sheet_url = sheet_url or os.getenv("GOOGLE_SHEET_URL")
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
            if not self.sheet_url:
                raise ValueError("GOOGLE_SHEET_URL is not set.")
            try:
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
            rows = self.leads_worksheet.get_all_records()
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

    def get_existing_research_map(self) -> Dict[str, Tuple[int, Dict[str, Any]]]:
        """
        Loads all existing rows from RESEARCH_LOG tab and indexes them by normalized identity.
        """
        try:
            rows = self.research_worksheet.get_all_records()
        except Exception:
            return {}

        research_map = {}
        for idx, row in enumerate(rows, start=2):
            c_name = str(row.get("company_name", ""))
            city = str(row.get("city", ""))
            target_country = str(row.get("target_country", ""))
            web = str(row.get("website", ""))
            norm_id = self.normalize_identity(c_name, city, target_country, web)
            if norm_id:
                research_map[norm_id] = (idx, row)
        return research_map

    def get_existing_review_queue_map(self) -> Dict[str, Tuple[int, Dict[str, Any]]]:
        """
        Loads all existing rows from REVIEW_QUEUE tab and indexes them by normalized identity.
        """
        try:
            rows = self.review_queue_worksheet.get_all_records()
        except Exception:
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
            except Exception:
                existing_rows = []

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
                    rows_to_insert.append(row_values)
                    inserted_count += 1
                    new_dict = {col: row_values[i] for i, col in enumerate(LEADS_COLUMNS) if i < len(row_values)}
                    existing_rows.append(new_dict)

            if rows_to_insert:
                self.leads_worksheet.append_rows(rows_to_insert, value_input_option="USER_ENTERED")

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
        existing_map = self.get_existing_review_queue_map()
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
                rows_to_insert.append(row_values)
                inserted_count += 1
                existing_map[norm_id] = (len(existing_map) + len(rows_to_insert) + 1, {})

        if rows_to_insert:
            self.review_queue_worksheet.append_rows(rows_to_insert, value_input_option="USER_ENTERED")

        print(f"[GoogleSheets] REVIEW_QUEUE tab updated: {inserted_count} inserted, {updated_count} updated.")
        return inserted_count, updated_count

    def save_research_log(self, entries: List[ResearchLogEntry]) -> Tuple[int, int]:
        """
        Saves all discovered and researched businesses to the RESEARCH_LOG tab.
        """
        if not entries:
            return 0, 0

        self.ensure_research_columns()
        existing_map = self.get_existing_research_map()
        today_str = datetime.now().strftime("%Y-%m-%d")

        rows_to_insert = []
        updated_count = 0
        inserted_count = 0
        end_col = col_to_letter(len(RESEARCH_LOG_COLUMNS))

        for entry in entries:
            if not entry.date_researched:
                entry.date_researched = today_str

            if entry.phone and entry.phone.startswith("+") and not entry.phone.startswith("'"):
                entry.phone = f"'{entry.phone}"

            norm_id = self.normalize_identity(entry.company_name, entry.city, entry.target_country, entry.website)

            if norm_id in existing_map:
                row_idx, _ = existing_map[norm_id]
                row_values = entry.to_sheet_row(RESEARCH_LOG_COLUMNS)
                cell_range = f"A{row_idx}:{end_col}{row_idx}"
                try:
                    self.research_worksheet.update(values=[row_values], range_name=cell_range, value_input_option="USER_ENTERED")
                    updated_count += 1
                except Exception as e:
                    print(f"[GoogleSheets] Failed to update row {row_idx} in RESEARCH_LOG: {e}")
            else:
                row_values = entry.to_sheet_row(RESEARCH_LOG_COLUMNS)
                rows_to_insert.append(row_values)
                inserted_count += 1
                existing_map[norm_id] = (len(existing_map) + len(rows_to_insert) + 1, {})

        if rows_to_insert:
            self.research_worksheet.append_rows(rows_to_insert, value_input_option="USER_ENTERED")

        print(f"[GoogleSheets] RESEARCH_LOG tab updated: {inserted_count} inserted, {updated_count} updated.")
        return inserted_count, updated_count

    def save_leads(self, leads: List[Lead]) -> Tuple[int, int]:
        """Backward compatibility: saves leads to LEADS tab."""
        return self.save_qualified_leads(leads)

    def fetch_all_leads(self) -> List[Dict[str, Any]]:
        """
        Fetches all qualified leads from LEADS tab for dashboard views.
        Normalizes blank outreach_status values to 'NOT_READY' deterministically.
        """
        self.ensure_leads_columns()
        records = self.leads_worksheet.get_all_records()
        for r in records:
            r["outreach_status"] = normalize_outreach_status(r.get("outreach_status"))
        return records

    def fetch_review_queue(self) -> List[Dict[str, Any]]:
        """
        Fetches all manual review candidates from REVIEW_QUEUE tab.
        """
        self.ensure_review_queue_columns()
        return self.review_queue_worksheet.get_all_records()

    def fetch_research_log(self) -> List[Dict[str, Any]]:
        """
        Fetches all researched businesses from RESEARCH_LOG tab.
        """
        self.ensure_research_columns()
        return self.research_worksheet.get_all_records()

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


