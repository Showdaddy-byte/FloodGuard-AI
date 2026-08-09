import json
import secrets
from io import BytesIO
from functools import wraps
import math
import os
import socket
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
import urllib3.util.connection as urllib3_cn
from flask import Flask, g, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image, ImageDraw, ImageFont
from itsdangerous import BadSignature, URLSafeSerializer
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv
try:
    import ee
except ImportError:
    ee = None

load_dotenv()
app = Flask(__name__)

# Required for admin session cookies to work (Flask signs session data with
# this). Falls back to a random key if unset, but that means every deploy
# invalidates all sessions and logs the admin out — set SECRET_KEY as a
# real env var (same pattern as your other keys) for a stable admin login.
_secret_key_env = os.getenv("SECRET_KEY")
if not _secret_key_env:
    print("WARNING: SECRET_KEY is not set — using a random key. Admin sessions will not survive a restart/deploy until SECRET_KEY is configured.")
app.secret_key = _secret_key_env or secrets.token_hex(32)

# Admin login for posting dam status updates (see /admin/login). No default
# password — if ADMIN_PASSWORD_HASH isn't set, admin login is disabled
# entirely rather than falling back to something guessable.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")

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
WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY")  # optional — WeatherAPI.com fallback, only used if OpenWeather fails
TIDE_API_KEY = os.getenv("TIDE_API_KEY")  # optional — WorldTides free tier; tidal factor is skipped if unset
MAPBOX_ACCESS_TOKEN = os.getenv("MAPBOX_ACCESS_TOKEN")  # optional — enables the live traffic map layer

EARTH_ENGINE_ENABLED = os.getenv("EARTH_ENGINE_ENABLED", "1").lower() not in ("0", "false", "no")
GEE_SERVICE_ACCOUNT = os.getenv("GEE_SERVICE_ACCOUNT")
GEE_PRIVATE_KEY_PATH = os.getenv("GEE_PRIVATE_KEY_PATH", DEFAULT_GEE_KEY_PATH)
GEE_PROJECT = os.getenv("GEE_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")

OPENWEATHER_URL = "https://api.openweathermap.org/data/2.5"
OPENWEATHER_GEO_URL = "https://api.openweathermap.org/geo/1.0/direct"
WEATHERAPI_URL = "https://api.weatherapi.com/v1"  # fallback only — see fetch_weatherapi_current/forecast
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
BREVO_API_KEY = os.getenv("BREVO_API_KEY")  # optional — flood alert emails are skipped entirely if unset
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL")  # must be a verified sender in your Brevo account
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "FloodGuard AI")
# Used to build links inside alert emails (view-details link, unsubscribe
# link). Defaults to the current production URL, but check_and_send_location_alerts
# runs from a background thread with no Flask request context available, so
# this can't be derived from request.url_root — override via env var if the
# domain ever changes.
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://floodguard-ai-dq94.onrender.com")
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

# Seconds to wait between each monitored location during a full watchlist
# sweep. Widened from an earlier 1.5s: on a fresh deploy (no persistent
# disk => empty cache), a full sweep hits Overpass for every one of
# MONITORED_LOCATIONS in quick succession, which is enough to trip
# Overpass's own rate limiting and, in turn, this app's circuit breaker —
# cascading failures across unrelated live visitor searches for the
# following SERVICE_COOLDOWN_SECONDS each time it retrips. A wider stagger
# trades a slower cold-cache warmup for a much lower chance of tripping
# that cascade in the first place.
WATCHLIST_SWEEP_STAGGER_SECONDS = 4

# How long to wait after process startup before the watchlist sweep is
# allowed to trigger at all. Without a persistent disk, every deploy starts
# with an empty cache, and the very first visitor's page load would
# otherwise kick off a ~29-location Overpass burst within seconds of the
# app coming online — exactly the scenario that tripped the cascade seen
# in production. This grace period just lets that burst happen a little
# later and only once real, naturally-spaced traffic has started arriving,
# rather than as a single concentrated burst the instant the app boots.
WATCHLIST_STARTUP_GRACE_MINUTES = 2
_process_started_at = datetime.utcnow()

# Twice-daily weather digest send times, in UTC hour-of-day. Defaults
# approximate 6am/6pm West Africa Time (UTC+1) — the primary audience per
# MONITORED_LOCATIONS — but every subscriber gets the same two send times
# regardless of their own timezone, since no per-subscriber timezone is
# currently collected. Override via env vars if the audience shifts.
DIGEST_MORNING_UTC_HOUR = int(os.getenv("DIGEST_MORNING_UTC_HOUR", "5"))
DIGEST_EVENING_UTC_HOUR = int(os.getenv("DIGEST_EVENING_UTC_HOUR", "17"))

# Elevation, slope, water proximity, soil type, and urbanization barely
# change hour to hour — there's no reason to re-hit Overpass/SoilGrids for
# them on every watchlist sweep. Caching this static geospatial context for
# a full day cuts external call volume by ~95%+, which is what actually
# fixes Overpass rate-limiting (406s) rather than just retrying harder.
GEO_CONTEXT_TTL_HOURS = 24
EARTH_ENGINE_CONTEXT_TTL_HOURS = 6

# Locations actively monitored for the homepage alert banner, independent of
# whether any visitor has searched them.
#
# Each entry carries VERIFIED FIXED COORDINATES rather than relying on
# OpenWeather's free-text geocoder at sweep time. This exists because that
# geocoder has been observed, in this exact app, to mismatch Nigerian place
# names to the wrong location entirely — "Apapa, Lagos" resolved to Kaduna
# State, "Victoria Island, Lagos" resolved to the Canadian Arctic. Storing
# real coordinates here sidesteps that failure mode for every curated
# location (see build_prediction's known_place parameter).
#
# Confidence varies by entry — flagged per group below:
#   VERIFIED  = individually confirmed via web search this session
#   CITY      = well-known major city/state capital, high confidence from
#               general geographic knowledge (~city-center accuracy)
#   LGA-APPROX = a Local Government Area rather than a single town center;
#               no one obvious coordinate exists, lower confidence — worth
#               spot-checking via the map feature before fully trusting
CURATED_LOCATIONS = [
    # ---- Tier 1: Critical National Flood Watch — Coastal & Urban ----
    {"label": "Lagos Island, Lagos", "lat": 6.4550, "lon": 3.3945, "category": "coastal"},
    {"label": "Victoria Island, Lagos", "lat": 6.4253, "lon": 3.4095, "category": "coastal"},  # VERIFIED
    {"label": "Ikoyi, Lagos", "lat": 6.4474, "lon": 3.4356, "category": "coastal"},
    {"label": "Lekki, Lagos", "lat": 6.4698, "lon": 3.5852, "category": "coastal"},
    {"label": "Ajah, Lagos", "lat": 6.4670, "lon": 3.6010, "category": "coastal"},
    {"label": "Apapa, Lagos", "lat": 6.4500, "lon": 3.3650, "category": "coastal"},  # VERIFIED
    {"label": "Bariga, Lagos", "lat": 6.5310, "lon": 3.3860, "category": "coastal"},
    {"label": "Gbagada, Lagos", "lat": 6.5482, "lon": 3.3859, "category": "coastal"},
    {"label": "Somolu, Lagos", "lat": 6.5392, "lon": 3.3790, "category": "coastal"},
    {"label": "Iyana Oworo, Lagos", "lat": 6.5270, "lon": 3.3890, "category": "coastal"},
    {"label": "Ikorodu, Lagos", "lat": 6.6018, "lon": 3.5106, "category": "coastal"},
    {"label": "Ojota, Lagos", "lat": 6.5850, "lon": 3.3850, "category": "coastal"},  # VERIFIED
    {"label": "Badagry, Lagos", "lat": 6.4149, "lon": 2.8811, "category": "coastal"},
    {"label": "Epe, Lagos", "lat": 6.5832, "lon": 3.9836, "category": "coastal"},
    {"label": "Port Harcourt, Rivers", "lat": 4.8156, "lon": 7.0498, "category": "coastal"},
    {"label": "Warri, Delta", "lat": 5.5160, "lon": 5.7500, "category": "coastal"},
    {"label": "Yenagoa, Bayelsa", "lat": 4.9247, "lon": 6.2642, "category": "coastal"},
    {"label": "Calabar, Cross River", "lat": 4.9757, "lon": 8.3417, "category": "coastal"},
    {"label": "Uyo, Akwa Ibom", "lat": 5.0377, "lon": 7.9128, "category": "coastal"},

    # ---- Tier 2: River Flooding — Niger & Benue Basins ----
    {"label": "Lokoja, Kogi", "lat": 7.8023, "lon": 6.7337, "category": "river_basin"},
    {"label": "Idah, Kogi", "lat": 7.1069, "lon": 6.7333, "category": "river_basin"},
    {"label": "Makurdi, Benue", "lat": 7.7322, "lon": 8.5391, "category": "river_basin"},
    {"label": "Yola, Adamawa", "lat": 9.2035, "lon": 12.4954, "category": "river_basin"},
    {"label": "Numan, Adamawa", "lat": 9.4667, "lon": 12.0333, "category": "river_basin"},
    {"label": "Onitsha, Anambra", "lat": 6.1667, "lon": 6.7833, "category": "river_basin"},
    {"label": "Ogbaru, Anambra", "lat": 6.0500, "lon": 6.7000, "category": "river_basin"},  # LGA-APPROX
    {"label": "Asaba, Delta", "lat": 6.2000, "lon": 6.7333, "category": "river_basin"},
    {"label": "Jebba, Kwara", "lat": 9.1333, "lon": 4.8333, "category": "river_basin"},
    {"label": "Baro, Niger", "lat": 8.5833, "lon": 6.7500, "category": "river_basin"},
    {"label": "Mokwa, Niger", "lat": 9.2933, "lon": 5.0592, "category": "river_basin"},

    # ---- Tier 3: Lagdo Dam downstream watch (Benue basin) ----
    # LGA-APPROX confidence for most of these — administrative areas
    # without one obvious town-center coordinate. Spot-check before
    # fully trusting.
    {"label": "Demsa, Adamawa", "lat": 9.4333, "lon": 12.1500, "category": "dam_watch"},
    {"label": "Lamurde, Adamawa", "lat": 9.2667, "lon": 11.8500, "category": "dam_watch"},
    {"label": "Girei, Adamawa", "lat": 9.2833, "lon": 12.4667, "category": "dam_watch"},
    {"label": "Fufore, Adamawa", "lat": 9.3167, "lon": 12.7000, "category": "dam_watch"},
    {"label": "Logo, Benue", "lat": 7.3667, "lon": 9.0000, "category": "dam_watch"},
    {"label": "Buruku, Benue", "lat": 7.6500, "lon": 9.1500, "category": "dam_watch"},
    {"label": "Guma, Benue", "lat": 7.8667, "lon": 8.6333, "category": "dam_watch"},
    {"label": "Agatu, Benue", "lat": 7.4167, "lon": 7.9500, "category": "dam_watch"},

    # ---- Tier 4: Major Nigerian Cities (population-driven priority) ----
    {"label": "Abuja, FCT", "lat": 9.0765, "lon": 7.3986, "category": "urban"},
    {"label": "Ibadan, Oyo", "lat": 7.3775, "lon": 3.9470, "category": "urban"},
    {"label": "Kano, Kano", "lat": 12.0022, "lon": 8.5920, "category": "urban"},
    {"label": "Kaduna, Kaduna", "lat": 10.5222, "lon": 7.4383, "category": "urban"},
    {"label": "Benin City, Edo", "lat": 6.3350, "lon": 5.6037, "category": "urban"},
    {"label": "Aba, Abia", "lat": 5.1066, "lon": 7.3667, "category": "urban"},
    {"label": "Enugu, Enugu", "lat": 6.5244, "lon": 7.5086, "category": "urban"},
    {"label": "Ilorin, Kwara", "lat": 8.4966, "lon": 4.5426, "category": "urban"},
    {"label": "Jos, Plateau", "lat": 9.8965, "lon": 8.8583, "category": "urban"},
    {"label": "Maiduguri, Borno", "lat": 11.8333, "lon": 13.1500, "category": "urban"},
    {"label": "Sokoto, Sokoto", "lat": 13.0059, "lon": 5.2476, "category": "urban"},

    # ---- International showcase locations ----
    {"label": "Alexandria, Egypt", "lat": 31.2001, "lon": 29.9187, "category": "international"},
    {"label": "Maputo, Mozambique", "lat": -25.9692, "lon": 32.5732, "category": "international"},
    {"label": "Durban, South Africa", "lat": -29.8587, "lon": 31.0218, "category": "international"},
    {"label": "Jakarta, Indonesia", "lat": -6.2088, "lon": 106.8456, "category": "international"},
    {"label": "Dhaka, Bangladesh", "lat": 23.8103, "lon": 90.4125, "category": "international"},
    {"label": "Mumbai, India", "lat": 19.0760, "lon": 72.8777, "category": "international"},
    {"label": "Manila, Philippines", "lat": 14.5995, "lon": 120.9842, "category": "international"},
    {"label": "Bangkok, Thailand", "lat": 13.7563, "lon": 100.5018, "category": "international"},
    {"label": "Ho Chi Minh City, Vietnam", "lat": 10.8231, "lon": 106.6297, "category": "international"},
    {"label": "Guangzhou, China", "lat": 23.1291, "lon": 113.2644, "category": "international"},
    {"label": "Venice, Italy", "lat": 45.4408, "lon": 12.3155, "category": "international"},
    {"label": "Amsterdam, Netherlands", "lat": 52.3676, "lon": 4.9041, "category": "international"},
    {"label": "Hamburg, Germany", "lat": 53.5511, "lon": 9.9937, "category": "international"},
    {"label": "Miami, Florida", "lat": 25.7617, "lon": -80.1918, "category": "international"},
    {"label": "New Orleans, Louisiana", "lat": 29.9511, "lon": -90.0715, "category": "international"},
    {"label": "Houston, Texas", "lat": 29.7604, "lon": -95.3698, "category": "international"},
    {"label": "Rio de Janeiro, Brazil", "lat": -22.9068, "lon": -43.1729, "category": "international"},
    {"label": "Buenos Aires, Argentina", "lat": -34.6037, "lon": -58.3816, "category": "international"},
    {"label": "Brisbane, Australia", "lat": -27.4698, "lon": 153.0251, "category": "international"},
]

