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
from app.services import salary as salary_service
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
