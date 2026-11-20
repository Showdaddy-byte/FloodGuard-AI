import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import persistence as p

ORIG_CFG = p._cfg

SCHEMA = """
CREATE TABLE alert_subscribers (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, phone TEXT,
    whatsapp TEXT, unsubscribe_token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE telegram_subscribers (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL UNIQUE,
    username TEXT, status TEXT NOT NULL DEFAULT 'active', urgent_updates INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE telegram_locations (id INTEGER PRIMARY KEY AUTOINCREMENT, subscriber_id INTEGER NOT NULL, label TEXT NOT NULL,
    lat REAL, lon REAL, episode_active INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE contributions (id INTEGER PRIMARY KEY AUTOINCREMENT, city_key TEXT NOT NULL, city_label TEXT NOT NULL,
    category TEXT NOT NULL, rating INTEGER NOT NULL, comment TEXT, created_at TEXT NOT NULL, water_depth_cm INTEGER, roads_affected TEXT);
CREATE TABLE dam_status (dam_key TEXT PRIMARY KEY, status TEXT NOT NULL, notes TEXT, updated_at TEXT NOT NULL);
"""


class FakeTurso:
    """Speaks just enough of Turso's /v2/pipeline to test against a real SQLite."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.calls = 0
        self.fail = False

    @staticmethod
    def _arg(a):
        t = a["type"]
        return None if t == "null" else int(a["value"]) if t == "integer" else float(a["value"]) if t == "float" else a["value"]

    @staticmethod
    def _cell(v):
        if v is None: return {"type": "null"}
        if isinstance(v, int): return {"type": "integer", "value": str(v)}
        if isinstance(v, float): return {"type": "float", "value": v}
        return {"type": "text", "value": v}

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls += 1
        assert url.endswith("/v2/pipeline") and headers["Authorization"].startswith("Bearer ")
        class R:
            def __init__(s, body, status=200): s._b, s.status_code = body, status
            def raise_for_status(s):
                if s.status_code >= 400: raise p.requests.HTTPError(str(s.status_code))
            def json(s): return s._b
        if self.fail:
            return R({}, 500)
        results = []
        for req in json["requests"]:
            if req["type"] == "close":
                results.append({"type": "ok", "response": {"type": "close"}}); continue
            stmt = req["stmt"]
            try:
                cur = self.db.execute(stmt["sql"], [self._arg(a) for a in stmt.get("args", [])])
                self.db.commit()
                cols = [{"name": d[0]} for d in (cur.description or [])]
                rows = [[self._cell(v) for v in row] for row in cur.fetchall()] if cur.description else []
                results.append({"type": "ok", "response": {"type": "execute", "result": {"cols": cols, "rows": rows}}})
            except sqlite3.Error as e:
                results.append({"type": "error", "error": {"message": str(e)}})
        return R({"results": results})


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        c = sqlite3.connect(self.path); c.executescript(SCHEMA); c.commit(); c.close()
        self.turso = FakeTurso()
        self.patches = [
            mock.patch.object(p, "_cfg", return_value=("https://x.turso.io", "secret-token")),
            mock.patch.object(p.requests, "post", side_effect=self.turso.post),
        ]
        for m in self.patches: m.start()
        p._state.update(restored=False, started=False, last_sync_at=None, last_error=None, restored_rows={}, remote_only={}, pushed={})
        p._db_path = self.path

    def tearDown(self):
        for m in self.patches: m.stop()
        self.turso.db.close()
        os.remove(self.path)

    def local(self, sql, args=()):
        c = sqlite3.connect(self.path); rows = c.execute(sql, args).fetchall(); c.commit(); c.close(); return rows

    def remote(self, sql):
        return self.turso.db.execute(sql).fetchall()

    def boot(self):
        p.ensure_remote(self.path)
        p._state["restored_rows"] = p.restore(self.path)
        p._state["restored"] = True

    def test_new_subscription_reaches_turso_and_survives_wipe(self):
        self.boot()
        self.local("INSERT INTO telegram_subscribers (chat_id, username, created_at) VALUES ('123','salt','2026-11-20')")
        self.local("INSERT INTO telegram_locations (subscriber_id,label,lat,lon,created_at) VALUES (1,'Lekki, Lagos',6.45,3.47,'2026-11-20')")
        p.sync(self.path)
        self.assertEqual(self.remote("SELECT chat_id FROM telegram_subscribers"), [("123",)])
        # Render wipes the disk: a brand-new empty database, same Turso.
        os.remove(self.path)
        c = sqlite3.connect(self.path); c.executescript(SCHEMA); c.commit(); c.close()
        p._state.update(restored=False, pushed={})
        self.boot()
        self.assertEqual(self.local("SELECT id, chat_id, username FROM telegram_subscribers"), [(1, "123", "salt")])
        self.assertEqual(self.local("SELECT subscriber_id, label, lat FROM telegram_locations"), [(1, "Lekki, Lagos", 6.45)])

    def test_ids_are_kept_so_locations_still_match_subscribers(self):
        self.boot()
        for n in range(1, 4):
            self.local("INSERT INTO telegram_subscribers (chat_id, created_at) VALUES (?, 'd')", (str(n),))
        self.local("DELETE FROM telegram_subscribers WHERE id=1")
        self.local("INSERT INTO telegram_locations (subscriber_id,label,created_at) VALUES (3,'X','d')")
        p.sync(self.path)
        os.remove(self.path)
        c = sqlite3.connect(self.path); c.executescript(SCHEMA); c.commit(); c.close()
        p._state.update(restored=False, pushed={})
        self.boot()
        self.assertEqual(self.local("SELECT id FROM telegram_subscribers ORDER BY id"), [(2,), (3,)])
        self.assertEqual(self.local("SELECT subscriber_id FROM telegram_locations"), [(3,)])
        self.local("INSERT INTO telegram_subscribers (chat_id, created_at) VALUES ('new','d')")
        self.assertEqual(self.local("SELECT MAX(id) FROM telegram_subscribers"), [(4,)])

    def test_nothing_pushed_before_restore(self):
        self.local("INSERT INTO alert_subscribers (email, unsubscribe_token, created_at) VALUES ('a@b.c','t','d')")
        self.assertEqual(p.sync(self.path), 0)
        self.assertEqual(self.turso.calls, 0)

    def test_only_changed_rows_are_sent(self):
        self.boot()
        self.local("INSERT INTO alert_subscribers (email, unsubscribe_token, created_at) VALUES ('a@b.c','t','d')")
        self.assertEqual(p.sync(self.path), 1)
        self.assertEqual(p.sync(self.path), 0)
        self.local("UPDATE alert_subscribers SET phone='080' WHERE id=1")
        self.assertEqual(p.sync(self.path), 1)
        self.assertEqual(self.remote("SELECT phone FROM alert_subscribers"), [("080",)])

    def test_unsubscribe_deletes_remote_row(self):
        self.boot()
        self.local("INSERT INTO alert_subscribers (email, unsubscribe_token, created_at) VALUES ('a@b.c','t','d')")
        self.local("INSERT INTO alert_subscribers (email, unsubscribe_token, created_at) VALUES ('x@y.z','u','d')")
        p.sync(self.path)
        self.local("DELETE FROM alert_subscribers WHERE id=1")
        p.sync(self.path)
        self.assertEqual(self.remote("SELECT email FROM alert_subscribers"), [("x@y.z",)])

    def test_mass_delete_is_refused(self):
        self.boot()
        for n in range(8):
            self.local("INSERT INTO telegram_subscribers (chat_id, created_at) VALUES (?, 'd')", (str(n),))
        p.sync(self.path)
        self.local("DELETE FROM telegram_subscribers")
        p.sync(self.path)
        self.assertEqual(len(self.remote("SELECT id FROM telegram_subscribers")), 8)
        self.assertIn("refused", p.status()["last_error"])

    def test_community_reports_survive_a_wipe(self):
        self.boot()
        self.local("INSERT INTO contributions (city_key,city_label,category,rating,comment,created_at,water_depth_cm,roads_affected) "
                   "VALUES ('lekki, lagos','Lekki, Lagos','flooding',4,'Road is flooded',?, 30, 'Admiralty Way')", ("2026-11-20T08:00:00",))
        p.sync(self.path)
        os.remove(self.path)
        c = sqlite3.connect(self.path); c.executescript(SCHEMA); c.commit(); c.close()
        p._state.update(restored=False, pushed={})
        self.boot()
        self.assertEqual(self.local("SELECT city_label, rating, comment, water_depth_cm, roads_affected FROM contributions"),
                         [("Lekki, Lagos", 4, "Road is flooded", 30, "Admiralty Way")])

    def test_unsubscribe_and_stop_reach_turso_before_returning(self):
        self.boot()
        p._state["started"] = True
        self.local("INSERT INTO alert_subscribers (email, unsubscribe_token, created_at) VALUES ('a@b.c','t','d')")
        self.local("INSERT INTO telegram_subscribers (chat_id, created_at) VALUES ('9','d')")
        self.local("INSERT INTO telegram_locations (subscriber_id,label,created_at) VALUES (1,'Lekki','d')")
        self.local("INSERT INTO telegram_locations (subscriber_id,label,created_at) VALUES (1,'Ikeja','d')")
        self.assertTrue(p.sync_now())
        # email unsubscribe, one Telegram place removed, then /stop (subscriber + remaining place)
        self.local("DELETE FROM alert_subscribers WHERE id=1")
        self.local("DELETE FROM telegram_locations WHERE label='Ikeja'")
        self.assertTrue(p.sync_now())
        self.assertEqual(self.remote("SELECT COUNT(*) FROM alert_subscribers"), [(0,)])
        self.assertEqual(self.remote("SELECT label FROM telegram_locations"), [("Lekki",)])
        self.local("DELETE FROM telegram_locations")
        self.local("DELETE FROM telegram_subscribers")
        self.assertTrue(p.sync_now())
        self.assertEqual(self.remote("SELECT COUNT(*) FROM telegram_subscribers"), [(0,)])
        self.assertEqual(self.remote("SELECT COUNT(*) FROM telegram_locations"), [(0,)])

    def test_removed_subscribers_do_not_come_back_after_a_wipe(self):
        self.boot()
        p._state["started"] = True
        for n in ("1", "2"):
            self.local("INSERT INTO telegram_subscribers (chat_id, created_at) VALUES (?, 'd')", (n,))
        p.sync_now()
        self.local("DELETE FROM telegram_subscribers WHERE chat_id='1'")
        p.sync_now()
        os.remove(self.path)
        c = sqlite3.connect(self.path); c.executescript(SCHEMA); c.commit(); c.close()
        p._state.update(restored=False, pushed={})
        self.boot()
        self.assertEqual(self.local("SELECT chat_id FROM telegram_subscribers"), [("2",)])

    def test_interrupted_restore_changes_nothing(self):
        # Turso holds a row the local table refuses; nothing at all may be half-restored.
        self.turso.db.executescript("""
            CREATE TABLE alert_subscribers (id INTEGER PRIMARY KEY, email TEXT, phone TEXT, whatsapp TEXT, unsubscribe_token TEXT, created_at TEXT);
            INSERT INTO alert_subscribers VALUES (1,'ok@x.y',NULL,NULL,'tok','d');
            CREATE TABLE telegram_subscribers (id INTEGER PRIMARY KEY, chat_id TEXT, username TEXT, status TEXT, urgent_updates INTEGER, created_at TEXT);
            INSERT INTO telegram_subscribers VALUES (1,NULL,NULL,'active',1,'d');
        """)
        p.ensure_remote(self.path)
        with self.assertRaises(sqlite3.Error):
            p.restore(self.path)
        self.assertEqual(self.local("SELECT COUNT(*) FROM alert_subscribers"), [(0,)])
        self.assertEqual(self.local("SELECT COUNT(*) FROM telegram_subscribers"), [(0,)])

    def test_partly_filled_local_table_never_destroys_saved_rows(self):
        self.turso.db.executescript(SCHEMA)
        for n in ("1", "2", "3"):
            self.turso.db.execute("INSERT INTO telegram_subscribers (chat_id, created_at) VALUES (?, 'd')", (n,))
        self.turso.db.commit()
        self.local("INSERT INTO telegram_subscribers (id, chat_id, created_at) VALUES (1,'1','d')")
        self.boot()
        self.assertEqual(self.local("SELECT COUNT(*) FROM telegram_subscribers"), [(1,)])  # local data is not overwritten
        p.sync(self.path)
        self.assertEqual(self.remote("SELECT COUNT(*) FROM telegram_subscribers"), [(3,)])  # saved rows untouched
        self.assertEqual(p.status()["saved_rows_not_on_this_server"], {"telegram_subscribers": 2})

    def test_text_primary_key_table(self):
        self.boot()
        self.local("INSERT INTO dam_status (dam_key,status,notes,updated_at) VALUES ('kainji','WATCH','n','d')")
        p.sync(self.path)
        self.local("UPDATE dam_status SET status='NORMAL' WHERE dam_key='kainji'")
        p.sync(self.path)
        self.assertEqual(self.remote("SELECT dam_key,status FROM dam_status"), [("kainji", "NORMAL")])

    def test_remote_gets_columns_added_later(self):
        self.turso.db.execute("CREATE TABLE alert_subscribers (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, unsubscribe_token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)")
        self.boot()
        cols = {r[1] for r in self.remote("PRAGMA table_info(alert_subscribers)")}
        self.assertTrue({"phone", "whatsapp"} <= cols)

    def test_outage_is_reported_not_raised_and_retried(self):
        self.boot()
        self.local("INSERT INTO alert_subscribers (email, unsubscribe_token, created_at) VALUES ('a@b.c','t','d')")
        self.turso.fail = True
        p._run_sync_once()
        self.assertIsNotNone(p.status()["last_error"])
        self.turso.fail = False
        p._run_sync_once()
        self.assertEqual(self.remote("SELECT email FROM alert_subscribers"), [("a@b.c",)])
        self.assertIsNone(p.status()["last_error"])

    def test_disabled_without_settings(self):
        with mock.patch.object(p, "_cfg", return_value=None):
            self.assertFalse(p.enabled())
            self.assertFalse(p.start(self.path))

    def test_status_never_contains_token_or_rows(self):
        self.boot()
        self.assertNotIn("secret-token", repr(p.status()))

    def test_url_forms(self):
        for given in ("libsql://db-org.turso.io", "turso://db-org.turso.io", "https://db-org.turso.io/", "db-org.turso.io"):
            with mock.patch.dict(os.environ, {"TURSO_DATABASE_URL": given, "TURSO_AUTH_TOKEN": "t"}):
                self.assertEqual(ORIG_CFG(), ("https://db-org.turso.io", "t"))
        with mock.patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": "t"}):
            self.assertIsNone(ORIG_CFG())


if __name__ == "__main__":
    unittest.main()


class Wiring(unittest.TestCase):
    def test_admin_status_needs_login_and_hides_secrets(self):
        import app as flood_app
        client = flood_app.app.test_client()
        self.assertEqual(client.get("/api/admin/persistence").status_code, 401)
        with client.session_transaction() as sess:
            sess["is_admin"] = True
        body = client.get("/api/admin/persistence").get_json()
        self.assertTrue(body["ok"])
        self.assertNotIn("token", repr(body).lower())


class Gate(unittest.TestCase):
    def setUp(self):
        import app as flood_app
        self.client = flood_app.app.test_client()
        p._state.update(started=True, restored=False)

    def tearDown(self):
        p._state.update(started=False, restored=False)

    def test_subscription_and_unsubscribe_wait_until_restore_finished(self):
        self.assertEqual(self.client.post("/api/alert-subscribe", json={}).status_code, 503)
        self.assertEqual(self.client.post("/api/telegram/webhook", json={}).status_code, 503)
        self.assertEqual(self.client.get("/unsubscribe/abc").status_code, 503)
        self.assertEqual(self.client.post("/api/contribute", json={}).status_code, 503)

    def test_searches_and_pages_are_never_blocked(self):
        self.assertNotEqual(self.client.get("/").status_code, 503)
        self.assertNotEqual(self.client.post("/", data={"city": "Lagos"}).status_code, 503)
        self.assertNotEqual(self.client.get("/terms").status_code, 503)

    def test_open_again_once_restored(self):
        p._state["restored"] = True
        with mock.patch.object(p, "sync_now") as m:
            self.assertNotEqual(self.client.post("/api/alert-subscribe", json={}).status_code, 503)
        self.assertEqual(m.call_count, 1)

    def test_no_gate_when_turso_not_used(self):
        p._state.update(started=False, restored=False)
        self.assertNotEqual(self.client.post("/api/alert-subscribe", json={}).status_code, 503)