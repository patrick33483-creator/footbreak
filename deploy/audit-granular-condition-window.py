#!/usr/bin/env python3
"""Read-only Footbreak/Crown granular-condition window audit."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HKT = timezone(timedelta(hours=8))
SYSTEMS = ("footbreak", "crown")
MARKET_LABELS = {"HDC": "讓球", "HIL": "入球大細", "CHL": "角球大細"}


def parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=HKT) if parsed.tzinfo is None else parsed.astimezone(HKT)


def load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not a JSON object")
    return payload


def condition_rows(ledger: dict[str, Any], system: str) -> list[tuple[str, dict[str, Any]]]:
    output: list[tuple[str, dict[str, Any]]] = []
    for row in ledger.get("bets") or []:
        if (
            isinstance(row, dict)
            and row.get("portfolio") == f"{system}_wilson_test"
        ):
            output.append(("formal_bet", row))
    namespace = ledger.get("wilson_validation") or {}
    for row in namespace.get("observations") or []:
        if (
            isinstance(row, dict)
            and row.get("portfolio") == f"{system}_wilson_observations"
            and row.get("formal_bet") is False
        ):
            output.append(("observation", row))
    return output


def latest_learning_results(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not path.is_file():
        return {}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    query = """
        SELECT r.*
          FROM results r
          JOIN (
                SELECT system, fixture_id, MAX(result_attempt) AS result_attempt
                  FROM results
                 GROUP BY system, fixture_id
               ) latest
            ON latest.system=r.system
           AND latest.fixture_id=r.fixture_id
           AND latest.result_attempt=r.result_attempt
    """
    output: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        for raw in connection.execute(query):
            row = dict(raw)
            provenance = {}
            try:
                provenance = json.loads(row.get("provenance_json") or "{}")
            except json.JSONDecodeError:
                provenance = {"parse_error": True}
            output[(str(row["system"]), str(row["fixture_id"]))] = {
                "home_score": row.get("home_score"),
                "away_score": row.get("away_score"),
                "home_corners": row.get("home_corners"),
                "away_corners": row.get("away_corners"),
                "terminal_status": row.get("terminal_status"),
                "observed_at": row.get("observed_at"),
                "recorded_at": row.get("recorded_at"),
                "provenance": provenance,
            }
    finally:
        connection.close()
    return output


def saved_learning_results(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("saved learning results must be a JSON array")
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        provenance = row.pop("provenance_json", {})
        if isinstance(provenance, str):
            try:
                provenance = json.loads(provenance)
            except json.JSONDecodeError:
                provenance = {"parse_error": True}
        row["provenance"] = provenance
        output[(str(row.pop("system")), str(row.pop("fixture_id")))] = row
    return output


def compact_definition(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value.get(key)
        for key in (
            "system", "market", "path", "decision", "tier", "direction",
            "role", "bucket", "movement", "tier_path", "stage",
        )
        if value.get(key) is not None
    }


def score_text(row: dict[str, Any]) -> str | None:
    score = row.get("score")
    if score not in (None, ""):
        return str(score)
    home = row.get("home_score")
    away = row.get("away_score")
    return f"{home}-{away}" if home is not None and away is not None else None


def audit(
    ledgers: dict[str, dict[str, Any]],
    learning_results: dict[tuple[str, str], dict[str, Any]],
    start: datetime,
    end: datetime,
    generated_at: datetime,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    outside_or_invalid = Counter()
    for system in SYSTEMS:
        for kind, row in condition_rows(ledgers[system], system):
            kickoff = parse_time(row.get("kickoff"))
            if kickoff is None:
                outside_or_invalid[f"{system}_missing_kickoff"] += 1
                continue
            if not (start <= kickoff < end):
                outside_or_invalid[f"{system}_outside_window"] += 1
                continue
            fixture = str(row.get("match_id") or "")
            result = learning_results.get((system, fixture))
            due = kickoff + timedelta(hours=3) <= generated_at
            status = str(row.get("status") or "")
            candidate_available = bool(
                result
                and result.get("home_score") is not None
                and result.get("away_score") is not None
                and str(result.get("terminal_status") or "").upper()
                not in {"", "PENDING", "SCHEDULED", "LIVE"}
            )
            entries.append({
                "system": system,
                "row_kind": kind,
                "entry_id": row.get("bet_id") or row.get("observation_id"),
                "match_id": fixture,
                "hkjc_match_id": row.get("hkjc_match_id"),
                "league": row.get("league"),
                "home": row.get("home"),
                "away": row.get("away"),
                "kickoff_hkt": kickoff.isoformat(),
                "created_at": row.get("created_at"),
                "native_stage_at": row.get("native_stage_at"),
                "stage": row.get("stage"),
                "market": row.get("code") or row.get("market"),
                "market_label": row.get("market_label") or MARKET_LABELS.get(str(row.get("code") or "")),
                "side": row.get("selected_side") or row.get("side"),
                "selected_role": row.get("selected_role"),
                "line": row.get("selected_line", row.get("line", row.get("condition"))),
                "odds": row.get("odds"),
                "condition_number": row.get("condition_number"),
                "condition_signature": row.get("frozen_condition_signature"),
                "condition_definition": compact_definition(row.get("frozen_condition_definition")),
                "historical_evidence": row.get("frozen_historical_evidence"),
                "bet_status": row.get("bet_status"),
                "status": status,
                "result": row.get("result"),
                "score": score_text(row),
                "settlement_source": row.get("settlement_source"),
                "settled_at": row.get("settled_at"),
                "pending_reason": row.get("settlement_pending_reason"),
                "last_settlement_attempt_at": row.get("last_settlement_attempt_at"),
                "settlement_due": due,
                "learning_result_candidate": result,
                "safe_candidate_available": candidate_available,
            })
    entries.sort(key=lambda row: (
        row["kickoff_hkt"], row["system"], row["match_id"],
        str(row["condition_number"]), row["stage"], row["market"],
    ))

    by_condition: dict[str, dict[str, Any]] = {}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in entries:
        grouped[(row["system"], str(row["condition_signature"]))].append(row)
    for (system, signature), rows in grouped.items():
        first = rows[0]
        key = f"{system}:{signature}"
        by_condition[key] = {
            "system": system,
            "condition_number": first["condition_number"],
            "condition_signature": signature,
            "definition": first["condition_definition"],
            "entry_count": len(rows),
            "fixture_count": len({row["match_id"] for row in rows}),
            "fixtures": sorted({row["match_id"] for row in rows}),
        }

    pending = [row for row in entries if row["status"] == "PENDING"]
    repair_candidates = [
        row for row in pending
        if row["settlement_due"] and row["safe_candidate_available"]
    ]
    overdue_unresolved = [
        row for row in pending
        if row["settlement_due"] and not row["safe_candidate_available"]
    ]
    not_due = [row for row in pending if not row["settlement_due"]]
    return {
        "report": "granular_condition_window_audit",
        "read_only": True,
        "window": {
            "timezone": "Asia/Hong_Kong",
            "start_inclusive": start.isoformat(),
            "end_exclusive": end.isoformat(),
            "basis": "fixture kickoff",
        },
        "generated_at": generated_at.isoformat(),
        "summary": {
            "entries": len(entries),
            "unique_fixtures": len({(row["system"], row["match_id"]) for row in entries}),
            "formal_bets": sum(row["row_kind"] == "formal_bet" for row in entries),
            "observations": sum(row["row_kind"] == "observation" for row in entries),
            "settled": sum(row["status"] == "SETTLED" for row in entries),
            "pending": len(pending),
            "pending_not_due": len(not_due),
            "pending_overdue_with_candidate": len(repair_candidates),
            "pending_overdue_without_candidate": len(overdue_unresolved),
        },
        "entries": entries,
        "conditions": sorted(
            by_condition.values(),
            key=lambda row: (row["system"], row["condition_number"] or 999999, row["condition_signature"]),
        ),
        "pending": {
            "not_due": not_due,
            "repair_candidates": repair_candidates,
            "overdue_unresolved": overdue_unresolved,
        },
        "diagnostics": dict(outside_or_invalid),
    }


def fmt_definition(definition: dict[str, Any]) -> str:
    return "；".join(f"{key}={value}" for key, value in definition.items()) or "—"


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 足破與皇冠細緻條件逐場稽核",
        "",
        f"產生時間：{report['generated_at']}",
        f"時間窗：{report['window']['start_inclusive']} 至 {report['window']['end_exclusive']}（尾端不包含）",
        "篩選基準：賽事開賽時間；全程唯讀。",
        "",
        "## 總覽",
        "",
        f"- 條件紀錄：{summary['entries']}",
        f"- 系統內唯一場次：{summary['unique_fixtures']}",
        f"- 正式模擬注：{summary['formal_bets']}",
        f"- 純觀察：{summary['observations']}",
        f"- 已結算：{summary['settled']}",
        f"- Pending：{summary['pending']}（未到期 {summary['pending_not_due']}；逾期有可信候選 {summary['pending_overdue_with_candidate']}；逾期未有候選 {summary['pending_overdue_without_candidate']}）",
        "",
        "## 條件對應場次",
        "",
        "| 系統 | 條件 | 定義 | 紀錄 | 場次 |",
        "|---|---:|---|---:|---|",
    ]
    for row in report["conditions"]:
        lines.append(
            f"| {row['system']} | #{row['condition_number']} | "
            f"{fmt_definition(row['definition'])} | {row['entry_count']} | "
            f"{', '.join(row['fixtures'])} |"
        )
    lines.extend([
        "",
        "## 逐場明細",
        "",
        "| 開賽 | 系統 | 場次 | 條件 | 類型 | 階段/市場 | 選擇 | 賠率 | 狀態 | 賽果 |",
        "|---|---|---|---:|---|---|---|---:|---|---|",
    ])
    for row in report["entries"]:
        fixture = f"{row['home']} vs {row['away']} ({row['match_id']})"
        selection = f"{row['selected_role'] or row['side']} {row['line']}"
        outcome = row["result"] or row["score"] or "—"
        lines.append(
            f"| {row['kickoff_hkt']} | {row['system']} | {fixture} | "
            f"#{row['condition_number']} | {row['row_kind']} | "
            f"{row['stage']}/{row['market']} | {selection} | "
            f"{row['odds']} | {row['status']} | {outcome} |"
        )
    lines.extend(["", "## Pending 核對", ""])
    for title, key in (
        ("未到結算時間", "not_due"),
        ("逾期且已有可信賽果候選", "repair_candidates"),
        ("逾期但未有可信賽果候選", "overdue_unresolved"),
    ):
        rows = report["pending"][key]
        lines.extend([f"### {title}", ""])
        if not rows:
            lines.append("- 無")
        for row in rows:
            candidate = row.get("learning_result_candidate") or {}
            score = (
                f"{candidate.get('home_score')}-{candidate.get('away_score')}"
                if candidate.get("home_score") is not None else "—"
            )
            lines.append(
                f"- {row['system']}｜{row['home']} vs {row['away']}｜"
                f"條件 #{row['condition_number']}｜{row['stage']}/{row['market']}｜"
                f"候選賽果 {score}｜pending 原因 {row['pending_reason'] or '未記錄'}"
            )
        lines.append("")
    lines.extend([
        "## 安全聲明",
        "",
        "- 本報告沒有修改任何 ledger、prediction history、learning database 或 dashboard。",
        "- 「可信賽果候選」只表示不可變 learning database 已有同系統、同 fixture ID 的終局比分；仍需人工確認後才可執行正式結算。",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--footbreak-ledger", type=Path, default=Path("/opt/footbreak/system/sim_ledger.json"))
    parser.add_argument("--crown-ledger", type=Path, default=Path("/var/lib/footbreak/crown/ledger.json"))
    parser.add_argument("--learning-db", type=Path, default=Path("/var/lib/footbreak/learning/predictions.sqlite"))
    parser.add_argument("--learning-results-json", type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()
    start, end = parse_time(args.start), parse_time(args.end)
    if start is None or end is None or start >= end:
        raise SystemExit("invalid audit window")
    report = audit(
        {
            "footbreak": load(args.footbreak_ledger),
            "crown": load(args.crown_ledger),
        },
        (
            saved_learning_results(args.learning_results_json)
            if args.learning_results_json
            else latest_learning_results(args.learning_db)
        ),
        start,
        end,
        datetime.now(HKT),
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    markdown = render_markdown(report)
    if args.json_output:
        args.json_output.write_text(encoded, encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.write_text(markdown, encoding="utf-8")
    print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
