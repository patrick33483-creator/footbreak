"""Static guardrails for the manual missed-T-5 recovery workflow."""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/crown-t5-recovery.yml"


class T5RecoveryWorkflowTests(unittest.TestCase):
    def test_workflow_is_manual_and_parses(self):
        payload = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertIn("workflow_dispatch", payload["on"])
        self.assertIn("recover", payload["jobs"])

    def test_dry_run_precedes_confirmed_apply_and_company_three_is_fixed(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        audit = workflow.index("Dry-run audit first")
        apply = workflow.index("Confirmed production apply after audit")
        self.assertLess(audit, apply)
        self.assertIn("--dry-run --provider-company-id 3", workflow)
        self.assertIn("--apply --apply-confirmation APPLY_CROWN_T5_RECOVERY --provider-company-id 3", workflow)
        self.assertIn("inputs.apply_confirmation == 'APPLY_CROWN_T5_RECOVERY'", workflow)

    def test_workflow_has_no_provider_or_notification_path_and_keeps_artifacts_aggregate(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("titan007.com", workflow)
        self.assertNotIn("TELEGRAM", workflow)
        self.assertNotIn(" crown.settle", workflow.lower())
        self.assertIn("unexpected non-aggregate recovery audit output", workflow)
        self.assertIn("unexpected non-aggregate recovery apply output", workflow)


if __name__ == "__main__":
    unittest.main()
