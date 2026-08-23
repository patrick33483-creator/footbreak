"""Focused tests for Stage 4: crown/native_stage_staging_acceptance.py.

Every test constructs its own temporary state dir and ledger file -- no test
here ever touches a real environment variable, a real production path, or
the network. All fixture identities and timestamps are synthetic.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from crown.common import HKT, iso_hkt
from crown.config import settings
from crown import native_stage_store as store_mod
from crown import native_stage_staging_acceptance as acc
from crown.native_stage_reconciliation import FixtureLookup, ReconciliationStatus


def _config(state_dir: Path) -> "acc.Settings":
    return replace(settings(), state_dir=state_dir)


def _kickoff(minutes_from_now: int, now: datetime) -> datetime:
    return now + timedelta(minutes=minutes_from_now)


def _write_ledger(path: Path, watch: dict | None = None) -> Path:
    path.write_text(json.dumps({"watch": watch or {}}), encoding="utf-8")
    return path


def _seed_committed(store: store_mod.NativeStageStore, match_id: str, stage: str, kickoff: datetime) -> None:
    store.mark_started(match_id, stage, kickoff=kickoff)
    store.commit_snapshot(
        match_id, stage,
        {"match_id": match_id, "selected_odds_journal": [
            {"code": "HDC", "line": -0.25, "side": "H", "odds": 1.91, "source": "s", "observed_at": 1},
        ]},
        kickoff=kickoff,
    )


class ConsistentBatchTests(unittest.TestCase):
    """Requirement: deterministic output over a consistent batch."""

    def test_all_shadow_only_batch_reports_expected_aggregates(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            for i in range(20):
                _seed_committed(store, f"C{i}", "T-5", kickoff)
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            lookups = [FixtureLookup(f"C{i}", "T-5") for i in range(20)]
            report = acc.build_staging_acceptance_report(config, lookups, ledger=ledger_path, now=now)
            self.assertEqual(report["compared"], 20)
            self.assertEqual(report["reconciliation_aggregate"]["SHADOW_ONLY"], 20)
            self.assertEqual(report["attempt_state_aggregate"]["COMMITTED"], 20)
            self.assertEqual(report["terminal_completeness_ratio"], 1.0)
            self.assertEqual(report["conflict_count"], 0)
            self.assertEqual(report["post_kickoff_violation_count"], 0)
            self.assertEqual(report["provider_calls_made"], 0)
            self.assertEqual(report["writes_performed"], 0)

    def test_repeated_report_is_deterministic_aside_from_timing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "D1", "T-5", kickoff)
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            lookups = [FixtureLookup("D1", "T-5")]
            r1 = acc.build_staging_acceptance_report(config, lookups, ledger=ledger_path, now=now)
            r2 = acc.build_staging_acceptance_report(config, lookups, ledger=ledger_path, now=now)
            self.assertEqual(r1["reconciliation_aggregate"], r2["reconciliation_aggregate"])
            self.assertEqual(r1["attempt_state_aggregate"], r2["attempt_state_aggregate"])
            self.assertEqual(
                [{k: v for k, v in row.items() if k != "read_latency_seconds"} for row in r1["fixtures"]],
                [{k: v for k, v in row.items() if k != "read_latency_seconds"} for row in r2["fixtures"]],
            )

    def test_report_never_writes_to_state_dir_or_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "W1", "T-5", kickoff)
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            before_ledger = ledger_path.read_text(encoding="utf-8")
            before_shard = store.path_for("W1").read_text(encoding="utf-8")
            acc.build_staging_acceptance_verdict(
                config, [FixtureLookup("W1", "T-5")], ledger=ledger_path, now=now,
            )
            self.assertEqual(ledger_path.read_text(encoding="utf-8"), before_ledger)
            self.assertEqual(store.path_for("W1").read_text(encoding="utf-8"), before_shard)


class MixedTerminalStateTests(unittest.TestCase):
    """Requirement: mixed terminal states (STARTED/COMMITTED/FAILED/DATA_MISSING/EXPIRED)."""

    def test_mixed_states_tallied_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "M1", "T-5", kickoff)
            store.mark_started("M2", "T-5", kickoff=kickoff)
            store.mark_failed("M2", "T-5", kickoff=kickoff, reason="synthetic", retryable=True)
            store.mark_started("M3", "T-5", kickoff=kickoff)
            store.mark_failed("M3", "T-5", kickoff=kickoff, reason="synthetic", retryable=False)
            store.mark_started("M4", "T-5", kickoff=now - timedelta(minutes=1))
            store.mark_failed("M4", "T-5", kickoff=now - timedelta(minutes=1), reason="synthetic", retryable=True)
            store.mark_started("M5", "T-5", kickoff=kickoff)  # never terminal
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            lookups = [FixtureLookup(mid, "T-5") for mid in ("M1", "M2", "M3", "M4", "M5")]
            report = acc.build_staging_acceptance_report(config, lookups, ledger=ledger_path, now=now)
            terminal_states = {row["match_id"]: row["terminal_state"] for row in report["fixtures"]}
            self.assertEqual(terminal_states["M1"], "COMMITTED")
            self.assertEqual(terminal_states["M2"], "FAILED")
            self.assertIn(terminal_states["M3"], ("DATA_MISSING", "FAILED"))
            self.assertIn(terminal_states["M4"], ("EXPIRED", "DATA_MISSING", "FAILED"))
            self.assertIsNone(terminal_states["M5"])
            # 4 of 5 rows resolved to *some* terminal state -> ratio 0.8
            self.assertAlmostEqual(report["terminal_completeness_ratio"], 0.8)


class CorruptOrMissingShardTests(unittest.TestCase):
    """Requirement: corrupt/missing shard never blocks the rest of the batch."""

    def test_corrupt_shard_isolated_from_healthy_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "H1", "T-5", kickoff)
            bad_path = store.path_for("BAD1")
            bad_path.parent.mkdir(parents=True, exist_ok=True)
            bad_path.write_text("not json {{{", encoding="utf-8")
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            lookups = [FixtureLookup("H1", "T-5"), FixtureLookup("BAD1", "T-5")]
            report = acc.build_staging_acceptance_report(config, lookups, ledger=ledger_path, now=now)
            self.assertEqual(report["compared"], 2)
            by_id = {row["match_id"]: row for row in report["fixtures"]}
            self.assertEqual(by_id["H1"]["reconciliation_status"], "SHADOW_ONLY")
            self.assertEqual(by_id["BAD1"]["reconciliation_status"], "EXPIRED_INVALID")
            self.assertEqual(tuple(by_id["BAD1"]["attempt_states"]), ())

    def test_missing_shard_treated_as_absent_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            report = acc.build_staging_acceptance_report(
                config, [FixtureLookup("NOPE", "T-5")], ledger=ledger_path, now=now,
            )
            self.assertEqual(report["compared"], 1)
            self.assertEqual(report["fixtures"][0]["reconciliation_status"], "EXPIRED_INVALID")


class LegacyMismatchTests(unittest.TestCase):
    """Requirement: legacy mismatch (identity conflict) forces NO_GO."""

    def test_conflicting_fixture_forces_no_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "X1", "T-5", kickoff)
            watch = {
                "X1": {
                    "match_id": "X1", "kickoff_hkt": iso_hkt(kickoff),
                    "stages": [{
                        "stage": "T-5", "status": "PREDICTION_READY", "odds_status": "available",
                        "market_predictions": [
                            {"code": "HDC", "line": 0.25, "side": "H", "odds": 1.91, "source": "s", "observed_at": 1},
                        ],
                    }],
                },
            }
            # Also seed 14 clean SHADOW_ONLY fixtures so the batch clears the
            # minimum-batch-size gate and the conflict is the only reason left.
            for i in range(14):
                _seed_committed(store, f"OK{i}", "T-5", kickoff)
                watch.setdefault(f"OK{i}", {})  # absent in legacy -> SHADOW_ONLY
            ledger_path = _write_ledger(Path(tmp) / "ledger.json", watch={"X1": watch["X1"]})
            lookups = [FixtureLookup("X1", "T-5")] + [FixtureLookup(f"OK{i}", "T-5") for i in range(14)]
            result = acc.build_staging_acceptance_verdict(config, lookups, ledger=ledger_path, now=now)
            self.assertEqual(result["report"]["conflict_count"], 1)
            self.assertEqual(result["verdict"]["verdict"], "NO_GO")
            self.assertTrue(any(r.startswith("conflict_count_nonzero") for r in result["verdict"]["reasons"]))


class PostKickoffViolationTests(unittest.TestCase):
    """Requirement: any post-kickoff COMMITTED attempt forces NO_GO."""

    def test_post_kickoff_committed_attempt_detected_and_forces_no_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "P1", "T-5", kickoff)
            # Hand-craft an attempt_history entry that claims a COMMITTED
            # attempt happened *after* kickoff -- simulating a defect that
            # this harness must catch even though Stage 1's own commit path
            # refuses this at write time under normal operation.
            path = store.path_for("P1")
            state = json.loads(path.read_text(encoding="utf-8"))
            state["attempt_history"].append({
                "stage": "T-5", "state": "COMMITTED", "reason": None, "source": "s",
                "at": iso_hkt(kickoff + timedelta(minutes=1)), "attempt_id": None,
            })
            path.write_text(json.dumps(state), encoding="utf-8")
            for i in range(14):
                _seed_committed(store, f"OK{i}", "T-5", kickoff)
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            lookups = [FixtureLookup("P1", "T-5")] + [FixtureLookup(f"OK{i}", "T-5") for i in range(14)]
            result = acc.build_staging_acceptance_verdict(config, lookups, ledger=ledger_path, now=now)
            by_id = {row["match_id"]: row for row in result["report"]["fixtures"]}
            self.assertTrue(by_id["P1"]["post_kickoff_violation"])
            self.assertEqual(result["report"]["post_kickoff_violation_count"], 1)
            self.assertEqual(result["verdict"]["verdict"], "NO_GO")
            self.assertTrue(any(
                r.startswith("post_kickoff_violation_count_nonzero") for r in result["verdict"]["reasons"]
            ))


class DuplicateCommittedTests(unittest.TestCase):
    """Requirement: duplicate COMMITTED attempts for the same fixture/stage forces NO_GO."""

    def test_duplicate_committed_attempt_detected_and_forces_no_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "DUP1", "T-5", kickoff)
            path = store.path_for("DUP1")
            state = json.loads(path.read_text(encoding="utf-8"))
            # Hand-craft a second COMMITTED record -- normal operation is
            # idempotent (commit_snapshot refuses a second COMMITTED), so
            # this simulates a defect/corruption this harness must catch.
            state["attempt_history"].append({
                "stage": "T-5", "state": "COMMITTED", "reason": None, "source": "s",
                "at": iso_hkt(now), "attempt_id": None,
            })
            path.write_text(json.dumps(state), encoding="utf-8")
            for i in range(14):
                _seed_committed(store, f"OK{i}", "T-5", kickoff)
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            lookups = [FixtureLookup("DUP1", "T-5")] + [FixtureLookup(f"OK{i}", "T-5") for i in range(14)]
            result = acc.build_staging_acceptance_verdict(config, lookups, ledger=ledger_path, now=now)
            self.assertEqual(len(result["report"]["duplicate_committed_fixtures"]), 1)
            self.assertEqual(result["verdict"]["verdict"], "NO_GO")
            self.assertTrue(any(
                r.startswith("duplicate_committed_fixtures_nonzero") for r in result["verdict"]["reasons"]
            ))


class LatencySizeThresholdTests(unittest.TestCase):
    """Requirement: latency/size threshold violations are detected and gate GO."""

    def test_oversized_shard_flagged_as_violation_and_forces_no_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "BIG1", "T-5", kickoff)
            path = store.path_for("BIG1")
            state = json.loads(path.read_text(encoding="utf-8"))
            state["padding"] = "x" * (acc.MAX_SHARD_BYTES + 1000)
            path.write_text(json.dumps(state), encoding="utf-8")
            for i in range(14):
                _seed_committed(store, f"OK{i}", "T-5", kickoff)
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            lookups = [FixtureLookup("BIG1", "T-5")] + [FixtureLookup(f"OK{i}", "T-5") for i in range(14)]
            result = acc.build_staging_acceptance_verdict(config, lookups, ledger=ledger_path, now=now)
            by_id = {row["match_id"]: row for row in result["report"]["fixtures"]}
            self.assertIn("shard_size_exceeds_bound", by_id["BIG1"]["violations"])
            self.assertEqual(result["verdict"]["verdict"], "NO_GO")
            self.assertTrue(any(r.startswith("shard_size_exceeds_bound") for r in result["verdict"]["reasons"]))

    def test_within_bounds_shard_has_no_size_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "SMALL1", "T-5", kickoff)
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            report = acc.build_staging_acceptance_report(
                config, [FixtureLookup("SMALL1", "T-5")], ledger=ledger_path, now=now,
            )
            self.assertEqual(report["fixtures"][0]["violations"], [])
            self.assertLess(report["shard_size_summary"]["max_bytes"], acc.MAX_SHARD_BYTES)


class DeterministicOutputTests(unittest.TestCase):
    """Requirement: deterministic output (aside from timing fields) across reruns."""

    def test_go_no_go_is_pure_function_of_report(self):
        report = {
            "requested": 20, "compared": 20, "conflict_count": 0,
            "post_kickoff_violation_count": 0, "duplicate_committed_fixtures": [],
            "terminal_completeness_ratio": 1.0,
            "read_latency_summary": {"max_seconds": 0.001},
            "shard_size_summary": {"max_bytes": 100},
            "fixtures": [], "generated_at": "2026-01-01T00:00:00+08:00",
        }
        v1 = acc.evaluate_go_no_go(report)
        v2 = acc.evaluate_go_no_go(report)
        self.assertEqual(v1, v2)
        self.assertEqual(v1["verdict"], "GO")

    def test_evaluate_go_no_go_performs_no_io(self):
        # Passing a plain dict with no filesystem dependency proves this
        # function touches nothing beyond its argument.
        report = {"requested": 1, "compared": 1, "conflict_count": 1}
        result = acc.evaluate_go_no_go(report)
        self.assertEqual(result["verdict"], "NO_GO")


class NoForbiddenImportGuardTests(unittest.TestCase):
    """Static guard: module never imports provider/Wilson/Telegram/dashboard/
    config-resolving code, and is never called from crown/engine.py."""

    def test_module_never_imports_forbidden_symbols_or_config_settings_factory(self):
        import ast
        import inspect
        source = inspect.getsource(acc)
        tree = ast.parse(source)
        imported_names: set[str] = set()
        imported_from_config: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    imported_names.add(f"{module}.{alias.name}")
                    if module.endswith("config") or module == ".config":
                        imported_from_config.add(alias.name)
        forbidden = (
            "TitanClient", "PinnapiClient", "analysis.wilson_validation",
            "crown.notify", "crown.settle", "crown.dashboard_data",
            "crown.dashboard_api", "challenger_v2", "direct_t5_outbox",
        )
        for token in forbidden:
            self.assertFalse(
                any(token in name for name in imported_names),
                f"forbidden symbol {token!r} imported by staging acceptance module",
            )
        # config.settings (the env-driven production factory function) must
        # never be imported -- only the Settings dataclass type may be.
        self.assertNotIn("settings", imported_from_config)
        self.assertIn("Settings", imported_from_config)

    def test_module_never_calls_load_ledger_or_save_ledger(self):
        import inspect
        source = inspect.getsource(acc)
        self.assertNotIn("load_ledger(", source)
        self.assertNotIn("save_ledger(", source)

    def test_module_has_no_call_site_in_engine(self):
        import inspect
        from crown import engine as engine_mod
        engine_source = inspect.getsource(engine_mod)
        self.assertNotIn("native_stage_staging_acceptance", engine_source)

    def test_module_is_not_referenced_by_any_github_workflow(self):
        repo_root = Path(__file__).resolve().parents[2]
        workflows_dir = repo_root / ".github" / "workflows"
        if not workflows_dir.is_dir():
            self.skipTest("no .github/workflows directory in this checkout")
        for workflow_path in workflows_dir.glob("*.yml"):
            text = workflow_path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn(
                "native_stage_staging_acceptance", text,
                f"unexpected reference to the stage-4 module in {workflow_path.name}",
            )


class BoundedBatchSizeTests(unittest.TestCase):
    """Requirement: 1/15/26/50/89 fixture bounded run."""

    def test_batch_sizes_1_15_26_50_89(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            for size in (1, 15, 26, 50, 89):
                with self.subTest(size=size):
                    ids = [f"B{size}_{i}" for i in range(size)]
                    for mid in ids:
                        _seed_committed(store, mid, "T-5", kickoff)
                    lookups = [FixtureLookup(mid, "T-5") for mid in ids]
                    report = acc.build_staging_acceptance_report(config, lookups, ledger=ledger_path, now=now)
                    self.assertEqual(report["compared"], size)
                    verdict = acc.evaluate_go_no_go(report)
                    if size >= acc.MIN_BATCH_SIZE_FOR_GO:
                        self.assertEqual(verdict["verdict"], "GO")
                    else:
                        self.assertEqual(verdict["verdict"], "NO_GO")
                        self.assertTrue(any(
                            r.startswith("insufficient_batch_size_for_verdict") for r in verdict["reasons"]
                        ))

    def test_refuses_batch_larger_than_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            lookups = [FixtureLookup(f"Z{i}", "T-5") for i in range(acc.MAX_BATCH_SIZE + 1)]
            with self.assertRaises(ValueError):
                acc.build_staging_acceptance_report(config, lookups, ledger=ledger_path)


class SmallBatchInsufficientForGoTests(unittest.TestCase):
    """Requirement: a batch below MIN_BATCH_SIZE_FOR_GO is always NO_GO,
    even with zero violations."""

    def test_clean_but_tiny_batch_is_no_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "TINY1", "T-5", kickoff)
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            result = acc.build_staging_acceptance_verdict(
                config, [FixtureLookup("TINY1", "T-5")], ledger=ledger_path, now=now,
            )
            self.assertEqual(result["report"]["conflict_count"], 0)
            self.assertEqual(result["verdict"]["verdict"], "NO_GO")


class LedgerInputFlexibilityTests(unittest.TestCase):
    """Requirement: ledger can be supplied as a captured dict or a file path."""

    def test_ledger_as_dict_and_as_path_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "L1", "T-5", kickoff)
            watch = {}
            ledger_path = _write_ledger(Path(tmp) / "ledger.json", watch=watch)
            report_from_path = acc.build_staging_acceptance_report(
                config, [FixtureLookup("L1", "T-5")], ledger=ledger_path, now=now,
            )
            report_from_dict = acc.build_staging_acceptance_report(
                config, [FixtureLookup("L1", "T-5")], ledger={"watch": watch}, now=now,
            )
            self.assertEqual(
                report_from_path["reconciliation_aggregate"],
                report_from_dict["reconciliation_aggregate"],
            )

    def test_missing_ledger_file_degrades_to_empty_ledger_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "NF1", "T-5", kickoff)
            nonexistent = Path(tmp) / "does_not_exist.json"
            report = acc.build_staging_acceptance_report(
                config, [FixtureLookup("NF1", "T-5")], ledger=nonexistent, now=now,
            )
            self.assertEqual(report["fixtures"][0]["reconciliation_status"], "SHADOW_ONLY")


class CliTests(unittest.TestCase):
    """The offline CLI requires explicit --state-dir/--ledger-path and refuses
    an unbounded/empty invocation; never touches environment configuration."""

    def test_cli_requires_match_id_or_pairs_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            exit_code = acc.main([
                "--state-dir", tmp, "--ledger-path", str(ledger_path),
            ])
            self.assertEqual(exit_code, 2)

    def test_cli_runs_end_to_end_with_match_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "CLI1", "T-5", kickoff)
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            exit_code = acc.main([
                "--state-dir", tmp, "--ledger-path", str(ledger_path),
                "--match-id", "CLI1", "--stage", "T-5", "--json",
            ])
            self.assertEqual(exit_code, 0)

    def test_cli_pairs_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            now = datetime.now(HKT)
            kickoff = _kickoff(20, now)
            store = store_mod.NativeStageStore(config.state_dir)
            _seed_committed(store, "PF1", "T-5", kickoff)
            ledger_path = _write_ledger(Path(tmp) / "ledger.json")
            pairs_path = Path(tmp) / "pairs.json"
            pairs_path.write_text(json.dumps([{"match_id": "PF1", "stage": "T-5"}]), encoding="utf-8")
            exit_code = acc.main([
                "--state-dir", tmp, "--ledger-path", str(ledger_path),
                "--pairs-file", str(pairs_path),
            ])
            self.assertEqual(exit_code, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
