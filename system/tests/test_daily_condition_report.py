from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import daily_condition_report as report


class DailyConditionReportTests(unittest.TestCase):
    def test_daily_window_uses_previous_1159_to_current_1159(self) -> None:
        start, end = report.daily_window(datetime.fromisoformat("2026-08-27T12:15:00+08:00"))
        self.assertEqual(start.isoformat(), "2026-08-26T11:59:00+08:00")
        self.assertEqual(end.isoformat(), "2026-08-27T11:59:00+08:00")

    def test_before_1159_uses_the_previous_completed_window(self) -> None:
        start, end = report.daily_window(datetime.fromisoformat("2026-08-27T08:00:00+08:00"))
        self.assertEqual(start.isoformat(), "2026-08-25T11:59:00+08:00")
        self.assertEqual(end.isoformat(), "2026-08-26T11:59:00+08:00")

    def test_summary_is_explicit_about_server_ownership_and_pending(self) -> None:
        payload = {
            "window": {
                "start_inclusive": "2026-08-26T11:59:00+08:00",
                "end_exclusive": "2026-08-27T11:59:00+08:00",
            },
            "summary": {
                "unique_fixtures": 11,
                "entries": 57,
                "formal_bets": 0,
                "observations": 57,
                "settled": 49,
                "pending": 2,
                "pending_not_due": 2,
                "pending_overdue_with_candidate": 0,
                "pending_overdue_without_candidate": 0,
            },
            "entries": [
                {"system": "footbreak", "status": "VOIDED"},
                {"system": "footbreak", "status": "PENDING"},
            ],
        }
        text = report.summary_message(payload)
        self.assertIn("唯一場次：11", text)
        self.assertIn("作廢：1｜Pending：2", text)
        self.assertIn("沒有使用 Perplexity 點數", text)

    def test_send_state_tracks_message_and_document_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state = {"window_end": "x", "message_sent": True, "document_sent": False}
            report.atomic_json(state_path, state)
            self.assertEqual(json.loads(state_path.read_text()), state)
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

    def test_multipart_contains_document_and_caption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            path.write_text("# 報告\n", encoding="utf-8")
            body, content_type = report.multipart_document("123", path, "完整報告")
        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertIn(b'name="document"', body)
        self.assertIn("完整報告".encode(), body)
        self.assertIn("# 報告".encode(), body)

    def test_missing_telegram_credentials_fails_closed(self) -> None:
        with patch.dict(report.os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                report.telegram_credentials()


if __name__ == "__main__":
    unittest.main()
