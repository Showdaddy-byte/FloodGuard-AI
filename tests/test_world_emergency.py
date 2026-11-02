import unittest
import world_emergency as we
import app as flood_app


class WorldEmergency(unittest.TestCase):
    def test_clean_number(self):
        self.assertEqual(we.clean_number("10 111"), "10111")
        self.assertIsNone(we.clean_number("depends on town"))
        self.assertIsNone(we.clean_number(""))

    def test_dataset_loaded_and_plain(self):
        self.assertGreater(we.coverage()["countries"], 200)
        for cc in ("FR", "JP", "BR", "ZA"):
            e = we.reference_numbers(cc)
            for f in ("general", "police", "ambulance", "fire"):
                if e.get(f):
                    self.assertRegex(e[f], r"^\+?\d+$")

    def test_ambiguous_cells_do_not_invent_roles(self):
        tr = we.reference_numbers("TR")
        self.assertEqual(tr["general"], "112")
        self.assertNotIn("police", tr)
        self.assertNotEqual(tr.get("ambulance"), "911")
        self.assertEqual(we.reference_numbers("FR")["ambulance"], "15")

    def test_unknown_country_returns_none(self):
        self.assertIsNone(we.reference_numbers("ZZ"))
        self.assertIsNone(we.reference_numbers(""))

    def test_curated_has_priority_and_reference_labelled(self):
        ng = flood_app.get_official_emergency_numbers("NG")
        self.assertEqual(ng["national"], flood_app.NATIONAL_EMERGENCY_NUMBERS["NG"])
        jp = flood_app.get_official_emergency_numbers("JP")
        if "JP" not in flood_app.NATIONAL_EMERGENCY_NUMBERS:
            self.assertIn("confirm locally", jp["national"]["notes"])

    def test_reference_note_added_once(self):
        e = we.with_reference_note(we.reference_numbers("BR"))
        self.assertEqual(e["notes"].count("Please confirm locally"), 1)


if __name__ == "__main__":
    unittest.main()