#!/usr/bin/env python3
"""Apply a reviewed result batch to a Crown production prediction history.

Uses the audit-only ``audit_verified_result_backfill`` builder to construct the
proposed rows, verifies identity, and only then rewrites the production history
atomically.  Manual overrides are impossible: every row must be an exact match
on ``match_id``, ``league``, ``home``, ``away`` and kickoff date, and any pre-
existing final settlement that does not match raises before any bytes touch
production.  A pre-write backup is written next to the history file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.audit_verified_result_backfill import (
    DEFAULT_BATCH_ID,
    DEFAULT_VERIFIED_AT,
    build_proposal,
)
from crown.state import write_json_atomic


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True, type=Path,
                        help="Production prediction_history.json to update in place.")
    parser.add_argument("--verified-results", required=True, type=Path,
                        help="Gemini-verified match manifest.")
    parser.add_argument("--batch-id", default=DEFAULT_BATCH_ID)
    parser.add_argument("--verified-at", default=DEFAULT_VERIFIED_AT)
    parser.add_argument("--report-out", type=Path,
                        help="Write the apply report to this path.")
    parser.add_argument("--backup-dir", type=Path,
                        help="Directory to store the pre-write backup. "
                        "Defaults to the history parent directory.")
    parser.add_argument("--allow-empty", action="store_true",
                        help="Do not fail when no rows would change.")
    args = parser.parse_args()

    history_path: Path = args.history
    if not history_path.is_file():
        raise SystemExit(f"history file missing: {history_path}")
    verified_path: Path = args.verified_results
    if not verified_path.is_file():
        raise SystemExit(f"verified results missing: {verified_path}")

    before_sha = _sha256(history_path)
    history = json.loads(history_path.read_text(encoding="utf-8"))
    verified = json.loads(verified_path.read_text(encoding="utf-8"))

    proposed, report = build_proposal(
        history,
        verified,
        batch_id=args.batch_id,
        verified_at=args.verified_at,
    )
    if report["changed_rows"] == 0 and not args.allow_empty:
        raise SystemExit(
            f"no rows would change (already_rows={report['already_rows']}); "
            f"refuse to touch production without --allow-empty"
        )

    backup_dir = args.backup_dir or history_path.parent
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    backup_path = backup_dir / f"{history_path.name}.before-{args.batch_id}-{stamp}"
    shutil.copy2(history_path, backup_path)
    backup_sha = _sha256(backup_path)
    if backup_sha != before_sha:
        raise SystemExit("backup sha256 mismatch; aborting before write")

    write_json_atomic(history_path, proposed)
    after_sha = _sha256(history_path)

    report.update({
        "history_path": str(history_path),
        "backup_path": str(backup_path),
        "before_sha256": before_sha,
        "after_sha256": after_sha,
        "backup_sha256": backup_sha,
        "applied_at": datetime.now(timezone.utc).isoformat(),
    })
    if args.report_out:
        write_json_atomic(args.report_out, report)

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
