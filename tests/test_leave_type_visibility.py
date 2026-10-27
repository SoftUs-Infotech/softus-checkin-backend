"""Leave types an administrator keeps to themselves.

A type switched off for employees disappears from everything an employee sees
— the list, their balances, the dashboard — and cannot be applied for. It still
accrues, silently, so switching it back on shows a balance that is already
right. Administrators keep seeing every type.
"""

from datetime import date, timedelta

import pytest

from app.core.datetime import org_today
from app.db.mongo import Collections
from app.services import leave as leave_service
from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


async def _types(admin_client, api_prefix) -> tuple[dict, dict]:
    created = []
    for body in (
        {"name": "Casual Leave", "code": "CL"},
        {"name": "Sick Leave", "code": "SL", "visible_to_employees": False},
    ):
        response = await admin_client.post(
            f"{api_prefix}/leave-types",
            json={**body, "accrual": {"frequency": "MONTHLY", "amount_per_cycle": 1}},
        )
        assert response.status_code == 201, response.text
        created.append(response.json()["data"])
    return created[0], created[1]


def _next_wednesday() -> date:
    day = org_today() + timedelta(days=7)
    while day.weekday() != 2:
        day += timedelta(days=1)
    return day


async def test_the_switch_is_saved_and_can_be_turned_back(admin_client, api_prefix):
    _, sick = await _types(admin_client, api_prefix)
    assert sick["visible_to_employees"] is False

    response = await admin_client.patch(
        f"{api_prefix}/leave-types/{sick['id']}", json={"visible_to_employees": True}
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["visible_to_employees"] is True


async def test_new_types_are_visible_by_default(admin_client, api_prefix):
    casual, _ = await _types(admin_client, api_prefix)

    assert casual["visible_to_employees"] is True


async def test_employees_do_not_see_a_hidden_type(admin_client, api_prefix):
    casual, sick = await _types(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")

    async with authed_client(session["tokens"]["access_token"]) as employee:
        listed = await employee.get(f"{api_prefix}/leave-types")
        balances = await employee.get(f"{api_prefix}/leave/balances")
        one = await employee.get(f"{api_prefix}/leave-types/{sick['id']}")
        dashboard = await employee.get(f"{api_prefix}/dashboard/summary")

    assert [item["code"] for item in listed.json()["data"]] == ["CL"]
    assert [item["leave_type_code"] for item in balances.json()["data"]] == ["CL"]
    assert one.status_code == 404
    assert [
        item["leave_type_code"] for item in dashboard.json()["data"]["leave_balances"]
    ] == ["CL"]


async def test_administrators_still_see_every_type(admin_client, api_prefix):
    await _types(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")

    listed = await admin_client.get(f"{api_prefix}/leave-types")
    balances = await admin_client.get(f"{api_prefix}/leave/balances/{session['user']['id']}")

    assert sorted(item["code"] for item in listed.json()["data"]) == ["CL", "SL"]
    rows = {row["leave_type_code"]: row for row in balances.json()["data"]}
    assert rows["SL"]["visible_to_employees"] is False
    assert rows["CL"]["visible_to_employees"] is True


async def test_an_employee_cannot_apply_for_a_hidden_type(admin_client, api_prefix):
    _, sick = await _types(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")
    day = _next_wednesday()

    async with authed_client(session["tokens"]["access_token"]) as employee:
        response = await employee.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": sick["id"],
                "from_date": day.isoformat(),
                "to_date": day.isoformat(),
                "day_part": "FULL",
                "reason": "Feeling unwell",
            },
        )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "LEAVE_TYPE_NOT_FOUND"


async def test_a_visible_type_can_still_be_applied_for(admin_client, api_prefix):
    casual, _ = await _types(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")
    day = _next_wednesday()

    async with authed_client(session["tokens"]["access_token"]) as employee:
        response = await employee.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": casual["id"],
                "from_date": day.isoformat(),
                "to_date": day.isoformat(),
                "day_part": "FULL",
                "reason": "Family function",
            },
        )

    assert response.status_code == 201, response.text


async def test_a_hidden_type_accrues_without_telling_the_employee(
    db, admin_client, api_prefix
):
    _, sick = await _types(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")
    user_id = session["user"]["id"]
    year = org_today().year

    await leave_service.run_accruals(as_of=date(year, 3, 5))

    balance = await db[Collections.LEAVE_BALANCES].find_one(
        {"user_id": user_id, "leave_type_id": sick["id"], "year": year}
    )
    assert balance["allocated"] == 3
    notifications = await db[Collections.NOTIFICATIONS].find({"user_id": user_id}).to_list(
        length=20
    )
    assert notifications  # the visible type was announced…
    assert not any(sick["name"] in item["title"] for item in notifications)  # …this one not
