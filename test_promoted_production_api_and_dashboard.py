"""
test_promoted_production_api_and_dashboard.py
============================================
Automated post-promotion verification suite for Dripp Leads API & Dashboard:
1. Health probe on production primary URL.
2. Unauthenticated fail-closed endpoint security check.
3. Dashboard HTML & static asset delivery.
4. Headless Playwright browser verification for rendered UI components & view transitions.
"""

import sys
import time
import json
import urllib.request
import urllib.error
from playwright.sync_api import sync_playwright

PROD_URL = "https://dripp-leads-api-ukng7vge6q-uc.a.run.app"


def test_public_health():
    print("\n--- [TEST 1: Production Primary Health Probe] ---")
    url = f"{PROD_URL}/api/system/health"
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "HealthCheck/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        elapsed = time.time() - t0
        assert resp.status == 200, f"Expected 200, got {resp.status}"
        data = json.loads(resp.read().decode("utf-8"))
        status = data.get("canonical_health_status") or data.get("status")
        print(f"✓ HTTP {resp.status} in {elapsed:.2f}s | Status: {status}")
        assert status == "OPERATIONAL", f"Expected OPERATIONAL, got {status}"


def test_unauthenticated_fail_closed():
    print("\n--- [TEST 2: Fail-Closed Protected Endpoints Security] ---")
    protected_paths = [
        "/api/leads",
        "/api/crm/status",
        "/api/system/readiness",
        "/api/runs/candidates",
        "/api/operator/session"
    ]
    for path in protected_paths:
        url = f"{PROD_URL}{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "SecurityProbe/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raise AssertionError(f"Expected 401 for {path}, got {resp.status}")
        except urllib.error.HTTPError as e:
            assert e.code == 401, f"Expected 401 for {path}, got {e.code}"
            print(f"✓ {path} correctly fails closed with HTTP 401 Unauthorized")


def test_dashboard_assets():
    print("\n--- [TEST 3: Dashboard HTML & Static Assets Delivery] ---")
    req = urllib.request.Request(PROD_URL, headers={"User-Agent": "BrowserCheck/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 200
        html = resp.read().decode("utf-8")
        assert "Lead Operating System" in html
        assert "Apple Liquid Glass Material System" in html
        print("✓ Root dashboard HTML delivered (200 OK) with Lead Operating System title")

    for asset in ["/static/logos/favicon-32.png", "/static/logos/favicon.png"]:
        asset_url = f"{PROD_URL}{asset}"
        with urllib.request.urlopen(asset_url, timeout=10) as resp:
            assert resp.status == 200
            print(f"✓ Static asset {asset} delivered (HTTP 200)")


def test_browser_dashboard_rendering():
    print("\n--- [TEST 4: Playwright Headless Browser UI Verification] ---")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # Pre-configure backend API URL to production origin
        page.add_init_script("localStorage.setItem('dripp_backend_api_url', window.location.origin);")

        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        print(f"Navigating to {PROD_URL}...")
        t0 = time.time()
        page.goto(PROD_URL, wait_until="networkidle", timeout=30000)
        load_time = time.time() - t0
        print(f"✓ Page loaded in {load_time:.2f}s")

        # Verify page title
        title = page.title()
        print(f"Page Title: '{title}'")
        assert "Dripp International Leads" in title

        # Verify brand elements
        logo_visible = page.is_visible(".brand-logo-img")
        canvas_visible = page.is_visible("#ambientCanvas")
        print(f"Brand Logo Visible: {logo_visible} | Ambient Canvas Visible: {canvas_visible}")
        assert logo_visible and canvas_visible

        # Verify navigation items
        nav_links = page.eval_on_selector_all(".nav-link", "els => els.map(e => e.innerText.replace(/\\n/g, ' '))")
        print(f"Navigation Links: {nav_links}")
        assert len(nav_links) == 5

        # Test view transitions across all workspaces
        views = ["home", "discovery", "leads", "pipeline", "system"]
        for dest in views:
            page.evaluate(f'navigateTo("{dest}")')
            is_active = page.eval_on_selector(f"#view-{dest}", "e => e.classList.contains('active')")
            print(f"✓ Navigated to {dest.upper()} space (#view-{dest} active={is_active})")
            assert is_active is True

        # Navigate back to Home
        page.evaluate('navigateTo("home")')

        # Filter out benign 401 resource errors before operator authentication
        unhandled_js_errors = [e for e in console_errors if "401" not in e and "favicon" not in e]
        print(f"Unhandled JS Console Errors: {len(unhandled_js_errors)}")
        assert len(unhandled_js_errors) == 0, f"Unexpected browser console errors: {unhandled_js_errors}"

        context.close()
        browser.close()
    print("✓ Playwright Browser UI verification passed with zero unhandled errors.")


def main():
    print("==================================================================")
    print(f"PRODUCTION API & DASHBOARD VERIFICATION: {PROD_URL}")
    print("==================================================================")
    test_public_health()
    test_unauthenticated_fail_closed()
    test_dashboard_assets()
    test_browser_dashboard_rendering()
    print("\n==================================================================")
    print("ALL PRODUCTION POST-PROMOTION TESTS PASSED SUCCESSFULLY!")
    print("==================================================================")


if __name__ == "__main__":
    main()
