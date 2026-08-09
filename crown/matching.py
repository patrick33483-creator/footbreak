"""Strict event matching: both teams, a unique candidate, and kickoff are mandatory."""
from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Callable, Iterable

from .team_aliases import LEAGUE_ALIAS_SEEDS, TEAM_ALIAS_SEEDS

MATCHING_VERSION = "2026-08-09-hkjc-v4-observed-time"
KICKOFF_TOLERANCE_SECONDS = 10 * 60
STRONG_NAME_KICKOFF_TOLERANCE_SECONDS = 90 * 60
NAME_FLOOR = 0.72
STRONG_NAME_FLOOR = 0.92
ACCEPT_SCORE = 0.80
STRONG_NAME_ACCEPT_SCORE = 0.78
STRONG_NAME_LEAGUE_FLOOR = 0.35
AMBIGUITY_GAP = 0.05
_NOISE = re.compile(r"[\s\u3000·・.,'’`\"()（）\[\]【】\-–—_/\\|]+")
_LATIN_DROP = {"fc", "sc", "cf", "afc", "ac", "club"}
_CJK_DROP = {"足球", "足球會", "足球会", "俱樂部", "俱乐部", "隊", "队"}
_NEUTRAL_MARKER = re.compile(r"[\(\[（【]\s*(?:中|neutral|n)\s*[\)\]）】]", re.I)
_MIXED_SCRIPT_CLUB_AFFIX = re.compile(
    r"(?<![a-z0-9])(?:afc|fc|sc|cf|ac)(?=$|[^a-z0-9])|"
    r"(?<=[^a-z0-9])(?:afc|fc|sc|cf|ac)(?![a-z0-9])",
    re.I,
)
_UCONV = shutil.which("uconv")


@lru_cache(maxsize=8192)
def _traditional_to_simplified(value: str) -> str:
    """Use ICU's deterministic converter when installed; never guess otherwise.

    `icu-devtools` is installed by the DigitalOcean setup script.  The small
    reviewed seed list remains a safe fallback for environments (such as a
    minimal CI image) where ICU is not present.
    """
    if not _UCONV or not value:
        return value
    try:
        completed = subprocess.run(
            (_UCONV, "-x", "Traditional-Simplified"),
            input=value, text=True, capture_output=True, timeout=2, check=True,
        )
        return completed.stdout.strip() or value
    except (OSError, subprocess.SubprocessError):
        return value


def normalize_name(value: str | None) -> str:
    raw = _traditional_to_simplified(unicodedata.normalize("NFKD", str(value or ""))).lower()
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = _NEUTRAL_MARKER.sub(" ", raw)
    raw = _MIXED_SCRIPT_CLUB_AFFIX.sub(" ", raw)
    # Latin club suffixes are removed only as standalone tokens.  The old
    # substring replacement corrupted legitimate names such as Manchester,
    # Racing and Vasco whenever they happened to contain "ac", "fc" or "sc".
    parts = [part for part in _NOISE.sub(" ", raw).split() if part not in _LATIN_DROP]
    compact = "".join(parts)
    for token in _CJK_DROP:
        compact = compact.replace(token, "")
    return compact


def _seed_index(seeds: tuple[tuple[str, str], ...]) -> dict[str, str]:
    output: dict[str, str] = {}
    for number, pair in enumerate(seeds):
        canonical = f"seed:{number}"
        for name in pair:
            key = normalize_name(name)
            if key:
                output[key] = canonical
    return output


_TEAM_SEEDS = _seed_index(TEAM_ALIAS_SEEDS)
_LEAGUE_SEEDS = _seed_index(LEAGUE_ALIAS_SEEDS)


def canonical_team_key(value: str | None) -> str:
    key = normalize_name(value)
    return _TEAM_SEEDS.get(key, key)


def canonical_league_key(value: str | None) -> str:
    key = normalize_name(value)
    return _LEAGUE_SEEDS.get(key, key)


def qualifiers(event: "Event") -> frozenset[str]:
    """Preserve women/youth/reserve distinctions across fixture sources."""
    source = " ".join((event.home, event.away, event.league)).lower()
    normal = normalize_name(source)
    flags: set[str] = set()
    if "women" in source or "女足" in source or "女子" in source:
        flags.add("women")
    age = re.search(r"u(?:17|19|20|21|23)", normal)
    if age:
        flags.add(age.group(0))
    # Do not treat every occurrence of 青年 as a reserve marker: 青年人 is
    # the established HKJC name for Juventude, while 阿根廷青年人 is the
    # mainland name for Argentinos Juniors.  Require an explicit team marker.
    if (
        re.search(r"青年(?:隊|队|軍|军)", source)
        or any(token in source for token in ("預備", "预备", "后备", "後備", "reserves", "reserve", "jong", " youth"))
    ):
        flags.add("reserve")
    if re.search(r"b(?:隊|队|team)", source, re.I):
        flags.add("reserve")
    return frozenset(flags)