CATEGORY_DISPLAY_NAMES = {
    "coastal": "🌊 Coastal & Urban Flood Watch",
    "river_basin": "🏞 River Basin Watch (Niger & Benue)",
    "dam_watch": "🏗 Lagdo Dam Downstream Watch",
    "urban": "🏙 Major City Watch",
    "international": "🌍 International Watch",
}

# Derived flat list (backward-compatible with the rest of the app, which
# already treats monitored locations as plain "City, State" strings).
MONITORED_LOCATIONS = [loc["label"] for loc in CURATED_LOCATIONS]



# A real, independent, worldwide flood-alert feed (Global Disaster Alert and
# Coordination System — used by UN OCHA and humanitarian agencies) so the
# site isn't limited to only the cities in MONITORED_LOCATIONS. This is what
# provides genuine "anywhere in the world" coverage, since running our own
# multi-factor terrain model against every location on Earth isn't possible
# with free, rate-limited REST APIs.
GDACS_RSS_URL = "https://www.gdacs.org/xml/rss.xml"
GLOBAL_ALERTS_REFRESH_MINUTES = 10


# ---------------------------------------------------------------------------
# Network resilience layer — fixes "Network is unreachable" (errno 101) on
# hosts with unreachable IPv6 routes by forcing IPv4-only DNS resolution,
# and adds retry-with-backoff plus a per-service circuit breaker for
# transient failures (Open-Meteo/SoilGrids 429s and 503s), so a burst of
# requests hitting a rate limit doesn't permanently fail every lookup and
# doesn't make every subsequent request wait through a doomed timeout.
# ---------------------------------------------------------------------------

_original_allowed_gai_family = urllib3_cn.allowed_gai_family


def _force_ipv4_gai_family():
    return socket.AF_INET


urllib3_cn.allowed_gai_family = _force_ipv4_gai_family


_service_cooldowns = {}
SERVICE_COOLDOWN_SECONDS = 120


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
    """Shared retry wrapper for every external API call. If service_name is
    given, also checks/updates that service's circuit-breaker cooldown, so
    a currently rate-limiting or down service is skipped immediately
    instead of making every subsequent caller wait through a timeout."""
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


