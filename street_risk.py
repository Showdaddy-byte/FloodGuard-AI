"""
street_risk.py - street-level flood risk for FloodGuard AI (worldwide).

What this module does
---------------------
For one area (a neighbourhood / city centre anywhere in the world) it:
  1. pulls the NAMED roads around that point from OpenStreetMap (Overpass),
  2. pulls nearby drains / rivers / canals / water bodies from OpenStreetMap,
  3. samples terrain elevation along every street,
  4. combines those with the area's existing 0-100 flood score (the
     terrain-aware, rainfall-driven FloodGuard model) to give every street a
     MODELLED risk score on the same 0-100 scale,
  5. attaches visitor flood reports that name the street (unverified) and any
     official flood alerts that cover the area (area-level, never street-level),
  6. records exactly which data was and was not available for that country, and
  7. stores the result so it can be searched, mapped and quoted in alerts.

What it deliberately does NOT do
--------------------------------
  * It never says a street IS flooded. Scores are modelled. "Confirmed
    flooding" is a supported evidence type but nothing in the current data
    sources can produce it, so it is never shown.
  * It does not invent street-level data. If OpenStreetMap has too few named
    streets, or the terrain/drainage data cannot tell streets apart, the result
    is "insufficient data" and no road-specific predictions are made.
  * The alert threshold is a point on FloodGuard's 0-100 model scale. It is NOT
    a statistically calibrated probability of flooding.

All network/model access is injected (see assess_area) so the logic can be
unit-tested offline.
"""

import json
import math
import os
import re
import sqlite3
import unicodedata
from datetime import datetime


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _float_env(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _int_env(name, default):
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return int(default)


# Default point on the 0-100 FloodGuard scale at which a street/area is flagged
# and Telegram alerts fire. Not a probability.
ALERT_THRESHOLD = _float_env("FLOOD_ALERT_THRESHOLD", 30)


def _parse_country_thresholds(raw):
    out = {}
    for part in (raw or "").split(","):
        if "=" in part:
            code, _, value = part.partition("=")
            try:
                out[code.strip().upper()] = float(value)
            except ValueError:
                pass
    return out


# e.g. FLOOD_ALERT_THRESHOLD_BY_COUNTRY="GB=35,BD=25"
COUNTRY_THRESHOLDS = _parse_country_thresholds(os.getenv("FLOOD_ALERT_THRESHOLD_BY_COUNTRY"))


def threshold_for(country_code=None):
    return COUNTRY_THRESHOLDS.get((country_code or "").upper(), ALERT_THRESHOLD)


STREET_SEARCH_RADIUS_M = _int_env("STREET_SEARCH_RADIUS_M", 2000)
STREET_MAX_ROADS = _int_env("STREET_MAX_ROADS", 120)
STREET_SAMPLES_PER_ROAD = _int_env("STREET_SAMPLES_PER_ROAD", 4)
STREET_CACHE_MINUTES = _int_env("STREET_CACHE_MINUTES", 180)
STREET_STALE_MINUTES = _int_env("STREET_STALE_MINUTES", 720)
MIN_NAMED_STREETS = _int_env("STREET_MIN_NAMED_STREETS", 5)


def severity_for(score, threshold=None):
    t = ALERT_THRESHOLD if threshold is None else threshold
    if score >= max(70, t + 40):
        return "severe", "Severe"
    if score >= max(50, t + 20):
        return "high", "High"
    if score >= t:
        return "moderate", "Moderate"
    return "low", "Low"


HIGHWAY_RANK = {
    "motorway": 0, "trunk": 1, "primary": 2, "secondary": 3, "tertiary": 4,
    "unclassified": 5, "residential": 6, "living_street": 7,
}
HIGHWAY_LABEL = {
    "motorway": "Motorway / expressway", "trunk": "Trunk road", "primary": "Major road",
    "secondary": "Main road", "tertiary": "Local main street",
    "unclassified": "Local road", "residential": "Residential street",
    "living_street": "Residential street",
}

SOURCES_BASE = [
    "OpenStreetMap contributors, ODbL (road and drainage geometry)",
    "FloodGuard flood model (weather, forecast, terrain, soil, tide, river)",
]

LIMITATIONS = [
    "Scores are MODELLED on FloodGuard's 0-100 scale. They are not a statistically calibrated probability of flooding and not measured flooding.",
    "Only streets that have a name in OpenStreetMap are assessed; unnamed lanes are not shown. Coverage and accuracy of OpenStreetMap differ between countries.",
    "Terrain data is 30-90 m resolution; in very flat areas small drainage and kerb details are invisible to it.",
    "Official alerts are area-level warnings, not statements about a particular street. Where no national agency feed is connected, none is shown.",
    "Visitor reports are unverified and only count when they name the street. No source currently available can confirm flooding on a specific street.",
]

# Evidence types a result can carry. "confirmed" exists so the schema is honest
# about what we would show if a verified source were connected; nothing produces it today.
EVIDENCE_LABELS = {
    "modelled": "Modelled risk",
    "official_area_alert": "Official flood alert active for the area",
    "unverified_report": "Unverified visitor report",
    "confirmed": "Confirmed flooding",
}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _point_segment_distance_m(plat, plon, alat, alon, blat, blon):
    k_lat = 111320.0
    k_lon = 111320.0 * math.cos(math.radians(plat))
    ax, ay = (alon - plon) * k_lon, (alat - plat) * k_lat
    bx, by = (blon - plon) * k_lon, (blat - plat) * k_lat
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0:
        return math.hypot(ax, ay)
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    return math.hypot(ax + t * dx, ay + t * dy)


def polyline_length_m(points):
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))


