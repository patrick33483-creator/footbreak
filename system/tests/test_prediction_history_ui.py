"""Static regression checks for the Footbreak prediction-history layout."""
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PredictionHistoryUiTests(unittest.TestCase):
    def test_each_market_result_is_rendered_beside_its_prediction(self) -> None:
        app = (ROOT / "hkjc-dashboard" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "hkjc-dashboard" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('class="history-market-row"', app)
        self.assertIn('class="history-market-pick"', app)
        self.assertIn("各市場預測／結果", app)
        self.assertIn("function historyCornerResult(r, p)", app)
        self.assertIn('class="market-actual">賽果 <b>', app)
        self.assertIn("grid-template-columns: minmax(0, 1fr) max-content", css)
        self.assertIn(".history-market-outcome", css)
        self.assertIn("white-space: nowrap", css)
        self.assertIn("const historyTime = (row)", app)
        self.assertIn("b[0] - a[0] || b[1] - a[1]", app)

    def test_both_dashboards_include_top_and_bottom_shortcuts(self) -> None:
        for dashboard in ("hkjc-dashboard", "crown/dashboard"):
            root = ROOT / dashboard
            index = (root / "index.html").read_text(encoding="utf-8")
            app = (root / "app.js").read_text(encoding="utf-8")
            css = (root / "styles.css").read_text(encoding="utf-8")

            self.assertIn('id="scrollTop"', index)
            self.assertIn('id="scrollBottom"', index)
            self.assertIn("回到最頂", index)
            self.assertIn("去到最底", index)
            self.assertIn("function updateScrollDock()", app)
            self.assertIn("function scrollToPageBottom()", app)
            self.assertIn("window.scrollTo({ top: 0", app)
            self.assertIn("document.documentElement.scrollHeight", app)
            self.assertIn("window.addEventListener('scrollend', advance", app)
            self.assertIn("min-width: 44px", css)
            self.assertIn("min-height: 44px", css)

    def test_both_dashboards_name_the_wdl_topline_and_selected_odds_scope(self) -> None:
        for dashboard in ("hkjc-dashboard", "crown/dashboard"):
            app = (ROOT / dashboard / "app.js").read_text(encoding="utf-8")
            self.assertIn("1X2 已評分", app)
            self.assertIn("1X2 命中", app)
            self.assertIn("1X2 命中率", app)
            self.assertIn("wdl_graded", app)
            self.assertIn("wdl_hits", app)
            self.assertIn("wdl_accuracy", app)
            self.assertIn("主統計：選邊賠率 ≥1.70", app)

    def test_both_dashboards_render_three_stage_consensus_without_mobile_overflow(self) -> None:
        for dashboard in ("hkjc-dashboard", "crown/dashboard"):
            root = ROOT / dashboard
            app = (root / "app.js").read_text(encoding="utf-8")
            css = (root / "styles.css").read_text(encoding="utf-8")

            self.assertIn("function historyConsensusCards(stats)", app)
            self.assertIn("stats.three_stage_consensus", app)
            self.assertIn("三階段一致命中率", app)
            self.assertIn("每場只計一次，以 T-5 盤口結算", app)
            self.assertIn("主統計只計 T-5 賠率 ≥1.70", app)
            self.assertIn("exactGroup.breakdown", app)
            self.assertIn("完全一致拆分", app)
            self.assertIn('class="consensus-split-row"', app)
            self.assertIn("最高命中條件自動排名", app)
            self.assertIn("命中率排名唔等於 +EV", app)
            self.assertIn('class="consensus-rank-card"', app)
            self.assertIn("item.odds_bias", app)
            self.assertIn("≥1.70 主統計", app)
            self.assertIn("&lt;1.70 獨立", app)
            self.assertIn("低賠結果獨立列出，不會推高主統計", app)
            self.assertIn('class="consensus-odds-audit"', app)
            self.assertIn(".consensus-grid", css)
            self.assertIn(".consensus-split-row", css)
            self.assertIn(".consensus-ranking-grid", css)
            self.assertIn(".consensus-odds-audit", css)
            self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", css)
            self.assertRegex(
                css,
                r"@media \(max-width: 620px\) \{\s*"
                r"\.consensus-grid,\s*"
                r"\.consensus-ranking-grid \{ grid-template-columns: 1fr; \}",
            )


if __name__ == "__main__":
    unittest.main()
