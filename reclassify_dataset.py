#!/usr/bin/env python3
"""
Reclassify Current Dataset Script for Qualification Engine V2
Loads current records from Google Sheets (LEADS, REVIEW_QUEUE, RESEARCH_LOG),
re-evaluates them using Qualification Engine V2, determines any state transitions,
syncs the 3 tabs cleanly, and outputs the exact state change summary.
"""

import sys
import re
from typing import Dict, Any, List, Tuple
from datetime import datetime

from lib.types import (
    DiscoveredBusiness,
    VerificationStatus,
    QualificationState,
    CountryStatus,
    Priority,
    LeadStatus,
    SocialOwnershipStatus,
    SocialActivityStatus,
    SocialProfileStatus,
    OperationalStatus,
    OperationalConfidence,
    EvidenceFreshness,
    Lead,
    ResearchLogEntry
)
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.sheets.google_sheets import (
    GoogleSheetsStorageProvider,
    LEADS_COLUMNS,
    REVIEW_QUEUE_COLUMNS,
    RESEARCH_LOG_COLUMNS,
    col_to_letter
)
from lib.outreach.outreach_generator import OutreachAngleGenerator

def parse_int(val, default=0):
    try:
        if val is None or val == "":
            return default
        # handle commas e.g. "6,155"
        val_str = str(val).replace(",", "").strip()
        return int(float(val_str))
    except Exception:
        return default

def parse_float(val, default=0.0):
    try:
        if val is None or val == "":
            return default
        val_str = str(val).replace(",", "").strip()
        return float(val_str)
    except Exception:
        return default

