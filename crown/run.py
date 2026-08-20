"""Crown command entrypoint.  Dry runs never make a remote request."""
from __future__ import annotations

import argparse
import multiprocessing
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
_TICK_POSTPROCESS_MIN_SECONDS = 2.0
_TICK_POSTPROCESS_MAX_SECONDS = 4.0
_TICK_POSTPROCESS_TEARDOWN_MARGIN_SECONDS = 1.0
_TICK_NOTIFICATION_MIN_SECONDS = 6.0


def _tick_postprocess_process(send, config, ledger) -> None:
    """Run optional local tick publication outside the deadline-owning parent."""
    try:
        archive_watch_fast(config, ledger)
    except BaseException as exc:
        send.send(("history_error", type(exc).__name__))
    else:
        try:
            write_tick_dashboard_projection(config, ledger)
        except BaseException as exc:
            send.send(("dashboard_error", type(exc).__name__))
        else:
            send.send(("complete", None))
    finally:
        send.close()


def _run_tick_postprocess(config, ledger, max_seconds: float) -> tuple[str, str | None]:
    """Hard-bound optional archive/projection and preserve parent teardown room.

    Tick persistence and outbox delivery are already complete by this point.
    History/card publication is therefore deliberately fail-closed: it can be
    deferred to sweep rather than make the time-critical service wait through
    concurrent settlement or filesystem work.  A process is required because
    a blocked local write/serialization cannot be safely cancelled in a thread.
    """
    if max_seconds <= 0.0 or os.name != "posix":
        return "deferred", None
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_tick_postprocess_process,
        args=(sender, config, ledger),
    )
    process.start()
    sender.close()
    try:
        if not receiver.poll(max_seconds):
            return "deferred", None
        try:
            status, detail = receiver.recv()
        except EOFError:
            return "history_error", "worker_exited"
        return (
            str(status),
            str(detail) if detail is not None else None,
        )
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=0.05)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.05)


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
    if args.mode == "tick":
        try:
            # The durable outbox is persistence-first and performs its own
            # pre-kickoff/staleness/idempotency checks. Deliver it before any
            # optional archive or dashboard work: a fast no-op still retries
            # an unacknowledged Wilson T-30/T-5 opportunity, and a slow local
            # projection can never starve its one bounded transport attempt.
            remaining = tick_deadline - time.monotonic()
            if remaining >= _TICK_NOTIFICATION_MIN_SECONDS:
                notify_new(
                    ledger,
                    config,
                    result.get("fresh_condition_predictions") or [],
                    max_attempts=1,
                    max_seconds=min(5.0, remaining - 1.0),
                )
            else:
                result["notification_warning"] = "telegram_deferred_tick_deadline"
        except Exception as exc:
            # Signals are notification-only. A transport failure leaves the
            # durable outbox unacknowledged for the next safe tick.
            result["notification_warning"] = f"telegram_{type(exc).__name__}"
    try:
        if args.mode == "tick":
            # Tick has a sub-minute service cap.  Archive its committed stage
            # immediately, but leave provider result sync, grading, aggregate
            # stats, and granular mining to sweep/settle.  Publish only the
            # committed local card/ledger projection so the public board does
            # not falsely lag a native T-5 until the next full sweep.
            remaining = tick_deadline - time.monotonic()
            if remaining >= _TICK_POSTPROCESS_MIN_SECONDS:
                # This work is local but not assumed to be instantaneous: at
                # :00 it can overlap settlement and a still-running :50 sweep.
                # Reserve a final parent teardown second even when the child
                # must be SIGTERM/SIGKILLed after a blocked write.
                postprocess_budget = min(
                    _TICK_POSTPROCESS_MAX_SECONDS,
                    max(0.0, remaining - _TICK_POSTPROCESS_TEARDOWN_MARGIN_SECONDS),
                )
                status, detail = _run_tick_postprocess(
                    config, ledger, postprocess_budget,
                )
                if status == "complete":
                    result["dashboard_projection"] = "post_tick_local_state"
                elif status == "dashboard_error":
                    result["dashboard_projection_warning"] = (
                        f"dashboard_projection_{detail or 'worker_error'}"
                    )
                elif status == "history_error":
                    history_warning = f"prediction_history_{detail or 'worker_error'}"
                else:
                    result["dashboard_projection_warning"] = "deferred_tick_deadline"
            else:
                result["dashboard_projection_warning"] = "deferred_tick_deadline"
        elif args.mode == "sweep":
            # Settlement already fetches one bounded, exact-ID provider
            # snapshot.  Defer history grading to sweep rather than repeat
            # Titan/HKJC result reads in the same settlement invocation.
            update_history(config, ledger)
    except Exception as exc:
        history_warning = f"prediction_history_{type(exc).__name__}"
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
