from __future__ import annotations

import copy
import contextlib
import hashlib
import io
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

from analysis.tests import test_wilson_validation as validation_tests
from analysis.wilson_validation import (
    _expected_production_identity_manifest,
    create_production_identity_manifest,
    project_granular_ranking_evidence,
    recompute_namespace,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "condition17-production-preflight.py"
WORKFLOW = (
    ROOT / ".github" / "workflows"
    / "footbreak-condition17-production-preflight.yml"
)
SPEC = importlib.util.spec_from_file_location(
    "condition17_production_preflight", SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CAPTURE = load_script(
    "condition17_secure_capture", "capture-condition17-production-snapshot.py",
)
HOST_KEY = load_script("condition17_host_key", "verify_ssh_host_key.py")


def production_shape() -> tuple[dict, dict, str]:
    case = validation_tests.WilsonBatchRolloverTest()
    ledger = {"bets": []}
    seeds = []
    for index in range(1, 17):
        seed = copy.deepcopy(validation_tests.candidate())
        seed["line_bucket"] = f"seed-{index}"
        seed["key"] = [
            f"bucket=seed-{index}" if value.startswith("bucket=") else value
            for value in seed["key"]
        ]
        seeds.append(seed)
    now = datetime.now(timezone.utc).astimezone() - timedelta(days=2)
    migration_at = (now - timedelta(hours=2)).isoformat()
    project_granular_ranking_evidence(
        ledger, "footbreak", [*seeds, validation_tests.candidate()],
        now=migration_at,
    )
    rows = [
        case._settled(
            ledger,
            index,
            result="Won" if index <= 10 else "Lost",
            stage_at=(now + timedelta(minutes=index)).isoformat(),
        )
        for index in range(1, 19)
    ]
    recompute_namespace(ledger, "footbreak")
    expected, _validated, reason = _expected_production_identity_manifest(
        ledger["wilson_validation"], "footbreak",
    )
    assert reason is None and expected is not None
    create_production_identity_manifest(
        ledger, "footbreak", authorized_manifest=expected,
    )
    for row in rows:
        row.pop("native_stage_at")
        row["settled_at"] = row["created_at"]
    frozen = next(
        item
        for item in ledger["wilson_validation"]["conditions"].values()
        if item.get("condition_number") == 17
    )
    trusted = {
        "expected_manifest_hash": expected["manifest_hash"],
        "expected_signature": frozen["signature"],
        "expected_initial_evidence_hash": frozen["evidence_versions"][0][
            "evidence_hash"
        ],
    }
    return ledger, trusted, "sensitive-fixture-id-must-not-appear"


class Condition17ProductionPreflightTests(unittest.TestCase):
    def _write(self, ledger: dict, directory: str) -> Path:
        path = Path(directory) / "sim-ledger.snapshot"
        path.write_bytes(json.dumps(ledger, ensure_ascii=False).encode())
        path.chmod(0o400)
        return path

    def test_go_is_read_only_and_deep_copy_rollover_reaches_v2(self) -> None:
        ledger, trusted, _secret = production_shape()
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(ledger, directory)
            before = path.read_bytes()
            result = PREFLIGHT.run_preflight(path, **trusted)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result["result"], "GO")
        self.assertEqual(result["compatible_anomaly_rows"], 18)
        self.assertEqual(result["pending_hits"], 10)
        self.assertEqual(result["synthetic_progress"], ["19/20", "0/20"])
        self.assertEqual(result["synthetic_rollover_version"], 2)
        self.assertFalse(result["production_mutation"])
        self.assertFalse(result["synthetic_data_output"])

    def test_manifest_authority_mismatch_fails_closed(self) -> None:
        ledger, trusted, _secret = production_shape()
        trusted["expected_manifest_hash"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(ledger, directory)
            with self.assertRaisesRegex(
                PREFLIGHT.PreflightFailure, "trusted_manifest_hash_mismatch",
            ):
                PREFLIGHT.run_preflight(path, **trusted)

    def test_extra_malformed_same_signature_row_fails_closed(self) -> None:
        ledger, trusted, secret = production_shape()
        extra = copy.deepcopy(ledger["bets"][0])
        extra["match_id"] = secret
        ledger["bets"].append(extra)
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(ledger, directory)
            before = path.read_bytes()
            with self.assertRaisesRegex(
                PREFLIGHT.PreflightFailure,
                "extra_or_missing_same_signature_rows",
            ):
                PREFLIGHT.run_preflight(path, **trusted)
            self.assertEqual(path.read_bytes(), before)

    def test_projection_mutation_is_detected(self) -> None:
        ledger, trusted, _secret = production_shape()
        original = PREFLIGHT.wv.project_frozen_ranking_evidence

        def mutating_projection(candidate_ledger, *args, **kwargs):
            result = original(candidate_ledger, *args, **kwargs)
            candidate_ledger["unexpected"] = True
            return result

        with tempfile.TemporaryDirectory() as directory:
            path = self._write(ledger, directory)
            with patch.object(
                PREFLIGHT.wv,
                "project_frozen_ranking_evidence",
                side_effect=mutating_projection,
            ):
                with self.assertRaisesRegex(
                    PREFLIGHT.PreflightFailure, "projection_mutated_ledger",
                ):
                    PREFLIGHT.run_preflight(path, **trusted)

    def test_snapshot_symlink_and_hardlink_are_rejected(self) -> None:
        ledger, trusted, _secret = production_shape()
        with tempfile.TemporaryDirectory() as directory:
            original = self._write(ledger, directory)
            symlink = Path(directory) / "symlink"
            symlink.symlink_to(original)
            hardlink = Path(directory) / "hardlink"
            os.link(original, hardlink)
            for path in (symlink, hardlink, original):
                with self.subTest(path=path.name):
                    with self.assertRaisesRegex(
                        PREFLIGHT.PreflightFailure,
                        "ledger_snapshot_file_invalid",
                    ):
                        PREFLIGHT.run_preflight(path, **trusted)

    def test_same_content_snapshot_path_replacement_is_rejected(self) -> None:
        ledger, trusted, _secret = production_shape()
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(ledger, directory)
            original = PREFLIGHT._verify_durable_progress

            def replace_path(*args, **kwargs):
                result = original(*args, **kwargs)
                replacement = Path(directory) / "replacement"
                replacement.write_bytes(path.read_bytes())
                replacement.chmod(0o400)
                os.replace(replacement, path)
                return result

            with patch.object(
                PREFLIGHT, "_verify_durable_progress", side_effect=replace_path,
            ):
                with self.assertRaisesRegex(
                    PREFLIGHT.PreflightFailure,
                    "ledger_snapshot_(file_invalid|identity_changed)",
                ):
                    PREFLIGHT.run_preflight(path, **trusted)

    def test_cli_failure_summary_does_not_expose_row_details(self) -> None:
        ledger, trusted, secret = production_shape()
        extra = copy.deepcopy(ledger["bets"][0])
        extra["match_id"] = secret
        ledger["bets"].append(extra)
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(ledger, directory)
            output = Path(directory) / "summary.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = PREFLIGHT.main([
                    "--ledger", str(path),
                    "--expected-manifest-hash", trusted["expected_manifest_hash"],
                    "--expected-condition-signature", trusted["expected_signature"],
                    "--expected-initial-evidence-hash",
                    trusted["expected_initial_evidence_hash"],
                    "--output", str(output),
                ])
            payload = output.read_text(encoding="utf-8")
        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), payload)
        self.assertIn('"result": "NO-GO"', payload)
        self.assertNotIn(secret, payload)
        self.assertNotIn("bets", payload)

    def test_output_refuses_existing_symlink_and_hardlink_paths(self) -> None:
        ledger, trusted, _secret = production_shape()
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = self._write(ledger, directory)
            victim = Path(directory) / "victim"
            victim.write_text("unchanged", encoding="utf-8")
            paths = [
                Path(directory) / "existing",
                Path(directory) / "symlink",
                Path(directory) / "hardlink",
            ]
            paths[0].write_text("existing", encoding="utf-8")
            paths[1].symlink_to(victim)
            os.link(victim, paths[2])
            for output in paths:
                with self.subTest(path=output.name):
                    before = victim.read_bytes()
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        rc = PREFLIGHT.main([
                            "--ledger", str(ledger_path),
                            "--expected-manifest-hash",
                            trusted["expected_manifest_hash"],
                            "--expected-condition-signature",
                            trusted["expected_signature"],
                            "--expected-initial-evidence-hash",
                            trusted["expected_initial_evidence_hash"],
                            "--output", str(output),
                        ])
                    self.assertEqual(rc, 1)
                    self.assertIn('"result": "NO-GO"', stdout.getvalue())
                    self.assertIn("output_protection_failure", stdout.getvalue())
                    self.assertEqual(victim.read_bytes(), before)
            self.assertEqual(paths[0].read_text(encoding="utf-8"), "existing")

    def test_output_is_exclusive_regular_single_link_mode_0400(self) -> None:
        ledger, trusted, _secret = production_shape()
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = self._write(ledger, directory)
            output = Path(directory) / "summary"
            with contextlib.redirect_stdout(io.StringIO()):
                rc = PREFLIGHT.main([
                    "--ledger", str(ledger_path),
                    "--expected-manifest-hash", trusted["expected_manifest_hash"],
                    "--expected-condition-signature", trusted["expected_signature"],
                    "--expected-initial-evidence-hash",
                    trusted["expected_initial_evidence_hash"],
                    "--output", str(output),
                ])
            info = os.lstat(output)
            self.assertEqual(rc, 0)
            self.assertTrue(output.is_file())
            self.assertFalse(output.is_symlink())
            self.assertEqual(info.st_nlink, 1)
            self.assertEqual(info.st_mode & 0o777, 0o400)

    def test_output_path_and_parent_replacement_attacks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "summary"
            real_fsync = PREFLIGHT.os.fsync

            def replace_after_write(fd):
                real_fsync(fd)
                replacement = root / "replacement"
                replacement.write_bytes(b"same-size-malicious-replacement")
                replacement.chmod(0o400)
                os.replace(replacement, output)

            with patch.object(PREFLIGHT.os, "fsync", side_effect=replace_after_write):
                with self.assertRaises(PREFLIGHT.PreflightFailure):
                    PREFLIGHT._write_output_exclusive(output, b"trusted-summary")
            self.assertFalse(output.exists())

            real_parent = root / "real-parent"
            real_parent.mkdir(mode=0o700)
            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaisesRegex(
                PREFLIGHT.PreflightFailure, "output_parent_invalid",
            ):
                PREFLIGHT._write_output_exclusive(
                    linked_parent / "summary", b"trusted-summary",
                )


