from __future__ import annotations

import copy
import base64
import json
import os
import stat
import subprocess
import sys
from contextlib import redirect_stdout
from io import StringIO
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analysis.crown_condition4_recovery import (
    AUTHORITY_CONTEXT, AUTHORITY_PAYLOAD_SCHEMA, AUTHORITY_SCHEMA, EXPECTED,
    RecoveryBlocked, _create_output, _plan_with_payload, _preflight_output,
    _strict_json_bytes, _write_retained, bytes_hash, canonical_hash,
    main as recovery_main, plan_recovery, verify_external_authority,
)
from analysis.wilson_validation import project_granular_ranking_evidence
from analysis.wilson_registry_manifest import build_manifest
from analysis import crown_condition4_recovery as recovery
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from unittest.mock import patch


def ranking_row(bucket: str, *, intended: bool = False) -> dict:
    return {
        "system": "crown",
        "market": "HDC",
        "key": [
            "system=crown", "market=HDC", "path=首預→T-30→T-5",
            "decision=T-5", "direction=A→A→A", "role=主讓",
            f"bucket={bucket}", "tier=<1.70",
            "tier_path=<1.70→<1.70→<1.70", "movement=不變",
        ],
        "observed_path": "首預→T-30→T-5",
        "decision_stage": "T-5",
        "direction": "A→A→A",
        "role": "主讓",
        "line_bucket": bucket,
        "odds_tier": "<1.70",
        "odds_trajectory": "<1.70→<1.70→<1.70",
        "movement": "不變",
        "selected_side": "H",
        "selected_line": -0.25,
        "total": {
            "hits": 41 if intended else 40,
            "decided": 62,
            "pushes": 0,
        },
        "holdout": {
            "hits": 11 if intended else 10,
            "decided": 19,
            "pushes": 0,
        },
        "source_artifact": {
            "hash": ("4" if intended else bucket[-1]) * 64,
            "version": "condition-4-replay-fixture-v1",
            "as_of": "2026-08-20T00:00:00+08:00",
        },
    }


def ledger_fixture() -> dict:
    ledger = {"bets": []}
    ranking = [
        ranking_row("seed-1"),
        ranking_row("seed-2"),
        ranking_row("seed-3"),
        ranking_row("0.25–0.5", intended=True),
    ]
    project_granular_ranking_evidence(
        ledger, "crown", ranking, now="2026-08-20T12:00:00+08:00",
    )
    frozen = next(
        row for row in ledger["wilson_validation"]["conditions"].values()
        if row["condition_number"] == 4
    )
    assert (frozen["active_evidence"]["cumulative_hits"],
            frozen["active_evidence"]["cumulative_decided"]) == (52, 81)
    return ledger


