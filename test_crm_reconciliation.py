"""
Dripp Media — CRM Reconciliation & Deduplication Test Suite
============================================================
Comprehensive tests verifying all 13 reconciliation & ingestion invariants:
  1. Exact duplicate with same lead_id
  2. Exact duplicate with different lead_id
  3. Same business formatting variation
  4. Same business new enrichment
  5. Same name different branch
  6. Same name different city
  7. Simultaneous duplicate insert (concurrency & race protection)
  8. Possible duplicate (routed to REVIEW_QUEUE, not LEADS)
  9. Conflict (routed to REVIEW_QUEUE, not LEADS)
  10. Google Sheets dry-run reconciliation (0 live mutations)
  11. Google Sheets post-write duplicate prevention
  12. Canonical lead preservation (earliest timestamp, original lead_id)
  13. Outreach history preservation (response status, sales stage, notes)
"""

import unittest
from unittest.mock import MagicMock, patch
import threading
import time

from lib.types import Lead
from lib.crm.identity_matcher import (
    BusinessIdentityMatcher,
    IdentityMatchOutcome
)
from lib.crm.reconciliation import (
    CRMReconciler,
    ReconciliationCategory,
    ReconciliationAction,
    crm_write_lock
)
from lib.sheets.google_sheets import (
    GoogleSheetsStorageProvider,
    LEADS_COLUMNS,
    REVIEW_QUEUE_COLUMNS
)


