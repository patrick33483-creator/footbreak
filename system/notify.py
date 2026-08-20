"""足破 · Telegram 通知系統。

此模組只會在新建立的 Footbreak 條件模擬注後發送通知。舊有模型候選、
階段提示、健康、排程、掃描完成及結算通知均已停用；獨立 Odds Radar
通知流程不受此模組影響。
"""
import datetime as dt
import html
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

try:
    import model as _M
    _KNEE = _M.ODDS_CAP_KNEE
except Exception:
    _KNEE = 0.80

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from analysis.league_display import traditional_chinese_league

LEDGER = os.path.join(HERE, "sim_ledger.json")
STATE = os.path.join(HERE, "notify_state.json")
ACCURACY_HISTORY = os.path.join(HERE, "accuracy_history.json")
DASHBOARD_DATA = os.path.join(os.path.dirname(HERE), "hkjc-dashboard", "data.json")

HKT = dt.timezone(dt.timedelta(hours=8))

CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SOURCE_ID = "telegram_bot_api__pipedream"
TOOL = "telegram_bot_api-send-text-message-or-reply"

DEFAULT_WINDOW_MIN = 45.0          # 只通知近期建立嘅注單,避免補發舊注
MARKET_LABELS = {"HDC": "讓球", "HIL": "入球大細", "CHL": "角球大細"}
CONDITION_PORTFOLIO = "footbreak_wilson_test"
CONDITION_STRATEGY = "wilson-test-strategy-v1"
CROWN_EXECUTION_PORTFOLIO = "footbreak_crown_execution_test"
CROWN_EXECUTION_STRATEGY = "footbreak-crown-execution-test-v1"
STATE_LIMIT = 1600

SIDE_TXT = {"H": "主", "A": "客", "D": "和"}
RESULT_TXT = {"Won": "贏 ✅", "Lost": "輸 ❌", "Refunded": "走水 ➖",
              "Half Won": "半贏 ✅", "Half Lost": "半輸 ❌"}


def _public_condition_text(value):
    """Keep internal codes and legacy abstract paths out of public copy."""
    text = str(value or "")
    for code, label in MARKET_LABELS.items():
        text = re.sub(rf"\b{code}\b", label, text)
    # New descriptors preserve observed Chinese roles (for example
    # 主讓→客受讓→主讓).  An old A/B/C-only artifact has lost that semantic
    # mapping, so never invent one: state the historical-label limitation
    # without leaking unexplained tokens.
    return re.sub(r"\b[ABC](?:→[ABC])+\b", "方向曾變化（舊紀錄未保留角色）", text)


# ─────────────────────────── 狀態 ───────────────────────────
def _bounded_unique_ids(values):
    """Return the newest bounded, non-empty string IDs without replays.

    Notification state is durable across upgrades.  Iterate backwards so an
    ID retained in the newer Wilson outbox keeps its newest position when a
    legacy formal-bet list is merged into it.
    """
    if not isinstance(values, (list, tuple)):
        return []
    kept, seen = [], set()
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


def _seed_wilson_match_alerts(state):
    """Preserve formal Wilson acknowledgements when the shared outbox appears.

    ``condition_simulation_bets`` predates ``wilson_match_alerts`` and contains
    only formal simulated-bet acknowledgements.  Seed the new shared outbox
    from that list, but never put observations back into the legacy formal key.
    """
    formal_ids = _bounded_unique_ids(state.get("condition_simulation_bets"))
    current_ids = _bounded_unique_ids(state.get("wilson_match_alerts"))
    state["condition_simulation_bets"] = formal_ids
    state["wilson_match_alerts"] = _bounded_unique_ids(formal_ids + current_ids)
    return state


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                s = json.load(f)
            s.setdefault("bets", [])
            s.setdefault("settled", [])
            s.setdefault("queue", [])
            s.setdefault("sweeps", [])
            s.setdefault("watch", [])
            s.setdefault("reviews", [])
            s.setdefault("signals", [])
            s.setdefault("granular_conditions", [])
            s.setdefault("condition_simulation_bets", [])
            s.setdefault("wilson_match_alerts", [])
            s.setdefault("crown_execution_test_alerts", [])
            for key, value in list(s.items()):
                if isinstance(value, list):
                    s[key] = value[-STATE_LIMIT:]
            return _seed_wilson_match_alerts(s)
        except Exception:
            pass
    return {
        "bets": [], "settled": [], "queue": [], "sweeps": [], "watch": [],
        "reviews": [], "signals": [], "granular_conditions": [],
        "condition_simulation_bets": [], "wilson_match_alerts": [],
        "crown_execution_test_alerts": [],
    }


def save_state(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def _finite_positive(value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) and numeric > 0 else None


def _exact_numeric_line(value):
    """Validate a stored total line without silently normalizing a bad value."""
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        parts = [float(part.strip()) for part in text.split("/")]
    except ValueError:
        return None
    if not parts or any(not math.isfinite(part) for part in parts):
        return None
    return text


def _chl_t5_history(side, selected_odds):
    """Return the matching T-5 CHL side/odds-tier hit-rate, fail closed."""
    selected_odds = _finite_positive(selected_odds)
    if side not in {"H", "L"} or selected_odds is None or selected_odds <= 1.0:
        return None
    selected_high = selected_odds >= 1.70
    try:
        with open(ACCURACY_HISTORY, encoding="utf-8") as handle:
            matches = json.load(handle).get("matches") or []
    except (OSError, ValueError, TypeError, AttributeError):
        return None

    hits = decided = 0
    for match in matches:
        if not isinstance(match, dict):
            continue
        for stage in match.get("stages") or []:
            if not isinstance(stage, dict) or stage.get("stage") != "T-5":
                continue
            for grade in stage.get("market_grades") or []:
                if not (
                    isinstance(grade, dict)
                    and grade.get("grade_status") == "GRADED"
                    and grade.get("code") == "CHL"
                    and str(grade.get("side") or grade.get("selection") or "").upper() == side
                    and grade.get("hit") is not None
                ):
                    continue
                odds = _finite_positive(grade.get("odds"))
                if odds is None or odds <= 1.0 or (odds >= 1.70) != selected_high:
                    continue
                decided += 1
                hits += grade.get("hit") is True
    return {"hits": hits, "decided": decided}


def _fresh_t5_stage(ledger, item):
    """Resolve an exact T-5 identity only while its fixture is still upcoming."""
    mid = str(item.get("match_id") or "") if isinstance(item, dict) else str(item or "")
    watch = (ledger.get("watch") or {}).get(mid)
    if not isinstance(watch, dict):
        return None
    t5 = [stage for stage in (watch.get("stages") or [])
          if isinstance(stage, dict) and stage.get("stage") == "T-5"]
    if len(t5) != 1:
        return None
    stage = t5[0]
    kickoff_text = str(watch.get("kickoff") or "")
    if not (
        mid
        and str(watch.get("match_id") or "") == mid
        and kickoff_text
        and stage.get("home") == watch.get("home")
        and stage.get("away") == watch.get("away")
    ):
        return None
    try:
        kickoff = dt.datetime.fromisoformat(kickoff_text)
    except ValueError:
        return None
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=HKT)
    if kickoff <= dt.datetime.now(HKT):
        return None
    return mid, watch, stage, kickoff


