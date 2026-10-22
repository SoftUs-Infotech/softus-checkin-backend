"""Announcements, events, notifications and the dashboard summary."""

from datetime import timedelta

import pytest

from app.core.datetime import date_key, org_today
from tests.conftest import authed_client, invite_and_accept

pytestmark = pytest.mark.usefixtures("db")


async def _employee(admin_client, api_prefix, email="reader2@example.com", **invite):
    session = await invite_and_accept(admin_client, api_prefix, email=email, **invite)
    return authed_client(session["tokens"]["access_token"]), session


# --------------------------------------------------------------------------- #
# announcements
# --------------------------------------------------------------------------- #
async def test_an_announcement_reaches_every_employee(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix)

    posted = await admin_client.post(
        f"{api_prefix}/announcements",
        json={"title": "Office closed on Friday", "body": "Maintenance work in the building."},
    )
    assert posted.status_code == 201

    async with client:
        feed = await client.get(f"{api_prefix}/announcements")

    assert len(feed.json()["data"]) == 1
    assert feed.json()["data"][0]["title"] == "Office closed on Friday"


async def test_publishing_an_announcement_notifies_employees(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="notified@example.com")

    await admin_client.post(
        f"{api_prefix}/announcements",
        json={"title": "New attendance policy", "body": "Please read the updated policy."},
    )

    async with client:
        count = await client.get(f"{api_prefix}/notifications/unread-count")
        inbox = await client.get(f"{api_prefix}/notifications")

    assert count.json()["data"]["unread"] == 1
    assert inbox.json()["data"]["items"][0]["type"] == "ANNOUNCEMENT"


async def test_an_announcement_can_be_aimed_at_one_department(admin_client, api_prefix):
    engineering, _ = await _employee(admin_client, api_prefix, email="eng@example.com")
    await admin_client.post(
        f"{api_prefix}/auth/invite",
        json={"email": "sales@example.com", "department": "Sales"},
    )

    # Put the first employee in Engineering.
    async with engineering:
        await engineering.patch(f"{api_prefix}/auth/me", json={"department": "Engineering"})
        await admin_client.post(
            f"{api_prefix}/announcements",
            json={
                "title": "Engineering all-hands",
                "body": "Sprint review at 4pm.",
                "audience_departments": ["Engineering"],
            },
        )
        feed = await engineering.get(f"{api_prefix}/announcements")

    assert len(feed.json()["data"]) == 1


async def test_an_employee_in_another_department_does_not_see_it(admin_client, api_prefix):
    other, _ = await _employee(admin_client, api_prefix, email="marketing@example.com")

    async with other:
        await other.patch(f"{api_prefix}/auth/me", json={"department": "Marketing"})
        await admin_client.post(
            f"{api_prefix}/announcements",
            json={
                "title": "Engineering all-hands",
                "body": "Sprint review at 4pm.",
                "audience_departments": ["Engineering"],
            },
        )
        feed = await other.get(f"{api_prefix}/announcements")

    assert feed.json()["data"] == []


async def test_an_event_needs_a_date(admin_client, api_prefix):
    response = await admin_client.post(
        f"{api_prefix}/announcements",
        json={"title": "Team outing", "body": "Details to follow.", "type": "EVENT"},
    )

    assert response.status_code == 422


async def test_upcoming_events_are_listed_in_date_order(admin_client, api_prefix):
    for offset, title in ((10, "Later event"), (3, "Sooner event")):
        await admin_client.post(
            f"{api_prefix}/announcements",
            json={
                "title": title,
                "body": "See you there.",
                "type": "EVENT",
                "event_date": date_key(org_today() + timedelta(days=offset)),
            },
        )

    response = await admin_client.get(f"{api_prefix}/announcements/upcoming-events")

    titles = [item["title"] for item in response.json()["data"]]
    assert titles == ["Sooner event", "Later event"]


async def test_a_past_event_is_not_listed_as_upcoming(admin_client, api_prefix):
    await admin_client.post(
        f"{api_prefix}/announcements",
        json={
            "title": "Last month's party",
            "body": "It already happened.",
            "type": "EVENT",
            "event_date": date_key(org_today() - timedelta(days=30)),
        },
    )

    response = await admin_client.get(f"{api_prefix}/announcements/upcoming-events")

    assert response.json()["data"] == []


async def test_pinned_announcements_come_first(admin_client, api_prefix):
    await admin_client.post(
        f"{api_prefix}/announcements", json={"title": "Ordinary notice", "body": "Nothing urgent."}
    )
    await admin_client.post(
        f"{api_prefix}/announcements",
        json={"title": "Important notice", "body": "Read this first.", "pinned": True},
    )

    response = await admin_client.get(f"{api_prefix}/announcements")

    assert response.json()["data"][0]["title"] == "Important notice"


async def test_an_unpublished_draft_is_hidden_from_employees(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="nodraft@example.com")

    await admin_client.post(
        f"{api_prefix}/announcements",
        json={"title": "Draft notice", "body": "Not ready yet.", "is_published": False},
    )

    async with client:
        feed = await client.get(f"{api_prefix}/announcements")

    assert feed.json()["data"] == []


async def test_an_employee_cannot_post_an_announcement(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="notauthor@example.com")

    async with client:
        response = await client.post(
            f"{api_prefix}/announcements",
            json={"title": "Unauthorised notice", "body": "Should not be allowed."},
        )

    assert response.status_code == 403


