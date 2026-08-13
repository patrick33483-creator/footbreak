"""Workflow guardrails for explicit historical provider applies."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from analysis import odds_recovery


ROOT = Path(__file__).resolve().parents[2]


class HistoricalOddsRecoveryWorkflowTests(unittest.TestCase):
    def test_titan_apply_needs_exact_confirmation_before_write_or_regeneration(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "historical-odds-recovery.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            '[ "${{ inputs.provider_mode }}" = "TITAN_APPLY" ] && '
            '[ "${{ inputs.apply_confirmation }}" = '
            '"APPLY_HISTORICAL_ODDS_RECOVERY" ]',
            workflow,
        )
        confirmed_provider_apply = (
            "inputs.apply_confirmation == 'APPLY_HISTORICAL_ODDS_RECOVERY' && "
            "(inputs.provider_mode == 'LOCAL' || inputs.provider_mode == 'TITAN_APPLY')"
        )
        self.assertEqual(workflow.count(confirmed_provider_apply), 2)
        # TITAN_APPLY can appear in the audit-artifact step, but the two
        # projection regeneration conditions must be the confirmed form only.
        self.assertIn(
            "unconfirmed TITAN_APPLY is deliberately audit-only",
            workflow,
        )
        self.assertIn(
            "--apply-confirmation '${{ inputs.apply_confirmation }}'",
            workflow,
        )

    def test_local_sidecar_apply_is_not_run_for_provider_audit_mode(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "historical-odds-recovery.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "inputs.apply_confirmation == 'APPLY_HISTORICAL_ODDS_RECOVERY' && "
            "inputs.provider_mode == 'LOCAL'",
            workflow,
        )

    def test_cli_provider_apply_fails_before_provider_access_without_confirmation(self) -> None:
        args = [
            "odds_recovery.py",
            "--footbreak-history", "/does-not-matter-footbreak.json",
            "--crown-history", "/does-not-matter-crown.json",
            "--sidecar", "/does-not-matter-sidecar.json",
            "--provider-apply", "--provider", "titan",
            "--provider-cache", "/does-not-matter-cache",
        ]
        with patch("sys.argv", args), self.assertRaises(SystemExit) as exit_code:
            odds_recovery.main()
        self.assertEqual(exit_code.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
