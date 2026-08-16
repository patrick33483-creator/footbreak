#!/usr/bin/env python3
"""Row-free, read-only Crown Telegram notification audit."""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from crown.common import HKT, now_hkt, parse_time
from crown.config import settings
from crown.state import paths, read_json


WINDOW_HOURS = min(24, max(1, int(os.getenv("CROWN_NOTIFY_AUDIT_HOURS", "12"))))


def _valid_odds(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 1
    except (TypeError, ValueError):
        return False


def _stage_time(stage: dict[str, Any]) -> Any:
    return parse_time(stage.get("ts") or stage.get("source_snapshot_at"))


def main() -> None:
    config = settings()
    state_paths = paths(config)
    ledger = read_json(state_paths["ledger"], {})
    notify_state = read_json(state_paths["notify"], {})
    watches = ledger.get("watch") if isinstance(ledger, dict) else {}
    watches = watches if isinstance(watches, dict) else {}
    signal_ids = {
        str(item) for item in (notify_state.get("signals") or [])
        if isinstance(item, str)
    }

    cutoff = now_hkt() - timedelta(hours=WINDOW_HOURS)
    fresh_events: list[dict[str, str]] = []
    stage_counts: Counter[str] = Counter()
    rows_with_valid_odds: Counter[str] = Counter()
    market_rows_with_valid_odds: Counter[str] = Counter()
    latest_stage_at = None
    for match_id, watch in watches.items():
        if not isinstance(watch, dict):
            continue
        for stage in watch.get("stages") or []:
            if not isinstance(stage, dict):
                continue
            name = str(stage.get("stage") or "")
            observed = _stage_time(stage)
            if name not in {"T-30", "T-5"} or observed is None or observed < cutoff:
                continue
            stage_counts[name] += 1
            valid_markets = [
                str(item.get("code") or "")
                for item in (stage.get("market_predictions") or [])
                if isinstance(item, dict) and _valid_odds(item.get("odds"))
            ]
            if valid_markets:
                rows_with_valid_odds[name] += 1
            for market in valid_markets:
                market_rows_with_valid_odds[f"{name}:{market}"] += 1
            fresh_events.append({"match_id": str(match_id), "stage": name})
            if latest_stage_at is None or observed > latest_stage_at:
                latest_stage_at = observed

    parsed_signal_counts: Counter[str] = Counter()
    for signal_id in signal_ids:
        parts = signal_id.split("|")
        if len(parts) == 5 and parts[0] == "crown" and parts[4] == "granular-v1":
            parsed_signal_counts[f"{parts[3]}:{parts[2]}"] += 1

    output = {
        "audit_window_hours": WINDOW_HOURS,
        "configuration": {
            "telegram_enabled": bool(config.telegram_enabled),
            "bot_token_configured": bool(config.telegram_bot_token),
            "chat_id_configured": bool(config.telegram_chat_id),
        },
        "recent_persisted_stages": dict(sorted(stage_counts.items())),
        "recent_stages_with_valid_selected_odds": dict(
            sorted(rows_with_valid_odds.items())
        ),
        "recent_valid_market_rows": dict(
            sorted(market_rows_with_valid_odds.items())
        ),
        "recent_latest_stage_at_hkt": (
            latest_stage_at.astimezone(HKT).isoformat() if latest_stage_at else None
        ),
        "all_time_acknowledged_signal_keys": dict(sorted(parsed_signal_counts.items())),
        "notify_state_updated_at": notify_state.get("updated_at"),
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
