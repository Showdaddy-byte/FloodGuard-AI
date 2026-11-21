import os, sys, tempfile, unittest
from datetime import datetime
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as appmod


class DigestScheduling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._old = appmod.DB_PATH
        appmod.DB_PATH = self.tmp.name
        appmod.init_db()

    def tearDown(self):
        appmod.DB_PATH = self._old
        os.unlink(self.tmp.name)

    def test_due_window_catches_up_after_the_exact_hour(self):
        h = appmod.DIGEST_MORNING_UTC_HOUR
        self.assertEqual(appmod.due_digest_type(datetime(2026, 11, 21, h + 2)), "morning")
        self.assertIsNone(appmod.due_digest_type(datetime(2026, 11, 21, h - 1)))
        self.assertEqual(appmod.due_digest_type(datetime(2026, 11, 21, appmod.DIGEST_EVENING_UTC_HOUR)), "evening")

    def test_empty_database_does_not_use_up_the_day(self):
        self.assertEqual(appmod.run_digest_once("morning"), "no_subscribers")
        self.assertEqual(appmod.run_digest_once("morning"), "no_subscribers")

    def test_sent_once_per_day(self):
        with mock.patch.object(appmod, "send_daily_digests", return_value=3) as m:
            self.assertEqual(appmod.run_digest_once("evening"), "sent:3")
            self.assertEqual(appmod.run_digest_once("evening"), "already_sent")
            self.assertEqual(m.call_count, 1)

    def test_marker_is_saved_externally(self):
        import persistence
        self.assertIn("digest_sends", persistence.TABLES)


class ReclaimedLand(unittest.TestCase):
    def test_wording(self):
        ee = {"available": True, "jrc_water_occurrence_pct": 78, "dynamic_world_label": "built area",
              "dynamic_world_water_prob": 0.02, "dynamic_world_built_prob": 0.9}
        fn = appmod.classify_terrain_behaviour
        kwargs = dict(elevation=3, slope_percent=0.5, nearest_water_m=800, coastal=True,
                      building_count=300, earth_engine=ee)
        r = fn(**kwargs)
        self.assertEqual(r["label"], "Reclaimed / sand-filled land")
        self.assertIn("78%", r["detail"])
        self.assertIn("torrential rainfall", r["detail"])


class Privacy(unittest.TestCase):
    def test_factors_hidden_from_visitors(self):
        c = appmod.app.test_client()
        with mock.patch.object(appmod, "API_KEY", "x"):
            pass
        with appmod.app.test_request_context("/api/property-check"):
            pass
        resp = appmod.app.response_class('{"ok": true, "factors": ["a"], "score": 5}', mimetype="application/json")
        with appmod.app.test_request_context("/api/property-check", method="POST"):
            out = appmod._hide_reasoning_from_visitors(resp)
            self.assertNotIn("factors", out.get_json())


if __name__ == "__main__":
    unittest.main()