#!/usr/bin/env python3
"""Persist fixed forward-validation batches without changing any model."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis.time_order_backtest import (
    crown_market_rows,
    crown_rows,
    evaluate,
    footbreak_market_rows,
    footbreak_rows,
    latest,
    with_ci,
)
from analysis.champion_challenger import all_market_tests
from analysis.learning_store import LearningStore

TARGET_NEW_MATCHES = 100
MIN_HOLDOUT_MATCHES = 30
MIN_T5_HOLDOUT_COVERAGE = 0.70
MIN_MATCHES_FOR_UPGRADE_TEST = 300
MIN_UPGRADE_HOLDOUT_COVERAGE = 0.70
MIN_BRIER_IMPROVEMENT = 0.01
MAX_ACCURACY_DECLINE = 0.02
MAX_LOG_LOSS_INCREASE = 0.0


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def ordered_match_ids(rows: list[dict[str, Any]]) -> list[str]:
    times: dict[str, datetime] = {}
    for row in rows:
        match_id = row["match_id"]
        times[match_id] = min(times.get(match_id, row["kickoff"]), row["kickoff"])
    return sorted(times, key=lambda match_id: (times[match_id], match_id))


def locked_selection(report: dict[str, Any]) -> dict[str, Any]:
    stage_train_rows = int(report["stage_candidate"]["train"]["n"])
    confidence_train_rows = int(report["confidence_candidate"]["train"]["n"])
    return {
        "stage": report["stage_candidate"]["selected_on_train"],
        "confidence_threshold": report["confidence_candidate"]["selected_on_train"],
        "selected_at_baseline": True,
        "baseline_train_matches": report["train_matches"],
        "stage_train_rows": stage_train_rows,
        "confidence_train_rows": confidence_train_rows,
        "sample_sufficient": (
            report["train_matches"] >= 30
            and stage_train_rows >= 30
            and confidence_train_rows >= 30
        ),
    }


def forward_report(
    rows: list[dict[str, Any]],
    batch_ids: list[str],
    selection: dict[str, Any],
) -> dict[str, Any]:
    ids = set(batch_ids)
    batch = [row for row in rows if row["match_id"] in ids]
    batch_latest = latest(batch)
    stage = selection.get("stage")
    stage_rows = latest([row for row in batch if row.get("stage") == stage])
    threshold = selection.get("confidence_threshold")
    threshold_rows = [
        row for row in batch_latest
        if threshold is None
        or (row.get("conf") is not None and float(row["conf"]) >= float(threshold))
    ]
    t5_ids = {
        row["match_id"] for row in batch if row.get("stage") == "T-5"
    }
    return {
        "locked_selection": selection,
        "batch_matches": len(batch_ids),
        "latest": with_ci(batch_latest),
        "stage_candidate": {
            "stage": stage,
            "metrics": with_ci(stage_rows),
            "coverage": round(len({row["match_id"] for row in stage_rows}) / len(batch_ids), 6)
            if batch_ids else 0.0,
        },
        "confidence_candidate": {
            "threshold": threshold,
            "metrics": with_ci(threshold_rows),
            "coverage": round(len(threshold_rows) / len(batch_ids), 6)
            if batch_ids else 0.0,
        },
        "t5_matches": len(t5_ids),
        "t5_coverage": round(len(t5_ids) / len(batch_ids), 6) if batch_ids else 0.0,
    }


def candidate_gate(
    label: str,
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    coverage: float,
) -> dict[str, Any]:
    candidate_n = int(candidate.get("n") or 0)
    baseline_n = int(baseline.get("n") or 0)
    brier_delta = (
        round(float(candidate["brier"]) - float(baseline["brier"]), 6)
        if candidate.get("brier") is not None and baseline.get("brier") is not None
        else None
    )
    accuracy_delta = (
        round(float(candidate["accuracy"]) - float(baseline["accuracy"]), 6)
        if candidate.get("accuracy") is not None and baseline.get("accuracy") is not None
        else None
    )
    log_loss_delta = (
        round(float(candidate["log_loss"]) - float(baseline["log_loss"]), 6)
        if candidate.get("log_loss") is not None and baseline.get("log_loss") is not None
        else None
    )
    checks = {
        "holdout_rows": candidate_n >= MIN_HOLDOUT_MATCHES and baseline_n >= MIN_HOLDOUT_MATCHES,
        "holdout_coverage": coverage >= MIN_UPGRADE_HOLDOUT_COVERAGE,
        "brier_improvement": (
            brier_delta is not None and brier_delta <= -MIN_BRIER_IMPROVEMENT
        ),
        "accuracy_not_materially_worse": (
            accuracy_delta is not None and accuracy_delta >= -MAX_ACCURACY_DECLINE
        ),
        "log_loss_not_worse": (
            log_loss_delta is not None and log_loss_delta <= MAX_LOG_LOSS_INCREASE
        ),
    }
    return {
        "candidate": label,
        "coverage": coverage,
        "candidate_metrics": candidate,
        "baseline_metrics": baseline,
        "delta": {
            "brier": brier_delta,
            "accuracy": accuracy_delta,
            "log_loss": log_loss_delta,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def automatic_upgrade_test(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    match_count = len(ordered_match_ids(rows))
    if match_count < MIN_MATCHES_FOR_UPGRADE_TEST:
        return {
            "status": "waiting_for_300_matches",
            "eligible_matches": match_count,
            "required_matches": MIN_MATCHES_FOR_UPGRADE_TEST,
            "remaining_matches": MIN_MATCHES_FOR_UPGRADE_TEST - match_count,
            "auto_promote": False,
            "tests": [],
        }

    report = evaluate(name, rows)
    baseline = report["baseline_latest"]["holdout"]
    tests = [
        candidate_gate(
            f"stage:{report['stage_candidate']['selected_on_train']}",
            report["stage_candidate"]["holdout"],
            baseline,
            float(report["stage_candidate"]["holdout_coverage"]),
        ),
        candidate_gate(
            f"confidence:{report['confidence_candidate']['selected_on_train']}",
            report["confidence_candidate"]["holdout"],
            baseline,
            float(report["confidence_candidate"]["holdout_coverage"]),
        ),
    ]
    passed = [test for test in tests if test["passed"]]
    return {
        "status": "candidate_passed" if passed else "tested_no_safe_upgrade",
        "tested_at_matches": match_count,
        "eligible_matches": match_count,
        "required_matches": MIN_MATCHES_FOR_UPGRADE_TEST,
        "remaining_matches": 0,
        "selection_locked_to_train": True,
        "holdout_start": report["holdout_start"],
        "auto_promote": False,
        "recommended_candidate": passed[0]["candidate"] if passed else None,
        "tests": tests,
    }


def system_status(
    rows: list[dict[str, Any]],
    state: dict[str, Any],
) -> dict[str, Any]:
    current_ids = ordered_match_ids(rows)
    seen_ids = list(dict.fromkeys([*(state.get("seen_match_ids") or []), *current_ids]))
    state["seen_match_ids"] = seen_ids
    baseline_ids = set(state["baseline_match_ids"])
    new_ids = [match_id for match_id in seen_ids if match_id not in baseline_ids]
    batch_ids = new_ids[:TARGET_NEW_MATCHES]
    forward = forward_report(rows, batch_ids, state["locked_selection"])
    checks = {
        "new_matches": len(new_ids) >= TARGET_NEW_MATCHES,
        "holdout_matches": forward["batch_matches"] >= MIN_HOLDOUT_MATCHES,
        "t5_holdout_coverage": forward["t5_coverage"] >= MIN_T5_HOLDOUT_COVERAGE,
        "baseline_selection": bool(state["locked_selection"]["sample_sufficient"]),
    }
    return {
        "baseline_matches": len(baseline_ids),
        "known_matches": len(seen_ids),
        "source_available_matches": len(current_ids),
        "new_matches": len(new_ids),
        "target_new_matches": TARGET_NEW_MATCHES,
        "remaining_matches": max(0, TARGET_NEW_MATCHES - len(new_ids)),
        "holdout_matches": forward["batch_matches"],
        "t5_holdout_matches": forward["t5_matches"],
        "t5_holdout_coverage": forward["t5_coverage"],
        "minimum_t5_holdout_coverage": MIN_T5_HOLDOUT_COVERAGE,
        "checks": checks,
        "status": "ready_for_human_review" if all(checks.values()) else "accumulating",
        "forward_validation": forward,
        "baseline_report": state["baseline_report"],
    }


def initialise_system(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids = ordered_match_ids(rows)
    if len(ids) < 2:
        return {
            "initialized": False,
            "baseline_match_ids": ids,
            "seen_match_ids": ids,
            "locked_selection": {
                "stage": None,
                "confidence_threshold": None,
                "sample_sufficient": False,
            },
            "baseline_report": None,
        }
    report = evaluate(name, rows)
    selection = locked_selection(report)
    return {
        "initialized": selection["sample_sufficient"],
        "baseline_match_ids": ids,
        "seen_match_ids": ids,
        "locked_selection": selection,
        "baseline_report": report,
    }


def ensure_initialized(
    name: str,
    rows: list[dict[str, Any]],
    state: dict[str, Any],
) -> dict[str, Any]:
    if state.get("initialized"):
        return state
    candidate = initialise_system(name, rows)
    if candidate.get("initialized"):
        return candidate
    current_ids = ordered_match_ids(rows)
    candidate["seen_match_ids"] = list(dict.fromkeys([
        *(state.get("seen_match_ids") or []),
        *current_ids,
    ]))
    return candidate


def accumulating_status(state: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    current_ids = ordered_match_ids(rows)
    seen_ids = list(dict.fromkeys([*(state.get("seen_match_ids") or []), *current_ids]))
    state["seen_match_ids"] = seen_ids
    return {
        "baseline_matches": len(state.get("baseline_match_ids") or []),
        "known_matches": len(seen_ids),
        "source_available_matches": len(current_ids),
        "new_matches": 0,
        "target_new_matches": TARGET_NEW_MATCHES,
        "remaining_matches": TARGET_NEW_MATCHES,
        "holdout_matches": 0,
        "t5_holdout_matches": 0,
        "t5_holdout_coverage": 0.0,
        "minimum_t5_holdout_coverage": MIN_T5_HOLDOUT_COVERAGE,
        "checks": {
            "new_matches": False,
            "holdout_matches": False,
            "t5_holdout_coverage": False,
            "baseline_selection": False,
        },
        "status": "accumulating_baseline",
        "forward_validation": None,
        "baseline_report": state.get("baseline_report"),
    }


def run(
    crown_path: Path,
    footbreak_path: Path,
    state_path: Path,
    output_path: Path,
    public_paths: list[Path] | None = None,
    learning_db: Path | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    if learning_db is not None:
        if not learning_db.is_file():
            raise FileNotFoundError(
                f"immutable learning database does not exist: {learning_db}"
            )
        with LearningStore(learning_db) as store:
            crown_wdl, crown_markets = store.backtest_rows("crown")
            footbreak_wdl, footbreak_markets = store.backtest_rows("footbreak")
        rows_by_system = {
            "crown": crown_wdl,
            "footbreak": footbreak_wdl,
        }
        market_rows_by_system = {
            "crown": crown_markets,
            "footbreak": footbreak_markets,
        }
    else:
        rows_by_system = {
            "crown": crown_rows(read_json(crown_path)),
            "footbreak": footbreak_rows(read_json(footbreak_path)),
        }
        market_rows_by_system = {
            "crown": crown_market_rows(read_json(crown_path)),
            "footbreak": footbreak_market_rows(read_json(footbreak_path)),
        }
    if state_path.exists():
        state = read_json(state_path)
    else:
        state = {
            "schema_version": 2,
            "started_at": now,
            "systems": {
                name: initialise_system(name, rows)
                for name, rows in rows_by_system.items()
            },
        }

    systems = {}
    for name in ("crown", "footbreak"):
        state["systems"][name] = ensure_initialized(
            name, rows_by_system[name], state["systems"][name]
        )
        if state["systems"][name].get("initialized"):
            systems[name] = system_status(rows_by_system[name], state["systems"][name])
            systems[name]["automatic_upgrade_test"] = automatic_upgrade_test(
                name, rows_by_system[name]
            )
        else:
            systems[name] = accumulating_status(
                state["systems"][name], rows_by_system[name]
            )
        systems[name]["automatic_upgrade_test"] = automatic_upgrade_test(
            name, rows_by_system[name]
        )
        systems[name]["market_model_upgrade_tests"] = all_market_tests(
            market_rows_by_system[name]
        )
    review_required = any(
        item["status"] == "ready_for_human_review"
        or (item.get("automatic_upgrade_test") or {}).get("status") == "candidate_passed"
        or bool((item.get("market_model_upgrade_tests") or {}).get("review_required"))
        for item in systems.values()
    )
    result = {
        "schema_version": 2,
        "generated_at": now,
        "policy": {
            "design": "fixed baseline selection followed by an untouched forward batch",
            "target_new_matches_per_review": TARGET_NEW_MATCHES,
            "minimum_holdout_matches": MIN_HOLDOUT_MATCHES,
            "minimum_t5_holdout_coverage": MIN_T5_HOLDOUT_COVERAGE,
            "automatic_upgrade_test_at_matches": MIN_MATCHES_FOR_UPGRADE_TEST,
            "minimum_upgrade_holdout_coverage": MIN_UPGRADE_HOLDOUT_COVERAGE,
            "minimum_brier_improvement": MIN_BRIER_IMPROVEMENT,
            "maximum_accuracy_decline": MAX_ACCURACY_DECLINE,
            "maximum_log_loss_increase": MAX_LOG_LOSS_INCREASE,
            "auto_apply": False,
            "notifications": "passed_threshold_only",
        },
        "started_at": state["started_at"],
        "overall_status": (
            "ready_for_human_review"
            if all(item["status"] == "ready_for_human_review" for item in systems.values())
            else "accumulating"
        ),
        "review_required": review_required,
        "systems": systems,
    }
    state["last_run_at"] = now
    atomic_write(state_path, state)
    atomic_write(output_path, result)
    for public_path in public_paths or []:
        atomic_write(public_path, result, mode=0o644)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--crown",
        type=Path,
        default=Path("/var/lib/footbreak/crown/prediction_history.json"),
    )
    parser.add_argument(
        "--footbreak",
        type=Path,
        default=Path("/opt/footbreak/system/accuracy_history.json"),
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("/var/lib/footbreak/backtest/state.json"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("/var/lib/footbreak/backtest/latest.json"),
    )
    parser.add_argument("--public", action="append", type=Path, default=[])
    parser.add_argument(
        "--learning-db",
        type=Path,
        default=None,
        help="immutable learning SQLite database; production must provide this",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path("/var/lock/footbreak-backtest.lock"),
    )
    args = parser.parse_args()

    args.lock.parent.mkdir(parents=True, exist_ok=True)
    with args.lock.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = run(
            args.crown,
            args.footbreak,
            args.state,
            args.out,
            args.public,
            args.learning_db,
        )
    print(json.dumps({
        "generated_at": result["generated_at"],
        "overall_status": result["overall_status"],
        "counts": {
            name: {
                "known": value["known_matches"],
                "new": value["new_matches"],
                "remaining": value["remaining_matches"],
            }
            for name, value in result["systems"].items()
        },
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
