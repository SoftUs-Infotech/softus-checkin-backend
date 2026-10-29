"""A date counted as a full day for everybody who came in.

The office sometimes opens for three or four hours on purpose — a short
Saturday. Judged by the shift, everybody who came is short, half-day or late
for it, and loses pay for a day the owner decided to keep short. The
administrator ticks the date on the work calendar instead: whoever punched in
is present for a full day, whenever the tick is made. Leave stays leave, and
somebody who never came stays absent.

Saturday 14 March 2026 throughout: the second Saturday, a working day under the
default pattern. The office opened 10:00–13:00.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.db.mongo import Collections
from app.models.attendance import AttendanceStatus
from app.services import salary as salary_service
from app.services.attendance_rules import compute_day_status
from tests.conftest import authed_client, invite_and_accept

SHIFT = {
    "start_time": "09:30",
    "end_time": "18:30",
    "required_minutes": 540,
    "half_day_minutes": 240,
    "grace_minutes": 15,
    "break_minutes": 0,
    "working_weekdays": [0, 1, 2, 3, 4, 5],
}
SHORT_SATURDAY = date(2026, 3, 14)
NEXT_DAY = date(2026, 3, 16)


def local(day: date, hour: int, minute: int = 0) -> datetime:
    """A wall-clock time in Asia/Kolkata expressed as UTC."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC) - timedelta(
        hours=5, minutes=30
    )


def session(day: date, start: tuple[int, int], end: tuple[int, int] | None) -> dict:
    return {
        "punch_in": {"at": local(day, *start)},
        "punch_out": {"at": local(day, *end)} if end else None,
    }


def judge(day: date, sessions: list[dict], *, today: date = NEXT_DAY, **kw):
    return compute_day_status(
        day, sessions, SHIFT, today=today, now=local(today, 23, 0), **kw
    )


# --------------------------------------------------------------------------- #
# the rule, without a database
# --------------------------------------------------------------------------- #
def test_three_hours_on_a_short_day_is_short_without_the_ruling():
    result = judge(SHORT_SATURDAY, [session(SHORT_SATURDAY, (10, 0), (13, 0))])

    assert result.status is AttendanceStatus.SHORT_HOURS
    assert result.is_late is True


def test_five_hours_on_a_short_day_is_a_half_day_without_the_ruling():
    result = judge(SHORT_SATURDAY, [session(SHORT_SATURDAY, (10, 0), (15, 0))])

    assert result.status is AttendanceStatus.HALF_DAY


def test_five_hours_on_a_full_day_is_present():
    result = judge(
        SHORT_SATURDAY, [session(SHORT_SATURDAY, (10, 0), (15, 0))], full_day=True
    )

    assert result.status is AttendanceStatus.PRESENT


def test_three_hours_on_a_full_day_is_present_and_not_late():
    result = judge(
        SHORT_SATURDAY, [session(SHORT_SATURDAY, (10, 0), (13, 0))], full_day=True
    )

    assert result.status is AttendanceStatus.PRESENT
    assert result.is_late is False
    # The hours are kept as worked, and nothing is shown as missing.
    assert result.worked_minutes == 180
    assert result.shortfall_minutes == 0


def test_somebody_who_never_came_is_still_absent():
    assert judge(SHORT_SATURDAY, [], full_day=True).status is AttendanceStatus.ABSENT


def test_leave_on_a_full_day_stays_leave():
    result = judge(SHORT_SATURDAY, [], full_day=True, leave={"day_part": "FULL"})

    assert result.status is AttendanceStatus.ON_LEAVE


def test_a_holiday_stays_a_holiday():
    result = judge(SHORT_SATURDAY, [], full_day=True, holiday={"name": "Holi"})

    assert result.status is AttendanceStatus.HOLIDAY


def test_on_the_day_itself_somebody_still_inside_is_in_progress():
    now = local(SHORT_SATURDAY, 11, 0)
    result = compute_day_status(
        SHORT_SATURDAY,
        [session(SHORT_SATURDAY, (10, 0), None)],
        SHIFT,
        today=SHORT_SATURDAY,
        now=now,
        full_day=True,
    )

    assert result.status is AttendanceStatus.IN_PROGRESS


def test_a_forgotten_punch_out_on_a_full_day_still_counts_as_there():
    result = judge(SHORT_SATURDAY, [session(SHORT_SATURDAY, (10, 0), None)], full_day=True)

    assert result.status is AttendanceStatus.PRESENT


# --------------------------------------------------------------------------- #
# marking a date, through the API
# --------------------------------------------------------------------------- #
pytestmark_db = pytest.mark.usefixtures("db")


async def _short_saturday_record(
    db, user_id: str, *, until: int = 13, status: AttendanceStatus = AttendanceStatus.SHORT_HOURS
) -> None:
    """What punching 10:00 until ``until`` leaves behind, judged by the shift."""
    await db[Collections.ATTENDANCE].insert_one(
        {
            "user_id": user_id,
            "date": SHORT_SATURDAY.isoformat(),
            "sessions": [session(SHORT_SATURDAY, (10, 0), (until, 0))],
            "shift_snapshot": SHIFT,
            "status": status.value,
            "worked_minutes": (until - 10) * 60,
            "is_late": True,
            "is_deleted": False,
        }
    )


async def _stored(db, user_id: str) -> dict:
    return await db[Collections.ATTENDANCE].find_one(
        {"user_id": user_id, "date": SHORT_SATURDAY.isoformat()}
    )


