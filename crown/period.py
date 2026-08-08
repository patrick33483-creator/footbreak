"""Crown board period: 12:05 HKT through 11:59:59 HKT next day."""
from __future__ import annotations

from datetime import datetime, time, timedelta

from .common import HKT


PERIOD_START = time(12, 5)
PERIOD_DURATION = timedelta(hours=23, minutes=55)


def period_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = (now or datetime.now(HKT)).astimezone(HKT)
    start_date = current.date() if current.time() >= PERIOD_START else current.date() - timedelta(days=1)
    start = datetime.combine(start_date, PERIOD_START, tzinfo=HKT)
    return start, start + PERIOD_DURATION - timedelta(seconds=1)


def in_current_period(kickoff: datetime, now: datetime | None = None) -> bool:
    start, end = period_bounds(now)
    value = kickoff.astimezone(HKT)
    return start <= value <= end
