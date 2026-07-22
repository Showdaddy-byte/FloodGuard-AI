import json
import math
import os
import socket
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests
import urllib3.util.connection as urllib3_cn
from flask import Flask, g, jsonify, render_template, request
from dotenv import load_dotenv
try:
    import ee
except ImportError:
    ee = None

load_dotenv()
app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "community.db")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GEE_KEY_PATH = os.path.join(BASE_DIR, "credentials", "floodguard-ai-502609-81e725f17c81.json")

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

EARTH_ENGINE_ENABLED = os.getenv("EARTH_ENGINE_ENABLED", "1").lower() not in ("0", "false", "no")
GEE_SERVICE_ACCOUNT = os.getenv("GEE_SERVICE_ACCOUNT")
GEE_PRIVATE_KEY_PATH = os.getenv("GEE_PRIVATE_KEY_PATH", DEFAULT_GEE_KEY_PATH)
GEE_PROJECT = os.getenv("GEE_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")

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
# Free public OSRM demo routing server — no key required. OSRM's own docs
# note this demo instance isn't guaranteed for production/heavy use, which
# is worth knowing if this feature gets popular; a self-hosted OSRM
# instance would be the natural next step at that point.
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
ROUTE_SAMPLE_POINTS = 7

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
EARTH_ENGINE_CONTEXT_TTL_HOURS = 6

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


# ---------------------------------------------------------------------------
# Network resilience layer
#
# This is the fix for two distinct classes of error seen in production:
#
# 1. "Network is unreachable" (errno 101) on Overpass and similar hosts.
#    This is NOT a timeout or a rate limit — it's a routing failure. Many
#    hosts (overpass-api.de included) publish both an IPv4 (A) and IPv6
#    (AAAA) DNS record. If this environment's outbound network doesn't
#    actually have a working IPv6 route, Python/urllib3 will still try the
#    IPv6 address first (because DNS returned it) and fail immediately with
#    ENETUNREACH, never falling back to the IPv4 address that *would* have
#    worked. Forcing IPv4-only DNS resolution process-wide is the standard,
#    known fix for this exact symptom.
#
# 2. Transient failures (503s, connection resets, brief outages) on
#    SoilGrids and other public APIs. These services occasionally have real
#    outages that have nothing to do with this app's code. A short,
#    bounded retry with backoff rides out a brief blip; a per-service
#    circuit breaker stops repeatedly attempting (and waiting out the
#    timeout on) a service that just told us it's down, so one outage
#    doesn't slow down every subsequent request until it recovers.
# ---------------------------------------------------------------------------

_original_allowed_gai_family = urllib3_cn.allowed_gai_family


def _force_ipv4_gai_family():
    return socket.AF_INET


urllib3_cn.allowed_gai_family = _force_ipv4_gai_family


_service_cooldowns = {}
SERVICE_COOLDOWN_SECONDS = 120  # how long to skip a service after it fails


def _service_available(service_name):
    until = _service_cooldowns.get(service_name)
    return not (until and time.time() < until)


def _mark_service_down(service_name, cooldown_seconds=SERVICE_COOLDOWN_SECONDS):
    _service_cooldowns[service_name] = time.time() + cooldown_seconds


def _mark_service_up(service_name):
    _service_cooldowns.pop(service_name, None)


def request_with_retry(
    method,
    url,
    *,
    service_name=None,
    max_retries=2,
    backoff_base=0.6,
    retry_statuses=(429, 500, 502, 503, 504),
    **kwargs,
):
    """Shared retry wrapper used by every external API call in this app.
    Retries a small, bounded number of times with short exponential
    backoff on transient failures — enough to ride out a brief blip
    without making a user-facing request noticeably slower. If
    `service_name` is given, this also checks/updates that service's
    circuit-breaker cooldown, so a known-down service is skipped
    immediately (raising ConnectionError) instead of making the caller
    wait through a doomed timeout on every single request."""
    if service_name and not _service_available(service_name):
        raise requests.exceptions.ConnectionError(
            f"{service_name} is in cooldown after a recent failure; skipping request."
        )

    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code in retry_statuses and attempt < max_retries:
                time.sleep(backoff_base * (2 ** attempt))
                continue
            if response.status_code in retry_statuses:
                if service_name:
                    _mark_service_down(service_name)
            elif service_name:
                _mark_service_up(service_name)
            return response
        except requests.RequestException as error:
            last_exc = error
            if attempt < max_retries:
                time.sleep(backoff_base * (2 ** attempt))
                continue
            if service_name:
                _mark_service_down(service_name)
            raise
    if last_exc:
        raise last_exc


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
    try:
        conn.execute("ALTER TABLE contributions ADD COLUMN water_depth_cm INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE contributions ADD COLUMN roads_affected TEXT")
    except sqlite3.OperationalError:
        pass
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
    try:
        conn.execute("ALTER TABLE geo_context_cache ADD COLUMN emergency_contacts_json TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS earth_engine_cache (
            city_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
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


def save_contribution(city, category, rating, comment, water_depth_cm=None, roads_affected=None):
    db = get_db()
    db.execute(
        "INSERT INTO contributions (city_key, city_label, category, rating, comment, water_depth_cm, roads_affected, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            normalize_city(city),
            city.strip(),
            category,
            rating,
            comment.strip(),
            water_depth_cm,
            (roads_affected or "").strip() or None,
            datetime.utcnow().isoformat(),
        ),
    )
    db.commit()


