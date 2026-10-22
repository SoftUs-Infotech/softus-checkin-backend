"""Pay follows the calendar, immediately.

Changing which Saturdays are working changes how many working days the month
has, which changes the per-day rate, which changes every deduction priced from
it. If that chain breaks anywhere the figures are quietly wrong, so it is
tested end to end rather than a link at a time.
"""

from datetime import date

import pytest

from app.db.mongo import Collections
from app.models.settings import SETTINGS_KEY
from app.models.work_calendar import DayKind
from app.schemas.salary import SalaryCreate
from app.schemas.work_calendar import OverrideCreate
from app.services import salary as salary_service
from app.services import settings as settings_service
from app.services import work_calendar as work_calendar_service
from app.services.audit import Actor

pytestmark = pytest.mark.asyncio

# August 2026: 31 days, five Sundays, five Saturdays.
MONTH = "2026-08"
AFTER_THE_MONTH = date(2026, 9, 1)


async def set_pattern(db, saturdays: list[int]) -> None:
    await db[Collections.SETTINGS].update_one(
        {"key": SETTINGS_KEY},
        {"$set": {"working_saturdays": saturdays, "fifth_saturday_working": None}},
        upsert=True,
    )
    settings_service.clear_cache()


async def projection(user_id: str) -> dict:
    return await salary_service.project_for_user(user_id, MONTH, today=AFTER_THE_MONTH)


@pytest.fixture
async def paid_admin(db, admin):
    await salary_service.set_salary(
        admin["id"],
        SalaryCreate(monthly_ctc=46000, effective_from=date(2026, 1, 1)),
        Actor(id=admin["id"], email=admin["email"]),
    )
    return admin


async def test_every_saturday_working_gives_the_most_working_days(db, paid_admin):
    await set_pattern(db, [1, 2, 3, 4, 5])

    result = await projection(paid_admin["id"])

    assert result["counts"]["working_days"] == 26


async def test_the_second_and_fourth_pattern_gives_twenty_three(db, paid_admin):
    await set_pattern(db, [2, 4])

    result = await projection(paid_admin["id"])

    assert result["counts"]["working_days"] == 23


async def test_no_working_saturdays_gives_twenty_one(db, paid_admin):
    await set_pattern(db, [])

    result = await projection(paid_admin["id"])

    assert result["counts"]["working_days"] == 21


async def test_the_day_rate_moves_with_the_working_day_count(db, paid_admin):
    await set_pattern(db, [1, 2, 3, 4, 5])
    wide = await projection(paid_admin["id"])

    await set_pattern(db, [2, 4])
    narrow = await projection(paid_admin["id"])

    # Same salary, fewer working days, so each day is worth more.
    assert wide["monthly_ctc"] == narrow["monthly_ctc"]
    assert narrow["day_rate"] > wide["day_rate"]
    assert narrow["day_rate"] == round(46000 / 23, 2)


async def test_closing_one_saturday_by_hand_removes_a_working_day(db, paid_admin):
    await set_pattern(db, [2, 4])
    before = await projection(paid_admin["id"])

    await work_calendar_service.set_override(
        OverrideCreate(date=date(2026, 8, 8), kind=DayKind.OFF, reason="Given back"),
        Actor(id=paid_admin["id"], email=paid_admin["email"]),
    )
    after = await projection(paid_admin["id"])

    assert after["counts"]["working_days"] == before["counts"]["working_days"] - 1
    assert after["day_rate"] > before["day_rate"]


async def test_opening_one_saturday_by_hand_adds_a_working_day(db, paid_admin):
    await set_pattern(db, [2, 4])
    before = await projection(paid_admin["id"])

    await work_calendar_service.set_override(
        OverrideCreate(date=date(2026, 8, 1), kind=DayKind.WORKING, reason="Quarter close"),
        Actor(id=paid_admin["id"], email=paid_admin["email"]),
    )
    after = await projection(paid_admin["id"])

    assert after["counts"]["working_days"] == before["counts"]["working_days"] + 1
