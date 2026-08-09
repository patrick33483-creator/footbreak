"""Crown command entrypoint.  Dry runs never make a remote request."""
from __future__ import annotations

import argparse
import os

from .config import settings
from .dashboard_data import write_dashboard_data
from .engine import run
from .notify import notify_new
from .prediction_history import update_history
from .state import load_ledger


def _ensure_dashboard_path(path) -> None:
    """Keep the static web tree readable without relaxing private state files."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o755)
    parent = path.parent
    if parent.exists():
        os.chmod(parent, 0o755)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("tick", "sweep", "settle", "health"))
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
    try:
        result = run(args.mode, config)
    except Exception as exc:
        # Do not serialize an upstream response (which can contain unwanted
        # detail); a failed pass must be visible and must not create a bet.
        print({"ok": False, "mode": args.mode, "reason": f"upstream_{type(exc).__name__}"})
        return 4
    if not result.get("ok"):
        print(result)
        return 3
    if result.get("fast_noop") and args.mode != "settle":
        print(result)
        return 0
    ledger = load_ledger(config)
    history_warning = None
    try:
        update_history(config, ledger)
    except Exception as exc:
        history_warning = f"prediction_history_{type(exc).__name__}"
    notify_new(ledger, config)
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
