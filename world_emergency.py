"""World emergency numbers: a static, hardcoded reference dataset.

Why this exists
---------------
FloodGuard shows emergency numbers beside flood results and in the Emergency
& Rescue Assistance centre. This module widens that from a handful of
curated countries to the whole world WITHOUT guessing: every number comes
from a recorded source, and every entry says how far it can be trusted.

Trust tiers (shown to visitors as plain wording, never hidden)
  verified   - curated in app.py (NATIONAL_EMERGENCY_NUMBERS) or confirmed by
               you in data/emergency_overrides.json with a `verified_on` date.
  reference  - copied from a public directory (data/world_emergency_raw.json,
               source and retrieved date recorded). Shown with the note
               "please confirm locally".

How to update later (no code changes needed)
  1. Correct or add one country: edit data/emergency_overrides.json
     (see the example at the top of that file).
  2. Refresh the whole reference set: replace the rows in
     data/world_emergency_raw.json, then run  python tools/build_world_emergency.py
     and commit data/world_emergency_numbers.json.
Nothing here is fetched at request time, so it works when every other
service is down.
"""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(_HERE, "data", "world_emergency_numbers.json")
OVERRIDES_PATH = os.path.join(_HERE, "data", "emergency_overrides.json")

_cache = {"world": None, "overrides": None, "mtimes": (None, None)}

_FIELDS = ("general", "police", "ambulance", "fire", "fire_ambulance")


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _load():
    mt = (_mtime(DATA_PATH), _mtime(OVERRIDES_PATH))
    if _cache["world"] is not None and _cache["mtimes"] == mt:
        return _cache["world"], _cache["overrides"]
    world, overrides = {}, {}
    try:
        with open(DATA_PATH, encoding="utf-8") as fh:
            world = (json.load(fh) or {}).get("countries", {})
    except (OSError, ValueError):
        world = {}
    try:
        with open(OVERRIDES_PATH, encoding="utf-8") as fh:
            raw = json.load(fh) or {}
        overrides = {k.upper(): v for k, v in raw.items() if isinstance(v, dict) and re.fullmatch(r"[A-Za-z]{2}", k)}
    except (OSError, ValueError):
        overrides = {}
    _cache.update(world=world, overrides=overrides, mtimes=mt)
    return world, overrides


_NUM_RE = re.compile(r"^\+?\d[\d\-]{0,14}$")


def clean_number(value):
    """Return a dialable number string, or None. Spaces are removed so
    '10 111' becomes '10111'. Anything that is not a plain number is rejected
    rather than guessed at."""
    v = re.sub(r"\s+", "", str(value or ""))
    return v if _NUM_RE.match(v) else None


def reference_numbers(country_code):
    """Return a dict shaped like NATIONAL_EMERGENCY_NUMBERS entries, plus
    `tier`, `source`, `retrieved_on` - or None when nothing is held."""
    cc = (country_code or "").upper()
    if not cc:
        return None
    world, overrides = _load()
    entry = dict(world.get(cc) or {})
    ov = overrides.get(cc)
    if ov:
        for f in _FIELDS:
            if f in ov:
                entry[f] = ov[f]
        for f in ("notes", "source", "verified_on", "agency"):
            if ov.get(f):
                entry[f] = ov[f]
        entry["tier"] = "verified" if ov.get("verified_on") else entry.get("tier", "reference")
    if not entry or not any(entry.get(f) for f in _FIELDS):
        return None
    entry.setdefault("tier", "reference")
    return entry


def with_reference_note(entry):
    """Append the plain-language trust note to `notes`, so every screen that
    already prints `notes` shows it without any template change."""
    out = dict(entry)
    if out.get("tier") == "reference":
        note = "Listed from a public directory" + (f" (checked {out['retrieved_on']})" if out.get("retrieved_on") else "") + \
               "; not yet confirmed by FloodGuard. Please confirm locally."
        out["notes"] = (out["notes"] + " " if out.get("notes") else "") + note
    elif out.get("tier") == "verified" and out.get("verified_on"):
        out["notes"] = (out["notes"] + " " if out.get("notes") else "") + f"Confirmed by FloodGuard on {out['verified_on']}."
    return out


def coverage():
    world, overrides = _load()
    return {"countries": len(world), "overrides": len(overrides)}