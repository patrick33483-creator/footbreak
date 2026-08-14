#!/usr/bin/env python3
"""Emit a compact, read-only production result-state audit as JSON."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


FOOTBREAK_DATA = Path("/var/www/footbreak/data.json")
CROWN_DATA = Path("/var/www/crown/data.json")
CROWN_HISTORY = Path("/var/lib/footbreak/crown/prediction_history.json")
CROWN_LEDGER = Path("/var/lib/footbreak/crown/ledger.json")
CHALLENGER_STATUS = Path("/var/lib/footbreak/challenger/latest.json")
DATA_HEALTH_REPORTS = {
    "footbreak": Path("/var/www/footbreak/data-health.json"),
    "crown": Path("/var/www/crown/data-health.json"),
}
PINNAPI_SOURCE_HEALTH_REPORT = Path("/var/www/footbreak/pinnapi-source-health.json")
ODDS_RECOVERY_EVIDENCE = {
    "footbreak": [
        Path("/opt/footbreak/system/snapshots"),
        Path("/opt/footbreak/system/hk_snapshots.json"),
        Path("/var/lib/footbreak/prediction_history_archive.json"),
        Path("/opt/footbreak/system/sim_ledger.json"),
    ],
    "crown": [
        Path("/var/lib/footbreak/crown/prediction_history.json"),
        Path("/var/lib/footbreak/crown/ledger.json"),
        Path("/var/lib/footbreak/crown/source_snapshots"),
    ],
}
HKT = timezone(timedelta(hours=8))
sys.path.insert(0, "/opt/footbreak")

from crown.common import SETTLE_AFTER_SECONDS, parse_time  # noqa: E402
from crown.ledger import PREDICTION_ERA, completed_stages  # noqa: E402
from crown.matching import MATCHING_VERSION  # noqa: E402
from analysis.three_stage_consensus import (  # noqa: E402
    MARKETS,
    STAGES,
    calculate_three_stage_consensus,
)


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        value = payload.get("rows") or []
        return value if isinstance(value, list) else []
    return []


def compact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "match_id",
            "hkjc_match_id",
            "titan_match_id",
            "kickoff",
            "home",
            "away",
            "stage",
            "result_status",
            "score",
            "result_source",
            "result_missing_reason",
            "result_detail",
            "market_predictions",
            "market_grades",
        )
    }


def timer_state() -> dict[str, str]:
    completed = subprocess.run(
        [
            "systemctl",
            "show",
            "footbreak-result-reconcile.timer",
            "--no-pager",
            "--property=ActiveState",
            "--property=UnitFileState",
            "--property=LastTriggerUSec",
            "--property=NextElapseUSecRealtime",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    state: dict[str, str] = {"returncode": str(completed.returncode)}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            state[key] = value
    if completed.stderr.strip():
        state["stderr"] = completed.stderr.strip()
    return state


def challenger_state() -> dict[str, Any]:
    if not CHALLENGER_STATUS.is_file():
        return {"available": False}
    payload = load(CHALLENGER_STATUS)
    systems: dict[str, Any] = {}
    for system, system_payload in (payload.get("systems") or {}).items():
        tests = (system_payload or {}).get("tests") or {}
        systems[system] = {
            "review_required": bool((system_payload or {}).get("review_required")),
            "tests": {
                market: {
                    **{
                        key: test.get(key)
                        for key in (
                            "status",
                            "eligible_fixtures",
                            "eligible_rows",
                            "required_fixtures",
                            "remaining_fixtures",
                            "train_fixtures",
                            "holdout_fixtures",
                            "train_rows",
                            "holdout_rows",
                            "champion",
                            "challenger",
                            "delta",
                            "checks",
                            "rejection_reasons",
                            "model_version_hash",
                            "auto_apply",
                        )
                        if key in test
                    },
                    **(
                        {
                            "prospective_chl": {
                                key: test["prospective_chl"].get(key)
                                for key in (
                                    "market",
                                    "status",
                                    "model_version",
                                    "state_version_hash",
                                    "freeze_cutoff",
                                    "cutoff_boundary",
                                    "primary_unit",
                                    "primary_stage_rule",
                                    "selected_strategy",
                                    "selection",
                                    "minimum_prospective_fixtures",
                                    "strong_sample_fixtures",
                                    "prospective_fixtures",
                                    "prospective_rows",
                                    "remaining_fixtures",
                                    "sample_warning",
                                    "stage_diagnostics",
                                    "closing_reference",
                                    "feature_coverage",
                                    "champion",
                                    "baselines",
                                    "challenger",
                                    "delta",
                                    "checks",
                                    "rejection_reasons",
                                    "shadow_returns",
                                    "champion_shadow_returns",
                                    "reason",
                                    "auto_apply",
                                    "retraining",
                                    "live_integration",
                                )
                                if key in test["prospective_chl"]
                            }
                        }
                        if isinstance(test.get("prospective_chl"), dict)
                        else {}
                    ),
                    **(
                        {
                            "prospective_v3": {
                                key: test["prospective_v3"].get(key)
                                for key in (
                                    "status",
                                    "model_version",
                                    "state_version_hash",
                                    "freeze_cutoff",
                                    "selected_spec",
                                    "minimum_prospective_fixtures",
                                    "prospective_fixtures",
                                    "prospective_rows",
                                    "remaining_fixtures",
                                    "champion",
                                    "challenger",
                                    "delta",
                                    "checks",
                                    "rejection_reasons",
                                    "auto_apply",
                                )
                                if key in test["prospective_v3"]
                            }
                        }
                        if isinstance(test.get("prospective_v3"), dict)
                        else {}
                    ),
                }
                for market, test in tests.items()
            },
        }
    return {
        "available": True,
        "generated_at": payload.get("generated_at"),
        "policy": payload.get("policy"),
        "review_required": bool(payload.get("review_required")),
        "systems": systems,
    }


def data_health_state() -> dict[str, Any]:
    """Compact, row-free summary of both public data-health artifacts.

    Only aggregate counts, coverage ratios, issue counts and recommendation
    titles are exposed.  No fixture identifier, payload, coefficient, or raw
    private row can reach the audit output, and a missing or malformed report
    never fails the audit.
    """
    systems: dict[str, Any] = {}
    for system, path in DATA_HEALTH_REPORTS.items():
        if not path.is_file():
            systems[system] = {"available": False, "reason": "artifact_missing"}
            continue
        try:
            report = load(path)
        except (OSError, json.JSONDecodeError) as exc:
            systems[system] = {"available": False, "reason": type(exc).__name__}
            continue
        if not isinstance(report, dict) or report.get("report") != "data_health":
            systems[system] = {"available": False, "reason": "unexpected_report"}
            continue
        try:
            summary = data_health_summary(report)
        except Exception as exc:  # pragma: no cover - defensive only
            systems[system] = {"available": False, "reason": type(exc).__name__}
            continue
        systems[system] = {"available": True, **summary}
    return {
        "read_only": True,
        "auto_apply": False,
        "retraining": False,
        "primary_sample": "unique_fixtures",
        "stage_rows_are_reference_only": True,
        "systems": systems,
    }


def odds_recovery_state(footbreak: dict[str, Any], crown: dict[str, Any]) -> dict[str, Any]:
    """Read-only compact recovery inventory; never exposes production paths."""
    try:
        from analysis.odds_recovery import report
        payload, _ = report(
            {
                "footbreak": rows(footbreak.get("prediction_history") or {}),
                "crown": rows(crown),
            },
            ODDS_RECOVERY_EVIDENCE,
        )
        return payload
    except Exception as exc:
        # Audit must remain available if an old deployment lacks the optional
        # recovery module or a historical evidence file is malformed.
        return {"available": False, "reason": type(exc).__name__}


def data_health_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Whitelist projection of one data-health report."""
    overall = (report.get("completeness") or {}).get("overall") or {}
    result = overall.get("result") or {}
    corner = overall.get("corner_result") or {}
    baseline = report.get("baseline") or {}
    primary = report.get("primary_diagnostic") or {}
    primary_baseline = primary.get("baseline") or {}
    diagnostics = report.get("hil_v4_diagnostics") or {}
    return {
        "schema_version": report.get("schema_version"),
        "system": report.get("system"),
        "scope": {
            key: (report.get("scope") or {}).get(key)
            for key in ("model_version", "pre_kickoff_only")
        },
        "generated_at": report.get("generated_at"),
        "status": report.get("status"),
        "status_reason": report.get("status_reason"),
        "counts": {
            key: overall.get(key, 0)
            for key in (
                "unique_fixtures",
                "stage_rows",
                "prediction_rows",
                "graded_rows",
                "pending_rows",
                "excluded_rows",
                "duplicate_stage_keys",
                "quarantined_post_kickoff_rows",
            )
        },
        "coverage": {
            "result": result.get("coverage"),
            "corner_result": corner.get("coverage"),
            "stale_unresolved_fixtures": result.get("stale_unresolved_fixtures", 0),
            "stale_missing_corner_fixtures": corner.get("stale_beyond_retry_fixtures", 0),
        },
        # The metric unit travels with every number so an audit reader can
        # never mistake a graded-row aggregate for one row per fixture.
        "metrics_policy": {
            key: (report.get("metrics_policy") or {}).get(key)
            for key in (
                "sample_basis",
                "metric_unit",
                "correlated_stage_rows",
                "primary_diagnostic_metric_unit",
                "metrics_are_one_per_fixture",
                "recommendation_evidence_unit",
            )
        },
        "baseline": {
            key: baseline.get(key)
            for key in (
                "unique_fixtures",
                "graded_rows",
                "accuracy",
                "brier",
                "log_loss",
                "sample_status",
                "sample_basis",
                "metric_unit",
                "correlated_stage_rows",
            )
        },
        "primary_diagnostic_baseline": {
            "metric_unit": primary.get("unit"),
            "stage_priority": primary.get("stage_priority"),
            **{
                key: primary_baseline.get(key)
                for key in (
                    "unique_fixtures",
                    "graded_rows",
                    "accuracy",
                    "brier",
                    "log_loss",
                    "sample_status",
                    "sample_basis",
                    "correlated_stage_rows",
                )
            },
        },
        "issue_counts": report.get("issue_counts") or {},
        "top_issues": [
            {key: issue.get(key) for key in ("code", "severity", "scope", "count")}
            for issue in (report.get("issues") or [])[:5]
            if isinstance(issue, dict)
        ],
        "top_recommendations": [
            {key: item.get(key) for key in ("id", "kind", "priority", "title")}
            for item in (diagnostics.get("recommendations") or [])[:5]
            if isinstance(item, dict)
        ],
        "recommendation_evidence_unit": diagnostics.get("evidence_unit"),
        "recommendation_uses_repeated_stage_rows": diagnostics.get(
            "evidence_uses_repeated_stage_rows"
        ),
        "minimum_unique_fixtures": (report.get("policy") or {}).get(
            "minimum_unique_fixtures"
        ),
    }


