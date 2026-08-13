"""Refresh only current, future Footbreak card quote evidence.

This intentionally does *not* invoke ``run_predict``, ``record_picks``,
settlement, notification, or ledger code.  Immutable stage snapshots and
learning payloads cannot be modified through this utility.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:  # Supports both ``python system/...`` and test-package imports.
    from . import hkjc_feed as H
except ImportError:  # pragma: no cover - direct server command path
    import hkjc_feed as H


PRIVATE_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MARKETS = {"HDC", "HIL", "CHL"}


def _write_json_atomic(path: Path, payload: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        os.chmod(path.parent, PRIVATE_MODE)
    fd, temporary = tempfile.mkstemp(prefix=".refresh-current-odds-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            os.chmod(temporary, PRIVATE_FILE_MODE)
        os.replace(temporary, path)
        if private:
            os.chmod(path, PRIVATE_FILE_MODE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _future_kickoff(card: dict[str, Any], now: datetime) -> bool:
    try:
        kickoff = datetime.fromisoformat(str(card.get("kickoff_hkt") or ""))
    except (TypeError, ValueError):
        return False
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=H.HKT)
    return kickoff > now


def _line_key(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.001"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _selected_views(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Use the already-selected prediction views; never choose a new side."""
    persisted = card.get("market_predictions")
    if isinstance(persisted, list) and persisted:
        rows = persisted
    else:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in card.get("forecast_candidates") or card.get("candidates") or []:
            if isinstance(row, dict) and str(row.get("code") or "") in MARKETS:
                grouped.setdefault(str(row["code"]), []).append(row)

        def score(row: dict[str, Any]) -> tuple[float, float]:
            try:
                probability = float(row.get("prob") or 0)
                push = float(row.get("push") or 0)
            except (TypeError, ValueError):
                probability, push = 0.0, 0.0
            return probability / max(1e-9, 1.0 - push), probability

        rows = []
        for candidates in grouped.values():
            main = [row for row in candidates if row.get("is_main")]
            rows.append(max(main or candidates, key=score))
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "")
        if code not in MARKETS or code in seen:
            continue
        if row.get("side") is None or row.get("line", row.get("condition")) is None:
            continue
        output.append(row)
        seen.add(code)
    return output


def _current_journal(card: dict[str, Any], source: dict[str, Any], observed_at: str) -> list[dict[str, Any]]:
    flattened = H.flatten_odds(source)
    journal = []
    for selected in _selected_views(card):
        code = str(selected["code"])
        line = selected.get("line", selected.get("condition"))
        side = selected.get("side")
        quote = next((
            row for row in (flattened.get(code) or [])
            if _line_key(row.get("condition")) == _line_key(line)
            and side in (row.get("odds") or {})
        ), None)
        odds = (quote.get("odds") or {}).get(side) if quote else None
        try:
            available = float(odds) > 1.0
        except (TypeError, ValueError):
            available = False
        journal.append({
            "code": code,
            "line": line,
            "side": side,
            "odds": odds if available else None,
            "odds_status": "available" if available else "missing",
            "reason": None if available else "current_exact_quote_unavailable",
            "source": "hkjc_public_board",
            "provider": "HKJC",
            # The public board has no historical per-line tick time.  This is
            # accurately labelled as the time the board was observed.
            "observed_at": observed_at,
            "observed_board_at": observed_at,
        })
    return journal


def _refresh_card(card: dict[str, Any], source: dict[str, Any], observed_at: str) -> dict[str, Any]:
    refreshed = dict(card)
    journal = _current_journal(card, source, observed_at)
    complete = bool(journal) and all(
        item["odds_status"] == "available" for item in journal
    )
    refreshed.update({
        "current_selected_odds_journal": journal,
        "current_odds_status": "available" if complete else "missing",
        "current_odds_reason": (
            None if complete
            else ("no_current_selected_quote" if not journal
                  else "one_or_more_current_selected_quotes_unavailable")
        ),
        "current_odds_refreshed_at": observed_at,
        "current_odds_refresh_source": "hkjc_public_board",
    })
    return refreshed


def refresh(
    predictions_path: Path,
    dashboard_path: Path | None = None,
    status_path: Path | None = None,
    *,
    fetcher=H.fetch_matches,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Update separate current quote fields for future cards only.

    On a board error it writes a private failure status and does not mutate
    either predictions or dashboard data.  This failure-closed behavior is
    deliberately successful at the process level so a deployment does not
    masquerade a temporary public-board outage as a stage-processing failure.
    """
    current = now or datetime.now(H.HKT)
    cards = _load_json(predictions_path, [])
    if not isinstance(cards, list):
        result = {
            "ok": False, "status": "refresh_failed_closed",
            "reason": "predictions_payload_not_list", "updated": 0,
        }
        if status_path:
            _write_json_atomic(status_path, result, private=True)
        return result
    try:
        board = fetcher()
    except Exception as exc:  # no live price may ever be fabricated
        result = {
            "ok": False, "status": "refresh_failed_closed",
            "reason": f"board_fetch_{type(exc).__name__}", "updated": 0,
        }
        if status_path:
            _write_json_atomic(status_path, result, private=True)
        return result

    by_id = {
        str(row.get("id")): row for row in board
        if row.get("id") is not None and row.get("status") == "PREEVENT"
    }
    observed_at = current.isoformat(timespec="seconds")
    updated = 0
    refreshed_cards: list[dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            refreshed_cards.append(card)
            continue
        source = by_id.get(str(card.get("match_id") or ""))
        if source is None or not _future_kickoff(card, current):
            refreshed_cards.append(card)
            continue
        refreshed_cards.append(_refresh_card(card, source, observed_at))
        updated += 1

    # Only the mutable current-card file is replaced.  No stage, history,
    # learning, simulation ledger, settlement, notification, or bet path is
    # opened by this command.
    if updated:
        _write_json_atomic(predictions_path, refreshed_cards)

    dashboard_updated = 0
    if dashboard_path and dashboard_path.exists():
        dashboard = _load_json(dashboard_path, {})
        matches = dashboard.get("matches") if isinstance(dashboard, dict) else None
        if isinstance(matches, list):
            refreshed_by_id = {
                str(card.get("match_id")): card
                for card in refreshed_cards if isinstance(card, dict)
            }
            new_matches = []
            for card in matches:
                replacement = refreshed_by_id.get(
                    str(card.get("match_id") or card.get("id") or "")
                ) if isinstance(card, dict) else None
                if replacement and "current_selected_odds_journal" in replacement:
                    current_fields = {
                        key: replacement[key] for key in (
                            "current_selected_odds_journal", "current_odds_status",
                            "current_odds_reason", "current_odds_refreshed_at",
                            "current_odds_refresh_source",
                        )
                    }
                    new_matches.append({**card, **current_fields})
                    dashboard_updated += 1
                else:
                    new_matches.append(card)
            if dashboard_updated:
                _write_json_atomic(dashboard_path, {**dashboard, "matches": new_matches})

    result = {
        "ok": True, "status": "refreshed", "updated": updated,
        "dashboard_updated": dashboard_updated, "observed_at": observed_at,
        "scope": "future_current_cards_only",
    }
    if status_path:
        _write_json_atomic(status_path, result, private=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh Footbreak current odds for future dashboard cards only."
    )
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--dashboard-data", type=Path)
    parser.add_argument("--status", type=Path)
    args = parser.parse_args()
    print(json.dumps(
        refresh(args.predictions, args.dashboard_data, args.status),
        ensure_ascii=False, sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
