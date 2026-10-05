"""
Dripp Media — Google Sheets Theme & Visual Design Engine
=========================================================
Implements a luxury Yellow, Black, and White color theme for the CRM spreadsheet.
Optimized for high usability, clean typography, comfortable spacing, and immediate clarity.

Palette Tokens:
- Deep Onyx Black (#141417): Headers, High Priority Badges, Tab accents
- Electric Yellow (#FFD700 / #FACC15): Header Text, Ready/Sent Badges, Key Borders
- Warm Amber Yellow (#FDE68A): Manual Review Badges, Medium Priority
- Pale Cream Yellow (#FEF9C3): In-Flight Communication, Awaiting Response
- Crisp Pure White (#FFFFFF): Primary data row background, Card surfaces
- Soft Slate Off-White (#F9FAFB): Alternating zebra banding
- Cool Neutral Gray (#F1F5F9): Disqualified, Inactive, Terminal states
- Fine Grid Border (#E2E8F0): Subtle cell outlines
"""

import os
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

from sheets_sync import get_sheet_client
from lib.sheets.google_sheets import LEADS_COLUMNS, REVIEW_QUEUE_COLUMNS, RESEARCH_LOG_COLUMNS

load_dotenv()

# --- Color Tokens (Google Sheets v4 API: RGB 0.0 - 1.0) ---
COLOR_BLACK = {"red": 0.08, "green": 0.08, "blue": 0.09}          # #141417 (Header background)
COLOR_CHARCOAL = {"red": 0.12, "green": 0.12, "blue": 0.14}       # #1F1F24 (Sub-headers)
COLOR_DEEP_BLACK = {"red": 0.04, "green": 0.04, "blue": 0.05}     # #0A0A0D (Title banner)
COLOR_DARK_TEXT = {"red": 0.12, "green": 0.12, "blue": 0.14}      # #1F1F24 (Default text)

COLOR_YELLOW = {"red": 1.0, "green": 0.84, "blue": 0.0}           # #FFD700 (Electric Gold)
COLOR_VIVID_YELLOW = {"red": 0.98, "green": 0.80, "blue": 0.08}   # #FACC15 (Vivid Accent / SENT)
COLOR_SOFT_YELLOW = {"red": 1.0, "green": 0.94, "blue": 0.55}     # #FEF08A (OUTREACH_READY badge)
COLOR_PALE_CREAM = {"red": 0.996, "green": 0.976, "blue": 0.765}  # #FEF9C3 (Awaiting Response)
COLOR_AMBER_YELLOW = {"red": 0.99, "green": 0.88, "blue": 0.50}   # #FDE68A (MANUAL_REVIEW badge)
COLOR_YELLOW_TEXT_DARK = {"red": 0.40, "green": 0.22, "blue": 0.04} # #66380A (Dark bronze for readability)

COLOR_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}             # #FFFFFF (Band 1)
COLOR_OFF_WHITE = {"red": 0.976, "green": 0.980, "blue": 0.984}   # #F9FAFB (Band 2 zebra)
COLOR_LIGHT_GRAY = {"red": 0.94, "green": 0.95, "blue": 0.96}     # #F1F5F9 (Disqualified / Lost)
COLOR_GRAY_TEXT = {"red": 0.45, "green": 0.48, "blue": 0.54}      # #737B8A (Muted text)

COLOR_SOFT_RED = {"red": 0.99, "green": 0.89, "blue": 0.89}       # #FEE2E2 (Failed / Bounced)
COLOR_RED_TEXT = {"red": 0.65, "green": 0.15, "blue": 0.15}       # #A62626 (Failed text)

COLOR_BORDER_THIN = {"red": 0.88, "green": 0.90, "blue": 0.92}    # Subtle cell border


def get_optimal_column_width(col_name: str) -> int:
    """Returns the usage-optimized column pixel width based on data content type."""
    c = col_name.lower().strip()
    if c in ["lead_id", "research_id", "campaign_id", "pipeline_run_id", "outreach_message_id"]:
        return 125
    if c in ["company_name", "registered_name", "trading_name", "legal_entity_name"]:
        return 220
    if c in ["industry", "target_country", "country", "detected_country", "city", "region"]:
        return 130
    if c in ["postcode", "sic_codes", "country_status"]:
        return 95
    if c in ["address", "registered_office"]:
        return 240
    if c in ["phone"]:
        return 135
    if c in ["website", "instagram_url", "facebook_url", "tiktok_url", "companies_house_url", "source_url", "email_source_url"]:
        return 180
    if c in ["email", "primary_email"]:
        return 220
    if c in ["email_candidates", "email_search_queries"]:
        return 250
    if c in ["review_count", "rating", "lead_score", "priority", "follow_up_number", "contact_history_count", "email_searches_performed"]:
        return 90
    if any(k in c for k in ["status", "state", "stage", "mode", "confidence", "verified", "subscriber_type", "company_type"]):
        return 155
    if any(k in c for k in ["message", "notes", "evidence", "reason", "flags", "signals", "angle", "response", "queries", "context"]):
        return 280
    if any(k in c for k in ["date", "at", "until", "freshness"]):
        return 145
    return 130


