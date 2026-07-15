import math
import os
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

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
MAPBOX_ACCESS_TOKEN = os.getenv("MAPBOX_ACCESS_TOKEN")  # optional — enables the live traffic map layer

OPENWEATHER_URL = "https://api.openweathermap.org/data/2.5"
OPENWEATHER_GEO_URL = "https://api.openweathermap.org/geo/1.0/direct"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"
WORLDTIDES_URL = "https://www.worldtides.info/api/v3"
# Real hydrological modeling — GloFAS (Global Flood Awareness System), the
# same Copernicus/ECMWF model professional flood forecasters use, exposed
# free via Open-Meteo. Gives forecasted river discharge (m3/s) plus a
# 30-year historical mean, so we can tell a river running at 3x normal from
# one at normal levels — genuine catchment-routing hydrology, not a rainfall
# proxy invented by this app.
FLOOD_API_URL = "https://flood-api.open-meteo.com/v1/flood"
# Real-time soil saturation (ERA5-based), distinct from SoilGrids' static
# clay-content soil TYPE — this is current soil STATE, which matters because
# already-saturated ground can't absorb more rain regardless of soil type.
SOIL_MOISTURE_URL = "https://api.open-meteo.com/v1/forecast"

# How recent a "flooding observed" report must be to count as live ground-truth
GROUND_TRUTH_WINDOW_HOURS = 12

# How long a watchlist snapshot stays "fresh" before a page load triggers a
# background refresh. Keep this short enough that an ongoing flood event
# (like heavy rain hitting several Lagos neighborhoods at once) shows up for
# visitors without anyone needing to search that exact place first.
WATCHLIST_REFRESH_MINUTES = 15

# Elevation, slope, water proximity, soil type, and urbanization barely
# change hour to hour — there's no reason to re-hit Overpass/SoilGrids for
# them on every watchlist sweep. Caching this static geospatial context for
# a full day cuts external call volume by ~95%+, which is what actually
# fixes Overpass rate-limiting (406s) rather than just retrying harder.
GEO_CONTEXT_TTL_HOURS = 24

# Locations actively monitored for the homepage alert banner, independent of
# whether any visitor has searched them. Edit this list to match the areas
# that matter most for your audience — it doesn't need to be Lagos-only.
MONITORED_LOCATIONS = [
    # Lagos, Nigeria — original coastal/lowland focus
    "Ikoyi, Lagos",
    "Lekki, Lagos",
    "Victoria Island, Lagos",
    "Ajah, Lagos",
    "Bariga, Lagos",
    "Iyana Oworo, Lagos",
    "Gbagada, Lagos",
    "Somolu, Lagos",
    "Lagos Island, Lagos",
    "Apapa, Lagos",
    # Africa
    "Alexandria, Egypt",
    "Maputo, Mozambique",
    "Durban, South Africa",
    # Asia
    "Jakarta, Indonesia",
    "Dhaka, Bangladesh",
    "Mumbai, India",
    "Manila, Philippines",
    "Bangkok, Thailand",
    "Ho Chi Minh City, Vietnam",
    "Guangzhou, China",
    # Europe
    "Venice, Italy",
    "Amsterdam, Netherlands",
    "Hamburg, Germany",
    # North America
    "Miami, Florida",
    "New Orleans, Louisiana",
    "Houston, Texas",
    # South America
    "Rio de Janeiro, Brazil",
    "Buenos Aires, Argentina",
    # Oceania
    "Brisbane, Australia",
]

