from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import datetime

from analysis.wilson_portfolio import evaluate_stage
from analysis.wilson_registry_manifest import build_manifest
from analysis.wilson_validation import (
    _evidence_values,
    _fixture_market_hash,
    _version_hash,
    admission_arithmetic,
    active_observations,
    create_production_identity_manifest,
    formal_matcher_axes,
    match_formal_registry,
    project_condition_funnel,
    recompute_namespace,
)


SIGNATURE = "0bbe71d0bd504305f07d0b9e"
DEFINITION_HASH = (
    "0bbe71d0bd504305f07d0b9ea5b3776331ec5074cc20425e1d3cc7f16fc73b95"
)
NOW = "2026-08-25T12:00:00+08:00"
STAGE_AT = "2026-08-25T10:00:00+08:00"
KICKOFF = "2026-08-25T20:00:00+08:00"


def definition() -> dict:
    return {
        "direction": "A",
        "line_bucket": "≤2.5",
        "market": "HIL",
        "miner_key": [
            "system=footbreak", "market=HIL", "path=首預", "decision=首預",
            "tier=<1.70", "direction=A", "role=大", "bucket=≤2.5",
            "movement=不變",
        ],
        "movement": "不變",
        "odds_tier": "<1.70",
        "odds_trajectory": "",
        "path": "首預",
        "role": "大",
        "stage": "首預",
        "system": "footbreak",
        "version": "granular-condition-v1",
    }


