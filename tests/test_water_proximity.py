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