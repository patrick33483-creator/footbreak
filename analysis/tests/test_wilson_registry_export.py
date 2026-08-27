from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from analysis import wilson_validation as wv
from analysis.tests.test_wilson_37_condition_regression import (
    _checked_out_commit, _registry,
)
from analysis.wilson_registry_export import (
    ACTIVE_KEYS, ARTIFACT_KEYS, CONDITION_KEYS, HISTORICAL_KEYS,
    NAMESPACE_KEYS, TIMESTAMP_NAMESPACE_KEYS, TOP_LEVEL_KEYS, export_registry,
    main, verify_export,
)
from analysis.wilson_registry_manifest import build_manifest


class WilsonRegistryExportTests(unittest.TestCase):
    def _ledger(self, system: str) -> dict:
        ledger, _specs, _allowlist, document = _registry(system)
        if system == "footbreak":
            wv.apply_condition_identity_migration(
                ledger, system, authorized_manifest=document,
                expected_release_commit=_checked_out_commit(),
            )
        return ledger

    def test_allowlisted_shape_only_and_source_is_never_mutated(self):
        forbidden = {
            "bets", "observations", "watch", "match_id", "fixture",
            "home", "away", "league", "telegram", "provider", "secret",
        }
        for system, expected_count in (("footbreak", 17), ("crown", 20)):
            with self.subTest(system):
                ledger = self._ledger(system)
                before = copy.deepcopy(ledger)
                source_hash = hashlib.sha256(json.dumps(
                    ledger, ensure_ascii=False, sort_keys=True,
                ).encode()).hexdigest()
                exported = export_registry(
                    ledger, system, source_ledger_sha256=source_hash,
                )
                self.assertEqual(ledger, before)
                self.assertEqual(set(exported), TOP_LEVEL_KEYS)
                self.assertEqual(
                    set(exported["namespace_metadata"]).issubset(NAMESPACE_KEYS),
                    True,
                )
                self.assertEqual(len(exported["conditions"]), expected_count)
                for row in exported["conditions"]:
                    self.assertEqual(set(row), CONDITION_KEYS)
                    self.assertEqual(
                        set(row["historical_evidence"]), HISTORICAL_KEYS,
                    )
                    self.assertEqual(
                        set(row["historical_evidence"]["artifact"]),
                        ARTIFACT_KEYS,
                    )
                    self.assertEqual(set(row["active_evidence"]), ACTIVE_KEYS)
                    _definition, versions, reason = (
                        wv._validate_frozen_identity_and_chain(
                            row, row["signature"], system,
                        )
                    )
                    self.assertIsNone(reason)
                    self.assertIsNotNone(versions)
                keys = set()

                def visit(value):
                    if isinstance(value, dict):
                        keys.update(str(key).lower() for key in value)
                        for nested in value.values():
                            visit(nested)
                    elif isinstance(value, list):
                        for nested in value:
                            visit(nested)

                visit(exported)
                self.assertTrue(forbidden.isdisjoint(keys), forbidden & keys)
                self.assertEqual(verify_export(exported), exported)

    def test_export_digest_tamper_fails(self):
        ledger = self._ledger("crown")
        exported = export_registry(
            ledger, "crown", source_ledger_sha256="a" * 64,
        )
        for mutation in ("definition", "evidence", "digest"):
            with self.subTest(mutation):
                changed = copy.deepcopy(exported)
                if mutation == "definition":
                    changed["conditions"][0]["definition"]["role"] = "tampered"
                elif mutation == "evidence":
                    changed["conditions"][0]["evidence_versions"][0][
                        "cumulative_hits"
                    ] += 1
                else:
                    changed["export_digest"] = "0" * 64
                with self.assertRaisesRegex(ValueError, "export digest"):
                    verify_export(changed)

    def test_unknown_nested_fields_fail_even_when_consistently_resealed(self):
        ledger = self._ledger("crown")
        signature = ledger[wv.NAMESPACE]["condition_order"][0]
        frozen = ledger[wv.NAMESPACE]["conditions"][signature]
        mutations = {
            "evidence version": lambda row: row["evidence_versions"][0].update(
                private_customer_email="private@example.invalid",
            ),
            "historical evidence": lambda row: row[
                "historical_evidence"
            ].update(private_customer_email="private@example.invalid"),
            "active evidence": lambda row: row["active_evidence"].update(
                private_customer_email="private@example.invalid",
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                changed = copy.deepcopy(ledger)
                changed_frozen = changed[wv.NAMESPACE]["conditions"][signature]
                mutate(changed_frozen)
                # Unknown version fields are deliberately outside _version_hash,
                # so this models a consistently resealed private extension.
                if label == "evidence version":
                    version = changed_frozen["evidence_versions"][0]
                    version["evidence_hash"] = wv._version_hash(version)
                with self.assertRaisesRegex(
                    ValueError, "unknown.*fields",
                ):
                    export_registry(
                        changed, "crown", source_ledger_sha256="a" * 64,
                    )

        exported = export_registry(
            ledger, "crown", source_ledger_sha256="a" * 64,
        )
        changed = copy.deepcopy(exported)
        changed["conditions"][0]["evidence_versions"][0][
            "private_customer_email"
        ] = "private@example.invalid"
        body = {
            key: value for key, value in changed.items()
            if key != "export_digest"
        }
        changed["export_digest"] = wv._canonical_hash(body)
        with self.assertRaisesRegex(ValueError, "unknown or missing fields"):
            verify_export(changed)

    def test_namespace_scalars_reject_nested_and_wrong_types(self):
        base = self._ledger("crown")
        scalar_fields = {
            "schema_version": "2",
            "system": 7,
            "granular_ranking_initial_migration_version": "1",
            **{key: 7 for key in TIMESTAMP_NAMESPACE_KEYS},
        }
        for field, wrong_scalar in scalar_fields.items():
            for bad in (
                {"private_customer_email": "private@example.invalid"},
                ["private@example.invalid"],
                wrong_scalar,
            ):
                with self.subTest(field=field, bad=type(bad).__name__):
                    ledger = copy.deepcopy(base)
                    ledger[wv.NAMESPACE][field] = bad
                    with self.assertRaisesRegex(
                        ValueError, "namespace .*invalid",
                    ):
                        export_registry(
                            ledger, "crown", source_ledger_sha256="a" * 64,
                        )

    def test_unknown_source_namespace_key_is_rejected(self):
        ledger = self._ledger("crown")
        ledger[wv.NAMESPACE]["private_customer_email"] = (
            "private@example.invalid"
        )
        with self.assertRaisesRegex(ValueError, "unknown source fields"):
            export_registry(
                ledger, "crown", source_ledger_sha256="a" * 64,
            )

    def test_optional_stored_production_manifest_is_strictly_verified(self):
        ledger = self._ledger("crown")
        expected = copy.deepcopy(
            ledger[wv.NAMESPACE]["production_identity_manifest"],
        )

        matching = copy.deepcopy(ledger)
        matching_before = copy.deepcopy(matching)
        exported = export_registry(
            matching, "crown", source_ledger_sha256="a" * 64,
        )
        self.assertEqual(
            exported["production_identity_manifest"], expected,
        )
        self.assertEqual(matching, matching_before)

        absent = copy.deepcopy(ledger)
        absent[wv.NAMESPACE].pop("production_identity_manifest")
        absent_before = copy.deepcopy(absent)
        exported = export_registry(
            absent, "crown", source_ledger_sha256="b" * 64,
        )
        self.assertEqual(
            exported["production_identity_manifest"], expected,
        )
        self.assertEqual(absent, absent_before)
        self.assertNotIn(
            "production_identity_manifest", absent[wv.NAMESPACE],
        )

        mismatched = copy.deepcopy(ledger)
        stored = mismatched[wv.NAMESPACE]["production_identity_manifest"]
        stored["entries"][0]["definition_hash"] = "f" * 64
        body = {
            key: value for key, value in stored.items()
            if key != "manifest_hash"
        }
        stored["manifest_hash"] = wv._canonical_hash(body)
        with self.assertRaisesRegex(
            ValueError, "stored production identity manifest mismatch",
        ):
            export_registry(
                mismatched, "crown", source_ledger_sha256="c" * 64,
            )

        preinstall, _specs, _allowlist, _document = _registry("footbreak")
        preinstall[wv.NAMESPACE].pop("production_identity_manifest")
        before = copy.deepcopy(preinstall)
        self.assertFalse(build_manifest(preinstall, "footbreak")["valid"])
        exported = export_registry(
            preinstall, "footbreak", source_ledger_sha256="d" * 64,
        )
        self.assertIsNotNone(exported["production_identity_manifest"])
        self.assertEqual(preinstall, before)

    def test_cli_writes_only_sanitized_output_and_preserves_input(self):
        ledger = self._ledger("crown")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ledger.json"
            output = root / "chains.json"
            source.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8",
            )
            before = source.read_bytes()
            from unittest.mock import patch
            with patch(
                "sys.argv",
                [
                    "wilson_registry_export", "--system", "crown",
                    "--ledger", str(source), "--output", str(output),
                ],
            ):
                self.assertEqual(main(), 0)
            self.assertEqual(source.read_bytes(), before)
            verify_export(json.loads(output.read_text(encoding="utf-8")))
            with patch(
                "sys.argv",
                [
                    "wilson_registry_export", "--system", "crown",
                    "--ledger", str(source), "--output", str(source),
                ],
            ):
                self.assertEqual(main(), 2)
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
