
"""Deterministic, isolated fixtures for commercial workflow tests."""

import json
import os


def seed_commercial_test_fixtures(
    *,
    leads_path,
    outcomes_path,
    commercial_records_path,
    timelines_path,
    commercial_events_path,
    packages_path=None,
):
    """Build a stable historical dataset only inside the test temp directory."""
    specifications = [
        ("LEAD-MAN-3B9091", "The Old Monkey", "OUTREACH_READY", "SENT"),
        ("LEAD-MAN-902001", "Little Aladdin", "OUTREACH_READY", "SENT"),
        ("LEAD-MAN-E81185", "Manchester Shawarma", "OUTREACH_READY", "SENT"),
        ("LEAD-MAN-4098E1", "Dog and Partridge", "OUTREACH_READY", "SENT"),
        ("LEAD-MAN-0363CF", "Live Seafood Ltd", "OUTREACH_READY", "NOT_READY"),
        ("LEAD-MAN-4DB3EF", "Seoul Kimchi", "OUTREACH_READY", "SENT"),
        ("LEAD-MAN-709C66", "Hong Thai", "EXCLUDED", "SUPPRESSED"),
        # Legacy identifiers retained by the Phase 10.0 tests.
        ("LEAD-MAN-682E5D", "Live Seafood Ltd", "OUTREACH_READY", "NOT_READY"),
        ("LEAD-MAN-44F10A", "Seoul Kimchi", "OUTREACH_READY", "SENT"),
    ]

    leads = [
        {
            "lead_id": lead_id,
            "company_name": name,
            "city": "Manchester",
            "country": "United Kingdom",
            "qualification_state": qualification,
            "outreach_status": outreach,
            "date_added": "2026-09-01T12:00:00Z",
            "outreach_sent_at": (
                "2026-10-15T12:00:00Z" if outreach == "SENT" else None
            ),
        }
        for lead_id, name, qualification, outreach in specifications
    ]

    outcomes = [
        {
            "lead_id": "LEAD-MAN-902001",
            "outcome": "CONNECTED",
            "channel": "PHONE",
            "source": "OPERATOR_REPORTED",
            "operator_confirmed": True,
            "recorded_at": "2026-10-15T12:00:00Z",
        },
        {
            "lead_id": "LEAD-MAN-4098E1",
            "outcome": "NO_ANSWER",
            "channel": "PHONE",
            "source": "OPERATOR_REPORTED",
            "operator_confirmed": True,
            "recorded_at": "2026-10-15T11:00:00Z",
        },
    ]

    def write_json(path, value):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)

    write_json(leads_path, {"leads": leads})
    write_json(outcomes_path, outcomes)
    write_json(timelines_path, {})
    write_json(commercial_events_path, [])

    # Force the manager to bootstrap canonical historical states from the
    # deterministic fixtures instead of inheriting an empty copied data file.
    if os.path.exists(commercial_records_path):
        os.remove(commercial_records_path)

    if packages_path:
        write_json(packages_path, {
            "packages": {
                "STARTER": {
                    "price": 750.0,
                    "currency": "GBP",
                    "scope": ["Responsive website", "Location"],
                },
                "STANDARD": {
                    "price": 1250.0,
                    "currency": "GBP",
                    "scope": ["Responsive website", "Gallery"],
                },
                "PREMIUM": {
                    "price": 1950.0,
                    "currency": "GBP",
                    "scope": ["Responsive website", "CMS"],
                },
                "CUSTOM": {
                    "price": 0.0,
                    "currency": "GBP",
                    "scope": [],
                },
            }
        })