def ledger() -> dict:
    frozen_definition = definition()
    raw = json.dumps(
        frozen_definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    assert hashlib.sha256(raw.encode()).hexdigest() == DEFINITION_HASH
    values = _evidence_values(94, 141)
    version = {
        "version": 1,
        "condition_signature": SIGNATURE,
        "prior_version": None,
        "prior_evidence_hash": None,
        "batch_fixture_market_hashes": [],
        "batch_hits": 0,
        "batch_decided": 0,
        "cumulative_hits": 94,
        "cumulative_decided": 141,
        "wilson95_lower_raw": values["wilson95_lower_raw"],
        "minimum_acceptable_odds_raw": values["minimum_acceptable_odds_raw"],
        "minimum_acceptable_odds_display": values["display"]["minimum_acceptable_odds"],
        "activation_boundary_at": "2026-08-20T23:48:58+08:00",
        "created_at": "2026-08-20T23:48:58+08:00",
        "migration_baseline": True,
    }
    version["evidence_hash"] = _version_hash(version)
    active = {
        key: copy.deepcopy(version.get(key))
        for key in (
            "version", "cumulative_hits", "cumulative_decided",
            "wilson95_lower_raw", "minimum_acceptable_odds_raw",
            "minimum_acceptable_odds_display", "activation_boundary_at",
            "created_at", "evidence_hash",
        )
    }
    result = {
        "bets": [],
        "wilson_validation": {
            "schema_version": 2,
            "system": "footbreak",
            "activation_at": "2026-08-20T23:48:58+08:00",
            "condition_order": [SIGNATURE],
            "conditions": {
                SIGNATURE: {
                    "signature": SIGNATURE,
                    "condition_number": 1,
                    "frozen_at": "2026-08-20T23:48:58+08:00",
                    "definition": frozen_definition,
                    "historical_evidence": {
                        "hits": 94,
                        "decided": 141,
                        "pushes": 0,
                        "artifact": {
                            "hash": "a" * 64,
                            "version": "footbreak-history-v1",
                            "as_of": "2026-08-20T23:48:58+08:00",
                        },
                    },
                    "evidence_versions": [version],
                    "active_evidence_version": 1,
                    "active_evidence_hash": version["evidence_hash"],
                    "active_evidence": active,
                },
            },
            "observations": [],
            "audit": [],
        },
    }
    return result


def watch() -> dict:
    return {
        "match_id": "natural-first-look",
        "league": "測試聯賽",
        "home": "主隊",
        "away": "客隊",
        "kickoff": KICKOFF,
        "stages": [{
            "stage": "首預",
            "ts": STAGE_AT,
            "kickoff_hkt": KICKOFF,
            "market_predictions": [{
                "code": "HIL",
                "side": "H",
                "line": 2.5,
                "odds": 1.50,
                "observed_at": "2026-08-25T09:59:00+08:00",
                "source": "hkjc_public_board",
            }],
        }],
    }


class StageAwareWilsonTests(unittest.TestCase):
    @staticmethod
    def parse_time(raw):
        try:
            return datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None

    def evaluate(self, value: dict, card: dict, stage: str = "首預"):
        return evaluate_stage(
            value,
            card,
            system="footbreak",
            market_labels={"HDC": "讓球", "HIL": "入球大細", "CHL": "角球大細"},
            parse_time=self.parse_time,
            now=NOW,
            ranking=None,
            decision_stage=stage,
        )

    @staticmethod
    def canonical_clone(
        template, index, stage_at, version, evidence_hash, hits, decided,
    ):
        row = copy.deepcopy(template)
        fixture = f"parity-{version}-{index:02d}"
        row.update({
            "match_id": fixture,
            "observation_id": (
                f"{fixture}|HIL|首預|{SIGNATURE}|formal-observation"
            ),
            "status": "SETTLED", "result": "Won",
            "settled_at": "2026-08-25T21:00:00+08:00",
            "native_stage_at": stage_at,
            "evidence_version": version, "evidence_hash": evidence_hash,
        })
        row["rollover_provenance"].update({
            "stage_at": stage_at,
            "fixture_market_hash": _fixture_market_hash(
                "footbreak", fixture, "HIL",
            ),
            "admitted_evidence_version": version,
            "admitted_evidence_hash": evidence_hash,
        })
        row["frozen_historical_evidence"].update({
            "hits": hits, "decided": decided,
            "evidence_version": version, "evidence_hash": evidence_hash,
        })
        row["wilson_admission"] = admission_arithmetic(
            hits, decided, row["odds"],
        )
        return row

    def test_condition_three_natural_first_look_observation_and_idempotence(self):
        value = ledger()
        made, audit = self.evaluate(value, watch())
        self.assertEqual(made, [])
        self.assertEqual(len(value["bets"]), 0)
        observations = active_observations(value, "footbreak")
        self.assertEqual(len(observations), 1)
        row = observations[0]
        self.assertEqual(row["frozen_condition_signature"], SIGNATURE)
        self.assertEqual(
            row["observation_id"],
            f"natural-first-look|HIL|首預|{SIGNATURE}|formal-observation",
        )
        self.assertEqual(row["stage"], "首預")
        self.assertEqual(row["bet_status"], "FORMAL_OBSERVATION")
        self.assertFalse(row["formal_bet"])
        self.assertTrue(row["simulation_only"])
        self.assertFalse(row["real_betting_enabled"])
        for forbidden in ("stake", "turnover", "pnl", "bankroll"):
            self.assertNotIn(forbidden, row)
        self.assertEqual(
            next(item for item in audit if item["status"] == "MATCHED_NO_BET")["reason"],
            "early_stage_formal_observation",
        )
        repeated, _ = self.evaluate(value, watch())
        self.assertEqual(repeated, [])
        self.assertEqual(len(active_observations(value, "footbreak")), 1)

    def test_production_shaped_condition_three_keeps_number_and_identity(self):
        value = ledger()
        value["wilson_validation"]["conditions"][SIGNATURE]["condition_number"] = 3
        self.evaluate(value, watch())
        row = active_observations(value, "footbreak")[0]
        self.assertEqual(row["condition_number"], 3)
        self.assertEqual(row["frozen_condition_signature"], SIGNATURE)
        self.assertEqual(
            hashlib.sha256(json.dumps(
                row["frozen_condition_definition"], ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest(),
            DEFINITION_HASH,
        )

    def test_stage_isolation_later_snapshots_cannot_change_first_look(self):
        value = ledger()
        card = watch()
        self.evaluate(value, card)
        original = copy.deepcopy(active_observations(value, "footbreak")[0])
        for stage, minute, odds in (("T-30", "19:30", 2.4), ("T-5", "19:55", 2.8)):
            card["stages"].append({
                "stage": stage,
                "ts": f"2026-08-25T{minute}:00+08:00",
                "kickoff_hkt": KICKOFF,
                "market_predictions": [{
                    "code": "HIL", "side": "L", "line": 3.0, "odds": odds,
                    "observed_at": f"2026-08-25T{minute}:00+08:00",
                    "source": "hkjc_public_board",
                }],
            })
        self.evaluate(value, card, "T-30")
        self.evaluate(value, card, "T-5")
        self.assertEqual(active_observations(value, "footbreak"), [original])

    def test_settlement_x20_registry_and_funnel_accept_stage_provenance(self):
        value = ledger()
        self.evaluate(value, watch())
        row = value["wilson_validation"]["observations"][0]
        row.update(
            status="SETTLED", result="Won",
            settled_at="2026-08-25T20:20:00+08:00",
        )
        recompute_namespace(value, "footbreak")
        frozen = value["wilson_validation"]["conditions"][SIGNATURE]
        self.assertEqual(frozen["pending_rollover_progress"]["display"], "1/20")
        manifest = build_manifest(value, "footbreak")
        condition = manifest["conditions"][0]
        self.assertTrue(condition["own_stage_matcher_can_structurally_admit"])
        self.assertFalse(condition["current_matcher_can_structurally_admit"])
        self.assertEqual(condition["formal_rows"], 1)
        self.assertEqual(condition["prospective_x20"]["decided"], 1)
        expected, _validated, reason = (
            __import__(
                "analysis.wilson_validation", fromlist=["_expected_production_identity_manifest"],
            )._expected_production_identity_manifest(
                value["wilson_validation"], "footbreak",
            )
        )
        self.assertIsNone(reason)
        create_production_identity_manifest(
            value, "footbreak", authorized_manifest=expected,
        )
        funnel = project_condition_funnel(value, "footbreak")
        self.assertEqual(
            funnel["conditions"][0]["stages"]["recorded_formal_evidence"]["count"], 1,
        )
        self.assertEqual(
            funnel["conditions"][0]["stages"]["settled_valid_evidence"]["count"], 1,
        )

    def test_level_two_multistage_identity_without_tier_path_uses_own_stage(self):
        signatures = [
            "d3e112d8815b6e9df46e075d", "033986661061d8b486cff464",
            "785894424f74f7d3bf9f79e9", "715edfc03f06dba83f50d204",
            "76a34f2b71c6eedfba4b2496", "51717d4b680bbcb59d83a3ff",
            "91192678300cb3c01cd4a836", "09ba238cb8400670519ce95a",
            "36adac14f977adf9d9a3c70f", "f956f75e552c8de37b0f2656",
            "eed2751d41fde707cbb37002", "6fbac6848286d7fd9756b1fe",
            "c1a159dfd3c39224996e16a4", "fc2c52e9252aec081522f7a9",
        ]
        self.assertEqual(len(signatures), 14)
        for signature in signatures:
            system = "footbreak" if signature in set(signatures[:5]) else "crown"
            level_two = {
                "key": [
                    f"system={system}", "market=HIL", "path=首預→T-30",
                    "decision=T-30", "direction=A→A", "role=大",
                    "bucket=≤2.5", "tier=<1.70", "movement=不變",
                ],
            }
            axes = formal_matcher_axes(
                level_two, system=system, decision_stage="T-30",
            )
            self.assertIsNotNone(axes, signature)
            self.assertNotIn("tier_path", axes)
            for stage in ("首預", "T-5"):
                self.assertIsNone(
                    formal_matcher_axes(
                        level_two, system=system, decision_stage=stage,
                    ),
                    (signature, stage),
                )

    def test_present_multistage_tier_path_remains_strict(self):
        base = [
            "system=crown", "market=HIL", "path=首預→T-30",
            "decision=T-30", "direction=A→A", "role=大",
            "bucket=2.75–3.0", "tier=≥1.70", "movement=不變",
        ]
        for tier_path in ("低", "≥1.70→低", "低→≥1.70→≥1.70"):
            with self.subTest(tier_path=tier_path):
                self.assertIsNone(formal_matcher_axes(
                    {"key": base + [f"tier_path={tier_path}"]},
                    system="crown", decision_stage="T-30",
                ))
        axes = formal_matcher_axes(
            {"key": base + ["tier_path=低→≥1.70"]},
            system="crown", decision_stage="T-30",
        )
        self.assertEqual(axes["tier_path"], "低→≥1.70")

    def test_complete_t30_trajectory_matches_only_at_t30(self):
        candidate = {
            "__formal_frozen_signature": "t30-complete",
            "key": [
                "system=crown", "market=HDC", "path=首預→T-30",
                "decision=T-30", "tier=≥1.70", "direction=A→A",
                "role=主讓", "bucket=0.25–0.5", "movement=不變",
                "tier_path=≥1.70→≥1.70",
            ],
        }
        rows = []
        for stage, stamp in (
            ("首預", "2026-08-25T09:00:00+08:00"),
            ("T-30", "2026-08-25T19:30:00+08:00"),
        ):
            rows.append({
                "match_id": "t30-natural", "stage": stage, "kickoff": KICKOFF,
                "predicted_at": stamp,
                "market_predictions": [{
                    "code": "HDC", "side": "H", "line": -0.25, "odds": 1.8,
                }],
            })
        self.assertEqual(
            len(match_formal_registry(
                rows, [candidate], system="crown", decision_stage="T-30",
            )["t30-natural"]),
            1,
        )
        self.assertEqual(
            match_formal_registry(
                rows, [candidate], system="crown", decision_stage="首預",
            ),
            {},
        )
        self.assertEqual(
            match_formal_registry(
                rows, [candidate], system="crown", decision_stage="T-5",
            ),
            {},
        )

    def test_reverse_or_post_snapshot_quote_chronology_never_matches(self):
        candidate = {
            "__formal_frozen_signature": "chrono",
            "key": [
                "system=crown", "market=HDC", "path=首預→T-30",
                "decision=T-30", "tier=≥1.70", "direction=A→A",
                "role=主讓", "bucket=0.25–0.5", "movement=不變",
                "tier_path=≥1.70→≥1.70",
            ],
        }
        def row(stage, saved, observed):
            return {
                "match_id": "chrono", "stage": stage, "kickoff": KICKOFF,
                "predicted_at": saved,
                "market_predictions": [{
                    "code": "HDC", "side": "H", "line": -0.25, "odds": 1.8,
                    "observed_at": observed,
                }],
            }
        reverse = [
            row("首預", "2026-08-25T19:40:00+08:00", "2026-08-25T19:39:00+08:00"),
            row("T-30", "2026-08-25T19:30:00+08:00", "2026-08-25T19:29:00+08:00"),
        ]
        post_snapshot = [
            row("首預", "2026-08-25T18:00:00+08:00", "2026-08-25T18:01:00+08:00"),
            row("T-30", "2026-08-25T19:30:00+08:00", "2026-08-25T19:29:00+08:00"),
        ]
        for rows in (reverse, post_snapshot):
            self.assertEqual(
                match_formal_registry(
                    rows, [candidate], system="crown", decision_stage="T-30",
                ),
                {},
            )

    def test_runtime_rollover_rejects_forged_rows_before_version_mutation(self):
        value = ledger()
        self.evaluate(value, watch())
        template = active_observations(value, "footbreak")[0]
        forged = []
        for index in range(20):
            row = copy.deepcopy(template)
            fixture = f"forged-{index:02d}"
            row.update({
                "match_id": fixture,
                "observation_id": f"noncanonical-{index}",
                "status": "SETTLED",
                "result": "Won",
                "settled_at": "2026-08-25T09:00:00+08:00",
                "evidence_version": 999,
                "evidence_hash": "f" * 64,
            })
            row["frozen_historical_evidence"].update({
                "evidence_version": 999, "evidence_hash": "f" * 64,
            })
            row["rollover_provenance"].update({
                "fixture_market_hash": _fixture_market_hash(
                    "footbreak", fixture, "HIL",
                ),
                "admitted_evidence_version": 999,
                "admitted_evidence_hash": "f" * 64,
            })
            forged.append(row)
        value["wilson_validation"]["observations"] = forged
        frozen = value["wilson_validation"]["conditions"][SIGNATURE]
        before = copy.deepcopy(frozen)
        recompute_namespace(value, "footbreak")
        self.assertEqual(frozen, before)
        self.assertEqual(frozen["active_evidence_version"], 1)

    def test_runtime_rejects_immutable_historical_artifact_mismatch(self):
        value = ledger()
        self.evaluate(value, watch())
        template = active_observations(value, "footbreak")[0]
        frozen = value["wilson_validation"]["conditions"][SIGNATURE]
        v1 = frozen["evidence_versions"][0]
        rows = [
            self.canonical_clone(
                template, index, f"2026-08-25T10:{index:02d}:00+08:00",
                1, v1["evidence_hash"], 94, 141,
            )
            for index in range(20)
        ]
        for row in rows:
            row["frozen_historical_evidence"]["artifact"]["version"] = "tampered"
        value["wilson_validation"]["observations"] = rows
        before = copy.deepcopy(frozen)
        recompute_namespace(value, "footbreak")
        self.assertEqual(frozen, before)

    def test_runtime_rejects_evidence_version_before_creation_window(self):
        value = ledger()
        self.evaluate(value, watch())
        template = active_observations(value, "footbreak")[0]
        frozen = value["wilson_validation"]["conditions"][SIGNATURE]
        v1 = frozen["evidence_versions"][0]
        first = [
            self.canonical_clone(
                template, index, f"2026-08-25T10:{index:02d}:00+08:00",
                1, v1["evidence_hash"], 94, 141,
            )
            for index in range(20)
        ]
        value["wilson_validation"]["observations"] = first
        recompute_namespace(value, "footbreak")
        self.assertEqual(frozen["active_evidence_version"], 2)
        v2 = frozen["evidence_versions"][1]
        second = [
            self.canonical_clone(
                template, 20 + index,
                f"2026-08-25T10:{20 + index:02d}:00+08:00",
                2, v2["evidence_hash"],
                v2["cumulative_hits"], v2["cumulative_decided"],
            )
            for index in range(20)
        ]
        value["wilson_validation"]["observations"] = first + second
        before = copy.deepcopy(frozen)
        recompute_namespace(value, "footbreak")
        self.assertEqual(frozen, before)
        self.assertEqual(frozen["active_evidence_version"], 2)


if __name__ == "__main__":
    unittest.main()
