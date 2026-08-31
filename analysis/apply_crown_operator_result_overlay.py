#!/usr/bin/env python3
"""Apply an identity-locked operator result overlay to Crown history."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import timezone
from pathlib import Path
from typing import Any

from crown.common import parse_time
from crown.prediction_history import _grade_market, normalize_history
from crown.state import write_json_atomic


FINAL = {"已核對", "不計"}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"object required: {path}")
    return value


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _manifest(path: Path) -> dict[str, Any]:
    value = _read(path)
    if value.get("schema_version") != 1:
        raise ValueError("overlay schema invalid")
    if value.get("score_scope") != (
        "90_minutes_including_stoppage_time_excluding_extra_time"
    ):
        raise ValueError("overlay score scope invalid")
    rows = value.get("results")
    if not isinstance(rows, list) or not rows:
        raise ValueError("overlay results missing")
    ids = [str(row.get("match_id") or "") for row in rows]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("overlay match ids invalid")
    for row in rows:
        for key in (
            "league", "home", "away", "kickoff", "provider_event_id",
            "provider_home", "provider_away", "provider_start",
        ):
            if not str(row.get(key) or "").strip():
                raise ValueError(f"overlay {key} missing: {row.get('match_id')}")
        if row.get("orientation") not in {"direct", "reversed"}:
            raise ValueError(f"overlay orientation invalid: {row.get('match_id')}")
        home, away = int(row["home_score"]), int(row["away_score"])
        if min(home, away) < 0:
            raise ValueError(f"overlay score invalid: {row.get('match_id')}")
        kickoff = parse_time(row["kickoff"])
        provider = parse_time(row["provider_start"])
        if not kickoff or not provider or kickoff.astimezone(timezone.utc) != provider.astimezone(timezone.utc):
            raise ValueError(f"overlay kickoff mismatch: {row.get('match_id')}")
    return value


def apply_overlay(
    history: dict[str, Any], manifest: dict[str, Any], *, apply: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    proposed = copy.deepcopy(history)
    rows = proposed.get("rows")
    if not isinstance(rows, list):
        raise ValueError("history rows missing")
    changed_rows = 0
    already_rows = 0
    by_fixture: dict[str, int] = {}
    verified_at = str(manifest["verified_at"])
    batch_id = str(manifest["batch_id"])

    for spec in manifest["results"]:
        match_id = str(spec["match_id"])
        candidates = [
            row for row in rows
            if isinstance(row, dict) and str(row.get("match_id") or "") == match_id
        ]
        if not candidates:
            raise ValueError(f"history fixture missing: {match_id}")
        for row in candidates:
            for key in ("league", "home", "away"):
                if str(row.get(key) or "") != str(spec[key]):
                    raise ValueError(f"history {key} mismatch: {match_id}")
            if parse_time(row.get("kickoff")) != parse_time(spec["kickoff"]):
                raise ValueError(f"history kickoff mismatch: {match_id}")
            score_text = f"{int(spec['home_score'])}-{int(spec['away_score'])}"
            if row.get("result_status") in FINAL:
                if row.get("result_status") == "已核對" and row.get("score") == score_text:
                    already_rows += 1
                    continue
                raise ValueError(f"existing result conflict: {match_id}")
            home, away = int(spec["home_score"]), int(spec["away_score"])
            actual = "主勝" if home > away else ("和局" if home == away else "客勝")
            score = {"home_score": home, "away_score": away, "corners_total": None}
            row.update({
                "actual": actual,
                "score": score_text,
                "correct": (
                    row.get("forecast") == actual if row.get("forecast") else None
                ),
                "result_status": "已核對",
                "verified_at": verified_at,
                "result_source": "opticodds_operator_verified_overlay",
                "result_detail": {
                    **score,
                    "operator_batch_id": batch_id,
                    "provider_event_id": spec["provider_event_id"],
                    "provider_home": spec["provider_home"],
                    "provider_away": spec["provider_away"],
                    "provider_start": spec["provider_start"],
                    "orientation": spec["orientation"],
                    "score_scope": manifest["score_scope"],
                },
                "market_grades": [
                    _grade_market(prediction, score)
                    for prediction in (row.get("market_predictions") or [])
                    if isinstance(prediction, dict)
                ],
                "result_missing_reason": None,
            })
            changed_rows += 1
        by_fixture[match_id] = len(candidates)

    proposed.setdefault("operator_result_overlays", {})[batch_id] = {
        "verified_at": verified_at,
        "fixtures": len(manifest["results"]),
        "changed_rows": changed_rows,
        "already_rows": already_rows,
        "manifest_hash": _hash(manifest),
        "excluded": copy.deepcopy(manifest.get("excluded") or []),
    }
    normalize_history(proposed)
    report = {
        "mode": "apply" if apply else "dry_run",
        "batch_id": batch_id,
        "fixtures": len(manifest["results"]),
        "changed_rows": changed_rows,
        "already_rows": already_rows,
        "fixture_row_counts": by_fixture,
        "before_hash": _hash(history),
        "after_hash": _hash(proposed),
        "excluded": copy.deepcopy(manifest.get("excluded") or []),
    }
    return proposed, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    history = _read(args.history)
    manifest = _manifest(args.manifest)
    proposed, report = apply_overlay(history, manifest, apply=args.apply)
    if args.apply and report["before_hash"] != report["after_hash"]:
        write_json_atomic(args.history, proposed)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
