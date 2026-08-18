"""Account lifecycle: invite â†’ accept â†’ sign in â†’ rotate â†’ sign out, and passwords."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


async def _fresh_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


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


async def test_an_invitation_cannot_be_used_twice(admin_client, api_prefix):
    invite = await admin_client.post(
        f"{api_prefix}/auth/invite", json={"email": "twice@example.com"}
    )
    token = invite.json()["data"]["invite_url"].split("token=")[1]
    payload = {"token": token, "password": "Employee123", "first_name": "Twice"}

    assert (await admin_client.post(f"{api_prefix}/auth/accept-invite", json=payload)).status_code == 200

    second = await admin_client.post(f"{api_prefix}/auth/accept-invite", json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "INVITE_ALREADY_USED"


async def test_reinviting_a_pending_employee_revokes_the_previous_link(admin_client, api_prefix):
    first = await admin_client.post(
        f"{api_prefix}/auth/invite", json={"email": "pending@example.com"}
    )
    old_token = first.json()["data"]["invite_url"].split("token=")[1]

    second = await admin_client.post(
        f"{api_prefix}/auth/invite", json={"email": "pending@example.com"}
    )
    new_token = second.json()["data"]["invite_url"].split("token=")[1]
    assert new_token != old_token

    stale = await admin_client.get(f"{api_prefix}/auth/invite/{old_token}")
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "INVITE_REVOKED"
    assert (await admin_client.get(f"{api_prefix}/auth/invite/{new_token}")).status_code == 200


async def test_inviting_an_already_active_employee_is_rejected(admin_client, api_prefix):
    await invite_and_accept(admin_client, api_prefix, email="active@example.com")

    duplicate = await admin_client.post(
        f"{api_prefix}/auth/invite", json={"email": "active@example.com"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"


async def test_the_manager_role_cannot_be_assigned_yet(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/auth/invite", json={"email": "boss@example.com", "role": "MANAGER"}
    )
    assert response.status_code == 422


async def test_an_unknown_invite_token_is_not_found(admin_client, api_prefix):
    response = await admin_client.get(f"{api_prefix}/auth/invite/not-a-real-token")
    assert response.status_code == 404


async def test_inviting_requires_an_administrator(client, admin, api_prefix):
    response = await client.post(f"{api_prefix}/auth/invite", json={"email": "x@example.com"})
    assert response.status_code == 401


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


async def test_a_wrong_password_is_rejected(client, admin, api_prefix):
    response = await client.post(
        f"{api_prefix}/auth/login", json={"email": ADMIN_EMAIL, "password": "WrongPass123"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_an_unknown_email_gives_the_same_answer_as_a_wrong_password(
    client, admin, api_prefix
):
    response = await client.post(
        f"{api_prefix}/auth/login",
        json={"email": "nobody@example.com", "password": "WhateverPass1"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_an_invited_but_unaccepted_account_cannot_sign_in(admin_client, api_prefix):
    await admin_client.post(f"{api_prefix}/auth/invite", json={"email": "waiting@example.com"})

    async with await _fresh_client() as anonymous:
        response = await anonymous.post(
            f"{api_prefix}/auth/login",
            json={"email": "waiting@example.com", "password": "Employee123"},
        )

    assert response.status_code == 401


async def test_repeated_failures_are_rate_limited(client, admin, api_prefix):
    for _ in range(10):
        await client.post(
            f"{api_prefix}/auth/login", json={"email": ADMIN_EMAIL, "password": "Nope12345"}
        )

    blocked = await client.post(
        f"{api_prefix}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert blocked.status_code == 429
    assert blocked.json()["error"]["details"]["retry_after_seconds"] > 0


# --------------------------------------------------------------------------- #
# refresh rotation
# --------------------------------------------------------------------------- #
async def test_refresh_issues_a_new_pair(client, admin, api_prefix):
    login = await client.post(
        f"{api_prefix}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    original = login.json()["data"]["tokens"]["refresh_token"]

    refreshed = await client.post(f"{api_prefix}/auth/refresh", json={"refresh_token": original})

    assert refreshed.status_code == 200
    assert refreshed.json()["data"]["tokens"]["refresh_token"] != original


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

    # The still-current token is collateral damage â€” that is the point.
    after = await client.post(f"{api_prefix}/auth/refresh", json={"refresh_token": second})
    assert after.status_code == 401


async def test_an_access_token_is_not_accepted_as_a_refresh_token(client, admin, api_prefix):
    login = await client.post(
        f"{api_prefix}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    access = login.json()["data"]["tokens"]["access_token"]

    response = await client.post(f"{api_prefix}/auth/refresh", json={"refresh_token": access})
    assert response.status_code == 401


async def test_logout_revokes_the_presented_refresh_token(client, admin, api_prefix):
    login = await client.post(
        f"{api_prefix}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    tokens = login.json()["data"]["tokens"]
    client.headers["Authorization"] = f"Bearer {tokens['access_token']}"

    out = await client.post(
        f"{api_prefix}/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert out.status_code == 200

    reuse = await client.post(
        f"{api_prefix}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert reuse.status_code == 401


# --------------------------------------------------------------------------- #
# the signed-in user
# --------------------------------------------------------------------------- #
async def test_me_returns_the_signed_in_account(admin_client, api_prefix):
    response = await admin_client.get(f"{api_prefix}/auth/me")

    assert response.status_code == 200
    assert response.json()["data"]["email"] == ADMIN_EMAIL


async def test_me_requires_a_token(client, api_prefix):
    response = await client.get(f"{api_prefix}/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "MISSING_TOKEN"


async def test_an_employee_can_update_their_own_profile(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="self@example.com")

    async with await _fresh_client() as employee:
        employee.headers["Authorization"] = f"Bearer {session['tokens']['access_token']}"
        response = await employee.patch(
            f"{api_prefix}/auth/me", json={"phone": "9876543210", "designation": "Engineer"}
        )

    assert response.status_code == 200
    assert response.json()["data"]["phone"] == "9876543210"


# --------------------------------------------------------------------------- #
# passwords
# --------------------------------------------------------------------------- #
async def test_a_weak_password_is_refused_at_the_boundary(admin_client, api_prefix):
    invite = await admin_client.post(f"{api_prefix}/auth/invite", json={"email": "weak@example.com"})
    token = invite.json()["data"]["invite_url"].split("token=")[1]

    response = await admin_client.post(
        f"{api_prefix}/auth/accept-invite",
        json={"token": token, "password": "abcdefgh", "first_name": "Weak"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_changing_a_password_invalidates_existing_sessions(client, admin, api_prefix):
    login = await client.post(
        f"{api_prefix}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    tokens = login.json()["data"]["tokens"]
    client.headers["Authorization"] = f"Bearer {tokens['access_token']}"

    changed = await client.post(
        f"{api_prefix}/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "BrandNewPass123"},
    )
    assert changed.status_code == 200

    stale = await client.post(
        f"{api_prefix}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert stale.status_code == 401

    async with await _fresh_client() as anonymous:
        again = await anonymous.post(
            f"{api_prefix}/auth/login",
            json={"email": ADMIN_EMAIL, "password": "BrandNewPass123"},
        )
    assert again.status_code == 200


async def test_changing_a_password_requires_the_current_one(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/auth/change-password",
        json={"current_password": "NotMyPassword1", "new_password": "BrandNewPass123"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INCORRECT_PASSWORD"


async def test_the_new_password_must_actually_be_new(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": ADMIN_PASSWORD},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PASSWORD_UNCHANGED"


async def test_forgot_password_does_not_reveal_whether_an_account_exists(
    client, admin, api_prefix
):
    known = await client.post(f"{api_prefix}/auth/forgot-password", json={"email": ADMIN_EMAIL})
    unknown = await client.post(
        f"{api_prefix}/auth/forgot-password", json={"email": "ghost@example.com"}
    )

    assert known.status_code == unknown.status_code == 200
    assert known.json()["message"] == unknown.json()["message"]


async def test_a_reset_link_sets_a_new_password_and_works_only_once(client, admin, api_prefix, db):
    await client.post(f"{api_prefix}/auth/forgot-password", json={"email": ADMIN_EMAIL})

    record = await db["password_resets"].find_one({})
    assert record is not None

    # The plaintext token only exists in the email, so drive the service the way
    # the link would: look the row up by digest via a known token.
    from app.core.security import hash_token
    from app.services.auth import reset_password

    # Re-issue with a token we control, mirroring what the email contained.
    known_token = "known-reset-token-for-test"
    await db["password_resets"].update_one(
        {"_id": record["_id"]}, {"$set": {"token_hash": hash_token(known_token)}}
    )

    await reset_password(known_token, "ResetPass123")

    async with await _fresh_client() as anonymous:
        signed_in = await anonymous.post(
            f"{api_prefix}/auth/login", json={"email": ADMIN_EMAIL, "password": "ResetPass123"}
        )
    assert signed_in.status_code == 200

    from app.core.exceptions import ConflictError

    with pytest.raises(ConflictError):
        await reset_password(known_token, "AnotherPass123")
