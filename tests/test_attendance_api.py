"""Punching over HTTP: the gates, required location, idempotency, calendar."""

import pytest

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


# --------------------------------------------------------------------------- #
# where a punch may come from
# --------------------------------------------------------------------------- #
async def test_any_computer_may_punch_whatever_the_old_office_settings_say(
    admin_client, api_prefix, db
):
    # Production still holds the office-network and office-location switches,
    # turned on, from before they were removed. They must not stop anybody.
    from app.models.settings import SETTINGS_KEY
    from app.services import settings as settings_service

    await _seed_shift(admin_client, api_prefix)
    await admin_client.get(f"{api_prefix}/settings")  # the settings document exists
    stored = await db[Collections.SETTINGS].update_one(
        {"key": SETTINGS_KEY},
        {
            "$set": {
                "enforce_office_network_for_punch": True,
                "enforce_office_network_for_corrections": True,
                "office_ip_ranges": ["203.0.113.7"],
                "enforce_office_location_for_punch": True,
                "office_latitude": 21.234394,
                "office_longitude": 72.869334,
                "office_radius_metres": 50,
            }
        },
    )
    assert stored.modified_count == 1
    settings_service.clear_cache()
    client, _ = await _employee(admin_client, api_prefix, email="home@example.com")

    async with client:
        client.headers["x-forwarded-for"] = "198.51.100.24"  # a home connection
        response = await client.post(
            f"{api_prefix}/attendance/punch-in",
            json={"location": {"latitude": 19.076, "longitude": 72.8777}},  # another city
        )

    assert response.status_code == 200, response.text
