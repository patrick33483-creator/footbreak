#!/usr/bin/env python3
"""Rolling ROI watchlist for weakening V2 segmented conditions.

This module is a fail-closed read-only monitor. It re-uses the frozen
CONDITIONS in crown.segmented_conditions and the same _prospective_metrics
math (via _pnl_from_settlement below), then reports rolling-30 ROI over the
most recent decided observations per watched condition.

Watched conditions (as of 2026-09-01 backfilled sample):

- A-HIL-OPEN-T5-OVER-180 : val-30% ROI -2.2% after backfill
- A-HDC-OPEN-AWAY-MINUS-050 : val-30% ROI -2.5% after backfill

Alert threshold: rolling-30 ROI < -5% AND rolling window has >= 20 decided
matches. Fires a Telegram message to the ops channel via the same bot
credentials already used by analysis.health_alert. Never mutates ledger,
never opens a learning DB.

Modes
-----
- report : compute and print JSON summary; no side effects
- notify : additionally send Telegram alert when threshold breached
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from crown.segmented_conditions import (
    CONDITIONS,
    _SETTLEMENT_LABEL,
    _SETTLEMENT_PNL,
    build_segmented_conditions,
)


HKT = timezone(timedelta(hours=8))

# Conditions currently on watchlist. Adding an id here without also having a
# matching CONDITIONS entry is a startup-time error.
WATCHED_IDS: tuple[str, ...] = (
    "A-HIL-OPEN-T5-OVER-180",
    "A-HDC-OPEN-AWAY-MINUS-050",
)

# Alert firing rule
ROLLING_WINDOW = 30
MIN_DECIDED_FOR_ALERT = 20
ALERT_ROI_FLOOR = -0.05  # -5%
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _validate_watched_ids() -> None:
    known = {c["id"] for c in CONDITIONS}
    unknown = [c for c in WATCHED_IDS if c not in known]
    if unknown:
        raise SystemExit(f"watched condition id(s) not in CONDITIONS: {unknown}")


def _pnl_from_settlement(settlement: str, odds: float | None) -> float | None:
    """Return per-unit PnL, or None if not decided / unpriced."""
    if settlement not in _SETTLEMENT_LABEL or odds is None:
        return None
    if settlement == "Won":
        return odds - 1.0
    if settlement == "Half Won":
        return (odds - 1.0) / 2.0
    return _SETTLEMENT_PNL.get(settlement, 0.0)


def _rolling(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Sort observations newest-first (already done by build_...) and take up
    to ROLLING_WINDOW decided items to compute hit% and ROI."""
    decided = []
    for obs in observations:
        pnl = _pnl_from_settlement(str(obs.get("settlement") or ""), obs.get("odds"))
        if pnl is None:
            continue
        decided.append({
            "kickoff": obs.get("kickoff"),
            "settlement": obs["settlement"],
            "odds": obs["odds"],
            "pnl": pnl,
        })
        if len(decided) >= ROLLING_WINDOW:
            break

    if not decided:
        return {
            "decided": 0,
            "hit_rate": None,
            "roi": None,
            "profit": 0.0,
        }

    hits = sum(
        1 for d in decided if d["settlement"] in ("Won", "Half Won")
    )
    priced_denom = sum(
        1 for d in decided if d["settlement"] != "Refunded"
    )
    profit = sum(d["pnl"] for d in decided)
    return {
        "decided": len(decided),
        "hit_rate": (hits / priced_denom) if priced_denom else None,
        "roi": (profit / len(decided)) if decided else None,
        "profit": profit,
    }


def _breach(rolling: dict[str, Any]) -> bool:
    if rolling["decided"] < MIN_DECIDED_FOR_ALERT:
        return False
    roi = rolling.get("roi")
    return roi is not None and roi < ALERT_ROI_FLOOR


def _send_telegram(token: str, chat_id: str, text: str) -> dict[str, Any]:
    url = TELEGRAM_API.format(token=token)
    data = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json; charset=utf-8",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _format_alert(breaches: list[dict[str, Any]], now: datetime) -> str:
    lines = [
        "<b>皇冠 V2 條件監察警報</b>",
        f"時間：{now.strftime('%Y-%m-%d %H:%M HKT')}",
        f"觸發：rolling-{ROLLING_WINDOW} ROI &lt; {ALERT_ROI_FLOOR:.0%}",
        "",
    ]
    for b in breaches:
        rolling = b["rolling"]
        lines.append(
            f"• <code>{b['id']}</code>  "
            f"n={rolling['decided']}  "
            f"hit={rolling['hit_rate']:.1%}  "
            f"ROI={rolling['roi']:+.2%}"
        )
    lines.append("")
    lines.append("建議：觀察多 10-20 場後決定是否降 tier")
    return "\n".join(lines)


def main() -> int:
    _validate_watched_ids()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        default="/opt/footbreak/prediction_history.json",
        help="Path to production prediction_history.json (read-only)",
    )
    parser.add_argument(
        "--mode",
        choices=["report", "notify"],
        default="report",
    )
    parser.add_argument(
        "--out",
        default="/tmp/segmented_condition_watchlist.json",
    )
    args = parser.parse_args()

    now = datetime.now(HKT)

    history_path = Path(args.history)
    if not history_path.exists():
        print(f"ERROR: {history_path} not found", file=sys.stderr)
        return 2

    # Read as row list — build_segmented_conditions expects the same shape as
    # the production reconciliation writes.
    with history_path.open("r", encoding="utf-8") as f:
        ledger = json.load(f)
    rows = ledger.get("rows") or ledger.get("stage_rows") or []
    if not isinstance(rows, list):
        print("ERROR: ledger.rows not a list", file=sys.stderr)
        return 2

    payload = build_segmented_conditions(rows, generated_at=now.isoformat())
    # Public and matching observations are separately populated; we need to
    # look at all_observations for tier B too. Filter by condition_id and
    # sort by kickoff desc (already done in build_...).
    all_obs = payload.get("matching_observations", [])

    results = []
    breaches = []
    for cid in WATCHED_IDS:
        cond_obs = [o for o in all_obs if o.get("condition_id") == cid]
        rolling = _rolling(cond_obs)
        entry = {
            "id": cid,
            "total_observations": len(cond_obs),
            "rolling": rolling,
            "breach": _breach(rolling),
        }
        results.append(entry)
        if entry["breach"]:
            breaches.append(entry)

    report = {
        "generated_at": now.isoformat(),
        "mode": args.mode,
        "rolling_window": ROLLING_WINDOW,
        "min_decided_for_alert": MIN_DECIDED_FOR_ALERT,
        "alert_roi_floor": ALERT_ROI_FLOOR,
        "watched_ids": list(WATCHED_IDS),
        "results": results,
        "breaches_count": len(breaches),
    }

    if args.mode == "notify" and breaches:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            report["telegram"] = {
                "sent": False,
                "reason": "missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID",
            }
        else:
            try:
                resp = _send_telegram(token, chat_id, _format_alert(breaches, now))
                report["telegram"] = {"sent": bool(resp.get("ok")), "response": resp}
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                report["telegram"] = {"sent": False, "error": str(exc)}

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({
        "watched": len(WATCHED_IDS),
        "breaches": len(breaches),
        "artifact": args.out,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
