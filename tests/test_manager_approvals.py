"""The reporting-manager role: what it decides, and what it may not touch.

A manager is an approver for their own reports and an ordinary employee for
everything else. These tests pin both halves — the powers granted and, just as
importantly, the administration screens the role does *not* open up.
"""

from datetime import timedelta

import pytest

from app.core.datetime import date_key, org_today
from app.models.user import UserRole
from app.services import auth as auth_service
from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")

TODAY = date_key(org_today())


def _yesterday() -> str:
    return date_key(org_today() - timedelta(days=1))


def _future(days: int) -> str:
    return date_key(org_today() + timedelta(days=days))


async def _seed_config(admin_client, api_prefix):
    """A shift and a leave type — the minimum for a request to exist."""
    await admin_client.post(
        f"{api_prefix}/shifts",
        json={
            "name": "General",
            "code": "GENERAL",
            "required_minutes": 540,
            "half_day_minutes": 240,
            "working_weekdays": [0, 1, 2, 3, 4, 5, 6],
        },
    )
    response = await admin_client.post(
        f"{api_prefix}/leave-types",
        json={"name": "Casual Leave", "code": "CL", "annual_quota": 12, "accrual": "YEARLY"},
    )
    assert response.status_code == 201, response.text


async def _person(admin_client, api_prefix, *, email, first_name="Ada", role=None, manager=None):
    """Invite somebody, accept, then apply the role or the reporting line.

    The role is set with a ``PATCH`` rather than on the invitation because the
    invite schema still refuses ``MANAGER`` — see the note in the report.
    """
    session = await invite_and_accept(
        admin_client, api_prefix, email=email, first_name=first_name
    )
    user_id = session["user"]["id"]

    changes = {}
    if role:
        changes["role"] = role
    if manager:
        changes["reporting_manager_id"] = manager
    if changes:
        response = await admin_client.patch(f"{api_prefix}/users/{user_id}", json=changes)
        assert response.status_code == 200, response.text

    return authed_client(session["tokens"]["access_token"]), user_id


async def _team(admin_client, api_prefix):
    """A manager, somebody who reports to them, and somebody who does not."""
    manager, manager_id = await _person(
        admin_client, api_prefix, email="boss@example.com", first_name="Bo", role="MANAGER"
    )
    report, report_id = await _person(
        admin_client,
        api_prefix,
        email="report@example.com",
        first_name="Ravi",
        manager=manager_id,
    )
    outsider, outsider_id = await _person(
        admin_client, api_prefix, email="outsider@example.com", first_name="Sam"
    )
    return (manager, manager_id), (report, report_id), (outsider, outsider_id)


