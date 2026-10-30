"""Punching only at the office, decided by location rather than by IP.

The office's public IP changes whenever the router restarts or the power goes,
so an IP allowlist locks the office out after every power cut. The geofence
does not care which IP the office has today: a punch counts as "at the office"
when the browser's reading is within the radius of the office point.

The office point here is what an office desktop in Surat reported — a wired
PC with no GPS, ±210 m, the same point on every reading — and the radius is
the 50 m the organisation asked for.
"""

import pytest
from bson import ObjectId

from app.db.mongo import Collections
from app.utils.network import location_view
from tests.conftest import authed_client, invite_and_accept

OFFICE = {"latitude": 21.234394, "longitude": 72.869334}
AT_THE_OFFICE = {**OFFICE, "accuracy_metres": 210}
# 0.00027° of latitude is about 30 m; 0.0009° about 100 m.
ACROSS_THE_ROAD = {"latitude": 21.234664, "longitude": 72.869334, "accuracy_metres": 210}
DOWN_THE_STREET = {"latitude": 21.235294, "longitude": 72.869334, "accuracy_metres": 210}
AT_HOME = {"latitude": 21.1702, "longitude": 72.8311, "accuracy_metres": 30}  # ~8 km

GEOFENCE = {
    "office_latitude": OFFICE["latitude"],
    "office_longitude": OFFICE["longitude"],
    "office_radius_metres": 50,
}
SHIFT = {
    "name": "General",
    "code": "GENERAL",
    "start_time": "09:30",
    "end_time": "18:30",
    "required_minutes": 540,
    "half_day_minutes": 240,
    "working_weekdays": [0, 1, 2, 3, 4, 5, 6],
}


# --------------------------------------------------------------------------- #
# the rule, without a database
# --------------------------------------------------------------------------- #
def test_the_office_reading_itself_is_inside():
    view = location_view(GEOFENCE, **AT_THE_OFFICE)

    assert view["distance_metres"] == 0
    assert view["inside"] is True


def test_thirty_metres_away_is_inside_a_fifty_metre_circle():
    view = location_view(GEOFENCE, **ACROSS_THE_ROAD)

    assert 25 <= view["distance_metres"] <= 35
    assert view["inside"] is True


def test_a_hundred_metres_away_is_outside():
    view = location_view(GEOFENCE, **DOWN_THE_STREET)

    assert view["distance_metres"] > 50
    assert view["inside"] is False


def test_without_an_office_point_nothing_is_inside():
    view = location_view({}, **AT_THE_OFFICE)

    assert view["office_set"] is False
    assert view["inside"] is False


# --------------------------------------------------------------------------- #
# punching, through the API
# --------------------------------------------------------------------------- #
pytestmark_db = pytest.mark.usefixtures("db")


async def _enforce(admin_client, api_prefix, **extra) -> None:
    response = await admin_client.patch(
        f"{api_prefix}/settings",
        json={**GEOFENCE, "enforce_office_location_for_punch": True, **extra},
    )
    assert response.status_code == 200, response.text


async def _employee(admin_client, api_prefix):
    shift = await admin_client.post(f"{api_prefix}/shifts", json=SHIFT)
    assert shift.status_code == 201, shift.text
    session = await invite_and_accept(admin_client, api_prefix, email="worker@example.com")
    return session


async def _punch_in(session, api_prefix, location):
    async with authed_client(session["tokens"]["access_token"]) as client:
        return await client.post(
            f"{api_prefix}/attendance/punch-in",
            json={"location": location} if location else {},
        )


@pytestmark_db
async def test_a_punch_at_the_office_is_accepted(admin_client, api_prefix):
    session = await _employee(admin_client, api_prefix)
    await _enforce(admin_client, api_prefix)

    response = await _punch_in(session, api_prefix, AT_THE_OFFICE)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["sessions"][0]["punch_in"]["outside_office"] is False


@pytestmark_db
async def test_a_punch_from_home_is_refused_with_the_distance(admin_client, api_prefix):
    session = await _employee(admin_client, api_prefix)
    await _enforce(admin_client, api_prefix)

    response = await _punch_in(session, api_prefix, AT_HOME)

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "OFFICE_LOCATION_REQUIRED"
    assert body["error"]["details"]["radius_metres"] == 50
    assert body["error"]["details"]["distance_metres"] > 5_000
    assert "file a correction" in body["message"]


@pytestmark_db
async def test_a_punch_with_no_location_is_refused(admin_client, api_prefix):
    session = await _employee(admin_client, api_prefix)
    # Even with "require location on every punch" off, the geofence needs one.
    await _enforce(admin_client, api_prefix, geolocation_required=False)

    response = await _punch_in(session, api_prefix, None)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "LOCATION_REQUIRED"


