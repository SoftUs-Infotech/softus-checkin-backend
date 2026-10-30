"""Shift definitions, defaults, assignment and deletion guards."""

import pytest

from tests.conftest import invite_and_accept

pytestmark = pytest.mark.usefixtures("db")

GENERAL = {
    "name": "General Shift",
    "code": "general",
    "start_time": "09:30",
    "end_time": "18:30",
    "required_minutes": 540,
    "half_day_minutes": 240,
}


async def _create(admin_client, api_prefix, **overrides):
    return await admin_client.post(f"{api_prefix}/shifts", json={**GENERAL, **overrides})


async def test_a_shift_with_employees_on_it_cannot_be_deleted(admin_client, api_prefix):
    await _create(admin_client, api_prefix)  # default, keeps the guard below honest
    other = (await _create(admin_client, api_prefix, code="NIGHT", name="Night")).json()["data"]
    session = await invite_and_accept(admin_client, api_prefix, email="busy@example.com")
    await admin_client.post(
        f"{api_prefix}/shifts/{other['id']}/assign", json={"user_ids": [session["user"]["id"]]}
    )

    response = await admin_client.delete(f"{api_prefix}/shifts/{other['id']}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SHIFT_IN_USE"
    assert response.json()["error"]["details"]["assigned_count"] == 1