async def _file_correction(client, api_prefix):
    day = _yesterday()
    response = await client.post(
        f"{api_prefix}/corrections",
        json={
            "date": day,
            "type": "FULL_DAY",
            "requested_punch_in": f"{day}T04:00:00Z",
            "requested_punch_out": f"{day}T13:00:00Z",
            "reason": "Badge reader was down at the side entrance.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


async def _apply_for_leave(client, api_prefix, *, days_ahead=3):
    types = await client.get(f"{api_prefix}/leave-types")
    leave_type_id = types.json()["data"][0]["id"]
    day = _future(days_ahead)
    response = await client.post(
        f"{api_prefix}/leave",
        json={
            "leave_type_id": leave_type_id,
            "from_date": day,
            "to_date": day,
            "reason": "A day off for a family function.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


async def _log_task(client, api_prefix):
    response = await client.post(
        f"{api_prefix}/tasks",
        json={
            "date": TODAY,
            "title": "Reconcile the vendor invoices",
            "description": "Matched twelve invoices against the purchase orders.",
            "actual_minutes": 240,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


# --------------------------------------------------------------------------- #
# the role is assignable again
# --------------------------------------------------------------------------- #
async def test_an_administrator_can_promote_somebody_to_manager(admin_client, api_prefix):
    _, user_id = await _person(
        admin_client, api_prefix, email="promoted@example.com", role="MANAGER"
    )

    response = await admin_client.get(f"{api_prefix}/users/{user_id}")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["role"] == "MANAGER"


# --------------------------------------------------------------------------- #
# deciding for a report
# --------------------------------------------------------------------------- #
async def test_a_manager_decides_their_report_s_correction(admin_client, api_prefix):
    await _seed_config(admin_client, api_prefix)
    (manager, _), (report, _), _ = await _team(admin_client, api_prefix)

    async with report:
        correction_id = await _file_correction(report, api_prefix)

    async with manager:
        response = await manager.post(
            f"{api_prefix}/corrections/{correction_id}/approve",
            json={"note": "Confirmed with reception."},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "APPROVED"
    assert data["approver_role"] == "MANAGER"
    assert data["approver_email"] == "boss@example.com"


async def test_a_manager_decides_their_report_s_leave(admin_client, api_prefix):
    await _seed_config(admin_client, api_prefix)
    (manager, _), (report, _), _ = await _team(admin_client, api_prefix)

    async with report:
        request_id = await _apply_for_leave(report, api_prefix)

    async with manager:
        response = await manager.post(
            f"{api_prefix}/leave/{request_id}/approve", json={"note": "Enjoy."}
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "APPROVED"
    assert data["approver_role"] == "MANAGER"


async def test_a_manager_decides_their_report_s_task(admin_client, api_prefix):
    (manager, _), (report, _), _ = await _team(admin_client, api_prefix)

    async with report:
        task_id = await _log_task(report, api_prefix)

    async with manager:
        noted = await manager.post(
            f"{api_prefix}/tasks/{task_id}/comment", json={"note": "Any blockers?"}
        )
        response = await manager.post(
            f"{api_prefix}/tasks/{task_id}/approve", json={"note": "Good detail."}
        )

    assert noted.status_code == 200, noted.text
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "APPROVED"
    assert data["reviewer_role"] == "MANAGER"
    assert [entry["action"] for entry in data["timeline"]] == [
        "CREATED",
        "COMMENTED",
        "APPROVED",
    ]


async def test_a_manager_may_open_a_report_s_task_day(admin_client, api_prefix):
    (manager, _), (report, report_id), (_, outsider_id) = await _team(
        admin_client, api_prefix
    )

    async with report:
        await _log_task(report, api_prefix)

    async with manager:
        mine = await manager.get(f"{api_prefix}/tasks/day", params={"user_id": report_id})
        theirs = await manager.get(
            f"{api_prefix}/tasks/day", params={"user_id": outsider_id}
        )

    assert mine.status_code == 200, mine.text
    assert len(mine.json()["data"]) == 1
    assert theirs.status_code == 403


# --------------------------------------------------------------------------- #
# and nobody else's
# --------------------------------------------------------------------------- #
async def test_a_manager_is_refused_on_somebody_else_s_requests(admin_client, api_prefix):
    await _seed_config(admin_client, api_prefix)
    (manager, _), _, (outsider, _) = await _team(admin_client, api_prefix)

    async with outsider:
        correction_id = await _file_correction(outsider, api_prefix)
        request_id = await _apply_for_leave(outsider, api_prefix)
        task_id = await _log_task(outsider, api_prefix)

    async with manager:
        correction = await manager.post(
            f"{api_prefix}/corrections/{correction_id}/approve", json={}
        )
        leave = await manager.post(f"{api_prefix}/leave/{request_id}/approve", json={})
        task = await manager.post(f"{api_prefix}/tasks/{task_id}/approve", json={})

    for response in (correction, leave, task):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "NOT_YOUR_REPORT"


async def test_a_manager_cannot_decide_their_own_requests(admin_client, api_prefix):
    await _seed_config(admin_client, api_prefix)
    (manager, _), _, _ = await _team(admin_client, api_prefix)

    async with manager:
        correction_id = await _file_correction(manager, api_prefix)
        request_id = await _apply_for_leave(manager, api_prefix)
        task_id = await _log_task(manager, api_prefix)

        correction = await manager.post(
            f"{api_prefix}/corrections/{correction_id}/approve", json={}
        )
        leave = await manager.post(f"{api_prefix}/leave/{request_id}/approve", json={})
        task = await manager.post(f"{api_prefix}/tasks/{task_id}/approve", json={})

    assert correction.status_code == 403
    assert correction.json()["error"]["code"] == "CANNOT_DECIDE_OWN_REQUEST"
    assert leave.status_code == 403
    assert leave.json()["error"]["code"] == "CANNOT_DECIDE_OWN_LEAVE"
    assert task.status_code == 403
    assert task.json()["error"]["code"] == "CANNOT_REVIEW_OWN_TASK"


async def test_an_administrator_decides_a_manager_s_own_request(admin_client, api_prefix):
    """The fallback for everybody, the manager included."""
    await _seed_config(admin_client, api_prefix)
    (manager, _), _, _ = await _team(admin_client, api_prefix)

    async with manager:
        request_id = await _apply_for_leave(manager, api_prefix)

    response = await admin_client.post(
        f"{api_prefix}/leave/{request_id}/approve", json={"note": "Approved centrally."}
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "APPROVED"
    assert data["approver_role"] == "ADMIN"


# --------------------------------------------------------------------------- #
# queues
# --------------------------------------------------------------------------- #
async def test_the_queues_show_a_manager_only_their_reports(admin_client, api_prefix):
    await _seed_config(admin_client, api_prefix)
    (manager, _), (report, report_id), (outsider, _) = await _team(admin_client, api_prefix)

    async with report:
        await _file_correction(report, api_prefix)
        await _apply_for_leave(report, api_prefix)
        await _log_task(report, api_prefix)
    async with outsider:
        await _file_correction(outsider, api_prefix)
        await _apply_for_leave(outsider, api_prefix)
        await _log_task(outsider, api_prefix)

    async with manager:
        corrections = await manager.get(f"{api_prefix}/corrections")
        leave = await manager.get(f"{api_prefix}/leave")
        tasks = await manager.get(f"{api_prefix}/tasks")

    for response in (corrections, leave, tasks):
        assert response.status_code == 200, response.text
        items = response.json()["data"]["items"]
        assert [item["user_id"] for item in items] == [report_id]


async def test_pending_counts_differ_between_a_manager_and_an_administrator(
    admin_client, api_prefix
):
    await _seed_config(admin_client, api_prefix)
    (manager, _), (report, _), (outsider, _) = await _team(admin_client, api_prefix)

    async with report:
        await _file_correction(report, api_prefix)
        await _apply_for_leave(report, api_prefix)
        await _log_task(report, api_prefix)
    async with outsider:
        await _file_correction(outsider, api_prefix)
        await _apply_for_leave(outsider, api_prefix)
        await _log_task(outsider, api_prefix)

    async with manager:
        manager_counts = [
            (await manager.get(f"{api_prefix}/{path}/pending-count")).json()["data"]["pending"]
            for path in ("corrections", "leave", "tasks")
        ]
    admin_counts = [
        (await admin_client.get(f"{api_prefix}/{path}/pending-count")).json()["data"]["pending"]
        for path in ("corrections", "leave", "tasks")
    ]

    assert manager_counts == [1, 1, 1]
    assert admin_counts == [2, 2, 2]


async def test_a_manager_filtering_the_queue_by_somebody_else_is_refused(
    admin_client, api_prefix
):
    await _seed_config(admin_client, api_prefix)
    (manager, _), _, (outsider, outsider_id) = await _team(admin_client, api_prefix)

    async with outsider:
        await _file_correction(outsider, api_prefix)

    async with manager:
        response = await manager.get(
            f"{api_prefix}/corrections", params={"user_id": outsider_id}
        )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "NOT_YOUR_REPORT"


async def test_an_administrator_still_sees_and_decides_everybody(admin_client, api_prefix):
    """The admin path is untouched by the new role."""
    await _seed_config(admin_client, api_prefix)
    _, (report, report_id), (outsider, _) = await _team(admin_client, api_prefix)

    async with report:
        correction_id = await _file_correction(report, api_prefix)
    async with outsider:
        await _file_correction(outsider, api_prefix)

    queue = await admin_client.get(f"{api_prefix}/corrections")
    filtered = await admin_client.get(
        f"{api_prefix}/corrections", params={"user_id": report_id}
    )
    decided = await admin_client.post(
        f"{api_prefix}/corrections/{correction_id}/approve", json={"note": "Fine."}
    )

    assert queue.json()["data"]["pagination"]["total"] == 2
    assert filtered.json()["data"]["pagination"]["total"] == 1
    assert decided.status_code == 200, decided.text
    assert decided.json()["data"]["approver_role"] == "ADMIN"


# --------------------------------------------------------------------------- #
# what the role does not open up
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "salary/projection",
        "audit-logs",
        "users",
        "auth/invites",
    ],
)
async def test_a_manager_is_refused_on_administration(admin_client, api_prefix, path):
    (manager, _), _, _ = await _team(admin_client, api_prefix)

    async with manager:
        response = await manager.get(f"{api_prefix}/{path}")

    assert response.status_code == 403, response.text


async def test_a_manager_cannot_change_organisation_settings(admin_client, api_prefix):
    (manager, _), _, _ = await _team(admin_client, api_prefix)

    async with manager:
        settings_write = await manager.patch(
            f"{api_prefix}/settings", json={"correction_window_days": 30}
        )
        leave_type_write = await manager.post(
            f"{api_prefix}/leave-types", json={"name": "Study", "code": "SL"}
        )
        shift_write = await manager.post(
            f"{api_prefix}/shifts",
            json={"name": "Night", "code": "NIGHT", "required_minutes": 480},
        )
        holiday_write = await manager.post(
            f"{api_prefix}/holidays", json={"name": "Founders Day", "date": _future(30)}
        )
        calendar_write = await manager.post(
            f"{api_prefix}/work-calendar/overrides",
            json={"date": _future(31), "kind": "WORKING", "reason": "Catch-up day."},
        )

    for response in (
        settings_write,
        leave_type_write,
        shift_write,
        holiday_write,
        calendar_write,
    ):
        assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_the_last_administrator_cannot_be_demoted_to_manager(db, admin, admin_client, api_prefix):
    """Demotion reaches the same end as deletion: nobody left to undo it."""
    response = await admin_client.patch(
        f"{api_prefix}/users/{admin['id']}", json={"role": "MANAGER"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "LAST_ADMIN"


@pytest.mark.asyncio
async def test_an_administrator_can_be_demoted_once_another_exists(
    db, admin, admin_client, api_prefix
):
    second = await auth_service.create_active_user(
        email="second.admin@example.com",
        password="AdminPass123",
        first_name="Second",
        role=UserRole.ADMIN,
    )

    response = await admin_client.patch(
        f"{api_prefix}/users/{second['id']}", json={"role": "MANAGER"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["role"] == "MANAGER"
