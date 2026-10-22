"""The daily register once there is a year of data: filters, spans and sorting.

Every row here is derived rather than stored — an absence is the *absence* of an
attendance document — so these tests seed punches directly and assert on what
the register decides, not on what Mongo holds.
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.core.datetime import date_key, org_today
from app.services.attendance import MAX_REGISTER_SPAN_DAYS
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


def _event(day: date, hhmm: str, *, device_class="DESKTOP", outside_office=False):
    """One punch. Times are UTC; the office runs on Asia/Kolkata, so 04:00 is 09:30."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    return {
        "at": datetime.combine(day, time(hour, minute), tzinfo=UTC),
        "device_class": device_class,
        "platform": "Windows",
        "outside_office": outside_office,
        "distance_metres": 4000 if outside_office else 20,
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
        [{"punch_in": _event(day, "06:00", outside_office=True), "punch_out": None}],
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
# the single day, unchanged
# --------------------------------------------------------------------------- #
async def test_the_register_without_parameters_still_answers_for_today(
    admin_client, api_prefix
):
    await _seed_shift(admin_client, api_prefix)
    await invite_and_accept(admin_client, api_prefix, email="one@example.com")
    await invite_and_accept(admin_client, api_prefix, email="two@example.com")

    rows, meta = await _register(admin_client, api_prefix)

    assert len(rows) == 3  # two employees plus the admin
    assert all(row["employee_name"] for row in rows)
    assert all(row["date"] == date_key(org_today()) for row in rows)
    assert meta["from_date"] == meta["to_date"] == date_key(org_today())
    assert meta["days"] == 1
    assert meta["filtered"] is False


