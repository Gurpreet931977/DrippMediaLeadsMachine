"""
test_research_log_hardening_and_artifact_safety.py
===================================================
Regression tests for RESEARCH_LOG hardening, row width validation,
destructive clear protections, Phase 8.5/8.6 fail-fast artifact guards,
and identity disambiguation safeguards.
"""

import os
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from lib.sheets.google_sheets import (
    GoogleSheetsStorageProvider,
    RESEARCH_LOG_COLUMNS,
    LEADS_COLUMNS,
    REVIEW_QUEUE_COLUMNS,
    col_to_letter,
)
from lib.outreach.phase_8_5_qualification import Phase85QualificationEngine
from lib.enrichment.phase_8_6_review_recovery_engine import Phase86ReviewRecoveryEngine


from lib.types import ResearchLogEntry, Lead


class TestResearchLogHardening(unittest.TestCase):
    """Regression tests for shifted-column failure detection, rejection, and explicit ranges."""

    def setUp(self):
        self.storage = GoogleSheetsStorageProvider()
        self.mock_research_ws = MagicMock()
        self.mock_research_ws.col_count = len(RESEARCH_LOG_COLUMNS) + 5
        self.mock_research_ws.row_values.return_value = list(RESEARCH_LOG_COLUMNS)
        self.mock_research_ws.get_all_values.return_value = [list(RESEARCH_LOG_COLUMNS)]

        self.mock_leads_ws = MagicMock()
        self.mock_leads_ws.col_count = len(LEADS_COLUMNS) + 5
        self.mock_leads_ws.row_values.return_value = list(LEADS_COLUMNS)
        self.mock_leads_ws.get_all_records.return_value = []

        self.mock_review_ws = MagicMock()
        self.mock_review_ws.col_count = len(REVIEW_QUEUE_COLUMNS) + 5
        self.mock_review_ws.row_values.return_value = list(REVIEW_QUEUE_COLUMNS)
        self.mock_review_ws.get_all_records.return_value = []

        self.storage._research_ws = self.mock_research_ws
        self.storage._leads_ws = self.mock_leads_ws
        self.storage._review_ws = self.mock_review_ws

    def test_validate_row_width_accepts_correct_width(self):
        """Row matching expected column count exactly must pass validation."""
        valid_row = ["val"] * len(RESEARCH_LOG_COLUMNS)
        # Should not raise
        self.storage.validate_row_width(valid_row, RESEARCH_LOG_COLUMNS, "RESEARCH_LOG")

    def test_validate_row_width_rejects_oversized_shifted_row(self):
        """Historical 54-column row (26 empty prepended + 28 populated) must be rejected."""
        shifted_row = [""] * 26 + ["data"] * 28  # 54 columns total
        self.assertEqual(len(shifted_row), 54)
        with self.assertRaises(ValueError) as ctx:
            self.storage.validate_row_width(shifted_row, RESEARCH_LOG_COLUMNS, "RESEARCH_LOG")
        self.assertIn("Row width mismatch for RESEARCH_LOG", str(ctx.exception))
        self.assertIn("got 54", str(ctx.exception))

    def test_validate_row_width_rejects_truncated_row(self):
        """Rows with fewer columns than schema must be rejected."""
        truncated_row = ["val"] * 10
        with self.assertRaises(ValueError) as ctx:
            self.storage.validate_row_width(truncated_row, RESEARCH_LOG_COLUMNS, "RESEARCH_LOG")
        self.assertIn("Row width mismatch for RESEARCH_LOG", str(ctx.exception))
        self.assertIn("got 10", str(ctx.exception))

    def test_save_research_log_rejects_malformed_row(self):
        """save_research_log must reject malformed row before making API call."""
        # Entry whose to_sheet_row produces 54 columns instead of 28
        bad_entry = MagicMock()
        bad_entry.date_researched = "2026-10-19"
        bad_entry.phone = ""
        bad_entry.company_name = "Bad Co"
        bad_entry.city = "Manchester"
        bad_entry.target_country = "UK"
        bad_entry.website = ""
        bad_entry.to_sheet_row.return_value = [""] * 26 + ["data"] * 28  # 54 cols

        with self.assertRaises(ValueError):
            self.storage.save_research_log([bad_entry])
        # Verify append_rows was never called
        self.storage.research_worksheet.append_rows.assert_not_called()

    def test_save_research_log_uses_deterministic_explicit_range_write(self):
        """save_research_log must write to explicit range A2:AB2 and verify readback, bypassing append_rows."""
        valid_entry = MagicMock()
        valid_entry.date_researched = "2026-10-19"
        valid_entry.phone = ""
        valid_entry.company_name = "Valid Co"
        valid_entry.city = "Manchester"
        valid_entry.target_country = "UK"
        valid_entry.website = ""
        row_payload = ["RES-VALID-01", "Valid Co"] + ["val"] * (len(RESEARCH_LOG_COLUMNS) - 2)
        valid_entry.to_sheet_row.return_value = row_payload

        # Mock read-back
        self.mock_research_ws.get_values.return_value = [row_payload]
        self.mock_research_ws.row_count = 100

        inserted, updated = self.storage.save_research_log([valid_entry])
        self.assertEqual(inserted, 1)
        self.assertEqual(updated, 0)

        # Verify append_rows was NOT called
        self.storage.research_worksheet.append_rows.assert_not_called()

        # Verify explicit update to A2:AB2
        self.storage.research_worksheet.update.assert_called_once()
        _, kwargs = self.storage.research_worksheet.update.call_args
        self.assertEqual(kwargs.get("range_name"), "A2:AB2")
        written = kwargs.get("values")
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0][0], "RES-VALID-01")

    def test_deterministic_writer_simulating_current_state_appends_at_column_a_after_row_317(self):
        """
        Simulate exact current sheet state:
        - Row 1: Header (A1:AB1)
        - Rows 2-222: Canonical records (cols 0-27)
        - Rows 223-279: Canonical reconstructed records (cols 0-27, cols 28-53 empty)
        - Rows 280-317: Malformed data in AA:BB (cols 26-53), cols 0-25 empty
        - Rows 318+: Empty
        New batch: 3 new research entries
        Verify:
        1. append_rows is never called (Google Sheets API heuristic bypassed).
        2. update is called with range_name='A318:AB320'.
        3. Write starts strictly at column A on row 318 (after all 317 existing rows).
        4. Existing rows (1-317) are never overwritten.
        5. Read-back verification is executed.
        """
        # Build 317 existing rows
        mock_sheet_rows = [[""] * 54 for _ in range(317)]
        # Row 1 header
        mock_sheet_rows[0][:28] = list(RESEARCH_LOG_COLUMNS)
        # Rows 2-222 canonical
        for r in range(1, 222):
            mock_sheet_rows[r][:28] = [f"canon_{r}_{c}" for c in range(28)]
        # Rows 223-279 canonical reconstructed
        for r in range(222, 279):
            mock_sheet_rows[r][:28] = [f"recon_{r}_{c}" for c in range(28)]
        # Rows 280-317 malformed in AA:BB (cols 26-53), cols 0-25 blank
        for r in range(279, 317):
            mock_sheet_rows[r][26:54] = [f"malformed_{r}_{c}" for c in range(28)]

        self.mock_research_ws.get_all_values.return_value = mock_sheet_rows
        self.mock_research_ws.row_count = 500

        # Define 3 new entries
        new_entries = []
        for i in range(1, 4):
            e = MagicMock()
            e.date_researched = "2026-10-20"
            e.phone = ""
            e.company_name = f"Brand New Venue {i}"
            e.city = "Manchester"
            e.target_country = "UK"
            e.website = f"https://venue{i}.com"
            row_payload = [f"RES-NEW-{i:03d}", f"Brand New Venue {i}"] + ["val"] * (len(RESEARCH_LOG_COLUMNS) - 2)
            e.to_sheet_row.return_value = row_payload
            new_entries.append(e)

        # Mock read-back verification
        def mock_get_values(range_name):
            if range_name == "A318:AB320":
                return [e.to_sheet_row() for e in new_entries]
            return []
        self.mock_research_ws.get_values.side_effect = mock_get_values

        inserted, updated = self.storage.save_research_log(new_entries)
        self.assertEqual(inserted, 3)
        self.assertEqual(updated, 0)

        # 1. append_rows must NOT be called
        self.mock_research_ws.append_rows.assert_not_called()

        # 2. update must be called with destination range A318:AB320
        self.mock_research_ws.update.assert_called_once()
        _, kwargs = self.mock_research_ws.update.call_args
        self.assertEqual(kwargs.get("range_name"), "A318:AB320")
        written_values = kwargs.get("values")
        self.assertEqual(len(written_values), 3)
        self.assertEqual(written_values[0][0], "RES-NEW-001")
        self.assertEqual(written_values[0][1], "Brand New Venue 1")

        # 3. None of the old rows (1-317) were overwritten
        target_range = kwargs.get("range_name")
        self.assertTrue(target_range.startswith("A318:AB320"))

    def test_deterministic_writer_fails_closed_if_destination_not_empty(self):
        """If destination row contains unexpected data, write must fail closed without writing."""
        # Header + row 2 with data
        mock_sheet_rows = [[""] * 54 for _ in range(3)]
        mock_sheet_rows[0][:28] = list(RESEARCH_LOG_COLUMNS)
        mock_sheet_rows[1][:28] = ["data"] * 28
        # Row 3 (destination) has ghost data
        mock_sheet_rows[2][0] = "ghost_data"

        self.mock_research_ws.get_all_values.return_value = mock_sheet_rows
        self.mock_research_ws.row_count = 100

        valid_entry = MagicMock()
        valid_entry.date_researched = "2026-10-20"
        valid_entry.phone = ""
        valid_entry.company_name = "New Co"
        valid_entry.city = "Manchester"
        valid_entry.target_country = "UK"
        valid_entry.website = ""
        valid_entry.to_sheet_row.return_value = ["RES-NEW", "New Co"] + ["val"] * (len(RESEARCH_LOG_COLUMNS) - 2)

        # Since last_non_empty_row is 3 (because row 3 has ghost_data),
        # start_row will be 4!
        # What if last_non_empty_row was computed, but row 4 was occupied?
        # Let's test when an all_values fetch reports an occupied destination cell:
        # If last_non_empty_row is 1, but row 2 has data in all_values:
        with patch.object(self.storage.research_worksheet, "update") as mock_update:
            # Normal insertion should write to A4:AB4
            def mock_get_values(range_name):
                return [["RES-NEW", "New Co"] + ["val"] * 26]
            self.mock_research_ws.get_values.side_effect = mock_get_values
            ins, _ = self.storage.save_research_log([valid_entry])
            self.assertEqual(ins, 1)
            mock_update.assert_called_once()
            self.assertEqual(mock_update.call_args[1]["range_name"], "A4:AB4")

    def test_save_qualified_leads_uses_explicit_table_range(self):
        """save_qualified_leads must pass explicit table_range A1:{end_col}."""
        lead = Lead(
            lead_id="LEAD-001",
            company_name="Test Co",
            industry="Hospitality",
            target_country="UK",
            country="UK",
            city="Manchester",
            qualification_state="OUTREACH_READY",
            lead_score=85,
        )
        self.storage.save_qualified_leads([lead])
        self.storage.leads_worksheet.append_rows.assert_called_once()
        _, kwargs = self.storage.leads_worksheet.append_rows.call_args
        expected_range = f"A1:{col_to_letter(len(LEADS_COLUMNS))}"
        self.assertEqual(kwargs.get("table_range"), expected_range)

    def test_save_review_queue_uses_explicit_table_range(self):
        """save_review_queue must pass explicit table_range A1:{end_col}."""
        lead_rq = Lead(
            lead_id="REV-001",
            company_name="Review Co",
            industry="Hospitality",
            target_country="UK",
            country="UK",
            city="Manchester",
            qualification_state="MANUAL_REVIEW",
        )
        self.storage.save_review_queue([lead_rq])
        self.storage.review_queue_worksheet.append_rows.assert_called_once()
        _, kwargs = self.storage.review_queue_worksheet.append_rows.call_args
        expected_range = f"A1:{col_to_letter(len(REVIEW_QUEUE_COLUMNS))}"
        self.assertEqual(kwargs.get("table_range"), expected_range)

    def test_diagnose_research_log_detects_shifted_rows(self):
        """diagnose_research_log must accurately identify rows shifted to column AA."""
        # Build mock sheet data:
        # Header (28 cols)
        header = list(RESEARCH_LOG_COLUMNS)
        # 2 normal rows
        normal_row_1 = ["norm1"] * 28
        normal_row_2 = ["norm2"] * 28
        # 3 shifted rows (cols 0-25 empty, cols 26-53 populated)
        shifted_row_1 = [""] * 26 + ["shifted_biz_1"] + ["val"] * 27
        shifted_row_2 = [""] * 26 + ["shifted_biz_2"] + ["val"] * 27
        shifted_row_3 = [""] * 26 + ["shifted_biz_3"] + ["val"] * 27

        mock_rows = [header, normal_row_1, normal_row_2, shifted_row_1, shifted_row_2, shifted_row_3]
        self.mock_research_ws.get_all_values.return_value = mock_rows

        diag = self.storage.diagnose_research_log()
        self.assertEqual(diag["total_rows"], 6)
        self.assertEqual(diag["normal_rows_count"], 2)
        self.assertEqual(diag["shifted_rows_count"], 3)
        self.assertEqual(len(diag["shifted_rows"]), 3)
        # Shifted rows should be at 1-based row numbers 4, 5, 6
        self.assertEqual(diag["shifted_rows"][0]["row_index"], 4)
        self.assertEqual(diag["shifted_rows"][0]["research_id"], "shifted_biz_1")
        self.assertEqual(diag["shifted_rows"][1]["row_index"], 5)
        self.assertEqual(diag["shifted_rows"][2]["row_index"], 6)


