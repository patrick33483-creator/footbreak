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


def sent_to(messages: list[str]):
    def sender(text: str) -> bool:
        messages.append(text)
        return True
    return sender


def ledger_payload(*, watches: dict | None = None, bets: list | None = None) -> dict:
    return {"watch": watches or {}, "bets": bets or []}


class IncidentAlertTests(unittest.TestCase):
    def test_first_start_silently_baselines_historical_backlog(self) -> None:
        delivered: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "footbreak.json"
            ledger.write_text(json.dumps(ledger_payload(
                watches={
                    "old-missed": {"kickoff": (NOW - timedelta(minutes=20)).isoformat(), "stages": []},
                    "old-source": {
                        "kickoff": (NOW - timedelta(minutes=25)).isoformat(),
                        "stages": [{"stage": "T-5", "source": "unavailable", "ts": (NOW - timedelta(minutes=21)).isoformat()}],
                    },
                },
                bets=[{"status": "PENDING", "last_settlement_attempt_at": (NOW - timedelta(hours=5)).isoformat()}],
            )), encoding="utf-8")
            state = root / "state.json"
            alerts = alerts_module.IncidentAlerts(state, sender=sent_to(delivered))

            first = alerts_module.check_ledgers(alerts, system="footbreak", footbreak_ledger=ledger, now=NOW)
            later = alerts_module.check_ledgers(
                alerts, system="footbreak", footbreak_ledger=ledger, now=NOW + timedelta(minutes=1)
            )
            saved = json.loads(state.read_text(encoding="utf-8"))

        self.assertEqual(first, {"footbreak:missed_t5": 0, "footbreak:source_persistence": 0, "footbreak:settlement_stuck": 0})
        self.assertEqual(later, first)
        self.assertEqual(saved["monitoring_started_at"], NOW.isoformat(timespec="seconds"))
        self.assertFalse(saved["incidents"]["footbreak:ledger_digest"]["active"])
        self.assertEqual(saved["audit"], [])
        self.assertEqual(delivered, [])

    def test_new_ledger_findings_need_two_observations_group_once_and_recover_stably(self) -> None:
        delivered: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "footbreak.json"
            ledger.write_text(json.dumps(ledger_payload()), encoding="utf-8")
            alerts = alerts_module.IncidentAlerts(root / "state.json", sender=sent_to(delivered))
            alerts_module.check_ledgers(alerts, system="footbreak", footbreak_ledger=ledger, now=NOW)

            ledger.write_text(json.dumps(ledger_payload(
                watches={
                    "missed": {"kickoff": (NOW + timedelta(minutes=1)).isoformat(), "stages": []},
                    "source": {
                        "kickoff": (NOW + timedelta(minutes=2)).isoformat(),
                        "stages": [{"stage": "T-5", "source": "unavailable", "source_status": "analysis_exception", "ts": (NOW + timedelta(minutes=3)).isoformat()}],
                    },
                },
                bets=[{"status": "PENDING", "last_settlement_attempt_at": (NOW + timedelta(minutes=5)).isoformat()}],
            )), encoding="utf-8")
            first = alerts_module.check_ledgers(
                alerts, system="footbreak", footbreak_ledger=ledger, now=NOW + timedelta(hours=5)
            )
            second = alerts_module.check_ledgers(
                alerts, system="footbreak", footbreak_ledger=ledger, now=NOW + timedelta(hours=5, minutes=1)
            )
            alerts_module.check_ledgers(
                alerts, system="footbreak", footbreak_ledger=ledger, now=NOW + timedelta(hours=5, minutes=2)
            )

            ledger.write_text(json.dumps(ledger_payload()), encoding="utf-8")
            alerts_module.check_ledgers(
                alerts, system="footbreak", footbreak_ledger=ledger, now=NOW + timedelta(hours=5, minutes=3)
            )
            alerts_module.check_ledgers(
                alerts, system="footbreak", footbreak_ledger=ledger, now=NOW + timedelta(hours=5, minutes=4)
            )

        self.assertEqual(first, {"footbreak:missed_t5": 1, "footbreak:source_persistence": 1, "footbreak:settlement_stuck": 1})
        self.assertEqual(second, first)
        self.assertEqual(len(delivered), 2)
        self.assertIn("【運作警報：足破】", delivered[0])
        self.assertIn("T-5 快照逾時未保存 1 項", delivered[0])
        self.assertIn("必要資料來源未能保存 1 項", delivered[0])
        self.assertIn("模擬結算逾時 1 項", delivered[0])
        self.assertIn("【運作恢復：足破】", delivered[1])
        self.assertIn("連續健康", delivered[1])

    def test_settlement_requires_recent_post_start_attempt_not_kickoff_age(self) -> None:
        started = NOW
        now = NOW + timedelta(hours=5)
        watches: list[dict] = []
        old_pending = [{"status": "PENDING", "kickoff": (NOW - timedelta(days=2)).isoformat()}]
        pre_start_attempt = [{"status": "PENDING", "last_settlement_attempt_at": (NOW - timedelta(minutes=1)).isoformat()}]
        recent_attempt = [{"status": "PENDING", "last_settlement_attempt_at": (NOW + timedelta(minutes=30)).isoformat()}]

        self.assertEqual(alerts_module._operational_findings(watches, old_pending, started, now)["settlement_stuck"], 0)
        self.assertEqual(alerts_module._operational_findings(watches, pre_start_attempt, started, now)["settlement_stuck"], 0)
        self.assertEqual(alerts_module._operational_findings(watches, recent_attempt, started, now)["settlement_stuck"], 1)

    def test_no_qualifying_bet_or_observe_recommendation_is_never_an_incident(self) -> None:
        findings = alerts_module._operational_findings(
            [{
                "kickoff": (NOW + timedelta(minutes=2)).isoformat(),
                "stages": [{"stage": "T-5", "source": "pinnapi", "pick": None, "verdict": "觀望"}],
            }],
            [], NOW, NOW + timedelta(minutes=3),
        )
        self.assertEqual(findings, {"missed_t5": 0, "source_persistence": 0, "settlement_stuck": 0})

    def test_disabled_master_flag_is_silent_and_does_not_create_recovery_state(self) -> None:
        delivered: list[str] = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"INCIDENT_ALERT_ENABLED": "0"}, clear=False):
            state = Path(directory) / "state.json"
            alerts = alerts_module.IncidentAlerts(state, sender=sent_to(delivered))
            self.assertFalse(alerts.report(system="footbreak", kind="service_failure:footbreak-tick.service", active=True, now=NOW))
            self.assertEqual(alerts_module.check_ledgers(alerts, system="footbreak", now=NOW), {})
            self.assertFalse(state.exists())
        self.assertEqual(delivered, [])

    def test_service_failure_is_immediate_deduplicated_and_needs_two_healthy_runs(self) -> None:
        delivered: list[str] = []
        unit = "footbreak-tick.service"
        key = alerts_module.service_incident_key(unit)
        with tempfile.TemporaryDirectory() as directory:
            alerts = alerts_module.IncidentAlerts(Path(directory) / "state.json", sender=sent_to(delivered))
            alerts.report(system="footbreak", kind=key, active=True, now=NOW)
            alerts.report(system="footbreak", kind=key, active=True, now=NOW + timedelta(minutes=1))
            alerts.report(system="footbreak", kind=key, active=False, healthy_needed=2, now=NOW + timedelta(minutes=2))
            alerts.report(system="footbreak", kind=key, active=True, now=NOW + timedelta(minutes=3))
            alerts.report(system="footbreak", kind=key, active=False, healthy_needed=2, now=NOW + timedelta(minutes=4))
            alerts.report(system="footbreak", kind=key, active=False, healthy_needed=2, now=NOW + timedelta(minutes=5))

        self.assertEqual(len(delivered), 2)
        self.assertIn("排程／服務執行失敗或逾時", delivered[0])
        self.assertIn("連續健康並恢復", delivered[1])
        self.assertEqual(key, "service_failure:footbreak-tick.service")

    def test_health_check_anomaly_uses_traditional_chinese_and_stable_recovery(self) -> None:
        delivered: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            alerts = alerts_module.IncidentAlerts(Path(directory) / "state.json", sender=sent_to(delivered))
            alerts.report(system="footbreak", kind="health_check_failure", active=True, now=NOW)
            alerts.report(
                system="footbreak", kind="health_check_failure", active=False,
                healthy_needed=alerts_module.SERVICE_HEALTHY_OBSERVATIONS, now=NOW + timedelta(minutes=1),
            )
            alerts.report(
                system="footbreak", kind="health_check_failure", active=False,
                healthy_needed=alerts_module.SERVICE_HEALTHY_OBSERVATIONS, now=NOW + timedelta(minutes=2),
            )
        self.assertEqual(len(delivered), 2)
        self.assertIn("部署或健康檢查異常", delivered[0])
        self.assertIn("連續健康並恢復", delivered[1])

    def test_state_and_audit_are_durable_bounded_and_keep_only_category_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "nested" / "state.json"
            alerts = alerts_module.IncidentAlerts(state, sender=lambda _text: True)
            for index in range(alerts_module.STATE_LIMIT + 40):
                alerts.report(
                    system="footbreak", kind=f"test-{index}", active=True,
                    details={"missed_t5": 1, "provider-fixture-secret-999": 7}, now=NOW + timedelta(seconds=index),
                )
            payload = json.loads(state.read_text(encoding="utf-8"))
            mode = oct(state.stat().st_mode & 0o777)

        self.assertLessEqual(len(payload["incidents"]), alerts_module.STATE_LIMIT)
        self.assertLessEqual(len(payload["audit"]), alerts_module.AUDIT_LIMIT)
        self.assertEqual(mode, "0o600")
        serialised = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("provider-fixture-secret-999", serialised)
        self.assertNotIn("token", serialised.lower())

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

    def test_preemption_is_suppressed_and_wrappers_share_the_systemd_key_boundary(self) -> None:
        with patch.object(alerts_module.subprocess, "run") as run:
            run.return_value = Mock(stdout="failed\n75\n", returncode=0)
            self.assertTrue(alerts_module._systemd_expected_preemption("footbreak-tick.service"))
        root = SYSTEM.parents[0]
        for runner in ("run.sh", "crown-run.sh"):
            self.assertIn('--unit "$SERVICE_UNIT"', (root / "deploy" / runner).read_text(encoding="utf-8"))
        for name in (
            "footbreak-tick.service", "footbreak-sweep.service", "footbreak-settle.service",
            "crown-tick.service", "crown-sweep.service", "crown-settle.service",
        ):
            self.assertIn("OnFailure=footbreak-incident-alert@%n.service", (root / "deploy" / "systemd" / name).read_text(encoding="utf-8"))
        handler = (root / "deploy" / "systemd" / "footbreak-incident-alert@.service").read_text(encoding="utf-8")
        self.assertIn("systemd-failure --unit %I", handler)


if __name__ == "__main__":
    unittest.main()
