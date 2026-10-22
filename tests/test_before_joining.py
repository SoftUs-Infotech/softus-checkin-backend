"""Days before somebody joined are nobody's absence.

A mid-month joiner used to read as absent for every earlier working day, and
the salary projection docked them a day's pay for each one — a new hire on the
16th lost half a month twice over. March 2026 is the month: 24 working days
under the default pattern and a CTC of 24,000, so one working day is 1,000.
"""

from datetime import UTC, date, datetime

import pytest

from app.db.mongo import Collections
from app.models.attendance import AttendanceStatus
from app.models.settings import SalaryDayBasis
from app.services import salary as salary_service
from app.services.attendance_rules import compute_day_status, employment_start
from app.services.salary import DayCounts, deduction_lines
from app.services.work_calendar import decide_range
from tests.conftest import invite_and_accept

SHIFT = {"required_minutes": 540, "half_day_minutes": 240, "working_weekdays": [0, 1, 2, 3, 4]}
MONDAY = date(2026, 3, 2)
TODAY = date(2026, 3, 10)
NOW = datetime(2026, 3, 10, 8, 0, tzinfo=UTC)

CTC = 24_000.0
DAY_RATE = 1_000.0
JOINED = date(2026, 3, 16)


# --------------------------------------------------------------------------- #
# the rules, without a database
# --------------------------------------------------------------------------- #
def test_an_unpunched_day_before_joining_is_not_an_absence():
    result = compute_day_status(MONDAY, [], SHIFT, today=TODAY, now=NOW, employed_from=TODAY)

    assert result.status is AttendanceStatus.BEFORE_JOINING


def test_a_day_worked_before_the_recorded_joining_date_is_still_judged():
    # A joining date typed a week late must not erase work that was punched.
    sessions = [
        {
            "punch_in": {"at": datetime(2026, 3, 2, 4, 0, tzinfo=UTC)},
            "punch_out": {"at": datetime(2026, 3, 2, 13, 0, tzinfo=UTC)},
        }
    ]

    result = compute_day_status(
        MONDAY, sessions, SHIFT, today=TODAY, now=NOW, employed_from=TODAY
    )

    assert result.status is AttendanceStatus.PRESENT


def test_from_the_joining_day_on_a_missing_day_is_an_absence_again():
    result = compute_day_status(MONDAY, [], SHIFT, today=TODAY, now=NOW, employed_from=MONDAY)

    assert result.status is AttendanceStatus.ABSENT


def test_the_joining_date_wins_over_the_day_the_account_was_activated():
    user = {
        "joining_date": "2026-03-16",
        "activated_at": datetime(2026, 3, 2, 6, 0, tzinfo=UTC),
    }

    assert employment_start(user) == JOINED


def test_without_a_joining_date_the_activation_day_is_the_start_in_office_time():
    # 20:00 UTC on the 1st is already the 2nd in Asia/Kolkata.
    user = {"activated_at": datetime(2026, 3, 1, 20, 0, tzinfo=UTC)}

    assert employment_start(user) == date(2026, 3, 2)


def test_an_account_with_neither_date_has_no_lower_bound():
    assert employment_start({}) is None
    assert employment_start(None) is None


# --------------------------------------------------------------------------- #
# the deduction line
# --------------------------------------------------------------------------- #
def test_a_working_day_basis_prorates_by_working_days():
    counts = DayCounts(working_days=24, before_joining_days=11, before_joining_calendar_days=15)

    lines = deduction_lines(
        counts, day_rate=DAY_RATE, flags={"salary_day_basis": SalaryDayBasis.WORKING_DAYS}
    )

    assert lines == [
        {"kind": "BEFORE_JOINING", "label": "Before joining", "days": 11, "amount": 11_000.0}
    ]


def test_a_calendar_day_basis_prorates_by_calendar_days():
    counts = DayCounts(working_days=24, before_joining_days=11, before_joining_calendar_days=15)

    lines = deduction_lines(
        counts, day_rate=DAY_RATE, flags={"salary_day_basis": SalaryDayBasis.CALENDAR_DAYS}
    )

    assert [(line["kind"], line["days"]) for line in lines] == [("BEFORE_JOINING", 15)]


