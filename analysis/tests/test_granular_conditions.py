from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from analysis.granular_conditions import (
    _badge,
    _descriptor,
    _role,
    canonical_panels,
    match_upcoming,
    mine,
)


HKT = timezone(timedelta(hours=8))


def row(fixture: str, stage: str, *, code="HDC", side="H", line=-.25,
        odds=1.8, hit=True, kickoff=None, predicted=None):
    kickoff = kickoff or datetime(2026, 8, 1, 20, tzinfo=HKT)
    predicted = predicted or kickoff - timedelta(minutes=35)
    return {
        "match_id": fixture, "stage": stage, "kickoff": kickoff.isoformat(),
        "predicted_at": predicted.isoformat(),
        "market_grades": [{
            "code": code, "side": side, "line": line, "odds": odds,
            "grade_status": "GRADED", "hit": hit,
        }],
    }


class GranularConditionsTests(unittest.TestCase):
    def test_thresholds_and_badges(self):
        empty = {"decided": 0}
        self.assertEqual(_badge({"decided": 9}, empty, empty, None, None), "隱藏")
        self.assertEqual(_badge({"decided": 10}, empty, empty, None, None), "樣本不足")
        self.assertEqual(_badge({"decided": 29}, empty, empty, None, None), "樣本不足")
        self.assertEqual(_badge({"decided": 30}, empty, empty, None, None), "觀察")
        self.assertEqual(_badge({"decided": 60}, {"decided": 40}, {"decided": 20}, 0, .04), "穩健觀察 · q≤0.05")

        rows = [row(f"sixty-{i}", "T-5", hit=i < 12,
                    kickoff=datetime(2026, 8, 1, 20, tzinfo=HKT) + timedelta(days=i))
                for i in range(20)]
        self.assertEqual(mine(rows, system="footbreak")["ranking"], [])
        rows[-1]["market_grades"][0]["hit"] = True
        self.assertTrue(mine(rows, system="footbreak")["ranking"])

    def test_tier_role_path_push_and_lookahead_rules(self):
        self.assertEqual(_role("HDC", "H", -.25), "主讓")
        self.assertEqual(_role("HDC", "H", .25), "主受讓")
        self.assertEqual(_role("HDC", "H", 0), "平手盤主")
        self.assertEqual(_role("HDC", "A", 0), "平手盤客")
        self.assertEqual(_role("HDC", "A", .25), "客讓")
        self.assertEqual(_role("HDC", "A", -.25), "客受讓")
        self.assertEqual(_role("HIL", "H", 2.5), "大")
        self.assertEqual(_role("HIL", "L", 2.5), "細")
        self.assertEqual(_role("CHL", "H", 9.5), "角球大")
        self.assertEqual(_role("CHL", "L", 9.5), "角球細")

        rows = []
        for i in range(20):
            kickoff = datetime(2026, 8, 1, 20, tzinfo=HKT) + timedelta(days=i)
            rows.extend([
                row(f"p{i}", "首預", side="H", hit=True, kickoff=kickoff),
                row(f"p{i}", "T-30", side="A", hit=True, kickoff=kickoff),
                row(f"p{i}", "T-5", side="H", hit=True, kickoff=kickoff),
            ])
        rows.append(row("push", "T-5", hit=None))
        # A post-kickoff row and an invalid price cannot enter the canonical cohort.
        rows.append(row("late", "T-5", predicted=datetime(2026, 8, 1, 21, tzinfo=HKT)))
        rows.append(row("bad-price", "T-5", odds=1))
        panels = canonical_panels(rows)
        self.assertNotIn("late", {panel["fixture"] for panel in panels})
        self.assertNotIn("bad-price", {panel["fixture"] for panel in panels})
        report = mine(rows, system="footbreak")
        labels = [item["label"] for item in report["ranking"]]
        self.assertTrue(any("方向 主讓→客受讓→主讓" in label for label in labels))
        self.assertFalse(any("HDC" in label or "A→B→A" in label for label in labels))
        self.assertTrue(any(item["total"]["pushes"] == 1 for item in report["ranking"]))

    def test_public_descriptor_uses_observed_chinese_roles_for_total_and_corner_paths(self):
        def item(market, stage, side, line):
            return {
                "market": market, "stage": stage, "side": side,
                "selected_line": line, "role": _role(market, side, line),
                "line_bucket": "測試", "odds_tier": "≥1.70",
            }

        cases = [
            ("HIL", (("H", 2.5), ("L", 2.5), ("H", 2.5)), "入球大細", "方向 大→細→大"),
            ("CHL", (("H", 9.5), ("L", 9.5), ("H", 9.5)), "角球大細", "方向 角球大→角球細→角球大"),
        ]
        for market, selections, market_label, expected_path in cases:
            with self.subTest(market=market):
                path = tuple(
                    item(market, stage, side, line)
                    for stage, (side, line) in zip(("首預", "T-30", "T-5"), selections)
                )
                _key, label, _specificity = _descriptor("crown", path, 1)
                self.assertIn(market_label, label)
                self.assertIn(expected_path, label)
                self.assertNotRegex(label, r"\b[ABC](?:→[ABC])+\b")
                self.assertNotIn(market, label)

    def test_odds_tiers_are_distinct_and_t30_never_uses_t5(self):
        settled = []
        for i in range(20):
            kickoff = datetime(2026, 8, 1, 20, tzinfo=HKT) + timedelta(days=i)
            settled.append(row(f"high-{i}", "T-30", odds=1.7, kickoff=kickoff, hit=True))
            settled.append(row(f"low-{i}", "T-30", odds=1.69, kickoff=kickoff, hit=True))
        ranking = mine(settled, system="crown")["ranking"]
        self.assertEqual({item["odds_tier"] for item in ranking}, {"≥1.70", "<1.70"})

        upcoming = []
        for stage, side in (("首預", "H"), ("T-30", "A"), ("T-5", "H")):
            kickoff = datetime(2099, 8, 1, 20, tzinfo=HKT)
            upcoming.append({
                **row("future", stage, side=side, kickoff=kickoff,
                       predicted=kickoff - timedelta(minutes=40)),
                "market_predictions": [{
                    "code": "HDC", "side": side, "line": -.25, "odds": 1.8,
                }],
            })
            upcoming[-1]["market_grades"] = []
        t30 = match_upcoming(upcoming, ranking, system="crown", decision_stage="T-30")
        self.assertTrue(t30["future"])
        self.assertFalse(any("T-5" in match["observed_path"] for match in t30["future"]))
        t5 = match_upcoming(upcoming, ranking, system="crown", decision_stage="T-5")
        self.assertNotIn("future", t5)

    def test_identical_fixture_cohorts_do_not_collapse_different_markets(self):
        settled = []
        for i in range(20):
            kickoff = datetime(2026, 8, 1, 20, tzinfo=HKT) + timedelta(days=i)
            settled.extend([
                row(f"shared-{i}", "T-5", code="HDC", side="H", line=-.25,
                    kickoff=kickoff, hit=True),
                row(f"shared-{i}", "T-5", code="HIL", side="L", line=2.5,
                    kickoff=kickoff, hit=True),
            ])
        ranking = mine(settled, system="crown")["ranking"]
        self.assertEqual({item["market"] for item in ranking}, {"HDC", "HIL"})


if __name__ == "__main__":
    unittest.main()
