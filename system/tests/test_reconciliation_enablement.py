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
        (self.directory / "bin").mkdir()
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
            "[[ \"${PYTHONPATH:-}\" != \"${APP_DIR:-}\" ]]; then\n"
            "  echo 'integrity verifier missing APP_DIR on PYTHONPATH' >&2\n"
            "  exit 42\n"
            "fi\n"
            "if [[ \"${1:-}\" == */verify-result-integrity.py ]] && "
            "[[ \"${INTEGRITY_ALWAYS_FAIL:-0}\" == 1 ]]; then\n"
            "  exit 1\n"
            "fi\n"
            "if [[ \"${1:-}\" == */verify-result-integrity.py ]] && "
            "[[ -n \"${INTEGRITY_FAIL_ONCE_FILE:-}\" ]] && "
            "[[ ! -e \"$INTEGRITY_FAIL_ONCE_FILE\" ]]; then\n"
            "  touch \"$INTEGRITY_FAIL_ONCE_FILE\"\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (self.directory / "bin" / "sleep").write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$1\" >> \"$SLEEP_CAPTURE\"\n",
            encoding="utf-8",
        )
        for executable in (
            self.directory / "deploy" / "run.sh",
            self.directory / "deploy" / "crown-run.sh",
            self.directory / ".venv" / "bin" / "python3",
            self.directory / "bin" / "sleep",
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
        integrity_always_fail: bool = False,
        integrity_attempts: str = "3",
        integrity_retry_delay: str = "0",
    ) -> subprocess.CompletedProcess[str]:
        self.crown_env.write_text(
            f"CROWN_ENABLED={crown_enabled}\n", encoding="utf-8"
        )
        self.footbreak_env.write_text(extra_footbreak_env, encoding="utf-8")
        called = self.directory / "crown-called"
        sleep_capture = self.directory / "sleep-capture"
        sleep_capture.unlink(missing_ok=True)
        environment = {
            **os.environ,
            "PATH": f"{self.directory / 'bin'}:{os.environ['PATH']}",
            "APP_DIR": str(self.directory),
            "FOOTBREAK_ENV_FILE": str(self.footbreak_env),
            "CROWN_ENV_FILE": str(self.crown_env),
            "CROWN_CALLED": str(called),
            "LEARNING_DB": str(self.directory / "absent.sqlite"),
            "INTEGRITY_AUDIT_ATTEMPTS": integrity_attempts,
            "INTEGRITY_AUDIT_RETRY_DELAY_SECONDS": integrity_retry_delay,
            "SLEEP_CAPTURE": str(sleep_capture),
        }
        if integrity_fail_once:
            environment["INTEGRITY_FAIL_ONCE_FILE"] = str(
                self.directory / "integrity-failed-once"
            )
        if integrity_always_fail:
            environment["INTEGRITY_ALWAYS_FAIL"] = "1"
        result = subprocess.run(
            ["bash", str(RECONCILE)],
            cwd=self.directory,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        result.crown_called = called.exists()  # type: ignore[attr-defined]
        result.sleep_calls = (  # type: ignore[attr-defined]
            sleep_capture.read_text(encoding="utf-8").splitlines()
            if sleep_capture.exists()
            else []
        )
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

    def test_persistent_integrity_failure_remains_fail_closed(self) -> None:
        result = self.run_reconciler("0", integrity_always_fail=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("retrying attempt=3/3", result.stderr)
        self.assertIn("Prediction-history integrity audit failed rc=1", result.stderr)

    def test_integrity_attempt_override_accepts_only_bounded_canonical_decimal(self) -> None:
        for raw, expected_attempts in (
            ("1", 1),
            ("24", 24),
            ("0", 3),
            ("25", 3),
            ("abc", 3),
            ("08", 3),
            ("09", 3),
            ("010", 3),
            ("9223372036854775808", 3),
            ("18446744073709551616", 3),
        ):
            with self.subTest(raw=raw):
                result = self.run_reconciler(
                    "0",
                    integrity_always_fail=True,
                    integrity_attempts=raw,
                )
                self.assertEqual(result.returncode, 1)
                if expected_attempts == 1:
                    self.assertNotIn("transient failure; retrying", result.stderr)
                else:
                    self.assertIn(
                        f"retrying attempt={expected_attempts}/{expected_attempts}",
                        result.stderr,
                    )
                self.assertIn(
                    "Prediction-history integrity audit failed rc=1",
                    result.stderr,
                )

    def test_integrity_retry_delay_override_is_lexically_bounded(self) -> None:
        for raw, expected_delay in (
            ("0", "0"),
            ("15", "15"),
            ("16", "2"),
            ("010", "2"),
            ("9223372036854775808", "2"),
            ("18446744073709551616", "2"),
        ):
            with self.subTest(raw=raw):
                result = self.run_reconciler(
                    "0",
                    integrity_always_fail=True,
                    integrity_attempts="2",
                    integrity_retry_delay=raw,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.sleep_calls, [expected_delay])

    def test_integrity_verifier_is_independent_of_service_working_directory(self) -> None:
        result = self.run_reconciler("0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("missing APP_DIR on PYTHONPATH", result.stderr)

    def test_deploy_and_health_follow_the_same_validation_gate(self) -> None:
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        health = (ROOT / "deploy" / "health-check.sh").read_text(encoding="utf-8")
        bridge_health = (ROOT / "crown" / "reverse_t5_bridge_health.py").read_text(
            encoding="utf-8",
        )
        reconcile = RECONCILE.read_text(encoding="utf-8")
        self.assertIn("crown_is_enabled_in_config", update)
        self.assertIn("systemctl reenable \"$timer\"", update)
        self.assertIn("was not enabled after reenable", update)
        self.assertIn(
            "systemctl disable --now crown-round-update.timer crown-first-look-reconcile.timer "
            "crown-early-admission-reconcile.timer crown-sweep.timer "
            "crown-tick.timer crown-settle.timer crown-reverse-t5-drain.timer",
            update,
        )
        enable = (ROOT / "deploy" / "enable-crown.sh").read_text(encoding="utf-8")
        setup = (ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("crown-reverse-t5-drain.timer", update)
        self.assertIn("crown-reverse-t5-drain.timer", enable)
        self.assertIn("crown-reverse-t5-drain.timer", setup)
        self.assertIn("if crown_is_enabled; then", health)
        self.assertIn("crown-reverse-t5-drain.timer", health)
        self.assertIn("crown-reverse-t5-drain.service", health)
        self.assertIn("reverse_t5_bridge_is_enabled", health)
        self.assertIn("consecutive worker timeouts", bridge_health)
        self.assertIn("reverse_t5_bridge_health check", health)
        self.assertIn("recent parseable successful completion", health)
        self.assertIn("worker liveness is not required", health)
        self.assertIn("Crown timers are not required", health)
        self.assertIn("if crown_is_enabled; then", reconcile)

    def test_reverse_bridge_flag_and_rollout_workflow_document_the_dedicated_worker(self) -> None:
        example = (ROOT / "deploy" / "footbreak-crown.env.example").read_text(encoding="utf-8")
        setup = (ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/reverse-t5-bridge-rollout.yml").read_text(encoding="utf-8")
        self.assertIn("CROWN_REVERSE_T5_BRIDGE_ENABLED=0", example)
        self.assertIn("CROWN_REVERSE_T5_BRIDGE_ENABLED=1", setup)
        self.assertIn("crown-reverse-t5-drain.timer", workflow)
        self.assertIn("crown-reverse-t5-drain.service", workflow)
        self.assertIn("reverse_t5_bridge_health mark-enabled", workflow)
        self.assertIn("reverse_t5_bridge_health mark-disabled", workflow)
        self.assertIn("reverse_t5_bridge_health check --require-completion", workflow)
        self.assertIn("systemctl enable --now crown-reverse-t5-drain.timer", workflow)
        self.assertIn("systemctl disable --now crown-reverse-t5-drain.timer", workflow)
        self.assertIn('"reverse_t5_safe_evidence": True', workflow)
        self.assertIn('"post_enable_states"', workflow)
        self.assertIn('"worker_last_completed"', workflow)
        self.assertIn("deferred_native_priority", workflow)
        self.assertIn("reverse_t5_bridge_worker_triggered=completed", workflow)
        self.assertIn("sync_reverse_t5_bridge_enablement_marker", (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8"))
        self.assertIn("systemctl unmask crown-reverse-t5-drain.timer", (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8"))
        self.assertIn("reverse_t5_bridge_health mark-enabled", (ROOT / "deploy" / "enable-crown.sh").read_text(encoding="utf-8"))

    def test_enable_crown_parses_quoted_and_whitespace_padded_bridge_flag_values(self) -> None:
        script = ROOT / "deploy" / "enable-crown.sh"
        cases = (
            ('  CROWN_REVERSE_T5_BRIDGE_ENABLED = "1"  \n', "mark-enabled"),
            ("export CROWN_REVERSE_T5_BRIDGE_ENABLED = 'true'\n", "mark-enabled"),
            ('CROWN_REVERSE_T5_BRIDGE_ENABLED = "on"\n', "mark-enabled"),
            ('CROWN_REVERSE_T5_BRIDGE_ENABLED = "0"\n', "mark-disabled"),
            ("CROWN_REVERSE_T5_BRIDGE_ENABLED = 'false'\n", "mark-disabled"),
        )
        for flag_line, expected in cases:
            with self.subTest(flag_line=flag_line):
                with tempfile.TemporaryDirectory() as directory:
                    sandbox = Path(directory)
                    bin_dir = sandbox / "bin"
                    bin_dir.mkdir()
                    capture = sandbox / "python-calls"
                    crown_env = sandbox / "footbreak-crown.env"
                    crown_env.write_text(flag_line, encoding="utf-8")
                    for name, body in {
                        "systemctl": "#!/usr/bin/env bash\nexit 0\n",
                        "chown": "#!/usr/bin/env bash\nexit 0\n",
                        "python3": (
                            "#!/usr/bin/env bash\n"
                            "printf '%s\\n' \"$*\" >> \"$ENABLE_CROWN_PYTHON_CALLS\"\n"
                        ),
                    }.items():
                        path = bin_dir / name
                        path.write_text(body, encoding="utf-8")
                        path.chmod(0o755)
                    result = subprocess.run(
                        ["bash", str(script)],
                        env={
                            **os.environ,
                            "PATH": f"{bin_dir}:{os.environ['PATH']}",
                            "CROWN_ENV_FILE": str(crown_env),
                            "CROWN_STATE_DIR": str(sandbox / "state"),
                            "ENABLE_CROWN_PYTHON_CALLS": str(capture),
                        },
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = capture.read_text(encoding="utf-8")
                    self.assertIn(
                        f"crown.reverse_t5_bridge_health {expected}",
                        calls,
                    )


if __name__ == "__main__":
    unittest.main()
