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


def test_equal_odds_stage_is_level_and_still_needs_threshold() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-2', 2000, 'Home Team', 'Away Team')"
    )
    db.execute("INSERT INTO research_results VALUES ('fixture-2', 1, 1)")

    def add(match_id, stage, line, home_odds, away_odds, captured_at):
        origin = "external_opening" if stage == "initial" else "live_observation"
        db.executemany(
            "INSERT INTO research_timeline_snapshots VALUES (?, ?, 'pinnacle', 'AH', ?, ?, ?, ?, ?)",
            [
                (match_id, stage, line, "H", home_odds, captured_at, origin),
                (match_id, stage, line, "A", away_odds, captured_at, origin),
            ],
        )

    # Level (平) at every stage: home odds == away odds == 0.0 handicap.
    add("fixture-2", "initial", "0", 1.95, 1.95, 1000)
    add("fixture-2", "T30", "0", 1.90, 1.90, 1100)
    add("fixture-2", "T5", "0", 1.98, 1.98, 1200)

    report = AUDIT.run(db, 1.70)
    row = next(r for r in report["examples"] if r["fixture_id"] == "fixture-2")
    assert row["direction_path"] == "D→D→D"
    # 1-1 draw against a level (0.0) line is an exact push.
    assert row["t5_level_result"] == "走盤"
    assert row["settlement"] is None
    assert row["unit_return"] is None


def test_level_stage_below_threshold_is_excluded() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-3', 2000, 'Home Team', 'Away Team')"
    )
    db.execute("INSERT INTO research_results VALUES ('fixture-3', 2, 0)")

    def add(match_id, stage, line, home_odds, away_odds, captured_at):
        origin = "external_opening" if stage == "initial" else "live_observation"
        db.executemany(
            "INSERT INTO research_timeline_snapshots VALUES (?, ?, 'pinnacle', 'AH', ?, ?, ?, ?, ?)",
            [
                (match_id, stage, line, "H", home_odds, captured_at, origin),
                (match_id, stage, line, "A", away_odds, captured_at, origin),
            ],
        )

    add("fixture-3", "initial", "0", 1.60, 1.60, 1000)  # below threshold
    add("fixture-3", "T30", "0", 1.90, 1.90, 1100)
    add("fixture-3", "T5", "0", 1.98, 1.98, 1200)

    report = AUDIT.run(db, 1.70)
    assert not any(r["fixture_id"] == "fixture-3" for r in report["examples"])
    assert report["summary"]["complete_line_paths"] == 1
    assert report["summary"]["eligible_line_paths"] == 0


def test_actual_cover_side_helper() -> None:
    assert AUDIT.actual_cover_side(0.0, 1, 1) == "push"
    assert AUDIT.actual_cover_side(0.0, 2, 1) == "H"
    assert AUDIT.actual_cover_side(0.0, 1, 2) == "A"
    assert AUDIT.actual_cover_side(-0.75, 1, 1) == "A"
    assert AUDIT.actual_cover_side(-0.75, 2, 1) == "H"


def test_path_probability_ignores_selected_direction_hit_or_miss() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')"
    )
    # Final score: home wins by 2 -> home covers a -0.75 line regardless of
    # which side the T5 quote favoured.
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 2, 0)")
    add_pair(db, stage="initial", line="-0.75", home_odds=1.84, away_odds=1.99, captured_at=1000)
    add_pair(db, stage="T30", line="-0.75", home_odds=1.92, away_odds=1.99, captured_at=1100)
    # T5 favours the away side, so direction_path is H→H→A, but the actual
    # result still covers home.
    add_pair(db, stage="T5", line="-0.75", home_odds=1.97, away_odds=1.93, captured_at=1200)

    report = AUDIT.run(db, 1.70)
    row = next(
        r for r in report["path_probability"]
        if r["provider"] == "pinnacle" and r["direction_path"] == "H→H→A"
    )
    assert row["settled"] == 1
    assert row["counts"]["主開出"] == 1
    assert row["probability"]["主開出"] == 1.0
    assert row["probability"]["客開出"] == 0.0


