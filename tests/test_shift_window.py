"""A shift is a planned window, not a permanent property of a person.

An administrator assigns a shift for a stretch of work — "the general shift for
these two weeks". Outside that window the employee has no active assignment,
which must never stop them punching: it is a signal to the administrator that
the window they planned has run out while the work has not finished.
"""

from datetime import date

from bson import ObjectId

from app.db.mongo import Collections
from app.services import shift as shift_service

INSIDE = date(2026, 8, 20)
BEFORE = date(2026, 8, 1)
AFTER = date(2026, 9, 15)

SHIFT_ID = "65f000000000000000000001"


def person(**fields) -> dict:
    return {"id": "u1", "shift_id": SHIFT_ID, **fields}


# --------------------------------------------------------------------------- #
# the window
# --------------------------------------------------------------------------- #
def test_a_day_inside_the_window_is_covered():
    user = person(shift_from="2026-08-17", shift_to="2026-08-28")

    assert shift_service.assignment_active(user, INSIDE) is True


def test_a_day_before_the_window_is_not():
    user = person(shift_from="2026-08-17", shift_to="2026-08-28")

    assert shift_service.assignment_active(user, BEFORE) is False


def test_a_day_after_the_window_is_not():
    """The case the whole feature exists for: the planned fortnight has run out."""
    user = person(shift_from="2026-08-17", shift_to="2026-08-28")

    assert shift_service.assignment_active(user, AFTER) is False


def test_both_ends_are_inclusive():
    user = person(shift_from="2026-08-17", shift_to="2026-08-28")

    assert shift_service.assignment_active(user, date(2026, 8, 17)) is True
    assert shift_service.assignment_active(user, date(2026, 8, 28)) is True


def test_no_dates_means_it_runs_until_it_is_changed():
    """How every assignment written before the window existed behaves."""
    assert shift_service.assignment_active(person(), INSIDE) is True


def test_an_open_ended_start_covers_everything_up_to_the_end():
    user = person(shift_to="2026-08-28")

    assert shift_service.assignment_active(user, BEFORE) is True
    assert shift_service.assignment_active(user, AFTER) is False


def test_an_open_ended_end_covers_everything_after_the_start():
    user = person(shift_from="2026-08-17")

    assert shift_service.assignment_active(user, BEFORE) is False
    assert shift_service.assignment_active(user, AFTER) is True


def test_nobody_with_no_shift_at_all_has_an_active_assignment():
    assert shift_service.assignment_active({"id": "u1"}, INSIDE) is False


def test_real_dates_are_read_as_well_as_strings():
    """Mongo hands dates back as datetimes; the form sends strings."""
    user = person(shift_from=date(2026, 8, 17), shift_to=date(2026, 8, 28))

    assert shift_service.assignment_active(user, INSIDE) is True
    assert shift_service.assignment_active(user, AFTER) is False


# --------------------------------------------------------------------------- #
# what a lapsed window does and does not do
# --------------------------------------------------------------------------- #
async def seed_shift(db, *, name: str = "General", default: bool = True) -> str:
    result = await db[Collections.SHIFTS].insert_one(
        {
            "name": name,
            "code": name[:4].upper(),
            "start_time": "09:30",
            "end_time": "18:30",
            "required_minutes": 480,
            "half_day_minutes": 240,
            "grace_minutes": 15,
            "break_minutes": 0,
            "working_weekdays": [0, 1, 2, 3, 4, 5],
            "is_default": default,
            "is_active": True,
            "is_deleted": False,
        }
    )
    return str(result.inserted_id)


async def test_timings_still_resolve_when_the_window_has_lapsed(db):
    """The day is still measurable — it falls back to the default shift.

    Nobody is blocked and nothing is left unjudged; the organisation's standard
    timings apply, which is exactly what was asked for.
    """
    default_id = await seed_shift(db)
    user = {"id": "u1", "shift_id": default_id, "shift_from": "2026-08-17", "shift_to": "2026-08-28"}

    resolved = await shift_service.resolve_for_user(user, on=AFTER)

    assert resolved is not None
    assert resolved["start_time"] == "09:30"


async def test_an_active_window_resolves_to_the_assigned_shift(db):
    await seed_shift(db, name="General", default=True)
    early = await seed_shift(db, name="Early", default=False)
    await db[Collections.SHIFTS].update_one(
        {"_id": ObjectId(early)}, {"$set": {"start_time": "07:00"}}
    )
    user = {"id": "u1", "shift_id": early, "shift_from": "2026-08-17", "shift_to": "2026-08-28"}

    resolved = await shift_service.resolve_for_user(user, on=INSIDE)

    assert resolved["start_time"] == "07:00"


