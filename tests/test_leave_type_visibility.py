"""Leave types an administrator keeps to themselves.

A type switched off for employees disappears from everything an employee sees
— the list, their balances, the dashboard — and cannot be applied for. It still
accrues, silently, so switching it back on shows a balance that is already
right. Administrators keep seeing every type.
"""


import pytest

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
