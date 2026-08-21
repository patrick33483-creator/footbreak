from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SYSTEM = Path(__file__).resolve().parents[1]
ROOT = SYSTEM.parent
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import incident_alert
import server_health_monitor as monitor


NOW = datetime(2026, 8, 20, 20, 0, tzinfo=incident_alert.HKT)


def successful_systemctl(*_args, **_kwargs):
    return SimpleNamespace(
        stdout="Result=success\nExecMainStatus=0\nActiveState=inactive\n",
        returncode=0,
    )


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def valid_dashboard(path: Path, system: str) -> None:
    version = f"{system}-history-test"
    sidecar = path.parent / "history.json"
    write_json(path, {
        "history_data_url": "history.json",
        "history_data_version": version,
    })
    write_json(sidecar, {
        "schema_version": f"{system}-history-v1",
        "history_data_version": version,
        "prediction_history": {"rows": []},
    })


class ServerHealthMonitorTests(unittest.TestCase):
    def test_missing_timed_stage_is_checked_over_half_hour_window(self) -> None:
        ledger = {
            "watch": {
                "one": {
                    "kickoff": (NOW + timedelta(minutes=30)).isoformat(),
                    "discovered_at": (NOW - timedelta(hours=2)).isoformat(),
                    "stages": [{"stage": "首預"}],
                },
                "late-discovery": {
                    "kickoff": (NOW + timedelta(minutes=30)).isoformat(),
                    "discovered_at": (NOW + timedelta(minutes=31)).isoformat(),
                    "stages": [],
                },
            },
        }
        self.assertEqual(monitor.missing_native_stages(ledger, NOW), 1)
        ledger["watch"]["one"]["stages"].append({"stage": "T-30"})
        self.assertEqual(monitor.missing_native_stages(ledger, NOW), 0)

    def test_absence_of_telegram_or_wilson_candidate_is_not_healthy_by_assumption(self) -> None:
        # There is no message/event to inspect, but the native timed stage is
        # persisted.  A sample gate that created no eligible Wilson row must
        # remain deliberately quiet rather than become a notification fault.
        ledger = {
            "watch": {
                "one": {
                    "kickoff": (NOW + timedelta(minutes=30)).isoformat(),
                    "discovered_at": (NOW - timedelta(hours=1)).isoformat(),
                    "stages": [{"stage": "T-30", "ts": NOW.isoformat()}],
                },
            },
            "wilson_validation": {
                "conditions": {"sample-rejected": {"status": "sample_too_small"}},
                "observations": [],
            },
        }
        self.assertEqual(monitor.missing_native_stages(ledger, NOW), 0)
        self.assertEqual(monitor.stuck_notifications("footbreak", ledger, {}, NOW), 0)

    def test_cross_book_monitor_only_checks_due_t5_and_requires_evidence_and_outcome(self) -> None:
        """No due T-5 means no sidecar alert; a due T-5 needs both records."""
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.json"
            stage_at = NOW - timedelta(minutes=4)
            kickoff = NOW + timedelta(minutes=1)
            ledger = {
                "watch": {
                    "fixture-1": {
                        "match_id": "fixture-1",
                        "kickoff": kickoff.isoformat(),
                        "stages": [{"stage": "T-5", "ts": stage_at.isoformat()}],
                    },
                },
                "footbreak_crown_execution_test": {
                    "audit": [{
                        "match_id": "fixture-1",
                        "ts": (stage_at + timedelta(seconds=1)).isoformat(),
                        "status": "MATCHED_NO_BET",
                        "reason": "crown_wilson_gate_not_passed",
                    }],
                },
            }
            evidence.write_text(json.dumps([{
                "hkjc_match_id": "fixture-1",
                "kickoff_hkt": kickoff.isoformat(),
                "current_selected_odds_journal": [{
                    "observed_at": (stage_at - timedelta(seconds=10)).isoformat(),
                    "odds_status": "available",
                }],
            }]), encoding="utf-8")
            with patch.dict(
                os.environ,
                {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)},
                clear=False,
            ):
                self.assertEqual(monitor.cross_book_t5_findings(ledger, NOW), [])
                ledger["footbreak_crown_execution_test"]["audit"] = []
                self.assertEqual(
                    monitor.cross_book_t5_findings(ledger, NOW),
                    [monitor.Finding("footbreak", "cross_book_unevaluated_t5", 1)],
                )
                ledger["watch"]["fixture-1"]["stages"] = []
                self.assertEqual(monitor.cross_book_t5_findings(ledger, NOW), [])

    def test_cross_book_monitor_alerts_on_due_missing_or_stale_counterpart_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.json"
            stage_at = NOW - timedelta(minutes=4)
            kickoff = NOW + timedelta(minutes=1)
            ledger = {
                "watch": {"fixture-1": {
                    "match_id": "fixture-1", "kickoff": kickoff.isoformat(),
                    "stages": [{"stage": "T-5", "ts": stage_at.isoformat()}],
                }},
                "footbreak_crown_execution_test": {"audit": [{
                    "match_id": "fixture-1",
                    "ts": stage_at.isoformat(), "status": "SKIPPED",
                    "reason": "crown_execution_quote_stale_at_t5",
                }]},
            }
            evidence.write_text(json.dumps([{
                "hkjc_match_id": "fixture-1", "kickoff_hkt": kickoff.isoformat(),
                "current_selected_odds_journal": [{
                    "observed_at": (stage_at - timedelta(minutes=3)).isoformat(),
                    "odds_status": "available",
                }],
            }]), encoding="utf-8")
            with patch.dict(
                os.environ,
                {"FOOTBREAK_CROWN_EXECUTION_EVIDENCE_PATH": str(evidence)},
                clear=False,
            ):
                self.assertEqual(
                    monitor.cross_book_t5_findings(ledger, NOW),
                    [monitor.Finding("footbreak", "cross_book_counterpart_evidence", 1)],
                )

    def test_cross_book_health_alert_is_deduped_by_existing_local_incident_state(self) -> None:
        delivered: list[str] = []
        alerts = incident_alert.IncidentAlerts(
            Path(tempfile.mkdtemp()) / "state.json",
            sender=lambda text: delivered.append(text) or True,
        )
        health = {
            "footbreak": [monitor.Finding("footbreak", "cross_book_counterpart_evidence", 1)],
            "crown": [],
        }
        healthy_disk = SimpleNamespace(status=SimpleNamespace(free=10 * 1024**3))
        with patch.object(monitor, "assess", side_effect=lambda system, *_args: health[system]), \
             patch.object(monitor, "run_maintenance", return_value=healthy_disk):
            monitor.run(NOW, alerts=alerts, runner=successful_systemctl)
            monitor.run(NOW + timedelta(minutes=30), alerts=alerts, runner=successful_systemctl)
        self.assertEqual(len(delivered), 1)
        self.assertIn("足破×皇冠 T-5 對手證據缺失或過期", delivered[0])

    def test_only_qualified_unacknowledged_wilson_event_is_stuck(self) -> None:
        row = {
            "bet_id": "private-fixture-id",
            "portfolio": "footbreak_wilson_test",
            "strategy": "wilson-test-strategy-v1",
            "status": "PENDING",
            "created_at": (NOW - timedelta(minutes=13)).isoformat(),
            "kickoff": (NOW + timedelta(hours=1)).isoformat(),
        }
        ledger = {"bets": [row]}
        self.assertEqual(monitor.stuck_notifications("footbreak", ledger, {}, NOW), 1)
        self.assertEqual(
            monitor.stuck_notifications(
                "footbreak", ledger, {"wilson_match_alerts": ["private-fixture-id"]}, NOW,
            ),
            0,
        )
        row["portfolio"] = "footbreak_wilson_observations"
        row["bet_status"] = "NO_BET_LOW_ODDS"
        row["observation_id"] = row.pop("bet_id")
        self.assertEqual(monitor.stuck_notifications("footbreak", {"bets": [], "wilson_validation": {"observations": [row]}}, {}, NOW), 1)
        self.assertEqual(
            monitor.stuck_notifications(
                "footbreak", {"bets": []},
                {"outbox": [{
                    "status": "FAILED",
                    "created_at": (NOW - timedelta(minutes=13)).isoformat(),
                }]},
                NOW,
            ),
            1,
        )

    def test_sidecar_and_settlement_are_independent_local_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data.json"
            valid_dashboard(data, "footbreak")
            self.assertEqual(monitor.dashboard_sidecar_mismatch("footbreak", data), 0)
            write_json(root / "history.json", {"schema_version": "footbreak-history-v1"})
            self.assertEqual(monitor.dashboard_sidecar_mismatch("footbreak", data), 1)
        ledger = {
            "bets": [{
                "status": "PENDING",
                "kickoff": (NOW - timedelta(hours=5)).isoformat(),
            }],
        }
        self.assertEqual(monitor.settlement_backlog(ledger, NOW), 1)

    def test_reconcile_status_one_is_health_signal_not_stage_inference(self) -> None:
        def status_one(command, **_kwargs):
            unit = command[2]
            if unit == "footbreak-result-reconcile.service":
                return SimpleNamespace(
                    stdout="Result=exit-code\nExecMainStatus=1\nActiveState=inactive\n",
                    returncode=0,
                )
            return successful_systemctl()

        findings = monitor.local_service_findings("footbreak", status_one)
        self.assertEqual(findings, [monitor.Finding("footbreak", "health_check_failure", 1)])
        self.assertEqual(monitor.missing_native_stages({"watch": {}}, NOW), 0)

    def test_deduped_timeout_alert_needs_two_observations_and_two_healthy_recoveries(self) -> None:
        delivered: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for system in ("footbreak", "crown"):
                (root / system).mkdir()
                write_json(root / system / "ledger.json", {"watch": {}, "bets": []})
                write_json(root / system / "notify.json", {})
                valid_dashboard(root / system / "data.json", system)
            environment = {
                "FOOTBREAK_LEDGER_PATH": str(root / "footbreak" / "ledger.json"),
                "FOOTBREAK_NOTIFY_STATE_PATH": str(root / "footbreak" / "notify.json"),
                "FOOTBREAK_DATA": str(root / "footbreak" / "data.json"),
                "CROWN_LEDGER_PATH": str(root / "crown" / "ledger.json"),
                "CROWN_NOTIFY_STATE_PATH": str(root / "crown" / "notify.json"),
                "CROWN_DATA": str(root / "crown" / "data.json"),
                "SERVER_HEALTH_ALERT_COOLDOWN_SECONDS": "3600",
            }
            alerts = incident_alert.IncidentAlerts(
                root / "private-state.json", sender=lambda text: delivered.append(text) or True,
            )

            def timeout_runner(command, **_kwargs):
                if command[2] == "footbreak-tick.service":
                    return SimpleNamespace(
                        stdout="Result=timeout\nExecMainStatus=15\nActiveState=inactive\n",
                        returncode=0,
                    )
                return successful_systemctl()

            healthy_disk = SimpleNamespace(status=SimpleNamespace(free=10 * 1024**3))
            with patch.dict(os.environ, environment, clear=False), \
                 patch.object(monitor, "run_maintenance", return_value=healthy_disk):
                monitor.run(NOW, alerts=alerts, runner=timeout_runner)
                monitor.run(NOW + timedelta(minutes=30), alerts=alerts, runner=timeout_runner)
                monitor.run(NOW + timedelta(minutes=60), alerts=alerts, runner=successful_systemctl)
                monitor.run(NOW + timedelta(minutes=90), alerts=alerts, runner=successful_systemctl)

            payload = json.loads((root / "private-state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(delivered), 2)
        self.assertIn("排程連續逾時", delivered[0])
        self.assertIn("連續健康並恢復", delivered[1])
        self.assertNotIn("private-fixture", json.dumps(payload, ensure_ascii=False))

    def test_disk_pressure_uses_one_server_alert_and_two_healthy_recoveries(self) -> None:
        delivered: list[str] = []
        active: list[bool] = []
        alerts = incident_alert.IncidentAlerts(
            Path(tempfile.mkdtemp()) / "state.json",
            sender=lambda text: delivered.append(text) or True,
        )
        original_report = alerts.report

        def capture(**kwargs):
            if kwargs["kind"] == "disk_pressure":
                active.append(kwargs["active"])
            return original_report(**kwargs)

        alerts.report = capture
        empty = {"footbreak": [], "crown": []}
        low = SimpleNamespace(status=SimpleNamespace(free=100))
        high = SimpleNamespace(status=SimpleNamespace(free=10 * 1024**3))
        with patch.object(monitor, "assess", side_effect=lambda system, *_args: empty[system]), \
             patch.object(monitor, "warning_free_bytes", return_value=1000), \
             patch.object(monitor, "run_maintenance", side_effect=[low, high, high]):
            monitor.run(NOW, alerts=alerts, runner=successful_systemctl)
            monitor.run(NOW + timedelta(minutes=30), alerts=alerts, runner=successful_systemctl)
            monitor.run(NOW + timedelta(minutes=60), alerts=alerts, runner=successful_systemctl)
        self.assertEqual(active, [True, False, False])
        self.assertEqual(len(delivered), 2)
        self.assertIn("運作警報：伺服器", delivered[0])
        self.assertIn("磁碟", delivered[0])
        self.assertIn("運作恢復：伺服器", delivered[1])

    def test_systemd_and_deploy_contracts_install_only_local_monitor(self) -> None:
        service = (ROOT / "deploy/systemd/footbreak-server-health-monitor.service").read_text(encoding="utf-8")
        timer = (ROOT / "deploy/systemd/footbreak-server-health-monitor.timer").read_text(encoding="utf-8")
        update = (ROOT / "deploy/update.sh").read_text(encoding="utf-8")
        health = (ROOT / "deploy/health-check.sh").read_text(encoding="utf-8")
        self.assertIn("OnUnitActiveSec=30min", timer)
        self.assertIn("server_health_monitor.py", service)
        self.assertIn("TimeoutStartSec=120", service)
        self.assertIn("footbreak-server-health-monitor.timer", update)
        self.assertIn("reenable footbreak-server-health-monitor.timer", update)
        self.assertIn("is-enabled --quiet footbreak-server-health-monitor.timer", update)
        self.assertIn("footbreak-server-health-monitor.timer", health)
        self.assertNotIn("Radar", service + timer)
        self.assertNotIn("health-check.sh", service)


if __name__ == "__main__":
    unittest.main()
