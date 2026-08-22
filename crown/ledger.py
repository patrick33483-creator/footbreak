"""Idempotent Crown watch ledger.  It can create simulations, never real bets."""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from analysis.learning_store import LearningStore

from .common import HKT, iso_hkt, parse_time, read_json
from .config import Settings
from .condition_portfolio import FIXED_STAKE, STARTING_BANKROLL, STRATEGY, evaluate_new_t5
from . import challenger_v2
from .direct_t5_outbox import (
    ensure_namespace as ensure_direct_t5_outbox,
    record_new_native_t5,
)
from analysis.wilson_validation import (
    active_observations, ensure_namespace, portfolio_name, recompute_namespace, all_settleable_bets,
)

STAGES = {"首預": 1, "T-30": 2, "T-5": 3}
SCHEDULED_STAGES = {"T-30": 30, "T-5": 5}
# This deliberately is not part of ``STAGES``.  It is a post-hoc audit record,
# never a schedulable or genuine T-5 prediction stage.
RECOVERED_T5_STAGE = "T-5（事後回補）"
PREDICTION_ERA = "2026-08-12-hkjc-corner-forecast-v4"
PREDICTION_SCHEMA_VERSION = 2


def ensure_stage_jobs(watch: dict[str, Any], kickoff_value: Any) -> dict[str, dict[str, Any]]:
    """Materialize durable UTC deadline jobs without fabricating a stage.

    These jobs are scheduler metadata, not predictions.  They may be
    reconstructed from a known, future fixture after a deploy/restart, but a
    completed or failed prediction is only ever written by ``sync_prediction``.
    """
    kickoff = parse_time(kickoff_value)
    jobs = watch.get("stage_jobs")
    if not isinstance(jobs, dict):
        jobs = {}
        watch["stage_jobs"] = jobs
    if kickoff is None:
        return jobs
    kickoff_utc = kickoff.astimezone(timezone.utc)
    watch["kickoff"] = iso_hkt(kickoff)
    watch["kickoff_hkt"] = iso_hkt(kickoff)
    watch["kickoff_utc"] = kickoff_utc.isoformat()
    for stage, minutes_before in SCHEDULED_STAGES.items():
        due_at = kickoff_utc - timedelta(minutes=minutes_before)
        existing = jobs.get(stage)
        if isinstance(existing, dict) and existing.get("due_at_utc") == due_at.isoformat():
            continue
        # A changed authoritative kickoff supersedes only an uncommitted
        # scheduler record.  It never rewrites a persisted stage snapshot.
        if isinstance(existing, dict) and existing.get("state") == "COMMITTED":
            continue
        existing_stage = next((
            row for row in (watch.get("stages") or [])
            if isinstance(row, dict) and row.get("stage") == stage
        ), None)
        completed = _completed_stage_row(existing_stage)
        jobs[stage] = {
            "stage": stage,
            "due_at_utc": due_at.isoformat(),
            "due_at_hkt": iso_hkt(due_at),
            "kickoff_utc": kickoff_utc.isoformat(),
            "state": "COMMITTED" if completed else "PENDING",
            "retry_count": int(existing.get("retry_count") or 0) if isinstance(existing, dict) else 0,
            "reconstructed": not isinstance(existing, dict),
        }
    return jobs


def due_stage_jobs(
    watch: dict[str, Any], now: datetime,
) -> list[str]:
    """Return pending scheduled jobs whose full legal window is still open."""
    kickoff = parse_time(watch.get("kickoff_utc") or watch.get("kickoff_hkt") or watch.get("kickoff"))
    if kickoff is None or now.astimezone(timezone.utc) >= kickoff.astimezone(timezone.utc):
        return []
    jobs = watch.get("stage_jobs")
    if not isinstance(jobs, dict):
        return []
    current = now.astimezone(timezone.utc)
    due: list[str] = []
    for stage in ("T-30", "T-5"):
        job = jobs.get(stage)
        due_at = parse_time(job.get("due_at_utc")) if isinstance(job, dict) else None
        if (
            due_at is not None
            and current >= due_at.astimezone(timezone.utc)
            and str(job.get("state") or "") != "COMMITTED"
        ):
            due.append(stage)
    return due


