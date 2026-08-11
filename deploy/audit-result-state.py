#!/usr/bin/env python3
"""Emit a compact, read-only production result-state audit as JSON."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


FOOTBREAK_DATA = Path("/var/www/footbreak/data.json")
CROWN_HISTORY = Path("/var/lib/footbreak/crown/prediction_history.json")
CHALLENGER_STATUS = Path("/var/lib/footbreak/challenger/latest.json")
HKT = timezone(timedelta(hours=8))


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        value = payload.get("rows") or []
        return value if isinstance(value, list) else []
    return []


def compact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "match_id",
            "hkjc_match_id",
            "titan_match_id",
            "kickoff",
            "home",
            "away",
            "stage",
            "result_status",
            "score",
            "result_source",
            "result_missing_reason",
            "result_detail",
            "market_predictions",
            "market_grades",
        )
    }


def timer_state() -> dict[str, str]:
    completed = subprocess.run(
        [
            "systemctl",
            "show",
            "footbreak-result-reconcile.timer",
            "--no-pager",
            "--property=ActiveState",
            "--property=UnitFileState",
            "--property=LastTriggerUSec",
            "--property=NextElapseUSecRealtime",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    state: dict[str, str] = {"returncode": str(completed.returncode)}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            state[key] = value
    if completed.stderr.strip():
        state["stderr"] = completed.stderr.strip()
    return state


def challenger_state() -> dict[str, Any]:
    if not CHALLENGER_STATUS.is_file():
        return {"available": False}
    payload = load(CHALLENGER_STATUS)
    systems: dict[str, Any] = {}
    for system, system_payload in (payload.get("systems") or {}).items():
        tests = (system_payload or {}).get("tests") or {}
        systems[system] = {
            "review_required": bool((system_payload or {}).get("review_required")),
            "tests": {
                market: {
                    key: test.get(key)
                    for key in (
                        "status",
                        "eligible_fixtures",
                        "eligible_rows",
                        "required_fixtures",
                        "remaining_fixtures",
                        "train_fixtures",
                        "holdout_fixtures",
                        "train_rows",
                        "holdout_rows",
                        "champion",
                        "challenger",
                        "delta",
                        "checks",
                        "rejection_reasons",
                        "model_version_hash",
                        "auto_apply",
                    )
                    if key in test
                }
                for market, test in tests.items()
            },
        }
    return {
        "available": True,
        "generated_at": payload.get("generated_at"),
        "policy": payload.get("policy"),
        "review_required": bool(payload.get("review_required")),
        "systems": systems,
    }


def main() -> None:
    footbreak = load(FOOTBREAK_DATA)
    crown = load(CROWN_HISTORY)
    crown_rows = rows(crown)
    footbreak_rows = rows(footbreak.get("prediction_history") or {})
    cutoff = datetime.now(HKT) - timedelta(hours=4)

    def relevant(row: dict[str, Any]) -> bool:
        names = f"{row.get('home', '')} {row.get('away', '')}"
        if str(row.get("titan_match_id") or row.get("match_id") or "") == "3031468":
            return True
        if any(token in names for token in ("中央", "南市", "利昂女足", "堤格雷斯女足", "FC江原", "大阪飛腳")):
            return True
        try:
            kickoff = datetime.fromisoformat(
                str(row.get("kickoff") or "").replace("Z", "+00:00")
            )
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=HKT)
        except ValueError:
            return False
        return (
            kickoff < cutoff
            and row.get("result_status") not in {"已核對", "不計"}
        )

    audit = {
        "generated_at": datetime.now(HKT).isoformat(),
        "server_timer": timer_state(),
        "challenger": challenger_state(),
        "crown": {
            "stats": crown.get("stats"),
            "result_sync": crown.get("result_sync"),
            "relevant_rows": [compact(row) for row in crown_rows if relevant(row)],
            "row_count": len(crown_rows),
        },
        "footbreak": {
            "stats": (footbreak.get("prediction_history") or {}).get("stats"),
            "row_count": len(footbreak_rows),
            "t5_hdc_rows": [
                compact(row)
                for row in footbreak_rows
                if row.get("stage") == "T-5"
                and any(
                    isinstance(grade, dict)
                    and grade.get("code") == "HDC"
                    and grade.get("grade_status") == "GRADED"
                    for grade in (row.get("market_grades") or [])
                )
            ],
        },
    }
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
