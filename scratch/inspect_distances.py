import json
import math
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.crm.identity_matcher import BusinessIdentityMatcher, clean_ascii_text

def haversine_m(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    R = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0)**2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c

matcher = BusinessIdentityMatcher()

with open("data/phase_7_8_osm_address_source_eval.json") as f:
    eval_p78 = json.load(f)

scraped_places_pool = []
with open("scratch/phase_7_6_20_results.json") as f:
    for line in f:
        line = line.strip()
        if line:
            scraped_places_pool.append(json.loads(line))

unique_places = {}
for p in scraped_places_pool:
    pid = p.get("place_id") or p.get("data_id") or f"{p.get('title')}_{p.get('address')}"
    if pid not in unique_places:
        unique_places[pid] = p
places_list = list(unique_places.values())

candidates = eval_p78["candidate_results"]

print(f"{'Idx':<3} | {'Candidate Name':<22} | {'OSM Coords':<25} | Plausible Places")
print("-" * 110)

for c in candidates:
    idx = c["index"]
    c_name = c["candidate_name"]
    c_lat = c["latitude"]
    c_lon = c["longitude"]
    c_norm = clean_ascii_text(c_name)
    
    cand_places = []
    for p in places_list:
        p_name = p.get("title") or ""
        p_norm = clean_ascii_text(p_name)
        sim, _, _ = matcher.compute_name_similarity(c_name, p_name)
        if sim >= 0.60 or c_norm in p_norm or p_norm in c_norm:
            cand_places.append((p, sim))
            
    print(f"[{idx:2d}] | {c_name:<22} | ({c_lat:.6f}, {c_lon:.6f}) | Found {len(cand_places)} places:")
    for p, sim in cand_places:
        p_title = p.get("title")
        p_lat = p.get("latitude")
        p_lon = p.get("longitude")
        p_addr = p.get("address")
        dist = haversine_m(c_lat, c_lon, p_lat, p_lon)
        dist_str = f"{dist:.1f}m" if dist is not None else "NO_COORDS"
        addr_str = p_addr[:45] if p_addr else "None"
        print(f"      -> {p_title:<40} | Dist: {dist_str:<12} | Sim: {sim:.2f} | Addr: {addr_str}")
