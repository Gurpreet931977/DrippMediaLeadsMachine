"""
test_cloud_run_job_dispatcher.py
================================
Unit tests for CloudRunJobDispatcher:
- Verifies in-process fallback when running in local development mode.
- Verifies GCP Cloud Run Job REST API payload construction and dispatch when configured.
- Verifies least-privilege configuration and parameters.
"""

import os
import unittest
from unittest.mock import patch, MagicMock

from lib.system.cloud_run_job_dispatcher import CloudRunJobDispatcher


class TestCloudRunJobDispatcher(unittest.TestCase):

    def test_local_environment_falls_back_to_in_process(self):
        """In local development (no K_SERVICE or production), dispatcher indicates in-process execution."""
        dispatcher = CloudRunJobDispatcher(project_id="poised-eye-509816-a4")
        with patch.dict(os.environ, {"ENVIRONMENT": "development", "K_SERVICE": ""}, clear=False):
            res = dispatcher.dispatch_discovery_job(
                country="United Kingdom",
                cities=["Manchester"],
                industry="Restaurants",
                leads=5,
                batch_size=10,
                max_multiplier=5,
                run_id="TEST-RUN-001",
                dry_run=True,
            )
            self.assertFalse(res["dispatched"])
            self.assertEqual(res["execution_type"], "IN_PROCESS")
            self.assertIn("in-process background worker", res["message"])

    def test_production_cloud_run_job_payload_and_dispatch(self):
        """In production Cloud Run environment, dispatcher calls Cloud Run v2 REST API."""
        dispatcher = CloudRunJobDispatcher(
            project_id="poised-eye-509816-a4",
            region="us-central1",
            job_name="dripp-discovery-job"
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "projects/poised-eye-509816-a4/locations/us-central1/jobs/dripp-discovery-job/executions/exec-12345"
        }

        mock_session = MagicMock()
        mock_session.post.return_value = mock_response

        with patch.dict(os.environ, {
            "ENVIRONMENT": "production",
            "K_SERVICE": "dripp-leads-api",
            "CLOUD_RUN_JOB_NAME": "dripp-discovery-job"
        }):
            with patch("google.auth.default", return_value=(MagicMock(), "poised-eye-509816-a4")):
                with patch("google.auth.transport.requests.AuthorizedSession", return_value=mock_session):
                    res = dispatcher.dispatch_discovery_job(
                        country="United Kingdom",
                        cities=["Manchester"],
                        industry="Restaurants",
                        leads=5,
                        batch_size=10,
                        max_multiplier=5,
                        run_id="CRJ-RUN-TEST-001",
                        dry_run=False,
                    )

                    self.assertTrue(res["dispatched"])
                    self.assertEqual(res["execution_type"], "CLOUD_RUN_JOB")
                    self.assertEqual(res["job_name"], "dripp-discovery-job")
                    self.assertEqual(res["status"], "QUEUED_ON_GCP")

                    # Verify API call URL and arguments
                    mock_session.post.assert_called_once()
                    call_args, call_kwargs = mock_session.post.call_args
                    self.assertIn("https://run.googleapis.com/v2/projects/poised-eye-509816-a4/locations/us-central1/jobs/dripp-discovery-job:run", call_args[0])
                    payload = call_kwargs["json"]
                    container_args = payload["overrides"]["containerOverrides"][0]["args"]
                    self.assertIn("--country", container_args)
                    self.assertIn("United Kingdom", container_args)
                    self.assertIn("--cities", container_args)
                    self.assertIn("Manchester", container_args)
                    self.assertIn("--leads", container_args)
                    self.assertIn("5", container_args)
                    self.assertIn("--run-id", container_args)
                    self.assertIn("CRJ-RUN-TEST-001", container_args)
                    self.assertNotIn("--dry-run", container_args)

    def test_dry_run_flag_included_when_requested(self):
        """Dry-run flag is correctly propagated to container overrides."""
        dispatcher = CloudRunJobDispatcher(
            project_id="poised-eye-509816-a4",
            region="us-central1",
            job_name="dripp-discovery-job"
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"name": "execution-dry-run-001"}
        mock_session = MagicMock()
        mock_session.post.return_value = mock_response

        with patch.dict(os.environ, {
            "ENVIRONMENT": "production",
            "FORCE_CLOUD_RUN_JOB": "true",
            "CLOUD_RUN_JOB_NAME": "dripp-discovery-job"
        }):
            with patch("google.auth.default", return_value=(MagicMock(), "poised-eye-509816-a4")):
                with patch("google.auth.transport.requests.AuthorizedSession", return_value=mock_session):
                    res = dispatcher.dispatch_discovery_job(
                        country="United Kingdom",
                        cities=["Manchester"],
                        industry="Restaurants",
                        leads=10,
                        run_id="RUN-DRY-01",
                        dry_run=True,
                    )
                    self.assertTrue(res["dispatched"])
                    payload = mock_session.post.call_args[1]["json"]
                    args = payload["overrides"]["containerOverrides"][0]["args"]
                    self.assertIn("--dry-run", args)


if __name__ == "__main__":
    unittest.main()
