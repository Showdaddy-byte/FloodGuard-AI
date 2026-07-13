import math
import os
import sqlite3
from datetime import datetime

import requests
from flask import Flask, g, jsonify, render_template, request


app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "community.db")

CATEGORY_LABELS = {
    "flooding": "Flooding Observed",
    "construction": "Construction / Drainage Blockage",
    "road": "Road Closure or Damage",
    "infrastructure": "Bridge / Dam / Infrastructure Concern",
    "other": "Other Local Observation",
}

API_KEY = os.getenv("OPENWEATHER_API_KEY")
TIDE_API_KEY = os.getenv("TIDE_API_KEY")  # optional — WorldTides free tier; tidal factor is skipped if unset

OPENWEATHER_URL = "https://api.openweathermap.org/data/2.5"
OPENWEATHER_GEO_URL = "https://api.openweathermap.org/geo/1.0/direct"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"
WORLDTIDES_URL = "https://www.worldtides.info/api/v3"

# How recent a "flooding observed" report must be to count as live ground-truth
GROUND_TRUTH_WINDOW_HOURS = 12


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_key TEXT NOT NULL,
            city_label TEXT NOT NULL,
            category TEXT NOT NULL,
            rating INTEGER NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def normalize_city(city):
    return " ".join(city.strip().lower().split())


def save_contribution(city, category, rating, comment):
    db = get_db()
    db.execute(
        "INSERT INTO contributions (city_key, city_label, category, rating, comment, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (normalize_city(city), city.strip(), category, rating, comment.strip(), datetime.utcnow().isoformat()),
    )
    db.commit()


def get_city_contributions(city, limit=12):
    db = get_db()
    rows = db.execute(
        "SELECT city_label, category, rating, comment, created_at FROM contributions "
        "WHERE city_key = ? ORDER BY id DESC LIMIT ?",
        (normalize_city(city), limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_city_stats(city):
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) AS total, AVG(rating) AS avg_rating FROM contributions WHERE city_key = ?",
        (normalize_city(city),),
    ).fetchone()

    category_rows = db.execute(
        "SELECT category, COUNT(*) AS count FROM contributions WHERE city_key = ? GROUP BY category",
        (normalize_city(city),),
    ).fetchall()

    total = row["total"] or 0
    avg_rating = round(row["avg_rating"], 1) if row["avg_rating"] else 0
    category_counts = {r["category"]: r["count"] for r in category_rows}

    return {
        "total": total,
        "average_rating": avg_rating,
        "category_counts": category_counts,
        "construction_reports": category_counts.get("construction", 0) + category_counts.get("infrastructure", 0),
        "flooding_reports": category_counts.get("flooding", 0),
    }


def get_recent_flooding_reports(city, hours=GROUND_TRUTH_WINDOW_HOURS, min_rating=4):
    """Live visitor reports of active flooding in the last `hours`, used to
    override a model-only verdict when people on the ground say it's flooding."""
    from datetime import timedelta

    db = get_db()
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    rows = db.execute(
        "SELECT city_label, rating, comment, created_at FROM contributions "
        "WHERE city_key = ? AND category = 'flooding' AND rating >= ? AND created_at >= ? "
        "ORDER BY id DESC",
        (normalize_city(city), min_rating, cutoff),
    ).fetchall()
    return [dict(row) for row in rows]


def get_historical_frequency(city):
    """Proxy for historical flood frequency, built from our own community
    reports over time. This is NOT a substitute for a true historical flood
    archive (GDACS / Dartmouth Flood Observatory / EM-DAT) — those require
    downloading and hosting static datasets rather than a live point query,
    which is a Phase 3 infrastructure task. This proxy improves as more
    visitors contribute over time."""
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) AS total FROM contributions WHERE city_key = ? AND category = 'flooding'",
        (normalize_city(city),),
    ).fetchone()
    return row["total"] or 0


def total_contributions_count():
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM contributions").fetchone()[0]
    conn.close()
    return total


init_db()