def completed_stages(
    watch: dict[str, Any],
    matching_version: str,
    prediction_era: str | None = None,
) -> set[str]:
    """Refresh stale first looks once without replaying T-30/T-5 decisions."""
    done = {
        str(row.get("stage"))
        for row in watch.get("stages", [])
        if isinstance(row, dict)
        # A provider/mapping outage is not a completed prediction.  Keep the
        # stage eligible for a later recovery pass; sync_prediction updates
        # the same stage row idempotently and still cannot duplicate a bet.
        if (
            row.get("stage")
            and row.get("status") != "DATA_MISSING"
            # Schema-v2 rows explicitly record selected-odds availability.
            # A fixture-level baseline forecast with no priced market is only
            # an attempt, not a completed 首預/T-30/T-5 market prediction.
            # Keep legacy rows (which have no odds_status field) compatible,
            # but retry current rows until an auditable pre-kickoff market
            # quote is persisted.
            and (
                row.get("odds_status") is None
                or (
                    row.get("odds_status") == "available"
                    and bool(row.get("market_predictions"))
                )
            )
        )
    }
    if (
        (
            watch.get("matching_version") != matching_version
            or (
                prediction_era is not None
                and watch.get("prediction_era") != prediction_era
            )
        )
        and not done.intersection({"T-30", "T-5"})
    ):
        done.discard("首預")
    return done


def stage_for(minutes_to_kickoff: float, sweep: bool, done: set[str]) -> str | None:
    due = stages_due(minutes_to_kickoff, sweep, done)
    return due[0] if due else None


def stages_due(minutes_to_kickoff: float, sweep: bool, done: set[str]) -> list[str]:
    """Return every legitimate native stage that is still due before kickoff.

    A missing first look must be visible, but it must not consume the only
    T-5 opportunity.  The caller persists each returned stage independently;
    it never synthesizes a stage after kickoff.
    """
    if sweep:
        return ["首預"] if "首預" not in done else []
    # A known card can reach a timed window when a prior board sweep failed,
    # was delayed, or discovered it just after that sweep started.  Do not let
    # a later T-30/T-5 snapshot become the first persisted decision: it makes
    # the three-stage history incomplete and the dashboard misleadingly says
    # it is merely waiting for T-30.  The tick has enough local identity to
    # recover 首預 without another fixture-list request; the next tick can
    # process the still-due timed stage.
    output: list[str] = []
    # Once a fixture is known locally, a missing first-look is still a
    # legitimate native repair at any point before kickoff.  This intentionally
    # makes the normal native tick (rather than an optional discovery/counterpart
    # path) responsible for recording its explicit attempt or snapshot.
    if 0 < minutes_to_kickoff and "首預" not in done:
        output.append("首預")
    if 20 <= minutes_to_kickoff <= 40 and "T-30" not in done:
        output.append("T-30")
    if 0 < minutes_to_kickoff <= 10 and "T-5" not in done:
        output.append("T-5")
    return output


def _completed_stage_row(row: dict[str, Any] | None) -> bool:
    """Whether a saved row is a usable native prediction, not an attempt."""
    if not isinstance(row, dict) or row.get("status") == "DATA_MISSING":
        return False
    return (
        row.get("odds_status") is None
        or (
            row.get("odds_status") == "available"
            and bool(row.get("market_predictions"))
        )
    )


def _observed_before_kickoff(value: Any, kickoff: Any) -> bool:
    """Accept only a finite quote observation that predates this fixture."""
    kickoff_at = parse_time(str(kickoff or ""))
    if kickoff_at is None:
        return False
    try:
        observed_number = float(value)
    except (TypeError, ValueError):
        observed_at = parse_time(str(value or ""))
    else:
        if not math.isfinite(observed_number) or observed_number <= 0:
            return False
        # Provider rows normally use epoch seconds; tolerate milliseconds only
        # for an otherwise valid immutable observation.
        if observed_number >= 10_000_000_000:
            observed_number /= 1000
        try:
            observed_at = datetime.fromtimestamp(observed_number, HKT)
        except (OverflowError, OSError, ValueError):
            return False
    return observed_at is not None and observed_at < kickoff_at


