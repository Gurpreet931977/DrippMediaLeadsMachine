"""
test_browser_dashboard_verification.py
======================================
Automated Playwright browser verification for the Dripp Media dashboard.
Tests real rendered UI:
1. Latest-run banner display (run ID, timestamp, run mode, funnel counts).
2. Dog and Partridge visibility, inspection, evidence, and OUTREACH_READY qualification outcome.
3. Gateway configuration save flow reaching a definite success/error state.
4. Valid lead selection loading details vs. invalid lead ID showing safe empty/error state.
5. Strict separation of latest-run candidates from persistent Google Sheets CRM leads.
"""

import os
import time
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
OPERATOR_KEY = os.getenv("OPERATOR_KEY", "")


def run_browser_verification():
    print("==================================================================")
    print("STARTING PLAYWRIGHT LIVE BROWSER VERIFICATION")
    print("Target: http://127.0.0.1:8000/")
    print("==================================================================")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # Catch console errors or warnings
        console_logs = []
        page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))

        # Navigate to Dashboard
        page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        time.sleep(1)

        # 0. Authenticate via Operator Key in UI or Session
        print("\n--- [STEP 0: Authenticate in Browser] ---")
        login_result = page.evaluate("""async (opKey) => {
            const res = await apiFetch('/api/operator/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ operator_key: opKey })
            });
            if (res.ok) {
                const data = await res.json();
                DrippConfig.setSessionToken(data.session_token, data.expires_at);
                updateOperatorSessionUI(true, data.expires_at);
                await loadAllData();
                navigateTo('home');
                return { success: true, token: data.session_token };
            }
            return { success: false, status: res.status };
        }""", OPERATOR_KEY)
        
        assert login_result.get("success") is True, f"Browser login failed: {login_result}"
        print("✓ Operator authenticated in browser session.")
        time.sleep(1)

        # 1. Verify Latest-Run Banner
        print("\n--- [STEP 1: Verify Latest-Run Banner] ---")
        page.wait_for_selector("#homeLatestRunBanner", state="visible", timeout=5000)
        banner_visible = page.is_visible("#homeLatestRunBanner")
        banner_text = page.inner_text("#homeLatestRunText")
        print(f"Banner Visible: {banner_visible}")
        print(f"Banner Rendered Text: '{banner_text}'")

        assert banner_visible, "Latest run banner should be visible"
        assert "RUN-20261025_071312-C9AB1F" in banner_text, "Banner should display actual latest run ID"
        assert "PIPE-MAN-20261025-C112" in banner_text, "Banner should display pipeline run ID"
        assert "2026-10-25" in banner_text or "UTC" in banner_text, "Banner should display timestamp"
        assert "SHADOW_MODE" in banner_text, "Banner should display run mode"
        assert "40 Discovered" in banner_text, "Banner should display 40 Discovered"
        assert "2 Researched" in banner_text, "Banner should display 2 Researched"
        assert "1 Qualified" in banner_text, "Banner should display 1 Qualified"
        assert "0 Written" in banner_text, "Banner should indicate 0 written to CRM"
        print("✓ Verified Item 1: Latest-run banner displays actual run ID, timestamp, run mode, and accurate funnel counts.")

        # 2. Verify Dog and Partridge Candidate Visibility & Full Dossier Inspection
        print("\n--- [STEP 2: Verify Dog and Partridge Candidate Data & Inspection] ---")
        cands_count = page.evaluate("latestRunCandidates.length")
        print(f"Browser latestRunCandidates count: {cands_count}")
        assert cands_count >= 2, f"Expected at least 2 candidates in latestRunCandidates, got {cands_count}"

        # Navigate to Leads Workspace first
        page.evaluate("navigateTo('leads')")
        time.sleep(0.5)

        # Click Latest Run filter pill
        page.click("button[data-filter='LATEST_RUN']")
        time.sleep(0.5)

        # Check that Dog and Partridge is rendered in the stream list
        dog_stream_item = page.locator(".lead-stream-item:has-text('Dog and Partridge')")
        assert dog_stream_item.count() > 0, "Dog and Partridge must be rendered in the stream list"
        print("✓ Dog and Partridge found in stream items under Latest Run filter.")

        # Inspect Dog and Partridge
        page.evaluate("selectAndOpenDrawer('CAND-DOG-PARTRIDGE-M20')")
        time.sleep(0.5)

        # Verify Drawer is active and contains full evidence
        drawer_active = page.is_visible("#leadDrawer.active")
        print(f"Lead Drawer Open: {drawer_active}")
        assert drawer_active, "Lead drawer must be active"

        drawer_biz_name = page.inner_text("#drawerBusinessName")
        drawer_lead_id = page.inner_text("#drawerLeadId")
        dossier_why = page.inner_text("#dossierWhyQualified")
        dossier_evidence = page.inner_text("#dossierEvidence")
        dossier_outreach = page.inner_text("#dossierOutreachPossible")
        rule_b_rating = page.inner_text("#ruleBRatingBadge")
        rule_b_reviews = page.inner_text("#ruleBReviewCountBadge")
        rule_b_website = page.inner_text("#ruleBWebsiteBadge")

        print(f"Drawer Business Name: {drawer_biz_name}")
        print(f"Drawer Lead ID: {drawer_lead_id}")
        print(f"Drawer Rule B Rating Badge: {rule_b_rating}")
        print(f"Drawer Rule B Reviews Badge: {rule_b_reviews}")
        print(f"Drawer Rule B Website Badge: {rule_b_website}")
        print(f"Dossier Why Qualified: {dossier_why}")
        print(f"Dossier Evidence: {dossier_evidence}")
        print(f"Dossier Outreach: {dossier_outreach}")

        assert drawer_biz_name == "Dog and Partridge"
        assert "4.3" in dossier_why, "Dossier should show 4.3 rating"
        assert "88" in dossier_why, "Dossier should show 88 reviews"
        assert rule_b_rating == "PASS", "Rating check should PASS (4.3 >= 4.0)"
        assert rule_b_reviews == "PASS", "Review count check should PASS (88 >= 50)"
        assert rule_b_website == "PASS", "Website check should PASS (Confirmed No Website)"
        assert "Shadow Mode" in dossier_outreach or "0 saved to CRM" in dossier_outreach, "Drawer must explicitly indicate dry-run / shadow mode"

        # Verify Inspector Pane (Right side)
        insp_name = page.inner_text("#inspName")
        insp_reviews = page.inner_text("#inspReviews")
        insp_why = page.inner_text("#inspWhy")
        print(f"Inspector Pane Name: {insp_name}")
        print(f"Inspector Pane Reviews: {insp_reviews}")
        print(f"Inspector Pane Why: {insp_why}")
        assert insp_name == "Dog and Partridge"
        assert "4.3" in insp_reviews and "88" in insp_reviews
        assert "OUTREACH_READY" in insp_why or "Established Manchester pub" in insp_why or "88 reviews" in insp_why

        # Close Drawer
        page.evaluate("closeLeadDrawer()")
        time.sleep(0.3)
        print("✓ Verified Item 2: Dog and Partridge is visible as an individually inspectable candidate with evidence and OUTREACH_READY outcome.")

        # 3. Verify Gateway Configuration Save Flow
        print("\n--- [STEP 3: Verify Gateway Configuration Save Flow] ---")
        page.evaluate("navigateTo('system')")
        time.sleep(0.5)

        page.evaluate("saveBackendConfiguration()")
        time.sleep(1)

        probe_text = page.inner_text("#gatewayProbeResult")
        print(f"Gateway Probe Result Text: '{probe_text}'")
        assert "CRM synchronization complete" in probe_text or "✓" in probe_text, f"Unexpected probe result: {probe_text}"
        assert "Re-synchronizing CRM..." not in probe_text, "Gateway save flow must NOT be indefinitely stuck in synchronizing state"
        print("✓ Verified Item 3: Gateway configuration save flow reaches a definite success state.")

        # 4. Verify Valid Lead vs Invalid Lead ID Safe State
        print("\n--- [STEP 4: Select Valid Lead vs Invalid Lead ID] ---")
        page.evaluate("navigateTo('leads')")
        page.click("button[data-filter='ALL']")
        time.sleep(0.5)

        # 4a. Valid lead: Mary D's Beamish Bar (LEAD-MAN-682E5D)
        page.evaluate("selectLead('LEAD-MAN-682E5D')")
        time.sleep(0.3)
        insp_valid_name = page.inner_text("#inspName")
        insp_valid_id = page.inner_text("#inspId")
        print(f"Selected Valid Lead: {insp_valid_name} ({insp_valid_id})")
        assert "Mary D" in insp_valid_name, f"Expected Mary D's Beamish Bar, got {insp_valid_name}"
        assert insp_valid_id == "LEAD-MAN-682E5D"

        # 4b. Invalid lead ID: 'INVALID-FAKE-LEAD-999'
        page.evaluate("selectLead('INVALID-FAKE-LEAD-999')")
        time.sleep(0.3)
        empty_state_visible = page.is_visible("#inspectorEmptyState")
        detail_visible = page.is_visible("#inspectorDetailContent")
        print(f"After selecting invalid ID: Empty State Visible={empty_state_visible}, Detail Visible={detail_visible}")
        assert empty_state_visible is True, "Inspector empty state should be visible when invalid ID is selected"
        assert detail_visible is False, "Inspector detail content must be hidden when invalid ID is selected"

        page.evaluate("openLeadDrawer('INVALID-FAKE-LEAD-999')")
        time.sleep(0.3)
        drawer_open_invalid = page.is_visible("#leadDrawer.active")
        print(f"After openLeadDrawer with invalid ID: Drawer Open={drawer_open_invalid}")
        assert drawer_open_invalid is False, "Lead drawer must NOT open for an invalid lead ID"
        print("✓ Verified Item 4: Valid lead loads correctly, and invalid lead ID safely renders empty/error state without fake leads.")

        # 5. Verify Persistent CRM vs Latest Run Separation
        print("\n--- [STEP 5: Verify Persistent CRM vs Latest Run Separation] ---")
        all_leads_count = page.evaluate("allLeads.length")
        review_count = page.evaluate("reviewQueueLeads.length")
        run_cands_count = page.evaluate("latestRunCandidates.length")
        print(f"allLeads in Browser: {all_leads_count}")
        print(f"reviewQueueLeads in Browser: {review_count}")
        print(f"latestRunCandidates in Browser: {run_cands_count}")

        assert all_leads_count == 7, f"allLeads should contain exactly the 7 persistent Google Sheets CRM leads, got {all_leads_count}"
        assert review_count == 64, f"reviewQueueLeads should contain exactly the 64 REVIEW_QUEUE leads, got {review_count}"
        assert run_cands_count >= 2, f"latestRunCandidates should contain evaluated dry run candidates, got {run_cands_count}"
        
        is_dog_in_all_leads = page.evaluate("""
            allLeads.some(l => (l.company_name || l.name || '').toLowerCase().includes('dog and partridge'))
        """)
        assert is_dog_in_all_leads is False, "Dog and Partridge must NOT be in persistent CRM leads array!"
        print("✓ Verified Item 5: Persistent CRM (7 leads) and latest-run candidates remain strictly separated.")

        context.close()
        browser.close()

    print("\n==================================================================")
    print("ALL 5 BROWSER VERIFICATION TESTS PASSED SUCCESSFULLY!")
    print("==================================================================")


if __name__ == "__main__":
    run_browser_verification()
