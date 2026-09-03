"""Telegram 通知 —— shadow 期間預設唔發信。

Idempotency：JSONL append log，鍵為 "<fixture_id>:<stage>"。
啟用信號：環境變數 STAGE_V2_TELEGRAM_ENABLED=1。
Shadow：只 append log，唔真正 POST，方便對比。
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SENT_LOG = Path("/var/lib/footbreak/stage_engine_v2/telegram_sent.jsonl")
DEFAULT_CONDITION_SENT_LOG = Path("/var/lib/footbreak/stage_engine_v2/condition_telegram_sent.jsonl")

# 皇冠V2 細分條件當中，決策時點為 T-5 嘅 3 個公開條件（第 4 個
# A-HDC-OPEN-AWAY-MINUS-050 決策時點為首預，明確排除）。
T5_CONDITION_ALERT_IDS = frozenset({
    "S-HIL-T5-OVER-185",
    "A-HIL-OPEN-T5-OVER-180",
    "A-HDC-HHH-SAME-LINE",
})

TELEGRAM_API = "https://api.telegram.org"
CONDITION_ALERT_MAX_SECONDS_BEFORE_KICKOFF = 10 * 60


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_sent_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = row.get("key")
                if isinstance(key, str):
                    keys.add(key)
    except OSError:
        return set()
    return keys


def _append_sent(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def format_message(prediction: dict[str, Any]) -> str:
    """格式化 Telegram 訊息。純文字，唔用 markdown 避免 escape 問題。"""
    stage = prediction.get("stage", "")
    league = prediction.get("league", "")
    home = prediction.get("home", "")
    away = prediction.get("away", "")
    kickoff = prediction.get("kickoff_hkt", "")
    lead_market = prediction.get("lead_market", "")
    lead_label = prediction.get("lead_label", "")
    odds = prediction.get("lead_odds")
    prob = prediction.get("lead_prob")
    ev = prediction.get("lead_ev")
    lines = [
        f"[{stage}] {league}",
        f"{home} vs {away}",
        f"開賽 (HKT): {kickoff}",
    ]
    if lead_market or lead_label:
        lines.append(f"lead: {lead_market} {lead_label}")
    if odds is not None:
        lines.append(f"odds: {odds:.2f}")
    if prob is not None:
        lines.append(f"prob: {prob*100:.1f}%")
    if ev is not None:
        lines.append(f"EV: {ev*100:+.1f}%")
    return "\n".join(lines)


def _post_telegram(bot_token: str, chat_id: str, text: str, timeout: float = 8.0) -> bool:
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def send_stage(
    prediction: dict[str, Any],
    *,
    sent_log_path: Path | str = DEFAULT_SENT_LOG,
    enabled_env: str = "STAGE_V2_TELEGRAM_ENABLED",
    bot_token_env: str = "STAGE_V2_TELEGRAM_BOT_TOKEN",
    chat_id_env: str = "STAGE_V2_TELEGRAM_CHAT_ID",
) -> dict[str, Any]:
    """發（或 shadow 記錄）一個 stage 通知。

    返回 {sent: bool, shadow: bool, skipped: bool, reason: str}
    """
    fx_id = str(prediction.get("fixture_id") or "")
    stage = str(prediction.get("stage") or "")
    key = f"{fx_id}:{stage}"
    log_path = Path(sent_log_path)

    sent_keys = _load_sent_keys(log_path)
    if key in sent_keys:
        return {"sent": False, "shadow": False, "skipped": True, "reason": "duplicate"}

    text = format_message(prediction)
    enabled = _env_flag(enabled_env, default=False)
    bot_token = os.getenv(bot_token_env, "").strip()
    chat_id = os.getenv(chat_id_env, "").strip()
    ts = datetime.now(timezone.utc).isoformat()

    if not enabled or not bot_token or not chat_id:
        # Shadow：唔真正發，但記錄
        _append_sent(log_path, {
            "key": key,
            "sent_at_utc": ts,
            "mode": "shadow",
            "text": text,
        })
        return {
            "sent": False,
            "shadow": True,
            "skipped": False,
            "reason": "shadow_mode" if not enabled else "missing_credentials",
        }

    ok = _post_telegram(bot_token, chat_id, text)
    _append_sent(log_path, {
        "key": key,
        "sent_at_utc": ts,
        "mode": "live" if ok else "live_failed",
        "text": text,
    })
    return {
        "sent": ok,
        "shadow": False,
        "skipped": False,
        "reason": "ok" if ok else "telegram_error",
    }


def _condition_identity_key(observation: dict[str, Any]) -> str:
    match_id = str(observation.get("match_id") or "").strip()
    if match_id:
        return match_id
    home = str(observation.get("home") or "").strip().casefold()
    away = str(observation.get("away") or "").strip().casefold()
    kickoff = str(observation.get("kickoff") or "").strip()
    return f"{home}|{away}|{kickoff}"


def _condition_alert_is_current(
    observation: dict[str, Any],
    now_utc: datetime,
) -> bool:
    """Only allow a T-5 observation shortly before a future kickoff."""
    raw = str(observation.get("kickoff") or "").strip()
    if not raw:
        return False
    try:
        kickoff = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if kickoff.tzinfo is None:
        return False
    seconds_before = (kickoff.astimezone(timezone.utc) - now_utc).total_seconds()
    return 0 < seconds_before <= CONDITION_ALERT_MAX_SECONDS_BEFORE_KICKOFF


def format_condition_message(condition: dict[str, Any], observation: dict[str, Any]) -> str:
    """格式化細分條件達標 Telegram 訊息。純文字，不用 markdown。"""
    tier = condition.get("tier", "")
    title = condition.get("title", "")
    path_label = condition.get("path_label", "")
    market = condition.get("market", "")
    stage = observation.get("decision_stage", "")
    league = observation.get("league", "")
    home = observation.get("home", "")
    away = observation.get("away", "")
    kickoff = observation.get("kickoff", "")
    directions = observation.get("directions") or {}
    direction = directions.get(stage) or ""
    line = observation.get("selected_line")
    odds = observation.get("odds")
    hist = condition.get("historical") or {}
    sample = hist.get("sample")
    hit_rate = hist.get("hit_rate")
    roi = hist.get("roi")

    lines = [
        f"[皇冠V2 細分條件] {tier}級·{stage} 達標",
        title,
        league,
        f"{home} vs {away}",
        f"開賽 (HKT): {kickoff}",
        f"路徑: {path_label}",
    ]
    direction_line = f"方向: {direction}" if direction else ""
    if line is not None:
        direction_line += f"（線位 {line}）" if direction_line else f"線位 {line}"
    if direction_line:
        lines.append(direction_line)
    if odds is not None:
        try:
            lines.append(f"賠率: {float(odds):.2f}")
        except (TypeError, ValueError):
            pass
    if sample is not None and hit_rate is not None:
        roi_txt = f"，ROI {roi*100:+.1f}%" if roi is not None else ""
        lines.append(f"歷史樣本 {sample} 場：命中率 {hit_rate*100:.1f}%{roi_txt}")
    lines.append("（純統計追蹤，不代表投注建議）")
    return "\n".join(line for line in lines if line)


def send_condition_alert(
    condition: dict[str, Any],
    observation: dict[str, Any],
    *,
    sent_log_path: Path | str = DEFAULT_CONDITION_SENT_LOG,
    enabled_env: str = "STAGE_V2_CONDITION_ALERT_ENABLED",
    bot_token_env: str = "TELEGRAM_BOT_TOKEN",
    chat_id_env: str = "TELEGRAM_CHAT_ID",
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """發（或 shadow 記錄）一個細分條件達標通知。

    Idempotency：JSONL append log，鍵為 "<condition_id>:<match 識別>"。
    返回 {sent, shadow, skipped, reason, key}
    """
    condition_id = str(condition.get("id") or "")
    key = f"{condition_id}:{_condition_identity_key(observation)}"
    log_path = Path(sent_log_path)
    now = now_utc or datetime.now(timezone.utc)

    # Fail closed before reading or writing the dedupe log.  Dashboard rebuilds
    # and result backfills include historical observations; they must never
    # become actionable Telegram alerts after kickoff.
    if not _condition_alert_is_current(observation, now):
        return {
            "sent": False,
            "shadow": False,
            "skipped": True,
            "reason": "outside_t5_window",
            "key": key,
        }

    sent_keys = _load_sent_keys(log_path)
    if key in sent_keys:
        return {"sent": False, "shadow": False, "skipped": True, "reason": "duplicate", "key": key}

    text = format_condition_message(condition, observation)
    enabled = _env_flag(enabled_env, default=False)
    bot_token = os.getenv(bot_token_env, "").strip()
    chat_id = os.getenv(chat_id_env, "").strip()
    ts = now.isoformat()

    if not enabled or not bot_token or not chat_id:
        _append_sent(log_path, {
            "key": key,
            "sent_at_utc": ts,
            "mode": "shadow",
            "text": text,
        })
        return {
            "sent": False,
            "shadow": True,
            "skipped": False,
            "reason": "shadow_mode" if not enabled else "missing_credentials",
            "key": key,
        }

    ok = _post_telegram(bot_token, chat_id, text)
    _append_sent(log_path, {
        "key": key,
        "sent_at_utc": ts,
        "mode": "live" if ok else "live_failed",
        "text": text,
    })
    return {
        "sent": ok,
        "shadow": False,
        "skipped": False,
        "reason": "ok" if ok else "telegram_error",
        "key": key,
    }


def send_condition_alerts(
    segmented_conditions: dict[str, Any],
    *,
    condition_ids: frozenset[str] = T5_CONDITION_ALERT_IDS,
    sent_log_path: Path | str = DEFAULT_CONDITION_SENT_LOG,
    enabled_env: str = "STAGE_V2_CONDITION_ALERT_ENABLED",
    bot_token_env: str = "TELEGRAM_BOT_TOKEN",
    chat_id_env: str = "TELEGRAM_CHAT_ID",
    now_utc: datetime | None = None,
) -> list[dict[str, Any]]:
    """逐個遠過指定條件（預設 T-5 嘅 3 個公開條件）嘅 observations，
    對每一場尚未發過嘅比賽發 Telegram 達標。
    """
    results: list[dict[str, Any]] = []
    now = now_utc or datetime.now(timezone.utc)
    for condition in segmented_conditions.get("public_conditions") or []:
        if not isinstance(condition, dict):
            continue
        if str(condition.get("id") or "") not in condition_ids:
            continue
        for observation in condition.get("observations") or []:
            if not isinstance(observation, dict):
                continue
            result = send_condition_alert(
                condition,
                observation,
                sent_log_path=sent_log_path,
                enabled_env=enabled_env,
                bot_token_env=bot_token_env,
                chat_id_env=chat_id_env,
                now_utc=now,
            )
            results.append(result)
    return results


__all__ = [
    "send_stage",
    "format_message",
    "DEFAULT_SENT_LOG",
    "send_condition_alert",
    "send_condition_alerts",
    "format_condition_message",
    "DEFAULT_CONDITION_SENT_LOG",
    "T5_CONDITION_ALERT_IDS",
    "CONDITION_ALERT_MAX_SECONDS_BEFORE_KICKOFF",
]
