from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import notify


class TelegramTransportTests(unittest.TestCase):
    def test_review_events_only_include_passed_human_review_gates(self) -> None:
        report = {
            "systems": {
                "footbreak": {
                    "status": "accumulating",
                    "automatic_upgrade_test": {
                        "status": "candidate_passed",
                        "recommended_candidate": "stage:T-5",
                        "eligible_matches": 300,
                    },
                    "market_model_upgrade_tests": {
                        "tests": {
                            "HDC": {
                                "status": "candidate_passed_human_review_required",
                                "eligible_matches": 100,
                                "challenger": {"calibration_alpha": 0.8},
                                "delta": {"brier": -0.02, "accuracy": 0.01},
                            },
                            "CHL": {
                                "status": "waiting_for_100_verified_matches",
                            },
                        }
                    },
                },
                "crown": {
                    "status": "accumulating",
                    "automatic_upgrade_test": {
                        "status": "waiting_for_300_matches",
                    },
                    "market_model_upgrade_tests": {"tests": {}},
                },
            }
        }
        events = notify.review_events(report)
        self.assertEqual(
            [event["key"] for event in events],
            ["upgrade:footbreak:stage:T-5", "market:footbreak:HDC:0.8"],
        )

    def test_review_notification_is_sent_once_per_candidate(self) -> None:
        report = {
            "generated_at": "2026-08-11T12:30:00+08:00",
            "systems": {
                "footbreak": {
                    "status": "accumulating",
                    "automatic_upgrade_test": {
                        "status": "candidate_passed",
                        "recommended_candidate": "stage:T-5",
                        "eligible_matches": 300,
                    },
                    "market_model_upgrade_tests": {"tests": {}},
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            state_path = Path(tmp) / "notify_state.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with patch.object(notify, "STATE", str(state_path)), patch.object(
                notify, "send"
            ) as sender:
                self.assertEqual(
                    notify.main(["--review", "--report", str(report_path)]), 0
                )
                self.assertEqual(
                    notify.main(["--review", "--report", str(report_path)]), 0
                )

            sender.assert_called_once()
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["reviews"], ["upgrade:footbreak:stage:T-5"])

    def test_feature_challenger_only_creates_review_event_after_gate_passes(self) -> None:
        report = {
            "systems": {
                "crown": {
                    "model_challenger_tests": {
                        "tests": {
                            "HIL": {
                                "status": "candidate_passed_human_review_required",
                                "model_version": "challenger-logit-v1",
                                "model_version_hash": "abc123",
                                "holdout_fixtures": 30,
                                "holdout_rows": 60,
                                "delta": {"brier": -.02, "log_loss": -.01, "accuracy": .01},
                            },
                            "CHL": {"status": "tested_no_safe_upgrade"},
                        },
                    },
                },
            },
        }
        self.assertEqual(
            [event["key"] for event in notify.review_events(report)],
            ["challenger:crown:HIL:abc123"],
        )

    def test_long_lived_bot_token_uses_direct_telegram_api(self) -> None:
        response = Mock()
        response.read.return_value = json.dumps({"ok": True}).encode()
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)

        with (
            patch.object(notify, "CHAT_ID", "-123456"),
            patch.object(notify, "BOT_TOKEN", "token-value"),
            patch.object(notify.urllib.request, "urlopen", return_value=context) as urlopen,
            patch.object(notify.subprocess, "run") as external_tool,
        ):
            self.assertEqual(notify.send("<b>下注</b>"), "telegram_bot_api_ok")

        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/sendMessage"))
        payload = json.loads(request.data.decode())
        self.assertEqual(payload["chat_id"], "-123456")
        self.assertEqual(payload["parse_mode"], "HTML")
        external_tool.assert_not_called()
