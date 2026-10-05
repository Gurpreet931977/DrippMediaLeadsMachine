"""
Dripp Media — Campaign Dashboard Aggregator
============================================
Section 22 (§9): Campaign-level statistics across all outreach campaigns.

Computes measured numbers only:
  - Selected, Sent, Failed, Blocked, Replies, Interested,
    Not Interested, Meetings, Proposals, Won, Lost
  - Broken down by Manual vs Auto mode.

IMPORTANT:
  - Never infers which method performs better.
  - Never marks assumptions about unseen data.
  - Only counts what is explicitly recorded.
"""

import os
import json
from typing import Dict, Any, List

from lib.outreach.outcome_tracker import (
    _load_outcomes,
    ResponseStatus,
    ResponseType,
    SalesStage,
    MeetingStatus,
    ProposalStatus,
)

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data"
)
CAMPAIGNS_FILE = os.path.join(DATA_DIR, "campaigns.json")
QUEUE_FILE     = os.path.join(DATA_DIR, "outreach_queue.json")


def _load_campaigns() -> List[Dict[str, Any]]:
    try:
        with open(CAMPAIGNS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _load_queue() -> List[Dict[str, Any]]:
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def get_campaign_dashboard(campaign_id: str) -> Dict[str, Any]:
    """
    Returns the dashboard metrics for a single campaign.
    Joined from: campaigns.json + outreach_queue.json + outcome_tracking.json.
    """
    campaigns = _load_campaigns()
    campaign = next((c for c in campaigns if c.get("campaign_id") == campaign_id), None)
    if not campaign:
        return {"error": f"Campaign {campaign_id} not found"}

    queue    = _load_queue()
    outcomes = _load_outcomes()

    # Items belonging to this campaign
    items = [q for q in queue if q.get("campaign_id") == campaign_id]

    # Outreach counts from queue
    selected = len(items)
    sent     = sum(1 for i in items if i.get("status") == "SENT")
    delivered = sum(1 for i in items if i.get("status") == "DELIVERED")
    failed   = sum(1 for i in items if i.get("status") in ("FAILED", "FAILED_PERMANENT"))
    blocked  = sum(1 for i in items if i.get("status") == "BLOCKED")

    # Outcome counts from tracker
    lead_ids = {i.get("lead_id") for i in items if i.get("lead_id")}
    replies       = 0
    interested    = 0
    not_interested = 0
    meetings      = 0
    proposals     = 0
    won           = 0
    lost          = 0

    for lid in lead_ids:
        rec = outcomes.get(lid, {})
        if rec.get("response_status") == ResponseStatus.REPLIED:
            replies += 1
        if rec.get("response_type") == ResponseType.INTERESTED:
            interested += 1
        if rec.get("response_type") == ResponseType.NOT_INTERESTED:
            not_interested += 1
        if rec.get("meeting_status") in (MeetingStatus.BOOKED, MeetingStatus.HELD):
            meetings += 1
        if rec.get("proposal_status") in (ProposalStatus.SENT, ProposalStatus.ACCEPTED):
            proposals += 1
        if rec.get("sales_stage") == SalesStage.WON:
            won += 1
        if rec.get("sales_stage") == SalesStage.LOST:
            lost += 1

    # Manual vs Auto split
    mode = campaign.get("outreach_mode", "AUTO").upper()

    return {
        "campaign_id":   campaign_id,
        "campaign_name": campaign.get("campaign_name", ""),
        "mode":          mode,
        "channel":       campaign.get("channel", ""),
        "status":        campaign.get("status", ""),

        # Raw outreach numbers
        "selected":   selected,
        "sent":       sent,
        "delivered":  delivered,
        "failed":     failed,
        "blocked":    blocked,

        # Reply + funnel numbers (measured, not inferred)
        "replies":       replies,
        "interested":    interested,
        "not_interested": not_interested,
        "meetings":      meetings,
        "proposals":     proposals,
        "won":           won,
        "lost":          lost,
    }


def get_all_campaigns_dashboard() -> Dict[str, Any]:
    """
    Returns aggregated dashboard across ALL campaigns, split by mode.
    Also returns per-campaign breakdowns.
    """
    campaigns = _load_campaigns()
    campaign_ids = [c.get("campaign_id") for c in campaigns if c.get("campaign_id")]

    per_campaign = [get_campaign_dashboard(cid) for cid in campaign_ids]

    # Aggregate by mode
    summary: Dict[str, Dict[str, int]] = {
        "MANUAL": {
            "campaigns": 0, "leads_contacted": 0, "sent": 0, "delivered": 0,
            "failed": 0, "blocked": 0, "replies": 0, "interested": 0,
            "not_interested": 0, "meetings": 0, "proposals": 0, "won": 0, "lost": 0,
        },
        "AUTO": {
            "campaigns": 0, "leads_contacted": 0, "sent": 0, "delivered": 0,
            "failed": 0, "blocked": 0, "replies": 0, "interested": 0,
            "not_interested": 0, "meetings": 0, "proposals": 0, "won": 0, "lost": 0,
        },
    }

    for dash in per_campaign:
        if "error" in dash:
            continue
        mode = dash.get("mode", "AUTO")
        bucket = summary.get(mode, summary["AUTO"])
        bucket["campaigns"]        += 1
        bucket["leads_contacted"]  += dash.get("sent", 0)
        bucket["sent"]             += dash.get("sent", 0)
        bucket["delivered"]        += dash.get("delivered", 0)
        bucket["failed"]           += dash.get("failed", 0)
        bucket["blocked"]          += dash.get("blocked", 0)
        bucket["replies"]          += dash.get("replies", 0)
        bucket["interested"]       += dash.get("interested", 0)
        bucket["not_interested"]   += dash.get("not_interested", 0)
        bucket["meetings"]         += dash.get("meetings", 0)
        bucket["proposals"]        += dash.get("proposals", 0)
        bucket["won"]              += dash.get("won", 0)
        bucket["lost"]             += dash.get("lost", 0)

    return {
        "summary_by_mode": summary,
        "campaigns": per_campaign,
        "total_campaigns": len(per_campaign),
    }
