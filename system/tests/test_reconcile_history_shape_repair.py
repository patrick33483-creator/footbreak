"""Guard the reconciliation ordering for the local Crown history repair."""
from __future__ import annotations

import unittest
from pathlib import Path


class ReconcileHistoryShapeRepairTests(unittest.TestCase):
    def test_reconcile_repairs_before_crown_settle_publishes_and_verifies(self) -> None:
        script = (
            Path(__file__).parents[2] / "deploy" / "reconcile-results.sh"
        ).read_text(encoding="utf-8")
        settle = script.index('run_reconciler "Crown" "$APP_DIR/deploy/crown-run.sh" settle')
        repair = script.index("-m crown.history_shape_repair")
        verifier = script.index('"$APP_DIR/deploy/verify-result-integrity.py"')
        self.assertLess(repair, settle)
        self.assertLess(settle, verifier)
        self.assertIn("CROWN_HISTORY_REPAIR_LOCK_TIMEOUT_SECONDS", script)
        self.assertIn("crown_history_shape_ready", script)
        self.assertIn(
            'if [ "$crown_history_shape_ready" -eq 1 ]; then',
            script,
        )
        self.assertIn(
            'elif [ "$crown_history_shape_repair_rc" -eq 75 ]; then',
            script,
        )
        self.assertIn(
            "Crown local history-shape repair busy; automatic retry remains scheduled",
            script,
        )
        self.assertIn(
            "Crown local history-shape repair failed rc=$crown_history_shape_repair_rc",
            script,
        )


if __name__ == "__main__":
    unittest.main()
