#!/usr/bin/env python3
"""Audit Crown fixtures with predictions but no verified result.

Read-only. Reads a Crown prediction_history.json (path via --input),
finds every canonical pre-kickoff fixture-market row that lacks a
usable result, and writes an audit report and CSV to --report/--csv.

Usage:
    python3 crown_pending_results_audit.py \
        --input /var/lib/footbreak/crown/prediction_history.json \
        --report /tmp/crown-pending-results.json \
        --csv /tmp/crown-pending-results.csv

The script never mutates the input file and never writes anywhere
outside the paths given on the command line.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

STAGE_ORDER = {"OPEN": 0, "T-30": 1, "T-5": 2, "OPENING": 0}
MARKETS = {"HDC", "HIL"}
HKT_OFFSET_HOURS = 8


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_time(value):
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hkt_iso(dt):
    if dt is None:
        return None
    offset = timezone.utc.utcoffset(dt)  # noqa: unused
    return dt.astimezone(tz=timezone(_hkt_delta())).strftime("%Y-%m-%d %H:%M")


def _hkt_delta():
    from datetime import timedelta
    return timedelta(hours=HKT_OFFSET_HOURS)


def has_usable_result(row):
    """Match V3.1 script semantics: row.result_status in verified set AND
    row.result_detail has usable home/away scores."""
    if row.get("result_status") not in {"已核對", "已核實"}:
        return False
    detail = row.get("result_detail")
    if isinstance(detail, dict):
        home = detail.get("home_score")
        away = detail.get("away_score")
        try:
            if home is not None and away is not None:
                float(home); float(away)
                return True
        except (TypeError, ValueError):
            pass
    score = str(row.get("score") or "")
    if score:
        pieces = score.replace("：", "-").replace(":", "-").split("-")
        if len(pieces) == 2:
            try:
                float(pieces[0]); float(pieces[1])
                return True
            except (TypeError, ValueError):
                pass
    return False


def canonical_market_rows(rows):
    """Return newest pre-kickoff row per (fixture, market, stage)."""
    candidates = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            continue
        fixture = str(row.get("match_id") or row.get("history_key") or "").strip()
        stage = str(row.get("stage") or "").strip().upper()
        stage = "OPEN" if stage == "OPENING" else stage
        if not fixture or stage not in STAGE_ORDER:
            continue
        kickoff = parse_time(row.get("kickoff") or row.get("kickoff_hkt"))
        predicted_at = parse_time(row.get("predicted_at") or row.get("ts"))
        if kickoff is None or predicted_at is None:
            continue
        if predicted_at >= kickoff:
            continue  # not pre-kickoff
        row_settled = has_usable_result(row)
        predictions = row.get("market_predictions") or []
        seen = set()
        for item in predictions:
            if not isinstance(item, dict):
                continue
            market = str(item.get("code") or "").upper()
            if market not in MARKETS:
                continue
            sig = (market, item.get("side"), item.get("line") or item.get("condition"), item.get("odds"))
            if sig in seen:
                continue
            seen.add(sig)
            candidates[(fixture, market, stage)].append({
                "row": row,
                "item": item,
                "kickoff": kickoff,
                "predicted_at": predicted_at,
                "row_settled": row_settled,
            })
    newest = {}
    for key, values in candidates.items():
        best = max(values, key=lambda v: v["predicted_at"])
        newest[key] = best
    return newest


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--now", default=None, help="ISO override for testing")
    args = parser.parse_args(argv)

    src = Path(args.input)
    sha_before = sha256_file(src)
    with src.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    rows = payload.get("rows") or payload.get("entries") or payload.get("history") or []
    if not isinstance(rows, list):
        # some historic dumps use a dict of lists per stage
        collected = []
        for value in rows.values() if isinstance(rows, dict) else []:
            if isinstance(value, list):
                collected.extend(value)
        rows = collected

    canonical = canonical_market_rows(rows)

    now = parse_time(args.now) or datetime.now(timezone.utc)

    fixtures = defaultdict(lambda: {"markets": {}, "kickoff": None, "home": None, "away": None, "league": None})
    settled_fixtures = set()
    pending_records = []

    fixture_settled = defaultdict(bool)
    for (fixture, market, stage), entry in canonical.items():
        # a fixture is "settled" if any of its snapshots has verified result
        if entry["row_settled"]:
            fixture_settled[fixture] = True

    for (fixture, market, stage), entry in canonical.items():
        row = entry["row"]
        item = entry["item"]
        kickoff = entry["kickoff"]
        settled = fixture_settled[fixture]

        info = fixtures[fixture]
        info["kickoff"] = kickoff if info["kickoff"] is None or kickoff < info["kickoff"] else info["kickoff"]
        info["home"] = info["home"] or row.get("home") or row.get("home_team") or (row.get("teams") or {}).get("home")
        info["away"] = info["away"] or row.get("away") or row.get("away_team") or (row.get("teams") or {}).get("away")
        info["league"] = info["league"] or row.get("league") or row.get("league_name") or row.get("competition")

        info["markets"].setdefault(market, {"stages": {}, "any_settled": False})
        info["markets"][market]["stages"][stage] = {
            "predicted_at": entry["predicted_at"],
            "line": item.get("line") or item.get("condition"),
            "side": item.get("side"),
            "odds": item.get("odds"),
            "settled": settled,
        }
        if settled:
            info["markets"][market]["any_settled"] = True
            settled_fixtures.add(fixture)

    for fixture, info in fixtures.items():
        if not info["markets"]:
            continue
        # a fixture is "pending" if at least one predicted market is unsettled
        # AND kickoff has already passed by >= 6 hours (game should be done)
        kickoff = info["kickoff"]
        if kickoff is None:
            continue
        hours_since_kickoff = (now - kickoff).total_seconds() / 3600.0
        if hours_since_kickoff < 3.0:
            continue  # still in play or too early
        # fixture-level: 淨計 fixture 冇任何 verified result
        if fixture in settled_fixtures:
            continue
        unsettled_markets = sorted(info["markets"].keys())
        if not unsettled_markets:
            continue
        pending_records.append({
            "fixture": fixture,
            "kickoff_utc": kickoff.isoformat(),
            "kickoff_hkt": hkt_iso(kickoff),
            "hours_since_kickoff": round(hours_since_kickoff, 2),
            "home": info["home"],
            "away": info["away"],
            "league": info["league"],
            "unsettled_markets": sorted(unsettled_markets),
            "stages_missing": {
                m: sorted(
                    stage for stage, s in info["markets"][m]["stages"].items()
                    if not s["settled"]
                )
                for m in unsettled_markets
            },
            "hdc_line": (info["markets"].get("HDC", {}).get("stages", {}).get("T-5") or {}).get("line"),
            "hil_line": (info["markets"].get("HIL", {}).get("stages", {}).get("T-5") or {}).get("line"),
        })

    pending_records.sort(key=lambda r: (-r["hours_since_kickoff"], r["kickoff_utc"]))

    league_breakdown = defaultdict(lambda: {"count": 0, "hours_median": [], "markets": defaultdict(int)})
    age_buckets = {"3-24h": 0, "1-3d": 0, "3-7d": 0, "1-2w": 0, "gt_2w": 0}

    for rec in pending_records:
        league = rec["league"] or "(unknown)"
        league_breakdown[league]["count"] += 1
        league_breakdown[league]["hours_median"].append(rec["hours_since_kickoff"])
        for m in rec["unsettled_markets"]:
            league_breakdown[league]["markets"][m] += 1
        hours = rec["hours_since_kickoff"]
        if hours < 24:
            age_buckets["3-24h"] += 1
        elif hours < 24 * 3:
            age_buckets["1-3d"] += 1
        elif hours < 24 * 7:
            age_buckets["3-7d"] += 1
        elif hours < 24 * 14:
            age_buckets["1-2w"] += 1
        else:
            age_buckets["gt_2w"] += 1

    for league, data in league_breakdown.items():
        hs = sorted(data["hours_median"])
        data["hours_median"] = round(hs[len(hs) // 2], 2) if hs else None
        data["markets"] = dict(data["markets"])

    sha_after = sha256_file(src)

    report = {
        "generated_at_utc": now.isoformat(),
        "input": {
            "path": str(src),
            "sha256_before": sha_before,
            "sha256_after": sha_after,
            "read_only": sha_before == sha_after,
            "bytes": src.stat().st_size,
        },
        "summary": {
            "canonical_fixture_market_stage_rows": len(canonical),
            "total_fixtures_with_predictions": len(fixtures),
            "fixtures_with_at_least_one_settled_market": len(settled_fixtures),
            "pending_fixtures_reported": len(pending_records),
            "age_buckets": age_buckets,
        },
        "leagues": dict(sorted(league_breakdown.items(), key=lambda kv: -kv[1]["count"])),
        "pending_fixtures": pending_records,
    }

    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with Path(args.csv).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "kickoff_hkt", "hours_since_kickoff", "league", "home", "away",
            "unsettled_markets", "stages_missing_hdc", "stages_missing_hil",
            "hdc_line", "hil_line", "fixture",
        ])
        for rec in pending_records:
            writer.writerow([
                rec["kickoff_hkt"] or "",
                rec["hours_since_kickoff"],
                rec["league"] or "",
                rec["home"] or "",
                rec["away"] or "",
                ",".join(rec["unsettled_markets"]),
                ",".join(rec["stages_missing"].get("HDC", [])),
                ",".join(rec["stages_missing"].get("HIL", [])),
                rec["hdc_line"] or "",
                rec["hil_line"] or "",
                rec["fixture"],
            ])

    print(json.dumps({
        "pending_fixtures_reported": len(pending_records),
        "age_buckets": age_buckets,
        "input_hash_unchanged": sha_before == sha_after,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