def _market_predictions(
    candidates: list[dict[str, Any]],
    kickoff: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Persist only auditable pre-kickoff selected market observations.

    Fixture-level forecasts remain available in the stage metadata.  A
    scoreable market row, however, must never be created without an exact
    line/side, decimal odds, and an observation that predates kickoff.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates or []:
        code = str(candidate.get("code") or "")
        if code in {"HDC", "HIL", "CHL"}:
            grouped.setdefault(code, []).append(candidate)
    output: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for code, rows in grouped.items():
        best = max(rows, key=lambda row: float(row.get("prob") or 0))
        odds = best.get("odds")
        try:
            odds_valid = float(odds) > 1.0
        except (TypeError, ValueError):
            odds_valid = False
        observed_at = best.get("observed_at") or best.get("source_at")
        raw_line = best.get("line")
        try:
            line_valid = math.isfinite(float(raw_line))
        except (TypeError, ValueError):
            line_valid = False
        side_valid = best.get("side") in {
            "HDC": {"H", "A"},
            "HIL": {"H", "L"},
            "CHL": {"H", "L"},
        }[code]
        timestamp_valid = _observed_before_kickoff(observed_at, kickoff)
        if not (odds_valid and line_valid and side_valid and timestamp_valid):
            reason = (
                "selected_quote_unavailable"
                if not odds_valid
                else "selected_line_or_side_unavailable"
                if not (line_valid and side_valid)
                else "selected_quote_not_observed_pre_kickoff"
            )
            rejected.append({
                "code": code,
                "line": raw_line,
                "side": best.get("side"),
                "reason": reason,
            })
            continue
        output.append({
            "code": code,
            "market": best.get("market"),
            "condition": best.get("condition"),
            "line": best.get("line"),
            "side": best.get("side"),
            "label": best.get("label"),
            "odds": odds,
            "odds_status": "available",
            "odds_reason": None,
            # Crown price rows carry source_at as epoch seconds.  Preserve it
            # exactly; a missing observed timestamp remains explicit.
            "observed_at": observed_at,
            "probability": best.get("prob"),
            # A probability is usable by a strategy only when its immutable
            # source is retained.  In particular, Crown's own no-vig market
            # probability must never be reused as Kelly p against its price.
            "probability_source": best.get("reference"),
            "probability_observed_at": best.get("probability_observed_at"),
            # Keep the historical learning-source field backward compatible;
            # quote_source is the provider evidence for the selected price.
            # A missing provenance must remain missing.  In particular, do not
            # synthesize ``pinnapi_exact_line`` here: independent validation
            # may only admit a selection with an actually persisted source
            # observation, and the downstream source-evidence gate fails
            # closed when this field is empty.
            "source": best.get("reference") or best.get("source"),
            "quote_source": best.get("source") or best.get("reference"),
            "provider": best.get("provider") or "Crown",
            "quote_status": best.get("quote_status"),
            "quote_fallback_source": best.get("quote_fallback_source"),
        })
    return sorted(output, key=lambda row: row["code"]), rejected


def _selected_odds_journal(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "code": row.get("code"), "line": row.get("line", row.get("condition")),
        "side": row.get("side"), "odds": row.get("odds"),
        "odds_status": row.get("odds_status"), "reason": row.get("odds_reason"),
        "source": row.get("quote_source") or row.get("source"),
        "provider": row.get("provider"),
        "observed_at": row.get("observed_at"),
        "quote_status": row.get("quote_status"),
        "quote_fallback_source": row.get("quote_fallback_source"),
    } for row in rows]


_EXECUTION_MARKET_SIDES = {
    "HDC": ("H", "A"),
    "HIL": ("H", "L"),
    "CHL": ("H", "L"),
}


