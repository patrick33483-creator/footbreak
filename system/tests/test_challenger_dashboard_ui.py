"""Static and behavioural regression checks for the challenger (挑戰模型) panel.

The panel is read-only: it renders /challenger-status.json and must never imply
that a candidate model was applied.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = ("hkjc-dashboard", "crown/dashboard")
FORBIDDEN_APPLIED_WORDS = ("已套用", "已上線", "自動套用:<b class=\"good-txt\">是")


class ChallengerDashboardUiTests(unittest.TestCase):
    def _files(self, dashboard: str) -> tuple[str, str, str]:
        root = ROOT / dashboard
        return (
            (root / "index.html").read_text(encoding="utf-8"),
            (root / "app.js").read_text(encoding="utf-8"),
            (root / "styles.css").read_text(encoding="utf-8"),
        )

    def test_both_dashboards_expose_a_challenger_view(self) -> None:
        for dashboard in DASHBOARDS:
            index, app, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn('data-view="chal"', index)
                self.assertIn("挑戰模型", index)
                self.assertIn('id="viewChal"', index)
                self.assertIn("$('#viewChal').hidden = VIEW !== 'chal';", app)
                self.assertIn("function renderChallenger()", app)
                self.assertIn("if (CHAL.state === 'idle') void loadChallenger({});", app)

    def test_each_dashboard_reads_its_own_system(self) -> None:
        _, footbreak, _ = self._files("hkjc-dashboard")
        _, crown, _ = self._files("crown/dashboard")
        self.assertIn("const CHALLENGER_SYSTEM = 'footbreak';", footbreak)
        self.assertIn("const CHALLENGER_SYSTEM = 'crown';", crown)

    def test_fetch_is_cache_busted_and_bound_to_refresh(self) -> None:
        for dashboard in DASHBOARDS:
            _, app, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn(
                    "fetch(`${CHALLENGER_FILE}?v=${Date.now()}`, { cache: 'no-store' })", app
                )
                # 放喺 refresh() 嘅 finally,主資料或結算失敗都會重新讀取報告。
                refresh_hook = (
                    "if (VIEW === 'chal' || CHAL.state !== 'idle')"
                    " void loadChallenger({ quiet: silent });"
                )
                self.assertIn(refresh_hook, app)
                refresh_block = app.split(
                    "async function refresh(silent)", 1
                )[1].split("function bindUI()", 1)[0]
                finally_block = refresh_block.split("  } finally {", 1)[1][:400]
                self.assertIn(refresh_hook, finally_block)
                self.assertIn("id=\"challengerReload\"", app)

    def test_isolation_and_no_auto_apply_are_stated(self) -> None:
        for dashboard in DASHBOARDS:
            _, app, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn("永不自動套用", app)
                self.assertIn("只係等人手覆核", app)
                self.assertIn('自動套用:<b class="bad-txt">否</b>', app)
                for forbidden in FORBIDDEN_APPLIED_WORDS:
                    self.assertNotIn(forbidden, app)

    def test_states_and_translations_exist(self) -> None:
        for dashboard in DASHBOARDS:
            _, app, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                for marker in (
                    "state-challenger-loading",
                    "state-challenger-missing",
                    "state-challenger-error",
                    "flag-challenger-stale",
                    "banner-challenger-review",
                ):
                    self.assertIn(marker, app)
                for label in (
                    "樣本未夠",
                    "已測試 · 未達升級門檻",
                    "候選通過 · 等人手覆核",
                    "合資格賽事未夠 100 場",
                    "Brier 改善未夠 0.01",
                    "對數損失冇改善",
                    "準確率跌幅超過 2%",
                ):
                    self.assertIn(label, app)

    def test_crown_hil_card_renders_frozen_prospective_v3_progress(self) -> None:
        _, app, css = self._files("crown/dashboard")
        for marker in (
            "prospective_shadow_collecting",
            "prospective_tested_no_safe_upgrade",
            "function challengerProspectiveV3(test)",
            'data-testid="section-challenger-hil-v3"',
            'data-testid="status-challenger-hil-v3"',
            "嚴格凍結後才計入",
            "不會重訓或改變",
        ):
            self.assertIn(marker, app)
        self.assertIn(".chal-v3", css)

    def test_crown_chl_card_renders_frozen_prospective_section(self) -> None:
        _, app, css = self._files("crown/dashboard")
        for marker in (
            "function challengerProspectiveCHL(test)",
            'data-testid="section-challenger-chl-prospective"',
            'data-testid="status-challenger-chl"',
            'data-testid="table-challenger-chl-stages"',
            'data-testid="flag-challenger-chl-feature-coverage"',
            'data-testid="flag-challenger-chl-weak-sample"',
            "insufficient_feature_coverage",
            "主樣本階段規則(已凍結)",
            "每場一行,唔係每階段一行",
            "唔可以相加當獨立樣本",
            "證明唔到正期望值",
            "if (market === 'CHL') body += challengerProspectiveCHL(test.prospective_chl);",
        ):
            self.assertIn(marker, app)
        self.assertIn(".chal-chl", css)
        # The two frozen prospective panels stay separate.
        self.assertIn("if (market === 'HIL') body += challengerProspectiveV3", app)

    def test_footbreak_dashboard_has_no_crown_only_chl_model(self) -> None:
        _, footbreak, _ = self._files("hkjc-dashboard")
        for marker in (
            "challengerProspectiveCHL",
            "prospective_chl",
            "section-challenger-chl-prospective",
            "CHL 前瞻凍結影子驗證",
        ):
            self.assertNotIn(marker, footbreak)

    def test_crown_chl_mobile_layout_stacks(self) -> None:
        _, _, css = self._files("crown/dashboard")
        block = css.split(".chal-chl {", 1)[1]
        self.assertIn("@media (max-width: 600px)", block)
        mobile = block.split("@media (max-width: 600px)", 1)[1]
        self.assertIn(".chal-chl-row { grid-template-columns: 1fr;", mobile)
        self.assertIn(".chal-chl-head { display: none; }", mobile)
        self.assertIn("content: attr(data-label)", mobile)
        self.assertIn("overflow-wrap: anywhere", block)

    def test_unique_fixture_progress_and_metrics_are_rendered(self) -> None:
        for dashboard in DASHBOARDS:
            _, app, _ = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn("合資格獨立賽事(按場計,唔係按紀錄行數)", app)
                self.assertIn("const CHALLENGER_REQUIRED_FIXTURES = 100;", app)
                self.assertIn("驗證場次(holdout)", app)
                self.assertIn("challengerMetricRow('準確率'", app)
                self.assertIn("challengerMetricRow('Brier'", app)
                self.assertIn("challengerMetricRow('對數損失'", app)

    def test_passed_review_filter_is_available_and_accessible(self) -> None:
        for dashboard in DASHBOARDS:
            _, app, css = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn('data-testid="button-challenger-filter-all"', app)
                self.assertIn('data-testid="button-challenger-filter-review"', app)
                self.assertIn('aria-pressed="${CHAL_FILTER === \'review\'}"', app)
                self.assertIn("candidate_passed_human_review_required", app)
                self.assertIn("state-challenger-filter-empty", app)
                self.assertIn("min-height: 44px", css)

    def test_mobile_layout_stacks_without_horizontal_scrolling(self) -> None:
        for dashboard in DASHBOARDS:
            _, _, css = self._files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn("@media (max-width: 600px)", css)
                self.assertIn(".chal-grid { grid-template-columns: 1fr; }", css)
                self.assertIn(".chal-metric { grid-template-columns: 1fr;", css)
                self.assertIn(".chal-card { min-width: 0; }", css)

    def test_challenger_artifact_is_never_cached_by_nginx(self) -> None:
        for name in ("deploy/nginx-footbreak.conf", "deploy/nginx-crown.conf"):
            conf = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(conf=name):
                self.assertIn("location = /challenger-status.json {", conf)

    def test_node_syntax_and_render_smoke(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        for dashboard in DASHBOARDS:
            subprocess.run(
                [node, "--check", str(ROOT / dashboard / "app.js")], check=True
            )
        subprocess.run(
            [node, str(ROOT / "system" / "tests" / "challenger_ui_smoke.mjs")],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
