"""Two-factor authentication for administrator accounts.

The flow under test: an administrator sets up a secret, confirms it with a code
from their app, and from then on ``/auth/login`` hands back a challenge instead
of tokens. The tokens only exist once ``/auth/2fa/verify`` has seen a code.
"""

from datetime import timedelta

import pyotp
import pytest
from bson import ObjectId
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.datetime import utc_now
from app.db.mongo import Collections
from app.main import app
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, invite_and_accept

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
# setting it up
# --------------------------------------------------------------------------- #
async def test_setup_returns_a_uri_and_recovery_codes_but_enables_nothing(
    admin_client, api_prefix
):
    response = await admin_client.post(f"{api_prefix}/auth/2fa/setup")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["otpauth_uri"].startswith("otpauth://totp/")
    assert data["secret"] in data["otpauth_uri"]
    assert len(data["recovery_codes"]) == settings.TWO_FACTOR_RECOVERY_CODES
    assert len(set(data["recovery_codes"])) == settings.TWO_FACTOR_RECOVERY_CODES

    status = await admin_client.get(f"{api_prefix}/auth/2fa")
    assert status.json()["data"] == {
        "enabled": False,
        "pending": True,
        "confirmed_at": None,
        "recovery_codes_remaining": settings.TWO_FACTOR_RECOVERY_CODES,
    }


async def test_confirming_with_a_code_switches_it_on(admin_client, api_prefix):
    await enable_two_factor(admin_client, api_prefix)

    status = await admin_client.get(f"{api_prefix}/auth/2fa")
    data = status.json()["data"]
    assert data["enabled"] is True
    assert data["pending"] is False
    assert data["confirmed_at"] is not None


