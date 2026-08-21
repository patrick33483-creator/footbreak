"""Provider-free checks for the production cross-book audit utility."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deploy" / "audit-footbreak-crown-execution.py"
SPEC = importlib.util.spec_from_file_location("cross_book_audit", MODULE_PATH)
assert SPEC and SPEC.loader
audit_tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit_tool
SPEC.loader.exec_module(audit_tool)


class CrossBookAuditTests(unittest.TestCase):
    def test_reads_exact_t5_sidecar_and_never_calls_provider(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sidecar = root / "evidence.json"
            sidecar.write_text(json.dumps([{
                "hkjc_match_id": "fixture-1",
                "kickoff_hkt": (now + timedelta(minutes=30)).isoformat(),
                "current_selected_odds_journal": [{
                    "code": "HIL", "side": "H", "line": 2.5, "odds": 1.9,
                    "odds_status": "available", "source": "titan007-crown-id-3",
                    "observed_at": (now - timedelta(seconds=10)).isoformat(),
                }],
            }]), encoding="utf-8")
            ledger = root / "ledger.json"
            ledger.write_text(json.dumps({"footbreak_crown_execution_test": {
                "bets": [], "audit": [{"ts": now.isoformat(), "status": "SKIPPED",
                                        "reason": "no_live_candidate"}],
                "stats": {"n_pending": 0, "rejections": {"no_live_candidate": 1}},
            }, "watch": {
                "fixture-1": {
                    "match_id": "fixture-1",
                    "kickoff": (now + timedelta(minutes=30)).isoformat(),
                    "counterpart_bridges": {"crown": {
                        "first_look": {"status": "RESOLVED"},
                        "t30": {"status": "UNAVAILABLE",
                                "reason": "crown_t30_exact_line_unavailable"},
                    }},
                },
            }}), encoding="utf-8")
            notify_state = root / "notify.json"
            notify_state.write_text("{}", encoding="utf-8")
            dashboard = root / "dashboard.json"
            dashboard.write_text(json.dumps({"crown_execution_test": {
                "display_name": "足破×皇冠執行測試倉（模擬）", "bets": [], "rejections": {},
            }}), encoding="utf-8")
            unit = root / "footbreak-tick.service"
            unit.write_text(
                "Environment=FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH="
                "/var/lib/footbreak/crown/footbreak-execution-evidence.json\n"
                "ReadOnlyPaths=/var/lib/footbreak/crown/footbreak-execution-evidence.json\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                production_head="test-head", evidence_path=str(sidecar),
                ledger_path=str(ledger), notify_state_path=str(notify_state),
                dashboard_path=str(dashboard), unit_path=str(unit),
            )
            with patch.dict(
                os.environ,
                {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": "unchanged-after-audit"},
            ), patch.object(
                audit_tool, "_recent_journal", return_value={"available": True, "recent_errors": []},
            ):
                result = audit_tool.audit(args)
                self.assertEqual(
                    os.environ["FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH"],
                    "unchanged-after-audit",
                )
        self.assertTrue(result["safe_read_only"])
        self.assertFalse(result["providers_called"])
        self.assertTrue(result["sidecar"]["readable_by_footbreak_adapter"])
        witness = result["sidecar"]["structural_exact_match_witnesses"][0]
        self.assertTrue(witness["exact_match_accepted"])
        self.assertTrue(witness["mismatched_line_rejected"])
        self.assertTrue(result["service_contract"]["read_only_path_present"])
        self.assertTrue(result["dashboard"]["platform_crown_label_present"])
        bridges = result["ledger"]["counterpart_bridge_summary"]
        self.assertEqual(bridges["upcoming_fixture_count"], 1)
        self.assertEqual(bridges["stage_counts"]["first_look"]["resolved"], 1)
        self.assertEqual(bridges["stage_counts"]["t30"]["unavailable"], 1)
        self.assertEqual(
            bridges["stage_counts"]["t30"]["reasons"]["crown_t30_exact_line_unavailable"],
            1,
        )
        self.assertEqual(bridges["stage_counts"]["t5"]["missing"], 1)
