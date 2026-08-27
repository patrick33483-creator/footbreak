import copy
import base64
import hashlib
import json
import math
import os
import tempfile
import unittest
from pathlib import Path

from analysis import wilson_validation as wv
from analysis.legacy_batch_aggregate import (
    AUTHORITY_DOMAIN, CALCULATION_DOMAIN,
    LegacyBatchAuthorityContext, ValidatedLegacyBatchCalculationContext,
    REQUIRED_RUNTIME_MODULES,
    assemble_final_authority_candidate, build_live_discovery,
    canonical_hash_v1, canonical_json_bytes_v1,
    classify_authority_source_support,
    plan_disposable_poststate, serialize_ledger_bytes_v1,
    runtime_identity_from_checkout,
    _stats_conditions_projection,
    read_root_owned_json_config,
    validate_final_authority, validate_sanitized_calculation,
    validate_live_discovery,
    walk_evidence_hash_occurrences,
)
from analysis.migrate_legacy_batch_aggregates import (
    CONFIRMATION, _identity, _read_regular_nofollow, migrate,
)
from analysis.wilson_registry_manifest import build_manifest
from analysis.wilson_registry_export import export_registry_v2, verify_export_v2


ROOT = Path(__file__).resolve().parents[2]
CALCULATION = ROOT.parent / "proposed-legacy-batch-authority-calculation.json"
FIXTURE = (
    ROOT / "analysis" / "tests" / "fixtures"
    / "wilson_production_registry_5205a8b.json"
)


def calculation_document():
    return json.loads(CALCULATION.read_text())


def test_runtime():
    value = runtime_identity_from_checkout(
        ROOT, sorted(REQUIRED_RUNTIME_MODULES.values()),
    )
    value["working_tree_policy"] = "clean_tracked_no_shadow_files"
    return value


def fixture_ledger():
    exported = json.loads(FIXTURE.read_text())["systems"]["footbreak"]
    ns = {
        **copy.deepcopy(exported["namespace_metadata"]),
        "condition_order": copy.deepcopy(exported["condition_order"]),
        "conditions": {
            row["signature"]: copy.deepcopy(row)
            for row in exported["conditions"]
        },
        "production_identity_manifest": copy.deepcopy(
            exported["production_identity_manifest"]
        ),
        "observations": [],
        "audit": [],
    }
    for frozen in ns["conditions"].values():
        frozen["rollover_audit"] = copy.deepcopy(
            frozen["evidence_versions"][1:][-64:]
        )
        frozen["pending_rollover_progress"] = {
            "eligible_decided": 0, "eligible_hits": 0, "accuracy": None,
            "required": 20, "display": "0/20",
            "excluded": {
                "missing_or_invalid_provenance": 0,
                "before_snapshot_boundary": 0,
                "not_binary_decided": 0,
                "duplicate_or_conflicting_fixture_market": 0,
            },
        }
    ledger = {"bets": [], wv.NAMESPACE: ns, "watch": {}}
    from analysis.tests.test_wilson_37_condition_regression import (
        _checked_out_commit, _retirement_document,
    )
    document = _retirement_document(
        ledger, wv.CONDITION_IDENTITY_MIGRATION_ALLOWLIST,
    )
    wv.apply_condition_identity_migration(
        ledger, "footbreak", authorized_manifest=document,
        expected_release_commit=_checked_out_commit(),
    )
    return ledger