def test_proration_cannot_be_switched_off_with_the_absence_rule():
    counts = DayCounts(working_days=24, before_joining_days=3)

    lines = deduction_lines(counts, day_rate=DAY_RATE, flags={"salary_deduct_absent": False})

    assert [line["kind"] for line in lines] == ["BEFORE_JOINING"]


# --------------------------------------------------------------------------- #
# against real documents
# --------------------------------------------------------------------------- #
class TestMidMonthJoiner:
    pytestmark = pytest.mark.usefixtures("db")

    async def _joiner(self, admin_client, api_prefix):
        session = await invite_and_accept(
            admin_client, api_prefix, email="joiner@example.com", joining_date=JOINED.isoformat()
        )
        user_id = session["user"]["id"]
        response = await admin_client.post(
            f"{api_prefix}/salary/employees/{user_id}",
            json={"monthly_ctc": CTC, "effective_from": "2026-03-01"},
        )
        assert response.status_code == 201, response.text
        return user_id

    @staticmethod
    def _working_days(start: date, end: date) -> list[str]:
        return [
            decision.date
            for decision in decide_range(
                start, end, shift={"working_weekdays": [0, 1, 2, 3, 4]}, working_saturdays=[2, 4]
            )
            if decision.is_working
        ]

    async def test_the_month_is_prorated_and_nothing_reads_as_absent(
        self, db, admin_client, api_prefix
    ):
        user_id = await self._joiner(admin_client, api_prefix)
        worked = self._working_days(JOINED, date(2026, 3, 31))
        await db[Collections.ATTENDANCE].insert_many(
            [
                {
                    "user_id": user_id,
                    "date": day,
                    "status": AttendanceStatus.PRESENT.value,
                    "worked_minutes": 540,
                    "sessions": [],
                    "is_deleted": False,
                }
                for day in worked
            ]
        )
        before = len(self._working_days(date(2026, 3, 1), date(2026, 3, 15)))

        projection = await salary_service.project_for_user(
            user_id, "2026-03", today=date(2026, 4, 1)
        )

        assert projection["counts"]["absent_days"] == 0
        assert projection["counts"]["before_joining_days"] == before
        assert projection["counts"]["working_days"] == before + len(worked)
        assert projection["projected_net"] == CTC - before * DAY_RATE

    async def test_a_joiner_later_this_month_is_not_projected_a_full_month(
        self, db, admin_client, api_prefix
    ):
        user_id = await self._joiner(admin_client, api_prefix)
        before = len(self._working_days(date(2026, 3, 1), date(2026, 3, 15)))

        # The 10th: the joining date is still ahead, so those days are pending
        # for everybody else — but never for somebody who has not started.
        projection = await salary_service.project_for_user(
            user_id, "2026-03", today=date(2026, 3, 10)
        )

        assert projection["counts"]["before_joining_days"] == before
        assert projection["projected_net"] == CTC - before * DAY_RATE

    async def test_the_calendar_names_the_days_and_leaves_them_out_of_the_count(
        self, admin_client, api_prefix
    ):
        user_id = await self._joiner(admin_client, api_prefix)

        response = await admin_client.get(
            f"{api_prefix}/attendance/calendar",
            params={"year": 2026, "month": 3, "user_id": user_id},
        )
        assert response.status_code == 200, response.text
        body = response.json()["data"]
        statuses = {day["date"]: day["status"] for day in body["days"]}

        assert statuses["2026-03-02"] == AttendanceStatus.BEFORE_JOINING
        assert statuses["2026-03-13"] == AttendanceStatus.BEFORE_JOINING
        assert AttendanceStatus.ABSENT not in {
            status for day, status in statuses.items() if day < JOINED.isoformat()
        }
        assert body["labels"]["BEFORE_JOINING"] == "Before joining"
