"""Idempotent local reconciliation for immutable learning-store stage keys.

This module intentionally performs no provider, connector, or model calls.  The
15-minute result runners fetch and verify trusted results first; this pass only
projects legacy duplicate pre-kickoff snapshots to their recorded canonical
rows, using SQLite transactions and immutable audit records.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .learning_store import LearningStore


def run(learning_db: Path, system: str | None = None) -> dict[str, object]:
    """Reconcile legacy duplicate stage keys without deleting source evidence."""
    with LearningStore(learning_db) as store:
        return store.reconcile_stage_duplicates(system)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile legacy learning-store duplicate stage keys locally."
    )
    parser.add_argument("--learning-db", required=True, type=Path)
    parser.add_argument("--system", choices=("footbreak", "crown"))
    args = parser.parse_args()
    if not args.learning_db.is_file():
        raise SystemExit(f"learning database is missing: {args.learning_db}")
    print(json.dumps(run(args.learning_db, args.system), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
