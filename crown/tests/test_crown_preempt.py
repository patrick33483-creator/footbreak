"""Regression guard for deadline preemption versus bounded first-look repair."""

from pathlib import Path
import unittest


class CrownTickPreemptionTests(unittest.TestCase):
    def test_urgent_tick_does_not_kill_bounded_first_look_reconciliation(self) -> None:
        script = (
            Path(__file__).resolve().parents[2] / "deploy" / "crown-tick-preempt.sh"
        ).read_text(encoding="utf-8")
        stop_line = next(
            line for line in script.splitlines()
            if "/usr/bin/systemctl stop --no-block" in line
        )
        self.assertNotIn("crown-first-look-reconcile.service", stop_line)
        self.assertIn("crown-sweep.service", stop_line)
        self.assertIn("crown-settle.service", stop_line)


if __name__ == "__main__":
    unittest.main()
