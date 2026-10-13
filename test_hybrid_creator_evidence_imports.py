"""Regression test for creator evidence enum imports in the hybrid discovery engine."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from lib.discovery.hybrid import CreatorEvidenceValidator, HybridDiscoveryEngine
from lib.types import DiscoveredBusiness


class TestHybridCreatorEvidenceImports(unittest.TestCase):
    def test_found_creator_evidence_updates_transparency_without_name_error(self):
        engine = HybridDiscoveryEngine.__new__(HybridDiscoveryEngine)
        engine.web_search = MagicMock()
        engine.web_search.search_social_references.return_value = [{"title": "Creator reference"}]
        engine.transparency_stats = {
            "by_source": {"CREATOR_REFERENCES": 0},
            "creator_discovery": {
                "references_found": 0,
                "valid_references": 0,
                "high_confidence": 0,
                "medium_confidence": 0,
                "low_confidence": 0,
                "candidate_official_handles": 0,
                "independently_verified_official_handles": 0,
            },
        }
        candidate = DiscoveredBusiness(
            company_name="Test Restaurant",
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
        )
        summary = SimpleNamespace(
            creator_evidence_count=1,
            items=[SimpleNamespace(
                creator_evidence_status="FOUND",
                evidence_confidence="HIGH",
            )],
            discovered_official_handles=[],
            to_dict=lambda: {"creator_evidence_count": 1},
        )

        with (
            patch.object(CreatorEvidenceValidator, "should_search_creator_evidence", return_value=(True, "test")),
            patch.object(CreatorEvidenceValidator, "evaluate_content_item", return_value=object()),
            patch.object(CreatorEvidenceValidator, "summarize_evidence", return_value=summary),
        ):
            result = engine.enrich_candidate_creator_evidence(candidate, city="Manchester")

        self.assertIs(result, summary)
        self.assertEqual(engine.transparency_stats["creator_discovery"]["references_found"], 1)
        self.assertEqual(engine.transparency_stats["creator_discovery"]["valid_references"], 1)
        self.assertEqual(engine.transparency_stats["creator_discovery"]["high_confidence"], 1)
        self.assertEqual(candidate.raw_data["creator_evidence"]["creator_evidence_count"], 1)


if __name__ == "__main__":
    unittest.main()