def test_line_path_probability_slices_by_exact_line() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')"
    )
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 2, 0)")
    add_pair(db, stage="initial", line="-0.25", home_odds=1.84, away_odds=1.99, captured_at=1000)
    add_pair(db, stage="T30", line="-0.25", home_odds=1.90, away_odds=1.99, captured_at=1100)
    add_pair(db, stage="T5", line="-0.25", home_odds=1.90, away_odds=1.95, captured_at=1200)

    report = AUDIT.run(db, 1.70)
    row = next(
        r for r in report["line_path_probability"]
        if r["provider"] == "pinnacle" and r["line"] == -0.25 and r["direction_path"] == "H→H→H"
    )
    assert row["settled"] == 1
    assert row["counts"]["主開出"] == 1


def test_data_availability_distinguishes_line_movement_and_missing_stages() -> None:
    db = make_db()
    # fixture-1: same line at all 3 stages, above threshold -> counts everywhere.
    db.execute("INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')")
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 1, 0)")
    add_pair(db, stage="initial", line="-0.25", home_odds=1.84, away_odds=1.99, captured_at=1000)
    add_pair(db, stage="T30", line="-0.25", home_odds=1.90, away_odds=1.99, captured_at=1100)
    add_pair(db, stage="T5", line="-0.25", home_odds=1.90, away_odds=1.95, captured_at=1200)

    # fixture-2: pinnacle quotes at all 3 stages but the line MOVES
    # (-0.5 -> -0.25 -> -0.25), so it has data at all 3 stages but not on
    # one consistent line.
    db.execute("INSERT INTO matches VALUES ('fixture-2', 2000, 'Home Two', 'Away Two')")
    db.execute("INSERT INTO research_results VALUES ('fixture-2', 1, 1)")
    db.executemany(
        """
        INSERT INTO research_timeline_snapshots
        VALUES ('fixture-2', ?, 'pinnacle', 'AH', ?, ?, ?, ?, ?)
        """,
        [
            ("initial", "-0.5", "H", 1.90, 1000, "external_opening"),
            ("initial", "-0.5", "A", 1.95, 1000, "external_opening"),
            ("T30", "-0.25", "H", 1.88, 1100, "live_observation"),
            ("T30", "-0.25", "A", 1.97, 1100, "live_observation"),
            ("T5", "-0.25", "H", 1.85, 1200, "live_observation"),
            ("T5", "-0.25", "A", 1.99, 1200, "live_observation"),
        ],
    )

    # fixture-3: pinnacle only has initial + T30 quotes (missing T5 entirely).
    db.execute("INSERT INTO matches VALUES ('fixture-3', 2000, 'Home Three', 'Away Three')")
    db.executemany(
        """
        INSERT INTO research_timeline_snapshots
        VALUES ('fixture-3', ?, 'pinnacle', 'AH', ?, ?, ?, ?, ?)
        """,
        [
            ("initial", "0", "H", 1.90, 1000, "external_opening"),
            ("initial", "0", "A", 1.95, 1000, "external_opening"),
            ("T30", "0", "H", 1.88, 1100, "live_observation"),
            ("T30", "0", "A", 1.97, 1100, "live_observation"),
        ],
    )

    report = AUDIT.run(db, 1.70)
    avail = report["data_availability"]["pinnacle"]
    assert avail["raw_any_ah_quote_fixtures"] == 3  # fixture-1, fixture-2, fixture-3
    assert avail["all_three_stages_any_line_fixtures"] == 2  # fixture-1 and fixture-2
    assert avail["same_line_all_three_stages_fixtures"] == 1  # fixture-1 only (fixture-2's line moved)
    assert avail["eligible_after_threshold_fixtures"] == 1  # fixture-1 only

    hkjc_avail = report["data_availability"]["hkjc"]
    assert hkjc_avail["raw_any_ah_quote_fixtures"] == 0

    pattern = report["stage_pattern_breakdown"]["pinnacle"]["counts"]
    assert pattern["齊三個時點"] == 2  # fixture-1 and fixture-2 (line moved but all 3 stages present)
    assert pattern["缺:T5"] == 1  # fixture-3 is missing only T5


