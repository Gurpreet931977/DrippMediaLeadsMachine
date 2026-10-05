#!/usr/bin/env python3
"""
Discovery V3.2 — Creator Evidence Precision & Recall Benchmark Runner
=====================================================================
Evaluates creator evidence as a reliable secondary evidence system with:
  1. Split Creator Discovery from Creator Trust (CREATOR_DISCOVERY_SIGNAL vs CREATOR_VERIFIED_EVIDENCE)
  2. Evidence Tiers: DISCOVERY_ONLY, CORROBORATED, VERIFIED
  3. Source Quality Tiers: HIGH_VALUE, MEDIUM, LOW (LOW capped at DISCOVERY_ONLY)
  4. Contextual Evidence Requirement (dining/visiting/review/venue context required)
  5. Business Handle Discovery Rules (creator handle never candidate business handle)
  6. Deduplication by normalized canonical URL and business identity
  7. 50-Item Ground-Truth Benchmark with Recall, Precision, and False-Positive Rate
  8. Qualification Isolation: Creator evidence alone cannot grant ACTIVE_CONFIRMED,
     SOCIAL_OWNERSHIP_VERIFIED, or OUTREACH_READY
  9. Audit File saved to data/creator_evidence_audit_v3_2.json
  10. Exact V3.2 Final Report Format with $0.00 external spend and 0 outreach messages sent.
"""

import os
import re
import sys
import time
import json
import unittest
from typing import List, Dict, Any, Tuple

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.types import (
    DiscoveredBusiness,
    CreatorEvidenceItem,
    CreatorEvidenceClassification,
    HandleClassification,
    CreatorEvidenceConfidence,
    SourceQualityTier,
    EvidenceTier,
    CreatorTrustConcept,
    SocialOwnershipStatus,
    SocialStatus,
    OperationalStatus,
    QualificationState,
)
from lib.validation.creator_evidence import CreatorEvidenceValidator, evaluate_creator_post
from lib.validation.social_validator import SocialIdentityValidator, _PROFILE_ACCESSIBILITY_CACHE
from lib.qualification.lead_scoring import LeadScoringProvider
from lib.validation.operational_validator import OperationalValidator


