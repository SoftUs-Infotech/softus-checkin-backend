"""Daily tasks: logging, editing, review and the timeline."""

from datetime import timedelta

import pytest

from app.core.datetime import date_key, org_today
from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")

TODAY = date_key(org_today())


async def _employee(admin_client, api_prefix, email="logger@example.com"):
    session = await invite_and_accept(admin_client, api_prefix, email=email)
    return authed_client(session["tokens"]["access_token"]), session


def _task_body(**overrides):
    return {
        "date": TODAY,
        "title": "Implement the punch widget",
        "description": "Wired the timer and the device check into the dashboard.",
        "actual_minutes": 300,
        "project": "Attendance",
        **overrides,
    }


async def _log_task(client, api_prefix, **overrides):
    response = await client.post(f"{api_prefix}/tasks", json=_task_body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()["data"]


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
async def test_an_employee_logs_a_task_and_the_timeline_starts(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        task = await _log_task(client, api_prefix)

    assert task["status"] == "SUBMITTED"
    assert len(task["timeline"]) == 1
    assert task["timeline"][0]["action"] == "CREATED"
    assert task["timeline"][0]["actor_name"]


async def test_several_tasks_can_be_logged_for_one_day(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        await _log_task(client, api_prefix, title="Morning: code review")
        await _log_task(client, api_prefix, title="Afternoon: bug fixing")
        today = await client.get(f"{api_prefix}/tasks/today")

    assert len(today.json()["data"]) == 2


async def test_a_task_cannot_be_logged_for_a_future_date(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix)
    tomorrow = date_key(org_today() + timedelta(days=1))

    async with client:
        response = await client.post(
            f"{api_prefix}/tasks", json=_task_body(date=tomorrow)
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FUTURE_DATE"


async def test_a_very_short_title_is_rejected(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        response = await client.post(f"{api_prefix}/tasks", json=_task_body(title="x"))

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# editing
# --------------------------------------------------------------------------- #
async def test_an_employee_can_edit_their_own_task(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix)

    async with client:
        task = await _log_task(client, api_prefix)
        updated = await client.patch(
            f"{api_prefix}/tasks/{task['id']}", json={"actual_minutes": 420}
        )

    assert updated.status_code == 200
    data = updated.json()["data"]
    assert data["actual_minutes"] == 420
    assert data["timeline"][-1]["action"] == "UPDATED"


async def test_one_employee_cannot_edit_anothers_task(admin_client, api_prefix):
    owner, _ = await _employee(admin_client, api_prefix, email="owner2@example.com")
    async with owner:
        task = await _log_task(owner, api_prefix)

    intruder, _ = await _employee(admin_client, api_prefix, email="intruder2@example.com")
    async with intruder:
        response = await intruder.patch(
            f"{api_prefix}/tasks/{task['id']}", json={"title": "Stealing this task"}
        )

    assert response.status_code == 404


async def test_an_approved_task_cannot_be_edited(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="settled@example.com")
    async with client:
        task = await _log_task(client, api_prefix)

    await admin_client.post(f"{api_prefix}/tasks/{task['id']}/approve", json={})

    async with authed_client(
        (
            await admin_client.post(
                f"{api_prefix}/auth/login",
                json={"email": "settled@example.com", "password": "Employee123"},
            )
        ).json()["data"]["tokens"]["access_token"]
    ) as employee:
        response = await employee.patch(
            f"{api_prefix}/tasks/{task['id']}", json={"actual_minutes": 999}
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_NOT_EDITABLE"


async def test_an_approved_task_cannot_be_deleted(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="permanent@example.com")
    async with client:
        task = await _log_task(client, api_prefix)

    await admin_client.post(f"{api_prefix}/tasks/{task['id']}/approve", json={})

    async with authed_client(
        (
            await admin_client.post(
                f"{api_prefix}/auth/login",
                json={"email": "permanent@example.com", "password": "Employee123"},
            )
        ).json()["data"]["tokens"]["access_token"]
    ) as employee:
        response = await employee.delete(f"{api_prefix}/tasks/{task['id']}")

    assert response.status_code == 409


async def test_a_submitted_task_can_be_deleted(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="removable@example.com")

    async with client:
        task = await _log_task(client, api_prefix)
        deleted = await client.delete(f"{api_prefix}/tasks/{task['id']}")
        remaining = await client.get(f"{api_prefix}/tasks/today")

    assert deleted.status_code == 200
    assert remaining.json()["data"] == []


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #
async def test_an_administrator_approves_a_task_with_a_note(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="review1@example.com")
    async with client:
        task = await _log_task(client, api_prefix)

    response = await admin_client.post(
        f"{api_prefix}/tasks/{task['id']}/approve", json={"note": "Good detail, thanks."}
    )

    data = response.json()["data"]
    assert data["status"] == "APPROVED"
    assert data["reviewer_email"] == "admin@example.com"
    assert data["reviewer_role"] == "ADMIN"
    assert data["timeline"][-1]["action"] == "APPROVED"
    assert data["timeline"][-1]["note"] == "Good detail, thanks."


async def test_requesting_changes_reopens_the_task_for_editing(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="rework@example.com")
    async with client:
        task = await _log_task(client, api_prefix)

    await admin_client.post(
        f"{api_prefix}/tasks/{task['id']}/request-changes",
        json={"note": "Please add the ticket number."},
    )

    token = (
        await admin_client.post(
            f"{api_prefix}/auth/login",
            json={"email": "rework@example.com", "password": "Employee123"},
        )
    ).json()["data"]["tokens"]["access_token"]

    async with authed_client(token) as employee:
        resubmitted = await employee.patch(
            f"{api_prefix}/tasks/{task['id']}",
            json={"description": "Wired the timer. Ticket ATT-114."},
        )

    data = resubmitted.json()["data"]
    assert data["status"] == "SUBMITTED"
    actions = [entry["action"] for entry in data["timeline"]]
    assert actions == ["CREATED", "CHANGES_REQUESTED", "RESUBMITTED"]


async def test_a_rejected_task_records_the_reason(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="rejected@example.com")
    async with client:
        task = await _log_task(client, api_prefix)

    response = await admin_client.post(
        f"{api_prefix}/tasks/{task['id']}/reject",
        json={"note": "This work belongs to a different project."},
    )

    data = response.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["timeline"][-1]["note"] == "This work belongs to a different project."


async def test_an_employee_cannot_review_tasks(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="notreviewer@example.com")

    async with client:
        task = await _log_task(client, api_prefix)
        response = await client.post(f"{api_prefix}/tasks/{task['id']}/approve", json={})

    assert response.status_code == 403


async def test_approving_the_same_task_twice_is_refused(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="double@example.com")
    async with client:
        task = await _log_task(client, api_prefix)

    await admin_client.post(f"{api_prefix}/tasks/{task['id']}/approve", json={})
    again = await admin_client.post(f"{api_prefix}/tasks/{task['id']}/approve", json={})

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "TASK_ALREADY_IN_STATE"


async def test_an_administrator_cannot_approve_their_own_task(admin_client, api_prefix):
    created = await admin_client.post(f"{api_prefix}/tasks", json=_task_body())
    task_id = created.json()["data"]["id"]

    response = await admin_client.post(f"{api_prefix}/tasks/{task_id}/approve", json={})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CANNOT_REVIEW_OWN_TASK"


# --------------------------------------------------------------------------- #
# the timeline
# --------------------------------------------------------------------------- #
async def test_comments_build_up_a_conversation_without_changing_status(
    admin_client, api_prefix
):
    client, _ = await _employee(admin_client, api_prefix, email="chat@example.com")
    async with client:
        task = await _log_task(client, api_prefix)
        await client.post(
            f"{api_prefix}/tasks/{task['id']}/comment",
            json={"note": "Adding context: this took longer than planned."},
        )

    response = await admin_client.post(
        f"{api_prefix}/tasks/{task['id']}/comment", json={"note": "Noted, thanks."}
    )

    data = response.json()["data"]
    assert data["status"] == "SUBMITTED"
    assert [entry["action"] for entry in data["timeline"]] == [
        "CREATED",
        "COMMENTED",
        "COMMENTED",
    ]
    assert data["timeline"][-1]["actor_role"] == "ADMIN"


async def test_a_stranger_cannot_comment_on_a_task(admin_client, api_prefix):
    owner, _ = await _employee(admin_client, api_prefix, email="owner3@example.com")
    async with owner:
        task = await _log_task(owner, api_prefix)

    stranger, _ = await _employee(admin_client, api_prefix, email="stranger@example.com")
    async with stranger:
        response = await stranger.post(
            f"{api_prefix}/tasks/{task['id']}/comment", json={"note": "Butting in."}
        )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #
async def test_an_employee_sees_only_their_own_tasks(admin_client, api_prefix):
    first, _ = await _employee(admin_client, api_prefix, email="list1@example.com")
    async with first:
        await _log_task(first, api_prefix)

    second, _ = await _employee(admin_client, api_prefix, email="list2@example.com")
    async with second:
        await _log_task(second, api_prefix)
        mine = await second.get(f"{api_prefix}/tasks/mine")

    assert mine.json()["data"]["pagination"]["total"] == 1

    queue = await admin_client.get(f"{api_prefix}/tasks")
    assert queue.json()["data"]["pagination"]["total"] == 2


async def test_the_queue_can_be_filtered_by_status(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="filter@example.com")
    async with client:
        first = await _log_task(client, api_prefix, title="Task one for filtering")
        await _log_task(client, api_prefix, title="Task two for filtering")

    await admin_client.post(f"{api_prefix}/tasks/{first['id']}/approve", json={})

    submitted = await admin_client.get(f"{api_prefix}/tasks", params={"status": "SUBMITTED"})
    approved = await admin_client.get(f"{api_prefix}/tasks", params={"status": "APPROVED"})

    assert submitted.json()["data"]["pagination"]["total"] == 1
    assert approved.json()["data"]["pagination"]["total"] == 1


async def test_the_pending_count_drives_the_admin_badge(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="counter@example.com")
    async with client:
        await _log_task(client, api_prefix)

    response = await admin_client.get(f"{api_prefix}/tasks/pending-count")

    assert response.json()["data"]["pending"] == 1


# --------------------------------------------------------------------------- #
# what the administrator can see
# --------------------------------------------------------------------------- #
async def test_the_submission_time_is_on_the_record_not_just_the_day(
    admin_client, api_prefix
):
    client, _ = await _employee(admin_client, api_prefix, email="stamped@example.com")
    async with client:
        task = await _log_task(client, api_prefix)

    queue = await admin_client.get(f"{api_prefix}/tasks")
    listed = queue.json()["data"]["items"][0]

    assert listed["date"] == TODAY
    assert listed["created_at"] == task["created_at"]
    assert listed["created_at"].startswith(TODAY[:4])
    assert listed["timeline"][0]["at"]


async def test_every_status_change_records_who_when_and_what(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="trail@example.com")
    async with client:
        task = await _log_task(client, api_prefix)

    await admin_client.post(
        f"{api_prefix}/tasks/{task['id']}/request-changes", json={"note": "Expand it."}
    )
    response = await admin_client.post(
        f"{api_prefix}/tasks/{task['id']}/reject", json={"note": "Wrong project."}
    )

    data = response.json()["data"]
    last = data["timeline"][-1]
    assert (last["from_state"], last["to_state"]) == ("CHANGES_REQUESTED", "REJECTED")
    assert last["actor_role"] == "ADMIN"
    assert last["actor_name"] and last["at"]
    assert data["reviewed_at"]
