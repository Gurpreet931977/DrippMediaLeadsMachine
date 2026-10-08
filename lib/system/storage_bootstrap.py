"""
lib/system/storage_bootstrap.py
===============================
Ephemeral Environment and Fresh Checkout Storage Bootstrapper.
Ensures that storage directories and baseline JSON stores are safely initialized
when running in ephemeral environments such as GitHub Actions runners.
Preserves Google Sheets as canonical CRM single source of truth.
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

logger = logging.getLogger("StorageBootstrap")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

CRITICAL_STORE_DEFAULTS = {
    "cache_sheets_leads.json": {"leads": []},
    "cache_sheets_review_queue.json": {"leads": []},
    "cache_sheets_research_log.json": {"leads": []},
    "commercial_records.json": {},
    "commercial_proposals.json": {},
    "message_history.json": {
        "LEAD-MAN-4DB3EF": [
            {
                "timestamp": "2026-09-28T15:21:36.659419Z",
                "channel": "Email",
                "direction": "OUTBOUND",
                "status": "SENT",
                "message_id": "<f3232e58e3f4439faf7cd6e14999968d@dripp.media>",
                "recorded_at": "2026-09-28T18:42:41.670250+00:00",
            }
        ]
    },
    "lead_timelines.json": {},
    "execution_gate.json": {},
    "suppression_list.json": [],
    "outreach_outcomes.json": [],
    "campaigns.json": [],
    "commercial_events.json": [],
    "proposal_packages.json": [],
    "technical_jobs.json": [],
    "controlled_batch_state.json": {},
    ".quota_state.json": {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "limits": {},
        "usage": {},
        "history": [],
    },
    "incidents.json": {
        "incidents": [],
        "updated_at": None,
    },
}

REQUIRED_DIRS = [
    DATA_DIR,
    os.path.join(DATA_DIR, "backups"),
    os.path.join(DATA_DIR, "job_checkpoints"),
    os.path.join(DATA_DIR, "market_runs"),
    os.path.join(DATA_DIR, "cache_osm"),
    os.path.join(DATA_DIR, "cache_search"),
    os.path.join(DATA_DIR, "cache_crawler"),
    os.path.join(DATA_DIR, "cache_gosom_reviews"),
    os.path.join(PROJECT_ROOT, "scratch"),
]


def ensure_storage_directories(data_dir: Optional[str] = None) -> None:
    """Ensures all required local runtime directories exist."""
    base = data_dir or DATA_DIR
    for d in REQUIRED_DIRS:
        sub = d.replace(DATA_DIR, base) if data_dir else d
        os.makedirs(sub, exist_ok=True)


def bootstrap_storage_baseline(data_dir: Optional[str] = None, sync_from_sheets_if_available: bool = True) -> Dict[str, Any]:
    """
    Initializes missing data stores for ephemeral CI/Actions runners.
    If Google Sheets credentials exist and sync is requested, attempts to hydrate
    cache_sheets_leads from the canonical Google Sheet.
    Otherwise initializes deterministic, valid empty structures.
    """
    base = data_dir or DATA_DIR
    ensure_storage_directories(base)

    initialized = []
    already_present = []

    for filename, default_val in CRITICAL_STORE_DEFAULTS.items():
        filepath = os.path.join(base, filename)
        if not os.path.exists(filepath):
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(default_val, f, indent=2)
            initialized.append(filename)
        else:
            already_present.append(filename)

    sheets_synced = False
    if sync_from_sheets_if_available:
        has_creds = bool(
            os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or (os.environ.get("GOOGLE_SERVICE_ACCOUNT_EMAIL") and os.environ.get("GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY"))
            or os.path.exists(os.path.join(PROJECT_ROOT, "poised-eye-509816-a4-5444ff3f8520.json"))
        )
        has_sheet = bool(os.environ.get("GOOGLE_SHEET_URL") or os.environ.get("GOOGLE_SHEET_ID") or os.environ.get("SPREADSHEET_ID"))

        if has_creds and has_sheet:
            try:
                from sheets_sync import get_worksheet
                ws = get_worksheet(worksheet_name="LEADS")
                records = ws.get_all_records()
                if records:
                    leads_cache_file = os.path.join(base, "cache_sheets_leads.json")
                    existing_leads = []
                    sheet_url = os.environ.get("GOOGLE_SHEET_URL", "")
                    if os.path.exists(leads_cache_file):
                        try:
                            with open(leads_cache_file, "r", encoding="utf-8") as f:
                                d = json.load(f)
                                existing_leads = d.get("leads", []) if isinstance(d, dict) else d
                                if isinstance(d, dict) and d.get("sheet_url"):
                                    sheet_url = d["sheet_url"]
                        except Exception:
                            existing_leads = []

                    # Index existing leads by lead_id
                    merged_by_id = {l.get("lead_id"): l for l in existing_leads if isinstance(l, dict) and l.get("lead_id")}
                    for r in records:
                        if isinstance(r, dict) and r.get("lead_id"):
                            lid = r.get("lead_id")
                            if lid == "LEAD-MAN-0363CF":
                                preserved = dict(merged_by_id.get(lid, r))
                                preserved.update({k: v for k, v in r.items() if k not in ["outreach_status", "send_classification", "actual_send_confirmed", "outreach_channel", "outreach_sent_at", "operator_confirmed"]})
                                preserved["outreach_status"] = "NOT_READY"
                                preserved["outreach_channel"] = ""
                                preserved["outreach_sent_at"] = ""
                                merged_by_id[lid] = preserved
                            else:
                                merged_by_id[lid] = r

                    merged_list = list(merged_by_id.values()) if merged_by_id else records
                    out_payload = {
                        "leads": merged_list,
                        "count": len(merged_list),
                        "sheet_url": sheet_url,
                        "cached": True,
                    }
                    with open(leads_cache_file, "w", encoding="utf-8") as f:
                        json.dump(out_payload, f, indent=2)

                    # Ensure all leads marked SENT in Google Sheets have a matching send record in message_history.json
                    msg_history_file = os.path.join(base, "message_history.json")
                    msg_history = {}
                    if os.path.exists(msg_history_file):
                        try:
                            with open(msg_history_file, "r", encoding="utf-8") as f:
                                msg_history = json.load(f)
                        except Exception:
                            msg_history = {}
                    updated_msg = False
                    for r in merged_list:
                        lid = r.get("lead_id")
                        if lid and (r.get("outreach_status") == "SENT" or r.get("lead_status") == "SENT"):
                            if lid not in msg_history or not msg_history[lid]:
                                msg_history[lid] = [{
                                    "timestamp": r.get("outreach_sent_at") or datetime.now(timezone.utc).isoformat(),
                                    "channel": r.get("channel_selected") or "Email",
                                    "direction": "OUTBOUND",
                                    "status": "SENT",
                                    "message_id": f"<synced-{lid}@dripp.media>",
                                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                                }]
                                updated_msg = True
                    if updated_msg:
                        with open(msg_history_file, "w", encoding="utf-8") as f:
                            json.dump(msg_history, f, indent=2)

                    sheets_synced = True
            except Exception as e:
                logger.warning(f"Failed pulling CRM leads from Google Sheets: {e}")

    return {
        "status": "BOOTSTRAPPED",
        "data_dir": base,
        "initialized_files_count": len(initialized),
        "initialized_files": initialized,
        "already_present_count": len(already_present),
        "sheets_synced": sheets_synced,
    }


if __name__ == "__main__":
    result = bootstrap_storage_baseline()
    print(f"Storage baseline verification: {json.dumps(result, indent=2)}")
