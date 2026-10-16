import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from lib.production.market_runner import (
    _business_review_freshness,
    _has_complete_current_review_evidence,
    _apply_recovered_review_evidence,
)


class ReviewResearchTriggerTests(unittest.TestCase):
    def make_business(self, count=120, rating=4.4, date=None, raw_data=None):
        return SimpleNamespace(
            company_name="Regression Test Restaurant",
            city="Manchester",
            review_count=count,
            rating=rating,
            latest_review_date=date,
            raw_data=raw_data or {},
        )

    def test_fresh_complete_evidence_skips_research(self):
        biz = self.make_business(date=datetime.now().date().isoformat())
        self.assertTrue(_has_complete_current_review_evidence(biz))

    def test_missing_rating_requires_research(self):
        biz = self.make_business(rating=None, date=datetime.now().date().isoformat())
        self.assertFalse(_has_complete_current_review_evidence(biz))

    def test_stale_evidence_requires_research(self):
        old = (datetime.now() - timedelta(days=240)).date().isoformat()
        biz = self.make_business(date=old)
        self.assertEqual(_business_review_freshness(biz), "STALE")
        self.assertFalse(_has_complete_current_review_evidence(biz))

    def test_missing_date_requires_research(self):
        self.assertFalse(_has_complete_current_review_evidence(self.make_business()))

    def test_enrichment_date_is_used_when_business_date_missing(self):
        today = datetime.now().date().isoformat()
        biz = self.make_business(
            date=None,
            raw_data={"review_enrichment": {"review_evidence_date": today}},
        )
        self.assertTrue(_has_complete_current_review_evidence(biz))

    def test_missing_recovery_values_do_not_erase_existing_values(self):
        biz = self.make_business(date="2026-10-10")
        result = SimpleNamespace(
            recovered_review_count=None,
            recovered_rating=None,
            recovered_date=None,
            to_dict=lambda: {"status": "REMAINED_UNKNOWN"},
        )
        _apply_recovered_review_evidence(biz, result)
        self.assertEqual(biz.review_count, 120)
        self.assertEqual(biz.rating, 4.4)
        self.assertEqual(biz.latest_review_date, "2026-10-10")
        self.assertEqual(biz.raw_data["review_recovery"]["status"], "REMAINED_UNKNOWN")

    def test_recovery_can_fill_missing_rating(self):
        today = datetime.now().date().isoformat()
        biz = self.make_business(rating=None)
        result = SimpleNamespace(
            recovered_review_count=120,
            recovered_rating=4.5,
            recovered_date=today,
            to_dict=lambda: {"status": "RECOVERED_RECENT"},
        )
        _apply_recovered_review_evidence(biz, result)
        self.assertEqual(biz.review_count, 120)
        self.assertEqual(biz.rating, 4.5)
        self.assertEqual(biz.latest_review_date, today)

    def test_stale_recovered_metrics_do_not_replace_recent_metrics(self):
        today = datetime.now().date().isoformat()
        old = (datetime.now() - timedelta(days=240)).date().isoformat()
        biz = self.make_business(count=120, rating=4.4, date=today)
        result = SimpleNamespace(
            recovered_review_count=45,
            recovered_rating=3.8,
            recovered_date=old,
            to_dict=lambda: {"status": "RECOVERED_STALE"},
        )

        _apply_recovered_review_evidence(biz, result)

        self.assertEqual(biz.review_count, 120)
        self.assertEqual(biz.rating, 4.4)
        self.assertEqual(biz.latest_review_date, today)

    def test_newer_recovered_metrics_replace_stale_metrics(self):
        today = datetime.now().date().isoformat()
        old = (datetime.now() - timedelta(days=240)).date().isoformat()
        biz = self.make_business(count=45, rating=4.0, date=old)
        result = SimpleNamespace(
            recovered_review_count=130,
            recovered_rating=4.6,
            recovered_date=today,
            to_dict=lambda: {"status": "RECOVERED_RECENT"},
        )

        _apply_recovered_review_evidence(biz, result)

        self.assertEqual(biz.review_count, 130)
        self.assertEqual(biz.rating, 4.6)
        self.assertEqual(biz.latest_review_date, today)

    def test_stale_recovered_date_does_not_replace_recent_date(self):
        today = datetime.now().date().isoformat()
        biz = self.make_business(date=today)
        old = (datetime.now() - timedelta(days=240)).date().isoformat()
        result = SimpleNamespace(
            recovered_review_count=None,
            recovered_rating=None,
            recovered_date=old,
            to_dict=lambda: {"status": "RECOVERED_STALE"},
        )
        _apply_recovered_review_evidence(biz, result)
        self.assertEqual(biz.latest_review_date, today)


    def test_recovered_current_evidence_updates_effective_freshness(self):
        old = (datetime.now() - timedelta(days=240)).date().isoformat()
        today = datetime.now().date().isoformat()
        biz = self.make_business(count=45, rating=4.0, date=old, raw_data={
            "review_enrichment": {
                "review_evidence_date": old, "review_freshness": "STALE",
                "review_status": "FOUND", "review_confidence": "MEDIUM",
            }
        })
        result = SimpleNamespace(
            status="RECOVERED_RECENT", recovered_review_count=130,
            recovered_rating=4.6, recovered_date=today,
            to_dict=lambda: {"status": "RECOVERED_RECENT"},
        )
        _apply_recovered_review_evidence(biz, result)
        meta = biz.raw_data["review_enrichment"]
        self.assertEqual(biz.review_count, 130)
        self.assertEqual(biz.rating, 4.6)
        self.assertEqual(meta["review_freshness"], "STALE")
        self.assertEqual(meta["effective_review_evidence_date"], today)
        self.assertNotEqual(meta["effective_review_freshness"], "STALE")

    def test_explicit_conflict_blocks_recovered_metrics(self):
        old = (datetime.now() - timedelta(days=240)).date().isoformat()
        today = datetime.now().date().isoformat()
        biz = self.make_business(count=45, rating=3.8, date=old, raw_data={
            "review_enrichment": {
                "review_evidence_date": old, "review_freshness": "STALE",
                "review_status": "CONFLICT_REQUIRES_REVIEW",
                "review_confidence": "CONFLICT",
            }
        })
        result = SimpleNamespace(
            status="RECOVERED_RECENT", recovered_review_count=140,
            recovered_rating=4.8, recovered_date=today,
            to_dict=lambda: {"status": "RECOVERED_RECENT"},
        )
        _apply_recovered_review_evidence(biz, result)
        self.assertEqual(biz.review_count, 45)
        self.assertEqual(biz.rating, 3.8)
        self.assertEqual(biz.latest_review_date, old)
        self.assertEqual(
            biz.raw_data["review_enrichment"]["review_confidence"], "CONFLICT"
        )


if __name__ == "__main__":
    unittest.main()
