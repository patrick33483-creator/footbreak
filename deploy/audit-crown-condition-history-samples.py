#!/usr/bin/env python3
"""List real Crown history rows belonging to one frozen Wilson condition."""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from analysis.granular_conditions import _descriptor, _paths, canonical_panels


def parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} is not a JSON object")
    return value


def frozen_condition(ledger: dict[str, Any], number: int) -> dict[str, Any]:
    registry = ledger.get("wilson_validation")
    if not isinstance(registry, dict):
        raise RuntimeError("missing wilson_validation registry")
    conditions = registry.get("conditions")
    if not isinstance(conditions, dict):
        raise RuntimeError("missing frozen conditions")
    matches = [
        value for value in conditions.values()
        if isinstance(value, dict) and int(value.get("condition_number") or 0) == number
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one condition #{number}, found {len(matches)}")
    return matches[0]


def target_key(system: str, definition: dict[str, Any]) -> tuple[str, ...]:
    values = [
        ("system", system),
        ("market", definition.get("market")),
        ("path", definition.get("path")),
        ("decision", definition.get("stage")),
        ("tier", definition.get("odds_tier")),
        ("direction", definition.get("direction")),
        ("role", definition.get("role")),
        ("bucket", definition.get("line_bucket")),
        ("movement", definition.get("movement")),
    ]
    return tuple(f"{key}={value}" for key, value in values if value not in (None, ""))


def rows_at_boundary(rows: list[dict[str, Any]], boundary: datetime | None) -> list[dict[str, Any]]:
    if boundary is None:
        return rows
    output = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kickoff = parse_time(row.get("kickoff") or row.get("kickoff_hkt"))
        if kickoff is not None and kickoff <= boundary:
            output.append(row)
    return output


def rows_verified_at_boundary(
    rows: list[dict[str, Any]], boundary: datetime | None
) -> list[dict[str, Any]]:
    if boundary is None:
        return rows
    output = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kickoff = parse_time(row.get("kickoff") or row.get("kickoff_hkt"))
        verified = parse_time(row.get("verified_at"))
        if kickoff is not None and kickoff <= boundary and verified is not None and verified <= boundary:
            output.append(row)
    return output


def matching_samples(
    rows: list[dict[str, Any]], system: str, wanted: tuple[str, ...]
) -> list[dict[str, Any]]:
    raw_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        fixture = str(row.get("match_id") or row.get("history_key") or "").strip()
        stage = str(row.get("stage") or "").strip()
        if not fixture or not stage:
            continue
        key = (fixture, stage)
        old = raw_rows.get(key)
        if old is None or str(row.get("predicted_at") or row.get("ts") or "") >= str(
            old.get("predicted_at") or old.get("ts") or ""
        ):
            raw_rows[key] = row

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for panel in canonical_panels(rows, settled_only=True):
        for path in _paths(panel):
            key, _label, _specificity = _descriptor(system, path, 2)
            if key != wanted:
                continue
            terminal = path[-1]
            fixture = str(panel["fixture"])
            if fixture in seen:
                continue
            seen.add(fixture)
            raw = raw_rows.get((fixture, str(terminal["stage"])), {})
            grade = next(
                (
                    item for item in raw.get("market_grades") or []
                    if isinstance(item, dict)
                    and str(item.get("code") or "").upper() == str(terminal["market"])
                    and str(item.get("side") or item.get("selection") or "").upper()
                    == str(terminal["side"])
                ),
                {},
            )
            output.append({
                "kickoff": (
                    terminal["kickoff"].isoformat()
                    if terminal.get("kickoff") is not None else raw.get("kickoff")
                ),
                "match_id": fixture,
                "league": raw.get("league"),
                "home": raw.get("home"),
                "away": raw.get("away"),
                "stage": terminal["stage"],
                "selection": terminal["role"],
                "line": terminal["selected_line"],
                "odds": terminal["odds"],
                "hit": terminal["hit"],
                "score": raw.get("score"),
                "result_status": raw.get("result_status"),
                "grade": copy.deepcopy(grade),
            })
    return sorted(output, key=lambda row: (str(row.get("kickoff") or ""), row["match_id"]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--condition-number", type=int, default=2)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    history = load(args.history)
    ledger = load(args.ledger)
    condition = frozen_condition(ledger, args.condition_number)
    definition = condition.get("definition")
    historical = condition.get("historical_evidence")
    if not isinstance(definition, dict) or not isinstance(historical, dict):
        raise RuntimeError("condition lacks frozen definition or historical evidence")
    artifact = historical.get("artifact") if isinstance(historical.get("artifact"), dict) else {}
    boundary_text = artifact.get("as_of") or condition.get("frozen_at")
    boundary = parse_time(boundary_text)
    rows = [row for row in history.get("rows") or [] if isinstance(row, dict)]
    wanted = target_key("crown", definition)

    current = matching_samples(rows, "crown", wanted)
    boundary_rows = rows_at_boundary(rows, boundary)
    frozen = matching_samples(boundary_rows, "crown", wanted)
    verified_frozen = matching_samples(
        rows_verified_at_boundary(rows, boundary), "crown", wanted
    )
    expected_hits = int(historical.get("hits"))
    expected_decided = int(historical.get("decided"))
    frozen_hits = sum(row["hit"] is True for row in frozen)
    verified_frozen_hits = sum(row["hit"] is True for row in verified_frozen)
    current_hits = sum(row["hit"] is True for row in current)

    report = {
        "condition_number": args.condition_number,
        "definition": definition,
        "target_key": list(wanted),
        "artifact_as_of": boundary_text,
        "frozen_at": condition.get("frozen_at"),
        "frozen_registry": {
            "hits": expected_hits,
            "decided": expected_decided,
        },
        "reconstructed_at_boundary": {
            "hits": frozen_hits,
            "decided": len(frozen),
            "matches_registry": (
                frozen_hits == expected_hits and len(frozen) == expected_decided
            ),
        },
        "reconstructed_verified_at_boundary": {
            "hits": verified_frozen_hits,
            "decided": len(verified_frozen),
            "matches_registry": (
                verified_frozen_hits == expected_hits
                and len(verified_frozen) == expected_decided
            ),
        },
        "current_history_same_definition": {
            "hits": current_hits,
            "decided": len(current),
        },
        "prospective": copy.deepcopy(condition.get("prospective")),
        "active_evidence": copy.deepcopy(condition.get("active_evidence")),
        "active_evidence_version": condition.get("active_evidence_version"),
        "evidence_versions": copy.deepcopy(condition.get("evidence_versions")),
        "samples": frozen[: max(0, args.limit)],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