def replay_fixture(ledger: dict) -> dict:
    frozen = next(
        row for row in ledger["wilson_validation"]["conditions"].values()
        if row["condition_number"] == 4
    )
    signature = frozen["signature"]
    rows = []
    start = datetime.fromisoformat("2026-08-21T08:00:00+08:00")
    # Chronological first batch = 9/20; remaining settled tail = 12/19.
    hit_indexes = set(range(1, 10)) | set(range(21, 33))
    for index in range(1, 41):
        stage_at = start + timedelta(hours=index * 3)
        kickoff = stage_at + timedelta(minutes=5)
        pending = index == 40
        grade = None if pending else {
            "grade_status": "GRADED",
            "hit": index in hit_indexes,
            "result": "Won" if index in hit_indexes else "Lost",
        }
        row = {
            "match_id": f"crown-condition-4-{index:02d}",
            "league": "測試聯賽",
            "home": "Atlanta Reserves" if pending else f"Home {index}",
            "away": (
                "Estudiantes de Caseros Reserves" if pending else f"Away {index}"
            ),
            "kickoff_hkt": kickoff.isoformat(),
            "t5_recorded_at": stage_at.isoformat(),
            "stage_path": ["首預", "T-30", "T-5"],
            "role_path": ["主讓", "主讓", "主讓"],
            "selected_line_path": [-0.25, -0.25, -0.25],
            "market": "HDC",
            "selected_side": "H",
            "selected_line": -0.25,
            "selected_role": "主讓",
            "t5_odds": 1.50,
            "passes_wilson_price": False,
            "expected_record_type": "observation",
            "formal_row_count": 0,
            "formal_row_ids": [],
            "formal_statuses": [],
            "matching_record_count": 0,
            "missing_expected_record": True,
            "result_known": not pending,
            "result_source": None if pending else "prediction_history",
            "result_status": "POSTPONED" if pending else "SETTLED",
            "score": (
                None if pending else "1-0" if index in hit_indexes else "0-1"
            ),
            "hdc_grade": grade,
        }
        proof = {
            key: row.get(key) for key in (
                "match_id", "league", "home", "away", "market",
                "selected_side", "selected_line",
                "selected_role", "t5_odds", "t5_recorded_at", "kickoff_hkt",
                "stage_path", "role_path", "selected_line_path", "score", "hdc_grade",
                "result_known", "result_source", "result_status",
            )
        }
        row["replay_candidate_hash"] = canonical_hash(proof)
        rows.append(row)
    return {
        "schema": "crown_condition_read_only_replay_v1",
        "read_only": True,
        "provider_calls": 0,
        "writes": 0,
        "generated_at": "2026-08-28T09:00:00+08:00",
        "condition_number": 4,
        "condition_signature": signature,
        "activation_boundary_hkt": frozen["active_evidence"]["activation_boundary_at"],
        "definition": copy.deepcopy(frozen["definition"]),
        "minimum_acceptable_odds_raw": frozen["active_evidence"][
            "minimum_acceptable_odds_raw"
        ],
        "history_source_rows": 400,
        "learning_result_rows": 39,
        "excluded_matching_before_activation": 62,
        "v2_duplicate_audit": {
            "stored_v2_fixture_identities_available": False,
            "stored_v2_cumulative_decided": 81,
            "stored_v2_cumulative_hits": 52,
            "reconstructed_pre_boundary_fixture_count": 62,
            "reconstructed_pre_boundary_decided": 62,
            "reconstructed_pre_boundary_hits": 41,
            "reconstructed_pre_boundary_duplicate_fixture_ids": [],
            "post_boundary_duplicate_fixture_ids": [],
            "cross_boundary_duplicate_fixture_ids": [],
        },
        "summary": {
            "matching_fixture_count": 40,
            "wilson_price_pass_fixture_count": 0,
            "low_price_observation_fixture_count": 40,
            "recorded_expected_fixture_count": 0,
            "missing_expected_record_fixture_count": 40,
            "unknown_result_fixture_count": 1,
            "recorded_unknown_result_fixture_count": 0,
        },
        "matching_fixtures": rows,
        "missing_formal_fixtures": copy.deepcopy(rows),
        "unknown_result_fixtures": [copy.deepcopy(rows[-1])],
    }


def rehash_candidate(row: dict) -> None:
    row["replay_candidate_hash"] = canonical_hash({
        key: row.get(key) for key in (
            "match_id", "league", "home", "away", "market",
            "selected_side", "selected_line", "selected_role", "t5_odds",
            "t5_recorded_at", "kickoff_hkt", "stage_path", "role_path",
            "selected_line_path", "score", "hdc_grade", "result_known",
            "result_source", "result_status",
        )
    })


def sync_replay_projections(replay: dict) -> None:
    replay["missing_formal_fixtures"] = copy.deepcopy(replay["matching_fixtures"])
    replay["unknown_result_fixtures"] = [
        copy.deepcopy(row) for row in replay["matching_fixtures"]
        if row.get("result_known") is False
    ]


def external_authority(
    ledger: dict, replay: dict, *, apply: bool = False,
    private: Ed25519PrivateKey | None = None,
) -> tuple[dict, str]:
    private = private or Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    pending = replay["matching_fixtures"][-1]
    payload = {
        "schema": AUTHORITY_PAYLOAD_SCHEMA,
        "context": AUTHORITY_CONTEXT,
        "nonce": "review-authority-nonce-" + "a" * 32,
        "expires_at": "2099-01-01T00:00:00+00:00",
        "ledger_sha256": canonical_hash(ledger),
        "replay_sha256": canonical_hash(replay),
        "ordered_candidate_hashes": [
            row["replay_candidate_hash"] for row in replay["matching_fixtures"]
        ],
        "pending_proof": {
            "match_id": pending["match_id"],
            "market": pending["market"],
            "league": pending["league"],
            "home": pending["home"],
            "away": pending["away"],
            "kickoff_hkt": pending["kickoff_hkt"],
            "result_status": "POSTPONED",
            "score": None,
            "reason": "adverse_weather",
            "adverse_weather": True,
            "source": "independent-official-postponement-evidence",
            "source_sha256": "a" * 64,
            "evidence_sha256": "b" * 64,
        },
        "expected": copy.deepcopy(EXPECTED),
        "proposed_ledger_sha256": None,
        "deletions_authorized": False,
        "apply_authorized": apply,
    }
    _report, proposed = _plan_with_payload(
        ledger, replay, authority_payload=payload, enforce_proposed_hash=False,
    )
    payload["proposed_ledger_sha256"] = canonical_hash(proposed)
    signature = private.sign(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode())
    return {
        "schema": AUTHORITY_SCHEMA,
        "payload": payload,
        "public_key_base64": base64.b64encode(public).decode(),
        "signature_base64": base64.b64encode(signature).decode(),
    }, bytes_hash(public)


