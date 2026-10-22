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
from app.services.attendance_rules import compute_day_status
from app.services.audit import SYSTEM
from app.services.work_calendar import decide_range
from tests.conftest import authed_client, invite_and_accept

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


def judge(day: date, sessions: list[dict], *, today: date, now: datetime | None = None, **kw):
    return compute_day_status(
        day,
        sessions,
        SHIFT,
        today=today,
        now=now or local(today, 23, 0),
        tracking_from=GO_LIVE,
        **kw,
    )


# --------------------------------------------------------------------------- #
# the rules, without a database
# --------------------------------------------------------------------------- #
def test_nothing_before_go_live_counts_not_even_test_punches():
    result = judge(BEFORE, [session(BEFORE, (11, 0), (12, 0))], today=DAY_AFTER)

    assert result.status is AttendanceStatus.NOT_TRACKED


def test_hours_punched_before_go_live_do_not_reach_the_month_total():
    result = judge(BEFORE, [session(BEFORE, (10, 0), (19, 15))], today=DAY_AFTER)

    assert (result.worked_minutes, result.expected_minutes) == (0, 0)


def test_an_unpunched_day_before_go_live_is_not_an_absence():
    assert judge(BEFORE, [], today=DAY_AFTER).status is AttendanceStatus.NOT_TRACKED


def test_on_go_live_a_late_short_day_is_simply_present():
    # Arrived at 14:00 — five hours late — and worked four hours.
    result = judge(GO_LIVE, [session(GO_LIVE, (14, 0), (18, 0))], today=DAY_AFTER)

    assert result.status is AttendanceStatus.PRESENT
    assert result.is_late is False


def test_on_go_live_somebody_who_never_punched_is_not_absent():
    assert judge(GO_LIVE, [], today=DAY_AFTER).status is AttendanceStatus.NOT_TRACKED


def test_on_go_live_itself_the_day_is_live_until_it_ends():
    now = local(GO_LIVE, 15, 0)

    working = judge(GO_LIVE, [session(GO_LIVE, (14, 0), None)], today=GO_LIVE, now=now)
    waiting = judge(GO_LIVE, [], today=GO_LIVE, now=now)

    assert working.status is AttendanceStatus.IN_PROGRESS
    assert waiting.status is AttendanceStatus.NOT_STARTED


def test_a_holiday_on_go_live_is_still_a_holiday():
    result = judge(GO_LIVE, [], today=DAY_AFTER, holiday={"name": "Founders' Day"})

    assert result.status is AttendanceStatus.HOLIDAY


def test_from_the_next_day_the_ordinary_rules_apply():
    absent = judge(DAY_AFTER, [], today=date(2026, 3, 18))
    late = judge(DAY_AFTER, [session(DAY_AFTER, (11, 0), (20, 0))], today=date(2026, 3, 18))

    assert absent.status is AttendanceStatus.ABSENT
    assert late.is_late is True


def test_without_a_start_date_nothing_changes():
    result = compute_day_status(
        BEFORE, [], SHIFT, today=DAY_AFTER, now=local(DAY_AFTER, 12, 0)
    )

    assert result.status is AttendanceStatus.ABSENT


# --------------------------------------------------------------------------- #
# the reset itself
# --------------------------------------------------------------------------- #
class TestReset:
    pytestmark = pytest.mark.usefixtures("db")

    async def test_resetting_with_no_date_starts_today(self, admin_client, api_prefix):
        from app.core.datetime import date_key, org_today

        response = await admin_client.post(f"{api_prefix}/settings/tracking-start", json={})

        assert response.status_code == 200, response.text
        assert response.json()["data"]["tracking_starts_on"] == date_key(org_today())
        settings = await admin_client.get(f"{api_prefix}/settings")
        assert settings.json()["data"]["tracking_starts_on"] == date_key(org_today())

    async def test_a_start_in_the_future_is_refused(self, admin_client, api_prefix):
        from app.core.datetime import date_key, org_today

        response = await admin_client.post(
            f"{api_prefix}/settings/tracking-start",
            json={"starts_on": date_key(org_today() + timedelta(days=1))},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "TRACKING_START_IN_FUTURE"

    async def test_only_an_administrator_can_reset(self, admin_client, api_prefix):
        session = await invite_and_accept(admin_client, api_prefix, email="emp@example.com")

        async with authed_client(session["tokens"]["access_token"]) as employee:
            response = await employee.post(f"{api_prefix}/settings/tracking-start", json={})

        assert response.status_code == 403

    async def test_the_reset_is_on_the_audit_trail(self, admin_client, api_prefix):
        await admin_client.post(
            f"{api_prefix}/settings/tracking-start", json={"starts_on": GO_LIVE.isoformat()}
        )

        logs = await admin_client.get(f"{api_prefix}/audit-logs", params={"entity": "SETTINGS"})

        entries = logs.json()["data"]["items"]
        assert any(entry["entity_label"] == "Attendance tracking start" for entry in entries)

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

    async def test_no_correction_for_a_day_before_go_live(self, admin_client, api_prefix):
        from app.core.datetime import org_today

        session = await invite_and_accept(admin_client, api_prefix, email="late@example.com")
        start = org_today() - timedelta(days=1)
        await admin_client.post(
            f"{api_prefix}/settings/tracking-start", json={"starts_on": start.isoformat()}
        )
        day = start - timedelta(days=1)

        async with authed_client(session["tokens"]["access_token"]) as employee:
            response = await employee.post(
                f"{api_prefix}/corrections",
                json={
                    "date": day.isoformat(),
                    "type": "FULL_DAY",
                    "requested_punch_in": f"{day.isoformat()}T04:00:00Z",
                    "requested_punch_out": f"{day.isoformat()}T13:00:00Z",
                    "reason": "Forgot to punch before the system existed",
                },
            )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "BEFORE_TRACKING_START"

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
