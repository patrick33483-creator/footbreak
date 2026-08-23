"""Per-fixture native stage durability store (Crown T-5 deadline-first patch).

Context
-------
The 2026-08-23 21:00 HKT incident showed five sampled fixtures reaching T-30
but never reaching a committed T-5: ``crown-tick.service`` was killed by
``TimeoutStartSec`` at 20:55:50, 20:56:48, 20:57:45, 20:58:42 and 20:59:39,
each attempt recording ``retry_count`` 6 and
``reason=deadline_exhausted_before_native_stage_commit``.  The existing
deadline-critical path (``_journal_timed_stage_attempts`` /
``_commit_stage_predictions`` in ``crown/engine.py``) always performs a
whole-ledger ``load_ledger``/``save_ledger`` round trip, and
``crown/common.py:write_json_atomic`` pretty-prints and sorts the *entire*
JSON document on every write.  Under a same-minute batch of many due
fixtures this makes every single fixture's commit cost grow with the size of
the *whole* ledger (currently unbounded, and growing every day), not with the
size of that one fixture's own record.

This module adds an independent, per-fixture, bounded, atomic persistence
path that a deadline worker can use *instead of* the monolithic ledger for
the truly time-critical write:  "this fixture's T-30/T-5 attempt or snapshot
is durable."  It intentionally:

  * Never imports or calls ``crown.state.load_ledger`` / ``save_ledger``.
  * Never reads or writes ``ledger.json`` at all.
  * Writes one bounded JSON file per fixture under
    ``<state_dir>/native_stage/<safe_match_id>.json``.
  * Uses ``fsync`` + ``os.replace`` for every commit (same durability
    contract as ``write_json_atomic``, but scoped to one small file).
  * Keeps an append-only, length-bounded attempt history and immutable,
    bounded normalized snapshots (never a raw/unbounded provider payload).
  * Preserves the legacy watch identity (``match_id``, ``native_fixture_id``,
    ``kickoff``) the first time a fixture state is created, and never erases
    an existing stage (e.g. T-30) when a later stage (T-5) is written.
  * Never fetches or mutates state for a fixture whose kickoff has already
    passed except to mark it ``EXPIRED`` -- this store performs no provider
    I/O of its own.

This module is intentionally *not* wired into ``crown.engine.run`` by
default.  It is a self-contained, testable persistence primitive plus a
bounded worker seam (see ``commit_due_fixtures``) that a future, carefully
reviewed integration can call from the tick path once the project decides
the production risk is acceptable (see
``CROWN_NATIVE_STAGE_STORE_ENABLED``).  Until then it runs in shadow mode
only: nothing in ``crown/engine.py``, ``crown/ledger.py``, ``crown/state.py``,
``crown/settle.py``, ``crown/notify.py`` or the dashboard is modified by this
patch.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .common import HKT, iso_hkt

# Feature flag: default OFF.  Even when the caller explicitly imports and
# uses this module (as the focused test suite does), production tick/sweep
# code paths in crown/engine.py are completely unmodified by this patch, so
# this flag only gates the *optional* convenience entry point
# ``maybe_enabled`` below -- it is not read anywhere else in this patch.
ENV_ENABLED = "CROWN_NATIVE_STAGE_STORE_ENABLED"

SCHEMA_VERSION = 1
NATIVE_STAGE_SUBDIR = "native_stage"
TIMED_STAGES = ("T-30", "T-5")
TERMINAL_STATES = ("COMMITTED", "FAILED", "DATA_MISSING", "EXPIRED")
_MAX_ATTEMPT_HISTORY = 12
_MAX_SNAPSHOT_QUOTES = 240
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.\-]")


def is_enabled() -> bool:
    """Whether the optional integration seam should be considered active.

    Nothing in the existing production tick/sweep code calls this in the
    current patch; it exists so a later, separately reviewed integration
    change can gate itself cheaply and consistently once introduced.
    """
    return os.getenv(ENV_ENABLED, "0").strip().lower() in {"1", "true", "yes", "on"}


def _safe_match_id(match_id: str) -> str:
    """Return a filesystem-safe, collision-resistant per-fixture file stem."""
    text = str(match_id or "").strip()
    cleaned = _SAFE_ID_RE.sub("_", text) or "unknown"
    # Cap length; a pathological upstream ID must not create an unbounded
    # filename.  A short content hash keeps distinct long IDs distinguishable.
    if len(cleaned) <= 120:
        return cleaned
    import hashlib
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{cleaned[:100]}__{digest}"


def fixture_store_path(state_dir: Path, match_id: str) -> Path:
    return state_dir / NATIVE_STAGE_SUBDIR / f"{_safe_match_id(match_id)}.json"


@dataclass(frozen=True)
class FixtureIdentity:
    """Stable identity key: match_id + kickoff UTC, per the design mandate."""

    match_id: str
    kickoff_utc: str

    def as_dict(self) -> dict[str, str]:
        return {"match_id": self.match_id, "kickoff_utc": self.kickoff_utc}


def _now_iso() -> str:
    return iso_hkt()


def _bounded_snapshot(stage: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Copy only bounded, normalized fields -- never a raw provider payload.

    This deliberately allow-lists fields.  A caller that hands this function
    an entire raw provider response still only persists the safe subset;
    unknown/unbounded keys (arbitrarily large HTML/JSON blobs, credentials,
    etc.) are silently dropped rather than durably stored.
    """
    allowed_scalar_keys = (
        "match_id", "league", "home", "away", "kickoff_hkt", "kickoff_utc",
        "status", "odds_status", "odds_reason", "source", "quote_source",
        "native_snapshot_status", "native_snapshot_reason",
    )
    out: dict[str, Any] = {
        key: snapshot.get(key) for key in allowed_scalar_keys if key in snapshot
    }
    quotes = snapshot.get("selected_odds_journal")
    if isinstance(quotes, list):
        bounded_quotes = []
        for row in quotes[:_MAX_SNAPSHOT_QUOTES]:
            if not isinstance(row, dict):
                continue
            bounded_quotes.append({
                k: row.get(k) for k in (
                    "code", "line", "side", "odds", "odds_status", "reason",
                    "source", "provider", "observed_at",
                )
                if k in row
            })
        out["selected_odds_journal"] = bounded_quotes
    out["stage"] = stage
    out["ts"] = snapshot.get("ts") or _now_iso()
    out["schema_version"] = SCHEMA_VERSION
    return out


