"""Crown board periods and the isolated daily future-round refresh window."""
from __future__ import annotations

from datetime import datetime, time, timedelta

from .common import HKT


# The live Crown board rolls exactly at 11:59 HKT.  It is deliberately not
# coupled to a browser clock or a systemd minute boundary.
PERIOD_START = time(11, 59)
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


def future_round_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the one daily native-update window anchored to today's 11:59 HKT.

    The dedicated 11:00 HKT update intentionally looks ahead to this boundary.
    If systemd's persistent invocation is late, the same day's window is still
    used rather than skipping it or selecting a second, overlapping board.
    """
    current = (now or datetime.now(HKT)).astimezone(HKT)
    start = datetime.combine(current.date(), PERIOD_START, tzinfo=HKT)
    return start, start + PERIOD_DURATION - timedelta(seconds=1)


def in_future_round_update_window(
    kickoff: datetime, now: datetime | None = None,
) -> bool:
    """Whether a native fixture belongs to the daily future-round refresh."""
    start, end = future_round_bounds(now)
    value = kickoff.astimezone(HKT)
    return start <= value <= end
