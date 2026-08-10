"""Local-only Footbreak dashboard settlement API.

Nginx owns authentication and proxies /api/* to this service.  The only
write operation runs simulation settlement and republishes data.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


MAX_BODY_BYTES = 4096
RUNNER = os.environ.get("FOOTBREAK_RUNNER", "/opt/footbreak/deploy/run.sh")
DATA_PATH = os.environ.get("FOOTBREAK_DATA", "/var/www/footbreak/data.json")


def read_data() -> dict[str, Any]:
    return json.loads(Path(DATA_PATH).read_text(encoding="utf-8"))


def perform_settlement() -> dict[str, Any]:
    completed = subprocess.run(
        [RUNNER, "settle"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=300,
        check=False,
        env=os.environ.copy(),
    )
    if completed.returncode != 0:
        if completed.returncode == 75:
            raise BlockingIOError("settlement_busy")
        raise RuntimeError("settlement_failed")
    data = read_data()
    history = data.get("prediction_history") or {}
    return {
        "ok": True,
        "generated_at": data.get("generated_at"),
        "prediction_history_stats": history.get("stats") or {},
        "data": data,
    }


class FootbreakDashboardHandler(BaseHTTPRequestHandler):
    server_version = "FootbreakDashboardAPI/1"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/data":
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        try:
            self._json(HTTPStatus.OK, read_data())
        except Exception as exc:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": f"data_{type(exc).__name__}"},
            )

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/settle":
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        if self.headers.get("X-Footbreak-Action") != "settle-simulation":
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "action_not_allowed"})
            return
        try:
            size = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            size = -1
        if size < 0 or size > MAX_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "invalid_body"})
            return
        try:
            payload = json.loads(self.rfile.read(size) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
            return
        if payload != {"confirm": "simulation-only"}:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "confirmation_required"})
            return
        try:
            self._json(HTTPStatus.OK, perform_settlement())
        except BlockingIOError:
            self._json(HTTPStatus.CONFLICT, {"ok": False, "error": "settlement_busy"})
        except Exception as exc:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": f"settlement_{type(exc).__name__}"},
            )

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), FootbreakDashboardHandler).serve_forever()


if __name__ == "__main__":
    main()
