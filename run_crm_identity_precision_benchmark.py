#!/usr/bin/env python3
"""
Dripp Media — CRM Identity Matcher V2 Precision Benchmark
==========================================================
Evaluates precision and recall across:
  - 28 previously identified false-positive cases from Leeds audit
  - 10 true duplicate cases
  - 10 legitimate multi-location / branch cases
  - 10 clearly different business cases

Calculates:
  - true_duplicate_detection (%)
  - false_positive_rate (%)
  - legitimate_new_business_rate (%)
  - branch_separation_accuracy (%)
"""

import json
import os
import sys
from typing import Dict, Any, List, Tuple

from lib.crm.identity_matcher import BusinessIdentityMatcher, IdentityMatchOutcome


def load_leeds_candidates() -> Dict[str, Dict[str, Any]]:
    path = "data/leeds_100_candidates.json"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cands = json.load(f)
            return {c.get("company_name", ""): c for c in cands}
    return {}


def get_benchmark_datasets():
    leeds_lookup = load_leeds_candidates()

    # 1. 28 Audited False-Positive Cases (Must all evaluate to NEW_BUSINESS)
    fp_pairs = [
        ("The Sandwich Bar", "Sandys Sandwich Shop"),
        ("Calverley Wednesday Luncheon Club", "Calverley St Wilfreds Cricket Club"),
        ("The Turkuaz", "The Barn"),
        ("The Crown", "The Barn"),
        ("Delish", "El Gringos"),
        ("Casa Pizza", "Rivoli Pizza"),
        ("The Stage Door Café", "The Barn"),
        ("The Albert", "The Barn"),
        ("The New Inn", "The Barn"),
        ("The Little Fisherman", "The Lime Leaf"),
        ("The Corner 19 Cafe & Bistro", "The Barn"),
        ("Greggs", "El Gringos"),
        ("Yeadon Liberal Club", "Yeadon Constitutional Club"),
        ("Java Coffee Bar & Deli", "Rabbit Hole Coffee"),
        ("Gloucesters Tea Rooms", "Chippendales Tea Room"),
        ("Treehouse Bar & Kitchen", "Leeds House Café"),
        ("Mondo Kitchen & Coffee House", "Leeds House Café"),
        ("Pizza Base", "Impero Pizza"),
        ("Nostalgia", "Costa"),
        ("Coffee Station", "Olive & Fig"),
        ("The Curious Hop", "Cafe in the Courthouse"),
        ("Joys Coffee House", "Brewhouse"),
        ("Sancta", "Dragon City"),
        ("Il Vicoletto", "Il Sogno"),
        ("Super Wok", "Wok Away"),
        ("Maypole Food Bar", "Maypole Fisheries"),
        ("Cucina Pizza", "Impero Pizza"),
        ("W R V S Tea Bar", "Costa"),
    ]

    false_positive_cases = []
    for p1, p2 in fp_pairs:
        c1 = leeds_lookup.get(p1, {"company_name": p1, "city": "Leeds", "target_country": "United Kingdom"})
        c2 = leeds_lookup.get(p2, {"company_name": p2, "city": "Leeds", "target_country": "United Kingdom"})
        false_positive_cases.append((c1, c2, p1, p2))

    # 2. 10 True Duplicate Cases (Must all evaluate to EXISTING_BUSINESS)
    true_duplicate_cases = [
        # Exact duplicate
        ({"company_name": "Seoul Kimchi", "city": "Manchester", "target_country": "United Kingdom",
          "address": "275 Upper Brook St, Manchester M13 0HR", "street": "275 Upper Brook St", "postcode": "M13 0HR",
          "phone": "+44 7745 527603"},
         {"company_name": "Seoul Kimchi", "city": "Manchester", "target_country": "United Kingdom",
          "address": "275 Upper Brook St, Manchester M13 0HR", "street": "275 Upper Brook St", "postcode": "M13 0HR",
          "phone": "+44 7745 527603"},
         "Seoul Kimchi (Exact match)"),

        # Formatting, casing, and phone variation
        ({"company_name": "SEOUL-KIMCHI", "city": "Manchester", "target_country": "United Kingdom",
          "street": "275 Upper Brook Street", "postcode": "M130HR", "phone": "07745 527603"},
         {"company_name": "Seoul Kimchi", "city": "Manchester", "target_country": "United Kingdom",
          "street": "275 Upper Brook St", "postcode": "M13 0HR", "phone": "+44 7745 527603"},
         "Seoul Kimchi (Formatting / phone trunk variation)"),

        # Normalized company name with legal suffix
        ({"company_name": "THE FLYING PIZZA & CO. LTD", "city": "Leeds", "target_country": "United Kingdom", "postcode": "LS8 2AJ"},
         {"company_name": "Flying Pizza", "city": "Leeds", "target_country": "United Kingdom", "postcode": "LS8 2AJ"},
         "Flying Pizza (Legal suffix & stopword variation)"),

        # Phone match with city containment
        ({"company_name": "Bundobust", "city": "Leeds", "target_country": "United Kingdom", "phone": "+44 113 243 1248"},
         {"company_name": "Bundobust Leeds", "city": "Leeds", "target_country": "United Kingdom", "phone": "0113 243 1248"},
         "Bundobust (Phone match & city containment)"),

        # Spaced vs unspaced postal code
        ({"company_name": "Against The Grain", "city": "Yeadon", "target_country": "United Kingdom", "postcode": "LS197TA"},
         {"company_name": "Against The Grain Bar", "city": "Yeadon", "target_country": "United Kingdom", "postcode": "LS19 7TA"},
         "Against The Grain (Postcode space variation)"),

        # Street number + street name match with suite variation
        ({"company_name": "Raffertys Cafe", "city": "Otley", "target_country": "United Kingdom",
          "address": "13 Petergate, Suite 1", "street": "13 Petergate", "postcode": "LS21 3HN"},
         {"company_name": "Raffertys Cafe", "city": "Otley", "target_country": "United Kingdom",
          "street": "13 Petergate", "postcode": "LS21 3HN"},
         "Raffertys Cafe (Suite variation on same building number)"),

        # Brand name suffix variation on same street
        ({"company_name": "Gaucho - Leeds", "city": "Leeds", "target_country": "United Kingdom",
          "street": "21-22 Park Row", "postcode": "LS1 5QL"},
         {"company_name": "Gaucho", "city": "Leeds", "target_country": "United Kingdom",
          "street": "21-22 Park Row", "postcode": "LS1 5QL"},
         "Gaucho (Hyphenated city suffix at same address)"),

        # Verified official domain match
        ({"company_name": "Dishoom", "city": "Manchester", "target_country": "United Kingdom", "website": "https://dishoom.com/locations/manchester"},
         {"company_name": "Dishoom Manchester", "city": "Manchester", "target_country": "United Kingdom", "website": "https://dishoom.com"},
         "Dishoom (Exact verified official domain)"),

        # Phone match on same brand
        ({"company_name": "Mowgli Street Food", "city": "Manchester", "target_country": "United Kingdom", "phone": "+44 161 832 0109"},
         {"company_name": "Mowgli", "city": "Manchester", "target_country": "United Kingdom", "phone": "0161 832 0109"},
         "Mowgli (Phone match & brand name containment)"),

        # Micro-distance coordinates geoproximity (<50m)
        ({"company_name": "Tappino", "city": "Leeds", "target_country": "United Kingdom", "lat": 53.8012, "lon": -1.5521},
         {"company_name": "Tappino Italian Restaurant", "city": "Leeds", "target_country": "United Kingdom", "lat": 53.8013, "lon": -1.5522},
         "Tappino (Micro-distance geoproximity < 20m)"),
    ]

    # 3. 10 Legitimate Multi-Location Branches (Must all evaluate to NEW_BUSINESS)
    branch_cases = [
        # Different cities
        ({"company_name": "Seoul Kimchi", "city": "Leeds", "target_country": "United Kingdom", "street": "Russell St"},
         {"company_name": "Seoul Kimchi", "city": "Manchester", "target_country": "United Kingdom", "street": "Upper Brook St"},
         "Seoul Kimchi (Leeds vs Manchester)"),

        ({"company_name": "Gaucho", "city": "Leeds", "target_country": "United Kingdom", "street": "Russell St"},
         {"company_name": "Gaucho", "city": "Manchester", "target_country": "United Kingdom", "street": "St Mary St"},
         "Gaucho (Leeds vs Manchester)"),

        # Same city, distinct street numbers on same street
        ({"company_name": "Subway", "city": "Leeds", "target_country": "United Kingdom",
          "address": "12 Briggate", "street": "Briggate", "postcode": "LS1 6ER"},
         {"company_name": "Subway", "city": "Leeds", "target_country": "United Kingdom",
          "address": "450 Briggate", "street": "Briggate", "postcode": "LS1 6HJ"},
         "Subway (12 Briggate vs 450 Briggate)"),

        # Same brand, distinct streets and postcodes in same metro area
        ({"company_name": "Costa", "city": "Leeds", "target_country": "United Kingdom", "street": "Kirkgate", "postcode": "LS21 3HJ"},
         {"company_name": "Costa", "city": "Leeds", "target_country": "United Kingdom", "street": "High Street", "postcode": "LS19 7SP"},
         "Costa (Kirkgate vs High Street)"),

        ({"company_name": "Greggs", "city": "Leeds", "target_country": "United Kingdom", "street": "High Street", "postcode": "LS19 7SP"},
         {"company_name": "Greggs", "city": "Leeds", "target_country": "United Kingdom", "street": "Market Place", "postcode": "LS21 3AQ"},
         "Greggs (High Street vs Market Place)"),

        ({"company_name": "Nando's", "city": "Leeds", "target_country": "United Kingdom", "street": "Briggate", "postcode": "LS1 6ER"},
         {"company_name": "Nando's", "city": "Leeds", "target_country": "United Kingdom", "street": "Cardigan Fields", "postcode": "LS4 2DG"},
         "Nando's (Briggate vs Cardigan Fields)"),

        ({"company_name": "Starbucks", "city": "Manchester", "target_country": "United Kingdom", "street": "Oxford Road", "postcode": "M1 5QA"},
         {"company_name": "Starbucks", "city": "Manchester", "target_country": "United Kingdom", "street": "Piccadilly", "postcode": "M1 1RG"},
         "Starbucks (Oxford Road vs Piccadilly)"),

        ({"company_name": "Pizza Express", "city": "Leeds", "target_country": "United Kingdom", "street": "Albion Street", "postcode": "LS1 5AT"},
         {"company_name": "Pizza Express", "city": "Leeds", "target_country": "United Kingdom", "street": "Street Lane", "postcode": "LS8 1AP"},
         "Pizza Express (Albion Street vs Street Lane)"),

        ({"company_name": "Franco Manca", "city": "London", "target_country": "United Kingdom", "street": "Berwick St", "postcode": "W1F 8SF"},
         {"company_name": "Franco Manca", "city": "London", "target_country": "United Kingdom", "street": "Maiden Lane", "postcode": "WC2E 7JS"},
         "Franco Manca (Soho vs Covent Garden)"),

        ({"company_name": "Dishoom", "city": "Birmingham", "target_country": "United Kingdom", "street": "Chamberlain Sq", "postcode": "B3 3AX"},
         {"company_name": "Dishoom", "city": "Manchester", "target_country": "United Kingdom", "street": "Bridge St", "postcode": "M3 3BZ"},
         "Dishoom (Birmingham vs Manchester)"),
    ]

    # 4. 10 Clearly Different Businesses (Must all evaluate to NEW_BUSINESS)
    different_cases = [
        ({"company_name": "The Sandwich Bar", "city": "Leeds", "target_country": "United Kingdom"},
         {"company_name": "Sandys Sandwich Shop", "city": "Leeds", "target_country": "United Kingdom"},
         "The Sandwich Bar vs Sandys Sandwich Shop"),

        ({"company_name": "Casa Pizza", "city": "Leeds", "target_country": "United Kingdom"},
         {"company_name": "Rivoli Pizza", "city": "Leeds", "target_country": "United Kingdom"},
         "Casa Pizza vs Rivoli Pizza"),

        ({"company_name": "The Turkuaz", "city": "Leeds", "target_country": "United Kingdom"},
         {"company_name": "The Barn", "city": "Leeds", "target_country": "United Kingdom"},
         "The Turkuaz vs The Barn"),

        ({"company_name": "Greggs", "city": "Leeds", "target_country": "United Kingdom"},
         {"company_name": "El Gringos", "city": "Leeds", "target_country": "United Kingdom"},
         "Greggs vs El Gringos"),

        ({"company_name": "Nostalgia", "city": "Leeds", "target_country": "United Kingdom"},
         {"company_name": "Costa", "city": "Leeds", "target_country": "United Kingdom"},
         "Nostalgia vs Costa"),

        ({"company_name": "Il Vicoletto", "city": "Leeds", "target_country": "United Kingdom"},
         {"company_name": "Il Sogno", "city": "Leeds", "target_country": "United Kingdom"},
         "Il Vicoletto vs Il Sogno"),

        ({"company_name": "Super Wok", "city": "Leeds", "target_country": "United Kingdom"},
         {"company_name": "Wok Away", "city": "Leeds", "target_country": "United Kingdom"},
         "Super Wok vs Wok Away"),

        ({"company_name": "Sancta", "city": "Leeds", "target_country": "United Kingdom"},
         {"company_name": "Dragon City", "city": "Leeds", "target_country": "United Kingdom"},
         "Sancta vs Dragon City"),

        ({"company_name": "Coffee Station", "city": "Leeds", "target_country": "United Kingdom"},
         {"company_name": "Olive & Fig", "city": "Leeds", "target_country": "United Kingdom"},
         "Coffee Station vs Olive & Fig"),

        ({"company_name": "Java Coffee Bar & Deli", "city": "Leeds", "target_country": "United Kingdom"},
         {"company_name": "Rabbit Hole Coffee", "city": "Leeds", "target_country": "United Kingdom"},
         "Java Coffee Bar & Deli vs Rabbit Hole Coffee"),
    ]

    return false_positive_cases, true_duplicate_cases, branch_cases, different_cases


