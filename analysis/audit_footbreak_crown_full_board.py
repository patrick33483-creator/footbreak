#!/usr/bin/env python3
"""Price every eligible Footbreak T-5 HIL direction from persisted Crown full boards."""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import math
import random
import statistics
from pathlib import Path
from typing import Any

from analysis.audit_footbreak_direction_crown_price import (
    FOOTBREAK_HISTORY,
    CROWN_HISTORY,
    extract,
    line_key,
    number,
    parse_time,
    return_from_settlement,
)
from analysis.odds_recovery import (
    PrivateResponseCache,
    ProviderFetcher,
    canonical_line,
    titan_candidate,
)
from crown.matching import Event, canonical_league_key, canonical_team_key, match_event


CROWN_STATE = Path("/var/lib/footbreak/crown")
MIN_BETS = 10


def ids_of(row: dict[str, Any]) -> set[str]:
    return {
        str(row.get(key)).strip()
        for key in ("hkjc_match_id", "match_id", "fixture_id", "titan_match_id", "id")
        if row.get(key) not in (None, "")
    }


def load_crown_meta() -> dict[str, dict[str, Any]]:
    payload = json.loads(CROWN_HISTORY.read_text(encoding="utf-8"))
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        titan = str(row.get("titan_match_id") or row.get("match_id") or "").strip()
        if titan:
            grouped[titan].append(row)
    output = {}
    for titan, rows in grouped.items():
        preferred = next((row for row in rows if row.get("stage") == "T-5"), rows[-1])
        output[titan] = {
            "ids": set().union(*(ids_of(row) for row in rows)),
            "home": preferred.get("home") or "",
            "away": preferred.get("away") or "",
            "league": preferred.get("league") or "",
            "kickoff": parse_time(preferred.get("kickoff") or preferred.get("kickoff_hkt")),
        }
    return output


