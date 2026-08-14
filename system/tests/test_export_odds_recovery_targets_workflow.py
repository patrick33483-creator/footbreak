from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "export-odds-recovery-targets.yml"


class ExportOddsRecoveryTargetsWorkflowTests(unittest.TestCase):
    def test_export_is_read_only_compact_and_short_lived(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        parsed = yaml.safe_load(text)

        self.assertIsInstance(parsed, dict)
        self.assertIn("workflow_dispatch", parsed.get(True, {}))
        self.assertIn("compact_provider_target_rows", text)
        self.assertIn("/var/www/footbreak/data.json", text)
        self.assertIn("/var/lib/footbreak/crown/prediction_history.json", text)
        self.assertIn("retention-days: 1", text)
        self.assertIn("chmod 600", text)
        self.assertIn("rm -rf -- audit-targets", text)
        self.assertNotIn("--apply", text)
        self.assertNotIn("--provider-apply", text)
        self.assertNotIn("odds-recovery-overlay.json", text)
        self.assertNotIn("TELEGRAM", text)


if __name__ == "__main__":
    unittest.main()
