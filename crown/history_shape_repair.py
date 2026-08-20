"""Provider-free, fail-closed repair of Crown's persisted history ordering."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .common import write_json_atomic
from .config import Settings, settings
from .prediction_history import repair_history_shape
from .state import state_lock


class PersistedHistoryShapeError(ValueError):
    """The on-disk history cannot be safely normalized in place."""


class PersistedHistoryShapeBusy(RuntimeError):
    """A short Crown state commit did not release the lock before the deadline."""


@dataclass(frozen=True)
class PersistedHistoryShapeRepair:
    """The local repair result, suitable for operational logging and tests."""

    changed: bool
    rows: int


def history_path(config: Settings) -> Path:
    """Return the one immutable history document this repair is allowed to edit."""
    return config.state_dir / "prediction_history.json"


def _read_history_document(path: Path) -> dict[str, Any]:
    """Read strictly so a damaged document is never replaced by an empty one."""
    try:
        with path.open(encoding="utf-8") as handle:
            history = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PersistedHistoryShapeError(
            "crown_history_shape_unreadable_document"
        ) from exc
    if not isinstance(history, dict):
        raise PersistedHistoryShapeError("crown_history_shape_document_not_object")
    rows = history.get("rows")
    if not isinstance(rows, list):
        raise PersistedHistoryShapeError("crown_history_shape_rows_not_list")
    # ``repair_history_shape`` historically ignores non-object rows because it
    # is also called by normalizers that may filter them.  The persisted-file
    # repair has a stricter contract: it must retain every stored row.
    if any(not isinstance(row, dict) for row in rows):
        raise PersistedHistoryShapeError("crown_history_shape_row_not_object")
    return history


def repair_persisted_history_shape(
    config: Settings,
    *,
    lock_timeout_seconds: float = 2.0,
) -> PersistedHistoryShapeRepair:
    """Canonically order the saved history without providers or derived writes.

    Only the persisted Crown history path is read or written.  A bounded
    ``state_lock`` prevents a settle/tick commit from being overwritten.  The
    in-memory repair checks every identity before the atomic replacement, so
    ambiguous rows leave the source document untouched.
    """
    if not math.isfinite(lock_timeout_seconds) or lock_timeout_seconds < 0:
        raise ValueError("lock_timeout_seconds must be a finite non-negative number")
    with state_lock(config, timeout_seconds=lock_timeout_seconds) as acquired:
        if not acquired:
            raise PersistedHistoryShapeBusy("crown_history_shape_repair_busy")
        path = history_path(config)
        history = _read_history_document(path)
        before = json.dumps(
            history, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        repair_history_shape(history)
        after = json.dumps(
            history, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        if before != after:
            write_json_atomic(path, history)
        return PersistedHistoryShapeRepair(
            changed=before != after,
            rows=len(history["rows"]),
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Locally normalize Crown prediction-history identity/order only.",
    )
    parser.add_argument(
        "--lock-timeout-seconds",
        type=float,
        default=2.0,
        help="maximum bounded wait for the Crown state commit lock (default: 2)",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.lock_timeout_seconds) or args.lock_timeout_seconds < 0:
        parser.error("--lock-timeout-seconds must be a finite non-negative number")
    try:
        result = repair_persisted_history_shape(
            settings(),
            lock_timeout_seconds=args.lock_timeout_seconds,
        )
    except PersistedHistoryShapeBusy as exc:
        print(str(exc), file=sys.stderr)
        return os.EX_TEMPFAIL
    except (PersistedHistoryShapeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        "crown_history_shape_repair "
        f"changed={str(result.changed).lower()} rows={result.rows}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
