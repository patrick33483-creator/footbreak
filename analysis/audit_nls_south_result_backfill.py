#!/usr/bin/env python3
"""Build an identity-locked National League South result backfill proposal.

This tool never mutates the input history.  It verifies the reviewed fixture
identity, grades every pending stage for completed fixtures, and optionally
writes the proposed history to a separate path for operator review.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crown.prediction_history import _grade_market
from crown.state import write_json_atomic


BATCH_ID = "nls-south-2026-08-reviewed-results-v1"
VERIFIED_AT = "2026-09-01T17:19:00+08:00"
SCORE_SCOPE = "90_minutes_including_stoppage_time_excluding_extra_time"

# Crown simplified-Chinese display name -> reviewed English club identity.
# The three commonly confused names are deliberately separate:
# 富莫咸普顿 = Hampton & Richmond; 希美咸史特城 = Hemel Hempstead Town;
# 麦德黑联 = Maidenhead United.
NLS_TEAM_IDENTITIES = {
    "托顿": "AFC Totton",
    "比利列卡尔镇": "Billericay Town",
    "布伦特里": "Braintree Town",
    "车斯曼": "Chesham United",
    "车姆斯福特": "Chelmsford City",
    "特鲁罗城": "Truro City",
    "多佛": "Dover Athletic",
    "史洛治": "Slough Town",
    "法保罗夫": "Farnborough",
    "希美咸史特城": "Hemel Hempstead Town",
    "霍舍姆": "Horsham",
    "索尔兹伯里FC": "Salisbury",
    "麦德黑联": "Maidenhead United",
    "杜金": "Dorking Wanderers",
    "汤桥天使": "Tonbridge Angels",
    "托奎联": "Torquay United",
    "沃尔顿赫咸": "Walton & Hersham",
    "梅德斯托联": "Maidstone United",
    "维斯顿": "Weston-super-Mare",
    "富莫咸普顿": "Hampton & Richmond",
    "达根汉姆": "Dagenham & Redbridge",
    "法纳姆城": "Farnham Town",
    "福克斯顿": "Folkestone Invicta",
    "艾贝斯费特联": "Ebbsfleet United",
}

SOURCE_BY_DATE = {
    "2026-08-15": (
        "https://www.dazn.com/en-GB/news/football/"
        "national-league-south-fixtures-2026-2027-dazn/"
        "1ef0qp51wi7cu1myzg5kv5ljum"
    ),
    "2026-08-22": "https://www.bbc.co.uk/sport/football/scores-fixtures/2026-08-22",
    "2026-08-29": "https://www.bbc.co.uk/sport/football/scores-fixtures/2026-08-29",
    "2026-08-31": "https://www.bbc.co.uk/sport/football/scores-fixtures/2026-08-31",
}

# match_id, date, Crown home, Crown away, final score.
VERIFIED_RESULTS = (
    ("3028787", "2026-08-15", "托顿", "比利列卡尔镇", "1-0"),
    ("3028788", "2026-08-15", "布伦特里", "车斯曼", "3-2"),
    ("3028789", "2026-08-15", "车姆斯福特", "特鲁罗城", "0-0"),
    ("3028791", "2026-08-15", "多佛", "史洛治", "5-3"),
    ("3028793", "2026-08-15", "法保罗夫", "希美咸史特城", "0-2"),
    ("3028794", "2026-08-15", "霍舍姆", "索尔兹伯里FC", "0-0"),
    ("3028795", "2026-08-15", "麦德黑联", "杜金", "0-0"),
    ("3028796", "2026-08-15", "汤桥天使", "托奎联", "1-2"),
    ("3028797", "2026-08-15", "沃尔顿赫咸", "梅德斯托联", "0-3"),
    ("3028798", "2026-08-15", "维斯顿", "富莫咸普顿", "3-0"),
    ("3028809", "2026-08-22", "布伦特里", "霍舍姆", "1-2"),
    ("3028810", "2026-08-22", "车斯曼", "沃尔顿赫咸", "2-0"),
    ("3028811", "2026-08-22", "达根汉姆", "托奎联", "2-0"),
    ("3028812", "2026-08-22", "杜金", "梅德斯托联", "2-3"),
    ("3028814", "2026-08-22", "法纳姆城", "比利列卡尔镇", "2-4"),
    ("3028815", "2026-08-22", "福克斯顿", "车姆斯福特", "1-1"),
    ("3028818", "2026-08-22", "索尔兹伯里FC", "希美咸史特城", "1-2"),
    ("3028819", "2026-08-22", "汤桥天使", "特鲁罗城", "0-2"),
    ("3028820", "2026-08-22", "维斯顿", "多佛", "2-3"),
    ("3028821", "2026-08-29", "托顿", "车斯曼", "0-4"),
    ("3028822", "2026-08-29", "比利列卡尔镇", "汤桥天使", "2-2"),
    ("3028823", "2026-08-29", "车姆斯福特", "麦德黑联", "1-0"),
    ("3028824", "2026-08-29", "多佛", "布伦特里", "3-3"),
    ("3028826", "2026-08-29", "希美咸史特城", "维斯顿", "1-3"),
    ("3028827", "2026-08-29", "霍舍姆", "法纳姆城", "1-2"),
    ("3028828", "2026-08-29", "梅德斯托联", "索尔兹伯里FC", "2-1"),
    ("3028829", "2026-08-29", "史洛治", "福克斯顿", "0-2"),
    ("3028830", "2026-08-29", "托奎联", "富莫咸普顿", "1-1"),
    ("3028831", "2026-08-29", "特鲁罗城", "达根汉姆", "3-0"),
    ("3028832", "2026-08-29", "沃尔顿赫咸", "杜金", "1-4"),
    ("3028833", "2026-08-31", "布伦特里", "法保罗夫", "0-2"),
    ("3028834", "2026-08-31", "车斯曼", "多佛", "2-2"),
    ("3028835", "2026-08-31", "达根汉姆", "史洛治", "1-0"),
    ("3028836", "2026-08-31", "杜金", "特鲁罗城", "1-0"),
    ("3028838", "2026-08-31", "法纳姆城", "托奎联", "2-0"),
    ("3028839", "2026-08-31", "福克斯顿", "希美咸史特城", "2-0"),
    ("3028840", "2026-08-31", "富莫咸普顿", "梅德斯托联", "1-4"),
    ("3028841", "2026-08-31", "麦德黑联", "托顿", "1-1"),
    ("3028842", "2026-08-31", "索尔兹伯里FC", "比利列卡尔镇", "1-0"),
    ("3028843", "2026-08-31", "汤桥天使", "沃尔顿赫咸", "5-1"),
    ("3028844", "2026-08-31", "维斯顿", "车姆斯福特", "1-1"),
)

EXCLUDED = (
    {
        "match_id": "3028837",
        "reason": "postponed_no_final_score",
        "fixture": "艾贝斯费特联 vs 霍舍姆",
        "source_url": SOURCE_BY_DATE["2026-08-31"],
    },
    {
        "match_id": "3028808",
        "reason": "kickoff_date_mismatch",
        "fixture": "法保罗夫 vs 车斯曼",
        "crown_kickoff": "2026-08-20T02:45:00+08:00",
        "actual_fixture": "2026-08-19 Farnborough 1-0 Chesham United",
        "source_url": (
            "https://www.footlive.com/score/"
            "farnborough-fc-vs-chesham-united-2026-08-19/"
        ),
    },
)

FINAL = {"已核對", "不計"}


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _different_file(source: Path, output: Path) -> None:
    if source.resolve() == output.resolve():
        raise ValueError("output must not overwrite input history")
    if output.exists() and source.samefile(output):
        raise ValueError("output aliases input history")


def build_proposal(history: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    proposed = copy.deepcopy(history)
    rows = proposed.get("rows")
    if not isinstance(rows, list):
        raise ValueError("history rows missing")

    changed_rows = 0
    already_rows = 0
    fixture_row_counts: dict[str, int] = {}
    for match_id, day, home, away, score_text in VERIFIED_RESULTS:
        candidates = [
            row for row in rows
            if isinstance(row, dict) and str(row.get("match_id") or "") == match_id
        ]
        if not candidates:
            raise ValueError(f"history fixture missing: {match_id}")
        home_score, away_score = (int(value) for value in score_text.split("-", 1))
        actual = (
            "主勝" if home_score > away_score
            else ("和局" if home_score == away_score else "客勝")
        )
        score = {
            "home_score": home_score,
            "away_score": away_score,
            "corners_total": None,
        }
        for row in candidates:
            if row.get("league") != "英议南":
                raise ValueError(f"history league mismatch: {match_id}")
            if row.get("home") != home or row.get("away") != away:
                raise ValueError(f"history team identity mismatch: {match_id}")
            if str(row.get("kickoff") or "")[:10] != day:
                raise ValueError(f"history kickoff date mismatch: {match_id}")
            if row.get("result_status") in FINAL:
                if row.get("result_status") == "已核對" and row.get("score") == score_text:
                    already_rows += 1
                    continue
                raise ValueError(f"existing result conflict: {match_id}")
            row.update({
                "actual": actual,
                "score": score_text,
                "correct": row.get("forecast") == actual if row.get("forecast") else None,
                "result_status": "已核對",
                "verified_at": VERIFIED_AT,
                "result_source": "reviewed_nls_multi_source_audit",
                "result_detail": {
                    **score,
                    "audit_batch_id": BATCH_ID,
                    "provider_home": NLS_TEAM_IDENTITIES[home],
                    "provider_away": NLS_TEAM_IDENTITIES[away],
                    "source_url": SOURCE_BY_DATE[day],
                    "score_scope": SCORE_SCOPE,
                },
                "market_grades": [
                    _grade_market(prediction, score)
                    for prediction in (row.get("market_predictions") or [])
                    if isinstance(prediction, dict)
                ],
                "result_missing_reason": None,
            })
            changed_rows += 1
        fixture_row_counts[match_id] = len(candidates)

    proposed.setdefault("reviewed_result_backfills", {})[BATCH_ID] = {
        "verified_at": VERIFIED_AT,
        "fixtures": len(VERIFIED_RESULTS),
        "changed_rows": changed_rows,
        "already_rows": already_rows,
        "excluded": copy.deepcopy(EXCLUDED),
    }
    report = {
        "mode": "proposal_only",
        "batch_id": BATCH_ID,
        "verified_fixtures": len(VERIFIED_RESULTS),
        "excluded_fixtures": len(EXCLUDED),
        "changed_rows": changed_rows,
        "already_rows": already_rows,
        "fixture_row_counts": fixture_row_counts,
        "before_hash": _hash(history),
        "after_hash": _hash(proposed),
        "input_unchanged": True,
        "aliases": {
            name: NLS_TEAM_IDENTITIES[name]
            for name in ("富莫咸普顿", "希美咸史特城", "麦德黑联")
        },
        "excluded": copy.deepcopy(EXCLUDED),
    }
    return proposed, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--proposal-out", type=Path)
    parser.add_argument("--report-out", type=Path)
    args = parser.parse_args()

    history = json.loads(args.history.read_text(encoding="utf-8"))
    input_hash = hashlib.sha256(args.history.read_bytes()).hexdigest()
    proposed, report = build_proposal(history)

    after_input_hash = hashlib.sha256(args.history.read_bytes()).hexdigest()
    report["input_sha256"] = input_hash
    report["input_sha256_unchanged"] = after_input_hash == input_hash

    if args.proposal_out:
        _different_file(args.history, args.proposal_out)
        write_json_atomic(args.proposal_out, proposed)
        report["proposal_out"] = str(args.proposal_out)
    if args.report_out:
        _different_file(args.history, args.report_out)
        write_json_atomic(args.report_out, report)

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["input_sha256_unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