async def test_the_day_parameter_selects_that_day_alone(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, meta = await _register(admin_client, api_prefix, day=date_key(day))

    assert len(rows) == 4
    assert {row["date"] for row in rows} == {date_key(day)}
    assert meta["employees"] == 4


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
async def test_search_matches_a_name_case_insensitively(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, meta = await _register(admin_client, api_prefix, day=date_key(day), search="lOvElAcE")

    assert _names(rows) == ["Ada Lovelace"]
    assert meta["employees"] == 1
    assert meta["filtered"] is True


async def test_search_matches_an_email_address(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, _ = await _register(admin_client, api_prefix, day=date_key(day), search="GRACE@EXAMPLE")

    assert _names(rows) == ["Grace Hopper"]


async def test_search_matches_an_employee_code(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, _ = await _register(admin_client, api_prefix, day=date_key(day), search="eng-003")

    assert _names(rows) == ["Alan Turing"]


async def test_search_matching_nobody_returns_nothing_and_says_so(
    admin_client, api_prefix, db
):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, meta = await _register(admin_client, api_prefix, day=date_key(day), search="nobody")

    assert rows == []
    assert meta["employees"] == 0
    assert meta["filtered"] is True


# --------------------------------------------------------------------------- #
# department and status
# --------------------------------------------------------------------------- #
async def test_filtering_by_department(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, meta = await _register(
        admin_client, api_prefix, day=date_key(day), department="Engineering"
    )

    assert sorted(_names(rows)) == ["Ada Lovelace", "Alan Turing"]
    assert meta["employees"] == 2


async def test_filtering_by_status(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    present, _ = await _register(
        admin_client, api_prefix, day=date_key(day), status="PRESENT"
    )
    half, _ = await _register(admin_client, api_prefix, day=date_key(day), status="HALF_DAY")
    absent, _ = await _register(admin_client, api_prefix, day=date_key(day), status="ABSENT")

    assert _names(present) == ["Ada Lovelace"]
    assert _names(half) == ["Grace Hopper"]
    assert _names(absent) == ["Test Admin"]


async def test_an_unknown_status_is_rejected(admin_client, api_prefix):
    response = await admin_client.get(
        f"{api_prefix}/attendance/register", params={"status": "NAPPING"}
    )

    assert response.status_code == 422


async def test_filters_combine(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, _ = await _register(
        admin_client,
        api_prefix,
        day=date_key(day),
        department="Engineering",
        status="MISSING_PUNCH_OUT",
    )

    assert _names(rows) == ["Alan Turing"]

    # The same status in the other department is nobody, not Alan again.
    empty, _ = await _register(
        admin_client,
        api_prefix,
        day=date_key(day),
        department="Design",
        status="MISSING_PUNCH_OUT",
    )
    assert empty == []


async def test_search_and_exceptions_combine(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, _ = await _register(
        admin_client, api_prefix, day=date_key(day), search="ada", exceptions_only=True
    )

    # Ada's day is clean, so searching for her inside the exception feed is empty.
    assert rows == []


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


async def test_exceptions_only_catches_an_absence(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, _ = await _register(
        admin_client, api_prefix, day=date_key(day), search="admin", exceptions_only=True
    )

    assert [row["status"] for row in rows] == ["ABSENT"]


async def test_exceptions_only_catches_a_missing_punch_out(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, _ = await _register(
        admin_client, api_prefix, day=date_key(day), search="alan", exceptions_only=True
    )

    assert [row["status"] for row in rows] == ["MISSING_PUNCH_OUT"]


async def test_exceptions_only_catches_a_late_arrival(admin_client, api_prefix, db):
    day = _register_day()
    people = await _office(admin_client, api_prefix, db, day)
    # A punctual half day: late is the only thing that can put this row in the feed.
    await db["attendance"].delete_many({"user_id": people["grace"]})
    await _seed_day(
        db,
        people["grace"],
        day,
        [{"punch_in": _event(day, "04:00"), "punch_out": _event(day, "09:00")}],
    )

    clean, _ = await _register(
        admin_client, api_prefix, day=date_key(day), search="grace", exceptions_only=True
    )
    assert clean == []

    await db["attendance"].delete_many({"user_id": people["grace"]})
    await _seed_day(
        db,
        people["grace"],
        day,
        [{"punch_in": _event(day, "05:00"), "punch_out": _event(day, "10:00")}],
    )

    rows, _ = await _register(
        admin_client, api_prefix, day=date_key(day), search="grace", exceptions_only=True
    )
    assert [row["is_late"] for row in rows] == [True]
    # Lateness does not change the verdict on the hours; it only flags the day.
    assert rows[0]["status"] == "HALF_DAY"


async def test_exceptions_only_catches_a_punch_from_outside_the_office(
    admin_client, api_prefix, db
):
    day = _register_day()
    people = await _office(admin_client, api_prefix, db, day)
    await db["attendance"].delete_many({"user_id": people["ada"]})
    await _seed_day(
        db,
        people["ada"],
        day,
        [
            {
                "punch_in": _event(day, "04:00", outside_office=True),
                "punch_out": _event(day, "13:00"),
            }
        ],
    )

    rows, _ = await _register(
        admin_client, api_prefix, day=date_key(day), search="ada", exceptions_only=True
    )

    # A full, punctual, desktop day — flagged solely on where the punch came from.
    assert [row["status"] for row in rows] == ["PRESENT"]
    assert rows[0]["sessions"][0]["punch_in"]["outside_office"] is True


async def test_exceptions_only_catches_a_punch_from_a_phone(admin_client, api_prefix, db):
    day = _register_day()
    people = await _office(admin_client, api_prefix, db, day)
    await db["attendance"].delete_many({"user_id": people["ada"]})
    await _seed_day(
        db,
        people["ada"],
        day,
        [
            {
                "punch_in": _event(day, "04:00", device_class="MOBILE"),
                "punch_out": _event(day, "13:00", device_class="MOBILE"),
            }
        ],
    )

    rows, _ = await _register(
        admin_client, api_prefix, day=date_key(day), search="ada", exceptions_only=True
    )

    assert [row["status"] for row in rows] == ["PRESENT"]
    assert rows[0]["sessions"][0]["punch_in"]["device_class"] == "MOBILE"


async def test_exceptions_only_leaves_a_clean_day_out(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, _ = await _register(
        admin_client, api_prefix, day=date_key(day), exceptions_only=True
    )

    assert "Ada Lovelace" not in _names(rows)


# --------------------------------------------------------------------------- #
# spans
# --------------------------------------------------------------------------- #
async def test_a_range_returns_every_day_for_every_employee(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)
    start = day - timedelta(days=2)

    rows, meta = await _register(
        admin_client, api_prefix, from_date=date_key(start), to_date=date_key(day)
    )

    assert meta["days"] == 3
    assert meta["employees"] == 4
    assert len(rows) == 12
    assert {row["date"] for row in rows} == {
        date_key(start),
        date_key(start + timedelta(days=1)),
        date_key(day),
    }


async def test_one_end_of_a_range_is_enough(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    rows, meta = await _register(admin_client, api_prefix, from_date=date_key(day))

    assert meta["from_date"] == meta["to_date"] == date_key(day)
    assert len(rows) == 4


async def test_a_range_longer_than_the_maximum_is_refused(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)
    start = day - timedelta(days=MAX_REGISTER_SPAN_DAYS)

    response = await admin_client.get(
        f"{api_prefix}/attendance/register",
        params={"from_date": date_key(start), "to_date": date_key(day)},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REGISTER_RANGE_TOO_LONG"


async def test_the_longest_allowed_range_is_accepted(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)
    start = day - timedelta(days=MAX_REGISTER_SPAN_DAYS - 1)

    _, meta = await _register(
        admin_client, api_prefix, from_date=date_key(start), to_date=date_key(day)
    )

    assert meta["days"] == MAX_REGISTER_SPAN_DAYS


async def test_a_backwards_range_is_refused(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    response = await admin_client.get(
        f"{api_prefix}/attendance/register",
        params={"from_date": date_key(day), "to_date": date_key(day - timedelta(days=3))},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REGISTER_RANGE_INVALID"


async def test_asking_for_a_day_and_a_range_at_once_is_refused(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    response = await admin_client.get(
        f"{api_prefix}/attendance/register",
        params={"day": date_key(day), "from_date": date_key(day)},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REGISTER_RANGE_AMBIGUOUS"


# --------------------------------------------------------------------------- #
# sorting
# --------------------------------------------------------------------------- #
async def test_sorting_by_employee_name_both_ways(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    ascending, _ = await _register(
        admin_client, api_prefix, day=date_key(day), sort="employee"
    )
    descending, _ = await _register(
        admin_client, api_prefix, day=date_key(day), sort="employee", direction="desc"
    )

    assert _names(ascending) == ["Ada Lovelace", "Alan Turing", "Grace Hopper", "Test Admin"]
    assert _names(descending) == list(reversed(_names(ascending)))


async def test_sorting_by_worked_minutes_both_ways(admin_client, api_prefix, db):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    ascending, _ = await _register(
        admin_client, api_prefix, day=date_key(day), sort="worked_minutes"
    )
    descending, _ = await _register(
        admin_client, api_prefix, day=date_key(day), sort="worked_minutes", direction="desc"
    )

    assert [row["worked_minutes"] for row in ascending] == sorted(
        row["worked_minutes"] for row in ascending
    )
    assert _names(ascending)[-1] == "Ada Lovelace"
    assert _names(descending)[0] == "Ada Lovelace"


async def test_sorting_by_first_punch_in_keeps_unpunched_days_last(
    admin_client, api_prefix, db
):
    day = _register_day()
    await _office(admin_client, api_prefix, db, day)

    ascending, _ = await _register(
        admin_client, api_prefix, day=date_key(day), sort="first_punch_in"
    )
    descending, _ = await _register(
        admin_client, api_prefix, day=date_key(day), sort="first_punch_in", direction="desc"
    )

    assert _names(ascending) == ["Ada Lovelace", "Grace Hopper", "Alan Turing", "Test Admin"]
    # Nobody punched for the admin, and "no time" is not an early time.
    assert _names(descending) == ["Alan Turing", "Grace Hopper", "Ada Lovelace", "Test Admin"]


async def test_an_unknown_sort_is_rejected(admin_client, api_prefix):
    response = await admin_client.get(
        f"{api_prefix}/attendance/register", params={"sort": "salary"}
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# access
# --------------------------------------------------------------------------- #
async def test_an_employee_cannot_read_the_register(admin_client, api_prefix, db):
    from tests.conftest import authed_client

    day = _register_day()
    await _office(admin_client, api_prefix, db, day)
    session = await invite_and_accept(admin_client, api_prefix, email="nosy@example.com")

    async with authed_client(session["tokens"]["access_token"]) as client:
        response = await client.get(
            f"{api_prefix}/attendance/register", params={"day": date_key(day)}
        )

    assert response.status_code == 403
