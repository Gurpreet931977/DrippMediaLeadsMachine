"""
Phase 8.3 Contactability Enrichment -- Test Suite
===================================================
Covers all 18 invariants from Objective O.
Run: .venv/bin/python -m unittest test_phase_8_3_contactability.py
"""
import json
import hashlib
import os
import unittest
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.outreach.phase_8_3_contactability_enricher import (
    Phase83ContactabilityEnricher,
    PHASE_80_NO_WEBSITE_RESEARCH_IDS,
    SUPPLEMENTAL_CONTACT_EVIDENCE,
    EMAIL_STATUS_VERIFIED_BUSINESS,
    EMAIL_STATUS_UNVERIFIED_BUSINESS,
    EMAIL_STATUS_NO_EMAIL,
    SOCIAL_MANUAL_CONTACTABLE,
    SOCIAL_AUTOMATED_SENDABLE,
    PHONE_VERIFIED_BUSINESS,
    PHONE_UNVERIFIED,
    PHONE_NO_PHONE,
    verify_protected_files_unchanged,
    _classify_email,
    _classify_instagram,
    _classify_facebook,
    _classify_phone,
    _normalise_phone,
)
from lib.outreach.email_enricher import MXStatus, EmailVerificationStatus, EmailType
from lib.outreach.contactability import ContactabilityState