def get_city_contributions(city, limit=12):
    db = get_db()
    rows = db.execute(
        "SELECT city_label, category, rating, comment, water_depth_cm, roads_affected, created_at FROM contributions "
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


EMERGENCY_LABELS = {
    "hospital": "Nearest Hospital",
    "police": "Nearest Police Station",
    "fire_station": "Nearest Fire Station",
}


def fetch_emergency_contacts(lat, lon, radius_m=8000):
    """Real, live nearest hospital/police/fire station from OpenStreetMap —
    not a fabricated directory. Phone numbers only appear when OSM actually
    has one tagged; this app never invents contact details."""
    query = f"""
    [out:json][timeout:12];
    (
      nwr["amenity"="hospital"](around:{radius_m},{lat},{lon});
      nwr["amenity"="police"](around:{radius_m},{lat},{lon});
      nwr["amenity"="fire_station"](around:{radius_m},{lat},{lon});
    );
    out center tags 60;
    """
    try:
        response = request_with_retry(
            "POST",
            OVERPASS_URL,
            service_name="overpass",
            data={"data": query},
            timeout=14,
            headers={"User-Agent": "FloodGuardAI/1.0 (flood risk web app; contact via app owner)"},
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"Emergency contacts request failed: {error}")
        return []

    nearest_by_type = {}
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        amenity = tags.get("amenity")
        if amenity not in EMERGENCY_LABELS:
            continue

        center = el.get("center")
        point = (center["lat"], center["lon"]) if center else (el.get("lat"), el.get("lon"))
        if point[0] is None or point[1] is None:
            continue

        distance = haversine_meters(lat, lon, point[0], point[1])
        if amenity not in nearest_by_type or distance < nearest_by_type[amenity]["distance_m"]:
            nearest_by_type[amenity] = {
                "type": amenity,
                "label": EMERGENCY_LABELS[amenity],
                "name": tags.get("name") or EMERGENCY_LABELS[amenity],
                "phone": tags.get("phone") or tags.get("contact:phone"),
                "distance_m": round(distance),
            }

    return sorted(nearest_by_type.values(), key=lambda c: c["distance_m"])


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
        response = request_with_retry(
            "POST",
            OVERPASS_URL,
            service_name="overpass",
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
    print("Nearest water:", nearest_water_m, nearest_water_label)
    print("Nearest coast:", nearest_coast_m)
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
        response = request_with_retry(
            "GET",
            SOILGRIDS_URL,
            service_name="soilgrids",
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
    """Optional — only runs if TIDE_API_KEY (WorldTides) is configured.
    Returns current height plus the next high and low tide events, not just
    a single number — this is what lets the app actually say something like
    'high tide expected at 3:42 PM' instead of just 'tide: high'."""
    if not TIDE_API_KEY:
        return None

    try:
        response = request_with_retry(
            "GET",
            WORLDTIDES_URL,
            service_name="worldtides",
            params={
                "heights": "",
                "extremes": "",
                "lat": lat,
                "lon": lon,
                "key": TIDE_API_KEY,
                "duration": 1440,  # next 24h of extremes
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"WorldTides request failed: {error}")
        return None

    heights = data.get("heights", [])
    extremes = data.get("extremes", [])
    current_height = heights[0].get("height") if heights else None

    now_ts = datetime.utcnow().timestamp()
    next_high = None
    next_low = None
    for event in extremes:
        event_ts = event.get("dt")
        event_type = (event.get("type") or "").lower()
        if event_ts is None or event_ts < now_ts:
            continue
        if event_type == "high" and next_high is None:
            next_high = event
        elif event_type == "low" and next_low is None:
            next_low = event
        if next_high and next_low:
            break

    if current_height is None and not next_high and not next_low:
        return None

    return {"current_height": current_height, "next_high": next_high, "next_low": next_low}


def _format_tide_event(event):
    if not event:
        return None
    try:
        event_time = datetime.utcfromtimestamp(event["dt"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "time": event_time.strftime("%I:%M %p UTC").lstrip("0"),
        "height": event.get("height"),
    }


def classify_tide(tide_data):
    """Builds both the score contribution and the human-readable tide
    picture (current height + next high/low tide) from the full WorldTides
    response. Unlike the old version, this never returns None outright when
    the key just isn't configured — it returns an explicit 'not configured'
    state instead, matching how every other factor in this app behaves, so
    the tide card doesn't just silently disappear from the page."""
    if not TIDE_API_KEY:
        return {
            "score_bonus": 0,
            "label": "Tide monitoring not configured",
            "status": "Add a free WorldTides API key (TIDE_API_KEY) to factor tidal backflow into coastal flood risk.",
            "current_height": None,
            "next_high": None,
            "next_low": None,
        }

    next_high = _format_tide_event((tide_data or {}).get("next_high"))
    next_low = _format_tide_event((tide_data or {}).get("next_low"))
    height_m = (tide_data or {}).get("current_height")

    extras = []
    if next_high:
        extras.append(f"next high {next_high['height']:.1f} m at {next_high['time']}")
    if next_low:
        extras.append(f"next low {next_low['height']:.1f} m at {next_low['time']}")
    extras_text = f" ({'; '.join(extras)})" if extras else ""

    if height_m is None:
        return {
            "score_bonus": 0,
            "label": "Tide data unavailable",
            "status": "WorldTides did not return current tide data for this location — this can happen for inland points with no nearby tide station.",
            "current_height": None,
            "next_high": next_high,
            "next_low": next_low,
        }

    if height_m >= 0.6:
        return {
            "score_bonus": 8,
            "label": f"High tide (~{height_m:.1f} m){extras_text}",
            "status": "High tide reduces drainage capacity for coastal outfalls.",
            "current_height": height_m,
            "next_high": next_high,
            "next_low": next_low,
        }
    if height_m >= 0.2:
        return {
            "score_bonus": 3,
            "label": f"Mid tide (~{height_m:.1f} m){extras_text}",
            "status": "Moderate tidal influence on coastal drainage.",
            "current_height": height_m,
            "next_high": next_high,
            "next_low": next_low,
        }
    return {
        "score_bonus": 0,
        "label": f"Low tide (~{height_m:.1f} m){extras_text}",
        "status": "Low tide — coastal drainage largely unobstructed.",
        "current_height": height_m,
        "next_high": next_high,
        "next_low": next_low,
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


RISK_THRESHOLDS = {
    "coastal": {"watch": 10, "moderate": 22, "high": 38, "severe": 55, "critical": 75},
    "inland": {"watch": 20, "moderate": 35, "high": 55, "severe": 72, "critical": 88},
}


def classify_risk(score, coastal=False):
    # Coastal regions get a lower bar at every tier — storm surge, tidal
    # backflow, and lagoon/estuary effects mean coastal areas flood at
    # rainfall levels that wouldn't trouble inland terrain, so the same
    # numeric score should read as more urgent near a coastline. The shift
    # is smaller than before (was a flat -20 under the old unbounded
    # scoring model) because the new weighted-to-100 model makes very high
    # scores harder to reach in the first place — an equally large shift
    # here would make almost everything coastal read as CRITICAL.
    t = RISK_THRESHOLDS["coastal"] if coastal else RISK_THRESHOLDS["inland"]

    if score >= t["critical"]:
        return {
            "level": "CRITICAL",
            "color": "critical",
            "map_color": "#7f1d1d",
            "priority_action": "Evacuate people to higher ground now. Lives first — move property only if it's safe to do so.",
            "advice": "Severe flood conditions are likely or already happening. Move people to elevated ground immediately, avoid low bridges and flooded roads entirely, and relocate vehicles and valuables only if you can do so safely.",
        }
    if score >= t["severe"]:
        return {
            "level": "SEVERE",
            "color": "severe",
            "map_color": "#b91c1c",
            "priority_action": "Move property, vehicles, and valuables to elevated ground now.",
            "advice": "Serious flood risk given local terrain and conditions. Relocate property, vehicles, and valuables to elevated ground now, and avoid low-lying roads and waterside routes.",
        }
    if score >= t["high"]:
        return {
            "level": "HIGH",
            "color": "high",
            "map_color": "#dc2626",
            "priority_action": "Start moving furniture, electronics, and valuables to higher ground.",
            "advice": "High flood risk. Start moving furniture, electronics, and valuables to a higher floor or elevated ground now. Stay away from drainage channels and monitor official emergency updates.",
        }
    if score >= t["moderate"]:
        return {
            "level": "MODERATE",
            "color": "moderate",
            "map_color": "#eab308",
            "priority_action": "Keep valuables off the floor and stay aware of changing conditions.",
            "advice": "Moderate flood risk. Conditions warrant attention but aren't yet severe — keep an eye on rainfall updates and avoid parking or lingering near low-lying drainage routes.",
        }
    if score >= t["watch"]:
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


def build_travel_recommendation(risk_level, score, timeline):
    """A clear go/no-go verdict instead of just a number — this is
    deliberately conservative about what it claims: no specific road names
    or exact clock-time promises, since no free data source can verify
    which named roads are flooded right now. What it can honestly do is
    look at the same 3-hour forecast slots already fetched for the next
    ~24-36h and flag a materially lower-risk window if one exists."""
    if risk_level == "CRITICAL":
        verdict, color, headline = "AVOID TRAVEL", "critical", "Flooding is likely severe enough to make travel dangerous."
    elif risk_level == "SEVERE":
        verdict, color, headline = "AVOID TRAVEL", "severe", "Flood conditions are serious enough that travel is not recommended."
    elif risk_level == "HIGH":
        verdict, color, headline = "TRAVEL WITH CAUTION", "high", "Flooding is plausible — expect delays and standing water on low-lying routes."
    elif risk_level == "WATCH":
        verdict, color, headline = "TRAVEL WITH CAUTION", "watch", "Conditions are borderline — keep an eye on rainfall before heading out."
    else:
        verdict, color, headline = "SAFE TO TRAVEL", "low", "No significant flood signal for this location right now."

    better_window = None
    if timeline and risk_level not in ("LOW",):
        candidates = [slot for slot in timeline if slot["score"] <= max(0, score - 15)]
        if candidates:
            better_window = min(candidates, key=lambda s: s["score"])

    return {
        "verdict": verdict,
        "color": color,
        "headline": headline,
        "better_window": better_window,
    }


def _earth_engine_status_text(ee_data):
    """Only claims what actually succeeded for THIS specific location and
    request — Earth Engine 'available' just means it initialized, not that
    every dataset returned real data (Sentinel-1/2 depend on a recent
    satellite pass existing at all; any individual dataset call can fail
    independently). Listing exactly what worked and what didn't is the fix
    for output that previously claimed six datasets were 'active' even
    when several of them had silently failed or had no data for that
    point."""
    working = []
    missing = []

    if ee_data.get("gee_elevation_m") is not None:
        working.append("Copernicus DEM elevation/slope")
    else:
        missing.append("Copernicus DEM")

    if ee_data.get("jrc_water_occurrence_pct") is not None:
        working.append("JRC Global Surface Water")
    else:
        missing.append("JRC Global Surface Water")

    if ee_data.get("chirps_7d_mm") is not None:
        working.append("CHIRPS rainfall")
    else:
        missing.append("CHIRPS rainfall")

    if ee_data.get("sentinel2_ndwi") is not None:
        working.append("Sentinel-2 optical")
    elif ee_data.get("sentinel2_image_count") == 0:
        missing.append("Sentinel-2 (no cloud-free image in the last 45 days)")
    else:
        missing.append("Sentinel-2")

    if ee_data.get("sentinel1_flood_area_ha") is not None:
        working.append("Sentinel-1 radar flood detection")
    elif ee_data.get("sentinel1_before_count") == 0 or ee_data.get("sentinel1_after_count") == 0:
        missing.append("Sentinel-1 (no valid before/after radar pass for this location right now)")
    else:
        missing.append("Sentinel-1")

    if ee_data.get("dynamic_world_label") is not None:
        working.append("Dynamic World land cover")
    elif ee_data.get("dynamic_world_image_count") == 0:
        missing.append("Dynamic World (no recent image)")
    else:
        missing.append("Dynamic World")

    parts = []
    if working:
        parts.append("Returned real data for this location: " + ", ".join(working) + ".")
    if missing:
        parts.append("Not available for this specific location/time: " + ", ".join(missing) + ".")
    if not parts:
        return "Earth Engine initialized, but no datasets returned usable data for this specific location."
    return " ".join(parts)


def estimate_environment(city, weather, community=None, context=None):
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
    earth_engine = context.get("earth_engine")
    historical_reports = context.get("historical_reports", 0)

    # Drainage display now uses the exact same function that feeds the real
    # score (_drainage_severity) instead of a separate, disconnected
    # formula — previously these could show completely different numbers
    # for the same location, which is exactly the kind of mismatch between
    # displayed output and actual scoring this needed to stop doing.
    drainage_sev, _ = _drainage_severity(water, urban, community)
    drainage_score = round(drainage_sev * 10)

    # Base construction/land-use score from weather, boosted by live community reports
    construction_score = 5
    construction_score += min(4, community["construction_reports"])
    construction_score = min(10, construction_score)

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
            "label": "Drainage/runoff proxy",
            "score": drainage_score,
            "status": "Estimated from proximity to open water, built-up density (impervious runoff), and live community reports of blockages — not a direct measurement of actual drain capacity, since no free data source measures that.",
        },
        "construction": {
            "label": "Community-reported construction/drainage issues",
            "score": construction_score,
            "status": (
                f"{community['construction_reports']} live construction/drainage report(s) from visitors. "
                "This feeds into the Drainage/runoff figure above rather than being scored separately."
                if community["construction_reports"]
                else "No construction or drainage issues reported yet for this location — be the first to flag one."
            ),
        },
        "historical": {
            "label": f"Community-reported flooding history: {historical_reports} report(s)"
            if historical_reports
            else "No community flooding history recorded yet",
            "score": _historical_raw(historical_reports),
            "status": "Proxy based on past visitor reports, not a certified historical flood archive.",
        },
    }

    if tide:
        layers["tide"] = {
            "label": tide["label"],
            "score": max(0, min(10, tide.get("score_bonus", 0))),
            "status": tide["status"],
            "current_height": tide.get("current_height"),
            "next_high": tide.get("next_high"),
            "next_low": tide.get("next_low"),
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

    if earth_engine:
        if earth_engine.get("available"):
            layers["earth_engine"] = {
                "label": "Earth Engine satellite reinforcement",
                "score": max(0, min(10, round(earth_engine.get("score_bonus", 0) / 3.5))),
                "status": _earth_engine_status_text(earth_engine)
                + f" This contributes up to {SCORE_WEIGHTS['satellite']} of the 100 total risk points — a small reinforcing signal, not a primary driver.",
                "details": earth_engine,
            }
        else:
            layers["earth_engine"] = {
                "label": "Earth Engine unavailable",
                "score": 0,
                "status": earth_engine.get("error") or "Google Earth Engine is not configured for this deployment.",
                "details": earth_engine,
            }

    satellite_clause = ", satellite/raster," if earth_engine and earth_engine.get("available") else ""
    layers["summary"] = f"{city} is being evaluated with weather, terrain, water proximity, soil, hydrological{satellite_clause} and urbanization signals, plus live visitor contributions.{community_note}"

    return layers


# ---------------------------------------------------------------------------
# Weighted flood scoring model
#
# Each factor contributes at most its fixed WEIGHT (out of 100), scaled by
# a 0-1 "severity" normalized against that factor's own realistic maximum
# raw signal. This replaces an older unbounded additive model where every
# factor could independently add points with no shared ceiling until the
# final clamp to 100 -- which is exactly what caused two real, reported
# problems: (1) risk reading too high too often, because several
# moderately-elevated factors could stack past HIGH/SEVERE even when none
# of them individually looked severe, and (2) humidity being overweighted,
# since a rainy day is almost always a humid day too, so a separate
# large humidity contribution was effectively double-counting rainfall's
# own signal. Humidity now shares one small "synoptic conditions" bucket
# with pressure and wind instead of scoring independently.
#
# Weights sum to exactly 100, so the score is always a genuine share of
# maximum plausible risk, not a sum that happens to get capped afterward.
# This is a calibration choice, not a scientific constant -- it's the
# result of reasoning about relative importance, not fitted against real
# historical flood outcomes (this app doesn't have a labeled dataset to
# calibrate against). Treat these weights as a documented starting point
# to refine further as real-world results come in, not a finished answer.
# ---------------------------------------------------------------------------
SCORE_WEIGHTS = {
    "forecast_rainfall": 25,
    "current_rainfall": 20,
    "terrain": 12,
    "soil": 10,
    "river_discharge": 10,
    "tide": 8,
    "historical": 5,
    "drainage": 5,
    "synoptic": 3,   # humidity + pressure + wind, combined and deliberately small
    "satellite": 2,  # Earth Engine reinforcing signals (Sentinel-1/2, CHIRPS, Dynamic World)
}
assert sum(SCORE_WEIGHTS.values()) == 100, "SCORE_WEIGHTS must sum to exactly 100"


def _severity(raw, max_raw):
    """Normalize a raw score_bonus-style value to a 0-1 severity against
    its own known realistic maximum. Negative raw values (e.g. highland
    terrain, steep slope, sandy soil -- all of which reduce risk) floor at
    0 severity rather than going negative, since this model expresses risk
    as a positive weighted sum, not an add-and-subtract tally."""
    if max_raw <= 0:
        return 0.0
    return max(0.0, min(1.0, raw / max_raw))


def _rainfall_severity(rainfall_mm):
    """0-1 severity for a single rainfall reading, tiered rather than
    linear so a light drizzle doesn't read as meaningfully risky."""
    if rainfall_mm >= 50:
        return 1.0, "Extreme rainfall"
    if rainfall_mm >= 30:
        return 0.75, "Heavy rainfall"
    if rainfall_mm >= 15:
        return 0.5, "Moderate rainfall"
    if rainfall_mm >= 5:
        return 0.25, "Active rainfall"
    return 0.0, None


def _forecast_rainfall_severity(forecast_rain_total, max_forecast_rain):
    if forecast_rain_total >= 80:
        return 1.0, "Very wet 5-day forecast"
    if forecast_rain_total >= 35:
        return 0.6, "Sustained rainfall expected"
    if max_forecast_rain >= 10:
        return 0.3, "One or more rainy forecast periods"
    if forecast_rain_total > 0:
        return min(0.25, forecast_rain_total / 80), None
    return 0.0, None


def _synoptic_severity(humidity, pressure, wind_speed):
    """Humidity, pressure, and wind combined into ONE small severity
    (max 1.0) instead of each scoring independently. This is the direct
    fix for humidity being overweighted: previously humidity alone could
    contribute as much as an entire rainfall tier, which meant a merely
    humid (not actually heavily rainy) day could push risk up on its own —
    but humidity is a weak, indirect signal on its own, not a primary one,
    and it's already highly correlated with the rainfall signal that's
    scored separately and far more heavily."""
    severity = 0.0
    factors = []
    if humidity >= 90:
        severity += 0.5
        factors.append("Very high humidity")
    elif humidity >= 75:
        severity += 0.25
        factors.append("High humidity")
    if pressure <= 995:
        severity += 0.35
        factors.append("Low atmospheric pressure")
    elif pressure <= 1005:
        severity += 0.15
        factors.append("Falling pressure signal")
    if wind_speed >= 12:
        severity += 0.15
        factors.append("Strong wind may worsen storm impact")
    elif wind_speed >= 8:
        severity += 0.1
        factors.append("Moderate wind")
    return min(severity, 1.0), factors


def _drainage_severity(water, urban, community):
    """Drainage/runoff proxy: proximity to open water + built-up density
    (impervious surface runoff) + live community reports of blockages —
    there's no free API that measures actual drain capacity, so this is
    explicitly a proxy, not a direct measurement."""
    water_raw = max(0.0, water.get("score_bonus", 0))  # observed max 18
    urban_raw = max(0.0, urban.get("score_bonus", 0))  # observed max 9
    community = community or {}
    construction_reports = community.get("construction_reports", 0)
    community_raw = min(3.0, construction_reports * 0.75)  # observed max 3
    combined = water_raw + urban_raw + community_raw  # max 30
    return _severity(combined, 30), community_raw > 0


def _historical_raw(historical_reports):
    """Shared by both scoring (_context_severities) and display
    (estimate_environment) so the number a user sees always matches the
    number actually used in the risk score — these had drifted apart
    before into two different formulas."""
    if historical_reports >= 6:
        return 10
    if historical_reports >= 3:
        return 6
    if historical_reports >= 1:
        return 3
    return 0


def _context_severities(context):
    """Builds the 0-1 severity for every weighted bucket except rainfall
    (handled separately since it needs the raw weather reading, not just
    context). Returns (severities dict, factors list)."""
    context = context or {}
    terrain = context.get("terrain") or {"score_bonus": 0, "label": "Elevation data unavailable"}
    slope = context.get("slope") or {"score_bonus": 0, "label": "Slope data unavailable"}
    water = context.get("water") or {"score_bonus": 0, "label": "Water proximity unavailable"}
    soil = context.get("soil") or {"score_bonus": 0, "label": "Soil data unavailable"}
    urban = context.get("urban") or {"score_bonus": 0, "label": "Building density unavailable"}
    tide = context.get("tide")
    river = context.get("river_discharge")
    moisture = context.get("soil_moisture")
    earth_engine = context.get("earth_engine")
    community = context.get("community")
    historical_reports = context.get("historical_reports", 0)

    factors = []
    severities = {}

    # Terrain: elevation + slope combined (observed raw max 22 + 9 = 31)
    terrain_raw = max(0.0, terrain.get("score_bonus", 0)) + max(0.0, slope.get("score_bonus", 0))
    severities["terrain"] = _severity(terrain_raw, 31)
    if terrain.get("score_bonus", 0) > 0:
        factors.append(f"Terrain: {terrain['label']}")
    if slope.get("score_bonus", 0) > 0:
        factors.append(f"Slope: {slope['label']}")

    # Soil: static type + real-time moisture combined (observed raw max 7 + 10 = 17)
    soil_raw = max(0.0, soil.get("score_bonus", 0)) + (max(0.0, moisture.get("score_bonus", 0)) if moisture else 0)
    severities["soil"] = _severity(soil_raw, 17)
    if soil.get("score_bonus", 0) > 0:
        factors.append(f"Soil: {soil['label']}")
    if moisture and moisture.get("score_bonus", 0) > 0:
        factors.append(f"Soil moisture: {moisture['label']}")

    # River discharge (GloFAS), observed raw max 20
    river_raw = max(0.0, river.get("score_bonus", 0)) if river else 0
    severities["river_discharge"] = _severity(river_raw, 20)
    if river and river.get("score_bonus", 0) > 0:
        factors.append(f"Hydrology (GloFAS): {river['label']}")

    # Tide, coastal-only, observed raw max 8
    tide_raw = max(0.0, tide.get("score_bonus", 0)) if tide else 0
    severities["tide"] = _severity(tide_raw, 8)
    if tide and tide.get("score_bonus", 0) > 0:
        factors.append(f"Tide: {tide['label']}")

    # Drainage/runoff proxy
    drainage_sev, has_community_signal = _drainage_severity(water, urban, community)
    severities["drainage"] = drainage_sev
    if water.get("score_bonus", 0) > 0:
        factors.append(f"Water proximity: {water['label']}")
    if urban.get("score_bonus", 0) > 0:
        factors.append(f"Urbanization: {urban['label']}")
    if has_community_signal:
        factors.append("Community-reported construction/drainage blockage nearby")

    # Historical community-reported flooding frequency, tiered 0/3/6/10 raw -> /10
    hist_raw = _historical_raw(historical_reports)
    if historical_reports >= 6:
        factors.append(f"Community-reported flooding history: {historical_reports} past reports at this location")
    elif historical_reports >= 3:
        factors.append(f"Community-reported flooding history: {historical_reports} past reports at this location")
    elif historical_reports >= 1:
        factors.append(f"Community-reported flooding history: {historical_reports} past report(s) at this location")
    severities["historical"] = _severity(hist_raw, 10)

    # Earth Engine satellite reinforcement, observed raw max 35 (its own internal cap)
    if earth_engine and earth_engine.get("available"):
        sat_raw = max(0.0, earth_engine.get("score_bonus", 0))
        severities["satellite"] = _severity(sat_raw, 35)
        factors.extend(earth_engine.get("factors") or [])
    else:
        severities["satellite"] = 0.0

    return severities, factors


def calculate_day_score(rainfall, humidity, pressure, wind_speed, context):
    """Same terrain/coastal-aware weighted model as 'right now', applied to
    a single forecast day's weather — so the 5-day forecast can warn ahead
    of time for vulnerable terrain, not just flag it once flooding is
    already underway."""
    rain_sev, _ = _rainfall_severity(rainfall)
    synoptic_sev, _ = _synoptic_severity(humidity, pressure, wind_speed)
    context_sev, _ = _context_severities(context)

    score = (
        rain_sev * SCORE_WEIGHTS["current_rainfall"]
        + synoptic_sev * SCORE_WEIGHTS["synoptic"]
        + context_sev["terrain"] * SCORE_WEIGHTS["terrain"]
        + context_sev["soil"] * SCORE_WEIGHTS["soil"]
        + context_sev["river_discharge"] * SCORE_WEIGHTS["river_discharge"]
        + context_sev["tide"] * SCORE_WEIGHTS["tide"]
        + context_sev["drainage"] * SCORE_WEIGHTS["drainage"]
        + context_sev["historical"] * SCORE_WEIGHTS["historical"]
        + context_sev["satellite"] * SCORE_WEIGHTS["satellite"]
    )
    score = max(0, min(round(score), 100))
    coastal = bool((context or {}).get("coastal"))
    risk = classify_risk(score, coastal=coastal)
    return score, risk


def _compute_data_confidence(context):
    """What this app actually calls 'confidence' should mean 'how much of
    our real data did we actually get for this request' — not a restated
    function of the risk score itself. The previous formula, `min(95, 68 +
    score // 3)`, meant a CRITICAL reading always showed ~95% confidence
    even when half the underlying lookups had failed and fallen back to
    defaults, which is exactly the kind of output that doesn't correspond
    to its actual sources. This instead checks how many of the real
    external data sources returned usable data for this specific request."""
    context = context or {}
    checks = []

    terrain = context.get("terrain") or {}
    checks.append("unavailable" not in terrain.get("label", "").lower())

    slope = context.get("slope") or {}
    checks.append("unavailable" not in slope.get("label", "").lower())

    water = context.get("water") or {}
    checks.append("unavailable" not in water.get("label", "").lower())

    soil = context.get("soil") or {}
    checks.append("unavailable" not in soil.get("label", "").lower())

    river = context.get("river_discharge")
    checks.append(bool(river) and "unavailable" not in river.get("label", "").lower())

    moisture = context.get("soil_moisture")
    checks.append(bool(moisture) and "unavailable" not in moisture.get("label", "").lower())

    # Tide only meaningfully applies to coastal locations — don't penalize
    # an inland location's confidence for correctly having no tide data.
    if context.get("coastal"):
        tide = context.get("tide")
        checks.append(bool(tide) and tide.get("current_height") is not None)

    earth_engine = context.get("earth_engine")
    checks.append(bool(earth_engine) and bool(earth_engine.get("available")))

    if not checks:
        return 50
    return round(100 * sum(1 for c in checks if c) / len(checks))


def calculate_flood_score(weather, forecast, context=None):
    """Combines forecast rainfall, current rainfall, terrain, soil,
    hydrology, tide, drainage, historical pattern, synoptic conditions, and
    satellite reinforcement into a single weighted score out of 100 (see
    SCORE_WEIGHTS above) — rather than an unbounded additive tally, which
    is what let risk read too high too often and let humidity punch above
    its actual predictive weight."""
    context = context or {}
    factors = []

    rainfall = weather["rainfall"]
    humidity = weather["humidity"]
    pressure = weather["pressure"]
    wind_speed = weather["wind"]
    forecast_rain_total = sum(day["rain"] for day in forecast)
    max_forecast_rain = max([day["rain"] for day in forecast], default=0)

    rain_sev, rain_factor = _rainfall_severity(rainfall)
    if rain_factor:
        factors.append(f"Current {rain_factor.lower()}" if "rainfall" in rain_factor.lower() else rain_factor)

    forecast_sev, forecast_factor = _forecast_rainfall_severity(forecast_rain_total, max_forecast_rain)
    if forecast_factor:
        factors.append(forecast_factor)

    synoptic_sev, synoptic_factors = _synoptic_severity(humidity, pressure, wind_speed)
    factors.extend(synoptic_factors)

    context_sev, context_factors = _context_severities(context)
    factors.extend(context_factors)

    score = (
        rain_sev * SCORE_WEIGHTS["current_rainfall"]
        + forecast_sev * SCORE_WEIGHTS["forecast_rainfall"]
        + synoptic_sev * SCORE_WEIGHTS["synoptic"]
        + context_sev["terrain"] * SCORE_WEIGHTS["terrain"]
        + context_sev["soil"] * SCORE_WEIGHTS["soil"]
        + context_sev["river_discharge"] * SCORE_WEIGHTS["river_discharge"]
        + context_sev["tide"] * SCORE_WEIGHTS["tide"]
        + context_sev["drainage"] * SCORE_WEIGHTS["drainage"]
        + context_sev["historical"] * SCORE_WEIGHTS["historical"]
        + context_sev["satellite"] * SCORE_WEIGHTS["satellite"]
    )
    score = max(0, min(round(score), 100))
    coastal = bool(context.get("coastal"))
    risk = classify_risk(score, coastal=coastal)

    if coastal:
        t = RISK_THRESHOLDS["coastal"]
        factors.append(f"Coastal region — lower alert threshold applied (HIGH starts at {t['high']} instead of {RISK_THRESHOLDS['inland']['high']})")

    return {
        "score": score,
        "confidence": _compute_data_confidence(context),
        "risk": risk["level"],
        "risk_color": risk["color"],
        "map_color": risk["map_color"],
        "advice": risk["advice"],
        "priority_action": risk["priority_action"],
        "coastal": coastal,
        "factors": factors or ["No major flood trigger detected from current conditions"],
    }


def get_forecast(lat, lon, context=None):
    """Returns (daily_forecast, timeline). Both come from a single OpenWeather
    call: daily_forecast is one representative slot per day (existing 5-day
    cards), timeline is every 3-hour slot for the next ~24h (previously
    fetched and discarded) — reused for the flood timeline display and for
    picking a lower-risk travel window."""
    data = fetch_openweather(
        "forecast",
        {"lat": lat, "lon": lon, "appid": API_KEY, "units": "metric"},
    )
    if not data:
        return [], []

    forecast = []
    timeline = []
    seen_dates = set()
    raw_items = data.get("list", [])

    for item in raw_items:
        forecast_time = datetime.strptime(item["dt_txt"], "%Y-%m-%d %H:%M:%S")
        date_key = forecast_time.strftime("%Y-%m-%d")

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
        slot_score, slot_risk = calculate_day_score(rainfall, humidity, pressure, wind, context)

        if len(timeline) < 12:  # next ~36h at 3h resolution
            timeline.append(
                {
                    "time": forecast_time.strftime("%a %I:%M %p"),
                    "hour_label": forecast_time.strftime("%I %p").lstrip("0"),
                    "rain": rainfall,
                    "weather": description,
                    "score": slot_score,
                    "risk": slot_risk["level"],
                    "risk_color": slot_risk["color"],
                }
            )

        if date_key not in seen_dates and len(forecast) < 5:
            seen_dates.add(date_key)
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
                    "score": slot_score,
                    "risk": slot_risk["level"],
                    "risk_color": slot_risk["color"],
                    "priority_action": slot_risk["priority_action"],
                    "scene": scene["code"],
                }
            )

    return forecast, timeline


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


_ee_init_lock = threading.Lock()
_ee_initialized = False
_ee_init_error = None


def earth_engine_configured():
    return bool(EARTH_ENGINE_ENABLED and ee is not None)


def initialize_earth_engine():
    """Initialize Google Earth Engine once per process.

    Supports either a service-account JSON file (recommended for Flask
    hosting) or the default Earth Engine credentials available in the runtime.
    """
    global _ee_initialized, _ee_init_error

    if not EARTH_ENGINE_ENABLED:
        _ee_init_error = "Earth Engine disabled by EARTH_ENGINE_ENABLED."
        return False
    if ee is None:
        _ee_init_error = "earthengine-api is not installed."
        return False
    if _ee_initialized:
        return True

    with _ee_init_lock:
        if _ee_initialized:
            return True
        try:
            init_kwargs = {}
            if GEE_PROJECT:
                init_kwargs["project"] = GEE_PROJECT

            if GEE_PRIVATE_KEY_PATH and os.path.exists(GEE_PRIVATE_KEY_PATH):
                service_account = GEE_SERVICE_ACCOUNT
                if not service_account:
                    with open(GEE_PRIVATE_KEY_PATH, encoding="utf-8") as key_file:
                        service_account = json.load(key_file).get("client_email")
                credentials = ee.ServiceAccountCredentials(service_account, GEE_PRIVATE_KEY_PATH)
                ee.Initialize(credentials, **init_kwargs)
            else:
                ee.Initialize(**init_kwargs)

            _ee_initialized = True
            _ee_init_error = None
            return True
        except Exception as error:  # noqa: BLE001 - Earth Engine must fail closed, not break weather lookups
            _ee_init_error = str(error)
            print(f"Earth Engine initialization failed: {error}")
            return False


def _ee_reduce_mean(image, region, scale):
    return image.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=region,
        scale=scale,
        maxPixels=1e8,
        bestEffort=True,
    ).getInfo() or {}


def _ee_reduce_sum(image, region, scale):
    return image.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=region,
        scale=scale,
        maxPixels=1e8,
        bestEffort=True,
    ).getInfo() or {}


def _round_or_none(value, digits=1):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _land_cover_label(label_id):
    labels = {
        0: "water",
        1: "trees",
        2: "grass",
        3: "flooded vegetation",
        4: "crops",
        5: "shrub and scrub",
        6: "built area",
        7: "bare ground",
        8: "snow and ice",
    }
    if label_id is None:
        return None
    return labels.get(int(label_id), "unknown")


def _score_earth_engine_context(data):
    if not data or not data.get("available"):
        return {"score_bonus": 0, "factors": []}

    score = 0
    factors = []
    flood_area = data.get("sentinel1_flood_area_ha")
    if flood_area is not None:
        if flood_area >= 50:
            score += 18
            factors.append(f"Sentinel-1 detected a large possible flood extent (~{flood_area:.1f} ha)")
        elif flood_area >= 10:
            score += 12
            factors.append(f"Sentinel-1 detected possible flood extent (~{flood_area:.1f} ha)")
        elif flood_area >= 1:
            score += 6
            factors.append(f"Sentinel-1 detected small possible inundation patches (~{flood_area:.1f} ha)")

    rain_7d = data.get("chirps_7d_mm")
    if rain_7d is not None:
        if rain_7d >= 120:
            score += 16
            factors.append(f"CHIRPS shows extreme 7-day rainfall (~{rain_7d:.0f} mm)")
        elif rain_7d >= 70:
            score += 10
            factors.append(f"CHIRPS shows heavy 7-day rainfall (~{rain_7d:.0f} mm)")
        elif rain_7d >= 35:
            score += 5
            factors.append(f"CHIRPS shows notable recent rainfall (~{rain_7d:.0f} mm)")

    ndwi = data.get("sentinel2_ndwi")
    if ndwi is not None and ndwi >= 0.25:
        score += 6
        factors.append(f"Sentinel-2 NDWI indicates strong surface-water signal ({ndwi:.2f})")

    water_occurrence = data.get("jrc_water_occurrence_pct")
    if water_occurrence is not None and water_occurrence >= 20:
        score += 5
        factors.append(f"JRC Global Surface Water shows recurring water presence (~{water_occurrence:.0f}%)")

    watershed_score = data.get("watershed_susceptibility_score")
    if watershed_score is not None:
        if watershed_score >= 8:
            score += 8
            factors.append("Terrain/watershed proxy: low, flat terrain likely to retain runoff")
        elif watershed_score >= 5:
            score += 4
            factors.append("Terrain/watershed proxy: moderate runoff accumulation susceptibility")

    land_cover = data.get("dynamic_world_label")
    if land_cover in ("built area", "flooded vegetation", "water"):
        score += 4
        factors.append(f"Dynamic World land cover is {land_cover}, which can increase flood exposure")

    return {"score_bonus": min(score, 35), "factors": factors}


def _compute_earth_engine_context(lat, lon):
    if not initialize_earth_engine():
        return {"available": False, "error": _ee_init_error}

    point = ee.Geometry.Point([lon, lat])
    region = point.buffer(3000)
    now = datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
    after_start = (now - timedelta(days=14)).strftime("%Y-%m-%d")
    before_start = (now - timedelta(days=45)).strftime("%Y-%m-%d")
    before_end = (now - timedelta(days=16)).strftime("%Y-%m-%d")
    s2_start = (now - timedelta(days=45)).strftime("%Y-%m-%d")
    dw_start = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    chirps_7d_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    chirps_30d_start = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    result = {
        "available": True,
        "source": "Google Earth Engine",
        "analysis_radius_km": 3,
        "updated_at": now.isoformat(),
    }

    try:
        dem = ee.Image("COPERNICUS/DEM/GLO30_2024_1").select("DEM")
        slope_img = ee.Terrain.slope(dem).rename("slope")
        terrain_stats = _ee_reduce_mean(dem.rename("elevation").addBands(slope_img), region, 30)
        result["gee_elevation_m"] = _round_or_none(terrain_stats.get("elevation"), 1)
        result["gee_slope_deg"] = _round_or_none(terrain_stats.get("slope"), 2)
    except Exception as error:  # noqa: BLE001
        result["dem_error"] = str(error)
        dem = None
        slope_img = None

    try:
        jrc = ee.Image("JRC/GSW1_4/GlobalSurfaceWater")
        jrc_stats = _ee_reduce_mean(
            jrc.select("occurrence").rename("water_occurrence")
            .addBands(jrc.select("seasonality").rename("water_seasonality")),
            region,
            30,
        )
        result["jrc_water_occurrence_pct"] = _round_or_none(jrc_stats.get("water_occurrence"), 1)
        result["jrc_water_seasonality_months"] = _round_or_none(jrc_stats.get("water_seasonality"), 1)
    except Exception as error:  # noqa: BLE001
        result["jrc_error"] = str(error)

    try:
        chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").select("precipitation")
        rain_7d = chirps.filterDate(chirps_7d_start, today).sum().rename("rain_7d")
        rain_30d = chirps.filterDate(chirps_30d_start, today).sum().rename("rain_30d")
        rain_stats = _ee_reduce_mean(rain_7d.addBands(rain_30d), region, 5500)
        result["chirps_7d_mm"] = _round_or_none(rain_stats.get("rain_7d"), 1)
        result["chirps_30d_mm"] = _round_or_none(rain_stats.get("rain_30d"), 1)
    except Exception as error:  # noqa: BLE001
        result["chirps_error"] = str(error)

    try:
        s2 = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(point)
            .filterDate(s2_start, today)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
        )
        if s2.size().getInfo() > 0:
            s2_img = s2.median()
            ndwi = s2_img.normalizedDifference(["B3", "B8"]).rename("ndwi")
            ndvi = s2_img.normalizedDifference(["B8", "B4"]).rename("ndvi")
            s2_stats = _ee_reduce_mean(ndwi.addBands(ndvi), region, 20)
            result["sentinel2_ndwi"] = _round_or_none(s2_stats.get("ndwi"), 3)
            result["sentinel2_ndvi"] = _round_or_none(s2_stats.get("ndvi"), 3)
            result["sentinel2_image_count"] = s2.size().getInfo()
        else:
            result["sentinel2_image_count"] = 0
    except Exception as error:  # noqa: BLE001
        result["sentinel2_error"] = str(error)

    try:
        s1 = (
            ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterBounds(point)
            .filter(ee.Filter.eq("instrumentMode", "IW"))
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
            .select("VV")
        )
        before = s1.filterDate(before_start, before_end)
        after = s1.filterDate(after_start, today)
        before_count = before.size().getInfo()
        after_count = after.size().getInfo()
        result["sentinel1_before_count"] = before_count
        result["sentinel1_after_count"] = after_count

        if before_count > 0 and after_count > 0:
            before_img = before.median().rename("before_vv")
            after_img = after.median().rename("after_vv")
            vv_change = after_img.subtract(before_img).rename("vv_change")
            permanent_water = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence").gte(80)
            flood_mask = after_img.lt(-16).And(vv_change.lt(-3)).And(permanent_water.Not())
            if slope_img is not None:
                flood_mask = flood_mask.And(slope_img.lt(5))
            flood_area = flood_mask.rename("flood").multiply(ee.Image.pixelArea()).rename("flood_area_m2")
            area_stats = _ee_reduce_sum(flood_area, region, 30)
            vv_stats = _ee_reduce_mean(before_img.addBands(after_img).addBands(vv_change), region, 30)
            result["sentinel1_flood_area_ha"] = _round_or_none((area_stats.get("flood_area_m2") or 0) / 10000, 2)
            result["sentinel1_before_vv_db"] = _round_or_none(vv_stats.get("before_vv"), 2)
            result["sentinel1_after_vv_db"] = _round_or_none(vv_stats.get("after_vv"), 2)
            result["sentinel1_vv_change_db"] = _round_or_none(vv_stats.get("vv_change"), 2)
    except Exception as error:  # noqa: BLE001
        result["sentinel1_error"] = str(error)

    try:
        dw = (
            ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
            .filterBounds(point)
            .filterDate(dw_start, today)
        )
        dw_count = dw.size().getInfo()
        result["dynamic_world_image_count"] = dw_count
        if dw_count > 0:
            mode_label = dw.select("label").reduce(ee.Reducer.mode()).rename("landcover_mode")
            probabilities = dw.select(["water", "flooded_vegetation", "built"]).mean()
            dw_stats = _ee_reduce_mean(mode_label.addBands(probabilities), region, 10)
            result["dynamic_world_label"] = _land_cover_label(dw_stats.get("landcover_mode"))
            result["dynamic_world_water_prob"] = _round_or_none(dw_stats.get("water"), 3)
            result["dynamic_world_flooded_vegetation_prob"] = _round_or_none(dw_stats.get("flooded_vegetation"), 3)
            result["dynamic_world_built_prob"] = _round_or_none(dw_stats.get("built"), 3)
    except Exception as error:  # noqa: BLE001
        result["dynamic_world_error"] = str(error)

    elevation = result.get("gee_elevation_m")
    slope_deg = result.get("gee_slope_deg")
    water_occurrence = result.get("jrc_water_occurrence_pct") or 0
    watershed_score = 0
    if elevation is not None:
        watershed_score += 4 if elevation <= 10 else 2 if elevation <= 25 else 0
    if slope_deg is not None:
        watershed_score += 4 if slope_deg <= 1 else 2 if slope_deg <= 3 else 0
    if water_occurrence >= 20:
        watershed_score += 2
    result["watershed_susceptibility_score"] = min(10, watershed_score)
    result["score_bonus"] = _score_earth_engine_context(result)["score_bonus"]
    result["factors"] = _score_earth_engine_context(result)["factors"]
    return result


