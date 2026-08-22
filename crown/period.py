"""Crown board period: 12:00 HKT through 11:59:59 HKT next day."""
from __future__ import annotations

from datetime import datetime, time, timedelta

from .common import HKT


PERIOD_START = time(12, 0)
PERIOD_DURATION = timedelta(days=1)


def period_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = (now or datetime.now(HKT)).astimezone(HKT)
    start_date = current.date() if current.time() >= PERIOD_START else current.date() - timedelta(days=1)
    start = datetime.combine(start_date, PERIOD_START, tzinfo=HKT)
    return start, start + PERIOD_DURATION - timedelta(seconds=1)


def in_current_period(kickoff: datetime, now: datetime | None = None) -> bool:
    start, end = period_bounds(now)
    value = kickoff.astimezone(HKT)
    return start <= value <= end


def is_upcoming_in_current_period(kickoff: datetime, now: datetime | None = None) -> bool:
    """Only pre-match fixtures belong on the live Crown work board."""
    current = (now or datetime.now(HKT)).astimezone(HKT)
    return kickoff.astimezone(HKT) > current and in_current_period(kickoff, current)


def native_discovery_horizon(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the native Crown discovery window independent of dashboard dates.

    Every scheduled board pass starts at the next whole HKT hour.  Until noon,
    this deliberately admits the upcoming 12:00 board even though the current
    display period still ends at 11:59:59.  The end remains the following
    11:59:59 HKT, so fixtures receive a first look before their T-30/T-5 jobs
    can become due.
    """
    current = (now or datetime.now(HKT)).astimezone(HKT)
    start = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    noon = datetime.combine(current.date(), PERIOD_START, tzinfo=HKT)
    period_start = noon if current >= noon else noon
    end = period_start + PERIOD_DURATION - timedelta(seconds=1)
    return start, end


def in_native_discovery_horizon(kickoff: datetime, now: datetime | None = None) -> bool:
    """Whether an authoritative native fixture belongs in this board refresh."""
    start, end = native_discovery_horizon(now)
    value = kickoff.astimezone(HKT)
    return start <= value <= end
