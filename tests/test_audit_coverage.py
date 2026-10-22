"""Administrative decisions that move money or working days must leave a trail.

Salary and the work calendar were the two areas built without one; these tests
are here so they cannot quietly lose it again.
"""

from datetime import date

import pytest

from app.db.mongo import Collections
from app.models.work_calendar import DayKind
from app.schemas.salary import SalaryCreate
from app.schemas.work_calendar import OverrideCreate
from app.services import salary as salary_service
from app.services import work_calendar as work_calendar_service
from app.services.audit import Actor

pytestmark = pytest.mark.asyncio


async def entries(db, entity: str) -> list[dict]:
    cursor = db[Collections.AUDIT_LOGS].find({"entity": entity})
    return await cursor.to_list(length=20)


async def test_setting_a_salary_is_audited(db, admin):
    await salary_service.set_salary(
        admin["id"],
        SalaryCreate(monthly_ctc=50000, effective_from=date(2026, 1, 1), note="Joining"),
        Actor(id=admin["id"], email=admin["email"]),
    )

    rows = await entries(db, "SALARY")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == admin["email"]


async def test_a_raise_records_what_it_replaced(db, admin):
    actor = Actor(id=admin["id"], email=admin["email"])
    for amount in (50000, 60000):
        await salary_service.set_salary(
            admin["id"],
            SalaryCreate(monthly_ctc=amount, effective_from=date(2026, 1, 1)),
            actor,
        )

    rows = sorted(await entries(db, "SALARY"), key=lambda row: row["at"])
    assert rows[-1]["before"]["monthly_ctc"] == 50000
    assert rows[-1]["after"]["monthly_ctc"] == 60000


async def test_ruling_on_a_date_is_audited(db, admin):
    await work_calendar_service.set_override(
        OverrideCreate(date=date(2026, 3, 7), kind=DayKind.WORKING, reason="Quarter close"),
        Actor(id=admin["id"], email=admin["email"]),
    )

    rows = await entries(db, "WORK_CALENDAR")
    assert len(rows) == 1
    assert "2026-03-07" in rows[0]["entity_label"]


async def test_removing_a_ruling_is_audited(db, admin):
    actor = Actor(id=admin["id"], email=admin["email"])
    day = date(2026, 3, 7)
    await work_calendar_service.set_override(
        OverrideCreate(date=day, kind=DayKind.WORKING, reason="Quarter close"), actor
    )

    await work_calendar_service.remove_override(day, actor)

    actions = [row["action"] for row in await entries(db, "WORK_CALENDAR")]
    assert "DELETE" in actions
