#!/usr/bin/env python3
"""Build the reviewed condition #6 result manifest from the research report."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from crown.lines import settle_handicap


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--pending", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    text = args.report.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if len(blocks) != 1:
        raise ValueError("research report must contain exactly one JSON block")
    research = json.loads(blocks[0])
    pending = json.loads(args.pending.read_text(encoding="utf-8"))
    identities = {str(row["fixture"]): row for row in pending}
    if len(identities) != 58:
        raise ValueError("pending inventory must contain 58 unique fixtures")

    output = {
        "schema_version": 1,
        "condition_number": 6,
        "condition_signature": "09ba238cb8400670519ce95a",
        "decision_stage": "T-30",
        "market": "HDC",
        "selection": "H",
        "verified_at": research["verified_at"],
        "score_scope": research["score_scope"],
        "results": [],
        "postponed_pending": [],
    }
    seen: set[str] = set()
    for result in research["results"]:
        fixture = str(result["fixture_id"])
        if fixture in seen or fixture not in identities:
            raise ValueError(f"unexpected or duplicate fixture: {fixture}")
        seen.add(fixture)
        identity = identities[fixture]
        sources = result.get("sources")
        if (
            not isinstance(sources, list)
            or len(sources) < 2
            or not all(str(url).startswith(("http://", "https://")) for url in sources)
        ):
            raise ValueError(f"two HTTP result sources required: {fixture}")
        if result["status"] == "postponed":
            output["postponed_pending"].append({
                "match_id": fixture,
                "reason": "postponed_no_result",
                "sources": sources,
                "notes": result.get("notes"),
            })
            continue
        if result["status"] != "completed":
            raise ValueError(f"unsupported result status for {fixture}")
        home_score = int(result["home_score"])
        away_score = int(result["away_score"])
        line = float(identity["t30"]["selected_line"])
        output["results"].append({
            "match_id": fixture,
            "kickoff_hkt": identity["kickoff_hkt"],
            "league": identity.get("league"),
            "home": identity.get("home"),
            "away": identity.get("away"),
            "line": line,
            "home_score": home_score,
            "away_score": away_score,
            "expected_result": settle_handicap(
                line, "H", home_score, away_score
            ),
            "sources": sources,
            "notes": result.get("notes"),
        })
    if len(seen) != 58 or len(output["results"]) != 57:
        raise ValueError("manifest must contain 57 results and one postponed fixture")
    if [row["match_id"] for row in output["postponed_pending"]] != ["3072870"]:
        raise ValueError("unexpected postponed fixture")
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