def _footbreak_chl_event(ledger, item):
    resolved = _fresh_t5_stage(ledger, item)
    if resolved is None:
        return None
    mid, watch, stage, kickoff = resolved
    rows = [row for row in (stage.get("market_predictions") or [])
            if isinstance(row, dict) and row.get("code") == "CHL"]
    # A snapshot must contain one unambiguous selected direction.  In
    # particular, never fall back to the opposite side's price.
    if len(rows) != 1:
        return None
    selected = rows[0]
    side = selected.get("side")
    if side not in {"H", "L"}:
        return None
    line = _exact_numeric_line(selected.get("line", selected.get("condition")))
    odds = _finite_positive(selected.get("odds"))
    if (
        line is None
        or odds is None
        or odds <= 1.0
        or selected.get("odds_status") not in {None, "available"}
    ):
        return None
    observed_text = str(selected.get("observed_at") or "").strip()
    try:
        observed_at = dt.datetime.fromisoformat(observed_text)
    except ValueError:
        return None
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=HKT)
    if observed_at >= kickoff:
        return None
    identity = "|".join((
        mid, kickoff.isoformat(), str(watch.get("home") or ""),
        str(watch.get("away") or ""),
    ))
    key = f"footbreak|{identity}|T-5|CHL|corner-v1|{side}|{line}"
    league = str(watch.get("league") or "").strip()
    selection = "角球大" if side == "H" else "角球細"
    odds_tier = "≥1.70" if odds >= 1.70 else "<1.70"
    history = _chl_t5_history(side, odds)
    if history and history["decided"]:
        history_text = (
            f"{selection} {odds_tier}："
            f"{100.0 * history['hits'] / history['decided']:.1f}%"
            f"（{history['hits']}/{history['decided']}）"
        )
    else:
        history_text = f"{selection} {odds_tier}：待累積（0/0）"
    text = "\n".join([
        "足破 T-5 角球預測",
        f"開賽：{kickoff.strftime('%d/%m %H:%M')} HKT" + (f" · {esc(league)}" if league else ""),
        f"對賽：{esc(watch.get('home') or '')} vs {esc(watch.get('away') or '')}",
        "市場：角球大細",
        f"選擇：{selection}",
        f"盤口：{esc(line)}",
        f"當刻賠率：{odds:.3f}（{odds_tier}）",
        f"歷史命中率：{history_text}",
        f"賠率觀測：{observed_at.astimezone(HKT).strftime('%d/%m %H:%M:%S')} HKT",
        "觸發：新保存的 T-5 有完整角球方向、盤口及當刻賠率。",
        "只作預測通知，是否投注由你決定。",
    ])
    return key, text


def notify_fresh_t5_signals(ledger, fresh_t5):
    """Retired legacy entry point; granular notifications replace CHL-only alerts."""
    return 0


