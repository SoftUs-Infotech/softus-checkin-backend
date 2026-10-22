"""A holiday is a closed day, not a note attached to an open one.

Holidays used to live in their own collection that only the attendance labeller
consulted. The work calendar still called the day working, so the month kept its
full working-day count and the per-day rate everybody's deductions are priced
from was computed as though nobody had a day off. Declaring three holidays has
to take three days out of the month, everywhere, at once.
"""

from datetime import UTC, date, datetime

import pytest

from app.core.datetime import date_key
from app.db.mongo import Collections
from app.models.attendance import AttendanceStatus
from app.models.settings import SETTINGS_KEY
from app.models.work_calendar import DayKind
from app.schemas.holiday import HolidayCreate
from app.schemas.salary import SalaryCreate
from app.schemas.work_calendar import OverrideCreate
from app.services import holiday as holiday_service
from app.services import salary as salary_service
from app.services import settings as settings_service
from app.services import work_calendar as calendar_service
from app.services.attendance_rules import compute_day_status
from app.services.audit import Actor

# Only the database-backed tests below the divider are async; the pure rules
# above it are plain functions, so the marker is applied per test class of work
# rather than to the whole module.

MONTH = "2026-08"
AFTER_THE_MONTH = date(2026, 9, 1)

# 2026-08-12 is a Wednesday, so it is working under every shift and no Saturday
# rule can be credited with closing it.
WEDNESDAY = date(2026, 8, 12)


# --------------------------------------------------------------------------- #
# the pure rule
# --------------------------------------------------------------------------- #
def test_a_holiday_closes_a_working_weekday():
    decision = calendar_service.decide_working_day(
        WEDNESDAY, holiday={"name": "Independence Day"}
    )

    assert decision.is_working is False
    assert decision.source == calendar_service.SOURCE_HOLIDAY
    assert decision.holiday_name == "Independence Day"


def test_a_holiday_names_itself_in_the_reason():
    decision = calendar_service.decide_working_day(WEDNESDAY, holiday={"name": "Diwali"})

    assert "Diwali" in decision.reason


def test_an_override_still_beats_a_holiday():
    """The one year the office decides to work a holiday, it can say so."""
    decision = calendar_service.decide_working_day(
        WEDNESDAY,
        holiday={"name": "Diwali"},
        override={"kind": DayKind.WORKING, "reason": "Client go-live."},
    )

    assert decision.is_working is True
    assert decision.source == calendar_service.SOURCE_OVERRIDE
    # Still reported, because the reader wants to know what was worked through.
    assert decision.holiday_name == "Diwali"


def test_a_holiday_beats_the_saturday_pattern():
    saturday = date(2026, 8, 8)  # the second Saturday

    decision = calendar_service.decide_working_day(
        saturday, working_saturdays=[2, 4], holiday={"name": "Company day"}
    )

    assert decision.is_working is False


def test_a_month_loses_a_working_day_for_each_holiday():
    first, last = date(2026, 8, 1), date(2026, 8, 31)
    plain = calendar_service.decide_range(first, last, working_saturdays=[])
    with_holidays = calendar_service.decide_range(
        first,
        last,
        working_saturdays=[],
        holidays={
            "2026-08-12": {"name": "One"},
            "2026-08-13": {"name": "Two"},
            "2026-08-14": {"name": "Three"},
        },
    )

    assert calendar_service.count_working_days(plain) - calendar_service.count_working_days(
        with_holidays
    ) == 3


def test_a_holiday_on_a_day_the_office_was_shut_anyway_costs_nothing():
    sunday = date(2026, 8, 9)
    first, last = date(2026, 8, 1), date(2026, 8, 31)

    plain = calendar_service.count_working_days(
        calendar_service.decide_range(first, last, working_saturdays=[])
    )
    with_holiday = calendar_service.count_working_days(
        calendar_service.decide_range(
            first, last, working_saturdays=[], holidays={date_key(sunday): {"name": "Sunday"}}
        )
    )

    assert plain == with_holiday


# --------------------------------------------------------------------------- #
# end to end: the calendar, then the money
# --------------------------------------------------------------------------- #
@pytest.fixture
async def closed_saturdays(db):
    await db[Collections.SETTINGS].update_one(
        {"key": SETTINGS_KEY},
        {"$set": {"working_saturdays": [], "fifth_saturday_working": None}},
        upsert=True,
    )
    settings_service.clear_cache()


@pytest.fixture
async def paid_admin(db, admin, closed_saturdays):
    await salary_service.set_salary(
        admin["id"],
        SalaryCreate(monthly_ctc=31000, effective_from=date(2026, 1, 1)),
        Actor(id=admin["id"], email=admin["email"]),
    )
    return admin


