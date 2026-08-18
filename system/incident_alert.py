#!/usr/bin/env python3
"""Bounded, low-noise operational alerts for Footbreak and Crown.

This is intentionally independent of prediction/recommendation notifications.
It sends only sanitised operational facts and never inspects or emits provider
payloads, credentials, betting signals, or recommendation content.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

HKT = timezone(timedelta(hours=8))
STATE_VERSION = 1
STATE_LIMIT = 128
AUDIT_LIMIT = 256
DEFAULT_COOLDOWN_SECONDS = 30 * 60
DEFAULT_T5_LOOKBACK_SECONDS = 6 * 60 * 60
DEFAULT_SETTLEMENT_GRACE_SECONDS = 4 * 60 * 60
_SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_.:@/-]+")


class IncidentAlertError(Exception):
    """A local alerting error that must never break the production job."""


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(os.environ.get(name, default)), maximum))
    except (TypeError, ValueError):
        return default


def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(HKT)
    return value.replace(tzinfo=HKT) if value.tzinfo is None else value.astimezone(HKT)


def _iso(now: datetime | None = None) -> str:
    return _now(now).isoformat(timespec="seconds")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for layout in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, layout)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed.replace(tzinfo=HKT) if parsed.tzinfo is None else parsed.astimezone(HKT)


def _safe_token(value: Any, fallback: str = "unknown") -> str:
    cleaned = _SAFE_TOKEN.sub("-", str(value or "")).strip("-._")
    return (cleaned or fallback)[:96]


def _safe_count(value: Any) -> int:
    try:
        return max(0, min(int(value), 9999))
    except (TypeError, ValueError):
        return 0


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return fallback


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".incident-alert-", dir=path.parent)
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
def _state_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "incidents": {}, "audit": []}


def _normalise_state(value: Any) -> dict[str, Any]:
    state = value if isinstance(value, dict) else {}
    incidents = state.get("incidents") if isinstance(state.get("incidents"), dict) else {}
    audit = state.get("audit") if isinstance(state.get("audit"), list) else []
    return {
        "version": STATE_VERSION,
        "incidents": dict(list(incidents.items())[-STATE_LIMIT:]),
        "audit": [row for row in audit if isinstance(row, dict)][-AUDIT_LIMIT:],
    }


def telegram_sender(system: str) -> Callable[[str], bool]:
    """Use the already-configured direct Telegram transport without exposing it."""
    if system == "crown":
        enabled = _flag("CROWN_TELEGRAM_ENABLED")
        token = os.environ.get("CROWN_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("CROWN_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    else:
        enabled = bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    def send(text: str) -> bool:
        if not (enabled and token and chat_id):
            return False
        body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return bool(payload.get("ok"))
        except Exception:
            return False

    return send


class IncidentAlerts:
    def __init__(
        self,
        state_path: Path | None = None,
        sender: Callable[[str], bool] | None = None,
        cooldown_seconds: int | None = None,
    ) -> None:
        self.state_path = state_path or Path(
            os.environ.get("INCIDENT_ALERT_STATE_PATH", "/var/lib/footbreak/incident-alerts.json")
        )
        # Tests may inject a sender.  Production chooses each system's existing
        # Telegram configuration at report time, keeping Crown separate.
        self.sender = sender
        self.cooldown_seconds = cooldown_seconds or _positive_int(
            "INCIDENT_ALERT_COOLDOWN_SECONDS", DEFAULT_COOLDOWN_SECONDS, 24 * 60 * 60
        )

    @staticmethod
    def message(system: str, kind: str, active: bool, count: int = 0) -> str:
        system_label = "皇冠" if system == "crown" else "足破"
        kind_base = kind.split(":", 1)[0]
        descriptions = {
            "service_failure": "排程／服務執行失敗或逾時",
            "missed_t5": "T-5 必要賽前快照已越過安全時窗仍未保存",
            "source_persistence": "資料來源故障，未能保存可用的必要階段資料",
            "settlement_stuck": "模擬結算超過容許時間仍未完成",
            "health_check_failure": "部署或健康檢查異常",
        }
        description = descriptions.get(kind_base, "運作異常")
        suffix = f"（受影響項目：{_safe_count(count)}）" if active and count else ""
        if active:
            return f"【運作警報：{system_label}】{description}{suffix}。已記錄並會按冷卻時間去重。"
        return f"【運作恢復：{system_label}】{description}已恢復。"

    def report(
        self,
        *,
        system: str,
        kind: str,
        active: bool,
        count: int = 0,
        now: datetime | None = None,
    ) -> bool:
        system = "crown" if system == "crown" else "footbreak"
        kind = _safe_token(kind)
        key = f"{system}:{kind}"
        observed_at = _iso(now)
        epoch = _now(now).timestamp()
        delivered = False
        with _state_lock(self.state_path):
            state = _normalise_state(_read_json(self.state_path, _default_state()))
            incidents = state["incidents"]
            prior = incidents.get(key) if isinstance(incidents.get(key), dict) else {}
            was_active = bool(prior.get("active"))
            last_alert_epoch = float(prior.get("last_alert_epoch") or 0)
            send_now = active and (
                not was_active or epoch - last_alert_epoch >= self.cooldown_seconds
            )
            recovery = not active and was_active
            if send_now or recovery:
                sender = self.sender or telegram_sender(system)
                delivered = bool(sender(self.message(system, kind, active, count)))
            current = {
                "active": bool(active),
                "last_observed_at": observed_at,
                "last_count": _safe_count(count),
                "last_alert_epoch": epoch if send_now else last_alert_epoch,
                "last_transition_at": observed_at if was_active != bool(active) else prior.get("last_transition_at", observed_at),
            }
            incidents[key] = current
            # Keep the most recently observed incidents and a bounded redacted audit.
            state["incidents"] = dict(
                sorted(incidents.items(), key=lambda item: str(item[1].get("last_observed_at", "")))[-STATE_LIMIT:]
            )
            if send_now or recovery:
                state["audit"].append({
                    "at": observed_at,
                    "incident": key,
                    "event": "alert" if active else "recovery",
                    "count": _safe_count(count),
                    "delivered": delivered,
                })
            state["audit"] = state["audit"][-AUDIT_LIMIT:]
            _write_json_atomic(self.state_path, state)
        return delivered


def _stages(watch: dict[str, Any]) -> list[dict[str, Any]]:
    rows = watch.get("stages") if isinstance(watch, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _has_t5_stage(watch: dict[str, Any]) -> bool:
    return any(str(row.get("stage")) == "T-5" for row in _stages(watch))


def _is_unusable_t5_stage(row: dict[str, Any]) -> bool:
    # This only identifies explicit fail-closed source outcomes.  A normal
    # "觀望"/no-pick T-5 record is healthy and must never become an alert.
    if str(row.get("stage")) != "T-5":
        return False
    return (
        str(row.get("source") or "").lower() == "unavailable"
        or str(row.get("source_status") or "") in {
            "analysis_exception", "no_prediction_due_to_source", "source_or_model_unavailable"
        }
    )


def _load_footbreak(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ledger = _read_json(path, {})
    if not isinstance(ledger, dict):
        return [], []
    watch = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    rows = [row for row in watch.values() if isinstance(row, dict)]
    bets = ledger.get("bets") if isinstance(ledger.get("bets"), list) else []
    return rows, [row for row in bets if isinstance(row, dict)]


def _load_crown(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _load_footbreak(path)


def _operational_findings(
    watches: list[dict[str, Any]],
    bets: list[dict[str, Any]],
    now: datetime,
) -> dict[str, int]:
    t5_cutoff = now - timedelta(seconds=_positive_int(
        "INCIDENT_T5_LOOKBACK_SECONDS", DEFAULT_T5_LOOKBACK_SECONDS, 48 * 60 * 60
    ))
    settlement_cutoff = now - timedelta(seconds=_positive_int(
        "INCIDENT_SETTLEMENT_GRACE_SECONDS", DEFAULT_SETTLEMENT_GRACE_SECONDS, 7 * 24 * 60 * 60
    ))
    missed = source_failed = 0
    for watch in watches:
        kickoff = _parse_time(watch.get("kickoff") or watch.get("kickoff_hkt"))
        if not kickoff or not (t5_cutoff <= kickoff <= now):
            continue
        stages = _stages(watch)
        if not _has_t5_stage(watch):
            missed += 1
        elif any(_is_unusable_t5_stage(stage) for stage in stages):
            source_failed += 1
    settlement_stuck = sum(
        1
        for bet in bets
        if str(bet.get("status") or "").upper() == "PENDING"
        and (_parse_time(bet.get("kickoff") or bet.get("kickoff_hkt")) or now) < settlement_cutoff
    )
    return {
        "missed_t5": missed,
        "source_persistence": source_failed,
        "settlement_stuck": settlement_stuck,
    }


def check_ledgers(
    alerts: IncidentAlerts,
    *,
    system: str,
    footbreak_ledger: Path | None = None,
    crown_ledger: Path | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    current = _now(now)
    findings: dict[str, int] = {}
    targets: list[tuple[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]]] = []
    if system in {"footbreak", "all"}:
        path = footbreak_ledger or Path(os.environ.get("FOOTBREAK_LEDGER_PATH", "/opt/footbreak/system/sim_ledger.json"))
        targets.append(("footbreak", _load_footbreak(path)))
    if system in {"crown", "all"}:
        default = Path(os.environ.get("CROWN_STATE_DIR", "/var/lib/footbreak/crown")) / "ledger.json"
        path = crown_ledger or Path(os.environ.get("CROWN_LEDGER_PATH", default))
        targets.append(("crown", _load_crown(path)))
    for label, (watches, bets) in targets:
        for kind, count in _operational_findings(watches, bets, current).items():
            alerts.report(system=label, kind=kind, active=count > 0, count=count, now=current)
            findings[f"{label}:{kind}"] = count
    return findings


def _systemd_expected_preemption(unit: str) -> bool:
    """Do not page for documented exit-75 or T-5 preemption behaviour."""
    safe_unit = _safe_token(unit)
    try:
        output = subprocess.run(
            ["systemctl", "show", safe_unit, "-p", "Result", "-p", "ExecMainStatus", "--value"],
            text=True, capture_output=True, timeout=3, check=False,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return False
    result, status = (output + ["", ""])[:2]
    if status.strip() == "75":
        return True
    if result.strip() != "signal" or status.strip() != "15":
        return False
    timer = safe_unit.removesuffix(".service") + ".timer"
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", timer], timeout=3, check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _alerts_from_args(args: argparse.Namespace) -> IncidentAlerts:
    return IncidentAlerts(Path(args.state) if getattr(args, "state", None) else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Footbreak/Crown operational incident alert helper")
    parser.add_argument("--state", help="private durable alert state path")
    sub = parser.add_subparsers(dest="command", required=True)
    event = sub.add_parser("event")
    event.add_argument("--system", choices=("footbreak", "crown"), required=True)
    event.add_argument("--kind", required=True)
    event.add_argument("--count", type=int, default=1)
    clear = sub.add_parser("clear-service")
    clear.add_argument("--system", choices=("footbreak", "crown"), required=True)
    clear.add_argument("--unit", required=True)
    resolve = sub.add_parser("clear")
    resolve.add_argument("--system", choices=("footbreak", "crown"), required=True)
    resolve.add_argument("--kind", required=True)
    failure = sub.add_parser("systemd-failure")
    failure.add_argument("--unit", required=True)
    failure.add_argument("--system", choices=("footbreak", "crown"), default="footbreak")
    check = sub.add_parser("check")
    check.add_argument("--system", choices=("footbreak", "crown", "all"), default="all")
    check.add_argument("--footbreak-ledger")
    check.add_argument("--crown-ledger")
    args = parser.parse_args(argv)
    alerts = _alerts_from_args(args)
    if args.command == "event":
        alerts.report(system=args.system, kind=args.kind, active=True, count=args.count)
    elif args.command == "clear-service":
        alerts.report(
            system=args.system,
            kind=f"service_failure:{_safe_token(args.unit)}",
            active=False,
        )
    elif args.command == "clear":
        alerts.report(system=args.system, kind=args.kind, active=False)
    elif args.command == "systemd-failure":
        if not _systemd_expected_preemption(args.unit):
            system = "crown" if _safe_token(args.unit).startswith("crown-") else args.system
            alerts.report(
                system=system,
                kind=f"service_failure:{_safe_token(args.unit)}",
                active=True,
            )
    else:
        check_ledgers(
            alerts, system=args.system,
            footbreak_ledger=Path(args.footbreak_ledger) if args.footbreak_ledger else None,
            crown_ledger=Path(args.crown_ledger) if args.crown_ledger else None,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
