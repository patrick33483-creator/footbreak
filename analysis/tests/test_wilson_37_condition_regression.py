"""Production-shaped regression for the complete repaired Wilson registry.

The public release has 37 historical identities, not 37 live accumulators:
Footbreak owns 17 historical identities with #1 -> #7 and #2 -> #14 retired,
while Crown owns 20 active identities.  These tests exercise every one of the
35 active identities through the real matcher, admission, row writer,
accumulator, rollover, and strict read-only manifest.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from analysis import wilson_validation as wv
from analysis.granular_conditions import (
    _descriptor, _line_bucket, _role, _selected_line, _tier,
)
from analysis.migrate_condition_identity_retirement import _checked_out_commit
from analysis.migrate_wilson_formal_bindings import migrate_legacy_formal_bindings
from analysis.quarter_line import from_dixon_coles
from analysis.wilson_registry_manifest import build_manifest


BASELINE = "2026-08-20T00:00:00+08:00"
EFFECTIVE = "2026-08-27T09:00:00+08:00"
ROW_START = datetime.fromisoformat("2026-08-27T09:01:00+08:00")
PROJECTION_NOW = "2026-08-27T10:30:00+08:00"
EXCLUDED_ZERO = {
    "missing_or_invalid_provenance": 0,
    "before_snapshot_boundary": 0,
    "not_binary_decided": 0,
    "duplicate_or_conflicting_fixture_market": 0,
}
PINNED_FIXTURE = (
    Path(__file__).parent / "fixtures"
    / "wilson_production_registry_5205a8b.json"
)
PINNED_FIXTURE_DIGEST = (
    "f79e435a28ab33db5fa585cc58aa01340221daacdac16709a513d2ffac87be77"
)
PINNED_IDENTITY_ROOTS = {
    "footbreak": "3dac349bc973988342bc7689509e7d93d1aa5a17e8ab1b2f7d249f27e1419952",
    "crown": "d49a6f41d5a7b8284453d87c306c4355e87ec213f2a8428931a27eaa039de3a2",
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()


def _pinned_fixture(value: dict | None = None) -> dict:
    fixture = (
        json.loads(PINNED_FIXTURE.read_text(encoding="utf-8"))
        if value is None else copy.deepcopy(value)
    )
    if hashlib.sha256(_canonical_bytes(fixture)).hexdigest() != PINNED_FIXTURE_DIGEST:
        raise ValueError("pinned production fixture digest mismatch")
    if fixture.get("source") != {
        "repository": "patrick33483-creator/footbreak",
        "audited_commit": "5205a8ba03bbae46bdc065f38a68467df48038aa",
        "workflow_run_id": 33028833183,
        "captured_at_utc": "2026-08-27T01:03:25.872502+00:00",
        "captured_ledger_sha256": {
            "footbreak-ledger.json": (
                "539c86d9ad36b026bc52a786c2dbd1a72669ecf73bd67b9ac41b7ec6d6e15a94"
            ),
            "crown-ledger.json": (
                "6b50800e896729bb070adacd81df76d056f4a372e942cf98c2a3130e873dec6c"
            ),
        },
        "source_manifest_hashes": {
            "footbreak": (
                "a386b5e4ea7ea2041b83a2b6a9f09f5e1ace92939d3582d63043e41ea2e52f56"
            ),
            "crown": (
                "35ab6adb7e258fcc54d3fc39bbe06921ee04677dadfbcf272cd10df21ea6cc6c"
            ),
        },
    }:
        raise ValueError("pinned production fixture provenance mismatch")
    for system, expected_count in (("footbreak", 17), ("crown", 20)):
        identities = fixture["systems"][system]["identities"]
        if len(identities) != expected_count:
            raise ValueError(f"{system}: pinned identity cardinality mismatch")
        projection = []
        for position, row in enumerate(identities, start=1):
            definition_hash = hashlib.sha256(
                _canonical_bytes(row["definition"]),
            ).hexdigest()
            if (
                row["condition_number"] != position
                or row["order_position"] != position
                or row["signature"] != definition_hash[:24]
                or row["definition_hash"] != definition_hash
                or not isinstance(row["active_evidence_version"], int)
                or isinstance(row["active_evidence_version"], bool)
                or row["active_evidence_version"] < 1
                or len(row["active_evidence_hash"]) != 64
            ):
                raise ValueError(
                    f"{system}: pinned identity mismatch at {position}"
                )
            projection.append({
                key: copy.deepcopy(row[key])
                for key in (
                    "condition_number", "order_position", "signature",
                    "definition_hash", "definition", "active_evidence_version",
                    "active_evidence_hash", "activation_boundary",
                )
            })
        if hashlib.sha256(_canonical_bytes(projection)).hexdigest() != (
            PINNED_IDENTITY_ROOTS[system]
        ):
            raise ValueError(f"{system}: pinned identity root mismatch")
    return fixture


def _active_projection(version: dict) -> dict:
    return {
        key: copy.deepcopy(version.get(key))
        for key in (
            "version", "cumulative_hits", "cumulative_decided",
            "wilson95_lower_raw", "minimum_acceptable_odds_raw",
            "minimum_acceptable_odds_display", "activation_boundary_at",
            "created_at", "evidence_hash",
        )
    }


def _frozen(
    definition: dict, number: int, *, hits: int = 70, decided: int = 80,
    boundary: str = BASELINE,
) -> tuple[str, dict]:
    signature = wv._canonical_hash(definition)[:24]
    values = wv._evidence_values(hits, decided)
    version = {
        "condition_signature": signature,
        "version": 1,
        "prior_version": None,
        "prior_evidence_hash": None,
        "batch_fixture_market_hashes": [],
        "batch_hits": 0,
        "batch_decided": 0,
        "cumulative_hits": hits,
        "cumulative_decided": decided,
        "wilson95_lower_raw": values["wilson95_lower_raw"],
        "minimum_acceptable_odds_raw": values["minimum_acceptable_odds_raw"],
        "minimum_acceptable_odds_display": values["display"][
            "minimum_acceptable_odds"
        ],
        "activation_boundary_at": boundary,
        "created_at": boundary,
        "migration_baseline": True,
    }
    version["evidence_hash"] = wv._version_hash(version)
    return signature, {
        "signature": signature,
        "condition_number": number,
        "frozen_at": BASELINE,
        "definition": copy.deepcopy(definition),
        "historical_evidence": {
            "hits": hits,
            "decided": decided,
            "pushes": 0,
            "artifact": {
                "hash": f"{number:064x}",
                "version": "production-shaped-regression-v1",
                "as_of": boundary,
            },
            "label": f"historical Wilson condition {number}",
        },
        "evidence_versions": [version],
        "active_evidence_version": 1,
        "active_evidence_hash": version["evidence_hash"],
        "active_evidence": _active_projection(version),
        "prospective": {},
        "pending_rollover_progress": {
            "eligible_decided": 0,
            "eligible_hits": 0,
            "accuracy": None,
            "required": 20,
            "display": "0/20",
            "excluded": copy.deepcopy(EXCLUDED_ZERO),
        },
    }


def _spec_for_definition(definition: dict) -> dict:
    """Find a live path that reproduces one exact pinned production key."""
    system, market = definition["system"], definition["market"]
    stages = tuple(definition["path"].split("→"))
    sides = ("H", "A") if market == "HDC" else ("H", "L")
    lines = (
        (-1.25, -.75, -.5, -.25, 0., .25, .5, .75, 1.25)
        if market == "HDC"
        else (2., 2.5, 2.75, 3., 3.25, 4.)
        if market == "HIL"
        else (8.5, 9.5, 10., 10.5, 10.75, 11.)
    )
    tiers = definition["odds_trajectory"].split("→") if (
        definition["odds_trajectory"]
    ) else tuple(
        ["≥1.70"] * (len(stages) - 1) + [definition["odds_tier"]]
    )
    level = 3 if definition["odds_trajectory"] else 2
    for side_path in itertools.product(sides, repeat=len(stages)):
        for line_path in itertools.product(lines, repeat=len(stages)):
            terminal_fraction = abs(line_path[-1]) % 1
            if (
                market == "HIL" and stages[-1] != "T-5"
                and terminal_fraction in {.25, .75}
            ):
                continue
            items = []
            for stage, side, line, tier in zip(
                stages, side_path, line_path, tiers,
            ):
                selected_line = _selected_line(market, side, line)
                odds = 1.90 if tier == "≥1.70" else 1.55
                items.append({
                    "stage": stage, "market": market, "side": side,
                    "raw_line": line, "selected_line": selected_line,
                    "role": _role(market, side, line),
                    "line_bucket": _line_bucket(market, selected_line),
                    "odds": odds, "odds_tier": _tier(odds),
                })
            key, _label, _specificity = _descriptor(
                system, tuple(items), level,
            )
            if list(key) == definition["miner_key"]:
                return {
                    "definition": copy.deepcopy(definition),
                    "items": items,
                    "decision_stage": stages[-1],
                    "legacy_level_two": len(stages) > 1 and level == 2,
                }
    raise AssertionError(
        f"no live path reproduces pinned identity {definition['miner_key']}"
    )


def _retirement_document(
    ledger: dict, allowlist: tuple[dict, dict],
) -> dict:
    ns = ledger[wv.NAMESPACE]
    entries = []
    for allowed in allowlist:
        hashes = wv._historical_activity_hashes(
            ledger, allowed["source_signature"],
        )
        activity = {
            "scope": ["bets", "wilson_validation.observations"],
            "row_count": len(hashes),
            "row_hashes": hashes,
            "rows_are_evidence": False,
        }
        activity["root_hash"] = wv._migration_activity_root(activity)
        entries.append({
            **allowed,
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
            "production_identity_manifest_hash": ns[
                "production_identity_manifest"
            ]["manifest_hash"],
        },
        "entries": entries,
    }
    return {**body, "manifest_hash": wv._canonical_hash(body)}


def _registry(system: str) -> tuple[dict, list[dict], tuple[dict, ...], dict | None]:
    pinned = _pinned_fixture()["systems"][system]["identities"]
    definitions = [copy.deepcopy(row["definition"]) for row in pinned]
    specs = [_spec_for_definition(definition) for definition in definitions]
    source_counts = {1: (78, 124), 2: (34, 51)}
    items = []
    for number, (definition, pinned_row) in enumerate(
        zip(definitions, pinned), start=1,
    ):
        hits, decided = source_counts.get(number, (70, 80))
        signature, frozen = _frozen(
            definition, number, hits=hits, decided=decided,
            boundary=(
                pinned_row["activation_boundary"]
                if system == "footbreak" and number in source_counts
                else BASELINE
            ),
        )
        assert signature == pinned_row["signature"]
        if system == "footbreak" and number in source_counts:
            assert (
                frozen["active_evidence_hash"]
                == pinned_row["active_evidence_hash"]
            )
        items.append((signature, frozen))
    order = [signature for signature, _frozen_row in items]
    ledger = {
        "bets": [],
        wv.NAMESPACE: {
            "schema_version": wv.SCHEMA_VERSION,
            "system": system,
            "activation_at": BASELINE,
            "quarter_settlement_activation_at": EFFECTIVE,
            "condition_order": order,
            "conditions": dict(items),
            "observations": [],
            "audit": [],
        },
        "watch": {},
    }
    ns = ledger[wv.NAMESPACE]
    expected, _validated, reason = wv._expected_production_identity_manifest(
        ns, system,
    )
    assert reason is None and expected is not None
    ns["production_identity_manifest"] = expected
    allowlist: tuple[dict, ...] = ()
    document = None
    if system == "footbreak":
        for source_index in (0, 1):
            ledger["bets"].append({
                "frozen_condition_signature": order[source_index],
                "created_at": "2026-08-26T12:00:00+08:00",
                "historical_row": source_index + 1,
            })
        allowed = []
        for source_number, target_number in ((1, 7), (2, 14)):
            source, target = order[source_number - 1], order[target_number - 1]
            source_frozen, target_frozen = (
                ns["conditions"][source], ns["conditions"][target],
            )
            allowed.append({
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
            })
        allowlist = wv.CONDITION_IDENTITY_MIGRATION_ALLOWLIST
        assert tuple(allowed) == allowlist
        document = _retirement_document(ledger, allowlist)
    return ledger, specs, allowlist, document


def _selected_and_rows(
    system: str, spec: dict, fixture: str, stage_at: str,
) -> tuple[list[dict], dict, dict]:
    kickoff = (datetime.fromisoformat(stage_at) + timedelta(hours=1)).isoformat()
    rows = []
    quote = None
    for index, item in enumerate(spec["items"]):
        saved = datetime.fromisoformat(stage_at) - timedelta(
            minutes=len(spec["items"]) - index - 1,
        )
        selected = {
            "code": item["market"],
            "side": item["side"],
            "line": item["raw_line"],
            "odds": item["odds"],
            "observed_at": (saved - timedelta(seconds=10)).isoformat(),
        }
        if (
            item["market"] == "HIL"
            and item["stage"] == "T-5"
            and abs(item["raw_line"]) % 1 in {.25, .75}
        ):
            selected["quarter_line_settlement"] = from_dixon_coles(
                line=item["raw_line"], side=item["side"],
                lh=1.55, la=1.15, rho=-.03,
            )
        row = {
            "match_id": fixture,
            "stage": item["stage"],
            "predicted_at": saved.isoformat(),
            "ts": saved.isoformat(),
            "kickoff": kickoff,
            "market_predictions": [selected],
        }
        rows.append(row)
        if item["stage"] == spec["decision_stage"]:
            quote = selected
    assert quote is not None
    terminal = spec["items"][-1]
    selected = {
        "market": terminal["market"],
        "side": terminal["side"],
        "line": terminal["raw_line"],
        "selected_line": terminal["selected_line"],
        "odds": 3.5,
        **(
            {"quarter_line_settlement": quote["quarter_line_settlement"]}
            if quote.get("quarter_line_settlement") is not None else {}
        ),
    }
    watch = {
        "match_id": fixture,
        "league": "production-shaped-regression",
        "home": "主隊",
        "away": "客隊",
        "kickoff": kickoff,
        "stages": rows,
    }
    if selected.get("quarter_line_settlement") is not None:
        quote["odds"] = selected["odds"]
        terminal_stage = rows[-1]
        payload = copy.deepcopy(terminal_stage)
        snapshot_hash = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode()).hexdigest()
        id_key = (
            "formal_admission_snapshot_id"
            if system == "crown" else "native_snapshot_id"
        )
        hash_key = (
            "formal_admission_snapshot_hash"
            if system == "crown" else "native_snapshot_hash"
        )
        terminal_stage[id_key] = f"{system}:{fixture}:T-5"
        terminal_stage[hash_key] = snapshot_hash
        selected["native_snapshot_binding"] = {
            "schema_version": 1,
            "system": system,
            "snapshot_id": terminal_stage[id_key],
            "snapshot_hash": snapshot_hash,
        }
    return rows, selected, watch


def _append_settled(
    case: unittest.TestCase, ledger: dict, system: str, candidate: dict,
    spec: dict, condition_number: int, row_index: int,
) -> dict:
    stage_at = (ROW_START + timedelta(seconds=row_index)).isoformat()
    fixture = f"{system}-{condition_number:02d}-{row_index:02d}"
    rows, selected, watch = _selected_and_rows(
        system, spec, fixture, stage_at,
    )
    own_stage = spec["decision_stage"]
    matched = wv.match_formal_registry(
        rows, [candidate], system=system, decision_stage=own_stage,
    )
    case.assertEqual(
        [row["__formal_frozen_signature"] for row in matched.get(fixture, [])],
        [candidate["__formal_frozen_signature"]],
        (system, condition_number, candidate["key"]),
    )
    matched_candidate = matched[fixture][0]
    selected["selected_side"] = matched_candidate["selected_side"]
    selected["selected_line"] = matched_candidate["selected_line"]
    admissions, reason = wv.matching_admissions(
        system, candidate["market"], selected, [matched_candidate],
        stage_at=stage_at,
    )
    case.assertEqual(reason, "wilson_pass")
    case.assertEqual(len(admissions), 1)
    admission, reason = wv.apply_active_evidence(
        ledger, system, admissions[0], stage_at=stage_at, now=stage_at,
    )
    case.assertIsNone(reason)
    case.assertIsNotNone(admission)
    assert admission is not None
    ledger["watch"][fixture] = watch
    if own_stage == "T-5":
        row = wv.commit_bet(
            ledger, system, watch, candidate["market"], selected, admission,
            now=stage_at, market_label=candidate["market"],
            selected_label=f"condition {condition_number}",
            selected_role=spec["items"][-1]["role"],
            selected_line=matched_candidate["selected_line"],
        )
        case.assertIsNotNone(row)
        assert row is not None
        ledger["bets"].append(row)
    else:
        row = wv.record_match_observation(
            ledger, system, watch, candidate["market"], selected, admission,
            now=stage_at, market_label=candidate["market"],
            selected_role=spec["items"][-1]["role"],
            selected_line=matched_candidate["selected_line"],
            decision_stage=own_stage,
        )
        case.assertIsNotNone(row)
        assert row is not None
        case.assertEqual(row["bet_status"], "FORMAL_OBSERVATION")
        case.assertNotIn("stake", row)
    row.update({
        "status": "SETTLED",
        "result": "Won" if row_index % 2 else "Lost",
        "settled_at": (
            datetime.fromisoformat(stage_at) + timedelta(minutes=70)
        ).isoformat(),
    })
    if "stake" in row:
        row["pnl"] = 500 if row["result"] == "Won" else -500
    return row


class WilsonThirtySevenConditionRegression(unittest.TestCase):
    def _installed_registry(
        self, system: str,
    ) -> tuple[dict, list[dict], tuple[dict, ...]]:
        ledger, specs, allowlist, document = _registry(system)
        if system == "footbreak":
            assert document is not None
            wv.apply_condition_identity_migration(
                ledger, system, authorized_manifest=document,
                expected_release_commit=_checked_out_commit(),
            )
        return ledger, specs, allowlist

    def test_all_37_historical_35_active_end_to_end(self):
        # Establish the independent, commit-pinned production identity and
        # evidence-root authority before running the behavior simulation.
        pinned = _pinned_fixture()
        self.assertEqual(
            {
                system: len(value["identities"])
                for system, value in pinned["systems"].items()
            },
            {"footbreak": 17, "crown": 20},
        )
        manifests = {}
        active_total = 0
        retired_total = 0
        legacy_level_two_seen = 0
        quarter_line_seen = 0
        for system, expected in (
            ("footbreak", (17, 15, 2)),
            ("crown", (20, 20, 0)),
        ):
            ledger, specs, allowlist = self._installed_registry(system)
            pinned_rows = pinned["systems"][system]["identities"]
            namespace = ledger[wv.NAMESPACE]
            self.assertEqual(
                namespace["condition_order"],
                [row["signature"] for row in pinned_rows],
            )
            for row in pinned_rows:
                frozen = namespace["conditions"][row["signature"]]
                self.assertEqual(frozen["condition_number"], row["condition_number"])
                self.assertEqual(frozen["definition"], row["definition"])
                self.assertEqual(
                    hashlib.sha256(
                        _canonical_bytes(frozen["definition"]),
                    ).hexdigest(),
                    row["definition_hash"],
                )
                # The production evidence root is independently pinned before
                # fresh internally-valid simulation evidence is attached.
                self.assertRegex(row["active_evidence_hash"], r"^[0-9a-f]{64}$")
                self.assertGreaterEqual(row["active_evidence_version"], 1)
            if system == "footbreak":
                self.assertEqual(
                    allowlist, wv.CONDITION_IDENTITY_MIGRATION_ALLOWLIST,
                )
            with patch.object(wv, "_now", return_value=PROJECTION_NOW):
                registry = wv.formal_registry_candidates(
                    ledger, system, now=PROJECTION_NOW,
                )
                self.assertEqual(len(registry), expected[1])
                by_signature = {
                    row["__formal_frozen_signature"]: row for row in registry
                }
                active_signatures = [
                    signature
                    for signature in ledger[wv.NAMESPACE]["condition_order"]
                    if signature in by_signature
                ]
                self.assertEqual(len(active_signatures), expected[1])
                for signature in active_signatures:
                    candidate = by_signature[signature]
                    number = ledger[wv.NAMESPACE]["conditions"][signature][
                        "condition_number"
                    ]
                    spec = specs[number - 1]
                    legacy_level_two_seen += int(spec["legacy_level_two"])
                    first = _append_settled(
                        self, ledger, system, candidate, spec, number, 1,
                    )
                    quarter_line_seen += int(
                        first.get("quarter_line_settlement") is not None
                    )
                    wv.recompute_namespace(ledger, system)
                    frozen = ledger[wv.NAMESPACE]["conditions"][signature]
                    self.assertEqual(
                        (
                            frozen["active_evidence_version"],
                            frozen["pending_rollover_progress"][
                                "eligible_decided"
                            ],
                            frozen["pending_rollover_progress"]["eligible_hits"],
                        ),
                        (1, 1, 1),
                        (system, number),
                    )
                    v1 = copy.deepcopy(frozen["evidence_versions"][0])
                    for row_index in range(2, 21):
                        _append_settled(
                            self, ledger, system, candidate, spec, number,
                            row_index,
                        )
                    wv.recompute_namespace(ledger, system)
                    frozen = ledger[wv.NAMESPACE]["conditions"][signature]
                    self.assertEqual(frozen["evidence_versions"][0], v1)
                    self.assertEqual(len(frozen["evidence_versions"]), 2)
                    self.assertEqual(
                        (
                            frozen["active_evidence_version"],
                            frozen["evidence_versions"][1]["batch_decided"],
                            frozen["evidence_versions"][1]["batch_hits"],
                            frozen["pending_rollover_progress"]["display"],
                        ),
                        (2, 20, 10, "0/20"),
                        (system, number),
                    )
                manifest = build_manifest(ledger, system)
                self.assertTrue(manifest["valid"], manifest)
                self.assertEqual(
                    (
                        manifest["historical_condition_count"],
                        manifest["active_condition_count"],
                        manifest["retired_duplicate_count"],
                    ),
                    expected,
                )
                self.assertEqual(
                    sum(
                        row["identity_status"] == "active"
                        for row in manifest["conditions"]
                    ),
                    expected[1],
                )
                manifests[system] = manifest
                active_total += expected[1]
                retired_total += expected[2]
        self.assertEqual(
            sum(item["historical_condition_count"] for item in manifests.values()),
            37,
        )
        self.assertEqual((active_total, retired_total), (35, 2))
        self.assertGreater(legacy_level_two_seen, 0)
        self.assertGreater(quarter_line_seen, 0)

    def test_pinned_production_fixture_uses_real_retirement_allowlist(self):
        fixture = _pinned_fixture()
        footbreak = fixture["systems"]["footbreak"]["identities"]
        crown = fixture["systems"]["crown"]["identities"]
        self.assertEqual(
            [row["condition_number"] for row in footbreak], list(range(1, 18)),
        )
        self.assertEqual(
            [row["condition_number"] for row in crown], list(range(1, 21)),
        )
        for allowed in wv.CONDITION_IDENTITY_MIGRATION_ALLOWLIST:
            source = footbreak[allowed["source_condition_number"] - 1]
            target = footbreak[allowed["target_condition_number"] - 1]
            self.assertEqual(source["signature"], allowed["source_signature"])
            self.assertEqual(
                source["definition_hash"], allowed["source_definition_hash"],
            )
            self.assertEqual(
                source["active_evidence_hash"],
                allowed["source_initial_evidence_hash"],
            )
            self.assertEqual(target["signature"], allowed["target_signature"])
            self.assertEqual(
                target["definition_hash"], allowed["target_definition_hash"],
            )
            rebuilt, definition = wv.condition_signature(
                "footbreak", {
                    **source["definition"],
                    "key": source["definition"]["miner_key"],
                },
            )
            self.assertEqual(rebuilt, target["signature"])
            self.assertEqual(definition, target["definition"])

    def test_pinned_fixture_rejects_reorder_renumber_and_self_reseal(self):
        fixture = _pinned_fixture()
        mutations = {
            "reorder": lambda value: value["systems"]["crown"][
                "identities"
            ].reverse(),
            "renumber": lambda value: value["systems"]["footbreak"][
                "identities"
            ][2].update(condition_number=99),
            "self-reseal": self._self_reseal_fixture_identity,
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                changed = copy.deepcopy(fixture)
                mutate(changed)
                with self.assertRaisesRegex(
                    ValueError, "pinned production fixture digest mismatch",
                ):
                    _pinned_fixture(changed)

    def test_retired_sources_are_terminal_and_never_accumulate(self):
        ledger, specs, allowlist = self._installed_registry("footbreak")
        with patch.object(wv, "_now", return_value=PROJECTION_NOW):
            registry = wv.formal_registry_candidates(
                ledger, "footbreak", now=PROJECTION_NOW,
            )
            projected = {
                row["__formal_frozen_signature"] for row in registry
            }
            for source_number, target_number in ((1, 7), (2, 14)):
                source = allowlist[source_number - 1]["source_signature"]
                target = allowlist[source_number - 1]["target_signature"]
                self.assertNotIn(source, projected)
                self.assertIn(target, projected)
                self.assertEqual(
                    ledger[wv.NAMESPACE]["condition_order"][target_number - 1],
                    target,
                )
                frozen = ledger[wv.NAMESPACE]["conditions"][source]
                before = copy.deepcopy(frozen)
                rejected, reason = wv.apply_active_evidence(
                    ledger, "footbreak", {
                        "signature": source,
                        "definition": copy.deepcopy(frozen["definition"]),
                        "history": copy.deepcopy(frozen["historical_evidence"]),
                        "arithmetic": wv.admission_arithmetic(70, 80, 3.5),
                    },
                    stage_at=(ROW_START + timedelta(minutes=1)).isoformat(),
                    now=(ROW_START + timedelta(minutes=1)).isoformat(),
                )
                self.assertIsNone(rejected)
                self.assertEqual(reason, "retired_duplicate_target_only")
                wv.recompute_namespace(ledger, "footbreak")
                self.assertEqual(
                    ledger[wv.NAMESPACE]["conditions"][source], before,
                )
            manifest = build_manifest(ledger, "footbreak")
            retired = [
                row for row in manifest["conditions"]
                if row["identity_status"] == "retired_duplicate"
            ]
            self.assertEqual(
                [
                    (
                        row["condition_number"],
                        row["canonical_successor_condition_number"],
                        row["future_admission"],
                    )
                    for row in retired
                ],
                [(1, 7, "target_only"), (2, 14, "target_only")],
            )

    def test_legacy_binding_migrates_without_losing_pending_evidence(self):
        ledger, specs, _allowlist = self._installed_registry("crown")
        with patch.object(wv, "_now", return_value=PROJECTION_NOW):
            registry = wv.formal_registry_candidates(
                ledger, "crown", now=PROJECTION_NOW,
            )
            candidate = next(
                row for row in registry
                if row["stage"] == "T-5" and row["market"] != "HIL"
            )
            signature = candidate["__formal_frozen_signature"]
            number = ledger[wv.NAMESPACE]["conditions"][signature][
                "condition_number"
            ]
            row = _append_settled(
                self, ledger, "crown", candidate, specs[number - 1], number, 1,
            )
            wv.recompute_namespace(ledger, "crown")
            row["frozen_condition_definition"] = {}
            row.pop("native_stage_at")
            result = migrate_legacy_formal_bindings(
                ledger, "crown", now=PROJECTION_NOW, apply=True,
            )
            self.assertEqual(result["status"], "applied")
            frozen = ledger[wv.NAMESPACE]["conditions"][signature]
            self.assertEqual(
                frozen["pending_rollover_progress"]["eligible_decided"], 1,
            )
            self.assertTrue(build_manifest(ledger, "crown")["valid"])

    def test_strict_tamper_matrix_fails_closed(self):
        pristine, _specs, allowlist = self._installed_registry("footbreak")
        if allowlist != wv.CONDITION_IDENTITY_MIGRATION_ALLOWLIST:
            self.fail("working registry did not use production allowlist")
        else:
            active = pristine[wv.NAMESPACE]["condition_order"][2]
            mutations = {
                "duplicate identity": lambda value: value[wv.NAMESPACE][
                    "condition_order"
                ].append(active),
                "altered signature": lambda value: value[wv.NAMESPACE][
                    "conditions"
                ][active].update(signature="f" * 24),
                "altered definition": lambda value: value[wv.NAMESPACE][
                    "conditions"
                ][active]["definition"].update(role="tampered"),
                "altered evidence hash": lambda value: value[wv.NAMESPACE][
                    "conditions"
                ][active]["evidence_versions"][0].update(
                    evidence_hash="f" * 64,
                ),
                "extra registry identity": self._add_extra_identity,
                "missing registry identity": self._remove_last_identity,
            }
            for label, mutate in mutations.items():
                with self.subTest(label):
                    ledger = copy.deepcopy(pristine)
                    mutate(ledger)
                    manifest = build_manifest(ledger, "footbreak")
                    self.assertFalse(manifest["valid"], manifest)
                    self.assertEqual(
                        wv.formal_registry_candidates(
                            ledger, "footbreak", now=PROJECTION_NOW,
                        ),
                        [],
                    )

    def test_pending_count_hits_and_exclusion_tamper_fail_closed(self):
        for field in ("eligible_decided", "eligible_hits", "excluded"):
            with self.subTest(field):
                ledger, specs, _allowlist = self._installed_registry("crown")
                with patch.object(wv, "_now", return_value=PROJECTION_NOW):
                    candidate = wv.formal_registry_candidates(
                        ledger, "crown", now=PROJECTION_NOW,
                    )[0]
                    _append_settled(
                        self, ledger, "crown", candidate, specs[0], 1, 1,
                    )
                    wv.recompute_namespace(ledger, "crown")
                    frozen = ledger[wv.NAMESPACE]["conditions"][
                        candidate["__formal_frozen_signature"]
                    ]
                    if field == "excluded":
                        frozen["pending_rollover_progress"][field][
                            "not_binary_decided"
                        ] = 1
                    else:
                        frozen["pending_rollover_progress"][field] += 1
                    manifest = build_manifest(ledger, "crown")
                    self.assertFalse(manifest["valid"], manifest)
                    self.assertIn(
                        "pending_progress_mismatch",
                        manifest["rejection_reasons"],
                    )

    def test_retired_source_new_or_post_effective_activity_fails_closed(self):
        for label, row in (
            ("new row", {"created_at": None}),
            ("post-effective row", {"created_at": EFFECTIVE}),
        ):
            with self.subTest(label):
                ledger, _specs, allowlist = self._installed_registry(
                    "footbreak",
                )
                source = allowlist[0]["source_signature"]
                ledger["bets"].append({
                    "frozen_condition_signature": source,
                    "historical_row": label,
                    **row,
                })
                before = copy.deepcopy(
                    ledger[wv.NAMESPACE]["conditions"][source],
                )
                self.assertEqual(
                    wv.formal_registry_candidates(
                        ledger, "footbreak", now=PROJECTION_NOW,
                    ),
                    [],
                )
                with self.assertRaises(ValueError):
                    wv.recompute_namespace(ledger, "footbreak")
                self.assertEqual(
                    ledger[wv.NAMESPACE]["conditions"][source], before,
                )
                manifest = build_manifest(ledger, "footbreak")
                self.assertFalse(manifest["valid"], manifest)
                self.assertIn(
                    "condition_identity_migrations_historical_activity_drift",
                    manifest["rejection_reasons"],
                )

    @staticmethod
    def _add_extra_identity(ledger: dict) -> None:
        ns = ledger[wv.NAMESPACE]
        definition = wv.condition_definition(
            "footbreak", {
                "key": [
                    "system=footbreak", "market=CHL", "path=T-5",
                    "decision=T-5", "tier=≥1.70", "direction=A",
                    "role=角球大", "bucket=≥10.75", "movement=不變",
                ],
            },
        )
        signature, frozen = _frozen(definition, 18)
        ns["condition_order"].append(signature)
        ns["conditions"][signature] = frozen

    @staticmethod
    def _remove_last_identity(ledger: dict) -> None:
        ns = ledger[wv.NAMESPACE]
        signature = ns["condition_order"].pop()
        ns["conditions"].pop(signature)

    @staticmethod
    def _self_reseal_fixture_identity(fixture: dict) -> None:
        row = fixture["systems"]["crown"]["identities"][0]
        row["definition"]["role"] = "tampered-and-resealed"
        digest = hashlib.sha256(
            _canonical_bytes(row["definition"]),
        ).hexdigest()
        row["definition_hash"] = digest
        row["signature"] = digest[:24]


if __name__ == "__main__":
    unittest.main()
