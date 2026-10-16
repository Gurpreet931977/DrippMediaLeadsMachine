import inspect
from types import SimpleNamespace
import lib.discovery.hybrid as hybrid_module


def test_independent_web_queries_are_first():
    providers = [
        cls for cls in vars(hybrid_module).values()
        if isinstance(cls, type)
        and cls.__module__ == hybrid_module.__name__
        and "get_query_rounds" in cls.__dict__
    ]
    assert providers, "No locally defined discovery class exposes get_query_rounds"

    method = providers[0].get_query_rounds
    params = inspect.signature(method).parameters
    if len(params) == 4:
        rounds = method(None, "Manchester", "United Kingdom", "Restaurants")
    else:
        rounds = method("Manchester", "United Kingdom", "Restaurants")

    assert rounds[0][0].startswith("Web ")
    assert "independent" in rounds[0][0]
    assert "family-run" in rounds[0][1]
    assert len(rounds) == 4
    assert any(q.startswith("OSM ") for batch in rounds[1:] for q in batch)


def test_first_round_web_results_are_merged():
    cls = next(
        cls for cls in vars(hybrid_module).values()
        if isinstance(cls, type)
        and cls.__module__ == hybrid_module.__name__
        and "discover_round" in cls.__dict__
    )
    engine = cls.__new__(cls)
    engine.mode = "HYBRID"
    engine.apify_enabled = False
    engine.apify = None

    from lib.types import DiscoveredBusiness, CountryStatus, WebsiteStatus

    def candidate(name):
        biz = DiscoveredBusiness(
            company_name=name,
            category="Restaurant",
            city="Manchester",
            target_country="United Kingdom",
            detected_country="United Kingdom",
            address="",
            phone="",
            raw_website="",
            osm_website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
        )
        biz.country_status = CountryStatus.COUNTRY_MATCH.value
        biz.verification_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
        biz.raw_data = {}
        return biz

    engine.osm = SimpleNamespace(
        search_businesses=lambda **kwargs: [candidate("Local Restaurant")]
    )
    engine.foursquare = SimpleNamespace(api_key="")
    calls = []

    def web_search(**kwargs):
        calls.append(kwargs)
        return [candidate("Independent Cafe")]

    engine.web_search = SimpleNamespace(search_businesses=web_search)
    engine.transparency_stats = {
        "businesses_discovered_total": 0,
        "unique_candidates": 0,
        "duplicates_merged": 0,
        "by_source": {
            "OPENSTREETMAP": 0, "FOURSQUARE": 0,
            "WEB_SEARCH": 0, "APIFY": 0,
        },
    }

    results, _ = engine.discover_round(
        1, "United Kingdom", ["Manchester"], "Restaurants", limit=10
    )

    assert calls and calls[0]["industry"] == "independent Restaurants"
    assert {item.company_name for item in results} == {
        "Local Restaurant", "Independent Cafe"
    }
    assert engine.transparency_stats["by_source"]["WEB_SEARCH"] == 1


def test_social_profile_matching_rejects_wrong_city():
    from lib.types import DiscoveredBusiness, CountryStatus, WebsiteStatus

    cls = next(c for c in vars(hybrid_module).values()
        if isinstance(c, type) and c.__module__ == hybrid_module.__name__
        and "discover_round" in c.__dict__)

    def candidate(name):
        b = DiscoveredBusiness(
            company_name=name, category="Restaurant", city="Manchester",
            target_country="United Kingdom", detected_country="United Kingdom",
            raw_website="", address="", phone="",
            osm_website_status=WebsiteStatus.NO_WEBSITE_CONFIRMED.value,
        )
        b.country_status = CountryStatus.COUNTRY_MATCH.value
        b.verification_status = WebsiteStatus.NO_WEBSITE_CONFIRMED.value
        b.raw_data = {}
        return b

    dog = candidate("Dog and Partridge")
    cornishman = candidate("The Cornishman")
    engine = cls.__new__(cls)
    engine.mode, engine.apify_enabled, engine.apify = "HYBRID", False, None
    engine.osm = SimpleNamespace(
        search_businesses=lambda **kw: [dog, cornishman])
    engine.foursquare = SimpleNamespace(api_key="")
    engine.transparency_stats = {
        "businesses_discovered_total": 0, "unique_candidates": 0,
        "duplicates_merged": 0,
        "by_source": {"OPENSTREETMAP": 0, "FOURSQUARE": 0,
                      "WEB_SEARCH": 0, "APIFY": 0},
    }
    engine.web_search = SimpleNamespace(search_businesses=lambda **kw: [
        {"url": "https://www.facebook.com/thedogandpartridgepub",
         "title": "The Dog & Partridge | Manchester",
         "snippet": "The Dog & Partridge in Manchester",
         "query": 'site:facebook.com "Dog and Partridge" "Manchester"'},
        {"url": "https://www.facebook.com/TheCornishmanPub2016",
         "title": "The Cornishman Pub | London",
         "snippet": "Pub in London",
         "query": 'site:facebook.com "The Cornishman" "Manchester"'},
    ])

    results, _ = engine.discover_round(
        1, "United Kingdom", ["Manchester"], "Restaurants", limit=10)
    by_name = {b.company_name: b for b in results}

    assert by_name["Dog and Partridge"].raw_data.get(
        "web_search_evidence"), "Valid Manchester profile was not attached"
    assert not by_name["The Cornishman"].raw_data.get(
        "web_search_evidence"), "London profile was attached to Manchester business"
