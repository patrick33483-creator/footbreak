"""Read Crown's durable per-fixture timed-stage payloads.

The Crown collector commits T-30/T-5 before the slower legacy projection and
keeps the full normalized prediction in its deferred projection queue. V2
may read that queue, but never mutates it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_NATIVE_QUEUE_DIR = Path(
    "/var/lib/footbreak/crown/native_stage_projection_queue"
)


def load_native_payloads(
    queue_dir: Path | str = DEFAULT_NATIVE_QUEUE_DIR,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return usable exact-stage payloads keyed by ``(match_id, stage)``."""
    directory = Path(queue_dir)
    if not directory.is_dir():
        return {}

    payloads: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        match_id = str(item.get("match_id") or "")
        stage = str(item.get("stage") or "")
        payload = item.get("payload")
        if (
            not match_id
            or stage not in {"T-30", "T-5"}
            or not isinstance(payload, dict)
            or str(payload.get("match_id") or "") != match_id
            or str(payload.get("stage") or "") != stage
            or str(payload.get("status") or "") == "DATA_MISSING"
        ):
            continue
        rows = payload.get("forecast_candidates")
        if not isinstance(rows, list) or not any(isinstance(row, dict) for row in rows):
            continue
        payloads[(match_id, stage)] = payload
    return payloads


__all__ = ["DEFAULT_NATIVE_QUEUE_DIR", "load_native_payloads"]
