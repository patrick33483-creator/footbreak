"""Shared, fail-closed granular condition mining for dashboard observations.

This module deliberately consumes saved stage rows only.  It has no model,
staking, transport, or settlement side effects.  A condition is a deterministic
description of one fixture/market path, so the exact same key can be matched
against a newly persisted upcoming stage without consulting a future stage.
"""
from __future__ import annotations

import itertools
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


STAGES = ("首預", "T-30", "T-5")
STAGE_ORDER = {name: index for index, name in enumerate(STAGES)}
MARKETS = ("HDC", "HIL", "CHL")
MARKET_LABELS = {"HDC": "讓球", "HIL": "入球大細", "CHL": "角球大細"}
ODDS_SPLIT = 1.70
MAX_PUBLIC = 72
MAX_CARD_MATCHES = 4
HKT = timezone(timedelta(hours=8))


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _odds(value: Any) -> float | None:
    value = _number(value)
    return value if value is not None and value > 1.0 else None


def _time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=HKT) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _line(value: Any) -> float | None:
    return _number(value)


def _side(item: dict[str, Any]) -> str | None:
    value = str(item.get("side") or item.get("selection") or "").upper()
    return value if value in {"H", "A", "L"} else None


def _role(market: str, side: str | None, line: float | None) -> str | None:
    if market == "HIL":
        return {"H": "大", "L": "細"}.get(side)
    if market == "CHL":
        return {"H": "角球大", "L": "角球細"}.get(side)
    if market != "HDC" or side not in {"H", "A"} or line is None:
        return None
    if abs(line) < 1e-9:
        return "平手盤主" if side == "H" else "平手盤客"
    if side == "H":
        return "主讓" if line < 0 else "主受讓"
    return "客讓" if line > 0 else "客受讓"


def _selected_line(market: str, side: str | None, line: float | None) -> float | None:
    if line is None:
        return None
    return -line if market == "HDC" and side == "A" else line


def _line_bucket(market: str, selected_line: float | None) -> str | None:
    if selected_line is None:
        return None
    amount = abs(selected_line)
    if market == "HDC":
        if amount < 1e-9:
            return "平手"
        if amount <= 0.5:
            return "0.25–0.5"
        if amount <= 1:
            return "0.75–1.0"
        return ">1.0"
    if market == "HIL":
        return "≤2.5" if amount <= 2.5 else "2.75–3.0" if amount <= 3 else ">3.0"
    return "≤9.5" if amount <= 9.5 else "10–10.5" if amount <= 10.5 else "≥10.75"


def _relative_direction(market: str, sides: Iterable[str | None]) -> str | None:
    values = list(sides)
    if any(value is None for value in values):
        return None
    labels: dict[str, str] = {}
    alphabet = iter("ABC")
    output = []
    for value in values:
        assert value is not None
        # H/A and over/under each become relative trajectory labels.  This
        # makes a reversal A→B→A portable to a different fixture's teams.
        if value not in labels:
            labels[value] = next(alphabet)
        output.append(labels[value])
    return "→".join(output)


def _movement(lines: Iterable[float | None]) -> str | None:
    values = list(lines)
    if any(value is None for value in values):
        return None
    clean = [float(value) for value in values if value is not None]
    if len(clean) == 1 or max(clean) - min(clean) < 1e-9:
        return "不變"
    if len(clean) == 3 and abs(clean[0] - clean[2]) < 1e-9:
        return "先變後返"
    changes = [right - left for left, right in zip(clean, clean[1:])]
    if all(value > 0 for value in changes):
        return "上升"
    if all(value < 0 for value in changes):
        return "下降"
    return "反覆"


def _tier(odds: float | None) -> str | None:
    if odds is None:
        return None
    return "≥1.70" if odds >= ODDS_SPLIT else "<1.70"


def _pre_kickoff(row: dict[str, Any]) -> bool:
    """Require auditable timestamps; unknown timing is not a usable sample."""
    kickoff = _time(row.get("kickoff") or row.get("kickoff_hkt"))
    saved = _time(row.get("predicted_at") or row.get("ts") or row.get("observed_at"))
    if kickoff is not None and kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=HKT)
    if saved is not None and saved.tzinfo is None:
        saved = saved.replace(tzinfo=HKT)
    return kickoff is not None and saved is not None and saved < kickoff


