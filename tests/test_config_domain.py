"""Holidays, leave types, organisation settings and the audit trail."""

import pytest

from tests.conftest import invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


# --------------------------------------------------------------------------- #
# audit trail
# --------------------------------------------------------------------------- #


async def test_an_audit_entry_never_carries_a_password_hash(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="audited@example.com")
    await admin_client.patch(
        f"{api_prefix}/users/{session['user']['id']}", json={"designation": "Engineer"}
    )

    logs = await admin_client.get(f"{api_prefix}/audit-logs", params={"entity": "USER"})

    assert "hashed_password" not in logs.text
