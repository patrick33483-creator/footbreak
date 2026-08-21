#!/usr/bin/env python3
"""Private, low-noise operational alerts for Footbreak and Crown.

This component is independent of prediction/recommendation notifications.  It
stores only sanitised category/count state and never emits provider payloads,
credentials, exception details, fixture identifiers, or betting content.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

HKT = timezone(timedelta(hours=8))
STATE_VERSION = 2
STATE_LIMIT = 128
AUDIT_LIMIT = 256
DEFAULT_T5_LOOKBACK_SECONDS = 6 * 60 * 60
DEFAULT_SETTLEMENT_GRACE_SECONDS = 4 * 60 * 60
DEFAULT_SETTLEMENT_ATTEMPT_MAX_AGE_SECONDS = 24 * 60 * 60
LEDGER_POSITIVE_OBSERVATIONS = 2
LEDGER_HEALTHY_OBSERVATIONS = 2
SERVICE_HEALTHY_OBSERVATIONS = 2
SERVICE_FAILURE_OBSERVATIONS = 2
DEFAULT_SERVICE_FAILURE_CONFIRMATION_SECONDS = 36 * 60 * 60
DEFAULT_SERVICE_DUPLICATE_BUCKET_SECONDS = 2 * 60
_SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_.:@/-]+")
_SAFE_DETAIL_KEYS = {"missed_t5", "source_persistence", "settlement_stuck"}
_SERVICE_LABELS = {
    "crown-tick.service": "皇冠 T-30/T-5 臨場預測",
    "crown-sweep.service": "皇冠首預掃描",
    "crown-settle.service": "皇冠結算",
    "footbreak-tick.service": "足破 T-30/T-5 臨場預測",
    "footbreak-sweep.service": "足破首預掃描",
    "footbreak-settle.service": "足破結算",
}


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


def _safe_details(value: Mapping[str, Any] | None) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): _safe_count(count)
        for key, count in value.items()
        if str(key) in _SAFE_DETAIL_KEYS and _safe_count(count) > 0
    }


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
    return {
        "version": STATE_VERSION,
        "monitoring_started_at": None,
        "incidents": {},
        "audit": [],
    }


def _normalise_state(value: Any) -> dict[str, Any]:
    state = value if isinstance(value, dict) else {}
    incidents = state.get("incidents") if isinstance(state.get("incidents"), dict) else {}
    audit = state.get("audit") if isinstance(state.get("audit"), list) else []
    started = _parse_time(state.get("monitoring_started_at"))
    return {
        "version": STATE_VERSION,
        "monitoring_started_at": _iso(started) if started else None,
        "incidents": dict(list(incidents.items())[-STATE_LIMIT:]),
        "audit": [row for row in audit if isinstance(row, dict)][-AUDIT_LIMIT:],
    }


def telegram_sender(system: str) -> Callable[[str], bool]:
    """Use each system's existing direct Telegram configuration."""
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
                return bool(json.loads(response.read().decode("utf-8")).get("ok"))
        except Exception:
            return False

    return send


def service_incident_key(unit: str) -> str:
    """One canonical key shared by shell wrappers and systemd OnFailure."""
    return f"service_failure:{_safe_token(unit)}"


def service_label(kind: str) -> str:
    """Return a safe operator-facing Chinese label without exposing unit IDs."""
    unit = _safe_token(kind.split(":", 1)[1] if ":" in kind else "")
    return _SERVICE_LABELS.get(unit, "已識別排程服務")