def simplify(points, max_points=40):
    if len(points) <= max_points:
        return [[round(p[0], 6), round(p[1], 6)] for p in points]
    step = (len(points) - 1) / float(max_points - 1)
    picked = [points[int(round(i * step))] for i in range(max_points)]
    return [[round(p[0], 6), round(p[1], 6)] for p in picked]


# ---------------------------------------------------------------------------
# Name normalisation. Unicode-aware so Arabic, Chinese, Cyrillic, accented
# Latin etc. all match (the old ASCII-only version erased them).
# ---------------------------------------------------------------------------
_ABBREV = {
    "street": "st", "road": "rd", "avenue": "ave", "close": "cl", "crescent": "cres",
    "drive": "dr", "boulevard": "blvd", "expressway": "expy", "lane": "ln",
}


def normalize_name(text):
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]|_", " ", text)
    return " ".join(_ABBREV.get(w, w) for w in text.split())


def _name_in_text(name_norm, text_norm):
    if not name_norm or not text_norm:
        return False
    return re.search(r"(?<!\w)" + re.escape(name_norm) + r"(?!\w)", text_norm) is not None


# ---------------------------------------------------------------------------
# OpenStreetMap queries and parsing
# ---------------------------------------------------------------------------
def build_roads_query(lat, lon, radius_m):
    return (
        "[out:json][timeout:25][maxsize:60000000];"
        '(way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street)$"]'
        f'["name"](around:{int(radius_m)},{lat:.6f},{lon:.6f}););'
        "out tags geom qt;"
    )


def build_water_query(lat, lon, radius_m):
    return (
        "[out:json][timeout:25][maxsize:60000000];"
        '(way["waterway"~"^(river|canal|drain|stream|ditch)$"]'
        f"(around:{int(radius_m)},{lat:.6f},{lon:.6f});"
        'way["natural"="water"]'
        f"(around:{int(radius_m)},{lat:.6f},{lon:.6f}););"
        "out geom qt;"
    )


def parse_roads(osm_json, centre=None, max_roads=None):
    """Group OSM ways by street name. Streets nearest the area centre come
    first (with a small bonus for major roads), capped at max_roads, so a dense
    city centre is not filled with distant major roads."""
    max_roads = STREET_MAX_ROADS if max_roads is None else max_roads
    streets = {}
    for element in (osm_json or {}).get("elements", []):
        if element.get("type") != "way":
            continue
        tags = element.get("tags") or {}
        name = (tags.get("name") or "").strip()
        geom = [(p["lat"], p["lon"]) for p in element.get("geometry", []) if "lat" in p and "lon" in p]
        if not name or len(geom) < 2:
            continue
        highway = tags.get("highway", "unclassified")
        key = normalize_name(name)
        street = streets.setdefault(key, {
            "name": name, "name_en": (tags.get("name:en") or "").strip() or None,
            "highway": highway, "ways": [], "bridge": False, "tunnel": False,
        })
        if HIGHWAY_RANK.get(highway, 9) < HIGHWAY_RANK.get(street["highway"], 9):
            street["highway"] = highway
        street["ways"].append(geom)
        if tags.get("bridge") not in (None, "no"):
            street["bridge"] = True
        if tags.get("tunnel") not in (None, "no") or tags.get("covered") == "yes":
            street["tunnel"] = True
    out = []
    for street in streets.values():
        street["length_m"] = sum(polyline_length_m(w) for w in street["ways"])
        if centre:
            street["dist_m"] = min(haversine_m(centre[0], centre[1], p[0], p[1]) for w in street["ways"] for p in w[::3] or w)
        else:
            street["dist_m"] = 0
        out.append(street)
    out.sort(key=lambda s: (s["dist_m"] - 60 * (7 - HIGHWAY_RANK.get(s["highway"], 7)), -s["length_m"]))
    return out[:max_roads]


