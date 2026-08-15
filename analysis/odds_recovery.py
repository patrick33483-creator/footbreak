"""Strict, append-only historical selected-odds recovery.

This module intentionally never writes prediction snapshots, ledgers, archives,
or histories.  It only writes an optional private sidecar after an explicit
``--apply`` invocation.  Team names are display-only and are never evidence.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import tempfile
import base64
import html
import re
import socket
import threading
import time
import unicodedata
import urllib.parse
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
PARSER_VERSION = "odds-recovery-provider-v3"
MARKETS = {"HDC", "HIL", "CHL"}
PRIMARY_EVIDENCE_QUALITIES = {"A", "B"}
AUDIT_ONLY_EVIDENCE_QUALITY = "C"
DEFAULT_EXACT_WINDOW_SECONDS = 60.0
DEFAULT_CROSSWALK_KICKOFF_TOLERANCE_SECONDS = 60.0
MAX_CROSSWALK_KICKOFF_TOLERANCE_SECONDS = 300.0
DEFAULT_FRESHNESS_SECONDS = {"T-30": 3600.0, "T-5": 900.0}
DEFAULT_PROVIDER_MAX_PAGES = 250
DEFAULT_PROVIDER_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_ARTIFACT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_ARTIFACT_MAX_FILES = 5000
DEFAULT_ALLOWLIST = {
    "footbreak": (
        "system/snapshots", "system/hk_snapshots.json", "system/prediction_history_archive.json",
        "system/sim_ledger.json", "hkjc-dashboard/data.json",
    ),
    "crown": (
        "crown/prediction_history.json", "crown/ledger.json", "crown/source_snapshots",
        "crown/dashboard/data.json",
    ),
}


def _decimal(value: Any, *, odds: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("missing_decimal")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("malformed_decimal") from None
    if not result.is_finite():
        raise ValueError("non_finite_decimal")
    if odds and result <= Decimal("1"):
        raise ValueError("invalid_decimal_odds")
    return result.normalize()


def canonical_line(value: Any) -> str:
    """Exact decimal value, canonicalized only for equivalent encodings.

    Split/quarter lines (e.g. ``0.0/+0.5``) are deliberately rejected: they
    are not a single exact numeric line and cannot safely be matched here.
    """
    return format(_decimal(value), "f").rstrip("0").rstrip(".") or "0"


def parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive_timestamp")
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing_timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Older Footbreak records use the documented HKT no-offset format.
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone(timedelta(hours=8))
            )
        except ValueError:
            raise ValueError("malformed_timestamp") from None
    if parsed.tzinfo is None:
        # ``datetime.fromisoformat`` also accepts the legacy space-separated
        # form, so attach HKT here only when the text exactly matches that
        # documented representation.  Other naive timestamps still fail.
        try:
            legacy = datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError:
            raise ValueError("naive_timestamp") from None
        if legacy.strftime("%Y-%m-%d %H:%M") != text:
            raise ValueError("naive_timestamp")
        parsed = legacy.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(timezone.utc)


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _id(row: dict[str, Any], system: str) -> str | None:
    """Return one persisted exact-ID namespace, never a display identity.

    Footbreak's history commonly calls this value ``match_id`` while the
    immutable learning DB calls the same persisted provider value
    ``fixture_id``.  They must therefore share one namespace.  Crown rows can
    carry extra bridge IDs, but the persisted match ID remains authoritative
    so history, ledger, and dashboard copies do not diverge.
    """
    candidates = (
        ("match_id", "persisted"),
        ("fixture_id", "persisted"),
        ("hkjc_match_id", "hkjc"),
        ("titan_match_id", "titan"),
        ("pinnapi_event_id", "pinnapi"),
    )
    for field, prefix in candidates:
        value = row.get(field)
        if value is not None and str(value).strip():
            return f"{prefix}:{str(value).strip()}"
    return None


def snapshot_identity(row: dict[str, Any], system: str) -> str | None:
    fixture = _id(row, system)
    stage = str(row.get("stage") or "").strip()
    predicted = row.get("predicted_at") or row.get("ts")
    if not fixture or not stage or not predicted:
        return None
    try:
        moment = parse_time(predicted).isoformat()
    except ValueError:
        return None
    # The identity deliberately uses only fields available in both the
    # immutable learning DB and published history.  A DB-only surrogate ID
    # would make a recovered price invisible to the read-only data-health
    # projection.  Exact provider fixture + stage + offset-aware generation
    # timestamp remains a stable snapshot identity; no team text participates.
    return f"{system}|{fixture}|{stage}|{moment}"


def _prediction_items(row: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for item in row.get("market_predictions") or []:
        if isinstance(item, dict):
            yield item
    for item in row.get("market_grades") or []:
        if isinstance(item, dict):
            yield item


def missing_selected_odds_count(rows: Iterable[dict[str, Any]]) -> int:
    """Count every missing selected market cell, including unrecoverable ones."""
    missing = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        for item in _prediction_items(row):
            if str(item.get("code") or "") not in MARKETS:
                continue
            if str(item.get("side") or "") not in {"H", "A", "L"}:
                continue
            try:
                _decimal(item.get("odds"), odds=True)
            except ValueError:
                missing += 1
    return missing


def prediction_targets(rows: Iterable[dict[str, Any]], system: str) -> tuple[list[dict[str, Any]], Counter]:
    targets: list[dict[str, Any]] = []
    reasons: Counter = Counter()
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = snapshot_identity(row, system)
        fixture = _id(row, system)
        try:
            predicted_at = parse_time(row.get("predicted_at") or row.get("ts"))
        except ValueError as exc:
            reasons[str(exc)] += 1
            continue
        if not ident or not fixture:
            reasons["missing_stable_fixture_identity"] += 1
            continue
        for item in _prediction_items(row):
            code = str(item.get("code") or "")
            side = str(item.get("side") or "")
            try:
                line = canonical_line(item.get("line") if item.get("line") is not None else item.get("condition"))
            except ValueError as exc:
                reasons[f"prediction_{exc}"] += 1
                continue
            if code not in MARKETS or side not in {"H", "A", "L"}:
                continue
            # Recovery is only needed when no valid selected decimal odds exists.
            try:
                _decimal(item.get("odds"), odds=True)
                continue
            except ValueError:
                pass
            key = (ident, code, line, side)
            if key in seen:
                continue
            seen.add(key)
            targets.append({"system": system, "snapshot_identity": ident, "fixture_identity": fixture,
                            "stage": str(row.get("stage") or ""), "market_code": code, "line": line,
                            "side": side, "predicted_at": predicted_at, "row": row})
    return targets, reasons


def normalized_fixture_text(value: Any) -> str | None:
    """Normalize display text without applying aliases or fuzzy matching.

    This is deliberately a representation normalization only: it handles
    Unicode width/case/diacritics and separators, but never maps nicknames,
    abbreviations, cities, or translated club names.  Such mappings would be
    an unreviewed identity guess and are prohibited for provider crosswalks.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = unicodedata.normalize("NFKD", value).casefold()
    normalized = "".join(
        char for char in normalized if unicodedata.category(char) != "Mn"
    )
    normalized = re.sub(r"[\W_]+", " ", normalized, flags=re.UNICODE)
    normalized = " ".join(normalized.split())
    return normalized or None