def fetch_openweather(endpoint, params):
    if not API_KEY:
        print("Missing OPENWEATHER_API_KEY environment variable.")
        return None

    try:
        response = requests.get(f"{OPENWEATHER_URL}/{endpoint}", params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as error:
        print(f"OpenWeather request failed: {error}")
        return None


def geocode_location(query):
    """Resolve a free-text place name (city, neighborhood, suburb) to precise
    coordinates. This is what lets Lekki and Maryland resolve to different
    points instead of both collapsing into one city-wide weather reading."""
    if not API_KEY:
        return None

    try:
        response = requests.get(
            OPENWEATHER_GEO_URL,
            params={"q": query, "limit": 1, "appid": API_KEY},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
    except requests.RequestException as error:
        print(f"Geocoding request failed: {error}")
        return None

    if not results:
        return None

    place = results[0]
    return {
        "lat": place["lat"],
        "lon": place["lon"],
        "name": place.get("name", query),
        "state": place.get("state", ""),
        "country": place.get("country", ""),
    }


def fetch_elevation(lat, lon):
    """Real per-coordinate elevation, since flood exposure at a given rainfall
    level depends heavily on how low-lying and coastal a specific point is —
    a single city-wide score can't capture that Lekki sits near sea level
    while Maryland/Gbagada Phase 1&2 sit meaningfully higher."""
    try:
        response = requests.get(
            ELEVATION_URL,
            params={"latitude": lat, "longitude": lon},
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        values = data.get("elevation", [])
        return values[0] if values else None
    except (requests.RequestException, KeyError, IndexError, ValueError) as error:
        print(f"Elevation request failed: {error}")
        return None


def haversine_meters(lat1, lon1, lat2, lon2):
    r = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fetch_elevation_grid(lat, lon, offset_deg=0.0027):
    """One batched Open-Meteo call for the center point plus four points
    ~300m N/S/E/W, used to compute both elevation and slope without extra
    round trips."""
    lon_offset = offset_deg / max(math.cos(math.radians(lat)), 0.01)
    points = [
        (lat, lon),
        (lat + offset_deg, lon),
        (lat - offset_deg, lon),
        (lat, lon + lon_offset),
        (lat, lon - lon_offset),
    ]
    lats = ",".join(str(p[0]) for p in points)
    lons = ",".join(str(p[1]) for p in points)

    try:
        response = requests.get(ELEVATION_URL, params={"latitude": lats, "longitude": lons}, timeout=8)
        response.raise_for_status()
        values = response.json().get("elevation", [])
        if len(values) < 5 or any(v is None for v in values):
            return None, None
    except (requests.RequestException, ValueError) as error:
        print(f"Elevation grid request failed: {error}")
        return None, None

    center = values[0]
    spread = max(values[1:]) - min(values[1:])
    slope_percent = (spread / (offset_deg * 111000)) * 100  # rise/run as a percentage
    return center, round(slope_percent, 1)


def classify_slope(slope_percent):
    if slope_percent is None:
        return {"score_bonus": 0, "label": "Slope data unavailable", "status": "Slope lookup failed."}
    if slope_percent < 1:
        return {
            "score_bonus": 9,
            "label": f"Very flat terrain (~{slope_percent}% grade)",
            "status": "Flat ground drains slowly and retains standing water.",
        }
    if slope_percent < 3:
        return {
            "score_bonus": 5,
            "label": f"Gentle slope (~{slope_percent}% grade)",
            "status": "Modest drainage gradient; water clears slowly.",
        }
    if slope_percent < 8:
        return {
            "score_bonus": 1,
            "label": f"Moderate slope (~{slope_percent}% grade)",
            "status": "Reasonable natural drainage gradient.",
        }
    return {
        "score_bonus": -4,
        "label": f"Steep terrain (~{slope_percent}% grade)",
        "status": "Steep gradient drains quickly, lowering standing-water risk.",
    }


def fetch_water_and_urban_context(lat, lon, radius_m=3000):
    """Single Overpass (OpenStreetMap) query covering both nearby
    water/coastline features and built-up density within radius_m, to keep
    this to one external call instead of two."""
    query = f"""
    [out:json][timeout:12];
    (
      way["natural"="coastline"](around:{radius_m},{lat},{lon});
      way["natural"="water"](around:{radius_m},{lat},{lon});
      way["waterway"~"river|stream|canal"](around:{radius_m},{lat},{lon});
      node["place"="sea"](around:{radius_m},{lat},{lon});
    );
    out center 40;
    (
      nwr["building"](around:600,{lat},{lon});
    );
    out count;
    """
    try:
        response = requests.post(OVERPASS_URL, data={"data": query}, timeout=14)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"Overpass request failed: {error}")
        return None, None

    elements = data.get("elements", [])
    water_points = []
    building_count = 0

    for el in elements:
        if el.get("type") == "count":
            building_count = int(el.get("tags", {}).get("buildings", 0) or 0)
            continue
        center = el.get("center")
        if center:
            water_points.append((center["lat"], center["lon"]))
        elif el.get("type") == "node":
            water_points.append((el.get("lat"), el.get("lon")))

    nearest_water_m = None
    if water_points:
        nearest_water_m = min(haversine_meters(lat, lon, wp[0], wp[1]) for wp in water_points)

    return nearest_water_m, building_count


def classify_water_proximity(distance_m):
    if distance_m is None:
        return {
            "score_bonus": 0,
            "label": "No major water body detected nearby",
            "status": "No coastline, river, or lake found within 3 km in OpenStreetMap data.",
        }
    if distance_m <= 500:
        return {
            "score_bonus": 18,
            "label": f"~{distance_m:.0f} m from open water",
            "status": "Very close to a river, lake, or coastline — high overflow/surge exposure.",
        }
    if distance_m <= 2000:
        return {
            "score_bonus": 11,
            "label": f"~{distance_m/1000:.1f} km from open water",
            "status": "Close enough to a river, lake, or coastline for overflow to matter.",
        }
    if distance_m <= 5000:
        return {
            "score_bonus": 4,
            "label": f"~{distance_m/1000:.1f} km from open water",
            "status": "Moderate distance from major water bodies.",
        }
    return {
        "score_bonus": 0,
        "label": f"~{distance_m/1000:.1f} km from open water",
        "status": "No major water body close by.",
    }


def classify_urbanization(building_count):
    if building_count is None:
        return {
            "score_bonus": 0,
            "label": "Building density unavailable",
            "status": "OpenStreetMap building lookup failed.",
        }
    if building_count >= 250:
        return {
            "score_bonus": 9,
            "label": f"Very high building density (~{building_count} buildings within 600 m)",
            "status": "Dense paved surfaces increase runoff and reduce natural absorption.",
        }
    if building_count >= 100:
        return {
            "score_bonus": 5,
            "label": f"High building density (~{building_count} buildings within 600 m)",
            "status": "Significant paved surface area nearby.",
        }
    if building_count >= 30:
        return {
            "score_bonus": 2,
            "label": f"Moderate building density (~{building_count} buildings within 600 m)",
            "status": "Some paved surface, some open ground.",
        }
    return {
        "score_bonus": 0,
        "label": f"Low building density (~{building_count} buildings within 600 m)",
        "status": "Mostly open or vegetated land, more natural absorption.",
    }


def fetch_soil_clay(lat, lon):
    """Topsoil clay content (0-5cm) from ISRIC SoilGrids. Higher clay content
    absorbs water more slowly, worsening waterlogging and runoff."""
    try:
        response = requests.get(
            SOILGRIDS_URL,
            params={"lon": lon, "lat": lat, "property": "clay", "depth": "0-5cm", "value": "mean"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        layers = data.get("properties", {}).get("layers", [])
        for layer in layers:
            if layer.get("name") == "clay":
                depth_values = layer.get("depths", [])
                if depth_values:
                    raw = depth_values[0]["values"].get("mean")
                    if raw is not None:
                        return raw / 10  # SoilGrids returns g/kg *10; convert to %
    except (requests.RequestException, ValueError, KeyError, IndexError) as error:
        print(f"SoilGrids request failed: {error}")
    return None


def classify_soil(clay_percent):
    if clay_percent is None:
        return {
            "score_bonus": 0,
            "label": "Soil data unavailable",
            "status": "SoilGrids lookup failed.",
        }
    if clay_percent >= 40:
        return {
            "score_bonus": 7,
            "label": f"High-clay soil (~{clay_percent:.0f}% clay)",
            "status": "Clay-heavy soil absorbs water slowly, worsening waterlogging.",
        }
    if clay_percent >= 25:
        return {
            "score_bonus": 3,
            "label": f"Moderate-clay soil (~{clay_percent:.0f}% clay)",
            "status": "Moderate water absorption capacity.",
        }
    return {
        "score_bonus": -2,
        "label": f"Sandy/well-draining soil (~{clay_percent:.0f}% clay)",
        "status": "Better natural water absorption.",
    }


def fetch_tide_status(lat, lon):
    """Optional — only runs if TIDE_API_KEY (WorldTides) is configured."""
    if not TIDE_API_KEY:
        return None

    try:
        response = requests.get(
            WORLDTIDES_URL,
            params={"heights": "", "lat": lat, "lon": lon, "key": TIDE_API_KEY, "duration": 0},
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        heights = data.get("heights", [])
        if not heights:
            return None
        return heights[0].get("height")
    except (requests.RequestException, ValueError, KeyError, IndexError) as error:
        print(f"WorldTides request failed: {error}")
        return None


def classify_tide(height_m):
    if height_m is None:
        return None
    if height_m >= 0.6:
        return {
            "score_bonus": 8,
            "label": f"High tide (~{height_m:.1f} m)",
            "status": "High tide reduces drainage capacity for coastal outfalls.",
        }
    if height_m >= 0.2:
        return {
            "score_bonus": 3,
            "label": f"Mid tide (~{height_m:.1f} m)",
            "status": "Moderate tidal influence on coastal drainage.",
        }
    return {
        "score_bonus": 0,
        "label": f"Low tide (~{height_m:.1f} m)",
        "status": "Low tide — coastal drainage largely unobstructed.",
    }


def classify_terrain(elevation):
    """Elevation-based vulnerability. Low-lying/coastal terrain floods at
    rainfall levels that wouldn't trouble higher ground, independent of the
    day's weather — this is the missing "different data per region" factor."""
    if elevation is None:
        return {
            "score": 5,
            "score_bonus": 0,
            "label": "Elevation data unavailable",
            "status": "Elevation lookup failed — terrain risk not yet factored in for this location.",
        }
    if elevation <= 3:
        return {
            "score": 10,
            "score_bonus": 22,
            "label": f"Sea-level / coastal lowland (~{elevation:.0f} m)",
            "status": "Extremely flood-prone terrain — can flood at rainfall levels that wouldn't affect higher ground.",
        }
    if elevation <= 10:
        return {
            "score": 8,
            "score_bonus": 14,
            "label": f"Low-lying terrain (~{elevation:.0f} m)",
            "status": "Flood-prone with moderate rainfall due to low elevation.",
        }
    if elevation <= 25:
        return {
            "score": 5,
            "score_bonus": 6,
            "label": f"Moderately low terrain (~{elevation:.0f} m)",
            "status": "Some flood exposure; drainage quality matters more here.",
        }
    if elevation <= 60:
        return {
            "score": 3,
            "score_bonus": 0,
            "label": f"Elevated terrain (~{elevation:.0f} m)",
            "status": "Lower inherent flood exposure from elevation alone.",
        }
    return {
        "score": 1,
        "score_bonus": -6,
        "label": f"Highland terrain (~{elevation:.0f} m)",
        "status": "Flooding from rainfall alone is unlikely at this elevation.",
    }


def weather_scene(weather_id, description):
    description = (description or "").lower()

    if 200 <= weather_id < 300 or "thunder" in description:
        return {
            "code": "storm",
            "label": "Thunderstorm conditions",
            "summary": "Electrical storm signals detected. Avoid exposed routes and flooded roads.",
        }
    if 300 <= weather_id < 600 or "rain" in description or "drizzle" in description:
        return {
            "code": "rain",
            "label": "Rainfall conditions",
            "summary": "Rainfall is active or expected. Watch drainage channels and low-lying roads.",
        }
    if 600 <= weather_id < 700 or "snow" in description:
        return {
            "code": "snow",
            "label": "Cold precipitation",
            "summary": "Cold precipitation can reduce visibility and increase travel risk.",
        }
    if 700 <= weather_id < 800 or "mist" in description or "fog" in description or "haze" in description:
        return {
            "code": "mist",
            "label": "Low visibility",
            "summary": "Visibility may be reduced. Flood monitoring remains active.",
        }
    if weather_id == 800 or "clear" in description:
        return {
            "code": "clear",
            "label": "Clear conditions",
            "summary": "Current sky condition is clear. FloodGuard continues monitoring forecast changes.",
        }
    return {
        "code": "clouds",
        "label": "Cloudy conditions",
        "summary": "Cloud cover is present. Forecast rainfall is included in the flood score.",
    }


def classify_risk(score):
    if score >= 85:
        return {
            "level": "CRITICAL",
            "color": "critical",
            "map_color": "#7f1d1d",
            "advice": "Severe flood conditions are likely. Avoid low bridges, move valuables upward, and prepare to evacuate.",
        }
    if score >= 65:
        return {
            "level": "SEVERE",
            "color": "severe",
            "map_color": "#b91c1c",
            "advice": "Serious flood risk given local terrain and conditions. Avoid low-lying roads and waterside routes.",
        }
    if score >= 45:
        return {
            "level": "HIGH",
            "color": "high",
            "map_color": "#dc2626",
            "advice": "High flood risk. Stay away from drainage channels and monitor official emergency updates.",
        }
    if score >= 25:
        return {
            "level": "WATCH",
            "color": "watch",
            "map_color": "#f59e0b",
            "advice": "Elevated flood watch. Watch rainfall updates and avoid unnecessary travel through low areas.",
        }
    return {
        "level": "LOW",
        "color": "low",
        "map_color": "#16a34a",
        "advice": "No immediate flood signal, but continue monitoring local weather conditions.",
    }


def estimate_environment(city, weather, community=None, context=None):
    rainfall = weather["rainfall"]
    community = community or {"total": 0, "average_rating": 0, "construction_reports": 0, "flooding_reports": 0}
    context = context or {}

    terrain = context.get("terrain") or {
        "score": 5,
        "label": "Elevation data unavailable",
        "status": "Elevation lookup failed — terrain risk not yet factored in for this location.",
    }
    slope = context.get("slope") or {"label": "Slope data unavailable", "status": "Slope lookup failed."}
    water = context.get("water") or {"label": "Water proximity unavailable", "status": "OpenStreetMap lookup failed."}
    soil = context.get("soil") or {"label": "Soil data unavailable", "status": "SoilGrids lookup failed."}
    urban = context.get("urban") or {"label": "Building density unavailable", "status": "OpenStreetMap lookup failed."}
    tide = context.get("tide")
    historical_reports = context.get("historical_reports", 0)

    drainage_score = 8 if rainfall >= 30 else 6 if rainfall >= 10 else 3

    # Base construction/land-use score from weather, boosted by live community reports
    construction_score = 5
    construction_score += min(4, community["construction_reports"])
    construction_score = min(10, construction_score)

    # Community-perceived risk nudges drainage estimate slightly, since residents
    # often notice blocked drains and standing water before sensors or models do
    if community["total"] >= 3:
        drainage_score = min(10, round(drainage_score * 0.7 + community["average_rating"] / 5 * 10 * 0.3))

    if community["total"] > 0:
        community_note = (
            f" {community['total']} community report(s) submitted so far, averaging "
            f"{community['average_rating']}/5 perceived risk."
        )
    else:
        community_note = " No community reports yet for this city — be the first to contribute."

    layers = {
        "terrain": {
            "label": terrain["label"],
            "score": terrain["score"],
            "status": terrain["status"],
        },
        "slope": {
            "label": slope["label"],
            "score": max(0, min(10, 5 + slope.get("score_bonus", 0))),
            "status": slope["status"],
        },
        "water_proximity": {
            "label": water["label"],
            "score": max(0, min(10, water.get("score_bonus", 0))),
            "status": water["status"],
        },
        "soil": {
            "label": soil["label"],
            "score": max(0, min(10, 5 + soil.get("score_bonus", 0))),
            "status": soil["status"],
        },
        "urbanization": {
            "label": urban["label"],
            "score": max(0, min(10, urban.get("score_bonus", 0))),
            "status": urban["status"],
        },
        "drainage": {
            "label": "Drainage overload estimate",
            "score": drainage_score,
            "status": "Estimated from rainfall intensity and live community reports."
            if community["total"] >= 3
            else "Estimated from current rainfall intensity.",
        },
        "construction": {
            "label": "Construction and land-use impact",
            "score": construction_score,
            "status": f"{community['construction_reports']} live construction/drainage report(s) from visitors."
            if community["construction_reports"]
            else "Ready for roads, bridges, dams, and construction datasets.",
        },
        "historical": {
            "label": f"Community-reported flooding history: {historical_reports} report(s)"
            if historical_reports
            else "No community flooding history recorded yet",
            "score": min(10, historical_reports * 2),
            "status": "Proxy based on past visitor reports, not a certified historical flood archive.",
        },
    }

    if tide:
        layers["tide"] = {
            "label": tide["label"],
            "score": max(0, min(10, tide.get("score_bonus", 0))),
            "status": tide["status"],
        }

    layers["summary"] = f"{city} is being evaluated with weather, terrain, water proximity, soil, and urbanization signals, plus live visitor contributions.{community_note}"

    return layers


def calculate_flood_score(weather, forecast, context=None):
    """Combines rainfall/forecast/humidity/pressure/wind with terrain,
    slope, water proximity, soil, urbanization, tide, and community-observed
    frequency — rather than rainfall alone, matching how systems like
    Copernicus EMS or GDACS combine multiple layers instead of one signal."""
    score = 0
    factors = []
    context = context or {}
    terrain = context.get("terrain") or {"score_bonus": 0, "label": "Elevation data unavailable"}
    slope = context.get("slope") or {"score_bonus": 0, "label": "Slope data unavailable"}
    water = context.get("water") or {"score_bonus": 0, "label": "Water proximity unavailable"}
    soil = context.get("soil") or {"score_bonus": 0, "label": "Soil data unavailable"}
    urban = context.get("urban") or {"score_bonus": 0, "label": "Building density unavailable"}
    tide = context.get("tide")
    historical_reports = context.get("historical_reports", 0)

    rainfall = weather["rainfall"]
    humidity = weather["humidity"]
    pressure = weather["pressure"]
    wind_speed = weather["wind"]
    forecast_rain_total = sum(day["rain"] for day in forecast)
    max_forecast_rain = max([day["rain"] for day in forecast], default=0)

    if rainfall >= 50:
        score += 32
        factors.append("Extreme current rainfall")
    elif rainfall >= 20:
        score += 22
        factors.append("Heavy current rainfall")
    elif rainfall >= 5:
        score += 10
        factors.append("Active rainfall")

    if forecast_rain_total >= 80:
        score += 20
        factors.append("Very wet 5-day forecast")
    elif forecast_rain_total >= 35:
        score += 12
        factors.append("Sustained rainfall expected")
    elif max_forecast_rain >= 10:
        score += 6
        factors.append("One or more rainy forecast periods")

    if humidity >= 90:
        score += 10
        factors.append("Very high humidity")
    elif humidity >= 75:
        score += 5
        factors.append("High humidity")

    if pressure <= 995:
        score += 8
        factors.append("Low atmospheric pressure")
    elif pressure <= 1005:
        score += 4
        factors.append("Falling pressure signal")

    if wind_speed >= 12:
        score += 5
        factors.append("Strong wind may worsen storm impact")
    elif wind_speed >= 8:
        score += 3
        factors.append("Moderate wind")

    # Terrain, slope, water proximity, soil, and urbanization are independent
    # of today's weather — this is what lets two places with the same
    # rainfall land on very different risk levels.
    for layer, label_prefix in ((terrain, "Terrain"), (slope, "Slope"), (water, "Water proximity"), (soil, "Soil"), (urban, "Urbanization")):
        bonus = layer.get("score_bonus", 0)
        if bonus:
            score += bonus
            factors.append(f"{label_prefix}: {layer['label']}")

    if tide:
        bonus = tide.get("score_bonus", 0)
        if bonus:
            score += bonus
            factors.append(f"Tide: {tide['label']}")

    if historical_reports >= 6:
        score += 10
        factors.append(f"Community-reported flooding history: {historical_reports} past reports at this location")
    elif historical_reports >= 3:
        score += 6
        factors.append(f"Community-reported flooding history: {historical_reports} past reports at this location")
    elif historical_reports >= 1:
        score += 3
        factors.append(f"Community-reported flooding history: {historical_reports} past report(s) at this location")

    score = max(0, min(score, 100))
    risk = classify_risk(score)

    return {
        "score": score,
        "confidence": min(95, 68 + score // 3),
        "risk": risk["level"],
        "risk_color": risk["color"],
        "map_color": risk["map_color"],
        "advice": risk["advice"],
        "factors": factors or ["No major flood trigger detected from current conditions"],
    }


def get_forecast(lat, lon):
    data = fetch_openweather(
        "forecast",
        {"lat": lat, "lon": lon, "appid": API_KEY, "units": "metric"},
    )
    if not data:
        return []

    forecast = []
    seen_dates = set()

    for item in data.get("list", []):
        forecast_time = datetime.strptime(item["dt_txt"], "%Y-%m-%d %H:%M:%S")
        date_key = forecast_time.strftime("%Y-%m-%d")

        if date_key in seen_dates:
            continue

        seen_dates.add(date_key)
        rainfall = item.get("rain", {}).get("3h", 0)
        weather_id = item["weather"][0]["id"]
        description = item["weather"][0]["description"].title()
        scene = weather_scene(weather_id, description)

        if rainfall >= 20:
            day_risk = "HIGH RISK"
            day_color = "high-risk"
        elif rainfall >= 5:
            day_risk = "WATCH"
            day_color = "medium-risk"
        else:
            day_risk = "SAFE"
            day_color = "low-risk"

        forecast.append(
            {
                "day": forecast_time.strftime("%A"),
                "date": forecast_time.strftime("%d %b"),
                "time": forecast_time.strftime("%I:%M %p"),
                "temp": round(item["main"]["temp"], 1),
                "rain": rainfall,
                "weather": description,
                "humidity": item["main"]["humidity"],
                "wind": item["wind"]["speed"],
                "risk": day_risk,
                "risk_color": day_color,
                "scene": scene["code"],
            }
        )

        if len(forecast) == 5:
            break

    return forecast


def get_weather(lat, lon, display_name=None):
    data = fetch_openweather(
        "weather",
        {"lat": lat, "lon": lon, "appid": API_KEY, "units": "metric"},
    )
    if not data:
        return None

    description = data["weather"][0]["description"].title()
    weather_id = data["weather"][0]["id"]
    rainfall = data.get("rain", {}).get("1h", data.get("rain", {}).get("3h", 0))
    scene = weather_scene(weather_id, description)

    return {
        "city": display_name or data["name"],
        "country": data.get("sys", {}).get("country", ""),
        "description": description,
        "weather_id": weather_id,
        "scene": scene,
        "temperature": round(data["main"]["temp"], 1),
        "feels_like": round(data["main"]["feels_like"], 1),
        "humidity": data["main"]["humidity"],
        "pressure": data["main"]["pressure"],
        "wind": data["wind"]["speed"],
        "rainfall": rainfall,
        "latitude": data["coord"]["lat"],
        "longitude": data["coord"]["lon"],
    }


def build_prediction(query):
    place = geocode_location(query)

    if place:
        lat, lon = place["lat"], place["lon"]
        location_bits = [place["name"]]
        if place.get("state"):
            location_bits.append(place["state"])
        display_name = ", ".join(location_bits)
    else:
        # Geocoding failed (unrecognized place name) — nothing to look up.
        return None, []

    weather = get_weather(lat, lon, display_name=display_name)
    if not weather:
        return None, []

    forecast = get_forecast(lat, lon)

    # Each external lookup is independent and fails gracefully on its own,
    # so one flaky service (e.g. Overpass) can't take down the whole result.
    elevation, slope_percent = fetch_elevation_grid(lat, lon)
    terrain = classify_terrain(elevation)
    slope = classify_slope(slope_percent)

    nearest_water_m, building_count = fetch_water_and_urban_context(lat, lon)
    water = classify_water_proximity(nearest_water_m)
    urban = classify_urbanization(building_count)

    clay_percent = fetch_soil_clay(lat, lon)
    soil = classify_soil(clay_percent)

    tide_height = fetch_tide_status(lat, lon)
    tide = classify_tide(tide_height)

    community = get_city_stats(weather["city"])
    historical_reports = get_historical_frequency(weather["city"])

    context = {
        "terrain": terrain,
        "slope": slope,
        "water": water,
        "soil": soil,
        "urban": urban,
        "tide": tide,
        "historical_reports": historical_reports,
    }

    flood_model = calculate_flood_score(weather, forecast, context)
    environment = estimate_environment(weather["city"], weather, community, context)

    # Ground-truth override: if visitors are actively reporting flooding right
    # now, that outranks a model that hasn't caught up yet. This is the exact
    # failure mode where the app said "safe" while a place was flooding.
    recent_flood_reports = get_recent_flooding_reports(weather["city"])
    ground_alert = None
    if recent_flood_reports:
        if flood_model["score"] < 55:
            flood_model["score"] = max(flood_model["score"], 55)
            risk = classify_risk(flood_model["score"])
            flood_model["risk"] = risk["level"]
            flood_model["risk_color"] = risk["color"]
            flood_model["map_color"] = risk["map_color"]
            flood_model["advice"] = risk["advice"]
            flood_model["factors"].insert(0, "Live visitor reports of active flooding (overrides weather-only estimate)")
        ground_alert = {
            "count": len(recent_flood_reports),
            "message": (
                f"{len(recent_flood_reports)} visitor(s) reported active flooding in "
                f"{weather['city']} within the last {GROUND_TRUTH_WINDOW_HOURS} hours."
            ),
        }

    return {
        **weather,
        **flood_model,
        "environment": environment,
        "community": community,
        "ground_alert": ground_alert,
        "elevation": round(elevation) if elevation is not None else None,
        "slope_percent": slope_percent,
        "nearest_water_m": round(nearest_water_m) if nearest_water_m is not None else None,
    }, forecast


@app.route("/", methods=["GET", "POST"])
def home():
    prediction = None
    forecast = []
    error = None
    reports = []

    if request.method == "POST":
        city = request.form.get("city", "").strip()
        if not city:
            error = "Please enter a city name."
        elif not API_KEY:
            error = "Weather API key is missing. Add OPENWEATHER_API_KEY to your hosting environment variables."
        else:
            prediction, forecast = build_prediction(city)
            if not prediction:
                error = "City not found or weather service unavailable."
            else:
                reports = get_city_contributions(prediction["city"])

    return render_template(
        "index.html",
        prediction=prediction,
        forecast=forecast,
        error=error,
        reports=reports,
        category_labels=CATEGORY_LABELS,
        total_contributions=total_contributions_count(),
    )


@app.route("/api/contribute", methods=["POST"])
def api_contribute():
    payload = request.get_json(silent=True) or request.form

    city = (payload.get("city") or "").strip()
    category = (payload.get("category") or "other").strip()
    comment = (payload.get("comment") or "").strip()

    try:
        rating = int(payload.get("rating", 0))
    except (TypeError, ValueError):
        rating = 0

    if not city:
        return jsonify({"ok": False, "error": "A city is required."}), 400
    if category not in CATEGORY_LABELS:
        return jsonify({"ok": False, "error": "Unknown report category."}), 400
    if rating < 1 or rating > 5:
        return jsonify({"ok": False, "error": "Rating must be between 1 and 5."}), 400
    if len(comment) > 400:
        return jsonify({"ok": False, "error": "Comment is too long (400 characters max)."}), 400

    save_contribution(city, category, rating, comment)

    return jsonify(
        {
            "ok": True,
            "stats": get_city_stats(city),
            "reports": get_city_contributions(city),
            "total_contributions": total_contributions_count(),
        }
    )


@app.route("/api/contributions/<city>")
def api_contributions(city):
    return jsonify(
        {
            "ok": True,
            "stats": get_city_stats(city),
            "reports": get_city_contributions(city),
            "total_contributions": total_contributions_count(),
        }
    )


@app.route("/health")
def health():
    return {"status": "ok", "service": "FloodGuard AI"}


if __name__ == "__main__":
    app.run(debug=True)
