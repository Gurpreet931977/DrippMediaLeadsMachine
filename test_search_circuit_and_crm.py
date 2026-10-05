#!/usr/bin/env python3
"""
Unit Test Suite for Search Circuit Recovery and Global CRM Deduplication
========================================================================
Covers all Part 8 requirements:
  Search Circuit:
    1. Circuit state transitions (CLOSED -> OPEN -> HALF_OPEN -> CLOSED)
    2. Provider failure isolation (failure on one provider does not affect others)
    3. Provider recovery probe failure and successful probe recovery
    4. Real search result parsing
    5. Review evidence identity matching
    6. Wrong branch isolation (rejecting different major cities)
    7. Conflicting review sources detection
    8. Missing review data handling

  CRM Deduplication:
    9. Exact duplicate detection
    10. Normalized-name duplicate detection
    11. Address variation matching
    12. Postcode variation matching
    13. Phone variation matching (terminal digits)
    14. Same business with formatting differences
    15. Different branches / locations (NEW_BUSINESS)
    16. Possible duplicate (routes to review queue)
    17. Conflict detection (conflicting official domains / same phone different name)
    18. Duplicate across separate pipeline runs
    19. Duplicate Google Sheet row prevention (in-place update, preserving lead_id and outreach history)
    20. Duplicate outreach prevention (idempotency key and _already_sent check)
"""

import json
import time
import unittest
from unittest.mock import MagicMock, patch

from lib.types import Lead, QualificationState, OutreachStatus
from lib.discovery.web_search import CircuitState, ProviderCircuitBreaker, WebSearchProvider
from lib.enrichment.review_source_provider import GlobalReviewSourceProvider
from lib.enrichment.review_rating_enricher import (
    ReviewRatingEnricher,
    ReviewEvidenceItem,
    ReviewConfidence,
    ReviewStatus,
)
from lib.crm.identity_matcher import (
    BusinessIdentityMatcher,
    IdentityMatchOutcome,
    IdentityMatchResult,
)
from lib.sheets.google_sheets import GoogleSheetsStorageProvider, LEADS_COLUMNS
from lib.outreach.campaign_executor import _already_sent, final_pre_send_check


