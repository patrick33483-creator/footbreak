from __future__ import annotations

import copy
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analysis import wilson_validation as wv
from analysis.migrate_condition_identity_retirement import (
    CONFIRMATION,
    _checked_out_commit,
    migrate_file,
)
from analysis.wilson_registry_manifest import build_manifest

NOW = "2026-08-20T00:00:00+08:00"
EFFECTIVE = "2026-08-27T09:00:00+08:00"


def _candidate(index: int) -> dict:
    market = ("HDC", "HIL", "CHL")[(index - 1) % 3]
    role = f"role-{index}"
    bucket = f"bucket-{index}"
    key = [
        "system=footbreak", f"market={market}", "path=T-5", "decision=T-5",
        "tier=≥1.70", "direction=A", f"role={role}", f"bucket={bucket}",
        "movement=不變",
    ]
    return {
        "version": "granular-condition-v1",
        "key": key,
        "market": market,
        "stage": "T-5",
        "path": "T-5",
        "direction": "A",
        "role": role,
        "line_bucket": bucket,
        "odds_tier": "≥1.70",
        "movement": "不變",
        "odds_trajectory": "",
    }


def _version(signature: str, index: int) -> dict:
    values = wv._evidence_values(50, 80)
    row = {
        "condition_signature": signature,
        "version": 1,
        "prior_version": None,
        "prior_evidence_hash": None,
        "batch_fixture_market_hashes": [],
        "batch_hits": 0,
        "batch_decided": 0,
        "cumulative_hits": 50,
        "cumulative_decided": 80,
        "wilson95_lower_raw": values["wilson95_lower_raw"],
        "minimum_acceptable_odds_raw": values["minimum_acceptable_odds_raw"],
        "minimum_acceptable_odds_display": values[
            "display"
        ][
            "minimum_acceptable_odds"
        ],
        "activation_boundary_at": NOW,
        "created_at": NOW,
        "migration_baseline": True,
    }
    row["evidence_hash"] = wv._version_hash(row)
    return row


def _frozen(definition: dict, number: int) -> tuple[str, dict]:
    signature = wv._canonical_hash(definition)[:24]
    version = _version(signature, number)
    active = {
        key: copy.deepcopy(version.get(key)) for key in (
            "version", "cumulative_hits", "cumulative_decided",
            "wilson95_lower_raw", "minimum_acceptable_odds_raw",
            "minimum_acceptable_odds_display", "activation_boundary_at",
            "created_at", "evidence_hash",
        )
    }
    return signature, {
        "signature": signature,
        "condition_number": number,
        "frozen_at": NOW,
        "definition": copy.deepcopy(definition),
        "historical_evidence": {
            "hits": 50,
            "decided": 80,
            "pushes": 0,
            "artifact": {
                "hash": f"{number:064x}",
                "version": "fixture-v1",
                "as_of": NOW,
            },
        },
        "evidence_versions": [version],
        "active_evidence_version": 1,
        "active_evidence_hash": version["evidence_hash"],
        "active_evidence": active,
        "prospective": {"sentinel": number},
        "pending_rollover_progress": {
            "eligible_decided": 0,
            "eligible_hits": 0,
            "accuracy": None,
            "required": 20,
            "display": "0/20",
            "excluded": {
                "missing_or_invalid_provenance": 0,
                "before_snapshot_boundary": 0,
                "not_binary_decided": 0,
                "duplicate_or_conflicting_fixture_market": 0,
            },
        },
    }


