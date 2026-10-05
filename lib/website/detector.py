import re
import urllib.parse
import subprocess
import os
import requests
from typing import Tuple, Dict, Any, Optional
from lib.types import WebsiteStatus

PLATFORM_DOMAINS = [
    "facebook.com", "fb.com", "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "linkedin.com", "youtube.com", "pinterest.com",
    "linktr.ee", "bio.link", "beacons.ai", "solo.to", "taplink.cc", "campsite.bio", "bento.me",
    "deliveroo.co.uk", "deliveroo.com", "ubereats.com", "just-eat.co.uk", "just-eat.com",
    "doordash.com", "grubhub.com", "foodhub.co.uk", "zomato.com", "swiggy.com",
    "opentable.co.uk", "opentable.com", "resy.com", "thefork.co.uk", "thefork.com",
    "sevenrooms.com", "quandoo.co.uk", "designmynight.com", "tablein.com",
    "tripadvisor.co.uk", "tripadvisor.com", "yelp.co.uk", "yelp.com", "yell.com",
    "google.com", "goo.gl", "maps.google.com", "business.google.com", "restaurantguru.com",
    "wanderlog.com", "happycow.net", "square.site", "wixsite.com",
    "cylex-uk.co.uk", "thomsonlocal.com", "scoot.co.uk", "192.com", "misterwhat.co.uk",
    "allinlondon.co.uk", "eatout.co.uk", "menupages.co.uk", "food.gov.uk", "booking.com",
    "trustpilot.com", "foursquare.com", "checkatrade.com", "companieshouse.gov.uk"
]

class NodeWebsiteDetectionProvider:
    """
    Website detector that analyzes raw website URLs, detects platform/social links,
    tests live domain reachability, and interfaces with the Node.js website auditor.
    """
    def __init__(self, node_auditor_dir: Optional[str] = None):
        self.node_auditor_dir = node_auditor_dir or "/Users/metagurpreet/.gemini/antigravity-ide/scratch/apify-workspace"

    def is_platform_url(self, url: str) -> Tuple[bool, Optional[str]]:
        if not url:
            return False, None
        u_lower = url.lower()
        for p in PLATFORM_DOMAINS:
            if p in u_lower:
                return True, p
        return False, None

    def check_reachability(self, url: str, timeout: int = 5) -> Tuple[bool, str, Optional[int]]:
        """
        Tests if a given website URL is reachable or broken.
        Returns: (is_reachable, reason, status_code)
        """
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        try:
            r = requests.get(url, timeout=timeout, headers=headers, allow_redirects=True)
            if 200 <= r.status_code < 400:
                return True, f"Reachable (HTTP {r.status_code})", r.status_code
            elif r.status_code in [404, 410]:
                return False, f"Not Found (HTTP {r.status_code})", r.status_code
            elif r.status_code >= 500:
                return False, f"Server Error (HTTP {r.status_code})", r.status_code
            else:
                return False, f"Unreachable status code (HTTP {r.status_code})", r.status_code
        except requests.exceptions.SSLError:
            # Try plain http
            if url.startswith("https://"):
                return self.check_reachability("http://" + url[8:], timeout=timeout)
            return False, "SSL Certificate Error", None
        except requests.exceptions.ConnectionError:
            return False, "Connection Refused / DNS Resolution Failed", None
        except requests.exceptions.Timeout:
            return False, "Connection Timed Out", None
        except Exception as e:
            return False, f"Request failed: {str(e)}", None

    def detect_website(self, raw_website: str) -> Dict[str, Any]:
        """
        First-stage website detection.
        Returns classification: WEBSITE_EXISTS, NO_WEBSITE_CONFIRMED (candidate), or WEBSITE_BROKEN.
        """
        raw_website = (raw_website or "").strip()
        if not raw_website:
            return {
                "website_status": WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
                "clean_website": "",
                "is_platform": False,
                "platform_type": None,
                "reason": "No website URL provided in discovery record."
            }

        is_plat, plat_type = self.is_platform_url(raw_website)
        if is_plat:
            return {
                "website_status": WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
                "clean_website": "",
                "extracted_platform_url": raw_website,
                "is_platform": True,
                "platform_type": plat_type,
                "reason": f"Discovered website is a platform link ({plat_type}), not a custom business domain."
            }

        # Check reachability of the custom domain
        reachable, reach_reason, status_code = self.check_reachability(raw_website)
        if reachable:
            return {
                "website_status": WebsiteStatus.WEBSITE_EXISTS.value,
                "clean_website": raw_website,
                "is_platform": False,
                "platform_type": None,
                "reason": f"Official custom domain is active and reachable: {reach_reason}"
            }
        else:
            return {
                "website_status": WebsiteStatus.WEBSITE_BROKEN.value,
                "clean_website": raw_website,
                "is_platform": False,
                "platform_type": None,
                "reason": f"Custom domain listed but unreachable/broken: {reach_reason}"
            }

    def run_node_auditor(self, url: str) -> Optional[str]:
        """
        Runs the existing Node.js website auditor (Lighthouse/Pa11y) if available.
        Only called when a website exists or is broken for redesign intelligence.
        """
        if not os.path.exists(os.path.join(self.node_auditor_dir, "audit.js")):
            return None
        try:
            proc = subprocess.run(
                ["node", "audit.js", url],
                cwd=self.node_auditor_dir,
                capture_output=True,
                text=True,
                timeout=45
            )
            return proc.stdout
        except Exception as e:
            return f"Auditor error: {str(e)}"
