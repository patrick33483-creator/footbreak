#!/usr/bin/env python3
"""Regenerate only the Footbreak public recovery projection.

This helper deliberately reads an existing public dashboard artifact and
writes a replacement public artifact atomically.  It does not call a provider,
settlement, notification, model, or the normal ``gen_app_data.main`` path
(which persists the prediction archive).  Raw snapshots, histories, ledgers,
and the learning database are never opened for writing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def _write_public(path: Path, payload: dict[str, Any]) -> None:
    """Replace the public artifact atomically without touching source files."""
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".odds-recovery-dashboard.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def regenerate(data_path: Path) -> dict[str, int]:
    """Apply the private read-only overlay to Footbreak's dashboard projection."""
    from analysis.odds_recovery import overlay_rows
    from system.gen_app_data import (
        PREDICTION_ERA,
        PREDICTION_SCHEMA_VERSION,
        _prediction_history_stats,
    )

    payload: Any = json.loads(data_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("malformed_footbreak_dashboard")
    history = payload.get("prediction_history")
    if not isinstance(history, dict) or not isinstance(history.get("rows"), list):
        raise ValueError("missing_footbreak_prediction_history")
    raw_rows = history["rows"]
    if not all(isinstance(row, dict) for row in raw_rows):
        raise ValueError("malformed_footbreak_prediction_history_rows")

    # overlay_rows deep-copies, so parsed raw dashboard rows remain unchanged
    # until this explicitly requested public output is atomically replaced.
    rows = overlay_rows(raw_rows, "footbreak")
    comparable_rows = [
        row for row in rows if row.get("prediction_era") == PREDICTION_ERA
    ]
    stats = _prediction_history_stats(comparable_rows)
    stats["all_history_audit"] = _prediction_history_stats(rows)
    stats["scope"] = {
        "model_version": PREDICTION_ERA,
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "rows": len(comparable_rows),
        "all_history_rows": len(rows),
        "description": "目前模型版本；全歷史保留於 all_history_audit。",
    }
    history = {**history, "rows": rows, "stats": stats}
    _write_public(data_path, {**payload, "prediction_history": history})
    return {"prediction_history_rows": len(rows), "comparable_rows": len(comparable_rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("/var/www/footbreak/data.json"),
        help="existing Footbreak public dashboard artifact to replace atomically",
    )
    args = parser.parse_args()
    print(json.dumps(regenerate(args.data), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
