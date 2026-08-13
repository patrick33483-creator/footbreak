#!/usr/bin/env python3
"""Read-only 14-day PinnAPI source-health diagnostics for Footbreak and Crown.

The report only reads immutable learning rows through SQLite ``mode=ro``.  Its
metrics use exactly one latest pre-kickoff stage per fixture+market.  Crucially,
an immutable snapshot can prove an *observed* source failure, but cannot prove
that no invocation occurred when a historical invocation persisted no snapshot;
historical no-prediction counts are therefore explicitly lower bounds.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPORT = "pinnapi_source_health"
SCHEMA_VERSION = 2
SYSTEMS = ("footbreak", "crown")
WINDOW_DAYS = 14
STAGE_PRIORITY = ("T-5", "T-30", "首預")
STAGE_RANK = {stage: len(STAGE_PRIORITY) - index for index, stage in enumerate(STAGE_PRIORITY)}
MIN_LEAGUE_FIXTURES = 10
DELAY_AGE_SECONDS = 120.0


def finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def time_value(value: Any) -> float | None:
    number = finite(value)
    if number is not None:
        return number
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).timestamp()


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def load_rows(path: str | Path, system: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Return canonical pre-kickoff snapshot/grade raw rows without writing."""
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = 1")
        rows = connection.execute(
            """
            WITH snapshots AS (
                SELECT snapshot_id, fixture_id, stage, generated_at, kickoff, payload_json,
                       ROW_NUMBER() OVER (
                           PARTITION BY fixture_id, stage
                           ORDER BY generated_at DESC, snapshot_id DESC
                       ) AS snapshot_rank
                FROM prediction_snapshots
                WHERE system = ? AND pre_kickoff = 1
                  AND kickoff >= ? AND kickoff < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM stage_snapshot_reconciliations AS r
                      WHERE r.system = prediction_snapshots.system
                        AND r.fixture_id = prediction_snapshots.fixture_id
                        AND r.stage = prediction_snapshots.stage
                        AND r.canonical_snapshot_id != prediction_snapshots.snapshot_id
                  )
            ), latest_grades AS (
                SELECT grade_id, snapshot_id, market, target, state, metrics_json,
                       ROW_NUMBER() OVER (
                           PARTITION BY snapshot_id, market, target
                           ORDER BY grade_attempt DESC, grade_id DESC
                       ) AS grade_rank
                FROM grades
            )
            SELECT snapshots.snapshot_id, snapshots.fixture_id, snapshots.stage,
                   snapshots.generated_at, snapshots.kickoff, snapshots.payload_json,
                   latest_grades.market AS grade_market, latest_grades.target AS grade_target,
                   latest_grades.state AS grade_state, latest_grades.metrics_json
            FROM snapshots
            LEFT JOIN latest_grades ON latest_grades.snapshot_id = snapshots.snapshot_id
                                   AND latest_grades.grade_rank = 1
            WHERE snapshots.snapshot_rank = 1
            ORDER BY snapshots.kickoff, snapshots.fixture_id, snapshots.stage
            """,
            (system, since.isoformat(), until.isoformat()),
        ).fetchall()
    finally:
        connection.close()
    output = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        try:
            metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            metrics = {}
        output.append({**dict(row), "payload": payload if isinstance(payload, dict) else {},
                       "metrics": metrics if isinstance(metrics, dict) else {}})
    return output


def source_status(payload: dict[str, Any], system: str) -> str:
    explicit = str(payload.get("source_status") or "").strip()
    if explicit:
        return explicit
    if system == "crown":
        if payload.get("sharp_reference_available") is True:
            return "pinnapi_live"
        if not payload.get("pinnapi_event_id"):
            return "pinnapi_fixture_unmatched"
        if payload.get("edge_reference_status") == "unavailable":
            return "pinnapi_live_unavailable"
        return "pinnapi_status_unobserved"
    return "source_status_unobserved"


def source_kind(payload: dict[str, Any], system: str) -> str:
    """Classify only from persisted, verifiable provenance—never inference."""
    source = payload.get("source")
    if source in {"pinnapi_live", "fallback", "hkjc_full_market", "unavailable"}:
        return str(source)
    if system == "crown":
        # Crown snapshots have independent verifiable fields.  Historical rows
        # are live only where an event ID and explicit sharp-reference flag (or
        # a persisted available reference status) agree.
        if payload.get("pinnapi_event_id") and (
            payload.get("sharp_reference_available") is True
            or payload.get("edge_reference_status") == "available"
        ):
            return "pinnapi_live_legacy"
        return "without_pinnapi"
    if payload.get("model_source") == "pinnapi" and payload.get("sharp_reference_available") is True:
        return "pinnapi_live_legacy"
    return "without_pinnapi"


