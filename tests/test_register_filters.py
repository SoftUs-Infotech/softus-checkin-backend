"""The daily register once there is a year of data: filters, spans and sorting.

Every row here is derived rather than stored — an absence is the *absence* of an
attendance document — so these tests seed punches directly and assert on what
the register decides, not on what Mongo holds.
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.core.datetime import date_key, org_today
from tests.conftest import invite_and_accept

pytestmark = pytest.mark.usefixtures("db")

SATURDAY = 5


def _register_day() -> date:
    """A recent day the office is certainly open.

    Saturdays are decided by organisation policy rather than by the shift, so a
    test that happened to run on a Sunday would otherwise assert against a
    weekly off and fail one day in seven.
    """
    day = org_today() - timedelta(days=1)
    while day.weekday() == SATURDAY:
        day -= timedelta(days=1)
    return day


async def _seed_shift(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/shifts",
        json={
            "name": "General",
            "code": "GENERAL",
            "start_time": "09:30",
            "end_time": "18:30",
            "required_minutes": 540,
            "half_day_minutes": 240,
            "grace_minutes": 0,
            "break_minutes": 0,
            "working_weekdays": [0, 1, 2, 3, 4, 5, 6],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _person(admin_client, api_prefix, *, email, first, last, department, code):
    session = await invite_and_accept(admin_client, api_prefix, email=email, first_name=first)
    user_id = session["user"]["id"]
    updated = await admin_client.patch(
        f"{api_prefix}/users/{user_id}",
        json={
            "first_name": first,
            "last_name": last,
            "department": department,
            "employee_code": code,
        },
    )
    assert updated.status_code == 200, updated.text
    return user_id


def _event(day: date, hhmm: str, *, device_class="DESKTOP"):
    """One punch. Times are UTC; the office runs on Asia/Kolkata, so 04:00 is 09:30."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    return {
        "at": datetime.combine(day, time(hour, minute), tzinfo=UTC),
        "device_class": device_class,
        "platform": "Windows",
        "source": "WEB",
    }


async def _seed_day(db, user_id: str, day: date, sessions: list[dict]):
    await db["attendance"].insert_one(
        {
            "user_id": user_id,
            "date": date_key(day),
            "sessions": sessions,
            "is_deleted": False,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )


async def _office(admin_client, api_prefix, db, day: date) -> dict[str, str]:
    """Three employees and one seeded day each — the fixture every filter reads.

    * Ada worked a clean full day.
    * Grace arrived late, on a phone, and left after eight hours.
    * Alan never punched out, from outside the office.
    * The administrator has nothing recorded, so the day counts as an absence.
    """
    await _seed_shift(admin_client, api_prefix)

    ada = await _person(
        admin_client,
        api_prefix,
        email="ada@example.com",
        first="Ada",
        last="Lovelace",
        department="Engineering",
        code="ENG-001",
    )
    grace = await _person(
        admin_client,
        api_prefix,
        email="grace@example.com",
        first="Grace",
        last="Hopper",
        department="Design",
        code="DES-002",
    )
    alan = await _person(
        admin_client,
        api_prefix,
        email="alan@example.com",
        first="Alan",
        last="Turing",
        department="Engineering",
        code="ENG-003",
    )

    await _seed_day(
        db, ada, day, [{"punch_in": _event(day, "04:00"), "punch_out": _event(day, "13:00")}]
    )
    await _seed_day(
        db,
        grace,
        day,
        [
            {
                "punch_in": _event(day, "05:00", device_class="MOBILE"),
                "punch_out": _event(day, "13:00", device_class="MOBILE"),
            }
        ],
    )
    await _seed_day(
        db,
        alan,
        day,
        [{"punch_in": _event(day, "06:00"), "punch_out": None}],
    )
    return {"ada": ada, "grace": grace, "alan": alan}


async def _register(admin_client, api_prefix, **params):
    response = await admin_client.get(f"{api_prefix}/attendance/register", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    return body["data"], body["meta"]


def _names(rows) -> list[str]:
    return [row["employee_name"] for row in rows]


# --------------------------------------------------------------------------- #
# exceptions
# --------------------------------------------------------------------------- #
async def test_exceptions_only_returns_every_kind_and_nothing_clean(
    admin_client, api_prefix, db
):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, meta = await _register(
        admin_client, api_prefix, day=date_key(day), exceptions_only=True
    )

    assert sorted(_names(rows)) == ["Alan Turing", "Grace Hopper", "Test Admin"]
    assert meta["filtered"] is True