def _settled(item: dict[str, Any]) -> bool:
    return str(item.get("grade_status") or "") == "GRADED"


def _outcome(item: dict[str, Any]) -> bool | None | object:
    hit = item.get("hit")
    if hit is True or hit is False or hit is None:
        return hit
    return object()


def _canonical_rows(rows: Iterable[dict[str, Any]], *, settled_only: bool) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Newest valid immutable pre-kickoff selected row per fixture-market-stage."""
    candidates: dict[tuple[str, str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict) or not _pre_kickoff(row):
            continue
        fixture = str(row.get("match_id") or row.get("history_key") or "").strip()
        stage = str(row.get("stage") or "").strip()
        if not fixture or stage not in STAGE_ORDER:
            continue
        source = row.get("market_grades") if settled_only else row.get("market_predictions")
        for item in source or []:
            if not isinstance(item, dict):
                continue
            market = str(item.get("code") or "").upper()
            side = _side(item)
            odds = _odds(item.get("odds"))
            raw_line = _line(item.get("line", item.get("condition")))
            if market not in MARKETS or side is None or odds is None or raw_line is None:
                continue
            if settled_only and not _settled(item):
                continue
            result = _outcome(item)
            if settled_only and type(result) is object:
                continue
            candidates[(fixture, market, stage)].append((row, item))

    output: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, values in candidates.items():
        row, item = max(
            values,
            key=lambda pair: (
                str(pair[0].get("predicted_at") or pair[0].get("ts") or ""),
                repr(sorted(pair[1].items())),
            ),
        )
        market = key[1]
        side = _side(item)
        raw_line = _line(item.get("line", item.get("condition")))
        odds = _odds(item.get("odds"))
        assert side is not None and raw_line is not None and odds is not None
        output[key] = {
            "fixture": key[0],
            "market": market,
            "stage": key[2],
            "kickoff": _time(row.get("kickoff") or row.get("kickoff_hkt")),
            "side": side,
            "selected_line": _selected_line(market, side, raw_line),
            "role": _role(market, side, raw_line),
            "line_bucket": _line_bucket(market, _selected_line(market, side, raw_line)),
            "odds": odds,
            "odds_tier": _tier(odds),
            "hit": _outcome(item) if settled_only else None,
            "push": _outcome(item) is None if settled_only else False,
        }
    return output


def canonical_panels(rows: Iterable[dict[str, Any]], *, settled_only: bool = True) -> list[dict[str, Any]]:
    panels: dict[tuple[str, str], dict[str, Any]] = {}
    for (_fixture, _market, _stage), item in _canonical_rows(rows, settled_only=settled_only).items():
        panel = panels.setdefault(
            (item["fixture"], item["market"]),
            {"fixture": item["fixture"], "market": item["market"], "kickoff": item["kickoff"], "stages": {}},
        )
        panel["stages"][item["stage"]] = item
    return list(panels.values())


def _paths(panel: dict[str, Any], decision_stage: str | None = None) -> Iterable[tuple[dict[str, Any], ...]]:
    maximum = STAGE_ORDER.get(decision_stage, len(STAGES) - 1)
    available = [stage for stage in STAGES[:maximum + 1] if stage in panel["stages"]]
    for size in range(1, len(available) + 1):
        for names in itertools.combinations(available, size):
            yield tuple(panel["stages"][name] for name in names)


def _descriptor(system: str, path: tuple[dict[str, Any], ...], level: int) -> tuple[tuple[str, ...], str, int]:
    terminal = path[-1]
    stages = "→".join(item["stage"] for item in path)
    direction = _relative_direction(terminal["market"], (item["side"] for item in path))
    movement = _movement(item["selected_line"] for item in path)
    fields = [
        ("system", system), ("market", terminal["market"]), ("path", stages),
        ("decision", terminal["stage"]), ("tier", terminal["odds_tier"]),
    ]
    label = [
        f"{MARKET_LABELS.get(terminal['market'], terminal['market'])}｜{stages}",
        f"決策 {terminal['stage']}",
        terminal["odds_tier"],
    ]
    if level >= 1 and direction:
        fields.append(("direction", direction))
        # The key remains relative (portable across fixtures), but public
        # labels must name the observed Chinese directions instead of A/B/C.
        direction_label = "→".join(
            str(item.get("role") or "") for item in path
        )
        label.append(f"方向 {direction_label}" if direction_label else "方向已記錄")
    if level >= 2:
        for key, name in (("role", "角色"), ("bucket", "線位")):
            value = terminal["role"] if key == "role" else terminal["line_bucket"]
            if value:
                fields.append((key, value))
                label.append(f"{name} {value}")
        if movement:
            fields.append(("movement", movement))
            label.append(f"走勢 {movement}")
    if level >= 3 and len(path) > 1:
        odds_path = "→".join(str(item["odds_tier"]) for item in path)
        fields.append(("tier_path", odds_path))
        label.append(f"賠率路徑 {odds_path}")
    key = tuple(f"{name}={value}" for name, value in fields)
    return key, "｜".join(label), len(fields)


def _metrics(samples: list[tuple[dict[str, Any], tuple[dict[str, Any], ...]]]) -> dict[str, Any]:
    terminal = [path[-1] for _panel, path in samples]
    decided = [item for item in terminal if item["hit"] is True or item["hit"] is False]
    hits = sum(item["hit"] is True for item in decided)
    n = len(decided)
    return {
        "settled": len(terminal), "pushes": len(terminal) - n,
        "hits": hits, "decided": n,
        "accuracy": round(hits / n, 6) if n else None,
        "wilson95": _wilson(hits, n),
    }


def _wilson(hits: int, n: int) -> list[float] | None:
    if not n:
        return None
    z = 1.959963984540054
    p = hits / n
    d = 1 + z * z / n
    middle = (p + z * z / (2 * n)) / d
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0, middle - spread), 6), round(min(1, middle + spread), 6)]


def _split(samples: list[tuple[dict[str, Any], tuple[dict[str, Any], ...]]]) -> tuple[list[Any], list[Any], str | None]:
    kickoff = {
        panel["fixture"]: panel["kickoff"]
        for panel, _path in samples if panel.get("kickoff") is not None
    }
    ordered = sorted(kickoff, key=lambda fixture: (kickoff[fixture], fixture))
    if len(ordered) < 2:
        return ordered, [], None
    holdout = max(1, math.ceil(len(ordered) * .30))
    cut = len(ordered) - holdout
    return ordered[:cut], ordered[cut:], kickoff[ordered[cut]].isoformat()


def _pvalue(hits: int, n: int, expected: float | None) -> float | None:
    if not n or expected is None or not 0 <= expected <= 1:
        return None
    return min(1.0, sum(math.comb(n, i) * expected ** i * (1 - expected) ** (n - i) for i in range(hits, n + 1)))


def _assign_q(rows: list[dict[str, Any]]) -> None:
    indexed = sorted(((i, item["p_value"]) for i, item in enumerate(rows) if item["p_value"] is not None), key=lambda pair: pair[1])
    running, count, out = 1., len(indexed), {}
    for rank in range(count, 0, -1):
        index, value = indexed[rank - 1]
        running = min(running, value * count / rank)
        out[index] = round(running, 6)
    for index, item in enumerate(rows):
        item["q_value"] = out.get(index)


def _badge(total: dict[str, Any], train: dict[str, Any], holdout: dict[str, Any], lift: float | None, q: float | None) -> str:
    if total["decided"] < 10:
        return "隱藏"
    if total["decided"] < 30:
        return "樣本不足"
    if total["decided"] >= 60 and train["decided"] >= 40 and holdout["decided"] >= 20 and lift is not None and lift >= 0:
        return "穩健觀察" + (" · q≤0.05" if q is not None and q <= .05 else "")
    return "觀察"


def _stable_signature(item: dict[str, Any]) -> str:
    """A lexical final tie-breaker which cannot reward a lucky hit rate."""
    key = item.get("key")
    if isinstance(key, list):
        return "\x1f".join(str(value) for value in key)
    return str(key or item.get("label") or "")


_KEY_AXIS_ALIASES = {
    "stage": "decision",
    "decision_stage": "decision",
    "observed_path": "path",
    "odds_tier": "tier",
    "line_bucket": "bucket",
    "role": "role",
    "direction": "direction",
    "movement": "movement",
    "odds_trajectory": "tier_path",
    "tier_path": "tier_path",
}


def _ranking_axis_map(candidate: dict[str, Any]) -> dict[str, str] | None:
    """Read an exact legacy/current ranking definition without widening it.

    Older persisted rankings stored some matcher axes only in ``key`` while
    newer payloads also expose named fields.  This adapter accepts those
    spelling changes *only when every supplied value agrees*.  It never
    infers an absent direction, line bucket, market, stage, or system.
    """
    values: dict[str, str] = {}
    raw_key = candidate.get("key")
    if raw_key is not None:
        if not isinstance(raw_key, list):
            return None
        for raw in raw_key:
            if not isinstance(raw, str) or "=" not in raw:
                return None
            name, value = raw.split("=", 1)
            name = _KEY_AXIS_ALIASES.get(name.strip(), name.strip())
            value = value.strip()
            if not name or not value or (name in values and values[name] != value):
                return None
            values[name] = value
    for source, axis in (
        ("system", "system"), ("market", "market"),
        ("decision_stage", "decision"), ("stage", "decision"),
        ("observed_path", "path"), ("path", "path"),
        ("odds_tier", "tier"), ("line_bucket", "bucket"),
        ("role", "role"), ("direction", "direction"),
        ("movement", "movement"), ("odds_trajectory", "tier_path"),
        ("tier_path", "tier_path"),
    ):
        value = candidate.get(source)
        if value in (None, ""):
            continue
        value = str(value).strip()
        if not value or (axis in values and values[axis] != value):
            return None
        values[axis] = value
    return values


def canonical_ranking(
    ranking: Iterable[dict[str, Any]], *, system: str, decision_stage: str,
) -> list[dict[str, Any]]:
    """Return schema-compatible rankings with an exact canonical matcher key.

    Compatibility is deliberately narrow: aliases such as ``tier`` /
    ``odds_tier`` are normalized, but missing axes are not filled from a live
    quote.  A malformed or cross-system record is dropped, so callers remain
    fail-closed and cannot accidentally match another market or direction.
    """
    normalized: list[dict[str, Any]] = []
    # A portfolio admission must carry the complete immutable definition.  A
    # broad market/tier cohort is useful for a dashboard, but it cannot stand
    # in for a direction-and-line-specific validation condition.
    required = {
        "system", "market", "path", "decision", "tier",
        "direction", "role", "bucket",
    }
    optional = ("direction", "role", "bucket", "movement", "tier_path")
    for candidate in ranking:
        if not isinstance(candidate, dict):
            continue
        axes = _ranking_axis_map(candidate)
        if axes is None or not required.issubset(axes):
            continue
        if (
            axes["system"] != system
            or axes["market"] not in MARKETS
            or axes["decision"] != decision_stage
            or axes["path"].split("→")[-1] != decision_stage
        ):
            continue
        key_parts = [
            ("system", axes["system"]), ("market", axes["market"]),
            ("path", axes["path"]), ("decision", axes["decision"]),
            ("tier", axes["tier"]),
        ]
        for name in optional:
            if name in axes:
                key_parts.append((name, axes[name]))
        canonical_key = tuple(f"{name}={value}" for name, value in key_parts)
        # Exact key agreement is required when a stored key was supplied.
        # The emitted descriptor has a stable axis ordering, therefore this
        # detects incompatible legacy schemas rather than guessing.
        original_key = candidate.get("key")
        if isinstance(original_key, list) and tuple(
            f"{_KEY_AXIS_ALIASES.get(str(raw).split('=', 1)[0], str(raw).split('=', 1)[0])}="
            f"{str(raw).split('=', 1)[1]}"
            for raw in original_key if isinstance(raw, str) and "=" in raw
        ) != canonical_key:
            continue
        normalized.append(candidate | {
            "system": system,
            "market": axes["market"],
            "decision_stage": decision_stage,
            "observed_path": axes["path"],
            "odds_tier": axes["tier"],
            "key": list(canonical_key),
        })
    return normalized


def conservative_rank_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """Rank discovery candidates without using raw accuracy as a winner.

    Accuracy remains an eligibility threshold.  Once a condition has cleared
    it, prefer a usable Wilson lower bound, then more decided samples, then a
    less-specific definition and a stable signature.  This same ordering is
    used before bounded cache/public lists are cut, otherwise a raw-accuracy
    ranking could hide the conservative candidate from the T-5 fast path.
    """
    total = item.get("total") if isinstance(item.get("total"), dict) else {}
    wilson = total.get("wilson95")
    lower = _number(wilson[0]) if isinstance(wilson, (list, tuple)) and wilson else None
    return (
        0 if lower is not None else 1,
        -(lower or 0.0),
        -int(total.get("decided") or 0),
        int(item.get("specificity") or 0),
        _stable_signature(item),
    )


def mine(rows: Iterable[dict[str, Any]], *, system: str) -> dict[str, Any]:
    """Return a bounded dashboard payload from current-era, settled rows."""
    rows = list(rows)
    panels = canonical_panels(rows, settled_only=True)
    cohorts: dict[tuple[str, ...], list[tuple[dict[str, Any], tuple[dict[str, Any], ...]]]] = defaultdict(list)
    labels: dict[tuple[str, ...], tuple[str, int]] = {}
    baselines: dict[tuple[str, str, str], list[tuple[dict[str, Any], tuple[dict[str, Any], ...]]]] = defaultdict(list)
    for panel in panels:
        for path in _paths(panel):
            terminal = path[-1]
            if terminal["hit"] not in {True, False, None}:
                continue
            if len(path) == 1:
                baselines[(terminal["market"], terminal["stage"], terminal["odds_tier"])].append((panel, path))
            # A one-stage path has no odds trajectory, so descriptor levels 2
            # and 3 intentionally describe the same condition.  Count that
            # fixture once, not twice, or a nine-row cohort could falsely
            # satisfy the minimum decided-sample rule.
            path_keys: set[tuple[str, ...]] = set()
            for level in range(4):
                key, label, specificity = _descriptor(system, path, level)
                if key in path_keys:
                    continue
                path_keys.add(key)
                cohorts[key].append((panel, path))
                labels[key] = (label, specificity)

    candidates: list[dict[str, Any]] = []
    for key, samples in cohorts.items():
        total = _metrics(samples)
        # The discovery phase may retain all historical rows, but a condition
        # is not a candidate for the independent validation portfolio until it
        # has at least 20 decided outcomes and clears the strict rate gate.
        if total["decided"] < 20 or total["accuracy"] is None or total["accuracy"] <= .60:
            continue
        train_ids, holdout_ids, cutoff = _split(samples)
        train = _metrics([sample for sample in samples if sample[0]["fixture"] in train_ids])
        holdout = _metrics([sample for sample in samples if sample[0]["fixture"] in holdout_ids])
        terminal = samples[0][1][-1]
        baseline = _metrics([
            sample for sample in baselines[(terminal["market"], terminal["stage"], terminal["odds_tier"])]
            if sample[0]["fixture"] in holdout_ids
        ])
        lift = round(holdout["accuracy"] - baseline["accuracy"], 6) if holdout["accuracy"] is not None and baseline["accuracy"] is not None else None
        label, specificity = labels[key]
        candidates.append({
            "key": list(key), "label": label, "system": system, "market": terminal["market"],
            "market_label": MARKET_LABELS.get(terminal["market"], terminal["market"]),
            "observed_path": "→".join(item["stage"] for item in samples[0][1]),
            "decision_stage": terminal["stage"], "odds_tier": terminal["odds_tier"],
            "total": total, "train": train, "holdout": holdout, "holdout_baseline": baseline,
            "holdout_lift": lift, "holdout_cutoff": cutoff, "specificity": specificity,
            "p_value": _pvalue(holdout["hits"], holdout["decided"], baseline["accuracy"]),
        })
    _assign_q(candidates)
    for candidate in candidates:
        candidate["badge"] = _badge(candidate["total"], candidate["train"], candidate["holdout"], candidate["holdout_lift"], candidate["q_value"])

    # Multiple keys within one market sometimes select an identical fixture
    # cohort. Retain one condition at each specificity level: a more-specific
    # role condition must not hide a general or direction-only condition that
    # remains the only valid match for a later fixture.  The market remains in
    # the signature so an HDC cohort never suppresses HIL/CHL.
    retained: dict[tuple[str, ...], dict[str, Any]] = {}
    for candidate in candidates:
        signature = (
            str(candidate.get("market") or ""),
            str(candidate.get("decision_stage") or ""),
            int(candidate.get("specificity") or 0),
            *(sorted(
                f"{sample[0]['fixture']}|{sample[0]['market']}"
                for sample in cohorts[tuple(candidate["key"])]
            )),
        )
        old = retained.get(signature)
        if old is None or (candidate["specificity"], len(candidate["label"])) > (old["specificity"], len(old["label"])):
            retained[signature] = candidate
    ranked = sorted(retained.values(), key=conservative_rank_key)[:MAX_PUBLIC]
    return {
        "version": "granular-condition-v1", "system": system,
        "source_rows": len(rows),
        "canonical_fixture_market_panels": len(panels),
        "holdout": "latest 30% by kickoff; no future rows used in a path",
        "ranking": ranked,
        "disclaimer": "只作數據觀察，並非自動投注或下注建議。",
    }


def match_upcoming(
    rows: Iterable[dict[str, Any]], ranking: Iterable[dict[str, Any]], *,
    system: str, decision_stage: str,
) -> dict[str, list[dict[str, Any]]]:
    """Match only information available at the stage being persisted/rendered."""
    maximum = STAGE_ORDER.get(decision_stage)
    if maximum is None:
        return {}
    allowed = {
        tuple(item["key"]): item
        for item in canonical_ranking(
            ranking, system=system, decision_stage=decision_stage,
        )
    }
    matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for panel in canonical_panels(rows, settled_only=False):
        for path in _paths(panel, decision_stage):
            if path[-1]["stage"] != decision_stage:
                continue
            for level in range(4):
                key, _label, _specificity = _descriptor(system, path, level)
                candidate = allowed.get(key)
                if candidate is not None:
                    # Keep the exact selected direction/line that made this
                    # *upcoming* path match.  The ranking remains historical
                    # only; these fields simply let downstream execution
                    # reject any contradictory opportunity defensively.
                    matches[panel["fixture"]].append(candidate | {
                        "selected_side": path[-1]["side"],
                        "selected_line": path[-1]["selected_line"],
                    })
    for fixture, values in matches.items():
        dedup = {tuple(item["key"]): item for item in values}
        matches[fixture] = sorted(dedup.values(), key=conservative_rank_key)[:MAX_CARD_MATCHES]
    return dict(matches)


def notification_opportunities(
    history_rows: Iterable[dict[str, Any]], watches: dict[str, Any],
    fresh_events: Iterable[dict[str, Any]], *, system: str,
    ranking: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return granular opportunities from a cached or freshly mined ranking.

    The caller owns transport and durable acknowledgement.  This pure helper
    makes a failed transport harmless to prediction persistence.  ``ranking``
    lets deadline-sensitive callers reuse the persisted historical ranking
    instead of mining the full history on every notification attempt; omitted
    ranking retains the existing standalone/test behavior.
    """
    history_rows = list(history_rows)
    if ranking is None:
        ranking = mine(history_rows, system=system)["ranking"]
    else:
        ranking = [
            item for item in ranking
            if isinstance(item, dict) and item.get("system") == system
        ]
    live_rows = [
        {
            "match_id": str(match_id), "stage": stage.get("stage"),
            "kickoff": watch.get("kickoff") or watch.get("kickoff_hkt"),
            "predicted_at": stage.get("ts") or stage.get("source_snapshot_at"),
            "market_predictions": stage.get("market_predictions") or [],
        }
        for match_id, watch in watches.items() if isinstance(watch, dict)
        for stage in (watch.get("stages") or []) if isinstance(stage, dict)
    ]
    output: list[dict[str, Any]] = []
    for event in fresh_events:
        if not isinstance(event, dict):
            continue
        fixture, stage_name = str(event.get("match_id") or ""), str(event.get("stage") or "")
        if stage_name not in {"T-30", "T-5"} or fixture not in watches:
            continue
        watch = watches.get(fixture)
        if not isinstance(watch, dict):
            continue
        stages = [stage for stage in watch.get("stages") or []
                  if isinstance(stage, dict) and stage.get("stage") == stage_name]
        if len(stages) != 1:
            continue
        matches = match_upcoming(live_rows, ranking, system=system, decision_stage=stage_name).get(fixture, [])
        for market in MARKETS:
            selected = [item for item in stages[0].get("market_predictions") or []
                        if isinstance(item, dict) and str(item.get("code") or "") == market]
            if len(selected) != 1 or _odds(selected[0].get("odds")) is None:
                continue
            choices = [item for item in matches if item.get("market") == market]
            if not choices:
                continue
            choices.sort(key=conservative_rank_key)
            output.append({
                "fixture": fixture, "stage": stage_name, "market": market,
                "watch": watch, "selected": selected[0], "matches": choices[:MAX_CARD_MATCHES],
            })
    return sorted(output, key=lambda item: (item["fixture"], item["market"], item["stage"]))
