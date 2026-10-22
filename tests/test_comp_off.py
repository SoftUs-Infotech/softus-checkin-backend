"""Comp-off: who earns one, and when it lapses.

Attendance documents are written straight into the collection here. The credit
rule cares only that somebody has recorded time on the day, and going through
the punch endpoints would tie these tests to today's date.
"""

from datetime import date, timedelta

import pytest

from app.core.datetime import date_key, utc_now
from app.core.exceptions import ConflictError
from app.db.mongo import Collections
from app.models.attendance import AttendanceStatus
from app.models.comp_off import CompOffStatus
from app.services import comp_off as comp_off_service
from app.services.comp_off import effective_status, expiry_date, summarise
from tests.conftest import authed_client, invite_and_accept

WORKED_SATURDAY = date(2026, 3, 14)  # the second Saturday — a working day


# --------------------------------------------------------------------------- #
# expiry, without a database
# --------------------------------------------------------------------------- #
def test_expiry_is_the_earned_date_plus_the_configured_window():
    assert expiry_date(date(2026, 3, 14), 90) == "2026-06-12"


def test_a_zero_window_means_the_credit_never_lapses():
    assert expiry_date(date(2026, 3, 14), 0) is None


def test_a_credit_is_available_until_its_expiry_date_has_passed():
    credit = {"status": CompOffStatus.AVAILABLE, "expires_on": "2026-06-12"}
    assert effective_status(credit, today=date(2026, 6, 12)) == CompOffStatus.AVAILABLE
    assert effective_status(credit, today=date(2026, 6, 13)) == CompOffStatus.EXPIRED


def test_a_credit_without_an_expiry_never_lapses():
    credit = {"status": CompOffStatus.AVAILABLE, "expires_on": None}
    assert effective_status(credit, today=date(2099, 1, 1)) == CompOffStatus.AVAILABLE


def test_a_spent_credit_stays_spent_even_after_its_expiry():
    credit = {"status": CompOffStatus.USED, "expires_on": "2026-06-12"}
    assert effective_status(credit, today=date(2027, 1, 1)) == CompOffStatus.USED


