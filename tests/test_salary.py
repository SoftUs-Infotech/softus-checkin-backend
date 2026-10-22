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
from app.core.exceptions import ValidationError
from app.db.mongo import Collections
from app.models.attendance import AttendanceStatus
from app.models.settings import SalaryDayBasis
from app.services import salary as salary_service
from app.services.salary import (
    DayCounts,
    adjustment_lines,
    deduction_lines,
    parse_month,
    per_day_rate,
    salary_in_force,
)
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
def test_a_month_must_look_like_a_month():
    assert parse_month("2026-03") == (2026, 3)
    with pytest.raises(ValidationError):
        parse_month("March")


@pytest.mark.parametrize(
    ("basis", "expected"),
    [
        (SalaryDayBasis.WORKING_DAYS, 1000.0),
        (SalaryDayBasis.CALENDAR_DAYS, 774.19),
        (SalaryDayBasis.FIXED_26, 923.08),
        (SalaryDayBasis.FIXED_30, 800.0),
    ],
)
def test_every_day_basis_prices_a_day_its_own_way(basis, expected):
    assert per_day_rate(CTC, basis, working_days=WORKING_DAYS, calendar_days=31) == expected


def test_a_month_with_no_working_days_has_no_rate_rather_than_an_error():
    assert per_day_rate(CTC, SalaryDayBasis.WORKING_DAYS, working_days=0, calendar_days=31) == 0.0


def test_the_record_in_force_is_the_latest_one_that_had_started():
    history = [
        {"monthly_ctc": 24000, "effective_from": "2026-01-01"},
        {"monthly_ctc": 30000, "effective_from": "2026-04-01"},
    ]
    assert salary_in_force(history, on="2026-03-31")["monthly_ctc"] == 24000
    assert salary_in_force(history, on="2026-04-30")["monthly_ctc"] == 30000


def test_a_month_before_the_first_record_has_no_salary():
    history = [{"monthly_ctc": 24000, "effective_from": "2026-04-01"}]
    assert salary_in_force(history, on="2026-03-31") is None


def _counts(**kwargs) -> DayCounts:
    return DayCounts(working_days=WORKING_DAYS, **kwargs)


ALL_ON = {
    "salary_deduct_absent": True,
    "salary_deduct_unpaid_leave": True,
    "salary_deduct_half_day": True,
    "salary_deduct_missing_punch_out": True,
}
ALL_OFF = dict.fromkeys(ALL_ON, False)


def test_every_deduction_flag_switched_on_produces_a_line():
    lines = deduction_lines(
        _counts(absent_days=2, unpaid_leave_days=1, half_days=1, missing_punch_out_days=1),
        day_rate=DAY_RATE,
        flags=ALL_ON,
    )
    assert {line["kind"]: line["amount"] for line in lines} == {
        "ABSENT": 2000.0,
        "UNPAID_LEAVE": 1000.0,
        "HALF_DAY": 500.0,
        "MISSING_PUNCH_OUT": 1000.0,
    }


def test_every_deduction_flag_switched_off_produces_nothing():
    lines = deduction_lines(
        _counts(absent_days=2, unpaid_leave_days=1, half_days=1, missing_punch_out_days=1),
        day_rate=DAY_RATE,
        flags=ALL_OFF,
    )
    assert lines == []


@pytest.mark.parametrize(
    ("flag", "kind"),
    [
        ("salary_deduct_absent", "ABSENT"),
        ("salary_deduct_unpaid_leave", "UNPAID_LEAVE"),
        ("salary_deduct_half_day", "HALF_DAY"),
        ("salary_deduct_missing_punch_out", "MISSING_PUNCH_OUT"),
    ],
)
def test_each_flag_controls_only_its_own_line(flag, kind):
    counts = _counts(absent_days=1, unpaid_leave_days=1, half_days=1, missing_punch_out_days=1)
    lines = deduction_lines(
        counts, day_rate=DAY_RATE, flags={**ALL_OFF, flag: True}
    )
    assert [line["kind"] for line in lines] == [kind]


def test_a_half_day_costs_half_a_day():
    lines = deduction_lines(_counts(half_days=3), day_rate=DAY_RATE, flags=ALL_ON)
    assert lines[0]["days"] == 1.5
    assert lines[0]["amount"] == 1500.0


