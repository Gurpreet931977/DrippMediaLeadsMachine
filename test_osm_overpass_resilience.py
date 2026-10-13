"""
test_osm_overpass_resilience.py
===============================
Tests verifying:
1. Overpass query timeout on primary endpoint gracefully falls back to next mirror and succeeds.
2. HTTP 504 (Gateway Timeout) or 429 (Rate Limit) on primary endpoint falls back and succeeds.
3. When all endpoints fail, OverpassQueryError is raised (never silently swallowed into empty list).
4. HybridDiscoveryEngine fails-fast with descriptive RuntimeError when primary discovery has an outage and pool is empty.
5. HybridDiscoveryEngine returns empty results cleanly without error when provider legitimately returns 0 matching businesses.
6. LeadGenerationPipeline raises and aborts instead of marking PIPELINE_STATUS = COMPLETE when discovery provider fails with zero candidates.
7. Cloud Run Job runner (run_cloud_run_job.py) marks the run as FAILED in Firestore and exits with code 1 upon discovery outage.
8. OVERPASS_ENDPOINTS and OVERPASS_TIMEOUT environment variable overrides are honored.
"""

import os
import argparse
import unittest
from unittest.mock import MagicMock, patch
import requests

from lib.types import DiscoveredBusiness
from lib.discovery.osm import (
    OpenStreetMapProvider,
    OverpassQueryError,
    get_default_endpoints,
    DEFAULT_OVERPASS_ENDPOINTS,
)
from lib.discovery.hybrid import HybridDiscoveryEngine
from lib.pipeline import LeadGenerationPipeline
from lib.system.firestore_state_store import FirestoreStateStore
from scripts.run_cloud_run_job import run_discovery_task


