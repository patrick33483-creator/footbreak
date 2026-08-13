"""Idempotent Crown three-stage HDC Telegram signal notifications."""
from __future__ import annotations

import json
import math
import urllib.request
from typing import Any

from .common import iso_hkt, now_hkt, parse_time, read_json, write_json_atomic
from .config import Settings
from .state import paths, state_lock
from analysis.three_stage_consensus import calculate_three_stage_consensus


MIN_T5_SIGNAL_ODDS = 1.70


def _quarter_line(value: Any, signed: bool = True) -> str:
    try:
        line = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    sign = ("-" if line < 0 else "+" if line > 0 else "") if signed else ""
    amount = abs(line)
    quarters = round(amount * 4)
    whole, rem = divmod(quarters, 4)
    if rem == 0:
        body = str(whole)
    elif rem == 1:
        body = f"{whole}/{whole + 0.5:g}"
    elif rem == 2:
        body = f"{whole + 0.5:g}"
    else:
        body = f"{whole + 0.5:g}/{whole + 1}"
    return f"{sign}{body}"


def _bet_label(bet: dict[str, Any]) -> str:
    market = str(bet.get("market") or bet.get("code") or "")
    side = str(bet.get("side") or "")
    line = bet.get("line", bet.get("condition"))
    if market == "HDC":
        team = bet.get("home") if side == "H" else bet.get("away")
        # Stored handicap is from the home-team viewpoint.  Convert it to the
        # selected team's viewpoint before displaying an away-side bet.
        selected_line = -float(line) if side == "A" and line is not None else line
        return f"讓球 · {team} {_quarter_line(selected_line)}"
    if market == "HIL":
        return f"入球大細 · {'大' if side == 'H' else '細'} {_quarter_line(line, signed=False)}"
    if market == "HKJC角球大細" or bet.get("code") == "CHL":
        return f"HKJC角球大細 · {'大' if side == 'H' else '細'} {_quarter_line(line, signed=False)}"
    return str(bet.get("label") or f"{market} {side} {line}")


def _load(config: Settings) -> dict[str, Any]:
    state = read_json(paths(config)["notify"], {"bets": []})
    state.setdefault("bets", [])
    # Keep the retired corner key for compatibility with state already on a
    # server.  New signals use their own versioned keys and never inspect
    # historical predictions.
    state.setdefault("corner_t5", [])
    state.setdefault("signals", [])
    return state


def _send(config: Settings, text: str) -> bool:
    if not (config.telegram_enabled and config.telegram_bot_token and config.telegram_chat_id):
        return False
    body = json.dumps({"chat_id": config.telegram_chat_id, "text": text}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20):
        pass
    return True


def _finite_positive(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) and numeric > 0 else None


