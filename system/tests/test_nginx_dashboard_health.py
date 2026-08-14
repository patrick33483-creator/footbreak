from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class NginxDashboardHealthTests(unittest.TestCase):
    def test_deploy_repairs_auth_and_static_permissions(self):
        script = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

        self.assertIn(
            "for auth_file in /etc/nginx/.htpasswd-footbreak "
            "/etc/nginx/.htpasswd-crown",
            script,
        )
        self.assertIn('chown root:www-data "$auth_file"', script)
        self.assertIn('chmod 0640 "$auth_file"', script)
        self.assertIn('sudo -u www-data test -r "$auth_file"', script)
        self.assertIn('chown -R root:www-data "$WEB_ROOT"', script)
        self.assertIn('find "$WEB_ROOT" -type d -exec chmod 0755 {} +', script)
        self.assertIn('find "$WEB_ROOT" -type f -exec chmod 0644 {} +', script)

    def test_deploy_and_health_check_probe_nginx_entrypoints(self):
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        health = (ROOT / "deploy" / "health-check.sh").read_text(encoding="utf-8")

        for script in (update, health):
            self.assertIn("for dashboard in footbreak:8081 crown:8082", script)
            self.assertIn("--write-out '%{http_code}'", script)
            self.assertIn('[ "$status" != 401 ]', script)

    def test_dashboard_data_health_check_retries_transient_timeouts(self):
        health = (ROOT / "deploy" / "health-check.sh").read_text(encoding="utf-8")

        self.assertIn("dashboard_data_ready=0", health)
        self.assertIn("for _ in $(seq 1 10)", health)
        self.assertIn("dashboard API /api/data did not become ready after retries", health)


if __name__ == "__main__":
    unittest.main()
