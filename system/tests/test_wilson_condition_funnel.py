"""Adversarial read-only contract tests for the Wilson condition funnel."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from analysis.wilson_portfolio import evaluate
from analysis.wilson_validation import (
    CONDITION_AUDIT_LIMIT,
    _canonical_hash,
    _evidence_values,
    _expected_production_identity_manifest,
    _fixture_market_hash,
    _version_hash,
    admission_arithmetic,
    create_production_identity_manifest,
    project_condition_funnel,
)


def definition(market: str) -> dict:
    return {
        "system": "footbreak", "version": "granular-condition-v1",
        "market": market, "stage": "T-5", "path": "首預→T-30→T-5",
        "direction": "上升", "role": "主讓", "line_bucket": "0.5",
        "odds_tier": "中", "movement": "升", "odds_trajectory": "中→中",
        "miner_key": ["system=footbreak", f"market={market}", "stage=T-5"],
    }


def signature_for(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def evidence(signature: str, *, hits: int = 51, decided: int = 80,
             boundary: str = "2026-08-20T01:00:00+08:00") -> dict:
    values = _evidence_values(hits, decided)
    row = {
        "version": 1, "condition_signature": signature,
        "prior_version": None, "prior_evidence_hash": None,
        "batch_fixture_market_hashes": [], "batch_hits": 0, "batch_decided": 0,
        "cumulative_hits": hits, "cumulative_decided": decided,
        "wilson95_lower_raw": values["wilson95_lower_raw"],
        "minimum_acceptable_odds_raw": values["minimum_acceptable_odds_raw"],
        "minimum_acceptable_odds_display": values["display"]["minimum_acceptable_odds"],
        "activation_boundary_at": boundary, "created_at": boundary,
        "migration_baseline": True,
    }
    row["evidence_hash"] = _version_hash(row)
    return row


def active_pointer(row: dict) -> dict:
    return {key: copy.deepcopy(row.get(key)) for key in (
        "version", "cumulative_hits", "cumulative_decided", "wilson95_lower_raw",
        "minimum_acceptable_odds_raw", "minimum_acceptable_odds_display",
        "activation_boundary_at", "created_at", "evidence_hash",
    )}


def next_evidence(signature: str, prior: dict) -> dict:
    batch_hits, batch_decided = 12, 20
    hits = prior["cumulative_hits"] + batch_hits
    decided = prior["cumulative_decided"] + batch_decided
    values = _evidence_values(hits, decided)
    row = {
        "version": prior["version"] + 1, "condition_signature": signature,
        "prior_version": prior["version"],
        "prior_evidence_hash": prior["evidence_hash"],
        "batch_fixture_market_hashes": [f"{index:064x}" for index in range(1, 21)],
        "batch_hits": batch_hits, "batch_decided": batch_decided,
        "cumulative_hits": hits, "cumulative_decided": decided,
        "wilson95_lower_raw": values["wilson95_lower_raw"],
        "minimum_acceptable_odds_raw": values["minimum_acceptable_odds_raw"],
        "minimum_acceptable_odds_display": values["display"]["minimum_acceptable_odds"],
        "activation_boundary_at": "2026-08-23T01:00:00+08:00",
        "created_at": "2026-08-23T02:00:00+08:00",
    }
    row["evidence_hash"] = _version_hash(row)
    return row


def marker(signature: str, fixture_hash: str, stage_at: str,
           admitted: dict, *, include_admitted: bool = True) -> dict:
    value = {
        "schema_version": 1, "system": "footbreak",
        "condition_signature": signature, "native_pre_kickoff_t5": True,
        "stage_at": stage_at, "fixture_market_hash": fixture_hash,
    }
    if include_admitted:
        value["admitted_evidence_version"] = admitted["version"]
        value["admitted_evidence_hash"] = admitted["evidence_hash"]
    return value


def formal_row(signature: str, identity: str | None, fixture_hash: str, admitted: dict,
               *, result: str | None = None, status: str = "PENDING",
               observation: bool = False, include_admitted: bool = True,
               stage_at: str = "2026-08-22T10:00:00+08:00") -> dict:
    stage_time = datetime.fromisoformat(stage_at)
    created_at = (stage_time + timedelta(minutes=1)).isoformat()
    kickoff = (stage_time + timedelta(minutes=2)).isoformat()
    settled_at = (stage_time + timedelta(minutes=3)).isoformat()
    market = "HDC"
    frozen_definition = definition(market)
    odds = 1.1 if observation else 2.2
    arithmetic = admission_arithmetic(
        admitted["cumulative_hits"], admitted["cumulative_decided"], odds,
    )
    return {
        "bet_id": None if observation else identity,
        "observation_id": identity if observation else None,
        "portfolio": "footbreak_wilson_observations" if observation else "footbreak_wilson_test",
        "strategy": "wilson-test-strategy-v1", "formal_bet": not observation,
        "match_id": fixture_hash, "market": market, "code": market,
        "kickoff": kickoff,
        "created_at": created_at, "admission_at": created_at,
        "native_stage_at": stage_at,
        "frozen_condition_signature": signature,
        "frozen_condition_definition": frozen_definition,
        "condition_number": 2, "evidence_version": admitted["version"],
        "evidence_hash": admitted["evidence_hash"], "odds": odds,
        "frozen_historical_evidence": {
            "hits": admitted["cumulative_hits"],
            "decided": admitted["cumulative_decided"],
            "evidence_version": admitted["version"],
            "evidence_hash": admitted["evidence_hash"],
        },
        "wilson_admission": arithmetic, "stage": "T-5",
        "first_native_pre_kickoff_t5": True, "status": status, "result": result,
        "rollover_provenance": marker(
            signature, _fixture_market_hash("footbreak", fixture_hash, market),
            stage_at, admitted,
            include_admitted=include_admitted,
        ),
        **({"settled_at": settled_at} if status == "SETTLED" else {}),
    }


def frozen_condition(value: dict, number: int) -> tuple[str, dict]:
    signature = signature_for(value)
    version = evidence(signature)
    frozen = {
        "signature": signature, "condition_number": number,
        "frozen_at": "2026-08-20T01:00:00+08:00", "definition": copy.deepcopy(value),
        "evidence_versions": [version], "active_evidence_version": 1,
        "active_evidence_hash": version["evidence_hash"],
        "active_evidence": active_pointer(version),
        "pending_rollover_progress": {
            "eligible_decided": 0, "eligible_hits": 0, "accuracy": None,
            "required": 20, "display": "0/20", "excluded": {},
        },
    }
    return signature, frozen


class WilsonConditionFunnelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.signature, primary = frozen_condition(definition("HDC"), 2)
        self.other_signature, secondary = frozen_condition(definition("HIL"), 1)
        self.active = primary["evidence_versions"][0]
        self.valid_hash = "1" * 64
        duplicate_hash = "2" * 64
        primary["pending_rollover_progress"] = {
            "eligible_decided": 1, "eligible_hits": 1, "accuracy": 1.0,
            "required": 20, "display": "1/20",
            "excluded": {"missing_or_invalid_provenance": 0},
        }
        self.ledger = {
            "bets": [
                formal_row(self.signature, "valid", self.valid_hash, self.active,
                           result="Won", status="SETTLED"),
                formal_row(self.signature, "pending", "3" * 64, self.active),
                formal_row(self.signature, "dup-a", duplicate_hash, self.active,
                           result="Lost", status="SETTLED"),
                formal_row(self.signature, "dup-b", duplicate_hash, self.active,
                           result="Won", status="SETTLED"),
            ],
            "wilson_validation": {
                "schema_version": 2, "system": "footbreak",
                "activation_at": "2026-08-20T00:00:00+08:00",
                "condition_order": [self.other_signature, self.signature],
                "conditions": {self.signature: primary, self.other_signature: secondary},
                "observations": [
                    formal_row(self.signature, "refunded", "4" * 64, self.active,
                               result="Refunded", status="SETTLED", observation=True),
                ],
                "audit": [
                    {"match_id": "fixture-1", "market": "HDC", "status": "CREATED",
                     "reason": "wilson_candidate_frozen",
                     "ts": "2026-08-22T10:01:00+08:00",
                     "frozen_condition_signature": self.signature,
                     "exact_match_binding": {
                         "schema_version": 1, "condition_signature": self.signature,
                         "evidence_version": 1,
                         "evidence_hash": self.active["evidence_hash"],
                         "native_stage_at": "2026-08-22T10:00:00+08:00",
                         "definition_hash": _canonical_hash(primary["definition"]),
                     }},
                    {"match_id": "fixture-1", "market": "HDC", "status": "SKIPPED",
                     "reason": "idempotent_existing_market",
                     "frozen_condition_signature": self.signature},
                    {"match_id": "fixture-2", "market": "HDC", "status": "MATCHED_NO_BET",
                     "reason": "wilson_gate_not_passed",
                     "ts": "2026-08-22T10:01:00+08:00", "evidence_version": 1,
                     "frozen_condition_signature": self.signature,
                     "exact_match_binding": {
                         "schema_version": 1, "condition_signature": self.signature,
                         "evidence_version": 1,
                         "evidence_hash": self.active["evidence_hash"],
                         "native_stage_at": "2026-08-22T10:00:00+08:00",
                         "definition_hash": _canonical_hash(primary["definition"]),
                     }},
                    {"match_id": "global", "market": "*", "status": "SKIPPED",
                     "reason": "not_first_native_pre_kickoff_t5"},
                ],
            },
        }
        authorized, _validated, reason = _expected_production_identity_manifest(
            self.ledger["wilson_validation"], "footbreak",
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(authorized)
        create_production_identity_manifest(
            self.ledger, "footbreak", authorized_manifest=authorized,
        )

    def row(self, ledger: dict | None = None, index: int = 1) -> dict:
        return project_condition_funnel(ledger or self.ledger, "footbreak")["conditions"][index]

    def test_projection_is_pure_and_preserves_verified_identity_order(self) -> None:
        before = copy.deepcopy(self.ledger)
        payload = project_condition_funnel(self.ledger, "footbreak")
        self.assertEqual(self.ledger, before)
        self.assertTrue(payload["read_only"])
        self.assertEqual([r["condition_signature"] for r in payload["conditions"]],
                         [self.other_signature, self.signature])
        self.assertTrue(payload["conditions"][1]["identity_available"])

    def test_counts_use_only_exact_validated_persisted_evidence(self) -> None:
        stages = self.row()["stages"]
        self.assertEqual(stages["eligible_post_activation_t5_observations"]["reason"],
                         "condition_attribution_not_persisted_before_exact_match")
        self.assertEqual(stages["exact_condition_matches"]["availability"], "available")
        self.assertFalse(stages["exact_condition_matches"]["truncation_possible"])
        self.assertEqual(stages["exact_condition_matches"]["count"], 2)
        self.assertEqual(stages["recorded_formal_evidence"]["count"], 5)
        self.assertEqual(stages["recorded_formal_evidence"]["formal_bets"], 4)
        self.assertEqual(stages["recorded_formal_evidence"]["formal_observations"], 1)
        self.assertEqual(stages["settled_valid_evidence"]["count"], 1)
        self.assertEqual(stages["current_rollover_progress"]["display"], "1/20")

    def test_rejections_are_allowlisted_categorized_and_bounded(self) -> None:
        rejection = self.row()["rejections"]
        by_code = {item["code"]: item for item in rejection["items"]}
        self.assertEqual(by_code["wilson_gate_not_passed"]["category"], "execution_gate")
        self.assertEqual(by_code["idempotent_existing_market"]["count"], 1)
        self.assertEqual(by_code["not_binary_decided"]["count"], 1)
        self.assertEqual(by_code["duplicate_or_conflicting_fixture_market"]["count"], 2)
        self.assertLessEqual(len(rejection["items"]), 8)

    def test_definition_signature_drift_hides_current_identity(self) -> None:
        tampered = copy.deepcopy(self.ledger)
        tampered["wilson_validation"]["conditions"][self.signature]["definition"]["market"] = "CHL"
        payload = project_condition_funnel(tampered, "footbreak")
        self.assertEqual(payload["conditions"], [])
        self.assertIsNotNone(payload["unavailable_reason"])

    def test_corrupt_chain_hash_and_active_pointers_fail_closed(self) -> None:
        mutations = {
            "corrupt_tail": lambda frozen: frozen["evidence_versions"].append("bad"),
            "bad_hash": lambda frozen: frozen["evidence_versions"][0].__setitem__("evidence_hash", "bad"),
            "pointer_version": lambda frozen: frozen.__setitem__("active_evidence_version", 99),
            "pointer_hash": lambda frozen: frozen.__setitem__("active_evidence_hash", "f" * 64),
            "pointer_object": lambda frozen: frozen["active_evidence"].__setitem__("cumulative_hits", 999),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                tampered = copy.deepcopy(self.ledger)
                mutate(tampered["wilson_validation"]["conditions"][self.signature])
                payload = project_condition_funnel(tampered, "footbreak")
                self.assertEqual(payload["conditions"], [])
                self.assertIsNotNone(payload["unavailable_reason"])

    def test_prior_hash_sequence_and_cumulative_arithmetic_are_verified(self) -> None:
        valid = copy.deepcopy(self.ledger)
        frozen = valid["wilson_validation"]["conditions"][self.signature]
        second = next_evidence(self.signature, frozen["evidence_versions"][0])
        frozen["evidence_versions"].append(second)
        frozen["active_evidence_version"] = 2
        frozen["active_evidence_hash"] = second["evidence_hash"]
        frozen["active_evidence"] = active_pointer(second)
        frozen["pending_rollover_progress"].update(
            eligible_decided=0, eligible_hits=0, accuracy=None, display="0/20",
        )
        self.assertTrue(self.row(valid)["identity_available"])

        for field, value in (
            ("prior_evidence_hash", "f" * 64),
            ("prior_version", 99),
            ("cumulative_hits", second["cumulative_hits"] + 1),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(valid)
                tampered_second = tampered["wilson_validation"]["conditions"][self.signature]["evidence_versions"][1]
                tampered_second[field] = value
                tampered_second["evidence_hash"] = _version_hash(tampered_second)
                tampered_frozen = tampered["wilson_validation"]["conditions"][self.signature]
                tampered_frozen["active_evidence_hash"] = tampered_second["evidence_hash"]
                tampered_frozen["active_evidence"] = active_pointer(tampered_second)
                self.assertEqual(
                    project_condition_funnel(tampered, "footbreak")["conditions"], [],
                )

    def test_impossible_or_inexact_progress_is_unavailable(self) -> None:
        cases = [
            (20, 10, 0.5, "20/20"), (21, 10, 10 / 21, "21/20"),
            (1, 2, 2.0, "1/20"), (1, -1, -1.0, "1/20"),
            (1.0, 1, 1.0, "1/20"), (1, 1, 0.5, "1/20"),
            (1, 1, 1.0, "2/20"),
        ]
        for decided, hits, accuracy, display in cases:
            with self.subTest(decided=decided, hits=hits, accuracy=accuracy, display=display):
                tampered = copy.deepcopy(self.ledger)
                pending = tampered["wilson_validation"]["conditions"][self.signature]["pending_rollover_progress"]
                pending.update(eligible_decided=decided, eligible_hits=hits,
                               accuracy=accuracy, display=display)
                self.assertFalse(self.row(tampered)["stages"]["current_rollover_progress"]["available"])

    def test_nineteen_of_twenty_is_valid_only_when_rows_prove_it(self) -> None:
        ledger = copy.deepcopy(self.ledger)
        for index in range(2, 20):
            ledger["bets"].append(formal_row(
                self.signature, f"valid-{index}", f"{index:064x}", self.active,
                result="Won" if index <= 10 else "Lost", status="SETTLED",
                stage_at=f"2026-08-{22 + index // 10:02d}T{index % 10:02d}:00:00+08:00",
            ))
        pending = ledger["wilson_validation"]["conditions"][self.signature]["pending_rollover_progress"]
        pending.update(eligible_decided=19, eligible_hits=10, accuracy=10 / 19, display="19/20")
        self.assertEqual(self.row(ledger)["stages"]["current_rollover_progress"]["display"], "19/20")

    def test_blocked_or_stale_progress_is_not_presented_as_current(self) -> None:
        blocked = copy.deepcopy(self.ledger)
        pending = blocked["wilson_validation"]["conditions"][self.signature]["pending_rollover_progress"]
        pending["blocked_reason"] = "ambiguous_equal_stage_boundary"
        stage = self.row(blocked)["stages"]["current_rollover_progress"]
        self.assertFalse(stage["available"])
        self.assertEqual(stage["reason"], "persisted_rollover_progress_blocked")

        stale = copy.deepcopy(self.ledger)
        frozen = stale["wilson_validation"]["conditions"][self.signature]
        newer = evidence(self.signature, boundary="2026-08-24T00:00:00+08:00")
        frozen["evidence_versions"] = [newer]
        frozen["active_evidence_hash"] = newer["evidence_hash"]
        frozen["active_evidence"] = active_pointer(newer)
        self.assertEqual(project_condition_funnel(stale, "footbreak")["conditions"], [])

    def test_settlement_requires_exact_admitted_version_and_hash(self) -> None:
        for mode in ("missing", "fake"):
            with self.subTest(mode=mode):
                tampered = copy.deepcopy(self.ledger)
                marker_value = tampered["bets"][0]["rollover_provenance"]
                if mode == "missing":
                    marker_value.pop("admitted_evidence_version")
                    marker_value.pop("admitted_evidence_hash")
                else:
                    marker_value["admitted_evidence_version"] = 999
                    marker_value["admitted_evidence_hash"] = "f" * 64
                row = self.row(tampered)
                self.assertEqual(row["stages"]["settled_valid_evidence"]["count"], 0)
                codes = {item["code"] for item in row["rejections"]["items"]}
                self.assertIn("invalid_formal_admission_binding", codes)

    def test_every_formal_row_requires_complete_immutable_admission_binding(self) -> None:
        def future_stage(row):
            row["rollover_provenance"]["stage_at"] = "2099-08-22T10:00:00+08:00"
            row["native_stage_at"] = "2099-08-22T10:00:00+08:00"
            row["created_at"] = "2099-08-22T10:01:00+08:00"
            row["admission_at"] = "2099-08-22T10:01:00+08:00"
            row["kickoff"] = "2099-08-22T10:02:00+08:00"

        def fake_evidence_pointer(row):
            row["evidence_version"] = 999
            row["evidence_hash"] = "f" * 64
            row["rollover_provenance"]["admitted_evidence_version"] = 999
            row["rollover_provenance"]["admitted_evidence_hash"] = "f" * 64
            row["frozen_historical_evidence"]["evidence_version"] = 999
            row["frozen_historical_evidence"]["evidence_hash"] = "f" * 64

        attacks = {
            "recomputed_fixture_market_hash": lambda row: row[
                "rollover_provenance"
            ].__setitem__("fixture_market_hash", "f" * 64),
            "missing_rollover_marker": lambda row: row.__setitem__(
                "rollover_provenance", None
            ),
            "top_level_evidence_hash": lambda row: row.__setitem__(
                "evidence_hash", "f" * 64
            ),
            "top_level_native_stage": lambda row: row.__setitem__(
                "native_stage_at", "2026-08-22T09:59:00+08:00"
            ),
            "frozen_history_hash": lambda row: row[
                "frozen_historical_evidence"
            ].__setitem__("evidence_hash", "f" * 64),
            "frozen_definition": lambda row: row[
                "frozen_condition_definition"
            ].__setitem__("movement", "tampered"),
            "wilson_arithmetic": lambda row: row["wilson_admission"].__setitem__(
                "decided", 81
            ),
            "created_before_stage": lambda row: row.__setitem__(
                "created_at", "2026-08-22T09:59:00+08:00"
            ),
            "admission_after_created": lambda row: row.__setitem__(
                "admission_at", "2026-08-22T10:02:00+08:00"
            ),
            "created_after_kickoff": lambda row: row.__setitem__(
                "created_at", row["kickoff"]
            ),
            "impossible_future_stage": future_stage,
            "nonexistent_evidence_chain_member": fake_evidence_pointer,
        }
        for name, attack in attacks.items():
            with self.subTest(name=name):
                tampered = copy.deepcopy(self.ledger)
                attack(tampered["bets"][0])
                row = self.row(tampered)
                self.assertEqual(
                    row["stages"]["recorded_formal_evidence"]["count"], 4
                )
                by_code = {
                    item["code"]: item for item in row["rejections"]["items"]
                }
                self.assertEqual(
                    by_code["invalid_formal_admission_binding"]["count"], 1
                )

    def test_future_and_impossible_settlement_chronology_are_rejected(self) -> None:
        for name, settled_at in (
            ("before_kickoff", "2026-08-22T10:01:30+08:00"),
            ("future", "2099-08-22T10:03:00+08:00"),
        ):
            with self.subTest(name=name):
                tampered = copy.deepcopy(self.ledger)
                tampered["bets"][0]["settled_at"] = settled_at
                row = self.row(tampered)
                self.assertEqual(
                    row["stages"]["recorded_formal_evidence"]["count"], 5
                )
                self.assertEqual(
                    row["stages"]["settled_valid_evidence"]["count"], 0
                )
                self.assertIn(
                    "missing_or_invalid_provenance",
                    {item["code"] for item in row["rejections"]["items"]},
                )

    def test_actual_evaluate_audit_writer_projects_as_exact_match(self) -> None:
        ledger = copy.deepcopy(self.ledger)
        ledger["bets"] = []
        namespace = ledger["wilson_validation"]
        namespace["observations"] = []
        namespace["audit"] = []
        namespace["conditions"][self.signature]["pending_rollover_progress"].update(
            eligible_decided=0, eligible_hits=0, accuracy=None, display="0/20"
        )
        stage_at = "2026-08-22T10:00:00+08:00"
        kickoff = "2026-08-22T10:05:00+08:00"
        watch = {
            "match_id": "evaluate-fixture",
            "league": "測試聯賽",
            "home": "主隊",
            "away": "客隊",
            "kickoff": kickoff,
            "stages": [{
                "stage": "T-5",
                "ts": stage_at,
                "kickoff": kickoff,
                "market_predictions": [{
                    "code": "HDC", "side": "H", "line": -0.5, "odds": 2.2,
                    "quote_source": "persisted-native",
                    "observed_at": stage_at,
                }],
            }],
        }

        def parse_time(value):
            return datetime.fromisoformat(value) if isinstance(value, str) else None

        def admissions(_system, _market, selected, _matched, *, stage_at):
            return ([{
                "signature": self.signature,
                "definition": copy.deepcopy(
                    namespace["conditions"][self.signature]["definition"]
                ),
                "history": {
                    "hits": self.active["cumulative_hits"],
                    "decided": self.active["cumulative_decided"],
                    "artifact": {"hash": "persisted"},
                },
                "arithmetic": admission_arithmetic(
                    self.active["cumulative_hits"],
                    self.active["cumulative_decided"],
                    selected["odds"],
                ),
                "candidate": {},
            }], "wilson_pass")

        with (
            patch(
                "analysis.wilson_portfolio.formal_registry_candidates",
                return_value=[{"persisted": True}],
            ),
            patch(
                "analysis.wilson_portfolio.match_formal_registry",
                return_value={"evaluate-fixture": [{"persisted": True}]},
            ),
            patch(
                "analysis.wilson_portfolio.matching_admissions",
                side_effect=admissions,
            ),
        ):
            created, audit = evaluate(
                ledger,
                watch,
                system="footbreak",
                market_labels={"HDC": "讓球", "HIL": "入球", "CHL": "角球"},
                parse_time=parse_time,
                now="2026-08-22T10:01:00+08:00",
                ranking=None,
            )
        self.assertEqual(len(created), 1)
        ledger["bets"].extend(created)
        successful = next(row for row in audit if row["status"] == "CREATED")
        self.assertEqual(
            set(successful["exact_match_binding"]),
            {
                "schema_version", "condition_signature", "evidence_version",
                "evidence_hash", "native_stage_at", "definition_hash",
            },
        )
        projected = self.row(ledger)
        self.assertEqual(
            projected["stages"]["exact_condition_matches"]["count"], 1
        )
        self.assertEqual(
            projected["stages"]["recorded_formal_evidence"]["count"], 1
        )

    def test_identityless_formal_rows_are_excluded_with_diagnostic(self) -> None:
        ledger = copy.deepcopy(self.ledger)
        ledger["bets"].extend([
            formal_row(self.signature, None, "8" * 64, self.active),
            formal_row(self.signature, None, "9" * 64, self.active),
        ])
        row = self.row(ledger)
        self.assertEqual(row["stages"]["recorded_formal_evidence"]["count"], 5)
        by_code = {item["code"]: item for item in row["rejections"]["items"]}
        self.assertEqual(by_code["missing_formal_row_identity"]["count"], 2)

    def test_duplicate_order_and_condition_numbers_fail_whole_payload_closed(self) -> None:
        duplicate_order = copy.deepcopy(self.ledger)
        duplicate_order["wilson_validation"]["condition_order"].append(self.signature)
        payload = project_condition_funnel(duplicate_order, "footbreak")
        self.assertEqual(payload["conditions"], [])
        self.assertEqual(payload["unavailable_reason"], "frozen_condition_registry_malformed")

        duplicate_number = copy.deepcopy(self.ledger)
        duplicate_number["wilson_validation"]["conditions"][self.signature]["condition_number"] = 1
        payload = project_condition_funnel(duplicate_number, "footbreak")
        self.assertEqual(payload["unavailable_reason"], "frozen_condition_number_registry_malformed")

    def test_manifest_is_explicit_deterministic_immutable_and_required(self) -> None:
        missing = copy.deepcopy(self.ledger)
        missing["wilson_validation"].pop("production_identity_manifest")
        payload = project_condition_funnel(missing, "footbreak")
        self.assertEqual(
            payload["unavailable_reason"],
            "production_identity_manifest_unavailable_or_mismatch",
        )
        with self.assertRaises(ValueError):
            create_production_identity_manifest(missing, "footbreak")
        authorized = copy.deepcopy(
            self.ledger["wilson_validation"]["production_identity_manifest"]
        )
        created = create_production_identity_manifest(
            missing, "footbreak", authorized_manifest=authorized,
        )
        self.assertEqual(created, self.ledger["wilson_validation"]["production_identity_manifest"])
        self.assertTrue(created["immutable"])
        self.assertEqual(created["schema_version"], 1)
        self.assertEqual(created["manifest_version"], "wilson-production-identity-v1")
        self.assertEqual(
            create_production_identity_manifest(
                missing, "footbreak", trusted_manifest_hash=created["manifest_hash"],
            ),
            created,
        )
        missing["wilson_validation"]["production_identity_manifest"]["entries"][0][
            "condition_number"
        ] = 999
        with self.assertRaises(ValueError):
            create_production_identity_manifest(
                missing, "footbreak", authorized_manifest=authorized,
            )

    def test_manifest_bootstrap_rejects_reordered_or_stale_external_authority(self) -> None:
        authorized = copy.deepcopy(
            self.ledger["wilson_validation"]["production_identity_manifest"]
        )
        stale = copy.deepcopy(self.ledger)
        namespace = stale["wilson_validation"]
        namespace.pop("production_identity_manifest")
        namespace["condition_order"].reverse()
        for number, signature in enumerate(namespace["condition_order"], start=1):
            namespace["conditions"][signature]["condition_number"] = number
        with self.assertRaisesRegex(ValueError, "authorized.*mismatch"):
            create_production_identity_manifest(
                stale, "footbreak", authorized_manifest=authorized,
            )
        with self.assertRaisesRegex(ValueError, "trusted.*mismatch"):
            create_production_identity_manifest(
                stale, "footbreak", trusted_manifest_hash=authorized["manifest_hash"],
            )
        stale_screenshot = copy.deepcopy(authorized)
        stale_screenshot["entries"].reverse()
        with self.assertRaisesRegex(ValueError, "authorized.*mismatch"):
            create_production_identity_manifest(
                copy.deepcopy(self.ledger), "footbreak",
                authorized_manifest=stale_screenshot,
            )

    def test_manifest_rejects_swaps_arbitrary_numbers_order_and_entry_tampering(self) -> None:
        attacks = []
        swapped = copy.deepcopy(self.ledger)
        first, second = swapped["wilson_validation"]["condition_order"]
        conditions = swapped["wilson_validation"]["conditions"]
        conditions[first]["condition_number"], conditions[second]["condition_number"] = (
            conditions[second]["condition_number"], conditions[first]["condition_number"],
        )
        attacks.append(swapped)
        arbitrary = copy.deepcopy(self.ledger)
        arbitrary["wilson_validation"]["conditions"][self.signature]["condition_number"] = 999
        attacks.append(arbitrary)
        reordered = copy.deepcopy(self.ledger)
        reordered["wilson_validation"]["condition_order"].reverse()
        attacks.append(reordered)
        for key in ("missing", "extra", "definition_hash", "initial_evidence_hash"):
            value = copy.deepcopy(self.ledger)
            entries = value["wilson_validation"]["production_identity_manifest"]["entries"]
            if key == "missing":
                entries.pop()
            elif key == "extra":
                entries.append(copy.deepcopy(entries[0]))
            else:
                entries[0][key] = "f" * 64
            attacks.append(value)
        for attack in attacks:
            with self.subTest(index=attacks.index(attack)):
                payload = project_condition_funnel(attack, "footbreak")
                self.assertEqual(payload["conditions"], [])
                self.assertIsNotNone(payload["unavailable_reason"])

    def test_rewritten_initial_evidence_cannot_self_authenticate(self) -> None:
        tampered = copy.deepcopy(self.ledger)
        frozen = tampered["wilson_validation"]["conditions"][self.signature]
        version = frozen["evidence_versions"][0]
        version["cumulative_hits"] = version["cumulative_decided"] = 1
        values = _evidence_values(1, 1)
        version["wilson95_lower_raw"] = values["wilson95_lower_raw"]
        version["minimum_acceptable_odds_raw"] = values["minimum_acceptable_odds_raw"]
        version["minimum_acceptable_odds_display"] = values["display"]["minimum_acceptable_odds"]
        version["evidence_hash"] = _version_hash(version)
        frozen["active_evidence_hash"] = version["evidence_hash"]
        frozen["active_evidence"] = active_pointer(version)
        payload = project_condition_funnel(tampered, "footbreak")
        self.assertEqual(
            payload["unavailable_reason"],
            "production_identity_manifest_unavailable_or_mismatch",
        )

    def test_typed_formal_identity_normalization_precedes_all_counts(self) -> None:
        identical = copy.deepcopy(self.ledger)
        duplicate = copy.deepcopy(identical["bets"][0])
        identical["bets"].append(duplicate)
        pending = identical["wilson_validation"]["conditions"][self.signature][
            "pending_rollover_progress"
        ]
        pending.update(eligible_decided=0, eligible_hits=0, accuracy=None, display="0/20")
        row = self.row(identical)
        self.assertEqual(row["stages"]["recorded_formal_evidence"]["count"], 4)
        self.assertEqual(row["stages"]["settled_valid_evidence"]["count"], 0)
        by_code = {item["code"]: item for item in row["rejections"]["items"]}
        self.assertEqual(
            by_code["duplicate_or_conflicting_formal_identity"]["count"], 2,
        )

        conflict = copy.deepcopy(self.ledger)
        conflict["bets"] = [
            formal_row(self.signature, "same-id", "8" * 64, self.active,
                       result="Won", status="SETTLED"),
            formal_row(self.signature, "same-id", "9" * 64, self.active,
                       result="Lost", status="SETTLED"),
        ]
        conflict["wilson_validation"]["observations"] = []
        pending = conflict["wilson_validation"]["conditions"][self.signature][
            "pending_rollover_progress"
        ]
        pending.update(eligible_decided=0, eligible_hits=0, accuracy=None, display="0/20")
        row = self.row(conflict)
        self.assertEqual(row["stages"]["recorded_formal_evidence"]["count"], 0)
        self.assertEqual(row["stages"]["settled_valid_evidence"]["count"], 0)
        self.assertEqual(row["stages"]["current_rollover_progress"]["display"], "0/20")
        by_code = {item["code"]: item for item in row["rejections"]["items"]}
        self.assertEqual(
            by_code["duplicate_or_conflicting_formal_identity"]["count"], 2,
        )

        result_conflict = copy.deepcopy(self.ledger)
        result_conflict["bets"] = [
            formal_row(self.signature, "same-id", "8" * 64, self.active,
                       result="Won", status="SETTLED"),
            formal_row(self.signature, "same-id", "8" * 64, self.active,
                       result="Lost", status="SETTLED"),
        ]
        result_conflict["wilson_validation"]["observations"] = []
        pending = result_conflict["wilson_validation"]["conditions"][self.signature][
            "pending_rollover_progress"
        ]
        pending.update(eligible_decided=0, eligible_hits=0, accuracy=None, display="0/20")
        row = self.row(result_conflict)
        self.assertEqual(row["stages"]["recorded_formal_evidence"]["count"], 0)
        self.assertEqual(row["stages"]["settled_valid_evidence"]["count"], 0)

        cross_class = copy.deepcopy(self.ledger)
        cross_class["bets"] = [
            formal_row(self.signature, "typed-id", "8" * 64, self.active),
        ]
        cross_class["wilson_validation"]["observations"] = [
            formal_row(
                self.signature, "typed-id", "9" * 64, self.active, observation=True,
            ),
        ]
        row = self.row(cross_class)
        self.assertEqual(row["stages"]["recorded_formal_evidence"]["count"], 2)
        self.assertEqual(row["stages"]["recorded_formal_evidence"]["formal_bets"], 1)
        self.assertEqual(row["stages"]["recorded_formal_evidence"]["formal_observations"], 1)

    def test_exact_matches_exclude_boundary_and_integrity_rejections(self) -> None:
        ledger = copy.deepcopy(self.ledger)
        ledger["wilson_validation"]["audit"].extend([
            {"ts": "2020-01-01T00:00:00+08:00", "match_id": "ancient",
             "market": "HDC", "status": "SKIPPED",
             "reason": "stage_not_strictly_after_evidence_activation_boundary",
             "frozen_condition_signature": self.signature},
            {"match_id": "invalid", "market": "HDC", "status": "SKIPPED",
             "reason": "active_evidence_unavailable",
             "frozen_condition_signature": self.signature},
        ])
        self.assertEqual(
            self.row(ledger)["stages"]["exact_condition_matches"]["count"], 2,
        )

    def test_impossible_evidence_chronology_fails_closed(self) -> None:
        cases = []
        created_before_boundary = copy.deepcopy(self.ledger)
        frozen = created_before_boundary["wilson_validation"]["conditions"][self.signature]
        frozen["evidence_versions"][0]["created_at"] = "2020-01-01T00:00:00+08:00"
        frozen["active_evidence"] = active_pointer(frozen["evidence_versions"][0])
        cases.append(created_before_boundary)

        for mode in ("equal_boundary", "regressed_boundary", "regressed_created"):
            value = copy.deepcopy(self.ledger)
            frozen = value["wilson_validation"]["conditions"][self.signature]
            second = next_evidence(self.signature, frozen["evidence_versions"][0])
            if mode == "equal_boundary":
                second["activation_boundary_at"] = frozen["evidence_versions"][0][
                    "activation_boundary_at"
                ]
            elif mode == "regressed_boundary":
                second["activation_boundary_at"] = "2020-01-01T00:00:00+08:00"
            else:
                second["created_at"] = "2020-01-01T00:00:00+08:00"
            second["evidence_hash"] = _version_hash(second)
            frozen["evidence_versions"].append(second)
            frozen["active_evidence_version"] = 2
            frozen["active_evidence_hash"] = second["evidence_hash"]
            frozen["active_evidence"] = active_pointer(second)
            cases.append(value)
        for value in cases:
            self.assertEqual(
                project_condition_funnel(value, "footbreak")["conditions"], [],
            )

    def test_malformed_schema_and_formal_containers_fail_closed(self) -> None:
        for mutation in (
            lambda value: value["wilson_validation"].__setitem__("schema_version", 1),
            lambda value: value.__setitem__("bets", {}),
            lambda value: value["wilson_validation"].__setitem__("observations", {}),
        ):
            tampered = copy.deepcopy(self.ledger)
            mutation(tampered)
            payload = project_condition_funnel(tampered, "footbreak")
            self.assertEqual(payload["conditions"], [])
            self.assertIsNotNone(payload["unavailable_reason"])

    def test_audit_is_exact_bounded_only_with_proof_or_unavailable(self) -> None:
        exact = self.row()["stages"]["exact_condition_matches"]
        self.assertEqual(exact["availability"], "available")
        self.assertFalse(exact["truncation_possible"])

        full = copy.deepcopy(self.ledger)
        full["wilson_validation"]["audit"] = [
            {"match_id": f"m-{i}", "market": "HDC",
             "frozen_condition_signature": self.signature}
            for i in range(CONDITION_AUDIT_LIMIT)
        ]
        unavailable = self.row(full)["stages"]["exact_condition_matches"]
        self.assertFalse(unavailable["available"])
        self.assertIn("completeness_unproven", unavailable["reason"])
        full["wilson_validation"]["audit_retention"] = {
            "retained_limit": CONDITION_AUDIT_LIMIT, "dropped_count": 3,
        }
        bounded = self.row(full)["stages"]["exact_condition_matches"]
        self.assertEqual(bounded["availability"], "bounded")
        self.assertTrue(bounded["truncation_possible"])

    def test_missing_namespace_is_explicitly_unavailable(self) -> None:
        payload = project_condition_funnel({"bets": []}, "footbreak")
        self.assertEqual(payload["conditions"], [])
        self.assertEqual(payload["unavailable_reason"], "wilson_namespace_unavailable")


if __name__ == "__main__":
    unittest.main()
