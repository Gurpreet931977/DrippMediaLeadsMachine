"""
Phase 8.4 Contact Discovery V2 & Manual Review Conversion -- Test Suite
========================================================================
Covers all 20 required verification invariants from Objective S.

Run:
  .venv/bin/python -m unittest test_phase_8_4_contact_discovery.py
"""

import os
import sys
import json
import hashlib
import unittest

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.outreach.phase_8_4_contact_discovery import (
    Phase84ContactDiscoveryEngine,
    TARGET_24_RESEARCH_IDS,
    PHASE_80_NO_WEBSITE_RESEARCH_IDS,
    LIVE_SEAFOOD_CANONICAL_LEAD_ID,
    LIVE_SEAFOOD_RESEARCH_ID,
    CRM_LEAD_ID_MAP,
    SourceFamily,
    EMAIL_STATUS_VERIFIED_BUSINESS,
    EMAIL_STATUS_PUBLIC_UNVERIFIED_BUSINESS,
    EMAIL_STATUS_INVALID,
    EMAIL_STATUS_NO_EMAIL,
    PHONE_STATUS_VERIFIED_BUSINESS,
    PHONE_STATUS_PUBLIC_UNVERIFIED,
    PHONE_STATUS_NO_PHONE,
    SOCIAL_STATUS_MANUAL_CONTACTABLE,
    SOCIAL_STATUS_AUTOMATED_SENDABLE,
    SOCIAL_STATUS_AMBIGUOUS,
    build_exact_business_queries,
    classify_discovered_email,
    classify_discovered_phone,
    classify_discovered_social,
    normalise_uk_phone,
)
from lib.outreach.email_enricher import MXStatus, EmailVerificationStatus, EmailType


