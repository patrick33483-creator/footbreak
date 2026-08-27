import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "audit-granular-condition-window.py"
SPEC = importlib.util.spec_from_file_location("condition_window_audit", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class GranularConditionWindowAuditTests(unittest.TestCase):
    def row(self, *, status="PENDING", kickoff="2026-08-27T01:00:00+08:00"):
        return {
            "observation_id": "m1|HIL|T-5|sig|low-odds",
            "portfolio": "footbreak_wilson_observations",
            "formal_bet": False,
            "match_id": "m1",
            "home": "主",
            "away": "客",
            "kickoff": kickoff,
            "stage": "T-5",
            "code": "HIL",
            "selected_side": "H",
            "selected_role": "大",
            "selected_line": 2.5,
            "odds": 1.8,
            "condition_number": 7,
            "frozen_condition_signature": "sig",
            "frozen_condition_definition": {"market": "HIL", "path": "T-5"},
            "status": status,
        }

    def test_pending_with_exact_learning_result_is_repair_candidate(self):
        ledgers = {
            "footbreak": {
                "bets": [],
                "wilson_validation": {"observations": [self.row()]},
            },
            "crown": {"bets": [], "wilson_validation": {"observations": []}},
        }
        result = MODULE.audit(
            ledgers,
            {("footbreak", "m1"): {
                "home_score": 2, "away_score": 1, "terminal_status": "FINISHED",
            }},
            MODULE.parse_time("2026-08-26T11:59:00+08:00"),
            MODULE.parse_time("2026-08-27T11:59:00+08:00"),
            MODULE.parse_time("2026-08-27T05:00:00+08:00"),
        )
        self.assertEqual(result["summary"]["pending_overdue_with_candidate"], 1)
        self.assertEqual(len(result["pending"]["repair_candidates"]), 1)

    def test_future_pending_is_not_due(self):
        ledgers = {
            "footbreak": {
                "bets": [],
                "wilson_validation": {
                    "observations": [self.row(kickoff="2026-08-27T10:00:00+08:00")]
                },
            },
            "crown": {"bets": [], "wilson_validation": {"observations": []}},
        }
        result = MODULE.audit(
            ledgers, {},
            MODULE.parse_time("2026-08-26T11:59:00+08:00"),
            MODULE.parse_time("2026-08-27T11:59:00+08:00"),
            MODULE.parse_time("2026-08-27T09:00:00+08:00"),
        )
        self.assertEqual(result["summary"]["pending_not_due"], 1)
        self.assertEqual(result["summary"]["pending_overdue_without_candidate"], 0)

    def test_end_boundary_is_exclusive(self):
        ledgers = {
            "footbreak": {
                "bets": [],
                "wilson_validation": {
                    "observations": [self.row(kickoff="2026-08-27T11:59:00+08:00")]
                },
            },
            "crown": {"bets": [], "wilson_validation": {"observations": []}},
        }
        result = MODULE.audit(
            ledgers, {},
            MODULE.parse_time("2026-08-26T11:59:00+08:00"),
            MODULE.parse_time("2026-08-27T11:59:00+08:00"),
            MODULE.parse_time("2026-08-27T12:00:00+08:00"),
        )
        self.assertEqual(result["summary"]["entries"], 0)


if __name__ == "__main__":
    unittest.main()
