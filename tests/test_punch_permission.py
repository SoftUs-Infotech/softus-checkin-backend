"""Who is allowed the personal side of the application.

The trap here is the account that predates the flag. Reading a missing value as
"yes" hands every administrator created before today a punch widget they are
not supposed to have, which is exactly what happened in the demo database.
"""

import pytest

from app.models.user import may_punch
from app.schemas.user import UserOut
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, authed_client


def out(document: dict) -> UserOut:
    return UserOut.from_document({"id": "1", "email": "a@example.com", "status": "ACTIVE", **document})


def test_an_administrator_without_a_stored_value_does_not_punch():
    assert may_punch({"role": "ADMIN"}) is False


def test_an_employee_without_a_stored_value_does_punch():
    assert may_punch({"role": "EMPLOYEE"}) is True


def test_an_explicit_yes_beats_the_role_default():
    assert may_punch({"role": "ADMIN", "can_punch": True}) is True


def test_an_explicit_no_beats_the_role_default():
    assert may_punch({"role": "EMPLOYEE", "can_punch": False}) is False


def test_the_api_reports_the_effective_value_not_the_stored_one():
    assert out({"role": "ADMIN"}).can_punch is False
    assert out({"role": "EMPLOYEE"}).can_punch is True
    assert out({"role": "ADMIN", "can_punch": True}).can_punch is True


@pytest.mark.asyncio
async def test_an_administrator_of_the_seeded_kind_is_refused_a_punch(
    db, admin, client, api_prefix
):
    """The seeded administrator has no flag stored — the exact failing case.

    From a desktop, so the refusal is about the account and not the device.
    """
    login = await client.post(
        f"{api_prefix}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    desktop = authed_client(login.json()["data"]["tokens"]["access_token"])

    async with desktop:
        response = await desktop.post(
            f"{api_prefix}/attendance/punch-in", json={"request_key": "admin-attempt"}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PUNCH_NOT_PERMITTED"