def test_authority(calculation, ledger, post_hash, runtime):
    context = validate_sanitized_calculation(calculation)
    discovery = build_live_discovery(
        ledger, context,
        execution_identity=copy.deepcopy(runtime),
        writer_coordination={
            "all_writers_quiesced": True,
            "canonical_lock": {
                "realpath": "/test/lock", "st_dev": 1, "st_ino": 2,
                "st_uid": 0, "st_gid": 0, "st_mode": 384, "st_nlink": 1,
            },
            "writer_inventory_root": "a" * 64,
            "writer_count": 0,
            "service_configuration_sha256": "b" * 64,
            "runtime_config": {
                "realpath": "/test/runtime.json", "st_dev": 1, "st_ino": 4,
                "st_uid": 0, "st_gid": 0, "st_mode": 384, "st_nlink": 1,
                "sha256": "c" * 64,
            },
        },
        capture={
            "ledger_object": {
                "realpath": "/test/ledger", "st_dev": 1, "st_ino": 3,
                "st_uid": 0, "st_gid": 0, "st_mode": 420, "st_nlink": 1,
            },
        },
    )
    authority = assemble_final_authority_candidate(calculation, discovery)
    return authority, authority["authority_manifest_hash"]


class LegacyBatchCalculationTests(unittest.TestCase):
    def test_exact_calculation_and_reservations(self):
        context = validate_sanitized_calculation(calculation_document())
        self.assertEqual(context.domain_tag, CALCULATION_DOMAIN)
        self.assertEqual(len(context.entries), 10)
        self.assertEqual(
            {signature: len(values) for signature, values in context.reservations.items()},
            {
                "7b69b0c09392930f89bfe52d": 40,
                "e9b991435138c3c429a696a8": 40,
                "a7a8aae669b985ff87f8be6e": 60,
                "0869fbd4573b9dee57ffe2eb": 40,
                "a79e13125a194532c8194036": 20,
            },
        )
        occurrences = [
            value
            for entry in context.entries.values()
            for value in entry["source_version"]["batch_fixture_market_hashes"]
        ]
        self.assertEqual(len(occurrences), 200)
        self.assertEqual(len(set(occurrences)), 94)

    def test_mutation_duplicate_and_context_forgery_reject(self):
        mutations = []
        changed = calculation_document()
        changed["authorization_body"]["entries"][0]["source_version"][
            "batch_hits"
        ] += 1
        mutations.append(changed)
        changed = calculation_document()
        changed["authorization_body"]["entries"][1] = copy.deepcopy(
            changed["authorization_body"]["entries"][0]
        )
        mutations.append(changed)
        changed = calculation_document()
        changed["unexpected"] = True
        mutations.append(changed)
        for document in mutations:
            with self.assertRaises(ValueError):
                validate_sanitized_calculation(document)
        with self.assertRaises(TypeError):
            ValidatedLegacyBatchCalculationContext(None, {}, {}, {})
        with self.assertRaises(TypeError):
            LegacyBatchAuthorityContext(None, {}, None, "")

    def test_exact_serializer_and_type_rejections(self):
        value = {"é": "\\\n", "a": [-0.0, 1, {}, []]}
        self.assertEqual(
            canonical_json_bytes_v1(value),
            '{"a":[-0.0,1,{},[]],"é":"\\\\\\n"}'.encode(),
        )
        self.assertTrue(serialize_ledger_bytes_v1(value).endswith(b"\n"))
        self.assertFalse(serialize_ledger_bytes_v1(value).endswith(b"\n\n"))
        for bad in (
            float("nan"), float("inf"), 2**63, (1, 2), b"bytes",
            {1: "non-string"},
        ):
            with self.assertRaises(ValueError):
                canonical_json_bytes_v1(bad)

    def test_runtime_config_rejects_nonroot_symlink_and_hardlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "runtime.json"
            config.write_text("{}")
            os.chmod(config, 0o600)
            with self.assertRaises(ValueError):
                read_root_owned_json_config(config)
            symlink = root / "runtime-link.json"
            symlink.symlink_to(config)
            with self.assertRaises((ValueError, OSError)):
                read_root_owned_json_config(symlink)
            hardlink = root / "runtime-hardlink.json"
            os.link(config, hardlink)
            with self.assertRaises(ValueError):
                read_root_owned_json_config(hardlink)

    def test_ledger_lock_symlink_hardlink_and_inode_swap_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "ledger.json"
            target.write_text("{}")
            symlink = root / "ledger-link.json"
            symlink.symlink_to(target)
            with self.assertRaises(OSError):
                _read_regular_nofollow(symlink)
            hardlink = root / "ledger-hard.json"
            os.link(target, hardlink)
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                with self.assertRaises(ValueError):
                    _identity(target, descriptor)
            finally:
                os.close(descriptor)
            hardlink.unlink()
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
            replacement = root / "replacement.json"
            replacement.write_text("{}")
            os.replace(replacement, target)
            try:
                with self.assertRaises(ValueError):
                    _identity(target, descriptor)
            finally:
                os.close(descriptor)

    def test_runtime_identity_rejects_unrelated_checkout_and_empty_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                runtime_identity_from_checkout(Path(directory), [])
        with self.assertRaises(ValueError):
            runtime_identity_from_checkout(ROOT, ["analysis/wilson_validation.py"])
        identity = test_runtime()
        self.assertEqual(
            len(identity["module_manifest"]), len(REQUIRED_RUNTIME_MODULES),
        )
        self.assertTrue(all(
            Path(row["resolved_path"]).is_relative_to(ROOT)
            for row in identity["module_manifest"]
        ))


