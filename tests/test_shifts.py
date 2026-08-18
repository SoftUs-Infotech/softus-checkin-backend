"""Shift definitions, defaults, assignment and deletion guards."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
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


async def _fresh_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create(admin_client, api_prefix, **overrides):
    return await admin_client.post(f"{api_prefix}/shifts", json={**GENERAL, **overrides})


async def test_creating_a_shift_normalises_the_code_and_names_the_weekdays(
    admin_client, api_prefix
):
    response = await _create(admin_client, api_prefix, code="night shift")

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["code"] == "NIGHT_SHIFT"
    assert data["working_weekday_names"] == [
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
    ]


async def test_the_first_shift_becomes_the_default_even_if_not_asked_for(
    admin_client, api_prefix
):
    response = await _create(admin_client, api_prefix, is_default=False)

    assert response.json()["data"]["is_default"] is True


async def test_promoting_a_new_default_demotes_the_previous_one(admin_client, api_prefix):
    first = (await _create(admin_client, api_prefix)).json()["data"]
    second = (await _create(admin_client, api_prefix, code="NIGHT", name="Night Shift")).json()["data"]

    await admin_client.patch(f"{api_prefix}/shifts/{second['id']}", json={"is_default": True})

    listed = (await admin_client.get(f"{api_prefix}/shifts")).json()["data"]
    defaults = [shift["id"] for shift in listed if shift["is_default"]]
    assert defaults == [second["id"]]
    assert first["id"] not in defaults


async def test_a_night_shift_is_flagged_as_crossing_midnight(admin_client, api_prefix):
    response = await _create(
        admin_client, api_prefix, code="NIGHT", start_time="22:00", end_time="06:00"
    )

    assert response.json()["data"]["crosses_midnight"] is True


async def test_half_day_hours_must_be_below_a_full_day(admin_client, api_prefix):
    response = await _create(
        admin_client, api_prefix, required_minutes=480, half_day_minutes=480
    )

    assert response.status_code == 422


async def test_an_update_cannot_push_half_day_past_a_full_day(admin_client, api_prefix):
    shift = (await _create(admin_client, api_prefix)).json()["data"]

    response = await admin_client.patch(
        f"{api_prefix}/shifts/{shift['id']}", json={"half_day_minutes": 600}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_SHIFT_HOURS"


async def test_a_shift_needs_at_least_one_working_weekday(admin_client, api_prefix):
    response = await _create(admin_client, api_prefix, working_weekdays=[])

    assert response.status_code == 422


async def test_duplicate_weekdays_are_collapsed(admin_client, api_prefix):
    response = await _create(admin_client, api_prefix, working_weekdays=[0, 0, 2, 1])

    assert response.json()["data"]["working_weekdays"] == [0, 1, 2]


async def test_shift_codes_are_unique(admin_client, api_prefix):
    await _create(admin_client, api_prefix)

    duplicate = await _create(admin_client, api_prefix, name="Another")

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DUPLICATE_SHIFT_CODE"


async def test_employees_can_be_assigned_to_a_shift(admin_client, api_prefix):
    shift = (await _create(admin_client, api_prefix)).json()["data"]
    session = await invite_and_accept(admin_client, api_prefix, email="shifted@example.com")

    response = await admin_client.post(
        f"{api_prefix}/shifts/{shift['id']}/assign",
        json={"user_ids": [session["user"]["id"]]},
    )

    assert response.status_code == 200
    assert response.json()["data"]["assigned"] == 1

    detail = await admin_client.get(f"{api_prefix}/shifts/{shift['id']}")
    assert detail.json()["data"]["assigned_count"] == 1


async def test_assigning_an_unknown_employee_fails(admin_client, api_prefix):
    shift = (await _create(admin_client, api_prefix)).json()["data"]

    response = await admin_client.post(
        f"{api_prefix}/shifts/{shift['id']}/assign",
        json={"user_ids": ["507f1f77bcf86cd799439011"]},
    )

    assert response.status_code == 404


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


async def test_the_default_shift_cannot_be_deleted_or_deactivated(admin_client, api_prefix):
    shift = (await _create(admin_client, api_prefix)).json()["data"]

    deleted = await admin_client.delete(f"{api_prefix}/shifts/{shift['id']}")
    assert deleted.status_code == 422
    assert deleted.json()["error"]["code"] == "CANNOT_DELETE_DEFAULT"

    deactivated = await admin_client.patch(
        f"{api_prefix}/shifts/{shift['id']}", json={"is_active": False}
    )
    assert deactivated.status_code == 422


async def test_an_unused_shift_can_be_deleted(admin_client, api_prefix):
    await _create(admin_client, api_prefix)
    spare = (await _create(admin_client, api_prefix, code="SPARE", name="Spare")).json()["data"]

    assert (await admin_client.delete(f"{api_prefix}/shifts/{spare['id']}")).status_code == 200
    assert (await admin_client.get(f"{api_prefix}/shifts/{spare['id']}")).status_code == 404


async def test_employees_may_read_shifts_but_not_change_them(admin_client, api_prefix):
    await _create(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="reader@example.com")

    async with await _fresh_client() as employee:
        employee.headers["Authorization"] = f"Bearer {session['tokens']['access_token']}"
        assert (await employee.get(f"{api_prefix}/shifts")).status_code == 200
        assert (await employee.post(f"{api_prefix}/shifts", json=GENERAL)).status_code == 403
