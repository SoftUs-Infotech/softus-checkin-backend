"""Correction requests: raising, approving, rejecting, withdrawing."""

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


async def _employee(admin_client, api_prefix, email="fixer@example.com"):
    session = await invite_and_accept(admin_client, api_prefix, email=email)
    return authed_client(session["tokens"]["access_token"]), session


def _yesterday() -> str:
    return date_key(org_today() - timedelta(days=1))


# --------------------------------------------------------------------------- #
# approval
# --------------------------------------------------------------------------- #
async def test_approving_a_correction_rewrites_the_day(admin_client, api_prefix):
    """The whole point: a red day becomes green once the request is approved."""
    await _seed_shift(admin_client, api_prefix)
    client, session = await _employee(admin_client, api_prefix)
    day = _yesterday()

    async with client:
        created = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Was at the client site all day; forgot to punch.",
            },
        )
    correction_id = created.json()["data"]["id"]

    # Before: the day is absent.
    before = await admin_client.get(
        f"{api_prefix}/attendance/calendar",
        params={"user_id": session["user"]["id"], "year": int(day[:4]), "month": int(day[5:7])},
    )
    before_day = next(d for d in before.json()["data"]["days"] if d["date"] == day)
    assert before_day["status"] == "ABSENT"

    approved = await admin_client.post(
        f"{api_prefix}/corrections/{correction_id}/approve",
        json={"note": "Confirmed with the team lead."},
    )
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "APPROVED"

    after = await admin_client.get(
        f"{api_prefix}/attendance/calendar",
        params={"user_id": session["user"]["id"], "year": int(day[:4]), "month": int(day[5:7])},
    )
    after_day = next(d for d in after.json()["data"]["days"] if d["date"] == day)
    assert after_day["status"] == "PRESENT"
    assert after_day["worked_minutes"] == 540
    assert after_day["color"] == "green"


async def test_an_employee_cannot_approve_their_own_request(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="selfapprove@example.com")
    day = _yesterday()

    async with client:
        created = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Trying to approve my own correction.",
            },
        )
        correction_id = created.json()["data"]["id"]
        response = await client.post(
            f"{api_prefix}/corrections/{correction_id}/approve", json={}
        )

    assert response.status_code == 403