def test_classify_signal_thresholds() -> None:
    classify_signal = AUDIT.classify_signal
    assert classify_signal(12.0, 20) == "強烈訊號:跟隨該側"
    assert classify_signal(7.0, 20) == "輕微訊號:可留意"
    assert classify_signal(2.0, 20) == "訊號不明顯"
    assert classify_signal(-15.0, 20) == "反向訊號:避免/反向"
    assert classify_signal(20.0, 5) == "樣本不足,僅供參考"
    assert classify_signal(None, 20) == "無法評估(未有隱含機率)"


def test_odds_drift_breakdown_buckets_initial_to_t5_gap() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')"
    )
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 2, 0)")
    # Pure H→H→H path: home is cheaper (selected) at every stage, and its price
    # shortens 1.95→1.80 (gap 0.15 -> bucket 0.10-0.20).
    add_pair(db, stage="initial", line="0", home_odds=1.95, away_odds=2.05, captured_at=1000)
    add_pair(db, stage="T30", line="0", home_odds=1.88, away_odds=1.98, captured_at=1100)
    add_pair(db, stage="T5", line="0", home_odds=1.80, away_odds=1.90, captured_at=1200)

    report = AUDIT.run(db, 1.70)
    row = next(
        r for r in report["odds_drift_breakdown"]
        if r["provider"] == "pinnacle" and r["direction_path"] == "H→H→H"
    )
    assert row["bucket"] == "收縮0.10-0.20"
    assert row["avg_gap"] == 0.15
    assert row["avg_initial_odds"] == 1.95
    assert row["avg_t5_odds"] == 1.80


def test_odds_drift_breakdown_handles_side_switch_paths() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')"
    )
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 0, 2)")
    # initial selects Home (cheaper), T30/T5 flip to Away -> path H→A→A.
    # T5 side is A, so the gap must compare A's price at initial (1.98, via
    # other_odds) against A's price at T5 (1.80): gap = 0.18.
    add_pair(db, stage="initial", line="0", home_odds=1.90, away_odds=1.98, captured_at=1000)
    add_pair(db, stage="T30", line="0", home_odds=1.95, away_odds=1.85, captured_at=1100)
    add_pair(db, stage="T5", line="0", home_odds=2.00, away_odds=1.80, captured_at=1200)

    report = AUDIT.run(db, 1.70)
    row = next(
        r for r in report["odds_drift_breakdown"]
        if r["provider"] == "pinnacle" and r["direction_path"] == "H→A→A"
    )
    assert row["avg_gap"] == 0.18
    assert row["avg_initial_odds"] == 1.98
    assert row["avg_t5_odds"] == 1.80


def test_composite_signal_table_matches_drift_breakdown() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')"
    )
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 0, 2)")
    add_pair(db, stage="initial", line="0", home_odds=1.90, away_odds=1.98, captured_at=1000)
    add_pair(db, stage="T30", line="0", home_odds=1.95, away_odds=1.85, captured_at=1100)
    add_pair(db, stage="T5", line="0", home_odds=2.00, away_odds=1.80, captured_at=1200)

    report = AUDIT.run(db, 1.70)
    row = next(
        r for r in report["composite_signal_table"]
        if r["provider"] == "pinnacle" and r["direction_path"] == "H→A→A"
    )
    assert row["backed_side"] == "客開出"
    assert row["avg_t5_odds"] == 1.80
    assert row["observations"] == 1
    # Below the minimum-sample threshold -> flagged as reference-only regardless of edge.
    assert row["signal"] == "樣本不足,僅供參考"
