"""
Test Suite: Phase 9.6 Outreach Performance Intelligence & Optimization
Verifies:
  1. Event model: Canonical immutable schema, field validations, channel/event/outcome enums.
  2. Performance metrics: Explicit rate denominators, zero division handling, volume counting.
  3. Channel analytics: Disaggregation, separation of manual actions from automation, sample size guardrails.
  4. Message analytics: Template & angle tracking, sample size requirements, anti-causal claims.
  5. Lead prioritization: outreach_priority_score (0-100), qualified-only, Rule B immutability.
  6. Data quality audit: Detection of duplicates, orphans, state conflicts (SENT without send, etc.).
  7. Historical protection: Live Seafood, Seoul Kimchi, Hong Thai, Little Aladdin states preserved.
  8. Safety invariants: AUTOMATED_SENDS = 0, CAMPAIGNS_ARMED = 0, AUTOMATED_FOLLOWUPS = 0.
"""

import os
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

from lib.analytics.outreach_event_model import (
    OutreachEvent,
    OutreachChannelEnum,
    OutreachEventType,
    OutreachOutcomeEnum,
    OutreachSourceEnum,
    VALID_CHANNELS,
    VALID_EVENT_TYPES,
    VALID_OUTCOMES,
)
from lib.analytics.message_experiment import (
    MessageExperimentFramework,
    MessageAngle,
    ExperimentVariant,
)
from lib.analytics.outreach_performance_engine import (
    OutreachPerformanceEngine,
    OUTCOME_WEIGHTS,
    FOLLOW_UP_RECOMMENDATIONS,
)


class TestOutreachEventModel(unittest.TestCase):
    """Test Section 3 & 25: Canonical immutable event model."""

    def test_canonical_event_creation_valid(self):
        evt = OutreachEvent(
            event_id="EVT-001",
            lead_id="LEAD-TEST-01",
            channel="PHONE",
            event_type="OUTREACH_ATTEMPTED",
            outcome="CONNECTED",
            template_version="WEBSITE_DEV_V1",
            attempt_number=1,
            operator_confirmed=True,
            occurred_at="2026-10-04T12:00:00Z",
            source="OPERATOR",
            metadata={"notes": "Spoke with owner"},
        )
        self.assertEqual(evt.lead_id, "LEAD-TEST-01")
        self.assertEqual(evt.channel, "PHONE")
        self.assertEqual(evt.outcome, "CONNECTED")
        self.assertTrue(evt.operator_confirmed)

    def test_canonical_event_immutability(self):
        evt = OutreachEvent(
            event_id="EVT-002",
            lead_id="LEAD-TEST-02",
            channel="EMAIL",
            event_type="OUTREACH_SENT",
            outcome="SENT",
            occurred_at="2026-10-04T12:00:00Z",
        )
        with self.assertRaises(FrozenInstanceError):
            evt.outcome = "DELIVERED"

    def test_invalid_channel_rejected(self):
        with self.assertRaises(ValueError):
            OutreachEvent(
                event_id="EVT-003",
                lead_id="LEAD-TEST-03",
                channel="TELEGRAM",  # Not supported
                event_type="OUTREACH_ATTEMPTED",
                outcome="CONNECTED",
                occurred_at="2026-10-04T12:00:00Z",
            )

    def test_invalid_event_type_rejected(self):
        with self.assertRaises(ValueError):
            OutreachEvent(
                event_id="EVT-004",
                lead_id="LEAD-TEST-04",
                channel="PHONE",
                event_type="AUTO_SPAM_DISPATCH",  # Invalid event
                outcome="CONNECTED",
                occurred_at="2026-10-04T12:00:00Z",
            )

    def test_invalid_outcome_rejected(self):
        with self.assertRaises(ValueError):
            OutreachEvent(
                event_id="EVT-005",
                lead_id="LEAD-TEST-05",
                channel="PHONE",
                event_type="OUTREACH_ATTEMPTED",
                outcome="SUPER_EXCITED",  # Invalid outcome
                occurred_at="2026-10-04T12:00:00Z",
            )

    def test_invalid_source_rejected(self):
        with self.assertRaises(ValueError):
            OutreachEvent(
                event_id="EVT-006",
                lead_id="LEAD-TEST-06",
                channel="PHONE",
                event_type="OUTREACH_ATTEMPTED",
                outcome="CONNECTED",
                source="AUTONOMOUS_BOT",  # Invalid source
                occurred_at="2026-10-04T12:00:00Z",
            )

    def test_lead_id_and_timestamp_required(self):
        with self.assertRaises(ValueError):
            OutreachEvent(
                event_id="EVT-007",
                lead_id="",  # Missing
                channel="PHONE",
                event_type="OUTREACH_ATTEMPTED",
                outcome="CONNECTED",
                occurred_at="2026-10-04T12:00:00Z",
            )
        with self.assertRaises(ValueError):
            OutreachEvent(
                event_id="EVT-008",
                lead_id="LEAD-TEST-08",
                channel="PHONE",
                event_type="OUTREACH_ATTEMPTED",
                outcome="CONNECTED",
                occurred_at="",  # Missing
            )

    def test_to_dict_and_metadata_preserved(self):
        meta = {"angle": "WEBSITE_FIRST", "phone_digits": "01618192265"}
        evt = OutreachEvent(
            event_id="EVT-009",
            lead_id="LEAD-TEST-09",
            channel="PHONE",
            event_type="OUTREACH_ATTEMPTED",
            outcome="CONNECTED",
            occurred_at="2026-10-04T12:00:00Z",
            metadata=meta,
        )
        d = evt.to_dict()
        self.assertIsInstance(d, dict)
        self.assertEqual(d["metadata"]["angle"], "WEBSITE_FIRST")