async def test_a_lapsed_window_falls_back_rather_than_keeping_the_old_shift(db):
    await seed_shift(db, name="General", default=True)
    early = await seed_shift(db, name="Early", default=False)
    await db[Collections.SHIFTS].update_one(
        {"_id": ObjectId(early)}, {"$set": {"start_time": "07:00"}}
    )
    user = {"id": "u1", "shift_id": early, "shift_from": "2026-08-17", "shift_to": "2026-08-28"}

    resolved = await shift_service.resolve_for_user(user, on=AFTER)

    assert resolved["start_time"] == "09:30"


# --------------------------------------------------------------------------- #
# "who is on this shift" once assignments have windows
# --------------------------------------------------------------------------- #
async def put_on_shift(db, shift_id: str, *, email: str, to: str | None) -> None:
    await db[Collections.USERS].insert_one(
        {
            "email": email,
            "role": "EMPLOYEE",
            "status": "ACTIVE",
            "is_deleted": False,
            "shift_id": shift_id,
            "shift_from": "2026-01-01",
            "shift_to": to,
        }
    )


async def test_an_open_ended_assignment_counts(db):
    shift_id = await seed_shift(db)
    await put_on_shift(db, shift_id, email="open@example.com", to=None)

    assert await count_on(shift_id) == 1


async def test_a_window_that_has_already_ended_does_not(db):
    """It overstated the card's figure and blocked a deletion nobody relied on."""
    shift_id = await seed_shift(db)
    await put_on_shift(db, shift_id, email="past@example.com", to="2026-03-31")

    assert await count_on(shift_id) == 0


async def test_a_window_ending_today_still_counts(db):
    shift_id = await seed_shift(db)
    await put_on_shift(db, shift_id, email="today@example.com", to="2026-08-20")

    assert await count_on(shift_id, on=date(2026, 8, 20)) == 1


async def test_a_window_that_has_not_started_yet_counts(db):
    """Somebody is scheduled onto it next month — deleting it breaks that plan."""
    shift_id = await seed_shift(db)
    await put_on_shift(db, shift_id, email="future@example.com", to="2026-12-31")

    assert await count_on(shift_id, on=date(2026, 8, 20)) == 1


async def count_on(shift_id: str, *, on: date | None = None) -> int:
    return await shift_service.count_assigned(shift_id, on=on or date(2026, 8, 20))


# --------------------------------------------------------------------------- #
# the flag has to survive the trip to the API
# --------------------------------------------------------------------------- #
def test_the_register_row_carries_the_flag_through():
    """`to_day_out` builds the model field by field.

    The service set `no_shift_assigned` on the record and the schema declared
    it, so both halves typechecked — but the presenter in between never read
    the key, and every row went out `False`. Nothing but an end-to-end read
    would have caught it.
    """
    from app.services.attendance import to_day_out

    row = to_day_out(
        {
            "date": "2026-08-21",
            "status": "PRESENT",
            "sessions": [],
            "no_shift_assigned": True,
        }
    )

    assert row.no_shift_assigned is True


def test_a_row_that_says_nothing_about_it_is_not_flagged():
    from app.services.attendance import to_day_out

    row = to_day_out({"date": "2026-08-21", "status": "PRESENT", "sessions": []})

    assert row.no_shift_assigned is False


# --------------------------------------------------------------------------- #
# a lapse, not merely the absence of a plan
# --------------------------------------------------------------------------- #
def test_a_window_that_ended_is_a_lapse():
    user = person(shift_from="2026-08-03", shift_to="2026-08-14")

    assert shift_service.assignment_lapsed(user, date(2026, 8, 21)) is True


def test_never_having_been_assigned_is_not_a_lapse():
    """Otherwise an organisation that does not plan this way has every punched
    day in its exception feed, which is noise rather than a signal."""
    assert shift_service.assignment_lapsed({"id": "u1"}, date(2026, 8, 21)) is False


def test_a_standing_assignment_with_no_end_is_not_a_lapse():
    assert shift_service.assignment_lapsed(person(), date(2026, 8, 21)) is False


def test_a_window_still_running_is_not_a_lapse():
    user = person(shift_from="2026-08-17", shift_to="2026-08-28")

    assert shift_service.assignment_lapsed(user, date(2026, 8, 21)) is False


def test_the_last_day_of_the_window_is_not_yet_a_lapse():
    user = person(shift_from="2026-08-17", shift_to="2026-08-21")

    assert shift_service.assignment_lapsed(user, date(2026, 8, 21)) is False


def test_a_window_that_has_not_started_is_not_a_lapse():
    user = person(shift_from="2026-09-01", shift_to="2026-09-30")

    assert shift_service.assignment_lapsed(user, date(2026, 8, 21)) is False
