from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import incident_alert as alerts_module


NOW = datetime(2026, 8, 18, 16, 0, tzinfo=alerts_module.HKT)


class IncidentAlertTests(unittest.TestCase):
    def test_incident_is_deduplicated_then_sends_one_traditional_chinese_recovery(self) -> None:
        delivered: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            alerts = alerts_module.IncidentAlerts(
                Path(directory) / "state.json", sender=delivered.append, cooldown_seconds=600
            )
            alerts.report(system="footbreak", kind="service_failure:footbreak-tick.service", active=True, now=NOW)
            alerts.report(
                system="footbreak", kind="service_failure:footbreak-tick.service", active=True,
                now=NOW + timedelta(minutes=2),
            )
            alerts.report(
                system="footbreak", kind="service_failure:footbreak-tick.service", active=False,
                now=NOW + timedelta(minutes=3),
            )
            alerts.report(
                system="footbreak", kind="service_failure:footbreak-tick.service", active=False,
                now=NOW + timedelta(minutes=4),
            )

        self.assertEqual(len(delivered), 2)
        self.assertIn("【運作警報：足破】", delivered[0])
        self.assertIn("排程／服務執行失敗或逾時", delivered[0])
        self.assertIn("【運作恢復：足破】", delivered[1])
        self.assertNotIn("token", "\n".join(delivered).lower())

    def test_cooldown_allows_a_repeat_only_after_expiry(self) -> None:
        delivered: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            alerts = alerts_module.IncidentAlerts(
                Path(directory) / "state.json", sender=delivered.append, cooldown_seconds=60
            )
            alerts.report(system="crown", kind="settlement_stuck", active=True, count=1, now=NOW)
            alerts.report(
                system="crown", kind="settlement_stuck", active=True, count=3,
                now=NOW + timedelta(seconds=61),
            )
        self.assertEqual(len(delivered), 2)
        self.assertTrue(all("皇冠" in message for message in delivered))

    def test_state_and_audit_are_durable_private_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "nested" / "state.json"
            alerts = alerts_module.IncidentAlerts(state, sender=lambda _text: True, cooldown_seconds=1)
            for index in range(alerts_module.STATE_LIMIT + 40):
                alerts.report(system="footbreak", kind=f"test-{index}", active=True, now=NOW)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertLessEqual(len(payload["incidents"]), alerts_module.STATE_LIMIT)
            self.assertLessEqual(len(payload["audit"]), alerts_module.AUDIT_LIMIT)
            self.assertEqual(oct(state.stat().st_mode & 0o777), "0o600")
            self.assertTrue(all("token" not in json.dumps(row).lower() for row in payload["audit"]))

    def test_monitor_detects_missed_t5_source_failure_and_stuck_settlement_then_recovers(self) -> None:
        delivered: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            footbreak = root / "footbreak.json"
            crown = root / "crown.json"
            footbreak.write_text(json.dumps({
                "watch": {
                    "missed": {"kickoff": (NOW - timedelta(minutes=10)).isoformat(), "stages": []},
                    "source": {"kickoff": (NOW - timedelta(minutes=20)).isoformat(), "stages": [{
                        "stage": "T-5", "source": "unavailable", "source_status": "analysis_exception",
                    }]},
                    "healthy": {"kickoff": (NOW - timedelta(minutes=5)).isoformat(), "stages": [{
                        "stage": "T-5", "source": "pinnapi", "pick": None, "verdict": "觀望",
                    }]},
                },
                "bets": [{"status": "PENDING", "kickoff": (NOW - timedelta(hours=5)).isoformat()}],
            }), encoding="utf-8")
            crown.write_text(json.dumps({
                "watch": {
                    "crown-missed": {"kickoff_hkt": (NOW - timedelta(minutes=10)).isoformat(), "stages": []},
                },
                "bets": [],
            }), encoding="utf-8")
            alerts = alerts_module.IncidentAlerts(root / "state.json", sender=delivered.append)
            findings = alerts_module.check_ledgers(
                alerts, system="all", footbreak_ledger=footbreak, crown_ledger=crown, now=NOW
            )
            self.assertEqual(findings["footbreak:missed_t5"], 1)
            self.assertEqual(findings["footbreak:source_persistence"], 1)
            self.assertEqual(findings["footbreak:settlement_stuck"], 1)
            self.assertEqual(findings["crown:missed_t5"], 1)

            footbreak.write_text(json.dumps({
                "watch": {"healthy": {"kickoff": (NOW - timedelta(minutes=5)).isoformat(), "stages": [{
                    "stage": "T-5", "source": "pinnapi", "pick": None, "verdict": "觀望",
                }]}},
                "bets": [{"status": "SETTLED", "kickoff": (NOW - timedelta(hours=5)).isoformat()}],
            }), encoding="utf-8")
            crown.write_text(json.dumps({"watch": {}, "bets": []}), encoding="utf-8")
            alerts_module.check_ledgers(
                alerts, system="all", footbreak_ledger=footbreak, crown_ledger=crown,
                now=NOW + timedelta(minutes=1),
            )

        text = "\n".join(delivered)
        self.assertIn("T-5 必要賽前快照", text)
        self.assertIn("資料來源故障", text)
        self.assertIn("模擬結算", text)
        self.assertEqual(sum("運作恢復" in message for message in delivered), 4)

    def test_normal_no_bet_and_recent_pending_settlement_never_alert(self) -> None:
        delivered: list[str] = []
        watches = [{
            "kickoff": (NOW - timedelta(minutes=3)).isoformat(),
            "stages": [{"stage": "T-5", "source": "pinnapi", "pick": None, "verdict": "觀望"}],
        }]
        bets = [{"status": "PENDING", "kickoff": (NOW - timedelta(minutes=90)).isoformat()}]
        findings = alerts_module._operational_findings(watches, bets, NOW)
        self.assertEqual(findings, {
            "missed_t5": 0, "source_persistence": 0, "settlement_stuck": 0,
        })
        with tempfile.TemporaryDirectory() as directory:
            alerts = alerts_module.IncidentAlerts(Path(directory) / "state.json", sender=delivered.append)
            for kind, count in findings.items():
                alerts.report(system="footbreak", kind=kind, active=count > 0, count=count, now=NOW)
        self.assertEqual(delivered, [])

    def test_existing_telegram_configuration_is_used_without_exposing_credentials(self) -> None:
        response = Mock()
        response.read.return_value = json.dumps({"ok": True}).encode()
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret-token", "TELEGRAM_CHAT_ID": "123"}, clear=False), \
             patch.object(alerts_module.urllib.request, "urlopen", return_value=context) as opener:
            self.assertTrue(alerts_module.telegram_sender("footbreak")("【運作警報：足破】測試"))
        request = opener.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["chat_id"], "123")
        self.assertNotIn("secret-token", payload["text"])

    def test_crown_incident_uses_the_separate_crown_transport(self) -> None:
        response = Mock()
        response.read.return_value = json.dumps({"ok": True}).encode()
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        environment = {
            "TELEGRAM_BOT_TOKEN": "footbreak-token",
            "TELEGRAM_CHAT_ID": "footbreak-chat",
            "CROWN_TELEGRAM_ENABLED": "1",
            "CROWN_TELEGRAM_BOT_TOKEN": "crown-token",
            "CROWN_TELEGRAM_CHAT_ID": "crown-chat",
        }
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, environment, clear=False), \
             patch.object(alerts_module.urllib.request, "urlopen", return_value=context) as opener:
            alerts = alerts_module.IncidentAlerts(Path(directory) / "state.json")
            alerts.report(system="crown", kind="service_failure:crown-tick.service", active=True, now=NOW)
        request = opener.call_args.args[0]
        self.assertIn("/botcrown-token/sendMessage", request.full_url)
        self.assertEqual(json.loads(request.data.decode("utf-8"))["chat_id"], "crown-chat")

    def test_documented_preemption_is_suppressed_but_timeout_is_an_incident(self) -> None:
        with patch.object(alerts_module.subprocess, "run") as run:
            run.return_value = Mock(stdout="timeout\n15\n", returncode=0)
            self.assertFalse(alerts_module._systemd_expected_preemption("footbreak-tick.service"))
        with patch.object(alerts_module.subprocess, "run") as run:
            run.return_value = Mock(stdout="failed\n75\n", returncode=0)
            self.assertTrue(alerts_module._systemd_expected_preemption("footbreak-tick.service"))

    def test_existing_services_and_health_check_reuse_the_alert_boundaries(self) -> None:
        root = SYSTEM.parents[0]
        for name in (
            "footbreak-tick.service", "footbreak-sweep.service", "footbreak-settle.service",
            "crown-tick.service", "crown-sweep.service", "crown-settle.service",
        ):
            with self.subTest(service=name):
                unit = (root / "deploy" / "systemd" / name).read_text(encoding="utf-8")
                self.assertIn("OnFailure=footbreak-incident-alert@%n.service", unit)
        handler = (root / "deploy" / "systemd" / "footbreak-incident-alert@.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("systemd-failure --unit %I", handler)
        health = (root / "deploy" / "health-check.sh").read_text(encoding="utf-8")
        self.assertIn("trap report_health_check_exit EXIT", health)
        self.assertIn('"$ALERT_HELPER" check --system all', health)


if __name__ == "__main__":
    unittest.main()
