#!/usr/bin/env python3
"""Run the formal Crown condition #10 recovery engine in audit or apply mode."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from analysis import audit_crown_condition5_history as audit
from analysis import recover_crown_condition5_history as engine


SIGNATURE = "f956f75e552c8de37b0f2656"


def configure() -> None:
    engine.SIGNATURE = SIGNATURE
    engine.MIGRATION = "crown-condition10-t30-missed-admission-v1"
    engine.MIGRATION_FIELD = "condition10_history_recovery_v1"
    engine.RECOVERY_REASON = "condition10_missed_admission_recovery"
    engine.RECOVERY_ACTION = "條件 #10 漏入組修復：套用既有正常賽果"
    engine._condition = lambda ledger: audit._condition(ledger, 10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    configure()
    ledger = engine._read(args.ledger)
    result = engine.recover(
        ledger, engine._read(args.history), apply=args.apply,
    )
    if args.apply:
        engine._write_atomic(args.ledger, ledger)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
