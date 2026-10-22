"""Who the comp-off ledger is for.

The work calendar shows the ledger beside the month because closing a day is
what creates the credits. It was reading the administrator's own ledger, which
is empty by construction — an administrator does not punch, so they never work
the day that later gets given back. The card therefore said "nobody has earned a
comp-off yet" on an organisation full of them.
"""

from datetime import date

import pytest

from app.core.exceptions import PermissionDeniedError
from app.db.mongo import Collections
from app.models.comp_off import CompOffStatus
from app.services import comp_off as comp_off_service

TODAY = date(2026, 8, 20)


async def credit(db, user_id: str, *, earned_on: str, expires_on: str | None) -> None:
    await db[Collections.COMP_OFFS].insert_one(
        {
            "user_id": user_id,
            "user_email": f"{user_id}@example.com",
            "earned_on": earned_on,
            "reason": "Third Saturday given back.",
            "expires_on": expires_on,
            "status": CompOffStatus.AVAILABLE.value,
            "is_deleted": False,
        }
    )


async def test_the_organisation_ledger_shows_credits_the_reader_did_not_earn(db, admin):
    await credit(db, "someone-else", earned_on="2026-08-08", expires_on=None)

    ledger = await comp_off_service.list_for_org(today=TODAY)

    assert [row["user_id"] for row in ledger] == ["someone-else"]


async def test_the_row_names_somebody(db, admin):
    await credit(db, "someone-else", earned_on="2026-08-08", expires_on=None)

    row = (await comp_off_service.list_for_org(today=TODAY))[0]

    assert row["user_email"] == "someone-else@example.com"


async def test_an_expired_credit_reads_as_expired(db, admin):
    await credit(db, "someone-else", earned_on="2026-01-10", expires_on="2026-04-10")

    row = (await comp_off_service.list_for_org(today=TODAY))[0]

    assert row["status"] == CompOffStatus.EXPIRED


async def test_available_only_hides_a_lapsed_credit(db, admin):
    await credit(db, "someone-else", earned_on="2026-01-10", expires_on="2026-04-10")

    assert await comp_off_service.list_for_org(include_spent=False, today=TODAY) == []


async def test_newest_first(db, admin):
    await credit(db, "a", earned_on="2026-08-01", expires_on=None)
    await credit(db, "b", earned_on="2026-08-15", expires_on=None)

    ledger = await comp_off_service.list_for_org(today=TODAY)

    assert [row["earned_on"] for row in ledger] == ["2026-08-15", "2026-08-01"]


def test_an_employee_may_not_read_the_organisation_ledger():
    with pytest.raises(PermissionDeniedError):
        comp_off_service.require_org_ledger_access({"id": "x", "role": "EMPLOYEE"})


def test_a_manager_may_not_either():
    """A manager approves for their reports; the ledger is org-wide."""
    with pytest.raises(PermissionDeniedError):
        comp_off_service.require_org_ledger_access({"id": "x", "role": "MANAGER"})


def test_an_administrator_may():
    comp_off_service.require_org_ledger_access({"id": "x", "role": "ADMIN"})