class LegacyBatchTransformationTests(unittest.TestCase):
    def setUp(self):
        self.calculation_document = calculation_document()
        self.context = validate_sanitized_calculation(self.calculation_document)
        self.ledger = fixture_ledger()

    def test_exact_transform_preserves_approved_scope_and_is_pure(self):
        before = copy.deepcopy(self.ledger)
        plan = plan_disposable_poststate(self.ledger, self.context)
        self.assertEqual(self.ledger, before)
        self.assertEqual(len(plan["converted_version_paths"]), 10)
        ns = plan["ledger"][wv.NAMESPACE]
        condition_keys = {
            "signature", "condition_number", "definition", "historical_evidence",
            "evidence_versions", "active_evidence_version",
            "active_evidence_hash", "active_evidence",
        }
        scope = {
            "scope": "footbreak-legacy-batch-post-condition-registry-v1",
            "system": "footbreak",
            "namespace_metadata": copy.deepcopy(
                self.calculation_document[
                    "expected_post_condition_registry_scope"
                ]["namespace_metadata"]
            ),
            "condition_order": ns["condition_order"],
            "conditions": [{
                key: copy.deepcopy(ns["conditions"][signature][key])
                for key in condition_keys
            } for signature in ns["condition_order"]],
            "production_identity_manifest": ns["production_identity_manifest"],
        }
        self.assertEqual(
            canonical_hash_v1(scope),
            self.calculation_document["expected_post_condition_registry_sha256"],
        )
        for entry in self.context.entries.values():
            source = entry["source_version"]
            post = entry["expected_rewrite"]["expected_post_version"]
            for key, value in source.items():
                if key not in {
                    "batch_fixture_market_hashes", "prior_evidence_hash",
                    "evidence_hash",
                }:
                    self.assertEqual(post[key], value)

    def test_stale_partial_swap_and_extra_reference_reject(self):
        variants = []
        stale = copy.deepcopy(self.ledger)
        stale[wv.NAMESPACE]["conditions"][
            "7b69b0c09392930f89bfe52d"
        ]["evidence_versions"][2]["batch_hits"] += 1
        variants.append(stale)
        partial = plan_disposable_poststate(self.ledger, self.context)["ledger"]
        signature = "e9b991435138c3c429a696a8"
        partial[wv.NAMESPACE]["conditions"][signature]["evidence_versions"][2] = (
            copy.deepcopy(
                self.ledger[wv.NAMESPACE]["conditions"][signature][
                    "evidence_versions"
                ][2]
            )
        )
        variants.append(partial)
        swapped = copy.deepcopy(self.ledger)
        left = swapped[wv.NAMESPACE]["conditions"]["7b69b0c09392930f89bfe52d"][
            "evidence_versions"
        ]
        left[2], left[3] = left[3], left[2]
        variants.append(swapped)
        extra = copy.deepcopy(self.ledger)
        old_hash = next(iter(self.context.entries.values()))[
            "source_version"
        ]["evidence_hash"]
        extra["unknown_reference"] = old_hash
        variants.append(extra)
        for value in variants:
            with self.assertRaises(ValueError):
                plan_disposable_poststate(value, self.context)

    def test_discovery_is_hash_path_count_only_and_zero_exact(self):
        discovery = build_live_discovery(self.ledger, self.context)
        self.assertTrue(discovery["migration_ready"])
        self.assertEqual(
            {row["classification"] for row in discovery["source_support"]},
            {"zero_exact"},
        )
        self.assertNotIn("team", json.dumps(discovery).lower())
        self.assertNotIn("provider", json.dumps(discovery).lower())
        self.assertEqual(discovery["expected_post"]["converted_version_count"], 10)
        mapping = {
            entry["source_version"]["evidence_hash"]:
            entry["expected_rewrite"]["expected_evidence_hash"]
            for entry in self.context.entries.values()
        }
        inventory = walk_evidence_hash_occurrences(self.ledger, mapping)
        self.assertTrue(all("json_pointer" in row for row in inventory))
        for mutate in (
            lambda value: value.update(chain_preimages=[]),
            lambda value: value["execution_identity"].update(extra=True),
            lambda value: value["expected_post"].update(
                authority_neutral_manifest_payload_sha256="9" * 64
            ),
            lambda value: value["expected_post"].update(
                condition_funnel_semantic_root="9" * 64
            ),
        ):
            changed = copy.deepcopy(discovery)
            mutate(changed)
            changed.pop("discovery_document_sha256")
            changed["discovery_document_sha256"] = canonical_hash_v1(changed)
            with self.assertRaises(ValueError):
                validate_live_discovery(changed, self.context)

    def test_stats_absent_has_20_and_exact_cache_has_30_old_hash_paths(self):
        runtime = test_runtime()
        coordination = {
            "all_writers_quiesced": True,
            "canonical_lock": {
                "realpath": "/test/lock", "st_dev": 1, "st_ino": 2,
                "st_uid": 0, "st_gid": 0, "st_mode": 384, "st_nlink": 1,
            },
            "writer_inventory_root": "a" * 64, "writer_count": 5,
            "service_configuration_sha256": "b" * 64,
            "runtime_config": {
                "realpath": "/test/config", "st_dev": 1, "st_ino": 4,
                "st_uid": 0, "st_gid": 0, "st_mode": 384, "st_nlink": 1,
                "sha256": "c" * 64,
            },
        }
        capture = {"ledger_object": {
            "realpath": "/test/ledger", "st_dev": 1, "st_ino": 3,
            "st_uid": 0, "st_gid": 0, "st_mode": 420, "st_nlink": 1,
        }}
        absent = build_live_discovery(
            self.ledger, self.context, execution_identity=runtime,
            writer_coordination=coordination, capture=capture,
        )
        self.assertEqual(
            absent["expected_post"]["authenticated_source_hash_copies"][
                "total_count"
            ], 20,
        )
        present_ledger = copy.deepcopy(self.ledger)
        present_ledger[wv.NAMESPACE]["stats"] = {
            "conditions": _stats_conditions_projection(
                present_ledger[wv.NAMESPACE]["conditions"]
            )
        }
        present = build_live_discovery(
            present_ledger, self.context, execution_identity=runtime,
            writer_coordination=coordination, capture=capture,
        )
        copies = present["expected_post"]["authenticated_source_hash_copies"]
        self.assertEqual(
            (
                copies["authoritative_chain_marker_count"],
                copies["rollover_audit_marker_count"],
                copies["stats_conditions_marker_count"],
                copies["total_count"],
            ),
            (10, 10, 10, 30),
        )

    def test_every_nonzero_source_support_class_blocks_conversion(self):
        entry = next(iter(self.context.entries.values()))
        signature = entry["condition_signature"]
        wanted = entry["source_version"]["batch_fixture_market_hashes"]

        def item(value, index):
            return {
                "fixture_market_hash": value,
                "stage_at": (
                    entry["source_version"]["activation_boundary_at"]
                    if index == 19 else f"2026-01-{index + 1:02d}T00:00:00Z"
                ),
                "result": (
                    "Won"
                    if index < entry["source_version"]["batch_hits"] else "Lost"
                ),
            }

        for count, expected in ((0, "zero_exact"), (1, "partial"), (19, "partial")):
            index = {
                (signature, value): [item(value, offset)]
                for offset, value in enumerate(wanted[:count])
            }
            result = classify_authority_source_support(
                index, entry, self.context,
            )
            self.assertEqual(result["classification"], expected)
            self.assertEqual(result["migration_ready"], count == 0)
        duplicate = {
            (signature, value): [item(value, offset)]
            for offset, value in enumerate(wanted)
        }
        duplicate[(signature, wanted[0])].append(item(wanted[0], 0))
        self.assertEqual(
            classify_authority_source_support(
                duplicate, entry, self.context,
            )["classification"],
            "duplicate_or_conflicting",
        )
        all_rows = {
            (signature, value): [item(value, offset)]
            for offset, value in enumerate(wanted)
        }
        self.assertNotEqual(
            classify_authority_source_support(
                all_rows, entry, self.context,
            )["classification"],
            "zero_exact",
        )

    def test_manifest_requires_authority_and_reservation_blocks_reuse(self):
        post = plan_disposable_poststate(self.ledger, self.context)
        no_authority = build_manifest(post["ledger"], "footbreak")
        self.assertIn("authority_required", no_authority["rejection_reasons"])
        runtime = test_runtime()
        document, digest = test_authority(
            self.calculation_document, self.ledger,
            post["post_ledger_sha256"], runtime,
        )
        authority = validate_final_authority(document, digest, runtime)
        self.assertEqual(authority.domain_tag, AUTHORITY_DOMAIN)
        signature = "7b69b0c09392930f89bfe52d"
        reserved = next(iter(authority.reservations[signature]))
        row = {
            "status": "SETTLED", "frozen_condition_signature": signature,
            "result": "Won", "stage": "T-5",
            "first_native_pre_kickoff_t5": True,
            "rollover_provenance": {
                "fixture_market_hash": reserved, "stage_at": "2099-01-01T00:00:00Z",
                "condition_signature": signature,
                "schema_version": 1, "system": "footbreak",
                "admitted_evidence_version": 4,
                "admitted_evidence_hash": "a" * 64,
                "native_pre_kickoff_t5": True,
            },
        }
        eligible, excluded = wv._eligible_rollover_rows(
            [row], "footbreak", signature,
            {"activation_boundary_at": "2026-01-01T00:00:00Z"},
            authority.reservations[signature],
        )
        self.assertEqual(eligible, [])
        self.assertEqual(excluded["historical_authority_identity_reuse"], 1)

    def test_v2_export_requires_hash_pin_or_exact_ed25519_signature(self):
        post = plan_disposable_poststate(self.ledger, self.context)
        runtime = test_runtime()
        document, digest = test_authority(
            self.calculation_document, self.ledger,
            post["post_ledger_sha256"], runtime,
        )
        authority = validate_final_authority(document, digest, runtime)
        exported = export_registry_v2(
            post["ledger"], authority_context=authority,
        )
        self.assertEqual(
            verify_export_v2(exported, trusted_authority_hash=digest),
            exported,
        )
        with self.assertRaises(ValueError):
            verify_export_v2(exported, trusted_public_key=object())
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        signature = private.sign(
            b"footbreak-legacy-batch-final-authority-v1"
            + b"\0" + bytes.fromhex(digest)
        )
        sidecar = {
            "schema": "footbreak-legacy-batch-authority-signature-v1",
            "algorithm": "ed25519",
            "authority_manifest_hash": digest,
            "approver_key_id": "independent-review-key",
            "approved_at": "2026-08-27T12:00:00+08:00",
            "signature_base64": base64.b64encode(signature).decode(),
        }
        self.assertEqual(
            verify_export_v2(
                exported,
                trusted_public_key=base64.b64encode(public).decode(),
                detached_signature=sidecar,
                trusted_public_key_id="independent-review-key",
            ),
            exported,
        )
        changed = copy.deepcopy(sidecar)
        changed["signature_base64"] = base64.b64encode(b"x" * 64).decode()
        with self.assertRaises(ValueError):
            verify_export_v2(
                exported, trusted_public_key=public,
                detached_signature=changed,
                trusted_public_key_id="independent-review-key",
            )

    def test_authority_rejects_missing_or_nonready_discovery_and_runtime_drift(self):
        post = plan_disposable_poststate(self.ledger, self.context)
        runtime = test_runtime()
        authority, digest = test_authority(
            self.calculation_document, self.ledger,
            post["post_ledger_sha256"], runtime,
        )
        for mutate in (
            lambda value: value.pop("live_discovery_document"),
            lambda value: value["live_discovery_document"].update(
                migration_ready=False
            ),
            lambda value: value["implementation"].update(
                release_commit="9" * 40
            ),
            lambda value: value["live_prestate"].update(
                reference_inventory_root="9" * 64
            ),
        ):
            changed = copy.deepcopy(authority)
            mutate(changed)
            body = {
                key: value for key, value in changed.items()
                if key != "authority_manifest_hash"
            }
            changed["authority_manifest_hash"] = canonical_hash_v1(body)
            with self.assertRaises(ValueError):
                validate_final_authority(
                    changed, changed["authority_manifest_hash"], runtime,
                )
        with self.assertRaises(ValueError):
            validate_final_authority(authority, digest, {
                **runtime, "release_commit": "8" * 40,
            })


