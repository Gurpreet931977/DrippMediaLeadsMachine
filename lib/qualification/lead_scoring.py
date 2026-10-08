import re
from typing import Dict, Any, Tuple, List, Optional
from lib.types import (
    DiscoveredBusiness,
    VerificationStatus,
    Priority,
    SocialStatus,
    SocialOwnershipStatus,
    SocialActivityStatus,
    SocialProfileStatus,
    OperationalStatus,
    OperationalConfidence,
    EvidenceFreshness,
    QualificationState,
    DisqualificationReason,
    CreatorEvidenceStatus,
    CreatorEvidenceConfidence,
    ResearchFailureState,
)
from lib.validation.social_validator import SocialIdentityValidator
from lib.validation.operational_validator import OperationalValidator

SCORING_CONFIG = {
    "REVIEWS_2500_PLUS": 35,
    "REVIEWS_1000_TO_2499": 30,
    "REVIEWS_500_TO_999": 25,
    "REVIEWS_200_TO_499": 20,
    "REVIEWS_100_TO_199": 15,
    "REVIEWS_50_TO_99": 10,
    "RATING_4_5_PLUS": 10,
    "RATING_4_3_TO_4_49": 8,
    "RATING_4_0_TO_4_29": 5,
    "VERIFIED_INSTAGRAM": 5,
    "VERIFIED_FACEBOOK": 3,
    "VERIFIED_TIKTOK": 3,
    "RECENT_SOCIAL_ACTIVITY": 10,
    "ESTABLISHED_PHYSICAL_PRESENCE": 10,
    "BOOKING_ENQUIRY_OPPORTUNITY": 10,
    "MULTIPLE_LOCATIONS": 5,
    "STRONG_VISUAL_PRESENCE": 5,
    "LOCAL_VISIBILITY": 5,
    "MAX_SCORE": 100
}

