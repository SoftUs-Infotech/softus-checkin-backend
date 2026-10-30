"""Salary: history, the projected month, and who is allowed to see any of it.

March 2026 is the month throughout: 24 working days under the default pattern
(Monday to Friday plus the second and fourth Saturday), and a CTC of 24,000 so
one working day is worth exactly 1,000. Round numbers make a wrong deduction
obvious.

Attendance, leave requests and leave types are written straight into their
collections. This suite is about what the salary rules do with them, not about
how they got there.
"""

from datetime import date

import pytest
from bson import ObjectId

from app.core.datetime import utc_now
from app.db.mongo import Collections
from app.models.attendance import AttendanceStatus
from app.services import salary as salary_service
from app.services.work_calendar import decide_range
from tests.conftest import authed_client, invite_and_accept

MONTH = "2026-03"
CTC = 24_000.0
WORKING_DAYS = 24
DAY_RATE = 1_000.0

MON_TO_FRI = {"working_weekdays": [0, 1, 2, 3, 4]}


def march_working_days() -> list[str]:
    return [
        decision.date
        for decision in decide_range(
            date(2026, 3, 1), date(2026, 3, 31), shift=MON_TO_FRI, working_saturdays=[2, 4]
        )
        if decision.is_working
    ]


# --------------------------------------------------------------------------- #
# arithmetic, without a database
# --------------------------------------------------------------------------- #


ALL_ON = {
    "salary_deduct_absent": True,
    "salary_deduct_unpaid_leave": True,
    "salary_deduct_half_day": True,
    "salary_deduct_missing_punch_out": True,
}
ALL_OFF = dict.fromkeys(ALL_ON, False)


# --------------------------------------------------------------------------- #
# the projection, against real documents
# --------------------------------------------------------------------------- #
class TestProjection:
    pytestmark = pytest.mark.usefixtures("db")

    async def _employee(self, admin_client, api_prefix, email="paid@example.com"):
        session = await invite_and_accept(admin_client, api_prefix, email=email)
        user_id = session["user"]["id"]
        response = await admin_client.post(
            f"{api_prefix}/salary/employees/{user_id}",
            json={"monthly_ctc": CTC, "effective_from": "2026-01-01"},
        )
        assert response.status_code == 201, response.text
        return user_id, session["tokens"]["access_token"]

    async def _seed_march(self, db, user_id, overrides: dict[str, str] | None = None):
        """A full month of present days, with named exceptions."""
        overrides = overrides or {}
        documents = [
            {
                "user_id": user_id,
                "date": day,
                "status": overrides.get(day, AttendanceStatus.PRESENT.value),
                "worked_minutes": 540,
                "sessions": [],
                "is_deleted": False,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            for day in march_working_days()
            if overrides.get(day) != "MISSING"
        ]
        await db[Collections.ATTENDANCE].insert_many(documents)

    async def _project(self, user_id, **kwargs):
        return await salary_service.project_for_user(
            user_id, MONTH, today=date(2026, 4, 1), **kwargs
        )


    async def test_a_half_day_and_an_absence_are_both_deducted(
        self, db, admin_client, api_prefix
    ):
        user_id, _ = await self._employee(admin_client, api_prefix)
        await self._seed_march(
            db,
            user_id,
            {
                "2026-03-16": AttendanceStatus.ABSENT.value,
                "2026-03-17": AttendanceStatus.HALF_DAY.value,
            },
        )

        projection = await self._project(user_id)

        assert projection["total_deduction"] == 1500.0
        assert projection["projected_net"] == 22_500.0


    async def test_unpaid_leave_is_deducted_and_paid_leave_is_not(
        self, db, admin_client, api_prefix
    ):
        user_id, _ = await self._employee(admin_client, api_prefix)
        await self._seed_march(db, user_id, {"2026-03-16": "MISSING", "2026-03-17": "MISSING"})

        unpaid_type = ObjectId()
        paid_type = ObjectId()
        await db[Collections.LEAVE_TYPES].insert_many(
            [
                {"_id": unpaid_type, "name": "Leave without pay", "code": "LWP", "is_paid": False},
                {"_id": paid_type, "name": "Casual Leave", "code": "CL", "is_paid": True},
            ]
        )
        await db[Collections.LEAVE_REQUESTS].insert_many(
            [
                {
                    "user_id": user_id,
                    "leave_type_id": str(unpaid_type),
                    "status": "APPROVED",
                    "days": [{"date": "2026-03-16", "day_part": "FULL"}],
                    "is_deleted": False,
                },
                {
                    "user_id": user_id,
                    "leave_type_id": str(paid_type),
                    "status": "APPROVED",
                    "days": [{"date": "2026-03-17", "day_part": "FULL"}],
                    "is_deleted": False,
                },
            ]
        )

        projection = await self._project(user_id)

        assert projection["counts"]["unpaid_leave_days"] == 1
        assert projection["counts"]["paid_leave_days"] == 1
        assert projection["counts"]["absent_days"] == 0
        assert projection["projected_net"] == CTC - DAY_RATE


# --------------------------------------------------------------------------- #
# nobody but an administrator
# --------------------------------------------------------------------------- #
class TestAccess:
    pytestmark = pytest.mark.usefixtures("db")

    async def test_an_employee_is_refused_on_every_salary_route(
        self, admin_client, api_prefix
    ):
        session = await invite_and_accept(admin_client, api_prefix, email="curious@example.com")
        user_id = session["user"]["id"]
        await admin_client.post(
            f"{api_prefix}/salary/employees/{user_id}",
            json={"monthly_ctc": CTC, "effective_from": "2026-01-01"},
        )

        client = authed_client(session["tokens"]["access_token"])
        async with client:
            attempts = [
                await client.get(f"{api_prefix}/salary/employees/{user_id}"),
                await client.post(
                    f"{api_prefix}/salary/employees/{user_id}",
                    json={"monthly_ctc": 99_000, "effective_from": "2026-01-01"},
                ),
                await client.post(
                    f"{api_prefix}/salary/employees/{user_id}/adjustments",
                    json={"apply_month": MONTH, "days": 5, "reason": "A gift to myself."},
                ),
                await client.get(
                    f"{api_prefix}/salary/employees/{user_id}/projection",
                    params={"month": MONTH},
                ),
                await client.get(f"{api_prefix}/salary/projection", params={"month": MONTH}),
            ]

        assert [response.status_code for response in attempts] == [403] * 5
