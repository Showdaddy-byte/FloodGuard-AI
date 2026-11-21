import unittest
import app as flood_app


class SearchEngineRules(unittest.TestCase):
    def setUp(self):
        self.c = flood_app.app.test_client()

    def test_robots_blocks_api_admin_unsubscribe_and_lists_sitemap(self):
        r = self.c.get("/robots.txt")
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        for line in ("Disallow: /api/", "Disallow: /admin/", "Disallow: /unsubscribe/",
                     "Sitemap: https://myfloodguard.online/sitemap.xml"):
            self.assertIn(line, text)
        self.assertNotIn("Disallow: /\n", text)

    def test_api_responses_carry_noindex_without_changing_status(self):
        r = self.c.get("/api/map-geocode?q=")
        self.assertEqual(r.status_code, 400)
        self.assertIn("noindex", r.headers.get("X-Robots-Tag", ""))

    def test_normal_pages_are_not_marked_noindex(self):
        self.assertNotIn("X-Robots-Tag", self.c.get("/").headers)
        self.assertNotIn("X-Robots-Tag", self.c.get("/terms").headers)

    def test_sitemap_lists_real_pages_only(self):
        text = self.c.get("/sitemap.xml").get_data(as_text=True)
        self.assertIn("https://myfloodguard.online/terms", text)
        self.assertNotIn("/api/", text)


if __name__ == "__main__":
    unittest.main()