"""
Dripp Media — Contact Enrichment & Recipient Verification Runner
================================================================
Section 21: Enriches the 7 existing Manchester OUTREACH_READY leads:
  1. Mary D's Beamish Bar
  2. Mala
  3. Waka Waka MCR
  4. Manchester Shawarma
  5. Seoul Kimchi
  6. 99 Reasons
  7. Hong Thai

Strict Rules:
  - DO NOT rediscover them.
  - DO NOT send any real messages.
  - DO NOT fabricate emails or recipient IDs.
  - DO NOT guess emails.
  - Update Google Sheets CRM with new enrichment and compliance fields.
  - DO NOT create duplicate rows in Google Sheets.
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from lib.outreach.contactability import ContactabilityAssessor, ContactabilityState, ChannelStatus, ComplianceState
from lib.outreach.email_enricher import EmailVerificationStatus, MXStatus, BusinessDomainType
from lib.outreach.compliance import ContactHistoryManager, CooldownManager
from lib.sheets.google_sheets import GoogleSheetsStorageProvider


def main():
    parser = argparse.ArgumentParser(description="Run Contact Enrichment & Verification")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Run without mutating Google Sheets CRM (default: True)")
    parser.add_argument("--live-write", action="store_true", default=False, help="Explicitly enable live Google Sheets mutations")
    parser.add_argument("--file", type=str, default="", help="Path to local JSON leads file")
    args = parser.parse_args()

    is_dry_run = not args.live_write

    print("=" * 80)
    print("DRIPP MEDIA — RUNNING CONTACT ENRICHMENT ON QUALIFIED LEADS (PHASE 6)")
    print(f"MODE: {'DRY-RUN (0 CRM Mutations, 0 Sends)' if is_dry_run else 'LIVE CRM WRITE'}")
    print("=" * 80)

    if args.file and os.path.exists(args.file):
        with open(args.file, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
            leads = raw_data.get("leads", raw_data) if isinstance(raw_data, dict) else raw_data
        print(f"Loaded {len(leads)} leads from file: {args.file}")
    else:
        provider = GoogleSheetsStorageProvider()
        leads = provider.fetch_all_leads()
        print(f"Loaded {len(leads)} leads from Google Sheets LEADS tab.")

    enriched_reports = []


    for idx, lead in enumerate(leads, 1):
        name = lead.get("company_name", "Unknown")
        lead_id = lead.get("lead_id", "")
        print(f"\n[{idx}/7] Enriching Contact & Recipient Data for: {name} ({lead_id})")

        assessment = ContactabilityAssessor.assess_lead(lead)
        d = assessment.to_dict()

        # Extract fields for Google Sheets update
        email_cand = assessment.email_data or {}
        comp_data = assessment.compliance_data or {}
        entity_data = assessment.entity_data or {}
        ch_evals = assessment.channels

        enrich_data = assessment.email_enrichment_data or {}

        updates = {
            "trading_name": entity_data.get("trading_name", name),
            "legal_entity_name": entity_data.get("legal_entity_name", entity_data.get("registered_name", "")),
            "registered_name": entity_data.get("legal_entity_name", entity_data.get("registered_name", "")),
            "entity_source": entity_data.get("entity_source", "PUBLIC_FALLBACK"),
            "companies_house_number": entity_data.get("companies_house_number", ""),
            "company_type": entity_data.get("company_type", ""),
            "company_status": entity_data.get("company_status", ""),
            "registered_office": entity_data.get("registered_office", ""),
            "sic_codes": ", ".join(entity_data.get("sic_codes", [])),
            "companies_house_url": entity_data.get("companies_house_url", ""),
            "entity_match_status": entity_data.get("entity_match_status") or entity_data.get("match_status", "NO_MATCH"),
            "entity_match_confidence": entity_data.get("entity_match_confidence") or entity_data.get("match_confidence", "UNKNOWN"),
            "entity_match_reason": entity_data.get("entity_match_reason") or entity_data.get("match_reason", ""),
            "contactability_status": assessment.contactability_status,
            "contactability_reason": assessment.contactability_reason,
            "recipient_verified": str(any(c.recipient_verified for c in ch_evals.values())),
            "recipient_confidence": email_cand.get("confidence", "UNKNOWN"),
            "contact_history_count": str(ContactHistoryManager.get_contact_count(lead_id)),
            "email": email_cand.get("email", ""),
            "primary_email": email_cand.get("email", ""),
            "email_candidates": json.dumps([c.get("email") for c in enrich_data.get("candidates", [])] if enrich_data.get("candidates") else []),
            "email_type": email_cand.get("email_type", "UNKNOWN"),
            "email_source": email_cand.get("source", "NONE"),
            "email_source_url": email_cand.get("source_url", ""),
            "email_source_type": email_cand.get("source_type", email_cand.get("source", "NONE")),
            "email_source_context": email_cand.get("source_context", ""),
            "email_verification_status": email_cand.get("verification_status", "NONE"),
            "email_confidence": email_cand.get("confidence", "UNKNOWN"),
            "email_discovered_at": email_cand.get("discovered_at", ""),
            "email_last_verified_at": email_cand.get("last_verified_at", ""),
            "email_search_queries": json.dumps(enrich_data.get("search_queries", [])),
            "email_searches_performed": str(enrich_data.get("searches_performed", 0)),
            "email_search_status": enrich_data.get("search_status", "NOT_STARTED"),
            "email_search_reason": enrich_data.get("search_reason", ""),
            "website_recheck_required": str(enrich_data.get("website_recheck_required", False)),
            "subscriber_type": comp_data.get("subscriber_type", "UNKNOWN"),
            "marketing_email_status": comp_data.get("marketing_email_status", "UNKNOWN"),
            "lawful_basis_status": comp_data.get("lawful_basis_status", "NONE"),
            "opt_out_status": comp_data.get("opt_out_status", "ACTIVE"),
            "compliance_review_status": comp_data.get("compliance_review_status", "NONE"),
            "compliance_notes": comp_data.get("compliance_notes", ""),
        }

        # Cooldown check
        in_cd, cd_until = CooldownManager.is_in_cooldown(lead_id, "Email")
        if cd_until:
            updates["global_cooldown_until"] = cd_until

        # Channel selection recommendation based on router
        if assessment.sendable_channels:
            updates["selected_channel"] = assessment.sendable_channels[0]
            updates["channel_selection_reason"] = f"Auto-selected {assessment.sendable_channels[0]} (Verified & Sendable)"
        else:
            updates["selected_channel"] = "NONE"
            updates["channel_selection_reason"] = assessment.contactability_reason

        print(f"  -> Entity: {updates['legal_entity_name'] or 'NO_MATCH'} ({updates['entity_match_status']}, Conf: {updates['entity_match_confidence']}, Source: {updates['entity_source']})")
        print(f"  -> Primary Email: {updates['primary_email'] or 'NONE'} (Verification: {updates['email_verification_status']}, Conf: {updates['email_confidence']}, Source: {updates['email_source']})")
        print(f"  -> Searches: {updates['email_searches_performed']} queries | Candidates found: {len(enrich_data.get('candidates', []))} | Rejected: {enrich_data.get('emails_rejected', 0)}")
        print(f"  -> Subscriber Type: {updates['subscriber_type']} | Compliance: {updates['marketing_email_status']}")
        print(f"  -> Sendable Channels: {assessment.sendable_channels or ['NONE']}")
        print(f"  -> Contactability: {assessment.contactability_status} ({assessment.contactability_reason})")

        # Sync update to Google Sheets CRM only if live-write enabled
        if is_dry_run:
            print(f"  [DRY-RUN] Skipping Google Sheets CRM write for {lead_id} (0 mutations)")
        else:
            try:
                provider.update_lead_outreach(lead_id, updates)
                print(f"  ✓ Synced enrichment fields to Google Sheets CRM for row {lead_id}")
            except Exception as e:
                print(f"  ⚠ Google Sheets sync warning: {e}")

        ig_eval = ch_evals.get("Instagram Direct Message")
        fb_eval = ch_evals.get("Facebook Messenger")
        email_eval = ch_evals.get("Email")

        enriched_reports.append({
            "lead_id": lead_id,
            "business": name,
            "company_name": name,
            "qualification_state": lead.get("qualification_state", "UNKNOWN"),
            "priority": lead.get("priority", "UNKNOWN"),
            "trading_name": updates["trading_name"],
            "legal_entity": updates["legal_entity_name"],
            "legal_entity_name": updates["legal_entity_name"],
            "entity_match_status": updates["entity_match_status"],
            "companies_house_status": entity_data.get("company_status", "NO_MATCH"),
            "entity_confidence": updates["entity_match_confidence"],
            "entity_source": updates["entity_source"],
            "subscriber_type": comp_data.get("subscriber_type", "UNKNOWN"),
            "instagram_url": lead.get("instagram_url", ""),
            "instagram_business_owned": getattr(ig_eval, "business_owned", False),
            "instagram_recipient_id_available": getattr(ig_eval, "recipient_id_available", False),
            "instagram_sendable": getattr(ig_eval, "sendable", False),
            "instagram_status": getattr(ig_eval, "status", ChannelStatus.UNKNOWN.value),
            "facebook_url": lead.get("facebook_url", ""),
            "facebook_business_owned": getattr(fb_eval, "business_owned", False),
            "facebook_recipient_id_available": getattr(fb_eval, "recipient_id_available", False),
            "facebook_sendable": getattr(fb_eval, "sendable", False),
            "facebook_status": getattr(fb_eval, "status", ChannelStatus.UNKNOWN.value),
            "email_found": bool(email_cand.get("email")),
            "primary_email": email_cand.get("email", ""),
            "email": email_cand.get("email", ""),
            "email_status": getattr(email_eval, "status", ChannelStatus.UNKNOWN.value),
            "email_source": email_cand.get("source", "NONE"),
            "email_source_url": email_cand.get("source_url", ""),
            "mx_status": email_cand.get("mx_status", MXStatus.UNKNOWN.value),
            "email_domain_type": email_cand.get("business_domain_type", BusinessDomainType.UNKNOWN.value),
            "email_type": email_cand.get("email_type", "UNKNOWN"),
            "email_verification": email_cand.get("verification_status", "NONE"),
            "email_confidence": email_cand.get("confidence", "UNKNOWN"),
            "email_source_context": email_cand.get("source_context", ""),
            "supporting_sources_count": email_cand.get("supporting_sources_count", 1 if email_cand.get("email") else 0),
            "candidates": enrich_data.get("candidates", []),
            "searches_performed": enrich_data.get("searches_performed", 0),
            "search_queries": enrich_data.get("search_queries", []),
            "sources_checked": enrich_data.get("sources_checked", 0),
            "search_time": enrich_data.get("search_time", 0.0),
            "emails_rejected": enrich_data.get("emails_rejected", 0),
            "rejected_candidates": enrich_data.get("rejected_candidates", []),
            "website_recheck_required": enrich_data.get("website_recheck_required", False),
            "recipient_confidence": email_cand.get("confidence", "UNKNOWN"),
            "sendable_channels": assessment.sendable_channels,
            "manual_contactable": assessment.manual_contactable,
            "automated_contactable": assessment.automated_contactable,
            "compliance_status": assessment.compliance_status,
            "contactability_state": assessment.contactability_status,
            "contactability_status": assessment.contactability_status,
            "outreach_status": lead.get("outreach_status", "NOT_READY"),
            "reason": assessment.contactability_reason,
            "assessment": d
        })

    # Summary statistics
    total_leads = len(enriched_reports)
    contactable_count = sum(1 for r in enriched_reports if r["contactability_status"] == ContactabilityState.CONTACTABLE.value)
    partially_contactable_count = sum(1 for r in enriched_reports if r["contactability_status"] == ContactabilityState.PARTIALLY_CONTACTABLE.value)
    not_contactable_count = sum(1 for r in enriched_reports if r["contactability_status"] == ContactabilityState.NOT_CONTACTABLE.value)
    manual_review_count = sum(1 for r in enriched_reports if r["contactability_status"] == ContactabilityState.MANUAL_REVIEW.value)

    # Channel coverage (Phase 6 Section 20.B)
    valid_email_count = sum(1 for r in enriched_reports if r["email_verification"] == EmailVerificationStatus.VERIFIED.value)
    verified_ig_count = sum(1 for r in enriched_reports if r["instagram_business_owned"])
    automated_ig_count = sum(1 for r in enriched_reports if r["instagram_recipient_id_available"])
    verified_fb_count = sum(1 for r in enriched_reports if r["facebook_business_owned"])
    automated_fb_count = sum(1 for r in enriched_reports if r["facebook_recipient_id_available"])
    manual_only_social_count = sum(1 for r in enriched_reports if (r["instagram_business_owned"] or r["facebook_business_owned"]) and not r["automated_contactable"] and r["manual_contactable"])
    no_contact_count = sum(1 for r in enriched_reports if not r["manual_contactable"] and not r["automated_contactable"])

    chan_contactable = {
        "Instagram": sum(1 for r in enriched_reports if "Instagram Direct Message" in r["sendable_channels"]),
        "Facebook": sum(1 for r in enriched_reports if "Facebook Messenger" in r["sendable_channels"]),
        "Email": sum(1 for r in enriched_reports if "Email" in r["sendable_channels"]),
    }

    # Email Discovery Metrics
    emails_found_count = sum(1 for r in enriched_reports if r["email_found"])
    verified_emails_count = valid_email_count
    unverified_emails_count = sum(1 for r in enriched_reports if r["email_verification"] == EmailVerificationStatus.UNVERIFIED.value)
    invalid_emails_count = sum(1 for r in enriched_reports if r["email_verification"] == EmailVerificationStatus.INVALID.value)
    no_email_count = sum(1 for r in enriched_reports if not r["email_found"])

    total_searches = sum(r["searches_performed"] for r in enriched_reports)
    avg_searches = round(total_searches / total_leads, 1) if total_leads else 0.0
    total_search_time = sum(r["search_time"] for r in enriched_reports)
    avg_search_time = round(total_search_time / total_leads, 2) if total_leads else 0.0

    sources_used = set()
    total_emails_rejected = 0
    rejection_reasons = {}

    for r in enriched_reports:
        if r["email_source"] and r["email_source"] != "NONE":
            sources_used.add(r["email_source"])
        total_emails_rejected += r["emails_rejected"]
        for rej in r["rejected_candidates"]:
            reason = rej.get("reason", "UNKNOWN_REJECTION")
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

    channel_coverage = {
        "valid_email": valid_email_count,
        "verified_instagram": verified_ig_count,
        "automated_instagram_recipient": automated_ig_count,
        "verified_facebook": verified_fb_count,
        "automated_facebook_recipient": automated_fb_count,
        "manual_only_social_contact": manual_only_social_count,
        "no_contact_method": no_contact_count,
    }

    summary = {
        "total_leads": total_leads,
        "emails_found": emails_found_count,
        "verified_business_emails": verified_emails_count,
        "unverified_emails": unverified_emails_count,
        "invalid_emails": invalid_emails_count,
        "no_email_found": no_email_count,
        "contactable": contactable_count,
        "partially_contactable": partially_contactable_count,
        "not_contactable": not_contactable_count,
        "manual_review": manual_review_count,
        "channel_contactability": chan_contactable,
        "channel_coverage": channel_coverage,
        "avg_searches_per_lead": avg_searches,
        "avg_search_time_seconds": avg_search_time,
        "sources_used": sorted(list(sources_used)),
        "emails_rejected": total_emails_rejected,
        "main_rejection_reasons": rejection_reasons,
        "leads": enriched_reports
    }

    # Save summary artifact
    out_file = os.path.join(PROJECT_ROOT, "data", "contact_enrichment_results.json")
    with open(out_file, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    print("\n" + "=" * 80)
    print("PHASE 6: CONTACTABILITY FINAL REPORT")
    print("=" * 80)
    print(f"TOTAL LEADS EVALUATED: {total_leads}")
    print(f"CONTACTABLE (Automated): {contactable_count}")
    print(f"PARTIALLY CONTACTABLE (Manual): {partially_contactable_count}")
    print(f"NOT CONTACTABLE: {not_contactable_count}")
    print("-" * 80)
    print("CHANNEL COVERAGE:")
    print(f"  Valid Email: {valid_email_count}")
    print(f"  Verified Instagram: {verified_ig_count}")
    print(f"  Automated Instagram Recipient: {automated_ig_count}")
    print(f"  Verified Facebook: {verified_fb_count}")
    print(f"  Automated Facebook Recipient: {automated_fb_count}")
    print(f"  Manual-Only Social Contact: {manual_only_social_count}")
    print(f"  No Contact Method: {no_contact_count}")
    print("-" * 80)
    print("LEADS DETAIL MATRIX (Section 15):")
    for r in enriched_reports:
        print(f"\nBusiness: {r['business']}")
        print(f"  Qualification: {r['qualification_state']} | Priority: {r['priority']} | Outreach Status: {r['outreach_status']}")
        print(f"  Email: {r['email'] or 'NONE'} | Status: {r['email_status']} | Source: {r['email_source']} | MX: {r['mx_status']}")
        print(f"  Instagram URL: {r['instagram_url'] or 'NONE'} | Owned: {r['instagram_business_owned']} | RecipientID: {r['instagram_recipient_id_available']} | Sendable: {r['instagram_sendable']}")
        print(f"  Facebook URL: {r['facebook_url'] or 'NONE'} | Owned: {r['facebook_business_owned']} | RecipientID: {r['facebook_recipient_id_available']} | Sendable: {r['facebook_sendable']}")
        print(f"  Manual Contactable: {r['manual_contactable']} | Automated Contactable: {r['automated_contactable']}")
        print(f"  Compliance Status: {r['compliance_status']}")
        print(f"  Contactability State: {r['contactability_state']}")
        print(f"  Reason: {r['reason']}")
    print("=" * 80)


if __name__ == "__main__":
    main()
