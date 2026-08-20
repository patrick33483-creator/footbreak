"""Guard the reconciliation ordering for the local Crown history repair."""
from __future__ import annotations

import unittest
from pathlib import Path


class ReconcileHistoryShapeRepairTests(unittest.TestCase):
    def test_reconcile_repairs_local_crown_history_after_settle_before_verifier(self) -> None:
        script = (
            Path(__file__).parents[2] / "deploy" / "reconcile-results.sh"
        ).read_text(encoding="utf-8")
        settle = script.index('run_reconciler "Crown" "$APP_DIR/deploy/crown-run.sh" settle')
        repair = script.index("-m crown.history_shape_repair")
        verifier = script.index('"$APP_DIR/deploy/verify-result-integrity.py"')
        self.assertLess(settle, repair)
        self.assertLess(repair, verifier)
        self.assertIn("CROWN_HISTORY_REPAIR_LOCK_TIMEOUT_SECONDS", script)
        self.assertIn("crown_history_shape_ready", script)


if __name__ == "__main__":
    unittest.main()
