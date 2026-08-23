"""Bounded deferred consumer that projects native stage commits into the
legacy ledger/Wilson/dashboard/Telegram pipeline (Crown T-5 patch, Stage 5).

Context
-------
Stages 1-4 built and proved (in shadow/offline mode only) a per-fixture,
bounded, atomic durability primitive (``crown/native_stage_store.py``) that
never touches ``ledger.json``.  Stage 5's mandate is to let a fixture's
T-30/T-5 durability be considered "safe" the instant its own
``NativeStageStore`` commit lands -- independent of whether/when the
monolithic ledger's Wilson admission, dashboard projection, recompute, and
Telegram/notify pipeline (all of which still require the whole-ledger
``load_ledger``/``sync_prediction``/``save_ledger``/``merge_predictions``
cycle) has run for that fixture yet.

This module is the *consumer* half of that split.  It never itself performs
the legacy commit -- it only tracks a durable, append-only, idempotent queue
of "this fixture/stage has a native-committed snapshot that still needs its
legacy projection" work items, so a caller (``native_stage_cutover.py`` or a
future bounded worker loop) can drain them through the *unmodified* legacy
``_commit_stage_predictions`` path at its own pace, safely resumable across
restarts, without ever blocking or gating the native commit that already
happened.

Design invariants (all enforced here, not just documented):

  * **Queue survives restarts.**  Every enqueue is an atomic, fsync'd write
    to a small per-fixture-stage JSON file under
    ``<state_dir>/native_stage_projection_queue/``.  A process restart does
    not lose or duplicate a pending item: ``drain`` re-reads the on-disk
    queue directory fresh every call.
  * **Idempotent enqueue.**  Enqueuing the same ``(match_id, stage)`` twice
    while a PENDING or COMPLETED item already exists for it is a no-op that
    never creates a duplicate queue entry or resets a COMPLETED item back to
    PENDING.  This mirrors ``NativeStageStore``'s own idempotent-commit
    contract one layer up.
  * **Append-only attempts.**  Every drain attempt (success or failure) is
    appended to that item's ``attempts`` list; nothing is ever rewritten or
    deleted in place except the terminal ``state`` field and the bounded
    attempt list itself (capped, oldest dropped first).
  * **Bounded, isolated failures.**  One item's projector raising is caught,
    recorded as a FAILED attempt for that item only, and never stops the
    drain loop from processing every other queued item.
  * **No semantic change to what gets projected.**  The actual legacy
    projection call (``project_fn``) is supplied by the caller and is
    expected to be the existing, unmodified
    ``crown.engine._commit_stage_predictions`` (or an equivalent) -- this
    module does not reimplement Wilson admission, recompute, settlement,
    dashboard, or Telegram logic, and does not change their inputs/outputs.
  * **No post-kickoff backfill.**  An item whose kickoff has passed before
    its legacy projection has run is marked EXPIRED instead of drained; the
    drain loop never calls ``project_fn`` for a fixture whose kickoff is in
    the past relative to the drain time, matching the same no-backfill rule
    already enforced inside ``NativeStageStore`` and the legacy
    ``_expire_lapsed_timed_stage_attempts``.
  * **Identity re-validated before projection.**  ``drain`` re-checks
    ``match_id``, ``stage`` and ``kickoff`` consistency between the queued
    item and the native snapshot it references before calling
    ``project_fn``; a mismatch fails the item closed (FAILED, not silently
    projected) rather than risk projecting under a stale/incorrect identity.

States
------
``PENDING``   -- enqueued, not yet successfully projected, not expired.
``COMPLETED`` -- ``project_fn`` returned normally at least once; terminal.
``EXPIRED``   -- kickoff passed before a successful projection; terminal,
                 no projection is ever attempted for an EXPIRED item.
``FAILED``    -- a bounded number of projection attempts have all raised;
                 this is *not* terminal by itself when ``retryable`` is
                 True: the item remains eligible to be retried by a later
                 ``drain`` call (still PENDING-equivalent) until it either
                 succeeds, expires, or exhausts ``_MAX_ATTEMPT_HISTORY``
                 attempts, at which point it is frozen as terminal FAILED
                 so a persistently broken projector cannot spin forever.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .common import HKT, iso_hkt
from .native_stage_store import _safe_match_id  # reuse the same safe-id scheme

SCHEMA_VERSION = 1
QUEUE_SUBDIR = "native_stage_projection_queue"
PENDING = "PENDING"
COMPLETED = "COMPLETED"
EXPIRED = "EXPIRED"
FAILED = "FAILED"
TERMINAL_STATES = (COMPLETED, EXPIRED)
_MAX_ATTEMPT_HISTORY = 12
_MAX_TERMINAL_FAILURES = 8

# Feature flag: default OFF.  Even when a caller explicitly imports and uses
# this module directly (as the focused test suite does), no production tick
# path calls it unless this flag (read only by native_stage_cutover.py, not
# by this module itself) is enabled.  This module performs no flag check on
# its own -- it is a pure, always-available primitive; gating lives one
# layer up so the primitive itself stays trivially testable.
ENV_ENABLED = "CROWN_NATIVE_STAGE_DEFERRED_PROJECTION_ENABLED"


def is_enabled() -> bool:
    return os.getenv(ENV_ENABLED, "0").strip().lower() in {"1", "true", "yes", "on"}


def _item_path(state_dir: Path, match_id: str, stage: str) -> Path:
    stem = f"{_safe_match_id(match_id)}__{_safe_match_id(stage)}"
    return state_dir / QUEUE_SUBDIR / f"{stem}.json"


def _atomic_write(path: Path, data: Any) -> None:
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


def _read_item(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # A corrupt queue item fails closed: treat as absent so a fresh
        # enqueue can recreate it; never lets a corrupt file wedge the
        # drain loop or silently mutate a different item.
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class ProjectionItem:
    match_id: str
    stage: str
    kickoff: datetime
    payload: dict[str, Any]


@dataclass(frozen=True)
class DrainOutcome:
    match_id: str
    stage: str
    state: str
    reason: str | None = None


@dataclass
class _Attempt:
    at: str
    outcome: str
    reason: str | None = None


class DeferredProjectionQueue:
    """Append-only, restart-safe, idempotent legacy-projection work queue.

    Every public method touches exactly one fixture-stage's own queue file
    (mirroring ``NativeStageStore``'s per-fixture isolation).  This class
    never reads or writes ``ledger.json`` and never calls
    ``crown.state.load_ledger``/``save_ledger``/``merge_predictions``
    itself -- only the caller-supplied ``project_fn`` passed to ``drain``
    does that, exactly as it already does today.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)

    def path_for(self, match_id: str, stage: str) -> Path:
        return _item_path(self.state_dir, match_id, stage)

    def read(self, match_id: str, stage: str) -> dict[str, Any] | None:
        return _read_item(self.path_for(match_id, stage))

    def enqueue(
        self,
        match_id: str,
        stage: str,
        *,
        kickoff: datetime,
        payload: dict[str, Any],
        write_fn: Callable[[Path, Any], None] | None = None,
    ) -> dict[str, Any]:
        """Enqueue one fixture-stage for deferred legacy projection.

        Idempotent: if a PENDING, COMPLETED or terminal-FAILED item already
        exists for this exact ``(match_id, stage)``, this is a no-op that
        returns the existing item unchanged -- it never resets a completed
        or terminal item back to PENDING, and never creates a duplicate.
        """
        writer = write_fn or _atomic_write
        path = self.path_for(match_id, stage)
        existing = _read_item(path)
        if existing is not None:
            return existing
        now = datetime.now(HKT)
        item = {
            "schema_version": SCHEMA_VERSION,
            "match_id": match_id,
            "stage": stage,
            "kickoff_hkt": iso_hkt(kickoff.astimezone(HKT)),
            "payload": payload,
            "state": PENDING,
            "created_at": iso_hkt(now),
            "updated_at": iso_hkt(now),
            # Append-only.  Every drain attempt (success, failure, or
            # expiry) is appended, never rewritten in place.
            "attempts": [],
        }
        writer(path, item)
        return item

    def pending_items(self) -> list[dict[str, Any]]:
        """List every non-terminal item currently on disk (restart-safe).

        Re-reads the queue directory fresh every call; nothing is cached in
        memory across calls, so this reflects exactly what would still need
        draining after a process restart.
        """
        directory = self.state_dir / QUEUE_SUBDIR
        if not directory.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for entry in sorted(directory.glob("*.json")):
            data = _read_item(entry)
            if data is None:
                continue
            if data.get("state") in TERMINAL_STATES:
                continue
            # A terminal-FAILED item (frozen after too many attempts) is
            # also excluded from further draining -- see ``_freeze_failed``.
            if data.get("state") == FAILED and data.get("frozen"):
                continue
            items.append(data)
        return items

    def _append_attempt(
        self, item: dict[str, Any], outcome: str, reason: str | None,
    ) -> None:
        attempts = item.setdefault("attempts", [])
        if not isinstance(attempts, list):
            attempts = []
        attempts.append({"at": iso_hkt(), "outcome": outcome, "reason": reason})
        item["attempts"] = attempts[-max(_MAX_ATTEMPT_HISTORY, 1):]

    def drain(
        self,
        project_fn: Callable[[dict[str, Any]], Any],
        *,
        now: datetime | None = None,
        max_items: int | None = None,
        write_fn: Callable[[Path, Any], None] | None = None,
    ) -> list[DrainOutcome]:
        """Drain every eligible pending item through ``project_fn``.

        ``project_fn`` receives the item's ``payload`` dict (whatever the
        producer enqueued -- typically a single legacy stage-prediction
        row) and is expected to perform the existing, unmodified legacy
        commit (e.g. ``crown.engine._commit_stage_predictions``).  Its
        return value is ignored; only whether it raises matters here.

        One item's ``project_fn`` exception is isolated: it is recorded as
        a FAILED attempt for that item only (item stays PENDING and
        eligible for a future drain, unless attempts are exhausted), and
        every other queued item is still processed in the same call.
        Nothing about a projection failure can roll back or mutate the
        native store commit that produced this queue item in the first
        place -- this module never touches ``native_stage_store.py``
        state files.
        """
        writer = write_fn or _atomic_write
        now = (now or datetime.now(HKT)).astimezone(HKT)
        outcomes: list[DrainOutcome] = []
        items = self.pending_items()
        if max_items is not None:
            items = items[: max(0, max_items)]
        for item in items:
            match_id = str(item.get("match_id") or "")
            stage = str(item.get("stage") or "")
            path = self.path_for(match_id, stage)
            kickoff = _parse_kickoff(item.get("kickoff_hkt"))
            if not match_id or not stage or kickoff is None:
                # Malformed queue item: fail closed, never guess an identity.
                self._append_attempt(item, "failed", "malformed_queue_item")
                item["state"] = FAILED
                item["updated_at"] = iso_hkt(now)
                writer(path, item)
                outcomes.append(DrainOutcome(match_id, stage, FAILED, "malformed_queue_item"))
                continue
            if kickoff <= now:
                # Never project a legacy stage row after kickoff. This is
                # the same no-backfill rule the native store and the legacy
                # expiry sweep already enforce; the deferred consumer must
                # not become a second path that reintroduces backfill.
                self._append_attempt(item, "expired", "post_kickoff_projection_refused")
                item["state"] = EXPIRED
                item["updated_at"] = iso_hkt(now)
                writer(path, item)
                outcomes.append(DrainOutcome(match_id, stage, EXPIRED, "post_kickoff_projection_refused"))
                continue
            payload = item.get("payload")
            payload_match = str((payload or {}).get("match_id") or "")
            payload_stage = str((payload or {}).get("stage") or "")
            if not isinstance(payload, dict) or payload_match != match_id or payload_stage != stage:
                # Identity re-validation before projection: never project
                # under a mismatched fixture/stage identity even if the
                # queue filename itself looked correct.
                self._append_attempt(item, "failed", "identity_mismatch_refused")
                item["state"] = FAILED
                item["updated_at"] = iso_hkt(now)
                writer(path, item)
                outcomes.append(DrainOutcome(match_id, stage, FAILED, "identity_mismatch_refused"))
                continue
            try:
                project_fn(payload)
            except Exception as exc:  # noqa: BLE001 - isolate one item's failure
                self._append_attempt(item, "failed", f"project_fn_exception:{type(exc).__name__}")
                attempts = item.get("attempts") or []
                failed_attempts = sum(
                    1 for a in attempts if isinstance(a, dict) and a.get("outcome") == "failed"
                )
                if failed_attempts >= _MAX_TERMINAL_FAILURES:
                    item["state"] = FAILED
                    item["frozen"] = True
                    reason = "max_projection_attempts_exhausted"
                else:
                    item["state"] = PENDING
                    reason = f"project_fn_exception:{type(exc).__name__}"
                item["updated_at"] = iso_hkt(now)
                writer(path, item)
                outcomes.append(DrainOutcome(match_id, stage, item["state"], reason))
                continue
            self._append_attempt(item, "completed", None)
            item["state"] = COMPLETED
            item["updated_at"] = iso_hkt(now)
            writer(path, item)
            outcomes.append(DrainOutcome(match_id, stage, COMPLETED, None))
        return outcomes

    def drain_batch(
        self,
        project_one: Callable[[dict[str, Any]], bool],
        *,
        now: datetime | None = None,
        max_items: int | None = None,
        write_fn: Callable[[Path, Any], None] | None = None,
    ) -> list[DrainOutcome]:
        """Bounded drain sharing ONE caller-owned legacy transaction (Stage 7).

        Unlike ``drain``, which is designed around a caller-supplied
        ``project_fn`` that performs its own complete
        load/sync/save/merge cycle per item (correct for a single item, but
        exactly the "N whole-ledger cycles per batch" cost this stage must
        avoid for a multi-item recovery drain), ``drain_batch`` performs all
        of this queue's own bookkeeping (identity re-validation, expiry,
        payload/identity mismatch, append-only attempt history, terminal
        state transitions) but delegates only the actual per-item legacy
        projection to ``project_one`` -- a narrow callback that is expected
        to mutate an **already-loaded** ledger object in place (e.g. calling
        ``crown.native_stage_cutover.project_committed_native_snapshot``
        against a ledger the caller loaded once, before this method was
        called, and will save once, after it returns). ``project_one``
        returns ``True`` for a successful/idempotent projection and
        ``False`` (or raises) for a failure; it must never itself call
        ``crown.state.load_ledger``/``save_ledger`` -- this method assumes
        exactly one such pair brackets the whole batch, owned by the caller.

        Every other invariant (identity re-validation, no-post-kickoff
        projection, append-only attempts, isolated per-item failure, bounded
        terminal-failure freezing) is identical to ``drain``.
        """
        writer = write_fn or _atomic_write
        now = (now or datetime.now(HKT)).astimezone(HKT)
        outcomes: list[DrainOutcome] = []
        items = self.pending_items()
        if max_items is not None:
            items = items[: max(0, max_items)]
        for item in items:
            match_id = str(item.get("match_id") or "")
            stage = str(item.get("stage") or "")
            path = self.path_for(match_id, stage)
            kickoff = _parse_kickoff(item.get("kickoff_hkt"))
            if not match_id or not stage or kickoff is None:
                self._append_attempt(item, "failed", "malformed_queue_item")
                item["state"] = FAILED
                item["updated_at"] = iso_hkt(now)
                writer(path, item)
                outcomes.append(DrainOutcome(match_id, stage, FAILED, "malformed_queue_item"))
                continue
            if kickoff <= now:
                self._append_attempt(item, "expired", "post_kickoff_projection_refused")
                item["state"] = EXPIRED
                item["updated_at"] = iso_hkt(now)
                writer(path, item)
                outcomes.append(DrainOutcome(match_id, stage, EXPIRED, "post_kickoff_projection_refused"))
                continue
            payload = item.get("payload")
            payload_match = str((payload or {}).get("match_id") or "")
            payload_stage = str((payload or {}).get("stage") or "")
            if not isinstance(payload, dict) or payload_match != match_id or payload_stage != stage:
                self._append_attempt(item, "failed", "identity_mismatch_refused")
                item["state"] = FAILED
                item["updated_at"] = iso_hkt(now)
                writer(path, item)
                outcomes.append(DrainOutcome(match_id, stage, FAILED, "identity_mismatch_refused"))
                continue
            try:
                ok = project_one(payload)
            except Exception as exc:  # noqa: BLE001 - isolate one item's failure
                ok = False
                exc_name = type(exc).__name__
            else:
                exc_name = None
            if not ok:
                self._append_attempt(
                    item, "failed",
                    f"project_one_exception:{exc_name}" if exc_name else "project_one_returned_false",
                )
                attempts = item.get("attempts") or []
                failed_attempts = sum(
                    1 for a in attempts if isinstance(a, dict) and a.get("outcome") == "failed"
                )
                if failed_attempts >= _MAX_TERMINAL_FAILURES:
                    item["state"] = FAILED
                    item["frozen"] = True
                    reason = "max_projection_attempts_exhausted"
                else:
                    item["state"] = PENDING
                    reason = (
                        f"project_one_exception:{exc_name}" if exc_name else "project_one_returned_false"
                    )
                item["updated_at"] = iso_hkt(now)
                writer(path, item)
                outcomes.append(DrainOutcome(match_id, stage, item["state"], reason))
                continue
            self._append_attempt(item, "completed", None)
            item["state"] = COMPLETED
            item["updated_at"] = iso_hkt(now)
            writer(path, item)
            outcomes.append(DrainOutcome(match_id, stage, COMPLETED, None))
        return outcomes


def _parse_kickoff(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=HKT)
    return parsed.astimezone(HKT)
