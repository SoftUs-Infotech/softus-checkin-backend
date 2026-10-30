"""The reporting-manager role: what it decides, and what it may not touch.

A manager is an approver for their own reports and an ordinary employee for
everything else. These tests pin both halves — the powers granted and, just as
importantly, the administration screens the role does *not* open up.
"""

from datetime import timedelta

import pytest

from app.core.datetime import date_key, org_today
from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")

TODAY = date_key(org_today())


def _future(days: int) -> str:
    return date_key(org_today() + timedelta(days=days))


async def _seed_config(admin_client, api_prefix):
    """A shift and a leave type — the minimum for a request to exist."""
    await admin_client.post(
        f"{api_prefix}/shifts",
        json={
            "name": "General",
            "code": "GENERAL",
            "required_minutes": 540,
            "half_day_minutes": 240,
            "working_weekdays": [0, 1, 2, 3, 4, 5, 6],
        },
    )
    response = await admin_client.post(
        f"{api_prefix}/leave-types",
        json={"name": "Casual Leave", "code": "CL", "annual_quota": 12, "accrual": "YEARLY"},
    )
    assert response.status_code == 201, response.text


async def _person(admin_client, api_prefix, *, email, first_name="Ada", role=None, manager=None):
    """Invite somebody, accept, then apply the role or the reporting line.

    The role is set with a ``PATCH`` rather than on the invitation because the
    invite schema still refuses ``MANAGER`` — see the note in the report.
    """
    session = await invite_and_accept(
        admin_client, api_prefix, email=email, first_name=first_name
    )
    user_id = session["user"]["id"]

    changes = {}
    if role:
        changes["role"] = role
    if manager:
        changes["reporting_manager_id"] = manager
    if changes:
        response = await admin_client.patch(f"{api_prefix}/users/{user_id}", json=changes)
        assert response.status_code == 200, response.text

    return authed_client(session["tokens"]["access_token"]), user_id


async def _team(admin_client, api_prefix):
    """A manager, somebody who reports to them, and somebody who does not."""
    manager, manager_id = await _person(
        admin_client, api_prefix, email="boss@example.com", first_name="Bo", role="MANAGER"
    )
    report, report_id = await _person(
        admin_client,
        api_prefix,
        email="report@example.com",
        first_name="Ravi",
        manager=manager_id,
    )
    outsider, outsider_id = await _person(
        admin_client, api_prefix, email="outsider@example.com", first_name="Sam"
    )
    return (manager, manager_id), (report, report_id), (outsider, outsider_id)


async def _apply_for_leave(client, api_prefix, *, days_ahead=3):
    types = await client.get(f"{api_prefix}/leave-types")
    leave_type_id = types.json()["data"][0]["id"]
    day = _future(days_ahead)
    response = await client.post(
        f"{api_prefix}/leave",
        json={
            "leave_type_id": leave_type_id,
            "from_date": day,
            "to_date": day,
            "reason": "A day off for a family function.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


# --------------------------------------------------------------------------- #
# deciding for a report
# --------------------------------------------------------------------------- #


async def test_a_manager_decides_their_report_s_leave(admin_client, api_prefix):
    await _seed_config(admin_client, api_prefix)
    (manager, _), (report, _), _ = await _team(admin_client, api_prefix)

    async with report:
        request_id = await _apply_for_leave(report, api_prefix)

    async with manager:
        response = await manager.post(
            f"{api_prefix}/leave/{request_id}/approve", json={"note": "Enjoy."}
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "APPROVED"
    assert data["approver_role"] == "MANAGER"
