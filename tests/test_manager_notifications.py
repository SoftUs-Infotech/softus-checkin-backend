"""A request should reach the person it is actually waiting on.

Notifying only administrators means a reporting manager learns about their own
team's requests by going to look, which is the habit the notification exists to
remove.
"""

from datetime import timedelta

import pytest
from bson import ObjectId

from app.core.datetime import date_key, org_today
from app.db.mongo import Collections
from app.models.announcement import NotificationType
from app.services import notification as notification_service

pytestmark = pytest.mark.asyncio


async def make_user(db, *, email: str, role: str, manager_id: str | None = None) -> str:
    result = await db[Collections.USERS].insert_one(
        {
            "email": email,
            "role": role,
            "status": "ACTIVE",
            "is_deleted": False,
            "first_name": email.split("@")[0],
            "last_name": "",
            "reporting_manager_id": manager_id,
        }
    )
    return str(result.inserted_id)


async def notified(db, user_id: str) -> int:
    return await db[Collections.NOTIFICATIONS].count_documents({"user_id": user_id})


async def announce(subject_id: str) -> None:
    await notification_service.notify_approvers(
        subject_id,
        type=NotificationType.APPROVAL_PENDING,
        title="Leave awaiting approval",
        body=f"A request for {date_key(org_today() + timedelta(days=1))}.",
        link="/admin/approvals",
    )


async def test_the_reporting_manager_is_told(db, admin):
    manager = await make_user(db, email="manager@example.com", role="MANAGER")
    employee = await make_user(db, email="report@example.com", role="EMPLOYEE", manager_id=manager)

    await announce(employee)

    assert await notified(db, manager) == 1


async def test_administrators_are_still_told(db, admin):
    manager = await make_user(db, email="manager2@example.com", role="MANAGER")
    employee = await make_user(db, email="report2@example.com", role="EMPLOYEE", manager_id=manager)

    await announce(employee)

    assert await notified(db, admin["id"]) == 1


async def test_somebody_with_no_manager_only_reaches_the_administrators(db, admin):
    employee = await make_user(db, email="orphan@example.com", role="EMPLOYEE")

    await announce(employee)

    assert await notified(db, admin["id"]) == 1
    assert await db[Collections.NOTIFICATIONS].count_documents({}) == 1


async def test_a_manager_is_not_told_about_their_own_request(db, admin):
    manager = await make_user(db, email="manager3@example.com", role="MANAGER")
    await db[Collections.USERS].update_one(
        {"_id": ObjectId(manager)}, {"$set": {"reporting_manager_id": manager}}
    )

    await announce(manager)

    assert await notified(db, manager) == 0


async def test_a_manager_who_has_been_suspended_is_not_told(db, admin):
    manager = await make_user(db, email="manager4@example.com", role="MANAGER")
    employee = await make_user(db, email="report4@example.com", role="EMPLOYEE", manager_id=manager)
    await db[Collections.USERS].update_one(
        {"_id": ObjectId(manager)}, {"$set": {"status": "SUSPENDED"}}
    )

    await announce(employee)

    assert await notified(db, manager) == 0
