#!/usr/bin/env python3
"""Emit a compact, read-only production result-state audit as JSON."""

from __future__ import annotations

import json
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
HKT = timezone(timedelta(hours=8))
sys.path.insert(0, "/opt/footbreak")

from crown.ledger import PREDICTION_ERA, completed_stages  # noqa: E402
from crown.matching import MATCHING_VERSION  # noqa: E402


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
        "crown": {
            "stats": crown.get("stats"),
            "result_sync": crown.get("result_sync"),
            "corner_prediction_audit": crown_corner_state(
                crown_dashboard,
                crown_ledger,
            ),
            "relevant_rows": [compact(row) for row in crown_rows if relevant(row)],
            "row_count": len(crown_rows),
        },
        "footbreak": {
            "stats": (footbreak.get("prediction_history") or {}).get("stats"),
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
