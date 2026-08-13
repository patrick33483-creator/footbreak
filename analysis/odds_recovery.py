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
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
MARKETS = {"HDC", "HIL", "CHL"}
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
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing_timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Older Footbreak records use the documented HKT no-offset format.
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError:
            raise ValueError("malformed_timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("naive_timestamp")
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


def _quote(fixture: str, code: str, line: Any, side: str, odds: Any, observed_at: Any,
           source_kind: str, source_ref: str, stage: str | None = None) -> dict[str, Any] | None:
    try:
        return {"fixture_identity": fixture, "market_code": code, "line": canonical_line(line),
                "side": str(side), "odds": str(_decimal(odds, odds=True)),
                "observed_at": parse_time(observed_at), "source_kind": source_kind,
                "source_ref": source_ref, "stage": stage}
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


def evidence_from_paths(system: str, paths: Iterable[Path], root: Path) -> tuple[list[dict[str, Any]], Counter, list[dict[str, str]]]:
    quotes: list[dict[str, Any]] = []
    reasons: Counter = Counter()
    provenance: list[dict[str, str]] = []
    for path in _iter_json_paths(paths):
        try:
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


def choose_quote(target: dict[str, Any], evidence: Iterable[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    exact = [q for q in evidence if q["fixture_identity"] == target["fixture_identity"]
             and q["market_code"] == target["market_code"] and q["line"] == target["line"]
             and q["side"] == target["side"]]
    if not exact:
        return None, "no_exact_fixture_market_line_side_evidence"
    prior = [q for q in exact if q["observed_at"] <= target["predicted_at"]]
    if not prior:
        return None, "only_post_prediction_evidence"
    return max(prior, key=lambda q: q["observed_at"]), None


_ENTRY_BODY_FIELDS = (
    "system", "snapshot_identity", "market_code", "line", "side",
    "selected_odds", "observed_at", "evidence_source_kind",
    "evidence_source_hash", "evidence_age_seconds",
)


def _validate_entry(entry: Any) -> dict[str, Any]:
    """Fail closed unless an entry is a canonical, self-authenticating quote."""
    if not isinstance(entry, dict) or set(entry) != {*_ENTRY_BODY_FIELDS, "entry_hash"}:
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
    body = {key: entry[key] for key in _ENTRY_BODY_FIELDS}
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


def _entry(target: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    body = {"system": target["system"], "snapshot_identity": target["snapshot_identity"],
            "market_code": target["market_code"], "line": target["line"], "side": target["side"],
            "selected_odds": quote["odds"], "observed_at": quote["observed_at"].isoformat(),
            "evidence_source_kind": quote["source_kind"], "evidence_source_hash": quote["source_ref"],
            "evidence_age_seconds": round((target["predicted_at"] - quote["observed_at"]).total_seconds(), 6)}
    return {**body, "entry_hash": _sha(body)}


def report(rows_by_system: dict[str, list[dict[str, Any]]], paths_by_system: dict[str, list[Path]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    all_entries: list[dict[str, Any]] = []
    systems: dict[str, Any] = {}
    for system, rows in rows_by_system.items():
        targets, target_reasons = prediction_targets(rows, system)
        evidence, evidence_reasons, provenance = evidence_from_paths(system, paths_by_system.get(system, []), Path("."))
        recovered: Counter = Counter(); unresolved = target_reasons + evidence_reasons; ages: list[float] = []
        candidates: list[dict[str, Any]] = []
        for target in targets:
            quote, reason = choose_quote(target, evidence)
            if not quote:
                unresolved[reason or "unknown"] += 1; continue
            candidate = _entry(target, quote); candidates.append(candidate); all_entries.append(candidate)
            recovered[f"{target['stage']}|{target['market_code']}|{quote['source_kind']}"] += 1
            ages.append(candidate["evidence_age_seconds"])
        systems[system] = {"missing_total": missing_selected_odds_count(rows),
                           "strict_identity_target_total": len(targets),
                           "recovered_candidate_count": len(candidates),
                           "recovered_by_stage_market_source": dict(sorted(recovered.items())),
                           "unrecoverable_reasons": dict(sorted(unresolved.items())),
                           "evidence_age_seconds": {"count": len(ages), "min": min(ages) if ages else None,
                               "max": max(ages) if ages else None, "median": sorted(ages)[len(ages)//2] if ages else None},
                           "evidence_files": len(provenance), "provenance": provenance}
    return {"schema_version": SCHEMA_VERSION, "mode": "dry_run", "systems": systems}, all_entries


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
            if old.get("entry_hash") != entry.get("entry_hash"):
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


def overlay_rows(rows: list[dict[str, Any]], system: str, sidecar_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return a decorated copy; callers never mutate their raw payload."""
    path = Path(sidecar_path or os.environ.get("ODDS_RECOVERY_SIDECAR", "")) if (sidecar_path or os.environ.get("ODDS_RECOVERY_SIDECAR")) else None
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
                if entry:
                    collection["odds"] = float(entry["selected_odds"])
                    collection["recovery_provenance"] = "historical_exact_prior"
    return result


def _load_rows(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict): value = value.get("rows") or (value.get("prediction_history") or {}).get("rows") or []
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def main() -> None:
    parser = argparse.ArgumentParser(description="Strict historical odds recovery; dry-run by default")
    parser.add_argument("--footbreak-history", type=Path, required=True)
    parser.add_argument("--crown-history", type=Path, required=True)
    parser.add_argument("--allow", action="append", default=[], help="system=relative-or-absolute-path (repeatable)")
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="explicitly append candidates to private sidecar")
    args = parser.parse_args()
    paths = {system: [] for system in ("footbreak", "crown")}
    for raw in args.allow:
        system, sep, value = raw.partition("=")
        if system not in paths or not sep: parser.error("--allow must be footbreak=PATH or crown=PATH")
        paths[system].append(Path(value))
    if not args.allow:
        root = Path.cwd(); paths = {s: [root / p for p in values] for s, values in DEFAULT_ALLOWLIST.items()}
    result, entries = report({"footbreak": _load_rows(args.footbreak_history), "crown": _load_rows(args.crown_history)}, paths)
    if args.apply:
        result["mode"] = "apply"; result["apply"] = apply(args.sidecar, entries)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))

if __name__ == "__main__": main()