async def _mark(admin_client, api_prefix, reason: str = "Open till 1 pm"):
    return await admin_client.post(
        f"{api_prefix}/work-calendar/full-days",
        json={"date": SHORT_SATURDAY.isoformat(), "reason": reason},
    )


@pytestmark_db
async def test_marking_after_everyone_left_makes_the_day_full(db, admin_client, api_prefix):
    session_ = await invite_and_accept(admin_client, api_prefix, email="came@example.com")
    user_id = session_["user"]["id"]
    await _short_saturday_record(db, user_id)

    response = await _mark(admin_client, api_prefix)

    assert response.status_code == 200, response.text
    stored = await _stored(db, user_id)
    assert stored["status"] == AttendanceStatus.PRESENT.value
    assert stored["is_late"] is False
    # The punches are what they were: only the judgement changed.
    assert stored["worked_minutes"] == 180
    assert len(stored["sessions"]) == 1


@pytestmark_db
async def test_unmarking_judges_the_day_by_its_hours_again(db, admin_client, api_prefix):
    session_ = await invite_and_accept(admin_client, api_prefix, email="came@example.com")
    user_id = session_["user"]["id"]
    await _short_saturday_record(db, user_id)
    await _mark(admin_client, api_prefix)

    response = await admin_client.delete(
        f"{api_prefix}/work-calendar/full-days/{SHORT_SATURDAY.isoformat()}"
    )

    assert response.status_code == 200, response.text
    stored = await _stored(db, user_id)
    assert stored["status"] == AttendanceStatus.SHORT_HOURS.value
    assert stored["is_late"] is True


@pytestmark_db
async def test_the_calendar_and_the_register_show_it(db, admin_client, api_prefix):
    came = await invite_and_accept(admin_client, api_prefix, email="came@example.com")
    stayed_home = await invite_and_accept(
        admin_client, api_prefix, email="home@example.com", first_name="Bo"
    )
    await _short_saturday_record(db, came["user"]["id"])
    await _mark(admin_client, api_prefix)

    month = await admin_client.get(
        f"{api_prefix}/work-calendar/month", params={"year": 2026, "month": 3}
    )
    day = next(d for d in month.json()["data"]["days"] if d["date"] == "2026-03-14")
    assert day["full_day"] is True
    assert day["full_day_reason"] == "Open till 1 pm"
    one = await admin_client.get(
        f"{api_prefix}/work-calendar/day", params={"on": "2026-03-14"}
    )
    assert one.json()["data"]["full_day"] is True

    async def status_of(user_id: str) -> str:
        calendar = await admin_client.get(
            f"{api_prefix}/attendance/calendar",
            params={"year": 2026, "month": 3, "user_id": user_id},
        )
        days = calendar.json()["data"]["days"]
        return next(d for d in days if d["date"] == "2026-03-14")["status"]

    assert await status_of(came["user"]["id"]) == "PRESENT"
    # Ticking the day does not turn somebody who stayed home into present.
    assert await status_of(stayed_home["user"]["id"]) == "ABSENT"


@pytestmark_db
async def test_salary_pays_the_short_day_in_full(db, admin_client, api_prefix):
    # Five hours is past the half-day line, so by the shift it costs half a day.
    session_ = await invite_and_accept(admin_client, api_prefix, email="paid@example.com")
    user_id = session_["user"]["id"]
    await _short_saturday_record(db, user_id, until=15, status=AttendanceStatus.HALF_DAY)

    before = await salary_service.project_for_user(user_id, "2026-03", today=date(2026, 4, 1))
    await _mark(admin_client, api_prefix)
    after = await salary_service.project_for_user(user_id, "2026-03", today=date(2026, 4, 1))

    assert before["counts"]["half_days"] == 1
    assert after["counts"]["half_days"] == 0
    assert after["counts"]["present_days"] == before["counts"]["present_days"] + 1
    assert after["counts"]["late_days"] == before["counts"]["late_days"] - 1
    assert after["projected_net"] >= before["projected_net"]


@pytestmark_db
async def test_only_an_administrator_can_mark_a_day(admin_client, api_prefix):
    session_ = await invite_and_accept(admin_client, api_prefix, email="emp@example.com")

    async with authed_client(session_["tokens"]["access_token"]) as employee:
        response = await employee.post(
            f"{api_prefix}/work-calendar/full-days",
            json={"date": SHORT_SATURDAY.isoformat()},
        )

    assert response.status_code == 403


@pytestmark_db
async def test_marking_is_on_the_audit_trail_and_listed(admin_client, api_prefix):
    await _mark(admin_client, api_prefix)
    await _mark(admin_client, api_prefix, reason="Open till 2 pm")  # re-marking edits it

    listed = await admin_client.get(
        f"{api_prefix}/work-calendar/full-days",
        params={"from_date": "2026-03-01", "to_date": "2026-03-31"},
    )
    logs = await admin_client.get(
        f"{api_prefix}/audit-logs", params={"entity": "WORK_CALENDAR"}
    )

    assert [(d["date"], d["reason"]) for d in listed.json()["data"]] == [
        ("2026-03-14", "Open till 2 pm")
    ]
    labels = [entry["entity_label"] for entry in logs.json()["data"]["items"]]
    assert "2026-03-14 counted as a full day" in labels


@pytestmark_db
async def test_unmarking_a_day_that_was_never_marked_says_so(admin_client, api_prefix):
    response = await admin_client.delete(f"{api_prefix}/work-calendar/full-days/2026-03-21")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FULL_DAY_NOT_FOUND"
