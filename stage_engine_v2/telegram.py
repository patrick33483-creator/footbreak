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

# 只通知目前公開觀察條件；已降級背景條件不發 Telegram。
CONDITION_ALERT_IDS = frozenset({
    "S-HIL-T5-OVER-185",
    "WATCH-HIL-T5-OVER-180",
    "A-HIL-OPEN-T5-OVER-180",
    "A-HDC-OPEN-AWAY-MINUS-050",
    "S-HIL-OPEN-OVER-3-180",
})
CONDITION_ALERT_ACTIVATED_AT = datetime.fromisoformat("2026-09-02T20:41:00+08:00")

TELEGRAM_API = "https://api.telegram.org"


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
    home = " ".join(str(observation.get("home") or "").split()).casefold()
    away = " ".join(str(observation.get("away") or "").split()).casefold()
    kickoff_raw = observation.get("kickoff")
    if home and away and kickoff_raw:
        try:
            kickoff = datetime.fromisoformat(str(kickoff_raw).replace("Z", "+00:00"))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
            return f"{home}|{away}|{kickoff.astimezone(timezone.utc).isoformat()}"
        except ValueError:
            return f"{home}|{away}|{str(kickoff_raw).strip()}"
    return str(observation.get("match_id") or "").strip()


def _condition_identity_aliases(
    condition_id: str,
    observation: dict[str, Any],
) -> set[str]:
    """Include legacy ID-based keys so deployment never re-sends old alerts."""
    aliases = {f"{condition_id}:{_condition_identity_key(observation)}"}
    match_id = str(observation.get("match_id") or "").strip()
    if match_id:
        aliases.add(f"{condition_id}:{match_id}")
    return aliases


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


def format_condition_group_message(
    matches: list[tuple[dict[str, Any], dict[str, Any]]],
) -> str:
    """同一場命中多個條件時，只發一則通知並逐條列出歷史命中率。"""
    if not matches:
        return ""
    first_observation = matches[0][1]
    lines = [
        "[皇冠V2] 合資格賽事",
        str(first_observation.get("league") or ""),
        f"{first_observation.get('home') or ''} vs {first_observation.get('away') or ''}",
        f"開賽 (HKT): {first_observation.get('kickoff') or ''}",
        f"命中條件：{len(matches)} 條",
    ]
    for index, (condition, observation) in enumerate(matches, start=1):
        hist = condition.get("historical") or {}
        sample = hist.get("sample")
        hit_rate = hist.get("hit_rate")
        roi = hist.get("roi")
        stage = observation.get("decision_stage") or ""
        direction = (observation.get("directions") or {}).get(stage) or ""
        selected_line = observation.get("selected_line")
        odds = observation.get("odds")
        detail = f"{index}. {condition.get('title') or condition.get('id') or ''}"
        lines.extend(["", detail, f"時點：{stage}｜方向：{direction}"])
        market_line = []
        if selected_line is not None:
            market_line.append(f"線位 {selected_line}")
        if odds is not None:
            try:
                market_line.append(f"賠率 {float(odds):.2f}")
            except (TypeError, ValueError):
                pass
        if market_line:
            lines.append("｜".join(market_line))
        if sample is not None and hit_rate is not None:
            roi_txt = f"｜ROI {roi*100:+.1f}%" if roi is not None else ""
            lines.append(
                f"歷史：{sample} 場｜命中率 {hit_rate*100:.1f}%{roi_txt}"
            )
    lines.extend(["", "純統計追蹤，不代表投注建議。"])
    return "\n".join(line for line in lines if line)