def parse_water_segments(osm_json, max_segments=4000):
    segments = []
    for element in (osm_json or {}).get("elements", []):
        geom = [(p["lat"], p["lon"]) for p in element.get("geometry", []) if "lat" in p and "lon" in p]
        for a, b in zip(geom, geom[1:]):
            segments.append((a[0], a[1], b[0], b[1]))
            if len(segments) >= max_segments:
                return segments
    return segments


def sample_points(street, n=None):
    n = STREET_SAMPLES_PER_ROAD if n is None else n
    verts = []
    for way in sorted(street["ways"], key=polyline_length_m, reverse=True):
        verts.extend(way)
    if not verts:
        return []
    if len(verts) <= n:
        return list(verts)
    step = (len(verts) - 1) / float(n - 1)
    return [verts[int(round(i * step))] for i in range(n)]


def min_distance_to_water_m(points, segments):
    if not segments or not points:
        return None
    best = None
    for plat, plon in points:
        for alat, alon, blat, blon in segments:
            if best is not None and abs(alat - plat) * 111320.0 > best + 50 and abs(blat - plat) * 111320.0 > best + 50:
                continue
            d = _point_segment_distance_m(plat, plon, alat, alon, blat, blon)
            if best is None or d < best:
                best = d
    return best


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_street(area_score, low_rank, rank_reliable, water_m, bridge, tunnel, report_count):
    """Returns (score, factors, differentiators). `factors` lists only things
    the data supports; `differentiators` counts inputs that can make one street
    differ from another (used to refuse road-specific output when there are none)."""
    factors = []
    differentiators = 0
    score = float(area_score)
    factors.append(f"Area flood score {int(round(area_score))}/100 from the current weather, forecast and terrain model")

    if rank_reliable and low_rank is not None:
        differentiators += 1
        score *= 0.7 + 0.6 * low_rank          # 0.7x (highest ground) .. 1.3x (lowest)
        if low_rank >= 0.75:
            factors.append("Among the lowest-lying mapped streets in this area (terrain model)")
        elif low_rank <= 0.25:
            factors.append("Among the higher-lying mapped streets in this area (lowers risk)")
    if water_m is not None:
        if water_m <= 75:
            score += 8; differentiators += 1
            factors.append(f"Runs within ~{int(round(water_m))} m of a drain, canal, river or water body (OpenStreetMap)")
        elif water_m <= 200:
            score += 5; differentiators += 1
            factors.append(f"Within ~{int(round(water_m))} m of a drain, canal, river or water body (OpenStreetMap)")
        elif water_m <= 400:
            score += 2; differentiators += 1
            factors.append(f"About {int(round(water_m))} m from the nearest mapped drain or water body")
    if tunnel:
        score += 6; differentiators += 1
        factors.append("Includes an underpass or tunnel, where water collects (OpenStreetMap tag)")
    if bridge:
        score -= 8; differentiators += 1
        factors.append("Includes an elevated bridge section (lowers risk on the deck; approaches can still flood)")
    if report_count:
        score += min(10, 4 + 2 * report_count); differentiators += 1
        factors.append(f"{report_count} visitor flood report(s) in the last 7 days name this street (unverified)")
    return max(0, min(100, int(round(score)))), factors, differentiators


