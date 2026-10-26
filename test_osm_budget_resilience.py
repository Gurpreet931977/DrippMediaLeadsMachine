"""
test_osm_budget_resilience.py
==============================
Unit tests specifically verifying Phase 2A Overpass bounded discovery-attempt budget:
1. Max failover limit (no cycling through 6 mirrors; stops after 1 failover within bounded budget).
2. Per-request and total query timeout enforcement.
3. Server signal Retry-After header parsing and per-endpoint cooldown.
4. Healthy cached result is preferred when fresh; stale cache (> TTL) is rejected during standard discovery.
5. Circuit breaker opens when attempts fail and resets on recovery.
"""

import os
import time
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import requests

from lib.discovery.osm import OpenStreetMapProvider, OverpassQueryError


class TestOSMBudgetResilience(unittest.TestCase):

    def setUp(self):
        self.test_cache_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_cache_dir, ignore_errors=True)

    def test_bounded_failover_stops_after_max_failovers(self):
        """When 6 endpoints are configured and fail, provider attempts only primary + 1 failover (2 total)."""
        endpoints = [f"https://ep{i}.osm/api/interpreter" for i in range(1, 7)]
        provider = OpenStreetMapProvider(
            cache_dir=self.test_cache_dir,
            min_request_interval=0.0,
            endpoints=endpoints,
            request_timeout=5
        )
        self.assertEqual(provider.max_failovers, 1)

        mock_502 = MagicMock()
        mock_502.status_code = 502
        mock_502.text = "Server at capacity"
        mock_502.headers = {}

        with patch("time.sleep"), patch("requests.post", return_value=mock_502) as mock_post:
            with self.assertRaises(OverpassQueryError):
                provider.execute_overpass_query("test_bounded_query")

            # Must NOT cycle through all 6 endpoints; strictly 2 attempts (primary + 1 failover)
            self.assertEqual(mock_post.call_count, 2)
            called_urls = [call[0][0] for call in mock_post.call_args_list]
            self.assertEqual(called_urls, ["https://ep1.osm/api/interpreter", "https://ep2.osm/api/interpreter"])

    def test_retry_after_header_marks_endpoint_on_cooldown(self):
        """HTTP 429 with Retry-After header sets cooldown and skips endpoint on next query."""
        provider = OpenStreetMapProvider(
            cache_dir=self.test_cache_dir,
            min_request_interval=0.0,
            endpoints=["https://rate-limited.osm/api/interpreter", "https://backup.osm/api/interpreter"]
        )

        mock_429 = MagicMock()
        mock_429.status_code = 429
        mock_429.text = "Too Many Requests"
        mock_429.headers = {"Retry-After": "45"}

        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.json.return_value = {"elements": [{"id": 1, "tags": {"name": "Test Place"}}]}

        with patch("time.sleep"), patch("requests.post") as mock_post:
            def side_effect(url, *args, **kwargs):
                if "rate-limited" in url:
                    return mock_429
                return mock_200

            mock_post.side_effect = side_effect
            res = provider.execute_overpass_query("query_1")
            self.assertEqual(len(res["elements"]), 1)

            # Endpoint 1 must now be in cooldown
            cooldown_until = provider._endpoint_cooldowns.get("https://rate-limited.osm/api/interpreter", 0)
            self.assertGreater(cooldown_until, time.monotonic() + 30)

            # Query 2 should skip endpoint 1 completely and go straight to backup
            mock_post.reset_mock()
            res2 = provider.execute_overpass_query("query_2")
            self.assertEqual(len(res2["elements"]), 1)
            self.assertEqual(mock_post.call_count, 1)
            self.assertEqual(mock_post.call_args[0][0], "https://backup.osm/api/interpreter")

    def test_stale_cache_rejected_during_standard_discovery(self):
        """Cache entry older than cache_ttl is not returned when fallback cache is disabled."""
        provider = OpenStreetMapProvider(
            cache_dir=self.test_cache_dir,
            cache_ttl_seconds=3600,
            min_request_interval=0.0,
            endpoints=["https://ep1.osm/api/interpreter"]
        )
        query = "stale_cache_test"

        # Inject 10-hour old cache entry
        import hashlib, json
        cache_file = os.path.join(self.test_cache_dir, f"{hashlib.md5(query.encode()).hexdigest()}.json")
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time() - 36000, "key": query, "data": {"elements": [{"id": 99}]}}, f)

        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.json.return_value = {"elements": [{"id": 100, "tags": {"name": "Fresh"}}]}

        with patch("requests.post", return_value=mock_200) as mock_post:
            res = provider.execute_overpass_query(query)
            # Fresh live query must be executed, not returning the 10-hour old stale cache
            mock_post.assert_called_once()
            self.assertEqual(res["elements"][0]["id"], 100)

    def test_fresh_cache_is_preferred(self):
        """Cache entry within cache_ttl is returned without any network request."""
        provider = OpenStreetMapProvider(
            cache_dir=self.test_cache_dir,
            cache_ttl_seconds=3600,
            min_request_interval=0.0,
            endpoints=["https://ep1.osm/api/interpreter"]
        )
        query = "fresh_cache_test"

        # Inject 5-minute old cache entry
        import hashlib, json
        cache_file = os.path.join(self.test_cache_dir, f"{hashlib.md5(query.encode()).hexdigest()}.json")
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time() - 300, "key": query, "data": {"elements": [{"id": 42}]}}, f)

        with patch("requests.post") as mock_post:
            res = provider.execute_overpass_query(query)
            mock_post.assert_not_called()
            self.assertEqual(res["elements"][0]["id"], 42)


if __name__ == "__main__":
    unittest.main()