def build_tab_cleanup_requests(sheet_meta: Dict[str, Any], sheet_id: int) -> List[Dict[str, Any]]:
    """Generates idempotent requests to clear existing bandings, filters, and conditional rules."""
    reqs = []
    for s in sheet_meta.get("sheets", []):
        if s["properties"]["sheetId"] == sheet_id:
            for b in s.get("bandedRanges", []):
                reqs.append({"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}})
            if s.get("basicFilter"):
                reqs.append({"clearBasicFilter": {"sheetId": sheet_id}})
            for idx in reversed(range(len(s.get("conditionalFormats", [])))):
                reqs.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": idx}})
    return reqs


def populate_overview_tab(sh) -> None:
    """Populates the OVERVIEW command dashboard with live formulas and luxury yellow/black/white design."""
    ws = sh.worksheet("OVERVIEW")
    sheet_id = ws.id

    # 1. Update sheet tab color and dimensions
    sh.batch_update({
        "requests": [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "tabColor": COLOR_VIVID_YELLOW,
                        "gridProperties": {
                            "rowCount": 35,
                            "columnCount": 12,
                            "hideGridlines": False
                        }
                    },
                    "fields": "tabColor,gridProperties.rowCount,gridProperties.columnCount,gridProperties.hideGridlines"
                }
            }
        ]
    })

    # 2. Set Column Widths (A=30, B=175, C=18, D=175, E=18, F=175, G=18, H=175, I=18, J=175, K=18, L=30)
    col_widths = [30, 175, 18, 175, 18, 175, 18, 175, 18, 175, 18, 30]
    col_width_reqs = []
    for c_idx, w in enumerate(col_widths):
        col_width_reqs.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": c_idx,
                    "endIndex": c_idx + 1
                },
                "properties": {"pixelSize": w},
                "fields": "pixelSize"
            }
        })
    sh.batch_update({"requests": col_width_reqs})

    # 3. Set Row Heights
    row_heights = {
        0: 20, 1: 44, 2: 26, 3: 20, 4: 26, 5: 46, 6: 22, 7: 24,
        8: 32, 9: 28, 10: 28, 11: 28, 12: 28, 13: 24,
        14: 32, 15: 28, 16: 28, 17: 28, 18: 28, 19: 28, 20: 28
    }
    row_height_reqs = []
    for r_idx, h in row_heights.items():
        row_height_reqs.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": r_idx,
                    "endIndex": r_idx + 1
                },
                "properties": {"pixelSize": h},
                "fields": "pixelSize"
            }
        })
    sh.batch_update({"requests": row_height_reqs})

    # 4. Values & Formulas to write
    updates = [
        # Top banner
        {"range": "B2", "values": [["⚡  DRIPP MEDIA  —  INTERNATIONAL PROSPECTS INTELLIGENCE CRM"]]},
        {"range": "B3", "values": [["Single Source of Truth  •  Strict Qualification V3  •  Multi-Channel Outreach Engine"]]},

        # KPI Cards (Row 5: Titles, Row 6: Live Dynamic Formulas, Row 7: Subtitles)
        {"range": "B5", "values": [["TOTAL DISCOVERED"]]},
        {"range": "D5", "values": [["QUALIFIED & READY"]]},
        {"range": "F5", "values": [["OUTREACHES SENT"]]},
        {"range": "H5", "values": [["IN REVIEW QUEUE"]]},
        {"range": "J5", "values": [["AWAITING RESPONSE"]]},

        {"range": "B6", "values": [["=COUNTA('RESEARCH_LOG'!A2:A)"]]},
        {"range": "D6", "values": [["=COUNTA('LEADS'!A2:A)"]]},
        {"range": "F6", "values": [["=COUNTIF('LEADS'!AR2:AR, \"SENT\")"]]},
        {"range": "H6", "values": [["=COUNTA('REVIEW_QUEUE'!A2:A)"]]},
        {"range": "J6", "values": [["=COUNTIF('LEADS'!BG2:BG, \"AWAITING_RESPONSE\")"]]},

        {"range": "B7", "values": [["All Entities Researched"]]},
        {"range": "D7", "values": [["V3 Operational Passed"]]},
        {"range": "F7", "values": [["SMTP 250 Dispatched"]]},
        {"range": "H7", "values": [["Pending Human Audit"]]},
        {"range": "J7", "values": [["Follow-Up Candidate"]]},

        # Directory Section
        {"range": "B9", "values": [["📋  CRM WORKSPACE DIRECTORY & DATA ARCHITECTURE"]]},
        {"range": "B10", "values": [["Tab / Worksheet", "", "Pipeline Purpose & Qualification Criteria", "", "", "", "Target Status", "Panes", "Active Count"]]},
        {"range": "B11", "values": [["LEADS", "", "Confirmed high-intent prospects meeting operational, website, and social criteria", "", "", "", "OUTREACH_READY", "Row 1, Cols A-B", "=COUNTA('LEADS'!A2:A)"]]},
        {"range": "B12", "values": [["REVIEW_QUEUE", "", "Prospects with conflicting signals, missing direct email, or dissolved entity match", "", "", "", "MANUAL_REVIEW", "Row 1, Cols A-B", "=COUNTA('REVIEW_QUEUE'!A2:A)"]]},
        {"range": "B13", "values": [["RESEARCH_LOG", "", "Complete audit trail of all discovered leads including rejected, disqualified, and duplicates", "", "", "", "ALL_RESEARCHED", "Row 1, Cols A-B", "=COUNTA('RESEARCH_LOG'!A2:A)"]]},

        # Color System & Reference
        {"range": "B15", "values": [["🎨  YELLOW, BLACK & WHITE COLOR SYSTEM & STATUS REFERENCE"]]},
        {"range": "B16", "values": [["Color Accent", "", "Visual Pill", "", "Semantic Meaning", "", "Mapped CRM Statuses & States", "", ""]]},
        {"range": "B17", "values": [["Electric Gold Yellow", "", "OUTREACH_READY", "", "High-value milestone / Outreach approved", "", "OUTREACH_READY, SENT, WON, CONTACTABLE", "", ""]]},
        {"range": "B18", "values": [["Warm Amber Yellow", "", "MANUAL_REVIEW", "", "Action required / Partial verification", "", "MANUAL_REVIEW, MEDIUM Priority, NEEDS_ENRICHMENT", "", ""]]},
        {"range": "B19", "values": [["Pale Cream Yellow", "", "CONTACTED", "", "In-flight communication / Awaiting reply", "", "CONTACTED, AWAITING_RESPONSE, PENDING, QUEUED", "", ""]]},
        {"range": "B20", "values": [["Deep Onyx Black", "", "HIGH PRIORITY", "", "Top tier focus / Safety block compliance", "", "HIGH Priority, DO_NOT_CONTACT, UNSUBSCRIBED", "", ""]]},
        {"range": "B21", "values": [["Clean Slate Gray", "", "DISQUALIFIED", "", "Terminal state / Ineligible entity", "", "DISQUALIFIED, NOT_CONTACTABLE, NO_REPLY, LOST", "", ""]]}
    ]

    for u in updates:
        ws.update(values=u["values"], range_name=u["range"], value_input_option="USER_ENTERED")

    # 5. Format Cells, Merges, and Borders
    format_requests = [
        # Merges
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 1, "endColumnIndex": 10}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": 3, "startColumnIndex": 1, "endColumnIndex": 10}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 8, "endRowIndex": 9, "startColumnIndex": 1, "endColumnIndex": 10}, "mergeType": "MERGE_ALL"}},

        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 9, "endRowIndex": 10, "startColumnIndex": 1, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 9, "endRowIndex": 10, "startColumnIndex": 3, "endColumnIndex": 7}, "mergeType": "MERGE_ALL"}},

        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 10, "endRowIndex": 11, "startColumnIndex": 1, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 10, "endRowIndex": 11, "startColumnIndex": 3, "endColumnIndex": 7}, "mergeType": "MERGE_ALL"}},

        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 11, "endRowIndex": 12, "startColumnIndex": 1, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 11, "endRowIndex": 12, "startColumnIndex": 3, "endColumnIndex": 7}, "mergeType": "MERGE_ALL"}},

        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 12, "endRowIndex": 13, "startColumnIndex": 1, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 12, "endRowIndex": 13, "startColumnIndex": 3, "endColumnIndex": 7}, "mergeType": "MERGE_ALL"}},

        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 14, "endRowIndex": 15, "startColumnIndex": 1, "endColumnIndex": 10}, "mergeType": "MERGE_ALL"}},

        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 15, "endRowIndex": 16, "startColumnIndex": 1, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 15, "endRowIndex": 16, "startColumnIndex": 3, "endColumnIndex": 5}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 15, "endRowIndex": 16, "startColumnIndex": 5, "endColumnIndex": 7}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 15, "endRowIndex": 16, "startColumnIndex": 7, "endColumnIndex": 10}, "mergeType": "MERGE_ALL"}},

        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 16, "endRowIndex": 17, "startColumnIndex": 1, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 16, "endRowIndex": 17, "startColumnIndex": 3, "endColumnIndex": 5}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 16, "endRowIndex": 17, "startColumnIndex": 5, "endColumnIndex": 7}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 16, "endRowIndex": 17, "startColumnIndex": 7, "endColumnIndex": 10}, "mergeType": "MERGE_ALL"}},

        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 17, "endRowIndex": 18, "startColumnIndex": 1, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 17, "endRowIndex": 18, "startColumnIndex": 3, "endColumnIndex": 5}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 17, "endRowIndex": 18, "startColumnIndex": 5, "endColumnIndex": 7}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 17, "endRowIndex": 18, "startColumnIndex": 7, "endColumnIndex": 10}, "mergeType": "MERGE_ALL"}},

        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 18, "endRowIndex": 19, "startColumnIndex": 1, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 18, "endRowIndex": 19, "startColumnIndex": 3, "endColumnIndex": 5}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 18, "endRowIndex": 19, "startColumnIndex": 5, "endColumnIndex": 7}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 18, "endRowIndex": 19, "startColumnIndex": 7, "endColumnIndex": 10}, "mergeType": "MERGE_ALL"}},

        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 19, "endRowIndex": 20, "startColumnIndex": 1, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 19, "endRowIndex": 20, "startColumnIndex": 3, "endColumnIndex": 5}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 19, "endRowIndex": 20, "startColumnIndex": 5, "endColumnIndex": 7}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 19, "endRowIndex": 20, "startColumnIndex": 7, "endColumnIndex": 10}, "mergeType": "MERGE_ALL"}},

        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 20, "endRowIndex": 21, "startColumnIndex": 1, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 20, "endRowIndex": 21, "startColumnIndex": 3, "endColumnIndex": 5}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 20, "endRowIndex": 21, "startColumnIndex": 5, "endColumnIndex": 7}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 20, "endRowIndex": 21, "startColumnIndex": 7, "endColumnIndex": 10}, "mergeType": "MERGE_ALL"}},
    ]

    # Header format B2:J2
    format_requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 1, "endColumnIndex": 10},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_BLACK,
                    "textFormat": {"foregroundColor": COLOR_YELLOW, "fontFamily": "Roboto", "fontSize": 13, "bold": True},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })

    # Subtitle format B3:J3
    format_requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": 3, "startColumnIndex": 1, "endColumnIndex": 10},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_CHARCOAL,
                    "textFormat": {"foregroundColor": COLOR_WHITE, "fontFamily": "Roboto", "fontSize": 9, "bold": False},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })

    # Subtitle bottom border accent (Yellow)
    format_requests.append({
        "updateBorders": {
            "range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": 3, "startColumnIndex": 1, "endColumnIndex": 10},
            "bottom": {"style": "SOLID_MEDIUM", "color": COLOR_YELLOW}
        }
    })

    # KPI Headers format B5, D5, F5, H5, J5
    for c_idx in [1, 3, 5, 7, 9]:
        format_requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 4, "endRowIndex": 5, "startColumnIndex": c_idx, "endColumnIndex": c_idx + 1},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": COLOR_BLACK,
                        "textFormat": {"foregroundColor": COLOR_YELLOW, "fontFamily": "Roboto", "fontSize": 9, "bold": True},
                        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
            }
        })

    # KPI Values formats
    # Card 1 (B6): Off-white bg, dark text
    format_requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 5, "endRowIndex": 6, "startColumnIndex": 1, "endColumnIndex": 2},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_OFF_WHITE,
                    "textFormat": {"foregroundColor": COLOR_DARK_TEXT, "fontFamily": "Roboto", "fontSize": 22, "bold": True},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })
    # Card 2 (D6): Vivid Yellow bg, black text
    format_requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 5, "endRowIndex": 6, "startColumnIndex": 3, "endColumnIndex": 4},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_VIVID_YELLOW,
                    "textFormat": {"foregroundColor": {"red": 0.0, "green": 0.0, "blue": 0.0}, "fontFamily": "Roboto", "fontSize": 22, "bold": True},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })
    # Card 3 (F6): Jet Black bg, vivid yellow text
    format_requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 5, "endRowIndex": 6, "startColumnIndex": 5, "endColumnIndex": 6},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_BLACK,
                    "textFormat": {"foregroundColor": COLOR_YELLOW, "fontFamily": "Roboto", "fontSize": 22, "bold": True},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })
    # Card 4 (H6): Warm Amber Yellow bg, dark bronze text
    format_requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 5, "endRowIndex": 6, "startColumnIndex": 7, "endColumnIndex": 8},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_AMBER_YELLOW,
                    "textFormat": {"foregroundColor": COLOR_YELLOW_TEXT_DARK, "fontFamily": "Roboto", "fontSize": 22, "bold": True},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })
    # Card 5 (J6): Pale cream bg, dark text
    format_requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 5, "endRowIndex": 6, "startColumnIndex": 9, "endColumnIndex": 10},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_PALE_CREAM,
                    "textFormat": {"foregroundColor": COLOR_DARK_TEXT, "fontFamily": "Roboto", "fontSize": 22, "bold": True},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })

    # KPI Subtitles formats (Row 7)
    for c_idx in [1, 3, 5, 7, 9]:
        bg_col = [COLOR_OFF_WHITE, None, COLOR_VIVID_YELLOW, None, COLOR_BLACK, None, COLOR_AMBER_YELLOW, None, COLOR_PALE_CREAM][c_idx-1]
        fg_col = COLOR_YELLOW if c_idx == 5 else COLOR_GRAY_TEXT
        format_requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 6, "endRowIndex": 7, "startColumnIndex": c_idx, "endColumnIndex": c_idx + 1},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": bg_col,
                        "textFormat": {"foregroundColor": fg_col, "fontFamily": "Roboto", "fontSize": 8, "italic": True},
                        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
            }
        })
        # Card outer border
        format_requests.append({
            "updateBorders": {
                "range": {"sheetId": sheet_id, "startRowIndex": 4, "endRowIndex": 7, "startColumnIndex": c_idx, "endColumnIndex": c_idx + 1},
                "top": {"style": "SOLID", "color": COLOR_BORDER_THIN},
                "bottom": {"style": "SOLID_MEDIUM", "color": COLOR_YELLOW if c_idx in [3, 5] else COLOR_BORDER_THIN},
                "left": {"style": "SOLID", "color": COLOR_BORDER_THIN},
                "right": {"style": "SOLID", "color": COLOR_BORDER_THIN}
            }
        })

    # Section Header 1 (Directory B9:J9)
    format_requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 8, "endRowIndex": 9, "startColumnIndex": 1, "endColumnIndex": 10},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_BLACK,
                    "textFormat": {"foregroundColor": COLOR_YELLOW, "fontFamily": "Roboto", "fontSize": 10, "bold": True},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })
    format_requests.append({
        "updateBorders": {
            "range": {"sheetId": sheet_id, "startRowIndex": 8, "endRowIndex": 9, "startColumnIndex": 1, "endColumnIndex": 10},
            "bottom": {"style": "SOLID_MEDIUM", "color": COLOR_YELLOW}
        }
    })

    # Directory Table Header (Row 10)
    format_requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 9, "endRowIndex": 10, "startColumnIndex": 1, "endColumnIndex": 10},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_CHARCOAL,
                    "textFormat": {"foregroundColor": COLOR_YELLOW, "fontFamily": "Roboto", "fontSize": 9, "bold": True},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })

    # Directory Table Data (Rows 11-13)
    for r_idx in range(10, 13):
        bg = COLOR_WHITE if r_idx % 2 == 0 else COLOR_OFF_WHITE
        format_requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": r_idx, "endRowIndex": r_idx + 1, "startColumnIndex": 1, "endColumnIndex": 10},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": bg,
                        "textFormat": {"foregroundColor": COLOR_DARK_TEXT, "fontFamily": "Roboto", "fontSize": 9},
                        "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE"
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
            }
        })
        # Center align columns H, I, J
        format_requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": r_idx, "endRowIndex": r_idx + 1, "startColumnIndex": 7, "endColumnIndex": 10},
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                    }
                },
                "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment)"
            }
        })
        format_requests.append({
            "updateBorders": {
                "range": {"sheetId": sheet_id, "startRowIndex": r_idx, "endRowIndex": r_idx + 1, "startColumnIndex": 1, "endColumnIndex": 10},
                "bottom": {"style": "SOLID", "color": COLOR_BORDER_THIN},
                "left": {"style": "SOLID", "color": COLOR_BORDER_THIN},
                "right": {"style": "SOLID", "color": COLOR_BORDER_THIN}
            }
        })

    # Section Header 2 (Reference B15:J15)
    format_requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 14, "endRowIndex": 15, "startColumnIndex": 1, "endColumnIndex": 10},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_BLACK,
                    "textFormat": {"foregroundColor": COLOR_YELLOW, "fontFamily": "Roboto", "fontSize": 10, "bold": True},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })
    format_requests.append({
        "updateBorders": {
            "range": {"sheetId": sheet_id, "startRowIndex": 14, "endRowIndex": 15, "startColumnIndex": 1, "endColumnIndex": 10},
            "bottom": {"style": "SOLID_MEDIUM", "color": COLOR_YELLOW}
        }
    })

    # Reference Table Header (Row 16)
    format_requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 15, "endRowIndex": 16, "startColumnIndex": 1, "endColumnIndex": 10},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_CHARCOAL,
                    "textFormat": {"foregroundColor": COLOR_YELLOW, "fontFamily": "Roboto", "fontSize": 9, "bold": True},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
        }
    })

    # Legend rows formatting (Rows 17-21)
    legend_styles = [
        {"pill_bg": COLOR_VIVID_YELLOW, "pill_fg": {"red": 0.0, "green": 0.0, "blue": 0.0}},
        {"pill_bg": COLOR_AMBER_YELLOW, "pill_fg": COLOR_YELLOW_TEXT_DARK},
        {"pill_bg": COLOR_PALE_CREAM, "pill_fg": COLOR_DARK_TEXT},
        {"pill_bg": COLOR_BLACK, "pill_fg": COLOR_YELLOW},
        {"pill_bg": COLOR_LIGHT_GRAY, "pill_fg": COLOR_GRAY_TEXT},
    ]
    for idx, st in enumerate(legend_styles):
        r_idx = 16 + idx
        bg = COLOR_WHITE if idx % 2 == 0 else COLOR_OFF_WHITE
        format_requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": r_idx, "endRowIndex": r_idx + 1, "startColumnIndex": 1, "endColumnIndex": 10},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": bg,
                        "textFormat": {"foregroundColor": COLOR_DARK_TEXT, "fontFamily": "Roboto", "fontSize": 9},
                        "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE"
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
            }
        })
        format_requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": r_idx, "endRowIndex": r_idx + 1, "startColumnIndex": 3, "endColumnIndex": 5},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": st["pill_bg"],
                        "textFormat": {"foregroundColor": st["pill_fg"], "fontFamily": "Roboto", "fontSize": 9, "bold": True},
                        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
            }
        })
        format_requests.append({
            "updateBorders": {
                "range": {"sheetId": sheet_id, "startRowIndex": r_idx, "endRowIndex": r_idx + 1, "startColumnIndex": 1, "endColumnIndex": 10},
                "bottom": {"style": "SOLID", "color": COLOR_BORDER_THIN},
                "left": {"style": "SOLID", "color": COLOR_BORDER_THIN},
                "right": {"style": "SOLID", "color": COLOR_BORDER_THIN}
            }
        })

    sh.batch_update({"requests": format_requests})
    print("[ThemeEngine] OVERVIEW tab successfully populated and formatted.")


