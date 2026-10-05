import sys
import os
import json

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from lib.outreach.execution_gate import mark_campaign_previewed, arm_campaign
from lib.outreach.campaign_executor import execute_campaign, get_campaign_execution_status

def main():
    campaign_id = "OUT-MAN-2026-003"
    print(f"Executing Campaign Pilot for {campaign_id}...")

    # 1. Mark previewed
    prev_res = mark_campaign_previewed(campaign_id)
    print(f"Mark previewed: {prev_res}")

    # 2. Arm campaign
    arm_res = arm_campaign(campaign_id)
    print(f"Arm campaign: {arm_res}")
    token = arm_res.get("confirmation_token") or arm_res.get("token")
    if not token:
        print("Failed to get token!")
        return

    # 3. Execute campaign
    print(f"Dispatching send with confirmation token: {token[:8]}...")
    exec_res = execute_campaign(
        campaign_id=campaign_id,
        confirmation_token=token,
        max_per_run=3,
        delay_seconds=1.0
    )
    print("Execution Finished. Result:")
    print(json.dumps(exec_res, indent=2, default=str))

    # 4. Status
    status = get_campaign_execution_status(campaign_id)
    print("Final Status:")
    print(json.dumps(status, indent=2, default=str))

if __name__ == "__main__":
    main()
