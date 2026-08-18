from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ConditionSimulationUiTests(unittest.TestCase):
    def test_crown_navigation_has_one_simulation_portfolio_and_no_retired_sections(self) -> None:
        index = (ROOT / "crown" / "dashboard" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "crown" / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-view="ledger">獨立驗證倉', index)
        self.assertNotIn('data-view="shadow"', index)
        self.assertNotIn("影子倉", index)
        self.assertNotIn("讓球世界", index)
        self.assertNotIn("renderShadow", app)
        for text in (
            "歷史發現期唯讀封存", "起始 HK$50,000", "每注 HK$250", "每場 HK$500",
            "只在首次持久化原生賽前 T-5 建立注單", "歷史發現／獨立驗證",
            "前瞻盈虧", "前瞻回報率", "賠率分層統計",
            "1.70–1.79", "1.80–1.89", "1.90–1.99", "≥2.00",
            "只計前瞻獨立驗證倉有效注單／賽果", "走水不計入命中率分母",
        ):
            self.assertIn(text, app)
        self.assertIn("conditionBets", app)
        self.assertIn("oddsTierCard", app)

    def test_user_facing_market_labels_are_chinese_and_legacy_labels_are_sanitized(self) -> None:
        for path in (ROOT / "crown" / "dashboard" / "app.js", ROOT / "hkjc-dashboard" / "app.js"):
            source = path.read_text(encoding="utf-8")
            self.assertIn("HDC: '讓球'", source)
            self.assertIn("HIL: '入球大細'", source)
            self.assertIn("CHL: '角球大細'", source)
            self.assertIn("const publicText", source)
            public_block = source[source.index("const publicText"):source.index("const publicText") + 500]
            self.assertIn("方向變化", public_block)
            self.assertNotIn("HDC|HIL|CHL", public_block)
        self.assertIn("投注", (ROOT / "crown" / "notify.py").read_text(encoding="utf-8"))
        self.assertIn("投注", (ROOT / "system" / "notify.py").read_text(encoding="utf-8"))

    def test_legacy_creation_paths_are_inactive_and_reset_is_manual(self) -> None:
        engine = (ROOT / "crown" / "engine.py").read_text(encoding="utf-8")
        retired = (ROOT / "crown" / "handicap_world.py").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "reset-crown-condition-simulation.yml").read_text(encoding="utf-8")
        self.assertNotIn("_apply_confidence_only_pick", engine)
        self.assertNotIn("shadow_pick", engine)
        self.assertIn("def record_new_t5", retired)
        self.assertIn("return []", retired)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("RESET_CROWN_CONDITION_SIMULATION_50000", workflow)
        self.assertIn("aggregate-only output", workflow.lower())
        self.assertNotIn("on:\n  push:", workflow)


if __name__ == "__main__":
    unittest.main()
