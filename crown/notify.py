"""Idempotent Crown granular-condition Telegram notifications."""
from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .common import HKT, iso_hkt, now_hkt, parse_time, read_json, write_json_atomic
from .config import Settings
from .state import notification_lock, paths
from analysis.league_display import traditional_chinese_league
from analysis.three_stage_consensus import calculate_three_stage_consensus


ODDS_TIER_THRESHOLD = 1.70
MARKET_LABELS = {"HDC": "讓球", "HIL": "入球大細", "CHL": "角球大細"}
NOTIFICATION_STAGE_MAX_AGE = {
    "T-30": timedelta(minutes=45),
    "T-5": timedelta(minutes=15),
}
STATE_LIMIT = 1600


@dataclass
class _NotificationBudget:
    """One pass-wide transport budget shared by all committed outboxes."""

    max_attempts: int | None
    deadline: float | None
    attempted: int = 0

    @classmethod
    def create(cls, max_attempts: int | None, max_seconds: float | None) -> "_NotificationBudget":
        deadline = (
            time.monotonic() + max(0.0, max_seconds)
            if max_seconds is not None else None
        )
        return cls(max_attempts=max_attempts, deadline=deadline)

    def remaining(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - time.monotonic())

    def can_attempt(self) -> bool:
        if self.max_attempts is not None and self.attempted >= max(0, self.max_attempts):
            return False
        remaining = self.remaining()
        return remaining is None or remaining > 0.0

    def reserve_attempt(self) -> bool:
        if not self.can_attempt():
            return False
        self.attempted += 1
        return True


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
    state.setdefault("wilson_bets", [])
    state.setdefault("wilson_match_alerts", [])
    state.setdefault("hkjc_execution_test_alerts", [])
    return _seed_wilson_match_alerts(state)


def _bounded_unique_ids(values: Any) -> list[str]:
    """Keep the newest bounded acknowledgement IDs, retaining no duplicates."""
    if not isinstance(values, (list, tuple)):
        return []
    kept: list[str] = []
    seen: set[str] = set()
    for value in reversed(values):
        identity = str(value or "").strip()
        if not identity or identity in seen:
            continue
        seen.add(identity)
        kept.append(identity)
        if len(kept) >= STATE_LIMIT:
            break
    kept.reverse()
    return kept


