#!/usr/bin/env python3
"""Read-only inventory of persisted Crown artifacts that may contain full odds boards."""

from __future__ import annotations

import collections
import json
from pathlib import Path
from typing import Any, Iterable


ROOTS = [
    Path("/var/lib/footbreak/crown/source_snapshots"),
    Path("/var/lib/footbreak/crown/ledger.json"),
    Path("/var/lib/footbreak/crown/prediction_history.json"),
]
MAX_FILES = 10000
MAX_BYTES = 64 * 1024 * 1024


def walk(value: Any, depth: int = 0) -> Iterable[dict[str, Any]]:
    if depth > 7:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child, depth + 1)


def board_shape(node: dict[str, Any]) -> tuple[str, int, int] | None:
    for field in ("hk_odds", "crown_odds", "odds", "markets", "odds_board", "market_lines"):
        board = node.get(field)
        if not isinstance(board, (dict, list)):
            continue
        hil_lines = 0
        hil_two_sided = 0
        candidates = []
        if isinstance(board, dict):
            for code, lines in board.items():
                if str(code).upper() in {"HIL", "OU", "TOTAL", "TOTALS"}:
                    candidates.extend(lines if isinstance(lines, list) else [lines])
        else:
            candidates = board
        for item in candidates:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or item.get("market") or "").upper()
            if code and code not in {"HIL", "OU", "TOTAL", "TOTALS"}:
                continue
            prices = item.get("odds") or item.get("prices") or item.get("selections")
            if isinstance(prices, dict):
                keys = {str(key).upper() for key in prices}
                if keys & {"H", "O", "OVER"} and keys & {"L", "U", "UNDER"}:
                    hil_two_sided += 1
                hil_lines += 1
            elif isinstance(prices, list):
                sides = {
                    str(x.get("side") or x.get("selection") or "").upper()
                    for x in prices if isinstance(x, dict)
                }
                if sides & {"H", "O", "OVER"} and sides & {"L", "U", "UNDER"}:
                    hil_two_sided += 1
                hil_lines += 1
        return field, hil_lines, hil_two_sided
    return None


def main() -> None:
    files = []
    for root in ROOTS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(path for path in root.rglob("*.json") if path.is_file()))
    files = files[:MAX_FILES]

    counters: collections.Counter[str] = collections.Counter()
    field_counts: collections.Counter[str] = collections.Counter()
    top_level_shapes: collections.Counter[str] = collections.Counter()
    board_fields: collections.Counter[str] = collections.Counter()
    board_file_names: collections.Counter[str] = collections.Counter()
    sample_node_keys: dict[str, list[str]] = {}

    for path in files:
        counters["candidate_files"] += 1
        try:
            size = path.stat().st_size
            counters["candidate_bytes"] += size
            if size > MAX_BYTES:
                counters["oversize_files"] += 1
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            counters["unreadable_files"] += 1
            continue
        counters["readable_files"] += 1
        top_level_shapes[type(data).__name__] += 1
        file_has_board = False
        for node in walk(data):
            counters["dict_nodes"] += 1
            for key in node:
                field_counts[str(key)] += 1
            shape = board_shape(node)
            if shape is None:
                continue
            field, hil_lines, hil_two_sided = shape
            board_fields[field] += 1
            counters["hil_lines"] += hil_lines
            counters["hil_two_sided_lines"] += hil_two_sided
            if hil_lines:
                file_has_board = True
                sample_node_keys.setdefault(field, sorted(str(key) for key in node)[:30])
        if file_has_board:
            counters["files_with_hil_board"] += 1
            board_file_names[path.name] += 1

    print(json.dumps({
        "roots_present": {
            root.name: {"exists": root.exists(), "is_dir": root.is_dir()}
            for root in ROOTS
        },
        "counts": dict(counters),
        "top_level_shapes": dict(top_level_shapes),
        "board_fields": dict(board_fields),
        "top_fields": dict(field_counts.most_common(80)),
        "board_file_names": dict(board_file_names.most_common(30)),
        "sample_node_keys_by_board_field": sample_node_keys,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
