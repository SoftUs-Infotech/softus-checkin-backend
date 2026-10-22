"""Punching over HTTP: the gates, required location, idempotency, calendar."""

import pytest
from bson import ObjectId

from app.db.mongo import Collections
from tests.conftest import OFFICE_LOCATION, authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


async def _employee(admin_client, api_prefix, email="worker@example.com", desktop=True):
    session = await invite_and_accept(admin_client, api_prefix, email=email)
    return authed_client(session["tokens"]["access_token"], desktop=desktop), session


async def _seed_shift(admin_client, api_prefix, **overrides):
    payload = {
        "name": "General",
        "code": "GENERAL",
        "start_time": "09:30",
        "end_time": "18:30",
        "required_minutes": 540,
        "half_day_minutes": 240,
        "working_weekdays": [0, 1, 2, 3, 4, 5, 6],  # so tests pass on any weekday
        **overrides,
    }
    response = await admin_client.post(f"{api_prefix}/shifts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


# --------------------------------------------------------------------------- #
# the device gate
# --------------------------------------------------------------------------- #
async def test_punching_in_from_a_computer_succeeds(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "IN_PROGRESS"
    assert data["sessions"][0]["punch_in"]["device_class"] == "DESKTOP"


async def test_punching_in_from_a_phone_is_blocked(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, desktop=False)

    async with client:
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "MOBILE_DEVICE_BLOCKED"
    assert body["error"]["details"]["device_class"] == "MOBILE"


async def test_punching_out_from_a_phone_is_blocked_too(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    desktop, session = await _employee(admin_client, api_prefix)
    async with desktop:
        await desktop.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    phone = authed_client(session["tokens"]["access_token"], desktop=False)
    async with phone:
        response = await phone.post(
            f"{api_prefix}/attendance/punch-out", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 403


async def test_a_request_without_a_device_profile_is_refused(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        client.headers.pop("x-device-profile")
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 403


async def test_reading_the_calendar_from_a_phone_is_allowed(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, desktop=False)

    async with client:
        assert (await client.get(f"{api_prefix}/attendance/today")).status_code == 200
        assert (await client.get(f"{api_prefix}/attendance/calendar")).status_code == 200


async def test_an_administrator_can_turn_the_restriction_off(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await admin_client.patch(f"{api_prefix}/settings", json={"block_mobile_punch": False})
    client, _ = await _employee(admin_client, api_prefix, desktop=False)

    async with client:
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# location
# --------------------------------------------------------------------------- #
async def test_punching_without_a_location_is_refused(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        response = await client.post(f"{api_prefix}/attendance/punch-in", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "LOCATION_REQUIRED"


async def test_nothing_is_recorded_when_the_location_is_missing(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        await client.post(f"{api_prefix}/attendance/punch-in", json={})
        today = await client.get(f"{api_prefix}/attendance/today")

    assert today.json()["data"]["day"]["status"] == "NOT_STARTED"


async def test_the_location_is_stored_with_the_punch(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    punch = response.json()["data"]["sessions"][0]["punch_in"]
    assert punch["latitude"] == OFFICE_LOCATION["latitude"]
    assert punch["address"] == "Ahmedabad, Gujarat"


async def test_an_impossible_latitude_is_rejected(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        response = await client.post(
            f"{api_prefix}/attendance/punch-in",
            json={"location": {"latitude": 999, "longitude": 0}},
        )

    assert response.status_code == 422


async def test_location_can_be_made_optional_by_an_administrator(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await admin_client.patch(f"{api_prefix}/settings", json={"geolocation_required": False})
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        response = await client.post(f"{api_prefix}/attendance/punch-in", json={})

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# the punch cycle
# --------------------------------------------------------------------------- #
async def test_a_full_punch_cycle_records_a_session(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        out = await client.post(
            f"{api_prefix}/attendance/punch-out", json={"location": OFFICE_LOCATION}
        )

    assert out.status_code == 200
    data = out.json()["data"]
    assert data["sessions"][0]["punch_out"] is not None
    assert data["is_open"] is False


async def test_punching_in_twice_is_rejected(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        second = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ALREADY_PUNCHED_IN"


async def test_punching_out_without_punching_in_is_rejected(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        response = await client.post(
            f"{api_prefix}/attendance/punch-out", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NOT_PUNCHED_IN"


async def test_a_repeated_request_key_does_not_punch_twice(admin_client, api_prefix):
    """A double-clicked button or a retried request must not create two punches."""
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)
    body = {"location": OFFICE_LOCATION, "request_key": "same-key-for-both-calls"}

    async with client:
        first = await client.post(f"{api_prefix}/attendance/punch-in", json=body)
        await client.post(
            f"{api_prefix}/attendance/punch-out", json={"location": OFFICE_LOCATION}
        )
        # Same key again — the guard, not the "already punched in" check.
        repeat = await client.post(f"{api_prefix}/attendance/punch-in", json=body)

    assert first.status_code == 200
    assert repeat.status_code == 200
    assert len(repeat.json()["data"]["sessions"]) == 1


async def test_a_second_session_is_allowed_after_punching_out(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await admin_client.patch(f"{api_prefix}/settings", json={"min_punch_gap_seconds": 0})
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        await client.post(
            f"{api_prefix}/attendance/punch-out", json={"location": OFFICE_LOCATION}
        )
        again = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert again.status_code == 200
    assert len(again.json()["data"]["sessions"]) == 2


async def test_a_rapid_second_punch_is_throttled(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await admin_client.patch(f"{api_prefix}/settings", json={"min_punch_gap_seconds": 300})
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        await client.post(
            f"{api_prefix}/attendance/punch-out", json={"location": OFFICE_LOCATION}
        )
        too_soon = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert too_soon.status_code == 409
    assert too_soon.json()["error"]["code"] == "PUNCH_TOO_SOON"


async def test_multiple_sessions_can_be_disabled(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await admin_client.patch(
        f"{api_prefix}/settings",
        json={"allow_multiple_sessions": False, "min_punch_gap_seconds": 0},
    )
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        await client.post(
            f"{api_prefix}/attendance/punch-out", json={"location": OFFICE_LOCATION}
        )
        again = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "MULTIPLE_SESSIONS_DISABLED"


# --------------------------------------------------------------------------- #
# today and the calendar
# --------------------------------------------------------------------------- #
async def test_today_reports_what_the_widget_needs(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        before = (await client.get(f"{api_prefix}/attendance/today")).json()["data"]
        await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        after = (await client.get(f"{api_prefix}/attendance/today")).json()["data"]

    assert before["can_punch_in"] is True
    assert before["can_punch_out"] is False
    assert after["can_punch_in"] is False
    assert after["can_punch_out"] is True
    assert after["device_class"] == "DESKTOP"
    assert after["geolocation_required"] is True


async def test_a_holiday_tells_the_employee_not_to_punch(admin_client, api_prefix):
    from app.core.datetime import date_key, org_today

    await _seed_shift(admin_client, api_prefix)
    await admin_client.post(
        f"{api_prefix}/holidays", json={"date": date_key(org_today()), "name": "Festival"}
    )
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        today = (await client.get(f"{api_prefix}/attendance/today")).json()["data"]

    assert today["day"]["status"] == "HOLIDAY"
    assert today["can_punch_in"] is False
    assert "holiday" in today["blocked_reason"].lower()


async def test_the_calendar_covers_a_whole_month_with_a_legend(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        response = await client.get(
            f"{api_prefix}/attendance/calendar", params={"year": 2026, "month": 1}
        )

    data = response.json()["data"]
    assert len(data["days"]) == 31
    assert data["from_date"] == "2026-01-01"
    assert data["legend"]["PRESENT"] == "green"
    assert data["legend"]["ABSENT"] == "red"
    assert data["labels"]["MISSING_PUNCH_OUT"] == "Missing punch-out"


async def test_the_calendar_summarises_the_month(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        response = await client.get(f"{api_prefix}/attendance/calendar")

    summary = response.json()["data"]["summary"]
    assert summary["working_days"] > 0
    assert summary["expected_minutes"] > 0


async def test_an_employee_cannot_read_someone_elses_calendar(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="nosy2@example.com")
    other = await invite_and_accept(admin_client, api_prefix, email="other@example.com")

    async with client:
        response = await client.get(
            f"{api_prefix}/attendance/calendar", params={"user_id": other["user"]["id"]}
        )

    assert response.status_code == 403


async def test_an_administrator_can_read_any_calendar(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    other = await invite_and_accept(admin_client, api_prefix, email="viewed@example.com")

    response = await admin_client.get(
        f"{api_prefix}/attendance/calendar", params={"user_id": other["user"]["id"]}
    )

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# administration
# --------------------------------------------------------------------------- #
async def test_an_administrator_can_rewrite_a_day(admin_client, api_prefix):
    from app.core.datetime import date_key, org_today

    await _seed_shift(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="fixme@example.com")
    day = date_key(org_today())

    response = await admin_client.put(
        f"{api_prefix}/attendance/employee/{session['user']['id']}/{day}",
        json={
            "sessions": [
                {"punch_in": f"{day}T04:00:00Z", "punch_out": f"{day}T13:00:00Z"}
            ],
            "note": "Employee forgot to punch; verified with the team lead.",
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["worked_minutes"] == 540
    assert data["status"] == "PRESENT"
    assert data["is_manually_adjusted"] is True


async def test_an_adjustment_is_written_to_the_audit_trail(admin_client, api_prefix):
    from app.core.datetime import date_key, org_today

    await _seed_shift(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="audited2@example.com")
    day = date_key(org_today())

    await admin_client.put(
        f"{api_prefix}/attendance/employee/{session['user']['id']}/{day}",
        json={
            "sessions": [{"punch_in": f"{day}T04:00:00Z", "punch_out": f"{day}T13:00:00Z"}],
            "note": "Verified with the team lead.",
        },
    )

    logs = await admin_client.get(
        f"{api_prefix}/audit-logs", params={"entity": "ATTENDANCE"}
    )
    entries = logs.json()["data"]["items"]
    assert entries and entries[0]["action"] == "ADJUST"
    assert entries[0]["after"]["note"] == "Verified with the team lead."


async def test_overlapping_sessions_are_rejected(admin_client, api_prefix):
    from app.core.datetime import date_key, org_today

    await _seed_shift(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="overlap@example.com")
    day = date_key(org_today())

    response = await admin_client.put(
        f"{api_prefix}/attendance/employee/{session['user']['id']}/{day}",
        json={
            "sessions": [
                {"punch_in": f"{day}T04:00:00Z", "punch_out": f"{day}T10:00:00Z"},
                {"punch_in": f"{day}T09:00:00Z", "punch_out": f"{day}T13:00:00Z"},
            ],
            "note": "Overlapping on purpose.",
        },
    )

    assert response.status_code == 422


async def test_a_punch_out_before_a_punch_in_is_rejected(admin_client, api_prefix):
    from app.core.datetime import date_key, org_today

    await _seed_shift(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="reversed@example.com")
    day = date_key(org_today())

    response = await admin_client.put(
        f"{api_prefix}/attendance/employee/{session['user']['id']}/{day}",
        json={
            "sessions": [{"punch_in": f"{day}T13:00:00Z", "punch_out": f"{day}T04:00:00Z"}],
            "note": "Reversed on purpose.",
        },
    )

    assert response.status_code == 422


async def test_an_employee_cannot_adjust_attendance(admin_client, api_prefix):
    from app.core.datetime import date_key, org_today

    await _seed_shift(admin_client, api_prefix)
    client, session = await _employee(admin_client, api_prefix, email="cheat@example.com")
    day = date_key(org_today())

    async with client:
        response = await client.put(
            f"{api_prefix}/attendance/employee/{session['user']['id']}/{day}",
            json={
                "sessions": [{"punch_in": f"{day}T04:00:00Z", "punch_out": f"{day}T13:00:00Z"}],
                "note": "Giving myself a full day.",
            },
        )

    assert response.status_code == 403


async def test_the_daily_register_lists_every_active_employee(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await invite_and_accept(admin_client, api_prefix, email="one@example.com")
    await invite_and_accept(admin_client, api_prefix, email="two@example.com")

    response = await admin_client.get(f"{api_prefix}/attendance/register")

    assert response.status_code == 200
    rows = response.json()["data"]
    assert len(rows) == 3  # two employees plus the admin
    assert all(row["employee_name"] for row in rows)


# --------------------------------------------------------------------------- #
# the auto-close job
# --------------------------------------------------------------------------- #
async def test_a_forgotten_punch_out_becomes_a_missing_punch_out(admin_client, api_prefix, db):
    from datetime import timedelta

    from app.core.datetime import date_key, org_today, utc_now
    from app.services.attendance import close_stale_sessions

    await _seed_shift(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="forgot@example.com")
    yesterday = org_today() - timedelta(days=1)

    await db["attendance"].insert_one(
        {
            "user_id": session["user"]["id"],
            "date": date_key(yesterday),
            "sessions": [{"punch_in": {"at": utc_now() - timedelta(days=1)}, "punch_out": None}],
            "status": "IN_PROGRESS",
            "is_deleted": False,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
    )

    closed = await close_stale_sessions()

    assert closed == 1
    record = await db["attendance"].find_one({"date": date_key(yesterday)})
    assert record["status"] == "MISSING_PUNCH_OUT"
    assert record["sessions"][0]["auto_closed"] is True
    # No punch-out time is invented — that is what a correction request is for.
    assert record["sessions"][0]["punch_out"] is None


async def test_the_auto_close_job_leaves_today_alone(admin_client, api_prefix):
    from app.services.attendance import close_stale_sessions

    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="working@example.com")

    async with client:
        await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        closed = await close_stale_sessions()
        today = (await client.get(f"{api_prefix}/attendance/today")).json()["data"]

    assert closed == 0
    assert today["day"]["status"] == "IN_PROGRESS"


async def test_the_auto_close_job_is_safe_to_run_twice(admin_client, api_prefix, db):
    from datetime import timedelta

    from app.core.datetime import date_key, org_today, utc_now
    from app.services.attendance import close_stale_sessions

    await _seed_shift(admin_client, api_prefix)
    session = await invite_and_accept(admin_client, api_prefix, email="twice2@example.com")
    await db["attendance"].insert_one(
        {
            "user_id": session["user"]["id"],
            "date": date_key(org_today() - timedelta(days=2)),
            "sessions": [{"punch_in": {"at": utc_now() - timedelta(days=2)}, "punch_out": None}],
            "is_deleted": False,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
    )

    assert await close_stale_sessions() == 1
    assert await close_stale_sessions() == 0


# --------------------------------------------------------------------------- #
# the office network
# --------------------------------------------------------------------------- #
OFFICE_IP = "203.0.113.7"
HOME_IP = "198.51.100.24"


async def _set_flags(db, user_id, **flags):
    """Set fields the user API does not expose yet, straight on the document."""
    await db[Collections.USERS].update_one({"_id": ObjectId(user_id)}, {"$set": flags})


async def _enforce_network(admin_client, api_prefix, ranges, **extra):
    response = await admin_client.patch(
        f"{api_prefix}/settings",
        json={
            "enforce_office_network_for_punch": True,
            "office_ip_ranges": ranges,
            **extra,
        },
    )
    assert response.status_code == 200, response.text


async def test_punching_from_the_office_network_is_allowed(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await _enforce_network(admin_client, api_prefix, [OFFICE_IP])
    client, _ = await _employee(admin_client, api_prefix, email="onsite@example.com")

    async with client:
        client.headers["x-forwarded-for"] = OFFICE_IP
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 200, response.text


async def test_punching_from_outside_the_office_network_is_refused(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await _enforce_network(admin_client, api_prefix, [OFFICE_IP])
    client, _ = await _employee(admin_client, api_prefix, email="offsite@example.com")

    async with client:
        client.headers["x-forwarded-for"] = HOME_IP
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "OFFICE_NETWORK_REQUIRED"
    # The refusal has to name the way out, or it becomes a support ticket.
    assert "correction" in body["message"].lower()


async def test_a_cidr_range_covers_the_whole_office(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await _enforce_network(admin_client, api_prefix, ["203.0.113.0/24"])
    client, _ = await _employee(admin_client, api_prefix, email="cidr@example.com")

    async with client:
        client.headers["x-forwarded-for"] = "203.0.113.190"
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 200, response.text


async def test_enforcement_with_an_empty_allowlist_lets_nobody_through(admin_client, api_prefix):
    """Enforcement must not pass vacuously the moment it is switched on."""
    await _seed_shift(admin_client, api_prefix)
    await admin_client.patch(
        f"{api_prefix}/settings", json={"enforce_office_network_for_punch": True}
    )
    client, _ = await _employee(admin_client, api_prefix, email="vacuous@example.com")

    async with client:
        client.headers["x-forwarded-for"] = OFFICE_IP
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "OFFICE_NETWORK_REQUIRED"


async def test_a_malformed_range_does_not_stop_the_office_punching_in(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await _enforce_network(admin_client, api_prefix, ["office-wifi", "203.0.113.0/24"])
    client, _ = await _employee(admin_client, api_prefix, email="typo@example.com")

    async with client:
        client.headers["x-forwarded-for"] = OFFICE_IP
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 200, response.text


async def test_the_restriction_is_off_until_an_administrator_turns_it_on(
    admin_client, api_prefix
):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="anywhere@example.com")

    async with client:
        client.headers["x-forwarded-for"] = HOME_IP
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 200, response.text


async def test_punching_out_is_gated_by_the_network_too(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await _enforce_network(admin_client, api_prefix, [OFFICE_IP])
    client, _ = await _employee(admin_client, api_prefix, email="halfway@example.com")

    async with client:
        client.headers["x-forwarded-for"] = OFFICE_IP
        await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )
        client.headers["x-forwarded-for"] = HOME_IP
        response = await client.post(
            f"{api_prefix}/attendance/punch-out", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "OFFICE_NETWORK_REQUIRED"


async def test_an_employee_working_from_home_punches_from_anywhere(admin_client, api_prefix, db):
    await _seed_shift(admin_client, api_prefix)
    await _enforce_network(admin_client, api_prefix, [OFFICE_IP])
    client, session = await _employee(admin_client, api_prefix, email="remote@example.com")
    await _set_flags(db, session["user"]["id"], wfh_enabled=True)

    async with client:
        client.headers["x-forwarded-for"] = HOME_IP
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 200, response.text


async def test_an_expired_work_from_home_window_no_longer_exempts_anyone(
    admin_client, api_prefix, db
):
    from datetime import timedelta

    from app.core.datetime import date_key, org_today

    await _seed_shift(admin_client, api_prefix)
    await _enforce_network(admin_client, api_prefix, [OFFICE_IP])
    client, session = await _employee(admin_client, api_prefix, email="expired@example.com")
    await _set_flags(
        db,
        session["user"]["id"],
        wfh_enabled=True,
        wfh_from=date_key(org_today() - timedelta(days=30)),
        wfh_to=date_key(org_today() - timedelta(days=1)),
    )

    async with client:
        client.headers["x-forwarded-for"] = HOME_IP
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "OFFICE_NETWORK_REQUIRED"


async def test_filing_a_correction_can_be_restricted_to_the_office_network(
    admin_client, api_prefix
):
    from app.core.datetime import date_key, org_today

    await _seed_shift(admin_client, api_prefix)
    await admin_client.patch(
        f"{api_prefix}/settings",
        json={
            "enforce_office_network_for_corrections": True,
            "office_ip_ranges": [OFFICE_IP],
        },
    )
    client, _ = await _employee(admin_client, api_prefix, email="regularise@example.com")
    day = date_key(org_today())
    body = {
        "date": day,
        "type": "MISSED_PUNCH_IN",
        "requested_punch_in": f"{day}T04:00:00Z",
        "reason": "I forgot to punch in this morning.",
    }

    async with client:
        client.headers["x-forwarded-for"] = HOME_IP
        refused = await client.post(f"{api_prefix}/corrections", json=body)
        client.headers["x-forwarded-for"] = OFFICE_IP
        allowed = await client.post(f"{api_prefix}/corrections", json=body)

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "OFFICE_NETWORK_REQUIRED"
    assert allowed.status_code == 201, allowed.text


# --------------------------------------------------------------------------- #
# accounts that may not punch at all
# --------------------------------------------------------------------------- #
async def test_an_account_without_permission_cannot_punch(admin_client, api_prefix, db):
    await _seed_shift(admin_client, api_prefix)
    client, session = await _employee(admin_client, api_prefix, email="deskless@example.com")
    await _set_flags(db, session["user"]["id"], can_punch=False)

    async with client:
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PUNCH_NOT_PERMITTED"


async def test_the_widget_tells_an_account_that_it_may_not_punch(admin_client, api_prefix, db):
    await _seed_shift(admin_client, api_prefix)
    client, session = await _employee(admin_client, api_prefix, email="observer@example.com")
    await _set_flags(db, session["user"]["id"], can_punch=False)

    async with client:
        today = (await client.get(f"{api_prefix}/attendance/today")).json()["data"]

    assert today["can_punch_in"] is False
    assert today["can_punch_out"] is False
    assert "not set up to punch" in today["blocked_reason"]


# --------------------------------------------------------------------------- #
# what every punch records
# --------------------------------------------------------------------------- #
async def test_a_punch_records_the_ip_device_and_operating_system(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="traced@example.com")

    async with client:
        client.headers["x-forwarded-for"] = OFFICE_IP
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    punch = response.json()["data"]["sessions"][0]["punch_in"]
    assert punch["ip"] == OFFICE_IP
    assert punch["device_class"] == "DESKTOP"
    assert punch["platform"] == "Windows"
    assert "Chrome/131" in punch["user_agent"]


# --------------------------------------------------------------------------- #
# the geofence — a flag, never a refusal
# --------------------------------------------------------------------------- #
async def _set_geofence(admin_client, api_prefix, *, latitude, longitude, radius=200):
    response = await admin_client.patch(
        f"{api_prefix}/settings",
        json={
            "office_latitude": latitude,
            "office_longitude": longitude,
            "office_radius_metres": radius,
        },
    )
    assert response.status_code == 200, response.text


async def test_a_punch_inside_the_geofence_is_not_flagged(admin_client, api_prefix):
    await _seed_shift(admin_client, api_prefix)
    await _set_geofence(
        admin_client,
        api_prefix,
        latitude=OFFICE_LOCATION["latitude"],
        longitude=OFFICE_LOCATION["longitude"],
    )
    client, _ = await _employee(admin_client, api_prefix, email="atdesk@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    punch = response.json()["data"]["sessions"][0]["punch_in"]
    assert punch["outside_office"] is False
    assert punch["distance_metres"] == 0


async def test_a_punch_outside_the_geofence_is_flagged_but_still_recorded(
    admin_client, api_prefix
):
    """Flagged, never blocked — a bad GPS fix must not cost somebody their day."""
    await _seed_shift(admin_client, api_prefix)
    await _set_geofence(
        admin_client,
        api_prefix,
        latitude=OFFICE_LOCATION["latitude"] + 0.05,  # about five kilometres away
        longitude=OFFICE_LOCATION["longitude"],
    )
    client, _ = await _employee(admin_client, api_prefix, email="faraway@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "IN_PROGRESS"
    punch = data["sessions"][0]["punch_in"]
    assert punch["outside_office"] is True
    assert punch["distance_metres"] > 5000


async def test_no_distance_is_recorded_until_the_office_has_coordinates(
    admin_client, api_prefix
):
    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="unmapped@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": OFFICE_LOCATION}
        )

    punch = response.json()["data"]["sessions"][0]["punch_in"]
    assert punch["distance_metres"] is None
    assert punch["outside_office"] is False


# --------------------------------------------------------------------------- #
# geocoding a punch
# --------------------------------------------------------------------------- #
async def test_a_punch_without_an_address_is_geocoded(admin_client, api_prefix, monkeypatch):
    from app.core.config import settings as app_settings
    from app.services import geocode

    async def fake_fetch(latitude, longitude):
        return geocode.Place(address="12 Ashram Road, Ahmedabad", city="Ahmedabad")

    monkeypatch.setattr(app_settings, "GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(geocode, "_fetch_place", fake_fetch)

    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="located@example.com")
    location = {key: value for key, value in OFFICE_LOCATION.items() if key != "address"}

    async with client:
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": location}
        )

    punch = response.json()["data"]["sessions"][0]["punch_in"]
    assert punch["address"] == "12 Ashram Road, Ahmedabad"
    # The city is what the register column shows, so it has to survive the trip
    # out through the presenter as well as being stored.
    assert punch["city"] == "Ahmedabad"


async def test_a_failing_geocoder_does_not_fail_the_punch(admin_client, api_prefix, monkeypatch):
    import httpx

    from app.core.config import settings as app_settings
    from app.services import geocode

    async def explode(latitude, longitude):
        raise httpx.ReadTimeout("the geocoding API took too long")

    monkeypatch.setattr(app_settings, "GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(geocode, "_fetch_place", explode)

    await _seed_shift(admin_client, api_prefix)
    client, _ = await _employee(admin_client, api_prefix, email="unlocated@example.com")
    location = {key: value for key, value in OFFICE_LOCATION.items() if key != "address"}

    async with client:
        response = await client.post(
            f"{api_prefix}/attendance/punch-in", json={"location": location}
        )

    assert response.status_code == 200, response.text
    punch = response.json()["data"]["sessions"][0]["punch_in"]
    assert punch["address"] is None
    assert punch["latitude"] == OFFICE_LOCATION["latitude"]
