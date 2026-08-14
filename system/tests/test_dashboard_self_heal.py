from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DashboardSelfHealTests(unittest.TestCase):
    def test_timer_runs_every_thirty_minutes(self):
        timer = (
            ROOT
            / "deploy"
            / "systemd"
            / "footbreak-dashboard-self-heal.timer"
        ).read_text(encoding="utf-8")

        self.assertIn("OnUnitActiveSec=30min", timer)
        self.assertIn("Persistent=true", timer)

    def test_repair_is_local_and_bounded(self):
        script = (ROOT / "deploy" / "dashboard-self-heal.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("flock -n", script)
        self.assertIn("api_is_healthy 8766", script)
        self.assertIn("api_is_healthy 8765", script)
        self.assertIn('nginx_status 8081', script)
        self.assertIn('nginx_status 8082', script)
        self.assertIn("footbreak-dashboard-api.service", script)
        self.assertIn("crown-dashboard-api.service", script)
        self.assertIn("systemctl reload nginx || systemctl restart nginx", script)
        self.assertNotIn("Telegram", script)
        self.assertNotIn("external-tool", script)

    def test_deployment_enables_and_health_checks_timer(self):
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        health = (ROOT / "deploy" / "health-check.sh").read_text(encoding="utf-8")

        self.assertIn("footbreak-dashboard-self-heal.timer", update)
        self.assertIn("footbreak-dashboard-self-heal.timer", health)


if __name__ == "__main__":
    unittest.main()
