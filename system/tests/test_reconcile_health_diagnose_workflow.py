from __future__ import annotations

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "reconcile-health-diagnose.yml"


class ReconcileHealthDiagnoseWorkflowTests(unittest.TestCase):
    def test_workflow_is_valid_manual_read_only_diagnostic(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        parsed = yaml.load(text, Loader=yaml.BaseLoader)
        trigger = parsed.get("on", parsed.get(True))
        self.assertIsNotNone(trigger)
        self.assertIn("workflow_dispatch", trigger)
        inputs = trigger["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["lookback_minutes"]["options"], ["30", "90", "180"])
        self.assertEqual(inputs["journal_lines"]["options"], ["100", "250", "500"])
        self.assertEqual(parsed["permissions"]["contents"], "read")
        self.assertNotIn("actions/checkout", text)
        self.assertNotIn("upload-artifact", text)

    def test_workflow_only_emits_allowlisted_status_and_aggregate_journal_data(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("systemctl show footbreak-result-reconcile.service", text)
        self.assertIn("systemctl show footbreak-result-reconcile.timer", text)
        self.assertIn("journalctl -u footbreak-result-reconcile.service", text)
        self.assertIn("raw_journal_lines_printed=0 raw_journal_lines_uploaded=0", text)
        self.assertIn("No environment, credential, team identifier, state file, or provider response", text)
        self.assertNotIn("journalctl -u footbreak-result-reconcile.service --no-pager", text)
        self.assertNotIn('echo "$journal"', text)
        self.assertNotIn("cat /etc/footbreak", text)
        self.assertNotIn("printenv", text)
        self.assertNotIn("env |", text)

    def test_workflow_bounds_and_validates_all_dispatch_inputs_before_use(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('case "$LOOKBACK_MINUTES" in 30|90|180)', text)
        self.assertIn('case "$JOURNAL_LINES" in 100|250|500)', text)
        self.assertIn('printf \'LOOKBACK_MINUTES=%q\\n\' "$LOOKBACK_MINUTES"', text)
        self.assertIn('printf \'JOURNAL_LINES=%q\\n\' "$JOURNAL_LINES"', text)
        self.assertIn('--since "-${LOOKBACK_MINUTES} minutes"', text)
        self.assertIn('-n "$JOURNAL_LINES"', text)


if __name__ == "__main__":
    unittest.main()
