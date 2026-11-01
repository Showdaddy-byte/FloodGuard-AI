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