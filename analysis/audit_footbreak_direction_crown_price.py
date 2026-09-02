#!/usr/bin/env python3
"""Use Footbreak T-5 HIL direction and settle at a higher matched Crown price."""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import math
import random
import re
import statistics
import unicodedata
from pathlib import Path
from typing import Any


FOOTBREAK_HISTORY = Path("/opt/footbreak/system/accuracy_history.json")
CROWN_HISTORY = Path("/var/lib/footbreak/crown/prediction_history.json")


def parse_time(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    return parsed.astimezone(dt.timezone.utc)


def norm_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def line_key(value: Any) -> float | None:
    parsed = number(value)
    return None if parsed is None else round(parsed * 4) / 4


def side_key(value: Any) -> str | None:
    return {"H": "O", "O": "O", "L": "U", "U": "U"}.get(str(value or "").upper())


def excluded(row: dict[str, Any]) -> bool:
    for key in (
        "backfill", "post_hoc_backfill", "post_hoc", "recovered", "recovery_mode",
        "settlement_excluded", "exclude_from_settlement",
        "exclude_from_primary_statistics",
    ):
        if row.get(key) not in (None, False, "", 0, "native"):
            return True
    return str(row.get("prediction_origin") or "native").lower() not in {"", "native", "live"}


def source_rows(path: Path, system: str) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if system == "footbreak":
        output = []
        for match in payload.get("matches") or []:
            if not isinstance(match, dict) or excluded(match):
                continue
            for stage in match.get("stages") or []:
                if isinstance(stage, dict):
                    output.append({**match, **stage})
        return output
    return [row for row in payload.get("rows") or [] if isinstance(row, dict)]


def return_from_settlement(settlement: str, odds: float) -> float | None:
    return {
        "Won": odds - 1,
        "Half Won": (odds - 1) / 2,
        "Refunded": 0.0,
        "Half Lost": -0.5,
        "Lost": -1.0,
    }.get(settlement)


def extract(path: Path, system: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    canonical: dict[tuple[str, float, str], dict[str, Any]] = {}
    conflicts: set[tuple[str, float, str]] = set()
    diagnostics: collections.Counter[str] = collections.Counter()
    for row in source_rows(path, system):
        if excluded(row) or str(row.get("stage") or "") != "T-5":
            continue
        kickoff = parse_time(row.get("kickoff") or row.get("kickoff_hkt") or row.get("match_time"))
        observed = parse_time(
            row.get("predicted_at") or row.get("ts") or row.get("generated_at") or row.get("stage_at")
        )
        if kickoff is None:
            diagnostics["missing_kickoff"] += 1
            continue
        ids = {
            str(row.get(key))
            for key in ("hkjc_match_id", "match_id", "fixture_id", "titan_match_id", "id")
            if row.get(key) not in (None, "")
        }
        identity = sorted(ids)[0] if ids else f"{norm_name(row.get('home'))}:{norm_name(row.get('away'))}:{kickoff.isoformat()}"
        for grade in row.get("market_grades") or []:
            if (
                not isinstance(grade, dict)
                or excluded(grade)
                or str(grade.get("code") or "").upper() != "HIL"
                or grade.get("grade_status") != "GRADED"
            ):
                continue
            line = line_key(grade.get("line", grade.get("condition")))
            side = side_key(grade.get("side"))
            odds = number(grade.get("odds"))
            settlement = str(grade.get("settlement") or "")
            if line is None or side is None or odds is None or odds <= 1:
                diagnostics["invalid_grade_price"] += 1
                continue
            if return_from_settlement(settlement, odds) is None:
                diagnostics["invalid_settlement"] += 1
                continue
            key = (identity, line, side)
            candidate = {
                "system": system,
                "identity": identity,
                "ids": ids,
                "kickoff": kickoff,
                "observed": observed,
                "home": norm_name(row.get("home") or row.get("home_team")),
                "away": norm_name(row.get("away") or row.get("away_team")),
                "home_raw": row.get("home") or row.get("home_team"),
                "away_raw": row.get("away") or row.get("away_team"),
                "league": row.get("league") or row.get("competition") or "",
                "line": line,
                "side": side,
                "odds": odds,
                "settlement": settlement,
            }
            if key in canonical:
                old = canonical[key]
                signature = (old["odds"], old["settlement"])
                if signature != (odds, settlement):
                    if observed and (old["observed"] is None or observed > old["observed"]):
                        canonical[key] = candidate
                    else:
                        conflicts.add(key)
            else:
                canonical[key] = candidate
    output = [row for key, row in canonical.items() if key not in conflicts]
    diagnostics["conflicts_excluded"] = len(conflicts)
    return output, dict(diagnostics)


def resolve(foot: dict[str, Any], crowns: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    exact = [
        row for row in crowns
        if foot["ids"] & row["ids"]
        and row["line"] == foot["line"] and row["side"] == foot["side"]
    ]
    if len(exact) == 1:
        return exact[0], "id"
    named = [
        row for row in crowns
        if row["line"] == foot["line"] and row["side"] == foot["side"]
        and foot["home"] and foot["away"]
        and row["home"] == foot["home"] and row["away"] == foot["away"]
        and abs((row["kickoff"] - foot["kickoff"]).total_seconds()) <= 600
    ]
    if len(named) == 1:
        return named[0], "teams_time"
    return None, "ambiguous" if exact or named else "unmatched"


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - position) + ordered[hi] * (position - lo)


def summary(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    returns = [row["crown_return"] for row in rows]
    settlements = collections.Counter(row["settlement"] for row in rows)
    decided = sum(settlements[key] for key in ("Won", "Half Won", "Half Lost", "Lost"))
    hits = settlements["Won"] + settlements["Half Won"]
    boot = []
    if returns:
        rng = random.Random(int(hashlib.sha256(label.encode()).hexdigest()[:8], 16))
        for _ in range(10000):
            boot.append(sum(returns[rng.randrange(len(returns))] for __ in returns) / len(returns))
    return {
        "bets": len(rows),
        "decided": decided,
        "hit_rate_ex_push": hits / decided if decided else None,
        "crown_odds_mean": statistics.mean(row["crown_odds"] for row in rows) if rows else None,
        "footbreak_odds_mean": statistics.mean(row["footbreak_odds"] for row in rows) if rows else None,
        "mean_price_uplift": statistics.mean(row["price_uplift"] for row in rows) if rows else None,
        "crown_roi": statistics.mean(returns) if returns else None,
        "footbreak_roi_same_sample": (
            statistics.mean(row["footbreak_return"] for row in rows) if rows else None
        ),
        "crown_roi_bootstrap_95": [
            percentile(boot, 0.025),
            percentile(boot, 0.975),
        ],
        "unit_profit": sum(returns),
        "settlements": dict(settlements),
    }


def main() -> None:
    foot_rows, foot_diag = extract(FOOTBREAK_HISTORY, "footbreak")
    crown_rows, crown_diag = extract(CROWN_HISTORY, "crown")
    matched = []
    join_methods: collections.Counter[str] = collections.Counter()
    seen: set[tuple[str, float, str]] = set()
    for foot in foot_rows:
        crown, method = resolve(foot, crown_rows)
        join_methods[method] += 1
        if crown is None or crown["settlement"] != foot["settlement"]:
            continue
        unique = (foot["identity"], foot["line"], foot["side"])
        if unique in seen:
            continue
        seen.add(unique)
        uplift = crown["odds"] - foot["odds"]
        matched.append({
            "identity": foot["identity"],
            "kickoff": foot["kickoff"],
            "line": foot["line"],
            "side": foot["side"],
            "settlement": foot["settlement"],
            "footbreak_odds": foot["odds"],
            "crown_odds": crown["odds"],
            "price_uplift": uplift,
            "footbreak_return": return_from_settlement(foot["settlement"], foot["odds"]),
            "crown_return": return_from_settlement(foot["settlement"], crown["odds"]),
        })

    matched.sort(key=lambda row: (row["kickoff"], row["identity"], row["line"], row["side"]))
    cutoff_index = max(1, math.floor(len(matched) * 0.70))
    cutoff = matched[cutoff_index - 1]["kickoff"] if matched else None
    strategies = {}
    for minimum_uplift in (0.000001, 0.02, 0.05, 0.10):
        for minimum_crown_odds in (0.0, 1.70, 1.80):
            rows = [
                row for row in matched
                if row["price_uplift"] >= minimum_uplift
                and row["crown_odds"] >= minimum_crown_odds
            ]
            discovery = [row for row in rows if cutoff is not None and row["kickoff"] <= cutoff]
            holdout = [row for row in rows if cutoff is not None and row["kickoff"] > cutoff]
            name = f"uplift_gte_{minimum_uplift:.2f}_crown_odds_gte_{minimum_crown_odds:.2f}"
            strategies[name] = {
                "minimum_price_uplift": minimum_uplift,
                "minimum_crown_odds": minimum_crown_odds,
                "all": summary(rows, f"{name}:all"),
                "discovery": summary(discovery, f"{name}:discovery"),
                "holdout": summary(holdout, f"{name}:holdout"),
            }

    print(json.dumps({
        "definition": {
            "market": "T-5 HIL",
            "direction": "Footbreak V2 selected direction",
            "match": "same fixture, exact line and side; ID first, teams plus kickoff fallback",
            "execution": "Crown price only when Crown selected quote is higher",
            "important_limitation": (
                "Crown history stores its selected quote, not a guaranteed full two-sided board; "
                "therefore unmatched or opposite Crown selections cannot be priced here"
            ),
            "validation": "global chronological 70/30 split on matched rows",
        },
        "coverage": {
            "footbreak_t5_hil_rows": len(foot_rows),
            "crown_t5_hil_rows": len(crown_rows),
            "same_fixture_line_side_rows": len(matched),
            "join_methods": dict(join_methods),
            "footbreak_diagnostics": foot_diag,
            "crown_diagnostics": crown_diag,
            "cutoff_utc": cutoff.isoformat() if cutoff else None,
        },
        "strategies": strategies,
    }, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