class Condition17SecureCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lock = self.root / "footbreak.lock"
        self.ledger = self.root / "sim_ledger.json"
        self.lock.write_bytes(b"")
        self.ledger.write_bytes(b'{"safe":true}\n')
        self.commit = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True,
        ).strip()
        self.validation_hash = hashlib.sha256(
            (ROOT / "analysis" / "wilson_validation.py").read_bytes(),
        ).hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _capture(self, **kwargs):
        return CAPTURE.capture(
            self.lock,
            self.ledger,
            ROOT,
            self.commit,
            self.validation_hash,
            lock_timeout=0.1,
            **kwargs,
        )

    def test_capture_verifies_deployed_commit_and_validation_hash(self) -> None:
        self.assertEqual(self._capture(), self.ledger.read_bytes())
        with self.assertRaisesRegex(
            CAPTURE.CaptureFailure, "deployed_commit_mismatch",
        ):
            CAPTURE.capture(
                self.lock, self.ledger, ROOT, "0" * 40, self.validation_hash,
                lock_timeout=0.1,
            )
        with self.assertRaisesRegex(
            CAPTURE.CaptureFailure, "deployed_validation_hash_mismatch",
        ):
            CAPTURE.capture(
                self.lock, self.ledger, ROOT, self.commit, "0" * 64,
                lock_timeout=0.1,
            )

    def test_lock_symlink_and_hardlink_are_rejected(self) -> None:
        symlink = self.root / "lock-symlink"
        symlink.symlink_to(self.lock)
        hardlink = self.root / "lock-hardlink"
        os.link(self.lock, hardlink)
        for path in (symlink, hardlink, self.lock):
            with self.subTest(path=path.name):
                with self.assertRaises(CAPTURE.CaptureFailure):
                    CAPTURE.capture(
                        path, self.ledger, ROOT, self.commit,
                        self.validation_hash, lock_timeout=0.1,
                    )

    def test_lock_inode_replacement_after_acquisition_is_rejected(self) -> None:
        def attack():
            replacement = self.root / "new-lock"
            replacement.write_bytes(b"")
            os.replace(replacement, self.lock)

        with self.assertRaisesRegex(
            CAPTURE.CaptureFailure, "lock_path_invalid",
        ):
            self._capture(after_lock_hook=attack)

    def test_ledger_symlink_hardlink_and_replacement_are_rejected(self) -> None:
        symlink = self.root / "ledger-symlink"
        symlink.symlink_to(self.ledger)
        hardlink = self.root / "ledger-hardlink"
        os.link(self.ledger, hardlink)
        for path in (symlink, hardlink, self.ledger):
            with self.subTest(path=path.name):
                with self.assertRaises(CAPTURE.CaptureFailure):
                    CAPTURE.capture(
                        self.lock, path, ROOT, self.commit,
                        self.validation_hash, lock_timeout=0.1,
                    )
        hardlink.unlink()

        def attack():
            replacement = self.root / "new-ledger"
            replacement.write_bytes(self.ledger.read_bytes())
            os.replace(replacement, self.ledger)

        with self.assertRaisesRegex(
            CAPTURE.CaptureFailure, "ledger_path_invalid",
        ):
            self._capture(after_read_hook=attack)


