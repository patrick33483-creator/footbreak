"""Bounded local rebuild of the Footbreak -> Crown evidence projection.

The source is Crown's already-durable prediction-card file.  This module never
contacts a provider, invents a native stage, or changes a ledger/bet.  It only
re-publishes the narrow upcoming-fixture projection that `crown.state` normally
writes atomically after a Crown stage has committed.
"""
from __future__ import annotations

import argparse
import fcntl
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from crown.state import _footbreak_execution_evidence, read_json, write_json_atomic
from crown.common import parse_time


def _paths(
    predictions_path: Path | None = None,
    evidence_path: Path | None = None,
) -> tuple[Path, Path]:
    state = Path(os.environ.get("CROWN_STATE_DIR", "/var/lib/footbreak/crown"))
    return (
        predictions_path or state / "predictions.json",
        evidence_path
        or Path(
            os.environ.get(
                "FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH",
                state / "footbreak-execution-evidence.json",
            )
        ),
    )


@contextmanager
def _repair_lock(path: Path) -> Iterator[bool]:
    lock = path.with_name(f".{path.name}.repair.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def rebuild(
    *,
    predictions_path: Path | None = None,
    evidence_path: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Atomically republish upcoming native evidence; return False if busy/invalid.

    Past fixtures are deliberately omitted.  This is a projection repair, not
    a stage replay or a way to revive post-kickoff evidence.
    """
    predictions_path, evidence_path = _paths(predictions_path, evidence_path)
    current = now or datetime.now(timezone.utc)
    cards = read_json(predictions_path, [])
    if not isinstance(cards, list):
        return False
    upcoming: list[dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        kickoff = parse_time(card.get("kickoff_hkt") or card.get("kickoff"))
        if kickoff is not None and kickoff > current:
            upcoming.append(card)
    # Never use a repair call to rewrite the sidecar after all candidates have
    # kicked off.  A past fixture must remain a forensic artifact, not a new
    # bridge/quote source for a replayed Footbreak decision.
    if not upcoming:
        return False
    with _repair_lock(evidence_path) as acquired:
        if not acquired:
            return False
        # Pass the repair's explicit clock through to the projection.  This
        # preserves its strict upcoming-only contract in tests and in monitor
        # runs, without turning a delayed repair into a post-kickoff replay.
        write_json_atomic(
            evidence_path, _footbreak_execution_evidence(upcoming, now=current),
        )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions-path")
    parser.add_argument("--evidence-path")
    args = parser.parse_args()
    return 0 if rebuild(
        predictions_path=Path(args.predictions_path) if args.predictions_path else None,
        evidence_path=Path(args.evidence_path) if args.evidence_path else None,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
