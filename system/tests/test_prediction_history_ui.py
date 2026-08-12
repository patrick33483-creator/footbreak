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

    def test_both_dashboards_render_three_stage_consensus_without_mobile_overflow(self) -> None:
        for dashboard in ("hkjc-dashboard", "crown/dashboard"):
            root = ROOT / dashboard
            app = (root / "app.js").read_text(encoding="utf-8")
            css = (root / "styles.css").read_text(encoding="utf-8")

            self.assertIn("function historyConsensusCards(stats)", app)
            self.assertIn("stats.three_stage_consensus", app)
            self.assertIn("三階段一致命中率", app)
            self.assertIn("每場只計一次，以 T-5 盤口結算", app)
            self.assertIn("少於 30 個已決定樣本只作觀察", app)
            self.assertIn(".consensus-grid", css)
            self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", css)
            self.assertRegex(
                css,
                r"@media \(max-width: 620px\) \{\s*"
                r"\.consensus-grid \{ grid-template-columns: 1fr; \}",
            )


if __name__ == "__main__":
    unittest.main()
