"""Local-only Crown dashboard API.

Nginx provides authentication and proxies /api/* to this service.  The
service deliberately exposes simulation settlement only; no real-betting
operation exists.
"""
from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import Settings, settings
from .dashboard_data import build, write_dashboard_data
from .engine import run
from .prediction_history import update_history
from .state import load_ledger


MAX_BODY_BYTES = 4096


def perform_settlement(config: Settings) -> dict[str, Any]:
    """Settle due simulation bets and publish the refreshed dashboard."""
    result = run("settle", config)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("reason") or "settlement_rejected"))

    ledger = load_ledger(config)
    warning = None
    try:
        update_history(config, ledger)
    except Exception as exc:
        # Prediction-history grading is separate from simulation-ledger
        # settlement.  Keep the settled ledger visible and report a warning.
        warning = f"prediction_history_{type(exc).__name__}"

    write_dashboard_data(config)
    data = build(config)
    response = {
        "ok": True,
        "settled_count": int(result.get("settled") or 0),
        "pending_count": int(result.get("pending") or 0),
        "persisted": True,
        "project_submitted": True,
        "data": data,
    }
    if warning:
        response["warning"] = warning
    return response


class CrownDashboardHandler(BaseHTTPRequestHandler):
    server_version = "CrownDashboardAPI/1"

    @property
    def config(self) -> Settings:
        return self.server.config  # type: ignore[attr-defined]

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
        self._json(HTTPStatus.OK, build(self.config))

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/settle":
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        if self.headers.get("X-Crown-Action") != "settle-simulation":
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
            self._json(HTTPStatus.OK, perform_settlement(self.config))
        except Exception as exc:
            # Return only a stable error class.  Upstream responses and
            # credentials must never reach the browser or logs.
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": f"settlement_{type(exc).__name__}"},
            )

    def log_message(self, _format: str, *_args: Any) -> None:
        # Nginx already keeps authenticated access/error logs.
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), CrownDashboardHandler)
    server.config = settings()  # type: ignore[attr-defined]
    server.serve_forever()


if __name__ == "__main__":
    main()
