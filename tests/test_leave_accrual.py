"""Leave accrual: what a policy credits, when, and only once."""

from datetime import date

import pytest

from app.core.datetime import org_today
from app.db.mongo import Collections
from app.services import leave as leave_service
from app.services import leave_type as leave_type_service

pytestmark = pytest.mark.usefixtures("db")

YEAR = org_today().year


async def _leave_type(admin_client, api_prefix, **overrides) -> dict:
    body = {"name": "Sick Leave", "code": "SL", **overrides}
    response = await admin_client.post(f"{api_prefix}/leave-types", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _employee(admin_client, api_prefix, email, *, joining_date=None) -> str:
    """Invite and accept, optionally recording a joining date. Returns the id."""
    invite = await admin_client.post(
        f"{api_prefix}/auth/invite",
        json={
            "email": email,
            "first_name": "Ada",
            "role": "EMPLOYEE",
            **({"joining_date": joining_date.isoformat()} if joining_date else {}),
        },
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["data"]["invite_url"].split("token=")[1]

    accepted = await admin_client.post(
        f"{api_prefix}/auth/accept-invite",
        json={"token": token, "password": "Employee123", "first_name": "Ada",
              "last_name": "L"},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()["data"]["user"]["id"]


async def _balance(db, user_id: str, leave_type_id: str, year: int = YEAR) -> dict:
    return await db[Collections.LEAVE_BALANCES].find_one(
        {"user_id": user_id, "leave_type_id": leave_type_id, "year": year}
    )


# --------------------------------------------------------------------------- #
# idempotency and proration
# --------------------------------------------------------------------------- #
async def test_running_the_job_twice_credits_once(db, admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "MONTHLY", "amount_per_cycle": 1},
    )
    user_id = await _employee(admin_client, api_prefix, "twice@example.com")

    first = await leave_service.run_accruals(as_of=date(YEAR, 5, 9))
    second = await leave_service.run_accruals(as_of=date(YEAR, 5, 9))

    balance = await _balance(db, user_id, leave_type["id"])
    assert first == 2 and second == 0  # the administrator accrues too
    assert balance["allocated"] == 5
    assert len(balance["accrual_credits"]) == 5


# --------------------------------------------------------------------------- #
# changing how often a type credits, mid-year
# --------------------------------------------------------------------------- #
async def test_yearly_to_monthly_mid_year_does_not_pay_the_months_twice(
    db, admin_client, api_prefix
):
    leave_type = await _leave_type(
        admin_client, api_prefix,
        accrual={"frequency": "YEARLY", "amount_per_cycle": 12},
    )
    user_id = await _employee(admin_client, api_prefix, "switch@example.com")
    await leave_service.run_accruals(as_of=date(YEAR, 3, 1))

    await admin_client.patch(
        f"{api_prefix}/leave-types/{leave_type['id']}",
        json={"accrual": {"frequency": "MONTHLY", "amount_per_cycle": 1}},
    )
    changed = await leave_type_service.require_leave_type(leave_type["id"])
    await leave_service.run_accruals(as_of=date(YEAR, 10, 5))
    await leave_service.apply_due_credits(
        {"id": user_id}, changed, YEAR, as_of=date(YEAR, 10, 5)
    )

    # The January credit already paid for the whole year.
    assert (await _balance(db, user_id, leave_type["id"]))["allocated"] == 12
