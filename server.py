import os
import json
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from dotenv import load_dotenv

from lib.pipeline import LeadGenerationPipeline
from lib.sheets.google_sheets import GoogleSheetsStorageProvider
from lib.discovery.hybrid import HybridDiscoveryEngine, DiscoveryMode

from lib.system.pipeline_run_manager import run_manager, PipelineRunStatus

load_dotenv()

# Automatically recover any interrupted runs upon server startup
run_manager.recover_interrupted_runs()

app = FastAPI(title="Dripp Media Lead Intelligence Suite")

# Configure CORS for public Vercel frontend, local dev, and custom origins
allowed_origins = [
    "https://dripp-media-leads-machine.vercel.app",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
env_cors = os.getenv("CORS_ORIGINS", "").strip()
if env_cors:
    for o in env_cors.split(","):
        if o.strip() and o.strip() not in allowed_origins:
            allowed_origins.append(o.strip())

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"^https://.*\.vercel\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_private_network=True,
)

def check_operator_authorization(request: Request) -> bool:
    """
    Validates Operator API Key if configured in server environment (OPERATOR_API_KEY).
    If OPERATOR_API_KEY is not configured or blank, permits operation in local/dev mode.
    """
    expected = os.getenv("OPERATOR_API_KEY", "").strip()
    if not expected:
        return True
    auth_hdr = request.headers.get("Authorization", "").strip()
    if auth_hdr.startswith("Bearer ") and auth_hdr[7:].strip() == expected:
        return True
    if request.headers.get("X-Operator-Key", "").strip() == expected:
        return True
    if request.query_params.get("api_key", "").strip() == expected:
        return True
    return False

# Global in-memory state for active run & real-time logs (mirrors run_manager)
active_run_state = {
    "run_id": None,
    "status": "IDLE",
    "is_running": False,
    "dry_run": False,
    "progress_logs": [],
    "discovery_transparency": {
        "businesses_discovered_total": 0,
        "unique_candidates": 0,
        "duplicates_merged": 0,
        "by_source": {
            "OPENSTREETMAP": 0,
            "FOURSQUARE": 0,
            "WEB_SEARCH": 0,
            "CREATOR_REFERENCES": 0,
            "APIFY": 0
        }
    },
    "current_stats": {
        "requested_qualified_leads": 10,
        "businesses_researched": 0,
        "country_matches": 0,
        "country_mismatches": 0,
        "country_unclear": 0,
        "website_exists": 0,
        "no_website_confirmed": 0,
        "website_unclear": 0,
        "website_broken": 0,
        "not_qualified": 0,
        "duplicates": 0,
        "qualified_leads": 0,
        "high_priority": 0,
        "medium_priority": 0,
        "low_priority": 0,
        "saved_to_leads": 0,
        "saved_to_research_log": 0
    },
    "last_error": None
}

class SearchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    country: str = "United Kingdom"
    cities: List[str] = ["Manchester"]
    city: Optional[str] = None
    industry: str = "Restaurants"
    qualified_leads_needed: int = Field(default=10, alias="limit")
    batch_size: int = Field(default=10, ge=1, le=25)
    max_research_multiplier: int = 5
    discovery_mode: Optional[str] = "FREE_LOCAL"
    apify_enabled: Optional[bool] = False
    dry_run: Optional[bool] = False

def run_pipeline_task(
    run_id: str,
    country: str,
    cities: List[str],
    industry: str,
    qualified_leads_needed: int,
    batch_size: int = 10,
    max_research_multiplier: int = 5,
    discovery_mode: str = "HYBRID",
    apify_enabled: bool = False,
    dry_run: bool = False
):
    global active_run_state
    run_manager.start_run(run_id)
    active_run_state["run_id"] = run_id
    active_run_state["status"] = PipelineRunStatus.RUNNING.value
    active_run_state["is_running"] = True
    active_run_state["dry_run"] = dry_run
    active_run_state["progress_logs"] = [
        f"[START] Pipeline {run_id} running in {'SAFE DRY-RUN (Preflight)' if dry_run else 'LIVE CRM INGESTION'} mode..."
    ]
    active_run_state["last_error"] = None
    active_run_state["discovery_transparency"] = {
        "businesses_discovered_total": 0,
        "unique_candidates": 0,
        "duplicates_merged": 0,
        "by_source": {
            "OPENSTREETMAP": 0,
            "FOURSQUARE": 0,
            "WEB_SEARCH": 0,
            "CREATOR_REFERENCES": 0,
            "APIFY": 0
        }
    }
    active_run_state["current_stats"] = {
        "requested_qualified_leads": qualified_leads_needed,
        "businesses_researched": 0,
        "country_matches": 0,
        "country_mismatches": 0,
        "country_unclear": 0,
        "website_exists": 0,
        "no_website_confirmed": 0,
        "website_unclear": 0,
        "website_broken": 0,
        "not_qualified": 0,
        "duplicates": 0,
        "qualified_leads": 0,
        "high_priority": 0,
        "medium_priority": 0,
        "low_priority": 0,
        "saved_to_leads": 0,
        "saved_to_research_log": 0
    }

    def log_cb(msg: str, data: Dict[str, Any]):
        active_run_state["progress_logs"].append(msg)
        if len(active_run_state["progress_logs"]) > 300:
            active_run_state["progress_logs"].pop(0)
        run_manager.append_log(run_id, msg, data.get("stats") if data else None)

    try:
        discovery_engine = HybridDiscoveryEngine(mode=discovery_mode, apify_enabled=apify_enabled)
        # In dry_run mode, shadow_mode=True bypasses Google Sheets writes completely
        pipeline = LeadGenerationPipeline(discovery_provider=discovery_engine, shadow_mode=dry_run)
        result = pipeline.run(
            country=country,
            cities=cities,
            industry=industry,
            requested_qualified_leads=qualified_leads_needed,
            batch_size=batch_size,
            max_research_multiplier=max_research_multiplier,
            log_callback=log_cb
        )
        stats = result.get("stats", {})
        active_run_state["current_stats"] = stats
        active_run_state["discovery_transparency"] = result.get("discovery_transparency", {})
        active_run_state["status"] = PipelineRunStatus.COMPLETED.value
        run_manager.finish_run(run_id, result)
    except Exception as e:
        active_run_state["last_error"] = str(e)
        active_run_state["status"] = PipelineRunStatus.FAILED.value
        active_run_state["progress_logs"].append(f"FATAL ERROR: {str(e)}")
        run_manager.fail_run(run_id, str(e))
    finally:
        active_run_state["is_running"] = False

@app.get("/api/discovery/status")
async def get_discovery_status():
    """Returns connectivity and configuration status of all discovery providers."""
    engine = HybridDiscoveryEngine()
    return engine.health_check()

@app.post("/api/search")
async def start_search(req: SearchRequest, background_tasks: BackgroundTasks, request: Request = None):
    if request and not check_operator_authorization(request):
        return JSONResponse(
            {"status": "error", "message": "Unauthorized: Invalid or missing Operator API Key."},
            status_code=401
        )
    latest = run_manager.get_latest_run()
    if active_run_state.get("is_running") or (latest and latest.get("is_running")):
        return JSONResponse(
            {
                "status": "error",
                "message": "A lead generation task is already in progress.",
                "active_run_id": active_run_state.get("run_id") or (latest.get("run_id") if latest else None)
            },
            status_code=409
        )

    # 1. Market & Geographic Boundary Validation (Phase 12.0 Section 4)
    raw_cities = [req.city] if (req.city and req.city.strip()) else (req.cities or [])
    clean_cities = [c.strip().title() for c in raw_cities if c and c.strip()]
    req.cities = clean_cities
    if not clean_cities or any(c.lower() != "manchester" for c in clean_cities):
        return JSONResponse(
            {
                "status": "error",
                "message": "Unsupported market. Only Manchester, UK (#162378) is authorized for autonomous acquisition in Phase 12.0. Secondary markets (Leeds, Birmingham, London) are locked for future expansion."
            },
            status_code=400
        )

    clean_country = (req.country or "").strip().lower()
    if clean_country not in ["united kingdom", "uk", "great britain", "england"]:
        return JSONResponse(
            {
                "status": "error",
                "message": "Unsupported jurisdiction. Operating boundary is restricted to United Kingdom (OSM #162378)."
            },
            status_code=400
        )

    # 2. Provider Mode Validation (Phase 12.0 Section 2)
    mode = (req.discovery_mode or "FREE_LOCAL").strip().upper()
    if "PLACES" in mode or "GOOGLE_PLACES" in mode:
        return JSONResponse(
            {
                "status": "error",
                "message": "Google Places API is strictly disallowed by Phase 12.0 architecture constraints."
            },
            status_code=400
        )
    if mode == "APIFY":
        return JSONResponse(
            {
                "status": "error",
                "message": "Apify discovery engine is currently unavailable: Apify credits are exhausted. Select OpenStreetMap (FREE_LOCAL) or HYBRID."
            },
            status_code=400
        )
    if mode not in ["FREE_LOCAL", "HYBRID", "OSM"]:
        return JSONResponse(
            {
                "status": "error",
                "message": f"Unsupported discovery mode '{req.discovery_mode}'. Permitted modes: FREE_LOCAL, HYBRID, OSM."
            },
            status_code=400
        )
    if mode == "OSM":
        mode = "FREE_LOCAL"

    leads_needed = min(max(1, req.qualified_leads_needed or 10), 50)
    bounded_batch = min(max(1, req.batch_size), 25)
    multiplier = min(max(1, req.max_research_multiplier), 10)

    # Enforce zero credit consumption on exhausted providers
    enforced_apify = False

    run_record = run_manager.create_run(
        country="United Kingdom",
        cities=["Manchester"],
        industry=req.industry or "Restaurants",
        qualified_leads_needed=leads_needed,
        batch_size=bounded_batch,
        max_research_multiplier=multiplier,
        discovery_mode=mode,
        apify_enabled=enforced_apify,
        dry_run=bool(req.dry_run)
    )

    run_id = run_record["run_id"]
    active_run_state["run_id"] = run_id
    active_run_state["is_running"] = True
    active_run_state["status"] = PipelineRunStatus.QUEUED.value

    background_tasks.add_task(
        run_pipeline_task,
        run_id=run_id,
        country="United Kingdom",
        cities=["Manchester"],
        industry=req.industry or "Restaurants",
        qualified_leads_needed=leads_needed,
        batch_size=bounded_batch,
        max_research_multiplier=multiplier,
        discovery_mode=mode,
        apify_enabled=enforced_apify,
        dry_run=bool(req.dry_run)
    )
    return {
        "status": "started",
        "run_id": run_id,
        "lifecycle": PipelineRunStatus.QUEUED.value,
        "dry_run": bool(req.dry_run),
        "batch_size": bounded_batch,
        "message": f"Lead engine started in {mode} mode for {leads_needed} qualified leads in Manchester, UK ({'DRY-RUN' if req.dry_run else 'LIVE'})."
    }