def main():
    print("=" * 60)
    print("DRIPP MEDIA - RECLASSIFY CURRENT DATASET (QUALIFICATION ENGINE V2)")
    print("=" * 60)

    storage = GoogleSheetsStorageProvider()
    scorer = LeadScoringProvider()
    outreach = OutreachAngleGenerator()

    # 1. Fetch all records from Google Sheets
    print("\n[Step 1] Fetching existing records from Google Sheets...")
    existing_leads = storage.fetch_all_leads()
    existing_reviews = storage.fetch_review_queue()
    existing_research = storage.fetch_research_log()

    print(f"  Existing LEADS tab: {len(existing_leads)} rows")
    print(f"  Existing REVIEW_QUEUE tab: {len(existing_reviews)} rows")
    print(f"  Existing RESEARCH_LOG tab: {len(existing_research)} rows")

    total_records = len(existing_leads) + len(existing_reviews) + len(existing_research)
    print(f"  Total records fetched: {total_records}")

    # Track transitions: {biz_name: (old_state, new_state, reason)}
    state_changes: List[Dict[str, Any]] = []

    # Map existing records to normalized identity to preserve lead_ids and prevent duplicates
    # Priority of identity: LEADS -> REVIEW_QUEUE -> RESEARCH_LOG
    biz_registry: Dict[str, Dict[str, Any]] = {}

    def register_row(row: Dict[str, Any], origin_source: str):
        c_name = str(row.get("company_name", "")).strip()
        if not c_name:
            return
        city = str(row.get("city", "")).strip() or "Manchester"
        country = str(row.get("target_country", "") or row.get("country", "")).strip() or "United Kingdom"
        web = str(row.get("website", "")).strip()

        norm_id = storage.normalize_identity(c_name, city, country, web)
        if norm_id not in biz_registry:
            biz_registry[norm_id] = {
                "raw": row,
                "origin_source": origin_source,
                "lead_id": row.get("lead_id") or row.get("research_id") or "",
                "old_state": row.get("qualification_state") or (
                    "OUTREACH_READY" if origin_source == "LEADS" else
                    "MANUAL_REVIEW" if origin_source == "REVIEW_QUEUE" else
                    row.get("website_status") or "EXCLUDED"
                )
            }
        else:
            # If already registered, prefer the one from LEADS or REVIEW_QUEUE for richer metadata
            if origin_source == "LEADS" and biz_registry[norm_id]["origin_source"] != "LEADS":
                biz_registry[norm_id]["raw"] = row
                biz_registry[norm_id]["origin_source"] = origin_source
                biz_registry[norm_id]["lead_id"] = row.get("lead_id") or biz_registry[norm_id]["lead_id"]
                biz_registry[norm_id]["old_state"] = "OUTREACH_READY"

    for r in existing_leads:
        register_row(r, "LEADS")
    for r in existing_reviews:
        register_row(r, "REVIEW_QUEUE")
    for r in existing_research:
        register_row(r, "RESEARCH_LOG")

    print(f"\n[Step 2] Reclassifying {len(biz_registry)} unique businesses...")

    new_outreach_leads: List[Lead] = []
    new_review_leads: List[Lead] = []
    new_research_entries: List[ResearchLogEntry] = []

    counts = {
        "OUTREACH_READY": 0,
        "MANUAL_REVIEW": 0,
        "RESEARCH_ONLY": 0,
        "EXCLUDED": 0
    }

    for norm_id, item in biz_registry.items():
        row = item["raw"]
        old_state = item["old_state"]
        c_name = str(row.get("company_name", "")).strip()
        city = str(row.get("city", "")).strip() or "Manchester"
        country = str(row.get("target_country", "") or row.get("country", "")).strip() or "United Kingdom"
        industry = str(row.get("industry", "")).strip() or "Restaurant"
        revs = parse_int(row.get("review_count"))
        rating = parse_float(row.get("rating"))
        address = str(row.get("address", "")).strip()
        phone = str(row.get("phone", "")).strip()
        if phone.startswith("'"):
            phone = phone[1:]
        website = str(row.get("website", "")).strip()
        
        # Social links
        ig = str(row.get("instagram_url", "")).strip()
        fb = str(row.get("facebook_url", "")).strip()
        tt = str(row.get("tiktok_url", "")).strip()
        gmaps = str(row.get("google_maps_url", "") or row.get("source_url", "")).strip()

        # Determine verification status
        v_status = str(row.get("verification_status", "")).strip()
        w_status = str(row.get("website_status", "")).strip()
        
        # If record was in LEADS or REVIEW_QUEUE with confirmed no website:
        if item["origin_source"] in ["LEADS", "REVIEW_QUEUE"] or v_status == VerificationStatus.NO_WEBSITE_CONFIRMED.value or w_status == VerificationStatus.NO_WEBSITE_CONFIRMED.value:
            cur_v_status = VerificationStatus.NO_WEBSITE_CONFIRMED.value
        elif "broken" in v_status.lower() or "broken" in w_status.lower() or "WEBSITE_BROKEN" in old_state:
            cur_v_status = VerificationStatus.WEBSITE_BROKEN.value
        elif "unclear" in v_status.lower() or "unclear" in w_status.lower():
            cur_v_status = VerificationStatus.WEBSITE_UNCLEAR.value
        elif website and not any(s in website.lower() for s in ["instagram.com", "facebook.com", "tiktok.com"]):
            cur_v_status = VerificationStatus.WEBSITE_EXISTS.value
        elif "WEBSITE_EXISTS" in old_state or "EXISTS" in w_status:
            cur_v_status = VerificationStatus.WEBSITE_EXISTS.value
        else:
            cur_v_status = VerificationStatus.NO_WEBSITE_CONFIRMED.value

        # Country status
        c_status = str(row.get("country_status", "")).strip()
        if not c_status:
            c_status = CountryStatus.COUNTRY_MATCH.value if ("United Kingdom" in country or "UK" in country or "Manchester" in city) else CountryStatus.COUNTRY_MISMATCH.value

        biz = DiscoveredBusiness(
            company_name=c_name,
            category=industry,
            city=city,
            target_country=country,
            detected_country=str(row.get("detected_country", "")).strip() or country,
            country_status=c_status,
            address=address,
            phone=phone,
            raw_website=website,
            google_maps_url=gmaps,
            instagram_url=ig,
            facebook_url=fb,
            tiktok_url=tt,
            review_count=revs,
            rating=rating,
            places_count=parse_int(row.get("places_count"), 1)
        )

        v_reason = str(row.get("verification_reason", "")).strip() or "Verified in prior research run"
        audit = scorer.evaluate_lead(biz, cur_v_status, v_reason)

        new_state = audit["qualification_state"]
        new_score = audit["score"]
        new_priority = audit["priority"]
        new_reason = audit["qualification_reason"]
        q_signals = audit["signals"]
        social_status = audit["social_status"]
        social_ownership = audit["social_ownership_status"]
        social_profile_status = audit.get("social_profile_status", SocialProfileStatus.UNKNOWN.value)
        social_activity = audit["social_activity"]
        operational_status = audit.get("operational_status", OperationalStatus.OPERATIONAL_UNKNOWN.value)
        operational_confidence = audit.get("operational_confidence", OperationalConfidence.LOW.value)
        operational_evidence = audit.get("operational_evidence", "")
        evidence_freshness = audit.get("evidence_freshness", EvidenceFreshness.UNKNOWN.value)
        red_flags_str = "; ".join(audit["red_flags"]) if audit["red_flags"] else ""
        qual_signals_str = ", ".join([k for k, v in q_signals.items() if v and k != "no_website_confirmed"])

        counts[new_state] += 1

        # Check if state changed
        if old_state != new_state:
            state_changes.append({
                "business": c_name,
                "old_state": old_state,
                "new_state": new_state,
                "reason": new_reason
            })

        # Generate outreach angle if OUTREACH_READY or MANUAL_REVIEW
        outreach_text = outreach.generate_angle(
            biz, new_priority, q_signals, v_reason,
            verified_socials=audit.get("verified_social_urls", {})
        )

        existing_id = item["lead_id"]
        today_str = str(row.get("date_added", "") or row.get("date_researched", "")).strip() or datetime.now().strftime("%Y-%m-%d")

        if new_state == QualificationState.OUTREACH_READY.value:
            lead_id = existing_id if (existing_id and existing_id.startswith("LEAD-")) else f"LEAD-MAN-{abs(hash(c_name)) % 1000000:06X}"
            lead = Lead(
                lead_id=lead_id,
                company_name=c_name,
                industry=industry,
                target_country=country,
                country=country,
                city=city,
                region=str(row.get("region", "")).strip(),
                postcode=str(row.get("postcode", "")).strip(),
                address=address,
                phone=phone,
                website="",
                website_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
                verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
                verification_reason=v_reason,
                google_maps_url=gmaps,
                instagram_url=ig,
                facebook_url=fb,
                tiktok_url=tt,
                review_count=revs,
                rating=rating,
                social_status=social_status,
                social_ownership_status=social_ownership,
                social_profile_status=social_profile_status,
                social_activity=social_activity,
                operational_status=operational_status,
                operational_confidence=operational_confidence,
                operational_evidence=operational_evidence,
                evidence_freshness=evidence_freshness,
                multiple_locations="Yes" if q_signals.get("multiple_locations") else "No",
                business_activity_signal=f"Reviews: {revs}, Rating: {rating}",
                qualification_signals=qual_signals_str,
                lead_score=new_score,
                priority=new_priority,
                qualification_state=new_state,
                qualification_reason=new_reason,
                red_flags=red_flags_str,
                outreach_angle=outreach_text,
                lead_status=str(row.get("lead_status", LeadStatus.NOT_CONTACTED.value)).strip() or LeadStatus.NOT_CONTACTED.value,
                date_added=today_str,
                score_breakdown=audit["score_breakdown"]
            )
            new_outreach_leads.append(lead)

            # Also create research log record for complete audit trail
            res_entry = ResearchLogEntry(
                research_id=f"RES-{abs(hash(c_name)) % 1000000:06X}",
                company_name=c_name,
                industry=industry,
                target_country=country,
                detected_country=country,
                country_status=c_status,
                city=city,
                region=str(row.get("region", "")).strip(),
                postcode=str(row.get("postcode", "")).strip(),
                address=address,
                phone=phone,
                website="",
                website_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
                verification_status=VerificationStatus.NO_WEBSITE_CONFIRMED.value,
                review_count=revs,
                rating=rating,
                social_status=social_status,
                social_ownership_status=social_ownership,
                social_profile_status=social_profile_status,
                operational_status=operational_status,
                operational_confidence=operational_confidence,
                evidence_freshness=evidence_freshness,
                qualification_state=new_state,
                qualification_status="QUALIFIED",
                disqualification_reason="",
                red_flags=red_flags_str,
                source_url=gmaps,
                date_researched=today_str
            )
            new_research_entries.append(res_entry)

        elif new_state == QualificationState.MANUAL_REVIEW.value:
            lead_id = existing_id if (existing_id and existing_id.startswith("REV-")) else f"REV-MAN-{abs(hash(c_name)) % 1000000:06X}"
            rev_lead = Lead(
                lead_id=lead_id,
                company_name=c_name,
                industry=industry,
                target_country=country,
                country=country,
                city=city,
                region=str(row.get("region", "")).strip(),
                postcode=str(row.get("postcode", "")).strip(),
                address=address,
                phone=phone,
                website="",
                website_status=cur_v_status,
                verification_status=cur_v_status,
                verification_reason=v_reason,
                google_maps_url=gmaps,
                instagram_url=ig,
                facebook_url=fb,
                tiktok_url=tt,
                review_count=revs,
                rating=rating,
                social_status=social_status,
                social_ownership_status=social_ownership,
                social_profile_status=social_profile_status,
                social_activity=social_activity,
                operational_status=operational_status,
                operational_confidence=operational_confidence,
                operational_evidence=operational_evidence,
                evidence_freshness=evidence_freshness,
                multiple_locations="Yes" if q_signals.get("multiple_locations") else "No",
                business_activity_signal=f"Reviews: {revs}, Rating: {rating}",
                qualification_signals=qual_signals_str,
                lead_score=new_score,
                priority=Priority.MANUAL_REVIEW.value,
                qualification_state=new_state,
                qualification_reason=new_reason,
                red_flags=red_flags_str,
                outreach_angle=outreach_text,
                lead_status=str(row.get("lead_status", LeadStatus.NOT_CONTACTED.value)).strip() or LeadStatus.NOT_CONTACTED.value,
                date_added=today_str,
                score_breakdown=audit["score_breakdown"]
            )
            new_review_leads.append(rev_lead)

            res_entry = ResearchLogEntry(
                research_id=f"RES-{abs(hash(c_name)) % 1000000:06X}",
                company_name=c_name,
                industry=industry,
                target_country=country,
                detected_country=country,
                country_status=c_status,
                city=city,
                region=str(row.get("region", "")).strip(),
                postcode=str(row.get("postcode", "")).strip(),
                address=address,
                phone=phone,
                website=website,
                website_status=cur_v_status,
                verification_status=cur_v_status,
                review_count=revs,
                rating=rating,
                social_status=social_status,
                social_ownership_status=social_ownership,
                social_profile_status=social_profile_status,
                operational_status=operational_status,
                operational_confidence=operational_confidence,
                evidence_freshness=evidence_freshness,
                qualification_state=new_state,
                qualification_status="MANUAL_REVIEW",
                disqualification_reason=new_reason,
                red_flags=red_flags_str,
                source_url=gmaps,
                date_researched=today_str
            )
            new_research_entries.append(res_entry)

        elif new_state == QualificationState.RESEARCH_ONLY.value:
            res_entry = ResearchLogEntry(
                research_id=f"RES-{abs(hash(c_name)) % 1000000:06X}",
                company_name=c_name,
                industry=industry,
                target_country=country,
                detected_country=country,
                country_status=c_status,
                city=city,
                region=str(row.get("region", "")).strip(),
                postcode=str(row.get("postcode", "")).strip(),
                address=address,
                phone=phone,
                website=website,
                website_status=cur_v_status,
                verification_status=cur_v_status,
                review_count=revs,
                rating=rating,
                social_status=social_status,
                social_ownership_status=social_ownership,
                social_profile_status=social_profile_status,
                operational_status=operational_status,
                operational_confidence=operational_confidence,
                evidence_freshness=evidence_freshness,
                qualification_state=new_state,
                qualification_status="RESEARCH_ONLY",
                disqualification_reason=new_reason,
                red_flags=red_flags_str,
                source_url=gmaps,
                date_researched=today_str
            )
            new_research_entries.append(res_entry)

        else: # EXCLUDED
            res_entry = ResearchLogEntry(
                research_id=f"RES-{abs(hash(c_name)) % 1000000:06X}",
                company_name=c_name,
                industry=industry,
                target_country=country,
                detected_country=country,
                country_status=c_status,
                city=city,
                region=str(row.get("region", "")).strip(),
                postcode=str(row.get("postcode", "")).strip(),
                address=address,
                phone=phone,
                website=website,
                website_status=cur_v_status,
                verification_status=cur_v_status,
                review_count=revs,
                rating=rating,
                social_status=social_status,
                social_ownership_status=social_ownership,
                social_profile_status=social_profile_status,
                operational_status=operational_status,
                operational_confidence=operational_confidence,
                evidence_freshness=evidence_freshness,
                qualification_state=new_state,
                qualification_status="EXCLUDED",
                disqualification_reason=new_reason,
                red_flags=red_flags_str,
                source_url=gmaps,
                date_researched=today_str
            )
            new_research_entries.append(res_entry)

    # 3. Synchronize cleanly with Google Sheets
    print("\n[Step 3] Syncing 3 tabs in Google Sheets...")

    # Clear and rewrite LEADS tab
    print(f"  Writing {len(new_outreach_leads)} OUTREACH_READY leads to LEADS...")
    storage.leads_worksheet.clear()
    storage.ensure_leads_columns()
    if new_outreach_leads:
        lead_rows = [lead.to_sheet_row(LEADS_COLUMNS) for lead in new_outreach_leads]
        end_col = col_to_letter(len(LEADS_COLUMNS))
        storage.leads_worksheet.update(values=lead_rows, range_name=f"A2:{end_col}{len(lead_rows)+1}", value_input_option="USER_ENTERED")

    # Clear and rewrite REVIEW_QUEUE tab
    print(f"  Writing {len(new_review_leads)} MANUAL_REVIEW candidates to REVIEW_QUEUE...")
    storage.review_queue_worksheet.clear()
    storage.ensure_review_queue_columns()
    if new_review_leads:
        rev_rows = [lead.to_sheet_row(REVIEW_QUEUE_COLUMNS) for lead in new_review_leads]
        end_col = col_to_letter(len(REVIEW_QUEUE_COLUMNS))
        storage.review_queue_worksheet.update(values=rev_rows, range_name=f"A2:{end_col}{len(rev_rows)+1}", value_input_option="USER_ENTERED")

    # Clear and rewrite RESEARCH_LOG tab
    print(f"  Writing {len(new_research_entries)} records to RESEARCH_LOG...")
    storage.research_worksheet.clear()
    storage.ensure_research_columns()
    if new_research_entries:
        res_rows = [entry.to_sheet_row(RESEARCH_LOG_COLUMNS) for entry in new_research_entries]
        end_col = col_to_letter(len(RESEARCH_LOG_COLUMNS))
        storage.research_worksheet.update(values=res_rows, range_name=f"A2:{end_col}{len(res_rows)+1}", value_input_option="USER_ENTERED")

    print("\n[Step 4] Reclassification Complete!")
    print("-" * 60)
    print(f"Current dataset size: {len(biz_registry)}")
    print(f"OUTREACH_READY: {counts['OUTREACH_READY']}")
    print(f"MANUAL_REVIEW: {counts['MANUAL_REVIEW']}")
    print(f"RESEARCH_ONLY: {counts['RESEARCH_ONLY']}")
    print(f"EXCLUDED: {counts['EXCLUDED']}")
    print("-" * 60)

    print("\nState Changes Identified:")
    if state_changes:
        print(f"{'Business':<32} | {'Old State':<16} | {'New State':<16} | {'Reason'}")
        print("-" * 100)
        for sc in state_changes:
            print(f"{sc['business']:<32} | {sc['old_state']:<16} | {sc['new_state']:<16} | {sc['reason'][:50]}")
    else:
        print("No unexpected state changes; all records conform to strict rules.")

if __name__ == "__main__":
    main()
