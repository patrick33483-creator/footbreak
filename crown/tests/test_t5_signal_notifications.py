from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from crown.config import settings
from crown.notify import notify_new
from crown.state import load_predictions, save_predictions


KICKOFF = "2099-08-12T20:00:00+08:00"


def _market(code: str, side: str, line: float, odds: object) -> dict:
    return {
        "code": code, "market": code, "side": side, "line": line,
        "condition": f"{line:g}", "odds": odds,
    }


def _stage(name: str, markets: list[dict]) -> dict:
    return {
        "stage": name, "match_id": "safe-fixture-1", "kickoff_hkt": KICKOFF,
        "league": "測試聯賽", "home": "主隊", "away": "客隊",
        "market_predictions": markets,
    }


def _ledger(
    hdc_stages: tuple[dict, dict, dict] | None = None,
    t5_markets: list[dict] | None = None,
) -> dict:
    if hdc_stages is None:
        hdc_stages = (
            _market("HDC", "H", -0.25, 1.91),
            _market("HDC", "H", -0.25, 1.92),
            _market("HDC", "H", -0.25, 1.93),
        )
    if t5_markets is None:
        t5_markets = [_market("CHL", "L", 9.5, 1.88)]
    stages = [
        _stage("首預", [hdc_stages[0]]),
        _stage("T-30", [hdc_stages[1]]),
        _stage("T-5", [hdc_stages[2], *t5_markets]),
    ]
    return {
        "bets": [],
        "watch": {
            "safe-fixture-1": {
                "match_id": "safe-fixture-1", "kickoff_hkt": KICKOFF,
                "kickoff": KICKOFF, "league": "測試聯賽",
                "home": "主隊", "away": "客隊", "stages": stages,
            }
        },
    }


