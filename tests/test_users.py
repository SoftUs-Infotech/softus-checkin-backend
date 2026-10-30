"""Employee administration: listing, updating, suspending, removing."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.conftest import invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


async def _fresh_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


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


async def test_the_last_administrator_cannot_be_suspended(admin_client, admin, api_prefix):
    response = await admin_client.post(f"{api_prefix}/users/{admin['id']}/suspend")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "LAST_ADMIN"
