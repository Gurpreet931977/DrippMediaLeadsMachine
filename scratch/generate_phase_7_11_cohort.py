#!/usr/bin/env python3
"""
Phase 7.11 Candidate Selection & Blinding Script
Freezes 35 PARTIAL candidates and 12 COMPLETE control candidates.
"""
import os
import json
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load P7.9 and P7.10 to ensure zero overlap
with open(os.path.join(PROJECT_ROOT, "data/phase_7_9_coordinate_first_gosom_eval.json")) as f:
    p79 = json.load(f)
p79_cands = p79.get("candidate_results", [])
p79_names = {c.get("candidate_name", "").lower().strip() for c in p79_cands}
p79_ids = {str(c.get("osm_node_id")) for c in p79_cands}

with open(os.path.join(PROJECT_ROOT, "data/phase_7_10_coordinate_first_holdout_eval.json")) as f:
    p710 = json.load(f)
p710_cands = p710.get("holdout_candidates", [])
p710_names = {c.get("candidate_name", "").lower().strip() for c in p710_cands}
p710_ids = {str(c.get("osm_id")) for c in p710_cands}

used_names = p79_names | p710_names
used_ids = p79_ids | p710_ids

pure_partial = []
complete_cands = []

for filepath in [
    os.path.join(PROJECT_ROOT, "data/cache_osm/554dc5ede9e904197a2ddf8395a50759.json"),
    os.path.join(PROJECT_ROOT, "data/cache_osm/b8b181108354079510ef62af73ac60df.json"),
]:
    with open(filepath) as f:
        d = json.load(f)
        for el in d.get("data", {}).get("elements", []):
            tags = el.get("tags", {})
            name = tags.get("name", "").strip()
            amenity = tags.get("amenity", "")
            if not name or amenity not in ("restaurant", "cafe", "fast_food", "bar", "pub"):
                continue
            el_id = str(el.get("id"))
            if el_id in used_ids or name.lower() in used_names:
                continue
            lat = el.get("lat") or el.get("center", {}).get("lat")
            lon = el.get("lon") or el.get("center", {}).get("lon")
            if not (lat and lon and 53.3 <= lat <= 53.6 and -2.4 <= lon <= -2.1):
                continue

            street = (tags.get("addr:street") or tags.get("street") or "").strip()
            postcode = (tags.get("addr:postcode") or tags.get("postcode") or "").strip()
            housenumber = (tags.get("addr:housenumber") or tags.get("housenumber") or "").strip()

            if not street and not postcode and not housenumber:
                pure_partial.append({
                    "osm_id": el.get("id"),
                    "osm_type": el.get("type", "node"),
                    "name": name,
                    "amenity": amenity,
                    "lat": round(lat, 7),
                    "lon": round(lon, 7),
                    "street": "",
                    "postcode": "",
                    "housenumber": "",
                    "address_completeness": "PARTIAL",
                    "source_file": os.path.basename(filepath)
                })
            elif street and postcode:
                complete_cands.append({
                    "osm_id": el.get("id"),
                    "osm_type": el.get("type", "node"),
                    "name": name,
                    "amenity": amenity,
                    "lat": round(lat, 7),
                    "lon": round(lon, 7),
                    "street": street,
                    "postcode": postcode,
                    "housenumber": housenumber,
                    "address_completeness": "COMPLETE",
                    "source_file": os.path.basename(filepath)
                })

# Deduplicate
seen_p = set()
dedup_p = []
for p in pure_partial:
    if p["name"].lower() not in seen_p and p["osm_id"] not in seen_p:
        seen_p.add(p["name"].lower())
        seen_p.add(p["osm_id"])
        dedup_p.append(p)

seen_c = set()
dedup_c = []
for c in complete_cands:
    if c["name"].lower() not in seen_c and c["osm_id"] not in seen_c and c["name"].lower() not in seen_p:
        seen_c.add(c["name"].lower())
        seen_c.add(c["osm_id"])
        dedup_c.append(c)

selected_partial = dedup_p[:35]
selected_complete = dedup_c[:12]

cohort = []
queries = []

for idx, p in enumerate(selected_partial, 1):
    cid = f"MAN-PARTIAL-{idx:03d}"
    p_name = p["name"]
    query = f'"{p_name}" "Manchester"'
    rec = {
        "candidate_id": cid,
        "cohort_type": "PARTIAL",
        "index": idx,
        "osm_id": p["osm_id"],
        "osm_type": p["osm_type"],
        "company_name": p_name,
        "business_name": p_name,
        "city": "Manchester",
        "street": "",
        "postcode": "",
        "housenumber": "",
        "latitude": p["lat"],
        "longitude": p["lon"],
        "amenity": p["amenity"],
        "address_completeness": "PARTIAL",
        "discovery_query": query,
        "source_file": p["source_file"]
    }
    cohort.append(rec)
    queries.append(query)

for idx, c in enumerate(selected_complete, 1):
    cid = f"MAN-CONTROL-{idx:03d}"
    c_name = c["name"]
    c_street = c["street"]
    c_pc = c["postcode"]
    query = f'"{c_name}" "{c_street}" "{c_pc}" "Manchester"'
    rec = {
        "candidate_id": cid,
        "cohort_type": "COMPLETE_CONTROL",
        "index": idx,
        "osm_id": c["osm_id"],
        "osm_type": c["osm_type"],
        "company_name": c_name,
        "business_name": c_name,
        "city": "Manchester",
        "street": c_street,
        "postcode": c_pc,
        "housenumber": c["housenumber"],
        "latitude": c["lat"],
        "longitude": c["lon"],
        "amenity": c["amenity"],
        "address_completeness": "COMPLETE",
        "discovery_query": query,
        "source_file": c["source_file"]
    }
    cohort.append(rec)
    queries.append(query)

out_cands_path = os.path.join(PROJECT_ROOT, "scratch/phase_7_11_frozen_candidates.json")
out_queries_path = os.path.join(PROJECT_ROOT, "scratch/phase_7_11_queries.txt")

with open(out_cands_path, "w", encoding="utf-8") as f:
    json.dump(cohort, f, indent=2)

with open(out_queries_path, "w", encoding="utf-8") as f:
    for q in queries:
        f.write(q + "\n")

print(f"Phase 7.11 Frozen Cohort written: {len(cohort)} candidates ({len(selected_partial)} PARTIAL, {len(selected_complete)} COMPLETE)")
print(f"Queries written: {len(queries)}")
