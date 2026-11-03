import unittest
from unittest import mock
import app as flood_app

LAT, LON = -22.9100, -43.1700


def coast_way():
    # Long coastline ~300 m east of the pin; its centre is far away (~6 km).
    pts = [{"lat": LAT + i * 0.01, "lon": LON + 0.0027} for i in range(-6, 7)]
    return {"type": "way", "tags": {"natural": "coastline"}, "geometry": pts,
            "center": {"lat": LAT + 0.06, "lon": LON + 0.05}}


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
        with mock.patch.object(flood_app, "query_overpass", return_value=None):
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
        self.assertIn("not a confirmation", r["headline"])

    def test_low_with_full_data_unchanged(self):
        self.assertEqual(flood_app.build_travel_recommendation("LOW", 20, None)["verdict"], "SAFE TO TRAVEL")

    def test_higher_risk_unaffected(self):
        self.assertEqual(flood_app.build_travel_recommendation("HIGH", 60, None, data_incomplete=True)["verdict"], "TRAVEL WITH CAUTION")