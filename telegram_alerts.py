"""
telegram_alerts.py - worldwide Telegram flood alerts for FloodGuard AI.

Uses the official Telegram Bot API (https://core.telegram.org/bots/api) over
plain HTTPS with `requests`; no extra packages. The bot token is read only
from the TELEGRAM_BOT_TOKEN environment variable and is never returned to the
browser, written to logs, or stored in the database.

Flow
----
1. A resident picks country / region / city / community on the website.
   -> POST /api/telegram/prepare validates the place with the geocoder, stores
      the choice under a one-time token and returns https://t.me/<bot>?start=<token>.
2. They tap the link and press Start. Telegram POSTs the update to our webhook;
   /start <token> binds their chat to that place. (They can also /add places
   directly in the chat, e.g. "/add Paris, France".)
3. After each watchlist sweep (the same scheduler that refreshes the site)
   dispatch_alerts_for_prediction() runs for every monitored area. It reads the
   SAME prediction, street assessment and official-alert data the website shows,
   and messages only subscribers whose place matches that area.

Matching (worldwide)
--------------------
A subscriber location is (country, region?, city, community?). It matches an
alerting area when the country is the same AND
  * community given: the area's neighbourhood/place name equals the community;
  * city only: the area's city or place name equals the city (a region with the
    same name as the city, e.g. Lagos, also counts), and - if both sides know
    their region - the regions agree.

Alert rules (all configurable, see CONFIG below)
------------------------------------------------
* First alert when the area score reaches the threshold (default 30 on the
  0-100 FloodGuard scale; configurable per country; NOT a probability).
* No repeat while the same episode continues, plus a cooldown (default 6 h).
* "Urgent update" when the score rises by >= TELEGRAM_ESCALATION_DELTA over the
  last alerted score (and >= TELEGRAM_URGENT_MIN_GAP_MINUTES since the last one).
* Data older than TELEGRAM_MAX_DATA_AGE_MINUTES is never alerted on.
* TELEGRAM_MODE=test (default) sends nothing to residents: it logs what it would
  have sent and only messages the admin chat(s).
* Delivery is claimed atomically in SQLite (compare-and-swap) before sending, so
  two workers can never send the same alert twice.
* Every message shows times in the subscriber place's local time zone.
"""

import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

import street_risk

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None


# ---------------------------------------------------------------------------
# CONFIG (environment variables)
# ---------------------------------------------------------------------------
def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
BOT_USERNAME = (os.getenv("TELEGRAM_BOT_USERNAME") or "").lstrip("@").strip()
MODE = (os.getenv("TELEGRAM_MODE", "test").strip().lower() or "test")  # "test" | "live"
ADMIN_CHAT_IDS = [c.strip() for c in (os.getenv("TELEGRAM_ADMIN_CHAT_IDS") or "").split(",") if c.strip()]

ALERT_THRESHOLD = street_risk.ALERT_THRESHOLD          # default; see street_risk.threshold_for(country)
COOLDOWN_HOURS = _f("TELEGRAM_COOLDOWN_HOURS", 6)
ESCALATION_DELTA = _f("TELEGRAM_ESCALATION_DELTA", 15)
URGENT_MIN_GAP_MINUTES = _f("TELEGRAM_URGENT_MIN_GAP_MINUTES", 30)
MAX_DATA_AGE_MINUTES = _f("TELEGRAM_MAX_DATA_AGE_MINUTES", 90)
CLEAR_MARGIN = _f("TELEGRAM_CLEAR_MARGIN", 5)
MAX_LOCATIONS_PER_SUBSCRIBER = int(_f("TELEGRAM_MAX_LOCATIONS", 5))
MAX_EXTRA_AREAS = int(_f("TELEGRAM_MAX_EXTRA_AREAS", 25))
PENDING_TOKEN_HOURS = 24
SEND_PAUSE_SECONDS = 0.06  # stay far below Telegram's ~30 messages/second limit

