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
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
PARSER_VERSION = "odds-recovery-provider-v1"
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
_ENTRY_OPTIONAL_FIELDS = {"provider_evidence"}


def _validate_entry(entry: Any) -> dict[str, Any]:
    """Fail closed unless an entry is a canonical, self-authenticating quote."""
    if not isinstance(entry, dict) or set(entry) not in ({*_ENTRY_BODY_FIELDS, "entry_hash"}, {*_ENTRY_BODY_FIELDS, "entry_hash", *_ENTRY_OPTIONAL_FIELDS}):
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
    body = {key: entry[key] for key in _ENTRY_BODY_FIELDS}
    if "provider_evidence" in entry:
        body["provider_evidence"] = entry["provider_evidence"]
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
    if quote.get("provider_evidence") is not None:
        body["provider_evidence"] = quote["provider_evidence"]
    return {**body, "entry_hash": _sha(body)}




def _validate_provider_evidence(value: Any, entry: dict[str, Any]) -> None:
    """Provider evidence is compact, non-display metadata kept in the private sidecar."""
    required = {"provider", "source_url_hash", "company_id", "native_odds_format", "native_price", "normalized_decimal", "quote_timestamp", "target_timestamp", "age_seconds", "parser_version"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("malformed_provider_evidence")
    if value["provider"] not in {"titan_crown", "tipsme_hkjc"}:
        raise ValueError("malformed_provider_evidence")
    if not all(isinstance(value[key], str) and value[key] for key in ("source_url_hash", "company_id", "native_odds_format", "native_price", "normalized_decimal", "quote_timestamp", "target_timestamp", "parser_version")):
        raise ValueError("malformed_provider_evidence")
    if value["parser_version"] != PARSER_VERSION or value["normalized_decimal"] != entry["selected_odds"]:
        raise ValueError("malformed_provider_evidence")
    parse_time(value["quote_timestamp"]); parse_time(value["target_timestamp"])
    try:
        native = _decimal(value["native_price"])
    except ValueError:
        raise ValueError("malformed_provider_evidence") from None
    if value["native_odds_format"] == "hong_kong" and _decimal(value["normalized_decimal"], odds=True) != native + Decimal("1"):
        raise ValueError("malformed_provider_evidence")
    age = value["age_seconds"]
    if isinstance(age, bool) or not isinstance(age, (float, int)) or age < 0 or not math.isfinite(age):
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
                 retries: int = 2, timeout_seconds: float = 25.0, workers: int = 1):
        self.cache = cache
        self.rate_per_second = max(0.1, rate_per_second)
        self.retries = max(0, retries)
        self.timeout_seconds = max(0.1, timeout_seconds)
        self.workers = max(1, min(32, int(workers)))
        self._state_lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._next_start = 0.0
        self._results: dict[str, tuple[str | None, bool, str | None]] = {}
        self._inflight: dict[str, threading.Event] = {}
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
        result = self._fetch_uncached(url)
        return self._complete(url, result)

    def get_many(self, urls: Iterable[str]) -> dict[str, tuple[str | None, bool, str | None]]:
        """Fetch a deterministic unique URL set with controlled concurrency."""
        unique = sorted(set(urls))
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="provider-fetch") as pool:
            futures = {url: pool.submit(self.get, url) for url in unique}
            return {url: futures[url].result() for url in unique}

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

    def _fetch_uncached(self, url: str) -> tuple[str | None, bool, str | None]:
        last_error = "unknown"
        timed_out = False
        for attempt in range(self.retries + 1):
            self._wait_for_start()
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*;q=0.8"})
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read(); status = int(getattr(response, "status", 200) or 200)
                if status != 200:
                    raise OSError(f"http_{status}")
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
    if value is None: return None
    if market == "HDC" and not raw.strip().startswith(("-", "+")):
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
        if len(numeric) < 3: continue
        # Price / line / price is the verified movement-row ordering; take its
        # last triple before timestamp so sequence/table index cells cannot win.
        home, line_raw, away = numeric[-3:]
        line = _titan_line(before_time[line_raw[0]], market)
        if not line or home[1] <= 0 or away[1] <= 0: continue
        rows.append({"line": line, "H": home[1], "A" if market == "HDC" else "L": away[1], "observed_at": observed, "status": "即", "company_id": company_id})
    return sorted(rows, key=lambda row: row["observed_at"])


