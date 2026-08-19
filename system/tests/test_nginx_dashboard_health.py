from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class NginxDashboardHealthTests(unittest.TestCase):
    def test_crown_history_sidecar_survives_static_sync_and_is_never_cached(self):
        nginx = (ROOT / "deploy" / "nginx-crown.conf").read_text(encoding="utf-8")
        setup = (ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

        self.assertIn("location = /history.json {", nginx)
        self.assertIn('add_header Cache-Control "no-store, must-revalidate";', nginx)
        for script in (setup, update):
            self.assertIn("--exclude 'history.json'", script)
            self.assertIn("history.json", script)

    def test_deploy_repairs_auth_and_static_permissions(self):
        script = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

        self.assertIn(
            "for auth_file in /etc/nginx/.htpasswd-footbreak "
            "/etc/nginx/.htpasswd-crown",
            script,
        )
        self.assertIn('chown root:www-data "$auth_file"', script)
        self.assertIn('chmod 0640 "$auth_file"', script)
        self.assertIn('runuser -u www-data -- test -r "$auth_file"', script)
        self.assertIn("chmod 0755 /etc /etc/nginx", script)
        self.assertIn(
            'repair_auth_identity "$footbreak_auth" footbreak '
            "/root/footbreak-dashboard-password.txt",
            script,
        )
        self.assertIn(
            'repair_auth_identity "$crown_auth" crown '
            "/root/crown-dashboard-password.txt",
            script,
        )
        self.assertNotIn(
            'install -o root -g www-data -m 0640 "$crown_auth" "$footbreak_auth"',
            script,
        )
        self.assertNotIn(
            'install -o root -g www-data -m 0640 "$footbreak_auth" "$crown_auth"',
            script,
        )
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

    def test_all_auth_recovery_paths_preserve_dashboard_identity(self):
        paths = (
            ROOT / "deploy" / "update.sh",
            ROOT / "deploy" / "dashboard-self-heal.sh",
            ROOT / ".github" / "workflows" / "dashboard-emergency-repair.yml",
        )
        for path in paths:
            script = path.read_text(encoding="utf-8")
            self.assertIn("/root/footbreak-dashboard-password.txt", script)
            self.assertIn("/root/crown-dashboard-password.txt", script)
            self.assertIn("htpasswd -bc", script)
            self.assertNotIn(
                'install -o root -g www-data -m 0640 "$crown" "$footbreak"',
                script,
            )
            self.assertNotIn(
                'install -o root -g www-data -m 0640 "$footbreak" "$crown"',
                script,
            )

    def test_health_check_requires_the_expected_auth_accounts(self):
        health = (ROOT / "deploy" / "health-check.sh").read_text(encoding="utf-8")

        self.assertIn("/etc/nginx/.htpasswd-footbreak:footbreak", health)
        self.assertIn("/etc/nginx/.htpasswd-crown:crown", health)
        self.assertIn('grep -q "^${expected_user}:" "$auth_file"', health)

    def test_dashboard_data_health_check_retries_transient_timeouts(self):
        health = (ROOT / "deploy" / "health-check.sh").read_text(encoding="utf-8")

        self.assertIn("dashboard_data_ready=0", health)
        self.assertIn("for _ in $(seq 1 10)", health)
        self.assertIn("dashboard API /api/data did not become ready after retries", health)


if __name__ == "__main__":
    unittest.main()
