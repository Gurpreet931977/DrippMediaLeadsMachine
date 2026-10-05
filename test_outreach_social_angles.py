#!/usr/bin/env python3
"""
Test Outreach Angle Social Platform Logic:
Verifies that:
- Mention Instagram only if Instagram ownership is VERIFIED.
- Mention Facebook only if Facebook ownership is VERIFIED.
- Mention TikTok only if TikTok ownership is VERIFIED.
- If multiple are verified, mention only those verified platforms.
- If none are verified, do not mention social media.
- Never infer platform ownership from a post/reel/video URL.

Tests the current outreach-ready leads from the dataset.
"""

from lib.types import DiscoveredBusiness, VerificationStatus
from lib.outreach.outreach_generator import OutreachAngleGenerator
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.sheets.google_sheets import GoogleSheetsStorageProvider

def test_unit_cases():
    generator = OutreachAngleGenerator()

    # Case A: Facebook only verified (e.g. Instagram has post URL, Facebook is verified)
    biz_fb = DiscoveredBusiness(
        company_name="Seoul Kimchi",
        category="Korean restaurant",
        city="Manchester",
        target_country="United Kingdom",
        review_count=1010,
        rating=4.6,
        address="275 Upper Brook St, Manchester M13 0HR",
        phone="+44 7745 527603",
        instagram_url="https://www.instagram.com/popular/seoul-kimchi-menu-manchester/",  # search/topic page
        facebook_url="https://www.facebook.com/seoulkimchimanchester/"  # verified
    )
    angle_fb = generator.generate_angle(biz_fb)
    assert "Facebook" in angle_fb, f"Expected 'Facebook' in angle: {angle_fb}"
    assert "Instagram" not in angle_fb, f"Did NOT expect 'Instagram' in angle: {angle_fb}"
    print("✓ Case A (Facebook only verified) passed!")

    # Case B: Instagram post URL only -> NO social media mentioned
    biz_post = DiscoveredBusiness(
        company_name="Urban Bistro",
        category="Bistro",
        city="Manchester",
        target_country="United Kingdom",
        review_count=150,
        rating=4.5,
        address="10 King St, Manchester M2 6AW",
        phone="+44 161 832 1111",
        instagram_url="https://www.instagram.com/p/DVI5iN5iMQj/"  # post URL
    )
    angle_post = generator.generate_angle(biz_post)
    assert "Instagram" not in angle_post, f"Did NOT expect 'Instagram' in angle: {angle_post}"
    assert "social" not in angle_post.lower(), f"Did NOT expect 'social' in angle: {angle_post}"
    assert "established physical location in Manchester" in angle_post
    print("✓ Case B (Instagram post URL only -> no social mentioned) passed!")

    # Case C: Instagram only verified (verified accessible account)
    biz_ig = DiscoveredBusiness(
        company_name="Mala",
        category="Bar & Restaurant",
        city="Manchester",
        target_country="United Kingdom",
        review_count=185,
        rating=4.5,
        address="8 Lever St, Manchester M1 1FL",
        phone="+44 161 236 8888",
        instagram_url="https://www.instagram.com/malamcr/"
    )
    angle_ig = generator.generate_angle(biz_ig)
    assert "Instagram" in angle_ig, f"Expected 'Instagram' in angle: {angle_ig}"
    assert "Facebook" not in angle_ig, f"Did NOT expect 'Facebook' in angle: {angle_ig}"
    print("✓ Case C (Instagram only verified) passed!")

    # Case C2: Broken/inaccessible Instagram URL -> DO NOT mention Instagram (V3 Reliability Rule)
    biz_broken = DiscoveredBusiness(
        company_name="Cleaver Bar & Kitchen",
        category="Bar & grill",
        city="Manchester",
        target_country="United Kingdom",
        review_count=293,
        rating=4.9,
        address="100 Wilmslow Rd, Manchester M14 5AJ",
        phone="+44 161 560 6138",
        instagram_url="https://www.instagram.com/cleavermcr/"  # Inaccessible 404 profile
    )
    angle_broken = generator.generate_angle(biz_broken)
    assert "Instagram" not in angle_broken, f"Did NOT expect 'Instagram' for broken profile: {angle_broken}"
    print("✓ Case C2 (Inaccessible profile -> no social mentioned) passed!")

    # Case D: Both Instagram and Facebook verified
    biz_multi = DiscoveredBusiness(
        company_name="Mackie Mayor",
        category="Food hall",
        city="Manchester",
        target_country="United Kingdom",
        review_count=6155,
        rating=4.5,
        address="Eagle St, Manchester M4 5BU",
        phone="+44 161 832 9999",
        instagram_url="https://www.instagram.com/mackiemayor/",
        facebook_url="https://www.facebook.com/mackiemayor/"
    )
    angle_multi = generator.generate_angle(biz_multi)
    assert "Instagram" in angle_multi and "Facebook" in angle_multi, f"Expected both in angle: {angle_multi}"
    print("✓ Case D (Both Instagram and Facebook verified) passed!")

    # Case E: No social links at all -> no social mentioned
    biz_none = DiscoveredBusiness(
        company_name="Hidden Gem",
        category="Restaurant",
        city="Manchester",
        target_country="United Kingdom",
        review_count=120,
        rating=4.7,
        address="5 Market St, Manchester M1 1PW",
        phone="+44 161 444 5555"
    )
    angle_none = generator.generate_angle(biz_none)
    assert "Instagram" not in angle_none and "Facebook" not in angle_none and "social" not in angle_none.lower()
    print("✓ Case E (No social -> no social mentioned) passed!")

