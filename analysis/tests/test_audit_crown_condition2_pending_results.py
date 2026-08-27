from __future__ import annotations

import unittest

from analysis.audit_crown_condition2_pending_results import (
    _audit_row, _same_identity, _trusted_terminal,
)


def match(source: dict | None = None) -> dict:
    return {
        "fixture": "12345",
        "kickoff": "2026-08-01T20:00:00+08:00",
        "stage_at": "2026-08-01T10:00:00+08:00",
        "terminal": {"side": "H", "selected_line": 2.75, "odds": 1.8},
        "source": source or {},
    }


def recovered() -> dict:
    return {
        "match_id": "12345",
        "kickoff": "2026-08-01T20:00:00+08:00",
        "stage_at": "2026-08-01T10:00:00+08:00",
        "league": "L", "home": "H", "away": "A",
        "side": "H", "line": 2.75, "odds": 1.8, "result": "PENDING",
    }


class PendingResultAuditTest(unittest.TestCase):
    def test_exact_identity_uses_stage_side_line_and_odds(self) -> None:
        self.assertEqual(_same_identity(recovered(), match()), (True, None))
        changed = recovered()
        changed["odds"] = 1.81
        self.assertEqual(_same_identity(changed, match()), (False, "odds_mismatch"))

    def test_only_accepts_trusted_exact_terminal_source(self) -> None:
        source = {
            "result_status": "不計",
            "result_source": "hkjc_official_exact_id_terminal_status",
            "verified_at": "2026-08-02T00:00:00+08:00",
            "result_detail": {"terminal_status": "POSTPONED"},
        }
        value = _trusted_terminal(match(source))
        self.assertEqual(value[0], "Refunded")

        source["result_source"] = "team_name_guess"
        self.assertIsNone(_trusted_terminal(match(source)))

    def test_missing_history_is_reported_not_guessed(self) -> None:
        row = _audit_row(recovered(), {})
        self.assertEqual(row["audit_status"], "unresolved")
        self.assertEqual(row["reason"], "history_match_missing")

    def test_verified_exact_hil_grade_is_resolved(self) -> None:
        source = {
            "verified_at": "2026-08-02T00:00:00+08:00",
            "result_source": "hkjc_official_exact_id",
            "score": "3-1",
            "market_grades": [{
                "code": "HIL", "side": "H", "line": 2.75,
                "grade_status": "GRADED", "settlement": "Won",
            }],
        }
        row = _audit_row(recovered(), {"12345": match(source)})
        self.assertEqual(row["audit_status"], "resolved")
        self.assertEqual(row["result"], "Won")


if __name__ == "__main__":
    unittest.main()