def similarity(left: str | None, right: str | None) -> float:
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Reviewed alias IDs are categorical identities, not natural-language
    # strings.  Different seed numbers must never become fuzzy matches merely
    # because their textual prefixes and digits look alike.
    if a.startswith("seed:") or b.startswith("seed:"):
        return 0.0
    if a in b or b in a:
        return max(0.85, min(len(a), len(b)) / max(len(a), len(b)))
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass(frozen=True)
class Event:
    id: str
    league: str
    home: str
    away: str
    kickoff: datetime
    extra: dict | None = None


@dataclass(frozen=True)
class Match:
    event: Event | None
    reversed: bool
    score: float
    reason: str | None


def _score(
    target: Event,
    candidate: Event,
    reversed_order: bool,
    team_key: Callable[[str | None], str],
    league_key: Callable[[str | None], str],
    require_qualifiers: bool,
    kickoff_tolerance_seconds: int = KICKOFF_TOLERANCE_SECONDS,
) -> tuple[float, float, float]:
    if abs((candidate.kickoff - target.kickoff).total_seconds()) > kickoff_tolerance_seconds:
        return 0.0, 0.0, 0.0
    if require_qualifiers and qualifiers(target) != qualifiers(candidate):
        return 0.0, 0.0, 0.0
    home = similarity(team_key(target.home), team_key(candidate.away if reversed_order else candidate.home))
    away = similarity(team_key(target.away), team_key(candidate.home if reversed_order else candidate.away))
    league = similarity(league_key(target.league), league_key(candidate.league))
    time = max(0.0, 1 - abs((candidate.kickoff - target.kickoff).total_seconds()) / kickoff_tolerance_seconds)
    return 0.65 * ((home + away) / 2) + 0.20 * time + 0.15 * league, min(home, away), league