def _execution_quote_board(prediction: dict[str, Any], stage: str) -> dict[str, Any]:
    """Return the compact native Crown board needed by the Footbreak sidecar.

    This deliberately reads only the already assembled ``book_odds.crown``
    snapshot.  It does not inspect a raw provider response, call a provider, or
    derive an opposing price from the selected prediction.  The board is
    attached to the immutable native stage before that stage is durably saved.
    """
    raw_rows = ((prediction.get("book_odds") or {}).get("crown") or [])
    quote_source = str(prediction.get("crown_quote_source") or "").strip()
    observed_at = prediction.get("source_snapshot_at")
    quotes: list[dict[str, Any]] = []
    seen: dict[tuple[str, float, str], dict[str, Any]] = {}
    market_lines: dict[str, set[float]] = {
        market: set() for market in _EXECUTION_MARKET_SIDES
    }
    invalid_rows = 0
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(raw, dict):
            invalid_rows += 1
            continue
        code = str(raw.get("market") or raw.get("code") or "").upper()
        side = str(raw.get("selection") or raw.get("side") or "").upper()
        line = raw.get("line")
        odds = raw.get("odds")
        try:
            line_value = float(line)
        except (TypeError, ValueError):
            invalid_rows += 1
            continue
        if (
            code not in _EXECUTION_MARKET_SIDES
            or side not in _EXECUTION_MARKET_SIDES[code]
            or not math.isfinite(line_value)
        ):
            invalid_rows += 1
            continue
        market_lines[code].add(line_value)
        try:
            odds_value = float(odds)
        except (TypeError, ValueError):
            odds_value = None
        row_observed_at = raw.get("source_at") or raw.get("observed_at") or observed_at
        status = "AVAILABLE"
        reason = None
        if odds_value is None or not math.isfinite(odds_value) or odds_value <= 1.0:
            status, reason, odds_value = (
                "UNAVAILABLE", "crown_native_quote_invalid", None,
            )
        elif not row_observed_at:
            status, reason = "UNAVAILABLE", "crown_native_quote_timestamp_unavailable"
        item = {
            "code": code,
            "line": line_value,
            "side": side,
            "status": status,
            "reason": reason,
            "odds": odds_value,
            # ID 3 is the normalized venue identity.  The original compact
            # collector label remains available separately for provenance.
            "source": "titan007-crown-id-3",
            "native_source": quote_source or None,
            "observed_at": row_observed_at,
        }
        key = (code, line_value, side)
        # An internally duplicated exact row is retained as explicit
        # ambiguity rather than picking a price silently.
        if key in seen:
            seen[key]["status"] = "UNAVAILABLE"
            seen[key]["reason"] = "crown_native_quote_duplicate"
            item["status"] = "UNAVAILABLE"
            item["reason"] = "crown_native_quote_duplicate"
            item["odds"] = None
        seen[key] = item
    for code, lines in market_lines.items():
        for line_value in sorted(lines):
            for side in _EXECUTION_MARKET_SIDES[code]:
                key = (code, line_value, side)
                if key not in seen:
                    seen[key] = {
                        "code": code,
                        "line": line_value,
                        "side": side,
                        "status": "UNAVAILABLE",
                        "reason": "crown_native_quote_side_unavailable",
                        "odds": None,
                        "source": "titan007-crown-id-3",
                        "native_source": quote_source or None,
                        "observed_at": observed_at,
                    }
    for key in sorted(seen):
        quotes.append(seen[key])
    market_status = {
        code: (
            {"status": "AVAILABLE", "reason": None}
            if market_lines[code] else
            {"status": "UNAVAILABLE", "reason": "crown_native_market_unavailable"}
        )
        for code in ("HDC", "HIL")
    }
    if market_lines["CHL"]:
        market_status["CHL"] = {"status": "AVAILABLE", "reason": None}
    return {
        "schema_version": 1,
        "stage": stage,
        "native_observed_at": observed_at,
        "native_source": quote_source or None,
        "coverage": (
            "native_full_board"
            if isinstance(raw_rows, list) and raw_rows
            else "native_board_unavailable"
        ),
        "invalid_row_count": min(invalid_rows, 100),
        "market_status": market_status,
        "quotes": quotes[:240],
    }


