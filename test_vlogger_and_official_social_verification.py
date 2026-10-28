"""
test_vlogger_and_official_social_verification.py
=================================================
Automated verification for user requirements:
1. Social Verification row in Evidence Matrix displays strictly genuine official brand pages (e.g. @mala_mcr, @MalaSecretGarden).
2. Social Verification row NEVER mixes in unofficial vlogger chips.
3. Designated space for "Vlogger Reference (Unofficial)" is always kept visible (inspVloggerRow has display: flex).
4. When search has not discovered an unofficial vlogger reference, "None Discovered" is displayed in the kept space.
5. In Lead Dossier Drawer, business contact channels contain ONLY official channels (Phone, Official Socials, Email), NEVER unofficial vloggers.
6. In Lead Dossier Drawer "Why Did We Find It?", a clean space/status indicates "Vlogger Reference (Unofficial): None Discovered in Search".
7. Captures visual screenshots to artifacts directory.
"""

import os
import time
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
OPERATOR_KEY = os.getenv("OPERATOR_KEY", "")
ARTIFACTS_DIR = "/Users/metagurpreet/.gemini/antigravity-ide/brain/6013b427-9aa5-48ad-b63c-c15925dc9794"


def run_test():
    print("==================================================================")
    print("VERIFYING OFFICIAL BRAND PAGES & VLOGGER REFERENCE SPACE")
    print("Target: http://127.0.0.1:8000/")
    print("==================================================================")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        time.sleep(1)

        # Authenticate
        login_res = page.evaluate("""async (opKey) => {
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
                return { success: true };
            }
            return { success: false };
        }""", OPERATOR_KEY)
        assert login_res["success"], "Operator login failed"
        print("✓ Authenticated successfully.")

        # Navigate to Leads Workspace
        page.evaluate("navigateTo('leads')")
        time.sleep(1)

        # 1. Test Mala
        print("\n--- Testing Lead: Mala ---")
        page.evaluate("selectLead('LEAD-MAN-14A2D3')")
        time.sleep(0.5)

        # Check Social Verification cell: only official brand pages
        social_html = page.inner_html("#inspSocial")
        social_text = page.inner_text("#inspSocial")
        print(f"Mala inspSocial text: '{social_text}'")
        assert "Instagram Verified" in social_text, "Official Instagram must be present"
        assert "mala_mcr" in social_html, "@mala_mcr handle must be present"
        assert "Facebook Verified" in social_text, "Official Facebook must be present"
        assert "MalaSecretGarden" in social_html, "MalaSecretGarden handle must be present"
        assert "Vlogger" not in social_html, "Vlogger chip MUST NOT be in official Social Verification cell"
        print("✓ Mala Social Verification has only genuine official brand profiles.")

        # Check Vlogger Reference (Unofficial) row: space kept visible
        vlogger_row_visible = page.is_visible("#inspVloggerRow")
        vlogger_text = page.inner_text("#inspVlogger")
        print(f"Mala inspVloggerRow visible: {vlogger_row_visible}, text: '{vlogger_text}'")
        assert vlogger_row_visible, "Vlogger row space must be kept visible in Evidence Matrix"
        assert "None Discovered" in vlogger_text, "When no genuine vlogger is found, None Discovered must be shown"
        print("✓ Mala has space kept for Vlogger Reference with 'None Discovered'.")

        # Capture Mala Evidence Matrix Screenshot
        page.locator("#inspectorDetailContent").screenshot(path=os.path.join(ARTIFACTS_DIR, "mala_evidence_matrix_clean.png"))
        print("✓ Screenshot saved: mala_evidence_matrix_clean.png")

        # 2. Test Live Seafood Ltd
        print("\n--- Testing Lead: Live Seafood Ltd ---")
        page.evaluate("selectLead('LEAD-MAN-0363CF')")
        time.sleep(0.5)

        social_text_ls = page.inner_text("#inspSocial")
        social_html_ls = page.inner_html("#inspSocial")
        print(f"Live Seafood inspSocial: '{social_text_ls}'")
        assert "live_seafood_ltd" in social_html_ls, "Live Seafood official Instagram must be present"
        assert "Vlogger" not in social_html_ls, "Vlogger chip must not be in official Social Verification cell"

        vlogger_row_visible_ls = page.is_visible("#inspVloggerRow")
        vlogger_text_ls = page.inner_text("#inspVlogger")
        assert vlogger_row_visible_ls, "Vlogger row space must be kept visible"
        assert "None Discovered" in vlogger_text_ls, "None Discovered must be shown"
        print("✓ Live Seafood Ltd verified.")

        # 3. Test Lead Dossier Drawer for Mala
        print("\n--- Testing Lead Dossier Drawer ---")
        page.evaluate("openLeadDrawer('LEAD-MAN-14A2D3')")
        time.sleep(0.5)

        drawer_contact_html = page.inner_html("#dossierContact")
        drawer_contact_text = page.inner_text("#dossierContact")
        print(f"Drawer Contact text: '{drawer_contact_text}'")
        assert "Telephone:" in drawer_contact_text, "Telephone must be in contact channels"
        assert "Instagram" in drawer_contact_text, "Official Instagram must be in contact channels"
        assert "Facebook" in drawer_contact_text, "Official Facebook must be in contact channels"
        assert "Vlogger" not in drawer_contact_html, "Vlogger must NOT be in official contact channels"
        print("✓ Drawer Contact channels contain ONLY official channels (no vloggers).")

        drawer_why_html = page.inner_html("#dossierWhyFound")
        print(f"Drawer Why Found contains 'None Discovered in Search': {'None Discovered in Search' in drawer_why_html}")
        assert "Vlogger Reference (Unofficial): None Discovered in Search" in drawer_why_html, "Drawer Why Found must keep space for vlogger reference status"
        print("✓ Drawer Why Found keeps designated space for vlogger reference.")

        # Capture Drawer Screenshot
        page.locator("#leadDrawer").screenshot(path=os.path.join(ARTIFACTS_DIR, "mala_dossier_drawer_clean.png"))
        print("✓ Screenshot saved: mala_dossier_drawer_clean.png")

        browser.close()
        print("\n==================================================================")
        print("ALL TESTS PASSED SUCCESSFULLY!")
        print("==================================================================")


if __name__ == "__main__":
    run_test()
