#!/usr/bin/env python3
"""Read-only scan for reverse indicators in Odds Radar OU three-stage data.

For every condition family we compute:
  * direct : bet the market low-odds side at T-5
  * reverse: bet the OTHER side at T-5

We surface conditions where the direct side is a clear negative expectation
and the reverse side has a genuine positive edge. Each fixture/provider only
contributes one strict main-line observation per side, so direct and reverse
statistics are always drawn from the same underlying set of fixtures.
"""

from __future__ import annotations

import collections
import json
import sqlite3
from typing import Any, Callable

import audit_radar_new_high_hit as base


DIRECT_HIT_CEILING = 0.40
REVERSE_ROI_FLOOR = 0.10
REVERSE_WILSON_LOWER_FLOOR = 0.50
REVERSE_HOLDOUT_ROI_FLOOR = 0.0
REVERSE_TRAIN_ROI_FLOOR = 0.0
MIN_ALL = 20
MIN_HOLDOUT = 6

EXISTING_REVERSE_KEYS = {
    ("pinnacle", "O→U→U", "short_010_020", "O"),
    ("hkjc", "O→O→O", "flat_or_wider", "U"),
    ("pinnacle", "U→U→U", "flat_or_wider", "O"),
}


def build_direct_view(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per fixture/provider, describing both sides on the strict main line."""

    by_pair: dict[tuple[str, str], dict[str, dict[str, Any]]] = collections.defaultdict(dict)
    for row in rows:
        by_pair[(row["fixture_id"], row["provider"])][row["bet_side"]] = row
    view: list[dict[str, Any]] = []
    for pair, sides in by_pair.items():
        if "O" not in sides or "U" not in sides:
            continue
        anchor = sides["O"] if sides["O"]["mode"] == "direct" else sides["U"]
        reverse = sides["U"] if anchor["bet_side"] == "O" else sides["O"]
        view.append(
            {
                "fixture_id": anchor["fixture_id"],
                "kickoff": anchor["kickoff"],
                "league": anchor["league"],
                "provider": anchor["provider"],
                "line": anchor["line"],
                "path": anchor["path"],
                "final_side": anchor["final_side"],
                "gap": anchor["gap"],
                "drift_bucket": anchor["drift_bucket"],
                "line_band": anchor["line_band"],
                "odds_band": anchor["odds_band"],
                "direct_bet_side": anchor["bet_side"],
                "direct_odds": anchor["t5_odds"],
                "direct_settlement": anchor["settlement"],
                "direct_unit_return": anchor["unit_return"],
                "reverse_bet_side": reverse["bet_side"],
                "reverse_odds": reverse["t5_odds"],
                "reverse_settlement": reverse["settlement"],
                "reverse_unit_return": reverse["unit_return"],
            }
        )
    return view


def side_metrics(rows: list[dict[str, Any]], side_key: str) -> dict[str, Any]:
    remapped = [
        {
            "settlement": row[f"{side_key}_settlement"],
            "unit_return": row[f"{side_key}_unit_return"],
            "t5_odds": row[f"{side_key}_odds"],
            "fixture_id": row["fixture_id"],
        }
        for row in rows
    ]
    return base.metrics(remapped)


def split_side(rows: list[dict[str, Any]], side_key: str) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (row["kickoff"], row["fixture_id"]))
    cut = max(1, int(len(ordered) * 0.70))
    if cut >= len(ordered):
        cut = len(ordered) - 1
    train, holdout = ordered[:cut], ordered[cut:]
    return {
        "all": side_metrics(ordered, side_key),
        "train": side_metrics(train, side_key),
        "holdout": side_metrics(holdout, side_key),
    }


def scan_reverse(view: list[dict[str, Any]]) -> list[dict[str, Any]]:
    families: list[tuple[str, Callable[[dict[str, Any]], tuple[Any, ...]]]] = [
        ("path_drift", lambda r: (r["provider"], r["path"], r["drift_bucket"])),
        (
            "path_drift_line",
            lambda r: (r["provider"], r["path"], r["drift_bucket"], r["line_band"]),
        ),
        (
            "path_drift_odds",
            lambda r: (r["provider"], r["path"], r["drift_bucket"], r["odds_band"]),
        ),
        (
            "path_line",
            lambda r: (r["provider"], r["path"], r["line_band"]),
        ),
        (
            "path_odds",
            lambda r: (r["provider"], r["path"], r["odds_band"]),
        ),
        (
            "league_path",
            lambda r: (r["provider"], r["league"], r["path"]),
        ),
    ]
    results: list[dict[str, Any]] = []
    for family, key_func in families:
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
        for row in view:
            grouped[key_func(row)].append(row)
        for key, group in grouped.items():
            if len(group) < 12:
                continue
            direct = split_side(group, "direct")
            reverse = split_side(group, "reverse")
            reverse_side_sample = collections.Counter(row["reverse_bet_side"] for row in group)
            reverse_side = reverse_side_sample.most_common(1)[0][0]
            duplicate_of_existing = tuple(list(key) + [reverse_side]) in EXISTING_REVERSE_KEYS
            direct_all, reverse_all = direct["all"], reverse["all"]
            reverse_train, reverse_hold = reverse["train"], reverse["holdout"]
            direct_hit = direct_all["hit_rate"] or 0.0
            reverse_roi = reverse_all["roi"] or -99.0
            reverse_hit_lower = (reverse_all["wilson_95"] or [None, None])[0] or -1.0
            passes = bool(
                direct_all["bets"] >= MIN_ALL
                and reverse_hold["bets"] >= MIN_HOLDOUT
                and direct_hit <= DIRECT_HIT_CEILING
                and reverse_roi >= REVERSE_ROI_FLOOR
                and reverse_hit_lower >= REVERSE_WILSON_LOWER_FLOOR
                and (reverse_train["roi"] or -99) > REVERSE_TRAIN_ROI_FLOOR
                and (reverse_hold["roi"] or -99) >= REVERSE_HOLDOUT_ROI_FLOOR
            )
            near_miss = bool(
                not passes
                and direct_all["bets"] >= MIN_ALL
                and direct_hit <= DIRECT_HIT_CEILING
                and reverse_roi >= 0.05
                and (reverse_train["roi"] or -99) > 0
            )
            results.append(
                {
                    "family": family,
                    "condition": list(key),
                    "reverse_bet_side": reverse_side,
                    "duplicate_of_existing_rule": duplicate_of_existing,
                    "direct": direct,
                    "reverse": reverse,
                    "screen_pass": passes,
                    "near_miss": near_miss,
                    "sample_warning": (
                        "exploratory_only"
                        if direct_all["bets"] < 50 or reverse_hold["bets"] < 15
                        else None
                    ),
                }
            )
    return sorted(
        results,
        key=lambda item: (
            not item["screen_pass"],
            not item["near_miss"],
            -((item["reverse"]["all"]["roi"] or -99)),
            (item["direct"]["all"]["hit_rate"] or 99),
            -item["direct"]["all"]["bets"],
        ),
    )


def main() -> None:
    db = sqlite3.connect(base.DB_URI, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    rows, dataset = base.load_rows(db)
    view = build_direct_view(rows)
    scanned = scan_reverse(view)
    passes = [item for item in scanned if item["screen_pass"]]
    near_miss = [item for item in scanned if item["near_miss"]]
    print(
        json.dumps(
            {
                "mode": "read_only",
                "method": {
                    "market": "OU",
                    "stages": list(base.STAGES),
                    "same_line": True,
                    "selected_price_each_stage": ">1.70",
                    "strict_one_line_per_fixture_provider": True,
                    "time_split": "first 70% train, last 30% holdout within each condition",
                    "screen": {
                        "direct_hit_ceiling": DIRECT_HIT_CEILING,
                        "reverse_roi_floor": REVERSE_ROI_FLOOR,
                        "reverse_wilson_lower_floor": REVERSE_WILSON_LOWER_FLOOR,
                        "reverse_train_roi_floor": REVERSE_TRAIN_ROI_FLOOR,
                        "reverse_holdout_roi_floor": REVERSE_HOLDOUT_ROI_FLOOR,
                        "min_all_bets": MIN_ALL,
                        "min_holdout_bets": MIN_HOLDOUT,
                    },
                    "warning": "Every discovered condition remains exploratory until prospectively validated.",
                },
                "dataset": dataset,
                "screen_pass_count": len(passes),
                "screen_passes": passes,
                "near_miss_count": len(near_miss),
                "near_miss": near_miss[:20],
                "top_exploratory": scanned[:30],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    db.rollback()
    db.close()


if __name__ == "__main__":
    main()
