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


def _future(days: int) -> str:
    return date_key(org_today() + timedelta(days=days))


# --------------------------------------------------------------------------- #
# balances
# --------------------------------------------------------------------------- #
async def test_a_balance_appears_for_every_leave_type(admin_client, api_prefix):
    await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        response = await client.get(f"{api_prefix}/leave/balances")

    balances = response.json()["data"]
    assert len(balances) == 1
    assert balances[0]["leave_type_code"] == "CL"
    assert balances[0]["allocated"] == 12
    assert balances[0]["available"] == 12


async def test_monthly_accrual_credits_only_the_months_elapsed(admin_client, api_prefix):
    await _seed(admin_client, api_prefix, accrual="MONTHLY")
    client, _ = await _employee(admin_client, api_prefix, email="accrual@example.com")

    async with client:
        response = await client.get(f"{api_prefix}/leave/balances")

    expected = round(12 / 12 * org_today().month, 2)
    assert response.json()["data"][0]["allocated"] == expected


async def test_an_administrator_can_adjust_a_balance(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    _, session = await _employee(admin_client, api_prefix, email="adjusted@example.com")
    user_id = session["user"]["id"]

    response = await admin_client.patch(
        f"{api_prefix}/leave/balances/{user_id}/{leave_type['id']}",
        json={"allocated": 20, "note": "Carried over from the previous system."},
    )

    assert response.status_code == 200
    balances = await admin_client.get(f"{api_prefix}/leave/balances/{user_id}")
    assert balances.json()["data"][0]["allocated"] == 20


async def test_a_balance_adjustment_is_audited_with_its_reason(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    _, session = await _employee(admin_client, api_prefix, email="auditbal@example.com")

    await admin_client.patch(
        f"{api_prefix}/leave/balances/{session['user']['id']}/{leave_type['id']}",
        json={"allocated": 18, "note": "Agreed at the appraisal."},
    )

    logs = await admin_client.get(
        f"{api_prefix}/audit-logs", params={"entity": "LEAVE_BALANCE"}
    )
    entry = logs.json()["data"]["items"][0]
    assert entry["action"] == "ADJUST"
    assert entry["after"]["note"] == "Agreed at the appraisal."


async def test_an_employee_cannot_adjust_a_balance(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, session = await _employee(admin_client, api_prefix, email="greedy@example.com")

    async with client:
        response = await client.patch(
            f"{api_prefix}/leave/balances/{session['user']['id']}/{leave_type['id']}",
            json={"allocated": 999, "note": "Giving myself more leave."},
        )

    assert response.status_code == 403


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


async def test_a_range_of_only_weekends_is_refused(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix, working_weekdays=[0, 1, 2, 3, 4])
    client, _ = await _employee(admin_client, api_prefix, email="weekendonly@example.com")

    saturday = org_today() + timedelta(days=(5 - org_today().weekday()) % 7 + 7)

    async with client:
        response = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": date_key(saturday),
                "to_date": date_key(saturday + timedelta(days=1)),
                "reason": "Asking for the weekend off.",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NO_WORKING_DAYS"


async def test_a_half_day_costs_half(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="halfday@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(3),
                "to_date": _future(3),
                "day_part": "FIRST_HALF",
                "reason": "Morning medical appointment.",
            },
        )

    assert response.json()["data"]["total_days"] == 0.5


async def test_a_half_day_cannot_span_several_dates(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="halfrange@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(3),
                "to_date": _future(5),
                "day_part": "FIRST_HALF",
                "reason": "Trying to take three half days at once.",
            },
        )

    assert response.status_code == 422


async def test_a_half_day_is_refused_when_the_type_forbids_it(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix, allow_half_day=False)
    client, _ = await _employee(admin_client, api_prefix, email="nohalf@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(3),
                "to_date": _future(3),
                "day_part": "SECOND_HALF",
                "reason": "Half day on a type that does not allow it.",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "HALF_DAY_NOT_ALLOWED"


async def test_more_days_than_the_balance_is_refused(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="overdrawn@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(5),
                "to_date": _future(30),
                "reason": "A month off, which is more than the quota.",
            },
        )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert body["error"]["details"]["available"] == 12


async def test_a_type_that_allows_it_may_go_overdrawn(admin_client, api_prefix):
    """An advance against days not yet accrued, when the policy permits one."""
    leave_type = await _seed(admin_client, api_prefix, allow_negative_balance=True)
    client, _ = await _employee(admin_client, api_prefix, email="advance@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(5),
                "to_date": _future(20),
                "reason": "More days than accrued, taken as an advance.",
            },
        )
        balances = await client.get(f"{api_prefix}/leave/balances")

    assert response.status_code == 201, response.text
    assert balances.json()["data"][0]["available"] == -4


