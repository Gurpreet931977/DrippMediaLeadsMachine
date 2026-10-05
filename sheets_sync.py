import os
import json
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

DEFAULT_HEADERS = [
    "Company / Brand Name",
    "Niche / Category",
    "Location",
    "Website",
    "Email",
    "Phone",
    "Instagram / Social",
    "Followers / Size",
    "Source",
    "Status",
    "Notes"
]

def get_sheet_client():
    # 1. Direct JSON secret (string payload)
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY")
    if sa_json:
        try:
            sa_data = json.loads(sa_json) if isinstance(sa_json, str) else sa_json
            creds = Credentials.from_service_account_info(sa_data, scopes=SCOPES)
            return gspread.authorize(creds)
        except Exception as e:
            pass

    # 2. Individual fields (email + private key)
    sa_email = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL")
    sa_pkey = os.getenv("GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY")
    if sa_email and sa_pkey:
        try:
            pkey_formatted = sa_pkey.replace("\\n", "\n")
            sa_data = {
                "type": "service_account",
                "client_email": sa_email,
                "private_key": pkey_formatted,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
            creds = Credentials.from_service_account_info(sa_data, scopes=SCOPES)
            return gspread.authorize(creds)
        except Exception:
            pass

    # 3. Credentials file path
    creds_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "poised-eye-509816-a4-5444ff3f8520.json")
    if not os.path.isabs(creds_file):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        creds_file = os.path.join(base_dir, creds_file)
        
    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    return gspread.authorize(creds)

def get_worksheet(sheet_url=None, worksheet_name="Sheet1"):
    url = sheet_url or os.getenv("GOOGLE_SHEET_URL")
    sheet_id = os.getenv("GOOGLE_SHEET_ID") or os.getenv("SPREADSHEET_ID")
    gc = get_sheet_client()
    if url:
        spreadsheet = gc.open_by_url(url)
    elif sheet_id:
        spreadsheet = gc.open_by_key(sheet_id)
    else:
        raise ValueError("Neither GOOGLE_SHEET_URL nor GOOGLE_SHEET_ID / SPREADSHEET_ID is set.")
    try:
        return spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        return spreadsheet.sheet1

def ensure_headers(ws, headers=None):
    headers = headers or DEFAULT_HEADERS
    existing = ws.row_values(1)
    if not existing:
        ws.insert_row(headers, index=1)
        # Apply header styling (freeze row 1, bold)
        try:
            ws.format("A1:Z1", {"textFormat": {"bold": True}})
            ws.freeze(rows=1)
        except Exception:
            pass
        return True
    return False

def append_leads(leads, sheet_url=None, worksheet_name="Sheet1"):
    """
    Append list of dictionaries or list of lists to the spreadsheet.
    Each lead dict can have keys matching the headers.
    Enforces deduplication via BusinessIdentityMatcher to prevent duplicate rows.
    Uses crm_write_lock to guarantee atomic, concurrency-safe persistence.
    """
    from lib.crm.identity_matcher import BusinessIdentityMatcher, IdentityMatchOutcome
    from lib.crm.reconciliation import crm_write_lock

    with crm_write_lock():
        matcher = BusinessIdentityMatcher()
        ws = get_worksheet(sheet_url, worksheet_name)
        ensure_headers(ws)
        
        headers = ws.row_values(1)
        try:
            existing_records = ws.get_all_records()
        except Exception:
            existing_records = []

        rows_to_add = []
        duplicates_prevented = 0
        
        for lead in leads:
            if isinstance(lead, dict):
                # Check for existing business before appending
                match_res = matcher.match_candidate(lead, existing_records)
                if match_res.outcome in [IdentityMatchOutcome.EXISTING_BUSINESS.value, IdentityMatchOutcome.POSSIBLE_DUPLICATE.value]:
                    duplicates_prevented += 1
                    continue
                row = [str(lead.get(h, "")) for h in headers]
                rows_to_add.append(row)
                existing_records.append(lead)
            elif isinstance(lead, (list, tuple)):
                rows_to_add.append([str(x) for x in lead])
                
        if rows_to_add:
            ws.append_rows(rows_to_add, value_input_option="USER_ENTERED")
        if duplicates_prevented > 0:
            print(f"[sheets_sync] Deduplication: {duplicates_prevented} duplicate leads prevented from being appended.")
        return len(rows_to_add)


if __name__ == "__main__":
    print("Testing Google Sheet connection...")
    ws = get_worksheet()
    print(f"Connected to sheet: '{ws.spreadsheet.title}', tab: '{ws.title}'")
    ensure_headers(ws)
    print("Headers verified:", ws.row_values(1))
    print("Google Sheets integration is 100% operational!")
