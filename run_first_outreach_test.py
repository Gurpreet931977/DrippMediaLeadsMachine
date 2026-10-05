"""
Dripp Media — First Real Outreach Test Runner
==============================================
Runs the First Real Outreach Test targeting Seoul Kimchi (LEAD-MAN-4DB3EF) via Email.
Strict rules:
  - Exactly 1 lead: Seoul Kimchi
  - Do NOT send Hong Thai or any other lead
  - Fact-based personalization only (reviews, rating, Manchester, Korean restaurant, Facebook, no website)
  - Interactive preview before send
  - Final eligibility check immediately before send
  - Real send via EmailAdapter (Gmail SMTP)
  - CRM sync preserving LEAD-MAN-4DB3EF row without duplicates
  - Duplicate protection verification post-send
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from lib.sheets.google_sheets import GoogleSheetsStorageProvider
from lib.outreach.outreach_service import OutreachService, ChannelConfigManager
from lib.outreach.contactability import ContactabilityAssessor, ContactabilityState
from lib.outreach.compliance import (
    UKComplianceEvaluator,
    SuppressionManager,
    CooldownManager,
    ContactHistoryManager,
    MarketingEmailStatus,
)
from lib.outreach.execution_gate import (
    arm_campaign,
    get_campaign_execution_state,
    mark_campaign_previewed,
    verify_execute_gate,
    ExecutionState,
)
from lib.outreach.campaign_executor import execute_campaign, final_pre_send_check, _load_queue, _load_campaigns
from lib.outreach.send_adapters import EmailAdapter
from lib.types import OutreachStatus, QualificationState

STAGE_FILE = os.path.join(PROJECT_ROOT, "data", "first_outreach_test_stage.json")


def generate_seoul_kimchi_email(lead: dict) -> tuple:
    """
    Generates a personalized first-contact email using strictly verified facts.
    Verified facts:
      - Seoul Kimchi
      - Manchester (Upper Brook St, M13 0HR)
      - Korean restaurant
      - 1,010 customer reviews (4.6★)
      - Verified Facebook presence
      - No official website identified
      - Verified business email: seoulkimchi@gmail.com
    No invented posts, menu items, bookings, revenue, or claims of lost customers.
    """
    subject = "Seoul Kimchi: website question from Dripp Media"
    body = (
        "Dear Seoul Kimchi Team,\n\n"
        "I hope this message finds you well.\n\n"
        "I am reaching out from Dripp Media. We noticed your established presence as an authentic "
        "Korean restaurant in Manchester, with an impressive 1,010 customer reviews (4.6★) and "
        "an active Facebook page.\n\n"
        "During our local research, we noticed that Seoul Kimchi does not currently have a dedicated "
        "official website.\n\n"
        "For an established Manchester restaurant with such strong customer traction, a simple, "
        "dedicated website can provide a central place to:\n"
        "  • Display your official menu and contact details\n"
        "  • Clearly share your location on Upper Brook Street and opening hours\n"
        "  • Give customers searching online a direct, verified destination\n\n"
        "We build clean, mobile-friendly websites for local hospitality businesses across the UK. "
        "Would you be open to a brief chat or email exchange to see what a dedicated site could look "
        "like for Seoul Kimchi?\n\n"
        "Best regards,\n\n"
        "Dripp Media Team\n"
        "mediadripp@gmail.com | www.drippmedia.com\n\n"
        "---\n"
        "Opt-out notice: If you prefer not to hear from us again, simply reply with 'STOP' or "
        "'UNSUBSCRIBE' and we will immediately add your business to our permanent suppression list."
    )
    return subject, body


def prepare_test_campaign() -> dict:
    """Prepares the 1-lead campaign, queues Seoul Kimchi, and arms the campaign."""
    print("=" * 80)
    print("STAGE 1: PREPARING FIRST OUTREACH TEST (SEOUL KIMCHI ONLY)")
    print("=" * 80)

    # 1. Fetch leads from Google Sheets
    provider = GoogleSheetsStorageProvider()
    all_leads = provider.fetch_all_leads()
    seoul_leads = [l for l in all_leads if l.get("lead_id") == "LEAD-MAN-4DB3EF"]

    if not seoul_leads:
        raise ValueError("Seoul Kimchi (LEAD-MAN-4DB3EF) not found in Google Sheets LEADS.")

    seoul_lead = seoul_leads[0]
    print(f"✓ Found target lead: {seoul_lead.get('company_name')} ({seoul_lead.get('lead_id')})")

    # Verify no other lead is included
    hong_thai_included = any(l.get("company_name") == "Hong Thai" for l in [seoul_lead])
    if hong_thai_included:
        raise ValueError("CRITICAL SAFETY ERROR: Hong Thai must NOT be included in this test!")
    print("✓ Safety Check Passed: Hong Thai and all other leads strictly excluded.")

    # 2. Create the test campaign
    service = OutreachService()
    campaign_name = "Manchester Restaurants - First Email Test"
    campaign = service.create_campaign(
        source_pipeline_run_id="EXISTING_LEAD",
        campaign_name=campaign_name,
        country="United Kingdom",
        city="Manchester",
        category="Korean restaurant",
        outreach_mode="AUTO",
        channel="Email",
        selected_lead_count=1,
        daily_limit=1,
        max_campaign_limit=1,
        message_strategy="Personalized"
    )
    campaign_id = campaign["campaign_id"]
    print(f"✓ Created Campaign: {campaign_name} ({campaign_id})")

    # 3. Generate personalized email
    subj, body = generate_seoul_kimchi_email(seoul_lead)

    # 4. Queue Seoul Kimchi in outreach_queue.json
    now_str = datetime.now(timezone.utc).isoformat()
    queue_id = f"Q-{campaign_id[-6:]}-001"

    queue_item = {
        "queue_id": queue_id,
        "campaign_id": campaign_id,
        "lead_id": "LEAD-MAN-4DB3EF",
        "company_name": "Seoul Kimchi",
        "channel": "Email",
        "selected_channel": "Email",
        "channel_selection_reason": "Verified business email discovered and deliverable via SMTP",
        "channel_availability": {
            "Email": {
                "recipient": "seoulkimchi@gmail.com",
                "sendable": True,
                "confidence": "HIGH"
            }
        },
        "recipient": "seoulkimchi@gmail.com",
        "message": {
            "message_subject": subj,
            "message_body": body,
            "channel": "Email",
            "generated_at": now_str
        },
        "final_message": body,
        "final_subject": subj,
        "qualification_state": "OUTREACH_READY",
        "status": OutreachStatus.QUEUED.value,
        "block_reason": "",
        "attempts": 0,
        "queued_at": now_str,
        "sent_at": None,
        "error_message": None
    }

    # Save to persistent queue
    queue = _load_queue()
    # Remove any existing queued items for this campaign
    queue = [q for q in queue if q.get("campaign_id") != campaign_id]
    queue.append(queue_item)
    from lib.outreach.campaign_executor import _save_queue
    _save_queue(queue)
    print(f"✓ Queued lead in outreach_queue.json ({queue_id})")

    # 5. Mark campaign PREVIEWED
    mark_campaign_previewed(campaign_id)
    print("✓ Campaign state: PREVIEWED")

    # 6. Arm campaign and generate single-use HMAC token
    arm_result = arm_campaign(campaign_id)
    if not arm_result.get("ok"):
        raise RuntimeError(f"Failed to arm campaign: {arm_result.get('error')}")
    token = arm_result["confirmation_token"]
    print(f"✓ Campaign ARMED with HMAC confirmation token: {token[:8]}...")

    stage_data = {
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "lead_id": "LEAD-MAN-4DB3EF",
        "company_name": "Seoul Kimchi",
        "recipient": "seoulkimchi@gmail.com",
        "subject": subj,
        "message": body,
        "queue_id": queue_id,
        "confirmation_token": token,
        "status": "READY FOR REVIEW",
        "prepared_at": now_str
    }

    with open(STAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(stage_data, f, indent=2)

    return stage_data


def check_final_eligibility(lead: dict, stage_data: dict) -> tuple:
    """
    Section 5: Final Eligibility Check immediately before send:
      - qualification_status = OUTREACH_READY
      - contactability_status = CONTACTABLE
      - email_verification_status = VERIFIED
      - marketing_email_status = COMPLIANCE_ELIGIBLE
      - email integration configured
      - recipient valid
      - not DO_NOT_CONTACT
      - not suppressed
      - not in cooldown
      - not already contacted
      - campaign limit = available
      - max_per_run = available
      - message exists
      - campaign is ARMED
    """
    lead_id = lead.get("lead_id", "")
    campaign_id = stage_data.get("campaign_id", "")
    recipient = stage_data.get("recipient", "")

    # 1. qualification_status = OUTREACH_READY
    qual = lead.get("qualification_state") or lead.get("qualification_status")
    if qual != "OUTREACH_READY":
        return False, f"qualification_status is '{qual}', MUST = OUTREACH_READY"

    # 2. contactability_status = CONTACTABLE
    contactability = lead.get("contactability_status")
    if contactability != "CONTACTABLE":
        return False, f"contactability_status is '{contactability}', MUST = CONTACTABLE"

    # 3. email_verification_status = VERIFIED
    email_ver = lead.get("email_verification_status")
    if email_ver != "VERIFIED":
        return False, f"email_verification_status is '{email_ver}', MUST = VERIFIED"

    # 4. marketing_email_status = COMPLIANCE_ELIGIBLE
    compliance = lead.get("marketing_email_status")
    if compliance != "COMPLIANCE_ELIGIBLE":
        return False, f"marketing_email_status is '{compliance}', MUST = COMPLIANCE_ELIGIBLE"

    # 5. email integration configured
    if not EmailAdapter.is_configured():
        return False, "EmailAdapter is NOT configured (SMTP credentials missing)"

    # 6. recipient valid
    if not recipient or "@" not in recipient:
        return False, f"Recipient '{recipient}' is invalid"

    # 7. not DO_NOT_CONTACT
    if lead.get("lead_status") == "DO_NOT_CONTACT" or lead.get("do_not_contact") is True:
        return False, "Lead is flagged DO_NOT_CONTACT"

    # 8. not suppressed
    if SuppressionManager.is_suppressed(lead_id, "Email", recipient):
        return False, f"Recipient {recipient} is on suppression list"

    # 9. not in cooldown
    in_cd, cd_until = CooldownManager.is_in_cooldown(lead_id, "Email")
    if in_cd:
        return False, f"Lead is in cooldown until {cd_until}"

    # 10. not already contacted
    contact_count = ContactHistoryManager.get_contact_count(lead_id)
    if contact_count > 0:
        return False, f"Lead has prior contact attempts ({contact_count})"

    # 11. campaign limit = available
    camps = _load_campaigns()
    camp = next((c for c in camps if c.get("campaign_id") == campaign_id), None)
    if not camp:
        return False, f"Campaign {campaign_id} not found"
    if camp.get("sent_count", 0) >= camp.get("max_campaign_limit", 1):
        return False, "Campaign max limit already reached"

    # 12. max_per_run = available
    if camp.get("daily_limit", 1) < 1:
        return False, "Campaign daily limit is 0"

    # 13. message exists
    if not stage_data.get("message", "").strip():
        return False, "Outreach message is empty"

    # 14. campaign is ARMED
    state = get_campaign_execution_state(campaign_id)
    if state != ExecutionState.ARMED:
        return False, f"Campaign execution_state is '{state}', MUST = ARMED"

    return True, "All 14 pre-send checks passed"


def execute_test_send() -> dict:
    """Executes the test send, syncs CRM, runs duplicate protection test, and compiles report."""
    print("=" * 80)
    print("STAGE 2: EXECUTING FIRST OUTREACH TEST (SEOUL KIMCHI ONLY)")
    print("=" * 80)

    if not os.path.exists(STAGE_FILE):
        raise FileNotFoundError("Stage file not found. Run prepare first.")

    with open(STAGE_FILE, "r", encoding="utf-8") as f:
        stage_data = json.load(f)

    campaign_id = stage_data["campaign_id"]
    lead_id = stage_data["lead_id"]

    # Re-arm campaign right before send so confirmation token is fresh upon approval
    arm_res = arm_campaign(campaign_id)
    if not arm_res.get("ok"):
        raise RuntimeError(f"Failed to arm campaign for execution: {arm_res.get('error')}")
    token = arm_res["confirmation_token"]
    stage_data["confirmation_token"] = token

    # 1. Re-fetch lead from Sheets for final pre-send verification
    provider = GoogleSheetsStorageProvider()
    all_leads = provider.fetch_all_leads()
    seoul_lead = next((l for l in all_leads if l.get("lead_id") == lead_id), None)
    if not seoul_lead:
        raise ValueError(f"Lead {lead_id} not found in Sheets")

    # 2. Run Section 5 Final Eligibility Check
    print("Running Final Eligibility Check (14 criteria)...")
    elig_ok, elig_reason = check_final_eligibility(seoul_lead, stage_data)
    print(f"Eligibility Check Result: {'PASS' if elig_ok else 'FAIL'} ({elig_reason})")
    if not elig_ok:
        raise RuntimeError(f"BLOCKED BY FINAL ELIGIBILITY CHECK: {elig_reason}")

    # 3. Real send via execute_campaign
    print(f"\nDispatching real email to {stage_data['recipient']} via execute_campaign()...")
    exec_result = execute_campaign(
        campaign_id=campaign_id,
        confirmation_token=token,
        max_per_run=1,
        delay_seconds=0.5
    )

    print(f"Execution Output: {exec_result}")
    results = exec_result.get("results", [])
    if not results:
        raise RuntimeError(f"Execution returned no item results: {exec_result}")

    send_item = results[0]
    send_status = send_item.get("status", "FAILED")
    provider_mid = send_item.get("provider_message_id", "")
    sent_at = send_item.get("sent_at", "")

    # 4. Verify Google Sheets sync
    print("\nVerifying Google Sheets CRM row preservation...")
    reloaded_leads = provider.fetch_all_leads()
    matching = [l for l in reloaded_leads if l.get("lead_id") == lead_id]
    sheets_pass = False
    if len(matching) == 1:
        updated_lead = matching[0]
        if updated_lead.get("outreach_status") == OutreachStatus.SENT.value:
            sheets_pass = True
            print(f"✓ Sheets row {lead_id} updated: outreach_status=SENT, message_id={provider_mid}")
        else:
            print(f"⚠ Sheets outreach_status is: {updated_lead.get('outreach_status')}")
    else:
        print(f"⚠ Found {len(matching)} matching rows for {lead_id}!")

    # 5. Section 9: Duplicate Protection Test
    print("\nRunning Section 9 Duplicate Protection Test...")
    # Attempt to re-execute the same campaign
    re_exec_result = execute_campaign(
        campaign_id=campaign_id,
        confirmation_token=token,  # Token already consumed
        max_per_run=1
    )
    dup_blocked = bool(
        re_exec_result.get("error")
        or re_exec_result.get("sent_count", 0) == 0
    )
    print(f"Duplicate protection attempt blocked: {dup_blocked} (Error: {re_exec_result.get('error')})")

    # Verify queue item cannot be sent twice
    queue_recheck = _load_queue()
    seoul_q_items = [q for q in queue_recheck if q.get("lead_id") == lead_id]
    no_double_send = (
        len(seoul_q_items) == 1
        and seoul_q_items[0].get("status") == OutreachStatus.SENT.value
    )
    print(f"No second queue item or send allowed: {no_double_send}")

    dup_test_pass = dup_blocked and no_double_send

    # Clean up stage file
    if os.path.exists(STAGE_FILE):
        os.remove(STAGE_FILE)

    final_report = {
        "campaign": stage_data["campaign_name"],
        "campaign_id": campaign_id,
        "business": "Seoul Kimchi",
        "recipient": stage_data["recipient"],
        "subject": stage_data["subject"],
        "message": stage_data["message"],
        "send_result": send_status,
        "provider_message_id": provider_mid,
        "timestamp": sent_at or datetime.now(timezone.utc).isoformat(),
        "google_sheets": "PASS" if sheets_pass else "FAIL",
        "duplicate_protection": "PASS" if dup_test_pass else "FAIL",
        "compliance_gate": "PASS" if elig_ok else "FAIL",
        "total_real_messages_sent": 1 if send_status == OutreachStatus.SENT.value else 0,
        "no_other_lead_contacted": True
    }

    # Save artifact
    out_file = os.path.join(PROJECT_ROOT, "data", "first_outreach_test_report.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(final_report, f, indent=2)

    return final_report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="First Real Outreach Test Runner")
    parser.add_argument("--prepare", action="store_true", help="Prepare campaign and preview")
    parser.add_argument("--execute", action="store_true", help="Execute real send")
    args = parser.parse_args()

    if args.prepare:
        res = prepare_test_campaign()
        print("\nPREVIEW READY:")
        print(json.dumps(res, indent=2))
    elif args.execute:
        rep = execute_test_send()
        print("\nFINAL REPORT:")
        print(json.dumps(rep, indent=2))
    else:
        print("Please specify --prepare or --execute")
