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

from analysis import wilson_validation as wv
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
BOOTSTRAP = load_script(
    "condition17_bootstrap_audit", "condition17-bootstrap-audit.py",
)


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
    def setUp(self) -> None:
        self.marker_temp = tempfile.TemporaryDirectory()
        self.activation_marker = Path(self.marker_temp.name) / "activation.json"
        self.activation_marker.write_text(json.dumps({
            "schema": wv.CONDITION17_ACTIVATION_SCHEMA,
            "wilson_validation_sha256": hashlib.sha256(
                Path(wv.__file__).read_bytes(),
            ).hexdigest(),
            "quarter_line_sha256": hashlib.sha256(
                Path(wv.__file__).with_name("quarter_line.py").read_bytes(),
            ).hexdigest(),
        }), encoding="utf-8")
        self.activation_marker.chmod(0o400)
        self.marker_patch = patch.object(
            wv, "CONDITION17_ACTIVATION_MARKER", self.activation_marker,
        )
        self.marker_patch.start()

    def tearDown(self) -> None:
        self.marker_patch.stop()
        self.marker_temp.cleanup()

    def _cli_marker(self) -> list[str]:
        return ["--activation-marker", str(self.activation_marker)]

    def test_activation_is_default_off_and_marker_is_source_bound(self) -> None:
        missing = Path(self.marker_temp.name) / "missing"
        with patch.object(wv, "CONDITION17_ACTIVATION_MARKER", missing):
            self.assertFalse(wv._footbreak_17_legacy_cohort_is_activated())
        victim = Path(self.marker_temp.name) / "victim"
        victim.write_bytes(self.activation_marker.read_bytes())
        victim.chmod(0o400)
        symlink = Path(self.marker_temp.name) / "marker-symlink"
        symlink.symlink_to(victim)
        hardlink = Path(self.marker_temp.name) / "marker-hardlink"
        os.link(victim, hardlink)
        for marker in (symlink, hardlink, victim):
            with self.subTest(marker=marker.name):
                with patch.object(wv, "CONDITION17_ACTIVATION_MARKER", marker):
                    self.assertFalse(wv._footbreak_17_legacy_cohort_is_activated())
        tampered = json.loads(self.activation_marker.read_text(encoding="utf-8"))
        tampered["quarter_line_sha256"] = "0" * 64
        bad = Path(self.marker_temp.name) / "bad-marker"
        bad.write_text(json.dumps(tampered), encoding="utf-8")
        bad.chmod(0o400)
        with patch.object(wv, "CONDITION17_ACTIVATION_MARKER", bad):
            self.assertFalse(wv._footbreak_17_legacy_cohort_is_activated())

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
                    *self._cli_marker(),
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
                            *self._cli_marker(),
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
                    *self._cli_marker(),
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
        self.repo = self.root / "repo"
        subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(ROOT), str(self.repo)],
            check=True,
        )
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True,
        ).strip()
        self.tree = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD^{tree}"], text=True,
        ).strip()
        self.validation_hash = hashlib.sha256(
            (self.repo / "analysis" / "wilson_validation.py").read_bytes(),
        ).hexdigest()
        self.activation_marker = self.root / "production-activation.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _capture(self, **kwargs):
        return CAPTURE.capture(
            self.lock,
            self.ledger,
            self.repo,
            self.commit,
            self.tree,
            self.validation_hash,
            self.activation_marker,
            lock_timeout=0.1,
            **kwargs,
        )

    def test_capture_verifies_deployed_commit_and_validation_hash(self) -> None:
        self.assertEqual(self._capture(), self.ledger.read_bytes())
        with self.assertRaisesRegex(
            CAPTURE.CaptureFailure, "deployed_commit_mismatch",
        ):
            CAPTURE.capture(
                self.lock, self.ledger, self.repo, "0" * 40, self.tree,
                self.validation_hash, self.activation_marker,
                lock_timeout=0.1,
            )
        with self.assertRaisesRegex(
            CAPTURE.CaptureFailure, "deployed_validation_hash_mismatch",
        ):
            CAPTURE.capture(
                self.lock, self.ledger, self.repo, self.commit, self.tree,
                "0" * 64, self.activation_marker,
                lock_timeout=0.1,
            )
        with self.assertRaisesRegex(
            CAPTURE.CaptureFailure, "deployed_tree_mismatch",
        ):
            CAPTURE.capture(
                self.lock, self.ledger, self.repo, self.commit, "0" * 40,
                self.validation_hash, self.activation_marker,
                lock_timeout=0.1,
            )

    def test_tree_dirty_staged_and_untracked_source_fail_closed(self) -> None:
        dependency = self.repo / "analysis" / "quarter_line.py"
        dependency.write_text(
            dependency.read_text(encoding="utf-8") + "\n# dirty\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            CAPTURE.CaptureFailure,
            "deployed_(worktree_dirty|tracked_content_mismatch)",
        ):
            self._capture()

        subprocess.run(
            ["git", "-C", str(self.repo), "reset", "--hard", "--quiet", "HEAD"],
            check=True,
        )
        readme = self.repo / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\nstaged attack\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "README.md"], check=True,
        )
        with self.assertRaisesRegex(CAPTURE.CaptureFailure, "deployed_index_dirty"):
            self._capture()

        subprocess.run(
            ["git", "-C", str(self.repo), "reset", "--hard", "--quiet", "HEAD"],
            check=True,
        )
        (self.repo / "analysis" / "quarter_line.so").write_bytes(
            b"untracked-extension-module-shadow",
        )
        with self.assertRaisesRegex(
            CAPTURE.CaptureFailure, "deployed_untracked_source_shadow",
        ):
            self._capture()

    def test_dependency_symlink_and_same_content_replacement_fail_closed(self) -> None:
        dependency = self.repo / "analysis" / "quarter_line.py"
        outside = self.root / "quarter_line.py"
        outside.write_bytes(dependency.read_bytes())
        dependency.unlink()
        dependency.symlink_to(outside)
        with self.assertRaises(CAPTURE.CaptureFailure):
            self._capture()

        dependency.unlink()
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "--quiet", "--",
             "analysis/quarter_line.py"],
            check=True,
        )

        def replace_dependency() -> None:
            replacement = self.root / "same-content-quarter-line.py"
            replacement.write_bytes(dependency.read_bytes())
            replacement.chmod(dependency.stat().st_mode & 0o777)
            os.replace(replacement, dependency)

        with self.assertRaisesRegex(
            CAPTURE.CaptureFailure,
            "deployed_(worktree_dirty|tracked_identity_changed)",
        ):
            self._capture(after_read_hook=replace_dependency)

    def test_preactivation_and_sanitized_git_environment_are_required(self) -> None:
        self.activation_marker.write_text("already active", encoding="utf-8")
        with self.assertRaisesRegex(
            CAPTURE.CaptureFailure, "condition17_already_activated",
        ):
            self._capture()
        self.activation_marker.unlink()
        with patch.dict(
            os.environ,
            {
                "GIT_DIR": str(self.root / "attacker-git-dir"),
                "GIT_WORK_TREE": str(self.root / "attacker-worktree"),
                "GIT_CONFIG_GLOBAL": str(self.root / "attacker-config"),
            },
        ):
            self.assertEqual(self._capture(), self.ledger.read_bytes())

    def test_lock_symlink_and_hardlink_are_rejected(self) -> None:
        symlink = self.root / "lock-symlink"
        symlink.symlink_to(self.lock)
        hardlink = self.root / "lock-hardlink"
        os.link(self.lock, hardlink)
        for path in (symlink, hardlink, self.lock):
            with self.subTest(path=path.name):
                with self.assertRaises(CAPTURE.CaptureFailure):
                    CAPTURE.capture(
                        path, self.ledger, self.repo, self.commit, self.tree,
                        self.validation_hash, self.activation_marker,
                        lock_timeout=0.1,
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
                        self.lock, path, self.repo, self.commit, self.tree,
                        self.validation_hash, self.activation_marker,
                        lock_timeout=0.1,
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


class Condition17BootstrapAuditTests(unittest.TestCase):
    def test_bootstrap_requires_canonical_dispatched_commit_and_tree_ids(self) -> None:
        with self.assertRaisesRegex(
            BOOTSTRAP.preflight.PreflightFailure,
            "dispatched_deployed_commit_invalid",
        ):
            BOOTSTRAP.build_review(
                Path("/not-opened"),
                deployed_commit="main",
                deployed_tree="2" * 40,
                validation_sha256=BOOTSTRAP.PINNED_VALIDATION_SHA256,
                quarter_line_sha256=BOOTSTRAP.PINNED_QUARTER_LINE_SHA256,
            )
        with self.assertRaisesRegex(
            BOOTSTRAP.preflight.PreflightFailure,
            "dispatched_deployed_tree_invalid",
        ):
            BOOTSTRAP.build_review(
                Path("/not-opened"),
                deployed_commit="1" * 40,
                deployed_tree="A" * 40,
                validation_sha256=BOOTSTRAP.PINNED_VALIDATION_SHA256,
                quarter_line_sha256=BOOTSTRAP.PINNED_QUARTER_LINE_SHA256,
            )

    def test_bootstrap_artifact_has_full_manifest_and_no_rows(self) -> None:
        ledger, trusted, secret = production_shape()
        ledger["wilson_validation"].pop("production_identity_manifest")
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot"
            snapshot.write_text(json.dumps(ledger), encoding="utf-8")
            snapshot.chmod(0o400)
            marker = Path(directory) / "marker"
            marker.write_text(json.dumps({
                "schema": wv.CONDITION17_ACTIVATION_SCHEMA,
                "wilson_validation_sha256": hashlib.sha256(
                    Path(wv.__file__).read_bytes(),
                ).hexdigest(),
                "quarter_line_sha256": hashlib.sha256(
                    Path(wv.__file__).with_name("quarter_line.py").read_bytes(),
                ).hexdigest(),
            }), encoding="utf-8")
            marker.chmod(0o400)
            with patch.object(
                BOOTSTRAP, "PINNED_CONDITION17_SIGNATURE",
                trusted["expected_signature"],
            ), patch.object(
                BOOTSTRAP, "PINNED_CONDITION17_INITIAL_EVIDENCE_HASH",
                trusted["expected_initial_evidence_hash"],
            ), patch.object(
                BOOTSTRAP.wv, "CONDITION17_ACTIVATION_MARKER", marker,
            ):
                result = BOOTSTRAP.build_review(
                    snapshot,
                    deployed_commit="1" * 40,
                    deployed_tree="2" * 40,
                    validation_sha256=BOOTSTRAP.PINNED_VALIDATION_SHA256,
                    quarter_line_sha256=BOOTSTRAP.PINNED_QUARTER_LINE_SHA256,
                )
        encoded = json.dumps(result)
        self.assertEqual(result["result"], "GO")
        self.assertEqual(
            len(result["candidate_production_identity_manifest"]["entries"]), 17,
        )
        self.assertEqual(result["condition17"]["durable_progress"], "18/20")
        self.assertEqual(
            set(result["condition17"]["exclusions"]),
            PREFLIGHT.EXCLUSION_KEYS,
        )
        self.assertTrue(
            all(value == 0 for value in result["condition17"]["exclusions"].values()),
        )
        self.assertNotIn(secret, encoded)
        self.assertNotIn('"bets"', encoded)
        self.assertFalse(result["contains_fixture_ids_or_raw_rows"])


class Condition17ProductionPreflightWorkflowTests(unittest.TestCase):
    def test_workflow_is_manual_read_only_and_binds_dispatched_integration(self) -> None:
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
            {"expected_deployed_sha", "expected_deployed_tree", "ssh_port"},
        )
        self.assertTrue(all(value.get("required") is True for value in inputs.values()))
        self.assertNotIn("e5ca5f6e745bbb7671841b860aa81dbd0039210a", text)
        self.assertNotIn("50a9e26a20d90a93f481b9ea4ccd002f162232ad", text)
        self.assertIn(BOOTSTRAP.PINNED_VALIDATION_SHA256, text)
        self.assertIn(BOOTSTRAP.PINNED_QUARTER_LINE_SHA256, text)
        self.assertIn(
            "test \"$(sha256sum analysis/wilson_validation.py | awk '{print $1}')\"",
            text,
        )
        self.assertIn(
            "TRUSTED_DISPATCH_REF: refs/heads/integrate/"
            "condition17-stage-a-main-20260827",
            text,
        )
        self.assertIn("TRUSTED_REPOSITORY: patrick33483-creator/footbreak", text)
        self.assertIn(
            'test "$GITHUB_REPOSITORY" = "$TRUSTED_REPOSITORY"', text,
        )
        self.assertIn('test "$GITHUB_REF" = "$TRUSTED_DISPATCH_REF"', text)
        self.assertIn('test "$EXPECTED_DEPLOYED_SHA" = "$GITHUB_SHA"', text)
        self.assertIn(
            'test "$(git rev-parse --verify "$GITHUB_SHA^{tree}")" '
            '= "$EXPECTED_DEPLOYED_TREE"',
            text,
        )
        self.assertNotIn("git push", text)

    def test_checkout_and_reviewed_blobs_are_bound_to_exact_dispatch_sha(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        checkout = parsed_workflow()["jobs"]["audit"]["steps"][0]

        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(checkout["with"]["fetch-depth"], 1)
        self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertIn('test "$(git rev-parse --verify HEAD^{commit})" = "$GITHUB_SHA"', text)
        self.assertIn('entry="$(git ls-tree "$GITHUB_SHA" -- "$path")"', text)
        self.assertIn('test "$(git hash-object -- "$path")" = "$oid"', text)
        for path in (
            ".github/workflows/footbreak-condition17-production-preflight.yml",
            "deploy/verify_ssh_host_key.py",
            "deploy/capture-condition17-production-snapshot.py",
            "deploy/condition17-production-preflight.py",
            "deploy/condition17-bootstrap-audit.py",
        ):
            self.assertIn(path, text)

    def test_arbitrary_ref_or_mismatched_dispatch_authority_cannot_pass(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('test "$GITHUB_EVENT_NAME" = "workflow_dispatch"', text)
        self.assertIn(
            'test "$GITHUB_REPOSITORY" = "$TRUSTED_REPOSITORY"', text,
        )
        self.assertIn('test "$GITHUB_REF" = "$TRUSTED_DISPATCH_REF"', text)
        self.assertIn('[[ "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]]', text)
        self.assertIn('[[ "$EXPECTED_DEPLOYED_SHA" =~ ^[0-9a-f]{40}$ ]]', text)
        self.assertIn('[[ "$EXPECTED_DEPLOYED_TREE" =~ ^[0-9a-f]{40}$ ]]', text)
        self.assertNotIn("${{ github.event.inputs.ref", text)
        self.assertNotIn("refs/heads/${{", text)

    def test_workflow_uses_bounded_shared_lock_and_never_uploads_ledger(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("/var/lock/footbreak.lock", text)
        self.assertIn("/opt/footbreak/system/sim_ledger.json", text)
        self.assertIn("capture-condition17-production-snapshot.py", text)
        self.assertIn("--expected-commit '$EXPECTED_DEPLOYED_SHA'", text)
        self.assertIn("--expected-tree '$EXPECTED_DEPLOYED_TREE'", text)
        self.assertIn(
            "--expected-validation-sha256 '$PINNED_VALIDATION_SHA'", text,
        )
        self.assertIn("--lock-timeout 120", text)
        self.assertIn('chmod 400 "$snapshot"', text)
        self.assertIn("timeout --signal=TERM --kill-after=10s 180s", text)
        self.assertEqual(
            parsed_workflow()["jobs"]["audit"]["timeout-minutes"], 12,
        )
        self.assertEqual(
            parsed_workflow()["concurrency"]["group"], "production-maintenance",
        )
        upload = text.split("Upload exact reviewer document", 1)[1]
        self.assertNotIn("sim-ledger.snapshot", upload)
        self.assertIn("condition17-bootstrap-review.json", upload)

    def test_workflow_uses_pinned_host_key_and_cleans_all_temporary_files(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("secrets.DEPLOY_SSH_KEY", text)
        self.assertIn("secrets.DEPLOY_HOST", text)
        self.assertIn("secrets.DEPLOY_USER", text)
        self.assertIn("secrets.DEPLOY_SSH_HOST_KEY", text)
        self.assertIn("secrets.DEPLOY_SSH_HOST_FINGERPRINT", text)
        self.assertNotIn("ssh-keyscan", text)
        self.assertIn("-o StrictHostKeyChecking=yes", text)
        self.assertIn('-o UserKnownHostsFile="$WORK/known_hosts"', text)
        self.assertIn("-o GlobalKnownHostsFile=/dev/null", text)
        self.assertIn("trap 'rm -f \"$snapshot\"' EXIT", text)
        self.assertIn('--activation-marker "$WORK/synthetic-marker.json"', text)
        self.assertIn(
            "/var/lib/footbreak/activation/condition17-legacy-cohort-v1.json",
            text,
        )
        self.assertIn('rm -rf -- "$WORK"', text)
        self.assertNotIn("cat ~/.ssh/id_ed25519", text)

    def test_deployment_does_not_activate_condition_17(self) -> None:
        deploy_workflow = (
            ROOT / ".github" / "workflows" / "deploy.yml"
        ).read_text(encoding="utf-8")
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        marker = "condition17-legacy-cohort-v1.json"
        self.assertNotIn(marker, deploy_workflow)
        self.assertNotIn(marker, update)


def parsed_workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
