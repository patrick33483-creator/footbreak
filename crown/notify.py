"""Idempotent Crown granular-condition Telegram notifications."""
from __future__ import annotations

import json
import math
import re
import urllib.request
from typing import Any

from .common import HKT, iso_hkt, now_hkt, parse_time, read_json, write_json_atomic
from .config import Settings
from .state import paths, state_lock
from analysis.three_stage_consensus import calculate_three_stage_consensus


ODDS_TIER_THRESHOLD = 1.70
MARKET_LABELS = {"HDC": "讓球", "HIL": "入球大細", "CHL": "角球大細"}


def _public_condition_text(value: Any) -> str:
    """Keep internal codes and legacy abstract paths out of public copy."""
    text = str(value or "")
    for code, label in MARKET_LABELS.items():
        text = re.sub(rf"\b{code}\b", label, text)
    # New descriptors preserve observed Chinese roles (for example
    # 主讓→客受讓→主讓).  An old A/B/C-only artifact has lost that semantic
    # mapping, so never invent one: state the historical-label limitation
    # without leaking unexplained tokens.
    return re.sub(r"\b[ABC](?:→[ABC])+\b", "方向曾變化（舊紀錄未保留角色）", text)


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
        return f"角球大細 · {'大' if side == 'H' else '細'} {_quarter_line(line, signed=False)}"
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
        f"市場：{MARKET_LABELS.get(market, market)}",
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
    if odds is None:
        return None
    selected_side, home_line = sides[-1], lines[-1]
    team_raw = stage.get("home") if selected_side == "H" else stage.get("away")
    team = str(team_raw or "").strip()
    if not team:
        return None
    selected_line = home_line if selected_side == "H" else -home_line
    line_text = _quarter_line(selected_line)
    condition = _hdc_condition_label(
        history_rows, selected_side, home_line, odds
    )
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
    history_rows: list[dict[str, Any]],
    selected_side: str,
    home_line: float,
    selected_odds: float,
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
    if selected_odds >= ODDS_TIER_THRESHOLD:
        tier_label = "≥1.70"
        cohort = (item or {}).get("odds_bias", {}).get(
            "at_or_above_threshold", item or {}
        )
    else:
        tier_label = "<1.70"
        cohort = (item or {}).get("odds_bias", {}).get("low_odds", {})
    decided = int(cohort.get("decided") or 0)
    hits = int(cohort.get("hits") or 0)
    accuracy = cohort.get("accuracy")
    if decided <= 0 or accuracy is None:
        return f"{label}{tier_label} 累積中（0/0）"
    return (
        f"{label}{tier_label} {float(accuracy) * 100:.1f}%"
        f"（{hits}/{decided}）"
    )


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
    """Send only fresh persisted T-30/T-5 condition opportunities.

    Despite the retained argument name for callers on an old deployment, the
    accepted event is now ``{"match_id": ..., "stage": "T-30"|"T-5"}``.
    There is deliberately no recovery scan/backfill over watch history.
    """
    from analysis.granular_conditions import _role, notification_opportunities
    with state_lock(config):
        state, sent = _load(config), 0
        history = read_json(config.state_dir / "prediction_history.json", {})
        history_rows = (
            history.get("rows") or []
            if isinstance(history, dict)
            else history if isinstance(history, list) else []
        )
        history_rows = [row for row in history_rows if isinstance(row, dict)]
        seen_signals = set(state["signals"])
        for item in notification_opportunities(
            history_rows, ledger.get("watch") or {}, fresh_t5_predictions or [],
            system="crown",
        ):
            watch, selected, stage = item["watch"], item["selected"], item["stage"]
            try:
                kickoff = parse_time(watch.get("kickoff_hkt") or watch.get("kickoff"))
                raw_line = float(selected.get("line", selected.get("condition")))
                odds = _finite_positive(selected.get("odds"))
            except (TypeError, ValueError):
                continue
            if kickoff is None or kickoff <= now_hkt() or odds is None or odds <= 1:
                continue
            notification_id = (
                f"crown|{item['fixture']}|{item['market']}|{stage}|granular-v1"
            )
            if notification_id in seen_signals:
                continue
            league = str(watch.get("league") or "").strip()
            if not league:
                continue
            primary, extras = item["matches"][0], item["matches"][1:4]
            total = primary["total"]
            market_label = MARKET_LABELS.get(item["market"], "未分類市場")
            role = _role(item["market"], selected.get("side"), raw_line)
            selected_line = -raw_line if item["market"] == "HDC" and selected.get("side") == "A" else raw_line
            line_text = _quarter_line(selected_line, signed=item["market"] == "HDC")
            title = "預備提示" if stage == "T-30" else "數據提示"
            message = "\n".join([
                f"皇冠 {title}",
                f"開賽：{kickoff.astimezone(HKT).strftime('%d/%m %H:%M')} HKT",
                f"聯賽：{league}",
                f"對賽：{watch.get('home') or ''} vs {watch.get('away') or ''}",
                f"投注：{market_label}",
                f"選擇：{role or '—'}",
                f"盤口：{line_text}",
                f"賠率：{odds:.2f}",
                f"主條件：{_public_condition_text(primary['label'])}",
                f"命中率：{total['accuracy'] * 100:.1f}%（{total['hits']}/{total['decided']}）· {primary['odds_tier']} · {primary['badge']}",
                *[
                    f"＋ {_public_condition_text(extra['label'])}：{extra['total']['accuracy'] * 100:.1f}%（{extra['total']['hits']}/{extra['total']['decided']}）"
                    for extra in extras
                ],
                "只作數據提示，由你自行決定。",
            ])
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