def approved_plan(ledger: dict, replay: dict, *, apply: bool = False):
    authority, pin = external_authority(ledger, replay, apply=apply)
    return plan_recovery(
        ledger, replay, authority=authority, trusted_public_key_sha256=pin,
        require_apply=apply,
    )


class CrownCondition4RecoveryTest(unittest.TestCase):
    def test_dry_run_exact_totals_rollover_and_pending(self) -> None:
        ledger = ledger_fixture()
        replay = replay_fixture(ledger)
        before = copy.deepcopy(ledger)
        report, proposed = approved_plan(ledger, replay)
        self.assertEqual(ledger, before)
        self.assertEqual(report["changes"]["added"], 40)
        self.assertEqual(report["changes"]["settled"], 39)
        self.assertEqual(report["changes"]["hits"], 21)
        self.assertEqual(report["changes"]["pending"], 1)
        self.assertEqual(
            report["final_cohort"],
            {"observations": 121, "decided": 120, "hits": 73, "pending": 1},
        )
        self.assertEqual(report["rollover"]["first_20"], {
            "hits": 9, "decided": 20, "sealed": True,
        })
        self.assertEqual(
            report["rollover"]["active_cumulative"], {"hits": 61, "decided": 101},
        )
        self.assertEqual(report["rollover"]["tail"], {
            "hits": 12, "decided": 19, "pending_fixtures": 1, "sealed": False,
        })
        pending = [
            row for row in proposed["wilson_validation"]["observations"]
            if row["status"] == "PENDING"
        ]
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            pending[0]["pending_reason"],
            "externally_proved_adverse_weather_postponement",
        )
        self.assertEqual(
            pending[0]["postponement_proof"]["evidence_sha256"], "b" * 64,
        )
        self.assertNotIn("result", pending[0])
        self.assertTrue(all(not action["deleted"] for action in report["actions"]))
        manifest = build_manifest(proposed, "crown")
        self.assertTrue(manifest["valid"], manifest)

    def test_rerun_is_exact_idempotent_skip(self) -> None:
        ledger = ledger_fixture()
        replay = replay_fixture(ledger)
        first, proposed = approved_plan(ledger, replay)
        second, repeated = approved_plan(proposed, replay)
        self.assertEqual(repeated, proposed)
        self.assertEqual(second["changes"]["skipped_exact"], 40)
        self.assertNotIn("added", second["changes"])
        self.assertEqual(second["proposed_ledger_sha256"], canonical_hash(proposed))
        self.assertNotEqual(first["plan_sha256"], second["plan_sha256"])

    def test_exact_duplicate_conflict_rejects_and_partial_apply_blocks(self) -> None:
        ledger = ledger_fixture()
        replay = replay_fixture(ledger)
        conflict = copy.deepcopy(ledger)
        conflict["wilson_validation"].setdefault("observations", []).append({
            "match_id": replay["matching_fixtures"][0]["match_id"],
            "market": "HDC",
            "formal_bet": False,
            "frozen_condition_signature": replay["condition_signature"],
            "stage": "T-5",
            "bet_status": "NO_BET_LOW_ODDS",
            "side": "A",
            "line": 0.25,
            "odds": 1.50,
        })
        authority, _pin = external_authority(ledger, replay)
        negative_payload = copy.deepcopy(authority["payload"])
        negative_payload["ledger_sha256"] = canonical_hash(conflict)
        with self.assertRaisesRegex(
            RecoveryBlocked,
            "input_ledger_strict_manifest_invalid|exact_fixture_market_conflict",
        ):
            _plan_with_payload(
                conflict, replay, authority_payload=negative_payload,
                enforce_proposed_hash=False,
            )

        _report, complete = approved_plan(ledger, replay)
        partial = ledger_fixture()
        partial["wilson_validation"]["observations"] = [
            copy.deepcopy(complete["wilson_validation"]["observations"][0])
        ]
        partial_payload = copy.deepcopy(authority["payload"])
        partial_payload["ledger_sha256"] = canonical_hash(partial)
        with self.assertRaisesRegex(
            RecoveryBlocked,
            "input_ledger_strict_manifest_invalid|partial_existing",
        ):
            _plan_with_payload(
                partial, replay, authority_payload=partial_payload,
                enforce_proposed_hash=False,
            )

    def test_duplicate_audits_and_candidate_hash_fail_closed(self) -> None:
        ledger = ledger_fixture()
        for mutation in ("legacy", "new", "cross", "candidate"):
            replay = replay_fixture(ledger)
            if mutation == "legacy":
                replay["v2_duplicate_audit"][
                    "reconstructed_pre_boundary_duplicate_fixture_ids"
                ] = ["old"]
            elif mutation == "new":
                replay["v2_duplicate_audit"][
                    "post_boundary_duplicate_fixture_ids"
                ] = ["new"]
            elif mutation == "cross":
                replay["v2_duplicate_audit"][
                    "cross_boundary_duplicate_fixture_ids"
                ] = ["cross"]
            else:
                replay["matching_fixtures"][0]["selected_line"] = -0.5
            with self.subTest(mutation):
                with self.assertRaises(RecoveryBlocked):
                    payload = external_authority(
                        ledger, replay_fixture(ledger),
                    )[0]["payload"]
                    payload["replay_sha256"] = canonical_hash(replay)
                    _plan_with_payload(
                        ledger, replay, authority_payload=payload,
                        enforce_proposed_hash=False,
                    )

    def test_legacy_19_identity_aggregate_is_preserved_never_deleted(self) -> None:
        ledger = ledger_fixture()
        replay = replay_fixture(ledger)
        signature = replay["condition_signature"]
        legacy_v2 = copy.deepcopy(
            ledger["wilson_validation"]["conditions"][signature][
                "evidence_versions"
            ][1]
        )
        report, proposed = approved_plan(ledger, replay)
        after_v2 = proposed["wilson_validation"]["conditions"][signature][
            "evidence_versions"
        ][1]
        self.assertEqual(after_v2, legacy_v2)
        self.assertEqual(after_v2["batch_fixture_market_hashes"], [])
        self.assertTrue(
            after_v2["batch_fixture_market_ids_unavailable_from_legacy_aggregate"]
        )
        self.assertTrue(report["safety"]["legacy_19_preserved"])
        self.assertEqual(report["safety"]["delete_count"], 0)

    def test_authority_is_hash_bound_and_must_be_explicitly_authorized(self) -> None:
        ledger = ledger_fixture()
        replay = replay_fixture(ledger)
        authority, pin = external_authority(ledger, replay, apply=False)
        payload = verify_external_authority(
            authority, trusted_public_key_sha256=pin,
            ledger_sha256=canonical_hash(ledger),
            replay_sha256=canonical_hash(replay), require_apply=False,
        )
        self.assertEqual(payload["expected"], EXPECTED)
        with self.assertRaises(RecoveryBlocked):
            verify_external_authority(
                authority, trusted_public_key_sha256="0" * 64,
                ledger_sha256=canonical_hash(ledger),
                replay_sha256=canonical_hash(replay), require_apply=False,
            )
        with self.assertRaises(RecoveryBlocked):
            verify_external_authority(
                authority, trusted_public_key_sha256=pin,
                ledger_sha256=canonical_hash(ledger),
                replay_sha256=canonical_hash(replay), require_apply=True,
            )
        tampered = copy.deepcopy(authority)
        tampered["payload"]["ordered_candidate_hashes"].reverse()
        with self.assertRaises(RecoveryBlocked):
            verify_external_authority(
                tampered, trusted_public_key_sha256=pin,
                ledger_sha256=canonical_hash(ledger),
                replay_sha256=canonical_hash(replay), require_apply=False,
            )

    def test_cli_removed_self_author_and_never_overwrites_or_aliases(self) -> None:
        ledger = ledger_fixture()
        replay = replay_fixture(ledger)
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            ledger_path = work / "ledger.json"
            replay_path = work / "replay.json"
            authority_path = work / "authority.json"
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            replay_path.write_text(json.dumps(replay), encoding="utf-8")
            authority_path.write_text("{}", encoding="utf-8")
            before = ledger_path.read_bytes()
            replay_before = replay_path.read_bytes()
            base = [
                sys.executable, "-m", "analysis.crown_condition4_recovery",
                "--ledger", str(ledger_path), "--replay", str(replay_path),
                "--authority", str(authority_path),
            ]
            removed = subprocess.run(
                [
                    *base, "--authorize", "--reviewed-by", "self",
                ],
                cwd=root, capture_output=True, text=True,
            )
            self.assertNotEqual(removed.returncode, 0)
            self.assertEqual(ledger_path.read_bytes(), before)
            for target in (
                ledger_path, replay_path, work / "looks-like-production-ledger.json",
            ):
                if not target.exists():
                    target.write_text("DO NOT OVERWRITE", encoding="utf-8")
                target_before = target.read_bytes()
                rejected = subprocess.run(
                    [*base, "--apply-to-copy", str(target)],
                    cwd=root, capture_output=True, text=True,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(target.read_bytes(), target_before)
            self.assertEqual(ledger_path.read_bytes(), before)
            self.assertEqual(replay_path.read_bytes(), replay_before)

            new_output = work / "safe-proposed-copy.json"
            fd = _create_output(new_output)
            try:
                _write_retained(fd, {"safe": True})
            finally:
                __import__("os").close(fd)
            self.assertEqual(
                json.loads(new_output.read_text(encoding="utf-8")), {"safe": True},
            )
            with self.assertRaises(RecoveryBlocked):
                _preflight_output(new_output, [], [])

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(RecoveryBlocked, "duplicate_json_key"):
            _strict_json_bytes(b'{"schema":"one","schema":"two"}', "authority")

    def test_cli_with_external_root_pin_writes_only_new_proposed_copy(self) -> None:
        ledger = ledger_fixture()
        replay = replay_fixture(ledger)
        authority, pin = external_authority(ledger, replay, apply=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "captured-crown-ledger.json"
            replay_path = root / "captured-crown-replay.json"
            authority_path = root / "external-authority.json"
            output_path = root / "crown-ledger.proposed.json"
            report_path = root / "recovery-report.json"
            canonical = lambda value: json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            ledger_path.write_text(canonical(ledger), encoding="utf-8")
            replay_path.write_text(canonical(replay), encoding="utf-8")
            authority_path.write_text(canonical(authority), encoding="utf-8")
            before = [path.read_bytes() for path in (
                ledger_path, replay_path, authority_path,
            )]
            argv = [
                "crown_condition4_recovery", "--ledger", str(ledger_path),
                "--replay", str(replay_path), "--authority", str(authority_path),
                "--report", str(report_path), "--apply-to-copy", str(output_path),
            ]
            with (
                patch("sys.argv", argv),
                patch(
                    "analysis.crown_condition4_recovery._trusted_key_pin",
                    return_value=pin,
                ),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(recovery_main(), 0)
            self.assertTrue(output_path.is_file())
            self.assertTrue(report_path.is_file())
            self.assertEqual(
                [path.read_bytes() for path in (
                    ledger_path, replay_path, authority_path,
                )],
                before,
            )
            with (
                patch("sys.argv", argv),
                patch(
                    "analysis.crown_condition4_recovery._trusted_key_pin",
                    return_value=pin,
                ),
                redirect_stdout(StringIO()),
                self.assertRaises(RecoveryBlocked),
            ):
                recovery_main()

    def test_all_output_alias_symlink_hardlink_existing_and_production_paths_reject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = [root / name for name in ("ledger-input", "replay-input", "authority-input")]
            for index, path in enumerate(inputs):
                path.write_text(str(index), encoding="utf-8")
            stats = [os.stat(path, follow_symlinks=False) for path in inputs]
            existing = root / "existing-output"
            existing.write_text("preserve", encoding="utf-8")
            symlink = root / "symlink-output"
            symlink.symlink_to(inputs[0])
            hardlink = root / "hardlink-output"
            os.link(inputs[1], hardlink)
            production_like = root / "looks-like-production-ledger.json"
            for target in [*inputs, existing, symlink, hardlink, production_like]:
                with self.subTest(target=target.name):
                    with self.assertRaises(RecoveryBlocked):
                        _preflight_output(target, inputs, stats)
            self.assertEqual(existing.read_text(encoding="utf-8"), "preserve")
            self.assertEqual(inputs[0].read_text(encoding="utf-8"), "0")
            self.assertEqual(inputs[1].read_text(encoding="utf-8"), "1")
            safe = root / "crown-ledger.proposed.json"
            _preflight_output(safe, inputs, stats)
            fd = _create_output(safe)
            try:
                _write_retained(fd, {"new": True})
            finally:
                os.close(fd)
            self.assertEqual(json.loads(safe.read_text()), {"new": True})

    def test_reviewer_schema_extra_key_attacks_all_fail_closed(self) -> None:
        for location in ("top", "summary", "audit", "candidate", "grade", "authority"):
            ledger = ledger_fixture()
            replay = replay_fixture(ledger)
            authority, pin = external_authority(ledger, replay)
            if location == "top":
                replay["UNKNOWN"] = True
            elif location == "summary":
                replay["summary"]["UNKNOWN"] = True
            elif location == "audit":
                replay["v2_duplicate_audit"]["UNKNOWN"] = True
            elif location == "candidate":
                replay["matching_fixtures"][0]["UNKNOWN"] = True
            elif location == "grade":
                replay["matching_fixtures"][0]["hdc_grade"]["UNKNOWN"] = True
                rehash_candidate(replay["matching_fixtures"][0])
            else:
                authority["UNKNOWN"] = True
            with self.subTest(location):
                with self.assertRaises(RecoveryBlocked):
                    plan_recovery(
                        ledger, replay, authority=authority,
                        trusted_public_key_sha256=pin,
                    )

    def test_reviewer_score_grade_and_pending_attacks_fail_closed(self) -> None:
        ledger = ledger_fixture()
        for attack in ("grade", "pending_score", "move_pending"):
            replay = replay_fixture(ledger)
            authority, pin = external_authority(ledger, replay)
            if attack == "grade":
                row = replay["matching_fixtures"][0]
                row["hdc_grade"]["hit"] = False
                row["hdc_grade"]["result"] = "Lost"
                rehash_candidate(row)
            elif attack == "pending_score":
                row = replay["matching_fixtures"][-1]
                row["score"] = "9-0"
                rehash_candidate(row)
            else:
                first, pending = (
                    replay["matching_fixtures"][0],
                    replay["matching_fixtures"][-1],
                )
                first.update({
                    "home": pending["home"], "away": pending["away"],
                    "hdc_grade": None, "result_known": False,
                    "result_source": None, "result_status": "POSTPONED",
                    "score": None,
                })
                pending.update({
                    "home": "Replacement Home", "away": "Replacement Away",
                    "hdc_grade": {"grade_status": "GRADED", "hit": True, "result": "Won"},
                    "result_known": True, "result_source": "prediction_history",
                    "result_status": "SETTLED", "score": "1-0",
                })
                rehash_candidate(first)
                rehash_candidate(pending)
            sync_replay_projections(replay)
            with self.subTest(attack):
                with self.assertRaises(RecoveryBlocked):
                    plan_recovery(
                        ledger, replay, authority=authority,
                        trusted_public_key_sha256=pin,
                    )

    def test_reviewer_manifest_skip_and_extra_row_attacks_fail_closed(self) -> None:
        ledger = ledger_fixture()
        replay = replay_fixture(ledger)
        invalid = copy.deepcopy(ledger)
        invalid["wilson_validation"]["condition_order"].append(
            invalid["wilson_validation"]["condition_order"][0],
        )
        authority, _pin = external_authority(ledger, replay)
        payload = copy.deepcopy(authority["payload"])
        payload["ledger_sha256"] = canonical_hash(invalid)
        with self.assertRaisesRegex(RecoveryBlocked, "input_ledger_strict_manifest"):
            _plan_with_payload(
                invalid, replay, authority_payload=payload,
                enforce_proposed_hash=False,
            )

        _report, proposed = approved_plan(ledger, replay)
        tampered = copy.deepcopy(proposed)
        tampered["wilson_validation"]["observations"][0][
            "recovered_missing_observation"
        ]["replay_sha256"] = "0" * 64
        with self.assertRaises(RecoveryBlocked):
            approved_plan(tampered, replay)

        extra = copy.deepcopy(proposed)
        row = copy.deepcopy(extra["wilson_validation"]["observations"][0])
        row["match_id"] = "unrelated-extra-41st"
        row["observation_id"] = "invalid-extra-id"
        extra["wilson_validation"]["observations"].append(row)
        with self.assertRaises(RecoveryBlocked):
            approved_plan(extra, replay)

    def test_reviewer_timestamp_reordering_cannot_change_signed_sequence(self) -> None:
        ledger = ledger_fixture()
        replay = replay_fixture(ledger)
        authority, pin = external_authority(ledger, replay)
        first, last = replay["matching_fixtures"][0], replay["matching_fixtures"][39]
        first["t5_recorded_at"], last["t5_recorded_at"] = (
            last["t5_recorded_at"], first["t5_recorded_at"],
        )
        first["kickoff_hkt"], last["kickoff_hkt"] = (
            last["kickoff_hkt"], first["kickoff_hkt"],
        )
        rehash_candidate(first)
        rehash_candidate(last)
        sync_replay_projections(replay)
        with self.assertRaises(RecoveryBlocked):
            plan_recovery(
                ledger, replay, authority=authority,
                trusted_public_key_sha256=pin,
            )

    def test_rereview_exact_row_extra_and_selected_role_rehash_reject(self) -> None:
        ledger = ledger_fixture()
        replay = replay_fixture(ledger)
        _report, proposed = approved_plan(ledger, replay)
        for attack in ("extra", "selected_role"):
            tampered = copy.deepcopy(proposed)
            row = tampered["wilson_validation"]["observations"][0]
            if attack == "extra":
                row["attacker_extra"] = "accepted?"
            else:
                row["selected_role"] = "客受"
            # Reproduce the old bypass: the attacker authors a replacement
            # unkeyed self-hash. The v3 schema has no such field, so this is
            # itself an exact-schema violation and cannot authenticate the row.
            row["recovered_missing_observation"]["row_payload_sha256"] = (
                canonical_hash({
                    key: value for key, value in row.items()
                    if key != "recovered_missing_observation"
                })
            )
            with self.subTest(attack):
                with self.assertRaises(RecoveryBlocked):
                    approved_plan(tampered, replay)

    def test_rereview_transaction_cleanup_on_second_create_write_and_fsync_failure(self) -> None:
        failures = ("second_create", "mid_write", "fsync")
        for failure in failures:
            with self.subTest(failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                report = root / "report.json"
                proposal = root / "crown-ledger.proposed.json"
                real_create = recovery._create_output
                real_write = recovery._write_retained
                calls = 0

                def create(path):
                    nonlocal calls
                    calls += 1
                    if failure == "second_create" and calls == 2:
                        raise FileExistsError("simulated second create failure")
                    return real_create(path)

                def write(descriptor, payload):
                    if failure == "mid_write":
                        os.write(descriptor, b'{"truncated":')
                        raise OSError("simulated mid-write failure")
                    return real_write(descriptor, payload)

                patches = [
                    patch.object(recovery, "_create_output", side_effect=create),
                    patch.object(recovery, "_write_retained", side_effect=write),
                ]
                if failure == "fsync":
                    patches.append(patch.object(
                        recovery.os, "fsync",
                        side_effect=OSError("simulated fsync failure"),
                    ))
                with patches[0], patches[1]:
                    context = patches[2] if len(patches) == 3 else __import__(
                        "contextlib",
                    ).nullcontext()
                    with context, self.assertRaises(Exception):
                        recovery._publish_outputs_transactionally(
                            [(report, {"report": True}), (proposal, {"proposal": True})],
                            proposal_path=proposal,
                        )
                self.assertFalse(report.exists())
                self.assertFalse(proposal.exists())
                self.assertEqual(
                    list(root.glob(".*.crown4-stage-*")), [],
                )

    def test_rereview_canonical_dotdot_output_alias_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            aliased = root / "nested" / ".." / "report.json"
            canonical = root / "report.json"
            with self.assertRaisesRegex(RecoveryBlocked, "parent_alias"):
                _preflight_output(aliased, [], [])
            self.assertEqual(
                canonical.resolve(strict=False),
                aliased.resolve(strict=False),
            )

    def test_final_review_same_bytes_different_inode_rejects_and_preserves_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final = root / "crown-ledger.proposed.json"
            real_verify = recovery._verify_final
            replacement = {}

            def swap_before_verify(stage, *, expected_nlink):
                if not replacement:
                    staged = os.fstat(stage.descriptor)
                    os.unlink(
                        stage.target.path.name,
                        dir_fd=stage.target.parent.descriptor,
                    )
                    descriptor = os.open(
                        stage.target.path.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600, dir_fd=stage.target.parent.descriptor,
                    )
                    try:
                        os.write(descriptor, stage.raw)
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    current = os.stat(
                        stage.target.path.name,
                        dir_fd=stage.target.parent.descriptor,
                        follow_symlinks=False,
                    )
                    replacement.update({
                        "stage_inode": staged.st_ino,
                        "replacement_inode": current.st_ino,
                    })
                return real_verify(stage, expected_nlink=expected_nlink)

            with (
                patch.object(recovery, "_verify_final", side_effect=swap_before_verify),
                self.assertRaises(recovery.PublicationFailure) as raised,
            ):
                recovery._publish_outputs_transactionally(
                    [(final, {"proposal": True})], proposal_path=final,
                )
            self.assertNotEqual(
                replacement["stage_inode"], replacement["replacement_inode"],
            )
            self.assertTrue(final.is_file())
            self.assertEqual(json.loads(final.read_text()), {"proposal": True})
            self.assertIn(
                "published_output_identity_or_readback_mismatch",
                raised.exception.original_error,
            )
            self.assertTrue(any(
                "replacement_preserved" in item
                for item in raised.exception.cleanup_failures
            ))
            self.assertEqual(list(root.glob(".*.crown4-stage-*")), [])

    def test_final_review_first_final_unlink_error_continues_and_preserves_original_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            proposal = root / "crown-ledger.proposed.json"
            real_verify = recovery._verify_final
            real_unlink = os.unlink
            verify_calls = 0
            final_unlink_failed = False

            def fail_after_second_publish(stage, *, expected_nlink):
                nonlocal verify_calls
                verify_calls += 1
                real_verify(stage, expected_nlink=expected_nlink)
                if verify_calls == 2:
                    raise OSError("original publish verification failure")

            def fail_first_final(name, *args, **kwargs):
                nonlocal final_unlink_failed
                if (
                    not final_unlink_failed
                    and "crown4-stage" not in str(name)
                ):
                    final_unlink_failed = True
                    raise PermissionError("first final unlink denied")
                return real_unlink(name, *args, **kwargs)

            with (
                patch.object(recovery, "_verify_final", side_effect=fail_after_second_publish),
                patch.object(recovery.os, "unlink", side_effect=fail_first_final),
                self.assertRaises(recovery.PublicationFailure) as raised,
            ):
                recovery._publish_outputs_transactionally(
                    [(report, {"r": 1}), (proposal, {"p": 1})],
                    proposal_path=proposal,
                )
            self.assertIn(
                "original publish verification failure",
                raised.exception.original_error,
            )
            self.assertTrue(any(
                "first final unlink denied" in item
                for item in raised.exception.cleanup_failures
            ))
            # Cleanup continued: one denied final remains, the other final and
            # both stages were still processed.
            self.assertEqual(
                len([path for path in (report, proposal) if path.exists()]), 1,
            )
            self.assertEqual(list(root.glob(".*.crown4-stage-*")), [])
            self.assertTrue(raised.exception.residue)

    def test_final_review_stage_unlink_error_reports_residue_and_cleans_finals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            proposal = root / "crown-ledger.proposed.json"
            real_unlink = os.unlink

            def deny_stage(name, *args, **kwargs):
                if "crown4-stage" in str(name):
                    raise PermissionError("stage unlink denied")
                return real_unlink(name, *args, **kwargs)

            with (
                patch.object(recovery.os, "unlink", side_effect=deny_stage),
                self.assertRaises(recovery.PublicationFailure) as raised,
            ):
                recovery._publish_outputs_transactionally(
                    [(report, {"r": 1}), (proposal, {"p": 1})],
                    proposal_path=proposal,
                )
            self.assertFalse(report.exists())
            self.assertFalse(proposal.exists())
            self.assertEqual(len(list(root.glob(".*.crown4-stage-*"))), 2)
            self.assertIn(
                "stage_commit_cleanup_failed",
                raised.exception.original_error,
            )
            self.assertTrue(any(
                "stage unlink denied" in item
                for item in raised.exception.cleanup_failures
            ))
            self.assertTrue(raised.exception.residue)

    def test_final_review_output_parent_owner_mode_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_mode = stat.S_IMODE(root.stat().st_mode)
            try:
                os.chmod(root, 0o777)
                with self.assertRaisesRegex(RecoveryBlocked, "unsafe_output_parent"):
                    recovery._publish_outputs_transactionally(
                        [(root / "report.json", {"r": 1})],
                        proposal_path=None,
                    )
                self.assertEqual(list(root.iterdir()), [])
            finally:
                os.chmod(root, original_mode)


if __name__ == "__main__":
    unittest.main()