class CrownT5SignalNotificationTests(unittest.TestCase):
    def _config(self, directory: str):
        return replace(settings(), state_dir=Path(directory), telegram_enabled=False)

    def test_only_qualifying_hdc_three_stage_signal_is_sent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("crown.notify._send") as sender:
                self.assertEqual(
                    notify_new(_ledger(), self._config(directory), ["safe-fixture-1"]), 1
                )
            messages = [call.args[1] for call in sender.call_args_list]
            self.assertTrue(any("市場：皇冠讓球" in text for text in messages))
            self.assertTrue(any("選擇：主隊 -0/0.5" in text for text in messages))
            self.assertTrue(any("選項實際賠率：1.930" in text for text in messages))
            self.assertTrue(any("條件：主讓≥1.70 累積中（0/0）" in text for text in messages))
            self.assertFalse(any("角球" in text for text in messages))
            self.assertTrue(all("只作通知，絕不實際投注。" in text for text in messages))

    def test_hdc_signal_uses_settled_priced_history_for_category_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            rows = []
            for fixture, hit in (
                ("hist-1", True),
                ("hist-2", True),
                ("hist-3", True),
                ("hist-4", False),
            ):
                for stage in ("首預", "T-30", "T-5"):
                    rows.append({
                        "match_id": fixture,
                        "stage": stage,
                        "market_grades": [{
                            "code": "HDC",
                            "side": "H",
                            "line": -0.25,
                            "grade_status": "GRADED",
                            "hit": hit,
                            "odds": 1.80,
                        }],
                    })
            (config.state_dir / "prediction_history.json").write_text(
                json.dumps({"rows": rows}), encoding="utf-8"
            )
            with patch("crown.notify._send") as sender:
                self.assertEqual(
                    notify_new(_ledger(), config, ["safe-fixture-1"]), 1
                )
            self.assertIn(
                "條件：主讓≥1.70 75.0%（3/4）",
                sender.call_args.args[1],
            )

    def test_hdc_rejects_changed_direction_line_and_incomplete_stages(self) -> None:
        cases = [
            (
                (
                    _market("HDC", "H", -0.25, 1.91),
                    _market("HDC", "A", -0.25, 1.92),
                    _market("HDC", "H", -0.25, 1.93),
                ),
                False,
            ),
            (
                (
                    _market("HDC", "H", -0.25, 1.91),
                    _market("HDC", "H", -0.5, 1.92),
                    _market("HDC", "H", -0.25, 1.93),
                ),
                False,
            ),
        ]
        for hdc_stages, _ in cases:
            with self.subTest(hdc_stages=hdc_stages), tempfile.TemporaryDirectory() as directory:
                with patch("crown.notify._send") as sender:
                    self.assertEqual(
                        notify_new(
                            _ledger(hdc_stages, t5_markets=[]),
                            self._config(directory), ["safe-fixture-1"],
                        ),
                        0,
                    )
                sender.assert_not_called()

        incomplete = _ledger(t5_markets=[])
        incomplete["watch"]["safe-fixture-1"]["stages"].pop(1)
        with tempfile.TemporaryDirectory() as directory, patch("crown.notify._send") as sender:
            self.assertEqual(
                notify_new(incomplete, self._config(directory), ["safe-fixture-1"]), 0
            )
        sender.assert_not_called()

        identity_mismatch = _ledger(t5_markets=[])
        identity_mismatch["watch"]["safe-fixture-1"]["stages"][1]["away"] = "另一隊"
        with tempfile.TemporaryDirectory() as directory, patch("crown.notify._send") as sender:
            self.assertEqual(
                notify_new(
                    identity_mismatch, self._config(directory), ["safe-fixture-1"]
                ),
                0,
            )
        sender.assert_not_called()

    def test_rejects_missing_selected_odds_and_never_uses_opposite_side(self) -> None:
        hdc = (
            _market("HDC", "H", -0.25, 1.91),
            _market("HDC", "H", -0.25, 1.92),
            _market("HDC", "H", -0.25, None),
        )
        ledger = _ledger(
            hdc,
            t5_markets=[
                _market("CHL", "L", 9.5, None),
                # This must not become a fallback selected quote.
                _market("CHL", "H", 9.5, 1.77),
            ],
        )
        with tempfile.TemporaryDirectory() as directory, patch("crown.notify._send") as sender:
            self.assertEqual(
                notify_new(ledger, self._config(directory), ["safe-fixture-1"]), 0
            )
        sender.assert_not_called()

    def test_rejects_odds_below_1_70_but_accepts_exact_boundary(self) -> None:
        low_hdc = (
            _market("HDC", "H", -0.25, 1.91),
            _market("HDC", "H", -0.25, 1.92),
            _market("HDC", "H", -0.25, 1.699),
        )
        with tempfile.TemporaryDirectory() as directory, patch("crown.notify._send") as sender:
            self.assertEqual(
                notify_new(
                    _ledger(
                        low_hdc,
                        t5_markets=[_market("CHL", "L", 9.5, 1.699)],
                    ),
                    self._config(directory),
                    ["safe-fixture-1"],
                ),
                0,
            )
        sender.assert_not_called()

        boundary_hdc = (
            _market("HDC", "H", -0.25, 1.91),
            _market("HDC", "H", -0.25, 1.92),
            _market("HDC", "H", -0.25, 1.70),
        )
        with tempfile.TemporaryDirectory() as directory, patch("crown.notify._send") as sender:
            self.assertEqual(
                notify_new(
                    _ledger(
                        boundary_hdc,
                        t5_markets=[_market("CHL", "L", 9.5, 1.70)],
                    ),
                    self._config(directory),
                    ["safe-fixture-1"],
                ),
                1,
            )
        self.assertEqual(sender.call_count, 1)

    def test_upcoming_unacknowledged_t5_is_recovered_without_fresh_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("crown.notify._send") as sender:
            self.assertEqual(notify_new(_ledger(), self._config(directory), []), 1)
        self.assertEqual(sender.call_count, 1)

    def test_t30_or_first_stage_never_trigger(self) -> None:
        non_t5 = _ledger()
        non_t5["watch"]["safe-fixture-1"]["stages"] = non_t5["watch"]["safe-fixture-1"]["stages"][:2]
        with tempfile.TemporaryDirectory() as directory, patch("crown.notify._send") as sender:
            self.assertEqual(
                notify_new(non_t5, self._config(directory), ["safe-fixture-1"]), 0
            )
        sender.assert_not_called()

    def test_disabled_transport_does_not_acknowledge_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            self.assertEqual(notify_new(_ledger(), config, ["safe-fixture-1"]), 0)
            state = json.loads((config.state_dir / "notify_state.json").read_text())
            self.assertEqual(state["signals"], [])

    def test_signal_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            with patch("crown.notify._send") as sender:
                self.assertEqual(notify_new(_ledger(), config, ["safe-fixture-1"]), 1)
                self.assertEqual(notify_new(_ledger(), config, ["safe-fixture-1"]), 0)
            self.assertEqual(sender.call_count, 1)

    def test_transport_failure_never_corrupts_live_prediction_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            live_predictions = [{"match_id": "safe-fixture-1", "stage": "T-5"}]
            save_predictions(config, live_predictions)
            ledger = _ledger()
            before = copy.deepcopy(ledger)
            with patch("crown.notify._send", side_effect=RuntimeError("transport down")):
                with self.assertRaisesRegex(RuntimeError, "transport down"):
                    notify_new(ledger, config, ["safe-fixture-1"])
            self.assertEqual(ledger, before)
            self.assertEqual(load_predictions(config), live_predictions)
            notify_path = config.state_dir / "notify_state.json"
            self.assertFalse(notify_path.exists())
            with patch("crown.notify._send") as sender:
                self.assertEqual(notify_new(ledger, config, []), 1)
            self.assertEqual(sender.call_count, 1)