class TestSearchCircuitRecovery(unittest.TestCase):
    """Verifies 3-state circuit breaker, automatic recovery, and provider isolation."""

    def test_01_circuit_state_transitions_closed_open_half_open_closed(self):
        cb = ProviderCircuitBreaker("TEST_PROVIDER", failure_threshold=2, cooldown_seconds=0.1)
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertEqual(cb.status, "CLOSED")
        self.assertTrue(cb.can_request())

        # 1st failure — under threshold
        cb.record_failure("HTTP 500", latency=0.05)
        self.assertEqual(cb.failure_count, 1)
        self.assertEqual(cb.status, "CLOSED")
        self.assertTrue(cb.can_request())

        # 2nd failure — reaches threshold, trips to OPEN
        cb.record_failure("HTTP 503", latency=0.05)
        self.assertEqual(cb.failure_count, 2)
        self.assertEqual(cb.status, "OPEN")
        self.assertFalse(cb.can_request())
        self.assertIsNotNone(cb.opened_at)
        self.assertIsNotNone(cb.cooldown_until)

        # Wait for cooldown to expire
        time.sleep(0.12)

        # Should transition to HALF_OPEN automatically upon query
        self.assertEqual(cb.status, "HALF_OPEN")
        # In HALF_OPEN, allows exactly one probe request
        self.assertTrue(cb.can_request())
        # While probe is in progress, blocks additional requests
        self.assertFalse(cb.can_request())

        # Probe succeeds -> transitions back to CLOSED automatically
        cb.record_success(results_count=5, latency=0.04)
        self.assertEqual(cb.status, "CLOSED")
        self.assertEqual(cb.failure_count, 0)
        self.assertEqual(cb.probe_result, "SUCCESS")
        self.assertIsNone(cb.opened_at)
        self.assertIsNone(cb.cooldown_until)
        # Normal requests resume without restarting
        self.assertTrue(cb.can_request())

    def test_02_provider_failure_isolation(self):
        """Failure on one search engine must NOT trip or block other search engines."""
        web = WebSearchProvider(cooldown_seconds=10.0, failure_threshold=2)

        # Trip DUCKDUCKGO_FALLBACK
        ddg = web.circuit_breakers["DUCKDUCKGO_FALLBACK"]
        ddg.record_failure("Connection timed out")
        ddg.record_failure("Connection timed out")
        self.assertEqual(ddg.status, "OPEN")
        self.assertFalse(ddg.can_request())

        # Other providers remain unaffected
        self.assertEqual(web.circuit_breakers["TAVILY"].status, "CLOSED")
        self.assertTrue(web.circuit_breakers["TAVILY"].can_request())
        self.assertEqual(web.circuit_breakers["BRAVE"].status, "CLOSED")
        self.assertTrue(web.circuit_breakers["BRAVE"].can_request())
        self.assertEqual(web.circuit_breakers["SEARXNG"].status, "CLOSED")
        self.assertTrue(web.circuit_breakers["SEARXNG"].can_request())

        # Telemetry accurately reports isolated states
        telemetry = web.get_telemetry()
        self.assertEqual(telemetry["DUCKDUCKGO_FALLBACK"]["status"], "OPEN")
        self.assertEqual(telemetry["TAVILY"]["failure_count"], 0)

    def test_03_provider_recovery_probe_failure_then_success(self):
        """In HALF_OPEN, a failed probe resets back to OPEN; a subsequent probe succeeds to CLOSED."""
        cb = ProviderCircuitBreaker("SEARXNG", failure_threshold=2, cooldown_seconds=0.1)
        cb.trip(cooldown_seconds=0.1)
        self.assertEqual(cb.status, "OPEN")

        time.sleep(0.12)
        self.assertEqual(cb.status, "HALF_OPEN")
        self.assertTrue(cb.can_request())

        # Probe fails
        cb.record_failure("Still down")
        self.assertEqual(cb.status, "OPEN")
        self.assertEqual(cb.probe_result, "FAILURE")
        self.assertFalse(cb.can_request())

        # Wait for second cooldown
        time.sleep(0.12)
        self.assertEqual(cb.status, "HALF_OPEN")
        self.assertTrue(cb.can_request())

        # Second probe succeeds
        cb.record_success(results_count=3)
        self.assertEqual(cb.status, "CLOSED")
        self.assertEqual(cb.probe_result, "SUCCESS")
        self.assertTrue(cb.can_request())

    def test_04_real_search_result_parsing(self):
        """Verifies review count, rating, and platform extraction from realistic snippets."""
        prov = GlobalReviewSourceProvider()

        snippet = "Bundobust Leeds: 4.8 out of 5 stars based on 1,240 customer reviews on Tripadvisor. Award-winning Indian street food & craft beer."
        cnt = prov.extract_review_count(snippet)
        rat = prov.extract_rating(snippet)
        self.assertEqual(cnt, 1240)
        self.assertEqual(rat, 4.8)

        # Bullet separated format
        snippet2 = "Tokyo Ramen Hub · 4.6 ★ · 185 reviews · Authentic ramen bar in Leeds."
        cnt2 = prov.extract_review_count(snippet2)
        rat2 = prov.extract_rating(snippet2)
        self.assertEqual(cnt2, 185)
        self.assertEqual(rat2, 4.6)

    def test_05_review_evidence_identity_matching_and_listicle_rejection(self):
        """Valid venue matches pass; generic listicles and roundups are rejected."""
        prov = GlobalReviewSourceProvider()

        # Valid snippet
        valid, reason = prov.validate_business_identity(
            snippet="Welcome to Tharavadu Kerala Restaurant in Leeds. 640 reviews.",
            business_name="Tharavadu",
            city="Leeds",
            country="United Kingdom"
        )
        self.assertTrue(valid)
        self.assertIsNone(reason)

        # Generic listicle snippet
        valid_list, reason_list = prov.validate_business_identity(
            snippet="The 10 Best Restaurants in Leeds: Where to eat tonight with 5,000 reviews across venues.",
            business_name="Tharavadu",
            city="Leeds",
            country="United Kingdom"
        )
        self.assertFalse(valid_list)
        self.assertEqual(reason_list, "GENERIC_LISTICLE_OR_ROUNDUP_PAGE")

    def test_06_wrong_branch_isolation(self):
        """Snippets from the same brand in another major city are rejected."""
        prov = GlobalReviewSourceProvider()

        # Business is located in Leeds, but snippet is for Manchester branch
        valid, reason = prov.validate_business_identity(
            snippet="Bundobust Piccadilly Manchester — 4.7 stars with 900 reviews on Piccadilly Gardens.",
            business_name="Bundobust",
            city="Leeds",
            country="United Kingdom",
            street="Mill Hill",
            postcode="LS1 5DQ"
        )
        self.assertFalse(valid)
        self.assertIn("WRONG_BRANCH_LOCATION", reason)

    def test_07_conflicting_review_sources_detection(self):
        """Divergent review counts trigger conflict status and require manual review."""
        enricher = ReviewRatingEnricher()

        item_a = ReviewEvidenceItem(
            review_count=15,
            rating=2.5,
            source="Web",
            source_url="https://example.com/a",
            evidence_text="15 reviews",
            confidence=ReviewConfidence.HIGH.value
        )
        item_b = ReviewEvidenceItem(
            review_count=450,
            rating=4.9,
            source="Tripadvisor",
            source_url="https://tripadvisor.co.uk/b",
            evidence_text="450 reviews",
            confidence=ReviewConfidence.HIGH.value
        )

        enrich_res = ReviewRatingEnricher.resolve_best_evidence([item_a, item_b])
        self.assertEqual(enrich_res.review_confidence, ReviewConfidence.CONFLICT.value)
        self.assertEqual(enrich_res.review_status, ReviewStatus.CONFLICT_REQUIRES_REVIEW.value)

    def test_08_missing_review_data_returns_unknown(self):
        """Snippets without customer review metrics return None and UNKNOWN confidence."""
        prov = GlobalReviewSourceProvider()

        snippet = "Visit our official website for contact details, menus, and reservations."
        cnt = prov.extract_review_count(snippet)
        rat = prov.extract_rating(snippet)
        self.assertIsNone(cnt)
        self.assertIsNone(rat)

        enrich_res = ReviewRatingEnricher.resolve_best_evidence([])
        self.assertEqual(enrich_res.review_confidence, ReviewConfidence.UNKNOWN.value)
        self.assertEqual(enrich_res.review_status, ReviewStatus.NOT_FOUND.value)
        self.assertIsNone(enrich_res.review_count)
        self.assertIsNone(enrich_res.rating)


