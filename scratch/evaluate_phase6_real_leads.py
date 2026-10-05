"""
Phase 6: Real Leads Contactability Evaluation (Dry-Run Only)
============================================================
Evaluates real leads without any CRM mutation, email sends, or social DM sends:
  - Manchester OUTREACH_READY cohort:
      1. Mary D's Beamish Bar
      2. Mala
      3. Manchester Shawarma
      4. Seoul Kimchi
      5. Hong Thai
      6. 99 Reasons
  - Leeds OUTREACH_READY cohort:
      7. WinneBagel Cafe
  - Leeds MANUAL_REVIEW cohort (verify contactability NEVER promotes qualification):
      8. Casa Di Alessia
      9. Family Fortune
     10. Il Vicoletto
"""

import os
import sys
import json

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from lib.outreach.contactability import ContactabilityAssessor, ContactabilityState, ChannelStatus, ComplianceState
from lib.outreach.email_enricher import MXStatus, BusinessDomainType, EmailVerificationStatus


def evaluate_cohort():
    # 1. Load Manchester Leads from cache
    cache_file = os.path.join(PROJECT_ROOT, "data", "cache_sheets_leads.json")
    with open(cache_file, "r", encoding="utf-8") as f:
        mcr_data = json.load(f)
        mcr_leads = mcr_data.get("leads", [])

    # Filter to the requested 6
    requested_mcr = [
        "Mary D's Beamish Bar",
        "Mala",
        "Manchester Shawarma",
        "Seoul Kimchi",
        "Hong Thai",
        "99 Reasons",
    ]
    target_leads = []
    for name in requested_mcr:
        found = next((l for l in mcr_leads if l.get("company_name") == name), None)
        if found:
            target_leads.append(found)

    # 2. Add WinneBagel Cafe from Leeds benchmark results
    leeds_results_file = os.path.join(PROJECT_ROOT, "data", "leeds_lead_supply_test_results.json")
    with open(leeds_results_file, "r", encoding="utf-8") as f:
        leeds_data = json.load(f)
        leeds_candidates = leeds_data.get("candidate_details", [])

    winnebagel = next((l for l in leeds_candidates if "WinneBagel" in l.get("company_name", "")), None)
    if winnebagel:
        wb_copy = dict(winnebagel)
        wb_copy["lead_id"] = "LEAD-LDS-WINNEBAGEL"
        wb_copy["target_country"] = "United Kingdom"
        wb_copy["country"] = "United Kingdom"
        wb_copy["priority"] = wb_copy.get("priority", "HIGH")
        wb_copy["outreach_status"] = "NOT_READY"
        target_leads.append(wb_copy)

    # 3. Add Leeds MANUAL_REVIEW leads
    manual_review_names = ["Casa Di Alessia", "Family Fortune", "Il Vicoletto"]
    for mr_name in manual_review_names:
        cand = next((l for l in leeds_candidates if mr_name.lower() in l.get("company_name", "").lower()), None)
        if cand:
            c_copy = dict(cand)
            c_copy["lead_id"] = f"LEAD-LDS-{mr_name.upper().replace(' ', '-')}"
            c_copy["target_country"] = "United Kingdom"
            c_copy["country"] = "United Kingdom"
            c_copy["priority"] = c_copy.get("priority", "MEDIUM")
            c_copy["outreach_status"] = "NOT_READY"
            target_leads.append(c_copy)

    print("=" * 120)
    print("PHASE 6: REAL LEADS CONTACTABILITY EVALUATION (DRY-RUN)")
    print(f"Total leads loaded: {len(target_leads)}")
    print("=" * 120)

    results = []

    for idx, lead in enumerate(target_leads, 1):
        name = lead.get("company_name", "Unknown")
        qual_before = lead.get("qualification_state", "UNKNOWN")
        priority_before = lead.get("priority", "UNKNOWN")
        op_status_before = lead.get("operational_status", "UNKNOWN")

        # Run assessment
        assessment = ContactabilityAssessor.assess_lead(lead)
        d = assessment.to_dict()

        # Invariant checks: ensure lead dict was NOT mutated
        assert lead.get("qualification_state") == qual_before, "CRITICAL: lead qualification_state was mutated!"
        assert lead.get("operational_status") == op_status_before, "CRITICAL: lead operational_status was mutated!"

        # Extract required fields from assessment
        email_cand = assessment.email_data or {}
        ch_evals = assessment.channels
        ig_eval = ch_evals.get("Instagram Direct Message")
        fb_eval = ch_evals.get("Facebook Messenger")
        email_eval = ch_evals.get("Email")

        item = {
            "business": name,
            "qualification_state": lead.get("qualification_state", "UNKNOWN"),
            "priority": lead.get("priority", "UNKNOWN"),
            "email": email_cand.get("email", ""),
            "email_status": getattr(email_eval, "status", ChannelStatus.UNKNOWN.value),
            "email_source": email_cand.get("source", "NONE"),
            "mx_status": email_cand.get("mx_status", MXStatus.UNKNOWN.value),
            "instagram_url": lead.get("instagram_url", ""),
            "instagram_business_owned": getattr(ig_eval, "business_owned", False),
            "instagram_recipient_id_available": getattr(ig_eval, "recipient_id_available", False),
            "instagram_sendable": getattr(ig_eval, "sendable", False),
            "facebook_url": lead.get("facebook_url", ""),
            "facebook_business_owned": getattr(fb_eval, "business_owned", False),
            "facebook_recipient_id_available": getattr(fb_eval, "recipient_id_available", False),
            "facebook_sendable": getattr(fb_eval, "sendable", False),
            "manual_contactable": assessment.manual_contactable,
            "automated_contactable": assessment.automated_contactable,
            "compliance_status": assessment.compliance_status,
            "contactability_state": assessment.contactability_status,
            "outreach_status": lead.get("outreach_status", "NOT_READY"),
            "reason": assessment.contactability_reason
        }
        results.append(item)

        print(f"[{idx:02d}] {name.ljust(25)} | Qual: {item['qualification_state'].ljust(14)} | Contactability: {item['contactability_state'].ljust(22)} | Manual: {str(item['manual_contactable']).ljust(5)} | Auto: {str(item['automated_contactable']).ljust(5)}")

    # Print Detailed Matrix Table
    print("\n" + "=" * 120)
    print("DETAILED CONTACTABILITY MATRIX:")
    print("=" * 120)
    for r in results:
        print(f"\nBusiness: {r['business']}")
        print(f"  qualification_state:               {r['qualification_state']}")
        print(f"  priority:                          {r['priority']}")
        print(f"  email:                             {r['email'] or 'NONE'}")
        print(f"  email_status:                      {r['email_status']}")
        print(f"  email_source:                      {r['email_source']}")
        print(f"  mx_status:                         {r['mx_status']}")
        print(f"  instagram_url:                     {r['instagram_url'] or 'NONE'}")
        print(f"  instagram_business_owned:          {r['instagram_business_owned']}")
        print(f"  instagram_recipient_id_available:  {r['instagram_recipient_id_available']}")
        print(f"  instagram_sendable:                {r['instagram_sendable']}")
        print(f"  facebook_url:                      {r['facebook_url'] or 'NONE'}")
        print(f"  facebook_business_owned:           {r['facebook_business_owned']}")
        print(f"  facebook_recipient_id_available:   {r['facebook_recipient_id_available']}")
        print(f"  facebook_sendable:                 {r['facebook_sendable']}")
        print(f"  manual_contactable:                {r['manual_contactable']}")
        print(f"  automated_contactable:             {r['automated_contactable']}")
        print(f"  compliance_status:                 {r['compliance_status']}")
        print(f"  contactability_state:              {r['contactability_state']}")
        print(f"  outreach_status:                   {r['outreach_status']}")
        print(f"  reason:                            {r['reason']}")

    # Save to json artifact for report generation
    out_path = os.path.join(PROJECT_ROOT, "data", "phase6_real_leads_eval.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved evaluation artifact to: {out_path}")


if __name__ == "__main__":
    evaluate_cohort()
