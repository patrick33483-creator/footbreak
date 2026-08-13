"""Workflow guardrails for secure historical provider recovery."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from analysis import odds_recovery


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "historical-odds-recovery.yml"


def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


class HistoricalOddsRecoveryWorkflowTests(unittest.TestCase):
    def test_workflow_parses_as_yaml(self) -> None:
        # BaseLoader avoids YAML 1.1 treating GitHub's `on` key as a boolean.
        parsed = yaml.load(workflow_text(), Loader=yaml.BaseLoader)
        self.assertIsInstance(parsed, dict)
        self.assertIn("jobs", parsed)
        self.assertIn("recover", parsed["jobs"])

    def test_provider_apply_needs_exact_confirmation_before_write_or_regeneration(self) -> None:
        workflow = workflow_text()

        self.assertIn(
            '{ [ "${{ inputs.provider_mode }}" = "TITAN_APPLY" ] || '
            '[ "${{ inputs.provider_mode }}" = "PUBLIC_CROSSWALK_APPLY" ]; } && '
            '[ "${{ inputs.apply_confirmation }}" = '
            '"APPLY_HISTORICAL_ODDS_RECOVERY" ]',
            workflow,
        )
        confirmed_provider_apply = (
            "inputs.apply_confirmation == 'APPLY_HISTORICAL_ODDS_RECOVERY' && "
            "(inputs.provider_mode == 'LOCAL' || inputs.provider_mode == 'TITAN_APPLY' || "
            "inputs.provider_mode == 'PUBLIC_CROSSWALK_APPLY')"
        )
        self.assertEqual(workflow.count(confirmed_provider_apply), 2)
        self.assertIn(
            "unconfirmed provider APPLY mode is deliberately audit-only",
            workflow,
        )
        self.assertIn("confirmation=APPLY_HISTORICAL_ODDS_RECOVERY", workflow)
        self.assertIn("--apply-confirmation $confirmation", workflow)

    def test_local_sidecar_apply_is_not_run_for_provider_audit_mode(self) -> None:
        workflow = workflow_text()
        self.assertIn(
            "inputs.apply_confirmation == 'APPLY_HISTORICAL_ODDS_RECOVERY' && "
            "inputs.provider_mode == 'LOCAL'",
            workflow,
        )

    def test_runner_warms_only_a_private_cache_before_server_authoritative_apply(self) -> None:
        workflow = workflow_text()
        start = workflow.index("GitHub 建立私有公共供應商 crosswalk 快取並安全交接")
        runner_command = workflow.index("python3 analysis/odds_recovery.py", start)
        package = workflow.index("cache_archive=", runner_command)
        install = workflow.index("Validate every archive member", package)
        server_audit = workflow.index("The server re-reads its current complete histories", install)
        server_command = workflow.index("/opt/footbreak/analysis/odds_recovery.py", server_audit)

        runner_slice = workflow[runner_command:package]
        server_slice = workflow[server_command:server_command + 2200]
        self.assertLess(runner_command, package)
        self.assertLess(package, install)
        self.assertLess(install, server_audit)
        self.assertIn("--provider-audit", runner_slice)
        self.assertNotIn("--provider-apply", runner_slice)
        self.assertIn("--provider zgzcw", runner_slice)
        self.assertIn("--provider tipsme", runner_slice)
        self.assertIn("runner-never-written-sidecar.json", runner_slice)
        self.assertIn("--provider-cache \"$runner_tmp/provider-cache\"", runner_slice)
        self.assertIn("--footbreak-history /var/www/footbreak/data.json", server_slice)
        self.assertIn("--crown-history /var/lib/footbreak/crown/prediction_history.json", server_slice)
        self.assertIn("--sidecar /var/lib/footbreak/private/odds-recovery-overlay.json", server_slice)
        self.assertIn("--provider-cache /var/lib/footbreak/private/odds-recovery-provider-cache", server_slice)
        self.assertIn("--provider-cache-only", server_slice)
        self.assertIn("$mode", server_slice)
        self.assertIn("--apply-confirmation $confirmation", server_slice)

    def test_compact_private_target_export_is_the_only_history_copy_to_runner(self) -> None:
        workflow = workflow_text()
        start = workflow.index("GitHub 建立私有公共供應商 crosswalk 快取並安全交接")
        end = workflow.index("保存供應商輔助摘要", start)
        handoff = workflow[start:end]

        self.assertIn("Export only compact, unresolved targets with the strict fixture", handoff)
        self.assertIn("compact_provider_target_rows", handoff)
        self.assertIn("crown-provider-targets.json", handoff)
        self.assertIn("footbreak-provider-targets.json", handoff)
        self.assertIn('scp -q "$target:$remote_input_dir/crown-provider-targets.json"', handoff)
        self.assertIn('scp -q "$target:$remote_input_dir/footbreak-provider-targets.json"', handoff)
        self.assertNotIn('scp -q "$target:/var/www/footbreak/data.json"', handoff)
        self.assertNotIn('scp -q "$target:/var/lib/footbreak/crown/prediction_history.json"', handoff)
        self.assertIn("never logged", handoff)
        self.assertIn("or uploaded", handoff)

    def test_private_temps_cleanup_permissions_and_safe_extraction_are_required(self) -> None:
        workflow = workflow_text()
        start = workflow.index("GitHub 建立私有公共供應商 crosswalk 快取並安全交接")
        end = workflow.index("保存供應商輔助摘要", start)
        handoff = workflow[start:end]

        self.assertIn('runner_tmp="$(mktemp -d)"', handoff)
        self.assertIn("mktemp -d /tmp/footbreak-odds-recovery-input.XXXXXX", handoff)
        self.assertIn("mktemp -d /tmp/footbreak-odds-recovery-cache.XXXXXX", handoff)
        self.assertIn("trap cleanup EXIT", handoff)
        self.assertIn("rm -rf --", handoff)
        self.assertIn("chmod 700 \"$runner_tmp/provider-cache\"", handoff)
        self.assertIn("-exec chmod 600 {} +", handoff)
        self.assertIn("os.chmod(final_cache, 0o700)", handoff)
        self.assertIn("os.chmod(destination, 0o600)", handoff)
        self.assertIn("path_traversal", handoff)
        self.assertIn("member.issym()", handoff)
        self.assertIn("member.islnk()", handoff)
        self.assertIn("unpaired_cache_entry", handoff)
        self.assertIn("metadata_integrity", handoff)
        self.assertIn("os.replace(stage / name, final_cache / name)", handoff)

    def test_artifacts_are_aggregate_json_only_and_never_cache_or_history(self) -> None:
        parsed = yaml.load(workflow_text(), Loader=yaml.BaseLoader)
        steps = parsed["jobs"]["recover"]["steps"]
        uploads = [step["with"]["path"] for step in steps if step.get("uses") == "actions/upload-artifact@v4"]
        self.assertTrue(uploads)
        for path in uploads:
            names = [name.strip() for name in path.splitlines() if name.strip()]
            self.assertTrue(names)
            self.assertTrue(all(name.endswith(".json") for name in names), path)
            self.assertTrue(all("cache" not in name and "raw" not in name and "history" not in name for name in names), path)
        self.assertNotIn("upload-artifact@v4\n        with:\n          name: historical-odds-recovery-provider-cache", workflow_text())

    def test_titan_audit_uses_bounded_concurrent_provider_settings(self) -> None:
        workflow = workflow_text()
        self.assertIn("--provider-rate 1", workflow)
        self.assertIn("--provider-timeout 8", workflow)
        self.assertIn("--provider-retries 0", workflow)
        self.assertIn("--provider-workers 8", workflow)
        self.assertIn("--provider-max-pages 250", workflow)
        self.assertIn("--exact-window-seconds 60", workflow)
        self.assertIn("--freshness-t30-seconds 3600", workflow)
        self.assertIn("--freshness-t5-seconds 900", workflow)
        self.assertIn("--crosswalk-kickoff-tolerance-seconds 60", workflow)

    def test_public_crosswalk_mode_uses_configured_templates_on_runner_only(self) -> None:
        workflow = workflow_text()
        self.assertIn("PUBLIC_CROSSWALK_AUDIT", workflow)
        self.assertIn("PUBLIC_CROSSWALK_APPLY", workflow)
        self.assertIn("vars.ZGZCW_EVENT_URL_TEMPLATE", workflow)
        self.assertIn("vars.ZGZCW_HISTORY_URL_TEMPLATE", workflow)
        self.assertIn("vars.TIPSME_EVENT_URL_TEMPLATE", workflow)
        self.assertIn("vars.TIPSME_HISTORY_URL_TEMPLATE", workflow)
        server = workflow[workflow.index("The server re-reads its current complete histories"):]
        self.assertIn("--provider-cache-only", server)

    def test_regeneration_verifier_reads_the_same_private_overlay(self) -> None:
        workflow = workflow_text()
        expected = (
            'sudo env PYTHONPATH="$PYTHONPATH" '
            'ODDS_RECOVERY_SIDECAR="$ODDS_RECOVERY_SIDECAR" '
            '"$PYTHON" /opt/footbreak/deploy/verify-result-integrity.py'
        )
        self.assertIn(expected, workflow)

    def test_regeneration_uploads_aggregate_odds_statistics(self) -> None:
        workflow = workflow_text()
        self.assertIn("historical-odds-recovery-statistics.json", workflow)
        self.assertIn('"odds_availability": availability', workflow)
        self.assertIn('"stats": history.get("stats") or {}', workflow)
        self.assertNotIn('"rows": rows(history)', workflow)

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
