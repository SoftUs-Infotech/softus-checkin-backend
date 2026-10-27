"""Leave restarts with attendance on go-live day.

The administrator presses "reset" the day the system goes live. Before this,
balances were built as if everybody had been earning since joining — an
employee who joined years ago opened the app to ten months of monthly leave
nobody had tracked. After it, every balance is zero on go-live day and accrues
from there: nothing earned, carried over or taken before the day counts.

The suite runs against the real date, because the reset refuses a future day.
"""

from datetime import date, timedelta

import pytest

from app.core.datetime import date_key, org_today
from app.db.mongo import Collections
from app.services import leave as leave_service
from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")

TODAY = org_today()
YEAR = TODAY.year
MONTH_START = TODAY.replace(day=1)


async def _leave_type(admin_client, api_prefix, **overrides) -> dict:
    body = {"name": "Casual Leave", "code": "CL", **overrides}
    response = await admin_client.post(f"{api_prefix}/leave-types", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _reset(admin_client, api_prefix, day: date) -> None:
    response = await admin_client.post(
        f"{api_prefix}/settings/tracking-start", json={"starts_on": day.isoformat()}
    )
    assert response.status_code == 200, response.text


async def _balance(admin_client, api_prefix, user_id: str, leave_type_id: str) -> dict:
    response = await admin_client.get(f"{api_prefix}/leave/balances/{user_id}")
    assert response.status_code == 200, response.text
    return next(row for row in response.json()["data"] if row["leave_type_id"] == leave_type_id)


async def _request(db, user_id: str, leave_type: dict, day: date, status: str) -> str:
    """A one-day request written straight to the database, as old data would be."""
    result = await db[Collections.LEAVE_REQUESTS].insert_one(
        {
            "user_id": user_id,
            "user_email": "ada@example.com",
            "leave_type_id": leave_type["id"],
            "leave_type_name": leave_type["name"],
            "leave_type_code": leave_type["code"],
            "from_date": date_key(day),
            "to_date": date_key(day),
            "days": [{"date": date_key(day), "day_part": "FULL"}],
            "total_days": 1,
            "reason": "Family function",
            "status": status,
            "is_deleted": False,
        }
    )
    field = "used" if status == "APPROVED" else "pending"
    await db[Collections.LEAVE_BALANCES].update_one(
        {"user_id": user_id, "leave_type_id": leave_type["id"], "year": day.year},
        {"$inc": {field: 1}},
        upsert=True,
    )
    return str(result.inserted_id)


def _next_weekday(after: date) -> date:
    day = after + timedelta(days=7)
    while day.weekday() != 2:  # a Wednesday is a working day on every pattern
        day += timedelta(days=1)
    return day


# --------------------------------------------------------------------------- #
# when earning starts
# --------------------------------------------------------------------------- #
def test_earning_never_starts_before_go_live():
    go_live = date(2026, 10, 24)
    leave_type = {"applicable_after_days": 0}

    long_served = leave_service._earning_from(
        {"joining_date": "2020-01-01"}, leave_type, go_live
    )
    unknown = leave_service._earning_from({}, leave_type, go_live)
    joins_later = leave_service._earning_from(
        {"joining_date": "2026-11-02"}, leave_type, go_live
    )

    assert long_served == go_live
    assert unknown == go_live
    assert joins_later == date(2026, 11, 2)


def test_without_go_live_earning_follows_joining_as_before():
    assert leave_service._earning_from(
        {"joining_date": "2026-03-10"}, {"applicable_after_days": 5}
    ) == date(2026, 3, 15)


# --------------------------------------------------------------------------- #
# the reset
# --------------------------------------------------------------------------- #
async def test_monthly_leave_earned_before_go_live_is_wiped(admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client, api_prefix, accrual={"frequency": "MONTHLY", "amount_per_cycle": 1}
    )
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")
    user_id = session["user"]["id"]
    before = await _balance(admin_client, api_prefix, user_id, leave_type["id"])
    assert before["allocated"] == TODAY.month  # every month since January

    await _reset(admin_client, api_prefix, MONTH_START)

    after = await _balance(admin_client, api_prefix, user_id, leave_type["id"])
    assert after["allocated"] == 1  # this month only
    assert after["available"] == 1


async def test_a_yearly_quota_is_prorated_from_go_live(admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client, api_prefix, accrual={"frequency": "YEARLY", "amount_per_cycle": 12}
    )
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")
    user_id = session["user"]["id"]

    await _reset(admin_client, api_prefix, TODAY)

    served = (date(YEAR, 12, 31) - TODAY).days + 1
    whole = (date(YEAR, 12, 31) - date(YEAR, 1, 1)).days + 1
    balance = await _balance(admin_client, api_prefix, user_id, leave_type["id"])
    assert balance["allocated"] == round(12 * served / whole, 2)


async def test_leave_taken_before_go_live_is_given_back(db, admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client, api_prefix, accrual={"frequency": "MONTHLY", "amount_per_cycle": 1}
    )
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")
    user_id = session["user"]["id"]
    await _balance(admin_client, api_prefix, user_id, leave_type["id"])  # create it
    await _request(db, user_id, leave_type, TODAY - timedelta(days=1), "APPROVED")
    upcoming = _next_weekday(TODAY)
    await _request(db, user_id, leave_type, upcoming, "PENDING")

    await _reset(admin_client, api_prefix, TODAY)

    balance = await _balance(admin_client, api_prefix, user_id, leave_type["id"])
    assert balance["used"] == 0
    # Leave asked for after go-live is still on hold against the balance.
    assert balance["pending"] == (1 if upcoming.year == YEAR else 0)


async def test_nothing_carries_over_from_before_go_live(db, admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client,
        api_prefix,
        accrual={"frequency": "YEARLY", "amount_per_cycle": 12, "lapse_at_year_end": False},
    )
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")
    user_id = session["user"]["id"]
    await db[Collections.LEAVE_BALANCES].insert_one(
        {
            "user_id": user_id,
            "leave_type_id": leave_type["id"],
            "year": YEAR - 1,
            "allocated": 8,
            "carried_forward": 0,
            "used": 0,
            "pending": 0,
            "accrual_credits": [],
            "is_deleted": False,
        }
    )
    assert (await _balance(admin_client, api_prefix, user_id, leave_type["id"]))[
        "carried_forward"
    ] == 8

    await _reset(admin_client, api_prefix, TODAY)

    balance = await _balance(admin_client, api_prefix, user_id, leave_type["id"])
    assert balance["carried_forward"] == 0
    # Last year's record is history and is left alone.
    previous = await db[Collections.LEAVE_BALANCES].find_one(
        {"user_id": user_id, "leave_type_id": leave_type["id"], "year": YEAR - 1}
    )
    assert previous["allocated"] == 8


async def test_resetting_twice_does_not_double_anything(admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client, api_prefix, accrual={"frequency": "MONTHLY", "amount_per_cycle": 1}
    )
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")
    user_id = session["user"]["id"]

    await _reset(admin_client, api_prefix, MONTH_START)
    await _reset(admin_client, api_prefix, MONTH_START)

    balance = await _balance(admin_client, api_prefix, user_id, leave_type["id"])
    assert balance["allocated"] == 1


async def test_the_reset_message_says_leave_restarts_too(admin_client, api_prefix):
    response = await admin_client.post(f"{api_prefix}/settings/tracking-start", json={})

    assert "leave" in response.json()["message"].lower()


# --------------------------------------------------------------------------- #
# after the reset
# --------------------------------------------------------------------------- #
async def test_leave_cannot_be_requested_for_before_go_live(admin_client, api_prefix):
    leave_type = await _leave_type(
        admin_client,
        api_prefix,
        accrual={"frequency": "YEARLY", "amount_per_cycle": 12},
        allow_negative_balance=True,
    )
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")
    await _reset(admin_client, api_prefix, TODAY)
    day = TODAY - timedelta(days=1)

    async with authed_client(session["tokens"]["access_token"]) as employee:
        response = await employee.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": day.isoformat(),
                "to_date": day.isoformat(),
                "day_part": "FULL",
                "reason": "Before the system existed",
            },
        )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "BEFORE_TRACKING_START"


