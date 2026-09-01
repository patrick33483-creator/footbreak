from __future__ import annotations

from stage_engine_v2.segmented_conditions import build_segmented_conditions


def _stage(code: str, side: str, line: float, odds: float, label: str) -> dict:
    return {
        "lead_market": code,
        "lead_code": code,
        "lead_side": side,
        "lead_line": line,
        "lead_label": label,
        "lead_odds": odds,
        "predicted_at_utc": "2026-09-01T11:55:00+00:00",
    }


def _slot(match_id: str, stages: dict) -> dict:
    return {
        "id": match_id,
        "league": "測試聯賽",
        "home": f"{match_id} 主隊",
        "away": f"{match_id} 客隊",
        "kickoff_hkt": "2026-09-01T20:00:00+08:00",
        "kickoff_utc": "2026-09-01T12:00:00+00:00",
        "stages": stages,
    }


def _history(
    match_id: str,
    stage: str,
    code: str,
    side: str,
    line: float,
    settlement: str,
) -> dict:
    return {
        "match_id": match_id,
        "stage": stage,
        "home": f"{match_id} 主隊",
        "away": f"{match_id} 客隊",
        "kickoff": "2026-09-01T20:00:00+08:00",
        "predicted_at": "2026-09-01T19:55:00+08:00",
        "score": "2-1",
        "result_status": "已核對",
        "market_grades": [{
            "code": code,
            "side": side,
            "line": line,
            "odds": 1.9,
            "grade_status": "GRADED",
            "settlement": settlement,
        }],
    }


def test_projects_prediction_direction_conditions_and_settles_results() -> None:
    ledger = {"fixtures": {
        "over": _slot("over", {
            "首預": _stage("HIL", "H", 2.75, 1.81, "O 2.75"),
            "T-5": _stage("HIL", "H", 3.0, 1.90, "O 3.0"),
        }),
        "away": _slot("away", {
            # HDC line is the home-team line; selected away line is therefore -0.5.
            "首預": _stage("HDC", "A", 0.5, 1.88, "away 客隊"),
        }),
        "home": _slot("home", {
            "首預": _stage("HDC", "H", -0.25, 1.75, "home 主隊"),
            "T-30": _stage("HDC", "H", -0.25, 1.82, "home 主隊"),
            "T-5": _stage("HDC", "H", -0.25, 1.91, "home 主隊"),
        }),
    }}
    history = [
        _history("over", "T-5", "HIL", "H", 3.0, "Won"),
        _history("away", "首預", "HDC", "A", 0.5, "Lost"),
        _history("home", "T-5", "HDC", "H", -0.25, "Half Won"),
    ]

    payload = build_segmented_conditions(ledger, history)
    conditions = {item["id"]: item for item in payload["public_conditions"]}

    assert payload["direction_basis"] == "model_prediction_not_quote_direction"
    assert payload["public_tiers"] == ["觀察", "普通"]
    assert conditions["S-HIL-T5-OVER-185"]["prospective"]["qualified"] == 1
    assert conditions["S-HIL-T5-OVER-185"]["prospective"]["hit_rate"] == 1.0
    assert conditions["S-HIL-T5-OVER-185"]["prospective"]["roi"] == 0.9
    assert conditions["WATCH-HIL-T5-OVER-180"]["prospective"]["qualified"] == 1
    assert conditions["WATCH-HIL-T5-OVER-180"]["evidence_label"] == "大型樣本觀察"
    assert conditions["A-HIL-OPEN-T5-OVER-180"]["prospective"]["qualified"] == 1
    assert conditions["A-HDC-OPEN-AWAY-MINUS-050"]["prospective"]["full_loss"] == 1
    # A-HDC-HHH-SAME-LINE was downgraded to background tier and no longer
    # appears in the public list; it still evaluates in matching_observations
    # for continued measurement.
    assert "A-HDC-HHH-SAME-LINE" not in conditions
    hhh_obs = [
        row for row in payload["matching_observations"]
        if row["condition_id"] == "A-HDC-HHH-SAME-LINE"
    ]
    assert len(hhh_obs) == 1
    assert hhh_obs[0]["settlement"] == "Half Won"
    assert hhh_obs[0]["tier"] == "背景"
    assert hhh_obs[0]["evidence_status"] == "downgraded"


def test_strict_threshold_same_line_and_activation_cutoff() -> None:
    ledger = {"fixtures": {
        "exact": _slot("exact", {
            "T-5": _stage("HIL", "H", 2.5, 1.85, "O 2.5"),
        }),
        "exact-180": _slot("exact-180", {
            "T-5": _stage("HIL", "H", 2.5, 1.80, "O 2.5"),
        }),
        "changed": _slot("changed", {
            "首預": _stage("HDC", "H", -0.25, 1.8, "主"),
            "T-30": _stage("HDC", "H", -0.5, 1.8, "主"),
            "T-5": _stage("HDC", "H", -0.25, 1.8, "主"),
        }),
        "old": {
            **_slot("old", {"T-5": _stage("HIL", "H", 2.5, 2.0, "O 2.5")}),
            "kickoff_hkt": "2026-08-30T20:00:00+08:00",
            "kickoff_utc": "2026-08-30T12:00:00+00:00",
        },
        "pre-activation-decision": _slot("pre-activation-decision", {
            "T-5": {
                **_stage("HIL", "H", 2.5, 2.0, "O 2.5"),
                "predicted_at_utc": "2026-08-30T15:59:59+00:00",
            },
        }),
        "missing-decision-time": _slot("missing-decision-time", {
            "T-5": {
                key: value
                for key, value in _stage("HIL", "H", 2.5, 2.0, "O 2.5").items()
                if key != "predicted_at_utc"
            },
        }),
    }}

    payload = build_segmented_conditions(ledger)
    conditions = {item["id"]: item for item in payload["public_conditions"]}
    assert conditions["S-HIL-T5-OVER-185"]["prospective"]["qualified"] == 0
    assert conditions["WATCH-HIL-T5-OVER-180"]["prospective"]["qualified"] == 1
    # A-HDC-HHH-SAME-LINE was downgraded to background tier; verify it no
    # longer surfaces in the public list even when a fixture would match.
    assert "A-HDC-HHH-SAME-LINE" not in conditions


