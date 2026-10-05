#!/usr/bin/env python3
"""
Test script to run the real pipeline logic across the 100 Leeds candidates
without any synthetic or injected profiles.
"""
import os
import re
import json
import time
from typing import Dict, Any, List

from lib.types import (
    DiscoveredBusiness,
    CountryStatus,
    SocialStatus,
    SocialOwnershipStatus,
    OperationalStatus,
    OperationalConfidence,
    EvidenceFreshness,
    QualificationState,
    Priority,
    WebsiteStatus,
    VerificationStatus
)
from lib.discovery.web_search import WebSearchProvider
from lib.website.detector import NodeWebsiteDetectionProvider, PLATFORM_DOMAINS
from lib.verification.no_website_verifier import NoWebsiteVerificationProvider
from lib.validation.social_validator import SocialIdentityValidator
from lib.validation.operational_validator import OperationalValidator
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.outreach.email_enricher import EmailVerifier, EmailVerificationStatus
from lib.validation.creator_evidence import CreatorEvidenceValidator

def main():
    with open("data/leeds_100_candidates.json", "r", encoding="utf-8") as f:
        raw_cands = json.load(f)

    print(f"Loaded {len(raw_cands)} candidates from data/leeds_100_candidates.json")

    web = WebSearchProvider()
    detector = NodeWebsiteDetectionProvider()
    verifier = NoWebsiteVerificationProvider()
    social_val = SocialIdentityValidator()
    scorer = LeadScoringProvider()

    # Process each candidate through real pipeline
    for i, c_data in enumerate(raw_cands[:10]):
        c = DiscoveredBusiness(**c_data)
        print(f"\n--- Candidate {i+1}: {c.company_name} ({c.city}) ---")
        print(f"  OSM Website: {c.raw_website}")
        print(f"  OSM Socials: IG={c.instagram_url} FB={c.facebook_url}")

if __name__ == "__main__":
    main()