def _snapshot(prediction: dict[str, Any], stage: str) -> dict[str, Any]:
    market_predictions, market_prediction_rejections = _market_predictions(
        prediction.get("forecast_candidates") or prediction.get("candidates") or [],
        prediction.get("kickoff_hkt"),
    )
    odds_journal = _selected_odds_journal(market_predictions)
    unavailable_quotes = [
        row for row in odds_journal
        if row.get("odds_status") != "available"
    ]
    all_selected_quotes_available = bool(odds_journal) and not unavailable_quotes
    snapshot = {key: prediction.get(key) for key in (
        "match_id", "league", "home", "away", "kickoff_hkt", "mins_to_ko", "status", "verdict",
        "conviction", "no_bet_reason", "pick", "lead_view", "market_sources", "hkjc_match_id",
        "titan_match_id", "pinnapi_event_id", "source_snapshot_at", "execution",
        "outcome", "forecast", "probability", "likely_score", "prediction_source",
        "probabilities", "baseline_low_confidence", "edge_reference_status", "edge_reference_note",
        "sharp_reference_available", "source_status", "pinnapi_source_at",
        "pinnapi_timestamp_inferred", "pinnapi_timestamp_basis",
        "pinnapi_corner_event_id", "pinnapi_corner_source_at", "pinnapi_corner_timestamp_inferred",
        "matching_version", "crown_quote_cached_forecast_only", "crown_cached_source_at",
        "crown_quote_source", "crown_quote_status", "crown_cached_t5_fallback",
        "collection_attempt",
    )} | {
        "prediction_era": PREDICTION_ERA,
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "stage": stage,
        "ts": iso_hkt(),
        "market_predictions": market_predictions,
        # This is non-market metadata, so a missing quote remains auditable
        # without becoming a scoreable history row.  Historic rows are not
        # rewritten by this gate or by the recovery-overlay workflow.
        "market_prediction_rejections": market_prediction_rejections,
        "selected_odds_journal": odds_journal,
        # A partial journal must not make the complete stage appear priced.
        # Each market still retains its own explicit missing reason below.
        "odds_status": "available" if all_selected_quotes_available else "missing",
        "odds_reason": (
            None if all_selected_quotes_available
            else (
                "one_or_more_selected_quotes_unavailable"
                if odds_journal else "no_selected_market_quote"
            )
        ),
        # This is a bounded, normalized board from the exact native stage
        # snapshot, not a later dashboard refresh or a raw provider payload.
        "native_execution_quote_board": _execution_quote_board(prediction, stage),
    }
    # Persist only compact, verifiable provider provenance for the immutable
    # source-health report.  It distinguishes an unmatched PinnAPI fixture
    # from a matched-but-unavailable quote without making PinnAPI a prerequisite
    # for Crown's pure forecast path.
    if not snapshot.get("source_status"):
        if snapshot.get("sharp_reference_available") is True:
            snapshot["source_status"] = "pinnapi_live"
        elif not snapshot.get("pinnapi_event_id"):
            snapshot["source_status"] = "pinnapi_fixture_unmatched"
        elif snapshot.get("edge_reference_status") == "unavailable":
            snapshot["source_status"] = "pinnapi_live_unavailable"
        else:
            snapshot["source_status"] = "pinnapi_status_unobserved"
    return snapshot


def _bet_id(match_id: str, market: str) -> str:
    return f"{match_id}|{market}|T-5|{STRATEGY}"


