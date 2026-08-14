from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "settle-handicap-world.yml"


class HandicapWorldSettlementWorkflowTests(unittest.TestCase):
    def test_workflow_is_isolated_and_row_free(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        parsed = yaml.safe_load(text)

        self.assertIsInstance(parsed, dict)
        self.assertIn("workflow_dispatch", parsed.get(True, {}))
        self.assertIn("settle-handicap-world", text)
        self.assertIn("handicap-world-only", text)
        self.assertIn("127.0.0.1:8765/api/settle", text)
        self.assertIn("handicap_world_settled_count", text)
        self.assertNotIn("deploy/run.sh settle", text)
        self.assertNotIn("crown-run.sh settle", text)
        self.assertNotIn('payload.get("data")', text)
        self.assertNotIn("TELEGRAM", text)


if __name__ == "__main__":
    unittest.main()
