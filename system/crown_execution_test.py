"""Fail-closed Footbreak × Crown execution simulation.

This module consumes only persisted local Crown quote artifacts.  It does not
import the Crown engine and never performs network or provider work; a missing
or ambiguous local artifact is a durable rejection, not a fallback price.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from analysis import bilateral_decision as bilateral
from analysis.granular_conditions import MARKET_LABELS, MARKETS, _role, match_upcoming
from analysis.wilson_portfolio import _audit_selection, _native_t5, _selected
from analysis.wilson_validation import (
    DECISION_STAGE, EDGE_BUFFER, FIXED_STAKE, FIXTURE_MARKET_CAP,
    FIXTURE_STAKE_CAP, STARTING_BANKROLL, STRATEGY, admission_arithmetic,
    ensure_namespace, matching_admissions,
)

NAMESPACE = "footbreak_crown_execution_test"
PORTFOLIO = NAMESPACE
DISPLAY_NAME = "足破×皇冠執行測試倉（模擬）"
CROWN_SOURCE = "titan007-crown-id-3"
AUDIT_LIMIT = 1600
FRESHNESS_SECONDS = 120.0
HKT = timezone(timedelta(hours=8))
BRIDGE_KICKOFF_TOLERANCE_SECONDS = 10 * 60
T30_KICKOFF_REVERIFY_TOLERANCE_SECONDS = 1
T30_BOOTSTRAP_ORIGIN = "t30_bootstrap_existing_card"
T30_RECOVERY_ORIGIN = "t30_recovery_after_unresolved_first_look"
T5_IDENTITY_RECOVERY_ORIGIN = "t5_exact_id_recovery_after_unresolved_t30"

# These are reviewed display aliases, not a discovery mechanism.  They are
# used only to validate a card which already carries the same authoritative
# HKJC fixture id and exact kickoff.  In particular, they can never turn an
# unlinked Crown card into a counterpart candidate.
_IDENTITY_ALIAS_SEEDS = (
    ("貝迪斯", "皇家貝蒂斯", "皇家贝蒂斯", "Real Betis"),
    ("皇家蘇斯達", "皇家社會", "皇家社会", "Real Sociedad"),
)
_IDENTITY_LEAGUE_ALIASES = (
    ("Spain - La Liga", "西班牙甲組聯賽", "西甲", "La Liga"),
)
_IDENTITY_NOISE = re.compile(r"[\s\u3000·・.,'’`\"()（）\[\]【】\-–—_/\\|]+")


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> datetime | None:
    number = _num(value)
    if number is not None and number > 0:
        if number >= 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _same_timestamp(left: Any, right: Any, *, tolerance_seconds: float) -> bool:
    """Compare two persisted timestamps without manufacturing a fallback time."""
    left_time, right_time = _time(left), _time(right)
    return (
        left_time is not None
        and right_time is not None
        and abs((left_time - right_time).total_seconds()) <= tolerance_seconds
    )


def _identity_key(value: Any) -> str:
    """Stable, deterministic display-name normalisation for bridge auditing."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return _IDENTITY_NOISE.sub("", text)


def _seeded_identity_key(value: Any, seeds: tuple[tuple[str, ...], ...]) -> str:
    key = _identity_key(value)
    for number, variants in enumerate(seeds):
        if key in {_identity_key(candidate) for candidate in variants}:
            return f"seed:{number}"
    return key


