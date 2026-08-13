from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
RECONCILE = ROOT / "deploy" / "reconcile-results.sh"


class ReconciliationEnablementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        (self.directory / "deploy").mkdir()
        (self.directory / ".venv" / "bin").mkdir(parents=True)
        (self.directory / "deploy" / "run.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
        )
        (self.directory / "deploy" / "crown-run.sh").write_text(
            "#!/usr/bin/env bash\n"
            "printf 'called\\n' >> \"$CROWN_CALLED\"\n"
            "exit 3\n",
            encoding="utf-8",
        )
        (self.directory / ".venv" / "bin" / "python3").write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"${1:-}\" == */verify-result-integrity.py ]] && "
            "[[ -n \"${INTEGRITY_FAIL_ONCE_FILE:-}\" ]] && "
            "[[ ! -e \"$INTEGRITY_FAIL_ONCE_FILE\" ]]; then\n"
            "  touch \"$INTEGRITY_FAIL_ONCE_FILE\"\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        for executable in (
            self.directory / "deploy" / "run.sh",
            self.directory / "deploy" / "crown-run.sh",
            self.directory / ".venv" / "bin" / "python3",
        ):
            executable.chmod(0o755)
        self.footbreak_env = self.directory / "footbreak.env"
        self.crown_env = self.directory / "crown.env"
        self.footbreak_env.write_text("", encoding="utf-8")

    def run_reconciler(
        self,
        crown_enabled: str,
        *,
        extra_footbreak_env: str = "",
        integrity_fail_once: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.crown_env.write_text(
            f"CROWN_ENABLED={crown_enabled}\n", encoding="utf-8"
        )
        self.footbreak_env.write_text(extra_footbreak_env, encoding="utf-8")
        called = self.directory / "crown-called"
        environment = {
            **os.environ,
            "APP_DIR": str(self.directory),
            "FOOTBREAK_ENV_FILE": str(self.footbreak_env),
            "CROWN_ENV_FILE": str(self.crown_env),
            "CROWN_CALLED": str(called),
            "LEARNING_DB": str(self.directory / "absent.sqlite"),
        }
        if integrity_fail_once:
            environment["INTEGRITY_FAIL_ONCE_FILE"] = str(
                self.directory / "integrity-failed-once"
            )
        result = subprocess.run(
            ["bash", str(RECONCILE)],
            cwd=self.directory,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        result.crown_called = called.exists()  # type: ignore[attr-defined]
        return result

    def test_disabled_crown_is_a_successful_no_provider_reconciliation(self) -> None:
        result = self.run_reconciler("0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.crown_called)
        self.assertIn("Crown reconciliation skipped", result.stdout)

    def test_enabled_crown_failure_remains_visible(self) -> None:
        result = self.run_reconciler("1")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.crown_called)
        self.assertIn("Crown reconciliation failed rc=3", result.stderr)

    def test_environment_failed_variable_cannot_override_internal_exit_state(self) -> None:
        result = self.run_reconciler("0", extra_footbreak_env="failed=1\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.crown_called)

    def test_transient_integrity_read_race_is_retried_and_recovers(self) -> None:
        result = self.run_reconciler("0", integrity_fail_once=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("transient failure; retrying attempt=2/3", result.stderr)
        self.assertIn("Prediction-history integrity audit OK", result.stdout)

    def test_deploy_and_health_follow_the_same_validation_gate(self) -> None:
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        health = (ROOT / "deploy" / "health-check.sh").read_text(encoding="utf-8")
        reconcile = RECONCILE.read_text(encoding="utf-8")
        self.assertIn("crown_is_enabled_in_config", update)
        self.assertIn("systemctl disable --now crown-sweep.timer crown-tick.timer crown-settle.timer", update)
        self.assertIn("if crown_is_enabled; then", health)
        self.assertIn("Crown timers are not required", health)
        self.assertIn("if crown_is_enabled; then", reconcile)


if __name__ == "__main__":
    unittest.main()
