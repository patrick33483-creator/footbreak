from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crown import notify
from crown.common import now_hkt, parse_time


SIGNATURE = "condition-two-signature"


def _stage(name: str, kickoff, *, odds: float, line: float = 2.75):
    observed = now_hkt() - timedelta(minutes=2)
    return {
        "stage": name,
        "match_id": "fixture-2",
        "kickoff_hkt": kickoff.isoformat(),
        "ts": (now_hkt() - timedelta(minutes=1)).isoformat(),
        "market_predictions": [{
            "code": "HIL",
            "side": "H",
            "line": line,
            "odds": odds,
            "observed_at": observed.isoformat(),
            "quote_source": "crown_public_board",
        }],
    }


def _ledger(stages):
    kickoff = now_hkt() + timedelta(minutes=10)
    return {
        "bets": [],
        "watch": {
            "fixture-2": {
                "match_id": "fixture-2",
                "kickoff": kickoff.isoformat(),
                "kickoff_hkt": kickoff.isoformat(),
                "league": "USA - Major League Soccer",
                "home": "主隊",
                "away": "客隊",
                "stages": [
                    _stage(name, kickoff, odds=odds, line=line)
                    for name, odds, line in stages
                ],
            },
        },
        "wilson_validation": {
            "conditions": {
                SIGNATURE: {
                    "condition_number": 2,
                    "rollover_status": "active",
                    "active_evidence": {
                        "cumulative_hits": 273,
                        "cumulative_decided": 459,
                        "minimum_acceptable_odds_raw": 1.90,
                    },
                },
            },
            "observations": [{
                "observation_id": "fixture-2|HIL|首預|condition-two-signature|formal-observation",
                "portfolio": "crown_wilson_observations",
                "formal_bet": False,
                "bet_status": "FORMAL_OBSERVATION",
                "status": "PENDING",
                "match_id": "fixture-2",
                "code": "HIL",
                "market": "HIL",
                "stage": "首預",
                "selected_side": "H",
                "selected_role": "大",
                "selected_line": 2.75,
                "line": 2.75,
                "odds": 1.75,
                "frozen_condition_signature": SIGNATURE,
                "condition_number": 2,
            }],
        },
    }


class Condition2NotificationTests(unittest.TestCase):
    def _run(self, ledger, state, sender):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_dir=Path(directory))
            with patch.object(
                notify, "notification_lock", return_value=nullcontext(True)
            ), patch.object(
                notify, "_load", return_value=state
            ), patch.object(
                notify, "_send", sender
            ), patch.object(
                notify, "write_json_atomic"
            ):
                return notify.notify_condition2_pending(ledger, config)

    def test_t30_first_qualifying_quote_alerts_once_and_t5_is_silent(self):
        ledger = _ledger([
            ("首預", 1.75, 2.75),
            ("T-30", 1.90, 3.0),
        ])
        state = {"wilson_condition2_bet_alerts": []}
        sender = unittest.mock.Mock(return_value=True)

        self.assertEqual(self._run(ledger, state, sender), 1)
        message = sender.call_args.args[1]
        self.assertIn("達標時點：T-30", message)
        self.assertIn("大 3 @1.90", message)

        kickoff = parse_time(
            ledger["watch"]["fixture-2"]["kickoff_hkt"]
        )
        self.assertIsNotNone(kickoff)
        ledger["watch"]["fixture-2"]["stages"].append(
            _stage("T-5", kickoff, odds=1.95, line=2.75)
        )
        self.assertEqual(self._run(ledger, state, sender), 0)
        self.assertEqual(sender.call_count, 1)

    def test_t5_alerts_when_first_look_and_t30_are_below_minimum(self):
        ledger = _ledger([
            ("首預", 1.75, 2.75),
            ("T-30", 1.85, 3.0),
            ("T-5", 1.92, 2.75),
        ])
        state = {"wilson_condition2_bet_alerts": []}
        sender = unittest.mock.Mock(return_value=True)

        self.assertEqual(self._run(ledger, state, sender), 1)
        self.assertIn("達標時點：T-5", sender.call_args.args[1])

    def test_t5_never_falls_back_to_an_older_qualifying_t30_quote(self):
        ledger = _ledger([
            ("首預", 1.75, 2.75),
            ("T-30", 1.95, 3.0),
            ("T-5", 1.85, 2.75),
        ])
        state = {"wilson_condition2_bet_alerts": []}
        sender = unittest.mock.Mock(return_value=True)

        self.assertEqual(self._run(ledger, state, sender), 0)
        sender.assert_not_called()

    def test_first_look_can_alert_but_low_odds_still_enrols_silently(self):
        state = {"wilson_condition2_bet_alerts": []}
        sender = unittest.mock.Mock(return_value=True)
        qualifying = _ledger([("首預", 1.90, 2.75)])
        self.assertEqual(self._run(qualifying, state, sender), 1)
        self.assertIn("達標時點：首預", sender.call_args.args[1])

        low = _ledger([("首預", 1.75, 2.75)])
        low_state = {"wilson_condition2_bet_alerts": []}
        low_sender = unittest.mock.Mock(return_value=True)
        self.assertEqual(self._run(low, low_state, low_sender), 0)
        self.assertEqual(
            len(low["wilson_validation"]["observations"]), 1,
        )
        low_sender.assert_not_called()

    def test_later_line_or_direction_must_still_match(self):
        for line, side in ((3.25, "H"), (2.75, "L")):
            ledger = _ledger([
                ("首預", 1.75, 2.75),
                ("T-30", 1.95, line),
            ])
            ledger["watch"]["fixture-2"]["stages"][-1][
                "market_predictions"
            ][0]["side"] = side
            state = {"wilson_condition2_bet_alerts": []}
            sender = unittest.mock.Mock(return_value=True)
            self.assertEqual(self._run(ledger, state, sender), 0)
            sender.assert_not_called()

    def test_generic_wilson_outbox_never_replays_condition_two(self):
        row = _ledger([("首預", 1.75, 2.75)])[
            "wilson_validation"
        ]["observations"][0]
        row.update({
            "stage": "T-5",
            "bet_status": "NO_BET_LOW_ODDS",
            "wilson_admission": {"minimum_acceptable_odds_raw": 1.90},
            "kickoff": (now_hkt() + timedelta(minutes=10)).isoformat(),
            "league": "USA - Major League Soccer",
            "home": "主隊",
            "away": "客隊",
            "market_label": "入球大細",
        })
        state = {"wilson_bets": [], "wilson_match_alerts": []}
        sender = unittest.mock.Mock(return_value=True)
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_dir=Path(directory))
            with patch.object(
                notify, "notification_lock", return_value=nullcontext(True)
            ), patch.object(
                notify, "_load", return_value=state
            ), patch.object(
                notify, "_send", sender
            ), patch.object(
                notify, "write_json_atomic"
            ):
                self.assertEqual(
                    notify.notify_wilson_pending(
                        {"bets": [], "wilson_validation": {
                            "observations": [row],
                        }},
                        config,
                    ),
                    0,
                )
        sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
