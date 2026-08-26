from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import telegram_silence_monitor as monitor


NOW = datetime(2026, 8, 27, 1, 0, tzinfo=monitor.HKT)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def create_radar(path: Path, *, pending: bool = False, healthy: bool = True) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE simulation_bets (
          unique_key TEXT, placed_at INTEGER, excluded_from_stats INTEGER
        );
        CREATE TABLE app_state (
          key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER
        );
        CREATE TABLE provider_health (
          provider TEXT PRIMARY KEY, ok INTEGER, consecutive_failures INTEGER
        );
        """
    )
    for provider in ("hkjc", "pinnacle"):
        db.execute(
            "INSERT INTO provider_health VALUES(?,?,?)",
            (provider, 1 if healthy else 0, 0 if healthy else 3),
        )
    if pending:
        db.execute(
            "INSERT INTO simulation_bets VALUES(?,?,0)",
            ("case2|fixture|2.5|O", int((NOW - timedelta(minutes=20)).timestamp() * 1000)),
        )
    db.commit()
    db.close()


def healthy_systemd(command, **_kwargs):
    if command[1] == "is-active":
        return SimpleNamespace(returncode=0, stdout="")
    if command[1] == "is-failed":
        return SimpleNamespace(returncode=1, stdout="")
    raise AssertionError(command)


class TelegramSilenceMonitorTests(unittest.TestCase):
    def fixture(self, root: Path, *, radar_pending: bool = False, radar_healthy: bool = True) -> Path:
        footbreak = root / "footbreak"
        crown = root / "crown"
        write_json(footbreak / "ledger.json", {"watch": {}, "bets": []})
        write_json(footbreak / "notify.json", {})
        write_json(crown / "ledger.json", {"watch": {}, "bets": []})
        write_json(crown / "notify.json", {})
        radar = root / "radar.db"
        create_radar(radar, pending=radar_pending, healthy=radar_healthy)
        self.paths = patch.multiple(
            monitor,
            FOOTBREAK_LEDGER=footbreak / "ledger.json",
            FOOTBREAK_NOTIFY=footbreak / "notify.json",
            CROWN_STATE=crown,
        )
        return radar

    def test_waits_one_hour_then_sends_normal_summary(self) -> None:
        delivered: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            radar = self.fixture(root)
            state = root / "state.json"
            with self.paths, patch.object(monitor, "_radar_loopback_healthy", return_value=True):
                first = monitor.run(
                    NOW, state_path=state, radar_db=radar,
                    sender=lambda text: delivered.append(text) or True,
                    runner=healthy_systemd,
                )
                due = monitor.run(
                    NOW + timedelta(hours=1), state_path=state, radar_db=radar,
                    sender=lambda text: delivered.append(text) or True,
                    runner=healthy_systemd,
                )
                deduped = monitor.run(
                    NOW + timedelta(hours=1, minutes=5), state_path=state, radar_db=radar,
                    sender=lambda text: delivered.append(text) or True,
                    runner=healthy_systemd,
                )
        self.assertFalse(first["due"])
        self.assertTrue(due["sent"])
        self.assertEqual(due["classification"], "no_signal")
        self.assertFalse(deduped["due"])
        self.assertEqual(len(delivered), 1)
        self.assertIn("冇合資格訊號", delivered[0])

    def test_pending_radar_bet_is_missed_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            radar = self.fixture(root, radar_pending=True)
            state = root / "state.json"
            write_json(state, {
                "monitoring_started_at": (NOW - timedelta(hours=2)).isoformat(),
            })
            delivered: list[str] = []
            with self.paths, patch.object(monitor, "_radar_loopback_healthy", return_value=True):
                result = monitor.run(
                    NOW, state_path=state, radar_db=radar,
                    sender=lambda text: delivered.append(text) or True,
                    runner=healthy_systemd,
                )
        self.assertEqual(result["classification"], "missed_delivery")
        self.assertTrue(result["sent"])
        self.assertIn("疑似漏發", delivered[0])

    def test_unhealthy_radar_is_system_fault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            radar = self.fixture(root, radar_healthy=False)
            state = root / "state.json"
            write_json(state, {
                "monitoring_started_at": (NOW - timedelta(hours=2)).isoformat(),
            })
            delivered: list[str] = []
            with self.paths, patch.object(monitor, "_radar_loopback_healthy", return_value=False):
                result = monitor.run(
                    NOW, state_path=state, radar_db=radar,
                    sender=lambda text: delivered.append(text) or True,
                    runner=healthy_systemd,
                )
        self.assertEqual(result["classification"], "system_fault")
        self.assertTrue(result["sent"])
        self.assertIn("系統故障", delivered[0])
        self.assertIn("Radar", delivered[0])

    def test_unit_contract_is_server_owned_and_five_minute(self) -> None:
        root = SYSTEM.parent
        service = (root / "deploy/systemd/telegram-silence-monitor.service").read_text(encoding="utf-8")
        timer = (root / "deploy/systemd/telegram-silence-monitor.timer").read_text(encoding="utf-8")
        update = (root / "deploy/update.sh").read_text(encoding="utf-8")
        self.assertIn("TG_SILENCE_MONITOR_SECONDS=3600", service)
        self.assertIn("OnUnitActiveSec=5min", timer)
        self.assertIn("telegram-silence-monitor.timer", update)
        self.assertNotIn("pplx", service.lower() + timer.lower())


if __name__ == "__main__":
    unittest.main()