def condition_bets(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only rows belonging to the active fixed-stake portfolio.

    Old ledger rows remain readable until the explicit reset is performed, but
    must not appear in the new portfolio's statistics or settlement queue.
    """
    return all_settleable_bets(ledger, "crown")

def condition_observations(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """Formal native T-5 no-bet rows are validation evidence, not simulations."""
    return active_observations(ledger, "crown")


def _record_learning_snapshot(
    prediction: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any] | None:
    path = os.environ.get("LEARNING_DB_PATH")
    if not path:
        return None
    payload = {key: value for key, value in snapshot.items() if key != "ts"}
    with LearningStore(path) as store:
        return store.record_snapshot(
            "crown",
            str(prediction["match_id"]),
            str(snapshot["stage"]),
            snapshot["ts"],
            str(prediction["kickoff_hkt"]),
            payload,
            model_version=PREDICTION_ERA,
            schema_version=str(PREDICTION_SCHEMA_VERSION),
        )


def sync_prediction(ledger: dict[str, Any], prediction: dict[str, Any], config: Settings) -> list[str]:
    """Persist a stage and create only newly-observed eligible T-5 condition bets."""
    ledger.setdefault("bets", [])
    ledger.setdefault("watch", {})
    # The cutover only appends this namespace.  Legacy bet/stat rows remain
    # untouched and cannot enter the active validation flow.
    ensure_namespace(ledger, "crown")
    # Direct notifications have their own prospective-only durable namespace.
    # It is intentionally initialized before a new snapshot gets its
    # timestamp, so the deployment activation boundary is unambiguous.
    ensure_direct_t5_outbox(ledger)
    # v2 is a separate research namespace. Its creation and recomputation do
    # not touch v1 bets, conditions, stats, or dedupe keys.
    challenger_v2.ensure_namespace(ledger)
    stage = prediction.get("stage")
    if stage not in STAGES:
        return []
    match_id = str(prediction["match_id"])
    watch = ledger["watch"].setdefault(match_id, {
        "match_id": match_id, "league": prediction.get("league"), "home": prediction.get("home"),
        "away": prediction.get("away"), "kickoff": prediction.get("kickoff_hkt"),
        "titan_match_id": prediction.get("titan_match_id"),
        "native_fixture_id": prediction.get("titan_match_id") or match_id,
        "pinnapi_event_id": prediction.get("pinnapi_event_id"),
        "hkjc_match_id": prediction.get("hkjc_match_id"), "stages": [],
        "matching_version": prediction.get("matching_version"), "prediction_era": PREDICTION_ERA,
        "discovered_at": prediction.get("discovered_at") or iso_hkt(),
    })
    # First look locks the native Crown identity.  Later T-30/T-5 updates are
    # observations of that exact provider fixture, not an opportunity to
    # resolve another match from changing team names or a broad board.
    native_fixture_id = str(
        watch.get("native_fixture_id") or watch.get("titan_match_id")
        or prediction.get("titan_match_id") or match_id
    )
    watch.setdefault("native_fixture_id", native_fixture_id)
    if not watch.get("titan_match_id"):
        watch["titan_match_id"] = native_fixture_id
    for key in ("league", "home", "away", "kickoff_hkt", "pinnapi_event_id", "hkjc_match_id", "matching_version"):
        if key not in watch or watch.get(key) in {None, ""}:
            watch[key] = prediction.get(key)
    # Existing operational watch shells created by a write-ahead attempt or a
    # prior deployment may contain only ``stages``.  Restore the immutable
    # fixture identifier before T-5 admission; otherwise a real native T-5
    # appears anonymous and is silently rejected despite valid quote evidence.
    watch["match_id"] = match_id
    watch["prediction_era"] = PREDICTION_ERA
    watch.setdefault("kickoff", prediction.get("kickoff_hkt"))
    ensure_stage_jobs(watch, watch.get("kickoff_hkt") or watch.get("kickoff"))
    if not watch.get("discovered_at"):
        watch["discovered_at"] = prediction.get("discovered_at") or iso_hkt()
    stage_rows = watch.get("stages")
    if not isinstance(stage_rows, list):
        stage_rows = []
        watch["stages"] = stage_rows
    existing = next((
        row for row in stage_rows
        if isinstance(row, dict) and row.get("stage") == stage
    ), None)
    existing_was_completed = _completed_stage_row(existing)
    snapshot = _snapshot(prediction, stage)
    attempt = snapshot.pop("collection_attempt", None)
    if isinstance(attempt, dict):
        prior_attempts = (
            list(existing.get("collection_attempts") or [])
            if isinstance(existing, dict) else []
        )
        snapshot["collection_attempts"] = (prior_attempts + [attempt])[-8:]
    learning = _record_learning_snapshot(prediction, snapshot)
    if learning:
        snapshot.update({
            "learning_snapshot_id": learning["snapshot_id"], "learning_attempt": learning["attempt"],
            "learning_pre_kickoff": learning["pre_kickoff"], "learning_payload_sha256": learning["payload_sha256"],
        })
        if not learning["pre_kickoff"]:
            return []
    if existing is None:
        stage_rows.append(snapshot)
        # Older persisted watch data can contain an audit/legacy dict without
        # a stage. Scheduler reads already tolerate that shape; keep it
        # visible but place it after recognized stages instead of allowing a
        # single malformed historic row to abort every due T-5 commit.
        stage_rows.sort(key=lambda row: STAGES.get(
            str(row.get("stage")) if isinstance(row, dict) else "",
            len(STAGES) + 1,
        ))
    else:
        # A DATA_MISSING row is a write-ahead attempt, not a completed market
        # decision.  A later legitimate pre-kickoff retry therefore receives
        # its own immutable completion time and may perform the one native T-5
        # admission.  A completed stage, by contrast, cannot be repriced or
        # admitted again.
        original_ts = existing.get("ts") if existing_was_completed else None
        if not existing.get("stage_started_at"):
            existing["stage_started_at"] = existing.get("ts")
        existing.update(snapshot)
        if original_ts:
            existing["ts"] = original_ts
    stored = next((
        row for row in stage_rows
        if isinstance(row, dict) and row.get("stage") == stage
    ), None)
    if isinstance(stored, dict):
        stored.setdefault("stage_started_at", stored.get("ts"))
        attempts = watch.setdefault("stage_attempts", {})
        record: dict[str, Any] = {}
        if isinstance(attempts, dict):
            prior = attempts.get(stage)
            record = prior if isinstance(prior, dict) else {}
            record.update({
                "stage": stage,
                "state": "COMMITTED" if _completed_stage_row(stored) else "FAILED",
                "updated_at": stored.get("ts"),
                "reason": (
                    None if _completed_stage_row(stored)
                    else str((attempt or {}).get("reason") or stored.get("odds_reason")
                             or stored.get("status") or "native_quote_unavailable")
                ),
                "source": (
                    str((attempt or {}).get("source") or stored.get("crown_quote_source")
                        or "titan007-crown-id-3")
                ),
            })
            attempts[stage] = record
        job = (watch.get("stage_jobs") or {}).get(stage)
        if isinstance(job, dict):
            job.update({
                "state": "COMMITTED" if _completed_stage_row(stored) else "FAILED",
                "updated_at": stored.get("ts"),
                "reason": None if _completed_stage_row(stored) else record.get("reason"),
            })
    # Validation admission is singular: only the very first persisted native
    # pre-kickoff T-5 may create a bet.  A later quote refresh can enrich the
    # prediction record but is a replay, never a second admission chance.
    if (
        stage != "T-5"
        or not _completed_stage_row(stored)
        or existing_was_completed
    ):
        challenger_v2.recompute(ledger[challenger_v2.NAMESPACE], ledger)
        return []
    history = read_json(config.state_dir / "prediction_history.json", {})
    cached_ranking = (
        ((history.get("stats") or {}).get("granular_conditions") or {}).get("ranking")
        if isinstance(history, dict) else None
    )
    created, audit = evaluate_new_t5(
        ledger, watch, config,
        ranking=cached_ranking if isinstance(cached_ranking, list) else None,
    )
    snapshot["wilson_validation"] = {"strategy": STRATEGY, "stage": "T-5", "audit": audit}
    # ``evaluate_new_t5`` persists the admission audit itself.  Keeping the
    # returned list in the snapshot is useful for this run, without duplicating
    # durable audit records.
    ledger["bets"].extend(created)
    observation_rows = list(
        ((ledger.get("wilson_validation") or {}).get("observations") or [])
    )
    # The legacy native direct policy was broader than formal Wilson matching
    # but still exact: HDC must agree across 首預/T-30/T-5.  Record that
    # notification opportunity independently of whether formal admission
    # created a paper bet or a low-odds observation.
    record_new_native_t5(
        ledger,
        watch,
        snapshot,
        formal_rows=list(created) + observation_rows,
    )
    # The v2 challenger sees the same newly persisted native T-5 but records
    # only isolated research rows. It does not append to ``ledger['bets']``.
    challenger_v2.evaluate_new_t5(ledger, watch, snapshot)
    challenger_v2.recompute(ledger[challenger_v2.NAMESPACE], ledger)
    if created:
        ledger.setdefault("log", []).extend([
            {"ts": bet["created_at"], "action": "Wilson 模擬注建立", "bet_id": bet["bet_id"],
             "match_id": match_id, "market": bet["market_label"], "condition": bet.get("frozen_condition_definition", {}).get("path", "凍結條件")}
            for bet in created
        ])
    return [str(bet["bet_id"]) for bet in created]

def _portfolio_stats(bets: list[dict[str, Any]], bankroll: float) -> dict[str, Any]:
    settled = [bet for bet in bets if bet.get("status") == "SETTLED"]
    pending = [bet for bet in bets if bet.get("status") == "PENDING"]
    pnl = round(sum(float(bet.get("pnl") or 0) for bet in settled), 2)
    turnover = round(sum(float(bet.get("stake") or 0) for bet in settled), 2)
    decided = [bet for bet in settled if bet.get("result") != "Refunded"]
    hits = sum(bet.get("result") in {"Won", "Half Won"} for bet in decided)
    by_market: dict[str, dict[str, Any]] = {}
    for bet in settled:
        market = str(bet.get("market") or bet.get("code") or "其他")
        row = by_market.setdefault(
            market, {"n": 0, "stake": 0.0, "pnl": 0.0, "hit": 0, "dec": 0}
        )
        row["n"] += 1
        row["stake"] += float(bet.get("stake") or 0)
        row["pnl"] += float(bet.get("pnl") or 0)
        if bet.get("result") != "Refunded":
            row["dec"] += 1
            row["hit"] += int(bet.get("result") in {"Won", "Half Won"})
    for row in by_market.values():
        row["stake"] = round(row["stake"], 2)
        row["pnl"] = round(row["pnl"], 2)
        row["roi"] = round(row["pnl"] / row["stake"], 4) if row["stake"] else None
        row["hit_rate"] = round(row["hit"] / row["dec"], 4) if row["dec"] else None

    result_names = ("Won", "Half Won", "Refunded", "Half Lost", "Lost")
    res_counts = {name: sum(bet.get("result") == name for bet in settled) for name in result_names}
    running_equity = bankroll
    curve = []
    for bet in sorted(settled, key=lambda row: str(row.get("settled_at") or row.get("created_at") or "")):
        bet_pnl = float(bet.get("pnl") or 0)
        running_equity += bet_pnl
        curve.append({
            "ts": bet.get("settled_at") or bet.get("created_at"),
            "label": f"{bet.get('home', '')} v {bet.get('away', '')}".strip(),
            "pnl": round(bet_pnl, 2),
            "equity": round(running_equity, 2),
        })
    peak = bankroll
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    for point in curve:
        equity = float(point["equity"])
        peak = max(peak, equity)
        drawdown = max(0.0, peak - equity)
        max_drawdown = max(max_drawdown, drawdown)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, drawdown / peak)

    return {
        "n_pending": len(pending), "n_voided": sum(bet.get("status") == "VOIDED" for bet in bets), "n_settled": len(settled),
        "open_stake": round(sum(float(bet.get("stake") or 0) for bet in pending), 2),
        "open_pct": round(sum(float(bet.get("stake") or 0) for bet in pending) / bankroll, 4) if bankroll else 0,
        "pnl": pnl, "turnover": turnover, "roi": round(pnl / turnover, 4) if turnover else None,
        "n_decided": len(decided), "hits": hits, "hit_rate": round(hits / len(decided), 4) if decided else None,
        "equity": round(bankroll + pnl, 2), "by_market": by_market, "curve": curve,
        "res_counts": res_counts, "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 6),
    }


def recompute_stats(ledger: dict[str, Any], config: Settings) -> dict[str, Any]:
    """Compute statistics for the sole fixed-stake condition portfolio."""
    del config
    ledger.setdefault("bets", [])
    base = recompute_namespace(ledger, "crown")
    challenger = ledger.get(challenger_v2.NAMESPACE)
    if isinstance(challenger, dict):
        challenger_v2.recompute(challenger, ledger)
    base["entry_rule"] = (
        "首次持久化原生賽前 T-5；凍結歷史條件 decided >=50；"
        "Wilson 95% 下限 ≥ 實際賠率損益平衡率 +3pp；HK$500 每注、每場最多三市場／HK$1,500"
    )
    ledger["stats"] = base
    # Retired keys are tolerated if read from old state but are never created,
    # displayed, settled, or included in statistics.
    return base

def market_entry_thresholds(
    ledger: dict[str, Any],
    code: str,
    config: Settings,
    *,
    min_samples: int = 30,
) -> dict[str, Any]:
    """Return a conservative, performance-driven threshold for one market.

    Small samples never change policy.  Once the market has enough settled
    observations, poor ROI or probability calibration can only tighten entry;
    this function never loosens the configured production floor.
    """
    settled = [
        bet for bet in condition_bets(ledger)
        if bet.get("status") == "SETTLED" and str(bet.get("code") or "") == code
    ]
    decided = [bet for bet in settled if bet.get("result") != "Refunded"]
    stake = sum(float(bet.get("stake") or 0) for bet in settled)
    profit = sum(float(bet.get("pnl") or 0) for bet in settled)
    roi = profit / stake if stake else None
    hit_gap = None
    comparable = [
        bet for bet in decided
        if bet.get("model_prob", bet.get("prob")) is not None
    ]
    if comparable:
        hit = sum(
            1.0 if bet.get("result") == "Won"
            else 0.5 if bet.get("result") in {"Half Won", "Half Lost"}
            else 0.0
            for bet in comparable
        )
        predicted = sum(
            float(bet.get("model_prob", bet.get("prob")))
            for bet in comparable
        )
        hit_gap = hit / len(comparable) - predicted / len(comparable)

    edge_add = 0.0
    confidence_add = 0.0
    reason = "insufficient_market_sample"
    if len(settled) >= min_samples:
        reason = "market_performance_stable"
        if (roi is not None and roi <= -0.10) or (
            hit_gap is not None and hit_gap <= -0.12
        ):
            edge_add, confidence_add = 0.02, 4.0
            reason = "severe_market_underperformance"
        elif (roi is not None and roi < 0) or (
            hit_gap is not None and hit_gap < -0.08
        ):
            edge_add, confidence_add = 0.01, 2.0
            reason = "market_underperformance"

    return {
        "code": code,
        "n_settled": len(settled),
        "min_samples": min_samples,
        "roi": round(roi, 6) if roi is not None else None,
        "hit_gap": round(hit_gap, 6) if hit_gap is not None else None,
        "base_edge": config.min_edge,
        "base_confidence": config.confidence_floor,
        "min_edge": round(config.min_edge + edge_add, 6),
        "confidence_floor": round(config.confidence_floor + confidence_add, 1),
        "reason": reason,
    }