# A real, independent, worldwide flood-alert feed (Global Disaster Alert and
# Coordination System — used by UN OCHA and humanitarian agencies) so the
# site isn't limited to only the cities in MONITORED_LOCATIONS. This is what
# provides genuine "anywhere in the world" coverage, since running our own
# multi-factor terrain model against every location on Earth isn't possible
# with free, rate-limited REST APIs.
GDACS_RSS_URL = "https://www.gdacs.org/xml/rss.xml"
GLOBAL_ALERTS_REFRESH_MINUTES = 10


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_key TEXT NOT NULL,
            city_label TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            score INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist_cache (
            city_key TEXT PRIMARY KEY,
            city_label TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            risk_color TEXT NOT NULL,
            score INTEGER NOT NULL,
            top_factor TEXT,
            priority_action TEXT,
            ground_alert_message TEXT,
            coastal INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    try:
        conn.execute("ALTER TABLE watchlist_cache ADD COLUMN priority_action TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists — fine on repeated startups
    try:
        conn.execute("ALTER TABLE watchlist_cache ADD COLUMN coastal INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS global_alerts_cache (
            event_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            country TEXT,
            alert_level TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            event_url TEXT,
            published_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geo_context_cache (
            city_key TEXT PRIMARY KEY,
            elevation REAL,
            slope_percent REAL,
            nearest_water_m REAL,
            nearest_coast_m REAL,
            nearest_water_lat REAL,
            nearest_water_lon REAL,
            nearest_water_label TEXT,
            building_count INTEGER,
            clay_percent REAL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_locks (
            lock_name TEXT PRIMARY KEY,
            started_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def try_acquire_lock(lock_name, max_age_minutes=10):
    """A mutex that actually works across separate gunicorn worker
    processes, unlike an in-memory Python flag (which only guards within a
    single process — the real cause of duplicate simultaneous sweeps when
    a rolling deploy briefly runs two instances, or multiple workers each
    handle an early request at once). Stale locks (e.g. from a worker that
    crashed mid-refresh) are automatically reclaimed after max_age_minutes."""
    conn = sqlite3.connect(DB_PATH)
    now = datetime.utcnow()
    cutoff = (now - timedelta(minutes=max_age_minutes)).isoformat()
    conn.execute("DELETE FROM refresh_locks WHERE lock_name = ? AND started_at < ?", (lock_name, cutoff))
    conn.commit()

    acquired = False
    try:
        conn.execute("INSERT INTO refresh_locks (lock_name, started_at) VALUES (?, ?)", (lock_name, now.isoformat()))
        conn.commit()
        acquired = True
    except sqlite3.IntegrityError:
        acquired = False
    finally:
        conn.close()

    return acquired


def release_lock(lock_name):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM refresh_locks WHERE lock_name = ?", (lock_name,))
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


ALERT_LEVELS = ("HIGH", "SEVERE", "CRITICAL")


def log_search(city, risk_level, score):
    db = get_db()
    db.execute(
        "INSERT INTO searches (city_key, city_label, risk_level, score, created_at) VALUES (?, ?, ?, ?, ?)",
        (normalize_city(city), city.strip(), risk_level, score, datetime.utcnow().isoformat()),
    )
    db.commit()


def get_site_stats():
    """Real, site-wide numbers derived from actual searches — replaces the
    hardcoded '1 location monitored' / '0 alerts issued' placeholders."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    total_searches = conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
    distinct_locations = conn.execute("SELECT COUNT(DISTINCT city_key) FROM searches").fetchone()[0]
    placeholders = ",".join("?" for _ in ALERT_LEVELS)
    alerts_issued = conn.execute(
        f"SELECT COUNT(*) FROM searches WHERE risk_level IN ({placeholders})",
        ALERT_LEVELS,
    ).fetchone()[0]

    conn.close()

    return {
        "locations_monitored": distinct_locations,
        "total_searches": total_searches,
        "alerts_issued": alerts_issued,
    }


_watchlist_refresh_lock = threading.Lock()
_watchlist_refreshing = False


def get_all_monitored_locations():
    """Static curated list, plus every distinct location anyone has ever
    searched — so a place a visitor checks stays under continuous
    monitoring afterward instead of only being watched at the moment of
    that one search."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT DISTINCT city_label FROM searches").fetchall()
    conn.close()

    searched = [row[0] for row in rows]
    combined = list(MONITORED_LOCATIONS)
    seen = {normalize_city(loc) for loc in combined}
    for label in searched:
        key = normalize_city(label)
        if key not in seen:
            seen.add(key)
            combined.append(label)
    return combined


def _upsert_watchlist_row(conn, prediction, timestamp):
    top_factor = prediction["factors"][0] if prediction.get("factors") else None
    ground_message = prediction["ground_alert"]["message"] if prediction.get("ground_alert") else None
    priority_action = prediction.get("priority_action")
    coastal = 1 if prediction.get("coastal") else 0

    conn.execute(
        """
        INSERT INTO watchlist_cache
            (city_key, city_label, risk_level, risk_color, score, top_factor, priority_action, ground_alert_message, coastal, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(city_key) DO UPDATE SET
            city_label=excluded.city_label,
            risk_level=excluded.risk_level,
            risk_color=excluded.risk_color,
            score=excluded.score,
            top_factor=excluded.top_factor,
            priority_action=excluded.priority_action,
            ground_alert_message=excluded.ground_alert_message,
            coastal=excluded.coastal,
            updated_at=excluded.updated_at
        """,
        (
            normalize_city(prediction["city"]),
            prediction["city"],
            prediction["risk"],
            prediction["risk_color"],
            prediction["score"],
            top_factor,
            priority_action,
            ground_message,
            coastal,
            timestamp,
        ),
    )
    conn.commit()


def cache_watchlist_entry_now(prediction):
    """Immediately update the watchlist cache for a single just-searched
    location, instead of waiting for the next periodic sweep. This is what
    ensures a place someone actually checks reflects its real risk on the
    homepage banner right away, not up to WATCHLIST_REFRESH_MINUTES later."""
    if not prediction:
        return
    conn = sqlite3.connect(DB_PATH)
    _upsert_watchlist_row(conn, prediction, datetime.utcnow().isoformat())
    conn.close()


def refresh_watchlist_cache():
    """Recompute a fresh prediction for every monitored location and cache it.
    This is what lets a serious, ongoing event (heavy rain hitting several
    coastal neighborhoods at once) show up on the homepage for any visitor,
    not just someone who already knew to search that exact place."""
    conn = sqlite3.connect(DB_PATH)
    now = datetime.utcnow().isoformat()
    locations = get_all_monitored_locations()

    for index, location in enumerate(locations):
        try:
            with app.app_context():
                prediction, _ = build_prediction(location)
        except Exception as error:  # noqa: BLE001 — one bad location must not break the rest
            print(f"Watchlist refresh failed for {location}: {error}")
            continue

        if not prediction:
            continue

        _upsert_watchlist_row(conn, prediction, now)

        # Stagger requests so a cold-cache sweep across many locations
        # doesn't burst-hit Overpass/SoilGrids all at once (that burst is
        # what triggers their rate limiting in the first place).
        if index < len(locations) - 1:
            time.sleep(1.5)

    conn.close()


def maybe_refresh_watchlist_async():
    """Kick off a background refresh if the cache is stale. Non-blocking, so
    a visitor's page load is never delayed by the monitoring sweep."""
    global _watchlist_refreshing

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT MIN(updated_at) FROM watchlist_cache").fetchone()
    conn.close()

    oldest = row[0] if row else None
    is_stale = True
    if oldest:
        age_minutes = (datetime.utcnow() - datetime.fromisoformat(oldest)).total_seconds() / 60
        is_stale = age_minutes >= WATCHLIST_REFRESH_MINUTES

    # Cache has fewer rows than monitored locations (first run, or a new
    # location was searched) also counts as stale so new entries get picked up.
    conn = sqlite3.connect(DB_PATH)
    cached_count = conn.execute("SELECT COUNT(*) FROM watchlist_cache").fetchone()[0]
    conn.close()
    if cached_count < len(get_all_monitored_locations()):
        is_stale = True

    if not is_stale:
        return

    # Fast local check first (cheap, avoids a DB round-trip most of the time)...
    with _watchlist_refresh_lock:
        if _watchlist_refreshing:
            return
        _watchlist_refreshing = True

    # ...then the authoritative cross-process check. A rolling deploy that
    # briefly runs two instances, or multiple gunicorn workers each handling
    # an early request, would otherwise each pass the in-memory check above
    # and fire their own simultaneous sweep — this is what actually stops
    # duplicate sweeps from combining to exceed Overpass/Open-Meteo's rate
    # limits, as seen in production. The lock TTL is generous (60 min, well
    # above WATCHLIST_REFRESH_MINUTES) because a full sweep across many
    # locations with worst-case API timeouts can legitimately take a while;
    # reclaiming the lock mid-sweep would reintroduce the same duplication.
    if not try_acquire_lock("watchlist_refresh", max_age_minutes=60):
        with _watchlist_refresh_lock:
            _watchlist_refreshing = False
        return

    def _run():
        global _watchlist_refreshing
        try:
            refresh_watchlist_cache()
        finally:
            release_lock("watchlist_refresh")
            with _watchlist_refresh_lock:
                _watchlist_refreshing = False

    threading.Thread(target=_run, daemon=True).start()


def get_watchlist_status():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT city_label, risk_level, risk_color, score, top_factor, priority_action, ground_alert_message, coastal, updated_at "
        "FROM watchlist_cache ORDER BY score DESC"
    ).fetchall()
    conn.close()

    entries = [dict(row) for row in rows]
    for e in entries:
        e["coastal"] = bool(e.get("coastal"))
    active_alerts = [e for e in entries if e["risk_level"] in ALERT_LEVELS or e["ground_alert_message"]]
    oldest_update = min((e["updated_at"] for e in entries), default=None)

    # Never let the banner confidently say "no alerts" on data that's gone
    # stale — a missed refresh cycle (e.g. no external cron configured, or
    # low traffic between visits) shouldn't be presented as an all-clear.
    is_stale = False
    age_minutes = None
    if oldest_update:
        age_minutes = (datetime.utcnow() - datetime.fromisoformat(oldest_update)).total_seconds() / 60
        is_stale = age_minutes >= (WATCHLIST_REFRESH_MINUTES * 2)

    return {
        "entries": entries,
        "active_alerts": active_alerts,
        "initialized": len(entries) > 0,
        "last_updated": oldest_update,
        "is_stale": is_stale,
        "age_minutes": round(age_minutes) if age_minutes is not None else None,
    }


def _local_tag(tag):
    """Strip XML namespace prefix: '{uri}tagname' -> 'tagname'."""
    return tag.split("}")[-1] if "}" in tag else tag


def fetch_global_flood_alerts():
    """Pull the current worldwide flood alert list from GDACS (Global
    Disaster Alert and Coordination System) — the same feed used by UN OCHA
    and humanitarian agencies, not something computed by this app. This is
    what gives genuine 'anywhere in the world' coverage: running our own
    terrain model against every location on Earth isn't possible with free,
    rate-limited REST APIs, so real global coverage has to come from a
    source built for exactly this purpose."""
    try:
        response = requests.get(GDACS_RSS_URL, timeout=12)
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError) as error:
        print(f"GDACS feed request failed: {error}")
        return []

    alerts = []
    for item in root.iter():
        if _local_tag(item.tag) != "item":
            continue

        fields = {}
        lat = lon = None
        for child in item:
            name = _local_tag(child.tag)
            text = (child.text or "").strip()
            if name == "point" and text:
                parts = text.split()
                if len(parts) == 2:
                    try:
                        lat, lon = float(parts[0]), float(parts[1])
                    except ValueError:
                        pass
            elif name in ("eventtype", "alertlevel", "country", "title", "link", "pubDate", "eventid"):
                fields[name] = text

        if fields.get("eventtype") != "FL":  # FL = flood in GDACS's own taxonomy
            continue

        event_id = fields.get("eventid") or fields.get("link") or fields.get("title")
        if not event_id:
            continue

        alerts.append(
            {
                "event_id": event_id,
                "title": fields.get("title", "Flood alert"),
                "country": fields.get("country", ""),
                "alert_level": (fields.get("alertlevel") or "Green").strip().title(),
                "latitude": lat,
                "longitude": lon,
                "event_url": fields.get("link", ""),
                "published_at": fields.get("pubDate", ""),
            }
        )

    return alerts


def _gdacs_alert_to_risk(alert_level):
    """Map GDACS's own alert level to this app's risk vocabulary/styling."""
    level = (alert_level or "").lower()
    if level == "red":
        return {"level": "CRITICAL", "color": "critical"}
    if level == "orange":
        return {"level": "SEVERE", "color": "severe"}
    return {"level": "WATCH", "color": "watch"}


def refresh_global_alerts_cache():
    alerts = fetch_global_flood_alerts()
    if not alerts:
        return

    conn = sqlite3.connect(DB_PATH)
    now = datetime.utcnow().isoformat()
    seen_ids = []

    for alert in alerts:
        seen_ids.append(alert["event_id"])
        conn.execute(
            """
            INSERT INTO global_alerts_cache
                (event_id, title, country, alert_level, latitude, longitude, event_url, published_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                title=excluded.title,
                country=excluded.country,
                alert_level=excluded.alert_level,
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                event_url=excluded.event_url,
                published_at=excluded.published_at,
                updated_at=excluded.updated_at
            """,
            (
                alert["event_id"],
                alert["title"],
                alert["country"],
                alert["alert_level"],
                alert["latitude"],
                alert["longitude"],
                alert["event_url"],
                alert["published_at"],
                now,
            ),
        )

    # Drop alerts no longer present in the current feed (resolved/expired).
    if seen_ids:
        placeholders = ",".join("?" for _ in seen_ids)
        conn.execute(f"DELETE FROM global_alerts_cache WHERE event_id NOT IN ({placeholders})", seen_ids)

    conn.commit()
    conn.close()


_global_alerts_lock = threading.Lock()
_global_alerts_refreshing = False


def maybe_refresh_global_alerts_async():
    """Kick off a background GDACS refresh if stale. Runs independently of
    OPENWEATHER_API_KEY, since this feed needs no key at all — real global
    coverage works even before OpenWeather is configured."""
    global _global_alerts_refreshing

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT MAX(updated_at) FROM global_alerts_cache").fetchone()
    conn.close()

    last = row[0] if row else None
    is_stale = True
    if last:
        age_minutes = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() / 60
        is_stale = age_minutes >= GLOBAL_ALERTS_REFRESH_MINUTES

    if not is_stale:
        return

    with _global_alerts_lock:
        if _global_alerts_refreshing:
            return
        _global_alerts_refreshing = True

    if not try_acquire_lock("global_alerts_refresh", max_age_minutes=30):
        with _global_alerts_lock:
            _global_alerts_refreshing = False
        return

    def _run():
        global _global_alerts_refreshing
        try:
            refresh_global_alerts_cache()
        finally:
            release_lock("global_alerts_refresh")
            with _global_alerts_lock:
                _global_alerts_refreshing = False

    threading.Thread(target=_run, daemon=True).start()


def get_global_alerts_status():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT event_id, title, country, alert_level, latitude, longitude, event_url, published_at, updated_at "
        "FROM global_alerts_cache ORDER BY "
        "CASE alert_level WHEN 'Red' THEN 0 WHEN 'Orange' THEN 1 ELSE 2 END, updated_at DESC"
    ).fetchall()
    conn.close()

    entries = []
    for row in rows:
        d = dict(row)
        risk = _gdacs_alert_to_risk(d["alert_level"])
        d["risk_level"] = risk["level"]
        d["risk_color"] = risk["color"]
        entries.append(d)

    last_updated = max((e["updated_at"] for e in entries), default=None)
    is_stale = False
    age_minutes = None
    if last_updated:
        age_minutes = (datetime.utcnow() - datetime.fromisoformat(last_updated)).total_seconds() / 60
        is_stale = age_minutes >= (GLOBAL_ALERTS_REFRESH_MINUTES * 3)

    return {
        "entries": entries,
        "initialized": last_updated is not None,
        "last_updated": last_updated,
        "is_stale": is_stale,
        "age_minutes": round(age_minutes) if age_minutes is not None else None,
    }


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
    this to one external call instead of two. Coastline/sea features are
    tracked separately from rivers/lakes so we can tell whether a location
    is genuinely a coastal region, anywhere in the world."""
    query = f"""
    [out:json][timeout:12];
    (
      way["natural"="coastline"](around:{radius_m},{lat},{lon});
      way["natural"="water"](around:{radius_m},{lat},{lon});
      way["waterway"~"river|stream|canal"](around:{radius_m},{lat},{lon});
      node["place"="sea"](around:{radius_m},{lat},{lon});
    );
    out center tags 40;
    (
      nwr["building"](around:600,{lat},{lon});
    );
    out count;
    """
    try:
        response = requests.post(
            OVERPASS_URL,
            data={"data": query},
            timeout=14,
            headers={"User-Agent": "FloodGuardAI/1.0 (flood risk web app; contact via app owner)"},
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"Overpass request failed: {error}")
        return None, None, None, None, None

    elements = data.get("elements", [])
    water_candidates = []  # (point, tags)
    coast_points = []
    building_count = 0

    for el in elements:
        if el.get("type") == "count":
            building_count = int(el.get("tags", {}).get("buildings", 0) or 0)
            continue

        center = el.get("center")
        if center:
            point = (center["lat"], center["lon"])
        elif el.get("type") == "node":
            point = (el.get("lat"), el.get("lon"))
        else:
            continue

        tags = el.get("tags", {})
        water_candidates.append((point, tags))
        if tags.get("natural") == "coastline" or tags.get("place") == "sea":
            coast_points.append(point)

    nearest_water_m = None
    nearest_water_point = None
    nearest_water_label = None
    if water_candidates:
        nearest_point, nearest_tags = min(
            water_candidates, key=lambda c: haversine_meters(lat, lon, c[0][0], c[0][1])
        )
        nearest_water_point = nearest_point
        nearest_water_m = haversine_meters(lat, lon, nearest_point[0], nearest_point[1])
        nearest_water_label = _describe_water_feature(nearest_tags)

    nearest_coast_m = None
    if coast_points:
        nearest_coast_m = min(haversine_meters(lat, lon, cp[0], cp[1]) for cp in coast_points)

    return nearest_water_m, nearest_coast_m, building_count, nearest_water_point, nearest_water_label


def _describe_water_feature(tags):
    """Turn OSM tags into a human-readable label, e.g. 'Five Cowrie Creek
    (river)' or 'Unnamed coastline' — richer than a bare distance number."""
    tags = tags or {}
    name = tags.get("name")

    if tags.get("natural") == "coastline" or tags.get("place") == "sea":
        kind = "coastline"
    elif tags.get("waterway") in ("river", "stream", "canal"):
        kind = tags.get("waterway")
    elif tags.get("natural") == "water":
        kind = tags.get("water") or "lake/reservoir"
    else:
        kind = "waterway"

    return f"{name} ({kind})" if name else f"Unnamed {kind}"


# A location within this distance of an ocean/sea coastline is treated as a
# coastal region and gets a lower alert threshold, since storm surge, tidal
# backflow, and lagoon/estuary effects mean coastal areas flood at rainfall
# levels that wouldn't trouble inland terrain.
COASTAL_ZONE_KM = 10


def is_coastal_region(nearest_coast_m):
    return nearest_coast_m is not None and nearest_coast_m <= COASTAL_ZONE_KM * 1000


def classify_water_proximity(distance_m, feature_label=None):
    if distance_m is None:
        return {
            "score_bonus": 0,
            "label": "No major water body detected nearby",
            "status": "No coastline, river, or lake found within 3 km in OpenStreetMap data.",
        }
    named = f" — {feature_label}" if feature_label else ""
    if distance_m <= 500:
        return {
            "score_bonus": 18,
            "label": f"~{distance_m:.0f} m from open water{named}",
            "status": "Very close to a river, lake, or coastline — high overflow/surge exposure.",
        }
    if distance_m <= 2000:
        return {
            "score_bonus": 11,
            "label": f"~{distance_m/1000:.1f} km from open water{named}",
            "status": "Close enough to a river, lake, or coastline for overflow to matter.",
        }
    if distance_m <= 5000:
        return {
            "score_bonus": 4,
            "label": f"~{distance_m/1000:.1f} km from open water{named}",
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
    absorbs water more slowly, worsening waterlogging and runoff. SoilGrids
    is a known-slow public API, so this uses a longer timeout than most
    other lookups here."""
    try:
        response = requests.get(
            SOILGRIDS_URL,
            params={"lon": lon, "lat": lat, "property": "clay", "depth": "0-5cm", "value": "mean"},
            timeout=20,
            headers={"User-Agent": "FloodGuardAI/1.0 (flood risk web app; contact via app owner)"},
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


def fetch_river_discharge(lat, lon):
    """Real hydrological model output — GloFAS (Global Flood Awareness
    System), the Copernicus/ECMWF model professional flood forecasters use,
    exposed free via Open-Meteo's Flood API. Returns today's forecasted
    discharge (m3/s) for the nearest modeled river cell, plus its 30-year
    historical average for the same day of year, so we can tell a river
    running at several times its normal flow from one at a normal level —
    genuine upstream catchment-routing hydrology, not a rainfall proxy.
    Not every coordinate sits on a modeled river reach, so a clean 'no data'
    (None, None) is an expected, normal outcome, not a failure."""
    try:
        response = requests.get(
            FLOOD_API_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "river_discharge,river_discharge_mean",
                "forecast_days": 3,
            },
            timeout=12,
        )
        response.raise_for_status()
        data = response.json()
        daily = data.get("daily", {}) or {}
        discharge_values = daily.get("river_discharge") or []
        mean_values = daily.get("river_discharge_mean") or []
        if not discharge_values or not mean_values:
            return None, None
        current = discharge_values[0]
        mean = mean_values[0]
        if current is None or mean is None:
            return None, None
        return float(current), float(mean)
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
        print(f"GloFAS river discharge request failed: {error}")
        return None, None


def classify_river_discharge(current, mean):
    if current is None or mean is None or mean <= 0:
        return {
            "score_bonus": 0,
            "label": "River discharge data unavailable",
            "status": "No GloFAS-modeled river reach at this exact point, or data temporarily unavailable.",
        }

    ratio = current / mean
    detail = f"Modeled discharge is {current:.0f} m3/s vs a typical {mean:.0f} m3/s for this time of year (GloFAS)."

    if ratio >= 3:
        return {"score_bonus": 20, "label": f"River discharge {ratio:.1f}x normal — extreme swelling", "status": detail}
    if ratio >= 2:
        return {"score_bonus": 14, "label": f"River discharge {ratio:.1f}x normal — very high", "status": detail}
    if ratio >= 1.4:
        return {"score_bonus": 8, "label": f"River discharge {ratio:.1f}x normal — elevated", "status": detail}
    if ratio >= 1.1:
        return {"score_bonus": 3, "label": f"River discharge slightly above normal ({ratio:.1f}x)", "status": detail}
    return {"score_bonus": 0, "label": "River discharge near normal", "status": detail}


def fetch_soil_moisture(lat, lon):
    """Real-time soil saturation (ERA5-based, via Open-Meteo), distinct from
    SoilGrids' static clay-content soil TYPE fetched above — this is current
    soil STATE. Already-saturated ground can't absorb more rain regardless
    of its clay content, which a static soil-type lookup alone can't tell
    you."""
    try:
        response = requests.get(
            SOIL_MOISTURE_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "soil_moisture_0_to_1cm",
                "forecast_days": 1,
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        values = data.get("hourly", {}).get("soil_moisture_0_to_1cm") or []
        for value in values:
            if value is not None:
                return float(value)
        return None
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        print(f"Soil moisture request failed: {error}")
        return None


def classify_soil_moisture(value):
    if value is None:
        return {
            "score_bonus": 0,
            "label": "Soil moisture data unavailable",
            "status": "Real-time soil saturation lookup failed or unavailable for this location.",
        }
    if value >= 0.4:
        return {
            "score_bonus": 10,
            "label": f"Soil near saturation (~{value:.2f} m3/m3)",
            "status": "Ground is already close to saturated — little capacity left to absorb more rain.",
        }
    if value >= 0.3:
        return {
            "score_bonus": 5,
            "label": f"Soil moderately wet (~{value:.2f} m3/m3)",
            "status": "Ground is holding significant moisture already.",
        }
    return {
        "score_bonus": 0,
        "label": f"Soil moisture normal (~{value:.2f} m3/m3)",
        "status": "Ground currently has meaningful capacity to absorb rainfall.",
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


def classify_risk(score, coastal=False):
    # Coastal regions get every threshold shifted down by 20 points, so HIGH
    # starts at 25 instead of 45 — storm surge, tidal backflow, and lagoon/
    # estuary effects mean coastal areas flood at rainfall levels that
    # wouldn't trouble inland terrain, so the same numeric score should read
    # as more urgent near a coastline.
    if coastal:
        critical_at, severe_at, high_at, watch_at = 65, 45, 25, 5
    else:
        critical_at, severe_at, high_at, watch_at = 85, 65, 45, 25

    if score >= critical_at:
        return {
            "level": "CRITICAL",
            "color": "critical",
            "map_color": "#7f1d1d",
            "priority_action": "Evacuate people to higher ground now. Lives first — move property only if it's safe to do so.",
            "advice": "Severe flood conditions are likely or already happening. Move people to elevated ground immediately, avoid low bridges and flooded roads entirely, and relocate vehicles and valuables only if you can do so safely.",
        }
    if score >= severe_at:
        return {
            "level": "SEVERE",
            "color": "severe",
            "map_color": "#b91c1c",
            "priority_action": "Move property, vehicles, and valuables to elevated ground now.",
            "advice": "Serious flood risk given local terrain and conditions. Relocate property, vehicles, and valuables to elevated ground now, and avoid low-lying roads and waterside routes.",
        }
    if score >= high_at:
        return {
            "level": "HIGH",
            "color": "high",
            "map_color": "#dc2626",
            "priority_action": "Start moving furniture, electronics, and valuables to higher ground.",
            "advice": "High flood risk. Start moving furniture, electronics, and valuables to a higher floor or elevated ground now. Stay away from drainage channels and monitor official emergency updates.",
        }
    if score >= watch_at:
        return {
            "level": "WATCH",
            "color": "watch",
            "map_color": "#f59e0b",
            "priority_action": "Move loose valuables and documents off the floor as a precaution.",
            "advice": "Elevated flood watch. Move loose valuables and important documents off the floor as a precaution, and avoid unnecessary travel through low-lying areas.",
        }
    return {
        "level": "LOW",
        "color": "low",
        "map_color": "#16a34a",
        "priority_action": None,
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
    river = context.get("river_discharge")
    moisture = context.get("soil_moisture")
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
            else "No construction or drainage issues reported yet for this location — be the first to flag one.",
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

    if river:
        layers["river_discharge"] = {
            "label": river["label"],
            "score": max(0, min(10, river.get("score_bonus", 0))),
            "status": river["status"],
        }

    if moisture:
        layers["soil_moisture"] = {
            "label": moisture["label"],
            "score": max(0, min(10, moisture.get("score_bonus", 0))),
            "status": moisture["status"],
        }

    layers["summary"] = f"{city} is being evaluated with weather, terrain, water proximity, soil, hydrological, and urbanization signals, plus live visitor contributions.{community_note}"

    return layers


def _weather_bonus(rainfall, humidity, pressure, wind_speed, rainfall_word="current"):
    """Rainfall/humidity/pressure/wind contribution, shared between the
    current-conditions score and each forecast day's score."""
    score = 0
    factors = []

    if rainfall >= 50:
        score += 32
        factors.append(f"Extreme {rainfall_word} rainfall")
    elif rainfall >= 20:
        score += 22
        factors.append(f"Heavy {rainfall_word} rainfall")
    elif rainfall >= 5:
        score += 10
        factors.append(f"Active {rainfall_word} rainfall")

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

    return score, factors


def _context_bonus(context):
    """Terrain/slope/water/soil/urbanization/tide/historical contribution —
    independent of any single reading, shared between the current-conditions
    score and each forecast day's score. This is what lets a forecast day
    warn ahead of time for a low-lying coastal spot even when the rainfall
    number alone looks unremarkable."""
    context = context or {}
    terrain = context.get("terrain") or {"score_bonus": 0, "label": "Elevation data unavailable"}
    slope = context.get("slope") or {"score_bonus": 0, "label": "Slope data unavailable"}
    water = context.get("water") or {"score_bonus": 0, "label": "Water proximity unavailable"}
    soil = context.get("soil") or {"score_bonus": 0, "label": "Soil data unavailable"}
    urban = context.get("urban") or {"score_bonus": 0, "label": "Building density unavailable"}
    tide = context.get("tide")
    river = context.get("river_discharge")
    moisture = context.get("soil_moisture")
    historical_reports = context.get("historical_reports", 0)

    score = 0
    factors = []

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

    if river:
        bonus = river.get("score_bonus", 0)
        if bonus:
            score += bonus
            factors.append(f"Hydrology (GloFAS): {river['label']}")

    if moisture:
        bonus = moisture.get("score_bonus", 0)
        if bonus:
            score += bonus
            factors.append(f"Soil moisture: {moisture['label']}")

    if historical_reports >= 6:
        score += 10
        factors.append(f"Community-reported flooding history: {historical_reports} past reports at this location")
    elif historical_reports >= 3:
        score += 6
        factors.append(f"Community-reported flooding history: {historical_reports} past reports at this location")
    elif historical_reports >= 1:
        score += 3
        factors.append(f"Community-reported flooding history: {historical_reports} past report(s) at this location")

    return score, factors


def calculate_day_score(rainfall, humidity, pressure, wind_speed, context):
    """Same terrain/coastal-aware model as 'right now', applied to a single
    forecast day's weather — so the 5-day forecast can warn ahead of time
    for vulnerable terrain, not just flag it once flooding is already
    happening."""
    weather_bonus, _ = _weather_bonus(rainfall, humidity, pressure, wind_speed, rainfall_word="forecast")
    context_bonus, _ = _context_bonus(context)
    score = max(0, min(weather_bonus + context_bonus, 100))
    coastal = bool((context or {}).get("coastal"))
    risk = classify_risk(score, coastal=coastal)
    return score, risk


def calculate_flood_score(weather, forecast, context=None):
    """Combines rainfall/forecast/humidity/pressure/wind with terrain,
    slope, water proximity, soil, urbanization, tide, and community-observed
    frequency — rather than rainfall alone, matching how systems like
    Copernicus EMS or GDACS combine multiple layers instead of one signal."""
    context = context or {}
    factors = []

    rainfall = weather["rainfall"]
    humidity = weather["humidity"]
    pressure = weather["pressure"]
    wind_speed = weather["wind"]
    forecast_rain_total = sum(day["rain"] for day in forecast)
    max_forecast_rain = max([day["rain"] for day in forecast], default=0)

    weather_score, weather_factors = _weather_bonus(rainfall, humidity, pressure, wind_speed, rainfall_word="current")
    score = weather_score
    factors.extend(weather_factors)

    if forecast_rain_total >= 80:
        score += 20
        factors.append("Very wet 5-day forecast")
    elif forecast_rain_total >= 35:
        score += 12
        factors.append("Sustained rainfall expected")
    elif max_forecast_rain >= 10:
        score += 6
        factors.append("One or more rainy forecast periods")

    context_score, context_factors = _context_bonus(context)
    score += context_score
    factors.extend(context_factors)

    score = max(0, min(score, 100))
    coastal = bool(context.get("coastal"))
    risk = classify_risk(score, coastal=coastal)

    if coastal:
        factors.append("Coastal region — lower alert threshold applied (HIGH starts at 25 instead of 45)")

    return {
        "score": score,
        "confidence": min(95, 68 + score // 3),
        "risk": risk["level"],
        "risk_color": risk["color"],
        "map_color": risk["map_color"],
        "advice": risk["advice"],
        "priority_action": risk["priority_action"],
        "coastal": coastal,
        "factors": factors or ["No major flood trigger detected from current conditions"],
    }


def get_forecast(lat, lon, context=None):
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
        humidity = item["main"]["humidity"]
        pressure = item["main"].get("pressure", 1013)
        wind = item["wind"]["speed"]
        weather_id = item["weather"][0]["id"]
        description = item["weather"][0]["description"].title()
        scene = weather_scene(weather_id, description)

        # Same terrain/slope/water/soil/urbanization/coastal model as "right
        # now" — a low-lying coastal spot should show elevated risk here even
        # on a day with only moderate forecast rainfall, not just once
        # flooding is already underway.
        day_score, day_risk = calculate_day_score(rainfall, humidity, pressure, wind, context)

        forecast.append(
            {
                "day": forecast_time.strftime("%A"),
                "date": forecast_time.strftime("%d %b"),
                "time": forecast_time.strftime("%I:%M %p"),
                "temp": round(item["main"]["temp"], 1),
                "rain": rainfall,
                "weather": description,
                "humidity": humidity,
                "wind": wind,
                "score": day_score,
                "risk": day_risk["level"],
                "risk_color": day_risk["color"],
                "priority_action": day_risk["priority_action"],
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


def get_geo_context(city_key, lat, lon):
    """Elevation, slope, water proximity, soil type, and urbanization for a
    location, cached for GEO_CONTEXT_TTL_HOURS. This is the fix for
    Overpass/SoilGrids rate-limiting: a 29-location watchlist refreshing
    every 15 minutes was re-fetching static terrain data ~100 times/hour
    that hadn't changed since the last sweep. Now each location only hits
    those APIs once per day; weather, tide, and river discharge (which
    genuinely change) are still fetched fresh every time by the caller."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM geo_context_cache WHERE city_key = ?", (city_key,)).fetchone()

    if row:
        age_hours = (datetime.utcnow() - datetime.fromisoformat(row["updated_at"])).total_seconds() / 3600
        if age_hours < GEO_CONTEXT_TTL_HOURS:
            conn.close()
            water_point = (
                (row["nearest_water_lat"], row["nearest_water_lon"])
                if row["nearest_water_lat"] is not None
                else None
            )
            return {
                "elevation": row["elevation"],
                "slope_percent": row["slope_percent"],
                "nearest_water_m": row["nearest_water_m"],
                "nearest_coast_m": row["nearest_coast_m"],
                "nearest_water_point": water_point,
                "nearest_water_label": row["nearest_water_label"],
                "building_count": row["building_count"] or 0,
                "clay_percent": row["clay_percent"],
            }

    # Cache miss or stale — fetch live from Overpass/Open-Meteo/SoilGrids.
    elevation, slope_percent = fetch_elevation_grid(lat, lon)
    nearest_water_m, nearest_coast_m, building_count, nearest_water_point, nearest_water_label = (
        fetch_water_and_urban_context(lat, lon)
    )
    clay_percent = fetch_soil_clay(lat, lon)

    now = datetime.utcnow().isoformat()
    conn.execute(
        """
        INSERT INTO geo_context_cache
            (city_key, elevation, slope_percent, nearest_water_m, nearest_coast_m,
             nearest_water_lat, nearest_water_lon, nearest_water_label, building_count, clay_percent, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(city_key) DO UPDATE SET
            elevation=excluded.elevation,
            slope_percent=excluded.slope_percent,
            nearest_water_m=excluded.nearest_water_m,
            nearest_coast_m=excluded.nearest_coast_m,
            nearest_water_lat=excluded.nearest_water_lat,
            nearest_water_lon=excluded.nearest_water_lon,
            nearest_water_label=excluded.nearest_water_label,
            building_count=excluded.building_count,
            clay_percent=excluded.clay_percent,
            updated_at=excluded.updated_at
        """,
        (
            city_key,
            elevation,
            slope_percent,
            nearest_water_m,
            nearest_coast_m,
            nearest_water_point[0] if nearest_water_point else None,
            nearest_water_point[1] if nearest_water_point else None,
            nearest_water_label,
            building_count,
            clay_percent,
            now,
        ),
    )
    conn.commit()
    conn.close()

    return {
        "elevation": elevation,
        "slope_percent": slope_percent,
        "nearest_water_m": nearest_water_m,
        "nearest_coast_m": nearest_coast_m,
        "nearest_water_point": nearest_water_point,
        "nearest_water_label": nearest_water_label,
        "building_count": building_count,
        "clay_percent": clay_percent,
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

    # Elevation, slope, water proximity, soil, and urbanization are cached
    # for a day (GEO_CONTEXT_TTL_HOURS) since they don't meaningfully change
    # hour to hour — this is what keeps Overpass/SoilGrids call volume low
    # enough to avoid rate-limiting on repeated watchlist sweeps.
    geo = get_geo_context(normalize_city(weather["city"]), lat, lon)
    elevation = geo["elevation"]
    slope_percent = geo["slope_percent"]
    terrain = classify_terrain(elevation)
    slope = classify_slope(slope_percent)

    nearest_water_m = geo["nearest_water_m"]
    nearest_coast_m = geo["nearest_coast_m"]
    building_count = geo["building_count"]
    nearest_water_point = geo["nearest_water_point"]
    nearest_water_label = geo["nearest_water_label"]
    water = classify_water_proximity(nearest_water_m, nearest_water_label)
    urban = classify_urbanization(building_count)
    coastal = is_coastal_region(nearest_coast_m)

    clay_percent = geo["clay_percent"]
    soil = classify_soil(clay_percent)

    # Weather, tide, and river discharge genuinely change over time, so
    # these are still fetched fresh on every call.
    tide_height = fetch_tide_status(lat, lon)
    tide = classify_tide(tide_height)

    discharge_current, discharge_mean = fetch_river_discharge(lat, lon)
    river = classify_river_discharge(discharge_current, discharge_mean)

    moisture_value = fetch_soil_moisture(lat, lon)
    moisture = classify_soil_moisture(moisture_value)

    community = get_city_stats(weather["city"])
    historical_reports = get_historical_frequency(weather["city"])

    context = {
        "terrain": terrain,
        "slope": slope,
        "water": water,
        "soil": soil,
        "urban": urban,
        "tide": tide,
        "river_discharge": river,
        "soil_moisture": moisture,
        "historical_reports": historical_reports,
        "coastal": coastal,
    }

    # The forecast is fetched after context so each day can be scored with
    # the same terrain/coastal-aware model as "right now" — this is what
    # lets the 5-day forecast warn ahead of time for vulnerable terrain,
    # instead of only reacting to rainfall alone.
    forecast = get_forecast(lat, lon, context)

    flood_model = calculate_flood_score(weather, forecast, context)
    environment = estimate_environment(weather["city"], weather, community, context)

    # Ground-truth override: if visitors are actively reporting flooding right
    # now, that outranks a model that hasn't caught up yet. This is the exact
    # failure mode where the app said "safe" while a place was flooding.
    recent_flood_reports = get_recent_flooding_reports(weather["city"])
    ground_alert = None
    if recent_flood_reports:
        high_threshold = 25 if coastal else 45
        if flood_model["score"] < high_threshold:
            flood_model["score"] = high_threshold
            risk = classify_risk(flood_model["score"], coastal=coastal)
            flood_model["risk"] = risk["level"]
            flood_model["risk_color"] = risk["color"]
            flood_model["map_color"] = risk["map_color"]
            flood_model["advice"] = risk["advice"]
            flood_model["priority_action"] = risk["priority_action"]
            flood_model["factors"].insert(0, "Live visitor reports of active flooding (overrides weather-only estimate)")
        ground_alert = {
            "count": len(recent_flood_reports),
            "message": (
                f"{len(recent_flood_reports)} visitor(s) reported active flooding in "
                f"{weather['city']} within the last {GROUND_TRUTH_WINDOW_HOURS} hours. "
                "Move people to higher ground now; relocate property only if it's safe to do so."
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
        "nearest_coast_m": round(nearest_coast_m) if nearest_coast_m is not None else None,
        "nearest_water_lat": nearest_water_point[0] if nearest_water_point else None,
        "nearest_water_lon": nearest_water_point[1] if nearest_water_point else None,
        "nearest_water_label": nearest_water_label,
    }, forecast


@app.route("/", methods=["GET", "POST"])
def home():
    prediction = None
    forecast = []
    error = None
    reports = []

    # GDACS needs no API key, so real global flood coverage works even
    # before OpenWeather is configured.
    maybe_refresh_global_alerts_async()

    if API_KEY:
        maybe_refresh_watchlist_async()

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
                log_search(prediction["city"], prediction["risk"], prediction["score"])
                cache_watchlist_entry_now(prediction)

    return render_template(
        "index.html",
        prediction=prediction,
        forecast=forecast,
        error=error,
        reports=reports,
        category_labels=CATEGORY_LABELS,
        total_contributions=total_contributions_count(),
        site_stats=get_site_stats(),
        watchlist=get_watchlist_status(),
        watchlist_refresh_minutes=WATCHLIST_REFRESH_MINUTES,
        global_alerts=get_global_alerts_status(),
        mapbox_token=MAPBOX_ACCESS_TOKEN,
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


@app.route("/api/watchlist-status")
def api_watchlist_status():
    return jsonify({"ok": True, **get_watchlist_status()})


@app.route("/api/refresh-watchlist", methods=["GET", "POST"])
def api_refresh_watchlist():
    """Trigger a synchronous refresh of all monitored locations. Intended to
    be called by an external scheduler (e.g. a free GitHub Actions cron job
    or cron-job.org) every 10-15 minutes so the homepage alert banner stays
    current even with zero visitor traffic in between."""
    if not API_KEY:
        return jsonify({"ok": False, "error": "OPENWEATHER_API_KEY is not configured."}), 400

    if not try_acquire_lock("watchlist_refresh", max_age_minutes=60):
        return jsonify({"ok": True, "note": "A refresh is already in progress; returning current cache.", **get_watchlist_status()})

    try:
        refresh_watchlist_cache()
    finally:
        release_lock("watchlist_refresh")

    return jsonify({"ok": True, **get_watchlist_status()})


@app.route("/api/global-alerts")
def api_global_alerts():
    return jsonify({"ok": True, **get_global_alerts_status()})


@app.route("/api/refresh-global-alerts", methods=["GET", "POST"])
def api_refresh_global_alerts():
    """Trigger a synchronous GDACS refresh. Needs no API key — intended for
    an external scheduler to hit every 10 minutes for true always-fresh
    worldwide coverage."""
    if not try_acquire_lock("global_alerts_refresh", max_age_minutes=30):
        return jsonify({"ok": True, "note": "A refresh is already in progress; returning current cache.", **get_global_alerts_status()})

    try:
        refresh_global_alerts_cache()
    finally:
        release_lock("global_alerts_refresh")

    return jsonify({"ok": True, **get_global_alerts_status()})


@app.route("/api/stats")
def api_stats():
    stats = get_site_stats()
    stats["total_contributions"] = total_contributions_count()
    return jsonify({"ok": True, **stats})


@app.route("/health")
def health():
    return {
        "status": "ok",
        "service": "FloodGuard AI",
        "config": {
            "openweather_configured": bool(API_KEY),
            "tide_configured": bool(TIDE_API_KEY),
            "mapbox_configured": bool(MAPBOX_ACCESS_TOKEN),
        },
    }


if __name__ == "__main__":
    app.run(debug=True)
