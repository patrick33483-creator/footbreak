"""v2 ledger 讀寫。

- 自己一個 JSON file，唔碰舊 sim_ledger.json
- 原子寫入：先寫 tmp file，os.replace()
- file lock（fcntl）避免同時多個 tick 撞（不過 systemd tick 係 sequential，只係雙保險）
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .fixtures import Fixture

DEFAULT_LEDGER_PATH = Path("/var/lib/footbreak/stage_engine_v2/ledger.json")

SCHEMA_VERSION = 1


def _empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "fixtures": {},
    }


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """File lock 一次寫入。lock file 同 ledger 唔同 path 避免 replace 洗走 lock。"""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def load_ledger(path: Path | str = DEFAULT_LEDGER_PATH) -> dict[str, Any]:
    """讀 ledger。唔存在就返回 empty schema。"""
    p = Path(path)
    if not p.exists():
        return _empty_ledger()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_ledger()
    if not isinstance(data, dict):
        return _empty_ledger()
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("fixtures", {})
    if not isinstance(data["fixtures"], dict):
        data["fixtures"] = {}
    return data


def save_ledger(ledger: dict[str, Any], path: Path | str = DEFAULT_LEDGER_PATH) -> None:
    """原子寫入 ledger。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ledger["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    with _exclusive_lock(p):
        # 用 NamedTemporaryFile 保證 same-fs，避免 os.replace() cross-device
        fd, tmp_path = tempfile.mkstemp(
            dir=str(p.parent), prefix=".ledger-", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(ledger, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, p)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise


def _ensure_fixture_slot(ledger: dict[str, Any], fx: Fixture) -> dict[str, Any]:
    slot = ledger["fixtures"].setdefault(fx.id, {
        "id": fx.id,
        "league": fx.league,
        "home": fx.home,
        "away": fx.away,
        "kickoff_utc": fx.kickoff_utc.isoformat(),
        "kickoff_hkt": fx.kickoff_hkt.isoformat(),
        "source": fx.source,
        "stages": {},
        "first_seen_utc": datetime.now(timezone.utc).isoformat(),
    })
    # 若 fixture metadata 變過（例如舊系統補全咗隊名），更新元資料，
    # 但 stages 永不覆蓋
    slot["league"] = fx.league or slot.get("league", "")
    slot["home"] = fx.home or slot.get("home", "")
    slot["away"] = fx.away or slot.get("away", "")
    slot["kickoff_utc"] = fx.kickoff_utc.isoformat()
    slot["kickoff_hkt"] = fx.kickoff_hkt.isoformat()
    slot.setdefault("stages", {})
    return slot


def already_fired(ledger: dict[str, Any], fixture_id: str) -> set[str]:
    slot = ledger.get("fixtures", {}).get(fixture_id)
    if not isinstance(slot, dict):
        return set()
    stages = slot.get("stages")
    if not isinstance(stages, dict):
        return set()
    return {s for s, v in stages.items() if isinstance(v, dict)}


def record_stage(
    ledger: dict[str, Any],
    fx: Fixture,
    stage: str,
    prediction: dict[str, Any],
) -> None:
    """記錄一個 stage prediction。同 stage 已存在就 no-op（append-only）。"""
    slot = _ensure_fixture_slot(ledger, fx)
    stages = slot["stages"]
    if stage in stages and isinstance(stages[stage], dict):
        return  # 已 fire 過，唔覆蓋
    stages[stage] = {
        "stage": stage,
        "predicted_at_utc": datetime.now(timezone.utc).isoformat(),
        **prediction,
    }


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_LEDGER_PATH",
    "load_ledger",
    "save_ledger",
    "already_fired",
    "record_stage",
]
