"""Holidays, leave types, organisation settings and the audit trail."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.conftest import invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


async def _fresh_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------- #
# holidays
# --------------------------------------------------------------------------- #
async def test_a_holiday_records_its_year_and_weekday(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/holidays",
        json={"date": "2026-01-26", "name": "Republic Day", "type": "PUBLIC"},
    )

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["year"] == 2026
    assert data["weekday"] == "Monday"


async def test_the_same_holiday_cannot_be_added_twice_on_one_date(admin_client, api_prefix):
    payload = {"date": "2026-01-26", "name": "Republic Day"}
    await admin_client.post(f"{api_prefix}/holidays", json=payload)

    duplicate = await admin_client.post(f"{api_prefix}/holidays", json=payload)

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DUPLICATE_HOLIDAY"


async def test_two_different_holidays_may_share_a_date(admin_client, api_prefix):
    await admin_client.post(
        f"{api_prefix}/holidays", json={"date": "2026-04-14", "name": "Ambedkar Jayanti"}
    )
    second = await admin_client.post(
        f"{api_prefix}/holidays",
        json={"date": "2026-04-14", "name": "Tamil New Year", "type": "OPTIONAL"},
    )

    assert second.status_code == 201


async def test_holidays_can_be_loaded_in_bulk_with_duplicates_reported(admin_client, api_prefix):
    await admin_client.post(
        f"{api_prefix}/holidays", json={"date": "2026-01-01", "name": "New Year"}
    )

    response = await admin_client.post(
        f"{api_prefix}/holidays/bulk",
        json={
            "holidays": [
                {"date": "2026-01-01", "name": "New Year"},
                {"date": "2026-08-15", "name": "Independence Day"},
                {"date": "2026-10-02", "name": "Gandhi Jayanti"},
            ]
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["created"] == 2
    assert data["skipped"] == 1
    assert data["years"] == [2026]


async def test_holidays_can_be_filtered_by_year_and_range(admin_client, api_prefix):
    await admin_client.post(
        f"{api_prefix}/holidays/bulk",
        json={
            "holidays": [
                {"date": "2026-01-01", "name": "New Year"},
                {"date": "2026-12-25", "name": "Christmas"},
                {"date": "2027-01-01", "name": "New Year"},
            ]
        },
    )

    by_year = await admin_client.get(f"{api_prefix}/holidays", params={"year": 2026})
    assert len(by_year.json()["data"]) == 2

    by_range = await admin_client.get(
        f"{api_prefix}/holidays", params={"from_date": "2026-06-01", "to_date": "2027-06-01"}
    )
    assert len(by_range.json()["data"]) == 2


async def test_a_removed_holiday_disappears_from_the_calendar(admin_client, api_prefix):
    created = await admin_client.post(
        f"{api_prefix}/holidays", json={"date": "2026-03-08", "name": "Holi"}
    )
    holiday_id = created.json()["data"]["id"]

    assert (await admin_client.delete(f"{api_prefix}/holidays/{holiday_id}")).status_code == 200
    assert len((await admin_client.get(f"{api_prefix}/holidays")).json()["data"]) == 0


async def test_employees_can_read_holidays_but_not_add_them(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="cal@example.com")

    async with await _fresh_client() as employee:
        employee.headers["Authorization"] = f"Bearer {session['tokens']['access_token']}"
        assert (await employee.get(f"{api_prefix}/holidays")).status_code == 200
        blocked = await employee.post(
            f"{api_prefix}/holidays", json={"date": "2026-05-01", "name": "May Day"}
        )
        assert blocked.status_code == 403


# --------------------------------------------------------------------------- #
# leave types
# --------------------------------------------------------------------------- #
async def test_a_leave_type_is_created_with_a_normalised_code(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/leave-types",
        json={"name": "Casual Leave", "code": "casual leave", "annual_quota": 12},
    )

    assert response.status_code == 201
    assert response.json()["data"]["code"] == "CASUAL_LEAVE"


async def test_carry_forward_cannot_exceed_the_annual_quota(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/leave-types",
        json={"name": "Earned", "code": "EL", "annual_quota": 10, "carry_forward_max": 15},
    )

    assert response.status_code == 422


async def test_an_update_cannot_push_carry_forward_past_the_quota(admin_client, api_prefix):
    created = await admin_client.post(
        f"{api_prefix}/leave-types",
        json={"name": "Earned", "code": "EL", "annual_quota": 10, "carry_forward_max": 5},
    )
    leave_type_id = created.json()["data"]["id"]

    response = await admin_client.patch(
        f"{api_prefix}/leave-types/{leave_type_id}", json={"carry_forward_max": 20}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_CARRY_FORWARD"


async def test_a_leave_type_colour_must_be_a_hex_value(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/leave-types", json={"name": "Sick", "code": "SL", "color": "red"}
    )

    assert response.status_code == 422


async def test_leave_type_codes_are_unique(admin_client, api_prefix):
    await admin_client.post(f"{api_prefix}/leave-types", json={"name": "Sick", "code": "SL"})

    duplicate = await admin_client.post(
        f"{api_prefix}/leave-types", json={"name": "Sick Leave", "code": "sl"}
    )

    assert duplicate.status_code == 409


async def test_an_unused_leave_type_is_deleted_outright(admin_client, api_prefix):
    created = await admin_client.post(
        f"{api_prefix}/leave-types", json={"name": "Sabbatical", "code": "SAB"}
    )
    leave_type_id = created.json()["data"]["id"]

    response = await admin_client.delete(f"{api_prefix}/leave-types/{leave_type_id}")

    assert response.json()["data"]["deleted"] is True
    assert (await admin_client.get(f"{api_prefix}/leave-types/{leave_type_id}")).status_code == 404


async def test_a_referenced_leave_type_is_deactivated_rather_than_deleted(
    admin_client, api_prefix, db
):
    created = await admin_client.post(
        f"{api_prefix}/leave-types", json={"name": "Casual", "code": "CL"}
    )
    leave_type_id = created.json()["data"]["id"]

    # Stand in for a real leave request, which arrives in a later phase.
    await db["leave_requests"].insert_one({"leave_type_id": leave_type_id})

    response = await admin_client.delete(f"{api_prefix}/leave-types/{leave_type_id}")

    data = response.json()["data"]
    assert data["deleted"] is False
    assert data["deactivated"] is True

    # Still resolvable, so historical requests keep their meaning.
    still_there = await admin_client.get(f"{api_prefix}/leave-types/{leave_type_id}")
    assert still_there.status_code == 200
    assert still_there.json()["data"]["is_active"] is False


async def test_inactive_leave_types_are_hidden_unless_asked_for(admin_client, api_prefix):
    created = await admin_client.post(
        f"{api_prefix}/leave-types", json={"name": "Casual", "code": "CL"}
    )
    leave_type_id = created.json()["data"]["id"]
    await admin_client.patch(
        f"{api_prefix}/leave-types/{leave_type_id}", json={"is_active": False}
    )

    assert len((await admin_client.get(f"{api_prefix}/leave-types")).json()["data"]) == 0
    with_inactive = await admin_client.get(
        f"{api_prefix}/leave-types", params={"include_inactive": True}
    )
    assert len(with_inactive.json()["data"]) == 1


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
async def test_settings_are_created_on_first_read(admin_client, api_prefix):
    response = await admin_client.get(f"{api_prefix}/settings")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["correction_window_days"] == 7
    assert data["geolocation_required"] is True
    assert data["block_mobile_punch"] is True
    assert data["timezone"] == "Asia/Kolkata"


async def test_settings_can_be_updated_by_an_administrator(admin_client, api_prefix):
    response = await admin_client.patch(
        f"{api_prefix}/settings",
        json={"correction_window_days": 14, "org_name": "Acme Corp"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["correction_window_days"] == 14

    # And the change is visible on the next read, not just in the response.
    assert (await admin_client.get(f"{api_prefix}/settings")).json()["data"]["org_name"] == "Acme Corp"


async def test_the_timezone_is_read_only(admin_client, api_prefix):
    await admin_client.patch(f"{api_prefix}/settings", json={"timezone": "America/New_York"})

    assert (await admin_client.get(f"{api_prefix}/settings")).json()["data"]["timezone"] == "Asia/Kolkata"


async def test_settings_reject_an_impossible_window(admin_client, api_prefix):
    response = await admin_client.patch(
        f"{api_prefix}/settings", json={"correction_window_days": 500}
    )

    assert response.status_code == 422


async def test_employees_may_read_settings_but_not_change_them(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="settings@example.com")

    async with await _fresh_client() as employee:
        employee.headers["Authorization"] = f"Bearer {session['tokens']['access_token']}"
        assert (await employee.get(f"{api_prefix}/settings")).status_code == 200
        blocked = await employee.patch(f"{api_prefix}/settings", json={"org_name": "Mine"})
        assert blocked.status_code == 403


# --------------------------------------------------------------------------- #
# audit trail
# --------------------------------------------------------------------------- #
async def test_administrative_changes_are_recorded(admin_client, api_prefix):
    created = await admin_client.post(
        f"{api_prefix}/leave-types", json={"name": "Casual", "code": "CL"}
    )
    leave_type_id = created.json()["data"]["id"]
    await admin_client.patch(
        f"{api_prefix}/leave-types/{leave_type_id}", json={"annual_quota": 18}
    )

    logs = await admin_client.get(
        f"{api_prefix}/audit-logs", params={"entity": "LEAVE_TYPE", "entity_id": leave_type_id}
    )

    entries = logs.json()["data"]["items"]
    actions = {entry["action"] for entry in entries}
    assert {"CREATE", "UPDATE"} <= actions

    update = next(entry for entry in entries if entry["action"] == "UPDATE")
    assert update["before"]["annual_quota"] == 0
    assert update["after"]["annual_quota"] == 18
    assert update["actor_email"] == "admin@example.com"


async def test_an_audit_entry_never_carries_a_password_hash(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="audited@example.com")
    await admin_client.patch(
        f"{api_prefix}/users/{session['user']['id']}", json={"designation": "Engineer"}
    )

    logs = await admin_client.get(f"{api_prefix}/audit-logs", params={"entity": "USER"})

    assert "hashed_password" not in logs.text


async def test_an_update_that_changes_nothing_writes_no_audit_entry(admin_client, api_prefix):
    created = await admin_client.post(
        f"{api_prefix}/leave-types", json={"name": "Casual", "code": "CL", "annual_quota": 12}
    )
    leave_type_id = created.json()["data"]["id"]

    await admin_client.patch(
        f"{api_prefix}/leave-types/{leave_type_id}", json={"annual_quota": 12}
    )

    logs = await admin_client.get(
        f"{api_prefix}/audit-logs", params={"entity_id": leave_type_id}
    )
    actions = [entry["action"] for entry in logs.json()["data"]["items"]]
    assert actions == ["CREATE"]


async def test_the_audit_trail_is_admin_only(admin_client, api_prefix):
    session = await invite_and_accept(admin_client, api_prefix, email="curious@example.com")

    async with await _fresh_client() as employee:
        employee.headers["Authorization"] = f"Bearer {session['tokens']['access_token']}"
        response = await employee.get(f"{api_prefix}/audit-logs")

    assert response.status_code == 403
