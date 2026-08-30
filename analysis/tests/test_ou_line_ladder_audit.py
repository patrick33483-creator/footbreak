from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "ou_line_ladder_audit.py"
SPEC = importlib.util.spec_from_file_location("ou_line_ladder_audit", MODULE_PATH)
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
    over_odds: float,
    under_odds: float,
    captured_at: float,
) -> None:
    origin = "external_opening" if stage == "initial" else "live_observation"
    db.executemany(
        """
        INSERT INTO research_timeline_snapshots
        VALUES ('fixture-1', ?, 'pinnacle', 'OU', ?, ?, ?, ?, ?)
        """,
        [
            (stage, line, "O", over_odds, captured_at, origin),
            (stage, line, "U", under_odds, captured_at, origin),
        ],
    )


def test_follows_exact_line_instead_of_collapsing_stage_ladder() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Portland Timbers', 'Austin FC')"
    )
    # total_goals = 2 + 1 = 3
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 2, 1)")

    add_pair(db, stage="initial", line="2.75", over_odds=1.84, under_odds=1.99, captured_at=1000)
    add_pair(db, stage="T30", line="2.75", over_odds=1.92, under_odds=1.99, captured_at=1100)
    # T5 favours Under, so direction path is O -> O -> U.
    add_pair(db, stage="T5", line="2.75", over_odds=1.97, under_odds=1.93, captured_at=1200)

    # A second T30/T5 ladder line must not replace or invalidate 2.75.
    add_pair(db, stage="T30", line="3", over_odds=1.80, under_odds=2.10, captured_at=1100)
    add_pair(db, stage="T5", line="3", over_odds=1.85, under_odds=2.05, captured_at=1200)

    report = AUDIT.run(db, 1.70)

    assert report["summary"]["complete_line_paths"] == 1
    assert report["summary"]["eligible_line_paths"] == 1
    row = report["examples"][0]
    assert row["line"] == 2.75
    assert row["direction_path"] == "O→O→U"
    assert [row[stage]["odds"] for stage in AUDIT.STAGES] == [1.84, 1.92, 1.93]


def test_parses_split_handicap_as_midpoint() -> None:
    assert AUDIT.line_number("2.5/3") == 2.75
    assert AUDIT.line_number("2/2.5") == 2.25


def test_equal_odds_stage_is_level_and_still_needs_threshold() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-2', 2000, 'Home Team', 'Away Team')"
    )
    # total_goals = 1 + 2 = 3, exactly on a whole line -> true push.
    db.execute("INSERT INTO research_results VALUES ('fixture-2', 1, 2)")

    def add(match_id, stage, line, over_odds, under_odds, captured_at):
        origin = "external_opening" if stage == "initial" else "live_observation"
        db.executemany(
            "INSERT INTO research_timeline_snapshots VALUES (?, ?, 'pinnacle', 'OU', ?, ?, ?, ?, ?)",
            [
                (match_id, stage, line, "O", over_odds, captured_at, origin),
                (match_id, stage, line, "U", under_odds, captured_at, origin),
            ],
        )

    # Level (平) at every stage: over odds == under odds == 3.0 line.
    add("fixture-2", "initial", "3", 1.95, 1.95, 1000)
    add("fixture-2", "T30", "3", 1.90, 1.90, 1100)
    add("fixture-2", "T5", "3", 1.98, 1.98, 1200)

    report = AUDIT.run(db, 1.70)
    row = next(r for r in report["examples"] if r["fixture_id"] == "fixture-2")
    assert row["direction_path"] == "D→D→D"
    # 3 total goals against a level (3.0) line is an exact push.
    assert row["t5_level_result"] == "走盤"
    assert row["settlement"] is None
    assert row["unit_return"] is None


