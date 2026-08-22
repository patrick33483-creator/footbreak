#!/usr/bin/env python3
"""Provider-free read-only evidence for the Crown native core live proofs."""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from crown.common import HKT, parse_time
from analysis.wilson_validation import ROLLOVER_BATCH_SIZE, _eligible_rollover_rows


LEDGER = Path("/var/lib/footbreak/crown/ledger.json")
PREDICTIONS = Path("/var/lib/footbreak/crown/predictions.json")


def read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return default
    return value if isinstance(value, type(default)) else default


def systemctl(units: list[str]) -> str:
    fields = [
        "Id", "LoadState", "UnitFileState", "ActiveState", "SubState", "Result",
        "ExecMainStatus", "ExecMainStartTimestamp", "ExecMainExitTimestamp",
        "LastTriggerUSec", "NextElapseUSecRealtime", "TimeoutStartUSec",
    ]
    completed = subprocess.run(
        ["systemctl", "show", *units, "--no-pager", *sum((["-p", key] for key in fields), [])],
        text=True, capture_output=True, check=False, timeout=20,
    )
    return completed.stdout[-14000:]


def journal_summary() -> dict[str, Any]:
    completed = subprocess.run(
        [
            "journalctl", "-u", "crown-first-look-reconcile.service",
            "--since", "-180 minutes", "--no-pager", "-o", "cat",
        ],
        text=True, capture_output=True, check=False, timeout=20,
    )
    raw = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    # Keep only bounded operational facts; no provider payload is emitted.
    interesting = [
        line[-500:] for line in raw
        if any(token in line.lower() for token in (
            "first-look", "reconcile", "native", "data_missing", "failed",
            "timeout", "reconciled_first_look", "provider_status",
        ))
    ][-30:]
    return {
        "journal_line_count": len(raw),
        "interesting_tail": interesting,
    }


def stage_status(watch: dict[str, Any], stage: str) -> dict[str, Any]:
    jobs = watch.get("stage_jobs") if isinstance(watch.get("stage_jobs"), dict) else {}
    attempts = watch.get("stage_attempts") if isinstance(watch.get("stage_attempts"), dict) else {}
    snapshot = next((
        value for value in watch.get("stages") or []
        if isinstance(value, dict) and str(value.get("stage") or "") == stage
    ), {})
    job = jobs.get(stage) if isinstance(jobs.get(stage), dict) else {}
    attempt = attempts.get(stage) if isinstance(attempts.get(stage), dict) else {}
    return {
        "job": {key: job.get(key) for key in ("due_at_utc", "due_at_hkt", "state", "updated_at", "reason")},
        "attempt": {key: attempt.get(key) for key in ("state", "started_at", "updated_at", "reason", "source")},
        "snapshot": {
            key: snapshot.get(key) for key in (
                "stage", "status", "ts", "observed_at", "source_snapshot_at",
                "crown_quote_source", "native_snapshot_status", "native_snapshot_reason",
            )
        },
    }


