import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from analysis.wilson_audit_gate import EXPECTED_RELEASE, enforce
from analysis.tests.test_wilson_37_condition_regression import (
    _checked_out_commit, _registry,
)
from analysis import wilson_validation as wv
from analysis.wilson_registry_manifest import build_manifest
from analysis.wilson_registry_export import export_registry


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
        self.assertIn("WILSON_SANITIZED_CHAINS_%s_BEGIN", text)
        self.assertIn("WILSON_SANITIZED_CHAINS_%s_END", text)

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
            value, _specs, _allowlist, document = _registry(system)
            if system == "footbreak":
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


if __name__ == "__main__":
    unittest.main()
