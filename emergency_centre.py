"""
emergency_centre.py - FloodGuard AI Emergency & Rescue Assistance (worldwide).

What it provides for any place on Earth
---------------------------------------
  * Emergency NUMBERS for the place's country (and state where FloodGuard holds
    a sourced entry). Numbers come only from the tables in app.py / CURATED_AGENCIES
    below. Nothing is invented: an agency with no verified number is listed
    WITHOUT a number and a call button.
  * Nearest FACILITIES from OpenStreetMap: hospitals, fire stations, police,
    ambulance stations, rescue stations and mapped assembly points, ranked by
    straight-line distance, each with a call button (only if OpenStreetMap has a
    phone for it) and a directions link.

What it deliberately does NOT claim
-----------------------------------
  * "Nearest" is straight-line distance, not road distance or travel time.
  * A facility being mapped does not mean it is open, staffed or has an
    ambulance available. No live availability or dispatch status is shown,
    because no free, reliable feed for that exists. Say so, every time.
  * Government/private ownership and "has an emergency department" are shown
    only when OpenStreetMap tags (or, for ownership, a clear name such as
    "General Hospital" / "Teaching Hospital") say so; otherwise "not stated".
  * Official evacuation centres are rarely mapped. Only mapped assembly points
    are listed; follow local authorities for designated shelters.

Keeping contacts correct
------------------------
Every curated agency carries `source` and `verified_on` (YYYY-MM-DD or None).
The site shows "Verified <date>" only when verified_on is set, otherwise
"Not yet verified by FloodGuard". After you confirm a number with the agency,
set verified_on in CURATED_AGENCIES and redeploy. Wrong emergency numbers cost
lives, so leave verified_on as None until you have actually confirmed it.
"""

import math
import re
import time

# ---------------------------------------------------------------------------
# Curated agencies (numbers FloodGuard holds a source for)
# ---------------------------------------------------------------------------
# key: (COUNTRY_CODE, region_key or None). region_key = lower-case, no "state".
# services: any of "all", "fire", "ambulance", "rescue", "police", "road", "civil_defence", "relief"
CURATED_AGENCIES = {
    ("NG", None): [
        {"agency": "National Emergency Number", "services": ["all"], "numbers": ["112"],
         "note": "Connects to police, fire, medical and disaster response. Start here if unsure.",
         "source": "FloodGuard national emergency table", "verified_on": None},
        {"agency": "Federal Fire Service", "services": ["fire", "rescue"], "numbers": ["112"],
         "note": "Reached through 112.", "source": "FloodGuard owner brief (published agency information)", "verified_on": None},
        {"agency": "Federal Road Safety Corps (FRSC)", "services": ["road"], "numbers": ["122"],
         "note": "Road crashes and road hazards, including flooded roads.", "source": "FloodGuard owner brief (published agency information)", "verified_on": None},
        {"agency": "National Emergency Management Agency (NEMA)", "services": ["rescue", "relief"], "numbers": [],
         "note": "National flood response and relief. No verified number held yet: call 112 and ask for NEMA.", "source": None, "verified_on": None},
        {"agency": "Nigeria Security and Civil Defence Corps (NSCDC)", "services": ["civil_defence"], "numbers": [],
         "note": "No verified number held yet: call 112.", "source": None, "verified_on": None},
        {"agency": "Nigerian Red Cross Society", "services": ["relief", "ambulance"], "numbers": [],
         "note": "Disaster response and relief. No verified number held yet.", "source": None, "verified_on": None},
    ],
    ("NG", "lagos"): [
        {"agency": "Lagos State Emergency Management Agency (LASEMA)", "services": ["rescue", "all"], "numbers": ["767", "112"],
         "note": "Toll-free. Flood rescue and disaster response in Lagos State.", "source": "lasema.lagosstate.gov.ng", "verified_on": None},
        {"agency": "Lagos State Fire and Rescue Service", "services": ["fire", "rescue"], "numbers": ["112", "767"],
         "note": "", "source": "FloodGuard owner brief (published agency information)", "verified_on": None},
        {"agency": "Lagos State Ambulance Service (LASAMBUS)", "services": ["ambulance"], "numbers": ["112", "767"],
         "note": "", "source": "FloodGuard owner brief (published agency information)", "verified_on": None},
    ],
}


def _norm_region(text):
    words = [w for w in re.sub(r"[^\w\s]", " ", (text or "").lower()).split()
             if w not in {"state", "province", "region", "county", "of", "the", "territory"}]
    return " ".join(words)