class LegacyBatchCliTests(unittest.TestCase):
    def test_dry_run_apply_idempotency_and_post_replace_rollback(self):
        calculation = calculation_document()
        context = validate_sanitized_calculation(calculation)
        fault_points = (
            None, "after_backup_fsync", "after_temp_fsync", "before_replace",
            "after_replace", "after_directory_fsync", "after_readback",
            "after_postproof", "after_report_fsync",
            "rollback_failure",
        )
        for inject_failure in fault_points:
            with self.subTest(inject_failure=inject_failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                ledger = fixture_ledger()
                raw = serialize_ledger_bytes_v1(ledger)
                post = plan_disposable_poststate(ledger, context)
                runtime = test_runtime()
                ledger_path = root / "ledger.json"
                authority_path = root / "authority.json"
                lock_path = root / "writer.lock"
                config_path = root / "runtime.json"
                ledger_path.write_bytes(raw)
                lock_path.touch(mode=0o600)
                os.chmod(lock_path, 0o600)
                lock_stat = lock_path.stat()
                ledger_stat = ledger_path.stat()
                writer_inventory = [
                    {
                        "name": name, "unit_name": f"footbreak-{name}.service",
                        "state": "inactive",
                        "unit_sha256": "a" * 64,
                        "config_sha256": "b" * 64,
                    }
                    for name in (
                        "tick", "sweep", "settle", "t30",
                        "result_reconciliation",
                    )
                ]
                service_configuration = {
                    "ledger_path": str(ledger_path),
                    "canonical_lock_path": str(lock_path),
                }
                discovery = build_live_discovery(
                    ledger, context, raw_ledger_bytes=raw,
                    execution_identity=runtime,
                    writer_coordination={
                        "all_writers_quiesced": True,
                        "canonical_lock": {
                            "realpath": str(lock_path.resolve()),
                            "st_dev": lock_stat.st_dev, "st_ino": lock_stat.st_ino,
                            "st_uid": lock_stat.st_uid, "st_gid": lock_stat.st_gid,
                            "st_mode": 384, "st_nlink": 1,
                        },
                        "writer_inventory_root": canonical_hash_v1(
                            writer_inventory
                        ),
                        "writer_count": len(writer_inventory),
                        "service_configuration_sha256": canonical_hash_v1(
                            service_configuration
                        ),
                        "runtime_config": {
                            "realpath": str(config_path.resolve()),
                            "st_dev": 1, "st_ino": 1, "st_uid": 0, "st_gid": 0,
                            "st_mode": 384, "st_nlink": 1, "sha256": "c" * 64,
                        },
                    },
                    capture={"ledger_object": {
                        "realpath": str(ledger_path.resolve()),
                        "st_dev": ledger_stat.st_dev, "st_ino": ledger_stat.st_ino,
                        "st_uid": ledger_stat.st_uid, "st_gid": ledger_stat.st_gid,
                        "st_mode": 420, "st_nlink": 1,
                    }},
                )
                authority = assemble_final_authority_candidate(
                    calculation, discovery,
                )
                digest = authority["authority_manifest_hash"]
                authority_path.write_bytes(serialize_ledger_bytes_v1(authority))
                config = {
                    "ledger_path": str(ledger_path),
                    "canonical_lock_path": str(lock_path),
                    "canonical_lock_identity": {
                        "st_dev": lock_stat.st_dev, "st_ino": lock_stat.st_ino,
                    },
                    "all_writers_quiesced": True,
                    "repository_root": str(ROOT),
                    "writer_inventory": writer_inventory,
                    "service_configuration": service_configuration,
                    "authority_path": str(authority_path),
                }
                config_path.write_bytes(serialize_ledger_bytes_v1(config))
                collector = lambda _root, _paths: copy.deepcopy(runtime)
                loader = lambda _path: (
                    copy.deepcopy(config), b"test-config",
                    copy.deepcopy(discovery["writer_coordination"]["runtime_config"]),
                )
                code, dry = migrate(
                    ledger_path, authority_path, config_path,
                    trusted_manifest_hash=digest,
                    runtime_identity_collector=collector,
                    config_loader=loader,
                    writer_state_probe=lambda _unit: "inactive",
                )
                self.assertEqual(code, 0, dry)
                self.assertTrue(dry["dry_run"])
                self.assertEqual(ledger_path.read_bytes(), raw)

                def fault(point):
                    if inject_failure == "rollback_failure" and point in {
                        "after_replace", "before_rollback_replace",
                    }:
                        raise OSError(f"injected fault at {point}")
                    if point == inject_failure:
                        raise OSError(f"injected fault at {point}")

                code, report = migrate(
                    ledger_path, authority_path, config_path,
                    trusted_manifest_hash=digest, apply=True,
                    confirmation=CONFIRMATION, fault_hook=fault,
                    runtime_identity_collector=collector,
                    config_loader=loader,
                    writer_state_probe=lambda _unit: "inactive",
                )
                if inject_failure == "rollback_failure":
                    self.assertEqual(code, 5)
                    self.assertTrue(report["rollback_failed"])
                elif inject_failure in {
                    "after_replace", "after_directory_fsync", "after_readback",
                    "after_postproof", "after_report_fsync",
                }:
                    self.assertEqual(code, 4)
                    self.assertTrue(report["rolled_back"])
                    self.assertEqual(ledger_path.read_bytes(), raw)
                elif inject_failure is None:
                    self.assertEqual(code, 0, report)
                    self.assertTrue(report["committed"])
                    applied = ledger_path.read_bytes()
                    code, again = migrate(
                        ledger_path, authority_path, config_path,
                        trusted_manifest_hash=digest, apply=True,
                        confirmation=CONFIRMATION,
                        runtime_identity_collector=collector,
                        config_loader=loader,
                        writer_state_probe=lambda _unit: "inactive",
                    )
                    self.assertEqual(code, 0, again)
                    self.assertTrue(again["already_applied"])
                    self.assertEqual(ledger_path.read_bytes(), applied)
                else:
                    self.assertEqual(code, 2, report)
                    self.assertFalse(report["committed"])
                    self.assertEqual(ledger_path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
