"""Interns and part-timers: a morning-only late mark, and pay by the hours put in.

An intern comes in from college at noon. Under an ordinary shift that is three
hours late, every day, and the late penalty eats their stipend. The intern
shift carries a late window instead: only a *morning* arrival — after the grace
period and within the window — is late. Their pay is the fixed monthly amount
in proportion to the hours they actually put in, never more than the amount.

The intern shift throughout: 09:30 start, 30 minutes' grace, a 30-minute late
window, four hours a day.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.db.mongo import Collections
from app.services import salary as salary_service
from app.services import work_calendar as calendar_service
from app.services.attendance_rules import is_late_arrival
from tests.conftest import invite_and_accept

INTERN_SHIFT = {
    "start_time": "09:30",
    "end_time": "13:30",
    "required_minutes": 240,
    "half_day_minutes": 120,
    "grace_minutes": 30,
    "late_window_minutes": 30,
    "break_minutes": 0,
    "working_weekdays": [0, 1, 2, 3, 4],
}
GENERAL_SHIFT = {**INTERN_SHIFT, "late_window_minutes": 0, "grace_minutes": 15}
MARCH = (date(2026, 3, 1), date(2026, 3, 31))
STIPEND = 10_000.0


def at(hour: int, minute: int = 0, day: date = date(2026, 3, 18)) -> datetime:
    """A wall-clock time in Asia/Kolkata expressed as UTC."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC) - timedelta(
        hours=5, minutes=30
    )


# --------------------------------------------------------------------------- #
# late only in the morning
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("hour", "minute", "late"),
    [
        (9, 55, False),  # inside the grace period
        (10, 20, True),  # a late morning
        (12, 0, False),  # an afternoon arrival, straight from college
    ],
)
def test_an_intern_is_late_only_for_a_late_morning(hour, minute, late):
    assert is_late_arrival(at(hour, minute), INTERN_SHIFT) is late


# --------------------------------------------------------------------------- #
# the hours tally, without a database
# --------------------------------------------------------------------------- #
def _march(**kw):
    return calendar_service.decide_range(
        *MARCH, shift=INTERN_SHIFT, working_saturdays=[2, 4], **kw
    )


WORKING = [d for d in _march() if d.is_working]


# --------------------------------------------------------------------------- #
# through the API
# --------------------------------------------------------------------------- #
async def _intern(admin_client, api_prefix, *, part_time: bool = True) -> str:
    shift = await admin_client.post(
        f"{api_prefix}/shifts",
        json={"name": "Interns", "code": "INTERN", **INTERN_SHIFT},
    )
    assert shift.status_code == 201, shift.text
    assert shift.json()["data"]["late_window_minutes"] == 30

    session = await invite_and_accept(admin_client, api_prefix, email="intern@example.com")
    user_id = session["user"]["id"]
    updated = await admin_client.patch(
        f"{api_prefix}/users/{user_id}",
        json={
            "employment_type": "PART_TIME" if part_time else "FULL_TIME",
            "shift_id": shift.json()["data"]["id"],
        },
    )
    assert updated.status_code == 200, updated.text
    salary = await admin_client.post(
        f"{api_prefix}/salary/employees/{user_id}",
        json={"monthly_ctc": STIPEND, "effective_from": "2026-01-01"},
    )
    assert salary.status_code == 201, salary.text
    return user_id


async def _worked_every_day(db, user_id: str, minutes: int, *, late: bool = False) -> None:
    await db[Collections.ATTENDANCE].insert_many(
        [
            {
                "user_id": user_id,
                "date": d.date,
                "sessions": [{"punch_in": {"at": at(12, 0, d.day)}}],
                "shift_snapshot": INTERN_SHIFT,
                "status": "PRESENT",
                "worked_minutes": minutes,
                "is_late": late,
                "is_deleted": False,
            }
            for d in WORKING
        ]
    )


@pytest.mark.usefixtures("db")
async def test_an_intern_on_three_hours_a_day_is_paid_three_quarters(
    db, admin_client, api_prefix
):
    user_id = await _intern(admin_client, api_prefix)
    await _worked_every_day(db, user_id, 180)

    projection = await salary_service.project_for_user(user_id, "2026-03", today=date(2026, 4, 1))

    assert projection["pay_basis"] == "HOURS"
    assert projection["hours"]["expected_minutes"] == len(WORKING) * 240
    assert projection["hours"]["worked_minutes"] == len(WORKING) * 180
    assert projection["projected_net"] == pytest.approx(7_500.0)
    [line] = projection["deductions"]
    assert line["kind"] == "HOURS_SHORT"
    assert line["amount"] == pytest.approx(2_500.0)
