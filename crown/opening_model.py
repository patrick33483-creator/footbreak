"""Leakage-safe Crown opening model.

The model accepts only the first complete native Crown quote and results that
were already observed before that quote.  It deliberately has no parameters
for line movement, line-ups, weather, Pinnacle, HKJC, T-30, or T-5 inputs.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from .lines import settle_handicap, settle_total
from .matching import canonical_team_key


MODEL_VERSION = "crown-opening-fixed-v1"
INPUT_POLICY = "first_complete_crown_quote_plus_pre_cutoff_results"
_STATUS_TARGET = {
    "Won": 1.0,
    "Half Won": 0.75,
    "Refunded": 0.5,
    "Half Lost": 0.25,
    "Lost": 0.0,
}


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def opening_cutoff(prices: list[dict[str, Any]]) -> datetime | None:
    """Return when the complete quote became knowable."""
    observed = [
        parsed
        for row in prices
        for parsed in [_timestamp(row.get("source_at"))]
        if parsed is not None
    ]
    return max(observed) if observed else None


def snapshot_hash(
    fixture: dict[str, Any], prices: list[dict[str, Any]], cutoff: datetime,
) -> str:
    payload = {
        "fixture": {
            key: fixture.get(key)
            for key in ("id", "league", "home", "away")
        },
        "cutoff": cutoff.isoformat(),
        "prices": sorted(
            (
                {
                    "market": row.get("market"),
                    "line": row.get("line"),
                    "selection": row.get("selection"),
                    "odds": row.get("odds"),
                    "source_at": row.get("source_at"),
                }
                for row in prices
            ),
            key=lambda row: (
                str(row["market"]), str(row["line"]), str(row["selection"]),
            ),
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _database_fingerprint(path: Path) -> tuple[int, int, int, int]:
    main = path.stat()
    wal = Path(f"{path}-wal")
    try:
        wal_stat = wal.stat()
    except OSError:
        return (main.st_mtime_ns, main.st_size, 0, 0)
    return (main.st_mtime_ns, main.st_size, wal_stat.st_mtime_ns, wal_stat.st_size)


@lru_cache(maxsize=8)
def _load_result_rows(
    path_text: str, fingerprint: tuple[int, int, int, int], limit: int,
) -> tuple[dict[str, Any], ...]:
    """Read each immutable database revision once per runner process."""
    del fingerprint
    uri = f"file:{path_text}?mode=ro"
    query = """
        WITH latest_results AS (
            SELECT system, fixture_id, MAX(result_attempt) AS result_attempt
            FROM results
            WHERE system = 'crown'
            GROUP BY system, fixture_id
        ),
        first_snapshots AS (
            SELECT system, fixture_id, MIN(snapshot_id) AS snapshot_id
            FROM prediction_snapshots
            WHERE system = 'crown' AND pre_kickoff = 1
            GROUP BY system, fixture_id
        )
        SELECT r.fixture_id, r.home_score, r.away_score, r.observed_at,
               s.kickoff, s.payload_json
        FROM latest_results lr
        JOIN results r
          ON r.system = lr.system AND r.fixture_id = lr.fixture_id
         AND r.result_attempt = lr.result_attempt
        JOIN first_snapshots fs
          ON fs.system = r.system AND fs.fixture_id = r.fixture_id
        JOIN prediction_snapshots s ON s.snapshot_id = fs.snapshot_id
        WHERE r.terminal_status = 'finished'
          AND r.home_score IS NOT NULL AND r.away_score IS NOT NULL
        ORDER BY s.kickoff DESC
        LIMIT ?
    """
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, (int(limit),)).fetchall()
    except (sqlite3.Error, OSError):
        return ()
    finally:
        if "connection" in locals():
            connection.close()
    output: list[dict[str, Any]] = []
    for row in rows:
        observed = _timestamp(row["observed_at"])
        kickoff = _timestamp(row["kickoff"])
        if observed is None or kickoff is None:
            continue
        try:
            payload = json.loads(str(row["payload_json"]))
            home = str(payload.get("home") or "").strip()
            away = str(payload.get("away") or "").strip()
            home_score = int(row["home_score"])
            away_score = int(row["away_score"])
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not home or not away or home_score < 0 or away_score < 0:
            continue
        output.append({
            "fixture_id": str(row["fixture_id"]),
            "home": home,
            "away": away,
            "home_score": home_score,
            "away_score": away_score,
            "kickoff": kickoff,
            "observed_at": observed,
        })
    return tuple(output)


def _read_prior_results(
    path: str | Path | None, cutoff: datetime, *, limit: int = 5000,
) -> list[dict[str, Any]]:
    if not path or str(path) == ":memory:":
        return []
    resolved = Path(path).resolve()
    if not resolved.is_file():
        return []
    try:
        fingerprint = _database_fingerprint(resolved)
    except OSError:
        return []
    return [
        row
        for row in _load_result_rows(str(resolved), fingerprint, int(limit))
        if row["observed_at"] < cutoff and row["kickoff"] < cutoff
    ]


def _team_matches(
    rows: list[dict[str, Any]], team: str, *, limit: int = 10,
) -> list[tuple[float, float]]:
    key = canonical_team_key(team)
    matches: list[tuple[float, float]] = []
    for row in rows:
        home_key = canonical_team_key(str(row["home"]))
        away_key = canonical_team_key(str(row["away"]))
        if key == home_key:
            matches.append((float(row["home_score"]), float(row["away_score"])))
        elif key == away_key:
            matches.append((float(row["away_score"]), float(row["home_score"])))
        if len(matches) >= limit:
            break
    return matches


def _shrunk_mean(values: list[float], prior: float, strength: float = 5.0) -> float:
    return (sum(values) + strength * prior) / (len(values) + strength)


def _poisson(lam: float, maximum: int = 12) -> list[float]:
    values = [math.exp(-lam)]
    for score in range(1, maximum + 1):
        values.append(values[-1] * lam / score)
    values[-1] += max(0.0, 1.0 - sum(values))
    return values


def _history_probabilities(
    code: str, line: float, home_lambda: float, away_lambda: float,
) -> dict[str, float]:
    home_scores, away_scores = _poisson(home_lambda), _poisson(away_lambda)
    sides = ("H", "A") if code == "HDC" else ("H", "L")
    expected = {side: 0.0 for side in sides}
    for home, home_probability in enumerate(home_scores):
        for away, away_probability in enumerate(away_scores):
            weight = home_probability * away_probability
            for side in sides:
                status = (
                    settle_handicap(line, side, home, away)
                    if code == "HDC"
                    else settle_total(line, side, home, away)
                )
                expected[side] += weight * _STATUS_TARGET[status]
    total = sum(expected.values())
    return {
        side: expected[side] / total if total else 0.5
        for side in sides
    }


def _market_probabilities(
    prices: list[dict[str, Any]], code: str, line: float,
) -> tuple[dict[str, float], dict[str, dict[str, Any]]] | None:
    sides = ("H", "A") if code == "HDC" else ("H", "L")
    selected = {
        side: next(
            (
                row for row in prices
                if row.get("market") == code
                and row.get("selection") == side
                and abs(float(row.get("line")) - line) < 1e-9
            ),
            None,
        )
        for side in sides
    }
    if not all(selected.values()):
        return None
    try:
        implied = {side: 1.0 / float(selected[side]["odds"]) for side in sides}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    total = sum(implied.values())
    return ({side: implied[side] / total for side in sides}, selected)  # type: ignore[return-value]


def apply_opening_model(
    *,
    fixture: dict[str, Any],
    prices: list[dict[str, Any]],
    forecasts: list[dict[str, Any]],
    learning_db_path: str | Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Blend opening no-vig probabilities with pre-cutoff team results.

    Market weight is 70%; the leakage-safe team-history component is 30%.
    Fewer than three known prior matches for either team fails closed to the
    opening-market probability without inventing a history signal.
    """
    cutoff = opening_cutoff(prices)
    if cutoff is None:
        return [], {
            "prediction_model": MODEL_VERSION,
            "input_policy": INPUT_POLICY,
            "opening_model_status": "missing_quote_timestamp",
            "late_inputs_used": [],
        }
    prior = _read_prior_results(learning_db_path, cutoff)
    home_matches = _team_matches(prior, str(fixture.get("home") or ""))
    away_matches = _team_matches(prior, str(fixture.get("away") or ""))
    usable = len(home_matches) >= 3 and len(away_matches) >= 3
    global_home = (
        sum(float(row["home_score"]) for row in prior) / len(prior)
        if prior else 1.40
    )
    global_away = (
        sum(float(row["away_score"]) for row in prior) / len(prior)
        if prior else 1.15
    )
    home_lambda = away_lambda = None
    if usable:
        home_attack = _shrunk_mean([row[0] for row in home_matches], global_home)
        home_defence = _shrunk_mean([row[1] for row in home_matches], global_away)
        away_attack = _shrunk_mean([row[0] for row in away_matches], global_away)
        away_defence = _shrunk_mean([row[1] for row in away_matches], global_home)
        home_lambda = max(0.20, min(4.50, (home_attack + away_defence) / 2.0))
        away_lambda = max(0.20, min(4.50, (away_attack + home_defence) / 2.0))

    output: list[dict[str, Any]] = []
    for forecast in forecasts:
        code = str(forecast.get("code") or "")
        if code not in {"HDC", "HIL"}:
            continue
        line = float(forecast["line"])
        market = _market_probabilities(prices, code, line)
        if market is None:
            continue
        market_probs, quote_rows = market
        probabilities = dict(market_probs)
        if usable and home_lambda is not None and away_lambda is not None:
            history_probs = _history_probabilities(
                code, line, home_lambda, away_lambda,
            )
            probabilities = {
                side: 0.70 * market_probs[side] + 0.30 * history_probs[side]
                for side in market_probs
            }
        side = max(probabilities, key=probabilities.get)
        row = dict(forecast)
        row.update({
            "side": side,
            "odds": round(float(quote_rows[side]["odds"]), 3),
            "observed_at": quote_rows[side].get("source_at"),
            "prob": round(probabilities[side], 5),
            "conviction": round(probabilities[side] * 100, 1),
            "reference": (
                "opening_market_70_team_history_30"
                if usable else "opening_market_no_vig_history_unavailable"
            ),
            "source": "titan007-crown-id-3-opening-fixed",
        })
        if code == "HDC":
            selected_line = -line if side == "A" else line
            label = f"皇冠初盤讓球 {'主' if side == 'H' else '客'} {selected_line:+g}"
        else:
            label = f"皇冠初盤入球大細 {'大' if side == 'H' else '細'} {line:g}"
        row["label"] = label
        output.append(row)

    metadata = {
        "prediction_model": MODEL_VERSION,
        "input_policy": INPUT_POLICY,
        "input_cutoff_at": cutoff.isoformat(),
        "opening_snapshot_hash": snapshot_hash(fixture, prices, cutoff),
        "opening_model_status": (
            "market_plus_team_history" if usable else "market_only_history_insufficient"
        ),
        "team_history_as_of": cutoff.isoformat(),
        "team_history_sample": {
            "home": len(home_matches),
            "away": len(away_matches),
            "available_prior_results": len(prior),
            "minimum_per_team": 3,
        },
        "team_history_features": (
            {
                "home_expected_goals": round(float(home_lambda), 4),
                "away_expected_goals": round(float(away_lambda), 4),
            }
            if usable and home_lambda is not None and away_lambda is not None
            else None
        ),
        "blend": {"opening_market": 0.70, "team_history": 0.30 if usable else 0.0},
        "late_inputs_used": [],
    }
    return output, metadata
