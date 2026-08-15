"""Guarded, offline-only recovery for missed Crown T-5 prediction records.

This module never calls a provider, sends a notification, creates a simulation
bet, writes learning data, or settles a fixture.  It can only append a clearly
labelled audit stage when all identity, model-selection, and quote-evidence
gates pass.  A dry run is the default; production apply requires the exact
confirmation phrase and creates a durable, fsynced backup before each atomic
state-file replacement.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .common import HKT, iso_hkt, write_json_atomic
from .config import Settings, settings
from .ledger import RECOVERED_T5_STAGE
from .state import load_ledger, paths, save_ledger, state_lock

APPLY_CONFIRMATION = "APPLY_CROWN_T5_RECOVERY"
CROWN_COMPANY_ID = "3"
SUPPORTED_MARKETS = {"HDC": {"H", "A"}, "HIL": {"H", "L"}}
T5_TARGET_OFFSET = timedelta(minutes=5)
T5_EXACT_WINDOW = timedelta(seconds=60)


def _strict_time(value: Any) -> datetime | None:
    """Parse only explicit, offset-aware timestamps (or finite epoch values)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)) or float(value) <= 0:
            return None
        seconds = float(value) / 1000 if float(value) >= 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).astimezone(HKT)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(HKT) if parsed.tzinfo is not None else None


def _number(value: Any, *, odds: bool = False) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or (odds and parsed <= 1.0):
        return None
    return parsed


def _stage_time(stage: dict[str, Any]) -> datetime | None:
    return _strict_time(stage.get("ts") or stage.get("source_snapshot_at"))


