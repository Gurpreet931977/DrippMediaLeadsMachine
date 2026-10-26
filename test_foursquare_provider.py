"""
test_foursquare_provider.py
============================
Unit tests for Phase 2C Foursquare Places API adapter:
- Mocked response fixtures for status and search results.
- Unconfigured status when key is absent.
- Differentiated error handling (AUTH_ERROR, RATE_LIMITED, DEGRADED).
- Response parsing and rating normalization (10-point scale -> 5.0 scale).
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from lib.discovery.foursquare import FoursquareProvider
from lib.types import DiscoveredBusiness, SocialStatus


class TestFoursquareProvider(unittest.TestCase):

    def setUp(self):
        self.temp_cache = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_cache, ignore_errors=True)

    def test_unconfigured_status_when_key_is_missing(self):
        """When FOURSQUARE_API_KEY is not configured, health check reports NOT_CONFIGURED."""
        provider = FoursquareProvider(api_key="", cache_dir=self.temp_cache)
        health = provider.health_check()
        self.assertEqual(health["status"], "NOT_CONFIGURED")
        self.assertFalse(health["connected"])
        self.assertIn("not configured", health["reason"].lower())

        # search_businesses gracefully returns empty list
        results = provider.search_businesses("Manchester", "United Kingdom", "Restaurants")
        self.assertEqual(results, [])

    def test_auth_error_status(self):
        """HTTP 401/403 from Foursquare reports AUTH_ERROR."""
        provider = FoursquareProvider(api_key="invalid_test_key", cache_dir=self.temp_cache)
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        with patch("requests.get", return_value=mock_resp):
            health = provider.health_check()
            self.assertEqual(health["status"], "AUTH_ERROR")
            self.assertFalse(health["connected"])

    def test_rate_limited_status(self):
        """HTTP 429 reports RATE_LIMITED."""
        provider = FoursquareProvider(api_key="valid_key", cache_dir=self.temp_cache)
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "Quota exceeded"

        with patch("requests.get", return_value=mock_resp):
            health = provider.health_check()
            self.assertEqual(health["status"], "RATE_LIMITED")
            self.assertFalse(health["connected"])

    def test_connected_status(self):
        """HTTP 200 reports CONNECTED."""
        provider = FoursquareProvider(api_key="valid_key", cache_dir=self.temp_cache)
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("requests.get", return_value=mock_resp):
            health = provider.health_check()
            self.assertEqual(health["status"], "CONNECTED")
            self.assertTrue(health["connected"])

    def test_successful_search_and_rating_normalization(self):
        """Mocked successful Foursquare response correctly extracts candidate fields and normalizes rating."""
        provider = FoursquareProvider(api_key="valid_key", cache_dir=self.temp_cache)

        mock_payload = {
            "results": [
                {
                    "fsq_id": "4b0587ecf964a52093a822e3",
                    "name": "Hawksmoor Manchester",
                    "location": {
                        "address": "184-186 Deansgate",
                        "formatted_address": "184-186 Deansgate, Manchester, M3 3WB",
                        "locality": "Manchester",
                        "postcode": "M3 3WB",
                        "region": "Greater Manchester",
                        "country": "GB"
                    },
                    "tel": "+441618375877",
                    "website": "https://thehawksmoor.com/locations/manchester/",
                    "rating": 9.2,
                    "stats": {
                        "ratings_total": 450
                    },
                    "social_media": {
                        "instagram": "hawksmoorrestaurants",
                        "facebook_id": "HawksmoorRestaurants"
                    },
                    "geocodes": {
                        "main": {
                            "latitude": 53.4800,
                            "longitude": -2.2475
                        }
                    },
                    "categories": [
                        {"id": 13065, "name": "Steakhouse"}
                    ]
                }
            ]
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_payload

        with patch("requests.get", return_value=mock_resp):
            candidates = provider.search_businesses("Manchester", "United Kingdom", "Restaurants", limit=10)

        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c.company_name, "Hawksmoor Manchester")
        self.assertEqual(c.city, "Manchester")
        self.assertEqual(c.postcode, "M3 3WB")
        self.assertEqual(c.address, "184-186 Deansgate, Manchester, M3 3WB")
        self.assertEqual(c.phone, "+441618375877")
        self.assertEqual(c.raw_website, "https://thehawksmoor.com/locations/manchester/")
        # Rating 9.2 on FSQ 10-point scale must normalize to 4.6 on 5.0 scale
        self.assertEqual(c.rating, 4.6)
        self.assertEqual(c.review_count, 450)
        self.assertEqual(c.instagram_url, "https://instagram.com/hawksmoorrestaurants")
        self.assertEqual(c.facebook_url, "https://facebook.com/HawksmoorRestaurants")
        self.assertEqual(c.discovery_source, "FOURSQUARE")


if __name__ == "__main__":
    unittest.main()