def has_pinnapi(source: str) -> bool:
    return source in {"pinnapi_live", "pinnapi_live_legacy"}


def source_delayed(payload: dict[str, Any], source: str, system: str) -> bool:
    age = finite(payload.get("data_age_seconds"))
    if age is None and system == "crown":
        observed = time_value(payload.get("source_snapshot_at"))
        source_at = time_value(payload.get("pinnapi_source_at"))
        if observed is not None and source_at is not None:
            age = max(0.0, observed - source_at)
    return source == "fallback" or bool(age is not None and age > DELAY_AGE_SECONDS)


def aggregate_decisions(observations: list[dict[str, Any]]) -> dict[str, Any]:
    settled = pushes = hits = 0
    for item in observations:
        grade = item.get("grade") or {}
        metrics = grade.get("metrics") or {}
        if grade.get("state") != "GRADED":
            continue
        hit = metrics.get("hit")
        target = finite(metrics.get("target"))
        if hit is None or target == 0.5:
            pushes += 1
            continue
        settled += 1
        hits += int(hit is True)
    return {
        "fixture_market_latest_stage_rows": len(observations),
        "settled_decisions": settled,
        "pushes_excluded": pushes,
        "unsettled_or_ungraded": len(observations) - settled - pushes,
        "hits": hits,
        "hit_rate": ratio(hits, settled),
    }


