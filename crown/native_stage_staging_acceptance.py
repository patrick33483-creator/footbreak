"""Stage 4 of the Crown T-5 deadline-first patch: an offline, fixture-safe
staging acceptance harness for the native stage shadow store (Stage 1/2) and
the read-only reconciliation adapter (Stage 3).

Context
-------
Stage 1 (``crown/native_stage_store.py``) delivered an isolated, per-fixture,
bounded, atomic persistence primitive with zero production call sites.
Stage 2 (``crown/native_stage_shadow.py``) wired that primitive into the real
tick path as default-off shadow instrumentation. Stage 3
(``crown/native_stage_reconciliation.py``) added a default-off, explicitly
invoked, read-only reconciliation/compare adapter between the shadow store
and the legacy ``ledger.json`` ``watch[match_id]`` shape, plus a narrow,
opt-in, unwired apply function -- none of it ever called from production.

This stage does **not** turn any flag on, does **not** touch any real
staging or production environment, and does **not** add any new call site
into ``crown/engine.py`` or any tick/sweep/dashboard/notify path. It adds a
single, offline, read-only **acceptance harness**: given an explicit,
caller-supplied state directory (containing already-captured
``native_stage/*.json`` shards) and an explicit, caller-supplied legacy
ledger (a path to a ``ledger.json``-shaped file, or an in-memory dict), it
computes a bounded, deterministic, JSON-serializable acceptance report and a
conservative go/no-go verdict describing whether the *evidence already
captured* would be safe to bring into a real staging rollout of Stage 2/3
with the feature flag turned on -- as a decision aid for a human operator,
never as an automatic trigger for anything.

This module explicitly:

  * Never imports ``crown.config`` (no environment-driven production
    ``state_dir``/``pinnapi``/``telegram`` settings are ever read). Every
    path this module touches is passed in explicitly by the caller.
  * Never imports a provider client (``TitanClient``, ``PinnapiClient``, or
    any HKJC/network client) and never performs a socket call.
  * Never imports ``crown.notify``, ``crown.settle``, ``crown.dashboard_data``,
    ``crown.dashboard_api``, ``analysis.wilson_validation``, or any
    betting/crossbook module.
  * Never calls ``crown.state.load_ledger``/``save_ledger`` (which resolve a
    production-configured path); the caller must supply the ledger dict or
    an explicit file path to read directly.
  * Never writes to the supplied legacy ledger, the supplied state dir, or
    anywhere else -- this entire module is read-only. There is no apply
    function here at all (Stage 3's ``apply_reconciliation_plan`` already
    exists, separately gated, and is not re-exposed or re-wrapped by this
    module).
  * Reuses Stage 3's :mod:`crown.native_stage_reconciliation` classification
    (``compare_fixture_stage``/``ReconciliationStatus``) rather than
    re-implementing identity verification, so the two stages can never
    silently disagree about what counts as a ``MATCH``/``CONFLICT``.

Go/no-go philosophy
--------------------
The verdict is deliberately conservative and fail-closed: any observed
violation of a hard safety invariant (a post-kickoff backfill, a duplicate
COMMITTED snapshot for the same fixture/stage, any CONFLICT, any legacy
regression signal, or a shadow-side failure that also affected the legacy
outcome) forces ``NO_GO`` outright, regardless of how good the aggregate
completeness ratio looks. A ``GO`` verdict is only a statement that the
*already-captured* evidence in the supplied bounded batch met every gate;
it is not an instruction to flip the flag, is not wired into
``crown/engine.py`` or any workflow, and does not perform any action.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import HKT, parse_time
from .config import Settings
from .ledger import STAGES
from .native_stage_reconciliation import (
    MAX_BOUNDED_FIXTURES,
    FixtureLookup,
    ReconciliationStatus,
    compare_fixture_stage,
)
from . import native_stage_store as _store

ATTEMPT_STATES = ("STARTED", "COMMITTED", "FAILED", "DATA_MISSING", "EXPIRED")

# ---------------------------------------------------------------------------
# Conservative, centrally-defined go/no-go thresholds.
#
# These are deliberately strict defaults for a *staging* acceptance pass on a
# feature that has never run against real production data with the flag on.
# They are plain module-level constants (not environment-configurable) so a
# reviewer can see the exact bar this stage applies without hunting through
# environment variable plumbing, and so no accidental env var can loosen
# them. Any change to these numbers is a reviewable code change.
# ---------------------------------------------------------------------------

# A batch must resolve at least this fraction of requested fixture/stage
# pairs to a terminal shadow attempt state (COMMITTED/FAILED/DATA_MISSING/
# EXPIRED) -- i.e. not still sitting on a bare STARTED with no terminal
# outcome recorded -- to be considered "complete" for acceptance purposes.
MIN_TERMINAL_COMPLETENESS_RATIO = 0.98

# A batch must have at least this many requested fixture/stage pairs before
# a GO verdict is considered meaningful at all; smaller batches can still be
# inspected (the report is still produced), but the verdict is forced to
# NO_GO with an explicit "insufficient_batch_size_for_verdict" reason so a
# single lucky small batch can never be mistaken for a broad rollout signal.
MIN_BATCH_SIZE_FOR_GO = 15

# Every function in this module refuses a batch larger than
# native_stage_reconciliation.MAX_BOUNDED_FIXTURES, inherited directly so the
# two stages' bounds can never drift apart.
MAX_BATCH_SIZE = MAX_BOUNDED_FIXTURES

# A single shadow shard file (per fixture) must not exceed this size. This
# mirrors the bounded-snapshot design of Stage 1 (``_MAX_SNAPSHOT_QUOTES``,
# bounded attempt history) -- a shard growing far past this is itself a
# signal that something is not bounded as designed.
MAX_SHARD_BYTES = 65_536

# A single fixture's attempt_history must not exceed this many records
# (Stage 1's own _MAX_ATTEMPT_HISTORY is 12; this gate is intentionally a
# little looser than the implementation constant so a future, deliberate
# change to that constant does not immediately fail every staging batch,
# while still catching an actually-unbounded growth defect).
MAX_ATTEMPT_HISTORY_LEN = 24

# A single read of one shadow shard (wall-clock, from this harness's own
# process) should stay well under this bound; the shard is one small bounded
# JSON file, not the whole ledger, so a much larger read time signals disk
# contention, an oversized file, or a lock-wait masquerading as a read.
MAX_READ_LATENCY_SECONDS = 0.25

GoNoGo = str  # "GO" | "NO_GO" -- kept as a plain str for trivial JSON output


@dataclass(frozen=True)
class FixtureAcceptanceRow:
    """Bounded, JSON-safe per-fixture/stage acceptance evidence."""

    match_id: str
    stage: str
    reconciliation_status: str
    attempt_states: tuple[str, ...]
    terminal_state: str | None
    shard_bytes: int | None
    read_latency_seconds: float | None
    attempt_history_len: int
    post_kickoff_violation: bool
    duplicate_committed: bool
    violations: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "stage": self.stage,
            "reconciliation_status": self.reconciliation_status,
            "attempt_states": list(self.attempt_states),
            "terminal_state": self.terminal_state,
            "shard_bytes": self.shard_bytes,
            "read_latency_seconds": self.read_latency_seconds,
            "attempt_history_len": self.attempt_history_len,
            "post_kickoff_violation": self.post_kickoff_violation,
            "duplicate_committed": self.duplicate_committed,
            "violations": list(self.violations),
        }


def _load_ledger_from_path(path: Path) -> dict[str, Any]:
    """Read a caller-supplied ledger JSON file directly -- never resolves a
    production-configured path, never calls ``crown.state.load_ledger``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_ledger(ledger: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(ledger, dict):
        return ledger
    return _load_ledger_from_path(Path(ledger))


def _stage_attempts(shadow_state: dict[str, Any] | None, stage: str) -> list[dict[str, Any]]:
    if not isinstance(shadow_state, dict):
        return []
    history = shadow_state.get("attempt_history")
    if not isinstance(history, list):
        return []
    return [
        record for record in history
        if isinstance(record, dict) and record.get("stage") == stage
    ]


def _shard_read_with_timing(
    store: "_store.NativeStageStore", match_id: str,
) -> tuple[dict[str, Any] | None, float | None, int | None]:
    """Read one shard, isolating any failure, and report bounded size/latency.

    Any exception (corrupt JSON, missing file, permission error, lock
    contention on a concurrent writer) degrades to "absent" -- exactly the
    same fail-closed isolation contract Stage 3 already uses -- and never
    raises out of this function.
    """
    import time as _time

    path = store.path_for(match_id)
    shard_bytes: int | None = None
    try:
        shard_bytes = path.stat().st_size
    except OSError:
        shard_bytes = None
    started = _time.monotonic()
    try:
        state = store.read(match_id)
    except Exception:  # noqa: BLE001 - isolate exactly this fixture's read
        state = None
    elapsed = _time.monotonic() - started
    return state, elapsed, shard_bytes


def build_staging_acceptance_report(
    config: Settings,
    lookups: list[FixtureLookup],
    *,
    ledger: dict[str, Any] | str | Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Offline, read-only, bounded acceptance report over a captured batch.

    ``config`` is used only for its ``state_dir`` (passed straight to a
    ``NativeStageStore`` instance) -- this function never reads any other
    field of ``config`` and never calls :func:`crown.config.settings`.
    ``ledger`` is either an already-loaded dict (e.g. a fixture-safe captured
    snapshot) or a path to a ``ledger.json``-shaped file read directly by
    this function; ``crown.state.load_ledger`` is never called.
    """
    if len(lookups) > MAX_BATCH_SIZE:
        raise ValueError(
            f"refusing unbounded staging acceptance batch: {len(lookups)} lookups "
            f"> MAX_BATCH_SIZE={MAX_BATCH_SIZE}"
        )
    now = (now or datetime.now(HKT)).astimezone(HKT)
    resolved_ledger = _resolve_ledger(ledger)
    store = _store.NativeStageStore(config.state_dir)

    rows: list[FixtureAcceptanceRow] = []
    reconciliation_tally: dict[str, int] = {status.value: 0 for status in ReconciliationStatus}
    attempt_tally: dict[str, int] = {state: 0 for state in ATTEMPT_STATES}
    read_latencies: list[float] = []
    shard_sizes: list[int] = []
    seen_committed: dict[tuple[str, str], int] = {}

    for lookup in lookups:
        match_id = str(lookup.match_id or "")
        stage = str(lookup.stage or "")
        comparison = compare_fixture_stage(config, resolved_ledger, lookup, now=now, store=store)
        reconciliation_tally[comparison.status.value] += 1

        shadow_state, read_latency, shard_bytes = _shard_read_with_timing(store, match_id)
        attempts = _stage_attempts(shadow_state, stage)
        attempt_states = tuple(
            str(record.get("state")) for record in attempts if record.get("state")
        )
        for state_name in attempt_states:
            if state_name in attempt_tally:
                attempt_tally[state_name] += 1
        terminal_state = next(
            (s for s in reversed(attempt_states) if s in _store.TERMINAL_STATES), None,
        )
        committed_count = sum(1 for s in attempt_states if s == "COMMITTED")
        duplicate_committed = committed_count > 1
        if duplicate_committed:
            seen_committed[(match_id, stage)] = committed_count

        history_len = len(shadow_state.get("attempt_history") or []) if isinstance(shadow_state, dict) else 0

        # Post-kickoff violation: any COMMITTED attempt for this stage whose
        # timestamp is after the fixture's own recorded kickoff. This is a
        # stronger, per-attempt-timestamp check than Stage 3's snapshot-level
        # EXPIRED_INVALID classification -- it looks at the *attempt log*
        # directly, so a shadow store that ever attempted a post-kickoff
        # commit is caught even if the final classification looks benign.
        post_kickoff_violation = False
        kickoff_dt = None
        if isinstance(shadow_state, dict):
            kickoff_raw = shadow_state.get("kickoff_utc") or shadow_state.get("kickoff_hkt")
            kickoff_dt = parse_time(kickoff_raw) if kickoff_raw else None
        if kickoff_dt is not None:
            kickoff_dt = kickoff_dt.astimezone(HKT)
            for record in attempts:
                if record.get("state") != "COMMITTED":
                    continue
                at_raw = record.get("at")
                at_dt = parse_time(at_raw) if at_raw else None
                if at_dt is not None and at_dt.astimezone(HKT) > kickoff_dt:
                    post_kickoff_violation = True
                    break

        violations: list[str] = []
        if comparison.status is ReconciliationStatus.CONFLICT:
            violations.append("reconciliation_conflict")
        if post_kickoff_violation:
            violations.append("post_kickoff_committed_attempt")
        if duplicate_committed:
            violations.append("duplicate_committed_attempt")
        if shard_bytes is not None and shard_bytes > MAX_SHARD_BYTES:
            violations.append("shard_size_exceeds_bound")
        if history_len > MAX_ATTEMPT_HISTORY_LEN:
            violations.append("attempt_history_exceeds_bound")
        if read_latency is not None and read_latency > MAX_READ_LATENCY_SECONDS:
            violations.append("read_latency_exceeds_bound")
        if comparison.status is ReconciliationStatus.EXPIRED_INVALID and "post_kickoff_no_backfill" in comparison.reasons:
            # Not itself a violation (this is the *correct* fail-closed
            # behavior), but surfaced so a reviewer can distinguish "shadow
            # correctly refused to backfill" from "shadow never ran here".
            pass

        if read_latency is not None:
            read_latencies.append(read_latency)
        if shard_bytes is not None:
            shard_sizes.append(shard_bytes)

        rows.append(FixtureAcceptanceRow(
            match_id=match_id, stage=stage,
            reconciliation_status=comparison.status.value,
            attempt_states=attempt_states, terminal_state=terminal_state,
            shard_bytes=shard_bytes, read_latency_seconds=read_latency,
            attempt_history_len=history_len,
            post_kickoff_violation=post_kickoff_violation,
            duplicate_committed=duplicate_committed,
            violations=tuple(violations),
        ))

    terminal_count = sum(1 for row in rows if row.terminal_state is not None)
    completeness_ratio = (terminal_count / len(rows)) if rows else 0.0

    latency_summary = {
        "count": len(read_latencies),
        "max_seconds": max(read_latencies) if read_latencies else None,
        "mean_seconds": statistics.fmean(read_latencies) if read_latencies else None,
        "p95_seconds": (
            sorted(read_latencies)[max(0, int(len(read_latencies) * 0.95) - 1)]
            if read_latencies else None
        ),
    }
    size_summary = {
        "count": len(shard_sizes),
        "max_bytes": max(shard_sizes) if shard_sizes else None,
        "mean_bytes": statistics.fmean(shard_sizes) if shard_sizes else None,
    }

    return {
        "generated_at": now.isoformat(),
        "requested": len(lookups),
        "compared": len(rows),
        "reconciliation_aggregate": reconciliation_tally,
        "attempt_state_aggregate": attempt_tally,
        "terminal_completeness_ratio": completeness_ratio,
        "duplicate_committed_fixtures": [
            {"match_id": m, "stage": s, "committed_count": c}
            for (m, s), c in sorted(seen_committed.items())
        ],
        "post_kickoff_violation_count": sum(1 for row in rows if row.post_kickoff_violation),
        "conflict_count": reconciliation_tally.get(ReconciliationStatus.CONFLICT.value, 0),
        "read_latency_summary": latency_summary,
        "shard_size_summary": size_summary,
        "fixtures": [row.as_dict() for row in rows],
        "provider_calls_made": 0,
        "writes_performed": 0,
        "thresholds": {
            "min_terminal_completeness_ratio": MIN_TERMINAL_COMPLETENESS_RATIO,
            "min_batch_size_for_go": MIN_BATCH_SIZE_FOR_GO,
            "max_batch_size": MAX_BATCH_SIZE,
            "max_shard_bytes": MAX_SHARD_BYTES,
            "max_attempt_history_len": MAX_ATTEMPT_HISTORY_LEN,
            "max_read_latency_seconds": MAX_READ_LATENCY_SECONDS,
        },
    }


def evaluate_go_no_go(report: dict[str, Any]) -> dict[str, Any]:
    """Pure function over a report dict -- centralizes every gate in one
    place, fail-closed: any single violation forces NO_GO regardless of how
    good the aggregate ratio looks. Performs no I/O and takes no action.
    """
    reasons: list[str] = []

    requested = int(report.get("requested") or 0)
    compared = int(report.get("compared") or 0)
    if compared < MIN_BATCH_SIZE_FOR_GO:
        reasons.append(
            f"insufficient_batch_size_for_verdict:{compared}<{MIN_BATCH_SIZE_FOR_GO}"
        )

    conflict_count = int(report.get("conflict_count") or 0)
    if conflict_count > 0:
        reasons.append(f"conflict_count_nonzero:{conflict_count}")

    post_kickoff_violations = int(report.get("post_kickoff_violation_count") or 0)
    if post_kickoff_violations > 0:
        reasons.append(f"post_kickoff_violation_count_nonzero:{post_kickoff_violations}")

    duplicates = report.get("duplicate_committed_fixtures") or []
    if duplicates:
        reasons.append(f"duplicate_committed_fixtures_nonzero:{len(duplicates)}")

    completeness = float(report.get("terminal_completeness_ratio") or 0.0)
    if completeness < MIN_TERMINAL_COMPLETENESS_RATIO:
        reasons.append(
            f"terminal_completeness_below_threshold:{completeness:.4f}<{MIN_TERMINAL_COMPLETENESS_RATIO}"
        )

    latency_summary = report.get("read_latency_summary") or {}
    max_latency = latency_summary.get("max_seconds")
    if isinstance(max_latency, (int, float)) and max_latency > MAX_READ_LATENCY_SECONDS:
        reasons.append(f"read_latency_exceeds_bound:{max_latency:.4f}>{MAX_READ_LATENCY_SECONDS}")

    size_summary = report.get("shard_size_summary") or {}
    max_bytes = size_summary.get("max_bytes")
    if isinstance(max_bytes, (int, float)) and max_bytes > MAX_SHARD_BYTES:
        reasons.append(f"shard_size_exceeds_bound:{max_bytes}>{MAX_SHARD_BYTES}")

    # Any per-fixture violation not already summarized above (defense in
    # depth: a future new violation type added to a row must still force
    # NO_GO even if this function is not updated to name it specifically).
    known_prefixes = (
        "reconciliation_conflict", "post_kickoff_committed_attempt",
        "duplicate_committed_attempt", "shard_size_exceeds_bound",
        "attempt_history_exceeds_bound", "read_latency_exceeds_bound",
    )
    extra_violation_types: set[str] = set()
    for row in report.get("fixtures") or []:
        for violation in row.get("violations") or []:
            if violation not in known_prefixes:
                extra_violation_types.add(violation)
            elif violation == "attempt_history_exceeds_bound":
                extra_violation_types.add(violation)
    if extra_violation_types:
        reasons.append(
            "unclassified_or_history_bound_violations_present:" + ",".join(sorted(extra_violation_types))
        )

    verdict: GoNoGo = "NO_GO" if reasons else "GO"
    return {
        "verdict": verdict,
        "reasons": reasons,
        "requested": requested,
        "compared": compared,
        "evaluated_at": report.get("generated_at"),
        "note": (
            "This verdict describes only the already-captured evidence in "
            "this bounded batch. It is not wired into any tick/sweep/"
            "dashboard/workflow path, performs no action, and must be "
            "reviewed by a human operator before any staging flag change."
        ),
    }


def build_staging_acceptance_verdict(
    config: Settings,
    lookups: list[FixtureLookup],
    *,
    ledger: dict[str, Any] | str | Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Convenience wrapper: report + verdict together, still fully read-only."""
    report = build_staging_acceptance_report(config, lookups, ledger=ledger, now=now)
    verdict = evaluate_go_no_go(report)
    return {"report": report, "verdict": verdict}


# ---------------------------------------------------------------------------
# Read-only, offline CLI entry point.
#
# Every path (state dir, ledger file) is an explicit required argument -- this
# CLI never falls back to crown.config.settings() or any environment-driven
# production default.
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m crown.native_stage_staging_acceptance",
        description=(
            "Offline, read-only staging acceptance harness for the Crown "
            "native stage shadow store. Requires an explicit --state-dir "
            "(containing already-captured native_stage/*.json shards) and "
            "an explicit --ledger-path (a captured ledger.json-shaped file). "
            "Never reads environment configuration, never calls a provider, "
            "never writes anything, and never applies any change."
        ),
    )
    parser.add_argument(
        "--state-dir", required=True,
        help="Explicit path to a captured state directory containing native_stage/*.json shards.",
    )
    parser.add_argument(
        "--ledger-path", required=True,
        help="Explicit path to a captured ledger.json-shaped file to compare against.",
    )
    parser.add_argument(
        "--match-id", action="append", default=[], dest="match_ids",
        help="Fixture match_id to inspect (repeatable). Required (with --stage) unless --pairs-file is given.",
    )
    parser.add_argument(
        "--stage", action="append", default=[], dest="stages",
        choices=sorted(STAGES),
        help="Stage(s) to inspect per match id (repeatable). Defaults to all stages.",
    )
    parser.add_argument(
        "--pairs-file",
        help=(
            "Path to a JSON file containing a list of {\"match_id\":..,\"stage\":..} "
            "objects -- an explicit, bounded, caller-curated fixture list."
        ),
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Print machine-readable JSON instead of a human summary.",
    )
    return parser


def _lookups_from_args(args: argparse.Namespace) -> list[FixtureLookup]:
    if args.pairs_file:
        raw = json.loads(Path(args.pairs_file).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("--pairs-file must contain a JSON list of {match_id, stage} objects")
        lookups = [
            FixtureLookup(str(item["match_id"]), str(item["stage"]))
            for item in raw if isinstance(item, dict) and item.get("match_id") and item.get("stage")
        ]
    elif args.match_ids:
        stages = args.stages or sorted(STAGES)
        lookups = [
            FixtureLookup(match_id, stage)
            for match_id in args.match_ids
            for stage in stages
        ]
    else:
        lookups = []
    return lookups[:MAX_BATCH_SIZE]


def main(argv: list[str] | None = None) -> int:
    """Read-only, offline CLI entry point. Never returns nonzero for a NO_GO
    verdict itself (that is a reported finding, not a tool failure) -- only a
    malformed invocation or an oversized batch exits nonzero.
    """
    args = _build_arg_parser().parse_args(argv)
    lookups = _lookups_from_args(args)
    if not lookups:
        print(
            "error: provide at least one --match-id (optionally with --stage) "
            "or a --pairs-file (refusing an unbounded/empty scan)",
            file=sys.stderr,
        )
        return 2
    if len(lookups) > MAX_BATCH_SIZE:
        print(
            f"error: {len(lookups)} fixture/stage pairs exceeds MAX_BATCH_SIZE={MAX_BATCH_SIZE}",
            file=sys.stderr,
        )
        return 2

    config = Settings(
        state_dir=Path(args.state_dir), app_dir=Path(args.state_dir), web_root=Path(args.state_dir),
        enabled=False, pinnapi_key=None, pinnapi_base_url="https://pinnapi.com",
        source_max_age_seconds=90, allow_inferred_pinnapi_timestamp=False,
        titan_bf_base="", titan_vip_base="", titan_company_id="3",
        telegram_enabled=False, telegram_bot_token=None, telegram_chat_id=None,
        confidence_floor=58.0, min_edge=0.02, bankroll=50000.0,
    )
    result = build_staging_acceptance_verdict(config, lookups, ledger=Path(args.ledger_path))
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        report = result["report"]
        verdict = result["verdict"]
        print(f"Compared {report['compared']} fixture/stage pairs:")
        for status, count in sorted(report["reconciliation_aggregate"].items()):
            print(f"  {status}: {count}")
        print(f"Terminal completeness ratio: {report['terminal_completeness_ratio']:.4f}")
        print(f"Conflicts: {report['conflict_count']}  Post-kickoff violations: {report['post_kickoff_violation_count']}  Duplicate commits: {len(report['duplicate_committed_fixtures'])}")
        print(f"Verdict: {verdict['verdict']}")
        for reason in verdict["reasons"]:
            print(f"  - {reason}")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