def _numeric_line(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _stage_market(stage: dict[str, Any], code: str) -> dict[str, Any] | None:
    """Read only the immutable selected-market snapshot, never an opposite quote."""
    rows = [
        row for row in (stage.get("market_predictions") or [])
        if isinstance(row, dict) and str(row.get("code") or "") == code
    ]
    # A stage is malformed/ambiguous if it persisted more than one selected
    # direction for this market.  Do not try to choose one after the fact.
    return rows[0] if len(rows) == 1 else None


def _fixture_identity(watch: dict[str, Any], stage: dict[str, Any]) -> str | None:
    """Return a stable, complete identity without any fuzzy/fallback match."""
    fields = ("match_id", "kickoff_hkt", "home", "away")
    values = {field: str(stage.get(field) or "").strip() for field in fields}
    if not all(values.values()):
        return None
    if str(watch.get("match_id") or "").strip() != values["match_id"]:
        return None
    for field in ("kickoff_hkt", "home", "away"):
        watch_value = str(
            watch.get(field) or (watch.get("kickoff") if field == "kickoff_hkt" else "")
        ).strip()
        if not watch_value or watch_value != values[field]:
            return None
    return "|".join(values[field] for field in fields)


def _fresh_stage(
    ledger: dict[str, Any], fresh_t5: Any
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str] | None:
    """Resolve only an ID handed over by the just-completed persistence pass."""
    match_id = (
        str(fresh_t5.get("match_id") or "")
        if isinstance(fresh_t5, dict)
        else str(fresh_t5 or "")
    )
    watch = (ledger.get("watch") or {}).get(match_id)
    if not isinstance(watch, dict):
        return None
    rows = [
        row for row in (watch.get("stages") or [])
        if isinstance(row, dict) and row.get("stage") == "T-5"
    ]
    if len(rows) != 1:
        return None
    stage = rows[0]
    identity = _fixture_identity(watch, stage)
    if identity is None:
        return None
    kickoff = parse_time(str(stage.get("kickoff_hkt") or ""))
    if kickoff is None or kickoff <= now_hkt():
        return None
    return watch, stage, {"kickoff": kickoff}, identity


def _signal_message(
    system: str,
    stage: dict[str, Any],
    kickoff,
    market: str,
    selection: str,
    line: str,
    odds: float,
    reason: str,
) -> str:
    league = str(stage.get("league") or "").strip()
    lines = [
        f"{system} T-5 訊號",
        f"開賽：{kickoff.strftime('%d/%m %H:%M')} HKT" + (f" · {league}" if league else ""),
        f"對賽：{stage.get('home') or ''} vs {stage.get('away') or ''}",
        f"市場：{market}",
        f"選擇：{selection}",
        f"盤口：{line}",
        f"選項實際賠率：{odds:.3f}",
        f"觸發：{reason}",
        "只作通知，絕不實際投注。",
    ]
    return "\n".join(lines)


def _hdc_signal(
    history_rows: list[dict[str, Any]],
    watch: dict[str, Any],
    stage: dict[str, Any],
    kickoff,
    identity: str,
) -> tuple[str, str] | None:
    """Three exact persisted stages must agree on one HDC direction and line."""
    ordered: list[dict[str, Any]] = []
    for name in ("首預", "T-30", "T-5"):
        rows = [
            row for row in (watch.get("stages") or [])
            if isinstance(row, dict) and row.get("stage") == name
        ]
        if len(rows) != 1 or _fixture_identity(watch, rows[0]) != identity:
            return None
        selected = _stage_market(rows[0], "HDC")
        if selected is None:
            return None
        ordered.append(selected)
    sides = [str(row.get("side") or "") for row in ordered]
    lines = [_numeric_line(row.get("line", row.get("condition"))) for row in ordered]
    if any(side not in {"H", "A"} for side in sides) or any(line is None for line in lines):
        return None
    # Exact numeric equality, not a rounded/display comparison.
    if len(set(sides)) != 1 or not (lines[0] == lines[1] == lines[2]):
        return None
    odds = _finite_positive(ordered[-1].get("odds"))
    if odds is None or odds < MIN_T5_SIGNAL_ODDS:
        return None
    selected_side, home_line = sides[-1], lines[-1]
    team_raw = stage.get("home") if selected_side == "H" else stage.get("away")
    team = str(team_raw or "").strip()
    if not team:
        return None
    selected_line = home_line if selected_side == "H" else -home_line
    line_text = _quarter_line(selected_line)
    condition = _hdc_condition_label(history_rows, selected_side, home_line)
    key = f"crown|{identity}|T-5|HDC|three-stage-v1|{selected_side}|{home_line:g}"
    return key, _signal_message(
        "皇冠", stage, kickoff, "皇冠讓球", f"{team} {line_text}", line_text, odds,
        f"首預、T-30、T-5 三段同一方向、同一盤口。\n條件：{condition}",
    )


def _hdc_condition(side: str, home_line: float) -> tuple[str, str] | None:
    if side not in {"H", "A"}:
        return None
    if abs(home_line) < 1e-9:
        return (
            ("scratch_home", "平手盤（主）")
            if side == "H"
            else ("scratch_away", "平手盤（客）")
        )
    if side == "H":
        return (
            ("home_giving", "主讓")
            if home_line < 0
            else ("home_receiving", "主受讓")
        )
    return (
        ("away_giving", "客讓")
        if home_line > 0
        else ("away_receiving", "客受讓")
    )


def _hdc_condition_label(
    history_rows: list[dict[str, Any]], selected_side: str, home_line: float
) -> str:
    category = _hdc_condition(selected_side, home_line)
    if category is None:
        return "讓球類型未能分類"
    key, label = category
    stats = calculate_three_stage_consensus(history_rows)
    breakdown = (
        (((stats.get("markets") or {}).get("HDC") or {})
         .get("same_direction_and_line") or {})
        .get("breakdown") or []
    )
    item = next((row for row in breakdown if row.get("key") == key), None)
    decided = int((item or {}).get("decided") or 0)
    hits = int((item or {}).get("hits") or 0)
    accuracy = (item or {}).get("accuracy")
    if decided <= 0 or accuracy is None:
        return f"{label}≥1.70 累積中（0/0）"
    return f"{label}≥1.70 {float(accuracy) * 100:.1f}%（{hits}/{decided}）"


def _fresh_signal_events(
    ledger: dict[str, Any],
    fresh_t5_predictions: list[Any] | None,
    history_rows: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Build notifications from fresh or still-upcoming unacknowledged T-5 rows.

    The bounded recovery scan is intentionally limited by ``_fresh_stage`` to
    fixtures whose kickoff is still in the future.  It recovers a signal after
    a deployment boundary or temporary Telegram failure without ever sending a
    stale betting prompt after kickoff.
    """
    events: list[tuple[str, str]] = []
    items = list(fresh_t5_predictions or [])
    items.extend(str(match_id) for match_id in (ledger.get("watch") or {}))
    seen_items: set[str] = set()
    for item in items:
        match_id = (
            str(item.get("match_id") or "")
            if isinstance(item, dict)
            else str(item or "")
        )
        if not match_id or match_id in seen_items:
            continue
        seen_items.add(match_id)
        resolved = _fresh_stage(ledger, item)
        if resolved is None:
            continue
        watch, stage, context, identity = resolved
        event = _hdc_signal(history_rows, watch, stage, context["kickoff"], identity)
        if event is not None:
            events.append(event)
    return events


def notify_new(
    ledger: dict[str, Any],
    config: Settings,
    fresh_t5_predictions: list[Any] | None = None,
) -> int:
    # Sweep and tick intentionally fetch providers concurrently.  Serialize
    # the notification read/send/commit so both processes cannot send the same
    # bet after reading the same old notify state.
    with state_lock(config):
        state, sent = _load(config), 0
        history = read_json(config.state_dir / "prediction_history.json", {})
        history_rows = (
            history.get("rows") or []
            if isinstance(history, dict)
            else history if isinstance(history, list) else []
        )
        history_rows = [row for row in history_rows if isinstance(row, dict)]
        # Crown simulated-bet notifications are retired.  Keep the old state
        # key readable so an existing server state remains schema-compatible.
        seen_signals = set(state["signals"])
        for notification_id, message in _fresh_signal_events(
            ledger, fresh_t5_predictions, history_rows
        ):
            if notification_id in seen_signals:
                continue
            delivered = _send(config, message)
            if delivered is False:
                continue
            state["signals"].append(notification_id)
            seen_signals.add(notification_id)
            sent += 1
            # Commit each signal key immediately.  If a later independent
            # signal's transport call fails, a retry cannot duplicate this
            # one; no prediction/ledger state is involved here.
            state["updated_at"] = iso_hkt()
            write_json_atomic(paths(config)["notify"], state)
        state["updated_at"] = iso_hkt()
        write_json_atomic(paths(config)["notify"], state)
        return sent
