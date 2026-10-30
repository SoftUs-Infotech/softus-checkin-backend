"""Comp-off: who earns one, and when it lapses.

Attendance documents are written straight into the collection here. The credit
rule cares only that somebody has recorded time on the day, and going through
the punch endpoints would tie these tests to today's date.
"""

from datetime import date

import pytest

from app.core.datetime import date_key, utc_now
from app.db.mongo import Collections
from app.models.attendance import AttendanceStatus
from app.models.comp_off import CompOffStatus
from tests.conftest import authed_client, invite_and_accept

WORKED_SATURDAY = date(2026, 3, 14)  # the second Saturday — a working day


# --------------------------------------------------------------------------- #
# crediting
# --------------------------------------------------------------------------- #
class TestCrediting:
    pytestmark = pytest.mark.usefixtures("db")

    async def _employee(self, admin_client, api_prefix, email):
        session = await invite_and_accept(admin_client, api_prefix, email=email)
        return session["user"]["id"], session["tokens"]["access_token"]

    async def _record_attendance(self, db, user_id, day, *, worked_minutes):
        await db[Collections.ATTENDANCE].insert_one(
            {
                "user_id": user_id,
                "date": date_key(day),
                "worked_minutes": worked_minutes,
                "status": (
                    AttendanceStatus.PRESENT.value
                    if worked_minutes
                    else AttendanceStatus.ABSENT.value
                ),
                "sessions": [],
                "is_deleted": False,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )


    async def test_a_swap_credits_the_day_it_takes_away(self, db, admin_client, api_prefix):
        worker, token = await self._employee(admin_client, api_prefix, "swapped@example.com")
        await self._record_attendance(db, worker, WORKED_SATURDAY, worked_minutes=500)

        response = await admin_client.post(
            f"{api_prefix}/work-calendar/swaps",
            json={
                "off_date": "2026-03-14",
                "working_date": "2026-03-21",
                "reason": "Third Saturday off instead of the second.",
            },
        )
        assert response.json()["data"]["comp_offs_credited"] == 1

        client = authed_client(token)
        async with client:
            ledger = await client.get(f"{api_prefix}/work-calendar/comp-offs")
        credit = ledger.json()["data"][0]
        assert credit["earned_on"] == "2026-03-14"
        assert credit["expires_on"] == "2026-06-12"
        assert credit["status"] == CompOffStatus.EXPIRED  # 90 days on from March 2026
