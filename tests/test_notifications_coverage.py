"""The system has to speak up on its own.

A forgotten punch-out that nobody is told about becomes a correction three days
later; a queue nobody looks at stays full. Both of these fire without anyone
opening the application, which is the point.
"""

from datetime import timedelta

import pytest

from app.core.datetime import date_key, org_today, utc_now
from app.db.mongo import Collections
from app.models.announcement import NotificationType
from app.services import notification as notification_service
from app.services.attendance import close_stale_sessions

pytestmark = pytest.mark.asyncio


async def open_session_yesterday(db, user_id: str) -> str:
    """An attendance day left open on a day that has already ended."""
    yesterday = org_today() - timedelta(days=1)
    document = {
        "user_id": user_id,
        "date": date_key(yesterday),
        "status": "IN_PROGRESS",
        "worked_minutes": 0,
        "sessions": [{"punch_in": {"at": utc_now() - timedelta(days=1)}, "punch_out": None}],
        "is_deleted": False,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    await db[Collections.ATTENDANCE].insert_one(document)
    return document["date"]


async def test_a_forgotten_punch_out_tells_the_employee_the_same_evening(db, admin):
    await open_session_yesterday(db, admin["id"])

    closed = await close_stale_sessions()

    assert closed == 1
    notification = await db[Collections.NOTIFICATIONS].find_one(
        {"user_id": admin["id"], "type": NotificationType.MISSING_PUNCH_OUT.value}
    )
    assert notification is not None
    assert "correction" in notification["body"].lower()


async def test_a_quiet_week_produces_no_digest(db, admin):
    assert await notification_service.weekly_digest() == 0

    assert await db[Collections.NOTIFICATIONS].count_documents({}) == 0


async def test_the_digest_reports_last_week_exceptions_to_administrators(db, admin):
    await open_session_yesterday(db, admin["id"])
    await close_stale_sessions()
    await db[Collections.NOTIFICATIONS].delete_many({})

    told = await notification_service.weekly_digest()

    assert told == 1
    digest = await db[Collections.NOTIFICATIONS].find_one({"user_id": admin["id"]})
    assert digest is not None
    assert "waiting for a decision" in digest["body"]
