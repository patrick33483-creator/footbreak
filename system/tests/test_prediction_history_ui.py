"""Static regression checks for the Footbreak prediction-history layout."""
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PredictionHistoryUiTests(unittest.TestCase):
    def test_crown_stage_filters_use_persisted_completion_not_countdown_window(self) -> None:
        app = (ROOT / "crown" / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn("['首預', 'T-30', 'T-5'].includes(STAGE)", app)
        self.assertIn("(x) => x.stage === STAGE", app)
        self.assertNotIn(
            "else if (STAGE === '首預') { if (!(m.stages || []).some((x) => x.stage === '首預')) return false; }",
            app,
        )

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

    def test_both_dashboards_show_only_predictions_with_saved_odds(self) -> None:
        for dashboard in ("hkjc-dashboard", "crown/dashboard"):
            root = ROOT / dashboard
            app = (root / "app.js").read_text(encoding="utf-8")
            css = (root / "styles.css").read_text(encoding="utf-8")

            self.assertIn("function historyOdds(p)", app)
            self.assertIn("賠率 ${odds.toFixed(2)}", app)
            self.assertIn("Number.isFinite(odds) && odds > 1", app)
            self.assertNotIn("賠率缺失 · ${esc(reason)}", app)
            self.assertIn("history-market-meta", app)
            self.assertIn("${historyOdds(p)}", app)
            self.assertIn("overflow-wrap: anywhere", css)
            self.assertIn("flex-wrap: wrap", css)
            self.assertIn("@media (max-width: 620px)", css)
            self.assertIn("grid-template-columns: minmax(0, 1fr);", css)
            self.assertIn("function currentOddsCard(m)", app)
            self.assertIn("目前已選賠率", app)
            self.assertIn("HDC: '讓球'", app)
            self.assertIn("HIL: '入球大細'", app)
            self.assertIn("CHL: '角球大細'", app)
            self.assertIn("角球另拆大／細", app)
            self.assertIn("direction('角球大'", app)
            self.assertIn("direction('角球細'", app)
            self.assertIn(".stage-market-directions", css)
            self.assertIn(".stage-market-direction", css)
            self.assertIn("${esc(MKT[row.code] || row.code || '—')}", app)
            self.assertIn("資料來源：", app)
            self.assertIn("記錄時間：", app)
            self.assertIn("更新來源：", app)
            self.assertIn("current_selected_odds_journal", app)
            self.assertIn("observed_board_at", app)
            self.assertIn(".current-odds-list", css)

    def test_crown_current_predictions_use_chinese_market_wording(self) -> None:
        app = (ROOT / "crown" / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function chinesePredictionLabel(prediction)", app)
        self.assertIn("function selectedMarketLine(prediction)", app)
        self.assertIn("code === 'HDC' && side === 'A' ? -line : line", app)
        self.assertIn("讓球 ${side === 'H' ? '主隊'", app)
        self.assertIn("`讓球 ${side === 'H' ? '主隊'", app)
        self.assertIn("`入球大細 ${side === 'H' ? '大'", app)
        self.assertIn("`角球大細 ${side === 'H' ? '大'", app)
        self.assertIn("未有平博同方向盤口，未計預期價值", app)
        self.assertNotIn("未有 Pinnacle 同路盤，未計 EV", app)

    def test_crown_missing_current_quote_is_not_rendered_as_a_wilson_rejection(self) -> None:
        app = (ROOT / "crown" / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function currentExactQuoteUnavailable(m)", app)
        self.assertIn("賽前賠率資料不可用", app)
        self.assertIn("此為資料不可用，不是 Wilson 條件失敗、低賠率或可執行比較", app)
        self.assertIn("已開賽，現時賽前同盤賠率不可用", app)
        self.assertIn("不使用現時或賽中板重建比較", app)

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
            self.assertIn("只計有有效賠率紀錄；主統計為選邊賠率 ≥1.70", app)

    @unittest.skip("Replaced by granular condition ranking.")
    def test_both_dashboards_render_three_stage_consensus_without_mobile_overflow(self) -> None:
        for dashboard in ("hkjc-dashboard", "crown/dashboard"):
            root = ROOT / dashboard
            app = (root / "app.js").read_text(encoding="utf-8")
            css = (root / "styles.css").read_text(encoding="utf-8")

            self.assertIn("function historyConsensusCards(stats)", app)
            self.assertIn("stats.three_stage_consensus", app)
            self.assertIn("stats.three_stage_transitions", app)
            self.assertIn("三階段一致命中率", app)
            self.assertIn("每場只計一次，以 T-5 盤口結算", app)
            self.assertIn("主統計只計 T-5 賠率 ≥1.70", app)
            self.assertIn("exactGroup.breakdown", app)
            self.assertIn("完全一致拆分", app)
            self.assertIn('class="consensus-split-row"', app)
            self.assertIn("最高命中條件自動排名", app)
            self.assertIn("命中率排名唔等於預期價值", app)
            self.assertIn('class="consensus-rank-card"', app)
            self.assertIn("item.odds_bias", app)
            self.assertIn("≥1.70 主統計", app)
            self.assertIn("&lt;1.70 獨立", app)
            self.assertIn("低賠結果獨立列出，不會推高主統計", app)
            self.assertIn('class="consensus-odds-audit"', app)
            self.assertIn("三階段轉向統計", app)
            self.assertIn("同向改盤", app)
            self.assertIn("首預缺向後定", app)
            self.assertIn("T-30 反向後定", app)
            self.assertIn("first_missing_then_stable", app)
            self.assertIn("flip_then_stable", app)
            self.assertIn('class="transition-condition"', app)
            self.assertIn('class="transition-aggregate"', app)
            self.assertIn("待累積", app)
            self.assertIn("走水", app)
            self.assertIn(".consensus-grid", css)
            self.assertIn(".consensus-split-row", css)
            self.assertIn(".consensus-ranking-grid", css)
            self.assertIn(".consensus-odds-audit", css)
            self.assertIn(".transition-block", css)
            self.assertIn(".transition-condition", css)
            self.assertIn(".transition-aggregate", css)
            self.assertIn(".transition-condition { padding: var(--s2); }", css)
            self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", css)
            self.assertRegex(
                css,
                r"@media \(max-width: 620px\) \{\s*"
                r"\.consensus-grid,\s*"
                r"\.consensus-ranking-grid \{ grid-template-columns: 1fr; \}",
            )

    def test_both_dashboards_render_granular_conditions_without_mobile_overflow(self) -> None:
        for dashboard in ("hkjc-dashboard", "crown/dashboard"):
            root = ROOT / dashboard
            app = (root / "app.js").read_text(encoding="utf-8")
            css = (root / "styles.css").read_text(encoding="utf-8")
            self.assertIn("stats.granular_conditions", app)
            self.assertIn("細緻條件排名", app)
            self.assertIn("命中率嚴格高於 60%", app)
            self.assertIn("Wilson 95%", app)
            self.assertIn("Wilson 最低要求賠率", app)
            self.assertIn("歷史賠率層", app)
            self.assertIn("function conditionMatchesCard(m)", app)
            self.assertIn("m.condition_matches", app)
            self.assertIn(".granular-grid", css)
            self.assertIn(".condition-match-card", css)
            self.assertIn("@media (max-width: 620px) { .granular-grid { grid-template-columns: 1fr; }", css)

    def test_discovery_cards_are_visibly_research_only_on_both_dashboards(self) -> None:
        for dashboard in ("hkjc-dashboard", "crown/dashboard"):
            app = (ROOT / dashboard / "app.js").read_text(encoding="utf-8")
            history = app[
                app.index("function historicalConditionMatchText"):
                app.index("function historicalConditionMatchText") + 1800
            ]
            self.assertIn("研究吻合／未納入正式 Wilson", history)
            self.assertIn("不建立投注或 Telegram 通知", history)
            self.assertNotIn("合符條件 #", history)

    def test_wilson_condition_labels_are_scoped_and_ranking_cards_do_not_reuse_numbers(self) -> None:
        expected_scopes = {
            "hkjc-dashboard": "足破 Wilson",
            "crown/dashboard": "皇冠 Wilson",
        }
        for dashboard, scope in expected_scopes.items():
            app = (ROOT / dashboard / "app.js").read_text(encoding="utf-8")
            cards = app[
                app.index("function historyConsensusCards"):
                app.index("function conditionMatchesCard")
            ]
            with self.subTest(dashboard=dashboard):
                self.assertIn(f"const WILSON_CONDITION_SCOPE = '{scope}'", app)
                self.assertIn("function wilsonConditionLabel", app)
                self.assertIn("研究 R#${index + 1}（未凍結；不計前瞻）", cards)
                self.assertIn("研究卡未有凍結 Wilson 身份", cards)
                self.assertNotIn("? Number(item.condition_number) : index + 1", cards)

    def test_both_evidence_batch_details_are_collapsed_and_fail_closed(self) -> None:
        for dashboard in ("hkjc-dashboard", "crown/dashboard"):
            root = ROOT / dashboard
            app = (root / "app.js").read_text(encoding="utf-8")
            css = (root / "styles.css").read_text(encoding="utf-8")
            helper = app[
                app.index("function evidenceBatchDetails"):
                app.index("function historyConsensusCards")
            ]

            with self.subTest(dashboard=dashboard):
                self.assertIn('const detail = item.last_merged_evidence || {}', helper)
                self.assertIn('<details class="evidence-batch"', helper)
                self.assertNotIn("<details open", helper)
                self.assertIn('data-testid="evidence-batch-toggle-', helper)
                self.assertIn('data-testid="evidence-batch-row-', helper)
                self.assertIn("查看 v${esc(String(detail.version))} 的 ${decided || '—'} 場明細", helper)
                self.assertIn("${hits} 命中 · ${misses} 未中", helper)
                self.assertIn("為免撈錯 V2／V3／V4，暫不顯示明細", helper)
                self.assertIn("${evidenceBatchDetails(item)}", app)
                self.assertIn("min-height: 44px", css)
                self.assertIn(".granular-rank-card:has(.evidence-batch[open])", css)
                self.assertIn(".evidence-batch-panel ol", css)
                self.assertIn("@media (max-width: 900px)", css)
                self.assertIn(".evidence-batch-row { grid-template-columns: 22px minmax(0, 1fr) auto; }", css)

    def test_crown_dashboard_renders_stage_completeness_monitor(self) -> None:
        root = ROOT / "crown" / "dashboard"
        app = (root / "app.js").read_text(encoding="utf-8")
        css = (root / "styles.css").read_text(encoding="utf-8")

        self.assertIn("function historyStageCompletenessCard(raw)", app)
        self.assertIn("DATA.stage_completeness", app)
        self.assertIn("階段完整率監察", app)
        self.assertIn("未到期唔扣完整率", app)
        self.assertIn("DATA_MISSING 會當未完成", app)
        self.assertIn('data-stage-completeness="${stage}"', app)
        self.assertIn(".stage-completeness-grid", css)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", css)
        self.assertIn(".stage-completeness-health.bad", css)
        self.assertIn(".stage-completeness-item.bad .stage-completeness-rate", css)
        self.assertIn(".stage-completeness-grid { grid-template-columns: 1fr; }", css)


if __name__ == "__main__":
    unittest.main()