async def test_unpaid_leave_has_no_balance_to_exhaust(admin_client, api_prefix):
    await _seed(admin_client, api_prefix)
    unpaid = await admin_client.post(
        f"{api_prefix}/leave-types",
        json={"name": "Unpaid", "code": "LWP", "annual_quota": 0, "is_paid": False},
    )
    client, _ = await _employee(admin_client, api_prefix, email="unpaid@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": unpaid.json()["data"]["id"],
                "from_date": _future(5),
                "to_date": _future(9),
                "reason": "Extended personal leave without pay.",
            },
        )

    assert response.status_code == 201


async def test_a_notice_period_is_enforced(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix, min_notice_days=7)
    client, _ = await _employee(admin_client, api_prefix, email="lastminute@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(1),
                "to_date": _future(1),
                "reason": "Asking for tomorrow off at short notice.",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INSUFFICIENT_NOTICE"


async def test_a_maximum_run_of_consecutive_days_is_enforced(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix, max_consecutive_days=2)
    client, _ = await _employee(admin_client, api_prefix, email="toolong@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(5),
                "to_date": _future(9),
                "reason": "Five days on a type capped at two.",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TOO_MANY_CONSECUTIVE_DAYS"


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


async def test_a_reversed_date_range_is_refused(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="reversed2@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(9),
                "to_date": _future(5),
                "reason": "Dates the wrong way round.",
            },
        )

    assert response.status_code == 422


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


async def test_rejecting_releases_the_reservation(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="denied2@example.com")

    async with client:
        applied = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(5),
                "to_date": _future(6),
                "reason": "Two days I may not get.",
            },
        )
        request_id = applied.json()["data"]["id"]

    await admin_client.post(
        f"{api_prefix}/leave/{request_id}/reject", json={"note": "Release week."}
    )

    async with authed_client(
        (
            await admin_client.post(
                f"{api_prefix}/auth/login",
                json={"email": "denied2@example.com", "password": "Employee123"},
            )
        ).json()["data"]["tokens"]["access_token"]
    ) as employee:
        balances = await employee.get(f"{api_prefix}/leave/balances")

    balance = balances.json()["data"][0]
    assert balance["pending"] == 0
    assert balance["used"] == 0
    assert balance["available"] == 12


async def test_approved_leave_shows_on_the_calendar(admin_client, api_prefix):
    """The point of the whole feature: an approved day turns violet."""
    leave_type = await _seed(admin_client, api_prefix)
    client, session = await _employee(admin_client, api_prefix, email="calendar@example.com")
    start = _future(5)

    async with client:
        applied = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": start,
                "to_date": start,
                "reason": "A single day off for a personal errand.",
            },
        )
    request_id = applied.json()["data"]["id"]
    await admin_client.post(f"{api_prefix}/leave/{request_id}/approve", json={})

    calendar = await admin_client.get(
        f"{api_prefix}/attendance/calendar",
        params={
            "user_id": session["user"]["id"],
            "year": int(start[:4]),
            "month": int(start[5:7]),
        },
    )
    day = next(d for d in calendar.json()["data"]["days"] if d["date"] == start)
    assert day["status"] == "ON_LEAVE"
    assert day["color"] == "violet"
    assert day["leave_type"] == "Casual Leave"