def build_data_tab_theme_requests(
    sheet_id: int,
    columns: List[str],
    row_count: int = 1000,
    tab_color: Optional[Dict[str, float]] = None,
    frozen_rows: int = 1,
    frozen_cols: int = 2
) -> List[Dict[str, Any]]:
    """
    Constructs the complete Google Sheets v4 API request batch for a data worksheet:
    - Tab color
    - Frozen panes (row 1 + cols 1-2)
    - Row heights (38px header, 28px data rows)
    - Header styling (Onyx black bg, Yellow bold text, Roboto 10pt, bottom border)
    - Data rows styling (Roboto 9pt, vertical middle alignment, clip wrap)
    - Alternating row banding (white / off-white)
    - Column widths (usage-optimized)
    - Basic filter enabled
    - Conditional formatting rules
    """
    num_cols = len(columns)
    requests = []

    # 1. Sheet properties (tab color + frozen panes)
    props = {
        "sheetId": sheet_id,
        "gridProperties": {
            "frozenRowCount": frozen_rows,
            "frozenColumnCount": frozen_cols
        }
    }
    fields = "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"
    if tab_color:
        props["tabColor"] = tab_color
        fields = "tabColor," + fields

    requests.append({
        "updateSheetProperties": {
            "properties": props,
            "fields": fields
        }
    })

    # 2. Header row height: 38px
    requests.append({
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": 0,
                "endIndex": 1
            },
            "properties": {"pixelSize": 38},
            "fields": "pixelSize"
        }
    })

    # 3. Data row heights: 28px
    requests.append({
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": 1,
                "endIndex": min(row_count, 1000)
            },
            "properties": {"pixelSize": 28},
            "fields": "pixelSize"
        }
    })

    # 4. Header row formatting: Jet Black background + Electric Yellow text + Middle + Clip
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_BLACK,
                    "textFormat": {
                        "foregroundColor": COLOR_YELLOW,
                        "fontFamily": "Roboto",
                        "fontSize": 10,
                        "bold": True
                    },
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "CLIP"
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)"
        }
    })

    # 5. Header bottom border accent (Vibrant Yellow medium line)
    requests.append({
        "updateBorders": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols
            },
            "bottom": {
                "style": "SOLID_MEDIUM",
                "color": COLOR_YELLOW
            }
        }
    })

    # 6. Data rows base formatting: Roboto 9pt, Middle alignment, Clip wrap
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": min(row_count, 1000),
                "startColumnIndex": 0,
                "endColumnIndex": num_cols
            },
            "cell": {
                "userEnteredFormat": {
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "CLIP",
                    "textFormat": {
                        "fontFamily": "Roboto",
                        "fontSize": 9,
                        "foregroundColor": COLOR_DARK_TEXT
                    }
                }
            },
            "fields": "userEnteredFormat(verticalAlignment,wrapStrategy,textFormat.fontFamily,textFormat.fontSize,textFormat.foregroundColor)"
        }
    })

    # 7. Alternating row banding (pure white and soft off-white)
    requests.append({
        "addBanding": {
            "bandedRange": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": min(row_count, 1000),
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols
                },
                "rowProperties": {
                    "headerColor": COLOR_BLACK,
                    "firstBandColor": COLOR_WHITE,
                    "secondBandColor": COLOR_OFF_WHITE
                }
            }
        }
    })

    # 8. Basic filter on row 1
    requests.append({
        "setBasicFilter": {
            "filter": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": min(row_count, 1000),
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols
                }
            }
        }
    })

    # 9. Optimized column widths
    for c_idx, col_name in enumerate(columns):
        width = get_optimal_column_width(col_name)
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": c_idx,
                    "endIndex": c_idx + 1
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize"
            }
        })

    # 10. Conditional formatting rules for status & priority columns
    cond_rules = build_conditional_formatting_rules(sheet_id, columns, row_count)
    requests.extend(cond_rules)

    return requests