async def test_a_removed_announcement_disappears(admin_client, api_prefix):
    created = await admin_client.post(
        f"{api_prefix}/announcements", json={"title": "Temporary notice", "body": "Short lived."}
    )
    announcement_id = created.json()["data"]["id"]

    await admin_client.delete(f"{api_prefix}/announcements/{announcement_id}")
    feed = await admin_client.get(f"{api_prefix}/announcements")

    assert feed.json()["data"] == []


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #
async def test_an_approval_decision_notifies_the_employee(admin_client, api_prefix):
    await admin_client.post(
        f"{api_prefix}/shifts",
        json={"name": "General", "code": "GENERAL", "working_weekdays": [0, 1, 2, 3, 4, 5, 6]},
    )
    client, _ = await _employee(admin_client, api_prefix, email="decided@example.com")
    day = date_key(org_today() - timedelta(days=1))

    async with client:
        created = await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Client visit with no computer access.",
            },
        )
    correction_id = created.json()["data"]["id"]
    await admin_client.post(
        f"{api_prefix}/corrections/{correction_id}/approve", json={"note": "Verified."}
    )

    token = (
        await admin_client.post(
            f"{api_prefix}/auth/login",
            json={"email": "decided@example.com", "password": "Employee123"},
        )
    ).json()["data"]["tokens"]["access_token"]

    async with authed_client(token) as employee:
        inbox = await employee.get(f"{api_prefix}/notifications")

    items = inbox.json()["data"]["items"]
    assert any(item["type"] == "CORRECTION_DECIDED" for item in items)
    assert any("approved" in item["title"] for item in items)


async def test_a_pending_request_notifies_administrators(admin_client, api_prefix):
    await admin_client.post(
        f"{api_prefix}/shifts",
        json={"name": "General", "code": "GENERAL", "working_weekdays": [0, 1, 2, 3, 4, 5, 6]},
    )
    client, _ = await _employee(admin_client, api_prefix, email="asker@example.com")
    day = date_key(org_today() - timedelta(days=1))

    async with client:
        await client.post(
            f"{api_prefix}/corrections",
            json={
                "date": day,
                "type": "FULL_DAY",
                "requested_punch_in": f"{day}T04:00:00Z",
                "requested_punch_out": f"{day}T13:00:00Z",
                "reason": "Needs an administrator to look at it.",
            },
        )

    inbox = await admin_client.get(f"{api_prefix}/notifications")

    assert any(
        item["type"] == "APPROVAL_PENDING" for item in inbox.json()["data"]["items"]
    )


async def test_notifications_can_be_marked_read(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="reads@example.com")
    await admin_client.post(
        f"{api_prefix}/announcements", json={"title": "Something to read", "body": "Content."}
    )

    async with client:
        inbox = await client.get(f"{api_prefix}/notifications")
        notification_id = inbox.json()["data"]["items"][0]["id"]

        await client.post(f"{api_prefix}/notifications/{notification_id}/read")
        count = await client.get(f"{api_prefix}/notifications/unread-count")

    assert count.json()["data"]["unread"] == 0


async def test_everything_can_be_marked_read_at_once(admin_client, api_prefix):
    client, _ = await _employee(admin_client, api_prefix, email="readall@example.com")
    for index in range(3):
        await admin_client.post(
            f"{api_prefix}/announcements",
            json={"title": f"Notice number {index}", "body": "Content."},
        )

    async with client:
        marked = await client.post(f"{api_prefix}/notifications/read-all")
        count = await client.get(f"{api_prefix}/notifications/unread-count")

    assert marked.json()["data"]["updated"] == 3
    assert count.json()["data"]["unread"] == 0


async def test_an_employee_sees_only_their_own_notifications(admin_client, api_prefix):
    first, _ = await _employee(admin_client, api_prefix, email="inbox1@example.com")
    await admin_client.post(
        f"{api_prefix}/announcements", json={"title": "Broadcast", "body": "To everyone."}
    )

    async with first:
        inbox = await first.get(f"{api_prefix}/notifications")

    # One announcement notification each, not one per employee in one inbox.
    assert inbox.json()["data"]["pagination"]["total"] == 1


# --------------------------------------------------------------------------- #
# the dashboard summary
# --------------------------------------------------------------------------- #
async def test_the_dashboard_summary_answers_the_whole_right_rail(admin_client, api_prefix):
    await admin_client.post(f"{api_prefix}/leave-types", json={"name": "Casual", "code": "CL"})
    await admin_client.post(
        f"{api_prefix}/holidays",
        json={"date": date_key(org_today() + timedelta(days=20)), "name": "Founders Day"},
    )
    # The employee must exist before the announcement, or there is nobody to notify.
    client, _ = await _employee(admin_client, api_prefix, email="dash@example.com")
    await admin_client.post(
        f"{api_prefix}/announcements",
        json={
            "title": "Quarterly town hall",
            "body": "Everyone welcome.",
            "type": "EVENT",
            "event_date": date_key(org_today() + timedelta(days=7)),
        },
    )

    async with client:
        response = await client.get(f"{api_prefix}/dashboard/summary")

    data = response.json()["data"]
    assert len(data["announcements"]) == 1
    assert len(data["upcoming_events"]) == 1
    assert len(data["upcoming_holidays"]) == 1
    assert data["leave_balances"][0]["leave_type_code"] == "CL"
    assert data["unread_notifications"] == 1
    # Approval counts are for administrators only.
    assert "pending" not in data


async def test_an_administrator_also_gets_the_approval_counts(admin_client, api_prefix):
    response = await admin_client.get(f"{api_prefix}/dashboard/summary")

    pending = response.json()["data"]["pending"]
    assert set(pending) == {"corrections", "leave", "tasks"}