def recommended_action(severity, tunnel=False, official=False, emergency_text=None):
    extra = f" Emergency number: {emergency_text}." if emergency_text else " Know your local emergency number."
    if severity == "severe":
        text = ("Avoid this street. Use higher-ground alternatives and never drive or walk through "
                "floodwater of unknown depth. Follow instructions from local authorities.")
    elif severity == "high":
        text = "Avoid if you can while rain continues; choose a higher route and allow extra time. Do not enter standing water."
    elif severity == "moderate":
        text = ("Be cautious: expect ponding in low spots. Check FloodGuard again before setting out"
                + (" and avoid underpasses and low sections." if tunnel else "."))
    else:
        return "Below the alert threshold. No special action needed; keep an eye on updates."
    if official:
        text += " An official flood alert is active for this area: follow the issuing agency's guidance."
    return text + extra


def _evidence(report_count, official_alerts):
    ev = ["modelled"]
    if report_count:
        ev.append("unverified_report")
    if official_alerts:
        ev.append("official_area_alert")
    primary = "official_area_alert" if official_alerts else ("unverified_report" if report_count else "modelled")
    label = "Modelled risk (FloodGuard model score)"
    if official_alerts:
        label += " · official flood alert active for this area (not street-specific)"
    if report_count:
        label += " · unverified visitor reports name this street"
    return ev, primary, label


