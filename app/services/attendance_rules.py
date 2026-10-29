"""What a day of attendance means.

Pure functions, no database. This is the single source of truth for the status
and colour of every calendar cell, which is why it is isolated here: every rule
below is directly unit-testable without a running server.

Precedence, highest first:

-1. Before the organisation's tracking start (the go-live "reset") a day is
   ``NOT_TRACKED`` whatever was punched on it — test punches from before launch
   must not count. The start day itself is a grace day: anybody who punched is
   present, with no late, short-hours or half-day verdict, and nobody who did
   not is absent. Ordinary rules apply from the next day.
0. A day before the employee joined, with nothing punched on it, is
   ``BEFORE_JOINING``. Without this a mid-month joiner read as absent for every
   earlier working day, and the salary projection docked them for each one.
1. A holiday is a holiday even if the employee worked; the hours are still
   recorded and shown.
2. Approved full-day leave is ``ON_LEAVE``. Half-day leave halves what the day
   requires rather than excusing it.
3. A non-working weekday is a weekly off.
4. A future date with none of the above is ``UPCOMING`` — nothing is expected yet.
5. Otherwise the day is judged on hours worked against the shift.

Holidays, leave and weekly offs are decided *before* the future check on purpose.
An employee looking at next month wants to see the leave they have booked and the
holidays they can plan around, not a wall of blank upcoming days.

Break deduction is applied only when the whole day was one continuous session.
An employee who punched out for lunch has already lost that time; deducting the
break again would charge them for it twice.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from app.core.datetime import to_org_tz, to_utc
from app.models.attendance import AttendanceStatus

FULL_DAY = "FULL"
FIRST_HALF = "FIRST_HALF"
SECOND_HALF = "SECOND_HALF"

DEFAULT_REQUIRED_MINUTES = 540
DEFAULT_HALF_DAY_MINUTES = 240
DEFAULT_WORKING_WEEKDAYS = (0, 1, 2, 3, 4)


@dataclass(frozen=True, slots=True)
class DayComputation:
    status: AttendanceStatus
    worked_minutes: int
    expected_minutes: int
    is_late: bool
    has_open_session: bool
    first_punch_in: datetime | None
    last_punch_out: datetime | None

    @property
    def shortfall_minutes(self) -> int:
        return max(0, self.expected_minutes - self.worked_minutes)


def _shift_value(shift: dict[str, Any] | None, key: str, fallback: Any) -> Any:
    if not shift:
        return fallback
    value = shift.get(key)
    return fallback if value is None else value


def _event_time(event: dict[str, Any] | None) -> datetime | None:
    if not event:
        return None
    at = event.get("at")
    return to_utc(at) if isinstance(at, datetime) else None


def summarise_sessions(
    sessions: list[dict[str, Any]],
    *,
    now: datetime,
    count_open_until: datetime | None = None,
) -> tuple[int, bool, datetime | None, datetime | None]:
    """``(raw_minutes, has_open_session, first_in, last_out)``.

    ``count_open_until`` decides what an unfinished session is worth. On today it
    is ``now``, so the dashboard counts up live. On a past day it is ``None`` and
    the session contributes nothing: the employee forgot to punch out, and
    nobody knows when they actually left — inventing a number would turn one
    missed punch into hundreds of recorded hours.

    Negative or reversed intervals contribute nothing rather than subtracting.
    """
    total_seconds = 0.0
    has_open = False
    first_in: datetime | None = None
    last_out: datetime | None = None

    for session in sessions or []:
        started = _event_time(session.get("punch_in"))
        if started is None:
            continue
        if first_in is None or started < first_in:
            first_in = started

        ended = _event_time(session.get("punch_out"))
        if ended is None:
            has_open = True
            if count_open_until is None:
                continue
            ended = count_open_until
        elif last_out is None or ended > last_out:
            last_out = ended

        total_seconds += max(0.0, (ended - started).total_seconds())

    return int(total_seconds // 60), has_open, first_in, last_out


def apply_break_deduction(
    raw_minutes: int, session_count: int, break_minutes: int
) -> int:
    """Deduct the unpaid break only from an unbroken day."""
    if session_count != 1 or break_minutes <= 0:
        return raw_minutes
    return max(0, raw_minutes - break_minutes)


def is_late_arrival(
    first_punch_in: datetime | None, shift: dict[str, Any] | None
) -> bool:
    """Later than the shift start plus its grace period, in local time."""
    if first_punch_in is None or not shift:
        return False
    start = str(_shift_value(shift, "start_time", "")).strip()
    if not start or ":" not in start:
        return False
    try:
        hour, minute = (int(part) for part in start.split(":", 1))
        shift_start = time(hour=hour, minute=minute)
    except (TypeError, ValueError):
        return False

    grace = int(_shift_value(shift, "grace_minutes", 0))
    local = to_org_tz(first_punch_in)
    minutes_in = local.hour * 60 + local.minute
    allowed = shift_start.hour * 60 + shift_start.minute + grace
    return minutes_in > allowed


def expected_minutes_for(
    shift: dict[str, Any] | None, leave_day_part: str | None
) -> int:
    """What the day asks for, halved when half a day is on approved leave."""
    required = int(_shift_value(shift, "required_minutes", DEFAULT_REQUIRED_MINUTES))
    if leave_day_part in (FIRST_HALF, SECOND_HALF):
        half = int(_shift_value(shift, "half_day_minutes", DEFAULT_HALF_DAY_MINUTES))
        return min(half, required)
    return required


def is_working_weekday(day: date, shift: dict[str, Any] | None) -> bool:
    weekdays = _shift_value(shift, "working_weekdays", list(DEFAULT_WORKING_WEEKDAYS))
    return day.weekday() in set(weekdays or DEFAULT_WORKING_WEEKDAYS)


def employment_start(user: dict[str, Any] | None) -> date | None:
    """The first day this person could have been expected at work.

    The joining date when an administrator recorded one; otherwise the day the
    account was activated, which is the day the invitation was accepted. An
    account with neither — a seeded administrator — has no lower bound.
    """
    if not user:
        return None
    joining = user.get("joining_date")
    if isinstance(joining, datetime):
        return joining.date()
    if isinstance(joining, date):
        return joining
    if isinstance(joining, str) and joining:
        try:
            return date.fromisoformat(joining[:10])
        except ValueError:
            pass
    activated = user.get("activated_at")
    if isinstance(activated, datetime):
        return to_org_tz(to_utc(activated)).date()
    return None


def compute_day_status(
    day: date,
    sessions: list[dict[str, Any]],
    shift: dict[str, Any] | None,
    *,
    today: date,
    now: datetime,
    holiday: dict[str, Any] | None = None,
    leave: dict[str, Any] | None = None,
    working_day: bool | None = None,
    employed_from: date | None = None,
    tracking_from: date | None = None,
    full_day: bool = False,
) -> DayComputation:
    """Decide what a single day means. See the module docstring for precedence.

    ``working_day`` is the work calendar's ruling for the date — a swapped
    Saturday, or a day an administrator opened or closed by hand. It overrides
    the shift's weekday pattern, which knows nothing about either. Left as
    ``None``, the shift decides, which is what the pure unit tests exercise.

    ``employed_from`` is :func:`employment_start` for the person. A day before
    it with punches on it is still judged: somebody evidently worked it, and a
    joining date typed a week late must not erase that.

    ``full_day`` is an administrator's ruling that the date counts as a full day
    for everybody who came in — the Saturday the office opened for three hours
    on purpose. Anybody who punched is present with no late mark and no
    shortfall, however long they stayed; the punches themselves are kept as
    recorded. Leave, holidays and absence are judged as on any other day.
    """
    raw_minutes, has_open, first_in, last_out = summarise_sessions(
        sessions,
        now=now,
        # Only a day still in progress accrues time from an open session.
        count_open_until=now if day >= today else None,
    )
    session_count = len([s for s in (sessions or []) if s.get("punch_in")])
    break_minutes = int(_shift_value(shift, "break_minutes", 0))
    worked = apply_break_deduction(raw_minutes, session_count, break_minutes)

    leave_day_part = (leave or {}).get("day_part", FULL_DAY) if leave else None
    expected = expected_minutes_for(shift, leave_day_part)
    grace_day = tracking_from is not None and day == tracking_from
    # Nobody is late on the day the system went live: the shift had started
    # long before anybody could punch. Nor on a day the office chose to keep
    # short — the hours asked for were not the shift's.
    late = False if grace_day or full_day else is_late_arrival(first_in, shift)

    def result(
        status: AttendanceStatus, *, expected_minutes: int | None = None
    ) -> DayComputation:
        return DayComputation(
            status=status,
            worked_minutes=worked,
            expected_minutes=expected if expected_minutes is None else expected_minutes,
            is_late=late,
            has_open_session=has_open,
            first_punch_in=first_in,
            last_punch_out=last_out,
        )

    # -1. Before go-live nothing counts, not even punches made while testing —
    #     their hours included, or the month's total would carry them.
    if tracking_from is not None and day < tracking_from:
        return DayComputation(
            status=AttendanceStatus.NOT_TRACKED,
            worked_minutes=0,
            expected_minutes=0,
            is_late=False,
            has_open_session=False,
            first_punch_in=first_in,
            last_punch_out=last_out,
        )

    # 0. Nothing was expected of somebody who had not joined yet.
    if employed_from is not None and day < employed_from and session_count == 0:
        return result(AttendanceStatus.BEFORE_JOINING)

    # 1. A holiday stays a holiday even if someone chose to work — unless the
    #    work calendar has explicitly reopened the day, which is the office
    #    saying "we are working this one after all". Then everybody is expected
    #    in and the day is judged like any other.
    #
    #    Calling it a holiday regardless was a pay bug, not just a wrong label:
    #    the projection counts a reopened day as a working day, and a working
    #    day stamped HOLIDAY falls through every bucket to "absent", so the
    #    whole office was docked for a day it was told to come in and work.
    if holiday and working_day is not True:
        return result(AttendanceStatus.HOLIDAY)

    # 2. Full-day leave excuses the day outright — including leave booked ahead,
    #    which the employee needs to see on next month's calendar.
    if leave and leave_day_part == FULL_DAY:
        return result(AttendanceStatus.ON_LEAVE)

    # 3. A day nobody is expected to work: the calendar's ruling when there is
    #    one, the shift's weekday pattern otherwise.
    if not (is_working_weekday(day, shift) if working_day is None else working_day):
        return result(AttendanceStatus.WEEKEND)

    # 4. Nothing else is expected of a day that has not happened.
    if day > today:
        return result(AttendanceStatus.UPCOMING)

    # The go-live day is judged generously: there is no fair shift to measure.
    if grace_day:
        if has_open and day == today:
            return result(AttendanceStatus.IN_PROGRESS)
        if session_count:
            return result(AttendanceStatus.PRESENT)
        if day == today:
            return result(AttendanceStatus.NOT_STARTED)
        return result(AttendanceStatus.NOT_TRACKED)

    # A day ruled a full day: being there is what counts, not for how long.
    # Asked for no more than was worked, so the month shows no shortfall for it.
    if full_day and session_count:
        if has_open and day == today:
            return result(AttendanceStatus.IN_PROGRESS)
        return result(AttendanceStatus.PRESENT, expected_minutes=min(expected, worked))

    # 5. An unfinished session: still working today, forgotten if it is not.
    if has_open:
        if day == today:
            return result(AttendanceStatus.IN_PROGRESS)
        return result(AttendanceStatus.MISSING_PUNCH_OUT)

    # 6. Nothing recorded at all.
    if session_count == 0:
        if day == today:
            return result(AttendanceStatus.NOT_STARTED)
        return result(AttendanceStatus.ABSENT)

    # 7. Judge the hours.
    half_day = int(_shift_value(shift, "half_day_minutes", DEFAULT_HALF_DAY_MINUTES))
    if worked >= expected:
        return result(AttendanceStatus.PRESENT)
    if worked >= half_day:
        return result(AttendanceStatus.HALF_DAY)
    return result(AttendanceStatus.SHORT_HOURS)


def format_minutes(minutes: int) -> str:
    """``545`` becomes ``9h 5m`` — what the dashboard shows."""
    minutes = max(0, int(minutes))
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}h {remainder}m"
    if hours:
        return f"{hours}h"
    return f"{remainder}m"
