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

        # Mock OSM search_businesses to raise OverpassQueryError and web_search to return no candidates
        with patch.object(engine.osm, "search_businesses", side_effect=OverpassQueryError("All endpoints down")), \
             patch.object(engine.web_search, "discover_businesses", return_value=[]):
            with self.assertRaises(RuntimeError) as ctx:
                engine.discover_round(round_number=1, country="United Kingdom", cities=["Manchester"], industry="Restaurants")

            self.assertIn("Discovery provider outage", str(ctx.exception))
            self.assertIn("All endpoints down", str(ctx.exception))
            self.assertIn("web-search fallback returned no valid candidates", str(ctx.exception))

    def test_osm_outage_with_successful_web_search_fallback(self):
        """When OSM suffers an outage, web search fallback discovers candidates and engine succeeds without outage error."""
        engine = HybridDiscoveryEngine(mode="HYBRID", apify_enabled=False)
        engine.foursquare.api_key = ""

        fallback_candidate = DiscoveredBusiness(
            company_name="Rudy's Pizza Napoletana",
            category="Restaurants",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            country_status="COUNTRY_MATCH",
            address="9 Cotton St, Ancoats, Manchester M4 5BF",
            phone="01618343456",
            raw_website="https://www.rudyspizza.co.uk",
            discovery_source="WEB_SEARCH",
            operational_status="UNKNOWN"
        )

        with patch.object(engine.osm, "search_businesses", side_effect=OverpassQueryError("All Overpass endpoints down")), \
             patch.object(engine.web_search, "discover_businesses", return_value=[fallback_candidate]):
            results, queries = engine.discover_round(
                round_number=1,
                country="United Kingdom",
                cities=["Manchester"],
                industry="Restaurants",
                limit=10
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].company_name, "Rudy's Pizza Napoletana")
            self.assertEqual(results[0].discovery_source, "WEB_SEARCH")
            self.assertEqual(engine.transparency_stats["by_source"]["WEB_SEARCH"], 1)
            self.assertEqual(engine.transparency_stats["businesses_discovered_total"], 1)

    def test_osm_outage_fallback_returns_no_valid_candidates_raises_outage(self):
        """When OSM fails and web search fallback returns no valid candidates, explicit outage RuntimeError is preserved."""
        engine = HybridDiscoveryEngine(mode="HYBRID", apify_enabled=False)
        engine.foursquare.api_key = ""

        with patch.object(engine.osm, "search_businesses", side_effect=OverpassQueryError("All endpoints down")), \
             patch.object(engine.web_search, "discover_businesses", return_value=[]):
            with self.assertRaises(RuntimeError) as ctx:
                engine.discover_round(
                    round_number=1,
                    country="United Kingdom",
                    cities=["Manchester"],
                    industry="Restaurants"
                )

            err = str(ctx.exception)
            self.assertIn("Discovery provider outage", err)
            self.assertIn("Primary OSM discovery failed (All endpoints down)", err)
            self.assertIn("web-search fallback returned no valid candidates", err)

    def test_osm_outage_fallback_provider_failure_raises_outage(self):
        """When OSM fails and web search fallback itself fails/errors, explicit outage RuntimeError is preserved."""
        engine = HybridDiscoveryEngine(mode="HYBRID", apify_enabled=False)
        engine.foursquare.api_key = ""

        with patch.object(engine.osm, "search_businesses", side_effect=OverpassQueryError("All endpoints down")), \
             patch.object(engine.web_search, "discover_businesses", side_effect=RuntimeError("WebSearchProvider unavailable or failed: SEARCH_PROVIDER_UNAVAILABLE")):
            with self.assertRaises(RuntimeError) as ctx:
                engine.discover_round(
                    round_number=1,
                    country="United Kingdom",
                    cities=["Manchester"],
                    industry="Restaurants"
                )

            err = str(ctx.exception)
            self.assertIn("Discovery provider outage", err)
            self.assertIn("Primary OSM discovery failed (All endpoints down)", err)
            self.assertIn("web-search fallback failed", err)

    def test_fallback_candidates_pass_through_normal_qualification_never_auto_outreach_ready(self):
        """Fallback candidates must pass through normal qualification pipeline and never be marked OUTREACH_READY simply from discovery."""
        engine = HybridDiscoveryEngine(mode="HYBRID", apify_enabled=False)
        engine.foursquare.api_key = ""

        # Candidate with no website but verified social
        fallback_candidate = DiscoveredBusiness(
            company_name="Authentic Indie Bistro",
            category="Restaurants",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            country_status="COUNTRY_MATCH",
            address="12 Oldham St, Manchester M1 1JN",
            phone="01612345678",
            raw_website="",
            instagram_url="https://www.instagram.com/indiebistromcr/",
            social_status="SOCIAL_FOUND",
            discovery_source="WEB_SEARCH",
            operational_status="UNKNOWN"
        )
        self.assertNotEqual(getattr(fallback_candidate, "qualification_status", None), "OUTREACH_READY")

        pipeline = LeadGenerationPipeline(
            discovery_provider=engine,
            shadow_mode=True
        )

        mock_ver_res = {
            "verification_status": "NO_WEBSITE_CONFIRMED",
            "verification_reason": "Verified no website exists",
            "verified_website": "",
            "evidence": {}
        }
        mock_eval_res = {
            "score": 85,
            "qualification_state": "OUTREACH_READY",
            "priority": "HIGH",
            "qualification_reason": "High quality qualified lead",
            "signals": {"verified_business_social": True, "established_physical_premises": True},
            "positive_signals": ["verified_business_social", "established_physical_premises"],
            "red_flags": [],
            "score_breakdown": {},
            "evidence": {},
            "is_outreach_ready": True,
            "social_status": "SOCIAL_FOUND",
            "social_ownership_status": "VERIFIED",
            "social_profile_status": "ACCESSIBLE",
            "social_activity": "ACTIVE",
            "operational_status": "ACTIVE_CONFIRMED",
            "operational_confidence": "HIGH",
            "operational_evidence": "Active operations verified",
            "evidence_freshness": "RECENT",
            "verified_social_urls": {"instagram": "https://www.instagram.com/indiebistromcr/"}
        }

        with patch.object(engine.osm, "search_businesses", side_effect=OverpassQueryError("OSM down")), \
             patch.object(engine.web_search, "discover_businesses", return_value=[fallback_candidate]), \
             patch.object(pipeline.verifier, "verify_business", return_value=mock_ver_res) as mock_ver, \
             patch.object(pipeline.scorer, "evaluate_lead", return_value=mock_eval_res) as mock_eval:
            result = pipeline.run(
                country="United Kingdom",
                cities=["Manchester"],
                industry="Restaurants",
                requested_qualified_leads=1,
                batch_size=1
            )

            # Verification that candidate went through verifier and scorer
            self.assertTrue(mock_ver.called)
            self.assertTrue(mock_eval.called)
            # The lead is qualified only after going through pipeline evaluation
            self.assertEqual(len(result.get("leads", [])), 1)
            self.assertEqual(result["leads"][0].company_name, "Authentic Indie Bistro")
            self.assertEqual(result["leads"][0].qualification_state, "OUTREACH_READY")

    def test_convert_search_result_to_candidate_strict_validation(self):
        """convert_search_result_to_candidate rigorously rejects listicles, forums, US locations, and generic titles."""
        from lib.discovery.web_search import convert_search_result_to_candidate

        # 1. Reject listicles & roundups
        listicle_res = {
            "title": "The 10 Best Independent Restaurants in Manchester",
            "snippet": "Discover the top 10 independent dining spots in Manchester city centre.",
            "url": "https://example.com/best-restaurants-manchester"
        }
        self.assertIsNone(convert_search_result_to_candidate(listicle_res, "Manchester", "United Kingdom", "Restaurants"))

        # 2. Reject Reddit discussion threads
        reddit_res = {
            "title": "Best independent restaurant in City Centre? : r/manchester",
            "snippet": "Looking for recommendations for independent places to eat in Manchester.",
            "url": "https://www.reddit.com/r/manchester/comments/12345/best_indie_food/"
        }
        self.assertIsNone(convert_search_result_to_candidate(reddit_res, "Manchester", "United Kingdom", "Restaurants"))

        # 3. Reject US location (Manchester, NH)
        us_res = {
            "title": "The Foundry Restaurant",
            "snippet": "50 Commercial St, Manchester, NH 03101. Phone: (603) 555-1234.",
            "url": "https://foundrynh.com"
        }
        self.assertIsNone(convert_search_result_to_candidate(us_res, "Manchester", "United Kingdom", "Restaurants"))

        # 4. Reject generic industry names
        generic_res = {
            "title": "Independent Restaurant - Manchester",
            "snippet": "Local dining in Manchester, United Kingdom.",
            "url": "https://example.com/independent-restaurant"
        }
        self.assertIsNone(convert_search_result_to_candidate(generic_res, "Manchester", "United Kingdom", "Restaurants"))

        # 5. Accept genuine independent business with target location evidence
        valid_res = {
            "title": "Mackie Mayor - Manchester Food Hall",
            "snippet": "Eagle St, Northern Quarter, Manchester M4 5JY. Independent food hall and dining.",
            "url": "https://www.mackiemayor.co.uk"
        }
        cand = convert_search_result_to_candidate(valid_res, "Manchester", "United Kingdom", "Restaurants")
        self.assertIsNotNone(cand)
        self.assertEqual(cand.company_name, "Mackie Mayor")
        self.assertEqual(cand.city, "Manchester")
        self.assertEqual(cand.target_country, "United Kingdom")
        self.assertEqual(cand.country_status, "COUNTRY_MATCH")
        self.assertEqual(cand.discovery_source, "WEB_SEARCH")
        self.assertEqual(cand.raw_data.get("social_ownership_status"), "UNVERIFIED")

    def test_web_search_provider_search_businesses_preserves_raw_enrichment_behavior(self):
        """WebSearchProvider.search_businesses preserves existing behavior returning raw results for enrichment."""
        from lib.discovery.web_search import WebSearchProvider
        provider = WebSearchProvider()
        mock_results = [{"title": "Cafe Review", "snippet": "Food blogger post", "result_url": "https://example.com/review"}]
        with patch.object(provider, "search_web", return_value=mock_results) as mock_search:
            raw_res = provider.search_businesses(city="Manchester", country="United Kingdom", industry="Restaurants", limit=10)
            self.assertTrue(mock_search.called)
            self.assertIsInstance(raw_res, list)
            self.assertEqual(len(raw_res), 1)
            self.assertEqual(raw_res[0]["title"], "Cafe Review")

    def test_web_search_provider_discover_businesses_returns_validated_candidates(self):
        """discover_businesses produces DiscoveredBusiness records with UNVERIFIED social ownership and evidence sources."""
        from lib.discovery.web_search import WebSearchProvider
        provider = WebSearchProvider()
        mock_search_res = [
            {
                "title": "Bundobust Manchester - Indian Street Food",
                "snippet": "61 Piccadilly, Manchester M1 2AG. Call 0161 352 4057. Follow https://instagram.com/bundobust",
                "result_url": "https://bundobust.com/manchester"
            }
        ]
        with patch.object(provider, "search_web", return_value=mock_search_res):
            cands = provider.discover_businesses(city="Manchester", country="United Kingdom", industry="Restaurants", limit=5)
            self.assertEqual(len(cands), 1)
            cand = cands[0]
            self.assertIsInstance(cand, DiscoveredBusiness)
            self.assertEqual(cand.company_name, "Bundobust")
            self.assertEqual(cand.city, "Manchester")
            self.assertEqual(cand.instagram_url, "https://instagram.com/bundobust")
            self.assertEqual(cand.social_status, "SOCIAL_FOUND")
            self.assertEqual(cand.raw_data.get("social_ownership_status"), "UNVERIFIED")
            self.assertEqual(cand.evidence_sources.get("instagram"), "WEB_SEARCH")

    def test_round_2_enrichment_uses_search_businesses_path(self):
        """In round >= 2 when OSM succeeds, discover_round calls search_businesses for enrichment and tracks stats."""
        engine = HybridDiscoveryEngine(mode="HYBRID", apify_enabled=False)
        engine.foursquare.api_key = ""

        osm_candidate = DiscoveredBusiness(
            company_name="Northern Soul Grilled Cheese",
            category="Restaurants",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            country_status="COUNTRY_MATCH",
            address="Unit 10, Church St, Manchester M4 1PN",
            raw_website="https://northernsoulmcr.com"
        )

        mock_enrichment_results = [
            {
                "title": "Northern Soul Grilled Cheese Manchester",
                "snippet": "Northern Soul Grilled Cheese Manchester best grilled cheese",
                "url": "https://instagram.com/northernsoulmcr",
                "result_url": "https://instagram.com/northernsoulmcr"
            }
        ]

        with patch.object(engine.osm, "search_businesses", return_value=[osm_candidate]), \
             patch.object(engine.web_search, "search_businesses", return_value=mock_enrichment_results) as mock_search_biz, \
             patch.object(engine.web_search, "discover_businesses") as mock_disc_biz:
            results, queries = engine.discover_round(
                round_number=2,
                country="United Kingdom",
                cities=["Manchester"],
                industry="Restaurants",
                limit=10
            )

            # discover_businesses (the outage fallback) is NOT called when OSM succeeded
            self.assertFalse(mock_disc_biz.called)
            # search_businesses (enrichment) IS called
            self.assertTrue(mock_search_biz.called)
            self.assertEqual(engine.transparency_stats["by_source"]["WEB_SEARCH"], 1)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].instagram_url, "https://instagram.com/northernsoulmcr")
            self.assertEqual(results[0].raw_data.get("social_ownership_status"), "UNVERIFIED")

    def test_creator_evidence_enrichment_social_ownership_verification(self):
        """enrich_candidate_creator_evidence searches social references and sets VERIFIED or UNVERIFIED."""
        engine = HybridDiscoveryEngine(mode="HYBRID", apify_enabled=False)
        candidate = DiscoveredBusiness(
            company_name="Rudy's Pizza Napoletana",
            category="Restaurants",
            city="Manchester",
            target_country="United Kingdom",
            address="9 Cotton St, Ancoats, Manchester M4 5BF",
            social_status="SOCIAL_UNKNOWN"
        )

        mock_social_refs = [
            {
                "title": "Best pizza at Rudy's Pizza Napoletana (@rudyspizzamcr) in Manchester",
                "snippet": "Amazing sourdough pizza at @rudyspizzamcr on Cotton St Ancoats Manchester",
                "result_url": "https://instagram.com/p/12345"
            }
        ]

        with patch.object(engine.web_search, "search_social_references", return_value=mock_social_refs):
            summary = engine.enrich_candidate_creator_evidence(candidate, city="Manchester", country="United Kingdom")
            self.assertIsNotNone(summary)
            self.assertIn("creator_evidence", candidate.raw_data)
            self.assertIn(candidate.raw_data.get("social_ownership_status"), ["VERIFIED", "UNVERIFIED"])

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
             patch("lib.discovery.osm.OpenStreetMapProvider.execute_overpass_query", side_effect=OverpassQueryError("All Overpass endpoints timed out")), \
             patch("lib.discovery.web_search.WebSearchProvider.discover_businesses", return_value=[]):
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

    def test_checkpoint_callback_captures_current_pipeline_stats(self):
        """Checkpoint callback must include the live stats dict after it has been initialized."""
        candidate = DiscoveredBusiness(
            company_name="Country Mismatch Fixture",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United States",
            address="Example address in the United States",
            country_status="COUNTRY_UNCLEAR",
            discovery_source="FIXTURE",
        )
        pipeline = LeadGenerationPipeline(
            discovery_provider=MagicMock(),
            shadow_mode=True,
        )
        events = []

        with patch.object(
            pipeline.country_validator,
            "validate",
            return_value=("COUNTRY_MISMATCH", "United States", "", "", "fixture mismatch"),
        ):
            pipeline.run(
                country="United Kingdom",
                cities=["Manchester"],
                industry="Restaurants",
                requested_qualified_leads=0,
                inject_test_candidates=[candidate],
                log_callback=lambda msg, data=None: events.append((msg, data or {})),
            )

        checkpoint_events = [
            payload for msg, payload in events if "[Checkpoint Updated]" in msg
        ]
        self.assertTrue(checkpoint_events, "Expected a checkpoint callback for the processed fixture")
        self.assertIn("stats", checkpoint_events[-1])
        self.assertEqual(checkpoint_events[-1]["stats"]["country_mismatches"], 1)
        self.assertEqual(checkpoint_events[-1]["stats"]["wrong_country_results"], 1)


if __name__ == "__main__":
    unittest.main()