def _candidate_rows(stage: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("market_predictions", "forecast_candidates", "candidates"):
        for row in stage.get(key) or []:
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _candidate_key(candidate: dict[str, Any]) -> tuple[str, str, str] | None:
    code = str(candidate.get("code") or candidate.get("market") or "")
    side = str(candidate.get("side") or candidate.get("selection") or "")
    line = _number(candidate.get("line", candidate.get("condition")))
    if code not in SUPPORTED_MARKETS or side not in SUPPORTED_MARKETS[code] or line is None:
        return None
    # Exact numeric equivalence only; this is not a fuzzy/mapped market match.
    return code, format(line, ".12g"), side


def _is_crown_company_three(candidate: dict[str, Any]) -> bool:
    source = str(candidate.get("quote_source") or candidate.get("source") or "")
    provider = str(candidate.get("provider") or candidate.get("bookmaker") or "")
    company = str(candidate.get("company_id") or candidate.get("provider_company_id") or "")
    return (
        source == "titan007-crown-id-3"
        or (provider == "Crown" and company == CROWN_COMPANY_ID)
    )


def _valid_quote(candidate: dict[str, Any], kickoff: datetime) -> tuple[datetime, float] | None:
    if _candidate_key(candidate) is None or not _is_crown_company_three(candidate):
        return None
    observed = _strict_time(candidate.get("observed_at") or candidate.get("source_at"))
    odds = _number(candidate.get("odds"), odds=True)
    if observed is None or odds is None or observed >= kickoff:
        return None
    return observed, odds


def _strict_identity(watch: dict[str, Any], stages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    match_id = str(watch.get("match_id") or "").strip()
    titan_id = str(watch.get("titan_match_id") or "").strip()
    kickoff = _strict_time(watch.get("kickoff_hkt") or watch.get("kickoff"))
    if not match_id or not titan_id or kickoff is None:
        return None, "missing_strict_fixture_identity"
    if not str(watch.get("home") or "").strip() or not str(watch.get("away") or "").strip():
        return None, "missing_strict_fixture_identity"
    # A non-empty stage-level Titan ID must agree exactly with the immutable
    # watch identity.  Other populated identity fields must agree exactly too.
    # Never repair, aliases, or display-text match any identity component.
    for stage in stages:
        stage_id = stage.get("titan_match_id")
        if stage_id is not None and str(stage_id).strip() and str(stage_id).strip() != titan_id:
            return None, "fixture_identity_mismatch"
        for field, expected in (
            ("match_id", match_id),
            ("home", str(watch.get("home") or "").strip()),
            ("away", str(watch.get("away") or "").strip()),
        ):
            actual = stage.get(field)
            if actual is not None and str(actual).strip() and str(actual).strip() != expected:
                return None, "fixture_identity_mismatch"
        stage_kickoff = stage.get("kickoff_hkt") or stage.get("kickoff")
        if stage_kickoff is not None:
            parsed = _strict_time(stage_kickoff)
            if parsed is None or parsed != kickoff:
                return None, "fixture_identity_mismatch"
    return {"match_id": match_id, "titan_match_id": titan_id, "kickoff": kickoff}, None


def _native_t5_exists(stages: list[dict[str, Any]]) -> bool:
    return any(
        str(stage.get("stage") or "") == "T-5" and not stage.get("post_hoc_backfill")
        for stage in stages
    )


def _already_recovered(stages: list[dict[str, Any]]) -> bool:
    return any(str(stage.get("stage") or "") == RECOVERED_T5_STAGE for stage in stages)


def _source_stage(stages: list[dict[str, Any]], kickoff: datetime) -> tuple[dict[str, Any] | None, str | None]:
    valid: list[tuple[datetime, dict[str, Any]]] = []
    saw_prior = False
    for stage in stages:
        if not isinstance(stage, dict) or str(stage.get("stage") or "") in {"T-5", RECOVERED_T5_STAGE}:
            continue
        at = _stage_time(stage)
        if at is None or at >= kickoff:
            continue
        saw_prior = True
        if any(_candidate_key(row) is not None and _is_crown_company_three(row) for row in _candidate_rows(stage)):
            valid.append((at, stage))
    if not valid:
        return None, "no_valid_saved_model_payload" if saw_prior else "no_valid_pre_kickoff_saved_stage"
    return max(valid, key=lambda pair: pair[0])[1], None


def _quotes(stages: list[dict[str, Any]], kickoff: datetime) -> dict[tuple[str, str, str], list[tuple[datetime, float]]]:
    output: dict[tuple[str, str, str], list[tuple[datetime, float]]] = defaultdict(list)
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        # No quote from a post-kickoff saved payload can enter the pool even if
        # its embedded timestamp is malformed or looks pre-match.
        saved_at = _stage_time(stage)
        if saved_at is None or saved_at >= kickoff:
            continue
        for candidate in _candidate_rows(stage):
            key = _candidate_key(candidate)
            quote = _valid_quote(candidate, kickoff)
            if key is not None and quote is not None:
                output[key].append(quote)
    return output


def _select_evidence(
    key: tuple[str, str, str], quote_pool: dict[tuple[str, str, str], list[tuple[datetime, float]]], kickoff: datetime,
) -> tuple[dict[str, Any] | None, str | None]:
    quotes = quote_pool.get(key, [])
    if not quotes:
        return None, "no_valid_pre_kickoff_crown_quote"
    target = kickoff - T5_TARGET_OFFSET
    prior = [quote for quote in quotes if quote[0] <= target]
    if prior:
        observed, odds = max(prior, key=lambda quote: quote[0])
        evidence_type = "t5_exact" if abs(observed - target) <= T5_EXACT_WINDOW else "t5_locf"
    else:
        observed, odds = max(quotes, key=lambda quote: quote[0])
        evidence_type = "closing_substitution"
    return {
        "odds": odds,
        "observed_at": observed.isoformat(),
        "evidence_type": evidence_type,
        "closing_odds_substitution": evidence_type == "closing_substitution",
    }, None


def _recovered_stage(
    watch: dict[str, Any], source: dict[str, Any], identity: dict[str, Any],
    quote_pool: dict[tuple[str, str, str], list[tuple[datetime, float]]], recovered_at: str,
) -> tuple[dict[str, Any] | None, Counter]:
    kickoff = identity["kickoff"]
    selected: dict[tuple[str, str, str], dict[str, Any]] = {}
    reasons: Counter = Counter()
    for candidate in _candidate_rows(source):
        key = _candidate_key(candidate)
        if key is None or not _is_crown_company_three(candidate):
            continue
        # Preserve the latest exact selection for a code/line/side; the model
        # payload is copied rather than recomputed or made up.
        selected[key] = candidate
    recovered_markets: list[dict[str, Any]] = []
    evidence_types: list[str] = []
    for key, model in sorted(selected.items()):
        evidence, reason = _select_evidence(key, quote_pool, kickoff)
        if evidence is None:
            reasons[reason or "quote_selection_failed"] += 1
            continue
        market = copy.deepcopy(model)
        market.update({
            "code": key[0], "line": _number(model.get("line", model.get("condition"))), "side": key[2],
            "odds": evidence["odds"], "observed_at": evidence["observed_at"],
            "odds_status": "available", "odds_reason": None,
            "provider": "Crown", "quote_source": "titan007-crown-id-3",
            "recovery_evidence_type": evidence["evidence_type"],
            "closing_odds_substitution": evidence["closing_odds_substitution"],
        })
        recovered_markets.append(market)
        evidence_types.append(evidence["evidence_type"])
    if not recovered_markets:
        return None, reasons or Counter({"no_recoverable_market_selection": 1})
    source_at = _stage_time(source)
    source_name = str(source.get("stage") or "unknown")
    return {
        "match_id": identity["match_id"],
        "league": source.get("league") or watch.get("league"), "home": source.get("home") or watch.get("home"),
        "away": source.get("away") or watch.get("away"),
        "kickoff_hkt": (source.get("kickoff_hkt") or watch.get("kickoff_hkt") or watch.get("kickoff")),
        "titan_match_id": identity["titan_match_id"],
        "hkjc_match_id": source.get("hkjc_match_id") or watch.get("hkjc_match_id"),
        "pinnapi_event_id": source.get("pinnapi_event_id") or watch.get("pinnapi_event_id"),
        "stage": RECOVERED_T5_STAGE,
        "status": "RECOVERED_AUDIT_ONLY", "verdict": "事後回補稽核紀錄（不可作真實 T-5）",
        "ts": recovered_at, "source_snapshot_at": source_at.isoformat() if source_at else None,
        "outcome": copy.deepcopy(source.get("outcome")), "forecast": source.get("forecast"),
        "probability": source.get("probability"), "likely_score": source.get("likely_score"),
        "prediction_source": source.get("prediction_source"), "conviction": source.get("conviction"),
        "market_predictions": recovered_markets, "odds_status": "available", "odds_reason": None,
        "execution": {"enabled": False, "mode": "recovered_audit_only", "real_betting_enabled": False},
        "post_hoc_backfill": True, "exclude_from_telegram": True,
        "exclude_from_simulation": True, "exclude_from_learning": True,
        "exclude_from_primary_statistics": True,
        "recovery": {
            "version": 1, "source_stage": source_name,
            "source_model_timestamp": source_at.isoformat() if source_at else None,
            "recovered_at": recovered_at, "provider_company_id": CROWN_COMPANY_ID,
            "evidence_types": sorted(set(evidence_types)),
            "closing_odds_substitution": "closing_substitution" in evidence_types,
            "label": "POST-HOC / BACKFILLED — NOT A NATIVE T-5 PREDICTION",
        },
        "no_bet_reason": "事後回補紀錄；禁止 Telegram、模擬注、學習及主要命中率／排名統計。",
    }, reasons


def _aggregate(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter = Counter()
    for item in items:
        kind = item["kind"]
        if kind == "recovered":
            stage = item["source_stage"]
            kickoff = item["kickoff_day"]
            for market, evidence in item["markets"]:
                counts[("recovered", kickoff, stage, market, evidence)] += 1
        else:
            counts[(kind, item.get("kickoff_day", "unknown"), item.get("source_stage", "unknown"), "none", item["reason"])] += 1
    rendered: dict[str, Any] = {"by_kickoff_stage_market_evidence": {}, "unresolved_reasons": {}}
    for (kind, kickoff, stage, market, evidence), count in sorted(counts.items()):
        if kind == "recovered":
            rendered["by_kickoff_stage_market_evidence"].setdefault(kickoff, {}).setdefault(stage, {}).setdefault(market, {})[evidence] = count
        elif kind == "unresolved":
            rendered["unresolved_reasons"][evidence] = rendered["unresolved_reasons"].get(evidence, 0) + count
        else:
            rendered.setdefault("skipped", {})[evidence] = rendered.setdefault("skipped", {}).get(evidence, 0) + count
    rendered["total_recovered_records"] = sum(1 for item in items if item["kind"] == "recovered")
    rendered["total_unresolved_records"] = sum(1 for item in items if item["kind"] == "unresolved")
    rendered["total_skipped_records"] = sum(1 for item in items if item["kind"] == "skipped")
    return rendered


def build_plan(ledger: dict[str, Any], *, recovered_at: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return non-identifying planned ledger appends and an aggregate-only audit."""
    recovered_at = recovered_at or iso_hkt()
    planned: list[dict[str, Any]] = []
    audit_items: list[dict[str, Any]] = []
    watches = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
    for watch in watches.values():
        if not isinstance(watch, dict):
            audit_items.append({"kind": "unresolved", "kickoff_day": "unknown", "source_stage": "unknown", "reason": "malformed_watch"})
            continue
        stages = [stage for stage in watch.get("stages") or [] if isinstance(stage, dict)]
        identity, reason = _strict_identity(watch, stages)
        kickoff_day = identity["kickoff"].date().isoformat() if identity else "unknown"
        if identity is None:
            audit_items.append({"kind": "unresolved", "kickoff_day": kickoff_day, "source_stage": "unknown", "reason": reason or "invalid_identity"})
            continue
        if _native_t5_exists(stages):
            audit_items.append({"kind": "skipped", "kickoff_day": kickoff_day, "source_stage": "T-5", "reason": "native_t5_already_exists"})
            continue
        if _already_recovered(stages):
            audit_items.append({"kind": "skipped", "kickoff_day": kickoff_day, "source_stage": RECOVERED_T5_STAGE, "reason": "recovery_already_exists"})
            continue
        source, reason = _source_stage(stages, identity["kickoff"])
        if source is None:
            audit_items.append({"kind": "unresolved", "kickoff_day": kickoff_day, "source_stage": "unknown", "reason": reason or "missing_source_stage"})
            continue
        stage, rejected = _recovered_stage(watch, source, identity, _quotes(stages, identity["kickoff"]), recovered_at)
        source_name = str(source.get("stage") or "unknown")
        if stage is None:
            for unresolved_reason, amount in rejected.items():
                for _ in range(amount):
                    audit_items.append({"kind": "unresolved", "kickoff_day": kickoff_day, "source_stage": source_name, "reason": unresolved_reason})
            continue
        planned.append({"match_id": identity["match_id"], "stage": stage})
        audit_items.append({
            "kind": "recovered", "kickoff_day": kickoff_day, "source_stage": source_name,
            "markets": [(str(row["code"]), str(row["recovery_evidence_type"])) for row in stage["market_predictions"]],
        })
        for unresolved_reason, amount in rejected.items():
            for _ in range(amount):
                audit_items.append({"kind": "unresolved", "kickoff_day": kickoff_day, "source_stage": source_name, "reason": unresolved_reason})
    return planned, {"schema_version": 1, "mode": "dry_run", "aggregate": _aggregate(audit_items)}


def _backup(config: Settings, ledger_path: Path, history_path: Path) -> Path:
    backup_root = config.state_dir / "t5-recovery-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_root, 0o700)
    run = Path(tempfile.mkdtemp(prefix="t5-recovery-", dir=backup_root))
    manifest: dict[str, Any] = {"schema_version": 1, "files": {}}
    for name, source in (("ledger.json", ledger_path), ("prediction_history.json", history_path)):
        raw = source.read_bytes() if source.exists() else b""
        destination = run / name
        with destination.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(destination, 0o600)
        manifest["files"][name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    manifest_path = run / "manifest.json"
    with manifest_path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(manifest_path, 0o600)
    directory_fd = os.open(run, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    os.chmod(run, 0o700)
    root_fd = os.open(backup_root, os.O_RDONLY)
    try:
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return run


def apply_plan(config: Settings, planned: list[dict[str, Any]]) -> dict[str, int]:
    """Append recovery audit stages under the state lock; never touch bets/results."""
    with state_lock(config):
        ledger = load_ledger(config)
        # The dry-run plan is intentionally not trusted as an apply payload.
        # Rebuild it under the lock from the just-read state, so a concurrent
        # native T-5 or a changed fixture identity wins and blocks recovery.
        planned, _ = build_plan(ledger)
        if not planned:
            existing = sum(
                any(
                    isinstance(stage, dict)
                    and str(stage.get("stage") or "") == RECOVERED_T5_STAGE
                    for stage in (watch.get("stages") or [])
                )
                for watch in (ledger.get("watch") or {}).values()
                if isinstance(watch, dict)
            )
            return {"added": 0, "already_present": existing}
        watch = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
        added = already_present = 0
        for item in planned:
            match_id, stage = str(item["match_id"]), item["stage"]
            target = watch.get(match_id)
            if not isinstance(target, dict):
                continue
            stages = target.setdefault("stages", [])
            if any(str(row.get("stage") or "") == "T-5" for row in stages if isinstance(row, dict)):
                already_present += 1
                continue
            if any(str(row.get("stage") or "") == RECOVERED_T5_STAGE for row in stages if isinstance(row, dict)):
                already_present += 1
                continue
            stages.append(copy.deepcopy(stage))
            added += 1
        if not added:
            return {"added": 0, "already_present": already_present}
        ledger_path = paths(config)["ledger"]
        history_path = config.state_dir / "prediction_history.json"
        _backup(config, ledger_path, history_path)
        # The history row is appended directly rather than calling the normal
        # history updater: that updater includes result synchronization, which
        # must never run as part of recovery.  Both replacements are atomic
        # per file, protected by the same state lock, and preceded by one
        # durable backup set.
        from .prediction_history import _history_row, normalize_history
        if history_path.exists():
            try:
                history = json.loads(history_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("malformed_prediction_history_refusing_recovery") from exc
        else:
            history = {"rows": [], "stats": {}}
        if not isinstance(history, dict):
            history = {"rows": [], "stats": {}}
        rows = history.setdefault("rows", [])
        if not isinstance(rows, list):
            rows = history["rows"] = []
        known = {str(row.get("history_key") or "") for row in rows if isinstance(row, dict)}
        for item in planned:
            target = watch.get(str(item["match_id"]))
            if not isinstance(target, dict):
                continue
            stage = next((
                row for row in target.get("stages") or []
                if isinstance(row, dict) and str(row.get("stage") or "") == RECOVERED_T5_STAGE
            ), None)
            if stage is None:
                continue
            row = _history_row(target, stage)
            if row["history_key"] not in known:
                rows.append(row)
                known.add(row["history_key"])
        normalize_history(history)
        save_ledger(config, ledger)
        write_json_atomic(history_path, history)
        return {"added": added, "already_present": already_present}


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline guarded Crown missed-T-5 recovery")
    parser.add_argument("--dry-run", action="store_true", help="required audit mode; never writes")
    parser.add_argument("--apply", action="store_true", help="append guarded audit stages")
    parser.add_argument("--apply-confirmation", default="", help=f"must equal {APPLY_CONFIRMATION}")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--provider-company-id", default=CROWN_COMPANY_ID)
    args = parser.parse_args()
    if args.dry_run == args.apply:
        parser.error("choose exactly one of --dry-run or --apply")
    if str(args.provider_company_id) != CROWN_COMPANY_ID:
        parser.error("only provider company ID 3 is permitted")
    if args.apply and args.apply_confirmation != APPLY_CONFIRMATION:
        parser.error("production apply requires the exact confirmation phrase")
    config = settings()
    if args.state_dir is not None:
        config = Settings(**{**config.__dict__, "state_dir": args.state_dir})
    ledger = load_ledger(config)
    planned, audit = build_plan(ledger)
    if args.dry_run:
        print(json.dumps(audit, ensure_ascii=False, sort_keys=True))
        return
    result = apply_plan(config, planned)
    audit["mode"] = "applied"
    audit["apply"] = result
    print(json.dumps(audit, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
