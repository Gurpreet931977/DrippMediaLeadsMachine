import sys
import os
from datetime import datetime

base_dir = os.path.dirname(os.path.abspath(__file__))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from lib.types import (
    Lead,
    DiscoveredBusiness,
    ResearchLogEntry,
    VerificationStatus,
    QualificationState,
    Priority,
    CountryStatus
)
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.outreach.outreach_generator import OutreachAngleGenerator
from lib.sheets.google_sheets import (
    GoogleSheetsStorageProvider,
    LEADS_COLUMNS,
    REVIEW_QUEUE_COLUMNS,
    RESEARCH_LOG_COLUMNS,
    col_to_letter
)

def main():
    print("[MIGRATION] Starting Qualification Engine V2 Migration & Validation...")
    storage = GoogleSheetsStorageProvider()
    scorer = LeadScoringProvider()
    angle_gen = OutreachAngleGenerator()

    # 1. Fetch raw rows from LEADS worksheet
    all_raw_rows = storage.leads_worksheet.get_all_values()
    if len(all_raw_rows) <= 1:
        print("[MIGRATION] No existing rows found in LEADS worksheet to migrate.")
        return

    print(f"[MIGRATION] Found {len(all_raw_rows) - 1} records in current LEADS worksheet.")

    outreach_ready_leads = []
    manual_review_leads = []
    research_only_entries = []

    today_str = datetime.now().strftime("%Y-%m-%d")

    for idx, row in enumerate(all_raw_rows[1:], start=1):
        company_name = row[1] if len(row) > 1 else ""
        if not company_name:
            continue

        raw_phone = row[9] if len(row) > 9 else ""
        if raw_phone and raw_phone.startswith("+") and not raw_phone.startswith("'"):
            safe_phone = f"'{raw_phone}"
        else:
            safe_phone = raw_phone

        gmaps = row[14] if len(row) > 14 and "google.com/maps" in row[14] else ""
        ig = row[15] if len(row) > 15 and "instagram.com" in row[15] else ""
        fb = row[16] if len(row) > 16 and "facebook.com" in row[16] else ""
        tt = row[17] if len(row) > 17 and "tiktok.com" in row[17] else ""
        revs_str = row[18] if len(row) > 18 else "0"
        rev_count = int(revs_str) if revs_str.isdigit() else 0
        rating_str = row[19] if len(row) > 19 else "0.0"
        try:
            rating_val = float(rating_str)
        except ValueError:
            rating_val = 0.0

        ver_status = row[12] if len(row) > 12 and row[12] else VerificationStatus.NO_WEBSITE_CONFIRMED.value
        ver_reason = row[13] if len(row) > 13 else "Multi-step verification confirmed no official website exists."

        # Reconstruct DiscoveredBusiness
        biz = DiscoveredBusiness(
            company_name=company_name,
            category=row[2] if len(row) > 2 else "Restaurant",
            target_country=row[3] if len(row) > 3 else "United Kingdom",
            detected_country=row[4] if len(row) > 4 else "United Kingdom",
            city=row[5] if len(row) > 5 else "Manchester",
            region=row[6] if len(row) > 6 else "",
            postcode=row[7] if len(row) > 7 else "",
            address=row[8] if len(row) > 8 else "",
            phone=safe_phone,
            raw_website=row[10] if len(row) > 10 else "",
            google_maps_url=gmaps,
            instagram_url=ig,
            facebook_url=fb,
            tiktok_url=tt,
            review_count=rev_count,
            rating=rating_val
        )

        # Run strict V2 evaluation
        audit = scorer.evaluate_lead(
            business=biz,
            verification_status=ver_status,
            verification_reason=ver_reason
        )

        q_state = audit["qualification_state"]
        score = audit["score"]
        priority = audit["priority"]
        q_reason = audit["qualification_reason"]
        red_flags_list = audit.get("red_flags", [])
        red_flags_str = "; ".join(red_flags_list) if red_flags_list else ""
        soc_status = audit.get("social_status", "")
        soc_ownership = audit.get("social_ownership_status", "")
        soc_activity = audit.get("social_activity", "")
        verified_socials = audit.get("verified_social_urls", {})
        signals_dict = audit.get("signals", {})
        signals_str = ", ".join(audit.get("evidence", {}).get("qualification_signals", []))

        outreach_angle = angle_gen.generate_angle(
            business=biz,
            priority=priority,
            signals=signals_dict,
            verification_reason=ver_reason,
            verified_socials=verified_socials
        )

        lead_obj = Lead(
            lead_id=row[0] if len(row) > 0 and row[0] else f"LEAD-MAN-{idx:03d}",
            company_name=biz.company_name,
            industry=biz.category,
            target_country=biz.target_country,
            country=biz.detected_country or biz.target_country,
            city=biz.city,
            region=biz.region,
            postcode=biz.postcode,
            address=biz.address,
            phone=safe_phone,
            website=biz.raw_website,
            website_status=ver_status,
            verification_status=ver_status,
            verification_reason=ver_reason,
            google_maps_url=biz.google_maps_url,
            instagram_url=biz.instagram_url,
            facebook_url=biz.facebook_url,
            tiktok_url=biz.tiktok_url,
            review_count=biz.review_count,
            rating=biz.rating,
            social_status=soc_status,
            social_ownership_status=soc_ownership,
            social_activity=soc_activity,
            multiple_locations="Yes" if "multiple_locations" in signals_dict else "No",
            business_activity_signal=f"Reviews: {biz.review_count}, Rating: {biz.rating}",
            qualification_signals=signals_str,
            lead_score=score,
            priority=priority,
            qualification_state=q_state,
            qualification_reason=q_reason,
            red_flags=red_flags_str,
            outreach_angle=outreach_angle,
            lead_status="NOT_CONTACTED",
            date_added=today_str
        )

        research_entry = ResearchLogEntry(
            research_id=f"RES-MAN-{idx:03d}",
            company_name=biz.company_name,
            industry=biz.category,
            target_country=biz.target_country,
            detected_country=biz.detected_country or biz.target_country,
            country_status=CountryStatus.COUNTRY_MATCH.value,
            city=biz.city,
            region=biz.region,
            postcode=biz.postcode,
            address=biz.address,
            phone=safe_phone,
            website=biz.raw_website,
            website_status=ver_status,
            verification_status=ver_status,
            review_count=biz.review_count,
            rating=biz.rating,
            social_status=soc_status,
            social_ownership_status=soc_ownership,
            qualification_state=q_state,
            qualification_status=q_state,
            disqualification_reason=q_reason if q_state != QualificationState.OUTREACH_READY.value else "",
            red_flags=red_flags_str,
            source_url=biz.google_maps_url,
            date_researched=today_str
        )

        if q_state == QualificationState.OUTREACH_READY.value:
            outreach_ready_leads.append(lead_obj)
        elif q_state == QualificationState.MANUAL_REVIEW.value:
            manual_review_leads.append(lead_obj)
        else:
            research_only_entries.append(research_entry)

        print(f"[{q_state}] {biz.company_name:30} | Score: {score:2d} | Priority: {priority:12} | Flags: {red_flags_str or 'None'}")

    print("\n" + "="*60)
    print(f"MIGRATION EVALUATION SUMMARY:")
    print(f"Total leads evaluated:    {len(all_raw_rows) - 1}")
    print(f"OUTREACH_READY (LEADS):   {len(outreach_ready_leads)}")
    print(f"MANUAL_REVIEW (QUEUE):    {len(manual_review_leads)}")
    print(f"RESEARCH_ONLY (LOG):      {len(research_only_entries)}")
    print("="*60 + "\n")

    # 2. Update LEADS tab in Google Sheets (Only OUTREACH_READY leads)
    storage.ensure_leads_columns()
    leads_ws = storage.leads_worksheet
    num_rows = leads_ws.row_count
    if num_rows > 1:
        leads_ws.batch_clear([f"A2:{col_to_letter(len(LEADS_COLUMNS))}{num_rows}"])
    
    if outreach_ready_leads:
        leads_rows = [l.to_sheet_row(LEADS_COLUMNS) for l in outreach_ready_leads]
        leads_ws.append_rows(leads_rows, value_input_option="USER_ENTERED")
        print(f"[GoogleSheets] Synced {len(leads_rows)} OUTREACH_READY leads to LEADS tab.")

    # 3. Update REVIEW_QUEUE tab in Google Sheets (Only MANUAL_REVIEW leads)
    storage.ensure_review_queue_columns()
    rq_ws = storage.review_queue_worksheet
    num_rq_rows = rq_ws.row_count
    if num_rq_rows > 1:
        rq_ws.batch_clear([f"A2:{col_to_letter(len(REVIEW_QUEUE_COLUMNS))}{num_rq_rows}"])

    if manual_review_leads:
        rq_rows = [l.to_sheet_row(REVIEW_QUEUE_COLUMNS) for l in manual_review_leads]
        rq_ws.append_rows(rq_rows, value_input_option="USER_ENTERED")
        print(f"[GoogleSheets] Synced {len(rq_rows)} MANUAL_REVIEW leads to REVIEW_QUEUE tab.")

    # 4. Save/Update RESEARCH_LOG
    all_log_entries = []
    for l in outreach_ready_leads:
        all_log_entries.append(ResearchLogEntry(
            research_id=l.lead_id.replace("LEAD", "RES"),
            company_name=l.company_name,
            industry=l.industry,
            target_country=l.target_country,
            detected_country=l.country,
            country_status=CountryStatus.COUNTRY_MATCH.value,
            city=l.city,
            region=l.region,
            postcode=l.postcode,
            address=l.address,
            phone=l.phone,
            website=l.website,
            website_status=l.website_status,
            verification_status=l.verification_status,
            review_count=l.review_count,
            rating=l.rating,
            social_status=l.social_status,
            social_ownership_status=l.social_ownership_status,
            qualification_state=l.qualification_state,
            qualification_status=l.qualification_state,
            disqualification_reason="",
            red_flags=l.red_flags,
            source_url=l.google_maps_url,
            date_researched=today_str
        ))
    for l in manual_review_leads:
        all_log_entries.append(ResearchLogEntry(
            research_id=l.lead_id.replace("LEAD", "RES"),
            company_name=l.company_name,
            industry=l.industry,
            target_country=l.target_country,
            detected_country=l.country,
            country_status=CountryStatus.COUNTRY_MATCH.value,
            city=l.city,
            region=l.region,
            postcode=l.postcode,
            address=l.address,
            phone=l.phone,
            website=l.website,
            website_status=l.website_status,
            verification_status=l.verification_status,
            review_count=l.review_count,
            rating=l.rating,
            social_status=l.social_status,
            social_ownership_status=l.social_ownership_status,
            qualification_state=l.qualification_state,
            qualification_status=l.qualification_state,
            disqualification_reason=l.qualification_reason,
            red_flags=l.red_flags,
            source_url=l.google_maps_url,
            date_researched=today_str
        ))
    all_log_entries.extend(research_only_entries)

    storage.save_research_log(all_log_entries)
    print(f"[GoogleSheets] RESEARCH_LOG updated with {len(all_log_entries)} total research entries.")
    print("[MIGRATION] Migration and validation completed successfully!")

if __name__ == "__main__":
    main()
