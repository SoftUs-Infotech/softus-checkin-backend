"""The day-status rules — every branch, no database.

Times are given in the organisation timezone (Asia/Kolkata, UTC+05:30) and
converted, because that is how they arrive in real life.
"""

from datetime import UTC, date, datetime, timedelta

from app.models.attendance import AttendanceStatus
from app.services.attendance_rules import (
    compute_day_status,
    is_late_arrival,
)

SHIFT = {
    "start_time": "09:30",
    "end_time": "18:30",
    "required_minutes": 540,
    "half_day_minutes": 240,
    "grace_minutes": 15,
    "break_minutes": 0,
    "working_weekdays": [0, 1, 2, 3, 4],
}

MONDAY = date(2026, 3, 2)
SATURDAY = date(2026, 3, 7)
TODAY = date(2026, 3, 10)  # a Tuesday
NOW = datetime(2026, 3, 10, 8, 0, tzinfo=UTC)  # 13:30 local


def local(day: date, hour: int, minute: int = 0) -> datetime:
    """A wall-clock time in Asia/Kolkata expressed as UTC."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC) - timedelta(
        hours=5, minutes=30
    )


def session(day: date, start: tuple[int, int], end: tuple[int, int] | None):
    entry = {"punch_in": {"at": local(day, *start)}}
    entry["punch_out"] = {"at": local(day, *end)} if end else None
    return entry


def compute(day, sessions, shift=SHIFT, **kwargs):
    return compute_day_status(
        day, sessions, shift, today=kwargs.pop("today", TODAY), now=kwargs.pop("now", NOW), **kwargs
    )


# --------------------------------------------------------------------------- #
# lateness
# --------------------------------------------------------------------------- #


def test_arriving_after_the_grace_period_is_late():
    assert is_late_arrival(local(MONDAY, 9, 46), SHIFT) is True


# --------------------------------------------------------------------------- #
# statuses
# --------------------------------------------------------------------------- #
def test_a_full_day_is_present():
    result = compute(MONDAY, [session(MONDAY, (9, 30), (18, 30))])

    assert result.status is AttendanceStatus.PRESENT
    assert result.worked_minutes == 540
    assert result.shortfall_minutes == 0


def test_four_hours_is_a_half_day():
    result = compute(MONDAY, [session(MONDAY, (9, 30), (13, 30))])

    assert result.status is AttendanceStatus.HALF_DAY
    assert result.shortfall_minutes == 300


# --------------------------------------------------------------------------- #
# precedence
# --------------------------------------------------------------------------- #


def test_a_holiday_outranks_leave():
    result = compute(MONDAY, [], holiday={"name": "Holi"}, leave={"day_part": "FULL"})

    assert result.status is AttendanceStatus.HOLIDAY