def test_level_stage_below_threshold_is_excluded() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-3', 2000, 'Home Team', 'Away Team')"
    )
    db.execute("INSERT INTO research_results VALUES ('fixture-3', 2, 0)")

    def add(match_id, stage, line, over_odds, under_odds, captured_at):
        origin = "external_opening" if stage == "initial" else "live_observation"
        db.executemany(
            "INSERT INTO research_timeline_snapshots VALUES (?, ?, 'pinnacle', 'OU', ?, ?, ?, ?, ?)",
            [
                (match_id, stage, line, "O", over_odds, captured_at, origin),
                (match_id, stage, line, "U", under_odds, captured_at, origin),
            ],
        )

    add("fixture-3", "initial", "3", 1.60, 1.60, 1000)  # below threshold
    add("fixture-3", "T30", "3", 1.90, 1.90, 1100)
    add("fixture-3", "T5", "3", 1.98, 1.98, 1200)

    report = AUDIT.run(db, 1.70)
    assert not any(r["fixture_id"] == "fixture-3" for r in report["examples"])
    assert report["summary"]["complete_line_paths"] == 1
    assert report["summary"]["eligible_line_paths"] == 0


def test_actual_cover_side_helper() -> None:
    assert AUDIT.actual_cover_side(3.0, 3) == "push"
    assert AUDIT.actual_cover_side(2.5, 3) == "O"
    assert AUDIT.actual_cover_side(2.5, 2) == "U"
    assert AUDIT.actual_cover_side(2.75, 2) == "U"
    assert AUDIT.actual_cover_side(2.75, 3) == "O"


def test_path_probability_ignores_selected_direction_hit_or_miss() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')"
    )
    # total_goals = 2 + 1 = 3 -> actual_cover_side(2.75, 3) == "O" regardless
    # of which side the T5 quote favoured.
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 2, 1)")
    add_pair(db, stage="initial", line="2.75", over_odds=1.84, under_odds=1.99, captured_at=1000)
    add_pair(db, stage="T30", line="2.75", over_odds=1.92, under_odds=1.99, captured_at=1100)
    # T5 favours Under, so direction_path is O→O→U, but the actual result
    # still leans Over.
    add_pair(db, stage="T5", line="2.75", over_odds=1.97, under_odds=1.93, captured_at=1200)

    report = AUDIT.run(db, 1.70)
    row = next(
        r for r in report["path_probability"]
        if r["provider"] == "pinnacle" and r["direction_path"] == "O→O→U"
    )
    assert row["settled"] == 1
    assert row["counts"]["大球開出"] == 1
    assert row["probability"]["大球開出"] == 1.0
    assert row["probability"]["小球開出"] == 0.0


def test_line_path_probability_slices_by_exact_line() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')"
    )
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 2, 1)")
    add_pair(db, stage="initial", line="2.5", over_odds=1.84, under_odds=1.99, captured_at=1000)
    add_pair(db, stage="T30", line="2.5", over_odds=1.90, under_odds=1.99, captured_at=1100)
    add_pair(db, stage="T5", line="2.5", over_odds=1.90, under_odds=1.95, captured_at=1200)

    report = AUDIT.run(db, 1.70)
    row = next(
        r for r in report["line_path_probability"]
        if r["provider"] == "pinnacle" and r["line"] == 2.5 and r["direction_path"] == "O→O→O"
    )
    assert row["settled"] == 1
    assert row["counts"]["大球開出"] == 1


def test_odds_drift_breakdown_buckets_initial_to_t5_gap() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')"
    )
    # total_goals = 3 -> actual_cover_side(2.5, 3) == "O".
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 2, 1)")
    # Over odds shorten from 1.95 (initial) to 1.80 (T5): gap = 0.15 -> "收縮0.10-0.20".
    # Over stays cheaper than Under at every stage, so the path is O→O→O.
    add_pair(db, stage="initial", line="2.5", over_odds=1.95, under_odds=2.05, captured_at=1000)
    add_pair(db, stage="T30", line="2.5", over_odds=1.88, under_odds=1.95, captured_at=1100)
    add_pair(db, stage="T5", line="2.5", over_odds=1.80, under_odds=2.00, captured_at=1200)

    report = AUDIT.run(db, 1.70)
    row = next(
        r for r in report["odds_drift_breakdown"]
        if r["provider"] == "pinnacle"
        and r["direction_path"] == "O→O→O"
        and r["bucket"] == "收縮0.10-0.20"
    )
    assert row["observations"] == 1
    assert row["avg_gap"] == 0.15
    assert row["avg_initial_odds"] == 1.95
    assert row["avg_t5_odds"] == 1.80
    assert row["counts"]["大球開出"] == 1

    outcome_row = next(
        r for r in report["drift_by_outcome"]
        if r["provider"] == "pinnacle"
        and r["direction_path"] == "O→O→O"
        and r["actual_result"] == "大球開出"
    )
    assert outcome_row["observations"] == 1
    assert outcome_row["avg_gap"] == 0.15


