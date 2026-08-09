import unittest

from crown.pinnapi import parse_corner_lines


class PinnapiCornerParserTests(unittest.TestCase):
    def test_parses_unique_full_match_corner_event(self):
        payload = {
            "events": [
                {
                    "event_id": "corner-1",
                    "league_name": "Japan - J League Corners",
                    "home": "V-Varen Nagasaki (Corners)",
                    "away": "Kyoto Sanga (Corners)",
                    "periods": {
                        "num_0": {
                            "spreads": {
                                "0": {"hdp": 0, "home": 2.04, "away": 1.7246, "max": 375},
                                "0.5": {"hdp": 0.5, "home": 1.8696, "away": 1.9009, "max": 375},
                            },
                            "totals": {
                                "9": {"points": 9, "over": 1.6135, "under": 2.26, "max": 750},
                                "9.5": {"points": 9.5, "over": 1.8547, "under": 1.9434, "max": 750},
                            },
                        },
                        "num_1": {
                            "totals": {
                                "4.5": {"points": 4.5, "over": 1.9259, "under": 1.8547}
                            }
                        },
                    },
                }
            ]
        }

        result = parse_corner_lines(payload, "parent-1", observed_at=1234)

        self.assertEqual(result["corner_event_id"], "corner-1")
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(len(result["prices"]), 8)
        self.assertFalse(any(row["line"] == 4.5 for row in result["prices"]))
        main_spread = [
            row for row in result["prices"]
            if row["market"] == "CHDC" and row["main"]
        ]
        main_total = [
            row for row in result["prices"]
            if row["market"] == "CHL" and row["main"]
        ]
        self.assertEqual({row["line"] for row in main_spread}, {0.5})
        self.assertEqual({row["line"] for row in main_total}, {9.5})
        self.assertTrue(all(row["source_at"] == 1234 for row in result["prices"]))

    def test_fails_closed_without_corner_event(self):
        result = parse_corner_lines(
            {"events": [{"event_id": "normal", "league_name": "J League"}]},
            "parent-1",
            observed_at=1234,
        )
        self.assertEqual(result["prices"], [])
        self.assertEqual(result["market_status"], "unavailable")
        self.assertEqual(result["candidate_count"], 0)

    def test_fails_closed_when_corner_children_are_ambiguous(self):
        event = {
            "league_name": "League Corners",
            "home": "Home (Corners)",
            "away": "Away (Corners)",
            "periods": {
                "num_0": {
                    "totals": {
                        "9.5": {"points": 9.5, "over": 1.9, "under": 1.9}
                    }
                }
            },
        }
        result = parse_corner_lines(
            {"events": [
                dict(event, event_id="corner-1"),
                dict(event, event_id="corner-2"),
            ]},
            "parent-1",
            observed_at=1234,
        )
        self.assertEqual(result["prices"], [])
        self.assertEqual(result["market_status"], "ambiguous")
        self.assertEqual(result["candidate_count"], 2)

    def test_rejects_one_sided_or_invalid_prices(self):
        payload = {
            "events": [{
                "event_id": "corner-1",
                "league_name": "League Corners",
                "home": "Home (Corners)",
                "away": "Away (Corners)",
                "periods": {
                    "num_0": {
                        "totals": {
                            "9.5": {"points": 9.5, "over": 1.9},
                            "10": {"points": 10, "over": 1.0, "under": 2.0},
                        }
                    }
                },
            }]
        }
        result = parse_corner_lines(payload, "parent-1", observed_at=1234)
        self.assertEqual(result["prices"], [])
        self.assertEqual(result["market_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
