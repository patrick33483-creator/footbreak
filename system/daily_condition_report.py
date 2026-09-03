#!/usr/bin/env python3
"""Generate and deliver the daily Footbreak/Crown granular-condition report."""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import secrets
import shutil
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


HKT = timezone(timedelta(hours=8))
APP_DIR = Path("/opt/footbreak")
FOOTBREAK_LEDGER = APP_DIR / "system/sim_ledger.json"
CROWN_LEDGER = Path("/var/lib/footbreak/crown/ledger.json")
LEARNING_DB = Path("/var/lib/footbreak/learning/predictions.sqlite")
FOOTBREAK_LOCK = Path("/var/lock/footbreak.lock")
CROWN_LOCK = Path("/var/lib/footbreak/crown/.state.lock")
REPORT_DIR = Path("/var/lib/footbreak/daily-condition-reports")
STATE_PATH = REPORT_DIR / "send-state.json"
AUDIT_MODULE_PATH = APP_DIR / "deploy/audit-granular-condition-window.py"
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024


def daily_window(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(HKT)
    end = datetime.combine(local.date(), time(11, 59), tzinfo=HKT)
    if local < end:
        end -= timedelta(days=1)
    return end - timedelta(days=1), end


def locked_copy(lock_path: Path, source: Path, destination: Path) -> None:
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        shutil.copyfile(source, destination)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def load_audit_module(path: Path):
    spec = importlib.util.spec_from_file_location("granular_condition_window_audit", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load granular-condition audit module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def telegram_credentials() -> tuple[str, str]:
    if os.environ.get("FOOTBREAK_TELEGRAM_ENABLED", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        raise RuntimeError("Legacy Footbreak Telegram is disabled")
    token = str(
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("CROWN_TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    chat_id = str(
        os.environ.get("TELEGRAM_CHAT_ID")
        or os.environ.get("CROWN_TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    if not token or not chat_id:
        raise RuntimeError("Telegram bot token or chat ID is not configured")
    return token, chat_id


def telegram_call(token: str, method: str, body: bytes, content_type: str) -> None:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError(f"Telegram {method} was not acknowledged")


def send_message(token: str, chat_id: str, text: str) -> None:
    body = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    telegram_call(token, "sendMessage", body, "application/x-www-form-urlencoded")


def multipart_document(chat_id: str, path: Path, caption: str) -> tuple[bytes, str]:
    content = path.read_bytes()
    if len(content) > MAX_DOCUMENT_BYTES:
        raise RuntimeError("daily report exceeds Telegram document size limit")
    boundary = f"----footbreak-{secrets.token_hex(16)}"
    chunks: list[bytes] = []

    def field(name: str, value: str) -> None:
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"),
            b"\r\n",
        ])

    field("chat_id", chat_id)
    field("caption", caption)
    safe_name = path.name.replace('"', "")
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="document"; filename="{safe_name}"\r\n'.encode(),
        b"Content-Type: text/markdown; charset=utf-8\r\n\r\n",
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def send_document(token: str, chat_id: str, path: Path, caption: str) -> None:
    body, content_type = multipart_document(chat_id, path, caption)
    telegram_call(token, "sendDocument", body, content_type)


def summary_message(report: dict[str, Any]) -> str:
    summary = report["summary"]
    window = report["window"]
    systems = sorted({str(row["system"]) for row in report["entries"]})
    system_text = "、".join("足破" if item == "footbreak" else "皇冠" for item in systems) or "無"
    voided = sum(row.get("status") == "VOIDED" for row in report["entries"])
    return "\n".join([
        "每日細緻條件報告",
        f"時段：{window['start_inclusive'][:16]} 至 {window['end_exclusive'][:16]} HKT",
        f"系統：{system_text}",
        f"唯一場次：{summary['unique_fixtures']}｜條件記錄：{summary['entries']}",
        f"正式模擬注：{summary['formal_bets']}｜純觀察：{summary['observations']}",
        f"已結算：{summary['settled']}｜作廢：{voided}｜Pending：{summary['pending']}",
        (
            "Pending 分類："
            f"未到期 {summary['pending_not_due']}｜"
            f"逾期有候選 {summary['pending_overdue_with_candidate']}｜"
            f"逾期無候選 {summary['pending_overdue_without_candidate']}"
        ),
        "完整逐條件及逐場明細見附加檔案。",
        "由 DigitalOcean 伺服器產生，沒有使用 Perplexity 點數。",
    ])


def run(
    now: datetime,
    *,
    report_dir: Path = REPORT_DIR,
    state_path: Path = STATE_PATH,
    footbreak_ledger: Path = FOOTBREAK_LEDGER,
    crown_ledger: Path = CROWN_LEDGER,
    learning_db: Path = LEARNING_DB,
    footbreak_lock: Path = FOOTBREAK_LOCK,
    crown_lock: Path = CROWN_LOCK,
    audit_module_path: Path = AUDIT_MODULE_PATH,
    message_sender: Callable[[str, str, str], None] = send_message,
    document_sender: Callable[[str, str, Path, str], None] = send_document,
) -> dict[str, Any]:
    report_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(report_dir, 0o700)
    start, end = daily_window(now)
    date_key = end.strftime("%Y%m%d")
    json_path = report_dir / f"granular-condition-report-{date_key}.json"
    markdown_path = report_dir / f"granular-condition-report-{date_key}.md"
    audit_module = load_audit_module(audit_module_path)

    with tempfile.TemporaryDirectory(prefix="daily-condition-report-") as temporary:
        snapshot_dir = Path(temporary)
        footbreak_snapshot = snapshot_dir / "footbreak-ledger.json"
        crown_snapshot = snapshot_dir / "crown-ledger.json"
        locked_copy(footbreak_lock, footbreak_ledger, footbreak_snapshot)
        locked_copy(crown_lock, crown_ledger, crown_snapshot)
        report = audit_module.audit(
            {
                "footbreak": audit_module.load(footbreak_snapshot),
                "crown": audit_module.load(crown_snapshot),
            },
            audit_module.latest_learning_results(learning_db),
            start,
            end,
            now.astimezone(HKT),
        )
        markdown = audit_module.render_markdown(report)

    atomic_json(json_path, report)
    atomic_text(markdown_path, markdown)
    token, chat_id = telegram_credentials()
    window_key = report["window"]["end_exclusive"]
    state = load_state(state_path)
    if state.get("window_end") != window_key:
        state = {"window_end": window_key, "message_sent": False, "document_sent": False}
        atomic_json(state_path, state)
    if not state.get("message_sent"):
        message_sender(token, chat_id, summary_message(report))
        state["message_sent"] = True
        atomic_json(state_path, state)
    if not state.get("document_sent"):
        document_sender(
            token,
            chat_id,
            markdown_path,
            f"完整細緻條件報告：{start:%Y-%m-%d} 至 {end:%Y-%m-%d}",
        )
        state["document_sent"] = True
        atomic_json(state_path, state)
    return {
        "window_end": window_key,
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
        "message_sent": bool(state["message_sent"]),
        "document_sent": bool(state["document_sent"]),
        "summary": report["summary"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", help="ISO timestamp override for reviewed tests")
    args = parser.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(HKT)
    result = run(now)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