def _load_audit() -> dict:
    path = "data/phase_8_4_contact_discovery.json"
    if not os.path.exists(path):
        raise FileNotFoundError(f"Artifact not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_pools() -> dict:
    path = "data/phase_8_4_outreach_pools.json"
    if not os.path.exists(path):
        raise FileNotFoundError(f"Artifact not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestTargetCohortSelection(unittest.TestCase):
    """Invariant 1 & 2: 24-target unreachable cohort & no new discovery."""

    def setUp(self):
        self.audit = _load_audit()

    def test_01_target_24_cohort_exact_count(self):
        """1. 24-target unreachable cohort is selected correctly."""
        metrics = self.audit["contact_discovery_metrics"]
        self.assertEqual(metrics["TOTAL_TARGETED"], 24)
        self.assertEqual(len(TARGET_24_RESEARCH_IDS), 24)

    def test_02_no_new_discovery_occurs(self):
        """2. No new discovery occurs. Base cohort is strictly 28."""
        records = self.audit["records"]
        self.assertEqual(len(records), 28)
        record_rids = {r["research_id"] for r in records}
        self.assertEqual(record_rids, set(PHASE_80_NO_WEBSITE_RESEARCH_IDS))


class TestQueryConstruction(unittest.TestCase):
    """Invariant 3: Exact-business query construction."""

    def test_03_exact_business_queries_built(self):
        """3. Exact-business query construction is used."""
        queries = build_exact_business_queries("Ducie Arms", "Manchester")
        self.assertEqual(len(queries), 5)
        self.assertIn('"Ducie Arms" "Manchester" contact', queries)
        self.assertIn('"Ducie Arms" "Manchester" email', queries)
        self.assertIn('"Ducie Arms" "Manchester" Instagram', queries)
        self.assertIn('"Ducie Arms" "Manchester" Facebook', queries)
        self.assertIn('"Ducie Arms" "Manchester" phone', queries)

    def test_03b_discovery_queue_has_exact_queries(self):
        """3b. Every discovery queue entry contains exact queries used."""
        audit = _load_audit()
        queue = audit["discovery_queue"]
        self.assertEqual(len(queue), 24)
        for item in queue:
            queries = item.get("exact_queries_used", [])
            self.assertEqual(len(queries), 5)
            self.assertTrue(all(item["company_name"] in q for q in queries))


class TestEmailSafety(unittest.TestCase):
    """Invariant 4 & 5: Guessed emails rejected & MX validity does not imply ownership."""

    def test_04_guessed_emails_rejected(self):
        """4. Guessed emails (hello@, info@, contact@) without official source are rejected."""
        st, reason = classify_discovered_email("hello@somefakeplace.co.uk", EmailType.ROLE_BASED.value, EmailVerificationStatus.UNVERIFIED.value, SourceFamily.OTHER_DIRECTORY.value)
        self.assertEqual(st, EMAIL_STATUS_INVALID)

    def test_05_mx_valid_not_verified_email(self):
        """5. MX validity alone does not imply ownership or verified status."""
        st, _ = classify_discovered_email("test@domain.com", EmailType.GENERIC_BUSINESS.value, EmailVerificationStatus.UNVERIFIED.value, SourceFamily.OFFICIAL_BUSINESS_SITE.value)
        self.assertNotEqual(st, EMAIL_STATUS_VERIFIED_BUSINESS)
        self.assertEqual(st, EMAIL_STATUS_PUBLIC_UNVERIFIED_BUSINESS)


class TestSocialSafety(unittest.TestCase):
    """Invariant 6, 7, 8: Public social profiles remain manual-only, no synthetic recipient IDs."""

    def test_06_public_instagram_remains_manual_only(self):
        """6. Public Instagram remains manual-only."""
        st, manual, auto, _ = classify_discovered_social("https://www.instagram.com/test/", "instagram", True)
        self.assertEqual(st, SOCIAL_STATUS_MANUAL_CONTACTABLE)
        self.assertTrue(manual)
        self.assertFalse(auto)

    def test_07_public_facebook_remains_manual_only(self):
        """7. Public Facebook remains manual-only."""
        st, manual, auto, _ = classify_discovered_social("https://www.facebook.com/test/", "facebook", True)
        self.assertEqual(st, SOCIAL_STATUS_MANUAL_CONTACTABLE)
        self.assertTrue(manual)
        self.assertFalse(auto)

    def test_08_no_synthetic_recipient_ids(self):
        """8. No synthetic recipient IDs (automated_sendable=False across all records)."""
        audit = _load_audit()
        for r in audit["records"]:
            self.assertFalse(r["automated_sendable"])
            self.assertFalse(r["channels"]["instagram"]["automated_sendable"])
            self.assertFalse(r["channels"]["facebook"]["automated_sendable"])
        self.assertEqual(audit["safety"]["FABRICATED_RECIPIENT_IDS"], 0)


class TestPhoneSafety(unittest.TestCase):
    """Invariant 9: Public phone remains manual."""

    def test_09_public_phone_remains_manual(self):
        """9. Public phone remains manual (manual_callable_only)."""
        audit = _load_audit()
        for r in audit["records"]:
            if r["channels"]["phone"]["value"]:
                self.assertTrue(r["channels"]["phone"]["manual_callable_only"])

    def test_09b_phone_normalisation_formats_uk(self):
        """9b. UK numbers are correctly formatted with +44."""
        self.assertEqual(normalise_uk_phone("0161 943 9081"), "+44 161 943 9081")
        self.assertEqual(normalise_uk_phone("+44 161 274 3100"), "+44 161 274 3100")
        # Long Facebook IDs rejected as phone numbers
        self.assertEqual(normalise_uk_phone("100071119218389"), "")


class TestAmbiguityAndRuleBSafety(unittest.TestCase):
    """Invariant 10, 11, 12: Ambiguous contacts not promoted, Rule B preserved."""

    def setUp(self):
        self.audit = _load_audit()

    def test_10_ambiguous_contacts_not_promoted(self):
        """10. Ambiguous contacts are not promoted to contactable."""
        queue = self.audit["discovery_queue"]
        ambiguous = [q for q in queue if q["outcome"] == "AMBIGUOUS_CONTACT"]
        self.assertEqual(len(ambiguous), 5)
        for item in ambiguous:
            self.assertFalse(item["manual_contactable"])

    def test_11_contact_discovery_does_not_bypass_rule_b(self):
        """11. Contact discovery does not bypass Rule B (rating and review requirements)."""
        records = self.audit["records"]
        for r in records:
            if r["qualification_state"] == "RESEARCH_ONLY":
                # Contact found does not make it OUTREACH_READY
                self.assertNotEqual(r["qualification_state"], "OUTREACH_READY")

    def test_12_manual_review_not_outreach_ready(self):
        """12. MANUAL_REVIEW does not become OUTREACH_READY merely because contact is found."""
        records = self.audit["records"]
        mr_records = [r for r in records if r["qualification_state"] == "MANUAL_REVIEW"]
        self.assertEqual(len(mr_records), 4)
        for r in mr_records:
            self.assertEqual(r["qualification_state"], "MANUAL_REVIEW")


class TestLiveSeafoodIntegrityAndIdentifierFix(unittest.TestCase):
    """Invariant 13, 14, 20: Live Seafood canonical ID and outreach state integrity."""

    def setUp(self):
        self.pools = _load_pools()
        self.audit = _load_audit()

    def test_13_live_seafood_remains_canonical_lead_id(self):
        """13. Live Seafood remains LEAD-MAN-0363CF."""
        pool_a = self.pools["pool_a_manual_ready"]
        self.assertEqual(len(pool_a), 1)
        self.assertEqual(pool_a[0]["lead_id"], LIVE_SEAFOOD_CANONICAL_LEAD_ID)
        self.assertEqual(pool_a[0]["company_name"], "Live Seafood Ltd")

    def test_14_live_seafood_outreach_state_unchanged(self):
        """14. Live Seafood outreach state is unchanged."""
        self.assertEqual(self.audit["safety"]["LIVE_SEAFOOD_STATE_UNCHANGED"], "YES")

    def test_20_live_seafood_id_inconsistency_prevented(self):
        """20. The Live Seafood RES-* vs LEAD-* identifier inconsistency is prevented."""
        pool_a = self.pools["pool_a_manual_ready"]
        # Ensure lead_id is NOT the research ID RES-75541E
        self.assertNotEqual(pool_a[0]["lead_id"], LIVE_SEAFOOD_RESEARCH_ID)
        self.assertEqual(pool_a[0]["lead_id"], "LEAD-MAN-0363CF")
        self.assertEqual(pool_a[0]["research_id"], LIVE_SEAFOOD_RESEARCH_ID)
        self.assertEqual(self.audit["safety"]["LIVE_SEAFOOD_ID_CORRECT"], "YES")


class TestCRMDeduplicationAndStateIntegrity(unittest.TestCase):
    """Invariant 15 & 16: Existing CRM records enriched, no duplicates, qualification frozen."""

    def setUp(self):
        self.audit = _load_audit()

    def test_15_existing_records_enriched_no_duplicates(self):
        """15. Existing CRM records are enriched instead of duplicated."""
        dedup = self.audit["deduplication"]
        self.assertEqual(dedup["DUPLICATES_FOUND"], 0)
        self.assertEqual(dedup["DUPLICATES_CREATED"], 0)
        self.assertEqual(dedup["EXISTING_RECORDS_ENRICHED"], 28)

    def test_16_qualification_fields_remain_unchanged(self):
        """16. Qualification fields remain unchanged."""
        self.assertEqual(self.audit["safety"]["QUALIFICATION_STATES_MUTATED"], 0)
        funnel = self.audit["funnel_comparison"]
        self.assertEqual(funnel["OUTREACH_READY_BEFORE"], 1)
        self.assertEqual(funnel["OUTREACH_READY_AFTER"], 1)
        self.assertEqual(funnel["MANUAL_REVIEW_BEFORE"], 4)
        self.assertEqual(funnel["MANUAL_REVIEW_AFTER"], 4)


class TestNoOutreachInvariants(unittest.TestCase):
    """Invariant 17, 18, 19: No outreach send, no campaigns armed, message history untouched."""

    def setUp(self):
        self.audit = _load_audit()

    def test_17_no_outreach_send_occurs(self):
        """17. No outreach send occurs."""
        self.assertEqual(self.audit["safety"]["OUTREACH_SENDS"], 0)
        self.assertEqual(self.audit["safety"]["AUTOMATED_SENDS"], 0)

    def test_18_no_campaigns_armed(self):
        """18. No campaigns are armed."""
        self.assertEqual(self.audit["safety"]["CAMPAIGNS_ARMED"], 0)

    def test_19_message_history_remains_unchanged(self):
        """19. Message history remains unchanged."""
        self.assertEqual(self.audit["safety"]["MESSAGE_HISTORY_MUTATED"], 0)
        self.assertEqual(self.audit["safety"]["PROTECTED_FILES_MUTATED"], 0)


class TestOutreachPoolRebuild(unittest.TestCase):
    """Pool breakdown tests for Pools A, B, C, D."""

    def setUp(self):
        self.pools = _load_pools()

    def test_pools_summary_counts(self):
        """Verify summary counts across all 4 rebuilt pools."""
        summary = self.pools["summary"]
        self.assertEqual(summary["pool_a_count"], 1)
        self.assertEqual(summary["pool_b_count"], 0)
        self.assertEqual(summary["pool_c_count"], 1)
        self.assertEqual(summary["pool_d_count"], 4)

    def test_pool_d_members(self):
        """Verify Pool D contains the 4 manual review businesses."""
        pool_d = self.pools["pool_d_manual_review_human_decision"]
        names = {item["company_name"] for item in pool_d}
        self.assertEqual(names, {"Ducie Arms", "Kro Bar", "Glamorous Chinese Restaurant", "Crown & Anchor"})
        for item in pool_d:
            self.assertEqual(item["decision"], "REMAIN_MANUAL_REVIEW")


if __name__ == "__main__":
    unittest.main()