def _private_observation_token(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def service_observation_token(
    unit: str,
    invocation: str | None,
    now: datetime | None = None,
    *,
    outcome: str = "failure",
) -> str:
    """Hash an invocation ID, or a short fallback bucket, before persisting it."""
    safe_unit = _safe_token(unit)
    raw_invocation = str(invocation or "").strip()
    if raw_invocation:
        source = f"{safe_unit}:{outcome}:invocation:{raw_invocation}"
    else:
        seconds = _positive_int(
            "INCIDENT_SERVICE_DUPLICATE_BUCKET_SECONDS",
            DEFAULT_SERVICE_DUPLICATE_BUCKET_SECONDS,
            15 * 60,
        )
        source = f"{safe_unit}:{outcome}:bucket:{int(_now(now).timestamp() // seconds)}"
    return _private_observation_token(source) or ""


def _service_confirmation_seconds() -> int:
    return _positive_int(
        "INCIDENT_SERVICE_FAILURE_CONFIRMATION_SECONDS",
        DEFAULT_SERVICE_FAILURE_CONFIRMATION_SECONDS,
        7 * 24 * 60 * 60,
    )


class IncidentAlerts:
    def __init__(
        self,
        state_path: Path | None = None,
        sender: Callable[[str], bool] | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.state_path = state_path or Path(
            os.environ.get("INCIDENT_ALERT_STATE_PATH", "/var/lib/footbreak/incident-alerts.json")
        )
        self.sender = sender
        self.enabled = _flag("INCIDENT_ALERT_ENABLED", True) if enabled is None else bool(enabled)

    @staticmethod
    def message(
        system: str,
        kind: str,
        active: bool,
        count: int = 0,
        details: Mapping[str, Any] | None = None,
        repaired: bool = False,
    ) -> str:
        system_label = (
            "伺服器" if system == "server"
            else "皇冠" if system == "crown"
            else "足破"
        )
        kind_base = kind.split(":", 1)[0]
        descriptions = {
            "service_failure": "排程／服務執行失敗或逾時",
            "health_check_failure": "部署或健康檢查異常",
            "missing_expected_stage": "預期 T-30/T-5 階段未持久化",
            "tick_internal_deadline": "Crown T-5/T-30 tick 內部截止，未確認階段持久化",
            "repeated_timeout": "排程連續逾時",
            "stuck_notification": "通知佇列或傳輸逾時",
            "dashboard_sidecar_mismatch": "儀表板／歷史資料不同步",
            "settlement_backlog": "模擬結算積壓逾時",
            "cross_book_counterpart_evidence": "足破×皇冠 T-5 對手證據缺失或過期",
            "cross_book_unevaluated_t5": "足破×皇冠 T-5 未持久化評估結果",
            "cross_book_first_look_bridge": "足破×皇冠 首預同場身分橋未持久化",
            "cross_book_t30_bridge": "足破×皇冠 T-30 同場／盤口橋未持久化",
            "cross_book_t5_capture": "足破×皇冠 T-5 對照擷取未持久化",
            "disk_pressure": "磁碟可用空間低於安全門檻",
        }
        if kind_base == "ledger_digest":
            if not active:
                return f"【運作恢復：{system_label}】帳本監察異常已連續健康並恢復。"
            labels = {
                "missed_t5": "T-5 快照逾時未保存",
                "source_persistence": "必要資料來源未能保存",
                "settlement_stuck": "模擬結算逾時",
            }
            parts = [
                f"{labels[key]} {_safe_count(value)} 項"
                for key, value in _safe_details(details).items()
                if key in labels
            ]
            return f"【運作警報：{system_label}】帳本監察發現：{'、'.join(parts) or '異常'}。"
        if kind_base == "service_failure":
            label = service_label(kind)
            if active:
                return f"【運作警報：{system_label}】{label}：排程／服務執行失敗或逾時。"
            return f"【運作恢復：{system_label}】{label}：排程／服務已連續健康並恢復。"
        description = descriptions.get(kind_base, "運作異常")
        suffix = f"（受影響項目：{_safe_count(count)}）" if active and count else ""
        if active:
            return f"【運作警報：{system_label}】{description}{suffix}。"
        if repaired:
            return f"【系統自動修復：{system_label}】{description}已自動修復並通過複核。"
        return f"【運作恢復：{system_label}】{description}已連續健康並恢復。"

    def monitoring_started_at(self, now: datetime | None = None) -> tuple[datetime | None, bool]:
        """Create a silent ledger baseline exactly once when monitoring is enabled."""
        if not self.enabled:
            return None, False
        current = _now(now)
        with _state_lock(self.state_path):
            state = _normalise_state(_read_json(self.state_path, _default_state()))
            started = _parse_time(state.get("monitoring_started_at"))
            if started:
                return started, False
            state["monitoring_started_at"] = _iso(current)
            _write_json_atomic(self.state_path, state)
            return current, True

    def report(
        self,
        *,
        system: str,
        kind: str,
        active: bool,
        count: int = 0,
        details: Mapping[str, Any] | None = None,
        positive_needed: int = 1,
        positive_window_seconds: int | None = None,
        healthy_needed: int = 1,
        cooldown_seconds: int | None = None,
        observation_token: str | None = None,
        repaired: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """Record an observation and emit at most one transition notification.

        An active incident never repeats periodically.  Positive/healthy
        observation thresholds provide hysteresis for ledger findings and
        stable service recovery without delaying service-failure alerts.
        """
        if not self.enabled:
            return False
        system = system if system in {"crown", "server"} else "footbreak"
        kind = _safe_token(kind)
        key = f"{system}:{kind}"
        observed_at = _iso(now)
        count = _safe_count(count)
        details = _safe_details(details)
        positive_needed = max(1, int(positive_needed))
        healthy_needed = max(1, int(healthy_needed))
        cooldown_seconds = (
            max(0, int(cooldown_seconds)) if cooldown_seconds is not None else None
        )
        if kind.startswith("service_failure:"):
            positive_needed = max(positive_needed, SERVICE_FAILURE_OBSERVATIONS)
            healthy_needed = max(healthy_needed, SERVICE_HEALTHY_OBSERVATIONS)
            if positive_window_seconds is None:
                positive_window_seconds = _service_confirmation_seconds()
        positive_window_seconds = (
            max(1, int(positive_window_seconds)) if positive_window_seconds is not None else None
        )
        observation_token = _private_observation_token(observation_token)
        delivered = False
        with _state_lock(self.state_path):
            state = _normalise_state(_read_json(self.state_path, _default_state()))
            incidents = state["incidents"]
            prior = incidents.get(key) if isinstance(incidents.get(key), dict) else {}
            was_active = bool(prior.get("active"))
            alert_suppressed = bool(prior.get("alert_suppressed"))
            positive_streak = int(prior.get("positive_streak") or 0)
            healthy_streak = int(prior.get("healthy_streak") or 0)
            if observation_token and observation_token == prior.get("last_observation_digest"):
                # Wrapper EXIT and systemd OnFailure can describe the same
                # failed invocation.  A private token makes it one observation.
                return False
            emit = False
            event = ""
            if active:
                previous_positive = _parse_time(prior.get("last_positive_at"))
                if (
                    positive_window_seconds is not None
                    and previous_positive is not None
                    and (_now(now) - previous_positive).total_seconds() > positive_window_seconds
                ):
                    positive_streak = 0
                positive_streak = min(9999, positive_streak + 1)
                healthy_streak = 0
                current_active = was_active
                if not was_active and positive_streak >= positive_needed:
                    current_active = True
                    last_alert = _parse_time(prior.get("last_alert_at"))
                    cooling_down = bool(
                        cooldown_seconds
                        and last_alert is not None
                        and (_now(now) - last_alert).total_seconds() < cooldown_seconds
                    )
                    if not cooling_down:
                        emit, event = True, "alert"
                        alert_suppressed = False
                    else:
                        alert_suppressed = True
                elif (
                    was_active
                    and alert_suppressed
                    and cooldown_seconds
                    and (last_alert := _parse_time(prior.get("last_alert_at"))) is not None
                    and (_now(now) - last_alert).total_seconds() >= cooldown_seconds
                ):
                    # A brief clear/reopen inside cooldown is intentionally
                    # quiet.  If it remains unhealthy past cooldown, emit one
                    # delayed transition rather than leaving it invisible.
                    emit, event = True, "alert"
                    alert_suppressed = False
            else:
                positive_streak = 0
                healthy_streak = min(9999, healthy_streak + 1)
                current_active = was_active
                if was_active and healthy_streak >= healthy_needed:
                    current_active = False
                    emit, event = True, "recovery"
                    alert_suppressed = False
                elif repaired and not was_active:
                    # Repairs are noteworthy even when the incident was caught
                    # before a normal alert was emitted.  The monitor supplies
                    # this only after a fresh local re-audit is healthy.
                    last_repair = _parse_time(prior.get("last_repair_at"))
                    cooling_down = bool(
                        cooldown_seconds
                        and last_repair is not None
                        and (_now(now) - last_repair).total_seconds() < cooldown_seconds
                    )
                    if not cooling_down:
                        emit, event = True, "repair_recovery"
            if emit:
                sender = self.sender or telegram_sender(system)
                delivered = bool(sender(self.message(
                    system, kind, current_active, count, details, repaired=event == "repair_recovery",
                )))
            incidents[key] = {
                "active": current_active,
                "positive_streak": positive_streak,
                "healthy_streak": healthy_streak,
                "last_observed_at": observed_at,
                "last_count": count,
                "last_details": details,
                "last_transition_at": observed_at if emit else prior.get("last_transition_at"),
                "last_alert_at": (
                    observed_at if emit and event == "alert" else prior.get("last_alert_at")
                ),
                "last_repair_at": (
                    observed_at if emit and event == "repair_recovery" else prior.get("last_repair_at")
                ),
                "alert_suppressed": alert_suppressed,
                "last_positive_at": observed_at if active else prior.get("last_positive_at"),
                "last_observation_digest": observation_token,
            }
            state["incidents"] = dict(
                sorted(incidents.items(), key=lambda item: str(item[1].get("last_observed_at", "")))[-STATE_LIMIT:]
            )
            if emit:
                state["audit"].append({
                    "at": observed_at,
                    "incident": key,
                    "event": event,
                    "count": count,
                    "details": details,
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
    # A normal "觀望"/no-pick T-5 is healthy and cannot become an incident.
    if str(row.get("stage")) != "T-5":
        return False
    return (
        str(row.get("source") or "").lower() == "unavailable"
        or str(row.get("source_status") or "") in {
            "analysis_exception", "no_prediction_due_to_source", "source_or_model_unavailable"
        }
    )


def _stage_time(row: dict[str, Any]) -> datetime | None:
    return _parse_time(
        row.get("ts") or row.get("captured_at") or row.get("created_at") or row.get("observed_at")
    )


def _load_footbreak(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ledger = _read_json(path, {})
    if not isinstance(ledger, dict):
        return [], []
    watch = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    rows = [row for row in watch.values() if isinstance(row, dict)]
    bets = ledger.get("bets") if isinstance(ledger.get("bets"), list) else []
    return rows, [row for row in bets if isinstance(row, dict)]


def _operational_findings(
    watches: list[dict[str, Any]],
    bets: list[dict[str, Any]],
    monitoring_started_at: datetime,
    now: datetime,
) -> dict[str, int]:
    t5_cutoff = max(
        monitoring_started_at,
        now - timedelta(seconds=_positive_int(
            "INCIDENT_T5_LOOKBACK_SECONDS", DEFAULT_T5_LOOKBACK_SECONDS, 48 * 60 * 60
        )),
    )
    settlement_grace = timedelta(seconds=_positive_int(
        "INCIDENT_SETTLEMENT_GRACE_SECONDS", DEFAULT_SETTLEMENT_GRACE_SECONDS, 7 * 24 * 60 * 60
    ))
    attempt_max_age = timedelta(seconds=_positive_int(
        "INCIDENT_SETTLEMENT_ATTEMPT_MAX_AGE_SECONDS", DEFAULT_SETTLEMENT_ATTEMPT_MAX_AGE_SECONDS, 14 * 24 * 60 * 60
    ))
    missed = source_failed = 0
    for watch in watches:
        kickoff = _parse_time(watch.get("kickoff") or watch.get("kickoff_hkt"))
        # Existing/deployed backlog is not actionable.  Only fixtures which
        # crossed their T-5 window after monitoring began can be considered.
        if not kickoff or not (t5_cutoff < kickoff <= now):
            continue
        stages = _stages(watch)
        if not _has_t5_stage(watch):
            missed += 1
            continue
        if any(
            _is_unusable_t5_stage(stage)
            and (stage_at := _stage_time(stage)) is not None
            and monitoring_started_at < stage_at <= now
            for stage in stages
        ):
            source_failed += 1
    settlement_stuck = 0
    for bet in bets:
        if str(bet.get("status") or "").upper() != "PENDING":
            continue
        attempt = _parse_time(bet.get("last_settlement_attempt_at"))
        # Kickoff age alone is intentionally insufficient: the operator needs
        # proof a real post-start settlement pass attempted this pending row.
        if not attempt or not (monitoring_started_at < attempt <= now):
            continue
        age = now - attempt
        if settlement_grace <= age <= attempt_max_age:
            settlement_stuck += 1
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
    monitoring_started_at, baseline = alerts.monitoring_started_at(current)
    if not alerts.enabled or monitoring_started_at is None:
        return {}
    targets: list[tuple[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]]] = []
    if system in {"footbreak", "all"}:
        path = footbreak_ledger or Path(os.environ.get("FOOTBREAK_LEDGER_PATH", "/opt/footbreak/system/sim_ledger.json"))
        targets.append(("footbreak", _load_footbreak(path)))
    if system in {"crown", "all"}:
        default = Path(os.environ.get("CROWN_STATE_DIR", "/var/lib/footbreak/crown")) / "ledger.json"
        path = crown_ledger or Path(os.environ.get("CROWN_LEDGER_PATH", default))
        targets.append(("crown", _load_footbreak(path)))
    findings: dict[str, int] = {}
    for label, (watches, bets) in targets:
        grouped = _operational_findings(watches, bets, monitoring_started_at, current)
        findings.update({f"{label}:{kind}": count for kind, count in grouped.items()})
        # First observed ledger state is a silent baseline.  It must not alert
        # or resolve historical conditions even if they look overdue.
        if baseline:
            continue
        alerts.report(
            system=label,
            kind="ledger_digest",
            active=any(grouped.values()),
            count=sum(grouped.values()),
            details=grouped,
            positive_needed=LEDGER_POSITIVE_OBSERVATIONS,
            healthy_needed=LEDGER_HEALTHY_OBSERVATIONS,
            now=current,
        )
    return findings


def report_service_failure(
    alerts: IncidentAlerts,
    *,
    system: str,
    unit: str,
    invocation: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Record one confirmed service failure through every runtime entry point."""
    return alerts.report(
        system=system,
        kind=service_incident_key(unit),
        active=True,
        positive_needed=SERVICE_FAILURE_OBSERVATIONS,
        positive_window_seconds=_service_confirmation_seconds(),
        observation_token=service_observation_token(unit, invocation, now, outcome="failure"),
        now=now,
    )


def report_service_success(
    alerts: IncidentAlerts,
    *,
    system: str,
    unit: str,
    invocation: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Reset provisional failure state or recover an already-confirmed incident."""
    return alerts.report(
        system=system,
        kind=service_incident_key(unit),
        active=False,
        healthy_needed=SERVICE_HEALTHY_OBSERVATIONS,
        observation_token=service_observation_token(unit, invocation, now, outcome="success"),
        now=now,
    )


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


def _systemd_invocation_id(unit: str) -> str | None:
    """Read only the failed unit's opaque invocation marker for deduplication."""
    try:
        marker = subprocess.run(
            ["systemctl", "show", _safe_token(unit), "-p", "InvocationID", "--value"],
            text=True, capture_output=True, timeout=3, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return marker if marker and marker.lower() not in {"n/a", "not-found"} else None


def _alerts_from_args(args: argparse.Namespace) -> IncidentAlerts:
    return IncidentAlerts(Path(args.state) if getattr(args, "state", None) else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Footbreak/Crown operational incident alert helper")
    parser.add_argument("--state", help="private durable alert state path")
    sub = parser.add_subparsers(dest="command", required=True)
    event = sub.add_parser("event")
    event.add_argument("--system", choices=("footbreak", "crown"), required=True)
    event.add_argument("--kind")
    event.add_argument("--unit")
    event.add_argument("--invocation")
    event.add_argument("--count", type=int, default=1)
    clear = sub.add_parser("clear-service")
    clear.add_argument("--system", choices=("footbreak", "crown"), required=True)
    clear.add_argument("--unit", required=True)
    clear.add_argument("--invocation")
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
        if not args.kind and not args.unit:
            parser.error("event requires --kind or --unit")
        if args.unit:
            report_service_failure(
                alerts, system=args.system, unit=args.unit, invocation=args.invocation,
            )
        else:
            alerts.report(system=args.system, kind=args.kind, active=True, count=args.count)
    elif args.command == "clear-service":
        report_service_success(
            alerts, system=args.system, unit=args.unit, invocation=args.invocation,
        )
    elif args.command == "clear":
        alerts.report(
            system=args.system,
            kind=args.kind,
            active=False,
            healthy_needed=SERVICE_HEALTHY_OBSERVATIONS if args.kind == "health_check_failure" else 1,
        )
    elif args.command == "systemd-failure":
        if not _systemd_expected_preemption(args.unit):
            system = "crown" if _safe_token(args.unit).startswith("crown-") else args.system
            report_service_failure(
                alerts,
                system=system,
                unit=args.unit,
                invocation=_systemd_invocation_id(args.unit),
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