def _atomic_write(path: Path, data: Any) -> None:
    """Write one bounded per-fixture JSON file with fsync + os.replace.

    This intentionally mirrors ``crown.common.write_json_atomic``'s
    durability contract but is scoped to a single small file, so its cost
    does not grow with the whole ledger.  It never touches ``ledger.json``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # A corrupt per-fixture file fails closed: the caller treats this
        # fixture as if no state existed yet and can safely re-STARTED it.
        # It never silently fabricates or discards a *different* fixture's
        # state (each fixture's durability is fully isolated on disk).
        return None
    return data if isinstance(data, dict) else None


class FixtureLockTimeout(RuntimeError):
    """Raised when a per-fixture advisory lock cannot be acquired in time."""


def _fixture_lock_path(state_dir: Path, match_id: str) -> Path:
    return state_dir / NATIVE_STAGE_SUBDIR / f".{_safe_match_id(match_id)}.lock"


class _FixtureLock:
    """Per-fixture advisory lock so two workers never race one file.

    Deliberately scoped to a single fixture: contention on fixture A's lock
    can never block fixture B's independent commit.
    """

    def __init__(self, state_dir: Path, match_id: str, timeout_seconds: float = 2.0) -> None:
        self._path = _fixture_lock_path(state_dir, match_id)
        self._timeout = timeout_seconds
        self._handle = None

    def __enter__(self) -> bool:
        import fcntl
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + max(0.0, self._timeout)
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    return False
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))

    def __exit__(self, *_exc: Any) -> None:
        if self._handle is not None:
            import fcntl
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None


def _new_fixture_state(
    match_id: str,
    *,
    league: str | None,
    home: str | None,
    away: str | None,
    kickoff: datetime,
    native_fixture_id: str | None,
    legacy_watch_identity: dict[str, Any] | None,
) -> dict[str, Any]:
    kickoff_utc = kickoff.astimezone(timezone.utc)
    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "match_id": match_id,
        "native_fixture_id": native_fixture_id or match_id,
        "league": league or "",
        "home": home or "",
        "away": away or "",
        "kickoff_hkt": iso_hkt(kickoff),
        "kickoff_utc": kickoff_utc.isoformat(),
        "identity": FixtureIdentity(match_id, kickoff_utc.isoformat()).as_dict(),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        # Append-only.  Every transition (STARTED/COMMITTED/FAILED/
        # DATA_MISSING/EXPIRED) is appended, never rewritten in place, so a
        # crash mid-write can never silently erase prior evidence.
        "attempt_history": [],
        # Keyed by stage; each stage's own immutable normalized snapshot.
        # Writing T-5 must never remove or overwrite the T-30 entry.
        "snapshots": {},
        # Preserve legacy monolithic-ledger watch identity fields the first
        # time this fixture state is created, so any later compatibility
        # projection back into the legacy ledger shape has what it needs.
        "legacy_watch_identity": (
            dict(legacy_watch_identity) if isinstance(legacy_watch_identity, dict) else None
        ),
    }
    return state


def _append_attempt(
    state: dict[str, Any],
    stage: str,
    new_state: str,
    *,
    reason: str | None = None,
    source: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    history = state.setdefault("attempt_history", [])
    if not isinstance(history, list):
        history = []
        state["attempt_history"] = history
    record = {
        "stage": stage,
        "state": new_state,
        "reason": reason,
        "source": source,
        "at": _now_iso(),
        "attempt_id": attempt_id,
    }
    history.append(record)
    state["attempt_history"] = history[-max(_MAX_ATTEMPT_HISTORY, 1):]
    return record


def _latest_terminal_attempt(state: dict[str, Any], stage: str) -> dict[str, Any] | None:
    for record in reversed(state.get("attempt_history") or []):
        if isinstance(record, dict) and record.get("stage") == stage and record.get("state") in TERMINAL_STATES:
            return record
    return None


def _latest_attempt(state: dict[str, Any], stage: str) -> dict[str, Any] | None:
    for record in reversed(state.get("attempt_history") or []):
        if isinstance(record, dict) and record.get("stage") == stage:
            return record
    return None


class NativeStageStore:
    """Per-fixture, bounded, atomic native stage persistence.

    Every public method here touches exactly one fixture's own file.  None
    of them read or write ``ledger.json``, and none of them call
    ``crown.state.load_ledger``/``save_ledger``.
    """

    def __init__(self, state_dir: Path, *, lock_timeout_seconds: float = 2.0) -> None:
        self.state_dir = Path(state_dir)
        self.lock_timeout_seconds = lock_timeout_seconds

    def path_for(self, match_id: str) -> Path:
        return fixture_store_path(self.state_dir, match_id)

    def read(self, match_id: str) -> dict[str, Any] | None:
        return _read_state(self.path_for(match_id))

    def mark_started(
        self,
        match_id: str,
        stage: str,
        *,
        kickoff: datetime,
        league: str | None = None,
        home: str | None = None,
        away: str | None = None,
        native_fixture_id: str | None = None,
        legacy_watch_identity: dict[str, Any] | None = None,
        reason: str = "native_timed_stage_collection_started",
        source: str = "titan007-crown-id-3",
        write_fn: Callable[[Path, Any], None] | None = None,
    ) -> dict[str, Any]:
        """Write STARTED before any provider work, atomically.

        This must be called before the deadline worker performs any
        provider I/O, per the design mandate.  If the fixture already has a
        COMMITTED terminal attempt for this stage, this is a no-op (no
        duplicate attempt/snapshot is created) and the existing state is
        returned unchanged on disk (still written once for updated_at
        bookkeeping avoidance -- we explicitly skip writing at all).
        """
        writer = write_fn or _atomic_write
        path = self.path_for(match_id)
        with _FixtureLock(self.state_dir, match_id, self.lock_timeout_seconds) as acquired:
            if not acquired:
                raise FixtureLockTimeout(f"could not lock fixture {match_id!r}")
            state = _read_state(path)
            if state is None:
                state = _new_fixture_state(
                    match_id,
                    league=league, home=home, away=away, kickoff=kickoff,
                    native_fixture_id=native_fixture_id,
                    legacy_watch_identity=legacy_watch_identity,
                )
            else:
                # Never erase a prior stage snapshot; only add/refresh this
                # stage's bookkeeping. Preserve the very first legacy watch
                # identity ever recorded for this fixture.
                state.setdefault("snapshots", {})
                if legacy_watch_identity is not None and not state.get("legacy_watch_identity"):
                    state["legacy_watch_identity"] = dict(legacy_watch_identity)
            existing_terminal = _latest_terminal_attempt(state, stage)
            if existing_terminal is not None and existing_terminal.get("state") == "COMMITTED":
                # Idempotent: a duplicate STARTED for an already-committed
                # stage must not create a new attempt or touch the snapshot.
                return state
            attempt_id = f"{stage}:{_now_iso()}"
            _append_attempt(state, stage, "STARTED", reason=reason, source=source, attempt_id=attempt_id)
            state["updated_at"] = _now_iso()
            writer(path, state)
            return state

    def commit_snapshot(
        self,
        match_id: str,
        stage: str,
        snapshot: dict[str, Any],
        *,
        kickoff: datetime,
        source: str = "titan007-crown-id-3",
        write_fn: Callable[[Path, Any], None] | None = None,
    ) -> dict[str, Any]:
        """Atomically commit a bounded, immutable stage snapshot.

        Post-kickoff commits are refused (fail closed): this store never
        backfills a stage after kickoff, matching the production rule that
        a request admitted before kickoff but resolved after it must not
        become a persisted observation.
        """
        writer = write_fn or _atomic_write
        path = self.path_for(match_id)
        now = datetime.now(HKT)
        with _FixtureLock(self.state_dir, match_id, self.lock_timeout_seconds) as acquired:
            if not acquired:
                raise FixtureLockTimeout(f"could not lock fixture {match_id!r}")
            state = _read_state(path)
            if state is None:
                state = _new_fixture_state(
                    match_id, league=snapshot.get("league"), home=snapshot.get("home"),
                    away=snapshot.get("away"), kickoff=kickoff,
                    native_fixture_id=snapshot.get("native_fixture_id"),
                    legacy_watch_identity=None,
                )
            if kickoff.astimezone(HKT) <= now:
                # Never backfill a post-kickoff stage.  Record EXPIRED instead
                # and leave any earlier stage snapshot exactly as it was.
                _append_attempt(state, stage, "EXPIRED", reason="post_kickoff_commit_refused")
                state["updated_at"] = _now_iso()
                writer(path, state)
                return state
            existing_terminal = _latest_terminal_attempt(state, stage)
            if existing_terminal is not None and existing_terminal.get("state") == "COMMITTED":
                # Duplicate commit for an already-committed stage: idempotent
                # no-op.  Never create a second snapshot or terminal attempt.
                return state
            snapshots = state.setdefault("snapshots", {})
            if not isinstance(snapshots, dict):
                snapshots = {}
                state["snapshots"] = snapshots
            bounded = _bounded_snapshot(stage, snapshot)
            snapshots[stage] = bounded
            state["snapshots"] = snapshots
            _append_attempt(state, stage, "COMMITTED", reason=None, source=source)
            state["updated_at"] = _now_iso()
            writer(path, state)
            return state

    def mark_failed(
        self,
        match_id: str,
        stage: str,
        *,
        kickoff: datetime,
        reason: str,
        retryable: bool = True,
        write_fn: Callable[[Path, Any], None] | None = None,
    ) -> dict[str, Any]:
        """Record a terminal FAILED/DATA_MISSING outcome for one attempt.

        Pre-kickoff failures are retryable (the caller may re-run
        ``mark_started`` for the same stage on the next tick).  Post-kickoff,
        this always records EXPIRED instead and never leaves the door open
        for a later fetch/backfill.
        """
        writer = write_fn or _atomic_write
        path = self.path_for(match_id)
        now = datetime.now(HKT)
        with _FixtureLock(self.state_dir, match_id, self.lock_timeout_seconds) as acquired:
            if not acquired:
                raise FixtureLockTimeout(f"could not lock fixture {match_id!r}")
            state = _read_state(path)
            if state is None:
                state = _new_fixture_state(
                    match_id, league=None, home=None, away=None, kickoff=kickoff,
                    native_fixture_id=None, legacy_watch_identity=None,
                )
            existing_terminal = _latest_terminal_attempt(state, stage)
            if existing_terminal is not None and existing_terminal.get("state") == "COMMITTED":
                return state
            if kickoff.astimezone(HKT) <= now:
                _append_attempt(state, stage, "EXPIRED", reason="deadline_exhausted_before_native_stage_commit")
            else:
                outcome = "FAILED" if retryable else "DATA_MISSING"
                _append_attempt(state, stage, outcome, reason=reason)
            state["updated_at"] = _now_iso()
            writer(path, state)
            return state

    def expire_post_kickoff(
        self,
        match_id: str,
        stage: str,
        *,
        kickoff: datetime,
        write_fn: Callable[[Path, Any], None] | None = None,
    ) -> dict[str, Any] | None:
        """Close a due-but-unresolved stage after kickoff with zero I/O.

        This performs no provider call and creates no snapshot.  If the
        fixture has no state file at all yet (a job that never even
        journaled STARTED before the tick was killed), it creates one purely
        to record the EXPIRED incident -- never a stage snapshot.
        """
        writer = write_fn or _atomic_write
        now = datetime.now(HKT)
        if kickoff.astimezone(HKT) > now:
            return None
        path = self.path_for(match_id)
        with _FixtureLock(self.state_dir, match_id, self.lock_timeout_seconds) as acquired:
            if not acquired:
                raise FixtureLockTimeout(f"could not lock fixture {match_id!r}")
            state = _read_state(path)
            if state is None:
                state = _new_fixture_state(
                    match_id, league=None, home=None, away=None, kickoff=kickoff,
                    native_fixture_id=None, legacy_watch_identity=None,
                )
            existing_terminal = _latest_terminal_attempt(state, stage)
            if existing_terminal is not None and existing_terminal.get("state") in {"COMMITTED", "EXPIRED"}:
                return state
            _append_attempt(state, stage, "EXPIRED", reason="deadline_exhausted_before_native_stage_commit")
            state["updated_at"] = _now_iso()
            writer(path, state)
            return state


# ---------------------------------------------------------------------------
# Bounded per-fixture worker seam (default-off integration point)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DueFixture:
    match_id: str
    stage: str
    kickoff: datetime
    league: str | None = None
    home: str | None = None
    away: str | None = None
    native_fixture_id: str | None = None
    legacy_watch_identity: dict[str, Any] | None = None


@dataclass(frozen=True)
class FixtureOutcome:
    match_id: str
    stage: str
    state: str  # COMMITTED | FAILED | DATA_MISSING | EXPIRED
    reason: str | None = None


def commit_due_fixtures(
    store: NativeStageStore,
    fixtures: list[DueFixture],
    collect_snapshot: Callable[[DueFixture], dict[str, Any] | None],
    *,
    now: datetime | None = None,
    per_fixture_isolation: bool = True,
) -> list[FixtureOutcome]:
    """Deadline path order: STARTED -> bounded collection -> independent commit.

    ``collect_snapshot`` performs the (already-existing, unmodified) bounded
    provider collection for exactly one fixture and returns either a
    normalized snapshot dict (success) or ``None`` (unavailable).  This
    function does not implement its own provider policy or concurrency
    primitive: production integration is expected to reuse the existing
    bounded process-pool collectors in ``crown/engine.py``
    (``_collect_locked_direct_snapshots`` / ``_collect_same_id_bulk_fallback``)
    and simply call ``NativeStageStore`` per completed result instead of
    batching into ``_commit_stage_predictions``. This helper exists to prove
    and exercise the per-fixture commit contract in isolation.

    One fixture's collection raising is isolated: it is recorded as a FAILED
    attempt for that fixture only, and every other fixture in the batch is
    still processed and committed independently.
    """
    now = (now or datetime.now(HKT)).astimezone(HKT)
    outcomes: list[FixtureOutcome] = []
    for fixture in fixtures:
        kickoff = fixture.kickoff.astimezone(HKT)
        if kickoff <= now:
            # Post-kickoff: zero provider call, no snapshot, EXPIRED only.
            store.expire_post_kickoff(fixture.match_id, fixture.stage, kickoff=kickoff)
            outcomes.append(FixtureOutcome(fixture.match_id, fixture.stage, "EXPIRED", "post_kickoff_due"))
            continue
        # STARTED before provider work, per the deadline path order mandate.
        store.mark_started(
            fixture.match_id, fixture.stage, kickoff=kickoff,
            league=fixture.league, home=fixture.home, away=fixture.away,
            native_fixture_id=fixture.native_fixture_id,
            legacy_watch_identity=fixture.legacy_watch_identity,
        )
        try:
            snapshot = collect_snapshot(fixture)
        except Exception as exc:  # noqa: BLE001 - isolate one fixture's failure
            if not per_fixture_isolation:
                raise
            state = store.mark_failed(
                fixture.match_id, fixture.stage, kickoff=kickoff,
                reason=f"collection_exception:{type(exc).__name__}",
            )
            terminal = _latest_attempt(state, fixture.stage)
            outcomes.append(FixtureOutcome(
                fixture.match_id, fixture.stage,
                str(terminal.get("state")) if terminal else "FAILED",
                str(terminal.get("reason")) if terminal else None,
            ))
            continue
        if snapshot is None:
            state = store.mark_failed(
                fixture.match_id, fixture.stage, kickoff=kickoff,
                reason="native_quote_unavailable", retryable=True,
            )
            terminal = _latest_attempt(state, fixture.stage)
            outcomes.append(FixtureOutcome(
                fixture.match_id, fixture.stage,
                str(terminal.get("state")) if terminal else "DATA_MISSING",
                str(terminal.get("reason")) if terminal else None,
            ))
            continue
        store.commit_snapshot(fixture.match_id, fixture.stage, snapshot, kickoff=kickoff)
        outcomes.append(FixtureOutcome(fixture.match_id, fixture.stage, "COMMITTED", None))
    return outcomes


# ---------------------------------------------------------------------------
# Non-deadline compatibility/projection layer
# ---------------------------------------------------------------------------

def project_legacy_watch_row(state: dict[str, Any]) -> dict[str, Any]:
    """Project one fixture's native store state into a legacy-watch-shaped row.

    This is a *read-only, best-effort* consumer helper.  It never writes
    ``ledger.json`` itself and is not called from any deadline-critical path.
    A caller (e.g. a future reconciliation job) may use this to merge native
    store evidence into the monolithic ledger's ``watch[match_id]`` shape for
    legacy dashboard/Wilson/settlement consumers, entirely after the durable
    per-fixture commit has already happened.  A failure while building this
    projection must never be able to roll back or mutate the native store.
    """
    stages = []
    snapshots = state.get("snapshots") or {}
    for stage in ("首預", "T-30", "T-5"):
        row = snapshots.get(stage)
        if isinstance(row, dict):
            stages.append(dict(row))
    legacy_identity = state.get("legacy_watch_identity") or {}
    projected = {
        "match_id": state.get("match_id"),
        "native_fixture_id": state.get("native_fixture_id"),
        "league": state.get("league"),
        "home": state.get("home"),
        "away": state.get("away"),
        "kickoff": state.get("kickoff_hkt"),
        "kickoff_hkt": state.get("kickoff_hkt"),
        "kickoff_utc": state.get("kickoff_utc"),
        "stages": stages,
        "native_stage_store_projection": True,
    }
    for key, value in legacy_identity.items():
        projected.setdefault(key, value)
    return projected
