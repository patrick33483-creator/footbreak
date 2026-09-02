#!/usr/bin/env python3
"""Read-only wide reverse-signal scan for Odds Radar OU three-stage data.

Extends `audit_radar_reverse_signals` with broader bucketing families that
aggregate across providers or across path detail to surface candidates with
whole-sample sizes of at least 50. Same strict main-line data set.
"""

from __future__ import annotations

import collections
import json
import sqlite3
from typing import Any, Callable

import audit_radar_new_high_hit as base
import audit_radar_reverse_signals as fine


MIN_ALL = 50
MIN_HOLDOUT = 15
DIRECT_HIT_CEILING = 0.42
REVERSE_ROI_FLOOR = 0.05
REVERSE_WILSON_LOWER_FLOOR = 0.50
REVERSE_TRAIN_ROI_FLOOR = 0.0
REVERSE_HOLDOUT_ROI_FLOOR = 0.0


def path_family(path: str) -> str:
    letters = path.split("→")
    if len(letters) != 3:
        return path
    if letters[0] == letters[1] == letters[2]:
        return f"{letters[0]}-mono"
    if letters[1] == letters[2]:
        return f"{letters[0]}-then-{letters[1]}"
    if letters[0] == letters[1]:
        return f"{letters[0]}-hold-{letters[2]}"
    return f"{letters[0]}-{letters[1]}-{letters[2]}"


def enrich(view: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in view:
        gap = row["gap"]
        drift_direction = "short" if gap > 0 else "flat_or_wider"
        agg_line = "low" if row["line"] <= 2.5 else "mid" if row["line"] <= 2.75 else "high"
        rows.append({**row, "drift_direction": drift_direction, "line_group": agg_line, "path_family": path_family(row["path"])})
    return rows


def scan_wide(view: list[dict[str, Any]]) -> list[dict[str, Any]]:
    families: list[tuple[str, Callable[[dict[str, Any]], tuple[Any, ...]]]] = [
        ("provider_only", lambda r: (r["provider"],)),
        ("provider_direction", lambda r: (r["provider"], r["drift_direction"])),
        ("provider_line_group", lambda r: (r["provider"], r["line_group"])),
        ("provider_odds_band", lambda r: (r["provider"], r["odds_band"])),
        ("path_only", lambda r: (r["path"],)),
        ("path_provider_only", lambda r: (r["provider"], r["path"])),
        ("path_family_provider", lambda r: (r["provider"], r["path_family"])),
        ("provider_path_direction", lambda r: (r["provider"], r["path"], r["drift_direction"])),
        ("provider_path_line_group", lambda r: (r["provider"], r["path"], r["line_group"])),
        ("provider_path_odds", lambda r: (r["provider"], r["path"], r["odds_band"])),
        ("provider_drift_bucket", lambda r: (r["provider"], r["drift_bucket"])),
        ("provider_line_band", lambda r: (r["provider"], r["line_band"])),
        ("path_family_only", lambda r: (r["path_family"],)),
        ("both_provider_path_family", lambda r: (r["path_family"], r["drift_direction"])),
    ]
    results: list[dict[str, Any]] = []
    for family, key_func in families:
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
        for row in view:
            grouped[key_func(row)].append(row)
        for key, group in grouped.items():
            if len(group) < MIN_ALL:
                continue
            direct = fine.split_side(group, "direct")
            reverse = fine.split_side(group, "reverse")
            direct_all, reverse_all = direct["all"], reverse["all"]
            reverse_train, reverse_hold = reverse["train"], reverse["holdout"]
            reverse_side_sample = collections.Counter(row["reverse_bet_side"] for row in group)
            most_common_reverse = reverse_side_sample.most_common(1)[0]
            reverse_side, reverse_side_share = most_common_reverse[0], most_common_reverse[1] / len(group)
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
            results.append(
                {
                    "family": family,
                    "condition": list(key),
                    "reverse_bet_side": reverse_side,
                    "reverse_side_share": round(reverse_side_share, 4),
                    "direct": direct,
                    "reverse": reverse,
                    "screen_pass": passes,
                }
            )
    return sorted(
        results,
        key=lambda item: (
            not item["screen_pass"],
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
    view = enrich(fine.build_direct_view(rows))
    scanned = scan_wide(view)
    passes = [item for item in scanned if item["screen_pass"]]
    print(
        json.dumps(
            {
                "mode": "read_only",
                "method": {
                    "market": "OU",
                    "min_all_bets": MIN_ALL,
                    "min_holdout_bets": MIN_HOLDOUT,
                    "direct_hit_ceiling": DIRECT_HIT_CEILING,
                    "reverse_roi_floor": REVERSE_ROI_FLOOR,
                    "reverse_wilson_lower_floor": REVERSE_WILSON_LOWER_FLOOR,
                    "reverse_train_roi_floor": REVERSE_TRAIN_ROI_FLOOR,
                    "reverse_holdout_roi_floor": REVERSE_HOLDOUT_ROI_FLOOR,
                    "families_scanned": [
                        "provider_only", "provider_direction", "provider_line_group",
                        "provider_odds_band", "path_only", "path_provider_only",
                        "path_family_provider", "provider_path_direction",
                        "provider_path_line_group", "provider_path_odds",
                        "provider_drift_bucket", "provider_line_band",
                        "path_family_only", "both_provider_path_family",
                    ],
                    "warning": "Any candidate remains exploratory until prospectively validated.",
                },
                "dataset": dataset,
                "screen_pass_count": len(passes),
                "screen_passes": passes,
                "top_exploratory": scanned[:40],
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