def test_margin_and_odds_level_summary_report_per_provider_per_stage() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')"
    )
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 2, 1)")
    # over_odds=1.90, under_odds=1.95 -> margin = 1/1.90 + 1/1.95 - 1.
    add_pair(db, stage="initial", line="2.5", over_odds=1.90, under_odds=1.95, captured_at=1000)
    add_pair(db, stage="T30", line="2.5", over_odds=1.85, under_odds=1.95, captured_at=1100)
    add_pair(db, stage="T5", line="2.5", over_odds=1.80, under_odds=2.00, captured_at=1200)

    report = AUDIT.run(db, 1.70)
    margin_row = next(
        r for r in report["margin_summary"]
        if r["provider"] == "pinnacle" and r["stage"] == "initial"
    )
    expected_margin_pct = round((1 / 1.90 + 1 / 1.95 - 1) * 100, 3)
    assert margin_row["avg_margin_pct"] == expected_margin_pct

    odds_row = next(
        r for r in report["odds_level_summary"]
        if r["provider"] == "pinnacle" and r["stage"] == "T5"
    )
    assert odds_row["avg_selected_odds"] == 1.80


def test_classify_signal_thresholds() -> None:
    classify_signal = AUDIT.classify_signal
    assert classify_signal(12.0, 20) == "強烈訊號:跟隨該側"
    assert classify_signal(7.0, 20) == "輕微訊號:可留意"
    assert classify_signal(2.0, 20) == "訊號不明顯"
    assert classify_signal(-15.0, 20) == "反向訊號:避免/反向"
    assert classify_signal(20.0, 5) == "樣本不足,僅供參考"
    assert classify_signal(None, 20) == "無法評估(未有隱含機率)"


def test_composite_signal_table_matches_drift_breakdown() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')"
    )
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 2, 1)")
    add_pair(db, stage="initial", line="2.5", over_odds=1.98, under_odds=1.90, captured_at=1000)
    add_pair(db, stage="T30", line="2.5", over_odds=1.85, under_odds=1.95, captured_at=1100)
    add_pair(db, stage="T5", line="2.5", over_odds=1.80, under_odds=2.00, captured_at=1200)

    report = AUDIT.run(db, 1.70)
    row = next(
        r for r in report["composite_signal_table"]
        if r["provider"] == "pinnacle" and r["direction_path"] == "U→O→O"
    )
    assert row["backed_side"] == "大球開出"
    assert row["avg_t5_odds"] == 1.80
    assert row["observations"] == 1
    # Below the minimum-sample threshold -> flagged as reference-only regardless of edge.
    assert row["signal"] == "樣本不足,僅供參考"


def test_odds_drift_breakdown_handles_side_switch_paths() -> None:
    db = make_db()
    db.execute(
        "INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')"
    )
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 2, 1)")
    # initial selects Under (cheaper), but T30/T5 flip to Over -> path U→O→O.
    # T5 side is O, so the gap must compare O's price at initial (1.98, via
    # other_odds) against O's price at T5 (1.80): gap = 0.18.
    add_pair(db, stage="initial", line="2.5", over_odds=1.98, under_odds=1.90, captured_at=1000)
    add_pair(db, stage="T30", line="2.5", over_odds=1.85, under_odds=1.95, captured_at=1100)
    add_pair(db, stage="T5", line="2.5", over_odds=1.80, under_odds=2.00, captured_at=1200)

    report = AUDIT.run(db, 1.70)
    row = next(
        r for r in report["odds_drift_breakdown"]
        if r["provider"] == "pinnacle" and r["direction_path"] == "U→O→O"
    )
    assert row["avg_gap"] == 0.18
    assert row["avg_initial_odds"] == 1.98
    assert row["avg_t5_odds"] == 1.80


