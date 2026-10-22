"""Clearing a queue in one action.

The rule that matters: one bad identifier costs you that record and no other.
An administrator clearing a morning's queue must not lose good decisions
because one request was withdrawn in another tab.
"""

from datetime import timedelta

import pytest

from app.core.datetime import date_key, org_today
from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


async def _seed_shift(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/shifts",
        json={
            "name": "General",
            "code": "GENERAL",
            "required_minutes": 540,
            "half_day_minutes": 240,
            "working_weekdays": [0, 1, 2, 3, 4, 5, 6],
        },
    )
    assert response.status_code == 201
    return response.json()["data"]


async def _file(client, api_prefix, day: str) -> str:
    response = await client.post(
        f"{api_prefix}/corrections",
        json={
            "date": day,
            "type": "FULL_DAY",
            "requested_punch_in": f"{day}T04:00:00Z",
            "requested_punch_out": f"{day}T13:00:00Z",
            "reason": "Forgot to punch out before leaving for the client visit.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


async def test_many_corrections_are_decided_in_one_call(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="bulk1@example.com")
    client = authed_client(session["tokens"]["access_token"])

    async with client:
        ids = [
            await _file(client, api_prefix, date_key(org_today() - timedelta(days=offset)))
            for offset in (1, 2)
        ]

    response = await admin_client.post(
        f"{api_prefix}/corrections/bulk-decision",
        json={"ids": ids, "approve": True, "note": "Reviewed together"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["succeeded"], data["failed"]) == (2, 0)


async def test_a_bad_identifier_only_costs_its_own_record(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="bulk2@example.com")
    client = authed_client(session["tokens"]["access_token"])

    async with client:
        good = await _file(client, api_prefix, date_key(org_today() - timedelta(days=1)))

    response = await admin_client.post(
        f"{api_prefix}/corrections/bulk-decision",
        json={"ids": [good, "not-an-id", "6501f1a2b3c4d5e6f7a8b9c0"], "approve": True},
    )

    data = response.json()["data"]
    assert (data["succeeded"], data["failed"]) == (1, 2)
    assert [result["success"] for result in data["results"]] == [True, False, False]


async def test_the_same_identifier_twice_is_decided_once(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="bulk3@example.com")
    client = authed_client(session["tokens"]["access_token"])

    async with client:
        one = await _file(client, api_prefix, date_key(org_today() - timedelta(days=1)))

    response = await admin_client.post(
        f"{api_prefix}/corrections/bulk-decision",
        json={"ids": [one, one], "approve": True},
    )

    data = response.json()["data"]
    assert data["requested"] == 1
    assert data["succeeded"] == 1


async def test_an_employee_cannot_decide_in_bulk(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="bulk4@example.com")
    client = authed_client(session["tokens"]["access_token"])

    async with client:
        response = await client.post(
            f"{api_prefix}/corrections/bulk-decision",
            json={"ids": ["6501f1a2b3c4d5e6f7a8b9c0"], "approve": True},
        )

    assert response.status_code == 403
