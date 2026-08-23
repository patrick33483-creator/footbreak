#!/usr/bin/env python3
"""Read-only, provider-free T-30 to T-5 odds-path outcome audit.

The program opens ledger files only for reading and writes no files.  It does
not import Footbreak/Crown application modules, providers, notification code,
or betting logic.  Its stdout is a privacy-safe aggregate JSON report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

Z_95 = 1.959963984540054
SEED = 20260823
STAGES = ("T-30", "T-5")
BINARY_SETTLEMENTS = {"Won", "Lost", "Half Won", "Half Lost"}
# A deliberately finite, pre-declared set.  Each family has no more than three
# conceptual features; market_side is a single categorical feature.
FAMILIES = {
    "market_side_t30_odds_return": ("market_side", "t30_log_odds", "log_odds_return"),
    "market_side_return_line_move": ("market_side", "log_odds_return", "line_move"),
    "market_side_t30_odds_line_move": ("market_side", "t30_log_odds", "line_move"),
}


def num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def utc_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        result = float(value)
        if result > 10_000_000_000:
            result /= 1000
        return result if 946684800 <= result <= 4102444800 else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone(timedelta(hours=8)))
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.timestamp()


def wilson_lower(hits: int, decided: int) -> float | None:
    """Matches analysis.wilson_market_report's decided-row Wilson endpoint."""
    if decided <= 0 or hits < 0 or hits > decided:
        return None
    p = hits / decided
    z2 = Z_95 * Z_95
    return (p + z2 / (2 * decided) - Z_95 * math.sqrt(
        p * (1 - p) / decided + z2 / (4 * decided * decided)
    )) / (1 + z2 / decided)


def compact_line(value: Any) -> str | None:
    # Footbreak's persisted quarter lines may be "0.0/-0.5" while the
    # corresponding immutable stage stores -0.25.  This is canonical parsing,
    # not a settlement calculation.
    text = str(value or "").strip()
    if "/" in text:
        try:
            parts = [float(part.strip().lstrip("+")) for part in text.split("/") if part.strip()]
            if parts:
                value = sum(parts) / len(parts)
        except ValueError:
            pass
    number = num(value)
    if number is not None:
        return f"{number:.4f}".rstrip("0").rstrip(".")
    text = str(value or "").strip()
    return text or None


def stage_quotes(stage: dict[str, Any]) -> list[dict[str, Any]]:
    rows = stage.get("market_predictions") or stage.get("selected_markets") or []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def quote_fields(row: dict[str, Any]) -> tuple[str, str, str | None, float | None]:
    market = str(row.get("code") or row.get("market") or "").upper().strip()
    side = str(row.get("side") or row.get("selection") or "").upper().strip()
    line = compact_line(row.get("line", row.get("condition")))
    odds = num(row.get("odds", row.get("selected_odds")))
    if odds is None or odds <= 1.0:
        odds = None
    return market, side, line, odds


def grade_binary(stage: dict[str, Any], market: str, side: str, line: str | None) -> tuple[bool | None, str]:
    """Use only persisted formal grades: no re-settlement or inferred outcomes."""
    grades = stage.get("market_grades") or []
    if not isinstance(grades, list):
        return None, "missing_formal_grade"
    matches = []
    for grade in grades:
        if not isinstance(grade, dict) or grade.get("grade_status") != "GRADED":
            continue
        g_market, g_side, g_line, _ = quote_fields(grade)
        if g_market == market and g_side == side and (line is None or g_line is None or g_line == line):
            matches.append(grade)
    if not matches:
        return None, "missing_formal_grade"
    if len(matches) != 1:
        return None, "missing_or_ambiguous_formal_grade"
    grade = matches[0]
    settlement = str(grade.get("settlement") or "")
    hit = grade.get("hit")
    if settlement in {"Refunded", "Push", "Void", "VOIDED"}:
        return None, "push_or_void"
    # Existing formal Wilson policy makes the half results binary only when the
    # formal grade's hit is explicit.  Otherwise they are excluded, not guessed.
    if settlement not in BINARY_SETTLEMENTS or not isinstance(hit, bool):
        return None, "nonbinary_or_unrecognized_grade"
    return hit, "decided"

