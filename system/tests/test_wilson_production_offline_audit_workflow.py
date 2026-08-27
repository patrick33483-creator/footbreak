import copy
import base64
import hashlib
import json
import os
import subprocess
import tempfile
import signal
import time
import unittest
import importlib.util
import pwd
from pathlib import Path
from unittest import mock

import yaml

from analysis.wilson_audit_gate import (
    EXPECTED_RELEASE, enforce, summary_projection,
)
from analysis.wilson_audit_bundle import (
    ARTIFACT_NAMES, validate_and_commit, validate_expected_document,
)
from analysis.tests.test_wilson_37_condition_regression import (
    _active_projection, _checked_out_commit, _registry, _retirement_document,
)
from analysis import wilson_validation as wv
from analysis.wilson_registry_manifest import build_manifest
from analysis.wilson_registry_export import export_registry
from analysis.legacy_batch_aggregate import (
    build_live_discovery, canonical_hash_v1, serialize_ledger_bytes_v1,
    validate_sanitized_calculation,
)
from analysis.legacy_batch_discovery_publication import (
    INVALID_KEYS, INVALID_SCHEMA, finalize_publication,
    publish_remote_receipt, run_captured_command, seal_publication,
    verify_root_sealed, write_known_hosts_pin, _secure_delete,
)
from analysis.tests.test_legacy_batch_aggregate import (
    calculation_document, fixture_ledger, test_runtime,
)


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "wilson-production-offline-audit.yml"
AUDITED_COMMIT = "a" * 40


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
        self.assertIn("footbreak-wilson-registry-chains.json", text)
        self.assertIn("crown-wilson-registry-chains.json", text)
        self.assertNotIn("cat audit-input", text)
        self.assertIn("footbreak-legacy-batch-live-discovery.json", text)
        upload = text.split(
            "- name: Upload root-owned validated audit bundle", 1
        )[1].split(
            "- name: Enforce final bundle status", 1
        )[0]
        self.assertNotIn("audit-input/", upload)
        self.assertIn("${{ steps.root_bundle.outputs.bundle_path }}", upload)

    def test_legacy_discovery_is_remote_root_configured_and_commit_bound(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Capture authority-neutral legacy batch discovery", text)
        self.assertIn(
            "git -C /opt/footbreak rev-parse HEAD", text,
        )
        self.assertIn("'$AUDITED_COMMIT'", text)
        self.assertIn(
            "--runtime-config /etc/footbreak/legacy-batch-runtime.json", text,
        )
        self.assertIn("--require-quiesced", text)
        self.assertIn("sudo -n test \\\"\\$(stat -c %u", text)
        self.assertIn("= 0", text)
        self.assertIn("stat -c %a", text)
        self.assertIn("= 600", text)
        self.assertIn("LEGACY_BATCH_DISCOVERY_LEDGER_HASH_BOUND", text)
        self.assertIn('test "$LEGACY_DISCOVERY_BINDING_RC" -eq 0', text)

    def test_legacy_discovery_has_privacy_allowlist_and_invalid_fallback(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "analysis.legacy_batch_discovery_publication receipt", text,
        )
        self.assertIn(
            "analysis.legacy_batch_discovery_publication finalize", text,
        )
        self.assertNotIn('cat -- "$process_log"', text)
        self.assertIn(
            '--path "$staging" --identity "$staging_identity"', text,
        )
        self.assertIn(
            '--path "$calculation" --identity "$calculation_identity"', text,
        )
        self.assertIn(
            '--path "$process_log" --identity "$process_log_identity"', text,
        )
        self.assertNotIn("cat footbreak-legacy-batch-live-discovery.json", text)
        self.assertIn("Pre-publication enforce valid audit", text)
        self.assertIn("Finalize sanitized discovery publication", text)
        self.assertLess(
            text.index("Pre-publication enforce valid audit"),
            text.index("Upload root-owned validated audit bundle"),
        )

    def test_legacy_discovery_workflow_has_no_production_mutation_command(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        discovery_step = text.split(
            "- name: Capture authority-neutral legacy batch discovery", 1
        )[1].split(
            "- name: Capture immutable ledger copies under production locks", 1
        )[0]
        self.assertNotIn("migrate_legacy_batch_aggregates", discovery_step)
        self.assertNotIn("--apply", discovery_step)
        self.assertNotIn("os.replace('/opt/footbreak", discovery_step)
        self.assertNotIn("sim_ledger.json >", discovery_step)
        self.assertNotIn("tee /opt/footbreak", discovery_step)
        self.assertIn(
            "analysis.export_legacy_batch_live_authority", discovery_step,
        )
        self.assertIn('private_receipt="$(mktemp -d)"', discovery_step)
        self.assertIn('staging="$private_receipt/remote-discovery.json"', discovery_step)
        self.assertNotIn("LEGACY_BATCH_DISCOVERY_CAPTURE_BEGIN", discovery_step)
        self.assertNotIn("LEGACY_BATCH_DISCOVERY_CAPTURE_END", discovery_step)

    def test_ssh_host_key_is_independently_pinned_and_strict(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("ssh-keyscan", text)
        self.assertIn(
            "DEPLOY_SSH_KNOWN_HOSTS: ${{ secrets.DEPLOY_SSH_KNOWN_HOSTS }}",
            text,
        )
        self.assertIn(
            "analysis.legacy_batch_discovery_publication host-pin", text,
        )
        self.assertGreaterEqual(text.count("StrictHostKeyChecking=yes"), 3)
        self.assertGreaterEqual(text.count("UserKnownHostsFile="), 3)

    def test_host_pin_rejects_blank_cr_space_malformed_and_wrong_host(self):
        valid = "prod.example ssh-ed25519 " + base64.b64encode(
            b"a" * 32
        ).decode()
        invalid = (
            "", valid + "\n", valid + "\n\n", valid + "\r",
            " " + valid, valid + " ", valid.replace("prod.example", "wrong.example"),
            "prod.example unknown-key YQ==", "prod.example ssh-ed25519 !!!",
            valid + " extra",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, value in enumerate(invalid):
                with self.subTest(value=repr(value)):
                    with self.assertRaises(ValueError):
                        write_known_hosts_pin(
                            "prod.example", value, root / f"bad-{index}",
                        )
            output = root / "known_hosts"
            write_known_hosts_pin("prod.example", valid, output)
            self.assertEqual(output.read_text(), valid + "\n")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def _staged_discovery(
        self, root: Path, commit: str | None = None, ledger=None,
    ):
        calculation = calculation_document()
        context = validate_sanitized_calculation(calculation)
        runtime = test_runtime()
        if commit is not None:
            runtime["release_commit"] = commit
        commit = runtime["release_commit"]
        ledger = fixture_ledger() if ledger is None else ledger
        discovery = build_live_discovery(
            ledger, context, execution_identity=runtime,
            writer_coordination={
                "all_writers_quiesced": True,
                "canonical_lock": {
                    "realpath": "/var/lock/footbreak.lock",
                    "st_dev": 1, "st_ino": 2, "st_uid": 0, "st_gid": 0,
                    "st_mode": 384, "st_nlink": 1,
                },
                "writer_inventory_root": "a" * 64, "writer_count": 5,
                "service_configuration_sha256": "b" * 64,
                "runtime_config": {
                    "realpath": "/etc/footbreak/legacy-batch-runtime.json",
                    "st_dev": 1, "st_ino": 4, "st_uid": 0, "st_gid": 0,
                    "st_mode": 384, "st_nlink": 1, "sha256": "c" * 64,
                },
            },
            capture={"ledger_object": {
                "realpath": "/opt/footbreak/system/sim_ledger.json",
                "st_dev": 1, "st_ino": 3, "st_uid": 0, "st_gid": 0,
                "st_mode": 420, "st_nlink": 1,
            }},
        )
        staging = root / "remote.json"
        approved = root / "calculation.json"
        publication = root / "publication.json"
        staging.write_bytes(serialize_ledger_bytes_v1(discovery))
        approved.write_bytes(serialize_ledger_bytes_v1(calculation))
        staging.chmod(0o600)
        approved.chmod(0o600)
        return discovery, staging, approved, publication, commit

    def test_malformed_sensitive_remote_bytes_are_destroyed_and_never_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _document, staging, calculation, publication, commit = (
                self._staged_discovery(root)
            )
            secret = b'SECRET-CUSTOMER-DATA@example.invalid'
            staging.write_bytes(
                b'{"capture":{"secret_token":"' + secret + b'"}}'
            )
            self.assertFalse(publish_remote_receipt(
                staging, calculation, publication, commit, 0,
            ))
            self.assertFalse(staging.exists())
            published = publication.read_bytes()
            self.assertNotIn(secret, published)
            value = json.loads(published)
            self.assertEqual(value["schema"], INVALID_SCHEMA)
            self.assertEqual(set(value), INVALID_KEYS)

    def test_wrong_hash_spoof_duplicate_and_truncation_are_invalidated(self):
        mutations = (
            lambda value: value.update(discovery_document_sha256="f" * 64),
            lambda value: value["chain_preimages"].append(
                copy.deepcopy(value["chain_preimages"][0])
            ),
            lambda value: value["chain_preimages"].pop(),
            lambda value: value["capture"].update(
                secret_token="TOP-SECRET-CUSTOMER@example.invalid"
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                document, staging, calculation, publication, commit = (
                    self._staged_discovery(root)
                )
                mutation(document)
                if document["discovery_document_sha256"] != "f" * 64:
                    body = {
                        key: value for key, value in document.items()
                        if key != "discovery_document_sha256"
                    }
                    document["discovery_document_sha256"] = canonical_hash_v1(body)
                staging.write_bytes(serialize_ledger_bytes_v1(document))
                self.assertFalse(publish_remote_receipt(
                    staging, calculation, publication, commit, 0,
                ))
                self.assertEqual(
                    json.loads(publication.read_text())["schema"], INVALID_SCHEMA,
                )

    def test_gate_failure_atomically_invalidates_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _document, staging, calculation, publication, commit = (
                self._staged_discovery(root)
            )
            self.assertTrue(publish_remote_receipt(
                staging, calculation, publication, commit, 0,
            ))
            self.assertFalse(finalize_publication(
                publication, calculation, commit, gates_ok=False,
            ))
            invalid = json.loads(publication.read_text())
            self.assertEqual(set(invalid), INVALID_KEYS)
            self.assertEqual(invalid["schema"], INVALID_SCHEMA)
            self.assertEqual(
                invalid["failure_classification"],
                "whole_workflow_gate_failed_closed",
            )

    def test_staging_calculation_and_publication_aliases_never_touch_targets(self):
        for alias_kind in ("symlink", "hardlink"):
            for field in ("staging", "calculation"):
                with self.subTest(alias_kind=alias_kind, field=field), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    _document, staging, calculation, publication, commit = (
                        self._staged_discovery(root)
                    )
                    original = staging if field == "staging" else calculation
                    target = root / "alias-target"
                    secret = b"ALIAS-TARGET-MUST-STAY-UNCHANGED"
                    target.write_bytes(secret)
                    target.chmod(0o600)
                    original.unlink()
                    if alias_kind == "symlink":
                        original.symlink_to(target)
                    else:
                        os.link(target, original)
                    self.assertFalse(publish_remote_receipt(
                        staging, calculation, publication, commit, 0,
                    ))
                    self.assertEqual(target.read_bytes(), secret)
                    self.assertEqual(
                        json.loads(publication.read_text())["schema"],
                        INVALID_SCHEMA,
                    )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _document, staging, calculation, publication, commit = (
                self._staged_discovery(root)
            )
            self.assertTrue(publish_remote_receipt(
                staging, calculation, publication, commit, 0,
            ))
            target = root / "publication-target"
            target.write_bytes(b"PUBLICATION-TARGET-UNCHANGED")
            target.chmod(0o600)
            publication.unlink()
            publication.symlink_to(target)
            self.assertFalse(finalize_publication(
                publication, calculation, commit, gates_ok=False,
            ))
            self.assertEqual(
                target.read_bytes(), b"PUBLICATION-TARGET-UNCHANGED",
            )

    def test_workflow_has_local_and_remote_signal_cleanup_traps(self):
        text = WORKFLOW.read_text()
        discovery = text.split(
            "- name: Capture authority-neutral legacy batch discovery", 1
        )[1].split(
            "- name: Capture immutable ledger copies under production locks", 1
        )[0]
        self.assertIn("trap cleanup EXIT HUP INT TERM", discovery)
        self.assertIn("trap remote_cleanup EXIT", discovery)
        self.assertIn("trap 'remote_signal 15' TERM", discovery)
        self.assertIn("remote_output=\\$(sudo -n mktemp", discovery)
        self.assertIn("legacy_batch_discovery_publication cleanup", discovery)
        self.assertNotIn("/var/lib/footbreak-audit/legacy-batch-discovery-${", discovery)

    def test_retained_capture_fd_rejects_preopen_alias_without_truncating_target(self):
        for alias_kind in ("symlink", "hardlink"):
            with self.subTest(alias_kind=alias_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "target"
                target.write_bytes(b"ALIAS-TARGET-MUST-STAY")
                target.chmod(0o600)
                stdout = root / "stdout"
                if alias_kind == "symlink":
                    stdout.symlink_to(target)
                else:
                    os.link(target, stdout)
                with self.assertRaises((FileExistsError, OSError, ValueError)):
                    run_captured_command(
                        ["python", "-c", "print('REMOTE')"],
                        stdout, root / "stderr",
                    )
                self.assertEqual(target.read_bytes(), b"ALIAS-TARGET-MUST-STAY")

    def test_actual_capture_cli_cleans_and_kills_child_on_hup_int_term(self):
        for signum, expected in (
            (signal.SIGHUP, 129), (signal.SIGINT, 130),
            (signal.SIGTERM, 143),
        ):
            with self.subTest(signum=signum), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                stdout, stderr, pidfile = (
                    root / "stdout", root / "stderr", root / "child.pid",
                )
                child_code = (
                    "import os,time,pathlib;"
                    f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));"
                    "print('private', flush=True);time.sleep(30)"
                )
                process = subprocess.Popen([
                    "python", "-m", "analysis.legacy_batch_discovery_publication",
                    "capture", "--stdout", str(stdout), "--stderr", str(stderr),
                    "--", "python", "-c", child_code,
                ], cwd=ROOT)
                deadline = time.time() + 5
                while not pidfile.exists() and time.time() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pidfile.exists())
                child_pid = int(pidfile.read_text())
                os.kill(process.pid, signum)
                self.assertEqual(process.wait(timeout=8), expected)
                self.assertFalse(stdout.exists())
                self.assertFalse(stderr.exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)

    def test_remote_signal_handler_exits_and_does_not_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "remote"
            resumed = root / "resumed"
            script = f"""
            remote_output={output!s}
            : > "$remote_output"
            remote_cleanup() {{ rm -f -- "$remote_output"; }}
            remote_signal() {{
              signal_number="$1"
              trap - HUP INT TERM
              remote_cleanup
              exit $((128 + signal_number))
            }}
            trap remote_cleanup EXIT
            trap 'remote_signal 1' HUP
            trap 'remote_signal 2' INT
            trap 'remote_signal 15' TERM
            kill -TERM $$
            echo resumed > {resumed!s}
            """
            result = subprocess.run(["bash", "-c", script])
            self.assertEqual(result.returncode, 143)
            self.assertFalse(output.exists())
            self.assertFalse(resumed.exists())

    def test_final_receipt_identity_race_cannot_reach_sealed_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _document, staging, calculation, publication, commit = (
                self._staged_discovery(root)
            )
            self.assertTrue(publish_remote_receipt(
                staging, calculation, publication, commit, 0,
            ))
            original = publication.stat()
            digest = hashlib.sha256(publication.read_bytes()).hexdigest()
            identity = (original.st_dev, original.st_ino)
            self.assertTrue(finalize_publication(
                publication, None, commit, gates_ok=True,
                expected_sha256=digest, expected_identity=identity,
            ))
            replacement = root / "replacement"
            replacement.write_bytes(b"SECRET-CUSTOMER-UPLOAD")
            replacement.chmod(0o600)
            os.replace(replacement, publication)
            with self.assertRaises(ValueError):
                seal_publication(
                    publication, root / "sealed" / "discovery.json",
                    digest, identity,
                )

    def test_seal_internal_path_swap_is_rejected_after_fd_fchmod(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _document, staging, calculation, publication, commit = (
                self._staged_discovery(root)
            )
            self.assertTrue(publish_remote_receipt(
                staging, calculation, publication, commit, 0,
            ))
            info = publication.stat()
            digest = hashlib.sha256(publication.read_bytes()).hexdigest()
            sealed_dir = root / "sealed"
            sealed = sealed_dir / "discovery.json"
            replacement_bytes = b"SECRET-CUSTOMER-UPLOAD"
            def swap(path):
                replacement = root / "seal-race"
                replacement.write_bytes(replacement_bytes)
                replacement.chmod(0o600)
                os.replace(replacement, path)
            try:
                with self.assertRaisesRegex(
                    ValueError, "sealed_publication_identity_changed",
                ):
                    seal_publication(
                        publication, sealed, digest,
                        (info.st_dev, info.st_ino), race_hook=swap,
                    )
                self.assertEqual(sealed.read_bytes(), replacement_bytes)
            finally:
                if sealed_dir.exists():
                    sealed_dir.chmod(0o700)

    def test_cleanup_requires_identity_and_replacement_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "private"
            path.write_bytes(b"ORIGINAL")
            path.chmod(0o600)
            original = path.stat()
            replacement = root / "replacement"
            replacement.write_bytes(b"REPLACEMENT-MUST-SURVIVE")
            replacement.chmod(0o600)
            os.replace(replacement, path)
            with self.assertRaises(ValueError):
                _secure_delete(path, (original.st_dev, original.st_ino))
            self.assertEqual(path.read_bytes(), b"REPLACEMENT-MUST-SURVIVE")

    def test_seal_clears_residue_and_can_seal_canonical_invalid_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publication = root / "publication.json"
            invalid = {
                "schema": INVALID_SCHEMA, "valid": False,
                "audited_commit": AUDITED_COMMIT, "exporter_exit_code": 2,
                "failure_classification": "whole_workflow_gate_failed_closed",
                "production_mutation": False,
            }
            publication.write_bytes(serialize_ledger_bytes_v1(invalid))
            publication.chmod(0o600)
            info = publication.stat()
            digest = hashlib.sha256(publication.read_bytes()).hexdigest()
            sealed_dir = root / "sealed"
            sealed_dir.mkdir()
            residue = sealed_dir / "discovery.json"
            residue.write_bytes(b"STALE-SECRET")
            residue.chmod(0o600)
            try:
                sealed_hash, _identity = seal_publication(
                    publication, residue, digest,
                    (info.st_dev, info.st_ino),
                )
                self.assertEqual(sealed_hash, digest)
                self.assertEqual(
                    residue.read_bytes(), serialize_ledger_bytes_v1(invalid),
                )
                self.assertEqual(residue.stat().st_mode & 0o777, 0o400)
                self.assertEqual(sealed_dir.stat().st_mode & 0o777, 0o500)
            finally:
                sealed_dir.chmod(0o700)

    def test_workflow_promotes_and_verifies_root_object_before_upload(self):
        text = WORKFLOW.read_text()
        promote = text.index("Build root-owned immutable audit bundle")
        verify = text.index("Verify root-owned bundle immediately before upload")
        upload = text.index("Upload root-owned validated audit bundle")
        self.assertLess(promote, verify)
        self.assertLess(verify, upload)
        self.assertIn(
            "runs-on: [self-hosted, linux, x64, footbreak-audit-hardened]",
            text,
        )
        self.assertIn("/usr/local/sbin/footbreak-audit-bundle", text)
        self.assertIn("AUDIT_BUNDLE_WRAPPER_SHA256", text)
        self.assertIn("AUDIT_SUDO_POLICY_ROOT", text)
        self.assertIn("footbreak-audit-sudo-preflight-v1", text)
        self.assertNotIn("sudo -n -l", text)
        self.assertNotIn("grep 'NOPASSWD:'", text)
        self.assertNotIn("sys.path.insert(0,root)", text)
        self.assertIn(
            "if: always() && steps.verify_bundle.outcome == 'success'", text,
        )
        self.assertIn("Remove root-owned audit bundle", text)

    def test_root_bundle_covers_exact_seven_artifacts_and_fixed_trust_boundary(self):
        text = WORKFLOW.read_text()
        names = (
            "ledger-sha256.txt",
            "wilson-production-audit-summary.json",
            "footbreak-wilson-registry-audit.json",
            "crown-wilson-registry-audit.json",
            "footbreak-wilson-registry-chains.json",
            "crown-wilson-registry-chains.json",
            "footbreak-legacy-batch-live-discovery.json",
        )
        validation = text.split(
            "- name: Validate and hash exact seven workspace artifacts", 1
        )[1].split("- name: Build root-owned immutable audit bundle", 1)[0]
        self.assertIn("analysis.wilson_audit_bundle", validation)
        self.assertIn("approved-legacy-batch-calculation.json", validation)
        self.assertNotIn("json.loads", validation)
        self.assertNotIn("read_bytes", validation)
        root_step = text.split(
            "- name: Build root-owned immutable audit bundle", 1
        )[1].split(
            "- name: Verify root-owned bundle immediately before upload", 1
        )[0]
        self.assertIn("/usr/local/sbin/footbreak-audit-bundle", root_step)
        self.assertIn('bundle_path="/var/tmp/footbreak-audit-bundle-$RUN_KEY"', root_step)
        self.assertNotIn("import analysis", root_step)
        self.assertNotIn("/usr/bin/env -i", root_step)
        self.assertNotIn("command -v python", root_step)
        self.assertNotIn("sys.path.insert", root_step)
        verify = text.split(
            "- name: Verify root-owned bundle immediately before upload", 1
        )[1].split("- name: Upload root-owned validated audit bundle", 1)[0]
        self.assertIn('len(raw)!=item["size"]', verify)
        self.assertIn("artifact_hash", verify)
        self.assertIn("bundle_set", verify)
        upload = text.split(
            "- name: Upload root-owned validated audit bundle", 1
        )[1].split("- name: Enforce final bundle status", 1)[0]
        self.assertIn("steps.root_bundle.outputs.bundle_path", upload)
        self.assertNotIn("GITHUB_WORKSPACE", upload)
        self.assertNotIn("ledger-sha256.txt", upload)

    def test_cleanup_is_fixed_run_key_and_does_not_depend_on_receipt(self):
        text = WORKFLOW.read_text()
        cleanup = text.split("- name: Remove root-owned audit bundle", 1)[1]
        self.assertIn("if: always()", cleanup)
        self.assertIn("/usr/local/sbin/footbreak-audit-bundle cleanup", cleanup)
        self.assertIn('--run-key "$RUN_KEY"', cleanup)
        self.assertNotIn("steps.root_bundle.outputs.bundle_path", cleanup)

    def test_root_sealed_verifier_rejects_runner_owned_and_tampered_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sealed = root / "discovery.json"
            sealed.write_bytes(b"canonical")
            sealed.chmod(0o444)
            root.chmod(0o555)
            info = sealed.stat()
            try:
                with self.assertRaisesRegex(
                    ValueError, "root_sealed_identity_or_mode_mismatch",
                ):
                    verify_root_sealed(
                        sealed, hashlib.sha256(b"canonical").hexdigest(),
                        (info.st_dev, info.st_ino),
                    )
                replacement = root.parent / (root.name + "-replacement")
                replacement.write_bytes(b"SECRET")
                replacement.chmod(0o444)
                root.chmod(0o755)
                os.replace(replacement, sealed)
                root.chmod(0o555)
                with self.assertRaises(ValueError):
                    verify_root_sealed(
                        sealed, hashlib.sha256(b"canonical").hexdigest(),
                        (info.st_dev, info.st_ino),
                    )
            finally:
                root.chmod(0o700)

    def test_invalid_helper_bytes_are_exact_canonical_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            result = subprocess.run([
                "python", "-m", "analysis.legacy_batch_discovery_publication",
                "invalid", "--publication", str(path),
                "--commit", AUDITED_COMMIT, "--exit-code", "2",
                "--reason", "receipt_validation_failed_closed",
            ], cwd=ROOT, capture_output=True)
            self.assertEqual(result.returncode, 2)
            expected = {
                "schema": INVALID_SCHEMA, "valid": False,
                "audited_commit": AUDITED_COMMIT, "exporter_exit_code": 2,
                "failure_classification": "receipt_validation_failed_closed",
                "production_mutation": False,
            }
            self.assertEqual(path.read_bytes(), serialize_ledger_bytes_v1(expected))

    def test_workflow_enforces_exact_37_historical_35_active_release_gate(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("python3 -m analysis.wilson_audit_gate", text)
        self.assertIn('--audited-commit "$GITHUB_SHA"', text)
        self.assertNotIn("expected 37 total conditions", text)
        self.assertIn("--require-valid", text)

    def _artifacts(self, root: Path) -> None:
        inputs = root / "audit-input"
        inputs.mkdir()
        hashes = {}
        for system in EXPECTED_RELEASE:
            ledger = inputs / f"{system}-ledger.json"
            value, _specs, allowlist, _document = _registry(system)
            namespace = value[wv.NAMESPACE]
            for frozen in namespace["conditions"].values():
                first = copy.deepcopy(frozen["evidence_versions"][0])
                frozen["evidence_versions"] = [first]
                frozen["active_evidence_version"] = 1
                frozen["active_evidence_hash"] = first["evidence_hash"]
                frozen["active_evidence"] = _active_projection(first)
            production, _validated, reason = (
                wv._expected_production_identity_manifest(namespace, system)
            )
            self.assertIsNone(reason)
            namespace["production_identity_manifest"] = production
            if system == "footbreak":
                document = _retirement_document(value, allowlist)
                wv.apply_condition_identity_migration(
                    value, system, authorized_manifest=document,
                    expected_release_commit=_checked_out_commit(),
                )
            ledger.write_text(
                json.dumps(value, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            hashes[ledger.name] = hashlib.sha256(ledger.read_bytes()).hexdigest()
            manifest = build_manifest(value, system)
            self.assertTrue(manifest["valid"], manifest)
            (root / f"{system}-wilson-registry-audit.json").write_text(
                json.dumps(manifest), encoding="utf-8",
            )
            (root / f"{system}-wilson-registry-chains.json").write_text(
                json.dumps(export_registry(
                    value, system, source_ledger_sha256=hashes[ledger.name],
                )),
                encoding="utf-8",
            )
        (root / "ledger-sha256.txt").write_text(
            "".join(
                f"{digest}  audit-input/{name}\n"
                for name, digest in hashes.items()
            ),
            encoding="utf-8",
        )
        (root / "wilson-production-audit-summary.json").write_text(
            json.dumps({
                "schema": "wilson-production-offline-audit-v1",
                "audited_commit": AUDITED_COMMIT,
                "captured_at_utc": "2026-08-27T02:00:00+00:00",
                "capture_outcome": "success",
                "capture_exit_codes": {"footbreak": 0, "crown": 0},
                "ledger_sha256": hashes,
                "exit_codes": {"footbreak": 0, "crown": 0},
                "systems": {
                    system: {
                        "valid": build_manifest(
                            json.loads(
                                (inputs / f"{system}-ledger.json").read_text(
                                    encoding="utf-8",
                                )
                            ),
                            system,
                        ).get("valid") is True,
                        **{
                            key: build_manifest(
                                json.loads(
                                    (inputs / f"{system}-ledger.json").read_text(
                                        encoding="utf-8",
                                    )
                                ),
                                system,
                            ).get(key)
                            for key in (
                                "condition_count", "historical_condition_count",
                                "active_condition_count",
                                "retired_duplicate_count",
                                "decision_stage_counts", "rejection_reasons",
                                "recovery",
                            )
                        },
                    }
                    for system in EXPECTED_RELEASE
                },
                "production_mutation": False,
                "recovery_enabled": False,
            }),
            encoding="utf-8",
        )

    def _bundle_fixture(self, root: Path) -> Path:
        self._artifacts(root)
        footbreak_raw = (
            root / "audit-input" / "footbreak-ledger.json"
        ).read_bytes()
        document = {
            "execution_identity": {"release_commit": AUDITED_COMMIT},
            "capture": {
                "full_pre_ledger_sha256": hashlib.sha256(footbreak_raw).hexdigest(),
            },
        }
        (root / "footbreak-legacy-batch-live-discovery.json").write_bytes(
            serialize_ledger_bytes_v1(document),
        )
        approved = root / "approved-legacy-batch-calculation.json"
        approved.write_bytes(serialize_ledger_bytes_v1(calculation_document()))
        return approved

    def test_descriptor_bound_bundler_commits_exact_seven_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approved = self._bundle_fixture(root)
            output = root / "bundle-expected.json"
            discovery = json.loads(
                (root / ARTIFACT_NAMES[-1]).read_text(),
            )
            with mock.patch(
                "analysis.wilson_audit_bundle.validate_live_discovery",
                return_value=discovery,
            ):
                digest, identity = validate_and_commit(
                    root, approved, output, audited_commit=AUDITED_COMMIT,
                )
            value = validate_expected_document(output.read_bytes())
            self.assertEqual(
                tuple(row["filename"] for row in value["artifacts"]),
                ARTIFACT_NAMES,
            )
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), digest)
            self.assertEqual(
                (output.stat().st_dev, output.stat().st_ino), identity,
            )
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_bundler_rejects_each_post_gate_artifact_substitution(self):
        for name in ARTIFACT_NAMES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                approved = self._bundle_fixture(root)
                path = root / name
                if name.endswith(".json"):
                    path.write_text(json.dumps({
                        "credential": "sk-live-undetected",
                        "customer": "Alice",
                    }))
                else:
                    path.write_text("password=not-json\n")
                discovery = json.loads(
                    (root / ARTIFACT_NAMES[-1]).read_text(),
                )
                patcher = (
                    mock.patch(
                        "analysis.wilson_audit_bundle.validate_live_discovery",
                        return_value=discovery,
                    )
                    if name != ARTIFACT_NAMES[-1]
                    else mock.patch(
                        "analysis.wilson_audit_bundle.validate_live_discovery",
                        side_effect=ValueError("discovery_schema"),
                    )
                )
                with patcher, self.assertRaises(Exception):
                    validate_and_commit(
                        root, approved, root / "bundle-expected.json",
                        audited_commit=AUDITED_COMMIT,
                    )

    def test_expected_manifest_aliases_are_not_mutated(self):
        for alias in ("symlink", "hardlink"):
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                approved = self._bundle_fixture(root)
                target = root / "target"
                target.write_bytes(b"UNCHANGED")
                output = root / "bundle-expected.json"
                if alias == "symlink":
                    output.symlink_to(target)
                else:
                    os.link(target, output)
                discovery = json.loads(
                    (root / ARTIFACT_NAMES[-1]).read_text(),
                )
                with mock.patch(
                    "analysis.wilson_audit_bundle.validate_live_discovery",
                    return_value=discovery,
                ), self.assertRaises(FileExistsError):
                    validate_and_commit(
                        root, approved, output, audited_commit=AUDITED_COMMIT,
                    )
                self.assertEqual(target.read_bytes(), b"UNCHANGED")

    def test_expected_manifest_rejects_duplicate_extra_and_wrong_types(self):
        row = {"filename": ARTIFACT_NAMES[0], "sha256": "a" * 64, "size": 1}
        attacks = [
            {"schema": "footbreak-audit-bundle-expected-v1", "artifacts": [row] * 7},
            {
                "schema": "footbreak-audit-bundle-expected-v1",
                "artifacts": [
                    {"filename": name, "sha256": "a" * 64, "size": 1}
                    for name in ARTIFACT_NAMES
                ],
                "extra": True,
            },
            {
                "schema": "footbreak-audit-bundle-expected-v1",
                "artifacts": [
                    {"filename": name, "sha256": "a" * 64, "size": True}
                    for name in ARTIFACT_NAMES
                ],
            },
        ]
        for value in attacks:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_expected_document(serialize_ledger_bytes_v1(value))

    def test_root_wrapper_cleans_final_path_after_post_rename_failure(self):
        wrapper_path = ROOT / "deploy" / "footbreak_audit_bundle_wrapper.py"
        spec = importlib.util.spec_from_file_location("audit_wrapper", wrapper_path)
        wrapper = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(wrapper)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            rows = []
            for name in ARTIFACT_NAMES:
                raw = name.encode()
                (workspace / name).write_bytes(raw)
                rows.append({
                    "filename": name, "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": len(raw),
                })
            expected = root / "expected.json"
            expected.write_text(json.dumps({
                "schema": "footbreak-audit-bundle-expected-v1",
                "artifacts": rows,
            }))
            removed = []
            def test_parent(path):
                return os.open(path, os.O_RDONLY | os.O_DIRECTORY)

            def tracked_remove(path, expected_identity=None):
                try:
                    os.stat(path, follow_symlinks=False)
                except FileNotFoundError:
                    return
                removed.append(path)
                os.chmod(path, 0o700)
                for child in Path(path).iterdir():
                    child.unlink()
                os.rmdir(path)

            with mock.patch.object(wrapper.os, "geteuid", return_value=0), \
                    mock.patch.object(wrapper.os, "chown"), \
                    mock.patch.object(wrapper, "_parent", side_effect=test_parent), \
                    mock.patch.object(wrapper, "_safe_remove", side_effect=tracked_remove):
                with self.assertRaisesRegex(RuntimeError, "after rename"):
                    wrapper.seal_bundle(
                        str(expected), hashlib.sha256(expected.read_bytes()).hexdigest(),
                        str(workspace), "1-1", parent=str(root),
                        fault_after_rename=lambda: (_ for _ in ()).throw(
                            RuntimeError("after rename"),
                        ),
                    )
            self.assertEqual(
                removed, [str(root / "footbreak-audit-bundle-1-1")],
            )
            self.assertFalse((root / "footbreak-audit-bundle-1-1").exists())

    def test_sudo_policy_inventory_accepts_exact_graph_and_detects_drift(self):
        wrapper_path = ROOT / "deploy" / "footbreak_audit_bundle_wrapper.py"
        spec = importlib.util.spec_from_file_location(
            "audit_wrapper_policy", wrapper_path,
        )
        wrapper = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(wrapper)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fragments = root / "sudoers.d"
            fragments.mkdir(mode=0o700)
            policy = root / "sudoers"
            policy.write_text(f"@includedir {fragments}\n")
            narrow = fragments / "footbreak-audit"
            narrow.write_text(
                "runner ALL=(root) NOPASSWD: "
                "/usr/local/sbin/footbreak-audit-bundle preflight\n"
            )
            policy.chmod(0o440)
            narrow.chmod(0o440)

            def direct_open(path, *, directory=False):
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if directory:
                    flags |= os.O_DIRECTORY
                return os.open(path, flags)

            with mock.patch.object(
                wrapper, "_open_absolute", side_effect=direct_open,
            ):
                inventory, approved = wrapper.sudo_policy_inventory(
                    str(policy), required_uid=os.getuid(),
                    required_gid=os.getgid(),
                )
                self.assertEqual(len(inventory["files"]), 2)
                for attack in (
                    "runner ALL=(root) NOPASSWD: /bin/bash\n",
                    "runner ALL=(root) NOPASSWD: ALL\n",
                    "runner ALL=(root) NOPASSWD: /bin/ba\\\nsh\n",
                    "Cmnd_Alias X=/bin/bash\nrunner ALL=(root) NOPASSWD: X\n",
                    "runner ALL=(root) NOPASSWD: /bin/*\n",
                    "runner ALL=(root) NOPASSWD: /bin/true, /bin/bash\n",
                ):
                    narrow.chmod(0o600)
                    narrow.write_text(attack)
                    narrow.chmod(0o440)
                    _document, drifted = wrapper.sudo_policy_inventory(
                        str(policy), required_uid=os.getuid(),
                        required_gid=os.getgid(),
                    )
                    self.assertNotEqual(drifted, approved)
                narrow.chmod(0o600)
                narrow.write_text(
                    "runner ALL=(root) NOPASSWD: "
                    "/usr/local/sbin/footbreak-audit-bundle preflight\n"
                )
                narrow.chmod(0o440)
                extra = fragments / "extra"
                extra.write_text("runner ALL=(root) NOPASSWD: /bin/bash\n")
                extra.chmod(0o440)
                _document, extra_root = wrapper.sudo_policy_inventory(
                    str(policy), required_uid=os.getuid(),
                    required_gid=os.getgid(),
                )
                self.assertNotEqual(extra_root, approved)
                extra.unlink()
                policy.chmod(0o600)
                policy.write_text(f"@include {narrow} trailing\n")
                policy.chmod(0o440)
                with self.assertRaisesRegex(
                    ValueError, "unknown_sudo_include_syntax",
                ):
                    wrapper.sudo_policy_inventory(
                        str(policy), required_uid=os.getuid(),
                        required_gid=os.getgid(),
                    )

    def test_sudo_policy_inventory_rejects_alias_mode_and_owner_attacks(self):
        wrapper_path = ROOT / "deploy" / "footbreak_audit_bundle_wrapper.py"
        spec = importlib.util.spec_from_file_location(
            "audit_wrapper_aliases", wrapper_path,
        )
        wrapper = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(wrapper)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("runner ALL=(root) NOPASSWD: /bin/true\n")
            target.chmod(0o440)

            def direct_open(path, *, directory=False):
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if directory:
                    flags |= os.O_DIRECTORY
                return os.open(path, flags)

            with mock.patch.object(
                wrapper, "_open_absolute", side_effect=direct_open,
            ):
                symlink = root / "sudoers-symlink"
                symlink.symlink_to(target)
                with self.assertRaises(OSError):
                    wrapper.sudo_policy_inventory(
                        str(symlink), required_uid=os.getuid(),
                        required_gid=os.getgid(),
                    )
                hardlink = root / "sudoers-hardlink"
                os.link(target, hardlink)
                with self.assertRaisesRegex(ValueError, "unsafe_policy_file"):
                    wrapper.sudo_policy_inventory(
                        str(hardlink), required_uid=os.getuid(),
                        required_gid=os.getgid(),
                    )
                hardlink.unlink()
                target.chmod(0o660)
                with self.assertRaisesRegex(ValueError, "unsafe_policy_file"):
                    wrapper.sudo_policy_inventory(
                        str(target), required_uid=os.getuid(),
                        required_gid=os.getgid(),
                    )
                target.chmod(0o440)
                with self.assertRaisesRegex(ValueError, "unsafe_policy_file"):
                    wrapper.sudo_policy_inventory(
                        str(target), required_uid=os.getuid() + 1,
                        required_gid=os.getgid(),
                    )

    def test_root_preflight_accepts_only_approved_inventory_and_identity(self):
        wrapper_path = ROOT / "deploy" / "footbreak_audit_bundle_wrapper.py"
        spec = importlib.util.spec_from_file_location(
            "audit_wrapper_preflight", wrapper_path,
        )
        wrapper = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(wrapper)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "sudoers"
            policy.write_text("runner ALL=(root) NOPASSWD: wrapper preflight\n")
            policy.chmod(0o440)
            installed = root / "wrapper"
            installed.write_bytes(wrapper_path.read_bytes())
            installed.chmod(0o555)

            def direct_open(path, *, directory=False):
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if directory:
                    flags |= os.O_DIRECTORY
                return os.open(path, flags)

            user = pwd.getpwuid(os.getuid()).pw_name
            with mock.patch.object(
                wrapper, "_open_absolute", side_effect=direct_open,
            ), mock.patch.object(wrapper.os, "geteuid", return_value=0), \
                    mock.patch.dict(os.environ, {
                        "SUDO_USER": user, "SUDO_UID": str(os.getuid()),
                    }):
                _inventory, policy_root = wrapper.sudo_policy_inventory(
                    str(policy), required_uid=os.getuid(),
                    required_gid=os.getgid(),
                )
                digest = hashlib.sha256(installed.read_bytes()).hexdigest()
                receipt = wrapper.preflight(
                    policy_root, digest, user, policy_path=str(policy),
                    wrapper_path=str(installed), required_uid=os.getuid(),
                    required_gid=os.getgid(),
                )
                self.assertEqual(receipt["policy_root"], policy_root)
                self.assertEqual(receipt["wrapper_sha256"], digest)
                policy.chmod(0o600)
                policy.write_text("runner ALL=(root) NOPASSWD: /bin/bash\n")
                policy.chmod(0o440)
                with self.assertRaisesRegex(
                    ValueError, "sudo_policy_root_mismatch",
                ):
                    wrapper.preflight(
                        policy_root, digest, user, policy_path=str(policy),
                        wrapper_path=str(installed), required_uid=os.getuid(),
                        required_gid=os.getgid(),
                    )

    def test_executable_gate_accepts_bound_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._artifacts(root)
            enforce(root, audited_commit=AUDITED_COMMIT)
            result = subprocess.run(
                [
                    "python", "-m", "analysis.wilson_audit_gate",
                    "--base-dir", str(root),
                    "--audited-commit", AUDITED_COMMIT,
                ],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_executable_gate_rejects_zeroed_manifest_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._artifacts(root)
            path = root / "footbreak-wilson-registry-audit.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["manifest_hash"] = "0" * 64
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest hash mismatch"):
                enforce(root, audited_commit=AUDITED_COMMIT)

    def test_executable_gate_rejects_arbitrary_declared_ledger_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._artifacts(root)
            path = root / "ledger-sha256.txt"
            lines = path.read_text(encoding="utf-8").splitlines()
            lines[0] = f"{'f' * 64}  audit-input/footbreak-ledger.json"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            summary_path = root / "wilson-production-audit-summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["ledger_sha256"]["footbreak-ledger.json"] = "f" * 64
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "captured ledger SHA-256"):
                enforce(root, audited_commit=AUDITED_COMMIT)

    def test_executable_gate_cross_checks_summary_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._artifacts(root)
            path = root / "wilson-production-audit-summary.json"
            summary = json.loads(path.read_text(encoding="utf-8"))
            summary["ledger_sha256"]["crown-ledger.json"] = "0" * 64
            path.write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "summary ledger SHA-256"):
                enforce(root, audited_commit=AUDITED_COMMIT)

    def test_executable_gate_rejects_contradictory_summary_systems(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._artifacts(root)
            path = root / "wilson-production-audit-summary.json"
            summary = json.loads(path.read_text(encoding="utf-8"))
            summary["systems"]["footbreak"]["active_condition_count"] = 17
            path.write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "systems projection"):
                enforce(root, audited_commit=AUDITED_COMMIT)

    def test_executable_gate_rejects_rehashed_ledger_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._artifacts(root)
            ledger_path = root / "audit-input" / "crown-ledger.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            signature = ledger[wv.NAMESPACE]["condition_order"][0]
            ledger[wv.NAMESPACE]["conditions"][signature]["definition"][
                "role"
            ] = "substituted"
            ledger_path.write_text(
                json.dumps(ledger, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            digest = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
            hash_path = root / "ledger-sha256.txt"
            lines = hash_path.read_text(encoding="utf-8").splitlines()
            lines = [
                (
                    f"{digest}  audit-input/crown-ledger.json"
                    if line.endswith("audit-input/crown-ledger.json") else line
                )
                for line in lines
            ]
            hash_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            summary_path = root / "wilson-production-audit-summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["ledger_sha256"]["crown-ledger.json"] = digest
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "supplied manifest does not match ledger",
            ):
                enforce(root, audited_commit=AUDITED_COMMIT)

    def test_gate_rejects_different_valid_export_labeled_with_real_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._artifacts(root)
            ledger_path = root / "audit-input" / "crown-ledger.json"
            real_hash = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
            different = json.loads(ledger_path.read_text(encoding="utf-8"))
            different[wv.NAMESPACE][
                "quarter_settlement_activation_at"
            ] = "2026-08-21T00:00:00+08:00"
            substituted = export_registry(
                different, "crown", source_ledger_sha256=real_hash,
            )
            # This document is internally valid and claims the genuine source
            # hash, but it was derived from a different ledger object.
            (root / "crown-wilson-registry-chains.json").write_text(
                json.dumps(substituted), encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "sanitized export does not match ledger",
            ):
                enforce(root, audited_commit=AUDITED_COMMIT)

    def test_full_gate_rejects_nested_private_namespace_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._artifacts(root)
            ledger_path = root / "audit-input" / "crown-ledger.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger[wv.NAMESPACE]["cutover_at"] = {
                "private_customer_email": "private@example.invalid",
            }
            ledger_path.write_text(
                json.dumps(ledger, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            digest = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
            hash_path = root / "ledger-sha256.txt"
            lines = [
                (
                    f"{digest}  audit-input/crown-ledger.json"
                    if line.endswith("audit-input/crown-ledger.json") else line
                )
                for line in hash_path.read_text(encoding="utf-8").splitlines()
            ]
            hash_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            summary_path = root / "wilson-production-audit-summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["ledger_sha256"]["crown-ledger.json"] = digest
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            # The ordinary registry manifest does not consume cutover_at, so
            # even a freshly rebuilt manifest cannot authorize this payload.
            rebuilt = build_manifest(ledger, "crown")
            (root / "crown-wilson-registry-audit.json").write_text(
                json.dumps(rebuilt), encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "namespace timestamp invalid",
            ):
                enforce(root, audited_commit=AUDITED_COMMIT)

    def test_full_gate_accepts_computed_export_root_when_crown_stored_root_absent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._artifacts(root)
            ledger_path = root / "audit-input" / "crown-ledger.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger[wv.NAMESPACE].pop("production_identity_manifest")
            ledger_path.write_text(
                json.dumps(ledger, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            digest = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
            hash_path = root / "ledger-sha256.txt"
            lines = [
                (
                    f"{digest}  audit-input/crown-ledger.json"
                    if line.endswith("audit-input/crown-ledger.json") else line
                )
                for line in hash_path.read_text(encoding="utf-8").splitlines()
            ]
            hash_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            manifest = build_manifest(ledger, "crown")
            self.assertTrue(manifest["valid"], manifest)
            (root / "crown-wilson-registry-audit.json").write_text(
                json.dumps(manifest), encoding="utf-8",
            )
            exported = export_registry(
                ledger, "crown", source_ledger_sha256=digest,
            )
            self.assertIsNotNone(exported["production_identity_manifest"])
            self.assertNotIn(
                "production_identity_manifest", ledger[wv.NAMESPACE],
            )
            (root / "crown-wilson-registry-chains.json").write_text(
                json.dumps(exported), encoding="utf-8",
            )

            summary_path = root / "wilson-production-audit-summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["ledger_sha256"]["crown-ledger.json"] = digest
            summary["systems"]["crown"] = summary_projection(manifest)
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            enforce(root, audited_commit=AUDITED_COMMIT)


if __name__ == "__main__":
    unittest.main()
