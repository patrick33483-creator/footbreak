from __future__ import annotations

import unittest

from analysis.audit_nls_south_result_backfill import (
    EXCLUDED,
    NLS_TEAM_IDENTITIES,
    VERIFIED_RESULTS,
    build_proposal,
)


def row(
    match_id: str = "3028840",
    day: str = "2026-08-31",
    home: str = "富莫咸普顿",
    away: str = "梅德斯托联",
) -> dict:
    return {
        "match_id": match_id,
        "stage": "T-5",
        "league": "英议南",
        "home": home,
        "away": away,
        "kickoff": f"{day}T22:00:00+08:00",
        "result_status": "待賽果",
        "market_predictions": [],
    }


def complete_history() -> dict:
    return {
        "rows": [
            row(match_id, day, home, away)
            for match_id, day, home, away, _score in VERIFIED_RESULTS
        ]
    }


class NlsSouthResultBackfillTest(unittest.TestCase):
    def test_reviewed_aliases_are_unambiguous(self) -> None:
        self.assertEqual(
            NLS_TEAM_IDENTITIES["富莫咸普顿"], "Hampton & Richmond"
        )
        self.assertEqual(
            NLS_TEAM_IDENTITIES["希美咸史特城"], "Hemel Hempstead Town"
        )
        self.assertEqual(
            NLS_TEAM_IDENTITIES["麦德黑联"], "Maidenhead United"
        )
        self.assertEqual(len(set(NLS_TEAM_IDENTITIES.values())), len(NLS_TEAM_IDENTITIES))

    def test_manifest_has_41_scores_and_two_fail_closed_exclusions(self) -> None:
        self.assertEqual(len(VERIFIED_RESULTS), 41)
        self.assertEqual({item["match_id"] for item in EXCLUDED}, {"3028808", "3028837"})
        self.assertEqual(
            {item["reason"] for item in EXCLUDED},
            {"kickoff_date_mismatch", "postponed_no_final_score"},
        )

    def test_builds_proposal_without_mutating_input(self) -> None:
        history = complete_history()
        proposed, report = build_proposal(history)
        self.assertEqual(report["changed_rows"], 41)
        self.assertTrue(all(row["result_status"] == "待賽果" for row in history["rows"]))
        self.assertTrue(all(row["result_status"] == "已核對" for row in proposed["rows"]))
        target = next(
            row for row in proposed["rows"] if row["match_id"] == "3028840"
        )
        self.assertEqual(target["score"], "1-4")
        self.assertEqual(
            target["result_detail"]["provider_home"], "Hampton & Richmond"
        )

    def test_refuses_team_identity_mismatch(self) -> None:
        history = complete_history()
        history["rows"][0]["away"] = "錯隊"
        with self.assertRaisesRegex(ValueError, "team identity mismatch"):
            build_proposal(history)

    def test_same_score_is_idempotent(self) -> None:
        history = complete_history()
        target = next(row for row in history["rows"] if row["match_id"] == "3028840")
        target.update({"result_status": "已核對", "score": "1-4"})
        _proposed, report = build_proposal(history)
        self.assertEqual(report["changed_rows"], 40)
        self.assertEqual(report["already_rows"], 1)

    def test_refuses_conflicting_final_score(self) -> None:
        history = complete_history()
        target = next(row for row in history["rows"] if row["match_id"] == "3028840")
        target.update({"result_status": "已核對", "score": "2-0"})
        with self.assertRaisesRegex(ValueError, "existing result conflict"):
            build_proposal(history)


if __name__ == "__main__":
    unittest.main()