def test_old_labels_are_safely_parsed_without_rewriting_ledger() -> None:
    ledger = {"fixtures": {
        "legacy": _slot("legacy", {
            "首預": {
                "lead_market": "OU", "lead_label": "over 2.5", "lead_odds": 1.81,
                "predicted_at_utc": "2026-09-01T10:00:00+00:00",
            },
            "T-5": {
                "lead_market": "入球大細", "lead_label": "O 3.0", "lead_odds": 1.90,
                "predicted_at_utc": "2026-09-01T11:55:00+00:00",
            },
        }),
    }}
    before = repr(ledger)
    payload = build_segmented_conditions(ledger)
    conditions = {item["id"]: item for item in payload["public_conditions"]}
    assert conditions["S-HIL-T5-OVER-185"]["prospective"]["qualified"] == 1
    assert conditions["A-HIL-OPEN-T5-OVER-180"]["prospective"]["qualified"] == 1
    assert repr(ledger) == before


def test_old_chinese_handicap_label_uses_displayed_selected_line() -> None:
    ledger = {"fixtures": {
        "legacy-away": _slot("legacy-away", {
            "首預": {
                "lead_market": "讓球",
                "lead_label": "皇冠讓球 客 -0.5",
                "lead_odds": 1.9,
                "predicted_at_utc": "2026-09-01T10:00:00+00:00",
            },
        }),
    }}
    payload = build_segmented_conditions(ledger)
    condition = next(
        row for row in payload["public_conditions"]
        if row["id"] == "A-HDC-OPEN-AWAY-MINUS-050"
    )
    assert condition["prospective"]["qualified"] == 1
    assert condition["observations"][0]["selected_line"] == -0.5


def test_split_line_label_uses_midpoint_and_preserves_negative_sign() -> None:
    ledger = {"fixtures": {
        "split": _slot("split", {
            "首預": {
                "lead_market": "讓球", "lead_label": "皇冠讓球 主 -0/0.5",
                "lead_odds": 1.9, "predicted_at_utc": "2026-09-01T10:00:00+00:00",
            },
            "T-30": {
                "lead_market": "讓球", "lead_label": "皇冠讓球 主 -0.25",
                "lead_odds": 1.9, "predicted_at_utc": "2026-09-01T11:30:00+00:00",
            },
            "T-5": {
                "lead_market": "讓球", "lead_label": "皇冠讓球 主 -0/0.5",
                "lead_odds": 1.9, "predicted_at_utc": "2026-09-01T11:55:00+00:00",
            },
        }),
    }}
    payload = build_segmented_conditions(ledger)
    # A-HDC-HHH-SAME-LINE is now background-only: still evaluates the split
    # HDC labels, but only surfaces in matching_observations rather than the
    # public list.
    matches = [
        row for row in payload["matching_observations"]
        if row["condition_id"] == "A-HDC-HHH-SAME-LINE"
    ]
    assert len(matches) == 1
    assert matches[0]["selected_line"] == -0.25
    assert not any(
        row["id"] == "A-HDC-HHH-SAME-LINE"
        for row in payload["public_conditions"]
    )


def test_s_hil_open_over_3_180_matches_only_within_thresholds() -> None:
    ledger = {"fixtures": {
        "match": _slot("match", {
            "首預": _stage("HIL", "H", 3.0, 1.85, "O 3.0"),
            "T-5": _stage("HIL", "H", 3.0, 1.85, "O 3.0"),
        }),
        "under-line": _slot("under-line", {
            "首預": _stage("HIL", "H", 2.75, 1.85, "O 2.75"),
        }),
        "under-odds": _slot("under-odds", {
            "首預": _stage("HIL", "H", 3.0, 1.80, "O 3.0"),
        }),
        "wrong-side": _slot("wrong-side", {
            "首預": _stage("HIL", "A", 3.0, 1.85, "U 3.0"),
        }),
    }}
    payload = build_segmented_conditions(ledger)
    conditions = {row["id"]: row for row in payload["public_conditions"]}
    assert "S-HIL-OPEN-OVER-3-180" in conditions
    prospective = conditions["S-HIL-OPEN-OVER-3-180"]["prospective"]
    assert prospective["qualified"] == 1
    assert conditions["S-HIL-OPEN-OVER-3-180"]["tier"] == "觀察"
    assert conditions["S-HIL-OPEN-OVER-3-180"]["evidence_status"] == "watch"
    obs = conditions["S-HIL-OPEN-OVER-3-180"]["observations"]
    assert len(obs) == 1
    assert obs[0]["match_id"] == "match"
    assert obs[0]["selected_line"] == 3.0
    assert obs[0]["odds"] == 1.85
