"""Build data/world_emergency_numbers.json from data/world_emergency_raw.json.

Run from the project root:   python tools/build_world_emergency.py
Needs:  pip install pycountry   (build time only; the website does not need it)

The raw file holds the public-directory rows exactly as copied. This script
only normalises them: it splits "112 or 999" into a primary number plus
alternatives, removes spaces inside numbers, maps country names to ISO codes,
and refuses anything that is not a plain number. It never invents a number.
Entries it cannot map are listed at the end instead of being dropped silently.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from world_emergency import clean_number  # noqa: E402

RAW = os.path.join(ROOT, "data", "world_emergency_raw.json")
OUT = os.path.join(ROOT, "data", "world_emergency_numbers.json")

NAME_TO_CC = {
    "People's Republic of China": "CN", "The Bahamas": "BS", "Ivory Coast": "CI", "East Timor": "TL",
    "Republic of Congo": "CG", "Democratic Republic of Congo": "CD", "Curacao": "CW",
    "Caribbean Netherlands": "BQ", "United States of America": "US", "Taiwan": "TW", "Turkey": "TR", "Türkiye": "TR", "Macau": "MO", "Russia": "RU",
    "Vatican City": "VA", "Palestine": "PS", "Micronesia": "FM", "Turks and Caicos": "TC",
    "Cape Verde": "CV", "Syria": "SY", "Laos": "LA", "South Korea": "KR", "Falkland Islands": "FK",
    "Réunion": "RE", "Åland Islands": "AX", "Saint Barthélemy": "BL", "Saint Martin": "MF",
    "Sint Maarten": "SX", "Brunei": "BN", "Iran": "IR", "Moldova": "MD", "Tanzania": "TZ",
    "Vietnam": "VN", "Venezuela": "VE", "Bolivia": "BO", "Kosovo": "XK", "Eswatini": "SZ",
    "British Indian Ocean Territory": "IO", "Cocos (Keeling) Islands": "CC", "Christmas Island": "CX",
    "Saint Pierre and Miquelon": "PM", "U.S. Virgin Islands": "VI", "Saint Helena": "SH",
    "South Georgia and the South Sandwich Islands": "GS", "Wallis and Futuna": "WF",
}
SKIP = {"Ascension Island", "Tristan da Cunha", "Akrotiri and Dhekelia", "Northern Cyprus", "Clipperton Island"}


def country_code(name):
    if name in NAME_TO_CC:
        return NAME_TO_CC[name]
    try:
        import pycountry
        return pycountry.countries.lookup(name).alpha_2
    except Exception:  # noqa: BLE001
        return None


def tokens(cell):
    """'112 or 999' -> ['112','999'] (valid numbers only, order kept)."""
    out = []
    for part in re.split(r"\s+(?:or|and)\s+|/", cell or ""):
        n = clean_number(part)
        if n and n not in out:
            out.append(n)
    return out


def pick(toks):
    """Return (role_number, general_number, extras) for one directory cell.
    Rule (keeps wrong numbers out): one number -> that number is the role
    number, or the general number when it is 112. Exactly two numbers where
    one is 112 -> 112 is general, the other is the role number. Anything
    longer is ambiguous: keep 112 as general if present, list the rest as
    'also listed' text, and do NOT choose a role number."""
    if not toks:
        return None, None, []
    if len(toks) == 1:
        return (None, toks[0], []) if toks[0] == "112" else (toks[0], None, [])
    if len(toks) == 2 and "112" in toks:
        other = [t for t in toks if t != "112"][0]
        if other == "911":  # 911 beside 112 is a redirect alias, not a separate service
            return None, "112", ["911"]
        return other, "112", []
    return None, ("112" if "112" in toks else None), [t for t in toks if t != "112"]


def build_entry(row):
    main, amb, fire = tokens(row.get("main")), tokens(row.get("ambulance")), tokens(row.get("fire"))
    entry, notes = {}, []
    only_main = bool(main) and not amb and not fire
    for field, toks in (("police", main), ("ambulance", amb), ("fire", fire)):
        role, gen, extras = pick(toks)
        if only_main and role:
            entry["general"] = role
        elif role:
            entry[field] = role
        if gen and not entry.get("general"):
            entry["general"] = gen
        if extras:
            notes.append(f"{field.capitalize()} also listed: " + ", ".join(extras) + ".")
    if only_main and not entry.get("general") and main:
        entry["general"] = main[0]
    other = (row.get("other") or "").strip()
    if other:
        notes.append(other if other.endswith((".", ")", ";")) else other + ".")
    if notes:
        entry["notes"] = " ".join(notes)[:400]
    unparsed = [c for c in (row.get("main"), row.get("ambulance"), row.get("fire")) if c and not tokens(c)]
    if unparsed:
        entry["notes"] = ((entry.get("notes", "") + " ") + "Directory entry: " + "; ".join(unparsed) + ".").strip()
    return entry


def main():
    raw = json.load(open(RAW, encoding="utf-8"))
    countries, unmapped, empty = {}, [], []
    for row in raw["rows"]:
        name = row["country"]
        if name in SKIP:
            unmapped.append(name)
            continue
        cc = country_code(name)
        if not cc:
            unmapped.append(name)
            continue
        entry = build_entry(row)
        if not any(entry.get(k) for k in ("general", "police", "ambulance", "fire")):
            empty.append(name)
            continue
        entry.update(name=name, region=row.get("region"), tier="reference",
                     source=raw["source"], retrieved_on=raw["retrieved_on"])
        countries[cc] = entry
    out = {"source": raw["source"], "retrieved_on": raw["retrieved_on"], "countries": countries}
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"Wrote {len(countries)} countries to {OUT}")
    if unmapped:
        print("Not mapped to a country code (left out):", ", ".join(unmapped))
    if empty:
        print("No dialable number found (left out):", ", ".join(empty))


if __name__ == "__main__":
    main()