"""Rainfall aggregation helpers for FloodGuard AI.

This module turns the raw per-source rainfall numbers already being
fetched elsewhere in app.py (OpenWeather's current observation and
3-hour forecast timeline, Earth Engine's GPM IMERG satellite estimate,
and CHIRPS historical accumulation) into one consistent "rainfall
state" structure that the rest of the app can read from a single
place, with explicit data-quality flags rather than silently treating
missing data as zero rainfall.

Two entry points are used by app.py:

- calculate_forecast_rainfall_windows(timeline): rolls the 3-hour
  OpenWeather forecast timeline up into standard forward-looking
  windows (next 3h / 6h / 12h / 24h).
- build_rainfall_state(...): assembles the current/forecast/historical
  rainfall picture plus a data_quality flag for each section. The
  caller (app.py) then fills in the satellite-specific fields itself,
  since those come from Earth Engine, not this module.
"""

# Each forecast window is defined by how many leading 3-hour timeline
# slots it covers. OpenWeather's forecast timeline (built in
# get_forecast()) is already ordered from "soonest" to "furthest out",
# so the first N slots are exactly the next N*3 hours.
_FORECAST_WINDOW_SLOTS = {
    "next_3h_mm": 1,
    "next_6h_mm": 2,
    "next_12h_mm": 4,
    "next_24h_mm": 8,
}


def calculate_forecast_rainfall_windows(timeline):
    """Sum forecast rainfall (mm) into standard forward-looking windows.

    `timeline` is the list built by get_forecast() in app.py — each
    item is one 3-hour OpenWeather forecast slot with a "rain" key
    (mm of rain in that 3-hour slot). Returns a dict with one key per
    window (see _FORECAST_WINDOW_SLOTS) plus a "coverage" dict saying
    how many of the needed slots were actually available for each
    window, so a window built from incomplete data can be flagged as
    partial rather than presented as a complete total.
    """
    windows = {}
    coverage = {}

    if not timeline:
        for window_name in _FORECAST_WINDOW_SLOTS:
            windows[window_name] = None
            coverage[window_name] = {"available_slots": 0, "needed_slots": _FORECAST_WINDOW_SLOTS[window_name]}
        return {"windows": windows, "coverage": coverage}

    for window_name, needed_slots in _FORECAST_WINDOW_SLOTS.items():
        slots = timeline[:needed_slots]
        available = [slot.get("rain") for slot in slots if slot.get("rain") is not None]

        coverage[window_name] = {
            "available_slots": len(available),
            "needed_slots": needed_slots,
        }

        if not available:
            windows[window_name] = None
        else:
            # Partial coverage still gets a real sum (it's a real, if
            # incomplete, forecast total) — callers use "coverage" to
            # decide whether to label it as partial.
            windows[window_name] = round(sum(available), 2)

    return {"windows": windows, "coverage": coverage}


def _forecast_quality(forecast_windows):
    """Classify forecast rainfall data quality as available/partial/unavailable."""
    if not forecast_windows or not forecast_windows.get("coverage"):
        return "unavailable"

    coverage = forecast_windows["coverage"]
    total_needed = sum(c["needed_slots"] for c in coverage.values())
    total_available = sum(c["available_slots"] for c in coverage.values())

    if total_available <= 0:
        return "unavailable"
    if total_available < total_needed:
        return "partial"
    return "available"


def build_rainfall_state(current_rainfall, forecast_windows, historical, current_source, current_period_hours):
    """Assemble the combined current/forecast/historical rainfall state.

    Parameters:
        current_rainfall: mm of rain in the current observation window,
            or None if the active weather source didn't report any.
        forecast_windows: the dict returned by
            calculate_forecast_rainfall_windows().
        historical: dict with "24h", "72h", "7d", "30d" keys (any may
            be None if that layer wasn't available) — the 7d/30d
            values are expected to come from CHIRPS via Earth Engine,
            and the caller may overwrite them after this call returns.
        current_source: short label for where current_rainfall came
            from (e.g. "openweather", "weatherapi", "unknown").
        current_period_hours: how many hours current_rainfall covers
            (OpenWeather reports a rolling 1h figure for "rain").

    Returns a dict with "current", "forecast", "historical", and
    "data_quality" sections. The caller in app.py adds
    "satellite_recent" and "satellite" afterwards (those come from
    Earth Engine's GPM IMERG layer, which this module has no access
    to), and may overwrite historical/data_quality["historical"]
    once the CHIRPS figures are available.
    """
    historical = historical or {}
    windows = (forecast_windows or {}).get("windows", {}) if forecast_windows else {}

    current_quality = "observed" if current_rainfall is not None else "unavailable"
    forecast_quality = _forecast_quality(forecast_windows)

    historical_values = (historical.get("24h"), historical.get("72h"), historical.get("7d"), historical.get("30d"))
    historical_quality = "available" if any(value is not None for value in historical_values) else "unavailable"

    return {
        "current": {
            "value_mm": current_rainfall,
            "source": current_source or "unknown",
            "period_hours": current_period_hours,
            "quality": current_quality,
        },
        "forecast": {
            "next_3h_mm": windows.get("next_3h_mm"),
            "next_6h_mm": windows.get("next_6h_mm"),
            "next_12h_mm": windows.get("next_12h_mm"),
            "next_24h_mm": windows.get("next_24h_mm"),
            "quality": forecast_quality,
        },
        "historical": {
            "24h": historical.get("24h"),
            "72h": historical.get("72h"),
            "7d": historical.get("7d"),
            "30d": historical.get("30d"),
        },
        "data_quality": {
            "current": current_quality,
            "forecast": forecast_quality,
            "historical": historical_quality,
        },
    }