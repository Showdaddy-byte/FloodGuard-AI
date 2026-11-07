import unittest
from unittest import mock
import app as flood_app

LAT, LON = -22.9100, -43.1700


def coast_way():
    # Long coastline ~300 m east of the pin; its centre is far away (~6 km).
    pts = [{"lat": LAT + i * 0.01, "lon": LON + 0.0027} for i in range(-6, 7)]
    return {"type": "way", "tags": {"natural": "coastline"}, "geometry": pts,
            "center": {"lat": LAT + 0.06, "lon": LON + 0.05}}



_REAL_GEOAPIFY_KEY = flood_app.GEOAPIFY_API_KEY


def setUpModule():
    flood_app.GEOAPIFY_API_KEY = None  # never call the real Geoapify from these tests


def tearDownModule():
    flood_app.GEOAPIFY_API_KEY = _REAL_GEOAPIFY_KEY


class WaterProximity(unittest.TestCase):
    def test_uses_geometry_not_far_centre(self):
        data = {"elements": [coast_way(), {"type": "count", "tags": {"total": "50"}}]}
        with mock.patch.object(flood_app, "query_overpass", return_value=data):
            nw, nc, b, pt, label, nopen = flood_app.fetch_water_and_urban_context(LAT, LON)
        self.assertLess(nw, 400)
        self.assertLess(nc, 400)
        self.assertLess(nopen, 400)

    def test_segment_distance_between_sparse_vertices(self):
        el = {"type": "way", "tags": {"natural": "coastline"},
              "geometry": [{"lat": LAT - 0.05, "lon": LON + 0.0027}, {"lat": LAT + 0.05, "lon": LON + 0.0027}]}
        p = flood_app._closest_point_on_element(LAT, LON, el)
        self.assertLess(flood_app.haversine_meters(LAT, LON, p[0], p[1]), 320)

    def test_nothing_nearby_stays_none(self):
        with mock.patch.object(flood_app, "query_overpass", return_value={"elements": [{"type": "count", "tags": {"total": "5"}}]}):
            self.assertIsNone(flood_app.fetch_water_and_urban_context(LAT, LON)[0])


if __name__ == "__main__":
    unittest.main()


class EmptyAnswerIsFailure(unittest.TestCase):
    def test_remark_without_elements_is_not_success(self):
        class R:
            def raise_for_status(self): pass
            def json(self): return {"elements": [], "remark": "runtime error: Query timed out"}
        with mock.patch.object(flood_app, "request_with_retry", return_value=R()):
            self.assertIsNone(flood_app.query_overpass("x"))


class Plausibility(unittest.TestCase):
    def test_no_water_at_sea_level_is_not_trusted(self):
        self.assertTrue(flood_app._water_result_implausible(1, None))
        self.assertFalse(flood_app._water_result_implausible(40, None))
        self.assertFalse(flood_app._water_result_implausible(1, 300))
        self.assertFalse(flood_app._water_result_implausible(None, None))

    def test_satellite_fallback_also_checked(self):
        self.assertTrue(flood_app._water_result_implausible(1, None, True))


class SeparatedLookups(unittest.TestCase):
    def test_partial_answer_with_remark_is_rejected(self):
        partial = {"elements": [coast_way()], "remark": "runtime error: Query timed out"}
        with mock.patch.object(flood_app, "query_overpass", return_value=partial):
            self.assertIsNone(flood_app.fetch_water_context(LAT, LON))

    def test_water_kept_when_building_request_fails(self):
        def fake(q):
            return {"elements": [coast_way()]} if "coastline" in q else None
        with mock.patch.object(flood_app, "query_overpass", side_effect=fake):
            r = flood_app.fetch_water_and_urban_context(LAT, LON, fallback_building_count=120)
        self.assertLess(r[0], 400)
        self.assertEqual(r[2], 120)

    def test_water_failure_returns_all_none(self):
        with mock.patch.object(flood_app, "query_overpass", return_value=None), \
             mock.patch.object(flood_app, "fetch_water_context_geoapify", return_value=None):
            self.assertEqual(flood_app.fetch_water_and_urban_context(LAT, LON), (None,) * 6)

    def test_no_result_cap_in_water_query(self):
        seen = []
        with mock.patch.object(flood_app, "query_overpass", side_effect=lambda q: seen.append(q) or {"elements": []}):
            flood_app.fetch_water_context(LAT, LON)
        self.assertNotIn("300;", seen[0])
        self.assertNotIn("building", seen[0])


