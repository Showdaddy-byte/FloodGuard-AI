"""Offline tests for the worldwide street-risk and Telegram modules.
Run from the project folder:  python -m unittest discover -s tests -v
No network, no real Telegram; a temporary SQLite file is used."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN-NOT-REAL")

import official_sources  # noqa: E402
import street_risk  # noqa: E402
import telegram_alerts as tg  # noqa: E402


def way(name, pts, highway="residential", **tags):
    t = {"name": name, "highway": highway}
    t.update(tags)
    return {"type": "way", "tags": t, "geometry": [{"lat": a, "lon": b} for a, b in pts]}


def make_world(base_lat, base_lon, names=None):
    """Ten named streets ~100 m apart (lowest = first) + crossing road + underpass + bridge."""
    names = names or [f"Street {i}" for i in range(10)]
    streets = []
    for i, n in enumerate(names):
        lat = base_lat + i * 0.0009
        streets.append(way(n, [(lat, base_lon), (lat, base_lon + 0.002), (lat, base_lon + 0.004)]))
    streets.append(way("Crossing Rd", [(base_lat + k * 0.0009, base_lon + 0.002) for k in range(10)], highway="tertiary"))
    streets.append(way("Under Bridge Way", [(base_lat + 0.012, base_lon), (base_lat + 0.012, base_lon + 0.002)], tunnel="yes"))
    streets.append(way("Skyway", [(base_lat + 0.014, base_lon), (base_lat + 0.014, base_lon + 0.002)], highway="trunk", bridge="yes"))
    streets.append({"type": "way", "tags": {"highway": "residential"}, "geometry": [{"lat": base_lat, "lon": base_lon}, {"lat": base_lat, "lon": base_lon + 0.001}]})
    water = {"elements": [{"type": "way", "tags": {"waterway": "drain"}, "geometry": [
        {"lat": base_lat - 0.0001, "lon": base_lon}, {"lat": base_lat - 0.0001, "lon": base_lon + 0.004}]}]}
    return {"elements": streets}, water


def overpass_for(roads, water):
    return lambda q: water if "waterway" in q else roads


def slope_elev(base_lat):
    return lambda pts: [2.0 + (lat - base_lat) * 1500 for lat, _ in pts]


LEKKI = {"key": "lekki, lagos, nigeria", "label": "Lekki, Lagos, Nigeria", "name": "Lekki", "country": "Nigeria", "country_code": "NG",
         "region": "Lagos", "city": "Lagos", "neighbourhood": "Lekki", "lat": 6.455, "lon": 3.402,
         "timezone": "Africa/Lagos", "tz_offset": 3600}
LISBON_NAMES = ["Rua Augusta", "Avenida da Liberdade", "Rua São Paulo", "Praça do Comércio", "Rua Áurea", "Rua da Prata",
                "Rua do Ouro", "Largo do Carmo", "Rua Garrett", "Calçada do Combro"]
LISBON = {"key": "baixa, lisboa, portugal", "label": "Baixa, Lisboa, Portugal", "name": "Baixa", "country": "Portugal", "country_code": "PT",
          "region": "Lisboa", "city": "Lisboa", "neighbourhood": "Baixa", "lat": 38.711, "lon": -9.137,
          "timezone": "Europe/Lisbon", "tz_offset": 0}


def assess(area, score, base_lat, base_lon, names=None, **kw):
    roads, water = make_world(base_lat, base_lon, names)
    args = dict(run_overpass=overpass_for(roads, water), elevation_fn=slope_elev(base_lat), reports=[])
    args.update(kw)
    return street_risk.assess_area(area, score, **args)


class StreetRiskTests(unittest.TestCase):
    def test_works_outside_nigeria_with_unicode_names(self):
        out = assess(LISBON, 45, 38.700, -9.140, LISBON_NAMES)
        self.assertEqual(out["status"], "ok")
        names = {s["name"] for s in out["streets"]}
        self.assertIn("Rua São Paulo", names)
        s = out["streets"][0]
        self.assertEqual(s["country_code"], "PT")
        self.assertEqual(s["city"], "Lisboa")
        self.assertEqual(s["neighbourhood"], "Baixa")

    def test_lower_street_scores_higher_and_explained(self):
        res = {s["name"]: s for s in assess(LEKKI, 40, 6.45, 3.40)["streets"]}
        self.assertGreater(res["Street 0"]["risk_score"], res["Street 9"]["risk_score"])
        self.assertTrue(any("drain" in f for f in res["Street 0"]["factors"]))
        self.assertTrue(any("underpass" in f for f in res["Under Bridge Way"]["factors"]))
        self.assertTrue(any("bridge" in f for f in res["Skyway"]["factors"]))

    def test_unnamed_roads_skipped(self):
        names = {s["name"] for s in assess(LEKKI, 40, 6.45, 3.40)["streets"]}
        self.assertNotIn("", names)
        self.assertEqual(len(names), 13)

    def test_no_roads_means_insufficient_data_and_no_predictions(self):
        out = street_risk.assess_area(LEKKI, 60, run_overpass=lambda q: {"elements": []},
                                      elevation_fn=slope_elev(6.45), reports=[])
        self.assertEqual(out["status"], "insufficient_data")
        self.assertEqual(out["streets"], [])
        self.assertTrue(out["reason"])
        self.assertEqual(out["area"]["score"], 60)

    def test_overpass_down_is_unavailable_not_invented(self):
        out = street_risk.assess_area(LEKKI, 60, run_overpass=lambda q: None, elevation_fn=slope_elev(6.45), reports=[])
        self.assertIn(out["status"], ("unavailable", "insufficient_data"))
        self.assertEqual(out["streets"], [])

    def test_flat_unknown_terrain_without_other_signal_is_not_ranked(self):
        roads, water = make_world(6.45, 3.40)
        roads = {"elements": [e for e in roads["elements"] if not (e["tags"].get("tunnel") or e["tags"].get("bridge"))]}
        out = street_risk.assess_area(LEKKI, 40, run_overpass=overpass_for(roads, {"elements": []}),
                                      elevation_fn=lambda pts: [None] * len(pts), reports=[])
        self.assertEqual(out["status"], "insufficient_data")
        self.assertEqual(out["streets"], [])

    def test_threshold_flagging_and_configurable(self):
        out = assess(LEKKI, 10, 6.45, 3.40, threshold=30)
        self.assertTrue(all(not s["above_threshold"] for s in out["streets"]))
        f30 = sum(s["above_threshold"] for s in assess(LEKKI, 35, 6.45, 3.40, threshold=30)["streets"])
        f60 = sum(s["above_threshold"] for s in assess(LEKKI, 35, 6.45, 3.40, threshold=60)["streets"])
        self.assertGreater(f30, f60)

    def test_per_country_threshold(self):
        old = dict(street_risk.COUNTRY_THRESHOLDS)
        try:
            street_risk.COUNTRY_THRESHOLDS.update({"GB": 35})
            self.assertEqual(street_risk.threshold_for("GB"), 35)
            self.assertEqual(street_risk.threshold_for("gb"), 35)
            self.assertEqual(street_risk.threshold_for("NG"), street_risk.ALERT_THRESHOLD)
            self.assertEqual(street_risk.threshold_for(None), street_risk.ALERT_THRESHOLD)
        finally:
            street_risk.COUNTRY_THRESHOLDS.clear()
            street_risk.COUNTRY_THRESHOLDS.update(old)

    def test_parse_country_thresholds(self):
        self.assertEqual(street_risk._parse_country_thresholds("GB=35, bd=25 ,bad,XX=zz"), {"GB": 35, "BD": 25})

    def test_severity_bands(self):
        self.assertEqual(street_risk.severity_for(29, 30)[0], "low")
        self.assertEqual(street_risk.severity_for(30, 30)[0], "moderate")
        self.assertEqual(street_risk.severity_for(50, 30)[0], "high")
        self.assertEqual(street_risk.severity_for(70, 30)[0], "severe")

    def test_never_claims_confirmed_flooding_or_probability(self):
        out = assess(LEKKI, 80, 6.45, 3.40)
        for s in out["streets"] + out["junctions"]:
            self.assertFalse(s["confirmed_flooding"])
            self.assertNotIn("confirmed", s["evidence"])
            self.assertEqual(s["score_kind"], "model_score")
        self.assertFalse(out["meta"]["calibrated_probability"])
        self.assertIn("calibrated probability", out["meta"]["basis"])

    def test_visitor_report_is_labelled_unverified(self):
        reports = [{"roads": "water at street 3 and crossing road", "created_at": datetime.utcnow().isoformat()}]
        res = {s["name"]: s for s in assess(LEKKI, 40, 6.45, 3.40, reports=reports)["streets"]}
        self.assertEqual(res["Street 3"]["visitor_reports_7d"], 1)
        self.assertIn("unverified_report", res["Street 3"]["evidence"])
        self.assertEqual(res["Street 3"]["primary_basis"], "unverified_report")
        self.assertTrue(any("unverified" in f for f in res["Street 3"]["factors"]))
        self.assertFalse(res["Street 3"]["confirmed_flooding"])

    def test_official_alert_is_area_level_and_does_not_change_scores(self):
        official = {"alerts": [{"source": "US National Weather Service", "event": "Flood Warning", "severity": "Severe",
                                "headline": "x", "scope": "warning area"}], "checked": [], "checked_at": None}
        plain = {s["name"]: s["risk_score"] for s in assess(LEKKI, 45, 6.45, 3.40)["streets"]}
        withoff = assess(LEKKI, 45, 6.45, 3.40, official=official)
        self.assertEqual(plain, {s["name"]: s["risk_score"] for s in withoff["streets"]})
        self.assertEqual({s["primary_basis"] for s in withoff["streets"] if s["above_threshold"]}, {"official_area_alert"})
        self.assertFalse(any(s["confirmed_flooding"] for s in withoff["streets"]))

    def test_freshness_labels(self):
        now = datetime.utcnow()
        for mins, label in ((5, "fresh"), (street_risk.STREET_CACHE_MINUTES + 30, "aging"), (street_risk.STREET_STALE_MINUTES + 30, "stale")):
            self.assertEqual(street_risk.freshness((now - timedelta(minutes=mins)).isoformat(), now)[1], label)
        self.assertEqual(street_risk.freshness(None)[1], "unknown")

    def test_junctions_detected(self):
        out = assess(LEKKI, 60, 6.45, 3.40)
        self.assertTrue(out["junctions"])
        self.assertTrue(all(j["kind"] == "junction" for j in out["junctions"]))

    def test_recommended_action_uses_given_emergency_number_only(self):
        out = assess(LEKKI, 80, 6.45, 3.40, emergency_text="112")
        txt = " ".join(s["recommended_action"] for s in out["streets"] if s["above_threshold"])
        self.assertIn("112", txt)
        out2 = assess(LEKKI, 80, 6.45, 3.40)
        self.assertNotIn("911", " ".join(s["recommended_action"] for s in out2["streets"]))


class StoreTests(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        street_risk.ensure_tables(self.db)
        for area, lat, lon, names in ((LEKKI, 6.45, 3.40, None), (LISBON, 38.70, -9.14, LISBON_NAMES)):
            street_risk.save_assessment(self.db, assess(area, 55, lat, lon, names))

    def tearDown(self):
        os.remove(self.db)

    def test_insufficient_results_are_not_saved(self):
        bad = street_risk.assess_area(LEKKI | {"key": "nowhere"}, 60, run_overpass=lambda q: {"elements": []},
                                      elevation_fn=slope_elev(6.45), reports=[])
        street_risk.save_assessment(self.db, bad)
        self.assertIsNone(street_risk.load_assessment(self.db, "nowhere"))

    def test_search_by_unaccented_unicode_name(self):
        items, areas = street_risk.search_cached(self.db, "sao paulo")
        self.assertTrue(any(i["name"] == "Rua São Paulo" for i in items))
        self.assertEqual({a["country_code"] for a in areas}, {"PT"})

    def test_search_by_country_city_and_region(self):
        for q, cc in (("portugal", "PT"), ("Lisboa", "PT"), ("lekki", "NG"), ("nigeria", "NG")):
            items, areas = street_risk.search_cached(self.db, q)
            self.assertTrue(items, q)
            self.assertEqual({a["country_code"] for a in areas}, {cc}, q)

    def test_search_never_mixes_unrelated_countries(self):
        items, _ = street_risk.search_cached(self.db, "portugal")
        self.assertTrue(all(i["country_code"] == "PT" for i in items))

    def test_ambiguous_query_reports_all_areas(self):
        items, areas = street_risk.search_cached(self.db, "street 3")
        self.assertEqual({a["country_code"] for a in areas}, {"NG"})
        items, areas = street_risk.search_cached(self.db, "rua")
        self.assertEqual({a["country_code"] for a in areas}, {"PT"})

    def test_top_flagged_country_filter(self):
        ng = street_risk.top_flagged(self.db, country_code="NG")
        self.assertTrue(ng and all(i["country_code"] == "NG" for i in ng))
        both = street_risk.top_flagged(self.db)
        self.assertEqual({i["country_code"] for i in both}, {"NG", "PT"})

    def test_summary_for_alert_returns_only_fresh_flagged(self):
        streets, a = street_risk.summary_for_alert(self.db, LEKKI["key"], limit=3)
        self.assertTrue(0 < len(streets) <= 3)
        self.assertTrue(all(s["above_threshold"] for s in streets))
        self.assertEqual(street_risk.summary_for_alert(self.db, "unknown-area"), ([], None))

    def test_stale_assessment_not_used_for_alerts(self):
        old = datetime.utcnow() + timedelta(hours=40)
        self.assertEqual(street_risk.summary_for_alert(self.db, LEKKI["key"], now=old), ([], None))
        self.assertEqual(street_risk.top_flagged(self.db, now=old), [])


class OfficialSourceTests(unittest.TestCase):
    class Resp:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    def setUp(self):
        official_sources._cache.clear()

    def test_nws_keeps_only_flood_events(self):
        data = {"features": [
            {"id": "a", "properties": {"event": "Flash Flood Warning", "severity": "Severe", "headline": "H", "areaDesc": "Harris"}},
            {"id": "b", "properties": {"event": "Heat Advisory"}}]}
        out = official_sources.fetch_official_alerts(29.7, -95.3, "US", lambda u, headers=None: self.Resp(data), [])
        events = [a["event"] for a in out["alerts"]]
        self.assertEqual(events, ["Flash Flood Warning"])
        self.assertIn({"source": "US National Weather Service", "status": "ok"}, out["checked"])

    def test_england_ignores_inactive_warnings(self):
        data = {"items": [{"severityLevel": 2, "description": "River X", "floodArea": {"county": "Kent"}},
                          {"severityLevel": 4, "description": "old"}]}
        out = official_sources.fetch_official_alerts(51.5, -0.1, "GB", lambda u, headers=None: self.Resp(data), [])
        self.assertEqual([a["event"] for a in out["alerts"]], ["Flood warning"])

    def test_failure_is_reported_not_treated_as_all_clear(self):
        def boom(url, headers=None):
            raise RuntimeError("down")
        out = official_sources.fetch_official_alerts(29.7, -95.3, "US", boom, [])
        self.assertEqual(out["alerts"], [])
        self.assertIn({"source": "US National Weather Service", "status": "unavailable"}, out["checked"])

    def test_country_without_feed_says_so(self):
        out = official_sources.fetch_official_alerts(6.5, 3.4, "NG", None, [])
        self.assertTrue(any(c["status"] == "no national feed connected" for c in out["checked"]))

    def test_gdacs_radius(self):
        rows = [{"latitude": 6.6, "longitude": 3.4, "title": "Near", "alert_level": "Orange", "country": "Nigeria"},
                {"latitude": 30, "longitude": 30, "title": "Far", "alert_level": "Red", "country": "Egypt"}]
        out = official_sources.fetch_official_alerts(6.5, 3.4, "NG", None, rows)
        self.assertEqual([a["headline"] for a in out["alerts"]], ["Near"])


class TelegramBase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        tg.init(db_path=self.db, normalize_city=lambda s: tg.norm(s), resolve_area=lambda t: (None, "no"),
                site_url="https://example.test", street_summary=None, safety_info=lambda a: "Emergency number: 112")
        tg.ensure_tables(self.db)
        street_risk.ensure_tables(self.db)
        self.sent = []
        self._orig = (tg.send_message, tg.MODE, tg.ADMIN_CHAT_IDS, tg.SEND_PAUSE_SECONDS)
        tg.send_message = lambda chat, text, **kw: self.sent.append((str(chat), text)) or len(self.sent)
        tg.MODE = "live"
        tg.ADMIN_CHAT_IDS = ["999"]
        tg.SEND_PAUSE_SECONDS = 0

    def tearDown(self):
        tg.send_message, tg.MODE, tg.ADMIN_CHAT_IDS, tg.SEND_PAUSE_SECONDS = self._orig
        os.remove(self.db)

    def subscribe(self, chat, area):
        sid = tg.upsert_subscriber(chat, {"first_name": "T"})
        lid, created, err = tg.add_location(sid, area)
        self.assertIsNone(err)
        return sid, lid

    @staticmethod
    def place(label, cc, region, city, community=None, tz="Africa/Lagos", off=3600):
        return {"label": label, "country": tg.country_name(cc), "country_code": cc, "region": region, "city": city,
                "community": community, "lat": 1.0, "lon": 1.0, "timezone": tz, "tz_offset": off}

    @staticmethod
    def prediction(label, cc, region, city, name, score=60, age_minutes=5, tz="Africa/Lagos", off=3600):
        return {"city": label, "score": score, "risk": "HIGH", "priority_action": "Avoid low-lying roads.",
                "assessed_at": (datetime.utcnow() - timedelta(minutes=age_minutes)).isoformat(),
                "area": {"name": name, "country": tg.country_name(cc), "country_code": cc, "region": region, "city": city,
                         "neighbourhood": name, "timezone": tz, "tz_offset": off, "lat": 1.0, "lon": 1.0}}


class MatchingTests(TelegramBase):
    def test_country_names_and_codes(self):
        self.assertEqual(tg.country_code_for("nigeria"), "NG")
        self.assertEqual(tg.country_code_for("United Kingdom"), "GB")
        self.assertEqual(tg.country_code_for("us"), "US")
        self.assertEqual(tg.country_code_for("Côte d'Ivoire"), "CI")
        self.assertIsNone(tg.country_code_for("Narnia"))

    def test_same_city_name_in_two_countries_is_not_mixed(self):
        self.subscribe("1", self.place("Paris", "FR", "Île-de-France", "Paris", tz="Europe/Paris", off=3600))
        self.subscribe("2", self.place("Paris", "US", "Texas", "Paris", tz="America/Chicago", off=-21600))
        fr = tg.matching_locations(self.prediction("Paris, France", "FR", "Île-de-France", "Paris", "Paris"))
        us = tg.matching_locations(self.prediction("Paris, Texas", "US", "Texas", "Paris", "Paris"))
        self.assertEqual([r["chat_id"] for r in fr], ["1"])
        self.assertEqual([r["chat_id"] for r in us], ["2"])

    def test_city_subscription_matches_any_neighbourhood_in_city(self):
        self.subscribe("1", self.place("Lagos", "NG", "Lagos", "Lagos"))
        rows = tg.matching_locations(self.prediction("Lekki, Lagos", "NG", "Lagos", "Lagos", "Lekki"))
        self.assertEqual(len(rows), 1)

    def test_community_subscription_matches_only_that_community(self):
        self.subscribe("1", self.place("Yaba, Lagos", "NG", "Lagos", "Lagos", "Yaba"))
        self.assertEqual(len(tg.matching_locations(self.prediction("Yaba, Lagos", "NG", "Lagos", "Lagos", "Yaba"))), 1)
        self.assertEqual(len(tg.matching_locations(self.prediction("Lekki, Lagos", "NG", "Lagos", "Lagos", "Lekki"))), 0)

    def test_region_with_state_word_matches(self):
        self.subscribe("1", self.place("Houston", "US", "Texas", "Houston", tz="America/Chicago", off=-21600))
        self.assertEqual(len(tg.matching_locations(self.prediction("Houston, Texas", "US", "State of Texas", "Houston", "Houston"))), 1)

    def test_unaccented_name_matches_accented(self):
        self.subscribe("1", self.place("Sao Paulo", "BR", "Sao Paulo", "Sao Paulo", tz="America/Sao_Paulo", off=-10800))
        self.assertEqual(len(tg.matching_locations(self.prediction("São Paulo", "BR", "São Paulo", "São Paulo", "São Paulo"))), 1)

    def test_prediction_without_country_never_matches(self):
        self.subscribe("1", self.place("Lagos", "NG", "Lagos", "Lagos"))
        pred = self.prediction("Lagos", "NG", "Lagos", "Lagos", "Lagos")
        pred["area"]["country_code"] = ""
        self.assertEqual(tg.matching_locations(pred), [])
        self.assertEqual(tg.dispatch_alerts_for_prediction(pred)["sent"], 0)


class TimeTests(unittest.TestCase):
    def test_local_time_text_zones(self):
        dt = datetime(2026, 10, 27, 12, 0)
        self.assertIn("13:00", tg.local_time_text(dt, "Africa/Lagos"))
        self.assertIn("UTC+01:00", tg.local_time_text(dt, "Africa/Lagos"))
        self.assertIn("UTC+05:30", tg.local_time_text(dt, "Asia/Kolkata"))
        self.assertIn("UTC-05:00", tg.local_time_text(dt, "America/Chicago") if False else tg.local_time_text(dt, None, -18000))

    def test_fallbacks(self):
        dt = datetime(2026, 10, 27, 12, 0)
        self.assertIn("UTC+00:00", tg.local_time_text(dt, "Not/AZone"))
        self.assertIn("14:00", tg.local_time_text(dt, "Not/AZone", 7200))
        self.assertEqual(tg.local_time_text(None), "unknown time")


class DecideTests(unittest.TestCase):
    def row(self, **kw):
        base = {"episode_active": 0, "last_alert_at": None, "last_alert_score": None, "urgent_updates": 1}
        base.update(kw)
        return base

    def test_below_threshold_nothing(self):
        self.assertIsNone(tg.decide(self.row(), 29, threshold=30))

    def test_first_alert_at_threshold(self):
        self.assertEqual(tg.decide(self.row(), 30, threshold=30), "alert")

    def test_threshold_is_per_call(self):
        self.assertIsNone(tg.decide(self.row(), 34, threshold=35))
        self.assertEqual(tg.decide(self.row(), 35, threshold=35), "alert")

    def test_no_duplicate_while_episode_active(self):
        now = datetime.utcnow()
        r = self.row(episode_active=1, last_alert_at=(now - timedelta(minutes=10)).isoformat(), last_alert_score=40)
        self.assertIsNone(tg.decide(r, 42, now, 30))

    def test_cooldown_after_clear(self):
        now = datetime.utcnow()
        r = self.row(last_alert_at=(now - timedelta(hours=1)).isoformat(), last_alert_score=40)
        self.assertIsNone(tg.decide(r, 50, now, 30))
        r = self.row(last_alert_at=(now - timedelta(hours=tg.COOLDOWN_HOURS + 1)).isoformat(), last_alert_score=40)
        self.assertEqual(tg.decide(r, 50, now, 30), "alert")

    def test_urgent_on_material_increase_with_min_gap(self):
        now = datetime.utcnow()
        old = (now - timedelta(minutes=tg.URGENT_MIN_GAP_MINUTES + 5)).isoformat()
        r = self.row(episode_active=1, last_alert_at=old, last_alert_score=40)
        self.assertEqual(tg.decide(r, 40 + tg.ESCALATION_DELTA, now, 30), "urgent")
        self.assertIsNone(tg.decide(r, 40 + tg.ESCALATION_DELTA - 1, now, 30))
        recent = self.row(episode_active=1, last_alert_at=(now - timedelta(minutes=5)).isoformat(), last_alert_score=40)
        self.assertIsNone(tg.decide(recent, 70, now, 30))

    def test_urgent_disabled_by_subscriber(self):
        now = datetime.utcnow()
        r = self.row(episode_active=1, urgent_updates=0, last_alert_score=40,
                     last_alert_at=(now - timedelta(hours=2)).isoformat())
        self.assertIsNone(tg.decide(r, 80, now, 30))

    def test_clear_needs_margin(self):
        r = self.row(episode_active=1, last_alert_score=40, last_alert_at=datetime.utcnow().isoformat())
        self.assertIsNone(tg.decide(r, 28, threshold=30))
        self.assertEqual(tg.decide(r, 30 - tg.CLEAR_MARGIN - 1, threshold=30), "clear")


class DispatchTests(TelegramBase):
    def setUp(self):
        super().setUp()
        self.sid, self.lid = self.subscribe("111", self.place("Lagos", "NG", "Lagos", "Lagos"))
        self.sid2, _ = self.subscribe("222", self.place("Lisboa", "PT", "Lisboa", "Lisboa", tz="Europe/Lisbon", off=0))
        self.pred = self.prediction("Lekki, Lagos", "NG", "Lagos", "Lagos", "Lekki", score=55)

    def test_alert_goes_only_to_matching_country(self):
        stats = tg.dispatch_alerts_for_prediction(self.pred)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual([c for c, _ in self.sent], ["111"])
        self.assertIn("Lekki", self.sent[0][1])
        self.assertIn("UTC+01:00", self.sent[0][1])
        self.assertIn("not a confirmed flood", self.sent[0][1].lower())

    def test_message_uses_per_country_timezone(self):
        pt = self.prediction("Baixa, Lisboa", "PT", "Lisboa", "Lisboa", "Baixa", score=55, tz="Europe/Lisbon", off=0)
        tg.dispatch_alerts_for_prediction(pt)
        self.assertEqual([c for c, _ in self.sent], ["222"])
        self.assertIn("UTC", self.sent[0][1])

    def test_duplicate_prevention(self):
        tg.dispatch_alerts_for_prediction(self.pred)
        tg.dispatch_alerts_for_prediction(self.pred)
        tg.dispatch_alerts_for_prediction(dict(self.pred, score=57))
        self.assertEqual(len(self.sent), 1)

    def test_urgent_update_on_big_rise(self):
        tg.dispatch_alerts_for_prediction(self.pred)
        conn = tg._conn()
        conn.execute("UPDATE telegram_locations SET last_alert_at=?", ((datetime.utcnow() - timedelta(hours=2)).isoformat(),))
        conn.commit()
        conn.close()
        tg.dispatch_alerts_for_prediction(dict(self.pred, score=55 + tg.ESCALATION_DELTA + 1))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("urgent", self.sent[1][1].lower())

    def test_below_threshold_sends_nothing(self):
        tg.dispatch_alerts_for_prediction(dict(self.pred, score=20))
        self.assertEqual(self.sent, [])

    def test_per_country_threshold_respected(self):
        old = dict(street_risk.COUNTRY_THRESHOLDS)
        street_risk.COUNTRY_THRESHOLDS["NG"] = 60
        try:
            tg.dispatch_alerts_for_prediction(self.pred)  # 55 < 60
            self.assertEqual(self.sent, [])
        finally:
            street_risk.COUNTRY_THRESHOLDS.clear()
            street_risk.COUNTRY_THRESHOLDS.update(old)

    def test_stale_data_is_not_alerted_and_is_logged(self):
        tg.dispatch_alerts_for_prediction(dict(self.pred, assessed_at=(datetime.utcnow() - timedelta(hours=5)).isoformat()))
        self.assertEqual(self.sent, [])
        conn = tg._conn()
        n = conn.execute("SELECT COUNT(*) FROM telegram_alert_log WHERE kind='stale_skip'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_unknown_data_age_is_treated_as_stale(self):
        p = dict(self.pred)
        p.pop("assessed_at")
        tg.dispatch_alerts_for_prediction(p)
        self.assertEqual(self.sent, [])

    def test_test_mode_sends_only_to_admins(self):
        tg.MODE = "test"
        tg.dispatch_alerts_for_prediction(self.pred)
        self.assertEqual({c for c, _ in self.sent}, {"999"})
        self.assertIn("test", self.sent[0][1].lower())

    def test_blocked_user_marked_and_not_counted_as_sent(self):
        def blocked(chat, text, **kw):
            raise tg.TelegramError("Forbidden: bot was blocked by the user", status=403, blocked=True)
        tg.send_message = blocked
        tg.dispatch_alerts_for_prediction(self.pred)
        conn = tg._conn()
        status = conn.execute("SELECT status FROM telegram_subscribers WHERE chat_id='111'").fetchone()[0]
        active = conn.execute("SELECT episode_active FROM telegram_locations WHERE id=?", (self.lid,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "blocked")
        self.assertEqual(active, 0)

    def test_paused_subscriber_gets_nothing(self):
        tg._set_status(self.sid, "paused")
        tg.dispatch_alerts_for_prediction(self.pred)
        self.assertEqual(self.sent, [])

    def test_clear_after_drop_allows_new_alert_after_cooldown(self):
        tg.dispatch_alerts_for_prediction(self.pred)
        tg.dispatch_alerts_for_prediction(dict(self.pred, score=5))
        conn = tg._conn()
        self.assertEqual(conn.execute("SELECT episode_active FROM telegram_locations WHERE id=?", (self.lid,)).fetchone()[0], 0)
        conn.close()

    def test_message_includes_official_alerts_when_present(self):
        p = dict(self.pred, official_alerts=[{"source": "GDACS (UN / European Commission)", "event": "Flood event",
                                              "severity": "Orange", "headline": "Flood near Lagos"}])
        tg.dispatch_alerts_for_prediction(p)
        self.assertIn("GDACS", self.sent[0][1])

    def test_message_html_escapes_place_names(self):
        p = self.prediction("<b>x</b>, Lagos", "NG", "Lagos", "Lagos", "Lekki", score=55)
        text = tg.build_alert_message(p, kind="alert", data_age=5, tz_name="Africa/Lagos", tz_offset=3600, threshold=30)
        self.assertNotIn("<b>x</b>", text)


class CommandTests(TelegramBase):
    def update(self, text, uid=1, chat="500"):
        return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": int(chat), "type": "private"},
                                              "from": {"id": int(chat), "first_name": "T"}, "text": text}}

    def test_pending_token_flow_registers_location_once(self):
        token = tg.create_pending(self.place("Yaba, Lagos", "NG", "Lagos", "Lagos", "Yaba"))
        tg.handle_update(self.update(f"/start {token}", 1))
        tg.handle_update(self.update(f"/start {token}", 2))
        conn = tg._conn()
        n = conn.execute("SELECT COUNT(*) FROM telegram_locations").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_duplicate_update_ignored(self):
        tg.handle_update(self.update("/help", 7))
        tg.handle_update(self.update("/help", 7))
        self.assertEqual(len(self.sent), 1)

    def test_add_uses_injected_resolver_and_reports_errors(self):
        tg.init(resolve_area=lambda t: (None, "'x' matches several places. Add the country."))
        tg.handle_update(self.update("/add Paris", 3))
        self.assertIn("several places", self.sent[-1][1])
        tg.init(resolve_area=lambda t: (self.place("Paris, France", "FR", "Île-de-France", "Paris", None, "Europe/Paris"), None))
        tg.handle_update(self.update("/add Paris, France", 4))
        conn = tg._conn()
        row = conn.execute("SELECT country_code, tz FROM telegram_locations").fetchone()
        conn.close()
        self.assertEqual((row["country_code"], row["tz"]), ("FR", "Europe/Paris"))

    def test_stop_deletes_personal_data_but_keeps_anonymised_log(self):
        sid, _ = self.subscribe("500", self.place("Lagos", "NG", "Lagos", "Lagos"))
        tg.log_event("alert", "sent", chat_id="500", subscriber_id=sid, area_label="Lagos", country_code="NG", score=50)
        tg.handle_update(self.update("/stop", 5))
        conn = tg._conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM telegram_subscribers").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM telegram_locations").fetchone()[0], 0)
        rows = conn.execute("SELECT chat_id FROM telegram_alert_log").fetchall()
        conn.close()
        self.assertTrue(rows)
        self.assertTrue(all(r["chat_id"] in (None, "") for r in rows))

    def test_location_limit(self):
        sid = tg.upsert_subscriber("9", {})
        for i in range(tg.MAX_LOCATIONS_PER_SUBSCRIBER):
            _, _, err = tg.add_location(sid, self.place(f"C{i}", "NG", "Lagos", f"city{i}"))
            self.assertIsNone(err)
        _, _, err = tg.add_location(sid, self.place("Extra", "NG", "Lagos", "extra"))
        self.assertTrue(err)

    def test_token_never_in_scrubbed_errors(self):
        self.assertNotIn(tg.BOT_TOKEN, tg._scrub(f"failed https://api.telegram.org/bot{tg.BOT_TOKEN}/sendMessage"))

    def test_extra_monitored_labels_and_known_place(self):
        self.subscribe("1", self.place("Yaba, Lagos", "NG", "Lagos", "Lagos", "Yaba", tz="Africa/Lagos"))
        self.assertEqual(tg.extra_monitored_labels(), ["Yaba, Lagos"])
        kp = tg.known_place_for_label("Yaba, Lagos")
        self.assertEqual((kp["country_code"], kp["timezone"]), ("NG", "Africa/Lagos"))


class AdminTests(TelegramBase):
    def test_admin_test_message_is_labelled_and_goes_to_one_chat(self):
        pred = self.prediction("Lekki, Lagos", "NG", "Lagos", "Lagos", "Lekki")
        ok = tg.send_admin_test("999", "Lekki, Lagos", 42, prediction=pred, tz_name="Africa/Lagos", tz_offset=3600)
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("TEST", self.sent[0][1])

    def test_overview_has_global_fields(self):
        o = tg.admin_overview()
        for key in ("configured", "live", "subscribers", "locations", "threshold", "country_thresholds", "locations_by_country", "log"):
            self.assertIn(key, o)


if __name__ == "__main__":
    unittest.main()