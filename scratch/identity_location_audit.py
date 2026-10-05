import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.enrichment.review_rating_enricher import ReviewRatingEnricher

enricher = ReviewRatingEnricher()

scenarios = [
    {
        "name": "1. Multi-branch chain: Gaucho Leeds vs Gaucho Manchester branch snippet",
        "business": "Gaucho", "city": "Leeds", "postcode": "LS1 1HA",
        "title": "Gaucho Manchester — Steak Restaurant",
        "snippet": "Gaucho Manchester located on 2A St Mary's Street, Manchester. Rated 4.6 from 1,450 reviews.",
        "url": "https://tripadvisor.com/gaucho-manchester",
        "expect_reject": True,
        "reason_contains": "WRONG_BRANCH"
    },
    {
        "name": "2. Multi-branch chain: Target city present, other city mentioned as comparison",
        "business": "Gaucho", "city": "Leeds", "postcode": "LS1 1HA",
        "title": "Gaucho Leeds — Reviews",
        "snippet": "Gaucho Leeds on Park Row in Leeds. Unlike the London branch, this Leeds venue has 890 reviews and 4.5 stars.",
        "url": "https://tripadvisor.com/gaucho-leeds",
        "expect_reject": False,
        "expected_reviews": 890
    },
    {
        "name": "3. Same business name in different city without target city in snippet",
        "business": "The Flower Pot", "city": "Mirfield", "postcode": "WF14 8NN",
        "title": "The Flower Pot, Derby - Pub Reviews",
        "snippet": "The Flower Pot in Derby is a renowned music pub with 650 reviews.",
        "url": "https://tripadvisor.com/flowerpot-derby",
        "expect_reject": True
    },
    {
        "name": "4. Apostrophe handling: Shezzaan's vs Shezzaans vs Shezzan",
        "business": "Shezzaan's", "city": "Pudsey", "postcode": "LS28 8LE",
        "title": "SHEZZAAN'S PUDSEY, Pudsey - Restaurant menu, prices and reviews",
        "snippet": "SHEZZAAN'SPUDSEYinPudseyrated 4.1 out of 5 onRestaurantGuru: 577 reviews by visitors.",
        "url": "https://restaurantguru.com/Shezzan-Pudsey",
        "expect_reject": False,
        "expected_reviews": 577
    },
    {
        "name": "5. Punctuation & Special Chars: Against The Grain vs against the grain",
        "business": "Against The Grain", "city": "Yeadon", "postcode": "LS19 7EP",
        "title": "Against The Grain - Yeadon Craft Beer",
        "snippet": "Against The Grain on Kirk Lane, Yeadon. 165 reviews on Google.",
        "url": "https://google.com/maps/place/Against+The+Grain",
        "expect_reject": False,
        "expected_reviews": 165
    },
    {
        "name": "6. Postcode area disambiguation: Yeadon LS19 vs Otley LS21",
        "business": "The Crown", "city": "Yeadon", "postcode": "LS19 7RE",
        "title": "The Crown Hotel, Boston Spa - Pub",
        "snippet": "The Crown Hotel in Boston Spa LS23 6AA. 420 reviews.",
        "url": "https://tripadvisor.com/crown-boston-spa",
        "expect_reject": True
    },
    {
        "name": "7. Suburb vs Metro City: Pudsey (suburb of Leeds) vs Leeds",
        "business": "Sandys Sandwich Shop", "city": "Pudsey", "postcode": "LS28 6PS",
        "title": "Sandys Sandwich Shop | Pudsey",
        "snippet": "Sandys Sandwich Shop in Pudsey has 127 reviews.",
        "url": "https://eateasy.co.uk/sandys",
        "expect_reject": False,
        "expected_reviews": 127
    }
]

print("Running Location and Identity Matching Audit:\n")
passed = 0
for sc in scenarios:
    ev = enricher.extract_evidence_from_snippet(
        snippet=sc["snippet"],
        title=sc["title"],
        url=sc["url"],
        business_name=sc["business"],
        city=sc["city"],
        postcode=sc.get("postcode", "")
    )
    rej = ev.reject_reason if ev else "NO_EVIDENCE_FOUND"
    rev = ev.review_count if ev and not ev.reject_reason else None
    
    if sc.get("expect_reject"):
        is_ok = (ev is None or ev.reject_reason is not None)
        if sc.get("reason_contains") and ev and ev.reject_reason:
            is_ok = sc["reason_contains"] in ev.reject_reason
    else:
        is_ok = (ev is not None and not ev.reject_reason and rev == sc.get("expected_reviews"))
        
    status = "PASS" if is_ok else "FAIL"
    if is_ok: passed += 1
    print(f"[{status}] {sc['name']}")
    print(f"  Result: rej='{rej}', rev={rev}")

print(f"\nLocation/Identity Audit: {passed}/{len(scenarios)} passed ({passed/len(scenarios)*100:.1f}%)")
