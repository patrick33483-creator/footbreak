"""Local-only Crown dashboard API.

Nginx provides authentication and proxies /api/* to this service.  The
service deliberately exposes simulation settlement only; no real-betting
operation exists.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import Settings, settings
from .dashboard_data import write_dashboard_data
from .engine import run
from .prediction_history import update_history
from .settle import settle_due
from .state import load_ledger, state_lock


MAX_BODY_BYTES = 4096


def read_published_data(config: Settings) -> dict[str, Any]:
    """Read the already-published snapshot without rebuilding Crown state."""
    payload = json.loads((config.web_root / "data.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("dashboard_payload_not_object")
    if payload.get("schema_version") != "crown-dashboard-v2":
        raise ValueError("dashboard_schema_invalid")
    return payload


def perform_settlement(config: Settings) -> dict[str, Any]:
    """Settle due simulation bets and publish the refreshed dashboard."""
    result = run("settle", config)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("reason") or "settlement_rejected"))

    ledger = load_ledger(config)
    # The button promises both ledger settlement and prediction-history
    # reconciliation.  Never return a false success when history grading
    # failed: the browser must show an error and the automatic timer retries.
    history = update_history(config, ledger)

    write_dashboard_data(config)
    data = read_published_data(config)
    response = {
        "ok": True,
        "settled_count": int(result.get("settled") or 0),
        "pending_count": int(result.get("pending") or 0),
        "persisted": True,
        "project_submitted": True,
        "data": data,
    }
    response["prediction_result_sync"] = history.get("result_sync") or {}
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

    def _published_json(self) -> None:
        """Stream the immutable published snapshot without rebuilding it.

        The Crown history can be large.  Parsing and serialising the complete
        document for every GET multiplied memory use and could exceed the
        dashboard health-check timeout.  The writer already replaces this
        trusted file atomically, so an opened descriptor is a consistent
        snapshot for the whole response.
        """
        path = self.config.web_root / "data.json"
        with path.open("rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
                raise ValueError("dashboard_payload_not_regular")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(metadata.st_size))
            self.end_headers()
            try:
                shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                # A client timeout must not produce a noisy traceback or
                # terminate the threaded API server.
                return

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            self._json(
                HTTPStatus.OK,
                {"ok": True, "service": "crown-dashboard-api"},
            )
            return
        if path != "/api/data":
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        try:
            self._published_json()
        except Exception as exc:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": f"data_{type(exc).__name__}"},
            )

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/settle":
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        action = self.headers.get("X-Crown-Action")
        if action != "settle-simulation":
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
        expected_payload = {"confirm": "simulation-only"}
        if payload != expected_payload:
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