def test_the_summary_counts_each_state_once():
    ledger = [
        {"status": CompOffStatus.AVAILABLE, "expires_on": None},
        {"status": CompOffStatus.AVAILABLE, "expires_on": "2020-01-01"},
        {"status": CompOffStatus.USED, "expires_on": None},
    ]
    assert summarise(ledger, today=date(2026, 3, 14)) == {
        "available": 1,
        "used": 1,
        "expired": 1,
        "total": 3,
    }


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

    async def test_only_the_people_who_worked_the_day_are_credited(
        self, db, admin_client, api_prefix
    ):
        worker, _ = await self._employee(admin_client, api_prefix, "worked@example.com")
        stayer, _ = await self._employee(admin_client, api_prefix, "stayed-home@example.com")
        await self._record_attendance(db, worker, WORKED_SATURDAY, worked_minutes=540)
        await self._record_attendance(db, stayer, WORKED_SATURDAY, worked_minutes=0)

        response = await admin_client.post(
            f"{api_prefix}/work-calendar/overrides",
            json={
                "date": date_key(WORKED_SATURDAY),
                "kind": "OFF",
                "reason": "Declared off after the fact.",
            },
        )

        assert response.status_code == 201, response.text
        assert response.json()["data"]["comp_offs_credited"] == 1
        inside_window = WORKED_SATURDAY + timedelta(days=1)
        assert await comp_off_service.balance(worker, today=inside_window) == {
            "user_id": worker,
            "available": 1,
            "used": 0,
            "expired": 0,
            "total": 1,
        }
        assert (await comp_off_service.balance(stayer))["total"] == 0

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

    async def test_opening_a_day_credits_nobody(self, db, admin_client, api_prefix):
        """A day that was already off cannot have been taken away from anybody."""
        worker, _ = await self._employee(admin_client, api_prefix, "sunday@example.com")
        await self._record_attendance(db, worker, date(2026, 3, 15), worked_minutes=300)

        response = await admin_client.post(
            f"{api_prefix}/work-calendar/overrides",
            json={"date": "2026-03-15", "kind": "WORKING", "reason": "Release weekend."},
        )
        assert response.json()["data"]["comp_offs_credited"] == 0

    async def test_ruling_the_same_day_off_twice_credits_once(
        self, db, admin_client, api_prefix
    ):
        worker, _ = await self._employee(admin_client, api_prefix, "twice@example.com")
        await self._record_attendance(db, worker, WORKED_SATURDAY, worked_minutes=540)

        payload = {
            "date": date_key(WORKED_SATURDAY),
            "kind": "OFF",
            "reason": "Declared off after the fact.",
        }
        await admin_client.post(f"{api_prefix}/work-calendar/overrides", json=payload)
        second = await admin_client.post(f"{api_prefix}/work-calendar/overrides", json=payload)

        assert second.json()["data"]["comp_offs_credited"] == 0
        assert (await comp_off_service.balance(worker))["total"] == 1

    async def test_a_credit_that_never_expires_stays_available(
        self, db, admin_client, api_prefix
    ):
        await admin_client.patch(f"{api_prefix}/settings", json={"comp_off_expiry_days": 0})
        worker, _ = await self._employee(admin_client, api_prefix, "forever@example.com")
        await self._record_attendance(db, worker, WORKED_SATURDAY, worked_minutes=540)

        await admin_client.post(
            f"{api_prefix}/work-calendar/overrides",
            json={
                "date": date_key(WORKED_SATURDAY),
                "kind": "OFF",
                "reason": "Declared off after the fact.",
            },
        )

        credits = await comp_off_service.list_for_user(worker)
        assert credits[0]["expires_on"] is None
        assert credits[0]["status"] == CompOffStatus.AVAILABLE

    async def test_a_recent_credit_is_still_available(self, db, admin_client, api_prefix):
        """The same rule, read at a date inside the window rather than years later."""
        worker, _ = await self._employee(admin_client, api_prefix, "recent@example.com")
        await self._record_attendance(db, worker, WORKED_SATURDAY, worked_minutes=540)
        await admin_client.post(
            f"{api_prefix}/work-calendar/overrides",
            json={
                "date": date_key(WORKED_SATURDAY),
                "kind": "OFF",
                "reason": "Declared off after the fact.",
            },
        )

        inside_window = WORKED_SATURDAY + timedelta(days=30)
        balance = await comp_off_service.balance(worker, today=inside_window)
        assert balance["available"] == 1
        assert balance["expired"] == 0

    async def test_a_spent_credit_leaves_the_balance(self, db, admin_client, api_prefix):
        worker, _ = await self._employee(admin_client, api_prefix, "spender@example.com")
        await self._record_attendance(db, worker, WORKED_SATURDAY, worked_minutes=540)
        await admin_client.post(
            f"{api_prefix}/work-calendar/overrides",
            json={
                "date": date_key(WORKED_SATURDAY),
                "kind": "OFF",
                "reason": "Declared off after the fact.",
            },
        )
        credit = (await comp_off_service.list_for_user(worker))[0]

        spent_on = WORKED_SATURDAY + timedelta(days=7)
        await comp_off_service.spend(credit["id"], used_on=spent_on)

        balance = await comp_off_service.balance(worker, today=spent_on)
        assert balance["available"] == 0
        assert balance["used"] == 1

    async def test_an_expired_credit_cannot_be_spent(self, db, admin_client, api_prefix):
        worker, _ = await self._employee(admin_client, api_prefix, "too-late@example.com")
        await self._record_attendance(db, worker, WORKED_SATURDAY, worked_minutes=540)
        await admin_client.post(
            f"{api_prefix}/work-calendar/overrides",
            json={
                "date": date_key(WORKED_SATURDAY),
                "kind": "OFF",
                "reason": "Declared off after the fact.",
            },
        )
        credit = (await comp_off_service.list_for_user(worker))[0]

        with pytest.raises(ConflictError):
            await comp_off_service.spend(credit["id"], used_on=date(2027, 1, 1))

    async def test_an_employee_cannot_read_somebody_elses_ledger(
        self, admin_client, api_prefix
    ):
        _, token = await self._employee(admin_client, api_prefix, "nosy@example.com")
        other, _ = await self._employee(admin_client, api_prefix, "private@example.com")

        client = authed_client(token)
        async with client:
            response = await client.get(
                f"{api_prefix}/work-calendar/comp-offs", params={"user_id": other}
            )
        assert response.status_code == 403