def formal_bets(payload: dict[str, Any]) -> dict[tuple[str, str, str, str | None], list[tuple[bool | None, str]]]:
    """Index only already-settled ledger bets; never recalculate a result."""
    indexed: dict[tuple[str, str, str, str | None], list[tuple[bool | None, str]]] = defaultdict(list)
    for bet in payload.get("bets") or []:
        if not isinstance(bet, dict):
            continue
        fixture = str(bet.get("fixture_id") or bet.get("match_id") or bet.get("id") or "")
        market = str(bet.get("code") or bet.get("market") or "").upper().strip()
        side = str(bet.get("side") or bet.get("selection") or "").upper().strip()
        line = compact_line(bet.get("line", bet.get("condition")))
        status, settlement = str(bet.get("status") or "").upper(), str(bet.get("result") or bet.get("settlement") or "")
        if not fixture or not market or not side or line is None:
            continue
        if status in {"VOIDED", "VOID", "CANCELLED", "CANCELED"}:
            indexed[(fixture, market, side, line)].append((None, "push_or_void"))
        elif status == "SETTLED":
            if settlement in {"Refunded", "Push", "Void", "VOIDED"}:
                indexed[(fixture, market, side, line)].append((None, "push_or_void"))
            elif settlement in BINARY_SETTLEMENTS:
                indexed[(fixture, market, side, line)].append((settlement in {"Won", "Half Won"}, "decided"))
    return indexed


