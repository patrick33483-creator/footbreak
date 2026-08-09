from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SYSTEM = Path(__file__).resolve().parents[1]
if str(SYSTEM) not in sys.path:
    sys.path.insert(0, str(SYSTEM))

import notify


class TelegramTransportTests(unittest.TestCase):
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

