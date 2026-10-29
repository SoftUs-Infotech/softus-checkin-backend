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
        (10, 0, False),  # the last minute of grace
        (10, 20, True),  # a late morning
        (10, 30, True),  # the window's last minute
        (10, 31, False),  # past the window: an afternoon arrival
        (12, 0, False),
        (13, 30, False),
    ],
)
def test_an_intern_is_late_only_for_a_late_morning(hour, minute, late):
    assert is_late_arrival(at(hour, minute), INTERN_SHIFT) is late


def test_a_shift_without_a_window_still_calls_any_later_arrival_late():
    assert is_late_arrival(at(13, 0), GENERAL_SHIFT) is True


def test_a_shift_saved_before_the_window_existed_behaves_as_before():
    legacy = {key: value for key, value in GENERAL_SHIFT.items() if key != "late_window_minutes"}

    assert is_late_arrival(at(13, 0), legacy) is True


# --------------------------------------------------------------------------- #
# the hours tally, without a database
# --------------------------------------------------------------------------- #
def _march(**kw):
    return calendar_service.decide_range(
        *MARCH, shift=INTERN_SHIFT, working_saturdays=[2, 4], **kw
    )


WORKING = [d for d in _march() if d.is_working]


def _tally(*, attendance=None, leave=None, full_days=None, **kw):
    return salary_service._count_hours(
        _march(),
        today=date(2026, 4, 1),
        attendance=attendance or {},
        leave=leave or {},
        holidays={},
        shift=INTERN_SHIFT,
        full_days=full_days or {},
        **kw,
    )


def _days_worked(minutes: int) -> dict:
    return {d.date: {"worked_minutes": minutes, "sessions": [{}]} for d in WORKING}


def test_the_month_expects_every_working_day_at_the_shifts_hours():
    assert _tally().expected_minutes == len(WORKING) * 240


def test_three_hours_a_day_earns_three_quarters():
    hours = _tally(attendance=_days_worked(180))

    assert hours.share == pytest.approx(0.75)


def test_a_long_day_makes_up_for_a_short_one():
    attendance = _days_worked(240)
    first, second = WORKING[0].date, WORKING[1].date
    attendance[first]["worked_minutes"] = 120
    attendance[second]["worked_minutes"] = 360

    assert _tally(attendance=attendance).share == pytest.approx(1.0)


def test_more_hours_than_expected_never_pay_more_than_the_amount():
    assert _tally(attendance=_days_worked(400)).share == 1.0


def test_paid_leave_counts_and_unpaid_leave_does_not():
    attendance = _days_worked(240)
    paid, unpaid = WORKING[0].date, WORKING[1].date
    del attendance[paid], attendance[unpaid]
    leave = {
        paid: {"day_part": "FULL", "is_paid": True},
        unpaid: {"day_part": "FULL", "is_paid": False},
    }

    hours = _tally(attendance=attendance, leave=leave)

    assert hours.paid_leave_minutes == 240
    assert hours.credited_minutes == hours.expected_minutes - 240


def test_a_day_ruled_a_full_day_pays_its_full_hours():
    attendance = _days_worked(240)
    short = WORKING[3].date
    attendance[short]["worked_minutes"] = 90

    hours = _tally(attendance=attendance, full_days={short: {}})

    assert hours.full_day_minutes == 150
    assert hours.share == 1.0


def test_days_before_joining_are_expected_but_earn_nothing():
    joined = WORKING[10].day
    attendance = {d.date: {"worked_minutes": 240, "sessions": [{}]} for d in WORKING[10:]}

    hours = _tally(attendance=attendance, employed_from=joined)

    assert hours.expected_minutes == len(WORKING) * 240
    assert hours.credited_minutes == (len(WORKING) - 10) * 240


def test_days_still_to_come_are_assumed_worked():
    hours = salary_service._count_hours(
        _march(),
        today=WORKING[-1].day,
        attendance=_days_worked(240),
        leave={},
        holidays={},
        shift=INTERN_SHIFT,
        full_days={},
    )

    assert hours.pending_minutes == 240
    assert hours.share == 1.0


def test_a_full_month_has_no_hours_short_line():
    assert salary_service.hours_lines(STIPEND, _tally(attendance=_days_worked(240))) == []


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
async def test_employment_type_is_saved_and_returned(admin_client, api_prefix):
    user_id = await _intern(admin_client, api_prefix)

    user = await admin_client.get(f"{api_prefix}/users/{user_id}")

    assert user.json()["data"]["employment_type"] == "PART_TIME"


@pytest.mark.usefixtures("db")
async def test_an_invite_can_be_part_time_from_the_start(admin_client, api_prefix):
    invite = await admin_client.post(
        f"{api_prefix}/auth/invite",
        json={"email": "new@example.com", "first_name": "Neha", "employment_type": "PART_TIME"},
    )
    assert invite.status_code == 201, invite.text

    listed = await admin_client.get(f"{api_prefix}/users", params={"search": "new@example.com"})

    assert listed.json()["data"]["items"][0]["employment_type"] == "PART_TIME"


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


@pytest.mark.usefixtures("db")
async def test_late_marks_do_not_cost_an_intern_twice(db, admin_client, api_prefix):
    # Every day "late" by the record, with the late penalty switched on: the
    # hours already carry it, so it must not be charged again.
    await admin_client.patch(f"{api_prefix}/settings", json={"late_arrivals_per_half_day": 3})
    user_id = await _intern(admin_client, api_prefix)
    await _worked_every_day(db, user_id, 240, late=True)

    projection = await salary_service.project_for_user(user_id, "2026-03", today=date(2026, 4, 1))

    assert projection["deductions"] == []
    assert projection["projected_net"] == STIPEND


@pytest.mark.usefixtures("db")
async def test_a_full_timer_is_still_paid_by_the_day(db, admin_client, api_prefix):
    user_id = await _intern(admin_client, api_prefix, part_time=False)
    await _worked_every_day(db, user_id, 180)

    projection = await salary_service.project_for_user(user_id, "2026-03", today=date(2026, 4, 1))

    assert projection["pay_basis"] == "DAYS"
    assert projection["hours"] is None


@pytest.mark.usefixtures("db")
async def test_the_salary_page_carries_the_hours(db, admin_client, api_prefix):
    user_id = await _intern(admin_client, api_prefix)
    await _worked_every_day(db, user_id, 180)

    response = await admin_client.get(f"{api_prefix}/salary/projection", params={"month": "2026-03"})

    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["data"]["employees"] if r["user_id"] == user_id)
    assert row["employment_type"] == "PART_TIME"
    assert row["pay_basis"] == "HOURS"
    assert row["hours"]["credited_minutes"] == len(WORKING) * 180