async def test_confirming_with_a_wrong_code_leaves_it_off(admin_client, api_prefix):
    await admin_client.post(f"{api_prefix}/auth/2fa/setup")

    response = await admin_client.post(f"{api_prefix}/auth/2fa/confirm", json={"code": "000000"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_TWO_FACTOR_CODE"
    assert (await admin_client.get(f"{api_prefix}/auth/2fa")).json()["data"]["enabled"] is False


async def test_confirming_before_setup_is_refused(admin_client, api_prefix):
    response = await admin_client.post(f"{api_prefix}/auth/2fa/confirm", json={"code": "123456"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TWO_FACTOR_NOT_STARTED"


async def test_setup_cannot_replace_a_secret_that_is_already_in_use(admin_client, api_prefix):
    await enable_two_factor(admin_client, api_prefix)

    response = await admin_client.post(f"{api_prefix}/auth/2fa/setup")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TWO_FACTOR_ALREADY_ENABLED"


# --------------------------------------------------------------------------- #
# employees are not part of this
# --------------------------------------------------------------------------- #
async def test_an_employee_cannot_reach_the_setup_endpoint(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="staff@example.com")

    async with await _fresh_client() as employee:
        employee.headers["Authorization"] = f"Bearer {session['tokens']['access_token']}"
        for path in ("/auth/2fa", "/auth/2fa/setup"):
            method = employee.get if path == "/auth/2fa" else employee.post
            response = await method(f"{api_prefix}{path}")
            assert response.status_code == 403, path


async def test_an_employee_signs_in_exactly_as_before(admin_client, api_prefix):
    await invite_and_accept(admin_client, api_prefix, email="plain@example.com")

    async with await _fresh_client() as employee:
        response = await employee.post(
            f"{api_prefix}/auth/login",
            json={"email": "plain@example.com", "password": "Employee123"},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tokens"]["access_token"]
    assert "challenge_token" not in data


async def test_an_administrator_without_two_factor_still_gets_tokens(client, admin, api_prefix):
    data = await start_login(client, api_prefix)

    assert data["tokens"]["access_token"]
    assert "challenge_token" not in data


# --------------------------------------------------------------------------- #
# signing in with the second factor
# --------------------------------------------------------------------------- #
async def test_login_returns_a_challenge_instead_of_tokens(admin_client, api_prefix):
    await enable_two_factor(admin_client, api_prefix)

    async with await _fresh_client() as anonymous:
        data = await start_login(anonymous, api_prefix)

    assert data["two_factor_required"] is True
    assert data["challenge_token"]
    assert data["expires_in"] == settings.TWO_FACTOR_CHALLENGE_EXPIRE_MINUTES * 60
    assert "tokens" not in data


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


async def test_a_wrong_code_never_issues_tokens(admin_client, api_prefix):
    await enable_two_factor(admin_client, api_prefix)

    async with await _fresh_client() as anonymous:
        challenge = await start_login(anonymous, api_prefix)
        response = await anonymous.post(
            f"{api_prefix}/auth/2fa/verify",
            json={"challenge_token": challenge["challenge_token"], "code": "000000"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TWO_FACTOR_CODE"
    assert "tokens" not in (response.json()["data"] or {})


async def test_verification_is_rate_limited(admin_client, api_prefix):
    await enable_two_factor(admin_client, api_prefix)

    async with await _fresh_client() as anonymous:
        challenge = await start_login(anonymous, api_prefix)
        body = {"challenge_token": challenge["challenge_token"], "code": "000000"}
        for _ in range(10):
            await anonymous.post(f"{api_prefix}/auth/2fa/verify", json=body)

        blocked = await anonymous.post(f"{api_prefix}/auth/2fa/verify", json=body)

    assert blocked.status_code == 429
    assert blocked.json()["error"]["details"]["retry_after_seconds"] > 0


async def test_a_code_is_accepted_a_step_either_side_of_now(admin_client, api_prefix, db, admin):
    setup = await enable_two_factor(admin_client, api_prefix)
    # Confirmation already spent the previous step; clear the replay guard so it
    # is the drift window being tested here and not the guard.
    await db[Collections.USERS].update_one(
        {"_id": ObjectId(admin["id"])}, {"$set": {"two_factor.last_used_step": None}}
    )

    for steps in (-1, 1):
        async with await _fresh_client() as anonymous:
            challenge = await start_login(anonymous, api_prefix)
            response = await anonymous.post(
                f"{api_prefix}/auth/2fa/verify",
                json={
                    "challenge_token": challenge["challenge_token"],
                    "code": code_for(setup["secret"], steps=steps),
                },
            )
        assert response.status_code == 200, f"step offset {steps}: {response.text}"
        await db[Collections.USERS].update_one(
            {"_id": ObjectId(admin["id"])}, {"$set": {"two_factor.last_used_step": None}}
        )


async def test_a_code_already_used_cannot_be_replayed(admin_client, api_prefix):
    setup = await enable_two_factor(admin_client, api_prefix)
    code = code_for(setup["secret"])

    async with await _fresh_client() as anonymous:
        first = await start_login(anonymous, api_prefix)
        accepted = await anonymous.post(
            f"{api_prefix}/auth/2fa/verify",
            json={"challenge_token": first["challenge_token"], "code": code},
        )
        assert accepted.status_code == 200

        second = await start_login(anonymous, api_prefix)
        replayed = await anonymous.post(
            f"{api_prefix}/auth/2fa/verify",
            json={"challenge_token": second["challenge_token"], "code": code},
        )

    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] == "INVALID_TWO_FACTOR_CODE"


# --------------------------------------------------------------------------- #
# the challenge itself
# --------------------------------------------------------------------------- #
async def test_a_challenge_token_is_single_use(admin_client, api_prefix):
    setup = await enable_two_factor(admin_client, api_prefix)

    async with await _fresh_client() as anonymous:
        challenge = await start_login(anonymous, api_prefix)
        body = {
            "challenge_token": challenge["challenge_token"],
            "code": code_for(setup["secret"]),
        }
        assert (await anonymous.post(f"{api_prefix}/auth/2fa/verify", json=body)).status_code == 200

        reused = await anonymous.post(f"{api_prefix}/auth/2fa/verify", json=body)

    assert reused.status_code == 401
    assert reused.json()["error"]["code"] == "TWO_FACTOR_CHALLENGE_INVALID"


async def test_an_expired_challenge_is_refused(admin_client, api_prefix, db, admin):
    setup = await enable_two_factor(admin_client, api_prefix)

    async with await _fresh_client() as anonymous:
        challenge = await start_login(anonymous, api_prefix)
        await db[Collections.USERS].update_one(
            {"_id": ObjectId(admin["id"])},
            {"$set": {"two_factor.challenge.expires_at": utc_now() - timedelta(seconds=1)}},
        )
        response = await anonymous.post(
            f"{api_prefix}/auth/2fa/verify",
            json={
                "challenge_token": challenge["challenge_token"],
                "code": code_for(setup["secret"]),
            },
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TWO_FACTOR_CHALLENGE_INVALID"


async def test_a_challenge_is_useless_once_the_password_changes(admin_client, api_prefix):
    setup = await enable_two_factor(admin_client, api_prefix)

    async with await _fresh_client() as anonymous:
        challenge = await start_login(anonymous, api_prefix)

        changed = await admin_client.post(
            f"{api_prefix}/auth/change-password",
            json={"current_password": ADMIN_PASSWORD, "new_password": "BrandNewPass123"},
        )
        assert changed.status_code == 200

        response = await anonymous.post(
            f"{api_prefix}/auth/2fa/verify",
            json={
                "challenge_token": challenge["challenge_token"],
                "code": code_for(setup["secret"]),
            },
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TWO_FACTOR_CHALLENGE_INVALID"


async def test_an_unknown_challenge_token_is_refused(admin_client, api_prefix):
    await enable_two_factor(admin_client, api_prefix)

    async with await _fresh_client() as anonymous:
        response = await anonymous.post(
            f"{api_prefix}/auth/2fa/verify",
            json={"challenge_token": "not-a-real-challenge", "code": "000000"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TWO_FACTOR_CHALLENGE_INVALID"


# --------------------------------------------------------------------------- #
# recovery codes
# --------------------------------------------------------------------------- #
async def test_a_recovery_code_signs_in_and_is_spent(admin_client, api_prefix):
    setup = await enable_two_factor(admin_client, api_prefix)
    recovery = setup["recovery_codes"][0]

    async with await _fresh_client() as anonymous:
        challenge = await start_login(anonymous, api_prefix)
        # Typed back in as a person would: spacing and case are not the secret.
        accepted = await anonymous.post(
            f"{api_prefix}/auth/2fa/verify",
            json={"challenge_token": challenge["challenge_token"], "code": recovery.lower()},
        )
        assert accepted.status_code == 200
        assert accepted.json()["data"]["tokens"]["access_token"]

        again = await start_login(anonymous, api_prefix)
        reused = await anonymous.post(
            f"{api_prefix}/auth/2fa/verify",
            json={"challenge_token": again["challenge_token"], "code": recovery},
        )

    assert reused.status_code == 401
    assert reused.json()["error"]["code"] == "INVALID_TWO_FACTOR_CODE"

    remaining = await admin_client.get(f"{api_prefix}/auth/2fa")
    assert (
        remaining.json()["data"]["recovery_codes_remaining"]
        == settings.TWO_FACTOR_RECOVERY_CODES - 1
    )


# --------------------------------------------------------------------------- #
# turning it off
# --------------------------------------------------------------------------- #
async def test_disabling_needs_the_account_password(admin_client, api_prefix):
    await enable_two_factor(admin_client, api_prefix)

    response = await admin_client.post(
        f"{api_prefix}/auth/2fa/disable", json={"password": "NotThePassword1"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INCORRECT_PASSWORD"
    assert (await admin_client.get(f"{api_prefix}/auth/2fa")).json()["data"]["enabled"] is True


async def test_disabling_clears_the_secret_and_every_recovery_code(
    admin_client, api_prefix, db, admin
):
    setup = await enable_two_factor(admin_client, api_prefix)

    response = await admin_client.post(
        f"{api_prefix}/auth/2fa/disable", json={"password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["data"] == {
        "enabled": False,
        "pending": False,
        "confirmed_at": None,
        "recovery_codes_remaining": 0,
    }

    stored = await db[Collections.USERS].find_one({"_id": ObjectId(admin["id"])})
    assert stored["two_factor"]["secret"] is None
    assert stored["two_factor"]["recovery_code_hashes"] == []
    assert stored["two_factor"]["challenge"] is None

    # And the sign-in goes straight back to handing out tokens.
    async with await _fresh_client() as anonymous:
        data = await start_login(anonymous, api_prefix)
    assert data["tokens"]["access_token"]
    assert setup["secret"] not in str(stored["two_factor"])
