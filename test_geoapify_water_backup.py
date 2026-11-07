import unittest
from unittest import mock
import app as flood_app

LAT, LON = 6.4500, 3.4300


class Resp:
    def __init__(self, features):
        self._f = features
    def raise_for_status(self): pass
    def json(self): return {"features": self._f}


def feat(lat, lon, name="Lagos Lagoon"):
    return {"properties": {"name": name}, "geometry": {"coordinates": [lon, lat]}}


class GeoapifyBackup(unittest.TestCase):
    def setUp(self):
        self.key = mock.patch.object(flood_app, "GEOAPIFY_API_KEY", "test-key")
        self.key.start()
        flood_app._geoapify_backup_usage.update(day=None, count=0)
        flood_app._mark_service_up("geoapify-places")

    def tearDown(self):
        self.key.stop()

    def test_returns_distance_label_and_point(self):
        with mock.patch.object(flood_app, "request_with_retry", return_value=Resp([feat(LAT, LON + 0.003)])):
            d, coast, pt, label, open_m = flood_app.fetch_water_context_geoapify(LAT, LON)
        self.assertTrue(250 < d < 400)
        self.assertEqual(label, "Lagos Lagoon (water)")
        self.assertEqual(open_m, d)
        self.assertIsNotNone(coast)

    def test_nothing_found_is_unknown_not_no_water(self):
        with mock.patch.object(flood_app, "request_with_retry", return_value=Resp([])):
            self.assertIsNone(flood_app.fetch_water_context_geoapify(LAT, LON))

    def test_no_key_means_no_call(self):
        with mock.patch.object(flood_app, "GEOAPIFY_API_KEY", None), \
             mock.patch.object(flood_app, "request_with_retry") as r:
            self.assertIsNone(flood_app.fetch_water_context_geoapify(LAT, LON))
            r.assert_not_called()

    def test_daily_cap(self):
        flood_app._geoapify_backup_usage.update(day=flood_app.datetime.utcnow().strftime("%Y-%m-%d"),
                                                count=flood_app.GEOAPIFY_WATER_BACKUP_DAILY_CAP)
        with mock.patch.object(flood_app, "request_with_retry") as r:
            self.assertIsNone(flood_app.fetch_water_context_geoapify(LAT, LON))
            r.assert_not_called()

    def test_failure_returns_none(self):
        with mock.patch.object(flood_app, "request_with_retry", side_effect=flood_app.requests.ConnectionError("x")):
            self.assertIsNone(flood_app.fetch_water_context_geoapify(LAT, LON))

    def test_used_only_after_overpass_fails(self):
        with mock.patch.object(flood_app, "fetch_water_context", return_value=None), \
             mock.patch.object(flood_app, "fetch_water_context_geoapify",
                               return_value=(300.0, 300.0, (LAT, LON), "Lagos Lagoon (water)", 300.0)), \
             mock.patch.object(flood_app, "fetch_building_count") as b:
            out = flood_app.fetch_water_and_urban_context(LAT, LON, fallback_building_count=120)
        self.assertEqual(out[0], 300.0)
        self.assertEqual(out[2], 120)          # saved building count, no extra Overpass call
        b.assert_not_called()

    def test_overpass_success_never_calls_backup(self):
        ok = (200.0, None, (LAT, LON), "River (river)", 200.0)
        with mock.patch.object(flood_app, "fetch_water_context", return_value=ok), \
             mock.patch.object(flood_app, "fetch_building_count", return_value=50), \
             mock.patch.object(flood_app, "fetch_water_context_geoapify") as g:
            out = flood_app.fetch_water_and_urban_context(LAT, LON)
        g.assert_not_called()
        self.assertEqual(out[2], 50)

    def test_both_failing_returns_all_none(self):
        with mock.patch.object(flood_app, "fetch_water_context", return_value=None), \
             mock.patch.object(flood_app, "fetch_water_context_geoapify", return_value=None):
            self.assertEqual(flood_app.fetch_water_and_urban_context(LAT, LON), (None,) * 6)


if __name__ == "__main__":
    unittest.main()