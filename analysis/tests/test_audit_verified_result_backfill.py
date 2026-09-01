from __future__ import annotations

import copy
import unittest

from analysis.audit_verified_result_backfill import build_proposal


def _row(match_id: str, league: str, home: str, away: str, day: str) -> dict:
    return {
        "match_id": match_id,
        "stage": "T-5",
        "league": league,
        "home": home,
        "away": away,
        "kickoff": f"{day}T22:00:00+08:00",
        "result_status": "待賽果",
        "market_predictions": [],
    }


def _sample_verified() -> dict:
    return {
        "summary": {"total": 2, "completed": 2},
        "backfill_mapping": {"1000001": "1-1", "1000002": "2-0"},
        "matches": [
            {
                "match_id": "1000001",
                "league_orig": "科威特联",
                "home_orig": "卡达西亚SC",
                "away_orig": "阿尔科威特",
                "hk_time": "2026-09-01T01:55:00+08:00",
                "score": "1-1",
                "status": "completed",
                "source_name": "FootLive",
                "source_url": "https://example.test/kw",
                "note": "n/a",
            },
            {
                "match_id": "1000002",
                "league_orig": "亚洲杯U20",
                "home_orig": "塔吉克斯坦U20",
                "away_orig": "巴林U20",
                "hk_time": "2026-09-01T01:30:00+08:00",
                "score": "2-0",
                "status": "completed",
                "source_name": "The AFC",
                "source_url": "https://example.test/afc",
                "note": "n/a",
            },
        ],
    }


def _sample_history() -> dict:
    return {
        "rows": [
            _row("1000001", "科威特联", "卡达西亚SC", "阿尔科威特", "2026-09-01"),
            _row("1000002", "亚洲杯U20", "塔吉克斯坦U20", "巴林U20", "2026-09-01"),
        ],
    }


class VerifiedResultBackfillTest(unittest.TestCase):
    def test_apply_changes_expected_rows_and_leaves_input_untouched(self) -> None:
        history = _sample_history()
        snapshot = copy.deepcopy(history)
        proposed, report = build_proposal(history, _sample_verified())
        self.assertEqual(report["verified_fixtures"], 2)
        self.assertEqual(report["changed_rows"], 2)
        self.assertEqual(report["already_rows"], 0)
        self.assertEqual(history, snapshot)
        for row in proposed["rows"]:
            self.assertEqual(row["result_status"], "已核對")
        applied = report["applied"]
        self.assertEqual({item["match_id"] for item in applied}, {"1000001", "1000002"})

    def test_rejects_league_mismatch(self) -> None:
        history = _sample_history()
        history["rows"][0]["league"] = "英超"
        with self.assertRaisesRegex(ValueError, "history league mismatch"):
            build_proposal(history, _sample_verified())

    def test_rejects_team_mismatch(self) -> None:
        history = _sample_history()
        history["rows"][0]["away"] = "錯隊"
        with self.assertRaisesRegex(ValueError, "history team identity mismatch"):
            build_proposal(history, _sample_verified())

    def test_rejects_date_mismatch(self) -> None:
        history = _sample_history()
        history["rows"][0]["kickoff"] = "2025-01-01T00:00:00+08:00"
        with self.assertRaisesRegex(ValueError, "history kickoff date mismatch"):
            build_proposal(history, _sample_verified())

    def test_rejects_conflicting_existing_score(self) -> None:
        history = _sample_history()
        history["rows"][0].update({"result_status": "已核對", "score": "3-3"})
        with self.assertRaisesRegex(ValueError, "existing result conflict"):
            build_proposal(history, _sample_verified())

    def test_treats_same_final_score_as_idempotent(self) -> None:
        history = _sample_history()
        history["rows"][0].update({"result_status": "已核對", "score": "1-1"})
        _, report = build_proposal(history, _sample_verified())
        self.assertEqual(report["changed_rows"], 1)
        self.assertEqual(report["already_rows"], 1)

    def test_rejects_non_completed_batch(self) -> None:
        verified = _sample_verified()
        verified["matches"][0]["status"] = "postponed"
        with self.assertRaisesRegex(ValueError, "non-completed fixture"):
            build_proposal(_sample_history(), verified)

    def test_rejects_missing_match_in_history(self) -> None:
        history = _sample_history()
        history["rows"] = [row for row in history["rows"] if row["match_id"] != "1000001"]
        with self.assertRaisesRegex(ValueError, "history fixture missing"):
            build_proposal(history, _sample_verified())


if __name__ == "__main__":
    unittest.main()