class TravelVerdictWithGaps(unittest.TestCase):
    def test_low_with_missing_data_is_not_called_safe(self):
        r = flood_app.build_travel_recommendation("LOW", 20, None, data_incomplete=True)
        self.assertNotEqual(r["verdict"], "SAFE TO TRAVEL")
        self.assertEqual(r["verdict"], "NO FLOOD SIGNAL DETECTED")
        self.assertIn("limited", r["headline"])

    def test_low_with_full_data_unchanged(self):
        self.assertEqual(flood_app.build_travel_recommendation("LOW", 20, None)["verdict"], "SAFE TO TRAVEL")

    def test_higher_risk_unaffected(self):
        self.assertEqual(flood_app.build_travel_recommendation("HIGH", 60, None, data_incomplete=True)["verdict"], "TRAVEL WITH CAUTION")


class BroadArea(unittest.TestCase):
    def test_country_name_is_flagged(self):
        self.assertIn("whole country", flood_app.broad_area_note("United States"))
        self.assertIn("whole country", flood_app.broad_area_note("Nigeria"))
        self.assertIn("whole country", flood_app.broad_area_note("Anything", "country"))

    def test_state_type_is_flagged(self):
        self.assertIn("state or region", flood_app.broad_area_note("Lagos State", "state"))

    def test_cities_and_neighbourhoods_are_not(self):
        for name in ("Lagos", "Banana Island", "Rio de Janeiro", "Kano"):
            self.assertIsNone(flood_app.broad_area_note(name))


class OpenWater(unittest.TestCase):
    def test_oceans_and_seas_flagged(self):
        for n in ("North Atlantic Ocean", "Atlantic Ocean", "South China Sea", "Mediterranean Sea",
                  "Gulf of Guinea", "Bight of Benin", "North Sea, Europe", "Pacific Ocean"):
            self.assertTrue(flood_app.is_open_water_label(n), n)

    def test_land_places_not_flagged(self):
        for n in ("Lagos", "Banana Island", "Sea Point, Cape Town", "Seaside, Oregon", "Ocean City, Maryland",
                  "Bay of Plenty", "Hotel Ocean Breeze Lagos", "Rio de Janeiro", "United States"):
            self.assertFalse(flood_app.is_open_water_label(n), n)

    def test_address_types_never_flagged(self):
        self.assertFalse(flood_app.is_open_water_label("Blue Sea", "amenity"))

    def test_homepage_shows_message_not_score(self):
        client = flood_app.app.test_client()
        with mock.patch.object(flood_app, "API_KEY", "x"):
            r = client.get("/?map_lat=30&map_lon=-40&map_label=North+Atlantic+Ocean")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"open water", r.data)
        self.assertNotIn(b"WATCH FLOOD RISK", r.data)


class OpenWaterRule(unittest.TestCase):
    def test_ocean_like_point(self):
        self.assertTrue(flood_app._looks_like_open_water(0, None, 0))
        self.assertTrue(flood_app._looks_like_open_water(2, None, 0))

    def test_failed_building_lookup_never_qualifies(self):
        self.assertFalse(flood_app._looks_like_open_water(0, None, None))

    def test_towns_and_water_do_not_qualify(self):
        self.assertFalse(flood_app._looks_like_open_water(1, None, 250))   # Banana Island-style: buildings present
        self.assertFalse(flood_app._looks_like_open_water(1, 300, 0))      # water found
        self.assertFalse(flood_app._looks_like_open_water(40, None, 0))    # real inland ground
        self.assertFalse(flood_app._looks_like_open_water(None, None, 0))