class TestOSMOverpassResilience(unittest.TestCase):

    def setUp(self):
        # Use isolated cache dir to avoid cache hits during unit testing
        self.test_cache_dir = "/tmp/test_cache_osm_resilience"
        if os.path.exists(self.test_cache_dir):
            import shutil
            shutil.rmtree(self.test_cache_dir, ignore_errors=True)

    def tearDown(self):
        if os.path.exists(self.test_cache_dir):
            import shutil
            shutil.rmtree(self.test_cache_dir, ignore_errors=True)

    def test_overpass_timeout_falls_back_to_next_endpoint_and_succeeds(self):
        """When the first endpoint times out, provider retries / falls back to the next mirror and succeeds."""
        provider = OpenStreetMapProvider(
            cache_dir=self.test_cache_dir,
            min_request_interval=0.0,
            endpoints=["https://endpoint1.osm/api/interpreter", "https://endpoint2.osm/api/interpreter"]
        )

        mock_success_resp = MagicMock()
        mock_success_resp.status_code = 200
        mock_success_resp.json.return_value = {
            "elements": [
                {
                    "type": "node",
                    "id": 101,
                    "lat": 53.48,
                    "lon": -2.24,
                    "tags": {"name": "Fallback Cafe", "amenity": "cafe"}
                }
            ]
        }

        # First endpoint times out, second endpoint returns 200 OK
        def mock_post_side_effect(url, *args, **kwargs):
            if "endpoint1.osm" in url:
                raise requests.exceptions.Timeout("Read timed out on endpoint1")
            elif "endpoint2.osm" in url:
                return mock_success_resp
            raise requests.exceptions.ConnectionError("Unknown endpoint")

        with patch("requests.post", side_effect=mock_post_side_effect):
            result = provider.execute_overpass_query("test_query_timeout")
            self.assertEqual(len(result.get("elements", [])), 1)
            self.assertEqual(result["elements"][0]["tags"]["name"], "Fallback Cafe")
            self.assertEqual(provider.metrics.errors, 0)
            self.assertGreater(provider.metrics.calls, 0)

    def test_overpass_http_504_gateway_timeout_falls_back_and_succeeds(self):
        """When an endpoint returns HTTP 504 (e.g. lz4 overloaded), it falls back to a healthy mirror."""
        provider = OpenStreetMapProvider(
            cache_dir=self.test_cache_dir,
            min_request_interval=0.0,
            endpoints=["https://lz4.overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
        )

        mock_504_resp = MagicMock()
        mock_504_resp.status_code = 504
        mock_504_resp.text = "Gateway Timeout"

        mock_success_resp = MagicMock()
        mock_success_resp.status_code = 200
        mock_success_resp.json.return_value = {
            "elements": [
                {
                    "type": "node",
                    "id": 202,
                    "lat": 53.48,
                    "lon": -2.24,
                    "tags": {"name": "Private Coffee Grill", "amenity": "restaurant"}
                }
            ]
        }

        with patch("time.sleep"), patch("requests.post") as mock_post:
            def side_effect(url, *args, **kwargs):
                if "lz4.overpass-api.de" in url:
                    return mock_504_resp
                return mock_success_resp

            mock_post.side_effect = side_effect
            result = provider.execute_overpass_query("test_query_504")
            self.assertEqual(len(result.get("elements", [])), 1)
            self.assertEqual(result["elements"][0]["tags"]["name"], "Private Coffee Grill")

    def test_overpass_http_429_rate_limit_backs_off_and_falls_back(self):
        """When an endpoint returns HTTP 429, provider backs off and falls over to the next mirror."""
        provider = OpenStreetMapProvider(
            cache_dir=self.test_cache_dir,
            min_request_interval=0.0,
            endpoints=["https://busy.overpass/api/interpreter", "https://backup.overpass/api/interpreter"]
        )

        mock_429_resp = MagicMock()
        mock_429_resp.status_code = 429
        mock_429_resp.text = "Rate limit exceeded"

        mock_success_resp = MagicMock()
        mock_success_resp.status_code = 200
        mock_success_resp.json.return_value = {
            "elements": [{"type": "node", "id": 303, "tags": {"name": "Backup Bistro"}}]
        }

        with patch("time.sleep") as mock_sleep, patch("requests.post") as mock_post:
            def side_effect(url, *args, **kwargs):
                if "busy.overpass" in url:
                    return mock_429_resp
                return mock_success_resp

            mock_post.side_effect = side_effect
            result = provider.execute_overpass_query("test_query_429")
            self.assertEqual(len(result.get("elements", [])), 1)
            self.assertEqual(result["elements"][0]["tags"]["name"], "Backup Bistro")
            # Verify backoff sleep was called
            self.assertTrue(mock_sleep.called)

    def test_overpass_all_endpoints_fail_raises_overpass_query_error(self):
        """When ALL endpoints fail/time out, OverpassQueryError is raised, never silently returning elements: []."""
        provider = OpenStreetMapProvider(
            cache_dir=self.test_cache_dir,
            min_request_interval=0.0,
            endpoints=["https://ep1.test/api/interpreter", "https://ep2.test/api/interpreter"]
        )

        with patch("time.sleep"), patch("requests.post", side_effect=requests.exceptions.Timeout("Connection timed out")):
            with self.assertRaises(OverpassQueryError) as ctx:
                provider.execute_overpass_query("test_query_all_fail")

            self.assertIn("OpenStreetMap Overpass query failed across all 2 endpoints", str(ctx.exception))
            self.assertIn("timeout", str(ctx.exception).lower())
            self.assertEqual(provider.metrics.errors, 1)

    def test_hybrid_discovery_raises_outage_when_osm_fails_and_no_candidates(self):
        """HybridDiscoveryEngine raises descriptive RuntimeError when OSM fails and no other provider yields candidates."""
        engine = HybridDiscoveryEngine(mode="HYBRID", apify_enabled=False)
        engine.foursquare.api_key = ""  # No Foursquare

        # Mock OSM search_businesses to raise OverpassQueryError
        with patch.object(engine.osm, "search_businesses", side_effect=OverpassQueryError("All endpoints down")):
            with self.assertRaises(RuntimeError) as ctx:
                engine.discover_round(round_number=1, country="United Kingdom", cities=["Manchester"], industry="Restaurants")

            self.assertIn("Discovery provider outage", str(ctx.exception))
            self.assertIn("All endpoints down", str(ctx.exception))

    def test_hybrid_discovery_legitimate_empty_result_does_not_raise(self):
        """HybridDiscoveryEngine returns empty list cleanly when provider legitimately finds 0 matching businesses (no error)."""
        engine = HybridDiscoveryEngine(mode="HYBRID", apify_enabled=False)
        engine.foursquare.api_key = ""

        # Mock OSM search_businesses returning clean empty list (successful response with 0 elements)
        with patch.object(engine.osm, "search_businesses", return_value=[]):
            results, queries = engine.discover_round(
                round_number=1, country="United Kingdom", cities=["Manchester"], industry="UnusualIndustry"
            )
            self.assertEqual(results, [])
            self.assertTrue(len(queries) > 0)

    def test_pipeline_discovery_outage_raises_and_aborts_run(self):
        """LeadGenerationPipeline raises RuntimeError on initial discovery outage with 0 candidates (never claiming COMPLETE)."""
        mock_discovery = MagicMock()
        mock_discovery.name = "MockDiscovery"
        mock_discovery.has_more_rounds.return_value = True
        mock_discovery.discover_round.side_effect = RuntimeError("Discovery provider outage: Overpass timed out")

        pipeline = LeadGenerationPipeline(
            discovery_provider=mock_discovery,
            shadow_mode=True
        )

        logs = []
        def log_cb(msg, *args, **kwargs):
            logs.append(msg)

        with self.assertRaises(RuntimeError) as ctx:
            pipeline.run(
                country="United Kingdom",
                cities=["Manchester"],
                industry="Restaurants",
                requested_qualified_leads=5,
                batch_size=5,
                log_callback=log_cb
            )

        self.assertIn("Discovery provider failure", str(ctx.exception))
        # Ensure log did NOT claim PIPELINE_STATUS = COMPLETE
        all_logs_text = "\n".join(logs)
        self.assertNotIn("STATUS: PIPELINE_STATUS = COMPLETE", all_logs_text)
        self.assertIn("[FATAL ERROR] Discovery provider outage resulted in 0 candidates", all_logs_text)

    def test_cloud_run_job_marks_failed_on_discovery_outage(self):
        """run_discovery_task marks the run as FAILED in Firestore and exits with code 1 when discovery fails."""
        mock_store = FirestoreStateStore(force_mock=True)

        args = argparse.Namespace(
            country="United Kingdom",
            cities=["Manchester"],
            industry="Restaurants",
            leads=5,
            batch_size=5,
            max_multiplier=2,
            run_id="RUN-OUTAGE-TEST-001",
            dry_run=True,
            lease_name="test_outage_lease",
            ttl_seconds=60.0
        )

        env_patch = {
            "TRAVEL_MODE": "ACTIVE",
            "COMMERCIAL_ACTIONS_ENABLED": "false",
            "AUTOMATED_EMAIL_ENABLED": "false",
            "DRY_RUN": "TRUE",
            "LEAD_LIMIT": "5"
        }

        # Simulate discovery provider raising an outage error
        with patch.dict(os.environ, env_patch), \
             patch("scripts.run_cloud_run_job.firestore_store", mock_store), \
             patch("lib.discovery.osm.OpenStreetMapProvider.execute_overpass_query", side_effect=OverpassQueryError("All Overpass endpoints timed out")):
            exit_code = run_discovery_task(args)
            self.assertEqual(exit_code, 1)

            # Check Firestore record
            saved_record = mock_store.get_run_record("RUN-OUTAGE-TEST-001")
            self.assertIsNotNone(saved_record)
            self.assertEqual(saved_record.get("status"), "FAILED")
            self.assertIn("All Overpass endpoints timed out", saved_record.get("error", ""))

    def test_overpass_endpoints_env_override(self):
        """OVERPASS_ENDPOINTS environment variable cleanly configures custom endpoints."""
        custom_endpoints = "https://custom1.osm/api/interpreter, https://custom2.osm/api/interpreter"
        with patch.dict(os.environ, {"OVERPASS_ENDPOINTS": custom_endpoints}):
            endpoints = get_default_endpoints()
            self.assertEqual(endpoints, ["https://custom1.osm/api/interpreter", "https://custom2.osm/api/interpreter"])

            provider = OpenStreetMapProvider(cache_dir=self.test_cache_dir)
            self.assertEqual(provider.endpoints, ["https://custom1.osm/api/interpreter", "https://custom2.osm/api/interpreter"])

    def test_overpass_timeout_env_override(self):
        """OVERPASS_TIMEOUT environment variable cleanly configures request timeout."""
        with patch.dict(os.environ, {"OVERPASS_TIMEOUT": "45"}):
            provider = OpenStreetMapProvider(cache_dir=self.test_cache_dir)
            self.assertEqual(provider.timeout, 45)


if __name__ == "__main__":
    unittest.main()
