"""決定邊個 stage 而家應該 fire。

三個 stage：
- 首預：fixture 出現就 fire 一次
- T-30：kickoff 前 30 分鐘至 kickoff（10 分鐘窗口）
- T-5： kickoff 前 5 分鐘至 kickoff（5 分鐘窗口）

補跑窗口容許 tick miss 兩三次（例如服務重啟）仍能追跑。
開賽後永不 fire。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from .fixtures import Fixture

STAGE_FIRST = "首預"
STAGE_T30 = "T-30"
STAGE_T5 = "T-5"

ALL_STAGES = (STAGE_FIRST, STAGE_T30, STAGE_T5)

# 每 stage 觸發設定：kickoff 前幾多秒開始，可補跑窗口幾多秒。
# 首預唔用 seconds_before，只要 fixture 出現就 fire。
_TIMED_STAGES: dict[str, tuple[int, int]] = {
    # stage: (seconds_before_kickoff, catchup_window_seconds)
    STAGE_T30: (30 * 60, 25 * 60),  # 30 分前 fire，容許遲 25 分鐘補跑（唔會超過開賽）
    STAGE_T5:  (5 * 60, 5 * 60),    # 5 分前 fire，容許遲到 kickoff 前
}


def due_stages(
    fixture: Fixture,
    now_utc: datetime,
    done: set[str],
) -> list[str]:
    """返回 fixture 而家該 fire 邊啲 stage。

    Args:
        fixture: fixture 記錄
        now_utc: 現在 UTC 時間
        done: 已 fire 過嘅 stage 名集合
    """
    due: list[str] = []

    # 開賽後禁止 fire 任何 stage
    if now_utc >= fixture.kickoff_utc:
        return due

    # 首預：只要出現就 fire，冇時間限制（但仍要開賽前）
    if STAGE_FIRST not in done:
        due.append(STAGE_FIRST)

    for stage, (seconds_before, catchup) in _TIMED_STAGES.items():
        if stage in done:
            continue
        fire_at = fixture.kickoff_utc - timedelta(seconds=seconds_before)
        window_end = min(
            fire_at + timedelta(seconds=catchup),
            fixture.kickoff_utc,
        )
        if fire_at <= now_utc <= window_end:
            due.append(stage)

    return due


def next_wake_utc(
    fixtures: Iterable[Fixture],
    now_utc: datetime,
    done_by_fixture: dict[str, set[str]] | None = None,
) -> datetime | None:
    """返回下一個應該 fire 嘅 UTC 時間（用嚟 systemd OnCalendar 提示，非必要）。

    可用嚟印 log 提示下場幾時要 tick，唔係 scheduler 主邏輯。
    """
    done_by_fixture = done_by_fixture or {}
    candidates: list[datetime] = []
    for fx in fixtures:
        done = done_by_fixture.get(fx.id, set())
        if fx.kickoff_utc <= now_utc:
            continue
        for stage, (seconds_before, _catchup) in _TIMED_STAGES.items():
            if stage in done:
                continue
            fire_at = fx.kickoff_utc - timedelta(seconds=seconds_before)
            if fire_at > now_utc:
                candidates.append(fire_at)
        if STAGE_FIRST not in done:
            candidates.append(now_utc)
    return min(candidates) if candidates else None


__all__ = [
    "STAGE_FIRST",
    "STAGE_T30",
    "STAGE_T5",
    "ALL_STAGES",
    "due_stages",
    "next_wake_utc",
]
