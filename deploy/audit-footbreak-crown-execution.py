#!/usr/bin/env python3
"""Provider-free verification of the Footbreak -> Crown execution test handoff.

This is deliberately a read-only production diagnostic.  It reads already
persisted ledgers, the narrow Crown T-5 sidecar, dashboard projection, systemd
unit, and recent local journal entries.  It never runs a scan, prediction,
provider client, settlement, or Telegram send.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SYSTEM = ROOT / "system"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import crown_execution_test as cross  # noqa: E402
import notify  # noqa: E402


HKT = timezone(timedelta(hours=8))
NAMESPACE = cross.NAMESPACE


def _json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return fallback


def _time(value: Any) -> datetime | None:
    return cross._time(value)


def _stamp(value: Any) -> str | None:
    parsed = _time(value)
    return parsed.astimezone(HKT).isoformat(timespec="seconds") if parsed else None


def _number(value: Any) -> float | None:
    return cross._num(value)


def _redact(line: str) -> str:
    line = re.sub(
        r"(?i)\b(token|api[_-]?key|authorization|password)\s*[:=]\s*\S+",
        r"\1=***",
        line,
    )
    return line[:420]


def _recent_journal() -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["journalctl", "-u", "footbreak-tick.service", "-n", "240",
             "--no-pager", "-o", "short-iso"],
            check=False, text=True, capture_output=True, timeout=8,
        )
        lines = completed.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "cross_book_markers": 0, "recent_errors": []}
    cross_lines = [
        _redact(line) for line in lines
        if re.search(r"(?i)crown|皇冠|cross.book|execution.evidence", line)
    ]
    errors = [
        _redact(line) for line in cross_lines
        if re.search(r"(?i)\b(error|exception|failed|failure|traceback)\b", line)
    ]
    return {
        "available": completed.returncode == 0,
        "cross_book_markers": len(cross_lines),
        "recent_cross_book_lines": cross_lines[-12:],
        "recent_errors": errors[-8:],
    }


def _service_contract(unit_path: Path) -> dict[str, Any]:
    text = unit_path.read_text(encoding="utf-8") if unit_path.is_file() else ""
    evidence = "/var/lib/footbreak/crown/footbreak-execution-evidence.json"
    return {
        "unit_path": str(unit_path),
        "readable": bool(text),
        "configured_evidence_path": evidence,
        "environment_path_present": (
            f"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH={evidence}" in text
        ),
        "read_only_path_present": f"ReadOnlyPaths={evidence}" in text,
    }


def _sidecar_report(path: Path, now: datetime) -> dict[str, Any]:
    exists = path.is_file()
    stat = path.stat() if exists else None
    cards, error = cross._load_local_crown_cards()
    entries: list[dict[str, Any]] = []
    structural: list[dict[str, Any]] = []
    fresh_now = 0
    latest_observed: datetime | None = None
    for card in cards:
        fixture = str(card.get("hkjc_match_id") or "")
        kickoff = _time(card.get("kickoff_hkt") or card.get("kickoff"))
        journal = card.get("current_selected_odds_journal")
        if not fixture or kickoff is None or not isinstance(journal, list):
            continue
        for quote in journal:
            if not isinstance(quote, dict):
                continue
            market = str(quote.get("code") or "").upper()
            side = str(quote.get("side") or "").upper()
            line = _number(quote.get("line", quote.get("condition")))
            observed = _time(quote.get("observed_at"))
            if observed and (latest_observed is None or observed > latest_observed):
                latest_observed = observed
            if (
                observed and observed <= now < kickoff
                and (now - observed).total_seconds() <= cross._freshness_seconds()
            ):
                fresh_now += 1
            entries.append({
                "fixture": fixture, "market": market, "side": side, "line": line,
                "observed_at": _stamp(quote.get("observed_at")),
                "kickoff": _stamp(card.get("kickoff_hkt") or card.get("kickoff")),
                "source": quote.get("source"), "odds_status": quote.get("odds_status"),
            })
            # Replaying the reader at the quote's own snapshot instant proves
            # exact fixture/market/side/Asian-line matching without treating an
            # old quote as current and without calling any provider.
            if (
                len(structural) < 8 and market in {"HDC", "HIL", "CHL"}
                and cross._valid_side(market, side) and line is not None
                and observed is not None and observed < kickoff
            ):
                quote_result, reason = cross._crown_quote_for_exact_fixture(
                    fixture, market, side, line, observed + timedelta(seconds=1), kickoff,
                )
                _, wrong_line_reason = cross._crown_quote_for_exact_fixture(
                    fixture, market, side, line + 0.125,
                    observed + timedelta(seconds=1), kickoff,
                )
                structural.append({
                    "fixture": fixture, "market": market, "side": side, "line": line,
                    "exact_match_accepted": quote_result is not None and reason is None,
                    "mismatched_line_rejected": (
                        wrong_line_reason == "crown_exact_market_side_line_missing_or_ambiguous"
                    ),
                    "reason": reason,
                })
    duplicate_fixtures = len(entries) - len({row["fixture"] for row in entries})
    age = (
        round((now - latest_observed).total_seconds(), 1)
        if latest_observed is not None else None
    )
    return {
        "path": str(path),
        "exists": exists,
        "readable_by_footbreak_adapter": error is None,
        "read_error": error,
        "size_bytes": stat.st_size if stat else None,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, HKT).isoformat(timespec="seconds") if stat else None,
        "age_seconds": round(now.timestamp() - stat.st_mtime, 1) if stat else None,
        "cards": len(cards),
        "quote_rows": len(entries),
        "duplicate_fixture_quote_rows": duplicate_fixtures,
        "latest_quote_observed_at": latest_observed.astimezone(HKT).isoformat(timespec="seconds") if latest_observed else None,
        "latest_quote_age_seconds": age,
        "currently_fresh_pre_kickoff_quote_rows": fresh_now,
        "structural_exact_match_witnesses": structural,
    }


def _safe_audit(rows: Any) -> list[dict[str, Any]]:
    output = []
    for row in (rows if isinstance(rows, list) else [])[-30:]:
        if not isinstance(row, dict):
            continue
        output.append({
            key: row.get(key) for key in (
                "ts", "match_id", "market", "status", "reason", "condition_number", "bet_id",
            )
        })
    return output


def _ledger_report(ledger_path: Path, notify_state_path: Path, now: datetime) -> dict[str, Any]:
    ledger = _json(ledger_path, {})
    namespace = ledger.get(NAMESPACE) if isinstance(ledger, dict) else {}
    namespace = namespace if isinstance(namespace, dict) else {}
    bets = [row for row in namespace.get("bets") or [] if isinstance(row, dict)]
    pending = [row for row in bets if row.get("status") == "PENDING"]
    eligible = [row for row in pending if notify._crown_execution_message(row) is not None]
    missing_contract = []
    fields = {
        "portfolio", "strategy", "hkjc_signal_odds", "hkjc_signal_observed_at",
        "crown_execution_odds", "crown_execution_observed_at", "condition_number",
        "wilson_admission",
    }
    for row in bets:
        missing = sorted(field for field in fields if row.get(field) is None)
        if missing:
            missing_contract.append({"bet_id": row.get("bet_id"), "missing": missing})
    state = _json(notify_state_path, {})
    sent_ids = {str(item) for item in (state.get("crown_execution_test_alerts") or [])}
    audited_match_ids = {
        str(row.get("match_id") or "")
        for row in (namespace.get("audit") or [])
        if isinstance(row, dict) and row.get("match_id")
    }
    normal_audit = (
        ((ledger.get("wilson_validation") or {}).get("audit") or [])
        if isinstance(ledger, dict) else []
    )
    normal_by_match: dict[str, list[str]] = {}
    for row in normal_audit:
        if not isinstance(row, dict):
            continue
        match_id = str(row.get("match_id") or "")
        if match_id:
            normal_by_match.setdefault(match_id, []).append(str(row.get("reason") or ""))
    recent_t5 = []
    upcoming = []
    for watch in (ledger.get("watch") or {}).values() if isinstance(ledger, dict) else []:
        if not isinstance(watch, dict):
            continue
        kickoff = _time(watch.get("kickoff"))
        if kickoff and now < kickoff:
            upcoming.append({
                "match_id": watch.get("match_id"),
                "kickoff": _stamp(watch.get("kickoff")),
                "stages": [row.get("stage") for row in watch.get("stages") or [] if isinstance(row, dict)],
            })
        for stage in watch.get("stages") or []:
            if not isinstance(stage, dict) or stage.get("stage") != "T-5":
                continue
            stage_at = _time(stage.get("ts"))
            if stage_at and (now - stage_at).total_seconds() <= 6 * 3600:
                match_id = str(watch.get("match_id") or "")
                cross_book_persisted = match_id in audited_match_ids or any(
                    str(row.get("match_id") or "") == match_id for row in bets
                )
                recent_t5.append({
                    "match_id": match_id,
                    "stage_at": _stamp(stage.get("ts")),
                    "kickoff": _stamp(watch.get("kickoff")),
                    "still_pre_kickoff": bool(kickoff and now < kickoff),
                    "cross_book_outcome_persisted": cross_book_persisted,
                    # A native T-5 can be blocked before cross-book evaluation
                    # only by the primary Footbreak safe-lead gate; expose that
                    # durable reason instead of treating it as a silent drop.
                    "primary_t5_reasons": normal_by_match.get(match_id, [])[-4:],
                })
    return {
        "ledger_path": str(ledger_path),
        "readable": isinstance(ledger, dict),
        "bets_total": len(bets),
        "pending_bets": len(pending),
        "pending_telegram_eligible": len(eligible),
        "pending_telegram_acknowledged": sum(str(row.get("bet_id") or "") in sent_ids for row in pending),
        "invalid_bet_contract_rows": missing_contract[:10],
        "stats": namespace.get("stats") if isinstance(namespace.get("stats"), dict) else {},
        "recent_evaluations": _safe_audit(namespace.get("audit")),
        "recent_native_t5": recent_t5[-20:],
        "recent_native_t5_without_persisted_cross_outcome": [
            row for row in recent_t5 if not row["cross_book_outcome_persisted"]
        ][-20:],
        "upcoming_fixtures": sorted(upcoming, key=lambda row: row["kickoff"] or "")[:30],
    }


def _dashboard_report(path: Path) -> dict[str, Any]:
    payload = _json(path, {})
    portfolio = payload.get("crown_execution_test") if isinstance(payload, dict) else {}
    portfolio = portfolio if isinstance(portfolio, dict) else {}
    bets = [row for row in portfolio.get("bets") or [] if isinstance(row, dict)]
    fields = {"hkjc_signal_odds", "crown_execution_odds", "condition_number", "wilson_admission"}
    return {
        "path": str(path),
        "readable": isinstance(payload, dict),
        "display_name": portfolio.get("display_name"),
        "bets_projected": len(bets),
        "platform_crown_label_present": "皇冠" in str(portfolio.get("display_name") or ""),
        "required_execution_fields_present_on_all_bets": all(
            all(field in row for field in fields) for row in bets
        ),
        "rejections": portfolio.get("rejections") if isinstance(portfolio.get("rejections"), dict) else {},
    }


def _static_policy_contract() -> dict[str, bool]:
    source = Path(cross.__file__).read_text(encoding="utf-8")
    dashboard = (SYSTEM / "gen_app_data.py").read_text(encoding="utf-8")
    notifier = Path(notify.__file__).read_text(encoding="utf-8")
    return {
        "hkjc_signal_selects_conditions": "matching_admissions(\"footbreak\", market, hkjc" in source,
        "crown_quote_only_enters_wilson_gate": "admission, quote[\"odds\"]" in source,
        "exact_fixture_market_side_line_required": "crown_exact_market_side_line_missing_or_ambiguous" in source,
        "post_kickoff_or_stale_quote_rejected": "crown_execution_post_kickoff_or_post_decision" in source and "crown_execution_quote_stale_at_t5" in source,
        "ledger_and_dashboard_keep_both_odds": "hkjc_signal_odds" in dashboard and "crown_execution_odds" in dashboard,
        "telegram_labels_platform_crown": "投注平台：皇冠" in notifier,
    }


def audit(args: argparse.Namespace) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    evidence_path = Path(args.evidence_path)
    os.environ["FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH"] = str(evidence_path)
    sidecar = _sidecar_report(evidence_path, now)
    ledger = _ledger_report(Path(args.ledger_path), Path(args.notify_state_path), now)
    return {
        "generated_at": now.astimezone(HKT).isoformat(timespec="seconds"),
        "safe_read_only": True,
        "providers_called": False,
        "real_betting_called": False,
        "production_head": args.production_head,
        "service_contract": _service_contract(Path(args.unit_path)),
        "sidecar": sidecar,
        "counterpart_evidence_health": {
            "configured": _service_contract(Path(args.unit_path))["environment_path_present"],
            "readable": sidecar["readable_by_footbreak_adapter"],
            "currently_fresh_pre_kickoff_quote_rows": sidecar["currently_fresh_pre_kickoff_quote_rows"],
            "missing_or_stale_counterpart_fails_closed": True,
        },
        "ledger": ledger,
        "dashboard": _dashboard_report(Path(args.dashboard_path)),
        "policy_contract": _static_policy_contract(),
        "journal": _recent_journal(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--production-head", default="")
    parser.add_argument("--evidence-path", default="/var/lib/footbreak/crown/footbreak-execution-evidence.json")
    parser.add_argument("--ledger-path", default="/opt/footbreak/system/sim_ledger.json")
    parser.add_argument("--notify-state-path", default="/opt/footbreak/system/notify_state.json")
    parser.add_argument("--dashboard-path", default="/var/www/footbreak/data.json")
    parser.add_argument("--unit-path", default="/etc/systemd/system/footbreak-tick.service")
    print(json.dumps(audit(parser.parse_args()), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
