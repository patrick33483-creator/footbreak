#!/usr/bin/env python3
"""Server-owned Telegram silence classification for all football systems.

The monitor reads only local Footbreak/Crown state, the Odds Radar SQLite
database, local systemd metadata and Radar's loopback health endpoint.  It
never calls an odds provider or starts a prediction.  After a configurable
silence window it sends one sanitised summary, then waits for another full
window before repeating.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import subprocess
import tempfile
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


HKT = timezone(timedelta(hours=8))
DEFAULT_SILENCE_SECONDS = 60 * 60
DEFAULT_STATE_PATH = Path("/var/lib/footbreak/telegram-silence-monitor.json")
DEFAULT_RADAR_DB = Path("/opt/odds-radar/data/data.db")
FOOTBREAK_LEDGER = Path("/opt/footbreak/system/sim_ledger.json")
FOOTBREAK_NOTIFY = Path("/opt/footbreak/system/notify_state.json")
CROWN_STATE = Path("/var/lib/footbreak/crown")


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(HKT)
    return current.replace(tzinfo=HKT) if current.tzinfo is None else current.astimezone(HKT)


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, HKT)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for layout in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, layout)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed.replace(tzinfo=HKT) if parsed.tzinfo is None else parsed.astimezone(HKT)


def _json(path: Path, fallback: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".telegram-silence-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    with lock.open("a+", encoding="utf-8") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _rows(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    output = [row for row in ledger.get("bets") or [] if isinstance(row, dict)]
    for namespace in ("footbreak_crown_execution_test", "crown_hkjc_execution_test"):
        value = ledger.get(namespace)
        if isinstance(value, dict):
            output.extend(row for row in value.get("bets") or [] if isinstance(row, dict))
    return output


def _system_summary(
    name: str,
    ledger_path: Path,
    notify_path: Path,
    start: datetime,
    current: datetime,
) -> dict[str, Any]:
    ledger = _json(ledger_path, {})
    notify = _json(notify_path, {})
    acknowledgements: set[str] = set()
    for key in (
        "wilson_match_alerts",
        "bilateral_decision_alerts",
        "crown_execution_test_alerts",
        "hkjc_execution_test_alerts",
    ):
        acknowledgements.update(str(item) for item in notify.get(key) or [] if str(item))
    recent: list[dict[str, Any]] = []
    pending = 0
    for row in _rows(ledger):
        created = _parse_time(row.get("created_at") or row.get("decision_at") or row.get("ts"))
        if created is None or created < start:
            continue
        identity = str(row.get("bet_id") or row.get("decision_id") or "")
        recent.append(row)
        if identity and identity not in acknowledgements and (current - created).total_seconds() >= 12 * 60:
            pending += 1
    stages = 0
    for watch in (ledger.get("watch") or {}).values():
        if not isinstance(watch, dict):
            continue
        stages += sum(
            1
            for row in watch.get("stages") or []
            if isinstance(row, dict)
            and (_parse_time(row.get("ts") or row.get("source_snapshot_at")) or datetime.min.replace(tzinfo=HKT)) >= start
        )
    return {
        "name": name,
        "formal_signals": len(recent),
        "pending_delivery": pending,
        "stage_activity": stages,
        "last_sent": _parse_time(notify.get("last_sent")),
        "state_readable": bool(ledger),
    }


def _radar_summary(db_path: Path, start: datetime, current: datetime) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": "Odds Radar",
        "formal_signals": 0,
        "pending_delivery": 0,
        "last_sent": None,
        "state_readable": False,
        "providers_healthy": False,
    }
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        start_ms = int(start.timestamp() * 1000)
        bets = connection.execute(
            "SELECT unique_key, placed_at FROM simulation_bets "
            "WHERE placed_at >= ? AND excluded_from_stats=0",
            (start_ms,),
        ).fetchall()
        result["formal_signals"] = len(bets)
        for bet in bets:
            found = connection.execute(
                "SELECT 1 FROM app_state WHERE key=? LIMIT 1",
                (f"telegram_sent:{bet['unique_key']}",),
            ).fetchone()
            if found is None and int(current.timestamp() * 1000) - int(bet["placed_at"]) >= 12 * 60 * 1000:
                result["pending_delivery"] += 1
        last = connection.execute(
            "SELECT MAX(updated_at) AS sent_at FROM app_state WHERE key LIKE 'telegram_sent:%'"
        ).fetchone()
        result["last_sent"] = _parse_time(last["sent_at"] if last else None)
        health = connection.execute(
            "SELECT ok, consecutive_failures FROM provider_health"
        ).fetchall()
        result["providers_healthy"] = bool(health) and all(
            int(row["ok"]) == 1 and int(row["consecutive_failures"]) < 3 for row in health
        )
        result["state_readable"] = True
        connection.close()
    except (OSError, sqlite3.Error, ValueError, TypeError):
        pass
    return result


def _unit_ok(unit: str, runner: Callable[..., Any] = subprocess.run) -> bool:
    try:
        result = runner(
            ["systemctl", "is-active", "--quiet", unit],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return int(getattr(result, "returncode", 1)) == 0


def _service_not_failed(service: str, runner: Callable[..., Any] = subprocess.run) -> bool:
    try:
        result = runner(
            ["systemctl", "is-failed", "--quiet", service],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return int(getattr(result, "returncode", 0)) != 0


def _radar_loopback_healthy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:5001/healthz", timeout=4) as response:
            return 200 <= int(response.status) < 300
    except (OSError, ValueError):
        return False


def _pipeline_faults(
    radar: dict[str, Any],
    runner: Callable[..., Any] = subprocess.run,
) -> list[str]:
    faults: list[str] = []
    timers = [
        ("footbreak-tick.timer", "足破排程"),
        ("footbreak-server-health-monitor.timer", "足破健康監察"),
        ("footbreak-result-reconcile.timer", "賽果整合排程"),
    ]
    if str(os.environ.get("CROWN_ENABLED", "0")).lower() in {"1", "true", "yes", "on"}:
        timers.append(("crown-tick.timer", "皇冠排程"))
    for unit, label in timers:
        if not _unit_ok(unit, runner):
            faults.append(f"{label}未運行")
    for unit, label in (
        ("footbreak-tick.service", "足破服務"),
        ("footbreak-result-reconcile.service", "賽果整合"),
        ("crown-tick.service", "皇冠服務"),
    ):
        if not _service_not_failed(unit, runner):
            faults.append(f"{label}失敗")
    if not radar.get("state_readable"):
        faults.append("Radar資料庫不可讀")
    if not radar.get("providers_healthy"):
        faults.append("Radar供應商健康異常")
    if not _radar_loopback_healthy():
        faults.append("Radar服務健康檢查失敗")
    return faults


def _telegram_sender(text: str) -> bool:
    token = (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("CROWN_TELEGRAM_BOT_TOKEN")
    )
    chat_id = (
        os.environ.get("TELEGRAM_CHAT_ID")
        or os.environ.get("CROWN_TELEGRAM_CHAT_ID")
    )
    if not token or not chat_id:
        return False
    body = json.dumps({
        "chat_id": str(chat_id).strip(),
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{str(token).strip()}/sendMessage",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return 200 <= int(response.status) < 300 and payload.get("ok") is True
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _message(
    classification: str,
    systems: list[dict[str, Any]],
    faults: list[str],
    hours: int,
) -> str:
    if classification == "system_fault":
        heading = "⚠️ TG 靜默監察：系統故障"
        verdict = "判定：並非單純冇訊號，需要檢查伺服器。"
    elif classification == "missed_delivery":
        heading = "⚠️ TG 靜默監察：疑似漏發"
        verdict = "判定：有合資格訊號未見送達確認。"
    else:
        heading = "TG 靜默監察：運作正常"
        verdict = "判定：冇合資格訊號，並非 Telegram 故障。"
    lines = [heading, f"已連續 {hours} 小時冇一般投注通知。"]
    for row in systems:
        status = "正常" if row.get("state_readable") else "資料不可讀"
        lines.append(
            f"- {row['name']}：{status}｜掃描/階段 {row.get('stage_activity', '—')}｜"
            f"正式訊號 {row.get('formal_signals', 0)}｜待送 {row.get('pending_delivery', 0)}"
        )
    if faults:
        lines.append("故障：" + "、".join(faults[:6]))
    lines.extend([verdict, "下次再連續靜默 1 小時先會重複摘要。"])
    return "\n".join(lines)


def _recovery_message(
    previous: str,
    systems: list[dict[str, Any]],
) -> str:
    previous_label = "系統故障" if previous == "system_fault" else "疑似漏發"
    lines = [
        "✅ TG 靜默監察：故障已恢復",
        f"上一狀態：{previous_label}",
    ]
    for row in systems:
        status = "正常" if row.get("state_readable") else "資料不可讀"
        lines.append(
            f"- {row['name']}：{status}｜待送 {row.get('pending_delivery', 0)}"
        )
    lines.extend([
        "判定：排程、資料及通知鏈已回復正常。",
        "恢復通知只會為同一宗事故發一次。",
    ])
    return "\n".join(lines)


def run(
    now: datetime | None = None,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    radar_db: Path = DEFAULT_RADAR_DB,
    sender: Callable[[str], bool] = _telegram_sender,
    runner: Callable[..., Any] = subprocess.run,
    dry_run: bool = False,
) -> dict[str, Any]:
    current = _now(now)
    silence_seconds = max(
        60,
        min(int(os.environ.get("TG_SILENCE_MONITOR_SECONDS", DEFAULT_SILENCE_SECONDS)), 24 * 60 * 60),
    )
    start = current - timedelta(seconds=silence_seconds)
    footbreak = _system_summary("足破", FOOTBREAK_LEDGER, FOOTBREAK_NOTIFY, start, current)
    crown = _system_summary(
        "皇冠",
        Path(os.environ.get("CROWN_LEDGER_PATH", str(CROWN_STATE / "ledger.json"))),
        Path(os.environ.get("CROWN_NOTIFY_STATE_PATH", str(CROWN_STATE / "notify_state.json"))),
        start,
        current,
    )
    radar = _radar_summary(radar_db, start, current)
    systems = [footbreak, crown, radar]
    faults = _pipeline_faults(radar, runner)
    pending = sum(int(row.get("pending_delivery") or 0) for row in systems)
    classification = "system_fault" if faults else "missed_delivery" if pending else "no_signal"

    with _lock(state_path):
        state = _json(state_path, {})
        started = _parse_time(state.get("monitoring_started_at")) or current
        source_activity = [
            row.get("last_sent") for row in systems if isinstance(row.get("last_sent"), datetime)
        ]
        monitor_sent = _parse_time(state.get("last_monitor_sent_at"))
        activity = max(source_activity + ([monitor_sent] if monitor_sent else []) + [started])
        elapsed = max(0, int((current - activity).total_seconds()))
        due = elapsed >= silence_seconds
        previous = str(state.get("last_classification") or "")
        recovery_due = (
            previous in {"system_fault", "missed_delivery"}
            and classification == "no_signal"
            and not faults
            and pending == 0
        )
        result = {
            "classification": classification,
            "due": due,
            "recovery_due": recovery_due,
            "silence_seconds": elapsed,
            "faults": faults,
            "systems": [
                {
                    key: (value.isoformat() if isinstance(value, datetime) else value)
                    for key, value in row.items()
                }
                for row in systems
            ],
            "sent": False,
        }
        if recovery_due:
            text = _recovery_message(previous, systems)
            result["message"] = text
            result["notification_type"] = "recovery"
            if not dry_run and sender(text):
                state["last_monitor_sent_at"] = current.isoformat(timespec="seconds")
                state["last_classification"] = "no_signal"
                state["last_delivery_ok"] = True
                state["last_recovered_at"] = current.isoformat(timespec="seconds")
                result["sent"] = True
            elif not dry_run:
                state["last_delivery_ok"] = False
                state["last_delivery_attempt_at"] = current.isoformat(timespec="seconds")
        elif due:
            text = _message(
                classification,
                systems,
                faults,
                max(1, silence_seconds // 3600),
            )
            result["message"] = text
            result["notification_type"] = "silence_summary"
            if not dry_run and sender(text):
                state["last_monitor_sent_at"] = current.isoformat(timespec="seconds")
                state["last_classification"] = classification
                state["last_delivery_ok"] = True
                result["sent"] = True
            elif not dry_run:
                state["last_delivery_ok"] = False
                state["last_delivery_attempt_at"] = current.isoformat(timespec="seconds")
        state.setdefault("monitoring_started_at", current.isoformat(timespec="seconds"))
        state["last_checked_at"] = current.isoformat(timespec="seconds")
        if not dry_run:
            _atomic_json(state_path, state)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--now", help=argparse.SUPPRESS)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--radar-db", type=Path, default=DEFAULT_RADAR_DB)
    args = parser.parse_args(argv)
    current = _parse_time(args.now) if args.now else None
    result = run(
        current,
        state_path=args.state_path,
        radar_db=args.radar_db,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if args.dry_run or result["sent"] or not result["due"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
