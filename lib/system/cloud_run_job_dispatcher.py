"""
lib/system/cloud_run_job_dispatcher.py
======================================
Dispatcher adapter for triggering Google Cloud Run Jobs from the FastAPI Service.

Architecture:
- Cloud Run Service (FastAPI): Inbound HTTP request gateway, operator auth, status polling.
- Cloud Run Job (discovery worker): Heavyweight autonomous execution (up to 24h timeout),
  CPU allocated for task duration, single-worker execution, Firestore lease/checkpointing.

In GCP Cloud Run:
- The service initiates the job run via the Cloud Run v2 REST API using Application Default Credentials (ADC).
In Local Development:
- Safe fallback to in-process background task execution when Cloud Run Jobs is not configured.
"""

import os
import json
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("CloudRunJobDispatcher")

DEFAULT_PROJECT_ID = "poised-eye-509816-a4"
DEFAULT_REGION = "us-central1"
DEFAULT_JOB_NAME = "dripp-discovery-job"


class CloudRunJobDispatcher:
    """Dispatches discovery tasks to Cloud Run Jobs or falls back to in-process execution."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        region: Optional[str] = None,
        job_name: Optional[str] = None,
    ):
        self.project_id = (
            project_id
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or os.getenv("GCP_PROJECT")
            or DEFAULT_PROJECT_ID
        )
        self.region = (
            region
            or os.getenv("CLOUD_RUN_REGION")
            or os.getenv("GCP_REGION")
            or DEFAULT_REGION
        )
        self.job_name = (
            job_name
            or os.getenv("CLOUD_RUN_JOB_NAME")
            or DEFAULT_JOB_NAME
        )

    def is_cloud_run_configured(self) -> bool:
        """
        True if running in a managed Cloud Run environment or explicitly enabled via environment.
        Checks for Cloud Run metadata environment variables (K_SERVICE) or FORCE_CLOUD_RUN_JOB.
        Never dispatches to Cloud Run if LOCAL_ONLY is enabled or ENVIRONMENT is not production.
        """
        if os.getenv("LOCAL_ONLY", "").strip().lower() in ("true", "1", "yes"):
            return False
        in_cloud_run = bool(os.getenv("K_SERVICE"))
        forced = os.getenv("FORCE_CLOUD_RUN_JOB", "").strip().lower() in ("true", "1", "yes")
        explicit_job = bool(os.getenv("CLOUD_RUN_JOB_NAME"))
        return (in_cloud_run or forced or explicit_job) and os.getenv("ENVIRONMENT") == "production"

    def dispatch_discovery_job(
        self,
        country: str,
        cities: List[str],
        industry: str,
        leads: int,
        batch_size: int = 10,
        max_multiplier: int = 5,
        run_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Dispatches discovery task to Cloud Run Job API if in production Cloud Run,
        or instructs caller to execute in-process.
        """
        if not self.is_cloud_run_configured():
            logger.info("Cloud Run Job dispatch bypassed: running in local/fallback mode.")
            return {
                "dispatched": False,
                "execution_type": "IN_PROCESS",
                "message": "Local development runtime; task queued for in-process background worker.",
                "job_name": self.job_name,
                "run_id": run_id,
            }

        # Build CLI container override arguments
        # Cloud Run Jobs configured with entrypoint command 'python' requires the script path as args[0]
        city_arg = cities[0] if cities else "Manchester"
        cmd_args = [
            "scripts/run_cloud_run_job.py",
            "--country", country,
            "--cities", city_arg,
            "--industry", industry,
            "--leads", str(leads),
            "--batch-size", str(batch_size),
            "--max-multiplier", str(max_multiplier),
        ]
        if run_id:
            cmd_args.extend(["--run-id", run_id])
        if dry_run:
            cmd_args.append("--dry-run")

        payload = {
            "overrides": {
                "containerOverrides": [
                    {
                        "args": cmd_args
                    }
                ]
            }
        }

        api_url = (
            f"https://run.googleapis.com/v2/projects/{self.project_id}/"
            f"locations/{self.region}/jobs/{self.job_name}:run"
        )

        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            scopes = ["https://www.googleapis.com/auth/cloud-platform"]
            credentials, _ = google.auth.default(scopes=scopes)
            authed_session = AuthorizedSession(credentials)

            logger.info("Triggering Cloud Run Job '%s' via REST API: %s", self.job_name, api_url)
            response = authed_session.post(api_url, json=payload, timeout=30.0)

            if response.status_code in (200, 202):
                data = response.json()
                execution_name = data.get("name", "unknown")
                logger.info("Successfully triggered Cloud Run Job execution: %s", execution_name)
                return {
                    "dispatched": True,
                    "execution_type": "CLOUD_RUN_JOB",
                    "job_name": self.job_name,
                    "execution_name": execution_name,
                    "run_id": run_id,
                    "status": "QUEUED_ON_GCP",
                }
            else:
                error_msg = f"Cloud Run Jobs API returned HTTP {response.status_code}: {response.text}"
                logger.error(error_msg)
                return {
                    "dispatched": False,
                    "execution_type": "FAILED_DISPATCH",
                    "error": error_msg,
                    "run_id": run_id,
                }

        except Exception as exc:
            logger.exception("Failed to dispatch Cloud Run Job: %s", exc)
            return {
                "dispatched": False,
                "execution_type": "DISPATCH_EXCEPTION",
                "error": str(exc),
                "run_id": run_id,
            }


# Singleton instance
cloud_run_dispatcher = CloudRunJobDispatcher()
