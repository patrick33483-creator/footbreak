#!/usr/bin/env python3
"""Build the reviewer artifact for condition 17 from a locked ledger copy.

This program is deliberately offline and read-only.  It emits only irreversible
identity hashes and aggregate counts; fixture identifiers and source rows never
leave the private snapshot.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis import wilson_validation as wv

PREFLIGHT_PATH = ROOT / "deploy" / "condition17-production-preflight.py"
SPEC = importlib.util.spec_from_file_location("condition17_preflight", PREFLIGHT_PATH)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)

PINNED_VALIDATION_SHA256 = "d0b2d4a4b9605459dbacb71dc18f099e684814546c75b5a3ff37cc870de1f47d"
PINNED_QUARTER_LINE_SHA256 = "f38c63c879ffe48f5bec77c289652152b215f8d24be9a9b8634f22b576cda3a9"
PINNED_CONDITION17_SIGNATURE = "0a53d616f4b205339da39824"
PINNED_CONDITION17_INITIAL_EVIDENCE_HASH = (
    "eef5807b2cf919727c668c8e933ac9398338032ff02059a0dd3ded8163babca3"
)
GIT_OID_RE = re.compile(r"[0-9a-f]{40}")
PUBLIC_BOOTSTRAP_FAILURE_CODES = frozenset({
    "canonical_manifest_mismatch",
    "canonical_manifest_unavailable",
    "condition_17_active_pointer_invalid",
    "condition_17_anomaly_cohort_mismatch",
    "condition_17_definition_invalid",
    "condition_17_identity_chain_invalid",
    "condition_17_manifest_entry_mismatch",
    "condition_17_missing",
    "condition_17_not_at_v1",
    "condition_17_projection_progress_invalid",
    "condition_17_projection_unavailable",
    "condition_17_registry_position_invalid",
    "dispatched_deployed_commit_invalid",
    "dispatched_deployed_tree_invalid",
    "durable_18_of_20_progress_invalid",
    "durable_progress_missing",
    "existing_manifest_mismatch",
    "extra_or_missing_same_signature_rows",
    "formal_evidence_container_invalid",
    "formal_evidence_row_invalid",
    "independent_condition17_initial_evidence_pin_mismatch",
    "independent_condition17_signature_pin_mismatch",
    "ledger_not_object",
    "ledger_size_exceeded",
    "ledger_snapshot_changed_during_preflight",
    "ledger_snapshot_changed_during_read",
    "ledger_snapshot_file_invalid",
    "ledger_snapshot_identity_changed",
    "namespace_invalid",
    "output_file_verification_failed",
    "output_parent_identity_changed",
    "output_parent_invalid",
    "output_path_exists",
    "output_path_identity_changed",
    "output_write_failed",
    "pinned_quarter_line_sha256_mismatch",
    "pinned_wilson_validation_sha256_mismatch",
    "projection_mutated_ledger",
    "simulation_identity_drift",
    "source_object_mutated",
    "strict_synthetic_row_derivation_failed",
    "synthetic_marker_missing",
    "synthetic_row_19_progress_invalid",
    "synthetic_row_19_projection_invalid",
    "synthetic_row_20_rollover_invalid",
    "synthetic_simulation_mutated_history",
    "synthetic_snapshot_binding_invalid",
    "synthetic_snapshot_source_ambiguous",
    "synthetic_snapshot_source_missing",
    "synthetic_time_source_invalid",
    "synthetic_time_window_unavailable",
    "synthetic_v2_projection_invalid",
    "synthetic_watch_container_invalid",
    "trusted_condition_signature_mismatch",
    "trusted_hash_input_invalid",
    "trusted_initial_evidence_hash_mismatch",
    "trusted_manifest_hash_mismatch",
})


def _public_failure_code(exc: BaseException) -> str:
    code = str(exc)
    if code in PUBLIC_BOOTSTRAP_FAILURE_CODES:
        return code
    return "bootstrap_invariant_failure"


class BootstrapArgumentFailure(Exception):
    pass


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise BootstrapArgumentFailure("bootstrap_argument_invalid")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def build_review(
    snapshot: Path, *, deployed_commit: str, deployed_tree: str,
    validation_sha256: str, quarter_line_sha256: str,
) -> dict[str, Any]:
    # Commit and tree authority is supplied by the workflow only after it binds
    # both values to GITHUB_SHA and that commit's tree. Keeping those identities
    # out of this file avoids an impossible self-referential commit/tree pin.
    preflight._require(
        GIT_OID_RE.fullmatch(deployed_commit) is not None,
        "dispatched_deployed_commit_invalid",
    )
    preflight._require(
        GIT_OID_RE.fullmatch(deployed_tree) is not None,
        "dispatched_deployed_tree_invalid",
    )
    authorities = (
        (validation_sha256, PINNED_VALIDATION_SHA256, "wilson_validation_sha256"),
        (quarter_line_sha256, PINNED_QUARTER_LINE_SHA256, "quarter_line_sha256"),
    )
    for actual, expected, name in authorities:
        preflight._require(actual == expected, f"pinned_{name}_mismatch")

    with preflight._verified_snapshot(snapshot) as raw:
        ledger_sha256 = hashlib.sha256(raw).hexdigest()
        ledger = json.loads(raw)
        preflight._require(isinstance(ledger, dict), "ledger_not_object")
        before = canonical_bytes(ledger)
        frozen, signature = preflight._condition_17(ledger)
        preflight._require(
            signature == PINNED_CONDITION17_SIGNATURE,
            "independent_condition17_signature_pin_mismatch",
        )
        namespace = ledger[wv.NAMESPACE]
        candidate, _validated, reason = wv._expected_production_identity_manifest(
            namespace, preflight.SYSTEM,
        )
        preflight._require(
            reason is None and isinstance(candidate, dict),
            "canonical_manifest_unavailable",
        )
        existing = namespace.get("production_identity_manifest")
        preflight._require(
            existing is None or canonical_bytes(existing) == canonical_bytes(candidate),
            "existing_manifest_mismatch",
        )
        versions = frozen.get("evidence_versions")
        preflight._require(
            isinstance(versions, list) and len(versions) == 1
            and isinstance(versions[0], dict),
            "condition_17_not_at_v1",
        )
        initial_hash = versions[0].get("evidence_hash")
        preflight._require(
            initial_hash == PINNED_CONDITION17_INITIAL_EVIDENCE_HASH,
            "independent_condition17_initial_evidence_pin_mismatch",
        )
        entries = candidate.get("entries")
        preflight._require(
            isinstance(entries, list)
            and len(entries) >= preflight.CONDITION_NUMBER
            and entries[preflight.CONDITION_NUMBER - 1] == {
                "condition_number": preflight.CONDITION_NUMBER,
                "condition_signature": signature,
                "definition_hash": wv._canonical_hash(frozen.get("definition")),
                "initial_evidence_hash": initial_hash,
            },
            "condition_17_manifest_entry_mismatch",
        )
        now = datetime.now(timezone.utc)
        # The compatibility runtime deliberately requires the identity root
        # before its path can activate. Bootstrap the candidate only in an
        # in-memory audit copy; the locked snapshot and production ledger stay
        # untouched.
        simulation_input = copy.deepcopy(ledger)
        simulation_input[wv.NAMESPACE][
            "production_identity_manifest"
        ] = copy.deepcopy(candidate)
        simulated_frozen, simulated_signature = preflight._condition_17(
            simulation_input,
        )
        preflight._require(
            simulated_signature == signature, "simulation_identity_drift",
        )
        anomaly_rows = preflight._verify_exact_anomaly_cohort(
            simulation_input, simulated_frozen, signature, projection_time=now,
        )
        preflight._verify_durable_progress(
            simulation_input, simulated_frozen, signature,
        )

        # Exercise the exact post-marker compatibility path only in a deep
        # copy. The workflow supplies a private synthetic marker to the
        # imported module.
        preflight._simulate_rollover(
            simulation_input, simulated_frozen, signature, anomaly_rows, now,
        )
        preflight._require(canonical_bytes(ledger) == before, "source_object_mutated")
        return {
            "schema": "footbreak-condition17-bootstrap-review-v1",
            "result": "GO",
            "read_only": True,
            "production_mutation": False,
            "provider_calls": 0,
            "telegram_calls": 0,
            "settlement_calls": 0,
            "deployed_commit": deployed_commit,
            "deployed_tree": deployed_tree,
            "wilson_validation_sha256": validation_sha256,
            "quarter_line_sha256": quarter_line_sha256,
            "ledger_sha256": ledger_sha256,
            "candidate_production_identity_manifest": copy.deepcopy(candidate),
            "manifest_hash": candidate["manifest_hash"],
            "condition17": {
                "condition_number": 17,
                "condition_signature": signature,
                "definition_hash": entries[16]["definition_hash"],
                "initial_evidence_hash": initial_hash,
                "compatible_rows": 18,
                "durable_decided": 18,
                "durable_required": 20,
                "durable_hits": 10,
                "durable_progress": "18/20",
                "exclusions": {
                    key: 0 for key in sorted(preflight.EXCLUSION_KEYS)
                },
            },
            "snapshot_exported": False,
            "contains_fixture_ids_or_raw_rows": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser()
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--deployed-tree", required=True)
    parser.add_argument("--validation-sha256", required=True)
    parser.add_argument("--quarter-line-sha256", required=True)
    parser.add_argument("--activation-marker", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
    except BootstrapArgumentFailure:
        print(
            "condition17_bootstrap_failure=bootstrap_argument_invalid",
            file=sys.stderr,
        )
        return 1
    original = wv.CONDITION17_ACTIVATION_MARKER
    diagnostic: str | None = None
    try:
        wv.CONDITION17_ACTIVATION_MARKER = args.activation_marker
        result = build_review(
            args.ledger,
            deployed_commit=args.deployed_commit,
            deployed_tree=args.deployed_tree,
            validation_sha256=args.validation_sha256,
            quarter_line_sha256=args.quarter_line_sha256,
        )
        rc = 0
    except preflight.PreflightFailure as exc:
        reason = _public_failure_code(exc)
        result = {
            "schema": "footbreak-condition17-bootstrap-review-v1",
            "result": "NO-GO", "read_only": True,
            "production_mutation": False, "reason": reason,
        }
        diagnostic = reason
        rc = 1
    except Exception:
        reason = "bootstrap_unexpected_failure"
        result = {
            "schema": "footbreak-condition17-bootstrap-review-v1",
            "result": "NO-GO", "read_only": True,
            "production_mutation": False,
            "reason": reason,
        }
        diagnostic = reason
        rc = 1
    finally:
        wv.CONDITION17_ACTIVATION_MARKER = original
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    try:
        preflight._write_output_exclusive(args.output, payload)
    except preflight.PreflightFailure as exc:
        diagnostic = _public_failure_code(exc)
        print(
            f"condition17_bootstrap_failure={diagnostic}",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "condition17_bootstrap_failure=bootstrap_output_failure",
            file=sys.stderr,
        )
        return 1
    if diagnostic is not None:
        print(
            f"condition17_bootstrap_failure={diagnostic}",
            file=sys.stderr,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
