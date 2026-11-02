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