class LeadScoringProvider:
    """
    Qualification Engine V3 Provider:
    Implements strict multi-gate qualification, operational verification,
    and evidence reliability scoring.
    
    Qualification States:
      1. OUTREACH_READY (counts toward requested lead goal - ACTIVE_CONFIRMED only)
      2. MANUAL_REVIEW (ACTIVE_LIKELY, uncertain operation, ambiguous social, routed to REVIEW_QUEUE)
      3. RESEARCH_ONLY (0-9 reviews, rating < 3.5, or OPERATIONAL_UNKNOWN with low reviews)
      4. EXCLUDED (website exists, wrong country, CLOSED_OR_UNVERIFIED, duplicate)
    
    Hard Gates for OUTREACH_READY:
      - Country match confirmed
      - NO_WEBSITE_CONFIRMED
      - Commercial traction gate (50+ reviews)
      - Rating gate (Rating >= 4.0)
      - Credible business identity
      - ACTIVE_CONFIRMED (at least 1 strong current source + 1 corroborating source)
      - Verified business social ownership OR strong independent digital signal
      - Minimum 2 meaningful commercial signals beyond 'no website'
      - No major red flags or unresolved identity contradictions
    """

    def __init__(self, min_reviews_outreach: int = 50, min_rating_outreach: float = 4.0):
        self.min_reviews_outreach = min_reviews_outreach
        self.min_rating_outreach = min_rating_outreach

    def evaluate_lead(
        self,
        business: DiscoveredBusiness,
        verification_status: str,
        verification_reason: str = "",
        website_evidence: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Executes complete multi-gate evaluation and returns full qualification audit dictionary.
        """
        red_flags: List[str] = []
        positive_signals: List[str] = []
        breakdown_dict: Dict[str, int] = {}
        breakdown_items: List[str] = []
        score = 0

        revs = business.review_count
        rating = business.rating
        city = business.city or "the area"
        category = business.category or "Business"

        # -------------------------------------------------------------
        # 1. HARD GATE: WEBSITE STATUS
        # -------------------------------------------------------------
        is_no_website = (verification_status == VerificationStatus.NO_WEBSITE_CONFIRMED.value)

        # -------------------------------------------------------------
        # 2. SOCIAL IDENTITY & PROFILE OWNERSHIP VALIDATION
        # -------------------------------------------------------------
        social_dict = {}
        if business.instagram_url: social_dict["instagram"] = business.instagram_url
        if business.facebook_url: social_dict["facebook"] = business.facebook_url
        if business.tiktok_url: social_dict["tiktok"] = business.tiktok_url

        social_audit = SocialIdentityValidator.verify_ownership(
            business_name=business.company_name,
            city=business.city,
            industry=business.category or "",
            social_urls=social_dict
        )

        social_status = social_audit["social_status"]
        social_ownership = social_audit["social_ownership_status"]
        social_profile_status = social_audit.get("social_profile_status", SocialProfileStatus.UNKNOWN.value)
        social_activity = social_audit["social_activity"]
        verified_social_urls = social_audit["verified_urls"]
        verified_handles = social_audit["verified_handles"]
        red_flags.extend(social_audit["red_flags"])

        # -------------------------------------------------------------
        # 2B. CREATOR / INFLUENCER EVIDENCE INTEGRATION (Sections 8, 9, 10)
        # -------------------------------------------------------------
        creator_ev_raw = business.raw_data.get("creator_evidence") if hasattr(business, "raw_data") and isinstance(business.raw_data, dict) else getattr(business, "creator_evidence", None)
        creator_ev = None
        if creator_ev_raw:
            if isinstance(creator_ev_raw, dict):
                creator_ev = creator_ev_raw
            elif hasattr(creator_ev_raw, "to_dict"):
                creator_ev = creator_ev_raw.to_dict()

        # If official social was unverified or missing, check if creator evidence discovered an independently verified official handle
        if creator_ev and social_ownership != SocialOwnershipStatus.VERIFIED.value:
            disc_handles = creator_ev.get("discovered_official_handles") or []
            for dh in disc_handles:
                if dh.get("validation_status") == "VERIFIED" and dh.get("verified_url"):
                    plat = dh.get("platform", "Instagram").lower()
                    social_dict[plat] = dh["verified_url"]
                    # Re-verify with the newly discovered candidate
                    social_audit = SocialIdentityValidator.verify_ownership(
                        business_name=business.company_name,
                        city=business.city,
                        industry=business.category or "",
                        social_urls=social_dict
                    )
                    social_status = social_audit["social_status"]
                    social_ownership = social_audit["social_ownership_status"]
                    social_profile_status = social_audit.get("social_profile_status", SocialProfileStatus.UNKNOWN.value)
                    social_activity = social_audit["social_activity"]
                    verified_social_urls = social_audit["verified_urls"]
                    verified_handles = social_audit["verified_handles"]
                    break

        # -------------------------------------------------------------
        # 3. OPERATIONAL VERIFICATION & EVIDENCE RELIABILITY (V3)
        # -------------------------------------------------------------
        op_audit = OperationalValidator.verify_operations(business, social_audit, creator_evidence=creator_ev, website_verification_status=verification_status)
        current_maps_status = op_audit["current_maps_status"]
        recent_business_activity = op_audit.get("recent_business_activity", "UNKNOWN")
        recent_review_activity = op_audit.get("recent_review_activity", "UNKNOWN")
        current_social_activity = op_audit.get("current_social_activity", "UNKNOWN")
        current_contact_evidence = op_audit.get("current_contact_evidence", "UNKNOWN")
        operational_status = op_audit["operational_status"]
        operational_confidence = op_audit["operational_confidence"]
        operational_evidence = op_audit["operational_evidence"]
        evidence_freshness = op_audit["evidence_freshness"]
        for rf in op_audit.get("red_flags", []):
            if rf not in red_flags:
                red_flags.append(rf)

        # Operational Evidence Object (Prompt Section 3)
        operational_evidence_obj = {
            "current_maps_status": current_maps_status,
            "recent_business_activity": recent_business_activity,
            "recent_review_activity": recent_review_activity,
            "current_social_activity": current_social_activity,
            "current_contact_evidence": current_contact_evidence,
            "operational_status": operational_status,
            "operational_confidence": operational_confidence,
            "operational_evidence": operational_evidence,
            "red_flags": list(red_flags)
        }

        # -------------------------------------------------------------
        # 4. COMMERCIAL SIGNALS GATHERING
        # -------------------------------------------------------------
        # Signal A: Review Traction
        if revs is not None and revs >= 50:
            positive_signals.append("review_traction_50_plus")
        elif revs is not None and revs >= 10:
            positive_signals.append("moderate_reviews_10_to_49")

        # Signal B: High Rating
        if rating is not None and rating >= 4.0:
            positive_signals.append("high_customer_rating_4_plus")
        elif rating is not None and rating < 3.5 and revs is not None and revs > 0:
            red_flags.append(f"Low customer satisfaction rating ({rating}★ from {revs} reviews)")

        # Signal C: Verified Accessible Business-Owned Social
        has_verified_social = (
            social_ownership in [SocialOwnershipStatus.VERIFIED.value, "VERIFIED_OWNED", "VERIFIED"]
            and social_profile_status == SocialProfileStatus.ACCESSIBLE.value
            and len(verified_social_urls) > 0
        )
        if has_verified_social:
            positive_signals.append("verified_business_social")
            if social_activity == SocialActivityStatus.ACTIVE.value or social_activity == "SOCIAL_ACTIVE":
                positive_signals.append("recent_social_activity")

        # Signal D: Established Physical Premises
        has_address = bool(business.address and len(business.address.strip()) > 8)
        has_phone = bool(business.phone and len(business.phone.strip()) > 6)
        if has_address and has_phone:
            positive_signals.append("established_physical_premises")
        elif not has_address and not has_phone:
            red_flags.append("No verified physical address and no phone number found")

        # Signal E: Booking / Customer Enquiry Opportunity
        cat_lower = (category or "").lower()
        booking_keywords = ["restaurant", "bar", "grill", "bistro", "cafe", "kitchen", "dining", "pub", "pizzeria", "food", "lounge", "bakery"]
        is_booking_opp = any(w in cat_lower for w in booking_keywords)
        if is_booking_opp:
            positive_signals.append("clear_booking_enquiry_opportunity")

        # Signal F: Multiple Locations
        if business.places_count > 1:
            positive_signals.append("multiple_locations")

        # Signal G: Local Visibility (Rating >= 4.0 and 100+ reviews)
        if rating is not None and revs is not None and rating >= 4.0 and revs >= 100:
            positive_signals.append("strong_local_visibility")

        # -------------------------------------------------------------
        # 4. SCORING ENGINE V2 (0-100 MAX)
        # -------------------------------------------------------------
        # Review Traction Scoring
        if revs is not None:
            if revs >= 2500:
                pts = 35
                score += pts
                breakdown_dict["reviews_2500_plus"] = pts
                breakdown_items.append(f"+{pts} Review traction (2,500+ reviews: {revs})")
            elif revs >= 1000:
                pts = 30
                score += pts
                breakdown_dict["reviews_1000_to_2499"] = pts
                breakdown_items.append(f"+{pts} Review traction (1,000-2,499 reviews: {revs})")
            elif revs >= 500:
                pts = 25
                score += pts
                breakdown_dict["reviews_500_to_999"] = pts
                breakdown_items.append(f"+{pts} Review traction (500-999 reviews: {revs})")
            elif revs >= 200:
                pts = 20
                score += pts
                breakdown_dict["reviews_200_to_499"] = pts
                breakdown_items.append(f"+{pts} Review traction (200-499 reviews: {revs})")
            elif revs >= 100:
                pts = 15
                score += pts
                breakdown_dict["reviews_100_to_199"] = pts
                breakdown_items.append(f"+{pts} Review traction (100-199 reviews: {revs})")
            elif revs >= 50:
                pts = 10
                score += pts
                breakdown_dict["reviews_50_to_99"] = pts
                breakdown_items.append(f"+{pts} Review traction (50-99 reviews: {revs})")
            elif revs >= 10:
                pts = 5
                score += pts
                breakdown_dict["reviews_10_to_49"] = pts
                breakdown_items.append(f"+{pts} Review traction (10-49 reviews: {revs})")
        else:
            breakdown_dict["reviews_unknown"] = 0
            breakdown_items.append("+0 Review data unknown / missing")

        # Rating Scoring
        if rating is not None and revs is not None and revs >= 10:
            if rating >= 4.5:
                pts = 10
                score += pts
                breakdown_dict["rating_4_5_plus"] = pts
                breakdown_items.append(f"+{pts} High customer rating (4.5+: {rating}★)")
            elif rating >= 4.3:
                pts = 8
                score += pts
                breakdown_dict["rating_4_3_to_4_49"] = pts
                breakdown_items.append(f"+{pts} Positive customer rating (4.3-4.49: {rating}★)")
            elif rating >= 4.0:
                pts = 5
                score += pts
                breakdown_dict["rating_4_0_to_4_29"] = pts
                breakdown_items.append(f"+{pts} Good customer rating (4.0-4.29: {rating}★)")
        elif rating is None:
            breakdown_dict["rating_unknown"] = 0
            breakdown_items.append("+0 Rating unknown / missing")

        # Verified Business Social Ownership (up to +10)
        social_pts = 0
        if len(verified_social_urls) >= 2:
            social_pts = 10
            breakdown_dict["verified_business_social"] = 10
            breakdown_items.append("+10 Multiple verified business-owned social channels")
        elif len(verified_social_urls) == 1:
            social_pts = 10
            breakdown_dict["verified_business_social"] = 10
            net_name = list(verified_social_urls.keys())[0].capitalize()
            breakdown_items.append(f"+10 Verified business-owned {net_name} profile")
        elif has_verified_social:
            social_pts = 10
            breakdown_dict["verified_business_social"] = 10
            breakdown_items.append("+10 Verified business-owned digital channel")
        score += social_pts

        # Recent Social Activity (+10)
        if has_verified_social and social_activity in [SocialActivityStatus.ACTIVE.value, "ACTIVE", "SOCIAL_ACTIVE"]:
            pts = 10
            score += pts
            breakdown_dict["recent_social_activity"] = pts
            breakdown_items.append("+10 Active business-owned social channel")

        # Established Physical Presence (+10)
        if has_address and has_phone:
            pts = 10
            score += pts
            breakdown_dict["established_physical_presence"] = pts
            breakdown_items.append("+10 Established physical premises and direct phone")

        # Booking / Enquiry Opportunity (+10)
        if is_booking_opp:
            pts = 10
            score += pts
            breakdown_dict["booking_enquiry_opportunity"] = pts
            breakdown_items.append("+10 Website-relevant customer booking/enquiry opportunity")

        # Multiple Locations (+5)
        if business.places_count > 1 or getattr(business, "multiple_locations", "No") == "Yes":
            pts = 5
            score += pts
            breakdown_dict["multiple_locations"] = pts
            breakdown_items.append(f"+5 Multi-location operation ({business.places_count} locations)")

        # Strong Visual / Content Presence (+5)
        # Note: verified social channel with visual medium exists
        has_visual_presence = bool(has_verified_social and ("instagram" in verified_social_urls or "tiktok" in verified_social_urls or (getattr(business, "photo_count", 0) or 0) >= 50))
        if has_visual_presence:
            pts = 5
            score += pts
            breakdown_dict["strong_visual_presence"] = pts
            breakdown_items.append("+5 Verified visual media presence on Instagram/TikTok")

        # Local Visibility (+5)
        if rating is not None and revs is not None and rating >= 4.0 and revs >= 100:
            pts = 5
            score += pts
            breakdown_dict["local_visibility"] = pts
            breakdown_items.append("+5 Strong verified local visibility (100+ reviews, 4.0+ rating)")

        # Cap score at 100
        score = min(score, 100)

        # -------------------------------------------------------------
        # 5. QUALIFICATION STATE DETERMINATION (STRICT HARD GATES)
        # -------------------------------------------------------------
        qualification_state = QualificationState.EXCLUDED.value
        qualification_reason = ""
        priority = Priority.LOW.value

        # A. Wrong Country -> EXCLUDED
        if getattr(business, "country_status", "") == "COUNTRY_MISMATCH":
            qualification_state = QualificationState.EXCLUDED.value
            qualification_reason = f"Excluded: Country mismatch (Detected {getattr(business, 'detected_country', 'foreign')}, expected {getattr(business, 'target_country', 'target')})."
            priority = Priority.LOW.value

        # B. Permanently Closed or CLOSED_OR_UNVERIFIED -> EXCLUDED
        elif business.is_permanently_closed or operational_status == OperationalStatus.CLOSED_OR_UNVERIFIED.value:
            qualification_state = QualificationState.EXCLUDED.value
            qualification_reason = f"Excluded: Business is permanently closed or operational status unverified ({operational_evidence})."
            priority = Priority.LOW.value

        # C. Website Exists -> EXCLUDED
        elif verification_status == VerificationStatus.WEBSITE_EXISTS.value:
            qualification_state = QualificationState.EXCLUDED.value
            qualification_reason = f"Excluded: Official website confirmed ({business.raw_website or 'identified in checks'})."
            priority = Priority.LOW.value

        # D. Website Broken or Unclear -> MANUAL_REVIEW
        elif verification_status == VerificationStatus.WEBSITE_BROKEN.value:
            qualification_state = QualificationState.MANUAL_REVIEW.value
            qualification_reason = f"Manual Review: Listed website is broken/unreachable ({verification_reason}). Potential recovery candidate."
            priority = Priority.MANUAL_REVIEW.value

        elif verification_status == VerificationStatus.WEBSITE_UNCLEAR.value:
            qualification_state = QualificationState.MANUAL_REVIEW.value
            qualification_reason = f"Manual Review: Website existence unclear ({verification_reason}). Requires human audit."
            priority = Priority.MANUAL_REVIEW.value

        elif not is_no_website:
            qualification_state = QualificationState.EXCLUDED.value
            qualification_reason = f"Excluded: Website status '{verification_status}'."
            priority = Priority.LOW.value

        # E. Website is confirmed NO WEBSITE -> Check Commercial & Operational Hard Gates
        else:
            # Check 0A: Conflicting Review Data Across Sources -> MANUAL_REVIEW
            review_enrich = getattr(business, "raw_data", {}).get("review_enrichment", {}) if isinstance(getattr(business, "raw_data", None), dict) else {}
            if review_enrich.get("review_confidence") == "CONFLICT" or review_enrich.get("review_status") == "CONFLICT_REQUIRES_REVIEW":
                qualification_state = QualificationState.MANUAL_REVIEW.value
                qualification_reason = "Manual Review: Conflicting review/rating data detected across sources. Requires human review."
                priority = Priority.MANUAL_REVIEW.value

            # Check 0B: Stale Review Data -> Cannot be treated as current verified traction
            elif review_enrich.get("review_freshness") == "STALE":
                qualification_state = QualificationState.MANUAL_REVIEW.value
                qualification_reason = f"Manual Review: Review data is stale ({review_enrich.get('review_evidence_date', 'historic')}). Current operational traction unverified."
                priority = Priority.MANUAL_REVIEW.value

            # Check 0C: Missing / Unknown Review Data -> RESEARCH_ONLY (Differentiated per Phase 11.1)
            elif revs is None:
                qualification_state = QualificationState.RESEARCH_ONLY.value
                priority = Priority.RESEARCH_ONLY.value

                res_telem = getattr(business, "raw_data", {}).get("research_telemetry", {}) if isinstance(getattr(business, "raw_data", None), dict) else {}
                fail_state = res_telem.get("failure_reason") or getattr(business, "research_failure_state", None)

                if fail_state == ResearchFailureState.PROVIDER_NOT_CONFIGURED.value:
                    qualification_reason = "Research Only: Review research provider not configured (Tavily/Brave/SearXNG missing). Insufficient verified customer traction for outreach."
                elif fail_state == ResearchFailureState.PROVIDER_UNAVAILABLE.value:
                    qualification_reason = "Research Only: Review research provider unavailable (connection refused or network unreachable). Insufficient verified customer traction for outreach."
                elif fail_state == ResearchFailureState.PROVIDER_TIMEOUT.value:
                    qualification_reason = "Research Only: Review research provider timed out. Insufficient verified customer traction for outreach."
                elif fail_state == ResearchFailureState.PROVIDER_FAILED.value:
                    qualification_reason = "Research Only: Review research provider failed (HTTP error or circuit open). Insufficient verified customer traction for outreach."
                elif fail_state == ResearchFailureState.QUOTA_EXCEEDED.value:
                    qualification_reason = "Research Only: Review research provider quota exceeded. Insufficient verified customer traction for outreach."
                elif fail_state == ResearchFailureState.EXTRACTION_FAILED.value:
                    qualification_reason = "Research Only: Review evidence extraction failed from discovered sources. Insufficient verified customer traction for outreach."
                elif fail_state == ResearchFailureState.IDENTITY_MISMATCH.value:
                    qualification_reason = "Research Only: Discovered review sources rejected due to identity/branch mismatch. Insufficient verified customer traction for outreach."
                elif fail_state == ResearchFailureState.EVIDENCE_CONFLICT.value:
                    qualification_reason = "Research Only: Conflicting review evidence detected across sources. Insufficient verified customer traction for outreach."
                elif fail_state == ResearchFailureState.NO_EVIDENCE_FOUND.value:
                    qualification_reason = "Research Only: Genuinely no review evidence found across verified sources. Insufficient verified customer traction for outreach."
                else:
                    qualification_reason = "Research Only: Review data unknown / missing. Insufficient verified customer traction for outreach."


            # Check 1: Very low review count (0-9 reviews) -> RESEARCH_ONLY
            elif revs < 10:
                qualification_state = QualificationState.RESEARCH_ONLY.value
                qualification_reason = f"Research Only: Minimal customer review traction ({revs} reviews). Insufficient commercial activity for outreach."
                priority = Priority.RESEARCH_ONLY.value

            # Check 2: Low customer satisfaction rating (< 3.5)
            elif rating is not None and rating < 3.5:
                if revs >= 50:
                    qualification_state = QualificationState.MANUAL_REVIEW.value
                    qualification_reason = f"Manual Review: Low rating ({rating}★ from {revs} reviews). Requires brand audit before outreach."
                    priority = Priority.MANUAL_REVIEW.value
                else:
                    qualification_state = QualificationState.RESEARCH_ONLY.value
                    qualification_reason = f"Research Only: Low rating ({rating}★) and low traction ({revs} reviews)."
                    priority = Priority.RESEARCH_ONLY.value

            # Check 3: Mid-tier reviews (10-49 reviews) -> MANUAL_REVIEW
            elif revs < self.min_reviews_outreach:
                qualification_state = QualificationState.MANUAL_REVIEW.value
                qualification_reason = f"Manual Review: Moderate customer traction ({revs} reviews, minimum {self.min_reviews_outreach} required for automated outreach)."
                priority = Priority.MANUAL_REVIEW.value

            # Check 4: Borderline rating or missing rating for 50+ reviews
            elif rating is None or rating < self.min_rating_outreach:
                qualification_state = QualificationState.MANUAL_REVIEW.value
                if rating is None:
                    qualification_reason = f"Manual Review: Rating unknown / missing for business with {revs} reviews. Minimum {self.min_rating_outreach}★ required."
                else:
                    qualification_reason = f"Manual Review: Rating {rating}★ is below {self.min_rating_outreach}★ outreach threshold."
                priority = Priority.MANUAL_REVIEW.value

            # Check 5: Must have at least TWO meaningful commercial signals beyond 'no website'
            elif len(positive_signals) < 2:
                qualification_state = QualificationState.MANUAL_REVIEW.value
                qualification_reason = f"Manual Review: Found only {len(positive_signals)} commercial signals (minimum 2 required)."
                priority = Priority.MANUAL_REVIEW.value

            # Check 6: Must have verified business-owned social OR confirmed active operations (ACTIVE_CONFIRMED) OR another strong independent digital presence
            elif not (
                has_verified_social
                or (operational_status == OperationalStatus.ACTIVE_CONFIRMED.value)
                or (revs is not None and revs >= 500 and has_address and has_phone and ((getattr(business, 'photo_count', 0) or 0) >= 50))
            ):
                qualification_state = QualificationState.MANUAL_REVIEW.value
                if red_flags:
                    qualification_reason = f"Manual Review: Social identity unverified ({red_flags[0]}). Human verification needed."
                else:
                    qualification_reason = "Manual Review: No verified business-owned social presence and insufficient alternative digital footprint."
                priority = Priority.MANUAL_REVIEW.value

            # Check 7: OPERATIONAL STATUS HARD GATE (V3)
            # Only ACTIVE_CONFIRMED may become OUTREACH_READY
            elif operational_status == OperationalStatus.ACTIVE_LIKELY.value:
                qualification_state = QualificationState.MANUAL_REVIEW.value
                priority = Priority.MANUAL_REVIEW.value
                qualification_reason = f"Manual Review: Business appears active but operational evidence lacks dual independent corroboration ({operational_evidence})."

            elif operational_status == OperationalStatus.OPERATIONAL_UNKNOWN.value:
                if revs is not None and revs >= 50:
                    qualification_state = QualificationState.MANUAL_REVIEW.value
                    priority = Priority.MANUAL_REVIEW.value
                    qualification_reason = f"Manual Review: Current operational status unknown ({operational_evidence}). Human verification required."
                else:
                    qualification_state = QualificationState.RESEARCH_ONLY.value
                    priority = Priority.RESEARCH_ONLY.value
                    qualification_reason = f"Research Only: Current operational status unknown ({operational_evidence})."

            elif operational_status != OperationalStatus.ACTIVE_CONFIRMED.value:
                qualification_state = QualificationState.MANUAL_REVIEW.value
                priority = Priority.MANUAL_REVIEW.value
                qualification_reason = f"Manual Review: Operational status '{operational_status}' requires manual verification."

            # Check 8: Unresolved identity contradiction or major red flags
            elif any("mismatch" in rf.lower() or "unrelated" in rf.lower() or "contradict" in rf.lower() for rf in red_flags):
                qualification_state = QualificationState.MANUAL_REVIEW.value
                priority = Priority.MANUAL_REVIEW.value
                matching_rf = next(rf for rf in red_flags if any(k in rf.lower() for k in ["mismatch", "unrelated", "contradict"]))
                qualification_reason = f"Manual Review: Unresolved identity contradiction ({matching_rf})."

            # ALL HARD GATES PASSED -> Lead is OUTREACH_READY; Score determines Priority
            else:
                qualification_state = QualificationState.OUTREACH_READY.value
                if score >= 80:
                    priority = Priority.HIGH.value
                elif score >= 65:
                    priority = Priority.MEDIUM.value
                else:
                    priority = Priority.LOW.value

                channels = []
                if "instagram" in verified_social_urls: channels.append(f"Instagram (@{verified_handles.get('instagram')})")
                if "facebook" in verified_social_urls: channels.append(f"Facebook (@{verified_handles.get('facebook')})")
                if "tiktok" in verified_social_urls: channels.append(f"TikTok (@{verified_handles.get('tiktok')})")
                channel_str = ", ".join(channels) if channels else "active customer base"
                qualification_reason = (
                    f"Established {city} {category} with {revs} reviews ({rating}★), "
                    f"{channel_str}, verified active operations ({operational_status}), and confirmed absence of an official website."
                )

        is_outreach_ready = (qualification_state == QualificationState.OUTREACH_READY.value)

        # Build evidence object
        evidence = {
            "website_evidence": verification_reason or (website_evidence.get("summary", "") if website_evidence else ""),
            "business_identity_evidence": f"{business.company_name} ({category}) in {city} {business.postcode}".strip(),
            "review_evidence": f"{revs} reviews",
            "rating_evidence": f"{rating}★",
            "social_evidence": f"{social_status} ({social_ownership})",
            "social_ownership_status": social_ownership,
            "social_profile_status": social_profile_status,
            "social_activity_evidence": social_activity,
            "operational_status": operational_status,
            "operational_confidence": operational_confidence,
            "operational_evidence": operational_evidence,
            "evidence_freshness": evidence_freshness,
            "current_maps_status": current_maps_status,
            "operational_evidence_obj": operational_evidence_obj,
            "premises_evidence": "Verified physical address & phone" if (has_address and has_phone) else ("Address only" if has_address else "None"),
            "booking_opportunity_evidence": "Direct hospitality customer booking/enquiry opportunity" if is_booking_opp else "Standard service",
            "qualification_signals": positive_signals,
            "red_flags": red_flags,
            "creator_evidence": creator_ev,
            "creator_evidence_status": creator_ev.get("creator_evidence_status", CreatorEvidenceStatus.NOT_FOUND.value) if creator_ev else CreatorEvidenceStatus.NOT_FOUND.value,
            "creator_evidence_count": creator_ev.get("creator_evidence_count", 0) if creator_ev else 0,
            "creator_evidence_confidence": creator_ev.get("creator_evidence_confidence", CreatorEvidenceConfidence.UNKNOWN.value) if creator_ev else CreatorEvidenceConfidence.UNKNOWN.value,
            "creator_latest_date": creator_ev.get("creator_latest_date", "") if creator_ev else "",
            "creator_evidence_summary": creator_ev.get("creator_evidence_summary", "") if creator_ev else ""
        }

        breakdown_text = f"Lead Score: {score}/100 ({priority})\nBreakdown:\n" + "\n".join(breakdown_items)
        if red_flags:
            breakdown_text += "\nRed Flags:\n" + "\n".join([f"- {rf}" for rf in red_flags])

        return {
            "score": score,
            "priority": priority,
            "qualification_state": qualification_state,
            "qualification_reason": qualification_reason,
            "score_breakdown": breakdown_dict,
            "signals": {s: True for s in positive_signals},
            "is_outreach_ready": is_outreach_ready,
            "evidence": evidence,
            "red_flags": red_flags,
            "breakdown_text": breakdown_text,
            "social_status": social_status,
            "social_ownership_status": social_ownership,
            "social_profile_status": social_profile_status,
            "social_activity": social_activity,
            "verified_social_urls": verified_social_urls,
            "operational_status": operational_status,
            "operational_confidence": operational_confidence,
            "operational_evidence": operational_evidence,
            "evidence_freshness": evidence_freshness,
            "current_maps_status": current_maps_status,
            "operational_evidence_obj": operational_evidence_obj,
            "creator_evidence": creator_ev,
            "creator_evidence_status": creator_ev.get("creator_evidence_status", CreatorEvidenceStatus.NOT_FOUND.value) if creator_ev else CreatorEvidenceStatus.NOT_FOUND.value,
            "creator_evidence_count": creator_ev.get("creator_evidence_count", 0) if creator_ev else 0,
            "creator_evidence_confidence": creator_ev.get("creator_evidence_confidence", CreatorEvidenceConfidence.UNKNOWN.value) if creator_ev else CreatorEvidenceConfidence.UNKNOWN.value,
            "creator_latest_date": creator_ev.get("creator_latest_date", "") if creator_ev else "",
            "creator_evidence_summary": creator_ev.get("creator_evidence_summary", "") if creator_ev else "",
            "creator_evidence_urls": creator_ev.get("creator_evidence_urls", []) if creator_ev else [],
            "creator_discovered_at": creator_ev.get("creator_discovered_at", "") if creator_ev else ""
        }

    # Backward compatibility method for existing pipeline calls
    def score_and_qualify(
        self,
        business: DiscoveredBusiness,
        verification_status: str
    ) -> Tuple[int, str, str, Dict[str, int], Dict[str, Any], bool, str]:
        audit = self.evaluate_lead(business, verification_status)
        return (
            audit["score"],
            audit["priority"],
            audit["qualification_reason"],
            audit["score_breakdown"],
            audit["signals"],
            audit["is_outreach_ready"],
            audit["breakdown_text"]
        )
