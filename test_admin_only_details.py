import unittest
import app as flood_app

BODY = {"items": [{"name": "A Street", "sources": ["x"], "coverage": [{"source": "y"}], "data_gaps": ["z"], "risk_score": 40}],
        "sources": ["x"], "coverage": [1], "limitations": ["l"], "status": "ok"}


class AdminOnlyDetails(unittest.TestCase):
    def test_visitor_sees_no_providers_or_coverage(self):
        with flood_app.app.test_request_context("/"):
            out = flood_app._for_viewer(dict(BODY))
        for k in ("sources", "coverage", "limitations"):
            self.assertNotIn(k, out)
        self.assertEqual(out["items"][0], {"name": "A Street", "risk_score": 40})
        self.assertEqual(out["status"], "ok")

    def test_admin_keeps_everything(self):
        with flood_app.app.test_request_context("/"):
            flood_app.session["is_admin"] = True
            out = flood_app._for_viewer(dict(BODY))
        self.assertIn("sources", out)
        self.assertIn("coverage", out["items"][0])

    def test_homepage_hides_sources_panel_for_visitors(self):
        client = flood_app.app.test_client()
        html = client.get("/").get_data(as_text=True)
        self.assertNotIn("Data sources and limits", html)
        self.assertIn("var IS_ADMIN = false", html)

    def test_homepage_shows_sources_panel_for_admin(self):
        client = flood_app.app.test_client()
        with client.session_transaction() as sess:
            sess["is_admin"] = True
        html = client.get("/").get_data(as_text=True)
        self.assertIn("Data sources and limits (admin only)", html)
        self.assertIn("var IS_ADMIN = true", html)


if __name__ == "__main__":
    unittest.main()