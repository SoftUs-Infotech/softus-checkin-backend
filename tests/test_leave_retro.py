"""Approving leave after its month has closed."""

from datetime import date, datetime, timedelta

import pytest
from bson import ObjectId

from app.core.datetime import date_key, org_today
from app.db.mongo import Collections
from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


async def _seed(admin_client, api_prefix) -> dict:
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
        json={"name": "Casual Leave", "code": "CL", "annual_quota": 12},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _employee(admin_client, api_prefix, email):
    session = await invite_and_accept(admin_client, api_prefix, email=email)
    return authed_client(session["tokens"]["access_token"]), session


def _last_month() -> date:
    """The fifteenth of the month before this one."""
    return (org_today().replace(day=1) - timedelta(days=1)).replace(day=15)


async def _apply(client, api_prefix, leave_type, from_date, to_date=None) -> str:
    async with client:
        response = await client.post(
            f"{api_prefix}/leave",
            json={
                "leave_type_id": leave_type["id"],
                "from_date": date_key(from_date),
                "to_date": date_key(to_date or from_date),
                "reason": "Was unwell and told the office by phone.",
            },
        )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


async def _adjustments(db) -> list[dict]:
    return await db[Collections.SALARY_ADJUSTMENTS].find({}).to_list(length=10)


# --------------------------------------------------------------------------- #
# the adjustment line
# --------------------------------------------------------------------------- #
async def test_a_late_approval_posts_the_days_into_the_current_month(
    db, admin_client, api_prefix
):
    leave_type = await _seed(admin_client, api_prefix)
    client, session = await _employee(admin_client, api_prefix, "late@example.com")
    day = _last_month()
    request_id = await _apply(client, api_prefix, leave_type, day)

    approved = await admin_client.post(
        f"{api_prefix}/leave/{request_id}/approve", json={"note": "Confirmed."}
    )
    assert approved.status_code == 200, approved.text

    adjustments = await _adjustments(db)
    assert len(adjustments) == 1
    adjustment = adjustments[0]
    assert set(adjustment) == {
        "_id",
        "user_id",
        "apply_month",
        "source_month",
        "source_date",
        "kind",
        "days",
        "reason",
        "reference_type",
        "reference_id",
        "created_by",
        "created_at",
    }
    assert adjustment["user_id"] == ObjectId(session["user"]["id"])
    assert adjustment["apply_month"] == date_key(org_today())[:7]
    assert adjustment["source_month"] == date_key(day)[:7]
    assert adjustment["source_date"] == date_key(day)
    assert adjustment["kind"] == "LEAVE_APPROVED_LATE"
    assert adjustment["days"] == 1.0  # a deduction to pay back, not money
    assert adjustment["reference_type"] == "leave_request"
    assert adjustment["reference_id"] == ObjectId(request_id)
    assert isinstance(adjustment["created_by"], ObjectId)
    assert isinstance(adjustment["created_at"], datetime)
    assert "Casual Leave" in adjustment["reason"]


async def test_only_closed_months_are_adjusted(db, admin_client, api_prefix):
    """A request straddling the month end pays back the closed part only."""
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, "straddle@example.com")
    last_day = org_today().replace(day=1) - timedelta(days=1)
    request_id = await _apply(
        client, api_prefix, leave_type,
        last_day - timedelta(days=1), org_today().replace(day=1) + timedelta(days=1),
    )

    await admin_client.post(f"{api_prefix}/leave/{request_id}/approve", json={})

    adjustments = await _adjustments(db)
    assert len(adjustments) == 1
    assert adjustments[0]["source_month"] == date_key(last_day)[:7]
    assert adjustments[0]["days"] == 2.0
    assert adjustments[0]["source_date"] == date_key(last_day - timedelta(days=1))


async def test_leave_in_the_current_month_needs_no_adjustment(
    db, admin_client, api_prefix
):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, "ontime@example.com")
    request_id = await _apply(client, api_prefix, leave_type, org_today())

    await admin_client.post(f"{api_prefix}/leave/{request_id}/approve", json={})

    assert await _adjustments(db) == []


# --------------------------------------------------------------------------- #
# the window
# --------------------------------------------------------------------------- #
async def test_an_approval_beyond_the_window_is_refused(db, admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, "toolate@example.com")
    request_id = await _apply(client, api_prefix, leave_type, _last_month())
    await admin_client.patch(
        f"{api_prefix}/settings", json={"retro_approval_months": 0}
    )

    response = await admin_client.post(
        f"{api_prefix}/leave/{request_id}/approve", json={}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "RETRO_APPROVAL_TOO_OLD"
    assert body["error"]["details"]["months_back"] == 1
    # Nothing moved: no adjustment, and the request is still waiting.
    assert await _adjustments(db) == []
    still = await admin_client.get(f"{api_prefix}/leave")
    assert still.json()["data"]["items"][0]["status"] == "PENDING"


async def test_an_approval_inside_the_window_is_allowed(db, admin_client, api_prefix):
    leave_type = await _seed(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, "justintime@example.com")
    request_id = await _apply(client, api_prefix, leave_type, _last_month())
    await admin_client.patch(
        f"{api_prefix}/settings", json={"retro_approval_months": 1}
    )

    response = await admin_client.post(
        f"{api_prefix}/leave/{request_id}/approve", json={}
    )

    assert response.status_code == 200
    assert len(await _adjustments(db)) == 1


# --------------------------------------------------------------------------- #
# the calendar
# --------------------------------------------------------------------------- #
async def test_a_late_approval_recomputes_the_month_it_belongs_to(
    admin_client, api_prefix
):
    leave_type = await _seed(admin_client, api_prefix)
    client, session = await _employee(admin_client, api_prefix, "recompute@example.com")
    day = _last_month()
    request_id = await _apply(client, api_prefix, leave_type, day)

    await admin_client.post(f"{api_prefix}/leave/{request_id}/approve", json={})

    calendar = await admin_client.get(
        f"{api_prefix}/attendance/calendar",
        params={"user_id": session["user"]["id"], "year": day.year, "month": day.month},
    )
    entry = next(
        item for item in calendar.json()["data"]["days"] if item["date"] == date_key(day)
    )
    assert entry["status"] == "ON_LEAVE"