def _provider_quote(target: dict[str, Any], tick: dict[str, Any], target_time: datetime, *, provider: str, source_url: str, company_id: str) -> dict[str, Any] | None:
    try:
        native = _decimal(tick["price"])
        decimal = native + Decimal("1")
        return _quote(target["fixture_identity"], target["market_code"], target["line"], target["side"], decimal, tick["observed_at"], f"provider_{provider}", _url_hash(source_url), target["stage"], {
            "provider": provider, "source_url_hash": _url_hash(source_url), "company_id": company_id,
            "native_odds_format": "hong_kong", "native_price": str(native), "normalized_decimal": str(decimal.normalize()),
            "quote_timestamp": tick["observed_at"].isoformat(), "target_timestamp": target_time.isoformat(),
            "age_seconds": round((target_time - tick["observed_at"]).total_seconds(), 6), "parser_version": PARSER_VERSION,
        })
    except (KeyError, ValueError): return None


def titan_candidate(target: dict[str, Any], source: str, url: str) -> tuple[dict[str, Any] | None, str | None]:
    kickoff = _kickoff_for_target(target)
    if not kickoff: return None, "kickoff_context_unavailable"
    if target["market_code"] not in {"HDC", "HIL"}: return None, "provider_market_unsupported"
    ticks = parse_titan_change_rows(source, target["market_code"], kickoff)
    exact = [dict(tick, price=tick.get(target["side"])) for tick in ticks if tick["line"] == target["line"] and target["side"] in tick]
    if not exact: return None, "no_exact_fixture_market_line_side_evidence"
    if target["stage"] == "首預":
        eligible = [tick for tick in exact if tick["observed_at"] <= kickoff]
        target_time = kickoff
        selected = min(eligible, key=lambda row: row["observed_at"]) if eligible else None
    elif target["stage"] in {"T-30", "T-5"}:
        target_time = kickoff - timedelta(minutes=30 if target["stage"] == "T-30" else 5)
        eligible = [tick for tick in exact if tick["observed_at"] <= target_time]
        selected = max(eligible, key=lambda row: row["observed_at"]) if eligible else None
    else: return None, "unsupported_stage"
    if not selected: return None, "no_qualifying_prior_quote"
    return _provider_quote(target, selected, target_time, provider="titan_crown", source_url=url, company_id="3"), None


def tipsme_crosswalk(target: dict[str, Any]) -> str | None:
    row = target.get("row") or {}
    hkjc = str(row.get("hkjc_match_id") or "").strip()
    direct = str(row.get("tipsme_match_id") or "").strip()
    bridge = row.get("tipsme_crosswalk")
    if direct and str(row.get("tipsme_hkjc_match_id") or "").strip() == hkjc and hkjc:
        return direct
    if isinstance(bridge, dict) and hkjc and str(bridge.get("hkjc_match_id") or "").strip() == hkjc:
        value = str(bridge.get("tipsme_match_id") or "").strip()
        if value and bridge.get("provider_id_evidence") is True: return value
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
            out.append({"line": normalized_line, "side": side, "price": price, "observed_at": observed, "opening": bool(node.get("opening") or node.get("isOpening") or node.get("initial") or node.get("label") == "初")})
    return out

def tipsme_candidate(target: dict[str, Any], source: str, url: str) -> tuple[dict[str, Any] | None, str | None]:
    kickoff = _kickoff_for_target(target)
    if not kickoff: return None, "kickoff_context_unavailable"
    ticks = [tick for tick in parse_tipsme_chart_ticks(source, target["market_code"]) if tick["line"] == target["line"] and tick["side"] == target["side"]]
    if target["stage"] == "首預":
        eligible = [tick for tick in ticks if tick["opening"] and tick["observed_at"] <= kickoff]; at = kickoff
        selected = min(eligible, key=lambda row: row["observed_at"]) if eligible else None
    elif target["stage"] in {"T-30", "T-5"}:
        at = kickoff - timedelta(minutes=30 if target["stage"] == "T-30" else 5)
        eligible = [tick for tick in ticks if tick["observed_at"] <= at]; selected = max(eligible, key=lambda row: row["observed_at"]) if eligible else None
    else: return None, "unsupported_stage"
    if not selected: return None, "no_qualifying_prior_quote"
    return _provider_quote(target, selected, at, provider="tipsme_hkjc", source_url=url, company_id="hkjc"), None