async def test_withdrawing_a_request_from_before_go_live_leaves_the_balance_alone(
    db, admin_client, api_prefix
):
    leave_type = await _leave_type(
        admin_client, api_prefix, accrual={"frequency": "MONTHLY", "amount_per_cycle": 1}
    )
    session = await invite_and_accept(admin_client, api_prefix, email="ada@example.com")
    user_id = session["user"]["id"]
    await _balance(admin_client, api_prefix, user_id, leave_type["id"])
    request_id = await _request(
        db, user_id, leave_type, TODAY - timedelta(days=1), "PENDING"
    )
    await _reset(admin_client, api_prefix, TODAY)

    async with authed_client(session["tokens"]["access_token"]) as employee:
        response = await employee.post(f"{api_prefix}/leave/{request_id}/cancel")

    assert response.status_code == 200, response.text
    balance = await _balance(admin_client, api_prefix, user_id, leave_type["id"])
    assert (balance["pending"], balance["used"]) == (0, 0)


async def test_someone_joining_after_go_live_earns_from_their_joining(
    admin_client, api_prefix
):
    leave_type = await _leave_type(
        admin_client, api_prefix, accrual={"frequency": "MONTHLY", "amount_per_cycle": 1}
    )
    await _reset(admin_client, api_prefix, MONTH_START)
    session = await invite_and_accept(
        admin_client, api_prefix, email="new@example.com", joining_date=TODAY.isoformat()
    )

    balance = await _balance(admin_client, api_prefix, session["user"]["id"], leave_type["id"])

    last = (MONTH_START.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    served = (last - TODAY).days + 1
    assert balance["allocated"] == round(served / last.day, 2)