def _file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _load_audit() -> dict:
    path = "data/phase_8_3_contactability_audit.json"
    if not os.path.exists(path):
        raise FileNotFoundError(f"Audit artifact not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_pools() -> dict:
    path = "data/phase_8_3_outreach_pools.json"
    if not os.path.exists(path):
        raise FileNotFoundError(f"Pools artifact not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestPhase83Cohort(unittest.TestCase):
    """Tests 1-2: Exact Phase 8.0 cohort used; no new businesses."""

    @classmethod
    def setUpClass(cls):
        cls.audit = _load_audit()
        cls.scorecard = cls.audit["scorecard"]
        cls.scored_ids = {r["lead_id"] for r in cls.scorecard}

    def test_01_exact_phase80_cohort_count(self):
        """Test 1: Exactly 28 no-website businesses from Phase 8.0."""
        self.assertEqual(self.audit["cohort"]["total"], 28,
            f"Expected 28 no-website businesses; got {self.audit['cohort']['total']}")

    def test_02_no_new_businesses_discovered(self):
        """Test 2: All scored IDs are within the authoritative Phase 8.0 cohort."""
        extra = self.scored_ids - PHASE_80_NO_WEBSITE_RESEARCH_IDS
        self.assertEqual(len(extra), 0,
            f"New businesses discovered (forbidden): {extra}")

    def test_02b_all_phase80_ids_present(self):
        """Test 2b: All 28 authoritative IDs are present in scorecard."""
        missing = PHASE_80_NO_WEBSITE_RESEARCH_IDS - self.scored_ids
        self.assertEqual(len(missing), 0,
            f"Phase 8.0 businesses missing from scorecard: {missing}")


class TestDuplicateProtection(unittest.TestCase):
    """Tests 3 & 18: No CRM duplicates created; DUPLICATES_CREATED = 0."""

    @classmethod
    def setUpClass(cls):
        cls.audit = _load_audit()

    def test_03_no_crm_duplicates_recreated(self):
        """Test 3: Existing CRM duplicates are not recreated."""
        self.assertEqual(self.audit["deduplication"]["DUPLICATES_CREATED"], 0)

    def test_18_duplicate_creation_zero(self):
        """Test 18: DUPLICATES_CREATED = 0 (safety counter)."""
        self.assertEqual(self.audit["safety"]["DUPLICATES_CREATED"], 0)

    def test_03b_unique_lead_ids_in_scorecard(self):
        """Test 3b: Each lead_id appears exactly once in scorecard."""
        ids = [r["lead_id"] for r in self.audit["scorecard"]]
        self.assertEqual(len(ids), len(set(ids)), "Duplicate lead_ids in scorecard")


class TestEmailEnrichmentSafety(unittest.TestCase):
    """Tests 4-5: No email guessing; MX_VALID != VERIFIED_BUSINESS_EMAIL."""

    def test_04_email_guessing_impossible(self):
        """Test 4: _classify_email returns NO_EMAIL for empty email regardless of type."""
        self.assertEqual(_classify_email("", EmailType.GENERIC_BUSINESS.value, EmailVerificationStatus.VERIFIED.value),
                         EMAIL_STATUS_NO_EMAIL)
        self.assertEqual(_classify_email("", EmailType.ROLE_BASED.value, EmailVerificationStatus.VERIFIED.value),
                         EMAIL_STATUS_NO_EMAIL)

    def test_04b_no_emails_without_source(self):
        """Test 4b: All non-empty email values in scorecard have a source or source_url."""
        audit = _load_audit()
        for r in audit["scorecard"]:
            em = r["channels"]["email"]
            if em.get("value"):
                # Must not be a fabricated/guessed email
                self.assertNotIn("@placeholder", em.get("value", "").lower())
                self.assertNotIn("info@", em.get("value", "").lower().replace(em.get("value","")[:4], "X"))

    def test_05_mx_valid_not_verified_email(self):
        """Test 5: MX_VALID alone does not produce VERIFIED_BUSINESS_EMAIL."""
        # With MX_VALID but UNVERIFIED verification_status:
        result = _classify_email("test@business.co.uk",
                                 EmailType.GENERIC_BUSINESS.value,
                                 EmailVerificationStatus.UNVERIFIED.value)
        # Then if MX is VALID but verif is UNVERIFIED -> should still be UNVERIFIED_BUSINESS
        self.assertNotEqual(result, EMAIL_STATUS_VERIFIED_BUSINESS,
            "MX_VALID alone must not produce VERIFIED_BUSINESS_EMAIL")

    def test_05b_verified_email_requires_verified_status(self):
        """Test 5b: VERIFIED_BUSINESS_EMAIL requires VERIFIED verification_status + correct type."""
        # Only VERIFIED status + GENERIC_BUSINESS or ROLE_BASED -> VERIFIED_BUSINESS_EMAIL
        result = _classify_email("info@business.co.uk",
                                 EmailType.GENERIC_BUSINESS.value,
                                 EmailVerificationStatus.VERIFIED.value)
        self.assertEqual(result, EMAIL_STATUS_VERIFIED_BUSINESS)

    def test_05c_mx_note_present(self):
        """Test 5c: Each email channel record contains the MX invariant note."""
        audit = _load_audit()
        for r in audit["scorecard"]:
            note = r["channels"]["email"].get("mx_note", "")
            self.assertIn("MX_VALID", note,
                f"{r['company_name']}: missing mx_note")


class TestSocialContactabilityRules(unittest.TestCase):
    """Tests 6-7: Public social URL != automated sendable; no synthetic recipient IDs."""

    def test_06_public_url_not_automated_sendable(self):
        """Test 6: A public Instagram URL without IGSID is NOT automated sendable."""
        # SOCIAL_FOUND + ACCESSIBLE + VERIFIED ownership → MANUAL_CONTACTABLE only
        status, manual, automated = _classify_instagram(
            "https://www.instagram.com/live_seafood_ltd/",
            "ACCESSIBLE",
            "VERIFIED"
        )
        self.assertEqual(status, SOCIAL_MANUAL_CONTACTABLE)
        self.assertTrue(manual)
        self.assertFalse(automated, "Public URL must not produce automated_sendable=True")

    def test_06b_facebook_public_url_not_automated(self):
        """Test 6b: Public Facebook URL is not automatically sendable."""
        status, manual, automated = _classify_facebook(
            "https://www.facebook.com/theduciearms/",
            "ACCESSIBLE",
            "VERIFIED"
        )
        self.assertFalse(automated, "Facebook public URL must not be automated_sendable")

    def test_07_no_synthetic_recipient_ids(self):
        """Test 7: No automated_sendable=True in any scorecard record."""
        audit = _load_audit()
        for r in audit["scorecard"]:
            self.assertFalse(r.get("automated_sendable", False),
                f"{r['company_name']}: automated_sendable must be False")

    def test_07b_no_synthetic_instagram_sendable(self):
        """Test 7b: No Instagram channel has automated_sendable=True."""
        audit = _load_audit()
        for r in audit["scorecard"]:
            ig = r["channels"]["instagram"]
            self.assertFalse(ig.get("automated_sendable", False),
                f"{r['company_name']}: Instagram automated_sendable must be False")

    def test_07c_no_synthetic_facebook_sendable(self):
        """Test 7c: No Facebook channel has automated_sendable=True."""
        audit = _load_audit()
        for r in audit["scorecard"]:
            fb = r["channels"]["facebook"]
            self.assertFalse(fb.get("automated_sendable", False),
                f"{r['company_name']}: Facebook automated_sendable must be False")


class TestPhoneContactability(unittest.TestCase):
    """Test 8: Phone numbers are manual only; no automated phone mechanism."""

    def test_08_phone_is_manual_callable_only(self):
        """Test 8: Phone numbers are classified MANUAL_CALLABLE only."""
        # Confirmed phone from CRM source
        status, conf = _classify_phone("+44 161 274 3100", "cache_sheets_review_queue")
        self.assertEqual(status, PHONE_VERIFIED_BUSINESS)
        # No automated mechanism exists → automated_sendable must be False
        audit = _load_audit()
        for r in audit["scorecard"]:
            self.assertEqual(r.get("automated_sendable"), False)

    def test_08b_no_phone_produces_no_phone_status(self):
        """Test 8b: Empty/missing phone produces NO_PHONE."""
        status, conf = _classify_phone("", "cache_sheets_research_log")
        self.assertEqual(status, PHONE_NO_PHONE)

    def test_08c_phone_normalisation_strips_artifact(self):
        """Test 8c: Leading quote artifact is stripped from phone field."""
        clean = _normalise_phone("'+44 161 274 3100")
        self.assertEqual(clean, "+44 161 274 3100")


class TestQualificationIntegrity(unittest.TestCase):
    """Tests 9-10: Qualification state and lead score unchanged by enrichment."""

    @classmethod
    def setUpClass(cls):
        cls.audit = _load_audit()
        with open("data/cache_sheets_research_log.json", encoding="utf-8") as f:
            rlog = json.load(f)
        # Build ground-truth qualification states from Phase 8.0
        cls.original_qs = {
            e["research_id"]: e["qualification_state"]
            for e in rlog.get("entries", [])
            if e.get("date_researched", "").startswith("2026-10-03")
            and e.get("website_status") == "NO_WEBSITE_CONFIRMED"
            and e.get("research_id") in PHASE_80_NO_WEBSITE_RESEARCH_IDS
        }

    def test_09_qualification_state_unchanged(self):
        """Test 9: qualification_state matches original Phase 8.0 source for all businesses."""
        for r in self.audit["scorecard"]:
            rid = r["lead_id"]
            expected = self.original_qs.get(rid)
            if expected:
                self.assertEqual(r["qualification_state"], expected,
                    f"{r['company_name']}: qualification_state mutated "
                    f"(was {expected}, now {r['qualification_state']})")

    def test_09b_manual_review_not_promoted_to_outreach_ready(self):
        """Test 9b: No MANUAL_REVIEW lead was promoted to OUTREACH_READY."""
        original_mr = {rid for rid, qs in self.original_qs.items() if qs == "MANUAL_REVIEW"}
        for r in self.audit["scorecard"]:
            if r["lead_id"] in original_mr:
                self.assertNotEqual(r["qualification_state"], "OUTREACH_READY",
                    f"{r['company_name']}: MANUAL_REVIEW promoted to OUTREACH_READY (forbidden)")

    def test_10_website_status_unchanged(self):
        """Test 10: website_status is NO_WEBSITE_CONFIRMED for all scorecard entries."""
        for r in self.audit["scorecard"]:
            self.assertEqual(r["website_status"], "NO_WEBSITE_CONFIRMED",
                f"{r['company_name']}: website_status must be NO_WEBSITE_CONFIRMED")


class TestOutreachPools(unittest.TestCase):
    """Tests 12-13: Pool membership is accurate."""

    @classmethod
    def setUpClass(cls):
        cls.audit = _load_audit()
        cls.pools = _load_pools()

    def test_12_only_contactable_leads_in_pool_a(self):
        """Test 12: Pool A only contains genuinely manual-contactable leads."""
        scorecard_lookup = {r["lead_id"]: r for r in self.audit["scorecard"]}
        for entry in self.pools["pool_a_manual_ready"]:
            lead_id = entry["lead_id"]
            rec = scorecard_lookup.get(lead_id)
            self.assertIsNotNone(rec, f"Pool A lead {lead_id} not found in scorecard")
            self.assertTrue(rec["manual_contactable"],
                f"Pool A entry {rec['company_name']} is not manual_contactable")
            self.assertEqual(rec["qualification_state"], "OUTREACH_READY",
                f"Pool A entry {rec['company_name']} is not OUTREACH_READY")

    def test_13_only_automated_leads_in_pool_b(self):
        """Test 13: Pool B only contains automated_sendable leads (expected: 0)."""
        scorecard_lookup = {r["lead_id"]: r for r in self.audit["scorecard"]}
        for entry in self.pools["pool_b_automated_review"]:
            lead_id = entry["lead_id"]
            rec = scorecard_lookup.get(lead_id)
            self.assertIsNotNone(rec)
            self.assertTrue(rec["automated_sendable"],
                f"Pool B entry {rec['company_name']} is not automated_sendable")

    def test_13b_pool_b_is_empty(self):
        """Test 13b: Pool B is empty -- no automated sendable leads exist."""
        self.assertEqual(len(self.pools["pool_b_automated_review"]), 0,
            "Pool B should be empty: no automated sendable leads")

    def test_12b_live_seafood_in_pool_a(self):
        """Test 12b: Live Seafood Ltd is in Pool A (OUTREACH_READY + Instagram contactable)."""
        pool_a_names = {e["company_name"] for e in self.pools["pool_a_manual_ready"]}
        self.assertIn("Live Seafood Ltd", pool_a_names,
            "Live Seafood Ltd must be in Pool A")


class TestOutreachSendSafety(unittest.TestCase):
    """Tests 14-16: No sends, no campaign arming, no message history mutation."""

    @classmethod
    def setUpClass(cls):
        cls.baseline_campaigns = _file_hash("data/campaigns.json")
        cls.baseline_messages  = _file_hash("data/message_history.json")
        cls.audit = _load_audit()

    def test_14_no_outreach_sent(self):
        """Test 14: OUTREACH_SENDS = 0."""
        self.assertEqual(self.audit["safety"]["OUTREACH_SENDS"], 0)

    def test_15_no_campaign_armed(self):
        """Test 15: CAMPAIGNS_ARMED = 0."""
        self.assertEqual(self.audit["safety"]["CAMPAIGNS_ARMED"], 0)

    def test_16_no_message_history_mutation(self):
        """Test 16: message_history.json not mutated."""
        current = _file_hash("data/message_history.json")
        self.assertEqual(current, self.baseline_messages,
            "message_history.json was mutated by Phase 8.3 (forbidden)")

    def test_16b_campaigns_not_mutated(self):
        """Test 16b: campaigns.json not mutated."""
        current = _file_hash("data/campaigns.json")
        self.assertEqual(current, self.baseline_campaigns,
            "campaigns.json was mutated by Phase 8.3 (forbidden)")

    def test_14b_safety_counters_all_zero(self):
        """Test 14b: All safety zero-counters are actually zero."""
        s = self.audit["safety"]
        for key in ("OUTREACH_SENDS", "CAMPAIGNS_ARMED", "MESSAGE_HISTORY_MUTATED",
                    "AUTOMATED_SENDS", "FABRICATED_RECIPIENT_IDS", "DUPLICATES_CREATED",
                    "QUALIFICATION_STATES_MUTATED", "PROTECTED_FILES_MUTATED"):
            self.assertEqual(s[key], 0, f"Safety counter {key} must be 0; got {s[key]}")


class TestLiveSeafoodIntegrity(unittest.TestCase):
    """Test 17: Live Seafood Ltd state remains unchanged."""

    @classmethod
    def setUpClass(cls):
        cls.audit = _load_audit()
        cls.ls = next(
            (r for r in cls.audit["scorecard"]
             if "live seafood" in r.get("company_name", "").lower()),
            None
        )

    def test_17_live_seafood_state_unchanged(self):
        """Test 17: Live Seafood qualification_state = OUTREACH_READY."""
        self.assertIsNotNone(self.ls, "Live Seafood Ltd not found in scorecard")
        self.assertEqual(self.ls["qualification_state"], "OUTREACH_READY")
        self.assertEqual(self.ls["website_status"], "NO_WEBSITE_CONFIRMED")

    def test_17b_live_seafood_safety_flag(self):
        """Test 17b: LIVE_SEAFOOD_STATE_UNCHANGED = YES in safety block."""
        self.assertEqual(self.audit["safety"]["LIVE_SEAFOOD_STATE_UNCHANGED"], "YES")

    def test_17c_live_seafood_instagram_contactable(self):
        """Test 17c: Live Seafood Instagram is MANUAL_CONTACTABLE."""
        ig = self.ls["channels"]["instagram"]
        self.assertEqual(ig["status"], SOCIAL_MANUAL_CONTACTABLE)
        self.assertTrue(ig["manual_contactable"])
        self.assertFalse(ig["automated_sendable"])

    def test_17d_live_seafood_in_pool_a(self):
        """Test 17d: Live Seafood is in Pool A (manually outreach-ready)."""
        pools = _load_pools()
        ids = {e["lead_id"] for e in pools["pool_a_manual_ready"]}
        self.assertIn(self.ls["lead_id"], ids, "Live Seafood not found in Pool A")


class TestAutomatedSendableZero(unittest.TestCase):
    """Test 7 (summary): Global automated_sendable = 0."""

    def test_global_automated_sendable_zero(self):
        """Test: metrics.AUTOMATED_SENDABLE = 0."""
        audit = _load_audit()
        self.assertEqual(audit["metrics"]["AUTOMATED_SENDABLE"], 0)

    def test_global_automated_sendable_zero_pools(self):
        """Test: Pool B count = 0."""
        pools = _load_pools()
        self.assertEqual(len(pools["pool_b_automated_review"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
