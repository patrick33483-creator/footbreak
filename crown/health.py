"""Offline Crown health and configuration report.  No network activity."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .common import iso_hkt, write_json_atomic
from .config import settings
from .state import paths


def main() -> int:
    config = settings()
    report = {
        "checked_at": iso_hkt(), "ok": True, "enabled": config.enabled, "pinnapi_configured": config.pinnapi_configured,
        "telegram_enabled": config.telegram_enabled, "real_betting_enabled": False,
        "state_dir": str(config.state_dir), "web_root": str(config.web_root),
        "checks": {
            "separate_state": config.state_dir != Path("/var/lib/footbreak"),
            "pinnapi_required_for_remote_run": True,
            "timer_validation_gate": not config.enabled,
            "telegram_default_off": not config.telegram_enabled,
            "traditional_simplified_converter": bool(shutil.which("uconv")),
            "company_id": config.titan_company_id,
        },
    }
    config.state_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(paths(config)["health"], report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