def _first_text(row: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def strict_fixture_identity(target: dict[str, Any]) -> dict[str, Any] | None:
    """Return the minimum provider crosswalk identity or fail closed.

    A persisted Footbreak/Crown ID identifies the local record, not a public
    provider event.  Cross-provider matching therefore additionally requires
    a timestamped kickoff and home/away teams.  League is compared whenever
    both the target and provider event publish one.
    """
    row = target.get("row")
    if not isinstance(row, dict):
        return None
    kickoff = _kickoff_for_target(target)
    home = _first_text(row, ("home", "home_team", "home_name", "team_home"))
    away = _first_text(row, ("away", "away_team", "away_name", "team_away"))
    normalized_home = normalized_fixture_text(home)
    normalized_away = normalized_fixture_text(away)
    if kickoff is None or normalized_home is None or normalized_away is None:
        return None
    league = _first_text(row, ("league", "league_name", "competition"))
    return {
        "kickoff": kickoff,
        "home": normalized_home,
        "away": normalized_away,
        "league": normalized_fixture_text(league),
    }


def compact_provider_target_rows(
    rows: Iterable[dict[str, Any]], system: str,
) -> list[dict[str, Any]]:
    """Export only unresolved targets and strict identity context to a runner.

    The caller writes this compact representation to a private temporary file.
    It contains no historical market payload, result, stake, model, or
    notification fields, and it cannot be used to mutate a source history.
    """
    compact: list[dict[str, Any]] = []
    kept_row_fields = (
        "match_id", "fixture_id", "hkjc_match_id", "titan_match_id",
        "pinnapi_event_id", "stage", "predicted_at", "ts", "kickoff_hkt",
        "kickoff", "start_time", "match_time", "home", "home_team",
        "home_name", "team_home", "away", "away_team", "away_name",
        "team_away", "league", "league_name", "competition",
    )
    targets, _ = prediction_targets(rows, system)
    for target in targets:
        row = target["row"]
        item = {field: row[field] for field in kept_row_fields if field in row}
        item["market_predictions"] = [{
            "code": target["market_code"], "line": target["line"],
            "side": target["side"], "odds": None,
        }]
        compact.append(item)
    return compact


def _quote(fixture: str, code: str, line: Any, side: str, odds: Any, observed_at: Any,
           source_kind: str, source_ref: str, stage: str | None = None,
           provider_evidence: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        return {"fixture_identity": fixture, "market_code": code, "line": canonical_line(line),
                "side": str(side), "odds": str(_decimal(odds, odds=True)),
                "observed_at": parse_time(observed_at), "source_kind": source_kind,
                "source_ref": source_ref, "stage": stage, "provider_evidence": provider_evidence}
    except ValueError:
        return None


def _iter_json_paths(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(p for p in path.rglob("*.json") if p.is_file())
        elif path.is_file() and path.suffix == ".json":
            yield path


def _row_quotes(row: dict[str, Any], system: str, source_ref: str) -> Iterable[dict[str, Any]]:
    # A source assigned to one system must not contribute a record that
    # explicitly declares the other system.  Records without the optional
    # field remain eligible only through the caller's per-system allowlist.
    declared_system = row.get("system")
    if declared_system is not None and str(declared_system).strip() != system:
        return
    fixture = _id(row, system)
    observed_at = row.get("saved_at") or row.get("observed_at") or row.get("ts") or row.get("predicted_at")
    if not fixture or not observed_at:
        return
    # Canonical persisted market predictions / historical stages.
    for item in row.get("market_predictions") or []:
        if isinstance(item, dict):
            quote = _quote(fixture, str(item.get("code") or ""), item.get("line", item.get("condition")),
                           item.get("side"), item.get("odds"), observed_at, "market_prediction", source_ref,
                           str(row.get("stage") or ""))
            if quote: yield quote
    # Footbreak saved raw HKJC board (same fixture ID only).
    board = row.get("hk_odds")
    if isinstance(board, dict):
        for code, lines in board.items():
            for entry in lines if isinstance(lines, list) else []:
                if not isinstance(entry, dict): continue
                for side, price in (entry.get("odds") or {}).items():
                    quote = _quote(fixture, str(code), entry.get("condition"), side, price, observed_at,
                                   "hk_odds", source_ref, str(row.get("stage") or ""))
                    if quote: yield quote
    # Compact Footbreak hk_snapshots fingerprint format.
    for key, price in (row.get("fingerprint") or {}).items():
        if not isinstance(key, str): continue
        parts = key.split("|")
        if len(parts) != 3: continue
        quote = _quote(fixture, parts[0], parts[1], parts[2], price, observed_at,
                       "hk_snapshot_fingerprint", source_ref, str(row.get("stage") or ""))
        if quote: yield quote


def _evidence_records(data: Any, *, source_name: str) -> Iterable[dict[str, Any]]:
    """Yield only documented snapshot/history/ledger records.

    Arbitrary nested dictionary keys are not fixture IDs.  The only key-based
    inference is for known ``watch`` and top-level snapshot/archive mappings,
    whose values demonstrably have a stage/snapshot structure.  Unknown
    containers are ignored rather than guessed.
    """
    def stage_records(record: dict[str, Any]) -> Iterable[dict[str, Any]]:
        yield record
        stages = record.get("stages")
        if isinstance(stages, list):
            for stage in stages:
                if isinstance(stage, dict):
                    yield {**record, **stage}

    def keyed_mapping(mapping: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for key, value in mapping.items():
            if not isinstance(value, dict) or not str(key).strip():
                continue
            # A map key may stand in only for a documented snapshot or watch
            # record, never an arbitrary nested object.
            if not any(name in value for name in ("hk_odds", "fingerprint", "stages")):
                continue
            record = value if _id(value, "") else {**value, "match_id": str(key)}
            yield from stage_records(record)

    if isinstance(data, list):
        for record in data:
            if isinstance(record, dict):
                yield from stage_records(record)
        return
    if not isinstance(data, dict):
        return

    # Single saved snapshot file.
    if any(key in data for key in ("hk_odds", "fingerprint", "market_predictions")):
        yield from stage_records(data)
        return

    # Published dashboard payload and persisted prediction history.
    history = data.get("prediction_history")
    if isinstance(history, dict) and isinstance(history.get("rows"), list):
        for record in history["rows"]:
            if isinstance(record, dict):
                yield from stage_records(record)
    if isinstance(data.get("rows"), list):
        for record in data["rows"]:
            if isinstance(record, dict):
                yield from stage_records(record)

    # Known ledger and dashboard containers.
    if isinstance(data.get("watch"), dict):
        yield from keyed_mapping(data["watch"])
    if isinstance(data.get("matches"), list):
        for record in data["matches"]:
            if isinstance(record, dict):
                yield from stage_records(record)

    # A top-level key is an exact fixture identifier only in these documented
    # persisted maps.  In particular, do not reinterpret arbitrary JSON object
    # keys in a source-snapshot directory as fixture identities.
    if source_name in {
        "hk_snapshots.json",
        "prediction_history_archive.json",
        "sim_ledger.json",
        "ledger.json",
    }:
        yield from keyed_mapping(data)


def evidence_from_paths(
    system: str,
    paths: Iterable[Path],
    root: Path,
    *,
    max_file_bytes: int = DEFAULT_ARTIFACT_MAX_BYTES,
    max_files: int = DEFAULT_ARTIFACT_MAX_FILES,
) -> tuple[list[dict[str, Any]], Counter, list[dict[str, str]]]:
    """Read only bounded JSON artifacts and return no payload text or paths."""
    quotes: list[dict[str, Any]] = []
    reasons: Counter = Counter()
    provenance: list[dict[str, str]] = []
    for number, path in enumerate(_iter_json_paths(paths), start=1):
        if number > max_files:
            reasons["evidence_file_budget_exhausted"] += 1
            break
        try:
            # Do not follow a symlink out of an operator-approved evidence
            # directory, and do not load an unexpectedly large raw payload.
            if path.is_symlink() or path.stat().st_size > max_file_bytes:
                reasons["unsafe_or_oversize_evidence_file"] += 1
                continue
            raw = path.read_bytes(); data = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            reasons["malformed_evidence_file"] += 1; continue
        source_ref = hashlib.sha256(raw).hexdigest()[:16]
        provenance.append({"source_hash": source_ref, "source_name": path.name})
        records = list(_evidence_records(data, source_name=path.name))
        if not records:
            reasons["unrecognized_evidence_structure"] += 1
        for record in records:
            for quote in _row_quotes(record, system, source_ref):
                quotes.append(quote)
    return quotes, reasons, provenance


def artifact_inventory(paths_by_system: dict[str, list[Path]]) -> dict[str, Any]:
    """Return a safe, aggregate manifest of candidate immutable artifacts.

    This deliberately reports neither paths nor payloads.  It is useful before
    provider access to show whether local/server evidence was actually
    available, while retaining the existing source-hash-only provenance in
    detailed recovery reports.
    """
    systems: dict[str, Any] = {}
    for system, paths in paths_by_system.items():
        files = 0
        bytes_total = 0
        rejected = Counter()
        names = Counter()
        for number, path in enumerate(_iter_json_paths(paths), start=1):
            if number > DEFAULT_ARTIFACT_MAX_FILES:
                rejected["file_budget_exhausted"] += 1
                break
            try:
                if path.is_symlink() or not path.is_file():
                    rejected["symlink_or_non_regular"] += 1
                    continue
                size = path.stat().st_size
            except OSError:
                rejected["unreadable"] += 1
                continue
            if size > DEFAULT_ARTIFACT_MAX_BYTES:
                rejected["oversize"] += 1
                continue
            files += 1
            bytes_total += size
            # Basenames are only classified so a public report cannot expose
            # a server directory structure or raw source naming convention.
            if path.name in {"hk_snapshots.json", "prediction_history_archive.json", "sim_ledger.json", "ledger.json"}:
                names[path.name] += 1
            elif path.name == "data.json":
                names["dashboard_data.json"] += 1
            else:
                names["snapshot_or_json_artifact"] += 1
        systems[system] = {
            "candidate_files": files,
            "candidate_bytes": bytes_total,
            "artifact_classes": dict(sorted(names.items())),
            "rejected": dict(sorted(rejected.items())),
        }
    return {"scan_policy": "bounded_regular_json_only", "max_files_per_system": DEFAULT_ARTIFACT_MAX_FILES, "systems": systems}


def _stage_cutoff(target: dict[str, Any]) -> tuple[datetime, bool, str]:
    """Return a no-lookahead stage cutoff and whether it is kickoff-grounded."""
    predicted = target["predicted_at"]
    kickoff = _kickoff_for_target(target)
    stage = target.get("stage")
    if kickoff is None:
        return predicted, False, "prediction_timestamp_fallback"
    if stage == "首預":
        return min(kickoff, predicted), True, "opening_earliest_pre_kickoff"
    if stage == "T-30":
        return min(kickoff - timedelta(minutes=30), predicted), True, "locf_cutoff"
    if stage == "T-5":
        return min(kickoff - timedelta(minutes=5), predicted), True, "locf_cutoff"
    return predicted, False, "prediction_timestamp_fallback"


def _evidence_quality(
    target: dict[str, Any],
    quote: dict[str, Any],
    cutoff: datetime,
    kickoff_grounded: bool,
    selection_method: str,
    *,
    exact_window_seconds: float,
    freshness_seconds: dict[str, float],
) -> tuple[str, float | None]:
    """Grade only timing quality; exact identity is always required separately."""
    if not kickoff_grounded:
        return AUDIT_ONLY_EVIDENCE_QUALITY, None
    if target.get("stage") == "首預":
        # The requirement for opening is the first valid pre-kickoff quote,
        # rather than closeness to kickoff.
        return "A", None
    age = round((cutoff - quote["observed_at"]).total_seconds(), 6)
    if age < 0:
        return AUDIT_ONLY_EVIDENCE_QUALITY, None
    if age <= max(0.0, exact_window_seconds):
        return "A", age
    ceiling = freshness_seconds.get(str(target.get("stage")))
    if ceiling is not None and age <= ceiling:
        return "B", age
    return AUDIT_ONLY_EVIDENCE_QUALITY, age


def choose_quote(
    target: dict[str, Any],
    evidence: Iterable[dict[str, Any]],
    *,
    exact_window_seconds: float = DEFAULT_EXACT_WINDOW_SECONDS,
    freshness_seconds: dict[str, float] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Select a stage-correct quote and decorate it with auditable quality."""
    exact = [q for q in evidence if q["fixture_identity"] == target["fixture_identity"]
             and q["market_code"] == target["market_code"] and q["line"] == target["line"]
             and q["side"] == target["side"]]
    if not exact:
        return None, "no_exact_fixture_market_line_side_evidence"
    cutoff, kickoff_grounded, method = _stage_cutoff(target)
    prior = [q for q in exact if q["observed_at"] <= cutoff]
    if not prior:
        return None, "only_post_prediction_evidence"
    selected = min(prior, key=lambda q: q["observed_at"]) if target.get("stage") == "首預" else max(prior, key=lambda q: q["observed_at"])
    quality, age = _evidence_quality(
        target, selected, cutoff, kickoff_grounded, method,
        exact_window_seconds=exact_window_seconds,
        freshness_seconds=freshness_seconds or DEFAULT_FRESHNESS_SECONDS,
    )
    return {
        **selected,
        "evidence_quality": quality,
        "evidence_target_timestamp": cutoff,
        "freshness_ceiling_seconds": (freshness_seconds or DEFAULT_FRESHNESS_SECONDS).get(str(target.get("stage"))),
        "selection_method": method,
        "selection_age_seconds": age,
    }, None


_ENTRY_BODY_FIELDS = (
    "system", "snapshot_identity", "market_code", "line", "side",
    "selected_odds", "observed_at", "evidence_source_kind",
    "evidence_source_hash", "evidence_age_seconds",
)
_ENTRY_OPTIONAL_FIELDS = {
    "provider_evidence", "evidence_quality", "evidence_target_timestamp",
    "freshness_ceiling_seconds", "selection_method",
}


def _validate_entry(entry: Any) -> dict[str, Any]:
    """Fail closed unless an entry is a canonical, self-authenticating quote."""
    if not isinstance(entry, dict) or not set(_ENTRY_BODY_FIELDS).issubset(entry) or "entry_hash" not in entry:
        raise ValueError("malformed_sidecar_entry")
    allowed = {*_ENTRY_BODY_FIELDS, "entry_hash", *_ENTRY_OPTIONAL_FIELDS}
    if set(entry) - allowed:
        raise ValueError("malformed_sidecar_entry")
    if any(not isinstance(entry.get(key), str) or not entry.get(key) for key in (
        "system", "snapshot_identity", "market_code", "line", "side",
        "selected_odds", "observed_at", "evidence_source_kind",
        "evidence_source_hash", "entry_hash",
    )):
        raise ValueError("malformed_sidecar_entry")
    if entry["system"] not in {"footbreak", "crown"}:
        raise ValueError("malformed_sidecar_entry")
    if not entry["snapshot_identity"].startswith(f"{entry['system']}|"):
        raise ValueError("malformed_sidecar_entry")
    if entry["market_code"] not in MARKETS or entry["side"] not in {"H", "A", "L"}:
        raise ValueError("malformed_sidecar_entry")
    if canonical_line(entry["line"]) != entry["line"]:
        raise ValueError("noncanonical_sidecar_line")
    _decimal(entry["selected_odds"], odds=True)
    parse_time(entry["observed_at"])
    age = entry["evidence_age_seconds"]
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or age < 0:
        raise ValueError("malformed_sidecar_entry")
    if "provider_evidence" in entry:
        _validate_provider_evidence(entry["provider_evidence"], entry)
    quality = entry.get("evidence_quality", AUDIT_ONLY_EVIDENCE_QUALITY)
    if quality not in {"A", "B", AUDIT_ONLY_EVIDENCE_QUALITY}:
        raise ValueError("malformed_evidence_quality")
    if "evidence_target_timestamp" in entry:
        if not isinstance(entry["evidence_target_timestamp"], str):
            raise ValueError("malformed_evidence_quality")
        parse_time(entry["evidence_target_timestamp"])
    if "freshness_ceiling_seconds" in entry:
        ceiling = entry["freshness_ceiling_seconds"]
        if ceiling is not None and (isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not math.isfinite(ceiling) or ceiling < 0):
            raise ValueError("malformed_evidence_quality")
    if "selection_method" in entry and entry["selection_method"] not in {
        "opening_earliest_pre_kickoff", "locf_cutoff", "prediction_timestamp_fallback",
    }:
        raise ValueError("malformed_evidence_quality")
    body = {key: entry[key] for key in _ENTRY_BODY_FIELDS}
    for key in _ENTRY_OPTIONAL_FIELDS:
        if key in entry:
            body[key] = entry[key]
    if entry["entry_hash"] != _sha(body):
        raise ValueError("sidecar_entry_hash_mismatch")
    return entry


def _sidecar(path: Path) -> dict[str, Any]:
    if not path.exists(): return {"schema_version": SCHEMA_VERSION, "entries": [], "audit": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list) or not isinstance(data.get("audit"), list):
        raise ValueError("malformed_sidecar")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported_sidecar_schema")
    for entry in data["entries"]:
        _validate_entry(entry)
    return data


def _entry_quality(entry: dict[str, Any]) -> str:
    """Read legacy entries conservatively without changing their bytes/hash."""
    explicit = entry.get("evidence_quality")
    if explicit in {"A", "B", AUDIT_ONLY_EVIDENCE_QUALITY}:
        return explicit
    # Version-1 entries had a verified exact identity and explicit age but no
    # grade.  Preserve only those inside the new default stage freshness
    # ceiling as B/LOCF; all other legacy evidence is audit-only.
    try:
        stage = str(entry["snapshot_identity"]).split("|", 3)[2]
        age = float(entry["evidence_age_seconds"])
    except (IndexError, KeyError, TypeError, ValueError):
        return AUDIT_ONLY_EVIDENCE_QUALITY
    ceiling = DEFAULT_FRESHNESS_SECONDS.get(stage)
    return "B" if ceiling is not None and 0 <= age <= ceiling else AUDIT_ONLY_EVIDENCE_QUALITY


def _entry(target: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    age = quote.get("selection_age_seconds")
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or age < 0:
        age = round((target["predicted_at"] - quote["observed_at"]).total_seconds(), 6)
    body = {"system": target["system"], "snapshot_identity": target["snapshot_identity"],
            "market_code": target["market_code"], "line": target["line"], "side": target["side"],
            "selected_odds": quote["odds"], "observed_at": quote["observed_at"].isoformat(),
            "evidence_source_kind": quote["source_kind"], "evidence_source_hash": quote["source_ref"],
            "evidence_age_seconds": age}
    if quote.get("provider_evidence") is not None:
        body["provider_evidence"] = quote["provider_evidence"]
    # Every newly generated candidate writes an explicit grade and timing
    # policy.  Grade-less legacy entries are handled conservatively by
    # _entry_quality without rewriting their hashes.
    body["evidence_quality"] = quote.get("evidence_quality", AUDIT_ONLY_EVIDENCE_QUALITY)
    target_timestamp = quote.get("evidence_target_timestamp")
    if isinstance(target_timestamp, datetime):
        body["evidence_target_timestamp"] = target_timestamp.isoformat()
    if "freshness_ceiling_seconds" in quote:
        body["freshness_ceiling_seconds"] = quote["freshness_ceiling_seconds"]
    if quote.get("selection_method"):
        body["selection_method"] = quote["selection_method"]
    return {**body, "entry_hash": _sha(body)}




def _validate_provider_evidence(value: Any, entry: dict[str, Any]) -> None:
    """Provider evidence is compact, non-display metadata kept in the private sidecar."""
    required = {"provider", "source_url_hash", "company_id", "native_odds_format", "native_price", "normalized_decimal", "quote_timestamp", "target_timestamp", "age_seconds", "parser_version"}
    requires_crosswalk = (
        isinstance(value, dict)
        and value.get("provider") in {"zgzcw_history", "tipsme_hkjc"}
        and value.get("parser_version") == PARSER_VERSION
    )
    expected = required | ({"crosswalk"} if requires_crosswalk else set())
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("malformed_provider_evidence")
    if value["provider"] not in {"titan_crown", "zgzcw_history", "tipsme_hkjc"}:
        raise ValueError("malformed_provider_evidence")
    if not all(isinstance(value[key], str) and value[key] for key in ("source_url_hash", "company_id", "native_odds_format", "native_price", "normalized_decimal", "quote_timestamp", "target_timestamp", "parser_version")):
        raise ValueError("malformed_provider_evidence")
    if value["parser_version"] not in {
        "odds-recovery-provider-v1", "odds-recovery-provider-v2", PARSER_VERSION,
    } or value["normalized_decimal"] != entry["selected_odds"]:
        raise ValueError("malformed_provider_evidence")
    parse_time(value["quote_timestamp"]); parse_time(value["target_timestamp"])
    try:
        native = _decimal(value["native_price"])
    except ValueError:
        raise ValueError("malformed_provider_evidence") from None
    normalized = _decimal(value["normalized_decimal"], odds=True)
    if value["native_odds_format"] == "hong_kong" and normalized != native + Decimal("1"):
        raise ValueError("malformed_provider_evidence")
    if value["native_odds_format"] == "decimal" and normalized != native:
        raise ValueError("malformed_provider_evidence")
    if value["native_odds_format"] not in {"hong_kong", "decimal"}:
        raise ValueError("malformed_provider_evidence")
    age = value["age_seconds"]
    if isinstance(age, bool) or not isinstance(age, (float, int)) or age < 0 or not math.isfinite(age):
        raise ValueError("malformed_provider_evidence")
    if requires_crosswalk:
        crosswalk = value["crosswalk"]
        required_crosswalk = {
            "provider", "source_url_hash", "event_id_hash",
            "kickoff_delta_seconds", "kickoff_tolerance_seconds",
            "league_compared", "method",
        }
        if not isinstance(crosswalk, dict) or set(crosswalk) != required_crosswalk:
            raise ValueError("malformed_provider_evidence")
        if crosswalk["provider"] not in {"zgzcw", "tipsme"}:
            raise ValueError("malformed_provider_evidence")
        if not isinstance(crosswalk["source_url_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", crosswalk["source_url_hash"]):
            raise ValueError("malformed_provider_evidence")
        if not isinstance(crosswalk["event_id_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", crosswalk["event_id_hash"]):
            raise ValueError("malformed_provider_evidence")
        delta = crosswalk["kickoff_delta_seconds"]
        tolerance = crosswalk["kickoff_tolerance_seconds"]
        if (isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(delta) or delta < 0
                or isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance)
                or not 0 <= tolerance <= MAX_CROSSWALK_KICKOFF_TOLERANCE_SECONDS or delta > tolerance):
            raise ValueError("malformed_provider_evidence")
        if not isinstance(crosswalk["league_compared"], bool) or crosswalk["method"] not in {
            "structured_event_index_exact_fixture_identity",
            "recorded_exact_fixture_identity",
        }:
            raise ValueError("malformed_provider_evidence")


def _kickoff_for_target(target: dict[str, Any]) -> datetime | None:
    row = target.get("row") or {}
    for key in ("kickoff_hkt", "kickoff", "start_time", "match_time"):
        try:
            return parse_time(row.get(key))
        except ValueError:
            pass
    return None


def _titan_id_for_target(target: dict[str, Any]) -> str | None:
    # Crown's persisted match_id is its stable Titan identity.  Do not use any
    # inferred name/time relationship to manufacture an ID.
    if target.get("system") != "crown":
        return None
    row = target.get("row") or {}
    value = row.get("titan_match_id") or row.get("match_id")
    return str(value).strip() if value is not None and str(value).strip() else None


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class PrivateResponseCache:
    """Private, reusable raw response cache.  It stores no public audit data."""
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.RLock()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def get(self, url: str) -> tuple[bytes, dict[str, Any]] | None:
        with self._lock:
            key = _url_hash(url); raw = self.root / f"{key}.raw"; meta = self.root / f"{key}.json"
            try:
                info = json.loads(meta.read_text(encoding="utf-8"))
                body = raw.read_bytes()
            except (OSError, json.JSONDecodeError):
                return None
            if not isinstance(info, dict) or info.get("url_hash") != key or info.get("http_status") != 200:
                return None
            return body, info

    def put(self, url: str, body: bytes, status: int) -> None:
        with self._lock:
            key = _url_hash(url); raw = self.root / f"{key}.raw"; meta = self.root / f"{key}.json"
            raw.write_bytes(body); os.chmod(raw, 0o600)
            meta.write_text(json.dumps({"url_hash": key, "http_status": status, "fetched_at": datetime.now(timezone.utc).isoformat()}, separators=(",", ":")), encoding="utf-8")
            os.chmod(meta, 0o600)


class ProviderFetcher:
    """Fetch each URL at most once while bounding starts and simultaneous I/O."""
    def __init__(self, cache: PrivateResponseCache, rate_per_second: float = 1.0,
                 retries: int = 2, timeout_seconds: float = 25.0, workers: int = 1,
                 max_pages: int = DEFAULT_PROVIDER_MAX_PAGES,
                 max_response_bytes: int = DEFAULT_PROVIDER_MAX_RESPONSE_BYTES,
                 cache_only: bool = False):
        self.cache = cache
        self.rate_per_second = max(0.1, rate_per_second)
        self.retries = max(0, retries)
        self.timeout_seconds = max(0.1, timeout_seconds)
        self.workers = max(1, min(32, int(workers)))
        self.max_pages = max(1, int(max_pages))
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self.cache_only = bool(cache_only)
        self._state_lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._next_start = 0.0
        self._results: dict[str, tuple[str | None, bool, str | None]] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._planned_pages = 0
        self.pages_fetched = 0
        self.cache_hits = 0
        self.http_failures = 0
        self.timeout_failures = 0

    def get(self, url: str) -> tuple[str | None, bool, str | None]:
        """Return a cached or single-flight fetch result for one URL."""
        with self._state_lock:
            known = self._results.get(url)
            if known is not None:
                return known
            event = self._inflight.get(url)
            owner = event is None
            if owner:
                event = threading.Event()
                self._inflight[url] = event
        if not owner:
            assert event is not None
            event.wait()
            with self._state_lock:
                return self._results[url]

        cached = self.cache.get(url)
        if cached:
            result = (cached[0].decode("gb18030", errors="replace"), True, None)
            with self._state_lock:
                self.cache_hits += 1
            return self._complete(url, result)
        if self.cache_only:
            # Production is allowed to consume an already-reviewed private
            # runner cache, but must never make a public-provider request.
            return self._complete(url, (None, False, "private_cache_miss"))
        result = self._fetch_uncached(url)
        return self._complete(url, result)

    def get_many(self, urls: Iterable[str]) -> dict[str, tuple[str | None, bool, str | None]]:
        """Fetch a deterministic unique URL set with controlled concurrency."""
        unique = sorted(set(urls))
        with self._state_lock:
            uncached = [url for url in unique if url not in self._results]
            available = max(0, self.max_pages - self._planned_pages)
            allowed_new = set(uncached[:available])
            self._planned_pages += len(allowed_new)
        allowed = [url for url in unique if url not in uncached or url in allowed_new]
        skipped = [url for url in unique if url in uncached and url not in allowed_new]
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="provider-fetch") as pool:
            futures = {url: pool.submit(self.get, url) for url in allowed}
            results = {url: futures[url].result() for url in allowed}
        results.update({url: (None, False, "request_budget_exhausted") for url in skipped})
        return {url: results[url] for url in unique}

    def _complete(self, url: str, result: tuple[str | None, bool, str | None]) -> tuple[str | None, bool, str | None]:
        with self._state_lock:
            self._results[url] = result
            event = self._inflight.pop(url)
            event.set()
        return result

    def _wait_for_start(self) -> None:
        """Serialize request starts; ongoing requests may still overlap."""
        with self._start_lock:
            pause = self._next_start - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            self._next_start = time.monotonic() + (1.0 / self.rate_per_second)

    @staticmethod
    def _is_timeout(exc: BaseException) -> bool:
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return True
        if not isinstance(exc, urllib.error.URLError):
            return False
        reason = str(exc.reason).lower()
        return isinstance(exc.reason, (TimeoutError, socket.timeout)) or "timeout" in reason or "timed out" in reason

    @staticmethod
    def _request_headers(url: str) -> dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/139.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        }
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname == "vip.titan007.com":
            query = urllib.parse.parse_qs(parsed.query)
            fixture_id = (query.get("id") or [""])[0]
            if fixture_id:
                parent = "OverDown_n.aspx" if parsed.path.lower().endswith("/overunder.aspx") else "AsianOdds_n.aspx"
                headers["Referer"] = f"https://vip.titan007.com/{parent}?id={fixture_id}&l=0"
        return headers

    def _fetch_uncached(self, url: str) -> tuple[str | None, bool, str | None]:
        last_error = "unknown"
        timed_out = False
        for attempt in range(self.retries + 1):
            self._wait_for_start()
            try:
                request = urllib.request.Request(url, headers=self._request_headers(url))
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    try:
                        body = response.read(self.max_response_bytes + 1)
                    except TypeError:  # Minimal offline test doubles may omit size.
                        body = response.read()
                    status = int(getattr(response, "status", 200) or 200)
                if status != 200:
                    raise OSError(f"http_{status}")
                if len(body) > self.max_response_bytes:
                    raise OSError("response_too_large")
                self.cache.put(url, body, status)
                with self._state_lock:
                    self.pages_fetched += 1
                return body.decode("gb18030", errors="replace"), False, None
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, ValueError) as exc:
                last_error = type(exc).__name__
                timed_out = timed_out or self._is_timeout(exc)
                if attempt < self.retries:
                    time.sleep(min(3.0, 0.5 * (2 ** attempt)))
        with self._state_lock:
            self.http_failures += 1
            if timed_out:
                self.timeout_failures += 1
        return None, False, last_error


_TAG = re.compile(r"<[^>]+>")
_TIME = re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?!\d)")
_NUMBER = re.compile(r"(?<![\d.])([+-]?\d+(?:\.\d+)?)(?![\d.])")

def _clean_html(value: str) -> str:
    return html.unescape(_TAG.sub(" ", value)).replace("\xa0", " ").strip()

def _infer_titan_year(month: int, day: int, hour: int, minute: int, kickoff: datetime) -> datetime | None:
    local = kickoff.astimezone(timezone(timedelta(hours=8)))
    candidates = []
    for year in (local.year - 1, local.year, local.year + 1):
        try: candidates.append(datetime(year, month, day, hour, minute, tzinfo=local.tzinfo))
        except ValueError: continue
    if not candidates: return None
    # Around New Year, nearest calendar instance is the only defensible choice.
    chosen = min(candidates, key=lambda item: abs((item - local).total_seconds()))
    return chosen.astimezone(timezone.utc)

def _numeric_cell(value: str) -> Decimal | None:
    found = _NUMBER.fullmatch(value.strip())
    if not found: return None
    try: return _decimal(found.group(1))
    except ValueError: return None

def _titan_line(raw: str, market: str) -> str | None:
    value = _numeric_cell(raw)
    if value is None and market == "HDC":
        text = re.sub(r"\s+", "", raw.strip()).replace("讓", "让").replace("兩", "两")
        receiving = text.startswith("受让")
        if receiving:
            text = text[2:]
        chinese_lines = {
            "平手": Decimal("0"),
            "平手/半球": Decimal("0.25"),
            "半球": Decimal("0.5"),
            "半球/一球": Decimal("0.75"),
            "一球": Decimal("1"),
            "一球/球半": Decimal("1.25"),
            "球半": Decimal("1.5"),
            "球半/两球": Decimal("1.75"),
            "两球": Decimal("2"),
            "两球/两球半": Decimal("2.25"),
            "两球半": Decimal("2.5"),
            "两球半/三球": Decimal("2.75"),
            "三球": Decimal("3"),
            "三球/三球半": Decimal("3.25"),
            "三球半": Decimal("3.5"),
            "三球半/四球": Decimal("3.75"),
            "四球": Decimal("4"),
        }
        magnitude = chinese_lines.get(text)
        if magnitude is None:
            return None
        value = magnitude if receiving else -magnitude
    if value is None:
        return None
    if market == "HDC" and _numeric_cell(raw) is not None and not raw.strip().startswith(("-", "+")):
        value = -value
    if market == "HIL": value = abs(value)
    try: return canonical_line(value)
    except ValueError: return None

def parse_titan_change_rows(source: str, market: str, kickoff: datetime, company_id: str = "3") -> list[dict[str, Any]]:
    """Parse only company-ID 3, pre-match (即) movement ticks from Titan HTML."""
    if market not in {"HDC", "HIL"}: return []
    rows = []
    for raw_row in re.findall(r"<tr\b([^>]*)>([\s\S]*?)</tr>", source, re.I):
        attrs, body = raw_row
        id_match = re.search(r"(?:data-(?:company-)?id|companyID)\s*=\s*['\"]?(\d+)", attrs, re.I)
        if id_match and id_match.group(1) != company_id: continue
        cells = [_clean_html(cell) for cell in re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", body, re.I)]
        whole = " ".join(cells)
        tm = _TIME.search(whole)
        if not tm or "即" not in cells: continue
        observed = _infer_titan_year(*map(int, tm.groups()), kickoff)
        if not observed: continue
        before_time = cells[:next((i for i, text in enumerate(cells) if _TIME.search(text)), len(cells))]
        numeric = [(i, _numeric_cell(text)) for i, text in enumerate(before_time)]
        numeric = [(i, value) for i, value in numeric if value is not None]
        textual_lines = [
            (i, _titan_line(text, market))
            for i, text in enumerate(before_time)
            if market == "HDC" and _numeric_cell(text) is None and _titan_line(text, market) is not None
        ]
        if textual_lines:
            line_index, line = textual_lines[-1]
            before_prices = [(i, value) for i, value in numeric if i < line_index and value > 0]
            after_prices = [(i, value) for i, value in numeric if i > line_index and value > 0]
            if not before_prices or not after_prices:
                continue
            home = before_prices[-1]
            away = after_prices[0]
        else:
            if len(numeric) < 3:
                continue
            # Numeric provider rows are price / line / price.  Take the last
            # triple before timestamp so sequence/table index cells cannot win.
            home, line_raw, away = numeric[-3:]
            line = _titan_line(before_time[line_raw[0]], market)
        if not line or home[1] <= 0 or away[1] <= 0: continue
        rows.append({"line": line, "H": home[1], "A" if market == "HDC" else "L": away[1], "observed_at": observed, "status": "即", "company_id": company_id})
    return sorted(rows, key=lambda row: row["observed_at"])


def _provider_quote(
    target: dict[str, Any],
    tick: dict[str, Any],
    target_time: datetime,
    *,
    provider: str,
    source_url: str,
    company_id: str,
    native_odds_format: str = "hong_kong",
    exact_window_seconds: float = DEFAULT_EXACT_WINDOW_SECONDS,
    freshness_seconds: dict[str, float] | None = None,
    selection_method: str = "locf_cutoff",
    crosswalk: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        native = _decimal(tick["price"])
        decimal = native + Decimal("1") if native_odds_format == "hong_kong" else native
        candidate = _quote(target["fixture_identity"], target["market_code"], target["line"], target["side"], decimal, tick["observed_at"], f"provider_{provider}", _url_hash(source_url), target["stage"], {
            "provider": provider, "source_url_hash": _url_hash(source_url), "company_id": company_id,
            "native_odds_format": native_odds_format, "native_price": str(native), "normalized_decimal": str(decimal.normalize()),
            "quote_timestamp": tick["observed_at"].isoformat(), "target_timestamp": target_time.isoformat(),
            "age_seconds": round((target_time - tick["observed_at"]).total_seconds(), 6), "parser_version": PARSER_VERSION,
            **({"crosswalk": crosswalk} if crosswalk is not None else {}),
        })
        if candidate is None:
            return None
        _, kickoff_grounded, _ = _stage_cutoff(target)
        quality, age = _evidence_quality(
            target, candidate, target_time, kickoff_grounded, selection_method,
            exact_window_seconds=exact_window_seconds,
            freshness_seconds=freshness_seconds or DEFAULT_FRESHNESS_SECONDS,
        )
        return {
            **candidate,
            "evidence_quality": quality,
            "evidence_target_timestamp": target_time,
            "freshness_ceiling_seconds": (freshness_seconds or DEFAULT_FRESHNESS_SECONDS).get(target["stage"]),
            "selection_method": selection_method,
            "selection_age_seconds": age,
        }
    except (KeyError, ValueError): return None


def titan_candidate(
    target: dict[str, Any],
    source: str,
    url: str,
    *,
    exact_window_seconds: float = DEFAULT_EXACT_WINDOW_SECONDS,
    freshness_seconds: dict[str, float] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    kickoff = _kickoff_for_target(target)
    if not kickoff: return None, "kickoff_context_unavailable"
    if target["market_code"] not in {"HDC", "HIL"}: return None, "provider_market_unsupported"
    ticks = parse_titan_change_rows(source, target["market_code"], kickoff)
    exact = [dict(tick, price=tick.get(target["side"])) for tick in ticks if tick["line"] == target["line"] and target["side"] in tick]
    if not exact: return None, "no_exact_fixture_market_line_side_evidence"
    if target["stage"] == "首預":
        target_time = min(kickoff, target["predicted_at"])
        eligible = [tick for tick in exact if tick["observed_at"] <= target_time]
        selected = min(eligible, key=lambda row: row["observed_at"]) if eligible else None
    elif target["stage"] in {"T-30", "T-5"}:
        nominal_time = kickoff - timedelta(minutes=30 if target["stage"] == "T-30" else 5)
        target_time = min(nominal_time, target["predicted_at"])
        eligible = [tick for tick in exact if tick["observed_at"] <= target_time]
        selected = max(eligible, key=lambda row: row["observed_at"]) if eligible else None
    else: return None, "unsupported_stage"
    if not selected: return None, "no_qualifying_prior_quote"
    return _provider_quote(
        target, selected, target_time, provider="titan_crown", source_url=url, company_id="3",
        exact_window_seconds=exact_window_seconds, freshness_seconds=freshness_seconds,
        selection_method="opening_earliest_pre_kickoff" if target["stage"] == "首預" else "locf_cutoff",
    ), None


def _provider_event_id(node: dict[str, Any], provider: str) -> str | None:
    fields = (
        ("zgzcw_match_id", "match_id", "event_id", "id")
        if provider == "zgzcw" else
        ("tipsme_match_id", "match_id", "event_id", "id")
    )
    for field in fields:
        value = node.get(field)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def parse_provider_event_index(source: str, provider: str) -> list[dict[str, Any]]:
    """Read self-describing structured event records from a public index page.

    No DOM position, page-title text, current price, or free-text team search
    is used.  An event must carry its own provider ID, kickoff, and both teams;
    malformed/incomplete nodes are simply unavailable for a crosswalk.
    """
    if provider not in {"zgzcw", "tipsme"}:
        return []
    decoder = json.JSONDecoder()
    blobs: list[Any] = []
    for match in re.finditer(r"[\[{]", source):
        try:
            value, _ = decoder.raw_decode(source[match.start():])
            blobs.append(value)
        except json.JSONDecodeError:
            continue
    events: list[dict[str, Any]] = []
    for node in _walk_json(blobs):
        event_id = _provider_event_id(node, provider)
        home = _first_text(node, ("home", "home_team", "homeName", "team_home"))
        away = _first_text(node, ("away", "away_team", "awayName", "team_away"))
        kickoff = _tipsme_time(
            node.get("kickoff", node.get("start_time", node.get("startTime",
            node.get("match_time", node.get("time"))))))
        if not event_id or home is None or away is None or kickoff is None:
            continue
        normalized_home = normalized_fixture_text(home)
        normalized_away = normalized_fixture_text(away)
        if normalized_home is None or normalized_away is None:
            continue
        events.append({
            "event_id": event_id, "kickoff": kickoff, "home": normalized_home,
            "away": normalized_away, "league": normalized_fixture_text(
                _first_text(node, ("league", "league_name", "competition", "leagueName"))
            ),
        })
    # Duplicated serialized data is common in hydration scripts.  A repeated
    # identical record does not create ambiguity; differing records do.
    unique = {
        (row["event_id"], row["kickoff"].isoformat(), row["home"], row["away"], row["league"]): row
        for row in events
    }
    return sorted(unique.values(), key=lambda row: (
        row["event_id"], row["kickoff"].isoformat(), row["home"], row["away"],
    ))


def exact_event_crosswalk(
    target: dict[str, Any], events: Iterable[dict[str, Any]], provider: str,
    source_url: str, *, kickoff_tolerance_seconds: float = DEFAULT_CROSSWALK_KICKOFF_TOLERANCE_SECONDS,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return one exact public event mapping, never a best/closest match."""
    identity = strict_fixture_identity(target)
    if identity is None:
        return None, "strict_fixture_identity_unavailable"
    if provider not in {"zgzcw", "tipsme"}:
        return None, "crosswalk_provider_unsupported"
    matches: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("home") != identity["home"] or event.get("away") != identity["away"]:
            continue
        event_kickoff = event.get("kickoff")
        if not isinstance(event_kickoff, datetime):
            continue
        delta = abs((event_kickoff - identity["kickoff"]).total_seconds())
        if delta > kickoff_tolerance_seconds:
            continue
        # League is a required equality check only when both sources provide
        # one.  Missing league never grants a fuzzy equivalence.
        if identity["league"] and event.get("league") and event["league"] != identity["league"]:
            continue
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            continue
        matches.append({**event, "kickoff_delta_seconds": round(delta, 6)})
    unique_ids = {row["event_id"] for row in matches}
    if not matches:
        return None, "no_exact_provider_fixture_identity"
    if len(unique_ids) != 1:
        return None, "ambiguous_provider_fixture_identity"
    chosen = matches[0]
    return {
        "provider": provider,
        "provider_match_id": chosen["event_id"],
        "source_url_hash": _url_hash(source_url),
        "event_id_hash": _sha(chosen["event_id"]),
        "kickoff_delta_seconds": chosen["kickoff_delta_seconds"],
        "kickoff_tolerance_seconds": kickoff_tolerance_seconds,
        "league_compared": bool(identity["league"] and chosen.get("league")),
        "method": "structured_event_index_exact_fixture_identity",
    }, None


def _bridge_crosswalk(
    target: dict[str, Any], provider: str, bridge: dict[str, Any],
    *,
    kickoff_tolerance_seconds: float = DEFAULT_CROSSWALK_KICKOFF_TOLERANCE_SECONDS,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a previously recorded provider ID using its fixture evidence."""
    if bridge.get("provider_id_evidence") is not True:
        return None, "provider_id_evidence_unavailable"
    event_id = _provider_event_id(bridge, provider)
    kickoff = _tipsme_time(bridge.get("kickoff", bridge.get("start_time", bridge.get("match_time"))))
    home = normalized_fixture_text(_first_text(bridge, ("home", "home_team", "home_name")))
    away = normalized_fixture_text(_first_text(bridge, ("away", "away_team", "away_name")))
    if not event_id or kickoff is None or home is None or away is None:
        return None, "crosswalk_fixture_evidence_unavailable"
    event = {
        "event_id": event_id, "kickoff": kickoff, "home": home, "away": away,
        "league": normalized_fixture_text(_first_text(bridge, ("league", "league_name", "competition"))),
    }
    crosswalk, reason = exact_event_crosswalk(
        target, [event], provider, "recorded-private-crosswalk",
        kickoff_tolerance_seconds=kickoff_tolerance_seconds,
    )
    if crosswalk:
        crosswalk["method"] = "recorded_exact_fixture_identity"
    return crosswalk, reason


def tipsme_crosswalk(target: dict[str, Any]) -> dict[str, Any] | None:
    row = target.get("row") or {}
    hkjc = str(row.get("hkjc_match_id") or "").strip()
    direct = str(row.get("tipsme_match_id") or "").strip()
    bridge = row.get("tipsme_crosswalk")
    # A coincidental pair of IDs is not a crosswalk.  Older direct fields are
    # accepted only after they include the same strict fixture proof required
    # of a page-built crosswalk.
    if direct and str(row.get("tipsme_hkjc_match_id") or "").strip() == hkjc and hkjc:
        direct_bridge = {
            **row, "tipsme_match_id": direct,
            "provider_id_evidence": row.get("tipsme_provider_id_evidence"),
        }
        verified, _ = _bridge_crosswalk(target, "tipsme", direct_bridge)
        return verified
    if isinstance(bridge, dict) and hkjc and str(bridge.get("hkjc_match_id") or "").strip() == hkjc:
        verified, _ = _bridge_crosswalk(target, "tipsme", bridge)
        return verified
    return None

def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values(): yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value: yield from _walk_json(child)

def _tipsme_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try: return datetime.fromtimestamp(float(value) / (1000 if float(value) > 10_000_000_000 else 1), tz=timezone.utc)
        except (ValueError, OSError): return None
    try: return parse_time(value)
    except ValueError: return None

def parse_tipsme_chart_ticks(source: str, market: str) -> list[dict[str, Any]]:
    """Extract only explicit timestamped embedded chart points; never page current price."""
    decoder = json.JSONDecoder(); blobs = []
    for match in re.finditer(r"[\[{]", source):
        try:
            value, _ = decoder.raw_decode(source[match.start():]); blobs.append(value)
        except json.JSONDecodeError: continue
    mapping = {"HDC": "hdp", "HIL": "hilo", "CHL": "corner"}
    out = []
    for node in _walk_json(blobs):
        tag = str(node.get("market") or node.get("marketType") or node.get("type") or "").lower()
        if tag and mapping[market] not in tag: continue
        observed = _tipsme_time(node.get("timestamp", node.get("time", node.get("x"))))
        line = node.get("line", node.get("handicap", node.get("condition")))
        if not observed or line is None: continue
        names = (("H", "home", "homeOdds"), ("A" if market == "HDC" else "L", "away" if market == "HDC" else "under", "awayOdds" if market == "HDC" else "underOdds"))
        for side, first, second in names:
            price = node.get(first, node.get(second))
            try:
                price = _decimal(price)
                normalized_line = canonical_line(line)
            except ValueError: continue
            native_format = str(node.get("odds_format") or node.get("oddsFormat") or "").strip().lower()
            if native_format not in {"hong_kong", "decimal"}:
                continue
            out.append({"line": normalized_line, "side": side, "price": price, "observed_at": observed,
                        "opening": bool(node.get("opening") or node.get("isOpening") or node.get("initial") or node.get("label") == "初"),
                        "native_odds_format": native_format})
    return out

def tipsme_candidate(
    target: dict[str, Any],
    source: str,
    url: str,
    *,
    crosswalk: dict[str, Any] | None = None,
    exact_window_seconds: float = DEFAULT_EXACT_WINDOW_SECONDS,
    freshness_seconds: dict[str, float] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    kickoff = _kickoff_for_target(target)
    if not kickoff: return None, "kickoff_context_unavailable"
    if crosswalk is None:
        return None, "exact_fixture_crosswalk_unavailable"
    ticks = [tick for tick in parse_tipsme_chart_ticks(source, target["market_code"]) if tick["line"] == target["line"] and tick["side"] == target["side"]]
    if target["stage"] == "首預":
        # A chart's earliest verified pre-kickoff tick is the only defensible
        # opening reconstruction; a presentational "opening" label is not
        # required and must not be guessed when absent.
        at = min(kickoff, target["predicted_at"])
        eligible = [tick for tick in ticks if tick["observed_at"] <= at]
        selected = min(eligible, key=lambda row: row["observed_at"]) if eligible else None
    elif target["stage"] in {"T-30", "T-5"}:
        at = min(
            kickoff - timedelta(minutes=30 if target["stage"] == "T-30" else 5),
            target["predicted_at"],
        )
        eligible = [tick for tick in ticks if tick["observed_at"] <= at]
        selected = max(eligible, key=lambda row: row["observed_at"]) if eligible else None
    else: return None, "unsupported_stage"
    if not selected: return None, "no_qualifying_prior_quote"
    # Corners are permitted only through this exact crosswalk plus an explicit
    # timestamped chart tick.  Missing format/line/side/timestamp has already
    # failed closed in the parser.
    return _provider_quote(
        target, selected, at, provider="tipsme_hkjc", source_url=url, company_id="hkjc",
        native_odds_format=selected["native_odds_format"],
        exact_window_seconds=exact_window_seconds, freshness_seconds=freshness_seconds,
        selection_method="opening_earliest_pre_kickoff" if target["stage"] == "首預" else "locf_cutoff",
        crosswalk={key: value for key, value in crosswalk.items() if key not in {"bookmaker_id", "provider_match_id"}},
    ), None


def zgzcw_crosswalk(target: dict[str, Any]) -> dict[str, Any] | None:
    """Return only a fixture-proven source/bookmaker mapping for ZGZCW."""
    row = target.get("row") or {}
    bridge = row.get("zgzcw_crosswalk")
    if not isinstance(bridge, dict):
        return None
    source_id = str(bridge.get("zgzcw_match_id") or row.get("zgzcw_match_id") or "").strip()
    bookmaker_id = str(bridge.get("bookmaker_id") or row.get("zgzcw_bookmaker_id") or "").strip()
    if not source_id or not bookmaker_id:
        return None
    anchor = _titan_id_for_target(target) or str(row.get("hkjc_match_id") or "").strip()
    if not anchor or str(bridge.get("source_anchor_id") or "").strip() != anchor:
        return None
    verified, _ = _bridge_crosswalk(
        target, "zgzcw", {**bridge, "zgzcw_match_id": source_id},
    )
    if verified is None:
        return None
    return {**verified, "bookmaker_id": bookmaker_id}


def parse_zgzcw_history_ticks(source: str, market: str, bookmaker_id: str) -> list[dict[str, Any]]:
    """Parse only self-identifying, timestamped, exact-bookmaker ZGZCW ticks.

    A configurable endpoint is deliberately required.  The public URL shape
    was not established in the repository handoffs, so this parser does not
    invent one or infer an event by team name.  It accepts only embedded JSON
    nodes that name market, bookmaker, line, price format and timestamp.
    """
    if market not in {"HDC", "HIL"}:
        return []
    decoder = json.JSONDecoder()
    blobs: list[Any] = []
    for match in re.finditer(r"[\[{]", source):
        try:
            value, _ = decoder.raw_decode(source[match.start():])
            blobs.append(value)
        except json.JSONDecodeError:
            continue
    market_tags = {"HDC": {"hdc", "handicap", "asian_handicap"}, "HIL": {"hil", "overunder", "over_under", "total"}}
    result: list[dict[str, Any]] = []
    for node in _walk_json(blobs):
        tag = str(node.get("market") or node.get("marketType") or node.get("type") or "").strip().lower()
        if tag not in market_tags[market]:
            continue
        tick_bookmaker = str(node.get("bookmaker_id") or node.get("company_id") or node.get("bookmakerId") or "").strip()
        if tick_bookmaker != bookmaker_id:
            continue
        observed = _tipsme_time(node.get("timestamp", node.get("time", node.get("changed_at"))))
        native_format = str(node.get("odds_format") or node.get("oddsFormat") or "").strip().lower()
        line = node.get("line", node.get("handicap", node.get("condition")))
        if observed is None or line is None or native_format not in {"hong_kong", "decimal"}:
            continue
        try:
            normalized_line = canonical_line(line)
        except ValueError:
            continue
        sides = (
            ("H", node.get("home", node.get("homeOdds"))),
            ("A" if market == "HDC" else "L", node.get("away" if market == "HDC" else "under",
                                                          node.get("awayOdds" if market == "HDC" else "underOdds"))),
        )
        for side, price in sides:
            try:
                result.append({
                    "line": normalized_line, "side": side, "price": _decimal(price),
                    "observed_at": observed, "native_odds_format": native_format,
                    "opening": bool(node.get("opening") or node.get("isOpening") or node.get("initial")),
                    "bookmaker_id": tick_bookmaker,
                })
            except ValueError:
                continue
    deduplicated = {
        (tick["line"], tick["side"], str(tick["price"]), tick["observed_at"].isoformat(), tick["bookmaker_id"]): tick
        for tick in result
    }
    return sorted(deduplicated.values(), key=lambda tick: tick["observed_at"])


def zgzcw_candidate(
    target: dict[str, Any],
    source: str,
    url: str,
    bookmaker_id: str,
    *,
    crosswalk: dict[str, Any] | None = None,
    exact_window_seconds: float = DEFAULT_EXACT_WINDOW_SECONDS,
    freshness_seconds: dict[str, float] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    kickoff = _kickoff_for_target(target)
    if kickoff is None:
        return None, "kickoff_context_unavailable"
    if crosswalk is None:
        return None, "exact_fixture_crosswalk_unavailable"
    if target["market_code"] not in {"HDC", "HIL"}:
        return None, "provider_market_unsupported"
    ticks = [
        tick for tick in parse_zgzcw_history_ticks(source, target["market_code"], bookmaker_id)
        if tick["line"] == target["line"] and tick["side"] == target["side"] and tick["bookmaker_id"] == bookmaker_id
    ]
    if not ticks:
        return None, "no_exact_fixture_market_line_side_bookmaker_evidence"
    if target["stage"] == "首預":
        at = min(kickoff, target["predicted_at"])
        eligible = [tick for tick in ticks if tick["observed_at"] <= at]
        selected = min(eligible, key=lambda tick: tick["observed_at"]) if eligible else None
    elif target["stage"] in {"T-30", "T-5"}:
        at = min(kickoff - timedelta(minutes=30 if target["stage"] == "T-30" else 5), target["predicted_at"])
        eligible = [tick for tick in ticks if tick["observed_at"] <= at]
        selected = max(eligible, key=lambda tick: tick["observed_at"]) if eligible else None
    else:
        return None, "unsupported_stage"
    if selected is None:
        return None, "no_qualifying_prior_quote"
    return _provider_quote(
        target, selected, at, provider="zgzcw_history", source_url=url, company_id=bookmaker_id,
        native_odds_format=selected["native_odds_format"],
        exact_window_seconds=exact_window_seconds, freshness_seconds=freshness_seconds,
        selection_method="opening_earliest_pre_kickoff" if target["stage"] == "首預" else "locf_cutoff",
        crosswalk={key: value for key, value in crosswalk.items() if key not in {"bookmaker_id", "provider_match_id"}},
    ), None


def _event_index_url(template: str, target: dict[str, Any]) -> str | None:
    identity = strict_fixture_identity(target)
    if identity is None:
        return None
    date = identity["kickoff"].astimezone(timezone(timedelta(hours=8))).date().isoformat()
    try:
        url = template.format(KICKOFF_DATE=date, kickoff_date=date, DATE=date, date=date)
    except (KeyError, ValueError):
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or url == template:
        return None
    return url


def _quote_url(template: str, match_id: str, market: str, bookmaker_id: str = "") -> str | None:
    try:
        url = template.format(
            MATCH_ID=match_id, match_id=match_id, BOOKMAKER_ID=bookmaker_id,
            bookmaker_id=bookmaker_id, market=market,
        )
    except (KeyError, ValueError):
        return None
    parsed = urllib.parse.urlparse(url)
    return url if parsed.scheme == "https" and parsed.netloc else None


def provider_entries(targets: list[dict[str, Any]], *, providers: set[str], cache_dir: Path,
                     rate_per_second: float, retries: int, timeout_seconds: float = 25.0,
                     workers: int = 1, tipsme_url_template: str | None = None,
                     zgzcw_url_template: str | None = None,
                     tipsme_event_url_template: str | None = None,
                     zgzcw_event_url_template: str | None = None,
                     zgzcw_bookmaker_id: str | None = None,
                     kickoff_tolerance_seconds: float = DEFAULT_CROSSWALK_KICKOFF_TOLERANCE_SECONDS,
                     cache_only: bool = False,
                     exact_window_seconds: float = DEFAULT_EXACT_WINDOW_SECONDS,
                     freshness_seconds: dict[str, float] | None = None,
                     max_pages: int = DEFAULT_PROVIDER_MAX_PAGES) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve pages in batches, but evaluate each target in original order.

    The ladder is local evidence (handled by report), Titan company-ID 3,
    ZGZCW exact-crosswalk histories, then Tipsme.  A later source is requested
    only when every preceding rung failed for that exact target.
    """
    fetcher = ProviderFetcher(
        PrivateResponseCache(cache_dir), rate_per_second, retries,
        timeout_seconds=timeout_seconds, workers=workers, max_pages=max_pages,
        cache_only=cache_only,
    )
    entries: list[dict[str, Any]] = []; recovered = Counter(); failures = Counter(); attempted = set()
    crosswalks = Counter()
    plans: list[dict[str, Any]] = []
    for target in targets:
        plan: dict[str, Any] = {"target": target, "candidate": None, "reason": None}
        if "titan" in providers:
            titan_id = _titan_id_for_target(target)
            if titan_id and target["market_code"] in {"HDC", "HIL"}:
                attempted.add(("titan", titan_id))
                page = "handicap.aspx" if target["market_code"] == "HDC" else "overunder.aspx"
                plan["titan_url"] = f"https://vip.titan007.com/changeDetail/{page}?id={titan_id}&companyID=3&l=0"
        plans.append(plan)

    titan_pages = fetcher.get_many(plan["titan_url"] for plan in plans if "titan_url" in plan)
    for plan in plans:
        url = plan.get("titan_url")
        if not url:
            continue
        source, _cached, err = titan_pages[url]
        if err:
            plan["reason"] = "http_failure"
        elif source is not None:
            plan["candidate"], plan["reason"] = titan_candidate(
                plan["target"], source, url, exact_window_seconds=exact_window_seconds,
                freshness_seconds=freshness_seconds,
            )

    # Resolve provider IDs first.  A recorded bridge still has to prove the
    # same kickoff/team/league identity; otherwise an HTTPS date-index page is
    # parsed and must yield exactly one structured provider event.
    for provider, event_template in (("zgzcw", zgzcw_event_url_template), ("tipsme", tipsme_event_url_template)):
        for plan in plans:
            if plan["candidate"] is not None or provider not in providers:
                continue
            target = plan["target"]
            eligible = (
                target["market_code"] in {"HDC", "HIL"} if provider == "zgzcw"
                else target["system"] == "footbreak" or (
                    target["system"] == "crown" and target["market_code"] == "CHL"
                )
            )
            if not eligible:
                continue
            direct = zgzcw_crosswalk(target) if provider == "zgzcw" else tipsme_crosswalk(target)
            if direct is not None:
                if provider == "zgzcw":
                    plan["zgzcw_crosswalk"] = direct
                    plan["zgzcw_bookmaker_id"] = str(direct["bookmaker_id"])
                else:
                    plan["tipsme_crosswalk"] = direct
                crosswalks[f"{provider}_recorded_exact"] += 1
                continue
            event_url = _event_index_url(event_template, target) if event_template else None
            if event_url is None:
                plan["reason"] = (
                    "strict_fixture_identity_unavailable"
                    if strict_fixture_identity(target) is None
                    else f"{provider}_event_index_unconfigured"
                )
                continue
            plan[f"{provider}_event_url"] = event_url

        event_pages = fetcher.get_many(
            plan[f"{provider}_event_url"] for plan in plans
            if f"{provider}_event_url" in plan
        )
        for plan in plans:
            if plan["candidate"] is not None or f"{provider}_event_url" not in plan:
                continue
            url = plan[f"{provider}_event_url"]
            source, _cached, err = event_pages[url]
            if err:
                plan["reason"] = "private_cache_miss" if err == "private_cache_miss" else "http_failure"
                continue
            crosswalk, reason = exact_event_crosswalk(
                plan["target"], parse_provider_event_index(source or "", provider), provider, url,
                kickoff_tolerance_seconds=kickoff_tolerance_seconds,
            )
            if crosswalk is None:
                plan["reason"] = reason
                continue
            if provider == "zgzcw":
                bookmaker = str(zgzcw_bookmaker_id or "").strip()
                if not bookmaker:
                    plan["reason"] = "zgzcw_bookmaker_unconfigured"
                    continue
                plan["zgzcw_crosswalk"] = {**crosswalk, "bookmaker_id": bookmaker}
                plan["zgzcw_bookmaker_id"] = bookmaker
            else:
                plan["tipsme_crosswalk"] = crosswalk
            crosswalks[f"{provider}_structured_exact"] += 1

    for plan in plans:
        if plan["candidate"] is not None or "zgzcw" not in providers:
            continue
        target = plan["target"]
        crosswalk = plan.get("zgzcw_crosswalk")
        if target["market_code"] not in {"HDC", "HIL"}:
            continue
        if not crosswalk:
            continue
        if not zgzcw_url_template:
            plan["reason"] = "zgzcw_public_url_unconfigured"
        else:
            match_id = crosswalk["provider_match_id"]
            bookmaker_id = plan["zgzcw_bookmaker_id"]
            url = _quote_url(zgzcw_url_template, match_id, {"HDC": "hdp", "HIL": "hilo"}[target["market_code"]], bookmaker_id)
            if url is None:
                plan["reason"] = "zgzcw_public_url_unconfigured"
                continue
            attempted.add(("zgzcw", match_id, bookmaker_id))
            plan["zgzcw_url"] = url

    zgzcw_pages = fetcher.get_many(plan["zgzcw_url"] for plan in plans if "zgzcw_url" in plan)
    for plan in plans:
        if plan["candidate"] is not None:
            continue
        url = plan.get("zgzcw_url")
        if not url:
            continue
        source, _cached, err = zgzcw_pages[url]
        if err:
            plan["reason"] = "private_cache_miss" if err == "private_cache_miss" else "http_failure"
        elif source is not None:
            plan["candidate"], plan["reason"] = zgzcw_candidate(
                plan["target"], source, url, plan["zgzcw_bookmaker_id"],
                exact_window_seconds=exact_window_seconds, freshness_seconds=freshness_seconds,
                crosswalk=plan["zgzcw_crosswalk"],
            )

    for plan in plans:
        if plan["candidate"] is not None or "tipsme" not in providers:
            continue
        target = plan["target"]
        crosswalk = plan.get("tipsme_crosswalk")
        eligible = target["system"] == "footbreak" or (
            target["system"] == "crown" and target["market_code"] == "CHL"
        )
        if not eligible or not crosswalk:
            continue
        if not tipsme_url_template:
            plan["reason"] = "tipsme_public_url_unconfigured"
        else:
            match_id = crosswalk["provider_match_id"]
            url = _quote_url(tipsme_url_template, match_id, {"HDC": "hdp", "HIL": "hilo", "CHL": "corner"}[target["market_code"]])
            if url is None:
                plan["reason"] = "tipsme_public_url_unconfigured"
                continue
            attempted.add(("tipsme", match_id))
            plan["tipsme_url"] = url

    tipsme_pages = fetcher.get_many(plan["tipsme_url"] for plan in plans if "tipsme_url" in plan)
    for plan in plans:
        if plan["candidate"] is not None:
            continue
        url = plan.get("tipsme_url")
        if not url:
            continue
        source, _cached, err = tipsme_pages[url]
        if err:
            plan["reason"] = "private_cache_miss" if err == "private_cache_miss" else "http_failure"
        elif source is not None:
            plan["candidate"], plan["reason"] = tipsme_candidate(
                plan["target"], source, url, exact_window_seconds=exact_window_seconds,
                freshness_seconds=freshness_seconds,
                crosswalk=plan["tipsme_crosswalk"],
            )

    for plan in plans:
        target = plan["target"]; candidate = plan["candidate"]; reason = plan["reason"]
        if candidate:
            entry = _entry(target, candidate)
            _validate_entry(entry)
            entries.append(entry)
            recovered[f"{target['stage']}|{target['market_code']}|{candidate['source_kind']}"] += 1
        elif reason:
            failures[reason] += 1
    ages = [entry["evidence_age_seconds"] for entry in entries]
    bands = Counter("under_5m" if age < 300 else "5m_to_30m" if age < 1800 else "30m_to_6h" if age < 21600 else "over_6h" for age in ages)
    return entries, {
        "fixtures_attempted": len(attempted),
        "pages_fetched": fetcher.pages_fetched,
        "cache_hits": fetcher.cache_hits,
        "http_failures": fetcher.http_failures,
        "timeout_failures": fetcher.timeout_failures,
        "cache_only": cache_only,
        "request_budget": max_pages,
        "crosswalks_verified": dict(sorted(crosswalks.items())),
        "targets_recovered_by_stage_market_source": dict(sorted(recovered.items())),
        "stale_age_bands": dict(sorted(bands.items())),
        "parser_failures": dict(sorted((key, value) for key, value in failures.items() if key not in {
            "http_failure", "private_cache_miss", "exact_id_crosswalk_unavailable",
            "zgzcw_exact_id_crosswalk_unavailable", "no_exact_provider_fixture_identity",
            "ambiguous_provider_fixture_identity", "strict_fixture_identity_unavailable",
        })),
        "no_qualifying_prior_quote": failures.get("no_qualifying_prior_quote", 0),
        "exact_id_crosswalk_unavailable": (
            failures.get("exact_id_crosswalk_unavailable", 0)
            + failures.get("zgzcw_exact_id_crosswalk_unavailable", 0)
        ),
        "strict_fixture_identity_unavailable": failures.get("strict_fixture_identity_unavailable", 0),
        "no_exact_provider_fixture_identity": failures.get("no_exact_provider_fixture_identity", 0),
        "ambiguous_provider_fixture_identity": failures.get("ambiguous_provider_fixture_identity", 0),
        "private_cache_miss": failures.get("private_cache_miss", 0),
    }

def report(
    rows_by_system: dict[str, list[dict[str, Any]]],
    paths_by_system: dict[str, list[Path]],
    *,
    provider_options: dict[str, Any] | None = None,
    exact_window_seconds: float = DEFAULT_EXACT_WINDOW_SECONDS,
    freshness_seconds: dict[str, float] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    all_entries: list[dict[str, Any]] = []
    systems: dict[str, Any] = {}
    freshness_seconds = freshness_seconds or DEFAULT_FRESHNESS_SECONDS
    for system, rows in rows_by_system.items():
        targets, target_reasons = prediction_targets(rows, system)
        evidence, evidence_reasons, provenance = evidence_from_paths(system, paths_by_system.get(system, []), Path("."))
        recovered: Counter = Counter(); unresolved = target_reasons + evidence_reasons; ages: list[float] = []; qualities: Counter = Counter()
        candidates: list[dict[str, Any]] = []
        for target in targets:
            quote, reason = choose_quote(
                target, evidence, exact_window_seconds=exact_window_seconds,
                freshness_seconds=freshness_seconds,
            )
            if not quote:
                unresolved[reason or "unknown"] += 1; continue
            candidate = _entry(target, quote); candidates.append(candidate)
            # C evidence is surfaced only as aggregate audit coverage.  It is
            # intentionally not persisted into the overlay, so it cannot
            # conflict with or block a later exact/fresh provider recovery.
            if candidate["evidence_quality"] in PRIMARY_EVIDENCE_QUALITIES:
                all_entries.append(candidate)
            recovered[f"{target['stage']}|{target['market_code']}|{quote['source_kind']}"] += 1
            ages.append(candidate["evidence_age_seconds"])
            qualities[candidate["evidence_quality"]] += 1
        systems[system] = {"missing_total": missing_selected_odds_count(rows),
                           "strict_identity_target_total": len(targets),
                           "recovered_candidate_count": len(candidates),
                           "primary_eligible_candidate_count": sum(qualities[q] for q in PRIMARY_EVIDENCE_QUALITIES),
                           "audit_only_candidate_count": qualities[AUDIT_ONLY_EVIDENCE_QUALITY],
                           "evidence_quality_grades": dict(sorted(qualities.items())),
                           "recovered_by_stage_market_source": dict(sorted(recovered.items())),
                           "unrecoverable_reasons": dict(sorted(unresolved.items())),
                           "evidence_age_seconds": {"count": len(ages), "min": min(ages) if ages else None,
                               "max": max(ages) if ages else None, "median": sorted(ages)[len(ages)//2] if ages else None},
                           "evidence_files": len(provenance), "provenance": provenance}
    output = {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry_run",
        "recovery_ladder": ["local_immutable_evidence", "titan_company_id_3", "zgzcw_exact_history", "tipsme_exact_crosswalk"],
        "quality_policy": {
            "A": "exact_window_or_verified_opening",
            "B": "locf_within_freshness_ceiling",
            "C": "approximate_audit_only_never_primary_statistics",
            "exact_window_seconds": exact_window_seconds,
            "freshness_seconds": freshness_seconds,
        },
        "artifact_inventory": artifact_inventory(paths_by_system),
        "systems": systems,
    }
    if provider_options:
        unresolved_targets = []
        indexed = {(entry["system"], entry["snapshot_identity"], entry["market_code"], entry["line"], entry["side"]) for entry in all_entries}
        for system, rows in rows_by_system.items():
            for target in prediction_targets(rows, system)[0]:
                key = (system, target["snapshot_identity"], target["market_code"], target["line"], target["side"])
                if key not in indexed: unresolved_targets.append(target)
        supplied, provider_audit = provider_entries(
            unresolved_targets,
            exact_window_seconds=exact_window_seconds,
            freshness_seconds=freshness_seconds,
            **provider_options,
        )
        all_entries.extend(entry for entry in supplied if entry.get("evidence_quality") in PRIMARY_EVIDENCE_QUALITIES)
        output["provider_assisted"] = provider_audit
        for entry in supplied:
            system = entry["system"]; systems[system]["recovered_candidate_count"] += 1
            if entry.get("evidence_quality") in PRIMARY_EVIDENCE_QUALITIES:
                systems[system]["primary_eligible_candidate_count"] += 1
            else:
                systems[system]["audit_only_candidate_count"] += 1
            systems[system]["evidence_quality_grades"][entry.get("evidence_quality", AUDIT_ONLY_EVIDENCE_QUALITY)] = systems[system]["evidence_quality_grades"].get(entry.get("evidence_quality", AUDIT_ONLY_EVIDENCE_QUALITY), 0) + 1
            try:
                stage = entry["snapshot_identity"].split("|", 3)[2]
            except (AttributeError, IndexError):
                stage = "provider"
            marker = f"{stage}|{entry['market_code']}|{entry['evidence_source_kind']}"
            systems[system]["recovered_by_stage_market_source"][marker] = systems[system]["recovered_by_stage_market_source"].get(marker, 0) + 1
    return output, all_entries


def apply(path: Path, entries: list[dict[str, Any]]) -> dict[str, int]:
    # Validate all candidates before creating even a private directory.  This
    # makes malformed/non-finite inputs a fail-closed no-op.
    for entry in entries:
        _validate_entry(entry)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    current = _sidecar(path)
    existing = {(e.get("system"), e.get("snapshot_identity"), e.get("market_code"), e.get("line"), e.get("side")): e for e in current["entries"] if isinstance(e, dict)}
    added = 0; already = 0
    for entry in entries:
        key = tuple(entry[k] for k in ("system", "snapshot_identity", "market_code", "line", "side"))
        old = existing.get(key)
        if old:
            # Parser/schema metadata may become richer while the immutable
            # quote itself remains byte-for-byte equivalent. Treat an exact
            # price + observation-time match as idempotently present; never
            # replace the original evidence entry or relax a real quote
            # conflict.
            same_quote = (
                old.get("selected_odds") == entry.get("selected_odds")
                and old.get("observed_at") == entry.get("observed_at")
            )
            if old.get("entry_hash") != entry.get("entry_hash") and not same_quote:
                raise ValueError("conflicting_recovery_evidence")
            already += 1; continue
        current["entries"].append(entry); existing[key] = entry; added += 1
    current["audit"].append({
        "action": "apply", "entry_count": added, "entries_hash": _sha(entries),
        "applied_at": datetime.now(timezone.utc).isoformat(),
    })
    if path.exists():
        backup = path.with_name(f"{path.name}.backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        backup.write_bytes(path.read_bytes()); os.chmod(backup, 0o600)
    fd, temp = tempfile.mkstemp(prefix=".odds-recovery.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(current, out, ensure_ascii=False, separators=(",", ":")); out.flush(); os.fsync(out.fileno())
        os.chmod(temp, 0o600); os.replace(temp, path); os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp): os.unlink(temp)
    return {"added": added, "already_present": already}


def sidecar_comparison(path: Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Return aggregate-only candidate/sidecar conflict diagnostics.

    This deliberately exposes no fixture IDs, source URLs, prices, or hashes.
    It lets an operator distinguish harmless metadata evolution from a genuine
    quote conflict before deciding whether any existing immutable entry needs
    manual review.
    """
    for entry in entries:
        _validate_entry(entry)
    current = _sidecar(path)
    existing = {
        (e.get("system"), e.get("snapshot_identity"), e.get("market_code"), e.get("line"), e.get("side")): e
        for e in current["entries"] if isinstance(e, dict)
    }
    counts: Counter = Counter()
    new_key_breakdown: Counter = Counter()
    for entry in entries:
        key = tuple(entry[k] for k in ("system", "snapshot_identity", "market_code", "line", "side"))
        old = existing.get(key)
        if old is None:
            counts["new_key"] += 1
            try:
                stage = str(entry["snapshot_identity"]).split("|", 3)[2]
            except (KeyError, IndexError):
                stage = "unknown"
            marker = f"{entry.get('system') or 'unknown'}|{stage}|{entry.get('market_code') or 'unknown'}"
            new_key_breakdown[marker] += 1
        elif old.get("entry_hash") == entry.get("entry_hash"):
            counts["exact_hash_match"] += 1
        elif (
            old.get("selected_odds") == entry.get("selected_odds")
            and old.get("observed_at") == entry.get("observed_at")
        ):
            counts["same_quote_metadata_changed"] += 1
        elif old.get("selected_odds") == entry.get("selected_odds"):
            counts["same_price_different_observation"] += 1
        else:
            counts["different_price_conflict"] += 1
    counts["candidate_total"] = len(entries)
    counts["existing_entry_total"] = len(existing)
    result: dict[str, Any] = dict(sorted(counts.items()))
    result["new_key_by_system_stage_market"] = dict(sorted(new_key_breakdown.items()))
    return result


def overlay_rows(rows: list[dict[str, Any]], system: str, sidecar_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return a decorated copy; callers never mutate their raw payload."""
    if sidecar_path is not None:
        path = Path(sidecar_path)
    elif (
        os.environ.get("ODDS_RECOVERY_ENABLED", "").strip().lower()
        in {"1", "true", "yes", "on"}
        and os.environ.get("ODDS_RECOVERY_SIDECAR")
    ):
        path = Path(os.environ["ODDS_RECOVERY_SIDECAR"])
    else:
        path = None
    if not path or not path.exists(): return copy.deepcopy(rows)
    try: data = _sidecar(path)
    except (ValueError, OSError, json.JSONDecodeError): return copy.deepcopy(rows)
    index = {(e.get("system"), e.get("snapshot_identity"), e.get("market_code"), e.get("line"), e.get("side")): e for e in data["entries"] if isinstance(e, dict)}
    result = copy.deepcopy(rows)
    for row in result:
        ident = snapshot_identity(row, system)
        if not ident: continue
        for items in (row.get("market_predictions") or [], row.get("market_grades") or []):
            for collection in items if isinstance(items, list) else []:
                if not isinstance(collection, dict):
                    continue
                try: line = canonical_line(collection.get("line") if collection.get("line") is not None else collection.get("condition"))
                except ValueError: continue
                entry = index.get((system, ident, collection.get("code"), line, collection.get("side")))
                # Grade C is deliberately retained only for audit/conflict
                # review.  It can never populate odds buckets or primary
                # >=1.70/<1.70 statistics through the public projections.
                quality = _entry_quality(entry) if entry else AUDIT_ONLY_EVIDENCE_QUALITY
                if entry and quality in PRIMARY_EVIDENCE_QUALITIES:
                    collection["odds"] = float(entry["selected_odds"])
                    collection["recovery_provenance"] = "historical_exact_prior"
                    collection["recovery_evidence_quality"] = quality
    return result


def _load_rows(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict): value = value.get("rows") or (value.get("prediction_history") or {}).get("rows") or []
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def main() -> None:
    parser = argparse.ArgumentParser(description="Strict historical odds recovery; local dry-run by default")
    parser.add_argument("--footbreak-history", type=Path, required=True)
    parser.add_argument("--crown-history", type=Path, required=True)
    parser.add_argument("--allow", action="append", default=[], help="system=relative-or-absolute-path (repeatable)")
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="append local-only candidates to private sidecar")
    parser.add_argument(
        "--apply-confirmation",
        default="",
        help="required exact phrase before any sidecar write",
    )
    provider_mode = parser.add_mutually_exclusive_group()
    provider_mode.add_argument("--provider-audit", action="store_true", help="explicitly enable network provider audit; no sidecar write")
    provider_mode.add_argument("--provider-apply", action="store_true", help="explicitly enable network provider audit and append candidates")
    parser.add_argument("--provider", action="append", choices=("titan", "zgzcw", "tipsme"), default=[], help="network provider to use (repeatable)")
    parser.add_argument("--provider-cache", type=Path, help="private 0700/0600 raw-response cache directory")
    parser.add_argument("--provider-rate", type=float, default=1.0, help="maximum provider requests per second (default 1)")
    parser.add_argument("--provider-retries", type=int, default=2)
    parser.add_argument("--provider-timeout", type=float, default=25.0, help="per-request timeout seconds (default 25)")
    parser.add_argument("--provider-workers", type=int, default=1, help="concurrent provider page workers, capped at 32 (default 1)")
    parser.add_argument("--provider-max-pages", type=int, default=DEFAULT_PROVIDER_MAX_PAGES, help="total unique provider-page budget (default 250)")
    parser.add_argument("--provider-cache-only", action="store_true", help="read private runner cache only; never contact a provider")
    parser.add_argument("--exact-window-seconds", type=float, default=DEFAULT_EXACT_WINDOW_SECONDS, help="A-grade cutoff window for T-30/T-5")
    parser.add_argument("--freshness-t30-seconds", type=float, default=DEFAULT_FRESHNESS_SECONDS["T-30"], help="B-grade LOCF ceiling for T-30")
    parser.add_argument("--freshness-t5-seconds", type=float, default=DEFAULT_FRESHNESS_SECONDS["T-5"], help="B-grade LOCF ceiling for T-5")
    parser.add_argument("--crosswalk-kickoff-tolerance-seconds", type=float, default=DEFAULT_CROSSWALK_KICKOFF_TOLERANCE_SECONDS, help="exact public fixture crosswalk kickoff tolerance, 0-300 seconds")
    parser.add_argument("--tipsme-event-url-template", help="verified HTTPS Tipsme event-index URL template; requires {KICKOFF_DATE}")
    parser.add_argument("--zgzcw-event-url-template", help="verified HTTPS ZGZCW event-index URL template; requires {KICKOFF_DATE}")
    parser.add_argument("--tipsme-url-template", help="verified HTTPS Tipsme timestamp-history URL template; uses {MATCH_ID} and {market}")
    parser.add_argument("--zgzcw-url-template", help="verified HTTPS ZGZCW timestamp-history URL template; uses {MATCH_ID}, {BOOKMAKER_ID}, and {market}")
    parser.add_argument("--zgzcw-bookmaker-id", help="exact ZGZCW bookmaker ID for a page-built crosswalk")
    args = parser.parse_args()
    if args.provider and not (args.provider_audit or args.provider_apply): parser.error("--provider requires --provider-audit or --provider-apply")
    if (args.provider_audit or args.provider_apply) and not args.provider: parser.error("provider mode requires at least one --provider")
    if (args.provider_audit or args.provider_apply) and not args.provider_cache: parser.error("provider mode requires --provider-cache private directory")
    if args.exact_window_seconds < 0 or args.freshness_t30_seconds < 0 or args.freshness_t5_seconds < 0 or args.provider_max_pages < 1:
        parser.error("freshness, exact-window, and provider page budgets must be non-negative (pages >= 1)")
    if not 0 <= args.crosswalk_kickoff_tolerance_seconds <= MAX_CROSSWALK_KICKOFF_TOLERANCE_SECONDS:
        parser.error("crosswalk kickoff tolerance must be between 0 and 300 seconds")
    if (args.apply or args.provider_apply) and args.apply_confirmation != "APPLY_HISTORICAL_ODDS_RECOVERY":
        parser.error("--apply and --provider-apply require --apply-confirmation APPLY_HISTORICAL_ODDS_RECOVERY")
    paths = {system: [] for system in ("footbreak", "crown")}
    for raw in args.allow:
        system, sep, value = raw.partition("=")
        if system not in paths or not sep: parser.error("--allow must be footbreak=PATH or crown=PATH")
        paths[system].append(Path(value))
    if not args.allow:
        root = Path.cwd(); paths = {s: [root / p for p in values] for s, values in DEFAULT_ALLOWLIST.items()}
    provider_options = None
    if args.provider_audit or args.provider_apply:
        provider_options = {
            "providers": set(args.provider), "cache_dir": args.provider_cache,
            "rate_per_second": args.provider_rate, "retries": args.provider_retries,
            "timeout_seconds": args.provider_timeout, "workers": args.provider_workers,
            "tipsme_url_template": args.tipsme_url_template, "zgzcw_url_template": args.zgzcw_url_template,
            "tipsme_event_url_template": args.tipsme_event_url_template,
            "zgzcw_event_url_template": args.zgzcw_event_url_template,
            "zgzcw_bookmaker_id": args.zgzcw_bookmaker_id,
            "kickoff_tolerance_seconds": args.crosswalk_kickoff_tolerance_seconds,
            "cache_only": args.provider_cache_only,
            "max_pages": args.provider_max_pages,
        }
    freshness = {"T-30": args.freshness_t30_seconds, "T-5": args.freshness_t5_seconds}
    result, entries = report(
        {"footbreak": _load_rows(args.footbreak_history), "crown": _load_rows(args.crown_history)},
        paths, provider_options=provider_options, exact_window_seconds=args.exact_window_seconds,
        freshness_seconds=freshness,
    )
    result["sidecar_comparison"] = sidecar_comparison(args.sidecar, entries)
    if args.apply or args.provider_apply:
        result["mode"] = "provider_apply" if args.provider_apply else "apply"; result["apply"] = apply(args.sidecar, entries)
    elif args.provider_audit:
        result["mode"] = "provider_audit"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))

if __name__ == "__main__": main()
