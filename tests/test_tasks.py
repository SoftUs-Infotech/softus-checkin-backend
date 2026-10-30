"""Daily tasks: logging, editing, review and the timeline."""


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