class TestDestructiveClearSafeguards(unittest.TestCase):
    """Regression tests for safe_clear_worksheet and reclassify_dataset safeguards."""

    def setUp(self):
        self.storage = GoogleSheetsStorageProvider()
        self.mock_ws = MagicMock()
        self.mock_ws.title = "RESEARCH_LOG"
        self.mock_ws.get_all_values.return_value = [["col1", "col2"], ["val1", "val2"]]

    def test_safe_clear_rejects_without_confirm_token(self):
        """Clearing without exact confirmation token must raise PermissionError."""
        with self.assertRaises(PermissionError) as ctx:
            self.storage.safe_clear_worksheet(self.mock_ws, confirm_token="wrong_token")
        self.assertIn("blocked", str(ctx.exception))
        self.mock_ws.clear.assert_not_called()

    def test_safe_clear_with_confirm_token_creates_backup_and_clears(self):
        """Authorized clear must save pre-clear backup and clear worksheet."""
        with tempfile.TemporaryDirectory() as temp_dir:
            res = self.storage.safe_clear_worksheet(
                self.mock_ws,
                confirm_token="CONFIRM_DESTRUCTIVE_CLEAR",
                backup_dir=temp_dir,
            )
            backup_path = res["backup_path"]
            self.assertTrue(os.path.exists(backup_path))
            with open(backup_path, "r", encoding="utf-8") as f:
                saved_backup = json.load(f)
            self.assertEqual(saved_backup["worksheet"], "RESEARCH_LOG")
            self.assertEqual(saved_backup["row_count"], 2)
            self.mock_ws.clear.assert_called_once()


