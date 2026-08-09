#!/usr/bin/env python3
"""Chronological train/holdout evaluation for Crown and Footbreak."""
from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

STAGE_RANK = {"首預": 1, "T-30": 2, "T-5": 3}
THRESHOLDS = [None, 50, 52, 55, 58, 60, 64]


def dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "accuracy": None, "brier": None, "log_loss": None}
    return {
        "n": len(rows),
        "accuracy": round(sum(float(row["hit"]) for row in rows) / len(rows), 6),
        "brier": round(sum(float(row["brier"]) for row in rows) / len(rows), 6),
        "log_loss": round(sum(float(row["log_loss"]) for row in rows) / len(rows), 6),
    }


def bootstrap_ci(rows: list[dict[str, Any]], key: str, n_boot: int = 5000) -> list[float] | None:
    if len(rows) < 2:
        return None
    rng = random.Random(20260809)
    values = [float(row[key]) for row in rows]
    samples = []
    for _ in range(n_boot):
        samples.append(sum(rng.choice(values) for _ in values) / len(values))
    samples.sort()
    return [round(samples[int(.025 * n_boot)], 6), round(samples[int(.975 * n_boot) - 1], 6)]


def with_ci(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = metrics(rows)
    out["accuracy_ci95"] = bootstrap_ci(rows, "hit")
    out["brier_ci95"] = bootstrap_ci(rows, "brier")
    return out


def crown_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    actual_key = {"主勝": "home", "和局": "draw", "客勝": "away"}
    history = payload.get("prediction_history", payload)
    for row in history.get("rows") or []:
        outcome, key = row.get("outcome"), actual_key.get(row.get("actual"))
        if not isinstance(outcome, dict) or not key or not row.get("kickoff"):
            continue
        try:
            p = {name: float(outcome[name]) for name in ("home", "draw", "away")}
            total = sum(p.values())
            p = {name: value / total for name, value in p.items()}
            brier = sum((p[name] - (1 if name == key else 0)) ** 2 for name in p)
            loss = -math.log(max(p[key], 1e-9))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        output.append({
            "match_id": str(row["match_id"]),
            "kickoff": dt(row["kickoff"]),
            "stage": row.get("stage"),
            "predicted_at": str(row.get("predicted_at") or ""),
            "conf": row.get("conviction"),
            "hit": int(row.get("forecast") == row.get("actual")),
            "brier": brier,
            "log_loss": loss,
        })
    return output


def footbreak_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    accuracy = payload.get("accuracy", payload)
    for match in accuracy.get("matches") or []:
        kickoff = dt(match["kickoff"])
        for index, row in enumerate(match.get("stages") or []):
            if any(row.get(key) is None for key in ("wdl_hit", "wdl_brier", "wdl_ll")):
                continue
            output.append({
                "match_id": str(match["match_id"]),
                "kickoff": kickoff,
                "stage": row.get("stage"),
                "predicted_at": f"{index:03d}",
                "conf": row.get("conf"),
                "hit": int(row["wdl_hit"]),
                "brier": float(row["wdl_brier"]),
                "log_loss": float(row["wdl_ll"]),
            })
    return output


def latest(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_match: dict[str, dict[str, Any]] = {}
    for row in rows:
        old = by_match.get(row["match_id"])
        rank = (STAGE_RANK.get(row.get("stage"), 0), row.get("predicted_at", ""))
        old_rank = (STAGE_RANK.get((old or {}).get("stage"), 0), (old or {}).get("predicted_at", ""))
        if old is None or rank > old_rank:
            by_match[row["match_id"]] = row
    return list(by_match.values())


def split_ids(rows: list[dict[str, Any]]) -> tuple[set[str], set[str], str]:
    match_times: dict[str, datetime] = {}
    for row in rows:
        match_times[row["match_id"]] = min(match_times.get(row["match_id"], row["kickoff"]), row["kickoff"])
    ordered = sorted(match_times, key=lambda match_id: (match_times[match_id], match_id))
    cut = max(1, min(len(ordered) - 1, int(len(ordered) * .70)))
    train, holdout = set(ordered[:cut]), set(ordered[cut:])
    return train, holdout, match_times[ordered[cut]].isoformat()


def select_stage(train_rows: list[dict[str, Any]]) -> str | None:
    candidates = []
    for stage in STAGE_RANK:
        subset = [row for row in train_rows if row.get("stage") == stage]
        score = metrics(subset)
        if score["n"] >= 30:
            candidates.append((score["brier"], -score["accuracy"], -score["n"], stage))
    if candidates:
        return min(candidates)[-1]
    fallback = [
        (metrics([row for row in train_rows if row.get("stage") == stage])["n"], rank, stage)
        for stage, rank in STAGE_RANK.items()
    ]
    return max(fallback)[-1] if fallback and max(fallback)[0] else None


def select_threshold(train_latest: list[dict[str, Any]]) -> float | None:
    candidates = []
    for threshold in THRESHOLDS:
        subset = [
            row for row in train_latest
            if threshold is None or (row.get("conf") is not None and float(row["conf"]) >= threshold)
        ]
        coverage = len(subset) / len(train_latest) if train_latest else 0
        score = metrics(subset)
        if score["n"] >= 30 and coverage >= .50:
            candidates.append((score["brier"], -score["accuracy"], -coverage, threshold))
    return min(candidates, key=lambda row: row[:3])[-1] if candidates else None


def paired_delta(candidate: list[dict[str, Any]], baseline: list[dict[str, Any]]) -> dict[str, Any]:
    left = {row["match_id"]: row for row in candidate}
    right = {row["match_id"]: row for row in baseline}
    ids = sorted(left.keys() & right.keys())
    if not ids:
        return {"n": 0, "accuracy_pp": None, "brier": None}
    return {
        "n": len(ids),
        "accuracy_pp": round(100 * sum(left[i]["hit"] - right[i]["hit"] for i in ids) / len(ids), 3),
        "brier": round(sum(left[i]["brier"] - right[i]["brier"] for i in ids) / len(ids), 6),
    }


def evaluate(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len({row["match_id"] for row in rows}) < 2:
        raise ValueError(f"{name} needs at least two evaluable matches")
    train_ids, holdout_ids, cutoff = split_ids(rows)
    train = [row for row in rows if row["match_id"] in train_ids]
    holdout = [row for row in rows if row["match_id"] in holdout_ids]
    train_latest, holdout_latest = latest(train), latest(holdout)

    chosen_stage = select_stage(train)
    train_stage = [row for row in train if row.get("stage") == chosen_stage]
    holdout_stage = [row for row in holdout if row.get("stage") == chosen_stage]

    threshold = select_threshold(train_latest)
    train_threshold = [
        row for row in train_latest
        if threshold is None or (row.get("conf") is not None and float(row["conf"]) >= threshold)
    ]
    holdout_threshold = [
        row for row in holdout_latest
        if threshold is None or (row.get("conf") is not None and float(row["conf"]) >= threshold)
    ]

    return {
        "system": name,
        "n_unique_matches": len(train_ids) + len(holdout_ids),
        "train_matches": len(train_ids),
        "holdout_matches": len(holdout_ids),
        "holdout_start": cutoff,
        "time_range": [min(row["kickoff"] for row in rows).isoformat(), max(row["kickoff"] for row in rows).isoformat()],
        "baseline_latest": {
            "train": with_ci(train_latest),
            "holdout": with_ci(holdout_latest),
        },
        "stage_candidate": {
            "selected_on_train": chosen_stage,
            "train": with_ci(train_stage),
            "holdout": with_ci(holdout_stage),
            "holdout_coverage": round(len({row["match_id"] for row in holdout_stage}) / len(holdout_ids), 6),
            "holdout_paired_vs_latest": paired_delta(holdout_stage, holdout_latest),
        },
        "confidence_candidate": {
            "selected_on_train": threshold,
            "train": with_ci(train_threshold),
            "holdout": with_ci(holdout_threshold),
            "holdout_coverage": round(len(holdout_threshold) / len(holdout_latest), 6),
        },
        "stage_diagnostics": {
            stage: {
                "train": metrics([row for row in train if row.get("stage") == stage]),
                "holdout": metrics([row for row in holdout if row.get("stage") == stage]),
                "train_coverage": round(
                    len({
                        row["match_id"]
                        for row in train
                        if row.get("stage") == stage
                    }) / len(train_ids),
                    6,
                ),
                "holdout_coverage": round(
                    len({
                        row["match_id"]
                        for row in holdout
                        if row.get("stage") == stage
                    }) / len(holdout_ids),
                    6,
                ),
            }
            for stage in STAGE_RANK
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crown", type=Path, required=True)
    parser.add_argument("--footbreak", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    crown = json.loads(args.crown.read_text(encoding="utf-8"))
    footbreak = json.loads(args.footbreak.read_text(encoding="utf-8"))
    result = {
        "method": {
            "split": "chronological 70/30 by unique match; no match crosses partitions",
            "selection_metric": "lowest train multiclass Brier, accuracy as tie-break",
            "holdout_locked": True,
            "minimum_train_rows_per_candidate": 30,
            "confidence_minimum_coverage": .50,
            "bootstrap": "5000 resamples, seed 20260809",
        },
        "crown": evaluate("crown", crown_rows(crown)),
        "footbreak": evaluate("footbreak", footbreak_rows(footbreak)),
    }
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
