import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "wilson-production-offline-audit.yml"


class WilsonProductionOfflineAuditWorkflowTests(unittest.TestCase):
    def test_workflow_is_manual_read_only_and_uses_dispatched_commit(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        parsed = yaml.safe_load(text)
        trigger = parsed.get(True, {})

        self.assertIn("workflow_dispatch", trigger)
        self.assertNotIn("push", trigger)
        self.assertEqual(parsed.get("permissions"), {"contents": "read"})
        self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', text)
        self.assertNotIn("c18050edd0b0727308dc16bbe5e44bd2cd14dec1", text)
        self.assertNotIn("git push", text)

    def test_workflow_never_uploads_raw_ledgers(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertNotIn("audit-input/footbreak-ledger.json\n            ", text)
        self.assertNotIn("audit-input/crown-ledger.json\n            ", text)
        self.assertIn("ledger-sha256.txt", text)
        self.assertIn("footbreak-wilson-registry-audit.json", text)
        self.assertIn("crown-wilson-registry-audit.json", text)

    def test_workflow_enforces_exact_37_condition_release_gate(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('expected_counts = {"footbreak": 17, "crown": 20}', text)
        self.assertIn("if observed_total != 37:", text)
        self.assertIn('condition.get("valid") is not True', text)
        self.assertIn(
            'condition.get("own_stage_matcher_can_structurally_admit") is not True',
            text,
        )
        self.assertIn("--require-valid", text)


if __name__ == "__main__":
    unittest.main()
