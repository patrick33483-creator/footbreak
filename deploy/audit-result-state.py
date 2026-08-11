#!/usr/bin/env python3
"""Emit a compact, read-only production result-state audit as JSON."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


FOOTBREAK_DATA = Path("/var/www/footbreak/data.json")
CROWN_HISTORY = Path("/var/lib/footbreak/crown/prediction_history.json")
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
        "crown": {
            "stats": crown.get("stats"),
            "result_sync": crown.get("result_sync"),
            "relevant_rows": [compact(row) for row in crown_rows if relevant(row)],
            "row_count": len(crown_rows),
        },
        "footbreak": {
            "stats": (footbreak.get("prediction_history") or {}).get("stats"),
            "row_count": len(footbreak_rows),
        },
    }
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
