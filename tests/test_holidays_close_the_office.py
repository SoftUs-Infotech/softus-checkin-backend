"""A holiday is a closed day, not a note attached to an open one.

Holidays used to live in their own collection that only the attendance labeller
consulted. The work calendar still called the day working, so the month kept its
full working-day count and the per-day rate everybody's deductions are priced
from was computed as though nobody had a day off. Declaring three holidays has
to take three days out of the month, everywhere, at once.
"""

from datetime import UTC, date, datetime

import pytest

from app.db.mongo import Collections
from app.models.settings import SETTINGS_KEY
from app.schemas.holiday import HolidayCreate
from app.schemas.salary import SalaryCreate
from app.services import holiday as holiday_service
from app.services import salary as salary_service
from app.services import settings as settings_service
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