# ISO 3166-1 alpha-2 codes. Used to validate the country a subscriber picks.
_COUNTRY_DATA = (
    "AF Afghanistan|AL Albania|DZ Algeria|AD Andorra|AO Angola|AG Antigua and Barbuda|AR Argentina|AM Armenia|"
    "AU Australia|AT Austria|AZ Azerbaijan|BS Bahamas|BH Bahrain|BD Bangladesh|BB Barbados|BY Belarus|BE Belgium|"
    "BZ Belize|BJ Benin|BT Bhutan|BO Bolivia|BA Bosnia and Herzegovina|BW Botswana|BR Brazil|BN Brunei|BG Bulgaria|"
    "BF Burkina Faso|BI Burundi|CV Cabo Verde|KH Cambodia|CM Cameroon|CA Canada|CF Central African Republic|TD Chad|"
    "CL Chile|CN China|CO Colombia|KM Comoros|CG Congo|CD Democratic Republic of the Congo|CR Costa Rica|"
    "CI Cote d'Ivoire|HR Croatia|CU Cuba|CY Cyprus|CZ Czechia|DK Denmark|DJ Djibouti|DM Dominica|"
    "DO Dominican Republic|EC Ecuador|EG Egypt|SV El Salvador|GQ Equatorial Guinea|ER Eritrea|EE Estonia|"
    "SZ Eswatini|ET Ethiopia|FJ Fiji|FI Finland|FR France|GA Gabon|GM Gambia|GE Georgia|DE Germany|GH Ghana|"
    "GR Greece|GD Grenada|GT Guatemala|GN Guinea|GW Guinea-Bissau|GY Guyana|HT Haiti|HN Honduras|HK Hong Kong|"
    "HU Hungary|IS Iceland|IN India|ID Indonesia|IR Iran|IQ Iraq|IE Ireland|IL Israel|IT Italy|JM Jamaica|JP Japan|"
    "JO Jordan|KZ Kazakhstan|KE Kenya|KI Kiribati|KW Kuwait|KG Kyrgyzstan|LA Laos|LV Latvia|LB Lebanon|LS Lesotho|"
    "LR Liberia|LY Libya|LI Liechtenstein|LT Lithuania|LU Luxembourg|MO Macao|MG Madagascar|MW Malawi|MY Malaysia|"
    "MV Maldives|ML Mali|MT Malta|MH Marshall Islands|MR Mauritania|MU Mauritius|MX Mexico|FM Micronesia|"
    "MD Moldova|MC Monaco|MN Mongolia|ME Montenegro|MA Morocco|MZ Mozambique|MM Myanmar|NA Namibia|NR Nauru|"
    "NP Nepal|NL Netherlands|NZ New Zealand|NI Nicaragua|NE Niger|NG Nigeria|KP North Korea|MK North Macedonia|"
    "NO Norway|OM Oman|PK Pakistan|PW Palau|PS Palestine|PA Panama|PG Papua New Guinea|PY Paraguay|PE Peru|"
    "PH Philippines|PL Poland|PT Portugal|PR Puerto Rico|QA Qatar|RO Romania|RU Russia|RW Rwanda|"
    "KN Saint Kitts and Nevis|LC Saint Lucia|VC Saint Vincent and the Grenadines|WS Samoa|SM San Marino|"
    "ST Sao Tome and Principe|SA Saudi Arabia|SN Senegal|RS Serbia|SC Seychelles|SL Sierra Leone|SG Singapore|"
    "SK Slovakia|SI Slovenia|SB Solomon Islands|SO Somalia|ZA South Africa|KR South Korea|SS South Sudan|ES Spain|"
    "LK Sri Lanka|SD Sudan|SR Suriname|SE Sweden|CH Switzerland|SY Syria|TW Taiwan|TJ Tajikistan|TZ Tanzania|"
    "TH Thailand|TL Timor-Leste|TG Togo|TO Tonga|TT Trinidad and Tobago|TN Tunisia|TR Turkey|TM Turkmenistan|"
    "TV Tuvalu|UG Uganda|UA Ukraine|AE United Arab Emirates|GB United Kingdom|US United States|UY Uruguay|"
    "UZ Uzbekistan|VU Vanuatu|VA Vatican City|VE Venezuela|VN Vietnam|YE Yemen|ZM Zambia|ZW Zimbabwe"
)


def norm(text):
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]|_", " ", text).split())


COUNTRIES = {}          # code -> name
_COUNTRY_BY_NAME = {}   # normalised name -> code
for _entry in _COUNTRY_DATA.split("|"):
    _code, _, _name = _entry.partition(" ")
    COUNTRIES[_code] = _name
    _COUNTRY_BY_NAME[norm(_name)] = _code
_ALIASES = {"usa": "US", "united states of america": "US", "america": "US", "uk": "GB", "great britain": "GB",
            "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB", "ivory coast": "CI",
            "czech republic": "CZ", "burma": "MM", "russian federation": "RU", "korea": "KR", "uae": "AE",
            "holland": "NL", "swaziland": "SZ", "turkiye": "TR", "republic of the congo": "CG", "drc": "CD"}


_ADMIN_WORDS = {"state", "province", "region", "county", "prefecture", "governorate", "district", "territory", "of", "the"}


def norm_region(text):
    """'Lagos State' / 'Ontario Province' -> 'lagos' / 'ontario'; FCT aliases unified."""
    words = [w for w in norm(text).split() if w not in _ADMIN_WORDS]
    key = " ".join(words)
    if key in ("fct", "abuja", "federal capital", "federal capital territory", "f c t"):
        return "fct"
    return key


def country_code_for(text):
    """'Nigeria' / 'ng' / 'UK' -> 'NG' / 'NG' / 'GB'; None if not recognised."""
    key = norm(text)
    if not key:
        return None
    if len(key) == 2 and key.upper() in COUNTRIES:
        return key.upper()
    return _COUNTRY_BY_NAME.get(key) or _ALIASES.get(key)


def country_name(code):
    return COUNTRIES.get((code or "").upper(), code or "")


_dispatch_lock = threading.Lock()
_deps = {}


def init(**deps):
    """app.py injects what it owns: db_path, normalize_city, resolve_area,
    site_url, street_summary, safety_info."""
    _deps.update(deps)


def enabled():
    return bool(BOT_TOKEN)


def live():
    return enabled() and MODE == "live"


def _now():
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Identity / matching
# ---------------------------------------------------------------------------
def area_identity(area):
    """Normalised identity of an alerting area, from prediction["area"]."""
    area = area or {}
    return {
        "country_code": (area.get("country_code") or "").upper(),
        "region_key": norm_region(area.get("region")),
        "city_key": norm(area.get("city")),
        "name_key": norm(area.get("name")),
        "neighbourhood_key": norm(area.get("neighbourhood")),
    }


