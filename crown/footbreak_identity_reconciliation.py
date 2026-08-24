"""Best-effort HKJC identity enrichment after durable Crown work."""
from __future__ import annotations

import multiprocessing
import os
import signal
from pathlib import Path

from .common import iso_hkt, write_json_atomic
from .config import Settings

_WORKER_TIMEOUT_SECONDS = 3.0
_REMOTE_TIMEOUT_SECONDS = 1.0
_HEALTH_FILE = "hkjc-identity-reconciliation-health.json"


def _record_health(
    config: Settings,
    status: str,
    *,
    detail: str | None = None,
    reconciled: int | None = None,
    stages: tuple[str, ...] = (),
) -> None:
    """Write isolated diagnostic state without touching native ledgers."""
    try:
        write_json_atomic(Path(config.state_dir) / _HEALTH_FILE, {
            "at": iso_hkt(),
            "status": status,
            "detail": detail,
            "reconciled": reconciled,
            "stages": sorted(set(stages)),
            "provider": "hkjc_public_board",
        })
    except BaseException:
        pass


def reconcile_persisted_hkjc_identities(config: Settings) -> int:
    """Fetch one bounded HKJC board and enrich already-persisted Crown cards.

    Provider I/O happens before the engine acquires its short state lock.  The
    engine helper changes identity/provenance only and cannot replay a stage,
    evaluate Wilson, create a simulation, or enqueue Telegram.
    """
    from .engine import _reconcile_hkjc_identities
    from .hkjc import event_from_match, fetch_matches

    previous_timeout = os.environ.get("FOOTBREAK_REMOTE_TIMEOUT_SECONDS")
    os.environ["FOOTBREAK_REMOTE_TIMEOUT_SECONDS"] = str(_REMOTE_TIMEOUT_SECONDS)
    try:
        rows = fetch_matches()
    finally:
        if previous_timeout is None:
            os.environ.pop("FOOTBREAK_REMOTE_TIMEOUT_SECONDS", None)
        else:
            os.environ["FOOTBREAK_REMOTE_TIMEOUT_SECONDS"] = previous_timeout
    events = [event_from_match(row) for row in rows]
    return _reconcile_hkjc_identities(
        config, [event for event in events if event is not None],
    )


def _worker(config: Settings, stages: tuple[str, ...]) -> None:
    def timed_out(_signum, _frame):
        raise TimeoutError("HKJC identity reconciliation timed out")

    timer_set = False
    try:
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, timed_out)
            signal.setitimer(signal.ITIMER_REAL, _WORKER_TIMEOUT_SECONDS)
            timer_set = True
        count = reconcile_persisted_hkjc_identities(config)
        if count:
            from .state import project_footbreak_execution_evidence
            project_footbreak_execution_evidence(config)
        _record_health(
            config, "COMPLETED", reconciled=count, stages=stages,
        )
    except BaseException as exc:
        _record_health(
            config, "FAILED", detail=type(exc).__name__, stages=stages,
        )
    finally:
        if timer_set:
            signal.setitimer(signal.ITIMER_REAL, 0)


def schedule_hkjc_identity_reconciliation(
    config: Settings,
    stages: list[str] | tuple[str, ...],
) -> bool:
    """Launch an isolated, hard-bounded post-commit identity worker.

    This is deliberately best effort.  A launch, provider, timeout, parser or
    lock failure cannot alter the result of the Crown pass that already
    committed its native state.
    """
    if os.getenv("CROWN_HKJC_IDENTITY_RECONCILE_ENABLED", "1").lower() in {
        "0", "false", "no", "off",
    }:
        return False
    eligible = tuple(
        stage for stage in stages if stage in {"首預", "T-30", "T-5"}
    )
    if not eligible or os.name != "posix":
        return False
    try:
        _record_health(config, "LAUNCHED", stages=eligible)
        context = multiprocessing.get_context("fork")
        child = context.Process(
            target=_worker,
            args=(config, eligible),
            name="crown-hkjc-identity",
        )
        child.daemon = False
        child.start()
    except BaseException as exc:
        _record_health(
            config, "LAUNCH_FAILED", detail=type(exc).__name__, stages=eligible,
        )
        return False
    return True