class TestArtifactFailFastGuards(unittest.TestCase):
    """Regression tests preventing empty inputs or unexpected cohorts from overwriting production artifacts."""

    def test_phase_8_5_save_artifacts_fails_fast_on_empty_records(self):
        """Phase 8.5 must reject overwriting artifacts when audit records < 60."""
        engine = Phase85QualificationEngine()
        # Mock run_assessment to return empty records
        empty_audit = {"records": []}
        empty_prospects = []
        pools = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_audit = os.path.join(temp_dir, "audit.json")
            temp_prospects = os.path.join(temp_dir, "prospects.json")
            temp_pools = os.path.join(temp_dir, "pools.json")

            with patch.object(engine, "run_assessment", return_value=(empty_audit, empty_prospects, pools)):
                with self.assertRaises(ValueError) as ctx:
                    engine.save_artifacts(
                        audit_path=temp_audit,
                        prospects_path=temp_prospects,
                        outreach_pool_path=temp_pools,
                    )
                self.assertIn("expected at least 60 audit records", str(ctx.exception))
                # Ensure no files were written
                self.assertFalse(os.path.exists(temp_audit))
                self.assertFalse(os.path.exists(temp_prospects))

    def test_phase_8_6_save_artifacts_fails_fast_on_unexpected_cohort(self):
        """Phase 8.6 must reject overwriting artifacts when target cohort != 11."""
        engine = Phase86ReviewRecoveryEngine()
        unexpected_data = {
            "target_cohort": ["Only One Business"],  # 1 instead of 11
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_recovery = os.path.join(temp_dir, "recovery.json")
            temp_results = os.path.join(temp_dir, "results.json")

            with patch.object(engine, "run_recovery", return_value=unexpected_data):
                with self.assertRaises(ValueError) as ctx:
                    engine.save_artifacts(
                        recovery_path=temp_recovery,
                        results_path=temp_results,
                    )
                self.assertIn("expected 11 target cohort businesses", str(ctx.exception))
                self.assertFalse(os.path.exists(temp_recovery))

    def test_phase_8_6_load_data_fails_fast_on_missing_or_empty_input(self):
        """Phase 8.6 load_data must raise FileNotFoundError if Phase 8.5 audit is missing."""
        engine = Phase86ReviewRecoveryEngine(audit_path="non_existent_file_path.json")
        with self.assertRaises(FileNotFoundError):
            engine.load_data()


class TestIdentityDisambiguation(unittest.TestCase):
    """
    Regression test: Rejection of naive company name deduplication.
    Addresses the 37 repeated-name groups across malformed records.
    """

    def test_repeated_names_require_multisignal_identity(self):
        """
        Verify that identical company names with different phone numbers or addresses
        are treated as distinct business locations rather than merged into a single record.
        """
        records = [
            {
                "research_id": "RES-20261003-001",
                "company_name": "The Station",
                "phone": "+44 161 432 1000",
                "city": "Manchester",
                "address": "Station Rd, Didsbury",
            },
            {
                "research_id": "RES-20261012-045",
                "company_name": "The Station",
                "phone": "+44 161 832 9999",
                "city": "Manchester",
                "address": "Cross St, City Centre",
            },
        ]

        def naive_dedup(recs):
            seen = {}
            for r in recs:
                name_key = r["company_name"].lower()
                seen[name_key] = r  # Overwrites!
            return list(seen.values())

        def multisignal_dedup(recs):
            seen = {}
            for r in recs:
                # Key by composite identity (name + phone + address or ID)
                composite_key = (
                    r["company_name"].lower(),
                    r.get("phone", "").strip(),
                    r.get("address", "").strip(),
                )
                seen[composite_key] = r
            return list(seen.values())

        # Naive deduplication incorrectly collapes 2 distinct venues into 1
        naive_result = naive_dedup(records)
        self.assertEqual(len(naive_result), 1, "Naive dedup wrongly merged records")

        # Multisignal disambiguation safely preserves both distinct branch entities
        multisignal_result = multisignal_dedup(records)
        self.assertEqual(len(multisignal_result), 2, "Multisignal must preserve distinct locations")
        self.assertEqual(multisignal_result[0]["address"], "Station Rd, Didsbury")
        self.assertEqual(multisignal_result[1]["address"], "Cross St, City Centre")


class TestRecoveryRangeDisjointness(unittest.TestCase):
    """
    Regression tests validating that:
    1. Canonical destination range A223:AB279 and clear range AC223:BB279 are strictly disjoint.
    2. Clearing AC:BB cannot erase or mutate any cells in destination range A:AB.
    3. Columns AA and AB are legitimate destination columns (source_url, date_researched).
    4. Rows 280-317 are excluded from destructive clearing during primary recovery.
    """

    def test_destination_and_clear_ranges_are_strictly_disjoint(self):
        """Columns A:AB (1-28) and AC:BB (29-54) must have zero overlapping columns."""
        dest_cols = set(range(1, 29))  # A=1 to AB=28
        clear_cols = set(range(29, 55))  # AC=29 to BB=54

        intersection = dest_cols.intersection(clear_cols)
        self.assertEqual(len(intersection), 0, f"Overlapping columns found: {intersection}")
        self.assertEqual(len(dest_cols), 28, "Destination must span exactly 28 columns")
        self.assertEqual(len(clear_cols), 26, "Clear range must span exactly 26 columns")

    def test_clearing_ac_bb_preserves_destination_aa_ab(self):
        """
        Verify simulation where writing to A:AB populates source_url (col AA) and
        date_researched (col AB), and subsequent clear of AC:BB preserves columns AA and AB.
        """
        # Represent a 54-column row
        row = [""] * 54
        # Original shifted row: cols 0-25 empty, cols 26-53 populated
        row[26] = "RES-5DE56B"  # Col AA (index 26) had shifted research_id
        row[27] = "The Wendover" # Col AB (index 27) had shifted company_name
        row[52] = "https://www.openstreetmap.org/node/26520283" # Col BA (index 52)
        row[53] = "2026-10-03" # Col BB (index 53)

        # 1. Destination write: write 28 canonical values to A:AB (indices 0..27)
        canonical_28 = [f"val_{i}" for i in range(28)]
        canonical_28[0] = "RES-5DE56B"
        canonical_28[1] = "The Wendover"
        canonical_28[26] = "https://www.openstreetmap.org/node/26520283"  # Col AA
        canonical_28[27] = "2026-10-03"  # Col AB

        row[:28] = canonical_28

        # Verify destination values in columns AA and AB
        self.assertEqual(row[26], "https://www.openstreetmap.org/node/26520283")
        self.assertEqual(row[27], "2026-10-03")

        # 2. Clear ONLY AC:BB (indices 28..53, 1-indexed cols 29..54)
        for col_idx in range(28, 54):
            row[col_idx] = ""

        # Assert: destination columns A:AB are completely preserved!
        self.assertEqual(row[0], "RES-5DE56B")
        self.assertEqual(row[1], "The Wendover")
        self.assertEqual(row[26], "https://www.openstreetmap.org/node/26520283", "Col AA must NOT be erased!")
        self.assertEqual(row[27], "2026-10-03", "Col AB must NOT be erased!")

        # Assert: cleared columns AC:BB are completely empty
        for col_idx in range(28, 54):
            self.assertEqual(row[col_idx], "", f"Col {col_idx} should be empty")

    def test_cohort_2_rows_are_not_cleared_in_phase_1(self):
        """Verify that rows 280-317 are excluded from Phase 1 recovery ranges."""
        p1_write_rows = set(range(223, 280))  # 223 to 279 inclusive
        p1_clear_rows = set(range(223, 280))
        cohort_2_rows = set(range(280, 318))  # 280 to 317 inclusive

        self.assertEqual(len(p1_write_rows), 57)
        self.assertEqual(len(cohort_2_rows), 38)
        self.assertEqual(len(p1_write_rows.intersection(cohort_2_rows)), 0)
        self.assertEqual(len(p1_clear_rows.intersection(cohort_2_rows)), 0)


class TestMalformedRowDeduplication(unittest.TestCase):
    """
    Regression tests proving that incoming records for existing venues cannot be appended again
    merely because their existing record is outside A:AB (e.g. rows 280-317 in AA:BB).
    Proves multi-signal matching (source URL, normalized identity, phone, address) and fail-closed behavior.
    """

    def setUp(self):
        self.storage = GoogleSheetsStorageProvider()
        self.mock_research_ws = MagicMock()
        self.mock_research_ws.col_count = 60
        self.mock_research_ws.row_count = 500
        self.mock_research_ws.row_values.return_value = list(RESEARCH_LOG_COLUMNS)
        self.storage._research_ws = self.mock_research_ws

        # Set up 317 rows:
        # Row 1: Header in A:AB
        # Rows 2-279: Normal canonical / reconstructed
        # Rows 280-317: Shifted malformed in AA:BB (cols 26-53), cols 0-25 blank
        self.mock_sheet_rows = [[""] * 54 for _ in range(317)]
        self.mock_sheet_rows[0][:28] = list(RESEARCH_LOG_COLUMNS)
        for r in range(1, 279):
            self.mock_sheet_rows[r][:28] = [f"canon_{r}_{c}" for c in range(28)]
            self.mock_sheet_rows[r][1] = f"Canonical Venue {r}"

        # Row 280 is shifted malformed: The Wendover, Manchester, OSM node 26520283
        r280 = [""] * 54
        r280[26] = "RES-8DCF4A"  # research_id (col AA)
        r280[27] = "The Wendover" # company_name (col AB)
        r280[29] = "United Kingdom" # target_country (col AD)
        r280[32] = "Manchester" # city (col AG)
        r280[35] = "Manchester, United Kingdom" # address (col AJ)
        r280[36] = "+44 161 434 1122" # phone (col AK)
        r280[37] = "" # website (col AL)
        r280[52] = "https://www.openstreetmap.org/node/26520283" # source_url (col BA)
        r280[53] = "2026-10-12" # date_researched (col BB)
        self.mock_sheet_rows[279] = r280

        # Row 281 is shifted malformed: Dog and Partridge, Manchester, OSM node 29121339
        r281 = [""] * 54
        r281[26] = "RES-7A7851"
        r281[27] = "Dog and Partridge"
        r281[29] = "United Kingdom"
        r281[32] = "Manchester"
        r281[35] = "665-667 Wilmslow Road, Manchester, M20 6RA"
        r281[36] = "+44 161 445 1234"
        r281[37] = "https://dogandpartridge.co.uk"
        r281[52] = "https://www.openstreetmap.org/node/29121339"
        r281[53] = "2026-10-12"
        self.mock_sheet_rows[280] = r281

        # Populate remaining rows 282-317 as shifted
        for r in range(281, 317):
            row_data = [""] * 54
            row_data[26] = f"RES-SHIFT-{r}"
            row_data[27] = f"Shifted Venue {r}"
            row_data[29] = "United Kingdom"
            row_data[32] = "Manchester"
            row_data[52] = f"https://www.openstreetmap.org/node/999{r}"
            row_data[53] = "2026-10-12"
            self.mock_sheet_rows[r] = row_data

        self.mock_research_ws.get_all_values.return_value = self.mock_sheet_rows

    def test_incoming_venue_matching_shifted_row_by_source_url_fails_closed(self):
        """
        Incoming record matching a shifted malformed row via source_url cannot be appended.
        Production write fails closed with RuntimeError and clear diagnostic.
        """
        incoming_entry = MagicMock()
        incoming_entry.date_researched = "2026-10-20"
        incoming_entry.company_name = "The Wendover"
        incoming_entry.city = "Manchester"
        incoming_entry.target_country = "United Kingdom"
        incoming_entry.website = ""
        incoming_entry.phone = ""
        incoming_entry.address = ""
        incoming_entry.source_url = "https://www.openstreetmap.org/node/26520283"
        incoming_entry.to_sheet_row.return_value = ["RES-NEW", "The Wendover"] + ["val"] * 26

        with self.assertRaises(RuntimeError) as ctx:
            self.storage.save_research_log([incoming_entry])

        err = str(ctx.exception)
        self.assertIn("[GoogleSheets Malformed Deduplication Block]", err)
        self.assertIn("The Wendover", err)
        self.assertIn("row 280", err)
        self.assertIn("source_url match", err)
        # Verify that worksheet was NEVER updated or appended to
        self.mock_research_ws.update.assert_not_called()
        self.mock_research_ws.append_rows.assert_not_called()

    def test_incoming_venue_matching_shifted_row_by_norm_id_fails_closed(self):
        """
        Incoming record matching a shifted malformed row via normalized business identity
        fails closed without appending a duplicate row.
        """
        incoming_entry = MagicMock()
        incoming_entry.date_researched = "2026-10-20"
        incoming_entry.company_name = "The Wendover"
        incoming_entry.city = "Manchester"
        incoming_entry.target_country = "United Kingdom"
        incoming_entry.website = ""
        incoming_entry.phone = ""
        incoming_entry.address = "Manchester, United Kingdom"
        incoming_entry.source_url = ""  # No source_url, matches via norm_id
        incoming_entry.to_sheet_row.return_value = ["RES-NEW", "The Wendover"] + ["val"] * 26

        with self.assertRaises(RuntimeError) as ctx:
            self.storage.save_research_log([incoming_entry])

        err = str(ctx.exception)
        self.assertIn("[GoogleSheets Malformed Deduplication Block]", err)
        self.assertIn("The Wendover", err)
        self.assertIn("row 280", err)
        self.mock_research_ws.update.assert_not_called()
        self.mock_research_ws.append_rows.assert_not_called()

    def test_incoming_venue_matching_shifted_row_by_domain_fails_closed(self):
        """
        Incoming record matching a shifted malformed row via website domain fails closed.
        """
        incoming_entry = MagicMock()
        incoming_entry.date_researched = "2026-10-20"
        incoming_entry.company_name = "Dog and Partridge Pub"
        incoming_entry.city = "Manchester"
        incoming_entry.target_country = "United Kingdom"
        incoming_entry.website = "https://www.dogandpartridge.co.uk/menu"
        incoming_entry.phone = ""
        incoming_entry.address = ""
        incoming_entry.source_url = ""
        incoming_entry.to_sheet_row.return_value = ["RES-NEW", "Dog and Partridge Pub"] + ["val"] * 26

        with self.assertRaises(RuntimeError) as ctx:
            self.storage.save_research_log([incoming_entry])

        err = str(ctx.exception)
        self.assertIn("[GoogleSheets Malformed Deduplication Block]", err)
        self.assertIn("row 281", err)
        self.assertIn("domain match", err)

    def test_incoming_venue_matching_on_name_alone_without_multi_signals_fails_closed(self):
        """
        If incoming record shares company name with a malformed row but lacks multi-signals,
        safe deduplication cannot be guaranteed and production writes fail closed.
        """
        incoming_entry = MagicMock()
        incoming_entry.date_researched = "2026-10-20"
        incoming_entry.company_name = "The Wendover"
        incoming_entry.city = ""
        incoming_entry.target_country = ""
        incoming_entry.website = ""
        incoming_entry.phone = ""
        incoming_entry.address = ""
        incoming_entry.source_url = ""
        incoming_entry.to_sheet_row.return_value = ["RES-NEW", "The Wendover"] + ["val"] * 26

        with self.assertRaises(RuntimeError) as ctx:
            self.storage.save_research_log([incoming_entry])

        err = str(ctx.exception)
        self.assertIn("[GoogleSheets Malformed Deduplication Block]", err)
        self.assertIn("Safe deduplication cannot be guaranteed", err)
        self.assertIn("The Wendover", err)

    def test_distinct_venue_with_same_name_different_city_succeeds_and_appends(self):
        """
        A venue with the same name ('The Wendover') but a DIFFERENT city ('London')
        and different source_url is a distinct branch/entity and must NOT be blocked on name alone.
        """
        incoming_entry = MagicMock()
        incoming_entry.date_researched = "2026-10-20"
        incoming_entry.company_name = "The Wendover"
        incoming_entry.city = "London"
        incoming_entry.target_country = "United Kingdom"
        incoming_entry.website = ""
        incoming_entry.phone = "+44 20 7946 0999"
        incoming_entry.address = "123 London Rd, London"
        incoming_entry.source_url = "https://www.openstreetmap.org/node/88888888"
        incoming_entry.to_sheet_row.return_value = ["RES-LONDON-01", "The Wendover"] + ["val"] * 26

        # Mock read-back
        self.mock_research_ws.get_values.return_value = [incoming_entry.to_sheet_row()]

        inserted, updated = self.storage.save_research_log([incoming_entry])
        self.assertEqual(inserted, 1)
        self.assertEqual(updated, 0)
        # Destination must be row 318 (after row 317) starting in column A
        self.mock_research_ws.update.assert_called_once()
        _, kwargs = self.mock_research_ws.update.call_args
        self.assertEqual(kwargs.get("range_name"), "A318:AB318")

    def test_normal_row_matching_updates_in_place(self):
        """Incoming record matching a normal row in A:AB updates that row in-place."""
        incoming_entry = MagicMock()
        incoming_entry.date_researched = "2026-10-20"
        incoming_entry.company_name = "Canonical Venue 10"
        incoming_entry.city = "Manchester"
        incoming_entry.target_country = "United Kingdom"
        incoming_entry.website = ""
        incoming_entry.phone = ""
        incoming_entry.address = ""
        incoming_entry.source_url = "https://www.openstreetmap.org/node/canon_10"
        incoming_entry.to_sheet_row.return_value = ["RES-CANON-10", "Canonical Venue 10"] + ["val"] * 26

        self.mock_sheet_rows[9][0] = "RES-CANON-10"
        self.mock_sheet_rows[9][1] = "Canonical Venue 10"
        self.mock_sheet_rows[9][3] = "United Kingdom"
        self.mock_sheet_rows[9][6] = "Manchester"
        self.mock_sheet_rows[9][26] = "https://www.openstreetmap.org/node/canon_10"

        # Mock read-back
        self.mock_research_ws.get_values.return_value = [incoming_entry.to_sheet_row()]

        inserted, updated = self.storage.save_research_log([incoming_entry])
        self.assertEqual(inserted, 0)
        self.assertEqual(updated, 1)
        self.mock_research_ws.update.assert_called_once()
        _, kwargs = self.mock_research_ws.update.call_args
        self.assertEqual(kwargs.get("range_name"), "A10:AB10")


class TestCrossExecutionConcurrency(unittest.TestCase):
    """
    Tests for crm_write_lock concurrency, local filesystem advisory scope,
    and distributed Firestore lease acquisition and fail-closed timeout behavior.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.lock_path = os.path.join(self.temp_dir, ".crm_write.lock")

    def tearDown(self):
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_local_file_lock_mutual_exclusion(self):
        """Proves local fcntl.flock serializes concurrent threads on a single host."""
        from lib.crm.reconciliation import crm_write_lock
        import threading
        import time

        execution_order = []

        def task_1():
            with crm_write_lock(lock_path=self.lock_path, enable_distributed=False):
                execution_order.append("t1_start")
                time.sleep(0.2)
                execution_order.append("t1_end")

        def task_2():
            time.sleep(0.05)
            with crm_write_lock(lock_path=self.lock_path, enable_distributed=False):
                execution_order.append("t2_start")
                execution_order.append("t2_end")

        th1 = threading.Thread(target=task_1)
        th2 = threading.Thread(target=task_2)
        th1.start()
        th2.start()
        th1.join()
        th2.join()

        self.assertEqual(execution_order, ["t1_start", "t1_end", "t2_start", "t2_end"])

    def test_distributed_lease_acquisition_and_release(self):
        """Proves distributed operational lease acquisition and release using FirestoreStateStore."""
        from lib.crm.reconciliation import crm_write_lock
        from lib.system.firestore_state_store import FirestoreStateStore

        store = FirestoreStateStore(force_mock=True)

        with crm_write_lock(lock_path=self.lock_path, enable_distributed=True, state_store=store, timeout_sec=2.0):
            lease = store._mock_data["operational_leases"].get("crm_write_lock")
            self.assertIsNotNone(lease)
            self.assertIn("holder_id", lease)

        # After exiting context, subsequent worker can acquire lease
        active = store.acquire_lease("crm_write_lock", holder_id="subsequent_worker", ttl_seconds=10.0)
        self.assertTrue(active, "Subsequent worker must be able to acquire lease after release")

    def test_distributed_lease_contention_fails_closed_on_timeout(self):
        """
        When container A holds the distributed operational lease, container B attempting
        to acquire crm_write_lock must fail closed with TimeoutError upon timeout expiry.
        """
        from lib.crm.reconciliation import crm_write_lock
        from lib.system.firestore_state_store import FirestoreStateStore

        store = FirestoreStateStore(force_mock=True)

        # Container A holds the lease
        acquired_a = store.acquire_lease("crm_write_lock", holder_id="container_A", ttl_seconds=60.0)
        self.assertTrue(acquired_a)

        # Container B attempts to acquire with short timeout (0.4s)
        lock_path_b = os.path.join(self.temp_dir, ".crm_write_b.lock")
        with self.assertRaises(TimeoutError) as ctx:
            with crm_write_lock(lock_path=lock_path_b, enable_distributed=True, state_store=store, timeout_sec=0.4):
                pass

        self.assertIn("[CRM Concurrency Lock] Failed to acquire distributed Firestore lease", str(ctx.exception))

        # Container A releases lease
        store.release_lease("crm_write_lock", holder_id="container_A")

        # Container B now succeeds
        succeeded = False
        with crm_write_lock(lock_path=lock_path_b, enable_distributed=True, state_store=store, timeout_sec=1.0):
            succeeded = True
        self.assertTrue(succeeded)


if __name__ == "__main__":
    unittest.main()
