#!/usr/bin/env python3
"""
scripts/setup_gosom.py
======================
Automated cross-platform Gosom scraper runner bootstrapper.
Ensures the correct pinned version of gosom/google-maps-scraper (v1.18.1)
is present and executable for the current operating system (Ubuntu Linux or macOS).
"""

import os
import sys
import platform
import subprocess
import urllib.request
import shutil

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRATCH_DIR = os.path.join(PROJECT_ROOT, "scratch")
SCRAPER_BIN = os.path.join(SCRATCH_DIR, "google_maps_scraper")
PINNED_VERSION = "v1.18.1"

# Official releases from github.com/gosom/google-maps-scraper/releases/tag/v1.18.1
RELEASE_URLS = {
    ("Linux", "x86_64"): f"https://github.com/gosom/google-maps-scraper/releases/download/{PINNED_VERSION}/google_maps_scraper-1.18.1-linux-amd64",
    ("Darwin", "x86_64"): f"https://github.com/gosom/google-maps-scraper/releases/download/{PINNED_VERSION}/google_maps_scraper-1.18.1-darwin-amd64",
    ("Darwin", "arm64"): f"https://github.com/gosom/google-maps-scraper/releases/download/{PINNED_VERSION}/google_maps_scraper-1.18.1-darwin-arm64",
}


def is_binary_runnable(binary_path: str) -> bool:
    if not os.path.exists(binary_path) or not os.access(binary_path, os.X_OK):
        return False
    try:
        proc = subprocess.run([binary_path, "-help"], capture_output=True, text=True, timeout=5)
        # Even if it exits with error or prints usage, if it started, it is runnable
        return True
    except (PermissionError, OSError):
        return False
    except Exception:
        return False


def setup_gosom() -> bool:
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    os_name = platform.system()
    machine = platform.machine()
    print(f"[Gosom Setup] Current platform: {os_name} {machine}")

    # 1. Check if existing binary is runnable
    if is_binary_runnable(SCRAPER_BIN):
        print(f"[Gosom Setup] Existing binary at {SCRAPER_BIN} is valid and executable.")
        return True

    print(f"[Gosom Setup] Binary missing or incompatible with {os_name} {machine}. Acquiring {PINNED_VERSION}...")

    # 2. Try downloading official pinned release binary
    download_url = RELEASE_URLS.get((os_name, machine))
    if download_url:
        print(f"[Gosom Setup] Downloading release from: {download_url}")
        try:
            req = urllib.request.Request(
                download_url,
                headers={"User-Agent": "Mozilla/5.0 (GitHubActions/CI; DrippLeadEngine)"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp, open(SCRAPER_BIN, "wb") as out_file:
                shutil.copyfileobj(resp, out_file)
            os.chmod(SCRAPER_BIN, 0o755)
            if is_binary_runnable(SCRAPER_BIN):
                print(f"[Gosom Setup] Successfully downloaded and verified {SCRAPER_BIN}")
                return True
        except Exception as e:
            print(f"[Gosom Setup] Download failed: {e}")

    # 3. Fallback: try `go install` if Go toolchain is installed
    go_bin = shutil.which("go")
    if go_bin:
        print("[Gosom Setup] Attempting build via 'go install'...")
        try:
            subprocess.run([go_bin, "install", f"github.com/gosom/google-maps-scraper@{PINNED_VERSION}"], check=True)
            gopath = os.environ.get("GOPATH") or os.path.expanduser("~/go")
            installed_path = os.path.join(gopath, "bin", "google-maps-scraper")
            if os.path.exists(installed_path):
                shutil.copy2(installed_path, SCRAPER_BIN)
                os.chmod(SCRAPER_BIN, 0o755)
                if is_binary_runnable(SCRAPER_BIN):
                    print(f"[Gosom Setup] Successfully built and verified {SCRAPER_BIN}")
                    return True
        except Exception as e:
            print(f"[Gosom Setup] Go build failed: {e}")

    print("[Gosom Setup] WARNING: Could not install native Gosom binary. Fallback caches & mock providers will be used.")
    return False


if __name__ == "__main__":
    success = setup_gosom()
    # Return 0 so step doesn't abort if network is offline in synthetic environments
    sys.exit(0)
