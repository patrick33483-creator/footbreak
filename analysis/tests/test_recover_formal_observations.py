from __future__ import annotations

import copy
import unittest

from analysis.recover_formal_observations import recover_system
from analysis.wilson_validation import (
    admission_arithmetic, condition_signature, freeze_condition, ensure_namespace,
    formal_registry_candidates, match_formal_registry,
)


STAGE_AT = "2026-08-21T18:21:23+08:00"
KICKOFF = "2026-08-21T18:30:00+08:00"


def formal_candidate() -> dict:
    return {
        "system": "footbreak", "version": "granular-condition-v1", "market": "HIL",
        "key": [
            "system=footbreak", "market=HIL", "path=T-5",
            "decision=T-5", "tier=<1.70", "direction=A",
            "role=大", "bucket=≤2.5", "movement=不變",
        ],
        "path": "T-5", "decision_stage": "T-5", "odds_tier": "<1.70",
        "direction": "A", "role": "大", "line_bucket": "≤2.5",
        "movement": "不變", "selected_side": "H", "selected_line": 2.5,
        "total": {"hits": 41, "decided": 59, "pushes": 0},
        "source_artifact": {"hash": "immutable-discovery", "version": "v1",
                            "as_of": "2026-08-20T00:00:00+08:00"},
    }


def legacy_row(signature: str, *, suffix: str = "") -> dict:
    return {
        "observation_id": f"50073037|HIL|T-5|{signature}|low-odds{suffix}",
        "portfolio": "footbreak_wilson_observations",
        "strategy": "wilson-test-strategy-v1", "formal_bet": False,
        "bet_status": "NO_BET_LOW_ODDS", "match_id": "50073037",
        "home": "FC東京", "away": "千葉市原", "kickoff": KICKOFF,
        "code": "HIL", "market": "HIL", "side": "H",
        "line": 2.5, "condition": 2.5, "odds": 1.50, "stage": "T-5",
        "created_at": STAGE_AT, "condition_number": 7,
        "frozen_condition_signature": signature, "status": None,
    }


def history(*, graded: bool = True) -> list[dict]:
    grade = {"code": "HIL", "side": "H", "line": 2.5, "grade_status": "GRADED", "hit": True}
    if not graded:
        grade = {"code": "HIL", "side": "H", "line": 2.5, "grade_status": "PENDING"}
    return [{
        "match_id": "50073037", "stage": "T-5", "ts": STAGE_AT,
        "kickoff": KICKOFF, "market_predictions": [{
            "code": "HIL", "side": "H", "line": 2.5, "odds": 1.50,
            "quote_source": "hkjc_public_board",
            "observed_at": "2026-08-21T18:20:50+08:00",
        }], "market_grades": [grade],
    }]


class FormalObservationRecoveryTest(unittest.TestCase):
    def _ledger(self, *, duplicate: bool = False) -> dict:
        candidate = formal_candidate()
        signature, definition = condition_signature("footbreak", candidate)
        admission = {
            "signature": signature, "definition": definition,
            "history": {"hits": 41, "decided": 59, "pushes": 0,
                        "artifact": copy.deepcopy(candidate["source_artifact"])},
            "arithmetic": admission_arithmetic(41, 59, 1.5),
        }
        ledger = {"bets": []}
        freeze_condition(ledger, "footbreak", admission, now="2026-08-20T12:00:00+08:00")
        ns = ensure_namespace(ledger, "footbreak")
        ns["conditions"][signature]["condition_number"] = 7
        ns["observations"] = [legacy_row(signature)]
        if duplicate:
            ns["observations"].append(legacy_row(signature, suffix="-duplicate"))
        return ledger

    def test_proven_legacy_low_odds_recovers_once_without_pnl(self):
        ledger = self._ledger()
        audit = recover_system(ledger, history(), "footbreak", apply=False)
        self.assertEqual((audit["accepted"], audit["rejected"]), (1, 0))
        applied = recover_system(ledger, history(), "footbreak", apply=True)
        self.assertEqual((applied["accepted"], applied["rejected"]), (1, 0))
        row = ledger["wilson_validation"]["observations"][0]
        self.assertEqual((row["match_id"], row["home"], row["away"], row["condition_number"]),
                         ("50073037", "FC東京", "千葉市原", 7))
        self.assertEqual((row["status"], row["result"]), ("SETTLED", "Won"))
        self.assertNotIn("pnl", row)
        self.assertNotIn("stake", row)
        self.assertTrue(row["recovered_formal_observation"]["admitted_without_result_input"])
        frozen = ledger["wilson_validation"]["conditions"][row["frozen_condition_signature"]]
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "1/20")
        rerun = recover_system(ledger, history(), "footbreak", apply=True)
        self.assertEqual((rerun["accepted"], rerun["rejected"], rerun["skipped"]), (0, 0, 1))
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "1/20")

    def test_missing_normal_grade_and_duplicate_conflict_fail_closed(self):
        missing = recover_system(self._ledger(), history(graded=False), "footbreak", apply=False)
        self.assertEqual(missing["accepted"], 0)
        self.assertEqual(missing["reasons"]["normal_result_grade_missing_or_ambiguous"], 1)
        conflict = recover_system(self._ledger(duplicate=True), history(), "footbreak", apply=False)
        self.assertEqual(conflict["accepted"], 0)
        self.assertEqual(conflict["reasons"]["duplicate_or_conflicting_observation"], 2)

    def test_native_stage_under_immutable_watch_wrapper_is_accepted(self):
        ledger = self._ledger()
        native = history()[0]
        nested_watch = {
            "50073037": {
                "match_id": "50073037",
                "kickoff": KICKOFF,
                "stages": [{
                    key: value for key, value in native.items()
                    if key not in {"match_id", "kickoff", "market_grades"}
                }],
            },
        }
        ledger["watch"] = nested_watch
        grade_only = copy.deepcopy(native)
        grade_only.pop("market_predictions")
        audit = recover_system(ledger, [grade_only], "footbreak", apply=False)
        self.assertEqual((audit["accepted"], audit["rejected"]), (1, 0))

    def test_registry_matcher_rejects_research_and_mismatched_native_rows(self):
        ledger = self._ledger()
        registry = formal_registry_candidates(ledger, "footbreak")
        current = [{
            "match_id": "50073037", "stage": "T-5", "kickoff": KICKOFF,
            "predicted_at": STAGE_AT, "market_predictions": [{
                "code": "HIL", "side": "H", "line": 2.5, "odds": 1.50,
                "quote_source": "hkjc_public_board",
                "observed_at": "2026-08-21T18:20:50+08:00",
            }],
        }]
        self.assertIn("50073037", match_formal_registry(
            current, registry, system="footbreak",
        ))
        # A structurally identical mutable card is still research unless it
        # carries the immutable registry proof marker.
        research = copy.deepcopy(registry[0])
        research.pop("__formal_frozen_signature")
        self.assertEqual(match_formal_registry(
            current, [research], system="footbreak",
        ), {})
        mismatched = copy.deepcopy(current)
        mismatched[0]["market_predictions"][0]["side"] = "L"
        self.assertEqual(match_formal_registry(
            mismatched, registry, system="footbreak",
        ), {})