@pytestmark_db
async def test_the_ip_address_no_longer_matters(admin_client, api_prefix):
    # The network rule stays off; whatever IP the office has today, a punch at
    # the office point goes through.
    session = await _employee(admin_client, api_prefix)
    await _enforce(admin_client, api_prefix, enforce_office_network_for_punch=False)

    response = await _punch_in(session, api_prefix, ACROSS_THE_ROAD)

    assert response.status_code == 200, response.text


@pytestmark_db
async def test_work_from_home_is_still_allowed(db, admin_client, api_prefix):
    session = await _employee(admin_client, api_prefix)
    await db[Collections.USERS].update_one(
        {"_id": ObjectId(session["user"]["id"])}, {"$set": {"wfh_enabled": True}}
    )
    await _enforce(admin_client, api_prefix)

    response = await _punch_in(session, api_prefix, AT_HOME)

    assert response.status_code == 200, response.text


@pytestmark_db
async def test_with_enforcement_off_a_far_punch_is_only_flagged(admin_client, api_prefix):
    session = await _employee(admin_client, api_prefix)
    await admin_client.patch(f"{api_prefix}/settings", json=GEOFENCE)

    response = await _punch_in(session, api_prefix, AT_HOME)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["sessions"][0]["punch_in"]["outside_office"] is True


@pytestmark_db
async def test_enforcement_cannot_be_switched_on_without_an_office(admin_client, api_prefix):
    response = await admin_client.patch(
        f"{api_prefix}/settings", json={"enforce_office_location_for_punch": True}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "OFFICE_LOCATION_NOT_SET"


@pytestmark_db
async def test_anyone_can_check_their_distance_but_not_the_office_point(
    admin_client, api_prefix
):
    session = await _employee(admin_client, api_prefix)
    await _enforce(admin_client, api_prefix)

    async with authed_client(session["tokens"]["access_token"]) as client:
        response = await client.post(
            f"{api_prefix}/settings/location-check", json=DOWN_THE_STREET
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["inside"] is False
    assert data["enforced"] is True
    assert data["radius_metres"] == 50
    assert "office_latitude" not in data and "latitude" not in data


@pytestmark_db
async def test_settings_report_the_enforcement(admin_client, api_prefix):
    await _enforce(admin_client, api_prefix)

    settings = await admin_client.get(f"{api_prefix}/settings")

    assert settings.json()["data"]["enforce_office_location_for_punch"] is True


# --------------------------------------------------------------------------- #
# the IP rule, left switched on underneath
# --------------------------------------------------------------------------- #
@pytestmark_db
async def test_the_location_rule_overrides_an_ip_rule_left_on(db, admin_client, api_prefix):
    # What production looked like: the IP rule still on, listing an address
    # the office no longer has after a router restart.
    from app.models.settings import SETTINGS_KEY
    from app.services import settings as settings_service

    session = await _employee(admin_client, api_prefix)
    await _enforce(admin_client, api_prefix)
    await db[Collections.SETTINGS].update_one(
        {"key": SETTINGS_KEY},
        {"$set": {"enforce_office_network_for_punch": True, "office_ip_ranges": ["203.0.113.7"]}},
    )
    settings_service.clear_cache()

    response = await _punch_in(session, api_prefix, AT_THE_OFFICE)

    assert response.status_code == 200, response.text


@pytestmark_db
async def test_without_the_location_rule_the_ip_rule_still_applies(admin_client, api_prefix):
    session = await _employee(admin_client, api_prefix)
    await admin_client.patch(
        f"{api_prefix}/settings",
        json={"enforce_office_network_for_punch": True, "office_ip_ranges": ["203.0.113.7"]},
    )

    response = await _punch_in(session, api_prefix, AT_THE_OFFICE)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "OFFICE_NETWORK_REQUIRED"


@pytestmark_db
async def test_turning_the_location_rule_on_switches_the_ip_rule_off(admin_client, api_prefix):
    await admin_client.patch(
        f"{api_prefix}/settings",
        json={"enforce_office_network_for_punch": True, "office_ip_ranges": ["203.0.113.7"]},
    )

    await _enforce(admin_client, api_prefix)

    settings = (await admin_client.get(f"{api_prefix}/settings")).json()["data"]
    assert settings["enforce_office_location_for_punch"] is True
    assert settings["enforce_office_network_for_punch"] is False
    # The correction rule is a separate decision and is left as it was.
