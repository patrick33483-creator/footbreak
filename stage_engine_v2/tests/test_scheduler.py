from datetime import datetime, timedelta, timezone

import pytest

from stage_engine_v2.fixtures import Fixture, HKT
from stage_engine_v2.scheduler import (
    STAGE_FIRST, STAGE_T30, STAGE_T5, due_stages, next_wake_utc,
)

UTC = timezone.utc


def _fx(kickoff_hkt_str: str, fx_id: str = "1") -> Fixture:
    kickoff_hkt = datetime.strptime(kickoff_hkt_str, "%Y-%m-%d %H:%M").replace(tzinfo=HKT)
    return Fixture(
        id=fx_id, league="L", home="H", away="A",
        kickoff_utc=kickoff_hkt.astimezone(UTC),
        kickoff_hkt=kickoff_hkt,
        source="hkjc",
    )


def test_first_stage_fires_immediately_if_not_done():
    fx = _fx("2026-08-29 12:00")
    now = fx.kickoff_utc - timedelta(hours=5)
    assert STAGE_FIRST in due_stages(fx, now, set())


def test_first_stage_not_refired():
    fx = _fx("2026-08-29 12:00")
    now = fx.kickoff_utc - timedelta(hours=5)
    assert STAGE_FIRST not in due_stages(fx, now, {STAGE_FIRST})


def test_t30_window_starts_at_30min_before():
    fx = _fx("2026-08-29 12:00")
    # 剛好 30 分前
    now = fx.kickoff_utc - timedelta(minutes=30)
    assert STAGE_T30 in due_stages(fx, now, {STAGE_FIRST})
    # 31 分前 → 未到
    assert STAGE_T30 not in due_stages(
        fx, fx.kickoff_utc - timedelta(minutes=31), {STAGE_FIRST}
    )


def test_t30_catchup_extends_past_fire_time():
    fx = _fx("2026-08-29 12:00")
    # 20 分前（miss 咗 T-30 但仲在 25 min catchup 內）
    now = fx.kickoff_utc - timedelta(minutes=20)
    assert STAGE_T30 in due_stages(fx, now, {STAGE_FIRST})


def test_t5_window_starts_at_5min_before():
    fx = _fx("2026-08-29 12:00")
    now = fx.kickoff_utc - timedelta(minutes=5)
    assert STAGE_T5 in due_stages(fx, now, {STAGE_FIRST, STAGE_T30})


def test_no_fire_after_kickoff():
    fx = _fx("2026-08-29 12:00")
    now = fx.kickoff_utc + timedelta(seconds=1)
    assert due_stages(fx, now, set()) == []


def test_no_fire_exactly_at_kickoff():
    fx = _fx("2026-08-29 12:00")
    assert due_stages(fx, fx.kickoff_utc, set()) == []


def test_done_stages_never_refired():
    fx = _fx("2026-08-29 12:00")
    now = fx.kickoff_utc - timedelta(minutes=5)
    done = {STAGE_FIRST, STAGE_T30, STAGE_T5}
    assert due_stages(fx, now, done) == []


def test_t30_catchup_expires_before_t5_window():
    """T-30 catchup 25 分鐘，即 kickoff-5 min 後就唔再補跑，避免撞 T-5。"""
    fx = _fx("2026-08-29 12:00")
    now = fx.kickoff_utc - timedelta(minutes=3)
    result = due_stages(fx, now, set())
    assert STAGE_FIRST in result
    assert STAGE_T30 not in result  # T-30 catchup 已過
    assert STAGE_T5 in result


def test_first_plus_t30_when_t30_catchup_still_open():
    """fixture 首次見到，時間 T-10 min：首預及 T-30 都 fire，T-5 未到窗。"""
    fx = _fx("2026-08-29 12:00")
    now = fx.kickoff_utc - timedelta(minutes=10)
    result = due_stages(fx, now, set())
    assert STAGE_FIRST in result
    assert STAGE_T30 in result
    assert STAGE_T5 not in result


def test_next_wake_returns_earliest_upcoming():
    fx1 = _fx("2026-08-29 12:00", fx_id="a")
    fx2 = _fx("2026-08-29 13:00", fx_id="b")
    now = fx1.kickoff_utc - timedelta(hours=2)
    wake = next_wake_utc([fx1, fx2], now, done_by_fixture={
        "a": {STAGE_FIRST}, "b": {STAGE_FIRST},
    })
    # 應該係 fx1 嘅 T-30 = kickoff-30min
    assert wake == fx1.kickoff_utc - timedelta(minutes=30)
