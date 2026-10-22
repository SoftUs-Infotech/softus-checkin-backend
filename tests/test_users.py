"""Employee administration: listing, updating, suspending, removing."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.conftest import invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


async def _fresh_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_listing_employees_is_paginated(admin_client, api_prefix):
    for index in range(3):
        await admin_client.post(
            f"{api_prefix}/auth/invite",
            json={"email": f"person{index}@example.com", "first_name": f"P{index}"},
        )

    response = await admin_client.get(f"{api_prefix}/users", params={"page_size": 2})

    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["items"]) == 2
    assert data["pagination"]["total"] == 4  # three invitees plus the admin
    assert data["pagination"]["has_next"] is True


async def test_employees_can_be_searched_and_filtered(admin_client, api_prefix):
    await admin_client.post(
        f"{api_prefix}/auth/invite",
        json={"email": "grace@example.com", "first_name": "Grace", "department": "QA"},
    )

    by_name = await admin_client.get(f"{api_prefix}/users", params={"search": "grace"})
    assert by_name.json()["data"]["pagination"]["total"] == 1

    by_role = await admin_client.get(f"{api_prefix}/users", params={"role": "ADMIN"})
    assert by_role.json()["data"]["pagination"]["total"] == 1

    by_status = await admin_client.get(f"{api_prefix}/users", params={"status": "INVITED"})
    assert by_status.json()["data"]["pagination"]["total"] == 1


async def test_an_employee_cannot_list_employees(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="nosy@example.com")

    async with await _fresh_client() as employee:
        employee.headers["Authorization"] = f"Bearer {session['tokens']['access_token']}"
        response = await employee.get(f"{api_prefix}/users")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_an_administrator_can_update_an_employee(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="edit@example.com")
    user_id = session["user"]["id"]

    response = await admin_client.patch(
        f"{api_prefix}/users/{user_id}",
        json={"designation": "Senior Engineer", "department": "Platform"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["designation"] == "Senior Engineer"


async def test_an_employee_cannot_be_made_their_own_manager(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="loop@example.com")
    user_id = session["user"]["id"]

    response = await admin_client.patch(
        f"{api_prefix}/users/{user_id}", json={"reporting_manager_id": user_id}
    )

    assert response.status_code == 422


async def test_updating_an_unknown_employee_is_a_404(admin_client, api_prefix):
    response = await admin_client.patch(
        f"{api_prefix}/users/507f1f77bcf86cd799439011", json={"designation": "Ghost"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


async def test_suspending_an_employee_blocks_sign_in_immediately(admin_client, api_prefix):
    session = await invite_and_accept(
        admin_client, api_prefix, email="gone@example.com", password="Employee123"
    )
    user_id = session["user"]["id"]

    suspended = await admin_client.post(f"{api_prefix}/users/{user_id}/suspend")
    assert suspended.status_code == 200
    assert suspended.json()["data"]["status"] == "SUSPENDED"

    async with await _fresh_client() as anonymous:
        blocked = await anonymous.post(
            f"{api_prefix}/auth/login",
            json={"email": "gone@example.com", "password": "Employee123"},
        )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "ACCOUNT_SUSPENDED"

    # Their existing refresh token is dead too, not just future sign-ins.
    async with await _fresh_client() as anonymous:
        refreshed = await anonymous.post(
            f"{api_prefix}/auth/refresh",
            json={"refresh_token": session["tokens"]["refresh_token"]},
        )
    assert refreshed.status_code == 401


async def test_a_suspended_employee_can_be_reinstated(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="back@example.com")
    user_id = session["user"]["id"]

    await admin_client.post(f"{api_prefix}/users/{user_id}/suspend")
    reinstated = await admin_client.post(f"{api_prefix}/users/{user_id}/activate")

    assert reinstated.json()["data"]["status"] == "ACTIVE"


async def test_the_last_administrator_cannot_be_suspended(admin_client, admin, api_prefix):
    response = await admin_client.post(f"{api_prefix}/users/{admin['id']}/suspend")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "LAST_ADMIN"


async def test_an_administrator_cannot_delete_their_own_account(admin_client, admin, api_prefix):
    response = await admin_client.delete(f"{api_prefix}/users/{admin['id']}")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CANNOT_DELETE_SELF"


async def test_a_removed_employee_disappears_from_listings(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="bye@example.com")
    user_id = session["user"]["id"]

    assert (await admin_client.delete(f"{api_prefix}/users/{user_id}")).status_code == 200

    listed = await admin_client.get(f"{api_prefix}/users", params={"search": "bye"})
    assert listed.json()["data"]["pagination"]["total"] == 0
    assert (await admin_client.get(f"{api_prefix}/users/{user_id}")).status_code == 404
