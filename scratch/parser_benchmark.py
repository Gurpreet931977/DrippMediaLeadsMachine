import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import re
from lib.enrichment.review_rating_enricher import ReviewRatingEnricher

enricher = ReviewRatingEnricher()

test_cases = [
    {
        "case": "1. Rating only",
        "title": "Cafe 53 - Leeds Cafe",
        "snippet": "Cafe 53 in Leeds is rated 4.6 out of 5 stars by local customers.",
        "url": "https://example.com/cafe53",
        "business": "Cafe 53", "city": "Leeds",
        "expected_rating": 4.6, "expected_reviews": None
    },
    {
        "case": "2. Review count only",
        "title": "Vault 768, Bradford",
        "snippet": "Vault 768 on Bradford Road with 85 reviews from visitors.",
        "url": "https://example.com/vault768",
        "business": "Vault 768", "city": "Bradford",
        "expected_rating": None, "expected_reviews": 85
    },
    {
        "case": "3. Rating + Review count",
        "title": "Shezzaan Pudsey",
        "snippet": "Rated 4.1 out of 5 on Restaurant Guru: 577 reviews by visitors.",
        "url": "https://restaurantguru.com/shezzaan",
        "business": "Shezzaan", "city": "Pudsey",
        "expected_rating": 4.1, "expected_reviews": 577
    },
    {
        "case": "4. Date / Year in title before reviews (Tripadvisor 2026 bug)",
        "title": "ORIGINAL PATTY MEN, Birmingham - 2026 Reviews & Information - Tripadvisor",
        "snippet": "Order takeaway and delivery at Original Patty Men, Birmingham with Tripadvisor: See 783 unbiased reviews.",
        "url": "https://tripadvisor.co.uk/opm",
        "business": "Original Patty Men", "city": "Birmingham",
        "expected_rating": None, "expected_reviews": 783
    },
    {
        "case": "5. Prices in snippet (£15 - £25) with rating and reviews",
        "title": "The Corner 19 Cafe & Bistro - Yeadon",
        "snippet": "The Corner 19 Cafe & Bistro in Yeadon. Price £15-£25 per person. Rated 4.8 stars with 120 reviews.",
        "url": "https://example.com/corner19",
        "business": "The Corner 19 Cafe & Bistro", "city": "Yeadon",
        "expected_rating": 4.8, "expected_reviews": 120
    },
    {
        "case": "6. Address & Phone number (0113 250 2466, 37 High St)",
        "title": "Heebie Jeebies Cafe - Yeadon",
        "snippet": "Heebie Jeebies Cafe located at 37 High Street, Yeadon LS19 7SP. Call 0113 250 2466. 94 reviews, rated 4.5.",
        "url": "https://example.com/heebie",
        "business": "Heebie Jeebies Cafe", "city": "Yeadon",
        "expected_rating": 4.5, "expected_reviews": 94
    },
    {
        "case": "7. Business name containing number: 99 Reasons",
        "title": "99 Reasons Chorlton - Cafe Reviews",
        "snippet": "99 Reasons in Manchester has 214 reviews and a 4.7 star rating on Facebook.",
        "url": "https://facebook.com/99reasons",
        "business": "99 Reasons", "city": "Manchester",
        "expected_rating": 4.7, "expected_reviews": 214
    },
    {
        "case": "8. Business name containing number: 6 Acres",
        "title": "6 Acres Bradford - Pub & Dining",
        "snippet": "6 Acres pub in Bradford. Rated 4.2 stars based on 350 customer reviews.",
        "url": "https://example.com/6acres",
        "business": "6 Acres", "city": "Bradford",
        "expected_rating": 4.2, "expected_reviews": 350
    },
    {
        "case": "9. Multiple businesses in one search snippet (unrelated neighbor with reviews)",
        "title": "Visit Yeadon - Dining Guide",
        "snippet": "Next to The Stage Door Cafe is against the grain with 450 reviews.",
        "url": "https://example.com/guide",
        "business": "The Stage Door Cafe", "city": "Yeadon",
        "expected_rating": None, "expected_reviews": None
    },
    {
        "case": "10. Comma decimal rating (4,6 out of 5)",
        "title": "Browns Greens - Rawdon",
        "snippet": "Browns Greens vegetarian cafe in Rawdon rated 4,6 out of 5 from 88 reviews.",
        "url": "https://example.com/browns",
        "business": "Browns Greens", "city": "Rawdon",
        "expected_rating": 4.6, "expected_reviews": 88
    },
    {
        "case": "11. Multiple rating values in snippet (food 4.8, service 4.2, overall 4.5 from 200 reviews)",
        "title": "The Turkuaz - Rawdon",
        "snippet": "The Turkuaz in Rawdon: Food 4.8, Service 4.2, Overall score 4.5 from 200 reviews.",
        "url": "https://example.com/turkuaz",
        "business": "The Turkuaz", "city": "Rawdon",
        "expected_rating": 4.8,
        "expected_reviews": 200
    }
]

print("Running controlled parser benchmark across " + str(len(test_cases)) + " cases:\n")
passed = 0
failed = 0
for tc in test_cases:
    c_name = tc["case"]
    ev = enricher.extract_evidence_from_snippet(
        snippet=tc["snippet"],
        title=tc["title"],
        url=tc["url"],
        business_name=tc["business"],
        city=tc["city"]
    )
    rev = ev.review_count if ev and not ev.reject_reason else None
    rat = ev.rating if ev and not ev.reject_reason else None
    rej = ev.reject_reason if ev else "NO_EVIDENCE_FOUND"
    
    rev_ok = (rev == tc["expected_reviews"])
    rat_ok = (rat == tc["expected_rating"])
    status = "PASS" if (rev_ok and rat_ok) else "FAIL"
    if status == "PASS":
        passed += 1
    else:
        failed += 1
    print(f"[{status}] {c_name}")
    print(f"  Got:      Reviews={rev}, Rating={rat} (Reject={rej})")
    print(f"  Expected: Reviews={tc['expected_reviews']}, Rating={tc['expected_rating']}")
    if not (rev_ok and rat_ok):
        print(f"  Diff: rev_ok={rev_ok}, rat_ok={rat_ok}")

print(f"\nSummary: Total={len(test_cases)}, Passed={passed}, Failed={failed}, Accuracy={passed/len(test_cases)*100:.1f}%")