@app.get("/api/status")
async def get_status():
    latest = run_manager.get_latest_run()
    if latest:
        resp = dict(latest)
        resp["is_running"] = active_run_state.get("is_running", latest.get("is_running", False))
        if active_run_state.get("progress_logs"):
            resp["progress_logs"] = active_run_state["progress_logs"]
        if active_run_state.get("current_stats"):
            resp["current_stats"] = active_run_state["current_stats"]
        return resp
    return active_run_state

@app.post("/api/search/cancel")
async def cancel_search(request: Request = None):
    if request and not check_operator_authorization(request):
        return JSONResponse(
            {"status": "error", "message": "Unauthorized: Invalid or missing Operator API Key."},
            status_code=401
        )
    latest = run_manager.get_latest_run()
    if not latest or not latest.get("is_running"):
        return JSONResponse({"status": "error", "message": "No active lead generation task to cancel."}, status_code=400)
    cancelled_rec = run_manager.cancel_run(latest["run_id"])
    active_run_state["is_running"] = False
    active_run_state["status"] = PipelineRunStatus.CANCELLED.value
    return {"status": "ok", "message": f"Run {latest['run_id']} marked CANCELLED.", "run": cancelled_rec}

@app.get("/api/search/runs")
async def list_search_runs(limit: int = 50):
    return {"runs": run_manager.list_runs(limit=limit)}

@app.get("/api/search/runs/{run_id}")
async def get_search_run(run_id: str):
    rec = run_manager.get_run(run_id)
    if not rec:
        return JSONResponse({"status": "error", "message": f"Run {run_id} not found"}, status_code=404)
    return rec

@app.get("/api/stream")
async def stream_logs(request: Request):
    """Server-Sent Events endpoint for real-time progress streaming."""
    async def event_generator():
        last_idx = 0
        while True:
            if await request.is_disconnected():
                break

            current_logs = active_run_state["progress_logs"]
            if len(current_logs) > last_idx:
                new_logs = current_logs[last_idx:]
                last_idx = len(current_logs)
                payload = {
                    "is_running": active_run_state["is_running"],
                    "stats": active_run_state["current_stats"],
                    "logs": new_logs
                }
                yield f"data: {json.dumps(payload)}\n\n"
            else:
                payload = {
                    "is_running": active_run_state["is_running"],
                    "stats": active_run_state["current_stats"],
                    "logs": []
                }
                yield f"data: {json.dumps(payload)}\n\n"

            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/api/leads")
async def get_leads(request: Request = None):
    """Fetches all qualified leads directly from the LEADS Google Sheet tab."""
    try:
        storage = GoogleSheetsStorageProvider()
        leads = storage.fetch_all_leads()
        return {
            "status": "ok",
            "source": "GOOGLE_SHEETS",
            "tab": "LEADS",
            "spreadsheet_id": os.getenv("GOOGLE_SHEET_ID", "1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc"),
            "leads": leads,
            "count": len(leads),
            "sheet_url": storage.sheet_url,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "source": "GOOGLE_SHEETS",
            "message": str(e),
            "leads": [],
            "count": 0
        }, status_code=500)

@app.get("/api/review-queue")
async def get_review_queue(request: Request = None):
    """Fetches manual review candidates directly from the REVIEW_QUEUE Google Sheet tab."""
    try:
        storage = GoogleSheetsStorageProvider()
        queue = storage.fetch_review_queue()
        return {
            "status": "ok",
            "source": "GOOGLE_SHEETS",
            "tab": "REVIEW_QUEUE",
            "spreadsheet_id": os.getenv("GOOGLE_SHEET_ID", "1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc"),
            "queue": queue,
            "count": len(queue),
            "sheet_url": storage.sheet_url,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "source": "GOOGLE_SHEETS",
            "message": str(e),
            "queue": [],
            "count": 0
        }, status_code=500)

@app.get("/api/research-log")
async def get_research_log(request: Request = None):
    """Fetches all entries directly from the RESEARCH_LOG Google Sheet tab."""
    try:
        storage = GoogleSheetsStorageProvider()
        entries = storage.fetch_research_log()
        return {
            "status": "ok",
            "source": "GOOGLE_SHEETS",
            "tab": "RESEARCH_LOG",
            "spreadsheet_id": os.getenv("GOOGLE_SHEET_ID", "1Inan5Laj_CsxraX0JpByJ3466QNbcGO-B6xY5r4fwyc"),
            "entries": entries,
            "count": len(entries),
            "sheet_url": storage.sheet_url,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "source": "GOOGLE_SHEETS",
            "message": str(e),
            "entries": [],
            "count": 0
        }, status_code=500)


# ==============================================================================
# PHASE 8.2: MANUAL OUTREACH EXECUTION CONTROL ENDPOINTS
# ==============================================================================
from lib.outreach.manual_outreach_controller import (
    get_manual_outreach_queue,
    log_audit_event,
    confirm_manual_send,
    get_audit_log,
)

class ManualOutreachActionRequest(BaseModel):
    lead_id: str
    action: str
    details: Optional[Dict[str, Any]] = None

class ConfirmSendRequest(BaseModel):
    lead_id: str
    operator_confirmed: bool
    notes: Optional[str] = ""

@app.get("/api/manual-outreach/queue")
async def get_manual_queue():
    """
    Returns the operator manual outreach queue.
    Only OUTREACH_READY leads with MANUAL outreach mode are eligible.
    Logs QUEUE_VIEWED event in audit log.
    """
    try:
        queue_data = get_manual_outreach_queue(log_view=True)
        return queue_data
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/manual-outreach/action")
async def record_manual_action(req: ManualOutreachActionRequest):
    """
    Records operator manual actions: DRAFT_COPIED, INSTAGRAM_OPENED, etc.
    """
    try:
        entry = log_audit_event(
            action=req.action,
            lead_id=req.lead_id,
            details=req.details
        )
        return {"status": "ok", "entry": entry}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/manual-outreach/confirm-send")
