from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "settle-handicap-world.yml"


class RetiredPortfolioWorkflowTests(unittest.TestCase):
    def test_workflow_is_explicitly_retired_and_has_no_state_or_network_actions(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        parsed = yaml.safe_load(text)
        self.assertIsInstance(parsed, dict)
        self.assertIn("workflow_dispatch", parsed.get(True, {}))
        self.assertIn("retired", text.lower())
        for forbidden in ("ssh ", "curl", "shadow_bets", "handicap_world", "crown-run.sh", "record_new_t5"):
            self.assertNotIn(forbidden, text.lower())
        steps = parsed["jobs"]["retired"]["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["name"], "Confirm retirement")


if __name__ == "__main__":
    unittest.main()