def pinnapi_source_health_state() -> dict[str, Any]:
    """Return a small aggregate-only multi-system health summary; never raise."""
    if not PINNAPI_SOURCE_HEALTH_REPORT.is_file():
        return {"available": False, "reason": "artifact_missing"}
    try:
        report = load(PINNAPI_SOURCE_HEALTH_REPORT)
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": type(exc).__name__}
    if not isinstance(report, dict) or report.get("report") != "pinnapi_source_health":
        return {"available": False, "reason": "unexpected_report"}
    policy = report.get("policy") or {}
    systems: dict[str, Any] = {}
    for system, value in (report.get("systems") or {}).items():
        if not isinstance(value, dict):
            continue
        metrics = value.get("primary_metrics") or {}
        coverage = value.get("coverage") or {}
        systems[str(system)] = {
            "fixture_categories": {
                key: (value.get("fixture_categories") or {}).get(key, 0)
                for key in (
                    "no_prediction_due_to_source",
                    "predicted_but_unmatched",
                    "with_pinnapi",
                    "without_pinnapi",
                )
            },
            "coverage": {
                key: coverage.get(key)
                for key in (
                    "observed_pre_kickoff_stage_rows",
                    "observed_unique_fixtures",
                    "observed_stage_rows_with_source_status",
                    "observed_stage_rows_without_source_status",
                )
            } | {
                "historical_no_prediction_count": {
                    key: (
                        coverage.get("historical_no_prediction_count")
                        or coverage.get("historical_no_prediction_due_to_pinnapi")
                        or {}
                    ).get(key)
                    for key in ("observed_count", "count_status", "reason")
                }
            },
            "primary_metrics": {
                key: {
                    item: (metrics.get(key) or {}).get(item)
                    for item in (
                        "fixture_market_latest_stage_rows", "settled_decisions",
                        "pushes_excluded", "unsettled_or_ungraded", "hits", "hit_rate",
                    )
                }
                for key in ("all", "with_pinnapi", "without_pinnapi")
            },
            "top_league_candidates": [
                {
                    key: row.get(key)
                    for key in (
                        "league", "fixtures", "missing_rate", "delay_rate", "match_rate",
                        "settled_decisions", "hit_rate", "sample_status", "candidate_only",
                    )
                }
                for row in (value.get("league_health_candidates") or [])[:10]
                if isinstance(row, dict)
            ],
        }
    combined = report.get("combined_summary") or {}
    return {
        "available": True,
        "read_only": bool(report.get("read_only")),
        "generated_at": report.get("generated_at"),
        "window": {
            key: (report.get("window") or {}).get(key)
            for key in ("days", "from", "to")
        },
        "combined_summary": {
            "fixture_categories": {
                key: (combined.get("fixture_categories") or {}).get(key, 0)
                for key in (
                    "no_prediction_due_to_source",
                    "predicted_but_unmatched",
                    "with_pinnapi",
                    "without_pinnapi",
                )
            },
            "coverage": (combined.get("coverage") or {}),
            "primary_metrics": (combined.get("primary_metrics") or {}),
        },
        "policy": {
            key: policy.get(key)
            for key in (
                "primary_unit", "push_and_unsettled_excluded_from_hit_rate",
                "minimum_league_fixtures", "league_candidates_are_not_auto_filtered",
                "historical_no_prediction_counts_are_observed_lower_bounds",
            )
        },
        "systems": systems,
        "journal_supplemental": pinnapi_journal_supplemental_state(),
    }


