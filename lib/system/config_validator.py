"""
lib/system/config_validator.py
==============================
Startup and environment configuration audit.
Validates required system resources without leaking credentials.
Reports status as:
  - CONFIGURED
  - MISSING
  - INVALID
  - DISABLED

CRITICAL INVARIANT:
  Do not fail the application because a disabled commercial provider is missing.
"""

import os
import json
import logging
from typing import Dict, Any, Tuple

from lib.system.system_config import SystemConfig

logger = logging.getLogger("ConfigValidator")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


class ConfigValidator:
    """
    Validates technical operational readiness and third-party provider configurations.
    """

    @classmethod
    def validate_all(cls) -> Dict[str, Any]:
        """
        Runs comprehensive configuration audit across storage, technical, and commercial services.
        """
        results: Dict[str, Dict[str, Any]] = {}

        # 1. Storage & Filesystem
        data_exists = os.path.exists(DATA_DIR)
        data_writable = os.access(DATA_DIR, os.W_OK) if data_exists else False
        if data_exists and data_writable:
            results["storage"] = {
                "status": "CONFIGURED",
                "details": f"Data directory verified at {DATA_DIR}",
                "critical": True,
            }
        else:
            results["storage"] = {
                "status": "INVALID",
                "details": f"Data directory missing or not writable at {DATA_DIR}",
                "critical": True,
            }

        # 2. Google Sheets CRM Integration
        sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
        sa_env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        default_sa = os.path.join(PROJECT_ROOT, "poised-eye-509816-a4-5444ff3f8520.json")
        sa_path = sa_env if (sa_env and os.path.exists(sa_env)) else (default_sa if os.path.exists(default_sa) else None)

        sheet_id = os.environ.get("SPREADSHEET_ID") or os.environ.get("GOOGLE_SHEET_ID") or os.environ.get("GOOGLE_SHEET_URL")
        if sa_json:
            try:
                sa_data = json.loads(sa_json) if isinstance(sa_json, str) else sa_json
                client_email = sa_data.get("client_email", "")
                masked_email = client_email[:4] + "***" + client_email[-12:] if len(client_email) > 16 else "[MASKED]"
                results["google_sheets"] = {
                    "status": "CONFIGURED",
                    "details": f"Service account verified from env ({masked_email})",
                    "critical": False,
                }
            except Exception as e:
                results["google_sheets"] = {
                    "status": "INVALID",
                    "details": f"Failed reading GOOGLE_SERVICE_ACCOUNT_JSON: {e}",
                    "critical": False,
                }
        elif os.environ.get("GOOGLE_SERVICE_ACCOUNT_EMAIL") and os.environ.get("GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY"):
            client_email = os.environ.get("GOOGLE_SERVICE_ACCOUNT_EMAIL", "")
            masked_email = client_email[:4] + "***" + client_email[-12:] if len(client_email) > 16 else "[MASKED]"
            results["google_sheets"] = {
                "status": "CONFIGURED",
                "details": f"Service account verified from individual env vars ({masked_email})",
                "critical": False,
            }
        elif sa_path and os.path.exists(sa_path):
            try:
                with open(sa_path, "r", encoding="utf-8") as f:
                    sa_data = json.load(f)
                client_email = sa_data.get("client_email", "")
                masked_email = client_email[:4] + "***" + client_email[-12:] if len(client_email) > 16 else "[MASKED]"
                results["google_sheets"] = {
                    "status": "CONFIGURED",
                    "details": f"Service account verified ({masked_email})",
                    "critical": False,  # Local fallback caches exist
                }
            except Exception as e:
                results["google_sheets"] = {
                    "status": "INVALID",
                    "details": f"Failed reading service account credentials: {e}",
                    "critical": False,
                }
        else:
            results["google_sheets"] = {
                "status": "MISSING",
                "details": "Service account JSON not located (using local JSON cache fallback)",
                "critical": False,
            }

        # 3. Gosom Local Review Scraper
        default_scraper = os.path.join(PROJECT_ROOT, "scratch", "google_maps_scraper")
        gosom_path = (
            os.environ.get("GOSOM_BINARY_PATH")
            or os.environ.get("GOSOM_SCRAPER_BIN")
            or (default_scraper if os.path.exists(default_scraper) else None)
            or shutil_which("gosom")
            or shutil_which("google_maps_scraper")
        )
        if gosom_path and os.path.exists(gosom_path):
            results["gosom"] = {
                "status": "CONFIGURED",
                "details": f"Gosom binary found at {gosom_path}",
                "critical": False,
            }
        else:
            results["gosom"] = {
                "status": "DISABLED",
                "details": "Gosom binary not found (using local review cache & provider fallback)",
                "critical": False,
            }

        # 4. Apify Actor Integration
        apify_token = os.environ.get("APIFY_API_TOKEN") or os.environ.get("APIFY_TOKEN")
        apify_enabled = os.environ.get("APIFY_ENABLED", "false").lower() in ("true", "1")
        if apify_enabled:
            if apify_token and len(apify_token) > 10:
                results["apify"] = {
                    "status": "CONFIGURED",
                    "details": "Apify token configured",
                    "critical": False,
                }
            else:
                results["apify"] = {
                    "status": "INVALID",
                    "details": "Apify enabled but API token is missing or malformed",
                    "critical": False,
                }
        else:
            results["apify"] = {
                "status": "DISABLED",
                "details": "Apify scraping disabled (using OSM + Foursquare + web search)",
                "critical": False,
            }

        # 5. Commercial Outbound: SMTP / Email
        commercial_active = SystemConfig.can_execute_commercial_actions()
        smtp_host = os.environ.get("SMTP_HOST") or os.environ.get("SENDGRID_API_KEY")
        if commercial_active:
            if smtp_host:
                results["email_provider"] = {
                    "status": "CONFIGURED",
                    "details": "Email outbound credentials configured",
                    "critical": True,
                }
            else:
                results["email_provider"] = {
                    "status": "MISSING",
                    "details": "Commercial actions active but no email provider configured",
                    "critical": True,
                }
        else:
            results["email_provider"] = {
                "status": "DISABLED",
                "details": "Commercial actions locked (Travel Mode active); outbound email disabled",
                "critical": False,
            }

        # 6. Commercial Outbound: Meta APIs (Instagram / Facebook)
        meta_token = os.environ.get("META_ACCESS_TOKEN") or os.environ.get("INSTAGRAM_ACCESS_TOKEN")
        if commercial_active:
            if meta_token:
                results["meta_apis"] = {
                    "status": "CONFIGURED",
                    "details": "Meta Graph API token configured",
                    "critical": False,
                }
            else:
                results["meta_apis"] = {
                    "status": "MISSING",
                    "details": "Meta token missing for commercial social messaging",
                    "critical": False,
                }
        else:
            results["meta_apis"] = {
                "status": "DISABLED",
                "details": "Commercial actions locked (Travel Mode active); Meta messaging disabled",
                "critical": False,
            }

        # 7. Scheduler & Technical Automation
        results["scheduler"] = {
            "status": "CONFIGURED" if SystemConfig.TECHNICAL_AUTOMATION_ENABLED else "DISABLED",
            "details": f"Technical automation master switch is {'ENABLED' if SystemConfig.TECHNICAL_AUTOMATION_ENABLED else 'DISABLED'}",
            "critical": True,
        }

        # Determine overall startup viability
        critical_failures = [
            k for k, v in results.items()
            if v.get("critical") and v.get("status") in ("INVALID", "MISSING")
        ]
        startup_ok = len(critical_failures) == 0

        return {
            "startup_ok": startup_ok,
            "critical_failures": critical_failures,
            "services": results,
            "travel_mode": SystemConfig.TRAVEL_MODE,
            "commercial_actions_enabled": SystemConfig.COMMERCIAL_ACTIONS_ENABLED,
        }


def shutil_which(cmd: str) -> Optional[str]:
    import shutil
    return shutil.which(cmd)