def _is_alert_eligible(observation: dict[str, Any]) -> bool:
    """只發上線後仍未開賽的項目，避免首次啟用時補發歷史紀錄。"""
    kickoff_raw = observation.get("kickoff")
    if not kickoff_raw:
        return False
    try:
        kickoff = datetime.fromisoformat(str(kickoff_raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    return kickoff >= CONDITION_ALERT_ACTIVATED_AT


def send_condition_alert(
    condition: dict[str, Any],
    observation: dict[str, Any],
    *,
    sent_log_path: Path | str = DEFAULT_CONDITION_SENT_LOG,
    enabled_env: str = "STAGE_V2_CONDITION_ALERT_ENABLED",
    bot_token_env: str = "TELEGRAM_BOT_TOKEN",
    chat_id_env: str = "TELEGRAM_CHAT_ID",
) -> dict[str, Any]:
    """發（或 shadow 記錄）一個細分條件達標通知。

    Idempotency：JSONL append log，鍵為 "<condition_id>:<match 識別>"。
    返回 {sent, shadow, skipped, reason, key}
    """
    condition_id = str(condition.get("id") or "")
    key = f"{condition_id}:{_condition_identity_key(observation)}"
    log_path = Path(sent_log_path)

    sent_keys = _load_sent_keys(log_path)
    if sent_keys.intersection(_condition_identity_aliases(condition_id, observation)):
        return {"sent": False, "shadow": False, "skipped": True, "reason": "duplicate", "key": key}

    text = format_condition_message(condition, observation)
    enabled = _env_flag(enabled_env, default=False)
    bot_token = os.getenv(bot_token_env, "").strip()
    chat_id = os.getenv(chat_id_env, "").strip()
    ts = datetime.now(timezone.utc).isoformat()

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
    condition_ids: frozenset[str] = CONDITION_ALERT_IDS,
    sent_log_path: Path | str = DEFAULT_CONDITION_SENT_LOG,
    enabled_env: str = "STAGE_V2_CONDITION_ALERT_ENABLED",
    bot_token_env: str = "TELEGRAM_BOT_TOKEN",
    chat_id_env: str = "TELEGRAM_CHAT_ID",
) -> list[dict[str, Any]]:
    """按比賽合併公開條件；每場只發一則並列出各條件命中率。"""
    log_path = Path(sent_log_path)
    sent_keys = _load_sent_keys(log_path)
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = {}
    queued_keys: set[str] = set()
    results: list[dict[str, Any]] = []
    for condition in segmented_conditions.get("public_conditions") or []:
        if not isinstance(condition, dict):
            continue
        if str(condition.get("id") or "") not in condition_ids:
            continue
        for observation in condition.get("observations") or []:
            if not isinstance(observation, dict):
                continue
            if not _is_alert_eligible(observation):
                continue
            key = f"{condition.get('id') or ''}:{_condition_identity_key(observation)}"
            aliases = _condition_identity_aliases(
                str(condition.get("id") or ""), observation
            )
            if sent_keys.intersection(aliases) or key in queued_keys:
                results.append({
                    "sent": False, "shadow": False, "skipped": True,
                    "reason": "duplicate", "key": key,
                })
                continue
            queued_keys.add(key)
            identity = _condition_identity_key(observation)
            grouped.setdefault(identity, []).append((condition, observation, key))

    enabled = _env_flag(enabled_env, default=False)
    bot_token = os.getenv(bot_token_env, "").strip()
    chat_id = os.getenv(chat_id_env, "").strip()
    for matches in grouped.values():
        text = format_condition_group_message([
            (condition, observation) for condition, observation, _ in matches
        ])
        ts = datetime.now(timezone.utc).isoformat()
        if not enabled or not bot_token or not chat_id:
            reason = "shadow_mode" if not enabled else "missing_credentials"
            for _, _, key in matches:
                _append_sent(log_path, {
                    "key": key, "sent_at_utc": ts, "mode": "shadow", "text": text,
                })
                results.append({
                    "sent": False, "shadow": True, "skipped": False,
                    "reason": reason, "key": key,
                })
            continue

        ok = _post_telegram(bot_token, chat_id, text)
        for _, _, key in matches:
            if ok:
                _append_sent(log_path, {
                    "key": key, "sent_at_utc": ts, "mode": "live", "text": text,
                })
            results.append({
                "sent": ok, "shadow": False, "skipped": False,
                "reason": "ok" if ok else "telegram_error", "key": key,
            })
    return results


__all__ = [
    "send_stage",
    "format_message",
    "DEFAULT_SENT_LOG",
    "send_condition_alert",
    "send_condition_alerts",
    "format_condition_message",
    "DEFAULT_CONDITION_SENT_LOG",
    "CONDITION_ALERT_IDS",
    "CONDITION_ALERT_ACTIVATED_AT",
    "format_condition_group_message",
]
