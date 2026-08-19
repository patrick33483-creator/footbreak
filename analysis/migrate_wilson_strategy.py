"""One-time-safe, rerunnable ledger migration for Wilson 測試攻略.

Usage (offline only): ``python -m analysis.migrate_wilson_strategy LEDGER SYSTEM``.
It only adds the versioned namespace and one read-only v1 archive snapshot;
existing bets and settlement data are never rewritten or removed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .wilson_validation import migrate_ledger, recompute_namespace


def migrate_file(path: Path, system: str) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(payload, dict):
        raise ValueError("ledger must contain a JSON object")
    migrate_ledger(payload, system)
    recompute_namespace(payload, system)
    temporary = path.with_suffix(path.suffix + ".wilson.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Idempotently add Wilson simulation namespace")
    parser.add_argument("ledger", type=Path)
    parser.add_argument("system", choices=("footbreak", "crown"))
    args = parser.parse_args()
    migrate_file(args.ledger, args.system)


if __name__ == "__main__":
    main()
