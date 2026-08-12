"""Static and behavioural regression checks for the 資料健康 dashboard view.

The panel is read-only: it renders /data-health.json and must never imply that
anything was changed, retrained, or auto-applied.  It must also stay usable at
375px without introducing any new horizontal page overflow.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = ("hkjc-dashboard", "crown/dashboard")
FORBIDDEN_APPLIED_WORDS = ("已上線", "已自動套用", "自動修復", "已重訓")


class DataHealthDashboardUiTests(unittest.TestCase):
    def _files(self, dashboard: str) -> tuple[str, str, str]:
        root = ROOT / dashboard
        return (
            (root / "index.html").read_text(encoding="utf-8"),
            (root / "app.js").read_text(encoding="utf-8"),
            (root / "styles.css").read_text(encoding="utf-8"),
        )

    def test_both_dashboards_expose_a_data_health_view(self) -> None:
        for dashboard in DASHBOARDS:
            index, app, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn('data-view="health"', index)
                self.assertIn("資料健康", index)
                self.assertIn('id="viewHealth"', index)
                self.assertIn("$('#viewHealth').hidden = VIEW !== 'health';", app)
                self.assertIn("function renderHealth()", app)
                self.assertIn("if (HEALTH.state === 'idle') void loadHealth({});", app)

    def test_each_dashboard_reads_its_own_artifact(self) -> None:
        _, footbreak, _ = self._files("hkjc-dashboard")
        _, crown, _ = self._files("crown/dashboard")
        self.assertIn("const HEALTH_SYSTEM = 'footbreak';", footbreak)
        self.assertIn("const HEALTH_SYSTEM = 'crown';", crown)
        for app in (footbreak, crown):
            self.assertIn("const HEALTH_FILE = 'data-health.json';", app)
            # The artifact of the other system must be rejected, not rendered.
            self.assertIn("if (payload.system !== HEALTH_SYSTEM)", app)

    def test_fetch_is_cache_busted_no_store_and_bound_to_refresh(self) -> None:
        for dashboard in DASHBOARDS:
            _, app, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn(
                    "fetch(`${HEALTH_FILE}?v=${Date.now()}`, { cache: 'no-store' })", app
                )
                refresh_hook = (
                    "if (VIEW === 'health' || HEALTH.state !== 'idle')"
                    " void loadHealth({ quiet: silent });"
                )
                self.assertIn(refresh_hook, app)
                finally_block = app.split("  } finally {", 1)[1][:600]
                self.assertIn(refresh_hook, finally_block)
                self.assertIn('id="healthReload"', app)

    def test_read_only_and_no_auto_apply_are_stated(self) -> None:
        for dashboard in DASHBOARDS:
            _, app, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn("唔會改動任何預測、賽果、結算、注碼、模擬倉或通知", app)
                self.assertIn("唔會重訓、唔會自動套用", app)
                self.assertIn('自動套用:<b class="bad-txt">否</b>', app)
                self.assertIn("並非因果", app)
                for forbidden in FORBIDDEN_APPLIED_WORDS:
                    self.assertNotIn(forbidden, app)

    def test_unique_fixtures_are_declared_the_primary_sample(self) -> None:
        for dashboard in DASHBOARDS:
            _, app, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn("獨立賽事(主要樣本)", app)
                self.assertIn("階段列(只作參考)", app)
                self.assertIn("市場預測列(只作參考)", app)
                self.assertIn("同一場<b>唔可以當三場</b>", app)
                self.assertIn("預測列(指標單位)", app)
                self.assertIn("獨立賽事(樣本量)", app)

    def test_defensive_states_exist(self) -> None:
        for dashboard in DASHBOARDS:
            _, app, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                for marker in (
                    "state-health-loading",
                    "state-health-missing",
                    "state-health-error",
                    "state-health-unavailable",
                    "state-health-slices-empty",
                    "state-health-no-issues",
                    "state-health-no-rec",
                    "flag-health-stale",
                    "banner-health-insufficient",
                    "flag-health-small-sample",
                ):
                    self.assertIn(marker, app)
                self.assertIn("const HEALTH_STALE_HOURS = 26;", app)
                self.assertIn("function healthValidate(payload)", app)

    def test_all_required_filters_and_applied_summary_are_present(self) -> None:
        for dashboard in DASHBOARDS:
            _, app, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn('data-testid="select-health-${dimension}"', app)
                self.assertIn(
                    "const HEALTH_FILTER_DIMENSIONS = "
                    "['market', 'stage', 'league', 'direction'];",
                    app,
                )
                self.assertIn('data-testid="button-health-sample-sufficient"', app)
                self.assertIn('data-testid="button-health-reset"', app)
                self.assertIn('data-testid="applied-health-filters"', app)
                self.assertIn("生效篩選", app)
                self.assertIn('aria-label="資料健康篩選"', app)

    def test_all_required_sections_are_rendered(self) -> None:
        for dashboard in DASHBOARDS:
            _, app, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn("完整率問題", app)
                self.assertIn("錯誤分層", app)
                self.assertIn("HIL v4 診斷建議", app)
                self.assertIn("card-health-families", app)
                self.assertIn("Wilson 95%", app)
                self.assertIn("const HEALTH_MIN_FIXTURES = 30;", app)

    def test_mobile_layout_stacks_without_horizontal_scrolling(self) -> None:
        for dashboard in DASHBOARDS:
            _, app, css = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                block = css.split("/* ─── 資料健康", 1)[1]
                self.assertIn("@media (max-width: 600px)", block)
                mobile = block.split("@media (max-width: 600px)", 1)[1]
                # Tables collapse into stacked cards instead of scrolling.
                self.assertIn(".health-row, .health-issue { grid-template-columns: 1fr;", mobile)
                self.assertIn(".health-row-head { display: none; }", mobile)
                self.assertIn('content: attr(data-label)', mobile)
                self.assertIn(".health-filter { grid-template-columns: 1fr; }", mobile)
                # Every grid child can shrink below its content width.
                for selector in (".health-card { min-width: 0;", ".health-cell {"):
                    self.assertIn(selector, block)
                self.assertIn("overflow-wrap: anywhere", block)
                # No fixed pixel widths that could force page overflow.
                self.assertNotRegex(block, r"\.health-[a-z-]*\s*\{[^}]*\bwidth:\s*\d{3,}px")
                # Data cells must carry the stacked-card label.
                self.assertIn('data-label="${esc(label)}"', app)

    def test_touch_targets_and_tokens_match_the_existing_style(self) -> None:
        for dashboard in DASHBOARDS:
            _, _, css = self._files(dashboard)
            block = css.split("/* ─── 資料健康", 1)[1]
            with self.subTest(dashboard=dashboard):
                self.assertIn("min-height: 44px", block)
                # Only existing design tokens, never new hard-coded brand colours.
                self.assertFalse(
                    re.search(r"#[0-9a-fA-F]{3,8}\b", block),
                    "data health CSS must use existing custom properties",
                )

    def test_artifact_is_never_cached_by_nginx(self) -> None:
        for name in ("deploy/nginx-footbreak.conf", "deploy/nginx-crown.conf"):
            conf = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(conf=name):
                self.assertIn("location = /data-health.json {", conf)

    def test_static_asset_versions_were_bumped(self) -> None:
        for dashboard in DASHBOARDS:
            index, _, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                versions = re.findall(r'(?:styles\.css|app\.js)\?v=([\w.-]+)', index)
                self.assertEqual(len(versions), 2)
                self.assertEqual(len(set(versions)), 1, versions)
                self.assertIn("data-health", versions[0])

    def test_node_syntax_and_render_smoke(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        for dashboard in DASHBOARDS:
            subprocess.run(
                [node, "--check", str(ROOT / dashboard / "app.js")], check=True
            )
        subprocess.run(
            [node, str(ROOT / "system" / "tests" / "data_health_ui_smoke.mjs")],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()


class DataHealthMetricUnitUiTests(unittest.TestCase):
    """The UI must never let a graded-row metric read as one row per fixture."""

    def _app(self, dashboard: str) -> str:
        return (ROOT / dashboard / "app.js").read_text(encoding="utf-8")

    def test_both_dashboards_default_to_the_primary_diagnostic_unit(self) -> None:
        for dashboard in DASHBOARDS:
            app = self._app(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn("sample: 'all', unit: 'primary'", app)
                self.assertIn("function healthSliceSource(payload)", app)
                self.assertIn("payload.primary_diagnostic", app)

    def test_metric_unit_is_stated_on_every_slice_view(self) -> None:
        for dashboard in DASHBOARDS:
            app = self._app(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn("note-health-metric-unit", app)
                self.assertIn("指標單位:<b>已結算預測列</b>", app)
                self.assertIn("<b>唔係每場一行</b>", app)
                self.assertIn("獨立賽事只係樣本量基礎", app)
                self.assertIn("data-metric-unit=", app)
                self.assertIn("data-correlated-stage-rows=", app)

    def test_unit_switch_exists_and_is_reflected_in_applied_filters(self) -> None:
        for dashboard in DASHBOARDS:
            app = self._app(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn("button-health-unit-primary", app)
                self.assertIn("button-health-unit-all-stages", app)
                self.assertIn("applied-health-unit", app)
                self.assertIn("data-health-unit", app)
                # Reset must restore the safer primary unit, not all stages.
                self.assertIn("sample: 'all', unit: 'primary',", app)

    def test_all_stage_view_is_labelled_correlated_and_reference_only(self) -> None:
        for dashboard in DASHBOARDS:
            app = self._app(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn("全部階段列(相關,只作參考)", app)
                self.assertIn("唔可以當獨立樣本", app)
                self.assertIn("flag-health-correlated", app)

    def test_recommendation_note_states_the_evidence_unit(self) -> None:
        for dashboard in DASHBOARDS:
            app = self._app(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn("note-health-rec-unit", app)
                self.assertIn("證據一律取自「每場每市場最新階段」主要診斷", app)
                self.assertIn("絕對唔會當作獨立證據", app)

    def test_metric_unit_styles_exist_in_both_dashboards(self) -> None:
        for dashboard in DASHBOARDS:
            css = (ROOT / dashboard / "styles.css").read_text(encoding="utf-8")
            with self.subTest(dashboard=dashboard):
                self.assertIn(".health-unit-note", css)
                self.assertIn(".health-applied-unit", css)
                # No fixed widths that could reintroduce mobile overflow.
                block = css[css.index(".health-unit-note"):css.index(".health-unit-note") + 400]
                self.assertIn("min-width: 0", block)
                self.assertIn("overflow-wrap: anywhere", block)
