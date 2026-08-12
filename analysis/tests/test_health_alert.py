from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from analysis import health_alert


NOW = datetime(2026, 8, 12, 10, 20, tzinfo=timezone.utc)


def report(system: str, **changes: object) -> dict[str, object]:
    overall = {
        "duplicate_stage_keys": 0,
        "result": {
            "settle_due_fixtures": 0,
            "fixtures_with_result": 0,
            "stale_unresolved_fixtures": 0,
        },
        "corner_result": {
            "coverage": 1.0,
            "stale_beyond_retry_fixtures": 0,
        },
    }
    for key, value in changes.items():
        if key in overall:
            overall[key] = value
        elif key in overall["result"]:
            overall["result"][key] = value
        elif key in overall["corner_result"]:
            overall["corner_result"][key] = value
        else:
            raise AssertionError(f"unknown report change: {key}")
    return {
        "report": "data_health",
        "system": system,
        "completeness": {"overall": overall},
    }


class HealthAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.footbreak = self.directory / "footbreak.json"
        self.crown = self.directory / "crown.json"
        self.write_reports()

    def write_reports(
        self, footbreak: dict[str, object] | None = None, crown: dict[str, object] | None = None
    ) -> None:
        self.footbreak.write_text(
            json.dumps(footbreak or report("footbreak")), encoding="utf-8"
        )
        self.crown.write_text(json.dumps(crown or report("crown")), encoding="utf-8")

    def argv(self, *extra: str) -> list[str]:
        return [
            "--footbreak-report",
            str(self.footbreak),
            "--crown-report",
            str(self.crown),
            *extra,
        ]

    def test_all_normal_is_silent_and_only_logs_locally(self) -> None:
        sender = Mock()
        with patch("builtins.print") as local_log:
            result = health_alert.main(
                self.argv(), sender=sender, now=NOW, environ={}
            )
        self.assertEqual(result, 0)
        sender.assert_not_called()
        local_log.assert_called_once()
        self.assertIn("Telegram not sent", local_log.call_args.args[0])

    def test_duplicate_stage_keys_alerts_with_both_system_metrics(self) -> None:
        self.write_reports(footbreak=report("footbreak", duplicate_stage_keys=2))
        evaluation = health_alert.evaluate_reports(
            {"footbreak": self.footbreak, "crown": self.crown}
        )
        self.assertTrue(evaluation.abnormal)
        message = health_alert.format_message(evaluation, now=NOW)
        self.assertIn("足破：重複=2", message)
        self.assertIn("皇冠：重複=0", message)
        self.assertIn("重複階段鍵", message)

    def test_missing_or_incomplete_corner_coverage_alerts(self) -> None:
        for coverage, expected in ((None, "角球覆蓋缺失"), (0.999, "角球覆蓋不足")):
            with self.subTest(coverage=coverage):
                self.write_reports(
                    crown=report("crown", coverage=coverage),
                )
                evaluation = health_alert.evaluate_reports(
                    {"footbreak": self.footbreak, "crown": self.crown}
                )
                self.assertTrue(evaluation.abnormal)
                self.assertIn(expected, health_alert.format_message(evaluation, now=NOW))

    def test_any_unresolved_result_alerts_even_if_not_stale(self) -> None:
        self.write_reports(
            crown=report(
                "crown",
                settle_due_fixtures=10,
                fixtures_with_result=9,
                stale_unresolved_fixtures=0,
            )
        )
        evaluation = health_alert.evaluate_reports(
            {"footbreak": self.footbreak, "crown": self.crown}
        )
        self.assertTrue(evaluation.abnormal)
        message = health_alert.format_message(evaluation, now=NOW)
        self.assertIn("未解賽果", message)
        self.assertIn("未解總數=1", message)
        self.assertIn("逾期未解=0", message)

    def test_stale_unresolved_or_missing_corner_alerts(self) -> None:
        self.write_reports(
            crown=report(
                "crown",
                settle_due_fixtures=3,
                fixtures_with_result=0,
                stale_unresolved_fixtures=3,
                stale_beyond_retry_fixtures=4,
            )
        )
        evaluation = health_alert.evaluate_reports(
            {"footbreak": self.footbreak, "crown": self.crown}
        )
        message = health_alert.format_message(evaluation, now=NOW)
        self.assertIn("逾期未解賽果", message)
        self.assertIn("逾期缺角球", message)
        self.assertIn("未解總數=3", message)
        self.assertIn("逾期未解=3", message)
        self.assertIn("逾期缺角=4", message)

    def test_missing_or_malformed_report_alerts(self) -> None:
        self.footbreak.unlink()
        malformed = health_alert.evaluate_reports(
            {"footbreak": self.footbreak, "crown": self.crown}
        )
        self.assertTrue(malformed.abnormal)
        message = health_alert.format_message(malformed, now=NOW)
        self.assertIn("足破：重複=N/A", message)
        self.assertIn("報告缺失", message)

        self.footbreak.write_text("{bad json", encoding="utf-8")
        malformed = health_alert.evaluate_reports(
            {"footbreak": self.footbreak, "crown": self.crown}
        )
        self.assertTrue(malformed.abnormal)
        self.assertIn("報告讀取/格式錯誤", health_alert.format_message(malformed, now=NOW))

        self.write_reports(
            crown=report(
                "crown",
                settle_due_fixtures=1,
                fixtures_with_result=2,
            )
        )
        malformed = health_alert.evaluate_reports(
            {"footbreak": self.footbreak, "crown": self.crown}
        )
        self.assertTrue(malformed.abnormal)
        self.assertIn(
            "fixtures_with_result大過result.settle_due_fixtures",
            health_alert.format_message(malformed, now=NOW),
        )

    def test_generation_failure_alerts_even_when_artifacts_look_normal(self) -> None:
        evaluation = health_alert.evaluate_reports(
            {"footbreak": self.footbreak, "crown": self.crown},
            generation_failed=True,
        )
        self.assertTrue(evaluation.abnormal)
        self.assertIn("資料健康重生失敗", health_alert.format_message(evaluation, now=NOW))

    def test_send_failure_causes_nonzero_result_and_never_falls_back(self) -> None:
        self.write_reports(crown=report("crown", duplicate_stage_keys=1))
        sender = Mock(side_effect=RuntimeError("network down"))
        result = health_alert.main(
            self.argv(),
            sender=sender,
            now=NOW,
            environ={"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "-100"},
        )
        self.assertEqual(result, 1)
        sender.assert_called_once()

    def test_missing_credentials_fail_clearly_when_alert_is_needed(self) -> None:
        self.write_reports(crown=report("crown", duplicate_stage_keys=1))
        self.assertEqual(
            health_alert.main(self.argv(), now=NOW, environ={}),
            1,
        )

    def test_env_file_reads_only_literal_telegram_assignments(self) -> None:
        env_file = self.directory / "footbreak.env"
        env_file.write_text(
            "OTHER=$(must-not-run)\n"
            "export TELEGRAM_BOT_TOKEN='bot token'\n"
            "TELEGRAM_CHAT_ID=\"-100123\"\n",
            encoding="utf-8",
        )
        env = health_alert.load_telegram_environment(env_file, {"OTHER": "base"})
        self.assertEqual(env["TELEGRAM_BOT_TOKEN"], "bot token")
        self.assertEqual(env["TELEGRAM_CHAT_ID"], "-100123")
        self.assertEqual(env["OTHER"], "base")


if __name__ == "__main__":
    unittest.main()
