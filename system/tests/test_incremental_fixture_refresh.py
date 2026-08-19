"""Offline regression coverage for the 15-minute Footbreak board refresh."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SYSTEM = ROOT / "system"
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import gen_app_data
import record_picks
import run_predict


def match(match_id: str) -> dict:
    return {
        "id": match_id,
        "status": "PREEVENT",
        "homeTeam": {"name_ch": f"主隊{match_id}", "name_en": f"Home {match_id}"},
        "awayTeam": {"name_ch": f"客隊{match_id}", "name_en": f"Away {match_id}"},
        "tournament": {"nameCH": "測試聯賽"},
        "foPools": [],
    }


def prediction(row: dict, stage: str, kickoff: dt.datetime) -> dict:
    return {
        "match_id": row["id"],
        "stage": stage,
        "home": row["homeTeam"]["name_ch"],
        "away": row["awayTeam"]["name_ch"],
        "home_en": row["homeTeam"]["name_en"],
        "away_en": row["awayTeam"]["name_en"],
        "league": "測試聯賽",
        "kickoff_hkt": kickoff.astimezone(run_predict.HKT).strftime("%Y-%m-%d %H:%M"),
        "fixture_id": None,
        "league_id": None,
        "venue": None,
        "venue_city": None,
        "mins_to_ko": 120.0,
        "conviction": 50.0,
        "candidates": [],
        "weather": {},
        "final": None,
        "open": None,
        "now": {},
        "movement": {},
        "adjustments": [],
        "mults": {},
        "outcome": None,
        "hk_fingerprint": {},
    }


class IncrementalRefreshTests(unittest.TestCase):
    def test_late_board_fixture_gets_one_native_first_look_and_refresh_is_idempotent(self):
        kickoff = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)
        existing, late = match("known"), match("late")
        immutable_first_look = {
            "stage": "首預",
            "ts": "2026-08-01T12:00:00+08:00",
            "conviction": 41.0,
            "marker": "must-not-change",
        }
        ledger = {
            "bankroll": 50000,
            "bets": [],
            "log": [],
            "stats": {},
            "watch": {
                "known": {
                    "match_id": "known",
                    "league": "舊聯賽",
                    "home": "舊主隊",
                    "away": "舊客隊",
                    "kickoff": "2099-08-01 20:00",
                    "stages": [copy.deepcopy(immutable_first_look)],
                }
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "sim_ledger.json"
            ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
            analyse = lambda row, *_args, stage_override=None, **_kwargs: prediction(
                row, stage_override, kickoff
            )
            with patch.object(run_predict, "HERE", directory), \
                 patch.object(run_predict, "HK_SNAP", str(root / "hk_snapshots.json")), \
                 patch.object(run_predict.H, "fetch_matches", return_value=[existing, late]), \
                 patch.object(run_predict.H, "parse_kickoff", return_value=kickoff), \
                 patch.object(run_predict.S, "list_fixtures", return_value=[]), \
                 patch.object(run_predict.S, "match_fixture", return_value=(None, 0.0)), \
                 patch.object(run_predict, "analyse_match", side_effect=analyse) as analyse_match, \
                 patch.object(run_predict, "pick_one", return_value=(None, "測試觀望")), \
                 patch.object(record_picks, "HERE", directory), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)):
                first = run_predict.main(mode="sweep", horizon_min=2160, out="refresh.json")
                self.assertEqual([row["match_id"] for row in first], ["late"])
                analyse_match.assert_called_once()

                record_picks.sync("refresh.json", send_notifications=False)
                saved = json.loads(ledger_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    saved["watch"]["known"]["stages"][0], immutable_first_look
                )
                self.assertEqual(
                    [stage["stage"] for stage in saved["watch"]["late"]["stages"]],
                    ["首預"],
                )

                analyse_match.reset_mock()
                second = run_predict.main(mode="sweep", horizon_min=2160, out="refresh.json")
                self.assertEqual(second, [])
                analyse_match.assert_not_called()

    def test_existing_first_look_t30_and_t5_rows_are_append_only(self):
        kickoff = (dt.datetime.now(record_picks.HKT) + dt.timedelta(hours=2)).strftime(
            "%Y-%m-%d %H:%M"
        )
        original_stages = [
            {"stage": "首預", "ts": "2026-08-01T10:00:00+08:00", "marker": "first"},
            {"stage": "T-30", "ts": "2026-08-01T11:00:00+08:00", "marker": "t30"},
            {"stage": "T-5", "ts": "2026-08-01T11:25:00+08:00", "marker": "t5"},
        ]
        results = [
            {
                "match_id": "existing",
                "stage": stage["stage"],
                "kickoff_hkt": kickoff,
                "league": "新聯賽名稱不應重寫階段",
                "home": "主隊",
                "away": "客隊",
                "can_bet": stage["stage"] == "T-5",
                "candidates": [],
            }
            for stage in original_stages
        ]
        ledger = {
            "bankroll": 50000,
            "bets": [],
            "log": [],
            "stats": {},
            "watch": {
                "existing": {
                    "match_id": "existing",
                    "league": "原始聯賽",
                    "home": "主隊",
                    "away": "客隊",
                    "kickoff": kickoff,
                    "stages": copy.deepcopy(original_stages),
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predictions = root / "predictions.json"
            ledger_path = root / "sim_ledger.json"
            predictions.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
            ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
            with patch.object(record_picks, "HERE", directory), \
                 patch.object(record_picks, "LEDGER", str(ledger_path)), \
                 patch.object(record_picks, "evaluate_new_t5") as evaluate:
                _, notes, saved = record_picks.sync(send_notifications=False)
            self.assertEqual(saved["watch"]["existing"]["stages"], original_stages)
            self.assertEqual(len(saved["bets"]), 0)
            evaluate.assert_not_called()
            self.assertEqual(sum("已保存；保留原始記錄" in note for note in notes), 3)

    def test_refresh_runner_skips_settlement_accuracy_and_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "calls.log"
            fake_python = root / "python3"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$TEST_CALL_LOG\"\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            env = os.environ | {
                "PATH": f"{root}:{os.environ['PATH']}",
                "TEST_CALL_LOG": str(log),
            }
            result = subprocess.run(
                ["bash", "run_all.sh", "sweep"],
                cwd=SYSTEM,
                env=env,
                text=True,
                capture_output=True,
            )
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                calls,
                ["run_predict.py --sweep 2160", "record_picks.py --no-notify", "gen_app_data.py"],
            )

    def test_successful_refresh_dashboard_includes_new_upcoming_fixture(self):
        fixture = {
            "match_id": "late",
            "stage": "首預",
            "home": "新增主隊",
            "away": "新增客隊",
            "league": "新增聯賽",
            "kickoff_hkt": "2099-08-01 20:00",
            "conviction": 50.0,
            "candidates": [],
            "final": None,
            "open": None,
            "now": {},
            "movement": {},
            "adjustments": [],
            "mults": {},
            "outcome": None,
        }
        ledger = {
            "watch": {
                "late": {
                    "match_id": "late",
                    "home": "新增主隊",
                    "away": "新增客隊",
                    "league": "新增聯賽",
                    "kickoff": "2099-08-01 20:00",
                    "stages": [{"stage": "首預", "ts": "2099-08-01T10:00:00+08:00"}],
                }
            },
            "bets": [],
            "log": [],
            "stats": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "predictions.json").write_text(
                json.dumps([fixture], ensure_ascii=False), encoding="utf-8"
            )
            ledger_path = root / "sim_ledger.json"
            ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
            out = root / "data.json"
            with patch.object(gen_app_data, "HERE", directory), \
                 patch.object(gen_app_data, "LEDGER_PATH", str(ledger_path)), \
                 patch.object(gen_app_data, "OUT", str(out)), \
                 patch.object(gen_app_data, "PREDICTION_ARCHIVE", str(root / "archive.json")):
                gen_app_data.main()
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual([row["match_id"] for row in data["matches"]], ["late"])


if __name__ == "__main__":
    unittest.main()
