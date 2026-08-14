"""Regression guards for the two system-separated shadow condition cards."""
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ShadowConditionDashboardUiTests(unittest.TestCase):
    def files(self, dashboard: str) -> tuple[str, str, str]:
        directory = ROOT / dashboard
        return tuple((directory / name).read_text(encoding="utf-8")
                     for name in ("index.html", "app.js", "styles.css"))

    def test_each_dashboard_has_its_own_report_only_condition(self) -> None:
        expectations = {
            "hkjc-dashboard": ("footbreak", "footbreak_hil_t5_under", "crown_hdc_three_stage_exact"),
            "crown/dashboard": ("crown", "crown_hdc_three_stage_exact", "footbreak_hil_t5_under"),
        }
        for dashboard, (system, own, other) in expectations.items():
            index, app, _ = self.files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn('data-view="condition"', index)
                self.assertIn('id="viewCondition"', index)
                self.assertIn("const CONDITION_FILE = 'shadow-condition-report.json';", app)
                self.assertIn(f"const CONDITION_SYSTEM = '{system}';", app)
                self.assertIn(f"const CONDITION_ID = '{own}';", app)
                self.assertNotIn(other, app)
                self.assertIn("只作報告 / 不自動套用", app)
                self.assertIn("完全隔離", app)
                self.assertIn("唔會改機率、推介、模擬倉、注碼、Telegram 或模型升級", app)

    def test_reports_are_cache_busted_and_refreshed_independently(self) -> None:
        for dashboard in ("hkjc-dashboard", "crown/dashboard"):
            _, app, _ = self.files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn(
                    "fetch(`${CONDITION_FILE}?v=${Date.now()}`, { cache: 'no-store' })",
                    app,
                )
                self.assertIn("if (VIEW === 'condition' || CONDITION.state !== 'idle') void loadCondition({ quiet: silent });", app)
                self.assertIn("if (CONDITION.state === 'idle') void loadCondition({});", app)
                self.assertIn("metrics.roi_reason", app)
                self.assertIn("metrics.clv_reason", app)
                self.assertIn("progress.decided_unique_fixtures", app)
                self.assertIn("進度只計已判定場", app)

    def test_regeneration_is_non_fatal_and_has_its_own_public_routes(self) -> None:
        root = ROOT
        daily = (root / "deploy" / "backtest-run.sh").read_text(encoding="utf-8")
        reconcile = (root / "deploy" / "reconcile-results.sh").read_text(encoding="utf-8")
        for script in (daily, reconcile):
            self.assertIn("analysis.shadow_conditions", script)
            self.assertIn("shadow-condition-report.json", script)
            self.assertIn("條件影子報告生成失敗", script)
        for config in ("nginx-footbreak.conf", "nginx-crown.conf"):
            nginx = (root / "deploy" / config).read_text(encoding="utf-8")
            self.assertIn("location = /shadow-condition-report.json {", nginx)
            self.assertIn("add_header Cache-Control \"no-store, must-revalidate\";", nginx)

    def test_deploy_generates_reports_immediately_without_blocking_rollout(self) -> None:
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        self.assertIn("-m analysis.shadow_conditions", update)
        self.assertIn("--public-footbreak /var/www/footbreak/shadow-condition-report.json", update)
        self.assertIn("--public-crown /var/www/crown/shadow-condition-report.json", update)
        self.assertIn("下個 15 分鐘週期會重試", update)
        self.assertRegex(update, r"if ! PYTHONPATH=.*analysis\.shadow_conditions")

    def test_deploy_has_github_ssh_443_fallback(self) -> None:
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        for content in (update, workflow):
            self.assertIn("Hostname=ssh.github.com", content)
            self.assertIn("HostKeyAlias=github.com", content)
            self.assertIn("-p 443", content)
        self.assertIn("ConnectTimeout=10", update)
        self.assertIn("git reset --hard --quiet origin/main", workflow)

    def test_mobile_guards_do_not_introduce_horizontal_overflow(self) -> None:
        for dashboard in ("hkjc-dashboard", "crown/dashboard"):
            _, _, css = self.files(dashboard)
            with self.subTest(dashboard=dashboard):
                self.assertIn("@media (max-width: 375px)", css)
                self.assertIn(".condition-card { overflow-x: hidden; }", css)
                self.assertIn(".condition-grid { grid-template-columns: 1fr; }", css)


if __name__ == "__main__":
    unittest.main()
