import unittest
from unittest import mock
import app as A


class AgeText(unittest.TestCase):
    def test_no_raw_large_minute_counts(self):
        self.assertEqual(A._age_text(None), None)
        self.assertEqual(A._age_text(0.2), "just now")
        self.assertEqual(A._age_text(1), "1 minute ago")
        self.assertEqual(A._age_text(14), "14 minutes ago")
        self.assertEqual(A._age_text(1500), "about 25 hours ago")
        self.assertEqual(A._age_text(60 * 72), "about 3 days ago")


class RefreshEndpoint(unittest.TestCase):
    def setUp(self):
        self.c = A.app.test_client()

    def test_returns_immediately_and_starts_background_sweep(self):
        with mock.patch.object(A, "API_KEY", "x"), mock.patch.object(A, "CRON_SECRET", None), \
             mock.patch.object(A, "start_watchlist_sweep_background", return_value=True) as st, \
             mock.patch.object(A, "refresh_watchlist_cache") as sync:
            r = self.c.get("/api/refresh-watchlist")
        self.assertEqual(r.status_code, 202)
        self.assertTrue(r.get_json()["started"])
        st.assert_called_once()
        sync.assert_not_called()  # the sweep never runs inside the request

    def test_requires_token_when_secret_set(self):
        with mock.patch.object(A, "API_KEY", "x"), mock.patch.object(A, "CRON_SECRET", "s3"):
            self.assertEqual(self.c.get("/api/refresh-watchlist").status_code, 403)

    def test_already_running_reports_not_started(self):
        with mock.patch.object(A, "API_KEY", "x"), mock.patch.object(A, "CRON_SECRET", None), \
             mock.patch.object(A, "start_watchlist_sweep_background", return_value=False):
            r = self.c.get("/api/refresh-watchlist")
        self.assertFalse(r.get_json()["started"])


if __name__ == "__main__":
    unittest.main()


class AutoRefresh(unittest.TestCase):
    def test_not_started_under_tests(self):
        self.assertFalse(A.start_auto_refresh())


class WeatherScene(unittest.TestCase):
    def test_smoke_alone_is_low_visibility(self):
        s = A.weather_scene(711, "Smoke")
        self.assertEqual((s["code"], s["label"]), ("mist", "Low visibility"))

    def test_smoke_with_rain_reports_both(self):
        s = A.weather_scene(711, "Smoke", rainfall=0.4)
        self.assertEqual(s["code"], "rain")
        self.assertIn("rainfall", s["label"].lower())
        s2 = A.weather_scene(711, "Smoke", 0, [711, 300])
        self.assertEqual(s2["code"], "rain")

    def test_existing_scenes_unchanged(self):
        self.assertEqual(A.weather_scene(800, "clear sky")["code"], "clear")
        self.assertEqual(A.weather_scene(500, "light rain")["code"], "rain")
        self.assertEqual(A.weather_scene(211, "thunderstorm")["code"], "storm")
        self.assertEqual(A.weather_scene(803, "broken clouds")["code"], "clouds")
        self.assertEqual(A.weather_scene(803, "broken clouds", 1.2)["code"], "rain")