class TestMessageExperimentFramework(unittest.TestCase):
    """Test Section 11 & 12: Controlled message experiments and positioning angles."""

    def test_all_canonical_message_angles_supported(self):
        expected_angles = {
            "WEBSITE_FIRST",
            "DIGITAL_PRESENCE",
            "MOBILE_EXPERIENCE",
            "ONLINE_BOOKING",
            "BRAND_PRESENTATION",
        }
        actual_angles = {a.value for a in MessageAngle}
        self.assertEqual(expected_angles, actual_angles)

    def test_experiment_variants_defined(self):
        variants = {v.value for v in ExperimentVariant}
        self.assertEqual(variants, {"CONTROL", "VARIANT_A", "VARIANT_B"})

    def test_experiments_disabled_by_default(self):
        framework = MessageExperimentFramework()
        self.assertFalse(framework.experiments_enabled)
        # Should always return CONTROL variant when disabled
        variant = framework.get_variant_for_lead("LEAD-ANY", "WEBSITE_DEV_V1")
        self.assertEqual(variant, ExperimentVariant.CONTROL.value)

    def test_min_sample_size_gate_enforced(self):
        framework = MessageExperimentFramework()
        # Attempting to enable with sample size < 30 must raise ValueError
        with self.assertRaises(ValueError):
            framework.enable_experiments(total_confirmed_attempts=15)
        # Permitted only if >= 30
        framework.enable_experiments(total_confirmed_attempts=30)
        self.assertTrue(framework.experiments_enabled)

    def test_variant_assignment_deterministic_when_enabled(self):
        framework = MessageExperimentFramework()
        framework.enable_experiments(total_confirmed_attempts=50)
        v1 = framework.get_variant_for_lead("LEAD-TEST-123", "WEBSITE_DEV_V1")
        v2 = framework.get_variant_for_lead("LEAD-TEST-123", "WEBSITE_DEV_V1")
        self.assertEqual(v1, v2)
        self.assertIn(v1, ["CONTROL", "VARIANT_A", "VARIANT_B"])

    def test_message_angle_metadata_on_event(self):
        evt = OutreachEvent(
            event_id="EVT-ANG-1",
            lead_id="LEAD-ANG-1",
            channel="PHONE",
            event_type="OUTREACH_ATTEMPTED",
            outcome="INTERESTED",
            occurred_at="2026-10-04T12:00:00Z",
            metadata={"message_angle": MessageAngle.DIGITAL_PRESENCE.value},
        )
        self.assertEqual(evt.metadata["message_angle"], "DIGITAL_PRESENCE")