def retirement_fixture() -> tuple[dict, tuple[dict, dict], dict]:
    definitions = [
        wv.condition_definition("footbreak", _candidate(i)) for i in range(1, 18)
    ]
    # Sources retain their original digest while their miner keys canonicalize
    # byte-for-byte to the already-existing #7 and #14 definitions.
    source1 = copy.deepcopy(definitions[0])
    source1.update(direction="H", role="", line_bucket="", movement="")
    source2 = copy.deepcopy(definitions[1])
    source2.update(direction="H", role="", line_bucket="", movement="")
    definitions[0] = source1
    definitions[1] = source2
    definitions[6] = wv.condition_definition(
        "footbreak", {**source1, "key": source1["miner_key"]},
    )
    definitions[13] = wv.condition_definition(
        "footbreak", {**source2, "key": source2["miner_key"]},
    )
    items = [_frozen(definition, number) for number, definition in enumerate(
        definitions, start=1,
    )]
    order = [signature for signature, _frozen_row in items]
    ledger = {
        "bets": [],
        wv.NAMESPACE: {
            "schema_version": wv.SCHEMA_VERSION,
            "system": "footbreak",
            "activation_at": NOW,
            "condition_order": order,
            "conditions": dict(items),
            "observations": [],
            "audit": [],
        },
    }
    for source, count in ((order[0], 11), (order[1], 4)):
        ledger["bets"].extend({
            "frozen_condition_signature": source,
            "created_at": "2026-08-26T00:00:00+08:00",
            "historical_row": index,
        } for index in range(count))
    ns = ledger[wv.NAMESPACE]
    production, _validated, reason = wv._expected_production_identity_manifest(
        ns, "footbreak",
    )
    assert reason is None and production is not None
    ns["production_identity_manifest"] = production
    allowed = []
    entries = []
    for source_number, target_number in ((1, 7), (2, 14)):
        source = order[source_number - 1]
        target = order[target_number - 1]
        source_frozen = ns["conditions"][source]
        target_frozen = ns["conditions"][target]
        allowed_row = {
            "source_condition_number": source_number,
            "source_signature": source,
            "source_definition_hash": wv._canonical_hash(
                source_frozen["definition"],
            ),
            "source_initial_evidence_hash": source_frozen[
                "evidence_versions"
            ][0]["evidence_hash"],
            "target_condition_number": target_number,
            "target_signature": target,
            "target_definition_hash": wv._canonical_hash(
                target_frozen["definition"],
            ),
        }
        hashes = wv._historical_activity_hashes(ledger, source)
        activity = {
            "scope": ["bets", "wilson_validation.observations"],
            "row_count": len(hashes),
            "row_hashes": hashes,
            "rows_are_evidence": False,
        }
        activity["root_hash"] = wv._migration_activity_root(activity)
        allowed.append(allowed_row)
        entries.append({
            **allowed_row,
            "relation": "retired_duplicate_of",
            "canonicalization": "condition_definition_from_source_miner_key",
            "historical_activity": activity,
            "future_admission": "target_only",
            "evidence_merge": "none",
        })
    body = {
        "schema_version": 1,
        "migration_version": wv.CONDITION_IDENTITY_MIGRATION_VERSION,
        "system": "footbreak",
        "immutable": True,
        "effective_at": EFFECTIVE,
        "authority": {
            "kind": "reviewed-manifest-sha256",
            "release_commit": _checked_out_commit(),
            "production_identity_manifest_hash": production["manifest_hash"],
        },
        "entries": entries,
    }
    document = {**body, "manifest_hash": wv._canonical_hash(body)}
    return ledger, tuple(allowed), document


def _reseal_source_activity(
    ledger: dict, document: dict, source_signature: str,
) -> dict:
    updated = copy.deepcopy(document)
    for entry in updated["entries"]:
        if entry["source_signature"] != source_signature:
            continue
        hashes = wv._historical_activity_hashes(ledger, source_signature)
        activity = {
            "scope": ["bets", "wilson_validation.observations"],
            "row_count": len(hashes),
            "row_hashes": hashes,
            "rows_are_evidence": False,
        }
        activity["root_hash"] = wv._migration_activity_root(activity)
        entry["historical_activity"] = activity
    body = {key: value for key, value in updated.items() if key != "manifest_hash"}
    updated["manifest_hash"] = wv._canonical_hash(body)
    return updated


class ConditionIdentityRetirementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger, self.allowlist, self.document = retirement_fixture()
        self.patch = patch.object(
            wv, "CONDITION_IDENTITY_MIGRATION_ALLOWLIST", self.allowlist,
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def install(self, ledger: dict | None = None) -> dict:
        value = ledger if ledger is not None else self.ledger
        wv.apply_condition_identity_migration(
            value, "footbreak", authorized_manifest=self.document,
            expected_release_commit=_checked_out_commit(),
        )
        return value

    def test_plan_is_pure_and_apply_changes_metadata_only(self):
        before = copy.deepcopy(self.ledger)
        plan = wv.plan_condition_identity_migration(
            self.ledger, "footbreak", self.document,
            expected_release_commit=_checked_out_commit(),
        )
        self.assertEqual(self.ledger, before)
        self.assertEqual(plan["historical_condition_count"], 17)
        self.assertEqual(plan["active_condition_count"], 15)
        self.assertNotEqual(
            plan["before_identity_projection_hash"],
            plan["after_identity_projection_hash"],
        )
        self.install()
        installed = self.ledger[wv.NAMESPACE].pop(
            "condition_identity_migrations",
        )
        self.assertEqual(installed, self.document)
        self.assertEqual(self.ledger, before)

    def test_idempotent_target_only_registry_and_no_double_admission(self):
        self.install()
        first_bytes = json.dumps(
            self.ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
        wv.apply_condition_identity_migration(
            self.ledger, "footbreak", trusted_manifest_hash=self.document[
                "manifest_hash"
            ],
        )
        self.assertEqual(first_bytes, json.dumps(
            self.ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode())
        candidates = wv.formal_registry_candidates(
            self.ledger, "footbreak", now=EFFECTIVE,
        )
        signatures = [row["__formal_frozen_signature"] for row in candidates]
        self.assertEqual(len(signatures), 15)
        self.assertNotIn(self.allowlist[0]["source_signature"], signatures)
        self.assertNotIn(self.allowlist[1]["source_signature"], signatures)
        self.assertEqual(signatures.count(self.allowlist[0]["target_signature"]), 1)
        self.assertEqual(signatures.count(self.allowlist[1]["target_signature"]), 1)
        source = self.allowlist[0]["source_signature"]
        frozen = self.ledger[wv.NAMESPACE]["conditions"][source]
        admitted, reason = wv.apply_active_evidence(
            self.ledger, "footbreak", {
                "signature": source,
                "definition": copy.deepcopy(frozen["definition"]),
                "history": copy.deepcopy(frozen["historical_evidence"]),
                "arithmetic": {},
            },
            stage_at=EFFECTIVE,
            now=EFFECTIVE,
        )
        self.assertIsNone(admitted)
        self.assertEqual(reason, "retired_duplicate_target_only")

    def test_recompute_preserves_retired_accumulators_and_funnel_exposes_lineage(self):
        self.install()
        sources = {
            row["source_signature"]: copy.deepcopy(
                self.ledger[wv.NAMESPACE]["conditions"][row["source_signature"]],
            )
            for row in self.allowlist
        }
        wv.recompute_namespace(self.ledger, "footbreak")
        for signature, frozen in sources.items():
            self.assertEqual(
                self.ledger[wv.NAMESPACE]["conditions"][signature], frozen,
            )
        funnel = wv.project_condition_funnel(self.ledger, "footbreak")
        self.assertEqual(
            (funnel["historical_condition_count"], funnel["active_condition_count"],
             funnel["retired_duplicate_count"]),
            (17, 15, 2),
        )
        source_card = funnel["conditions"][0]
        self.assertEqual(source_card["identity_status"], "retired_duplicate")
        self.assertEqual(source_card["future_admission"], "target_only")
        self.assertEqual(
            source_card["canonical_successor_condition_number"], 7,
        )

    def test_strict_manifest_accepts_only_verified_retirements(self):
        self.install()
        manifest = build_manifest(self.ledger, "footbreak")
        self.assertTrue(manifest["valid"], manifest)
        self.assertEqual(
            (manifest["historical_condition_count"],
             manifest["active_condition_count"],
             manifest["retired_duplicate_count"]),
            (17, 15, 2),
        )
        self.assertNotIn(
            "production_signature_roundtrip_failed",
            manifest["conditions"][0]["rejection_reasons"],
        )
        self.assertEqual(
            manifest["conditions"][0]["historical_activity_row_count"], 11,
        )

    def test_tamper_unknown_cycle_activity_and_post_effective_fail_closed(self):
        mutations = {
            "manifest hash": lambda d: d.update(manifest_hash="0" * 64),
            "unknown entry": lambda d: d["entries"][0].update(
                source_signature="f" * 24,
            ),
            "cycle": lambda d: d["entries"][0].update(
                target_signature=d["entries"][0]["source_signature"],
            ),
            "evidence merge": lambda d: d["entries"][0].update(
                evidence_merge="copy",
            ),
            "activity": lambda d: d["entries"][0]["historical_activity"][
                "row_hashes"
            ].append("0" * 64),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                document = copy.deepcopy(self.document)
                mutate(document)
                body = {k: v for k, v in document.items() if k != "manifest_hash"}
                if label != "manifest hash":
                    document["manifest_hash"] = wv._canonical_hash(body)
                with self.assertRaises(ValueError):
                    wv.apply_condition_identity_migration(
                        copy.deepcopy(self.ledger), "footbreak",
                        authorized_manifest=document,
                        expected_release_commit=_checked_out_commit(),
                    )
        installed = self.install(copy.deepcopy(self.ledger))
        installed["bets"][0]["created_at"] = EFFECTIVE
        self.assertEqual(wv.formal_registry_candidates(
            installed, "footbreak",
        ), [])
        with self.assertRaises(ValueError):
            wv.recompute_namespace(installed, "footbreak")
        self.assertFalse(build_manifest(installed, "footbreak")["valid"])

    def test_cli_dry_run_confirmation_locking_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger_path = root / "ledger.json"
            manifest_path = root / "authorized.json"
            lock_path = root / "shared.lock"
            ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")
            manifest_path.write_text(json.dumps(self.document), encoding="utf-8")
            before = ledger_path.read_bytes()
            result = migrate_file(
                ledger_path, manifest_path, lock_path=lock_path,
                apply=False, confirmation=None,
            )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(ledger_path.read_bytes(), before)
            with self.assertRaises(ValueError):
                migrate_file(
                    ledger_path, manifest_path, lock_path=lock_path,
                    apply=True, confirmation="wrong",
                )
            with lock_path.open("a+") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with self.assertRaises(RuntimeError):
                        migrate_file(
                            ledger_path, manifest_path, lock_path=lock_path,
                            apply=False, confirmation=None,
                        )
                finally:
                    fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            with patch(
                "analysis.migrate_condition_identity_retirement.os.replace",
                side_effect=OSError("simulated"),
            ):
                with self.assertRaises(OSError):
                    migrate_file(
                        ledger_path, manifest_path, lock_path=lock_path,
                        apply=True, confirmation=CONFIRMATION,
                    )
            self.assertEqual(ledger_path.read_bytes(), before)
            applied = migrate_file(
                ledger_path, manifest_path, lock_path=lock_path,
                apply=True, confirmation=CONFIRMATION,
            )
            self.assertEqual(applied["status"], "applied")
            self.assertTrue(applied["readback_verified"])

    def test_sealed_rows_require_parseable_strictly_pre_effective_timestamps(self):
        source = self.allowlist[0]["source_signature"]
        mutations = {
            "missing": {"frozen_condition_signature": source, "probe": "missing"},
            "unparseable": {
                "frozen_condition_signature": source,
                "created_at": "not-a-time",
            },
            "at cutoff": {
                "frozen_condition_signature": source,
                "created_at": EFFECTIVE,
            },
            "after cutoff": {
                "frozen_condition_signature": source,
                "created_at": "2026-08-27T09:00:01+08:00",
            },
            "mixed timestamps": {
                "frozen_condition_signature": source,
                "created_at": "2026-08-26T00:00:00+08:00",
                "admission_at": EFFECTIVE,
            },
        }
        for label, row in mutations.items():
            with self.subTest(label):
                ledger = copy.deepcopy(self.ledger)
                ledger["bets"].append(row)
                document = _reseal_source_activity(ledger, self.document, source)
                validated, reason = (
                    wv._validate_condition_identity_migration_document(
                        ledger, "footbreak", document,
                    )
                )
                self.assertIsNone(validated)
                self.assertEqual(
                    reason,
                    "condition_identity_migrations_historical_activity_drift",
                )
                with self.assertRaises(ValueError):
                    wv.apply_condition_identity_migration(
                        ledger, "footbreak", authorized_manifest=document,
                        expected_release_commit=_checked_out_commit(),
                    )

    def test_known_conflicts_without_migration_fail_closed_everywhere(self):
        self.assertEqual(
            wv.formal_registry_candidates(self.ledger, "footbreak"), [],
        )
        funnel = wv.project_condition_funnel(self.ledger, "footbreak")
        self.assertEqual(
            funnel["unavailable_reason"],
            "required_condition_identity_migration_missing",
        )
        before = copy.deepcopy(self.ledger)
        with self.assertRaisesRegex(
            ValueError, "required_condition_identity_migration_missing",
        ):
            wv.recompute_namespace(self.ledger, "footbreak")
        self.assertEqual(self.ledger, before)
        manifest = build_manifest(self.ledger, "footbreak")
        self.assertFalse(manifest["valid"])
        self.assertIn(
            "required_condition_identity_migration_missing",
            manifest["rejection_reasons"],
        )
        self.assertIsNone(manifest["active_condition_count"])
        self.assertIsNone(manifest["retired_duplicate_count"])

    def test_cli_rejects_unrelated_registry_defect_before_replace(self):
        ledger = copy.deepcopy(self.ledger)
        active = ledger[wv.NAMESPACE]["condition_order"][2]
        ledger["bets"].append({
            "frozen_condition_signature": active,
            "created_at": "2026-08-26T00:00:00+08:00",
            "probe": "unverifiable-active-row",
        })
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger_path = root / "ledger.json"
            manifest_path = root / "authorized.json"
            lock_path = root / "shared.lock"
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            manifest_path.write_text(json.dumps(self.document), encoding="utf-8")
            before = ledger_path.read_bytes()
            with self.assertRaisesRegex(
                ValueError, "candidate_registry_manifest_invalid",
            ):
                migrate_file(
                    ledger_path, manifest_path, lock_path=lock_path,
                    apply=True, confirmation=CONFIRMATION,
                )
            self.assertEqual(ledger_path.read_bytes(), before)

    def test_post_replace_failures_return_committed_warnings(self):
        import analysis.migrate_condition_identity_retirement as cli

        for failure in ("directory_fsync", "readback"):
            with self.subTest(failure), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                ledger_path = root / "ledger.json"
                manifest_path = root / "authorized.json"
                lock_path = root / "shared.lock"
                ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")
                manifest_path.write_text(
                    json.dumps(self.document), encoding="utf-8",
                )
                if failure == "directory_fsync":
                    real_fsync = os.fsync
                    calls = {"count": 0}

                    def fail_directory(descriptor):
                        calls["count"] += 1
                        if calls["count"] == 2:
                            raise OSError("simulated directory fsync failure")
                        return real_fsync(descriptor)

                    context = patch.object(
                        cli.os, "fsync", side_effect=fail_directory,
                    )
                else:
                    original_read = cli._read_object
                    calls = {"count": 0}

                    def fail_readback(path, label):
                        calls["count"] += 1
                        if calls["count"] == 3:
                            raise OSError("simulated readback failure")
                        return original_read(path, label)

                    context = patch.object(
                        cli, "_read_object", side_effect=fail_readback,
                    )
                with context:
                    result = migrate_file(
                        ledger_path, manifest_path, lock_path=lock_path,
                        apply=True, confirmation=CONFIRMATION,
                    )
                self.assertEqual(result["status"], "applied")
                self.assertTrue(result["committed"])
                self.assertTrue(result.get("durability_warnings"), result)
                persisted = json.loads(ledger_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    persisted[wv.NAMESPACE]["condition_identity_migrations"],
                    self.document,
                )

    def test_commit_pin_and_trusted_hash_bootstrap_paths(self):
        wrong_commit = "f" * 40
        if wrong_commit == _checked_out_commit():
            wrong_commit = "e" * 40
        with self.assertRaisesRegex(ValueError, "release commit mismatch"):
            wv.apply_condition_identity_migration(
                copy.deepcopy(self.ledger), "footbreak",
                authorized_manifest=self.document,
                expected_release_commit=wrong_commit,
            )
        zero_document = copy.deepcopy(self.document)
        zero_document["authority"]["release_commit"] = "0" * 40
        body = {
            key: value for key, value in zero_document.items()
            if key != "manifest_hash"
        }
        zero_document["manifest_hash"] = wv._canonical_hash(body)
        with self.assertRaises(ValueError):
            wv.apply_condition_identity_migration(
                copy.deepcopy(self.ledger), "footbreak",
                authorized_manifest=zero_document,
                expected_release_commit="0" * 40,
            )
        trusted = copy.deepcopy(self.ledger)
        installed = wv.apply_condition_identity_migration(
            trusted, "footbreak",
            trusted_manifest_hash=self.document["manifest_hash"],
            candidate_manifest=self.document,
            expected_release_commit=_checked_out_commit(),
        )
        self.assertEqual(installed, self.document)
        self.assertEqual(
            trusted[wv.NAMESPACE]["condition_identity_migrations"],
            self.document,
        )
        with self.assertRaisesRegex(ValueError, "trusted.*hash mismatch"):
            wv.apply_condition_identity_migration(
                copy.deepcopy(self.ledger), "footbreak",
                trusted_manifest_hash="0" * 64,
                candidate_manifest=self.document,
                expected_release_commit=_checked_out_commit(),
            )

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger_path = root / "ledger.json"
            manifest_path = root / "authorized.json"
            lock_path = root / "shared.lock"
            ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")
            manifest_path.write_text(json.dumps(self.document), encoding="utf-8")
            result = migrate_file(
                ledger_path, manifest_path, lock_path=lock_path,
                apply=True, confirmation=CONFIRMATION,
                trusted_manifest_hash=self.document["manifest_hash"],
            )
            self.assertEqual(result["status"], "applied")
            self.assertTrue(result["readback_verified"])

    def test_cli_commit_pin_cannot_be_overridden_by_caller(self):
        import analysis.migrate_condition_identity_retirement as cli

        fake_commit = "f" * 40
        checkout_commit = "e" * 40
        document = copy.deepcopy(self.document)
        document["authority"]["release_commit"] = fake_commit
        body = {key: value for key, value in document.items() if key != "manifest_hash"}
        document["manifest_hash"] = wv._canonical_hash(body)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger_path = root / "ledger.json"
            manifest_path = root / "authorized.json"
            lock_path = root / "shared.lock"
            ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")
            manifest_path.write_text(json.dumps(document), encoding="utf-8")
            before = ledger_path.read_bytes()
            with patch.object(
                cli, "_checked_out_commit", return_value=checkout_commit,
            ), self.assertRaisesRegex(ValueError, "release commit mismatch"):
                migrate_file(
                    ledger_path, manifest_path, lock_path=lock_path,
                    apply=False, confirmation=None,
                )
            self.assertEqual(ledger_path.read_bytes(), before)

    def test_already_installed_authority_is_never_ignored(self):
        installed = self.install(copy.deepcopy(self.ledger))
        with self.assertRaisesRegex(ValueError, "trusted.*hash mismatch"):
            wv.apply_condition_identity_migration(
                installed, "footbreak", trusted_manifest_hash="0" * 64,
            )
        wrong_document = copy.deepcopy(self.document)
        wrong_document["effective_at"] = "2026-08-27T08:59:59+08:00"
        body = {
            key: value for key, value in wrong_document.items()
            if key != "manifest_hash"
        }
        wrong_document["manifest_hash"] = wv._canonical_hash(body)
        with self.assertRaisesRegex(ValueError, "immutable.*mismatch"):
            wv.apply_condition_identity_migration(
                installed, "footbreak", authorized_manifest=wrong_document,
            )

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger_path = root / "ledger.json"
            manifest_path = root / "authorized.json"
            lock_path = root / "shared.lock"
            ledger_path.write_text(json.dumps(installed), encoding="utf-8")
            manifest_path.write_text(json.dumps(self.document), encoding="utf-8")
            before = ledger_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "trusted.*hash mismatch"):
                migrate_file(
                    ledger_path, manifest_path, lock_path=lock_path,
                    apply=True, confirmation=CONFIRMATION,
                    trusted_manifest_hash="0" * 64,
                )
            self.assertEqual(ledger_path.read_bytes(), before)

    def test_closed_migration_requires_exact_17_to_15_registry(self):
        for shape in ("extra", "missing"):
            with self.subTest(shape):
                ledger = copy.deepcopy(self.ledger)
                ns = ledger[wv.NAMESPACE]
                if shape == "extra":
                    definition = wv.condition_definition(
                        "footbreak", _candidate(18),
                    )
                    signature, frozen = _frozen(definition, 18)
                    ns["condition_order"].append(signature)
                    ns["conditions"][signature] = frozen
                else:
                    signature = ns["condition_order"].pop()
                    ns["conditions"].pop(signature)
                production, _validated, reason = (
                    wv._expected_production_identity_manifest(ns, "footbreak")
                )
                self.assertIsNone(reason)
                self.assertIsNotNone(production)
                ns["production_identity_manifest"] = production
                document = copy.deepcopy(self.document)
                document["authority"][
                    "production_identity_manifest_hash"
                ] = production["manifest_hash"]
                body = {
                    key: value for key, value in document.items()
                    if key != "manifest_hash"
                }
                document["manifest_hash"] = wv._canonical_hash(body)
                with self.assertRaisesRegex(ValueError, "cardinality"):
                    wv.plan_condition_identity_migration(
                        ledger, "footbreak", document,
                        expected_release_commit=_checked_out_commit(),
                    )
                ledger[wv.NAMESPACE][
                    "condition_identity_migrations"
                ] = document
                self.assertEqual(
                    wv.formal_registry_candidates(ledger, "footbreak"), [],
                )
                funnel = wv.project_condition_funnel(ledger, "footbreak")
                self.assertEqual(
                    funnel["unavailable_reason"],
                    "condition_identity_migrations_registry_cardinality_invalid",
                )
                before = copy.deepcopy(ledger)
                with self.assertRaisesRegex(ValueError, "cardinality"):
                    wv.recompute_namespace(ledger, "footbreak")
                self.assertEqual(ledger, before)
                manifest = build_manifest(ledger, "footbreak")
                self.assertFalse(manifest["valid"])
                self.assertIn(
                    "condition_identity_migrations_registry_cardinality_invalid",
                    manifest["rejection_reasons"],
                )
                self.assertIsNone(manifest["active_condition_count"])


if __name__ == "__main__":
    unittest.main()
