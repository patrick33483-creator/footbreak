import unittest
from repair_result_3092501 import validate, SID, SOURCE_KO, RECORDED_KO


class ScoreExceptionTests(unittest.TestCase):
    def setUp(self):
        self.match = {"sid": SID, "kickoff_utc": RECORDED_KO,
                      "home": "蒙特塞拉特", "away": "特克斯和凯科斯群岛"}
        self.cells = [""]*80
        for i, v in {0: self.match["home"], 1: self.match["away"], 4: "-1",
                     5: "20261007040000", 10: "4", 11: "1", 15: "中北美国联", 76: SID}.items():
            self.cells[i] = v
        self.now = SOURCE_KO+5*3600000

    def test_valid_only_explicit_exception(self):
        proof = validate(self.match, "^".join(self.cells), self.now)
        self.assertEqual(proof["expected_kickoff"], RECORDED_KO)
        self.assertEqual(proof["source_kickoff"], SOURCE_KO)
        self.assertTrue(proof["notification_timing_not_reclassified"])

    def test_reject_wrong_fixture(self):
        for key, value in (("sid", "3092502"), ("kickoff_utc", SOURCE_KO), ("home", "other")):
            with self.assertRaises(ValueError):
                validate({**self.match, key: value}, "^".join(self.cells), self.now)

    def test_reject_source_changes(self):
        for key, value in ((4, "1"), (5, "20261007030000"), (5, "20261007040100"),
                           (10, "3"), (11, "2"), (0, "other"), (15, "other"), (76, "3092502")):
            cells = list(self.cells)
            cells[key] = value
            with self.assertRaises(ValueError):
                validate(self.match, "^".join(cells), self.now)

    def test_not_too_early(self):
        with self.assertRaises(ValueError):
            validate(self.match, "^".join(self.cells), SOURCE_KO+30*60000)
