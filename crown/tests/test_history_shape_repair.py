"""Regression coverage for the provider-free persisted Crown history repair."""
from __future__ import annotations

import copy
import json
import os
import runpy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from crown.config import settings
from crown.history_shape_repair import (
    PersistedHistoryShapeBusy,
    main,
    repair_persisted_history_shape,
)
from crown.prediction_history import (
    HistoryShapeConflict,
    HistoryShapeMissingIdentity,
)
from crown.state import state_lock


class PersistedHistoryShapeRepairTests(unittest.TestCase):
    def _config(self, directory: str):
        return replace(settings(), state_dir=Path(directory))

    @staticmethod
    def _row(
        match_id: str,
        stage: str,
        kickoff: str,
        predicted_at: str,
        *,
        score: str,
    ) -> dict[str, object]:
        return {
            "match_id": match_id,
            "stage": stage,
            "kickoff": kickoff,
            "predicted_at": predicted_at,
            "forecast": "主勝",
            "result_status": "已核對",
            "score": score,
            "result_detail": {
                "source": "already-persisted",
                "nested_evidence": ["keep", {"exact": True}],
            },
            "market_predictions": [{
                "code": "HDC", "line": -0.25, "side": "H", "odds": 1.91,
            }],
            "market_grades": [{
                "code": "HDC", "grade_status": "GRADED", "hit": True,
            }],
        }

    def _write(self, config, history: dict[str, object]) -> tuple[Path, str]:
        path = config.state_dir / "prediction_history.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(history, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        return path, path.read_text(encoding="utf-8")

    def test_unsorted_unique_rows_become_verifier_compatible_without_field_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            older = self._row(
                "older", "T-5", "2026-08-10T18:00:00+08:00",
                "2026-08-10T17:55:00+08:00", score="1-0",
            )
            same_t30 = self._row(
                "same", "T-30", "2026-08-20T19:00:00+08:00",
                "2026-08-20T18:30:00+08:00", score="2-1",
            )
            same_t5 = self._row(
                "same", "T-5", "2026-08-20T19:00:00+08:00",
                "2026-08-20T18:55:00+08:00", score="2-1",
            )
            newest = self._row(
                "newest", "T-5", "2026-08-21T19:00:00+08:00",
                "2026-08-21T18:55:00+08:00", score="3-2",
            )
            source = {
                "rows": [older, same_t30, newest, same_t5],
                "stats": {"unchanged_cache": {"graded": 4}},
                "operator_note": "not derived by this repair",
            }
            path, _ = self._write(config, copy.deepcopy(source))

            result = repair_persisted_history_shape(config, lock_timeout_seconds=0.1)
            repaired = json.loads(path.read_text(encoding="utf-8"))

            self.assertTrue(result.changed)
            self.assertEqual(result.rows, 4)
            verifier = runpy.run_path(
                str(Path(__file__).parents[2] / "deploy" / "verify-result-integrity.py"),
            )
            verifier["assert_unique_and_sorted"]("Crown", repaired["rows"])
            self.assertEqual(
                [(row["match_id"], row["stage"]) for row in repaired["rows"]],
                [
                    ("newest", "T-5"),
                    ("same", "T-5"),
                    ("same", "T-30"),
                    ("older", "T-5"),
                ],
            )
            # The repair only reorders the list: all persisted evidence and
            # unrelated envelope fields remain semantically identical.
            expected = copy.deepcopy(source)
            expected["rows"] = [newest, same_t5, same_t30, older]
            self.assertEqual(repaired, expected)

    def test_missing_identity_or_conflicting_duplicate_fails_before_write(self) -> None:
        cases = {
            "missing": ({
                "rows": [self._row(
                    "", "T-5", "2026-08-20T19:00:00+08:00",
                    "2026-08-20T18:55:00+08:00", score="1-0",
                )],
                "stats": {},
            }, HistoryShapeMissingIdentity),
            "conflicting": ({
                "rows": [self._row(
                    "conflict", "T-5", "2026-08-20T19:00:00+08:00",
                    "2026-08-20T18:55:00+08:00", score="1-0",
                ), self._row(
                    "conflict", "T-5", "2026-08-20T19:00:00+08:00",
                    "2026-08-20T18:55:00+08:00", score="2-0",
                )],
                "stats": {},
            }, HistoryShapeConflict),
        }
        for label, (history, error_type) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                config = self._config(directory)
                path, original = self._write(config, history)
                with self.assertRaises(error_type):
                    repair_persisted_history_shape(config, lock_timeout_seconds=0.1)
                self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_lock_contention_returns_retryable_cli_status_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            path, original = self._write(config, {
                "rows": [self._row(
                    "one", "T-5", "2026-08-20T19:00:00+08:00",
                    "2026-08-20T18:55:00+08:00", score="1-0",
                )],
                "stats": {},
            })
            with state_lock(config):
                with self.assertRaises(PersistedHistoryShapeBusy):
                    repair_persisted_history_shape(config, lock_timeout_seconds=0.01)
                old_state_dir = os.environ.get("CROWN_STATE_DIR")
                os.environ["CROWN_STATE_DIR"] = str(config.state_dir)
                try:
                    self.assertEqual(
                        main(["--lock-timeout-seconds", "0.01"]),
                        os.EX_TEMPFAIL,
                    )
                finally:
                    if old_state_dir is None:
                        del os.environ["CROWN_STATE_DIR"]
                    else:
                        os.environ["CROWN_STATE_DIR"] = old_state_dir
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