async def test_a_decided_request_cannot_be_decided_again(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="twice4@example.com")

    async with client:
        applied = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(5),
                "to_date": _future(5),
                "reason": "One day that will be decided twice.",
            },
        )
    request_id = applied.json()["data"]["id"]

    await admin_client.post(f"{api_prefix}/leave/{request_id}/approve", json={})
    again = await admin_client.post(f"{api_prefix}/leave/{request_id}/reject", json={})

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "LEAVE_ALREADY_DECIDED"


async def test_an_employee_cannot_approve_their_own_leave(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="selfleave@example.com")

    async with client:
        applied = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(5),
                "to_date": _future(5),
                "reason": "Trying to approve my own leave.",
            },
        )
        request_id = applied.json()["data"]["id"]
        response = await client.post(f"{api_prefix}/leave/{request_id}/approve", json={})

    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# withdrawal and listing
# --------------------------------------------------------------------------- #
async def test_withdrawing_a_pending_request_returns_the_days(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="withdraw2@example.com")

    async with client:
        applied = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(5),
                "to_date": _future(7),
                "reason": "Plans that later changed.",
            },
        )
        request_id = applied.json()["data"]["id"]
        cancelled = await client.post(f"{api_prefix}/leave/{request_id}/cancel")
        balances = await client.get(f"{api_prefix}/leave/balances")

    assert cancelled.json()["data"]["status"] == "CANCELLED"
    assert balances.json()["data"][0]["available"] == 12


async def test_withdrawing_approved_future_leave_returns_the_days(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="unbook@example.com")

    async with client:
        applied = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(10),
                "to_date": _future(11),
                "reason": "A trip that was later cancelled.",
            },
        )
    request_id = applied.json()["data"]["id"]
    await admin_client.post(f"{api_prefix}/leave/{request_id}/approve", json={})

    token = (
        await admin_client.post(
            f"{api_prefix}/auth/login",
            json={"email": "unbook@example.com", "password": "Employee123"},
        )
    ).json()["data"]["tokens"]["access_token"]

    async with authed_client(token) as employee:
        await employee.post(f"{api_prefix}/leave/{request_id}/cancel")
        balances = await employee.get(f"{api_prefix}/leave/balances")

    assert balances.json()["data"][0]["available"] == 12


async def test_who_is_away_today_is_listed_for_the_dashboard(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="awaytoday@example.com")
    today = date_key(org_today())

    async with client:
        applied = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": today,
                "to_date": today,
                "reason": "Unwell and taking today off.",
            },
        )
    request_id = applied.json()["data"]["id"]
    await admin_client.post(f"{api_prefix}/leave/{request_id}/approve", json={})

    response = await admin_client.get(f"{api_prefix}/leave/on-leave-today")

    employees = response.json()["data"]["employees"]
    assert len(employees) == 1
    assert employees[0]["leave_type_name"] == "Casual Leave"


async def test_an_employee_sees_only_their_own_requests(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    body = {
        "leave_type_id": leave_type["id"],
        "from_date": _future(5),
        "to_date": _future(5),
        "reason": "A single day of leave.",
    }

    first, _ = await _employee(admin_client, api_prefix, email="leave1@example.com")
    async with first:
        await first.post(f"{api_prefix}/leave", json=body)

    second, _ = await _employee(admin_client, api_prefix, email="leave2@example.com")
    async with second:
        await second.post(f"{api_prefix}/leave", json=body)
        mine = await second.get(f"{api_prefix}/leave/mine")

    assert mine.json()["data"]["pagination"]["total"] == 1
    queue = await admin_client.get(f"{api_prefix}/leave")
    assert queue.json()["data"]["pagination"]["total"] == 2


async def test_the_pending_count_drives_the_admin_badge(admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="leavebadge@example.com")

    async with client:
        await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": _future(5),
                "to_date": _future(5),
                "reason": "Waiting on a decision.",
            },
        )

    response = await admin_client.get(f"{api_prefix}/leave/pending-count")

    assert response.json()["data"]["pending"] == 1
