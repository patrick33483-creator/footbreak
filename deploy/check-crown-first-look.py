#!/usr/bin/env python3
"""Fail visibly when an eligible future Crown card still lacks a first look."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

APP_DIR = Path("/opt/footbreak")
if not APP_DIR.exists():
    APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from crown.common import HKT, parse_time  # noqa: E402
from crown.ledger import PREDICTION_ERA, completed_stages  # noqa: E402
from crown.matching import MATCHING_VERSION  # noqa: E402
from crown.period import in_current_period  # noqa: E402


def _load(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _has_valid_crown_quote(card: dict[str, Any]) -> bool:
    rows = ((card.get("book_odds") or {}).get("crown") or [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        market = str(row.get("market") or row.get("code") or "").upper()
        side = str(row.get("selection") or row.get("side") or "").upper()
        odds = _finite(row.get("odds"))
        line = _finite(row.get("line"))
        if (
            market in {"HDC", "HIL"}
            and side in {"H", "A", "L"}
            and odds is not None
            and odds > 1
            and line is not None
        ):
            return True
    return False


def build_report(state_dir: Path, now: datetime | None = None) -> dict[str, Any]:
    checked_at = (now or datetime.now(HKT)).astimezone(HKT)
    predictions = _load(state_dir / "predictions.json", [])
    ledger = _load(state_dir / "ledger.json", {})
    predictions = predictions if isinstance(predictions, list) else []
    watches = (ledger.get("watch") or {}) if isinstance(ledger, dict) else {}
    watches = watches if isinstance(watches, dict) else {}

    eligible = 0
    missing: list[str] = []
    for card in predictions:
        if not isinstance(card, dict):
            continue
        match_id = str(card.get("match_id") or "").strip()
        kickoff = parse_time(card.get("kickoff_hkt") or card.get("kickoff"))
        if (
            not match_id
            or kickoff is None
            or kickoff <= checked_at
            or not in_current_period(kickoff, checked_at)
            or not _has_valid_crown_quote(card)
        ):
            continue
        eligible += 1
        done = completed_stages(
            watches.get(match_id) if isinstance(watches.get(match_id), dict) else {},
            MATCHING_VERSION,
            PREDICTION_ERA,
        )
        if "首預" not in done:
            missing.append(
                hashlib.sha256(
                    f"{match_id}|{kickoff.astimezone(HKT).isoformat()}".encode("utf-8")
                ).hexdigest()[:16]
            )

    return {
        "report": "crown_first_look_health_v1",
        "read_only": True,
        "provider_requests": False,
        "checked_at_hkt": checked_at.isoformat(),
        "eligible_future_cards": eligible,
        "missing_first_look": len(missing),
        "healthy": not missing,
        "missing_fixture_keys": missing[:10],
        "missing_fixture_keys_truncated": len(missing) > 10,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("/var/lib/footbreak/crown"),
    )
    parser.add_argument("--now", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    now = parse_time(args.now) if args.now else None
    report = build_report(args.state_dir, now)
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
