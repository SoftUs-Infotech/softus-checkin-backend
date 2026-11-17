"""Breaks: the time between one punch-out and the next punch-in on a day."""

from datetime import UTC, datetime, time, timedelta

import pytest

from app.core.config import settings
from app.core.datetime import org_today
from app.db.mongo import Collections
from tests.conftest import invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


def _at(day, hour, minute=0) -> str:
    """A local wall-clock time on ``day``, as the UTC ISO string the API takes."""
    return (
        datetime.combine(day, time(hour, minute), tzinfo=settings.org_tz)
        .astimezone(UTC)
        .isoformat()
    )


async def test_each_gap_between_sessions_is_a_break_with_its_length(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="breaks@example.com")
    user_id = session["user"]["id"]
    day = org_today() - timedelta(days=7)

    adjusted = await admin_client.put(
        f"{api_prefix}/attendance/employee/{user_id}/{day.isoformat()}",
        json={
            "note": "Three sessions with lunch and tea in between.",
            "sessions": [
                {"punch_in": _at(day, 9, 30), "punch_out": _at(day, 13, 0)},
                {"punch_in": _at(day, 13, 45), "punch_out": _at(day, 16, 30)},
                {"punch_in": _at(day, 16, 45), "punch_out": _at(day, 19, 0)},
            ],
        },
    )
    assert adjusted.status_code == 200, adjusted.text

    response = await admin_client.get(
        f"{api_prefix}/attendance/employee/{user_id}",
        params={"from_date": day.isoformat(), "to_date": day.isoformat()},
    )
    [record] = response.json()["data"]

    assert [gap["minutes"] for gap in record["breaks"]] == [45, 15]
    assert {gap["kind"] for gap in record["breaks"]} == {"BREAK"}
    assert record["break_minutes"] == 60
    assert record["unshared_minutes"] == 0
    assert record["worked_minutes"] == 210 + 165 + 135


async def test_time_after_sharing_stopped_is_not_counted_as_a_break(
    admin_client, api_prefix, db
):
    session = await invite_and_accept(admin_client, api_prefix, email="away@example.com")
    user_id = session["user"]["id"]
    day = org_today() - timedelta(days=7)
    await admin_client.put(
        f"{api_prefix}/attendance/employee/{user_id}/{day.isoformat()}",
        json={
            "note": "Sharing stopped once, then a real break.",
            "sessions": [
                {"punch_in": _at(day, 9, 0), "punch_out": _at(day, 11, 0)},
                {"punch_in": _at(day, 11, 20), "punch_out": _at(day, 13, 0)},
                {"punch_in": _at(day, 14, 0), "punch_out": _at(day, 18, 0)},
            ],
        },
    )
    # The first session was ended by the system, as it is for somebody working
    # from home whose screen stopped being shared.
    await db[Collections.ATTENDANCE].update_one(
        {"user_id": user_id, "date": day.isoformat()},
        {"$set": {"sessions.0.closed_reason": "SCREEN_SHARE_STOPPED"}},
    )

    response = await admin_client.get(
        f"{api_prefix}/attendance/employee/{user_id}",
        params={"from_date": day.isoformat(), "to_date": day.isoformat()},
    )
    [record] = response.json()["data"]

    assert [(gap["kind"], gap["minutes"]) for gap in record["breaks"]] == [
        ("SCREEN_SHARE_STOPPED", 20),
        ("BREAK", 60),
    ]
    assert record["break_minutes"] == 60
    assert record["unshared_minutes"] == 20
