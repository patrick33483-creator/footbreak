#!/usr/bin/env python3
"""Read-only inventory of persisted Crown artifacts that may contain full odds boards."""

from __future__ import annotations

import collections
import json
from pathlib import Path
from typing import Any, Iterable


ROOTS = [
    Path("/var/lib/footbreak/crown"),
]
MAX_FILES = 10000
MAX_BYTES = 256 * 1024 * 1024


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


def walk_paths(value: Any, path: str = "$", depth: int = 0) -> Iterable[tuple[str, dict[str, Any]]]:
    if depth > 9:
        return
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from walk_paths(child, f"{path}.{key}", depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_paths(child, f"{path}[{index}]", depth + 1)


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
    file_inventory: list[dict[str, Any]] = []
    interesting_samples: list[dict[str, Any]] = []
    snapshot_schema_samples: list[dict[str, Any]] = []

    for path in files:
        counters["candidate_files"] += 1
        try:
            size = path.stat().st_size
            counters["candidate_bytes"] += size
            file_inventory.append({"name": path.name, "bytes": size})
            if size > MAX_BYTES:
                counters["oversize_files"] += 1
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            counters["unreadable_files"] += 1
            continue
        counters["readable_files"] += 1
        top_level_shapes[type(data).__name__] += 1
        if (
            "__T-5" in path.name
            and len(snapshot_schema_samples) < 8
            and isinstance(data, dict)
        ):
            relevant_nodes = []
            for node_path, node in walk_paths(data):
                keys = set(node)
                if keys & {"HIL", "odds", "markets", "market_predictions"}:
                    relevant_nodes.append({
                        "path": node_path,
                        "keys": sorted(str(key) for key in keys)[:60],
                        "values": {
                            key: node.get(key)
                            for key in (
                                "match_id", "fixture_id", "titan_match_id", "stage",
                                "saved_at", "observed_at", "ts", "source_snapshot_at",
                                "market", "code", "line", "condition", "side", "selection", "odds",
                            )
                            if key in node and isinstance(node.get(key), (str, int, float, bool, type(None)))
                        },
                        "complex_types": {
                            key: type(node.get(key)).__name__
                            for key in ("HIL", "odds", "markets", "market_predictions")
                            if key in node and isinstance(node.get(key), (dict, list))
                        },
                    })
                if len(relevant_nodes) >= 20:
                    break
            snapshot_schema_samples.append({
                "file": path.name,
                "top_keys": sorted(str(key) for key in data)[:80],
                "top_values": {
                    key: data.get(key)
                    for key in ("match_id", "fixture_id", "titan_match_id", "stage", "kickoff_hkt", "created_at", "updated_at")
                    if key in data and isinstance(data.get(key), (str, int, float, bool, type(None)))
                },
                "relevant_nodes": relevant_nodes,
            })
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
            keys = set(node)
            if (
                len(interesting_samples) < 30
                and keys & {"odds", "markets", "market_lines", "raw_odds", "current_odds"}
                and keys & {"match_id", "fixture_id", "titan_match_id", "stage", "code", "market"}
            ):
                interesting_samples.append({
                    "file": path.name,
                    "keys": sorted(str(key) for key in keys)[:50],
                    "stage": node.get("stage"),
                    "market": node.get("market") or node.get("code"),
                    "line": node.get("line") or node.get("condition"),
                    "side": node.get("side"),
                    "odds_type": type(node.get("odds")).__name__,
                    "markets_type": type(node.get("markets")).__name__,
                })
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
        "file_inventory": sorted(file_inventory, key=lambda row: (-row["bytes"], row["name"]))[:100],
        "interesting_samples": interesting_samples,
        "snapshot_schema_samples": snapshot_schema_samples,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