def full_boards(meta: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], collections.Counter[str]]:
    diagnostics: collections.Counter[str] = collections.Counter()
    canonical: dict[str, dict[str, Any]] = {}
    for path in CROWN_STATE.rglob("*__T-5.json"):
        diagnostics["snapshot_files"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            diagnostics["unreadable_snapshot"] += 1
            continue
        if not isinstance(data, dict) or str(data.get("stage") or "") != "T-5":
            diagnostics["not_t5"] += 1
            continue
        fixture = str(data.get("match_id") or "").strip()
        kickoff = parse_time(data.get("kickoff_hkt") or data.get("kickoff"))
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        quotes = []
        for quote in ((payload.get("book_odds") or {}).get("crown") or []):
            if not isinstance(quote, dict) or str(quote.get("market") or "").upper() != "HIL":
                continue
            side = {"H": "O", "O": "O", "L": "U", "U": "U"}.get(
                str(quote.get("selection") or quote.get("side") or "").upper()
            )
            line = line_key(quote.get("line"))
            odds = number(quote.get("odds"))
            observed = parse_time(quote.get("source_at") or data.get("updated_at") or data.get("created_at"))
            if side and line is not None and odds is not None and odds > 1:
                quotes.append({"side": side, "line": line, "odds": odds, "observed": observed})
        if not fixture or kickoff is None or not quotes:
            diagnostics["missing_identity_or_hil"] += 1
            continue
        by_line: dict[float, dict[str, dict[str, Any]]] = collections.defaultdict(dict)
        for quote in quotes:
            old = by_line[quote["line"]].get(quote["side"])
            if old is None or (quote["observed"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) > (
                old["observed"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
            ):
                by_line[quote["line"]][quote["side"]] = quote
        complete = {
            line: sides for line, sides in by_line.items()
            if set(sides) >= {"O", "U"}
        }
        if not complete:
            diagnostics["no_complete_hil_line"] += 1
            continue
        info = meta.get(fixture) or {}
        candidate = {
            "fixture": fixture,
            "ids": set(info.get("ids") or {fixture}) | {fixture},
            "home": info.get("home") or payload.get("home") or "",
            "away": info.get("away") or payload.get("away") or "",
            "league": info.get("league") or payload.get("league") or "",
            "kickoff": kickoff,
            "lines": complete,
            "updated": parse_time(data.get("updated_at") or data.get("created_at")),
        }
        old = canonical.get(fixture)
        if old is None or (candidate["updated"] or kickoff) > (old["updated"] or old["kickoff"]):
            canonical[fixture] = candidate
    diagnostics["fixtures_with_complete_hil_board"] = len(canonical)
    diagnostics["complete_hil_lines"] = sum(len(row["lines"]) for row in canonical.values())
    return list(canonical.values()), diagnostics


def resolve_board(foot: dict[str, Any], boards: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    exact = [row for row in boards if foot["ids"] & row["ids"]]
    exact = [row for row in exact if foot["line"] in row["lines"]]
    if len(exact) == 1:
        return exact[0], "verified_id"
    if len(exact) > 1:
        return None, "ambiguous_id"
    target = Event(
        id=foot["identity"],
        league=str(foot.get("league") or ""),
        home=str(foot.get("home_raw") or ""),
        away=str(foot.get("away_raw") or ""),
        kickoff=foot["kickoff"],
    )
    candidates = [
        Event(
            id=row["fixture"],
            league=str(row.get("league") or ""),
            home=str(row.get("home") or ""),
            away=str(row.get("away") or ""),
            kickoff=row["kickoff"],
            extra=row,
        )
        for row in boards if foot["line"] in row["lines"]
    ]
    found = match_event(
        target,
        candidates,
        team_key=canonical_team_key,
        league_key=canonical_league_key,
        allow_reversed=True,
        require_qualifiers=True,
    )
    if found.event is None:
        return None, found.reason or "unmatched"
    return found.event.extra, "strict_teams_kickoff"


def resolve_meta(foot: dict[str, Any], meta: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    rows = [{"fixture": fixture, **value} for fixture, value in meta.items() if value.get("kickoff")]
    exact = [row for row in rows if foot["ids"] & row["ids"]]
    if len(exact) == 1:
        return exact[0], "verified_id"
    if len(exact) > 1:
        return None, "ambiguous_id"
    target = Event(
        id=foot["identity"],
        league=str(foot.get("league") or ""),
        home=str(foot.get("home_raw") or ""),
        away=str(foot.get("away_raw") or ""),
        kickoff=foot["kickoff"],
    )
    candidates = [
        Event(
            id=row["fixture"], league=str(row.get("league") or ""),
            home=str(row.get("home") or ""), away=str(row.get("away") or ""),
            kickoff=row["kickoff"], extra=row,
        )
        for row in rows
    ]
    found = match_event(
        target, candidates, team_key=canonical_team_key, league_key=canonical_league_key,
        allow_reversed=True, require_qualifiers=True,
    )
    if found.event is None:
        return None, found.reason or "unmatched"
    return found.event.extra, "strict_teams_kickoff"


def provider_quotes(
    requests: list[tuple[int, dict[str, Any], dict[str, Any], str]],
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """Recover exact T-5 Crown quotes from Titan company-3 movement history."""
    fetcher = ProviderFetcher(
        PrivateResponseCache(Path("/tmp/footbreak-crown-full-board-cache")),
        rate_per_second=2.0,
        retries=2,
        timeout_seconds=25.0,
        workers=4,
        max_pages=1200,
    )
    plans = []
    for index, foot, meta, method in requests:
        titan = str(meta["fixture"])
        url = (
            "https://vip.titan007.com/changeDetail/overunder.aspx"
            f"?id={titan}&companyID=3&l=0"
        )
        target = {
            "system": "crown",
            "fixture_identity": f"persisted:{titan}",
            "market_code": "HIL",
            "line": canonical_line(foot["line"]),
            "side": "H" if foot["side"] == "O" else "L",
            "stage": "T-5",
            "predicted_at": foot["observed"] or foot["kickoff"] - dt.timedelta(minutes=5),
            "row": {
                "kickoff": foot["kickoff"].isoformat(),
                "titan_match_id": titan,
                "match_id": titan,
            },
        }
        plans.append((index, foot, meta, method, url, target))
    pages = fetcher.get_many(plan[4] for plan in plans)
    output = {}
    reasons: collections.Counter[str] = collections.Counter()
    for index, foot, meta, method, url, target in plans:
        source, cached, error = pages[url]
        if error or source is None:
            reasons[error or "empty_response"] += 1
            continue
        quote, reason = titan_candidate(
            target, source, url,
            exact_window_seconds=90,
            freshness_seconds={"T-5": 20 * 60, "T-30": 60 * 60},
        )
        if quote is None:
            reasons[reason or "no_candidate"] += 1
            continue
        output[index] = {
            "fixture": meta["fixture"],
            "odds": float(quote["odds"]),
            "observed": quote["observed_at"],
            "method": f"titan_history_{method}",
            "cached": cached,
            "quality": quote.get("evidence_quality"),
            "age_seconds": quote.get("selection_age_seconds"),
        }
    return output, {
        "requested_targets": len(plans),
        "unique_pages": len({plan[4] for plan in plans}),
        "recovered_targets": len(output),
        "failure_reasons": dict(reasons),
        "pages_fetched": fetcher.pages_fetched,
        "cache_hits": fetcher.cache_hits,
        "http_failures": fetcher.http_failures,
        "timeout_failures": fetcher.timeout_failures,
    }


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - position) + values[hi] * (position - lo)


def metrics(rows: list[dict[str, Any]], label: str, cutoff: dt.datetime | None) -> dict[str, Any]:
    returns = [row["crown_return"] for row in rows]
    settlements = collections.Counter(row["settlement"] for row in rows)
    decided = sum(settlements[key] for key in ("Won", "Half Won", "Half Lost", "Lost"))
    hits = settlements["Won"] + settlements["Half Won"]
    boot = []
    if len(rows) >= MIN_BETS:
        rng = random.Random(int(hashlib.sha256(label.encode()).hexdigest()[:8], 16))
        for _ in range(3000):
            boot.append(sum(returns[rng.randrange(len(returns))] for __ in returns) / len(returns))
    discovery = [row for row in rows if cutoff is not None and row["kickoff"] <= cutoff]
    holdout = [row for row in rows if cutoff is not None and row["kickoff"] > cutoff]
    return {
        "bets": len(rows),
        "decided": decided,
        "hit_rate_ex_push": hits / decided if decided else None,
        "mean_footbreak_odds": statistics.mean(row["footbreak_odds"] for row in rows) if rows else None,
        "mean_crown_odds": statistics.mean(row["crown_odds"] for row in rows) if rows else None,
        "mean_uplift": statistics.mean(row["uplift"] for row in rows) if rows else None,
        "roi": statistics.mean(returns) if returns else None,
        "footbreak_roi_same_sample": statistics.mean(row["footbreak_return"] for row in rows) if rows else None,
        "roi_bootstrap_95": [percentile(boot, 0.025), percentile(boot, 0.975)] if boot else None,
        "discovery_bets": len(discovery),
        "discovery_roi": statistics.mean(row["crown_return"] for row in discovery) if discovery else None,
        "holdout_bets": len(holdout),
        "holdout_roi": statistics.mean(row["crown_return"] for row in holdout) if holdout else None,
        "settlements": dict(settlements),
    }


def line_band(line: float) -> str:
    if line <= 2.25:
        return "<=2.25"
    if line == 2.5:
        return "2.50"
    if line == 2.75:
        return "2.75"
    if line == 3:
        return "3.00"
    return ">=3.25"


def uplift_band(value: float) -> str:
    if value <= 0:
        return "<=0"
    if value < 0.02:
        return "(0,0.02)"
    if value < 0.05:
        return "[0.02,0.05)"
    if value < 0.10:
        return "[0.05,0.10)"
    if value < 0.15:
        return "[0.10,0.15)"
    return ">=0.15"


def odds_band(value: float) -> str:
    if value < 1.75:
        return "<1.75"
    if value < 1.85:
        return "1.75-1.84"
    if value < 1.95:
        return "1.85-1.94"
    return ">=1.95"


def grouped(rows: list[dict[str, Any]], fields: tuple[str, ...], cutoff: dt.datetime | None) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        groups[tuple(str(row[field]) for field in fields)].append(row)
    output = []
    for key, members in groups.items():
        result = {field: value for field, value in zip(fields, key)}
        result.update(metrics(members, "|".join(key), cutoff))
        output.append(result)
    return sorted(output, key=lambda row: (-row["bets"], tuple(row[field] for field in fields)))


def main() -> None:
    foot_rows, foot_diag = extract(FOOTBREAK_HISTORY, "footbreak")
    meta = load_crown_meta()
    boards, board_diag = full_boards(meta)
    local_matches: dict[int, tuple[dict[str, Any], str]] = {}
    provider_requests = []
    pre_match_misses: collections.Counter[str] = collections.Counter()
    for index, foot in enumerate(foot_rows):
        board, method = resolve_board(foot, boards)
        if board is not None:
            local_matches[index] = (board, method)
            continue
        event, event_method = resolve_meta(foot, meta)
        if event is None:
            pre_match_misses[event_method] += 1
            continue
        provider_requests.append((index, foot, event, event_method))
    recovered_quotes, provider_diag = provider_quotes(provider_requests)

    resolved = []
    misses: collections.Counter[str] = collections.Counter()
    join_methods: collections.Counter[str] = collections.Counter()
    ordered_foot = sorted(enumerate(foot_rows), key=lambda item: (
        item[1]["kickoff"], item[1]["identity"], item[1]["line"], item[1]["side"]
    ))
    for index, foot in ordered_foot:
        board_method = local_matches.get(index)
        recovered = recovered_quotes.get(index)
        if board_method is None and recovered is None:
            _meta, meta_reason = resolve_meta(foot, meta)
            method = meta_reason if _meta is None else "historical_quote_unavailable"
            misses[method] += 1
            resolved.append({
                "identity": foot["identity"], "kickoff": foot["kickoff"].isoformat(),
                "league": foot.get("league"), "home": foot.get("home_raw"), "away": foot.get("away_raw"),
                "line": foot["line"], "side": foot["side"], "settlement": foot["settlement"],
                "footbreak_odds": foot["odds"], "crown_odds": None, "uplift": None,
                "match_status": method,
            })
            continue
        if board_method is not None:
            board, method = board_method
            quote = board["lines"][foot["line"]][foot["side"]]
            crown_fixture = board["fixture"]
            crown_odds = quote["odds"]
            observed = quote["observed"]
            price_source = "persisted_full_board"
        else:
            assert recovered is not None
            method = recovered["method"]
            crown_fixture = recovered["fixture"]
            crown_odds = recovered["odds"]
            observed = recovered["observed"]
            price_source = "titan_company_3_history"
        join_methods[method] += 1
        crown_return = return_from_settlement(foot["settlement"], crown_odds)
        foot_return = return_from_settlement(foot["settlement"], foot["odds"])
        row = {
            "identity": foot["identity"], "crown_fixture": crown_fixture,
            "kickoff": foot["kickoff"], "kickoff_iso": foot["kickoff"].isoformat(),
            "league": foot.get("league") or board.get("league") or "未分類",
            "home": foot.get("home_raw"), "away": foot.get("away_raw"),
            "line": foot["line"], "line_band": line_band(foot["line"]),
            "side": foot["side"], "settlement": foot["settlement"],
            "footbreak_odds": foot["odds"], "crown_odds": crown_odds,
            "uplift": crown_odds - foot["odds"],
            "uplift_band": uplift_band(crown_odds - foot["odds"]),
            "crown_odds_band": odds_band(crown_odds),
            "crown_return": crown_return, "footbreak_return": foot_return,
            "match_status": method,
            "quote_observed": observed.isoformat() if observed else None,
            "price_source": price_source,
        }
        resolved.append(row)

    priced = [row for row in resolved if row.get("crown_odds") is not None]
    higher = [row for row in priced if row["uplift"] > 0]
    ordered = sorted(higher, key=lambda row: row["kickoff"])
    cutoff = ordered[max(0, math.ceil(len(ordered) * 0.70) - 1)]["kickoff"] if ordered else None

    scans = {
        "overall_priced": metrics(priced, "overall_priced", cutoff),
        "crown_higher": metrics(higher, "crown_higher", cutoff),
        "by_line": grouped(higher, ("line_band",), cutoff),
        "by_direction": grouped(higher, ("side",), cutoff),
        "by_uplift": grouped(higher, ("uplift_band",), cutoff),
        "by_crown_odds": grouped(higher, ("crown_odds_band",), cutoff),
        "by_league": grouped(higher, ("league",), cutoff),
        "line_direction_uplift": grouped(higher, ("line_band", "side", "uplift_band"), cutoff),
        "league_direction": grouped(higher, ("league", "side"), cutoff),
    }
    candidates = []
    for source in ("by_line", "by_direction", "by_uplift", "by_crown_odds", "by_league",
                   "line_direction_uplift", "league_direction"):
        for row in scans[source]:
            if (
                row["bets"] >= MIN_BETS
                and row["roi"] is not None and row["roi"] > 0
                and row["discovery_bets"] >= 5 and row["holdout_bets"] >= 3
                and row["discovery_roi"] is not None and row["discovery_roi"] > 0
                and row["holdout_roi"] is not None and row["holdout_roi"] > 0
            ):
                candidates.append({"scan": source, **row})
    candidates.sort(key=lambda row: (-row["bets"], -row["roi"]))

    serializable_rows = []
    for row in resolved:
        clean = dict(row)
        if isinstance(clean.get("kickoff"), dt.datetime):
            clean.pop("kickoff")
        serializable_rows.append(clean)
    print(json.dumps({
        "definition": {
            "universe": "all unique native Footbreak T-5 graded HIL selections",
            "execution": "same fixture, exact line and Footbreak side priced from persisted Crown T-5 full board",
            "price_rule_for_strategy_scans": "Crown decimal odds strictly greater than Footbreak decimal odds",
            "validation": "global chronological 70/30 cutoff among higher-Crown-price rows",
            "minimum_candidate_sample": MIN_BETS,
        },
        "coverage": {
            "footbreak_rows": len(foot_rows),
            "crown_full_board_fixtures": len(boards),
            "priced_rows": len(priced),
            "higher_crown_price_rows": len(higher),
            "unpriced_rows": len(foot_rows) - len(priced),
            "join_methods": dict(join_methods),
            "unmatched_reasons": dict(misses),
            "cutoff_utc": cutoff.isoformat() if cutoff else None,
            "footbreak_diagnostics": foot_diag,
            "crown_snapshot_diagnostics": dict(board_diag),
            "historical_provider_diagnostics": provider_diag,
            "pre_provider_identity_misses": dict(pre_match_misses),
        },
        "scans": scans,
        "stable_positive_candidates": candidates,
        "rows": serializable_rows,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