class TestCRMReconciliation(unittest.TestCase):

    def setUp(self):
        self.matcher = BusinessIdentityMatcher()
        self.reconciler = CRMReconciler(self.matcher)

    def test_01_exact_duplicate_same_lead_id(self):
        """Exact duplicate with identical lead_id is classified as EXACT_DUPLICATE and recommended for merge."""
        row_5 = {
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "address": "275 Upper Brook St, Manchester M13 0HR, United Kingdom",
            "phone": "+44 7745 527603",
            "date_added": "2026-09-27",
            "outreach_status": "SENT"
        }
        row_7 = {
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "address": "275 Upper Brook St, Manchester M13 0HR, United Kingdom",
            "phone": "+44 7745 527603",
            "date_added": "2026-09-27",
            "outreach_status": "SENT"
        }

        candidates = self.reconciler.audit_rows([row_5, row_7])
        self.assertEqual(len(candidates), 1)
        cand = candidates[0]

        self.assertEqual(cand.category, ReconciliationCategory.EXACT_DUPLICATE.value)
        self.assertEqual(cand.recommended_action, ReconciliationAction.MERGE_TO_CANONICAL.value)
        self.assertEqual(cand.canonical_lead_id, "LEAD-MAN-4DB3EF")
        self.assertEqual(cand.duplicate_lead_id, "LEAD-MAN-4DB3EF")
        self.assertEqual(cand.canonical_row, 2)  # 1-based index (Header=1, first record=2)
        self.assertEqual(cand.duplicate_row, 3)
        self.assertTrue(cand.safe_to_remove_duplicate)

    def test_02_exact_duplicate_different_lead_id(self):
        """High-confidence match with different lead IDs preserves canonical lead ID."""
        row_a = {
            "lead_id": "LEAD-ORIG-100",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "address": "275 Upper Brook St",
            "postcode": "M13 0HR",
            "phone": "+44 7745 527603",
            "date_added": "2026-09-25",
            "outreach_status": "SENT"
        }
        row_b = {
            "lead_id": "LEAD-TMP-999",
            "company_name": "Seoul Kimchi Ltd",
            "city": "Manchester",
            "country": "United Kingdom",
            "address": "275 Upper Brook Street",
            "postcode": "M130HR",
            "phone": "07745 527603",
            "date_added": "2026-09-30",
            "outreach_status": "NOT_READY"
        }

        candidates = self.reconciler.audit_rows([row_a, row_b])
        self.assertEqual(len(candidates), 1)
        cand = candidates[0]

        self.assertEqual(cand.category, ReconciliationCategory.SAME_BUSINESS_DIFFERENT_LEAD_ID.value)
        self.assertEqual(cand.recommended_action, ReconciliationAction.MERGE_TO_CANONICAL_PRESERVE_PRIMARY_ID.value)
        self.assertEqual(cand.canonical_lead_id, "LEAD-ORIG-100")
        self.assertEqual(cand.duplicate_lead_id, "LEAD-TMP-999")
        self.assertEqual(cand.merged_record["lead_id"], "LEAD-ORIG-100", "Must never create or switch to a new lead_id")
        self.assertTrue(cand.safe_to_remove_duplicate)

    def test_03_same_business_formatting_variation(self):
        """Cross-formatting variations correctly resolve to EXISTING_BUSINESS."""
        crm = [{
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "address": "275 Upper Brook St",
            "postcode": "M13 0HR",
            "phone": "+44 7745 527603"
        }]
        candidate = {
            "company_name": "SEOUL-KIMCHI",
            "city": "Manchester",
            "country": "United Kingdom",
            "address": "275 Upper Brook Street",
            "postcode": "M130HR",
            "phone": "07745 527603"
        }
        match = self.matcher.match_candidate(candidate, crm)
        self.assertEqual(match.outcome, IdentityMatchOutcome.EXISTING_BUSINESS.value)
        self.assertEqual(match.matched_lead_id, "LEAD-MAN-4DB3EF")

    def test_04_same_business_new_enrichment(self):
        """Newly discovered enrichment updates the canonical record in-place without creating a new row."""
        provider = GoogleSheetsStorageProvider(sheet_url="https://docs.google.com/spreadsheets/d/dummy")
        mock_ws = MagicMock()
        provider._leads_ws = mock_ws
        provider.ensure_leads_columns = MagicMock()

        existing_row = {
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "275 Upper Brook St",
            "postcode": "M13 0HR",
            "phone": "+44 7745 527603",
            "review_count": "",
            "rating": "",
            "outreach_status": "SENT"
        }
        mock_ws.get_all_records.return_value = [existing_row]

        new_candidate = Lead(
            lead_id="LEAD-NEW-TEMP",
            company_name="Seoul Kimchi",
            industry="Restaurants",
            city="Manchester",
            target_country="United Kingdom",
            country="United Kingdom",
            address="275 Upper Brook St",
            postcode="M13 0HR",
            phone="+44 7745 527603",
            review_count=185,
            rating=4.7,
            qualification_state="OUTREACH_READY"
        )

        inserted, updated = provider.save_qualified_leads([new_candidate])
        self.assertEqual(inserted, 0, "Zero new rows inserted")
        self.assertEqual(updated, 1, "Canonical record updated in-place")
        self.assertEqual(new_candidate.lead_id, "LEAD-MAN-4DB3EF", "Preserved canonical lead_id")
        self.assertEqual(new_candidate.review_count, 185, "Enriched review count retained")
        self.assertEqual(new_candidate.rating, 4.7, "Enriched rating retained")
        mock_ws.append_rows.assert_not_called()
        mock_ws.update.assert_called_once()

    def test_05_same_name_different_branch(self):
        """Same business name on different streets within same city is recognized as DISTINCT_BRANCH."""
        branch_a = {
            "lead_id": "LEAD-SUB-001",
            "company_name": "Subway",
            "city": "Leeds",
            "country": "United Kingdom",
            "address": "12 Briggate",
            "postcode": "LS1 6ER"
        }
        branch_b = {
            "lead_id": "LEAD-SUB-002",
            "company_name": "Subway",
            "city": "Leeds",
            "country": "United Kingdom",
            "address": "450 Headingley Lane",
            "postcode": "LS6 2BX"
        }

        candidates = self.reconciler.audit_rows([branch_a, branch_b])
        self.assertEqual(len(candidates), 1)
        cand = candidates[0]
        self.assertEqual(cand.category, ReconciliationCategory.DISTINCT_BRANCH.value)
        self.assertEqual(cand.recommended_action, ReconciliationAction.PRESERVE_SEPARATE_RECORDS.value)
        self.assertFalse(cand.safe_to_remove_duplicate, "Branches must NOT be removed or merged")

    def test_06_same_name_different_city(self):
        """Same business name in different cities is recognized as DISTINCT_BRANCH."""
        branch_manchester = {
            "lead_id": "LEAD-SEO-001",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "address": "275 Upper Brook St",
            "postcode": "M13 0HR"
        }
        branch_leeds = {
            "lead_id": "LEAD-SEO-002",
            "company_name": "Seoul Kimchi",
            "city": "Leeds",
            "country": "United Kingdom",
            "address": "Vicar Lane",
            "postcode": "LS1 6JL"
        }

        candidates = self.reconciler.audit_rows([branch_manchester, branch_leeds])
        self.assertEqual(len(candidates), 1)
        cand = candidates[0]
        self.assertEqual(cand.category, ReconciliationCategory.DISTINCT_BRANCH.value)
        self.assertEqual(cand.recommended_action, ReconciliationAction.PRESERVE_SEPARATE_RECORDS.value)
        self.assertFalse(cand.safe_to_remove_duplicate)

    def test_07_simultaneous_duplicate_insert(self):
        """Two simultaneous pipeline runs discovering the same business are serialized by crm_write_lock."""
        provider = GoogleSheetsStorageProvider(sheet_url="https://docs.google.com/spreadsheets/d/dummy")
        mock_ws = MagicMock()
        provider._leads_ws = mock_ws
        provider.ensure_leads_columns = MagicMock()

        shared_sheet_records = []

        def mock_get_all_records():
            return list(shared_sheet_records)

        def mock_append_rows(rows, value_input_option=None):
            for r in rows:
                shared_sheet_records.append({col: r[i] for i, col in enumerate(LEADS_COLUMNS) if i < len(r)})

        def mock_update(values, range_name=None, value_input_option=None):
            # Parse row index from range e.g. "A2:AN2"
            row_idx = int(range_name[1:].split(":")[0])
            for i, col in enumerate(LEADS_COLUMNS):
                if i < len(values[0]):
                    shared_sheet_records[row_idx - 2][col] = values[0][i]

        mock_ws.get_all_records.side_effect = mock_get_all_records
        mock_ws.append_rows.side_effect = mock_append_rows
        mock_ws.update.side_effect = mock_update

        # Candidate discovered by two concurrent threads
        lead_run_a = Lead(
            lead_id="LEAD-RUN-A",
            company_name="Seoul Kimchi",
            industry="Restaurants",
            city="Manchester",
            target_country="United Kingdom",
            country="United Kingdom",
            address="275 Upper Brook St",
            postcode="M13 0HR",
            phone="+44 7745 527603"
        )
        lead_run_b = Lead(
            lead_id="LEAD-RUN-B",
            company_name="Seoul Kimchi Ltd",
            industry="Restaurants",
            city="Manchester",
            target_country="United Kingdom",
            country="United Kingdom",
            address="275 Upper Brook St",
            postcode="M13 0HR",
            phone="+44 7745 527603",
            review_count=190
        )

        results = []

        def worker(lead_item):
            ins, upd = provider.save_qualified_leads([lead_item])
            results.append((ins, upd))

        thread_a = threading.Thread(target=worker, args=(lead_run_a,))
        thread_b = threading.Thread(target=worker, args=(lead_run_b,))

        thread_a.start()
        thread_b.start()
        thread_a.join()
        thread_b.join()

        # Invariant: exactly 1 inserted, 1 updated, exactly 1 row in sheet
        total_inserted = sum(r[0] for r in results)
        total_updated = sum(r[1] for r in results)
        self.assertEqual(total_inserted, 1, "Only 1 insert allowed across concurrent threads")
        self.assertEqual(total_updated, 1, "Second run updates the existing row in-place")
        self.assertEqual(len(shared_sheet_records), 1, "Exactly 1 record in sheet: ONE BUSINESS IDENTITY = ONE LEADS RECORD")

    def test_08_possible_duplicate_routed_to_review_queue(self):
        """Ambiguous duplicate candidates are routed strictly to REVIEW_QUEUE, preventing insertion into LEADS."""
        provider = GoogleSheetsStorageProvider(sheet_url="https://docs.google.com/spreadsheets/d/dummy")
        mock_leads_ws = MagicMock()
        mock_review_ws = MagicMock()
        provider._leads_ws = mock_leads_ws
        provider._review_ws = mock_review_ws
        provider.ensure_leads_columns = MagicMock()
        provider.ensure_review_queue_columns = MagicMock()

        # Existing lead
        existing = {
            "lead_id": "LEAD-ORIG-555",
            "company_name": "Kimchi Restaurant",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "Upper Brook St",
            "postcode": "M13 0HR"
        }
        mock_leads_ws.get_all_records.return_value = [existing]
        mock_review_ws.get_all_records.return_value = []

        # Candidate with ambiguous similarity
        ambiguous_cand = Lead(
            lead_id="LEAD-AMB-001",
            company_name="Kimchi House",
            industry="Restaurants",
            city="Manchester",
            target_country="United Kingdom",
            country="United Kingdom",
            address="Upper Brook St",
            postcode="M13 0HR"
        )

        with patch.object(provider.matcher, "match_candidate") as mock_match:
            mock_match.return_value = MagicMock(
                outcome=IdentityMatchOutcome.POSSIBLE_DUPLICATE.value,
                matched_lead_id="LEAD-ORIG-555",
                confidence=0.72,
                match_reasons=["DISTINCTIVE_TOKEN_OVERLAP_HIGH", "STREET_NAME_TOKEN_MATCH"]
            )
            inserted, updated = provider.save_qualified_leads([ambiguous_cand])

        # Verifications: NOT inserted into LEADS
        self.assertEqual(inserted, 0)
        self.assertEqual(updated, 0)
        mock_leads_ws.append_rows.assert_not_called()
        # Successfully saved to REVIEW_QUEUE
        mock_review_ws.append_rows.assert_called_once()
        self.assertIn("POSSIBLE_DUPLICATE", ambiguous_cand.notes)

    def test_09_conflict_routed_to_review_queue(self):
        """Entity conflict (e.g. same phone used by different brand) routes strictly to REVIEW_QUEUE."""
        provider = GoogleSheetsStorageProvider(sheet_url="https://docs.google.com/spreadsheets/d/dummy")
        mock_leads_ws = MagicMock()
        mock_review_ws = MagicMock()
        provider._leads_ws = mock_leads_ws
        provider._review_ws = mock_review_ws
        provider.ensure_leads_columns = MagicMock()
        provider.ensure_review_queue_columns = MagicMock()

        existing = {
            "lead_id": "LEAD-ORIG-777",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "phone": "+44 7745 527603"
        }
        mock_leads_ws.get_all_records.return_value = [existing]
        mock_review_ws.get_all_records.return_value = []

        conflict_cand = Lead(
            lead_id="LEAD-CONF-001",
            company_name="Dental Excellence Clinic",
            industry="Healthcare",
            city="Manchester",
            target_country="United Kingdom",
            country="United Kingdom",
            phone="+44 7745 527603"
        )

        with patch.object(provider.matcher, "match_candidate") as mock_match:
            mock_match.return_value = MagicMock(
                outcome=IdentityMatchOutcome.CONFLICT.value,
                matched_lead_id="LEAD-ORIG-777",
                confidence=0.55,
                conflict_notes="Same telephone number used by distinct business names."
            )
            inserted, updated = provider.save_qualified_leads([conflict_cand])

        self.assertEqual(inserted, 0)
        self.assertEqual(updated, 0)
        mock_leads_ws.append_rows.assert_not_called()
        mock_review_ws.append_rows.assert_called_once()
        self.assertIn("IDENTITY_CONFLICT", conflict_cand.red_flags)

    def test_10_google_sheets_dry_run_reconciliation(self):
        """Reconciliation in DRY RUN mode generates plan without mutating or deleting rows."""
        mock_ws = MagicMock()
        row_5 = {
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "country": "United Kingdom",
            "address": "275 Upper Brook St, Manchester M13 0HR, United Kingdom",
            "phone": "+44 7745 527603",
            "date_added": "2026-09-27",
            "outreach_status": "SENT"
        }
        row_7 = dict(row_5)

        candidates = self.reconciler.audit_rows([row_5, row_7])
        res = self.reconciler.apply_reconciliation(mock_ws, candidates, LEADS_COLUMNS, dry_run=True)

        self.assertEqual(res["status"], "DRY_RUN")
        self.assertEqual(res["actionable_candidates"], 1)
        mock_ws.update.assert_not_called()
        mock_ws.delete_rows.assert_not_called()

    def test_11_google_sheets_post_write_duplicate_prevention(self):
        """Post-write duplicate prevention ensures subsequent writes update in-place."""
        provider = GoogleSheetsStorageProvider(sheet_url="https://docs.google.com/spreadsheets/d/dummy")
        mock_ws = MagicMock()
        provider._leads_ws = mock_ws
        provider.ensure_leads_columns = MagicMock()

        # Simulate sheet already having the lead
        mock_ws.get_all_records.return_value = [{
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "target_country": "United Kingdom",
            "address": "275 Upper Brook St",
            "postcode": "M13 0HR",
            "phone": "+44 7745 527603"
        }]

        lead_repeat = Lead(
            lead_id="LEAD-MAN-4DB3EF",
            company_name="Seoul Kimchi",
            industry="Restaurants",
            city="Manchester",
            target_country="United Kingdom",
            country="United Kingdom",
            address="275 Upper Brook St",
            postcode="M13 0HR",
            phone="+44 7745 527603"
        )

        ins, upd = provider.save_qualified_leads([lead_repeat])
        self.assertEqual(ins, 0)
        self.assertEqual(upd, 1)
        mock_ws.append_rows.assert_not_called()
        mock_ws.update.assert_called_once()

    def test_12_canonical_lead_preservation(self):
        """Deterministic policy selects earliest timestamp and never creates a new lead_id."""
        early_rec = {
            "lead_id": "LEAD-EARLY-001",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "date_added": "2026-09-20",
            "outreach_status": "SENT"
        }
        late_rec = {
            "lead_id": "LEAD-LATE-002",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "date_added": "2026-09-28",
            "outreach_status": "SENT"
        }

        canonical, duplicate, can_row, dup_row = self.reconciler.select_canonical_record(
            early_rec, late_rec, row_idx_a=2, row_idx_b=3
        )
        self.assertEqual(canonical["lead_id"], "LEAD-EARLY-001")
        self.assertEqual(duplicate["lead_id"], "LEAD-LATE-002")

        merged = self.reconciler.merge_records(canonical, duplicate)
        self.assertEqual(merged["lead_id"], "LEAD-EARLY-001", "Must preserve canonical lead_id")

    def test_13_outreach_history_preservation(self):
        """Outreach history, response status, and enrichment evidence are fully preserved upon merge."""
        canonical = {
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "outreach_status": "SENT",
            "outreach_sent_at": "2026-09-28T10:00:00Z",
            "creator_evidence_status": "FOUND",
            "creator_evidence_count": 2,
            "response_status": "AWAITING_RESPONSE",
            "manual_outreach_notes": "First touch via IG DM"
        }
        duplicate = {
            "lead_id": "LEAD-MAN-4DB3EF",
            "company_name": "Seoul Kimchi",
            "city": "Manchester",
            "outreach_status": "SENT",
            "creator_evidence_status": "",
            "response_status": "REPLIED",
            "response_type": "INTERESTED",
            "sales_stage": "QUALIFIED_CONVERSATION",
            "manual_outreach_notes": "Owner replied interested in food reels. Call booked for Tuesday.",
            "follow_up_notes": "Send reel deck"
        }

        merged = self.reconciler.merge_records(canonical, duplicate)

        # Invariants
        self.assertEqual(merged["lead_id"], "LEAD-MAN-4DB3EF")
        self.assertEqual(merged["creator_evidence_status"], "FOUND", "Creator evidence preserved")
        self.assertEqual(merged["creator_evidence_count"], 2)
        self.assertEqual(merged["response_status"], "REPLIED", "Response progression advanced")
        self.assertEqual(merged["response_type"], "INTERESTED")
        self.assertEqual(merged["sales_stage"], "QUALIFIED_CONVERSATION")
        self.assertIn("First touch via IG DM", merged["manual_outreach_notes"])
        self.assertIn("Owner replied interested", merged["manual_outreach_notes"])
        self.assertEqual(merged["follow_up_notes"], "Send reel deck")


if __name__ == "__main__":
    unittest.main()
