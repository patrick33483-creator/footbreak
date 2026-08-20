"""Crown command entrypoint.  Dry runs never make a remote request."""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
import traceback

from .config import settings
from .dashboard_data import write_dashboard_data, write_tick_dashboard_projection
from .engine import _tick_pass_deadline_seconds, run
from .notify import notify_new
from .prediction_history import archive_watch_fast, update_history
from .state import load_ledger

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _exception_origin(exc: BaseException) -> dict[str, object]:
    """Return only a repository-relative failure origin, never exception data."""
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        try:
            module = Path(frame.filename).resolve().relative_to(_REPOSITORY_ROOT)
        except (OSError, ValueError):
            continue
        return {
            "type": type(exc).__name__,
            "module": module.as_posix(),
            "function": frame.name,
            "line": frame.lineno,
        }
    return {"type": type(exc).__name__}


def _ensure_dashboard_path(path) -> None:
    """Keep the static web tree readable without relaxing private state files."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o755)
    parent = path.parent
    if parent.exists():
        os.chmod(parent, 0o755)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("tick", "sweep", "settle", "refresh", "health"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = settings()
    _ensure_dashboard_path(config.web_root)
    if args.dry_run:
        print({"ok": True, "dry_run": True, "mode": args.mode, "enabled": config.enabled,
               "pinnapi_configured": config.pinnapi_configured, "real_betting_enabled": False})
        return 0
    if args.mode == "health":
        print({"ok": True, "enabled": config.enabled, "pinnapi_configured": config.pinnapi_configured,
               "telegram_enabled": config.telegram_enabled, "real_betting_enabled": False})
        return 0
    tick_deadline = (
        time.monotonic() + _tick_pass_deadline_seconds()
        if args.mode == "tick" else None
    )
    try:
        result = run(args.mode, config)
    except Exception as exc:
        # Do not serialize an upstream response (which can contain unwanted
        # detail); a failed pass must be visible and must not create a bet.
        print({
            "ok": False,
            "mode": args.mode,
            "reason": f"upstream_{type(exc).__name__}",
            "exception_origin": _exception_origin(exc),
        })
        return 4
    if not result.get("ok"):
        print(result)
        return 3
    # `refresh` changes only current, not-yet-kicked-off dashboard quote
    # fields.  It must never replay a stage, touch history, or contact
    # Telegram.
    if args.mode == "refresh":
        write_dashboard_data(config)
        print(result)
        return 0
    ledger = load_ledger(config)
    history_warning = None
    try:
        if args.mode == "tick":
            # Tick has a sub-minute service cap.  Archive its committed stage
            # immediately, but leave provider result sync, grading, aggregate
            # stats, and granular mining to sweep/settle.  Publish only the
            # committed local card/ledger projection so the public board does
            # not falsely lag a native T-5 until the next full sweep.
            archive_watch_fast(config, ledger)
            try:
                write_tick_dashboard_projection(config, ledger)
                result["dashboard_projection"] = "post_tick_local_state"
            except Exception as exc:
                # The persisted stage remains the source of truth and will be
                # projected by the next sweep.  A display write must never
                # turn a successful deadline-bound prediction into a failure.
                result["dashboard_projection_warning"] = (
                    f"dashboard_projection_{type(exc).__name__}"
                )
        elif args.mode == "sweep":
            # Settlement already fetches one bounded, exact-ID provider
            # snapshot.  Defer history grading to sweep rather than repeat
            # Titan/HKJC result reads in the same settlement invocation.
            update_history(config, ledger)
    except Exception as exc:
        history_warning = f"prediction_history_{type(exc).__name__}"
    try:
        # A notification is retryable from persisted state.  Do not start a
        # five-second Telegram transport when the tick's shared pass budget
        # has expired; this preserves the next T-30/T-5 timer slot.
        remaining = (
            tick_deadline - time.monotonic()
            if tick_deadline is not None else None
        )
        if remaining is None or remaining >= 0.25:
            notify_new(
                ledger,
                config,
                result.get("fresh_condition_predictions") or [],
                max_attempts=1 if args.mode == "tick" else None,
                max_seconds=remaining,
            )
        elif args.mode == "tick":
            result["notification_warning"] = "telegram_deferred_tick_deadline"
    except Exception as exc:
        # Signals are notification-only.  A transport failure must never roll
        # back or corrupt the already committed live prediction state.
        result["notification_warning"] = f"telegram_{type(exc).__name__}"
    if args.mode != "tick":
        write_dashboard_data(config)
    if history_warning:
        result["warning"] = history_warning
        if args.mode == "settle":
            print(result)
            return 5
    print(result)
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