def ledger_rows(payload: dict[str, Any], source: str) -> tuple[list[dict[str, Any]], Counter[str]]:
    diagnostics: Counter[str] = Counter()
    settled_bets = formal_bets(payload)
    watch = payload.get("watch") or {}
    watches = list(watch.items()) if isinstance(watch, dict) else [(str(i), row) for i, row in enumerate(watch) if isinstance(row, dict)]
    candidates: list[dict[str, Any]] = []
    for fallback_identity, fixture in watches:
        if not isinstance(fixture, dict):
            diagnostics["invalid_watch"] += 1; continue
        fixture_id = str(fixture.get("fixture_id") or fixture.get("match_id") or fixture.get("id") or fallback_identity or "")
        kickoff = utc_seconds(fixture.get("kickoff_utc") or fixture.get("kickoff_hkt") or fixture.get("kickoff"))
        stages = fixture.get("stages") or []
        if not fixture_id or kickoff is None or not isinstance(stages, list):
            diagnostics["missing_identity_or_kickoff_or_stages"] += 1; continue
        by_stage = {name: [] for name in STAGES}
        for stage in stages:
            if isinstance(stage, dict) and str(stage.get("stage") or "") in by_stage:
                by_stage[str(stage["stage"])].append(stage)
        if len(by_stage["T-30"]) != 1 or len(by_stage["T-5"]) != 1:
            diagnostics["missing_or_ambiguous_stage"] += 1; continue
        t30, t5 = by_stage["T-30"][0], by_stage["T-5"][0]
        t30_quotes: dict[tuple[str, str], list[tuple[str | None, float]]] = defaultdict(list)
        t5_quotes: dict[tuple[str, str], list[tuple[str | None, float]]] = defaultdict(list)
        for quote in stage_quotes(t30):
            market, side, line, odds = quote_fields(quote)
            if market and side and odds is not None: t30_quotes[(market, side)].append((line, odds))
        for quote in stage_quotes(t5):
            market, side, line, odds = quote_fields(quote)
            if market and side and odds is not None: t5_quotes[(market, side)].append((line, odds))
        for key in set(t30_quotes) | set(t5_quotes):
            a, b = t30_quotes.get(key, []), t5_quotes.get(key, [])
            if not a or not b:
                diagnostics["unpaired_t30_t5"] += 1; continue
            if len(a) != 1 or len(b) != 1:
                diagnostics["ambiguous_quote_identity"] += 1; continue
            (t30_line, t30_odds), (t5_line, t5_odds) = a[0], b[0]
            market, side = key
            outcome, reason = grade_binary(t5, market, side, t5_line)
            # Footbreak/Crown keep an independently settled simulation record
            # in bets.  It is an allowed fallback only where the stage has no
            # formal grade at all and the fixture/market/side/canonical line
            # match exactly; it never contacts a provider or recomputes a bet.
            if reason == "missing_formal_grade":
                recorded = settled_bets.get((fixture_id, market, side, t5_line), [])
                if len(recorded) == 1:
                    outcome, reason = recorded[0]
                elif len(recorded) > 1:
                    outcome, reason = None, "ambiguous_settled_bet"
            if outcome is None:
                diagnostics[reason] += 1; continue
            t30_line_num, t5_line_num = num(t30_line), num(t5_line)
            candidates.append({
                "fixture": fixture_id, "kickoff": kickoff, "market": market, "side": side,
                "t30_line": t30_line, "t5_line": t5_line, "t30_odds": t30_odds, "t5_odds": t5_odds,
                "outcome": outcome,
                "market_side": f"{market}:{side}",
                "t30_log_odds": math.log(t30_odds),
                "log_odds_return": math.log(t5_odds / t30_odds),
                "line_move": (t5_line_num - t30_line_num) if t5_line_num is not None and t30_line_num is not None else None,
                "line_move_available": t5_line_num is not None and t30_line_num is not None,
            })
    # The normalized unit has one row per fixture/market/side/line trajectory.
    by_identity: dict[tuple[str, str, str, str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        by_identity[(row["fixture"], row["market"], row["side"], row["t30_line"], row["t5_line"])].append(row)
    rows = []
    for items in by_identity.values():
        if len(items) == 1: rows.append(items[0])
        else: diagnostics["duplicate_normalized_identity"] += len(items)
    return sorted(rows, key=lambda r: (r["kickoff"], r["fixture"], r["market"], r["side"])), diagnostics


def chronological_split(rows: list[dict[str, Any]], fraction: float = .70) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cut only between equal-kickoff groups, preserving fixture membership."""
    if len(rows) < 2: return rows, []
    groups: list[list[dict[str, Any]]] = []
    for row in rows:
        if not groups or row["kickoff"] != groups[-1][0]["kickoff"]: groups.append([])
        groups[-1].append(row)
    if len(groups) < 2: return [], rows
    target = len(rows) * fraction
    cumulative, choices = 0, []
    for i, group in enumerate(groups[:-1], 1):
        cumulative += len(group); choices.append((abs(cumulative - target), i))
    cut = min(choices)[1]
    train = [row for group in groups[:cut] for row in group]
    test = [row for group in groups[cut:] for row in group]
    if {r["fixture"] for r in train} & {r["fixture"] for r in test}:
        raise AssertionError("fixture leakage in chronological split")
    return train, test


def vectorizer_fit(rows: list[dict[str, Any]], features: tuple[str, ...]) -> dict[str, Any]:
    categories = {feature: sorted({str(row[feature]) for row in rows}) for feature in features if feature == "market_side"}
    numeric = [feature for feature in features if feature != "market_side"]
    raw = []
    for row in rows:
        vector = []
        for feature in features:
            if feature == "market_side": vector.extend(1.0 if str(row[feature]) == cat else 0.0 for cat in categories[feature])
            else: vector.append(float(row[feature]))
        raw.append(vector)
    dims = len(raw[0]) if raw else 0
    means = [sum(v[j] for v in raw) / len(raw) for j in range(dims)] if raw else []
    scales = [math.sqrt(sum((v[j] - means[j]) ** 2 for v in raw) / len(raw)) or 1.0 for j in range(dims)] if raw else []
    return {"features": list(features), "categories": categories, "means": means, "scales": scales, "numeric": numeric}


def vectorizer_apply(rows: list[dict[str, Any]], spec: dict[str, Any]) -> list[list[float]]:
    output = []
    for row in rows:
        raw = []
        for feature in spec["features"]:
            if feature == "market_side": raw.extend(1.0 if str(row[feature]) == cat else 0.0 for cat in spec["categories"][feature])
            else: raw.append(float(row[feature]))
        output.append([(x - spec["means"][j]) / spec["scales"][j] for j, x in enumerate(raw)])
    return output


def dist2(a: list[float], b: list[float]) -> float: return sum((x - y) ** 2 for x, y in zip(a, b))

def kmeans(data: list[list[float]], k: int, seed: int) -> tuple[list[int], list[list[float]]]:
    rng = random.Random(seed)
    centers = [list(data[rng.randrange(len(data))])]
    while len(centers) < k:
        weights = [min(dist2(x, c) for c in centers) for x in data]
        total = sum(weights)
        if total <= 0: centers.append(list(data[len(centers) % len(data)])); continue
        draw, running = rng.random() * total, 0.0
        for row, weight in zip(data, weights):
            running += weight
            if running >= draw: centers.append(list(row)); break
    labels = [-1] * len(data)
    for _ in range(100):
        next_labels = [min(range(k), key=lambda c: (dist2(row, centers[c]), c)) for row in data]
        if next_labels == labels: break
        labels = next_labels
        for c in range(k):
            members = [row for row, label in zip(data, labels) if label == c]
            if members: centers[c] = [sum(row[j] for row in members) / len(members) for j in range(len(data[0]))]
    return labels, centers


def silhouette(data: list[list[float]], labels: list[int]) -> float:
    if len(set(labels)) < 2: return -1.0
    values = []
    for i, row in enumerate(data):
        same = [math.sqrt(dist2(row, other)) for j, other in enumerate(data) if labels[j] == labels[i] and j != i]
        a = sum(same) / len(same) if same else 0.0
        b = min(sum(math.sqrt(dist2(row, other)) for j, other in enumerate(data) if labels[j] == c) / sum(label == c for label in labels) for c in set(labels) if c != labels[i])
        values.append((b-a)/max(a,b) if max(a,b) else 0.0)
    return sum(values)/len(values)


def test_labels(data: list[list[float]], centers: list[list[float]]) -> list[int]:
    return [min(range(len(centers)), key=lambda c: (dist2(row, centers[c]), c)) for row in data]

def chi_square(groups: dict[int, list[bool]]) -> float:
    total = sum(len(v) for v in groups.values()); wins = sum(sum(v) for v in groups.values())
    if total == 0 or wins in {0, total}: return 0.0
    statistic = 0.0
    for values in groups.values():
        for observed, expected in ((sum(values), len(values)*wins/total), (len(values)-sum(values), len(values)*(total-wins)/total)):
            if expected: statistic += (observed-expected)**2/expected
    return statistic

def max_rate_spread(groups: dict[int, list[bool]]) -> float:
    rates = [sum(values)/len(values) for values in groups.values() if values]
    return max(rates)-min(rates) if rates else 0.0

def fixture_aware_permutation(rows: list[dict[str, Any]], labels: list[int], permutations: int = 2000) -> float | None:
    by_fixture: dict[str, list[tuple[int, bool]]] = defaultdict(list)
    for row, label in zip(rows, labels): by_fixture[row["fixture"]].append((label, bool(row["outcome"])))
    if len(by_fixture) < 2: return None
    vectors = [values for _, values in sorted(by_fixture.items())]
    observed_groups: dict[int, list[bool]] = defaultdict(list)
    for values in vectors:
        for label, outcome in values: observed_groups[label].append(outcome)
    observed = max_rate_spread(observed_groups)
    rng, ge = random.Random(SEED), 0
    for _ in range(permutations):
        shuffled = list(vectors); rng.shuffle(shuffled)
        simulated: dict[int, list[bool]] = defaultdict(list)
        for labels_and_outcomes, donor in zip(vectors, shuffled):
            # Outcome vectors preserve within-fixture correlation; unequal vector
            # shapes are skipped rather than forcing a misleading permutation.
            if len(labels_and_outcomes) != len(donor): continue
            for (label, _), (_, outcome) in zip(labels_and_outcomes, donor): simulated[label].append(outcome)
        if max_rate_spread(simulated) >= observed - 1e-12: ge += 1
    return (ge + 1) / (permutations + 1)

def holm(values: dict[str, float | None]) -> dict[str, float | None]:
    valid = sorted((p, name) for name, p in values.items() if p is not None)
    output = {name: None for name in values}
    previous, count = 0.0, len(valid)
    for rank, (p, name) in enumerate(valid):
        adjusted = max(previous, min(1.0, (count-rank)*p)); output[name] = adjusted; previous = adjusted
    return output

def analyze_source(payload: dict[str, Any], source: str) -> dict[str, Any]:
    rows, diagnostics = ledger_rows(payload, source)
    train, test = chronological_split(rows)
    result: dict[str, Any] = {"source": source, "normalized_decided_rows": len(rows), "diagnostics": dict(sorted(diagnostics.items())), "split": {"method": "chronological_kickoff_group_70_30", "train_rows": len(train), "test_rows": len(test), "train_fixtures": len({r['fixture'] for r in train}), "test_fixtures": len({r['fixture'] for r in test}), "fixture_overlap": 0}, "families": {}, "leaderboard": []}
    p_values: dict[str, float | None] = {}
    held = []
    for family, features in FAMILIES.items():
        entry: dict[str, Any] = {"features": list(features), "fit": "train_only", "status": "insufficient_train_or_test"}
        # A line-movement family never turns an unparseable line into a flat
        # move.  It simply uses the eligible numeric-line subset.
        train_family = [row for row in train if row["line_move_available"]] if "line_move" in features else train
        test_family = [row for row in test if row["line_move_available"]] if "line_move" in features else test
        if len(train_family) < 4 or len(test_family) < 1: result["families"][family] = entry; continue
        spec = vectorizer_fit(train_family, features); x_train, x_test = vectorizer_apply(train_family, spec), vectorizer_apply(test_family, spec)
        alternatives = []
        for k in range(2, min(6, len(train_family)-1)+1):
            labels, centers = kmeans(x_train, k, SEED + k)
            alternatives.append((silhouette(x_train, labels), k, labels, centers))
        if not alternatives: result["families"][family] = entry; continue
        score, k, _, centers = max(alternatives, key=lambda item: (item[0], -item[1]))
        labels = test_labels(x_test, centers)
        groups: dict[int, list[bool]] = defaultdict(list)
        for row, label in zip(test_family, labels): groups[label].append(bool(row["outcome"]))
        p = fixture_aware_permutation(test_family, labels)
        p_values[family] = p
        clusters = []
        for label in range(k):
            outcomes = groups.get(label, []); wins, n = sum(outcomes), len(outcomes)
            clusters.append({"cluster": label, "test_decided": n, "test_hits": wins, "test_rate": round(wins/n, 6) if n else None, "test_wilson_95_lower": round(wilson_lower(wins, n), 6) if n else None})
            train_outcomes = [bool(row["outcome"]) for row, lab in zip(train_family, test_labels(x_train, centers)) if lab == label]
            if n >= 20: held.append({"family": family, "cluster": label, "train_decided": len(train_outcomes), "train_hits": sum(train_outcomes), "train_rate": sum(train_outcomes)/len(train_outcomes) if train_outcomes else None, "train_wilson_95_lower": wilson_lower(sum(train_outcomes), len(train_outcomes)), "test_decided": n, "test_hits": wins, "test_rate": wins/n, "test_wilson_95_lower": wilson_lower(wins,n), "odds_path_definition": "same fixture-market-side; T-30 and T-5 numeric decimal selected odds; line trajectory retained"})
        entry.update({"status": "evaluated", "k": k, "train_silhouette": round(score, 6), "scaler_train_only": {"means": [round(x,6) for x in spec['means']], "scales": [round(x,6) for x in spec['scales']]}, "centroids_train_scaled": [[round(x,6) for x in center] for center in centers], "test_chi_square": round(chi_square(groups),6), "test_max_rate_spread": round(max_rate_spread(groups),6), "test_fixture_aware_permutation_p": p, "clusters": clusters})
        result["families"][family] = entry
    adjusted = holm(p_values)
    for family, adjusted_p in adjusted.items():
        result["families"][family]["test_holm_adjusted_p"] = adjusted_p
    # Discovery is restricted to train sample: candidates require >=30 train rows.
    allowed = []
    for item in held:
        if item["train_decided"] >= 30:
            item["test_family_p"] = p_values.get(item["family"]); item["test_holm_adjusted_p"] = adjusted.get(item["family"]); allowed.append(item)
    result["leaderboard"] = sorted(allowed, key=lambda x: (x["test_wilson_95_lower"] if x["test_wilson_95_lower"] is not None else -1, x["test_decided"]), reverse=True)[:3]
    result["thresholds"] = {"min_train_decided": 30, "min_test_decided": 20, "note": "If no row qualifies, the leaderboard is intentionally empty rather than lowering thresholds silently."}
    return result

def load(path: str) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crown-ledger", default="/var/lib/footbreak/crown/ledger.json")
    parser.add_argument("--footbreak-ledger", default="/opt/footbreak/system/sim_ledger.json")
    args = parser.parse_args(argv)
    reports = []
    for source, path in (("crown", args.crown_ledger), ("footbreak", args.footbreak_ledger)):
        try: reports.append(analyze_source(load(path), source))
        except (OSError, json.JSONDecodeError) as exc: reports.append({"source": source, "status": "unavailable", "error": type(exc).__name__})
    print(json.dumps({"schema_version": "audit-odds-path-outcome-v1", "audit_only": True, "provider_free": True, "paths": {"crown": args.crown_ledger, "footbreak": args.footbreak_ledger}, "analysis": {"split": "chronological kickoff group; no fixture can cross split", "unit": "fixture+market+side+T30_line_to_T5_line", "outcome": "persisted formal GRADED market_grades only; Wilson uses explicit boolean hit; Refunded/Push/Void excluded", "dependency_limit": "multiple market rows from one fixture may remain correlated; family p-values use fixture-vector permutation and no combined Crown/Footbreak inference is made", "leaderboard_warning": "Top test Wilson-lower rows are an OOS leaderboard, not final evidence; require a later forward validation."}, "sources": reports}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0
if __name__ == "__main__": raise SystemExit(main())