def test_an_adjustment_is_priced_at_the_day_rate_in_both_directions():
    lines = adjustment_lines(
        [
            {"kind": "LATE_LEAVE_APPROVAL", "days": 1, "reason": "August leave approved late."},
            {"kind": "MANUAL", "days": -0.5, "reason": "Half day missed in February."},
        ],
        day_rate=DAY_RATE,
    )
    assert [line["amount"] for line in lines] == [1000.0, -500.0]


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

    async def test_a_full_month_is_paid_in_full(self, db, admin_client, api_prefix):
        user_id, _ = await self._employee(admin_client, api_prefix)
        await self._seed_march(db, user_id)

        projection = await self._project(user_id)

        assert projection["counts"]["working_days"] == WORKING_DAYS
        assert projection["counts"]["present_days"] == WORKING_DAYS
        assert projection["day_rate"] == DAY_RATE
        assert projection["deductions"] == []
        assert projection["projected_net"] == CTC

    async def test_a_missing_day_is_an_absence(self, db, admin_client, api_prefix):
        user_id, _ = await self._employee(admin_client, api_prefix)
        await self._seed_march(db, user_id, {"2026-03-16": "MISSING"})

        projection = await self._project(user_id)

        assert projection["counts"]["absent_days"] == 1
        assert projection["projected_net"] == CTC - DAY_RATE

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

    async def test_switching_off_the_half_day_rule_stops_deducting_it(
        self, db, admin_client, api_prefix
    ):
        user_id, _ = await self._employee(admin_client, api_prefix)
        await self._seed_march(db, user_id, {"2026-03-17": AttendanceStatus.HALF_DAY.value})
        await admin_client.patch(
            f"{api_prefix}/settings", json={"salary_deduct_half_day": False}
        )

        projection = await self._project(user_id)

        assert projection["deductions"] == []
        assert projection["projected_net"] == CTC

    async def test_a_missing_punch_out_only_costs_when_the_rule_is_on(
        self, db, admin_client, api_prefix
    ):
        user_id, _ = await self._employee(admin_client, api_prefix)
        await self._seed_march(
            db, user_id, {"2026-03-17": AttendanceStatus.MISSING_PUNCH_OUT.value}
        )

        assert (await self._project(user_id))["projected_net"] == CTC

        await admin_client.patch(
            f"{api_prefix}/settings", json={"salary_deduct_missing_punch_out": True}
        )
        assert (await self._project(user_id))["projected_net"] == CTC - DAY_RATE

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

    async def test_every_day_basis_reaches_the_projection(
        self, db, admin_client, api_prefix
    ):
        user_id, _ = await self._employee(admin_client, api_prefix)
        await self._seed_march(db, user_id)

        for basis, expected in [
            (SalaryDayBasis.WORKING_DAYS, 1000.0),
            (SalaryDayBasis.CALENDAR_DAYS, 774.19),
            (SalaryDayBasis.FIXED_26, 923.08),
            (SalaryDayBasis.FIXED_30, 800.0),
        ]:
            await admin_client.patch(
                f"{api_prefix}/settings", json={"salary_day_basis": basis.value}
            )
            projection = await self._project(user_id)
            assert projection["day_rate"] == expected, basis

    async def test_a_raise_applies_from_its_month_and_not_before(
        self, db, admin_client, api_prefix
    ):
        user_id, _ = await self._employee(admin_client, api_prefix)
        await self._seed_march(db, user_id)
        await admin_client.post(
            f"{api_prefix}/salary/employees/{user_id}",
            json={"monthly_ctc": 36_000, "effective_from": "2026-04-01"},
        )

        history = await admin_client.get(f"{api_prefix}/salary/employees/{user_id}")
        assert len(history.json()["data"]) == 2

        march = await self._project(user_id)
        assert march["monthly_ctc"] == CTC
        assert march["effective_from"] == "2026-01-01"

        april = await salary_service.project_for_user(
            user_id, "2026-04", today=date(2026, 5, 1)
        )
        assert april["monthly_ctc"] == 36_000

    async def test_an_adjustment_is_applied_to_the_month_it_names(
        self, db, admin_client, api_prefix
    ):
        user_id, _ = await self._employee(admin_client, api_prefix)
        await self._seed_march(db, user_id, {"2026-03-16": "MISSING"})

        response = await admin_client.post(
            f"{api_prefix}/salary/employees/{user_id}/adjustments",
            json={
                "apply_month": MONTH,
                "days": 1,
                "kind": "LATE_LEAVE_APPROVAL",
                "reason": "February leave approved in March.",
                "source_month": "2026-02",
                "source_date": "2026-02-10",
            },
        )
        assert response.status_code == 201, response.text

        projection = await self._project(user_id)

        assert projection["total_deduction"] == DAY_RATE
        assert projection["total_adjustment"] == DAY_RATE
        assert projection["adjustments"][0]["source_date"] == "2026-02-10"
        assert projection["projected_net"] == CTC

    async def test_an_adjustment_does_not_leak_into_another_month(
        self, db, admin_client, api_prefix
    ):
        user_id, _ = await self._employee(admin_client, api_prefix)
        await self._seed_march(db, user_id)
        await admin_client.post(
            f"{api_prefix}/salary/employees/{user_id}/adjustments",
            json={"apply_month": "2026-04", "days": -1, "reason": "Recovered in April."},
        )

        assert (await self._project(user_id))["adjustments"] == []

    async def test_the_org_projection_lists_everybody_and_flags_missing_salaries(
        self, db, admin_client, api_prefix
    ):
        user_id, _ = await self._employee(admin_client, api_prefix)
        await self._seed_march(db, user_id)
        await invite_and_accept(admin_client, api_prefix, email="unpriced@example.com")

        response = await admin_client.get(
            f"{api_prefix}/salary/projection", params={"month": MONTH}
        )

        data = response.json()["data"]
        assert data["month"] == MONTH
        assert data["missing_salary_count"] == 2  # the administrator and the new employee
        priced = [item for item in data["employees"] if item["user_id"] == user_id][0]
        assert priced["projected_net"] == CTC

    async def test_a_swapped_month_keeps_the_same_day_rate(
        self, db, admin_client, api_prefix
    ):
        """The point of insisting a swap preserves the count: pay does not move."""
        user_id, _ = await self._employee(admin_client, api_prefix)
        await self._seed_march(db, user_id)
        await admin_client.post(
            f"{api_prefix}/work-calendar/swaps",
            json={
                "off_date": "2026-03-14",
                "working_date": "2026-03-21",
                "reason": "Third Saturday off instead of the second.",
            },
        )

        projection = await self._project(user_id)

        assert projection["counts"]["working_days"] == WORKING_DAYS
        assert projection["day_rate"] == DAY_RATE


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

    async def test_no_salary_route_answers_an_anonymous_caller(self, client, api_prefix):
        response = await client.get(f"{api_prefix}/salary/projection", params={"month": MONTH})
        assert response.status_code == 401

    async def test_the_employees_own_record_never_carries_a_salary_field(
        self, admin_client, api_prefix
    ):
        """An employee must not read their figure from a route they are allowed on."""
        session = await invite_and_accept(admin_client, api_prefix, email="profile@example.com")
        user_id = session["user"]["id"]
        await admin_client.post(
            f"{api_prefix}/salary/employees/{user_id}",
            json={"monthly_ctc": CTC, "effective_from": "2026-01-01"},
        )

        client = authed_client(session["tokens"]["access_token"])
        async with client:
            me = await client.get(f"{api_prefix}/auth/me")

        body = str(me.json()["data"])
        assert "monthly_ctc" not in body
        assert "24000" not in body

    async def test_the_salary_history_is_kept_and_never_overwritten(
        self, admin_client, api_prefix
    ):
        session = await invite_and_accept(admin_client, api_prefix, email="history@example.com")
        user_id = session["user"]["id"]
        for ctc, effective_from in [(CTC, "2026-01-01"), (36_000, "2026-04-01")]:
            await admin_client.post(
                f"{api_prefix}/salary/employees/{user_id}",
                json={"monthly_ctc": ctc, "effective_from": effective_from},
            )

        response = await admin_client.get(f"{api_prefix}/salary/employees/{user_id}")

        records = response.json()["data"]
        assert [record["effective_from"] for record in records] == ["2026-04-01", "2026-01-01"]
        assert [record["monthly_ctc"] for record in records] == [36_000, CTC]
