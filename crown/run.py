"""Crown command entrypoint.  Dry runs never make a remote request."""
from __future__ import annotations

import argparse
import multiprocessing
import os
import time
from pathlib import Path
import traceback

from .config import settings
from .common import iso_hkt, write_json_atomic
from .dashboard_data import write_dashboard_data, write_tick_dashboard_projection
from .engine import _tick_pass_deadline_seconds, run
from .notify import notify_new
from .prediction_history import archive_watch_fast, update_history
from .state import load_ledger, schedule_footbreak_execution_evidence_projection

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_TICK_POSTPROCESS_MIN_SECONDS = 2.0
_TICK_POSTPROCESS_MAX_SECONDS = 4.0
_TICK_POSTPROCESS_TEARDOWN_MARGIN_SECONDS = 1.0
_TICK_NOTIFICATION_MIN_SECONDS = 6.0
_TICK_ENGINE_TEARDOWN_MARGIN_SECONDS = 0.25


def _write_tick_health(config, result: dict[str, object]) -> None:
    """Persist the authoritative, small tick outcome for the local monitor.

    This is status only: it never changes the ledger, stage history, odds, or
    notifications.  In particular an internal deadline defer becomes visible
    to the monitor even though the bounded tick itself exits successfully.
    """
    write_json_atomic(config.state_dir / "tick-health.json", {
        "at": iso_hkt(),
        "engine_warning": str(result.get("engine_warning") or ""),
        "ok": bool(result.get("ok")),
        "mode": "tick",
    })


def _tick_notification_process(
    send, config, fresh_predictions, notification_deadline: float,
) -> None:
    """Load the authoritative outbox and attempt one send within one deadline."""
    try:
        ledger = load_ledger(config)
        remaining = max(0.0, notification_deadline - time.monotonic())
        if remaining < 0.25:
            send.send(("deferred", None))
        else:
            delivered = notify_new(
                ledger,
                config,
                fresh_predictions,
                max_attempts=1,
                max_seconds=min(5.0, remaining),
            )
            send.send(("complete", int(delivered)))
    except BaseException as exc:
        send.send(("error", type(exc).__name__))
    finally:
        send.close()


def _run_tick_notification(
    config, fresh_predictions, max_seconds: float,
) -> tuple[str, str | None]:
    """Hard-bound the second ledger read and the Telegram transport together."""
    if max_seconds <= 0.0:
        return "deferred", None
    if os.name != "posix":
        try:
            ledger = load_ledger(config)
            notify_new(
                ledger, config, fresh_predictions,
                max_attempts=1, max_seconds=min(5.0, max_seconds),
            )
        except BaseException as exc:
            return "error", type(exc).__name__
        return "complete", None
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    deadline = time.monotonic() + max_seconds
    process = context.Process(
        target=_tick_notification_process,
        args=(sender, config, fresh_predictions, deadline),
    )
    process.start()
    sender.close()
    try:
        if not receiver.poll(max_seconds):
            return "deferred", None
        try:
            status, detail = receiver.recv()
        except EOFError:
            return "error", "WorkerExited"
        return str(status), str(detail) if detail is not None else None
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=0.10)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.10)


def _tick_engine_process(send, config, tick_deadline: float) -> None:
    """Run all deadline-sensitive tick work behind a killable wall clock."""
    try:
        send.send(("complete", run(
            "tick", config, tick_pass_deadline=tick_deadline,
        )))
    except BaseException as exc:
        send.send(("error", {
            "type": type(exc).__name__,
            "origin": _exception_origin(exc),
        }))
    finally:
        send.close()