def _rank(
    target: Event,
    candidates: Iterable[Event],
    *,
    team_key: Callable[[str | None], str],
    league_key: Callable[[str | None], str],
    allow_reversed: bool,
    require_qualifiers: bool,
    kickoff_tolerance_seconds: int,
    strong_names_only: bool = False,
) -> list[tuple[float, float, float, bool, Event]]:
    scored: list[tuple[float, float, float, bool, Event]] = []
    for candidate in candidates:
        for reverse in ((False, True) if allow_reversed else (False,)):
            score, names, league = _score(
                target, candidate, reverse, team_key, league_key, require_qualifiers,
                kickoff_tolerance_seconds,
            )
            if not score:
                continue
            if strong_names_only and (names < STRONG_NAME_FLOOR or league < STRONG_NAME_LEAGUE_FLOOR):
                continue
            scored.append((score, names, league, reverse, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def _decision(
    scored: list[tuple[float, float, float, bool, Event]],
    *,
    accept_score: float,
) -> Match:
    if not scored:
        return Match(None, False, 0.0, "no_candidate_in_kickoff_window")
    score, names, _league, reverse, candidate = scored[0]
    runner = next((item for item in scored[1:] if item[4].id != candidate.id), None)
    if names < NAME_FLOOR:
        return Match(None, False, score, "team_name_similarity_below_floor")
    if score < accept_score:
        return Match(None, False, score, "combined_confidence_below_threshold")
    if runner and score - runner[0] < AMBIGUITY_GAP:
        return Match(None, False, score, "ambiguous_candidate")
    return Match(candidate, reverse, score, None)


def match_event(
    target: Event,
    candidates: Iterable[Event],
    *,
    team_key: Callable[[str | None], str] = normalize_name,
    league_key: Callable[[str | None], str] = normalize_name,
    allow_reversed: bool = True,
    require_qualifiers: bool = False,
) -> Match:
    rows = list(candidates)
    normal = _rank(
        target, rows, team_key=team_key, league_key=league_key,
        allow_reversed=allow_reversed, require_qualifiers=require_qualifiers,
        kickoff_tolerance_seconds=KICKOFF_TOLERANCE_SECONDS,
    )
    decision = _decision(normal, accept_score=ACCEPT_SCORE)
    if decision.event:
        return decision

    # A separately gated fallback handles genuine provider reschedules.  It
    # never widens fuzzy matching: both teams must be near-exact, the league
    # must remain compatible, qualifiers/orientation still apply, and the
    # candidate must remain unique.
    strong = _rank(
        target, rows, team_key=team_key, league_key=league_key,
        allow_reversed=allow_reversed, require_qualifiers=require_qualifiers,
        kickoff_tolerance_seconds=STRONG_NAME_KICKOFF_TOLERANCE_SECONDS,
        strong_names_only=True,
    )
    fallback = _decision(strong, accept_score=STRONG_NAME_ACCEPT_SCORE)
    return fallback if fallback.event or strong else decision


def same_event_for_hkjc(target: Event, candidates: Iterable[Event]) -> Match:
    """HKJC CHL can only use a unique match with both official team IDs present."""
    verified = [
        candidate for candidate in candidates
        if isinstance(candidate.extra, dict)
        and candidate.extra.get("home_team_id")
        and candidate.extra.get("away_team_id")
    ]
    return match_event(target, verified, team_key=canonical_team_key, league_key=canonical_league_key,
                       allow_reversed=False, require_qualifiers=True)


def same_identity_for_hkjc(target: Event, candidates: Iterable[Event]) -> Match:
    """Recognize one exact reversed identity without making its prices usable."""
    reversed_rows = []
    for candidate in candidates:
        extra = candidate.extra if isinstance(candidate.extra, dict) else {}
        if not extra.get("home_team_id") or not extra.get("away_team_id"):
            continue
        if abs((candidate.kickoff - target.kickoff).total_seconds()) > KICKOFF_TOLERANCE_SECONDS:
            continue
        if qualifiers(target) != qualifiers(candidate):
            continue
        if canonical_league_key(target.league) != canonical_league_key(candidate.league):
            continue
        if canonical_team_key(target.home) != canonical_team_key(candidate.away):
            continue
        if canonical_team_key(target.away) != canonical_team_key(candidate.home):
            continue
        reversed_rows.append(candidate)
    if not reversed_rows:
        return Match(None, False, 0.0, "no_exact_reversed_identity")
    unique = {candidate.id: candidate for candidate in reversed_rows}
    if len(unique) != 1:
        return Match(None, False, 1.0, "ambiguous_reversed_identity")
    return Match(next(iter(unique.values())), True, 1.0, None)


@dataclass(frozen=True)
class BridgeMatch:
    """Strict mapping evidence from a Titan event to a PinnAPI event."""

    hkjc: Match
    pinnapi: Match
    path: str
    reason: str | None

    @property
    def event(self) -> Event | None:
        return self.pinnapi.event

    @property
    def reversed(self) -> bool:
        return self.hkjc.reversed or self.pinnapi.reversed


def _script(value: str) -> str:
    han = sum("\u4e00" <= char <= "\u9fff" for char in value)
    latin = sum(char.isascii() and char.isalpha() for char in value)
    if han and not latin:
        return "han"
    if latin and not han:
        return "latin"
    return "mixed"


def _english_hkjc_event(event: Event) -> Event | None:
    extra = event.extra or {}
    home, away = extra.get("home_en"), extra.get("away_en")
    if not home or not away:
        return None
    return Event(event.id, str(extra.get("league_en") or event.league), str(home), str(away), event.kickoff, event.extra)


def bridge_titan_to_pinnapi(titan: Event, hkjc_events: Iterable[Event], pinnapi_events: Iterable[Event]) -> BridgeMatch:
    """Use HKJC bilingual identity as the only cross-script bridge.

    Titan's Chinese fixture names are never compared directly with PinnAPI
    English names.  A direct match is permitted solely when both inputs have
    the same unambiguous script and the normal strict direct-orientation gates
    pass.
    """
    hkjc_rows, pinnapi_rows = list(hkjc_events), list(pinnapi_events)
    hkjc = same_event_for_hkjc(titan, hkjc_rows)
    if hkjc.event:
        english = _english_hkjc_event(hkjc.event)
        if not english:
            return BridgeMatch(hkjc, Match(None, False, 0.0, "hkjc_english_identity_missing"),
                               "none", "hkjc_english_identity_missing")
        pinnapi = match_event(english, pinnapi_rows, allow_reversed=False, require_qualifiers=True)
        if pinnapi.event:
            return BridgeMatch(hkjc, pinnapi, "hkjc_bilingual_bridge", None)
        return BridgeMatch(hkjc, pinnapi, "none", f"hkjc_to_pinnapi:{pinnapi.reason}")

    identity = same_identity_for_hkjc(titan, hkjc_rows)
    if identity.event and identity.reversed:
        # Identity coverage may include a verified reversed fixture, but the
        # pricing bridge deliberately remains empty.  No EV, prediction or bet
        # can be produced until an independently tested reorientation layer
        # swaps H/A and flips the home-perspective handicap sign.
        blocked = Match(None, False, 0.0, "hkjc_orientation_reversed_unpriced")
        return BridgeMatch(identity, blocked, "hkjc_reversed_identity_only",
                           "hkjc_orientation_reversed_unpriced")

    if all(_script(value) in {"han", "latin"} for value in (titan.home, titan.away)):
        same_script = [event for event in pinnapi_rows if _script(event.home) == _script(titan.home)
                       and _script(event.away) == _script(titan.away)]
        direct = match_event(titan, same_script, allow_reversed=False, require_qualifiers=True)
        if direct.event:
            return BridgeMatch(hkjc, direct, "direct_same_script", None)
        return BridgeMatch(hkjc, direct, "none", f"titan_to_hkjc:{hkjc.reason};direct_same_script:{direct.reason}")
    return BridgeMatch(hkjc, Match(None, False, 0.0, "direct_cross_script_forbidden"),
                       "none", f"titan_to_hkjc:{hkjc.reason};direct_cross_script_forbidden")
