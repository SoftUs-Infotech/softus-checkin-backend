"""Account lifecycle: invite → accept → sign in → rotate → sign out, and passwords."""

import pytest

from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

pytestmark = pytest.mark.usefixtures("db")


# --------------------------------------------------------------------------- #
# invitations
# --------------------------------------------------------------------------- #
async def test_invite_then_accept_activates_the_account(admin_client, api_prefix):
    invite = await admin_client.post(
        f"{api_prefix}/auth/invite",
        json={"email": "ada@example.com", "first_name": "Ada", "department": "Engineering"},
    )
    assert invite.status_code == 201
    body = invite.json()["data"]
    assert body["status"] == "PENDING"
    token = body["invite_url"].split("token=")[1]

    preview = await admin_client.get(f"{api_prefix}/auth/invite/{token}")
    assert preview.status_code == 200
    assert preview.json()["data"]["email"] == "ada@example.com"
    assert preview.json()["data"]["first_name"] == "Ada"

    accepted = await admin_client.post(
        f"{api_prefix}/auth/accept-invite",
        json={
            "token": token,
            "password": "Employee123",
            "first_name": "Ada",
            "last_name": "Lovelace",
        },
    )
    assert accepted.status_code == 200
    session = accepted.json()["data"]
    assert session["user"]["status"] == "ACTIVE"
    assert session["user"]["full_name"] == "Ada Lovelace"
    assert session["tokens"]["access_token"]


# --------------------------------------------------------------------------- #
# sign in
# --------------------------------------------------------------------------- #
async def test_sign_in_returns_a_token_pair(client, admin, api_prefix):
    response = await client.post(
        f"{api_prefix}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tokens"]["token_type"] == "bearer"
    assert data["user"]["role"] == "ADMIN"
    assert "hashed_password" not in data["user"]


# --------------------------------------------------------------------------- #
# refresh rotation
# --------------------------------------------------------------------------- #


async def test_reusing_a_rotated_refresh_token_kills_the_whole_family(client, admin, api_prefix):
    login = await client.post(
        f"{api_prefix}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    first = login.json()["data"]["tokens"]["refresh_token"]

    rotated = await client.post(f"{api_prefix}/auth/refresh", json={"refresh_token": first})
    second = rotated.json()["data"]["tokens"]["refresh_token"]

    replayed = await client.post(f"{api_prefix}/auth/refresh", json={"refresh_token": first})
    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] == "TOKEN_REUSE_DETECTED"

    # The still-current token is collateral damage — that is the point.
    after = await client.post(f"{api_prefix}/auth/refresh", json={"refresh_token": second})
    assert after.status_code == 401
