"""
official_sources.py - official flood alerts from public agencies, by country.

FloodGuard never invents official information. Each adapter below queries a
real public service and returns ONLY what that service says. Official alerts
are area-level (a warning area, not a street), so they are shown next to the
modelled street scores and never change those scores.

Coverage today
--------------
  * Worldwide     GDACS (UN / European Commission Global Disaster Alert and
                  Coordination System) flood events, read from the cache the
                  app already refreshes (no extra requests).
  * United States National Weather Service alerts (api.weather.gov), flood-
                  related products only. Public domain, free, no key; a
                  descriptive User-Agent is required.
  * England       Environment Agency flood warnings and alerts
                  (environment.data.gov.uk/flood-monitoring). Open Government
                  Licence v3, free, no key. England only (Scotland, Wales and
                  Northern Ireland publish through different agencies).

Anywhere else the app says plainly that no national official feed is connected
and relies on the modelled score plus GDACS. Adding a country = write one
function that returns a list of alert dicts and register it in ADAPTERS.

The NWS and Environment Agency adapters follow those services' published
formats but could not be exercised against the live services from the build
environment; they fail closed (no data shown) on any error or format change.
"""

import math
import time
from datetime import datetime

USER_AGENT = "(myfloodguard.online, info@myfloodguard.online)"
GDACS_RADIUS_KM = 150
CACHE_SECONDS = 600

_cache = {}


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Adapters. Each returns (alerts, ok). ok=False means the service could not
# be read (so "no alerts" must not be reported as "all clear").
# ---------------------------------------------------------------------------
def nws_alerts(lat, lon, http_get):
    try:
        resp = http_get(
            f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        )
        data = resp.json()
    except Exception as error:  # noqa: BLE001
        print(f"NWS alerts unavailable: {error}")
        return [], False
    out = []
    for feature in data.get("features", []):
        props = feature.get("properties", {}) or {}
        event = props.get("event") or ""
        if "flood" not in event.lower():
            continue
        out.append({
            "source": "US National Weather Service",
            "event": event,
            "severity": props.get("severity") or "Unknown",
            "headline": props.get("headline") or event,
            "issued": props.get("effective") or props.get("sent"),
            "expires": props.get("expires") or props.get("ends"),
            "area": props.get("areaDesc"),
            "url": props.get("@id") or feature.get("id"),
            "scope": "warning area",
            "licence": "US Government work (public domain)",
        })
    return out, True


_EA_LEVELS = {1: ("Severe flood warning", "Extreme"), 2: ("Flood warning", "Severe"), 3: ("Flood alert", "Moderate")}


def ea_england_alerts(lat, lon, http_get):
    try:
        resp = http_get(
            f"https://environment.data.gov.uk/flood-monitoring/id/floods?lat={lat:.4f}&long={lon:.4f}&dist=5",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        data = resp.json()
    except Exception as error:  # noqa: BLE001
        print(f"Environment Agency alerts unavailable: {error}")
        return [], False
    out = []
    for item in data.get("items", []):
        level = item.get("severityLevel")
        if level not in _EA_LEVELS:  # 4 = no longer in force
            continue
        event, severity = _EA_LEVELS[level]
        out.append({
            "source": "Environment Agency (England)",
            "event": event,
            "severity": severity,
            "headline": item.get("description") or event,
            "issued": item.get("timeSeverityChanged") or item.get("timeMessageChanged"),
            "expires": None,
            "area": (item.get("floodArea") or {}).get("county") or item.get("eaAreaName"),
            "url": item.get("@id") or item.get("floodAreaID"),
            "scope": "warning area",
            "licence": "Open Government Licence v3.0",
        })
    return out, True


ADAPTERS = {
    "US": [("US National Weather Service", nws_alerts)],
    "GB": [("Environment Agency (England)", ea_england_alerts)],
}


def gdacs_nearby(lat, lon, gdacs_rows):
    out = []
    for row in gdacs_rows or []:
        if row.get("latitude") is None or row.get("longitude") is None:
            continue
        if _haversine_km(lat, lon, row["latitude"], row["longitude"]) > GDACS_RADIUS_KM:
            continue
        out.append({
            "source": "GDACS (UN / European Commission)",
            "event": "Flood event",
            "severity": row.get("alert_level") or "Green",
            "headline": row.get("title") or "Flood event",
            "issued": row.get("published_at"),
            "expires": None,
            "area": row.get("country"),
            "url": row.get("event_url"),
            "scope": f"event within ~{GDACS_RADIUS_KM} km",
            "licence": "GDACS public data",
        })
    return out


def fetch_official_alerts(lat, lon, country_code, http_get, gdacs_rows=None, now=None):
    """Returns {"alerts": [...], "checked": [{"source", "status"}], "checked_at": iso}.
    status is 'ok', 'unavailable' (service error) or 'no national feed connected'."""
    cc = (country_code or "").upper()
    key = (round(lat, 2), round(lon, 2), cc)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    alerts, checked = [], []
    for name, adapter in ADAPTERS.get(cc, []):
        found, ok = adapter(lat, lon, http_get)
        alerts.extend(found)
        checked.append({"source": name, "status": "ok" if ok else "unavailable"})
    if cc not in ADAPTERS:
        checked.append({"source": f"National agency feed for {cc or 'this country'}", "status": "no national feed connected"})
    near = gdacs_nearby(lat, lon, gdacs_rows)
    alerts.extend(near)
    checked.append({"source": "GDACS (global)", "status": "ok" if gdacs_rows is not None else "unavailable"})

    result = {"alerts": alerts, "checked": checked, "checked_at": (now or datetime.utcnow()).isoformat()}
    _cache[key] = (time.time(), result)
    return result


def alert_lines(alerts, limit=2):
    """Short human lines for Telegram/UI."""
    lines = []
    for a in alerts[:limit]:
        lines.append(f"{a['source']}: {a['event']} ({a['severity']}) - {a['headline']}"[:220])
    return lines