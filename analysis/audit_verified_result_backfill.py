#!/usr/bin/env python3
"""Apply a reviewed result batch to a separate prediction-history audit export."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crown.prediction_history import _grade_market
from crown.state import write_json_atomic


DEFAULT_BATCH_ID = "gemini-condition-a-hil-open-t5-over-180-2026-09-01-v1"
DEFAULT_VERIFIED_AT = "2026-09-01T17:39:00+08:00"
SCORE_SCOPE = "90_minutes_including_stoppage_time_excluding_extra_time"
FINAL = {"已核對", "不計"}


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _different_file(source: Path, output: Path) -> None:
    if source.resolve() == output.resolve():
        raise ValueError("output must not overwrite input history")
    if output.exists() and source.samefile(output):
        raise ValueError("output aliases input history")


def build_proposal(
    history: dict[str, Any],
    verified: dict[str, Any],
    *,
    batch_id: str = DEFAULT_BATCH_ID,
    verified_at: str = DEFAULT_VERIFIED_AT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    proposed = copy.deepcopy(history)
    rows = proposed.get("rows")
    matches = verified.get("matches")
    if not isinstance(rows, list):
        raise ValueError("history rows missing")
    if not isinstance(matches, list) or not matches:
        raise ValueError("verified matches missing")
    if any(match.get("status") != "completed" for match in matches):
        raise ValueError("verified batch contains a non-completed fixture")

    changed_rows = 0
    already_rows = 0
    fixture_row_counts: dict[str, int] = {}
    applied: list[dict[str, Any]] = []

    for match in matches:
        match_id = str(match.get("match_id") or "")
        league = match.get("league_orig")
        home = match.get("home_orig")
        away = match.get("away_orig")
        kickoff = str(match.get("hk_time") or "")
        score_text = str(match.get("score") or "")
        source_url = match.get("source_url")
        if not all((match_id, league, home, away, kickoff, score_text, source_url)):
            raise ValueError(f"incomplete verified identity: {match_id or 'unknown'}")

        candidates = [
            row for row in rows
            if isinstance(row, dict) and str(row.get("match_id") or "") == match_id
        ]
        if not candidates:
            raise ValueError(f"history fixture missing: {match_id}")

        home_score, away_score = (int(value) for value in score_text.split("-", 1))
        actual = (
            "主勝" if home_score > away_score
            else ("和局" if home_score == away_score else "客勝")
        )
        score = {
            "home_score": home_score,
            "away_score": away_score,
            "corners_total": None,
        }

        for row in candidates:
            if row.get("league") != league:
                raise ValueError(f"history league mismatch: {match_id}")
            if row.get("home") != home or row.get("away") != away:
                raise ValueError(f"history team identity mismatch: {match_id}")
            if str(row.get("kickoff") or "")[:10] != kickoff[:10]:
                raise ValueError(f"history kickoff date mismatch: {match_id}")
            if row.get("result_status") in FINAL:
                if row.get("result_status") == "已核對" and row.get("score") == score_text:
                    already_rows += 1
                    continue
                raise ValueError(f"existing result conflict: {match_id}")

            row.update({
                "actual": actual,
                "score": score_text,
                "correct": row.get("forecast") == actual if row.get("forecast") else None,
                "result_status": "已核對",
                "verified_at": verified_at,
                "result_source": "gemini_reviewed_multi_source_audit",
                "result_detail": {
                    **score,
                    "audit_batch_id": batch_id,
                    "source_name": match.get("source_name"),
                    "source_url": source_url,
                    "score_scope": SCORE_SCOPE,
                    "review_note": match.get("note"),
                },
                "market_grades": [
                    _grade_market(prediction, score)
                    for prediction in (row.get("market_predictions") or [])
                    if isinstance(prediction, dict)
                ],
                "result_missing_reason": None,
            })
            changed_rows += 1

        fixture_row_counts[match_id] = len(candidates)
        applied.append({
            "match_id": match_id,
            "fixture": f"{home} vs {away}",
            "score": score_text,
            "source_url": source_url,
        })

    proposed.setdefault("reviewed_result_backfills", {})[batch_id] = {
        "verified_at": verified_at,
        "fixtures": len(matches),
        "changed_rows": changed_rows,
        "already_rows": already_rows,
        "source": "Gemini multi-source research",
        "applied": copy.deepcopy(applied),
    }
    report = {
        "mode": "proposal_only",
        "batch_id": batch_id,
        "verified_fixtures": len(matches),
        "changed_rows": changed_rows,
        "already_rows": already_rows,
        "fixture_row_counts": fixture_row_counts,
        "before_hash": _hash(history),
        "after_hash": _hash(proposed),
        "input_unchanged": True,
        "applied": applied,
    }
    return proposed, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--verified-results", required=True, type=Path)
    parser.add_argument("--proposal-out", required=True, type=Path)
    parser.add_argument("--report-out", required=True, type=Path)
    parser.add_argument("--batch-id", default=DEFAULT_BATCH_ID)
    parser.add_argument("--verified-at", default=DEFAULT_VERIFIED_AT)
    args = parser.parse_args()

    for output in (args.proposal_out, args.report_out):
        _different_file(args.history, output)

    input_hash = hashlib.sha256(args.history.read_bytes()).hexdigest()
    history = json.loads(args.history.read_text(encoding="utf-8"))
    verified = json.loads(args.verified_results.read_text(encoding="utf-8"))
    proposed, report = build_proposal(
        history,
        verified,
        batch_id=args.batch_id,
        verified_at=args.verified_at,
    )
    report["input_sha256"] = input_hash
    report["input_sha256_unchanged"] = hashlib.sha256(
        args.history.read_bytes()
    ).hexdigest() == input_hash

    write_json_atomic(args.proposal_out, proposed)
    report["proposal_out"] = str(args.proposal_out)
    write_json_atomic(args.report_out, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["input_sha256_unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