def _run_tick_engine(config, tick_deadline: float) -> dict[str, object]:
    """Bound state reads, provider work and persistence before TG reserve.

    Atomic state writes remain authoritative if the worker reaches a commit.
    If any local read, provider call or lock stalls, the parent terminates the
    worker at the native-stage cutoff; any consumer is deferred rather than
    reducing this pre-kickoff evidence window.
    """
    # Native stage durability is the critical path.  Consumers such as the
    # Telegram transport are attempted below only from actual remaining time;
    # never reserve time for them by truncating a due T-30/T-5 collection.
    provider_cutoff = tick_deadline
    max_seconds = max(
        0.0,
        provider_cutoff - time.monotonic() - _TICK_ENGINE_TEARDOWN_MARGIN_SECONDS,
    )
    if max_seconds <= 0.0 or os.name != "posix":
        return {
            "ok": True,
            "mode": "tick",
            "engine_warning": "deferred_tick_deadline",
            "predictions": 0,
            "simulations_created": 0,
        }
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_tick_engine_process,
        args=(sender, config, tick_deadline),
    )
    process.start()
    sender.close()
    try:
        if not receiver.poll(max_seconds):
            # Do not leave a write-ahead STARTED record as the only evidence
            # when a same-minute provider batch overruns its native budget.
            # Kill that child first, then persist retryable DATA_MISSING rows
            # using only its already durable fixture identities.
            if process.is_alive():
                process.terminate()
                process.join(timeout=0.10)
            from .engine import persist_timed_stage_timeout_failures
            failures = persist_timed_stage_timeout_failures(config)
            return {
                "ok": True,
                "mode": "tick",
                "engine_warning": "deferred_tick_deadline",
                "predictions": 0,
                "simulations_created": 0,
                "timeout_failure_snapshots": failures,
            }
        try:
            status, payload = receiver.recv()
        except EOFError:
            status, payload = "error", {
                "type": "WorkerExited",
                "origin": {"type": "WorkerExited"},
            }
        if status == "complete" and isinstance(payload, dict):
            return payload
        return {
            "ok": False,
            "mode": "tick",
            "reason": f"upstream_{payload.get('type', 'WorkerError')}",
            "exception_origin": payload.get("origin", {}),
        }
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=0.10)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.10)


def _tick_postprocess_process(send, config) -> None:
    """Run optional local tick publication outside the deadline-owning parent."""
    try:
        ledger = load_ledger(config)
    except BaseException as exc:
        send.send(("history_error", type(exc).__name__))
    else:
        try:
            write_tick_dashboard_projection(config, ledger)
        except BaseException as exc:
            send.send(("dashboard_error", type(exc).__name__))
        else:
            # The native T-30/T-5 snapshot is already durable before this
            # child begins.  Publish that small projection first: archiving a
            # large local history document is optional and must not leave the
            # browser on an old card that falsely reports the stage missing.
            # The parent may terminate a slow archive at its postprocess
            # deadline, but the atomic dashboard replacement has already
            # completed and never consumes Telegram's earlier reservation.
            try:
                archive_watch_fast(config, ledger)
            except BaseException as exc:
                send.send(("history_error", type(exc).__name__))
            else:
                send.send(("complete", None))
    finally:
        send.close()


def _run_tick_postprocess(config, max_seconds: float) -> tuple[str, str | None]:
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
        args=(sender, config),
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
    parser.add_argument(
        "mode",
        choices=(
            "tick", "sweep", "round-update", "first-look-reconcile",
            "settle", "refresh", "health",
        ),
    )
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
        result = (
            _run_tick_engine(config, tick_deadline)
            if args.mode == "tick"
            else run(args.mode, config)
        )
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
    ledger = load_ledger(config) if args.mode != "tick" else None
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
                status, detail = _run_tick_notification(
                    config,
                    result.get("fresh_condition_predictions") or [],
                    min(5.5, remaining - 1.0),
                )
                if status == "deferred":
                    result["notification_warning"] = "telegram_deferred_tick_deadline"
                elif status == "error":
                    result["notification_warning"] = f"telegram_{detail or 'WorkerError'}"
            else:
                result["notification_warning"] = "telegram_deferred_tick_deadline"
        except Exception as exc:
            # Signals are notification-only. A transport failure leaves the
            # durable outbox unacknowledged for the next safe tick.
            result["notification_warning"] = f"telegram_{type(exc).__name__}"
        # The native stage, ledger and prediction card are already durable.
        # Launch the optional persisted-state sidecar only after the bounded
        # Telegram attempt, never from the deadline-owned engine child and
        # never as a prerequisite for a native observation or notification.
        # The helper only reads local persisted state; it performs no HKJC or
        # Crown provider request and is independently wall-clock bounded.
        schedule_footbreak_execution_evidence_projection(
            config, result.get("evidence_projection_stages") or [],
        )
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
                    config, postprocess_budget,
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
    if args.mode == "tick":
        try:
            _write_tick_health(config, result)
        except OSError:
            # A separate disk-pressure finding covers a filesystem failure;
            # do not turn the simulation worker into a retry/backfill path.
            result["tick_health_warning"] = "unpersisted"
    else:
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
