"""Leave restarts with attendance on go-live day.

The administrator presses "reset" the day the system goes live. Before this,
balances were built as if everybody had been earning since joining — an
employee who joined years ago opened the app to ten months of monthly leave
nobody had tracked. After it, every balance is zero on go-live day and accrues
from there: nothing earned, carried over or taken before the day counts.

The suite runs against the real date, because the reset refuses a future day.
"""

from datetime import date

import pytest

from app.core.datetime import org_today
from tests.conftest import invite_and_accept

pytestmark = pytest.mark.usefixtures("db")

TODAY = org_today()
YEAR = TODAY.year
MONTH_START = TODAY.replace(day=1)


async def _leave_type(admin_client, api_prefix, **overrides) -> dict:
    body = {"name": "Casual Leave", "code": "CL", **overrides}
    response = await admin_client.post(f"{api_prefix}/leave-types", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _reset(admin_client, api_prefix, day: date) -> None:
    response = await admin_client.post(
        f"{api_prefix}/settings/tracking-start", json={"starts_on": day.isoformat()}
    )
    assert response.status_code == 200, response.text


async def _balance(admin_client, api_prefix, user_id: str, leave_type_id: str) -> dict:
    response = await admin_client.get(f"{api_prefix}/leave/balances/{user_id}")
    assert response.status_code == 200, response.text
    return next(row for row in response.json()["data"] if row["leave_type_id"] == leave_type_id)


# --------------------------------------------------------------------------- #
# the reset
# --------------------------------------------------------------------------- #
async def test_monthly_leave_earned_before_go_live_is_wiped(admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client, api_prefix, accrual={"frequency": "MONTHLY", "amount_per_cycle": 1}
    )
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")
    user_id = session["user"]["id"]
    before = await _balance(admin_client, api_prefix, user_id, leave_type["id"])
    assert before["allocated"] == TODAY.month  # every month since January

    await _reset(admin_client, api_prefix, MONTH_START)

    after = await _balance(admin_client, api_prefix, user_id, leave_type["id"])
    assert after["allocated"] == 1  # this month only
    assert after["available"] == 1
