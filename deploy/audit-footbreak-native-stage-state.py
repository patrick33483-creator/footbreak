#!/usr/bin/env python3
"""Read-only Footbreak-native T-30/T-5 manifest completeness projection."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEM = ROOT / "system"
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

from native_stage_state import HKT, completeness_projection  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger-path", default="/opt/footbreak/system/sim_ledger.json")
    args = parser.parse_args()
    try:
        ledger = json.loads(Path(args.ledger_path).read_text(encoding="utf-8"))
        if not isinstance(ledger, dict):
            raise ValueError("ledger_not_object")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "reason": f"ledger_unreadable:{type(exc).__name__}"}, ensure_ascii=False))
        return 2
    print(json.dumps(
        {"ok": True, **completeness_projection(ledger, now=datetime.now(HKT))},
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
