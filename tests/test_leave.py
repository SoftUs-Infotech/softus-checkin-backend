"""Leave: balances, applying, approval, and how it lands on the calendar."""

from datetime import timedelta

import pytest

from app.core.datetime import date_key, org_today
from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


async def _seed(admin_client, api_prefix, *, working_weekdays=None, **leave_type_overrides):
    await admin_client.post(
        f"{api_prefix}/shifts",
        json={
            "name": "General",
            "code": "GENERAL",
            "required_minutes": 540,
            "half_day_minutes": 240,
            "working_weekdays": working_weekdays or [0, 1, 2, 3, 4, 5, 6],
        },
    )
    response = await admin_client.post(
        f"{api_prefix}/leave-types",
        json={
            "name": "Casual Leave",
            "code": "CL",
            "annual_quota": 12,
            "accrual": "YEARLY",
            **leave_type_overrides,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _employee(admin_client, api_prefix, email="taker@example.com"):
    session = await invite_and_accept(admin_client, api_prefix, email=email)
    return authed_client(session["tokens"]["access_token"]), session


# Saturdays follow the office pattern (2nd and 4th worked), so counts that
# depend on today's date are pinned to one Monday-to-Friday week: _future(5)
# is a Monday at least two weeks out.
_MONDAY = org_today() + timedelta(days=14 + (7 - org_today().weekday()) % 7)


def _future(days: int) -> str:
    return date_key(_MONDAY + timedelta(days=days - 5))


# --------------------------------------------------------------------------- #
# applying
# --------------------------------------------------------------------------- #
async def test_applying_reserves_the_days_immediately(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        applied = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(5),
                "to_date": _future(6),
                "reason": "Family function out of town.",
            },
        )
        balances = await client.get(f"{api_prefix}/leave/balances")

    assert applied.status_code == 201, applied.text
    assert applied.json()["data"]["total_days"] == 2
    balance = balances.json()["data"][0]
    assert balance["pending"] == 2
    assert balance["used"] == 0
    assert balance["available"] == 10


async def test_weekends_and_holidays_do_not_cost_leave(admin_client, api_prefix):
    """A five-day week and a holiday inside the range should both be skipped."""
    leave_type = await _seed(admin_client, api_prefix, working_weekdays=[0, 1, 2, 3, 4])
    client, _ = await _employee(admin_client, api_prefix, email="weekend@example.com")

    # Pick a fixed Monday-to-Sunday window well in the future.
    monday = org_today() + timedelta(days=(7 - org_today().weekday()) + 14)
    while ((monday + timedelta(days=5)).day - 1) // 7 + 1 in (2, 4):
        monday += timedelta(days=7)  # a worked Saturday would count
    sunday = monday + timedelta(days=6)
    await admin_client.post(
        f"{api_prefix}/holidays",
        json={"date": date_key(monday + timedelta(days=2)), "name": "Company Day"},
    )

    async with client:
        response = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": date_key(monday),
                "to_date": date_key(sunday),
                "reason": "Travelling for a week.",
            },
        )

    data = response.json()["data"]
    # Seven days, minus Saturday and Sunday, minus the holiday.
    assert data["total_days"] == 4
    assert len(data["days"]) == 4


async def test_overlapping_requests_are_refused(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="overlap2@example.com")
    body = {
        "leave_type_id": leave_type["id"],
        "from_date": _future(5),
        "to_date": _future(7),
        "reason": "The first request for these dates.",
    }

    async with client:
        assert (await client.post(f"{api_prefix}/leave", json=body)).status_code == 201
        clash = await client.post(
            f"{api_prefix}/leave",
            json={**body, "from_date": _future(6), "to_date": _future(8),
                  "reason": "Overlapping the first request."},
        )

    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "LEAVE_OVERLAP"


# --------------------------------------------------------------------------- #
# approval
# --------------------------------------------------------------------------- #
async def test_approving_moves_pending_into_used(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="approved@example.com")

    async with client:
        applied = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(5),
                "to_date": _future(6),
                "reason": "Two days for a family event.",
            },
        )
    request_id = applied.json()["data"]["id"]

    await admin_client.post(f"{api_prefix}/leave/{request_id}/approve", json={"note": "Fine."})

    async with authed_client(
        (
            await admin_client.post(
                f"{api_prefix}/auth/login",
                json={"email": "approved@example.com", "password": "Employee123"},
            )
        ).json()["data"]["tokens"]["access_token"]
    ) as employee:
        balances = await employee.get(f"{api_prefix}/leave/balances")

    balance = balances.json()["data"][0]
    assert balance["pending"] == 0
    assert balance["used"] == 2
    assert balance["available"] == 10


async def test_a_holiday_declared_later_gives_the_day_back(admin_client, api_prefix):
    """And the admin calendar and summary see the request."""
    leave_type = await _seed(admin_client, api_prefix)
    client, session = await _employee(admin_client, api_prefix)
    async with client:
        applied = (
            await client.post(
                f"{api_prefix}/leave",
                json={
                    "leave_type_id": leave_type["id"],
                    "from_date": _future(5),
                    "to_date": _future(7),
                    "reason": "Visiting family for a few days.",
                },
            )
        ).json()["data"]
    holiday_on = applied["days"][1]["date"]

    added = await admin_client.post(
        f"{api_prefix}/holidays", json={"date": holiday_on, "name": "Declared later"}
    )
    assert added.status_code == 201, added.text

    calendar = (
        await admin_client.get(
            f"{api_prefix}/leave/calendar",
            params={"from_date": _future(0), "to_date": _future(30)},
        )
    ).json()["data"]
    request = next(item for item in calendar if item["id"] == applied["id"])
    assert request["total_days"] == applied["total_days"] - 1
    assert holiday_on not in [day["date"] for day in request["days"]]

    summary = (await admin_client.get(f"{api_prefix}/leave/summary")).json()["data"]
    row = next(item for item in summary if item["user_id"] == session["user"]["id"])
    assert row["pending"] == applied["total_days"] - 1

    # Removing the holiday makes it a working day again, so it is leave again.
    await admin_client.delete(f"{api_prefix}/holidays/{added.json()['data']['id']}")
    calendar = (
        await admin_client.get(
            f"{api_prefix}/leave/calendar",
            params={"from_date": _future(0), "to_date": _future(30)},
        )
    ).json()["data"]
    request = next(item for item in calendar if item["id"] == applied["id"])
    assert request["total_days"] == applied["total_days"]
    assert holiday_on in [day["date"] for day in request["days"]]
