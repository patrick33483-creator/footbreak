"""Regression coverage for the retired legacy portfolio surface."""
from __future__ import annotations

import unittest

from crown.handicap_world import PORTFOLIO, record_new_t5


class RetiredHandicapWorldTests(unittest.TestCase):
    def test_legacy_entrypoint_is_a_noop_and_does_not_create_a_namespace(self) -> None:
        ledger = {"bets": [], "watch": {}, "stats": {}}
        before = dict(ledger)
        self.assertEqual(record_new_t5(ledger, {"match_id": "fixture"}), [])
        self.assertEqual(ledger, before)
        self.assertNotIn(PORTFOLIO, ledger)


if __name__ == "__main__":
    unittest.main()