def _granular_history_rows():
    """Read the generated immutable dashboard history; never rebuild history here."""
    try:
        with open(DASHBOARD_DATA, encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = (payload.get("prediction_history") or {}).get("rows") or []
        return [row for row in rows if isinstance(row, dict)]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def _granular_bet(selected, watch):
    code, side = selected.get("code"), selected.get("side")
    line = _exact_numeric_line(selected.get("line", selected.get("condition"))) or "—"
    odds = _finite_positive(selected.get("odds"))
    if odds is None or odds <= 1:
        return None
    if code == "HDC":
        team = watch.get("home") if side == "H" else watch.get("away")
        try:
            shown = float(line) * (-1 if side == "A" else 1)
            line = f"{shown:g}"
        except ValueError:
            pass
        return f"讓球 {team} {line} @{odds:.2f}"
    if code == "HIL":
        return f"入球大細 {'大' if side == 'H' else '細'} {line} @{odds:.2f}"
    if code == "CHL":
        return f"角球大細 {'大' if side == 'H' else '細'} {line} @{odds:.2f}"
    return None


def notify_fresh_granular_conditions(ledger, fresh_events):
    """Send the legacy, non-bet candidate notice in conservative Chinese.

    It is deliberately distinct from committed validation-bet notifications:
    the wording never presents a historical candidate as a formal bet.
    """
    # Retired at the Wilson cutover.  Keeping this inert compatibility entry
    # point ensures callers cannot accidentally resume pre-cutover candidate
    # notifications; only committed Wilson simulation bets may be sent.
    del ledger, fresh_events
    return 0

    from analysis.granular_conditions import _role, notification_opportunities
    history = _granular_history_rows()
    opportunities = notification_opportunities(
        history, ledger.get("watch") or {}, fresh_events or [], system="footbreak"
    )
    state = load_state()
    sent_keys = set(state.get("granular_conditions") or [])
    sent = 0
    for item in opportunities:
        watch, selected, stage = item["watch"], item["selected"], item["stage"]
        bet = _granular_bet(selected, watch)
        if not bet:
            continue
        kickoff = None
        try:
            kickoff = dt.datetime.fromisoformat(str(watch.get("kickoff") or ""))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=HKT)
        except ValueError:
            continue
        if kickoff <= dt.datetime.now(HKT):
            continue
        key = f"footbreak|{item['fixture']}|{item['market']}|{stage}|granular-v1"
        if key in sent_keys:
            continue
        league = str(watch.get("league") or "").strip()
        if not league:
            continue
        primary, extra = item["matches"][0], item["matches"][1:4]
        try:
            raw_line = float(selected.get("line", selected.get("condition")))
        except (TypeError, ValueError):
            continue
        role = _role(item["market"], selected.get("side"), raw_line)
        market_label = MARKET_LABELS.get(item["market"], "未分類市場")
        selected_line = -raw_line if item["market"] == "HDC" and selected.get("side") == "A" else raw_line
        selected_line_text = f"{selected_line:g}"
        summary = primary["total"]
        more = [
            "＋ %s：%.1f%%（%s/%s）" % (
                esc(_public_condition_text(match["label"])),
                100 * match["total"]["accuracy"],
                match["total"]["hits"],
                match["total"]["decided"],
            )
            for match in extra
        ]
        title = "候選條件，獨立驗證中"
        text = "\n".join([
            f"足破 · {title}",
            f"開賽：{kickoff.astimezone(HKT).strftime('%d/%m %H:%M')} HKT",
            f"聯賽：{esc(league)}",
            f"對賽：{esc(watch.get('home') or '')} vs {esc(watch.get('away') or '')}",
            f"投注：{esc(market_label)}",
            f"選擇：{esc(role or '—')}",
            f"盤口：{esc(selected_line_text)}",
            f"賠率：{float(selected['odds']):.2f}",
            f"主條件：{esc(_public_condition_text(primary['label']))}",
            f"凍結前歷史發現率：{100 * summary['accuracy']:.1f}%（{summary['hits']}/{summary['decided']}）· {esc(primary['odds_tier'])}",
            "獨立驗證率：尚未建立／不構成正式推介",
            *more,
            "候選條件，獨立驗證中；未達已驗證，不構成正式推介。",
        ])
        send(text)
        sent_keys.add(key)
        state["granular_conditions"] = (
            list(state.get("granular_conditions") or []) + [key]
        )[-1600:]
        state["last_sent"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
        save_state(state)
        sent += 1
    return sent


def _future_kickoff(value):
    try:
        kickoff = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=HKT)
    return kickoff if kickoff > dt.datetime.now(HKT) else None


def _condition_prospective(ledger, bet):
    """Read the frozen cohort's isolated prospective metrics without mutation."""
    namespace = ledger.get("wilson_validation") if isinstance(ledger, dict) else {}
    conditions = namespace.get("conditions") if isinstance(namespace, dict) else {}
    frozen = conditions.get(str(bet.get("frozen_condition_signature") or "")) if isinstance(conditions, dict) else {}
    prospective = frozen.get("prospective") if isinstance(frozen, dict) else {}
    return prospective if isinstance(prospective, dict) else {}


def _condition_bet_message(bet, prospective=None):
    """Return one safe Traditional-Chinese condition-bet message or ``None``."""
    observation = bet.get("portfolio") == "footbreak_wilson_observations"
    if (bet.get("portfolio") != CONDITION_PORTFOLIO or bet.get("strategy") != CONDITION_STRATEGY) and not observation:
        return None
    arithmetic = bet.get("wilson_admission") if isinstance(bet.get("wilson_admission"), dict) else {}
    league = traditional_chinese_league(bet.get("league"))
    home, away = str(bet.get("home") or "").strip(), str(bet.get("away") or "").strip()
    kickoff = _future_kickoff(bet.get("kickoff"))
    market = str(bet.get("market_label") or "").strip()
    direction = str(bet.get("selected_role") or "").strip()
    odds = _finite_positive(bet.get("odds"))
    try:
        line = float(bet.get("selected_line"))
        minimum = float(arithmetic.get("minimum_acceptable_odds_raw"))
    except (TypeError, ValueError):
        return None
    if (
        not league or not home or not away or kickoff is None or market not in set(MARKET_LABELS.values())
        or not direction or odds is None or odds <= 1 or not math.isfinite(line)
    ):
        return None
    number = bet.get("condition_number")
    try:
        number = int(number)
    except (TypeError, ValueError):
        return None
    return "\n".join([
        "【足破 Wilson】",
        f"{kickoff.astimezone(HKT).strftime('%H:%M')} {esc(league)}",
        f"{esc(home)} vs {esc(away)}",
        f"合符條件 #{number}",
        f"{'不投注（賠率不足）' if observation else '投注'} {esc(market)} · {esc(direction)} {line:g}{'' if observation else '（模擬）'}",
        f"現時賠率：{odds:.2f} · 最低賠率要求：{minimum:.2f}",
    ])


def _condition_observation_message(row):
    """Concise no-bet alert for an exact Wilson match below the raw minimum."""
    if row.get("portfolio") != "footbreak_wilson_observations" or row.get("bet_status") != "NO_BET_LOW_ODDS":
        return None
    return _condition_bet_message(row)


