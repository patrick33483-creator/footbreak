#!/usr/bin/env python3
"""Read-only, local-only half-hour health monitoring for Footbreak and Crown.

This monitor intentionally reads only persisted state, dashboard artifacts and
systemd's local unit metadata.  It makes no provider request and does not
invoke any prediction, Radar, reconciliation or Telegram recommendation path.
Operational delivery is delegated to the existing ``incident_alert`` helper so
both systems retain their established, separate Telegram configuration and
private atomic incident state.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from incident_alert import IncidentAlerts, _parse_time, _positive_int, _now
from disk_guard import run_maintenance, warning_free_bytes


MONITOR_WINDOW_SECONDS = 30 * 60
STAGE_GRACE_SECONDS = 2 * 60
NOTIFICATION_GRACE_SECONDS = 12 * 60
SETTLEMENT_GRACE_SECONDS = 4 * 60 * 60
ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
SYSTEMS = ("footbreak", "crown")
EXPECTED_STAGES = ("T-30", "T-5")


@dataclass(frozen=True)
class Finding:
    system: str
    kind: str
    count: int = 1


@dataclass(frozen=True)
class Paths:
    ledger: Path
    notify: Path
    dashboard: Path


def _json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return fallback


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


def paths_for(system: str) -> Paths:
    if system == "crown":
        state = _env_path("CROWN_STATE_DIR", "/var/lib/footbreak/crown")
        return Paths(
            ledger=_env_path("CROWN_LEDGER_PATH", str(state / "ledger.json")),
            notify=_env_path("CROWN_NOTIFY_STATE_PATH", str(state / "notify_state.json")),
            dashboard=_env_path("CROWN_DATA", "/var/www/crown/data.json"),
        )
    return Paths(
        ledger=_env_path("FOOTBREAK_LEDGER_PATH", "/opt/footbreak/system/sim_ledger.json"),
        notify=_env_path("FOOTBREAK_NOTIFY_STATE_PATH", "/opt/footbreak/system/notify_state.json"),
        dashboard=_env_path("FOOTBREAK_DATA", "/var/www/footbreak/data.json"),
    )


def _stage_names(watch: dict[str, Any]) -> set[str]:
    return {
        str(row.get("stage") or "")
        for row in (watch.get("stages") or [])
        if isinstance(row, dict)
    }


def _watch_kickoff(watch: dict[str, Any]) -> datetime | None:
    return _parse_time(watch.get("kickoff_hkt") or watch.get("kickoff"))


def _watch_known_at(watch: dict[str, Any], fallback: datetime) -> datetime:
    # A fixture learned after its timed window is not evidence that a native
    # stage was missed.  Legacy rows without discovery time remain observable,
    # but only after the monitor's initial grace.
    return _parse_time(watch.get("discovered_at")) or fallback


def _expected_stage_missing(
    watch: dict[str, Any], stage: str, now: datetime, window_start: datetime,
) -> bool:
    kickoff = _watch_kickoff(watch)
    if kickoff is None or stage in _stage_names(watch):
        return False
    # Every native stage has a bounded due interval.  Evaluate work due during
    # this monitor period, not merely the instant the timer happened to run.
    if stage == "T-30":
        due_start, due_end = kickoff - timedelta(minutes=40), kickoff - timedelta(minutes=20)
    else:
        due_start, due_end = kickoff - timedelta(minutes=10), kickoff
    if due_end < window_start or due_start > now - timedelta(seconds=STAGE_GRACE_SECONDS):
        return False
    return _watch_known_at(watch, window_start) <= due_end


def missing_native_stages(ledger: dict[str, Any], now: datetime) -> int:
    watches = ledger.get("watch") if isinstance(ledger, dict) else {}
    if not isinstance(watches, dict):
        return 0
    window_start = now - timedelta(seconds=MONITOR_WINDOW_SECONDS)
    return sum(
        _expected_stage_missing(watch, stage, now, window_start)
        for watch in watches.values() if isinstance(watch, dict)
        for stage in EXPECTED_STAGES
    )


def _candidate_id(row: dict[str, Any]) -> str:
    return str(row.get("bet_id") or row.get("observation_id") or "").strip()


def _candidate_created_at(row: dict[str, Any]) -> datetime | None:
    for field in ("created_at", "ts", "recorded_at"):
        value = _parse_time(row.get(field))
        if value is not None:
            return value
    return None


def _is_wilson_candidate(system: str, row: dict[str, Any]) -> bool:
    prefix = f"{system}_wilson_"
    if not str(row.get("portfolio") or "").startswith(prefix):
        return False
    # A condition that did not pass historical/sample selection has no
    # candidate row at all.  A low-odds observation is a deliberately healthy
    # no-bet outcome but still has a durable, retryable notification event.
    return (
        str(row.get("strategy") or "") == "wilson-test-strategy-v1"
        or str(row.get("bet_status") or "") == "NO_BET_LOW_ODDS"
    )


def _explicit_transport_backlog(notify: dict[str, Any], now: datetime) -> int:
    """Count only explicit, aged pending/failed outbox rows when a future schema has them.

    Current Footbreak/Crown Wilson delivery is acknowledgement-based, so a
    missing acknowledgement is the authoritative pending signal below.  This
    small compatibility reader also covers a future durable outbox without
    mistaking unrelated notify-state lists for a failed transport.
    """
    rows = notify.get("outbox") or notify.get("notification_outbox") or []
    rows = rows.values() if isinstance(rows, dict) else rows
    if not isinstance(rows, (list, tuple)) and not hasattr(rows, "__iter__"):
        return 0
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or row.get("transport_status") or "").upper()
        created = _candidate_created_at(row)
        if (
            status in {"PENDING", "FAILED", "RETRY", "RETRYING"}
            and created is not None
            and now - created >= timedelta(seconds=NOTIFICATION_GRACE_SECONDS)
        ):
            count += 1
    return count


def stuck_notifications(system: str, ledger: dict[str, Any], notify: dict[str, Any], now: datetime) -> int:
    acknowledged = {
        str(value) for value in (notify.get("wilson_match_alerts") or [])
        if str(value or "").strip()
    }
    if not acknowledged:
        # Accept the prior formal-bet acknowledgement key during an upgrade.
        acknowledged = {
            str(value) for value in (
                notify.get("condition_simulation_bets" if system == "footbreak" else "wilson_bets") or []
            ) if str(value or "").strip()
        }
    rows = list(ledger.get("bets") or [])
    validation = ledger.get("wilson_validation") or {}
    if isinstance(validation, dict):
        rows.extend(validation.get("observations") or [])
    stuck = _explicit_transport_backlog(notify, now)
    for row in rows:
        if not isinstance(row, dict) or not _is_wilson_candidate(system, row):
            continue
        identifier = _candidate_id(row)
        created = _candidate_created_at(row)
        kickoff = _parse_time(row.get("kickoff") or row.get("kickoff_hkt"))
        if (
            not identifier or identifier in acknowledged or created is None
            or now - created < timedelta(seconds=NOTIFICATION_GRACE_SECONDS)
            or (kickoff is not None and kickoff <= now)
        ):
            continue
        # Only committed Wilson bets and explicit low-odds observations enter
        # the transport outbox.  Sample/odds gate rejections without such an
        # event deliberately remain silent.
        stuck += 1
    return stuck


def settlement_backlog(ledger: dict[str, Any], now: datetime) -> int:
    grace = timedelta(seconds=_positive_int(
        "SERVER_HEALTH_SETTLEMENT_GRACE_SECONDS", SETTLEMENT_GRACE_SECONDS, 7 * 24 * 60 * 60,
    ))
    return sum(
        1
        for row in (ledger.get("bets") or [])
        if isinstance(row, dict)
        and str(row.get("status") or "").upper() == "PENDING"
        and (kickoff := _parse_time(row.get("kickoff") or row.get("kickoff_hkt"))) is not None
        and now - kickoff > grace
    )


def dashboard_sidecar_mismatch(system: str, dashboard: Path) -> int:
    payload = _json(dashboard, None)
    if not isinstance(payload, dict):
        return 1
    inline = payload.get("prediction_history")
    if isinstance(inline, dict) and isinstance(inline.get("rows"), list):
        return 0
    name = str(payload.get("history_data_url") or "").strip()
    expected = payload.get("history_data_version")
    if not name or not expected:
        return 1
    sidecar = (dashboard.parent / name).resolve()
    if sidecar.parent != dashboard.parent.resolve():
        return 1
    value = _json(sidecar, None)
    schema = "crown-history-v1" if system == "crown" else "footbreak-history-v1"
    if not isinstance(value, dict) or value.get("schema_version") != schema:
        return 1
    if value.get("history_data_version") != expected:
        return 1
    history = value.get("prediction_history")
    return 0 if isinstance(history, dict) and isinstance(history.get("rows"), list) else 1


def _unit_show(unit: str, runner: Callable[..., Any] = subprocess.run) -> tuple[str, str, str]:
    try:
        completed = runner(
            ["systemctl", "show", unit, "-p", "Result", "-p", "ExecMainStatus", "-p", "ActiveState"],
            text=True, capture_output=True, timeout=1, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown", "unknown", "unknown"
    values: dict[str, str] = {}
    for line in str(completed.stdout).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip()
    return values.get("Result", "unknown"), values.get("ExecMainStatus", "unknown"), values.get("ActiveState", "unknown")


def local_service_findings(system: str, runner: Callable[..., Any] = subprocess.run) -> list[Finding]:
    units = (
        ("footbreak-tick.service", "footbreak-settle.service", "footbreak-result-reconcile.service",
         "footbreak-dashboard-api.service")
        if system == "footbreak"
        else ("crown-tick.service", "crown-sweep.service", "crown-settle.service", "crown-dashboard-api.service")
    )
    timeout_count = failure_count = 0
    for unit in units:
        result, status, active = _unit_show(unit, runner)
        if result == "timeout":
            timeout_count += 1
        # Exit 75 is documented lock/pre-emption behaviour.  In particular,
        # reconcile status=1 is a health signal only; it never implies a
        # missing T-30/T-5 stage.
        elif (result not in {"success", "unknown"} or status not in {"0", "unknown"}) and status != "75":
            failure_count += 1
        elif active == "failed":
            failure_count += 1
    findings: list[Finding] = []
    if timeout_count:
        findings.append(Finding(system, "repeated_timeout", timeout_count))
    if failure_count:
        findings.append(Finding(system, "health_check_failure", failure_count))
    return findings


def assess(system: str, now: datetime, runner: Callable[..., Any] = subprocess.run) -> list[Finding]:
    paths = paths_for(system)
    ledger = _json(paths.ledger, {})
    notify = _json(paths.notify, {})
    findings = [
        Finding(system, "missing_expected_stage", missing_native_stages(ledger, now)),
        Finding(system, "stuck_notification", stuck_notifications(system, ledger, notify, now)),
        Finding(system, "dashboard_sidecar_mismatch", dashboard_sidecar_mismatch(system, paths.dashboard)),
        Finding(system, "settlement_backlog", settlement_backlog(ledger, now)),
    ]
    return [finding for finding in findings if finding.count > 0] + local_service_findings(system, runner)


def run(
    now: datetime | None = None,
    *,
    alerts: IncidentAlerts | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, list[Finding]]:
    current = _now(now)
    alerts = alerts or IncidentAlerts()
    maintenance = run_maintenance()
    active = {system: assess(system, current, runner) for system in SYSTEMS}
    kinds = (
        "missing_expected_stage", "repeated_timeout", "stuck_notification",
        "dashboard_sidecar_mismatch", "settlement_backlog", "health_check_failure",
    )
    cooldown = _positive_int(
        "SERVER_HEALTH_ALERT_COOLDOWN_SECONDS", ALERT_COOLDOWN_SECONDS, 7 * 24 * 60 * 60,
    )
    for system in SYSTEMS:
        by_kind = {finding.kind: finding.count for finding in active[system]}
        for kind in kinds:
            alerts.report(
                system=system, kind=kind, active=kind in by_kind,
                count=by_kind.get(kind, 0), healthy_needed=2,
                positive_needed=2 if kind == "repeated_timeout" else 1,
                cooldown_seconds=cooldown, now=current,
            )
    alerts.report(
        system="server",
        kind="disk_pressure",
        active=maintenance.status.free < warning_free_bytes(),
        count=0,
        positive_needed=1,
        healthy_needed=2,
        cooldown_seconds=cooldown,
        now=current,
    )
    return active


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    when = _parse_time(args.now) if args.now else None
    # The monitor reports its own transitions and always exits cleanly.  A
    # finding must not trigger systemd OnFailure in parallel with its deduped
    # notification flow.
    run(when)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
