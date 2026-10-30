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
from tests.conftest import invite_and_accept

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
