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
        self.assertIn("grid-template-columns: minmax(0, 1fr) max-content", css)
        self.assertIn("white-space: nowrap", css)


if __name__ == "__main__":
    unittest.main()
