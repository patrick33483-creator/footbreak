"""Bounded, provider-safe timing instrumentation for the Crown tick service.

Purpose
-------
Diagnose *where* a tick's wall clock is spent when it exceeds the systemd
``TimeoutStartSec`` budget.  This module never blocks the deadline-owning
path: every write is best-effort, wrapped in ``try/except BaseException`` and
capped to a hard per-call time budget, so a slow or failing disk write can
never itself become the reason a tick misses its commit deadline.

Safety
------
- No provider payload, quote, odds, price, team name, league name, or
  fixture-identifying content is ever recorded.  Only: a checkpoint label,
  a match_id opaque string (already public inside Crown's own ledger), a
  monotonic timestamp, remaining-budget seconds, and process resource
  counters (RSS/swap/major page faults) from ``resource.getrusage``.
- Output is an append-only, size-bounded JSONL file. Old lines are pruned
  opportunistically so the file cannot grow without bound if left enabled.
- Entirely gated by ``CROWN_TICK_TIMING_PROBE`` (default off in tests, and
  intended to default OFF in production too unless explicitly enabled via
  systemd Environment=, so this stays a diagnostic-only, opt-in tool).
"""
from __future__ import annotations

import json
import os
import resource
import time
from pathlib import Path
from typing import Any

_ENABLED_ENV = "CROWN_TICK_TIMING_PROBE"
_PATH_ENV = "CROWN_TICK_TIMING_PROBE_PATH"
_DEFAULT_PATH = "/var/lib/footbreak/crown/tick-timing-probe.jsonl"
_MAX_LINES = 4000
_MAX_WRITE_SECONDS = 0.05


def enabled() -> bool:
    return os.getenv(_ENABLED_ENV, "0").strip().lower() in {"1", "true", "yes", "on"}


def _probe_path() -> Path:
    return Path(os.getenv(_PATH_ENV, _DEFAULT_PATH))


def _rusage_fields() -> dict[str, float]:
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
    except Exception:
        return {}
    return {
        "self_max_rss_kb": float(usage.ru_maxrss),
        "self_majflt": float(usage.ru_majflt),
        "self_minflt": float(usage.ru_minflt),
        "self_utime_s": float(usage.ru_utime),
        "self_stime_s": float(usage.ru_stime),
        "children_max_rss_kb": float(children.ru_maxrss),
        "children_majflt": float(children.ru_majflt),
    }


def _meminfo_fields() -> dict[str, float]:
    """Best-effort system-wide swap pressure snapshot (no payload data)."""
    try:
        text = Path("/proc/meminfo").read_text(encoding="ascii", errors="ignore")
    except Exception:
        return {}
    wanted = {"SwapTotal", "SwapFree", "MemAvailable"}
    out: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        key = parts[0].strip()
        if key not in wanted:
            continue
        try:
            value = float(parts[1].strip().split()[0])
        except (ValueError, IndexError):
            continue
        out[f"sys_{key.lower()}_kb"] = value
    return out


def record(
    checkpoint: str,
    *,
    run_id: str | None = None,
    match_id: str | None = None,
    deadline: float | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one bounded timing sample.  Never raises; never blocks materially.

    ``deadline`` is a ``time.monotonic()``-based absolute cutoff (the same
    kind already threaded through crown/engine.py); this records only the
    remaining-seconds delta, never the raw deadline value tied to wall time.
    """
    if not enabled():
        return
    started = time.monotonic()
    try:
        sample: dict[str, Any] = {
            "checkpoint": str(checkpoint),
            "monotonic": time.monotonic(),
            "pid": os.getpid(),
        }
        if run_id:
            sample["run_id"] = str(run_id)
        if match_id:
            sample["match_id"] = str(match_id)
        if deadline is not None:
            sample["remaining_budget_s"] = round(deadline - time.monotonic(), 3)
        sample.update(_rusage_fields())
        sample.update(_meminfo_fields())
        if extra:
            # Only primitive, small values -- never provider payload blobs.
            for key, value in extra.items():
                if isinstance(value, (int, float, str, bool)) or value is None:
                    sample[str(key)] = value
        _append_bounded(sample)
    except BaseException:
        return
    finally:
        # If a single write ever took unexpectedly long (e.g. slow disk),
        # self-disable further writes for this process rather than risk
        # repeatedly stealing time from the deadline-owning caller.
        if time.monotonic() - started > _MAX_WRITE_SECONDS:
            os.environ[_ENABLED_ENV] = "0"


def _append_bounded(sample: dict[str, Any]) -> None:
    path = _probe_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(sample, separators=(",", ":"), sort_keys=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    # Opportunistic, cheap prune: only check/trim every so often via a coarse
    # heuristic (file size) to avoid stat-ing and rewriting on every call.
    try:
        if path.stat().st_size > 2_000_000:
            lines = path.read_text(encoding="utf-8").splitlines()[-_MAX_LINES:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