def _identity_context(
    watch: dict[str, Any], card: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Validate both display teams and league after the authoritative id gate.

    An incomplete display context is fail-closed rather than guessed.  A
    complete, contradictory context is also rejected even if a broken
    producer happened to put an HKJC id on the card.
    """
    footbreak = {key: watch.get(key) for key in ("league", "home", "away")}
    crown = {key: card.get(key) for key in ("league", "home", "away")}
    required = all(str(footbreak.get(key) or "").strip() and str(crown.get(key) or "").strip()
                   for key in ("league", "home", "away"))
    if not required:
        return False, {
            "status": "INCOMPLETE",
            "footbreak": footbreak,
            "crown": crown,
            "note": "team_and_league_required_with_authoritative_id_and_kickoff",
        }
    same = (
        _seeded_identity_key(footbreak["league"], _IDENTITY_LEAGUE_ALIASES)
        == _seeded_identity_key(crown["league"], _IDENTITY_LEAGUE_ALIASES)
        and _seeded_identity_key(footbreak["home"], _IDENTITY_ALIAS_SEEDS)
        == _seeded_identity_key(crown["home"], _IDENTITY_ALIAS_SEEDS)
        and _seeded_identity_key(footbreak["away"], _IDENTITY_ALIAS_SEEDS)
        == _seeded_identity_key(crown["away"], _IDENTITY_ALIAS_SEEDS)
    )
    return same, {
        "status": "VALIDATED" if same else "MISMATCH",
        "footbreak": footbreak,
        "crown": crown,
        "aliases_used": any(
            _identity_key(footbreak[key]) != _identity_key(crown[key])
            for key in ("league", "home", "away")
        ),
    }
def _iso_now() -> str:
    return datetime.now(HKT).isoformat(timespec="seconds")


def _freshness_seconds() -> float:
    try:
        configured = float(os.environ.get("FOOTBREAK_CROWN_EXECUTION_MAX_AGE_SECONDS", FRESHNESS_SECONDS))
    except ValueError:
        configured = FRESHNESS_SECONDS
    return max(1.0, min(600.0, configured))


def evidence_path() -> Path:
    explicit = os.environ.get("FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH")
    if explicit:
        return Path(explicit)
    state_dir = os.environ.get("CROWN_STATE_DIR", "/var/lib/footbreak/crown")
    return Path(state_dir) / "footbreak-execution-evidence.json"


def _load_local_crown_cards() -> tuple[list[dict[str, Any]], str | None]:
    """Read bounded persisted state only; never repair it by calling Crown."""
    path = evidence_path()
    try:
        if path.stat().st_size > 8_000_000:
            return [], "crown_local_evidence_too_large"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return [], "crown_local_evidence_unavailable"
    if not isinstance(payload, list):
        return [], "crown_local_evidence_invalid"
    return [row for row in payload if isinstance(row, dict)], None


def _bridge_identity(watch: dict[str, Any], cards: list[dict[str, Any]], *, now: str) -> dict[str, Any]:
    """Resolve an execution bridge only through an authoritative HKJC fixture ID.

    Native names are retained for audit.  They can be translated aliases, so
    they never substitute for the ID.  A bounded kickoff tolerance is allowed
    only after the same authoritative ID has selected one unique Crown card.
    """
    fixture = str(watch.get("match_id") or "")
    kickoff = _time(watch.get("kickoff"))
    exact = [row for row in cards if str(row.get("hkjc_match_id") or "") == fixture]
    bridge = {
        "at": now, "counterpart_book": "crown", "hkjc_match_id": fixture,
        "footbreak_kickoff": watch.get("kickoff"),
        "footbreak_fixture": {
            key: watch.get(key) for key in ("league", "home", "away")
        },
    }
    if not fixture or kickoff is None:
        return bridge | {"status": "UNAVAILABLE",
                         "reason": "crown_fixture_identity_incomplete"}
    if not cards:
        return bridge | {"status": "UNAVAILABLE",
                         "reason": "crown_collector_unavailable"}
    if not exact:
        return bridge | {"status": "UNAVAILABLE",
                         "reason": "crown_fixture_not_listed"}
    if len(exact) != 1 or not str(exact[0].get("match_id") or "").strip():
        return bridge | {"status": "UNAVAILABLE",
                         "reason": "crown_fixture_identity_ambiguous"}
    card = exact[0]
    crown_kickoff = _time(card.get("kickoff_hkt") or card.get("kickoff"))
    if crown_kickoff is None or abs((crown_kickoff - kickoff).total_seconds()) > BRIDGE_KICKOFF_TOLERANCE_SECONDS:
        return bridge | {"status": "UNAVAILABLE",
                         "reason": "crown_fixture_kickoff_identity_mismatch"}
    context_ok, context = _identity_context(watch, card)
    if not context_ok:
        return bridge | {
            "status": "UNAVAILABLE",
            "reason": (
                "crown_fixture_identity_incomplete"
                if context.get("status") == "INCOMPLETE"
                else "crown_fixture_team_or_league_identity_mismatch"
            ),
            "identity_context": context,
        }
    bridge_id = hashlib.sha256(_canonical_bridge_identity(
        fixture, str(card.get("match_id") or ""), kickoff, crown_kickoff,
    ).encode("utf-8")).hexdigest()
    return bridge | {
        "status": "RESOLVED", "reason": None,
        "crown_match_id": str(card.get("match_id") or ""),
        "crown_kickoff": card.get("kickoff_hkt") or card.get("kickoff"),
        "crown_fixture": {key: card.get(key) for key in ("league", "home", "away")},
        "mapping": copy.deepcopy(card.get("mapping") or {}),
        "identity_confidence": "authoritative_hkjc_id_unique",
        "kickoff_delta_seconds": round(abs((crown_kickoff - kickoff).total_seconds()), 3),
        "bridge_id": bridge_id,
        "identity_context": context,
    }


def _canonical_bridge_identity(
    hkjc_match_id: str, crown_match_id: str, footbreak_kickoff: datetime, crown_kickoff: datetime,
) -> str:
    return json.dumps({
        "hkjc_match_id": hkjc_match_id,
        "crown_match_id": crown_match_id,
        "footbreak_kickoff": footbreak_kickoff.isoformat(),
        "crown_kickoff": crown_kickoff.isoformat(),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _t30_market_mappings(watch: dict[str, Any], card: dict[str, Any]) -> dict[str, dict[str, Any]]:
    current = next((row for row in watch.get("stages") or []
                    if isinstance(row, dict) and row.get("stage") == "T-30"), {})
    kickoff = _time(watch.get("kickoff"))
    journal, board_reason, coverage = (
        _native_stage_board(card, "T-30", kickoff)
        if kickoff is not None else (None, "crown_fixture_kickoff_identity_mismatch", "unavailable")
    )
    output: dict[str, dict[str, Any]] = {}
    for signal in current.get("market_predictions") or []:
        if not isinstance(signal, dict):
            continue
        market = str(signal.get("code") or "").upper()
        side = str(signal.get("side") or "").upper()
        line = _num(signal.get("line", signal.get("condition")))
        if market not in MARKETS or line is None or not _valid_side(market, side):
            continue
        if board_reason:
            output[market] = {
                "side": side, "line": line, "status": "UNAVAILABLE",
                "reason": board_reason,
                "coverage": coverage,
            }
            continue
        if not isinstance(journal, list):
            output[market] = {
                "side": side, "line": line, "status": "UNAVAILABLE",
                "reason": "crown_t30_collector_unavailable",
                "coverage": coverage,
            }
            continue
        market_rows, side_rows, exact = _exact_board_rows(journal, market, side, line)
        if not market_rows:
            reason = "crown_t30_market_unavailable"
        elif not side_rows:
            reason = "crown_t30_market_side_unavailable"
        elif not exact:
            reason = "crown_t30_exact_line_unavailable"
        elif len(exact) != 1:
            reason = "crown_t30_exact_market_side_line_ambiguous"
        elif str(exact[0].get("status") or exact[0].get("odds_status") or "AVAILABLE").upper() != "AVAILABLE":
            reason = str(exact[0].get("reason") or "crown_t30_exact_quote_unavailable")
        else:
            reason = None
        output[market] = {
            "side": side, "line": line,
            "status": "AVAILABLE" if reason is None else "UNAVAILABLE",
            "reason": reason,
            "coverage": coverage,
            "crown_market": (
                {"code": market, "side": side, "line": line}
                if reason is None else None
            ),
        }
    return output


def _has_native_stage(watch: dict[str, Any], stage: str) -> bool:
    """Whether Footbreak has durably committed the named native stage."""
    return any(
        isinstance(row, dict) and str(row.get("stage") or "") == stage
        for row in (watch.get("stages") or [])
    )


def _record_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def _valid_unresolved_first_look(
    watch: dict[str, Any], first: Any, *, native_t30_at: Any = None,
) -> bool:
    if not isinstance(first, dict) or first.get("status") != "UNAVAILABLE":
        return False
    allowed_reasons = {
        "crown_local_evidence_too_large",
        "crown_local_evidence_unavailable",
        "crown_local_evidence_invalid",
        "crown_fixture_identity_incomplete",
        "crown_collector_unavailable",
        "crown_fixture_not_listed",
        "crown_fixture_identity_ambiguous",
        "crown_fixture_kickoff_identity_mismatch",
        "crown_fixture_team_or_league_identity_mismatch",
    }
    fixture = first.get("footbreak_fixture")
    first_at = _time(first.get("at"))
    t30_at = _time(native_t30_at)
    kickoff = _time(watch.get("kickoff"))
    return (
        first_at is not None and t30_at is not None and kickoff is not None
        and first_at < t30_at < kickoff
        and str(first.get("reason") or "") in allowed_reasons
        and str(first.get("counterpart_book") or "") == "crown"
        and str(first.get("hkjc_match_id") or "") == str(watch.get("match_id") or "")
        and _same_timestamp(
            first.get("footbreak_kickoff"), watch.get("kickoff"),
            tolerance_seconds=0,
        )
        and isinstance(fixture, dict)
        and all(str(fixture.get(key) or "") == str(watch.get(key) or "")
                for key in ("league", "home", "away"))
    )


def _native_t30_proof(
    ledger: dict[str, Any] | None, watch: dict[str, Any],
) -> dict[str, Any] | None:
    """Bind recovery to one manifest-backed snapshot and COMMITTED attempt."""
    if not isinstance(ledger, dict):
        return None
    manifest = watch.get("native_stage_manifest")
    identity = manifest.get("identity") if isinstance(manifest, dict) else None
    jobs = manifest.get("jobs") if isinstance(manifest, dict) else None
    job = jobs.get("T-30") if isinstance(jobs, dict) else None
    fixture = str(watch.get("match_id") or "")
    kickoff = _time(watch.get("kickoff"))
    manifest_kickoff = _time(identity.get("kickoff_at_utc")) if isinstance(identity, dict) else None
    manifest_top_kickoff = _time(manifest.get("kickoff_at_utc")) if isinstance(manifest, dict) else None
    manifest_kickoff_hkt = _time(manifest.get("kickoff_at_hkt")) if isinstance(manifest, dict) else None
    manifest_created = _time(manifest.get("created_at")) if isinstance(manifest, dict) else None
    due_utc = _time(job.get("due_at_utc")) if isinstance(job, dict) else None
    due_hkt = _time(job.get("due_at_hkt")) if isinstance(job, dict) else None
    expected_due = kickoff - timedelta(minutes=30) if kickoff is not None else None
    if (
        not fixture or kickoff is None or manifest_kickoff is None
        or not isinstance(manifest, dict) or manifest.get("schema_version") != 1
        or str(manifest.get("origin") or "") not in {
            "first_look", "migration_existing_future_card",
        }
        or manifest_created is None or manifest_created >= kickoff
        or manifest_top_kickoff is None or manifest_kickoff_hkt is None
        or not _same_timestamp(manifest_kickoff, manifest_top_kickoff, tolerance_seconds=0)
        or not _same_timestamp(manifest_kickoff, manifest_kickoff_hkt, tolerance_seconds=0)
        or str(identity.get("hkjc_match_id") or "") != fixture
        or not _same_timestamp(kickoff, manifest_kickoff, tolerance_seconds=0)
        or not isinstance(job, dict)
        or job.get("stage") != "T-30"
        or due_utc is None or due_hkt is None or expected_due is None
        or not _same_timestamp(due_utc, due_hkt, tolerance_seconds=0)
        or not _same_timestamp(due_utc, expected_due, tolerance_seconds=0)
    ):
        return None
    snapshots = [
        row for row in watch.get("stages") or []
        if isinstance(row, dict) and row.get("stage") == "T-30"
    ]
    if len(snapshots) != 1:
        return None
    snapshot = snapshots[0]
    snapshot_at = _time(snapshot.get("ts"))
    snapshot_kickoff_hkt = _time(snapshot.get("kickoff_at_hkt"))
    snapshot_due_utc = _time(snapshot.get("due_at_utc"))
    snapshot_due_hkt = _time(snapshot.get("due_at_hkt"))
    snapshot_id = str(snapshot.get("native_snapshot_id") or "")
    attempt_id = snapshot_id.removeprefix("attempt:") if snapshot_id.startswith("attempt:") else ""
    if (
        not attempt_id
        or snapshot_at is None or snapshot_at < due_utc or snapshot_at >= kickoff
        or str(snapshot.get("match_id") or "") != fixture
        or not _same_timestamp(
            snapshot.get("kickoff_at_utc"), identity.get("kickoff_at_utc"),
            tolerance_seconds=0,
        )
        or snapshot_kickoff_hkt is None
        or not _same_timestamp(snapshot_kickoff_hkt, kickoff, tolerance_seconds=0)
        or snapshot_due_utc is None or snapshot_due_hkt is None
        or not _same_timestamp(snapshot_due_utc, due_utc, tolerance_seconds=0)
        or not _same_timestamp(snapshot_due_hkt, due_utc, tolerance_seconds=0)
    ):
        return None
    attempt_events = [
        row for row in ledger.get("native_stage_attempts") or []
        if isinstance(row, dict)
        and str(row.get("attempt_id") or "") == attempt_id
        and row.get("stage") == "T-30"
        and str(row.get("hkjc_match_id") or "") == fixture
        and _same_timestamp(
            row.get("kickoff_at_utc"), identity.get("kickoff_at_utc"),
            tolerance_seconds=0,
        )
        and _same_timestamp(row.get("kickoff_at_hkt"), kickoff, tolerance_seconds=0)
        and _same_timestamp(row.get("due_at_utc"), due_utc, tolerance_seconds=0)
        and _same_timestamp(row.get("due_at_hkt"), due_utc, tolerance_seconds=0)
    ]
    if (
        len(attempt_events) != 2
        or [row.get("status") for row in attempt_events] != ["STARTED", "COMMITTED"]
    ):
        return None
    started_at = _time(attempt_events[0].get("at"))
    committed_at = _time(attempt_events[1].get("at"))
    if (
        started_at is None or committed_at is None
        or manifest_created > started_at
        or started_at < due_utc or started_at > snapshot_at
        or committed_at < snapshot_at or committed_at >= kickoff
    ):
        return None
    proof = {
        "manifest_identity": copy.deepcopy(identity),
        "manifest_job": copy.deepcopy(job),
        "manifest_origin": manifest.get("origin"),
        "manifest_created_at": manifest.get("created_at"),
        "snapshot": copy.deepcopy(snapshot),
        "attempts": copy.deepcopy(attempt_events),
    }
    return {
        "record": proof, "hash": _record_hash(proof),
        "snapshot_at": snapshot_at,
    }


def _verified_t30_bootstrap(
    watch: dict[str, Any], t30: dict[str, Any], ledger: dict[str, Any] | None = None,
) -> bool:
    """Validate the narrowly allowed existing-card T-30 bridge.

    This rejects an arbitrary ``t30`` object labelled as a bootstrap.  The
    persisted record must contain the same fully authoritative identity that
    normal first-look resolution would have written, including the canonical
    bridge hash and validated display context.  It is intentionally not a
    substitute for a normal first-look record on newly created cards.
    """
    origin = t30.get("origin")
    first = (((watch.get("counterpart_bridges") or {}).get("crown") or {}).get("first_look"))
    if origin == T30_BOOTSTRAP_ORIGIN:
        valid_transition = not isinstance(first, dict)
    elif origin == T30_RECOVERY_ORIGIN:
        native_proof = _native_t30_proof(ledger, watch)
        valid_transition = (
            isinstance(native_proof, dict)
            and _valid_unresolved_first_look(
                watch, first, native_t30_at=native_proof.get("snapshot_at"),
            )
            and t30.get("first_look_recorded") is True
            and t30.get("first_look_hash") == _record_hash(first)
            and t30.get("native_t30_proof_hash") == native_proof.get("hash")
        )
    else:
        valid_transition = False
    if (
        t30.get("status") != "RESOLVED"
        or not valid_transition
        or str(t30.get("hkjc_match_id") or "") != str(watch.get("match_id") or "")
        or not str(t30.get("crown_match_id") or "").strip()
        or t30.get("identity_confidence") != "authoritative_hkjc_id_unique"
    ):
        return False
    footbreak_kickoff = _time(watch.get("kickoff"))
    crown_kickoff = _time(t30.get("crown_kickoff"))
    if footbreak_kickoff is None or crown_kickoff is None:
        return False
    expected = hashlib.sha256(_canonical_bridge_identity(
        str(watch.get("match_id") or ""),
        str(t30.get("crown_match_id") or ""),
        footbreak_kickoff,
        crown_kickoff,
    ).encode("utf-8")).hexdigest()
    context = t30.get("identity_context")
    crown_fixture = t30.get("crown_fixture")
    context_ok, expected_context = _identity_context(
        watch, crown_fixture if isinstance(crown_fixture, dict) else {},
    )
    expected_delta = abs((crown_kickoff - footbreak_kickoff).total_seconds())
    return (
        t30.get("bridge_id") == expected
        and isinstance(context, dict)
        and context_ok
        and context == expected_context
        and expected_delta <= BRIDGE_KICKOFF_TOLERANCE_SECONDS
        and _num(t30.get("kickoff_delta_seconds")) is not None
        and abs(float(t30["kickoff_delta_seconds"]) - expected_delta) <= 1e-3
    )


def _resolved_identity_valid(
    watch: dict[str, Any], value: dict[str, Any],
) -> bool:
    """Revalidate one resolved exact-ID bridge without trusting its label."""
    if (
        value.get("status") != "RESOLVED"
        or str(value.get("hkjc_match_id") or "") != str(watch.get("match_id") or "")
        or value.get("identity_confidence") != "authoritative_hkjc_id_unique"
        or not str(value.get("crown_match_id") or "").strip()
    ):
        return False
    footbreak_kickoff = _time(watch.get("kickoff"))
    crown_kickoff = _time(value.get("crown_kickoff"))
    crown_fixture = value.get("crown_fixture")
    if (
        footbreak_kickoff is None or crown_kickoff is None
        or not isinstance(crown_fixture, dict)
    ):
        return False
    context_ok, expected_context = _identity_context(watch, crown_fixture)
    expected_delta = abs((crown_kickoff - footbreak_kickoff).total_seconds())
    expected_bridge_id = hashlib.sha256(_canonical_bridge_identity(
        str(watch.get("match_id") or ""),
        str(value.get("crown_match_id") or ""),
        footbreak_kickoff,
        crown_kickoff,
    ).encode("utf-8")).hexdigest()
    return (
        context_ok
        and value.get("identity_context") == expected_context
        and value.get("bridge_id") == expected_bridge_id
        and expected_delta <= BRIDGE_KICKOFF_TOLERANCE_SECONDS
        and _num(value.get("kickoff_delta_seconds")) is not None
        and abs(float(value["kickoff_delta_seconds"]) - expected_delta) <= 1e-3
    )


def _verified_t5_identity_recovery(
    watch: dict[str, Any], recovery: Any,
    ledger: dict[str, Any] | None,
) -> bool:
    """Validate the append-only exact-ID recovery used after a raced T-30."""
    if not isinstance(recovery, dict) or recovery.get("origin") != T5_IDENTITY_RECOVERY_ORIGIN:
        return False
    root = ((watch.get("counterpart_bridges") or {}).get("crown") or {})
    first, t30 = root.get("first_look"), root.get("t30")
    proof = _native_t30_proof(ledger, watch)
    return (
        isinstance(first, dict) and first.get("status") == "UNAVAILABLE"
        and isinstance(t30, dict) and t30.get("status") == "UNAVAILABLE"
        and isinstance(proof, dict)
        and _valid_unresolved_first_look(
            watch, first, native_t30_at=proof.get("snapshot_at"),
        )
        and recovery.get("first_look_hash") == _record_hash(first)
        and recovery.get("t30_hash") == _record_hash(t30)
        and recovery.get("native_t30_proof_hash") == proof.get("hash")
        and _resolved_identity_valid(watch, recovery)
    )


def _attempt_t5_identity_recovery(
    watch: dict[str, Any], *, now: str,
    ledger: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Append a narrowly proven identity recovery; never rewrite earlier rows."""
    root = watch.setdefault("counterpart_bridges", {}).setdefault(
        "crown", {"schema_version": 3, "counterpart_book": "crown"},
    )
    existing = root.get("t5_identity_recovery")
    if isinstance(existing, dict):
        return existing
    first, t30 = root.get("first_look"), root.get("t30")
    proof = _native_t30_proof(ledger, watch)
    if (
        not isinstance(first, dict) or first.get("status") != "UNAVAILABLE"
        or not isinstance(t30, dict) or t30.get("status") != "UNAVAILABLE"
        or not isinstance(proof, dict)
        or not _valid_unresolved_first_look(
            watch, first, native_t30_at=proof.get("snapshot_at"),
        )
    ):
        return None
    cards, error = _load_local_crown_cards()
    if error or not cards:
        return None
    value = _bridge_identity(watch, cards, now=now)
    if value.get("status") != "RESOLVED":
        return None
    recovery = value | {
        "origin": T5_IDENTITY_RECOVERY_ORIGIN,
        "first_look_hash": _record_hash(first),
        "t30_hash": _record_hash(t30),
        "native_t30_proof_hash": proof["hash"],
    }
    root["t5_identity_recovery"] = recovery
    return recovery


def prefetch_bridge(
    watch: dict[str, Any], *, stage: str = "T-30", now: str | None = None,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist first-look identity and T-30 exact-market evidence, locally only.

    A T-30 call may bootstrap *only* a card that was durably created before
    this bridge existed and has no first-look object.  That bounded transition
    keeps the historical fact intact: it never manufactures a ``first_look``
    stage, only records an independently authoritative T-30 identity.
    """
    now = now or _iso_now()
    now_time = _time(now)
    kickoff = _time(watch.get("kickoff"))
    root = watch.setdefault("counterpart_bridges", {}).setdefault(
        "crown", {"schema_version": 3, "counterpart_book": "crown"},
    )
    persisted_t30 = root.get("t30")
    if stage == "T-30" and isinstance(persisted_t30, dict):
        # Every persisted T-30 is immutable historical evidence.  Consumers
        # validate it fail-closed; a later provider read can never rewrite it.
        return persisted_t30
    cards, error = _load_local_crown_cards()
    if kickoff is None or now_time is None:
        value = {"at": now, "status": "UNAVAILABLE", "reason": "crown_fixture_identity_incomplete"}
    elif now_time >= kickoff:
        value = {"at": now, "status": "UNAVAILABLE", "reason": "crown_bridge_post_kickoff_rejected"}
    elif error:
        value = {"at": now, "status": "UNAVAILABLE", "reason": error}
    else:
        value = _bridge_identity(watch, cards, now=now)
    if stage == "首預":
        root["first_look"] = value
        return value
    original_first = root.get("first_look")
    first = original_first if isinstance(original_first, dict) else {}
    bootstrap = not isinstance(root.get("first_look"), dict)
    recovery_candidate = (
        isinstance(original_first, dict)
        and original_first.get("status") == "UNAVAILABLE"
        and value.get("status") == "RESOLVED"
    )
    native_proof = _native_t30_proof(ledger, watch) if recovery_candidate else None
    recovery = (
        recovery_candidate and isinstance(native_proof, dict)
        and _valid_unresolved_first_look(
            watch, original_first, native_t30_at=native_proof.get("snapshot_at"),
        )
    )
    if bootstrap and not _has_native_stage(watch, "T-30"):
        # This function is normally called immediately after record_picks has
        # atomically stored a genuine native T-30.  Do not make it usable as a
        # generic pre-kickoff bulk/backfill tool.
        value = {
            "at": now, "status": "UNAVAILABLE",
            "reason": "crown_t30_bootstrap_native_stage_missing",
            "origin": T30_BOOTSTRAP_ORIGIN,
        }
    elif bootstrap:
        # The identity resolver above still enforces authoritative HKJC id,
        # unique Crown card, kickoff, team, and league checks.  This explicit
        # provenance is what allows T-5 to distinguish the safe transition
        # path from a missing or fabricated first-look stage.
        value = value | {
            "origin": T30_BOOTSTRAP_ORIGIN,
            "first_look_recorded": False,
        }
    elif recovery_candidate and not recovery:
        value = {
            "at": now, "status": "UNAVAILABLE",
            "reason": "crown_t30_recovery_native_stage_missing",
            "origin": T30_RECOVERY_ORIGIN,
            "first_look_recorded": True,
            "first_look_hash": _record_hash(original_first),
        }
    elif recovery:
        # Preserve the failed first-look record as historical evidence.  A
        # genuine native T-30 may recover only through the same authoritative
        # fixture-id, kickoff, team and league checks used by the legacy
        # bootstrap path.
        value = value | {
            "origin": T30_RECOVERY_ORIGIN,
            "first_look_recorded": True,
            "first_look_hash": _record_hash(original_first),
            "native_t30_proof_hash": native_proof["hash"],
        }
    # T-30 must re-verify precisely the ID/card chosen at first look.  Never
    # replace a prior bridge with a later fuzzy/name-only candidate.
    if not bootstrap and not recovery and value.get("status") == "RESOLVED" and (
        first.get("status") != "RESOLVED"
        or first.get("crown_match_id") != value.get("crown_match_id")
        or first.get("bridge_id") != value.get("bridge_id")
        or not _same_timestamp(
            first.get("crown_kickoff"),
            value.get("crown_kickoff"),
            tolerance_seconds=T30_KICKOFF_REVERIFY_TOLERANCE_SECONDS,
        )
    ):
        value = value | {"status": "UNAVAILABLE",
                         "reason": "crown_first_look_bridge_missing_or_changed"}
    if value.get("status") == "RESOLVED":
        card = next((row for row in cards if str(row.get("match_id") or "") == value["crown_match_id"]), {})
        value["market_mappings"] = _t30_market_mappings(watch, card)
    root["t30"] = value
    return value


def ensure_namespace(ledger: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
    ns = ledger.get(NAMESPACE)
    if ns is None:
        ns = {}
        ledger[NAMESPACE] = ns
    if not isinstance(ns, dict):
        raise ValueError("cross-book namespace must be an object")
    ns.setdefault("schema_version", 1)
    ns.setdefault("display_name", DISPLAY_NAME)
    ns.setdefault("activation_at", now or _iso_now())
    ns.setdefault("starting_bankroll", STARTING_BANKROLL)
    ns.setdefault("fixed_stake", FIXED_STAKE)
    ns.setdefault("fixture_stake_cap", FIXTURE_STAKE_CAP)
    ns.setdefault("fixture_market_cap", FIXTURE_MARKET_CAP)
    ns.setdefault("bets", [])
    ns.setdefault("audit", [])
    ns.setdefault("notifications", {"sent": []})
    if not isinstance(ns["bets"], list) or not isinstance(ns["audit"], list):
        raise ValueError("cross-book namespace collections must be arrays")
    return ns


def bets(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    ns = ledger.get(NAMESPACE)
    return list(ns.get("bets") or []) if isinstance(ns, dict) else []


def _append_audit(ns: dict[str, Any], now: str, fixture: str, row: dict[str, Any]) -> None:
    ns["audit"] = (ns.get("audit") or []) + [{"ts": now, "match_id": fixture, **row}]
    ns["audit"] = ns["audit"][-AUDIT_LIMIT:]


def _valid_side(market: str, side: Any) -> bool:
    return str(side or "").upper() in ({"H", "A"} if market == "HDC" else {"H", "L"})


def _native_stage_board(
    card: dict[str, Any], stage: str, kickoff: datetime,
    decision_at: datetime | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None, str]:
    """Read one immutable normalized board, with explicit legacy coverage."""
    boards = card.get("native_stage_quote_boards")
    if isinstance(boards, dict) and stage in boards:
        board = boards.get(stage)
        if not isinstance(board, dict) or board.get("stage") != stage:
            return None, f"crown_native_{stage.lower().replace('-', '')}_board_invalid", "native_board_invalid"
        board_at = _time(board.get("stage_at"))
        if board_at is None:
            return None, f"crown_native_{stage.lower().replace('-', '')}_stage_timestamp_missing", "native_board_invalid"
        if board_at >= kickoff:
            return None, f"crown_native_{stage.lower().replace('-', '')}_post_kickoff_rejected", "native_board_invalid"
        if decision_at is not None and board_at > decision_at:
            return None, f"crown_native_{stage.lower().replace('-', '')}_post_decision_rejected", "native_board_invalid"
        rows = board.get("quotes")
        if not isinstance(rows, list):
            return None, f"crown_native_{stage.lower().replace('-', '')}_board_invalid", "native_board_invalid"
        return [row for row in rows if isinstance(row, dict)], None, str(
            board.get("coverage") or "native_full_board"
        )
    journals = card.get("native_stage_journals")
    journal = journals.get(stage) if isinstance(journals, dict) else None
    if isinstance(journal, list):
        # A pre-schema native snapshot had only selected rows.  It can still
        # prove a specific persisted price, but can never imply an absent
        # opposite side was available.
        return [row for row in journal if isinstance(row, dict)], None, "legacy_selected_quotes_only"
    if stage == "T-5":
        journal = card.get("current_selected_odds_journal")
        if isinstance(journal, list):
            return [row for row in journal if isinstance(row, dict)], None, "legacy_selected_quotes_only"
    return None, None, "unavailable"


def _exact_board_rows(
    rows: list[dict[str, Any]], market: str, side: str, line: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    market_rows = [
        row for row in rows
        if str(row.get("code") or "").upper() == market
    ]
    side_rows = [
        row for row in market_rows
        if str(row.get("side") or "").upper() == side
    ]
    exact = [
        row for row in side_rows
        if _num(row.get("line", row.get("condition"))) is not None
        and abs(float(_num(row.get("line", row.get("condition"))) - line)) <= 1e-8
    ]
    return market_rows, side_rows, exact


def _crown_quote_for_exact_fixture(
    fixture: str, market: str, side: str, line: float, stage_at: datetime, kickoff: datetime,
    *, expected_crown_match_id: str | None = None,
    decision_at: datetime | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    cards, error = _load_local_crown_cards()
    if error:
        return None, error
    # Cross-book identity is allowed only through the persisted Crown HKJC
    # bridge.  Title/team fuzzy matching is deliberately forbidden.
    # An empty local sidecar cannot establish that Crown had no same-fixture
    # market: it is a missing-native-T-5 collection/evidence state.  Once
    # cards exist, a missing or ambiguous bridge identity remains the genuine
    # fail-closed no-same-fixture outcome.
    if not cards:
        return None, "crown_native_t5_not_collected"
    cards = [row for row in cards if str(row.get("hkjc_match_id") or "") == fixture]
    if len(cards) != 1:
        return None, "crown_fixture_identity_missing_or_ambiguous"
    card = cards[0]
    if expected_crown_match_id and str(card.get("match_id") or "") != expected_crown_match_id:
        return None, "crown_t5_bridge_changed"
    card_kickoff = _time(card.get("kickoff_hkt") or card.get("kickoff"))
    if card_kickoff is None or abs((card_kickoff - kickoff).total_seconds()) > 1:
        return None, "crown_fixture_kickoff_identity_mismatch"
    cutoff = decision_at or stage_at
    rows, board_reason, coverage = _native_stage_board(
        card, "T-5", kickoff, decision_at=cutoff,
    )
    if board_reason:
        return None, board_reason
    if rows is None:
        return None, "crown_exact_quote_journal_missing"
    _market_rows, _side_rows, exact = _exact_board_rows(rows, market, side, line)
    if len(exact) != 1:
        return None, "crown_exact_market_side_line_missing_or_ambiguous"
    quote = exact[0]
    if str(quote.get("status") or quote.get("odds_status") or "AVAILABLE").upper() != "AVAILABLE":
        return None, str(quote.get("reason") or "crown_execution_quote_not_available")
    odds = _num(quote.get("odds"))
    source = str(quote.get("source") or "").strip().lower()
    observed = _time(quote.get("observed_at"))
    if odds is None or odds <= 1:
        return None, "crown_execution_odds_invalid_or_missing"
    if source != CROWN_SOURCE:
        return None, "crown_execution_source_invalid_or_missing"
    if observed is None:
        return None, "crown_execution_timestamp_missing"
    if cutoff < stage_at or cutoff >= kickoff:
        return None, "crown_execution_post_kickoff_or_post_decision"
    if observed >= kickoff or observed > cutoff:
        return None, "crown_execution_post_kickoff_or_post_decision"
    freshness_reference = stage_at if observed <= stage_at else observed
    if (freshness_reference - observed).total_seconds() > _freshness_seconds():
        return None, "crown_execution_quote_stale_at_t5"
    return {
        "odds": odds, "source": source, "observed_at": quote.get("observed_at"),
        "line": quote.get("line", quote.get("condition")), "side": side,
        "fixture_identity": {"hkjc_match_id": fixture, "crown_hkjc_match_id": fixture},
        "coverage": coverage,
    }, None


def _crown_quote_for_verified_bridge(
    watch: dict[str, Any], market: str, side: str, line: float,
    stage_at: datetime, kickoff: datetime, ledger: dict[str, Any] | None = None,
    decision_at: datetime | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Use a normal first-look bridge or one verified existing-card bootstrap."""
    bridge = ((watch.get("counterpart_bridges") or {}).get("crown") or {})
    first = bridge.get("first_look") if isinstance(bridge, dict) else None
    t30 = bridge.get("t30") if isinstance(bridge, dict) else None
    bootstrap_marked = not isinstance(first, dict) and isinstance(t30, dict) and (
        t30.get("origin") == T30_BOOTSTRAP_ORIGIN
    )
    recovery_marked = (
        isinstance(first, dict)
        and first.get("status") != "RESOLVED"
        and isinstance(t30, dict)
        and t30.get("origin") == T30_RECOVERY_ORIGIN
    )
    t5_recovery = bridge.get("t5_identity_recovery") if isinstance(bridge, dict) else None
    if _verified_t5_identity_recovery(watch, t5_recovery, ledger):
        return _crown_quote_for_exact_fixture(
            str(watch.get("match_id") or ""), market, side, line, stage_at, kickoff,
            expected_crown_match_id=str(t5_recovery.get("crown_match_id") or ""),
            decision_at=decision_at,
        )
    # A genuine T-30 bootstrap can itself fail closed (no fixture, ambiguous
    # identity, kickoff mismatch, etc.).  Retain that already-persisted fact
    # at T-5; do not mislabel a valid UNAVAILABLE bootstrap record as a forged
    # bridge.  Only a claimed *resolved* bootstrap needs the extra integrity
    # verification below.
    if (bootstrap_marked or recovery_marked) and t30.get("status") != "RESOLVED":
        return None, str(t30.get("reason") or "crown_t30_bridge_missing_or_unresolved")
    transition = (bootstrap_marked or recovery_marked) and t30.get("status") == "RESOLVED"
    if transition and not _verified_t30_bootstrap(watch, t30, ledger):
        return None, "crown_t30_bootstrap_unverified"
    if not transition and (not isinstance(first, dict) or first.get("status") != "RESOLVED"):
        return None, "crown_first_look_bridge_missing_or_unresolved"
    if not isinstance(t30, dict) or t30.get("status") != "RESOLVED":
        return None, "crown_t30_bridge_missing_or_unresolved"
    if not transition and first.get("crown_match_id") != t30.get("crown_match_id"):
        return None, "crown_t30_bridge_changed"
    mapping = (t30.get("market_mappings") or {}).get(market)
    if not isinstance(mapping, dict) or mapping.get("status") != "AVAILABLE":
        return None, str((mapping or {}).get("reason") or "crown_t30_market_unavailable")
    mapped_line = _num(mapping.get("line"))
    if str(mapping.get("side") or "").upper() != side:
        return None, "crown_t30_market_side_unavailable"
    if mapped_line is None or abs(mapped_line - line) > 1e-8:
        return None, "crown_t30_exact_line_unavailable"
    return _crown_quote_for_exact_fixture(
        str(watch.get("match_id") or ""), market, side, line, stage_at, kickoff,
        expected_crown_match_id=str(t30.get("crown_match_id") or ""),
        decision_at=decision_at,
    )


def capture_t5_counterparts(
    watch: dict[str, Any], *, now: str | None = None,
    ledger: dict[str, Any] | None = None,
    grace_deadline_at: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Persist T-5 comparison evidence independently of formal admission.

    It is an execution/presentation artifact only: neither a missing bridge
    nor an unavailable Crown quote can block native Footbreak observation,
    Wilson rollover, or durable Telegram outbox creation.
    """
    current = next((row for row in watch.get("stages") or []
                    if isinstance(row, dict) and row.get("stage") == DECISION_STAGE), None)
    kickoff = _time(watch.get("kickoff"))
    stage_at = _time((current or {}).get("ts") or (current or {}).get("source_snapshot_at"))
    captured_at = now or _iso_now()
    capture_time = _time(captured_at)
    output: dict[str, dict[str, Any]] = {}
    if not isinstance(current, dict) or kickoff is None or stage_at is None or stage_at >= kickoff:
        return output
    bridge = watch.setdefault("counterpart_bridges", {}).setdefault("crown", {"schema_version": 3})
    # The decision timestamp is immutable evidence, but a delayed/replayed
    # worker must not turn an old T-5 into a newly captured Crown quote after
    # kickoff.  Persist the refusal so the native Footbreak observation can
    # still format an accurate counterpart status.
    if capture_time is None or capture_time >= kickoff:
        output = {
            market: {
                "status": "UNAVAILABLE",
                "reason": "crown_t5_post_kickoff_capture_rejected",
                "market": market,
                "captured_at": captured_at,
            }
            for market in MARKETS
        }
        bridge["t5"] = {"at": captured_at, "markets": output}
        return output
    deadline = _time(grace_deadline_at)
    decision_cutoff = deadline or capture_time
    _attempt_t5_identity_recovery(watch, now=captured_at, ledger=ledger)
    for market in MARKETS:
        signal, reason = _hkjc_selected(current, market, watch)
        if signal is None:
            output[market] = {"status": "UNAVAILABLE", "reason": reason}
            continue
        side = str(signal.get("side") or "").upper()
        line = _num(signal.get("line", signal.get("condition")))
        if line is None or not _valid_side(market, side):
            output[market] = {"status": "UNAVAILABLE", "reason": "hkjc_signal_line_or_side_invalid"}
            continue
        quote, reason = _crown_quote_for_verified_bridge(
            watch, market, side, line, stage_at, kickoff, ledger=ledger,
            decision_at=decision_cutoff,
        )
        output[market] = {
            "status": "AVAILABLE" if quote else "UNAVAILABLE",
            "reason": reason, "market": market, "side": side, "line": line,
            "hkjc_observed_at": signal.get("observed_at"),
            "crown_quote": copy.deepcopy(quote) if quote else None,
            "captured_at": captured_at,
        }
    temporary_reasons = {
        "crown_first_look_bridge_missing_or_unresolved",
        "crown_t30_bridge_missing_or_unresolved",
        "crown_fixture_not_listed",
        "crown_collector_unavailable",
        "crown_local_evidence_unavailable",
        "crown_native_t5_not_collected",
        "crown_exact_quote_journal_missing",
    }
    temporary_unavailable = [
        row for row in output.values()
        if row.get("status") == "UNAVAILABLE"
        and str(row.get("reason") or "") in temporary_reasons
    ]
    if (
        deadline is not None and capture_time < min(deadline, kickoff)
        and temporary_unavailable
    ):
        return {
            market: (
                {
                    **row,
                    "status": "PENDING",
                    "reason": "crown_counterpart_grace_pending",
                    "grace_deadline_at": grace_deadline_at,
                }
                if row.get("status") == "UNAVAILABLE"
                and str(row.get("reason") or "") in temporary_reasons
                else row
            )
            for market, row in output.items()
        }
    bridge["t5"] = {"at": captured_at, "markets": output}
    return output


def _hkjc_selected(current: dict[str, Any], market: str, watch: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    selected, reason = _selected(current, market, _time, fixture_kickoff=watch.get("kickoff"))
    if selected is None:
        return None, reason
    source = str(selected.get("source") or "").strip().lower()
    observed = _time(selected.get("observed_at"))
    stage_at = _time(current.get("ts") or current.get("source_snapshot_at"))
    if source not in {"hkjc_public_board", "hkjc-current-board"}:
        return None, "hkjc_signal_source_non_native_or_missing"
    if observed is None:
        return None, "hkjc_signal_timestamp_missing"
    if stage_at is None or observed > stage_at:
        return None, "hkjc_signal_post_decision_or_stage_timestamp_missing"
    if (stage_at - observed).total_seconds() > _freshness_seconds():
        return None, "hkjc_signal_quote_stale_at_t5"
    return selected, None


def _same_market_existing(rows: Iterable[dict[str, Any]], fixture: str, market: str, side: str, line: float, signature: str) -> bool:
    return any(
        str(row.get("match_id") or "") == fixture
        and str(row.get("code") or "") == market
        and str(row.get("side") or "").upper() == side
        and _num(row.get("line")) is not None and abs(float(_num(row.get("line"))) - line) <= 1e-8
        and str(row.get("frozen_condition_signature") or "") == signature
        for row in rows if isinstance(row, dict)
    )


def _active_existing_admission(
    ledger: dict[str, Any], admission: dict[str, Any], execution_odds: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read an already frozen Wilson version without changing its chain."""
    signature = str(admission.get("signature") or "")
    frozen = ((ledger.get("wilson_validation") or {}).get("conditions") or {}).get(signature)
    active = frozen.get("active_evidence") if isinstance(frozen, dict) else None
    if not isinstance(active, dict):
        return None, "active_wilson_condition_unavailable"
    arithmetic = admission_arithmetic(
        int(active.get("cumulative_hits", 0)), int(active.get("cumulative_decided", 0)),
        execution_odds,
    )
    if arithmetic is None:
        return None, "active_evidence_arithmetic_invalid"
    updated = copy.deepcopy(admission)
    updated["history"] = {**copy.deepcopy(admission.get("history") or {}),
                          "hits": int(active["cumulative_hits"]),
                          "decided": int(active["cumulative_decided"]),
                          "evidence_version": active.get("version"),
                          "evidence_hash": active.get("evidence_hash")}
    updated["arithmetic"] = arithmetic
    updated["evidence_version"] = active.get("version")
    updated["evidence_hash"] = active.get("evidence_hash")
    return updated, None


def evaluate_new_t5(
    ledger: dict[str, Any], watch: dict[str, Any], *, ranking: Iterable[dict[str, Any]] | None,
    now: str | None = None, decision_at: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Commit only exact, fresh cross-book simulations after a native Footbreak T-5."""
    now = now or _iso_now()
    ns = ensure_namespace(ledger, now=now)
    bilateral.ensure_namespace(ns)
    fixture = str(watch.get("match_id") or "")
    audit: list[dict[str, Any]] = []
    if not fixture or ranking is None:
        reason = "missing_fixture_or_frozen_ranking"
        audit.append({"market": "*", "status": "SKIPPED", "reason": reason})
        _append_audit(ns, now, fixture, audit[-1])
        return [], audit
    current_rows = [row for row in watch.get("stages") or [] if isinstance(row, dict) and row.get("stage") == DECISION_STAGE]
    current = current_rows[0] if len(current_rows) == 1 else None
    if current is None or not _native_t5(watch, current, _time):
        audit.append({"market": "*", "status": "SKIPPED", "reason": "not_first_native_pre_kickoff_t5"})
        _append_audit(ns, now, fixture, audit[-1]); return [], audit
    stage_at = _time(current.get("ts") or current.get("source_snapshot_at"))
    kickoff = _time(watch.get("kickoff") or current.get("kickoff"))
    decision_at = _time(decision_at or now)
    if (
        stage_at is None or kickoff is None or stage_at >= kickoff
        or decision_at is None or decision_at >= kickoff
    ):
        audit.append({"market": "*", "status": "SKIPPED", "reason": "t5_decision_timestamp_invalid"})
        _append_audit(ns, now, fixture, audit[-1]); return [], audit
    current_rows_for_match = [{"match_id": fixture, "stage": DECISION_STAGE, "kickoff": watch.get("kickoff"),
                               "predicted_at": current.get("ts"), "market_predictions": current.get("market_predictions") or []}]
    matched = match_upcoming(current_rows_for_match, list(ranking), system="footbreak", decision_stage=DECISION_STAGE).get(fixture, [])
    proposed: list[dict[str, Any]] = []
    for market in MARKETS:
        hkjc, reason = _hkjc_selected(current, market, watch)
        if hkjc is None:
            audit.append({"market": market, "status": "SKIPPED", "reason": reason}); continue
        side = str(hkjc.get("side") or "").upper(); line = _num(hkjc.get("line", hkjc.get("condition")))
        if line is None or not _valid_side(market, side):
            audit.append({"market": market, "status": "SKIPPED", "reason": "hkjc_signal_line_or_side_invalid"}); continue
        admissions, reason = matching_admissions("footbreak", market, hkjc, matched, stage_at=str(current.get("ts") or now))
        if not admissions:
            audit.append({"market": market, "status": "SKIPPED", "reason": reason}); continue
        quote, quote_reason = _crown_quote_for_verified_bridge(
            watch, market, side, line, stage_at, kickoff, ledger,
            decision_at=decision_at,
        )
        for admission in admissions:
            signature = str(admission["signature"])
            # The normal Footbreak Wilson evaluator owns freezing/versioning of
            # the historical condition.  A cross-book price must never create
            # or revise that chain on its own.
            frozen_conditions = (ledger.get("wilson_validation") or {}).get("conditions") or {}
            if not isinstance(frozen_conditions.get(signature), dict):
                audit.append({"market": market, "status": "SKIPPED",
                              "reason": "active_wilson_condition_unavailable"})
                continue
            frozen = frozen_conditions[signature]
            # The native HKJC quote fixes condition identity and its immutable
            # Wilson minimum.  The counterpart is collected independently and
            # is never allowed to prevent this durable fan-in record.
            native_adjusted, native_reason = _active_existing_admission(
                ledger, admission, _num(hkjc.get("odds")) or 0,
            )
            if native_adjusted is None:
                audit.append({"market": market, "status": "SKIPPED",
                              "reason": native_reason or "active_evidence_unavailable"})
                continue
            counterpart_status = "AVAILABLE" if quote is not None else "UNAVAILABLE"
            bilateral.append_counterpart_attempt(ns, {
                "system": "footbreak", "match_id": fixture, "market": market,
                "side": side, "line": line, "stage_at": stage_at.isoformat(),
                "counterpart_book": "crown", "counterpart_status": counterpart_status,
                "counterpart_reason": quote_reason,
                "counterpart_quote": quote.get("odds") if quote else None,
                "counterpart_observed_at": quote.get("observed_at") if quote else None,
            })
            native_odds = _num(hkjc.get("odds")) or 0.0
            counterpart_odds = _num(quote.get("odds")) if quote else None
            chosen_odds = max(native_odds, counterpart_odds or 0.0)
            minimum = _num((native_adjusted.get("arithmetic") or {}).get(
                "minimum_acceptable_odds_raw",
            ))
            qualifies = minimum is not None and chosen_odds + 1e-12 >= minimum
            if quote is None:
                decision = "COUNTERPART_UNAVAILABLE"
                chosen_book = "hkjc" if qualifies else None
            elif qualifies:
                decision = "PAPER_SIMULATION"
                chosen_book = "crown" if counterpart_odds and counterpart_odds > native_odds else "hkjc"
            else:
                decision = "NO_BET_LOW_ODDS"
                chosen_book = None
            did = bilateral.decision_id(
                system="footbreak", fixture=fixture, market=market, side=side,
                line=line, condition_signature=signature,
                evidence_version=native_adjusted.get("evidence_version"),
            )
            decision_row, _ = bilateral.persist_decision(ns, {
                "decision_id": did, "system": "footbreak", "fixture": fixture,
                "market": market, "side": side, "line": line,
                "condition_signature": signature,
                "condition_number": frozen.get("condition_number"),
                "evidence_version": native_adjusted.get("evidence_version"),
                "evidence_hash": native_adjusted.get("evidence_hash"),
                "signal_book": "hkjc", "signal_quote": native_odds,
                "signal_observed_at": hkjc.get("observed_at"),
                "counterpart_book": "crown", "counterpart_status": counterpart_status,
                "counterpart_quote": counterpart_odds,
                "counterpart_observed_at": quote.get("observed_at") if quote else None,
                "counterpart_reason": quote_reason,
                "minimum_odds": minimum, "chosen_execution_book": chosen_book,
                "chosen_execution_odds": chosen_odds if chosen_book else None,
                "decision": decision, "created_at": now,
                "stage_at": stage_at.isoformat(), "kickoff": watch.get("kickoff"),
                "league": watch.get("league"), "home": watch.get("home"), "away": watch.get("away"),
                "freshness_seconds": _freshness_seconds(),
            })
            audit.append({"market": market, "status": "DECISION",
                          "reason": decision, "decision_id": decision_row["decision_id"]})
            if quote is None:
                continue
            if _same_market_existing(ns["bets"], fixture, market, side, line, signature):
                audit.append({"market": market, "status": "SKIPPED", "reason": "idempotent_existing_exact_entry", "condition_number": (ledger.get("wilson_validation") or {}).get("conditions", {}).get(signature, {}).get("condition_number")})
                continue
            # The condition was selected by the native HKJC quote/tier above;
            # only Crown's exact execution price enters Wilson arithmetic.
            adjusted, reason = _active_existing_admission(ledger, admission, quote["odds"])
            if adjusted is None:
                audit.append({"market": market, "status": "SKIPPED", "reason": reason or "active_evidence_unavailable"}); continue
            if not adjusted["arithmetic"].get("passes"):
                audit.append({"market": market, "status": "MATCHED_NO_BET", "reason": "crown_wilson_gate_not_passed", "condition_number": (ledger.get("wilson_validation") or {}).get("conditions", {}).get(signature, {}).get("condition_number"), "wilson_admission": adjusted["arithmetic"]}); continue
            proposed.append({"market": market, "hkjc": hkjc, "quote": quote, "admission": adjusted, "line": line, "side": side, "decision_id": did})
            break
    created: list[dict[str, Any]] = []
    for item in proposed:
        if len([b for b in ns["bets"] if str(b.get("match_id") or "") == fixture]) >= FIXTURE_MARKET_CAP or sum(float(b.get("stake") or 0) for b in ns["bets"] if str(b.get("match_id") or "") == fixture) + FIXED_STAKE > FIXTURE_STAKE_CAP:
            audit.append({"market": item["market"], "status": "SKIPPED", "reason": "fixture_cap_reached"}); continue
        admission = item["admission"]; frozen = (ledger.get("wilson_validation") or {}).get("conditions", {}).get(admission["signature"], {})
        role, selected_line, label = _audit_selection(item["market"], item["hkjc"])
        bid = f"{fixture}|{item['market']}|{item['side']}|{item['line']:g}|{admission['signature']}|crown-execution-v1"
        if any(str(row.get("bet_id") or "") == bid for row in ns["bets"]):
            audit.append({"market": item["market"], "status": "SKIPPED", "reason": "idempotent_existing_exact_entry"}); continue
        bet = {
            "bet_id": bid, "portfolio": PORTFOLIO, "strategy": "footbreak-crown-execution-test-v1", "strategy_name": DISPLAY_NAME,
            "match_id": fixture, "league": watch.get("league"), "home": watch.get("home"), "away": watch.get("away"), "kickoff": watch.get("kickoff"), "fixture_id": watch.get("fixture_id"),
            "fixture_identity": item["quote"]["fixture_identity"], "code": item["market"], "market": item["market"], "market_label": MARKET_LABELS[item["market"]], "side": item["side"], "line": item["line"], "condition": item["line"], "selected_role": role, "selected_line": selected_line,
            "hkjc_signal_odds": _num(item["hkjc"].get("odds")), "hkjc_signal_source": item["hkjc"].get("source"), "hkjc_signal_observed_at": item["hkjc"].get("observed_at"),
            "crown_execution_odds": item["quote"]["odds"], "crown_execution_source": item["quote"]["source"], "crown_execution_observed_at": item["quote"]["observed_at"], "odds": item["quote"]["odds"],
            "stake": FIXED_STAKE, "stage": DECISION_STAGE, "status": "PENDING", "simulation_only": True, "real_betting_enabled": False, "first_native_pre_kickoff_t5": True, "created_at": now, "decision_at": now,
            "frozen_condition_signature": admission["signature"], "condition_number": frozen.get("condition_number"), "evidence_version": admission.get("evidence_version"), "evidence_hash": admission.get("evidence_hash"), "frozen_historical_evidence": copy.deepcopy(admission.get("history")), "wilson_admission": copy.deepcopy(admission["arithmetic"]),
            "bilateral_decision_id": item.get("decision_id"),
            "history": [{"ts": now, "stage": DECISION_STAGE, "action": "足破×皇冠模擬注建立", "reason": "原生 HKJC T-5 訊號、皇冠相同盤口新鮮報價及 Wilson 閘門通過"}],
        }
        ns["bets"].append(bet); created.append(bet)
        audit.append({"market": item["market"], "status": "CREATED", "reason": "exact_cross_book_execution_committed", "bet_id": bid, "condition_number": bet["condition_number"], "wilson_admission": bet["wilson_admission"]})
    for row in audit:
        _append_audit(ns, now, fixture, row)
    recompute(ledger)
    return created, audit


def recompute(ledger: dict[str, Any]) -> dict[str, Any]:
    ns = ensure_namespace(ledger)
    rows = ns["bets"]
    settled = [row for row in rows if row.get("status") in {"SETTLED", "VOIDED"}]
    pending = [row for row in rows if row.get("status") == "PENDING"]
    decided = [row for row in settled if row.get("result") != "Refunded"]
    hits = sum(row.get("result") in {"Won", "Half Won"} for row in decided)
    pnl = round(sum(_num(row.get("pnl")) or 0.0 for row in settled), 2)
    turnover = round(sum(_num(row.get("stake")) or 0.0 for row in settled), 2)
    stats = {"portfolio": PORTFOLIO, "display_name": DISPLAY_NAME, "starting_bankroll": STARTING_BANKROLL, "fixed_stake": FIXED_STAKE, "fixture_stake_cap": FIXTURE_STAKE_CAP, "fixture_market_cap": FIXTURE_MARKET_CAP, "n_pending": len(pending), "n_settled": len(settled), "n_decided": len(decided), "hits": hits, "hit_rate": hits / len(decided) if decided else None, "pushes": len(settled) - len(decided), "pnl": pnl, "turnover": turnover, "roi": pnl / turnover if turnover else None, "open_stake": round(sum(_num(row.get("stake")) or 0 for row in pending), 2), "cash": STARTING_BANKROLL + pnl - sum(_num(row.get("stake")) or 0 for row in pending), "equity": STARTING_BANKROLL + pnl,
             "res_counts": {name: sum(row.get("result") == name for row in settled) for name in ("Won", "Half Won", "Refunded", "Half Lost", "Lost")},
             "rejections": {}}
    for row in ns.get("audit") or []:
        if isinstance(row, dict) and row.get("status") != "CREATED":
            reason = str(row.get("reason") or "unknown")
            stats["rejections"][reason] = stats["rejections"].get(reason, 0) + 1
    ns["stats"] = stats
    return stats
