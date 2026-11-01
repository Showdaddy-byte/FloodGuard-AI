"""Offline tests for emergency_centre.py. Run: python -m unittest discover -s tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import emergency_centre as ec  # noqa: E402

LAT, LON = 6.5482, 3.3859


def node(i, lat, lon, **tags):
    return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": tags}


SAMPLE = {"elements": [
    node(1, LAT + 0.01, LON, amenity="hospital", name="Gbagada General Hospital", emergency="yes", phone="+234 803 000 0000; +234 1 111"),
    node(2, LAT + 0.002, LON, amenity="hospital", name="Sunrise Clinic", **{"operator:type": "private"}),
    {"type": "way", "id": 3, "center": {"lat": LAT + 0.02, "lon": LON}, "tags": {"amenity": "hospital", "name": "Lagos University Teaching Hospital"}},
    node(4, LAT + 0.003, LON, amenity="fire_station", name="Gbagada Fire Station", phone="767"),
    node(5, LAT + 0.004, LON, amenity="police", name="Police Division"),
    node(6, LAT + 0.005, LON, emergency="ambulance_station", name="Ambulance Base"),
    node(7, LAT + 0.006, LON, emergency="assembly_point"),
    node(8, LAT + 0.007, LON, amenity="school", name="Not emergency"),
    node(9, LAT + 0.0021, LON, amenity="hospital", name="Sunrise Clinic"),  # duplicate object near same spot
]}


class Phones(unittest.TestCase):
    def test_first_number_and_tel(self):
        self.assertEqual(ec.clean_phone("+234 803 000 0000; +234 1 111"), ("+234 803 000 0000", "+2348030000000"))

    def test_rejects_junk(self):
        for bad in (None, "", "n/a", "12", "1234567890123456789"):
            self.assertEqual(ec.clean_phone(bad), (None, None))


class Parse(unittest.TestCase):
    def setUp(self):
        self.f = ec.parse_facilities(SAMPLE, LAT, LON)

    def test_sorted_nearest_first_and_deduped(self):
        names = [h["name"] for h in self.f["hospital"]]
        self.assertEqual(names, ["Sunrise Clinic", "Gbagada General Hospital", "Lagos University Teaching Hospital"])

    def test_non_emergency_ignored(self):
        self.assertNotIn("Not emergency", str(self.f))

    def test_ownership_never_guessed(self):
        h = {x["name"]: x for x in self.f["hospital"]}
        self.assertTrue(h["Gbagada General Hospital"]["government"])
        self.assertIn("name suggests", h["Gbagada General Hospital"]["ownership"])
        self.assertFalse(h["Sunrise Clinic"]["government"])
        self.assertEqual(h["Lagos University Teaching Hospital"]["emergency_dept"], "Emergency department: not stated")
        self.assertIn("Teaching hospital (by name)", h["Lagos University Teaching Hospital"]["kinds"])
        self.assertTrue(h["Gbagada General Hospital"]["has_emergency_dept"])

    def test_phone_only_when_mapped(self):
        self.assertIsNone(self.f["police"][0]["tel"])
        self.assertEqual(self.f["fire"][0]["tel"], "767")

    def test_unnamed_labelled_honestly(self):
        self.assertFalse(self.f["shelter"][0]["named"])
        self.assertIn("unnamed", self.f["shelter"][0]["name"])

    def test_directions(self):
        d = self.f["hospital"][0]["directions"]
        self.assertIn("destination=", d["google"])
        self.assertIn("openstreetmap.org/directions", d["osm"])


class Agencies(unittest.TestCase):
    def test_lagos_has_state_and_national(self):
        names = [a["agency"] for a in ec.agencies_for("NG", "Lagos State")]
        self.assertIn("Lagos State Emergency Management Agency (LASEMA)", names)
        self.assertIn("National Emergency Number", names)

    def test_other_state_gets_national_only(self):
        names = [a["agency"] for a in ec.agencies_for("NG", "Kano State")]
        self.assertNotIn("Lagos State Fire and Rescue Service", names)

    def test_unknown_country_nothing_invented(self):
        self.assertEqual(ec.agencies_for("FR", "Vendee"), [])

    def test_no_number_means_no_number(self):
        nema = [a for a in ec.agencies_for("NG") if "NEMA" in a["agency"]][0]
        self.assertEqual(nema["numbers"], [])

    def test_nothing_claims_verified(self):
        for items in ec.CURATED_AGENCIES.values():
            for a in items:
                self.assertIsNone(a["verified_on"])


class Lookup(unittest.TestCase):
    def setUp(self):
        ec._cache.clear()

    def test_failure_without_cache_is_an_error_not_empty_list(self):
        r = ec.nearby_facilities(LAT, LON, lambda q: None)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["facilities"])

    def test_cache_then_stale_fallback(self):
        r1 = ec.nearby_facilities(LAT, LON, lambda q: SAMPLE, now=1000)
        self.assertTrue(r1["ok"] and not r1["stale"])
        r2 = ec.nearby_facilities(LAT, LON, lambda q: None, now=1000 + ec.CACHE_TTL_SECONDS + 600)
        self.assertTrue(r2["ok"] and r2["stale"])
        self.assertGreaterEqual(r2["age_minutes"], 360)

    def test_overpass_remark_is_not_treated_as_empty_success(self):
        r = ec.nearby_facilities(LAT, LON, lambda q: {"elements": [], "remark": "runtime error: out of memory"})
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()