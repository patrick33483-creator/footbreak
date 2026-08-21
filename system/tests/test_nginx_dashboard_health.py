import json
import sys
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]


class NginxDashboardHealthTests(unittest.TestCase):
    @staticmethod
    def _dashboard_health_python() -> str:
        health = (ROOT / "deploy" / "health-check.sh").read_text(encoding="utf-8")
        marker = (
            '"$APP_DIR/.venv/bin/python3" - "$FOOTBREAK_DATA" '
            '"$CROWN_DATA" <<\'PY\'\n'
        )
        start = health.index(marker) + len(marker)
        return health[start:health.index("\nPY\n", start)]

    @staticmethod
    def _market_row() -> dict:
        return {
            "kickoff": "2026-08-20T01:00:00+08:00",
            "actual": {"goals_home": 1, "goals_away": 0},
            "score": {"HDC": "WON"},
            "market_predictions": [
                {"code": "HDC", "side": "H", "line": -0.25}
            ],
        }

    def test_dashboard_health_reads_versioned_footbreak_and_crown_history_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            foot_dir = root / "footbreak"
            crown_dir = root / "crown"
            foot_dir.mkdir()
            crown_dir.mkdir()
            row = self._market_row()
            foot = {
                "prediction_history": {"stats": {"graded": 1}},
                "history_data_url": "history.json",
                "history_data_version": "foot-v1",
            }
            crown = {
                "matches": [],
                "prediction_history": {"stats": {}},
                "history_data_url": "history.json",
                "history_data_version": "crown-v1",
            }
            (foot_dir / "data.json").write_text(json.dumps(foot), encoding="utf-8")
            (crown_dir / "data.json").write_text(json.dumps(crown), encoding="utf-8")
            (foot_dir / "history.json").write_text(json.dumps({
                "schema_version": "footbreak-history-v1",
                "history_data_version": "foot-v1",
                "prediction_history": {"stats": {"graded": 1}, "rows": [row]},
            }), encoding="utf-8")
            (crown_dir / "history.json").write_text(json.dumps({
                "schema_version": "crown-history-v1",
                "history_data_version": "crown-v1",
                "prediction_history": {"stats": {}, "rows": [row]},
            }), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable, "-",
                    str(foot_dir / "data.json"),
                    str(crown_dir / "data.json"),
                ],
                input=self._dashboard_health_python(),
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Footbreak prediction history market rows=1", result.stdout)
        self.assertIn("Crown prediction history market rows=1", result.stdout)

    def test_dashboard_health_rejects_mixed_history_sidecar_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            foot_dir = root / "footbreak"
            crown_dir = root / "crown"
            foot_dir.mkdir()
            crown_dir.mkdir()
            for target, system, version in (
                (foot_dir, "footbreak", "foot-v1"),
                (crown_dir, "crown", "crown-v1"),
            ):
                (target / "data.json").write_text(json.dumps({
                    "matches": [],
                    "prediction_history": {"stats": {}},
                    "history_data_url": "history.json",
                    "history_data_version": version,
                }), encoding="utf-8")
                (target / "history.json").write_text(json.dumps({
                    "schema_version": f"{system}-history-v1",
                    "history_data_version": (
                        "wrong-generation" if system == "footbreak" else version
                    ),
                    "prediction_history": {"stats": {}, "rows": []},
                }), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable, "-",
                    str(foot_dir / "data.json"),
                    str(crown_dir / "data.json"),
                ],
                input=self._dashboard_health_python(),
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Footbreak dashboard/history sidecar version mismatch",
            result.stderr,
        )

    def test_crown_history_sidecar_survives_static_sync_and_is_never_cached(self):
        nginx = (ROOT / "deploy" / "nginx-crown.conf").read_text(encoding="utf-8")
        setup = (ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

        self.assertIn("location = /history.json {", nginx)
        self.assertIn('add_header Cache-Control "no-store, must-revalidate";', nginx)
        for script in (setup, update):
            self.assertIn("--exclude 'history.json'", script)
            self.assertIn("history.json", script)

    def test_public_subpath_routes_use_authenticated_dashboard_apis(self):
        nginx = (
            ROOT / "deploy" / "nginx-unified-dashboard.conf"
        ).read_text(encoding="utf-8")
        setup = (ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        app = (ROOT / "crown" / "dashboard" / "app.js").read_text(encoding="utf-8")

        self.assertIn("location ^~ /crown/api/ {", nginx)
        self.assertIn("proxy_pass http://127.0.0.1:8765/api/;", nginx)
        self.assertIn("location ^~ /footbreak/api/ {", nginx)
        self.assertIn("proxy_pass http://127.0.0.1:8766/api/;", nginx)
        self.assertIn("CROWN_PUBLIC_PREFIX", app)
        self.assertIn("CURRENT_PATH.startsWith('/crown/')", app)
        self.assertIn("`${CROWN_PUBLIC_PREFIX}/api`", app)
        for script in (setup, update):
            self.assertIn("nginx-unified-dashboard.conf", script)
            self.assertIn(
                "/etc/nginx/sites-enabled/unified-dashboard",
                script,
            )

    def test_footbreak_history_sidecar_is_no_store_and_not_overwritten_on_update(self):
        nginx = (ROOT / "deploy" / "nginx-footbreak.conf").read_text(encoding="utf-8")
        setup = (ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        runner = (ROOT / "deploy" / "run.sh").read_text(encoding="utf-8")

        self.assertIn("location = /history.json {", nginx)
        self.assertIn('add_header Cache-Control "no-store, must-revalidate";', nginx)
        for script in (setup, update):
            self.assertIn("--exclude 'history.json'", script)
            self.assertIn('chmod 0644 "$WEB_ROOT/history.json"', script)
        self.assertIn('install -m 0644 "$APP_DIR/hkjc-dashboard/history.json" "$WEB_ROOT/history.json"', runner)

    def test_footbreak_setup_bootstraps_only_missing_runtime_payloads_offline(self):
        setup = (ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
        update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

        # Both paths preserve runtime artifacts during static synchronization.
        for script in (setup, update):
            self.assertIn("--exclude 'data.json' --exclude 'history.json'", script)
        # A new host has no runtime payload, so setup creates a minimal,
        # schema-compatible payload before nginx starts. This mode neither
        # reads runtime state nor makes a provider request.
        self.assertIn('if [ ! -f "$WEB_ROOT/data.json" ]; then', setup)
        self.assertIn('-m system.gen_app_data', setup)
        self.assertIn('--bootstrap-empty --out "$WEB_ROOT/data.json"', setup)
        self.assertIn('毋須 state 或網絡', setup)
        self.assertIn(
            'if [ ! -f "$WEB_ROOT/data.json" ] || [ ! -f "$WEB_ROOT/history.json" ]; then',
            setup,
        )
        self.assertIn(
            'chown root:www-data "$WEB_ROOT/data.json" "$WEB_ROOT/history.json"',
            setup,
        )
        # Update preserves runtime artifacts during static sync, then repairs
        # the versioned pair from persisted local state under the same lock as
        # tick/sweep/settle.  This path is provider-free and publishes the
        # history sidecar before the matching boot payload.
        self.assertIn('exec 8>/var/lock/footbreak.lock', update)
        self.assertIn('flock -w 60 8', update)
        self.assertIn("-m system.gen_app_data", update)
        self.assertIn('--out "$WEB_ROOT/data.json"', update)
        self.assertIn(
            'chown root:www-data "$WEB_ROOT/data.json" "$WEB_ROOT/history.json"',
            update,
        )
        self.assertNotIn(
            '"$APP_DIR/hkjc-dashboard/data.json" "$WEB_ROOT/data.json"',
            update,
        )

    def test_footbreak_setup_bootstrap_is_state_free_and_preserves_upgrade_payload(self):
        setup = (ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
        start = setup.index("bootstrap_footbreak_web_root() {")
        end = setup.index("\n}\n\necho ", start) + 2
        function = setup[start:end]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            web = root / "web"
            app.mkdir()
            web.mkdir()

            harness = textwrap.dedent(f"""\
                set -euo pipefail
                chown() {{ :; }}
                chmod() {{ :; }}
                {function}
                APP_DIR={app}
                WEB_ROOT={web}
                FOOTBREAK_DASHBOARD_PYTHON={sys.executable}
                PYTHONPATH={ROOT}
                bootstrap_footbreak_web_root
            """)
            first = subprocess.run(
                ["bash", "-c", harness], text=True, capture_output=True, check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            # The synthetic app has no hkjc payload and no state files. The
            # setup helper must still create a readable browser contract.
            self.assertFalse((app / "system" / "predictions.json").exists())
            self.assertFalse((app / "system" / "sim_ledger.json").exists())
            payload = json.loads((web / "data.json").read_text(encoding="utf-8"))
            history = json.loads((web / "history.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["dashboard_status"]["state"], "not_yet_run")
            self.assertEqual(payload["matches"], [])
            self.assertEqual(payload["ledger"]["bets"], [])
            self.assertEqual(payload["history_data_url"], "history.json")
            self.assertEqual(
                payload["history_data_version"], history["history_data_version"]
            )
            self.assertEqual(history["schema_version"], "footbreak-history-v1")
            self.assertEqual(history["prediction_history"]["rows"], [])

            # A setup rerun is an upgrade: existing runtime artifacts win even
            # if the bootstrap implementation later changes.
            (web / "data.json").write_text('{"runtime":"keep"}', encoding="utf-8")
            (web / "history.json").write_text('{"runtime_history":"keep"}', encoding="utf-8")
            second = subprocess.run(
                ["bash", "-c", harness], text=True, capture_output=True, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual((web / "data.json").read_text(encoding="utf-8"), '{"runtime":"keep"}')
            self.assertEqual(
                (web / "history.json").read_text(encoding="utf-8"),
                '{"runtime_history":"keep"}',
            )

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

    def test_dashboard_guards_validate_authenticated_static_json(self):
        health = (ROOT / "deploy" / "health-check.sh").read_text(encoding="utf-8")
        self_heal = (ROOT / "deploy" / "dashboard-self-heal.sh").read_text(
            encoding="utf-8"
        )
        emergency = (
            ROOT / ".github" / "workflows" / "dashboard-emergency-repair.yml"
        ).read_text(encoding="utf-8")

        for script in (health, self_heal, emergency):
            self.assertIn("/data.json?health=", script)
            self.assertIn("api/data", script)
            self.assertIn("footbreak-dashboard", script)
            self.assertIn("crown-dashboard-v2", script)
            self.assertIn("/root/footbreak-dashboard-password.txt", script)
            self.assertIn("/root/crown-dashboard-password.txt", script)
        self.assertIn("nginx-unified-dashboard.conf", self_heal)
        self.assertIn("nginx-unified-dashboard.conf", emergency)
        self.assertIn("republish_dashboard_json crown", self_heal)
        self.assertIn("-m crown.dashboard_data", emergency)

    def test_dashboard_data_health_check_retries_transient_timeouts(self):
        health = (ROOT / "deploy" / "health-check.sh").read_text(encoding="utf-8")

        self.assertIn("dashboard_data_ready=0", health)
        self.assertIn("for _ in $(seq 1 10)", health)
        self.assertIn("dashboard API /api/data did not become ready after retries", health)


if __name__ == "__main__":
    unittest.main()