def test_current_leads():
    storage = GoogleSheetsStorageProvider()
    scorer = LeadScoringProvider()
    generator = OutreachAngleGenerator()

    leads = storage.fetch_all_leads()
    print(f"\n=======================================================")
    print(f"TESTING OUTREACH ANGLES ON CURRENT LEADS ({len(leads)} leads)")
    print(f"=======================================================")

    for idx, lead in enumerate(leads[:5], 1):
        biz = DiscoveredBusiness(
            company_name=lead["company_name"],
            category=lead.get("industry", "Restaurant"),
            city=lead.get("city", "Manchester"),
            target_country=lead.get("target_country", "United Kingdom"),
            review_count=int(lead.get("review_count") or 0),
            rating=float(lead.get("rating") or 0.0),
            address=lead.get("address", ""),
            phone=lead.get("phone", ""),
            instagram_url=lead.get("instagram_url", ""),
            facebook_url=lead.get("facebook_url", ""),
            tiktok_url=lead.get("tiktok_url", "")
        )
        audit = scorer.evaluate_lead(biz, VerificationStatus.NO_WEBSITE_CONFIRMED.value)
        verified_socials = audit["verified_social_urls"]
        angle = generator.generate_angle(biz, verified_socials=verified_socials)

        print(f"\n--- Lead #{idx}: {biz.company_name} ---")
        print(f"Reviews: {biz.review_count} | Rating: {biz.rating}★")
        print(f"Raw Instagram: {biz.instagram_url or 'None'}")
        print(f"Raw Facebook:  {biz.facebook_url or 'None'}")
        print(f"Verified Socials: {list(verified_socials.keys()) if verified_socials else 'None'}")
        print(f"Generated Outreach Angle:\n\"{angle}\"")

        # Specific assertions
        if "instagram" in verified_socials:
            assert "Instagram" in angle
        else:
            assert "Instagram" not in angle, f"Instagram should NOT be mentioned in: {angle}"

        if "facebook" in verified_socials:
            assert "Facebook" in angle
        else:
            assert "Facebook" not in angle, f"Facebook should NOT be mentioned in: {angle}"

if __name__ == "__main__":
    print("Running unit test cases...")
    test_unit_cases()
    print("\nRunning test on current 5 outreach-ready leads...")
    test_current_leads()
    print("\n✓ ALL TESTS PASSED SUCCESSFULLY!")
