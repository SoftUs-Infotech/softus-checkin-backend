"""Employee-owned progress, and how it stays separate from admin approval."""

from datetime import timedelta

import pytest

from app.core.datetime import date_key, org_today
from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")

TODAY = date_key(org_today())


async def _employee(admin_client, api_prefix, email):
    session = await invite_and_accept(admin_client, api_prefix, email=email)
    return authed_client(session["tokens"]["access_token"])


def _task_body(**overrides):
    return {
        "date": TODAY,
        "title": "Rework the punch widget",
        "actual_minutes": 120,
        **overrides,
    }


async def _log_task(client, api_prefix, **overrides):
    response = await client.post(f"{api_prefix}/tasks", json=_task_body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _set_progress(client, api_prefix, task_id, progress, **body):
    return await client.patch(
        f"{api_prefix}/tasks/{task_id}/progress", json={"progress": progress, **body}
    )


# --------------------------------------------------------------------------- #
# transitions
# --------------------------------------------------------------------------- #
async def test_a_new_task_starts_as_to_do(admin_client, api_prefix):
    client = await _employee(admin_client, api_prefix, "prog1@example.com")

    async with client:
        task = await _log_task(client, api_prefix)

    assert task["progress"] == "TO_DO"
    assert task["completed_on"] is None
    assert task["carried_from_date"] == TODAY


async def test_progress_walks_through_every_state_and_the_timeline_records_it(
    admin_client, api_prefix
):
    client = await _employee(admin_client, api_prefix, "prog2@example.com")

    async with client:
        task = await _log_task(client, api_prefix)
        for step in ("IN_PROGRESS", "BLOCKED", "IN_PROGRESS", "DONE"):
            response = await _set_progress(client, api_prefix, task["id"], step)
            assert response.status_code == 200, response.text

    data = response.json()["data"]
    assert data["progress"] == "DONE"
    assert data["completed_on"] == TODAY
    # The approval status is untouched: progress is the employee's word, not a
    # second way of approving the record.
    assert data["status"] == "SUBMITTED"

    moves = [
        (entry["from_state"], entry["to_state"])
        for entry in data["timeline"]
        if entry["action"] == "PROGRESS_CHANGED"
    ]
    assert moves == [
        ("TO_DO", "IN_PROGRESS"),
        ("IN_PROGRESS", "BLOCKED"),
        ("BLOCKED", "IN_PROGRESS"),
        ("IN_PROGRESS", "DONE"),
    ]
    assert all(entry["actor_name"] and entry["at"] for entry in data["timeline"])


async def test_a_note_can_ride_along_with_a_progress_change(admin_client, api_prefix):
    client = await _employee(admin_client, api_prefix, "prog3@example.com")

    async with client:
        task = await _log_task(client, api_prefix)
        response = await _set_progress(
            client, api_prefix, task["id"], "BLOCKED", note="Waiting on the API key."
        )

    assert response.json()["data"]["timeline"][-1]["note"] == "Waiting on the API key."


async def test_setting_the_progress_it_already_has_is_refused(admin_client, api_prefix):
    client = await _employee(admin_client, api_prefix, "prog4@example.com")

    async with client:
        task = await _log_task(client, api_prefix)
        response = await _set_progress(client, api_prefix, task["id"], "TO_DO")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_PROGRESS_UNCHANGED"


async def test_an_unknown_progress_value_is_rejected(admin_client, api_prefix):
    client = await _employee(admin_client, api_prefix, "prog5@example.com")

    async with client:
        task = await _log_task(client, api_prefix)
        response = await _set_progress(client, api_prefix, task["id"], "NEARLY_THERE")

    assert response.status_code == 422


async def test_reopening_a_done_task_clears_the_day_it_was_finished(
    admin_client, api_prefix
):
    client = await _employee(admin_client, api_prefix, "prog6@example.com")

    async with client:
        task = await _log_task(client, api_prefix)
        await _set_progress(client, api_prefix, task["id"], "DONE")
        response = await _set_progress(client, api_prefix, task["id"], "IN_PROGRESS")

    data = response.json()["data"]
    assert data["completed_on"] is None
    assert data["progress"] == "IN_PROGRESS"


# --------------------------------------------------------------------------- #
# who may move it
# --------------------------------------------------------------------------- #
async def test_another_employee_cannot_move_your_progress(admin_client, api_prefix):
    owner = await _employee(admin_client, api_prefix, "owner-p@example.com")
    async with owner:
        task = await _log_task(owner, api_prefix)

    intruder = await _employee(admin_client, api_prefix, "intruder-p@example.com")
    async with intruder:
        response = await _set_progress(intruder, api_prefix, task["id"], "DONE")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_NOT_FOUND"


async def test_an_administrator_cannot_move_somebody_elses_progress(
    admin_client, api_prefix
):
    client = await _employee(admin_client, api_prefix, "notyours@example.com")
    async with client:
        task = await _log_task(client, api_prefix)

    response = await admin_client.patch(
        f"{api_prefix}/tasks/{task['id']}/progress", json={"progress": "DONE"}
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# progress is not bound to the day the task was logged
# --------------------------------------------------------------------------- #
async def test_progress_can_be_moved_on_a_later_day(admin_client, api_prefix):
    client = await _employee(admin_client, api_prefix, "later@example.com")
    logged = date_key(org_today() - timedelta(days=4))

    async with client:
        task = await _log_task(client, api_prefix, date=logged)
        response = await _set_progress(client, api_prefix, task["id"], "DONE")

    data = response.json()["data"]
    assert data["date"] == logged
    assert data["completed_on"] == TODAY
    assert data["days_open"] == 4


async def test_progress_still_moves_after_the_task_has_been_approved(
    admin_client, api_prefix
):
    client = await _employee(admin_client, api_prefix, "approved-p@example.com")
    async with client:
        task = await _log_task(client, api_prefix)

    approved = await admin_client.post(f"{api_prefix}/tasks/{task['id']}/approve", json={})
    assert approved.status_code == 200

    token = (
        await admin_client.post(
            f"{api_prefix}/auth/login",
            json={"email": "approved-p@example.com", "password": "Employee123"},
        )
    ).json()["data"]["tokens"]["access_token"]

    async with authed_client(token) as employee:
        response = await _set_progress(employee, api_prefix, task["id"], "DONE")

    data = response.json()["data"]
    assert response.status_code == 200
    assert data["status"] == "APPROVED"
    assert data["progress"] == "DONE"
