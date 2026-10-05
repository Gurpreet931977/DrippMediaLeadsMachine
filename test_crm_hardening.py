import unittest
from unittest.mock import MagicMock, patch
from lib.types import Lead, ResearchLogEntry, QualificationState, OutreachStatus
from lib.sheets.google_sheets import GoogleSheetsStorageProvider, normalize_outreach_status, LEADS_COLUMNS, REVIEW_QUEUE_COLUMNS, RESEARCH_LOG_COLUMNS
from lib.crm.identity_matcher import BusinessIdentityMatcher, IdentityMatchOutcome, IdentityMatchResult

class TestCRMHardening(unittest.TestCase):
    def setUp(self):
        self.provider = GoogleSheetsStorageProvider(sheet_url="https://docs.google.com/spreadsheets/d/dummy_sheet")
        self.mock_leads_ws = MagicMock()
        self.mock_review_ws = MagicMock()
        self.mock_research_ws = MagicMock()
        self.provider._leads_ws = self.mock_leads_ws
        self.provider._review_ws = self.mock_review_ws
        self.provider._research_ws = self.mock_research_ws

        # Base mock setup - return full headers so ensure_*_columns does not perform header updates
        self.mock_leads_ws.get_all_records.return_value = []
        self.mock_leads_ws.row_values.return_value = list(LEADS_COLUMNS)
        self.mock_leads_ws.col_count = len(LEADS_COLUMNS) + 10
        self.mock_review_ws.get_all_records.return_value = []
        self.mock_review_ws.row_values.return_value = list(REVIEW_QUEUE_COLUMNS)
        self.mock_review_ws.col_count = len(REVIEW_QUEUE_COLUMNS) + 10
        self.mock_research_ws.get_all_records.return_value = []
        self.mock_research_ws.row_values.return_value = list(RESEARCH_LOG_COLUMNS)
        self.mock_research_ws.col_count = len(RESEARCH_LOG_COLUMNS) + 10

    # 1. save_qualified_leads_rejects_non_outreach_ready
    def test_save_qualified_leads_rejects_non_outreach_ready(self):
        lead = Lead(
            lead_id="REV-TEST-001",
            company_name="Ambiguous Cafe",
            industry="Cafe",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            qualification_state=QualificationState.MANUAL_REVIEW.value
        )
        with self.assertRaises(ValueError) as ctx:
            self.provider.save_qualified_leads([lead])
        self.assertIn("save_qualified_leads received non-OUTREACH_READY lead", str(ctx.exception))
        self.assertIn("MANUAL_REVIEW", str(ctx.exception))
        self.mock_leads_ws.append_rows.assert_not_called()
        self.mock_leads_ws.update.assert_not_called()

    # 2. save_qualified_leads_rejects_research_only
    def test_save_qualified_leads_rejects_research_only(self):
        lead = Lead(
            lead_id="RES-TEST-002",
            company_name="Low Traction Bistro",
            industry="Restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            qualification_state=QualificationState.RESEARCH_ONLY.value
        )
        with self.assertRaises(ValueError) as ctx:
            self.provider.save_qualified_leads([lead])
        self.assertIn("save_qualified_leads received non-OUTREACH_READY lead", str(ctx.exception))
        self.assertIn("RESEARCH_ONLY", str(ctx.exception))
        self.mock_leads_ws.append_rows.assert_not_called()

    # 3. save_qualified_leads_rejects_excluded
    def test_save_qualified_leads_rejects_excluded(self):
        lead = Lead(
            lead_id="EXC-TEST-003",
            company_name="Has Website Eatery",
            industry="Restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            qualification_state=QualificationState.EXCLUDED.value
        )
        with self.assertRaises(ValueError) as ctx:
            self.provider.save_qualified_leads([lead])
        self.assertIn("save_qualified_leads received non-OUTREACH_READY lead", str(ctx.exception))
        self.assertIn("EXCLUDED", str(ctx.exception))
        self.mock_leads_ws.append_rows.assert_not_called()

    # 4. outreach_ready_still_inserts_normally
    def test_outreach_ready_still_inserts_normally(self):
        lead = Lead(
            lead_id="LEAD-TEST-004",
            company_name="Premier Korean Grill",
            industry="Restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            qualification_state=QualificationState.OUTREACH_READY.value
        )
        inserted, updated = self.provider.save_qualified_leads([lead])
        self.assertEqual(inserted, 1)
        self.assertEqual(updated, 0)
        self.mock_leads_ws.append_rows.assert_called_once()
        rows_added = self.mock_leads_ws.append_rows.call_args[0][0]
        self.assertEqual(len(rows_added), 1)

    # 5. existing_outreach_ready_updates_canonical
    def test_existing_outreach_ready_updates_canonical(self):
        existing_row = {
            "lead_id": "LEAD-CANON-005",
            "company_name": "Premier Korean Grill",
            "city": "Manchester",
            "address": "123 Oxford Rd, Manchester M1 5AN",
            "phone": "+44 161 111 2222",
            "qualification_state": "OUTREACH_READY",
            "outreach_status": "SENT",
            "outreach_sent_at": "2026-09-28T10:00:00Z"
        }
        self.mock_leads_ws.get_all_records.return_value = [existing_row]

        lead = Lead(
            lead_id="LEAD-NEW-TMP",
            company_name="Premier Korean Grill",
            industry="Restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            address="123 Oxford Rd, Manchester M1 5AN",
            phone="+44 161 111 2222",
            qualification_state=QualificationState.OUTREACH_READY.value
        )
        inserted, updated = self.provider.save_qualified_leads([lead])
        self.assertEqual(inserted, 0)
        self.assertEqual(updated, 1)
        self.mock_leads_ws.append_rows.assert_not_called()
        self.mock_leads_ws.update.assert_called_once()
        self.assertEqual(lead.lead_id, "LEAD-CANON-005")
        self.assertEqual(lead.outreach_status, "SENT")

    # 6. blank_outreach_status_normalizes_to_not_ready
    def test_blank_outreach_status_normalizes_to_not_ready(self):
        self.assertEqual(normalize_outreach_status(None), OutreachStatus.NOT_READY.value)
        self.assertEqual(normalize_outreach_status(""), OutreachStatus.NOT_READY.value)
        self.assertEqual(normalize_outreach_status("   "), OutreachStatus.NOT_READY.value)
        self.assertEqual(normalize_outreach_status("\t\n"), OutreachStatus.NOT_READY.value)
        self.assertEqual(normalize_outreach_status("SENT"), "SENT")
        self.assertEqual(normalize_outreach_status("BOUNCED"), "BOUNCED")
        self.assertEqual(normalize_outreach_status("FAILED"), "FAILED")
        self.assertEqual(normalize_outreach_status("READY_FOR_REVIEW"), "READY_FOR_REVIEW")

    # 7. qualification_state_never_changes_from_outreach_normalization
    def test_qualification_state_never_changes_from_outreach_normalization(self):
        lead = Lead(
            lead_id="LEAD-TEST-007",
            company_name="Shawarma Corner",
            industry="Restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            qualification_state=QualificationState.OUTREACH_READY.value,
            outreach_status=""
        )
        # Verify __post_init__ or serialization
        self.assertEqual(lead.qualification_state, QualificationState.OUTREACH_READY.value)
        self.assertEqual(lead.outreach_status, OutreachStatus.NOT_READY.value)

        inserted, updated = self.provider.save_qualified_leads([lead])
        self.assertEqual(lead.qualification_state, QualificationState.OUTREACH_READY.value)
        self.assertEqual(lead.outreach_status, OutreachStatus.NOT_READY.value)

    # 8. bounce_does_not_change_qualification
    def test_bounce_does_not_change_qualification(self):
        lead = Lead(
            lead_id="LEAD-TEST-008",
            company_name="Bounced Eatery",
            industry="Restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            qualification_state=QualificationState.OUTREACH_READY.value,
            outreach_status=OutreachStatus.BOUNCED.value
        )
        self.assertEqual(lead.qualification_state, QualificationState.OUTREACH_READY.value)
        self.assertEqual(lead.outreach_status, "BOUNCED")

        inserted, updated = self.provider.save_qualified_leads([lead])
        self.assertEqual(lead.qualification_state, QualificationState.OUTREACH_READY.value)
        self.assertEqual(lead.outreach_status, "BOUNCED")

    # 9. failed_outreach_does_not_change_qualification
    def test_failed_outreach_does_not_change_qualification(self):
        lead = Lead(
            lead_id="LEAD-TEST-009",
            company_name="Failed Delivery Kitchen",
            industry="Restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            qualification_state=QualificationState.OUTREACH_READY.value,
            outreach_status=OutreachStatus.FAILED.value
        )
        self.assertEqual(lead.qualification_state, QualificationState.OUTREACH_READY.value)
        self.assertEqual(lead.outreach_status, "FAILED")

        inserted, updated = self.provider.save_qualified_leads([lead])
        self.assertEqual(lead.qualification_state, QualificationState.OUTREACH_READY.value)
        self.assertEqual(lead.outreach_status, "FAILED")

    # 10. sent_and_replied_states_preserved
    def test_sent_and_replied_states_preserved(self):
        existing_row = {
            "lead_id": "LEAD-KIMCHI-010",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "address": "275 Upper Brook St, Manchester M13 0HR",
            "phone": "+44 7745 527603",
            "qualification_state": "OUTREACH_READY",
            "outreach_status": "SENT",
            "response_status": "REPLIED",
            "sales_stage": "QUALIFIED_CONVERSATION"
        }
        self.mock_leads_ws.get_all_records.return_value = [existing_row]

        lead = Lead(
            lead_id="LEAD-KIMCHI-010",
            company_name="Seoul Kimchi",
            industry="Korean restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            address="275 Upper Brook St, Manchester M13 0HR",
            phone="+44 7745 527603",
            qualification_state=QualificationState.OUTREACH_READY.value
        )
        inserted, updated = self.provider.save_qualified_leads([lead])
        self.assertEqual(inserted, 0)
        self.assertEqual(updated, 1)
        self.assertEqual(lead.qualification_state, "OUTREACH_READY")
        self.assertEqual(lead.outreach_status, "SENT")
        self.assertEqual(lead.response_status, "REPLIED")
        self.assertEqual(lead.sales_stage, "QUALIFIED_CONVERSATION")

    # 11. deduplication_behavior_unchanged
    def test_deduplication_behavior_unchanged(self):
        existing_rows = [{
            "lead_id": "LEAD-CANON-100",
            "company_name": "Anchor Restaurant",
            "city": "Manchester",
            "address": "100 Deansgate, Manchester M3 2QG",
            "phone": "+44 161 888 9999",
            "qualification_state": "OUTREACH_READY",
            "outreach_status": "NOT_READY"
        }]
        self.mock_leads_ws.get_all_records.return_value = existing_rows

        # Case A: EXACT_DUPLICATE -> updates canonical
        exact_dup = Lead(
            lead_id="LEAD-NEW-200",
            company_name="Anchor Restaurant",
            industry="Restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            address="100 Deansgate, Manchester M3 2QG",
            phone="+44 161 888 9999",
            qualification_state=QualificationState.OUTREACH_READY.value
        )
        ins, upd = self.provider.save_qualified_leads([exact_dup])
        self.assertEqual(ins, 0)
        self.assertEqual(upd, 1)
        self.assertEqual(exact_dup.lead_id, "LEAD-CANON-100")

        # Case B: POSSIBLE_DUPLICATE -> routes to REVIEW_QUEUE
        self.mock_leads_ws.update.reset_mock()
        self.mock_leads_ws.append_rows.reset_mock()
        self.mock_review_ws.append_rows.reset_mock()
        pos_dup = Lead(
            lead_id="LEAD-NEW-201",
            company_name="Anchor Rest",
            industry="Restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            qualification_state=QualificationState.OUTREACH_READY.value
        )
        # Mock matcher to return POSSIBLE_DUPLICATE
        with patch.object(self.provider.matcher, "match_candidate") as mock_match:
            mock_match.return_value = IdentityMatchResult(
                outcome=IdentityMatchOutcome.POSSIBLE_DUPLICATE.value,
                confidence=0.72,
                match_reasons=["Name similarity"],
                matched_lead_id="LEAD-CANON-100"
            )
            ins, upd = self.provider.save_qualified_leads([pos_dup])
            self.assertEqual(ins, 0)
            self.assertEqual(upd, 0)
            self.mock_leads_ws.append_rows.assert_not_called()
            self.mock_review_ws.append_rows.assert_called_once()

        # Case C: CONFLICT -> routes to REVIEW_QUEUE
        self.mock_review_ws.append_rows.reset_mock()
        conflict_lead = Lead(
            lead_id="LEAD-NEW-202",
            company_name="Different Place",
            industry="Restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Manchester",
            phone="+44 161 888 9999",
            qualification_state=QualificationState.OUTREACH_READY.value
        )
        with patch.object(self.provider.matcher, "match_candidate") as mock_match:
            mock_match.return_value = IdentityMatchResult(
                outcome=IdentityMatchOutcome.CONFLICT.value,
                confidence=1.0,
                conflict_notes="Same phone used by different name"
            )
            ins, upd = self.provider.save_qualified_leads([conflict_lead])
            self.assertEqual(ins, 0)
            self.assertEqual(upd, 0)
            self.mock_leads_ws.append_rows.assert_not_called()
            self.mock_review_ws.append_rows.assert_called_once()

        # Case D: DISTINCT_BRANCH (different city/address) -> NEW_BUSINESS inserted
        self.mock_leads_ws.append_rows.reset_mock()
        leeds_branch = Lead(
            lead_id="LEAD-NEW-203",
            company_name="Anchor Restaurant",
            industry="Restaurant",
            target_country="United Kingdom",
            country="United Kingdom",
            city="Leeds",
            address="50 Briggate, Leeds LS1 6HD",
            phone="+44 113 777 8888",
            qualification_state=QualificationState.OUTREACH_READY.value
        )
        with patch.object(self.provider.matcher, "match_candidate") as mock_match:
            mock_match.return_value = IdentityMatchResult(
                outcome=IdentityMatchOutcome.NEW_BUSINESS.value,
                confidence=0.0
            )
            ins, upd = self.provider.save_qualified_leads([leeds_branch])
            self.assertEqual(ins, 1)
            self.assertEqual(upd, 0)
            self.mock_leads_ws.append_rows.assert_called_once()

    # 12. research_log_preserves_non_qualified_states
    def test_research_log_preserves_non_qualified_states(self):
        entries = [
            ResearchLogEntry(
                research_id="RES-001",
                company_name="Manual Review Place",
                industry="Cafe",
                target_country="United Kingdom",
                detected_country="United Kingdom",
                country_status="COUNTRY_MATCH",
                city="Manchester",
                qualification_state=QualificationState.MANUAL_REVIEW.value,
                qualification_status="MANUAL_REVIEW"
            ),
            ResearchLogEntry(
                research_id="RES-002",
                company_name="Research Only Place",
                industry="Cafe",
                target_country="United Kingdom",
                detected_country="United Kingdom",
                country_status="COUNTRY_MATCH",
                city="Manchester",
                qualification_state=QualificationState.RESEARCH_ONLY.value,
                qualification_status="RESEARCH_ONLY"
            ),
            ResearchLogEntry(
                research_id="RES-003",
                company_name="Excluded Place",
                industry="Cafe",
                target_country="United Kingdom",
                detected_country="United Kingdom",
                country_status="COUNTRY_MATCH",
                city="Manchester",
                qualification_state=QualificationState.EXCLUDED.value,
                qualification_status="EXCLUDED"
            )
        ]
        inserted, updated = self.provider.save_research_log(entries)
        self.assertEqual(inserted, 3)
        self.assertEqual(updated, 0)
        self.mock_research_ws.append_rows.assert_called_once()
        rows_added = self.mock_research_ws.append_rows.call_args[0][0]
        self.assertEqual(len(rows_added), 3)

if __name__ == "__main__":
    unittest.main()
