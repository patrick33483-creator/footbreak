from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "ah_line_ladder_audit.py"
SPEC = importlib.util.spec_from_file_location("ah_line_ladder_audit", MODULE_PATH)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def make_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE matches (
            id TEXT PRIMARY KEY,
            kickoff_utc REAL,
            home_team TEXT,
            away_team TEXT
        );
        CREATE TABLE research_timeline_snapshots (
            match_id TEXT,
            stage TEXT,
            provider TEXT,
            market TEXT,
            line_key TEXT,
            selection TEXT,
            decimal_odds REAL,
            captured_at REAL,
            origin TEXT
        );
        CREATE TABLE research_results (
            match_id TEXT PRIMARY KEY,
            home_score INTEGER,
            away_score INTEGER
        );
        CREATE TABLE results (
            match_id TEXT PRIMARY KEY,
            home_score INTEGER,
            away_score INTEGER
        );
        """
    )
    return db


def add_pair(
    db: sqlite3.Connection,
    *,
    stage: str,
    line: str,
    home_odds: float,
    away_odds: float,
    captured_at: float,
) -> None:
    origin = "external_opening" if stage == "initial" else "live_observation"
    db.executemany(
        """
        INSERT INTO research_timeline_snapshots
        VALUES ('fixture-1', ?, 'pinnacle', 'AH', ?, ?, ?, ?, ?)
        """,
        [
            (stage, line, "H", home_odds, captured_at, origin),
            (stage, line, "A", away_odds, captured_at, origin),
        ],
    )


def test_follows_exact_line_instead_of_collapsing_stage_ladder() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Portland Timbers', 'Austin FC')"
    )
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 0, 1)")

    add_pair(
        db,
        stage="initial",
        line="-0.75",
        home_odds=1.84,
        away_odds=1.99,
        captured_at=1000,
    )
    add_pair(
        db,
        stage="T30",
        line="-0.75",
        home_odds=1.92,
        away_odds=1.99,
        captured_at=1100,
    )
    add_pair(
        db,
        stage="T5",
        line="-0.75",
        home_odds=1.97,
        away_odds=1.93,
        captured_at=1200,
    )

    # A second T30/T5 ladder line must not replace or invalidate -0.75.
    add_pair(
        db,
        stage="T30",
        line="0",
        home_odds=1.80,
        away_odds=2.10,
        captured_at=1100,
    )
    add_pair(
        db,
        stage="T5",
        line="0",
        home_odds=1.85,
        away_odds=2.05,
        captured_at=1200,
    )

    report = AUDIT.run(db, 1.70)

    assert report["summary"]["complete_line_paths"] == 1
    assert report["summary"]["eligible_line_paths"] == 1
    row = report["examples"][0]
    assert row["line"] == -0.75
    assert row["direction_path"] == "H→H→A"
    assert [row[stage]["odds"] for stage in AUDIT.STAGES] == [1.84, 1.92, 1.93]


def test_parses_split_handicap_as_midpoint() -> None:
    assert AUDIT.line_number("-0.5/-1") == -0.75
    assert AUDIT.line_number("0/0.5") == 0.25
