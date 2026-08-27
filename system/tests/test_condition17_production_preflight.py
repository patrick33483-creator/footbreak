from __future__ import annotations

import copy
import contextlib
import io
import importlib.util
import json
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
                "expected_manifest_hash",
                "expected_condition_signature",
                "expected_initial_evidence_hash",
            },
        )
        self.assertTrue(all(value.get("required") is True for value in inputs.values()))
        self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', text)
        self.assertNotIn("git push", text)

    def test_workflow_uses_bounded_shared_lock_and_never_uploads_ledger(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("/var/lock/footbreak.lock", text)
        self.assertIn("/opt/footbreak/system/sim_ledger.json", text)
        self.assertIn("fcntl.LOCK_SH | fcntl.LOCK_NB", text)
        self.assertIn("time.monotonic() + 120", text)
        self.assertIn("os.O_RDONLY | os.O_NOFOLLOW", text)
        self.assertIn("chmod 400 preflight-input/sim-ledger.snapshot", text)
        upload = text.split("Upload bounded preflight result", 1)[1]
        self.assertNotIn("sim-ledger.snapshot", upload)
        self.assertNotIn("preflight-input", upload)
        self.assertIn("condition17-production-preflight-summary.json", upload)

    def test_workflow_uses_existing_secrets_and_removes_private_key(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("secrets.DEPLOY_SSH_KEY", text)
        self.assertIn("secrets.DEPLOY_HOST", text)
        self.assertIn("secrets.DEPLOY_USER", text)
        self.assertIn("ssh-keyscan -H", text)
        self.assertIn("shred -u ~/.ssh/id_ed25519", text)
        self.assertNotIn("cat ~/.ssh/id_ed25519", text)


if __name__ == "__main__":
    unittest.main()