def notify_pending_condition_bets(ledger, bet_ids=None, *, max_attempts=8):
    """Send committed condition bets that have not yet been acknowledged.

    ``bet_ids`` narrows the first attempt to newly-created bets.  Passing
    ``None`` scans every still-upcoming active condition bet, which forms a
    durable retry outbox: persistence and Telegram transport are deliberately
    separate, and a transient transport failure must not make the alert vanish
    just because the T-5 snapshot is idempotent on the next tick.

    Missing or malformed public fixture information continues to fail closed.
    """
    requested = (
        {str(value) for value in bet_ids or [] if value}
        if bet_ids is not None else None
    )
    if requested is not None and not requested:
        return 0
    state = _seed_wilson_match_alerts(load_state())
    sent_ids = set(map(str, state.get("wilson_match_alerts") or []))
    sent_order = [str(value) for value in state.get("wilson_match_alerts") or []]
    rows = list(ledger.get("bets") or []) + list(
        (ledger.get("wilson_validation") or {}).get("observations") or []
    )
    sent = attempted = 0
    for bet in rows:
        if not isinstance(bet, dict):
            continue
        bid = str(bet.get("bet_id") or bet.get("observation_id") or "")
        if not bid or (requested is not None and bid not in requested) or bid in sent_ids:
            continue
        text = (_condition_bet_message(bet, _condition_prospective(ledger, bet))
                if bet.get("bet_id") else _condition_observation_message(bet))
        if text is None:
            continue
        if attempted >= max(0, max_attempts):
            break
        attempted += 1
        send(text)
        sent_ids.add(bid)
        sent_order.append(bid)
        state["wilson_match_alerts"] = _bounded_unique_ids(sent_order)
        # Preserve compatibility for operational views that count formal bets.
        if bet.get("bet_id"):
            state["condition_simulation_bets"] = _bounded_unique_ids(
                list(state.get("condition_simulation_bets") or []) + [bid]
            )
        state["last_sent"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
        save_state(state)
        sent += 1
    return sent


def notify_new_condition_bets(ledger, bet_ids):
    """Compatibility wrapper for the immediate first post-commit attempt."""
    return notify_pending_condition_bets(ledger, bet_ids)


def _crown_execution_message(bet):
    """Return a committed Footbreak × Crown simulation notice or ``None``.

    This is deliberately an outbox formatter rather than a candidate notifier:
    rows must already be committed by the isolated local-evidence evaluator.
    Re-check the timing and execution gate here so an old/replayed row can
    never produce a Telegram alert.
    """
    if (
        bet.get("portfolio") != CROWN_EXECUTION_PORTFOLIO
        or bet.get("strategy") != CROWN_EXECUTION_STRATEGY
        or bet.get("status") != "PENDING"
        or not bet.get("simulation_only")
        or bet.get("real_betting_enabled") is not False
    ):
        return None
    kickoff = _future_kickoff(bet.get("kickoff"))
    decision_at = _parse_time(bet.get("decision_at") or bet.get("created_at"))
    hkjc_observed = _parse_time(bet.get("hkjc_signal_observed_at"))
    crown_observed = _parse_time(bet.get("crown_execution_observed_at"))
    league = traditional_chinese_league(bet.get("league"))
    home, away = str(bet.get("home") or "").strip(), str(bet.get("away") or "").strip()
    market = str(bet.get("market_label") or "").strip()
    direction = str(bet.get("selected_role") or "").strip()
    hkjc_odds = _finite_positive(bet.get("hkjc_signal_odds"))
    crown_odds = _finite_positive(bet.get("crown_execution_odds"))
    admission = bet.get("wilson_admission") if isinstance(bet.get("wilson_admission"), dict) else {}
    try:
        line = float(bet.get("selected_line"))
        minimum = float(admission.get("minimum_acceptable_odds_raw"))
        number = int(bet.get("condition_number"))
    except (TypeError, ValueError):
        return None
    if (
        kickoff is None or decision_at is None or hkjc_observed is None or crown_observed is None
        or decision_at >= kickoff
        or (dt.datetime.now(HKT) - decision_at).total_seconds() > DEFAULT_WINDOW_MIN * 60
        or hkjc_observed >= kickoff or crown_observed >= kickoff
        or hkjc_observed > decision_at or crown_observed > decision_at
        or str(bet.get("hkjc_signal_source") or "").strip().lower()
           not in {"hkjc_public_board", "hkjc-current-board"}
        or str(bet.get("crown_execution_source") or "").strip().lower()
           != "titan007-crown-id-3"
        or not league or not home or not away or market not in set(MARKET_LABELS.values())
        or not direction or hkjc_odds is None or crown_odds is None
        or crown_odds + 1e-12 < minimum or not math.isfinite(line)
    ):
        return None
    selection = f"{direction} {line:g}"
    return "\n".join([
        "【足破×皇冠執行測試倉（模擬）】",
        f"{kickoff.astimezone(HKT).strftime('%H:%M')} {esc(league)}",
        f"{esc(home)} vs {esc(away)}",
        f"合符條件 #{number}",
        "投注平台：皇冠",
        f"馬會訊號：{esc(market)} {esc(selection)} @{hkjc_odds:.2f}",
        f"皇冠模擬：{esc(market)} {esc(selection)} @{crown_odds:.2f}",
        f"最低要求賠率：{minimum:.2f}",
        f"模擬投注 HK${float(bet.get('stake') or 0):,.0f}",
    ])


def _parse_time(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=HKT) if parsed.tzinfo is None else parsed.astimezone(HKT)


def notify_pending_crown_execution_bets(ledger, bet_ids=None, *, max_attempts=8):
    """Send only committed, still-upcoming cross-book simulation entries.

    A transport exception happens before acknowledgement is persisted, allowing
    a later notify-only run to retry.  No rejection or uncommitted candidate is
    ever sent.
    """
    requested = ({str(value) for value in bet_ids or [] if value}
                 if bet_ids is not None else None)
    if requested is not None and not requested:
        return 0
    state = load_state()
    sent_ids = set(map(str, state.get("crown_execution_test_alerts") or []))
    sent_order = [str(value) for value in state.get("crown_execution_test_alerts") or []]
    namespace = ledger.get(CROWN_EXECUTION_PORTFOLIO) if isinstance(ledger, dict) else {}
    rows = namespace.get("bets") if isinstance(namespace, dict) else []
    sent = attempted = 0
    for bet in rows or []:
        if not isinstance(bet, dict):
            continue
        bid = str(bet.get("bet_id") or "")
        if not bid or bid in sent_ids or (requested is not None and bid not in requested):
            continue
        text = _crown_execution_message(bet)
        if text is None:
            continue
        if attempted >= max(0, max_attempts):
            break
        attempted += 1
        send(text)
        sent_ids.add(bid)
        sent_order.append(bid)
        state["crown_execution_test_alerts"] = _bounded_unique_ids(sent_order)
        state["last_sent"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
        save_state(state)
        sent += 1
    return sent


def load_ledger():
    with open(LEDGER, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────── 工具 ───────────────────────────
def esc(x):
    return html.escape(str(x), quote=False)


def created_ts(b):
    """注單建立時間 —— created_at 缺失就用 history 第一筆。"""
    for v in (b.get("created_at"),):
        if v:
            return v
    hist = b.get("history") or []
    return hist[0].get("ts") if hist else None


def age_min(ts):
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=HKT)
    return (dt.datetime.now(HKT) - t).total_seconds() / 60.0


def money(x):
    return f"${float(x):,.0f}"


def pick_line(b):
    """可讀盤口 —— 優先用帳本 label(讓球會顯示隊名),否則自行組裝。

    label 格式係「<市場> <選項> <盤口>」,例如「讓球 域堡 0.0/+0.5」、
    「角球大小 角球大 10.5」,所以剝走市場前綴就得。
    """
    code, cond, side = b.get("code"), b.get("condition"), b.get("side")
    lbl, mkt = b.get("label"), b.get("market")
    # 讓球一律重組成選邊視角 ——馬會盤口係主隊視角,直接印原文會誤導。
    if code == "HDC" and cond:
        import model as _M
        team = b.get("home") if side == "H" else b.get("away")
        if not team and lbl:
            body = lbl[len(mkt):].strip() if mkt and lbl.startswith(mkt) else lbl
            team = body.rsplit(" ", 1)[0].strip()
        if team:
            return _M.hdc_label(team, cond, side or "H")
    if lbl:
        t = lbl[len(mkt):].strip() if mkt and lbl.startswith(mkt) else lbl.strip()
        if t:
            return t
    if code in ("HIL", "CHL"):
        return f"{'大' if side == 'H' else '細'} {cond}"
    return f"{SIDE_TXT.get(side, side or '')} {cond or ''}".strip()


def stages_of(led, mid):
    w = (led.get("watch") or {}).get(str(mid)) or {}
    return w.get("stages") or []


def drift_line(led, b):
    """三段預測有冇轉軚。"""
    order = {"首預": 0, "T-30": 1, "T-5": 2}
    st = sorted(stages_of(led, b.get("match_id")),
                key=lambda s: order.get(s.get("stage"), 9))
    if not st:
        return "三段對照:冇快照記錄"

    parts, verdicts = [], []
    for s in st:
        nm = s.get("stage") or "?"
        cv = s.get("conviction")
        vd = s.get("verdict") or "—"
        verdicts.append(vd)
        cvt = "—" if cv is None else f"{float(cv):.1f}"
        parts.append(f"{nm} 信念 {cvt} · {vd}")

    body = "\n".join("   " + esc(p) for p in parts)
    uniq = [v for i, v in enumerate(verdicts) if i == 0 or v != verdicts[i - 1]]
    turns = len(uniq) - 1
    if turns <= 0:
        tag = "三段結論一致 —— 由頭到尾同一方向"
    else:
        tag = f"轉軚 {turns} 次:" + " → ".join(esc(v) for v in uniq)
    return f"<b>三段對照</b>\n{body}\n   <i>{tag}</i>"


def stake_note(b):
    ss = b.get("stake_stage") or {}
    if not ss:
        return ""
    frac = ss.get("fraction")
    ft = "—"
    if frac:
        ft = ("1/3" if abs(frac - 1 / 3) < .01 else
              "1/2" if abs(frac - .5) < .01 else
              "2/3" if abs(frac - 2 / 3) < .01 else f"{frac:.2f}")
    mm = ss.get("market_mult")
    mmt = f" × {mm}(角球折讓)" if mm and mm != 1 else ""
    return (f"注碼階段:{esc(ss.get('label', '—'))} · "
            f"{ft} 凱利{mmt} · 單場上限 {float(ss.get('cap', 0)) * 100:.0f}%"
            f"(賠率低於 {1 + _KNEE:.2f} 按 b/{_KNEE:.2f} 遞減)")


# ─────────────────────────── 訊息 ───────────────────────────
def bet_msg(led, bets):
    n = dt.datetime.now(HKT)
    head = (f"<b>足破 · 新模擬注單</b>\n"
            f"{esc(n.strftime('%m/%d %H:%M'))} HKT · 共 {len(bets)} 注")

    blocks = []
    for b in bets:
        rows = [
            f"<b>{esc(b.get('home'))} v {esc(b.get('away'))}</b>",
            f"   {esc(b.get('league'))} · 開賽 {esc(b.get('kickoff'))} HKT",
            f"   投注：<b>{esc(b.get('market'))} · {esc(pick_line(b))}</b>",
            f"   賠率：<b>{float(b.get('odds', 0)):.2f}</b>",
            f"   注碼：<b>{esc(money(b.get('stake', 0)))}</b>",
        ]
        blocks.append("\n".join(rows))

    total = sum(float(b.get("stake") or 0) for b in bets)
    foot = f"<b>本次總注碼：{esc(money(total))}</b>\n只作模擬，絕不實際投注。"
    return "\n\n".join([head] + blocks + [foot])


def settled_msg(led, bets):
    n = dt.datetime.now(HKT)
    pnl = sum(float(b.get("pnl") or 0) for b in bets)
    head = (f"<b>📊 足破 · 賽果結算</b>\n"
            f"{esc(n.strftime('%m/%d %H:%M'))} HKT · {len(bets)} 注 · "
            f"本批盈虧 <b>{'+' if pnl >= 0 else ''}{esc(money(pnl))}</b>")
    rows = []
    for b in bets:
        p = float(b.get("pnl") or 0)
        rows.append(
            f"{esc(RESULT_TXT.get(b.get('result'), b.get('result') or '?'))}  "
            f"{esc(b.get('home'))} v {esc(b.get('away'))}\n"
            f"   {esc(b.get('market'))} {esc(pick_line(b))} @ "
            f"{float(b.get('odds', 0)):.2f} · 注 {esc(money(b.get('stake', 0)))}"
            f" → <b>{'+' if p >= 0 else ''}{esc(money(p))}</b>")
    s = led.get("stats") or {}
    hr = s.get("hit_rate")
    foot = (f"<b>累計</b> 盈虧 {esc(money(s.get('pnl') or 0))} · "
            f"ROI {('—' if s.get('roi') is None else f'{float(s['roi']) * 100:+.2f}%')} · "
            f"命中 {('—' if hr is None else f'{float(hr) * 100:.1f}% ({s.get('hits')}/{s.get('n_decided')})')} · "
            f"戶口 {esc(money(s.get('equity') if s.get('equity') is not None else led.get('bankroll')))}")
    return "\n\n".join([head] + rows + [foot])


def review_events(report):
    """Return stable, deduplicatable human-review events from a backtest report."""
    events = []
    labels = {"footbreak": "足破", "crown": "皇冠"}
    # The standalone challenger report is still notification-isolated: only a
    # completed frozen prospective v3 window that clears every existing gate
    # may enter this already-existing human-review channel.
    challenger_hil = (
        (((report.get("systems") or {}).get("crown") or {}).get("tests") or {})
        .get("HIL", {}).get("prospective_v3") or {}
    )
    if challenger_hil.get("status") == "candidate_passed_human_review_required":
        version = str(challenger_hil.get("state_version_hash") or "unknown")
        delta = challenger_hil.get("delta") or {}
        events.append({
            "key": f"prospective-v3:crown:HIL:{version}",
            "system": "皇冠",
            "kind": "HIL v3 前瞻影子模型通過",
            "detail": (
                f"凍結後 {challenger_hil.get('prospective_fixtures', 0)} 場/"
                f"{challenger_hil.get('prospective_rows', 0)} 預測 · "
                f"Brier Δ {delta.get('brier')} · "
                f"log loss Δ {delta.get('log_loss')} · "
                f"命中率 Δ {delta.get('accuracy')}"
            ),
        })
    # The Crown CHL frozen prospective shadow uses the same isolated rule: it
    # may only enter this channel after every final gate has already passed.
    challenger_chl = (
        (((report.get("systems") or {}).get("crown") or {}).get("tests") or {})
        .get("CHL", {}).get("prospective_chl") or {}
    )
    if challenger_chl.get("status") == "candidate_passed_human_review_required":
        version = str(challenger_chl.get("state_version_hash") or "unknown")
        delta = challenger_chl.get("delta") or {}
        events.append({
            "key": f"prospective-chl:crown:CHL:{version}",
            "system": "皇冠",
            "kind": "CHL 前瞻凍結影子模型通過",
            "detail": (
                f"策略 {challenger_chl.get('selected_strategy') or 'unknown'} · "
                f"凍結後 {challenger_chl.get('prospective_fixtures', 0)} 場獨立賽事"
                f"（參考 {challenger_chl.get('prospective_rows', 0)} 階段列）· "
                f"Brier Δ {delta.get('brier')} · "
                f"log loss Δ {delta.get('log_loss')} · "
                f"命中率 Δ {delta.get('accuracy')}"
            ),
        })
    for system, payload in (report.get("systems") or {}).items():
        label = labels.get(system, system)
        if payload.get("status") == "ready_for_human_review":
            baseline_matches = payload.get("baseline_matches", 0)
            events.append({
                "key": f"forward:{system}:{baseline_matches}",
                "system": label,
                "kind": "前向驗證批次完成",
                "detail": (
                    f"新賽事 {payload.get('new_matches', 0)} 場 · "
                    f"T-5 覆蓋 {float(payload.get('t5_holdout_coverage') or 0) * 100:.1f}%"
                ),
            })

        upgrade = payload.get("automatic_upgrade_test") or {}
        if upgrade.get("status") == "candidate_passed":
            candidate = str(upgrade.get("recommended_candidate") or "unknown")
            events.append({
                "key": f"upgrade:{system}:{candidate}",
                "system": label,
                "kind": "整體挑戰者通過",
                "detail": (
                    f"候選 {candidate} · "
                    f"已驗證 {upgrade.get('eligible_matches', 0)} 場"
                ),
            })

        market_tests = (
            (payload.get("market_model_upgrade_tests") or {}).get("tests") or {}
        )
        for market, test in market_tests.items():
            if test.get("status") != "candidate_passed_human_review_required":
                continue
            alpha = (test.get("challenger") or {}).get("calibration_alpha")
            delta = test.get("delta") or {}
            events.append({
                "key": f"market:{system}:{market}:{alpha}",
                "system": label,
                "kind": f"{market} 校準挑戰者通過",
                "detail": (
                    f"alpha {alpha} · 驗證 {test.get('eligible_matches', 0)} 場 · "
                    f"Brier Δ {delta.get('brier')} · "
                    f"命中率 Δ {delta.get('accuracy')}"
                ),
            })
        model_tests = (payload.get("model_challenger_tests") or {}).get("tests") or {}
        for market, test in model_tests.items():
            if test.get("status") != "candidate_passed_human_review_required":
                continue
            delta = test.get("delta") or {}
            version = str(test.get("model_version_hash") or "unknown")
            events.append({
                "key": f"challenger:{system}:{market}:{version}",
                "system": label,
                "kind": f"{market} 特徵挑戰模型通過",
                "detail": (
                    f"模型 {str(test.get('model_version') or 'unknown')} · "
                    f"驗證 {test.get('holdout_fixtures', 0)} 場/{test.get('holdout_rows', 0)} 預測 · "
                    f"Brier Δ {delta.get('brier')} · "
                    f"log loss Δ {delta.get('log_loss')} · "
                    f"命中率 Δ {delta.get('accuracy')}"
                ),
            })
    return events


def review_msg(events, generated_at=None):
    rows = [
        "<b>足破 · 模型候選等待人工審核</b>",
        esc(generated_at or dt.datetime.now(HKT).isoformat(timespec="minutes")),
        "",
    ]
    for event in events:
        rows.extend([
            f"<b>{esc(event['system'])} · {esc(event['kind'])}</b>",
            f"   {esc(event['detail'])}",
            "",
        ])
    rows.append("模型未有自動套用；請人工審核後先決定是否升級。")
    return "\n".join(rows)


# ─────────────────────────── 發送 ───────────────────────────
def send(text):
    if not CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID 未設定")
    if BOT_TOKEN:
        body = json.dumps({
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Telegram 直接發送失敗:{type(exc).__name__}") from exc
        if not result.get("ok"):
            raise RuntimeError(f"Telegram 直接發送失敗:{result.get('description', 'unknown')}")
        return "telegram_bot_api_ok"

    # Backward-compatible local fallback only. Production should always use
    # the long-lived bot token so delivery does not depend on an expiring
    # external-tool session.
    payload = json.dumps({
        "source_id": SOURCE_ID, "tool_name": TOOL,
        "arguments": {"chatId": CHAT_ID, "text": text, "parse_mode": "HTML"},
    }, ensure_ascii=False)
    p = subprocess.run(["external-tool", "call", payload],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Telegram 發送失敗:{p.stderr.strip()[:400]}")
    return p.stdout.strip()[:200]


# ─────────────────────────── 主流程 ───────────────────────────
def new_bets(led, state, window):
    out = []
    for b in led.get("bets", []):
        if b.get("status") != "PENDING":
            continue
        bid = b.get("bet_id")
        if not bid or bid in state["bets"]:
            continue
        a = age_min(created_ts(b))
        if a is None or a > window:
            continue
        out.append(b)
    out.sort(key=lambda b: b.get("kickoff") or "")
    return out


def new_settled(led, state):
    out = [b for b in led.get("bets", [])
           if b.get("status") == "SETTLED"
           and b.get("bet_id") and b["bet_id"] not in state["settled"]]
    out.sort(key=lambda b: b.get("kickoff") or "")
    return out


# ─────────────────── 排程佇列 / 每晚總結 ───────────────────
def read_queue(n=12):
    """跑 next_due.py 攞應有嘅預測時點佇列。失敗回傳 None。"""
    try:
        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "next_due.py"),
             "--queue", str(n)],
            capture_output=True, text=True, timeout=180, cwd=HERE)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def hhmm(iso):
    """'2026-08-08T00:28:00' → '08/08 00:28'"""
    try:
        d = dt.datetime.fromisoformat(iso)
        return d.strftime("%m/%d %H:%M")
    except Exception:
        return str(iso)


def sched_msg(q, added, dropped, note):
    rows = q.get("queue") or []
    now = dt.datetime.now(HKT)
    L = ["<b>🗓 足破 · 預測時點佇列更新</b>",
         "%s HKT" % now.strftime("%m/%d %H:%M"), ""]

    if added:
        L.append("<b>新排入 %d 個時點</b>" % len(added))
        for r in added[:8]:
            L.append("   ＋ %s　%s" % (hhmm(r["run_at"]), esc(r.get("label") or "")))
        if len(added) > 8:
            L.append("   … 另有 %d 個" % (len(added) - 8))
        L.append("")

    if dropped:
        L.append("<b>移出佇列 %d 個</b>(場次改期、取消,或者已經做完)" % len(dropped))
        for r in dropped[:6]:
            L.append("   － %s" % hhmm(r))
        if len(dropped) > 6:
            L.append("   … 另有 %d 個" % (len(dropped) - 6))
        L.append("")

    if rows:
        nxt = rows[0]
        L.append("下一個時點  <b>%s</b>" % hhmm(nxt["run_at"]))
        L.append("   %s" % esc(nxt.get("label") or ""))
        mins = nxt.get("in_min")
        if isinstance(mins, (int, float)):
            L.append("   %.0f 分鐘後" % mins)
        L.append("")
        L.append("佇列現有 <b>%d</b> 個時點,排到 <b>%s</b>"
                 % (len(rows), hhmm(rows[-1]["run_at"])))
    else:
        L.append("佇列而家係空 —— 暫時冇待做嘅 T-30 / T-5。")

    tw = q.get("total_wakes")
    if isinstance(tw, int) and rows and tw > len(rows):
        L.append("未來全板總共仲有 %d 個時點待排,之後會分批補上。" % tw)

    if note:
        L += ["", esc(note)]
    return "\n".join(L)


def sweep_msg(led, q):
    now = dt.datetime.now(HKT)
    watch = led.get("watch") or {}
    bets = led.get("bets") or []
    s = led.get("stats") or {}

    # 今日內做嘅首預
    today = now.date()
    fresh = 0
    for w in watch.values():
        for st in (w.get("stages") or []):
            if st.get("stage") != "首預":
                continue
            try:
                if dt.datetime.fromisoformat(st["ts"]).date() == today:
                    fresh += 1
            except Exception:
                pass
            break

    # 未來 24 小時開賽場數
    soon = 0
    for w in watch.values():
        try:
            ko = dt.datetime.strptime(w["kickoff"], "%Y-%m-%d %H:%M").replace(tzinfo=HKT)
        except Exception:
            continue
        if 0 <= (ko - now).total_seconds() <= 86400:
            soon += 1

    pend = [b for b in bets if b.get("status") == "PENDING"]
    open_stake = sum(b.get("stake") or 0 for b in pend)
    equity = s.get("equity") or 0
    pnl = s.get("pnl") or 0
    n_set = s.get("n_settled") or 0
    n_dec = s.get("n_decided") or 0
    hits = s.get("hits") or 0

    L = ["<b>🌙 足破 · 全板首預完成</b>",
         "%s HKT" % now.strftime("%m/%d %H:%M"), "",
         "<b>今晚掃板</b>",
         "   新做首預 <b>%d</b> 場" % fresh,
         "   追蹤中共 <b>%d</b> 場 · 未來 24 小時開賽 <b>%d</b> 場" % (len(watch), soon),
         "",
         "<b>倉位</b>",
         "   待決注單 <b>%d</b> 注 · 在場注碼 <b>%s</b>" % (len(pend), money(open_stake)),
         "   戶口 <b>%s</b> · 累計盈虧 <b>%s%s</b>"
         % (money(equity), "+" if pnl >= 0 else "-", money(abs(pnl)))]

    if n_dec:
        L.append("   已結算 %d 注 · 命中 %d/%d(%.0f%%) · ROI %.1f%%"
                 % (n_set, hits, n_dec, 100.0 * hits / n_dec,
                    100.0 * (s.get("roi") or 0)))
    else:
        L.append("   仲未有已結算注單 —— 樣本累積中")

    L += ["", "<b>注碼階段</b>", "   " + stake_note_plain()]

    rows = (q or {}).get("queue") or []
    L.append("")
    if rows:
        L.append("<b>明日時點</b>")
        L.append("   下一個 <b>%s</b> · %s" % (hhmm(rows[0]["run_at"]),
                                              esc(rows[0].get("label") or "")))
        L.append("   佇列 %d 個,排到 %s" % (len(rows), hhmm(rows[-1]["run_at"])))
    else:
        L.append("<b>明日時點</b>  暫時未有待做嘅 T-30 / T-5")

    L += ["", "落注一律只喺開賽前 5 分鐘決定。"]
    return "\n".join(L)


def stake_note_plain():
    """注碼階段一句摘要,獨立於注單。"""
    try:
        import staking as K
        st = K.stage()
    except Exception:
        return "讀取失敗"
    frac = st.get("fraction") or 0
    fr = None
    for val, txt in ((1 / 3, "1/3"), (0.5, "1/2"), (2 / 3, "2/3")):
        if abs(frac - val) < 1e-3:
            fr = txt
            break
    if fr is None:
        fr = "%.2f" % frac
    bits = ["%s · %s 凱利 · 單場上限 %.0f%%"
            % (esc(st.get("label") or ""), fr, 100.0 * (st.get("cap") or 0))]
    n = st.get("n_settled")
    sl = st.get("slope")
    if n is not None:
        tail = "已結算 %d 注" % n
        if sl is not None:
            tail += " · 校準斜率 %.2f" % sl
        bits.append(tail)
    return "\n   ".join(bits)



def main(argv):
    # All legacy bet, settlement, health, scheduler and simulation notices are
    # intentionally disabled.  New granular notices are transactionally
    # emitted by record_picks immediately after a fresh stage persistence.
    print("舊有 Telegram 通知已停用；只保留新保存階段的細緻條件提示。")
    return 0
    dry = "--dry" in argv
    mode_review = "--review" in argv
    mode_settled = "--settled" in argv
    mode_sched = "--sched" in argv
    mode_sweep = "--sweep" in argv
    mode_watch = "--watch" in argv
    note = ""
    if "--note" in argv:
        note = argv[argv.index("--note") + 1]
    window = DEFAULT_WINDOW_MIN
    if "--window" in argv:
        window = float(argv[argv.index("--window") + 1])

    if mode_review:
        print("模型候選 Telegram 通知已停用；只保留雷達模擬注")
        return 0

    # User preference: the command-line notifier is only for new radar
    # simulation bets. Fresh T-5 corner alerts are emitted transactionally by
    # record_picks.py after the immutable stage has been saved.
    if mode_watch or mode_sched or mode_sweep or mode_settled:
        print("此通知類型已停用；Telegram 只發新注單")
        return 0

    led = load_ledger()
    state = load_state()

    if mode_watch:
        import watch_msg as WM
        w = float(argv[argv.index("--window") + 1]) if "--window" in argv \
            else WM.WATCH_WINDOW_MIN
        rows = WM.collect(led, state, w)
        if not rows:
            print(f"冇 {w:.0f} 分鐘內新做而未通知嘅 T-5 觀望場 —— 不發送")
            return 0
        text = WM.build(led, rows)
        if dry:
            print(text)
            print(f"\n[dry-run] 會通知 {len(rows)} 場觀望,唔會寫狀態")
            return 0
        send(text)
        state["watch"] = ((state.get("watch") or [])
                          + [k for k, _w, _s in rows])[-800:]
        state["last_sent"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
        save_state(state)
        print(f"已發 T-5 觀望通知({len(rows)} 場)")
        return 0

    if mode_sched:
        q = read_queue(12)
        if q is None:
            print("next_due.py 讀唔到 —— 不發送")
            return 1
        rows = q.get("queue") or []
        cur = [r["run_at"] for r in rows]
        prev = state.get("queue") or []
        added = [r for r in rows if r["run_at"] not in prev]
        dropped = [t for t in prev if t not in cur]
        if not added and not dropped:
            print("排程佇列無變動 —— 不發送")
            return 0
        text = sched_msg(q, added, dropped, note)
        if dry:
            print(text)
            return 0
        send(text)
        state["queue"] = cur
        state["last_sent"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
        save_state(state)
        print("已發排程更新(新增 %d,移出 %d)" % (len(added), len(dropped)))
        return 0

    if mode_sweep:
        day = dt.datetime.now(HKT).strftime("%Y-%m-%d")
        if day in (state.get("sweeps") or []) and not dry:
            print("今日 (%s) 已經發過全板總結 —— 不重複發送" % day)
            return 0
        text = sweep_msg(led, read_queue(12))
        if dry:
            print(text)
            return 0
        send(text)
        state["sweeps"] = ((state.get("sweeps") or []) + [day])[-30:]
        # 總結入面已經報咗明日時點,同步佇列基準,避免緊接住又發一次佇列更新
        _q = read_queue(12) or {}
        state["queue"] = [r["run_at"] for r in (_q.get("queue") or [])]
        state["last_sent"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
        save_state(state)
        print("已發全板首預總結 (%s)" % day)
        return 0

    if mode_settled:
        sel = new_settled(led, state)
        if not sel:
            print("冇未通知嘅結算注單 —— 不發送")
            return 0
        text, key = settled_msg(led, sel), "settled"
    else:
        sel = new_bets(led, state, window)
        if not sel:
            print(f"冇 {window:.0f} 分鐘內新建立而未通知嘅注單 —— 不發送")
            return 0
        text, key = bet_msg(led, sel), "bets"

    if dry:
        print(text)
        print(f"\n[dry-run] 會通知 {len(sel)} 注,唔會寫狀態")
        return 0

    send(text)
    state[key].extend(b["bet_id"] for b in sel)
    state[key] = state[key][-500:]
    state["last_sent"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
    save_state(state)
    print(f"已通知 {len(sel)} 注（{key}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