class TestGlobalCRMDeduplication(unittest.TestCase):
    """Verifies global multi-attribute entity matching, branch isolation, and duplicate prevention."""

    def setUp(self):
        self.matcher = BusinessIdentityMatcher()

    def test_09_exact_duplicate(self):
        cand = {
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "275 Upper Brook Street",
            "postcode": "M13 0HR"
        }
        existing = [{
            "lead_id": "LEAD-MAN-001",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "275 Upper Brook Street",
            "postcode": "M13 0HR"
        }]

        res = self.matcher.match_candidate(cand, existing)
        self.assertEqual(res.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertGreaterEqual(res.confidence, 0.85)
        self.assertEqual(res.matched_lead_id, "LEAD-MAN-001")

    def test_10_normalized_name_duplicate(self):
        cand = {
            "company_name": "Seoul Kimchi Restaurant Ltd",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "postcode": "M13 0HR"
        }
        existing = [{
            "lead_id": "LEAD-MAN-002",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "postcode": "M13 0HR"
        }]

        res = self.matcher.match_candidate(cand, existing)
        self.assertEqual(res.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)

    def test_11_address_variation(self):
        cand = {
            "company_name": "Raffertys Cafe",
            "city": "Otley",
            "country": "United Kingdom",
            "address": "13 Petergate, Suite 1",
            "postcode": "LS21 3HN"
        }
        existing = [{
            "lead_id": "LEAD-OTL-001",
            "company_name": "Raffertys Cafe",
            "city": "Otley",
            "country": "United Kingdom",
            "street": "13 Petergate",
            "postcode": "LS21 3HN"
        }]

        res = self.matcher.match_candidate(cand, existing)
        self.assertEqual(res.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertIn("STREET_NUMBER_MATCH", res.match_reasons)

    def test_12_postcode_variation(self):
        cand = {
            "company_name": "Against The Grain",
            "city": "Yeadon",
            "country": "United Kingdom",
            "postcode": "LS197TA"  # Unspaced
        }
        existing = [{
            "lead_id": "LEAD-YEA-001",
            "company_name": "Against The Grain Bar",
            "city": "Yeadon",
            "country": "United Kingdom",
            "postcode": "LS19 7TA"  # Spaced
        }]

        res = self.matcher.match_candidate(cand, existing)
        self.assertEqual(res.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)

    def test_13_phone_variation(self):
        """Cross-format telephone matching using terminal digits."""
        cand = {
            "company_name": "Bundobust",
            "city": "Leeds",
            "country": "United Kingdom",
            "phone": "+44 113 243 1248"
        }
        existing = [{
            "lead_id": "LEAD-LDS-001",
            "company_name": "Bundobust Leeds",
            "city": "Leeds",
            "country": "United Kingdom",
            "phone": "0113 243 1248"
        }]

        res = self.matcher.match_candidate(cand, existing)
        self.assertEqual(res.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertTrue(any("PHONE_MATCH" in r for r in res.match_reasons))

    def test_14_same_business_different_formatting(self):
        cand = {
            "company_name": "THE FLYING PIZZA & CO.",
            "city": "Leeds",
            "country": "United Kingdom",
            "postcode": "LS8 2AJ"
        }
        existing = [{
            "lead_id": "LEAD-LDS-002",
            "company_name": "Flying Pizza",
            "city": "Leeds",
            "country": "United Kingdom",
            "postcode": "LS8 2AJ"
        }]

        res = self.matcher.match_candidate(cand, existing)
        self.assertEqual(res.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)

    def test_15_different_branches_separate_leads(self):
        """Different city branches or distinct street address branches in same city are NEW_BUSINESS."""
        # Case A: Same brand name in different cities
        cand_leeds = {
            "company_name": "Gaucho",
            "city": "Leeds",
            "country": "United Kingdom",
            "street": "Russell Street"
        }
        existing_manchester = [{
            "lead_id": "LEAD-MAN-099",
            "company_name": "Gaucho",
            "city": "Manchester",
            "country": "United Kingdom",
            "street": "St Mary's Street"
        }]
        res_city = self.matcher.match_candidate(cand_leeds, existing_manchester)
        self.assertEqual(res_city.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)

        # Case B: Distinct branches in same city (different street numbers and street tokens)
        cand_subway_a = {
            "company_name": "Subway",
            "city": "Leeds",
            "country": "United Kingdom",
            "address": "12 Briggate",
            "postcode": "LS1 6ER"
        }
        existing_subway_b = [{
            "lead_id": "LEAD-LDS-SUB-1",
            "company_name": "Subway",
            "city": "Leeds",
            "country": "United Kingdom",
            "address": "450 Kirkstall Road",
            "postcode": "LS4 2QD"
        }]
        res_branch = self.matcher.match_candidate(cand_subway_a, existing_subway_b)
        self.assertEqual(res_branch.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)

    def test_16_possible_duplicate(self):
        """Partial name similarity without address or phone falls into review queue threshold."""
        cand = {
            "company_name": "Kimchi Korean Kitchen",
            "city": "Manchester",
            "country": "United Kingdom"
        }
        existing = [{
            "lead_id": "LEAD-MAN-KIM",
            "company_name": "Seoul Kimchi Restaurant",
            "city": "Manchester",
            "country": "United Kingdom"
        }]

        res = self.matcher.match_candidate(cand, existing)
        self.assertEqual(res.outcome, IdentityMatchOutcome.POSSIBLE_DUPLICATE.value)
        self.assertGreaterEqual(res.confidence, 0.50)
        self.assertLess(res.confidence, 0.85)

    def test_17_conflict_detection(self):
        """Conflicting verified official domains or same phone under conflicting business name."""
        # Distinct official website domains
        cand_dom = {
            "company_name": "Korean BBQ House",
            "city": "Manchester",
            "website": "https://koreanbbqhouse-manchester.co.uk"
        }
        existing_dom = [{
            "lead_id": "LEAD-DOM-001",
            "company_name": "Korean BBQ House",
            "city": "Manchester",
            "website": "https://manchester-korean-kitchen.com"
        }]
        res_dom = self.matcher.match_candidate(cand_dom, existing_dom)
        self.assertEqual(res_dom.outcome, IdentityMatchOutcome.CONFLICT.value)

        # Same phone registered to completely distinct business
        cand_phone = {
            "company_name": "Dr Smith Dental Clinic",
            "city": "Manchester",
            "phone": "0161 224 0000"
        }
        existing_phone = [{
            "lead_id": "LEAD-PHO-001",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "phone": "0161 224 0000"
        }]
        res_phone = self.matcher.match_candidate(cand_phone, existing_phone)
        self.assertEqual(res_phone.outcome, IdentityMatchOutcome.CONFLICT.value)

    def test_18_duplicate_across_separate_pipeline_runs(self):
        """Pipeline Run 2 re-discovering Run 1 lead must be classified as EXISTING_BUSINESS."""
        run1_crm_database = [{
            "lead_id": "LEAD-2026-001",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "275 Upper Brook Street",
            "postcode": "M13 0HR",
            "outreach_status": "SENT"
        }]

        run2_candidate = {
            "company_name": "Seoul Kimchi Ltd",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "street": "275 Upper Brook St",
            "postcode": "M13 0HR"
        }

        match = self.matcher.match_candidate(run2_candidate, run1_crm_database)
        self.assertEqual(match.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertEqual(match.matched_lead_id, "LEAD-2026-001")

    def test_19_duplicate_google_sheet_row_prevention(self):
        """save_qualified_leads updates existing row in-place, preserving lead_id and history."""
        mock_tracker = GoogleSheetsStorageProvider(sheet_url="https://docs.google.com/spreadsheets/d/dummy")
        mock_worksheet = MagicMock()
        mock_tracker._leads_ws = mock_worksheet
        mock_tracker._review_ws = MagicMock()
        mock_tracker.ensure_leads_columns = MagicMock()

        # Existing record in Google Sheet
        existing_row = {
            "lead_id": "LEAD-ORIG-777",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "275 Upper Brook Street",
            "postcode": "M13 0HR",
            "campaign_id": "CAMP-SUMMER-2026",
            "outreach_status": "SENT",
            "outreach_channel": "Instagram Direct Message",
            "review_count": "150",
            "rating": "4.6",
            "qualification_state": "OUTREACH_READY"
        }
        mock_worksheet.get_all_records.return_value = [existing_row]

        # Candidate discovered in new run with newly enriched review count
        new_candidate = Lead(
            lead_id="LEAD-NEW-TEMPORARY",
            company_name="Seoul Kimchi Ltd",
            industry="Restaurants",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            address="275 Upper Brook St",
            postcode="M13 0HR",
            review_count=185,
            rating=4.7,
            qualification_state="OUTREACH_READY"
        )

        inserted, updated = mock_tracker.save_qualified_leads([new_candidate])

        # Verifications
        self.assertEqual(inserted, 0, "No duplicate row must be inserted")
        self.assertEqual(updated, 1, "Existing row must be updated in-place")
        self.assertEqual(mock_tracker.stats["duplicates_prevented"], 1)
        self.assertEqual(new_candidate.lead_id, "LEAD-ORIG-777", "Original lead_id must be preserved")
        self.assertEqual(new_candidate.outreach_status, "SENT", "Outreach history must be preserved")
        self.assertEqual(new_candidate.campaign_id, "CAMP-SUMMER-2026", "Campaign ID must be preserved")
        # Ensure append_rows was NOT called
        mock_worksheet.append_rows.assert_not_called()
        # Ensure update was called for the existing row
        mock_worksheet.update.assert_called_once()

    def test_20_duplicate_outreach_prevention(self):
        """Business identity uniqueness key and _already_sent prevent duplicate outreach."""
        # Uniqueness key format: business_identity_id + campaign + channel
        key = BusinessIdentityMatcher.get_outreach_uniqueness_key("biz:seoulkimchi@manchester@gb", "CAMP-100", "Instagram Direct Message")
        self.assertEqual(key, "biz:seoulkimchi@manchester@gb:camp-100:instagram direct message")

        # Mock queue with a previously sent outreach
        queue = [{
            "lead_id": "LEAD-001",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "channel": "Instagram Direct Message",
            "status": OutreachStatus.SENT.value
        }]

        # Check with identical lead_id
        self.assertTrue(_already_sent(queue, "LEAD-001", "Instagram Direct Message"))

        # Check with slightly formatted company name and different lead_id
        is_sent = _already_sent(
            queue,
            lead_id="LEAD-DIFFERENT-ID",
            channel="Instagram Direct Message",
            company_name="Seoul Kimchi Restaurant Ltd",
            city="Manchester",
            country="United Kingdom"
        )
        self.assertTrue(is_sent, "Must detect prior sent outreach via business identity matching")

        # Gate 9 in final_pre_send_check must block the duplicate item
        new_item = {
            "lead_id": "LEAD-DIFFERENT-ID",
            "company_name": "Seoul Kimchi Restaurant Ltd",
            "city": "Manchester",
            "country": "United Kingdom",
            "channel": "Email",
            "recipient": "hello@seoulkimchi.co.uk",
            "status": OutreachStatus.QUEUED.value,
            "qualification_state": QualificationState.OUTREACH_READY.value,
            "message": {"message_body": "Hello Seoul Kimchi!"}
        }
        email_queue = [{
            "lead_id": "LEAD-001",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "channel": "Email",
            "recipient": "contact@seoulkimchi.co.uk",
            "status": OutreachStatus.SENT.value
        }]

        with patch("lib.outreach.campaign_executor.ChannelConfigManager.get_channel_status", return_value={"configured": True, "message": "OK"}):
            passed, block_reason = final_pre_send_check(
                item=new_item,
                queue=email_queue,
                campaign={"max_campaign_limit": 100, "daily_limit": 10},
                campaign_sent_so_far=1,
                run_counts={},
                max_per_run=10
            )
        self.assertFalse(passed)
        self.assertIn("LEAD_ALREADY_SENT", block_reason)


class TestReviewExtractionAndCRMAudit(unittest.TestCase):
    """
    Covers all 14 Part 10 verification scenarios:
      1. review parser success (Records 2, 4, 12 from real search results)
      2. review parser miss recovery (apostrophes, concatenated text)
      3. review business identity mismatch (unrelated businesses in snippet)
      4. wrong branch rejection (different major city branch)
      5. rating extraction (decimals with dot/comma, 'out of 5', stars)
      6. review count extraction (thousands separators: comma, dot, space, 'based on', etc.)
      7. circuit successful recovery (CLOSED -> OPEN -> HALF_OPEN -> CLOSED)
      8. circuit failed recovery (HALF_OPEN -> OPEN)
      9. Seoul Kimchi duplicate 4-run regression (Run 1 insert, Run 2 re-discover, Run 3 variations, Run 4 new branch)
      10. normalized duplicate
      11. different branch separation
      12. possible-duplicate false positive audit
      13. Google Sheets duplicate prevention
      14. duplicate outreach safety
    """

    def setUp(self):
        self.prov = GlobalReviewSourceProvider()
        self.matcher = BusinessIdentityMatcher()

    def test_21_review_parser_success(self):
        """Verifies review count and rating extraction from real search results (Records 2, 4, 12)."""
        # Record 2: Shandar (RestaurantGuru)
        shandar_snippet = "ShandarinBradfordrated 3.5 out of 5 onRestaurantGuru: 146 reviews by visitors, 23 photos. Explore menu, check opening hours and order delivery"
        self.assertEqual(self.prov.extract_review_count(shandar_snippet), 146)
        self.assertEqual(self.prov.extract_rating(shandar_snippet), 3.5)

        # Record 4: Shezzaan's (RestaurantGuru)
        shezzaans_snippet = "SHEZZAAN'SPUDSEYinPudseyrated 4.1 out of 5 onRestaurantGuru: 577 reviews by visitors, 61 photos. Explore menu, check opening hours and order delivery"
        self.assertEqual(self.prov.extract_review_count(shezzaans_snippet), 577)
        self.assertEqual(self.prov.extract_rating(shezzaans_snippet), 4.1)

        # Record 12: Sandys Sandwich Shop (EatEasy)
        sandys_snippet = "AboutSandys Sandwich ShopSandys Sandwich ShopinPudseyhas quietly become the breakfast destination locals trust for a proper start to the day, earning a stellar 4.7 out of 5 from over 127 reviews on . It's the kind of place that knows its audience"
        self.assertEqual(self.prov.extract_review_count(sandys_snippet), 127)
        self.assertEqual(self.prov.extract_rating(sandys_snippet), 4.7)

    def test_22_review_parser_miss_recovery(self):
        """Apostrophe handling and concatenated text match successfully without dropping identity."""
        shezzaans_snippet = "SHEZZAAN'SPUDSEYinPudseyrated 4.1 out of 5 onRestaurantGuru: 577 reviews by visitors, 61 photos."
        valid, reason = self.prov.validate_business_identity(
            snippet=shezzaans_snippet,
            business_name="Shezzaan's",
            city="Pudsey",
            country="United Kingdom",
            street="Lowtown",
            postcode="LS28 7AB"
        )
        self.assertTrue(valid, "Apostrophe in Shezzaan's must match SHEZZAAN'SPUDSEY in snippet")
        self.assertIsNone(reason)

    def test_23_review_business_identity_mismatch(self):
        """Unrelated business in snippet is rejected with MISSING_BUSINESS_IDENTITY."""
        unrelated_snippet = "Bonehead Birmingham Review! #foodreview #food #foodie #chicken #chickensandwich"
        valid, reason = self.prov.validate_business_identity(
            snippet=unrelated_snippet,
            business_name="Rodie Pizza",
            city="Pudsey",
            country="United Kingdom",
            street="Robin Lane",
            postcode="LS28 5HL"
        )
        self.assertFalse(valid)
        self.assertEqual(reason, "MISSING_BUSINESS_IDENTITY")

    def test_24_wrong_branch(self):
        """Snippet referring to the same brand in another major city is rejected."""
        manchester_snippet = "Dishoom Manchester, 32 Bridge Street, Manchester M3 3BT. Rated 4.7 out of 5 from 3,200 reviews."
        valid, reason = self.prov.validate_business_identity(
            snippet=manchester_snippet,
            business_name="Dishoom",
            city="Leeds",
            country="United Kingdom",
            street="Briggate",
            postcode="LS1 6HD"
        )
        self.assertFalse(valid)
        self.assertIn("WRONG_BRANCH_LOCATION", reason)

    def test_25_rating_extraction_variations(self):
        """Supports decimals with dot, comma, 'out of 5', stars, and slash format."""
        self.assertEqual(self.prov.extract_rating("rated 4.5 out of 5"), 4.5)
        self.assertEqual(self.prov.extract_rating("4.8/5 based on 200 reviews"), 4.8)
        self.assertEqual(self.prov.extract_rating("4.6 stars on Google"), 4.6)
        self.assertEqual(self.prov.extract_rating("rated 4,5 out of 5"), 4.5)
        self.assertEqual(self.prov.extract_rating("average rating 4.1 out of 5 stars"), 4.1)

    def test_26_review_count_extraction_variations(self):
        """Supports thousands separators (comma, dot, space), 'based on', and 'Google reviews'."""
        self.assertEqual(self.prov.extract_review_count("1,010 reviews"), 1010)
        self.assertEqual(self.prov.extract_review_count("250 reviews"), 250)
        self.assertEqual(self.prov.extract_review_count("based on 900 reviews"), 900)
        self.assertEqual(self.prov.extract_review_count("1.010 reviews"), 1010)
        self.assertEqual(self.prov.extract_review_count("1 010 reviews"), 1010)
        self.assertEqual(self.prov.extract_review_count("4.5 stars (150 Google reviews)"), 150)

    def test_27_circuit_successful_recovery(self):
        """Verifies real transition CLOSED -> OPEN -> HALF_OPEN -> probe succeeds -> CLOSED."""
        cb = ProviderCircuitBreaker("TEST_RECOVERY", failure_threshold=2, cooldown_seconds=0.05)
        cb.record_failure("error 1")
        cb.record_failure("error 2")
        self.assertEqual(cb.status, "OPEN")

        time.sleep(0.06)
        self.assertEqual(cb.status, "HALF_OPEN")
        self.assertTrue(cb.can_request())  # probe initiated

        cb.record_success(results_count=3)
        self.assertEqual(cb.status, "CLOSED")
        self.assertEqual(cb.probe_result, "SUCCESS")
        self.assertEqual(cb.failure_count, 0)
        self.assertTrue(cb.can_request())

    def test_28_circuit_failed_recovery(self):
        """Verifies failed probe in HALF_OPEN transitions back to OPEN."""
        cb = ProviderCircuitBreaker("TEST_FAIL_RECOVERY", failure_threshold=2, cooldown_seconds=0.05)
        cb.trip(cooldown_seconds=0.05)
        time.sleep(0.06)
        self.assertEqual(cb.status, "HALF_OPEN")
        self.assertTrue(cb.can_request())

        cb.record_failure("probe request timed out")
        self.assertEqual(cb.status, "OPEN")
        self.assertEqual(cb.probe_result, "FAILURE")
        self.assertFalse(cb.can_request())

    def test_29_seoul_kimchi_four_run_regression(self):
        """Explicit 4-run regression test for Seoul Kimchi deduplication."""
        # RUN 1: Seoul Kimchi inserted into CRM
        crm_leads = [{
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "business_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "address": "275 Upper Brook St, Manchester M13 0HR, United Kingdom",
            "street": "275 Upper Brook St",
            "postcode": "M13 0HR",
            "phone": "+44 7745 527603",
            "qualification_state": "OUTREACH_READY",
            "outreach_status": "SENT",
            "campaign_id": "CAMP-2026-01"
        }]

        # RUN 2: Same business discovered again
        run2_candidate = {
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "street": "275 Upper Brook St",
            "postcode": "M13 0HR",
            "phone": "+44 7745 527603"
        }
        res2 = self.matcher.match_candidate(run2_candidate, crm_leads)
        self.assertEqual(res2.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertEqual(res2.matched_lead_id, "LEAD-MAN-4DB3EF")
        self.assertGreaterEqual(res2.confidence, 0.95)

        # RUN 3: Punctuation, capitalization, address & phone formatting variation
        run3_candidate = {
            "company_name": "SEOUL-KIMCHI",
            "city": "Manchester",
            "country": "United Kingdom",
            "street": "275 Upper Brook Street",
            "postcode": "M130HR",
            "phone": "07745 527603"
        }
        res3 = self.matcher.match_candidate(run3_candidate, crm_leads)
        self.assertEqual(res3.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertEqual(res3.matched_lead_id, "LEAD-MAN-4DB3EF")
        self.assertGreaterEqual(res3.confidence, 0.90)

        # RUN 4: Same name but genuinely different branch in another city
        run4_candidate = {
            "company_name": "Seoul Kimchi",
            "city": "Leeds",
            "country": "United Kingdom",
            "street": "Vicar Lane",
            "postcode": "LS1 6JL"
        }
        res4 = self.matcher.match_candidate(run4_candidate, crm_leads)
        self.assertEqual(res4.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)
        self.assertLess(res4.confidence, 0.50)

    def test_30_normalized_duplicate(self):
        """Legal suffixes and trade descriptors do not prevent duplicate detection."""
        crm = [{
            "lead_id": "LEAD-FLY-001",
            "company_name": "Flying Pizza",
            "city": "Leeds",
            "country": "United Kingdom",
            "postcode": "LS8 2AJ"
        }]
        cand = {
            "company_name": "THE FLYING PIZZA & CO. LTD",
            "city": "Leeds",
            "country": "United Kingdom",
            "postcode": "LS8 2AJ"
        }
        res = self.matcher.match_candidate(cand, crm)
        self.assertEqual(res.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertEqual(res.matched_lead_id, "LEAD-FLY-001")

    def test_31_different_branch_separation(self):
        """Distinct branch addresses in same city or different cities are treated as NEW_BUSINESS."""
        crm = [{
            "lead_id": "LEAD-SUB-001",
            "company_name": "Subway",
            "city": "Leeds",
            "country": "United Kingdom",
            "address": "12 Briggate",
            "postcode": "LS1 6ER"
        }]
        # Same city distinct street branch
        cand_branch = {
            "company_name": "Subway",
            "city": "Leeds",
            "country": "United Kingdom",
            "address": "450 Headingley Lane",
            "postcode": "LS6 2BX"
        }
        res_branch = self.matcher.match_candidate(cand_branch, crm)
        self.assertEqual(res_branch.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)

        # Different city branch
        cand_manchester = {
            "company_name": "Subway",
            "city": "Manchester",
            "country": "United Kingdom",
            "address": "100 Oxford Road",
            "postcode": "M1 5QA"
        }
        res_city = self.matcher.match_candidate(cand_manchester, crm)
        self.assertEqual(res_city.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)

    def test_32_possible_duplicate_false_positive_audit(self):
        """Unrelated businesses sharing common tokens and postcode district are classified as legitimate NEW_BUSINESS."""
        crm = [{
            "lead_id": "LEAD-SAN-001",
            "company_name": "Sandys Sandwich Shop",
            "city": "Pudsey",
            "country": "United Kingdom",
            "street": "Beechwood Street",
            "postcode": "LS28 6PS"
        }]
        cand = {
            "company_name": "The Sandwich Bar",
            "city": "Pudsey",
            "country": "United Kingdom",
            "street": "Bradford Road",
            "postcode": "LS28 6QB"
        }
        res = self.matcher.match_candidate(cand, crm)
        # Matcher V2 recognizes distinct businesses with different distinctive tokens and locations
        self.assertEqual(res.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)

    def test_33_google_sheets_duplicate(self):
        """Verifies duplicate prevention on real Google Sheets cache data."""
        with open("data/cache_sheets_leads.json") as f:
            leads = json.load(f).get("leads", [])

        # Seoul Kimchi is present in real Google Sheets cache
        seoul_in_sheet = any("Seoul Kimchi" in (l.get("business_name") or l.get("company_name", "")) for l in leads)
        self.assertTrue(seoul_in_sheet)

        # Re-discovering Seoul Kimchi against the real sheet records must match EXISTING_BUSINESS
        incoming = {
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "address": "275 Upper Brook St, Manchester M13 0HR, United Kingdom",
            "phone": "+44 7745 527603"
        }
        res = self.matcher.match_candidate(incoming, leads)
        self.assertEqual(res.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertEqual(res.matched_lead_id, "LEAD-MAN-4DB3EF")

    def test_34_duplicate_outreach_safety(self):
        """Verifies duplicate outreach is blocked across different CRM rows for the same business identity."""
        queue = [{
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "channel": "Instagram Direct Message",
            "status": OutreachStatus.SENT.value
        }]

        # Different lead_id, slightly varied company name
        is_blocked = _already_sent(
            queue,
            lead_id="LEAD-NEW-SEPARATE-ROW",
            channel="Instagram Direct Message",
            company_name="Seoul Kimchi Restaurant",
            city="Manchester",
            country="United Kingdom"
        )
        self.assertTrue(is_blocked, "Must block duplicate outreach even with different lead_id")

    def test_35_all_28_known_false_positives_produce_new_business(self):
        """Explicit regression verifying all 28 audited Leeds false-positive pairs produce NEW_BUSINESS."""
        from run_crm_identity_precision_benchmark import get_benchmark_datasets
        fp_cases, _, _, _ = get_benchmark_datasets()
        self.assertEqual(len(fp_cases), 28)
        for c1, c2, p1, p2 in fp_cases:
            res = self.matcher.match_candidate(c1, [c2])
            self.assertEqual(
                res.outcome,
                IdentityMatchOutcome.NEW_BUSINESS.value,
                f"False positive detected: {p1} vs {p2} was classified as {res.outcome} ({res.confidence})"
            )

    def test_36_cross_run_deduplication_five_runs(self):
        """
        Simulates 5 consecutive pipeline runs:
          Run 1: Business discovered -> inserted as canonical lead
          Run 2: Same business discovered -> matched to EXISTING_BUSINESS, 0 new rows
          Run 3: Name variation -> matched to EXISTING_BUSINESS, 0 new rows
          Run 4: Address formatting variation -> matched to EXISTING_BUSINESS, 0 new rows
          Run 5: Phone formatting variation -> matched to EXISTING_BUSINESS, 0 new rows
          Run 6: Genuinely new branch in different city -> created as distinct NEW_BUSINESS
        """
        crm = []
        pipeline_runs = [
            {"lead_id": "LEAD-MAN-001", "company_name": "Bundobust", "city": "Manchester", "address": "61 Piccadilly", "postcode": "M1 2AG", "phone": "0161 359 6757"},
            {"lead_id": "LEAD-TMP-002", "company_name": "Bundobust", "city": "Manchester", "address": "61 Piccadilly", "postcode": "M1 2AG", "phone": "0161 359 6757"},
            {"lead_id": "LEAD-TMP-003", "company_name": "Bundobust Manchester", "city": "Manchester", "address": "61 Piccadilly", "postcode": "M1 2AG", "phone": "0161 359 6757"},
            {"lead_id": "LEAD-TMP-004", "company_name": "Bundobust", "city": "Manchester", "address": "61 Piccadilly, Suite 1", "postcode": "M12AG", "phone": "0161 359 6757"},
            {"lead_id": "LEAD-TMP-005", "company_name": "Bundobust", "city": "Manchester", "address": "61 Piccadilly", "postcode": "M1 2AG", "phone": "+44 161 359 6757"},
            {"lead_id": "LEAD-LDS-001", "company_name": "Bundobust", "city": "Leeds", "address": "6 Mill Hill", "postcode": "LS1 5DQ", "phone": "0113 243 1248"}
        ]

        for i, cand in enumerate(pipeline_runs):
            res = self.matcher.match_candidate(cand, crm)
            if res.outcome == IdentityMatchOutcome.EXISTING_BUSINESS.value:
                # Deduplication triggers: preserve existing canonical lead ID
                self.assertEqual(res.matched_lead_id, "LEAD-MAN-001")
            else:
                crm.append(cand)

        # Only 2 distinct businesses in CRM: Manchester canonical lead and Leeds branch
        self.assertEqual(len(crm), 2)
        self.assertEqual([c["lead_id"] for c in crm], ["LEAD-MAN-001", "LEAD-LDS-001"])

    def test_37_seoul_kimchi_expanded_variations(self):
        """Retain and expand Seoul Kimchi regression: same business, variations, different branch, completely different city/postcode/phone/domain."""
        crm_leads = [{
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "address": "275 Upper Brook St, Manchester M13 0HR, United Kingdom",
            "street": "275 Upper Brook St",
            "postcode": "M13 0HR",
            "phone": "+44 7745 527603"
        }]

        # Run 2: Exact re-discovery
        res2 = self.matcher.match_candidate({
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "street": "275 Upper Brook St",
            "postcode": "M13 0HR",
            "phone": "+44 7745 527603"
        }, crm_leads)
        self.assertEqual(res2.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertEqual(res2.matched_lead_id, "LEAD-MAN-4DB3EF")

        # Run 3: Formatting & phone variation
        res3 = self.matcher.match_candidate({
            "company_name": "SEOUL-KIMCHI",
            "city": "Manchester",
            "country": "United Kingdom",
            "street": "275 Upper Brook Street",
            "postcode": "M130HR",
            "phone": "07745 527603"
        }, crm_leads)
        self.assertEqual(res3.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertEqual(res3.matched_lead_id, "LEAD-MAN-4DB3EF")

        # Run 4: Leeds branch
        res4 = self.matcher.match_candidate({
            "company_name": "Seoul Kimchi",
            "city": "Leeds",
            "country": "United Kingdom",
            "street": "Vicar Lane",
            "postcode": "LS1 6JL"
        }, crm_leads)
        self.assertEqual(res4.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)

        # Run 5: Different city, different postcode, different phone, different domain
        res5 = self.matcher.match_candidate({
            "company_name": "Seoul Kimchi",
            "city": "Birmingham",
            "country": "United Kingdom",
            "street": "New Street",
            "postcode": "B2 4QA",
            "phone": "+44 121 643 1111",
            "website": "https://seoulkimchibirmingham.co.uk"
        }, crm_leads)
        self.assertEqual(res5.outcome, IdentityMatchOutcome.NEW_BUSINESS.value)


if __name__ == "__main__":
    unittest.main()