# ---------------------------------------------------------------------------
# Main assessment
# ---------------------------------------------------------------------------
def assess_area(area, area_score, *, run_overpass, elevation_fn, reports, official=None, coverage=None,
                threshold=None, now=None, radius_m=None, elevation_source="terrain model", emergency_text=None):
    """
    area        dict: key, label, country, country_code, region, city, neighbourhood, lat, lon, timezone
    area_score  the area's current 0-100 FloodGuard score
    run_overpass(query) -> parsed JSON or None
    elevation_fn(list[(lat, lon)]) -> list[float|None]
    reports     list of {"roads": str, "created_at": iso} flooding reports (last 7 days)
    official    {"alerts": [...], "checked": [...], "checked_at": iso} or None
    coverage    list of {"factor", "status", "source"} describing what data this country supplied

    Returns an assessment dict. If street-level data is insufficient the dict
    has status == "insufficient_data" and NO street predictions.
    """
    threshold = threshold_for(area.get("country_code")) if threshold is None else threshold
    now = now or datetime.utcnow()
    radius_m = radius_m or STREET_SEARCH_RADIUS_M
    gaps = []
    official = official or {"alerts": [], "checked": [], "checked_at": None}
    official_alerts = official.get("alerts", [])

    def base(status, reason=None, extra_meta=None):
        meta = {
            "radius_m": radius_m, "data_gaps": gaps, "limitations": LIMITATIONS, "coverage": coverage or [],
            "sources": list(SOURCES_BASE) + [f"Terrain: {elevation_source}"],
            "basis": "Modelled risk from the FloodGuard model. Not confirmed flooding and not a calibrated probability.",
            "score_kind": "model_score", "calibrated_probability": False,
        }
        meta.update(extra_meta or {})
        return {
            "status": status, "reason": reason,
            "area": {k: area.get(k) for k in ("key", "label", "country", "country_code", "region", "city",
                                              "neighbourhood", "lat", "lon", "timezone", "tz_offset")}
                    | {"score": int(round(area_score))},
            "threshold": threshold, "assessed_at": now.isoformat(),
            "official": official, "streets": [], "junctions": [], "meta": meta,
        }

    centre = (area["lat"], area["lon"])
    roads_json = run_overpass(build_roads_query(area["lat"], area["lon"], radius_m))
    if roads_json is None and radius_m > 1000:
        radius_m = radius_m // 2
        roads_json = run_overpass(build_roads_query(area["lat"], area["lon"], radius_m))
    if roads_json is None:
        gaps.append("OpenStreetMap road data could not be retrieved right now")
        return base("unavailable", "OpenStreetMap road data could not be retrieved right now. Try again in a few minutes.")
    streets = parse_roads(roads_json, centre)
    if len(streets) < MIN_NAMED_STREETS:
        gaps.append(f"Only {len(streets)} named street(s) are mapped in OpenStreetMap within {radius_m} m")
        return base("insufficient_data",
                    f"OpenStreetMap has too few named streets mapped around here ({len(streets)}) to make road-specific "
                    "predictions. FloodGuard shows the area-level score only rather than guess at individual roads.",
                    {"roads_mapped": len(streets)})

    water_json = run_overpass(build_water_query(area["lat"], area["lon"], radius_m))
    water_segments = parse_water_segments(water_json) if water_json else []
    if water_json is None:
        gaps.append("Drainage/water layer unavailable, so closeness-to-drain was not scored")
    elif not water_segments:
        gaps.append("No mapped drains or water bodies nearby; closeness-to-drain not applicable")

    per_street_points = [sample_points(s) for s in streets]
    flat = [p for pts in per_street_points for p in pts]
    elevations = []
    if flat:
        try:
            elevations = elevation_fn(flat) or []
        except Exception as error:  # noqa: BLE001
            print(f"street_risk elevation lookup failed: {error}")
            elevations = []
    if len(elevations) != len(flat):
        elevations = [None] * len(flat)

    mean_elev, cursor = [], 0
    for pts in per_street_points:
        vals = [e for e in elevations[cursor:cursor + len(pts)] if e is not None]
        cursor += len(pts)
        mean_elev.append(sum(vals) / len(vals) if vals else None)

    known = sorted(e for e in mean_elev if e is not None)
    rank_reliable = False
    if len(known) >= 8:
        spread = known[int(0.9 * (len(known) - 1))] - known[int(0.1 * (len(known) - 1))]
        rank_reliable = spread >= 1.5
        if not rank_reliable:
            gaps.append("Terrain is too flat here for the elevation data to separate streets; elevation not used")
    else:
        gaps.append("Not enough elevation samples to compare streets; elevation not used")
    median_elev = known[len(known) // 2] if known else None

    reports_norm = [(normalize_name(r.get("roads") or ""), r.get("created_at")) for r in (reports or [])]

    results, differentiating = [], 0
    for street, pts, elev in zip(streets, per_street_points, mean_elev):
        low_rank = None
        if rank_reliable and elev is not None:
            below = sum(1 for e in known if e < elev)
            low_rank = 1.0 - below / max(1, len(known) - 1)
        water_m = min_distance_to_water_m(pts, water_segments)
        names = {normalize_name(street["name"])}
        if street.get("name_en"):
            names.add(normalize_name(street["name_en"]))
        matched = [(t, c) for t, c in reports_norm if any(_name_in_text(n, t) for n in names)]
        score, factors, diff = score_street(
            area_score, low_rank, rank_reliable and elev is not None, water_m,
            street["bridge"], street["tunnel"], len(matched),
        )
        differentiating += diff
        if elev is not None and rank_reliable and median_elev is not None and low_rank is not None and low_rank >= 0.75:
            factors = [f.replace("(terrain model)", f"(~{elev:.0f} m vs area median ~{median_elev:.0f} m)") for f in factors]
        severity, severity_label = severity_for(score, threshold)
        evidence, primary, basis_label = _evidence(len(matched), official_alerts)
        longest = max(street["ways"], key=polyline_length_m)
        centre_pt = pts[len(pts) // 2] if pts else longest[len(longest) // 2]
        results.append({
            "kind": "road",
            "name": street["name"], "name_en": street.get("name_en"),
            "road_class": HIGHWAY_LABEL.get(street["highway"], "Road"),
            "country": area.get("country"), "country_code": area.get("country_code"),
            "region": area.get("region"), "city": area.get("city"), "neighbourhood": area.get("neighbourhood"),
            "risk_score": score, "severity": severity, "severity_label": severity_label,
            "above_threshold": score >= threshold, "threshold": threshold,
            "score_kind": "model_score",
            "evidence": evidence, "primary_basis": primary, "status_label": basis_label,
            "confirmed_flooding": False,
            "visitor_reports_7d": len(matched),
            "factors": factors,
            "recommended_action": recommended_action(severity, street["tunnel"], bool(official_alerts), emergency_text),
            "elevation_m": round(elev, 1) if elev is not None else None,
            "nearest_water_m": int(round(water_m)) if water_m is not None else None,
            "lat": round(centre_pt[0], 6), "lon": round(centre_pt[1], 6),
            "segment": {"description": f"Whole mapped length near {area.get('neighbourhood') or area.get('city') or 'the area'}",
                        "length_m": int(round(street["length_m"]))},
            "geometry": simplify(longest),
            "length_m": int(round(street["length_m"])),
            "_geom_all": [simplify(w, 25) for w in street["ways"][:6]],
            "_vertices": [(round(p[0], 6), round(p[1], 6)) for w in street["ways"] for p in w],
        })

    if differentiating == 0:
        gaps.append("No terrain, drainage, structure or report data could tell these streets apart")
        return base("insufficient_data",
                    "Terrain and drainage data here are not detailed enough to rank individual streets, so FloodGuard shows "
                    "the area-level score only instead of inventing road-specific predictions.",
                    {"roads_mapped": len(streets), "roads_with_elevation": len(known)})

    results.sort(key=lambda r: (-r["risk_score"], r["name"]))
    junctions = find_junctions(results, threshold, area, official_alerts, emergency_text)
    for r in results:
        r.pop("_vertices", None)
        r["geometry_all"] = r.pop("_geom_all")

    out = base("ok", None, {
        "roads_mapped": len(streets), "roads_with_elevation": len(known), "water_segments": len(water_segments),
        "elevation_used": rank_reliable,
    })
    if reports_norm:
        out["meta"]["sources"].append("FloodGuard visitor reports (unverified)")
    for a in official_alerts:
        s = f"{a['source']} ({a.get('licence', '')})"
        if s not in out["meta"]["sources"]:
            out["meta"]["sources"].append(s)
    out["streets"], out["junctions"] = results, junctions
    return out


def find_junctions(streets, threshold, area, official_alerts, emergency_text, limit=10):
    node_streets = {}
    for s in streets:
        if s["risk_score"] < threshold:
            continue
        for v in set(s.get("_vertices", ())):
            node_streets.setdefault(v, []).append(s)
    junctions, seen = [], set()
    for (lat, lon), group in node_streets.items():
        if len({g["name"] for g in group}) < 2:
            continue
        group = sorted(group, key=lambda g: -g["risk_score"])[:2]
        pair = tuple(sorted(g["name"] for g in group))
        if pair in seen:
            continue
        seen.add(pair)
        score = min(g["risk_score"] for g in group)
        sev, sev_label = severity_for(score, threshold)
        a, b = group
        reports = a["visitor_reports_7d"] + b["visitor_reports_7d"]
        evidence, primary, basis_label = _evidence(reports, official_alerts)
        junctions.append({
            "kind": "junction", "name": f"{pair[0]} / {pair[1]} junction", "name_en": None, "road_class": "Junction",
            "country": area.get("country"), "country_code": area.get("country_code"),
            "region": area.get("region"), "city": area.get("city"), "neighbourhood": area.get("neighbourhood"),
            "risk_score": score, "severity": sev, "severity_label": sev_label,
            "above_threshold": True, "threshold": threshold, "score_kind": "model_score",
            "evidence": evidence, "primary_basis": primary, "status_label": basis_label,
            "confirmed_flooding": False, "visitor_reports_7d": reports,
            "factors": [f"Both {pair[0]} and {pair[1]} are flagged at/above the alert threshold where they meet"],
            "recommended_action": recommended_action(sev, False, bool(official_alerts), emergency_text),
            "segment": {"description": "Point where the two flagged streets meet", "length_m": 0},
            "lat": lat, "lon": lon, "geometry": [], "geometry_all": [],
        })
    junctions.sort(key=lambda j: -j["risk_score"])
    return junctions[:limit]


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------
def freshness(assessed_at_iso, now=None):
    now = now or datetime.utcnow()
    try:
        age = max(0, int((now - datetime.fromisoformat(assessed_at_iso)).total_seconds() // 60))
    except (TypeError, ValueError):
        return None, "unknown"
    if age <= STREET_CACHE_MINUTES:
        label = "fresh"
    elif age <= STREET_STALE_MINUTES:
        label = "aging"
    else:
        label = "stale"
    return age, label


# ---------------------------------------------------------------------------
# Persistence (only successful street-level assessments are cached)
# ---------------------------------------------------------------------------
def ensure_tables(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS street_risk_cache (
            area_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            area_score REAL,
            country_code TEXT,
            computed_at TEXT NOT NULL
        )
        """
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(street_risk_cache)")}
    if "country_code" not in cols:
        conn.execute("ALTER TABLE street_risk_cache ADD COLUMN country_code TEXT")
    conn.commit()
    conn.close()


def save_assessment(db_path, assessment):
    if assessment.get("status") != "ok":
        return False
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO street_risk_cache (area_key, payload, area_score, country_code, computed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(area_key) DO UPDATE SET payload=excluded.payload, area_score=excluded.area_score,
            country_code=excluded.country_code, computed_at=excluded.computed_at
        """,
        (assessment["area"]["key"], json.dumps(assessment), assessment["area"]["score"],
         (assessment["area"].get("country_code") or "").upper(), assessment["assessed_at"]),
    )
    conn.commit()
    conn.close()
    return True


def _decorate(assessment, now=None):
    age, label = freshness(assessment["assessed_at"], now)
    assessment["data_age_minutes"] = age
    assessment["freshness"] = label
    return assessment


def load_assessment(db_path, area_key, max_age_minutes=None, now=None):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT payload FROM street_risk_cache WHERE area_key = ?", (area_key,)).fetchone()
    conn.close()
    if not row:
        return None
    assessment = _decorate(json.loads(row[0]), now)
    if max_age_minutes is not None and (assessment["data_age_minutes"] is None or assessment["data_age_minutes"] > max_age_minutes):
        return None
    return assessment


def _all_assessments(db_path, now=None, country_code=None):
    conn = sqlite3.connect(db_path)
    if country_code:
        rows = conn.execute("SELECT payload FROM street_risk_cache WHERE country_code = ?", (country_code.upper(),)).fetchall()
    else:
        rows = conn.execute("SELECT payload FROM street_risk_cache").fetchall()
    conn.close()
    return [_decorate(json.loads(r[0]), now) for r in rows]


def flatten(assessment):
    out = []
    for item in assessment["streets"] + assessment["junctions"]:
        entry = dict(item)
        entry.update({
            "assessed_at": assessment["assessed_at"],
            "data_age_minutes": assessment["data_age_minutes"],
            "freshness": assessment["freshness"],
            "area_label": assessment["area"]["label"],
            "area_key": assessment["area"]["key"],
            "area_score": assessment["area"]["score"],
            "timezone": assessment["area"].get("timezone"),
            "tz_offset": assessment["area"].get("tz_offset"),
            "sources": assessment["meta"]["sources"],
            "official_alerts": assessment.get("official", {}).get("alerts", []),
            "coverage": assessment["meta"].get("coverage", []),
            "data_gaps": assessment["meta"].get("data_gaps", []),
        })
        out.append(entry)
    return out


def top_flagged(db_path, limit=40, include_stale=False, now=None, country_code=None):
    items = []
    for a in _all_assessments(db_path, now, country_code):
        if a["freshness"] == "stale" and not include_stale:
            continue
        items.extend(i for i in flatten(a) if i["above_threshold"])
    items.sort(key=lambda i: (-i["risk_score"], i["name"]))
    return items[:limit]


def _hay(i):
    return " | ".join(normalize_name(x) for x in (
        i["name"], i.get("name_en") or "", i.get("neighbourhood") or "", i.get("city") or "",
        i.get("region") or "", i.get("country") or "", i.get("country_code") or "", i["area_label"],
    ))


def search_cached(db_path, query, limit=80, now=None):
    """Search street, neighbourhood, city, region and country across cached
    assessments. Returns (items, areas) where areas lists the distinct assessed
    areas that matched, so the caller can detect an ambiguous query that spans
    several countries instead of silently mixing them."""
    q = normalize_name(query)
    if not q:
        return [], []
    items, areas = [], {}
    for a in _all_assessments(db_path, now):
        hits = [i for i in flatten(a) if q in _hay(i)]
        if hits:
            items.extend(hits)
            areas[a["area"]["key"]] = {
                "key": a["area"]["key"], "label": a["area"]["label"], "country": a["area"].get("country"),
                "country_code": a["area"].get("country_code"), "region": a["area"].get("region"),
                "lat": a["area"]["lat"], "lon": a["area"]["lon"],
            }
    items.sort(key=lambda i: (-i["risk_score"], i["name"]))
    return items[:limit], list(areas.values())


def area_items(db_path, area_key, now=None):
    a = load_assessment(db_path, area_key, now=now)
    return (sorted(flatten(a), key=lambda i: (-i["risk_score"], i["name"])), a) if a else ([], None)


def summary_for_alert(db_path, area_key, limit=3, max_age_minutes=None, now=None):
    """Top flagged streets for a Telegram message, or ([], None) if no fresh
    assessment exists. Never fabricates: no cache -> no street list."""
    max_age = STREET_STALE_MINUTES if max_age_minutes is None else max_age_minutes
    a = load_assessment(db_path, area_key, max_age_minutes=max_age, now=now)
    if not a:
        return [], None
    flagged = [s for s in a["streets"] if s["above_threshold"]][:limit]
    return flagged, a