def build_conditional_formatting_rules(sheet_id: int, columns: List[str], row_count: int = 1000) -> List[Dict[str, Any]]:
    """Builds status badges and priority highlights matching the Yellow/Black/White theme."""
    rules = []
    col_map = {name: idx for idx, name in enumerate(columns)}

    def make_rule(col_idx: int, text_val: str, bg_color: dict, fg_color: dict, bold: bool = True):
        return {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": min(row_count, 1000),
                        "startColumnIndex": col_idx,
                        "endColumnIndex": col_idx + 1
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": text_val}]
                        },
                        "format": {
                            "backgroundColor": bg_color,
                            "textFormat": {
                                "foregroundColor": fg_color,
                                "bold": bold
                            }
                        }
                    }
                },
                "index": 0
            }
        }

    # 1. qualification_state
    if "qualification_state" in col_map:
        idx = col_map["qualification_state"]
        rules.append(make_rule(idx, "OUTREACH_READY", COLOR_SOFT_YELLOW, COLOR_YELLOW_TEXT_DARK, bold=True))
        rules.append(make_rule(idx, "MANUAL_REVIEW", COLOR_AMBER_YELLOW, COLOR_YELLOW_TEXT_DARK, bold=True))
        rules.append(make_rule(idx, "DISQUALIFIED", COLOR_LIGHT_GRAY, COLOR_GRAY_TEXT, bold=False))

    # 2. outreach_status
    if "outreach_status" in col_map:
        idx = col_map["outreach_status"]
        rules.append(make_rule(idx, "SENT", COLOR_VIVID_YELLOW, {"red": 0.0, "green": 0.0, "blue": 0.0}, bold=True))
        rules.append(make_rule(idx, "PENDING", COLOR_PALE_CREAM, COLOR_YELLOW_TEXT_DARK, bold=False))
        rules.append(make_rule(idx, "DO_NOT_CONTACT", COLOR_BLACK, COLOR_YELLOW, bold=True))
        rules.append(make_rule(idx, "FAILED", COLOR_SOFT_RED, COLOR_RED_TEXT, bold=True))

    # 3. priority
    if "priority" in col_map:
        idx = col_map["priority"]
        rules.append(make_rule(idx, "HIGH", COLOR_BLACK, COLOR_YELLOW, bold=True))
        rules.append(make_rule(idx, "MEDIUM", COLOR_SOFT_YELLOW, COLOR_YELLOW_TEXT_DARK, bold=False))
        rules.append(make_rule(idx, "LOW", COLOR_LIGHT_GRAY, COLOR_GRAY_TEXT, bold=False))

    # 4. contactability_status
    if "contactability_status" in col_map:
        idx = col_map["contactability_status"]
        rules.append(make_rule(idx, "CONTACTABLE", COLOR_SOFT_YELLOW, COLOR_YELLOW_TEXT_DARK, bold=True))
        rules.append(make_rule(idx, "NOT_CONTACTABLE", COLOR_LIGHT_GRAY, COLOR_GRAY_TEXT, bold=False))

    # 5. response_status
    if "response_status" in col_map:
        idx = col_map["response_status"]
        rules.append(make_rule(idx, "REPLIED", COLOR_VIVID_YELLOW, {"red": 0.0, "green": 0.0, "blue": 0.0}, bold=True))
        rules.append(make_rule(idx, "AWAITING_RESPONSE", COLOR_PALE_CREAM, COLOR_YELLOW_TEXT_DARK, bold=False))
        rules.append(make_rule(idx, "NO_REPLY", COLOR_LIGHT_GRAY, COLOR_GRAY_TEXT, bold=False))
        rules.append(make_rule(idx, "BOUNCED", COLOR_SOFT_RED, COLOR_RED_TEXT, bold=True))

    # 6. sales_stage
    if "sales_stage" in col_map:
        idx = col_map["sales_stage"]
        rules.append(make_rule(idx, "WON", COLOR_VIVID_YELLOW, {"red": 0.0, "green": 0.0, "blue": 0.0}, bold=True))
        rules.append(make_rule(idx, "CONTACTED", COLOR_PALE_CREAM, COLOR_DARK_TEXT, bold=False))
        rules.append(make_rule(idx, "LOST", COLOR_LIGHT_GRAY, COLOR_GRAY_TEXT, bold=False))

    return rules


