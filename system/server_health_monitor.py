#!/usr/bin/env python3
"""Bounded local half-hour health monitoring and repair for Footbreak and Crown.

The normal path reads persisted state, dashboard artifacts and systemd metadata.
For a detected fault it makes at most one safe local repair, then re-audits
before alerting.  A normal bounded tick may be started only for a genuinely due,
pre-kickoff native stage; all other repairs are provider-free.  It never
backfills stages, touches settled evidence, places bets, or removes lock files.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from incident_alert import IncidentAlerts, _parse_time, _positive_int, _now
from disk_guard import run_maintenance, warning_free_bytes
from cross_book_evidence_repair import rebuild as rebuild_cross_book_evidence


MONITOR_WINDOW_SECONDS = 30 * 60
STAGE_GRACE_SECONDS = 2 * 60
NOTIFICATION_GRACE_SECONDS = 12 * 60
SETTLEMENT_GRACE_SECONDS = 4 * 60 * 60
ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
CROSS_BOOK_AUDIT_GRACE_SECONDS = 2 * 60
CROSS_BOOK_FRESHNESS_SECONDS = 120
REPAIR_COOLDOWN_SECONDS = 30 * 60
REPAIR_MAX_ATTEMPTS = 2
REPAIR_WINDOW_SECONDS = 6 * 60 * 60
REPAIR_TICK_TIMEOUT_SECONDS = 60
REPAIR_AUDIT_LIMIT = 128
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
    tick_health: Path | None = None


@dataclass(frozen=True)
class RepairAction:
    system: str
    kind: str
    action: str
    attempted: bool
    succeeded: bool


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
            tick_health=_env_path("CROWN_TICK_HEALTH_PATH", str(state / "tick-health.json")),
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


def _cross_book_evidence_path() -> Path:
    explicit = os.environ.get("FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH")
    if explicit:
        return Path(explicit)
    state = _env_path("CROWN_STATE_DIR", "/var/lib/footbreak/crown")
    return state / "footbreak-execution-evidence.json"


def _cross_book_freshness_seconds() -> int:
    return _positive_int(
        "FOOTBREAK_CROWN_EXECUTION_MAX_AGE_SECONDS",
        CROSS_BOOK_FRESHNESS_SECONDS,
        600,
    )


def _same_kickoff(left: datetime | None, right: datetime | None) -> bool:
    return bool(left and right and abs((left - right).total_seconds()) <= 1)


def _due_t5_stages(ledger: dict[str, Any], now: datetime) -> list[tuple[str, datetime, datetime]]:
    """Return only persisted T-5 decisions that became due this monitor period.

    The monitor must not alert merely because the sidecar has no active cards.
    A counterpart check is meaningful only after Footbreak has durably recorded
    a native T-5 stage, and the short bounded window prevents stale historical
    cards from being mistaken for a current incident.
    """
    watches = ledger.get("watch") if isinstance(ledger, dict) else {}
    if not isinstance(watches, dict):
        return []
    start = now - timedelta(seconds=MONITOR_WINDOW_SECONDS + STAGE_GRACE_SECONDS)
    due: list[tuple[str, datetime, datetime]] = []
    for key, watch in watches.items():
        if not isinstance(watch, dict):
            continue
        fixture = str(watch.get("match_id") or key or "").strip()
        kickoff = _watch_kickoff(watch)
        if not fixture or kickoff is None:
            continue
        for stage in watch.get("stages") or []:
            if not isinstance(stage, dict) or stage.get("stage") != "T-5":
                continue
            recorded = _parse_time(stage.get("ts"))
            if recorded is not None and start <= recorded <= now:
                due.append((fixture, kickoff, recorded))
    return due


def _cross_book_has_fresh_quote(
    cards: list[dict[str, Any]],
    fixture: str,
    kickoff: datetime,
    decision_at: datetime,
) -> bool:
    exact_cards = [
        card for card in cards
        if str(card.get("hkjc_match_id") or "").strip() == fixture
        and _same_kickoff(_parse_time(card.get("kickoff_hkt") or card.get("kickoff")), kickoff)
    ]
    # The execution reader refuses a missing or ambiguous identity.  Apply the
    # same fail-closed rule in health coverage, without interpreting odds or
    # contacting Crown.
    if len(exact_cards) != 1:
        return False
    journal = exact_cards[0].get("current_selected_odds_journal")
    if not isinstance(journal, list):
        return False
    freshness = _cross_book_freshness_seconds()
    return any(
        isinstance(quote, dict)
        and str(quote.get("odds_status") or "available") == "available"
        and (observed := _parse_time(quote.get("observed_at"))) is not None
        and observed <= decision_at
        and (decision_at - observed).total_seconds() <= freshness
        for quote in journal
    )


def cross_book_t5_findings(ledger: dict[str, Any], now: datetime) -> list[Finding]:
    """Audit only due Footbreak T-5 handoffs using local persisted records.

    Each due T-5 must have (1) one exact Crown sidecar card with at least one
    fresh native quote at that decision time and (2) a persisted cross-book
    outcome.  The outcome can be an entry, no-bet, or explicit rejection; it is
    the durable evidence that a candidate was not silently dropped.
    """
    due = _due_t5_stages(ledger, now)
    if not due:
        return []
    evidence = _json(_cross_book_evidence_path(), [])
    cards = [card for card in evidence if isinstance(card, dict)] if isinstance(evidence, list) else []
    namespace = ledger.get("footbreak_crown_execution_test") if isinstance(ledger, dict) else {}
    audit = namespace.get("audit") if isinstance(namespace, dict) else []
    audit = [row for row in audit if isinstance(row, dict)] if isinstance(audit, list) else []
    missing_evidence = unevaluated = 0
    for fixture, kickoff, decision_at in due:
        if not _cross_book_has_fresh_quote(cards, fixture, kickoff, decision_at):
            missing_evidence += 1
        outcome_start = decision_at - timedelta(seconds=CROSS_BOOK_AUDIT_GRACE_SECONDS)
        if not any(
            str(row.get("match_id") or "") == fixture
            and (recorded := _parse_time(row.get("ts"))) is not None
            and outcome_start <= recorded <= now
            for row in audit
        ):
            unevaluated += 1
    findings: list[Finding] = []
    if missing_evidence:
        findings.append(Finding("footbreak", "cross_book_counterpart_evidence", missing_evidence))
    if unevaluated:
        findings.append(Finding("footbreak", "cross_book_unevaluated_t5", unevaluated))
    return findings


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


def tick_internal_deadline(ledger: dict[str, Any], health: dict[str, Any], now: datetime) -> int:
    """Detect a recent internal Crown tick deadline while work is still due.

    A bounded tick deliberately exits 0 when it protects its teardown margin.
    That is not a healthy outcome if a pre-kickoff native stage remains due.
    The durable marker is overwritten by each tick, so no historical warning
    can trigger recovery or an alert after the fixture has started.
    """
    if str(health.get("engine_warning") or "") != "deferred_tick_deadline":
        return 0
    recorded = _parse_time(health.get("at"))
    if recorded is None or recorded > now:
        return 0
    freshness = _positive_int("CROWN_TICK_HEALTH_MAX_AGE_SECONDS", 125, 600)
    if (now - recorded).total_seconds() > freshness:
        return 0
    return int(_due_unfinished_stage("crown", ledger, now))


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


def _repair_state_path() -> Path:
    return Path(os.environ.get(
        "SERVER_HEALTH_REPAIR_STATE_PATH",
        "/var/lib/footbreak/server-health-repairs.json",
    ))


def _repair_state_default() -> dict[str, Any]:
    return {"version": 1, "incidents": {}, "audit": []}


def _repair_state_load(path: Path) -> dict[str, Any]:
    value = _json(path, _repair_state_default())
    if not isinstance(value, dict):
        return _repair_state_default()
    incidents = value.get("incidents") if isinstance(value.get("incidents"), dict) else {}
    audit = value.get("audit") if isinstance(value.get("audit"), list) else []
    return {"version": 1, "incidents": incidents, "audit": [row for row in audit if isinstance(row, dict)]}


def _repair_state_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".server-health-repair-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _repair_state_update(
    path: Path,
    *,
    system: str,
    kind: str,
    action: str,
    succeeded: bool,
    now: datetime,
) -> None:
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            state = _repair_state_load(path)
            key = f"{system}:{kind}"
            prior = state["incidents"].get(key) if isinstance(state["incidents"].get(key), dict) else {}
            previous = _parse_time(prior.get("last_attempt_at"))
            window = _positive_int(
                "SERVER_HEALTH_REPAIR_WINDOW_SECONDS", REPAIR_WINDOW_SECONDS, 7 * 24 * 60 * 60,
            )
            attempts = (
                max(0, min(int(prior.get("attempts") or 0), 9999))
                if previous is not None and (_now(now) - previous).total_seconds() <= window
                else 0
            )
            state["incidents"][key] = {
                "attempts": attempts + 1,
                "last_attempt_at": _now(now).isoformat(timespec="seconds"),
                "last_action": action,
                "last_succeeded": bool(succeeded),
            }
            state["audit"].append({
                "at": _now(now).isoformat(timespec="seconds"),
                "incident": key,
                "action": action,
                "succeeded": bool(succeeded),
            })
            state["audit"] = state["audit"][-REPAIR_AUDIT_LIMIT:]
            _repair_state_write(path, state)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _repair_allowed(path: Path, system: str, kind: str, now: datetime) -> bool:
    state = _repair_state_load(path)
    prior = state.get("incidents", {}).get(f"{system}:{kind}")
    if not isinstance(prior, dict):
        return True
    last = _parse_time(prior.get("last_attempt_at"))
    if last is None:
        return True
    current = _now(now)
    cooldown = _positive_int(
        "SERVER_HEALTH_REPAIR_COOLDOWN_SECONDS", REPAIR_COOLDOWN_SECONDS, 24 * 60 * 60,
    )
    if (current - last).total_seconds() < cooldown:
        return False
    window = _positive_int(
        "SERVER_HEALTH_REPAIR_WINDOW_SECONDS", REPAIR_WINDOW_SECONDS, 7 * 24 * 60 * 60,
    )
    attempts = max(0, int(prior.get("attempts") or 0))
    maximum = _positive_int("SERVER_HEALTH_REPAIR_MAX_ATTEMPTS", REPAIR_MAX_ATTEMPTS, 10)
    if (current - last).total_seconds() <= window and attempts >= maximum:
        return False
    return True


def _due_unfinished_stage(system: str, ledger: dict[str, Any], now: datetime) -> bool:
    """Whether a normal bounded tick is still allowed to repair a native stage."""
    watches = ledger.get("watch") if isinstance(ledger, dict) else {}
    if not isinstance(watches, dict):
        return False
    for watch in watches.values():
        if not isinstance(watch, dict):
            continue
        kickoff = _watch_kickoff(watch)
        if kickoff is None or kickoff <= now:
            continue
        minutes = (kickoff - now).total_seconds() / 60.0
        stages = watch.get("stages") if isinstance(watch.get("stages"), list) else []

        def complete(name: str) -> bool:
            return any(
                isinstance(row, dict)
                and row.get("stage") == name
                and (system != "crown" or row.get("status") != "DATA_MISSING")
                for row in stages
            )

        if (0.0 < minutes <= 10.5 and not complete("T-5")) or (
            20.0 <= minutes <= 40.5 and not complete("T-30")
        ):
            return True
    return False


def _timer_units(system: str) -> tuple[str, ...]:
    return (
        (
            "footbreak-tick.timer", "footbreak-sweep.timer", "footbreak-settle.timer",
            "footbreak-result-reconcile.timer", "footbreak-dashboard-self-heal.timer",
            "footbreak-server-health-monitor.timer",
        )
        if system == "footbreak"
        else ("crown-tick.timer", "crown-sweep.timer", "crown-settle.timer")
    )


def _service_units(system: str) -> tuple[str, ...]:
    return (
        (
            "footbreak-tick.service", "footbreak-sweep.service", "footbreak-settle.service",
            "footbreak-result-reconcile.service", "footbreak-dashboard-api.service",
            "footbreak-dashboard-self-heal.service",
        )
        if system == "footbreak"
        else (
            "crown-tick.service", "crown-sweep.service", "crown-settle.service",
            "crown-dashboard-api.service",
        )
    )


class RepairController:
    """One bounded local repair attempt per incident; never clears lock files."""

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        runner: Callable[..., Any] = subprocess.run,
        evidence_rebuilder: Callable[..., bool] = rebuild_cross_book_evidence,
    ) -> None:
        self.state_path = state_path or _repair_state_path()
        self.runner = runner
        self.evidence_rebuilder = evidence_rebuilder

    def _run(self, command: list[str], timeout: int = 12) -> bool:
        try:
            result = self.runner(command, text=True, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return int(getattr(result, "returncode", 1)) == 0

    def _repair_timers(self, system: str) -> tuple[bool, bool]:
        attempted = False
        succeeded = True
        for timer in _timer_units(system):
            enabled = self._run(["systemctl", "is-enabled", "--quiet", timer])
            active = self._run(["systemctl", "is-active", "--quiet", timer])
            if enabled and active:
                continue
            attempted = True
            succeeded = self._run(["systemctl", "unmask", timer]) and succeeded
            succeeded = self._run(["systemctl", "enable", timer]) and succeeded
            succeeded = self._run(["systemctl", "restart", timer]) and succeeded
        return attempted, attempted and succeeded

    def _repair_failed_services(self, system: str) -> tuple[bool, bool]:
        """Restart only units systemd itself identifies as failed.

        Oneshoot workers are commonly inactive between their normal timer
        invocations, so ``is-active`` would be unsafe here.  ``is-failed`` is
        the narrow evidence required for an automated service restart.
        """
        attempted = False
        succeeded = True
        for service in _service_units(system):
            if not self._run(["systemctl", "is-failed", "--quiet", service]):
                continue
            attempted = True
            succeeded = self._run(["systemctl", "restart", service]) and succeeded
        return attempted, attempted and succeeded

    def _start_due_tick(self, system: str, ledger: dict[str, Any], now: datetime) -> tuple[bool, bool]:
        if not _due_unfinished_stage(system, ledger, now):
            return False, False
        unit = f"{system}-tick.service"
        result, _status, active = _unit_show(unit, self.runner)
        # A live runner owns any lock it uses; never kill it or remove a lock
        # from the monitor.  Its normal bounded timeout and the timer retry are
        # the safe recovery path.
        if active in {"active", "activating"} or result == "timeout":
            return False, False
        return True, self._run(
            ["systemctl", "start", unit],
            timeout=_positive_int(
                "SERVER_HEALTH_REPAIR_TICK_TIMEOUT_SECONDS",
                REPAIR_TICK_TIMEOUT_SECONDS,
                90,
            ),
        )

    def attempt(
        self,
        finding: Finding,
        *,
        ledgers: dict[str, dict[str, Any]],
        now: datetime,
    ) -> RepairAction:
        if not _repair_allowed(self.state_path, finding.system, finding.kind, now):
            return RepairAction(finding.system, finding.kind, "cooldown_or_attempt_cap", False, False)
        action = "no_safe_repair"
        succeeded = False
        attempted = True
        if finding.kind == "dashboard_sidecar_mismatch":
            action = "dashboard_self_heal"
            succeeded = self._run(["systemctl", "start", "footbreak-dashboard-self-heal.service"], timeout=50)
        elif finding.kind == "cross_book_counterpart_evidence":
            action = "rebuild_cross_book_evidence"
            succeeded = bool(self.evidence_rebuilder(now=now))
        elif finding.kind in {"missing_expected_stage", "tick_internal_deadline", "health_check_failure", "repeated_timeout"}:
            action = "repair_timers"
            timer_attempted, succeeded = self._repair_timers(finding.system)
            service_attempted = False
            if not timer_attempted and finding.kind in {"health_check_failure", "repeated_timeout"}:
                action = "restart_failed_services"
                service_attempted, succeeded = self._repair_failed_services(finding.system)
            if not timer_attempted and not service_attempted:
                action = "due_normal_tick"
                attempted, succeeded = self._start_due_tick(finding.system, ledgers.get(finding.system, {}), now)
        elif finding.kind == "cross_book_unevaluated_t5":
            action = "due_footbreak_tick"
            attempted, succeeded = self._start_due_tick("footbreak", ledgers.get("footbreak", {}), now)
        if action == "no_safe_repair" or not attempted:
            return RepairAction(finding.system, finding.kind, action, False, False)
        _repair_state_update(
            self.state_path, system=finding.system, kind=finding.kind,
            action=action, succeeded=succeeded, now=now,
        )
        return RepairAction(finding.system, finding.kind, action, True, succeeded)


def assess(system: str, now: datetime, runner: Callable[..., Any] = subprocess.run) -> list[Finding]:
    paths = paths_for(system)
    ledger = _json(paths.ledger, {})
    notify = _json(paths.notify, {})
    tick_health = _json(paths.tick_health, {}) if system == "crown" and paths.tick_health else {}
    findings = [
        Finding(system, "missing_expected_stage", missing_native_stages(ledger, now)),
        Finding(system, "tick_internal_deadline", tick_internal_deadline(ledger, tick_health, now))
        if isinstance(tick_health, dict) else Finding(system, "tick_internal_deadline", 0),
        Finding(system, "stuck_notification", stuck_notifications(system, ledger, notify, now)),
        Finding(system, "dashboard_sidecar_mismatch", dashboard_sidecar_mismatch(system, paths.dashboard)),
        Finding(system, "settlement_backlog", settlement_backlog(ledger, now)),
    ]
    findings = [finding for finding in findings if finding.count > 0] + local_service_findings(system, runner)
    if system == "footbreak":
        findings.extend(cross_book_t5_findings(ledger, now))
    return findings


def run(
    now: datetime | None = None,
    *,
    alerts: IncidentAlerts | None = None,
    runner: Callable[..., Any] = subprocess.run,
    repairs: RepairController | None = None,
    repair_enabled: bool = True,
) -> dict[str, list[Finding]]:
    current = _now(now)
    alerts = alerts or IncidentAlerts()
    maintenance = run_maintenance()
    initial = {system: assess(system, current, runner) for system in SYSTEMS}
    ledgers = {system: _json(paths_for(system).ledger, {}) for system in SYSTEMS}
    controller = repairs or RepairController(runner=runner)
    actions: list[RepairAction] = []
    if repair_enabled:
        for system in SYSTEMS:
            for finding in initial[system]:
                actions.append(controller.attempt(finding, ledgers=ledgers, now=current))
    # A repair attempt is always followed by a local re-audit, including a
    # failed action.  This prevents an alert about a condition that another
    # service happened to clear while the repair was being attempted.
    active = (
        {system: assess(system, current, runner) for system in SYSTEMS}
        if any(action.attempted for action in actions)
        else initial
    )
    repaired = {
        (action.system, action.kind)
        for action in actions
        if action.attempted
        and action.succeeded
        and action.kind not in {finding.kind for finding in active[action.system]}
    }
    kinds = (
        "missing_expected_stage", "tick_internal_deadline", "repeated_timeout", "stuck_notification",
        "dashboard_sidecar_mismatch", "settlement_backlog", "health_check_failure",
        "cross_book_counterpart_evidence", "cross_book_unevaluated_t5",
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
                repaired=(system, kind) in repaired,
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