def row_matches_area(row, ident):
    if not ident["country_code"] or row["country_code"] != ident["country_code"]:
        return False
    if row["community_key"]:
        return row["community_key"] in (ident["name_key"], ident["neighbourhood_key"])
    city = row["city_key"]
    if city not in (ident["city_key"], ident["name_key"], ident["region_key"]):
        return False
    if row["region_key"] and ident["region_key"] and row["region_key"] != ident["region_key"]:
        return False
    return True


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def local_time_text(dt_utc, tz_name=None, offset_seconds=None):
    """'27 Oct 2026, 13:05 WAT (UTC+01:00)'. Falls back to a fixed offset and
    finally to UTC, never to a guess."""
    if dt_utc is None:
        return "unknown time"
    aware = dt_utc.replace(tzinfo=timezone.utc) if dt_utc.tzinfo is None else dt_utc
    local = None
    if tz_name and ZoneInfo:
        try:
            local = aware.astimezone(ZoneInfo(tz_name))
        except Exception:  # noqa: BLE001 - unknown zone or missing tzdata
            local = None
    if local is None and offset_seconds is not None:
        try:
            local = aware.astimezone(timezone(timedelta(seconds=int(offset_seconds))))
        except (TypeError, ValueError):
            local = None
    if local is None:
        local = aware.astimezone(timezone.utc)
    total = int((local.utcoffset() or timedelta(0)).total_seconds())
    sign = "+" if total >= 0 else "-"
    hh, mm = divmod(abs(total) // 60, 60)
    abbr = local.tzname() or ""
    abbr = "" if abbr.startswith(("UTC+", "UTC-")) else abbr
    return f"{local.day} {local.strftime('%b %Y, %H:%M')}{(' ' + abbr) if abbr else ''} (UTC{sign}{hh:02d}:{mm:02d})"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def _conn():
    conn = sqlite3.connect(_deps["db_path"], timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS telegram_subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL UNIQUE,
            username TEXT,
            first_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',      -- active | paused | blocked
            urgent_updates INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_interaction_at TEXT
        );
        CREATE TABLE IF NOT EXISTS telegram_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscriber_id INTEGER NOT NULL,
            label TEXT NOT NULL,                 -- local place name shown to the user, e.g. 'Lekki, Lagos'
            country TEXT NOT NULL, country_code TEXT NOT NULL,
            region TEXT, city TEXT NOT NULL, community TEXT,
            region_key TEXT, city_key TEXT NOT NULL, community_key TEXT,
            area_key TEXT,                       -- normalised label; NULL for whole-city subscriptions
            lat REAL, lon REAL,
            tz TEXT, tz_offset INTEGER,
            episode_active INTEGER NOT NULL DEFAULT 0,
            last_alert_score REAL,
            last_alert_at TEXT,
            last_alert_level TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (subscriber_id) REFERENCES telegram_subscribers(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_loc_unique
            ON telegram_locations(subscriber_id, country_code, IFNULL(region_key,''), city_key, IFNULL(community_key, ''));
        CREATE TABLE IF NOT EXISTS telegram_pending (
            token TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS telegram_alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            chat_id TEXT,
            subscriber_id INTEGER,
            location_id INTEGER,
            area_label TEXT,
            country_code TEXT,
            kind TEXT NOT NULL,        -- alert | urgent | test | system | stale_skip | test_mode_skip
            status TEXT NOT NULL,      -- sent | failed | skipped
            score REAL,
            threshold REAL,
            data_age_minutes REAL,
            mode TEXT,
            error TEXT,
            message_id INTEGER,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tg_log_created ON telegram_alert_log(created_at);
        CREATE INDEX IF NOT EXISTS idx_tg_log_chat ON telegram_alert_log(chat_id, created_at);
        CREATE TABLE IF NOT EXISTS telegram_updates (
            update_id INTEGER PRIMARY KEY,
            received_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def log_event(kind, status, *, chat_id=None, subscriber_id=None, location_id=None, area_label=None, country_code=None,
              score=None, threshold=None, data_age=None, error=None, message_id=None, note=None, mode=None):
    try:
        conn = _conn()
        conn.execute(
            """INSERT INTO telegram_alert_log
               (created_at, chat_id, subscriber_id, location_id, area_label, country_code, kind, status, score, threshold,
                data_age_minutes, mode, error, message_id, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now().isoformat(), chat_id, subscriber_id, location_id, area_label, country_code, kind, status, score,
             ALERT_THRESHOLD if threshold is None else threshold, data_age, mode or MODE,
             (error or None) and str(error)[:500], message_id, note),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as db_error:
        print(f"telegram log_event failed: {db_error}")


def _recently_logged(kind, area_label, minutes):
    conn = _conn()
    cutoff = (_now() - timedelta(minutes=minutes)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM telegram_alert_log WHERE kind=? AND area_label=? AND created_at>=? LIMIT 1",
        (kind, area_label, cutoff),
    ).fetchone()
    conn.close()
    return bool(row)


# ---------------------------------------------------------------------------
# Telegram Bot API
# ---------------------------------------------------------------------------
class TelegramError(Exception):
    def __init__(self, message, status=None, retry_after=None, blocked=False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.blocked = blocked


def _scrub(text):
    text = str(text)
    if BOT_TOKEN:
        text = text.replace(BOT_TOKEN, "***")
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***", text)


def api(method, payload=None, timeout=12):
    if not BOT_TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")
    try:
        resp = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload or {}, timeout=timeout)
    except requests.RequestException as error:
        raise TelegramError(_scrub(error))
    try:
        data = resp.json()
    except ValueError:
        raise TelegramError(f"Telegram returned non-JSON (HTTP {resp.status_code})", status=resp.status_code)
    if resp.status_code == 200 and data.get("ok"):
        return data.get("result")
    description = data.get("description", f"HTTP {resp.status_code}")
    params = data.get("parameters") or {}
    blocked = resp.status_code == 403 or "bot was blocked" in description.lower() or "user is deactivated" in description.lower() \
        or "chat not found" in description.lower()
    raise TelegramError(_scrub(description), status=resp.status_code, retry_after=params.get("retry_after"), blocked=blocked)


def send_message(chat_id, text, *, buttons=None):
    payload = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "url": u} for t, u in buttons]]}
    result = api("sendMessage", payload)
    return result.get("message_id") if isinstance(result, dict) else None


def bot_username():
    global BOT_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    try:
        BOT_USERNAME = (api("getMe") or {}).get("username", "") or ""
    except TelegramError as error:
        print(f"telegram getMe failed: {error}")
    return BOT_USERNAME


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=False)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
def _age_text(minutes):
    if minutes is None or minutes < 2:
        return "just now"
    if minutes < 90:
        return f"{int(minutes)} min ago"
    return f"{minutes / 60:.1f} h ago"


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def build_alert_message(prediction, *, kind, previous_score=None, streets=None, street_assessment=None,
                        data_age=None, test=False, mode_note=None, tz_name=None, tz_offset=None, threshold=None):
    area = prediction.get("area") or {}
    threshold = street_risk.threshold_for(area.get("country_code")) if threshold is None else threshold
    score = int(round(prediction["score"]))
    label = prediction["city"]
    level = prediction.get("risk", "")
    if kind == "urgent":
        head = f"🚨 <b>URGENT UPDATE — {esc(label)}</b>"
        trend = (f"Modelled flood-risk score rose from <b>{int(round(previous_score))}</b> to <b>{score}</b> out of 100"
                 if previous_score is not None else f"Modelled flood-risk score is now <b>{score}</b> out of 100")
    else:
        head = f"🌧️ <b>FloodGuard alert — {esc(label)}</b>"
        trend = f"Modelled flood-risk score: <b>{score}</b> out of 100 (alert threshold {int(threshold)})"
    lines = []
    if test:
        lines.append("🧪 <b>TEST MESSAGE — not a real alert</b>")
    lines.append(head)
    where = ", ".join(x for x in (area.get("region"), area.get("country")) if x and norm(x) not in norm(label))
    if where:
        lines.append(esc(where))
    lines.append(trend + (f" · level <b>{esc(level)}</b>" if level else ""))
    lines.append("<i>This is a model score, not a calibrated probability and not a confirmed flood report.</i>")

    official = prediction.get("official_alerts") or []
    if official:
        lines.append("")
        lines.append("<b>Official alerts for this area:</b>")
        for a in official[:2]:
            lines.append(f"• {esc(a['source'])}: {esc(a['event'])} ({esc(a['severity'])})")
        lines.append("<i>Official alerts cover a warning area, not specific streets.</i>")

    factors = [f for f in (prediction.get("factors") or [])[:3] if f]
    if factors:
        lines.append("")
        lines.append("<b>Why:</b>")
        lines.extend(f"• {esc(f)}" for f in factors)
    if prediction.get("ground_alert"):
        lines.append(f"• {esc(prediction['ground_alert']['message'])} (visitor reports, unverified)")

    lines.append("")
    if streets:
        lines.append("<b>Streets most at risk (modelled):</b>")
        for i, s in enumerate(streets, 1):
            nm = s["name"] + (f" ({s['name_en']})" if s.get("name_en") and s["name_en"] != s["name"] else "")
            lines.append(f"{i}. {esc(nm)} — {s['risk_score']}/100 ({esc(s['severity_label'])})")
        if street_assessment and street_assessment.get("data_age_minutes") is not None:
            lines.append(f"<i>Street data from {_age_text(street_assessment['data_age_minutes'])}; map and details on the site.</i>")
    else:
        lines.append("<i>No street-level breakdown is available for this area right now; this alert is for the whole area.</i>")

    action = prediction.get("priority_action")
    if action:
        lines.append("")
        lines.append(f"<b>What to do:</b> {esc(action)}")
    safety = _deps.get("safety_info")
    safety_text = safety(area) if safety else None
    lines.append(esc(safety_text) if safety_text else "Know your local emergency number and follow local authorities.")

    checked = _parse_dt(prediction.get("assessed_at")) or _now()
    lines.append("")
    lines.append(f"🕒 Data checked {_age_text(data_age)} · {esc(local_time_text(checked, tz_name, tz_offset))}")
    lines.append("Sources: OpenWeather, satellite rainfall, terrain model, OpenStreetMap" + (", official agencies as listed" if official else ""))
    if mode_note:
        lines.append(esc(mode_note))
    lines.append("")
    lines.append("/pause to mute · /stop to unsubscribe · /list to manage places")
    return "\n".join(lines)


def _buttons(prediction):
    base = (_deps.get("site_url") or "").rstrip("/")
    if not base:
        return None
    return [("Open FloodGuard", f"{base}/?location={requests.utils.quote(prediction['city'])}")]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def data_age_minutes(prediction):
    """Minutes since this prediction was computed (prediction["assessed_at"]).
    Without a timestamp we cannot prove freshness and return None (treated as stale)."""
    dt = _parse_dt(prediction.get("assessed_at"))
    if not dt:
        return None
    return max(0.0, (_now() - dt).total_seconds() / 60.0)


def matching_locations(prediction_or_area, conn=None):
    """Subscriber locations that cover this area. Accepts a prediction (uses
    prediction['area']) or an area dict."""
    area = prediction_or_area.get("area") if "area" in prediction_or_area else prediction_or_area
    ident = area_identity(area)
    own = conn is None
    conn = conn or _conn()
    rows = conn.execute(
        """SELECT tl.*, ts.chat_id, ts.status AS sub_status, ts.urgent_updates
           FROM telegram_locations tl JOIN telegram_subscribers ts ON ts.id = tl.subscriber_id
           WHERE tl.country_code = ?""",
        (ident["country_code"],),
    ).fetchall()
    if own:
        conn.close()
    return [r for r in rows if row_matches_area(r, ident)]


def decide(row, score, now=None, threshold=None):
    """Pure decision function (unit-tested): 'alert', 'urgent', 'clear' or None."""
    now = now or _now()
    threshold = ALERT_THRESHOLD if threshold is None else threshold
    if score < threshold:
        if row["episode_active"] and score < threshold - CLEAR_MARGIN:
            return "clear"
        return None
    last_at = datetime.fromisoformat(row["last_alert_at"]) if row["last_alert_at"] else None
    if not row["episode_active"]:
        if last_at and now - last_at < timedelta(hours=COOLDOWN_HOURS):
            return None
        return "alert"
    if (row["urgent_updates"] and row["last_alert_score"] is not None
            and score >= row["last_alert_score"] + ESCALATION_DELTA
            and (not last_at or now - last_at >= timedelta(minutes=URGENT_MIN_GAP_MINUTES))):
        return "urgent"
    return None


def _claim(conn, row, score, level, now_iso):
    """Atomically move a location into the 'alerted' state. True only for the one
    worker that wins, so duplicate sends are impossible even across processes."""
    cur = conn.execute(
        """UPDATE telegram_locations
           SET episode_active=1, last_alert_score=?, last_alert_level=?, last_alert_at=?
           WHERE id=? AND episode_active IS ? AND IFNULL(last_alert_at,'') = IFNULL(?, '')""",
        (score, level, now_iso, row["id"], row["episode_active"], row["last_alert_at"]),
    )
    conn.commit()
    return cur.rowcount == 1


def _revert(conn, row):
    conn.execute(
        "UPDATE telegram_locations SET episode_active=?, last_alert_score=?, last_alert_level=?, last_alert_at=? WHERE id=?",
        (row["episode_active"], row["last_alert_score"], row["last_alert_level"], row["last_alert_at"], row["id"]),
    )
    conn.commit()


def dispatch_alerts_for_prediction(prediction, now=None):
    """Entry point called from the watchlist sweep. Returns a small stats dict."""
    stats = {"matched": 0, "sent": 0, "failed": 0, "skipped": 0}
    if not enabled() or not prediction or not prediction.get("city"):
        return stats
    area = prediction.get("area")
    if not area or not area.get("country_code"):
        return stats  # cannot target without a known country; never guess
    label = prediction["city"]
    cc = area["country_code"].upper()
    threshold = street_risk.threshold_for(cc)
    score = float(prediction.get("score") or 0)
    now = now or _now()
    age = data_age_minutes(prediction)

    with _dispatch_lock:
        conn = _conn()
        try:
            rows = [r for r in matching_locations(prediction, conn) if r["sub_status"] == "active"]
            stats["matched"] = len(rows)

            if age is not None and age <= MAX_DATA_AGE_MINUTES:
                for r in rows:
                    if decide(r, score, now, threshold) == "clear":
                        conn.execute("UPDATE telegram_locations SET episode_active=0 WHERE id=?", (r["id"],))
                conn.commit()

            if score < threshold or not rows:
                return stats

            if age is None or age > MAX_DATA_AGE_MINUTES:
                stats["skipped"] = len(rows)
                if not _recently_logged("stale_skip", label, 60):
                    log_event("stale_skip", "skipped", area_label=label, country_code=cc, score=score, threshold=threshold,
                              data_age=age, note=f"Data older than {int(MAX_DATA_AGE_MINUTES)} min or of unknown age; no alert sent")
                return stats

            summary_fn = _deps.get("street_summary")
            streets, assessment = ([], None)
            if summary_fn:
                try:
                    streets, assessment = summary_fn(prediction)
                except Exception as error:  # noqa: BLE001
                    print(f"telegram street summary failed: {_scrub(error)}")

            if not live():
                stats["skipped"] = len(rows)
                if not _recently_logged("test_mode_skip", label, 60):
                    log_event("test_mode_skip", "skipped", area_label=label, country_code=cc, score=score, threshold=threshold,
                              data_age=age, note=f"TEST MODE: {len(rows)} subscriber location(s) would be alerted")
                    for admin_chat in ADMIN_CHAT_IDS:
                        text = build_alert_message(prediction, kind="alert", streets=streets, street_assessment=assessment,
                                                   data_age=age, test=True, tz_name=area.get("timezone"),
                                                   tz_offset=area.get("tz_offset"), threshold=threshold,
                                                   mode_note=f"[Test mode] Would notify {len(rows)} subscriber location(s).")
                        _send_logged(admin_chat, None, None, prediction, text, kind="test", age=age)
                return stats

            for r in rows:
                action = decide(r, score, now, threshold)
                if action not in ("alert", "urgent"):
                    continue
                if not _claim(conn, r, score, prediction.get("risk"), now.isoformat()):
                    stats["skipped"] += 1
                    continue
                text = build_alert_message(prediction, kind=action, previous_score=r["last_alert_score"], streets=streets,
                                           street_assessment=assessment, data_age=age, tz_name=r["tz"] or area.get("timezone"),
                                           tz_offset=r["tz_offset"] if r["tz_offset"] is not None else area.get("tz_offset"),
                                           threshold=threshold)
                ok = _send_logged(r["chat_id"], r["subscriber_id"], r["id"], prediction, text, kind=action, age=age)
                if ok is True:
                    stats["sent"] += 1
                else:
                    stats["failed"] += 1
                    _revert(conn, r)
                    if ok == "blocked":
                        conn.execute("UPDATE telegram_subscribers SET status='blocked' WHERE id=?", (r["subscriber_id"],))
                        conn.commit()
                time.sleep(SEND_PAUSE_SECONDS)
        finally:
            conn.close()
    return stats


def _send_logged(chat_id, subscriber_id, location_id, prediction, text, *, kind, age):
    """Send + log. Returns True, False, or 'blocked'."""
    cc = (prediction.get("area") or {}).get("country_code")
    try:
        message_id = send_message(chat_id, text, buttons=_buttons(prediction))
        log_event(kind, "sent", chat_id=chat_id, subscriber_id=subscriber_id, location_id=location_id,
                  area_label=prediction["city"], country_code=cc, score=prediction.get("score"), data_age=age, message_id=message_id)
        return True
    except TelegramError as error:
        if error.retry_after:
            time.sleep(min(float(error.retry_after), 5))
        log_event(kind, "failed", chat_id=chat_id, subscriber_id=subscriber_id, location_id=location_id,
                  area_label=prediction["city"], country_code=cc, score=prediction.get("score"), data_age=age, error=error)
        return "blocked" if error.blocked else False


def send_admin_test(chat_id, area_label, score, *, prediction=None, streets=None, tz_name=None, tz_offset=None):
    """Admin-only test: one clearly-labelled TEST message to one chat, built with
    the real message builder."""
    pred = dict(prediction or {})
    pred.setdefault("city", area_label)
    pred["score"] = score
    pred.setdefault("risk", "WATCH" if score < 45 else "HIGH")
    pred.setdefault("factors", ["Test message generated by an administrator"])
    pred.setdefault("priority_action", "No action needed - this is a test.")
    pred.setdefault("assessed_at", _now().isoformat())
    text = build_alert_message(pred, kind="alert", streets=streets or [], data_age=0, test=True,
                               mode_note=f"[Mode: {MODE}]", tz_name=tz_name, tz_offset=tz_offset)
    return _send_logged(chat_id, None, None, pred, text, kind="test", age=0) is True


# ---------------------------------------------------------------------------
# Registration (website -> bot)
# ---------------------------------------------------------------------------
def create_pending(area):
    token = secrets.token_urlsafe(16)
    conn = _conn()
    conn.execute("DELETE FROM telegram_pending WHERE expires_at < ?", (_now().isoformat(),))
    conn.execute(
        "INSERT INTO telegram_pending (token, payload, created_at, expires_at) VALUES (?,?,?,?)",
        (token, json.dumps(area), _now().isoformat(), (_now() + timedelta(hours=PENDING_TOKEN_HOURS)).isoformat()),
    )
    conn.commit()
    conn.close()
    return token


def deep_link(token):
    user = bot_username()
    return f"https://t.me/{user}?start={token}" if user else None


def _consume_pending(token):
    conn = _conn()
    row = conn.execute("SELECT * FROM telegram_pending WHERE token=?", (token,)).fetchone()
    if not row or row["used_at"] or row["expires_at"] < _now().isoformat():
        conn.close()
        return None
    conn.execute("UPDATE telegram_pending SET used_at=? WHERE token=? AND used_at IS NULL", (_now().isoformat(), token))
    conn.commit()
    conn.close()
    return json.loads(row["payload"])


def upsert_subscriber(chat, user=None):
    conn = _conn()
    now = _now().isoformat()
    row = conn.execute("SELECT * FROM telegram_subscribers WHERE chat_id=?", (str(chat),)).fetchone()
    if row:
        conn.execute("UPDATE telegram_subscribers SET last_interaction_at=?, username=COALESCE(?,username), "
                     "first_name=COALESCE(?,first_name), status=CASE WHEN status='blocked' THEN 'active' ELSE status END WHERE id=?",
                     (now, (user or {}).get("username"), (user or {}).get("first_name"), row["id"]))
        sid = row["id"]
    else:
        cur = conn.execute("INSERT INTO telegram_subscribers (chat_id, username, first_name, created_at, last_interaction_at) VALUES (?,?,?,?,?)",
                           (str(chat), (user or {}).get("username"), (user or {}).get("first_name"), now, now))
        sid = cur.lastrowid
    conn.commit()
    conn.close()
    return sid


def add_location(subscriber_id, area):
    """area: label, country, country_code, region, city, community, lat, lon, timezone, tz_offset.
    Returns (location_id, created: bool, error: str|None)."""
    conn = _conn()
    count = conn.execute("SELECT COUNT(*) FROM telegram_locations WHERE subscriber_id=?", (subscriber_id,)).fetchone()[0]
    cc = (area["country_code"] or "").upper()
    region_key = norm_region(area.get("region")) or None
    city_key = norm(area["city"])
    community_key = norm(area["community"]) if area.get("community") else None
    existing = conn.execute(
        "SELECT id FROM telegram_locations WHERE subscriber_id=? AND country_code=? AND IFNULL(region_key,'')=? "
        "AND city_key=? AND IFNULL(community_key,'')=?",
        (subscriber_id, cc, region_key or "", city_key, community_key or ""),
    ).fetchone()
    if existing:
        conn.close()
        return existing["id"], False, None
    if count >= MAX_LOCATIONS_PER_SUBSCRIBER:
        conn.close()
        return None, False, f"You can follow up to {MAX_LOCATIONS_PER_SUBSCRIBER} places. Use /remove to free a slot."
    cur = conn.execute(
        """INSERT INTO telegram_locations (subscriber_id, label, country, country_code, region, city, community, region_key,
                                           city_key, community_key, area_key, lat, lon, tz, tz_offset, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (subscriber_id, area["label"], area.get("country") or country_name(cc), cc, area.get("region"), area["city"],
         area.get("community"), region_key, city_key, community_key,
         norm(area["label"]) if community_key else None, area.get("lat"), area.get("lon"),
         area.get("timezone"), area.get("tz_offset"), _now().isoformat()),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid, True, None


def known_place_for_label(label):
    """Verified coordinates + metadata for a subscriber-chosen community, so the
    sweep never depends on the geocoder returning the same place twice."""
    conn = _conn()
    row = conn.execute("SELECT * FROM telegram_locations WHERE area_key=? AND lat IS NOT NULL LIMIT 1", (norm(label),)).fetchone()
    conn.close()
    if not row:
        return None
    return {"lat": row["lat"], "lon": row["lon"], "name": row["community"] or row["city"], "label": row["label"],
            "state": row["region"] or "", "country": row["country"], "country_code": row["country_code"],
            "city": row["city"], "timezone": row["tz"], "tz_offset": row["tz_offset"]}


def extra_monitored_labels(limit=None):
    """Community-level places followers asked for, swept like any curated place.
    Capped (TELEGRAM_MAX_EXTRA_AREAS) to protect the free API quotas."""
    limit = MAX_EXTRA_AREAS if limit is None else limit
    conn = _conn()
    rows = conn.execute(
        "SELECT label FROM telegram_locations WHERE area_key IS NOT NULL GROUP BY area_key ORDER BY COUNT(*) DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [r["label"] for r in rows]


# ---------------------------------------------------------------------------
# Webhook / commands
# ---------------------------------------------------------------------------
def help_text():
    return (
        "<b>FloodGuard AI flood alerts</b>\n"
        "I message you when the modelled flood-risk score for a place you follow reaches the alert level "
        f"(default <b>{int(ALERT_THRESHOLD)}</b> out of 100; a model score, not a probability), and send an urgent "
        "update if it rises sharply. Times are shown in your place's local time zone.\n\n"
        "/add <i>place, country</i> — e.g. <code>/add Lekki, Lagos, Nigeria</code> or <code>/add Houston, United States</code>\n"
        "/list — your places\n"
        "/remove <i>number</i> — stop following one place\n"
        "/status — latest score for your places\n"
        "/history — your recent alerts\n"
        "/urgent on|off — urgent updates\n"
        "/pause · /resume — mute or unmute\n"
        "/stop — unsubscribe and delete your data\n\n"
        "Alerts are predictions. In an emergency call your local emergency number."
    )


def _reply(chat_id, text, buttons=None):
    try:
        send_message(chat_id, text, buttons=buttons)
    except TelegramError as error:
        log_event("system", "failed", chat_id=str(chat_id), error=error, note="reply failed")


def _subscriber(chat_id):
    conn = _conn()
    row = conn.execute("SELECT * FROM telegram_subscribers WHERE chat_id=?", (str(chat_id),)).fetchone()
    conn.close()
    return row


def _locations(subscriber_id):
    conn = _conn()
    rows = conn.execute("SELECT * FROM telegram_locations WHERE subscriber_id=? ORDER BY id", (subscriber_id,)).fetchall()
    conn.close()
    return rows


def _seen_update(update_id):
    if update_id is None:
        return False
    conn = _conn()
    try:
        conn.execute("INSERT INTO telegram_updates (update_id, received_at) VALUES (?,?)", (update_id, _now().isoformat()))
        conn.execute("DELETE FROM telegram_updates WHERE update_id < ?", (update_id - 2000,))
        conn.commit()
        return False
    except sqlite3.IntegrityError:
        return True
    finally:
        conn.close()


def handle_update(update):
    """Process one Telegram update. Never raises."""
    try:
        if _seen_update(update.get("update_id")):
            return
        member = update.get("my_chat_member")
        if member:
            if (member.get("new_chat_member") or {}).get("status") in ("kicked", "left"):
                conn = _conn()
                conn.execute("UPDATE telegram_subscribers SET status='blocked' WHERE chat_id=?", (str(member["chat"]["id"]),))
                conn.commit()
                conn.close()
            return
        message = update.get("message")
        if not message or "text" not in message:
            return
        chat = message["chat"]
        if chat.get("type") != "private":
            _reply(chat["id"], "Please message me in a private chat to manage flood alerts.")
            return
        _handle_command(chat["id"], message["text"].strip(), message.get("from") or {})
    except Exception as error:  # noqa: BLE001
        print(f"telegram handle_update error: {_scrub(error)}")


def _place_line(loc):
    tail = ", ".join(x for x in (loc["region"], loc["country"]) if x and norm(x) not in norm(loc["label"]))
    return esc(loc["label"] + (f" — {tail}" if tail else "") + ("" if loc["community"] else " (whole city)"))


def _handle_command(chat_id, text, user):
    m = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)$", text, re.S)
    cmd, arg = (m.group(1).lower(), m.group(2).strip()) if m else ("", text)
    subscriber = _subscriber(chat_id)
    if subscriber:
        upsert_subscriber(chat_id, user)

    if cmd == "start":
        if arg:
            area = _consume_pending(arg)
            if not area:
                _reply(chat_id, "That sign-up link has expired or was already used. Please create a new one on the FloodGuard website, or send <code>/add Lekki, Lagos, Nigeria</code> here.")
                return
            sid = upsert_subscriber(chat_id, user)
            _, created, err = add_location(sid, area)
            if err:
                _reply(chat_id, esc(err))
                return
            _reply(chat_id, (f"✅ You're subscribed to flood alerts for <b>{esc(area['label'])}</b> ({esc(area.get('country', ''))})."
                             if created else f"You were already following <b>{esc(area['label'])}</b>.")
                   + "\n\n" + mode_notice() + help_text())
            log_event("system", "sent", chat_id=str(chat_id), area_label=area["label"], country_code=area.get("country_code"), note="subscribed")
        else:
            upsert_subscriber(chat_id, user)
            _reply(chat_id, "Welcome to FloodGuard AI. Add a place with <code>/add Lekki, Lagos, Nigeria</code> or use the sign-up form on the website.\n\n" + help_text())
        return

    if cmd == "help" or (not cmd and not subscriber):
        _reply(chat_id, help_text())
        return

    if cmd == "add" and not subscriber:
        upsert_subscriber(chat_id, user)
        subscriber = _subscriber(chat_id)

    if not subscriber:
        _reply(chat_id, "You are not subscribed yet. Send <code>/add Lekki, Lagos, Nigeria</code> (use your own place) to begin.")
        return

    if cmd == "add":
        if not arg:
            _reply(chat_id, "Tell me the place and country, e.g. <code>/add Yaba, Lagos, Nigeria</code>.")
            return
        resolve = _deps.get("resolve_area")
        area, error = resolve(arg) if resolve else (None, "Place lookup is not available.")
        if not area:
            _reply(chat_id, esc(error or "I could not find that place. Try 'Place, Region, Country'."))
            return
        _, created, err = add_location(subscriber["id"], area)
        _reply(chat_id, esc(err) if err else (f"✅ Now following <b>{esc(area['label'])}</b> ({esc(area['country'])})." if created
                                               else f"You already follow <b>{esc(area['label'])}</b>."))
    elif cmd == "list":
        locs = _locations(subscriber["id"])
        if not locs:
            _reply(chat_id, "You are not following any place. Use /add.")
        else:
            body = "\n".join(f"{i}. {_place_line(l)}" for i, l in enumerate(locs, 1))
            _reply(chat_id, f"<b>Your places</b> (status: {esc(subscriber['status'])})\n{body}\n\n/remove <i>number</i> to stop following one.")
    elif cmd == "remove":
        locs = _locations(subscriber["id"])
        try:
            target = locs[int(arg) - 1]
        except (ValueError, IndexError):
            _reply(chat_id, "Send /list, then <code>/remove 2</code> (the number from the list).")
            return
        conn = _conn()
        conn.execute("DELETE FROM telegram_locations WHERE id=?", (target["id"],))
        conn.commit()
        conn.close()
        _reply(chat_id, f"Removed <b>{esc(target['label'])}</b>.")
    elif cmd == "pause":
        _set_status(subscriber["id"], "paused")
        _reply(chat_id, "⏸ Alerts paused. Send /resume to turn them back on.")
    elif cmd == "resume":
        _set_status(subscriber["id"], "active")
        _reply(chat_id, "▶️ Alerts resumed.")
    elif cmd == "urgent":
        on = arg.lower() not in ("off", "0", "no")
        conn = _conn()
        conn.execute("UPDATE telegram_subscribers SET urgent_updates=? WHERE id=?", (1 if on else 0, subscriber["id"]))
        conn.commit()
        conn.close()
        _reply(chat_id, "Urgent updates are now <b>%s</b>." % ("ON" if on else "OFF"))
    elif cmd == "status":
        _reply(chat_id, _status_text(subscriber))
    elif cmd == "history":
        conn = _conn()
        rows = conn.execute("SELECT * FROM telegram_alert_log WHERE chat_id=? AND status='sent' AND kind IN ('alert','urgent') "
                            "ORDER BY id DESC LIMIT 5", (str(chat_id),)).fetchall()
        conn.close()
        if not rows:
            _reply(chat_id, "No alerts have been sent to you yet.")
        else:
            locs = {l["label"]: l for l in _locations(subscriber["id"])}

            def when(r):
                loc = locs.get(r["area_label"])
                return local_time_text(_parse_dt(r["created_at"]), loc["tz"] if loc else None, loc["tz_offset"] if loc else None)
            _reply(chat_id, "<b>Your last alerts</b>\n" + "\n".join(
                f"• {esc(when(r))} — {esc(r['area_label'])}: {int(r['score'] or 0)}/100 ({esc(r['kind'])})" for r in rows))
    elif cmd == "stop":
        delete_subscriber(subscriber["id"])
        _reply(chat_id, "You are unsubscribed and your places have been deleted. Send /start to subscribe again.")
    else:
        _reply(chat_id, help_text())


def mode_notice():
    if live():
        return ""
    return "ℹ️ Alerts are currently in <b>test mode</b>: you are registered, and real alerts will begin when live notifications are switched on.\n\n"


def _set_status(subscriber_id, status):
    conn = _conn()
    conn.execute("UPDATE telegram_subscribers SET status=? WHERE id=?", (status, subscriber_id))
    conn.commit()
    conn.close()


def delete_subscriber(subscriber_id):
    conn = _conn()
    row = conn.execute("SELECT chat_id FROM telegram_subscribers WHERE id=?", (subscriber_id,)).fetchone()
    conn.execute("DELETE FROM telegram_locations WHERE subscriber_id=?", (subscriber_id,))
    conn.execute("DELETE FROM telegram_subscribers WHERE id=?", (subscriber_id,))
    if row:  # keep aggregate delivery stats, drop the personal identifier
        conn.execute("UPDATE telegram_alert_log SET chat_id=NULL, subscriber_id=NULL WHERE chat_id=?", (row["chat_id"],))
    conn.commit()
    conn.close()


def _status_text(subscriber):
    locs = _locations(subscriber["id"])
    if not locs:
        return "You are not following any place. Use /add."
    conn = _conn()
    lines = ["<b>Latest scores</b> (modelled)"]
    for loc in locs:
        row = conn.execute("SELECT score, risk_level, updated_at FROM watchlist_cache WHERE city_key=?",
                           (_deps["normalize_city"](loc["label"]),)).fetchone() if loc["community"] else None
        if row:
            updated = _parse_dt(row["updated_at"])
            age = (_now() - updated).total_seconds() / 60 if updated else None
            stale = " ⚠️ old data" if age is None or age > MAX_DATA_AGE_MINUTES else ""
            lines.append(f"• {esc(loc['label'])}: {row['score']}/100 ({esc(row['risk_level'])}) — {_age_text(age)}, "
                         f"{esc(local_time_text(updated, loc['tz'], loc['tz_offset']))}{stale}")
        else:
            lines.append(f"• {esc(loc['label'])}: no recent reading yet")
    conn.close()
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------
def admin_overview(log_limit=60):
    conn = _conn()
    subs = conn.execute("SELECT status, COUNT(*) c FROM telegram_subscribers GROUP BY status").fetchall()
    locs = conn.execute("SELECT COUNT(*) FROM telegram_locations").fetchone()[0]
    by_country = conn.execute("SELECT country_code, COUNT(*) c FROM telegram_locations GROUP BY country_code ORDER BY c DESC LIMIT 15").fetchall()
    log = conn.execute("SELECT * FROM telegram_alert_log ORDER BY id DESC LIMIT ?", (log_limit,)).fetchall()
    stats = conn.execute(
        "SELECT status, COUNT(*) c FROM telegram_alert_log WHERE kind IN ('alert','urgent','test') AND created_at >= ? GROUP BY status",
        ((_now() - timedelta(days=7)).isoformat(),),
    ).fetchall()
    conn.close()
    return {
        "configured": enabled(), "mode": MODE, "live": live(),
        "bot_username": bot_username() if enabled() else "",
        "threshold": ALERT_THRESHOLD, "country_thresholds": street_risk.COUNTRY_THRESHOLDS,
        "cooldown_hours": COOLDOWN_HOURS, "escalation_delta": ESCALATION_DELTA,
        "max_data_age_minutes": MAX_DATA_AGE_MINUTES,
        "webhook_secret_set": bool(WEBHOOK_SECRET),
        "admin_chats": len(ADMIN_CHAT_IDS),
        "subscribers": {r["status"]: r["c"] for r in subs},
        "locations": locs,
        "locations_by_country": {r["country_code"]: r["c"] for r in by_country},
        "delivery_7d": {r["status"]: r["c"] for r in stats},
        "log": [dict(r) for r in log],
    }