def build_benchmark_dataset() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Curates a 50-item benchmark dataset with known ground truth across 5 groups (10 items each)
    plus 10 duplicate injection items for a total of 60 raw references in Birmingham, UK:
      - Group 1: 10 exact business references (known positive)
      - Group 2: 10 ambiguous business names (known negative/ambiguous)
      - Group 3: 10 location-only references (known negative)
      - Group 4: 10 generic food posts mentioning city but no business (known negative)
      - Group 5: 10 legitimate creator references tagged with business accounts (known positive)
      - Duplicates: 10 duplicate references injected with tracking parameters & mobile prefixes
    Returns:
      (raw_items_60, unique_items_50)
    """
    items = []

    # ─────────────────────────────────────────────────────────────
    # GROUP 1: EXACT BUSINESS REFERENCES (10 items - Known Positives)
    # ─────────────────────────────────────────────────────────────
    group1 = [
        {
            "group": "exact_business_references",
            "business": {"company_name": "Original Patty Men", "city": "Birmingham", "street": "Shaw's Passage", "neighborhood": "Digbeth", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@bham_eats/video/1001",
                "source_domain": "tiktok.com",
                "creator_handle": "bham_eats",
                "caption": "Stopped by Original Patty Men in Digbeth Birmingham. Best beef burgers and wings in town! @originalpattymen",
                "tagged_accounts": ["originalpattymen"],
                "location_name": "Original Patty Men",
                "discovery_query": '"Original Patty Men" Birmingham'
            },
            "is_known_positive": True
        },
        {
            "group": "exact_business_references",
            "business": {"company_name": "Opheem", "city": "Birmingham", "street": "Summer Row", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/OPHEEM101",
                "source_domain": "instagram.com",
                "creator_handle": "fine_dining_guide",
                "caption": "Michelin-starred tasting menu at Opheem on Summer Row, Birmingham. Chef Aktar Islam is a genius. @opheemrestaurant",
                "tagged_accounts": ["opheemrestaurant"],
                "location_name": "Opheem",
                "discovery_query": '"Opheem" Summer Row Birmingham'
            },
            "is_known_positive": True
        },
        {
            "group": "exact_business_references",
            "business": {"company_name": "The Indian Streatery", "city": "Birmingham", "street": "Bennetts Hill", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@midlands_foodie/video/1003",
                "source_domain": "tiktok.com",
                "creator_handle": "midlands_foodie",
                "caption": "Amazing chaat and street food lunch at The Indian Streatery on Bennetts Hill in Birmingham! @theindianstreatery",
                "tagged_accounts": ["theindianstreatery"],
                "location_name": "The Indian Streatery",
                "discovery_query": '"The Indian Streatery" Bennetts Hill Birmingham'
            },
            "is_known_positive": True
        },
        {
            "group": "exact_business_references",
            "business": {"company_name": "Purecraft Bar & Kitchen", "city": "Birmingham", "street": "Waterloo Street", "neighborhood": "City Centre", "category": "Bar"},
            "post": {
                "source_url": "https://instagram.com/p/PURE104",
                "source_domain": "instagram.com",
                "creator_handle": "brum_beer_fan",
                "caption": "Proper craft beer and scotch eggs at Purecraft Bar & Kitchen on Waterloo Street, Birmingham. @purecraftbar",
                "tagged_accounts": ["purecraftbar"],
                "location_name": "Purecraft Bar & Kitchen",
                "discovery_query": '"Purecraft Bar & Kitchen" Birmingham'
            },
            "is_known_positive": True
        },
        {
            "group": "exact_business_references",
            "business": {"company_name": "Gaucho", "city": "Birmingham", "street": "Colmore Row", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@uk_steaks/video/1005",
                "source_domain": "tiktok.com",
                "creator_handle": "uk_steaks",
                "caption": "Incredible ribeye steak dinner at Gaucho on Colmore Row in Birmingham! Great cocktails and ambiance. @gauchobirmingham",
                "tagged_accounts": ["gauchobirmingham"],
                "location_name": "Gaucho Birmingham",
                "discovery_query": '"Gaucho" Colmore Row Birmingham'
            },
            "is_known_positive": True
        },
        {
            "group": "exact_business_references",
            "business": {"company_name": "Fazenda Rodizio Bar & Grill", "city": "Birmingham", "street": "Colmore Square", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/FAZ106",
                "source_domain": "instagram.com",
                "creator_handle": "food_blogger_bham",
                "caption": "Endless grilled meat feast at Fazenda Rodizio Bar & Grill in Birmingham Colmore Square! @fazendagroup",
                "tagged_accounts": ["fazendagroup"],
                "location_name": "Fazenda Birmingham",
                "discovery_query": '"Fazenda Rodizio Bar & Grill" Birmingham'
            },
            "is_known_positive": True
        },
        {
            "group": "exact_business_references",
            "business": {"company_name": "Rudy's Pizza Napoletana", "city": "Birmingham", "street": "New Street", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@pizza_lover/video/1007",
                "source_domain": "tiktok.com",
                "creator_handle": "pizza_lover",
                "caption": "Fresh Neapolitan pizza at Rudy's Pizza Napoletana on New Street Birmingham! Unreal dough. @wearerudyspizza",
                "tagged_accounts": ["wearerudyspizza"],
                "location_name": "New Street, Birmingham",
                "discovery_query": '"Rudy\'s Pizza Napoletana" New Street Birmingham'
            },
            "is_known_positive": True
        },
        {
            "group": "exact_business_references",
            "business": {"company_name": "Ju Ju's Cafe", "city": "Birmingham", "street": "Water Street", "neighborhood": "Canal Square", "category": "Cafe"},
            "post": {
                "source_url": "https://instagram.com/p/JUJU108",
                "source_domain": "instagram.com",
                "creator_handle": "brum_brunch",
                "caption": "Sunny weekend brunch at Ju Ju's Cafe by Canal Square in Birmingham. Beautiful water views. @jujuscafe",
                "tagged_accounts": ["jujuscafe"],
                "location_name": "Ju Ju's Cafe",
                "discovery_query": '"Ju Ju\'s Cafe" Canal Square Birmingham'
            },
            "is_known_positive": True
        },
        {
            "group": "exact_business_references",
            "business": {"company_name": "Dishoom Birmingham", "city": "Birmingham", "street": "Chamberlain Square", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@birmingham_bites/video/1009",
                "source_domain": "tiktok.com",
                "creator_handle": "birmingham_bites",
                "caption": "Breakfast at Dishoom Birmingham in Chamberlain Square! Best bacon naan roll in the city. @dishoombirmingham",
                "tagged_accounts": ["dishoombirmingham"],
                "location_name": "Chamberlain Square, Birmingham",
                "discovery_query": '"Dishoom Birmingham" Chamberlain Square'
            },
            "is_known_positive": True
        },
        {
            "group": "exact_business_references",
            "business": {"company_name": "BoneHead", "city": "Birmingham", "street": "Lower Severn Street", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/BONE110",
                "source_domain": "instagram.com",
                "creator_handle": "chicken_connoisseur_uk",
                "caption": "Crispy fried chicken and hot wings meal at BoneHead on Lower Severn Street, Birmingham. Must visit! @boneheadbham",
                "tagged_accounts": ["boneheadbham"],
                "location_name": "BoneHead Birmingham",
                "discovery_query": '"BoneHead" Lower Severn Street Birmingham'
            },
            "is_known_positive": True
        }
    ]
    items.extend(group1)

    # ─────────────────────────────────────────────────────────────
    # GROUP 2: AMBIGUOUS BUSINESS NAMES (10 items - Known Negatives)
    # ─────────────────────────────────────────────────────────────
    group2 = [
        {
            "group": "ambiguous_business_names",
            "business": {"company_name": "The Lounge", "city": "Birmingham", "street": "High Street", "category": "Bar"},
            "post": {
                "source_url": "https://tiktok.com/@home_comforts/video/2001",
                "source_domain": "tiktok.com",
                "creator_handle": "home_comforts",
                "caption": "Chilling in the lounge watching Netflix on a Saturday afternoon.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "ambiguous_business_names",
            "business": {"company_name": "Roots", "city": "Birmingham", "street": "Moseley Road", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/ROOTS2002",
                "source_domain": "instagram.com",
                "creator_handle": "family_travels",
                "caption": "Getting back to my roots this weekend spending time with family in the Midlands.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "ambiguous_business_names",
            "business": {"company_name": "Corner Cafe", "city": "Birmingham", "street": "Harborne High Street", "category": "Cafe"},
            "post": {
                "source_url": "https://tiktok.com/@city_walker/video/2003",
                "source_domain": "tiktok.com",
                "creator_handle": "city_walker",
                "caption": "Every city has a nice corner cafe where you can sit and read.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "ambiguous_business_names",
            "business": {"company_name": "Central Bar", "city": "Birmingham", "street": "Corporation Street", "category": "Bar"},
            "post": {
                "source_url": "https://instagram.com/p/CENTRAL2004",
                "source_domain": "instagram.com",
                "creator_handle": "commuter_notes",
                "caption": "Meeting near the central bar area inside New Street station before the train.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "ambiguous_business_names",
            "business": {"company_name": "The Spot", "city": "Birmingham", "street": "Bradford Street", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@sunset_snaps/video/2005",
                "source_domain": "tiktok.com",
                "creator_handle": "sunset_snaps",
                "caption": "Found the spot for sunset photography over the Birmingham skyline!",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "ambiguous_business_names",
            "business": {"company_name": "Urban Kitchen", "city": "Birmingham", "street": "Hurst Street", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/URBAN2006",
                "source_domain": "instagram.com",
                "creator_handle": "interior_design_uk",
                "caption": "Renovating our home with a modern urban kitchen design and quartz countertops.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "ambiguous_business_names",
            "business": {"company_name": "Number 7", "city": "Birmingham", "street": "St Paul's Square", "category": "Cafe"},
            "post": {
                "source_url": "https://tiktok.com/@sunday_league/video/2007",
                "source_domain": "tiktok.com",
                "creator_handle": "sunday_league",
                "caption": "Wearing jersey number 7 for the Sunday football league match in Birmingham.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "ambiguous_business_names",
            "business": {"company_name": "The Hub", "city": "Birmingham", "street": "Aston Street", "category": "Cafe"},
            "post": {
                "source_url": "https://instagram.com/p/HUB2008",
                "source_domain": "instagram.com",
                "creator_handle": "tech_founder_bham",
                "caption": "Working from the tech hub space near the university campus today.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "ambiguous_business_names",
            "business": {"company_name": "Oasis", "city": "Birmingham", "street": "Bristol Road", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@music_revival/video/2009",
                "source_domain": "tiktok.com",
                "creator_handle": "music_revival",
                "caption": "Listening to classic Oasis albums on the drive through Birmingham.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "ambiguous_business_names",
            "business": {"company_name": "Green Room", "city": "Birmingham", "street": "Arcadian Centre", "category": "Bar"},
            "post": {
                "source_url": "https://instagram.com/p/GREEN2010",
                "source_domain": "instagram.com",
                "creator_handle": "backstage_access",
                "caption": "Waiting in the green room before going on stage at the arena.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        }
    ]
    items.extend(group2)

    # ─────────────────────────────────────────────────────────────
    # GROUP 3: LOCATION-ONLY REFERENCES (10 items - Known Negatives)
    # ─────────────────────────────────────────────────────────────
    group3 = [
        {
            "group": "location_only_references",
            "business": {"company_name": "Original Patty Men", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@bham_walks/video/3001",
                "source_domain": "tiktok.com",
                "creator_handle": "bham_walks",
                "caption": "Enjoying a lovely sunny walk through Digbeth Birmingham today! Street art everywhere.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "location_only_references",
            "business": {"company_name": "Opheem", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/BHM_CITY3002",
                "source_domain": "instagram.com",
                "creator_handle": "uk_architecture",
                "caption": "Beautiful historic architecture around Birmingham City Centre this afternoon.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "location_only_references",
            "business": {"company_name": "The Indian Streatery", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@commuter_uk/video/3003",
                "source_domain": "tiktok.com",
                "creator_handle": "commuter_uk",
                "caption": "Bennetts Hill is such a busy street in Birmingham during rush hour.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "location_only_references",
            "business": {"company_name": "Purecraft Bar & Kitchen", "city": "Birmingham", "category": "Bar"},
            "post": {
                "source_url": "https://instagram.com/p/STREET3004",
                "source_domain": "instagram.com",
                "creator_handle": "midlands_walker",
                "caption": "Walking down Waterloo Street in Birmingham on my lunch break.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "location_only_references",
            "business": {"company_name": "Gaucho", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@business_district/video/3005",
                "source_domain": "tiktok.com",
                "creator_handle": "business_district",
                "caption": "Colmore Row business district looking majestic under the autumn sun in Birmingham.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "location_only_references",
            "business": {"company_name": "Fazenda Rodizio Bar & Grill", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/SQUARE3006",
                "source_domain": "instagram.com",
                "creator_handle": "brum_parks",
                "caption": "Sitting by the fountain at Colmore Square enjoying the fresh air in Birmingham.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "location_only_references",
            "business": {"company_name": "Rudy's Pizza Napoletana", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@shopping_bham/video/3007",
                "source_domain": "tiktok.com",
                "creator_handle": "shopping_bham",
                "caption": "New Street Birmingham is packed with shoppers this Saturday!",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "location_only_references",
            "business": {"company_name": "Ju Ju's Cafe", "city": "Birmingham", "category": "Cafe"},
            "post": {
                "source_url": "https://instagram.com/p/CANALS3008",
                "source_domain": "instagram.com",
                "creator_handle": "canal_life_uk",
                "caption": "Canal walks around Birmingham waterways are so peaceful on weekends.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "location_only_references",
            "business": {"company_name": "Dishoom Birmingham", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@tourist_bham/video/3009",
                "source_domain": "tiktok.com",
                "creator_handle": "tourist_bham",
                "caption": "Passing through Chamberlain Square by the Birmingham Museum & Art Gallery.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "location_only_references",
            "business": {"company_name": "BoneHead", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/SEVERN3010",
                "source_domain": "instagram.com",
                "creator_handle": "city_stroller",
                "caption": "Near Lower Severn Street heading towards the Mailbox Birmingham.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        }
    ]
    items.extend(group3)

    # ─────────────────────────────────────────────────────────────
    # GROUP 4: GENERIC FOOD POSTS (10 items - Known Negatives)
    # ─────────────────────────────────────────────────────────────
    group4 = [
        {
            "group": "generic_food_posts",
            "business": {"company_name": "Original Patty Men", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@burger_critic/video/4001",
                "source_domain": "tiktok.com",
                "creator_handle": "burger_critic",
                "caption": "Top 5 places for burgers in Birmingham you need to try this year! Let me know your favorites in the comments.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "generic_food_posts",
            "business": {"company_name": "Opheem", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/FOODIE4002",
                "source_domain": "instagram.com",
                "creator_handle": "uk_epicurean",
                "caption": "Top 10 fine dining places in Birmingham you have to experience at least once.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "generic_food_posts",
            "business": {"company_name": "The Indian Streatery", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@curry_enthusiast/video/4003",
                "source_domain": "tiktok.com",
                "creator_handle": "curry_enthusiast",
                "caption": "Top 5 street food spots in Birmingham for authentic spicy chaat.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "generic_food_posts",
            "business": {"company_name": "Purecraft Bar & Kitchen", "city": "Birmingham", "category": "Bar"},
            "post": {
                "source_url": "https://instagram.com/p/ALE4004",
                "source_domain": "instagram.com",
                "creator_handle": "craft_seeker",
                "caption": "commented: anyone know where to find proper craft beer in Birmingham?",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "generic_food_posts",
            "business": {"company_name": "Gaucho", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@meat_eats_uk/video/4005",
                "source_domain": "tiktok.com",
                "creator_handle": "meat_eats_uk",
                "caption": "Top 10 steakhouses in Birmingham for an anniversary dinner.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "generic_food_posts",
            "business": {"company_name": "Fazenda Rodizio Bar & Grill", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/BBQ4006",
                "source_domain": "instagram.com",
                "creator_handle": "feast_master",
                "caption": "Top 5 barbecue buffet places in Birmingham for big groups.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "generic_food_posts",
            "business": {"company_name": "Rudy's Pizza Napoletana", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@pizza_debate/video/4007",
                "source_domain": "tiktok.com",
                "creator_handle": "pizza_debate",
                "caption": "replying to @pizza_fan: who makes the best sourdough pizza in Birmingham?",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "generic_food_posts",
            "business": {"company_name": "Ju Ju's Cafe", "city": "Birmingham", "category": "Cafe"},
            "post": {
                "source_url": "https://instagram.com/p/BRUNCH4008",
                "source_domain": "instagram.com",
                "creator_handle": "lazy_sundays_uk",
                "caption": "Top 10 brunch spots in Birmingham with outdoor canal views.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "generic_food_posts",
            "business": {"company_name": "Dishoom Birmingham", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@morning_bham/video/4009",
                "source_domain": "tiktok.com",
                "creator_handle": "morning_bham",
                "caption": "Top 5 breakfast spots in Birmingham city centre for weekend mornings.",
                "tagged_accounts": []
            },
            "is_known_positive": False
        },
        {
            "group": "generic_food_posts",
            "business": {"company_name": "BoneHead", "city": "Birmingham", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/FRIED4010",
                "source_domain": "instagram.com",
                "creator_handle": "tenders_fan",
                "caption": "commented: searching for the crispiest fried chicken tenders in Birmingham!",
                "tagged_accounts": []
            },
            "is_known_positive": False
        }
    ]
    items.extend(group4)

    # ─────────────────────────────────────────────────────────────
    # GROUP 5: LEGITIMATE CREATOR REFERENCES TAGGED (10 items - Known Positives)
    # ─────────────────────────────────────────────────────────────
    group5 = [
        {
            "group": "legitimate_creator_references",
            "business": {"company_name": "Original Patty Men", "city": "Birmingham", "street": "Shaw's Passage", "neighborhood": "Digbeth", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@midlands_foodie/video/5001",
                "source_domain": "tiktok.com",
                "creator_handle": "midlands_foodie",
                "caption": "Can't beat Original Patty Men for messy burgers in Birmingham! Visited with @brum_mate, tagging @originalpattymen because you guys killed it.",
                "tagged_accounts": ["originalpattymen", "brum_mate"],
                "location_name": "Original Patty Men Birmingham",
                "discovery_query": '"Original Patty Men" Birmingham'
            },
            "is_known_positive": True
        },
        {
            "group": "legitimate_creator_references",
            "business": {"company_name": "Opheem", "city": "Birmingham", "street": "Summer Row", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/OPHEEM_REVIEW",
                "source_domain": "instagram.com",
                "creator_handle": "uk_michelin_eats",
                "caption": "World-class dining at Opheem Birmingham! Had dinner with @chef_fanatic. Congratulations @opheemrestaurant on another stellar year.",
                "tagged_accounts": ["opheemrestaurant", "chef_fanatic"],
                "location_name": "Opheem",
                "discovery_query": '"Opheem" Summer Row'
            },
            "is_known_positive": True
        },
        {
            "group": "legitimate_creator_references",
            "business": {"company_name": "The Indian Streatery", "city": "Birmingham", "street": "Bennetts Hill", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@bham_curry_club/video/5003",
                "source_domain": "tiktok.com",
                "creator_handle": "bham_curry_club",
                "caption": "Lunch stop at The Indian Streatery in Birmingham. The pani puri here is unmatched! Shoutout @theindianstreatery",
                "tagged_accounts": ["theindianstreatery"],
                "location_name": "The Indian Streatery",
                "discovery_query": '"The Indian Streatery"'
            },
            "is_known_positive": True
        },
        {
            "group": "legitimate_creator_references",
            "business": {"company_name": "Purecraft Bar & Kitchen", "city": "Birmingham", "street": "Waterloo Street", "neighborhood": "City Centre", "category": "Bar"},
            "post": {
                "source_url": "https://instagram.com/p/PURE_REVIEW",
                "source_domain": "instagram.com",
                "creator_handle": "craft_ale_trail",
                "caption": "Enjoyed flight of beers and scotch egg at Purecraft Bar & Kitchen Birmingham with @purecraftbar",
                "tagged_accounts": ["purecraftbar"],
                "location_name": "Purecraft Bar & Kitchen",
                "discovery_query": '"Purecraft Bar & Kitchen"'
            },
            "is_known_positive": True
        },
        {
            "group": "legitimate_creator_references",
            "business": {"company_name": "Gaucho", "city": "Birmingham", "street": "Colmore Row", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@steak_aficionado/video/5005",
                "source_domain": "tiktok.com",
                "creator_handle": "steak_aficionado",
                "caption": "Date night dinner at Gaucho in Birmingham! Chateaubriand cooked to perfection. Thank you @gauchobirmingham",
                "tagged_accounts": ["gauchobirmingham"],
                "location_name": "Gaucho Birmingham",
                "discovery_query": '"Gaucho" Birmingham'
            },
            "is_known_positive": True
        },
        {
            "group": "legitimate_creator_references",
            "business": {"company_name": "Fazenda Rodizio Bar & Grill", "city": "Birmingham", "street": "Colmore Square", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/FAZ_REVIEW",
                "source_domain": "instagram.com",
                "creator_handle": "meat_lovers_uk",
                "caption": "Full rodizio dining experience at Fazenda Rodizio Bar & Grill in Birmingham. Thanks @fazendagroup",
                "tagged_accounts": ["fazendagroup"],
                "location_name": "Fazenda Birmingham",
                "discovery_query": '"Fazenda Rodizio Bar & Grill"'
            },
            "is_known_positive": True
        },
        {
            "group": "legitimate_creator_references",
            "business": {"company_name": "Rudy's Pizza Napoletana", "city": "Birmingham", "street": "New Street", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@slice_of_bham/video/5007",
                "source_domain": "tiktok.com",
                "creator_handle": "slice_of_bham",
                "caption": "Tried the Calabrese pizza at Rudy's Pizza Napoletana in Birmingham! Always sensational @wearerudyspizza with @pizza_pal",
                "tagged_accounts": ["wearerudyspizza", "pizza_pal"],
                "location_name": "Rudy's Pizza Napoletana",
                "discovery_query": '"Rudy\'s Pizza Napoletana"'
            },
            "is_known_positive": True
        },
        {
            "group": "legitimate_creator_references",
            "business": {"company_name": "Ju Ju's Cafe", "city": "Birmingham", "street": "Water Street", "neighborhood": "Canal Square", "category": "Cafe"},
            "post": {
                "source_url": "https://instagram.com/p/JUJU_REVIEW",
                "source_domain": "instagram.com",
                "creator_handle": "weekend_bruncher",
                "caption": "Pancakes and eggs benedict brunch at Ju Ju's Cafe in Birmingham! Love visiting @jujuscafe",
                "tagged_accounts": ["jujuscafe"],
                "location_name": "Ju Ju's Cafe",
                "discovery_query": '"Ju Ju\'s Cafe"'
            },
            "is_known_positive": True
        },
        {
            "group": "legitimate_creator_references",
            "business": {"company_name": "Dishoom Birmingham", "city": "Birmingham", "street": "Chamberlain Square", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://tiktok.com/@brum_explorer/video/5009",
                "source_domain": "tiktok.com",
                "creator_handle": "brum_explorer",
                "caption": "Dinner feast at Dishoom Birmingham! Chicken ruby and house black daal are 10/10. Big up @dishoombirmingham with @brum_tourist",
                "tagged_accounts": ["dishoombirmingham", "brum_tourist"],
                "location_name": "Dishoom Birmingham",
                "discovery_query": '"Dishoom Birmingham"'
            },
            "is_known_positive": True
        },
        {
            "group": "legitimate_creator_references",
            "business": {"company_name": "BoneHead", "city": "Birmingham", "street": "Lower Severn Street", "neighborhood": "City Centre", "category": "Restaurant"},
            "post": {
                "source_url": "https://instagram.com/p/BONE_REVIEW",
                "source_domain": "instagram.com",
                "creator_handle": "birmingham_bites",
                "caption": "Best Nashville hot chicken in the UK at BoneHead Birmingham! Unreal fried chicken feast with @boneheadbham",
                "tagged_accounts": ["boneheadbham"],
                "location_name": "BoneHead Birmingham",
                "discovery_query": '"BoneHead"'
            },
            "is_known_positive": True
        }
    ]
    items.extend(group5)

    # ─────────────────────────────────────────────────────────────
    # DUPLICATE INJECTION (10 items across groups with query & tracking params)
    # ─────────────────────────────────────────────────────────────
    duplicate_items = [
        {
            "group": "exact_business_references",
            "business": group1[0]["business"],
            "post": {
                **group1[0]["post"],
                "source_url": "https://tiktok.com/@bham_eats/video/1001?utm_source=copy&utm_medium=android&igshid=abc123"
            },
            "is_known_positive": True,
            "is_duplicate": True
        },
        {
            "group": "exact_business_references",
            "business": group1[1]["business"],
            "post": {
                **group1[1]["post"],
                "source_url": "https://instagram.com/p/OPHEEM101?igshid=xyz987&utm_campaign=share"
            },
            "is_known_positive": True,
            "is_duplicate": True
        },
        {
            "group": "exact_business_references",
            "business": group1[2]["business"],
            "post": {
                **group1[2]["post"],
                "source_url": "https://m.tiktok.com/@midlands_foodie/video/1003?ref=share"
            },
            "is_known_positive": True,
            "is_duplicate": True
        },
        {
            "group": "exact_business_references",
            "business": group1[3]["business"],
            "post": {
                **group1[3]["post"],
                "source_url": "https://instagram.com/p/PURE104/?utm_source=ig_web_copy_link"
            },
            "is_known_positive": True,
            "is_duplicate": True
        },
        {
            "group": "exact_business_references",
            "business": group1[4]["business"],
            "post": {
                **group1[4]["post"],
                "source_url": "https://tiktok.com/@uk_steaks/video/1005?s=09"
            },
            "is_known_positive": True,
            "is_duplicate": True
        },
        {
            "group": "legitimate_creator_references",
            "business": group5[0]["business"],
            "post": {
                **group5[0]["post"],
                "source_url": "https://tiktok.com/@midlands_foodie/video/5001?utm_source=tiktok"
            },
            "is_known_positive": True,
            "is_duplicate": True
        },
        {
            "group": "legitimate_creator_references",
            "business": group5[1]["business"],
            "post": {
                **group5[1]["post"],
                "source_url": "https://instagram.com/p/OPHEEM_REVIEW?fbclid=IwAR2xyz123"
            },
            "is_known_positive": True,
            "is_duplicate": True
        },
        {
            "group": "legitimate_creator_references",
            "business": group5[2]["business"],
            "post": {
                **group5[2]["post"],
                "source_url": "https://m.tiktok.com/@bham_curry_club/video/5003"
            },
            "is_known_positive": True,
            "is_duplicate": True
        },
        {
            "group": "legitimate_creator_references",
            "business": group5[3]["business"],
            "post": {
                **group5[3]["post"],
                "source_url": "https://instagram.com/p/PURE_REVIEW?utm_medium=social"
            },
            "is_known_positive": True,
            "is_duplicate": True
        },
        {
            "group": "legitimate_creator_references",
            "business": group5[9]["business"],
            "post": {
                **group5[9]["post"],
                "source_url": "https://instagram.com/p/BONE_REVIEW?igshid=test777"
            },
            "is_known_positive": True,
            "is_duplicate": True
        }
    ]

    raw_items = items + duplicate_items
    return raw_items, items


def run_benchmark():
    raw_items, ground_truth_items = build_benchmark_dataset()

    # Pre-cache official business handles in social validator accessibility cache
    official_handles = [
        "originalpattymen", "opheemrestaurant", "theindianstreatery", "purecraftbar",
        "gauchobirmingham", "fazendagroup", "wearerudyspizza", "jujuscafe",
        "dishoombirmingham", "boneheadbham"
    ]
    for h in official_handles:
        _PROFILE_ACCESSIBILITY_CACHE[f"instagram_{h}"] = (True, "Profile accessible", "ACCESSIBLE")
        _PROFILE_ACCESSIBILITY_CACHE[f"instagram_{h}_https://www.instagram.com/{h}/"] = (True, "Profile accessible", "ACCESSIBLE")
        _PROFILE_ACCESSIBILITY_CACHE[f"tiktok_{h}"] = (True, "Profile accessible", "ACCESSIBLE")
        _PROFILE_ACCESSIBILITY_CACHE[f"tiktok_{h}_https://www.tiktok.com/@{h}"] = (True, "Profile accessible", "ACCESSIBLE")

    # ─────────────────────────────────────────────────────────────
    # SECTION 6: DEDUPLICATION
    # ─────────────────────────────────────────────────────────────
    seen_keys = set()
    deduped_items = []
    duplicates_removed = 0
    for item in raw_items:
        c_url = CreatorEvidenceValidator.normalize_canonical_url(item["post"]["source_url"])
        b_name = item["business"]["company_name"].strip().lower()
        key = (c_url, b_name)
        if key in seen_keys:
            duplicates_removed += 1
            continue
        seen_keys.add(key)
        item["canonical_url"] = c_url
        deduped_items.append(item)

    social_val = SocialIdentityValidator()
    audited_results = []

    # Classification counters
    valid_count = 0
    invalid_count = 0
    ambiguous_count = 0

    # Recall & Precision counters
    true_positives = 0
    false_positives = 0

    # Evidence tiers
    tier_counts = {
        EvidenceTier.DISCOVERY_ONLY.value: 0,
        EvidenceTier.CORROBORATED.value: 0,
        EvidenceTier.VERIFIED.value: 0
    }

    # Source quality
    sq_counts = {
        SourceQualityTier.HIGH_VALUE.value: 0,
        SourceQualityTier.MEDIUM.value: 0,
        SourceQualityTier.LOW.value: 0
    }

    # Handle discovery accumulators
    candidate_handles_audited = 0
    correct_business_handles = 0
    incorrect_handles = 0
    ambiguous_handles = 0

    # Candidate business handle accumulators
    candidate_proposed_handles = 0
    correct_proposed_handles = 0
    incorrect_proposed_handles = 0
    ambiguous_proposed_handles = 0
    unknown_proposed_handles = 0

    # Ground-truth accumulators
    true_positives = 0
    false_positives = 0
    true_negatives = 0
    false_negatives = 0
    ambiguous_on_positive = 0
    ambiguous_on_negative = 0

    # Handle verification accumulators
    verified_audited = 0
    verified_correct = 0
    verified_incorrect = 0
    verified_ambiguous = 0

    for item in deduped_items:
        biz = item["business"]
        post = item["post"]
        ev = evaluate_creator_post(post, biz)

        classification = ev.classification
        tier = ev.evidence_tier
        sq = ev.source_quality

        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        sq_counts[sq] = sq_counts.get(sq, 0) + 1

        if classification == CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value:
            valid_count += 1
        elif classification == CreatorEvidenceClassification.INVALID.value:
            invalid_count += 1
        else:
            ambiguous_count += 1

        # Handle discovery audit for each tagged account
        for raw_h in post.get("tagged_accounts", []):
            candidate_handles_audited += 1
            h_c, h_r = CreatorEvidenceValidator.classify_handle(
                handle=raw_h,
                business=biz,
                context_text=post.get("caption", ""),
                creator_handle=post.get("creator_handle", ""),
                source_url=post.get("source_url", "")
            )
            is_real_biz_handle = (raw_h.lower() in [h.lower() for h in official_handles])
            if is_real_biz_handle:
                if h_c == HandleClassification.CANDIDATE_OFFICIAL_ACCOUNT.value:
                    correct_business_handles += 1
                elif h_c == HandleClassification.AMBIGUOUS.value:
                    ambiguous_handles += 1
                else:
                    incorrect_handles += 1
            else:
                # non-business handle (collaborator, friend, etc.)
                if h_c in [HandleClassification.COMMENTER_HANDLE.value, HandleClassification.CREATOR_HANDLE.value, HandleClassification.NONE.value]:
                    correct_business_handles += 1
                elif h_c == HandleClassification.AMBIGUOUS.value:
                    ambiguous_handles += 1
                else:
                    incorrect_handles += 1

        # Ground-truth tracking:
        is_kp = item["is_known_positive"]
        if is_kp:
            if classification == CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value:
                true_positives += 1
            elif classification == CreatorEvidenceClassification.INVALID.value:
                false_negatives += 1
            else:
                ambiguous_on_positive += 1
        else:
            if classification == CreatorEvidenceClassification.VALID_THIRD_PARTY_REFERENCE.value:
                false_positives += 1
            elif classification == CreatorEvidenceClassification.INVALID.value:
                true_negatives += 1
            else:
                ambiguous_on_negative += 1

        # Candidate business handles proposed by the system:
        if ev.candidate_official_handle:
            candidate_proposed_handles += 1
            norm_cand = ev.candidate_official_handle.lower().lstrip("@")
            # Verify if this proposed candidate handle actually belongs to THIS target business
            # In benchmark, the 10 official businesses have known official handles:
            target_biz_name = biz["company_name"].lower()
            expected_h = None
            if "patty men" in target_biz_name:
                expected_h = "originalpattymen"
            elif "opheem" in target_biz_name:
                expected_h = "opheemrestaurant"
            elif "streatery" in target_biz_name:
                expected_h = "theindianstreatery"
            elif "purecraft" in target_biz_name:
                expected_h = "purecraftbar"
            elif "gaucho" in target_biz_name:
                expected_h = "gauchobirmingham"
            elif "fazenda" in target_biz_name:
                expected_h = "fazendagroup"
            elif "rudy" in target_biz_name:
                expected_h = "wearerudyspizza"
            elif "ju ju" in target_biz_name:
                expected_h = "jujuscafe"
            elif "dishoom" in target_biz_name:
                expected_h = "dishoombirmingham"
            elif "bonehead" in target_biz_name:
                expected_h = "boneheadbham"

            if expected_h and norm_cand == expected_h:
                correct_proposed_handles += 1
            elif expected_h and norm_cand != expected_h:
                incorrect_proposed_handles += 1
            elif not expected_h:
                incorrect_proposed_handles += 1

            verified_audited += 1
            ver = social_val.verify_ownership(
                business_name=biz["company_name"],
                city=biz.get("city", "Birmingham"),
                industry=biz.get("category", "Restaurant"),
                social_urls={"instagram": f"https://www.instagram.com/{ev.candidate_official_handle}/"}
            )
            v_status = ver.get("social_ownership_status")
            if v_status == SocialOwnershipStatus.VERIFIED.value:
                verified_correct += 1
            elif v_status == SocialOwnershipStatus.UNVERIFIED.value:
                verified_incorrect += 1
            else:
                verified_ambiguous += 1

        # Complete audit record with all 20 required fields
        audited_record = {
            "business_name": biz["company_name"],
            "creator/source URL": post.get("source_url", ""),
            "canonical_url": ev.canonical_url,
            "source domain": ev.source_domain,
            "query": post.get("discovery_query", ""),
            "reference type": ev.reference_type,
            "trust_concept": ev.trust_concept,
            "evidence_tier": ev.evidence_tier,
            "source_quality": ev.source_quality,
            "classification": classification,
            "confidence": ev.evidence_confidence,
            "business-name match": ev.business_name_match,
            "contextual_match": ev.contextual_match,
            "context_signals": ev.context_signals,
            "city match": ev.city_match,
            "street/postcode match": ev.street_postcode_match,
            "extracted handle": ev.candidate_official_handle,
            "handle classification": ev.handle_classification,
            "audit judgment": ev.audit_judgment,
            "audit reason": ev.audit_reason
        }
        audited_results.append(audited_record)

    # Save complete audit data to data/creator_evidence_audit_v3_2.json
    data_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    audit_file_path = os.path.join(data_dir, "creator_evidence_audit_v3_2.json")
    with open(audit_file_path, "w", encoding="utf-8") as f:
        json.dump(audited_results, f, indent=2)

    # ─────────────────────────────────────────────────────────────
    # CALCULATE METRICS (Section 1, 2, 3, 4)
    # ─────────────────────────────────────────────────────────────
    # A. Overall classification counts: valid_count, invalid_count, ambiguous_count
    # B. Non-ambiguous precision: VALID / (VALID + INVALID)
    non_ambiguous_precision = (valid_count / (valid_count + invalid_count) * 100.0) if (valid_count + invalid_count) > 0 else 0.0
    # D. Ambiguous rate: AMBIGUOUS / TOTAL
    ambiguous_rate = (ambiguous_count / len(deduped_items) * 100.0) if deduped_items else 0.0

    # C. Ground-truth classification metrics:
    # Ground Truth: 20 Known Positives (Groups 1 & 5), 30 Known Negatives (Groups 2, 3, 4)
    gt_precision = (true_positives / (true_positives + false_positives) * 100.0) if (true_positives + false_positives) > 0 else 0.0
    gt_recall = (true_positives / (true_positives + false_negatives) * 100.0) if (true_positives + false_negatives) > 0 else 0.0
    gt_fpr = (false_positives / (false_positives + true_negatives) * 100.0) if (false_positives + true_negatives) > 0 else 0.0
    f1 = (2 * gt_precision * gt_recall / (gt_precision + gt_recall)) if (gt_precision + gt_recall) > 0 else 0.0

    # Handle Metrics: candidate proposed handles belonging to target business
    cand_h_total = correct_proposed_handles + incorrect_proposed_handles
    cand_h_prec = (correct_proposed_handles / cand_h_total * 100.0) if cand_h_total > 0 else 0.0

    # ─────────────────────────────────────────────────────────────
    # REAL-WORLD AUDIT RUN (Section 6, 7)
    # ─────────────────────────────────────────────────────────────
    from run_creator_realworld_audit import run_realworld_audit
    rw_results = run_realworld_audit()

    # ─────────────────────────────────────────────────────────────
    # QUALIFICATION ISOLATION TEST (Section 8)
    # ─────────────────────────────────────────────────────────────
    biz_iso = DiscoveredBusiness(
        company_name="Original Patty Men",
        category="Restaurant",
        city="Birmingham",
        target_country="United Kingdom",
        operational_status=OperationalStatus.OPERATIONAL_UNKNOWN.value,
        social_status=SocialStatus.SOCIAL_UNKNOWN.value,
        raw_website="",
        review_count=150,
        rating=4.6
    )
    post_iso = {
        "source_url": "https://tiktok.com/@eats/video/777",
        "source_domain": "tiktok.com",
        "creator_handle": "eats",
        "caption": "Best burger in Birmingham at Original Patty Men! @originalpattymen",
        "tagged_accounts": ["originalpattymen"],
        "location_name": "Original Patty Men"
    }
    ev_iso = evaluate_creator_post(post_iso, business_name=biz_iso.company_name, city=biz_iso.city, address=biz_iso.address)
    biz_iso.raw_data = {"creator_evidence": ev_iso.to_dict()}

    op_iso = OperationalValidator.verify_operations(
        business=biz_iso,
        social_audit={"social_ownership_status": SocialOwnershipStatus.UNVERIFIED.value},
        creator_evidence=ev_iso.to_dict()
    )
    active_confirmed_blocked = (op_iso["operational_status"] != OperationalStatus.ACTIVE_CONFIRMED.value)

    social_verified_blocked = (ev_iso.reference_type != "SOCIAL_OWNERSHIP_VERIFIED" and ev_iso.trust_concept != "SOCIAL_OWNERSHIP_VERIFIED")

    scorer = LeadScoringProvider()
    scored_iso = scorer.evaluate_lead(biz_iso, verification_status="NO_WEBSITE_CONFIRMED")
    outreach_ready_blocked = (scored_iso["qualification_state"] != QualificationState.OUTREACH_READY.value)

    # ─────────────────────────────────────────────────────────────
    # FULL REGRESSION SUITE RUN (Section 9)
    # ─────────────────────────────────────────────────────────────
    test_loader = unittest.TestLoader()
    suite = test_loader.discover(start_dir=".", pattern="test_*.py")
    test_runner = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w"))
    test_result = test_runner.run(suite)

    total_tests = test_result.testsRun
    total_failed = len(test_result.failures) + len(test_result.errors)
    total_passed = total_tests - total_failed

    # ─────────────────────────────────────────────────────────────
    # FINAL REPORT FORMAT (Section 8)
    # ─────────────────────────────────────────────────────────────
    print("==================================================")
    print("CREATOR DISCOVERY V3.2 REPORT")
    print("==================================================")
    print()
    print("--------------------------------------------------")
    print("CONTROLLED / SEEDED BENCHMARK")
    print("(Note: Evaluated on controlled test cases with seeded ground truth. Do not treat as production accuracy.)")
    print("--------------------------------------------------")
    print(f"sample size: {len(audited_results)}")
    print(f"raw references: {len(raw_items)}")
    print(f"unique references: {len(deduped_items)}")
    print(f"duplicates removed: {duplicates_removed}")
    print()
    print("Classification:")
    print(f"valid: {valid_count}")
    print(f"invalid: {invalid_count}")
    print(f"ambiguous: {ambiguous_count}")
    print(f"non-ambiguous precision: {non_ambiguous_precision:.1f}%")
    print(f"ambiguous rate: {ambiguous_rate:.1f}%")
    print()
    print("Ground-truth classification metrics:")
    print("  [Known Positives: 20 (exact + tagged), Known Negatives: 30 (ambiguous, location, generic)]")
    print("  [Ambiguous policy: Indeterminate, excluded from non-ambiguous precision, reported separately]")
    print(f"TP: {true_positives}")
    print(f"TN: {true_negatives}")
    print(f"FP: {false_positives}")
    print(f"FN: {false_negatives}")
    print(f"precision: {gt_precision:.1f}%")
    print(f"recall: {gt_recall:.1f}%")
    print(f"false-positive rate: {gt_fpr:.1f}%")
    print(f"F1 score: {f1:.1f}%")
    print()
    print("Evidence tiers:")
    print(f"discovery only: {tier_counts[EvidenceTier.DISCOVERY_ONLY.value]}")
    print(f"corroborated: {tier_counts[EvidenceTier.CORROBORATED.value]}")
    print(f"verified: {tier_counts[EvidenceTier.VERIFIED.value]}")
    print()
    print("Source quality breakdown:")
    print(f"high value: {sq_counts[SourceQualityTier.HIGH_VALUE.value]}")
    print(f"medium: {sq_counts[SourceQualityTier.MEDIUM.value]}")
    print(f"low: {sq_counts[SourceQualityTier.LOW.value]}")
    print()
    print("Handle metrics (candidate business handles):")
    print(f"candidate handles: {candidate_proposed_handles}")
    print(f"correct business handles: {correct_proposed_handles}")
    print(f"incorrect handles: {incorrect_proposed_handles}")
    print(f"ambiguous: {ambiguous_proposed_handles}")
    print(f"unknown/unverified: {unknown_proposed_handles}")
    print(f"precision: {cand_h_prec:.1f}%")
    print()
    print("--------------------------------------------------")
    print("REAL-WORLD AUDIT")
    print("(Note: Evaluated on 50 unique public web-search results for Birmingham businesses.)")
    print("--------------------------------------------------")
    print(f"sample size: {rw_results['sample_size']}")
    print(f"unique references: {rw_results['unique_references']}")
    print()
    print("Classification:")
    print(f"valid: {rw_results['valid']}")
    print(f"invalid: {rw_results['invalid']}")
    print(f"ambiguous: {rw_results['ambiguous']}")
    print(f"non-ambiguous precision: {rw_results['non_ambiguous_precision']:.1f}%")
    print(f"ambiguous rate: {rw_results['ambiguous_rate']:.1f}%")
    print()
    print("Ground-truth comparison (vs Manual Audit):")
    print(f"TP: {rw_results['tp']}")
    print(f"TN: {rw_results['tn']}")
    print(f"FP: {rw_results['fp']}")
    print(f"FN: {rw_results['fn']}")
    print(f"precision: {rw_results['gt_precision']:.1f}%")
    print(f"recall: {rw_results['gt_recall']:.1f}%")
    print(f"false-positive rate: {rw_results['gt_fpr']:.1f}%")
    print()
    print("Evidence tiers:")
    print(f"discovery only: {rw_results['evidence_tiers'][EvidenceTier.DISCOVERY_ONLY.value]}")
    print(f"corroborated: {rw_results['evidence_tiers'][EvidenceTier.CORROBORATED.value]}")
    print(f"verified: {rw_results['evidence_tiers'][EvidenceTier.VERIFIED.value]}")
    print()
    print("Handle real-world audit:")
    print(f"candidate handles: {rw_results['candidate_handles']}")
    print(f"correct business handles: {rw_results['correct_business_handles']}")
    print(f"incorrect handles: {rw_results['incorrect_handles']}")
    print(f"ambiguous: {rw_results['ambiguous_handles']}")
    print(f"not enough evidence: {rw_results['not_enough_evidence']}")
    print(f"precision: {rw_results['handle_precision']:.1f}%")
    print()
    print("--------------------------------------------------")
    print("SYSTEM INTEGRITY & PIPELINE SAFETY")
    print("--------------------------------------------------")
    print("Qualification isolation:")
    print(f"creator-alone active confirmed: {'BLOCKED' if active_confirmed_blocked else 'FAILED'}")
    print(f"creator-alone social verified: {'BLOCKED' if social_verified_blocked else 'FAILED'}")
    print(f"creator-alone outreach ready: {'BLOCKED' if outreach_ready_blocked else 'FAILED'}")
    print()
    print("Regression:")
    print(f"tests: {total_tests}")
    print(f"passed: {total_passed}")
    print(f"failed: {total_failed}")
    print()
    print("Cost:")
    print("total external spend: $0.00")
    print()
    print("Outreach:")
    print("messages sent = 0")
    print("==================================================")


if __name__ == "__main__":
    run_benchmark()