class Condition17PinnedHostKeyTests(unittest.TestCase):
    def test_exact_host_port_fingerprint_and_single_entry_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "host"
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                check=True,
            )
            public = key.with_suffix(".pub").read_text().split()[1]
            fingerprint = subprocess.check_output(
                ["ssh-keygen", "-lf", str(key.with_suffix(".pub")), "-E", "sha256"],
                text=True,
            ).split()[1]
            known = root / "known_hosts"
            known.write_text(
                f"[production.example]:2222 ssh-ed25519 {public}\n",
                encoding="utf-8",
            )
            known.chmod(0o600)
            HOST_KEY.verify(known, "production.example", 2222, fingerprint)
            for host, port, expected in (
                ("other.example", 2222, fingerprint),
                ("production.example", 22, fingerprint),
                ("production.example", 2222, "SHA256:" + "A" * 43),
            ):
                with self.subTest(host=host, port=port):
                    with self.assertRaises(ValueError):
                        HOST_KEY.verify(known, host, port, expected)
            known.write_text(
                known.read_text(encoding="utf-8")
                + f"other.example ssh-ed25519 {public}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "exactly_one"):
                HOST_KEY.verify(
                    known, "production.example", 2222, fingerprint,
                )

    def test_known_hosts_symlink_and_hardlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            known = root / "known_hosts"
            known.write_text(
                "production.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n",
                encoding="utf-8",
            )
            known.chmod(0o600)
            symlink = root / "known-symlink"
            symlink.symlink_to(known)
            hardlink = root / "known-hardlink"
            os.link(known, hardlink)
            for path in (symlink, hardlink, known):
                with self.subTest(path=path.name):
                    with self.assertRaisesRegex(ValueError, "single_link"):
                        HOST_KEY.verify(
                            path, "production.example", 22, "SHA256:" + "A" * 43,
                        )


class Condition17ProductionPreflightWorkflowTests(unittest.TestCase):
    def test_workflow_is_manual_read_only_and_requires_trusted_hashes(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        parsed = yaml.safe_load(text)
        trigger = parsed.get(True, {})
        dispatch = trigger.get("workflow_dispatch", {})
        inputs = dispatch.get("inputs", {})

        self.assertNotIn("push", trigger)
        self.assertNotIn("schedule", trigger)
        self.assertEqual(parsed.get("permissions"), {"contents": "read"})
        self.assertEqual(
            set(inputs),
            {
                "expected_deployed_sha",
                "expected_wilson_validation_sha256",
                "expected_manifest_hash",
                "expected_condition_signature",
                "expected_initial_evidence_hash",
                "ssh_port",
            },
        )
        self.assertTrue(all(value.get("required") is True for value in inputs.values()))
        self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', text)
        self.assertIn('test "$GITHUB_SHA" = "$EXPECTED_DEPLOYED_SHA"', text)
        self.assertIn(
            "test \"$(sha256sum analysis/wilson_validation.py | awk '{print $1}')\"",
            text,
        )
        self.assertNotIn("git push", text)

    def test_workflow_uses_bounded_shared_lock_and_never_uploads_ledger(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("/var/lock/footbreak.lock", text)
        self.assertIn("/opt/footbreak/system/sim_ledger.json", text)
        self.assertIn("capture-condition17-production-snapshot.py", text)
        self.assertIn("--expected-commit '$EXPECTED_DEPLOYED_SHA'", text)
        self.assertIn(
            "--expected-validation-sha256 '$EXPECTED_VALIDATION_SHA'", text,
        )
        self.assertIn("--lock-timeout 120", text)
        self.assertIn('chmod 400 "$snapshot"', text)
        self.assertIn("timeout --signal=TERM --kill-after=10s 180s", text)
        self.assertEqual(
            parsed_workflow()["jobs"]["preflight"]["timeout-minutes"], 10,
        )
        upload = text.split("Upload bounded preflight result", 1)[1]
        self.assertNotIn("sim-ledger.snapshot", upload)
        self.assertIn("condition17-production-preflight-summary.json", upload)

    def test_workflow_uses_pinned_host_key_and_cleans_all_temporary_files(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("secrets.DEPLOY_SSH_KEY", text)
        self.assertIn("secrets.DEPLOY_HOST", text)
        self.assertIn("secrets.DEPLOY_USER", text)
        self.assertIn("secrets.DEPLOY_SSH_HOST_KEY", text)
        self.assertIn("secrets.DEPLOY_SSH_HOST_FINGERPRINT", text)
        self.assertNotIn("ssh-keyscan", text)
        self.assertIn("-o StrictHostKeyChecking=yes", text)
        self.assertIn('-o UserKnownHostsFile="$PREFLIGHT_DIR/known_hosts"', text)
        self.assertIn("-o GlobalKnownHostsFile=/dev/null", text)
        self.assertIn("trap cleanup_on_error EXIT", text)
        self.assertIn("trap cleanup_snapshot EXIT", text)
        self.assertIn("trap cleanup EXIT", text)
        self.assertGreaterEqual(text.count("trap 'exit 130' HUP INT TERM"), 3)
        self.assertIn('rm -rf -- "$PREFLIGHT_DIR"', text)
        self.assertNotIn("cat ~/.ssh/id_ed25519", text)


def parsed_workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