def run_benchmark():
    matcher = BusinessIdentityMatcher()
    fp_cases, td_cases, branch_cases, diff_cases = get_benchmark_datasets()

    print("==================================================")
    print("CRM IDENTITY MATCHER V2 PRECISION BENCHMARK")
    print("==================================================")

    # 1. Benchmark False Positive Cases
    print(f"\nEvaluating {len(fp_cases)} Audited False-Positive Cases...")
    fp_count = 0
    fp_details = []
    for c1, c2, n1, n2 in fp_cases:
        res = matcher.match_candidate(c1, [c2])
        if res.outcome != IdentityMatchOutcome.NEW_BUSINESS.value:
            fp_count += 1
            fp_details.append((n1, n2, res.outcome, res.confidence, res.match_reasons))

    fp_rate = (fp_count / len(fp_cases)) * 100.0
    print(f"  False Positives Flagged: {fp_count}/{len(fp_cases)} ({fp_rate:.1f}%)")
    if fp_details:
        for n1, n2, outcome, conf, reasons in fp_details:
            print(f"    [FAIL] {n1} vs {n2} -> {outcome} ({conf:.2f}) {reasons}")
    else:
        print("  [SUCCESS] All 28 false-positive cases correctly classified as NEW_BUSINESS!")

    # 2. Benchmark True Duplicate Cases
    print(f"\nEvaluating {len(td_cases)} True Duplicate Cases...")
    td_detected = 0
    td_details = []
    for c1, c2, desc in td_cases:
        res = matcher.match_candidate(c1, [c2])
        if res.outcome == IdentityMatchOutcome.EXISTING_BUSINESS.value:
            td_detected += 1
        else:
            td_details.append((desc, res.outcome, res.confidence, res.match_reasons))

    td_rate = (td_detected / len(td_cases)) * 100.0
    print(f"  True Duplicates Detected: {td_detected}/{len(td_cases)} ({td_rate:.1f}%)")
    if td_details:
        for desc, outcome, conf, reasons in td_details:
            print(f"    [FAIL] {desc} -> {outcome} ({conf:.2f}) {reasons}")
    else:
        print("  [SUCCESS] All 10 true duplicate cases correctly classified as EXISTING_BUSINESS!")

    # 3. Benchmark Legitimate Branch Cases
    print(f"\nEvaluating {len(branch_cases)} Legitimate Branch Cases...")
    branch_separated = 0
    branch_details = []
    for c1, c2, desc in branch_cases:
        res = matcher.match_candidate(c1, [c2])
        if res.outcome == IdentityMatchOutcome.NEW_BUSINESS.value:
            branch_separated += 1
        else:
            branch_details.append((desc, res.outcome, res.confidence, res.match_reasons))

    branch_acc = (branch_separated / len(branch_cases)) * 100.0
    print(f"  Branches Correctly Separated: {branch_separated}/{len(branch_cases)} ({branch_acc:.1f}%)")
    if branch_details:
        for desc, outcome, conf, reasons in branch_details:
            print(f"    [FAIL] {desc} -> {outcome} ({conf:.2f}) {reasons}")
    else:
        print("  [SUCCESS] All 10 legitimate branch cases correctly classified as NEW_BUSINESS!")

    # 4. Benchmark Clearly Different Businesses
    print(f"\nEvaluating {len(diff_cases)} Distinct Business Cases...")
    diff_separated = 0
    diff_details = []
    for c1, c2, desc in diff_cases:
        res = matcher.match_candidate(c1, [c2])
        if res.outcome == IdentityMatchOutcome.NEW_BUSINESS.value:
            diff_separated += 1
        else:
            diff_details.append((desc, res.outcome, res.confidence, res.match_reasons))

    diff_acc = (diff_separated / len(diff_cases)) * 100.0
    print(f"  Distinct Businesses Preserved: {diff_separated}/{len(diff_cases)} ({diff_acc:.1f}%)")
    if diff_details:
        for desc, outcome, conf, reasons in diff_details:
            print(f"    [FAIL] {desc} -> {outcome} ({conf:.2f}) {reasons}")
    else:
        print("  [SUCCESS] All 10 distinct business cases correctly classified as NEW_BUSINESS!")

    total_legitimate = len(fp_cases) + len(branch_cases) + len(diff_cases)
    total_preserved = (len(fp_cases) - fp_count) + branch_separated + diff_separated
    legit_rate = (total_preserved / total_legitimate) * 100.0

    print("\n==================================================")
    print("BENCHMARK SUMMARY METRICS")
    print("==================================================")
    print(f"True duplicate detection:           {td_rate:.1f}%")
    print(f"False-positive rate:                {fp_rate:.1f}%")
    print(f"Legitimate-new-business rate:       {legit_rate:.1f}%")
    print(f"Branch separation accuracy:         {branch_acc:.1f}%")
    print("==================================================")

    return {
        "true_duplicate_detection": td_rate,
        "false_positive_rate": fp_rate,
        "legitimate_new_business_rate": legit_rate,
        "branch_separation_accuracy": branch_acc
    }


if __name__ == "__main__":
    results = run_benchmark()
    if results["false_positive_rate"] > 0.0 or results["true_duplicate_detection"] < 100.0 or results["branch_separation_accuracy"] < 100.0:
        sys.exit(1)
    sys.exit(0)