def _add_metrics(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    keys = ("fixture_market_latest_stage_rows", "settled_decisions", "pushes_excluded",
            "unsettled_or_ungraded", "hits")
    result = {key: int(left.get(key, 0) or 0) + int(right.get(key, 0) or 0) for key in keys}
    result["hit_rate"] = ratio(result["hits"], result["settled_decisions"])
    return result


def _observability(stages: dict[int, dict[str, Any]], latest_fixture_stage: dict[str, dict[str, Any]],
                   observed_no_prediction: int) -> dict[str, Any]:
    statuses = Counter(str(stage.get("source_status") or "") for stage in stages.values())
    source_provenance = sum(status not in {"", "source_status_unobserved", "pinnapi_status_unobserved"}
                            for status in statuses for _ in range(statuses[status]))
    # A zero must never be presented as evidence that no historical invocation
    # failed: attempts which never wrote an immutable snapshot cannot be seen.
    status = "lower_bound" if stages else "unavailable"
    historical_no_prediction_count = {
        "observed_count": observed_no_prediction if stages else None,
        "count_status": status,
        "reason": (
            "Immutable snapshots observe only persisted attempts; historical invocations "
            "that produced no snapshot are unknowable from this database."
        ),
    }
    return {
        "observed_pre_kickoff_stage_rows": len(stages),
        "observed_unique_fixtures": len(latest_fixture_stage),
        "observed_stage_rows_with_source_status": source_provenance,
        "observed_stage_rows_without_source_status": len(stages) - source_provenance,
        "historical_no_prediction_count": historical_no_prediction_count,
        # Explicit source label retained for consumers of the initial report
        # schema; both objects have identical lower-bound semantics.
        "historical_no_prediction_due_to_pinnapi": historical_no_prediction_count,
    }


def build_system_report(path: str | Path, system: str, *, now: datetime,
                        window_days: int) -> dict[str, Any]:
    since = now - timedelta(days=window_days)
    raw = load_rows(path, system, since, now)
    stages: dict[int, dict[str, Any]] = {}
    for row in raw:
        snapshot_id = int(row["snapshot_id"])
        stage = stages.setdefault(snapshot_id, {
            "fixture_id": str(row["fixture_id"]), "stage": str(row["stage"]),
            "kickoff": row["kickoff"], "payload": row["payload"], "grades": [],
        })
        if row.get("grade_market"):
            stage["grades"].append({"market": row["grade_market"], "target": row["grade_target"],
                                    "state": row["grade_state"], "metrics": row["metrics"]})

    latest_fixture_stage: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    for stage in stages.values():
        payload = stage["payload"]
        kind = source_kind(payload, system)
        status = source_status(payload, system)
        stage.update({"source": kind, "source_status": status,
                      "league": str(payload.get("league") or "未知聯賽"),
                      "delayed": source_delayed(payload, kind, system)})
        fixture_id = stage["fixture_id"]
        old = latest_fixture_stage.get(fixture_id)
        if old is None or STAGE_RANK.get(stage["stage"], 0) > STAGE_RANK.get(old["stage"], 0):
            latest_fixture_stage[fixture_id] = stage
        for prediction in payload.get("market_predictions") or []:
            if not isinstance(prediction, dict) or not prediction.get("code"):
                continue
            market = str(prediction["code"])
            matched = [grade for grade in stage["grades"] if grade["market"] == market]
            condition = str(prediction.get("condition", prediction.get("line", "")))
            side = str(prediction.get("side") or "")
            grade = next((item for item in matched if str(item.get("target") or "") == f"{condition}|{side}"),
                         matched[0] if matched else None)
            candidates.append({"fixture_id": fixture_id, "market": market, "stage": stage["stage"],
                               "league": stage["league"], "source": kind, "delayed": stage["delayed"],
                               "grade": grade})

    latest_market: dict[tuple[str, str], dict[str, Any]] = {}
    for observation in candidates:
        key = (observation["fixture_id"], observation["market"])
        old = latest_market.get(key)
        if old is None or STAGE_RANK.get(observation["stage"], 0) > STAGE_RANK.get(old["stage"], 0):
            latest_market[key] = observation
    primary = list(latest_market.values())

    fixture_categories, fixture_sources = Counter(), Counter()
    league_fixtures: dict[str, list[dict[str, Any]]] = defaultdict(list)
    observed_no_prediction = 0
    for stage in latest_fixture_stage.values():
        payload, kind, status = stage["payload"], stage["source"], stage["source_status"]
        predictions = [row for row in payload.get("market_predictions") or []
                       if isinstance(row, dict) and row.get("code")]
        pinnapi_id = payload.get("pinnapi_event_id") or payload.get("pinnapi_fixture_identity")
        source_failure = (
            kind == "unavailable" or "unavailable" in status or "no_prediction_due" in status
            or "provider" in status or "analysis_exception" in status
        )
        if not predictions and source_failure:
            category = "no_prediction_due_to_source"
            observed_no_prediction += 1
        elif predictions and (not pinnapi_id or "unmatched" in status):
            category = "predicted_but_unmatched"
        elif has_pinnapi(kind):
            category = "with_pinnapi"
        else:
            category = "without_pinnapi"
        fixture_categories[category] += 1
        fixture_sources[kind] += 1
        league_fixtures[stage["league"]].append({
            "source": kind, "delayed": stage["delayed"],
            "matched": bool(pinnapi_id) or has_pinnapi(kind) or kind == "fallback",
        })

    by_source = {
        "with_pinnapi": aggregate_decisions([item for item in primary if has_pinnapi(item["source"])]),
        "without_pinnapi": aggregate_decisions([item for item in primary if not has_pinnapi(item["source"])]),
    }
    per_league_obs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in primary:
        per_league_obs[item["league"]].append(item)
    leagues = []
    for league, fixtures in league_fixtures.items():
        metrics = aggregate_decisions(per_league_obs.get(league, []))
        total = len(fixtures)
        leagues.append({
            "league": league, "fixtures": total,
            "fixture_market_latest_stage_rows": metrics["fixture_market_latest_stage_rows"],
            "missing_rate": ratio(sum(not has_pinnapi(item["source"]) for item in fixtures), total),
            "delay_rate": ratio(sum(bool(item["delayed"]) for item in fixtures), total),
            "match_rate": ratio(sum(bool(item["matched"]) for item in fixtures), total),
            "settled_decisions": metrics["settled_decisions"], "hit_rate": metrics["hit_rate"],
            "sample_status": "sufficient" if total >= MIN_LEAGUE_FIXTURES else "insufficient",
            "candidate_only": True,
        })
    leagues.sort(key=lambda item: (-(item["missing_rate"] or 0), -(item["delay_rate"] or 0),
                                   item["match_rate"] if item["match_rate"] is not None else 1,
                                   -item["fixtures"], item["league"]))
    return {
        "system": system,
        "fixture_categories": {key: int(fixture_categories.get(key, 0)) for key in (
            "no_prediction_due_to_source", "predicted_but_unmatched", "with_pinnapi", "without_pinnapi")},
        "fixture_sources": dict(sorted(fixture_sources.items())),
        "coverage": _observability(stages, latest_fixture_stage, observed_no_prediction),
        "primary_metrics": {"all": aggregate_decisions(primary), **by_source},
        "league_health_candidates": leagues,
    }


def combined_summary(systems: dict[str, dict[str, Any]]) -> dict[str, Any]:
    categories = Counter()
    all_metrics = {"fixture_market_latest_stage_rows": 0, "settled_decisions": 0,
                   "pushes_excluded": 0, "unsettled_or_ungraded": 0, "hits": 0}
    with_metrics, without_metrics = dict(all_metrics), dict(all_metrics)
    coverage = Counter()
    availability = {}
    for name, report in systems.items():
        availability[name] = report.get("coverage", {}).get("historical_no_prediction_due_to_pinnapi", {}).get("count_status")
        categories.update(report.get("fixture_categories") or {})
        for aggregate, key in ((all_metrics, "all"), (with_metrics, "with_pinnapi"), (without_metrics, "without_pinnapi")):
            aggregate.update(_add_metrics(aggregate, (report.get("primary_metrics") or {}).get(key) or {}))
        for key in ("observed_pre_kickoff_stage_rows", "observed_unique_fixtures",
                    "observed_stage_rows_with_source_status", "observed_stage_rows_without_source_status"):
            coverage[key] += int((report.get("coverage") or {}).get(key, 0) or 0)
    return {
        "fixture_categories": {key: int(categories.get(key, 0)) for key in (
            "no_prediction_due_to_source", "predicted_but_unmatched", "with_pinnapi", "without_pinnapi")},
        "primary_metrics": {"all": all_metrics, "with_pinnapi": with_metrics,
                            "without_pinnapi": without_metrics},
        "coverage": {**dict(coverage), "historical_no_prediction_count_status_by_system": availability,
                     "historical_no_prediction_count_status": "lower_bound_or_unavailable"},
    }


def build_report(path: str | Path, *, now: datetime | None = None,
                 window_days: int = WINDOW_DAYS) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reports = {system: build_system_report(path, system, now=now, window_days=window_days) for system in SYSTEMS}
    since = now - timedelta(days=window_days)
    return {
        "schema_version": SCHEMA_VERSION, "report": REPORT, "generated_at": now.isoformat(),
        "window": {"days": window_days, "from": since.isoformat(), "to": now.isoformat()},
        "read_only": True,
        "policy": {
            "primary_unit": "one_latest_pre_kickoff_stage_per_fixture_market",
            "stage_priority": list(STAGE_PRIORITY), "push_and_unsettled_excluded_from_hit_rate": True,
            "minimum_league_fixtures": MIN_LEAGUE_FIXTURES, "delay_age_seconds": DELAY_AGE_SECONDS,
            "league_candidates_are_not_auto_filtered": True,
            "fallback_never_enables_ev_kelly_official_portfolio_or_notification": True,
            "historical_no_prediction_counts_are_observed_lower_bounds": True,
        },
        "systems": reports, "combined_summary": combined_summary(reports),
    }


def _public_system(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: report.get(key) for key in ("system", "fixture_categories", "fixture_sources", "coverage", "primary_metrics")
    } | {"league_health_candidates": [{key: row.get(key) for key in (
        "league", "fixtures", "fixture_market_latest_stage_rows", "missing_rate", "delay_rate",
        "match_rate", "settled_decisions", "hit_rate", "sample_status", "candidate_only")}
        for row in (report.get("league_health_candidates") or [])[:50]]}


def public_view(report: dict[str, Any]) -> dict[str, Any]:
    """Bounded aggregate public projection, with no fixture/team/provider IDs."""
    return {key: report.get(key) for key in ("schema_version", "report", "generated_at", "window", "read_only", "policy", "combined_summary")} | {
        "systems": {name: _public_system(value) for name, value in (report.get("systems") or {}).items() if isinstance(value, dict)}
    }


def write_atomic(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pinnapi-source-health-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def run(learning_db: str | Path, *, out: str | Path | None = None,
        public: str | Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    report = build_report(learning_db, now=now)
    if out: write_atomic(out, report)
    if public: write_atomic(public, public_view(report))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--learning-db", required=True); parser.add_argument("--out", required=True)
    parser.add_argument("--public"); parser.add_argument("--lock")
    args = parser.parse_args(); lock = None
    try:
        if args.lock:
            Path(args.lock).parent.mkdir(parents=True, exist_ok=True)
            lock = open(args.lock, "a+", encoding="utf-8")
            try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise SystemExit(75)
        run(args.learning_db, out=args.out, public=args.public)
    finally:
        if lock is not None: lock.close()


if __name__ == "__main__":
    main()