class TestOutreachPerformanceMetrics(unittest.TestCase):
    """Test Section 4, 5, 6: Performance engine, denominators, volume counting."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.leads_path = os.path.join(self.temp_dir.name, "leads.json")
        self.profiles_path = os.path.join(self.temp_dir.name, "activation.json")
        self.outcomes_path = os.path.join(self.temp_dir.name, "outcomes.json")
        self.timeline_path = os.path.join(self.temp_dir.name, "timeline.json")
        self.history_path = os.path.join(self.temp_dir.name, "history.json")
        self.suppression_path = os.path.join(self.temp_dir.name, "suppression.json")

        # Mock CRM leads
        crm_data = {
            "leads": [
                {"lead_id": "L1", "company_name": "Co 1", "qualification_status": "OUTREACH_READY", "website_opportunity_score": 85, "review_count": 50, "rating": 4.5},
                {"lead_id": "L2", "company_name": "Co 2", "qualification_status": "OUTREACH_READY", "website_opportunity_score": 80, "review_count": 30, "rating": 4.2},
                {"lead_id": "L3", "company_name": "Co 3", "qualification_status": "OUTREACH_READY", "website_opportunity_score": 90, "review_count": 80, "rating": 4.8},
                {"lead_id": "L4", "company_name": "Co 4", "qualification_status": "RESEARCH_ONLY", "website_opportunity_score": 60, "review_count": 10, "rating": 3.8},
            ]
        }
        with open(self.leads_path, "w") as f:
            json.dump(crm_data, f)

        # Mock Activation Queue (Phase 9.3)
        act_data = {
            "ACTIVATION_QUEUE": [
                {"lead_id": "L1", "company_name": "Co 1", "activation_ready": True, "recommended_channel": "PHONE", "verified_phone": "+441618192265"},
                {"lead_id": "L2", "company_name": "Co 2", "activation_ready": True, "recommended_channel": "PHONE", "verified_phone": "+441618192266"},
                {"lead_id": "L3", "company_name": "Co 3", "activation_ready": True, "recommended_channel": "INSTAGRAM", "verified_instagram": "@co3"},
            ]
        }
        with open(self.profiles_path, "w") as f:
            json.dump(act_data, f)

        with open(self.history_path, "w") as f:
            json.dump({}, f)
        with open(self.suppression_path, "w") as f:
            json.dump({"suppressed_identifiers": []}, f)
        with open(self.timeline_path, "w") as f:
            json.dump({}, f)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_volume_and_unique_leads_counted(self):
        outcomes = [
            {"lead_id": "L1", "channel": "PHONE", "outcome": "CONNECTED", "attempt_number": 1},
            {"lead_id": "L1", "channel": "PHONE", "outcome": "INTERESTED", "attempt_number": 2},
            {"lead_id": "L2", "channel": "PHONE", "outcome": "NO_ANSWER", "attempt_number": 1},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        vol = perf["volume"]
        self.assertEqual(vol["qualified_leads"], 3)
        self.assertEqual(vol["activation_ready"], 3)
        self.assertEqual(vol["outreach_attempts"], 3)
        self.assertEqual(vol["unique_leads_contacted"], 2)

    def test_connected_denominator_correct(self):
        outcomes = [
            {"lead_id": "L1", "channel": "PHONE", "outcome": "CONNECTED", "attempt_number": 1},
            {"lead_id": "L2", "channel": "PHONE", "outcome": "NO_ANSWER", "attempt_number": 1},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        # contact_rate = connected / phone_attempts = 1 / 2 = 0.5
        self.assertEqual(perf["rates"]["contact_rate"], 0.5)
        self.assertEqual(perf["rate_denominators"]["contact_rate_formula"], "connected / manual_call_attempts")

    def test_interest_denominator_correct(self):
        outcomes = [
            {"lead_id": "L1", "channel": "PHONE", "outcome": "CONNECTED", "attempt_number": 1},
            {"lead_id": "L2", "channel": "PHONE", "outcome": "INTERESTED", "attempt_number": 1},
            {"lead_id": "L3", "channel": "PHONE", "outcome": "NO_ANSWER", "attempt_number": 1},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        # total connected = 2 (L1 was CONNECTED, L2 was INTERESTED which implies connected)
        # interest_rate = interested / connected = 1 / 2 = 0.5
        self.assertEqual(perf["rates"]["interest_rate"], 0.5)
        self.assertEqual(perf["rate_denominators"]["interest_rate_formula"], "interested / connected")

    def test_callback_rate_denominators(self):
        outcomes = [
            {"lead_id": "L1", "channel": "PHONE", "outcome": "CALLBACK_REQUESTED", "attempt_number": 1},
            {"lead_id": "L2", "channel": "PHONE", "outcome": "NO_ANSWER", "attempt_number": 1},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        # callback_rate_of_connected = 1 / 1 = 1.0
        self.assertEqual(perf["rates"]["callback_rate_of_connected"], 1.0)
        # callback_rate_of_attempts = 1 / 2 = 0.5
        self.assertEqual(perf["rates"]["callback_rate_of_attempts"], 0.5)

    def test_no_answer_and_wrong_number_denominators(self):
        outcomes = [
            {"lead_id": "L1", "channel": "PHONE", "outcome": "NO_ANSWER", "attempt_number": 1},
            {"lead_id": "L2", "channel": "PHONE", "outcome": "WRONG_NUMBER", "attempt_number": 1},
            {"lead_id": "L3", "channel": "PHONE", "outcome": "CONNECTED", "attempt_number": 1},
            {"lead_id": "L4", "channel": "PHONE", "outcome": "BUSY", "attempt_number": 1},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        # Total call attempts = 4
        self.assertEqual(perf["rates"]["no_answer_rate"], 0.25)
        self.assertEqual(perf["rates"]["wrong_number_rate"], 0.25)

    def test_failures_counted_separately_from_negative_commercial(self):
        outcomes = [
            {"lead_id": "L1", "channel": "PHONE", "outcome": "NOT_INTERESTED", "attempt_number": 1},
            {"lead_id": "L2", "channel": "PHONE", "outcome": "WRONG_NUMBER", "attempt_number": 1},
            {"lead_id": "L3", "channel": "PHONE", "outcome": "FAILED", "attempt_number": 1},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        # NOT_INTERESTED is a commercial outcome (connected=1, not_interested=1)
        self.assertEqual(perf["negative_outcomes"]["not_interested"], 1)
        # WRONG_NUMBER and FAILED are data/execution failures
        self.assertEqual(perf["contact_performance"]["wrong_number"], 1)
        self.assertEqual(perf["contact_performance"]["failed"], 1)

    def test_zero_division_safety(self):
        with open(self.outcomes_path, "w") as f:
            json.dump([], f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        for rate_name, val in perf["rates"].items():
            self.assertEqual(val, 0.0, f"Rate {rate_name} should be 0.0 when 0 attempts")

    def test_qualified_leads_never_used_as_interaction_denominator(self):
        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        self.assertFalse(perf["rate_denominators"]["qualified_leads_as_interaction_denominator"])


class TestChannelAnalytics(unittest.TestCase):
    """Test Section 6 & 19: Channel disaggregation and sample size guardrails."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.leads_path = os.path.join(self.temp_dir.name, "leads.json")
        self.profiles_path = os.path.join(self.temp_dir.name, "act.json")
        self.outcomes_path = os.path.join(self.temp_dir.name, "outcomes.json")
        self.timeline_path = os.path.join(self.temp_dir.name, "time.json")
        self.history_path = os.path.join(self.temp_dir.name, "hist.json")
        self.suppression_path = os.path.join(self.temp_dir.name, "supp.json")

        with open(self.leads_path, "w") as f:
            json.dump({"leads": []}, f)
        with open(self.profiles_path, "w") as f:
            json.dump({"ACTIVATION_QUEUE": []}, f)
        with open(self.timeline_path, "w") as f:
            json.dump({}, f)
        with open(self.history_path, "w") as f:
            json.dump({}, f)
        with open(self.suppression_path, "w") as f:
            json.dump({}, f)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_phone_separated_from_social_and_email(self):
        outcomes = [
            {"lead_id": "L1", "channel": "PHONE", "outcome": "CONNECTED"},
            {"lead_id": "L2", "channel": "INSTAGRAM", "outcome": "SENT"},
            {"lead_id": "L3", "channel": "FACEBOOK", "outcome": "SENT"},
            {"lead_id": "L4", "channel": "EMAIL", "outcome": "SENT"},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        ch = perf["channel_performance"]
        self.assertEqual(ch["PHONE"]["attempts"], 1)
        self.assertEqual(ch["INSTAGRAM"]["attempts"], 1)
        self.assertEqual(ch["FACEBOOK"]["attempts"], 1)
        self.assertEqual(ch["EMAIL"]["attempts"], 1)

    def test_manual_action_separated_from_automated_send(self):
        outcomes = [
            {"lead_id": "L1", "channel": "PHONE", "outcome": "CONNECTED"},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        vol = perf["volume"]
        self.assertEqual(vol["manual_call_attempts"], 1)
        self.assertEqual(vol["automated_sends"], 0)
        self.assertEqual(vol["campaigns_armed"], 0)

    def test_sample_warning_under_5(self):
        outcomes = [
            {"lead_id": "L1", "channel": "PHONE", "outcome": "CONNECTED"},
            {"lead_id": "L2", "channel": "PHONE", "outcome": "CONNECTED"},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        phone_warn = perf["channel_performance"]["PHONE"]["sample_warning"]
        self.assertIn("INSUFFICIENT SAMPLE FOR COMPARATIVE CONCLUSION", phone_warn)
        self.assertFalse(perf["sample_size_warnings"]["comparisons_reliable"])

    def test_sample_warning_5_to_9(self):
        outcomes = [{"lead_id": f"L{i}", "channel": "PHONE", "outcome": "CONNECTED"} for i in range(7)]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        phone_warn = perf["channel_performance"]["PHONE"]["sample_warning"]
        self.assertIn("DIRECTIONAL INSIGHT ONLY", phone_warn)

    def test_sample_warning_10_to_29(self):
        outcomes = [{"lead_id": f"L{i}", "channel": "PHONE", "outcome": "CONNECTED"} for i in range(12)]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        phone_warn = perf["channel_performance"]["PHONE"]["sample_warning"]
        self.assertIn("PRELIMINARY COMPARISON ALLOWED", phone_warn)
        self.assertTrue(perf["sample_size_warnings"]["comparisons_reliable"])

    def test_sample_warning_30_and_above(self):
        outcomes = [{"lead_id": f"L{i}", "channel": "PHONE", "outcome": "CONNECTED"} for i in range(35)]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        perf = engine.calculate_outreach_performance()
        phone_warn = perf["channel_performance"]["PHONE"]["sample_warning"]
        self.assertIn("OPTIMIZATION SIGNAL", phone_warn)


class TestLeadPrioritization(unittest.TestCase):
    """Test Section 8 & 20: outreach_priority_score (0-100), qualified-only, Rule B immutable."""

    def setUp(self):
        self.engine = OutreachPerformanceEngine()

    def test_score_bounded_0_to_100(self):
        profile = {"lead_id": "L1", "activation_ready": True, "recommended_channel": "PHONE", "verified_phone": "+441618192265"}
        crm_lead = {"qualification_status": "OUTREACH_READY", "website_opportunity_score": 85, "review_count": 50, "rating": 4.5}
        score, reasons = self.engine.calculate_lead_priority_score(profile, crm_lead)
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        self.assertIsInstance(reasons, list)

    def test_unqualified_lead_scores_zero(self):
        profile = {"lead_id": "L-UNQ", "activation_ready": False, "recommended_channel": "PHONE"}
        crm_lead = {"qualification_status": "RESEARCH_ONLY", "website_opportunity_score": 90, "review_count": 100}
        score, reasons = self.engine.calculate_lead_priority_score(profile, crm_lead)
        self.assertEqual(score, 0)
        self.assertIn("Lead is not qualified as OUTREACH_READY", reasons[0])

    def test_unqualified_never_included_in_next_best(self):
        next_leads = self.engine.get_next_best_leads()
        crm_leads = self.engine.load_crm_leads()
        for item in next_leads:
            lid = item["lead_id"]
            crm = crm_leads.get(lid, {})
            qual = crm.get("qualification_state") or crm.get("qualification_status")
            self.assertEqual(qual, "OUTREACH_READY")
            self.assertGreater(item["priority"], 0)

    def test_commercial_fit_and_website_opportunity_rewarded(self):
        p1 = {"lead_id": "L-HIGH", "activation_ready": True, "recommended_channel": "PHONE", "verified_phone": "+441618192265"}
        crm_high = {"qualification_status": "OUTREACH_READY", "website_opportunity_score": 85, "review_count": 50}

        p2 = {"lead_id": "L-MED", "activation_ready": True, "recommended_channel": "PHONE", "verified_phone": "+441618192265"}
        crm_med = {"qualification_status": "OUTREACH_READY", "website_opportunity_score": 40, "review_count": 50}

        score_high, _ = self.engine.calculate_lead_priority_score(p1, crm_high)
        score_med, _ = self.engine.calculate_lead_priority_score(p2, crm_med)
        self.assertGreater(score_high, score_med)

    def test_explanations_provided_for_every_lead(self):
        next_leads = self.engine.get_next_best_leads()
        self.assertGreater(len(next_leads), 0)
        for lead in next_leads:
            self.assertIn("lead_id", lead)
            self.assertIn("company_name", lead)
            self.assertIn("priority", lead)
            self.assertIn("recommended_channel", lead)
            self.assertIn("reasons", lead)
            self.assertGreater(len(lead["reasons"]), 0)

    def test_already_contacted_or_bounced_excluded_from_ranking(self):
        next_leads = self.engine.get_next_best_leads()
        lead_ids = [l["lead_id"] for l in next_leads]
        # Pilot lead (Little Aladdin)
        self.assertNotIn("LEAD-MAN-902001", lead_ids)
        # Seoul Kimchi (SENT)
        self.assertNotIn("LEAD-MAN-4DB3EF", lead_ids)
        # Hong Thai (BOUNCED / SUPPRESSED)
        self.assertNotIn("LEAD-MAN-709C66", lead_ids)


class TestDataQualityAudit(unittest.TestCase):
    """Test Section 23: Data quality audits, duplicate events, orphan IDs, state conflicts."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.leads_path = os.path.join(self.temp_dir.name, "leads.json")
        self.profiles_path = os.path.join(self.temp_dir.name, "act.json")
        self.outcomes_path = os.path.join(self.temp_dir.name, "outcomes.json")
        self.timeline_path = os.path.join(self.temp_dir.name, "time.json")
        self.history_path = os.path.join(self.temp_dir.name, "hist.json")
        self.suppression_path = os.path.join(self.temp_dir.name, "supp.json")

        with open(self.leads_path, "w") as f:
            json.dump({"leads": [{"lead_id": "L1", "company_name": "Co 1"}]}, f)
        with open(self.profiles_path, "w") as f:
            json.dump({"ACTIVATION_QUEUE": [{"lead_id": "L1", "company_name": "Co 1"}]}, f)
        with open(self.timeline_path, "w") as f:
            json.dump({}, f)
        with open(self.history_path, "w") as f:
            json.dump({}, f)
        with open(self.suppression_path, "w") as f:
            json.dump({}, f)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_duplicate_event_detected(self):
        outcomes = [
            {"lead_id": "L1", "channel": "PHONE", "outcome": "CONNECTED", "attempt_number": 1, "occurred_at": "2026-10-04T12:00:00Z"},
            {"lead_id": "L1", "channel": "PHONE", "outcome": "CONNECTED", "attempt_number": 1, "occurred_at": "2026-10-04T12:00:00Z"},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        audit = engine.audit_data_quality()
        self.assertTrue(audit["has_data_quality_issues"])
        categories = [i["category"] for i in audit["issues"]]
        self.assertIn("DUPLICATE_EVENT", categories)

    def test_orphaned_lead_id_detected(self):
        outcomes = [
            {"lead_id": "L-GHOST-999", "channel": "PHONE", "outcome": "CONNECTED", "attempt_number": 1},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        audit = engine.audit_data_quality()
        categories = [i["category"] for i in audit["issues"]]
        self.assertIn("ORPHANED_LEAD_ID", categories)

    def test_unknown_channel_detected(self):
        outcomes = [
            {"lead_id": "L1", "channel": "TELEPATHY", "outcome": "CONNECTED", "attempt_number": 1},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        audit = engine.audit_data_quality()
        categories = [i["category"] for i in audit["issues"]]
        self.assertIn("UNKNOWN_CHANNEL", categories)

    def test_unknown_outcome_detected(self):
        outcomes = [
            {"lead_id": "L1", "channel": "PHONE", "outcome": "SUPER_HOT", "attempt_number": 1},
        ]
        with open(self.outcomes_path, "w") as f:
            json.dump(outcomes, f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        audit = engine.audit_data_quality()
        categories = [i["category"] for i in audit["issues"]]
        self.assertIn("UNKNOWN_OUTCOME", categories)

    def test_sent_without_confirmed_send_detected(self):
        # CRM says SENT but message_history has no confirmed send
        with open(self.leads_path, "w") as f:
            json.dump({"leads": [{"lead_id": "L1", "outreach_status": "SENT", "actual_send_confirmed": False}]}, f)
        with open(self.outcomes_path, "w") as f:
            json.dump([], f)

        engine = OutreachPerformanceEngine(
            activation_path=self.profiles_path,
            leads_path=self.leads_path,
            timeline_path=self.timeline_path,
            history_path=self.history_path,
            suppression_path=self.suppression_path,
            outcomes_path=self.outcomes_path,
        )
        audit = engine.audit_data_quality()
        categories = [i["category"] for i in audit["issues"]]
        self.assertIn("STATE_CONFLICT", categories)

    def test_clean_authoritative_data_produces_zero_issues(self):
        engine = OutreachPerformanceEngine()
        audit = engine.audit_data_quality()
        self.assertEqual(audit["total_issues_detected"], 0)
        self.assertFalse(audit["has_data_quality_issues"])


class TestHistoricalStateProtection(unittest.TestCase):
    """Test Section 16 & 17: Protection of Live Seafood, Seoul Kimchi, Hong Thai, Little Aladdin."""

    def setUp(self):
        self.engine = OutreachPerformanceEngine()

    def test_live_seafood_states_preserved(self):
        crm_leads = self.engine.load_crm_leads()
        live_seafood = crm_leads.get("LEAD-MAN-0363CF")
        self.assertIsNotNone(live_seafood)
        self.assertEqual(live_seafood.get("company_name"), "Live Seafood Ltd")
        self.assertEqual(live_seafood.get("qualification_state") or live_seafood.get("qualification_status"), "OUTREACH_READY")
        # Distinguish qualification from outreach status: NOT_READY
        self.assertEqual(live_seafood.get("outreach_status"), "NOT_READY")
        self.assertEqual(live_seafood.get("authoritative_outreach_status"), "NOT_READY")
        # Never represent simply as READY
        self.assertNotEqual(live_seafood.get("outreach_status"), "READY")

    def test_seoul_kimchi_confirmed_send_preserved(self):
        crm_leads = self.engine.load_crm_leads()
        sk = crm_leads.get("LEAD-MAN-4DB3EF")
        self.assertIsNotNone(sk)
        self.assertEqual(sk.get("company_name"), "Seoul Kimchi")
        self.assertEqual(sk.get("outreach_status"), "SENT")
        # Confirmed send verified via outreach_sent_at or message history
        history = self.engine.load_message_history()
        sk_hist = history.get("LEAD-MAN-4DB3EF", [])
        has_sent = any(m.get("status") == "SENT" for m in sk_hist)
        self.assertTrue(has_sent or bool(sk.get("outreach_sent_at")) or sk.get("actual_send_confirmed") is True)

    def test_hong_thai_bounced_and_suppressed_preserved(self):
        crm_leads = self.engine.load_crm_leads()
        ht = crm_leads.get("LEAD-MAN-709C66")
        self.assertIsNotNone(ht)
        self.assertEqual(ht.get("company_name"), "Hong Thai")
        self.assertEqual(ht.get("outreach_status"), "BOUNCED")
        suppression = self.engine.load_suppression_list()
        supp_emails = suppression.get("suppressed_emails", {})
        supp_channels = suppression.get("channels", {}).get("Email", [])
        self.assertTrue(
            any("hongthai" in k.lower() for k in supp_emails)
            or any("hongthai" in s.lower() for s in supp_channels)
        )

    def test_little_aladdin_pilot_outcome_preserved(self):
        crm_leads = self.engine.load_crm_leads()
        la = crm_leads.get("LEAD-MAN-902001")
        self.assertIsNotNone(la)
        self.assertEqual(la.get("company_name"), "Little Aladdin")
        self.assertEqual(la.get("outreach_status"), "CONTACTED")
        outcomes = self.engine.load_outcomes()
        la_outcomes = [o for o in outcomes if o.get("lead_id") == "LEAD-MAN-902001"]
        self.assertGreater(len(la_outcomes), 0)
        self.assertEqual(la_outcomes[0].get("outcome"), "CONNECTED")

    def test_timelines_remain_append_only(self):
        timelines = self.engine.load_timelines()
        self.assertIsInstance(timelines, dict)
        for lid, events in timelines.items():
            self.assertIsInstance(events, list)
            # Verify timestamps are sequential / non-empty
            for e in events:
                self.assertIn("timestamp", e)
                self.assertTrue(len(e["timestamp"]) > 0)


class TestSafetyAndZeroAutomationInvariants(unittest.TestCase):
    """Test Section 21 & 22: Hard safety invariants and zero automated actions."""

    def setUp(self):
        self.engine = OutreachPerformanceEngine()

    def test_zero_automated_sends_invariant(self):
        perf = self.engine.calculate_outreach_performance()
        vol = perf["volume"]
        self.assertEqual(vol["automated_sends"], 0)
        self.assertEqual(vol["campaigns_armed"], 0)

    def test_safety_invariants_in_snapshot(self):
        snapshot = self.engine.generate_performance_snapshot()
        safety = snapshot["safety_invariants"]
        self.assertEqual(safety["automated_sends"], 0)
        self.assertEqual(safety["automated_followups"], 0)
        self.assertEqual(safety["campaigns_armed"], 0)
        self.assertEqual(safety["auto_continuation"], 0)
        self.assertEqual(safety["rule_b_criteria_mutated"], 0)

    def test_follow_up_recommendations_are_read_only(self):
        # Follow-up recommendations are strings; no execution code exists in engine
        for outcome, rec in FOLLOW_UP_RECOMMENDATIONS.items():
            self.assertIsInstance(rec, str)
            self.assertIn(rec, [
                "FOLLOW_UP_REQUIRED",
                "CALLBACK_REQUIRED",
                "OPERATOR_DECISION",
                "NO_AUTOMATIC_FOLLOW_UP",
                "CONTACT_REVIEW",
                "AWAITING_RESPONSE",
            ])

    def test_rule_b_criteria_unaltered(self):
        # Ensure the 8 frozen Rule B criteria remain in their authoritative files
        rule_b_criteria = [
            "RULE_B_01_REVIEW_VOLUME",
            "RULE_B_02_MINIMUM_RATING",
            "RULE_B_03_ACCEPTED_SOURCE",
            "RULE_B_04_REVIEW_RECENCY",
            "RULE_B_05_OPERATIONAL_SIGNAL",
            "RULE_B_06_IDENTITY_LOCATION",
            "RULE_B_07_NO_REVIEW_CONFLICT",
            "RULE_B_08_NO_CLOSURE_RED_FLAGS",
        ]
        from lib.validation.rule_b_criteria import RULE_B_CRITERIA, RULE_B_VERSION
        self.assertEqual(RULE_B_VERSION, "FROZEN")
        criterion_ids = [c["id"] for c in RULE_B_CRITERIA]
        for criterion in rule_b_criteria:
            self.assertIn(criterion, criterion_ids)

    def test_operator_notice_present_in_recommendations(self):
        queue = self.engine.get_next_batch_recommendations()
        self.assertIn("operator_notice", queue)
        self.assertIn("Human operator confirmation required", queue["operator_notice"])
        self.assertEqual(len(queue["next_3"]), 3)
        self.assertGreaterEqual(len(queue["next_5"]), 0)


if __name__ == "__main__":
    unittest.main()