async def declare(admin, day: date, name: str) -> dict:
    return await holiday_service.create_holiday(
        HolidayCreate(date=day, name=name),
        Actor(id=admin["id"], email=admin["email"]),
    )


async def test_the_month_grid_greys_a_declared_holiday(db, admin, closed_saturdays):
    await declare(admin, WEDNESDAY, "Founders' day")

    decisions = await calendar_service.month_decisions(2026, 8)
    day = next(item for item in decisions if item.date == date_key(WEDNESDAY))

    assert day.is_working is False
    assert day.holiday_name == "Founders' day"


async def test_declaring_three_holidays_takes_three_working_days_off_the_month(
    db, admin, closed_saturdays
):
    before = await calendar_service.working_days_in_month(2026, 8)

    for offset, name in enumerate(("One", "Two", "Three")):
        await declare(admin, date(2026, 8, 12 + offset), name)

    assert await calendar_service.working_days_in_month(2026, 8) == before - 3


async def test_the_per_day_rate_follows_the_holidays(db, paid_admin):
    before = await salary_service.project_for_user(
        paid_admin["id"], MONTH, today=AFTER_THE_MONTH
    )

    await declare(paid_admin, WEDNESDAY, "Founders' day")

    after = await salary_service.project_for_user(
        paid_admin["id"], MONTH, today=AFTER_THE_MONTH
    )

    assert after["counts"]["working_days"] == before["counts"]["working_days"] - 1
    assert after["counts"]["holiday_days"] == before["counts"]["holiday_days"] + 1
    # Fewer working days over the same salary means each one is worth more.
    assert after["day_rate"] > before["day_rate"]


async def test_reopening_a_holiday_by_hand_puts_the_day_back(db, admin, closed_saturdays):
    await declare(admin, WEDNESDAY, "Founders' day")
    before = await calendar_service.working_days_in_month(2026, 8)

    await calendar_service.set_override(
        OverrideCreate(date=WEDNESDAY, kind=DayKind.WORKING, reason="Client go-live."),
        Actor(id=admin["id"], email=admin["email"]),
    )

    assert await calendar_service.working_days_in_month(2026, 8) == before + 1


async def test_withdrawing_a_holiday_puts_the_day_back(db, admin, closed_saturdays):
    created = await declare(admin, WEDNESDAY, "Founders' day")
    closed = await calendar_service.working_days_in_month(2026, 8)

    await holiday_service.delete_holiday(
        created["id"], Actor(id=admin["id"], email=admin["email"])
    )

    assert await calendar_service.working_days_in_month(2026, 8) == closed + 1


# --------------------------------------------------------------------------- #
# reopening one
# --------------------------------------------------------------------------- #
# A holiday the office decides to work is a working day in every sense.
#
# The work calendar lets an administrator reopen a holiday. The attendance
# labeller used to stamp HOLIDAY on the day regardless — and because the salary
# projection counts a reopened day as a working day, a day labelled HOLIDAY fell
# through every bucket to "absent". Everybody who came in as instructed was
# marked absent and docked for it.

SHIFT = {"required_minutes": 480, "half_day_minutes": 240, "working_weekdays": [0, 1, 2, 3, 4]}
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def judge(**kwargs):
    return compute_day_status(
        day=WEDNESDAY,
        today=date(2026, 8, 31),
        now=NOW,
        sessions=[],
        shift=SHIFT,
        **kwargs,
    )


def test_an_ordinary_holiday_is_still_a_holiday():
    assert judge(holiday={"name": "Diwali"}, working_day=False).status == AttendanceStatus.HOLIDAY


def test_a_holiday_with_no_calendar_ruling_is_still_a_holiday():
    """The pure callers that pass no ruling must not change behaviour."""
    assert judge(holiday={"name": "Diwali"}).status == AttendanceStatus.HOLIDAY


def test_a_reopened_holiday_is_judged_like_any_working_day():
    decision = judge(holiday={"name": "Diwali"}, working_day=True)

    # Nobody punched, and the office was open, so it is an absence — which is
    # the correct answer, and the one the projection is already assuming.
    assert decision.status == AttendanceStatus.ABSENT


def test_somebody_who_worked_a_reopened_holiday_is_present():
    decision = compute_day_status(
        day=WEDNESDAY,
        today=date(2026, 8, 31),
        now=NOW,
        # Real datetimes, not strings: `_event_time` ignores anything else,
        # and a session it cannot read is worth nothing.
        sessions=[
            {
                "punch_in": {"at": datetime(2026, 8, 12, 3, 30, tzinfo=UTC)},
                "punch_out": {"at": datetime(2026, 8, 12, 12, 30, tzinfo=UTC)},
            }
        ],
        shift=SHIFT,
        holiday={"name": "Diwali"},
        working_day=True,
    )

    assert decision.status == AttendanceStatus.PRESENT
