"""Two-factor authentication for administrator accounts.

The flow under test: an administrator sets up a secret, confirms it with a code
from their app, and from then on ``/auth/login`` hands back a challenge instead
of tokens. The tokens only exist once ``/auth/2fa/verify`` has seen a code.
"""

from datetime import timedelta

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.datetime import utc_now
from app.main import app
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

pytestmark = pytest.mark.usefixtures("db")


async def _fresh_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def code_for(secret: str, *, steps: int = 0) -> str:
    """A valid code for the step ``steps`` away from now."""
    return pyotp.TOTP(secret).at(utc_now() + timedelta(seconds=30 * steps))


async def enable_two_factor(admin_client: AsyncClient, api_prefix: str) -> dict:
    """Run setup → confirm and return the setup payload, secret included.

    Confirmation deliberately spends the *previous* step, so every test that
    follows can use the current one without tripping the replay guard.
    """
    setup = await admin_client.post(f"{api_prefix}/auth/2fa/setup")
    assert setup.status_code == 200, setup.text
    payload = setup.json()["data"]

    confirmed = await admin_client.post(
        f"{api_prefix}/auth/2fa/confirm",
        json={"code": code_for(payload["secret"], steps=-1)},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["enabled"] is True
    return payload


async def start_login(client: AsyncClient, api_prefix: str, *, password=ADMIN_PASSWORD) -> dict:
    response = await client.post(
        f"{api_prefix}/auth/login", json={"email": ADMIN_EMAIL, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --------------------------------------------------------------------------- #
# signing in with the second factor
# --------------------------------------------------------------------------- #


async def test_the_challenge_and_a_code_produce_the_token_pair(admin_client, api_prefix):
    setup = await enable_two_factor(admin_client, api_prefix)

    async with await _fresh_client() as anonymous:
        challenge = await start_login(anonymous, api_prefix)
        response = await anonymous.post(
            f"{api_prefix}/auth/2fa/verify",
            json={
                "challenge_token": challenge["challenge_token"],
                "code": code_for(setup["secret"]),
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tokens"]["access_token"]
    assert data["user"]["email"] == ADMIN_EMAIL
