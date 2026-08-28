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


__all__ = ["send_stage", "format_message", "DEFAULT_SENT_LOG"]