def main() -> None:
    now = datetime.now(HKT)
    ledger = read_json(LEDGER, {})
    watches = ledger.get("watch") if isinstance(ledger, dict) and isinstance(ledger.get("watch"), dict) else {}
    cards = read_json(PREDICTIONS, [])
    cards_by_id = {
        str(row.get("match_id") or ""): row for row in cards
        if isinstance(row, dict) and row.get("match_id")
    } if isinstance(cards, list) else {}
    namespace = ledger.get("wilson_validation") if isinstance(ledger, dict) else {}
    namespace = namespace if isinstance(namespace, dict) else {}
    conditions = namespace.get("conditions") if isinstance(namespace.get("conditions"), dict) else {}
    observations = [
        row for row in namespace.get("observations") or [] if isinstance(row, dict)
    ]
    current: list[dict[str, Any]] = []
    first_look_missing = 0
    terminal_data_missing = 0
    for key, watch in watches.items():
        if not isinstance(watch, dict):
            continue
        kickoff = parse_time(watch.get("kickoff_hkt") or watch.get("kickoff"))
        if kickoff is None or kickoff <= now or kickoff > now + timedelta(hours=24):
            continue
        first = stage_status(watch, "首預")
        if not first["snapshot"].get("stage"):
            first_look_missing += 1
        if first["snapshot"].get("status") == "DATA_MISSING":
            terminal_data_missing += 1
        current.append({
            "fixture_id": str(watch.get("match_id") or key),
            "native_fixture_id": str(watch.get("native_fixture_id") or watch.get("titan_match_id") or ""),
            "fixture": f"{watch.get('home') or ''} vs {watch.get('away') or ''}",
            "kickoff_hkt": kickoff.isoformat(),
            "discovered_at": watch.get("discovered_at"),
            "first_look": first,
            "t30": stage_status(watch, "T-30"),
            "t5": stage_status(watch, "T-5"),
            "card_present": str(watch.get("match_id") or key) in cards_by_id,
        })
    current.sort(key=lambda row: row["kickoff_hkt"])

    target = [
        row for row in observations
        if str(row.get("match_id") or "") == "2920948"
        and str(row.get("condition_number") or "") in {"8", "13", "14"}
    ]
    target.sort(key=lambda row: int(row.get("condition_number") or 0))
    rendered_target = []
    duplicate_ids = len({str(row.get("observation_id") or "") for row in target}) != len(target)
    for row in target:
        signature = str(row.get("frozen_condition_signature") or "")
        frozen = conditions.get(signature) if isinstance(conditions.get(signature), dict) else {}
        active = (
            (frozen.get("evidence_versions") or [])[-1]
            if isinstance(frozen.get("evidence_versions"), list) and frozen.get("evidence_versions")
            else {}
        )
        eligible, excluded = _eligible_rollover_rows(
            observations, "crown", signature, active,
        ) if active else ([], {"active_version_missing": 1})
        rendered_target.append({
            "observation_id": row.get("observation_id"),
            "condition_number": row.get("condition_number"),
            "signature": signature,
            "status": row.get("status"),
            "result": row.get("result"),
            "settled_at": row.get("settled_at"),
            "settlement_source": row.get("settlement_source"),
            "bet_status": row.get("bet_status"),
            "stake_present": "stake" in row,
            "pnl_present": "pnl" in row,
            "stake": row.get("stake"),
            "pnl": row.get("pnl"),
            "rollover_provenance": row.get("rollover_provenance"),
            "active_version": active.get("version"),
            "active_evidence_hash": str(active.get("evidence_hash") or "")[:24],
            "eligible_progress": f"{len(eligible)}/{ROLLOVER_BATCH_SIZE}",
            "eligible_excluded": excluded,
            "stored_progress": frozen.get("pending_rollover_progress"),
            "rollover_status": frozen.get("rollover_status"),
        })
    incidents = ledger.get("hourly_first_look_reconciliation_incidents")
    incidents = incidents if isinstance(incidents, list) else []
    payload = {
        "mode": "provider_free_read_only",
        "at_hkt": now.isoformat(),
        "server_sha": subprocess.run(
            ["git", "-C", "/opt/footbreak", "rev-parse", "HEAD"],
            text=True, capture_output=True, check=False, timeout=10,
        ).stdout.strip(),
        "reconciliation": {
            "future_watch_count": len(current),
            "future_first_look_missing": first_look_missing,
            "future_first_look_data_missing": terminal_data_missing,
            "recent_incidents": incidents[-20:],
            "fixtures": current[:50],
            "journal": journal_summary(),
        },
        "formal_target_2920948": {
            "observation_count": len(target),
            "duplicate_observation_ids": duplicate_ids,
            "observation_statuses": dict(Counter(str(row.get("status") or "") for row in target)),
            "rows": rendered_target,
        },
        "units": systemctl([
            "crown-first-look-reconcile.timer", "crown-first-look-reconcile.service",
            "crown-tick.timer", "crown-tick.service",
            "crown-settle.timer", "crown-settle.service",
            "crown-dashboard-api.service",
        ]),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