def pinnapi_journal_supplemental_state() -> dict[str, Any]:
    """Bounded local journal counts; never expose lines, IDs, teams, or errors."""
    units = (
        "footbreak-tick.service", "footbreak-t30.service", "footbreak-sweep.service",
        "crown-tick.service", "crown-sweep.service",
    )
    try:
        completed = subprocess.run(
            [
                "journalctl", "--since", "14 days ago", "--no-pager", "--output=cat",
                "--lines=4000", *[f"--unit={unit}" for unit in units],
            ],
            check=False, capture_output=True, text=True, timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": type(exc).__name__}
    if completed.returncode not in {0, 1}:
        return {"available": False, "reason": f"journalctl_rc_{completed.returncode}"}
    lines = completed.stdout.splitlines()
    invocation_pattern = re.compile(r"(?:═══|Starting |Started |run_predict\.py|crown.*(?:tick|sweep))", re.I)
    pinnapi_pattern = re.compile(r"pinnapi", re.I)
    error_pattern = re.compile(r"(?:unavailable|不可用|providererror|error|failed|outage)", re.I)
    pinnapi_lines = [line for line in lines if pinnapi_pattern.search(line)]
    return {
        "available": True,
        "window_days": 14,
        "lines_scanned": len(lines),
        "lines_truncated_at": 4000,
        "service_invocations_observed": sum(bool(invocation_pattern.search(line)) for line in lines),
        "pinnapi_mentions_observed": len(pinnapi_lines),
        "pinnapi_error_mentions_observed": sum(bool(error_pattern.search(line)) for line in pinnapi_lines),
        "counts_are_supplemental_not_fixture_level": True,
        "raw_journal_lines_excluded": True,
    }


def handicap_world_settlement_state(
    ledger: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return only aggregate Handicap World settlement visibility.

    This intentionally excludes fixture/team/provider identifiers, bet rows,
    and timestamps.  Pending reasons are a closed machine-readable vocabulary
    from Crown settlement rather than copied ledger text.
    """
    world = ledger.get("handicap_world")
    if not isinstance(world, dict):
        return {"available": False, "reason": "portfolio_missing"}
    signals = world.get("signals") if isinstance(world.get("signals"), list) else []
    bets = world.get("bets") if isinstance(world.get("bets"), list) else []
    observed_now = now or datetime.now(HKT)
    pending_reasons = {
        "pinnapi_live_cache_fresh",
        "pinnapi_live_cache_stale_fallback_unresolved",
        "verified_result_unavailable",
    }
    strategies: dict[str, dict[str, int]] = {}
    for strategy in ("fixed_stake", "conservative_kelly"):
        legs = [
            bet for bet in bets
            if isinstance(bet, dict) and bet.get("strategy") == strategy
        ]
        pending = [bet for bet in legs if bet.get("status") == "PENDING"]
        due = [
            bet for bet in pending
            if (kickoff := parse_time(bet.get("kickoff"))) is not None
            and (observed_now - kickoff).total_seconds() >= SETTLE_AFTER_SECONDS
        ]
        strategies[strategy] = {
            "bets": len(legs),
            "pending": len(pending),
            "due": len(due),
            "settled": sum(bet.get("status") == "SETTLED" for bet in legs),
            "with_last_attempt": sum(bool(bet.get("last_settlement_attempt_at")) for bet in legs),
        }
    pending_reason_counts = {
        reason: sum(
            isinstance(bet, dict)
            and bet.get("status") == "PENDING"
            and bet.get("settlement_pending_reason") == reason
            for bet in bets
        )
        for reason in sorted(pending_reasons)
    }
    return {
        "available": True,
        "read_only": True,
        "row_free": True,
        "signals": sum(isinstance(signal, dict) for signal in signals),
        "strategies": strategies,
        "diagnostics": {
            "bets_with_last_attempt": sum(
                isinstance(bet, dict) and bool(bet.get("last_settlement_attempt_at"))
                for bet in bets
            ),
            "pending_reason_counts": pending_reason_counts,
        },
    }


def crown_corner_state(
    payload: dict[str, Any],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(HKT)
    recent_cutoff = now - timedelta(hours=12)
    future_cutoff = now + timedelta(hours=36)
    audited: list[dict[str, Any]] = []
    for match in payload.get("matches") or []:
        try:
            kickoff = datetime.fromisoformat(
                str(match.get("kickoff_hkt") or match.get("kickoff") or "").replace(
                    "Z", "+00:00"
                )
            )
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=HKT)
        except ValueError:
            continue
        if not recent_cutoff <= kickoff <= future_cutoff:
            continue
        hkjc_chl = (match.get("book_odds") or {}).get("hkjc_chl") or []
        forecasts = match.get("forecast_candidates") or []
        candidates = match.get("candidates") or []
        watch = (ledger.get("watch") or {}).get(str(match.get("match_id")), {})
        computed_done = completed_stages(
            watch,
            MATCHING_VERSION,
            PREDICTION_ERA,
        )
        audited.append(
            {
                "match_id": match.get("match_id"),
                "kickoff_hkt": match.get("kickoff_hkt") or match.get("kickoff"),
                "home": match.get("home"),
                "away": match.get("away"),
                "stage": match.get("stage"),
                "status": match.get("status"),
                "generated_at": match.get("generated_at"),
                "prediction_source": match.get("prediction_source"),
                "forecast_codes": [
                    row.get("code") for row in forecasts
                ],
                "hkjc_match_id": match.get("hkjc_match_id"),
                "pinnapi_event_id": match.get("pinnapi_event_id"),
                "pinnapi_corner_event_id": match.get("pinnapi_corner_event_id"),
                "hkjc_chl_lines": len(hkjc_chl),
                "hkjc_chl": hkjc_chl,
                "chl_forecast_count": sum(
                    row.get("code") == "CHL" for row in forecasts
                ),
                "chl_candidate_count": sum(
                    row.get("code") == "CHL" for row in candidates
                ),
                "corner_no_bet_reason": match.get("corner_no_bet_reason"),
                "edge_reference_status": match.get("edge_reference_status"),
                "edge_reference_note": match.get("edge_reference_note"),
                "watch_matching_version": watch.get("matching_version"),
                "watch_prediction_era": watch.get("prediction_era"),
                "watch_stages": [
                    {
                        "stage": row.get("stage"),
                        "ts": row.get("ts"),
                        "prediction_era": row.get("prediction_era"),
                        "market_codes": [
                            item.get("code")
                            for item in (row.get("market_predictions") or [])
                        ],
                    }
                    for row in (watch.get("stages") or [])
                ],
                "computed_completed_stages": sorted(computed_done),
            }
        )
    audited.sort(key=lambda row: str(row.get("kickoff_hkt") or ""), reverse=True)
    return {
        "dashboard_generated_at": payload.get("generated_at"),
        "window": {
            "from": recent_cutoff.isoformat(),
            "to": future_cutoff.isoformat(),
        },
        "match_count": len(audited),
        "with_hkjc_chl": sum(bool(row["hkjc_chl_lines"]) for row in audited),
        "with_chl_forecast": sum(bool(row["chl_forecast_count"]) for row in audited),
        "matches": audited,
    }


def prediction_condition_analysis(rows_: list[dict[str, Any]]) -> dict[str, Any]:
    """Read-only T-5 condition slices; one fixture-market observation each."""
    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    league_by_match: dict[str, str] = {}
    for row in rows_:
        match_id = str(row.get("match_id") or "")
        stage = str(row.get("stage") or "")
        if not match_id or stage not in STAGES:
            continue
        league_by_match[match_id] = str(row.get("league") or "未知聯賽")
        for grade in row.get("market_grades") or []:
            if not isinstance(grade, dict):
                continue
            code = str(grade.get("code") or "")
            if code in MARKETS:
                grouped.setdefault((match_id, code), {})[stage] = grade

    observations: list[dict[str, Any]] = []
    for (match_id, code), stage_grades in grouped.items():
        grade = stage_grades.get("T-5")
        if (
            not grade
            or grade.get("grade_status") != "GRADED"
            or grade.get("hit") is None
        ):
            continue
        all_stages = all(stage in stage_grades for stage in STAGES)
        sides = (
            {str(stage_grades[stage].get("side") or "") for stage in STAGES}
            if all_stages else set()
        )
        same_direction = all_stages and len(sides) == 1 and "" not in sides
        lines: list[float] = []
        if same_direction:
            for stage in STAGES:
                raw = stage_grades[stage].get("line")
                if raw is None:
                    raw = stage_grades[stage].get("condition")
                try:
                    lines.append(float(raw))
                except (TypeError, ValueError):
                    break
        same_line = (
            same_direction
            and len(lines) == len(STAGES)
            and len(set(lines)) == 1
        )
        try:
            probability = float(grade.get("probability"))
        except (TypeError, ValueError):
            probability = None
        if probability is None:
            confidence_band = "missing"
        elif probability < .55:
            confidence_band = "<55%"
        elif probability < .60:
            confidence_band = "55-60%"
        elif probability < .65:
            confidence_band = "60-65%"
        else:
            confidence_band = ">=65%"
        observations.append({
            "match_id": match_id,
            "market": code,
            "side": str(grade.get("side") or ""),
            "league": league_by_match.get(match_id, "未知聯賽"),
            "hit": grade.get("hit") is True,
            "confidence_band": confidence_band,
            "same_direction": same_direction,
            "same_line": same_line,
        })

    def aggregate(key_fn, *, minimum: int = 1) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for observation in observations:
            buckets.setdefault(str(key_fn(observation)), []).append(observation)
        output = []
        for key, bucket in buckets.items():
            if len(bucket) < minimum:
                continue
            hits = sum(row["hit"] for row in bucket)
            output.append({
                "condition": key,
                "decided": len(bucket),
                "hits": hits,
                "accuracy": round(hits / len(bucket), 6),
            })
        return sorted(
            output,
            key=lambda row: (-row["accuracy"], -row["decided"], row["condition"]),
        )

    return {
        "primary_unit": "one fixture-market at T-5",
        "minimum_reliable_sample": 30,
        "observations": len(observations),
        "by_market": aggregate(lambda row: row["market"]),
        "by_market_direction": aggregate(
            lambda row: f"{row['market']}|{row['side']}"
        ),
        "by_market_confidence": aggregate(
            lambda row: f"{row['market']}|{row['confidence_band']}"
        ),
        "by_market_stability": aggregate(
            lambda row: (
                f"{row['market']}|"
                + (
                    "same_direction_same_line"
                    if row["same_line"]
                    else "same_direction_line_moved"
                    if row["same_direction"]
                    else "changed_or_incomplete"
                )
            )
        ),
        "by_league_market_min_10": aggregate(
            lambda row: f"{row['market']}|{row['league']}",
            minimum=10,
        ),
    }


def main() -> None:
    footbreak = load(FOOTBREAK_DATA)
    crown_dashboard = load(CROWN_DATA)
    crown = load(CROWN_HISTORY)
    crown_ledger = load(CROWN_LEDGER)
    crown_rows = rows(crown)
    footbreak_rows = rows(footbreak.get("prediction_history") or {})
    cutoff = datetime.now(HKT) - timedelta(hours=4)

    def relevant(row: dict[str, Any]) -> bool:
        names = f"{row.get('home', '')} {row.get('away', '')}"
        if str(row.get("titan_match_id") or row.get("match_id") or "") == "3031468":
            return True
        if any(token in names for token in ("中央", "南市", "利昂女足", "堤格雷斯女足", "FC江原", "大阪飛腳")):
            return True
        try:
            kickoff = datetime.fromisoformat(
                str(row.get("kickoff") or "").replace("Z", "+00:00")
            )
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=HKT)
        except ValueError:
            return False
        return (
            kickoff < cutoff
            and row.get("result_status") not in {"已核對", "不計"}
        )

    audit = {
        "generated_at": datetime.now(HKT).isoformat(),
        "production_prediction_versions": {
            "prediction_era": PREDICTION_ERA,
            "matching_version": MATCHING_VERSION,
        },
        "server_timer": timer_state(),
        "challenger": challenger_state(),
        "data_health": data_health_state(),
        "historical_odds_recovery": odds_recovery_state(footbreak, crown),
        "pinnapi_source_health": pinnapi_source_health_state(),
        "crown": {
            "stats": crown.get("stats"),
            "result_sync": crown.get("result_sync"),
            "three_stage_consensus": calculate_three_stage_consensus(crown_rows),
            "condition_analysis": prediction_condition_analysis(crown_rows),
            "corner_prediction_audit": crown_corner_state(
                crown_dashboard,
                crown_ledger,
            ),
            "handicap_world_settlement": handicap_world_settlement_state(
                crown_ledger,
            ),
            "relevant_rows": [compact(row) for row in crown_rows if relevant(row)],
            "row_count": len(crown_rows),
        },
        "footbreak": {
            "stats": (footbreak.get("prediction_history") or {}).get("stats"),
            "three_stage_consensus": calculate_three_stage_consensus(footbreak_rows),
            "condition_analysis": prediction_condition_analysis(footbreak_rows),
            "row_count": len(footbreak_rows),
            "t5_hdc_rows": [
                compact(row)
                for row in footbreak_rows
                if row.get("stage") == "T-5"
                and any(
                    isinstance(grade, dict)
                    and grade.get("code") == "HDC"
                    and grade.get("grade_status") == "GRADED"
                    for grade in (row.get("market_grades") or [])
                )
            ],
        },
    }
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