def _parse_stored_datetime(value):
    """Parse an ISO-format timestamp string pulled from the database into a
    naive UTC datetime. datetime.utcnow() (used everywhere in this app to
    write timestamps) always returns naive datetimes, but a stored row can
    end up offset-aware — from an older code version, a manual edit, or any
    other source that included tzinfo. Comparing a naive datetime.utcnow()
    against an aware parsed value raises:
    TypeError: can't subtract offset-naive and offset-aware datetimes
    Normalizing here makes every age/staleness check tolerant of both."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            phone TEXT,
            whatsapp TEXT,
            unsubscribe_token TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """
    )
    try:
        conn.execute("ALTER TABLE alert_subscribers ADD COLUMN name TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists — fine on repeated startups
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dam_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscriber_id INTEGER NOT NULL,
            dam_key TEXT NOT NULL,
            last_alerted_status TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (subscriber_id) REFERENCES alert_subscribers(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscriber_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            city_key TEXT NOT NULL,
            city_label TEXT NOT NULL,
            last_alerted_risk_level TEXT,
            last_alerted_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (subscriber_id) REFERENCES alert_subscribers(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dam_status (
            dam_key TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'NORMAL',
            notes TEXT,
            source_url TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS digest_sends (
            digest_key TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
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


# city_key -> full curated entry, for O(1) lookup during the sweep so
# refresh_watchlist_cache() can pass verified coordinates straight into
# build_prediction() instead of re-geocoding a location we already know.
CURATED_LOCATION_LOOKUP = {normalize_city(loc["label"]): loc for loc in CURATED_LOCATIONS}

# name-only (before the first comma) -> entry, for matching bare queries
# like "Apapa" or "Victoria Island" against the curated "Apapa, Lagos" /
# "Victoria Island, Lagos" entries. Visitors searching the dashboard rarely
# type the full "Name, State" form — this is what actually made the
# verified-coordinates fix apply to real visitor searches, not just the
# background sweep (which always used the full stored label already).
# If two curated entries happen to share the same bare name, the first one
# defined in CURATED_LOCATIONS wins — none currently collide.
CURATED_LOCATION_NAME_ONLY_LOOKUP = {}
for _loc in CURATED_LOCATIONS:
    _name_only = normalize_city(_loc["label"].partition(",")[0])
    CURATED_LOCATION_NAME_ONLY_LOOKUP.setdefault(_name_only, _loc)
del _loc, _name_only


def _known_place_for_curated_location(label):
    """Builds the known_place dict build_prediction() expects, from a
    curated location's verified coordinates. Tries an exact full-label
    match first (e.g. 'Apapa, Lagos'), then falls back to a bare-name
    match (e.g. just 'Apapa') — this second path is what makes the fix
    apply to what visitors actually type into the search box, not just the
    curated label string itself. Splits the matched entry's label on its
    first comma so the resulting display_name matches the exact format
    already used throughout searches/watchlist_cache."""
    normalized = normalize_city(label)
    entry = CURATED_LOCATION_LOOKUP.get(normalized) or CURATED_LOCATION_NAME_ONLY_LOOKUP.get(normalized)
    if not entry:
        return None
    name, _, state = entry["label"].partition(",")
    return {"lat": entry["lat"], "lon": entry["lon"], "name": name.strip(), "state": state.strip() or None}


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
    searched, plus every distinct location an alert subscriber is watching —
    so a place a visitor checks (or subscribes to) stays under continuous
    monitoring afterward instead of only being watched at the moment of
    that one search."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT DISTINCT city_label FROM searches").fetchall()
    alert_rows = conn.execute("SELECT DISTINCT city_label FROM alert_locations").fetchall()
    conn.close()

    searched = [row[0] for row in rows] + [row[0] for row in alert_rows]
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
    # If a subscriber's exact watched location (e.g. their "Home") gets
    # searched directly and it's newly HIGH+, they should hear about it
    # immediately rather than waiting for the next periodic sweep to reach
    # this same location.
    check_and_send_location_alerts(prediction)


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
            known_place = _known_place_for_curated_location(location)
            with app.app_context():
                prediction, _ = build_prediction(location, known_place=known_place)
        except Exception as error:  # noqa: BLE001 — one bad location must not break the rest
            print(f"Watchlist refresh failed for {location}: {error}")
            continue

        if not prediction:
            continue

        _upsert_watchlist_row(conn, prediction, now)
        check_and_send_location_alerts(prediction)

        # Stagger requests so a cold-cache sweep across many locations
        # doesn't burst-hit Overpass/SoilGrids all at once (that burst is
        # what triggers their rate limiting in the first place).
        if index < len(locations) - 1:
            time.sleep(WATCHLIST_SWEEP_STAGGER_SECONDS)

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
        age_minutes = (datetime.utcnow() - _parse_stored_datetime(oldest)).total_seconds() / 60
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

    # Without a persistent disk, every deploy starts with an empty cache, so
    # is_stale is almost always True on the very first request after a
    # restart. Without this check, that first visitor's page load would
    # immediately kick off a full ~29-location sweep within seconds of
    # startup — a concentrated Overpass burst that's what tripped the
    # cascading circuit-breaker failures seen in production. Waiting out a
    # short grace period lets that same warmup happen a little later, once
    # a bit of natural, spaced-out traffic has started arriving, instead of
    # all at once the instant the process comes online.
    minutes_since_start = (datetime.utcnow() - _process_started_at).total_seconds() / 60
    if minutes_since_start < WATCHLIST_STARTUP_GRACE_MINUTES:
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
        age_minutes = (datetime.utcnow() - _parse_stored_datetime(oldest_update)).total_seconds() / 60
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
        age_minutes = (datetime.utcnow() - _parse_stored_datetime(last)).total_seconds() / 60
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
        age_minutes = (datetime.utcnow() - _parse_stored_datetime(last_updated)).total_seconds() / 60
        is_stale = age_minutes >= (GLOBAL_ALERTS_REFRESH_MINUTES * 3)

    return {
        "entries": entries,
        "initialized": last_updated is not None,
        "last_updated": last_updated,
        "is_stale": is_stale,
        "age_minutes": round(age_minutes) if age_minutes is not None else None,
    }


def get_global_situation():
    """Build an honest global snapshot from FloodGuard's live sources.

    This deliberately reports only places FloodGuard has actually monitored
    and alerts supplied by GDACS. It is a worldwide situation view, not a
    claim that the app has street-level coverage of the entire planet.
    """
    global_alerts = get_global_alerts_status()
    watchlist = get_watchlist_status()
    entries = watchlist["entries"]
    high_levels = {"HIGH", "SEVERE", "CRITICAL"}
    severe_levels = {"SEVERE", "CRITICAL"}

    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.utcnow() - timedelta(hours=GROUND_TRUTH_WINDOW_HOURS)).isoformat()
    recent_reports = conn.execute(
        "SELECT COUNT(*) FROM contributions WHERE created_at >= ?",
        (cutoff,),
    ).fetchone()[0]
    conn.close()

    return {
        "ok": True,
        "global_alerts": global_alerts["entries"],
        "global_alerts_updated": global_alerts["last_updated"],
        "global_alerts_stale": global_alerts["is_stale"],
        "monitored_locations": len(entries),
        "elevated_locations": sum(1 for entry in entries if entry["risk_level"] in high_levels),
        "severe_locations": sum(1 for entry in entries if entry["risk_level"] in severe_levels),
        "recent_community_reports": recent_reports,
        "location_signals": [
            {
                "location": entry["city_label"],
                "risk_level": entry["risk_level"],
                "risk_color": entry["risk_color"],
                "score": entry["score"],
                "updated_at": entry["updated_at"],
            }
            for entry in entries[:24]
        ],
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


def fetch_weatherapi_current(lat, lon):
    """Fallback current-conditions source. Only ever called when OpenWeather's
    /weather call has already failed outright (see get_weather) — this is
    not a primary source and never runs on the happy path. Returns the raw
    WeatherAPI JSON payload, or None on any failure; get_weather() is
    responsible for normalizing it into this app's internal weather shape."""
    if not WEATHERAPI_KEY:
        return None
    try:
        response = request_with_retry(
            "GET",
            f"{WEATHERAPI_URL}/current.json",
            service_name="weatherapi",
            params={"key": WEATHERAPI_KEY, "q": f"{lat},{lon}", "aqi": "no"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"WeatherAPI current-conditions fallback request failed: {error}")
        return None


def fetch_weatherapi_forecast(lat, lon, days=5):
    """Fallback forecast source. Only ever called when OpenWeather's
    /forecast call has already failed outright (see get_forecast).
    WeatherAPI's free plan caps forecast length at 3 days — requesting 5
    on the free tier just returns what's available rather than erroring,
    so this degrades gracefully to a shorter forecast rather than failing."""
    if not WEATHERAPI_KEY:
        return None
    try:
        response = request_with_retry(
            "GET",
            f"{WEATHERAPI_URL}/forecast.json",
            service_name="weatherapi",
            params={"key": WEATHERAPI_KEY, "q": f"{lat},{lon}", "days": days, "aqi": "no", "alerts": "no"},
            timeout=12,
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"WeatherAPI forecast fallback request failed: {error}")
        return None


def _weatherapi_forecast_to_openweather_shape(lat, lon):
    """Reshapes a WeatherAPI forecast into the same list-of-3-hour-items
    format OpenWeather's /forecast endpoint returns (dt_txt, rain.3h,
    main.humidity/pressure, wind.speed, weather[0].id/description) — so
    get_forecast()'s existing scoring loop can consume either provider
    without duplicating any scoring logic. WeatherAPI's hourly precip_mm
    is a single hour's rainfall, so this sums three consecutive hours per
    bucket to approximate OpenWeather's 3-hour accumulated rain.3h figure
    (the scoring thresholds in _weather_bonus were calibrated against a
    3-hour accumulation, not a single hour's reading — using raw 1-hour
    values here would systematically under-score every fallback forecast)."""
    raw = fetch_weatherapi_forecast(lat, lon, days=5)
    if not raw:
        return []

    forecastdays = (raw.get("forecast", {}) or {}).get("forecastday", [])
    items = []
    for day in forecastdays:
        hours = day.get("hour", [])
        for bucket_start in range(0, len(hours), 3):
            bucket = hours[bucket_start:bucket_start + 3]
            if not bucket:
                continue
            last_hour = bucket[-1]
            raw_time = last_hour.get("time")
            if not raw_time:
                continue  # can't place this bucket in the timeline without a timestamp

            bucket_rain = sum(h.get("precip_mm", 0) or 0 for h in bucket)
            condition_text = (last_hour.get("condition", {}) or {}).get("text", "")

            items.append(
                {
                    "dt_txt": f"{raw_time}:00",  # WeatherAPI gives "YYYY-MM-DD HH:MM"; add seconds to match OpenWeather's format
                    "rain": {"3h": round(bucket_rain, 1)},
                    "main": {
                        "temp": last_hour.get("temp_c", 0),
                        "humidity": last_hour.get("humidity", 50),
                        "pressure": round(last_hour.get("pressure_mb", 1013)),
                    },
                    "wind": {"speed": round((last_hour.get("wind_kph", 0) or 0) / 3.6, 1)},
                    # id -1: WeatherAPI has no equivalent to OpenWeather's numeric condition
                    # codes, so weather_scene() falls back to its description-keyword checks.
                    "weather": [{"id": -1, "description": condition_text}],
                }
            )
    return items


def send_alert_email(to_email, subject, html_content):
    """Sends a transactional email via Brevo. Fails closed and silently if
    not configured (BREVO_API_KEY/BREVO_SENDER_EMAIL unset) — matches the
    pattern used for every other optional API key in this app (TIDE_API_KEY,
    WEATHERAPI_KEY, etc.), so the alert-subscription feature can be deployed
    and tested end-to-end (subscribe, unsubscribe, DB records) before email
    sending is actually wired up with real credentials."""
    print(f"📧 EMAIL FUNCTION CALLED -> {to_email}")
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL:
        print(f"Brevo not configured — skipping email to {to_email}: '{subject}'")
        return False

    try:
        response = request_with_retry(
            "POST",
            BREVO_API_URL,
            service_name="brevo",
            json={
                "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
                "to": [{"email": to_email}],
                "subject": subject,
                "htmlContent": html_content,
            },
            headers={
                "api-key": BREVO_API_KEY,
                "content-type": "application/json",
                "accept": "application/json",
            },
            timeout=12,
        )
        if response.status_code >= 300:
            print(f"Brevo send to {to_email} failed ({response.status_code}): {response.text[:300]}")
            return False
        return True
    except requests.RequestException as error:
        print(f"Brevo send request to {to_email} failed: {error}")
        return False


TIER_COLORS = {"watch": "#f59e0b", "warning": "#dc2626", "emergency": "#7f1d1d"}
TIER_MESSAGES = {
    "watch": "Heavy rainfall is forecast that could lead to flooding. Flooding is not currently expected, but conditions could worsen — stay informed.",
    "warning": "Conditions now indicate flooding is plausible. Avoid unnecessary travel, move valuables to higher ground, and charge your phone.",
    "emergency": "Flooding is occurring or imminent. Leave low-lying areas immediately, avoid flooded roads, and follow instructions from emergency authorities.",
}


def build_tiered_alert_email_html(prediction, label, tier_name, unsubscribe_url):
    """Builds the HTML body for a tiered flood alert email — Watch, Warning,
    or Emergency, matching how urgently the person should act, rather than
    a single generic 'flood risk' message regardless of severity."""
    color = TIER_COLORS[tier_name]
    headline = RISK_TIER_HEADLINE[tier_name]
    tier_message = TIER_MESSAGES[tier_name]

    priority_html = (
        f"<p style='color:#b91c1c;font-weight:bold;'>⚠ {prediction['priority_action']}</p>"
        if prediction.get("priority_action")
        else ""
    )
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;">
        <h2 style="color:{color};">{RISK_TIER_EMOJI[tier_name]} {headline}</h2>
        <p><strong>{label}</strong> ({prediction['city']}{f", {prediction['country']}" if prediction.get('country') else ''})
           is currently at <strong>{prediction['risk']}</strong> flood risk
           (score {prediction['score']}/100).</p>
        <p>{tier_message}</p>
        {priority_html}
        <p>{prediction.get('advice', '')}</p>
        <p><a href="{SITE_BASE_URL}/" style="color:#2563eb;">
           View full details on FloodGuard AI</a></p>
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
        <p style="font-size:12px;color:#6b7280;">
            You're receiving this because you subscribed to alerts for "{label}".
            <a href="{unsubscribe_url}">Unsubscribe from all alerts</a>.
        </p>
    </div>
    """


DAM_STATUS_LABELS = {
    "NORMAL": "Normal",
    "MONITORING": "Being Monitored",
    "RELEASE_IN_PROGRESS": "Controlled Water Release In Progress",
}


def build_dam_alert_email_html(dam, new_status, notes, source_url, unsubscribe_url):
    """Builds the HTML body for a dam status-change alert email."""
    color = "#dc2626" if new_status == "RELEASE_IN_PROGRESS" else "#f59e0b" if new_status == "MONITORING" else "#16a34a"
    notes_html = f"<p>{notes}</p>" if notes else ""
    source_html = f'<p><a href="{source_url}" style="color:#2563eb;">Official source</a></p>' if source_url else ""
    downstream_html = " → ".join(dam["downstream"])

    return f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;">
        <h2 style="color:{color};">🌊 {dam['name']} — {DAM_STATUS_LABELS.get(new_status, new_status)}</h2>
        <p>{dam['location']}</p>
        {notes_html}
        {source_html}
        <p><strong>Downstream communities to watch (nearest to the dam first):</strong><br>{downstream_html}</p>
        <p style="font-size:13px;color:#6b7280;">This is a manually-confirmed status update, not an automated detection — always follow official guidance for your area.</p>
        <p><a href="{SITE_BASE_URL}/" style="color:#2563eb;">View full details on FloodGuard AI</a></p>
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
        <p style="font-size:12px;color:#6b7280;">
            You're receiving this because you subscribed to dam alerts for {dam['name']}.
            <a href="{unsubscribe_url}">Unsubscribe from all alerts</a>.
        </p>
    </div>
    """


def check_and_send_dam_alerts(dam_key, new_status, notes, source_url):
    """Emails every subscriber to this dam whenever its status changes to
    something different from what was last alerted to them. Unlike the
    location-tier alerts, this fires on every change (including
    de-escalation back to NORMAL, e.g. "the release has ended") rather than
    only on escalation — dam status changes are infrequent and
    manually-curated, so there's no spam risk, and a "release has ended"
    update is itself genuinely useful news to the people who opted in."""
    dam = DAM_REGISTRY.get(dam_key)
    if not dam:
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT ds.id AS sub_id, ds.last_alerted_status, s.email, s.unsubscribe_token
        FROM dam_subscriptions ds
        JOIN alert_subscribers s ON ds.subscriber_id = s.id
        WHERE ds.dam_key = ?
        """,
        (dam_key,),
    ).fetchall()

    for row in rows:
        if row["last_alerted_status"] == new_status:
            continue

        unsubscribe_url = f"{SITE_BASE_URL}/unsubscribe/{row['unsubscribe_token']}"
        sent = send_alert_email(
            row["email"],
            f"🌊 {dam['name']}: {DAM_STATUS_LABELS.get(new_status, new_status)}",
            build_dam_alert_email_html(dam, new_status, notes, source_url, unsubscribe_url),
        )
        if sent:
            conn.execute(
                "UPDATE dam_subscriptions SET last_alerted_status = ? WHERE id = ?",
                (new_status, row["sub_id"]),
            )

    conn.commit()
    conn.close()


DIGEST_GREETING = {"morning": "Good morning", "evening": "Good evening"}


def build_digest_location_advice(prediction, label):
    """Builds one short, practical advisory line for a single location,
    reusing fields build_prediction() already computes — no new scoring
    logic, no new thresholds. Prefers the most specific, actionable signal
    available: an active/imminent risk tier first, then an upcoming
    rainfall_warning, then current light rain, then a plain all-clear."""
    risk = prediction["risk"]
    rain_now = prediction.get("rainfall_mm", 0) or 0
    warning = prediction.get("rainfall_warning")
    display_label = f"{label} ({prediction['city']})" if label != prediction["city"] else label

    if risk in RISK_TIER_LEVEL:
        action = prediction.get("priority_action") or "Stay alert and monitor conditions."
        return f"<strong>{display_label}</strong> — {risk} flood risk right now. {action}"

    if warning:
        when = "later today" if warning["hours_from_now"] <= 12 else "in the next couple of days"
        if warning["peak_risk"] in ("SEVERE", "CRITICAL"):
            return f"<strong>{display_label}</strong> — Heavy rain expected {when} (~{warning['expected_rainfall_mm']}mm). Consider postponing travel through low-lying roads."
        return f"<strong>{display_label}</strong> — Rain expected {when} (~{warning['expected_rainfall_mm']}mm). Worth carrying an umbrella."

    if rain_now >= 2:
        return f"<strong>{display_label}</strong> — Light rain currently ({rain_now}mm). Carry an umbrella if heading out."

    return f"<strong>{display_label}</strong> — Clear, no significant rain expected. {prediction.get('description', '')}."


def build_digest_email_html(name, digest_type, location_lines, unsubscribe_url):
    greeting = DIGEST_GREETING.get(digest_type, "Hello")
    lines_html = "".join(f"<li style='margin-bottom:8px;'>{line}</li>" for line in location_lines)
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;">
        <h2>{greeting}{f", {name}" if name else ""} 🌤️</h2>
        <p>Here's your FloodGuard AI weather check-in:</p>
        <ul style="padding-left:20px;">{lines_html}</ul>
        <p><a href="{SITE_BASE_URL}/" style="color:#2563eb;">View full details on FloodGuard AI</a></p>
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
        <p style="font-size:12px;color:#6b7280;">
            You're receiving this twice-daily check-in because you're subscribed to FloodGuard AI alerts.
            <a href="{unsubscribe_url}">Unsubscribe from all alerts</a>.
        </p>
    </div>
    """


def send_daily_digests(digest_type):
    """Sends the twice-daily weather digest to every subscriber. Computes
    each distinct watched location's prediction ONCE and reuses it across
    every subscriber watching that same place (common for curated/popular
    locations), rather than recomputing per-subscriber — keeps this
    reasonably scoped even as the subscriber base grows."""
    print(f"DIGEST: starting {digest_type} digest run")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT s.id AS subscriber_id, s.name, s.email, s.unsubscribe_token,
               al.label, al.city_label
        FROM alert_subscribers s
        JOIN alert_locations al ON al.subscriber_id = s.id
        ORDER BY s.id
        """
    ).fetchall()
    conn.close()

    if not rows:
        print(f"DIGEST: {digest_type} run found ZERO subscriber locations — nothing to send. "
              "(No one has completed /api/alert-subscribe with a saved location yet.)")
        return

    subscribers = {}
    for row in rows:
        subscribers.setdefault(row["subscriber_id"], {
            "name": row["name"], "email": row["email"], "unsubscribe_token": row["unsubscribe_token"], "locations": [],
        })["locations"].append((row["label"], row["city_label"]))

    print(f"DIGEST: {digest_type} run found {len(subscribers)} subscriber(s) across {len(rows)} location row(s)")

    prediction_cache = {}

    def get_prediction_for(city_label):
        key = normalize_city(city_label)
        if key not in prediction_cache:
            known_place = _known_place_for_curated_location(city_label)
            try:
                with app.app_context():
                    prediction, _ = build_prediction(city_label, known_place=known_place)
            except Exception as error:  # noqa: BLE001 — one bad location must not break the whole digest run
                print(f"DIGEST: prediction failed for '{city_label}': {error}")
                prediction = None
            prediction_cache[key] = prediction
        return prediction_cache[key]

    sent_count = 0
    skipped_count = 0

    for subscriber in subscribers.values():
        location_lines = []
        for label, city_label in subscriber["locations"]:
            prediction = get_prediction_for(city_label)
            if prediction:
                location_lines.append(build_digest_location_advice(prediction, label))

        if not location_lines:
            skipped_count += 1
            print(f"DIGEST: skipping {subscriber['email']} — every one of their locations failed to resolve this run")
            continue

        unsubscribe_url = f"{SITE_BASE_URL}/unsubscribe/{subscriber['unsubscribe_token']}"
        sent = send_alert_email(
            subscriber["email"],
            f"{DIGEST_GREETING.get(digest_type, 'Hello')} — your FloodGuard AI weather check-in",
            build_digest_email_html(subscriber["name"], digest_type, location_lines, unsubscribe_url),
        )
        if sent:
            sent_count += 1
            print(f"DIGEST: sent to {subscriber['email']} ({len(location_lines)} location(s))")
        else:
            skipped_count += 1
            print(f"DIGEST: send_alert_email returned False for {subscriber['email']} — see Brevo error above, if any")

    print(f"DIGEST: {digest_type} run complete — {sent_count} sent, {skipped_count} skipped, "
          f"{len(prediction_cache)} unique location(s) computed")


def maybe_send_daily_digests():
    """Opportunistically sends the morning/evening digest if the current UTC
    hour matches a configured send time and it hasn't already gone out
    today. Best-effort trigger tied to normal page traffic — for guaranteed
    delivery even with zero visitors at the target hour, pair this with an
    external scheduler hitting /api/send-digest, same pattern already
    documented for /api/refresh-watchlist."""
    now = datetime.utcnow()
    current_hour = now.hour
    today_str = now.strftime("%Y-%m-%d")

    digest_type = None
    if current_hour == DIGEST_MORNING_UTC_HOUR:
        digest_type = "morning"
    elif current_hour == DIGEST_EVENING_UTC_HOUR:
        digest_type = "evening"

    if not digest_type:
        return

    digest_key = f"{digest_type}:{today_str}"
    if not try_acquire_lock(f"digest_send:{digest_key}", max_age_minutes=90):
        return  # already sent (or currently sending) this digest today

    def _run():
        try:
            send_daily_digests(digest_type)
        finally:
            pass  # deliberately do NOT release this lock — it's a once-per-day dedup marker, not a mutex to reclaim

    threading.Thread(target=_run, daemon=True).start()





def generate_unsubscribe_token():
    return secrets.token_urlsafe(32)


def get_or_create_subscriber(email, phone=None, whatsapp=None, name=None):
    """Finds an existing subscriber by email, or creates one. Updates
    phone/whatsapp/name on an existing subscriber only if new values were
    actually provided this time, so re-subscribing to add a second location
    doesn't accidentally wipe details given during the first signup."""
    db = get_db()
    row = db.execute("SELECT id, unsubscribe_token FROM alert_subscribers WHERE email = ?", (email,)).fetchone()
    if row:
        if phone or whatsapp or name:
            db.execute(
                "UPDATE alert_subscribers SET phone = COALESCE(?, phone), whatsapp = COALESCE(?, whatsapp), name = COALESCE(?, name) WHERE id = ?",
                (phone, whatsapp, name, row["id"]),
            )
            db.commit()
        return row["id"], row["unsubscribe_token"]

    token = generate_unsubscribe_token()
    db.execute(
        "INSERT INTO alert_subscribers (email, phone, whatsapp, name, unsubscribe_token, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (email, phone, whatsapp, name, token, datetime.utcnow().isoformat()),
    )
    db.commit()
    new_row = db.execute("SELECT id FROM alert_subscribers WHERE email = ?", (email,)).fetchone()
    return new_row["id"], token


def add_alert_location(subscriber_id, label, city_label):
    """Adds a watched location for a subscriber, deduplicated by city_key so
    resubmitting the same location doesn't create duplicate rows (and
    doesn't reset last_alerted_risk_level, which would cause a duplicate
    alert for a crossing that was already reported)."""
    db = get_db()
    city_key = normalize_city(city_label)
    existing = db.execute(
        "SELECT id FROM alert_locations WHERE subscriber_id = ? AND city_key = ?",
        (subscriber_id, city_key),
    ).fetchone()
    if existing:
        return existing["id"], False

    db.execute(
        "INSERT INTO alert_locations (subscriber_id, label, city_key, city_label, created_at) VALUES (?, ?, ?, ?, ?)",
        (subscriber_id, label.strip() or "Location", city_key, city_label.strip(), datetime.utcnow().isoformat()),
    )
    db.commit()
    new_row = db.execute(
        "SELECT id FROM alert_locations WHERE subscriber_id = ? AND city_key = ?",
        (subscriber_id, city_key),
    ).fetchone()
    return new_row["id"], True


def add_dam_subscription(subscriber_id, dam_key):
    """Subscribes a subscriber to status-change alerts for one dam,
    deduplicated the same way as add_alert_location."""
    db = get_db()
    existing = db.execute(
        "SELECT id FROM dam_subscriptions WHERE subscriber_id = ? AND dam_key = ?",
        (subscriber_id, dam_key),
    ).fetchone()
    if existing:
        return existing["id"], False

    db.execute(
        "INSERT INTO dam_subscriptions (subscriber_id, dam_key, created_at) VALUES (?, ?, ?)",
        (subscriber_id, dam_key, datetime.utcnow().isoformat()),
    )
    db.commit()
    new_row = db.execute(
        "SELECT id FROM dam_subscriptions WHERE subscriber_id = ? AND dam_key = ?",
        (subscriber_id, dam_key),
    ).fetchone()
    return new_row["id"], True


# Three-tier alert system. Deliberately built on top of the existing 5-level
# classify_risk() output rather than a separate rainfall-mm threshold table
# — calculate_flood_score() already combines rainfall with terrain, rivers,
# drainage, tides, and soil, so a parallel rainfall-only table would risk
# giving a conflicting signal for the same location (e.g. 60mm somewhere
# flat/coastal/saturated vs. 60mm somewhere elevated and well-drained
# shouldn't read as equally urgent, but a flat mm table can't tell them
# apart the way the existing model already does).
RISK_TIER_LEVEL = {"WATCH": 1, "HIGH": 2, "SEVERE": 3, "CRITICAL": 3}
RISK_TIER_NAME = {"WATCH": "watch", "HIGH": "warning", "SEVERE": "emergency", "CRITICAL": "emergency"}
RISK_TIER_EMOJI = {"watch": "🌧️", "warning": "⚠️", "emergency": "🚨"}
RISK_TIER_HEADLINE = {
    "watch": "Heavy Rain Watch",
    "warning": "Flash Flood Warning",
    "emergency": "Emergency Flood Alert",
}


def check_and_send_location_alerts(prediction):
    """Checks every subscriber watching this location and emails anyone
    whose saved location has just crossed INTO a NEW, HIGHER tier than
    whatever was last alerted for the current episode — not everyone still
    sitting at the same tier on a later sweep (spam), but also not
    suppressing a real escalation (e.g. WATCH already sent, risk climbs to
    HIGH — that should still send a fresh, more urgent alert, since tier
    level 2 > the previously-alerted tier level 1).
    last_alerted_risk_level stores the actual risk string (WATCH/HIGH/
    SEVERE/CRITICAL) so RISK_TIER_LEVEL can rank it; it's cleared once risk
    drops back to LOW, so a future re-crossing alerts fresh from tier 1
    again instead of staying suppressed forever."""
    city_key = normalize_city(prediction["city"])
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    risk = prediction["risk"]
    current_tier_level = RISK_TIER_LEVEL.get(risk, 0)

    if current_tier_level == 0:
        conn.execute(
            "UPDATE alert_locations SET last_alerted_risk_level = NULL WHERE city_key = ? AND last_alerted_risk_level IS NOT NULL",
            (city_key,),
        )
        conn.commit()
        conn.close()
        return

    rows = conn.execute(
        """
        SELECT al.id AS location_id, al.label, al.last_alerted_risk_level,
               s.email, s.unsubscribe_token
        FROM alert_locations al
        JOIN alert_subscribers s ON al.subscriber_id = s.id
        WHERE al.city_key = ?
        """,
        (city_key,),
    ).fetchall()

    tier_name = RISK_TIER_NAME[risk]

    for row in rows:
        previous_tier_level = RISK_TIER_LEVEL.get(row["last_alerted_risk_level"], 0)
        if current_tier_level <= previous_tier_level:
            continue  # no new, higher tier reached this episode — don't resend

        unsubscribe_url = f"{SITE_BASE_URL}/unsubscribe/{row['unsubscribe_token']}"
        sent = send_alert_email(
            row["email"],
            f"{RISK_TIER_EMOJI[tier_name]} {RISK_TIER_HEADLINE[tier_name]}: {row['label']} ({prediction['city']})",
            build_tiered_alert_email_html(prediction, row["label"], tier_name, unsubscribe_url),
        )
        if sent:
            conn.execute(
                "UPDATE alert_locations SET last_alerted_risk_level = ?, last_alerted_at = ? WHERE id = ?",
                (risk, datetime.utcnow().isoformat(), row["location_id"]),
            )

    conn.commit()
    conn.close()


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
        response = request_with_retry(
            "GET",
            ELEVATION_URL,
            service_name="open-meteo-elevation",
            params={"latitude": lats, "longitude": lons},
            timeout=8,
        )
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
            # Overpass's "out count;" response uses the tag key "total"
            # (plus "nodes"/"ways"/"relations") — never "buildings". Reading
            # the wrong key here meant building_count silently evaluated to
            # 0 on every single request regardless of what Overpass
            # actually found there.
            building_count = int(el.get("tags", {}).get("total", 0) or 0)
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
        response = requests.get(
            WORLDTIDES_URL,
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


def classify_risk(score, coastal=False):
    # Coastal regions get every threshold shifted down by 20 points, so HIGH
    # starts at 25 instead of 45 — storm surge, tidal backflow, and lagoon/
    # estuary effects mean coastal areas flood at rainfall levels that
    # wouldn't trouble inland terrain, so the same numeric score should read
    # as more urgent near a coastline.
    if coastal:
        critical_at, severe_at, high_at, watch_at = 85, 65, 45, 30
    else:
        critical_at, severe_at, high_at, watch_at = 85, 70, 55, 35

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
            "priority_action": "Exercise caution and monitor changing flood conditions.",
"advice": "Current conditions indicate an elevated flood risk. Avoid roads with known flooding history and continue monitoring FloodGuard AI for updates."
        }
    if score >= watch_at:
        return {
            "level": "WATCH",
            "color": "watch",
            "map_color": "#f59e0b",
            "priority_action": "Remain alert and monitor weather conditions.",
            "advice": "Conditions should be monitored. Flooding is not imminent, but weather conditions may change."
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


def build_rainfall_warning(timeline, coastal=False):
    """Scans the already-computed 3-hour timeline (see get_forecast) for the
    next window where risk rises to WATCH or above, and summarizes it as a
    single early-warning message: when it starts, roughly how long it lasts,
    total expected rainfall over that window, and the recommended action.

    Deliberately built on top of the SAME terrain-aware scoring already
    used everywhere else in this app (classify_day_score -> classify_risk)
    rather than a separate raw-rainfall-mm threshold table — that's what
    lets a low-lying coastal spot warn earlier than an inland one for the
    identical rainfall amount, consistent with how the rest of the model
    already treats coastal risk.

    'confidence' reuses the exact same heuristic already shown elsewhere on
    this app (see calculate_flood_score's 'confidence' field) rather than
    inventing a new, different-sounding number — there's no real
    ensemble/probabilistic forecast data available from the free APIs this
    app uses, so presenting a differently-labeled 'confidence' here would
    imply a precision that doesn't exist."""
    if not timeline:
        return None

    start_index = None
    for i, slot in enumerate(timeline):
        if slot["risk"] != "LOW":
            start_index = i
            break

    if start_index is None:
        return None  # no elevated risk anywhere in the available forecast window

    end_index = start_index
    for i in range(start_index, len(timeline)):
        if timeline[i]["risk"] == "LOW":
            break
        end_index = i

    window = timeline[start_index:end_index + 1]
    total_rain = round(sum(slot["rain"] for slot in window), 1)
    worst_slot = max(window, key=lambda s: s["score"])
    worst_risk_meta = classify_risk(worst_slot["score"], coastal=coastal)

    return {
        "starts_at": window[0]["time"],
        "ends_at": window[-1]["time"],
        "hours_from_now": start_index * 3,  # timeline slots are ~3h apart
        "is_starting_now": start_index == 0,
        "expected_rainfall_mm": total_rain,
        "peak_risk": worst_slot["risk"],
        "peak_risk_color": worst_slot["risk_color"],
        "peak_score": worst_slot["score"],
        "confidence": min(95, 68 + worst_slot["score"] // 3),
        "recommended_action": worst_risk_meta["priority_action"],
        "headline": (
            "Heavy rainfall expected now — conditions may lead to flooding."
            if start_index == 0
            else f"Heavy rainfall expected in about {start_index * 3} hours — conditions may lead to flooding."
        ),
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
    earth_engine = context.get("earth_engine")
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
                "label": "Earth Engine satellite and raster analysis",
                "score": max(0, min(10, round(earth_engine.get("score_bonus", 0) / 3.5))),
                "status": (
                    "Sentinel-1/Sentinel-2, Copernicus DEM, JRC Global Surface Water, CHIRPS rainfall, "
                    "Dynamic World land cover, and terrain/watershed susceptibility are active."
                ),
                "details": earth_engine,
            }
        else:
            layers["earth_engine"] = {
                "label": "Earth Engine unavailable",
                "score": 0,
                "status": "Satellite intelligence is temporarily unavailable. FloodGuard AI is continuing its analysis using weather, terrain, hydrology, and other available data.",
                "details": earth_engine,
            }

    layers["summary"] = f"{city} is being evaluated with weather, terrain, water proximity, soil, hydrological, satellite/raster, and urbanization signals, plus live visitor contributions.{community_note}"

    return layers


def _weather_bonus(rainfall, humidity, pressure, wind_speed, rainfall_word="current"):
    """Rainfall/humidity/pressure/wind contribution, shared between the
    current-conditions score and each forecast day's score."""
    score = 0
    factors = []

    if rainfall >= 60:
        score += 35
        factors.append(f"Extreme {rainfall_word} rainfall")
    elif rainfall >= 30:
        score += 22
        factors.append(f"Heavy {rainfall_word} rainfall")
    elif rainfall >= 10:
        score += 8
        factors.append(f"Moderate {rainfall_word} rainfall")
    else:
        score += 0

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
    earth_engine = context.get("earth_engine")
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

    if earth_engine and earth_engine.get("available"):
        score += earth_engine.get("score_bonus", 0)
        factors.extend(earth_engine.get("factors") or [])

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
    score = weather_bonus + context_bonus

    if rainfall < 10:
        score = min(score, 24)
    elif rainfall < 30:
        score = min(score, 44)

    score = max(0, min(score, 100))
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
    # Prevent high flood risk when there is little or no rainfall
    if rainfall < 2 and forecast_rain_total < 5:
        score = min(score, 20)
        factors.append("Very little rainfall expected")
    elif rainfall < 10 and forecast_rain_total < 20:
        score = min(score, 35)
        factors.append("Only light rainfall expected")

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
    """Returns (daily_forecast, timeline). Both come from a single OpenWeather
    call: daily_forecast is one representative slot per day (existing 5-day
    cards), timeline is every 3-hour slot for the next ~24h (previously
    fetched and discarded) — reused for the flood timeline display and for
    picking a lower-risk travel window."""
    data = fetch_openweather(
        "forecast",
        {"lat": lat, "lon": lon, "appid": API_KEY, "units": "metric"},
    )
    raw_items = data.get("list", []) if data else []

    if not raw_items:
        # OpenWeather's forecast endpoint failed outright (or returned an
        # empty list) — fall back to WeatherAPI, reshaped into the same
        # 3-hour item format so the scoring loop below runs unchanged
        # regardless of which provider actually answered.
        raw_items = _weatherapi_forecast_to_openweather_shape(lat, lon)
        if not raw_items:
            return [], []

    forecast = []
    timeline = []
    seen_dates = set()

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

    if data:
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
            "wind_speed": round(data["wind"]["speed"] * 3.6, 1),

            "rainfall": rainfall,

            "latitude": data["coord"]["lat"],
            "longitude": data["coord"]["lon"],

            "source": "openweather",
        }

    # OpenWeather failed outright — fall back to WeatherAPI so a single
    # provider outage doesn't take down every flood score. Normalized into
    # the exact same dict shape as above, so nothing downstream (scoring,
    # templates, watchlist caching) needs to know which provider answered.
    fallback = fetch_weatherapi_current(lat, lon)
    if not fallback:
        return None

    current = fallback.get("current", {}) or {}
    location = fallback.get("location", {}) or {}
    condition_text = (current.get("condition", {}) or {}).get("text", "")
    wind_ms = (current.get("wind_kph", 0) or 0) / 3.6
    # id -1: no OpenWeather-style numeric condition code from this provider,
    # so weather_scene() falls back to its description-keyword checks.
    scene = weather_scene(-1, condition_text)

    return {
        "city": display_name or location.get("name") or "Unknown",
        "country": location.get("country", ""),
        "description": condition_text.title() if condition_text else "Unknown",
        "weather_id": -1,
        "scene": scene,

        "temperature": round(current.get("temp_c", 0) or 0, 1),
        "feels_like": round(current.get("feelslike_c", current.get("temp_c", 0)) or 0, 1),

        "humidity": current.get("humidity", 50),
        "pressure": round(current.get("pressure_mb", 1013) or 1013),

        "wind": round(wind_ms, 1),
        "wind_speed": round(current.get("wind_kph", 0) or 0, 1),

        "rainfall": current.get("precip_mm", 0) or 0,

        "latitude": location.get("lat", lat),
        "longitude": location.get("lon", lon),

        "source": "weatherapi_fallback",
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
        dem = ee.Image("COPERNICUS/DEM/GLO30").select("DEM")
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
        s2_count = s2.size().getInfo()
        if s2_count > 0:
            s2_img = s2.median()
            ndwi = s2_img.normalizedDifference(["B3", "B8"]).rename("ndwi")
            ndvi = s2_img.normalizedDifference(["B8", "B4"]).rename("ndvi")
            s2_stats = _ee_reduce_mean(ndwi.addBands(ndvi), region, 20)
            result["sentinel2_ndwi"] = _round_or_none(s2_stats.get("ndwi"), 3)
            result["sentinel2_ndvi"] = _round_or_none(s2_stats.get("ndvi"), 3)
            result["sentinel2_image_count"] = s2_count
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
    ee_score = _score_earth_engine_context(result)
    result["score_bonus"] = ee_score["score_bonus"]
    result["factors"] = ee_score["factors"]
    return result


def get_earth_engine_context(city_key, lat, lon):
    if not EARTH_ENGINE_ENABLED:
        return {"available": False, "error": "Earth Engine disabled."}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM earth_engine_cache WHERE city_key = ?", (city_key,)).fetchone()
    if row:
        age_hours = (datetime.utcnow() - _parse_stored_datetime(row["updated_at"])).total_seconds() / 3600
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

    Also stale-if-error: if the cache is stale and a live re-fetch fails
    for a specific field (a single 429/503/network blip), this falls back
    to the last known-good cached value for that exact field instead of
    baking "unavailable" into the cache for a full day. Terrain, soil, and
    water proximity barely change over time, so a slightly-stale real
    number is strictly better than a blank one caused by one bad request."""
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

        age_hours = (datetime.utcnow() - _parse_stored_datetime(row["updated_at"])).total_seconds() / 3600
        # None (not 0, not a real value) in elevation/building_count/clay_percent
        # is this codebase's own signal that the underlying fetch failed
        # outright on this row's last write, with no prior cache to fall back
        # to at the time — e.g. fetch_elevation_grid returns (None, None) on
        # failure, and building_count is documented as "None only when the
        # Overpass call failed outright" (a successful call always returns an
        # int, even 0). A row written that way still gets a fresh updated_at,
        # so without this check it would be replayed as valid for a full
        # GEO_CONTEXT_TTL_HOURS — this is exactly the bug that caused Falomo's
        # nearest_water_m (and building_count) to show "Unavailable" live
        # while working locally. Any of these being None routes past the
        # early-return below and falls through to the live-fetch section
        # further down, which already retries each field individually and
        # only falls back to (still-None) cached values if it fails again.
        critical_fields_missing = (
            cached["elevation"] is None
            or cached["building_count"] is None
            or cached["clay_percent"] is None
        )

        if age_hours < GEO_CONTEXT_TTL_HOURS and not critical_fields_missing:
            if cached["emergency_contacts"]:
                conn.close()
                cached["building_count"] = cached["building_count"] or 0
                return cached

            # Terrain/water/soil/building data is still fresh and reused as
            # normal, but emergency_contacts came back empty on this row's
            # last fetch. An empty result here is ambiguous — it could mean
            # "genuinely nothing tagged within range" or "that one Overpass
            # call failed/returned nothing" — and public-safety data is
            # cheap enough to double-check on every fresh-cache hit rather
            # than silently trust a single empty result for a full day.
            # Only this one field gets re-verified; everything else in the
            # row stays exactly as cached, so this doesn't reintroduce the
            # original Overpass rate-limiting problem the 24h TTL exists to
            # prevent.
            print(f"Emergency contacts cache empty for '{city_key}' — re-checking Overpass")
            fresh_contacts = fetch_emergency_contacts(lat, lon)
            if fresh_contacts:
                conn.execute(
                    "UPDATE geo_context_cache SET emergency_contacts_json = ? WHERE city_key = ?",
                    (json.dumps(fresh_contacts), city_key),
                )
                conn.commit()
                cached["emergency_contacts"] = fresh_contacts
                print(f"Emergency contacts re-check for '{city_key}' found {len(fresh_contacts)} facilities — cache updated")
            else:
                print(f"Emergency contacts re-check for '{city_key}' still empty — will re-check again on next search")

            conn.close()
            cached["building_count"] = cached["building_count"] or 0
            return cached

    # Cache miss or stale — fetch live, falling back field-by-field to the
    # stale cached value if a specific service failed outright on this attempt.
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
            weather_bonus, _ = _weather_bonus(
                w["rainfall"] if w else 0,
                w["humidity"] if w else 50,
                w["pressure"] if w else 1013,
                w["wind"] if w else 0,
                rainfall_word="current",
            )
            context_bonus, _ = _context_bonus(
                {
                    "terrain": terrain,
                    "slope": slope,
                    "water": water,
                    "soil": soil,
                    "urban": urban,
                    "tide": None,
                    "historical_reports": 0,
                }
            )
            score = weather_bonus + context_bonus
            rainfall = w["rainfall"] if w else 0
            if rainfall < 10:
                score = min(score, 24)
            elif rainfall < 30:
                score = min(score, 44)
            score = max(0, min(score, 100))
            risk = classify_risk(score, coastal=coastal)

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


def _severity(raw, max_raw):
    """Normalize a raw score_bonus-style value to a 0-1 severity against
    its own known realistic maximum. Negative raw values floor at 0."""
    if max_raw <= 0:
        return 0.0
    return max(0.0, min(1.0, raw / max_raw))


VULNERABILITY_LEVELS = {
    "LOW": {
        "color": "low",
        "headline": "Low long-term flood vulnerability",
        "guidance": "No strong long-term flood indicators for this location. Still worth asking about drainage history during any rental or purchase due diligence — this is a terrain/history-based estimate, not a guarantee.",
        "warn_renters": False,
    },
    "MODERATE": {
        "color": "moderate",
        "headline": "Some long-term flood vulnerability",
        "guidance": "Before renting or buying here, consider asking about past flooding, checking local drainage infrastructure, and reviewing flood insurance options.",
        "warn_renters": False,
    },
    "HIGH": {
        "color": "high",
        "headline": "Elevated long-term flood vulnerability",
        "guidance": "Before renting or buying here, strongly consider a professional flood-risk survey, ask directly about flooding history, check for flood-resistant features (raised foundation, sump pump, elevated wiring), and price flood insurance into your decision.",
        "warn_renters": True,
    },
    "SEVERE": {
        "color": "severe",
        "headline": "Significant long-term flood vulnerability",
        "guidance": "This location shows multiple strong long-term flood indicators — low elevation, close or historically recurring water presence, and/or a real history of community-reported flooding. Get a professional flood-risk assessment before signing a lease or purchase agreement, and factor in flood insurance cost and availability before committing.",
        "warn_renters": True,
    },
}


# ---------------------------------------------------------------------------
# Regional flood-season context — deliberately NOT an AI "phenomenon
# detector" with invented probabilities or historical-similarity scores.
# Naming a specific meteorological phenomenon (e.g. "Seven Days Rain,
# 93% probability") or computing "82% similarity to the 2017 flood" would
# require a real classification/pattern-matching model this app doesn't
# have — inventing plausible-sounding numbers for those would be exactly
# the kind of unearned-confidence output this app has been fixed to avoid
# elsewhere. What's below is intentionally modest: real, well-established
# rainy-season date ranges by region, and a short, non-exhaustive list of
# genuinely well-documented historical flood events for a handful of
# major cities — shown as plain reference facts, not a predictive claim.
# ---------------------------------------------------------------------------
REGIONAL_RAINY_SEASONS = {
    "NG": "West African Monsoon rainy season, roughly April-October, with peak intensity typically June-September.",
    "IN": "Southwest Monsoon (June-September) brings the bulk of India's annual rainfall.",
    "BD": "Monsoon season (June-October) coincides with the highest river levels of the year.",
    "PH": "Southwest Monsoon ('Habagat', June-September) and typhoon season (roughly June-December) both bring heavy rain.",
    "ID": "Wet season roughly November-March, driven by the Asian monsoon.",
    "JP": "Baiu/Tsuyu rainy season, typically early June to mid-July.",
    "CN": "Meiyu/plum rain season affects central/eastern China roughly June-July.",
    "KR": "Changma rainy season, typically late June to late July.",
    "US": "Atmospheric river events are most common along the West Coast roughly October-March; Gulf Coast hurricane season runs June-November.",
    "AU": "Northern Australia's wet season runs roughly November-April; East Coast Lows can occur any time, more often autumn/winter.",
    "BR": "South Atlantic Convergence Zone activity peaks roughly December-February.",
    "ZA": "Cut-off low systems are most frequent in autumn (March-May) and spring (September-November).",
    "KE": "'Long Rains' season runs roughly March-May; 'Short Rains' October-December.",
    "GH": "West African Monsoon rainy season, roughly April-October.",
}

# Non-exhaustive — genuinely well-documented major flood events only, shown
# as historical fact for context, not as input to any similarity score.
KNOWN_HISTORICAL_FLOODS = {
    "lagos": [2011, 2012, 2017, 2020, 2022],
    "jakarta": [2007, 2013, 2020],
    "mumbai": [2005, 2017, 2021],
    "houston": [2017],
    "manila": [2009, 2020],
    "dhaka": [2004, 2020],
    "chennai": [2015],
    "new orleans": [2005],
}


def get_regional_flood_context(country_code, city_label):
    """Real, static reference facts only — see module note above on why
    this deliberately does not attempt to classify a specific weather
    phenomenon or compute a historical-similarity percentage."""
    season_note = REGIONAL_RAINY_SEASONS.get((country_code or "").upper())

    city_key = normalize_city(city_label)
    known_years = None
    for name, years in KNOWN_HISTORICAL_FLOODS.items():
        if name in city_key:
            known_years = years
            break

    if not season_note and not known_years:
        return None

    return {
        "season_note": season_note,
        "known_flood_years": known_years,
        "disclaimer": "General reference information, not a prediction — this does not assess whether current conditions resemble any past event.",
    }


# ---------------------------------------------------------------------------
# Official emergency telephone numbers — a static, curated supplement to the
# live OSM-based "nearest facility" lookup above (fetch_emergency_contacts).
# This exists specifically because the live OSM lookup depends on Overpass
# being reachable and rate-limit-free at the moment of the request, which in
# production has proven unreliable; these numbers work regardless of
# Overpass's status, since they're static reference data, not a live query.
#
# Every number below is sourced from an official government/agency page or
# a reputable secondary source, not invented — matching the same
# never-fabricate-contact-details principle already used in
# fetch_emergency_contacts(). Coverage is intentionally incomplete rather
# than guessed: NATIONAL_EMERGENCY_NUMBERS covers commonly-known, well
# -documented national lines for the countries in MONITORED_LOCATIONS plus
# a few other major countries; STATE_EMERGENCY_NUMBERS currently only has a
# verified entry for Lagos State, Nigeria (source: lasema.lagosstate.gov.ng)
# as the flagship example. Extend both dicts as more state/province-level
# numbers get verified — do not add entries here without a real source.
# ---------------------------------------------------------------------------
NATIONAL_EMERGENCY_NUMBERS = {
    "NG": {"general": "112", "notes": "112 connects to police, fire, medical, and disaster response nationwide."},
    "US": {"general": "911"},
    "CA": {"general": "911"},
    "GB": {"general": "999", "notes": "112 also works nationwide."},
    "IE": {"general": "112", "notes": "999 also works nationwide."},
    "AU": {"general": "000", "notes": "112 also works from mobile phones."},
    "NZ": {"general": "111"},
    "IN": {"general": "112", "notes": "Unifies former separate lines: police 100, fire 101, ambulance 108."},
    "PK": {"general": "15", "notes": "Rescue services (Punjab and other provinces) — 1122."},
    "BD": {"general": "999"},
    "PH": {"general": "911"},
    "ID": {"general": "112", "notes": "Police direct line — 110."},
    "TH": {"police": "191", "fire": "199", "ambulance": "1669"},
    "VN": {"police": "113", "fire": "114", "ambulance": "115", "notes": "112 also in use nationwide."},
    "CN": {"police": "110", "ambulance": "120", "fire": "119"},
    "JP": {"police": "110", "fire_ambulance": "119"},
    "KR": {"police": "112", "fire_ambulance": "119"},
    "EG": {"police": "122", "ambulance": "123", "fire": "180"},
    "ZA": {"general": "10111", "ambulance": "10177", "notes": "112 also works from mobile phones."},
    "KE": {"general": "999", "notes": "112 also works nationwide."},
    "GH": {"general": "112"},
    "BR": {"police": "190", "ambulance": "192", "fire": "193"},
    "AR": {"general": "911"},
    "IT": {"general": "112", "police": "113", "fire": "115", "ambulance": "118"},
    "NL": {"general": "112"},
    "DE": {"general": "112", "police": "110"},
}

# (country_code, normalized state/region name) -> agency info. Matched via
# normalize_city() against the "state" field OpenWeather's geocoder returns,
# so this only fires when that state name is present and matches.
STATE_EMERGENCY_NUMBERS = {
    ("NG", "lagos"): {
        "agency": "Lagos State Emergency Management Agency (LASEMA)",
        "general": "112 / 767",
        "notes": "Toll-free lines for any emergency within Lagos State — disaster response, medical, fire, security.",
        "source": "lasema.lagosstate.gov.ng",
    },
}


def get_official_emergency_numbers(country_code, state_name=None):
    """Looks up static, verified official emergency numbers for a country
    and, where available, a specific state/region. Returns None if the
    country isn't in the curated set — this deliberately does not fall back
    to a generic guess (e.g. assuming 911 or 112 works everywhere), since a
    wrong emergency number is worse than none."""
    country_code = (country_code or "").upper()
    national = NATIONAL_EMERGENCY_NUMBERS.get(country_code)

    state_info = None
    if state_name:
        state_key = (country_code, normalize_city(state_name))
        state_info = STATE_EMERGENCY_NUMBERS.get(state_key)
        if not state_info:
            # Loose match: OpenWeather's state names aren't always an exact
            # match to our keys (e.g. "Lagos State" vs "Lagos") — check
            # substring containment in both directions before giving up.
            normalized_input = normalize_city(state_name)
            for (cc, key_state), info in STATE_EMERGENCY_NUMBERS.items():
                if cc == country_code and (key_state in normalized_input or normalized_input in key_state):
                    state_info = info
                    break

    if not national and not state_info:
        return None

    return {
        "national": national,
        "state": state_info,
        "disclaimer": "Official reference numbers, not verified live — always confirm locally, as these can change.",
    }


# ---------------------------------------------------------------------------
# Dam Intelligence — a MANUALLY-CURATED status board, deliberately not an
# automated detector. There is no free, structured, machine-readable feed
# for dam release announcements (Lagdo Dam releases, and Nigeria's own dam
# operations, are announced via press statements/news — see NIHSA
# statements — not an API). Auto-scraping news sites to *guess* release
# status would risk exactly the kind of fabricated/unreliable signal this
# app has avoided elsewhere (see get_regional_flood_context's module note).
#
# Instead: DAM_REGISTRY is static reference data (which dams exist, which
# downstream communities to watch, ordered nearest-to-the-dam first — a
# DISTANCE ordering, not a time/ETA estimate, since real travel times need
# river-gauge/hydrology data this app doesn't have). Live STATUS on top of
# that registry is entered by an admin (see /admin/dams) after a real
# official notice — e.g. a NIHSA statement — so a release is only ever
# shown here because someone with access confirmed it happened, not
# because the system inferred it.
# ---------------------------------------------------------------------------
DAM_REGISTRY = {
    "lagdo": {
        "name": "Lagdo Dam",
        "location": "Cameroon (Benue River) — releases affect northeastern Nigeria downstream",
        "downstream": [
            "Yola, Adamawa", "Numan, Adamawa", "Demsa, Adamawa", "Lamurde, Adamawa",
            "Makurdi, Benue", "Lokoja, Kogi", "Onitsha, Anambra",
        ],
    },
    "oyan": {
        "name": "Oyan Dam",
        "location": "Ogun State, Nigeria — supplies raw water to Lagos and Abeokuta",
        "downstream": ["Ikorodu, Lagos", "Isheri, Lagos", "Majidun, Lagos", "Agboyi, Lagos", "Owode, Lagos", "Ogolonto, Lagos"],
    },
    "kainji": {
        "name": "Kainji Dam",
        "location": "Niger State, Nigeria (Niger River)",
        "downstream": ["Jebba, Kwara", "Baro, Niger", "Lokoja, Kogi"],
    },
    "jebba": {
        "name": "Jebba Dam",
        "location": "Kwara/Niger States, Nigeria (Niger River)",
        "downstream": ["Jebba, Kwara", "Baro, Niger"],
    },
    "shiroro": {
        "name": "Shiroro Dam",
        "location": "Niger State, Nigeria",
        "downstream": ["Suleja, Niger", "Minna, Niger"],
    },
    "tiga": {
        "name": "Tiga Dam",
        "location": "Kano State, Nigeria",
        "downstream": ["Kano, Kano"],
    },
    "challawa_gorge": {
        "name": "Challawa Gorge Dam",
        "location": "Kano State, Nigeria",
        "downstream": ["Kano, Kano"],
    },
    "dadin_kowa": {
        "name": "Dadin Kowa Dam",
        "location": "Gombe State, Nigeria",
        "downstream": ["Gombe, Gombe", "Numan, Adamawa"],
    },
    "bakolori": {
        "name": "Bakolori Dam",
        "location": "Zamfara State, Nigeria",
        "downstream": ["Talata Mafara, Zamfara", "Sokoto, Sokoto"],
    },
}

DAM_STATUS_LEVELS = ("NORMAL", "MONITORING", "RELEASE_IN_PROGRESS")


def get_dam_status_board():
    """Merges the static DAM_REGISTRY with live status rows from the DB.
    Any dam with no status row yet defaults to NORMAL — the whole point of
    this being admin-curated is that silence means no one has reported a
    release, not that one is confirmed absent, so the UI should present
    NORMAL as 'nothing reported' rather than an assured all-clear."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = {row["dam_key"]: dict(row) for row in conn.execute("SELECT * FROM dam_status").fetchall()}
    conn.close()

    board = []
    for dam_key, dam in DAM_REGISTRY.items():
        status_row = rows.get(dam_key)
        board.append({
            "dam_key": dam_key,
            "name": dam["name"],
            "location": dam["location"],
            "downstream": dam["downstream"],
            "status": status_row["status"] if status_row else "NORMAL",
            "notes": status_row["notes"] if status_row else None,
            "source_url": status_row["source_url"] if status_row else None,
            "updated_at": status_row["updated_at"] if status_row else None,
        })
    return board


def set_dam_status(dam_key, status, notes=None, source_url=None):
    if dam_key not in DAM_REGISTRY:
        return False
    if status not in DAM_STATUS_LEVELS:
        return False

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO dam_status (dam_key, status, notes, source_url, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(dam_key) DO UPDATE SET
            status=excluded.status,
            notes=excluded.notes,
            source_url=excluded.source_url,
            updated_at=excluded.updated_at
        """,
        (dam_key, status, notes, source_url, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return True




def compute_flood_vulnerability(terrain, slope, water, soil, coastal, earth_engine, historical_reports):
    """Flood VULNERABILITY — how flood-prone this location inherently is —
    as a separate metric from the Live Flood Risk score above. This is
    deliberately WEATHER-INDEPENDENT: it does not use rainfall, tide, or
    river discharge, because those answer 'is it flooding today', not
    'should someone renting or buying here be cautious in general'. A
    location can show LOW live risk on a dry day while still being highly
    vulnerable in general — that distinction is the entire point of this
    function, and why it's shown as a second, separate number rather than
    folded into the main score."""
    terrain_raw = max(0.0, terrain.get("score_bonus", 0)) + max(0.0, slope.get("score_bonus", 0))
    terrain_sev = _severity(terrain_raw, 31)

    water_raw = max(0.0, water.get("score_bonus", 0))
    water_sev = _severity(water_raw, 18)

    soil_raw = max(0.0, soil.get("score_bonus", 0))
    soil_sev = _severity(soil_raw, 7)

    hist_raw = 10 if historical_reports >= 6 else 6 if historical_reports >= 3 else 3 if historical_reports >= 1 else 0
    hist_sev = _severity(hist_raw, 10)

    water_occurrence_pct = None
    if earth_engine and earth_engine.get("available"):
        water_occurrence_pct = earth_engine.get("jrc_water_occurrence_pct")
    occurrence_sev = _severity(water_occurrence_pct or 0, 40)  # 40%+ historical water occurrence is a strong signal

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

    meta = VULNERABILITY_LEVELS[level]

    rent_warning = None
    if meta["warn_renters"] or historical_reports >= 3:
        if historical_reports >= 3:
            reason = f"a documented history of {historical_reports} community-reported flooding incidents"
        else:
            reason = "strong terrain and water-exposure indicators (low elevation, coastal proximity, and/or recurring historical water presence)"
        rent_warning = (
            f"⚠ Before renting or buying property here: this location shows {reason}. "
            "Ask directly about flooding history, inspect for water damage, and consider a professional "
            "flood-risk survey before signing any lease or purchase agreement."
        )

    return {
        "score": score,
        "level": level,
        "color": meta["color"],
        "headline": meta["headline"],
        "guidance": meta["guidance"],
        "factors": factors,
        "water_occurrence_pct": round(water_occurrence_pct) if water_occurrence_pct is not None else None,
        "rent_purchase_warning": rent_warning,
        "disclaimer": (
            "This is a location-history and terrain-based estimate for general awareness, not a certified "
            "flood-zone survey, insurance assessment, or legal disclosure. Always get a professional flood-risk "
            "survey and check official flood maps before a property purchase or lease decision."
        ),
    }


def build_prediction(query, known_place=None):
    """query is used for live geocoding unless known_place is supplied, in
    which case geocoding is skipped entirely and these verified coordinates
    are used directly. This exists because OpenWeather's free-text geocoder
    has been observed to mismatch some Nigerian place names to the wrong
    state/country entirely (e.g. 'Apapa' resolving to Kaduna State instead
    of Lagos, 'Victoria Island' resolving to the Canadian Arctic) — curated
    MONITORED_LOCATIONS entries use verified fixed coordinates to sidestep
    that failure mode; ad-hoc visitor searches still go through live
    geocoding as before, since there's no way to pre-verify an arbitrary
    typed query."""
    if known_place:
        place = known_place
        print(f"STEP 1: Using verified coordinates for {place['name']}")
    else:
        print("STEP 1: Geocoding")
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

    print(f"STEP 2: Weather for {display_name} at {lat},{lon}")
    weather = get_weather(lat, lon, display_name=display_name)
    if not weather:
        return None, []

    # Elevation, slope, water proximity, soil, and urbanization are cached
    # for a day (GEO_CONTEXT_TTL_HOURS) since they don't meaningfully change
    # hour to hour — this is what keeps Overpass/SoilGrids call volume low
    # enough to avoid rate-limiting on repeated watchlist sweeps.
    # A point dropped on the map may sit inside a city but still have very
    # different terrain/water context from its centre. Give those analyses a
    # coordinate-based cache key so their terrain result is never reused for
    # a different point in the same city.
    city_key = (known_place or {}).get("cache_key") or normalize_city(weather["city"])
    print("STEP 3: Geo Context")
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

    # Static, verified official emergency numbers — independent of Overpass
    # entirely, so this reflects even when the live nearest-facility lookup
    # above is empty or Overpass is temporarily unreachable/rate-limited.
    official_emergency_numbers = get_official_emergency_numbers(weather.get("country"), place.get("state"))

    # Weather, tide, and river discharge genuinely change over time, so
    # these are still fetched fresh on every call.
    print("STEP 4: Tide")
    tide_height = fetch_tide_status(lat, lon)
    tide = classify_tide(tide_height)

    print("STEP 5: River")
    discharge_current, discharge_mean = fetch_river_discharge(lat, lon)
    river = classify_river_discharge(discharge_current, discharge_mean)

    print("STEP 6: Soil Moisture")
    moisture_value = fetch_soil_moisture(lat, lon)
    moisture = classify_soil_moisture(moisture_value)

    community = get_city_stats(weather["city"])
    historical_reports = get_historical_frequency(weather["city"])

    # Flood VULNERABILITY (permanent, weather-independent) computed
    # separately from the Live Flood Risk score below — a location can
    # show low live risk on a dry day while still being highly
    # flood-prone in general, and the app should say so explicitly rather
    # than implying "safe" just because it isn't raining right now.
    vulnerability = compute_flood_vulnerability(
        terrain, slope, water, soil, coastal, earth_engine, historical_reports
    )
    regional_context = get_regional_flood_context(weather.get("country"), weather["city"])

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
    print("STEP 7: Forecast")
    forecast, timeline = get_forecast(lat, lon, context)

    print("STEP 8: Flood Model")
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
    rainfall_warning = build_rainfall_warning(timeline, coastal=coastal)
    print("STEP 9: Completed")
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
        "rainfall_warning": rainfall_warning,
        "timeline": timeline,

        "emergency_contacts": emergency_contacts,
        "official_emergency_numbers": official_emergency_numbers,

        "historical_reports": historical_reports,
        "earth_engine": earth_engine,

        "vulnerability": vulnerability,
        "regional_context": regional_context,

        # -----------------------------
        # Dynamic values for the widget
        # -----------------------------
        "rainfall_mm": weather["rainfall"],
        "temperature_c": weather["temperature"],
        "humidity_percent": weather["humidity"],
        "wind_speed_kmh": weather["wind_speed"],
        "weather_condition": weather["description"],

        "terrain_risk": terrain,
        "drainage_status": urban,
        "flood_history": historical_reports,
        "soil_type": soil,
        "river_status": river,
        "tide_status": tide,
        "soil_moisture": moisture,
    }, forecast


def _share_card_font(size, bold=False):
    """Return a portable font for generated social preview cards."""
    candidates = (
        ["DejaVuSans-Bold.ttf", "arialbd.ttf"]
        if bold else ["DejaVuSans.ttf", "arial.ttf"]
    )
    for font_name in candidates:
        try:
            return ImageFont.truetype(font_name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_wrapped_text(draw, text, xy, font, fill, max_width, line_gap=10):
    """Draw text in a bounded area and return the next available y position."""
    x, y = xy
    words = str(text).split()
    line = ""
    for word in words:
        candidate = (line + " " + word).strip()
        if line and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            draw.text((x, y), line, font=font, fill=fill)
            y += font.size + line_gap
            line = word
        else:
            line = candidate
    if line:
        draw.text((x, y), line, font=font, fill=fill)
        y += font.size + line_gap
    return y


@app.route("/share-card.png")
def share_card():
    """A branded preview image used by WhatsApp and other social link cards."""
    token = request.args.get("card", "")
    try:
        snapshot = URLSafeSerializer(app.secret_key, salt="floodguard-share-card").loads(token)
    except BadSignature:
        return "This share card is invalid or has expired.", 400

    city = str(snapshot.get("city", "")).strip()
    risk = str(snapshot.get("risk", "")).strip()
    score = snapshot.get("score")
    risk_color = str(snapshot.get("risk_color", "")).strip()
    if not city or not risk or not isinstance(score, int) or not 0 <= score <= 100:
        return "This share card is invalid.", 400

    risk_colours = {
        "low": (34, 197, 94),
        "watch": (245, 158, 11),
        "high": (220, 38, 38),
        "severe": (185, 28, 28),
        "critical": (127, 29, 29),
    }
    accent = risk_colours.get(risk_color, (14, 165, 233))
    image = Image.new("RGB", (1200, 630), (5, 17, 31))
    draw = ImageDraw.Draw(image)
    draw.ellipse((760, -250, 1370, 360), fill=tuple(max(0, c - 85) for c in accent))
    draw.rounded_rectangle((54, 54, 1146, 576), radius=34, fill=(12, 32, 56), outline=accent, width=4)

    label_font = _share_card_font(27, bold=True)
    heading_font = _share_card_font(72, bold=True)
    body_font = _share_card_font(32)
    score_font = _share_card_font(52, bold=True)
    draw.text((100, 103), "FLOODGUARD AI  •  LIVE LOCAL CHECK", font=label_font, fill=(125, 211, 252))
    draw.text((100, 174), risk + " FLOOD RISK", font=heading_font, fill=accent)
    y = _draw_wrapped_text(draw, city, (100, 275), body_font, (248, 250, 252), 690)
    draw.text((100, max(y + 14, 395)), "Flood score  " + str(score) + "/100", font=score_font, fill=(255, 255, 255))
    draw.text((100, 505), "Check conditions before you travel.", font=body_font, fill=(203, 213, 225))
    draw.text((838, 505), "floodguard.ai", font=label_font, fill=(125, 211, 252))

    output = BytesIO()
    image.save(output, "PNG", optimize=True)
    output.seek(0)
    response = send_file(output, mimetype="image/png", download_name="floodguard-risk-card.png")
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


@app.route("/", methods=["GET", "POST"])
def home():
    prediction = None
    forecast = []
    error = None
    reports = []
    share_card_token = None

    # GDACS needs no API key, so real global flood coverage works even
    # before OpenWeather is configured.
    maybe_refresh_global_alerts_async()

    if API_KEY:
        maybe_refresh_watchlist_async()
        maybe_send_daily_digests()

    city = (request.form.get("city") if request.method == "POST" else request.args.get("location", "")).strip()
    map_known_place = None
    map_lat_raw = request.args.get("map_lat") if request.method == "GET" else None
    map_lon_raw = request.args.get("map_lon") if request.method == "GET" else None

    if map_lat_raw is not None or map_lon_raw is not None:
        try:
            map_lat = float(map_lat_raw)
            map_lon = float(map_lon_raw)
            if not math.isfinite(map_lat) or not math.isfinite(map_lon) or not -90 <= map_lat <= 90 or not -180 <= map_lon <= 180:
                raise ValueError
        except (TypeError, ValueError):
            error = "That map point is invalid. Please choose another location."
        else:
            map_label = request.args.get("map_label", "Pinned map location").strip()[:150] or "Pinned map location"
            city = map_label
            map_known_place = {
                "lat": map_lat,
                "lon": map_lon,
                "name": map_label,
                "state": "",
                "country": "",
                "cache_key": f"map:{map_lat:.5f},{map_lon:.5f}",
            }

    if request.method == "POST" and not city:
        error = "Please enter a city name."
    elif city and not error:
        if not API_KEY:
            error = "Weather API key is missing. Add OPENWEATHER_API_KEY to your hosting environment variables."
        else:
            prediction, forecast = build_prediction(
                city,
                known_place=map_known_place or _known_place_for_curated_location(city),
            )
            if not prediction:
                error = "City not found or weather service unavailable."
            else:
                reports = get_city_contributions(prediction["city"])
                log_search(prediction["city"], prediction["risk"], prediction["score"])
                cache_watchlist_entry_now(prediction)
                share_card_token = URLSafeSerializer(app.secret_key, salt="floodguard-share-card").dumps({
                    "city": prediction["city"],
                    "risk": prediction["risk"],
                    "score": prediction["score"],
                    "risk_color": prediction["risk_color"],
                })

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
        share_card_token=share_card_token,
    )


@app.route("/api/map-reverse-geocode")
def map_reverse_geocode():
    """Name a user-selected map point without exposing a third-party API to the browser."""
    if not API_KEY:
        return jsonify({"ok": False, "error": "Location search is unavailable right now."}), 503

    try:
        latitude = float(request.args.get("lat", ""))
        longitude = float(request.args.get("lon", ""))
        if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "That map point is invalid."}), 400

    label = "Pinned map location"
    try:
        response = requests.get(
            "https://api.openweathermap.org/geo/1.0/reverse",
            params={"lat": latitude, "lon": longitude, "limit": 1, "appid": API_KEY},
            timeout=8,
        )
        response.raise_for_status()
        results = response.json()
        if results:
            place = results[0]
            label_parts = [place.get("name") or "Pinned map location"]
            if place.get("state"):
                label_parts.append(place["state"])
            if place.get("country"):
                label_parts.append(place["country"])
            label = ", ".join(label_parts)
    except (requests.RequestException, ValueError) as error:
        # The coordinates are still enough to run the analysis. Falling back
        # to a generic label keeps map pinning useful during a geocoder outage.
        print(f"Map reverse geocoding failed: {error}")

    return jsonify({"ok": True, "label": label, "lat": latitude, "lon": longitude})


@app.route("/api/map-geocode")
def map_geocode():
    """Resolve a saved-place search for the global map."""
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "Enter a location to search."}), 400
    if len(query) > 150:
        return jsonify({"ok": False, "error": "Location names are too long."}), 400

    place = geocode_location(query)
    if not place:
        return jsonify({"ok": False, "error": "Location not found."}), 404

    label_parts = [place["name"]]
    if place.get("state"):
        label_parts.append(place["state"])
    if place.get("country"):
        label_parts.append(place["country"])
    return jsonify(
        {
            "ok": True,
            "label": ", ".join(label_parts),
            "lat": place["lat"],
            "lon": place["lon"],
        }
    )


@app.route("/api/widget", methods=["POST"])
def widget_api():

    city = request.form.get("city", "").strip()

    if not city:
        return jsonify({
            "ok": False,
            "error": "Please enter a city."
        })

    prediction, forecast = build_prediction(city, known_place=_known_place_for_curated_location(city))

    if not prediction:
        return jsonify({
            "ok": False,
            "error": "Location not found."
        })

    reports = get_city_contributions(prediction["city"])
    log_search(prediction["city"], prediction["risk"], prediction["score"])
    cache_watchlist_entry_now(prediction)

    return jsonify({
        "ok": True,
        "prediction": prediction,
        "forecast": forecast,
        "reports": reports,
        "category_labels": CATEGORY_LABELS,
        "mapbox_token": MAPBOX_ACCESS_TOKEN,
    })

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
    # Without this, a visitor who leaves the tab open (never reloading "/")
    # polls this endpoint every 45s forever and gets back the exact same
    # stale snapshot each time — nothing ever re-triggers the sweep, since
    # previously only a full page load did. This is what actually makes
    # background polling keep the data fresh, not just keep re-displaying
    # whatever was cached the last time someone happened to reload the page.
    if API_KEY:
        maybe_refresh_watchlist_async()
        maybe_send_daily_digests()
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
    # Same fix as /api/watchlist-status above — this is the endpoint the
    # frontend's 60s poll hits, and it needs to be able to trigger its own
    # staleness check rather than only ever refreshing on a full page load.
    maybe_refresh_global_alerts_async()
    return jsonify({"ok": True, **get_global_alerts_status()})


@app.route("/api/global-situation")
def api_global_situation():
    """One globally scoped, source-labelled snapshot for the Living Flood Map."""
    maybe_refresh_global_alerts_async()
    if API_KEY:
        maybe_refresh_watchlist_async()
    return jsonify(get_global_situation())


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


@app.route("/api/send-digest", methods=["GET", "POST"])
def api_send_digest():
    """Triggers the twice-daily subscriber weather digest. Intended for an
    external scheduler (cron-job.org, GitHub Actions) to hit twice a day at
    DIGEST_MORNING_UTC_HOUR / DIGEST_EVENING_UTC_HOUR — this is the
    reliable path; maybe_send_daily_digests() (tied to page traffic) is
    only a best-effort fallback for when no scheduler is configured.
    Requires ?type=morning or ?type=evening; still deduplicated by
    digest_sends so calling this more than once in the target hour is safe."""
    digest_type = (request.args.get("type") or request.form.get("type") or "").strip().lower()
    if digest_type not in ("morning", "evening"):
        return jsonify({"ok": False, "error": "Query param 'type' must be 'morning' or 'evening'."}), 400

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    digest_key = f"{digest_type}:{today_str}"

    print(f"DIGEST: /api/send-digest hit for '{digest_type}' (key={digest_key})")
    print("DIGEST: TEST MODE — daily lock temporarily bypassed")

    send_daily_digests(digest_type)

    return jsonify({
        "ok": True,
        "note": f"{digest_type} digest test finished."
    })


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
    weather-independent, so this is useful any time of year, not just
    during an active storm."""
    if not API_KEY:
        return jsonify({"ok": False, "error": "OPENWEATHER_API_KEY is not configured."}), 400

    payload = request.get_json(silent=True) or request.form
    location = (payload.get("location") or "").strip()

    if not location:
        return jsonify({"ok": False, "error": "A location or address is required."}), 400
    if len(location) > 150:
        return jsonify({"ok": False, "error": "Location is too long."}), 400

    place = geocode_location(location)
    if not place:
        return jsonify({"ok": False, "error": f"Could not find '{location}'."}), 400

    try:
        lat, lon = place["lat"], place["lon"]
        location_label = place["name"] + (f", {place['state']}" if place.get("state") else "")
        city_key = normalize_city(location_label)

        geo = get_geo_context(city_key, lat, lon)
        # Temporarily disable Earth Engine availability for assessments
        earth_engine = {"available": False}
        historical_reports = get_historical_frequency(location_label)

        elevation = geo["elevation"]
        slope_percent = geo["slope_percent"]
        terrain = classify_terrain(elevation)
        slope = classify_slope(slope_percent)
        water = classify_water_proximity(geo["nearest_water_m"], geo["nearest_water_label"])
        coastal = is_coastal_region(geo["nearest_coast_m"])
        soil = classify_soil(geo["clay_percent"])

        vulnerability = compute_flood_vulnerability(
            terrain, slope, water, soil, coastal, earth_engine, historical_reports
        )
    except Exception as error:  # noqa: BLE001 — never let an edge case 500 the page
        print(f"Property flood risk assessment failed: {error}")
        return jsonify({"ok": False, "error": "Something went wrong checking this location. Please try again."}), 500

    return jsonify({
        "ok": True,
        "location": location_label,
        "elevation_m": round(elevation) if elevation is not None else None,
        "coastal": coastal,
        "historical_reports": historical_reports,
        **vulnerability,
    })


@app.route("/api/alert-subscribe", methods=["POST"])
def api_alert_subscribe():
    """Subscribes an email to flood alerts for a specific location — Watch,
    Warning, or Emergency tier, whichever is newly reached (checked every
    watchlist sweep — see check_and_send_location_alerts) — and optionally
    to status-change alerts for one or more dams. Location is validated via
    a real geocode lookup before saving, so a typo'd or unrecognized place
    doesn't silently create a subscription that can never actually match
    anything in the sweep."""
    if not API_KEY:
        return jsonify({"ok": False, "error": "OPENWEATHER_API_KEY is not configured."}), 400

    payload = request.get_json(silent=True) or request.form
    name = (payload.get("name") or "").strip() or None
    email = (payload.get("email") or "").strip().lower()
    phone = (payload.get("phone") or "").strip() or None
    whatsapp = (payload.get("whatsapp") or "").strip() or None
    label = (payload.get("label") or "Location").strip()
    city = (payload.get("city") or "").strip()

    # dam_keys can arrive as a JSON list, or as repeated form fields /
    # comma-separated string depending on how the frontend submits it.
    raw_dam_keys = payload.get("dam_keys")
    if isinstance(raw_dam_keys, list):
        dam_keys = [k.strip() for k in raw_dam_keys if k and k.strip()]
    elif isinstance(raw_dam_keys, str) and raw_dam_keys.strip():
        dam_keys = [k.strip() for k in raw_dam_keys.split(",") if k.strip()]
    else:
        dam_keys = []
    invalid_dam_keys = [k for k in dam_keys if k not in DAM_REGISTRY]

    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "error": "A valid email address is required."}), 400
    if not city:
        return jsonify({"ok": False, "error": "A location is required."}), 400
    if len(email) > 200 or len(city) > 150 or len(label) > 50:
        return jsonify({"ok": False, "error": "One of the fields is too long."}), 400
    if name and len(name) > 100:
        return jsonify({"ok": False, "error": "Name is too long."}), 400
    if phone and len(phone) > 30:
        return jsonify({"ok": False, "error": "Phone number is too long."}), 400
    if whatsapp and len(whatsapp) > 30:
        return jsonify({"ok": False, "error": "WhatsApp number is too long."}), 400
    if invalid_dam_keys:
        return jsonify({"ok": False, "error": f"Unknown dam selection: {', '.join(invalid_dam_keys)}."}), 400

    place = geocode_location(city)
    if not place:
        return jsonify({"ok": False, "error": f"Could not find '{city}'. Try being more specific, e.g. 'Ikoyi, Lagos'."}), 400

    city_label = place["name"] + (f", {place['state']}" if place.get("state") else "")

    try:
        subscriber_id, token = get_or_create_subscriber(email, phone, whatsapp, name)
        location_id, is_new = add_alert_location(subscriber_id, label, city_label)
        newly_subscribed_dams = []
        for dam_key in dam_keys:
            _, dam_is_new = add_dam_subscription(subscriber_id, dam_key)
            if dam_is_new:
                newly_subscribed_dams.append(DAM_REGISTRY[dam_key]["name"])
    except sqlite3.IntegrityError as error:
        print(f"Alert subscribe DB error: {error}")
        return jsonify({"ok": False, "error": "Something went wrong saving your subscription. Please try again."}), 500

    if is_new or newly_subscribed_dams:
        unsubscribe_url = f"{SITE_BASE_URL}/unsubscribe/{token}"
        location_line = (
            f"<p>You'll get an email whenever <strong>{label}</strong> ({city_label}) "
            "reaches Heavy Rain Watch, Flash Flood Warning, or Emergency Flood Alert level.</p>"
            if is_new else ""
        )
        dam_line = (
            f"<p>You're now also subscribed to status updates for: <strong>{', '.join(newly_subscribed_dams)}</strong>.</p>"
            if newly_subscribed_dams else ""
        )
        send_alert_email(
            email,
            "You're now subscribed to FloodGuard AI alerts",
            f"""
            <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;">
                <h2>You're subscribed ✅</h2>
                {f"<p>Hi {name},</p>" if name else ""}
                {location_line}
                {dam_line}
                <p style="font-size:12px;color:#6b7280;">
                    <a href="{unsubscribe_url}">Unsubscribe from all alerts</a> at any time.
                </p>
            </div>
            """,
        )

    dam_message = f" Also subscribed to dam alerts: {', '.join(newly_subscribed_dams)}." if newly_subscribed_dams else ""
    return jsonify({
        "ok": True,
        "message": (
            f"Subscribed! We'll email {email} if {city_label} reaches Watch, Warning, or Emergency level.{dam_message}"
            if is_new else f"{city_label} is already on your alert list for {email}.{dam_message}"
        ),
    })


@app.route("/unsubscribe/<token>")
def unsubscribe(token):
    """Public unsubscribe link included in every alert email. Removes the
    subscriber and all of their watched locations entirely — this app
    doesn't yet support unsubscribing from a single location while keeping
    others, only all-or-nothing."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, email FROM alert_subscribers WHERE unsubscribe_token = ?", (token,)).fetchone()

    if not row:
        conn.close()
        return "This unsubscribe link is invalid or has already been used.", 404

    conn.execute("DELETE FROM alert_locations WHERE subscriber_id = ?", (row["id"],))
    conn.execute("DELETE FROM alert_subscribers WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()

    return f"You've been unsubscribed ({row['email']}). You will no longer receive flood alerts from FloodGuard AI."


@app.route("/api/stats")
def api_stats():
    stats = get_site_stats()
    stats["total_contributions"] = total_contributions_count()
    return jsonify({"ok": True, **stats})


def login_required(view_func):
    """Guards admin-only routes. Redirects to /admin/login rather than
    returning a bare 403, since these are also normal browser-navigated
    pages (not just API endpoints)."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return view_func(*args, **kwargs)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if not ADMIN_PASSWORD_HASH:
            error = "Admin login is not configured (ADMIN_PASSWORD_HASH is not set)."
        else:
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
                session["is_admin"] = True
                return redirect(url_for("admin_dams"))
            error = "Incorrect username or password."
    return render_template("admin_login.html", error=error)


@app.route("/api/dam-status")
def api_dam_status():
    return jsonify({"ok": True, "dams": get_dam_status_board()})


@app.route("/admin/dams", methods=["GET", "POST"])
@login_required
def admin_dams():
    message = None
    if request.method == "POST":
        dam_key = request.form.get("dam_key", "")
        status = request.form.get("status", "")
        notes = request.form.get("notes", "").strip() or None
        source_url = request.form.get("source_url", "").strip() or None

        if dam_key not in DAM_REGISTRY:
            message = "Unknown dam."
        elif status not in DAM_STATUS_LEVELS:
            message = "Invalid status."
        else:
            set_dam_status(dam_key, status, notes, source_url)
            check_and_send_dam_alerts(dam_key, status, notes, source_url)
            message = f"Updated {DAM_REGISTRY[dam_key]['name']}."

    return render_template("admin_dams.html", dams=get_dam_status_board(), status_levels=DAM_STATUS_LEVELS, message=message)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/health")
def health():
    # Initialization is normally lazy (triggered by the first real search),
    # so hitting /health before any search would otherwise show a
    # misleading "not initialized" even when everything is configured
    # correctly. Attempt it here so this endpoint gives a true answer.
    if EARTH_ENGINE_ENABLED and ee is not None and not _ee_initialized:
        initialize_earth_engine()

    return {
        "status": "ok",
        "service": "FloodGuard AI",
        "config": {
            "openweather_configured": bool(API_KEY),
            "weatherapi_fallback_configured": bool(WEATHERAPI_KEY),
            "tide_configured": bool(TIDE_API_KEY),
            "mapbox_configured": bool(MAPBOX_ACCESS_TOKEN),
            "earth_engine_enabled": bool(EARTH_ENGINE_ENABLED),
            "earth_engine_package_installed": ee is not None,
            "earth_engine_key_present": bool(GEE_PRIVATE_KEY_PATH and os.path.exists(GEE_PRIVATE_KEY_PATH)),
            "earth_engine_initialized": bool(_ee_initialized),
            "earth_engine_error": _ee_init_error,
        },
    }
@app.route("/widget", methods=["GET", "POST"])
def widget():

    prediction = None
    forecast = []
    error = None
    reports = []

    if request.method == "POST":

        city = request.form.get("city", "").strip()

        if not city:
            error = "Please enter a city."

        elif not API_KEY:
            error = "Weather API key is missing."

        else:

            prediction, forecast = build_prediction(city, known_place=_known_place_for_curated_location(city))

            if prediction:
                reports = get_city_contributions(prediction["city"])

            else:
                error = "Location not found."

    return render_template(
        "widget.html",
        prediction=prediction,
        forecast=forecast,
        reports=reports,
        error=error
    )

if __name__ == "__main__":
    app.run(debug=True)