def _seed_wilson_match_alerts(state: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy formal-bet acknowledgements into the Wilson outbox.

    ``wilson_bets`` is the pre-upgrade formal simulated-bet acknowledgement
    list.  It is deliberately never populated from low-odds observations.
    """
    formal_ids = _bounded_unique_ids(state.get("wilson_bets"))
    current_ids = _bounded_unique_ids(state.get("wilson_match_alerts"))
    state["wilson_bets"] = formal_ids
    state["wilson_match_alerts"] = _bounded_unique_ids(formal_ids + current_ids)
    return state


_HKJC_COMPARISON_REASONS = {
    "hkjc_fixture_identity_missing_or_ambiguous": "無同場盤",
    "hkjc_fixture_kickoff_identity_mismatch": "開賽時間不一致",
    # A missing native counterpart records a collection outage/absence; it
    # must never be worded as evidence that HKJC had no same-fixture market.
    "hkjc_exact_native_t5_missing": "系統未取得原生T-5",
    "hkjc_exact_market_side_line_missing_or_ambiguous": "盤口不一致",
    "hkjc_execution_quote_stale_at_t5": "非新鮮T-5",
    "hkjc_execution_odds_invalid_or_missing": "賠率未能確認",
    "hkjc_execution_source_invalid_or_missing": "非原生馬會報價",
    "hkjc_execution_timestamp_missing": "報價時間缺失",
    "hkjc_execution_post_kickoff_or_post_decision": "賽後或決策後報價",
    "hkjc_local_evidence_unavailable": "馬會資料未能讀取",
    "hkjc_local_evidence_invalid": "馬會資料無效",
    "hkjc_local_evidence_too_large": "馬會資料異常",
}


def _hkjc_counterpart(bet: dict[str, Any]) -> tuple[float | None, str]:
    """Read one exact, pre-decision native HKJC T-5 counterpart, locally only."""
    fixture = str(bet.get("hkjc_match_id") or "").strip()
    market = str(bet.get("code") or bet.get("market") or "").strip().upper()
    side = str(bet.get("selected_side") or bet.get("side") or "").strip().upper()
    kickoff = parse_time(bet.get("kickoff"))
    stage_at = parse_time(bet.get("admission_at") or bet.get("created_at"))
    try:
        line = float(bet.get("line", bet.get("selected_line")))
    except (TypeError, ValueError):
        line = None
    if (
        not fixture or market not in MARKET_LABELS or not side or line is None
        or not math.isfinite(line) or kickoff is None or stage_at is None
        or stage_at >= kickoff or str(bet.get("stage") or "") != "T-5"
    ):
        return None, "馬會對照：未能確認（資料不足）"
    try:
        from .hkjc_execution_test import _exact_hkjc_quote
        quote, reason = _exact_hkjc_quote(
            fixture, market, side, line, stage_at, kickoff,
        )
    except Exception:
        quote, reason = None, "hkjc_local_evidence_unavailable"
    if quote is None:
        return None, f"馬會對照：未能確認（{_HKJC_COMPARISON_REASONS.get(reason, '未能確認')}）"
    try:
        odds = float(quote["odds"])
    except (KeyError, TypeError, ValueError):
        return None, "馬會對照：未能確認（賠率未能確認）"
    if not math.isfinite(odds) or odds <= 1:
        return None, "馬會對照：未能確認（賠率未能確認）"
    return odds, f"馬會對照：{odds:.2f}"


def _wilson_message(bet: dict[str, Any]) -> str | None:
    """Traditional-Chinese committed Wilson simulation notification only."""
    if bet.get("portfolio") != "crown_wilson_test" or bet.get("strategy") != "wilson-test-strategy-v1":
        return None
    history = bet.get("frozen_historical_evidence")
    arithmetic = bet.get("wilson_admission")
    if not isinstance(history, dict) or not isinstance(arithmetic, dict):
        return None
    try:
        kickoff = parse_time(bet.get("kickoff"))
        odds = float(bet.get("odds"))
        minimum = float(arithmetic["minimum_acceptable_odds_raw"])
        line = float(bet.get("selected_line"))
        number = int(bet.get("condition_number"))
    except (KeyError, TypeError, ValueError):
        return None
    if kickoff is None or kickoff <= now_hkt() or odds <= 1:
        return None
    market = str(bet.get("market_label") or MARKET_LABELS.get(str(bet.get("market") or bet.get("code") or "").upper()) or "").strip()
    if market not in set(MARKET_LABELS.values()):
        return None
    league = traditional_chinese_league(bet.get("league"))
    if not league:
        return None
    selection = f"{market} · {bet.get('selected_role') or '—'} {line:g}"
    hkjc_odds, hkjc_line = _hkjc_counterpart(bet)
    # The frozen Crown Wilson minimum never changes here.  Crown selected the
    # condition/tier; this read-only counterpart can only choose the better
    # exact execution price for the same condition.
    selected_odds, platform = odds, "皇冠"
    if hkjc_odds is not None and hkjc_odds > selected_odds:
        selected_odds, platform = hkjc_odds, "馬會"
    qualifies = selected_odds + 1e-12 >= minimum
    is_observation = (
        bet.get("portfolio") == "crown_wilson_observations"
        or (bet.get("formal_bet") is False and bet.get("bet_status") == "NO_BET_LOW_ODDS")
    )
    if is_observation and not qualifies:
        decision = "不投注：賠率不足"
    elif not qualifies:
        # A malformed legacy formal row cannot become a recommendation merely
        # because it was previously persisted.
        decision = "不投注：賠率不足"
    else:
        decision = "投注"
    condition_numbers = bet.get("condition_numbers")
    if isinstance(condition_numbers, (list, tuple)):
        numbers = sorted({
            int(value) for value in condition_numbers
            if isinstance(value, int) or str(value).strip().isdigit()
        })
    else:
        numbers = [number]
    if not numbers:
        return None
    condition_line = "、".join(f"皇冠 Wilson 條件 #{value}" for value in numbers)
    minimums = bet.get("minimum_odds_by_condition")
    if isinstance(minimums, (list, tuple)) and minimums:
        minimum_line = "最低賠率要求：" + "；".join(
            f"#{int(number)} {float(value):.2f}"
            for number, value in minimums
        )
    else:
        minimum_line = f"最低賠率要求：{minimum:.2f}"
    return "\n".join([
        "【皇冠 Wilson】",
        f"{kickoff.astimezone(HKT).strftime('%H:%M')} {league}",
        f"{bet.get('home') or ''} vs {bet.get('away') or ''}",
        "",
        f"合符 {condition_line}",
        f"皇冠訊號：{selection} @{odds:.2f}",
        hkjc_line,
        minimum_line,
        f"決定：{decision}",
        f"投注平台：{platform}",
    ])


def _bilateral_decision_message(row: dict[str, Any]) -> str | None:
    """Render only a persisted reciprocal fan-in decision; no ledger reread."""
    try:
        kickoff = parse_time(str(row.get("kickoff") or ""))
        signal = float(row.get("signal_quote"))
        minimum = float(row.get("minimum_odds"))
        line = float(row.get("line"))
        number = int(row.get("condition_number"))
    except (TypeError, ValueError):
        return None
    if kickoff is None or kickoff <= now_hkt():
        return None
    market = MARKET_LABELS.get(str(row.get("market") or "").upper())
    if not market:
        return None
    counterpart = row.get("counterpart_quote")
    comparison = (f"馬會對照：@{float(counterpart):.2f}" if counterpart is not None
                  else f"馬會對照：未能確認（{row.get('counterpart_reason') or '收集不可用'}）")
    decision = {"PAPER_SIMULATION": "模擬投注",
                "NO_BET_LOW_ODDS": "不投注：賠率不足",
                "COUNTERPART_UNAVAILABLE": "對照收集失敗；保留原生訊號決定"}.get(
                    str(row.get("decision")), "不投注")
    platform = {"crown": "皇冠", "hkjc": "馬會"}.get(
        str(row.get("chosen_execution_book") or ""), "—")
    return "\n".join([
        "【皇冠 Wilson】", f"{kickoff.astimezone(HKT).strftime('%H:%M')} bilateral T-5",
        f"合符 皇冠 Wilson 條件 #{number}",
        f"皇冠訊號：{market} · {row.get('side') or ''} {line:g} @{signal:.2f}",
        comparison, f"最低賠率要求：{minimum:.2f}", f"決定：{decision}",
        f"投注平台：{platform}",
    ])


def notify_bilateral_decisions(
    ledger: dict[str, Any], config: Settings, *, max_attempts: int | None = None,
    max_seconds: float | None = None, _budget: _NotificationBudget | None = None,
) -> int:
    """Durable decision outbox; acknowledge only after `_send` confirms ok."""
    budget = _budget or _NotificationBudget.create(max_attempts, max_seconds)
    ns = ledger.get("crown_hkjc_execution_test") or {}
    decisions = {str(row.get("decision_id")): row for row in ns.get("decisions") or []
                 if isinstance(row, dict)}
    with notification_lock(config) as acquired:
        if not acquired:
            return 0
        state = _load(config)
        sent_ids = {str(value) for value in state.get("bilateral_decision_alerts") or []}
        sent = 0
        for outbox in ns.get("decision_outbox") or []:
            if not isinstance(outbox, dict) or not outbox.get("notification_required"):
                continue
            did = str(outbox.get("decision_id") or "")
            if not did or did in sent_ids:
                continue
            message = _bilateral_decision_message(decisions.get(did) or {})
            if message is None or not budget.reserve_attempt():
                continue
            if _send(config, message, max_seconds=budget.remaining()) is False:
                continue
            state["bilateral_decision_alerts"] = _bounded_unique_ids(
                list(state.get("bilateral_decision_alerts") or []) + [did]
            )
            state["updated_at"] = iso_hkt()
            write_json_atomic(paths(config)["notify"], state)
            sent_ids.add(did); sent += 1
    return sent


def _wilson_observation_message(row: dict[str, Any]) -> str | None:
    if row.get("portfolio") != "crown_wilson_observations" or row.get("bet_status") != "NO_BET_LOW_ODDS":
        return None
    clone = dict(row)
    clone["portfolio"] = "crown_wilson_test"
    clone["strategy"] = "wilson-test-strategy-v1"
    return _wilson_message(clone)


def _observation_group_key(row: dict[str, Any]) -> tuple[str, ...] | None:
    """Return the one native T-5 signal identity shared by its condition rows.

    Matching conditions have independent frozen evidence and therefore
    independent observation IDs.  Their selected native quote is nevertheless
    one atomic alert opportunity; grouping it prevents a per-row loop from
    dropping #9 when the bounded tick has only one Telegram attempt.
    """
    if (
        row.get("portfolio") != "crown_wilson_observations"
        or row.get("bet_status") != "NO_BET_LOW_ODDS"
    ):
        return None
    try:
        line = float(row.get("selected_line", row.get("line")))
        odds = float(row.get("odds"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(line) or not math.isfinite(odds):
        return None
    values = (
        row.get("match_id"),
        row.get("code", row.get("market")),
        row.get("selected_side", row.get("side")),
        row.get("stage"),
        row.get("kickoff"),
        row.get("created_at"),
        row.get("hkjc_match_id"),
    )
    if not all(str(value or "").strip() for value in values[:-1]):
        return None
    return tuple(str(value).strip() for value in values) + (f"{line:.8f}", f"{odds:.8f}")


def _observation_groups(
    rows: list[dict[str, Any]], sent_ids: set[str],
) -> list[list[dict[str, Any]]]:
    """Group unacknowledged low-odds rows by exact native T-5 selection."""
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        ident = str(row.get("observation_id") or "")
        key = _observation_group_key(row)
        if not ident or ident in sent_ids or key is None:
            continue
        groups.setdefault(key, []).append(row)
    return list(groups.values())


def _wilson_observation_group_message(rows: list[dict[str, Any]]) -> str | None:
    """Render one concise no-bet alert for all exact matching conditions."""
    if not rows:
        return None
    base = dict(rows[0])
    if any(_observation_group_key(row) != _observation_group_key(base) for row in rows):
        return None
    pairs: list[tuple[int, float]] = []
    for row in rows:
        arithmetic = row.get("wilson_admission")
        try:
            number = int(row.get("condition_number"))
            minimum = float((arithmetic or {})["minimum_acceptable_odds_raw"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(minimum) or minimum <= 1:
            return None
        pairs.append((number, minimum))
    pairs.sort()
    base["condition_number"] = pairs[0][0]
    base["condition_numbers"] = [number for number, _ in pairs]
    base["minimum_odds_by_condition"] = pairs
    base["wilson_admission"] = dict(base.get("wilson_admission") or {})
    base["wilson_admission"]["minimum_acceptable_odds_raw"] = min(
        minimum for _, minimum in pairs
    )
    return _wilson_observation_message(base)


def notify_wilson_pending(
    ledger: dict[str, Any],
    config: Settings,
    *,
    max_attempts: int | None = None,
    max_seconds: float | None = None,
    _budget: _NotificationBudget | None = None,
) -> int:
    """Durable retryable Crown Wilson outbox; old strategy entries are excluded."""
    budget = _budget or _NotificationBudget.create(max_attempts, max_seconds)
    with notification_lock(config) as acquired:
        if not acquired:
            return 0
        state = _seed_wilson_match_alerts(_load(config))
        sent_ids = {str(value) for value in state.get("wilson_match_alerts") or []}
        sent = attempted = 0
        formal_rows = list(ledger.get("bets") or [])
        observation_rows = list(
            (ledger.get("wilson_validation") or {}).get("observations") or []
        )
        for bet in formal_rows:
            if not isinstance(bet, dict):
                continue
            bid = str(bet.get("bet_id") or "")
            if not bid or bid in sent_ids or bet.get("status") != "PENDING":
                continue
            try:
                from analysis.bilateral_decision import decision_for_bet
                if decision_for_bet(
                    ledger.get("crown_hkjc_execution_test") or {}, bet, "crown"
                ):
                    continue
            except Exception:
                continue
            message = _wilson_message(bet)
            if message is None:
                continue
            if not budget.reserve_attempt():
                break
            remaining = budget.remaining()
            attempted += 1
            if _send(config, message, max_seconds=remaining) is False:
                continue
            state["wilson_match_alerts"] = _bounded_unique_ids(
                list(state.get("wilson_match_alerts") or []) + [bid]
            )
            if bet.get("bet_id"):
                state["wilson_bets"] = _bounded_unique_ids(
                    list(state.get("wilson_bets") or []) + [bid]
                )
            sent_ids.add(bid)
            sent += 1
            state["updated_at"] = iso_hkt()
            write_json_atomic(paths(config)["notify"], state)
        for group in _observation_groups(
            [row for row in observation_rows if isinstance(row, dict)], sent_ids,
        ):
            ids = [str(row.get("observation_id") or "") for row in group]
            message = _wilson_observation_group_message(group)
            if not all(ids) or message is None:
                continue
            if not budget.reserve_attempt():
                break
            remaining = budget.remaining()
            attempted += 1
            if _send(config, message, max_seconds=remaining) is False:
                continue
            state["wilson_match_alerts"] = _bounded_unique_ids(
                list(state.get("wilson_match_alerts") or []) + ids
            )
            sent_ids.update(ids)
            sent += 1
            state["updated_at"] = iso_hkt()
            write_json_atomic(paths(config)["notify"], state)
        state["updated_at"] = iso_hkt()
        write_json_atomic(paths(config)["notify"], state)
        return sent


def _hkjc_execution_message(bet: dict[str, Any]) -> str | None:
    """Traditional-Chinese committed 皇冠×馬會 simulation notification only."""
    if (
        bet.get("portfolio") != "crown_hkjc_execution_test"
        or bet.get("strategy") != "crown-hkjc-execution-test-v1"
        or bet.get("status") != "PENDING"
        or not bet.get("simulation_only")
        or bet.get("real_betting_enabled") is not False
    ):
        return None
    arithmetic = bet.get("wilson_admission") if isinstance(bet.get("wilson_admission"), dict) else {}
    try:
        kickoff = parse_time(bet.get("kickoff"))
        decision_at = parse_time(bet.get("decision_at") or bet.get("created_at"))
        crown_observed = parse_time(bet.get("crown_signal_observed_at"))
        hkjc_observed = parse_time(bet.get("hkjc_execution_observed_at"))
        crown_odds = float(bet.get("crown_signal_odds"))
        hkjc_odds = float(bet.get("hkjc_execution_odds"))
        minimum = float(arithmetic["minimum_acceptable_odds_raw"])
        line, number = float(bet.get("selected_line")), int(bet.get("condition_number"))
    except (KeyError, TypeError, ValueError):
        return None
    market = str(bet.get("market_label") or "").strip()
    league = traditional_chinese_league(bet.get("league"))
    crown_source = str(bet.get("crown_signal_source") or "").strip().lower()
    hkjc_source = str(bet.get("hkjc_execution_source") or "").strip().lower()
    if (
        kickoff is None or kickoff <= now_hkt() or decision_at is None or crown_observed is None or hkjc_observed is None
        or decision_at >= kickoff
        or (now_hkt() - decision_at).total_seconds() > NOTIFICATION_STAGE_MAX_AGE["T-5"].total_seconds()
        or crown_observed >= kickoff or hkjc_observed >= kickoff
        or crown_observed > decision_at or hkjc_observed > decision_at
        or crown_odds <= 1 or hkjc_odds + 1e-12 < minimum
        or crown_source != "titan007-crown-id-3"
        or hkjc_source not in {"hkjc_public_board", "hkjc-current-board"}
        or not league or market not in set(MARKET_LABELS.values())
    ):
        return None
    selection = f"{market} · {bet.get('selected_role') or '—'} {line:g}"
    platform = "皇冠" if crown_odds > hkjc_odds else "馬會"
    return "\n".join([
        "【皇冠×馬會執行測試倉（模擬）】",
        f"{kickoff.astimezone(HKT).strftime('%H:%M')} {league}",
        f"{bet.get('home') or ''} vs {bet.get('away') or ''}",
        "",
        f"合符 皇冠 Wilson 條件 #{number}",
        f"投注：{selection}",
        f"投注平台：{platform}",
        "",
        f"皇冠訊號賠率：{crown_odds:.2f}",
        f"馬會執行賠率：{hkjc_odds:.2f}",
        f"最低賠率要求：{minimum:.2f}",
    ])


def notify_hkjc_execution_pending(
    ledger: dict[str, Any], config: Settings, *, max_attempts: int | None = None,
    max_seconds: float | None = None,
    _budget: _NotificationBudget | None = None,
) -> int:
    """Durable retry outbox for committed reciprocal rows; no rejections."""
    budget = _budget or _NotificationBudget.create(max_attempts, max_seconds)
    with notification_lock(config) as acquired:
        if not acquired:
            return 0
        state = _load(config)
        sent_ids = set(map(str, state.get("hkjc_execution_test_alerts") or []))
        namespace = ledger.get("crown_hkjc_execution_test") if isinstance(ledger, dict) else {}
        rows = namespace.get("bets") if isinstance(namespace, dict) else []
        sent = attempted = 0
        for bet in rows or []:
            bid = str(bet.get("bet_id") or "") if isinstance(bet, dict) else ""
            if not bid or bid in sent_ids:
                continue
            if bet.get("bilateral_decision_id"):
                continue
            message = _hkjc_execution_message(bet)
            if message is None:
                continue
            if not budget.reserve_attempt():
                break
            remaining = budget.remaining()
            attempted += 1
            if _send(config, message, max_seconds=remaining) is False:
                continue
            state["hkjc_execution_test_alerts"] = _bounded_unique_ids(
                list(state.get("hkjc_execution_test_alerts") or []) + [bid]
            )
            state["updated_at"] = iso_hkt()
            write_json_atomic(paths(config)["notify"], state)
            sent_ids.add(bid); sent += 1
        return sent


def _send(
    config: Settings, text: str, *, max_seconds: float | None = None,
    return_response: bool = False,
) -> bool | dict[str, Any]:
    if not (config.telegram_enabled and config.telegram_bot_token and config.telegram_chat_id):
        return False
    body = json.dumps({"chat_id": config.telegram_chat_id, "text": text}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
    # Notification transport is retryable on the next tick.  Keep a failed
    # Telegram call well below the prediction service deadline.
    timeout = 5.0 if max_seconds is None else min(5.0, max(0.1, max_seconds))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        result = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Telegram 直接發送失敗:{type(exc).__name__}") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        description = result.get("description", "unknown") if isinstance(result, dict) else "invalid_response"
        raise RuntimeError(f"Telegram 直接發送失敗:{description}")
    if return_response:
        message = result.get("result")
        return {
            "transport": "telegram_bot_api",
            "ok": True,
            "message_id": message.get("message_id") if isinstance(message, dict) else None,
        }
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


def _native_recent_condition_events(
    ledger: dict[str, Any], fresh_events: list[Any] | None,
) -> list[dict[str, str]]:
    """Return bounded native stage events for delivery or transport recovery.

    A stage is eligible only when its own persisted timestamp is auditable,
    pre-kickoff, and still recent enough to be actionable.  Recovery is
    intentionally limited to the same currently upcoming window as delivery:
    this is not a history/backfill scan.  The caller's durable signal key
    remains the acknowledgement authority.
    """
    watches = ledger.get("watch") or {}
    if not isinstance(watches, dict):
        return []
    now = now_hkt()
    requested: list[tuple[str, str]] = []
    for item in fresh_events or []:
        if not isinstance(item, dict):
            continue
        requested.append((
            str(item.get("match_id") or "").strip(),
            str(item.get("stage") or "").strip(),
        ))
    # A timeout leaves no acknowledgement.  Reconsider only the short,
    # native T-30/T-5 action window on later ticks.
    requested.extend(
        (str(match_id).strip(), stage_name)
        for match_id in watches
        for stage_name in NOTIFICATION_STAGE_MAX_AGE
    )

    output: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match_id, stage_name in requested:
        event_key = (match_id, stage_name)
        if event_key in seen or stage_name not in NOTIFICATION_STAGE_MAX_AGE:
            continue
        seen.add(event_key)
        watch = watches.get(match_id)
        if not isinstance(watch, dict):
            continue
        kickoff = parse_time(str(watch.get("kickoff_hkt") or watch.get("kickoff") or ""))
        if kickoff is None or kickoff <= now:
            continue
        rows = [
            row for row in (watch.get("stages") or [])
            if isinstance(row, dict) and row.get("stage") == stage_name
        ]
        if len(rows) != 1:
            continue
        stage = rows[0]
        stage_kickoff = parse_time(str(stage.get("kickoff_hkt") or ""))
        stage_at = parse_time(str(stage.get("ts") or ""))
        if (
            stage.get("post_hoc_backfill")
            or stage.get("exclude_from_telegram")
            or str(stage.get("match_id") or "").strip() != match_id
            or stage_kickoff is None
            or stage_kickoff != kickoff
            or stage_at is None
            or stage_at > now
            or stage_at >= kickoff
            or now - stage_at > NOTIFICATION_STAGE_MAX_AGE[stage_name]
        ):
            continue
        selected = [
            row for row in (stage.get("market_predictions") or [])
            if isinstance(row, dict)
            and str(row.get("code") or "") in MARKET_LABELS
            and _finite_positive(row.get("odds")) is not None
            and _finite_positive(row.get("odds")) > 1
        ]
        if not selected:
            continue
        output.append({"match_id": match_id, "stage": stage_name})
    return output


def notify_new(
    ledger: dict[str, Any],
    config: Settings,
    fresh_t5_predictions: list[Any] | None = None,
    *,
    max_attempts: int | None = None,
    max_seconds: float | None = None,
) -> int:
    """Send committed Wilson simulations exactly once.

    ``fresh_t5_predictions`` supplies just-persisted events.  An
    unacknowledged notification may also be retried from the same live watch
    data after a transport failure, but only before kickoff and inside the
    short per-stage action window.  Historical, recovered, post-hoc, malformed
    and stale rows fail closed.
    """
    # v1 granular candidate alerts were retired at the immutable Wilson
    # cutover.  `fresh_t5_predictions` stays accepted for API compatibility
    # but notification eligibility is the committed Wilson bet itself.
    del fresh_t5_predictions
    # A tick grants one bounded Telegram opportunity, not one opportunity per
    # outbox.  Wilson retains priority; the reciprocal row remains durable for
    # the next pass if Wilson consumed the shared attempt or wall-clock budget.
    budget = _NotificationBudget.create(max_attempts, max_seconds)
    delivered = notify_wilson_pending(
        ledger,
        config,
        max_attempts=max_attempts,
        max_seconds=max_seconds,
        _budget=budget,
    )
    delivered += notify_bilateral_decisions(
        ledger, config, max_attempts=max_attempts, max_seconds=max_seconds,
        _budget=budget,
    )
    return delivered + notify_hkjc_execution_pending(
        ledger, config, max_attempts=max_attempts, max_seconds=max_seconds,
        _budget=budget,
    )

    from analysis.granular_conditions import _role, notification_opportunities
    with notification_lock(config) as acquired:
        if not acquired:
            return 0
        state, sent = _load(config), 0
        attempted = 0
        history = read_json(config.state_dir / "prediction_history.json", {})
        history_rows = (
            history.get("rows") or []
            if isinstance(history, dict)
            else history if isinstance(history, list) else []
        )
        history_rows = [row for row in history_rows if isinstance(row, dict)]
        # Tick is deadline-bound.  A full granular mine belongs to sweep/settle
        # history maintenance, not to the post-commit Telegram transport
        # path.  An absent/malformed cache therefore fails closed rather than
        # rebuilding statistics or touching result providers here.
        cached_ranking = (
            ((history.get("stats") or {}).get("granular_conditions") or {})
            .get("ranking")
            if isinstance(history, dict)
            else None
        )
        cached_ranking = (
            cached_ranking if isinstance(cached_ranking, list) else []
        )
        seen_signals = set(state["signals"])
        eligible_events = _native_recent_condition_events(
            ledger, fresh_t5_predictions,
        )
        for item in notification_opportunities(
            history_rows, ledger.get("watch") or {}, eligible_events,
            system="crown", ranking=cached_ranking,
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
            if max_attempts is not None and attempted >= max(0, max_attempts):
                break
            league = str(watch.get("league") or "").strip()
            if not league:
                continue
            primary, extras = item["matches"][0], item["matches"][1:4]
            total = primary["total"]
            market_label = MARKET_LABELS.get(item["market"], "未分類市場")
            role = _role(item["market"], selected.get("side"), raw_line)
            selected_line = -raw_line if item["market"] == "HDC" and selected.get("side") == "A" else raw_line
            line_text = _quarter_line(selected_line, signed=item["market"] == "HDC")
            # Granular rankings are discovery candidates, never a formal
            # recommendation.  A frozen cohort can be promoted only by the
            # independent validation ledger, not by this notification path.
            title = "候選條件，獨立驗證中"
            message = "\n".join([
                f"皇冠 · {title}",
                f"開賽：{kickoff.astimezone(HKT).strftime('%d/%m %H:%M')} HKT",
                f"聯賽：{league}",
                f"對賽：{watch.get('home') or ''} vs {watch.get('away') or ''}",
                f"投注：{market_label}",
                f"選擇：{role or '—'}",
                f"盤口：{line_text}",
                f"賠率：{odds:.2f}",
                f"主條件：{_public_condition_text(primary['label'])}",
                f"凍結前歷史發現率：{total['accuracy'] * 100:.1f}%（{total['hits']}/{total['decided']}）· {primary['odds_tier']}",
                "獨立驗證率：尚未建立／不構成正式推介",
                *[
                    f"＋ {_public_condition_text(extra['label'])}：{extra['total']['accuracy'] * 100:.1f}%（{extra['total']['hits']}/{extra['total']['decided']}）"
                    for extra in extras
                ],
                "候選條件，獨立驗證中；未達已驗證，不構成正式推介。",
            ])
            attempted += 1
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
