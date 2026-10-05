"""
Unit tests for Phase 4: Multi-Signal Operational Verification & Evidence Independence.

Tests:
  - Cases A through J (real observed and edge cases)
  - Section 11 Counterfactual Safety Matrix
"""

import unittest
from datetime import datetime, timedelta

from lib.types import (
    DiscoveredBusiness,
    OperationalStatus,
    OperationalConfidence,
    SourceFamily,
    WebsiteStatus,
    QualificationState,
)
from lib.validation.operational_validator import (
    OperationalValidator,
)
from lib.qualification.lead_scoring import LeadScoringProvider


class TestOperationalMultiSignal(unittest.TestCase):
    def setUp(self):
        self.validator = OperationalValidator()
        self.scorer = LeadScoringProvider()
        self.now = datetime.now()
        self.recent_date = (self.now - timedelta(days=20)).strftime("%Y-%m-%d")
        self.stale_date = (self.now - timedelta(days=220)).strftime("%Y-%m-%d")

    # =========================================================================
    # SECTION 9: REAL OBSERVED CASES (A THROUGH J)
    # =========================================================================

    def test_case_a_casa_di_alessia(self):
        """CASE A: Casa Di Alessia (143 reviews, 4.9★, recent Tripadvisor review, verified OSM phone/address) -> ACTIVE_CONFIRMED"""
        biz = DiscoveredBusiness(
            company_name="Casa Di Alessia",
            category="Italian Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="15 New Briggate, Leeds LS1 6BT",
            phone="+44 113 244 5566",
            raw_website="",
            review_count=143,
            rating=4.9,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 143,
                "rating": 4.9,
                "review_source": "Tripadvisor",
                "review_source_url": "https://www.tripadvisor.co.uk/Restaurant_Review-g186411-d12345-Casa_Di_Alessia-Leeds.html",
                "review_evidence_date": self.recent_date,
                "review_confidence": "HIGH",
                "review_freshness": "RECENT"
            }
        }
        biz.evidence_sources = {
            "name": "OPENSTREETMAP",
            "address": "OPENSTREETMAP",
            "phone": "OPENSTREETMAP"
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {},
            "verified_handles": {}
        }

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(op_res["operational_confidence"], OperationalConfidence.HIGH.value)
        self.assertTrue(op_res["multi_signal_rule_applied"])
        self.assertEqual(op_res["review_source_family"], SourceFamily.TRIPADVISOR.value)
        self.assertIn("OPENSTREETMAP", op_res["independent_source_families"])

        # End-to-end qualification check: passed all hard gates, reaches score evaluation
        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(qual_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertIn(qual_res["qualification_state"], [QualificationState.OUTREACH_READY.value, QualificationState.MANUAL_REVIEW.value])

        # If score reaches 60+ (e.g. 500+ reviews), verify OUTREACH_READY is achieved without social media
        biz.review_count = 550
        qual_60 = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(qual_60["qualification_state"], QualificationState.OUTREACH_READY.value)

    def test_case_b_family_fortune(self):
        """CASE B: Family Fortune (133 reviews, 4.6★, recent Tripadvisor review, verified OSM phone/address) -> ACTIVE_CONFIRMED"""
        biz = DiscoveredBusiness(
            company_name="Family Fortune",
            category="Chinese Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="87 Kirkgate, Leeds LS2 7DJ",
            phone="+44 113 245 8899",
            raw_website="",
            review_count=133,
            rating=4.6,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 133,
                "rating": 4.6,
                "review_source": "Tripadvisor",
                "review_source_url": "https://www.tripadvisor.co.uk/Restaurant_Review-g186411-d54321-Family_Fortune-Leeds.html",
                "review_evidence_date": self.recent_date,
                "review_confidence": "HIGH",
                "review_freshness": "RECENT"
            }
        }
        biz.evidence_sources = {
            "name": "OPENSTREETMAP",
            "address": "OPENSTREETMAP",
            "phone": "OPENSTREETMAP"
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {},
            "verified_handles": {}
        }

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertTrue(op_res["multi_signal_rule_applied"])

        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(qual_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

    def test_case_c_il_vicoletto(self):
        """CASE C: Il Vicoletto (88 reviews, 4.6★, recent Tripadvisor review, verified OSM phone/address) -> ACTIVE_CONFIRMED"""
        biz = DiscoveredBusiness(
            company_name="Il Vicoletto",
            category="Italian Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="12 Cross Belgrave St, Leeds LS2 8JP",
            phone="+44 113 243 0011",
            raw_website="",
            review_count=88,
            rating=4.6,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 88,
                "rating": 4.6,
                "review_source": "Tripadvisor",
                "review_source_url": "https://www.tripadvisor.co.uk/Restaurant_Review-g186411-d99887-Il_Vicoletto-Leeds.html",
                "review_evidence_date": self.recent_date,
                "review_confidence": "HIGH",
                "review_freshness": "RECENT"
            }
        }
        biz.evidence_sources = {
            "name": "OPENSTREETMAP",
            "address": "OPENSTREETMAP",
            "phone": "OPENSTREETMAP"
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {},
            "verified_handles": {}
        }

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertTrue(op_res["multi_signal_rule_applied"])

        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(qual_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

    def test_case_d_no_independent_operational_signal(self):
        """CASE D: 50+ reviews + 4.5 rating, recent review, NO independent operational signal (no phone, no address) -> ACTIVE_LIKELY, NOT ACTIVE_CONFIRMED"""
        biz = DiscoveredBusiness(
            company_name="Virtual Pizza",
            category="Pizzeria",
            city="Leeds",
            target_country="United Kingdom",
            address="",
            phone="",
            raw_website="",
            review_count=120,
            rating=4.5,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 120,
                "rating": 4.5,
                "review_source": "Tripadvisor",
                "review_source_url": "https://www.tripadvisor.co.uk/Restaurant_Review-virtual.html",
                "review_evidence_date": self.recent_date,
                "review_confidence": "HIGH",
                "review_freshness": "RECENT"
            }
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {},
            "verified_handles": {}
        }

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertIn(op_res["operational_status"], [OperationalStatus.ACTIVE_LIKELY.value, OperationalStatus.OPERATIONAL_UNKNOWN.value])
        self.assertFalse(op_res["multi_signal_rule_applied"])

        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertNotEqual(qual_res["qualification_state"], QualificationState.OUTREACH_READY.value)

    def test_case_e_closure_evidence_overrides_reviews(self):
        """CASE E: 50+ reviews + 4.5 rating, recent reviews, operational signal, but explicit closure signal -> CLOSED_OR_UNVERIFIED"""
        biz = DiscoveredBusiness(
            company_name="Closed Leeds Diner",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="10 Headrow, Leeds LS1 5AA",
            phone="+44 113 222 3344",
            raw_website="",
            review_count=200,
            rating=4.8,
            latest_review_date=self.recent_date,
            is_permanently_closed=True,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 200,
                "rating": 4.8,
                "review_source": "Tripadvisor",
                "review_evidence_date": self.recent_date,
                "review_confidence": "HIGH",
                "review_freshness": "RECENT"
            }
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {},
            "verified_handles": {}
        }

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertEqual(op_res["operational_status"], OperationalStatus.CLOSED_OR_UNVERIFIED.value)
        self.assertFalse(op_res["multi_signal_rule_applied"])

        # Also test with textual closure phrase in operational evidence
        biz.is_permanently_closed = False
        biz.operational_evidence = "Notice: The business has ceased trading and is permanently closed."
        op_res_text = self.validator.verify_operations(biz, social_audit)
        self.assertEqual(op_res_text["operational_status"], OperationalStatus.CLOSED_OR_UNVERIFIED.value)

    def test_case_f_stale_reviews_not_eligible(self):
        """CASE F: 50+ reviews + 4.5 rating, phone/address present, but stale reviews (>180d) -> NOT ACTIVE_CONFIRMED"""
        biz = DiscoveredBusiness(
            company_name="Historic Leeds Pub",
            category="Pub",
            city="Leeds",
            target_country="United Kingdom",
            address="50 Briggate, Leeds LS1 6HF",
            phone="+44 113 234 5678",
            raw_website="",
            review_count=150,
            rating=4.7,
            latest_review_date=self.stale_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 150,
                "rating": 4.7,
                "review_source": "Tripadvisor",
                "review_evidence_date": self.stale_date,
                "review_confidence": "HIGH",
                "review_freshness": "STALE"
            }
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {},
            "verified_handles": {}
        }

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(op_res["operational_status"], OperationalStatus.ACTIVE_LIKELY.value)
        self.assertFalse(op_res["multi_signal_rule_applied"])

    def test_case_g_same_source_family_not_independent(self):
        """CASE G: Google Maps listing supplies reviews + phone + address (all GOOGLE) -> does NOT count as independent corroboration -> NOT ACTIVE_CONFIRMED"""
        biz = DiscoveredBusiness(
            company_name="Single Source Cafe",
            category="Cafe",
            city="Leeds",
            target_country="United Kingdom",
            address="20 Boar Lane, Leeds LS1 6EN",
            phone="+44 113 244 1122",
            raw_website="",
            review_count=95,
            rating=4.5,
            latest_review_date=self.recent_date,
            discovery_source="APIFY",
            city_match=True
        )
        biz.phone_source_family = "GOOGLE"
        biz.address_source_family = "GOOGLE"
        biz.hours_source_family = "GOOGLE"
        biz.evidence_sources = {
            "review": "GOOGLE",
            "phone": "GOOGLE",
            "address": "GOOGLE"
        }
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 95,
                "rating": 4.5,
                "review_source": "Google Maps",
                "review_source_url": "https://maps.google.com/?cid=123",
                "review_evidence_date": self.recent_date,
                "review_confidence": "HIGH",
                "review_freshness": "RECENT"
            }
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {},
            "verified_handles": {}
        }

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(op_res["operational_status"], OperationalStatus.ACTIVE_LIKELY.value)
        self.assertFalse(op_res["multi_signal_rule_applied"])
        self.assertEqual(len(op_res["independent_operational_signals"]), 0)

    def test_case_h_independent_sources_eligible(self):
        """CASE H: Tripadvisor review (TRIPADVISOR) + verified phone from separate source (OPENSTREETMAP) -> eligible for ACTIVE_CONFIRMED"""
        biz = DiscoveredBusiness(
            company_name="Independent Corroboration Grill",
            category="Grill",
            city="Leeds",
            target_country="United Kingdom",
            address="5 Call Lane, Leeds LS1 6DN",
            phone="+44 113 245 6677",
            raw_website="",
            review_count=75,
            rating=4.4,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.phone_source_family = "OPENSTREETMAP"
        biz.address_source_family = "OPENSTREETMAP"
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 75,
                "rating": 4.4,
                "review_source": "Tripadvisor",
                "review_source_url": "https://www.tripadvisor.co.uk/Restaurant_Review-example.html",
                "review_evidence_date": self.recent_date,
                "review_confidence": "HIGH",
                "review_freshness": "RECENT"
            }
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {},
            "verified_handles": {}
        }

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertTrue(op_res["multi_signal_rule_applied"])
        self.assertIn("OPENSTREETMAP", op_res["independent_source_families"])

    def test_case_i_creator_mention_alone_not_independent_ops_signal(self):
        """CASE I: Creator mention + recent reviews, but NO independent operational signal -> NOT ACTIVE_CONFIRMED"""
        biz = DiscoveredBusiness(
            company_name="Influencer Food Truck",
            category="Street Food",
            city="Leeds",
            target_country="United Kingdom",
            address="",
            phone="",
            raw_website="",
            review_count=90,
            rating=4.6,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 90,
                "rating": 4.6,
                "review_source": "Tripadvisor",
                "review_source_url": "https://www.tripadvisor.co.uk/Restaurant_Review-truck.html",
                "review_evidence_date": self.recent_date,
                "review_confidence": "HIGH",
                "review_freshness": "RECENT"
            }
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {},
            "verified_handles": {}
        }

        creator_ev = {
            "creator_evidence_status": "FOUND",
            "creator_evidence_confidence": "HIGH",
            "creator_latest_date": self.recent_date,
            "platform": "tiktok"
        }

        op_res = self.validator.verify_operations(biz, social_audit, creator_evidence=creator_ev)
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(op_res["operational_status"], OperationalStatus.ACTIVE_LIKELY.value)
        self.assertFalse(op_res["multi_signal_rule_applied"])

    def test_case_j_wrong_city_branch_rejected(self):
        """CASE J: Wrong city branch (Leeds business, but review references Birmingham) -> location conflict, NOT ACTIVE_CONFIRMED"""
        biz = DiscoveredBusiness(
            company_name="The Botanist",
            category="Bar",
            city="Leeds",
            target_country="United Kingdom",
            address="67 Boar Lane, Leeds LS1 6HW",
            phone="+44 113 205 3240",
            raw_website="",
            review_count=1200,
            rating=4.5,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        # Review URL points to Birmingham branch
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 1200,
                "rating": 4.5,
                "review_source": "Tripadvisor",
                "review_source_url": "https://www.tripadvisor.co.uk/Restaurant_Review-g186402-d6990432-Reviews-The_Botanist-Birmingham_West_Midlands.html",
                "review_evidence": "Great drinks in Birmingham!",
                "review_evidence_date": self.recent_date,
                "review_confidence": "HIGH",
                "review_freshness": "RECENT"
            }
        }

        social_audit = {
            "social_status": "SOCIAL_NOT_FOUND",
            "social_ownership_status": "UNKNOWN",
            "social_profile_status": "UNKNOWN",
            "social_activity": "UNKNOWN",
            "verified_urls": {},
            "verified_handles": {}
        }

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertEqual(op_res["operational_status"], OperationalStatus.ACTIVE_LIKELY.value)
        self.assertFalse(op_res["multi_signal_rule_applied"])
        self.assertTrue(any("location conflict" in rf.lower() for rf in op_res["red_flags"]))

    # =========================================================================
    # SECTION 11: COUNTERFACTUAL SAFETY TEST MATRIX
    # =========================================================================

    def test_counterfactual_01_high_reviews_high_rating_no_operational_signal(self):
        """Matrix 1: high reviews + high rating + no operational signal -> NOT ACTIVE_CONFIRMED & NOT OUTREACH_READY"""
        biz = DiscoveredBusiness(
            company_name="Matrix Eatery 1",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="",
            phone="",
            raw_website="",
            review_count=500,
            rating=4.8,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {"review_enrichment": {"review_source": "Tripadvisor", "review_evidence_date": self.recent_date}}
        social_audit = {"verified_urls": {}, "social_ownership_status": "UNKNOWN", "social_profile_status": "UNKNOWN", "social_activity": "UNKNOWN"}

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertNotEqual(qual_res["qualification_state"], QualificationState.OUTREACH_READY.value)

    def test_counterfactual_02_high_reviews_high_rating_stale_reviews(self):
        """Matrix 2: high reviews + high rating + stale reviews (>180d) -> NOT ACTIVE_CONFIRMED & NOT OUTREACH_READY"""
        biz = DiscoveredBusiness(
            company_name="Matrix Eatery 2",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="12 Main St, Leeds LS1 1AA",
            phone="+44 113 111 2222",
            raw_website="",
            review_count=350,
            rating=4.7,
            latest_review_date=self.stale_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {"review_enrichment": {"review_source": "Tripadvisor", "review_evidence_date": self.stale_date}}
        social_audit = {"verified_urls": {}, "social_ownership_status": "UNKNOWN", "social_profile_status": "UNKNOWN", "social_activity": "UNKNOWN"}

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertNotEqual(qual_res["qualification_state"], QualificationState.OUTREACH_READY.value)

    def test_counterfactual_03_high_reviews_high_rating_closure_signal(self):
        """Matrix 3: high reviews + high rating + closure signal -> CLOSED_OR_UNVERIFIED & NOT OUTREACH_READY"""
        biz = DiscoveredBusiness(
            company_name="Matrix Eatery 3",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="15 Main St, Leeds LS1 1AA",
            phone="+44 113 111 3333",
            raw_website="",
            review_count=400,
            rating=4.9,
            latest_review_date=self.recent_date,
            is_permanently_closed=True,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {"review_enrichment": {"review_source": "Tripadvisor", "review_evidence_date": self.recent_date}}
        social_audit = {"verified_urls": {}, "social_ownership_status": "UNKNOWN", "social_profile_status": "UNKNOWN", "social_activity": "UNKNOWN"}

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertEqual(op_res["operational_status"], OperationalStatus.CLOSED_OR_UNVERIFIED.value)

        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(qual_res["qualification_state"], QualificationState.EXCLUDED.value)

    def test_counterfactual_04_high_reviews_high_rating_wrong_city(self):
        """Matrix 4: high reviews + high rating + wrong city (city_match=False) -> NOT ACTIVE_CONFIRMED & NOT OUTREACH_READY"""
        biz = DiscoveredBusiness(
            company_name="Matrix Eatery 4",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="10 New St, York YO1 8AA",
            phone="+44 1904 111 444",
            raw_website="",
            review_count=250,
            rating=4.6,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=False
        )
        biz.raw_data = {"review_enrichment": {"review_source": "Tripadvisor", "review_evidence_date": self.recent_date}}
        social_audit = {"verified_urls": {}, "social_ownership_status": "UNKNOWN", "social_profile_status": "UNKNOWN", "social_activity": "UNKNOWN"}

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertNotEqual(qual_res["qualification_state"], QualificationState.OUTREACH_READY.value)

    def test_counterfactual_05_high_reviews_high_rating_conflicting_sources(self):
        """Matrix 5: high reviews + high rating + conflicting sources -> NOT ACTIVE_CONFIRMED & NOT OUTREACH_READY"""
        biz = DiscoveredBusiness(
            company_name="Matrix Eatery 5",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="20 Main St, Leeds LS1 1AA",
            phone="+44 113 111 5555",
            raw_website="",
            review_count=300,
            rating=4.8,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_source": "Tripadvisor",
                "review_evidence_date": self.recent_date,
                "review_confidence": "CONFLICT",
                "review_status": "CONFLICT_REQUIRES_REVIEW"
            }
        }
        social_audit = {"verified_urls": {}, "social_ownership_status": "UNKNOWN", "social_profile_status": "UNKNOWN", "social_activity": "UNKNOWN"}

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(qual_res["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    def test_counterfactual_06_low_reviews_high_rating_strong_phone(self):
        """Matrix 6: low reviews (<50) + high rating + strong phone -> fails review gate (<50), NOT ACTIVE_CONFIRMED & NOT OUTREACH_READY"""
        biz = DiscoveredBusiness(
            company_name="Matrix Eatery 6",
            category="Bistro",
            city="Leeds",
            target_country="United Kingdom",
            address="30 Main St, Leeds LS1 1AA",
            phone="+44 113 111 6666",
            raw_website="",
            review_count=35,  # Under 50 threshold
            rating=4.9,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {"review_enrichment": {"review_source": "Tripadvisor", "review_evidence_date": self.recent_date}}
        social_audit = {"verified_urls": {}, "social_ownership_status": "UNKNOWN", "social_profile_status": "UNKNOWN", "social_activity": "UNKNOWN"}

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(qual_res["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    def test_counterfactual_07_high_reviews_low_rating_current_phone(self):
        """Matrix 7: high reviews (150) + low rating (<4.0) + current phone -> fails rating gate (<4.0), NOT ACTIVE_CONFIRMED & NOT OUTREACH_READY"""
        biz = DiscoveredBusiness(
            company_name="Matrix Eatery 7",
            category="Takeaway",
            city="Leeds",
            target_country="United Kingdom",
            address="40 Main St, Leeds LS1 1AA",
            phone="+44 113 111 7777",
            raw_website="",
            review_count=150,
            rating=3.7,  # Under 4.0 threshold
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {"review_enrichment": {"review_source": "Tripadvisor", "review_evidence_date": self.recent_date}}
        social_audit = {"verified_urls": {}, "social_ownership_status": "UNKNOWN", "social_profile_status": "UNKNOWN", "social_activity": "UNKNOWN"}

        op_res = self.validator.verify_operations(biz, social_audit)
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(qual_res["qualification_state"], QualificationState.MANUAL_REVIEW.value)

    def test_counterfactual_08_high_reviews_creator_content_only(self):
        """Matrix 8: high reviews + high rating + creator content only (no independent operational signal) -> NOT ACTIVE_CONFIRMED & NOT OUTREACH_READY"""
        biz = DiscoveredBusiness(
            company_name="Matrix Eatery 8",
            category="Dessert Bar",
            city="Leeds",
            target_country="United Kingdom",
            address="",
            phone="",
            raw_website="",
            review_count=220,
            rating=4.7,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {"review_enrichment": {"review_source": "Tripadvisor", "review_evidence_date": self.recent_date}}
        social_audit = {"verified_urls": {}, "social_ownership_status": "UNKNOWN", "social_profile_status": "UNKNOWN", "social_activity": "UNKNOWN"}
        creator_ev = {"creator_evidence_status": "FOUND", "creator_evidence_confidence": "HIGH", "creator_latest_date": self.recent_date, "platform": "instagram"}

        op_res = self.validator.verify_operations(biz, social_audit, creator_evidence=creator_ev)
        self.assertNotEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertNotEqual(qual_res["qualification_state"], QualificationState.OUTREACH_READY.value)

    # =========================================================================
    # PHASE 4.1: CLEAN VALIDATION & REGRESSION INVARIANTS
    # =========================================================================

    def test_missing_review_date_is_unknown_never_recent(self):
        """Phase 4.1 Invariant: missing review date (None/empty) is UNKNOWN, never RECENT, and fails Rule B"""
        from lib.enrichment.review_rating_enricher import ReviewRatingEnricher, ReviewFreshness

        # Enricher parsing check
        d_str, freshness = ReviewRatingEnricher.parse_date_and_freshness("See 143 unbiased reviews of Casa Di Alessia, rated 4.9 of 5 on Tripadvisor")
        self.assertIsNone(d_str)
        self.assertEqual(freshness, ReviewFreshness.UNKNOWN.value)
        self.assertNotEqual(freshness, ReviewFreshness.RECENT.value)

        # OperationalValidator check: missing date must prevent ACTIVE_CONFIRMED
        biz = DiscoveredBusiness(
            company_name="Casa Di Alessia",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="Market Street, Otley, LS21 3NX",
            phone="+44 113 244 5566",
            raw_website="",
            review_count=143,
            rating=4.9,
            latest_review_date=None,  # Missing date
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 143,
                "rating": 4.9,
                "review_source": "Tripadvisor",
                "review_evidence_date": None,
                "review_freshness": ReviewFreshness.UNKNOWN.value,
            }
        }
        social_audit = {"verified_urls": {}, "social_ownership_status": "UNKNOWN", "social_profile_status": "UNKNOWN", "social_activity": "UNKNOWN"}

        op_res = self.validator.verify_operations(biz, social_audit, website_verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(op_res["operational_status"], OperationalStatus.ACTIVE_LIKELY.value)
        self.assertFalse(op_res.get("multi_signal_rule_applied", False))

    def test_synthetic_run_date_never_substituted_for_evidence_date(self):
        """Phase 4.1 Invariant: Neither enricher nor validator must ever substitute current/run date for evidence date"""
        from lib.enrichment.review_rating_enricher import ReviewRatingEnricher, ReviewFreshness, ReviewEvidenceItem

        # Enricher resolve_best_evidence check
        item = ReviewEvidenceItem(
            review_count=143,
            rating=4.9,
            source="Tripadvisor",
            source_url="https://www.tripadvisor.com/test",
            evidence_text="A Casa di Alessia, Otley: See 143 unbiased reviews",
            evidence_date=None,
            confidence="HIGH",
            freshness=ReviewFreshness.UNKNOWN.value,
            business_match_tier="EXACT_LOCATION",
            review_provider="SEARXNG"
        )
        res = ReviewRatingEnricher.resolve_best_evidence([item])
        self.assertIsNone(res.review_evidence_date)
        self.assertEqual(res.review_freshness, ReviewFreshness.UNKNOWN.value)

        # Validator check: biz without date preserves None and does not generate current date
        biz = DiscoveredBusiness(
            company_name="Clean Data Eatery",
            category="Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="123 High Street, Leeds",
            phone="+44 113 999 8888",
            raw_website="",
            review_count=100,
            rating=4.8,
            latest_review_date=None,
            city_match=True
        )
        op_res = self.validator.verify_operations(biz, {})
        self.assertIsNone(biz.latest_review_date)
        self.assertNotEqual(op_res["evidence_freshness"], "RECENT")

    def test_active_confirmed_candidate_promoted_to_outreach_ready_despite_low_score(self):
        """Phase 5.1 Architecture Check: ACTIVE_CONFIRMED lead without social passes all 8 hard gates and becomes OUTREACH_READY with Priority.LOW (score 50)"""
        biz = DiscoveredBusiness(
            company_name="Casa Di Alessia",
            category="Italian Restaurant",
            city="Leeds",
            target_country="United Kingdom",
            address="Market Street, Otley, LS21 3NX",
            phone="+44 113 244 5566",
            raw_website="",
            review_count=143,
            rating=4.9,
            latest_review_date=self.recent_date,
            discovery_source="OPENSTREETMAP",
            city_match=True
        )
        biz.raw_data = {
            "review_enrichment": {
                "review_count": 143,
                "rating": 4.9,
                "review_source": "Tripadvisor",
                "review_evidence_date": self.recent_date,
                "review_freshness": "RECENT",
            }
        }
        social_audit = {"verified_urls": {}, "social_ownership_status": "UNKNOWN", "social_profile_status": "UNKNOWN", "social_activity": "UNKNOWN"}

        # 1. Operational verification passes Rule B
        op_res = self.validator.verify_operations(biz, social_audit, website_verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)
        self.assertEqual(op_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)
        self.assertTrue(op_res.get("multi_signal_rule_applied", False))

        # 2. Qualification evaluation
        qual_res = self.scorer.evaluate_lead(biz, verification_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value)

        # Verified: Passed all 8 hard gates
        self.assertEqual(qual_res["operational_status"], OperationalStatus.ACTIVE_CONFIRMED.value)

        # Score is 50 (< 65 threshold)
        self.assertEqual(qual_res["score"], 50)
        self.assertIn("reviews_100_to_199", qual_res["score_breakdown"])   # +15
        self.assertIn("rating_4_5_plus", qual_res["score_breakdown"])       # +10
        self.assertIn("established_physical_presence", qual_res["score_breakdown"]) # +10
        self.assertIn("booking_enquiry_opportunity", qual_res["score_breakdown"])   # +10
        self.assertIn("local_visibility", qual_res["score_breakdown"])      # +5

        # Qualification is decoupled from score: OUTREACH_READY achieved, priority is LOW
        self.assertEqual(qual_res["qualification_state"], QualificationState.OUTREACH_READY.value)
        self.assertEqual(qual_res["priority"], "LOW")
        self.assertTrue(qual_res["is_outreach_ready"])
        self.assertIn("verified active operations (ACTIVE_CONFIRMED)", qual_res["qualification_reason"])


if __name__ == "__main__":
    unittest.main()

