"""Ensure CRM/dashboard reads never mutate or auto-repair live Google Sheets."""

import unittest
from unittest.mock import MagicMock

import gspread

from lib.sheets.google_sheets import GoogleSheetsStorageProvider


class TestGoogleSheetsReadOnlyAccess(unittest.TestCase):
    def setUp(self):
        self.provider = GoogleSheetsStorageProvider(
            sheet_url="https://docs.google.com/spreadsheets/d/dummy"
        )
        self.spreadsheet = MagicMock()
        self.leads_ws = MagicMock()
        self.review_ws = MagicMock()
        self.research_ws = MagicMock()
        self.worksheets = {
            "LEADS": self.leads_ws,
            "REVIEW_QUEUE": self.review_ws,
            "RESEARCH_LOG": self.research_ws,
        }
        self.spreadsheet.worksheet.side_effect = self._get_worksheet
        # Pre-inject the client so these tests never authenticate or touch a real sheet.
        self.provider._spreadsheet = self.spreadsheet

    def _get_worksheet(self, title):
        if title not in self.worksheets:
            raise gspread.WorksheetNotFound(title)
        return self.worksheets[title]

    def assert_no_sheet_mutations(self):
        self.spreadsheet.add_worksheet.assert_not_called()
        for worksheet in self.worksheets.values():
            worksheet.update.assert_not_called()
            worksheet.append_rows.assert_not_called()
            worksheet.append_row.assert_not_called()
            worksheet.insert_row.assert_not_called()
            worksheet.add_cols.assert_not_called()
        # A read must not populate the schema-repairing, create-if-missing properties.
        self.assertIsNone(self.provider._leads_ws)
        self.assertIsNone(self.provider._review_ws)
        self.assertIsNone(self.provider._research_ws)

    def test_fetch_all_leads_is_read_only_and_normalizes_blank_status(self):
        self.leads_ws.get_all_records.return_value = [
            {"company_name": "Synthetic Bistro", "outreach_status": ""},
            {"company_name": "Synthetic Cafe", "outreach_status": None},
            {"company_name": "Synthetic Deli", "outreach_status": "SENT"},
        ]

        result = self.provider.fetch_all_leads()

        self.assertEqual(
            [row["outreach_status"] for row in result],
            ["NOT_READY", "NOT_READY", "SENT"],
        )
        self.spreadsheet.worksheet.assert_called_once_with("LEADS")
        self.leads_ws.get_all_records.assert_called_once_with()
        self.assert_no_sheet_mutations()

    def test_fetch_review_queue_is_read_only(self):
        self.review_ws.get_all_records.return_value = [
            {"company_name": "Synthetic Review Candidate"}
        ]

        result = self.provider.fetch_review_queue()

        self.assertEqual(len(result), 1)
        self.spreadsheet.worksheet.assert_called_once_with("REVIEW_QUEUE")
        self.review_ws.get_all_records.assert_called_once_with()
        self.assert_no_sheet_mutations()

    def test_fetch_research_log_is_read_only(self):
        self.research_ws.get_all_records.return_value = [
            {"company_name": "Synthetic Research Candidate"}
        ]

        result = self.provider.fetch_research_log()

        self.assertEqual(len(result), 1)
        self.spreadsheet.worksheet.assert_called_once_with("RESEARCH_LOG")
        self.research_ws.get_all_records.assert_called_once()
        self.assert_no_sheet_mutations()

    def test_missing_tab_fails_closed_without_creating_it(self):
        self.spreadsheet.worksheet.side_effect = gspread.WorksheetNotFound("LEADS")

        with self.assertRaises(gspread.WorksheetNotFound):
            self.provider.fetch_all_leads()

        self.spreadsheet.add_worksheet.assert_not_called()
        self.leads_ws.update.assert_not_called()
        self.leads_ws.append_rows.assert_not_called()
        self.assertIsNone(self.provider._leads_ws)


if __name__ == "__main__":
    unittest.main()
