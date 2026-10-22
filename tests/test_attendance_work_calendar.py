"""The work calendar has to reach the attendance calendar.

The two were built separately on purpose — one decides what a date means, the
other what an employee's day means — so the seam between them is exactly where
a regression would hide.
"""

from datetime import date

import pytest

from app.models.attendance import AttendanceStatus
from app.models.work_calendar import DayKind
from app.schemas.work_calendar import OverrideCreate
from app.services import attendance as attendance_service
from app.services import work_calendar as work_calendar_service
from app.services.audit import Actor

pytestmark = pytest.mark.asyncio

# March 2026: the 7th is the first Saturday (off by default), the 14th the
# second (working under the default 2nd-and-4th pattern).
FIRST_SATURDAY = date(2026, 3, 7)
SECOND_SATURDAY = date(2026, 3, 14)


def day_named(calendar, key: str):
    return next(entry for entry in calendar.days if entry.date == key)


async def test_the_saturday_pattern_reaches_the_month_calendar(db, admin):
    calendar = await attendance_service.month_calendar(admin, 2026, 3)

    assert day_named(calendar, "2026-03-07").status is AttendanceStatus.WEEKEND
    assert day_named(calendar, "2026-03-14").status is not AttendanceStatus.WEEKEND


async def test_opening_a_saturday_by_hand_reaches_the_month_calendar(db, admin):
    await work_calendar_service.set_override(
        OverrideCreate(date=FIRST_SATURDAY, kind=DayKind.WORKING, reason="Quarter close"),
        Actor(id=admin["id"], email=admin["email"]),
    )

    calendar = await attendance_service.month_calendar(admin, 2026, 3)

    # Nobody worked it and it is in the past, so an opened day is an absence —
    # what matters is that it stopped being a weekly off.
    assert day_named(calendar, "2026-03-07").status is not AttendanceStatus.WEEKEND


async def test_closing_a_working_day_by_hand_reaches_the_month_calendar(db, admin):
    await work_calendar_service.set_override(
        OverrideCreate(date=SECOND_SATURDAY, kind=DayKind.OFF, reason="Given back"),
        Actor(id=admin["id"], email=admin["email"]),
    )

    calendar = await attendance_service.month_calendar(admin, 2026, 3)

    assert day_named(calendar, "2026-03-14").status is AttendanceStatus.WEEKEND