def agencies_for(country_code, region=None):
    """Curated agencies for the country, plus the state/region entry when one exists."""
    cc = (country_code or "").upper()
    out = [dict(a, level="national") for a in CURATED_AGENCIES.get((cc, None), [])]
    key = _norm_region(region)
    if key:
        for (c, r), items in CURATED_AGENCIES.items():
            if c == cc and r and (r == key or r in key or key in r):
                out = [dict(a, level="state") for a in items] + out
                break
    return out


# ---------------------------------------------------------------------------
# Geometry / formatting helpers
# ---------------------------------------------------------------------------
def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def clean_phone(raw):
    """-> (display, tel) or (None, None). Keeps the FIRST number when several are
    listed. Never fabricates: unusable values return (None, None)."""
    if not raw:
        return None, None
    first = re.split(r"[;,/]|\bor\b", str(raw))[0].strip()
    digits = re.sub(r"\D", "", first)
    if len(digits) < 3 or len(digits) > 15:
        return None, None
    tel = ("+" if first.lstrip().startswith("+") else "") + digits
    return first[:40], tel


def directions_links(origin, dest):
    d = f"{dest[0]:.6f},{dest[1]:.6f}"
    google = f"https://www.google.com/maps/dir/?api=1&destination={d}&travelmode=driving"
    osm = f"https://www.openstreetmap.org/directions?engine=fossgis_osrm_car&route="
    if origin:
        google += f"&origin={origin[0]:.6f},{origin[1]:.6f}"
        osm += f"{origin[0]:.6f}%2C{origin[1]:.6f}%3B"
    else:
        osm += "%3B"
    osm += f"{dest[0]:.6f}%2C{dest[1]:.6f}"
    return {"google": google, "osm": osm}


# ---------------------------------------------------------------------------
# OpenStreetMap query and parsing
# ---------------------------------------------------------------------------
CATEGORIES = ("hospital", "ambulance", "fire", "rescue", "police", "shelter")
CATEGORY_LABEL = {
    "hospital": "Hospitals", "ambulance": "Ambulance stations", "fire": "Fire stations",
    "rescue": "Rescue stations", "police": "Police stations", "shelter": "Mapped assembly points / shelters",
}


def build_query(lat, lon, radius_m):
    r = int(radius_m)
    a = f"(around:{r},{lat:.6f},{lon:.6f})"
    return (
        "[out:json][timeout:25][maxsize:60000000];("
        f'nwr["amenity"="hospital"]{a};'
        f'nwr["amenity"="fire_station"]{a};'
        f'nwr["amenity"="police"]{a};'
        f'nwr["emergency"="ambulance_station"]{a};'
        f'nwr["amenity"="ambulance_station"]{a};'
        f'nwr["emergency"="rescue_station"]{a};'
        f'nwr["emergency"="assembly_point"]{a};'
        ");out center tags 500;"
    )


_GOV_NAME = re.compile(r"general hospital|teaching hospital|federal medical|primary health|comprehensive health|"
                       r"district hospital|government|state hospital|referral|military|police hospital|"
                       r"hôpital (?:public|universitaire)|hospital (?:público|general|universitario)", re.I)
_TEACHING = re.compile(r"teaching hospital|university (?:teaching )?hospital|universit(?:y|é|ario)", re.I)
_FMC = re.compile(r"federal medical cent", re.I)
_GENERAL = re.compile(r"general hospital|hospital general", re.I)


def classify_hospital(tags):
    """Ownership and type, from tags/name only. Unknown stays unknown."""
    name = tags.get("name") or ""
    op_type = (tags.get("operator:type") or "").lower()
    if op_type in ("government", "public"):
        ownership = "Government / public (OpenStreetMap operator tag)"
    elif op_type in ("private", "community", "religious", "ngo", "charity"):
        ownership = "Not government (" + op_type + ", OpenStreetMap operator tag)"
    elif _GOV_NAME.search(name):
        ownership = "Government (the name suggests this; confirm by phone)"
    else:
        ownership = "Ownership not stated"
    kinds = []
    if _TEACHING.search(name):
        kinds.append("Teaching hospital (by name)")
    if _FMC.search(name):
        kinds.append("Federal medical centre (by name)")
    if _GENERAL.search(name):
        kinds.append("General hospital (by name)")
    em = (tags.get("emergency") or "").lower()
    emergency_dept = ("Emergency department: yes (mapped)" if em == "yes"
                      else "Emergency department: no (mapped)" if em == "no"
                      else "Emergency department: not stated")
    return {"ownership": ownership, "government": ownership.startswith("Government"),
            "kinds": kinds, "emergency_dept": emergency_dept, "has_emergency_dept": em == "yes"}


