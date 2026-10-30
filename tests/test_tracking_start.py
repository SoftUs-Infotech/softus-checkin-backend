"""The go-live reset: attendance counts from the day the system went live.

Without it, the day an organisation deploys reads as a month of absences behind
it, and the deploy day itself as a short day for everybody — the app arrived
halfway through a shift nobody could have punched into. The administrator
presses "reset" that day: every earlier day stops counting, the day itself is a
grace day, and the ordinary rules start the next morning.

March 2026 throughout: 24 working days under the default pattern, a CTC of
24,000, so one working day is worth 1,000. Go-live is Monday the 16th.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.attendance import AttendanceStatus
from app.services import salary as salary_service
from app.services import settings as settings_service
from app.services.audit import SYSTEM
from app.services.work_calendar import decide_range
from tests.conftest import invite_and_accept

SHIFT = {
    "start_time": "09:30",
    "end_time": "18:30",
    "required_minutes": 540,
    "half_day_minutes": 240,
    "grace_minutes": 15,
    "break_minutes": 0,
    "working_weekdays": [0, 1, 2, 3, 4],
}
GO_LIVE = date(2026, 3, 16)  # a Monday
DAY_AFTER = date(2026, 3, 17)
BEFORE = date(2026, 3, 10)
CTC = 24_000.0
DAY_RATE = 1_000.0


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
# the reset itself
# --------------------------------------------------------------------------- #
class TestReset:
    pytestmark = pytest.mark.usefixtures("db")


    async def test_the_month_and_the_register_start_at_go_live(self, admin_client, api_prefix):
        session = await invite_and_accept(admin_client, api_prefix, email="riya@example.com")
        user_id = session["user"]["id"]
        await admin_client.post(
            f"{api_prefix}/settings/tracking-start", json={"starts_on": GO_LIVE.isoformat()}
        )

        calendar = await admin_client.get(
            f"{api_prefix}/attendance/calendar",
            params={"year": 2026, "month": 3, "user_id": user_id},
        )
        body = calendar.json()["data"]
        statuses = {day["date"]: day["status"] for day in body["days"]}

        assert statuses["2026-03-02"] == "NOT_TRACKED"
        assert statuses["2026-03-16"] == "NOT_TRACKED"  # go-live, never punched
        assert statuses["2026-03-17"] == "ABSENT"
        assert body["labels"]["NOT_TRACKED"] == "Not tracked"
        # Only the days from the 17th are working days that could be missed.
        assert body["summary"]["absent_days"] == sum(
            1 for day, status in statuses.items() if status == "ABSENT"
        )
        assert all(day > "2026-03-16" for day, s in statuses.items() if s == "ABSENT")

        before, _ = await self._register(admin_client, api_prefix, "2026-03-10")
        after, _ = await self._register(admin_client, api_prefix, "2026-03-17")
        assert before == []
        assert "Ada L" in [row["employee_name"] for row in after]

    @staticmethod
    async def _register(admin_client, api_prefix, day):
        response = await admin_client.get(
            f"{api_prefix}/attendance/register",
            params={"day": day, "exceptions_only": True, "search": "riya"},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"], response.json()["meta"]


    async def test_salary_pays_the_days_before_go_live_in_full(self, admin_client, api_prefix, db):
        session = await invite_and_accept(admin_client, api_prefix, email="paid@example.com")
        user_id = session["user"]["id"]
        await admin_client.post(
            f"{api_prefix}/salary/employees/{user_id}",
            json={"monthly_ctc": CTC, "effective_from": "2026-01-01"},
        )
        await settings_service.set_tracking_start(GO_LIVE, SYSTEM)
        working = [
            decision.date
            for decision in decide_range(
                date(2026, 3, 1),
                date(2026, 3, 31),
                shift={"working_weekdays": [0, 1, 2, 3, 4]},
                working_saturdays=[2, 4],
            )
            if decision.is_working
        ]
        # Present on every working day after go-live; nothing at all before it.
        await db["attendance"].insert_many(
            [
                {
                    "user_id": user_id,
                    "date": day,
                    "status": AttendanceStatus.PRESENT.value,
                    "worked_minutes": 540,
                    "sessions": [],
                    "is_deleted": False,
                }
                for day in working
                if day > GO_LIVE.isoformat()
            ]
        )

        projection = await salary_service.project_for_user(
            user_id, "2026-03", today=date(2026, 4, 1)
        )

        through_go_live = sum(1 for day in working if day <= GO_LIVE.isoformat())
        assert projection["counts"]["untracked_days"] == through_go_live
        assert projection["counts"]["absent_days"] == 0
        assert projection["deductions"] == []
        assert projection["projected_net"] == CTC