def test_data_availability_distinguishes_line_movement_and_missing_stages() -> None:
    db = make_db()
    # fixture-1: same line at all 3 stages, above threshold -> counts everywhere.
    db.execute("INSERT INTO matches VALUES ('fixture-1', 2000, 'Home Team', 'Away Team')")
    db.execute("INSERT INTO research_results VALUES ('fixture-1', 2, 1)")
    add_pair(db, stage="initial", line="2.5", over_odds=1.84, under_odds=1.99, captured_at=1000)
    add_pair(db, stage="T30", line="2.5", over_odds=1.90, under_odds=1.99, captured_at=1100)
    add_pair(db, stage="T5", line="2.5", over_odds=1.90, under_odds=1.95, captured_at=1200)

    # fixture-2: pinnacle quotes at all 3 stages but the line MOVES
    # (2.75 -> 2.5 -> 2.5), so it has data at all 3 stages but not on
    # one consistent line.
    db.execute("INSERT INTO matches VALUES ('fixture-2', 2000, 'Home Two', 'Away Two')")
    db.execute("INSERT INTO research_results VALUES ('fixture-2', 1, 1)")
    db.executemany(
        """
        INSERT INTO research_timeline_snapshots
        VALUES ('fixture-2', ?, 'pinnacle', 'OU', ?, ?, ?, ?, ?)
        """,
        [
            ("initial", "2.75", "O", 1.90, 1000, "external_opening"),
            ("initial", "2.75", "U", 1.95, 1000, "external_opening"),
            ("T30", "2.5", "O", 1.88, 1100, "live_observation"),
            ("T30", "2.5", "U", 1.97, 1100, "live_observation"),
            ("T5", "2.5", "O", 1.85, 1200, "live_observation"),
            ("T5", "2.5", "U", 1.99, 1200, "live_observation"),
        ],
    )

    # fixture-3: pinnacle only has initial + T30 quotes (missing T5 entirely).
    db.execute("INSERT INTO matches VALUES ('fixture-3', 2000, 'Home Three', 'Away Three')")
    db.executemany(
        """
        INSERT INTO research_timeline_snapshots
        VALUES ('fixture-3', ?, 'pinnacle', 'OU', ?, ?, ?, ?, ?)
        """,
        [
            ("initial", "3", "O", 1.90, 1000, "external_opening"),
            ("initial", "3", "U", 1.95, 1000, "external_opening"),
            ("T30", "3", "O", 1.88, 1100, "live_observation"),
            ("T30", "3", "U", 1.97, 1100, "live_observation"),
        ],
    )

    report = AUDIT.run(db, 1.70)
    avail = report["data_availability"]["pinnacle"]
    assert avail["raw_any_ou_quote_fixtures"] == 3  # fixture-1, fixture-2, fixture-3
    assert avail["all_three_stages_any_line_fixtures"] == 2  # fixture-1 and fixture-2
    assert avail["same_line_all_three_stages_fixtures"] == 1  # fixture-1 only (fixture-2's line moved)
    assert avail["eligible_after_threshold_fixtures"] == 1  # fixture-1 only

    hkjc_avail = report["data_availability"]["hkjc"]
    assert hkjc_avail["raw_any_ou_quote_fixtures"] == 0

    pattern = report["stage_pattern_breakdown"]["pinnacle"]["counts"]
    assert pattern["齊三個時點"] == 2  # fixture-1 and fixture-2 (line moved but all 3 stages present)
    assert pattern["缺:T5"] == 1  # fixture-3 is missing only T5