def get_earth_engine_context(city_key, lat, lon):
    if not EARTH_ENGINE_ENABLED:
        return {"available": False, "error": "Earth Engine disabled."}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM earth_engine_cache WHERE city_key = ?", (city_key,)).fetchone()
    if row:
        age_hours = (datetime.utcnow() - datetime.fromisoformat(row["updated_at"])).total_seconds() / 3600
        if age_hours < EARTH_ENGINE_CONTEXT_TTL_HOURS:
            conn.close()
            try:
                return json.loads(row["payload_json"])
            except (TypeError, ValueError):
                pass

    payload = _compute_earth_engine_context(lat, lon)
    conn.execute(
        """
        INSERT INTO earth_engine_cache (city_key, payload_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(city_key) DO UPDATE SET
            payload_json=excluded.payload_json,
            updated_at=excluded.updated_at
        """,
        (city_key, json.dumps(payload), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return payload


def get_geo_context(city_key, lat, lon):
    """Elevation, slope, water proximity, soil type, and urbanization for a
    location, cached for GEO_CONTEXT_TTL_HOURS. This is the fix for
    Overpass/SoilGrids rate-limiting: a 29-location watchlist refreshing
    every 15 minutes was re-fetching static terrain data ~100 times/hour
    that hadn't changed since the last sweep. Now each location only hits
    those APIs once per day; weather, tide, and river discharge (which
    genuinely change) are still fetched fresh every time by the caller.

    Also stale-if-error: if the TTL has expired and a live re-fetch fails
    for a specific field (Overpass down, SoilGrids down, etc.), this falls
    back to the last successfully cached value for that exact field rather
    than returning None/"unavailable". Terrain, soil, and water proximity
    barely change over time, so a slightly-stale real number is strictly
    better than a blank one during a temporary outage."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM geo_context_cache WHERE city_key = ?", (city_key,)).fetchone()

    cached = None
    if row:
        water_point = (
            (row["nearest_water_lat"], row["nearest_water_lon"])
            if row["nearest_water_lat"] is not None
            else None
        )
        try:
            cached_contacts = json.loads(row["emergency_contacts_json"]) if row["emergency_contacts_json"] else []
        except (ValueError, TypeError):
            cached_contacts = []
        cached = {
            "elevation": row["elevation"],
            "slope_percent": row["slope_percent"],
            "nearest_water_m": row["nearest_water_m"],
            "nearest_coast_m": row["nearest_coast_m"],
            "nearest_water_point": water_point,
            "nearest_water_label": row["nearest_water_label"],
            "building_count": row["building_count"],
            "clay_percent": row["clay_percent"],
            "emergency_contacts": cached_contacts,
        }

        age_hours = (datetime.utcnow() - datetime.fromisoformat(row["updated_at"])).total_seconds() / 3600
        if age_hours < GEO_CONTEXT_TTL_HOURS:
            conn.close()
            cached["building_count"] = cached["building_count"] or 0
            return cached

    # Cache miss or stale — fetch live from Overpass/Open-Meteo/SoilGrids,
    # falling back field-by-field to the stale cached value if a specific
    # service failed outright on this attempt.
    elevation, slope_percent = fetch_elevation_grid(lat, lon)
    if elevation is None and cached and cached["elevation"] is not None:
        elevation, slope_percent = cached["elevation"], cached["slope_percent"]

    nearest_water_m, nearest_coast_m, building_count, nearest_water_point, nearest_water_label = (
        fetch_water_and_urban_context(lat, lon)
    )
    # building_count is None only when the Overpass call failed outright
    # (a successful call always returns an int, even 0) — that's the
    # reliable signal to fall back to the whole stale bundle from that call.
    if building_count is None and cached and cached["building_count"] is not None:
        nearest_water_m = cached["nearest_water_m"]
        nearest_coast_m = cached["nearest_coast_m"]
        building_count = cached["building_count"]
        nearest_water_point = cached["nearest_water_point"]
        nearest_water_label = cached["nearest_water_label"]

    clay_percent = fetch_soil_clay(lat, lon)
    if clay_percent is None and cached and cached["clay_percent"] is not None:
        clay_percent = cached["clay_percent"]

    emergency_contacts = fetch_emergency_contacts(lat, lon)
    if not emergency_contacts and cached and cached["emergency_contacts"]:
        emergency_contacts = cached["emergency_contacts"]

    now = datetime.utcnow().isoformat()
    conn.execute(
        """
        INSERT INTO geo_context_cache
            (city_key, elevation, slope_percent, nearest_water_m, nearest_coast_m,
             nearest_water_lat, nearest_water_lon, nearest_water_label, building_count, clay_percent,
             emergency_contacts_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            emergency_contacts_json=excluded.emergency_contacts_json,
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
            json.dumps(emergency_contacts),
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
        "emergency_contacts": emergency_contacts,
    }


def fetch_route(origin_lat, origin_lon, dest_lat, dest_lon, alternatives=True):
    """Real driving route from OSRM (free, no key). Returns a list of
    routes, each with distance (m), duration (s), and a coordinate path."""
    coords = f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    try:
        response = requests.get(
            f"{OSRM_URL}/{coords}",
            params={
                "overview": "full",
                "geometries": "geojson",
                "alternatives": "true" if alternatives else "false",
                "steps": "false",
            },
            timeout=15,
            headers={"User-Agent": "FloodGuardAI/1.0 (flood risk web app; contact via app owner)"},
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"OSRM routing request failed: {error}")
        return []

    if data.get("code") != "Ok":
        return []

    routes = []
    for route in data.get("routes", []):
        coords_geojson = route.get("geometry", {}).get("coordinates", [])
        # GeoJSON is [lon, lat] — flip to (lat, lon) for consistency with the rest of the app.
        path = [(c[1], c[0]) for c in coords_geojson]
        routes.append({
            "distance_m": route.get("distance", 0),
            "duration_s": route.get("duration", 0),
            "path": path,
        })
    return routes


def sample_route_points(path, n=ROUTE_SAMPLE_POINTS):
    """Evenly-spaced sample points along a route path."""
    if not path:
        return []
    if len(path) <= n:
        return path
    step = (len(path) - 1) / (n - 1)
    return [path[round(i * step)] for i in range(n)]


def assess_route_safety(origin_query, destination_query):
    """The flagship feature: 'can I safely travel from A to B right now?'
    Samples points along a real driving route (OSRM) and scores each with
    the same terrain/coastal-aware model used everywhere else in this app,
    using cached geo context so repeat route queries over the same area
    don't re-hit Overpass, and a small number of real weather readings
    along the way rather than one per sample point (a deliberate tradeoff:
    rain genuinely varies across a long route, but per-point weather calls
    would multiply external API usage for limited extra accuracy)."""
    origin = geocode_location(origin_query)
    destination = geocode_location(destination_query)

    if not origin:
        return {"ok": False, "error": f"Could not find '{origin_query}'."}
    if not destination:
        return {"ok": False, "error": f"Could not find '{destination_query}'."}

    routes = fetch_route(origin["lat"], origin["lon"], destination["lat"], destination["lon"])
    if not routes:
        return {
            "ok": False,
            "error": "Could not find a driving route between these locations. They may be too far apart, "
            "on different landmasses, or the routing service is temporarily unavailable.",
        }

    weather_points = []
    for lat, lon in ((origin["lat"], origin["lon"]), (destination["lat"], destination["lon"])):
        w = get_weather(lat, lon, display_name="route-point")
        if w:
            weather_points.append({"lat": lat, "lon": lon, "weather": w})

    def nearest_weather(lat, lon):
        if not weather_points:
            return None
        return min(weather_points, key=lambda wp: haversine_meters(lat, lon, wp["lat"], wp["lon"]))["weather"]

    assessed_routes = []
    for route in routes[:2]:  # primary + at most one alternative
        sample_points = sample_route_points(route["path"])
        segments = []
        available_count = 0

        for idx, (lat, lon) in enumerate(sample_points):
            city_key = f"route:{round(lat, 3)},{round(lon, 3)}"
            geo = get_geo_context(city_key, lat, lon)
            terrain = classify_terrain(geo["elevation"])
            slope = classify_slope(geo["slope_percent"])
            water = classify_water_proximity(geo["nearest_water_m"], geo["nearest_water_label"])
            urban = classify_urbanization(geo["building_count"])
            coastal = is_coastal_region(geo["nearest_coast_m"])
            soil = classify_soil(geo["clay_percent"])

            if geo["elevation"] is not None:
                available_count += 1

            w = nearest_weather(lat, lon)
            score, risk = calculate_day_score(
                w["rainfall"] if w else 0,
                w["humidity"] if w else 50,
                w["pressure"] if w else 1013,
                w["wind"] if w else 0,
                {
                    "terrain": terrain,
                    "slope": slope,
                    "water": water,
                    "soil": soil,
                    "urban": urban,
                    "tide": None,
                    "historical_reports": 0,
                    "coastal": coastal,
                },
            )

            segments.append(
                {
                    "position_pct": round(idx / max(1, len(sample_points) - 1) * 100),
                    "lat": lat,
                    "lon": lon,
                    "score": score,
                    "risk": risk["level"],
                    "risk_color": risk["color"],
                    "coastal": coastal,
                    "water_label": geo["nearest_water_label"],
                    "elevation": round(geo["elevation"]) if geo["elevation"] is not None else None,
                }
            )

        worst_segment = max(segments, key=lambda s: s["score"]) if segments else None
        risky_segments = [s for s in segments if s["risk"] in ("HIGH", "SEVERE", "CRITICAL")]

        assessed_routes.append(
            {
                "distance_km": round(route["distance_m"] / 1000, 1),
                "duration_min": round(route["duration_s"] / 60),
                "segments": segments,
                "worst_risk": worst_segment["risk"] if worst_segment else "LOW",
                "worst_score": worst_segment["score"] if worst_segment else 0,
                "risky_segments": risky_segments,
                "confidence_pct": round(100 * available_count / len(sample_points)) if sample_points else 0,
            }
        )

    # Sort so the lowest-risk route comes first.
    assessed_routes.sort(key=lambda r: r["worst_score"])
    primary = assessed_routes[0]
    alternative = assessed_routes[1] if len(assessed_routes) > 1 else None

    primary_coastal = any(s["coastal"] for s in primary["segments"])
    risk_meta = classify_risk(primary["worst_score"], coastal=primary_coastal)
    travel_rec = build_travel_recommendation(primary["worst_risk"], primary["worst_score"], None)

    origin_label = origin["name"] + (f", {origin['state']}" if origin.get("state") else "")
    destination_label = destination["name"] + (f", {destination['state']}" if destination.get("state") else "")

    return {
        "ok": True,
        "origin": origin_label,
        "destination": destination_label,
        "origin_coords": [origin["lat"], origin["lon"]],
        "destination_coords": [destination["lat"], destination["lon"]],
        "primary_route": primary,
        "alternative_route": alternative,
        "verdict": travel_rec["verdict"],
        "verdict_color": travel_rec["color"],
        "advice": risk_meta["advice"],
        "priority_action": risk_meta["priority_action"],
    }


PROPERTY_RISK_LEVELS = {
    "LOW": {
        "color": "low",
        "headline": "Low long-term flood exposure",
        "guidance": "No strong long-term flood indicators for this location. Still worth asking about drainage history during due diligence — this is a terrain/history-based estimate, not a guarantee.",
    },
    "MODERATE": {
        "color": "moderate",
        "headline": "Some long-term flood exposure",
        "guidance": "Consider asking the seller/landlord about past flooding, checking local drainage infrastructure, and reviewing flood insurance options before committing.",
    },
    "HIGH": {
        "color": "high",
        "headline": "Elevated long-term flood exposure",
        "guidance": "Strongly consider a professional flood-risk survey, ask directly about flooding history, check whether the property has flood-resistant features (raised foundation, sump pump, etc.), and price flood insurance into your decision.",
    },
    "SEVERE": {
        "color": "severe",
        "headline": "Significant long-term flood exposure",
        "guidance": "This location shows multiple strong long-term flood indicators (low elevation, close/frequent water presence, and/or a real history of community-reported flooding). Get a professional flood-risk assessment before buying or signing a lease, and factor in flood insurance cost and availability.",
    },
}


def assess_property_flood_risk(location_query):
    """A deliberately WEATHER-INDEPENDENT long-term flood vulnerability
    check for prospective buyers/renters — this is the opposite question
    from the main flood score ('is it flooding right now'). It only uses
    static, slow-changing signals: elevation/slope, distance to water,
    soil drainage character, JRC Global Surface Water's historical water
    occurrence (Earth Engine — a genuine long-term indicator of a
    location's water-adjacent character, not today's weather), coastal
    status, and the community's historical flood-report count. This is
    what makes the site useful in the dry season too, not just during an
    active flood event."""
    place = geocode_location(location_query)
    if not place:
        return {"ok": False, "error": f"Could not find '{location_query}'."}

    lat, lon = place["lat"], place["lon"]
    location_label = place["name"] + (f", {place['state']}" if place.get("state") else "")
    city_key = normalize_city(location_label)

    geo = get_geo_context(city_key, lat, lon)
    earth_engine = get_earth_engine_context(city_key, lat, lon)
    historical_reports = get_historical_frequency(location_label)

    elevation = geo["elevation"]
    slope_percent = geo["slope_percent"]
    terrain = classify_terrain(elevation)
    slope = classify_slope(slope_percent)
    water = classify_water_proximity(geo["nearest_water_m"], geo["nearest_water_label"])
    coastal = is_coastal_region(geo["nearest_coast_m"])
    soil = classify_soil(geo["clay_percent"])

    # Static score out of 100, reusing the same weighting philosophy as the
    # main model but WITHOUT any of the weather/hydrology-of-the-moment
    # factors (rainfall, tide, river discharge, soil moisture) — those
    # answer "is it flooding today", not "should a buyer be cautious here
    # in general".
    terrain_raw = max(0.0, terrain.get("score_bonus", 0)) + max(0.0, slope.get("score_bonus", 0))
    terrain_sev = _severity(terrain_raw, 31)

    water_raw = max(0.0, water.get("score_bonus", 0))
    water_sev = _severity(water_raw, 18)

    soil_raw = max(0.0, soil.get("score_bonus", 0))
    soil_sev = _severity(soil_raw, 7)

    hist_raw = _historical_raw(historical_reports)
    hist_sev = _severity(hist_raw, 10)

    water_occurrence_pct = earth_engine.get("jrc_water_occurrence_pct") if earth_engine and earth_engine.get("available") else None
    occurrence_sev = _severity(water_occurrence_pct or 0, 40)  # 40%+ occurrence is a strong long-term water-presence signal

    coastal_sev = 1.0 if coastal else 0.0

    weights = {"terrain": 25, "water": 20, "occurrence": 20, "coastal": 15, "historical": 12, "soil": 8}
    score = (
        terrain_sev * weights["terrain"]
        + water_sev * weights["water"]
        + occurrence_sev * weights["occurrence"]
        + coastal_sev * weights["coastal"]
        + hist_sev * weights["historical"]
        + soil_sev * weights["soil"]
    )
    score = max(0, min(round(score), 100))

    if score >= 65:
        level = "SEVERE"
    elif score >= 42:
        level = "HIGH"
    elif score >= 20:
        level = "MODERATE"
    else:
        level = "LOW"

    factors = []
    if terrain.get("score_bonus", 0) > 0:
        factors.append(f"Terrain: {terrain['label']}")
    if slope.get("score_bonus", 0) > 0:
        factors.append(f"Slope: {slope['label']}")
    if water.get("score_bonus", 0) > 0:
        factors.append(f"Water proximity: {water['label']}")
    if coastal:
        factors.append("Coastal region (within 10 km of an ocean/sea coastline)")
    if water_occurrence_pct is not None and water_occurrence_pct >= 10:
        factors.append(f"Earth Engine JRC data shows this area has been water-covered ~{water_occurrence_pct:.0f}% of the time historically")
    if soil.get("score_bonus", 0) > 0:
        factors.append(f"Soil: {soil['label']}")
    if historical_reports > 0:
        factors.append(f"{historical_reports} community-reported flooding incident(s) at this location since tracking began")
    if not factors:
        factors.append("No significant long-term flood indicators found for this location")

    meta = PROPERTY_RISK_LEVELS[level]

    return {
        "ok": True,
        "location": location_label,
        "coordinates": [lat, lon],
        "level": level,
        "color": meta["color"],
        "headline": meta["headline"],
        "guidance": meta["guidance"],
        "score": score,
        "factors": factors,
        "elevation_m": round(elevation) if elevation is not None else None,
        "coastal": coastal,
        "historical_reports": historical_reports,
        "water_occurrence_pct": round(water_occurrence_pct) if water_occurrence_pct is not None else None,
        "earth_engine_available": bool(earth_engine and earth_engine.get("available")),
        "disclaimer": (
            "This is a location-history and terrain-based estimate for general awareness, not a certified "
            "flood-zone survey, insurance assessment, or legal disclosure. Always get a professional flood-risk "
            "survey and check official flood maps before a property purchase or lease decision."
        ),
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
    city_key = normalize_city(weather["city"])
    geo = get_geo_context(city_key, lat, lon)
    earth_engine = get_earth_engine_context(city_key, lat, lon)

    if earth_engine.get("available"):
        if geo["elevation"] is None and earth_engine.get("gee_elevation_m") is not None:
            geo["elevation"] = earth_engine["gee_elevation_m"]
        if geo["slope_percent"] is None and earth_engine.get("gee_slope_deg") is not None:
            # For small gradients, degrees and percent are close enough for this coarse risk bucket.
            geo["slope_percent"] = round(math.tan(math.radians(earth_engine["gee_slope_deg"])) * 100, 1)

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

    emergency_contacts = geo.get("emergency_contacts", [])

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
        "earth_engine": earth_engine,
        "historical_reports": historical_reports,
        "coastal": coastal,
    }

    # The forecast is fetched after context so each day can be scored with
    # the same terrain/coastal-aware model as "right now" — this is what
    # lets the 5-day forecast warn ahead of time for vulnerable terrain,
    # instead of only reacting to rainfall alone.
    forecast, timeline = get_forecast(lat, lon, context)

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

    travel_recommendation = build_travel_recommendation(flood_model["risk"], flood_model["score"], timeline)

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
        "travel_recommendation": travel_recommendation,
        "timeline": timeline,
        "emergency_contacts": emergency_contacts,
        "historical_reports": historical_reports,
        "earth_engine": earth_engine,
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
    roads_affected = (payload.get("roads_affected") or "").strip()

    try:
        rating = int(payload.get("rating", 0))
    except (TypeError, ValueError):
        rating = 0

    water_depth_cm = None
    raw_depth = payload.get("water_depth_cm")
    if raw_depth not in (None, ""):
        try:
            water_depth_cm = max(0, min(500, int(raw_depth)))
        except (TypeError, ValueError):
            water_depth_cm = None

    if not city:
        return jsonify({"ok": False, "error": "A city is required."}), 400
    if category not in CATEGORY_LABELS:
        return jsonify({"ok": False, "error": "Unknown report category."}), 400
    if rating < 1 or rating > 5:
        return jsonify({"ok": False, "error": "Rating must be between 1 and 5."}), 400
    if len(comment) > 400:
        return jsonify({"ok": False, "error": "Comment is too long (400 characters max)."}), 400
    if len(roads_affected) > 200:
        return jsonify({"ok": False, "error": "Roads affected is too long (200 characters max)."}), 400

    save_contribution(city, category, rating, comment, water_depth_cm, roads_affected)

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


@app.route("/api/route-safety", methods=["POST"])
def api_route_safety():
    """The flagship feature: 'Can I safely travel from A to B right now?'"""
    if not API_KEY:
        return jsonify({"ok": False, "error": "OPENWEATHER_API_KEY is not configured."}), 400

    payload = request.get_json(silent=True) or request.form
    origin = (payload.get("origin") or "").strip()
    destination = (payload.get("destination") or "").strip()

    if not origin or not destination:
        return jsonify({"ok": False, "error": "Both a starting point and a destination are required."}), 400
    if len(origin) > 120 or len(destination) > 120:
        return jsonify({"ok": False, "error": "Location names are too long."}), 400

    try:
        result = assess_route_safety(origin, destination)
    except Exception as error:  # noqa: BLE001 — never let a routing edge case 500 the page
        print(f"Route safety assessment failed: {error}")
        return jsonify({"ok": False, "error": "Something went wrong assessing this route. Please try again."}), 500

    status_code = 200 if result.get("ok") else 400
    return jsonify(result), status_code


@app.route("/api/property-check", methods=["POST"])
def api_property_check():
    """Long-term flood vulnerability check for prospective buyers/renters —
    weather-independent, so this is useful even in the dry season."""
    if not API_KEY:
        return jsonify({"ok": False, "error": "OPENWEATHER_API_KEY is not configured."}), 400

    payload = request.get_json(silent=True) or request.form
    location = (payload.get("location") or "").strip()

    if not location:
        return jsonify({"ok": False, "error": "A location or address is required."}), 400
    if len(location) > 150:
        return jsonify({"ok": False, "error": "Location is too long."}), 400

    try:
        result = assess_property_flood_risk(location)
    except Exception as error:  # noqa: BLE001 — never let an edge case 500 the page
        print(f"Property flood risk assessment failed: {error}")
        return jsonify({"ok": False, "error": "Something went wrong checking this location. Please try again."}), 500

    status_code = 200 if result.get("ok") else 400
    return jsonify(result), status_code


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
            "earth_engine_enabled": bool(EARTH_ENGINE_ENABLED),
            "earth_engine_package_installed": ee is not None,
            "earth_engine_key_present": bool(GEE_PRIVATE_KEY_PATH and os.path.exists(GEE_PRIVATE_KEY_PATH)),
            "earth_engine_initialized": bool(_ee_initialized),
            "earth_engine_error": _ee_init_error,
        },
    }


if __name__ == "__main__":
    app.run(debug=True)
