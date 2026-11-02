import unittest
from unittest import mock
import app as flood_app


class Resp:
    def __init__(self, code, body=None):
        self.status_code, self._b = code, body or {}
    def raise_for_status(self):
        if self.status_code >= 400:
            raise flood_app.requests.HTTPError(str(self.status_code))
    def json(self):
        return self._b


class SoilMoisture(unittest.TestCase):
    def setUp(self):
        flood_app._mark_service_up("open-meteo-soil")
    tearDown = setUp

    def test_429_pauses_further_calls(self):
        with mock.patch.object(flood_app.requests, "get", return_value=Resp(429)) as g:
            self.assertIsNone(flood_app.fetch_soil_moisture(1, 1))
            self.assertIsNone(flood_app.fetch_soil_moisture(2, 2))
            self.assertIsNone(flood_app.fetch_soil_moisture(3, 3))
        self.assertEqual(g.call_count, 1)

    def test_normal_value_still_read(self):
        ok = Resp(200, {"hourly": {"soil_moisture_0_to_1cm": [0.31]}})
        with mock.patch.object(flood_app.requests, "get", return_value=ok):
            self.assertEqual(flood_app.fetch_soil_moisture(1, 1), 0.31)

    def test_cache_window_is_hours(self):
        self.assertGreaterEqual(flood_app.SOIL_MOISTURE_TTL_MINUTES, 240)
        self.assertGreaterEqual(flood_app.SOIL_MOISTURE_RATE_LIMIT_PAUSE_SECONDS, 180)


if __name__ == "__main__":
    unittest.main()