def provider_entries(targets: list[dict[str, Any]], *, providers: set[str], cache_dir: Path,
                     rate_per_second: float, retries: int, timeout_seconds: float = 25.0,
                     workers: int = 1, tipsme_url_template: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve pages in batches, but evaluate each target in original order.

    Titan is always evaluated before Tipsme as before.  Tipsme pages are
    requested only for targets that Titan did not recover, so page batching
    cannot broaden recovery eligibility or change provider precedence.
    """
    fetcher = ProviderFetcher(
        PrivateResponseCache(cache_dir), rate_per_second, retries,
        timeout_seconds=timeout_seconds, workers=workers,
    )
    entries: list[dict[str, Any]] = []; recovered = Counter(); failures = Counter(); attempted = set()
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
            plan["candidate"], plan["reason"] = titan_candidate(plan["target"], source, url)

    for plan in plans:
        if plan["candidate"] is not None or "tipsme" not in providers:
            continue
        target = plan["target"]
        crosswalk = tipsme_crosswalk(target)
        if not crosswalk:
            plan["reason"] = "exact_id_crosswalk_unavailable"
        elif not tipsme_url_template:
            plan["reason"] = "tipsme_public_url_unconfigured"
        else:
            attempted.add(("tipsme", crosswalk))
            plan["tipsme_url"] = tipsme_url_template.format(
                MATCH_ID=crosswalk, match_id=crosswalk,
                market={"HDC": "hdp", "HIL": "hilo", "CHL": "corner"}[target["market_code"]],
            )

    tipsme_pages = fetcher.get_many(plan["tipsme_url"] for plan in plans if "tipsme_url" in plan)
    for plan in plans:
        if plan["candidate"] is not None:
            continue
        url = plan.get("tipsme_url")
        if not url:
            continue
        source, _cached, err = tipsme_pages[url]
        if err:
            plan["reason"] = "http_failure"
        elif source is not None:
            plan["candidate"], plan["reason"] = tipsme_candidate(plan["target"], source, url)

    for plan in plans:
        target = plan["target"]; candidate = plan["candidate"]; reason = plan["reason"]
        if candidate:
            entries.append(_entry(target, candidate)); recovered[f"{target['stage']}|{target['market_code']}|{candidate['source_kind']}"] += 1
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
        "targets_recovered_by_stage_market_source": dict(sorted(recovered.items())),
        "stale_age_bands": dict(sorted(bands.items())),
        "parser_failures": dict(sorted((key, value) for key, value in failures.items() if key not in {"http_failure", "exact_id_crosswalk_unavailable"})),
        "no_qualifying_prior_quote": failures.get("no_qualifying_prior_quote", 0),
        "exact_id_crosswalk_unavailable": failures.get("exact_id_crosswalk_unavailable", 0),
    }

def report(rows_by_system: dict[str, list[dict[str, Any]]], paths_by_system: dict[str, list[Path]], *, provider_options: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
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
    output = {"schema_version": SCHEMA_VERSION, "mode": "dry_run", "systems": systems}
    if provider_options:
        unresolved_targets = []
        indexed = {(entry["system"], entry["snapshot_identity"], entry["market_code"], entry["line"], entry["side"]) for entry in all_entries}
        for system, rows in rows_by_system.items():
            for target in prediction_targets(rows, system)[0]:
                key = (system, target["snapshot_identity"], target["market_code"], target["line"], target["side"])
                if key not in indexed: unresolved_targets.append(target)
        supplied, provider_audit = provider_entries(unresolved_targets, **provider_options)
        all_entries.extend(supplied); output["provider_assisted"] = provider_audit
        for entry in supplied:
            system = entry["system"]; systems[system]["recovered_candidate_count"] += 1
            marker = f"{entry.get("stage", "provider")}|{entry["market_code"]}|{entry["evidence_source_kind"]}"
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
    parser.add_argument("--provider", action="append", choices=("titan", "tipsme"), default=[], help="network provider to use (repeatable)")
    parser.add_argument("--provider-cache", type=Path, help="private 0700/0600 raw-response cache directory")
    parser.add_argument("--provider-rate", type=float, default=1.0, help="maximum provider requests per second (default 1)")
    parser.add_argument("--provider-retries", type=int, default=2)
    parser.add_argument("--provider-timeout", type=float, default=25.0, help="per-request timeout seconds (default 25)")
    parser.add_argument("--provider-workers", type=int, default=1, help="concurrent provider page workers, capped at 32 (default 1)")
    parser.add_argument("--tipsme-url-template", help="verified public Tipsme URL template; uses {MATCH_ID} and {market}")
    args = parser.parse_args()
    if args.provider and not (args.provider_audit or args.provider_apply): parser.error("--provider requires --provider-audit or --provider-apply")
    if (args.provider_audit or args.provider_apply) and not args.provider: parser.error("provider mode requires at least one --provider")
    if (args.provider_audit or args.provider_apply) and not args.provider_cache: parser.error("provider mode requires --provider-cache private directory")
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
            "tipsme_url_template": args.tipsme_url_template,
        }
    result, entries = report({"footbreak": _load_rows(args.footbreak_history), "crown": _load_rows(args.crown_history)}, paths, provider_options=provider_options)
    if args.apply or args.provider_apply:
        result["mode"] = "provider_apply" if args.provider_apply else "apply"; result["apply"] = apply(args.sidecar, entries)
    elif args.provider_audit:
        result["mode"] = "provider_audit"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))

if __name__ == "__main__": main()