async def handle_confirm_send(req: ConfirmSendRequest):
    """
    Transitions lead to SENT strictly upon explicit operator confirmation.
    Appends typed manual record to message history and logs SEND_CONFIRMED.
    Enforces duplicate-send blocking.
    """
    try:
        res = confirm_manual_send(
            lead_id=req.lead_id,
            operator_confirmed=req.operator_confirmed,
            notes=req.notes or "",
            sync_sheets=True
        )
        if not res.get("success"):
            return JSONResponse(res, status_code=400)
        return res
    except ValueError as ve:
        return JSONResponse({"status": "error", "message": str(ve)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/manual-outreach/audit-log")
async def read_manual_audit_log():
    """
    Returns full audit trail for manual outreach operations.
    """
    try:
        records = get_audit_log()
        return {"records": records, "count": len(records)}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ==============================================================================
# PHASE 8.7: THREE-LEAD OUTREACH BATCH PREPARATION ENDPOINT
# ==============================================================================
from lib.outreach.phase_8_7_outreach_batch_engine import Phase87OutreachBatchEngine

@app.get("/api/manual-outreach/batch-8-7")
async def get_outreach_batch_8_7():
    """
    Returns the Phase 8.7 active outreach batch prepared for operator review.
    Shows 3 active outreach leads (0 automated, 3 manual) with approved drafts (WEBSITE_001).
    """
    try:
        batch_path = os.path.join(os.path.dirname(__file__), "data", "phase_8_7_outreach_batch.json")
        if not os.path.exists(batch_path):
            engine = Phase87OutreachBatchEngine()
            batch_payload = engine.generate_batch()
        else:
            with open(batch_path, "r", encoding="utf-8") as f:
                batch_payload = json.load(f)
        return batch_payload
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ==============================================================================
# PHASE 8.8: REAL MANUAL OUTREACH EXECUTION & OUTCOME TRACKING ENDPOINTS
# ==============================================================================
from lib.outreach.phase_8_8_outreach_executor import Phase88OutreachExecutor
from pydantic import BaseModel

class OutreachActionRequest(BaseModel):
    lead_id: str
    channel: str
    action_type: str  # MESSAGE_SENT, CALL_CONNECTED, CALL_UNANSWERED, SKIP
    operator_confirmed: bool
    notes: Optional[str] = ""
    call_connected: Optional[bool] = None

class OutreachResponseRequest(BaseModel):
    lead_id: str
    outcome: str
    reply_date: Optional[str] = None
    reply_channel: Optional[str] = None
    reply_summary: Optional[str] = None
    interest_level: Optional[str] = None
    next_action: Optional[str] = None
    notes: Optional[str] = ""

class OutreachResetRequest(BaseModel):
    lead_id: str
    admin_confirmed: bool

@app.get("/api/manual-outreach/phase-8-8/queue")
async def get_phase_8_8_queue():
    try:
        executor = Phase88OutreachExecutor()
        queue = executor.get_active_queue()
        return {"status": "ok", "active_batch": len(queue), "queue": queue}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/manual-outreach/phase-8-8/daily-view")
async def get_phase_8_8_daily_view():
    try:
        executor = Phase88OutreachExecutor()
        daily_view = executor.get_daily_outreach_view()
        return {"status": "ok", **daily_view}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/manual-outreach/phase-8-8/record-action")
async def record_phase_8_8_action(req: OutreachActionRequest):
    try:
        executor = Phase88OutreachExecutor()
        res = executor.record_outreach_action(
            lead_id=req.lead_id,
            channel=req.channel,
            action_type=req.action_type,
            operator_confirmed=req.operator_confirmed,
            notes=req.notes or "",
            call_connected=req.call_connected,
        )
        status_code = 200 if res.get("success") else 400
        return JSONResponse(res, status_code=status_code)
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/manual-outreach/phase-8-8/record-response")
async def record_phase_8_8_response(req: OutreachResponseRequest):
    try:
        executor = Phase88OutreachExecutor()
        res = executor.record_response(
            lead_id=req.lead_id,
            outcome=req.outcome,
            reply_date=req.reply_date,
            reply_channel=req.reply_channel,
            reply_summary=req.reply_summary,
            interest_level=req.interest_level,
            next_action=req.next_action,
            notes=req.notes or "",
        )
        return JSONResponse(res, status_code=200)
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/manual-outreach/phase-8-8/reset")
async def reset_phase_8_8_outreach(req: OutreachResetRequest):
    try:
        executor = Phase88OutreachExecutor()
        res = executor.reset_lead_outreach(lead_id=req.lead_id, admin_confirmed=req.admin_confirmed)
        status_code = 200 if res.get("success") else 400
        return JSONResponse(res, status_code=status_code)
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/manual-outreach/phase-8-8/analytics")
async def get_phase_8_8_analytics():
    try:
        executor = Phase88OutreachExecutor()
        analytics = executor.generate_analytics()
        return {"status": "ok", **analytics}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ==============================================================================
# PHASE 8.9: FULL OUTREACH PRODUCT ENGINE ENDPOINTS
# ==============================================================================
from lib.outreach.phase_8_9_product_engine import OutreachProductEngine

_product_engine_instance = None

def get_product_engine() -> OutreachProductEngine:
    global _product_engine_instance
    if _product_engine_instance is None:
        crm_path = os.path.join(os.path.dirname(__file__), "data", "cache_sheets_leads.json")
        camp_path = os.path.join(os.path.dirname(__file__), "data", "campaigns.json")
        supp_path = os.path.join(os.path.dirname(__file__), "data", "suppression_list.json")
        time_path = os.path.join(os.path.dirname(__file__), "data", "lead_timelines.json")
        _product_engine_instance = OutreachProductEngine(
            crm_leads_path=crm_path,
            campaigns_path=camp_path,
            suppression_path=supp_path,
            timeline_path=time_path,
        )
    return _product_engine_instance

class CreateCampaignRequest(BaseModel):
    campaign_name: str
    target_filter: Dict[str, Any]
    offer: str
    template_version: Optional[str] = "WEBSITE_001"
    channels: Optional[List[str]] = None
    daily_limit: Optional[int] = 10
    schedule: Optional[Dict[str, Any]] = None
    outreach_mode: Optional[str] = "SANDBOX"

class ApproveCampaignRequest(BaseModel):
    operator_confirmed: bool
    confirmation_statement: str

class ScheduleCampaignRequest(BaseModel):
    schedule_time: Optional[str] = None

class ExecuteBatchRequest(BaseModel):
    is_sandbox: Optional[bool] = True

class RecordProductResponseRequest(BaseModel):
    lead_id: str
    response_stage: str
    evidence_summary: Optional[str] = None
    campaign_id: Optional[str] = ""
    channel: Optional[str] = ""

@app.post("/api/outreach/phase-8-9/campaigns")
async def create_phase_8_9_campaign(req: CreateCampaignRequest):
    try:
        engine = get_product_engine()
        camp = engine.create_campaign(
            campaign_name=req.campaign_name,
            target_filter=req.target_filter,
            offer=req.offer,
            template_version=req.template_version or "WEBSITE_001",
            channels=req.channels,
            daily_limit=req.daily_limit or 10,
            schedule=req.schedule,
            outreach_mode=req.outreach_mode or "SANDBOX",
        )
        return {"status": "ok", "campaign": camp.to_dict()}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

@app.get("/api/outreach/phase-8-9/campaigns/{campaign_id}/preview")
async def preview_phase_8_9_campaign(campaign_id: str):
    try:
        engine = get_product_engine()
        preview = engine.preview_campaign(campaign_id)
        return {"status": "ok", "preview": preview}
    except KeyError as ke:
        return JSONResponse({"status": "error", "message": str(ke)}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/outreach/phase-8-9/campaigns/{campaign_id}/approve")
async def approve_phase_8_9_campaign(campaign_id: str, req: ApproveCampaignRequest):
    try:
        engine = get_product_engine()
        res = engine.approve_campaign(
            campaign_id=campaign_id,
            operator_confirmed=req.operator_confirmed,
            confirmation_statement=req.confirmation_statement,
        )
        return {"status": "ok", **res}
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/outreach/phase-8-9/campaigns/{campaign_id}/schedule")
async def schedule_phase_8_9_campaign(campaign_id: str, req: ScheduleCampaignRequest):
    try:
        engine = get_product_engine()
        res = engine.schedule_campaign(campaign_id=campaign_id, schedule_time=req.schedule_time)
        return {"status": "ok", **res}
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/outreach/phase-8-9/campaigns/{campaign_id}/execute")
async def execute_phase_8_9_batch(campaign_id: str, req: ExecuteBatchRequest):
    try:
        engine = get_product_engine()
        res = engine.execute_campaign_batch(campaign_id=campaign_id, is_sandbox=req.is_sandbox)
        return {"status": "ok", **res}
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/outreach/phase-8-9/campaigns/{campaign_id}/pause")
async def pause_phase_8_9_campaign(campaign_id: str):
    try:
        engine = get_product_engine()
        res = engine.pause_campaign(campaign_id=campaign_id)
        return {"status": "ok", **res}
    except KeyError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/outreach/phase-8-9/campaigns/{campaign_id}/resume")
async def resume_phase_8_9_campaign(campaign_id: str):
    try:
        engine = get_product_engine()
        res = engine.resume_campaign(campaign_id=campaign_id)
        return {"status": "ok", **res}
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/outreach/phase-8-9/campaigns/{campaign_id}/cancel")
async def cancel_phase_8_9_campaign(campaign_id: str):
    try:
        engine = get_product_engine()
        res = engine.cancel_campaign(campaign_id=campaign_id)
        return {"status": "ok", **res}
    except KeyError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/outreach/phase-8-9/campaigns/{campaign_id}/analytics")
async def get_phase_8_9_campaign_analytics(campaign_id: str):
    try:
        engine = get_product_engine()
        analytics = engine.get_campaign_analytics(campaign_id=campaign_id)
        return {"status": "ok", **analytics}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/outreach/phase-8-9/campaigns/{campaign_id}/channel-analytics")
async def get_phase_8_9_channel_analytics(campaign_id: str):
    try:
        engine = get_product_engine()
        analytics = engine.get_channel_analytics(campaign_id=campaign_id)
        return {"status": "ok", "channel_breakdown": analytics}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/outreach/phase-8-9/leads/{lead_id}/timeline")
async def get_phase_8_9_lead_timeline(lead_id: str):
    try:
        engine = get_product_engine()
        timeline = engine.get_lead_timeline(lead_id=lead_id)
        return {"status": "ok", "lead_id": lead_id, "timeline": timeline}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/outreach/phase-8-9/response")
async def record_phase_8_9_response(req: RecordProductResponseRequest):
    try:
        engine = get_product_engine()
        res = engine.record_response(
            lead_id=req.lead_id,
            response_stage=req.response_stage,
            evidence_summary=req.evidence_summary,
            campaign_id=req.campaign_id or "",
            channel=req.channel or "",
        )
        return {"status": "ok", **res}
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

# =========================================================================
# Phase 9.0 Production Scale Engine & Multi-Market Acquisition Endpoints
# =========================================================================

@app.get("/api/production/markets")
async def get_production_markets():
    try:
        from lib.production.market_config import MarketRegistry
        markets = MarketRegistry.get_all_markets()
        return {
            "status": "ok",
            "markets": [m.to_dict() for m in markets]
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/production/runs/latest")
async def get_latest_production_run():
    try:
        run_file = os.path.join(os.path.dirname(__file__), "data", "phase_9_0_market_run.json")
        if not os.path.exists(run_file):
            return JSONResponse({"status": "error", "message": "No production run found"}, status_code=404)
        with open(run_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"status": "ok", **data}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/production/scorecard")
async def get_production_scorecard():
    try:
        run_file = os.path.join(os.path.dirname(__file__), "data", "phase_9_0_market_run.json")
        if not os.path.exists(run_file):
            return JSONResponse({"status": "error", "message": "No production run found"}, status_code=404)
        with open(run_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "status": "ok",
            "market_id": data.get("market_id"),
            "scorecard": data.get("scorecard", {}),
            "stats": data.get("stats", {}),
            "quota_usage": data.get("quota_usage", {})
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

# =========================================================================
# Phase 9.3 Qualified Lead Contactability & Activation Queue Endpoints
# =========================================================================

@app.get("/api/outreach/activation-queue")
async def get_activation_queue(request: Request, include_all: bool = False):
    """
    Returns only leads satisfying qualification_state == OUTREACH_READY with their
    LeadActivationProfile, verified channels, recommended channel, and blockers.
    Does not include RESEARCH_ONLY, MANUAL_REVIEW, or EXCLUDED unless explicitly requested.
    """
    try:
        from lib.enrichment.contactability_enrichment import ContactabilityEnrichmentEngine
        
        run_file = os.path.join(os.path.dirname(__file__), "data", "phase_9_3_contactability_run.json")
        if os.path.exists(run_file):
            with open(run_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            queue = data.get("ACTIVATION_QUEUE", [])
            if not include_all:
                queue = [p for p in queue if p.get("qualification_state") == "OUTREACH_READY"]
            return {
                "status": "ok",
                "run_id": data.get("RUN_ID"),
                "total_outreach_ready": data.get("INPUT_OUTREACH_READY"),
                "activation_ready_count": data.get("ACTIVATION_READY"),
                "activation_blocked_count": data.get("ACTIVATION_BLOCKED"),
                "queue": queue
            }

        engine = ContactabilityEnrichmentEngine(max_search_calls=20)
        leads_cache_file = os.path.join(os.path.dirname(__file__), "data", "cache_sheets_leads.json")
        existing_leads = []
        if os.path.exists(leads_cache_file):
            with open(leads_cache_file, "r", encoding="utf-8") as f:
                d = json.load(f)
                existing_leads = d.get("leads", d) if isinstance(d, dict) else d

        promotions = []
        phase_9_2_file = os.path.join(os.path.dirname(__file__), "data", "phase_9_2_evidence_recovery_run.json")
        if os.path.exists(phase_9_2_file):
            with open(phase_9_2_file, "r", encoding="utf-8") as f:
                d92 = json.load(f)
                promotions = [c for c in d92.get("CANDIDATES", []) if c.get("qualification_state") == "OUTREACH_READY"]

        result = engine.enrich_qualified_cohort(existing_leads, new_promotions=promotions)
        queue = result.get("ACTIVATION_QUEUE", [])
        if not include_all:
            queue = [p for p in queue if p.get("qualification_state") == "OUTREACH_READY"]

        return {
            "status": "ok",
            "run_id": result.get("RUN_ID"),
            "total_outreach_ready": result.get("INPUT_OUTREACH_READY"),
            "activation_ready_count": result.get("ACTIVATION_READY"),
            "activation_blocked_count": result.get("ACTIVATION_BLOCKED"),
            "queue": queue
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# =========================================================================
# Phase 9.4 Controlled Outreach Execution & Operator Gateway Endpoints
# =========================================================================

@app.get("/api/outreach/operator-preview/{lead_id}")
async def get_operator_preview(lead_id: str):
    """
    Returns the comprehensive pre-contact inspection payload.
    Emits MESSAGE_PREVIEWED in lead timeline. Does NOT trigger any outreach send.
    """
    try:
        from lib.outreach.phase_9_4_operator_gateway import OperatorSendGateway
        gateway = OperatorSendGateway()
        preview = gateway.generate_operator_preview(lead_id)
        return {"status": "ok", "preview": preview}
    except KeyError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/outreach/operator-action/call")
async def execute_operator_phone_call(request: Request):
    """
    Executes a manual phone outreach action with explicit outcome recording.
    Requires operator_confirmed == True. Never fakes a call.
    Blocked if Travel Mode is active or commercial actions are locked.
    """
    try:
        from lib.system.system_config import SystemConfig, CommercialActionForbiddenError
        try:
            SystemConfig.assert_commercial_actions_allowed("execute_phone_call")
        except CommercialActionForbiddenError as cfe:
            return JSONResponse({"status": "blocked", "error_type": "COMMERCIAL_ACTIONS_LOCKED", "message": str(cfe), "detail": f"Commercial action blocked: {cfe}"}, status_code=403)

        body = await request.json()
        lead_id = body.get("lead_id")
        operator_confirmed = body.get("operator_confirmed", False)
        outcome = body.get("outcome", "")
        notes = body.get("notes", "")
        test_mode = body.get("test_mode", False)

        from lib.outreach.phase_9_4_operator_gateway import OperatorSendGateway
        gateway = OperatorSendGateway()
        res = gateway.execute_manual_phone_action(
            lead_id=lead_id,
            operator_confirmed=operator_confirmed,
            outcome=outcome,
            notes=notes,
            test_mode=test_mode
        )
        if not res.get("success"):
            return JSONResponse(res, status_code=400)
        return res
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/outreach/operator-action/social")
async def execute_operator_social_action(request: Request):
    """
    Executes manual social outreach actions (OPEN_PROFILE, COPY_MESSAGE, CONFIRM_SENT).
    Requires explicit operator confirmation for CONFIRM_SENT before status becomes SENT.
    """
    try:
        body = await request.json()
        lead_id = body.get("lead_id")
        channel = body.get("channel", "INSTAGRAM")
        action = body.get("action", "")
        operator_confirmed = body.get("operator_confirmed", False)
        notes = body.get("notes", "")
        test_mode = body.get("test_mode", False)

        from lib.outreach.phase_9_4_operator_gateway import OperatorSendGateway
        gateway = OperatorSendGateway()
        res = gateway.execute_manual_social_action(
            lead_id=lead_id,
            channel=channel,
            action=action,
            operator_confirmed=operator_confirmed,
            notes=notes,
            test_mode=test_mode
        )
        if not res.get("success"):
            return JSONResponse(res, status_code=400)
        return res
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/outreach/operator-action/response")
async def record_operator_response(request: Request):
    """
    Records verified customer response from human operator.
    """
    try:
        body = await request.json()
        lead_id = body.get("lead_id")
        response_type = body.get("response_type", "")
        notes = body.get("notes", "")
        evidence = body.get("evidence", "")
        test_mode = body.get("test_mode", False)

        from lib.outreach.phase_9_4_operator_gateway import OperatorSendGateway
        gateway = OperatorSendGateway()
        res = gateway.record_manual_response(
            lead_id=lead_id,
            response_type=response_type,
            notes=notes,
            evidence=evidence,
            test_mode=test_mode
        )
        if not res.get("success"):
            return JSONResponse(res, status_code=400)
        return res
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

# =========================================================================
# Phase 9.5 Controlled Outreach Batch & Real Outcome Analytics Endpoints
# =========================================================================

@app.get("/api/outreach/controlled-batch-analytics")
async def get_controlled_batch_analytics():
    """
    Returns disaggregated outreach metrics and real outcome analytics.
    Enforces explicit rate denominators (contact_rate, interest_rate, etc.).
    """
    try:
        from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
        executor = ControlledBatchExecutor()
        analytics = executor.get_controlled_batch_analytics()
        return {"status": "ok", "analytics": analytics}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/outreach/controlled-batch")
async def get_controlled_batch_state():
    """
    Returns current batch status and active lead information.
    """
    try:
        from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
        executor = ControlledBatchExecutor()
        current = executor.get_current_batch_lead()
        if "error" in current and current["error"] == "NO_ACTIVE_BATCH":
            # Auto-initialize or return empty state
            state = executor.init_batch()
            current = executor.get_current_batch_lead()
        return {"status": "ok", "batch": current}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/outreach/controlled-batch/init")
async def init_controlled_batch(request: Request):
    """
    Initializes a controlled outreach batch with ceiling MAX_BATCH_SIZE = 3.
    """
    try:
        body = await request.json() if (await request.body()) else {}
        lead_ids = body.get("lead_ids")
        from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
        executor = ControlledBatchExecutor()
        batch_state = executor.init_batch(lead_ids=lead_ids)
        return {"status": "ok", "batch": batch_state}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@app.post("/api/outreach/controlled-batch/preview")
async def preview_batch_lead(request: Request):
    """
    Generates preview for current lead in the controlled batch.
    """
    try:
        from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
        executor = ControlledBatchExecutor()
        res = executor.preview_current_lead()
        if not res.get("success"):
            return JSONResponse(res, status_code=400)
        return {"status": "ok", **res}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/outreach/controlled-batch/action")
async def execute_batch_action(request: Request):
    """
    Executes operator-confirmed outreach action for current batch lead.
    """
    try:
        body = await request.json()
        operator_confirmed = body.get("operator_confirmed", False)
        action = body.get("action", "CALL")
        notes = body.get("notes", "")
        test_mode = body.get("test_mode", False)

        from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
        executor = ControlledBatchExecutor()
        res = executor.execute_current_lead_action(
            operator_confirmed=operator_confirmed,
            action=action,
            notes=notes,
            test_mode=test_mode
        )
        if not res.get("success"):
            return JSONResponse(res, status_code=400)
        return {"status": "ok", **res}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/outreach/controlled-batch/outcome")
async def record_batch_outcome(request: Request):
    """
    Records operator outcome for current batch lead.
    """
    try:
        body = await request.json()
        outcome = body.get("outcome", "")
        notes = body.get("notes", "")
        callback_time = body.get("callback_time")
        test_mode = body.get("test_mode", False)

        from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
        executor = ControlledBatchExecutor()
        res = executor.record_current_lead_outcome(
            outcome=outcome,
            notes=notes,
            callback_time=callback_time,
            test_mode=test_mode
        )
        if not res.get("success"):
            return JSONResponse(res, status_code=400)
        return {"status": "ok", **res}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/outreach/controlled-batch/next")
async def advance_batch_lead():
    """
    Advances the batch pointer to the next lead.
    Requires current lead outcome to be recorded.
    """
    try:
        from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
        executor = ControlledBatchExecutor()
        res = executor.next_lead()
        if not res.get("success"):
            return JSONResponse(res, status_code=400)
        return {"status": "ok", **res}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# =============================================================================
# Phase 9.6: Outreach Performance Intelligence & Optimization Endpoints
# =============================================================================

@app.get("/api/outreach/performance")
async def get_outreach_performance():
    """
    Returns Phase 9.6 outreach performance intelligence dashboard data.
    Strictly read-only; no automated outreach actions.
    """
    try:
        from lib.analytics.outreach_performance_engine import OutreachPerformanceEngine
        engine = OutreachPerformanceEngine()
        data = engine.calculate_outreach_performance()
        return {"status": "ok", **data}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/outreach/next-best-leads")
async def get_next_best_leads_endpoint():
    """
    Returns already-qualified leads ranked by outreach_priority_score with explanations.
    Does not include unqualified leads. Read-only recommendations.
    """
    try:
        from lib.analytics.outreach_performance_engine import OutreachPerformanceEngine
        engine = OutreachPerformanceEngine()
        leads = engine.get_next_best_leads()
        queue = engine.get_next_batch_recommendations()
        return {
            "status": "ok",
            "count": len(leads),
            "leads": leads,
            "queue_recommendation": queue,
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)



# =============================================================================
# Post-9.7: Commercial Conversion Workflow Endpoints
# =============================================================================

@app.get("/api/commercial/pipeline")
async def get_commercial_pipeline():
    """
    Returns Section 12 commercial funnel analytics, counts, and conversion rates.
    Enforces sample-size warning: n < 30 is descriptive only.
    """
    try:
        from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager
        manager = CommercialPipelineManager()
        data = manager.get_commercial_pipeline_analytics()
        return {"status": "ok", **data}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/commercial/next-actions")
async def get_commercial_next_actions():
    """
    Returns Section 13 & 15 prioritized next commercial actions for human operators.
    Strictly prioritized: callback > preview > interest > recent connection > retry > qualified.
    No automatic execution.
    """
    try:
        from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager
        manager = CommercialPipelineManager()
        actions = manager.get_next_commercial_actions()
        return {"status": "ok", "count": len(actions), "actions": actions}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/commercial/dashboard")
async def get_commercial_dashboard():
    """
    Returns Section 14 dashboard payload: top summary, cards, next actions, and analytics.
    """
    try:
        from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager
        manager = CommercialPipelineManager()
        data = manager.get_commercial_dashboard_data()
        return {"status": "ok", **data}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/commercial/leads/{lead_id}")
async def get_commercial_lead(lead_id: str):
    """
    Returns full commercial record for a single lead.
    """
    try:
        from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager
        manager = CommercialPipelineManager()
        records = manager.load_commercial_records()
        if lead_id not in records:
            return JSONResponse({"status": "error", "message": f"Lead {lead_id} not found"}, status_code=404)
        return {"status": "ok", "record": records[lead_id]}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/stage")
async def update_commercial_stage_endpoint(request: Request):
    """
    Explicitly transitions a lead's commercial stage.
    Never auto-promotes stages.
    """
    try:
        body = await request.json()
        lead_id = body.get("lead_id")
        new_stage = body.get("new_stage")
        reason = body.get("reason", "Operator transition")
        notes = body.get("notes", "")
        operator = body.get("operator", "HUMAN_OPERATOR")
        operator_confirmed = body.get("operator_confirmed", True)

        from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager
        manager = CommercialPipelineManager()
        res = manager.update_commercial_stage(
            lead_id=lead_id,
            new_stage=new_stage,
            reason=reason,
            operator=operator,
            notes=notes,
            operator_confirmed=operator_confirmed,
        )
        return res
    except (ValueError, KeyError, PermissionError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/notes")
async def record_commercial_notes_endpoint(request: Request):
    """
    Saves structured commercial notes (decision maker, role, interest level, budget, timeline, objection, next step).
    """
    try:
        body = await request.json()
        lead_id = body.get("lead_id")
        notes_data = body.get("notes", {})
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager
        manager = CommercialPipelineManager()
        res = manager.record_commercial_notes(
            lead_id=lead_id,
            notes_data=notes_data,
            operator=operator,
        )
        return res
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/follow-up")
async def schedule_follow_up_endpoint(request: Request):
    """
    Schedules a callback or follow-up for a lead (e.g. Manchester Shawarma).
    auto_schedule and auto_call remain strictly False.
    """
    try:
        body = await request.json()
        lead_id = body.get("lead_id")
        follow_up_type = body.get("follow_up_type", "CALLBACK")
        scheduled_for = body.get("scheduled_for", "")
        notes = body.get("notes", "")
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager
        manager = CommercialPipelineManager()
        res = manager.schedule_follow_up(
            lead_id=lead_id,
            follow_up_type=follow_up_type,
            scheduled_for=scheduled_for,
            notes=notes,
            operator=operator,
        )
        return res
    except KeyError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/follow-up/complete")
async def complete_follow_up_endpoint(request: Request):
    """
    Marks a scheduled follow-up as completed by human operator.
    """
    try:
        body = await request.json()
        lead_id = body.get("lead_id")
        follow_up_id = body.get("follow_up_id", "LATEST")
        outcome = body.get("outcome", "CONNECTED")
        notes = body.get("notes", "")
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager
        manager = CommercialPipelineManager()
        res = manager.complete_follow_up(
            lead_id=lead_id,
            follow_up_id=follow_up_id,
            outcome=outcome,
            notes=notes,
            operator=operator,
        )
        return res
    except KeyError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/preview")
async def create_preview_endpoint(request: Request):
    """
    Creates or updates concept preview details (e.g. The Old Monkey).
    Does NOT auto-send.
    """
    try:
        body = await request.json()
        lead_id = body.get("lead_id")
        preview_url = body.get("preview_url", "")
        preview_description = body.get("preview_description", "")
        what_demonstrated = body.get("what_demonstrated", "")
        next_action = body.get("next_action", "")
        operator_notes = body.get("operator_notes", "")
        preview_status = body.get("preview_status", "PREVIEW_DRAFT")
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager
        manager = CommercialPipelineManager()
        res = manager.create_or_update_preview(
            lead_id=lead_id,
            preview_url=preview_url,
            preview_description=preview_description,
            what_demonstrated=what_demonstrated,
            next_action=next_action,
            operator_notes=operator_notes,
            preview_status=preview_status,
            operator=operator,
        )
        return res
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/preview/mark-sent")
async def mark_preview_sent_endpoint(request: Request):
    """
    Operator explicitly marks preview as sent after human delivery.
    Blocked if Travel Mode is active or commercial actions are locked.
    """
    try:
        from lib.system.system_config import SystemConfig, CommercialActionForbiddenError
        try:
            SystemConfig.assert_commercial_actions_allowed("mark_preview_sent")
        except CommercialActionForbiddenError as cfe:
            return JSONResponse({"status": "blocked", "error_type": "COMMERCIAL_ACTIONS_LOCKED", "message": str(cfe), "detail": f"Commercial action blocked: {cfe}"}, status_code=403)

        body = await request.json()
        lead_id = body.get("lead_id")
        preview_url = body.get("preview_url")
        operator_notes = body.get("operator_notes", "")
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager
        manager = CommercialPipelineManager()
        res = manager.mark_preview_sent(
            lead_id=lead_id,
            preview_url=preview_url,
            operator_notes=operator_notes,
            operator=operator,
        )
        return res
    except KeyError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/proposal")
async def record_proposal_endpoint(request: Request):
    """
    Records proposal status and financial terms (amount, currency, notes).
    """
    try:
        body = await request.json()
        lead_id = body.get("lead_id")
        proposal_status = body.get("proposal_status", "DRAFTED")
        amount = body.get("amount")
        currency = body.get("currency", "GBP")
        notes = body.get("notes", "")
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager
        manager = CommercialPipelineManager()
        res = manager.record_proposal(
            lead_id=lead_id,
            proposal_status=proposal_status,
            amount=float(amount) if amount is not None else None,
            currency=currency,
            notes=notes,
            operator=operator,
        )
        return res
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/close")
async def close_deal_endpoint(request: Request):
    """
    Marks a deal as WON or LOST with mandatory reason / value fields.
    WON requires explicit operator confirmation (operator_confirmed=True).
    """
    try:
        body = await request.json()
        lead_id = body.get("lead_id")
        status = body.get("status", "")
        data = body.get("data", {})
        operator = body.get("operator", "HUMAN_OPERATOR")
        operator_confirmed = body.get("operator_confirmed", False)

        if status == "WON":
            from lib.system.system_config import SystemConfig, CommercialActionForbiddenError
            try:
                SystemConfig.assert_commercial_actions_allowed("close_deal_won")
            except CommercialActionForbiddenError as cfe:
                return JSONResponse({"status": "blocked", "error_type": "COMMERCIAL_ACTIONS_LOCKED", "message": str(cfe), "detail": f"Commercial action blocked: {cfe}"}, status_code=403)

        from lib.commercial.commercial_pipeline_manager import CommercialPipelineManager
        manager = CommercialPipelineManager()
        res = manager.close_deal(
            lead_id=lead_id,
            status=status,
            data=data,
            operator=operator,
            operator_confirmed=operator_confirmed,
        )
        return res
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# =============================================================================
# Phase 10.0: Proposal & Deal Closing Workspace Endpoints
# =============================================================================

@app.get("/api/commercial/proposal/packages")
async def get_proposal_packages():
    """
    Returns available website proposal packages (STARTER, STANDARD, PREMIUM, CUSTOM) and components.
    """
    try:
        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        return {"status": "ok", **pm.load_packages()}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/commercial/proposal/pipeline")
async def get_all_proposals():
    """
    Returns all proposals across commercial stages and Section 26 pipeline table.
    """
    try:
        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        proposals = pm.load_proposals()
        table_data = pm.get_proposal_pipeline_table()
        return {
            "status": "ok",
            "count": len(proposals),
            "proposals": proposals,
            "pipeline_table": table_data,
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/commercial/proposal/analytics")
async def get_proposal_analytics():
    """
    Returns Section 27 proposal funnel analytics, rates, and values.
    """
    try:
        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        data = pm.get_proposal_pipeline_analytics()
        return {"status": "ok", **data}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/commercial/proposal/snapshot")
async def get_proposal_snapshot():
    """
    Generates and returns Section 33 proposal pipeline machine snapshot.
    """
    try:
        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        snap = pm.generate_proposal_pipeline_snapshot()
        return {"status": "ok", "snapshot": snap}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/proposal/create")
async def create_proposal_endpoint(request: Request):
    """
    Creates a new proposal in DRAFT status (Section 12).
    Strictly creates DRAFT only; NEVER automatically sends.
    """
    try:
        body = await request.json()
        lead_id = body.get("lead_id")
        service = body.get("service", "WEBSITE_DEVELOPMENT")
        package_name = body.get("package_name", "CUSTOM")
        currency = body.get("currency", "GBP")
        subtotal = float(body.get("subtotal", 0.0))
        discount = float(body.get("discount", 0.0))
        scope = body.get("scope")
        deliverables = body.get("deliverables")
        exclusions = body.get("exclusions")
        payment_terms = body.get("payment_terms", "50% upfront deposit upon agreement, 50% upon final delivery prior to launch")
        timeline = body.get("timeline", "10 - 14 business days")
        revision_limit = body.get("revision_limit", "2 revision rounds included")
        valid_until_days = int(body.get("valid_until_days", 30))
        operator_notes = body.get("operator_notes", "")
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        res = pm.create_proposal(
            lead_id=lead_id,
            service=service,
            package_name=package_name,
            currency=currency,
            subtotal=subtotal,
            discount=discount,
            scope=scope,
            deliverables=deliverables,
            exclusions=exclusions,
            payment_terms=payment_terms,
            timeline=timeline,
            revision_limit=revision_limit,
            valid_until_days=valid_until_days,
            operator_notes=operator_notes,
            operator=operator,
        )
        return res
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/proposal/update")
async def update_proposal_endpoint(request: Request):
    """
    Updates an unsent proposal (Section 12).
    IMMUTABILITY GUARD: Rejects edits to proposals in SENT, ACCEPTED, or REJECTED states.
    """
    try:
        body = await request.json()
        proposal_id = body.get("proposal_id")
        updates = body.get("updates", {})
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        res = pm.update_proposal(
            proposal_id=proposal_id,
            updates=updates,
            operator=operator,
        )
        return res
    except PermissionError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=403)
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/proposal/approve")
async def approve_proposal_endpoint(request: Request):
    """
    Transitions proposal from DRAFT to READY_TO_SEND (Section 12).
    """
    try:
        body = await request.json()
        proposal_id = body.get("proposal_id")
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        res = pm.approve_proposal(
            proposal_id=proposal_id,
            operator=operator,
        )
        return res
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/commercial/proposal/{proposal_id}/preview")
async def preview_proposal_endpoint(proposal_id: str):
    """
    Renders structured 10-section proposal preview (Section 13).
    """
    try:
        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        prev = pm.render_proposal_preview(proposal_id=proposal_id)
        return {"status": "ok", **prev, "preview": prev}
    except KeyError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/commercial/proposal/{proposal_id}/shareable")
async def shareable_proposal_endpoint(proposal_id: str):
    """
    Generates professional HTML document for export / printing (Section 14).
    """
    try:
        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        html = pm.render_proposal_shareable_html(proposal_id=proposal_id)
        return HTMLResponse(content=html, status_code=200)
    except KeyError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/proposal/mark-sent")
async def mark_proposal_sent_endpoint(request: Request):
    """
    Marks proposal as SENT after manual operator delivery (Section 15).
    Requires operator_confirmed == True. Proposal becomes immutable.
    Blocked if Travel Mode is active or commercial actions are locked.
    """
    try:
        from lib.system.system_config import SystemConfig, CommercialActionForbiddenError
        try:
            SystemConfig.assert_commercial_actions_allowed("mark_proposal_sent")
        except CommercialActionForbiddenError as cfe:
            return JSONResponse({"status": "blocked", "error_type": "COMMERCIAL_ACTIONS_LOCKED", "message": str(cfe), "detail": f"Commercial action blocked: {cfe}"}, status_code=403)

        body = await request.json()
        proposal_id = body.get("proposal_id")
        delivery_method = body.get("delivery_method", "DIRECT_EMAIL")
        operator_confirmed = body.get("operator_confirmed", False)
        operator_notes = body.get("operator_notes", "")
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        res = pm.mark_proposal_sent(
            proposal_id=proposal_id,
            delivery_method=delivery_method,
            operator_confirmed=operator_confirmed,
            operator_notes=operator_notes,
            operator=operator,
        )
        return res
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/proposal/revision")
async def create_proposal_revision_endpoint(request: Request):
    """
    Creates a new proposal version superseding a sent proposal (Section 16).
    """
    try:
        body = await request.json()
        proposal_id = body.get("proposal_id")
        change_reason = body.get("change_reason", "")
        updates = body.get("updates", {})
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        res = pm.create_revision(
            proposal_id=proposal_id,
            change_reason=change_reason,
            updates=updates,
            operator=operator,
        )
        return res
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/proposal/negotiate")
async def record_negotiation_endpoint(request: Request):
    """
    Records negotiation feedback / proposed changes (Section 17).
    """
    try:
        body = await request.json()
        proposal_id = body.get("proposal_id")
        note_type = body.get("note_type", "other")
        details = body.get("details", "")
        proposed_changes = body.get("proposed_changes", {})
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        res = pm.record_negotiation(
            proposal_id=proposal_id,
            note_type=note_type,
            details=details,
            proposed_changes=proposed_changes,
            operator=operator,
        )
        return res
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/proposal/won")
async def mark_proposal_won_endpoint(request: Request):
    """
    Marks proposal and deal as WON (Section 19).
    Requires agreed_value, currency, start_date, payment_terms, and operator_confirmed == True.
    Blocked if Travel Mode is active or commercial actions are locked.
    """
    try:
        from lib.system.system_config import SystemConfig, CommercialActionForbiddenError
        try:
            SystemConfig.assert_commercial_actions_allowed("mark_proposal_won")
        except CommercialActionForbiddenError as cfe:
            return JSONResponse({"status": "blocked", "error_type": "COMMERCIAL_ACTIONS_LOCKED", "message": str(cfe), "detail": f"Commercial action blocked: {cfe}"}, status_code=403)

        body = await request.json()
        proposal_id = body.get("proposal_id")
        agreed_value = float(body.get("agreed_value", 0.0))
        currency = body.get("currency", "GBP")
        start_date = body.get("start_date", "")
        payment_terms = body.get("payment_terms", "")
        operator_notes = body.get("operator_notes", "")
        operator_confirmed = body.get("operator_confirmed", False)
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        res = pm.mark_deal_won(
            proposal_id=proposal_id,
            agreed_value=agreed_value,
            currency=currency,
            start_date=start_date,
            payment_terms=payment_terms,
            operator_notes=operator_notes,
            operator_confirmed=operator_confirmed,
            operator=operator,
        )
        return res
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/commercial/proposal/lost")
async def mark_proposal_lost_endpoint(request: Request):
    """
    Marks proposal and deal as LOST with mandatory reason (Section 20).
    """
    try:
        body = await request.json()
        proposal_id = body.get("proposal_id")
        reason = body.get("reason", "")
        operator_notes = body.get("operator_notes", "")
        operator = body.get("operator", "HUMAN_OPERATOR")

        from lib.commercial.proposal_manager import ProposalManager
        pm = ProposalManager()
        res = pm.mark_deal_lost(
            proposal_id=proposal_id,
            reason=reason,
            operator_notes=operator_notes,
            operator=operator,
        )
        return res
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# =============================================================================
# Phase 10.1: Production Hardening & Technical Operations Endpoints
# =============================================================================

@app.get("/api/system/health")
async def get_system_health_endpoint():
    """
    Returns global system health status (HEALTHY, DEGRADED, FAILED)
    aggregating file integrity, last successful runs, reconciliation, backups,
    quota status, scheduler status, and data quality issues.
    """
    try:
        from lib.system.system_health import SystemHealthMonitor
        monitor = SystemHealthMonitor()
        snapshot = monitor.generate_snapshot()
        return {"status": "ok", **snapshot}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/system/config")
async def get_system_config_endpoint():
    """
    Returns current operating mode, Travel Mode state, Commercial Kill Switch,
    and service configuration audit.
    """
    try:
        from lib.system.system_config import SystemConfig
        from lib.system.config_validator import ConfigValidator
        cfg = SystemConfig.get_status_dict()
        val = ConfigValidator.validate_all()
        return {"status": "ok", "system_config": cfg, "provider_audit": val}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/system/readiness")
async def get_system_readiness_endpoint():
    """
    Returns Phase 10.6 Production Readiness audit: 20 deterministic gates,
    overall status (STAGING_READY / TECHNICALLY_READY / BLOCKED),
    and structured evidence for each gate.
    """
    try:
        from lib.system.production_readiness import ProductionReadinessAuditor
        auditor = ProductionReadinessAuditor()
        report = auditor.audit_all_gates()
        return {"status": "ok", **report}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/system/freshness-summary")
async def get_system_freshness_summary_endpoint():
    """
    Returns canonical freshness summary across 6 dimensions for all known leads.
    """
    try:
        from lib.system.canonical_freshness import CanonicalFreshnessEngine
        from dataclasses import asdict
        provider = GoogleSheetsStorageProvider()
        leads = provider.fetch_all_leads()
        engine = CanonicalFreshnessEngine()

        summaries = []
        counts = {"FRESH": 0, "DUE": 0, "STALE": 0, "REFRESHING": 0, "REFRESH_FAILED": 0}
        dimension_stale_counts = {
            "WEBSITE": 0,
            "OPERATIONAL": 0,
            "PHONE": 0,
            "SOCIAL": 0,
            "REVIEW": 0,
            "CONTACTABILITY": 0,
        }

        for lead in leads:
            summ = engine.evaluate_lead(lead)
            counts[summ.freshness_status] = counts.get(summ.freshness_status, 0) + 1
            for dim_name in summ.stale_dimensions:
                if dim_name in dimension_stale_counts:
                    dimension_stale_counts[dim_name] += 1
            summaries.append(asdict(summ))

        return {
            "status": "ok",
            "total_evaluated": len(leads),
            "counts": counts,
            "dimension_stale_counts": dimension_stale_counts,
            "cadences_days": engine.cadences,
            "leads": summaries[:100],  # Return up to 100 for inspection
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/system/mode")
async def update_operating_mode_endpoint(request: Request):
    """
    Explicitly updates Travel Mode and Commercial Actions master switch.
    """
    try:
        body = await request.json()
        from lib.system.system_config import SystemConfig
        if "travel_mode" in body:
            SystemConfig.set_travel_mode(bool(body["travel_mode"]))
        if "commercial_actions_enabled" in body:
            SystemConfig.set_commercial_actions(bool(body["commercial_actions_enabled"]))
        return {"status": "ok", "system_config": SystemConfig.get_status_dict()}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/system/jobs")
async def list_technical_jobs_endpoint():
    """
    Returns list of technical operations runs (Job, Market, Started, Finished, Status, Processed, Changed, Skipped, Errors, Quota).
    """
    try:
        from lib.system.technical_orchestrator import TechnicalOrchestrator
        orch = TechnicalOrchestrator()
        jobs = orch.list_jobs(limit=100)
        return {"status": "ok", "count": len(jobs), "jobs": jobs}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/system/jobs/{run_id}")
async def get_technical_job_endpoint(run_id: str):
    """
    Returns details, errors, and checkpoint state for a specific technical job.
    """
    try:
        from lib.system.technical_orchestrator import TechnicalOrchestrator
        orch = TechnicalOrchestrator()
        job = orch._get_job(run_id)
        if not job:
            return JSONResponse({"status": "error", "message": f"Job {run_id} not found"}, status_code=404)
        chk = orch._load_checkpoint(run_id)
        return {"status": "ok", "job": job, "checkpoint": chk}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/system/jobs/run")
async def trigger_technical_job_endpoint(request: Request):
    """
    Triggers a safe technical job (RUN_BACKUP, RUN_RECONCILIATION, RUN_DATA_QUALITY, RUN_ANALYTICS, etc.).
    Only safe technical jobs are accepted. Commercial send actions are strictly rejected.
    """
    try:
        from lib.system.technical_orchestrator import TechnicalOrchestrator, JobType
        body = await request.json()
        job_type = body.get("job_type")
        valid_job_types = [j.value for j in JobType]
        if not job_type or job_type not in valid_job_types:
            return JSONResponse(
                {"status": "error", "message": f"Missing or invalid job_type: '{job_type}'. Must be one of {valid_job_types}"},
                status_code=400,
            )
        market_id = body.get("market_id", "MANCHESTER_UK")
        dry_run = body.get("dry_run", False)
        candidate_limit = int(body.get("candidate_limit", 50))

        orch = TechnicalOrchestrator()
        res = orch.trigger_job(
            job_type=job_type,
            market_id=market_id,
            dry_run=dry_run,
            candidate_limit=candidate_limit,
        )
        return {"status": "ok", **res}
    except PermissionError as pe:
        return JSONResponse({"status": "forbidden", "message": str(pe)}, status_code=403)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/system/jobs/{run_id}/resume")
async def resume_technical_job_endpoint(run_id: str):
    """
    Resumes a paused or partial technical job from checkpoint.
    """
    try:
        from lib.system.technical_orchestrator import TechnicalOrchestrator
        orch = TechnicalOrchestrator()
        res = orch.resume_job(run_id)
        return {"status": "ok", **res}
    except (KeyError, ValueError) as ve:
        return JSONResponse({"status": "error", "message": str(ve)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/system/jobs/{run_id}/cancel")
async def cancel_technical_job_endpoint(run_id: str):
    """
    Cancels an active or paused technical job.
    """
    try:
        from lib.system.technical_orchestrator import TechnicalOrchestrator
        orch = TechnicalOrchestrator()
        res = orch.cancel_job(run_id)
        return {"status": "ok", **res}
    except KeyError as ke:
        return JSONResponse({"status": "error", "message": str(ke)}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/system/reconcile")
async def reconcile_system_endpoint():
    """
    Runs full cross-store contradiction detection.
    """
    try:
        from lib.system.reconciliation_engine import ReconciliationEngine
        reconciler = ReconciliationEngine()
        report = reconciler.run_reconciliation()
        return {"status": "ok", **report}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/system/backup")
async def trigger_system_backup_endpoint():
    """
    Creates an immediate point-in-time state backup with SHA-256 manifest.
    """
    try:
        from lib.system.backup_manager import BackupManager
        bm = BackupManager()
        res = bm.create_backup(label="manual_api")
        return {"status": "ok", **res}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 10.2: AUTOMATED EMAIL OUTREACH ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/email/sender-health")
async def get_email_sender_health_endpoint():
    """
    Returns domain authentication health for sending domain (SPF, DKIM, DMARC, MX).
    """
    try:
        from lib.outreach.email_sender_health import EmailSenderHealthAuditor
        auditor = EmailSenderHealthAuditor()
        return auditor.check_sender_health()
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/email/unsubscribe/{token}", response_class=HTMLResponse)
async def get_email_unsubscribe_endpoint(token: str):
    """
    Handles user click on email unsubscribe link (idempotent).
    """
    try:
        from lib.outreach.email_suppression import EmailSuppressionManager
        sm = EmailSuppressionManager()
        res = sm.process_unsubscribe(token, reason="link_click")
        email_display = res.get("email") or "your email"
        return f"""<!DOCTYPE html>
<html>
<head><title>Unsubscribed | Dripp Media</title></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; text-align: center; padding: 60px 20px; background: #0f172a; color: #f8fafc;">
  <div style="max-width: 500px; margin: 0 auto; background: #1e293b; padding: 40px; border-radius: 12px; border: 1px solid #334155;">
    <h2 style="color: #38bdf8; margin-bottom: 12px;">Unsubscribed Successfully</h2>
    <p style="color: #94a3b8; font-size: 16px; line-height: 1.5;">
      The address <strong style="color: #f1f5f9;">{email_display}</strong> has been Unsubscribed from our automated outreach list.
    </p>
    <p style="color: #64748b; font-size: 14px; margin-top: 24px;">You will receive no further automated marketing communications from Dripp Media.</p>
  </div>
</body>
</html>"""
    except Exception as e:
        return HTMLResponse(f"<h3>Unable to process unsubscribe: {str(e)}</h3>", status_code=500)


@app.post("/api/email/unsubscribe")
async def post_email_unsubscribe_endpoint(request: Request):
    """
    Processes an unsubscribe request via API (idempotent).
    """
    try:
        body = await request.json()
        identifier = body.get("token") or body.get("email") or ""
        reason = body.get("reason", "api_request")
        if not identifier:
            return JSONResponse({"status": "error", "message": "Missing 'token' or 'email' parameter"}, status_code=400)
        from lib.outreach.email_suppression import EmailSuppressionManager
        sm = EmailSuppressionManager()
        res = sm.process_unsubscribe(identifier, reason=reason)
        return res
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/email/automation/status")
async def get_email_automation_status_endpoint():
    """
    Returns automated email engine status, quotas, and sender health.
    """
    try:
        from lib.system.system_config import SystemConfig
        from lib.outreach.automated_email_executor import AutomatedEmailExecutor
        executor = AutomatedEmailExecutor()
        analytics = executor.generate_analytics()
        sender_health = executor.sender_auditor.check_sender_health()
        rate_data = executor.governor._load_rate_data()
        return {
            "automated_email_enabled": SystemConfig.AUTOMATED_EMAIL_ENABLED,
            "email_automation_kill_switch": SystemConfig.EMAIL_AUTOMATION_KILL_SWITCH,
            "kill_switch_active": SystemConfig.EMAIL_AUTOMATION_KILL_SWITCH,
            "automated_email_active": SystemConfig.is_automated_email_enabled(),
            "max_batch_size": 5,
            "max_email_batch": 5,
            "sender_health": sender_health,
            "analytics": analytics,
            "today_sends": len(rate_data.get("sends", [])),
            "daily_limit": executor.governor.limit_per_day,
            "quota": {
                "sends_today": len(rate_data.get("sends", [])),
                "daily_limit": executor.governor.limit_per_day,
                "remaining_daily": max(0, executor.governor.limit_per_day - len(rate_data.get("sends", []))),
            },
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/email/automation/enable")
async def enable_email_automation_endpoint(request: Request):
    """
    Enables automated email outreach after running pre-flight sender and configuration audits (Section 29).
    """
    try:
        from lib.system.system_config import SystemConfig
        from lib.outreach.automated_email_executor import AutomatedEmailExecutor
        executor = AutomatedEmailExecutor()
        sender_health = executor.sender_auditor.check_sender_health()

        body = {}
        if request.headers.get("content-type") == "application/json":
            try:
                body = await request.json()
            except Exception:
                body = {}

        is_sandbox = (
            os.environ.get("EMAIL_SANDBOX_MODE", "false").lower() in ("true", "1", "yes")
            or bool(body.get("allow_sandbox", False))
            or bool(body.get("force", False))
        )
        if not sender_health["can_send_automated"] and not is_sandbox:
            return JSONResponse({
                "status": "ENABLE_BLOCKED",
                "reason": f"Sender health verification failed: overall_status={sender_health['overall_status']}, credentials_configured={sender_health['credentials_configured']}",
                "sender_health": sender_health,
            }, status_code=400)

        SystemConfig.set_automated_email(True)
        SystemConfig.set_email_kill_switch(False)
        return {
            "status": "ENABLED",
            "automated_email_enabled": True,
            "message": "Automated email outreach activated.",
            "sender_health": sender_health,
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/email/automation/disable")
async def disable_email_automation_endpoint():
    """
    Disables automated email outreach.
    """
    try:
        from lib.system.system_config import SystemConfig
        SystemConfig.set_automated_email(False)
        return {
            "status": "DISABLED",
            "automated_email_enabled": False,
            "message": "Automated email outreach disabled.",
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/email/automation/dry-run")
async def dry_run_email_automation_endpoint(request: Request):
    """
    Executes a non-mutating preview of candidates, message QA, and sendability.
    """
    try:
        body = {}
        if request.headers.get("content-type") == "application/json":
            try:
                body = await request.json()
            except Exception:
                body = {}
        max_batch = int(body.get("max_batch", 5)) if body else 5
        from lib.outreach.automated_email_executor import AutomatedEmailExecutor
        executor = AutomatedEmailExecutor()
        return executor.run_dry_run(max_batch=max_batch)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/email/automation/execute-batch")
async def execute_email_batch_endpoint(request: Request):
    """
    Executes an automated email outreach batch (max 5).
    """
    try:
        from lib.system.system_config import SystemConfig, EmailAutomationBlockedError
        body = {}
        if request.headers.get("content-type") == "application/json":
            try:
                body = await request.json()
            except Exception:
                body = {}
        campaign_id = body.get("campaign_id", "CAMP-EMAIL-001") if body else "CAMP-EMAIL-001"
        max_batch = int(body.get("max_batch", 5)) if body else 5
        allow_sandbox = bool(body.get("allow_sandbox", False)) if body else False

        from lib.outreach.automated_email_executor import AutomatedEmailExecutor
        executor = AutomatedEmailExecutor()
        res = executor.execute_automated_batch(
            campaign_id=campaign_id,
            max_batch=max_batch,
            allow_sandbox=allow_sandbox,
        )
        return res
    except EmailAutomationBlockedError as e:
        return JSONResponse({"status": "blocked", "error_type": "AUTOMATION_BLOCKED", "message": str(e)}, status_code=403)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/email/automation/emergency-stop")
async def emergency_stop_email_endpoint(request: Request):
    """
    Immediately halts all automated email submissions. Idempotent.
    """
    try:
        body = {}
        if request.headers.get("content-type") == "application/json":
            try:
                body = await request.json()
            except Exception:
                body = {}
        reason = body.get("reason", "Operator triggered emergency stop") if body else "Operator triggered emergency stop"
        operator = body.get("operator", "HUMAN_OPERATOR") if body else "HUMAN_OPERATOR"

        from lib.outreach.automated_email_executor import AutomatedEmailExecutor
        executor = AutomatedEmailExecutor()
        res = executor.emergency_stop(reason=reason, operator=operator)
        return {
            "status": "EMERGENCY_STOP_ACTIVATED",
            "kill_switch_active": True,
            **res,
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/email/inbound/reply-webhook")
async def inbound_email_reply_endpoint(request: Request):
    """
    Inbound webhook receiver: logs EMAIL_REPLY_RECEIVED and routes to OPERATOR_REVIEW.
    """
    try:
        body = await request.json()
        lead_id = body.get("lead_id")
        if not lead_id:
            return JSONResponse({"status": "error", "message": "Missing 'lead_id'"}, status_code=400)
        message_id = body.get("message_id") or f"IN-{uuid.uuid4().hex[:8]}"
        thread_ref = body.get("thread_ref")
        provider = body.get("provider", "INBOUND_WEBHOOK")
        snippet = body.get("snippet", "")

        from lib.outreach.automated_email_executor import AutomatedEmailExecutor
        executor = AutomatedEmailExecutor()
        res = executor.record_inbound_reply(
            lead_id=lead_id, message_id=message_id, thread_ref=thread_ref, provider=provider, snippet=snippet
        )
        return res
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html"), "r") as f:
        return f.read()

if not os.path.exists("static"):
    os.makedirs("static")


app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False)