def apply_full_crm_theme(storage_provider=None) -> Dict[str, Any]:
    """
    Applies the modern Yellow, Black, and White theme to the entire CRM spreadsheet:
    1. Polishes OVERVIEW executive command center
    2. Styles LEADS tab
    3. Styles REVIEW_QUEUE tab
    4. Styles RESEARCH_LOG tab
    """
    gc = get_sheet_client()
    url = os.getenv("GOOGLE_SHEET_URL")
    if not url:
        raise ValueError("GOOGLE_SHEET_URL environment variable is missing.")

    sh = gc.open_by_url(url)
    print(f"[ThemeEngine] Connecting to '{sh.title}'...")

    # Ensure OVERVIEW exists and is populated
    try:
        sh.worksheet("OVERVIEW")
    except Exception:
        sh.add_worksheet(title="OVERVIEW", rows=40, cols=12, index=0)

    print("[ThemeEngine] Populating OVERVIEW command dashboard...")
    populate_overview_tab(sh)

    # Fetch current metadata for cleanups
    meta = sh.fetch_sheet_metadata()

    # Define targets
    targets = [
        {
            "name": "LEADS",
            "columns": LEADS_COLUMNS,
            "tab_color": COLOR_BLACK,
            "frozen_rows": 1,
            "frozen_cols": 2
        },
        {
            "name": "REVIEW_QUEUE",
            "columns": REVIEW_QUEUE_COLUMNS,
            "tab_color": COLOR_AMBER_YELLOW,
            "frozen_rows": 1,
            "frozen_cols": 2
        },
        {
            "name": "RESEARCH_LOG",
            "columns": RESEARCH_LOG_COLUMNS,
            "tab_color": COLOR_CHARCOAL,
            "frozen_rows": 1,
            "frozen_cols": 2
        }
    ]

    results = {}
    for target in targets:
        tab_name = target["name"]
        print(f"[ThemeEngine] Applying theme to '{tab_name}'...")
        try:
            ws = sh.worksheet(tab_name)
        except Exception as e:
            print(f"[ThemeEngine] Worksheet '{tab_name}' not found: {e}")
            continue

        sheet_id = ws.id
        row_count = ws.row_count or 1000

        # Step A: Clean up any old rules/banding
        clean_reqs = build_tab_cleanup_requests(meta, sheet_id)
        if clean_reqs:
            try:
                sh.batch_update({"requests": clean_reqs})
            except Exception as e:
                print(f"[ThemeEngine] Cleanup warning for {tab_name}: {e}")

        # Step B: Build and execute theme batch requests
        theme_reqs = build_data_tab_theme_requests(
            sheet_id=sheet_id,
            columns=target["columns"],
            row_count=row_count,
            tab_color=target["tab_color"],
            frozen_rows=target["frozen_rows"],
            frozen_cols=target["frozen_cols"]
        )

        try:
            sh.batch_update({"requests": theme_reqs})
            results[tab_name] = {"success": True, "rules_applied": len(theme_reqs)}
            print(f"[ThemeEngine] '{tab_name}' successfully styled with {len(theme_reqs)} rules.")
        except Exception as e:
            results[tab_name] = {"success": False, "error": str(e)}
            print(f"[ThemeEngine] Failed styling '{tab_name}': {e}")

    print("[ThemeEngine] Yellow, Black & White CRM theme applied successfully across all tabs.")
    return results


if __name__ == "__main__":
    apply_full_crm_theme()
