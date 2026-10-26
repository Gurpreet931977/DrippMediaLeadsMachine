"""
test_test_namespace_isolation.py
================================
Unit tests verifying isolated test namespace functionality, tab routing,
fail-closed guards, and protection of production operational tabs.
"""

import unittest
from unittest.mock import MagicMock, patch
import os
import pytest

from lib.sheets.google_sheets import (
    GoogleSheetsStorageProvider,
    OPERATIONAL_TABS,
    TEST_TAB_MAP,
    TEST_NAMESPACE_PREFIX,
    NamespaceSpreadsheetProxy,
    CANONICAL_PRODUCTION_SHEET_ID,
)
from lib.types import Lead, QualificationState, ResearchLogEntry


class TestTestNamespaceIsolation(unittest.TestCase):
    def setUp(self):
        # Ensure clean environment
        self.env_patcher = patch.dict(os.environ, {
            "TRAVEL_MODE": "ACTIVE",
            "COMMERCIAL_ACTIONS_ENABLED": "false",
            "AUTOMATED_EMAIL_ENABLED": "false",
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_01_default_production_mode_routes_to_operational_tabs(self):
        """Default provider (test_namespace=False) must resolve to standard operational tabs."""
        provider = GoogleSheetsStorageProvider()
        self.assertFalse(provider.test_namespace)
        self.assertEqual(provider.resolve_tab_name("LEADS"), "LEADS")
        self.assertEqual(provider.resolve_tab_name("REVIEW_QUEUE"), "REVIEW_QUEUE")
        self.assertEqual(provider.resolve_tab_name("RESEARCH_LOG"), "RESEARCH_LOG")
        self.assertEqual(provider.resolve_tab_name("OVERVIEW"), "OVERVIEW")
        self.assertEqual(provider.resolve_tab_name("RESEARCH_LOG_QUARANTINE"), "RESEARCH_LOG_QUARANTINE")

    def test_02_test_namespace_mode_routes_to_dedicated_test_tabs(self):
        """When test_namespace=True, logical operational tab names resolve to TEST_ONLY_* tabs."""
        provider = GoogleSheetsStorageProvider(test_namespace=True)
        self.assertTrue(provider.test_namespace)
        self.assertEqual(provider.resolve_tab_name("LEADS"), "TEST_ONLY_LEADS")
        self.assertEqual(provider.resolve_tab_name("REVIEW_QUEUE"), "TEST_ONLY_REVIEW_QUEUE")
        self.assertEqual(provider.resolve_tab_name("RESEARCH_LOG"), "TEST_ONLY_RESEARCH_LOG")
        self.assertEqual(provider.resolve_tab_name("OVERVIEW"), "TEST_ONLY_OVERVIEW")
        self.assertEqual(provider.resolve_tab_name("RESEARCH_LOG_QUARANTINE"), "TEST_ONLY_RESEARCH_LOG_QUARANTINE")

    def test_03_test_namespace_direct_operational_tab_access_blocked(self):
        """In test namespace mode, opening an operational tab directly via spreadsheet.worksheet fails closed."""
        raw_mock = MagicMock()
        provider = GoogleSheetsStorageProvider(test_namespace=True)
        provider._raw_spreadsheet = raw_mock

        proxy = provider.spreadsheet
        self.assertIsInstance(proxy, NamespaceSpreadsheetProxy)

        for op_tab in OPERATIONAL_TABS:
            with self.assertRaises(PermissionError) as ctx:
                proxy.worksheet(op_tab)
            self.assertIn("Test Namespace Safety Block", str(ctx.exception))
            self.assertIn(op_tab, str(ctx.exception))

    def test_04_test_namespace_safe_clear_blocks_operational_tabs(self):
        """In test namespace mode, safe_clear_worksheet strictly forbids targeting operational tabs."""
        provider = GoogleSheetsStorageProvider(test_namespace=True)
        for op_tab in OPERATIONAL_TABS:
            with self.assertRaises(PermissionError) as ctx:
                provider.safe_clear_worksheet(op_tab, confirm_token="CONFIRM_DESTRUCTIVE_CLEAR")
            self.assertIn("strictly prohibited", str(ctx.exception))

    def test_05_clear_test_tab_data_fails_closed_in_production_mode(self):
        """clear_test_tab_data must raise PermissionError if called on production provider."""
        provider = GoogleSheetsStorageProvider(test_namespace=False)
        with self.assertRaises(PermissionError):
            provider.clear_test_tab_data("TEST_ONLY_LEADS")

    def test_06_test_namespace_ensures_columns_and_creates_test_tabs(self):
        """ensure_test_namespace_tabs initializes all 5 dedicated test tabs."""
        mock_ss = MagicMock()
        mock_ws_map = {}
        for tab in TEST_TAB_MAP.values():
            ws = MagicMock()
            ws.title = tab
            ws.col_count = 150
            ws.row_values.return_value = ["header1", "header2"]
            mock_ws_map[tab] = ws

        def get_ws(title):
            return mock_ws_map.get(title, MagicMock())

        mock_ss.worksheet.side_effect = get_ws

        provider = GoogleSheetsStorageProvider(test_namespace=True)
        provider._raw_spreadsheet = mock_ss

        res = provider.ensure_test_namespace_tabs()
        self.assertEqual(len(res), 5)
        for logical_tab, test_tab in TEST_TAB_MAP.items():
            self.assertEqual(res[logical_tab], test_tab)

    def test_07_qualified_leads_persistence_targets_test_leads_tab(self):
        """save_qualified_leads in test_namespace mode writes only to TEST_ONLY_LEADS."""
        provider = GoogleSheetsStorageProvider(test_namespace=True)
        mock_leads_ws = MagicMock()
        mock_leads_ws.title = "TEST_ONLY_LEADS"
        mock_leads_ws.col_count = 150
        mock_leads_ws.get_all_records.return_value = []
        mock_leads_ws.row_values.return_value = []

        provider._leads_ws = mock_leads_ws
        provider.ensure_leads_columns = MagicMock()

        lead = Lead(
            lead_id="LEAD-TEST-001",
            company_name="[TEST ONLY] Sample Restaurant",
            industry="Restaurants",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            website="https://sample.co.uk",
            qualification_state=QualificationState.OUTREACH_READY.value,
            lead_score=85
        )

        ins, upd = provider.save_qualified_leads([lead])
        self.assertEqual(ins, 1)
        self.assertEqual(upd, 0)
        self.assertTrue(mock_leads_ws.append_rows.called)


if __name__ == "__main__":
    unittest.main()
