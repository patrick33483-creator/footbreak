#!/usr/bin/env python3
"""Read-only, provider-free forensic for persisted Crown native T-5 evidence."""
from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from analysis.granular_conditions import MARKETS
from analysis.wilson_portfolio import _native_match_rows, _native_t5
from analysis.wilson_validation import (
    DECISION_STAGE, formal_registry_candidates, match_formal_registry,
    matching_admissions,
)
from analysis.granular_conditions import _descriptor, _paths, canonical_panels
from crown.common import HKT, iso_hkt
from crown.config import settings
from crown.hkjc_execution_test import NAMESPACE, _native_crown_signal, _number, _time
from crown.notify import _observation_group_key
from crown.state import paths


def _read(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return default
    return value if isinstance(value, type(default)) else default


def _iso(value: Any) -> datetime | None:
    return _time(value)


def _at_or_after(value: Any, since: datetime) -> bool:
    parsed = _iso(value)
    return parsed is not None and parsed >= since


def _systemctl() -> dict[str, str]:
    command = [
        "systemctl", "show", "crown-tick.timer", "crown-tick.service",
        "crown-sweep.timer", "crown-sweep.service", "footbreak-server-health-monitor.timer",
        "--no-pager", "-p", "Id", "-p", "ActiveState", "-p", "SubState", "-p", "Result",
        "-p", "LastTriggerUSec", "-p", "NextElapseUSecRealtime",
    ]
    run = subprocess.run(command, text=True, capture_output=True, check=False, timeout=15)
    return {"exit_code": str(run.returncode), "output": run.stdout[-12000:]}


def _journal(since: str) -> list[str]:
    command = [
        "journalctl", "-u", "crown-tick.service", "-u", "crown-sweep.service",
        "-u", "footbreak-server-health-monitor.service", "--since", since,
        "--no-pager", "-o", "short-iso",
    ]
    run = subprocess.run(command, text=True, capture_output=True, check=False, timeout=20)
    rows = run.stdout.splitlines()
    interesting = [row for row in rows if any(token in row.lower() for token in (
        "t-5", "wilson", "telegram", "notify", "outbox", "error", "exception", "fail",
    ))]
    return interesting[-160:]


def _fixture_name(watch: dict[str, Any]) -> str:
    return f"{watch.get('home') or ''} vs {watch.get('away') or ''}".strip()


def _frozen_axes(candidate: dict[str, Any]) -> dict[str, str] | None:
    """Mirror the formal matcher axes so a rejected path is explainable."""
    aliases = {
        "stage": "decision", "decision_stage": "decision",
        "observed_path": "path", "odds_tier": "tier",
        "line_bucket": "bucket", "tier_path": "tier_path",
        "odds_trajectory": "tier_path",
    }
    result: dict[str, str] = {}
    for raw in candidate.get("key") or []:
        if not isinstance(raw, str) or "=" not in raw:
            return None
        key, value = raw.split("=", 1)
        key, value = aliases.get(key.strip(), key.strip()), value.strip()
        if not key or not value or (key in result and result[key] != value):
            return None
        result[key] = value
    return result or None


def _descriptor_explanations(
    rows: list[dict[str, Any]], registry: list[dict[str, Any]], *, system: str,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Provide exact persisted axes and the nearest frozen mismatch per market."""
    frozen = [
        (candidate, axes)
        for candidate in registry
        if isinstance(candidate, dict) and (axes := _frozen_axes(candidate)) is not None
    ]
    output: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for panel in canonical_panels(rows, settled_only=False):
        fixture, market = str(panel.get("fixture") or ""), str(panel.get("market") or "")
        if not fixture or not market:
            continue
        entries: list[dict[str, Any]] = []
        for path in _paths(panel, DECISION_STAGE):
            if not path or path[-1].get("stage") != DECISION_STAGE:
                continue
            for level in range(4):
                descriptor, _label, _specificity = _descriptor(system, path, level)
                live = dict(piece.split("=", 1) for piece in descriptor)
                candidates = [
                    (candidate, axes)
                    for candidate, axes in frozen
                    if axes.get("market") == market
                ]
                closest = None
                if candidates:
                    ranked = sorted(
                        (
                            (
                                sum(live.get(axis) != value for axis, value in axes.items()),
                                str(candidate.get("__formal_frozen_signature") or ""),
                                candidate,
                                axes,
                            )
                            for candidate, axes in candidates
                        ),
                        key=lambda row: (row[0], row[1]),
                    )
                    distance, _signature, candidate, axes = ranked[0]
                    mismatch = {
                        axis: {"live": live.get(axis), "frozen": value}
                        for axis, value in axes.items()
                        if live.get(axis) != value
                    }
                    closest = {
                        "condition_number": candidate.get("__formal_condition_number"),
                        "signature": candidate.get("__formal_frozen_signature"),
                        "mismatch_count": distance,
                        "mismatch_axes": mismatch,
                    }
                entries.append({
                    "live_axes": live,
                    "nearest_frozen": closest,
                })
        output[(fixture, market)] = entries
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True, help="inclusive ISO-8601 HKT deployment boundary")
    args = parser.parse_args()
    since = _iso(args.since)
    if since is None:
        raise SystemExit("--since must be a valid ISO-8601 timestamp")

    config = settings()
    ledger_path = paths(config)["ledger"]
    notify_path = paths(config)["notify"]
    ledger = _read(ledger_path, {})
    state = _read(notify_path, {})
    watches = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    validation = ledger.get("wilson_validation") if isinstance(ledger.get("wilson_validation"), dict) else {}
    raw_conditions = validation.get("conditions") if isinstance(validation.get("conditions"), dict) else {}
    registry = formal_registry_candidates(ledger, "crown", now=iso_hkt())
    registry_sigs = {str(row.get("__formal_frozen_signature") or "") for row in registry}
    coverage = Counter(
        str((row.get("__formal_frozen_definition") or {}).get("market") or "")
        for row in registry
    )
    for row in registry:
        signature = str(row.get("__formal_frozen_signature") or "")
        row["__formal_condition_number"] = raw_conditions.get(signature, {}).get("condition_number")
    reciprocal = ledger.get(NAMESPACE) if isinstance(ledger.get(NAMESPACE), dict) else {}
    decisions = [row for row in reciprocal.get("decisions") or [] if isinstance(row, dict)]
    outboxes = [row for row in reciprocal.get("decision_outbox") or [] if isinstance(row, dict)]
    attempts = [row for row in reciprocal.get("counterpart_attempts") or [] if isinstance(row, dict)]
    audit_rows = [row for row in reciprocal.get("audit") or [] if isinstance(row, dict)]
    observations = [row for row in validation.get("observations") or [] if isinstance(row, dict)]
    sent_wilson = {str(value) for value in state.get("wilson_match_alerts") or []}
    sent_bilateral = {str(value) for value in state.get("bilateral_decision_alerts") or []}

    all_records: list[dict[str, Any]] = []
    all_native_rows: list[dict[str, Any]] = []
    native_t5_count = 0
    for fixture, watch in sorted(watches.items()):
        if not isinstance(watch, dict):
            continue
        stages = [stage for stage in watch.get("stages") or []
                  if isinstance(stage, dict) and stage.get("stage") == DECISION_STAGE]
        for stage in stages:
            stage_at = _iso(stage.get("ts") or stage.get("source_snapshot_at"))
            if stage_at is None or stage_at < since:
                continue
            native = _native_t5(watch, stage, _time)
            if native:
                native_t5_count += 1
            # Formal identity is the immutable 首預→T-30→T-5 native sequence,
            # including its tier trajectory.  Auditing a mutable or T-5-only
            # projection would falsely report every multi-stage condition as
            # unmatched.
            stage_rows = _native_match_rows(watch, _time)
            all_native_rows.extend(stage_rows)
            matched = match_formal_registry(
                stage_rows, registry, system="crown", decision_stage=DECISION_STAGE,
            ).get(str(fixture), []) if native and registry else []
            for market in MARKETS:
                signal, signal_reason = (None, "not_native_t5")
                if native and stage_at is not None:
                    signal, signal_reason = _native_crown_signal(stage, market, watch, stage_at)
                admissions: list[dict[str, Any]] = []
                admission_reason = None
                if signal is not None:
                    admissions, admission_reason = matching_admissions(
                        "crown", market, signal, matched, stage_at=str(stage.get("ts") or ""),
                    )
                row_observations = [row for row in observations if (
                    str(row.get("match_id") or "") == str(fixture)
                    and str(row.get("market") or row.get("code") or "") == market
                    and str(row.get("stage") or "") == DECISION_STAGE
                    and str(row.get("created_at") or "") == stage_at.isoformat()
                )]
                row_decisions = [row for row in decisions if (
                    str(row.get("fixture") or "") == str(fixture)
                    and str(row.get("market") or "") == market
                    and str(row.get("stage_at") or "") == stage_at.isoformat()
                )]
                decision_ids = {str(row.get("decision_id") or "") for row in row_decisions}
                row_outbox = [row for row in outboxes if str(row.get("decision_id") or "") in decision_ids]
                row_attempts = [row for row in attempts if (
                    str(row.get("match_id") or "") == str(fixture)
                    and str(row.get("market") or "") == market
                    and str(row.get("stage_at") or "") == stage_at.isoformat()
                )]
                row_audit = [row for row in audit_rows if (
                    str(row.get("match_id") or "") == str(fixture)
                    and str(row.get("market") or "") == market
                    and _at_or_after(row.get("ts"), since)
                )][-12:]
                obs_ids = [str(row.get("observation_id") or "") for row in row_observations]
                eligible_obs = [row for row in row_observations if (
                    str(row.get("observation_id") or "") not in sent_wilson
                    and _observation_group_key(row) is not None
                )]
                all_records.append({
                    "fixture": str(fixture), "fixture_name": _fixture_name(watch),
                    "hkjc_match_id": watch.get("hkjc_match_id"), "kickoff": watch.get("kickoff"),
                    "stage_at": stage_at.isoformat(), "market": market,
                    "native_first_pre_kickoff_t5": native,
                    "signal": ({key: signal.get(key) for key in (
                        "code", "side", "line", "condition", "odds", "quote_source", "source", "observed_at",
                    )} if signal else None),
                    "signal_reason": signal_reason,
                    "structural_registry_matches": [{
                        "signature": row.get("__formal_frozen_signature"),
                        "condition_number": raw_conditions.get(str(row.get("__formal_frozen_signature") or ""), {}).get("condition_number"),
                        "definition": row.get("__formal_frozen_definition"),
                    } for row in matched if str(row.get("market") or "") == market],
                    "admission": {
                        "outcome": admission_reason,
                        "count": len(admissions),
                        "conditions": [{
                            "signature": row.get("signature"),
                            "condition_number": raw_conditions.get(str(row.get("signature") or ""), {}).get("condition_number"),
                            "passes_native_price": (row.get("arithmetic") or {}).get("passes"),
                            "minimum_odds": (row.get("arithmetic") or {}).get("minimum_acceptable_odds_raw"),
                        } for row in admissions],
                    },
                    "formal_observations": [{
                        "observation_id": row.get("observation_id"), "status": row.get("status"),
                        "bet_status": row.get("bet_status"), "odds": row.get("odds"),
                        "condition_number": row.get("condition_number"),
                        "acknowledged": str(row.get("observation_id") or "") in sent_wilson,
                    } for row in row_observations],
                    "wilson_outbox": {
                        "eligible_unacknowledged_observations": [str(row.get("observation_id") or "") for row in eligible_obs],
                        "acknowledged_observations": [identity for identity in obs_ids if identity in sent_wilson],
                    },
                    "counterpart_attempts": row_attempts,
                    "bilateral_decisions": row_decisions,
                    "bilateral_outbox": [{
                        **row, "acknowledged": str(row.get("decision_id") or "") in sent_bilateral,
                    } for row in row_outbox],
                    "persisted_audit": row_audit,
                })

    explanations = _descriptor_explanations(all_native_rows, registry, system="crown")
    for row in all_records:
        key = (str(row["fixture"]), str(row["market"]))
        row["persisted_path_descriptors"] = explanations.get(key, [])
        if not row["structural_registry_matches"] and row["signal"] is not None:
            descriptors = row["persisted_path_descriptors"]
            nearest = [
                descriptor.get("nearest_frozen") for descriptor in descriptors
                if isinstance(descriptor.get("nearest_frozen"), dict)
            ]
            if nearest:
                nearest.sort(key=lambda value: (
                    int(value.get("mismatch_count") or 999),
                    str(value.get("signature") or ""),
                ))
                row["no_match_reason"] = {
                    "code": "frozen_axes_mismatch",
                    "nearest_frozen": nearest[0],
                }
            else:
                row["no_match_reason"] = {
                    "code": "no_frozen_formal_condition_for_market",
                    "market": row["market"],
                }
        elif row["structural_registry_matches"]:
            row["no_match_reason"] = None
        else:
            row["no_match_reason"] = {
                "code": row["signal_reason"] or "native_signal_unavailable",
            }

    wrong_rejections = [row for row in all_records if (
        row["native_first_pre_kickoff_t5"] and row["signal"] is not None
        and row["structural_registry_matches"] and row["admission"]["count"] == 0
    )]
    eligible_observation_count = sum(len(row["wilson_outbox"]["eligible_unacknowledged_observations"]) for row in all_records)
    pending_bilateral = sum(1 for row in outboxes if row.get("notification_required") and str(row.get("decision_id") or "") not in sent_bilateral)
    payload = {
        "schema_version": 1, "mode": "read_only_provider_free", "provider_calls": 0,
        "writes": 0, "telegram_send_attempts": 0, "generated_at": iso_hkt(),
        "since": since.isoformat(), "ledger_path": str(ledger_path),
        "registry": {
            "raw_condition_count": len(raw_conditions), "loaded_formal_count": len(registry),
            "invalid_or_nonformal_count": len(raw_conditions) - len(registry_sigs),
            "coverage_by_market": dict(sorted(coverage.items())),
            "expected_markets": list(MARKETS),
            "missing_expected_markets": [market for market in MARKETS if not coverage.get(market)],
        },
        "summary": {
            "native_t5_stage_count": native_t5_count, "fixture_market_rows": len(all_records),
            "structural_matcher_rejections": len(wrong_rejections),
            "formal_observation_rows": sum(len(row["formal_observations"]) for row in all_records),
            "eligible_unacknowledged_wilson_observations": eligible_observation_count,
            "pending_unacknowledged_bilateral_outbox": pending_bilateral,
            "transport": {
                "telegram_enabled": config.telegram_enabled,
                "bot_token_configured": bool(config.telegram_bot_token),
                "chat_id_configured": bool(config.telegram_chat_id),
                "notify_state_updated_at": state.get("updated_at"),
                "wilson_ack_count": len(sent_wilson), "bilateral_ack_count": len(sent_bilateral),
            },
        },
        "records": all_records,
        "structural_matcher_rejections": wrong_rejections,
        "runner": {"systemctl": _systemctl(), "journal_interesting": _journal(args.since)},
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