def _category(tags):
    if tags.get("amenity") == "hospital":
        return "hospital"
    if tags.get("amenity") == "fire_station":
        return "fire"
    if tags.get("amenity") == "police":
        return "police"
    if tags.get("emergency") == "ambulance_station" or tags.get("amenity") == "ambulance_station":
        return "ambulance"
    if tags.get("emergency") == "rescue_station":
        return "rescue"
    if tags.get("emergency") == "assembly_point":
        return "shelter"
    return None


def _address(tags):
    street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
    parts = [p for p in (street, tags.get("addr:suburb") or tags.get("addr:neighbourhood"),
                         tags.get("addr:city") or tags.get("addr:town")) if p]
    return ", ".join(parts) or None


def parse_facilities(osm_json, lat, lon, per_category=6, hospitals=10):
    """-> dict category -> list (nearest first). Every field comes from OpenStreetMap
    tags or from arithmetic on coordinates."""
    best = {c: {} for c in CATEGORIES}
    for el in (osm_json or {}).get("elements", []):
        tags = el.get("tags") or {}
        cat = _category(tags)
        if not cat:
            continue
        c = el.get("center") or {}
        flat, flon = c.get("lat", el.get("lat")), c.get("lon", el.get("lon"))
        if flat is None or flon is None:
            continue
        dist = haversine_m(lat, lon, flat, flon)
        display, tel = clean_phone(tags.get("phone") or tags.get("contact:phone") or tags.get("contact:mobile")
                                   or tags.get("emergency:phone"))
        name = (tags.get("name") or "").strip()
        label_default = {"hospital": "Hospital (unnamed in OpenStreetMap)", "fire": "Fire station (unnamed)",
                         "police": "Police station (unnamed)", "ambulance": "Ambulance station (unnamed)",
                         "rescue": "Rescue station (unnamed)", "shelter": "Assembly point (unnamed)"}[cat]
        item = {
            "category": cat, "name": name or label_default, "named": bool(name),
            "lat": round(flat, 6), "lon": round(flon, 6), "distance_m": int(round(dist)),
            "phone": display, "tel": tel, "address": _address(tags),
            "osm": f"https://www.openstreetmap.org/{el.get('type', 'node')}/{el.get('id')}",
            "directions": directions_links((lat, lon), (flat, flon)),
        }
        if cat == "hospital":
            item.update(classify_hospital(tags))
        key = (item["name"].lower(), round(flat, 3), round(flon, 3))  # one OSM object may appear as node+way
        prev = best[cat].get(key)
        if prev is None or dist < prev["distance_m"]:
            best[cat][key] = item
    out = {}
    for cat in CATEGORIES:
        rows = sorted(best[cat].values(), key=lambda i: i["distance_m"])
        out[cat] = rows[: (hospitals if cat == "hospital" else per_category)]
        out[cat + "_total"] = len(rows)
    return out


# ---------------------------------------------------------------------------
# Lookup with cache (stale copy is served, and labelled, if OpenStreetMap is down)
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS = 6 * 3600
_cache = {}


def nearby_facilities(lat, lon, run_overpass, radius_m=8000, now=None):
    """Returns {"ok": bool, "facilities": {...}|None, "stale": bool, "age_minutes": int|None, "error": str|None}."""
    now = now if now is not None else time.time()
    key = (round(lat, 2), round(lon, 2), int(radius_m))
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL_SECONDS:
        # re-rank from the cached raw data so distances are measured from THIS point
        return {"ok": True, "facilities": parse_facilities(hit[1], lat, lon), "stale": False,
                "age_minutes": int((now - hit[0]) // 60), "error": None}
    data = run_overpass(build_query(lat, lon, radius_m))
    if data is not None and (data.get("elements") or not data.get("remark")):
        _cache[key] = (now, data)
        if len(_cache) > 300:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                _cache.pop(k, None)
        return {"ok": True, "facilities": parse_facilities(data, lat, lon), "stale": False, "age_minutes": 0, "error": None}
    if hit:
        return {"ok": True, "facilities": parse_facilities(hit[1], lat, lon), "stale": True,
                "age_minutes": int((now - hit[0]) // 60), "error": None}
    return {"ok": False, "facilities": None, "stale": False, "age_minutes": None,
            "error": "OpenStreetMap facility data could not be retrieved right now. The emergency numbers above still apply."}


CAVEATS = [
    "Distances are straight-line, not driving distance or travel time.",
    "A facility appearing here does not mean it is open or that an ambulance is available. FloodGuard has no live availability or dispatch data.",
    "Facilities and phone numbers come from OpenStreetMap, edited by volunteers, and may be missing or out of date. Call the emergency number first in a life-threatening situation.",
    "Ownership (government or private), type and emergency-department status are shown only when OpenStreetMap or the facility's name says so.",
    "Official evacuation centres are rarely mapped. Only mapped assembly points are listed; follow instructions from local authorities for designated shelters.",
]