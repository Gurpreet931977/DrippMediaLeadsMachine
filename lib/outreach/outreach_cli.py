import sys
import os
import json
from datetime import datetime

base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from lib.outreach.outreach_service import OutreachService, ChannelConfigManager
from lib.outreach.channel_router import ChannelRouter, summarize_channel_availability
from lib.outreach.campaign_executor import (
    execute_campaign,
    update_queue_item_message,
    get_campaign_execution_status,
    _get_daily_limits,
    _get_daily_sent,
)
from lib.outreach.execution_gate import (
    arm_campaign,
    mark_campaign_previewed,
    get_campaign_execution_state,
    ExecutionState,
)



def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "list_campaigns"
    service = OutreachService()

    # Redirect print/logging noise to stderr so stdout is clean JSON
    old_stdout = sys.stdout
    sys.stdout = sys.stderr

    try:
        # ── Campaign Management ──────────────────────────────────
        if action == "list_campaigns":
            res = service.list_campaigns()

        elif action == "create_campaign":
            payload = json.loads(sys.stdin.read())
            res = service.create_campaign(
                source_pipeline_run_id=payload.get("source_pipeline_run_id", ""),
                campaign_name=payload.get("campaign_name", ""),
                country=payload.get("country", "United Kingdom"),
                city=payload.get("city", "Manchester"),
                category=payload.get("category", "Restaurants"),
                outreach_mode=payload.get("outreach_mode", "AUTO"),
                channel=payload.get("channel", "Instagram Direct Message"),
                selected_lead_count=payload.get("selected_lead_count", 0),
                daily_limit=payload.get("daily_limit", 10),
                max_campaign_limit=payload.get("max_campaign_limit", 10),
                message_strategy=payload.get("message_strategy", "Personalized")
            )

        # ── Queue & Sending ──────────────────────────────────────
        elif action == "get_queue":
            campaign_id = sys.argv[2] if len(sys.argv) > 2 else None
            res = service.get_queue(campaign_id=campaign_id)

        elif action == "queue_leads":
            payload = json.loads(sys.stdin.read())
            campaign_id = payload.get("campaign_id")
            leads_data = payload.get("leads", [])
            channel = payload.get("channel", "Instagram Direct Message")

            # Auto-create campaign if campaign_name provided without campaign_id
            if not campaign_id:
                c_name = payload.get("campaign_name") or (
                    f"Auto Campaign - {payload.get('city', 'Manchester')} "
                    f"{datetime.utcnow().strftime('%Y%m%d')}"
                )
                camp = service.create_campaign(
                    source_pipeline_run_id=payload.get("source_pipeline_run_id", ""),
                    campaign_name=c_name,
                    country=payload.get("country", "United Kingdom"),
                    city=payload.get("city", "Manchester"),
                    category=payload.get("category", "Restaurants"),
                    outreach_mode="AUTO",
                    channel=channel,
                    selected_lead_count=len(leads_data),
                    daily_limit=payload.get("daily_limit", 10),
                    max_campaign_limit=payload.get("max_campaign_limit", 10),
                    message_strategy=payload.get("message_strategy", "Personalized")
                )
                campaign_id = camp.get("campaign_id")

            res = service.queue_leads_for_auto_outreach(
                campaign_id=campaign_id,
                leads_data=leads_data,
                channel=channel
            )

        # ── Dry Run (Section 25) ─────────────────────────────────
        elif action == "dry_run":
            payload = json.loads(sys.stdin.read())
            campaign_id = payload.get("campaign_id")
            leads_data = payload.get("leads", [])
            channel = payload.get("channel", "Auto-select")

            # Auto-create campaign for dry run if not provided
            if not campaign_id:
                c_name = payload.get("campaign_name") or (
                    f"DRY RUN - {payload.get('city', 'Manchester')} "
                    f"{datetime.utcnow().strftime('%Y%m%d-%H%M')}"
                )
                camp = service.create_campaign(
                    source_pipeline_run_id=payload.get("source_pipeline_run_id", ""),
                    campaign_name=c_name,
                    country=payload.get("country", "United Kingdom"),
                    city=payload.get("city", "Manchester"),
                    category=payload.get("category", "Restaurants"),
                    outreach_mode="AUTO",
                    channel=channel,
                    selected_lead_count=len(leads_data),
                    daily_limit=payload.get("daily_limit", 10),
                    max_campaign_limit=payload.get("max_campaign_limit", 100),
                    message_strategy=payload.get("message_strategy", "Personalized")
                )
                campaign_id = camp.get("campaign_id")

            res = service.dry_run_auto_outreach(
                campaign_id=campaign_id,
                leads_data=leads_data,
                channel=channel
            )

        # ── Channel Router — evaluate channels for leads (Section 24) ───
        elif action == "get_channel_router":
            payload = json.loads(sys.stdin.read())
            leads_data = payload.get("leads", [])
            router = ChannelRouter()
            summary = summarize_channel_availability(leads_data)
            per_lead = []
            for lead in leads_data:
                evals = router.evaluate_all_channels(lead)
                per_lead.append({
                    "lead_id": lead.get("lead_id", ""),
                    "company_name": lead.get("company_name", ""),
                    "channels": {k: v.to_dict() for k, v in evals.items()}
                })
            res = {"channel_summary": summary, "per_lead": per_lead}

        # ── Manual Outreach Assignment (Section 19) ─────────────────────
        elif action == "assign_manual":
            payload = json.loads(sys.stdin.read())
            leads_data = payload.get("leads", [])
            res = service.assign_manual_outreach(leads_data=leads_data)

        # ── Call Logging ─────────────────────────────────────────
        elif action == "log_call":
            payload = json.loads(sys.stdin.read())
            res = service.log_manual_call(
                lead_id=payload.get("lead_id"),
                company_name=payload.get("company_name", ""),
                call_status=payload.get("call_status", ""),
                call_notes=payload.get("call_notes", ""),
                call_outcome=payload.get("call_outcome", ""),
                next_follow_up=payload.get("next_follow_up", ""),
                marked_contacted=payload.get("marked_contacted", True)
            )

        elif action == "get_manual_logs":
            lead_id = sys.argv[2] if len(sys.argv) > 2 else None
            res = service.get_manual_logs(lead_id=lead_id)

        # ── Experiment Tracking ──────────────────────────────────
        elif action == "get_experiments":
            res = service.get_experiment_stats()

        # ── Channel Config Guard ─────────────────────────────────
        elif action == "get_channels":
            res = ChannelConfigManager.get_all_channels()

        # ── Campaign Execution (Section 13) ──────────────────────
        elif action == "execute_campaign":
            payload = json.loads(sys.stdin.read())
            campaign_id = payload.get("campaign_id")
            confirmation_token = payload.get("confirmation_token", "")
            max_per_run = payload.get("max_per_run", None)
            delay_seconds = float(payload.get("delay_seconds", 1.0))
            res = execute_campaign(
                campaign_id=campaign_id,
                confirmation_token=confirmation_token,
                max_per_run=max_per_run,
                delay_seconds=delay_seconds
            )

        # ── Arm Campaign (Server-Side Gate) ───────────────────────
        elif action == "arm_campaign":
            payload = json.loads(sys.stdin.read())
            campaign_id = payload.get("campaign_id")
            res = arm_campaign(campaign_id)

        # ── Mark Campaign Previewed ──────────────────────────────
        elif action == "mark_previewed":
            payload = json.loads(sys.stdin.read())
            campaign_id = payload.get("campaign_id")
            res = mark_campaign_previewed(campaign_id)

        # ── Get Campaign Execution State ─────────────────────────
        elif action == "get_execution_gate_state":
            campaign_id = sys.argv[2] if len(sys.argv) > 2 else None
            state = get_campaign_execution_state(campaign_id) if campaign_id else None
            res = {"campaign_id": campaign_id, "execution_state": state}

        # ── Execution Status / Live Progress ─────────────────────
        elif action == "get_execution_status":
            campaign_id = sys.argv[2] if len(sys.argv) > 2 else None
            if not campaign_id:
                payload = json.loads(sys.stdin.read() or "{}")
                campaign_id = payload.get("campaign_id")
            res = get_campaign_execution_status(campaign_id)

        # ── Message Edit (Section 9) ─────────────────────────────
        elif action == "edit_queue_item":
            payload = json.loads(sys.stdin.read())
            res = update_queue_item_message(
                queue_id=payload.get("queue_id"),
                final_message=payload.get("final_message", ""),
                final_subject=payload.get("final_subject", ""),
                edited_by_user=payload.get("edited_by_user", True)
            )

        # ── Rate Limits ───────────────────────────────────────────
        elif action == "get_rate_limits":
            limits = _get_daily_limits()
            res = {
                "channels": {
                    ch: {
                        "daily_limit": limit,
                        "sent_today": _get_daily_sent(ch),
                        "remaining": max(0, limit - _get_daily_sent(ch))
                    }
                    for ch, limit in limits.items()
                }
            }

        # ── Contact Enrichment & Verification (Sections 3 - 21) ──
        elif action == "assess_contactability":
            payload = json.loads(sys.stdin.read() or "{}")
            from lib.outreach.contactability import ContactabilityAssessor
            lead = payload.get("lead", payload)
            assessment = ContactabilityAssessor.assess_lead(lead)
            res = assessment.to_dict()

        elif action == "enrich_qualified_leads":
            from lib.outreach.contactability import ContactabilityAssessor
            from lib.outreach.compliance import ContactHistoryManager, CooldownManager
            from lib.sheets.google_sheets import GoogleSheetsStorageProvider
            provider = GoogleSheetsStorageProvider()
            leads = provider.fetch_all_leads()
            results = []
            for l in leads:
                assessment = ContactabilityAssessor.assess_lead(l)
                updates = {
                    "contactability_status": assessment.contactability_status,
                    "contactability_reason": assessment.contactability_reason,
                    "recipient_verified": str(any(c.recipient_verified for c in assessment.channels.values())),
                    "recipient_confidence": assessment.email_data.get("confidence", "UNKNOWN") if assessment.email_data else "UNKNOWN",
                    "contact_history_count": str(ContactHistoryManager.get_contact_count(l.get("lead_id", ""))),
                }
                if assessment.entity_data:
                    updates.update({
                        "companies_house_number": assessment.entity_data.get("companies_house_number", ""),
                        "registered_name": assessment.entity_data.get("registered_name", ""),
                        "company_type": assessment.entity_data.get("company_type", ""),
                        "company_status": assessment.entity_data.get("company_status", ""),
                        "registered_office": assessment.entity_data.get("registered_office", ""),
                        "sic_codes": ", ".join(assessment.entity_data.get("sic_codes", [])),
                        "companies_house_url": assessment.entity_data.get("companies_house_url", ""),
                        "entity_match_status": assessment.entity_data.get("match_status", ""),
                        "entity_match_confidence": assessment.entity_data.get("match_confidence", ""),
                        "entity_match_reason": assessment.entity_data.get("match_reason", ""),
                    })
                if assessment.email_data:
                    updates.update({
                        "email": assessment.email_data.get("email", ""),
                        "email_type": assessment.email_data.get("email_type", ""),
                        "email_source": assessment.email_data.get("source", ""),
                        "email_source_url": assessment.email_data.get("source_url", ""),
                        "email_verification_status": assessment.email_data.get("verification_status", ""),
                        "email_confidence": assessment.email_data.get("confidence", ""),
                        "email_last_verified_at": assessment.email_data.get("last_verified_at", ""),
                    })
                if assessment.compliance_data:
                    updates.update({
                        "subscriber_type": assessment.compliance_data.get("subscriber_type", ""),
                        "marketing_email_status": assessment.compliance_data.get("marketing_email_status", ""),
                        "lawful_basis_status": assessment.compliance_data.get("lawful_basis_status", ""),
                        "opt_out_status": assessment.compliance_data.get("opt_out_status", ""),
                        "compliance_review_status": assessment.compliance_data.get("compliance_review_status", ""),
                        "compliance_notes": assessment.compliance_data.get("compliance_notes", ""),
                    })
                in_cd, cd_until = CooldownManager.is_in_cooldown(l.get("lead_id", ""), "Email")
                if cd_until:
                    updates["global_cooldown_until"] = cd_until

                provider.update_lead_outreach(l.get("lead_id", ""), updates)
                results.append(assessment.to_dict())

            res = {"count": len(results), "leads": results}

        else:
            res = {"error": f"Unknown action: {action}"}

    except Exception as e:
        import traceback
        traceback.print_exc()
        res = {"error": str(e)}
    finally:
        sys.stdout = old_stdout

    print(json.dumps(res, default=str))


if __name__ == "__main__":
    main()
